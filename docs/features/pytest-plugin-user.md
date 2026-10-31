# pytest plugin

esp32tl ships a pytest plugin that runs an ESP-IDF firmware image and lets a test read what the
firmware prints. A test asks for the `console` fixture (or `board`, which `console` needs), names
the app with the `app_path` marker, and runs once on every backend. pytest loads the plugin by
itself once esp32tl is installed; it builds on
[pytest-embedded](https://github.com/espressif/pytest-embedded), which esp32tl installs with it.

## Writing a test

```python
import pytest

pytestmark = pytest.mark.app_path("../firmware/hello_world")


def test_hello_world(console):
    console.expect("Hello world!")
```

This is `tests/test_hello_world.py`. `pytestmark` puts the marker on every test of the file; it
can also decorate one test or a class, and the closest one wins.

- The path is relative to the directory of the test file. Without the marker, the app is that
  directory itself.
- The backend runs the build in the app's `build/` directory, the layout `idf.py build` leaves:
  every file `flasher_args.json` lists is flashed at its offset. For this repository's firmware,
  `make firmware` builds it ([validation firmware](validation-firmware-user.md)).
- The marker takes exactly one path. `@pytest.mark.app_path` with none, or with more than one
  argument or a keyword argument, is a collection error (exit code 2) that names the test:
  `test_bad.py::test_bare: the app_path marker takes one path, as in @pytest.mark.app_path("../firmware/hello_world")`.

What the backend needs besides the build, the runner and the ROM ELF, is in
[the esp32sim backend](esp32sim-backend-user.md#what-it-needs).

## The fixtures

`console` is the firmware's console, its UART0:

- `console.expect(pattern, **kwargs)` is pytest-embedded's `dut.expect`, and
  `console.expect_exact(pattern, **kwargs)` its `dut.expect_exact`: the same arguments
  (`timeout`, `expect_all`, `not_matching`, `return_what_before_match`), the same return values,
  and the same 30 s default timeout. `expect` takes a string, a compiled regex or a list of them,
  and searches for them as regular expressions; `expect_exact` matches strings as they are.
- An expect that does not match fails with pexpect's `TIMEOUT`, or with its `EOF` once the runner
  has exited, and pytest-embedded's message `Not found "<pattern>"`.
- `console.expect(pexpect.EOF)` waits for the end of the run, within its timeout, and returns
  once the runner has exited.
- There is no `console.write`: the runner does not read its stdin.

`board` is the running backend:

- `board.app` is the `IdfApp` (`pytest_embedded_idf.app.IdfApp`) the runner flashes, with its
  `target`, `flash_files` and `flash_settings`.
- `board.dut` is pytest-embedded's `Dut`, the same object as its `dut` fixture.
- `dut.app`, from pytest-embedded's `dut` or `app` fixture, is pytest-embedded's base `App`,
  without `flash_files` or `target`: the backend runs no pytest-embedded service, so
  pytest-embedded builds nothing more. Use `board.app`.

## Backends and test ids

A test that uses `board`, or `console`, runs once per backend, with the backend in its id:
`test_hello_world[esp32sim]`. `esp32sim`, the runner on the emulated clock, is the only backend
([esp32sim backend](esp32sim-backend-user.md)). Tests that use neither fixture are not
parametrised.

`--esp32tl-backend NAME` runs the tests that use `board` on that backend only; the default is
every backend. A name that is not a backend is a usage error (exit code 4):
`--esp32tl-backend: invalid choice: 'qemu'`.

## Options esp32tl refuses

esp32tl chooses the backend, the app and the build of every test that uses `board`, so when at
least one selected test uses it, these options stop the run before any test, with exit code 4 and
one line:

| Option | Message |
|---|---|
| `--embedded-services`, `--app-path`, `--build-dir` | ``ERROR: --{option} is set: esp32tl chooses it for every test that uses `board` (the backend through --esp32tl-backend, the app through the app_path marker, the build in its build/ directory)`` |
| `--count` with any value but 1 | `ERROR: --count is set: esp32tl runs one board in a test` |

A test is selected when `-k` and `-m` leave it in the run: `-k test_two --app-path elsewhere`
passes when `test_two` does not use `board`. In that case the options go to pytest-embedded as
usual.

A test that parametrises pytest-embedded's `count` itself above 1, as in
`@pytest.mark.parametrize("count", [2], indirect=True)`, gets an error at setup:
`count is above 1: esp32tl runs one board in a test`.

## What a failure looks like

- The backend cannot start (no runner, no ROM ELF, no usable build): an error at setup, the test
  body does not run, and the message starts with `esp32sim: `. The messages are listed in
  [the esp32sim backend](esp32sim-backend-user.md#when-the-backend-cannot-start).
- An expect that does not match in its timeout: the test fails with pexpect's `TIMEOUT`.
- The runner has exited: the expect fails at once with pexpect's `EOF`, which names the exit code
  and carries the runner's last output
  ([when the runner exits](esp32sim-backend-user.md#when-the-runner-exits)).
- The runner exited with an error: an error at teardown, even when every expect passed
  ([esp32sim backend](esp32sim-backend-user.md#when-the-runner-exits-with-an-error)).

pytest-embedded's `app_path` fixture records an XML attribute for every test, which pytest reports
as `PytestExperimentalApiWarning: record_xml_attribute is an experimental feature`. This
repository filters it in `pyproject.toml`; a project of your own can do the same with
`filterwarnings = ["ignore:record_xml_attribute is an experimental feature:pytest.PytestExperimentalApiWarning"]`
under `[tool.pytest.ini_options]`.

## See also

- [pytest plugin: how it works](pytest-plugin-technical.md)
- [esp32sim backend](esp32sim-backend-user.md)
- [Validation firmware](validation-firmware-user.md)
- [Testing this repository](../guides/testing.md)
