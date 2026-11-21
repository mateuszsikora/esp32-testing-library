# Link protocol: runner ⇄ harness

The contract between `esp32tl-runner` (the Rust crate in `runner/`, which runs the guest in
esp32sim) and the harness (`esp32tl/link/`, `BoardLink`), or any other peer that takes the
runner's board slot. Protocol version 2. It writes down section 4.1 of the
[implementation plan](implementation-plan.md) and the parts of 4.2 a peer needs, checked against
esp32sim at the project's pin, `357b0e5`. Where this document and the plan differ, this document
holds; section 11 lists the differences and why.

The protocol follows the `SocketBoard` proposed in mikroverk/esp32sim#325, so the runner can be
replaced by upstream's once that lands (section 10). Messages marked *ext* are runner extensions
that #325 does not cover; they stay in the runner whatever happens upstream (plan, finding 10).

Who implements what: the runner side in #11 (M1: UART, `reset`, `uart_cfg`, `warn`, `stats`) and #23
(M3: GPIO and the future-input queue); the harness side in #12, and its GPIO lines in #24. #31
(M7: Wi-Fi) adds `eth_tx`, `eth_rx`, `wifi_ap` and `stats.wifi` on both sides, and #106 `sd_card` and
`sd_card_ack`, the SD card coming and going while the firmware runs. `adc` waits for an
issue of its own, opened when an example needs an analog pin. Wall time under `--realtime`, for a
real device at the far end of a route (section 12), came with #28. #105 adds `power` and
`chip_reset`, the board's supply and the chip's EN pin, on both sides, #107 the SD card's fault
rules, `sd_fault`, `sd_clear` and `sd_op` (#260 the `observe` action), #131 `flash_dump` and
`flash_dump_ack`, a read of the flash in a turn, #34 `hci_tx` and `hci_rx`, the guest's Bluetooth
HCI with `--bt`, #245 `uptime`, where each boot's `esp_timer` counts zero on the socket's clock
(5.24), #261 the `hint` of `reset`, the register ESP-IDF's `esp_reset_reason()` reads at the next
boot (5.5), and #278 `flash_write` and `flash_write_ack`, a write of the flash in a turn with the
board's supply off (5.25). `console_rx` and `stats.console_rx_lost` are the console on the chip's
USB-Serial/JTAG controller, with `--console-port usb`, and since #233 on UART0 of the ESP32-P4 and
the ESP32-S31, whose UARTs have no pins in esp32sim (5.23).

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
does not restart at a guest reset, a wake from deep sleep, a pulse of EN (`chip_reset`) or the
board's supply coming back (`power`), and it runs on while the supply is off: the runner keeps the
same machine, and neither `reboot()` nor its power-on reset (5.17) touches the bus cycle counter.
What the firmware counts from its own boot, `esp_timer_get_time()` for one, starts again at every
reset; the times on the socket do not. `uptime` (5.24) says where on the socket's clock the counter
`esp_timer_get_time()` reads counted zero in each boot.

The runner counts time in bus cycles, `bus.cycles()`. They run at a fixed reference independent
of the momentary CPU clock (`tick` scales through `cpu_scale_half`, `esp32/src/bus.rs:642`), so
one conversion holds for the whole run, across the XTAL to PLL switch at boot:

```
ns     = floor(cycles × 10⁹ / CPU_HZ)      runner → harness
cycles = ceil(ns × CPU_HZ / 10⁹)           harness → runner
```

`CPU_HZ` is the chip's `Soc::CPU_HZ`, the same constant `Machine::seconds` divides by
(`esp-soc/src/machine.rs:169`): 240 000 000 on the classic chip (`esp32::periph::CPU_HZ`,
`esp32/src/periph.rs:18`) and on the ESP32-S3 (`esp32s3/src/periph.rs:50`), 160 000 000 on the
ESP32-C3 and the ESP32-C6 (`esp32c3/src/periph.rs:13`, `esp32c6/src/periph.rs:16`), 360 000 000 on
the ESP32-P4, both revisions (`esp32p4/src/periph.rs:18`), and 320 000 000 on the ESP32-S31
(`esp32s31/src/periph.rs:26`). At 240 MHz the fraction reduces to `ns = floor(cycles × 25 / 6)`
and `cycles = ceil(ns × 6 / 25)`; at 160 MHz to `ns = floor(cycles × 25 / 4)` and
`cycles = ceil(ns × 4 / 25)`; at 360 MHz to `ns = floor(cycles × 25 / 9)`; at 320 MHz to
`ns = floor(cycles × 25 / 8)`.
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
of input messages, then exactly one `wait` (or `stop`, section 9), which ends it. A `flash_dump`
(5.21) or a `flash_write` (5.25) may come among them, and the runner answers it within the turn. The
runner reads the socket only between its `sync` and the peer's `wait`, never while the guest runs,
so how fast a peer answers in wall time cannot change what the guest sees (section 6 of the plan:
two runs of one recording give identical output).

Outside its turn a peer reads and does not write. The runner cannot tell an early message from
the next turn's, so it would be checked against that turn's `sync`; and a peer blocked on writing
while the runner is blocked on writing `console` or `uart_tx` to it is a deadlock.

The first `sync` has `t = 0` and comes before the guest executes its first instruction (rule 4).
A peer may take its time over any turn, turn 0 included, since the guest does not run while it
does: `BoardLink`, made with a `hold`, asks it at the start of every turn until the link fails,
after `Interface.arrive(now)` has let each interface take in what came due by that turn, and before
it polls the interfaces, and takes the turn only once it returns. The `esp32sim` backend's hold is the test's
clock, which returns once the test waits, with the latest time of the next turn, so emulated time
moves only while the test waits; `None` answers the turn with `stop`, as at the end of the test.
A hold may also return a `RunTo`, the latest time of the next turn and a `latency`, which the link
folds into the `wait`'s `latency` with the interfaces' (5.13): the test's clock does so while the
test waits for a wake from deep sleep, with 1 ms, so that the turn comes right after the `reset`
(5.5) without a `quiet_until` step in the sleep. The hold answers no event, so its `latency` is a
promise nothing can break; the messages on the wire are those of version 2.
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
| runner → harness | `reset` | `seq`, `t`, `cause`, `hint` | M1, #261 |
| runner → harness | `sync` | `t` | M1 |
| runner → harness | `console` | `t`, `data` | M1 |
| runner → harness | `warn` *ext* | `t`, `kind`, `detail`, and per kind `port`, `pin`, `baud`, `bytes` | M1 |
| runner → harness | `eth_tx` *ext* | `seq`, `t`, `data` | M7 |
| runner → harness | `hci_tx` *ext* | `seq`, `t`, `data` | #34 |
| runner → harness | `sd_card_ack` *ext* | `t`, `image`, `error` | #106 |
| runner → harness | `sd_op` *ext* | `t`, `fault`, `op`, `blocks`, `action`, `delay`, `written` | #107 |
| runner → harness | `flash_dump_ack` *ext* | `t`, `error` | #131 |
| runner → harness | `flash_write_ack` *ext* | `t`, `error` | #278 |
| runner → harness | `uptime` *ext* | `t`, `earliest`, `latest` | #245 |
| runner → harness | `stats` *ext* | `t`, `end`, `resets`, `turns`, `uart`, `unrouted_rx_bytes`, `inputs_on_output`, `inputs_not_applied`, `rx_bytes_not_paced`; `wifi` with `--wifi` or `--wifi-sta`; under `--realtime` also `rt_resyncs` (M5); with `--console-port usb`, or on the ESP32-P4 and the ESP32-S31, also `console_rx_lost` | M1 |
| harness → runner | `uart_rx` | `t`, `pin`, `baud`, `data` | M1 |
| harness → runner | `gpio_in` | `t`, `levels` | M3 |
| harness → runner | `release` | `t`, `pins` | M3 |
| harness → runner | `adc` *ext* | `t`, `pin`, `volts` | M3 |
| harness → runner | `eth_rx` *ext* | `t`, `data` | M7 |
| harness → runner | `hci_rx` *ext* | `t`, `data` | #34 |
| harness → runner | `console_rx` *ext* | `t`, `data` | USB console; UART0 of the P4 and the S31 (#233) |
| runner → harness | `wifi_link` | `seq`, `t`, `ready` | v2 |
| harness → runner | `wifi_ap` *ext* | `t`, `on` | M7 |
| harness → runner | `power` *ext* | `t`, `on` | #105 |
| harness → runner | `chip_reset` *ext* | `t` | #105 |
| harness → runner | `sd_card` *ext* | `t`, `image` | #106 |
| harness → runner | `sd_fault` *ext* | `t`, `id`, `op`, `action`, `delay`, `count`, `blocks` | #107 |
| harness → runner | `sd_clear` *ext* | `t`, `ids` | #107 |
| harness → runner | `flash_dump` *ext* | none | #131 |
| harness → runner | `flash_write` *ext* | `offset`, `path` | #278 |
| harness → runner | `wait` | `ack`, `quiet_until`, `latency` | M1 |
| harness → runner | `stop` | none | M1 |

The runner answers `adc` with a protocol error naming the message, `adc is not implemented yet`,
until it has an issue of its own (5.12).

The Wi-Fi Ethernet messages (`eth_tx`, `eth_rx`) and `stats.wifi` pass on runs started with
`--wifi` or `--wifi-sta`. `wifi_ap` passes only with `--wifi`: a virtual station has no access
point to switch. Without either radio option, `eth_rx` is a protocol error; `wifi_ap` is a protocol
error without `--wifi`, including in station mode (section 9). These messages originally kept protocol version 1.

`power` and `chip_reset` (5.17, 5.18) originally left the version at 1. A peer that never sends them
sees no difference: the runner sends no new message, and `reset` (5.5) keeps its fields. A runner
built before them answers either with a protocol error, `unknown type "power"`, so a harness that
needs them finds out at the first one; the harness and the runner come from one checkout.

The SD card messages (`sd_card`, `sd_card_ack`, 5.19) pass only on a run started with `--sd-spi`, and
the version originally stayed 1 for the same reason: the runner sends `sd_card_ack` only in answer to the
peer's `sd_card`, so a peer that never sends it never sees one; without `--sd-spi`, `sd_card` is a
protocol error.

The SD card's messages (`sd_fault`, `sd_clear`, `sd_op`) pass only on a run started with `--sd-spi`;
without it `sd_fault` and `sd_clear` are protocol errors. The runner sends `sd_op` only for a rule
the peer gave. The version originally stayed 1
([decision](decisions/runner.md#the-sd-cards-fault-messages-keep-protocol-version-1)).

`flash_dump` and `flash_dump_ack` (5.21) originally left the version at 1: the runner sends the answer only
to a peer that asked, and without `--flash-out` the message is a protocol error.

`flash_write` and `flash_write_ack` (5.25) leave the version at 2: the runner sends the answer only
to a peer that asked, and a runner built before them answers `flash_write` with a protocol error,
`unknown type "flash_write"`; the harness and the runner come from one checkout.

`uptime` (5.24) left the version at 2. The runner sends it on every run, and a harness built before
it refuses it as `no message type 'uptime' from the runner`; the harness and the runner come from
one checkout.

`reset`'s `hint` (5.5) left the version at 2 for the same reason. A peer that does not know it
ignores it (section 2); the harness refuses a `reset` without it, `reset: no hint`.

The Bluetooth messages (`hci_tx`, `hci_rx`, 5.22) pass only on a run started with `--bt`, and the
version originally stayed 1, as with the Wi-Fi ones: the runner sends `hci_tx` only with `--bt`, which only a
harness that speaks both gives it, and without `--bt` `hci_rx` is a protocol error
([decision](decisions/runner.md#the-bluetooth-messages-keep-the-link-protocol-at-version-1)).

`console_rx` and `stats.console_rx_lost` (5.23) leave the version at 2 for the same reason: the
runner reads `console_rx` only on a run started with `--console-port usb`, which only a harness
that sends it gives it, or on a chip whose UART0 has no pins, the P4 and the S31, which only such a
harness runs; otherwise it is a protocol error, `console_rx without --console-port usb, on a UART0
with pins`; `console_rx_lost` is a field added to `stats`, which a peer that does not know
it ignores (section 2). The console's output keeps its message, `console` (5.7)
([decision](decisions/runner.md#the-usb-serialjtag-console-adds-console_rx-at-protocol-version-2)).

**Events** are the runner messages that carry `seq`: `uart_tx`, `uart_cfg`, `gpio_out`, `reset`,
`eth_tx`, `hci_tx` and `wifi_link`. They are what a peer may answer, so they set the block point (rule 1). `seq`
is one counter over all seven, starting at 1 and growing by 1. `sync`, `console`, `warn`,
`sd_card_ack`, `sd_op`, `flash_dump_ack`, `flash_write_ack`, `uptime` and `stats` carry no `seq`
and ask for nothing.

### 5.1 `hello`

```json
{"type": "hello", "version": 2, "chip": "esp32", "clock": "emulated-fifo-write",
 "esp32sim": "357b0e542dfe97d4c62ec4a15c01c4969164448c"}
{"type": "hello", "version": 2, "chip": "esp32", "board": "esp32-devkitc", "watch": [4, 5]}
```

The first line is the runner's, the second the harness's.

- `version`: 2. Both sides require equality; there is no negotiation.
- `chip`: the chip the runner runs, its `--chip`: `esp32` (the classic chip), `esp32s3`,
  `esp32c3`, `esp32c6`, `esp32p4` (either revision) or `esp32s31`. The harness sends the chip of its board profile and the runner refuses
  another.
- `clock`: what the times of `uart_tx` mean, in the vocabulary of the recording header (plan 4.3).
  `emulated-fifo-write`: emulated time, and a transmitted byte is stamped when the guest writes
  the UART FIFO, not when it would leave the wire (`esp32/src/bus.rs:319-322`). `wall`: the same
  emulated nanoseconds, paced to wall time under `--realtime` (section 12, M5). A `BoardLink`
  made with `clock` fails the link on a runner whose `clock` is another one: the `esp32sim`
  backend passes `emulated-fifo-write`, the clock its recordings are checked against, and `wall`
  for a run with a `real` source (12.4).
- `esp32sim`: the full revision the runner was built against, for the recording header's `tools`.
- `board`: the harness's board profile name. The runner does not interpret it; its board is the
  socket.
- `watch`: the pins the peer is wired to. The runner forwards only what touches them: `uart_tx`
  for routes with a watched TX pin, `uart_cfg` for routes with a watched TX or RX pin (a
  receive-only peer learns its baud from it), `gpio_out` for watched pins. Inputs (`uart_rx`,
  `gpio_in`, `release`, `adc`) on a pin outside `watch` are a protocol error: the socket's board is
  wired to those pins only. The console watches UART0 RX (GPIO 3 on the classic chip, 44 on the
  S3, 20 on the C3, 17 on the C6), but not TX, so console output costs no turns; it has its own
  `console` message. A console on the USB-Serial/JTAG controller (5.23) has no pins and watches
  none. An empty or absent `watch` makes the socket a
  board with nothing on it. Every pin in `watch` must be one the chip has, as the board profiles
  count them (`esp32tl/boards`): on the S3 GPIO 0 to 21 and 26 to 48, on the C3 0 to 21, on the
  C6 0 to 30, which are esp32sim's own pin tables (`esp-soc/src/pins.rs`); on the classic chip
  GPIO 0 to 39 but 24 and 28 to 31. esp32sim's
  `gpio_state` also leaves out GPIO 20, which only some packages bond (`esp32/src/soc.rs:212`),
  but takes a level on it, so it counts. Any other pin is a protocol error at the handshake: on
  40 and above esp32sim would drop a level without a word (`esp32/src/periph.rs:386-389`).

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

A route's configuration, read with `bus.periph.uart_route(port)` (`esp32/src/periph.rs:939`). The
runner compares the routes at every slice boundary (section 6.1, at most one emulated millisecond
apart while the chip runs) and in every `uart_tx` callback, which carries the route. It sends
`uart_cfg` when a port's route differs from the last one it sent for that port and either of the
two touches a watched pin; before the first, every port counts as having no pins and `null` baud.
`t` is where the runner saw the change, at most one millisecond after the firmware made it. A
peer on a receive-only route (GNSS: the firmware never transmits) learns the baud from it instead
of trusting the board profile; a mismatch is otherwise a silent frame error.

`rx_pin` and `baud` are `null` until the firmware configures them; that is the valid initial
state, not an error. A guest reset sends the reset routes again.

On the ESP32-P4 and the ESP32-S31 esp32sim tells the runner no route (#229, G6 and G7): every port
has no pins and `null` baud, so `uart_cfg` never comes, and neither does `uart_tx`.

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
passes (`esp32/src/bus.rs:324,355`). `changes` are pad levels, as esp32sim resolves them
(`ClassicGpio::driven` and `note_driven`, `esp32/src/periph.rs:297,335`): a pad driven as a plain
GPIO, by a peripheral signal through the GPIO matrix or by an IO_MUX function, at the level it is
driven to, and a pad that stops being driven at `0`. The two can disagree: a pad UART TX drives
through the matrix comes in `changes` at `1` with its bit in `output` at 0, and a pad an IO_MUX
function drives has no bit in either word. The *level* of a pin, here and in 5.5, is the one its
last `changes` entry reported, `0` before any. `gpio_out` is an event: it has a `seq` and sets the
block point, so a peer that answers a pin, a modem's power key for one, sees it within its
`latency`.

### 5.5 `reset`

```json
{"type": "reset", "seq": 12, "t": 4100000000, "cause": 12, "hint": 2147745796}
```

The guest reset. From the run loop: `run` returned `Stop::SwReset`, and `cause` is what
`reboot()` returned (`esp-soc/src/machine.rs:309`), the ROM's reset reason (`RESET_REASON` in
ESP-IDF's `esp_rom/esp32/include/esp32/rom/rtc.h`). The classic chip in esp32sim raises
3 (`SW_RESET`), 5 (`DEEPSLEEP_RESET`, the wake from deep sleep), 9 (`RTCWDT_SYS_RESET`),
12 (`SW_CPU_RESET`, `esp_restart`), 13 (`RTCWDT_CPU_RESET`) and 16 (`RTCWDT_RTC_RESET`). From the
peer: a `chip_reset` (5.18) and a `power` that brings the supply back (5.17) reset the chip with
cause 1 (`POWERON_RESET`), at the input's `t`. The power-on at the start of the run sends no
`reset`, so the `n`-th `reset` of a run starts the chip's boot `n + 1`.

`hint` is the register ESP-IDF keeps its reset-reason hint in, `RTC_RESET_CAUSE_REG` of the chip's
`esp_rom/<chip>/include/<chip>/rom/rtc.h`, as the guest left it: the runner reads it through the
bus just before `reboot()`, as a 32-bit number. The panic handler, the task and interrupt
watchdogs and the brownout handler write `h | h << 16 | 0x80000000` there for an
`esp_reset_reason_t` `h` before they reset the chip, and the next boot's `esp_reset_reason()`
takes it for the reason of a software reset, causes 3 and 12, which the ROM reports alike for
`esp_restart()` and a panic. The example is `ESP_RST_PANIC`'s, 4. The register also holds a
deep-sleep wake stub's address, and whatever else the guest wrote there; the runner sends it as it
is. A reset from the peer has `hint` 0: a power-on clears the RTC registers.

| Chip | Register | Address |
|---|---|---|
| ESP32 | `RTC_CNTL_STORE6_REG` | `0x3ff480b8` |
| ESP32-S3 | `RTC_CNTL_STORE6_REG` | `0x600080c8` |
| ESP32-C3 | `RTC_CNTL_STORE6_REG` | `0x600080c0` |
| ESP32-C6 | `LP_AON_STORE6_REG` | `0x600b1018` |
| ESP32-P4 | `LP_SYSTEM_REG_LP_STORE6_REG` | `0x50110044` |
| ESP32-S31 | `LP_SYSTEM_REG_LP_STORE6_REG` | `0x20700044` |

The chip keeps the register through every reset but a power-on, and so does esp32sim on four of
the six chips. On the ESP32-C3 and the ESP32-S31 esp32sim's reset makes it anew, zero, and the
runner writes the value it read back after `reboot()`, so that the firmware's
`esp_reset_reason()` reads it as on the chip
([runner technical documentation](features/runner-technical.md#the-reset-reason-hint)).

The board persists across a reset, as on a real board where a firmware restart does not reset the
modem (the supply going off is another matter, 5.17): levels the peer drives stay driven, queued inputs stay queued, and paced bytes keep coming.
The chip does not: `reboot()` replaces the peripherals (`esp32/src/soc.rs:155`), so the RX FIFOs
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
0. On the 24 pads whose function 0 is GPIO (`GPIO_AT_FUNCTION_0`, `esp32/src/periph.rs:197-202`)
that is a GPIO with its output disabled, and through function 0 of the others esp32sim drives no
pad. On the chip, IO_MUX function 0 of GPIO 1 is U0TXD, which
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

Console input uses `uart_rx` on watched GPIO 3 (§5.10), not this output message; with
`--console-port usb`, and on the ESP32-P4 and the ESP32-S31, `console_rx` (5.23), and there the
console's bytes are stamped with the end of the slice they were written in.

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

With `--console-port usb` the bytes are those the guest wrote to its USB-Serial/JTAG controller
instead of UART0, and UART0's go out as `console` no more; its TX pin in `watch` still gets them as
`uart_tx`. They are not coalesced by time: esp32sim keeps no time of the controller's bytes and
hands them over inside `run` (`Machine::drain_console`, `esp-soc/src/machine.rs:339-371`), so the
runner takes what the guest flushed in a slice at the slice's end, and stamps it with that end, at
most 1 ms after the guest wrote it (6.1). A message ends after a `\n`, and at the end of what the
slice brought; the bytes of one slice that make several messages share their `t`.

### 5.8 `warn` *ext*

```json
{"type": "warn", "t": 1000000000, "kind": "unrouted_rx", "pin": 5, "bytes": 72,
 "detail": "uart_rx on GPIO 5, which no UART receives on: 72 bytes dropped"}
```

What the emulator dropped or refused and the peer could not see otherwise. `detail` is for
people; the fields are for code. Kinds in version 2:

| `kind` | When | Fields |
|---|---|---|
| `rxfifo_ovf` | a paced byte found the 128-byte RX FIFO full and was dropped, and the UART raised `UART_RXFIFO_OVF_INT` (`esp-periph/src/uart.rs:50-58`) | `port`, `pin`, `bytes` |
| `frm_err` | the route's baud differs from the `uart_rx` message's by more than 3 % of the message's, or is `null` (`UartRoute::matches_baud`, `esp-soc/src/uart.rs:21`); the UART raises `FRM_ERR` and drops the byte | `port`, `pin`, `baud` (the route's), `bytes` |
| `unrouted_rx` | a `uart_rx` byte on a pin no UART receives on ("Unrouted input is dropped", `esp-soc/src/board.rs:61`) | `pin`, `bytes` |
| `input_on_output` | `gpio_in` on a pin whose bit is set in `GPIO_ENABLE` when the level comes due; the level is still applied. Not a drop: one `warn` per pin and message, never merged, with no `bytes`. A pad a peripheral drives with its output enable, or an IO_MUX function, gives none: the runner sees `GPIO_ENABLE` only | `pin` |

The runner decides the drops itself, in its run loop where it has the bus: the route and
`matches_baud` before it delivers a byte, the FIFO's `rx_pending()` against its 128 bytes. A byte
is delivered at a slice boundary `T` at or after its paced time `dₖ` (6.1), and the drop's `t` is
`T`, where the runner saw it, as for `uart_cfg`. A byte goes to every UART whose RX pin it is, as
esp32sim's `uart_pin_input` hands it (`esp32/src/periph.rs:942-952`), each port on its own, so with
two ports on one pin there is one `warn` per port. A lost byte is lost for good, as on the chip:
the UART raises `UART_RXFIFO_OVF_INT` for a full FIFO and `UART_FRM_ERR_INT` for the baud, as the
chip does, and a firmware's driver sees them (ESP-IDF's resets the FIFO on the first and reports
`UART_FIFO_OVF`). The runner hands each byte to the shared `Uart` inside esp32sim's classic UART,
which drops what does not fit; it does not go through the classic UART itself, whose queue is for
a host that waits on the line, as a peer's line does not
([runner](features/runner-technical.md#received-bytes)).

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
{"type": "stats", "t": 15000000000, "end": "stop", "resets": 0, "turns": 1840, "uart": [],
 "unrouted_rx_bytes": 0, "inputs_on_output": 0, "inputs_not_applied": 0,
 "rx_bytes_not_paced": 0,
 "wifi": {"eth_tx": 38, "eth_tx_dropped": 0, "eth_rx": 41, "eth_rx_dropped": 0, "rx_dropped": 0}}
```

The last message of a run, sent just before the runner closes the socket (section 9). `t` is
where the run ended: the end of the last slice, or, after a `stop`, that turn's `sync` `t`.
`resets` counts the `reset` messages: the guest's own resets, and those of the peer's
`chip_reset` and of its `power` that brings the supply back (5.5). `inputs_on_output` counts the
`input_on_output` warnings. `end`
says why: `max_seconds`, `stop`, `signal`, `emulator` (then also `stop`, the `Stop` variant's name,
and `detail`, its `Debug` form), `protocol_error` (then also `detail`), or `bt_controller`: under
`--no-bt` the guest called `esp_bt_controller_init`, and `t` is the end of the slice it called it
in, at most one emulated millisecond later (§9). `inputs_not_applied`
counts the queued input messages not yet applied when the run ended, and `rx_bytes_not_paced` the
received bytes not yet paced into the chip, of `uart_rx` messages already applied (a message still
in the queue counts in `inputs_not_applied` only), so `BoardLink` can tell inputs that reached
past the end of the run. `uart` has one entry per
port that carried a watched route during the run, with the bytes each way and the drops by kind.
`rt_resyncs`, only under `--realtime`, counts the times the run skipped a lag behind wall time
(12.2). `console_rx_lost`, only with `--console-port usb` or on the P4 and the S31, counts the bytes of
`console_rx` that came due while the board's supply was off (5.23). `BoardLink` puts the counts in the test report next to the matching engine's unseen requests.

`wifi` is there with `--wifi` or `--wifi-sta`, as in the second example, and counts the station's link over
the whole run:

- `eth_tx`: the `eth_tx` messages sent, one per Ethernet frame the station sent (5.15);
- `eth_tx_dropped`: frames the station sent that esp32sim's relay dropped, longer than 1518 bytes or
  more than 64 between two slice boundaries (`StationLink::tx_dropped`,
  `esp-soc/src/wifi.rs:705-724`);
- `eth_rx`: `eth_rx` frames the station's link took;
- `eth_rx_dropped`: `eth_rx` frames that came due while the access point was off the air, or that
  the runner still held when it went off (5.16), and those that came due while the board's supply
  was off (5.17), plus queued and future inputs discarded at a virtual station transition.
  Frames already accepted by esp32sim and then flushed from its relay are not included;
- `rx_dropped`: frames from the air, beacons included, that found no free descriptor in the
  station's receive ring (`StationLink::rx_dropped`).

esp32sim starts its two counts again when the access point is attached anew and at a chip reset;
the runner adds them up before each, so `wifi` holds the run's totals. The harness reads `wifi` as
an object of these five integers, all required.

### 5.10 `uart_rx`

The harness console sends on GPIO 3 at 115200 baud, timestamped when the test writes.
UART0 receives through its configured native or GPIO matrix RX route. The ordinary framing,
overflow and route warnings apply. The runner restores GPIO 3's input-enabled reset state
missing from the pinned emulator (see [runner technical documentation](features/runner-technical.md#uart0-rx-reset-state)).
No new message or protocol version is needed.

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
`released_inputs`, as the bus does at its tick (`esp32/src/bus.rs:359-369`). Inputs due at the
same boundary apply in the queue's order, so a `release` and a `gpio_in` of one pin at one `t`
leave the pin as the later of the two says. An undriven pad without a pull reads `1` in
esp32sim: a peer that wants a line low from power-on sends the level stamped `t = 0` (rule 4).

The pulls `release` goes back to are the chip's own, which the firmware sets in IO_MUX (`sync_pull`,
`esp32/src/periph.rs:407`); esp32sim knows nothing of a resistor on the board. A peer that models
one, as the harness's `Lines` interface (`esp32tl/interfaces/lines.py`) does for a line its board
profile pulls, drives the line to the pull's level instead: in the first turn at `t = 0`, and in
place of each `release`. A level the peer drives also takes the pad from the chip's pulls, which
apply only to a pad nothing external drives, so the board's resistor wins over them; the guest's own
output still wins over it, as the pad's level is the guest's wherever it drives the pad
(`pad_level`, `:328`).

What the peer drives survives a guest reset (5.5): `reboot()` keeps the chip's external levels
(`esp32/src/soc.rs:158`), and the board reports them again through `BoardModel::input_levels`
when it is attached anew.

### 5.12 `adc` *ext* (M3)

```json
{"type": "adc", "t": 3000000000, "pin": 34, "volts": 1.25}
```

From `t` the pad sees `volts`, held until the next `adc` on that pin. Applied through the
machine's analog source (`SocBus::analog_set(pin, AnalogSource::Const(volts))`,
`esp32/src/soc.rs:238`), not through the board, because the trait has no analog method (plan,
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

### 5.15 `eth_tx` and `eth_rx` *ext* (M7)

```json
{"type": "eth_tx", "seq": 41, "t": 2804000000, "data": "////////JG8oABEiCAYAAQgABgQAASRvKAARIgpAAAIAAAAAAAAKQAAB"}
{"type": "eth_rx", "t": 2805000000, "data": "JG8oABEiAlNJTQACCAYAAQgABgQAAgJTSU0AAgpAAAEkbygAESIKQAAC"}
```

With `--wifi`, the chip's Wi-Fi station joins esp32sim's virtual access point, and the network
behind the access point is the peer's: the runner puts esp32sim's station link in Ethernet relay
mode (`SocBus::set_ethernet_relay`, `esp32/src/soc.rs:107-110`), so esp32sim's own network (DHCP,
DNS, NTP, NAT to the host) answers nothing. The example is an ARP request from the station,
`24:6f:28:00:11:22`, for `10.64.0.1`, and the harness's answer from the gateway's MAC,
`02:53:49:4d:00:02`, a millisecond later.

With `--wifi-sta`, the virtual station joins the guest's SoftAP. The harness sends and receives
at the virtual station's MAC and acquires its address from the guest's DHCP server. The same
`eth_tx` and `eth_rx` directions are relative to the guest: guest frames go out, harness frames
go in. Frames sent before association wait at the runner queue's head until esp32sim accepts
them. Identical pending DHCP client frames (IPv4 UDP 68 to 67) occupy one queue entry in this
mode; `eth_rx` counts accepted frames, not coalesced copies. The harness retries on board time.
`VirtualSta::on_ap_tx` already trims the guest's FCS slot in this mode; the harness still bounds
ARP and IPv4 by their payload lengths. `relay_lost`, frames accepted by esp32sim but lost on a
later disassociation, appears in `[emu] wifi-sta:` stderr, not in the five `stats.wifi` fields.

`data` is one Ethernet II frame without its FCS: destination, source, EtherType and payload, 14 to
1518 bytes. An `eth_rx` is that. An `eth_tx` from ESP-IDF's station is that and 4 bytes more,
`ef be ad de`, within the same limit: the frame of a 1500-byte datagram is an `eth_tx` of 1518
bytes. The 4 bytes are libpp's guard word, which is not part of the frame:

- libpp allocates the frames the station sends with `esf_buf_alloc_dynamic` (`esf_buf.o` in
  `libpp.a`), as the layout of every transmit buffer measured shows: the descriptor 48 bytes into
  the buffer's header, the data 92. It stores `0xDEADBEEF`, `ef be ad de` in memory, right after
  the buffer's data. A data frame fills its buffer to the end, so the guard word follows the
  frame's last byte.
- libpp counts more than the frame in the length of its transmit descriptor: 4 bytes more for
  every frame measured, data, null data and authentication, taken to be its FCS, and for a
  CCMP-protected frame the 8 of its MIC as well, which `ppProcTxSecFrame` adds. A 24-byte null data
  frame has a length of 28, and a protected data frame a length 12 bytes over its frame.
  TODO(owner): where libpp adds the 4. On a real board the MAC presumably writes the MIC and the
  FCS there, so that the guard word never goes on the air. TODO(owner): no capture of a real
  board's frames was compared.
- esp32sim reads the descriptor's length in bytes (`esp32/src/wifi.rs:193`), and
  `VirtualAp::on_station_tx` takes the last 8 off a protected frame, as its MIC, and nothing off an
  open one (`esp-soc/src/wifi.rs:370-378`). Either way 4 bytes past the frame remain: on an open
  network where its FCS would be, on WPA2 where the first 4 bytes of the MIC would be. Both hold
  the guard word.

`http_request`, built with ESP-IDF v5.3.6 and run at esp32sim's pin, showed this over WPA2, and a
build of it with an empty password on an open network: every data frame the station sent, IPv4,
ARP and IPv6 alike, ended in the guard word, and an ICMP echo reply of 1500 bytes came through as
an `eth_tx` of 1518. Other ESP-IDF versions were not checked. To check it again, at another pin or
ESP-IDF version: with `ESP_EMU_DEBUG=wifi-frames` the runner logs each frame the station sends as
`[wifi] TX queue {queue} desc {address} {length} bytes {kind} …`, the descriptor's length, so a
null data frame shows `28 bytes null (2/4)`; the 4 bytes themselves are the end of each `eth_tx`.

`Ethernet.receive` and `Station.receive` leave the 4 bytes out: ARP reads 28 bytes, and an IPv4 datagram ends at its
total length. An `eth_rx` outside the range is a protocol error, as is `eth_rx` on a run without
`--wifi` or `--wifi-sta`; the harness's codec refuses an `eth_tx` outside the range too.

`eth_tx`: a frame the station sent. It is an event: it carries `seq`, sets the block point, and an
answer stamped `t + latency` is on time (rule 2). esp32sim's relay hands over frames without their
time (`StationLink::take_relay_frames`, `esp-soc/src/wifi.rs:861-863`), so `t` is the slice
boundary at which the runner took them (6.1): at most one emulated millisecond after the station
sent them, or at most `latency` while no event is unacknowledged. The runner also takes them right
before a chip reset, so they go out before its `reset`, and at the end of the run. Frames taken at
one boundary go out in the order the station sent them, one message each, with one `t`. The relay
holds at most 64 frames between two boundaries and drops the rest, and any frame over 1518 bytes,
which `stats.wifi.eth_tx_dropped` counts (5.9).

`eth_rx`: from `t` the frame is for the station. It goes into the input queue (section 8) and,
when it comes due, the runner hands it to the station's link, whose queue holds 64 frames
(`StationLink::receive_relay_frame`, `esp-soc/src/wifi.rs:866-873`). Frames the link does not take
wait in the runner, in order, and are offered again at every slice boundary, at most one emulated
millisecond apart. esp32sim then puts one frame on the air for the station at most every 400 µs,
and only when the access point has nothing of its own due: its answers and beacons go first
(`StationLink::next_rx`, `esp-soc/src/wifi.rs:783-828`). The access point wraps a frame for the
air only while the station is associated with it (`VirtualAp::data_from_ds`,
`esp-soc/src/wifi.rs:477-478`); one the link takes before then is lost there, and still counts in
`stats.wifi.eth_rx`. A frame that comes due while the access point is off the air (5.16) is
dropped and counted in `stats.wifi.eth_rx_dropped`.

A peer answers a frame within its `latency` like any event. `BoardLink` gives every `eth_tx` to
the one interface that is the network behind the access point (`Interface.ethernet`), and sends
each output of it as one `eth_rx`.

### 5.16 `wifi_ap` *ext* (M7)

```json
{"type": "wifi_ap", "t": 10000000000, "on": false}
```

From `t` the access point of `--wifi` is off the air (`on: false`) or on it (`on: true`). `on` is
JSON `true` or `false`; anything else is a protocol error, as is `wifi_ap` without `--wifi`, including with `--wifi-sta`. It
goes into the input queue (section 8) like any input. The access point is on the air from
power-on; a switch to the state it is in already does nothing.

Off: the access point leaves as one that shuts down. If a station has authenticated or associated
with it, it drops the answers it had queued, queues a deauthentication with reason 3, "the sending
station is leaving" (IEEE 802.11-2020, 9.3.3.12 and Table 9-49), and stops beaconing. At every
slice boundary the runner then checks whether anything but beacons is still in the access point's
queue; once nothing is, the access point is detached (`StationLink::attach(None, None)`,
`esp-soc/src/wifi.rs:586-588`): at the first boundary after esp32sim has put the deauthentication
on the air. With no station joined, it waits only for answers already queued, such as probe
responses. Until it is detached it still answers what the station sends; a station that
authenticates again in that window is deauthenticated again. The networks around
(`--wifi-nearby`) go off the air with it: esp32sim keeps them on the access point. Frames the
runner still held for the station are dropped, as are `eth_rx` frames that come due while it is
off or leaving; frames the station's link had taken are lost with it.

The deauthentication is there because at the pin esp32sim's station does not notice beacons that
stop coming: with the access point merely detached, ESP-IDF's `http_request` example kept its
connection state for 60 emulated seconds, and only its TCP connects failed, with errno 113.

On: a fresh access point from the same `--wifi` spec, with the same networks around, as at
power-on. The station must join it again: scan, authentication, association, the WPA2 handshake
with a passphrase, then DHCP from the peer. An `on` that comes while the old access point is
still leaving, an `off` and an `on` at one `t` among them, takes effect once it has left, at most
a slice or two later: attaching the fresh one would take the deauthentication off the air, and the
station would never learn that it had to join again.

### 5.17 `power` *ext* (#105)

```json
{"type": "power", "t": 12000000000, "on": false}
```

From `t` the board's supply is off (`on: false`) or on (`on: true`). `on` is JSON `true` or
`false`; anything else is a protocol error. It goes into the input queue (section 8) like any
input. The supply is on from the start of the run; a switch to the state it is in already does
nothing.

Off: the guest stops where it is, between two instructions, and runs nothing until the supply
comes back. The runner still takes the peer's turns at their block points, and applies the inputs
at their times, so the run's time goes on (section 3); a slice without the guest takes no wall
time. The chip drives no pad and routes no UART: a `uart_cfg` with no pins goes out for every
port whose route touched a watched pin, and a `gpio_out` with empty `enabled` and `output` and,
in `changes`, every watched pin the peer last saw high, at `0`; both at the switch's `t`. Bytes
that come due while the supply is off, those of a `uart_rx` still being paced included, are
dropped as `unrouted_rx` (5.8). Levels the peer drives stay driven, and the chip has them when it
comes back. A card in the slot of `--sd-spi` loses its power: it leaves SPI mode and forgets its
initialisation, and a block it had not received whole is lost; the blocks it took are in the
image already. With `--wifi`, the station leaves the air without a word, the access point forgets
it at once, and `eth_rx` frames that come due while the supply is off are dropped
(`stats.wifi.eth_rx_dropped`). A `wifi_ap` while the supply is off only records whether the
access point is on the air: there is no station to deauthenticate, and the power-on reset puts it
on the air, or leaves it off, as the peer last switched it.

On: a power-on reset of the chip, then `reset` with cause 1 (5.5), and what follows a reset. The
chip starts as at the start of the run, but for its flash:

| | Kept | Starts again |
|---|---|---|
| Flash: the build, NVS, OTA data, everything the guest wrote, in the runner's memory | ✓ | |
| eFuses | ✓ | |
| SRAM (DRAM, IRAM, the cache's RAM), RTC fast and RTC slow memory | | zeroed |
| PSRAM | | zeroed |
| RTC registers, the RTC timer, the RTC watchdog | | as at power-on, `RESET_STATE` saying `POWERON_RESET` |
| The cores and the digital peripherals | | as at every reset (`reboot()`) |
| The levels the peer drives on the pins | ✓ | |

The runner does it with esp32sim's `reboot()`, then writes zeros over SRAM and the RTC memories
through the bus, so that code esp32sim decoded from them is decoded again, loads the ROM's ELF
again for the data its reset handler expects in DRAM, and replaces the RTC registers with a new
chip's. With `--wifi` the station comes back to a fresh access point, as at power-on, which it
must join again; the access point stays off the air if the peer switched it off.

The flash lives in the runner, from the `--flash-at` files read at the start, and is never
written back to them: what the guest wrote lasts as long as the run. While the supply is off the
peer may write into it too (`flash_write`, 5.25), for the next boot to find. esp32sim carries out
each program and erase command whole when the guest issues it, so a power cut falls between two
commands, never inside one: torn pages and half-erased sectors are not modelled.

### 5.18 `chip_reset` *ext* (#105)

```json
{"type": "chip_reset", "t": 4500000000}
```

At `t` the chip's EN pin is pulsed, as a board's reset button does: a power-on reset of the chip
alone, as `power` gives one when the supply comes back (5.17), then `reset` with cause 1 (5.5). The
board keeps its power: the SD card stays where it was, and so does what the peer models around
the chip. It goes into the input queue (section 8) like any input. With the supply off it does
nothing. The pulse takes no time: the guest runs again from the same `t`.

### 5.19 `sd_card` and `sd_card_ack` *ext* (#106)

```json
{"type": "sd_card", "t": 2000000000, "image": null}
{"type": "sd_card", "t": 2500000000, "image": "/tmp/esp32tl-sd-x1/card.img"}
{"type": "sd_card_ack", "t": 2000000000, "image": null}
{"type": "sd_card_ack", "t": 2500000000, "image": "/tmp/esp32tl-sd-x1/card.img"}
```

`sd_card`, from the peer, changes what the slot of `--sd-spi` holds from `t`: with `image` a path,
a card holding that image file goes into the empty slot; with `image` `null` or absent, the card
comes out. It goes into the input queue (section 8) like any input, and the runner applies it at
the first slice boundary at or after `t` (6.1): between two of the chip's SPI transactions, never
inside one, since the card answers each transaction whole in the board's `spi_transfer_pins`. A
relative path is the runner's working directory's; `BoardLink` sends absolute ones. `image` that is
not a string is a protocol error, as is `sd_card` without `--sd-spi`.

`sd_card_ack`, from the runner, answers each `sd_card` once it is applied, in the order they were
sent: `t` is the slice boundary it was applied at, `image` what the slot holds then, `null` when it
is empty, and `error`, `null` or absent unless the runner refused the change, why. A refused change
leaves the slot as it was. The runner refuses a card for a slot that holds one already
(`<image>: the slot holds <held> already`) and an image it cannot open for reading and writing or
whose size is not a whole number of 512 KiB units up to 2 TiB, naming the image and the error
(`<image>: No such file or directory (os error 2)`). Taking the card out of an empty slot changes
nothing and is no error. `sd_card_ack` is not an event: nothing answers it, it carries no `seq`
and sets no block point, and it reaches the peer in `t` order with the other messages, before the
first `sync` whose `t` is at or after its own. `BoardLink` hands it to the one interface that is the slot
(`Interface.card_slot`), which fails the link on an `error`, and on an `image` other than the one
its `sd_card` asked for.

What the slot does, in `runner/src/sd_card.rs` (`Slot`):

- **An empty slot**, and a card that is not selected or whose MISO is not routed to the master,
  gives the chip 0xff for every byte: the line's pull-up. A host waiting for an R1, a data token or
  a data response times out; one polling for the end of a busy time (MISO low) sees it at once.
- **Taking the card out** closes its image. Every block the card answered `0x05` (data accepted)
  for is in the image already, since the card writes through; a block still coming in, the rest of
  a multiple block write and a read in progress are dropped. Nothing the firmware had not finished
  is written for it, so a file system on the image is as the firmware left it at that instant,
  which need not be consistent.
- **A card that goes in** starts as a card just powered: in SD mode, answering nothing until a
  CMD0 with its CS low, then idle until the host initialises it again (CMD8, ACMD41). Its CID and
  CSD come from the image's size, as at power-on, so two images of one size read as the same card.
- A chip reset changes nothing in the slot: the card keeps its state until CMD0 (5.5).
- **With the supply off** (5.17) the slot still changes: an `sd_card` is applied at its time like
  any input, and acknowledged. The card in the slot has lost its power, and one put in meanwhile
  is the same as any other, so the guest that comes back initialises whichever is there.

The runner's line at the end of the run counts the commands and blocks of every card the slot
held, and, when the peer changed the card, how many went in and came out.

### 5.20 `sd_fault`, `sd_clear` and `sd_op` *ext* (#107)

```json
{"type": "sd_fault", "t": 2500000000, "id": 1, "op": "write", "action": "error", "count": 1}
{"type": "sd_fault", "t": 3000000000, "id": 2, "op": "read", "action": "delay",
 "delay": 50000000, "blocks": [64, 127]}
{"type": "sd_fault", "t": 3000000000, "id": 3, "op": "write", "action": "observe"}
{"type": "sd_clear", "t": 4000000000, "ids": [2]}
{"type": "sd_op", "t": 3067868737, "fault": 2, "op": "read", "blocks": [100, 100],
 "action": "delay", "delay": 50000000, "written": false}
{"type": "sd_op", "t": 3101529437, "fault": 3, "op": "write", "blocks": [541, 541],
 "action": "observe", "delay": null, "written": true}
```

With `--sd-spi`, the peer can make the SD card in the slot fail or slow down at block level, or
only watch what the guest does with it, after the firmware has initialised it or from power-on, and
learns what each of its rules did. The card answers SPI in the runner, within each transaction
([runner-technical.md](features/runner-technical.md#the-sd-card)): the rules are the peer's, the
card applies them.

`sd_fault`: from `t` the card has the rule `id`. It goes into the input queue (section 8) like any
input, and applies to the block operations that start once it is in place.

- `id`: a non-negative integer the peer chooses, once per run: one the run has seen, in an earlier
  rule or queued, is a protocol error (`sd_fault: id N is in use already`).
- `op`: `read`, a block read, of CMD17 or each block of CMD18 the host clocks on for; `write`, a block written, of CMD24 or
  each block of CMD25, once its 512 bytes and CRC are in and its CRC is good; or `erase`, a CMD38
  over its whole range. Each block of a multiple block read or write is one operation.
- `action`: `error`, `delay` or `observe`. An error for a read is the data error token `0x04`, "card
  ECC failed", in place of the data block, after which the card sends nothing more of the read, a
  CMD18 included; for a write it is the data response `0x0d`, "write error", and the block on the
  image stays as it was; a CMD25 then waits for its next token, as after any write error. An erase
  cannot fail: `error` with `erase` is a protocol error. `observe` changes nothing: the operation
  goes on as without the rule, its bytes and their timing included, and the rule only reports it. It
  takes any `op`.
- `delay`: with `delay` only, required, 1 ns to 10 000 000 000 ns (10 s), measured on the bus's
  clock from the operation's start. A read's data block goes out once it has passed: until then the
  card sends `0xff`, its access time (N<sub>AC</sub>). A write is busy after its data response, an
  erase after its R1: the card holds MISO at `0x00` (SD spec §7.2.4) and hears nothing that comes in,
  a command included, until the delay has passed; with CS high it lets go of MISO. A write or erase
  is in the image from its data response or R1 on: the busy time is time only. `delay` with
  `error` or `observe` is a protocol error (`sd_fault: an observe takes no "delay"`).
- `count`: how many operations the rule applies to before it is gone, over every card it meets, 1
  to 2⁶³ − 1; `null` or
  absent, every one until it is cleared.
- `blocks`: `[first, last]`, the first not after the last; `null` or absent, any block. A read or
  write meets the rule when its block is in the range, an erase when its range overlaps it. The
  card may change before the rule comes due or after it (5.19), so the runner does not hold the
  range against a card's size: a block past the last of the card in the slot is never met.

An operation meets at most one rule that fails or delays it: of those whose `op` and `blocks` it
matches, the one given last, so a one-shot rule given after a lasting one applies first, and the
lasting one again once it is spent. It also meets every rule with `observe` whose `op` and `blocks`
it matches, whenever they were given: one with `observe` given last does not take the operation
away from a rule that fails or delays it. Each rule an operation meets counts it and reports it
with an `sd_op` of its own. A rule counts the operation when the operation starts: a read at its
command or, for the next block of a CMD18, at the first byte the host clocks after the block before
it that does not start a command; a write at its data response; an erase at its CMD38. So a CMD12
right after a block's CRC, as ESP-IDF's `sdspi` sends it after the last block it wants, begins no
further block and meets no rule; a host that clocks idle bytes before its CMD12 has begun the next
block, which counts. A rule that comes due while an operation is under way leaves it alone, and so
does a clear: a delayed read or a busy write keeps the delay it met.

The rules are the peer's and outlive the card: they stay through `power`, `chip_reset` and a change
of card, and apply to whichever card is in the slot; one given while it is empty waits for the next.
A delayed read and a busy time end when the supply goes off (5.17) and when the card comes out
(5.19), and go on through a `chip_reset`, since the card keeps its power (5.18).

`sd_clear`: from `t` the rules `ids` are gone, or, with `ids` `null` or absent, all of them. An id
no rule has, one spent or cleared already among them, is no error. Into the input queue, like
`sd_fault`.

Without a rule the card is as it always was: it answers at once.

`sd_op`: what a rule did, one message per operation, at `t` the operation's start as above. It is
not an event: it carries no `seq`, sets no block point and asks for no answer, and comes in `t`
order with the events, before the `sync` of the turn whose block point is at or after its `t`.
`fault` is the rule's `id`; `op` and `action` are the rule's; `blocks` the operation's first and
last block, one block for a read or a write; `delay` the rule's delay, `null` for an error and an
observe; and `written` whether the card wrote the image: `false` for a read and a failed write,
`true` for a write or erase the card carried out, delayed or observed. A write that the image
itself refuses, as without the rule, is `false` too, and so is a write a rule with `observe` met
that another rule failed. An operation that meets several rules has an `sd_op` for each, at the
same `t`, in the order the peer gave the rules. The runner's `SD card:` line counts the operations
that a rule with `error` or `delay` met as `N operations faulted`, and those that one or more rules
with `observe` met as `N operations observed` ([runner](features/runner-user.md#what-it-prints)).
The harness's `BoardLink` hands each `sd_op` to the SD card slot, which fails the link on a `fault`
it never gave and on an `op`, `action` or `delay` that is not that rule's.

The card's clock is the bus's: the board tells the slot the bus cycle at every tick
(`BoardModel::advance_to`), and a transaction happens at the cycle of the tick before it, which is
`bus.cycles()` at the register write that starts it (`esp32/src/bus.rs:641-653`, `:327-330`). All the
bytes of one transaction are at one time. So a delay passes only between transactions, as a
firmware's driver polls: ESP-IDF's `sdspi` polls a busy card one byte per transaction until its
timeout, `esp_timer_get_time()` on the same clock, and gives up with `ESP_ERR_TIMEOUT` once
it has passed (its write timeout is 5 s, its read timeout 1 s).

### 5.21 `flash_dump` and `flash_dump_ack` *ext* (#131)

```json
{"type": "flash_dump"}
{"type": "flash_dump_ack", "t": 2000000000, "error": null}
{"type": "flash_dump_ack", "t": 2000000000, "error": "/tmp/esp32tl-x1/flash.bin: No space left on device (os error 28)"}
```

The flash goes to the peer through a file, the same way `--flash-at` and an SD card's image reach
the runner: a path both sides can read. `--flash-out PATH` names it.

`flash_dump`, from the peer in its turn, asks for the flash now. It carries no `t` and does not go
into the input queue: the runner writes the whole flash to `PATH` while it reads the turn, answers
`flash_dump_ack`, and goes on reading the same turn, up to its `wait` or `stop`. No slice runs, so
the bus's clock does not move, and an input the peer sent before it in the turn has not been
applied. A peer may send any number of them in a turn. Each answer starts `--peer-timeout` again
(section 9): the write is the runner's time, not the peer's. `flash_dump` without `--flash-out` is a
protocol error (`flash_dump without --flash-out`).

`flash_dump_ack`, from the runner, says that `PATH` holds the flash: `t` is the `sync`'s, and `error`,
`null` or absent unless the file could not be written, says why, as the path and the error. The run
goes on either way. It is not an event: it carries no `seq` and sets no block point.

The runner writes `PATH.partial` and renames it, so whoever reads `PATH` finds one instant's flash
whole, or the file from before. `PATH` is the flash as the guest has it at the slice boundary the
turn was taken at, which may lie past the `sync`'s `t` (section 6.1): a write of the guest's after
the block point is already there, though its `uart_tx` or `console` goes out after the `wait`.

On the ESP32-S31 esp32sim keeps what the guest programs and erases in its cache's own copy of the
NOR array, and hands it to the flash the runner reads only at a chip reset (`reboot`,
`esp32s31/src/soc.rs`), with no other way to read it. So with the supply on, `PATH` there is the
flash as of the chip's last reset. With the supply off from the bus's current cycle on, as for
`flash_write` (5.25), the runner resets the chip first (`Guest::sync_flash`), which changes nothing
the chip runs: its next instruction comes after the power-on reset. TODO(owner): the S31's flash
while the supply is on, once esp32sim lets the runner read the cache's array.

**At the end of the run** the runner writes `PATH` on its own, however the run ended, exit codes 1,
3 and 4 included (section 9), before it sends `stats`: a peer that has read `stats` finds the file in
place. That covers what the guest wrote after the peer's last turn, when no turn is left to ask in.
A file that cannot be written then is a line on stderr, `esp32tl-runner: cannot write the flash:
PATH: ERROR`, and changes no exit code. A panic (101), a signal that kills the process and a failed
connection write nothing. Without `--board` the end of the run writes it too.

**Rejected:** base64 chunks on the socket
([decision](decisions/runner.md#the-flash-reaches-the-harness-through-a-file-not-the-socket)).

### 5.22 `hci_tx` and `hci_rx` *ext* (#34)

```json
{"type": "hci_tx", "seq": 3, "t": 412345678, "data": "AQMMAA=="}
{"type": "hci_rx", "t": 413345678, "data": "BA4EAQMMAA=="}
```

With `--bt ELF`, the guest's Bluetooth controller is the peer's. esp32sim's VHCI hooks, installed
over the symbols of the application's ELF as its `--ble` installs them, take the place of the
controller: the guest's host (Bluedroid or NimBLE) sends its H4 packets through
`esp_vhci_host_send_packet`, and esp32sim's controller task, in the guest, hands it the
controller's through the host's receive callback. The runner puts a controller of its own in
esp32sim's session, in place of esp32sim's virtual BLE controller, that forwards each packet to
the peer and gives the host the peer's (`runner/src/hci.rs`,
[runner-technical.md](features/runner-technical.md#bluetooth-the-guests-hci-on-the-socket)). The
example is the host's first command, `HCI_Reset`, and the controller's `Command_Complete` for it a
millisecond later.

`data` is one H4 packet with its packet-type byte first: `01` a command, `02` ACL data, `03` SCO
data, `04` an event (`05` ISO data with LE Audio). Neither side looks into it further. It is 1 to
496 bytes, the most esp32sim's VHCI carries either way: its buffer is `BUFFER_SIZE`, 512 bytes
(`esp-soc/src/ble/vhci.rs:9`), and the packet starts 16 bytes into it, after its 4-byte length word
and the controller task's name, `vhci` (`:198`, `:338-340`). esp32sim drops a longer packet from the
host with `[ble] rejected invalid VHCI packet pointer, length or controller state`
(`esp-soc/src/ble/vhci.rs:309-316`), and one for the host with `[ble] rejected oversized or
unmapped controller packet` (`:338-345`), so the runner refuses an `hci_rx` outside the range as a
protocol error, `hci_rx: an H4 packet of N bytes; esp32sim's VHCI carries 1 to 496`, as it does
`hci_rx` on a run without `--bt` (`hci_rx without --bt`); the harness's codec refuses an `hci_tx`
outside the range too. A controller that wants the host's ACL packets to fit sizes its ACL buffer
to 491 bytes: 496 less the 5 of the packet-type byte and the ACL header.

`hci_tx`: a packet the guest's host sent. It is an event: it carries `seq`, sets the block point,
and an answer stamped `t + latency` is on time (rule 2). Unlike `eth_tx`, `t` is the packet's own:
esp32sim's hook moves the controller to the bus's cycle before it hands it the packet
(`intercept`, `esp-soc/src/ble/vhci.rs:226-227`), and the runner keeps that cycle with it. Packets
go out in the order the host sent them, merged by `t` with the `uart_tx`, `console` and `gpio_out`
of the same slice; at the same `t` a UART byte or a pin change goes first.

`hci_rx`: from `t` the packet is for the guest's host. It goes into the input queue (section 8), and
when it comes due the runner queues it for the host, which reads it at its controller task's next
VHCI poll, not at `t`. esp32sim services the controller only at the task's polls ("The machine
currently services controllers only at guest adapter polls", `esp-soc/src/ble.rs:10-11`), and the
task delivers at most one packet a poll: after a packet its next poll returns nothing, so that the
host finishes with it, and with nothing to deliver the task sleeps one FreeRTOS tick
(`vTaskDelay(1)`, the trampoline described at `esp-soc/src/ble/vhci.rs:18-20`; the `Poll` hook at
`:328-351`). The host gets at most one controller packet a tick, 10 ms at ESP-IDF's default
`CONFIG_FREERTOS_HZ=100`; what comes due meanwhile waits, in order. Nothing in the runner changes
that pace. Nothing reaches the host either before `esp_bt_controller_enable`, or after
`esp_bt_controller_disable` (`:276-280`), or before it registers its callbacks (`:335`): packets
wait, and a later `enable` delivers those from before the `disable`. A chip reset or a power-on
reset, and the guest's `esp_bt_controller_deinit`, lose the packets the host has not read
(`Ble::reset`, `esp-soc/src/ble/vhci.rs:146-153`); the host's packets from before the reset still go
out, before its `reset`.

A peer answers a packet within its `latency` like any event. `BoardLink` gives every `hci_tx` to
the one interface that is the guest's Bluetooth controller (`Interface.bluetooth`), and sends each
output of it as one `hci_rx`. Without such an interface the packets go nowhere, and the host waits
for answers that never come.

`flash_dump` and the end of the run (5.21) write the flash as the guest has it: from the guest's
`esp_bt_controller_init` on, esp32sim's controller task runs from a trampoline it wrote into the
flash over the function's code (`install`, `esp-soc/src/ble/vhci.rs:177-200`), and the runner
writes the code esp32sim keeps for `reboot()` to put back (`Ble::original_flash`) in its place. A
`flash_write` (5.25) over the trampoline writes that copy too, so that `reboot()` puts the peer's
bytes back.

`esp_bt_controller_enable` takes the mode of the config given to `esp_bt_controller_init`, and no
other, as ESP-IDF does (`esp-soc/src/ble/vhci.rs:261-275`, from mikroverk/esp32sim#347): a
firmware that enables mode 2, BR/EDR only, as ESP-IDF's `bt_spp_initiator` does, sends its packets
as `hci_tx` like any other. esp32sim then prints `[ble] the virtual controller is LE-only: enabled
in mode 2 (BR/EDR), its commands get Unknown HCI Command` on the runner's stderr, which with `--bt`
does not hold: the controller is the peer's.

### 5.23 `console_rx` *ext* (USB console)

```json
{"type": "console_rx", "t": 1100000000, "data": "aGVscAo="}
```

With `--console-port usb` the console is the chip's USB-Serial/JTAG controller, esp32sim's
`UsbSerialJtag` (`esp-periph/src/usb_serial_jtag.rs`), on the ESP32-S3, ESP32-C3, ESP32-C6, ESP32-P4 and ESP32-S31; the
classic ESP32 has none, and the runner refuses the flag there before the run (exit code 2). The
controller has no pins and no line rate, so its input is not `uart_rx`: `console_rx` carries the
bytes a USB host sends the guest, the example's `help\n`. Its output is `console` (5.7).

From `t` the bytes are the host's. The message goes into the input queue (section 8), and when it
comes due the runner hands the whole of `data` to esp32sim's `serial_input` (`esp-soc/src/soc.rs:201`),
at once: no pacing, since USB has no character time the guest could see. esp32sim cuts it into USB
packets of 64 bytes and puts the next one in the controller's RX FIFO, with its interrupt, once the
guest has read the one before (`UsbSerialJtag::host_input`), as Arduino's HWCDC driver needs; nothing
is dropped. A chip reset, a power-on reset or a wake from deep sleep loses what the guest has not
read: esp32sim makes its peripherals again (`Machine::reboot`), and the lost bytes are not counted
in `console_rx_lost`. While the board's supply is off no USB device is there to
take the bytes: the runner drops them and counts them in `stats.console_rx_lost` (5.9). Empty
`data` changes nothing.

On the ESP32-P4 and the ESP32-S31 `console_rx` is also UART0's input with `--console-port uart0`,
the default: esp32sim tells the board no UART pins on these chips, so `uart_rx` cannot reach UART0
there, and the console's output is `console` (5.7) stamped with the end of the slice it was
written in, as on USB. When the message comes due, the P4's runner hands `data` to esp32sim's
`uart_input` (`esp32p4/src/soc.rs:259-265`), which puts into UART0's 128-byte RX FIFO what fits and
releases the rest at UART0's baud rate; the S31's keeps it and puts one byte into the RX FIFO per
character time, ten bits at UART0's baud (115200 until the guest sets one), while the FIFO has room
and UART0 takes it, since esp32sim's own S31 `uart_input` drops what does not fit and refuses a
byte while UART0's clock or receiver is off; a refused byte waits for the next character time.
Nothing is dropped either way; a
reset loses what UART0, or on the S31 the runner's queue, has not taken yet, uncounted, and bytes
that come due with the supply off are counted in `stats.console_rx_lost`, which `stats` then has.

`console_rx` on a run without `--console-port usb` on a chip whose UART0 has pins is a protocol
error, `console_rx without --console-port usb, on a UART0 with pins`.

`BoardLink` sends each output of the one interface that is the console without pins
(`Interface.pinless_console`) as `console_rx`, cut into messages of at most 512 KiB, as `uart_rx`.

### 5.24 `uptime` *ext* (#245)

```json
{"type": "uptime", "t": 49000000, "earliest": 48000000, "latest": 49000000}
{"type": "uptime", "t": 1000000, "earliest": 0, "latest": 0}
{"type": "uptime", "t": 3000000, "earliest": null, "latest": null}
```

Where the boot's uptime counter, the hardware counter ESP-IDF's `esp_timer_get_time()` reads,
counted zero, on the socket's clock: no sooner than `earliest` and no later than `latest`, in
nanoseconds. Unlike the other times (section 3), `earliest` is rounded down and `latest` up, so that
the two hold the zero. Both are `null`, never one alone, when the runner no longer knows where the
counter counts from: it counts from a zero that may lie before power-on, so the zero it sent for the
boot no longer holds and it has no other to send. A counter that stops, as the classic chip's does
in deep sleep, leaves the zero sent: it still dates every stamp the firmware took before the stop. A
stamp of `u` the firmware took from the counter was taken between `earliest + u` and `latest + u`.
The message belongs to the boot that runs when it comes: after the run's `n`-th `reset` (5.5), boot
`n + 1`. `t` is the slice boundary the runner saw it at. It is not an event: it carries no `seq`,
sets no block point and nothing answers it. The first example is the classic chip's, the second the
ESP32-S3's at power-on, the third a load that counts from before power-on.

The runner reads the counter's registers at every slice boundary while the board's supply is on, at
most one emulated millisecond apart while the chip runs (section 6.1), as it reads the UART routes
(5.3). It reads no guest memory, and what it reads changes nothing the guest sees.

| Chip | The counter | What a boundary tells | `latest − earliest` |
|---|---|---|---|
| Classic ESP32 | TIMG0's LACT, ESP-IDF's only `esp_timer` implementation there (`CONFIG_ESP_TIMER_IMPL_TG0_LAC`, `esp_timer_impl_lac.c`) | whether it counts, and the count it was loaded with: esp32sim keeps the count itself private | the time between two boundaries, at most 1 ms; for a load while it counts, between three, at most 2 ms |
| ESP32-S3, -C3, -C6 | SYSTIMER unit 0, `SYSTIMER_COUNTER_ESPTIMER` (`esp_timer_impl_systimer.c`) | the count, `unit[0]` of esp32sim's `Systimer` | under one 16 MHz tick: 14 bus cycles on the S3, 9 on the C3 and the C6, 60 ns at most with the rounding |
| ESP32-P4 | the same, `periph.systimer.st`, which esp32sim ticks 2 for every 5 XTAL ticks (`SystimerP4`, `esp32p4/src/periph.rs`): 22.5 bus cycles a tick at 360 MHz | the count, rounded down to bus cycles | 23 bus cycles, 65 ns at most with the rounding |
| ESP32-S31 | none: its SYSTIMER's rate is esp32sim's own state (`SystimerS31`'s private divider) | nothing | no `uptime` |

**The classic chip.** The LACT counts while LACT_EN, bit 31 of LACTCONFIG, is set, from what
LACTLOAD last loaded, at the APB clock divided by LACT_DIVIDER (ESP-IDF v5.5
`components/soc/esp32/register/soc/timer_group_reg.h`). The APB clock is 80 MHz with the CPU on the
PLL and 40 MHz otherwise, as esp32sim counts it (`Peripherals::apb_divider`, `esp32/src/periph.rs`).
ESP-IDF's `esp_timer_impl_early_init` loads 0 and only then sets LACT_EN, at the start of the
application, in its CORE init stage (`esp_timer_init_nonos`), after the ROM, the second-stage
bootloader and the application's own start-up. The first boundary of the boot that sees it count
sends `uptime`: `latest` is that boundary, `earliest` the boundary before it, which saw it stopped,
or the reset that started the boot, both less the time the load it counts from takes at the divider
and the APB clock, 0 for ESP-IDF. For `firmware/persistence` the first boot's counter starts between
48 and 49 ms after power-on (`tests/test_power.py`).

A load while it counts, as `esp_timer_impl_set` and `esp_timer_impl_advance` write after a light
sleep without touching LACT_EN, is seen as a new LACTLOADLO and LACTLOADHI. The boundary that sees
them may come between the guest's write of them and its write of LACTLOAD, which ESP-IDF makes in
the same critical section, so the runner places the load one boundary later: between the boundary
before the one that saw it and the one after, less the time the new load takes. A change of
LACT_DIVIDER or of the APB clock with the same load is no new load: it changes the rate, which
ESP-IDF keeps at 2 counts a microsecond, not the zero.

**The ESP32-S3, -C3, -C6 and -P4.** The runner reads unit 0's count while TIMER_UNIT0_WORK_EN, bit 30 of
CONF, is set (`esp-periph/src/systimer.rs`): the counter read zero that many ticks before the
boundary, less the part of a tick it has counted since its last, which esp32sim's clock tree keeps
to itself. esp32sim restarts the SYSTIMER at every reset, as `reboot()` replaces the peripherals,
and does not model the application's own reset of it (`systimer_ll_reset_register` in
`esp_timer_impl_early_init`, ESP-IDF v5.x), so on these chips the counter counts from the reset that
started the boot: `earliest ≤` the `reset`'s `t` `≤ latest`, from the boot's first boundary on. On
the chip itself the application's reset puts the zero at its early init, as on the classic chip.

**When.** The runner sends `uptime` at the first boundary of a boot that sees the counter count, and
again when the boot's counter starts anew or is loaded anew: on the classic chip, when a boundary
sees it counting after one saw it stopped, and a boundary after one saw a new load; on the others,
when a boundary reads a zero outside the one it sent. It sends `null` for both when a boundary finds
that the zero it sent no longer holds and has none to put in its place: the counter counts from a
zero that may lie before power-on, which has no time on the socket, as a load of more than the run
has lasted gives. It sends `null` once, until it has a zero again. A boundary that sees the counter
stopped sends nothing: a stop does not move the zero, so the one sent holds for the stamps taken
before it, and a start after it is compared with that zero, on the other chips as at any boundary:
the same zero sends nothing, another sends `uptime`. On the classic chip esp32sim's
`enter_deep_sleep` replaces both timer groups, which clears LACT_EN, so the boot that sleeps keeps
the zero it had and the wake's `reset` starts the next boot. A firmware that
never starts the counter gets no `uptime`: on the classic chip, one that does not use ESP-IDF's
`esp_timer`. While the board's supply is off the chip has no counter; the next boot's `reset` starts
again (5.17).

On the classic chip the application can stamp with the counter before the boundary that reports it.
At a start, what it sent in that slice comes before the `uptime` on the socket, by at most the
slice, while the boot has no zero yet. After a load while the counter counts, what it sent in the
next two slices comes before the new `uptime`, and until then the boot's zero is the one sent before
the load: for ESP-IDF's `esp_timer_impl_advance` after a light sleep, which makes up for the time
asleep, that is off by what the making up got wrong.

### 5.25 `flash_write` and `flash_write_ack` *ext* (#278)

```json
{"type": "flash_write", "offset": 36864, "path": "/tmp/esp32tl-x1/nvs-9000-t2000000000.bin"}
{"type": "flash_write_ack", "t": 2000000000, "error": null}
{"type": "flash_write_ack", "t": 2000000000, "error": "/tmp/esp32tl-x1/nvs-3fc000.bin: 24576 bytes at 0x3fc000 end past the flash, of 4194304 bytes"}
```

The bytes reach the runner through a file, as `--flash-at` and an SD card's image do (5.21).
`flash_write`, from the peer in its turn, writes the whole file `path` into the flash at `offset`,
in bytes from the flash's start, now. Like `flash_dump` it carries no `t` and does not go into the
input queue: the runner writes while it reads the turn, answers `flash_write_ack`, and goes on
reading the same turn, up to its `wait` or `stop`. No slice runs, so the bus's clock does not move.
A peer may send any number of them in a turn, and each answer starts `--peer-timeout` again
(section 9). It needs no `--flash-out`.

The board's supply must be off: off since an earlier turn, or switched off by a `power` (5.17) the
runner has read and that is due by the bus's current cycle, as the turn's own `power` with the
`sync`'s `t` is. The runner applies such inputs before the guest runs again, so the guest never
runs between the write and the switch. With the supply on, or switched on again by then,
`flash_write` is a protocol error, `flash_write with the supply on` (section 9): the guest could
read the bytes half written, or write over them. What the peer writes is what the guest reads from
its next boot on, and what `flash_dump` writes from now on. On the ESP32-S31 the runner first takes
in what the guest wrote since its last reset, as for `flash_dump` (5.21), since esp32sim's
`write_flash` replaces the cache's whole NOR array with the flash it writes into.

The runner writes through esp32sim's `write_flash`, as it writes the `--flash-at` files at the
start; on the classic ESP32, the ESP32-S3 and the ESP32-C3, bytes over esp32sim's Bluetooth
trampoline also go into the copy of the code `reboot()` puts back (5.22). On the ESP32-C6
esp32sim keeps that copy to itself, so bytes written over the trampoline there go at the next
power-on.

`flash_write_ack`, from the runner, says the flash holds the file: `t` is the `sync`'s, and `error`,
`null` or absent unless nothing was written, says why, as the path and the error: the file could
not be read, or it does not fit in the flash from `offset` on. The run goes on either way. It is not
an event: it carries no `seq` and sets no block point.

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
cycles (section 3); while the board's supply is off the guest does not run, and the next
millisecond is left out (5.17). While no event is unacknowledged, so that `B` has no `latency` term yet, a
slice is also at most `latency` long: an event in it sets `B = t + latency`, and the callback
that records it cannot stop `run`, so a longer slice would carry the guest past that `B`.
Without `--realtime`, while the chip is in a deep sleep with the RTC watchdog off, neither the
next millisecond nor `latency` ends a slice: its digital domain is off, so no route changes and
nothing is recorded, and the only event, the wake, stops `run` at its cycle with the `reset`. The
runner moves the bus through such a sleep in long steps of its own, which tick the RTC, to two RTC
slow ticks before the timer's alarm, and esp32sim's idle rounds run the rest and wake the chip at
the alarm's tick; what the peer sees is the run it would see without the steps, but for a wake up
to one idle round, 512 CPU cycles, from where it would come. Under `--realtime`, and with the RTC
watchdog on, a sleep's slices end as an awake chip's do
([decision](decisions/runner.md#a-deep-sleep-runs-in-long-steps-of-the-rtc-to-just-short-of-its-alarm)). With a
`latency` of `0` a slice is one bus cycle until the next event, which is slow: `hello_world`
runs at about 30 wall seconds per emulated second on an M2 Pro, against 0.3 with a `latency` of
1 µs and 0.1 with none. Between slices, where it has the bus, the runner:

- compares the UART routes (`uart_cfg`);
- delivers the bytes now due, port by port, after checking the route, the baud and the FIFO's
  `rx_pending()`, which is how it sees the drops `warn` reports: to the port's `Uart`, which drops
  a byte that finds the FIFO full, or as a frame error (5.8);
- applies the queued inputs now due: received bytes onto their lines, GPIO levels and releases
  through the board (5.11), Ethernet frames to the station's link and the access point's switches
  (5.15, 5.16), the SD card's rules to the card (5.20), H4 packets to the queue the guest's host
  reads at its next VHCI poll (5.22);
- with `--bt`, takes the H4 packets the guest's host sent in the slice, each with its cycle
  (`hci_tx`);
- with `--wifi`, takes the station's Ethernet frames from the relay (`eth_tx`), asks an access
  point on its way off the air whether it has left, and offers the station's link the frames that
  are due and that it has not taken yet;
- at `B`, takes the turn.

`run` stops close to `max_cycles`, not a quantum past it: on the path without a cost model the
last round is cut to the cycles left (`esp-soc/src/machine.rs:699`; "the cycle ceiling cuts the
final round short", `:511-512`), and the idle skip is bounded by `max_cycles` too (`:608`). The
round is counted in CPU cycles and rounded up into bus cycles (`cpu_cycles_for_bus`,
`esp32/src/soc.rs:118-120`), so the runner gets control at some `T ≥ B` less than one CPU cycle
later. A CPU cycle is 1 bus cycle at 240 MHz, 1.5 at 160 MHz (ESP-IDF's default for the ESP32),
3 at 80 MHz and 6 at the 40 MHz XTAL the chip boots from (`cpu_scale_half`,
`esp32/src/periph.rs:1121-1124`), so `T` is in the bus cycle of `B` itself at 240 MHz and up to 1, 2
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

`Machine::run_until_cycle` (`esp-soc/src/machine.rs:925-937`) stops exactly and would look like the
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
`BoardLink` checks each interface's outputs and `quiet_until` against the turn before it sends
the `wait` (rule 2 of section 7 is the runner's side of it), and its error names the interface and
writes the times as the interface states them, `Interface.instant(t)`: in nanoseconds (`1500 ns`),
or, for a UART whose source is a `Model` (`esp32tl/sources/model/__init__.py`), as a `Duration`
(`0.0000015 s`). The wire carries nanoseconds either way.

## 8. Future-dated inputs

Any input (`uart_rx`, `gpio_in`, `release`, `adc`, `eth_rx`, `hci_rx`, `console_rx`, `wifi_ap`, `sd_card`, `sd_fault`, `sd_clear`) may carry a `t`
later than the turn's `sync`: the runner keeps it in a queue of its own, ordered by `t` and, at equal `t`, by arrival, and
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
| `--max-seconds` reached (`Stop::Halted`, `esp-soc/src/machine.rs:592`) | sends events not yet sent, `stats` with `end: max_seconds`, closes | 0 |
| `stop` from the peer | drops the events held after `B`, sends `stats` with `end: stop`, closes | 0 |
| `SIGTERM` or `SIGINT` | ends the slice in progress (at most one emulated millisecond, or the rest of a deep sleep, a few wall microseconds), or at once if it is waiting for the peer's turn or, under `--realtime`, for wall time on its own clock (12.2). A sleep inside esp32sim's pacing comes to its end first, which is about 2 ms at most but for the board kept off from power-on (12.2); sends the events and `warn`s not yet sent, `stats` with `end: signal`, closes | 0 |
| any other `Stop` (`esp-soc/src/soc.rs:9`: `MaxInsns`, `Breakpoint`, `Unimplemented`, `Simcall`, `Ebreak`, `Watch`, `Exceptions`, `CostModel`, `CostModelLifecycle`) | events not yet sent, `stats` with `end: emulator` and the variant, closes | 1 |
| under `--no-bt`, the guest calls `esp_bt_controller_init`, which the runner's stub answers with `ESP_ERR_NOT_SUPPORTED` | ends the slice the call came in, sends events not yet sent, `stats` with `end: bt_controller`, closes | 5 |
| usage error, or an image or ROM that cannot be loaded | nothing on the socket (it may not be connected yet) | 2 |
| protocol error by the peer: malformed or oversized line, unknown `type`, version or chip mismatch, late `t`, wrong `ack`, `quiet_until` not after the `sync`, a `baud` of `0`, a `watch` with a pin the chip does not have, a message the runner does not implement yet, `eth_rx` without either Wi-Fi mode (`eth_rx without --wifi or --wifi-sta`), `wifi_ap` without `--wifi` (`wifi_ap without --wifi`), or `wifi_ap` in station mode (`wifi_ap with --wifi-sta: there is no virtual access point to switch`), an `eth_rx` frame outside 14 to 1518 bytes (`eth_rx: an Ethernet frame of N bytes; one without FCS has 14 to 1518`), `hci_rx` without `--bt` (`hci_rx without --bt`), an `hci_rx` packet outside 1 to 496 bytes (`hci_rx: an H4 packet of N bytes; esp32sim's VHCI carries 1 to 496`), an `on` that is not `true` or `false`, `sd_card` without `--sd-spi` (`sd_card without --sd-spi`), an `image` that is not a string, `sd_fault` or `sd_clear` without `--sd-spi` (`sd_fault without --sd-spi`, `sd_clear without --sd-spi`), an `sd_fault` that 5.20 does not allow (an `op` or `action` it does not list, an erase that fails, a `delay` missing, out of range or with `error`, a `count` out of range, `blocks` that are not `[first, last]`, an `id` in use already), `flash_dump` without `--flash-out` (`flash_dump without --flash-out`), `flash_write` with the board's supply on (`flash_write with the supply on`) | `stats` with `end: protocol_error`, closes | 3 |
| the peer is gone: the connection failed, the socket closed, a read or write failed, the peer's turn took longer than `--peer-timeout`, or a write made no progress for that long (in or outside a turn) | nothing more | 4 |
| the process that started the runner is gone: end of file on `--parent-fd` | ends the slice in progress, or a wait for the peer, as for `SIGTERM`; nothing more on the socket | 4 |
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

The runner finds a peer gone from the socket only when it reads or writes it, and between the
`sync` of one turn and the next it may not: a window up to `B` with no event in it, the test's
`board.wait` of an hour on a guest that does little, runs at full speed for as long as it lasts,
also after the peer's process died. `--parent-fd N` names a descriptor the runner inherits, the
read end of a pipe whose write end the process that started it holds and passes to no other. A
thread of the runner blocks reading it; at end of file, which comes once that process has ended,
however it ended, it sets the flag `SIGTERM` sets, and the run ends within the time a signal takes,
above, but as the peer gone: no `stats`, the line `esp32tl-runner: end: the peer at BOARD is gone:
end of file on --parent-fd: the process that started the runner has ended at …` and exit code 4.
What the parent writes on the pipe means nothing. The `esp32sim` backend passes it, and closes the
write end only after the runner has exited; a `SIGTERM` that arrives after the end of file ends the
process at once, as a second signal does.

`--peer-timeout SECONDS` bounds, in wall time, how long the runner waits for the peer's turn to
end, counted again after each `flash_dump_ack` (5.21) and `flash_write_ack` (5.25), and how long a
write may make no progress because the peer does not read (default 60; `0` waits for ever). For
`tcp:` it also bounds each connection attempt, though not the name lookup
before it. A hung peer, say a model in another process, then fails the
run with code 4 instead of hanging a CI job. It acts only on that error path, so it cannot change a
run that completes. The `esp32sim` backend passes `0`: its turns last as long as the test body
runs, and the test's own wall-time timeout ends a run that hangs
([pytest plugin](features/pytest-plugin-user.md#the-tests-timeout)).

With `--flash-out`, every end in the table from `--max-seconds` to the peer gone, once the run has
started, writes the whole flash to the file, before `stats` where there is one (5.21).

`--no-bt ELF` is how the `esp32sim` backend runs a test that has not looked at `board.bluetooth`:
without esp32sim's VHCI hooks, a guest that starts its controller would run ESP-IDF's own against
Bluetooth hardware esp32sim does not model, crash and boot-loop. The backend fails such a test
with a message that names `board.bluetooth` ([esp32sim
backend](features/esp32sim-backend-user.md#a-firmware-that-starts-its-controller-without-boardbluetooth)).

`Stop::SwReset` is not an end: the runner sends `reset` and continues (5.5), and neither is the
supply going off (5.17). `stats` is the last
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
| `eth_tx`, `eth_rx`, `wifi_ap` and `stats.wifi` (*ext*), with `--wifi` | #325 has nothing of Wi-Fi. They carry the Ethernet frames of esp32sim's station link in relay mode, and switch its access point, so that the network behind the access point is the peer's, in emulated time ([decision](decisions/harness.md#the-network-the-device-reaches-is-simulated-in-the-harness-in-emulated-time)) |
| `hci_tx` and `hci_rx` (*ext*), with `--bt` | #325 has nothing of Bluetooth. They carry the guest's HCI, so that its controller is the peer's, in emulated time ([decision](decisions/runner.md#the-guests-hci-goes-over-the-board-socket-on-the-boards-clock)) |
| `power` and `chip_reset` (*ext*) | #325's board has no supply and no EN pin of its own; a test resets or power-cycles the board it describes, and the runner does both in one run, on one clock (5.17, 5.18) |
| The SD card slot of `--sd-spi` answers SPI on `SocketBoard`, in the runner; `sd_card` and `sd_card_ack` (*ext*) change the card in it, and `sd_fault`, `sd_clear` and `sd_op` (*ext*) make it fail or slow down | a card answers every byte of a transaction in the same transaction (`spi_transfer_pins`), which a peer a turn away could not; only the card going in and coming out, the peer's fault rules and what they did go on the socket (5.19, 5.20, [runner-technical.md](features/runner-technical.md#the-sd-card)) |

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
    (`esp32/src/soc.rs:24`, `esp-soc/src/machine.rs:608`), and the classic bus never defers device
    ticks (`can_defer` keeps its default `false`, `esp-soc/src/soc.rs:170`). What does stand in the
    way is that board callbacks cannot reach the bus, which `uart_cfg`, the drop warnings and `adc`
    need; the run loop's slices give it to them and stop for `B` as well. For #11 this means:
    `SocketBoard` implements `uses_uart_pins`, `uart_tx` and `gpio_output_at` (recording only),
    not `uart_rx`, `next_deadline` or `advance_to`; bytes reach the chip through
    each port's `Uart` from the run loop (5.8); no `sync` between turns; `Unimplemented` and
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
20. **`SocketBoard` implements `advance_to` when it holds an SD card**, from #107, against item
    10: the card needs the bus's time inside a transaction for the delays of the peer's rules, and
    `spi_transfer_pins` carries none. The bus calls it at every tick (`deliver_board_inputs`,
    `esp32/src/bus.rs:360-362`), so no esp32sim change is needed (5.20). It only sets the card's
    clock; GPIO inputs still reach the chip at the slice boundary (item 16).
21. **The last stretch of the wait at the block point is a sleep**, found while implementing #28,
    where the plan and #27 had the runner wait on the socket with the time left as the timeout. A
    read's timeout is rounded up to the kernel's tick, 4 ms at Linux's usual 250 Hz, on a wait that
    is under 2.3 ms nearly every time (12.2).
22. **The source's bound is at least 128 bytes on the line**, found validating #28 on hardware,
    where #27 had 50 ms at any rate as above an FTDI adapter's 16 ms burst. A CH9102 at 9600 baud
    delivers 64 bytes at once, and 50 ms of that line is 48 bytes: every packet lost its first 16, and
    most NMEA sentences with them, in a run that kept up with wall time (12.3).


### Harness policy for simulated network receive loss

On an interface whose `lossless` property is true (currently a modem with the simulated network),
`BoardLink` treats `warn.kind == "rxfifo_ovf"` on that interface's RX pin as fatal. For LilyGO:
`uart.modem: UART receive overflow (rxfifo_ovf) on GPIO 27: the simulated network requires a lossless route`.
The warning is retained in the report and the run stops as for `frm_err`. Ordinary UART routes
keep overflow as a warning. This changes harness policy only, not message fields or version 2.

## 12. Wall time: `--realtime` and the `real` source (M5)

Designed in #27, before code; the runner's half and the harness's `real` source came with #28.
Nothing here changes the wire: `wall` is a value `clock` already reserved, and `rt_resyncs` a
field `stats` may add (section 2), so the protocol stays at version 2.

A real device on a host serial port, a GNSS receiver on a USB adapter for one, sends when it
sends, in wall time; it cannot be held in a turn or say when it will next send. Putting it at the
far end of an emulated route takes emulated time paced to wall time, a rule for the emulated time
each of its bytes gets, and turns often enough that the rule stays close to the wire.
mikroverk/esp32sim#325 leaves exactly this open: "the mechanics of stamping wall time as emulated
time need their own design" (plan, finding 11).

### 12.1 The runner under `--realtime`

`--realtime` sets `machine.rt.enabled`, which is all esp32sim's flag of the same name does
(`cli/src/lib.rs:958`), though only once the guest has run 2¹⁶ bus cycles (12.2), and the
runner's `hello` says `clock: wall`. esp32sim paces `run` itself (`Machine::rt_pace`,
`esp-soc/src/machine.rs:1099-1123`): every 2¹⁶ bus cycles, 273 emulated µs, it compares emulated
time since power-on with the wall time since its first such check, which takes the wall instant
of that check as emulated time 0 (`wall_start`, set by `get_or_insert_with(Instant::now)`). It
sleeps while emulated time is more than 2 ms ahead, lets the guest run as fast as it can while it
is behind, and once wall time is more than 500 ms ahead it moves its anchor up to emulated time,
skipping the lag, and counts the skip in `rt.resyncs`. Real-time pacing also turns off round
batching and virtual quanta (`:581`, `:785`), which changes the emulator's speed and nothing the
guest sees.

Times on the socket are emulated nanoseconds as in section 3, and every rule of section 6 holds as
written. What changes is what the times mean: they follow wall time, less what a held turn or a
skipped lag took out of it (12.2), so a run under `--realtime` does not repeat, whatever its peer
does.

### 12.2 The runner waits for wall time at the block point

esp32sim's pacing alone lets the guest reach a block point up to 2 ms plus one pacing interval,
about 2.3 ms, before wall time does, and its anchor is private (`Realtime::wall_start`), so the
runner cannot ask it where wall time stands. The runner keeps a wall clock of its own, and starts
it with esp32sim's. esp32sim takes its anchor at its first check, in the first round, with pacing
on, at which the bus has counted at least 2¹⁶ cycles since power-on: `rt.last_check` starts at 0,
and turning pacing on does not reset it. A turn held before that check, by the test or the peer,
would come before one anchor and after the other. So the runner sets
`machine.rt.enabled` only at the first slice boundary at or after 2¹⁶ bus cycles, after any turn
taken there and just before it calls `run` again, and anchors its clock at the same moment with
esp32sim's convention: emulated time 0 is now. esp32sim's first check comes in the first round of
that `run`, microseconds later, so the two anchors agree to within that, whatever turns were held
before. Until then, the first emulated millisecond at most, the run is not paced and the runner
does not wait. When `rt.resyncs` has grown by a slice boundary `T`, the runner moves its anchor so
that `T` is now, as esp32sim moved its own; the two then differ by less than the wall time of one
slice.

A board switched off before that boundary starts both only when the guest runs again. Its slices
take no wall time until then, and do not wait; esp32sim's first check then finds emulated time as
far ahead of its new anchor as the board was off, and sleeps that long, so a test that keeps the
board off from power-on pays for it once, in wall time. A sleep inside `run` looks at no signal and
no timeout, and the backend's teardown kills a runner that has not exited 5 s after `SIGTERM`. The
anchor it would take to avoid this is private (`Realtime::wall_start`).

At each block point `B`, before it sends `sync`, the runner under `--realtime` waits until its
wall clock reaches `B`. It reads the socket in the transport's 20 ms steps while more than a step is
left, as it waits for a turn: a signal ends the wait (section 9), and so does the peer closing the
socket (exit code 4), and what has been queued for the peer for 20 ms goes out meanwhile. The last stretch, under 20 ms and nearly always the whole wait, is a sleep,
since a read's timeout is rounded up to the kernel's tick; a signal or a closed socket is then found
at the turn that follows. What the peer sends meanwhile is kept for that turn. The wait is never
without a bound: it is at most
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
of its own, so the operating system's buffer does not fill while a turn is held, and takes the port's
advisory lock, since a second reader would take bytes from it: that stops a second `real` source and
a program that asks for the lock, and no other. A port that fails during the run fails the run at its
next turn, or at the end of the run if there is none. It declares:

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
16 ms by default, and a CH9102, as measured on a T-A7670G at 9600 baud, until it has a 64-byte USB
packet or the line goes quiet, so a byte can arrive 67 ms after it left the device.

**Backlog.** A wire carries no more than its line rate, and emulated time does not move while a
turn is held, so the bytes a device sends during a held turn, or while the guest is behind, cannot
all reach the guest when they should. Kept, they would queue on the line (rule 5) and stay there:
a device sending at its full line rate through a 10 s hold leaves its bytes about 10 s behind for
the rest of the run, whatever the clock does, and an answer to a new command waits behind them. The
source therefore keeps its line at most a bound behind: 50 ms, or as long as the line takes to carry
128 bytes if that is longer, 133 ms at 9600 baud. It works out where its line will be free as
the runner paces it, `line_free(pin)` of rule 5, from its own messages, which are the only ones on
its RX pin. At each turn at `now` it sends no more of what the port gave than the line carries by
`now` plus the bound, the newest bytes, and drops the older ones, counting them for the test report.
The guest then hears what the device says now, an answer included, at most the bound after its stamp.
The cut falls wherever the bound does, so it can split a sentence or a frame, as bytes lost on a wire
do: the bound limits how late the guest hears the device, not what of it arrives whole.
128 bytes are two packets of a full-speed USB adapter. An adapter that hands over 64 bytes at once
needs room for one on a line still carrying the last, so while the guest keeps up with wall time no
byte is dropped. A device that delivers faster than the wire's line rate, a receiver with native USB
handing over its epoch at once for one, does lose bytes to the bound, in every burst the line cannot
carry within it. The bound is what the test report's `dropped` counts, from the run's first turn to
its last.
What the device sent before that is no loss of the run's: the source keeps the last of it the bound
carries, for the first turn to hand over, and discards the rest without counting it.

**Transmitted bytes.** The source queues what the firmware transmits as soon as the link hands it
over, in or between turns, and a thread of its own writes the queue to the port, so a turn never
waits for the wire. A `uart_tx` reaches the harness at the latest in the turn after its `t`, within
an emulated millisecond, and from there the port's UART puts the bytes on the wire at its baud,
behind what it has not sent yet. Under `--realtime` the bytes of a firmware that transmits no faster
than the line leave at their `uart_tx` time to within about a millisecond. One that transmits faster
gets ahead of the wire: esp32sim's UART reads as idle whatever has been written to it
(`esp-periph/src/uart.rs:2`), so nothing holds the firmware back as a real FIFO would, and a burst
leaves over as long as the line takes to carry it, later than its stamps. The port's rate is fixed
for the run. A `uart_tx` whose `baud` is `null`, or further from the port's than the 3 %
`matches_baud` allows, would give the device what a wire does not carry intact, which the port
cannot reproduce: the run fails on it, as it does on a `frm_err` (5.8). A port that takes no byte for
5 wall seconds fails the run as a failed port does.
What it has not sent when the run ends, in the queue or in its driver, is not sent.

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
([replay](features/replay-user.md#what-a-replay-refuses)), and must be given after the `real` source,
which puts the backend on that clock; the `real` source refuses a replay already given, on a route
of its own, which was checked against the emulated clock. Recordings on the wall clock are never
compared with emulated ones, and tests with a `real` source stay out of the determinism job
([testing](guides/testing.md#determinism)). A bridge from a simulated network to a real service
([network](features/network-user.md#bridge-a-port-to-a-real-service)) puts the run on the wall
clock in the same way, under the same rules; its stream's bytes go to the guest in `eth_rx` or
`uart_rx` stamped from the turn that hands them over, and its network asks for a turn at the next
whole emulated millisecond while a bridged connection is open.

The run stays on the test's clock: it is held while the test body runs, and `board.wait(d)` lets
it go for `d` of the board's time, which under `--realtime` is also about `d` of wall time. The
test's own timeout, 600 wall seconds by default
([pytest plugin](features/pytest-plugin-user.md#the-tests-timeout)), then bounds the board's time
too.

`tests/test_real_source.py` tests this without hardware: a pseudo-terminal pair stands in for the
USB adapter, with a thread on one end writing the device's bytes in wall time. In CI that checks the
stamping rule on the link, exactly, and a paced run's stamps against the bytes' arrivals with a margin
for the host's scheduler, wide enough that it does not hold the millisecond itself,
the wait at the block point, a turn held before the first 2¹⁶ bus cycles (after which the runner
still waits at the block points), and a turn held for more than 500 ms against a device sending at
its full line rate, with the bytes dropped counted and an answer sent after the hold reaching the
guest within the bound. A GNSS receiver on a USB serial adapter validated the whole (plan M5, with what it covered); its
recording replaces none of the generated ones, which stay the deterministic reference.

On the `serial` backend `real(port)` is not attempted (finding 11). The `real` its capability
declaration gives is the device wired to the board, which no host port reaches, so the test skips at
the call and the port stays closed: `serial: uart.gnss has the device wired to the board at its far
end: a host serial port goes there on the emulator only`.

## Virtual station lifecycle (version 2)

With `--wifi-sta`, the runner emits a sequenced event at the slice boundary when
`VirtualSta.state == StaLink::Joined` changes, or its completed-join counter changes:

```json
{"type":"wifi_link","seq":14,"t":15300000000,"ready":true}
```

`ready: false` invalidates the host's lease, DHCP exchange, ARP state, queued packets and
connections. `ready: true` starts a fresh DHCP exchange at `t`, with a new transaction ID
and initial retry interval. The initial disconnected state is also reported. The event
precedes Ethernet events collected at the same boundary. All times are emulated nanoseconds.
WPA2 Joined means message 3 passed MIC validation and keys are installed; the queued message 4
is sent before relay data by esp32sim's `StationLink::next_rx`.

On transitions the runner clears pending Ethernet inputs, its backpressure queue and both
emulator relay queues (`set_relay(false)` then `set_relay(true)`, without stepping the guest).
The harness clears unsent Ethernet answers. Ethernet input while disconnected is dropped.
Reset additionally invalidates the host immediately; the preserved host object keeps transaction
IDs advancing across resets. Version 1 peers are rejected by the existing handshake.
