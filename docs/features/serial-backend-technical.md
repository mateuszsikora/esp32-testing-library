# Serial backend: how it works

`esp32tl/backends/serial.py` implements `Serial`; `esp32tl/backends/__init__.py` registers
`serial: esp,idf` alongside `esp32sim: None`. The plugin defaults to `Esp32Sim.name`; explicit
selection uses a single backend parameter, with `embedded_services` and `app_path` in the same
indirect parametrisation. See [the user contract](serial-backend-user.md).

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
   attributing the change to `ESPPORT`. It validates the build and optional `app_recording`
   once per item, caching the digest under `SERIAL_ARTIFACT`.
   `pytest_fixture_setup` runs it before pytest-embedded's `app` fixture, so an explicitly requested
   `dut` cannot flash before validation. `board` checks it too, then obtains `dut` dynamically.
   `check_artifact` calls `load_app(app_path, backend="serial")` for finished-build checks, then
   `app_image_sha256(app)` from `esp32tl/tap.py`. This hashes the app entry in `flasher_args.json`,
   not `IdfApp.bin_file` (which may name another `.bin`), the ELF or its embedded digest.
3. pytest-embedded creates `IdfApp`, `IdfSerial` and `IdfDut`. `IdfSerial._start` owns autoflash,
   skip-autoflash and its session cache. `Serial` neither opens another port nor calls `flash`.
   The plugin requires an `IdfDut` and passes it the profile, message queue and artifact digest.
4. `power_on` stops and joins pytest-embedded's redirect thread, discards the input buffer,
   records monotonic and UTC origins, calls `hard_reset`, then starts its reader. An exception
   rolls back `_start` and `_reader` and restores redirection; `powered` remains false, so close
   writes no tap. A subsequent power-on may retry. Successful power-on logs
   `board.power_on(): hard reset` at 0; elapsed time includes reset latency.
5. The reader calls `proc.read_all()` every `POLL_SECONDS` (10 ms), stamps each nonempty batch
   after that poll, forwards it to pytest-embedded's message queue and queues `(elapsed_ns, bytes)`.
   Stamps include USB, driver, polling and host scheduling delay; they are not exact wire-arrival
   times. The test thread drains the queue into its bytearray, `ConsoleLines`, `RunLog` and tap
   when reading console properties, waiting or closing. Only that thread mutates the buffer,
   avoiding a resize while `Console` holds a matching memoryview. Read errors are queued too.
6. UARTs use the existing `Uart` and `Gnss` capability callback, refusing replay/model requests
   before reading a recording or constructing a model. Their clock/world callbacks are lazy;
   serial never creates a `GnssReceiver`. `SdCard.insert` likewise skips before reading an image.
   GPIO fixture access skips after checking the profile has lines. Physical peripherals operate
   independently: the harness has no transport to their wires. Console tapping reuses `Uart.tap`
   through `_console`, sharing `log` and the file map, which reserves reference and flash files.
7. Waits drain console input and check conditions in the test thread every 10 ms plus scheduling
   delay. `wait` logs only `board.wait({duration})` on success and adds
   `the run ended while the test waited` on `RunEnded`, matching esp32sim. `wait_until` uses
   `board.wait_until: within` in duration validation errors. `wait_on_clock` logs
   `waits up to {within} for {description}` and the result, `{description}: happened`,
   `{description}: did not happen within {within}`, or `{description}: the run ended first`.
   The timeout reports the actual monotonic start/end interval.
8. Close sets the stop event, waits at most `READER_STOP_SECONDS` (1 s) for its reader, drains the
   queue, fixes the final elapsed time and writes the tap. A stuck reader is a `BackendError`;
   its daemon thread discards any bytes returned after the stop event. Redirection restarts only
   after a healthy reader stopped, avoiding another reader and reconnect attempts on a dead port.
   pytest-embedded remains the port owner and subsequently closes it. Close is idempotent, even
   after its error. This bounds esp32tl's join, not arbitrary USB-driver or fixture-owner calls.
9. The plugin suppresses duplicate teardown errors when the call already reported `RunEnded`,
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
| `clock` | `wall` |
| `app_sha256` | Required hash of the selected local app image |
| `console_bytes` | Number of captured console bytes |
| `error` | Connection/reader error string, or null |
| `tap` | Console tap path, or null |

The `serial console` section starts with `serial: wall clock; {n} console bytes`, followed by
`console: tapped into {path}` if tapped, and the error when present. Failures also include the
run log: the tap, power-on and wait steps, and received console lines.

## Interfaces

