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

`lilygo-t-a7670g-r2` holds only what the specification gives, from firmware built for the board:
the A7670G modem's UART and its PWRKEY and RI lines. The pull-down on RI is what stage 0 found
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
  report starts with its name ([esp32sim backend](esp32sim-backend-user.md)). A route is wired to
  the board only when a `source` marker names it ([below](#saying-what-a-test-needs)); otherwise
  the link watches no pins. The profile's lines are never wired yet.
- Only the profiles esp32tl ships can be named; there is no marker argument for a file of your
  own.
- The marker goes on the test, its class or its module. One given in a
  `pytest.param(..., marks=...)`, or added by a hook such as a conftest's
  `pytest_collection_modifyitems`, reaches the test after esp32tl has checked it, so it stops the
  run before any test, with exit code 4:
  `ERROR: test_x.py::test_p[esp32sim-2]: the board_profile marker reached the test after esp32tl read it, from a pytest.param or a hook: put it on the test, its class or its module`.
  The same holds for `app_path`, `source` and `tap`.

## Saying what a test needs

Two markers say what a test does with its board's interfaces. An interface is `console`, or the
name of a route or a set of lines in the profile: `uart.gnss`, `uart.modem`, `lines.modem`.

```python
@pytest.mark.source("uart.gnss", "replay")  # a recording at the far end of the GNSS route
@pytest.mark.tap("console", "uart.gnss")  # both are recorded
def test_reports_a_fix(board, console): ...
```

- `source(interface, source)`: what is at the far end of the interface: `model`, `replay` or
  `real`. For each interface the closest marker wins, so a test's own `source` overrides its
  class's and its module's, and a subclass's overrides its base's; two on the same test, class or
  module for one interface are an error.
- `tap(*interfaces)`: the interfaces the test records. Taps add up: a module's and a test's both
  count.

The markers decide whether the test runs on a backend. On `esp32sim`, a `replay` source also
wires the route to the board and holds the run at power-on until the test body gives the
recording, as `gnss.replay(path)` does ([replay](replay-user.md)); a `model` or `real` source is an
error at setup there, since the backend does not give them yet. No fixture taps an interface in
this version.

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
backend first; for the test above, on `serial`:

```
SKIPPED [1] test_gnss.py:4: serial: uart.gnss takes no replay source on this backend, only real
```

| Reason, after `<backend>: ` | When |
|---|---|
| `INTERFACE takes no SOURCE source on this backend, only real` | The backend declares other sources for the interface; the list is its own |
| `INTERFACE takes no SOURCE source on this backend` | The backend declares no source for the interface |
| `INTERFACE cannot be tapped on this backend` | The backend cannot tap the interface |

With several requests the backend cannot give, the reason names one of them, a `source` marker's
before a `tap` marker's.

The skip comes before any fixture: the backend never starts, and a real board's serial port is
never opened.

## When no backend can give it

A request that no backend can give, or that names an interface the profile does not have, is a
mistake in the test, not a reason to skip it everywhere. It is a collection error that names the
test, and like any collection error it stops the session before any test runs (exit code 2):

| Message, after `<test id>: ` | When |
|---|---|
| `the board_profile marker takes one name, as in @pytest.mark.board_profile("esp32-devkitc")` | The marker has no argument, more than one, a keyword argument, or a name that is not a string |
| `no board profile named 'NAME'; the profiles are esp32-devkitc, lilygo-t-a7670g-r2` | The marker names a profile esp32tl does not ship |
| `the source marker takes an interface and a source, as in @pytest.mark.source("uart.gnss", "replay")` | A `source` marker without exactly two strings |
| `two source markers on NAME name INTERFACE` | Two `source` markers on the same test, class or module (`NAME` is its name, as `test_one` or `test_x.py`) name the same interface |
| `the tap marker takes one or more interfaces, as in @pytest.mark.tap("console")` | A `tap` marker with no interface, a keyword argument, or one that is not a string |
| `the board profile PROFILE has no INTERFACE; its interfaces are console, uart.gnss` | The interface is not in the profile (the list is the profile's own) |
| `no backend takes a 'SOURCE' source on INTERFACE; the sources there are model, replay, real` | No backend declares that source for the interface |
| `no backend takes a source on INTERFACE` | No backend declares any source for it: `console`, `lines.*` |
| `no backend can tap INTERFACE` | No backend can tap it: `lines.*` |

A test that does not use `board` (or `console`) is not checked: the markers mean nothing to it.

## See also

- [Board profiles: how they work](board-profiles-technical.md): the file format, the loader, the
  declaration.
- [pytest plugin](pytest-plugin-user.md): the `board` fixture and the other markers.
- [Plan section 4.5](../implementation-plan.md#45-board-profiles).
