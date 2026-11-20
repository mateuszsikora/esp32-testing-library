# Serial backend: how it works

`esp32tl/backends/serial.py` implements `Serial`; `esp32tl/backends/__init__.py` registers
`serial: esp,idf` alongside `esp32sim: None`. The plugin defaults to `Esp32Sim.name`; explicit
selection uses a single backend parameter, with `embedded_services`, `app_path` and `build_dir` in
the same indirect parametrisation. See [the user contract](serial-backend-user.md).

## Data flow

1. The collection hook requires an exact `--port` or indirect `port` parameter for each selected
   serial item, after `-k` / `-m` deselection. A falsey parameter falls back to the CLI value,
   matching pytest-embedded. Missing, blank and wildcard effective values fail with usage
   error 4. `--port-location`, `--port-mac` and `--port-serial-number` alone do not satisfy it:
   pytest-embedded can probe other ports while resolving them. `ESPPORT` must be unset, empty or
   equal to the explicit port, and a separate `flash_port` is refused. No duplicate port option
   is added. An empty indirect `flash_port` parameter falls back to the CLI value (or `None`);
   an explicitly empty CLI value or fixture result is refused too.
2. The plugin's `serial_artifact` checks resolved `port` / `flash_port` fixtures and `ESPPORT`
   again, then the port options cached in `_fixture_classes_and_options`. This catches an
   environment override even if another fixture restored it after the options were built.
   Missing serial options fail with the services requirement message. A cached port different
   from the explicit selection (including `None`) fails with `CACHED_PORT_CONFLICT`, without
   attributing the change to `ESPPORT`. First `serial_chip` checks the profile: the classic ESP32,
   or a chip `SERIAL_CHIPS` maps to the profile's `console`, `esp32s3` to `usb-serial-jtag`, goes
   on; a C3 or C6 with that console skips, and any other chip fails
   ([Failure modes](#failure-modes)). It validates the build and optional `app_recording`
   once per item.
   `pytest_fixture_setup` runs it before pytest-embedded's `app` fixture, so an explicitly requested
   `dut` cannot flash before validation. `board` checks it too, then obtains `dut` dynamically.
   `serial_artifact` passes the resolved `build_dir` fixture to `check_artifact`; pytest-embedded
   receives the same fixture for its app and flashing. `check_artifact` calls
   `load_app(app_path, build_dir=build_dir, backend="serial", chip=profile.chip)` for finished-build
   checks, then
   `app_image_sha256(app)` from `esp32tl/tap.py`, and returns an `Artifact` of that digest and the
   `Build`. This hashes the app entry in `flasher_args.json` (`Build.app_image`) of an ESP-IDF
   build, `firmware.bin` of a PlatformIO one, not `IdfApp.bin_file` (which may name another
   `.bin`), the ELF or its embedded digest. The item's `SERIAL_ARTIFACT` stash holds the `Artifact`.
3. For a PlatformIO build (`Artifact.build.kind` is `esp32tl.build.PLATFORMIO`), the same
   `pytest_fixture_setup` hook then sets `classes["app"]` of `_fixture_classes_and_options` to
   `PlatformioApp.for_chip(profile.chip)`, a subclass whose `chip` is the profile's (the class
   itself for `esp32`), before pytest-embedded's `app` fixture makes its app from `classes["app"]`
   with the `app_path`, `build_dir` and `part_tool` it would give `IdfApp`. `IdfApp.__init__`
   reads `flasher_args.json`; `PlatformioApp.__init__` runs `App.__init__` instead and loads the
   `Build` again through `load_app`, then sets what `IdfSerial` and `IdfDut` read of an `IdfApp`:
   - `flash_files`, the build's files at their offsets (`pytest_embedded_idf.app.FlashFile`, none
     encrypted), sorted by offset, plus erased files, `0xFF` bytes, where no file of the build
     covers a partition's offset: `otadata-erased.bin` at the table's otadata partition (type 1,
     subtype 0), as long as the partition; and `factory-erased.bin` at a `factory` partition
     (type 0, subtype 0), its first `SECTOR`, `0x1000` bytes, where the bootloader reads the image
     header, unless the build's own otadata selects a slot: a file of the build covers the
     otadata and its bytes there (`build.flash_bytes`, as far as that file goes) are not all
     `0xFF`. A `firmware.factory.bin` covers the otadata: an Arduino build's holds
     `boot_app0.bin`'s, and gets no erased file; a `framework = espidf` build's holds an erased
     `ota_data_initial.bin`, and gets `factory-erased.bin`. `firmware.bin` covers `factory` when
     it is flashed there. The files are in a `tempfile.mkdtemp` directory (`esp32tl-erased-*`),
     made with the first of them, that a `weakref.finalize` removes with the app, without the
     `ResourceWarning` of a `TemporaryDirectory` cleaned up by the garbage collector;
   - `flash_args`: `extra_esptool_args` `PLATFORMIO_ESPTOOL_ARGS`, `before` `default-reset`,
     `after` `hard-reset` and `stub` true, with `chip` the `Build`'s; `write_flash_args`
     `PLATFORMIO_WRITE_FLASH_ARGS`, `--flash-mode keep --flash-freq keep --flash-size keep`;
     `flash_files` by hexadecimal offset;
   - `flash_settings`, `keep` for the three and `encrypt` false; `target` the `Build`'s `chip`;
     `bin_file` `firmware.bin`; `elf_file` `firmware.elf` or `None`; `is_loadable_elf` and
     `is_linux_elf` false;
   - `partition_table`, the table by name as `flash.partitions` reads it, each a dict of `type`,
     `subtype`, `offset`, `size` and `flags` as integers (`IdfApp` runs ESP-IDF's
     `gen_esp32part.py` and gives `type` and `subtype` as names); `IdfSerial.flash` reads the
     `nvs` entry's `offset` and `size` for `--erase-nvs`.

   `IdfSerial.flash()` then runs `esptool.main` with
   `--before default-reset --after hard_reset --chip esp32 --baud {ESPBAUD or 921600} write-flash`,
   each file's offset and path, and the three `keep` options: it passes `after` as `hard_reset`
   whatever `extra_esptool_args` say. `binary_path` is the build directory,
   so the session cache (`Meta.hit_port_app_cache`) works as for an ESP-IDF build. With
   `--confirm-target-elf-sha256`, on a cache hit, `IdfSerial.read_flash_elf_sha256` looks for
   `bin_file` among `flash_files` for the application's offset: with `firmware.factory.bin`,
   `firmware.bin` is not one of them, and it raises
   `ValueError: .bin file not found in flash files`. With separate files it reads 32 bytes at
   `firmware.bin`'s offset plus `0xB0`, the app descriptor's `app_elf_sha256`, and compares them
   with the SHA-256 of `firmware.elf`. pioarduino fills that field in: its `frameworks/espidf.py`
   passes `--elf-sha256-offset 0xb0` to `elf2image`, and the Arduino framework's
   `tools/pioarduino-build.py` adds it to `ElfToBin`; in the `firmware.bin` of an Arduino 3.3.12
   build with pioarduino 55.03.312 the 32 bytes at `0xb0` are the SHA-256 of its `firmware.elf`.
