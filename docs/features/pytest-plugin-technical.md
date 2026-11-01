# pytest plugin: how it works

The plugin is `esp32tl/pytest_plugin.py`, with the backend registry in
`esp32tl/backends/__init__.py`, the console in `esp32tl/interfaces/console.py`, the UART
interface in `esp32tl/interfaces/uart.py` and the one behind `gnss` in
`esp32tl/interfaces/gnss.py`, the exceptions a test sees in
`esp32tl/interfaces/__init__.py`, and the board
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
   `list(SERVICES)`. **`pytest_configure`** registers the `app_path`, `board_profile` and `tap`
   markers, which `--strict-markers` would otherwise refuse.
3. **`pytest_generate_tests`** returns at once unless `board` is in `metafunc.fixturenames`, which
   holds the whole fixture closure, so a test that asks only for `console` counts. It starts from
   `metafunc.definition.path.parent`, joins the single argument of the closest `app_path` marker to
   it (an absolute argument replaces it, as `pathlib` joins) and resolves the result. A marker with
   other than one positional argument, or with any keyword argument, is `pytest.fail(...,
   pytrace=False)`. It then loads the board profile and the test's taps from the `board_profile`
   and `tap` markers and checks them
   ([board-profiles-technical.md](board-profiles-technical.md#data-flow)); a `MarkerError`,
   `ProfileError` or `CapabilityError` is raised as
   `pytest.fail.Exception(f"{nodeid}: {error}", pytrace=False) from None`, a collection error
   without the chained exception. The backends are `[chosen]` when `--esp32tl-backend` is set, else
   `list(SERVICES)`. Each gets a `pytest.param`, marked `skip` with
   `f"{backend}: {reason}"` when `refusal(backend, requests, declared())` gives a reason. Then
   one call:

   ```python
   metafunc.parametrize(("board", "embedded_services", "app_path"), params, indirect=True)
   ```

   where every element of `params` is
   `pytest.param(backend, SERVICES[backend], str(app_path.resolve()), marks=marks, id=backend)`.
   One `parametrize` over the three keeps them together under one id per backend. `indirect=True`
   hands each value to its fixture as `request.param`: `board` gets the backend name, and
   pytest-embedded's `embedded_services` and `app_path` fixtures read the parameter before the
   command line (`_request_param_or_config_option_or_default` in `pytest_embedded/plugin.py`).
4. **`pytest_collection_modifyitems`**, `trylast=True` so that `-k` and `-m` have deselected
   their tests first, refuses the options esp32tl sets itself when any remaining item has `board`
   in its `fixturenames`: `--embedded-services`, `--app-path` and `--build-dir` when
   `config.getoption(option, None)` is truthy, and `--count` when it is not 1. It raises
   `pytest.UsageError`. The refusal is needed because a parameter of `None`, which is what
   `SERVICES` gives `esp32sim`, falls back to the command line in pytest-embedded: a session
   started with `--embedded-services esp` would make the `esp32sim` case open a serial port.
   It then refuses the first remaining item that uses `board` and whose board markers
   (`app_path`, `board_profile`, `tap`) say something else than its definition's did:
   `late_board_marker` reads them from the item with `board_profile` and `board_requests` and
   compares them, one marker at a time, with the `BoardMarkers` `pytest_generate_tests` kept in
   `config.stash[READ]`, by the definition's nodeid (the item's
   without `[{callspec.id}]`). A `pytest.param`'s marks, and those a hook of another plugin or a
   conftest adds (this one runs `trylast`), reach the item but not `metafunc.definition`, so
   they would go unchecked: a `tap` there would never skip its backend, and a `board_profile`
   there would reach `board` but not the check. A marker a fixture adds at setup is not seen.
   Last, unless `timeout_configured(config)`, it adds `pytest.mark.timeout(DEFAULT_TIMEOUT)`, 600,
   to every item that uses `board` and has no `timeout` marker. `timeout_configured` holds when
   pytest-timeout is not loaded (`-p no:timeout`), or when
   `pytest_timeout.get_env_settings(config).timeout` is not `None`: that function reads
   `--timeout`, then `PYTEST_TIMEOUT`, then the `timeout` ini option, as pytest-timeout itself
   does, so a configured 0 counts as set. pytest-timeout reads the marker per item in its
   `pytest_runtest_protocol` hook, after collection, and times setup, call and teardown together.
5. **`board` fixture**, at setup: asserts `request.param == Esp32Sim.name`, makes the test an error
   if `dut` is not a single `Dut` (pytest-embedded makes a tuple of duts when `count` is
   parametrised above 1), and constructs
   `Esp32Sim(dut, app_path, msg_queue, _meta, board_profile(request.node), directory=request.path.parent, skip=skip_at_the_test)`.
   The directory is the test file's. The backend starts no runner here: it does at
   `board.power_on()` ([esp32sim-backend-technical.md](esp32sim-backend-technical.md#data-flow)).
   A `BackendError` goes through `backend_failed`, which raises
   `pytest.fail.Exception(f"{Esp32Sim.name}: {error}", pytrace=False) from None`: one line, no
   chained traceback.
6. **A source the backend cannot give**, asked for in the body (`gnss.replay`, `gnss.fix`), reaches
   `skip_at_the_test(reason)`, which raises
   `pytest.skip.Exception(reason, _use_item_location=True)`. `_use_item_location` is pytest's own
   argument for its skip marker: the report names the test's file and line, not the line of
   esp32tl's that raised the skip, which plain `pytest.skip` would name.
7. **`pytest_runtest_makereport`**, a `wrapper=True` hook, looks at the report of the call phase.
   When the call raised and `ended_while_waiting(call.excinfo.value)` holds, that is the error's
   `__cause__`/`__context__` chain holds a `RunEnded`, it sets `item.stash[CALL_SAW_RUN_END] = True`.
   The call's report is made before the fixtures are torn down, so the stash is set by then.
8. **`board` fixture**, at teardown: `backend.close()`. A `BackendError` from it goes through
   `backend_failed` unless `request.node.stash.get(CALL_SAW_RUN_END, False)`: a test that waited on
   the run when it ended has already reported the exit code and the link's error, and would
   otherwise show them twice. When `close` returned, `backend.set_up()` names a source and
   `backend.powered` does not hold, while `request.node.stash.get(DID_NOT_PASS, False)` does not
   hold either, it fails the teardown with `pytest.fail(..., pytrace=False)`:
   `esp32sim: the test set up the {kind} of {interface} and never called board.power_on(): it ran no firmware`
   (` and ` between several). `pytest_runtest_makereport` sets `DID_NOT_PASS` on an item whose setup
   or call did not pass (`not report.passed`: failed, skipped, or xfailed, which pytest reports as
   skipped), so a test with an outcome of its own already is not reported on top of it. Then,
   whatever `close` did, it adds the report section
   `("teardown", "esp32sim link", backend.link.report())` with `request.node.add_report_section`,
   and appends `("esp32sim_link", json.dumps(backend.link.properties()))` to
   `request.node.user_properties`. The property is a JSON string because JUnit XML writes a
   property's `str()`, which for a `dict` is Python's `repr`.
9. **`console` fixture** returns `Console(board)`. `Console.expect` and `Console.expect_exact`
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
   [replay-technical.md](replay-technical.md#data-flow); before it reads the recording it calls
   the backend's `asked(interface, "replay")`, which refuses a source after power-on or one no
   backend gives, and skips the test when this backend does not give it. Each model call
   (`fix`, `move`, `stand_still`, `degrade`, `lose_fix`, `go_silent`, `resume`) goes through
   `Gnss._tell(call)`: `call` on the route's source when it is a `GnssReceiver` already; a
   `SourceError` when it is a replay; otherwise `_ask("model")` first, the same `asked` check
   with `"model"`, then `call` on a new `GnssReceiver(now, world)`, which becomes the source of
   kind `model` only once `call` has returned, so a refused first call leaves the route without a
   source. `position` and `receiver()` read the receiver and make none: without one they raise
   `SourceError`. `now` and
   `world` are the backend's, the held clock's time and the world's time at power-on
   ([models-technical.md](models-technical.md#data-flow)).

## Interfaces

| Name | Kind | What |
|---|---|---|
| `--esp32tl-backend NAME` | option | Choices `list(SERVICES)`; default unset, every backend |
| `app_path(path)` | marker | One positional path, relative to the test file's directory; default that directory |
| `board_profile(name)`, `tap(*interfaces)` | markers | The board profile and the test's taps ([board-profiles-technical.md](board-profiles-technical.md#interfaces)) |
| `board(request, dut, app_path, msg_queue, _meta)` | fixture, function scope | Yields an `Esp32Sim`, off; closes it at teardown, then adds the `esp32sim link` report section and the `esp32sim_link` user property. Its methods (`power_on`, `wait`, `wait_until`, `now`, `utc`) are in [esp32sim-backend-technical.md](esp32sim-backend-technical.md#interfaces) |
| `console(board)` | fixture, function scope | Returns `Console(board)` |
| `gnss(board)` | fixture, function scope | Returns `board.uart("gnss")`, a `Gnss` |
| `Console(board)` | class, `esp32tl/interfaces/console.py` | `expect(pattern, timeout=30, *, return_what_before_match=False)`, `expect_exact(...)` with the same arguments, `timeout` a `Duration` or a number of seconds; `line(pattern)`, `lines(pattern=None, *, since=None)`; no `write`. `board` is a `Clocked`: `console_output`, `console_lines` and `wait_on_clock(condition, within, description)`, `within` a `Duration` |
| `Line(at, text, match=None)` | frozen dataclass, `esp32tl/interfaces/console.py` | A console line: `at` a `Duration`, `text` a `str`, `match` an `re.Match[str]`, left out of comparisons; `line[group]` is `match[group]`, a `TypeError` without a match |
| `ConsoleLines()` | class, `esp32tl/interfaces/console.py` | `feed(t, data)`, `lines`: the console cut into `Line`s |
| `LineCondition(lines, pattern)` | class, `esp32tl/interfaces/console.py` | What `console.line` returns: called, the first matching `Line` since it was made, or since the last it found, or `None` |
| `DEFAULT_TIMEOUT` | `int`, `esp32tl/interfaces/console.py` | 30: an expect's `timeout`, in seconds of the board's time |
| `Gnss(name, wire, *, clock, directory, now, world, asked=...)` | class, `esp32tl/interfaces/gnss.py`, a `Uart` | `fix(at, *, hdop=0.9, satellites=8)`, `move(*, heading, speed)`, `stand_still()`, `degrade(*, hdop=None, satellites=None)`, `lose_fix()`, `go_silent()`, `resume()`, `position`, each the `GnssReceiver`'s; `receiver()` the receiver, made by the first call that succeeds |
| `Uart(name, wire, *, clock, directory, asked=...)` | class, `esp32tl/interfaces/uart.py`, a `BoardLink` `Interface` | `uart.<role>` and its source: attributes `clock`, `directory`, `asked`, `source` and `source_kind` (`None` until given, then the source and `"replay"`); `replay(path)` returns the `Replay`. Without a source it hears everything and says nothing; with one, `receive`, `poll`, `quiet_until` and `latency` are the source's. `asked(name, source)` is called before a source is made, and refuses it by raising |
| `Source` | protocol, `esp32tl/interfaces/uart.py` | `receive(t, data)`, `poll(now)`, `quiet_until()`, `latency()`: what drives a `Uart` |
| `SourceError` | exception, `esp32tl/interfaces/__init__.py` | A source after power-on, a second one on a route, or one no backend gives |
| `BoardOff` | exception, `esp32tl/interfaces/__init__.py` | A wait, an expect, `now` or `utc` before `power_on()`, or a second `power_on()` |
| `RunEnded` | exception, `esp32tl/interfaces/__init__.py` | The run ended while the test waited |
| `NeverHappened` | `AssertionError`, `esp32tl/interfaces/__init__.py` | A condition or a pattern that did not come within its time |
| `SERVICES` | `dict[str, str \| None]`, `esp32tl/backends/__init__.py` | Backend name to the `embedded_services` it runs under, in parametrisation order; `None` is no service, pytest-embedded's base `Dut`. Now `{"esp32sim": None}` |
| `DEFAULT_TIMEOUT` | `int`, `esp32tl/pytest_plugin.py` | 600: the wall-time timeout, in seconds, of a test that uses `board` |
| `timeout_configured(config)` | function | Whether the session sets pytest-timeout's timeout, or pytest-timeout is not loaded |
| `CALL_SAW_RUN_END` | `pytest.StashKey[bool]` | Set on an item whose call failed on `RunEnded` |
| `DID_NOT_PASS` | `pytest.StashKey[bool]` | Set on an item whose setup or call failed, skipped or xfailed |
| `skip_at_the_test(reason)` | function | Raises pytest's skip, reported at the test |
| `backend_failed(error)` | function | Raises the one-line `pytest.fail` for a `BackendError` |

`board` constructs `Esp32Sim` for every parameter and asserts that the parameter is its name: a
second entry in `SERVICES` without a change to `board` fails that assertion.

## Configuration

| Setting | Default | Read by |
|---|---|---|
| `--esp32tl-backend` | unset: every key of `SERVICES` | `pytest_generate_tests` |
| `app_path` marker | the test file's directory | `pytest_generate_tests` |
| `board_profile` marker | `esp32-devkitc` (`DEFAULT_PROFILE`) | `pytest_generate_tests`, `board` |
| `tap` marker | none: the test runs on every backend | `pytest_generate_tests` |
| pytest-timeout's `--timeout`, `PYTEST_TIMEOUT`, `timeout` ini option, `timeout` marker | unset: `DEFAULT_TIMEOUT`, 600 s, for a test that uses `board` | `pytest_collection_modifyitems`, then pytest-timeout |
| pytest-embedded's `embedded_services`, `app_path` | set per test by the parametrisation | pytest-embedded's fixtures |
| pytest-embedded's `--embedded-services`, `--app-path`, `--build-dir`, `--count` | must be unset (`--count` 1) when a selected test uses `board` | `pytest_collection_modifyitems` |

The build directory is not a setting: the backend reads `<app_path>/build` and never consults
pytest-embedded's `build_dir` fixture. The backend's own environment variables are in
[esp32sim-backend-technical.md](esp32sim-backend-technical.md#configuration).

## Failure modes

| Message | When | Outcome |
|---|---|---|
| `{metafunc.definition.nodeid}: the app_path marker takes one path, as in @pytest.mark.app_path("../firmware/hello_world")` | The marker has no argument, more than one, or a keyword argument | Collection error, exit code 2 |
| `{metafunc.definition.nodeid}: {error}` | A `board_profile` or `tap` marker used otherwise than registered, a profile esp32tl does not ship, or a tap no backend can give; the messages are in [board-profiles-user.md](board-profiles-user.md#when-no-backend-can-give-it) | Collection error, exit code 2 |
| `{backend}: {reason}` | The backend cannot give a tap of the `tap` marker | The test is skipped on that backend, before setup |
| `esp32sim: {reason}`, as `esp32sim: uart.gnss takes no replay source on this backend, only model` | The test asks a route for a source the backend's declaration does not give | The test is skipped at the call, reported at the test |
| `--esp32tl-backend: invalid choice: 'NAME'` (argparse's wording around it varies with the Python version) | A name not in `SERVICES` | Usage error, exit code 4 |
| ``ERROR: --{option} is set: esp32tl chooses it for every test that uses `board` (the backend through --esp32tl-backend, the app through the app_path marker, the build in its build/ directory)`` | `--embedded-services`, `--app-path` or `--build-dir` set, and a selected test uses `board` | Usage error, exit code 4, no traceback |
| `ERROR: --count is set: esp32tl runs one board in a test` | `--count` other than 1, and a selected test uses `board` | Usage error, exit code 4 |
| `ERROR: {item.nodeid}: the {marker} marker reached the test after esp32tl read it, from a pytest.param or a hook: put it on the test, its class or its module` | A selected test that uses `board` carries a board marker from a `pytest.param` or a hook | Usage error, exit code 4 |
| `count is above 1: esp32tl runs one board in a test` | The test parametrises `count` above 1 itself, so `dut` is a tuple | Error at setup |
| `esp32sim: {error}` | `Esp32Sim(...)` raised a `BackendError` | Error at setup; messages in [esp32sim-backend-technical.md](esp32sim-backend-technical.md#failure-modes) |
| `esp32sim: the board profile {name} has no uart.gnss; its interfaces are {interfaces}` | The test uses `gnss` on a profile without the route | Error at setup |
| `esp32sim: the test set up the {kind} of {interface} and never called board.power_on(): it ran no firmware` | A test whose setup and call passed, which gave a source and never powered on | Error at teardown |
| `the board is off; call board.power_on() first` | `BoardOff` from a wait, an expect, `now` or `utc` before power-on | Fails the test |
| `the board is on already: power it on once` | `BoardOff` from a second `power_on()` | Fails the test |
| `{interface}: the board is on already: give a route its source before board.power_on()`, `no backend takes a '{source}' source on {interface}; the sources there are {sources}` | `SourceError` from `Uart.replay`, or from the first model call of `Gnss` | Fails the test ([replay-user.md](replay-user.md#when-the-test-gets-it-wrong)) |
| `uart.gnss has its source already: one per test` | `SourceError` from `Uart.replay` on a route with a source, a replay or a model | Fails the test |
| `uart.gnss replays a recording: it has no receiver to describe; one source per test` | `SourceError` from a model call, `position` or `receiver()` of `Gnss` after `gnss.replay` | Fails the test |
| `uart.gnss has no receiver: give it one with gnss.fix(at=...)` | `SourceError` from `Gnss.position` or `receiver()` before any model call | Fails the test |
| `{description} did not happen within {within}: waited from {start} to {end} of the board's time` | `NeverHappened` from `wait_until` or an expect | Fails the test |
| `the run ended {when} while the test waited {what}: {reason}` | `RunEnded` from a wait or an expect; `{when}` is `at {time}` from the runner's `stats`, `after the turn at {time}` without them, the last held turn being only a bound, or `before its first turn`; `{what}` is `until {time}` or `for {description}`, `{reason}` the backend's ([esp32sim-backend-technical.md](esp32sim-backend-technical.md#failure-modes)) | Fails the test |
| `expect: no pattern to wait for`, `expect: {pattern!r} is not a pattern`, `expect_exact takes text or bytes, not a compiled pattern`, `expect: timeout is {timeout!r}, not a Duration or a number of seconds`, `expect: timeout is {timeout}, less than 0` | `ValueError`, `TypeError` from an expect | Fails the test |
| `console.line: {pattern!r} is not a regular expression on text`, `console.lines: since is {since!r}, not a Duration, as seconds(5)`, `{text!r} was found by no pattern, and has no groups` | `TypeError` from `line`, `lines`, or a `Line`'s group | Fails the test |
| `Failed: Timeout (>{timeout}s) from pytest-timeout.` | The test ran out of its wall-time timeout | Fails the test; the teardown ends the runner |
| `esp32sim: {problems}; its output is in {self.dut.logfile}` | `Esp32Sim.close` raised, and the call did not fail on `RunEnded`. `{problems}` is the exit code, the kill or the link's error ([esp32sim-backend-technical.md](esp32sim-backend-technical.md#failure-modes)) | Error at teardown, also after a passing call |

The refusals are raised in `pytest_collection_modifyitems`, not `pytest_generate_tests`: raised
there, a `UsageError` (or `pytest.exit`) is a collection error, with a traceback through pluggy and
exit code 2; here it is one line and exit code 4.

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
  `test_the_board_s_clock_and_sources_go_in_order` (each `BoardOff` and `SourceError`, a model
  after power-on, after a replay and before one),
  `test_a_source_the_backend_cannot_give_skips_at_the_call` (the skip at the test's line, and the
  error for a source no backend gives, with a declaration a conftest swaps in),
  `test_a_model_the_backend_cannot_give_skips_at_the_call` and
  `test_a_test_that_sets_up_a_source_and_never_powers_on_fails_at_teardown` (a replay and a
  model; only the tests that passed are reported, and no runner starts).
- Against a fake runner that speaks the link protocol (`LINK_RUNNER`):
  `test_the_backend_serves_the_link_and_ends_the_run_with_stop` (the `esp32sim link` section
  with `-rA`, and the `esp32sim_link` property read back from `--junitxml` as JSON),
  `test_every_route_of_the_profile_is_on_the_link`, `test_the_test_s_waits_move_the_board_s_clock`,
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

The markers of the board profiles are tested in `tests/test_pytest_plugin.py` too:
`test_a_backend_that_cannot_give_a_tap_skips_with_the_reason`,
`test_a_marker_no_backend_can_satisfy_is_a_collection_error`,
`test_markers_on_a_class_or_a_module_count`, `test_a_board_marker_in_a_pytest_param_is_refused`,
`test_a_board_marker_a_hook_adds_is_refused` and
`test_the_board_runs_the_profile_the_test_chose_and_starts_no_runner_unpowered`
([board-profiles-technical.md](board-profiles-technical.md#tests)).

`tests/test_gnss_fixture.py` runs `gnss.replay` into `nmea_reader` on the real runner, and,
through `pytester`, a source after power-on, an expect before it, a source never powered on and a
profile without the route ([replay-technical.md](replay-technical.md#tests)).
`tests/test_replay.py` tests `Uart` without an emulator.

`tests/test_nmea_reader_model.py` runs the receiver model into `nmea_reader` on the real runner,
with `console.line` and `console.lines` ([models-technical.md](models-technical.md#tests)).

`tests/test_package.py` checks the `pytest11` entry point (`test_plugin_is_registered_through_pytest11`)
and that pytest loaded the module under the name `esp32tl` (`test_pytest_loaded_the_plugin`).
`tests/test_hello_world.py` and `tests/test_nmea_reader.py` are the end-to-end cases on the real
runner and images.

Not covered: deselection by `-m` (only `-k` is tested), a marker on a class, an absolute marker
path, a marker with keyword arguments (only the bare marker is tested), limiting the run to one of
several backends, since `SERVICES` has one, and `power_on(utc=...)` with a timezone other than
UTC.

## See also

- [pytest plugin](pytest-plugin-user.md)
- [esp32sim backend: how it works](esp32sim-backend-technical.md)
- [Plan section 3, architecture](../implementation-plan.md#3-architecture)
- [Invariants](../invariants.md)
