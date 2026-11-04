# Replay

A replay puts a [recording](../recording-format.md) at the far end of one of the board's
interfaces: the firmware receives what the recording holds, at the times it holds it, as if the
device that sent it were wired to the board. A GNSS receiver's sentences replayed into
`uart.gnss` reach the firmware's UART on the emulated clock, so a test of an hour's drive runs in
seconds and the same recording gives the same console every time.

Streams such as NMEA sentences play at their absolute recorded times. Recordings with requests
also answer firmware commands, preserving their relative timing and recorded timeouts. The
[matching engine](matching-user.md) supplies AT, PMTK and UBX defaults; use
`gnss.replay(path, protocol=PMTK)` to choose one explicitly.

## Replaying a GNSS recording

```python
import pytest

pytestmark = pytest.mark.app_path("../firmware/nmea_reader")


def test_reports_a_fix(board, gnss, console):
    gnss.replay("../recordings/gnss-generated-1.jsonl")
    board.power_on()
    console.expect_exact("fix acquired lat=52.229677 lon=21.012233")
```

- Calling `gnss.replay` is what puts a replay at the far end of `uart.gnss`; no marker says it
  again. On a backend that cannot replay into `uart.gnss`, the test is skipped at the call
  ([board profiles](board-profiles-user.md#saying-what-a-test-needs)).
- `gnss` is the far end of the board profile's `uart.gnss` route: on the default `esp32-devkitc`
  profile UART1, the ESP32's RX on GPIO 5, at 9600 baud
  ([board profiles](board-profiles-user.md#the-profiles-esp32tl-ships)).
- `gnss.replay(path)` takes a path relative to the test file's directory, as the `app_path`
  marker does, or an absolute one. Call it before `board.power_on()`: the replay starts with the
  run, at the board's time 0.
- Another route of the profile replays the same way, through `board.uart(role)`, as in
  `board.uart("modem").replay(path)`; `gnss` is `board.uart("gnss")`.
- It returns the replay; its `sent` attribute counts the events the run has been given so far.

## What a replay does

- Each unprompted event goes into the ESP32 on the route's RX pin at the route's baud, from the
  board profile, starting at its recorded time `t`: emulated nanoseconds since power-on. The replay is
  given before power-on, so an event at `0` is in time, and two runs of one recording give the
  same console.
- Nothing goes in before its time, and the run stops at each event's time to send it: a recording
  of one sentence a second costs one exchange with the runner per second of emulated time.
- A pure stream with no explicit `protocol` ignores firmware transmissions. Recordings with
  requests match commands and schedule the recorded answer relative to the completed command.
  Unknown commands receive the protocol's fallback and are counted in the report.
- After the last unprompted event, `end: hold` is silent; `end: loop` repeats those events with
  a period equal to the final recording timestamp. Command matching continues in both cases.
  The run goes on for as long as the test waits
  ([esp32sim backend](esp32sim-backend-user.md#how-a-run-goes)).
- Bytes that reach the UART before the firmware has configured it are dropped, as on a wire; the
  runner reports them as an `unrouted_rx` warning in the
  [link's report](esp32sim-backend-user.md#the-links-report).
- A firmware that configures the UART at another baud than the profile's loses the bytes as frame
  errors, and the runner reports them as `frm_err`. That fails the test, at once: the run ends,
  a wait or an expect that is waiting raises `RunEnded` with the reason first, and the teardown
  reports it otherwise:

  ```
  the link failed: uart.gnss sends at 115200 baud on GPIO 5, and the firmware's UART1 receives there at 9600 baud: 72 bytes dropped as frame errors (frm_err)
  ```

  The recording carries no baud: its bytes go in at the profile's, so the fix is in the profile
  or in the firmware's configuration. Bytes that reach a UART whose RX pin the firmware has set
  before its baud are frame errors too, and fail the test the same way. A firmware that should
  detect a wrong baud, or find the right one by itself, cannot be tested through a replay at the
  wrong baud yet: no marker or option lets a test expect frame errors.

## What a replay refuses

`gnss.replay(path)` reads the recording first and raises, in the test body, when it cannot be
replayed; no runner has started by then. A recording that breaks the format
raises `RecordingError`, with the file and the line
([its messages](../recording-format.md#errors)). One that is well formed but cannot be replayed
here raises `ReplayError`, `{path}` being the path as the test gave it:

| Message | Cause |
|---|---|
| `{path} is a recording of {interface}, not of uart.gnss` | The header's `interface` is another one |
| `{path} has times on the wall clock, and uart.gnss runs on the emulated-fifo-write clock: replaying it would compare times across clocks` | The recording was made on a real board or device. `{clocks}` lists every clock it uses, `emulated-fifo-write and wall` for one with a clock switch. A run with a [real source](real-source-user.md) on another route is on the wall clock from that call on, and takes such a recording and refuses an emulated one |

Malformed request framing also raises `ReplayError`, prefixed with `{path}:`, including
`the recording ends with an incomplete request`. A nonempty loop of duration zero raises
`a looping recording with unprompted events must have positive duration`.

## When the test gets it wrong

These raise `SourceError` (`esp32tl.interfaces.SourceError`) in the test body:

| Message | Cause |
|---|---|
| `uart.gnss: the board is on already: give an interface its source before board.power_on()` | `gnss.replay` after `board.power_on()`; `the board has been on` once `board.power_off()` has switched it off |
| `uart.gnss has its source already: one per test` | A second `gnss.replay` in one test |
| `uart.gnss: cannot replay {path}: it is the tap of uart.gnss` | The recording is the file a route is tapped into ([taps](run-report-user.md#tapping-a-route)) |

A wait or an expect before `board.power_on()` raises
`BoardOff: the board is off; call board.power_on() first`.

On a profile without the route, `gnss` is an error at setup, and the test body does not run:
`esp32sim: the board profile {name} has no uart.gnss; its interfaces are {interfaces}`. Both
profiles esp32tl ships have the route.

A test that calls `gnss.replay` and never `board.power_on()` ran no firmware, and would pass
whatever the firmware does: no runner starts, the link's report says `the runner never
connected`, and the test gets an error at teardown, even when its body passed:

```
esp32sim: the test set up the replay of uart.gnss and never called board.power_on(): it ran no firmware
```

A test with an outcome of its own does not get it as well: one that failed (on a `SourceError`
or a `ReplayError`, for instance), was skipped with `pytest.skip`, xfailed, or whose other
fixtures failed at setup.

## In the link's report

The first line of the [link's report](esp32sim-backend-user.md#the-links-report) names the pins of
every route of the profile, `board esp32-devkitc, watching GPIO 4, GPIO 5`, with a replay or
without one. With a replay the runner takes a turn at each recorded time it reached, as well as
those the test's waits bring. The route's line under `routes:` names the recording and counts the
bytes that went in, `uart.gnss: the replay of ../recordings/gnss-generated-1.jsonl; {n} bytes in, 0 out`,
and a failing test's report shows each event that went in, at its time
([the run on the board's clock](run-report-user.md#the-run-on-the-boards-clock)). To keep what
went in as a recording, tap the route ([taps](run-report-user.md#tapping-a-route)).

## See also

- [Replay: how it works](replay-technical.md)
- [Recording format](../recording-format.md)
- [pytest plugin](pytest-plugin-user.md): the `gnss` fixture
- [Validation firmware](validation-firmware-user.md): `nmea_reader` and the generated GNSS recordings
