# Serial backend

`serial` runs the same `board` and `console` tests on a real classic ESP32, or an ESP32-S3 on its
USB-Serial/JTAG port ([below](#a-board-whose-console-is-usb-serialjtag)), through
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

The `app_path` marker selects the app, with its finished build in `build/` by default, just as on
esp32sim: an ESP-IDF build or a PlatformIO one, `.pio/build/<env>/` ([builds](builds-user.md)).
Use the CI-built files, `make firmware` or `pio run`; pytest builds nothing. An ESP-IDF build must
target the board profile's chip, list its app image under `app` in `flasher_args.json`, contain
every flash file, and request no flash encryption. A PlatformIO build needs the files
[builds](builds-user.md#what-each-kind-of-build-needs) lists, images for the profile's chip, and no
flash encryption in its `config/sdkconfig.json` when it has one. Encrypted firmware needs
provisioning and key/efuse management that this backend does not implement or validate; it is
deliberately outside this milestone. No runner or ROM ELF is needed for serial. The chips it runs
are the classic ESP32, and the ESP32-S3 with its console on USB-Serial/JTAG
([below](#a-board-whose-console-is-usb-serialjtag)). A board profile for another chip with its
console on UART0 is refused before the port is opened:
`serial: the board profile esp32-s3-devkitc carries an ESP32-S3 with its console on UART0; the serial backend runs the classic ESP32, and the ESP32-S3 with its console on USB-Serial/JTAG`.
One for the ESP32-C3 or ESP32-C6 with its console on USB-Serial/JTAG skips, before the port is
opened:
`serial: the board profile my-c3-board has its console on the ESP32-C3's USB-Serial/JTAG controller, which the serial backend has been validated on with the ESP32-S3 only`.

A PlatformIO build is flashed as esp32sim runs it: `firmware.factory.bin` at `0x0`, or else
`bootloader.bin` at the chip's bootloader offset (`0x1000` on the classic ESP32, `0x0` on the S3,
[builds](builds-user.md#what-each-kind-of-build-needs)), `partitions.bin` at `0x8000` and `firmware.bin` at the table's
`ota_0` (at `0x10000` without one). esptool keeps the flash mode, frequency and size the images
were built with. Parts of the flash that no file of the build covers are written erased where
esp32sim's flash is erased and what an earlier firmware left on the board could choose which
application boots:

- the table's otadata partition, all of it, which could name another slot. An Arduino build's
  `firmware.factory.bin` holds `boot_app0.bin`'s otadata, which selects `ota_0`, and a
  `framework = espidf` build's an erased `ota_data_initial.bin`: either covers it;
- the first sector (4 KiB) of a `factory` partition that `firmware.bin` is not in, as on
  Arduino's `app3M_fat9M_fact512k_16MB.csv`, unless the build's own otadata selects a slot: with
  the otadata erased the bootloader tries `factory` before `ota_0`, and would boot a launcher or
  an older application left there. The console then shows
  `boot: Factory app partition is not bootable` before the application starts, as on esp32sim,
  and a launcher on the board no longer boots.

An Arduino build's `pio run -t upload` writes `boot_app0.bin`'s otadata, and a
`framework = espidf` build's an erased `ota_data_initial.bin`; neither touches `factory`. So on a
table with `factory`, `esp_ota_get_boot_partition()` can name another partition than after an
Arduino build's upload of separate files, and a `framework = espidf` build boots `ota_0` here
where its upload would leave a board's launcher in `factory` to boot
(see [builds](builds-user.md#what-each-kind-of-build-needs)).
Nothing else on the flash is erased: the NVS and the data partitions keep what the board had, for
both kinds of build, unless pytest-embedded's `--erase-all` or `--erase-nvs` says otherwise.

pytest-embedded's `--confirm-target-elf-sha256` does not work with `firmware.factory.bin`: when its
session cache would skip flashing, it fails with `ValueError: .bin file not found in flash files`.
With separate files it compares the SHA-256 of `firmware.elf` with the 32 bytes at `0xb0` of the
application on the board, the app descriptor's `app_elf_sha256`, which pioarduino's builds fill in
(`elf2image --elf-sha256-offset 0xb0`), Arduino's as well as `framework = espidf`'s.

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
def test_hello(board):
    board.power_on()
    board.console.expect_exact("Hello world!")
```

`app_recording(path)` takes one path, relative to the test file or absolute. On serial, before
pytest-embedded opens the port, esp32tl compares the recording's `app_sha256` with SHA-256 of the
app binary named by `flasher_args.json`, or of `firmware.bin` for a PlatformIO build. A missing
hash fails with `serial: <path>: recording has no app_sha256`; a mismatch fails with
`serial: <path>: recording app_sha256 <expected> does not match artifact <actual>`.
This check applies with and without autoflash. The marker is ignored on esp32sim, where replaying
recordings against a different application is allowed. It does not inject the recording.

The comparison establishes recording/artifact identity, **not the contents of flash on the
board**. With `--skip-autoflash y`, keeping the board on that image is the caller's responsibility.
Without `app_recording` there is no reference recording to compare. A new console tap records the
selected artifact's hash; under skip-autoflash that is also a statement about the artifact only.

An external build is selected with
`@pytest.mark.app_path("../firmware", build_dir="../builds/debug")`. The build path is relative to
the application or absolute, and must hold an ESP-IDF or a PlatformIO build. Preflight, flashing,
the `app_recording` hash comparison and tap artifact hashes all use that same resolved directory. An
invalid selection fails before opening the port, including when `dut` is requested before `board`.
No default build is substituted. pytest-embedded's raw `--build-dir` is still refused; use the
marker's keyword ([build selection](pytest-plugin-user.md)).

## Power-on, time and console

`board.power_on()` hard-resets the board once. Setup may already have booted it; power-on discards
pending pre-reset serial bytes and starts a new console capture. It is a reset, not a power switch.
The board continues running after teardown. `board.powered` and `board.started` mean power-on
succeeded. If reset fails, the error propagates, capture remains off and no tap is written; a test
may retry.

`board.reset()` hard-resets the board again, as power-on does, through pytest-embedded's
`hard_reset()` (EN pulsed through the adapter's RTS); the console capture and `board.now` go on
across it, and the run's log gets `board.reset(): hard reset`. Before power-on it raises
`BoardOff: the board is off; call board.power_on() first`. The backend cannot switch the board's
supply, nor see the chip's resets: `board.power_off()` skips the test with
`serial: the backend cannot switch the board's power`, and `board.boots`, `board.boot`,
`board.next_boot()`, `board.uptime_zero(boot)` and `board.wait_for_wake()` with
`serial: the backend does not see the chip's resets`, at the line that asks, as an unsupported
source does ([resets and power cycles](pytest-plugin-user.md#resets-and-power-cycles)), and
`board.sleeps` and `board.next_sleep()` with `serial: the backend does not see the chip's deep
sleeps` ([waking from deep sleep](pytest-plugin-user.md#waking-from-deep-sleep)). An
argument of the wrong type to `board.next_boot()` or `board.uptime_zero()` raises its `TypeError`
first, as on esp32sim.

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
condition that times out raises `NeverHappened`, including the actual start/end time interval,
and before it what a [described condition](pytest-plugin-user.md#described-conditions)'s
`waiting=` says, when it says anything.

`console.tap(path)` before power-on captures console output as a version 1 recording, written
at teardown even after a failed assertion: `interface: console`, `source: real`, `clock: wall`,
`app_sha256` of the selected artifact, and `out` events in host receipt order. Paths and file
collision checks follow [taps](run-report-user.md); a tap also refuses to overwrite the reference
recording or any flash artifact. Setup and flash-tool output are excluded.
A tap without power-on fails teardown with
`serial: the test set up the tap of console and never called board.power_on(): it ran no firmware`.

The report section is `serial console`. Its first line names the build the test checked and
flashed, as on esp32sim:
`build: /home/me/app/build (ESP-IDF for esp32); app_sha256 {sha256}`
([the run on the board's clock](run-report-user.md#the-run-on-the-boards-clock)); under
`--skip-autoflash y` it is the local build, not proof of what the board holds. Then the byte count
and any connection error; failures also show the tap, power-on and wait steps, their outcomes, and
received console lines. The JSON user property `serial_console` contains `build` (with the
build's `app_sha256`), `clock`, `console_bytes`, `error` and `tap`.

Teardown waits at most one second for esp32tl's reader. If it remains blocked, the test gets
`serial: console reader did not stop within 1 s`. The capture stops accepting new bytes and
redirection is not restarted on a stuck or disconnected port. pytest-embedded still owns port
cleanup; the bound does not cover its calls or a blocked USB driver.

## A board whose console is USB-Serial/JTAG

An ESP32-S3 board without a USB-to-UART bridge has one port, the chip's own USB-Serial/JTAG
device (USB 303a:1001, `/dev/cu.usbmodem…` on macOS, `/dev/ttyACM…` on Linux), and its profile
says `console = "usb-serial-jtag"` ([board profiles](board-profiles-user.md#a-console-on-usb-serialjtag)).
Pass that port as `--port`; the test is the one that runs on esp32sim:

```sh
.venv/bin/pytest tests/test_usb_console.py -k esp32s3 --esp32tl-backend serial --port /dev/cu.usbmodem1101
```

esptool flashes through the port and picks its USB-Serial/JTAG reset itself; `board.power_on()`
and `board.reset()` pulse RTS, which resets the chip without the port going away, so the console
holds the whole boot, from the ROM's banner on. What was measured on a real ESP32-S3, and what is
not guaranteed, is in [the decision](../decisions/harness.md#the-serial-backend-runs-an-esp32-s3-through-its-usb-serialjtag-port):

- after `board.reset()` the ROM's output stopped for about 3 s after its first line, and the
  application's prompt came about 4.1 s after the reset, against 1.0 s after the flashing's reset:
  give a console expect after a reset a `timeout` of 10 s;
- twice, a measuring script that closed the port and opened it again left the chip answering
  nothing, esptool included, until the board was unplugged and plugged in again; the test runs did
  not meet it;
- UART0 on such a board was not measured, and the ESP32-C3 and ESP32-C6 not at all.

The board API types `boots` and `sleeps` as read-only sequences. Reading either on serial
skips the test because this backend cannot observe the chip's resets and deep sleeps.

## Peripheral limits

The [capability table](board-profiles-user.md#saying-what-a-test-needs) decides source and tap skips
at the call in the test. UART peripherals and the SD card are physical parts; the host cannot
replace them. Only the console can be tapped. Merely requesting `modem` on a profile with that UART does not
select a model or skip the test; requesting its model, replay or tap skips at that call.
`board.nvs(...)` skips with `serial: NVS contents cannot be configured on this backend`, with or
without `merge=True`; it does not generate or flash a replacement partition. `board.flash()` skips
with `serial: flash contents cannot be read on this backend`: reading it would put the chip in
download mode and break the run. Examples:

- `serial: uart.gnss takes no model source on this backend, only real`
- `serial: uart.gnss takes no replay source on this backend, only real`
- `serial: uart.gnss cannot be tapped on this backend`
- `serial: sd_card takes no model source on this backend, only real`
- `serial: uart.gnss has the device wired to the board at its far end: a host serial port goes there
  on the emulator only`, for `gnss.real(port)`, which leaves `port` closed:
  [the real source](real-source-user.md#on-the-serial-backend) is not attempted here

These skips happen after fixture setup, which may flash the board. A profile's missing interface
is an error. Requesting `lines` on a profile that has GPIO lines skips with
`serial: GPIO lines cannot be controlled or observed on this backend`. Serial does not drive or
observe those pins through the console adapter. The model/replay GNSS tests skip; the console-only
smoke tests run. Host-side hardware to drive off-board peripherals remains postponed.

`modem.network()` and the `broker` fixture also skip on serial: the simulated network
requires a modem model, which this backend cannot provide. So do `wifi.access_point(...)` and
`wifi.nearby(...)`, with `serial: wifi takes no model source on this backend, only real`: the real
board's station hears whatever access points are around it, and the host puts none on its air.
Reaching `board.wifi` alone does not skip. Reaching `board.bluetooth` does, at once, with
`serial: bluetooth takes no model source on this backend`: the real board's controller is on a real
air, where the harness has no device, and the capability declaration gives serial no `bluetooth`.

## Comparing the two backends

`scripts/compare-backends.py` runs the same tests on both backends and lists, for each test that
uses `board`, whether it ran on both and whether the two runs agreed. This is the specification's
"Every test that a real board can run, runs on one too, and the two results are compared"
(issue #1). It needs everything both backends need: the runner and the ROM ELF for esp32sim, the
board on the port for serial, and the builds of the apps the tests name.

```sh
.venv/bin/python scripts/compare-backends.py --port /dev/ttyUSB0
.venv/bin/python scripts/compare-backends.py --port /dev/ttyUSB0 tests/test_hello_world.py
ESPBAUD=230400 .venv/bin/python scripts/compare-backends.py --port /dev/cu.usbserial-5C854994151 tests/test_nmea_reader.py -k starts_uart
```

It runs `python -m pytest` twice on its other arguments, pytest's options included, or on
`tests/test_hello_world.py tests/test_nmea_reader.py` when it is given none. Options without a
path, as `-k hello` alone, select from pytest's `testpaths`, the whole suite: name the test files
as well. The first run is `--esp32tl-backend=serial --port=PORT`, the second
`--esp32tl-backend=esp32sim`, so that a port the plugin refuses (one with a wildcard, or an
`ESPPORT` that names another) ends the script before the esp32sim run. Without `--port` the
serial run would stop on `serial requires an explicit --port (or a parametrised port) without
wildcards: automatic port discovery is disabled`, so the script requires it before running
anything (`the following arguments are required: --port`, exit 2). Arguments after a `--` go
after the script's own options.

Each run writes its JUnit XML under `build/backends/` (`--out DIR` names another directory), as
`serial.xml` and `esp32sim.xml`; each run first removes its file from an earlier comparison. A
`--out` that is a file is refused with `✗ <DIR> is not a directory: name a directory with --out`,
exit 2. The serial run flashes the board as any serial run does
([builds and flashing](#builds-and-flashing)).

Tests that fail do not stop the script: their failures are what it compares. A run whose pytest
exits with any code other than 0 or 1 (interrupted, an internal or a usage error, or no test
collected), or leaves no JUnit XML, ends it before anything is compared, with
`✗ <backend>: pytest exited with code <code>; the backends were not compared`, exit 1. A missing
app build is not one of those: it fails the test at setup, exit 1, and the comparison shows it.

The two cases of a test (`test_hello_world[esp32sim]` and `test_hello_world[serial]`) are matched
by the test's id without the backend. A test whose id is the same in both runs does not use
`board`, and is left out. Each test that uses `board` gets one line, in the order of the esp32sim
run; an error at setup or teardown counts as a failure, and a message is its first line, cut after
200 characters:

| Line | Means |
|---|---|
| `✓ <test>: passed on both` | Ran on both and agreed |
| `✗ <test>: failed on both: on esp32sim: <message>; on serial: <message>` | Ran on both and agreed, but failed |
| `✗ <test>: passed on esp32sim, failed on serial: <message>` | Ran on both and disagreed; or the other way round |
| `- <test>: esp32sim only (passed): skipped on serial: <reason>` | The capability table skipped it on serial, as `serial: uart.gnss takes no replay source on this backend, only real`; `✗` and `(failed: <message>)` when it failed on esp32sim |
| `✗ <test>: serial only (passed): skipped on esp32sim: <reason>` | A test the real board ran and the emulator did not: there is no result to compare with |
| `- <test>: skipped on both: <reason>` | Ran on neither; the reason is the esp32sim run's |
| `✗ <test>: collected on esp32sim only` | One run collected a case the other did not; or the other way round |

The last line counts each kind of line, with `1 test` for one:

```text
backends: <n> tests ran on both backends, <a> agreed and <d> did not; <s> ran on esp32sim only, <r> on serial only, <k> on neither, <c> collected on one only; <l> tests without `board` left out
```

The script exits 1 when a line starts with `✗`, and when no test ran on both backends, with
`✗ no test ran on both backends: nothing was compared`; it exits 0 otherwise. Agreeing means the
same outcome, passed or failed: the script compares neither console output nor times, which run on
the board's emulated clock on one backend and on wall time on the other.

pytest-embedded opens the port at setup, before a test reaches the call that the capability table
skips. With no board on the port, every serial case therefore fails at setup, those that would skip
included, and the lines say
`passed on esp32sim, failed on serial: failed on setup with "esptool.util.FatalError: Could not open <PORT>, the port is busy or doesn't exist.`

## Validation status

The console smoke tests passed on 2026-11-02 on a LilyGO board with a classic ESP32-D0WD-V3
(revision 3.1) and 4 MB flash: `test_hello_world` and `test_starts_uart`, **2 passed**.
The USB adapter required `ESPBAUD=230400`. This verifies console capture, reset and autoflash;
it does not verify a GNSS receiver, modem or other physical peripherals. The reproducible
command, tool versions and app hashes are in
[physical validation](serial-backend-technical.md#physical-validation).

A PlatformIO build passed on 2026-11-15 on a LOLIN32 board with a classic ESP32-D0WDQ6
(revision 1.0) and 4 MB flash: the Arduino image of #193, `arduino_shell`, from its
`firmware.factory.bin` and from its separate files, each printing its banner and
`arduino_shell: ready; type help`, **2 passed**, with `ESPBAUD=230400`. Before the run with
separate files, the board's `ota_1` held another application and its otadata selected `ota_1`:
the board booted `arduino_shell` from `ota_0`, so the erased otadata reached it.
`scripts/compare-backends.py` reported both tests `passed on both`. The details are in
[physical validation](serial-backend-technical.md#a-platformio-build).

An ESP32-S3 (QFN56) revision v0.2 with 16 MB of flash on its USB-Serial/JTAG port passed on
2026-11-17 on macOS: `firmware/console_repl_usb` built for the S3, flashed and reset by the
backend, `test_the_usb_console_takes_a_command_and_comes_back_after_a_reset` and
`test_a_tap_of_the_usb_console_records_both_ways` of `tests/test_usb_console.py`, **2 passed**, 2
skipped (a tap of `uart.uart0` and the power switch, which the backend does not give). The details
are in [physical validation](serial-backend-technical.md#an-esp32-s3-on-its-usb-serialjtag-port).

## See also

- [Serial backend internals](serial-backend-technical.md)
- [pytest plugin](pytest-plugin-user.md)
- [Taps and run reports](run-report-user.md)
- [Running repository tests](../guides/testing.md)

`board.console.write(bytes_or_text)` writes to the open serial port after power-on. Text is
UTF-8; `sendline(line)` adds LF. Partial port writes are completed, and a zero-progress write
raises `BackendError`. Console taps record input at write time as well as received output.

`wifi.station(...)`, like `wifi.access_point(...)`, asks for a model on the Wi-Fi interface.
The serial backend has no virtual air and refuses that request through its existing capability
check; it cannot join a real device's SoftAP through esp32sim's virtual station. Wi-Fi replay is not a supported source in the capability table. The new station API is for the esp32sim backend only.

## pytest-xdist

Run serial tests with `-n 0` (or without xdist). Selecting `--esp32tl-backend serial`
with distributed execution, including one worker, fails during configuration with
`serial backend cannot run in pytest-xdist workers; use -n 0`, before any port is opened.
This does not lock hardware against other pytest invocations or external programs.
