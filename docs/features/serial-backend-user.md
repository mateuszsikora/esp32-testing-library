# Serial backend

`serial` runs the same `board` and `console` tests on a real classic ESP32 through
pytest-embedded's `esp,idf` services. It is **off by default**. Select it explicitly:

```sh
.venv/bin/pytest tests/test_hello_world.py --esp32tl-backend serial --port /dev/ttyUSB0
.venv/bin/pytest tests/test_nmea_reader.py -k starts_uart --esp32tl-backend serial --port /dev/ttyUSB0
```

On macOS use the device's exact path, for example `/dev/cu.usbserial-5C854994151`. `--port` is
pytest-embedded's option; esp32tl adds no port option. Serial requires an explicit, nonblank path,
without wildcards, through `--port` or an indirectly parametrised `port`. Missing/blank/wildcard
values fail before setup with
`serial requires an explicit --port (or a parametrised port) without wildcards: automatic port discovery is disabled`.
`--port-location`, `--port-mac` or `--port-serial-number` alone do not satisfy this requirement.
The check also covers a fixture overriding the port to `None`. `ESPPORT` must be unset, empty
or equal to that explicit port; otherwise setup is refused before opening a device with
`serial requires ESPPORT to be unset, empty or equal to the explicit port`. This prevents
pytest-embedded's environment override or automatic discovery from selecting another ESP32.
`--flash-port` is unsupported: flashing, console capture and hard reset must use the same port.
A separate flash port fails with
`serial does not support --flash-port: use one port for flashing and console`. These checks apply
at collection and again to resolved fixtures and pytest-embedded's cached port options.
If those cached options select a different port, setup fails with
`serial requires pytest-embedded's cached port to match the explicit port`.

Each selected test has `[serial]` in its id. Without `--esp32tl-backend`, only
`esp32sim` runs. The hardware smoke tests are `test_hello_world` and `test_starts_uart`; the latter
checks `nmea_reader: UART1 RX GPIO5 TX GPIO4 9600 baud` without needing a GNSS receiver.

## Builds and flashing

The `app_path` marker selects an ESP-IDF app, with its finished build in `build/`, just as on
esp32sim. Use the CI-built files or `make firmware`; pytest builds nothing. The build must target
`esp32`, list its app image under `app` in `flasher_args.json`, contain every flash file, and
request no flash encryption. Encrypted firmware needs provisioning and key/efuse management
that this backend does not implement or validate; it is deliberately outside this milestone.
No runner or ROM ELF is needed for serial.

pytest-embedded's `IdfSerial` flashes during fixture setup, before the test body, unless its
session cache reuses the app or `--skip-autoflash y` is set. esp32tl never flashes a second time.
Flashing replaces the device's current firmware. For iteration on a board already carrying it:

```sh
.venv/bin/pytest tests/test_hello_world.py --esp32tl-backend serial --port /dev/ttyUSB0 --skip-autoflash y
```

If the USB adapter cannot sustain the default flashing speed, set `ESPBAUD`, for example
`ESPBAUD=230400 .venv/bin/pytest tests/test_hello_world.py --esp32tl-backend serial --port /dev/ttyUSB0`.
In pytest-embedded-idf 2.9.3, `IdfSerial.flash()` uses `ESPBAUD` (default 921600); the
`--esptool-baud` option alone does not change that flash command. `--baud` independently sets
the console speed, normally 115200.

A test associated with a recording can require the local artifact to match it:

```python
@pytest.mark.app_path("../firmware/hello_world")
@pytest.mark.app_recording("hello-console.jsonl")
def test_hello(board, console):
    board.power_on()
    console.expect_exact("Hello world!")
```

`app_recording(path)` takes one path, relative to the test file or absolute. On serial, before
pytest-embedded opens the port, esp32tl compares the recording's `app_sha256` with SHA-256 of the
app binary named by `flasher_args.json`. A missing hash fails with
`serial: <path>: recording has no app_sha256`; a mismatch fails with
`serial: <path>: recording app_sha256 <expected> does not match artifact <actual>`.
This check applies with and without autoflash. The marker is ignored on esp32sim, where replaying
recordings against a different application is allowed. It does not inject the recording.

The comparison establishes recording/artifact identity, **not the contents of flash on the
board**. With `--skip-autoflash y`, keeping the board on that image is the caller's responsibility.
Without `app_recording` there is no reference recording to compare. A new console tap records the
selected artifact's hash; under skip-autoflash that is also a statement about the artifact only.

