# Replay: how it works

The replay is `Replay` in `esp32tl/sources/replay.py`, plan §4.4's stream case. It reads nothing
itself: it takes a `Recording` from `esp32tl/recording.py`
([recording format](../recording-format.md)). The `Uart` interface of `esp32tl/interfaces/uart.py`
puts it on the link, and the `gnss` fixture hands that interface to the test
([pytest plugin](pytest-plugin-technical.md)). What the user sees is in
[replay-user.md](replay-user.md).

## Data flow

1. **`board` fixture.** It passes the test file's directory to `Esp32Sim`, which makes a `Uart`
   for every UART route of the profile, all on the link and without a source
   ([esp32sim backend](esp32sim-backend-technical.md#data-flow)).
2. **`gnss.replay(path)`**, in the test body, before `board.power_on()`: `Uart.replay` refuses a
   UART that has a source already (`SourceError`), then calls
   `asked(Request(name, source="replay"))`, through which the backend refuses a source after
   power-on or one no backend gives (`SourceError`), and skips the test when its own capability
   declaration does not give `replay` on the route ([board
   profiles](board-profiles-technical.md#data-flow)). It refuses a file a route is tapped into,
   which `Uart.files` holds (`SourceError`), but not one another route replays, then reads
   `directory / path` with `recording.read`, builds
   `Replay(recording, self.name, self.clock, name=str(path))`, and gives it: under a lock, a UART
   that has a source already raises `SourceError`; otherwise `Uart.source` is set,
   `Uart.source_kind` is `"replay"` and `Uart.source_detail` the path as the test gave it, and the
   run's log, when the UART has one, gets `{name}.replay({path!r})` at 0.
3. **`Replay.__init__`** refuses, in this order, a recording of another interface, one with a
   clock other than `clock` anywhere (in its header, an event or a clock switch), one with an
   `out` event, and one whose `end` is not `hold` (`ReplayError`). Then it keeps the events as
   `(t, data)` pairs, in order, in `Replay.events`, and `sent = 0`.
4. **The link's turns.** From `board.power_on()`, `BoardLink` calls the `Uart` from its thread, which hands each call to
   its source ([link protocol](../protocol.md#7-latency-is-bound-to-the-source)):
   - `poll(now)` returns every event from `sent` on whose `t` is at or before `now`, and moves
     `sent` past them. Events of one time go out together, in recorded order.
   - `quiet_until()` is the `t` of `events[sent]`, `None` once all are sent. After `poll(now)`
     it is later than `now`, so the runner's next block point is the next event's time, or the
     end of the test's wait when that is sooner: each event goes out in the turn at its own `t`,
     stamped `t`.
   - `latency()` is `None`: a stream answers no request (protocol §7), so the firmware's
     transmissions never bring a turn forward.
   - `receive(t, data)` returns nothing.

   `BoardLink` turns each output into a `uart_rx` on the wire's RX pin at its baud, the board
   profile's `[uart.gnss]` `rx` and `baud`; the runner paces the bytes (protocol §6, rule 5). On
   the way the `Uart` hands every output, and every byte the firmware transmitted, to its tap and
   the run's log ([run-report-technical.md](run-report-technical.md#data-flow)).

Events are sent in their turn rather than all at once in the first one, which the runner would
queue (protocol §8): `quiet_until` then comes from the recording, as plan §4.4 and protocol §7
have it. The cost is one turn per distinct event time
([decision](../decisions/harness.md#a-stream-replay-sends-each-event-in-the-turn-at-its-time)).

## Interfaces

| Name | What |
|---|---|
| `Replay(recording, interface, clock, name="")` | The stream replay of `recording` on `interface`, whose times are on `clock`; `name` is the recording in the errors, `the recording` when empty. Attributes `events` (the `(t, data)` pairs) and `sent` |
| `Replay.receive(t, data)`, `poll(now)`, `quiet_until()`, `latency()` | The source's side of the `Interface` methods of `esp32tl/link/boardlink.py`, without the wire |
| `ReplayError` | A recording that cannot be replayed there |
| `Output` | `tuple[int, bytes]`: from `t`, these bytes |

## Failure modes

`ReplayError`, from `Replay.__init__`, `{name}` the path the test gave:

| Message | Cause |
|---|---|
| `{name} is a recording of {header.interface}, not of {interface}` | Another interface |
| `{name} has times on the {clocks} clock, and {interface} runs on the {clock} clock: replaying it would compare times across clocks` | `{clocks}`: every clock of the header, the events and the clock switches, sorted, joined by ` and `; refused unless it is `clock` alone, so a recording that leaves the clock and comes back is refused too |
| `{name} has events out of the ESP32, the first at {t} ns: it holds requests, and replay answers none yet, only streams with events in` | An `out` event |
| `{name} ends with {end}: replay holds at the end only` | `end: loop` |

An event stamped before the turn it is polled in cannot come from a `Replay` the link drives,
since its `quiet_until` stops the runner at the next event; were it to, `BoardLink` would fail the
link with `uart.gnss sent bytes stamped {t} ns in the turn at {now} ns: ...`.

## Tests

- `tests/test_replay.py`, without an emulator: the events going out at their times, together when
  they share one, and `quiet_until` before, between and after them; a stream not listening;
  each `ReplayError`; a `Uart` with a `Replay` on a `BoardLink`, from `replay("r.jsonl")`
  relative to its directory, and the source asked for, to the `uart_rx` and `wait` of each turn;
  a second source, and one the backend refuses, leaving the UART without one; a recording that
  cannot be read or replayed leaving it without a source;
  and `gnss-generated-1.jsonl` replayed through the turns the runner would take, one event a
  second from 1 s to 60 s.
- `tests/test_gnss_fixture.py`, on the real runner and `nmea_reader`:
  `test_a_replay_reaches_nmea_reader_on_the_gnss_route` expects the first two fixes of
  `gnss-generated-1.jsonl` and no warning;
  `test_a_source_given_out_of_order_fails_the_test` runs, through `pytester`, an expect before
  power-on, a replay after it, a replay never powered on (passing its call, an error at teardown,
  no runner) and a profile without `uart.gnss` (an error at setup), which its `conftest.py` makes
  because every profile esp32tl ships has the route;
  `test_reports_a_fix_fails_on_a_changed_recording` and
  `test_reports_a_fix_fails_on_a_baud_mismatch` run a copy of `tests/test_nmea_reader.py`'s
  `test_reports_a_fix` on a recording with one digit changed and with the profile's route at
  115200 baud, and require it to fail
  ([validation firmware](validation-firmware-technical.md)).
- `tests/test_nmea_reader.py`, the first milestone's tests: the fixes of both generated
  recordings, ten emulated seconds in under five wall seconds with `board.now` just past 10 s, and
  the sentences `nmea_reader` must ignore, all through `gnss.replay` before `board.power_on()`.
- `tests/test_boardlink.py::test_a_frame_error_fails_the_run_at_once`: a `frm_err` warning is the
  link's error, with the interface's name and both bauds, or the pin alone, and calls `on_error`
  at once; `test_other_warnings_fail_nothing`: the other kinds fail nothing.
- `tests/test_pytest_plugin.py::test_a_source_or_a_tap_the_backend_cannot_give_skips_at_the_call`: the
  skip at the call, and the `SourceError` for a source no backend gives, against a declaration a
  conftest swaps in.
- `tests/test_cli.py`: `esp32tl replay` on `nmea_reader`, and its exit code 1 on a baud mismatch.

Not covered: a recording whose events reach the UART before the firmware has configured it (the
generated ones start at 1 s), and two replays on one board, since no profile has two `uart.*`
routes that a test can replay into.

## See also

- [Replay](replay-user.md)
- [Recording format](../recording-format.md)
- [Plan section 4.4](../implementation-plan.md#44-matching-engine), the matching engine whose
  stream case this is
- [Link protocol, section 7](../protocol.md#7-latency-is-bound-to-the-source)
