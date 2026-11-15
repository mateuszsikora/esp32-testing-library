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
.venv/bin/pytest --shard=2/4                # the second of four shards, as one CI job runs it
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

## Real-device checks

`tests/test_real_source.py` runs without hardware, with a pseudo-terminal in place of the USB adapter.
One test needs a GNSS receiver on a USB serial adapter at 9600 baud, with a view of the sky, and runs
only when told where it is:

```sh
ESP32TL_REAL_GNSS_PORT=/dev/cu.usbserial-0001 .venv/bin/pytest tests/test_real_source.py -k usb_adapter -s
```

It waits up to 300 s of the board's time, which under `--realtime` is wall time, for a fix, checks no
byte was dropped once the firmware had started, and writes what crossed the route to `/tmp/esp32tl-real-gnss.jsonl`, or to
`ESP32TL_REAL_GNSS_TAP`. The suite reads no such recording: the generated ones stay the deterministic
reference. See [the real source](../features/real-source-user.md).

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

`tests/conftest.py` also adds `--shard=K/N`: of the tests collected, only those of shard K of N run,
and the rest are deselected. A test's shard is the SHA-256 of its node id modulo N, so every job puts
it in the same shard, and a new test needs no list. Anything but `1 <= K <= N` is a usage error:
`--shard must be K/N with 1 <= K <= N, not '<value>'`. The option exists only where
`tests/conftest.py` is loaded: a bare `pytest`, or one given a path under `tests/`.

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
mtype, mmd on PATH (mtools)`, naming those missing. The tests of `tests/test_sdspi.py` and
`tests/test_sd_remount.py` carry it, and so do the three tests of `tests/test_power.py` with a card
in the slot. In
CI the test job installs mtools first, so there they run ([cicd.md](../cicd.md#test)).

## Wall time in the tests

A busy host slows every test down, and a test that reads the wall clock must not fail for that
alone ([decision](../decisions/harness.md#a-tests-wall-time-bound-proves-what-was-not-waited-for-and-a-paced-one-holds-only-while-the-host-kept-up)).
Each such read is one of three kinds:

- A hang guard: a deadline loop, an `Event.wait`, a `join`, or the `timeout` of `communicate` or
  `runpytest_subprocess`, which fails only when what it waits for never happens. Its margin is
  generous.
- A check that something is skipped or not waited for. It reads what the run leaves behind rather
  than how long it took: the board's time at which a run ended
  (`test_reports_a_fix_fails_on_a_baud_mismatch`, 1.1 s of 30), the runner's clock
  (`test_time_is_emulated`, with ten emulated seconds in under the ten wall seconds a paced run
  takes), the child still holding the link
  (`test_a_link_left_open_after_the_runner_exits_is_shut_down`), the reader still stuck
  (`test_stuck_reader_has_bounded_teardown_and_no_late_capture`), the runner gone
  (`test_ctrl_c_before_the_runner_connects_exits_130`). Where nothing shows it, the bound is
  relative to what is avoided, and the comment says to what: half of the `WRITE_SECONDS` a port
  that takes nothing is given, a sixtieth of the hour on the air, a hang guard of a fifth of the
  plugin's 600 s or of a fake runner's 300 s, the sleeps of a day of wakes against its boots.
- Under `--realtime` wall time is the feature (`tests/test_real_source.py`). A bound on it holds
  only while the host kept up, and the test skips, saying by how much the host fell behind, when it
  did not; one on the board's clock needs no skip.

A lower bound, at least `AT_LEAST_S` of wall time, only grows on a slow host, and stays as it is.

## What each test file needs

| File | What it covers | What it needs |
|---|---|---|
| `tests/test_package.py` | The `pytest11` entry point, that pytest loaded the plugin, `esp32tl --version` | The package installed |
| `tests/test_cli.py` | `esp32tl run` and `esp32tl replay`: the arguments they refuse and the runs that cannot start (exit 2), in this process; runs of `hello_world`, `nmea_reader` and `gpio_echo` (the LilyGO profile's pull-down on RI) in a subprocess, a baud mismatch (exit 1) and Ctrl-C (exit 130), also before the runner connects, against a fake runner that never does; the profile's `--psram-mb`, against a fake runner that prints its arguments | The refusals nothing beyond the package; the runs the built images, the runner and the ROM ELF |
| `tests/test_boards.py` | The board profiles esp32tl ships, the profile schema's refusals, the capability declaration and the requests it refuses | The package installed |
| `tests/test_firmware_build.py` | `firmware/build.sh` run against a fake `docker` and a fake `idf.py` that record how they were called; the `requires_docker` marker | Neither Docker nor ESP-IDF; milliseconds |
| `tests/test_platformio_firmware_build.py` | `firmware/build-platformio.sh` run against a fake `docker` and a fake `pio` that record how they were called | Neither Docker nor PlatformIO; milliseconds |
| `tests/test_arduino_shell.py` | The `arduino_shell` image, Arduino-ESP32 built by PlatformIO, loaded from `.pio/build/esp32dev/`: its banner, a `Preferences` value the test wrote with `board.nvs`, and `GET /status` from its `WebServer` once it joined the `wifi` fixture's access point | The built `arduino_shell` image, the runner and the ROM ELF; about 1.5 s of wall time for the file on an arm64 Mac |
| `tests/test_firmware_images.py` | The `hello_world`, `nmea_reader`, `gpio_echo`, `sdspi`, `sd_remount`, `nvs_reader`, `persistence`, `ota_state`, `modem_recovery`, `pppos_client`, `mqtt_session`, `http_request`, `http_server` and `bt_spp_initiator` images as `make firmware` built them: target `esp32`, bootloader at `0x1000`, partition table at `0x8000`, app at `0x10000`, the ELF; `nmea_reader`'s UART settings, `gpio_echo`'s pins, `sdspi`'s SD card pins and `http_request`'s default Wi-Fi SSID and passphrase in their `sdkconfig`; the `arduino_shell` PlatformIO build, loaded by `esp32tl.build`: `firmware.factory.bin` at `0x0`, the table's `nvs` at `0x9000` and `app0` at `0x10000` | The built images |
| `tests/test_platformio_build.py` | PlatformIO build directories ([builds](../features/builds-technical.md#tests)): made of synthetic images, the files at the platform's offsets or `firmware.factory.bin` at `0x0`, the partition table, the hash, the ELF, the chip check, every refusal, serial's refusal, `board.nvs` and `board.flash`'s table; made of built images, `nvs_reader` run with and without `firmware.factory.bin` through `@pytest.mark.app_path(".", build_dir=".pio/build/esp32")`, and `esp32tl run --build-dir` on `hello_world` | Nothing beyond the package for the synthetic ones; the built `nvs_reader` and `hello_world` images, the runner and the ROM ELF for the runs |
| `tests/test_flash.py` | Partition tables, NVS value validation, blobs above 64 KiB, UTF-8 page boundaries and CRCs, real generator, pre-boot overrides and errors; reading NVS and OTA data from a snapshot: a round trip through the generator, and pages written by hand for page order, the state bitmap, repeated entries and what a power cut leaves | Python dependencies only |
| `tests/test_flash_snapshot.py` | `board.flash()` on `persistence`: `boots` after a reset, after a power-off and after `RunEnded`, the board's time and turns unmoved, a partition's bytes, what it refuses, a run that left no file | The built `persistence` image, runner and ROM ELF |
| `tests/test_ota_state.py` | `snapshot.ota()` against what `ota_state` prints, through an update and a rollback | The built `ota_state` image, runner and ROM ELF |
| `tests/test_timer_wake.py` | `timer_wake` through two deep sleeps with a timer wake: each wake a boot after `DEEPSLEEP_RESET`, with the timer as its cause, the RTC count going on, `board.boots` and the time between boots; `board.wait_for_wake` through the same sleeps, its turns, `NeverHappened` and refusals, and `hello_world`'s restart, which it fails on | The built `timer_wake` and `hello_world` images, runner and ROM ELF |
| `tests/test_deep_sleep.py` | ESP-IDF's `system/deep_sleep`, unmodified, through two 20 s sleeps with `board.wait_for_wake`: each wake a `DEEPSLEEP_RESET` 20 s after `Entering deep sleep`, and the sleep the firmware measured on its RTC | The built `deep_sleep` image, runner and ROM ELF |
| `tests/test_bt_spp_initiator.py` | ESP-IDF's `bt_spp_initiator`, unmodified, with the `bt` fixture: against an `SppPeer` with `secure=True` it discovers, pairs with the PIN, opens SPP and streams, and the peer hears what the firmware says it wrote; another PIN fails the authentication; with nobody, or a device of another name, its one inquiry runs 38.4 s and opens nothing ([validation firmware](../features/validation-firmware-user.md#bt_spp_initiator)); the same with Bluedroid's buffers in PSRAM, `bt_spp_initiator_psram` on `lilygo-t-a7670g-r2` ([validation firmware](../features/validation-firmware-user.md#bt_spp_initiator_psram)); skipped at the `bt` fixture on the serial backend | The built `bt_spp_initiator` and `bt_spp_initiator_psram` images, runner and ROM ELF; about 16 wall seconds on an M2 Pro, measured on 2026-11-14, about 1 s of it the `bt_spp_initiator_psram` test |
| `tests/test_nvs_reader.py` | Firmware reads all NVS value types through ESP-IDF | The built `nvs_reader` image, runner and ROM ELF |
| `tests/test_power.py` | `persistence` through `esp_restart()`, `board.reset()` and a power cycle: reset causes, NVS, RTC and RAM counts, the card's lines, a card kept in `card_cache` and written on its clones, `board.boots`, `board.now` while off, the errors of the power calls, the build's files unchanged | The built `persistence` image, runner and ROM ELF; mtools for the three tests with a card (`requires_mtools`) |
| `tests/test_sdspi.py` | ESP-IDF's `sdspi` with the runner's SD card: a FAT image mounted, written and read back with mtools, a 32 GiB one with 32 KiB clusters, one without a file system, an empty slot, the LilyGO profile's slot with the GNSS and modem models selected; the card's fault rules on the example: reads held on the board's clock, a read slower than the driver's timeout and a failed read of the boot sector that fail the mount, rules that outlast a power cycle and a reset of the chip, and a rule refused after the end of the run; through `pytester`, the `sd_card` fixture used out of order and images it refuses | The built `sdspi` image, the runner, the ROM ELF and mtools (`requires_mtools`) |
| `tests/test_sd_remount.py` | `sd_remount` with the card taken out and put back while it runs: the I/O error it sees, the lines kept on the card that went out, another card and the same one again mounted afresh, `sd_card.changes`, the report and `board.properties()["sd_card"]` with a change the run did not reach; a slot empty at power-on that takes a card later; a card swapped with the supply off, which the next boot finds; through `pytester`, a card the runner cannot open ending the run, a fault rule that stays through a change of card until it is cleared | The built `sd_remount` image, the runner, the ROM ELF and mtools (`requires_mtools`) |
| `tests/test_card_cache.py` | The `card_cache` fixture's `CardCache` without the emulator, on both ways of cloning: what reaches a clone, the key, eviction, the limit counted from what is allocated, atomic writes, what `put` refuses; through `pytester`, the fixture across tests and sessions and `esp32tl_card_cache_mib` | Python dependencies; for the `pytester` tests the build directory of `persistence`, which they do not run |
| `tests/test_sd_card_slot.py` | The `SdCard` slot without the emulator: a card in and out before power-on and with the board on, the `sd_card` inputs it sends on a `BoardLink` at their instants, the runner's `sd_card_ack` taken in order, the card the run has (`held`) and the changes it has not reached, a refused or mismatched acknowledgement failing the link, one card at a time, the checks of every insertion, one slot per link | Nothing beyond the package; milliseconds |
| `tests/test_http_request.py` | ESP-IDF's unmodified `http_request` through the `wifi` fixture: joining the test's access point and fetching from its HTTP server, fetching over `network.set_path`, joining again after `wifi.access_point_off()` and `wifi.access_point_on()` and after `board.reset()` and a power cycle, no access point, a wrong password that gets no lease, the report and `board.properties()["wifi"]`; through `pytester`, `wifi` refused after `board.power_on()`; a fetch from Python's `http.server` on loopback through `network.bridge`, on the wall clock, and a bridge refused after power-on and after power-off | The built `http_request` image, the runner and the ROM ELF; the bridged test takes a few wall seconds |
| `tests/test_http_server.py` | The `http_server` image behind the `wifi` fixture's access point, with the test as its client through `network.connect(80)`: a `GET /status` closed on `Connection: close`, two `POST /echo` requests on one connection read by their `Content-Length`, a body over the firmware's limit, a refused port, `NotReachable` before the lease, and the test's connections lost at `board.reset()` and `board.power_off()` until the server listens again | The built `http_server` image, the runner and the ROM ELF; about 5 s of wall time for the file on an M-series Mac. TODO(owner): measure on hp600g1 |
| `tests/test_sd_faults.py` | The SD card's fault rules without the emulator: `sd_card.fail`, `sd_card.delay` and the clears in the link's turns at the test's instants, what they refuse, the runner's `sd_op` into the rules' hits, the report and the properties, a spent rule, a run that has ended, rules in order with the card's changes, and an `sd_fault` from another interface or one the runner would refuse | Nothing beyond the package; milliseconds |
| `tests/test_wifi.py` | The `Wifi` interface without the emulator: the runner's `--wifi` and `--wifi-nearby` arguments, the parameters it refuses, one access point and networks around that are not it, a backend that refuses the access point, the `wifi_ap` inputs at the instants of the switches | Nothing beyond the package; milliseconds |
| `tests/test_net_path.py` | `network.set_path` in exact stamps, from hand-made PPP and Ethernet frames, with the clock moved by hand: the RTT from a request to its answer, each direction's share, the rates, FIFO order and no overtaking, the floor's message, what is not delayed, resets, `quiet_until` | Nothing beyond the package; milliseconds |
| `tests/test_net_mqtt_sessions.py` | The broker's sessions by client ID in raw MQTT packets, exact bytes, flags and packet IDs: Session Present, the queue, DUP and original packet IDs on redelivery, CleanSession=1, a refused CONNECT, `session`, `forget_session`, `deliveries` and `next_delivery` | Nothing beyond the package; milliseconds |
| `tests/test_mqtt_session.py` | esp-mqtt with CleanSession=0 in `mqtt_session`: Session Present, a QoS 1 message published while offline, one redelivered with DUP after `broker.disconnect` in the same instant; on a path, a backlog's spacing at 200 ms and 600 ms, its ratio, the rate, and a redelivery | The built `mqtt_session` image, the runner and the ROM ELF |
| `tests/test_net_bridge.py` | `network.bridge` against services on loopback sockets, in wall time: the stream both ways, each end's close, what the service sends after the device closed, a refused connection, a lost link, the turns it asks for, what it refuses, and an answer reaching the station in a TCP segment | Loopback TCP; under a second |
| `tests/test_net_client.py` | The connections the test opens with `network.connect`, behind PPP and behind Ethernet, against a device made of handmade segments with independent checksums: the handshake, data both ways, both closes, refused, reset, a zero window, a segment out of order, ephemeral ports, the conditions' names, text both ways, the transcript recording what goes when it goes, the device's reset and power loss, a DHCP RELEASE, and `NotReachable` before the device has an address | Nothing beyond the package; milliseconds |
| `tests/test_net_ethernet.py`, `tests/test_net_http.py` | The network behind the access point with handmade Ethernet frames and independent checksums: DHCP, ARP, IPv4 to the services, other ethertypes dropped; the HTTP server's side of a connection without TCP: what it serves, keeps and refuses | Nothing beyond the package; milliseconds |
| `tests/test_hello_world.py` | ESP-IDF's `hello_world`, unmodified, powered on and printing `Hello world!` through the backend | The built image, the runner and the ROM ELF |
| `tests/test_nmea_reader.py` | The first milestone (plan M1): `nmea_reader` fed the generated GNSS recordings, and sentences it must ignore, through `gnss.replay` before `board.power_on()`: `test_reports_a_fix`, `test_fix_follows_the_recording`, `test_time_is_emulated` (ten fixes, `board.now` just past 10 s, and a runner on the emulated clock, with no `rt_resyncs` in its stats, in under the ten wall seconds a paced run takes) | The built image, the runner and the ROM ELF |
| `tests/test_nmea_reader_model.py` | `nmea_reader` fed by the GNSS receiver model: a fix in both hemispheres, a kilometre of movement, the fix lost and coming back, silence, a power cycle of the board, read with `console.line` and compared in units | The built image, the runner and the ROM ELF |
| `tests/test_gpio_echo.py` | `gpio_echo` on the `lilygo-t-a7670g-r2` profile through the `lines` fixture: RI at the board's pull-down from power-on, PWRKEY following RI as the test drives and releases it, the lines' events with their times, a level driven before `board.power_on()` | The built image, the runner and the ROM ELF |
| `tests/test_lines.py` | The `Lines` interface: line names, refused lines and levels, the board's pulls in the first turn and on release, the stamps of what the test drives, the firmware's level from `gpio_out`, on a `BoardLink` | Nothing beyond the package; milliseconds |
| `tests/test_clock.py` | The test's clock and the console on it: the run standing still until the test waits, `wait`, `wait_until`'s steps, `wait_for_event`'s turn at a reset and none before it, a datagram on the network's path taken at its time, `NeverHappened`, `RunEnded`, `console.expect`'s matching, and the console's lines and `console.line`, against a runner thread that feeds a `BoardLink` | Nothing beyond the package; under a second |
| `tests/test_units.py`, `tests/test_gnss_model.py` | The units: constructors, arithmetic, great-circle movement, `about`; the GNSS receiver model's sentences against NMEA worked out by hand, its epochs and what the test tells it, on a clock of the test's own | Nothing beyond the package; milliseconds |
| `tests/test_air.py`, `tests/test_spp_peer.py` | The air's loop on the board's clock; the SPP peer and the controller under it against a Bumble device that does what `bt_spp_initiator` does: inquiry, legacy pairing with a PIN and with a stored key, encryption, SDP, RFCOMM, the channel to one authenticated device at a time, and a model's answers at their times, without the emulator | Nothing beyond the package; under a second |
| `tests/test_bluetooth_guest.py` | The `bt` fixture's controller and interface: a Bumble host in Bluedroid's place, through a real `BoardLink` held by a `HeldClock` and turns made as the runner makes them; power-on, the features, the commands Bluedroid sends, unknown commands, `HCI_Reset`, `bt_spp_initiator`'s session against an `SppPeer`, packets within 496 bytes, an output due before its turn, the air on the board's clock, without the emulator | Nothing beyond the package; under a second |
| `tests/test_matching.py` | Synthetic request/answer recordings: per-command cycling, silence, deadlines, loops, fragmentation, prompts and AT/PMTK/UBX defaults | Nothing beyond the package |
| `tests/test_recording.py`, `tests/test_replay.py` | The recording format's reader and writer, its errors and clocks; the replay's timing and refusals, GNSS configuration responses and unseen-request reporting, and the `Uart` interface that puts it on a `BoardLink` | Nothing beyond the package; milliseconds |
| `tests/test_uart_model.py` | `board.uart(role).model(source)`: a model the test wrote on `Uart` alone, on a `BoardLink`, and on the GNSS and modem routes, with what it refuses; then `modem_at`, unmodified, writing its AT commands to `uart.aux` of `tests/boards/aux-uart.toml` and reading the model's answers, through a power cycle of the board | Nothing beyond the package for the first; the built `modem_at` image, the runner and the ROM ELF for the others |
| `tests/test_real_source.py` | `gnss.real(port)` on a pseudo-terminal pair: the source alone, the backend's refusals and clock, and whole runs paced to wall time, each of which takes a few wall seconds and holds wall-time bounds with wide margins, and skips the bound on stamps when the host fell behind wall time ([details](../features/real-source-technical.md#tests)) | The built `nmea_reader` and `modem_at` images, the runner and the ROM ELF for the runs; the source alone needs nothing beyond the package. One test needs a real receiver, and skips without `ESP32TL_REAL_GNSS_PORT` |
| `tests/test_gnss_fixture.py` | `gnss.replay` and `gnss.tap` into `nmea_reader` through the `gnss` fixture, and, through `pytester`, a source given out of order with `board.power_on()`, and `test_nmea_reader.py`'s `test_reports_a_fix` failing on a changed recording and on a baud mismatch | The built `nmea_reader` image, the runner and the ROM ELF |
| `tests/test_gnss_recordings.py` | `recordings/gnss-*.jsonl` are what `scripts/generate-gnss-recordings.py` writes, `--check` names a stale one, and every recording is one valid sentence per emulated second | Nothing beyond Python |
| `tests/test_pytest_plugin.py` | The plugin's parametrisation and refused options, the `board_profile` marker, the wall-time timeout it gives, the runner's command line, `load_app`'s refusals, the runner and ROM paths, power-on, sources and taps, the `lines` fixture's refusals and how a runner's exit is reported (against a fake runner, a shell script), how the backend serves the link, holds it on the test's clock, ends the run, writes a tap and reports the run (against a fake runner that speaks the link protocol, a Python script) | Most tests nothing beyond the package; `test_hello_world_fails_on_an_image_that_prints_something_else` the built image, the runner and the ROM ELF |
| `tests/test_determinism.py` | The runs the determinism job runs twice: `nmea_reader` fed each generated recording, and fed the receiver model through a fix, a move, a lost fix and a new one, each run tapped and ended at a time the test chose; `pppos_client` publishing to the broker; a TLS exchange without firmware; the `position_tracker` example publishing over TLS through the modem (`test_tls_on_the_device`); `mqtt_session` on a path with a backlog and a redelivery (`test_mqtt_session_on_a_path`); `http_request` fetching behind the access point (`test_wifi_http`); `bt_spp_initiator` streaming to an `SppPeer` through the `bt` fixture (`test_spp_to_a_peer`); each test leaves its console, its tap or its network transcript in its `tmp_path` ([Determinism](#determinism)) and checks that its run did what it should | The built `nmea_reader`, `pppos_client`, `mqtt_session`, `http_request` and `bt_spp_initiator` images and the `position_tracker` example, the runner and the ROM ELF |
| `tests/test_check_determinism.py` | `scripts/check-determinism.py` on two runs made up in the test: the files, a tap's `created` left out, the links in the JUnit XML without the run's basetemp, and the check itself against a stand-in for pytest (its arguments, a run that fails, the `--out` it refuses) | Nothing beyond Python; milliseconds |
| `tests/test_compare_backends.py` | `scripts/compare-backends.py` on JUnit XML made up in the test: the outcomes it reads, the ids without the backend, the line each test gets and the counts, and the script against a stand-in for pytest (the arguments of each run, the required `--port`, a run that cannot test, no test on both backends) ([comparing the two backends](../features/serial-backend-user.md#comparing-the-two-backends)) | Nothing beyond Python; milliseconds |
| `tests/test_tap.py`, `tests/test_run_report.py` | A tap's recording and a tapped `Uart` on a `BoardLink`; the run's log and how the report shows it | Nothing beyond the package; milliseconds |
| `tests/test_link_protocol.py`, `tests/test_boardlink.py` | The link protocol's codec and handshake; `BoardLink`'s turns, routing and errors, fed messages directly or over a socket pair | Nothing beyond the package; milliseconds |
| `tests/test_check_docs.py`, `tests/test_decisions_index.py` | `scripts/check-docs.py` and `scripts/decisions-index.py`, run against temporary git repositories; one test runs the integrity checks of `check-docs.py` on this repository's own documentation | git; neither Docker nor the runner |

`test_hello_world_fails_on_an_image_that_prints_something_else` copies the `hello_world` build,
patches the app image to print `Hello WORLD!`, fixes its checksum and SHA-256, and runs
`tests/test_hello_world.py` on it: that test must fail there, after the image has run.

A missing image fails the tests that need it rather than skipping them: `<build> holds no finished
build: run make firmware` from `tests/test_firmware_images.py`, the runs of `tests/test_cli.py`, the
`pytester` tests of `tests/test_gnss_fixture.py` and
`test_hello_world_fails_on_an_image_that_prints_something_else`,
`test_nvs_reader_built_by_platformio_runs_as_it_is` and `test_esp32tl_run_runs_a_platformio_build`
in `tests/test_platformio_build.py`, and the backend's `esp32sim: <build> holds no ESP-IDF or
PlatformIO build: no flasher_args.json and no firmware.bin` from `tests/test_arduino_shell.py`
(build `firmware/arduino_shell/.pio/build/esp32dev`), `tests/test_hello_world.py`,
`tests/test_nmea_reader.py`, `tests/test_nmea_reader_model.py`, `tests/test_gpio_echo.py`,
`tests/test_modem_recovery.py`, `tests/test_sdspi.py`, `tests/test_sd_remount.py`,
`tests/test_nvs_reader.py`, `tests/test_power.py`, `tests/test_http_request.py`,
`tests/test_mqtt_session.py`, `tests/test_bt_spp_initiator.py`, `tests/test_determinism.py` and
`test_a_replay_reaches_nmea_reader_on_the_gnss_route`. `tests/test_firmware_images.py` also fails on
an image built in a container other than the one `firmware/build.sh` pins (`<image> was built in
<container> (ESP-IDF <version>), not in <pinned> (<version>): run make firmware`), or, for
`arduino_shell`, than the one `firmware/build-platformio.sh` pins, or with another platform than its
`platformio.ini` names, and passes an `IDF_LOCAL=1` or `PIO_LOCAL=1` build with a warning. A missing
runner or ROM ELF fails with `esp32sim: no runner at <path>: run make setup, or set ESP32TL_RUNNER`
or `esp32sim: no ROM ELF at <path>: run make setup, or set ESP32TL_ROM`.

In `tests/test_firmware_build.py`, `test_build_refuses_esp32tl_container_set_on_the_host` skips
with `this suite runs in a Docker container` when `/.dockerenv` exists, and so does
`test_refuses_esp32tl_container_set_on_the_host` in `tests/test_platformio_firmware_build.py`.

### Modem validation

`tests/test_modem_model.py` runs the AT dialogue, the clock and operator, timing, power and GPIO
ownership without an emulator or network. `tests/test_modem_fixture.py` runs `firmware/modem_at` on
`lilygo-t-a7670g-r2`, including two byte-for-byte identical UART taps, and the modem through `board.reset()` and a power cycle. Build it with
`firmware/build.sh modem_at`; `make firmware` and `make test` include it. It uses only the runner,
the ROM and its firmware image at test time; PPP and host network services are not involved.
`tests/test_modem_recovery.py` runs `firmware/modem_recovery` with the simulated network, through
`esp_modem` and PPP: the recovery of a hung modem, with its pulses read from `lines.events()`, also of
one that hangs while the firmware dials, and the dial after a dropped call, with and without a Terminate-Request. Build it with `firmware/build.sh modem_recovery`.

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
`determinism: the two runs are identical: 19 files, and the link of 8 tests that used the board`,
and exit 0. One run of the three GNSS tests took 2.3 to 5.3 s of wall time on an M2 Pro.

A test of `tests/test_determinism.py` ends its run at a time it chose (`board.wait`), so that the
two runs end at the same instant, and checks that its run printed its fixes, so that two runs that
both did nothing do not pass. `test_plain_mqtt` additionally runs `pppos_client`, leaves its raw modem tap and network
transcript, and compares bytes and times exactly. `test_tls_plaintext` performs a real TLS 1.2
handshake and bidirectional MQTT exchange using MemoryBIO, validates the CA and server name,
and leaves `network.json`. This test needs no firmware. `test_tls_on_the_device` runs the
`position_tracker` example through the modem: the firmware verifies the CA from NVS, does its
ECDHE and publishes three reports over TLS, and the test leaves its `console.txt`, raw modem tap
and network transcript, so the times after the handshake are compared too.
`test_mqtt_session_on_a_path` runs `mqtt_session` with `set_path(rtt=ms(200), downlink=kbit_s(512),
uplink=kbit_s(128))`, a backlog of five and a redelivery, and leaves its `console.txt`, raw modem tap
and network transcript, compared exactly: two runs give the same stamps on a path.
`test_wifi_http` runs `http_request` behind the `wifi` fixture's access point (`myssid`,
`mypassword`), serves `/`, waits for the request and two more emulated seconds, and leaves its
`console.txt` and the network's transcript as `network.json`; it checks that the firmware printed
the page. With no TLS on that network, its transcript is compared exactly.
`test_spp_to_a_peer` runs `bt_spp_initiator` against an `SppPeer` named `ESP_SPP_ACCEPTOR` with
the PIN `1234` for 5 s of the board's time, and leaves its `console.txt`, `spp.txt`, each write
the peer heard on the SPP channel with its board time and its bytes in hex, and `hci.btsnoop` with
its sidecar `hci.btsnoop.json`, the tap of the HCI, every packet both ways at its time
([tapping the HCI](../features/run-report-user.md#tapping-the-hci)); it checks that the channel is
open and that the firmware printed a `speed(...)` line. The btsnoop file is compared byte for
byte, and the sidecar as a recording's header is, without its `created`.

Network transcripts (`"esp32tl_network": 1`) are compared exactly like any other file, times
after a TLS handshake included: the harness's TLS server draws its keys and nonces from a seed,
so the bytes on the wire, PPP's escapes of them and the device's work on them repeat
([the decision](../decisions/harness.md#the-harnesss-tls-server-draws-every-random-value-from-a-seed)).
A tap of a TLS route holds the same ciphertext in both runs.

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
  sends what ESP-IDF's `sdspi` sends, and the slot it goes in and out of.

New tests go into these files, or a `tests.rs` beside a new module, not into inline
`mod tests { ... }` blocks: `make docs-check` leaves `runner/src/**/tests.rs` out of what counts as
a behaviour change ([documentation.md](documentation.md#the-check)).

## In CI

The `runner` job runs `make build-runner test-runner`; the `test` jobs run `.venv/bin/pytest
--shard=K/4` for each K on Python 3.11, 3.13 and 3.14 with the images, the runner and the ROM ELF the build jobs left in the
Actions cache; the `determinism` job runs `make determinism` with the same builds; the `lint` job
runs `make lint`. Both private and public repositories run the matrix on
`ubuntu-24.04` (x64) and `macos-15` (ARM64). Linux test jobs start and check Docker;
all test jobs install mtools and require `mformat`, `mcopy`, `mdir`, `mtype` and `mmd`.
The existing capability markers are unchanged. [docs/cicd.md](../cicd.md) describes each job.

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
`tests/test_wifi.py` for the `wifi` interface. `tests/test_net_mqtt_failures.py` drives held,
released and delayed PUBACKs over PPP/TCP with both plain and TLS clients, then disconnects before
ACK and retransmits after reconnect. Another stream and NTP remain usable on the same PPP link.
`tests/test_net_mqtt_sessions.py` checks the broker's stored sessions with raw packets. These tests
need no firmware; `tests/test_mqtt_session.py` needs `firmware/mqtt_session/build/`, the runner and
ROM ELF, and runs esp-mqtt against those sessions and on a network path.
`tests/test_net_path.py` checks that path in exact stamps, without firmware.
`tests/test_pppos_client.py` needs `firmware/pppos_client/build/`, the runner and ROM ELF; it
validates plain MQTT in both directions, and publishing through a bridge to esp32tl's broker on a
loopback socket, on the wall clock. `tests/test_http_request.py` needs
`firmware/http_request/build/`, the runner and ROM ELF; it validates joining over WPA2, DHCP, DNS
and HTTP behind the access point, and a fetch through a bridge. `tests/test_net_client.py` checks
the connections the test opens, without firmware, and `tests/test_http_server.py` needs
`firmware/http_server/build/`, the runner and ROM ELF, and opens them to `esp_http_server` behind
the access point. `make determinism` needs `nmea_reader`, `pppos_client`, `mqtt_session` and
`http_request`; it builds any missing one. `tests/test_modem_model.py` also checks PPP lifecycle,
guarded escape and fatal receive overflow on the network route.

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
`.venv/bin/pytest examples/position_tracker/tests`. `make determinism` runs it over TLS too
(`test_tls_on_the_device`).
See [the example guide](../features/examples-user.md) for the scenarios and wiring.

`examples/sd_data_logger/tests/test_logger.py` prepares FAT images with mtools and verifies
the firmware's CSV rows, date rollover (New Year and leap day), fix loss/recovery and malformed
NMEA rejection, and, through the card's fault rules, a failed write and a failed read and the
next ones that succeed, a slow write, and a write slower than ESP-IDF's 5 s timeout (about 10 wall seconds). Its library image builder requires mtools and reports missing commands as `SourceError`; the
empty-slot test still runs and observes the error GPIO and continued GNSS processing.
Build with `firmware/build.sh --examples sd_data_logger`, then run
`.venv/bin/pytest examples/sd_data_logger/tests`. It needs the runner and ROM ELF as well.

`examples/periodic_wake/tests/test_periodic_wake.py` runs deep sleep with a timer wake: a pulse
on GPIO18 every 15 minutes, the wake count in RTC memory through sleeps, a reset and a power
cycle, and a day of wakes whose sleeps take less wall time than its boots: about 0.4 s against
6.6 s on an M2 Pro, where without the runner's steps through the sleeps they take 330 s. Build with
`firmware/build.sh --examples periodic_wake`, then run
`.venv/bin/pytest examples/periodic_wake/tests`.

`examples/obd_reader/tests/test_obd_reader.py` runs a Bluetooth Classic client through
`board.bluetooth`: it searches for an ELM327 adapter, pairs with its PIN, opens SPP and asks for
the readings the vehicle answers for, against the example's adapter model, with the adapter absent,
with another PIN, with the ignition off, with an adapter that stops answering or ends its link,
with a device that is no ELM327 and with a vehicle that has none of the readings.
`tests/test_elm327.py` beside it checks the model without a board. Build with
`firmware/build.sh --examples obd_reader`, then run `.venv/bin/pytest examples/obd_reader/tests`.


`tests/test_card_image.py` checks builder/reader behavior against real FAT images and marks the
cases needing mtools with `requires_mtools`. `tests/test_board_api.py` checks the shared `Board`
interface and fixture identities; its lifecycle test also uses that marker. The marker is
implemented by `tests/conftest.py`; standalone example tests require their documented tools.
