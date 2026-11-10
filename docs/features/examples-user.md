# Runnable examples

Each directory under `examples/` is a small ESP-IDF project with readable tests in `tests/`.
The tests exercise the compiled firmware through esp32tl, using the same API as an application's
own tests. CI builds and runs every example.

## Build and run

From the repository root, with Python, Rust and Docker installed
([development setup](../guides/development.md)):

```sh
make setup
make examples
.venv/bin/pytest examples
```

`make examples` builds every `examples/<name>/CMakeLists.txt` project in the same pinned
ESP-IDF v5.3.6 container as `make firmware`, into `examples/<name>/build/`. To build and run only
the GPS alarm:

```sh
firmware/build.sh --examples gps_alarm_clock
.venv/bin/pytest examples/gps_alarm_clock/tests
```

`IDF_LOCAL=1 make examples` uses the shell's ESP-IDF for iteration. `FIRMWARE_OUT` overrides the
output root for direct `firmware/build.sh --examples` calls; `make examples` clears it so the
tests find their builds. The builder's provenance and completion markers work just like the
[validation firmware's](validation-firmware-user.md).

`make test` builds missing validation images and examples, then runs both suites. A direct
`.venv/bin/pytest` also collects both, but never builds firmware. Rebuild with `make examples`
after changing example C code; the completion marker only detects a missing or unfinished
build. After changing `sdkconfig.defaults`, remove that example's `build/` directory first so
the saved `sdkconfig` does not retain the old values.

## GPS alarm clock

Read [the firmware](../../examples/gps_alarm_clock/main/main.c) and
[its tests](../../examples/gps_alarm_clock/tests/test_alarm.py). This is a location alarm: it
sounds while a valid GNSS position is inside a configured radius. It has no wall-clock alarm time.

The `gps-alarm-clock` board profile describes this example's assembly:

| Connection | ESP32 side |
|---|---|
| GNSS UART | UART1, TX GPIO4, RX GPIO5, 9600 baud, 8N1 |
| Active buzzer control | GPIO18, high means on |
| Console | UART0, the default ESP-IDF console |

On real hardware use a suitable buzzer driver; the example supplies a steady logic level, not
an audio waveform. The test observes GPIO18 through `buzzer = lines.output("alarm.buzzer")`, with
`buzzer.is_on` and `buzzer.is_off` conditions for `board.wait_until`.

Before `board.power_on()`, write the destination to the default NVS partition with `board.nvs`:

| Namespace | Key | NVS type | Meaning |
|---|---|---|---|
| `alarm` | `lat_e6` | `i32` | Latitude in microdegrees, from -90000000 to 90000000 |
| `alarm` | `lon_e6` | `i32` | Longitude in microdegrees, from -180000000 to 180000000 |
| `alarm` | `radius_m` | `u32` | Positive radius in metres |

The example's `configure_alarm(board, target=TARGET, radius=m(100))` helper accepts a
`Position` and a `Distance`, converts coordinates to the nearest microdegree and requires a
positive whole-metre radius. It writes `i32(52229677)`, `i32(21012233)` and `u32(100)` for a
100 m radius around Warsaw. The helper belongs to this application; the library's `board.nvs`
continues to accept typed NVS values. Each configured test calls the helper explicitly and uses
`board.power_on()`; there is no second board object named `alarm`. There is no default destination. Missing, mistyped or invalid values
leave the buzzer off and print `gps_alarm_clock: invalid or missing NVS alarm configuration`.
A configured image prints `gps_alarm_clock: ready`.

A valid GGA or RMC sentence turns the buzzer on when the great-circle distance is at most the
radius, and off outside it. A GGA/RMC without a fix or with an invalid checksum/position turns
it off. Other sentence types are ignored. With no valid fix for 2.5 s it turns off too; the
UART loop checks this at least every 100 ms. This prevents a silent receiver leaving the alarm on.

The movement tests calculate `time_to_boundary` from distance divided by speed. They check
the old state one fix interval before the crossing, advance to the boundary, then wait for the
new state with `REACTION_TIME = 2 * FIX_INTERVAL` (`FIX_INTERVAL = seconds(1)`). The response
budget starts at the boundary, independent of travel time. `assert_never_buzzed` checks both
the current off state and the output's event history, so short pulses cannot pass unnoticed
from boot. It allows an unknown pin only until the first firmware-driven level, then rejects
both active levels and an undriven output. A failure reports the first violation, for example
`alarm.buzzer expected off after initialization (GPIO 18, level 0); first violation at 2 s: undriven/unknown`.
A pin that never initializes also fails. Once a state is established, tests use
`buzzer.assert_stayed_on(since=...)` or `buzzer.assert_stayed_off(since=...)` to require that
state throughout an interval. In particular, loss of fix must keep the buzzer off for three
seconds after it first turns off. The silence test requires the buzzer to stay on for 2.3 s
after the first observed on state, then turn off within another 0.4 s. This brackets the 2.5 s
fix lifetime with a 0.2 s tolerance for observing the output, completing the NMEA epoch already
in flight, and the firmware's UART polling. It catches both premature and late expiry;
it does not establish an exact 2.5 s deadline from the firmware's internal last-fix timestamp.
`assert_on()` / `assert_off()` check only the current state.
The scenarios come first in the file; the NVS helper and fixture follow them. Separate smaller-
and larger-radius cases show the expected output directly, without conditional assertions.

The tests demonstrate:

- A far-away fix leaves the buzzer off throughout the observation window.
- Moving inward at 10 m/s turns it on within two seconds of crossing the radius; moving out
  turns it off within two seconds of crossing.
- No fix at boot leaves it off; losing a fix or going silent turns an active buzzer off.
- NVS controls both the destination and radius, including southern/western coordinates.
- Missing configuration leaves it off even when the receiver has a fix at the example target.

## SD data logger

Read [the firmware](../../examples/sd_data_logger/main/main.c) and
[its tests](../../examples/sd_data_logger/tests/test_logger.py). It appends one CSV row per valid
RMC fix to an SD card over SPI, with a separate `YYYYMMDD.CSV` file for each GNSS UTC date.

```sh
firmware/build.sh --examples sd_data_logger
.venv/bin/pytest examples/sd_data_logger/tests
```

Install mtools (`brew install mtools` or `sudo apt-get install mtools`) to prepare and read the
test cards. The library's builder requires `mformat`, `mcopy`, `mmd`, `mdir` and `mtype` and
raises `SourceError` naming missing commands. The empty-slot test needs no mtools.

The `sd-data-logger` profile describes the assembly:

| Connection | ESP32 side |
|---|---|
| GNSS UART | UART1, TX GPIO4, RX GPIO5, 9600 baud, 8N1 |
| SD card SPI | SPI2, SCLK GPIO14, MOSI GPIO15, MISO GPIO2, CS GPIO13 |
| Storage error | GPIO18, high means unavailable |
| Console | UART0, the default ESP-IDF console |

Use a 3.3 V SD interface with the required pull-ups. GPIO2, GPIO5 and GPIO15 are strapping
pins; attached peripherals must permit normal boot. GPIO18 can drive a logic input or an LED
through a suitable resistor.

Tests request `board: Board` and use `board.gnss`, `board.lines` and `board.console` directly.
`board.sd_card.image(size_mb=32).with_text(name, contents).build()` prepares a card with an
existing row; `.build()` alone prepares an empty card. Each scenario explicitly calls
`board.sd_card.insert(card)` before
`board.power_on(utc=...)`. On hardware, insert a preformatted FAT card before boot. The firmware
never formats the card. Each row is `hhmmss[.fraction],latitude,longitude`, followed by a newline;
coordinates are signed decimal degrees with six decimal places. There is no header. For example,
`20261102.CSV` can contain:

```text
120001.00,52.229677,21.012233
120002.00,52.229677,21.012233
```

Files are opened in append mode and closed after each row, so existing entries survive and the
test can read completed writes while the board's clock is stopped. The date comes from RMC,
not the ESP32's wall clock. Years `00` through `99` mean 2000 through 2099. GGA is ignored because
it supplies no date; a receiver sending RMC and GGA once a second therefore produces one row per
second. Missing fixes, invalid checksums, impossible dates and malformed times produce no rows.
Loss of fix pauses writing; recovery appends again. Each accepted RMC sentence is a row, including
a repeated RMC; there is no retransmission deduplication.

GPIO18 starts high and goes low only after a successful mount. A missing or unmountable card
leaves it high and prints `sd_data_logger: SD card unavailable (<ESP-IDF error name>)`.
The firmware then prints `sd_data_logger: ready` and continues receiving fixes. Each accepted
fix prints, for example, `sd_data_logger: fix 20261102 120001.00 (saved)`,
`sd_data_logger: fix 20261102 120001.00 (not saved)` when its row could not be written, or
`sd_data_logger: fix 20261102 120001.00 (card unavailable)` without a mounted card. A row that
cannot be written raises GPIO18 and prints `sd_data_logger: SD card write failed` before its fix
line; the next fix tries again, and the first row written after a failure lowers GPIO18 and
prints `sd_data_logger: SD card writes again`. A row that failed is not written again.
Mounting is attempted once at boot; insertion/removal during a run is not supported.

Tests verify existing rows and new coordinates byte for byte, a new file across New Year and
leap-day midnight, no files without a fix, pause/recovery after fix loss, malformed input, and
continued reception with the error pin continuously high after an empty-slot failure. With the
card's fault rules ([pytest plugin](pytest-plugin-user.md#the-fixtures)) they also verify, after a
successful mount and first rows: a failed block write that raises GPIO18, loses its row and
leaves the image readable, and the next fix that writes again and lowers the pin; a failed block
read, which ESP-IDF reports as its read timeout 1 s later, with the same recovery; a write busy
for 300 ms, whose row is saved later with the pin low throughout; and a write busy for 6 s, past
ESP-IDF's 5 s write timeout, which the firmware reports as a failure 5 s after the write on the
board's clock, then writes again once the card is no longer busy.

## Position tracker over a cellular modem

Read [the firmware](../../examples/position_tracker/main/main.c) and
[its tests](../../examples/position_tracker/tests/test_tracker.py). The tracker takes GNSS
positions, dials PPP through `esp_modem`, synchronizes UTC with NTP and publishes JSON over
MQTT with TLS. The tests supply the receiver, network, broker and CA in process; no external
broker, cellular service or host clock is involved.

```sh
firmware/build.sh --examples position_tracker
.venv/bin/pytest examples/position_tracker/tests
```

The `lilygo-t-a7670g-r2` profile supplies the wiring: modem UART1 on TX GPIO26/RX GPIO27 at
115200 baud, GNSS UART2 on TX GPIO21/RX GPIO22 at 9600 baud, both 8N1. The modem must already
be powered and registered; the simulated modem begins in that state. The example uses SIM7600
AT commands compatible with the modeled modem and does not drive PWRKEY.

Provision these required values in the default NVS partition before `board.power_on()`:

| Namespace | Key | NVS type | Meaning |
|---|---|---|---|
| `config` | `broker_host` | string | Broker DNS name, verified against its TLS certificate |
| `config` | `broker_port` | u16 | TLS listener port, e.g. `u16(8883)` |
| `config` | `broker_ca` | string | Trusted CA certificate in PEM format |
| `config` | `ntp_host` | string | NTP server DNS name |
| `config` | `apn` | string | Cellular access point name |

Tests request the public `board: Board`, as in the SD logger, and reach the GNSS receiver and
console through `board.gnss` and `board.console`. The local helper
`network, tls = serve_broker(board)` selects the modem network, creates a CA for `broker.test`
and registers its broker on TLS port 8883. `serve_broker(board, ca=custom_ca)` uses supplied
credentials unchanged. It returns the existing network and TLS listener, without writing NVS,
starting firmware or advancing time. The helper lives in this example's `tests/tracker_support.py`;
it is not an esp32tl API or an implicit fixture.
`configure_tracker(board, trusted_ca=tls.ca.ca_pem)` writes only NVS: the trusted CA,
`broker.test`, port 8883, `time.test` and APN `internet`. Its `broker_host` and `broker_port`
keyword arguments override the destination. This helper also belongs to the example.
Missing or invalid configuration prints `position_tracker: invalid or missing NVS configuration`
and stops before connecting. There are no default credentials. The hostnames are limited to
253 characters, the APN to 63 and the PEM string to 8191 bytes, excluding its terminator.

After PPP and NTP succeed, the tracker connects with client ID `position-tracker`, verifies the
CA, server name and certificate validity dates against NTP time, and immediately publishes to
`tracker/position`. It then publishes every 10 seconds on the firmware's monotonic clock.
Each MQTT reconnect starts with a new immediate report and resets the interval. Reports have QoS 0 and are not retained:

```json
{"fix":true,"lat":52.229677,"lon":21.012233,"time":1935837296}
```

`time` is Unix UTC seconds from NTP, independent of the GNSS sentence's time. `fix` is true
only for a valid position received less than 2.5 seconds ago. With no fix, an invalid GGA/RMC
or a silent receiver, the tracker still reports time, with `"fix":false,"lat":null,"lon":null`.
The first report can precede the receiver's first epoch and then has no fix even when the
model already has a position. It never connects before NTP synchronization or falls back to unverified/plain MQTT. It retries
NTP while printing `position_tracker: waiting for NTP` every 10 seconds of waiting. Modem power
sequencing and PPP redial are outside this example; MQTT reconnect operates while PPP is up.

The local `Reports(network.broker)` adapter interprets publications on `tracker/position`.
Constructing it registers no listener and does not select a network or advance time. Its
conditions return a `Report` with `received_at` (the board's time), `utc` (a timezone-aware
datetime) and `position` (`Position` or `None`):

```python
from esp32tl import Board


def test_losing_the_fix_clears_the_reported_position(board: Board) -> None:
    network, tls = serve_broker(board)
    reports = Reports(network.broker)
    configure_tracker(board, trusted_ca=tls.ca.ca_pem)
    board.gnss.fix(at=WARSAW)
    board.power_on(utc=WORLD)
    first = board.wait_until(reports.when_received(with_position=True), within=seconds(60))
    assert first.position == WARSAW

    board.gnss.lose_fix()
    report = board.wait_until(reports.when_received(), within=seconds(10.25))
    assert report.position is None
```

`reports.when_received()` matches the first report published after the condition is created;
`reports.when_received(with_position=True)` additionally requires a fix. Neither call consumes
a message: each creates a condition, whose repeated polls return the same first match. A position
condition can wait past the initial no-fix report without assuming which numbered message has a position. `reports.history` includes only
tracker reports from the firmware's client ID, `position-tracker`, excluding unrelated topics,
publications made by the test and any other client. The adapter reads them with
`broker.mark()` and `broker.find_messages(..., since=mark, client=...)`
([every message since a point](network-user.md#every-message-since-a-point)).
Malformed reports fail decoding instead of disappearing from a position wait. This application
adapter lives in `tests/tracker_support.py`; `board` remains the only clock and power control.
Timeouts and the run report describe the expected result as `a tracker report` or
`a tracker report with a position`, instead of the adapter's internal function name. Treating the
condition itself as a boolean
(for example `assert reports.when_received()`) raises `TypeError`: call it with `()` to inspect
it or pass it to `board.wait_until()`.

TLS date tests use `CertificateAuthority(..., server_not_before=..., server_not_after=...)`;
the NTP outage test uses `network.ntp.go_silent()`. After the firmware resolves
`time.test`, it observes 30 seconds with no broker lookup, TLS attempt, MQTT connection or
publication. It then calls `ntp.resume()` and requires a report with the GNSS position and UTC
matching the advancing world clock within 60 seconds. These network controls belong to the
library and know nothing about the tracker report format.

The tests show a report within one second of connection, three subsequent 10-second intervals
on `board.now`, position following a moving GNSS model, and timestamps following two different
`power_on(utc=...)` epochs. They also reject the wrong CA and hostname, expired and not-yet-valid
server certificates (with a valid CA). Each rejection case waits on
`tls.handshake_started`, then checks the complete broker history after 30 seconds of simulated time
for any connection or publication, including short-lived ones. It does not match firmware log
wording. Other cases check no-fix, lost-fix, receiver silence, missing
configuration and a nonresponding NTP server. One puts a service of the test's own on the
broker's port, over TLS, that takes each connection and answers nothing: the tracker's MQTT
CONNECT, with its client ID, reaches it, and the tracker closes that connection and opens
another within 30 seconds
([writing a model](../guides/writing-tests.md#writing-a-model)). These tests assert application behavior with
small timing tolerances. The harness's TLS server repeats its bytes, so two runs of a test give
the same times; `make determinism` runs the tracker over TLS and checks that
([testing](../guides/testing.md#determinism)).

## Periodic wake

Read [the firmware](../../examples/periodic_wake/main/main.c) and
[its tests](../../examples/periodic_wake/tests/test_periodic_wake.py). The chip spends its time in
deep sleep and wakes on its RTC timer every 15 minutes. At every boot it counts the wake in RTC
memory, pulses GPIO18 high for 20 ms, prints the count and sleeps again:

```sh
firmware/build.sh --examples periodic_wake
.venv/bin/pytest examples/periodic_wake/tests
```

The board profile is the example's own file,
[`tests/boards/periodic-wake.toml`](../../examples/periodic_wake/tests/boards/periodic-wake.toml),
which the tests select with `board_profile(path="boards/periodic-wake.toml")`, as a project of
its own does ([a project-local profile](board-profiles-user.md#a-project-local-profile)):

| Connection | ESP32 side |
|---|---|
| Wake pulse | GPIO18, high for 20 ms at every boot |
| Console | UART0, the default ESP-IDF console |

GPIO18 can drive a logic input, or an LED through a suitable resistor. Between pulses the pin is
low while the chip is awake and undriven while it sleeps. The tests observe it with
`board.lines.output("wake.pulse")`.

After its pulse the firmware prints one line, `periodic_wake: wakes=N`. `N` is 0 at a power-on or
a reset and goes up by one at every wake from the timer: RTC slow memory keeps the count through
a deep sleep, and loses it with the power. The sleep is a fixed 15 minutes after the pulse
(`esp_sleep_enable_timer_wakeup`, then `esp_deep_sleep_start`), so each wake comes a little over
15 minutes after the one before: by the boot and the pulse, about 42 ms, and on esp32sim by a
sleep about 0.025 % longer than the firmware asks, 0.22 s. The tests hold each interval to 15
minutes and at most one second more (`TOLERANCE = seconds(1)`), and each pulse to 20 ms and at most
1 ms more.

`board.wait_for_wake(within=INTERVAL + TOLERANCE)` runs the board through each sleep to the wake
([waking from deep sleep](pytest-plugin-user.md#waking-from-deep-sleep)); the count on the console
comes within `BOOT_TIME = seconds(0.1)` after it. The tests demonstrate:

- The pin pulses every 15 minutes: four pulses, from power-on and three wakes, and the count from
  0 to 3.
- A day of wakes spends less wall time asleep than booting: 95 wakes, 96 pulses, in under 24 hours
  of the board's time, every interval within the tolerance, and the sleeps take less wall time
  than the boots between them (about 0.4 s against 6.6 s on an M2 Pro).
- The count survives deep sleep, not a reset (`board.reset()` five minutes into a sleep starts it
  at 0 again) nor a power cycle.

## OBD-II reader over Bluetooth Classic

Read [the firmware](../../examples/obd_reader/main/main.c) and
[its tests](../../examples/obd_reader/tests/test_obd_reader.py). The reader is the client of an
ELM327-compatible OBD-II adapter, the kind that plugs into a vehicle's diagnostic port and offers a
serial port over Bluetooth. It searches for a device named `OBDII`, pairs with PIN `1234`, opens
the adapter's SPP channel, talks to it in the adapter's AT and OBD-II commands, and prints what the
vehicle answers:

```sh
firmware/build.sh --examples obd_reader
.venv/bin/pytest examples/obd_reader/tests
```

It has no wiring of its own: the Bluetooth is the chip's, and the console is the default ESP-IDF
console on UART0. The tests run on the default profile, `esp32-devkitc`, and reach the air through
`board.bluetooth` ([the `bt` fixture](bluetooth-peer-user.md#in-front-of-the-firmware-the-bt-fixture)).
The firmware runs its controller for Bluetooth Classic alone (`CONFIG_BTDM_CTRL_MODE_BR_EDR_ONLY`)
with Bluedroid and SPP, and turns Secure Simple Pairing off (`ssp_en = false`), so it always pairs
with the PIN, as Bluetooth 2.0 adapters do. The name and the PIN are constants in `main.c`,
`ADAPTER_NAME` and `ADAPTER_PIN`.

| Step | What the firmware does | What it prints |
|---|---|---|
| Search | An inquiry of 5.12 s. A device whose name, in its extended inquiry response, is `OBDII` ends it; other devices are ignored. A search that found nothing starts the next one at once | `obd_reader: searching for OBDII`, then `obd_reader: found OBDII at 00:00:00:00:5b:01` |
| Connect | An SDP search for the Serial Port service, then the first channel found, with `ESP_SPP_SEC_AUTHENTICATE`, as the central; the PIN when the adapter asks for one | `obd_reader: paired with OBDII`, `obd_reader: connected`; with another PIN, `obd_reader: pairing failed (status 9)` and `obd_reader: could not connect` |
| Opening | `ATZ`, whose answer must start with `ELM327`; `ATE0`; `ATSP0` | `obd_reader: adapter ELM327 v1.5`; otherwise `obd_reader: not an ELM327 adapter: ...` with the answer's first line, and it disconnects |
| Supported PIDs | `0100`. The first line of the answer that is `41 00` with four bytes counts, so the `SEARCHING...` an adapter on protocol 0 sends before its first answer is skipped. An answer with no such line, as `SEARCHING...` and `UNABLE TO CONNECT` with the ignition off, is asked again 2 s later. A vehicle with none of the three readings ends the session | `obd_reader: no answer from the vehicle: UNABLE TO CONNECT`, the answer's last line; then `obd_reader: the vehicle supports rpm speed coolant`, with the names of those it has, or `obd_reader: the vehicle supports none of rpm speed coolant`, and it disconnects |
| Readings | For each of `010C` (rpm), `010D` (speed, km/h) and `0105` (coolant, °C) that the vehicle supports, in that order, one round; then 1 s, then the next round | `obd_reader: rpm=850 speed=0 coolant=90`; `name=?` for an answer it cannot read, as `NO DATA` |

Each command is one line ending in `\r`. The reader reads up to the adapter's prompt, `>`, and
drops empty lines and the echo of its command. An adapter that sends no prompt within 2 s of a
command is disconnected (`obd_reader: the adapter stopped answering`, then
`obd_reader: disconnected`), and so is one whose answer runs past 255 bytes without a prompt
(`obd_reader: the adapter sent an answer too long to read`); the next search starts at once. A
session the adapter closes ends at once, also during one of the reader's pauses: the next search
starts at once, and the session after it starts again from `ATZ`. A failed pairing or connection,
an adapter that offers no serial port, a device that is no ELM327 and a vehicle with none of the
three readings start the next search 5 s after the end, so a wrong PIN does not repeat a pairing
every 0.4 s. A successful pairing leaves Bluedroid a key, and the next connection to the same
adapter authenticates with it instead of the PIN.

The tests put the adapter on the air as an `SppPeer` named `OBDII` with PIN `1234`. Its source
([the model that answers](bluetooth-peer-user.md#the-model-that-answers)) is this example's model
of the adapter, `Elm327` in [`tests/elm327.py`](../../examples/obd_reader/tests/elm327.py), which
answers for a `Vehicle` and keeps every command it hears:

```python
from elm327 import Elm327, Vehicle

from esp32tl import Board
from esp32tl.bluetooth.peer import SppPeer
from esp32tl.units import seconds


def test_a_vehicle_with_speed_only(board: Board) -> None:
    adapter = Elm327(Vehicle(rpm=None, coolant=None, speed=42))
    SppPeer(board.bluetooth.air, name="OBDII", pin="1234", source=adapter)
    board.power_on()
    board.wait_until(board.console.line("obd_reader: speed=42"), within=seconds(5))
    assert adapter.commands()[:5] == ["ATZ", "ATE0", "ATSP0", "0100", "010D"]
```

`Vehicle(ignition=True, rpm=850.0, speed=0, coolant=90)` is what the diagnostic port answers. A
reading of `None` is a PID the vehicle does not support. With `ignition` off, every mode 01
request gets `SEARCHING...` and `UNABLE TO CONNECT`. The test can change it at any time, and the
next answer has the change. `Elm327(vehicle, answer_time=ms(50), reset_time=ms(500),
version="ELM327 v1.5", silent=False, echo=True)` answers:

| Command | Answer |
|---|---|
| `ATZ` | `version`, `reset_time` after the command; echo on again, and the protocol to be searched for |
| `ATI` | `version` |
| `ATE0`, `ATE1` | `OK`; echo off, echo on |
| `ATSP` and one digit | `OK`; the protocol to be searched for |
| `0100` | `41 00` and four bytes, the PIDs 01 to 20 the vehicle supports, PID n at bit 32 − n |
| `010C`, `010D`, `0105` | `41`, the PID and its data, encoded as OBD-II encodes them: rpm × 4 in two bytes, km/h in one, °C + 40 in one; `NO DATA` for a PID the vehicle does not support |
| Any mode 01 request, the first after power-on, `ATZ` or `ATSP` | `SEARCHING...` on a line of its own, then the answer above; with the ignition off, `SEARCHING...` and `UNABLE TO CONNECT` every time, until a request finds the vehicle on |
| Anything else | `?` |

Every answer except `ATZ`'s comes `answer_time` after its command. While echo is on it starts with
the command, then the answer's lines, each ending in `\r`, an empty line and `>`. With `silent`,
the adapter hears every command and answers none. `adapter.requests` lists every command as a
`Request` with `at`, its time on the board's clock, and `command`; `adapter.commands()` lists the
commands alone, in upper case without spaces. The model belongs to this example, as the tracker's
report adapter does; esp32tl itself has no OBD-II model. `tests/test_elm327.py` checks the model
without a board.

The tests demonstrate:

- Without the adapter, the reader keeps searching: four searches 5.12 s apart, to within 0.1 s,
  with a phone on the air that it does not take for the adapter, and no pairing. An adapter put on
  the air during a search is found by the next one, within 5.12 s, and the session opens within
  1 s after that.
- With the adapter, the session opens: one legacy pairing, from the firmware's side, which
  succeeds; the channel open and nothing refused; the adapter's name on the console; and `ATZ`,
  `ATE0`, `ATSP0`, `0100` as the first commands the adapter hears, followed by `010C`: the reader
  took the answer below the first `SEARCHING...`.
- The vehicle's answer decides what the reader asks next, which the test reads at the adapter:
  after `0100`, rounds of `010C`, `010D`, `0105` for a vehicle with all three, `010D` alone for one
  with speed only, and `010C`, `0105` for one without speed. Each round's line has what the vehicle
  answered, also after the test changes the vehicle between two rounds.
- With the ignition off, the reader asks `0100` again every 2 s, to within 0.2 s, and nothing else;
  once the ignition is on, it asks for the readings.
- With another PIN on the adapter, no session opens: each attempt is one legacy pairing that fails
  with `AUTHENTICATION_FAILURE`, the next search starts 5 s after it, to within a 10 ms tick, and
  the adapter hears no command.
- An adapter that stops answering is dropped 2 s after the command it left unanswered, to within a
  tick, and the reader searches again at once. Once the adapter answers again, the reader finds
  it, opens the session with the key of the first pairing, without pairing again, and starts again
  with `ATZ`.
- An adapter that ends its link between two rounds gets a new session: the reader finds it and
  connects again before its 1 s pause is over, and starts the new session from `ATZ`, with the key
  of the first pairing. The test ends the link through the peer's Bumble device,
  `peer.device.connections`, in a coroutine started with `board.bluetooth.air.start(...)`.
- A device that is no ELM327 (`Elm327(version="OK")`) and a vehicle with none of the three readings
  end the session: the reader disconnects, searches again 5 s later, to within a tick, and the
  next session ends the same way.

## Adding an example

Add `examples/<name>/CMakeLists.txt`, `main/`, `sdkconfig.defaults` and `tests/test_*.py`.
Use `app_path("..")` in tests directly under `tests/`, with the board profile that describes
the assembly. Keep assertions on the public fixtures and observed wires. The builder discovers
projects, pytest collects their tests, and CI caches their builds automatically; no image list
needs updating. Update these documents in the same change (ownership rule F24).
