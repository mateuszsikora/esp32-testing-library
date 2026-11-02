# esp32-testing-library
Test unmodified ESP32 firmware through its wires: an emulated or real board, with modelled, replayed or real peripherals

esp32tl is a pytest plugin. Today it runs an unmodified ESP-IDF image for the classic ESP32 in the
[esp32sim](https://github.com/mikroverk/esp32sim) emulator, through the runner crate in `runner/`,
replays a recorded GNSS stream into its UART on the emulated clock, drives and reads its GPIO lines,
puts an SD card holding an image file on its SPI pins, and lets a test read the firmware's
console. The `esp32tl` command does the same outside pytest, but for the SD card. Beside the
board, a Bluetooth Classic peer with the Serial Port Profile runs on Bumble's virtual link and a
model in the test answers it; no firmware talks to it yet, as esp32sim has no Classic controller.

## Getting started

From a clean checkout, one command sets everything up and runs the tests:

```sh
make test
```

It needs Python 3.11 or newer, Rust through [rustup](https://rustup.rs), network access to GitHub,
mtools for the SD card tests and, for the first run, Docker:
[the development guide](https://github.com/mateuszsikora/esp32-testing-library/blob/main/docs/guides/development.md#requirements)
says what each is for.

## Writing a test

```python
import pytest

pytestmark = pytest.mark.app_path("../firmware/hello_world")


def test_hello_world(board, console):
    board.power_on()
    console.expect("Hello world!")
```

The test runs once on the selected backend, esp32sim by default, as
`test_hello_world[esp32sim]`. On the emulator the firmware runs only while the test waits on it,
here in `console.expect`, whose timeout is on the board's own clock: a `Duration`
or a number of seconds.
A test that replays a recording into the board's GNSS UART gives it before power-on:

```python
@pytest.mark.app_path("../firmware/nmea_reader")
def test_reports_a_fix(board, gnss, console):
    gnss.replay("../recordings/gnss-generated-1.jsonl")
    board.power_on()
    console.expect_exact("fix acquired lat=52.229677 lon=21.012233")
```

[The pytest plugin's documentation](https://github.com/mateuszsikora/esp32-testing-library/blob/main/docs/features/pytest-plugin-user.md)
describes the fixtures, the `app_path` marker and the options.

## Documentation

[`docs/README.md`](https://github.com/mateuszsikora/esp32-testing-library/blob/main/docs/README.md)
says what lives where: the features, the guides for working on the repository, CI, the link
protocol, the decisions and the
[implementation plan](https://github.com/mateuszsikora/esp32-testing-library/blob/main/docs/implementation-plan.md).
Contributions follow
[`CONTRIBUTING.md`](https://github.com/mateuszsikora/esp32-testing-library/blob/main/CONTRIBUTING.md);
a change that alters behaviour updates its documentation in the same pull request.
