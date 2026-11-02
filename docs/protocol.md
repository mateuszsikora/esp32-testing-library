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

Who implements what: the runner side in #11 (M1: UART, `reset`, `uart_cfg`, `warn`, `stats`) and #23
(M3: GPIO and the future-input queue); the harness side in #12, and its GPIO lines in #24. `adc`
waits for an issue of its own, opened when an example needs an analog pin. Wall time under
`--realtime`, for a real device at the far end of a route (section 12), comes with #28.

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
A peer may take its time over any turn, turn 0 included, since the guest does not run while it
does: `BoardLink`, made with a `hold`, asks it at the start of every turn until the link fails,
before it polls the interfaces, and takes the turn only once it returns. The `esp32sim` backend's hold is the test's
clock, which returns once the test waits, with the latest time of the next turn, so emulated time
moves only while the test waits; `None` answers the turn with `stop`, as at the end of the test.
The wait is wall time, bounded by `--peer-timeout` (section 9), which the backend sets to 0 so
that a test body can take as long as it does.

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
| runner → harness | `stats` *ext* | `t`, `end`, `resets`, `turns`, `uart`, `unrouted_rx_bytes`, `inputs_on_output`, `inputs_not_applied`, `rx_bytes_not_paced`; under `--realtime` also `rt_resyncs` (M5) | M1 |
| harness → runner | `uart_rx` | `t`, `pin`, `baud`, `data` | M1 |
| harness → runner | `gpio_in` | `t`, `levels` | M3 |
| harness → runner | `release` | `t`, `pins` | M3 |
| harness → runner | `adc` *ext* | `t`, `pin`, `volts` | M3 |
| harness → runner | `wait` | `ack`, `quiet_until`, `latency` | M1 |
| harness → runner | `stop` | none | M1 |

The runner answers `adc` with a protocol error naming the message, `adc is not implemented yet`,
until it has an issue of its own (5.12).

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
  the UART FIFO, not when it would leave the wire (`esp32/src/bus.rs:308-311`). `wall`: the same
  emulated nanoseconds, paced to wall time under `--realtime` (section 12, M5). A `BoardLink`
  made with `clock` fails the link on a runner whose `clock` is another one: the `esp32sim`
  backend passes `emulated-fifo-write`, the clock its recordings are checked against, and, from
  #28, `wall` for a run with a `real` source (12.4).
- `esp32sim`: the full revision the runner was built against, for the recording header's `tools`.
- `board`: the harness's board profile name. The runner does not interpret it; its board is the
  socket.
- `watch`: the pins the peer is wired to. The runner forwards only what touches them: `uart_tx`
  for routes with a watched TX pin, `uart_cfg` for routes with a watched TX or RX pin (a
  receive-only peer learns its baud from it), `gpio_out` for watched pins. Inputs (`uart_rx`,
  `gpio_in`, `release`, `adc`) on a pin outside `watch` are a protocol error: the socket's board is
  wired to those pins only. The console's UART0 pins are normally not watched, so console output
  costs no turns; it has its own `console` message. An empty or absent `watch` makes the socket a
  board with nothing on it. Every pin in `watch` must be one the classic chip has, GPIO 0 to 39
  but 24 and 28 to 31, as the board profiles count them (`esp32tl/boards`). esp32sim's
  `gpio_state` also leaves out GPIO 20, which only some packages bond (`esp32/src/soc.rs:191`),
  but takes a level on it, so it counts. Any other pin is a protocol error at the handshake: on
  40 and above esp32sim would drop a level without a word (`esp32/src/periph.rs:373-376`).

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
  start the next message;
- the message holds 48 KiB, which keeps its line well under the 1 MiB limit.

When the route's baud is known, a message whose next byte has not come one character time after
its last goes out at the next slice boundary, at most a millisecond later: the next byte would
start a new message anyway, and the peer does not wait for it. Without a baud the message stays
open until another message is due. Messages outside the peer's turn reach the socket within about
20 wall milliseconds, so a peer that reads between its turns (the console, a tap) sees them as
they come.

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
`changes`. A change of an unwatched pin alone sends nothing.

