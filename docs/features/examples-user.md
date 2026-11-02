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
fix prints, for example, `sd_data_logger: fix 20261102 120001.00 (saved)` or
`sd_data_logger: fix 20261102 120001.00 (card unavailable)`. A write/close failure raises GPIO18,
prints `sd_data_logger: SD card write failed` and disables further writes until reboot.
Mounting is attempted once at boot; insertion/removal during a run is not supported.

Tests verify existing rows and new coordinates byte for byte, a new file across New Year and
leap-day midnight, no files without a fix, pause/recovery after fix loss, malformed input, and
continued reception with the error pin continuously high after an empty-slot failure.

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

The tests separate their simulated broker from firmware provisioning. The `broker_ca` fixture
creates a per-test CA for `broker.test`; `tls_listener` registers TLS on port 8883
and returns its `Tls` handle. Tests select this setup fixture explicitly. Neither fixture writes
NVS or powers on the board.
`configure_tracker(board, trusted_ca=broker_ca.ca_pem)` writes only NVS: the trusted CA,
`broker.test`, port 8883, `time.test` and APN `internet`. Its `broker_host` and `broker_port`
keyword arguments override the destination. This helper belongs to the example, not the library.
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

The example's `reports` fixture depends only on `broker` and interprets publications on
`tracker/position`; it registers no listener, so custom TLS setups can use it too. Its conditions
return a `Report` with `received_at` (the board's time), `utc` (a timezone-aware datetime) and
`position` (`Position` or `None`):

```python
@pytest.mark.usefixtures("tls_listener")
def test_losing_the_fix_clears_the_reported_position(board, gnss, broker_ca, reports):
    configure_tracker(board, trusted_ca=broker_ca.ca_pem)
    gnss.fix(at=WARSAW)
    board.power_on(utc=WORLD)
    first = board.wait_until(reports.when_received(with_position=True), within=seconds(60))
    assert first.position == WARSAW

    gnss.lose_fix()
    report = board.wait_until(reports.when_received(), within=seconds(10.25))
    assert report.position is None
```

`reports.when_received()` matches the first report published after the condition is created;
`reports.when_received(with_position=True)` additionally requires a fix. Neither call consumes
a message: each creates a condition, whose repeated polls return the same first match. A position
condition can wait past the initial no-fix report without assuming which numbered message has a position. `reports.history` includes only
tracker reports from firmware, excluding unrelated topics and publications made by the test.
Malformed reports fail decoding instead of disappearing from a position wait. This application
adapter lives in `tests/tracker_support.py`; `board` remains the only clock and power control.
Timeouts and the run report describe the expected result as `a tracker report` or
`a tracker report with a position`, instead of the adapter's internal function name.

TLS date tests use `CertificateAuthority(..., server_not_before=..., server_not_after=...)`;
the NTP outage test uses `modem.network().ntp.go_silent()`. After the firmware resolves
`time.test`, it observes 30 seconds with no broker lookup, TLS attempt, MQTT connection or
publication. It then calls `ntp.resume()` and requires a report with the GNSS position and UTC
matching the advancing world clock within 60 seconds. These network controls belong to the
library and know nothing about the tracker report format.

The tests show a report within one second of connection, three subsequent 10-second intervals
on `board.now`, position following a moving GNSS model, and timestamps following two different
`power_on(utc=...)` epochs. They also reject the wrong CA and hostname, expired and not-yet-valid
server certificates (with a valid CA). Each rejection case waits for the TLS listener to receive
handshake input, then checks the complete broker history after 30 seconds of simulated time
for any connection or publication, including short-lived ones. It does not match firmware log
wording. Other cases check no-fix, lost-fix, receiver silence, missing
configuration and a nonresponding NTP server. TLS uses system randomness, so these tests
assert application behavior with small timing tolerances, not identical ciphertext.

## Adding an example

Add `examples/<name>/CMakeLists.txt`, `main/`, `sdkconfig.defaults` and `tests/test_*.py`.
Use `app_path("..")` in tests directly under `tests/`, with the board profile that describes
the assembly. Keep assertions on the public fixtures and observed wires. The builder discovers
projects, pytest collects their tests, and CI caches their builds automatically; no image list
needs updating. Update these documents in the same change (ownership rule F24).
