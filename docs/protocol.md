# Link protocol: runner ⇄ harness

The contract between `esp32tl-runner` (the Rust crate in `runner/`, which runs the guest in
esp32sim) and the harness (`esp32tl/link/`, `BoardLink`), or any other peer that takes the
runner's board slot. Protocol version 1. It writes down section 4.1 of the
[implementation plan](implementation-plan.md) and the parts of 4.2 a peer needs, checked against
esp32sim at the project's pin, `8c65215`. Where this document and the plan differ, this document
holds; section 11 lists the differences and why.

The protocol follows the `SocketBoard` proposed in mikroverk/esp32sim#325, so the runner can be
replaced by upstream's once that lands (section 10). Messages marked *ext* are runner extensions
that #325 does not cover; they stay in the runner whatever happens upstream (plan, finding 10).

Who implements what: the runner side in #11 (M1: UART, `reset`, `uart_cfg`, `warn`, `stats`) and
#23 (M3: GPIO, `adc`, the future-input queue); the harness side in #12.

## 1. Transport

The harness listens and the runner connects, once, at start:

| Runner flag | Transport |
|---|---|
| `--board socket:PATH` | Unix stream socket at `PATH`. The default. Keep `PATH` short: `sun_path` holds 104 bytes on macOS, 108 on Linux |
| `--board tcp:HOST:PORT` | TCP, for a runner in a Linux container (a Unix socket does not cross Docker Desktop's file sharing on macOS) |

The harness binds before it spawns the runner, so one connection attempt is enough; if it fails
the runner exits with code 4 (section 9). One socket carries one run. The flag names copy those
proposed in #325, so a switch to `esp32sim --board socket:PATH` is a rename.

## 2. Framing

- One JSON object per line (NDJSON): UTF-8, terminated by `\n`, no newline inside the object.
  A line is at most 1 MiB; a longer one is a protocol error.
- Every object has `"type"`, the message name. An unknown `type` is a protocol error. Unknown
  fields are ignored, so a field can be added without a version change; anything else that
  changes the meaning of a message changes the version.
- Byte payloads (`data`) are base64 with padding (RFC 4648, section 4).
- Times (`t`, `quiet_until`, `latency`) are integers in emulated nanoseconds (section 3). JSON
  numbers above 2⁵³ lose precision in some parsers; 2⁵³ ns is 104 emulated days.
- Pins are the chip's GPIO numbers. Levels are `0` or `1`. Pin sets are JSON arrays of pin
  numbers, not bit masks.
- For a field that may be `null`, `null` and an absent field mean the same.

## 3. Time

All times are emulated nanoseconds since the runner created the machine (power-on). The clock
does not restart at a guest reset or a wake from deep sleep: the runner keeps the same machine,
and `reboot()` leaves the bus cycle counter alone.

The runner counts time in bus cycles, `bus.cycles()`. They run at a fixed reference independent
of the momentary CPU clock (`tick` scales through `cpu_scale_half`, `esp32/src/bus.rs:631`), so
one conversion holds for the whole run, across the XTAL to PLL switch at boot:

```
ns     = floor(cycles × 10⁹ / CPU_HZ)      runner → harness
cycles = ceil(ns × CPU_HZ / 10⁹)           harness → runner
```

`CPU_HZ` is `esp32::periph::CPU_HZ` (`esp32/src/periph.rs:18`), 240 000 000 on the classic chip,
the same constant `Machine::seconds` divides by (`esp-soc/src/machine.rs:166`). There the
fraction reduces to `ns = floor(cycles × 25 / 6)` and `cycles = ceil(ns × 6 / 25)`.
Implementations compute in 128-bit integers or with the reduced fraction: `cycles × 10⁹`
overflows 64 bits after 76 emulated seconds.

The rounding directions are chosen so that nothing the protocol promises is lost to rounding:
an event at cycle `c` is reported at `t ≤` its exact time, and an input stamped `t` takes effect
at the first cycle at or after `t`. One cycle is longer than a nanosecond, so a cycle converted to
nanoseconds and back is the same cycle, and an answer stamped exactly `t_event + latency` is
never earlier than the cycle the runner computed from the same sum.

A peer never needs `CPU_HZ`: every time on the socket is in nanoseconds.

## 4. Session

```
runner                                   harness
  hello  ─────────────────────────────▶
         ◀─────────────────────────────  hello
  sync t=0 ───────────────────────────▶                       turn 0
         ◀─────────────────────────────  inputs…, wait
  (the guest boots and runs to the block point B₁)
  events with t ≤ B₁, sync t=B₁ ──────▶                       turn 1
         ◀─────────────────────────────  inputs…, wait
  …
  events, stats, close                                        end of run
```

**Handshake.** The runner sends `hello` first; the harness checks `version` and answers with its
own `hello`, also when it has found a mismatch; the runner checks `version` and `chip`. A
mismatch ends the run with exit code 3 whichever side found it first: the harness's `hello`
carries its version, so the runner finds it too. The harness then reads to end of file, as at
every end of a run, so the runner's `stats` is written before the socket closes.

**Turns.** After the handshake the runner and the peer take turns. The runner's turn ends with a
`sync` whose `t` is the block point `B` it stopped at (rule 1). The peer's turn follows: any number
of input messages, then exactly one `wait` (or `stop`, section 9), which ends it. The runner reads
the socket only between its `sync` and the peer's `wait`, never while the guest runs, so how fast
a peer answers in wall time cannot change what the guest sees (section 6 of the plan: two runs of
one recording give identical output).

Outside its turn a peer reads and does not write. The runner cannot tell an early message from
the next turn's, so it would be checked against that turn's `sync`; and a peer blocked on writing
while the runner is blocked on writing `console` or `uart_tx` to it is a deadlock.

The first `sync` has `t = 0` and comes before the guest executes its first instruction (rule 4).

**Ordering.** The runner's messages are in non-decreasing `t`. Before the `sync` at `B` the
runner sends every event with `t ≤ B` and nothing later; what the guest did after `B` before the
runner got control (section 6.1) goes out after the peer's `wait`. So in each turn the peer has
seen exactly the events up to the block point, however the emulator schedules its work.

## 5. Messages

| Direction | Message | Fields | Since |
|---|---|---|---|
| both | `hello` | runner: `version`, `chip`, `clock`, `esp32sim`; harness: `version`, `chip`, `board`, `watch` | M1 |
| runner → harness | `uart_tx` | `seq`, `t`, `port`, `tx_pins`, `baud`, `data` | M1 |
| runner → harness | `uart_cfg` *ext* | `seq`, `t`, `port`, `tx_pins`, `rx_pin`, `baud` | M1 |
| runner → harness | `gpio_out` | `seq`, `t`, `changes`, `enabled`, `output` | M3 |
| runner → harness | `reset` | `seq`, `t`, `cause` | M1 |
| runner → harness | `sync` | `t` | M1 |
| runner → harness | `console` | `t`, `data` | M1 |
| runner → harness | `warn` *ext* | `t`, `kind`, `detail`, and per kind `port`, `pin`, `baud`, `bytes` | M1 |
| runner → harness | `stats` *ext* | `t`, `end`, `resets`, `turns`, `uart`, `unrouted_rx_bytes`, `inputs_on_output`, `inputs_not_applied`, `rx_bytes_not_paced` | M1 |
| harness → runner | `uart_rx` | `t`, `pin`, `baud`, `data` | M1 |
| harness → runner | `gpio_in` | `t`, `levels` | M3 |
| harness → runner | `release` | `t`, `pins` | M3 |
| harness → runner | `adc` *ext* | `t`, `pin`, `volts` | M3 |
| harness → runner | `wait` | `ack`, `quiet_until`, `latency` | M1 |
| harness → runner | `stop` | none | M1 |

Until #23 lands, the runner answers `gpio_in`, `release` and `adc` with a protocol error naming
the message, and sends no `gpio_out`.

**Events** are the runner messages that carry `seq`: `uart_tx`, `uart_cfg`, `gpio_out` and
`reset`. They are what a peer may answer, so they set the block point (rule 1). `seq` is one
counter over all four, starting at 1 and growing by 1. `sync`, `console`, `warn` and `stats` carry
no `seq` and ask for nothing.

### 5.1 `hello`

```json
{"type": "hello", "version": 1, "chip": "esp32", "clock": "emulated-fifo-write",
 "esp32sim": "8c652151dd5a521249a785ca147f20a9f8eda68f"}
{"type": "hello", "version": 1, "chip": "esp32", "board": "esp32-devkitc", "watch": [4, 5]}
```

The first line is the runner's, the second the harness's.

- `version`: 1. Both sides require equality; there is no negotiation.
- `chip`: `esp32`, the classic chip, the only one for now. The harness sends the chip of its board
  profile and the runner refuses another.
- `clock`: what the times of `uart_tx` mean, in the vocabulary of the recording header (plan 4.3).
  `emulated-fifo-write`: emulated time, and a transmitted byte is stamped when the guest writes
  the UART FIFO, not when it would leave the wire (`esp32/src/bus.rs:308-311`). `wall` is reserved
  for the design of the `real` source on the emulator (#27).
- `esp32sim`: the full revision the runner was built against, for the recording header's `tools`.
- `board`: the harness's board profile name. The runner does not interpret it; its board is the
  socket.
- `watch`: the pins the peer is wired to. The runner forwards only what touches them: `uart_tx`
  for routes with a watched TX pin, `uart_cfg` for routes with a watched TX or RX pin (a
  receive-only peer learns its baud from it), `gpio_out` for watched pins. Inputs (`uart_rx`,
  `gpio_in`, `release`, `adc`) on a pin outside `watch` are a protocol error: the socket's board is
  wired to those pins only. The console's UART0 pins are normally not watched, so console output
  costs no turns; it has its own `console` message. An empty or absent `watch` makes the socket a
  board with nothing on it.

### 5.2 `uart_tx`

```json
{"type": "uart_tx", "seq": 7, "t": 1500000000, "port": 1, "tx_pins": [26], "baud": 115200,
 "data": "QVQN"}
```

Bytes the guest wrote into the TX FIFO of `port` (0 to 2), with the route as it was at the write:
`tx_pins`, every enabled, non-inverted output the UART's TX signal reaches (more than one under
GPIO matrix fan-out), and `baud`, `null` while the UART's clock divider is zero. `tx_pins` is the
route's full set, watched or not, as in `uart_cfg`: `watch` decides whether the message is sent,
not what it says.

The trait reports one byte at a time (`BoardModel::uart_tx`); the runner coalesces consecutive
bytes into one message. `t` is the time of the first byte. A message ends, and the next byte
starts a new one, when:

- the route changes (`port`, `tx_pins` or `baud`);
- any other runner message is due, so the stream stays in `t` order;
- the next byte comes more than one character time after the previous one, at the route's baud
  (10 bit times, 8N1), when the baud is known. Two requests the firmware sends apart thus stay
  apart, and a recording made through the tap keeps each burst's own time;
- the block point is reached: bytes written at or before `B` go out before the `sync`, later ones
  start the next message.

Message boundaries carry no meaning beyond the `t` of each message's first byte: they depend on the
`--console` mode and on the peer's `latency` and `quiet_until`. Consumers (the matching engine, the
tap) join the bytes of one route and find requests by their protocol's terminator.