`enabled` and `output` are the chip's `GPIO_ENABLE` and `GPIO_OUT` words, which is what the hook
passes (`esp32/src/bus.rs:313,344`). `changes` are pad levels, as esp32sim resolves them
(`ClassicGpio::driven` and `note_driven`, `esp32/src/periph.rs:284,322`): a pad driven as a plain
GPIO, by a peripheral signal through the GPIO matrix or by an IO_MUX function, at the level it is
driven to, and a pad that stops being driven at `0`. The two can disagree: a pad UART TX drives
through the matrix comes in `changes` at `1` with its bit in `output` at 0, and a pad an IO_MUX
function drives has no bit in either word. The *level* of a pin, here and in 5.5, is the one its
last `changes` entry reported, `0` before any. `gpio_out` is an event: it has a `seq` and sets the
block point, so a peer that answers a pin, a modem's power key for one, sees it within its
`latency`.

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
`reboot()`, since the reset itself calls no `gpio_output_at`. `enabled` and `output` are
`GPIO_ENABLE` and `GPIO_OUT` as `reboot()` left them, and `changes` holds every watched pin whose
level (5.4) is not the one the peer last saw, at its new level: a pin a peripheral drove high
before the reset comes back at `0`. It goes out after every reset while `watch` is not empty, with
empty `changes` when nothing changed, after the reset's `uart_cfg`s and at the reset's `t`; the
`reset` at the same `t` has set the block point already, so it costs no turn of its own.

The runner cannot read a pad's level: esp32sim keeps `ClassicGpio::driven` private. It takes a
pad as driven where `GPIO_ENABLE` and `GPIO_OUT` both have its bit, which right after `reboot()`
is the pad's state in esp32sim at the pin: `reboot()` leaves both words 0 and IO_MUX at function
0, through which esp32sim drives no pad. On the chip, IO_MUX function 0 of GPIO 1 is U0TXD, which
drives the pin high from the reset on without its bit in `GPIO_ENABLE`; esp32sim does not model
that, and the runner reports such a pad as undriven, as the emulator has it, until the firmware
drives it again (section 11, item 17).

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
point. With UART0's TX pin in `watch` the same bytes go out as `uart_tx` too, and the two
messages end together. The `esp32sim` backend runs the runner with `--console both`: `BoardLink`
hands each message to its `console` callback until the link fails, and the test's console
expects match on those bytes, which reach the harness before the `sync` that follows them;
`dut.log` still gets stdout.

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
| `input_on_output` | `gpio_in` on a pin whose bit is set in `GPIO_ENABLE` when the level comes due; the level is still applied. Not a drop: one `warn` per pin and message, never merged, with no `bytes`. A pad a peripheral drives with its output enable, or an IO_MUX function, gives none: the runner sees `GPIO_ENABLE` only | `pin` |

The runner detects the drops itself, in its run loop where it has the bus: the route and
`matches_baud` before it delivers a byte, the FIFO's `rx_pending()` against its 128 bytes. A byte
is delivered at a slice boundary `T` at or after its paced time `dₖ` (6.1), and the drop's `t` is
`T`, where the runner saw it, as for `uart_cfg`. `uart_pin_input` hands a byte to every UART whose
RX pin matches (`esp-soc/src/uart.rs:73-79`), so with two ports on one pin there is one `warn` per
port.

Consecutive drops of one kind on one pin and port are merged into one `warn` with their count in
`bytes` and the first drop's `t`; `input_on_output` is not merged. The merged `warn` goes out when any other message is due (an
event, `sync`, `console`, a `warn` of another kind, pin or port, `stats`), so the stream stays in
`t` order, and at no other time. A time limit would defeat the merging: at 9600 baud a character
takes 1.04 ms, so even a millisecond window would give each dropped byte of a mismatched NMEA stream
its own `warn`. The last warnings of a run are not held back either, because every end of a run
that sends `stats` sends them before it. A peer accepts kinds it does not know. `BoardLink` puts
warnings in the test report, and takes a `frm_err` as an error of the run: the bytes went in at
another baud than the guest's UART receives at, which would otherwise fail a test only later, by
a timeout that does not say why. The `esp32sim` backend then ends the run at once (§9), and the
error reads `uart.gnss sends at 115200 baud on GPIO 5, and the firmware's UART1 receives there at
9600 baud: 72 bytes dropped as frame errors (frm_err)`, naming the interface whose RX pin it is,
or `bytes on GPIO {pin}` for a pin no interface owns, or `bytes on a pin the runner did not name`
for a `frm_err` without its `pin`; `receives there with no baud set` when the route's `baud` is
`null`.

