# Taps and the run report: how they work

A tap is `Tap` in `esp32tl/tap.py`; the run's log is `RunLog` in `esp32tl/report.py`. The `Uart`
interface of `esp32tl/interfaces/uart.py` feeds both, the `esp32sim` backend
(`esp32tl/backends/esp32sim.py`) owns them, writes the taps and composes the report, and the
`board` fixture of the [pytest plugin](pytest-plugin-technical.md) puts the report into the test's.
What the user sees is in [run-report-user.md](run-report-user.md). Plan §4.3 sketched the tap,
M2 asked for it.

## Data flow

1. **`Esp32Sim.__init__`** makes `Esp32Sim.log = RunLog()` and hands it to every route's `Uart`
   (`profile_uart(..., log=self.log)`), and to the console: the link's `console` callback is
   `Esp32Sim._printed`, which appends to `console_output` and calls `log.console(t, data)`
   ([esp32sim-backend-technical.md](esp32sim-backend-technical.md#data-flow)). It hands the log
   to the `Lines` of the profile too: `lines.drive` and `lines.release` log
   `lines.drive({line!r}, {level})` and `lines.release({line!r})` as steps at `now`, on every
   call, and `Lines.driven` calls `log.line(t, name, level)` for each change of a line the
   firmware drives, in the link's thread. The `SdCard` of a profile's slot gets the log and the
   routes' `files` too: `sd_card.insert(image)` refuses an image `files` holds already
   (`same_file`; `SourceError`, `{name}: cannot hold {path}: it is the {kind} of {interface}`),
   enters it as `("card", "sd_card")`, so that a tap into it is refused as any other, and logs
   `sd_card.insert({image!r})` at 0.
2. **`gnss.tap(path)`**, in the test body, before `board.power_on()`. `Uart.tap` refuses a UART that
   has a tap already (`SourceError`), calls `asked(Request(name, tap=True))`, through which the
   backend refuses a tap after power-on or one no backend gives (`SourceError`) and skips the test
   when its own declaration does not give it, then refuses a path whose directory
   (`(directory / path).parent`) is not a directory (`FileNotFoundError`), and one that names a file
   `Uart.files` holds already (`same_file`; `SourceError`, `it is the {kind} of {route}`). The
   backend gives every `Uart` of the run the same `files` mapping, so a route's replay and another
   route's tap count too. Under the lock that guards the source it checks again, sets
   `Uart.tapped = Tap(name, directory / path)` and enters the file in `files` as `("tap", name)`. It
   logs `{name}.tap({path!r})` at 0. `Uart.replay` refuses a file a route is tapped into, or the
   SD card's image (`WRITERS`, the kinds whose file the run writes), the same way before it reads
   it (two replays of one file harm nothing), enters it as `("replay", name)`
   once the source is given, logs `{name}.replay({path!r})` at 0, and keeps the path as
   `Uart.source_detail`.
3. **The link's turns.** `BoardLink` calls the `Uart` from its thread
   ([link protocol](../protocol.md#4-session)):
   - `receive(t, "out", data)` hands `(t, "out", data)` to `_crossed` before it asks the source,
     then reads the source's answers into a list, `_sent`, and returns it.
   - `poll(now)` reads the source's outputs into a list the same way.
   - `_sent` hands every output that is a `(t, bytes)` pair, `t` an `int` from 0 and not a
     `bool`, to `_crossed` as `in`, at the output's own `t`, the check `BoardLink._outputs`
     makes; anything else goes on to `BoardLink`, which fails the link with it, unrecorded. So an
     answer stamped `t_req + latency` is recorded at that time, before the runner has put it in,
     and an output the link refuses after its own checks (stamped before the turn, or for a route
     without an RX pin) is recorded although the run fails on it.
   - `_crossed` calls `Tap.record(t, direction, data)` when the UART is tapped and
     `RunLog.route(t, name, direction, data)` when it has a log. Empty bytes are no event in
     either.
4. **The test's steps.** `Esp32Sim.power_on` logs `board.power_on(), the world's time {utc}` at
   0, `Esp32Sim.wait` `board.wait({d})` at `now`, and `Esp32Sim.wait_on_clock`, which
   `wait_until` and the console's expects go through, logs `waits up to {within} for
   {description}` at `now` and the outcome at the board's time it returns or raises at:
   `{description}: happened`, `{description}: did not happen within {within}`,
   `{description}: the run ended first`; `wait` logs `the run ended while the test waited`.
   The `RunLog` takes them from the test's thread under its lock; the link is held then.
5. **`Esp32Sim.close`**, at teardown, once the runner is gone and the link read to end of file:
   `_write_taps()` writes `Tap.recording(...)` of every tapped route with `recording.write`
   ([esp32sim-backend-technical.md](esp32sim-backend-technical.md#data-flow), step 6). It writes
   even when the run failed. An `OSError` or a `RecordingError` from making or writing one is a
   problem of the test's, `the tap of {interface} cannot be written: {error}`, beside the link's
   error and the exit code. A board never powered on returns from `close` before, and writes
   nothing.
6. **The `board` fixture's teardown** adds the section
   `("teardown", "esp32sim link", backend.report(run=run))`, where `run` holds when the teardown
   fails, or when the setup or the call failed (`FAILED` in the item's stash), and the user
   property `("esp32sim_link", json.dumps(backend.properties()))`
   ([pytest-plugin-technical.md](pytest-plugin-technical.md#data-flow), step 8).
   `Esp32Sim.report(run)` is the link's report, `routes:` with one line per route, the slot's line
   on a profile with an SD card slot, then, with `run`, `RunLog.lines()`.

**`Tap.recording(board, source, clock, app_sha256=None, tools=None, created=None)`** sorts the
events by `t` with a stable sort, so that events of one time keep the order they were recorded
in, and builds a `Recording` of `Header(board, interface, source, clock, created, app_sha256,
tools={"esp32tl": __version__} | tools)` and one `Event(t, dir, data, clock)` per event. `created`
is `utc_time(created or datetime.now(UTC))`, to the second. The events arrive out of time order
when a source answers ahead: an answer stamped `t_req + latency` can be recorded before a byte
the firmware transmits at a time between the two.

**`RunLog`** keeps entries `(t, n, who, what)`, `n` counting every entry ever added: the test's
steps in a list, all of them, with the chip's resets (`chip(t, text)`, who `chip`), which the
backend adds from the link's thread, and the console's lines and the routes' events in a `deque` of
`ENTRIES` (200), so the oldest of those go first. `what` is the step's text, or the bytes as they
came: nothing is turned into text until `lines()`, which merges the two, sorts by `(t, n)` and
writes each entry with `text(who, what)`. `console(t, data)` cuts `data` at line ends
(`bytes.splitlines(keepends=True)`), carries a line without its `\n` to the next call, and adds a
line at the `t` of the call that brought its first byte; `lines()` adds the unfinished line at the
end. `console_line` drops the line ending and the colour codes (`\x1b[...m`), decodes UTF-8 with
`backslashreplace`, and escapes every other control character but the tab (`\r`, `\x1b`).
`route(t, interface, direction, data)` counts the bytes and the events each way in
`RunLog.traffic[interface]`, a `Traffic`, and adds the bytes as `{interface} {direction}`, which
`lines()` writes with `shown`. `line(t, name, level)` adds `{name} out` with `0`, `1` or
`not driven` as its text; it goes in the same `deque` and counts among the routes' entries in the
heading. `lines()` pads the who column to the longest in the entries kept.

Console capture uses the same `Tap` and file checks: `Console.tap` calls the backend's
`tap_console`, whose console-only `Uart` receives `log=self.log` and records the setup step.
On esp32sim, `_printed(t, data)` records `out` events using the link time and `_write_taps`
includes the console. On serial, the reader queues bytes stamped after its poll (10 ms interval,
plus USB/driver/host scheduling delay); `_drain` records them on the test thread and `close`
writes them. Serial also logs `waits up to ...`, success, timeout and run-end steps.
A console tap's path is in both backends' human report and JSON property. Their `report_section`
and `report_property` attributes choose the names; serial uses `serial console` / `serial_console`.

## Schema

What a tap writes is a recording of [version 1 of the format](../recording-format.md), with:

| Field | Value |
|---|---|
| `board` | The backend's `profile.name` |
| `interface` | `console` or the route, `uart.<role>` |
| `source` | UART source (`replay`, `model:gnss`, or `none`); console is `none` on esp32sim and `real` on serial |
| `clock` | `emulated-fifo-write` on esp32sim, `wall` on serial |
| `end` | `hold` |
| `app_sha256` | `app_image_sha256(app)`, hashing `binary_path / flasher_args["app"]["file"]`; on esp32sim `null` when `flasher_args.json` names no app or the file cannot be read |
| `tools` | `{"esp32tl": esp32tl.__version__}`, and `esp32sim`, the runner's `hello`'s revision, when it sent one |
| `created` | The UTC time the recording was made, to the second |

Serial requires an app hash at setup; a missing entry or unreadable file fails before the port
opens. It writes the selected artifact's digest, including with skip-autoflash, which does not
prove the device's flash contents. Console taps contain only `out` events. The schema is unchanged
(version 1): `console` was already a valid interface name.

The esp32sim report section, after `BoardLink.report()`:

```
routes:
  {interface}: {source}; {bytes}{tap}
console: {n} bytes out; tapped into {path}
sd_card: a card holding {image}
  fault {id} from {at}: {rule}[, cleared at {t}]; {n} operation(s)
    {at} {op} block {n}: {failed|delayed {delay}}, {written|not written} (fault {id}|faults {ids})
the run on the board's clock, {kept}:
  {seconds}.{nanoseconds:09d} s  {who}  {text}
```

`{source}` is `the {kind} of {detail}` or `nothing at the far end`; `{bytes}`
`{in} bytes in, {out} out`, or `no bytes` for a route nothing crossed; `{tap}`
`; tapped into {path}` or nothing. The `sd_card:` line is there only on a profile with a slot, as
`sd_card: empty` while the test has inserted no card. `{rule}` is `CardFault.describe()`, as
`fail writes on block 8, 1 time` or `delay by 0.3 s writes on every block, until cleared`; an
erase's line has `blocks {first} to {last}`. `{kept}` is `{n} entries` (`1 entry`), every entry of the run,
the steps included; when the `deque` has dropped some,
`: every step of the test, and the last {kept} of the console and the routes` follows. With no entry
the heading is `the run on the board's clock: nothing happened`. `{who}` is `test`, `chip`,
`console`, or `{interface} in` / `{interface} out`.

The `console:` line and a JSON `console: {"bytes_out": n, "tap": path}` object appear only when
the console is tapped. Serial instead uses the [serial schema](serial-backend-technical.md#schema).

The esp32sim user property's `routes`, by interface: `{"source": kind or null, "detail": what
the source was made from or null, "bytes_in": n, "bytes_out": n, "tap": path or null}`;
and `sd_card`, `{"image": path or null, "faults": [...], "affected": [...]}`, or null on a profile
without a slot ([esp32sim-backend-technical.md](esp32sim-backend-technical.md#data-flow) gives
their fields).

`RunLog.line(t, name, level, direction="out")` records firmware GPIO output by default.
`Lines.inputs` calls it with `direction="in"` when a model's pending GPIO response is due and
changes the sent level. These entries share the bounded wire log; cancelled responses never
reach it. `tests/test_modem_model.py::test_only_sent_model_gpio_changes_appear_in_the_run_log`
checks the timing and cancellation. The modem's separate command history is described in
[modem-technical.md](modem-technical.md).

## Interfaces

| Name | What |
|---|---|
| `Tap(interface, path)` | `esp32tl/tap.py`. Attributes `interface`, `path`, `events` (`(t, dir, data)` in the order recorded) |
| `Tap.record(t, direction, data)` | One event; nothing for empty `data` |
| `Tap.recording(*, board, source, clock, app_sha256=None, tools=None, created=None)` | The `Recording`, events in time order |
| `NO_SOURCE` | `"none"`: the header's `source` for a route without one |
| `utc_time(moment)`, `sha256(path)`, `app_image_sha256(app)` | The header's `created` and app-image hash; the last reads exactly `flasher_args["app"]["file"]`, returns `None` without an app entry, and propagates file errors |
| `Console.tap(path)` | Delegates to the backend's `tap_console`; the same path and duplicate checks as a UART tap |
| `Uart.tap(path)` | `esp32tl/interfaces/uart.py`: the route's `Tap`, `Uart.tapped` |
| `Uart.files` | `dict[Path, tuple[str, str]]`: the files the run replays from and taps into, by path, with what each is for, the kind (`replay`, `tap`) and the route; one mapping for every `Uart` of an `Esp32Sim` |
| `same_file(a, b)` | `esp32tl/interfaces/uart.py`: whether two paths name one file: `os.path.samefile` when both exist, else the same directory and the same name but for its case |
| `RunLog(entries=ENTRIES)` | `esp32tl/report.py`. `test(t, text)`, `chip(t, text)`, `console(t, data)`, `route(t, interface, direction, data)`, `line(t, name, level, *, direction="out")`, `lines()`; attribute `traffic`, by interface |
| `Traffic` | `bytes_in`, `bytes_out`, `events_in`, `events_out` |
| `shown(data, limit=SHOWN_BYTES)`, `console_line(line)`, `board_time(t)`, `text(who, what)` | How the log writes bytes, console lines, times and an entry |
| `ENTRIES`, `SHOWN_BYTES` | 200 entries of the console and the routes kept; 100 bytes shown of an event |
| `Esp32Sim.log`, `Esp32Sim.report(run=False)`, `Esp32Sim.properties()` | [esp32sim-backend-technical.md](esp32sim-backend-technical.md#interfaces) |

## Failure modes

From `Uart.tap`, in the test body:

| Message | Exception | Cause |
|---|---|---|
| `{interface} is tapped already: one tap per test` | `SourceError` | A second tap of the route |
| `{interface}: the board is on already: tap a route before board.power_on()` | `SourceError` | From the backend's `_asked`, after power-on; `the board has been on` after `power_off()` |
| `no backend can tap {interface}` | `SourceError` | `capabilities.check`: no backend declares the tap |
| `esp32sim: {interface} cannot be tapped on this backend` | pytest's skip | `capabilities.refusal` for this backend |
| `{interface}: cannot tap into {path}: {directory} is not a directory` | `FileNotFoundError` | The file's directory does not exist; checked after the backend's checks |
| `{interface}: cannot tap into {path}: it is the {kind} of {route}` | `SourceError` | The file is in `files`: `{kind}` is `replay`, `tap` or `card` (the SD card's image, `{route}` then `sd_card`) |

From `Uart.replay`: `{interface}: cannot replay {path}: it is the tap of {route}`, or
`... it is the card of sd_card`, a `SourceError`.
From `SdCard.insert`: `sd_card: cannot hold {path}: it is the {kind} of {interface}`, a
`SourceError`, for an image a route replays from or is tapped into.

`same_file` takes two names that differ only in case for one file when either does not exist yet,
as macOS's file system does; on one that tells them apart, such a tap is refused all the same.

From `Esp32Sim.close`: `the tap of {interface} cannot be written: {error}`, for an `OSError` or a
`RecordingError`, first among the problems of its `BackendError`
([esp32sim-backend-technical.md](esp32sim-backend-technical.md#failure-modes)).

An output that is not a `(t, bytes)` pair with `t` from 0 is not recorded, and fails the link as
before ([protocol, section 7](../protocol.md#7-latency-is-bound-to-the-source)).

Serial errors and its report properties are specified in
[serial failure modes](serial-backend-technical.md#failure-modes).

## Tests

- `tests/test_pytest_plugin.py::test_console_tap_preserves_emulated_output`: passing and failing
  runs, console event bytes, hash, report path and byte count, JSON property and the tap step.
- `tests/test_serial_backend.py::test_console_reset_clock_tap_and_autoflash` and
  `test_serial_waits_and_console_tap_are_in_the_failure_report`: wall-clock console tap, shared
  app hash, protected files, tap setup step, wait starts and outcomes, timeout interval.

- `tests/test_tap.py`, without an emulator: a `Tap`'s recording in time order, ties in the order
  recorded, no event for empty bytes, the header with the tools, and its round trip through the
  format; `utc_time` and `sha256`; a tapped `Uart` with a source that answers and sends
  unprompted, through a `BoardLink`'s turns, recording both ways at the outputs' times and
  counting the bytes in the log; a `Uart` without a source recording what the firmware sent; the
  backend asked once per tap, a second tap and a missing directory refused; a tap the backend
  refuses leaving none; outputs the link would refuse as no pair (a `bool` time, a negative one,
  text) left out of the tap and the log; a tap into a route's replay or another route's tap, and
  a replay of a tapped file, refused; the test's steps on a `Uart` logged at 0.
- `tests/test_sdspi.py`: the card's line and `sd_card.insert` step in the report of a run with a
  card, and the `sd_card` property; a tap into the card's image, a replay of it, and a card
  holding a tap's file, refused, with `sd_card: empty` in the report.
- `tests/test_run_report.py`: entries in the order of the board's time, ties in the order they
  came; the console cut into lines at their first byte's time, colour codes dropped, an unfinished
  line shown; every step and the last `entries` of the rest kept, with the heading that says so,
  and the bytes counted; a line the firmware drives, and stops driving; an empty log; `shown` and
  `console_line`; a console line's `\r` and other escape sequences escaped.
  `tests/test_lines.py::test_the_test_s_calls_and_the_firmware_s_changes_go_into_the_run_s_log`:
  the test's calls on the lines as steps, a call that changes nothing included, and the
  firmware's change as an entry.
- `tests/test_pytest_plugin.py`, with the fake runner that speaks the link protocol and
  `FAKE_LINK=tx`: `test_a_tap_records_what_crossed_the_route_both_ways` reads the file a passing
  test's tap wrote (header, `app_sha256` of the fake build's empty `app.bin`, tools, the `out`
  event and the replay's first two events) and finds the routes but not the run in its report;
  `test_the_report_of_a_failing_test_shows_the_run_on_the_board_s_clock` matches the run line by
  line and the routes in the `--junitxml` property;
  `test_a_tap_that_cannot_be_written_fails_the_test_at_teardown` makes the tap's path a
  directory, and finds the teardown error and the run in the report. With the fake runner that
  never connects:
  `test_the_board_s_clock_and_sources_go_in_order` (a tap after power-on, two taps, a missing
  directory), `test_a_source_or_a_tap_the_backend_cannot_give_skips_at_the_call` and
  `test_a_test_that_taps_a_route_and_never_powers_on_fails_at_teardown` (no file).
- `tests/test_gnss_fixture.py::test_a_tap_of_a_replay_records_what_went_in`, on the real runner
  and `nmea_reader`, through `pytester`: after 5.5 emulated seconds the tap holds the replay's
  first five events, at their recorded times, and nothing out, and so does the file written at
  teardown, whose `app_sha256` is that of the build's `app.bin` and whose `tools` name the
  runner's esp32sim revision.

Not covered: a log of more than `ENTRIES` entries through the fixture (the cut is tested on
`RunLog`); a run report of a test that failed at setup, which ran nothing; a `RecordingError` at
teardown, which `_sent`'s check leaves no way to reach from a source.

## See also

- [Taps and the run report](run-report-user.md)
- [Recording format](../recording-format.md)
- [esp32sim backend: how it works](esp32sim-backend-technical.md)
- [pytest plugin: how it works](pytest-plugin-technical.md)
- [Plan section 4.3](../implementation-plan.md#43-recording-format) and
  [M2](../implementation-plan.md#m2-tap-and-the-matching-engine)
- [Harness decisions](../decisions/harness.md#a-test-taps-a-route-by-calling-tappath-before-power-on-not-with-a-marker)
