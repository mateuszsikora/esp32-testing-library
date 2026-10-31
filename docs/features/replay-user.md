# Replay

A replay puts a [recording](../recording-format.md) at the far end of one of the board's
interfaces: the firmware receives what the recording holds, at the times it holds it, as if the
device that sent it were wired to the board. A GNSS receiver's sentences replayed into
`uart.gnss` reach the firmware's UART on the emulated clock, so a test of an hour's drive runs in
seconds and the same recording gives the same console every time.

This version replays streams only: recordings with nothing but events into the ESP32, such as
NMEA sentences. A recording with requests and answers (a modem's AT commands) is refused.

## Replaying a GNSS recording

```python
import pytest

pytestmark = pytest.mark.app_path("../firmware/nmea_reader")


@pytest.mark.source("uart.gnss", "replay")
def test_reports_a_fix(gnss, console):
    gnss.replay("../recordings/gnss-generated-1.jsonl")
    console.expect_exact("fix acquired lat=52.229677 lon=21.012233")
```

- The `source` marker says that a replay is at the far end of `uart.gnss`
  ([board profiles](board-profiles-user.md#saying-what-a-test-needs)). Without it, `gnss.replay`
  is refused; on a backend that cannot replay into `uart.gnss`, the test is skipped.
- `gnss` is the far end of the board profile's `uart.gnss` route: on the default `esp32-devkitc`
  profile UART1, the ESP32's RX on GPIO 5, at 9600 baud
  ([board profiles](board-profiles-user.md#the-profiles-esp32tl-ships)).
- `gnss.replay(path)` takes a path relative to the test file's directory, as the `app_path`
  marker does, or an absolute one. Call it before anything waits on the console: until it is
  called the run waits, before the firmware's first instruction. The wait is bounded: after 60
  wall seconds without the replay (a debugger stopped in the body, a slow fixture set up after
  `board`) the runner gives up on the harness and exits with code 4, and the next expect fails
  with `the runner exited with code 4`.
- Another route of the profile replays the same way, through `board.uart(role)`, as in
  `board.uart("modem").replay(path)` with `@pytest.mark.source("uart.modem", "replay")`; `gnss` is
  `board.uart("gnss")`.
- It returns the replay; its `sent` attribute counts the events the run has been given so far.

## What a replay does

- Each event goes into the ESP32 on the route's RX pin at the route's baud, from the board
  profile, starting at its recorded time `t`: emulated nanoseconds since power-on. The run is
  held at power-on until `replay` is called, so an event at `0` is in time, and two runs of one
  recording give the same console.
- Nothing goes in before its time, and the run stops at each event's time to send it: a recording
  of one sentence a second costs one exchange with the runner per second of emulated time.
- What the firmware transmits on the route's TX pin is heard and dropped: a stream answers
  nothing.
- After the last event the far end is silent, and the run goes on until the test ends it or 300
  emulated seconds have passed ([esp32sim backend](esp32sim-backend-user.md#how-a-run-goes)).
- Bytes that reach the UART before the firmware has configured it are dropped, as on a wire; the
  runner reports them as an `unrouted_rx` warning in the
  [link's report](esp32sim-backend-user.md#the-links-report). A firmware that configures the UART
  at another baud than the profile's sees frame errors, reported as `frm_err`.

## What a replay refuses

`gnss.replay(path)` reads the recording first and raises, in the test body, when it cannot be
replayed; the run is then stopped before the firmware boots. A recording that breaks the format
raises `RecordingError`, with the file and the line
([its messages](../recording-format.md#errors)). One that is well formed but cannot be replayed
here raises `ReplayError`, `{path}` being the path as the test gave it:

| Message | Cause |
|---|---|
| `{path} is a recording of {interface}, not of uart.gnss` | The header's `interface` is another one |
| `{path} has times on the wall clock, and uart.gnss runs on the emulated-fifo-write clock: replaying it would compare times across clocks` | The recording was made on a real board or device. `{clocks}` lists every clock it uses, `emulated-fifo-write and wall` for one with a clock switch |
| `{path} has events out of the ESP32, the first at {t} ns: it holds requests, and replay answers none yet, only streams with events in` | An `out` event: the recording holds requests |
| `{path} ends with loop: replay holds at the end only` | `end` is `loop` |

## When the test gets it wrong

These raise `SourceError` (`esp32tl.interfaces.SourceError`) in the test body:

| Message | Cause |
|---|---|
| `uart.gnss has no source marker that names replay: mark the test @pytest.mark.source("uart.gnss", "replay") to replay into it` | `gnss.replay` without the marker |
| `uart.gnss has its source already: one per test` | A second `gnss.replay` in one test |
| `the run has not started: it waits for the replay of uart.gnss, which the source marker declares; give it before waiting on the console` | `console.expect` or `console.expect_exact` before `gnss.replay`: the run could never get to what they wait for, so they fail at once rather than after their timeout. pytest-embedded's own `dut.expect` does not check, and waits out its timeout |

These are errors at setup, and the test body does not run:

| Message | Cause |
|---|---|
| `esp32sim: uart.gnss: a model source is not implemented yet; the backend gives replay` | A `source` marker with `model` or `real`: the backend declares them, and does not give them yet |
| `esp32sim: the board profile lilygo-t-a7670g-r2 has no uart.gnss; its interfaces are console, uart.modem, lines.modem` | The test uses `gnss` on a profile without that route |

A test that declares a replay and never calls `gnss.replay` ran no firmware, and would pass
whatever the firmware does: the run ends at teardown before the firmware's first instruction, the
link's report says `stats: the run ended at 0.000000000 s (stop); 1 turn, 0 resets`, and the test
gets an error at teardown, even when its body passed:

```
esp32sim: the run never started: it waited for the replay of uart.gnss, which the source marker declares; give it in the test body
```

A test with an outcome of its own does not get it as well: one that failed (on a `SourceError`
or a `ReplayError`, for instance), was skipped with `pytest.skip`, xfailed, or whose other
fixtures failed at setup.

## In the link's report

With a replay, the first line of the [link's report](esp32sim-backend-user.md#the-links-report)
names the route's pins, `board esp32-devkitc, watching GPIO 4, GPIO 5`, and the runner takes one
turn at power-on and one for each recorded time it reached.

## See also

- [Replay: how it works](replay-technical.md)
- [Recording format](../recording-format.md)
- [pytest plugin](pytest-plugin-user.md): the `gnss` fixture
- [Validation firmware](validation-firmware-user.md): `nmea_reader` and the generated GNSS recordings
