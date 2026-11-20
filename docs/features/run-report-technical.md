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
   `sd_card.insert({image!r})` at 0, or at `now` once the board is on. `sd_card.remove()` takes
   the image out of `files` again and logs `sd_card.remove()` the same way.
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
   `Esp32Sim.report(run)` is `build_line(...)` of the `Build` the runner flashes, the link's
   report, `routes:` with one line per route, the slot's line on a profile with an SD card slot,
   then, with `run`, `RunLog.lines()`.

**`Tap.recording(board, source, clock, app_sha256=None, tools=None, created=None, extra=None)`**
sorts the events by `t` with a stable sort, so that events of one time keep the order they were
recorded in, and builds a `Recording` of `Header(board, interface, source, clock, created,
app_sha256, tools={"esp32tl": __version__} | tools, extra=extra or {})` and one `Event(t, dir,
data, clock)` per event. `created`
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

### The HCI tap

`Bluetooth.tap(path)` (`esp32tl/interfaces/bluetooth.py`) makes a `Tap("bluetooth", path)` and
records into it from the link's thread: `receive` each `hci_tx` packet as `out` at its `t`, `poll`
each packet it returns as `in` at its due time, the `hci_rx`'s `t`
([pytest-plugin-technical.md](pytest-plugin-technical.md#the-chips-bluetooth)). `_write_taps` makes
its `Tap.recording` with `source="model:bluetooth"` (`TAP_SOURCE`), the backend's clock, the app's
hash, the tools, and `extra={"devices": [...]}`, the name and address of each device
`Bluetooth.devices()` lists at the end of the run, in the order of their addresses; it writes it
with `recording.write_btsnoop`, which writes the btsnoop file and its sidecar
([recording format](../recording-format.md#hci-taps)). Its errors are a problem of the test's as a
UART's are, `the tap of bluetooth cannot be written: {error}`, and come before the UARTs'. A
packet's bytes are not split or joined: one record is one H4 packet, its packet-type byte first.

## Schema

What a tap writes is a recording of [version 1 of the format](../recording-format.md), with:

| Field | Value |
|---|---|
| `board` | The backend's `profile.name` |
| `interface` | `console` or the route, `uart.<role>` |
| `source` | UART source (`replay`, `model:gnss`, `real`, or `none`); console is `none` on esp32sim and `real` on serial |
| `clock` | `emulated-fifo-write` on esp32sim, `wall` in a run with a real source there and on serial |
| `end` | `hold` |
| `app_sha256` | `app_image_sha256(app)`, hashing `Build.app_image`: `flasher_args["app"]["file"]` in an ESP-IDF build directory, `firmware.bin` in a PlatformIO one; on esp32sim `null` when `flasher_args.json` names no app or the file cannot be read |
| `tools` | `{"esp32tl": esp32tl.__version__}`, and `esp32sim`, the runner's `hello`'s revision, when it sent one |
| `created` | The UTC time the recording was made, to the second |

Serial requires an app hash at setup; a missing entry or unreadable file fails before the port
opens. It writes the selected artifact's digest, including with skip-autoflash, which does not
prove the device's flash contents. Console taps contain `out` events for firmware output
and `in` events for test writes. The schema is unchanged (version 1): `console` was already a valid interface name.

**`build_line(app, app_sha256)`** (`esp32tl/report.py`) is the first line of both backends'
sections, `build: {directory} ({kind} for {chip}); app_sha256 {app_sha256}`, of the `Build` `app`:
`{directory}` is `Build.directory`, absolute (`load_app` resolves it), `{kind}` is
`build.KIND_NAMES[Build.kind]`, `ESP-IDF` for `build.ESP_IDF` and `PlatformIO` for
`build.PLATFORMIO`, `{chip}` is `Build.chip`, and `{app_sha256}` is `none` for `None`.
**`build_properties(app, app_sha256)`** is the user property's `build`, `{"directory": str,
"kind": "esp-idf" or "platformio", "chip": str, "app_sha256": str or null}`. esp32sim gives them
`Esp32Sim.app` and `Esp32Sim.app_sha256`; serial `Artifact.build` and `Artifact.app_sha256`, which
`check_artifact` refuses to leave `None`.

On esp32sim the hash is taken at setup, as the backend loads the build, and again as the first
`power_on` starts the runner, which reads the image then: the report names the image the runner
flashed, not one written into the directory later in the run, and `report()`, `properties()` and
the taps read the same value without hashing the file again. A build directory built again
between those two points shows in the report as the image the runner read; a test that never
powers on reports the image at setup. `None` when the build names no app image or the file cannot
be read.

`make determinism` runs both runs in one checkout, so the directory is the same in both
`esp32sim_link` properties; `scripts/check-determinism.py` writes only the basetemp as
`{basetemp}`, and the build directory is outside it.

The esp32sim report section, `build_line`, then `BoardLink.report()`, then:

```
routes:
  {interface}: {source}; {bytes}{tap}
console: {n} bytes out; tapped into {path}
sd_card: a card holding {image}
  {seconds}.{nanoseconds:09d} s  taken out
  {seconds}.{nanoseconds:09d} s  {image} put in
  {seconds}.{nanoseconds:09d} s  refused: {image} put in
  {seconds}.{nanoseconds:09d} s  not reached: taken out
  fault {id}: {call} at {at}[, cleared at {cleared}]; {n} operation(s)
    {at}  {op} of block {block} failed, not written
    {at}  {op} of block {block} delayed {delay}, {written|not written}
    {at}  erase of blocks {first} to {last} delayed {delay}, {written|not written}
    {at}  {op} of block {block} observed, {written|not written}
    {at}  erase of blocks {first} to {last} observed, {written|not written}
the run on the board's clock, {kept}:
  {seconds}.{nanoseconds:09d} s  {who}  {text}
```

`{source}` is `the {kind} of {detail}` or `nothing at the far end`; `{bytes}`
`{in} bytes in, {out} out`, or `no bytes` for a route nothing crossed; `{tap}`
`; tapped into {path}` or nothing. The `sd_card:` line is there only on a profile with a slot, as
`sd_card: empty`, and names `SdCard.held`, the card after the last change the runner
acknowledged, else the one at power-on; after it, one line per change the runner acknowledged
while the board was on (`SdCard.changes`), at its time, then the one it refused
(`SdCard.refused`), at that time, with `refused: ` before it, then one per change it did not
reach (`SdCard.not_reached`), at the time the test made it, with `not reached: ` before it. Then
`SdCard.report()`: one `fault` line per rule the test gave, cleared and spent ones too, in order, and
under it one line per operation the rule met (`FaultedOperation.__str__`). `{call}` is the call in
the test's words, as `sd_card.fail('write', count=1)`,
`sd_card.delay('read', seconds(0.02), blocks=range(0, 4))` or `sd_card.observe('write')`;
`{at}`, `{cleared}` and `{delay}` are
`Duration`s as `str` shows them, as `2.5 s` or `3.0681844 s`, and `{n} operation(s)` is `1 operation`
or `2 operations`. `{kept}` is `{n} entries` (`1 entry`), every entry of the run,
the steps included; when the `deque` has dropped some,
`: every step of the test, and the last {kept} of the console and the routes` follows. With no entry
the heading is `the run on the board's clock: nothing happened`. `{who}` is `test`, `chip`,
`console`, or `{interface} in` / `{interface} out`.

The `console:` line and a JSON `console: {"bytes_out": n, "tap": path}` object appear only when
the console is tapped. Serial instead uses the [serial schema](serial-backend-technical.md#schema).

The esp32sim user property's `build`, `build_properties(...)` as above; its `routes`, by
interface: `{"source": kind or null, "detail": what
the source was made from or null, "bytes_in": n, "bytes_out": n, "tap": path or null}`;
and `sd_card`, `{"image": path or null, "changes": [{"t_ns": t, "image": path or null}, ...],
"refused": [...], "not_reached": [{"t_ns": t, "image": path or null}, ...], "faults": [...]}`, `image` being
`SdCard.held`, or null
on a profile without a slot. `faults` is `SdCard.properties()`, per rule `{"id": n, "call": text,
"t_ns": t, "cleared_t_ns": t or null, "operations": [{"t_ns": t, "op": "read" or "write" or "erase",
"blocks": [first, last], "action": "error" or "delay" or "observe", "delay_ns": ns or null,
"written": bool}, ...]}`, `delay_ns` null but for a delay.

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
| `Tap.recording(*, board, source, clock, app_sha256=None, tools=None, created=None, extra=None)` | The `Recording`, events in time order; `extra` the header's fields of the tap's own |
| `NO_SOURCE` | `"none"`: the header's `source` for a route without one |
| `utc_time(moment)`, `sha256(path)`, `app_image_sha256(build)` | The header's `created` and app-image hash; the last reads exactly `build.app_image` (`esp32tl.build.Build`), returns `None` when it is `None`, and propagates file errors |
| `Console.tap(path)` | Delegates to the backend's `tap_console`; the same path and duplicate checks as a UART tap |
| `Uart.tap(path)` | `esp32tl/interfaces/uart.py`: the route's `Tap`, `Uart.tapped` |
| `Bluetooth.tap(path)` | `esp32tl/interfaces/bluetooth.py`: the HCI's `Tap`, `Bluetooth.tapped`; claims the path and its sidecar in `Bluetooth.files`, the backend's one mapping |
| `TAP_SOURCE` | `esp32tl/interfaces/bluetooth.py`: `"model:bluetooth"`, the HCI tap's `source` |
| `Uart.files` | `dict[Path, tuple[str, str]]`: the files the run replays from and taps into, by path, with what each is for, the kind (`replay`, `tap`) and the route; one mapping for every `Uart` of an `Esp32Sim` |
| `same_file(a, b)` | `esp32tl/interfaces/uart.py`: whether two paths name one file: `os.path.samefile` when both exist, else the same directory and the same name but for its case |
| `RunLog(entries=ENTRIES)` | `esp32tl/report.py`. `test(t, text)`, `chip(t, text)`, `console(t, data)`, `route(t, interface, direction, data)`, `line(t, name, level, *, direction="out")`, `lines()`; attribute `traffic`, by interface |
| `Traffic` | `bytes_in`, `bytes_out`, `events_in`, `events_out` |
| `shown(data, limit=SHOWN_BYTES)`, `console_line(line)`, `board_time(t)`, `text(who, what)` | How the log writes bytes, console lines, times and an entry |
| `ENTRIES`, `SHOWN_BYTES` | 200 entries of the console and the routes kept; 100 bytes shown of an event |
| `build_line(app, app_sha256)`, `build_properties(app, app_sha256)` | `esp32tl/report.py`: the first line of a backend's report section, and its user property's `build`, of an `esp32tl.build.Build` and its app image's hash or `None` ([schema](#schema)) |
| `KIND_NAMES` | `esp32tl/build.py`: a build's `kind` as the report names it, `{"esp-idf": "ESP-IDF", "platformio": "PlatformIO"}` |
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

From `Bluetooth.tap`, the same but for the files: `bluetooth is tapped already: one tap per test`;
`bluetooth: cannot tap into {path}: {directory} is not a directory`; and `bluetooth: cannot tap
into {path}: {file} is the {kind} of {route}`, `{file}` the path or its sidecar.

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
- `tests/test_bluetooth_guest.py`, without an emulator: a tapped `Bluetooth` recording every
  packet both ways through a `BoardLink`'s turns, as many as it counts, an answer at the latency
  after its command, with the report's `tapped into` and the `tap` property; a second tap, a
  missing directory, a path or a sidecar another file's, refused; a tap the backend refuses
  leaving none.
- `tests/test_bt_spp_initiator.py::test_the_tap_writes_the_hci_both_ways_as_btsnoop`, on the real
  runner and `bt_spp_initiator`, through `pytester`: the btsnoop file written at teardown holds
  what the `Tap` held, to the microsecond, from `HCI_Reset` and its `Command_Complete` 100 µs later
  on, the firmware's `PIN_Code_Request_Reply` and the peer's name among them; the sidecar's
  `interface`, `source`, `clock`, `devices` and esp32sim revision.
- `tests/test_sdspi.py`: the card's line and `sd_card.insert` step in the report of a run with a
  card, and the `sd_card` property; a tap into the card's image, a replay of it, and a card
  holding a tap's file, refused, with `sd_card: empty` in the report; a rule's `fault` line in the
  report of a run on `sdspi`.
- `tests/test_sd_faults.py`: the `fault` lines and operation lines of `SdCard.report()`, a spent
  rule that shows no `cleared at`, and the entries of `SdCard.properties()`.
- `tests/test_run_report.py`: entries in the order of the board's time, ties in the order they
  came; the console cut into lines at their first byte's time, colour codes dropped, an unfinished
  line shown; every step and the last `entries` of the rest kept, with the heading that says so,
  and the bytes counted; a line the firmware drives, and stops driving; an empty log; `shown` and
  `console_line`; a console line's `\r` and other escape sequences escaped;
  `test_the_build_line_names_each_kind_as_its_tool_does`: `build_line` and `build_properties` of a
  PlatformIO and an ESP-IDF build, `none` for a missing hash, and a name for every kind.
  `tests/test_lines.py::test_the_test_s_calls_and_the_firmware_s_changes_go_into_the_run_s_log`:
  the test's calls on the lines as steps, a call that changes nothing included, and the
  firmware's change as an entry.
- `tests/test_pytest_plugin.py`, with the fake runner that speaks the link protocol and
  `FAKE_LINK=tx`: `test_a_tap_records_what_crossed_the_route_both_ways` reads the file a passing
  test's tap wrote (header, `app_sha256` of the fake build's empty `app.bin`, tools, the `out`
  event and the replay's first two events) and finds the routes but not the run in its report;
  `test_the_report_of_a_failing_test_shows_the_run_on_the_board_s_clock` matches the build line,
  the run line by line and the routes in the `--junitxml` property;
  `test_the_report_names_the_build_the_test_loaded`, with the plain fake runner, that a passing
  test's section and its property name the `build_dir` the marker selected, absolute, and its
  image's hash, not the app's own `build/` beside it;
  `test_the_report_names_the_image_the_runner_read_at_power_on`, with the link-speaking fake
  runner, that an image written before `power_on` is the one reported, and one written after it
  is not;
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
