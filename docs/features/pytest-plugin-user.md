# pytest plugin

esp32tl ships a pytest plugin that runs an ESP-IDF firmware image, lets a test read what the
firmware prints, puts a GNSS receiver on the firmware's GNSS UART, a recording replayed or a
receiver the test describes, records what crosses it, drives and reads the board's GPIO lines,
puts an SD card holding an image file in the board's slot, and puts a Wi-Fi access point with a
simulated network behind it on the chip's air. A test asks for the
`board` fixture, names the app with the `app_path` marker, sets up what is around the board,
powers it on, and waits on the board's own clock for what the firmware does. It runs once on
the selected backend, esp32sim by default. pytest loads the plugin by itself once esp32tl is
installed; it builds on
[pytest-embedded](https://github.com/espressif/pytest-embedded), which esp32tl installs with it.

## Writing a test

```python
import pytest

pytestmark = pytest.mark.app_path("../firmware/hello_world")


def test_hello_world(board):
    board.power_on()
    board.console.expect("Hello world!")
```

This is `tests/test_hello_world.py`. `pytestmark` puts the marker on every test of the file; it
can also decorate one test or a class, and the closest one wins.

A test with a GNSS recording, from `tests/test_nmea_reader.py`:

```python
def test_reports_a_fix(board):
    board.gnss.replay("../recordings/gnss-generated-1.jsonl")
    board.power_on()
    board.console.expect_exact("fix acquired lat=52.229677 lon=21.012233", timeout=30)
```

The body first sets up what is at the far end of the board's wires, here a replay, then calls
`board.power_on()`, which starts the run. From there the firmware runs only while the test waits:
in `board.wait`, `board.wait_until` or a console expect. The `timeout=30` is 30 seconds of the
board's time, not of the wall clock.

A test with a receiver it describes instead, adapted from `tests/test_nmea_reader_model.py`:

```python
from esp32tl.units import NORTH, Position, about, km, km_h, m, minutes, seconds

WARSAW = Position(52.229677, 21.012233)
FIX = r"^fix lat=(?P<lat>\S+) lon=(?P<lon>\S+)$"


def test_follows_the_receiver_as_it_moves(board):
    board.gnss.fix(at=WARSAW)
    board.power_on()
    board.wait(seconds(10))

    board.gnss.move(heading=NORTH, speed=km_h(36))  # 10 m/s
    board.wait(minutes(1) + seconds(40))  # 1 km

    latest = board.wait_until(board.console.line(FIX), within=seconds(2))
    reported = Position(float(latest["lat"]), float(latest["lon"]))
    assert reported.distance_to(WARSAW) == about(km(1), within=m(15))
```

What the receiver can be told, and the units, are in [models and units](models-user.md).

- The path is relative to the directory of the test file. Without the marker, the app is the
  project's `esp32tl_app_path` when it is set ([project defaults](#project-defaults)), else that
  directory itself.
- By default, the backend runs the build in the app's `build/` directory: an ESP-IDF build,
  every file `flasher_args.json` lists flashed at its offset, or a PlatformIO build
  ([builds](builds-user.md)). For this repository's firmware, `make firmware` builds it
  ([validation firmware](validation-firmware-user.md)).
- The marker takes exactly one positional path (`str` or `pathlib.Path`), plus an optional
  `build_dir` keyword of the same types. An empty build string, another type, an unknown keyword
  or the wrong number of positional arguments is a collection error that names the test (the
  check sees text only: `Path("")` is `Path(".")`, so it selects the application directory itself):
  `test_bad.py::test_bare: the app_path marker takes one path, and optionally build_dir as a non-empty path, as in @pytest.mark.app_path("../firmware/hello_world", build_dir="build")`.
  The error is the whole module's, and like any collection error it stops the session before any
  test runs (`Interrupted: 1 error during collection`, exit code 2), unless pytest runs with
  `--continue-on-collection-errors`.

To select an existing build outside the application, use:

```python
@pytest.mark.app_path("../firmware", build_dir="../CI builds/debug")
def test_debug(board):
    board.power_on()
    board.console.expect("Hello world!")
```

`build_dir` is relative to the resolved **application directory**, or absolute. In this example,
with a test in `project/tests/`, it selects `project/CI builds/debug/`, independently of pytest's
working directory. The directory itself must hold an ESP-IDF build, with `flasher_args.json`,
or a PlatformIO build, as `build_dir=".pio/build/esp32"` selects ([builds](builds-user.md)), on
either backend. Paths with spaces
work without special handling in Python. The closest `app_path` marker wins as a whole (test,
then class, then module); omitting `build_dir` on that marker selects its application's `build/`,
without inheriting a more distant marker's keyword.

Both backends use the resolved absolute directory, including serial preflight, flashing and
recording hash checks. A missing or invalid build fails: esp32tl never searches another build,
rebuilds it, moves its artifacts or edits the application's sources. The same target, encryption
and missing-file checks apply. Repository validation firmware still has its separate provenance
checks; this selector does not bypass them or require external builds to carry repository stamps.

pytest-embedded's raw `--build-dir` remains refused for selected `board` tests, even when it names
the same directory. Use the marker in pytest and `esp32tl run --build-dir` or
`esp32tl replay --build-dir` at the CLI. Do not parametrise `build_dir` yourself: esp32tl supplies
it together with `app_path`; a second parametrisation is a collection error. A change to the
marker's keyword after collection has read it is refused like a late `app_path` argument.

### Project defaults

A project whose tests all run one app on one board sets them once, in its pytest configuration,
instead of a `pytestmark` in every file:

```toml
# pyproject.toml, beside firmware/ and tests/
[tool.pytest.ini_options]
esp32tl_app_path = "firmware"
esp32tl_build_dir = ".pio/build/esp32"
esp32tl_board_profile_path = "tests/boards/my-board.toml"
```

| Option | What it sets | Relative to | Without it |
|---|---|---|---|
| `esp32tl_app_path` | The app of a test without an `app_path` marker | The configuration file's directory | The test file's directory |
| `esp32tl_build_dir` | That app's build directory | The app, as `build_dir=` is; or absolute | `build` |
| `esp32tl_board_profile` | The bundled profile of a test without a `board_profile` marker | (a name) | `esp32-devkitc` |
| `esp32tl_board_profile_path` | A local profile file for a test without a `board_profile` marker | The configuration file's directory | `esp32-devkitc` |

The options go wherever pytest reads its configuration: `[tool.pytest.ini_options]` in
`pyproject.toml`, `pytest.ini`, `tox.ini` or `setup.cfg`, or `-o NAME=VALUE` on the command line.
TOML takes the quotes off its strings; an ini file keeps them, so there the values go without
quotes: `esp32tl_app_path = firmware`. The configuration file's directory is pytest's `inipath`'s,
the base pytest uses for its own `paths` options: the paths name the same directories whichever
directory pytest is started from, `pytest`, `pytest tests/` or `pytest /path/to/project/tests`
alike. A value given with `-o` is resolved against the same directory; pytest started with no
configuration file at all resolves it against the invocation directory. Absolute paths are taken as
they are. An empty value is the same as no value.

Each test's selection, closest first:

1. **The app and its build:** the closest `app_path` marker (test, class, module), whose path is
   relative to the test file and whose `build_dir`, without the keyword, is that app's `build/`.
   A marker replaces both project defaults: `esp32tl_build_dir` belongs to `esp32tl_app_path`'s
   app, not to another app a marker names. Then `esp32tl_app_path` with `esp32tl_build_dir`.
   Then the test file's directory with `esp32tl_build_dir`, or with `build`.
2. **The board profile:** the closest `board_profile` marker, a name or `path=` relative to the
   test file. Then `esp32tl_board_profile` or `esp32tl_board_profile_path`. Then
   `esp32-devkitc`.

Each marker keeps only the selection it controls. A test with both markers runs what it ran
before, with or without the options; a test with an `app_path` marker alone takes the project's
profile default, and one with a `board_profile` marker alone the project's app and build. The
options are read once per session, when pytest configures; a profile file is loaded and checked
at collection, as `board_profile(path=...)`'s is, and is a snapshot for the session.

Errors:

- A value in quotes, as `esp32tl_app_path = "firmware"` in `pytest.ini`:
  `ERROR: esp32tl_app_path is "firmware", in quotes: write it without them (esp32tl_app_path = firmware); an ini file keeps the quotes`,
  exit code 4, before collection. The last clause comes only with a value read from an ini file:
  a value still in quotes after TOML took its own off, as `esp32tl_app_path = "'firmware'"`, and
  one given in quotes with `-o`, with or without a configuration file, get the same error
  without it.
- Both profile options set:
  `ERROR: esp32tl_board_profile and esp32tl_board_profile_path are both set: set the bundled name or the file`,
  exit code 4, before collection.
- A default profile that does not load fails collection of each test that reads it, with the
  option named at the end, as
  `tests/test_app.py::test_boots: no board profile named 'boards/my-board.toml'; the profiles are esp32-c3-devkitm, esp32-c6-devkitc, esp32-devkitc, esp32-s3-devkitc, gps-alarm-clock, lilygo-t-a7670g-r2, sd-data-logger; for a local file, set esp32tl_board_profile_path to boards/my-board.toml (from esp32tl_board_profile)`
  or `tests/test_app.py::test_boots: /path/to/project/tests/boards/missing.toml: missing.toml: [Errno 2] No such file or directory: '/path/to/project/tests/boards/missing.toml' (from esp32tl_board_profile_path)`.
  A test with its own `board_profile` marker does not read the default and is not affected.
- A default app or build that does not exist fails at the test's setup, as the marker's does.

pytest-embedded's `--app-path` and `--build-dir` stay refused, also in `addopts`
([options esp32tl refuses](#options-esp32tl-refuses)): they are command-line options relative to
the working directory, with no board profile beside them.

On esp32sim, before `board.power_on()`, `board.nvs({"config": {"broker_host": "broker.test"}})`
can replace a named NVS partition from the build's table, and `merge=True` puts the keys over what
the partition holds instead. After `board.power_off()`, while the board is off, it writes the
runner's flash for the next boot, with `merge=True` over what the firmware left; while the board is
on it raises `BoardOff`. Integer values require a width,
for example `u16(8883)` from `esp32tl`. On serial, `board.nvs(...)` skips without writing
a replacement partition. See [flash contents](flash-contents-user.md).
A test that supplies NVS and passes without calling `board.power_on()` fails teardown:
it ran no firmware.

From `board.power_on()` on, `board.flash()` returns an immutable copy of the flash: a partition by
name, NVS in the shape `board.nvs` takes, and the OTA state. Reading it moves none of the board's
time, works with the board off, and works after `RunEnded`. On serial it skips.
See [read the flash of a run](flash-contents-user.md#read-the-flash-of-a-run).

What the backend needs besides the build, the runner and the ROM ELF, is in
[the esp32sim backend](esp32sim-backend-user.md#what-it-needs).

### One suite on several chips

A firmware built for several chips, one PlatformIO environment or ESP-IDF build directory per
chip, runs its suite once per chip: one pytest session for each pair of a build and a board
profile, with the [project defaults](#project-defaults) overridden by `-o`. esp32tl has no targets
of its own: no option lists the chips, and a test id does not name one
([why](../decisions/harness.md#one-suite-runs-on-several-chips-as-one-pytest-session-per-build-and-profile)).

The PlatformIO project of [writing tests](../guides/writing-tests.md#where-the-tests-go), with
a second environment for an ESP32-S3 and a profile file per chip, `tests/boards/esp32.toml` and
`tests/boards/esp32-s3.toml`:

```ini
; platformio.ini
[env:esp32]
platform = espressif32
board = esp32dev
framework = espidf

[env:esp32-s3]
platform = espressif32
board = esp32-s3-devkitc-1
framework = espidf
```

```toml
# pyproject.toml, beside platformio.ini and tests/
[tool.pytest.ini_options]
addopts = ["-ra"]
testpaths = ["tests"]
esp32tl_app_path = "."
esp32tl_build_dir = ".pio/build/esp32"
esp32tl_board_profile_path = "tests/boards/esp32.toml"
```

In GitHub Actions, a matrix job per chip:

```yaml
jobs:
  test:
    name: test (${{ matrix.name }})
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        include:
          - name: esp32
            env: esp32
            # Relative to the app, esp32tl_app_path, here the project's directory.
            build: .pio/build/esp32
            # Relative to the directory of pyproject.toml, the configuration file.
            profile: tests/boards/esp32.toml
          - name: esp32-s3
            env: esp32-s3
            build: .pio/build/esp32-s3
            profile: tests/boards/esp32-s3.toml
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-python@v7
        with:
          python-version: "3.12"
      # mtools builds the SD card's image, for the tests that use one.
      - run: sudo apt-get install -y mtools
      - run: python -m pip install esp32tl platformio
      - run: esp32tl runtime prepare
      - run: pio run -e ${{ matrix.env }}
      - run: >-
          pytest tests
          -o esp32tl_build_dir=${{ matrix.build }}
          -o esp32tl_board_profile_path=${{ matrix.profile }}
          --junitxml=junit-${{ matrix.name }}.xml
      - uses: actions/upload-artifact@v7
        if: ${{ !cancelled() }}
        with:
          name: junit-${{ matrix.name }}
          path: junit-${{ matrix.name }}.xml
```

The two paths have different bases, as in the configuration file:

- `esp32tl_build_dir` is relative to the app. With `esp32tl_app_path = "."` that is the
  directory of `pyproject.toml`. Without `esp32tl_app_path` the app is each test file's
  directory, and `.pio/build/esp32-s3` names `tests/.pio/build/esp32-s3`, which holds no build.
- `esp32tl_board_profile_path` is relative to the configuration file's directory, whether it comes
  from the file or from `-o`.

Neither is relative to the directory pytest starts in. An absolute path avoids the question:
`-o esp32tl_build_dir=$GITHUB_WORKSPACE/.pio/build/esp32-s3`.

`-o` sets the project defaults, and a marker still wins over them for its test, class or module
([the selection, closest first](#project-defaults)). A test with an `app_path` marker runs the
marker's app and its build, the marker's `build_dir` or that app's `build/`, in every job, whatever
`esp32tl_build_dir` the job sets; one with a `board_profile` marker runs the marker's profile, and
skips what that profile's chip lacks, whatever `esp32tl_board_profile_path` the job sets. A
marker can so select another build or profile than the job names, and with them another chip; a
build and a profile of different chips, one from a marker and one from the job, give the test an
error at setup, as below. Keep both markers out of the tests that follow the matrix.

The board profile's `chip` is the chip esp32sim runs, and the build is checked against it. A
matrix entry whose build and profile name different chips gives each test an error at setup, as
`esp32sim: /path/to/project/.pio/build/esp32-s3 is built for esp32s3; the board's chip is esp32: build for esp32 or choose a board profile with that chip`
([when the build cannot be loaded](builds-user.md#when-the-build-cannot-be-loaded)): a wrong pair
fails rather than being skipped.

A job may take a bundled profile instead of a file, as `esp32-s3-devkitc`. When `pyproject.toml`
sets `esp32tl_board_profile_path`, the job empties it as it names the profile, since an empty value
is no value:

```sh
pytest tests -o esp32tl_build_dir=.pio/build/esp32-s3 \
             -o esp32tl_board_profile=esp32-s3-devkitc -o esp32tl_board_profile_path=
```

Without `-o esp32tl_board_profile_path=`, both options are set and pytest stops before collection:
`ERROR: esp32tl_board_profile and esp32tl_board_profile_path are both set: set the bundled name or the file`.

Each job writes its own JUnit file and uploads it under its own name, so the reports of two chips
never mix: the test ids are the same in both, and the job's name and the file's say which chip
ran. `-o junit_suite_name=${{ matrix.name }}`, a pytest option, also puts the name in the
report's `testsuite` element. Each test's run report starts with the build it loaded, as
`build: /path/to/project/.pio/build/esp32-s3 (PlatformIO for esp32s3); app_sha256 {sha256}`, and
names its board profile by the profile's `name` on the next line, as
`board my-s3-board, watching …`, so a test that ran another build or profile than its matrix entry
names shows it
([the link's report](esp32sim-backend-user.md#the-links-report)).

Within one job, `-n auto` runs the session's tests in parallel as usual, with pytest-xdist
installed beside esp32tl ([parallel execution](#parallel-execution)). On the serial backend each job is one board on its own `--port`, on a runner the board is attached
to; the chips that backend runs, and the profiles it refuses, are in
[builds and flashing](serial-backend-user.md#builds-and-flashing).

A test that asks for what esp32sim does not model on a chip, an SD card on the S3 for example, is
skipped in that chip's job with the reason
([chips other than the classic ESP32](esp32sim-backend-user.md#chips-other-than-the-classic-esp32)),
and runs in the others. `-ra`, in `addopts` above, lists each job's skips with their reasons, so
a job's skip list is its chip's. Two gaps that table names fail instead of skipping, since nothing
asks for them before the firmware reaches them: deep sleep on the S3, C3 and C6, where
`board.wait_for_wake` raises when its time is up, and Bluetooth on the C6 with an image built with
ESP-IDF v5.3.6, which fails at `board.power_on()`.

On your machine, the same with a shell loop, which fails when any chip's session fails:

```sh
status=0
for chip in esp32 esp32-s3; do
  pio run -e "$chip" || { status=1; continue; }
  pytest tests -o esp32tl_build_dir=".pio/build/$chip" \
               -o esp32tl_board_profile_path="tests/boards/$chip.toml" \
               --junitxml="junit-$chip.xml" || status=1
done
test "$status" -eq 0
```

or with pytest's `PYTEST_ADDOPTS`, which pytest reads as options before the command line's, for a
shell that stays on one chip:

```sh
export PYTEST_ADDOPTS="-o esp32tl_build_dir=.pio/build/esp32-s3 -o esp32tl_board_profile_path=tests/boards/esp32-s3.toml"
pytest tests
pytest tests/test_console.py
```

A `-o` on the command line comes after `PYTEST_ADDOPTS` and wins over it for the same option.

## One board and its interfaces

Annotate tests with `from esp32tl import Board` and request just `board: Board`:

```python
from esp32tl import Board


def test_hello_world(board: Board):
    board.power_on()
    board.console.expect_exact("Hello world!")
```

`board.console`, `board.gnss`, `board.modem`, `board.lines`, `board.sd_card`, `board.wifi` and
`board.bluetooth` expose the profile's interfaces. Access does not power on the board or select a
UART source, with one exception: the first access to `board.bluetooth` puts the firmware's
controller on the air, so it must come before `board.power_on()`. Missing profile interfaces fail
when accessed; serial capabilities still skip unsupported calls.

Write `board.<interface>`: it is the one way the documentation and the examples use, and a test
that asks for `board` alone needs nothing else. The `console`, `gnss`, `modem`, `lines`,
`sd_card`, `wifi` and `bt` fixtures are shortcuts to these same objects, kept for tests that
already use them and not deprecated ([decision](../decisions/harness.md#tests-reach-the-interfaces-through-board-and-the-fixtures-stay-as-shortcuts)).
Mixing the two styles shares state, including the console's match cursor.

The two styles differ in where a missing interface fails. `board.gnss` on a board profile without
`uart.gnss`, and so `board.modem`, `board.lines` and `board.sd_card` on one without theirs, and
`board.bluetooth` with a build that holds no ELF, raises `BackendError` where the test body reaches
it: the test fails there, after what the body did before, with a traceback through the backend and
the message without a prefix, as
`esp32tl.backends.esp32sim.BackendError: the board profile esp32-devkitc has no lines; its interfaces are console, uart.gnss, sd_card, wifi, bluetooth`.
The `gnss`, `modem`, `lines`, `sd_card` and `bt` fixtures turn the same `BackendError` into an
error at setup, before the body runs, with no traceback and the backend's name before the message:
`esp32sim: the board profile esp32-devkitc has no lines; its interfaces are console, uart.gnss, sd_card, wifi, bluetooth`.
A skip on the serial backend is a skip in either style, at the fixture or where the body reaches
the interface.

For MQTT, use
`board.modem.network().broker`: `network()` explicitly selects the modem's simulated network before
power-on. The `broker` fixture is a shortcut to that service. There is no
`board.broker` property that implicitly configures a network. `board.uart(role)` remains available
for arbitrary UART roles.

For code using backend accessors directly, replace `board.lines()` with `board.lines` and
`board.sd_card()` with `board.sd_card`. The inserted image's host path is now
`board.sd_card.image_path`; `image(...)` starts the builder. Existing fixture-based tests using
`sd_card.insert(path)` need no change.

### The public API

A test imports from three modules, and from no other:

| Module | What it holds |
|---|---|
| `esp32tl` | Every class, exception and constant a test names: the board (`Board`, `Boot`, `UptimeZero`, `POWERON_RESET`, `DEEPSLEEP_RESET`, `RESET_REASONS`, `Condition`, `condition`), its profile (`BoardProfile`, `UartRoute`, `LineRoute`, `SdCardRoute`), the interfaces and what they return (`Console`, `ConsoleLine`, `Uart`, `Gnss`, `Modem`, `Lines`, `OutputLine`, `LineEvent`, `SdCard`, `CardImage`, `Wifi`, `Bluetooth`, `Tap`, ...), the sources and models (`Replay`, `Real`, `Model`, `ReactiveModel`, `IdleFramedModel`, `Timed`, `GnssReceiver`, `ModemModel`, `ModemCommand`, `ModemRestart`, `ModeChange`, `Protocol`, `AT`, `PMTK`, `UBX`, `GNSS`), the build and the flash (`Build`, `FlashSnapshot`, `Namespaces`, `Typed`, `u8` to `i64`), the network and its services (`Internet`, `LinkChange`, `Broker`, `Message`, `MqttClient`, `ConnectionEnd`, `TEST_CLIENT`, `Publication`, `HttpServer`, `HttpRequest`, `NtpExchange`, `DnsQuestion`, `TcpConnection`, `CertificateAuthority`, ...), Bluetooth (`Air`, `SppPeer`, `BluetoothDevice`, `ChannelEvent`, `WrittenLine`, `Refusal`, `Pairing`, `BlePeripheral`, `BleCentral`, `BleDevice`, `GattService`, `GattCharacteristic`, `Advertisement`, `LeConnection`, `Disconnection`, `GattRead`, `GattWrite`, `Notification`, `Subscription`, `LePairing`, `Discovery`, `DiscoveredService`, `DiscoveredCharacteristic`, `LeEvent`, ...) and the exceptions (`NeverHappened`, `RunEnded`, `BoardOff`, `SourceError`, `BackendError`, `PeerError`, `AirError`, `BleError`, `ConnectionEnded`, `NotReachable`, ...). `esp32tl.__all__` lists them all |
| `esp32tl.units` | The units: `Duration`, `Speed`, `Distance`, `Rate`, `Heading`, `Position`, their constructors (`ms`, `seconds`, `km_h`, `m`, ...), the compass points and `about` ([units](models-user.md#units)) |
| `esp32tl.recording` | The recording format: reading and writing a recording, and its `Recording`, `Header`, `Event` and `ClockSwitch` ([the recording format](../recording-format.md)) |

```python
from esp32tl import ChannelEvent, SppPeer
from esp32tl.units import Duration


def answered_within(peer: SppPeer, limit: Duration) -> list[ChannelEvent]:
    return [event for event in peer.events if event.at <= limit]
```

A type a test meets is importable from one of the three: the argument or the result of a public
method or property, or the type of a public attribute or field, of anything `esp32tl` exports,
the methods a test writes for a `Model`, a `Source` or a `StreamService` among them. What the
backend alone calls, an interface's side of the link (`receive`, `poll`, `quiet_until`, ...) and
the constructors of what the backend makes, is the package's own. The other modules,
`esp32tl.interfaces.board`, `esp32tl.net.mqtt`, `esp32tl.bluetooth.peer`, `esp32tl.flash` and the
rest, are where the package keeps the names, which may move between them:
`from esp32tl.net.mqtt import Message` works today, but only `from esp32tl import Message` is
promised.

Each name is the class's own, and says what it belongs to where a bare one would not: `ConsoleLine`
beside `LineRoute`, the board profile's GPIO line; `HttpRequest`, what the HTTP
server was asked; `MqttClient` of the broker, beside `TcpClient`, the test's side of a TCP connection it
opens; `ConnectionEnd`, how an MQTT connection ended, beside `ConnectionEnded`, the exception a
TCP connection the test opened raises once it has ended; `ModemCommand`, `ModemRestart` and
`ModeChange` of the modem's model. The model itself is a class named
`Modem`, exported as `ModemModel`, since `Modem` is the board's modem interface.

## The fixtures

`board` is the board, off until the test powers it on. The details below describe esp32sim.
On [serial](serial-backend-user.md#power-on-time-and-console), power-on is a reset, the clock
advances continuously in wall time, waits poll the running board, and UTC comes from the host.
Setup may already have flashed and booted the hardware.

For esp32sim:

- `board.power_on(utc=None)` starts the run: the runner starts, the firmware boots, and the
  board's clock starts at 0, where it stays until the test waits. `utc`, a `datetime` with a
  timezone, is the world's time at power-on, `2026-01-01T12:00:00Z` unless given: `board.utc` and
  the GNSS receiver model's sentences are dated from it. On a board that is on it raises
  `BoardOff: the board is on already: call board.power_off() first`. After `board.power_off()` it
  switches the supply back on ([resets and power cycles](#resets-and-power-cycles)).
- `board.reset()` and `board.power_off()` reset the chip and switch the board's supply off, at
  the board's time now; `board.boots` and `board.boot` are the chip's boots
  ([resets and power cycles](#resets-and-power-cycles)).
- `board.powered` is whether the supply is on, from `power_on()` to `power_off()`;
  `board.started`, whether the test has called `power_on()` at all.
- `board.wait(duration)` lets the board run for `duration` of its own time, and returns at
  exactly that instant: `board.wait(seconds(2.5))` moves `board.now` by 2.5 s.
- `board.wait_until(condition, within=duration)` lets the board run until `condition()`, a
  function of no arguments, returns something true, for at most `within` of the board's
  time, and returns what it returned. The condition is asked at once, then at every turn of the
  link: at least every 100 ms of the board's time, and at every other turn the link takes, such
  as the one at which a replayed sentence goes in. It runs in the test's thread while the board stands still, so it can
  read anything the test can. When `within` runs out it raises `NeverHappened`, an
  `AssertionError`, which names the condition by its description when it is a
  [described condition](#described-conditions), by `repr` when it is an object without a
  `__qualname__`, and otherwise by its qualified name:
  `the condition test_x.<locals>.<lambda> did not happen within 2 s: waited from 10 s to 12 s of the board's time`.
- `board.wait_for_wake(within=duration)` lets the board run to the chip's next boot, which must be
  a wake from deep sleep, and returns its `Boot`
  ([waking from deep sleep](#waking-from-deep-sleep)).
- `board.next_boot(cause=None, *, uptime_zero=False)` is a [described condition](#described-conditions)
  for the chip's next boot, of any cause or of `cause` only, with `uptime_zero=True` only once its
  `uptime_zero` is known, for `board.wait_until`; `board.uptime_zero(boot)` is a held `Boot`'s
  `uptime_zero` as the backend knows it now
  ([resets and power cycles](#resets-and-power-cycles)).
- `board.now` is the board's time since the first power-on, a `Duration`; `board.utc` the
  world's time, the UTC instant at the first power-on plus `board.now`. Neither restarts at a
  reset or a power cycle.
- Waiting, or reading `board.now`, `board.utc` or `board.boots`, before the first
  `board.power_on()` raises `BoardOff: the board is off; call board.power_on() first`. A board
  switched off by `board.power_off()` can be waited on.
- When the run ends while the test waits, because the runner exited or the harness found the link
  wrong, the wait raises `RunEnded`, which says when, as far as the harness knows, and why
  ([when the run ends](#what-a-failure-looks-like)).
- `board.app` is the `Build` (from `esp32tl`) the runner flashes, with its `directory`,
  `kind`, `flash_files`, `partition_table`, `app_image`, `elf` and `chip`
  ([builds](builds-technical.md#interfaces)).
- `board.profile` is the board profile the test runs on: `esp32-devkitc`, unless the
  `board_profile` marker selects another by bundled name or by
  `@pytest.mark.board_profile(path="boards/test-board.toml")`. A relative path starts at the test
  file's directory, also for inherited markers; strings and `pathlib.Path` are accepted.
  Name and path cannot be combined ([board profiles](board-profiles-user.md#choosing-a-profile)).
  A test that only reads it needs no `power_on()`, and no runner starts for it.
- `board.console_output` is every byte the firmware has printed on its console so far.
- `board.dut` is pytest-embedded's `Dut`, the same object as its `dut` fixture; its log,
  `dut.log`, still gets the runner's output. Do not call `dut.expect`: it waits in wall time while
  the board stands still, and times out.
- `dut.app`, from pytest-embedded's `dut` or `app` fixture, is pytest-embedded's base `App`,
  without `flash_files` or `target`: the backend runs no pytest-embedded service, so
  pytest-embedded builds nothing more. Use `board.app`.

Durations are `Duration`s, made by `ms()`, `seconds()`, `minutes()` and `hours()` from
`esp32tl.units` ([models and units](models-user.md#units)), 0 or more. A plain number raises
`TypeError`, as in `board.wait takes a Duration, as seconds(2.5), not 2.5`, and one below 0
`ValueError`, as in `board.wait takes a Duration of 0 or more, not -1 s`. An expect's `timeout` is
the exception: it takes a `Duration`, or a number of seconds as in pytest-embedded.

### Described conditions

`esp32tl.condition(find, description)` makes a condition for `board.wait_until` that speaks in the
test's words. `find` is a function of no arguments, as for any condition; `description` names
the event:

```python
from esp32tl import Condition, condition


def trip_end(broker: Broker, device: str) -> Condition[Message | None]:
    start = len(broker.messages)
    topic = f"fleet/{device}/trip/end"
    return condition(
        lambda: next((m for m in broker.messages[start:] if m.topic == topic), None),
        f"a trip/end from {device}",
    )


end = board.wait_until(trip_end(broker, "car-1"), within=minutes(3))
```

- Called, the condition calls `find` and returns what it returns. `repr` is the description,
  and `board.wait_until` names the condition by it, on both backends: in the run report,
  `waits up to 180 s for a trip/end from car-1`, and when it times out,
  `NeverHappened: a trip/end from car-1 did not happen within 180 s: waited from …`.
- It refuses to be read as a boolean, before and after a match:
  `assert trip_end(broker, "car-1")` raises
  `TypeError: call the condition 'a trip/end from car-1' with (), or pass it to board.wait_until`
  instead of passing on the truth of a function. The optional `service=` names it there instead
  of the description: `condition(find, "the next MQTT message", service="MQTT")` raises
  `TypeError: call the MQTT condition with (), or pass it to board.wait_until`.
- The optional `waiting=`, a function of no arguments, says why the event has not happened yet.
  It is called once, when a wait on the condition times out, on both backends, in the test's
  thread, as `find` is. On esp32sim the board is held while it runs; the serial backend's board
  keeps running, so what it reads may change meanwhile. What it returns, unless `None`, goes after the
  window's length, followed by `; `, and is the exception's `why`. With
  `waiting=lambda: None if broker.messages else "the broker has no message"`, the wait above times
  out as `NeverHappened: a trip/end from car-1 did not happen within 180 s: the broker has no
  message; waited from …`, and the run report has `: the broker has no message` after `did not
  happen within 180 s`. The TCP and HTTP
  conditions of the network use it to name the SYNs the device has not answered
  ([network](network-user.md#open-a-tcp-connection-to-the-device)).
- It is a frozen `esp32tl.Condition`, generic in what `find` returns, for annotating helpers.
  The conditions of the network's services, of TLS, of `lines.output(...).is_on`, `.is_off`
  and `.next_change()`, `lines.next_change()`, and `board.next_boot()` are the same type, and
  are annotated so: `broker.next_message(...)` is a `Condition[Message | None]`, which a helper
  typed `def wait(c: Condition[Message | None])` accepts.
- What it checks is up to `find`: the condition itself keeps nothing and consumes nothing; `find`
  may keep what it likes, as the package's own do. Capture a boundary, as `start` above, when the
  condition makes it, so that a message already received does not satisfy it. A wait on esp32sim
  calls `find` at every turn of the link, at least every 100 ms of the board's time, so a `find`
  that reads the whole history since `start` at each call costs, over a long wait on a busy
  history, time that grows with the square of its records. The package's own `next_*` conditions
  look at each poll only at what came since their last poll; `board.next_boot()` too, though
  reading `board.boots` on esp32sim builds every boot so far.
- `find` that is not callable, a `description` that is not non-empty text, or an empty
  `service` raises `TypeError` at once, as
  `condition: description is '', not text naming the event`.

### Resets and power cycles

A test can reset the board and switch it off and on, as many times as it likes, in one run on one
clock:

```python
from esp32tl import Board
from esp32tl.units import minutes


def test_the_count_survives_a_power_cut(board: Board):
    board.power_on()
    board.console.expect_exact("boots: 1")
    board.power_off()
    board.wait(minutes(5))
    board.power_on()
    board.console.expect_exact("boots: 2")
    assert board.boot.reason == "POWERON_RESET"
```

- `board.reset()` pulses the chip's EN pin, as a board's reset button does: the ESP32 alone
  restarts, through a power-on reset, and the rest of the board keeps its power. On a board that
  is off it raises `BoardOff: the board is off: board.reset() resets a board that is on`.
- `board.power_off()` switches the board's supply off. The chip stops; the SD card in the slot,
  and the parts of the board a model stands for, the GNSS receiver and the modem, lose their power
  with it. The board's clock runs on: `board.wait` and `board.wait_until` work as before, and the
  world around the board, a broker or an access point, goes on in that time. Before the first
  `power_on()` it raises `BoardOff: the board is off; call board.power_on() first`, and on a board
  that is off already `BoardOff: the board is off already: call board.power_on() to switch it on`.
- `board.power_on()` after `power_off()` switches the supply back on at the board's time now, and
  the chip boots through a power-on reset. `utc` cannot be given again: `ValueError: power_on: utc
  is the world's time at the first power-on; it has run on since, and the board's clock with it`.
- Sources and taps are still given before the first `power_on()` only:
  `SourceError: uart.gnss: the board has been on: give an interface its source before
  board.power_on()`. `board.nvs()` is not: with the board off it writes the NVS the next boot
  finds, `merge=True` keeping what the firmware stored
  ([flash contents](flash-contents-user.md#change-nvs-between-two-boots)), and with the board on it
  raises `BoardOff: board.nvs: the board is on: call board.power_off() first, and write the flash
  while it is off`. What the firmware wrote to flash, a test reads with
  [`board.flash()`](flash-contents-user.md#read-the-flash-of-a-run), through both.
- `board.boots` is the chip's boots, in order, as `Boot`s (from `esp32tl`): `number`,
  1 for the first; `at`, the board's time the boot started, a `Duration`; `cause`, the reset cause
  the ROM reports for it (`RESET_REASON` in ESP-IDF's `esp32/rom/rtc.h`); and `reason`, its name,
  as the ROM prints it, such as `POWERON_RESET` (1) or `SW_CPU_RESET` (12, `esp_restart()`). The
  first boot is at 0 with cause 1; every reset adds one, whoever caused it: `board.reset()` and
  `power_on()` give cause 1, the firmware and its watchdogs their own. `board.boot` is the last.
  A boot is there once the board has run into it: right after `board.reset()`, or the
  `power_on()` that follows a `power_off()`, `board.boot` is still the one before, until the test
  lets the board run, for as little as `board.wait(ms(1))`.
- `boot.idf_reason` is what ESP-IDF's `esp_reset_reason()` reports for the boot, by name:
  `ESP_RST_POWERON`, `ESP_RST_SW`, `ESP_RST_PANIC`, `ESP_RST_TASK_WDT`, `ESP_RST_DEEPSLEEP` and
  the rest of `esp_reset_reason_t`. It is what an ESP-IDF application, an Arduino-ESP32 one
  included, built with ESP-IDF v5.3 to v6.1 reports, not a property of the chip:
  `get_reset_reason()` of each chip's `components/esp_system/port/soc/<chip>/reset_reason.c`,
  the same in v5.3.6 (the classic ESP32, the S3, C3 and C6), v5.5.4 (the P4) and v6.1 (the S31),
  the versions the validation images are built with. It needs more than `cause`: the ROM reports
  12 (`SW_CPU_RESET`) alike for `esp_restart()` and for a panic, and only the hint the boot before
  left in an RTC register (`RTC_RESET_CAUSE_REG`, `RTC_CNTL_STORE6` on the classic ESP32) tells
  them apart, as `ESP_RST_SW` and `ESP_RST_PANIC`. After a reset of cause 3 or 12 the hint gives
  `ESP_RST_PANIC`, `ESP_RST_INT_WDT`, `ESP_RST_TASK_WDT` or `ESP_RST_BROWNOUT`, and anything else,
  no hint included, gives `ESP_RST_SW`; every other cause gives the same name whatever the
  register holds. On esp32sim the runner reads the register at each reset
  ([protocol 5.5](../protocol.md#55-reset)), and on the classic ESP32 `firmware/reset_reasons`
  checks five of them against what the firmware printed: a power-on, `esp_restart()`, a panic, a
  task watchdog's panic and a wake from deep sleep. A cause ESP-IDF does not map is
  `ESP_RST_UNKNOWN`, as `esp_reset_reason()` reports it: on the classic ESP32 4 (`OWDT_RESET`), 10
  (`INTRUSION_RESET`) and 14 (`EXT_CPU_RESET`, the APP CPU reset by the PRO CPU), on the S3 and
  the C3 6, 19 and 23 among others, on the P4 6, 8, 17, 21 and 27 among others. Where a chip's tables differ:

  | Cause | ESP32 | ESP32-S3 | ESP32-C3 | ESP32-C6 | ESP32-P4 | ESP32-S31 |
  |---|---|---|---|---|---|---|
  | 6 | `ESP_RST_SDIO` | `ESP_RST_UNKNOWN` | `ESP_RST_UNKNOWN` | `ESP_RST_SDIO` | `ESP_RST_UNKNOWN` | `ESP_RST_DEEPSLEEP` |
  | 7 | `ESP_RST_TASK_WDT` | `ESP_RST_TASK_WDT` | `ESP_RST_TASK_WDT` | `ESP_RST_TASK_WDT` | `ESP_RST_WDT` | `ESP_RST_TASK_WDT` |
  | 8 | `ESP_RST_INT_WDT` | `ESP_RST_INT_WDT` | `ESP_RST_INT_WDT` | `ESP_RST_INT_WDT` | `ESP_RST_UNKNOWN` | `ESP_RST_INT_WDT` |
  | 17 | `ESP_RST_UNKNOWN` | `ESP_RST_WDT` | `ESP_RST_WDT` | `ESP_RST_WDT` | `ESP_RST_UNKNOWN` | `ESP_RST_UNKNOWN` |
  | 18 | `ESP_RST_UNKNOWN` | `ESP_RST_WDT` | `ESP_RST_WDT` | `ESP_RST_WDT` | `ESP_RST_WDT` | `ESP_RST_WDT` |
  | 19 | `ESP_RST_UNKNOWN` | `ESP_RST_UNKNOWN` | `ESP_RST_UNKNOWN` | `ESP_RST_UNKNOWN` | `ESP_RST_PWR_GLITCH` | `ESP_RST_PWR_GLITCH` |
  | 20 | `ESP_RST_UNKNOWN` | `ESP_RST_UNKNOWN` | `ESP_RST_POWERON` | `ESP_RST_EFUSE` | `ESP_RST_EFUSE` | `ESP_RST_EFUSE` |
  | 21 | `ESP_RST_UNKNOWN` | `ESP_RST_USB` | `ESP_RST_USB` | `ESP_RST_USB` | `ESP_RST_UNKNOWN` | `ESP_RST_UNKNOWN` |
  | 22 | `ESP_RST_UNKNOWN` | `ESP_RST_USB` | `ESP_RST_USB` | `ESP_RST_USB` | `ESP_RST_USB` | `ESP_RST_USB` |
  | 23 | `ESP_RST_UNKNOWN` | `ESP_RST_UNKNOWN` | `ESP_RST_UNKNOWN` | `ESP_RST_UNKNOWN` | `ESP_RST_USB` | `ESP_RST_USB` |
  | 24 | `ESP_RST_UNKNOWN` | `ESP_RST_UNKNOWN` | `ESP_RST_UNKNOWN` | `ESP_RST_JTAG` | `ESP_RST_JTAG` | `ESP_RST_JTAG` |
  | 26 | `ESP_RST_UNKNOWN` | `ESP_RST_UNKNOWN` | `ESP_RST_UNKNOWN` | `ESP_RST_UNKNOWN` | `ESP_RST_CPU_LOCKUP` | `ESP_RST_CPU_LOCKUP` |

  On every chip 1 is `ESP_RST_POWERON`, 3 and 12 the software resets above, 5 `ESP_RST_DEEPSLEEP`,
  15 `ESP_RST_BROWNOUT`, and 9, 11, 13 and 16 `ESP_RST_WDT`. On the P4 an eFuse CRC reset, 20, is
  `ESP_RST_EFUSE` from chip revision v0.1 on, every revision esp32sim models; ESP-IDF says
  `ESP_RST_DEEPSLEEP` for it on v0.0, which esp32tl does not tell apart. `idf_reason` is
  `None` for a chip esp32tl has no table for, which no board profile names, and on the serial
  backend, which skips `board.boots`. It takes no part in comparing two boots, as `uptime_zero`.

  For a firmware that panics, the next boot:

  ```python
  booted = board.next_boot()
  boot = board.wait_until(booted, within=seconds(5))
  assert (boot.reason, boot.idf_reason) == ("SW_CPU_RESET", "ESP_RST_PANIC")
  ```

- `boot.uptime_zero` is where the boot's uptime counter, the one `esp_timer_get_time()` reads,
  counted zero on the board's clock, an `UptimeZero` (from `esp32tl`) of two
  `Duration`s, `earliest` and `latest`; `None` until the backend has seen the counter count, for a
  firmware that never starts it, and again while the zero is not known: after the counter stopped,
  or once it counts from a zero that may lie before power-on, as a load of more than the run has
  lasted gives. A load while it counts, as ESP-IDF makes after a light sleep, gives the boot a new
  `uptime_zero`. A stamp of `uptime` the firmware took from the counter, an `esp_timer_get_time()`
  it sent, was taken between `earliest + uptime` and `latest + uptime` on the board's clock, the
  clock of `board.now`, the console's lines and the run's log. On esp32sim the runner reads the
  counter at every slice boundary ([protocol 5.24](../protocol.md#524-uptime-ext-245)). On the
  classic ESP32 the zero is known to within a millisecond, the slice in which ESP-IDF started the
  counter, after the ROM, the bootloader and the application's start-up, some tens of milliseconds
  after `boot.at`: for `firmware/persistence`'s first boot, between 48 and 49 ms after power-on. A
  load while it counts is known to within two milliseconds. On the ESP32-S3, -C3 and -C6 the zero is
  known to within 60 ns, on the ESP32-P4 within 65 ns, and `earliest ≤ boot.at ≤ latest`: esp32sim does not model the
  application's reset of the counter, which counts from the reset; on the chip the zero comes at the
  application's start, as on the classic ESP32. On the ESP32-S31 `uptime_zero` stays `None`: the
  runner cannot tell the rate its counter counts at. After a load while the counter counts, `uptime_zero`
  is still the zero from before the load for up to two slices of the run, a millisecond each: a
  stamp the firmware sent in them, put on the board's clock with it, is off by what the load
  changed, for ESP-IDF's load after a light sleep, which makes up for the time asleep, by what the
  making up got wrong. `boot.uptime_zero` takes no part in comparing two boots.

  A `Boot` is a record of what was known when it was read, and frozen: one read before the zero
  was seen keeps `None`, and one read before a load keeps the zero from before it. The boot that
  `board.wait_until(board.next_boot(), ...)` and `board.wait_for_wake()` return was read at the
  first poll that saw the boot. On the classic ESP32 the counter counts some tens of milliseconds
  after the reset, so whether that `Boot` has the zero depends on where the link's turn that
  brought the reset ended: `board.wait_for_wake()`'s ends within a millisecond of it, so its boot
  has `None` for good; a turn of `board.wait_until` may run past the zero or stop before it, as
  at the end of a short `within` or of a `board.wait`. There are two ways to be sure of the zero:

  - `board.next_boot(uptime_zero=True)` returns the boot only once its zero is known, so the `Boot`
    the wait returns has it (below).
  - `board.uptime_zero(boot)` is the zero of a `Boot` the test holds, as the backend knows it now:
    the same as `board.boots[boot.number - 1].uptime_zero`, `None` while it is not known. Use it
    for the boot of a wake from deep sleep, for a stamp of an older boot, or after a load. A
    `boot` that is not a `Boot` raises `TypeError: board.uptime_zero: boot is 2, not a Boot from
    board.boots, board.next_boot() or board.wait_for_wake()`, and one whose `number`, `at` or
    `cause` is not that of this board's boot of its number raises `ValueError:
    board.uptime_zero: boot 3 at 2.5 s (reset cause 1) is not one of this board's 2 boots`.

  ```python
  from esp32tl.units import ms, seconds

  booted = board.next_boot(uptime_zero=True)
  printed = console.line(r"uptime_ms=(\d+)")
  board.reset()
  boot = board.wait_until(booted, within=seconds(1))
  line = board.wait_until(printed, within=seconds(5))
  zero = boot.uptime_zero
  assert zero is not None
  uptime = ms(int(line[1]))
  assert zero.earliest + uptime <= line.at <= zero.latest + uptime + ms(2)
  ```

  Both conditions are made before the reset: `console.line` matches only a line that ends after
  it is made, and the firmware may print its line in the same turn of the link that brought the
  zero. The firmware printed its uptime in whole milliseconds, rounded down, so the stamp can be up to
  1 ms early; the second millisecond is room for the time from its read of the counter to the end
  of the line, which `line.at` is the time of. A stamp in microseconds, read just before the line,
  needs less.

  Serial skips it with `board.boots` (the backend does not see the chip's resets).
- `board.next_boot(cause=None, *, uptime_zero=False)` is a [described condition](#described-conditions), a
  `Condition[Boot | None]`, for the first boot after the call: of any cause, or of `cause` only,
  a reset cause as a number such as `POWERON_RESET` (from `esp32tl`). A boot of
  another cause does not satisfy it, and a later one of `cause` still can; to check that the very
  next boot had a cause, wait on `board.next_boot()` and assert on its `reason`. It consumes
  nothing, and returns the same `Boot` each time it is asked. Make it before the call that causes
  the boot, as any `next_*` condition: before `board.reset()`, before `board.power_on()` (while the
  board is off is fine), or before what makes the firmware restart.

  ```python
  from esp32tl import POWERON_RESET

  booted = board.next_boot(POWERON_RESET)
  pressed = board.now
  board.reset()
  boot = board.wait_until(booted, within=seconds(1))
  assert boot.at == pressed
  ```

  A timeout names it, `NeverHappened: the next boot (POWERON_RESET) did not happen within 1 s:
  waited from …`, and so does the run report: `waits up to 1 s for the next boot (POWERON_RESET)`.
  Without `cause` it is `the next boot`; a cause `RESET_REASONS` does not name reads
  `the next boot (reset cause 42)`. With `uptime_zero=True` it returns the boot only while its
  `uptime_zero` is known, read again at each poll, and `None` before; it still waits on the first
  boot after the call (of `cause`), not on a later one whose zero is known. Its description ends
  `, with its uptime zero`: a boot whose zero never comes, on a firmware that never starts the
  counter, on the ESP32-S31, or a boot that resets again before its counter counts, times out as
  `NeverHappened: the next boot, with its uptime zero did not happen within 1 s: waited from …`.
  An `uptime_zero` that is not `True` or `False` raises `TypeError: board.next_boot: uptime_zero is
  1, not True or False`. A `cause` that is not a number raises `TypeError:
  board.next_boot: cause is 'POWERON_RESET', not a reset cause as a number, such as
  POWERON_RESET`, and before the first `power_on()` it raises `BoardOff` as `board.boots` does.
  `board.reset()` and `board.power_on()` return `None`, not this condition
  ([the decision](../decisions/harness.md#the-next-boot-is-a-condition-the-test-makes-reset-and-power_on-return-nothing)).
  For a wake from deep sleep use `board.wait_for_wake`, which takes no turn of the link through
  the sleep and fails on a boot that is no wake; `board.next_boot(DEEPSLEEP_RESET)` would be asked
  every 100 ms of the sleep and pass over other boots.
- Once the backend has the run's end, `board.reset()`, `board.power_off()` and
  `board.power_on()` raise `RunEnded` without waiting, as a wait does, and change nothing. In the
  moment between the runner's exit and the backend learning of it, a call still goes into the
  run's log, and the next wait raises `RunEnded`:
  `RunEnded: the run ended at 12 s before board.reset(): the runner exited with code 0; …`.
- Times stay on the board's clock: `board.now`, the console's lines, the run's log in a failed
  test's report and every tap's recording go on from the first power-on through every boot. What
  the firmware counts from its own boot, `esp_timer_get_time()` or the ESP-IDF log's
  milliseconds, starts again at each one: `board.now - board.boot.at` is the board's time since.
  `board.boot.at` is when the chip reset, not when its `esp_timer` counted zero; to put an
  `esp_timer_get_time()` the firmware sent on the board's clock, add it to
  `board.boot.uptime_zero`.

What each kind of restart keeps, on esp32sim:

| | The firmware restarts (`esp_restart()`, a watchdog) | `board.reset()` | `board.power_off()`, `board.power_on()` |
|---|---|---|---|
| Reset cause | the firmware's, 12 for `esp_restart()` | 1, `POWERON_RESET` | 1, `POWERON_RESET` |
| Flash: the app, NVS, OTA data, what the firmware wrote | kept | kept | kept |
| RAM, `.noinit` (`__NOINIT_ATTR`) included | kept | zeroed | zeroed |
| RTC memory (`RTC_NOINIT_ATTR`) | kept | zeroed | zeroed |
| PSRAM | zeroed | zeroed | zeroed |
| The RTC timer | runs on | starts at 0 | starts at 0 |
| SD card: its files | kept | kept | kept |
| SD card: its state | kept | kept | uninitialised: the driver mounts it again |
| GNSS receiver model | sends on | sends on | silent while off; then sends again, with the fix the test gave it |
| Modem model | keeps its state | keeps its state | off: PPP and its TCP sessions end; it starts again on a PWRKEY pulse, or the test's `modem.power_on()` |
| Wi-Fi station | the access point keeps its side of the link | joins a fresh access point again | joins a fresh access point again |
| Simulated network: broker, HTTP server, DHCP | goes on | goes on | goes on |
| The test's levels on the lines | kept | kept | kept |
| `board.now`, `board.utc` | run on | run on | run on, also while off |

`RTC_DATA_ATTR` variables are no exception: ESP-IDF's bootloader loads them from the image at every
boot but a wake from deep sleep. The build's files are never written: the flash the firmware
writes to is the runner's copy, and
lasts as long as the run. esp32sim carries out each flash program and erase command whole, so a
power cut falls between two of them: torn writes are not modelled, nor is what a real chip's RAM
holds after a reset, which is zero here. A replay or a real device at the far end of a route is not
on the board's supply, and keeps sending. Bytes that reach the chip while it is off, or after a
reset before the firmware has set up its UART again, are lost, each such drop an `unrouted_rx`
warning in the link's report: from a replay or a real device, and from a model too, an answer or
a sentence that was on the line when the chip reset or the power went. Every interface of a board
profile is on the board's supply ([board profiles](board-profiles-user.md#the-profiles-esp32tl-ships)).

On [serial](serial-backend-user.md#power-on-time-and-console), `board.reset()` pulses EN through
the adapter, and `board.power_off()`, `board.boots`, `board.next_boot()` and
`board.wait_for_wake()` skip the test.

`console` is the firmware's console, read and written on the board's clock: its UART0, or the
chip's USB-Serial/JTAG controller on a board profile with `console = "usb-serial-jtag"`
([board profiles](board-profiles-user.md#a-console-on-usb-serialjtag)), with the same methods either
way. On the ESP32-P4 and the ESP32-S31, UART0 is the console without its pins: what the test writes
goes into the firmware's UART0 from the board's time it wrote it on, as fast as UART0 takes it,
and the output's times are those of the runner's slices, at most 1 ms late ([esp32sim backend](esp32sim-backend-user.md#the-esp32-p4-and-the-esp32-s31)):

- `console.expect(pattern, timeout=30, *, return_what_before_match=False)` lets the board run until
  `pattern` appears on the console after the last match, for at most `timeout` of the board's time,
  a `Duration` or a number of seconds, and returns the `re.Match`, on bytes, for a pattern given as
  text too, as pytest-embedded's does: `match.group(1)` is `bytes`, and `.decode()` makes it text.
  For a value printed on a line, `console.line(pattern)`, below, matches the line's text and gives
  `str` groups. `pattern` is a regular
  expression, as a string, bytes or a compiled pattern, or a list of them, which matches whichever
  appears first. A string or bytes compiles with `re.DOTALL`, as pexpect compiles it, so `.` matches
  across lines, and `^` matches where the last match ended. A compiled pattern keeps its own flags,
  where pexpect drops them from a compiled string pattern. With `return_what_before_match=True` it
  returns the bytes between the last match and this one. These are pytest-embedded's names, its
  search from the end of the last match and its return values.
- `console.expect_exact(pattern, ...)` is the same with strings or bytes found as they are, not as
  regular expressions.
- `console.expect(pexpect.EOF)` waits for the end of the run and returns what the console printed
  after the last match, without trailing whitespace; `pexpect.EOF` may also be one pattern of a
  list.
- A pattern that does not appear within `timeout` raises `NeverHappened`:
  `'fix' on the console did not happen within 30 s: waited from 0 s to 30 s of the board's time`.
  A run that ends first raises `RunEnded`.
- An expect before `board.power_on()` raises `BoardOff`.
- `console.line(pattern)` is a condition for `board.wait_until`: the first console line that
  ends from now on and matches `pattern`, a regular expression on the line's text, as a string or
  compiled. `board.wait_until(console.line(r"^fix lat=(?P<lat>\S+)"), within=seconds(2))` returns
  a `ConsoleLine`, whose `at` is the board's time its line ending came at, `text` the line without its
  line ending and without ESP-IDF's colour codes, and `line["lat"]` or `line[1]` a group of the
  match. One that does not come raises
  `NeverHappened: a console line matching '^fix lat=(?P<lat>\\S+)' did not happen within 2 s: …`,
  the pattern as Python's `repr` writes it, its backslashes doubled.
  Once it has found a line, the same condition looks after it, so asking it again in a loop
  finds the next one.
- `console.lines(pattern=None, *, since=None)` is the list of lines printed so far, those that
  ended at `since` or later when it is given, and those that match `pattern` when it is given:
  `console.lines(since=lost_at) == []` says nothing was printed since. A line not ended yet is not
  in it. `line` and `lines` do not move `expect`'s search, nor it theirs.
- pytest-embedded's `expect_all` and `not_matching` are not there.

### Typing on the console

After `board.power_on()`, `board.console.write(data)` accepts bytes or UTF-8 text.
`board.console.sendline(line)` accepts the same types and appends LF (`\n`). Neither waits for
an answer; use `expect_exact` or `expect` afterwards:

```python
board.power_on()
board.console.expect_exact("console_repl>")
board.console.sendline("help")
board.console.expect_exact("Print the summary of all registered commands")
```

On esp32sim writes enter GPIO 3 at the current board instant in the next link poll, paced at
115200 baud. The board advances when the test waits. The rate does not follow the guest's UART
configuration: mismatches raise `frm_err`; FIFO overflow and missing routes retain the existing
UART warnings in the report. On serial, writes go directly to the open port at its configured
baud. Console taps include both test input (`in`) and firmware output (`out`).

`gnss` is the far end of the board profile's `uart.gnss` route:

- `gnss.replay(path)` replays a recording into it, at the recording's times, from power-on. It
  must come before `board.power_on()`. The path is relative to the test file's directory. What it
  does, and what it refuses, is in [replay](replay-user.md).
- `gnss.fix(at=...)`, `gnss.move(...)`, `gnss.lose_fix()` and the receiver model's other calls
  put a receiver the test describes there instead. The first of them that succeeds makes the
  receiver, and must come before `board.power_on()`; every later one changes it from the board's
  current instant ([models and units](models-user.md#the-gnss-receiver)). Reading
  `gnss.position` makes no receiver.
- `gnss.real(port)` puts a device on a host serial port there, and the whole run on the wall clock
  ([real source](real-source-user.md)). Only on `esp32sim`: `serial` skips the test at the call.
- One source per test: a model call after `gnss.replay`, or a replay after a model call, raises
  `SourceError`.
- `gnss.tap(path)` records what crosses the route from power-on, both ways, and writes it to
  `path` as a recording once the run has ended. It must come before `board.power_on()` too, and
  goes with a replay, a receiver or nothing at the far end. What it writes is in
  [taps and the run report](run-report-user.md).
- Until the test gives it a source, the route is on the link, listening and silent. So is every
  other UART route of the profile.
- On a board profile without `uart.gnss`, `board.gnss` raises
  `BackendError: the board profile {name} has no uart.gnss; its interfaces are {interfaces}` where
  the test body reaches it, and the `gnss` fixture is an error at setup with the same message after
  `esp32sim: ` ([where it fails](#one-board-and-its-interfaces)). Both profiles esp32tl ships have
  the route.

`board.sd_card` (also the `sd_card` fixture) is the profile's slot, empty until insertion.
For a test running `firmware/sdspi`, prepare and inspect its FAT contents through the library:

```python
def test_the_example_writes_its_files_into_the_image(board: Board):
    card = (
        board.sd_card.image(size_mb=32)
        .with_text("foo.txt", "from the test\n")
        .with_bytes("config.bin", b"\x01\x02")
        .build()
    )
    board.sd_card.insert(card)
    board.power_on()
    board.console.expect_exact("example: Card unmounted", timeout=5)
    assert card.read_text("foo.txt") == "Hello SIMSD!\n"
    assert card.read_bytes("config.bin") == b"\x01\x02"
```

- `image(size_mb=32, cluster_kb=None)` starts a builder. `size_mb` is a whole number from 1 to
  1048576 MiB (1 TiB), or `ValueError`: `size_mb must be a whole number from 1 to 1048576 (MiB)`.
  `insert` takes larger images, up to 2 TiB, made elsewhere. The image is sparse: a card of 32 GiB
  with 32 KiB clusters takes 8 MiB of disk until the firmware writes to it, and one of 1 TiB 256 MiB.
- `cluster_kb` is the size of the clusters, without it mformat's choice (4 KiB at 512 MiB, 32 KiB
  from 32 GiB on), or `ValueError`: `cluster_kb must be one of 1, 2, 4, 8, 16, 32, 64 (KiB)`. From
  512 MiB on the card is FAT32, and a FAT32 file system has 65525 to 268435445 clusters, so that a
  cluster too large for the card, or too small, is refused:
  `a FAT32 card of 512 MiB with 64 KiB clusters has 8190 clusters; FAT32 takes 65525 to 268435445: choose another size_mb or cluster_kb`.
  Below 512 MiB mformat chooses the FAT that suits the count. `minfo -i IMAGE ::` shows them.
  `with_text(name, contents, encoding="utf-8")` and `with_bytes(name, contents)` declare initial
  files and return the same builder. Subdirectories are created automatically. Paths are relative
  with `/` separators; traversal, wildcards (`*`, `?`, `[` and `]`), FAT-invalid characters and
  case-insensitive file collisions are refused with `ValueError`. Readers and listings reject
  the same path syntax, including literal names containing brackets created outside this API.
- `build()` creates a fresh FAT image each time, without inserting it, before power-on or after
  it, for a card to put in while the firmware runs. Existing builds are unaffected by later
  builder changes. The same builds in the same order give the same bytes in every run, whatever
  the host's clock and time zone say: every file and directory the builder writes, with its `.`
  and `..`, is dated 1980-01-01 00:00, the earliest FAT holds, and the volume serial number of
  the n-th card a slot builds is `E32D` followed by n in four hex digits, `E32D-0001` for the
  first, as `mdir` shows it, so that two cards of one test still differ. The builder uses mtools
  (`mformat`, `mcopy`, `mtype`, `mdir`); install it with `brew install mtools` or
  `sudo apt-get install mtools`. Missing commands raise `SourceError`, for example
  `sd_card: missing mformat, mcopy, mtype, mdir; install mtools`.
- `card.read_text(name, encoding="utf-8")` and `card.read_bytes(name)` reopen the current image
  every time, so they see firmware writes. Text decoding preserves line endings, including CRLF.
  Wait for firmware to finish and close a write before reading: the readers never mount the image
  or keep its FAT between calls, and a file the firmware has not closed may not be on the card
  yet, whether the card is in the slot or has been taken out
  ([safe image inspection](#taking-the-card-out-and-putting-one-in)). Missing paths raise
  `FileNotFoundError`; reading a directory raises `IsADirectoryError`. Passing a file as a parent
  directory raises `NotADirectoryError`. `card.list_files(directory="")` returns sorted immediate
  filenames, excluding subdirectories, at the root or a specified relative directory; a file
  argument raises `NotADirectoryError`, and a missing directory raises `FileNotFoundError`.
  These exceptions carry the card-relative path in `filename`. Tool failures, including an
  unreadable FAT filesystem, remain `SourceError` naming the command and its stderr.
- `card.path` is the host image path; the object also supports `os.fspath`. Images created by the
  builder are removed when the board closes, even when never inserted or when the test fails.
  Read or copy results before teardown. Later reader/builder calls raise
  `sd_card: the board is closed`. Externally supplied image paths are never deleted.
- On serial, starting a builder skips just like inserting a model card. These APIs prepare the
  simulator's storage; they do not format a physical card.

- `sd_card.insert(image)` puts a card holding the image file in the empty slot, and returns the
  image's absolute path. Before `board.power_on()` the card is in from power-on; after it, from
  the board's current instant ([below](#taking-the-card-out-and-putting-one-in)). The path is relative to the test
  file's directory. The image is a whole number of 512 KiB units, up to 2 TiB, readable and
  writable; what is in it is the test's,
  usually a FAT file system made with mtools (`mformat`, `mcopy`).
- On `esp32sim` the card is the runner's model of an SD card in SPI mode, on the slot's pins
  ([runner](runner-user.md#put-an-sd-card-in)). It writes what the firmware writes into the file
  as it goes, so the test reads the image back with `CardImage` or mtools once the firmware
  is done. Builder-owned images remain available until the board closes; external images also
  survive teardown. Copy the image first to keep it as it was.
- Without `insert`, the slot is empty, as a board with no card in it: ESP-IDF's driver times out
  (`ESP_ERR_TIMEOUT`).
- `insert` refuses, with `SourceError`: a slot that holds a card
  (`sd_card has a card already: remove it first`), an image that is not there
  (`sd_card: no image at PATH: No such file or directory`) or not a file
  (`sd_card: PATH is not a file`), one of another size
  (`sd_card: PATH has N bytes; an SD card image is a whole number of 512 KiB units`), one above
  2 TiB (`sd_card: PATH has N bytes; an SD card image is at most 2 TiB`), and one the test cannot
  read and write (`sd_card: PATH cannot be read and written: the card writes into it`), and a
  file a route replays from or is tapped into
  (`sd_card: cannot hold PATH: it is the tap of uart.gnss`). A tap into the card's image is
  refused the same way ([taps](run-report-user.md#when-the-test-gets-it-wrong)).
- The report of the run names the card the run had in the slot at its end,
  `sd_card: a card holding PATH`, or `sd_card: empty`, then each change the runner applied while
  the board was on, as `  0.300000000 s  taken out` and `  0.800000000 s  PATH put in`, then a
  card the runner refused, as `  0.900000000 s  refused: PATH put in`, then each change the test
  made that the run ended before, as `  0.900000000 s  not reached: taken out`. The
  `esp32sim_link` property has `sd_card`, `{"image": PATH, "changes": [{"t_ns": T, "image": PATH
  or None}, ...], "refused": [...], "not_reached": [...], "faults": [...]}`
  ([the run report](run-report-user.md)).
- On a board profile without a slot, `board.sd_card` raises `BackendError` where the test body
  reaches it, and the `sd_card` fixture is an error at setup
  ([where it fails](#one-board-and-its-interfaces)), with the message after `esp32sim: `:
  `esp32sim: the board profile gps-alarm-clock has no sd_card; its interfaces are console, uart.gnss, lines.alarm, wifi, bluetooth`.

#### Keeping a prepared card

Firmware that prepares a card on its first mount, preallocating files say, spends that time on every
fresh card: 56 s of the board's time and 20 s of wall time on one firmware and a 2048 MiB card. The
`card_cache` fixture keeps the card a test prepared, for the tests after it and the sessions after
this one:

```python
def test_logs_to_a_prepared_card(board, card_cache):
    card = card_cache.get("first-mount")
    if card is None:
        card = board.sd_card.image(size_mb=32768, cluster_kb=32).build()
        board.sd_card.insert(card)
        board.power_on()
        board.console.expect_exact("logger: card ready")
        board.power_off()
        card_cache.put("first-mount", card)
    else:
        board.sd_card.insert(card)
    board.power_on()
    ...
```

- `card_cache.get(name)` returns a `CardImage`, a clone of the card kept as `name` for this
  application, in the same temporary directory as the builder's images and gone when the board
  closes; or `None`, when no card is kept: never put, evicted, or put by another build. The key is
  the name and the SHA-256 of the app image, so a new build of the firmware starts again. Anything
  else that shapes the card, its size, the board profile or what `board.nvs(...)` gave the firmware,
  belongs in the name.
- The clone is the test's own. The image in the slot still writes through, and what the firmware
  writes reaches that clone only: not the cache, nor another clone.
- `card_cache.put(name, image)` keeps a copy of the image, a `CardImage` or a path relative to the
  test file's directory, in place of one kept before. What the firmware writes to the image after
  that is not kept. Power the board off first, or take the card out: a card in the slot of a board
  that is on is refused, because the firmware may have files open on it:
  `card_cache: 'first-mount': PATH is in the slot of a board that is on: call board.power_off() first`.
  Nor is a missing image kept (`card_cache: 'first-mount': no image at PATH`).
- **A miss costs one more boot.** A test that counts boots, or reads `board.boots`, allows for the
  boot that prepares the card, or powers the board off and asserts on what comes after. The cache
  holds the card alone: NVS and the rest of the flash start as they always do.
- The cards take at most `esp32tl_card_cache_mib` MiB of disk, an ini option, 4096 by default,
  counted from what the files have allocated rather than from their size. When a card needs room,
  those least recently used go first, and using one with `get` counts. A card that alone is above
  the limit is refused, and evicts nothing:
  `card_cache: 'first-mount' needs N bytes on disk; the limit is M: raise esp32tl_card_cache_mib`,
  with both in bytes. Anything but a whole number of MiB from 1 is a usage error:
  `esp32tl_card_cache_mib must be a whole number of MiB, not 'none'`.
- The cards are in pytest's cache, `.pytest_cache/d/esp32tl-card-cache/`, which `--cache-clear`
  empties. Under `-p no:cacheprovider` they last for the session. A card appears whole under its
  name, and pytest-xdist's workers, or another session, may share the directory.
- `card_cache` is a session fixture that works on the test's board: a test that uses it without
  `board` gets `card_cache: no board is running: use it in a test that uses board`.
- On serial the fixture works and `sd_card.insert` skips, as it does for any other image.

#### Taking the card out and putting one in

`sd_card.remove()` takes the card out of the slot and returns its image's absolute path;
`sd_card.insert(image)` puts one in. Before `board.power_on()`, `remove()` takes back an
`insert`, so that the slot is empty at power-on. After it, both act at the board's current
instant, `board.now`, as a hand would: the firmware sees the change the next time it talks to the
card. They return at once; the change reaches the firmware when the test next waits.

```python
def test_writes_go_on_on_the_card_put_in(board: Board):
    first = board.sd_card.image(size_mb=32).build()
    board.sd_card.insert(first)
    board.power_on()
    board.console.expect_exact("sd_remount: wrote 3", timeout=10)
    board.sd_card.remove()
    board.console.expect_exact("sd_remount: write failed", timeout=10)
    second = board.sd_card.image(size_mb=32).build()
    board.sd_card.insert(second)
    board.console.expect_exact("sd_remount: mounted 2", timeout=10)
```

- The change happens between two of the chip's SPI transactions, never inside one. With the slot
  empty, every byte the firmware reads from the card is `0xff`, as on a board whose MISO line is
  pulled up with nothing on it: a command gets no answer and ESP-IDF's driver times out, and a
  file system call on the mounted card fails with an I/O error (`EIO`). The firmware learns of
  the removal that way only: the slot has no card-detect line.
- What the card had written stays in its image: every block the card took before it came out.
  What the firmware had not finished, a file not closed, a block on its way, a directory entry not
  updated yet, is not written for it. The file system on the image is as the firmware left it at
  that instant, which need not be consistent. A firmware that closes each file before it moves on
  leaves only closed files.
- A card that goes in is a card just powered: the firmware must initialise and mount it again
  (`esp_vfs_fat_sdspi_mount`), as after a power-on. A firmware that latches its first storage
  error, as the `sd_data_logger` example does, stays latched. The same image can go in again
  after it came out, and so can another; two images of the same size read as the same card (the
  card's identity comes from its size).
- `remove()` and `insert(image)` also work between `board.power_off()` and the next
  `board.power_on()`, as a hand swaps a card on a board that is off: the firmware that boots then
  finds the card that is in the slot. They act at `board.now`, which runs on while the board is
  off, and reach the run when the test next waits.
- Every insertion checks the image as before power-on, and that no route replays from it or taps
  into it. The image is the card's while it is in the slot; once it is out, the test may read it,
  change it or delete it: the runner has closed it before the firmware runs on.
- `remove()` refuses an empty slot with `SourceError` (`sd_card is empty: there is no card to
  remove`).
- `sd_card.changes` lists what the slot held after each change made with the board on, as
  `CardChange(t, image)`, `image` `None` for an empty slot, once the runner has applied it: the
  test reads it after a wait. `t` is the first slice boundary at or after the instant the test
  made the change, at most an emulated millisecond later. `sd_card.held` is the image of the
  card the run has, from those changes; `sd_card.refused` is the change the runner refused, and
  `sd_card.not_reached` the changes it has not applied yet, both as `CardChange(t, image)` too;
  `sd_card.image_path` is the card as the test last put it. A change made just before the test
  ends, without a wait after it, never reaches the run. The runner refuses a card it cannot
  open, as when the image was deleted after `insert`; the run then fails with
  `sd_card: the runner refused the card at T ns: PATH: ERROR`.
- **Reading a card safely.** `CardImage.read_bytes`, `read_text` and `list_files` read the image
  as it is on the host when they are called, whether the card is in or out. While the test is
  not waiting the firmware does not run, so the image does not change under a read; but it holds
  only what the firmware has written. Wait until the firmware has closed its file, by its console
  or a line it drives, before you read; taking the card out does not finish a write.

#### Making the card fail or slow down

On `esp32sim` the test can make the card in the slot fail or slow down at block level, or only
watch what the firmware does with it, from the board's current instant on, and see what each rule
did:

```python
def test_a_failed_write_raises_the_error_pin(board: Board):
    card = board.sd_card.image(size_mb=32).build()
    board.sd_card.insert(card)
    board.power_on()
    board.wait(seconds(2.5))  # the firmware has mounted the card and written to it
    fault = board.sd_card.fail("write", count=1)
    board.wait_until(board.console.line("SD card write failed"), within=seconds(1))
    (write,) = fault.hits
    assert not write.written  # the card left the block as it was
    board.wait(seconds(1))
    assert not fault.active  # spent: the next write goes through
```

- `sd_card.fail(op, *, count=None, blocks=None)`, `op` `"read"` or `"write"`: a read gets the
  data error token "card ECC failed" instead of its data, a write the data response "write error",
  and its block on the image stays as it was. ESP-IDF's driver returns `ESP_FAIL` for the write at
  once. For the read it returns `ESP_ERR_TIMEOUT` 1 s later on the board's clock: it looks for the
  start token alone in the first bytes after a command, misses the error token, and polls until
  its read timeout.
- `sd_card.delay(op, by, *, count=None, blocks=None)`, `op` `"read"`, `"write"` or `"erase"`, `by`
  a `Duration` from 1 ns to 10 s: a read's data comes `by` after the read starts, and after a
  write or an erase the card is busy for `by`, hearing nothing meanwhile. The time is the board's:
  firmware that polls the card sees it pass, and gives up when its own timeout does. ESP-IDF waits
  1 s for a read's data and 5 s for a busy write, then returns `ESP_ERR_TIMEOUT`. A delayed write
  is in the image when it starts, also when the firmware gives up on it.
- `sd_card.observe(op, *, count=None, blocks=None)`, `op` `"read"`, `"write"` or `"erase"`:
  changes nothing. The firmware sees the same card, the same answers at the same times and the
  same image as without the rule, and the rule's `hits` list the operations it met, with their
  blocks, their time and whether the card wrote them:

  ```python
  writes = board.sd_card.observe("write")
  board.power_on()
  board.wait_until(board.console.line("log closed"), within=seconds(5))
  assert writes.hits, "the firmware wrote to the card"
  assert all(hit.written for hit in writes.hits)
  written = sorted({hit.blocks.start for hit in writes.hits})  # the blocks the driver wrote
  ```
- `count` is how many operations the rule applies to, 1 to 2**63 - 1; `None`, the default, every
  one until it is cleared. `blocks` is a block number or a `range` of them (512-byte blocks, as the
  driver addresses the card), `None` for any; a read or a write meets a rule on its block, an
  erase one whose blocks overlap its range. Each block of a multiple block read or write counts
  as one operation, and a read that fails sends nothing more. A multiple block read reaches only
  the blocks the firmware's driver clocks on for: ESP-IDF's sends its stop command right after the
  last block it wants, so a rule on the next block waits for a read of that block.
- Of the rules that fail or delay an operation matches, the one given last applies:
  `fail("write", count=1)` after `delay("write", ms(300))` fails the next write, and the writes
  after it are slow again. Every rule that observes an operation matches meets it as well, given
  before or after the others, and each gets its own hit, counted against its own `count`: an
  `observe("write")` given after a `fail("write")` does not stop the failure, and its hit says
  `written` false. An
  operation under way when a rule is given or cleared keeps what it met when it started. Rules stay
  through `board.power_off()`, `board.reset()` and a change of card: a rule lasts until it is spent
  or cleared, so clear it when the next card should be a good one. An operation under way ends with
  the power and with the card, and goes on through a reset of the chip alone.
- All three return a `CardFault`: `hits`, the operations it met so far; `active`, whether it
  still applies (not cleared, with operations left); `clear()`, which removes it from the board's
  current instant on and does nothing to a rule already spent or cleared; and `id`, `op`, `action`
  (`"error"`, `"delay"` or `"observe"`), `delay` (`None` but for a delay), `count`, `blocks`, `at`
  (when it was given) and `cleared_at`. `sd_card.clear_faults()` removes every rule,
  `sd_card.faults` lists them in order, and `sd_card.faulted` lists what all of them did: an
  operation two rules met is there twice, once for each, in the order the test gave them.
- Each operation is a `FaultedOperation`: `at`, its start on the board's clock (a read at its
  command, or for the next block of a multiple block read when the driver clocks on for it; a
  write at its data response; an erase at its command); `fault`, the rule; `op`; `blocks`, a
  `range`; `action` and `delay`; and `written`, whether the card wrote the image: never for a
  read or a failed write, always for a write or erase it carried out, delayed or observed.
- A rule is given before power-on, from the first instruction, or while the board is on; a rule
  given before the firmware has initialised the card can fail the mount. `fail`, `delay` and
  `observe` refuse, at the call: an empty slot, as the test last left it (`SourceError`:
  `sd_card.fail: the slot is empty: insert a card first`), an `op` the method does not take, a
  delay that is not a `Duration` or outside 1 ns to 10 s
  (`sd_card.delay: a delay of 10.5 s; one is 1 ns to 10 s`), a `count` out of range, a run that
  has ended (`RunEnded`: `sd_card.fail: the run has ended: no rule reaches the card any more`), and
  blocks the
  card does not have (`sd_card.fail: blocks range(2048, 2049), and the card has blocks 0 to 2047`)
  or a range that is empty or steps.
- Read the image as above, between the firmware's writes: it holds every block the card
  accepted, delayed ones included, and none of a failed write, so it is never a block written in
  part. What a failed write does to the file system is the firmware's: in
  `examples/sd_data_logger`, a row whose data block fails leaves the file as it was before the
  row, but a firmware that writes on after an error can leave a file system that mtools reads
  differently, or refuses (`SourceError` naming the command). A
  modeled block error is never on the image itself: read again, the block is what the card last
  accepted.
- The report of the run lists each rule under `sd_card:`, with what it did
  (`fault 1: sd_card.fail('write', count=1) at 2.5 s; 1 operation`, then
  `3.0681844 s  write of block 541 failed, not written`; for a rule that observes,
  `3.0681844 s  write of block 541 observed, written`), and `fail`, `delay`, `observe`, `clear`
  and `clear_faults` are steps of the run. The `esp32sim_link` property's `sd_card` has `faults`,
  one entry per rule with its operations.

`board.uart(role)` is the same for any `uart.<role>` route of the profile: `gnss` is
`board.uart("gnss")`. It takes `replay(path)`, `real(port)` and `tap(path)` as `gnss` does, and
`model(source)`, a model the test wrote ([a UART's own model](#a-uarts-own-model)).

`lines` is every GPIO line of the board profile's `[lines.<role>]` sets, wires to the board's
other parts; the console's lines of text, `console.line` and `console.lines()` above, are another
thing. On the `lilygo-t-a7670g-r2` profile, `modem.pwrkey` on GPIO 4 and `modem.ri` on GPIO 33,
which the board pulls down ([board profiles](board-profiles-user.md#the-profiles-esp32tl-ships)).
Shortened from
`tests/test_gpio_echo.py`, where `gpio_echo` drives PWRKEY to the level it reads on RI
([validation firmware](validation-firmware-user.md)):

```python
from esp32tl.units import seconds


@pytest.mark.board_profile("lilygo-t-a7670g-r2")
@pytest.mark.app_path("../firmware/gpio_echo")
def test_the_firmware_follows_ri_and_drives_pwrkey(board):
    board.power_on()
    board.console.expect_exact("in=0")
    board.wait(seconds(1))
    board.lines.drive("modem.ri", 1)
    board.console.expect_exact("in=1", timeout=1)
    assert board.wait_until(lambda: board.lines.level("modem.pwrkey") == 1, within=seconds(1))
```

- A line is named as `<role>.<line>`, `"modem.ri"`, or by its GPIO number, `33`.
- `lines.drive(line, level)` drives the line to `level`, 0 or 1 (`True` and `False` count), from
  the board's current instant, `board.now`, until the test drives it again or releases it. Called
  before `board.power_on()`, the level holds from power-on.
- `lines.release(line)` stops driving the line, from the board's current instant. A line the
  profile pulls, as `ri = { pin = 33, pull = "down" }`, goes back to its pull; any other to what
  the chip makes of an undriven pad: in esp32sim the chip's own pull, if the firmware set one, else
  1. GPIO 34 to 39 have no pulls on the chip, and a pad on the RTC mux takes none from IO_MUX, so
  those read 1.
- The board's pulls hold from power-on in every test on the profile, whether it uses `lines` or
  not, so RI reads 0 with no modem at the far end. A pull is weaker than the firmware's own
  output: firmware that drives a pulled line wins over the pull. The harness puts a pull on the
  line as a level it drives, so the release of a pulled line that the firmware drives as an output
  is reported as an `input_on_output` warning in [the link's
  report](esp32sim-backend-user.md#the-links-report), as a drive of it is.
- `lines.level(line)` is what the firmware drives the line to, 0 or 1, or `None` while its output
  is off; it is a condition for `board.wait_until`, as above. A change on a line takes no turn of
  the link of its own, so `wait_until` sees it at its next look, within 100 ms of the board's
  time; `lines.events()` has the exact time. A line counts as driven while the firmware's output
  on it is enabled, or while its pad is high: a peripheral that drives the pad through the GPIO
  matrix with its own output enable shows as 1 when high and `None` when low.
- `lines.events()` is every change on the lines so far, by either end, in the order of the board's
  time, as a list of `LineEvent(t, name, pin, by, level)`: `t` a `Duration`, an instant of the
  board's time as `board.now` is; `name` as `modem.ri`; `by` `"firmware"` or `"test"`; `level` 0 or
  1, or `None` for a line no longer driven. A drive to the level the test drives the line to
  already, or a release of a line the test does not drive, is no change, and sends nothing. A drive
  after the run has ended is still listed, though it reaches no board.
- `lines.next_change(line, level="any", *, by="firmware")` is a condition for the line's next
  change after the call ([the next change of a line](#the-next-change-of-a-line)).
- A line that is not one of the profile's raises `ValueError`:
  `GPIO 5 is no line of the board profile lilygo-t-a7670g-r2; its lines are modem.pwrkey (GPIO 4), modem.ri (GPIO 33)`.
  A level other than 0 or 1 raises `ValueError: lines.drive: level 2 is not 0 or 1`, or
  `TypeError` with the same message for one that is not an integer.
- On a board profile without lines, `board.lines` raises `BackendError` where the test body
  reaches it, and the `lines` fixture is an error at setup
  ([where it fails](#one-board-and-its-interfaces)), with the message after `esp32sim: `:
  `esp32sim: the board profile esp32-devkitc has no lines; its interfaces are console, uart.gnss, sd_card, wifi, bluetooth`.

`NeverHappened`, `RunEnded`, `BoardOff`, `SourceError` and `LineEvent` are imported from `esp32tl`.

### Waking from deep sleep

A firmware that goes to deep sleep with a timer wake (`esp_sleep_enable_timer_wakeup()`, then
`esp_deep_sleep_start()`) wakes on the board's clock, through a boot of its own with cause 5,
`DEEPSLEEP_RESET` (`esp32tl.DEEPSLEEP_RESET`). `board.wait_for_wake` runs the
board through the sleep to that boot:

```python
from esp32tl import Board
from esp32tl.units import seconds


def test_reports_after_its_sleep(board: Board):
    board.power_on()
    asleep = board.wait_until(board.console.line("^Entering deep sleep$"), within=seconds(2))
    wake = board.wait_for_wake(within=seconds(21))
    assert wake.at - asleep.at >= seconds(20)
    board.console.expect(r"Time spent in deep sleep: 2\d{4}ms", timeout=1)
```

- `board.wait_for_wake(within=duration)` lets the board run until the chip boots again, for at
  most `within` of the board's time, and returns that boot, the new `board.boot`. The board's
  clock then stands at most 1 ms after the boot's `at`: what the firmware prints after its wake is
  still to come, and the test expects it on the console as usual.
- It takes no 100 ms steps, as `board.wait_until` does: nothing happens in a deep sleep, so the
  board runs to the wake in one stretch, and an hour's sleep costs no more of the test's turns
  than a second's, and on esp32sim next to no more wall time, with the RTC watchdog off: a day of
  15-minute sleeps takes about 7 s, the boots' time
  ([periodic wake example](examples-user.md#periodic-wake)); with it on, the sleep is not stepped
  through, and the watchdog counts through it
  ([esp32sim backend](esp32sim-backend-user.md#how-a-run-goes)). It works
  the same when the firmware is still awake at the call: the wait goes on through whatever comes
  before the sleep, with a turn of the link at most 1 ms after each event on it (a level on a
  line of the profile, a byte on a watched UART, a frame of the station), where `wait_until`
  takes at least one every 100 ms. The console's bytes bring no turn. When the firmware is busy
  before its sleep, wait for the sleep's last line first, then for the wake.
- A boot through another reset, `esp_restart()` or a watchdog, fails the test with an
  `AssertionError`:
  `board.wait_for_wake: boot 2 at 11.0429706 s is SW_CPU_RESET (reset cause 12), not a wake from deep sleep (DEEPSLEEP_RESET)`.
- A chip that does not boot within `within` raises `NeverHappened`:
  `a wake from deep sleep did not happen within 21 s: waited from 1.2 s to 22.2 s of the board's time`.
  A run that ends first raises `RunEnded`, as one does that reaches its limit, a day of the
  board's time: a chip that never wakes fails the test rather than passing it in silence.
- Before the first `power_on()` it raises
  `BoardOff: the board is off; call board.power_on() first`, and after `power_off()`
  `BoardOff: the board is off: board.wait_for_wake() waits for a board that is on`.
- ESP-IDF's `system/deep_sleep` arms EXT1 on two pins and two touch pads beside its timer, and on
  esp32sim only the timer wakes it ([`deep_sleep`](validation-firmware-user.md#deep_sleep)).
  TODO(owner): whether esp32sim wakes the chip on a GPIO or a touch pad at all is not checked.

### Named output conditions

`lines.output(line, active=1)` returns a read-only `OutputLine` for a profile line, named by
its role or GPIO number. It observes what the firmware drives; it neither configures the chip
nor drives the input. The default is high means on. Pass `active=0` for an active-low output;
this is explicit test-side polarity, independent of the profile's model `active` field.

```python
buzzer = lines.output("alarm.buzzer")  # on the gps-alarm-clock profile
board.power_on()
board.wait_until(buzzer.is_on, within=seconds(2))
buzzer.assert_on()
```

`is_on` and `is_off` are callable conditions, like `console.line(...)`. Pass the condition
without parentheses to `board.wait_until`, or call it to check the current state without
advancing time. Both return false while the firmware leaves the output undriven: unknown is
not off. A condition reads the current level every time; it does not remember an earlier match.
`assert buzzer.is_on` without `()` raises `TypeError: call the output condition with (), or
pass it to board.wait_until`, rather than silently passing on the condition object's truthiness.

The wait's log and `NeverHappened` message name the line, state, pin and level, for example
`alarm.buzzer is on (GPIO 18, level 1) did not happen within 2 s`, followed by the time window.
`buzzer.events()` returns only that line's events, retaining the same `LineEvent` fields and
all three origins (`firmware`, `test`, `model`) as `lines.events()`. It can catch short pulses
between waits. The handle also exposes `name`, `pin` and `active`.

Use `buzzer.assert_on()` and `buzzer.assert_off()` for assertions about the current state.
Failures name the line, GPIO, expected level and observed level (or `undriven/unknown`).
They do not advance time. To check an entire interval, save its starting instant:

```python
board.wait_until(buzzer.is_off, within=seconds(2))
off_since = board.now
board.wait(seconds(3))
buzzer.assert_stayed_off(since=off_since)
```

`assert_stayed_on(since=...)` and `assert_stayed_off(since=...)` inspect recorded firmware
transitions through `board.now`, including pulses between waits. The starting state is the
last firmware event at or before `since`; every subsequent transition up to and including
`board.now` must match. Unknown or undriven states fail, including an unknown starting state.
Events from test/model input writes do not count. These assertions neither wait nor drive GPIO.
For example, a recovered pulse can still fail with
`alarm.buzzer expected off (GPIO 18, level 0) at 2 s during [1, 4] s; observed level 1`.
`since` must be a `Duration` on the board's clock between zero and `board.now` (inclusive).
A wrong type raises `TypeError: since must be a Duration on the board's clock`; an out-of-range
instant raises `ValueError: since must be between zero and board.now`.

Names are checked when the handle is created, with the same errors as `lines.level`.
An invalid polarity raises `ValueError: lines.output: active 2 is not 0 or 1`, or
`TypeError: lines.output: active '1' is not 0 or 1` for a non-integer; booleans count as 0/1,
as in `lines.drive`. Existing `drive`, `release`, `level` and `events` calls remain available.

### The next change of a line

`is_on` holds as long as the level does, so a wait on it returns at once while PWRKEY is still
held. `next_change` waits for the line's next change instead, as for "the firmware's next press of
this pin". Shortened from `tests/test_modem_recovery.py`, where `modem_recovery` presses PWRKEY
twice to bring a hung modem back
([validation firmware](validation-firmware-user.md#modem_recovery)):

```python
key = lines.output("modem.pwrkey")
modem.hang(pwrkey=True)
first = board.wait_until(key.next_change(on=True), within=seconds(120))
second = board.wait_until(key.next_change(on=True), within=seconds(30))
assert second.t - first.t > seconds(10)
```

- `output.next_change(*, on=None, by="firmware")` is a [described condition](#described-conditions)
  for the line's first change after the call: with `on=True` a change to its active level, with
  `on=False` one to the other level, and with `on=None` any change, to undriven too. It returns
  the `LineEvent` of that change, with its exact `t`, though `board.wait_until` sees it at its
  next look, within 100 ms of the board's time.
- `lines.next_change(line, level="any", *, by="firmware")` is the same by the line's name or
  GPIO number, in pin levels: `level` is 0 or 1 (`True` and `False` count), `None` for a change
  to undriven, or `"any"` for any change.
- `by` is who makes the change, as in `LineEvent.by`: `"firmware"`, the default, `"test"` or
  `"model"`, or `None` for any of them. A change the test or a model makes on the line does not
  match `by="firmware"`.
- The boundary is the line's changes when the condition is made: a change before it does not
  count, however recent, and a level that holds is no change. Polling never moves the boundary,
  and nothing is consumed: the condition returns the same event every time it is asked, so the
  second press needs a second `next_change`, made after the first one matched. Make it before
  what makes the firmware change the line.
- A timeout names the line, the level and who was to change it:
  `the next change of modem.pwrkey (GPIO 4) to on (level 1) by the firmware did not happen within 30 s`,
  followed by the time window; without `on`,
  `the next change of modem.pwrkey (GPIO 4) by the firmware`, and from `lines`,
  `the next change of modem.ri (GPIO 33) to level 0 by a model` or
  `the next change of modem.ri (GPIO 33) to undriven by the firmware`. `by=None` reads
  `by the firmware, the test or a model`.
- `bool()` of it raises `TypeError: call the output condition with (), or pass it to board.wait_until`,
  or `call the lines condition` for one from `lines.next_change`.
- A line that is not one of the profile's raises `ValueError` as `lines.level` does. Anything else
  is refused when the condition is made: `ValueError: lines.next_change: level 2 is not 0, 1, None or "any"`,
  or `TypeError` with the same message for a level that is not an integer;
  `ValueError: lines.next_change: by 'fw' is not "firmware", "test", "model" or None`, or
  `TypeError` with the same message for a `by` that is not text, and `output.next_change: …`
  from the handle; and
  `TypeError: output.next_change: on 1 is not True, False or None`.

### A UART's own model

`board.uart(role).model(source)` puts a model the test wrote at the far end of a route, for a part
esp32tl ships no model for. Shortened from `tests/test_uart_model.py`, where `modem_at` writes AT
commands to `uart.aux` of a local profile and reads the answers
([validation firmware](validation-firmware-user.md#modem_at)):

```python
from collections.abc import Iterable

from esp32tl import ReactiveModel, Timed
from esp32tl.units import Duration, ms, seconds


class AtResponder(ReactiveModel):
    """OK to every command line, answer_after its CR LF."""

    answer_after = ms(1)

    def __init__(self) -> None:
        self.lines: list[bytes] = []
        self._heard = b""

    def receive(self, at: Duration, data: bytes) -> Iterable[Timed]:
        self._heard += data
        answers = []
        while b"\r\n" in self._heard:
            line, _, self._heard = self._heard.partition(b"\r\n")
            self.lines.append(line)
            answers.append((at + self.answer_after, b"\r\nOK\r\n"))
        return answers


@pytest.mark.app_path("../firmware/modem_at")
@pytest.mark.board_profile(path="boards/aux-uart.toml")
def test_the_firmware_talks_to_the_test_s_model_on_its_own_uart(board):
    part = board.uart("aux").model(AtResponder())
    board.power_on()
    board.console.expect_exact("modem_at: dialogue complete", timeout=seconds(5))
    assert part.lines[1] == b"ATE0"
```

- `model(source)` returns `source`, so the test keeps it to read what it heard. It must come
  before `board.power_on()`, and is the route's one source, as a replay or a real device is.
- `source` is a subclass of `esp32tl.Model`, whose times are `Duration`s on the
  board's clock, or any object with the four methods of `esp32tl.Source`, whose
  times are nanoseconds as `int`s. The methods are the same four, with the same names:

  | Method | Called | Returns |
  |---|---|---|
  | `receive(at, data)` | When the firmware's bytes cross the wire. `at` is the instant the firmware wrote the first byte of `data` into the TX FIFO; each byte after came within one character time of the one before. esp32sim does not pace the transmission at the baud, so what the firmware writes in one go comes at about one instant. The bytes come cut anywhere: find a request by its terminator, or by the silence after it with `IdleFramedModel` | The answers, `(at, bytes)` pairs (a `Timed`), each stamped `latency()` or more after `at` |
  | `poll(now)` | At every turn of the link | What the part sends unprompted, each stamped `now` or later |
  | `quiet_until()` | After `poll(now)` | The next time it sends unprompted, later than `now`; `None` for nothing scheduled |
  | `latency()` | At every turn | The shortest time in which it answers; `None` when it answers nothing |

  A model that only answers starts from `ReactiveModel`, as `AtResponder` above does: it defines
  `receive` and sets `answer_after`, and the base answers the other three. One of a part that
  frames by silence, as a Modbus RTU slave, starts from `IdleFramedModel`, which gets each frame
  in `on_frame(at, frame)` ([the bases](models-user.md#a-model-that-only-answers)). A `Model`
  subclass defines all four: they are abstract, so one that lacks one, or misspells it,
  cannot be made (`TypeError: Can't instantiate abstract class Part without an
  implementation for abstract method 'poll'`, as Python 3.12 on words it), rather than be a model
  that silently says nothing. A source in nanoseconds has the same four, takes `t: int` and
  `now: int`, and returns `(t, bytes)` pairs with `t` an `int`, an `Output` of
  `esp32tl.link.boardlink`; the run is the same, to the nanosecond and the byte. Prefer it for a
  source that exists already, for one that is itself a proxy of another (a `Model` behind a proxy
  is not a `Model`, and is called in nanoseconds), and for one whose own cost matters: a `Model`
  costs a `Duration` for every instant it is given and every one it returns, and a check of each.
  Everywhere else, a `Model` keeps the unit arithmetic out of the test.

  The board runs ahead of the model by up to `latency()` and up to `quiet_until()`: an answer
  stamped sooner than its latency, or something sent before the `quiet_until()` it gave, ends the
  run, with the link's error in the table below. A model that answers nothing returns `[]` from
  `receive`, and `None` from `latency()`.
- A model with a fifth method, `supply(on)`, of either kind, is a part on the board's supply, as
  the GNSS receiver and the modem are: `board.power_off()` calls `supply(False)`, and the
  `power_on()` after it `supply(True)`, at the board's time. It starts with power: the first
  `power_on()` calls nothing. A model without it keeps going through a power cycle; what it sends
  while the chip is off is lost.
- The model is called from the link's thread, inside the test's waits, while the runner waits for
  its answer: it must not block, and must not wait on the wall clock.
- What it refuses, at the call: an object without one of the methods, or with one that cannot be
  called, with
  `TypeError: uart.aux.model: <...> is not a Source: it has no poll, quiet_until, latency`; a
  second source, `SourceError: uart.aux has its source already: one per test`; and a model after
  power-on, `SourceError: uart.aux: the board is on already: give an interface its source before
  board.power_on()`. On `uart.gnss` or `uart.modem` it goes in place of esp32tl's model, whose
  calls then raise `SourceError` (`uart.gnss has a model of the test's: it has no receiver to
  describe; one source per test`, `uart.modem has a model of the test's: it has no modem to
  describe; one source per test`). The
  `serial` backend skips the test at the call: `serial: uart.aux takes no model source on this
  backend, only real`.
- What the model does wrong during the run ends the run, as a link error: a test that waits gets
  `RunEnded`, which names it, and the teardown `the link failed: ...`. The run's log has what
  crossed the wire until then. The checks are the same for both kinds; a `Model` is told in
  `Duration`s, as `str` writes them (`0.0020005 s`), a source in nanoseconds in `int`s:

  | The model | A `Model`'s error | A source in nanoseconds' error |
  |---|---|---|
  | Raises in a method | `uart.aux.receive raised ValueError: ...`, and so for `poll`, `quiet_until` and `latency` | The same |
  | Is a `Model` behind a proxy, which is called in nanoseconds | — | `uart.aux.receive raised TypeError: unsupported operand type(s) for +: 'int' and 'Duration'`, or whatever the model's arithmetic on an `int` raises: give the route the `Model` itself, or a proxy whose `__class__` is the model's, which `isinstance` sees |
  | Returns from `receive` or `poll` something that is not a `(t, bytes)` pair | `uart.aux.receive returned (2000500, b'OK'), not a (Duration, bytes) pair` | `uart.aux.receive returned [1, 2], not a (t_ns, bytes) pair`, `t` not an `int` |
  | Stamps an answer sooner than its `latency()`, or sends before the `quiet_until()` it gave | `uart.aux sent bytes stamped 0.0020005 s in the turn at 0.0030005 s: it answered sooner than its latency, or sent past its quiet_until` | `uart.aux sent bytes stamped {t} ns in the turn at {now} ns: ...`, the rest the same |
  | Returns from `quiet_until()` what is not of its kind | `uart.aux.quiet_until returned 1.5, not a Duration` | `uart.aux.quiet_until returned 1.5, not nanoseconds` |
  | Returns from `quiet_until()` a time not after the turn | `uart.aux is quiet until 0.002 s, not after the turn at 0.002 s: what it sends by then belongs in this turn` | `uart.aux is quiet until {t} ns, not after the turn at {now} ns: ...`, the rest the same |
  | Returns from `latency()` what is not of its kind, or below 0 | `uart.aux declares a latency of 1000000, not a Duration`; `uart.aux declares a latency of -0.001 s` | `uart.aux declares a latency of -1 ns`, for anything but an `int` of 0 or more |

- The report names it by its class, `uart.aux: the model of AtResponder; 156 bytes in, 233 out`,
  the run's log has the step `uart.aux.model(AtResponder)`, and a tap of the route records it as
  `model:AtResponder` ([taps](run-report-user.md#what-a-tap-writes)).

### The modem fixture

`modem` is the profile's `uart.modem`, available on `lilygo-t-a7670g-r2`. Select its model with
`modem.signal(dbm=-73)` or another model call before `board.power_on()`, or select a recording with
`modem.replay(path)`. `modem.tap(path)` records both directions. A selected model responds to AT,
reads PWRKEY and drives RI through `lines`; the test can inspect all GPIO events, but cannot drive
or release the model's RI. Model changes use `by="model"` in `lines.events()`; `level()` continues
to read only the firmware side. The model adds a 1 ms response latency to the lines interface.

See [the modem guide](modem-user.md) for signal, registration, power, clock and operator, a hung
modem and RESET, command history and limits.
There is no default modem route on `esp32-devkitc`: `board.modem` there raises
`BackendError: the board profile esp32-devkitc has no uart.modem; its interfaces are console, uart.gnss, sd_card, wifi, bluetooth`
where the test body reaches it, and the `modem` fixture is an error at setup with the same message
after `esp32sim: ` ([where it fails](#one-board-and-its-interfaces)).

### The wifi fixture

`wifi` is the chip's Wi-Fi air, which every board profile has: every chip a profile may name has
the radio. `access_point(...)` gives the device a network to join; `station(...)` joins the device's
own SoftAP ([below](#a-virtual-station-joins-the-devices-softap)). Until the test configures one,
there is no virtual peer on the air. From `tests/test_http_request.py`, where ESP-IDF's unmodified `http_request`
example joins with ESP-IDF's default SSID and passphrase and fetches a page
([validation firmware](validation-firmware-user.md)):

```python
from esp32tl.units import seconds

pytestmark = pytest.mark.app_path("../firmware/http_request")


def test_joins_and_fetches_from_the_tests_server(board):
    network = board.wifi.access_point("myssid", password="mypassword")
    network.http.serve("/", "hello from the test\n")
    board.power_on()
    request = board.wait_until(network.http.next_request("/"), within=seconds(30))
    assert request.header("Host") == "example.com:80"
```

- `wifi.access_point(ssid, *, password=None, channel=6, bssid=None)`, before `board.power_on()`,
  puts an access point on the air from power-on: open without a password, WPA2-PSK with one, on
  `channel` 1 to 14, at `bssid` (`02:53:49:4d:00:01`, esp32sim's, unless given). It returns the
  simulated network behind it, an `Internet`: a DHCP server gives the station `10.64.0.2`, and DNS,
  NTP, the MQTT broker on 1883 and an HTTP server on 80 answer, all on the board's clock
  ([the network](network-user.md)). Once per test; `wifi.network` returns the same network later.
- `wifi.nearby(ssid, *, channel=1, rssi=-60, secure=True, bssid=None)`, before `board.power_on()`
  and after `access_point`, adds a network around the station: a scan lists it on `channel` at
  `rssi` dBm, WPA2-PSK when `secure`, and nothing can join it. At most 32.
- `wifi.access_point_off()` takes the access point, and the networks around, off the air from the
  board's current instant, as one that shuts down: a station that has joined gets a
  deauthentication with reason 3, "leaving" (ESP-IDF's examples print `Wi-Fi disconnected 3`).
  The network behind it loses the TCP connections the test opened and the replies it had
  queued; the device's connections stay open at the network's end until their service gives up,
  the broker at its keepalive ([the Will](network-user.md#a-lost-link-and-the-will)).
  `wifi.access_point_on()` puts a fresh access point back on the air from the board's current
  instant, and the station must join it again, DHCP included. A switch to the state the access
  point is in already does nothing. Both after `board.power_on()`, or before it to start off the
  air ([the runner](runner-user.md)).
- `wifi.on_air`, `wifi.configuration` (the `AccessPoint` given, or `None`), `wifi.around` (the
  `Nearby` networks) and `wifi.events` (each switch, an `AirEvent(t, on)` with `t` in nanoseconds
  of the board's time) describe what the test set up.
- The parameters are checked when called: `ValueError` for an SSID that is not 1 to 32 bytes
  (`wifi.access_point: an SSID has 1 to 32 bytes, '' has 0`), a WPA2 password that is not 8 to 63
  bytes (`wifi.access_point: a WPA2 password has 8 to 63 bytes`), a comma in either
  (`a comma cannot reach esp32sim's access point`), a channel outside 1 to 14, a BSSID that is not
  six hexadecimal octets, and a network around with the access point's SSID or BSSID
  (`wifi.nearby: 'myssid' would be the access point`). `SourceError` for a second access point
  (`wifi has its access point already: one per test`), `nearby` before `access_point`
  (`wifi.nearby: the networks around need wifi.access_point() first`), a switch or `wifi.network`
  without an access point (`wifi has no access point: give it with wifi.access_point()`), and
  `access_point` or `nearby` after `board.power_on()`
  (`wifi: the board is on already: give an interface its source before board.power_on()`).
- On [serial](serial-backend-user.md) the access points are whatever is around the real board:
  `access_point` and `nearby` skip with `serial: wifi takes no model source on this backend, only real`.
- The report of the run has `wifi: access point 'myssid', channel 6, WPA2-PSK`, a line per network
  around, then the network's events; the `esp32sim_link` property has `wifi`
  ([the esp32sim backend](esp32sim-backend-user.md#the-links-report)).

### The bt fixture

`board.bluetooth`, also the `bt` fixture, is the chip's Bluetooth, which every board profile has, as
every chip a profile may name does. On the classic ESP32 the peers are Bluetooth Classic devices,
`SppPeer` and `BluetoothDevice`; on a chip whose controller is LE only (the ESP32-S3 and the C3, the
C6 once esp32sim takes its images) the firmware's controller is LE only too, of the profile's chip,
and the peers are `BlePeripheral` and `BleCentral`, with their GATT databases, advertising and
pairing ([Bluetooth LE](bluetooth-peer-user.md#bluetooth-le-bleperipheral-and-blecentral)). On
esp32sim it puts the firmware's controller on an air around the board, on the board's clock, where
the test puts the devices the firmware talks to or finds, and the run forwards the firmware's HCI to
that controller:

```python
from esp32tl import SppPeer
from esp32tl.units import seconds


def test_the_firmware_opens_spp(board):
    peer = SppPeer(board.bluetooth.air, name="ESP_SPP_ACCEPTOR", pin="1234", source=model)
    board.power_on()
    board.wait_until(lambda: peer.channel_open, within=seconds(20))
```

- `board.bluetooth` is asked for the first time before `board.power_on()`, as the `bt` fixture is:
  the runner then gets `--bt` and the application's ELF. Asked for the first time after it, it
  raises `SourceError: bluetooth: the board is on already: give an interface its source before
  board.power_on()`.
- A build with no ELF of the application: `board.bluetooth` raises `BackendError: bluetooth: BUILD
  holds no ELF of the application, whose symbols the runner's --bt needs` where the test body
  reaches it, and the `bt` fixture is an error at setup with the same message after `esp32sim: `
  ([where it fails](#one-board-and-its-interfaces)).
- Time on `board.bluetooth.air` is the board's: the test waits through the board, and what it does
  on the air, making a device, switching one off, setting its `rssi`, happens at the board's
  current instant. Its `wait`, `advance` and `run` raise `AirError`.
- The firmware's controller answers its host 100 µs of board time after the instant the packet it
  answers was sent. The host reads the answer at a later poll of the VHCI: its controller task
  hands it at most one packet a poll, and after a packet the next poll returns nothing, so an
  answer waits behind those before it ([protocol 5.22](../protocol.md#522-hci_tx-and-hci_rx-ext-34)).
- Bluetooth histories use `Duration` on the board's clock: `peer.events` contains
  `ChannelEvent(at, direction, data)` named tuples, `peer.refused` contains `Refusal(at, why)`,
  `peer.pairings` and `bt.pairings()` contain frozen `Pairing` records with `t: Duration`, and
  `bt.unknown_commands()` contains `UnknownCommand(t, op_code, name)` named tuples with
  `t: Duration`. These times compare directly with GPIO `LineEvent.t` and MQTT `Message.at`.
  A peer on standalone `Air()` instead uses that air's clock starting at creation.
  The report and integer `t_ns` fields in `esp32sim_link` JSON are unchanged.
- On [serial](serial-backend-user.md) the test skips where it first reaches `board.bluetooth`, at
  setup for the `bt` fixture: `serial: bluetooth takes no model source on this backend`.
- `board.bluetooth.tap(path)` before `board.power_on()` records the HCI between the firmware's host
  and its controller, every packet both ways, and writes it at teardown as a btsnoop file, with its
  header in `path` with `.json` added ([tapping the HCI](run-report-user.md#tapping-the-hci)).
- What the devices are, what the firmware's controller does and what the report says are in
  [Bluetooth peer](bluetooth-peer-user.md#in-front-of-the-firmware-boardbluetooth).

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

## Tapping the console

`console.tap(path)` before `board.power_on()` records the console on either backend. It returns
a `Tap` and writes at teardown, with paths relative to the test file, one tap per console, and the
same file collision checks as `gnss.tap`. Its clock is `emulated-fifo-write` on esp32sim, `wall` in
a run with a [real source](real-source-user.md), and `wall` on serial, stamped after reader polls with polling, USB, driver and scheduling delays.
See [taps](run-report-user.md).

## Backends and test ids

A test that uses `board`, directly or through another fixture, runs once on the selected backend,
with its name in the id: `test_hello_world[esp32sim]` or `test_hello_world[serial]`. Tests without
`board` in their fixture dependencies are not parametrised.

`--esp32tl-backend NAME` chooses `esp32sim` or `serial`. Serial also requires an exact `--port`
(or an indirectly parametrised `port` ); missing, blank or wildcard ports are refused before
setup. The default is **esp32sim only**; serial requires explicit selection so ordinary test
runs never claim a USB board. [esp32sim](esp32sim-backend-user.md) runs on emulated time;
[serial](serial-backend-user.md) uses pytest-embedded's `esp,idf` services, `--port`, autoflash
and
`--skip-autoflash y`, and wall time. An unknown backend is a usage error (exit code 4):
`--esp32tl-backend: invalid choice: 'qemu'`.

`@pytest.mark.app_recording(path)` optionally checks a reference recording's `app_sha256` against
the artifact before serial opens its port, including with skip-autoflash. It takes one path,
relative to the test file or absolute, and is ignored on esp32sim. See
[builds and flashing](serial-backend-user.md#builds-and-flashing) for its guarantees and errors.

What is at the far end of an interface is what the test does with it before power-on, as
`gnss.replay(path)`, and so is whether it is tapped, as `gnss.tap(path)`; a backend that cannot
give that source or tap skips the test at the call, with the reason, and one that no backend gives
is an error there. Both are described in
[board profiles](board-profiles-user.md#saying-what-a-test-needs).

## Options esp32tl refuses

esp32tl chooses the backend, the app and the build of every test that uses `board`, so when at
least one selected test uses it, these options stop the run before any test, with exit code 4 and
one line:

| Option | Message |
|---|---|
| `--embedded-services`, `--app-path`, `--build-dir` | ``ERROR: --{option} is set: esp32tl chooses it for every test that uses `board` (the backend through --esp32tl-backend, the app through the app_path marker or esp32tl_app_path, the build through that marker's build_dir keyword, esp32tl_build_dir or the app's build/ directory)`` |
| `--count` with any value but 1 | `ERROR: --count is set: esp32tl runs one board in a test` |
| An `app_path` or `board_profile` marker in a `pytest.param(..., marks=...)`, or added by a hook | `ERROR: {test id}: the {marker} marker reached the test after esp32tl read it, from a pytest.param or a hook: put it on the test, its class or its module` |

A test is selected when `-k` and `-m` leave it in the run: `-k test_two --app-path elsewhere`
passes when `test_two` does not use `board`. In that case the options go to pytest-embedded as
usual.

A test that parametrises pytest-embedded's `count` itself above 1, as in
`@pytest.mark.parametrize("count", [2], indirect=True)`, gets an error at setup:
`count is above 1: esp32tl runs one board in a test`.

## What a failure looks like

- The backend cannot give a source or a tap the test asks for: the test is skipped on it, at the
  call ([board profiles](board-profiles-user.md#when-a-backend-cannot-give-it)). Serial skips
  models, replays and taps on UART peripherals, and SD-card models.
- The backend cannot start (no runner, no ROM ELF, no usable build): an error at setup, the test
  body does not run, and the message starts with the backend name, `esp32sim: ` or `serial: `.
  See [esp32sim](esp32sim-backend-user.md#when-the-backend-cannot-start) and
  [serial](serial-backend-technical.md#failure-modes) for their setup errors.
- A source or a tap given after `board.power_on()`, or a second one on the same route, or a card
  for a slot that holds one: the test fails with `SourceError`; an object given to
  `board.uart(role).model` that is not a `Source` with `TypeError`; a recording that cannot be replayed fails it with `RecordingError` or
  `ReplayError` ([replay](replay-user.md#what-a-replay-refuses)), a tap into a directory that
  does not exist with `FileNotFoundError` ([taps](run-report-user.md#when-the-test-gets-it-wrong)),
  and the receiver model told what it cannot be with `ModelError`
  ([models and units](models-user.md#when-the-test-gets-it-wrong)).
- A test that set up a source or a tap, or drove a line, and passed without calling
  `board.power_on()` gets an error at teardown, since it ran no firmware:
  `esp32sim: the test set up the replay of uart.gnss and never called board.power_on(): it ran no firmware`,
  `the model of uart.gnss` for a receiver, `the level of modem.ri` for a line, or
  `a card in sd_card` for a card.
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
([the link's report](esp32sim-backend-user.md#the-links-report)). Its first line names the build
the test loaded, `build: {directory} ({kind} for {chip}); app_sha256 {sha256}`, so that a build
directory other than the intended one shows
([the run on the board's clock](run-report-user.md#the-run-on-the-boards-clock)). For a test that
failed, the section ends with the run on the board's clock: what the test did, what the firmware
printed and the bytes on every route, in the order they happened
([the run on the board's clock](run-report-user.md#the-run-on-the-boards-clock)).

esp32tl automatically filters the exact warning
`PytestExperimentalApiWarning: record_xml_attribute is an experimental feature`, caused by
pytest-embedded's `app_path` fixture for every test. No consumer configuration is needed.
Other experimental API warnings remain visible. This also silences the same warning from your
own use of `record_xml_attribute`: pytest emits it when setting up the shared fixture and does
not distinguish its callers. Your `filterwarnings` ini settings, `-W` options and
`@pytest.mark.filterwarnings` take precedence; for example, `filterwarnings = ["error"]` or
`pytest -W error` turns it into an error again.

Revisit this filter if pytest-embedded stops requesting the fixture or pytest makes the API
stable ([decision](../decisions/harness.md#filter-the-xml-attribute-warning-as-an-overridable-default)).

## Names the plugin takes

- **`board`, `console`, `gnss`, `lines`, `sd_card`, `card_cache`, `wifi`**: the plugin's fixtures; a project's own fixture of one of these
  names overrides it, as pytest has it.
- **`board`**: esp32tl parametrises every test that asks for a fixture of that name, matched by
  name. A project's own `board` fixture, in a session where esp32tl is installed, gets the
  backend's id (`test_mine[esp32sim]`) and the plugin's parameters as well.
- **The `app_path` and `board_profile` markers**: esp32tl registers them and reads them on every
  test that uses `board`. A project's own marker of one of these names means esp32tl's there.
- **The `esp32tl_card_cache_mib` ini option**: esp32tl's, for [`card_cache`](#keeping-a-prepared-card).
- **The `esp32tl_app_path`, `esp32tl_build_dir`, `esp32tl_board_profile` and
  `esp32tl_board_profile_path` ini options**: esp32tl's, for [project defaults](#project-defaults).
- **`pytest_runtest_setup`**: esp32tl's runs first and numbers the TLS certificate authorities
  from 0 again, skipping the numbers of CAs still held, so that a test's own CAs have the same
  keys alone as in the suite ([TLS](network-user.md#dns-time-and-services)).
- **The `timeout` marker**: pytest-timeout's; esp32tl adds it to a test that uses `board` and has
  none, unless the session sets a timeout.
- **`app_path`, `build_dir` and `embedded_services`**: esp32tl parametrises them for a test that
  uses `board`. A test that also parametrises one of them itself is a collection error:
  `duplicate parametrization of 'app_path'`.

## See also

- [pytest plugin: how it works](pytest-plugin-technical.md)
- [esp32sim backend](esp32sim-backend-user.md)
- [Board profiles](board-profiles-user.md)
- [Replay](replay-user.md)
- [Taps and the run report](run-report-user.md)
- [Models and units](models-user.md)
- [Validation firmware](validation-firmware-user.md)
- [Testing this repository](../guides/testing.md)


### Selecting a replay protocol

`gnss.replay(path, protocol=...)` and `board.uart(role).replay(path, protocol=...)` accept a
`Protocol` from `esp32tl`: `AT`, `PMTK`, `UBX`, or `GNSS` (mixed PMTK/UBX).
Omitting it detects the protocol from recorded requests; a recording with no requests stays
passive. The returned replay exposes `sent`, `unseen_count` and `unseen`, a mapping of normalised
request bytes to occurrence counts. See [matching](matching-user.md) for timing and defaults.


## MQTT broker fixture

`broker` selects the modem model and its simulated network before power-on and returns its
MQTT 3.1.1 broker on port 1883. It requires a profile with `uart.modem`, such as
`lilygo-t-a7670g-r2`. Use `board.wait_until(broker.next_message(topic), within=seconds(...))`,
`broker.subscribed(topic)`, `broker.next_connection()` and `broker.publish(topic, payload)`.
`broker.find_message(topic)` and `broker.find_connection()` query recorded history; TLS
listeners add `next_handshake_started()` and `next_handshake_completed()`. Create a `next_*`
condition before the action that may produce its event. The
[network guide](network-user.md#history-next-events-and-current-state) defines the boundaries.
`modem.network()` gives the underlying network for DNS, NTP and TLS service configuration.
On serial, both `broker` and `modem.network()` skip because a modem model is unsupported.
See [the network guide](network-user.md) for complete examples and condition semantics.

### A virtual station joins the device's SoftAP

`wifi.station(ssid, *, password=None, mac=None)` returns the network behind a virtual station,
before `board.power_on()`, once per test. The SSID has 1 to 32 UTF-8 bytes and no comma; passwords
have 8 to 63 ASCII bytes and no comma, matching esp32sim's parser. Without a password the AP must
be open. The MAC defaults to `02:53:54:41:00:01`, must be six hexadecimal octets, unicast, and must
differ from the device's station and SoftAP MACs (`24:6f:28:00:11:22` and `24:6f:28:00:11:23`).

#### The device's MACs

A firmware that names its SoftAP after its MAC needs the device's MAC before power-on, to give
`wifi.station` the SSID. `esp32tl` names the emulated device's two Wi-Fi MACs:

```python
from esp32tl import ESP32SIM_STATION_MAC
from esp32tl.units import seconds


def test_a_phone_joins_the_setup_network(board, wifi):
    # The firmware names its SoftAP "setup-" and the last two octets of its station MAC.
    suffix = ESP32SIM_STATION_MAC.replace(":", "")[-4:].upper()
    network = wifi.station(f"setup-{suffix}")
    board.power_on()
    board.wait_until(network.when_leased(), within=seconds(20))
```

| Address | Value | Whose |
|---|---|---|
| `ESP32SIM_STATION_MAC` | `24:6f:28:00:11:22` | The emulated device's Wi-Fi station interface: the base MAC the runner gives the machine, the one esp32sim's CLI gives the classic chip |
| `ESP32SIM_SOFTAP_MAC` | `24:6f:28:00:11:23` | The emulated device's SoftAP interface: the base MAC plus one, as ESP-IDF derives it on the classic ESP32 (four universal MACs: station, SoftAP, Bluetooth, Ethernet) |
| `wifi.station(mac=...)` | `02:53:54:41:00:01` unless given | The harness's virtual station, the client that joins the device's SoftAP |
| `wifi.access_point(bssid=...)` | `02:53:49:4d:00:01` unless given | The harness's virtual access point, which the device's station joins |

The harness's own two addresses have no constant: a test that checks either names it, as
`wifi.station(mac=...)` or `wifi.access_point(bssid=...)`, so that what it checks is in the test.

The two `ESP32SIM_*` constants are the emulated classic ESP32's on esp32sim, the same in every run
and on every board profile whose chip is `esp32`. The ESP32-S3, C3 and C6 get other MACs from
esp32sim, and have no virtual station to join their SoftAP. They are not a physical board's: on the serial backend the MACs come
from the board's eFuses, and nothing in esp32tl reads them. A firmware that reads the station MAC
while its station interface does not exist (Arduino's `WiFi.macAddress()` in `WIFI_AP` mode) can
get something else, on esp32sim as on hardware. `tests/test_wifi.py` checks the station MAC
against `MAC` in `runner/src/chip/esp32.rs`, and the SoftAP MAC as that plus one; no test reads the
SoftAP MAC from a running firmware. `wifi.station` refuses both as its own `mac`.

This is exclusive with `wifi.access_point(...)` and `wifi.nearby(...)`. A second call raises
`wifi has its station already: one per test`. `access_point_off()` and `access_point_on()` raise
`wifi.access_point_off(): wifi.station has no virtual access point to switch` and the corresponding
`wifi.access_point_on()` message. Explicit station departure and rejoining are not implemented.

Wait for `network.when_leased()` after power-on, then use `network.connect(80)` or
`network.http_client.get("/status")`. The lease and host-side services are described in
[the network guide](network-user.md#joining-the-devices-own-access-point). Serial does not supply virtual air; Wi-Fi replay is not a supported source. Only the classic ESP32 has the virtual station: on the other chips `wifi.station(...)` skips the
test, `esp32sim: wifi on esp32s3: esp32sim has no virtual station for a SoftAP on the ESP32-S3 (#229, G4)`.
Open and WPA2-PSK are supported; there is no virtual
AP+STA combination
([postponed until esp32sim models it](../decisions/harness.md#a-virtual-access-point-and-a-virtual-station-together-wait-for-esp32sims-apsta)),
WPA3-SAE, required PMF, hidden SSID probing or keep-alive. An idle device may
disassociate the host after about 300 s; the emulator rejoins. Lease renewal is not implemented:
expiry clears the address and connections and starts DHCP again. Radio extensions await upstream
support, and renewal awaits a DHCP client extension.

`board.wifi.station(...)` may be configured before power-on, including for a device that starts its SoftAP much later. DHCP begins when the emulator reports the data link ready, after the WPA2 key exchange where applicable. `network.when_leased()` remains a current-state condition and becomes false after link loss.

## Parallel execution

The esp32sim backend supports independent board tests under pytest-xdist; see
[Running tests in parallel](../guides/writing-tests.md#running-tests-in-parallel) for installation,
resource costs, shared caches, reports and timeout handling. Selecting the serial backend with
xdist workers is rejected during configuration, before collection or fixture setup:
`serial backend cannot run in pytest-xdist workers; use -n 0`.
