# pytest plugin: how it works

The plugin is `esp32tl/pytest_plugin.py`, with the backend registry in
`esp32tl/backends/__init__.py`, the console in `esp32tl/interfaces/console.py`, and the board
profiles and capability declaration in `esp32tl/boards/`
([board-profiles-technical.md](board-profiles-technical.md)). What the user
sees is in [pytest-plugin-user.md](pytest-plugin-user.md); the backend it starts is in
[esp32sim-backend-technical.md](esp32sim-backend-technical.md).

## Data flow

1. **Loading.** `pyproject.toml` registers the module under the `pytest11` entry point,
   `esp32tl = "esp32tl.pytest_plugin"`, so pytest loads it next to pytest-embedded's own plugin.
   esp32tl has a plugin of its own rather than a pytest-embedded service because pytest-embedded
   resolves `--embedded-services` through a closed dictionary and cannot load a third-party
   service ([plan section 1](../implementation-plan.md#1-findings-that-amend-the-specification),
   finding 1).
2. **`pytest_addoption`** adds the `esp32tl` group with `--esp32tl-backend`, whose choices are
   `list(SERVICES)`. **`pytest_configure`** registers the `app_path`, `board_profile`, `source` and
   `tap` markers, which `--strict-markers` would otherwise refuse.
3. **`pytest_generate_tests`** returns at once unless `board` is in `metafunc.fixturenames`, which
   holds the whole fixture closure, so a test that asks only for `console` counts. It starts from
   `metafunc.definition.path.parent`, joins the single argument of the closest `app_path` marker to
   it (an absolute argument replaces it, as `pathlib` joins) and resolves the result. A marker with
   other than one positional argument, or with any keyword argument, is `pytest.fail(...,
   pytrace=False)`. It then loads the board profile and the test's requests from the
   `board_profile`, `source` and `tap` markers and checks them
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
   (`app_path`, `board_profile`, `source`, `tap`) say something else than its definition's did:
   `late_board_marker` reads them from the item with `board_profile` and `board_requests` and
   compares them, one marker at a time, with the `BoardMarkers` `pytest_generate_tests` kept in
   `config.stash[READ]`, by the definition's nodeid (the item's
   without `[{callspec.id}]`). A `pytest.param`'s marks, and those a hook of another plugin or a
   conftest adds (this one runs `trylast`), reach the item but not `metafunc.definition`, so
   they would go unchecked: a `source` there would never skip its backend, and a
   `board_profile` there would reach `board` but not the check. A marker a fixture adds at setup
   is not seen.
5. **`board` fixture**, at setup: asserts `request.param == Esp32Sim.name`, makes the test an error
   if `dut` is not a single `Dut` (pytest-embedded makes a tuple of duts when `count` is
   parametrised above 1), and constructs
   `Esp32Sim(dut, app_path, msg_queue, _meta, board_profile(request.node))`. A
   `BackendError` goes through `backend_failed`, which raises
   `pytest.fail.Exception(f"{Esp32Sim.name}: {error}", pytrace=False) from None`: one line, no
   chained traceback.
6. **`pytest_runtest_makereport`**, a `wrapper=True` hook, looks at the report of the call phase.
   When the call raised and `ended_on_runner_exit(call.excinfo.value)` holds, that is the error's
   `__cause__`/`__context__` chain holds a `RunnerExited`, it sets
   `item.stash[EXPECT_SAW_RUNNER_EXIT] = True`. The call's report is made before the fixtures are
   torn down, so the stash is set by then.
7. **`board` fixture**, at teardown: `backend.close()`. A `BackendError` from it goes through
   `backend_failed` unless `request.node.stash.get(EXPECT_SAW_RUNNER_EXIT, False)`: an expect that
   ended on the runner's exit has already reported the exit code and the link's error, and the
   test would otherwise show them twice. Then, whatever `close` did, it adds the report section
   `("teardown", "esp32sim link", backend.link.report())` with `request.node.add_report_section`,
   and appends `("esp32sim_link", json.dumps(backend.link.properties()))` to
   `request.node.user_properties`. The property is a JSON string because JUnit XML writes a
   property's `str()`, which for a `dict` is Python's `repr`.
