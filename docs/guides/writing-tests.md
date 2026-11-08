# Writing tests for your firmware

For someone who has an ESP-IDF project and wants to test the firmware it builds, unmodified,
through its wires. It goes from the first test to a suite that runs in CI. Each section shows the
idea and links to the feature document that has the details, and to the example in
[`examples/`](../../examples/README.md) that shows it in a whole project. Working on esp32tl itself
is [development.md](development.md).

A test sets up the world around the board, powers the board on, and waits on the board's own
clock for what the firmware does on its wires: its console, its pins, the bytes on its UARTs, the
files on its SD card, and what reaches the services it talks to. Nothing in the firmware changes
for the test, and the test reads nothing inside the firmware
([decision](../decisions/harness.md#tests-observe-the-device-from-outside-and-product-knowledge-stays-in-the-consumers-tests)).

## Where the tests go

Put them beside the project, as the examples do:

```text
my_project/
  CMakeLists.txt
  main/
  sdkconfig.defaults
  build/            # what idf.py build leaves; the tests run it
  tests/
    boards/my-board.toml
    test_my_project.py
```

A test file names the app and the board it runs on:

```python
import pytest

pytestmark = [
    pytest.mark.app_path(".."),
    pytest.mark.board_profile(path="boards/my-board.toml"),
]
```

- `app_path("..")` is the project, relative to the test file. The tests run the build in its
  `build/` directory, or the one `build_dir=` names. esp32tl never builds it: build it first, for
  the `esp32` target, without flash encryption
  ([the `app_path` marker](../features/pytest-plugin-user.md#writing-a-test)).
- The board profile says which UART and pins each part of your board uses. esp32tl ships four
  (`esp32-devkitc`, the default, `gps-alarm-clock`, `sd-data-logger`, `lilygo-t-a7670g-r2`); for
  your own wiring, write a TOML file beside the tests and select it with `path=`
  ([a project-local profile](../features/board-profiles-user.md#a-project-local-profile)).
- Each example has a profile of its own for its assembly: the
  [GPS alarm clock](../features/examples-user.md#gps-alarm-clock) uses `gps-alarm-clock`.

pytest loads the plugin by itself once esp32tl is installed ([In CI](#in-ci) says how). In your
`pyproject.toml`, `-ra` shows why a test was skipped, and one filter silences a warning
pytest-embedded causes for every test
([what a failure looks like](../features/pytest-plugin-user.md#what-a-failure-looks-like)):

```toml
[tool.pytest.ini_options]
addopts = ["-ra"]
filterwarnings = ["ignore:record_xml_attribute is an experimental feature:pytest.PytestExperimentalApiWarning"]
```

## Power-on

```python
from esp32tl import Board


def test_boots(board: Board) -> None:
    board.power_on()
    board.console.expect_exact("my_project: ready")
```

`board` is off until the test calls `board.power_on()`, which starts the run at 0 on the board's
clock. From then on the firmware runs only while the test waits, and stands still while the test
body runs: a test can read anything between two waits without the firmware moving under it.
`board.power_on(utc=datetime(...))` sets the world's time at power-on, `2026-01-01T12:00:00Z`
unless given; the GNSS receiver model's sentences, NTP and the modem's `AT+CCLK?` all tell it.

`board.reset()`, `board.power_off()` and `board.power_on()` again restart the board within the
same run, and `board.boots` says why each boot happened
([resets and power cycles](../features/pytest-plugin-user.md#resets-and-power-cycles)).

## The world before power-on

What is at the far end of each wire is what the test does with it, and most of it comes before
the first `board.power_on()`:

- A UART route's source (a model, a replay or a real device), the Wi-Fi access point and a bridge
  are given before it. One given later is refused with `SourceError`, and so is a second source on
  the same route.
- `board.nvs(...)` is written before it too; after it, it raises
  `BackendError: board.nvs: write the flash before board.power_on()`. A second call for the same
  partition replaces the first.
- A card in the SD card slot and the levels on the lines can be given before power-on, and changed
  at any time after it.

A test that sets something up and never powers on fails at teardown, since it ran no firmware.

| Your board has | The test gives it | Shown in |
|---|---|---|
| Configuration in NVS | `board.nvs({"namespace": {"key": value}})`, integers with their width, `u16(8883)` from `esp32tl.flash` ([flash contents](../features/flash-contents-user.md)) | `configure_alarm` in the [GPS alarm clock](../../examples/gps_alarm_clock/tests/test_alarm.py), `configure_tracker` in the [position tracker](../../examples/position_tracker/tests/tracker_support.py) |
| A GNSS receiver | `board.gnss.fix(at=...)`, `move(...)`, `lose_fix()`: a receiver the test describes ([models](../features/models-user.md#the-gnss-receiver)); `replay(path)`: a recording ([replay](../features/replay-user.md)); `real(port)`: a device on a host serial port ([real source](../features/real-source-user.md)) | The model in the [GPS alarm clock](../../examples/gps_alarm_clock/tests/test_alarm.py); a replay in the [SD data logger](../../examples/sd_data_logger/tests/test_logger.py) |
| An SD card | `board.sd_card.image(size_mb=32).with_text(name, text).build()`, then `board.sd_card.insert(card)`; nothing, for an empty slot ([SD card](../features/pytest-plugin-user.md#the-fixtures)) | [SD data logger](../../examples/sd_data_logger/tests/test_logger.py) |
| A cellular modem | `board.modem.signal(dbm=...)`, `register()`, `power_off()` and the model's other calls ([modem](../features/modem-user.md)); `board.modem.network()` for the network behind it | The network in the [position tracker](../../examples/position_tracker/tests/tracker_support.py); the model's calls in `tests/test_modem_fixture.py` |
| Wi-Fi | `board.wifi.access_point(ssid, password=...)`, which returns the network behind it ([the wifi fixture](../features/pytest-plugin-user.md#the-wifi-fixture)) | `tests/test_http_request.py` |
| Services on the network | The network's broker, HTTP server, DNS and NTP, all there by default; `network.listen(port, service, tls=ca)` for TLS or a service of your own; `network.bridge(port, to=...)` for a real one ([network](../features/network-user.md)) | [Position tracker](../../examples/position_tracker/tests/tracker_support.py) |
| Inputs on GPIO lines | `board.lines.drive(line, level)`, which holds from power-on when called before it ([lines](../features/pytest-plugin-user.md#the-fixtures)) | `tests/test_gpio_echo.py` |

Until the test gives an interface a source, it listens and says nothing: a UART route hears what
the firmware sends and answers nothing, and the SD card slot is empty. The models (the GNSS
receiver, the modem, the network and the card) take changes after power-on too, at the board's
current instant: `board.gnss.lose_fix()` in the middle of a test is the receiver losing its fix
then.

## Waiting for the device's reactions

Every wait is on the board's clock, and every limit a test states is part of what it tests:

| To wait for | Write | It returns |
|---|---|---|
| Time to pass | `board.wait(seconds(5))` | At exactly that instant: `board.now` moved by 5 s |
| Text on the console | `board.console.expect_exact("ready", timeout=seconds(10))` | The match |
| A console line, with its time | `board.wait_until(board.console.line(r"^fix lat=(?P<lat>\S+)"), within=seconds(2))` | A `Line`, with `at`, `text` and the groups |
| A pin | `buzzer = board.lines.output("alarm.buzzer")`, then `board.wait_until(buzzer.is_on, within=seconds(2))` | `True` |
| A message at the broker | `board.wait_until(broker.next_message("tracker/position"), within=seconds(60))` | The message, with `at` |
| Anything else | `board.wait_until(lambda: ..., within=...)`, any function of no arguments | What it returned, once true |

- `board.wait_until` asks the condition at once, then at least every 100 ms of the board's time. A
  condition that never holds raises `NeverHappened` when `within` runs out
  ([the fixtures](../features/pytest-plugin-user.md#the-fixtures)).
- A `next_*` condition of the network sees only what happens after the call that makes it, so make
  it before the action that causes the event, as `test_reports_on_connect` in the position
  tracker does with `network.broker.next_connection()`
  ([history, next events and current state](../features/network-user.md#history-next-events-and-current-state)).
- To check a state over a whole interval, not only at its end, keep the instant it started and
  assert on the line's history: `buzzer.assert_stayed_off(since=off_since)` fails on a pulse the
  waits did not see ([named output conditions](../features/pytest-plugin-user.md#named-output-conditions)).
  `test_losing_the_fix_turns_it_off` in the GPS alarm clock does that.
- Files on the SD card are read with `card.read_text(name)` after a wait long enough for the
  firmware to have closed them; the SD data logger waits half a fix interval past the last fix
  (`WRITE_MARGIN`).
- Time a step from the instant the world changed, not from power-on: the GPS alarm clock works out
  when the receiver crosses the radius, waits until one fix before it, checks the old state, and
  only then starts the reaction deadline.

The board's time and the wall clock are apart. A test also has a wall-time timeout, 600 s unless
pytest-timeout's options set another, which says only that something is stuck
([the test's timeout](../features/pytest-plugin-user.md#the-tests-timeout)). A run with a real
source or a bridge to a real service runs on the wall clock instead.

## Units

Every quantity a test states carries its unit, from `esp32tl.units`: `seconds(2.5)`, `ms(70)`,
`km_h(36)`, `m(100)`, `Position(52.229677, 21.012233)`, `NORTH`. A plain number where a unit
belongs raises `TypeError`, as in `board.wait takes a Duration, as seconds(2.5), not 2.5`. Physics
works across kinds: `(m(150) - m(100)) / km_h(36)` is the `Duration` the receiver takes to cover
50 m, which is how the GPS alarm clock times its crossings. Compare with a tolerance through
`about`:

```python
assert report.position.distance_to(expected) == about(m(0), within=m(15))
assert report.received_at == about(deadline, within=seconds(0.25))
```

[Models and units](../features/models-user.md#units) lists every unit and what mixes with what.

## What a failure tells you

- **`NeverHappened`** says what the test waited for, how long, and from when to when on the board's
  clock: `alarm.buzzer is on (GPIO 18, level 1) did not happen within 2 s`, then the window. A
  function or a lambda is named by its qualified name, `the condition test_x.<locals>.<lambda>`;
  give a condition object a `__repr__` and the message names it in your words: the position
  tracker's says `a tracker report with a position`.
- **The run on the board's clock.** A failed test's `esp32sim link` report section ends with every
  step of the test and the last 200 entries of the console, the routes and the lines, in the order
  they happened ([the run on the board's clock](../features/run-report-user.md#the-run-on-the-boards-clock)).
  Behind a modem or an access point it also has the network's transcript: DNS lookups, TCP
  connections, TLS and MQTT ([failures and reports](../features/network-user.md#failures-and-reports)).
  `pytest -rA` prints the section for a test that passed too, without the run on the board's
  clock.
- **`RunEnded`**: the runner exited while the test waited, with its exit code and its last output
  ([when the runner exits](../features/esp32sim-backend-user.md#when-the-runner-exits)).
- **A skip** names the backend and what it cannot give, such as
  `serial: uart.gnss takes no replay source on this backend, only real`
  ([when a backend cannot give it](../features/board-profiles-user.md#when-a-backend-cannot-give-it)).
- **An error at setup** starting with `esp32sim: ` is about the build, the runner or the ROM ELF,
  with what to do ([when the backend cannot start](../features/esp32sim-backend-user.md#when-the-backend-cannot-start)).
- **A tap** keeps what crossed a route as a recording: `board.gnss.tap(path)` before power-on
  writes it when the run ends, and `replay(path)` plays it back in a later test
  ([replaying a tap](../features/run-report-user.md#replaying-a-tap)).

## Knowledge of your product stays in your tests

esp32tl knows protocols (NMEA, AT, PPP, MQTT, HTTP) and the ESP-IDF platform (its partition table,
NVS), never what your product means by them. Which NVS keys hold your configuration, which topic
carries your report and what its JSON says belong to small helpers in your own tests, written in
your product's words:

- `configure_alarm(board, target=..., radius=...)` in the
  [GPS alarm clock](../../examples/gps_alarm_clock/tests/test_alarm.py) turns a `Position` and a
  `Distance` into the firmware's NVS keys.
- `serve_broker(board)`, `configure_tracker(board, trusted_ca=...)` and `Reports(network.broker)` in
  the [position tracker's support module](../../examples/position_tracker/tests/tracker_support.py)
  serve the broker over TLS, write the NVS schema, and turn MQTT messages into `Report`s with a
  `Position` and a UTC time. Its conditions reject a malformed report rather than skip it, and
  raise `TypeError` when used as a boolean without being called.

Keep `board` the only clock and power control: a helper prepares the world or reads what happened,
and leaves the waiting to the test. The examples' own documents say why each helper looks the way
it does ([examples](../features/examples-user.md)).

## Writing a model

A model answers the device the way the real part would, from what the test told it. The GNSS
receiver and the modem esp32tl ships are models; so is anything a test writes in the same shape.
Where a test can put a model of its own today:

- **A service on the simulated network.** `network.listen(port, service)` puts it on a TCP port of
  every address, and `tls=CertificateAuthority([...])` wraps it in TLS, so the service sees the
  plaintext. A service makes a connection for each one the device opens; the connection gets the
  device's bytes and answers through `write`:

  ```python
  class SilentBroker:
      """Takes every connection and answers nothing, as a broker hung before its CONNACK."""

      def __init__(self) -> None:
          self.connections: list[Connection] = []
          self.open_when_accepted: list[int] = []  # Connections still open at each accept.

      def accept(
          self, peer: tuple[str, int], write: Callable[[bytes], None], close: Callable[[], None]
      ) -> "Connection":
          self.open_when_accepted.append(sum(not c.closed for c in self.connections))
          self.connections.append(Connection())
          return self.connections[-1]


  class Connection:
      def __init__(self) -> None:
          self.heard = bytearray()
          self.closed = False

      def on_data(self, data: bytes) -> None:
          self.heard += data

      def on_close(self) -> None:
          self.closed = True


  def test_a_broker_that_never_answers_gets_tried_again(board: Board) -> None:
      network = board.modem.network()
      broker = SilentBroker()
      ca = CertificateAuthority([BROKER_HOST])
      network.listen(BROKER_PORT, broker, tls=ca)
      configure_tracker(board, trusted_ca=ca.ca_pem)
      board.power_on(utc=WORLD)

      board.wait_until(lambda: broker.connections and broker.connections[0].heard, within=STARTUP)
      connect = bytes(broker.connections[0].heard)  # The whole CONNECT: it comes in one TLS record.
      assert connect[0] == 0x10  # MQTT CONNECT
      assert b"position-tracker" in connect
      board.wait_until(lambda: len(broker.connections) > 1, within=RECONNECT_WINDOW)
      assert broker.open_when_accepted[1] == 0  # The first was closed before the second opened.
  ```

  This is the [position tracker](../../examples/position_tracker/tests/test_tracker.py)'s, on port
  8883 over TLS; the constants, `Callable` and `configure_tracker` come from that file and its
  `tracker_support.py`. The firmware's MQTT client sends its CONNECT, hears nothing, closes the
  connection, and connects again.
  `write(bytes)` answers the device and `close()` ends the connection. UDP services go in
  `network.ip.udp[port]`, with `on_datagram(bytes)`, which returns the reply or `None`. The
  callbacks run inside the test's waits, while the board's clock advances, so they must not block
  or wait on the wall clock
  ([DNS, time and services](../features/network-user.md#dns-time-and-services)).
- **The far end of a Bluetooth SPP channel.** `SppPeer(air, name=..., pin=..., source=...)` takes
  a source with `receive(t, data)`, `poll(now)`, `quiet_until()` and `latency()`, times in
  nanoseconds ([the model that answers](../features/bluetooth-peer-user.md#the-model-that-answers)).
  esp32sim has no Bluetooth Classic controller, so no firmware talks to a peer yet; esp32tl's own
  tests drive one from Python (`tests/test_spp_peer.py`).
- **The far end of a UART.** `board.uart(role).model(source)`, before power-on, puts a model of
  your own on any `uart.<role>` route of the profile, for a sensor, a display controller or a
  second modem esp32tl has no model for. The source has the same four methods as an SPP peer's;
  what it returns from `receive` and `poll` goes into the chip at its own time:

  ```python
  ANSWER = ms(1).ns


  class AtResponder:
      """Answers every command line with OK, ANSWER after its CR LF."""

      def __init__(self) -> None:
          self.lines: list[bytes] = []
          self._heard = b""

      def receive(self, t: int, data: bytes) -> Iterable[Output]:
          self._heard += data
          answers = []
          while b"\r\n" in self._heard:
              line, _, self._heard = self._heard.partition(b"\r\n")
              self.lines.append(line)
              answers.append((t + ANSWER, b"\r\nOK\r\n"))
          return answers

      def poll(self, now: int) -> Iterable[Output]:
          return []

      def quiet_until(self) -> int | None:
          return None

      def latency(self) -> int | None:
          return ANSWER


  def test_the_firmware_talks_to_the_test_s_model_on_its_own_uart(board: Board) -> None:
      part = board.uart("aux").model(AtResponder())
      board.power_on()
      board.console.expect_exact("modem_at: dialogue complete", timeout=seconds(5))
      assert part.lines[1] == b"ATE0"
  ```

  The route is one your profile names, `boards/my-board.toml` of the test file's `pytestmark`
  ([where the tests go](#where-the-tests-go)), with the UART's port, pins and baud:

  ```toml
  [uart.aux]
  port = 1
  tx = 26                   # ESP32 TX → the part's RX
  rx = 27
  baud = 115200
  ```

  This is esp32tl's own `tests/test_uart_model.py`, shortened, where `Iterable` comes from
  `collections.abc`, `Output` from `esp32tl.link.boardlink`, and `ms` and `seconds` from
  `esp32tl.units`: its firmware, `modem_at`, writes AT commands to `uart.aux` of
  `tests/boards/aux-uart.toml` and reads the answers. `latency()` is the shortest time the model
  takes to answer, and the board runs that far ahead of it; an answer stamped sooner fails the run.
  `poll(now)` and `quiet_until()` are for what the part sends unprompted, as a sensor reporting
  every second: `quiet_until()` says when the next report is due, and `poll` returns it then. Add
  `supply(on)` for a part on the board's supply: `board.power_off()` and the `power_on()` after it
  call it, as they switch the GNSS receiver and the modem. `model` returns the source, so the test
  reads what it heard; the details and what the call refuses are in
  [a UART's own model](../features/pytest-plugin-user.md#a-uarts-own-model).

  Instead of a model, `board.uart(role).replay(path)` gives the route a recording: requests in AT,
  PMTK or UBX get their recorded answers, and a recording with no requests plays at its recorded
  times ([replay](../features/replay-user.md), [matching](../features/matching-user.md)). An
  ordinary run takes a recording on the emulated clock only, written to the
  [recording format](../recording-format.md) as `scripts/generate-gnss-recordings.py` writes those
  of `uart.gnss`. What a `tap` records while a `real(port)` device is on the route is on the wall
  clock, and replays only in a run on the wall clock
  ([real source](../features/real-source-user.md#recording-what-crosses-the-route)).

A model belongs in your tests when it speaks your product's vocabulary, as a service that answers
your backend's API does; one that speaks only a protocol or a part's datasheet belongs in esp32tl.

## In CI

A job that runs the tests needs Python 3.11 or newer, Rust through [rustup](https://rustup.rs), a
checkout of esp32tl at a commit you pin, and your firmware's build. From a fresh machine:

```sh
git clone https://github.com/mateuszsikora/esp32-testing-library esp32tl
git -C esp32tl checkout <commit>
python3 -m venv .venv
.venv/bin/pip install ./esp32tl
make -C esp32tl build-runner rom-elf
export ESP32TL_RUNNER=$PWD/esp32tl/runner/target/release/esp32tl-runner
export ESP32TL_ROM=$PWD/esp32tl/.cache/esp-rom-elfs/20241011/esp32_rev300_rom.elf
idf.py -C my_project build                # or your own build, in your own container
.venv/bin/pytest my_project/tests
```

- `pip install ./esp32tl` installs the package and its pytest plugin; it does not build the
  runner. `make build-runner` does, with `cargo build --locked` in `esp32tl/runner/`; rustup
  installs the toolchain `runner/rust-toolchain.toml` pins on the first build, and cargo fetches
  esp32sim from GitHub ([development](development.md#make-targets)).
- `make rom-elf` puts the ESP32 mask ROM ELF in `esp32tl/.cache/`, from `~/.espressif` when ESP-IDF
  has it, else from GitHub, checked by SHA-256 ([the ROM ELF](development.md#the-rom-elf)).
- An installed esp32tl finds the runner and the ROM ELF only through `ESP32TL_RUNNER` and
  `ESP32TL_ROM`. With `pip install -e ./esp32tl` instead, it finds them in the checkout and needs
  neither ([what it needs](../features/esp32sim-backend-user.md#what-it-needs)).
- The SD card's image builder needs mtools: `sudo apt-get install mtools`.
- Your build needs `flasher_args.json` and every file it lists; a CI job that builds the firmware in
  one job and tests it in another hands over the files at the top of `build/`,
  `config/sdkconfig.json` and the `.bin` and `.elf` files of `bootloader/` and `partition_table/`,
  as esp32tl's own CI does ([`firmware`](../cicd.md#firmware)).

What to cache, and on what key:

| What | Key | Without it |
|---|---|---|
| `esp32tl/runner/target/release/esp32tl-runner` | The esp32tl commit | `make build-runner` builds it again; a cold build takes under a minute on esp32tl's CI machine ([`runner`](../cicd.md#runner)) |
| `esp32tl/.cache/esp-rom-elfs/` | The esp32tl commit, or `scripts/fetch-rom-elf`'s hash | `make rom-elf` downloads it again |
| Your firmware's build | Its sources and `sdkconfig.defaults` | It is built again |
| `.pytest_cache/d/esp32tl-card-cache/`, if you use `card_cache` | Any: each card is kept under its name and the SHA-256 of the app image, so a new build misses by itself | Each card is prepared again ([keeping a prepared card](../features/pytest-plugin-user.md#keeping-a-prepared-card)) |

A run on esp32sim without a real source or a bridge repeats on one machine: the board's clock
moves only while the test waits, and everything the far ends send comes from a model or a
recording ([determinism](testing.md#determinism)). esp32tl checks that one machine runs a test the
same way twice, not that two machines agree ([`determinism`](../cicd.md#determinism)): a failure in
CI is a run to reproduce locally from the same build and the same esp32tl commit, not one known
to repeat there.

## Not covered yet

- **Bluetooth Classic.** No firmware talks to a Bluetooth peer, as esp32sim has no Classic
  controller ([Bluetooth peer](../features/bluetooth-peer-user.md)).
- **Skipping a deep sleep.** A firmware that sleeps with a timer wake wakes on the pinned
  esp32sim ([stage 0, gap 4](../stage0-findings.md#gaps)), but the test's clock has no call that
  runs a sleeping board to its next wake: the test waits through the sleep as through any other
  time (#36).

## See also

- [Runnable examples](../features/examples-user.md): the three projects this guide links to.
- [pytest plugin](../features/pytest-plugin-user.md): every fixture, marker and message.
- [Serial backend](../features/serial-backend-user.md): the same tests on a real board.
- [Decisions](../decisions/README.md): why esp32tl works the way it does.