### 5.3 `uart_cfg` *ext*

```json
{"type": "uart_cfg", "seq": 3, "t": 312456789, "port": 1, "tx_pins": [4], "rx_pin": 5,
 "baud": 9600}
```

A route's configuration, read with `bus.periph.uart_route(port)` (`esp32/src/periph.rs:817`). The
runner compares the routes at every slice boundary (section 6.1, at most one emulated millisecond
apart) and in every `uart_tx` callback, which carries the route. It sends `uart_cfg` when a port's
route differs from the last one it sent for that port and either of the two touches a watched pin;
before the first, every port counts as having no pins and `null` baud. `t` is where the runner saw
the change, at most one millisecond after the firmware made it. A peer on a receive-only route
(GNSS: the firmware never transmits) learns the baud from it instead of trusting the board
profile; a mismatch is otherwise a silent frame error.

`rx_pin` and `baud` are `null` until the firmware configures them; that is the valid initial
state, not an error. A guest reset sends the reset routes again.

### 5.4 `gpio_out` (M3)

```json
{"type": "gpio_out", "seq": 9, "t": 2000000000, "changes": [[4, 1]], "enabled": [4], "output": [4]}
```

What `BoardModel::gpio_output_at` reports, restricted to watched pins and sent when a watched pin's
output level or output enable changes: `changes`, the `[pin, level]` pairs in the order written;
`enabled`, the watched pins whose output driver is enabled after the change; `output`, the watched
pins whose output latch is 1. An enable-only change (an open-drain line released) comes with empty
`changes`.

