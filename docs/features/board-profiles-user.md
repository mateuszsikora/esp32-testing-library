# Board profiles

A board profile says which chip a board carries and which UART and pins each of its interfaces
uses, its SD card slot among them: data, not code, and nothing about what the firmware does with them. Every test that uses
`board` runs on one profile, `esp32-devkitc` unless the test names another. Beside the profiles,
esp32tl declares what each backend can put at the far end of an interface, and which interfaces
it can tap; a test that asks for something its backend cannot give is skipped on that backend,
with the reason.

## The profiles esp32tl ships

| Profile | Chip | Flash, PSRAM | Interfaces |
|---|---|---|---|
| `esp32-devkitc` | `esp32` | 4 MB, none | `console`; `uart.gnss`: UART1, TX GPIO 4, RX GPIO 5, 9600 baud; `sd_card`: SCLK GPIO 14, MOSI GPIO 15, MISO GPIO 2, CS GPIO 13 |
| `gps-alarm-clock` | `esp32` | 4 MB, none | `console`; `uart.gnss`: UART1, TX GPIO4, RX GPIO5, 9600 baud; `lines.alarm`: buzzer GPIO18 |
| `sd-data-logger` | `esp32` | 4 MB, none | `console`; `uart.gnss`: UART1, TX GPIO4, RX GPIO5, 9600 baud; `sd_card`: SCLK GPIO14, MOSI GPIO15, MISO GPIO2, CS GPIO13; `lines.logger`: error GPIO18 |
| `esp32-s3-devkitc` | `esp32s3` (ESP32-S3-DevKitC-1-N8R8) | 8 MB, 8 MB | `console` (UART0, GPIO 43 and 44); `uart.gnss`: UART1, TX GPIO 4, RX GPIO 5, 9600 baud; `lines.echo`: in GPIO 6 (pulled down), out GPIO 7 |
| `esp32-c3-devkitm` | `esp32c3` (ESP32-C3-DevKitM-1) | 4 MB, none | `console` (UART0, GPIO 21 and 20); `uart.gnss`: UART1, TX GPIO 4, RX GPIO 5, 9600 baud; `lines.echo`: in GPIO 6 (pulled down), out GPIO 7 |
| `esp32-c6-devkitc` | `esp32c6` (ESP32-C6-DevKitC-1) | 8 MB, none | `console` (UART0, GPIO 16 and 17); `uart.gnss`: UART1, TX GPIO 4, RX GPIO 5, 9600 baud; `lines.echo`: in GPIO 6 (pulled down), out GPIO 7 |
| `lilygo-t-a7670g-r2` | `esp32` (ESP32-WROVER-E) | 4 MB, 4 MB | `console`; `uart.modem`: UART1, TX GPIO 26, RX GPIO 27, 115200 baud; `uart.gnss`: UART2, TX GPIO 21, RX GPIO 22, 9600 baud; `sd_card`: SCLK GPIO 14, MOSI GPIO 15, MISO GPIO 2, CS GPIO 13; `lines.modem`, `lines.gnss`, `lines.board` ([control lines](#lilygo-control-lines-and-simulation-limits)) |

The three devkits of the ESP32-S3, ESP32-C3 and ESP32-C6 carry the validation images built for
their chip. Their `flash_mb` is the module's, 8 MB on the S3's and the C6's, and the esp32sim
backend gives the chip that flash ([the flash a run gets](#the-flash-a-run-gets)). On these chips esp32sim does not
model everything it does on the classic ESP32: a test that asks for an SD card or a virtual
station there is skipped with the reason
([esp32sim backend](esp32sim-backend-user.md#chips-other-than-the-classic-esp32)).

Every profile also has `wifi`, the chip's Wi-Fi station, and `bluetooth`, its Bluetooth
controller: every chip a profile may name has both radios, so a profile does not list them, and they come
last in a profile's interfaces, in that order
([the wifi fixture](pytest-plugin-user.md#the-wifi-fixture),
[`board.bluetooth`](pytest-plugin-user.md#the-bt-fixture)).

Everything a profile lists is on the board's supply: `board.power_off()` takes the power of the
chip, the card in its slot, and the GNSS receiver and modem models at the far end of its routes
together, and `board.reset()` that of none of them, only the chip's EN pin. A replay or a real
device at the far end of a route stands for a part the test does not model, and keeps sending
whatever the board's supply does
([resets and power cycles](pytest-plugin-user.md#resets-and-power-cycles)). On the LilyGO board
the modem, the L76K receiver and the slot are on the board; on a devkit assembly the receiver
and the card are wired to the devkit's 3V3. What is beyond the board, the broker, the access
point and the network behind them, is not. A profile cannot yet put a part on a supply of its
own, nor does `lines.board.power_enable` gate one
([decision](../decisions/harness.md#every-interface-of-a-profile-is-on-the-boards-supply)).

`esp32-devkitc` is a generic ESP32 module on a devkit, and carries every validation firmware: its
GNSS route is chosen for this repository's own firmware. GPIO 16 and 17 are left free because on
WROVER modules they belong to the PSRAM. GPIO 5 is a strapping pin; a receiver holding it low at
boot does not change the boot mode. Its SD card slot has the pins ESP-IDF's
`storage/sd_card/sdspi` example takes on the ESP32 by default, so the example runs unmodified;
GPIO 2 and 15 are strapping pins too, which a card leaves alone until the firmware selects it.

`gps-alarm-clock` describes the devkit, receiver and active buzzer assembly of the
[GPS alarm example](examples-user.md#gps-alarm-clock). It is separate from the generic devkit
profile because the buzzer is an added peripheral.

`sd-data-logger` describes the [SD logger example](examples-user.md#sd-data-logger): the generic
devkit's GNSS and SPI SD wiring plus GPIO18, high while storage is unavailable. Tests observe
it with `lines.output("logger.error")`.

The profiles are `esp32tl/boards/<name>.toml`, in the installed package.

`lilygo-t-a7670g-r2` holds the A7670G modem's UART and its PWRKEY and RI lines, which the
specification gives. The pull-down on RI is what stage 0 found
([gap 6](../stage0-findings.md#gaps)): with no modem fitted the line must read low, and on
`esp32sim` it does, from power-on and whenever the test releases it, in every test on the profile
([the `lines` fixture](pytest-plugin-user.md#the-fixtures)). Its GNSS route, flash and PSRAM come
from LilyGO's
[pin table for the T-A7670X ESP32](https://github.com/Xinyuan-LilyGO/LilyGO-T-A76XX/blob/63af963038e93c91baf39d89a3a736d6bc26ff13/docs/en/esp32/a7670-esp32/README.MD)
and its
[`ExternalGPS_A7670G_Only`](https://github.com/Xinyuan-LilyGO/LilyGO-T-A76XX/blob/63af963038e93c91baf39d89a3a736d6bc26ff13/examples/ExternalGPS_A7670G_Only/ExternalGPS_A7670G_Only.ino)
example: the L76K receiver of the board's GPS version, an external module, on UART2 at 9600 baud,
and 4 MB of flash. The profile describes that version: on the one without GPS, LilyGO's pin table
leaves GPIO 21 and 22 free, the default I²C pins, and the profile still routes `uart.gnss` there. The ESP32-WROVER-E carries 8 MB of PSRAM; the profile gives 4, the most
`esp32sim` models, so that firmware that turns PSRAM on finds it rather than logging
`PSRAM not found`. The esp32sim backend gives the chip the profile's PSRAM
([esp32sim backend](esp32sim-backend-user.md#how-a-run-goes)). Firmware requiring all 8 MB
cannot be validated with this profile's emulated memory.

The SPI SD slot uses SCLK GPIO 14, MOSI GPIO 15, MISO GPIO 2 and CS GPIO 13, confirmed by the
same pin table and example. It can hold an image alongside both UART routes:

```python
import pytest


@pytest.mark.board_profile("lilygo-t-a7670g-r2")
def test_with_card(board):
    board.sd_card.insert("card.img")  # existing writable image, relative to this test file
    board.gnss.lose_fix()  # select the GNSS model, initially without a fix
    board.modem.register()
    board.power_on()
    # Observe your firmware through board.console and the other interfaces.
```

The slot starts empty; inserting an image enables the runner's SPI card model. See
[SD card images](pytest-plugin-user.md#the-fixtures) for preparing and inspecting an image.

### LilyGO control lines and simulation limits

These names are accepted by the `lines` fixture, for example `lines.level("modem.reset")`
observes the firmware's output on GPIO 5. Of the new entries, only `modem.reset` declares a pulse
timing, and none a pull.

| Line | GPIO | Role | Effect in esp32sim |
|---|---|---|---|
| `modem.pwrkey` | 4 | Modem power key | With the modem model selected, firmware pulses change modem power state using the thresholds below |
| `modem.ri` | 33 | Modem ring indication | Board pull-down; the selected modem model drives RI and reserves it against test drives |
| `modem.dtr` | 25 | Modem DTR | Observational only; no modem sleep/wake effect |
| `modem.reset` | 5 | Modem reset | With the modem model selected, a firmware pulse restarts a running modem, using the threshold below; [the modem guide](modem-user.md#a-hung-modem-and-reset) says what it does |
| `gnss.wake` | 19 | L76K wake/standby control | Observational only; no gating of GNSS messages or fix state |
| `gnss.pps` | 23 | L76K pulse-per-second output | Named GPIO only; no model-generated PPS pulses |
| `board.power_enable` | 12 | Peripheral supply enable | Observational only; no gating of the modem, GNSS or SD slot; a recovery that pulses it changes nothing on the modem |

Observational lines still support `lines.drive`, `lines.release`, `lines.level` and `lines.events`
through the generic GPIO interface. Tests can supply PPS transitions explicitly; naming the pin
does not create a pulse generator. The GNSS and modem models and an inserted SD image operate
without firmware asserting these additional controls. This cannot validate the real board's
power-up or sleep sequence, or its reset sequence beyond the pulse the model checks.

`board.power_enable` (GPIO 12), `modem.reset` (GPIO 5), the slot's MISO (GPIO 2) and MOSI
(GPIO 15) are strapping pins. esp32sim reads the strapping from a fixed value, not from the pins'
levels, so a test that drives GPIO 12 or 5 before `board.power_on()` boots normally. On the board,
GPIO 12 high at reset selects 1.8 V for the 3.3 V flash and the boot fails; a `lines.drive` of
GPIO 12 or GPIO 5, both ESP32 outputs, before power-on does not reproduce the board.

LilyGO's sources describe the ESP32 T-A7670X family and the A7670G with an external L76K;
they do not explicitly map the profile's `R2` suffix to a PCB revision. The linked schematic is
V1.4. GPIO assignments agree, but reset polarity, timing and power sequencing across revisions
are not inferred from them. The [technical audit](board-profiles-technical.md#lilygo-wiring-audit)
records the sources and discrepancies.

## Modem power timing

The LilyGO PWRKEY entry carries `active = 1`, `on_ms = 50`, `off_ms = 2500`. These are model
acceptance thresholds at the ESP32 pad, distinct from firmware's nominal delays. The
[modem guide](modem-user.md#pwrkey-and-reporting) explains their sources and limits. The RESET
entry carries `active = 1`, `pulse_ms = 2000`: a pulse of at least that length, judged at its
release ([sources](modem-user.md#a-hung-modem-and-reset)). Plain integer line entries and untimed
`{ pin, pull }` tables continue to work for other lines.

## Choosing a profile

```python
import pytest

pytestmark = pytest.mark.board_profile("lilygo-t-a7670g-r2")


def test_modem_route(board):
    assert board.profile.uart["modem"].baud == 115200
```

- Without the marker, the profile is `esp32-devkitc`. Like `app_path`, the marker can decorate a
  test, a class or a module (`pytestmark`), and the closest one wins.
- `board.profile` is the loaded profile ([its fields](board-profiles-technical.md#interfaces)).
  The esp32sim backend names it, with its chip, in the link's `hello`, and the `esp32sim link`
  report starts with its name ([esp32sim backend](esp32sim-backend-user.md)). Every UART route of
  the profile is wired to the board, and the link watches its pins; a route says nothing until the
  test gives it a source ([below](#saying-what-a-test-needs)). The SD card slot is empty until the
  test inserts a card, before power-on or while the firmware runs. The link also watches the profile's named GPIO lines; declaring a line
  does not give it a peripheral model.
- A positional name selects a bundled profile. To select a project-local TOML file, use
  `@pytest.mark.board_profile(path="boards/test-board.toml")`; `path` accepts a string or
  `pathlib.Path`. Give exactly one name or `path=`, never both.
- The marker goes on the test, its class or its module. One given in a
  `pytest.param(..., marks=...)`, or added by a hook such as a conftest's
  `pytest_collection_modifyitems`, reaches the test after esp32tl has checked it, so it stops the
  run before any test, with exit code 4:
  `ERROR: test_x.py::test_p[esp32sim-2]: the board_profile marker reached the test after esp32tl read it, from a pytest.param or a hook: put it on the test, its class or its module`.
  The same holds for `app_path`.

### A project-local profile

For `tests/test_device.py`, create `tests/boards/test-board.toml`:

```toml
name = "test-board"
chip = "esp32"

[uart.gnss]
port = 1
tx = 4
rx = 5
baud = 9600
```

Select it in `tests/test_device.py`:

```python
import pytest

pytestmark = pytest.mark.board_profile(path="boards/test-board.toml")


def test_wiring(board):
    assert board.profile.name == "test-board"
    assert board.profile.uart["gnss"].tx == 4
```

### The flash a run gets

On the esp32sim backend the chip's flash is the profile's `flash_mb`, which the runner takes as
1, 2, 4, 8 or 16 MB. A profile without `flash_mb` gets the size the build's image headers give,
the one ESP-IDF's `CONFIG_ESPTOOLPY_FLASHSIZE` or PlatformIO's `board_upload.flash_size` sets, and
a build whose headers give none gets the runner's default, 4 MB. The flash of `board.flash()` is
that size too ([flash contents](flash-contents-user.md#read-the-flash-of-a-run)).

A build for more flash than that does not start: its application stops at start-up, as a real
board with a smaller flash chip does, and reboots again and again. The backend refuses such a
run when the test sets up its board, before anything runs:

```text
.../bootloader.bin: the image header of the bootloader at 0x0 gives 8 MB of flash, more than the
4 MB of board profile 'my-board' (flash_mb = 4): the application would stop at start-up; set
flash_mb = 8 in the profile, or build for 4 MB of flash
```

Set `flash_mb` to the flash your board has. A build for less flash than the profile gives runs,
as it does on the board. A `flash_mb` the runner does not have, such as 0 or 32, is refused at
setup too, with the sizes it has. The serial backend flashes the build's headers as they are and
reads nothing from `flash_mb`.

### A console on USB-Serial/JTAG

Many ESP32-S3 and ESP32-C3 boards have no USB-to-UART bridge: the firmware's console is the chip's
own USB-Serial/JTAG controller, an ESP-IDF firmware's with `CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y`,
an Arduino-ESP32 3.x firmware's `Serial` with `ARDUINO_USB_CDC_ON_BOOT=1` and
`ARDUINO_USB_MODE=1`. On UART0 the console would hold only the ROM's banner, and what the test
writes would go where the firmware does not read. One key says so:

```toml
name = "my-s3-board"
chip = "esp32s3"
console = "usb-serial-jtag"
```

`console` is `"uart0"` by default, and `"usb-serial-jtag"` on the `esp32s3`, `esp32c3` and
`esp32c6`. On the classic ESP32, which has no such controller, the profile does not load:
`my-board.toml: console: the ESP32 has no USB-Serial/JTAG controller; use uart0`. Every profile
esp32tl ships keeps `uart0`: their devkits have a bridge. To move a test from UART0 to
USB-Serial/JTAG, add the line to the board's profile, a project-local one or a copy of a shipped
one, and build the firmware with its console on USB; the test's body does not change:
`board.console`, `console.expect`, `console.sendline`, `console.tap` and `board.console_output`
are the USB console's then ([esp32sim backend](esp32sim-backend-user.md#a-console-on-usb-serialjtag)).

With the console on USB-Serial/JTAG, UART0 and its pins are free: a route may take port 0, and a
route or a line UART0's pins (GPIO 43 and 44 on the S3, 21 and 20 on the C3, 16 and 17 on the C6).
The controller's own USB D- and D+ pads are taken instead (GPIO 19 and 20 on the S3, 18 and 19
on the C3, 12 and 13 on the C6): firmware that drives one of them as a UART or a GPIO takes it
from the controller, and the console goes with it. The ROM prints its banner on UART0 too, so a
route there shows that copy:

```toml
[uart.uart0]
port = 0
tx = 43
rx = 44
baud = 115200
```

On the serial backend a profile with this console runs on an ESP32-S3 only
([serial backend](serial-backend-user.md#a-board-whose-console-is-usb-serialjtag)).

### What a route's baud does

`baud` is the line rate of the far end of the route: what the far end sends goes into the ESP32's
RX pin at it, whatever the far end is. What the firmware sends is heard whatever its rate, except
by a real device:

| At the far end | What it sends goes in at | The firmware transmits at another rate |
|---|---|---|
| Nothing | It sends nothing | Heard, and answered by nothing |
| A model: esp32tl's GNSS receiver or modem, or the test's own ([a UART's own model](pytest-plugin-user.md#a-uarts-own-model)) | The profile's `baud` | Heard: a model is not told the rate, and gets the bytes as they are |
| A replay ([replay](replay-user.md)) | The profile's `baud`: a recording carries no baud | Heard, and matched against the recording |
| A real device ([real source](real-source-user.md)) | The profile's `baud`: the host port runs at it | Fails the run at that byte: `uart.gnss.transmitted raised SourceError: uart.gnss: the firmware transmits at {baud} baud, and the serial port {port} runs at {rate} baud` |

A firmware that receives at another rate than the profile's, more than 3 % off, loses what the far
end sends as frame errors, and the run fails at the first of them, whatever the far end is:
`the link failed: uart.aux sends at 115200 baud on GPIO 27, and the firmware's UART1 receives
there at 9600 baud: 6 bytes dropped as frame errors (frm_err)`
([protocol 5.8](../protocol.md#58-warn-ext)). So the profile's `baud` must be the rate the
firmware sets in the test: a firmware that takes its rate from NVS, and a test that writes another
one there with `board.nvs(...)`, needs a profile with that rate, one local profile per rate it
tests. A model that answers at the rate the firmware set, whatever it is, is not there yet
([decision](../decisions/harness.md#a-model-answers-at-the-profiles-baud-not-at-the-rate-the-firmware-set)).

Relative paths always start at the **test file's directory**, including markers inherited from
a class or module, or a `pytestmark` that a test module imports from a conftest. They do not start
at the process working directory or the conftest's directory. Absolute paths work too; a shared
conftest can build one from `Path(__file__).parent`. No change to the installed package is needed.

The file uses the same [schema](board-profiles-technical.md#schema) as bundled profiles, including
the requirement that `name` match the filename without `.toml`, UART constraints and GPIO conflict
checks. Files must be UTF-8. An unreadable, missing or invalid file, including invalid UTF-8,
fails collection with the test id, resolved path and loader error. Sources and taps use the same
backend capability checks as bundled profiles.

Each resolved local path is loaded once per pytest session. The validated snapshot is used at
setup even if the file changes or a symlink is retargeted after collection's marker checks;
setup uses the saved profile object without resolving the path again. Edit profiles before
starting the next session. Files with the same `name` in different directories remain separate,
and a local profile never replaces
a bundled profile of that name. A late marker switching between those files is refused even if
their contents match. Reports, the simulator handshake and recordings use the TOML `name`;
recordings also retain the selected route's pins and baud. The local path is not a recording id,
so choose distinct names if recordings need distinguishable board labels.

## Saying what a test needs

A test says what is at the far end of an interface, and whether it records what crosses it, by
what it does with it before `board.power_on()`. An interface is `console`, or the name of a route
or a set of lines in the profile: `uart.gnss`, `uart.modem`, `lines.modem`, `sd_card`, the SD
card slot, `wifi`, the chip's Wi-Fi station, or `bluetooth`, its Bluetooth controller.

```python
def test_reports_a_fix(board, tmp_path):
    board.gnss.replay("../recordings/gnss-generated-1.jsonl")  # a recording at the far end
    board.gnss.tap(tmp_path / "gnss.jsonl")  # what crosses uart.gnss, recorded
    board.power_on()
    ...
```

- The source of an interface is the call that gives it: `gnss.replay(path)` is a `replay`, and
  `gnss.fix(at=...)` or another call of the receiver model a `model`
  ([models and units](models-user.md#the-gnss-receiver)), `sd_card.insert(image)` a `model`,
  the runner's card holding the image, and `wifi.access_point(...)` or `wifi.nearby(...)` a
  `model`, esp32sim's access point with the simulated network behind it; `board.bluetooth` asks for
  a `model` on `bluetooth`, the firmware's controller in the harness on the air of the test's
  devices. `gnss.real(port)` asks for
  a `real` source, a device on a host serial port, which on `serial` skips all the same: the `real`
  declared there is the device wired to the board ([real source](real-source-user.md)).
- A tap is the call that asks for it: `gnss.tap(path)` taps `uart.gnss`
  ([taps and the run report](run-report-user.md)); `console.tap(path)` records the console on
  both backends, in addition to `dut.log` and the run report. Nothing taps the SD card slot:
  the image is the record.
- Asked for, a source or a tap a backend does not declare skips the test at the call
  ([below](#when-a-backend-cannot-give-it)).

Each backend declares what it can give:

| Interface | `esp32sim` | `serial` (a real board) |
|---|---|---|
| `console` | tap | tap |
| `uart.*` | `model`, `replay` or `real`; tap | `real` only; no tap |
| `lines.*` | nothing | nothing |
| `sd_card` | `model`: a card holding an image file; no tap | `real`: the card in the slot; no tap |
| `wifi` | `model`: esp32sim's access point, the network behind it the harness's; no tap | `real`: the access points around the board; no tap |
| `bluetooth` | `model`: the firmware's controller in the harness, with the test's devices on its air; the tap of the HCI between the firmware's host and it (`board.bluetooth.tap`) | nothing: `board.bluetooth` skips |

On a real board the on-board peripherals are soldered on, so only the real part answers, and a tap
on them would need a hardware sniffer. The `lines` fixture is no source in this table: on
`esp32sim` the test drives a line itself, through `lines.drive` and `lines.release`, without
asking the declaration, and nothing skips it. The `serial` backend runs only when explicitly
selected with `--esp32tl-backend serial`
([the pytest plugin](pytest-plugin-user.md#backends-and-test-ids)).

## When a backend cannot give it

The test is skipped on that backend, and runs on the others. `pytest -ra` shows the reason, the
backend first, at the test's line; for a replay on a backend that gives only `real`, such as
`serial`:

```
SKIPPED [1] test_gnss.py:4: serial: uart.gnss takes no replay source on this backend, only real
```

| Reason, after `<backend>: ` | When |
|---|---|
| `INTERFACE takes no SOURCE source on this backend, only real` | The backend declares other sources for the interface; the list is its own |
| `INTERFACE takes no SOURCE source on this backend` | The backend declares no source for the interface |
| `INTERFACE cannot be tapped on this backend` | The backend cannot tap the interface |

A source and a tap are asked for in the test body, so the skip comes at that call, after the
fixtures are set up: on a real board, after its serial port is open.

## When no backend can give it

A request that no backend can give, or that names a profile esp32tl does not ship, is a mistake
in the test, not a reason to skip it everywhere. For the `board_profile` marker it is a collection
error that names the test, and like any collection error it stops the session before any test
runs (exit code 2):

| Message, after `<test id>: ` | When |
|---|---|
| `the board_profile marker takes one bundled name or path= (str or pathlib.Path), as in @pytest.mark.board_profile("esp32-devkitc") or @pytest.mark.board_profile(path="boards/test-board.toml"); do not combine them` | No argument, mixed name/path arguments, extra arguments, a non-string name, or a `path` that is not a string or `Path`, is blank, or is `Path("")` or `"."` (the test file's directory) |
| `no board profile named 'NAME'; the profiles are esp32-c3-devkitm, esp32-c6-devkitc, esp32-devkitc, esp32-s3-devkitc, gps-alarm-clock, lilygo-t-a7670g-r2, sd-data-logger` | The marker names a profile esp32tl does not ship |
| `no board profile named 'NAME'; the profiles are esp32-c3-devkitm, esp32-c6-devkitc, esp32-devkitc, esp32-s3-devkitc, gps-alarm-clock, lilygo-t-a7670g-r2, sd-data-logger; for a local file, use board_profile(path="NAME")` | The positional name contains `/` or ends in `.toml`, so it looks like a local file |
| `<resolved path>: <file>: <loader error>` | A local file is missing, unreadable, invalid TOML or violates the profile schema |
| `cannot resolve board_profile path <path repr>: <error>` | The local path cannot be resolved |

For a source or a tap it is a `SourceError` at the call, which fails the test:

| Message | When |
|---|---|
| `no backend takes a 'SOURCE' source on INTERFACE; the sources there are model, replay, real` | No backend declares that source for the interface |
| `no backend takes a source on INTERFACE` | No backend declares any source for it |
| `no backend can tap INTERFACE` | No backend declares a tap of it |

A test that does not use `board` (or `console`) is not checked: the marker means nothing to it.

## See also

- [Board profiles: how they work](board-profiles-technical.md): the file format, the loader, the
  declaration.
- [pytest plugin](pytest-plugin-user.md): the `board` fixture and the other markers.
- [Plan section 4.5](../implementation-plan.md#45-board-profiles).