4. pytest-embedded creates the app, `IdfSerial` and `IdfDut`. `IdfSerial._start` owns autoflash,
   skip-autoflash and its session cache. `Serial` neither opens another port nor calls `flash`.
   The plugin requires an `IdfDut` and passes it the profile, message queue and artifact digest.
5. `power_on` stops and joins pytest-embedded's redirect thread, discards the input buffer,
   records monotonic and UTC origins, calls `hard_reset`, then starts its reader. An exception
   rolls back `_start` and `_reader` and restores redirection; `powered` remains false, so close
   writes no tap. A subsequent power-on may retry. Successful power-on logs
   `board.power_on(): hard reset` at 0; elapsed time includes reset latency.
6. The reader calls `proc.read_all()` every `POLL_SECONDS` (10 ms), stamps each nonempty batch
   after that poll, forwards it to pytest-embedded's message queue and queues `(elapsed_ns, bytes)`.
   Stamps include USB, driver, polling and host scheduling delay; they are not exact wire-arrival
   times. The test thread drains the queue into its bytearray, `ConsoleLines`, `RunLog` and tap
   when reading console properties, waiting or closing. Only that thread mutates the buffer,
   avoiding a resize while `Console` holds a matching memoryview. Read errors are queued too.
7. UARTs use the existing `Uart`, `Gnss` and `Modem` capability callback, refusing replay/model requests
   before reading a recording or constructing a model. Their clock/world callbacks are lazy;
   serial never creates a `GnssReceiver` or modem model. The modem adapter has a local `Lines`
   object for its interface contract, but no model attaches or drives it. `SdCard.insert` skips
   before reading an image, and `nvs` skips before generating or writing a partition.
   GPIO fixture access skips after checking the profile has lines. Physical peripherals operate
   independently: the harness has no transport to their wires. Console tapping reuses `Uart.tap`
   through `_console`, sharing `log` and the file map, which reserves reference and flash files.