8. **`console` fixture** returns `Console(board.dut)`. `Console.__init__` binds `expect` and
   `expect_exact` to the dut's own methods, so their arguments, return values and 30 s default
   timeout (pexpect's `timeout=30`) are pytest-embedded's.

## Interfaces

| Name | Kind | What |
|---|---|---|
| `--esp32tl-backend NAME` | option | Choices `list(SERVICES)`; default unset, every backend |
| `app_path(path)` | marker | One positional path, relative to the test file's directory; default that directory |
| `board_profile(name)`, `source(interface, source)`, `tap(*interfaces)` | markers | The board profile and the test's requests ([board-profiles-technical.md](board-profiles-technical.md#interfaces)) |
| `board(request, dut, app_path, msg_queue, _meta)` | fixture, function scope | Yields an `Esp32Sim`; closes it at teardown, then adds the `esp32sim link` report section and the `esp32sim_link` user property |
| `console(board)` | fixture, function scope | Returns `Console(board.dut)` |
| `Console` | class, `esp32tl/interfaces/console.py` | Attributes `dut`, `expect`, `expect_exact`; no `write` |
| `SERVICES` | `dict[str, str \| None]`, `esp32tl/backends/__init__.py` | Backend name to the `embedded_services` it runs under, in parametrisation order; `None` is no service, pytest-embedded's base `Dut`. Now `{"esp32sim": None}` |
| `EXPECT_SAW_RUNNER_EXIT` | `pytest.StashKey[bool]` | Set on an item whose call failed on an expect that read the runner's end |
| `backend_failed(error)` | function | Raises the one-line `pytest.fail` for a `BackendError` |

`board` constructs `Esp32Sim` for every parameter and asserts that the parameter is its name: a
second entry in `SERVICES` without a change to `board` fails that assertion.

## Configuration

| Setting | Default | Read by |
|---|---|---|
| `--esp32tl-backend` | unset: every key of `SERVICES` | `pytest_generate_tests` |
| `app_path` marker | the test file's directory | `pytest_generate_tests` |
| `board_profile` marker | `esp32-devkitc` (`DEFAULT_PROFILE`) | `pytest_generate_tests`, `board` |
| `source`, `tap` markers | none: the test runs on every backend | `pytest_generate_tests` |
| pytest-embedded's `embedded_services`, `app_path` | set per test by the parametrisation | pytest-embedded's fixtures |
| pytest-embedded's `--embedded-services`, `--app-path`, `--build-dir`, `--count` | must be unset (`--count` 1) when a selected test uses `board` | `pytest_collection_modifyitems` |

The build directory is not a setting: the backend reads `<app_path>/build` and never consults
pytest-embedded's `build_dir` fixture. The backend's own environment variables are in
[esp32sim-backend-technical.md](esp32sim-backend-technical.md#configuration).

## Failure modes

| Message | When | Outcome |
|---|---|---|
| `{metafunc.definition.nodeid}: the app_path marker takes one path, as in @pytest.mark.app_path("../firmware/hello_world")` | The marker has no argument, more than one, or a keyword argument | Collection error, exit code 2 |
| `{metafunc.definition.nodeid}: {error}` | A `board_profile`, `source` or `tap` marker used otherwise than registered, a profile esp32tl does not ship, or a request no backend can give; the messages are in [board-profiles-user.md](board-profiles-user.md#when-no-backend-can-give-it) | Collection error, exit code 2 |
| `{backend}: {reason}` | The backend cannot give a request of the `source` or `tap` markers | The test is skipped on that backend, before setup |
| `--esp32tl-backend: invalid choice: 'NAME'` (argparse's wording around it varies with the Python version) | A name not in `SERVICES` | Usage error, exit code 4 |
| ``ERROR: --{option} is set: esp32tl chooses it for every test that uses `board` (the backend through --esp32tl-backend, the app through the app_path marker, the build in its build/ directory)`` | `--embedded-services`, `--app-path` or `--build-dir` set, and a selected test uses `board` | Usage error, exit code 4, no traceback |
| `ERROR: --count is set: esp32tl runs one board in a test` | `--count` other than 1, and a selected test uses `board` | Usage error, exit code 4 |
| `ERROR: {item.nodeid}: the {marker} marker reached the test after esp32tl read it, from a pytest.param or a hook: put it on the test, its class or its module` | A selected test that uses `board` carries a board marker from a `pytest.param` or a hook; `a source or tap marker` in place of `the {marker} marker` when the item's `source` markers clash, as when a hook adds one to the module once per item | Usage error, exit code 4 |
| `count is above 1: esp32tl runs one board in a test` | The test parametrises `count` above 1 itself, so `dut` is a tuple | Error at setup |
| `esp32sim: {error}` | `Esp32Sim(...)` raised a `BackendError` | Error at setup; messages in [esp32sim-backend-technical.md](esp32sim-backend-technical.md#failure-modes) |
| `esp32sim: {problems}; its output is in {self.dut.logfile}` | `Esp32Sim.close` raised, and no expect of the call reported the runner's exit. `{problems}` is the exit code, the kill or the link's error ([esp32sim-backend-technical.md](esp32sim-backend-technical.md#failure-modes)) | Error at teardown, also after a passing call |

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
- `test_a_runner_that_fails_to_start_is_reported_once`: the stash keeps the teardown quiet after
  an expect reported the exit. `test_an_eof_from_elsewhere_does_not_hide_the_runner_s_exit`: an
  `EOF` that is not the runner's does not set it.
- `test_the_backend_serves_the_link_and_ends_the_run_with_sigterm`: the `esp32sim link` section
  with `-rA`, and the `esp32sim_link` property read back from `--junitxml` as JSON.

The markers of the board profiles are tested in the same file:
`test_a_backend_that_cannot_give_a_source_or_tap_skips_with_the_reason`,
`test_a_marker_no_backend_can_satisfy_is_a_collection_error`,
`test_markers_on_a_class_or_a_module_count`, `test_the_closest_source_marker_wins`,
`test_a_board_marker_in_a_pytest_param_is_refused`,
`test_a_board_marker_a_hook_adds_is_refused`, `test_a_subclass_s_source_overrides_its_base_s` and
`test_the_board_runs_the_profile_the_test_chose`
([board-profiles-technical.md](board-profiles-technical.md#tests)).

`tests/test_package.py` checks the `pytest11` entry point (`test_plugin_is_registered_through_pytest11`)
and that pytest loaded the module under the name `esp32tl` (`test_pytest_loaded_the_plugin`).
`tests/test_hello_world.py` is the end-to-end case on the real runner and image.

Not covered: deselection by `-m` (only `-k` is tested), a marker on a class, an absolute marker
path, a marker with keyword arguments (only the bare marker is tested), and limiting the run to
one of several backends, since `SERVICES` has one.

## See also

- [pytest plugin](pytest-plugin-user.md)
- [esp32sim backend: how it works](esp32sim-backend-technical.md)
- [Plan section 3, architecture](../implementation-plan.md#3-architecture)
- [Invariants](../invariants.md)
