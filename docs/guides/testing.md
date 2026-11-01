# Testing esp32tl

For whoever changes this repository: how to run its own test suite, how pytest is configured
here, and what each test file needs to run. Writing tests for your own firmware with esp32tl is
[pytest-plugin-user.md](../features/pytest-plugin-user.md). Setting up the machine is
[development.md](development.md).

## Running the tests

```sh
make test                                   # everything, from a clean checkout
.venv/bin/pytest                            # the pytest suite, after make setup and make firmware
.venv/bin/pytest tests/test_firmware_build.py -k requires_docker
make test-runner                            # the runner's Rust unit tests only
make test-runner RUNNER_PROFILE=dev         # the same, without the fat-LTO link
```

`make test` runs `make setup`, then `make test-runner`, builds the validation images that are
missing, and runs `.venv/bin/pytest` ([development.md](development.md#make-targets)).

The suite runs the runner from `runner/target/release/` and the ROM ELF from `.cache/`, where
`make setup` leaves them. To run it against a `RUNNER_PROFILE=dev` build, name that one:

```sh
ESP32TL_RUNNER=runner/target/debug/esp32tl-runner .venv/bin/pytest
```

`ESP32TL_RUNNER` and `ESP32TL_ROM` are described in
[esp32sim-backend-user.md](../features/esp32sim-backend-user.md).

## How pytest is configured

`[tool.pytest.ini_options]` in `pyproject.toml`:

- `testpaths = ["tests"]`: a bare `pytest` collects `tests/` only.
- `addopts = ["-ra", "--strict-markers"]`: the summary lists every outcome but passes, skip reasons
  included, and a marker nobody registered is an error. The esp32tl plugin registers `app_path`,
  `board_profile` and `tap` itself, and pytest-timeout `timeout`; `requires_docker` and
  `requires_mtools` are registered here.
- `markers`: `requires_docker: needs a Docker daemon`, `requires_mtools: needs mtools (mformat,
  mcopy, mdir)`.
- `filterwarnings` ignores `record_xml_attribute is an experimental feature`
  (`pytest.PytestExperimentalApiWarning`), which pytest-embedded's `app_path` fixture causes on every
  test.

The esp32tl plugin is loaded through its `pytest11` entry point, so the suite needs the package
installed in `.venv`, as `make setup` does. The suite sets no `timeout`, so every test that uses
`board`, here and in the suites the `pytester` tests run, gets the plugin's wall-time default of
600 s ([the test's timeout](../features/pytest-plugin-user.md#the-tests-timeout)); `--timeout`
or `PYTEST_TIMEOUT` overrides it for a run.

`tests/conftest.py` loads the `pytester` plugin (`pytest_plugins = ["pytester"]`), which the tests
of the plugin and of the marker use to run pytest on a generated suite. They run it in a
subprocess (`pytester.runpytest_subprocess`), because pytest-embedded starts a multiprocessing
manager for every session.

### `requires_docker` and `requires_mtools`

A test that needs a container carries `@pytest.mark.requires_docker`. When at least one collected
test carries it, `pytest_collection_modifyitems` in `tests/conftest.py` checks Docker once
(`docker_missing`) and, if Docker cannot run a container, skips every marked test with one of
these reasons:

| Reason | When |
|---|---|
| `requires_docker: no docker on PATH` | `docker` is not on `PATH` |
| `requires_docker: 'docker info' did not answer in 30 s` | `docker info` timed out |
| `requires_docker: the Docker daemon is not reachable ('docker info' failed)` | `docker info` exited non-zero |

No test of the suite carries the marker. The only marked tests are the ones
`tests/test_firmware_build.py` generates to test the marker itself.

A test that makes or reads a FAT image carries `@pytest.mark.requires_mtools`: the same hook
checks once (`mtools_missing`) that `mformat`, `mcopy` and `mdir` are on `PATH`, and skips every
marked test without them, with the reason `requires_mtools: no mformat, mcopy, mdir on PATH
(mtools)`, naming those missing. The tests of `tests/test_sdspi.py` carry it. In CI the test job
installs mtools first, so there they run ([cicd.md](../cicd.md#test)).

## What each test file needs

| File | What it covers | What it needs |
|---|---|---|
| `tests/test_package.py` | The `pytest11` entry point, that pytest loaded the plugin, `esp32tl --version` | The package installed |
| `tests/test_cli.py` | `esp32tl run` and `esp32tl replay`: the arguments they refuse and the runs that cannot start (exit 2), in this process; runs of `hello_world` and `nmea_reader` in a subprocess, a baud mismatch (exit 1) and Ctrl-C (exit 130), also before the runner connects, against a fake runner that never does | The refusals nothing beyond the package; the runs the built images, the runner and the ROM ELF |
| `tests/test_boards.py` | The board profiles esp32tl ships, the profile schema's refusals, the capability declaration and the requests it refuses | The package installed |
| `tests/test_firmware_build.py` | `firmware/build.sh` run against a fake `docker` and a fake `idf.py` that record how they were called; the `requires_docker` marker | Neither Docker nor ESP-IDF; milliseconds |
| `tests/test_firmware_images.py` | The `hello_world`, `nmea_reader` and `sdspi` images as `make firmware` built them: target `esp32`, bootloader at `0x1000`, partition table at `0x8000`, app at `0x10000`, the ELF; `nmea_reader`'s UART settings and `sdspi`'s SD card pins in their `sdkconfig` | The built images |
| `tests/test_sdspi.py` | ESP-IDF's `sdspi` with the runner's SD card: a FAT image mounted, written and read back with mtools, one without a file system, an empty slot; through `pytester`, the `sd_card` fixture used out of order and images it refuses | The built `sdspi` image, the runner, the ROM ELF and mtools (`requires_mtools`) |
| `tests/test_hello_world.py` | ESP-IDF's `hello_world`, unmodified, powered on and printing `Hello world!` through the backend | The built image, the runner and the ROM ELF |
| `tests/test_nmea_reader.py` | The first milestone (plan M1): `nmea_reader` fed the generated GNSS recordings, and sentences it must ignore, through `gnss.replay` before `board.power_on()`: `test_reports_a_fix`, `test_fix_follows_the_recording`, `test_time_is_emulated` (ten fixes in under five wall seconds, and `board.now` just past 10 s) | The built image, the runner and the ROM ELF |
| `tests/test_clock.py` | The test's clock and the console on it: the run standing still until the test waits, `wait`, `wait_until`'s steps, `NeverHappened`, `RunEnded`, and `console.expect`'s matching, against a runner thread that feeds a `BoardLink` | Nothing beyond the package; under a second |
| `tests/test_recording.py`, `tests/test_replay.py` | The recording format's reader and writer, its errors and clocks; the stream replay's timing and refusals, and the `Uart` interface that puts it on a `BoardLink` | Nothing beyond the package; milliseconds |
| `tests/test_gnss_fixture.py` | `gnss.replay` into `nmea_reader` through the `gnss` fixture, and, through `pytester`, a source given out of order with `board.power_on()`, and `test_nmea_reader.py`'s `test_reports_a_fix` failing on a changed recording and on a baud mismatch | The built `nmea_reader` image, the runner and the ROM ELF |
| `tests/test_gnss_recordings.py` | `recordings/gnss-*.jsonl` are what `scripts/generate-gnss-recordings.py` writes, `--check` names a stale one, and every recording is one valid sentence per emulated second | Nothing beyond Python |
| `tests/test_pytest_plugin.py` | The plugin's parametrisation and refused options, the `board_profile` and `tap` markers, the wall-time timeout it gives, the runner's command line, `load_app`'s refusals, the runner and ROM paths, power-on, sources and how a runner's exit is reported (against a fake runner, a shell script), how the backend serves the link, holds it on the test's clock and ends the run (against a fake runner that speaks the link protocol, a Python script) | Most tests nothing beyond the package; `test_hello_world_fails_on_an_image_that_prints_something_else` the built image, the runner and the ROM ELF |
| `tests/test_link_protocol.py`, `tests/test_boardlink.py` | The link protocol's codec and handshake; `BoardLink`'s turns, routing and errors, fed messages directly or over a socket pair | Nothing beyond the package; milliseconds |
| `tests/test_check_docs.py`, `tests/test_decisions_index.py` | `scripts/check-docs.py` and `scripts/decisions-index.py`, run against temporary git repositories; one test runs the integrity checks of `check-docs.py` on this repository's own documentation | git; neither Docker nor the runner |

`test_hello_world_fails_on_an_image_that_prints_something_else` copies the `hello_world` build,
patches the app image to print `Hello WORLD!`, fixes its checksum and SHA-256, and runs
`tests/test_hello_world.py` on it: that test must fail there, after the image has run.

A missing image fails the tests that need it rather than skipping them:
`<build> holds no finished build: run make firmware` from `tests/test_firmware_images.py`,
the runs of `tests/test_cli.py`, the `pytester` tests of `tests/test_gnss_fixture.py` and
`test_hello_world_fails_on_an_image_that_prints_something_else`,
and the backend's `esp32sim: <build> holds no ESP-IDF build: no flasher_args.json` from
`tests/test_hello_world.py`, `tests/test_nmea_reader.py`, `tests/test_sdspi.py` and
`test_a_replay_reaches_nmea_reader_on_the_gnss_route`.
`tests/test_firmware_images.py` also fails
on an image built in a container other than the one `firmware/build.sh` pins
(`<image> was built in <container> (ESP-IDF <version>), not in <pinned> (<version>): run make firmware`),
and passes an `IDF_LOCAL=1` build with a warning. A missing runner or ROM ELF fails with
`esp32sim: no runner at <path>: run make setup, or set ESP32TL_RUNNER` or
`esp32sim: no ROM ELF at <path>: run make setup, or set ESP32TL_ROM`.

In `tests/test_firmware_build.py`, `test_build_refuses_esp32tl_container_set_on_the_host` skips
with `this suite runs in a Docker container` when `/.dockerenv` exists.

## The runner's Rust tests

`make test-runner` runs `cargo test --locked` in `runner/`, in the profile `RUNNER_PROFILE` names
(default `release`, where the test binary also goes through the fat-LTO link). The tests live in
files of their own, declared with `#[cfg(test)] mod tests;` in the module they test:

- `runner/src/tests.rs`, for `runner/src/main.rs`: `stop_names_are_the_variant_names` checks that
  `stop_name` returns each `Stop` variant's own name, so a misspelt one fails. It needs the
  `emu-core` dev-dependency for `Stop::CostModelLifecycle`.
- `runner/src/cli/tests.rs`, for `runner/src/cli.rs`: the command-line parser, from a minimal
  command line to `--help` and every usage error.
- `runner/src/sd_card/tests.rs`, for `runner/src/sd_card.rs`: the SD card against a host that
  sends what ESP-IDF's `sdspi` sends.

New tests go into these files, or a `tests.rs` beside a new module, not into inline
`mod tests { ... }` blocks: `make docs-check` leaves `runner/src/**/tests.rs` out of what counts as
a behaviour change ([documentation.md](documentation.md#the-check)).

## In CI

The `runner` job runs `make build-runner test-runner`; the `test` jobs run `.venv/bin/pytest` on
Python 3.11, 3.13 and 3.14 with the images, the runner and the ROM ELF the build jobs left in the
Actions cache; the `lint` job runs `make lint`. [docs/cicd.md](../cicd.md) describes each job.

## See also

- [development.md](development.md): requirements, setup, make targets, hooks.
- [pytest-plugin-user.md](../features/pytest-plugin-user.md): writing tests with esp32tl.
- [validation-firmware-user.md](../features/validation-firmware-user.md): the images the tests
  read.
- [docs/cicd.md](../cicd.md): the CI workflow.