### 5.5 `reset`

```json
{"type": "reset", "seq": 12, "t": 4100000000, "cause": 12}
```

The guest reset, from the run loop: `run` returned `Stop::SwReset`, and `cause` is what
`reboot()` returned (`esp-soc/src/machine.rs:306`), the ROM's reset reason (`RESET_REASON` in
ESP-IDF's `esp_rom/esp32/include/esp32/rom/rtc.h`). The classic chip in esp32sim raises
3 (`SW_RESET`), 5 (`DEEPSLEEP_RESET`, the wake from deep sleep), 9 (`RTCWDT_SYS_RESET`),
12 (`SW_CPU_RESET`, `esp_restart`), 13 (`RTCWDT_CPU_RESET`) and 16 (`RTCWDT_RTC_RESET`). The
power-on at the start of the run sends no `reset`.

The board persists across a reset, as on a real board where a firmware restart does not reset the
modem: levels the peer drives stay driven, queued inputs stay queued, and paced bytes keep coming.
The chip does not: `reboot()` replaces the peripherals (`esp32/src/soc.rs:138`), so the RX FIFOs
are empty, and bytes that arrive before the firmware routes its UART again are dropped as
`unrouted_rx`. Right after `reset` the runner sends `uart_cfg` for every watched route that
changed and, from M3, a `gpio_out` with the watched pins' state as read from the bus after
`reboot()` (not assumed: IO_MUX can drive a pin, U0TXD on GPIO 1 for one, without its bit in
`GPIO_ENABLE`), since the reset itself calls no `gpio_output_at`.