8. Waits drain console input and check conditions in the test thread every 10 ms plus scheduling
   delay. `wait` logs only `board.wait({duration})` on success and adds
   `the run ended while the test waited` on `RunEnded`, matching esp32sim. `wait_until` uses
   `board.wait_until: within` in duration validation errors. `wait_on_clock` logs
   `waits up to {within} for {description}` and the result, `{description}: happened`,
   `{description}: did not happen within {within}`, with `: {why}` when the condition's
   `waiting` returns `why`, or `{description}: the run ended first`. The timeout reports the
   actual monotonic start/end interval, after `{why}; ` when there is one.
9. Close sets the stop event, waits at most `READER_STOP_SECONDS` (1 s) for its reader, drains the
   queue, fixes the final elapsed time and writes the tap. A stuck reader is a `BackendError`;
   its daemon thread discards any bytes returned after the stop event. Redirection restarts only
   after a healthy reader stopped, avoiding another reader and reconnect attempts on a dead port.
   pytest-embedded remains the port owner and subsequently closes it. Close is idempotent, even
   after its error. This bounds esp32tl's join, not arbitrary USB-driver or fixture-owner calls.
10. The plugin suppresses duplicate teardown errors when the call already reported `RunEnded`,
   then uses `report_section` / `report_property` to attach the report and JSON property. A
   passing test that tapped without successful power-on gets the no-firmware teardown error.

## Schema

The console tap uses [recording version 1](../recording-format.md), with `interface: console`,
`source: real`, `clock: wall`, `end: hold`, the selected artifact's `app_sha256`, the esp32tl
version under `tools` and write time under `created`. Events are `out` only, stamped at reader
poll completion. `app_recording` compares the reference's hash even under skip-autoflash; this
proves artifact/reference identity, not flash contents. It does not replay console traffic.

`serial_console` is a JSON string in the pytest user properties:

