# pytest plugin

esp32tl ships a pytest plugin that runs an ESP-IDF firmware image, lets a test read what the
firmware prints, replays a recording into the firmware's GNSS UART, and puts an SD card holding
an image file in the board's slot. A test asks for the
`board` fixture, names the app with the `app_path` marker, sets up what is around the board,
powers it on, and waits on the board's own clock for what the firmware does. It runs once on
every backend. pytest loads the plugin by itself once esp32tl is installed; it builds on
[pytest-embedded](https://github.com/espressif/pytest-embedded), which esp32tl installs with it.

## Writing a test

```python
import pytest

pytestmark = pytest.mark.app_path("../firmware/hello_world")


def test_hello_world(board, console):
    board.power_on()
    console.expect("Hello world!")
```

This is `tests/test_hello_world.py`. `pytestmark` puts the marker on every test of the file; it
can also decorate one test or a class, and the closest one wins.

A test with a GNSS recording, from `tests/test_nmea_reader.py`:

```python
def test_reports_a_fix(board, gnss, console):
    gnss.replay("../recordings/gnss-generated-1.jsonl")
    board.power_on()
    console.expect_exact("fix acquired lat=52.229677 lon=21.012233", timeout=30)
```

The body first sets up what is at the far end of the board's wires, here a replay, then calls
`board.power_on()`, which starts the run. From there the firmware runs only while the test waits:
in `board.wait`, `board.wait_until` or a console expect. The `timeout=30` is 30 seconds of the
board's time, not of the wall clock.

- The path is relative to the directory of the test file. Without the marker, the app is that
  directory itself.
- The backend runs the build in the app's `build/` directory, the layout `idf.py build` leaves:
  every file `flasher_args.json` lists is flashed at its offset. For this repository's firmware,
  `make firmware` builds it ([validation firmware](validation-firmware-user.md)).
- The marker takes exactly one path. `@pytest.mark.app_path` with none, or with more than one
  argument or a keyword argument, is a collection error that names the test:
  `test_bad.py::test_bare: the app_path marker takes one path, as in @pytest.mark.app_path("../firmware/hello_world")`.
  The error is the whole module's, and like any collection error it stops the session before any
  test runs (`Interrupted: 1 error during collection`, exit code 2), unless pytest runs with
  `--continue-on-collection-errors`.

What the backend needs besides the build, the runner and the ROM ELF, is in
[the esp32sim backend](esp32sim-backend-user.md#what-it-needs).

## The fixtures

`board` is the board, off until the test powers it on:

- `board.power_on(utc=None)` starts the run: the runner starts, the firmware boots, and the
  board's clock starts at 0, where it stays until the test waits. `utc`, a `datetime` with a
  timezone, is the world's time at power-on, `2026-01-01T12:00:00Z` unless given; nothing reads it
  yet but `board.utc`. A second call raises `BoardOff: the board is on already: power it on once`.
- `board.wait(seconds)` lets the board run for that many seconds of its own time, and returns at
  exactly that instant: `board.wait(2.5)` moves `board.now` by 2.5.
- `board.wait_until(condition, within=seconds)` lets the board run until `condition()`, a
  function of no arguments, returns something true, for at most `within` seconds of the board's
  time, and returns what it returned. The condition is asked at once, then at every turn of the
  link: at least every 100 ms of the board's time, and at every other turn the link takes, such
  as the one at which a replayed sentence goes in. It runs in the test's thread while the board stands still, so it can
  read anything the test can. When `within` runs out it raises `NeverHappened`, an
  `AssertionError`:
  `the condition test_x.<locals>.<lambda> did not happen within 2 s: waited from 10 s to 12 s of the board's time`.
- `board.now` is the board's time since power-on, in seconds, as a `float`; `board.utc` the
  world's time, the UTC instant at power-on plus `board.now`.
- Waiting, or reading `board.now` or `board.utc`, before `board.power_on()` raises
  `BoardOff: the board is off; call board.power_on() first`.
- When the run ends while the test waits, because the runner exited or the harness found the link
  wrong, the wait raises `RunEnded`, which says when, as far as the harness knows, and why
  ([when the run ends](#what-a-failure-looks-like)).
- `board.app` is the `IdfApp` (`pytest_embedded_idf.app.IdfApp`) the runner flashes, with its
  `target`, `flash_files` and `flash_settings`.
- `board.profile` is the board profile the test runs on: `esp32-devkitc`, unless the
  `board_profile` marker names another ([board profiles](board-profiles-user.md#choosing-a-profile)).
  A test that only reads it needs no `power_on()`, and no runner starts for it.
- `board.console_output` is every byte the firmware has printed on its console so far.
- `board.dut` is pytest-embedded's `Dut`, the same object as its `dut` fixture; its log,
  `dut.log`, still gets the runner's output. Do not call `dut.expect`: it waits in wall time while
  the board stands still, and times out.
- `dut.app`, from pytest-embedded's `dut` or `app` fixture, is pytest-embedded's base `App`,
  without `flash_files` or `target`: the backend runs no pytest-embedded service, so
  pytest-embedded builds nothing more. Use `board.app`.

Durations are plain numbers of seconds of the board's time, `int` or `float`, 0 or more; anything
else raises `TypeError` or `ValueError`, as in
`-1 is not a number of seconds of the board's time, 0 or more`.

`console` is the firmware's console, its UART0, read on the board's clock:

- `console.expect(pattern, timeout=30, *, return_what_before_match=False)` lets the board run until
  `pattern` appears on the console after the last match, for at most `timeout` seconds of the
  board's time, and returns the `re.Match`, on bytes. `pattern` is a regular expression, as a
  string, bytes or a compiled pattern, or a list of them, which matches whichever appears first.
  A string or bytes compiles with `re.DOTALL`, as pexpect compiles it, so `.` matches across
  lines, and `^` matches where the last match ended. A compiled pattern keeps its own flags,
  where pexpect drops them from a compiled string pattern.
  With `return_what_before_match=True` it returns the bytes between the last match and this one.
  These are pytest-embedded's names, its search from the end of the last match and its return
  values.
- `console.expect_exact(pattern, ...)` is the same with strings or bytes found as they are, not as
  regular expressions.
- `console.expect(pexpect.EOF)` waits for the end of the run and returns what the console printed
  after the last match, without trailing whitespace; `pexpect.EOF` may also be one pattern of a
  list.
- A pattern that does not appear within `timeout` raises `NeverHappened`:
  `'fix' on the console did not happen within 30 s: waited from 0 s to 30 s of the board's time`.
  A run that ends first raises `RunEnded`.
- An expect before `board.power_on()` raises `BoardOff`.
- pytest-embedded's `expect_all` and `not_matching` are not there; nor is `console.write`, as the
  runner does not read input for UART0.

`gnss` is the far end of the board profile's `uart.gnss` route:

- `gnss.replay(path)` replays a recording into it, at the recording's times, from power-on. It
  must come before `board.power_on()`. The path is relative to the test file's directory. What it
  does, and what it refuses, is in [replay](replay-user.md).
- Until the test gives it a source, the route is on the link, listening and silent. So is every
  other UART route of the profile.
- On a board profile without `uart.gnss`, `gnss` is an error at setup:
  `esp32sim: the board profile lilygo-t-a7670g-r2 has no uart.gnss; its interfaces are console, uart.modem, lines.modem`.

`sd_card` is the board profile's SD card slot, empty until the test inserts a card. From
`tests/test_sdspi.py`, with `fat_image` and `read` its helpers around mtools:

```python
def test_the_example_writes_its_files_into_the_image(tmp_path, board, sd_card, console):
    image = fat_image(tmp_path / "card.img", {"foo.txt": b"from the test\n"})
    sd_card.insert(image)
    board.power_on()
    console.expect_exact("example: Card unmounted", timeout=5)
    assert read(image, "foo.txt") == b"Hello SIMSD!\n"
```

- `sd_card.insert(image)` puts a card holding the image file in the slot, before
  `board.power_on()`, and returns the image's absolute path. The path is relative to the test
  file's directory. The image is a whole number of 512 KiB units; what is in it is the test's,
  usually a FAT file system made with mtools (`mformat`, `mcopy`).
- On `esp32sim` the card is the runner's model of an SD card in SPI mode, on the slot's pins
  ([runner](runner-user.md#put-an-sd-card-in)). It writes what the firmware writes into the file
  as it goes, so the test reads the image back with mtools (`mtype`, `mcopy`, `mdir`) once the
  firmware is done, during the run or after it. Copy the image first to keep it as it was.
- Without `insert`, the slot is empty, as a board with no card in it: ESP-IDF's driver times out
  (`ESP_ERR_TIMEOUT`).
- `insert` refuses, with `SourceError`: a call after `board.power_on()`
  (`sd_card: the board is on already: give an interface its source before board.power_on()`), a
  second call (`sd_card has a card already: one per test`), an image that is not there
  (`sd_card: no image at PATH: No such file or directory`) or not a file
  (`sd_card: PATH is not a file`), and one of another size
  (`sd_card: PATH has N bytes; an SD card image is a whole number of 512 KiB units`).
- On a board profile without a slot, `sd_card` is an error at setup:
  `esp32sim: the board profile lilygo-t-a7670g-r2 has no sd_card; its interfaces are console, uart.modem, lines.modem`.
- The card stays in from power-on to the end of the run: inserting or removing it while the
  firmware runs is not there yet.

`NeverHappened`, `RunEnded`, `BoardOff` and `SourceError` are in `esp32tl.interfaces`.

## The test's timeout

The board's time and the wall clock are apart: the board stands still while the test body runs,
and the runner waits for it for as long as that takes. What ends a test whose run hangs, in the
harness or in the emulator, is its timeout in wall time, from
[pytest-timeout](https://github.com/pytest-dev/pytest-timeout), which esp32tl installs with it.

Every test that uses `board` gets 600 s (`DEFAULT_TIMEOUT` in `esp32tl/pytest_plugin.py`) unless
the session or the test sets a timeout: `--timeout`, the `PYTEST_TIMEOUT` environment variable,
the `timeout` ini option, or a `@pytest.mark.timeout(...)` marker. A timeout of 0 set that way
turns the limit off, and is kept. A test that runs out fails with
`Failed: Timeout (>600.0s) from pytest-timeout.`, and its teardown ends the runner. pytest-timeout
turns itself off while a debugger such as pdb runs, unless `--timeout-disable-debugger-detection`
is given. With `-p no:timeout` there is no timeout.

The limits a test states, `within=` and `timeout=`, are part of what it tests, on the board's
clock; the wall-time timeout says only that something is stuck.

## Backends and test ids

A test that uses `board`, or `console`, runs once per backend, with the backend in its id:
`test_hello_world[esp32sim]`. `esp32sim`, the runner on the emulated clock, is the only backend
([esp32sim backend](esp32sim-backend-user.md)). Tests that use neither fixture are not
parametrised.

`--esp32tl-backend NAME` runs the tests that use `board` on that backend only; the default is
every backend. A name that is not a backend is a usage error (exit code 4):
`--esp32tl-backend: invalid choice: 'qemu'`.

What is at the far end of an interface is what the test does with it before power-on, as
`gnss.replay(path)`; a backend that cannot give that source skips the test at the call, with the
reason, and a source that no backend gives is an error there. The `tap` marker says which
interfaces a test taps, as in `@pytest.mark.tap("console")`; on a backend that cannot tap them the
test is skipped before any fixture is set up, and a tap that no backend can give is a collection
error. Both are described in
[board profiles](board-profiles-user.md#saying-what-a-test-needs).

## Options esp32tl refuses

esp32tl chooses the backend, the app and the build of every test that uses `board`, so when at
least one selected test uses it, these options stop the run before any test, with exit code 4 and
one line:

| Option | Message |
|---|---|
| `--embedded-services`, `--app-path`, `--build-dir` | ``ERROR: --{option} is set: esp32tl chooses it for every test that uses `board` (the backend through --esp32tl-backend, the app through the app_path marker, the build in its build/ directory)`` |
| `--count` with any value but 1 | `ERROR: --count is set: esp32tl runs one board in a test` |
| An `app_path`, `board_profile` or `tap` marker in a `pytest.param(..., marks=...)`, or added by a hook | `ERROR: {test id}: the {marker} marker reached the test after esp32tl read it, from a pytest.param or a hook: put it on the test, its class or its module` |

A test is selected when `-k` and `-m` leave it in the run: `-k test_two --app-path elsewhere`
passes when `test_two` does not use `board`. In that case the options go to pytest-embedded as
usual.

A test that parametrises pytest-embedded's `count` itself above 1, as in
`@pytest.mark.parametrize("count", [2], indirect=True)`, gets an error at setup:
`count is above 1: esp32tl runs one board in a test`.

## What a failure looks like

- The backend cannot give a source the test asks for, or a tap the `tap` marker asks for: the test
  is skipped on it ([board profiles](board-profiles-user.md#when-a-backend-cannot-give-it)). No run
  skips a test this way yet: `esp32sim`, the only backend tests run on, gives every source and tap
  that any backend declares, and `serial`, which would skip, is declared but not run.
- The backend cannot start (no runner, no ROM ELF, no usable build): an error at setup, the test
  body does not run, and the message starts with `esp32sim: `. The messages are listed in
  [the esp32sim backend](esp32sim-backend-user.md#when-the-backend-cannot-start).
- A source given after `board.power_on()`, or a second one on the same route or slot: the test
  fails with `SourceError`; a recording that cannot be replayed fails it with `RecordingError` or
  `ReplayError` ([replay](replay-user.md#what-a-replay-refuses)).
- A test that set up a source and passed without calling `board.power_on()` gets an error at
  teardown, since it ran no firmware:
  `esp32sim: the test set up the replay of uart.gnss and never called board.power_on(): it ran no firmware`;
  for a card, `the test set up a card in sd_card and never called board.power_on()`.
- A wait or an expect whose condition does not hold in its time: `NeverHappened`.
- The run ends while the test waits: `RunEnded`, which names the board's time it ended at (from
  the runner's statistics; without them, `after the turn at` the last turn the test saw, or
  `before its first turn`), what
  the test waited for, the runner's exit code and its last output, and first what the harness found
  wrong with the link, if anything
  ([when the runner exits](esp32sim-backend-user.md#when-the-runner-exits)):
  `RunEnded: the run ended before its first turn while the test waited for 'Hello world!' on the console: the runner exited with code 2; its last output:`
  followed by the output.
- The runner exited with an error, had to be killed, or the harness found the link to it wrong:
  an error at teardown, even when every expect passed
  ([esp32sim backend](esp32sim-backend-user.md#when-the-run-fails)). A test that failed on
  `RunEnded` has said so already, and gets no second error.
- The test runs out of its wall-time timeout: `Failed: Timeout (>600.0s) from pytest-timeout.`
  ([the test's timeout](#the-tests-timeout)).

Every test that uses `board` also reports what the run told: a section `esp32sim link`, which
pytest prints with a failure or with `-rA`, and the `esp32sim_link` user property, a JSON string
that `--junitxml` writes into the report
([the link's report](esp32sim-backend-user.md#the-links-report)).

pytest-embedded's `app_path` fixture records an XML attribute for every test, which pytest reports
as `PytestExperimentalApiWarning: record_xml_attribute is an experimental feature`. This
repository filters it in `pyproject.toml`; a project of your own can do the same with
`filterwarnings = ["ignore:record_xml_attribute is an experimental feature:pytest.PytestExperimentalApiWarning"]`
under `[tool.pytest.ini_options]`.

## Names the plugin takes

- **`board`, `console`, `gnss`, `sd_card`**: the plugin's fixtures; a project's own fixture of one of these
  names overrides it, as pytest has it.
- **`board`**: esp32tl parametrises every test that asks for a fixture of that name, matched by
  name. A project's own `board` fixture, in a session where esp32tl is installed, gets the
  backend's id (`test_mine[esp32sim]`) and the plugin's parameters as well.
- **The `app_path`, `board_profile` and `tap` markers**: esp32tl registers them and reads them on
  every test that uses `board`. A project's own marker of one of these names means esp32tl's
  there.
- **The `timeout` marker**: pytest-timeout's; esp32tl adds it to a test that uses `board` and has
  none, unless the session sets a timeout.
- **`app_path` and `embedded_services`**: esp32tl parametrises them for a test that uses `board`.
  A test that also parametrises one of them itself is a collection error:
  `duplicate parametrization of 'app_path'`.

## See also

- [pytest plugin: how it works](pytest-plugin-technical.md)
- [esp32sim backend](esp32sim-backend-user.md)
- [Board profiles](board-profiles-user.md)
- [Replay](replay-user.md)
- [Validation firmware](validation-firmware-user.md)
- [Testing this repository](../guides/testing.md)