| Name | Contract |
|---|---|
| `Serial(dut, profile, msg_queue, *, app_sha256, directory, reference=None, skip=refuse)` | Captures a real `IdfDut`; initialization itself does not start capture |
| `name`, `clock`, `report_section`, `report_property` | `serial`, `wall`, `serial console`, `serial_console` |
| `power_on(utc=None)` | Reset and begin capture; rejects a supplied UTC and repeated successful power-on |
| `powered`, `now`, `utc` | Successful power-on, monotonic `Duration` since reset request, host-origin UTC plus elapsed time |
| `wait(duration)`, `wait_until(condition, *, within)` | Wait on wall time; durations are nonnegative `Duration`s |
| `wait_on_clock(condition, within, description)` | Console-facing wait with a description used in logs and errors |
| `console_output`, `console_lines` | Drain queued input, then return the shared bytearray / `ConsoleLines` |
| `uart(role)` | Profile's UART object, a `Gnss` for `gnss`, whose unsupported source/tap requests skip |
| `lines()`, `sd_card()` | Skip GPIO access, or return the slot whose model insertion skips; missing interfaces fail |
| `tap_console(path)` | Console `Tap` via `Uart.tap`; before power-on, with path collision checks |
| `set_up()` | `the tap of console` when tapped, otherwise empty |
| `close()` | Stop capture, restore healthy redirection, write tap, report errors; idempotent |
| `report(run=False)`, `properties()` | Human report (with run log when requested) and schema above |
| `check_artifact(app_path, reference=None)` | Validate build/reference before port setup, return the app-image hash |

## Configuration

| Option or constant | Default / owner |
|---|---|
| `--esp32tl-backend` | esp32sim unless explicitly set to serial; esp32tl |
| `--port`, indirect `port` parameter | No default for serial; an exact, nonblank value is required; pytest-embedded opens it |
| `ESPPORT` | Must be unset, empty or equal to the explicit port; pytest-embedded otherwise overrides `port` |
| `--flash-port`, indirect `flash_port` parameter | No separate flashing port supported; resolved value must be `None` |
| `--skip-autoflash` | False; pytest-embedded, use `--skip-autoflash y` to reuse flash |
| `--baud` | 115200; pytest-embedded's console baud |
| `app_path(path)` | Test directory when absent; build is its `build/` directory |
| `app_recording(path)` | No reference by default; relative to the test file or absolute; ignored on esp32sim |
| `POLL_SECONDS` | 0.01; reader and wait polling interval, not a precision guarantee |
| `READER_STOP_SECONDS` | 1.0; maximum wait for esp32tl's reader during close |

esp32tl adds no environment variables; pytest-embedded's `ESPPORT` is checked as above. Runner
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
| `serial: {build} holds no ESP-IDF build: no flasher_args.json` | Missing build metadata |
| `serial: {build} is built for {target}; serial runs the classic esp32` | Non-classic target |
| `serial: {build} asks for flash encryption, which serial does not support` | Encryption outside the supported workflow |
| `serial: {build} is not a finished build, missing {files}` | Missing files listed for flashing |
| `serial: {app_path}/build/flasher_args.json names no app image` | No `app.file` entry |
| `serial: {reference}: recording has no app_sha256` | Reference missing its required hash |
| `serial: {reference}: recording app_sha256 {expected} does not match artifact {digest}` | Reference mismatch |
| `serial: the serial backend requires pytest-embedded's esp,idf services` | Cached serial options are absent (for example an overridden `embedded_services` fixture), or `dut` is not an `IdfDut` |
| `count is above 1: esp32tl runs one board in a test` | A test parametrises multiple boards; setup fails before the port opens |
| `the board is off; call board.power_on() first` | `BoardOff` when waiting/reading time before successful power-on |
| `the board is on already: power it on once` | `BoardOff` on repeated power-on or power-on after close |
| `serial: power_on cannot set the real board's UTC; omit utc` | `ValueError` for supplied UTC |
| `{description} did not happen within {within}: waited from {start} to {end} of wall time` | `NeverHappened`; times are formatted seconds, e.g. `0.01 s` |
| `serial: console connection lost: {error}` | `RunEnded` while waiting, or a teardown failure if not already reported |
| `serial: the run is closed` | `RunEnded` when waiting on a closed capture without a connection error |
| `serial: console reader did not stop within 1 s` | Bounded join expired; teardown failure, no redirect restart |
| `serial: the tap of console cannot be written: {error}` | Tap `OSError` or `RecordingError` at teardown |
| `serial: the test set up the tap of console and never called board.power_on(): it ran no firmware` | Passing test with a tap but no successful power-on; no tap written |
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
`tests/test_pytest_plugin.py` covers console tap reports on esp32sim and ignoring references there.

`tests/test_hello_world.py::test_hello_world` and
`tests/test_nmea_reader.py::test_starts_uart` are the physical devkit smoke tests. **They have not
been run on hardware for this change.**
Loopback success does not satisfy this acceptance criterion; #25 must remain open pending it.
TODO(mateuszsikora): run both smoke tests on a designated classic devkit and record the board,
port, firmware artifacts and results before closing #25. A PR must state this missing validation.

## See also

- [Serial backend](serial-backend-user.md)
- [pytest plugin internals](pytest-plugin-technical.md)
- [Recording format](../recording-format.md)
- [Taps and run reports](run-report-technical.md)
- [Serial decisions](../decisions/harness.md#serial-is-opt-in-and-pytest-embedded-owns-flashing-and-the-port)
