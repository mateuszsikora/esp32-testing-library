# Real source: how it works

The design is [section 12 of the link protocol](../protocol.md#12-wall-time---realtime-and-the-real-source-m5),
which holds the rules and why they are so. This document says what the code does. The user's view
is [real-source-user.md](real-source-user.md).

Code: `esp32tl/sources/real.py`, `Uart.real` in `esp32tl/interfaces/uart.py`, `Esp32Sim.clock`
(`esp32tl/backends/esp32sim.py`), and the runner's half in `runner/src/session.rs`
([runner-technical.md](runner-technical.md#--realtime)).

## Data flow

1. `Uart.real(port)` asks for a `real` source through `_ask`, which on `esp32sim` runs
   `Esp32Sim._asked`: refused after power-on, then capability checks, then `_refuse_replays`, which
   refuses a replay already given on another route. On `serial`, `_asked` skips here, so the port is
   not opened.
2. `Real.__init__` opens the port with `serial.Serial(port, baud, timeout=READ_SECONDS,
   exclusive=True)`, where `exclusive` is pyserial's advisory `flock`, and
   starts two daemon threads, `_read_port` and
   `_write_port`.
   `Uart._give` makes it the route's source; if that loses to another source given at the same time,
   `real` closes it.
3. `Esp32Sim.clock` is a property: `wall` while any route's `source_kind` is `real`, else
   `emulated-fifo-write`. A call that failed gave no source, so it moves nothing. Every `Uart` of the
   backend asks it when a replay is given, and `BoardLink` when the runner says hello, through a
   `clock` that may be a callable. `power_on` passes `realtime=self.clock == recording.WALL` to
   `runner_command`, which adds `--realtime`. The runner's `hello` then says `wall`.
4. `_read_port` reads whatever the port has, up to `READ_SECONDS` at a time, into `_read` under the
   lock. It keeps at most `_carried(backlog_ns)` bytes, the most a turn can send, so a long hold costs
   no memory; what it trims goes into `_cut` once `_turned` is set, by the first `poll`, and each
   `poll` moves `_cut` into `dropped`. Before the first turn there is no run to lose bytes, and after
   the last none to lose them from. Any exception is kept in `_failed` and ends the thread.
5. Each turn, `BoardLink._turn` calls `Uart.poll(now)`, which calls `Real.poll(now)`:
   - raises `SourceError` if the thread kept a failure;
   - takes all of `_read`;
   - works out `start = max(now, _line_free)` and `room = _carried(now + backlog_ns - start)`, the
     bytes the line carries by the bound, as rule 5 of the protocol paces them;
   - keeps the newest `room` bytes, adds the rest to `dropped`, and advances `_line_free` by the
     floor of `n × 10 × 10⁹ / baud`, the runner's own arithmetic (`line_free(pin)`); `_carried` and
     `_on_the_line` share `CHAR_NS_AT_1_BAUD`, since each is the other's inverse;
   - returns one `(now, data)`, or nothing.
6. `Uart._sent` records the output in the tap and the run's log, so neither holds a dropped byte.
7. `Real.quiet_until()` returns `(now // MS + 1) * MS`, the next whole millisecond, where the runner
   ends a slice anyway. `latency()` is `None`.
8. For each `uart_tx` on the route, `BoardLink._uart_tx` calls `Interface.transmitted(t, baud, data)` before
   `receive`. `Uart.transmitted` hands it to a source that is `RateBound`, a protocol as `Supplied`
   is, which `Real` is; `Real.transmitted` raises `SourceError` on a `None`
   baud or one further from the port's than 3 % of the port's, `UartRoute::matches_baud`'s test. Then
   `Real.receive` queues the bytes, and returns. `_write_port` writes the queue into the port's
   non-blocking descriptor with `os.write`, so a turn never waits for the port: esp32sim's UART reads as idle (`esp-periph/src/uart.rs:2`), and the
   runner coalesces what the firmware sends in a burst into a `uart_tx` of up to 48 KiB. Any exception,
   `SerialTimeoutException` among them, goes into `_failed` through `_fail`, as in `_read_port`, and
   ends the thread.
9. `Esp32Sim.close` calls `_release` in its `finally`, which closes the SD card and every `Real`,
   each whatever another raises, and reports what raised unless the end of the run has failed first.
   `Real.close` sets `_closing`, wakes the writer with `None`, joins both threads,
   the reader within `READ_SECONDS`, discards what the driver holds, and closes the port; what is still
   queued is not written. The flush is for Linux, where closing a tty can wait for its buffer for up to
   a driver's `closing_wait`, 30 s by default for usb-serial; that comes from the kernel's documentation
   and is not measured here. On macOS, measured on a CH9102, closing takes no time with or without it. Then it
   raises what is in `_failed`: `BoardLink._turn` returns `Stop` before it polls the sources, so a
   failure after the last turn has no other way out. It may be called twice.

`transmitted` is its own hook because `Interface.configured` gives one baud per wire, the receiving
route's when two ports share it, and `receive` has none.

`Real` has no `supply`: the device is not on the board's power, and keeps sending while the board is
off, which the runner counts as `unrouted_rx`.

## Interfaces

| Name | What |
|---|---|
| `Real(name, port, baud)` | Opens the port and starts reading. `SourceError` if it cannot, `TypeError` for a `port` that is not a non-empty string |
| `Real.dropped` | Bytes dropped so far. Written under the lock from both threads; reading it for a report is fine |
| `Real.poll`, `receive`, `quiet_until`, `latency` | The `Source` protocol of `esp32tl/interfaces/uart.py` |
| `Real.transmitted(t, baud)` | Refuses a transmission the port cannot carry |
| `Real.close()` | Ends the reading, closes the port; idempotent |
| `Uart.real(port)` | The test's call |
| `Interface.transmitted(t, baud, data)` | The link's hook, a no-op by default; called before `receive` for each `uart_tx` |

## Configuration

| Name | Value | Meaning |
|---|---|---|
| `BACKLOG_NS` | 50 ms | How far behind the line may fall, unless `BURST_BYTES` takes longer on it. `Real.backlog_ns` is the larger of the two |
| `BURST_BYTES` | 128 | Two packets of a full-speed USB adapter, which can hand over 64 bytes at once while the line still carries the last 64. A CH9102 at 9600 baud delivers 64 bytes every 67 ms |
| `READ_SECONDS` | 0.05 | How long a read blocks before the thread looks at `_closing` |
| `WRITE_SECONDS` | 5 | How long the port may take no byte before the run fails at its next turn |
| `WRITE_RETRY_SECONDS` | 0.005 | How often the writing thread tries a port that took nothing. It does not wait in `select`, as pyserial's `write` does after every write: Linux says a tty is writable only once its buffer has nearly drained, under 256 characters, so that wait lasts as long as the whole buffer takes on the line, about 19 KiB for a pty |

## Failure modes

The messages are in the [user's table](real-source-user.md#when-the-test-gets-it-wrong). What raises
on the link's thread goes through `calling` into `LinkError`, then `BoardLink.error`, so the run ends
at once with `stop` through `on_error`, as for any interface that raises.

A burst into a line at the bound loses its head, since `room` counts only what the line carries by
`now + backlog_ns`. That includes the case a real device can hit: an answer sent right after a hold,
into the backlog, within less time than its own first bytes take on the line. This is the
design's cut (protocol 12.3), and `tests/test_real_source.py` sends its answer a byte at a time after
a pause of three characters for that reason. A stall of the host, a turn longer than the bound on a
loaded machine, cuts the same way. A turn costs 200 to 300 µs of wall time on an M2 Pro, loaded or not, so the
source's 1000 turns a second leave the guest room to keep up.

`os.openpty` stands in for the adapter in the tests and hides what a real adapter does on its modem
lines at open: pyserial leaves DTR and RTS at their defaults, which can reset a device that wires them
to its reset. A T-A7670G does not reset on that open, on its CH9102, and does when they are cleared.

A `uart_tx` a `RateBound` source refuses still reaches the tap and the run's log, from
`Uart.transmitted`, since the firmware did transmit it, and does not reach `receive`, so not the port.

## Tests

`tests/test_real_source.py`, no hardware, a pseudo-terminal pair as the adapter:

- The source alone, milliseconds: a turn's stamp and bytes; `quiet_until` at the next whole
  millisecond; no loss at the line rate, nor with whole USB packets as a CH9102 delivers them; the
  bound, with the exact bytes kept and the exact count dropped; the line never more than the bound behind; the reader's own bound; what came before the
  first turn or after the last not counted; a burst larger than the port takes not holding the turn, and reaching the
  device in order; a slow port that makes progress not failing; a stalled one failing the next turn;
  closing without waiting for what is queued, discarding what the driver holds, and raising a failure
  no turn raised, once; settings the port refuses; writes reaching the
  port; the 3 % at both edges, and no baud; a failed port; a port that cannot be opened, is not named,
  or is taken; a second close.
- On the `Uart` and `BoardLink`, milliseconds: the protocol's millisecond, exactly
  (`test_on_the_link_a_byte_gets_the_time_of_the_next_turn_within_a_millisecond`: each turn's
  `quiet_until` and the stamp of a byte between two turns); the tap's events and `source`; the run
  failing on another baud, with its bytes in the tap and the log and not in the port's queue.
- The backend's refusals and clock, with `nmea_reader` or `modem_at`: `wall` from the call on, and
  nothing moved by a call that failed; only a real source passes `--realtime`; one source per route;
  before power-on; replays before and after; one port that cannot be closed not keeping another open.
- Whole runs, a few seconds each, whose devices say a sentence again until it is heard, since a stall
  of a busy host costs bytes: the firmware reads a sentence from the device while the run follows
  wall time, with every recording on the wall clock; most stamps within 10 ms of their arrivals, around
  the run's offset between the clocks, and all within the 500 ms a guest may lag
  (`test_in_a_paced_run_bytes_are_stamped_close_to_their_arrival`, the margin being the host's
  scheduler, and skipped when the run skipped a lag, whose premise is a guest that keeps up); a turn
  held before the first 2¹⁶ bus cycles still waits at the
  block points; a turn held for 2 s against a device at its full line rate, with the bytes dropped
  counted, a resync, and an answer reaching the guest with the line within the bound; `modem_at` and
  the device talking both ways on `lilygo-t-a7670g-r2`.
- `test_a_receiver_on_a_usb_adapter` is the validation with real hardware, and runs only with
  `ESP32TL_REAL_GNSS_PORT` set ([testing](../guides/testing.md#real-device-checks)).

`tests/test_serial_backend.py::test_serial_capabilities_skip_at_the_call` checks the skip on `serial`.

## See also

- [Link protocol, section 12](../protocol.md#12-wall-time---realtime-and-the-real-source-m5)
- [Runner: `--realtime`](runner-technical.md#--realtime)
- [Harness decisions](../decisions/harness.md)