### 5.9 `stats` *ext*

```json
{"type": "stats", "t": 12000000000, "end": "max_seconds", "resets": 0, "turns": 13,
 "uart": [{"port": 1, "tx_bytes": 0, "rx_bytes": 864, "rxfifo_ovf": 0, "frm_err": 0}],
 "unrouted_rx_bytes": 72, "inputs_on_output": 0, "inputs_not_applied": 0,
 "rx_bytes_not_paced": 0}
```

The last message of a run, sent just before the runner closes the socket (section 9). `t` is
where the run ended: the end of the last slice, or, after a `stop`, that turn's `sync` `t`.
`inputs_on_output` counts the `input_on_output` warnings. `end`
says why: `max_seconds`, `stop`, `signal`, `emulator` (then also `stop`, the `Stop` variant's name,
and `detail`, its `Debug` form), or `protocol_error` (then also `detail`). `inputs_not_applied`
counts the queued input messages not yet applied when the run ended, and `rx_bytes_not_paced` the
received bytes not yet paced into the chip, of `uart_rx` messages already applied (a message still
in the queue counts in `inputs_not_applied` only), so `BoardLink` can tell inputs that reached
past the end of the run. `uart` has one entry per
port that carried a watched route during the run, with the bytes each way and the drops by kind.
`rt_resyncs`, only under `--realtime`, counts the times the run skipped a lag behind wall time
(12.2). `BoardLink` puts the counts in the test report next to the matching engine's unseen requests.

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
pins, which go back to the chip's own pulls (`BoardModel::released_inputs`), not to a resistor on
the board (below).

`levels` is a list of `[pin, level]` pairs, `level` `0` or `1`, applied in order, so of two pairs
for one pin the second wins; it is required, and may be empty. `pins` is a pin set; absent or
`null` is the empty set. Every pin must be in `watch` (5.1), which holds only pins the chip has, or
the message is a protocol error. Both go into the input queue (section 8) and reach the chip through
the board: at the slice boundary where one comes due, the runner hands its levels to
`SocketBoard`, and has the chip take them at once through `BoardModel::take_edges` and
`released_inputs`, as the bus does at its tick (`esp32/src/bus.rs:348-358`). Inputs due at the
same boundary apply in the queue's order, so a `release` and a `gpio_in` of one pin at one `t`
leave the pin as the later of the two says. An undriven pad without a pull reads `1` in
esp32sim: a peer that wants a line low from power-on sends the level stamped `t = 0` (rule 4).

The pulls `release` goes back to are the chip's own, which the firmware sets in IO_MUX (`sync_pull`,
`esp32/src/periph.rs:394`); esp32sim knows nothing of a resistor on the board. A peer that models
one, as the harness's `Lines` interface (`esp32tl/interfaces/lines.py`) does for a line its board
profile pulls, drives the line to the pull's level instead: in the first turn at `t = 0`, and in
place of each `release`. A level the peer drives also takes the pad from the chip's pulls, which
apply only to a pad nothing external drives, so the board's resistor wins over them; the guest's own
output still wins over it, as the pad's level is the guest's wherever it drives the pad
(`pad_level`, `:315`).

What the peer drives survives a guest reset (5.5): `reboot()` keeps the chip's external levels
(`esp32/src/soc.rs:141`), and the board reports them again through `BoardModel::input_levels`
when it is attached anew.

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
   an interface that holds inputs back instead of sending them early (section 8): its
   `quiet_until` is the next one's `t`, and `BoardLink` folds it into the turn's.

2. **A `t` earlier than the turn's `sync` `t` is a protocol error** that ends the run naming the
   peer: the peer broke its `latency` or `quiet_until` promise. The runner never clamps a late
   time. This is what makes two runs of one recording identical (plan, section 6), as long as
   every `t` a peer sends comes from its recording or its model, never from the wall clock.

   **"At once" means `t_event + latency`.** The peer sees an event in the turn at some
   `B ≤ t_event + latency`, so an answer stamped `t_event + latency` is always on time. One
   stamped `t_event` is late unless the turn happens to fall exactly at `t_event`.

