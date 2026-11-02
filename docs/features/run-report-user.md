# Taps and the run report

A tap records what crosses one of the board's routes during a run, both ways: what the firmware
transmitted, and what the far end sent it, whether that is a replay or nothing at all. It writes a
[recording](../recording-format.md), in the format a replay reads. The run report is what a failing
test shows besides its error: the run on the board's clock, the test's steps, the console's lines
and the bytes on every route side by side, in the order they happened.

## Tapping a route

```python
import pytest

pytestmark = pytest.mark.app_path("../firmware/nmea_reader")


def test_reports_a_fix(board, gnss, console, tmp_path):
    gnss.replay("../recordings/gnss-generated-1.jsonl")
    gnss.tap(tmp_path / "gnss.jsonl")
    board.power_on()
    console.expect_exact("fix acquired lat=52.229677 lon=21.012233")
```

- `gnss.tap(path)` taps `uart.gnss`; `board.uart(role).tap(path)` taps any other UART route of the
  profile. Call it before `board.power_on()`, before or after the route's source: it records from
  power-on.
- The path is relative to the test file's directory, as a replay's is, or absolute: pytest's
  `tmp_path` keeps the file out of the source tree. Its directory must exist. A file already there
  is replaced, but not one the run replays from or another route is tapped into: those are
  refused at the call, also when the name differs only in case (`R.jsonl` for `r.jsonl`).
- The file is written once the run has ended, at the test's teardown, also when the test failed.
  A test that taps a route and never calls `board.power_on()` ran no firmware: nothing is written,
  and the test gets an error at teardown
  (`esp32sim: the test set up the tap of uart.gnss and never called board.power_on(): it ran no firmware`).
- `gnss.tap` returns the `Tap`. During the test its `events` list holds what the tap has recorded
  so far, as `(t, dir, data)` tuples in the order they reached the route, and `path` is the file.
- One tap per route in a test.

## What a tap writes

A recording, [version 1](../recording-format.md):

```json
{"esp32tl_recording": 1, "board": "esp32-devkitc", "interface": "uart.gnss", "source": "replay", "clock": "emulated-fifo-write", "end": "hold", "app_sha256": "4f0c…", "tools": {"esp32tl": "0.1.0", "esp32sim": "8c652151dd5a521249a785ca147f20a9f8eda68f"}, "created": "2026-11-01T08:00:54Z"}
{"t": 1000000000, "dir": "in", "data": "JEdQR0dB..."}
```

- `source` is what was at the far end: `replay`, `model:gnss` for the GNSS receiver the test
  described ([models and units](models-user.md)), or `none` for a route the test gave nothing.
- `clock` is the backend's, `emulated-fifo-write`: times are the board's nanoseconds since
  power-on.
- `app_sha256` is the SHA-256 of the app image the run flashed, the file `flasher_args.json`
  names under `app`; `tools` the esp32tl version and the esp32sim revision the runner reported;
  `created` when the file was written, in UTC.
- An `out` event is bytes the firmware wrote to the route's UART, at the time it wrote them. An
  `in` event is bytes the far end gave, at the time it stamped them: a replay's events keep their
  recorded times, so a tap of a replay holds the replay's events up to where the run ended. Bytes
  the harness refused to send, because they were stamped too early, are in the tap as well; the
  run has failed on them.
- Events come in the order of their times; two of one time in the order they reached the route.

## The run on the board's clock

