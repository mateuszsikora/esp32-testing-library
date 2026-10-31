# esp32-testing-library
Test unmodified ESP32 firmware through its wires: an emulated or real board, with modelled, replayed or real peripherals

esp32tl is a pytest plugin. Today it runs an unmodified ESP-IDF image for the classic ESP32 in the
[esp32sim](https://github.com/mikroverk/esp32sim) emulator, through the runner crate in `runner/`,
and lets a test read the firmware's console.

## Getting started

From a clean checkout, one command sets everything up and runs the tests:

```sh
make test
```

It needs Python 3.11 or newer, Rust through [rustup](https://rustup.rs), network access to GitHub
and, for the first run, Docker:
[the development guide](https://github.com/mateuszsikora/esp32-testing-library/blob/main/docs/guides/development.md#requirements)
says what each is for.

## Writing a test

```python
import pytest

pytestmark = pytest.mark.app_path("../firmware/hello_world")


def test_hello_world(console):
    console.expect("Hello world!")
```

The test runs once per backend, as `test_hello_world[esp32sim]`.
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
