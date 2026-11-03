# Testing esp32tl

For whoever changes this repository: how to run its own test suite, how pytest is configured
here, and what each test file needs to run. Writing tests for your own firmware with esp32tl is
[pytest-plugin-user.md](../features/pytest-plugin-user.md). Setting up the machine is
[development.md](development.md).

## Running the tests

```sh
make test                                   # everything, from a clean checkout
.venv/bin/pytest                            # the pytest suite, after make setup and make firmware examples
.venv/bin/pytest tests/test_firmware_build.py -k requires_docker
make test-runner                            # the runner's Rust unit tests only
make test-runner RUNNER_PROFILE=dev         # the same, without the fat-LTO link
make determinism                            # tests/test_determinism.py twice, the runs compared
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

## Real-board checks

Serial is off by default. After building firmware, these commands **flash the selected board**:

```sh
.venv/bin/pytest tests/test_hello_world.py --esp32tl-backend serial --port /dev/ttyUSB0
.venv/bin/pytest tests/test_nmea_reader.py -k starts_uart --esp32tl-backend serial --port /dev/ttyUSB0
```

Use the connected device's exact path (on macOS, for example `/dev/cu.usbserial-5C854994151`),
without wildcards. `ESPPORT` must be unset, empty or equal to the explicit `--port` value.
The NMEA smoke test checks UART startup without a receiver; the replay/model tests skip on
serial. `--skip-autoflash y` reuses the image already on the board.
[Serial documentation](../features/serial-backend-user.md) explains time, reset and recording/hash
guarantees. Unit tests do not establish hardware compatibility.

`tests/test_serial_backend.py` needs no hardware or runner: it runs the actual pytest-embedded
fixture chain with an in-memory port and fake flash/reset, checking selection, autoflash,
console capture, wall time, capability skips, reference hashes before port creation and disconnects.

## How pytest is configured

`[tool.pytest.ini_options]` in `pyproject.toml`:

- `testpaths = ["tests", "examples"]`: a bare `pytest` collects both suites.
- `addopts = ["-ra", "--strict-markers"]`: the summary lists every outcome but passes, skip reasons
  included, and a marker nobody registered is an error. The esp32tl plugin registers `app_path`,
  `app_recording` and `board_profile` itself, and pytest-timeout `timeout`; `requires_docker` and
  `requires_mtools` are registered here.
- `markers`: `requires_docker: needs a Docker daemon`, `requires_mtools: needs mtools (mformat,
  mcopy, mdir, mtype, mmd)`.
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
checks once (`mtools_missing`) that `mformat`, `mcopy`, `mdir`, `mtype` and `mmd` are on `PATH`, and
skips every marked test without them, with the reason `requires_mtools: no mformat, mcopy, mdir,
mtype, mmd on PATH (mtools)`, naming those missing. The tests of `tests/test_sdspi.py` carry it, and so do the reset and power-cycle tests of `tests/test_power.py`. In
CI the test job installs mtools first, so there they run ([cicd.md](../cicd.md#test)).

## What each test file needs

| File | What it covers | What it needs |
|---|---|---|
| `tests/test_package.py` | The `pytest11` entry point, that pytest loaded the plugin, `esp32tl --version` | The package installed |
| `tests/test_cli.py` | `esp32tl run` and `esp32tl replay`: the arguments they refuse and the runs that cannot start (exit 2), in this process; runs of `hello_world`, `nmea_reader` and `gpio_echo` (the LilyGO profile's pull-down on RI) in a subprocess, a baud mismatch (exit 1) and Ctrl-C (exit 130), also before the runner connects, against a fake runner that never does; the profile's `--psram-mb`, against a fake runner that prints its arguments | The refusals nothing beyond the package; the runs the built images, the runner and the ROM ELF |
| `tests/test_boards.py` | The board profiles esp32tl ships, the profile schema's refusals, the capability declaration and the requests it refuses | The package installed |
| `tests/test_firmware_build.py` | `firmware/build.sh` run against a fake `docker` and a fake `idf.py` that record how they were called; the `requires_docker` marker | Neither Docker nor ESP-IDF; milliseconds |
| `tests/test_firmware_images.py` | The `hello_world`, `nmea_reader`, `gpio_echo`, `sdspi`, `nvs_reader`, `persistence`, `pppos_client` and `http_request` images as `make firmware` built them: target `esp32`, bootloader at `0x1000`, partition table at `0x8000`, app at `0x10000`, the ELF; `nmea_reader`'s UART settings, `gpio_echo`'s pins, `sdspi`'s SD card pins and `http_request`'s default Wi-Fi SSID and passphrase in their `sdkconfig` | The built images |
| `tests/test_flash.py` | Partition tables, NVS value validation, blobs above 64 KiB, UTF-8 page boundaries and CRCs, real generator, pre-boot overrides and errors | Python dependencies only |
| `tests/test_nvs_reader.py` | Firmware reads all NVS value types through ESP-IDF | The built `nvs_reader` image, runner and ROM ELF |
| `tests/test_power.py` | `persistence` through `esp_restart()`, `board.reset()` and a power cycle: reset causes, NVS, RTC and RAM counts, the card's lines, `board.boots`, `board.now` while off, the errors of the power calls, the build's files unchanged | The built `persistence` image, runner and ROM ELF; mtools for the two tests with a card (`requires_mtools`) |
| `tests/test_sdspi.py` | ESP-IDF's `sdspi` with the runner's SD card: a FAT image mounted, written and read back with mtools, one without a file system, an empty slot, the LilyGO profile's slot with the GNSS and modem models selected; through `pytester`, the `sd_card` fixture used out of order and images it refuses | The built `sdspi` image, the runner, the ROM ELF and mtools (`requires_mtools`) |
| `tests/test_http_request.py` | ESP-IDF's unmodified `http_request` through the `wifi` fixture: joining the test's access point and fetching from its HTTP server, joining again after `wifi.access_point_off()` and `wifi.access_point_on()` and after `board.reset()` and a power cycle, no access point, a wrong password that gets no lease, the report and `board.properties()["wifi"]`; through `pytester`, `wifi` refused after `board.power_on()` | The built `http_request` image, the runner and the ROM ELF |
| `tests/test_wifi.py` | The `Wifi` interface without the emulator: the runner's `--wifi` and `--wifi-nearby` arguments, the parameters it refuses, one access point and networks around that are not it, a backend that refuses the access point, the `wifi_ap` inputs at the instants of the switches | Nothing beyond the package; milliseconds |
| `tests/test_net_ethernet.py`, `tests/test_net_http.py` | The network behind the access point with handmade Ethernet frames and independent checksums: DHCP, ARP, IPv4 to the services, other ethertypes dropped; the HTTP server's side of a connection without TCP: what it serves, keeps and refuses | Nothing beyond the package; milliseconds |
| `tests/test_hello_world.py` | ESP-IDF's `hello_world`, unmodified, powered on and printing `Hello world!` through the backend | The built image, the runner and the ROM ELF |
| `tests/test_nmea_reader.py` | The first milestone (plan M1): `nmea_reader` fed the generated GNSS recordings, and sentences it must ignore, through `gnss.replay` before `board.power_on()`: `test_reports_a_fix`, `test_fix_follows_the_recording`, `test_time_is_emulated` (ten fixes in under five wall seconds, and `board.now` just past 10 s) | The built image, the runner and the ROM ELF |
| `tests/test_nmea_reader_model.py` | `nmea_reader` fed by the GNSS receiver model: a fix in both hemispheres, a kilometre of movement, the fix lost and coming back, silence, a power cycle of the board, read with `console.line` and compared in units | The built image, the runner and the ROM ELF |
| `tests/test_gpio_echo.py` | `gpio_echo` on the `lilygo-t-a7670g-r2` profile through the `lines` fixture: RI at the board's pull-down from power-on, PWRKEY following RI as the test drives and releases it, the lines' events with their times, a level driven before `board.power_on()` | The built image, the runner and the ROM ELF |
| `tests/test_lines.py` | The `Lines` interface: line names, refused lines and levels, the board's pulls in the first turn and on release, the stamps of what the test drives, the firmware's level from `gpio_out`, on a `BoardLink` | Nothing beyond the package; milliseconds |
| `tests/test_clock.py` | The test's clock and the console on it: the run standing still until the test waits, `wait`, `wait_until`'s steps, `NeverHappened`, `RunEnded`, `console.expect`'s matching, and the console's lines and `console.line`, against a runner thread that feeds a `BoardLink` | Nothing beyond the package; under a second |
| `tests/test_units.py`, `tests/test_gnss_model.py` | The units: constructors, arithmetic, great-circle movement, `about`; the GNSS receiver model's sentences against NMEA worked out by hand, its epochs and what the test tells it, on a clock of the test's own | Nothing beyond the package; milliseconds |
| `tests/test_air.py`, `tests/test_spp_peer.py` | The air's loop on the board's clock; the SPP peer and the controller under it against a Bumble device that does what `bt_spp_initiator` does: inquiry, legacy pairing with a PIN and with a stored key, encryption, SDP, RFCOMM, the channel to one authenticated device at a time, and a model's answers at their times, without the emulator | Nothing beyond the package; under a second |
| `tests/test_matching.py` | Synthetic request/answer recordings: per-command cycling, silence, deadlines, loops, fragmentation, prompts and AT/PMTK/UBX defaults | Nothing beyond the package |
| `tests/test_recording.py`, `tests/test_replay.py` | The recording format's reader and writer, its errors and clocks; the replay's timing and refusals, GNSS configuration responses and unseen-request reporting, and the `Uart` interface that puts it on a `BoardLink` | Nothing beyond the package; milliseconds |
| `tests/test_gnss_fixture.py` | `gnss.replay` and `gnss.tap` into `nmea_reader` through the `gnss` fixture, and, through `pytester`, a source given out of order with `board.power_on()`, and `test_nmea_reader.py`'s `test_reports_a_fix` failing on a changed recording and on a baud mismatch | The built `nmea_reader` image, the runner and the ROM ELF |
| `tests/test_gnss_recordings.py` | `recordings/gnss-*.jsonl` are what `scripts/generate-gnss-recordings.py` writes, `--check` names a stale one, and every recording is one valid sentence per emulated second | Nothing beyond Python |
| `tests/test_pytest_plugin.py` | The plugin's parametrisation and refused options, the `board_profile` marker, the wall-time timeout it gives, the runner's command line, `load_app`'s refusals, the runner and ROM paths, power-on, sources and taps, the `lines` fixture's refusals and how a runner's exit is reported (against a fake runner, a shell script), how the backend serves the link, holds it on the test's clock, ends the run, writes a tap and reports the run (against a fake runner that speaks the link protocol, a Python script) | Most tests nothing beyond the package; `test_hello_world_fails_on_an_image_that_prints_something_else` the built image, the runner and the ROM ELF |
| `tests/test_determinism.py` | The runs the determinism job runs twice: `nmea_reader` fed each generated recording, and fed the receiver model through a fix, a move, a lost fix and a new one, each run tapped and ended at a time the test chose; `pppos_client` publishing to the broker; a TLS exchange without firmware; `http_request` fetching behind the access point (`test_wifi_http`); each test leaves its console, its tap or its network transcript in its `tmp_path` ([Determinism](#determinism)) and checks that its run did what it should | The built `nmea_reader`, `pppos_client` and `http_request` images, the runner and the ROM ELF |
| `tests/test_check_determinism.py` | `scripts/check-determinism.py` on two runs made up in the test: the files, a tap's `created` left out, the links in the JUnit XML without the run's basetemp, and the check itself against a stand-in for pytest (its arguments, a run that fails, the `--out` it refuses) | Nothing beyond Python; milliseconds |
| `tests/test_tap.py`, `tests/test_run_report.py` | A tap's recording and a tapped `Uart` on a `BoardLink`; the run's log and how the report shows it | Nothing beyond the package; milliseconds |
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
`tests/test_hello_world.py`, `tests/test_nmea_reader.py`, `tests/test_nmea_reader_model.py`,
`tests/test_gpio_echo.py`, `tests/test_sdspi.py`, `tests/test_nvs_reader.py`, `tests/test_power.py`,
`tests/test_http_request.py`, `tests/test_determinism.py` and
`test_a_replay_reaches_nmea_reader_on_the_gnss_route`.
`tests/test_firmware_images.py` also fails
on an image built in a container other than the one `firmware/build.sh` pins
(`<image> was built in <container> (ESP-IDF <version>), not in <pinned> (<version>): run make firmware`),
and passes an `IDF_LOCAL=1` build with a warning. A missing runner or ROM ELF fails with
`esp32sim: no runner at <path>: run make setup, or set ESP32TL_RUNNER` or
`esp32sim: no ROM ELF at <path>: run make setup, or set ESP32TL_ROM`.

In `tests/test_firmware_build.py`, `test_build_refuses_esp32tl_container_set_on_the_host` skips
with `this suite runs in a Docker container` when `/.dockerenv` exists.

### Modem validation

`tests/test_modem_model.py` runs the AT dialogue, timing, power and GPIO ownership without an
emulator or network. `tests/test_modem_fixture.py` runs `firmware/modem_at` on
`lilygo-t-a7670g-r2`, including two byte-for-byte identical UART taps, and the modem through `board.reset()` and a power cycle. Build it with
`firmware/build.sh modem_at`; `make firmware` and `make test` include it. It uses only the runner,
the ROM and its firmware image at test time; PPP and host network services are not involved.

## Determinism

Two runs of one recording are identical (plan, section 6): the board's clock moves only while the
test waits, and every time a peer sends comes from its recording or its model
([protocol.md](../protocol.md), rule 2 of the turn). `make determinism` checks it:

```sh
make determinism                                            # after make setup
.venv/bin/python scripts/check-determinism.py               # the same
.venv/bin/python scripts/check-determinism.py tests/test_nmea_reader.py -k fix
```

`scripts/check-determinism.py` runs `python -m pytest` twice on its arguments, pytest's options
included, or on `tests/test_determinism.py` when it is given none; arguments after a `--` go after
the script's own options. Options alone, as `-k replay`, are no longer none: pytest then collects
its `testpaths`, the whole suite, so name the file too
(`scripts/check-determinism.py tests/test_determinism.py -k replay`). Each run has a `--basetemp` and a `--junitxml` of its own under
`build/determinism/` (`--out DIR` names another directory): `run-1/` and `run-1.xml`, `run-2/` and
`run-2.xml`. Those four are removed first, when an earlier check left them; a directory that holds
anything else is refused, with exit code 2 and
`✗ <DIR> holds <names>, which no determinism check left there: name an empty or new directory with --out`,
and so is a file, with `✗ <DIR> is not a directory: name an empty or new directory with --out`, so
that a mistyped `--out` removes nothing. Both runs must pass; one that does not ends the check
with `✗ run-1: pytest exited with code 1; the runs were not compared`. Then it compares:

- Every file the two runs left under their basetemps, byte for byte, but for the `created` field of
  a recording's header, which says when the tap was written. With a fixed basetemp a test's
  `tmp_path` has the same name in both runs. The tests of `tests/test_determinism.py` write two
  files there: `console.txt`, each line the firmware printed after the board's time it ended at,
  as `1.067802937 s  fix acquired lat=52.229677 lon=21.012233`, and `uart.gnss.jsonl`, the tap of
  the GNSS route. pytest's `*current` symbolic links are left out.
- For each test, the `esp32sim_link` property the plugin gives every test that uses `board`
  ([the link's report](../features/esp32sim-backend-user.md#the-links-report)): the link's
  warnings and error, the runner's stats (the board's time the run ended at, its turns, the bytes
  each UART moved) and the bytes on each route. The run's basetemp in a tap's path is left out.

Each difference is one of these lines, and the check exits 1:

- `✗ <file> differs:` or `✗ <test>: the link differs:`, with a unified diff of up to 40 lines
  under it;
- `✗ <file>: only in run-1` or `✗ <test>: only in run-1` (or `run-2`), with no diff.

After them comes `determinism: the two runs differ in <n> places` (`1 place` for one). Two runs
that left no file and no test that used the board compared nothing, and fail too:
`✗ the runs left no file and no test that used the board: nothing was compared`.
Two identical runs end with
`determinism: the two runs are identical: 11 files, and the link of 5 tests that used the board`,
and exit 0. One run of the three GNSS tests took 2.3 to 5.3 s of wall time on an M2 Pro.

A test of `tests/test_determinism.py` ends its run at a time it chose (`board.wait`), so that the
two runs end at the same instant, and checks that its run printed its fixes, so that two runs that
both did nothing do not pass. `test_plain_mqtt` additionally runs `pppos_client`, leaves its raw modem tap and network
transcript, and compares bytes and times exactly. `test_tls_plaintext` performs a real TLS 1.2
handshake and bidirectional MQTT exchange using MemoryBIO, validates the CA and server name,
and leaves `network.json`. This test needs no firmware; `examples/position_tracker/tests/test_tracker.py` separately
validates firmware TLS/NTP through the modem.
`test_wifi_http` runs `http_request` behind the `wifi` fixture's access point (`myssid`,
`mypassword`), serves `/`, waits for the request and two more emulated seconds, and leaves its
`console.txt` and the network's transcript as `network.json`; it checks that the firmware printed
the page. With no TLS on that network, its transcript is compared exactly.

Files with `"esp32tl_network": 1` are explicit plaintext network transcripts. Before the first
`tls: handshake started` event, every event and time is compared exactly, including that event's
time. Afterwards the checker compares event order without `t_ns` and concatenated plaintext
bytes per stream/direction, ignoring chunk boundaries. Changed messages, missing events, changed
pre-handshake times and changed plain-network times still fail. This accounts for system TLS
randomness ([the postponed seeded TLS](../decisions/harness.md#a-tls-stack-with-a-seeded-random-source-is-postponed)).
Raw UART taps and `esp32sim_link` properties are still compared exactly; do not save ciphertext
or random CA files as determinism artifacts expecting the checker to discard them. The current
TLS determinism test has no board link property; a firmware TLS test must explicitly choose its
plaintext artifact rather than weaken the checker for unrelated routes or warnings.

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
Actions cache; the `determinism` job runs `make determinism` with the same builds; the `lint` job
runs `make lint`. [docs/cicd.md](../cicd.md) describes each job.

## See also

- [development.md](development.md): requirements, setup, make targets, hooks.
- [pytest-plugin-user.md](../features/pytest-plugin-user.md): writing tests with esp32tl.
- [validation-firmware-user.md](../features/validation-firmware-user.md): the images the tests
  read.
- [docs/cicd.md](../cicd.md): the CI workflow.


## Simulated network validation

`tests/test_net_ppp.py`, `test_net_ip.py`, `test_net_services.py`, `test_net_mqtt.py` and
`test_net_tls.py` check the protocol layers with handmade frames, independent checksums and
in-memory TLS. They need the installed package, including cryptography, but no emulator or
network service. `tests/test_net_ethernet.py` and `tests/test_net_http.py` do the same for the
network behind the Wi-Fi access point (Ethernet, ARP, DHCP) and the HTTP server, and
`tests/test_wifi.py` for the `wifi` interface. `tests/test_pppos_client.py` needs
`firmware/pppos_client/build/`, the runner and ROM ELF; it validates plain MQTT in both directions.
`tests/test_http_request.py` needs `firmware/http_request/build/`, the runner and ROM ELF; it
validates joining over WPA2, DHCP, DNS and HTTP behind the access point. `make determinism` needs
`nmea_reader`, `pppos_client` and `http_request`; it builds any missing one. `tests/test_modem_model.py`
also checks PPP lifecycle, guarded escape and fatal receive overflow on the network route.

## Example tests

The default pytest collection includes `examples/` alongside `tests/`.
`examples/gps_alarm_clock/tests/test_alarm.py` exercises NVS configuration, a moving GNSS
receiver and the buzzer GPIO through the public fixtures. It needs `make examples`, the runner
and the ROM ELF. Run it alone with `.venv/bin/pytest examples/gps_alarm_clock/tests` after
`make setup examples`; `make test` builds missing example images automatically.
`examples/position_tracker/tests/test_tracker.py` exercises PPP, NTP and MQTT/TLS with a CA
from NVS, GNSS movement, report deadlines on the test clock and UTC from the simulated world.
It also checks certificate rejection and missing/stale GNSS data. Build with
`firmware/build.sh --examples position_tracker` and run with
`.venv/bin/pytest examples/position_tracker/tests`. TLS ciphertext is not a determinism artifact.
See [the example guide](../features/examples-user.md) for the scenarios and wiring.

`examples/sd_data_logger/tests/test_logger.py` prepares FAT images with mtools and verifies
the firmware's CSV rows, date rollover (New Year and leap day), fix loss/recovery and malformed
NMEA rejection. Its library image builder requires mtools and reports missing commands as `SourceError`; the
empty-slot test still runs and observes the error GPIO and continued GNSS processing.
Build with `firmware/build.sh --examples sd_data_logger`, then run
`.venv/bin/pytest examples/sd_data_logger/tests`. It needs the runner and ROM ELF as well.


`tests/test_card_image.py` checks builder/reader behavior against real FAT images and marks the
cases needing mtools with `requires_mtools`. `tests/test_board_api.py` checks the shared `Board`
interface and fixture identities; its lifecycle test also uses that marker. The marker is
implemented by `tests/conftest.py`; standalone example tests require their documented tools.