3. **`sync` is the turn**, one per block point and nothing in between. A peer that wants to hear
   from the runner at least every millisecond of emulated time sets `quiet_until` no later than one
   millisecond ahead, as the `real` source on the emulator does (12.3).

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
cycles (section 3). While no event is unacknowledged, so that `B` has no `latency` term yet, a
slice is also at most `latency` long: an event in it sets `B = t + latency`, and the callback
that records it cannot stop `run`, so a longer slice would carry the guest past that `B`. With a
`latency` of `0` a slice is one bus cycle until the next event, which is slow: `hello_world`
runs at about 30 wall seconds per emulated second on an M2 Pro, against 0.3 with a `latency` of
1 µs and 0.1 with none. Between slices, where it has the bus, the runner:

- compares the UART routes (`uart_cfg`);
- delivers the bytes now due with `bus.periph.uart_pin_input`, after checking the route, the
  baud and the FIFO's `rx_pending()`, which is how it sees the drops `warn` reports;
- applies the queued inputs now due: received bytes onto their lines, GPIO levels and releases
  through the board (5.11);
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

- the `sync` still says `t = B`; events in `(B, T]` go out after the peer's turn (section 4). So
  does an event in the cycle `run` stops at: it happens once the guest runs again, at `t ≥ B`,
  `t = B` when `B` falls on a cycle;
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
| `replay` | the smallest request-to-answer delay in its recording, computed when the recording is loaded (plan 4.4); a replay never answers sooner. A passive stream with no requests and no explicit protocol declares `null`. An active protocol with no recorded answer delays declares `0`, including a recording of unanswered commands or a stream with an explicit protocol; unknown requests can still receive fallback answers | the minimum of the next unprompted event and every answer waiting to be sent |
| `model` | the shortest answer delay the model can produce, declared by the model | the model's next unprompted output |
| `real` | `null`: a real device promises no answer time, and its bytes are stamped at the turn they are handed over in (12.3) | the next whole emulated millisecond after the turn, renewed at every turn (rule 3) |

A replay that declared a `latency` larger than an answer in its recording would fail at that
request. A large `latency` costs nothing in correctness and saves turns: the runner coalesces
everything the guest sends within it into one turn. A `latency` of `0` is valid and costs a turn
per event.

When several interfaces share the socket, `BoardLink` sends the minimum `latency` over them, and the
minimum `quiet_until` over them and the test's clock (section 4), a `null` counting as no bound.
The `lines` interface, which drives GPIO lines at the instant the test does, declares neither.

## 8. Future-dated inputs

Any input (`uart_rx`, `gpio_in`, `release`, `adc`) may carry a `t` later than the turn's `sync`:
the runner keeps it in a queue of its own, ordered by `t` and, at equal `t`, by arrival, and
applies it at its time (section 6.1). This is how an interface's output that lies in the future, a
replay's next sentence for one, reaches the guest.
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
| `SIGTERM` or `SIGINT` | ends the slice in progress (at most one emulated millisecond), or at once if it is waiting for the peer's turn or, under `--realtime`, for wall time (12.2); sends the events and `warn`s not yet sent, `stats` with `end: signal`, closes | 0 |
| any other `Stop` (`esp-soc/src/soc.rs:9`: `MaxInsns`, `Breakpoint`, `Unimplemented`, `Simcall`, `Ebreak`, `Watch`, `Exceptions`, `CostModel`, `CostModelLifecycle`) | events not yet sent, `stats` with `end: emulator` and the variant, closes | 1 |
| usage error, or an image or ROM that cannot be loaded | nothing on the socket (it may not be connected yet) | 2 |
| protocol error by the peer: malformed or oversized line, unknown `type`, version or chip mismatch, late `t`, wrong `ack`, `quiet_until` not after the `sync`, a `baud` of `0`, a `watch` with a pin the chip does not have, a message the runner does not implement yet | `stats` with `end: protocol_error`, closes | 3 |
| the peer is gone: the connection failed, the socket closed, a read or write failed, the peer's turn took longer than `--peer-timeout`, or a write made no progress for that long (in or outside a turn) | nothing more | 4 |
| a second `SIGTERM` or `SIGINT` | ends the process at once | 128 + the signal's number |

