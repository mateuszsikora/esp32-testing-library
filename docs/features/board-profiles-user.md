# Board profiles

A board profile says which chip a board carries and which UART and pins each of its interfaces
uses: data, not code, and nothing about what the firmware does with them. Every test that uses
`board` runs on one profile, `esp32-devkitc` unless the test names another. Beside the profiles,
esp32tl declares what each backend can put at the far end of an interface, and which interfaces
it can tap; a test that asks for something its backend cannot give is skipped on that backend,
with the reason.

## The profiles esp32tl ships

| Profile | Chip | Flash, PSRAM | Interfaces |
|---|---|---|---|
| `esp32-devkitc` | `esp32` | 4 MB, none | `console`; `uart.gnss`: UART1, TX GPIO 4, RX GPIO 5, 9600 baud |
| `lilygo-t-a7670g-r2` | `esp32` (ESP32-WROVER-E) | not in the profile | `console`; `uart.modem`: UART1, TX GPIO 26, RX GPIO 27, 115200 baud; `lines.modem`: `pwrkey` GPIO 4, `ri` GPIO 33 pulled down |

`esp32-devkitc` is a generic ESP32 module on a devkit, and carries every validation firmware: its
GNSS route is chosen for this repository's own firmware. GPIO 16 and 17 are left free because on
WROVER modules they belong to the PSRAM. GPIO 5 is a strapping pin; a receiver holding it low at
boot does not change the boot mode.

`lilygo-t-a7670g-r2` holds only what the specification gives: the A7670G modem's UART and its
PWRKEY and RI lines. The pull-down on RI is what stage 0 found
([gap 6](../stage0-findings.md#gaps)): with no modem fitted the line must read low. Its other pins, its flash and PSRAM sizes, and
whether it has a GNSS route at all are not in it: they wait for the vendor schematic.

The profiles are `esp32tl/boards/<name>.toml`, in the installed package.

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
  test gives it a source ([below](#saying-what-a-test-needs)). The profile's lines are never wired
  yet.
- Only the profiles esp32tl ships can be named; there is no marker argument for a file of your
  own.
- The marker goes on the test, its class or its module. One given in a
  `pytest.param(..., marks=...)`, or added by a hook such as a conftest's
  `pytest_collection_modifyitems`, reaches the test after esp32tl has checked it, so it stops the
  run before any test, with exit code 4:
  `ERROR: test_x.py::test_p[esp32sim-2]: the board_profile marker reached the test after esp32tl read it, from a pytest.param or a hook: put it on the test, its class or its module`.
  The same holds for `app_path` and `tap`.

## Saying what a test needs

A test says what is at the far end of an interface by what it does with it before
`board.power_on()`, and which interfaces it taps with a marker. An interface is `console`, or the
name of a route or a set of lines in the profile: `uart.gnss`, `uart.modem`, `lines.modem`.

```python
@pytest.mark.tap("console", "uart.gnss")  # both are recorded
def test_reports_a_fix(board, gnss, console):
    gnss.replay("../recordings/gnss-generated-1.jsonl")  # a recording at the far end
    board.power_on()
    ...
```

- The source of an interface is the call that gives it: `gnss.replay(path)` is a `replay`, and
  `gnss.fix(at=...)` or another call of the receiver model a `model`
  ([models and units](models-user.md#the-gnss-receiver)); `real` gets its call later. Asked for, a
  source a backend does not declare skips the test at the call ([below](#when-a-backend-cannot-give-it)).
- `tap(*interfaces)`: the interfaces the test records. Taps add up: a module's and a test's both
  count. A tap decides, at collection, whether the test runs on a backend. No fixture taps an
  interface in this version.

Each backend declares what it can give:

| Interface | `esp32sim` | `serial` (a real board) |
|---|---|---|
| `console` | tap | tap |
| `uart.*` | `model`, `replay` or `real`; tap | `real` only; no tap |
| `lines.*` | nothing | nothing |

On a real board the on-board peripherals are soldered on, so only the real part answers, and a tap
on them would need a hardware sniffer. The `serial` backend is declared, but is not one of the
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

A tap the backend cannot give skips the test before any fixture: the backend never starts, and a
real board's serial port is never opened; with several, the reason names the first. A source is
asked for in the test body, so its skip comes at that call, after the fixtures are set up: on a
real board, after its serial port is open.

## When no backend can give it

A request that no backend can give, or that names an interface the profile does not have, is a
mistake in the test, not a reason to skip it everywhere. For a marker it is a collection error
that names the test, and like any collection error it stops the session before any test runs
(exit code 2):

| Message, after `<test id>: ` | When |
|---|---|
| `the board_profile marker takes one name, as in @pytest.mark.board_profile("esp32-devkitc")` | The marker has no argument, more than one, a keyword argument, or a name that is not a string |
| `no board profile named 'NAME'; the profiles are esp32-devkitc, lilygo-t-a7670g-r2` | The marker names a profile esp32tl does not ship |
| `the tap marker takes one or more interfaces, as in @pytest.mark.tap("console")` | A `tap` marker with no interface, a keyword argument, or one that is not a string |
| `the board profile PROFILE has no INTERFACE; its interfaces are console, uart.gnss` | The interface is not in the profile (the list is the profile's own) |
| `no backend can tap INTERFACE` | No backend can tap it: `lines.*` |

For a source it is a `SourceError` at the call, which fails the test:

| Message | When |
|---|---|
| `no backend takes a 'SOURCE' source on INTERFACE; the sources there are model, replay, real` | No backend declares that source for the interface |
| `no backend takes a source on INTERFACE` | No backend declares any source for it |

A test that does not use `board` (or `console`) is not checked: the markers mean nothing to it.

## See also

- [Board profiles: how they work](board-profiles-technical.md): the file format, the loader, the
  declaration.
- [pytest plugin](pytest-plugin-user.md): the `board` fixture and the other markers.
- [Plan section 4.5](../implementation-plan.md#45-board-profiles).