## Power-on, time and console

`board.power_on()` hard-resets the board once. Setup may already have booted it; power-on discards
pending pre-reset serial bytes and starts a new console capture. It is a reset, not a power switch.
The board continues running after teardown. `board.powered` means power-on succeeded. If reset
fails, the error propagates, capture remains off and no tap is written; a test may retry.

`board.clock` is `wall`. `board.now` uses monotonic elapsed time from the reset request, including
reset latency, and advances while the test body runs. `board.utc` is the host's UTC at reset plus
elapsed time. Supplying `utc` to `power_on` raises
`ValueError: serial: power_on cannot set the real board's UTC; omit utc`.

`board.wait`, `board.wait_until`, `console.expect`, `console.expect_exact`, `console.line` and
`console.lines` use the same interface as the emulator, but durations and timeouts are wall time.
Conditions run in the test thread, polled at intervals of up to 10 ms plus host scheduling delay.
Console lines and tap batches are stamped after the reader polls the port, at a nominal 10 ms
interval. Times include polling, USB, driver and host scheduling delays; they are neither exact
wire-arrival times nor firmware-write times. A lost port raises `RunEnded` while waiting; a
condition that times out raises `NeverHappened`, including the actual start/end time interval.

`console.tap(path)` before power-on captures console output as a version 1 recording, written
at teardown even after a failed assertion: `interface: console`, `source: real`, `clock: wall`,
`app_sha256` of the selected artifact, and `out` events in host receipt order. Paths and file
collision checks follow [taps](run-report-user.md); a tap also refuses to overwrite the reference
recording or any flash artifact. Setup and flash-tool output are excluded.
A tap without power-on fails teardown with
`serial: the test set up the tap of console and never called board.power_on(): it ran no firmware`.

The report section is `serial console`, with byte count and any connection error; failures also
show the tap, power-on and wait steps, their outcomes, and received console lines. The JSON user
property `serial_console` contains `clock`,
`app_sha256`, `console_bytes`, `error` and `tap`.

Teardown waits at most one second for esp32tl's reader. If it remains blocked, the test gets
`serial: console reader did not stop within 1 s`. The capture stops accepting new bytes and
redirection is not restarted on a stuck or disconnected port. pytest-embedded still owns port
cleanup; the bound does not cover its calls or a blocked USB driver.

## Peripheral limits

The [capability table](board-profiles-user.md#saying-what-a-test-needs) decides source and tap skips
at the call in the test. UART peripherals and the SD card are physical parts; the host cannot
replace them. Only the console can be tapped. Merely requesting `modem` on a profile with that UART does not
select a model or skip the test; requesting its model, replay or tap skips at that call.
`board.nvs(...)` skips with `serial: NVS contents cannot be configured on this backend`; it does
not generate or flash a replacement partition. Examples:

- `serial: uart.gnss takes no model source on this backend, only real`
- `serial: uart.gnss takes no replay source on this backend, only real`
- `serial: uart.gnss cannot be tapped on this backend`
- `serial: sd_card takes no model source on this backend, only real`

These skips happen after fixture setup, which may flash the board. A profile's missing interface
is an error. Requesting `lines` on a profile that has GPIO lines skips with
`serial: GPIO lines cannot be controlled or observed on this backend`. Serial does not drive or
observe those pins through the console adapter. The model/replay GNSS tests skip; the console-only
smoke tests run. Host-side hardware to drive off-board peripherals remains postponed.

`modem.network()` and the `broker` fixture also skip on serial: the simulated network
requires a modem model, which this backend cannot provide.

## Validation status

The console smoke tests passed on 2026-11-02 on a LilyGO board with a classic ESP32-D0WD-V3
(revision 3.1) and 4 MB flash: `test_hello_world` and `test_starts_uart`, **2 passed**.
The USB adapter required `ESPBAUD=230400`. This verifies console capture, reset and autoflash;
it does not verify a GNSS receiver, modem or other physical peripherals. The reproducible
command, tool versions and app hashes are in
[physical validation](serial-backend-technical.md#physical-validation).

## See also

- [Serial backend internals](serial-backend-technical.md)
- [pytest plugin](pytest-plugin-user.md)
- [Taps and run reports](run-report-user.md)
- [Running repository tests](../guides/testing.md)