### 5.6 `sync`

```json
{"type": "sync", "t": 2000000000}
```

Ends the runner's turn: the guest has reached the block point `B = t`, and the peer's turn
begins. The peer may stamp its inputs no earlier than `t` (rule 2). `t` never decreases from one
`sync` to the next; it repeats only with a `latency` of `0` and an event at the same nanosecond,
and the guest has run in between.

### 5.7 `console`

```json
{"type": "console", "t": 812345678, "data": "SGVsbG8gd29ybGQhCg=="}
```

Bytes the guest wrote to UART0, sent only with `--console socket` or `--console both`; by default
the console goes to stdout, where pytest-embedded's `DuplicateStdoutPopen` reads it. Coalesced like
`uart_tx`; a message also ends after a `\n`. Not an event: it has no `seq` and sets no block
point.

### 5.8 `warn` *ext*

```json
{"type": "warn", "t": 1000000000, "kind": "unrouted_rx", "pin": 5, "bytes": 72,
 "detail": "uart_rx on GPIO 5, which no UART receives on: 72 bytes dropped"}
```

What the emulator dropped or refused and the peer could not see otherwise. `detail` is for
people; the fields are for code. Kinds in version 1:

| `kind` | When | Fields |
|---|---|---|
| `rxfifo_ovf` | a paced byte found the 128-byte RX FIFO full and was dropped (`esp-periph/src/uart.rs:43-46`) | `port`, `pin`, `bytes` |
| `frm_err` | the route's baud differs from the `uart_rx` message's by more than 3 % of the message's, or is `null` (`UartRoute::matches_baud`, `esp-soc/src/uart.rs:21`); the UART raises `FRM_ERR` and drops the byte | `port`, `pin`, `baud` (the route's), `bytes` |
| `unrouted_rx` | a `uart_rx` byte on a pin no UART receives on ("Unrouted input is dropped", `esp-soc/src/board.rs:61`) | `pin`, `bytes` |
| `input_on_output` | `gpio_in` on a pin the guest drives as output; the level is still applied | `pin` |

The runner detects the drops itself, in its run loop where it has the bus: the route and
`matches_baud` before it delivers a byte, the FIFO's `rx_pending()` against its 128 bytes. A byte
is delivered at a slice boundary `T` at or after its paced time `dₖ` (6.1), and the drop's `t` is
`T`, where the runner saw it, as for `uart_cfg`. `uart_pin_input` hands a byte to every UART whose
RX pin matches (`esp-soc/src/uart.rs:73-79`), so with two ports on one pin there is one `warn` per
port.

Consecutive drops of one kind on one pin and port are merged into one `warn` with their count in
`bytes` and the first drop's `t`. The merged `warn` goes out as soon as any other message is due,
so the stream stays in `t` order, and at the latest at the next whole emulated millisecond, a
slice boundary in any case, so the last warnings of a run are not held back. Not at every slice
end: while bytes are paced each byte ends a slice, and a 1500-byte frame overflowing the FIFO would
give some 1400 warnings instead of one. A peer accepts kinds it
does not know. `BoardLink` puts warnings in the test report.

### 5.9 `stats` *ext*

```json
{"type": "stats", "t": 12000000000, "end": "max_seconds", "resets": 0, "turns": 13,
 "uart": [{"port": 1, "tx_bytes": 0, "rx_bytes": 864, "rxfifo_ovf": 0, "frm_err": 0}],
 "unrouted_rx_bytes": 72, "inputs_on_output": 0, "inputs_not_applied": 0,
 "rx_bytes_not_paced": 0}
```

The last message of a run, sent just before the runner closes the socket (section 9). `t` is
where the run ended: the end of the last slice, or, after a `stop`, that turn's `sync` `t`. `end`
says why: `max_seconds`, `stop`, `signal`, `emulator` (then also `stop`, the `Stop` variant's name,
and `detail`, its `Debug` form), or `protocol_error` (then also `detail`). `inputs_not_applied`
counts the queued input messages not yet applied when the run ended, and `rx_bytes_not_paced` the
received bytes not yet paced into the chip, so `BoardLink` can tell a scenario that reached past
the end of the run. `uart` has one entry per
port that carried a watched route during the run, with the bytes each way and the drops by kind.
`BoardLink` puts the counts in the test report next to the matching engine's unseen requests.

