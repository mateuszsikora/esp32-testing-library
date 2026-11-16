# esp32-testing-library

Test unmodified ESP32 firmware through its wires, in pytest.

esp32tl runs your ESP-IDF image, as built, in the [esp32sim](https://github.com/mikroverk/esp32sim)
emulator. The test sets up the world around the board, powers it on, and checks what the firmware
does on its pins, UARTs, SD card and network. In the emulator, time is the board's own clock, not
the wall's, so a run is fast and the same every time: all the tests of the GPS alarm clock below
run in a few seconds. On a real ESP32 on a serial port, the same `board` and `console` tests run
in wall time.

A shortened test from that example; `configure_alarm`, `TARGET` and the `buzzer` fixture are
defined in [its test file](https://github.com/mateuszsikora/esp32-testing-library/blob/main/examples/gps_alarm_clock/tests/test_alarm.py), not by esp32tl:

```python
def test_moving_into_the_radius_turns_it_on(board, buzzer):
    configure_alarm(board)  # the destination, in NVS
    board.gnss.fix(at=TARGET.moved(EAST, m(150)))  # a GNSS receiver 150 m away
    board.power_on()
    board.wait(seconds(3))
    buzzer.assert_off()

    board.gnss.move(heading=WEST, speed=km_h(36))  # drive towards the target
    board.wait(seconds(5))  # the 50 m to the radius
    board.wait_until(buzzer.is_on, within=seconds(2))
```

## Use in your own project

esp32tl is preparing its first PyPI release; the PyPI commands below are for use **after
publication**, not a claim that a release is already available. The project is alpha software.

With Python 3.11 or newer, install esp32tl from PyPI in your project directory, then explicitly
prepare its simulator runtime:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install esp32tl
esp32tl runtime prepare
pytest tests/
```

Build your firmware beforehand with ESP-IDF or PlatformIO: neither installation nor pytest builds
it. [Writing tests](https://github.com/mateuszsikora/esp32-testing-library/blob/main/docs/guides/writing-tests.md)
shows how to point tests at that build. No esp32tl checkout or Rust is needed for this path.

Prebuilt runtimes support Linux x86_64 with glibc (Ubuntu 22.04 or newer) and macOS arm64
(macOS 15 or newer). Windows, musl Linux and other architectures have no prebuilt runtime.
SD card image operations additionally need mtools: `brew install mtools` on macOS or
`sudo apt-get install mtools` on Debian/Ubuntu.

As an alternative to PyPI, download the `.whl` asset from a published
[GitHub release](https://github.com/mateuszsikora/esp32-testing-library/releases), install it with
`python -m pip install` followed by the downloaded wheel's filename, then run
`esp32tl runtime prepare`. Preparation downloads the runtime from `v<installed package version>`;
that GitHub release must be published even when installing a wheel locally. Draft assets are not
available anonymously. Before publication, use the developer path below.

[Runtime installation](https://github.com/mateuszsikora/esp32-testing-library/blob/main/docs/features/runtime-user.md)
covers version matching, offline reuse and CI.

## Your first green test

This developer path builds the firmware and runs the tests of the [GPS alarm clock](https://github.com/mateuszsikora/esp32-testing-library/tree/main/examples/gps_alarm_clock/),
a small ESP-IDF application that sounds a buzzer inside a radius of a destination.

You need Python 3.11 or newer, Rust through [rustup](https://rustup.rs), Docker running, and network
access to GitHub ([what each is for](https://github.com/mateuszsikora/esp32-testing-library/blob/main/docs/guides/development.md#requirements)).

```sh
git clone https://github.com/mateuszsikora/esp32-testing-library.git
cd esp32-testing-library

make setup                                       # .venv, the runner around esp32sim, the ESP32 ROM
firmware/build.sh --examples gps_alarm_clock     # the firmware, in the pinned ESP-IDF container
.venv/bin/pytest examples/gps_alarm_clock/tests  # its tests, in the emulator
```

The last command ends with:

```text
examples/gps_alarm_clock/tests/test_alarm.py .........                   [100%]

============================== 9 passed in 3.08s ===============================
```

Once Docker has the ESP-IDF image, the three commands take a few minutes; on a new machine the
first build also pulls the image, 7 GB once unpacked, which can take longer than the rest
([measured](https://github.com/mateuszsikora/esp32-testing-library/blob/main/docs/features/examples-user.md#build-and-run)).
With a local ESP-IDF's `export.sh` sourced,
`IDF_LOCAL=1 firmware/build.sh --examples gps_alarm_clock` builds without Docker
([a local ESP-IDF](https://github.com/mateuszsikora/esp32-testing-library/blob/main/docs/features/validation-firmware-user.md#build-with-a-local-esp-idf)).

### Make it fail

Open [`examples/gps_alarm_clock/tests/test_alarm.py`](https://github.com/mateuszsikora/esp32-testing-library/blob/main/examples/gps_alarm_clock/tests/test_alarm.py)
and bring the receiver of the first test inside the alarm's 100 m radius:

```python
board.gnss.fix(at=TARGET.moved(EAST, m(50)))  # was m(1000)
```

Run `.venv/bin/pytest examples/gps_alarm_clock/tests` again. The test fails with what went wrong,
on the board's clock; `assert_never_buzzed`, a helper of the example's test file, raises it:

```text
E               AssertionError: alarm.buzzer expected off after initialization (GPIO 18, level 0); first violation at 1.06781 s: level 1
```

Below it, the section `Captured esp32sim link teardown` ends with the run on the board's clock:
every step of the test and the last 200 entries of the console, the GNSS UART and the buzzer, in
the order they happened. An excerpt, with the boot log left out:

```text
the run on the board's clock, 69 entries:
  0.000000000 s  test              board.nvs(partition='nvs')
  0.000000000 s  test              uart.gnss.fix(at=Position(lat=52.22967699772281, lon=21.012967141827147), hdop=0.9, satellites=8)
  0.000000000 s  test              board.power_on(), the world's time 2026-01-01T12:00:00Z
  0.000000000 s  test              board.wait(5 s)
  …
  0.050060000 s  alarm.buzzer out  0
  0.053891600 s  console           gps_alarm_clock: ready
  1.000000000 s  uart.gnss in      $GPRMC,120001.00,A,5213.78062,N,02100.77803,E,0.00,,010126,,,A*40\r\n$GPGGA,120001.00,5213.78062,N,021... (139 bytes)
  1.067806337 s  alarm.buzzer out  1
  …
```

Undo the change with `git restore examples/gps_alarm_clock/tests/test_alarm.py`.

## What the test did

Read [the firmware](https://github.com/mateuszsikora/esp32-testing-library/blob/main/examples/gps_alarm_clock/main/main.c)
and [its tests](https://github.com/mateuszsikora/esp32-testing-library/blob/main/examples/gps_alarm_clock/tests/test_alarm.py)
side by side. In each test:

- `pytest.mark.app_path("..")` names the ESP-IDF project; the test runs its `build/` directory.
  `board_profile("gps-alarm-clock")` says which UART the GNSS receiver is on and which pin is the
  buzzer.
- `configure_alarm(board, ...)`, a helper of the test file, turns a `Position` and a radius into
  the firmware's NVS schema and writes it with `board.nvs(...)` before power-on, where the
  firmware reads it.
- `gnss.fix(...)`, `gnss.move(...)` and `gnss.lose_fix()` drive a model of a GNSS receiver, which
  writes NMEA sentences into the firmware's UART.
- `board.wait(...)` and `board.wait_until(...)` let the firmware run on the board's clock; the
  emulated board runs only while the test waits.
- `buzzer` is a fixture of the test file, `lines.output("alarm.buzzer")`: the GPIO line the
  firmware drives, read from outside. `assert_never_buzzed`, another helper there, checks every
  level the line took, not only the last one.

Nothing in the firmware changes for the test, and the test reads nothing inside it.

## Where to go next

- **Test your own firmware**: [writing tests for your firmware](https://github.com/mateuszsikora/esp32-testing-library/blob/main/docs/guides/writing-tests.md)
  goes from where the tests go to running them in CI.
- **More examples**, each with readable tests ([how to build and run them](https://github.com/mateuszsikora/esp32-testing-library/blob/main/docs/features/examples-user.md)):
  an [SD card data logger](https://github.com/mateuszsikora/esp32-testing-library/tree/main/examples/sd_data_logger/),
  a [position tracker](https://github.com/mateuszsikora/esp32-testing-library/tree/main/examples/position_tracker/)
  sending MQTT over TLS through a cellular modem, a
  [periodic deep-sleep wake](https://github.com/mateuszsikora/esp32-testing-library/tree/main/examples/periodic_wake/)
  and an [OBD-II reader](https://github.com/mateuszsikora/esp32-testing-library/tree/main/examples/obd_reader/)
  over Bluetooth Classic.
- **The fixtures, markers and options**: [the pytest plugin](https://github.com/mateuszsikora/esp32-testing-library/blob/main/docs/features/pytest-plugin-user.md).
- **Everything else**: [`docs/README.md`](https://github.com/mateuszsikora/esp32-testing-library/blob/main/docs/README.md)
  says what lives where.

## What it can do today

- Run an unmodified ESP-IDF image for the classic ESP32 in esp32sim, or the same `board` and
  `console` tests on a real ESP32 on a serial port
  ([the serial backend](https://github.com/mateuszsikora/esp32-testing-library/blob/main/docs/features/serial-backend-user.md)).
- Run a PlatformIO build, `.pio/build/<env>/`, as `pio run` left it, in esp32sim or flashed on
  the real board
  ([builds](https://github.com/mateuszsikora/esp32-testing-library/blob/main/docs/features/builds-user.md)).
- Feed a UART from a model, a recording, or a real device on a host serial port, such as a GNSS
  receiver on a USB adapter.
- Drive and read GPIO lines, read the console, and write the NVS partition before power-on.
- Put an SD card holding a FAT image on the SPI pins.
- Model a cellular modem the firmware talks AT to, and a network behind it or behind a Wi-Fi
  access point, with DNS, NTP, an MQTT broker and an HTTP server in the test.
- Wake the firmware from deep sleep on a timer, and run a day of wakes in seconds.
- Put a Bluetooth Classic peer with the Serial Port Profile beside the board, which the firmware
  finds, pairs with and talks to.

The `esp32tl` command runs an image outside pytest ([the CLI](https://github.com/mateuszsikora/esp32-testing-library/blob/main/docs/features/cli-user.md)).

## Working on esp32tl

`make test` sets everything up and runs the whole suite, the repository's own tests and every
example; the first run builds all the firmware in Docker, which takes much longer than one example,
and the SD card tests need mtools
([the development guide](https://github.com/mateuszsikora/esp32-testing-library/blob/main/docs/guides/development.md),
[the test suite](https://github.com/mateuszsikora/esp32-testing-library/blob/main/docs/guides/testing.md)).
Contributions follow [`CONTRIBUTING.md`](https://github.com/mateuszsikora/esp32-testing-library/blob/main/CONTRIBUTING.md);
a change that alters behaviour updates its documentation in the same pull request.