A test that fails, in its body, at setup or at teardown, gets the run at the end of its
`esp32sim link` report section, after the link's report and the routes
([the link's report](esp32sim-backend-user.md#the-links-report)):

```
routes:
  uart.gnss: the replay of gnss.jsonl; 136 bytes in, 15 out; tapped into /tmp/…/tap.jsonl
sd_card: empty
the run on the board's clock, 12 entries:
  0.000000000 s  test           uart.gnss.replay('gnss.jsonl')
  0.000000000 s  test           uart.gnss.tap('tap.jsonl')
  0.000000000 s  test           board.power_on(), the world's time 2026-01-01T12:00:00Z
  0.000000000 s  test           board.wait(2.5 s)
  0.000001000 s  console        Hello world!
  0.000001000 s  uart.gnss out  $PMTK314,0*28\r\n
  1.000000000 s  uart.gnss in   $GPGGA,120000.00,5213.7806,N,02100.7340,E,1,08,0.9,110.0,M,34.0,M,,*66\r\n
  2.000000000 s  uart.gnss in   $GPRMC,120001.00,A,5213.7794,N,02100.7356,E,0.0,,291026,,,A*70\r\n
  2.500000000 s  test           waits up to 30 s for 'Hello world!' on the console
  2.500000000 s  test           'Hello world!' on the console: happened
  2.500000000 s  test           waits up to 0.5 s for 'Goodbye' on the console
  3.000000000 s  test           'Goodbye' on the console: did not happen within 0.5 s
```

This is `tests/test_pytest_plugin.py`'s fake runner, which prints one line and transmits one
command. Each line is the board's time, who, and what:

- `test`: what the test did. Before power-on everything is at 0. A call of the receiver model at
  the board's time it was made, as `uart.gnss.move(heading=NORTH, speed=m_s(10))`; its route's line
  says `the model of a GNSS receiver`. `board.wait(d)` at the time it started from; a wait for a
  condition or a console pattern when it started, and again when it ended, with `happened`,
  `did not happen within {time}` or `the run ended first`.
- `console`: a line the firmware printed, without its line ending and colour codes, at the time of
  its first byte. Any other control character but the tab is escaped, as `\r` or `\x1b`, so that
  the line cannot write over the report. A line the run did not finish is shown too.
- `{route} out`, `{route} in`: the bytes the firmware transmitted on the route, and those the far
  end sent in, at the time they crossed, whether the route is tapped or not. Printable ASCII is
  shown as it is, `\r`, `\n`, `\t` and `\\` escaped, any other byte as `\xNN`; after 100 bytes the
  line is cut, with the length: `... (512 bytes)`.
- `{line} out`, as `modem.pwrkey out`: a level the firmware drove a line of the profile's
  `[lines.<role>]` sets to, `0` or `1`, or `not driven` once its output is off, at the time it
  changed. What the test drives on a line is a `test` step: `lines.drive('modem.ri', 1)`,
  `lines.release('modem.ri')`, every call, also one that changes nothing.

Entries are in the order of the board's time, those of one time in the order they came. The
report keeps every step of the test, and the last 200 lines of the console, events of the
routes and changes of the lines; when it has left some out, its heading says so:
`the run on the board's clock, {n} entries: every step of the test, and the last 200 of the console and the routes:`.
A run with no entry says `the run on the board's clock: nothing happened`.

On a profile with an SD card slot, as `esp32-devkitc`, a line after the routes names the card:
`sd_card: a card holding PATH`, or `sd_card: empty`; the test's `sd_card.insert(PATH)` is one of
its steps ([pytest plugin](pytest-plugin-user.md#the-fixtures)).

A test that passed gets the `routes:` lines and the slot's, without the run; `pytest -rA` shows
them.

## When the test gets it wrong

In the test body:

| Message | Exception | Cause |
|---|---|---|
| `uart.gnss: the board is on already: tap a route before board.power_on()` | `SourceError` | `gnss.tap` after `board.power_on()` |
| `uart.gnss is tapped already: one tap per test` | `SourceError` | A second `gnss.tap` in one test |
| `uart.gnss: cannot tap into {path}: {directory} is not a directory` | `FileNotFoundError` | The file's directory does not exist |
| `uart.gnss: cannot tap into {path}: it is the replay of uart.gnss` | `SourceError` | The file is the recording a route replays: the tap would write over it |
| `uart.gnss: cannot tap into {path}: it is the tap of uart.modem` | `SourceError` | Another route is tapped into the file |
| `uart.gnss: cannot tap into {path}: it is the card of sd_card` | `SourceError` | The file is the image of the card in the SD card slot, which the firmware writes |
| `sd_card: cannot hold {path}: it is the tap of uart.gnss` | `SourceError` | `sd_card.insert` of a file a route replays from or is tapped into: the card would write over it |
| `uart.gnss: cannot replay {path}: it is the tap of uart.gnss` | `SourceError` | `gnss.replay` of a file a route is tapped into |
| `uart.gnss: cannot replay {path}: it is the card of sd_card` | `SourceError` | `gnss.replay` of the SD card's image, which the firmware writes |
| `no backend can tap uart.gnss` | `SourceError` | No backend declares a tap of the route |

A backend that does not declare a tap of the route skips the test at the call, with
`esp32sim: uart.gnss cannot be tapped on this backend`
([board profiles](board-profiles-user.md#when-a-backend-cannot-give-it)); `esp32sim` taps every
UART route.

A tap that cannot be written at teardown is an error there:
`esp32sim: the tap of uart.gnss cannot be written: {reason}; its output is in {logfile}`.

## Replaying a tap

`gnss.replay(path)` can replay both passive streams and taps with outgoing commands and incoming
answers. The [matching engine](matching-user.md) groups complete `out` requests with the `in`
events that follow them. Use `protocol=` when automatic AT or GNSS detection is unsuitable;
the tap must use a supported framing and the replay route's clock and interface. A tap ending
part-way through a request is refused ([replay errors](replay-user.md#what-a-replay-refuses)).

## What it does not do

- No call taps the console: what the firmware prints is in `dut.log` and in the run report.
- The `esp32tl` command does not tap: there is no `esp32tl record`
  ([the esp32tl command](cli-user.md)).
- The run report has a line per UART route; esp32tl simulates no network service yet, so there
  are none of those.

## See also

- [Taps and the run report: how they work](run-report-technical.md)
- [Recording format](../recording-format.md)
- [Replay](replay-user.md)
- [pytest plugin](pytest-plugin-user.md)
- [esp32sim backend](esp32sim-backend-user.md#the-links-report)