Every run ends with one line on stderr naming the cause; a protocol error names the peer's
address and the offending message. The first cause decides the exit code: a final `stats` that
cannot be written, because the peer has already closed the socket, does not turn a 1 or a 3 into a
4.

`stop` reaches the runner only in a turn, and with `quiet_until` and `latency` both `null` the next
turn may be at `--max-seconds`. On the test's clock the runner is in a turn whenever the test is
not waiting, so the usual end of a test is `stop` in the turn the test left the run at, from the
backend's teardown. That end belongs to the `esp32sim` backend, not to pytest-embedded:
the runner is the backend's process, pytest-embedded's base `Dut` does not know it, and
pytest-embedded's own teardown calls `terminate()` and `kill()` back to back
(`pytest_embedded/plugin.py:505-506`), which would leave no time for `stats`. The backend's
teardown, in the `board` fixture, runs before the `dut` it depends on is torn down. It
(`Esp32Sim.close` in `esp32tl/backends/esp32sim.py`) answers the held turn with `stop`; for a
runner that is in no held turn, or has not exited five wall seconds after the `stop`, it sends
`SIGTERM`, reads the socket to end of file, waits for the process, and sends `SIGKILL` only when
the runner has not exited within five wall seconds of the signal. It gets `stats` and the last warnings within
one emulated millisecond. The runner then exits 0 rather than dying of the signal. The signal
changes only where the run ends, not what happened before it, so determinism is unaffected.
`SIGKILL` leaves no `stats`. Once the run ends, a signal no longer interrupts the last writes;
they get two wall seconds, then the runner gives up on a peer that does not read them. A second
`SIGTERM` or `SIGINT` ends the process at once, with exit code 128 plus the signal's number, as a
shell would report it. Before the connection is made a signal ends the process as it would any
other. The backend also sends `SIGTERM` as soon as `BoardLink` finds the run
wrong (a message out of protocol, an interface that broke its promises, a `frm_err`), rather than wait for the
runner's next turn to send `stop`, which may come only at the end of the test's wait, or at
`--max-seconds`. A handshake mismatch is
the exception: the runner finds it in the harness's `hello` and ends the run itself, with code 3.