| Field | Value |
|---|---|
| `build` | `build_properties(artifact.build, artifact.app_sha256)`: `{"directory", "kind", "chip", "app_sha256"}` ([run report](run-report-technical.md#schema)); `app_sha256` is the required hash of the selected local app image, also `Serial.app_sha256`, the key of [the card cache](pytest-plugin-technical.md#the-card-cache) |
| `clock` | `wall` |
| `console_bytes` | Number of captured console bytes |
| `error` | Connection/reader error string, or null |
| `tap` | Console tap path, or null |

The `serial console` section starts with `build_line(artifact.build, artifact.app_sha256)`,
`build: {directory} ({kind} for {chip}); app_sha256 {app_sha256}`, then
`serial: wall clock; {n} console bytes`, followed by
`console: tapped into {path}` if tapped, and the error when present. Failures also include the
run log: the tap, power-on and wait steps, and received console lines.

## Interfaces

| Name | Contract |
|---|---|
| `Serial(dut, profile, msg_queue, *, artifact, directory, reference=None, skip=refuse)` | Captures a real `IdfDut`; initialization itself does not start capture; `artifact` is `check_artifact`'s, whose hash is `Serial.app_sha256` |
| `name`, `clock`, `report_section`, `report_property` | `serial`, `wall`, `serial console`, `serial_console` |
| `power_on(utc=None)` | Reset and begin capture; rejects a supplied UTC and repeated successful power-on |
| `powered`, `started`, `now`, `utc` | Successful power-on (both), monotonic `Duration` since reset request, host-origin UTC plus elapsed time |
| `reset()` | After power-on, `dut.serial.hard_reset()` again with the reader running; logs `board.reset(): hard reset` at `now`. After `close()`, `BoardOff: serial: the board is closed: the port is pytest-embedded's again`, and the port is not touched |
| `power_off()`, `boots`, `wait_for_wake()` | Skip: `serial: the backend cannot switch the board's power`, `serial: the backend does not see the chip's resets` (both for `boots` and `wait_for_wake`) |
| `wait(duration)`, `wait_until(condition, *, within)` | Wait on wall time; durations are nonnegative `Duration`s |
| `wait_on_clock(condition, within, description)` | Console-facing wait with a description used in logs and errors |
| `console_output`, `console_lines` | Drain queued input, then return the shared bytearray / `ConsoleLines` |
| `uart(role)` | Profile's UART object, a `Gnss` for `gnss` or `Modem` for `modem`; unsupported source/tap requests skip, and so does `real(port)`: `_asked` skips a `real` source on a UART before the port is opened, since the declaration's `real` is the device wired to the board |
| `lines`, `sd_card` | Skip GPIO access, or return the slot whose model insertion skips; missing interfaces fail. `card_cache` works, on that slot's temporary directory |
| `wifi` | A `Wifi` made on first access with the backend's `asked`, on no link: `access_point` and `nearby` ask for a `model` source on `wifi`, which the declaration gives serial only as `real`, so they skip |
| `bluetooth` | `_get_bluetooth()` asks `_asked` for `Request("bluetooth", source="model")`, which the declaration does not give serial, so it skips; were it given, `BackendError: serial: the chip's Bluetooth has no interface on this backend` |
| `nvs(namespaces, partition="nvs", *, merge=False)` | Skip: hardware NVS replacement is unsupported, with or without `merge` |
| `flash()` | Skip: reading a real board's flash is unsupported ([decision](../decisions/harness.md#a-real-board-does-not-read-its-flash-the-test-skips)) |
| `tap_console(path)` | Console `Tap` via `Uart.tap`; before power-on, with path collision checks |
| `set_up()` | `the tap of console` when tapped, otherwise empty |
| `close()` | Stop capture, restore healthy redirection, write tap, report errors; idempotent |
| `report(run=False)`, `properties()` | Human report (with run log when requested) and schema above |
| `check_artifact(app_path, reference=None, *, build_dir=None)` | Validate build/reference before port setup, return an `Artifact` |
| `Artifact(app_sha256, build)` | A frozen dataclass: the app-image hash and the `esp32tl.build.Build` it is of, whose `kind` is `esp32tl.build.ESP_IDF` or `PLATFORMIO` |
| `PlatformioApp(app_path=None, build_dir=None, part_tool=None, **kwargs)` | The `IdfApp` of a PlatformIO build (data flow, step 3); `build` is its `esp32tl.build.Build` |

## Configuration

| Option or constant | Default / owner |
|---|---|
| `--esp32tl-backend` | esp32sim unless explicitly set to serial; esp32tl |
| `--port`, indirect `port` parameter | No default for serial; an exact, nonblank value is required; pytest-embedded opens it |
| `ESPBAUD` | 921600 for `IdfSerial.flash()` in pytest-embedded-idf 2.9.3; use a lower value for adapters that need it. `--esptool-baud` alone does not control this flash command |
| `ESPPORT` | Must be unset, empty or equal to the explicit port; pytest-embedded otherwise overrides `port` |
| `--flash-port`, indirect `flash_port` parameter | No separate flashing port supported; resolved value must be `None` |
| `--skip-autoflash` | False; pytest-embedded, use `--skip-autoflash y` to reuse flash |
| `--baud` | 115200; pytest-embedded's console baud |
| `app_path(path, build_dir="build")` | Test directory when absent; build is relative to the app or absolute |
| `app_recording(path)` | No reference by default; relative to the test file or absolute; ignored on esp32sim |
| `POLL_SECONDS` | 0.01; reader and wait polling interval, not a precision guarantee |
| `READER_STOP_SECONDS` | 1.0; maximum wait for esp32tl's reader during close |

esp32tl adds no environment variables; pytest-embedded's `ESPPORT` is checked as above and
`ESPBAUD` controls flashing as listed in the table. Runner
and ROM settings are unused. Encrypted builds are deliberately outside M4: provisioning,
efuse/key management and ciphertext verification are not implemented
([decision](../decisions/harness.md#serial-validation-accepts-unencrypted-classic-esp32-builds-only)).

## Failure modes

Backend errors below carry `serial: ` when rendered by the plugin. A setup error exits pytest
with code 1; collection errors use 2 and usage errors 4. Raw connection/flash/reset exceptions
from pytest-embedded propagate with their original messages.

| Message | Cause / outcome |
|---|---|
| `{nodeid}: serial requires an explicit --port (or a parametrised port) without wildcards: automatic port discovery is disabled` | No exact port at collection; usage error 4. Resolved fixture check uses the same message without `{nodeid}: ` as a setup error |
| `{nodeid}: serial requires ESPPORT to be unset, empty or equal to the explicit port` | Environment conflicts with the explicit port; usage error 4 at collection, setup error without nodeid for resolved options |
| `serial requires pytest-embedded's cached port to match the explicit port` | Cached port differs from the resolved explicit port, including a missing, blank or wildcard value; setup error before opening a device |
| `{nodeid}: serial does not support --flash-port: use one port for flashing and console` | A separate flashing port was supplied; same collection/setup error handling as the port check |
| `{nodeid}: the app_recording marker takes one path, as in @pytest.mark.app_recording("hello-console.jsonl")` | Wrong arguments; collection error 2 |
| `serial: {build} holds no ESP-IDF or PlatformIO build: no flasher_args.json and no firmware.bin` | Missing build metadata |
| `serial: {build} …` from a PlatformIO build | The PlatformIO checks of [builds](builds-technical.md#failure-modes): files, images, table, chip and flash encryption |
| `serial: {build} is built for {target}; the board's chip is {chip}: build for {chip} or choose a board profile with that chip` | A build for another chip than the profile's; `check_artifact` and `PlatformioApp` load the build for the profile's chip |
| `serial: the board profile {name} carries an {title} with its console on UART0; the serial backend runs the classic ESP32, and the ESP32-S3 with its console on USB-Serial/JTAG` | A profile whose `chip` is not `esp32` and whose console is not one `SERIAL_CHIPS` gives the chip; `serial_chip` in `esp32tl/pytest_plugin.py`, from `serial_artifact`, fails the test before the port is checked or the build loaded |
| `serial: the board profile {name} has its console on the {title}'s USB-Serial/JTAG controller, which the serial backend has been validated on with the ESP32-S3 only` | A profile for the ESP32-C3 or ESP32-C6 with `console = "usb-serial-jtag"`; `serial_chip` skips the test, before the port is opened |
| `serial: {build} asks for flash encryption, which serial does not support` | Encryption outside the supported workflow |
| `serial: {build} is not a finished build, missing {files}` | Missing files listed for flashing |
| `serial: {build}/flasher_args.json names no app image` | No `app.file` entry |
| `serial: {reference}: recording has no app_sha256` | Reference missing its required hash |
| `serial: {reference}: recording app_sha256 {expected} does not match artifact {digest}` | Reference mismatch |
| `serial: the serial backend requires pytest-embedded's esp,idf services` | Cached serial options are absent (for example an overridden `embedded_services` fixture), or `dut` is not an `IdfDut` |
| `count is above 1: esp32tl runs one board in a test` | A test parametrises multiple boards; setup fails before the port opens |
| `the board is off; call board.power_on() first` | `BoardOff` when waiting/reading time before successful power-on |
| `the board is on already: power it on once` | `BoardOff` on repeated power-on or power-on after close |
| `serial: power_on cannot set the real board's UTC; omit utc` | `ValueError` for supplied UTC |
| `{description} did not happen within {within}: waited from {start} to {end} of wall time`, or `{description} did not happen within {within}: {why}; waited from …` when the condition's `waiting` returns `why` | `NeverHappened`; times are formatted seconds, e.g. `0.01 s` |
| `serial: console connection lost: {error}` | `RunEnded` while waiting, or a teardown failure if not already reported |
| `serial: the run is closed` | `RunEnded` when waiting on a closed capture without a connection error |
| `serial: console reader did not stop within 1 s` | Bounded join expired; teardown failure, no redirect restart |
| `serial: the tap of console cannot be written: {error}` | Tap `OSError` or `RecordingError` at teardown |
| `serial: the test set up the tap of console and never called board.power_on(): it ran no firmware` | Passing test with a tap but no successful power-on; no tap written |
| `serial: NVS contents cannot be configured on this backend` | Skip before generating or writing a partition |
| `serial: flash contents cannot be read on this backend` | Skip at `board.flash()` |
| `serial: GPIO lines cannot be controlled or observed on this backend` | Skip for a profile with GPIO lines |
| `serial: the board profile {name} has no lines`, `serial: the board profile {name} has no sd_card` | Missing profile interface; fixture error |
| `this board cannot tap its console` | `SourceError` from `Console.tap` on an external board lacking `tap_console`; both shipped backends implement it |

File/recording read errors retain their original text, with `serial: ` at fixture setup. UART
profile errors, capability skips, tap path/duplicate errors and duration type/range errors use
[board-profile](board-profiles-technical.md#failure-modes),
[tap](run-report-technical.md#failure-modes) and
[clock](esp32sim-backend-technical.md#failure-modes) messages. Missing profile interfaces are
errors; unsupported sources/taps skip at the call, after fixture setup may have flashed.

## Tests

`tests/test_serial_backend.py` runs the real app/serial/dut fixture chain with an in-memory
pyserial loopback port. Hardware construction, flash and reset are replaced; `IdfSerial._start`
is real. Tests cover exact-port selection, environment overrides (including cached options),
separate flash-port rejection, fixture and cached-option overrides, missing serial services,
autoflash, marker errors,
reference hashes before port creation, console reset/capture, waits and their reports, capability
skips, failed reset with retry, disconnected open ports and a blocked reader released after close.
`test_the_serial_report_names_the_build_the_test_loaded` checks the section's first line, the
build's absolute directory, kind, chip and hash, and the property's `build`, without a top-level
`app_sha256`.
`tests/test_pytest_plugin.py` covers console tap reports on esp32sim and ignoring references there.
`tests/test_platformio_build.py` covers a PlatformIO build on serial:
`test_serial_flashes_the_files_esp32sim_runs_with_the_otadata_erased`,
`test_serial_flashes_a_factory_image_whole_from_0x0`, `test_a_table_without_otadata_gets_none`,
`test_a_factory_partition_the_build_does_not_fill_is_not_booted` and
`test_serial_erases_nvs_from_the_build_s_table` run pytest-embedded's own `IdfSerial.flash()` on a
`PlatformioApp`, with `esptool.main` replaced, and check its command, the erased files' bytes, their
removal with the app without a warning, no directory for them when nothing is erased, and
`--erase-nvs`'s `erase-region`; on a launcher's table, the factory partition's first sector is
erased with separate files and with a factory image whose otadata is erased, as a
`framework = espidf` build's, but not with one that holds `boot_app0.bin`'s;
`test_serial_runs_a_platformio_build` runs the fixture chain with the fake port, with and without a
factory image, and an `app_recording` of `firmware.bin`'s hash;
`test_an_esp_idf_build_keeps_pytest_embedded_s_app` that an ESP-IDF build keeps `IdfApp`.
`test_an_image_for_another_chip_is_refused_by_its_name` and
`test_a_build_that_enables_flash_encryption_or_secure_boot_is_refused` cover the checks on serial.
A PlatformIO build on a board is in [physical validation](#a-platformio-build).
`test_an_s3_with_its_console_on_usb_serial_jtag_runs_on_the_serial_backend` runs the fixture chain
with an S3 build and a profile with `console = "usb-serial-jtag"`;
`test_a_usb_serial_jtag_console_on_a_chip_not_validated_on_hardware_skips` checks the C3's and
the C6's skip, verbatim, and that the port is never opened; and
`test_the_serial_backend_refuses_a_board_with_another_chip` in `tests/test_pytest_plugin.py` the
S3 with its console on UART0. `tests/test_usb_console.py` holds the tests that ran on a real
ESP32-S3 ([physical validation](#an-esp32-s3-on-its-usb-serialjtag-port)).

### The backend comparison

`scripts/compare-backends.py` ([user guide](serial-backend-user.md#comparing-the-two-backends))
reads only the two JUnit XML files its pytest runs write, with `-o junit_family=xunit1` for the
reason `scripts/check-determinism.py` gives: pytest-embedded's `app_path` fixture records an XML
attribute, on which xunit2 warns. A test case's outcome is `failed` when it has a `<failure>` or an
`<error>`, which wins over a `<skipped>` (a test that skipped in its body and failed at teardown has
both), then `skipped`, else `passed`. pytest writes an expected failure as
`<skipped type="pytest.xfail">`, so an `xfail` test counts as skipped; no test of the suite that
uses `board` has one. Its id is `classname::name`. The serial run goes first: the plugin's
refusals of a port are usage errors at collection (`pytest_collection_modifyitems`), exit 4,
which end the script before the esp32sim run.

The plugin gives a test that uses `board` its backend as one parameter, whose id is the backend's
name (`pytest_generate_tests` in `esp32tl/pytest_plugin.py`), and pytest joins the ids of a case's
parameters with `-`. `without_backend` removes the first part equal to the run's backend from the
bracket: `test_a_replay[esp32sim-gnss-generated-1]` and `test_a_replay[serial-gnss-generated-1]`
both become `test_a_replay[gnss-generated-1]`. An id that is the same in both runs is left out
first, so that a test without `board` parametrised over the backends' names by itself, as
`tests/test_boards.py::test_a_backend_refuses_what_it_does_not_declare[esp32sim-request_0-None]`,
is not taken for one of the esp32sim run's board tests. A parameter of another kind whose id is a
backend's name, as `serial`, would collide with the backend's; no test of the suite has one.

`tests/test_compare_backends.py` covers the outcomes read from made-up XML, the ids without the
backend, every kind of line and the counts, and `main` against a stand-in for pytest: the
arguments of each run, the required `--port`, a run that cannot test, and a comparison with no
test on both backends. Neither the runner nor a board runs.

### Physical validation

On 2026-11-02, both console smoke tests passed on a user-identified LilyGO board. esptool
identified an ESP32-D0WD-V3 (revision 3.1), 4 MB flash, and disabled flash encryption and Secure
Boot. The board profile was the default `esp32-devkitc`; these tests check console output only,
not the LilyGO peripheral wiring. No external GNSS receiver was required.

```sh
ESPBAUD=230400 .venv/bin/pytest --esp32tl-backend serial \
  --port /dev/cu.usbserial-5C854994151 --esptool-baud 230400 -v \
  tests/test_hello_world.py::test_hello_world \
  tests/test_nmea_reader.py::test_starts_uart
```

Result: **2 passed in 19.85 s**, with autoflash enabled. Host: macOS 27.0.1 arm64, Python 3.14.6,
pytest-embedded and pytest-embedded-idf 2.9.3, esptool 5.6.0. The applications were built with the
repository's pinned ESP-IDF v5.3.6 container, without firmware changes.

| App binary | SHA-256 |
|---|---|
| `firmware/hello_world/build/hello_world.bin` | `a8964fe8b1b3cc8b361e3f26b30d0ff299f4ad019a1facc2a6c94d1e02d8ae47` |
| `firmware/nmea_reader/build/nmea_reader.bin` | `d1266deb2d3ded46e8c0e22540a619a0cdcac815f5c173a4cd1f796485726f85` |

The first attempt used `--esptool-baud 230400` alone; IDF flashing still selected 921600 and both
setups failed while checking the flash connection. Setting `ESPBAUD=230400` made the same tests
pass. This is the reason for the environment-variable guidance above, not a change to the test
firmware or to pytest-embedded's flashing implementation.

Before testing, two independent reads of the entire 4,194,304-byte flash matched byte for byte.
After testing, that original image was written back with flash mode, frequency and size kept
unchanged. A full readback matched the backup before the board was reset to run its original
firmware. Backup binaries and device logs remain private and are not repository artifacts.

#### An ESP32-S3 on its USB-Serial/JTAG port

On 2026-11-17, a user-identified ESP32-S3 board with no USB-to-UART bridge connected, through its
native port, `/dev/cu.usbmodem1101` (USB 303a:1001, `USB JTAG/serial debug unit`). esptool
identified an ESP32-S3 (QFN56) revision v0.2, `USB mode: USB-Serial/JTAG`, MAC
`44:1b:f6:ec:e7:a8`, 16 MB of flash; the user allowed its flash to be overwritten, and it was not
backed up or restored. The images were `firmware/console_repl_usb` built for the S3 and
`arduino_shell`'s `esp32s3_usb` environment, with the repository's pinned containers; the profile
was `tests/boards/esp32-s3-usb.toml`.

```sh
env -u ESPPORT .venv/bin/pytest -p no:cacheprovider tests/test_usb_console.py \
  -k 'esp32s3 or arduino' --esp32tl-backend serial --port /dev/cu.usbmodem1101 -rA
```

Result: **3 passed, 2 skipped in 26.18 s**, autoflash enabled, at esptool's default 921600 baud,
and again, after `platformio.ini` moved the platform pin to `[env]` and the images were rebuilt,
the same 3 tests passed and 2 skipped again in a run of 22.42 s, which also held a test of the
runner's stdout that uses no board, since moved to `tests/test_runner_link.py`; and once more,
**3 passed, 2 skipped in 21.89 s**, with `arduino_shell`'s `esp32s3_usb` image built for the
devkit's 8 MB of flash;
the skips were `serial: uart.uart0 cannot be tapped on this backend` and `serial: the backend
cannot switch the board's power`. Two earlier runs of the two `console_repl_usb` tests passed too,
2 of 2 the second time; the first time the tap test failed on a time it compared exactly, which
the test no longer does on the wall clock, and the Arduino test passed alone once. Host: macOS
27.0.1 arm64, Python 3.14.6, pytest-embedded and pytest-embedded-idf 2.9.3, esptool 5.6.0.

| App binary | SHA-256 |
|---|---|
| `firmware/console_repl_usb/build-esp32s3/console_repl_usb.bin` | `8a4a18133eb0e85e60bbc90aece61430e23a5f0b8da9155f95f1c8197d7ca553` |
| `firmware/arduino_shell/.pio/build/esp32s3_usb/firmware.bin`, first run | `d4cca0bafb6ee3b32422e642a0b7379d475ecb3783ff1b94c89d16bdabd9070f` |
| `firmware/arduino_shell/.pio/build/esp32s3_usb/firmware.bin`, after the rebuild | `f4dbba1b984c68e4cc14868e0379b007743d1e0666c3310ee9e23bdbdd97fdd0` |
| `firmware/arduino_shell/.pio/build/esp32s3_usb/firmware.bin`, 8 MB | `bde813da2bf00c3a17a3c4cb8469c78a254330a17f5ceca94dadcde09b82df9b` |

What was measured before the backend took the chip, with pytest-embedded's `esp,idf` services
alone and with esptool and pyserial, is in
[the decision](../decisions/harness.md#the-serial-backend-runs-an-esp32-s3-through-its-usb-serialjtag-port):
esptool's own USB-Serial/JTAG reset to connect, the RTS pulse that resets the chip without a
re-enumeration, the whole boot on the console, the 3 s stop in the ROM's banner after a reset in
the test, and the two times the chip stopped answering after the port was closed and opened
again, until it was unplugged.

#### A PlatformIO build

On 2026-11-15, a PlatformIO build ran on a user-identified LOLIN32 board. esptool identified an
ESP32-D0WDQ6 (revision 1.0) and 4 MB flash; `espefuse summary` showed `FLASH_CRYPT_CNT` 0 and
`ABS_DONE_0` and `ABS_DONE_1` false. The build was the `arduino_shell` image of #193
(pioarduino 55.03.312, Arduino-ESP32 3.3.12, `framework = arduino`, the default table: `app0` at
`0x10000`, `app1` at `0x150000`, otadata at `0xe000`), copied into two build directories: one
with `firmware.factory.bin`, one with `bootloader.bin`, `partitions.bin`, `firmware.bin` and
`firmware.elf` only. One test per directory, selected by
`@pytest.mark.app_path(".", build_dir="factory")` and `build_dir="split"`, called
`board.power_on()`, then `console.expect(r"arduino_shell: Arduino-ESP32 3\.\d+\.\d+\r\n")` and
`console.expect_exact("arduino_shell: ready; type help\r\n")`.

```sh
ESPBAUD=230400 .venv/bin/pytest -v --esp32tl-backend serial --port /dev/cu.usbserial-110 test_hw.py
```

Result: each test **passed**, in 36.18 s with the factory image and 36.52 s with the separate
files, autoflash enabled. Host: macOS 27.0.1 arm64, Python 3.14.6, pytest-embedded and
pytest-embedded-idf 2.9.3, esptool 5.6.0. esptool wrote `firmware.factory.bin` at `0x0`
(1,015,904 bytes); and `bootloader.bin` at `0x1000`, `partitions.bin` at `0x8000`,
`otadata-erased.bin` at `0xe000` (8,192 bytes) and `firmware.bin` at `0x10000`, each with
`Hash of data verified`.

Before the run with separate files, the board was given another application in `ota_1`: the ESP-IDF
image that was on the board before, written at `0x150000`, and an otadata whose first entry has
`ota_seq` 2 and its CRC, which selects `ota_1`. Reset, the board ran that application. The test
then wrote the erased otadata, and the board booted `arduino_shell` from `ota_0`: without that
file it would have booted the other application while the test's hash check passed.

`scripts/compare-backends.py --port /dev/cu.usbserial-110` on the same two tests, with
`ESPBAUD=230400`, printed `passed on both` for each and
`backends: 2 tests ran on both backends, 2 agreed and 0 did not; …`.
`esp32tl run --app … --build-dir` printed the banner and `arduino_shell: ready; type help` from
both directories, exit 0.

| File | SHA-256 |
|---|---|
| `firmware.bin` | `3973502447a86044bab4be42488a7f328cb28bc3070056e54afe9bc072cb15ca` |
| `firmware.factory.bin` | `8a008a4f4fff886ea4de43e559f1abebd7e0ba491940f9ef38c8050dae08f6cf` |
| `bootloader.bin` | `f7ec7b7d9026e12dd3616bf79cee0e78974a2605afbc2863fcfb0ac9aeb159d4` |
| `partitions.bin` | `148b959cbff1c38aa8e1d5c0ba9d612c54997b945e56a63f41223eef650653a1` |

This adapter dropped bytes when reading the flash at 460800 and 230400 in one long read, so the
backup was read in 256 KiB pieces, each retried; two reads of the whole 4,194,304 bytes matched
byte for byte. Writing at 230400 passed esptool's verification each time. After testing, the
backup was written back with flash mode, frequency and size kept, a full readback matched it, and
the board booted its original application from `0x10000`.

## See also

- [Serial backend](serial-backend-user.md)
- [pytest plugin internals](pytest-plugin-technical.md)
- [Recording format](../recording-format.md)
- [Taps and run reports](run-report-technical.md)
- [Serial decisions](../decisions/harness.md#serial-is-opt-in-and-pytest-embedded-owns-flashing-and-the-port)


`Serial` implements the same public `Board` base as the simulator. `board.console` is cached,
and the `console` fixture returns it. `board.gnss`, `board.modem` and the interface fixtures reuse
the same UART objects. GPIO access skips, and `board.sd_card.image()`, `insert(...)` and
`remove()` request a model source and skip before creating or opening an image: the host cannot
take the physical card out. `close()` closes its SD
slot in a `finally` block after ending the serial run.
