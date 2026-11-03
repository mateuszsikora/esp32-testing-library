# pytest plugin: how it works

The plugin is `esp32tl/pytest_plugin.py`, with the backend registry in
`esp32tl/backends/__init__.py`, the console in `esp32tl/interfaces/console.py`, the UART
interface in `esp32tl/interfaces/uart.py` and the one behind `gnss` in
`esp32tl/interfaces/gnss.py`, the GPIO lines behind `lines` in `esp32tl/interfaces/lines.py`, the
SD card slot behind `sd_card` in `esp32tl/interfaces/sd_card.py`, the Wi-Fi station behind
`wifi` in `esp32tl/interfaces/wifi.py`, the exceptions a test sees in
`esp32tl/interfaces/__init__.py`, the taps and the run's log in `esp32tl/tap.py` and
`esp32tl/report.py` ([run-report-technical.md](run-report-technical.md)), and the board
profiles and capability declaration in `esp32tl/boards/`
([board-profiles-technical.md](board-profiles-technical.md)). What the user
sees is in [pytest-plugin-user.md](pytest-plugin-user.md); the backend it starts is in
[esp32sim-backend-technical.md](esp32sim-backend-technical.md); the units and the receiver model
are in [models-technical.md](models-technical.md).

## Data flow

1. **Loading.** `pyproject.toml` registers the module under the `pytest11` entry point,
   `esp32tl = "esp32tl.pytest_plugin"`, so pytest loads it next to pytest-embedded's own plugin.
   esp32tl has a plugin of its own rather than a pytest-embedded service because pytest-embedded
   resolves `--embedded-services` through a closed dictionary and cannot load a third-party
   service ([plan section 1](../implementation-plan.md#1-findings-that-amend-the-specification),
   finding 1).
2. **`pytest_addoption`** adds the `esp32tl` group with `--esp32tl-backend`, whose choices are
   `list(SERVICES)`. **`pytest_configure`** registers the `app_path`, `app_recording` and
   `board_profile` markers, which `--strict-markers` would otherwise refuse.
3. **`pytest_generate_tests`** returns at once unless `board` is in `metafunc.fixturenames`, which
   holds the whole fixture closure, so a test that asks only for `console` counts. It starts from
   `metafunc.definition.path.parent`, joins the single argument of the closest `app_path` marker to
   it (an absolute argument replaces it, as `pathlib` joins) and resolves the result. A marker with
   other than one positional path (`str` or `Path`), an unknown keyword, or a `build_dir`
   that is not a non-empty string or a `Path` (`Path("")` is `Path(".")`, the app itself, and
   passes), is `pytest.fail(..., pytrace=False)`. It then loads the board profile the
   `board_profile` marker selects by bundled name or local `path=`. Local paths resolve against
   `metafunc.definition.path.parent` and are cached by absolute path in
   `config.stash[LOCAL_PROFILES]` for the session; `BoardMarkers` saves both the reference and
   the loaded object for each definition
   ([board-profiles-technical.md](board-profiles-technical.md#data-flow)); a `MarkerError` or
   `ProfileError` is raised as
   `pytest.fail.Exception(f"{nodeid}: {error}", pytrace=False) from None`, a collection error
   without the chained exception. The backends are `[chosen]` when `--esp32tl-backend` is set, else
   `["esp32sim"]`, each with a `pytest.param`; serial is opt-in. Then one call:

   ```python
   metafunc.parametrize(("board", "embedded_services", "app_path", "build_dir"), params, indirect=True)
   ```

   where every element of `params` is
   `pytest.param(backend, SERVICES[backend], str(app_path.resolve()),
   str((app_path / build_dir).resolve()), id=backend)`. `build_dir` comes from the closest
   marker's keyword, defaulting to `"build"`; relative builds resolve against the app.
   One `parametrize` over the four keeps them together under one id per backend. `indirect=True`
   hands each value to its fixture as `request.param`: `board` gets the backend name, and
   pytest-embedded's `embedded_services`, `app_path` and `build_dir` fixtures read the parameter
   before the command line (`_request_param_or_config_option_or_default` in
   `pytest_embedded/plugin.py`).
4. **`pytest_collection_modifyitems`**, `trylast=True` so that `-k` and `-m` have deselected
   their tests first, refuses the options esp32tl sets itself when any remaining item has `board`
   in its `fixturenames`: `--embedded-services`, `--app-path` and `--build-dir` when
   `config.getoption(option, None)` is truthy, and `--count` when it is not 1. It raises
   `pytest.UsageError`. The refusal is needed because a parameter of `None`, which is what
   `SERVICES` gives `esp32sim`, falls back to the command line in pytest-embedded: a session
   started with `--embedded-services esp` would make the `esp32sim` case open a serial port.
   It then refuses the first remaining item that uses `board` and whose board markers
   (`app_path`, `app_recording`, `board_profile`) say something else than its definition's did:
   `late_board_marker` reads them from the item, the profile reference with `profile_reference`,
   and compares them, one marker at a time, with the `BoardMarkers` (including a copy of
   `app_path.kwargs`) `pytest_generate_tests` kept in `config.stash[READ]`, by the definition's
   nodeid (the item's without `[{callspec.id}]`). A `pytest.param`'s marks, and those a hook of
   another plugin or a conftest adds (this one runs `trylast`), reach the item but not
   `metafunc.definition`, so they would go unchecked: a `board_profile` there would reach `board`
   but not the check, and an `app_path` there would not reach pytest-embedded. Profile references
   distinguish bundled names from local absolute paths, including different files with the same
   TOML name. A marker a fixture adds at setup is not seen.
   For a selected serial item it also requires an exact `--port` or indirect `port` parameter,
   refusing blank or wildcard values with `PORT_REQUIRED`, conflicting `ESPPORT` with
   `PORT_CONFLICT`, and separate flashing ports with `FLASH_PORT_UNSUPPORTED`. These checks run
   after deselection. `serial_artifact` checks resolved fixtures, the environment and cached
   pytest-embedded port options again before `app` setup, so a fixture override cannot restore
   discovery or redirect flashing. A cached port mismatch uses `CACHED_PORT_CONFLICT`; missing
   serial options use the services requirement error instead of a `KeyError`.
   Last, unless `timeout_configured(config)`, it adds
   `pytest.mark.timeout(DEFAULT_TIMEOUT)`, 600,
   to every item that uses `board` and has no `timeout` marker. `timeout_configured` holds when
   pytest-timeout is not loaded (`-p no:timeout`), or when
   `pytest_timeout.get_env_settings(config).timeout` is not `None`: that function reads
   `--timeout`, then `PYTEST_TIMEOUT`, then the `timeout` ini option, as pytest-timeout itself
   does, so a configured 0 counts as set. pytest-timeout reads the marker per item in its
   `pytest_runtest_protocol` hook, after collection, and times setup, call and teardown together.
5. **`board` fixture**, at setup: validates serial's artifact (and optional `app_recording`
   reference), then obtains `dut` with `request.getfixturevalue`. A `pytest_fixture_setup` hook
   validates before pytest-embedded's `app` too, covering tests that request `dut` directly.
   The digest is cached in the item's `SERIAL_ARTIFACT` stash. It refuses a non-single `Dut`,
   then constructs `Esp32Sim` or `Serial`, with the profile, the test file's directory and
   `skip_at_the_test`. Serial requires an `IdfDut`; its fixture setup has already handled
   flashing. `board_profile(request.node)` retrieves `BoardMarkers.loaded_profile` using the
   definition's nodeid, without resolving a symlink or reading the marker again at setup.
   `backend_failed(error, name)` raises a short pytest failure prefixed with the chosen
   backend. [Serial lifecycle](serial-backend-technical.md#data-flow) gives the timing details.
6. **A source or a tap the backend cannot give**, asked for in the body (`gnss.replay`,
   `gnss.fix`, `gnss.tap`), reaches
   `skip_at_the_test(reason)`, which raises
   `pytest.skip.Exception(reason, _use_item_location=True)`. `_use_item_location` is pytest's own
   argument for its skip marker: the report names the test's file and line, not the line of
   esp32tl's that raised the skip, which plain `pytest.skip` would name.
7. **`pytest_runtest_makereport`**, a `wrapper=True` hook, looks at the reports of the setup and
   call phases. When the call raised and `ended_while_waiting(call.excinfo.value)` holds, that is
   the error's `__cause__`/`__context__` chain holds a `RunEnded`, it sets
   `item.stash[CALL_SAW_RUN_END] = True`. It sets `DID_NOT_PASS` on an item whose setup or call
   did not pass, and `FAILED` on one whose setup or call failed. The call's report is made before
   the fixtures are torn down, so the stash is set by then.
8. **`board` fixture**, at teardown: `backend.close()`. A `BackendError` from it goes through
   `backend_failed` unless `request.node.stash.get(CALL_SAW_RUN_END, False)`: a test that waited on
   the run when it ended has already reported the exit code and the link's error, and would
   otherwise show them twice. When `close` returned, `backend.set_up()` names a source, a tap
   or a line the test drove and `backend.started` does not hold (a board switched off before
   teardown still ran its firmware), while `request.node.stash.get(DID_NOT_PASS, False)` does
   not hold either, it fails the teardown with `pytest.fail(..., pytrace=False)`:
   `esp32sim: the test set up the {kind} of {interface} and never called board.power_on(): it ran no firmware`
   (` and ` between several; `{kind}` is `replay` or `tap`, and a line reads `the level of
   {line}`). `DID_NOT_PASS` is `not report.passed`:
   failed, skipped, or xfailed, which pytest reports as skipped, so a test with an outcome of its
   own already is not reported on top of it. Then, whatever `close` did, it adds the report section
   `("teardown", "esp32sim link", backend.report(run=run))` with
   `request.node.add_report_section`, `run` holding when the teardown itself fails or
   `request.node.stash.get(FAILED, False)` holds, and appends
   `("esp32sim_link", json.dumps(backend.properties()))` to `request.node.user_properties`. The
   property is a JSON string because JUnit XML writes a property's `str()`, which for a `dict` is
   Python's `repr`. A test that failed on `RunEnded` and gets no teardown error has `FAILED` set,
   so its report shows the run too. The backend attributes `report_section` and
   `report_property` supply these names. Serial uses `serial console` and `serial_console`:
   its report gives console byte count, tap path and any error; failures include the tap, power-on
   and wait steps and console lines. Its JSON property gives `clock`, `app_sha256`, `console_bytes`,
   `error` and `tap` ([serial schema](serial-backend-technical.md#schema)).
9. **`console` fixture** returns the cached `board.console`. `Console.expect` and `Console.expect_exact`
   take a pattern or a list of them, compile each into a regular expression on bytes
   (`compiled`: text in UTF-8 and bytes with `re.DOTALL`, as pexpect compiles them for a bytes
   stream; `re.escape` for `expect_exact`; a compiled text pattern again with its flags but
   `re.UNICODE`, where pexpect drops every flag of it) and call
   `board.wait_on_clock(found, timeout_of(timeout), description)`. `timeout_of` takes a
   `Duration` as it is and a number as seconds, pytest-embedded's unit, through
   `esp32tl.units.number`, and refuses one below 0. `found` runs in the test's thread at every
   turn ([esp32sim-backend-technical.md](esp32sim-backend-technical.md#data-flow)). It skips a
   turn that left `board.console_output` as long as the last look saw it. Otherwise it searches
   the console from where the last match ended, with every pattern, through a `memoryview`
   released before it returns, so that the link can grow the buffer again; only for a hit does it
   copy that window to `bytes` and search it again, and it returns the window and the match that
   starts first, the first pattern's on a tie. The search is the window's length per turn, as
   pexpect's is per read, so waiting long on a busy console costs time that grows with the square
   of the wait.
   On a match the cursor moves past it, and the call returns the `re.Match`, or with
   `return_what_before_match` the window up to the match. `pexpect.EOF` among the patterns turns
   the `RunEnded` of the end of the run into a return: what is after the cursor, `rstrip()`ped as
   pytest-embedded does, or whole with `return_what_before_match`; the cursor moves to the end.
   `description` is the patterns' `repr`, ` or ` between them, then ` on the console`, which
   `NeverHappened` and `RunEnded` quote.
   `console.line(pattern)` and `console.lines(...)` read `board.console_lines`, a `ConsoleLines`
   that the backend feeds with every `console` message's time and bytes: it gathers the bytes up
   to each `\n` and appends a `Line(Duration(t), text)`, `t` being the time of the message that
   carried the line ending, `text` decoded as UTF-8 with replacement, its `\r\n` or `\n` and
   the colour codes `\x1b[…m` taken out (`COLOUR`). A line not ended stays out. `line` returns a
   `LineCondition`, which keeps the number of lines when it was made and, when called, searches
   those after it with the compiled pattern (`line_pattern`: text, or a compiled text pattern
   as it is) and returns the first match as a `Line` with its `match`, then starts its next look
   after that line; a look that finds none moves its start past the lines it saw. Its `repr`,
   `a console line matching {pattern!r}`, is
   what `board.wait_until` names it by, as an instance has no `__qualname__`. `lines(pattern=None,
   *, since=None)` filters the lines by `line.at >= since` and by the pattern. Neither moves
   `expect`'s cursor.
10. **`gnss` fixture** returns `board.uart("gnss")`, the `Gnss` the link carries for `uart.gnss`,
   a `Uart` with the receiver model's calls. A profile without the route makes `uart` raise
   `BackendError`, which goes through `backend_failed`. `Uart.replay(path)` is in
   [replay-technical.md](replay-technical.md#data-flow), `Uart.tap(path)` in
   [run-report-technical.md](run-report-technical.md#data-flow); before either makes anything it
   calls the backend's `asked(Request(...))`, which refuses a source or a tap after power-on or one
   no backend gives, and skips the test when this backend does not give it. Each model call
   (`fix`, `move`, `stand_still`, `degrade`, `lose_fix`, `go_silent`, `resume`) goes through
   `Gnss._tell(call, step)`: `call` on the route's source when it is a `GnssReceiver` already; a
   `SourceError` when it is a replay; otherwise `_ask("model")` first, the same `asked` check
   with `Request("uart.gnss", source="model")`, then `call` on a new `GnssReceiver(now, world)`,
   which becomes the source of kind `model`, its `source_detail` `a GNSS receiver`, only once
   `call` has returned, so a refused first call leaves the route without a source. Each call that
   succeeds goes into the `RunLog` as a step of the test at the board's time, as
   `uart.gnss.move(heading=NORTH, speed=m_s(10))`. `position` and `receiver()` read the receiver
   and make none: without one they raise `SourceError`. `now` and `world` are the backend's, the
   held clock's time and the world's time at power-on
   ([models-technical.md](models-technical.md#data-flow)).
11. **`lines` fixture** returns `board.lines`, the one `Lines` the link carries for every
   `[lines.<role>]` set of the profile, named `lines` on the link; a profile without lines makes
   `board.lines` raise `BackendError`, which goes through `backend_failed`. `Lines` is a `BoardLink`
   `Interface` without a `wire`: its `pins` are the lines' GPIOs, which the link watches.
   `drive(line, level)` and `release(line)` take a GPIO number or `<role>.<line>`, read the board's
   time from `now()`, the backend's `HeldClock.now` (0 before power-on and until the first turn),
   and, under the interface's lock, append a `LineEvent(Duration(t), name, pin, "test", level)` and
   queue `(t, pin, level)`, `None` for a release. A drive to the level the test drives already, or a
   release of a line it does not drive, does neither. The link asks `inputs(now)` in every turn,
   after the UARTs' outputs: in the run's first turn a `gpio_in` at `now` with every pulled line at
   its pull (`PULLED`, `up` 1 and `down` 0), then each queued change, stamped `max(t, now)` so that
   one asked for after its turn goes out in the next: a `gpio_in` with its level, a `gpio_in` with
   the pull for the release of a pulled line, a `release` for any other. The chip knows only its own
   pulls: a `release` puts the pad back to them (`release_input`, `esp32/src/periph.rs:384`), so the
   board's pull is a level the harness drives, and, as an external level, it takes the pad from the
   chip's pulls (`sync_pull` skips an external pad, `:394`); the firmware's output still wins over
   it, as `pad_level` prefers `driven`. The link calls `driven(t, changes, enabled)` on `gpio_out`:
   `Lines` keeps each pin's last reported pad level and counts a line as the firmware's while its
   bit is in `enabled` or its pad is reported at 1, since a pad a peripheral drives through the GPIO
   matrix may have no bit in `GPIO_ENABLE` and an undriven pad is reported at 0 (protocol 5.4). A
   change of that state, a level or `None`, appends `LineEvent(Duration(t), name, pin, "firmware",
   level)`, and `level(line)` returns it. With the backend's `RunLog`, every call of `drive` and
   `release` is a step of the run, and every change of the firmware's an entry
   ([run-report-technical.md](run-report-technical.md#data-flow)). `Lines` without an attached
   model declares no `latency` and no `quiet_until`: a change of the firmware's takes no turn
   of its own, and the test sees it at the next turn the clock lets the run take.
12. **`sd_card` fixture** returns `board.sd_card`, the `SdCard` of the profile's `[sd_card]`
   slot; a profile without one makes it raise `BackendError`, through `backend_failed`.
   `SdCard.insert(image)` refuses a second card, then calls the backend's
   `asked(Request("sd_card", source="model"))`, then resolves `image` against `directory`, the
   test file's, and checks with `stat` that it is a file of a whole number of `UNIT`s (512 KiB),
   at least one and at most `MAX_SIZE` (2 TiB), and with `os.access` that it can be read and
   written, and that no route replays from it or is tapped into it
   ([run-report-technical.md](run-report-technical.md#data-flow)). It keeps the absolute path in
   `image_path`, which `runner_command` turns into `--sd-spi`
   and `--sd-rw` at power-on ([esp32sim-backend-technical.md](esp32sim-backend-technical.md#data-flow)).

`Console.tap` delegates to the backend's `tap_console`. Both backends reuse `Uart.tap`'s file
checks for a console-only route and include it in teardown and the no-power-on guard. Serial's
console properties drain the reader queue before returning; waits also drain before checking
conditions, so `Console` uses the same matching and line logic on both clocks.

### Modem fixture and GPIO model responses

`pytest_plugin.modem` returns the backend's `interfaces.modem.Modem` for `uart.modem`. On serial,
model, replay and tap requests skip through the UART capability check. On esp32sim its first
model call selects `sources.model.modem.Modem`; the existing source and
tap checks still apply. See [modem-technical.md](modem-technical.md) for the AT parser and report.

`Lines.attach` reserves a model output and installs a callback for a firmware input. `driven`
collects callbacks under the line lock and invokes them after releasing it; the modem can then
queue RI with `model_drive` without a lock cycle. `quiet_until` names the earliest pending model
change, and `inputs` sends only those due, recording `LineEvent`s with `by="model"`. A new model
change replaces a pending response on the same pin, allowing the test to cancel an RI transition
before it reaches the wire. `events` returns chronological snapshots.
A line reserved for a model rejects direct test drive/release. While the modem is attached,
`Lines.latency()` is 1,000,000 ns, bounding GPIO replies as well as UART replies. With no model,
the existing no-latency behavior and board pulls are unchanged.

## Interfaces

| Name | Kind | What |
|---|---|---|
| `--esp32tl-backend NAME` | option | Choices `list(SERVICES)`; default unset, esp32sim only |
| `app_recording(path)` | marker | One positional path, relative to the test file or absolute; on serial checks the recording app hash before port setup; ignored on esp32sim |
| `app_path(path, build_dir="build")` | marker | One positional path, relative to the test file's directory; default that directory. The optional build path is relative to the app or absolute |
| `board_profile(name)` or `board_profile(path=...)` | marker | One bundled name or a local string/`Path` relative to the test file's directory; mixing them is a collection error ([board-profiles-technical.md](board-profiles-technical.md#interfaces)) |
| `board(request, app_path, build_dir, msg_queue, _meta)` | fixture, function scope | Yields an `Esp32Sim` or `Serial`; closes it at teardown, then adds its backend-specific report section and JSON property. Its methods (`power_on`, `wait`, `wait_until`, `now`, `utc`, `report`, `properties`) are in [esp32sim-backend-technical.md](esp32sim-backend-technical.md#interfaces) and [serial-backend-technical.md](serial-backend-technical.md#interfaces) |
| `console(board)` | fixture, function scope | Returns the cached `board.console` |
| `gnss(board)` | fixture, function scope | Returns `board.uart("gnss")`, a `Gnss` |
| `lines(board)` | fixture, function scope | Returns `board.lines` |
| `modem(board)` | fixture, `esp32tl/pytest_plugin.py` | The profile's `uart.modem`, a `Modem`; see [modem-user.md](modem-user.md) |
| `Modem(name, wire, *, clock, directory, now, lines, pins, asked=..., log=None, files=None)` | class, `esp32tl/interfaces/modem.py`, a `Uart` | Model calls, state properties and command history: [modem-technical.md](modem-technical.md#interfaces) |
| `Lines(board, lines, now, log=None)` | class, `esp32tl/interfaces/lines.py`, a `BoardLink` `Interface` named `lines` | The `[lines.<role>]` sets `lines` of the profile named `board`, the board's time from `now()`, in nanoseconds, and the `RunLog` it writes to, if any. `drive(line, level)`, `release(line)`, `level(line)` (`int \| None`), `events()` (`list[LineEvent]`); model attachment via `attach(owner, *, output, watch, driven, latency)` and `model_drive(owner, line, t, level)`; for the link, `pins`, `inputs(now)`, `driven(t, changes, enabled)`, `quiet_until()` and `latency()` |
| `LineEvent(t, name, pin, by, level)` | frozen dataclass, `esp32tl/interfaces/lines.py` | `t` a `Duration`, an instant of the board's time, `name` as `modem.ri`, `by` `"firmware"`, `"test"` or `"model"`, `level` 0, 1 or `None` |
| `PULLED` | `dict`, `esp32tl/interfaces/lines.py` | `{"up": 1, "down": 0}`: the level a board's pull gives an undriven line |
| `sd_card(board)` | fixture, function scope | Returns `board.sd_card` |
| `SdCard(route, *, directory, asked=..., log=None, files=None)` | class, `esp32tl/interfaces/sd_card.py` | The `sd_card` slot on `route` (an `SdCardRoute`): attributes `name` (`"sd_card"`), `route`, `directory`, `asked`, `log` (the `RunLog` its step goes to), `files` (shared with the routes), `image_path` (`None` until `insert`, then the image's absolute `Path`); `insert(image)` returns that path |
| `UNIT`, `MAX_SIZE` | `int`, `esp32tl/interfaces/sd_card.py` | 524288: an SD card image is a whole number of them; 2^41, the most it may be |
| `wifi(board)` | fixture, function scope | Returns `board.wifi` |
| `Wifi(*, now, world=..., asked=..., log=None)` | class, `esp32tl/interfaces/wifi.py`, a `BoardLink` `Interface` named `wifi` with no wire and `ethernet` true | The station's air. `access_point(ssid, *, password=None, channel=6, bssid=None)` returns the `Internet(now, world, link="ethernet")` behind it and starts it at 0; `nearby(ssid, *, channel=1, rssi=-60, secure=True, bssid=None)`; `access_point_off()`, `access_point_on()`; `network`, `on_air`, `configuration` (`AccessPoint \| None`), `around` (`tuple[Nearby, ...]`), `events` (`list[AirEvent]`); `runner_args()`, the runner's `--wifi` and `--wifi-nearby`; `report()` and `properties()`. For the link: `receive(t, direction, frame)` and `poll(now)` are the network's while it is active, `inputs(now)` the switches as `WifiAp`, `quiet_until()` the network's, `latency()` `LATENCY_NS` (1 ms) once there is an access point. `asked(Request("wifi", source="model"))` is called by `access_point` and `nearby` |
| `AccessPoint(ssid, password, channel, bssid)`, `Nearby(ssid, channel, rssi, secure, bssid)`, `AirEvent(t, on)` | frozen dataclasses, `esp32tl/interfaces/wifi.py` | What the test gave; `spec()` is the runner flag's value. `AirEvent.t` in nanoseconds of the board's time |
| `Console(board)` | class, `esp32tl/interfaces/console.py` | `expect(pattern, timeout=30, *, return_what_before_match=False)`, `expect_exact(...)` with the same arguments, `timeout` a `Duration` or a number of seconds; `line(pattern)`, `lines(pattern=None, *, since=None)`, `tap(path)`; no `write`. `board` is a `Clocked`: `console_output`, `console_lines` and `wait_on_clock(condition, within, description)`, `within` a `Duration` |
| `Line(at, text, match=None)` | frozen dataclass, `esp32tl/interfaces/console.py` | A console line: `at` a `Duration`, `text` a `str`, `match` an `re.Match[str]`, left out of comparisons; `line[group]` is `match[group]`, a `TypeError` without a match |
| `ConsoleLines()` | class, `esp32tl/interfaces/console.py` | `feed(t, data)`, `lines`: the console cut into `Line`s |
| `LineCondition(lines, pattern)` | class, `esp32tl/interfaces/console.py` | What `console.line` returns: called, the first matching `Line` since it was made, or since the last it found, or `None` |
| `DEFAULT_TIMEOUT` | `int`, `esp32tl/interfaces/console.py` | 30: an expect's `timeout`, in seconds of the board's time |
| `Gnss(name, wire, *, clock, directory, now, world, asked=..., log=None, files=None)` | class, `esp32tl/interfaces/gnss.py`, a `Uart` | `fix(at, *, hdop=0.9, satellites=8)`, `move(*, heading, speed)`, `stand_still()`, `degrade(*, hdop=None, satellites=None)`, `lose_fix()`, `go_silent()`, `resume()`, `position`, each the `GnssReceiver`'s; `receiver()` the receiver, made by the first call that succeeds |
| `Uart(name, wire, *, clock, directory, asked=..., log=None, files=None)` | class, `esp32tl/interfaces/uart.py`, a `BoardLink` `Interface` | `uart.<role>` and its source: attributes `clock`, `directory`, `asked`, `log`, `files` (the files the run replays from and taps into, shared by the backend's UARTs), `source`, `source_kind` and `source_detail` (`None` until given, then the source, `"replay"` or `"model"`, and the path as the test gave it or `a GNSS receiver`), and `tapped` (`None`, then the `Tap`); `replay(path)` returns the `Replay`, `tap(path)` the `Tap`. Without a source it hears everything and says nothing; with one, `quiet_until` and `latency` are the source's, and `receive` and `poll` return the source's outputs, read into a list. Every byte heard and every output goes to the tap and the `RunLog`, when there are. `asked(request)`, a `Request` of `esp32tl.boards.capabilities`, is called before a source or a tap is made, and refuses it by raising |
| `Source` | protocol, `esp32tl/interfaces/uart.py` | `receive(t, data)`, `poll(now)`, `quiet_until()`, `latency()`: what drives a `Uart` |
| `SourceError` | exception, `esp32tl/interfaces/__init__.py` | A source or a tap after power-on, a second one on a route or in the slot, one no backend gives, an image the card cannot hold, or a second access point |
| `BoardOff` | exception, `esp32tl/interfaces/__init__.py` | A wait, an expect, `now` or `utc` before `power_on()`, or a second `power_on()` |
| `RunEnded` | exception, `esp32tl/interfaces/__init__.py` | The run ended while the test waited |
| `NeverHappened` | `AssertionError`, `esp32tl/interfaces/__init__.py` | A condition or a pattern that did not come within its time |
| `SERVICES` | `dict[str, str \| None]`, `esp32tl/backends/__init__.py` | Backend name to the `embedded_services` it runs under, in parametrisation order; `None` is no service, pytest-embedded's base `Dut`. Now `{"esp32sim": None}` |
| `DEFAULT_TIMEOUT` | `int`, `esp32tl/pytest_plugin.py` | 600: the wall-time timeout, in seconds, of a test that uses `board` |
| `timeout_configured(config)` | function | Whether the session sets pytest-timeout's timeout, or pytest-timeout is not loaded |
| `CALL_SAW_RUN_END` | `pytest.StashKey[bool]` | Set on an item whose call failed on `RunEnded` |
| `DID_NOT_PASS` | `pytest.StashKey[bool]` | Set on an item whose setup or call failed, skipped or xfailed |
| `FAILED` | `pytest.StashKey[bool]` | Set on an item whose setup or call failed: its report shows the run |
| `skip_at_the_test(reason)` | function | Raises pytest's skip, reported at the test |
| `backend_failed(error)` | function | Raises the one-line `pytest.fail` for a `BackendError` |

`board` constructs `Esp32Sim` for every parameter and asserts that the parameter is its name: a
second entry in `SERVICES` without a change to `board` fails that assertion.

## Configuration

| Setting | Default | Read by |
|---|---|---|
| `--esp32tl-backend` | unset: `esp32sim` | `pytest_generate_tests` |
| `app_path` marker | the test file's directory | `pytest_generate_tests` |
| `board_profile` marker | `esp32-devkitc` (`DEFAULT_PROFILE`) | `pytest_generate_tests`, `board` |
| pytest-timeout's `--timeout`, `PYTEST_TIMEOUT`, `timeout` ini option, `timeout` marker | unset: `DEFAULT_TIMEOUT`, 600 s, for a test that uses `board` | `pytest_collection_modifyitems`, then pytest-timeout |
| pytest-embedded's `embedded_services`, `app_path`, `build_dir` | set per test by the parametrisation | pytest-embedded's fixtures |
| pytest-embedded's `--embedded-services`, `--app-path`, `--build-dir`, `--count` | must be unset (`--count` 1) when a selected test uses `board` | `pytest_collection_modifyitems` |

The marker's `build_dir` keyword defaults to `build`. The plugin resolves it against the app
and parametrises pytest-embedded's `build_dir` fixture with the absolute directory. `board`
passes that same value to `Esp32Sim`; serial's preflight and pytest-embedded's `IdfApp` use it too.
The backend's own environment variables are in
[esp32sim-backend-technical.md](esp32sim-backend-technical.md#configuration).

## Failure modes

| Message | When | Outcome |
|---|---|---|
| `{metafunc.definition.nodeid}: the app_path marker takes one path, and optionally build_dir as a non-empty path, as in @pytest.mark.app_path("../firmware/hello_world", build_dir="build")` | The marker has the wrong number/type of positional paths, an unknown keyword, or a build_dir that is not a non-empty string or Path | Collection error, exit code 2 |
| `{metafunc.definition.nodeid}: {error}` | A `board_profile` marker used otherwise than registered, an unknown bundled name, or a missing/invalid local file; the messages are in [board-profiles-user.md](board-profiles-user.md#when-no-backend-can-give-it) | Collection error, exit code 2 |
| `esp32sim: {reason}`, as `esp32sim: uart.gnss takes no replay source on this backend, only model` or `esp32sim: uart.gnss cannot be tapped on this backend` | The test asks a route for a source or a tap the backend's declaration does not give | The test is skipped at the call, reported at the test |
| `--esp32tl-backend: invalid choice: 'NAME'` (argparse's wording around it varies with the Python version) | A name not in `SERVICES` | Usage error, exit code 4 |
| ``ERROR: --{option} is set: esp32tl chooses it for every test that uses `board` (the backend through --esp32tl-backend, the app through the app_path marker, the build through that marker's build_dir keyword or its build/ directory)`` | `--embedded-services`, `--app-path` or `--build-dir` set, and a selected test uses `board` | Usage error, exit code 4, no traceback |
| `ERROR: --count is set: esp32tl runs one board in a test` | `--count` other than 1, and a selected test uses `board` | Usage error, exit code 4 |
| `ERROR: {item.nodeid}: the {marker} marker reached the test after esp32tl read it, from a pytest.param or a hook: put it on the test, its class or its module` | A selected test that uses `board` carries a board marker from a `pytest.param` or a hook | Usage error, exit code 4 |
| `count is above 1: esp32tl runs one board in a test` | The test parametrises `count` above 1 itself, so `dut` is a tuple | Error at setup |
| `esp32sim: {error}` | `Esp32Sim(...)` raised a `BackendError` | Error at setup; messages in [esp32sim-backend-technical.md](esp32sim-backend-technical.md#failure-modes) |
| `esp32sim: the board profile {name} has no uart.gnss; its interfaces are {interfaces}` | The test uses `gnss` on a profile without the route | Error at setup |
| `esp32sim: the board profile {name} has no lines; its interfaces are {interfaces}` | The test uses `lines` on a profile without `[lines.<role>]` sets | Error at setup |
| `esp32sim: the test set up the {kind} of {interface} and never called board.power_on(): it ran no firmware`, `the level of {line}` for a line, `a card in sd_card` for a card | A test whose setup and call passed, which gave a source, asked for a tap, drove a line or inserted a card and never powered on | Error at teardown |
| `GPIO {pin} is no line of the board profile {board}; its lines are {name} (GPIO {pin}), ...`, `{line!r} is no line ...`, `{line!r}: the board profile {board} has no lines` | `ValueError` from `drive`, `release` or `level` | Fails the test |
| `lines.drive: level {level!r} is not 0 or 1` | `ValueError` for an integer other than 0 or 1, `TypeError` for what is not an integer | Fails the test |
| `{interface} sent {type} on GPIO {pin}, which another interface is wired to` | An interface's `inputs` returned a `gpio_in`, `release` or `adc` on a pin of another interface's; `Lines` sends only on its own | The link's error: the run ends with `stop`, a waiting test gets `RunEnded`, the teardown `the link failed: ...` |
| `the pins of {interface} hold {pin!r}, not a GPIO number`, `GPIO {pin} is on {interface} and {other}` | `ValueError` from `BoardLink(...)`, for an interface whose `pins` hold something other than a GPIO number, or two interfaces on one pin; a profile cannot give either | Error at setup, with its traceback |
| `esp32sim: the board profile {name} has no sd_card; its interfaces are {interfaces}` | The test uses `sd_card` on a profile without a slot | Error at setup |
| `the board is off; call board.power_on() first` | `BoardOff` from a wait, an expect, `now` or `utc` before power-on; on esp32sim also from `boots` and `power_off()` (on serial these two skip the test) | Fails the test |
| `the board is on already: call board.power_off() first` | `BoardOff` from `power_on()` on esp32sim with the supply on | Fails the test |
| `the board is off already: call board.power_on() to switch it on`, `the board is off: board.reset() resets a board that is on` | `BoardOff` from `power_off()` with the supply off, `reset()` with it off ([resets and power cycles](pytest-plugin-user.md#resets-and-power-cycles)) | Fails the test |
| `{interface}: the board is on already: give an interface its source before board.power_on()` (`the board has been on` after `power_off()`), `no backend takes a '{source}' source on {interface}; the sources there are {sources}` | `SourceError` from `Uart.replay`, or from the first model call of `Gnss` | Fails the test ([replay-user.md](replay-user.md#when-the-test-gets-it-wrong)) |
| `uart.gnss has its source already: one per test` | `SourceError` from `Uart.replay` on a route with a source, a replay or a model | Fails the test |
| `uart.gnss replays a recording: it has no receiver to describe; one source per test` | `SourceError` from a model call, `position` or `receiver()` of `Gnss` after `gnss.replay` | Fails the test |
| `uart.gnss has no receiver: give it one with gnss.fix(at=...)` | `SourceError` from `Gnss.position` or `receiver()` before any model call | Fails the test |
| `{interface}: the board is on already: tap a route before board.power_on()`, `uart.gnss is tapped already: one tap per test`, `no backend can tap {interface}`, `{interface}: cannot tap into {path}: it is the {kind} of {route}`; `{interface}: cannot tap into {path}: {directory} is not a directory` | `SourceError`, and `FileNotFoundError` for the last, from `Uart.tap`; `{interface}: cannot replay {path}: it is the tap of {route}` or `... it is the card of sd_card` from `Uart.replay` | Fails the test ([run-report-user.md](run-report-user.md#when-the-test-gets-it-wrong)) |
| `sd_card: the board is on already: give an interface its source before board.power_on()`, `sd_card has a card already: one per test`, `sd_card: no image at {path}: {strerror}`, `sd_card: {path} is not a file`, `sd_card: {path} has {size} bytes; an SD card image is a whole number of 512 KiB units`, `sd_card: {path} has {size} bytes; an SD card image is at most 2 TiB`, `sd_card: {path} cannot be read and written: the card writes into it`, `sd_card: cannot hold {path}: it is the {kind} of {interface}` | `SourceError` from `SdCard.insert` | Fails the test |
| `{description} did not happen within {within}: waited from {start} to {end} of the board's time` | `NeverHappened` from `wait_until` or an expect | Fails the test |
| `the run ended {when} while the test waited {what}: {reason}` | `RunEnded` from a wait or an expect; `{when}` is `at {time}` from the runner's `stats`, `after the turn at {time}` without them, the last held turn being only a bound, or `before its first turn`; `{what}` is `until {time}` or `for {description}`, `{reason}` the backend's ([esp32sim-backend-technical.md](esp32sim-backend-technical.md#failure-modes)) | Fails the test |
| `expect: no pattern to wait for`, `expect: {pattern!r} is not a pattern`, `expect_exact takes text or bytes, not a compiled pattern`, `expect: timeout is {timeout!r}, not a Duration or a number of seconds`, `expect: timeout is {timeout}, less than 0` | `ValueError`, `TypeError` from an expect | Fails the test |
| `console.line: {pattern!r} is not a regular expression on text`, `console.lines: since is {since!r}, not a Duration, as seconds(5)`, `{text!r} was found by no pattern, and has no groups` | `TypeError` from `line`, `lines`, or a `Line`'s group | Fails the test |
| `Failed: Timeout (>{timeout}s) from pytest-timeout.` | The test ran out of its wall-time timeout | Fails the test; the teardown ends the runner |
| `esp32sim: {problems}; its output is in {self.dut.logfile}` | `Esp32Sim.close` raised, and the call did not fail on `RunEnded`. `{problems}` is the exit code, the kill or the link's error ([esp32sim-backend-technical.md](esp32sim-backend-technical.md#failure-modes)) | Error at teardown, also after a passing call |

The refusals are raised in `pytest_collection_modifyitems`, not `pytest_generate_tests`: raised
there, a `UsageError` (or `pytest.exit`) is a collection error, with a traceback through pluggy and
exit code 2; here it is one line and exit code 4.

The `app_recording` collection error includes the test id:
`{nodeid}: the app_recording marker takes one path, as in @pytest.mark.app_recording("hello-console.jsonl")`.
Serial checks the explicit port against `ESPPORT`, refuses a separate `flash_port`, and checks
resolved fixtures and cached pytest-embedded options before opening the device.
Serial's port selection and setup failures are listed in
[serial failure modes](serial-backend-technical.md#failure-modes). `Console.tap` raises
`SourceError: this board cannot tap its console` for an external `Clocked` object without
`tap_console`; both shipped backends implement it.

## Tests

`tests/test_pytest_plugin.py`, which runs pytest in a subprocess through `pytester` because
pytest-embedded starts a multiprocessing manager for every session:

- `test_board_is_parametrised_with_the_backend_its_services_and_the_app`: the id
  `test_marked[esp32sim]`, the parameters `("esp32sim", None, <resolved app path>)` with and
  without the marker, and no `callspec` on a test without `board`. It replaces `board` with a
  fixture that returns its parameters (`SHOW_PARAMS`).
- `test_backend_option_takes_a_backend_name_only`: the id with `--esp32tl-backend esp32sim`, and
  the usage error for `qemu`.
- `test_pytest_embedded_options_are_refused_for_tests_with_a_board`: exit code 4, the message, no
  traceback. `test_more_than_one_dut_is_refused`: exit code 4 and the message.
- `test_options_are_allowed_when_no_selected_test_uses_board`: `-k` deselection before the
  refusal.
- `test_a_parametrised_count_above_one_is_refused`, `test_app_path_marker_needs_one_path`.
- `test_a_test_that_uses_board_gets_a_wall_time_timeout_unless_one_is_set`: the 600 s marker,
  and none with `--timeout 5`, `-o timeout=0`, `PYTEST_TIMEOUT=0`, `-p no:timeout`, or a marker
  of the test's own.
- Against a fake runner that prints and exits without connecting (`FAKE_RUNNER`):
  `test_a_wait_fails_at_once_when_the_runner_has_exited`,
  `test_a_runner_that_fails_to_start_is_reported_once` (the stash keeps the teardown quiet after
  `RunEnded`), `test_the_runner_starts_at_power_on_on_the_test_s_clock` (its command line),
  `test_a_runner_that_exits_with_an_error_fails_the_test`,
  `test_a_failure_of_another_kind_does_not_hide_the_runner_s_exit`,
  `test_the_board_s_clock_and_sources_go_in_order` (each `BoardOff` and `SourceError`, of
  sources and taps, a tap into no directory, and a model after power-on, after a replay and
  before one),
  `test_a_source_or_a_tap_the_backend_cannot_give_skips_at_the_call` (the skip at the test's line,
  and the error for a source or a tap no backend gives, with a declaration a conftest swaps in),
  `test_a_model_the_backend_cannot_give_skips_at_the_call`,
  `test_a_test_that_sets_up_a_source_and_never_powers_on_fails_at_teardown` (a replay and a
  model; only the tests that passed are reported, and no runner starts),
  `test_the_lines_fixture_needs_a_profile_with_lines_and_a_drive_needs_power_on` (the error at
  setup on `esp32-devkitc`, a test that only reads the lines, and the teardown error for two
  driven lines) and `test_a_test_that_taps_a_route_and_never_powers_on_fails_at_teardown` (no
  file written).
- Against a fake runner that speaks the link protocol (`LINK_RUNNER`):
  `test_the_backend_serves_the_link_and_ends_the_run_with_stop` (the `esp32sim link` section
  with `-rA`, and the `esp32sim_link` property read back from `--junitxml` as JSON),
  `test_every_route_and_line_of_the_profile_is_on_the_link`, `test_the_test_s_waits_move_the_board_s_clock`,
  `test_a_tap_records_what_crossed_the_route_both_ways` and
  `test_the_report_of_a_failing_test_shows_the_run_on_the_board_s_clock`,
  `test_a_tap_that_cannot_be_written_fails_the_test_at_teardown`
  ([run-report-technical.md](run-report-technical.md#tests)),
  and the link and teardown failures listed in
  [esp32sim-backend-technical.md](esp32sim-backend-technical.md#tests), among them
  `test_a_hung_run_is_ended_by_the_test_s_timeout`.

`tests/test_clock.py` tests `HeldClock` and `Console` without an emulator, a runner thread
feeding `BoardLink`: that the run stands still until the test waits, that `wait` lands on its
instant, the 100 ms steps of `wait_until`, `NeverHappened` and `RunEnded` with their messages, a
condition that raises, a last look after the run's end, and `expect`'s search from the last match,
lists, `expect_exact`, `return_what_before_match`, `pexpect.EOF` and the board-time `timeout`, as a
number and as a `Duration`; the console's lines, their times, colour codes and partial lines, and
`line` as a condition from when it was made
(`test_console_lines_are_stamped_with_the_time_their_ending_came_at`,
`test_a_line_condition_finds_the_first_matching_line_from_when_it_was_made`).

The `board_profile` marker is tested in `tests/test_pytest_plugin.py` too:
`test_a_marker_no_backend_can_satisfy_is_a_collection_error`,
`test_markers_on_a_class_or_a_module_count`, `test_a_board_marker_in_a_pytest_param_is_refused`,
`test_a_board_marker_a_hook_adds_is_refused` and
`test_the_board_runs_the_profile_the_test_chose_and_starts_no_runner_unpowered`
([board-profiles-technical.md](board-profiles-technical.md#tests)).

`tests/test_lines.py` tests `Lines` without an emulator: the names, the refused lines and levels,
the pulls in the first turn, the stamps of what the test drives, what is no change, the firmware's
level from `gpio_out`, and `Lines` on a `BoardLink`. `tests/test_gpio_echo.py` runs `gpio_echo` on
the real runner on the LilyGO profile: RI at its pull-down from power-on, PWRKEY following RI
within a millisecond of the board's time both ways, the events with their times, and a level
driven before power-on.

`tests/test_gnss_fixture.py` runs `gnss.replay` and `gnss.tap` into `nmea_reader` on the real
runner, and,
through `pytester`, a source after power-on, an expect before it, a source never powered on and a
profile without the route ([replay-technical.md](replay-technical.md#tests)).
`tests/test_replay.py` and `tests/test_tap.py` test `Uart` without an emulator.
`tests/test_sdspi.py` runs `sd_card.insert` on ESP-IDF's `sdspi` example on the real runner, and,
through `pytester`, a card after power-on, one never powered on, two cards, a missing, a short and
a read-only image, and a profile without a slot;
`test_runner_command_puts_a_card_in_the_slot_once_the_test_inserts_one` in
`tests/test_pytest_plugin.py` checks the runner's flags. Not covered: an image path that is a
directory, and one above 2 TiB.

`tests/test_nmea_reader_model.py` runs the receiver model into `nmea_reader` on the real runner,
with `console.line` and `console.lines` ([models-technical.md](models-technical.md#tests)).

`tests/test_package.py` checks the `pytest11` entry point (`test_plugin_is_registered_through_pytest11`)
and that pytest loaded the module under the name `esp32tl` (`test_pytest_loaded_the_plugin`).
`tests/test_hello_world.py`, `tests/test_nmea_reader.py` and `tests/test_gpio_echo.py` are the
end-to-end cases on the real runner and images.

Build-selection tests in `tests/test_pytest_plugin.py` cover app-relative and absolute paths,
spaces, invocation from another directory, default/module/class/test precedence, late keyword
mutation, invalid selectors and the no-fallback guarantee.
`test_two_builds_of_one_app_run_the_selected_firmware` runs two distinct copies of `hello_world`
through the real emulator and verifies both their console text and tap hashes; it needs the built
`hello_world` image, runner and ROM ELF.
`tests/test_serial_backend.py::test_serial_preflight_and_flashing_use_the_selected_build` checks the
real pytest-embedded fixture chain with a fake serial port, including hash, target, encryption and
missing-file failures before port setup.
`tests/test_cli.py::test_cli_passes_only_the_selected_build_to_the_runner` checks both CLI commands
with a fake runner that reads the exact files passed to it.

Not covered: deselection by `-m` (only `-k` is tested), running several backends in one
session, and `power_on(utc=...)` with a timezone other than UTC.

## See also

- [pytest plugin](pytest-plugin-user.md)
- [esp32sim backend: how it works](esp32sim-backend-technical.md)
- [Link protocol, `gpio_out`, `gpio_in` and `release`](../protocol.md#54-gpio_out-m3)
- [Plan section 3, architecture](../implementation-plan.md#3-architecture)
- [Invariants](../invariants.md)


## Named GPIO output conditions

`Lines.output(line, active=1)` in `esp32tl/interfaces/lines.py` resolves and validates the line
immediately and returns an immutable `OutputLine`. The handle creates callable
`OutputCondition` values through `is_on` and `is_off`. Each call reads `Lines.level(pin)` under
its existing lock and compares it to the configured active or inactive level. `None` matches
neither. The handle's `events()` filters a snapshot of `Lines.events()` by pin, preserving the
origin and timestamp; it queues no messages and reserves no pins.

Conditions have a stable `repr`, such as `modem.pwrkey is on (GPIO 4, level 1)`. The existing
`Esp32Sim.wait_until` uses that representation in the run log and timeout errors; no new clock
or polling path is involved. Their boolean conversion raises a `TypeError` to catch assertions
that forgot to call the condition. The existing `Lines.level` API retains its 0/1/None semantics.

`tests/test_lines.py` checks both polarities, unknown/released outputs, live updates, name and
polarity validation, event filtering and the absence of input writes. The real-firmware test
`tests/test_gpio_echo.py::test_named_output_waits_report_the_line_and_state` checks successful
waits and the `NeverHappened` diagnostic through the board's clock.

`OutputLine.assert_on` / `assert_off` read the current firmware level without advancing time.
`assert_stayed_on(since=...)` / `assert_stayed_off(since=...)` take a sorted event snapshot,
filter to firmware events, reconstruct the state from the last event at or before the starting
instant, and check every transition after it through the current instant. Ties at the starting
instant use the last event in arrival order; all later transitions are checked, even when they
share a timestamp. No prior firmware event means unknown, which fails. The caller uses the
held board clock, as with other line reads. History is retained for the run; assertions do not
consume it. Input events cannot satisfy or violate a firmware-output assertion.

`tests/test_lines.py` covers current-state diagnostics, both polarities and states, a one-nanosecond
pulse, unknown/released outputs, starting/end boundaries, input filtering and invalid windows.


## Shared board and FAT image API

`esp32tl.interfaces.board.Board` is the abstract base of `Esp32Sim` and `Serial`, re-exported
from `esp32tl` and, for existing imports, `esp32tl.pytest_plugin`. It declares the common clock,
waiting, NVS, reporting and lifecycle API: `power_on`, `power_off`, `reset`, `powered`, `started`
and `boots` are abstract, and `boot` is `boots[-1]`. `Boot(number, at, cause)` and its `reason`,
from `RESET_REASONS`, the classic chip's ROM names, are in the same module, with
`POWERON_RESET = 1`. Interface properties reuse each backend's existing
UART/line/slot objects; `console` is a `cached_property` wrapping that backend's console buffers.
Fixture functions return the properties, preserving object identity and the console cursor.
The `broker` fixture delegates to `modem.network().broker`, which selects a model source.
`Board` has no broker property; tests selecting the service through the board explicitly call
`board.modem.network().broker`.

`SdCard.image(size_mb=32)` returns `CardImageBuilder`; both this call and `build()` check that
the slot is open and call `asked(Request("sd_card", source="model"))`. A saved builder therefore
cannot bypass the before-power-on or backend-capability checks. Declaring files allocates no
image. Each build creates a unique subdirectory of a lazily allocated `TemporaryDirectory`,
truncates `card.img`, formats it with `mformat` (FAT32 forced from 512 MiB), creates parents with
`mmd`, and copies bytes through temporary host files using `mcopy`. Failed builds remove their
partial directory. Duplicate and file/parent conflicts are checked case-insensitively.

`CardImage` exposes `path`, `__fspath__`, `read_bytes`, `read_text` and `list_files`. Reads use
`mtype`; listings use `mdir -b` and exclude directory entries. Before either operation,
`_resolve` walks directory entries case-insensitively to check the requested path's existence and
type. Missing entries raise `FileNotFoundError`, non-directory parents or listing targets raise
`NotADirectoryError`, and reading a directory raises `IsADirectoryError`. This uses actual
entries rather than parsing locale-dependent tool errors. `_path` rejects `*`, `?`, `[` and `]`
for both builder and reader calls, preventing mtools from expanding a requested single path
into several files. No FAT state or file contents are cached. All commands run without a shell and with `MTOOLS_SKIP_CHECK=1`; command failure becomes
`SourceError` including stderr. Text encoding/decoding defaults to UTF-8 with no newline
translation. `SdCard.close()` invalidates handles and removes only builder-owned directories;
backends call it after stopping the run, including on teardown errors. The inserted path stays
in `image_path` for reporting, even after the owned image has been removed.

### The Wi-Fi station

`Wifi` is on every backend's `Board` as `board.wifi`. On esp32sim it is one of the `BoardLink`'s
interfaces from construction, the only one whose `ethernet` is true, so the link hands it every
`eth_tx` frame and sends what it returns as `eth_rx` ([protocol](../protocol.md)). `access_point`
validates its arguments first, then calls `asked`, which refuses after power-on and skips on a
backend whose declaration has no `model` source for `wifi`, then makes the network and records the
access point under a lock, so that a call that fails leaves nothing behind. Before power-on the
backend reads `runner_args()` into the runner's command line; without an access point it adds no
flag, and the station finds nothing. `access_point_off` and `access_point_on` read the board's
clock, stop or start the network at that instant (`Internet.stop` drops TCP connections and queued
replies; `Internet.start` brings the Ethernet link up again, its DHCP leases kept), log the switch
in the network's events and the `RunLog`, and queue a `WifiAp` that `inputs` sends in the link's
next turn, at that instant or the turn's, the later of the two. While the network is not active
the station's frames reach nothing. On serial `board.wifi` is a `Wifi` that is on no link: its
`asked` skips `access_point` and `nearby` through the capability declaration.

`tests/test_wifi.py` covers the flags, the parameter checks, the capability request, the switches
and their timing without the emulator; `tests/test_http_request.py` runs the station against
ESP-IDF's `http_request` example: a fetch, a rejoin after the access point leaves and comes back,
no access point, a wrong password, the report and properties, and a call after power-on.

`tests/test_board_api.py` covers fixture identity, lazy interface access, broker sharing,
build-after-power-on refusal and backend cleanup. `tests/test_card_image.py` exercises real
FAT12/16/32 images, nested paths, binary/text fidelity, conflicts, failed builds and ownership.
It also checks standard path exceptions and bracket-pattern rejection with `A.TXT`, `B.TXT`
and an externally created literal `[AB].TXT` on the same image.
The logger integration tests verify that repeated reads see fresh guest writes.
