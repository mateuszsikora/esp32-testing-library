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
| `lilygo-t-a7670g-r2` | `esp32` (ESP32-WROVER-E) | 4 MB, 4 MB | `console`; `uart.modem`: UART1, TX GPIO 26, RX GPIO 27, 115200 baud; `uart.gnss`: UART2, TX GPIO 21, RX GPIO 22, 9600 baud; `lines.modem`: `pwrkey` GPIO 4, `ri` GPIO 33 pulled down |

`esp32-devkitc` is a generic ESP32 module on a devkit, and carries every validation firmware: its
GNSS route is chosen for this repository's own firmware. GPIO 16 and 17 are left free because on
WROVER modules they belong to the PSRAM. GPIO 5 is a strapping pin; a receiver holding it low at
boot does not change the boot mode. Its SD card slot has the pins ESP-IDF's
`storage/sd_card/sdspi` example takes on the ESP32 by default, so the example runs unmodified;
GPIO 2 and 15 are strapping pins too, which a card leaves alone until the firmware selects it.

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
([esp32sim backend](esp32sim-backend-user.md#how-a-run-goes)). Its other pins are not in it.

The profiles are `esp32tl/boards/<name>.toml`, in the installed package.

## Modem power timing

The LilyGO PWRKEY entry carries `active = 1`, `on_ms = 50`, `off_ms = 2500`. These are model
acceptance thresholds at the ESP32 pad, distinct from firmware's nominal delays. The
[modem guide](modem-user.md#pwrkey-and-reporting) explains their sources and limits. Plain integer
line entries and untimed `{ pin, pull }` tables continue to work for other lines.

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
  test inserts a card. The profile's lines are never wired yet.
- Only the profiles esp32tl ships can be named; there is no marker argument for a file of your
  own.
- The marker goes on the test, its class or its module. One given in a
  `pytest.param(..., marks=...)`, or added by a hook such as a conftest's
  `pytest_collection_modifyitems`, reaches the test after esp32tl has checked it, so it stops the
  run before any test, with exit code 4:
  `ERROR: test_x.py::test_p[esp32sim-2]: the board_profile marker reached the test after esp32tl read it, from a pytest.param or a hook: put it on the test, its class or its module`.
  The same holds for `app_path`.

## Saying what a test needs

A test says what is at the far end of an interface, and whether it records what crosses it, by
what it does with it before `board.power_on()`. An interface is `console`, or the name of a route
or a set of lines in the profile: `uart.gnss`, `uart.modem`, `lines.modem`, or `sd_card`, the SD
card slot.

```python
def test_reports_a_fix(board, gnss, console, tmp_path):
    gnss.replay("../recordings/gnss-generated-1.jsonl")  # a recording at the far end
    gnss.tap(tmp_path / "gnss.jsonl")  # what crosses uart.gnss, recorded
    board.power_on()
    ...
```

- The source of an interface is the call that gives it: `gnss.replay(path)` is a `replay`, and
  `gnss.fix(at=...)` or another call of the receiver model a `model`
  ([models and units](models-user.md#the-gnss-receiver)), and `sd_card.insert(image)` a `model`,
  the runner's card holding the image; `real` gets its call later.
- A tap is the call that asks for it: `gnss.tap(path)` taps `uart.gnss`
  ([taps and the run report](run-report-user.md)). No call taps the console: what it prints is in
  `dut.log` and in the report already. Nothing taps the SD card slot: the image is the record.
- Asked for, a source or a tap a backend does not declare skips the test at the call
  ([below](#when-a-backend-cannot-give-it)).

Each backend declares what it can give:

| Interface | `esp32sim` | `serial` (a real board) |
|---|---|---|
| `console` | tap | tap |
| `uart.*` | `model`, `replay` or `real`; tap | `real` only; no tap |
| `lines.*` | nothing | nothing |
| `sd_card` | `model`: a card holding an image file; no tap | `real`: the card in the slot; no tap |

On a real board the on-board peripherals are soldered on, so only the real part answers, and a tap
on them would need a hardware sniffer. The `lines` fixture is no source in this table: on
`esp32sim` the test drives a line itself, through `lines.drive` and `lines.release`, without
asking the declaration, and nothing skips it. The `serial` backend is declared, but is not one of the
backends tests run on yet ([the pytest plugin](pytest-plugin-user.md#backends-and-test-ids)).

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
| `the board_profile marker takes one name, as in @pytest.mark.board_profile("esp32-devkitc")` | The marker has no argument, more than one, a keyword argument, or a name that is not a string |
| `no board profile named 'NAME'; the profiles are esp32-devkitc, lilygo-t-a7670g-r2` | The marker names a profile esp32tl does not ship |
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