### 5.10 `uart_rx`

```json
{"type": "uart_rx", "t": 2000000000, "pin": 5, "baud": 9600,
 "data": "JEdQR0dBLDEyMDAwMC4wMCw1MjEzLjAwMDAsTiwwMjEwMC4wMDAwLEUsMSwwOCwwLjksMTAwLjAsTSwzNC4wLE0sLCo2RQ0K"}
```

Bytes the peer sends into the chip on GPIO `pin` at `baud`, its own line rate, a positive integer
(`0` is a protocol error). Peers send whole messages with one time and never pace bytes; the
runner does (rule 5). `t` is when the first byte is complete and enters the RX FIFO, unless the
line is still busy with an earlier message.

### 5.11 `gpio_in` and `release` (M3)

```json
{"type": "gpio_in", "t": 3000000000, "levels": [[33, 1]]}
{"type": "release", "t": 3500000000, "pins": [33]}
```

`gpio_in`: from `t` the peer drives each pin to its level. `release`: from `t` it stops driving the
pins, which go back to their pulls (`BoardModel::released_inputs`; a ring indicator with a
pull-down is the case).

### 5.12 `adc` *ext* (M3)

```json
{"type": "adc", "t": 3000000000, "pin": 34, "volts": 1.25}
```

From `t` the pad sees `volts`, held until the next `adc` on that pin. Applied through the
machine's analog source (`SocBus::analog_set(pin, AnalogSource::Const(volts))`,
`esp32/src/soc.rs:213`), not through the board, because the trait has no analog method (plan,
finding 10). The voltage is at the pad, before the ADC's attenuator.

### 5.13 `wait`

```json
{"type": "wait", "ack": 9, "quiet_until": 3000000000, "latency": 1000000}
```

Ends the peer's turn.

- `ack`: the `seq` of the last event the runner sent before this turn's `sync`, `0` when there has
  been none. Anything else is a protocol error: it means the peer answered without reading.
- `quiet_until`: before this time the peer will produce nothing unprompted. `null` when it has
  nothing scheduled. Must be later than this turn's `sync` `t`.
- `latency`: the shortest time in which the peer answers an event; `0` or more. `null` when it
  does not answer events at all (a GNSS stream with no configuration commands); events then wait
  for the next turn `quiet_until` brings.

Each `wait` replaces the values of the previous one. Every turn acknowledges all earlier events,
so a changed `latency` applies only to events after it, and no promise already made is broken.

### 5.14 `stop`

`{"type": "stop"}`. Ends the peer's turn and the run (section 9). Inputs sent before it in the
same turn are discarded.

## 6. Rules