`--peer-timeout SECONDS` bounds, in wall time, how long the runner waits for the peer's turn to
end, and how long a write may make no progress because the peer does not read (default 60; `0`
waits for ever). For `tcp:` it also bounds each connection attempt, though not the name lookup
before it. A hung peer, say a model in another process, then fails the
run with code 4 instead of hanging a CI job. It acts only on that error path, so it cannot change a
run that completes. The `esp32sim` backend passes `0`: its turns last as long as the test body
runs, and the test's own wall-time timeout ends a run that hangs
([pytest plugin](features/pytest-plugin-user.md#the-tests-timeout)).

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
| `console` | the console for the peer, opt-in: the `esp32sim` backend's console expects read it, in emulated-time order with the turns |
| `reset` comes from the runner's run loop | #325 proposes a `guest_reset` trait method; the peer sees the same message either way (plan, finding 4) |
| The SD card of `--sd-rw` answers SPI on `SocketBoard`, in the runner | a card answers every byte of a transaction in the same transaction (`spi_transfer_pins`), which a peer a turn away could not; nothing of it goes on the socket, and the protocol does not change ([runner-technical.md](features/runner-technical.md#the-sd-card)) |

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
    #23 adds `input_levels`, `take_edges` and `released_inputs` for GPIO inputs (item 16).
11. **`quiet_until` must be later than the turn's `sync`**: one at or before it would make the
    runner take a turn at every slice without the guest moving.
12. **`stats` counts per UART port**, not per route: a route is the port's configuration of the
    moment, and changes during a run.
13. **After `reset`** the runner repeats `uart_cfg` and `gpio_out` for the watched pins (5.5): the
    reset changes the chip's pins without calling the hooks that would report it.
14. **`SIGTERM` ends a run with `stats`**, and **`--peer-timeout`** bounds a turn (section 9). The
    plan ended a run early only by `stop`, which waits for a turn that may never come, and let a
    hung peer hang the runner.
15. **A slice is at most `latency` long while no event is unacknowledged** (6.1), found while
    implementing #11. The slice ends listed before it are all known when a slice starts; the
    `latency` term of `B` is not, since it comes from an event the guest has yet to produce.
    Without this bound a request sent mid-slice, with the guest asleep in WFI waiting for the
    answer, would be seen only at the next millisecond, and an answer stamped `t + latency`
    would reach the chip up to that late.
16. **GPIO inputs reach the chip at the slice boundary**, found while implementing #23. The plan
    has the bus take them through `advance_to` and `take_edges` at its own tick. That tick comes
    after the next round of instructions, so a level would raise its interrupt up to a quantum
    after `T`, or one idle chunk with the guest asleep. The run loop instead hands a level to the
    board when it comes due and has the chip take it at once, as it delivers received bytes.
17. **The pins after a reset are what esp32sim leaves**, found while implementing #23. This
    document asked for the state read after `reboot()`, not assumed, because IO_MUX can drive a
    pin, U0TXD on GPIO 1 for one, without its bit in `GPIO_ENABLE`. At the pin esp32sim drives no
    pad after `reboot()`, U0TXD on GPIO 1 included, and keeps the pad state private, so the runner
    reads `GPIO_ENABLE` and `GPIO_OUT`, which right after the reset agree with it (5.5). Should a
    later esp32sim model the IO_MUX functions a reset leaves, the runner needs the pad state from
    it.
18. **A run with a `real` source is on the wall clock from power-on**, designed in #27. Plan 4.3
    and its section 11 kept the recording format's clock switch for M5, for a session that crosses
    from emulated to wall time. Under `--realtime` the run is paced from its first instruction, so
    every time in it is a wall time already, and every route's timing depends on when the device's
    bytes came, so no switch has a place to go (12.4).
19. **The `real` source renews its turn at the next whole emulated millisecond**, not one
    millisecond after each turn as plan M5 has it: the runner ends a slice there anyway (6.1), so
    the source's turns cost no slices of their own (12.3).


### Harness policy for simulated network receive loss

On an interface whose `lossless` property is true (currently a modem with the simulated network),
`BoardLink` treats `warn.kind == "rxfifo_ovf"` on that interface's RX pin as fatal. For LilyGO:
`uart.modem: UART receive overflow (rxfifo_ovf) on GPIO 27: the simulated network requires a lossless route`.
The warning is retained in the report and the run stops as for `frm_err`. Ordinary UART routes
keep overflow as a warning. This changes harness policy only, not message fields or version 1.

## 12. Wall time: `--realtime` and the `real` source (M5)

Designed in #27, before code; the runner's half and the harness's `real` source come with #28.
Until then the runner has no `--realtime` and its `hello` always says `emulated-fifo-write`.
Nothing here changes the wire: `wall` is a value `clock` already reserved, and `rt_resyncs` a
field `stats` may add (section 2), so the protocol stays at version 1.

A real device on a host serial port, a GNSS receiver on a USB adapter for one, sends when it
sends, in wall time; it cannot be held in a turn or say when it will next send. Putting it at the
far end of an emulated route takes emulated time paced to wall time, a rule for the emulated time
each of its bytes gets, and turns often enough that the rule stays close to the wire.
mikroverk/esp32sim#325 leaves exactly this open: "the mechanics of stamping wall time as emulated
time need their own design" (plan, finding 11).

### 12.1 The runner under `--realtime`

`--realtime` sets `machine.rt.enabled`, which is all esp32sim's flag of the same name does
(`cli/src/lib.rs:829`), though only once the guest has run 2¹⁶ bus cycles (12.2), and the
runner's `hello` says `clock: wall`. esp32sim paces `run` itself (`Machine::rt_pace`,
`esp-soc/src/machine.rs:1091-1115`): every 2¹⁶ bus cycles, 273 emulated µs, it compares emulated
time since power-on with the wall time since its first such check, which takes the wall instant
of that check as emulated time 0 (`wall_start`, set by `get_or_insert_with(Instant::now)`). It
sleeps while emulated time is more than 2 ms ahead, lets the guest run as fast as it can while it
is behind, and once wall time is more than 500 ms ahead it moves its anchor up to emulated time,
skipping the lag, and counts the skip in `rt.resyncs`. Real-time pacing also turns off round
batching and virtual quanta (`:573`, `:777`), which changes the emulator's speed and nothing the
guest sees.

Times on the socket are emulated nanoseconds as in section 3, and every rule of section 6 holds as
written. What changes is what the times mean: they follow wall time, less what a held turn or a
skipped lag took out of it (12.2), so a run under `--realtime` does not repeat, whatever its peer
does.

### 12.2 The runner waits for wall time at the block point

esp32sim's pacing alone lets the guest reach a block point up to 2 ms plus one pacing interval,
about 2.3 ms, before wall time does, and its anchor is private (`Realtime::wall_start`), so the
runner cannot ask it where wall time stands. The runner keeps a wall clock of its own, and starts
it with esp32sim's. esp32sim takes its anchor at its first check, the first round after the guest
has run 2¹⁶ bus cycles since pacing was turned on, and a turn held before that check, by the test
or the peer, would come before one anchor and after the other. So the runner sets
`machine.rt.enabled` only at the first slice boundary at or after 2¹⁶ bus cycles, after any turn
taken there and just before it calls `run` again, and anchors its clock at the same moment with
esp32sim's convention: emulated time 0 is now. esp32sim's first check comes in the first round of
that `run`, microseconds later, so the two anchors agree to within that, whatever turns were held
before. Until then, the first emulated millisecond at most, the run is not paced and the runner
does not wait. When `rt.resyncs` has grown by a slice boundary `T`, the runner moves its anchor so
that `T` is now, as esp32sim moved its own; the two then differ by less than the wall time of one
slice.

At each block point `B`, before it sends `sync`, the runner under `--realtime` waits until its
wall clock reaches `B`. It waits on the socket with the wall time left as the timeout, in the
transport's 20 ms steps, as it waits for a turn: a signal ends the wait at once (section 9), and so
does the peer closing the socket (exit code 4). The wait is never without a bound: it is at most
the emulated time from where the guest last resumed to `B`, and under about 2.3 ms while
esp32sim's pacing keeps the guest close to wall time. It is not part of the peer's turn, so
`--peer-timeout` does not count it. Without it, the harness would take the turn at `B` before the
device's bytes up to `B` had arrived and give them the next turn's time, up to about 3.3 ms late
instead of within a millisecond (12.3).

A turn held in the harness, by the test's clock (section 4) or a slow peer, stops emulated time
while wall time goes on, and leaves the guest behind. Under 500 ms esp32sim runs it flat out until
it has caught up, and the runner does not wait at the block points meanwhile; past 500 ms both skip
the lag. A guest that cannot keep up falls behind the same way: stage 0 measured CPU-bound code at
0.3 to 1.7 times real time ([speed](stage0-findings.md#speed)). `stats` under `--realtime` carries
`rt_resyncs`, `rt.resyncs` at the end of the run, which `BoardLink` puts in the test report: each
resync is a stretch of wall time the guest did not see. Catching up or skipping puts the clock
right, not the device's bytes from that stretch: those are the `real` source's to bound (12.3).

### 12.3 The `real` source

The harness's `real` source (`esp32tl/sources/real.py`, given with `gnss.real(port)` or
`board.uart(role).real(port)`) opens the host serial port `port` at the wire's baud, the line rate
of the board profile's route, at which a replay and a model send too. It reads the port in a thread
of its own, so the operating system's buffer does not fill while a turn is held. It declares:

- `latency`: `null`. A real device promises no answer time, and needs none: its bytes are stamped
  at the turn they are handed over in, never before it (rule 2).
- `quiet_until`: the next whole emulated millisecond after the turn's `sync` `t`. The device does
  not know when it will next send, so the source renews its turn at least every emulated
  millisecond (rule 3). A whole millisecond is where the runner ends a slice anyway (6.1), so these
  turns add no slices of their own.

**Received bytes.** At each turn the source sends what the port has given since its last turn,
up to the backlog's bound below, as one `uart_rx` stamped with the turn's `sync` `t`, at the
wire's baud. A byte thus gets the time of the first turn that looks at the port after it arrived:
the end of the emulated millisecond in which it arrived or, if it arrived while the run was held in
a turn, that turn's time. The runner paces the bytes from there at the line rate (rule 5), as the
wire did. While the guest keeps up with wall time (12.2), a byte is stamped within one emulated
millisecond after it reached the harness. The USB adapter's and the host driver's delays come
before that and are not removed: an FTDI adapter holds received bytes for up to its latency timer,
16 ms by default.

**Backlog.** A wire carries no more than its line rate, and emulated time does not move while a
turn is held, so the bytes a device sends during a held turn, or while the guest is behind, cannot
all reach the guest when they should. Kept, they would queue on the line (rule 5) and stay there:
a device sending at its full line rate through a 10 s hold leaves its bytes about 10 s behind for
the rest of the run, whatever the clock does, and an answer to a new command waits behind them. The
source therefore keeps its line at most 50 ms behind. It works out where its line will be free as
the runner paces it, `line_free(pin)` of rule 5, from its own messages, which are the only ones on
its RX pin. At each turn at `now` it sends no more of what the port gave than the line carries by
`now + 50 ms`, the newest bytes, and drops the older ones, counting them for the test report. The
guest then hears what the device says now, an answer included, at most 50 ms after its stamp.
50 ms is above an FTDI adapter's 16 ms burst, so while the guest keeps up with wall time no byte
is dropped.

**Transmitted bytes.** The source writes what the firmware transmits to the port as soon as the
link hands it over, in or between turns. A `uart_tx` reaches the harness at the latest in the turn
after its `t`, within an emulated millisecond, so under `--realtime` the bytes leave at their
`uart_tx` time to within about a millisecond, and the port's UART puts them on the wire at its
baud. The port's rate is fixed for the run. A `uart_tx` whose `baud` is `null`, or further from
the port's than the 3 % `matches_baud` allows, would give the device what a wire does not carry
intact, which the port cannot reproduce: the run fails on it, as it does on a `frm_err` (5.8).

The source's turns are the run's cost: a thousand per emulated second, each a round trip through
`BoardLink`. A harness that takes close to a millisecond of wall time per turn keeps the guest
behind wall time, and the lag grows until it is skipped every half second (12.2).

### 12.4 The backend and recordings

A test gives a route a `real` source before `board.power_on()`, as any other source. The
`esp32sim` backend then starts the runner with `--realtime` and makes its `BoardLink` with `clock`
`wall`, so the whole run is on the wall clock from power-on and nothing switches clocks
([recording format](recording-format.md#clock-switches)). Every recording of such a run says
`clock: wall`, the console's and a model's route's too, since what the firmware did depended on
when the device's bytes came. The real route's tap writes `source: real`, its `in` events at the
source's stamps, without the bytes it dropped (12.3), and its `out` events at their `uart_tx`
times. A replay in such a run takes only a
`clock: wall` recording, since it refuses a clock other than its backend's
([replay](features/replay-user.md#what-a-replay-refuses)). Recordings on the wall clock are never
compared with emulated ones, and tests with a `real` source stay out of the determinism job
([testing](guides/testing.md#determinism)).

The run stays on the test's clock: it is held while the test body runs, and `board.wait(d)` lets
it go for `d` of the board's time, which under `--realtime` is also about `d` of wall time. The
test's own timeout, 600 wall seconds by default
([pytest plugin](features/pytest-plugin-user.md#the-tests-timeout)), then bounds the board's time
too.

#28 tests this without hardware: a pseudo-terminal pair stands in for the USB adapter, with a
thread on one end writing the device's bytes in wall time. In CI that checks the stamping bound,
the wait at the block point, a turn held before the first 2¹⁶ bus cycles (after which the runner
still waits at the block points), and a turn held for more than 500 ms against a device sending at
its full line rate, with the bytes dropped counted and an answer sent after the hold reaching the
guest within the 50 ms bound. A GNSS receiver on a USB serial adapter validates the whole (plan M5); its
recording replaces none of the generated ones, which stay the deterministic reference.

TODO(owner): what `gnss.real(port)` does on the `serial` backend. Its capability declaration gives
`serial` a `real` source for the device wired to the board, which no host port reaches (finding
11): skip the test there, or run it against the board's own device and leave `port` closed.