1. **Block point.** After each `wait` the runner runs the guest until the block point

   ```
   B = min(quiet_until, t_first_unacked + latency)
   ```

   where `t_first_unacked` is the `t` of the first event after the turn. A `null` drops its term;
   while both terms are missing, the runner runs until an event brings one, or to the end of the
   run. It never blocks per byte or per event. At `B` it sends the events up to `B`, then `sync`
   with `t = B`, and reads the peer's turn.

   If the new `B` is already behind the guest (the runner got control a little after the old one,
   6.1, and the peer's `quiet_until` falls in between), the runner takes the next turn at once,
   without running the guest. The gap is under one CPU cycle, but at every clock below 240 MHz,
   ESP-IDF's default of 160 MHz included, it is there, so this case comes up in ordinary runs. A
   block point at or after the end of the run is not taken: the run ends (section 9).

   A peer that acknowledges a request and answers later must keep `quiet_until` at or before the
   answer's `t`, or the runner runs past it and the answer is late under rule 2. The same goes for
   a harness that holds scenario events back instead of sending them early (section 8):
   `BoardLink` folds the next held event into `quiet_until`.

2. **A `t` earlier than the turn's `sync` `t` is a protocol error** that ends the run naming the
   peer: the peer broke its `latency` or `quiet_until` promise. The runner never clamps a late
   time. This is what makes two runs of one recording identical (plan, section 6), as long as
   every `t` a peer sends comes from its recording or its model, never from the wall clock.

   **"At once" means `t_event + latency`.** The peer sees an event in the turn at some
   `B ≤ t_event + latency`, so an answer stamped `t_event + latency` is always on time. One
   stamped `t_event` is late unless the turn happens to fall exactly at `t_event`.

3. **`sync` is the turn**, one per block point and nothing in between. A peer that wants to hear
   from the runner at least every millisecond of emulated time sets `quiet_until` no later than one
   millisecond ahead (the `real` source on the emulator will do this, #27).

4. **The guest boots after the first `wait`.** The runner sends `sync` with `t = 0` before the
   guest's first instruction and waits for the peer's turn, so a peer that comes up slower than
   the firmware does not miss the first configuration bytes, and inputs stamped `t = 0` (initial
   levels) are in place at the first instruction.

5. **The runner paces received bytes** (plan, finding 8). The classic RX FIFO holds 128 bytes and
   drops the rest; `UartInput` carries no per-byte timing. Bytes of a `uart_rx` enter the FIFO one
   character time apart, 10 bit times (8N1) at the message's `baud`:

   ```
   d₀ = max(t, line_free(pin))
   dₖ = d₀ + floor(k × 10 × 10⁹ / baud)            byte k, in ns
   line_free(pin) = d₀ + floor(n × 10 × 10⁹ / baud)  after n bytes
   ```

   `d₀` is computed when the message comes due, in the order of the input queue (section 8), not
   in the order messages arrive: a message sent in a later turn with an earlier `t` goes first. A
   message that comes due while the line is still busy queues behind the earlier one, as on a
   wire. Pacing is the runner's own business and costs no turns. Only 8N1 framing is modelled;
   the emulator checks the baud, not the frame format.

A closed socket, a malformed message, a version or chip mismatch, and a wrong `ack` also end the
run (section 9).

### 6.1 How the runner meets the block point

For the runner's implementer (#11); a peer needs only the consequences at the end.

The `BoardModel` callbacks receive no access to the bus, so the socket work cannot happen inside
them. `SocketBoard`'s callbacks (`uart_tx`, `gpio_output_at`) only record events, with their
cycle, into state the run loop shares. The run loop runs the machine in slices: it sets
`max_cycles` to the slice's end and calls `run`, which returns `Stop::Halted` there, as it does
today at `--max-seconds`. A slice ends at the earliest of `B`, the next paced byte, the next
queued input, the next whole emulated millisecond, and the end of the run, each converted to
cycles (section 3). Between slices, where it has the bus, the runner:

- compares the UART routes (`uart_cfg`);
- delivers the bytes now due with `bus.periph.uart_pin_input`, after checking the route, the
  baud and the FIFO's `rx_pending()`, which is how it sees the drops `warn` reports;
- applies the queued inputs now due (`adc` through `SocBus::analog_set`);
- at `B`, takes the turn.

`run` stops close to `max_cycles`, not a quantum past it: on the path without a cost model the
last round is cut to the cycles left (`esp-soc/src/machine.rs:691`; "the cycle ceiling cuts the
final round short", `:503-504`), and the idle skip is bounded by `max_cycles` too (`:600`). The
round is counted in CPU cycles and rounded up into bus cycles (`cpu_cycles_for_bus`,
`esp32/src/soc.rs:103-105`), so the runner gets control at some `T ≥ B` less than one CPU cycle
later. A CPU cycle is 1 bus cycle at 240 MHz, 1.5 at 160 MHz (ESP-IDF's default for the ESP32),
3 at 80 MHz and 6 at the 40 MHz XTAL the chip boots from (`cpu_scale_half`,
`esp32/src/periph.rs:989-992`), so `T` is in the bus cycle of `B` itself at 240 MHz and up to 1, 2
or 5 bus cycles after it at the others. In nanoseconds `T − B` also holds the rounding of `B` up
to a whole bus cycle (section 3), under 4.2 ns, so it stays below 25 ns. Three consequences, all
hidden from the peer:

- the `sync` still says `t = B`; events in `(B, T]` go out after the peer's turn (section 4);
- an input stamped in `[B, T]` takes effect at `T`, less than one CPU cycle after its `t`;
  deterministic all the same, because the gap depends only on the run so far, not on the peer's
  speed;
- an input stamped after `T` takes effect where the slice that ends at its cycle stops, by the
  same bound.

`Machine::run_until_cycle` (`esp-soc/src/machine.rs:917-929`) stops exactly and would look like the
better tool, but it schedules core 0 only, and the classic ESP32 has two cores.

The slices also replace `BoardModel::next_deadline` as the way to stop for `B` while the guest
idles in WFI waiting for an answer. `SocketBoard` needs no `next_deadline`.

## 7. `latency` is bound to the source

Rule 2 turns `latency` into a promise the source must keep for the whole run:

| Source | `latency` | `quiet_until` |
|---|---|---|
| `replay` | the smallest request-to-answer delay in its recording, computed when the recording is loaded (plan 4.4); a replay never answers sooner. A recording with no request-answer pair (an NMEA stream) declares `null` | the minimum of the next unprompted event and every answer waiting to be sent |
| `model` | the shortest answer delay the model can produce, declared by the model | the model's next unprompted output |
| `real` | designed in #27 | renewed in short slices (rule 3) |

A replay that declared a `latency` larger than an answer in its recording would fail at that
request. A large `latency` costs nothing in correctness and saves turns: the runner coalesces
everything the guest sends within it into one turn. A `latency` of `0` is valid and costs a turn
per event.

When several interfaces share the socket, `BoardLink` sends the minimum `latency` and the minimum
`quiet_until` over them and the scenario timeline, a `null` counting as no bound.

## 8. Future-dated inputs

Any input (`uart_rx`, `gpio_in`, `release`, `adc`) may carry a `t` later than the turn's `sync`:
the runner keeps it in a queue of its own, ordered by `t` and, at equal `t`, by arrival, and
applies it at its time (section 6.1). This is how scenario events with `at=` reach the guest.
The queue is the runner's, not `machine.script`: the script's cursor walks its events in order,
and an appended event would have to be later than everything the cursor has passed, an ordering
the runner should not depend on.

A harness may also hold an input back and send it in a later turn, as long as `quiet_until` stops
the runner at or before its `t` (rule 1).

## 9. Ending a run

| How | Runner does | Exit code |
|---|---|---|
| `--max-seconds` reached (`Stop::Halted`, `esp-soc/src/machine.rs:584`) | sends events not yet sent, `stats` with `end: max_seconds`, closes | 0 |
| `stop` from the peer | drops the events held after `B`, sends `stats` with `end: stop`, closes | 0 |
| `SIGTERM` or `SIGINT` | ends the slice in progress (at most one emulated millisecond), or at once if it is waiting for the peer's turn; sends the events and `warn`s not yet sent, `stats` with `end: signal`, closes | 0 |
| any other `Stop` (`esp-soc/src/soc.rs:9`: `MaxInsns`, `Breakpoint`, `Unimplemented`, `Simcall`, `Ebreak`, `Watch`, `Exceptions`, `CostModel`, `CostModelLifecycle`) | events not yet sent, `stats` with `end: emulator` and the variant, closes | 1 |
| usage error, or an image or ROM that cannot be loaded | nothing on the socket (it may not be connected yet) | 2 |
| protocol error by the peer: malformed or oversized line, unknown `type`, version or chip mismatch, late `t`, wrong `ack`, `quiet_until` not after the `sync`, a `baud` of `0`, a message the runner does not implement yet | `stats` with `end: protocol_error`, closes | 3 |
| the peer is gone: the connection failed, the socket closed, a read or write failed, or the peer's turn took longer than `--peer-timeout` | nothing more | 4 |

Every run ends with one line on stderr naming the cause; a protocol error names the peer's
address and the offending message. The first cause decides the exit code: a final `stats` that
cannot be written, because the peer has already closed the socket, does not turn a 1 or a 3 into a
4.

`stop` reaches the runner only in a turn, and with `quiet_until` and `latency` both `null` the next
turn may be at `--max-seconds`. The usual end of a test is earlier: `console.expect` matches, and
teardown ends the runner. That end belongs to the `esp32sim` backend, not to pytest-embedded:
the runner is the backend's process, pytest-embedded's base `Dut` does not know it, and
pytest-embedded's own teardown calls `terminate()` and `kill()` back to back
(`pytest_embedded/plugin.py:505-506`), which would leave no time for `stats`. The backend's
teardown, in the `board` fixture, runs before the `dut` it depends on is torn down. It sends
`SIGTERM` (`Esp32Sim.close` in `esp32tl/backends/esp32sim.py` already does), reads the socket to
end of file within a wall-time limit (#12), waits for the process, and sends `SIGKILL` only when
the runner has not exited within five wall seconds. It gets `stats` and the last warnings within
one emulated millisecond. The runner then exits 0 rather than dying of the signal. The signal
changes only where the run ends, not what happened before it, so determinism is unaffected.
`SIGKILL` leaves no `stats`.

`--peer-timeout SECONDS` bounds, in wall time, how long the runner waits for the peer's turn to
end (default 60; `0` waits for ever). A hung peer, say a model in another process, then fails the
run with code 4 instead of hanging a CI job. It acts only on that error path, so it cannot change a
run that completes.

`Stop::SwReset` is not an end: the runner sends `reset` and continues (5.5). `stats` is the last
message, after which the peer reads end of file; a peer that sees end of file without `stats`
knows the runner died.

## 10. Relation to mikroverk/esp32sim#325

The core messages are those #325 proposes: the handshake with version and chip, `uart_tx` with
its route, `gpio_out`, `reset`, `sync`, `uart_rx`, `gpio_in`, `release`, and `quiet_until` and
`latency` in the peer's `wait`; times in nanoseconds; receive pacing in-process; transmit times at
the FIFO write. If #325 lands with a different wire format, only the runner side and `BoardLink`'s
codec change (#37). What #325 does not have:

| Here | Why |
|---|---|
| `uart_cfg`, `warn`, `stats`, `adc` (*ext*) | route configuration, drops and analog input are outside the `BoardModel` hooks #325 forwards; proposed upstream in #38 |
| `watch` in the harness's `hello` | without it every console byte and every LED toggle would be an event the peer must acknowledge |
| `seq`, `ack`, turns, `sync` at the block point only | #325 leaves the exchange open; these make a run independent of the peer's wall-time speed |
| pacing at the `uart_rx` message's baud | #325 paces "at the route's baud"; here a baud mismatch reaches the UART as the frame error it is (`frm_err`), and bytes keep the peer's line rate (11.6) |
| `console` | the console as a tap, opt-in |
| `reset` comes from the runner's run loop | #325 proposes a `guest_reset` trait method; the peer sees the same message either way (plan, finding 4) |

## 11. Changes from the plan

Section 4.1 of the plan is superseded by this document. What changed, and why:

1. **Turns.** The plan let the peer acknowledge an event with `wait` or "with data at or after
   it", and did not say when the runner reads the socket. Here the runner reads only in the peer's
   turn and every turn ends with exactly one `wait`. If the runner applied whatever had arrived by
   wall time, a `wait` read early would move `B`, and with it where a slice ends, where the routes
   are compared and what `t` a `uart_cfg` gets: a fast and a slow peer would see different runs.
2. **`sync` at the block point only.** Plan rule 3 sent `sync` at every `advance_to`, at most once
   per emulated millisecond. A `sync` that does not hand over the turn gives the peer nothing to
   act on, since it may not send before its turn. A peer that wants a regular heartbeat asks for it
   with `quiet_until`. #11's checklist item for the cadence follows this document.
3. **The `sync` `t` is the block point, and rule 2 is measured against it**, not against "the
   runner's current emulated time". The runner can get control after `B` by less than one CPU
   cycle, at every clock below 240 MHz and from rounding nanoseconds to cycles (6.1); measured
   against that time, an answer stamped exactly `t_event + latency` would be late there.
4. **Events after the block point wait for the next turn**, so a peer's view of the run does not
   depend on the emulator's quantum.
5. **`uart_tx` also ends at a gap** of more than one character time, so separate requests keep
   their own times.
6. **Pacing at the message's baud**, the peer's line rate, rather than the route's. The route's
   baud may be unset, and a mismatch is the frame error `warn` reports, not something pacing
   should hide.
7. **`watch`**, new in the harness's `hello` (section 10). The harness's `hello` carries `chip`
   but not `clock`: the clock is the runner's to declare.
8. **The end of a run** sends `stats` without a final `sync`: a `sync` hands over a turn, and
   there is none at the end. The `Stop` variant that ended a run is in `stats.end`, not in a
   `warn`.
9. **Exit codes 3 and 4** are new: a protocol error and a vanished peer, apart from the
   emulator's own stops (1) and usage errors (2).
10. **Slices instead of `next_deadline`** (6.1). Plan 4.2 makes `B` a term of
    `SocketBoard::next_deadline` so that an idle guest does not "jump to the next timer" past it.
    At the pin that cannot happen: the idle skip is capped at `IDLE_CHUNK`, 512 CPU cycles
    (`esp32/src/soc.rs:24`, `esp-soc/src/machine.rs:600`), and the classic bus never defers device
    ticks (`can_defer` keeps its default `false`, `esp-soc/src/soc.rs:148`). What does stand in the
    way is that board callbacks cannot reach the bus, which `uart_cfg`, the drop warnings and `adc`
    need; the run loop's slices give it to them and stop for `B` as well. For #11 this means:
    `SocketBoard` implements `uses_uart_pins`, `uart_tx` and `gpio_output_at` (recording only),
    not `uart_rx`, `next_deadline` or `advance_to`; bytes reach the chip through
    `bus.periph.uart_pin_input` from the run loop; no `sync` between turns; `Unimplemented` and
    `Exceptions` go into `stats.end`, not `warn`. The WFI test case stays, as a test of the slices.
11. **`quiet_until` must be later than the turn's `sync`**: one at or before it would make the
    runner take a turn at every slice without the guest moving.
12. **`stats` counts per UART port**, not per route: a route is the port's configuration of the
    moment, and changes during a run.
13. **After `reset`** the runner repeats `uart_cfg` and `gpio_out` for the watched pins (5.5): the
    reset changes the chip's pins without calling the hooks that would report it.
14. **`SIGTERM` ends a run with `stats`**, and **`--peer-timeout`** bounds a turn (section 9). The
    plan ended a run early only by `stop`, which waits for a turn that may never come, and let a
    hung peer hang the runner.
