# pytest plugin: how it works

The plugin is `esp32tl/pytest_plugin.py`, with the backend registry in `esp32tl/backends/__init__.py`
and the console in `esp32tl/interfaces/console.py`. What the user sees is in
[pytest-plugin-user.md](pytest-plugin-user.md); the backend it starts is in
[esp32sim-backend-technical.md](esp32sim-backend-technical.md).

## Data flow

1. **Loading.** `pyproject.toml` registers the module under the `pytest11` entry point,
   `esp32tl = "esp32tl.pytest_plugin"`, so pytest loads it next to pytest-embedded's own plugin.
   esp32tl has a plugin of its own rather than a pytest-embedded service because pytest-embedded
   resolves `--embedded-services` through a closed dictionary and cannot load a third-party
   service ([plan section 1](../implementation-plan.md#1-findings-that-amend-the-specification),
   finding 1).
2. **`pytest_addoption`** adds the `esp32tl` group with `--esp32tl-backend`, whose choices are
   `list(SERVICES)`. **`pytest_configure`** registers the `app_path` marker, which
   `--strict-markers` would otherwise refuse.
3. **`pytest_generate_tests`** returns at once unless `board` is in `metafunc.fixturenames`, which
   holds the whole fixture closure, so a test that asks only for `console` counts. It starts from
   `metafunc.definition.path.parent`, joins the single argument of the closest `app_path` marker to
   it (an absolute argument replaces it, as `pathlib` joins) and resolves the result. A marker with
   other than one positional argument, or with any keyword argument, is `pytest.fail(...,
   pytrace=False)`. The backends are `[chosen]` when `--esp32tl-backend` is set, else
   `list(SERVICES)`. Then one call:

   ```python
   metafunc.parametrize(
       ("board", "embedded_services", "app_path"),
       [(backend, SERVICES[backend], str(app_path.resolve())) for backend in backends],
       indirect=True,
       ids=backends,
   )
   ```

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
5. **`board` fixture**, at setup: asserts `request.param == Esp32Sim.name`, fails the test if
   `dut` is not a single `Dut` (pytest-embedded makes a tuple of duts when `count` is parametrised
   above 1), and constructs `Esp32Sim(dut, app_path, msg_queue, _meta)`. A `BackendError` goes
   through `backend_failed`, which raises `pytest.fail.Exception(f"{Esp32Sim.name}: {error}",
   pytrace=False) from None`: one line, no chained traceback.
6. **`pytest_runtest_makereport`**, a `wrapper=True` hook, looks at the report of the call phase.
   When the call raised and `ended_on_runner_exit(call.excinfo.value)` holds, that is the error's
   `__cause__`/`__context__` chain holds a `RunnerExited`, it sets
   `item.stash[EXPECT_SAW_RUNNER_EXIT] = True`. The call's report is made before the fixtures are
   torn down, so the stash is set by then.
7. **`board` fixture**, at teardown: `backend.close()`. A `BackendError` from it goes through
   `backend_failed` unless `request.node.stash.get(EXPECT_SAW_RUNNER_EXIT, False)`: an expect that
   ended on the runner's exit has already reported the exit code, and the test would otherwise
   show it twice.
8. **`console` fixture** returns `Console(board.dut)`. `Console.__init__` binds `expect` and
   `expect_exact` to the dut's own methods, so their arguments, return values and 30 s default
   timeout (pexpect's `timeout=30`) are pytest-embedded's.

## Interfaces

| Name | Kind | What |
|---|---|---|
| `--esp32tl-backend NAME` | option | Choices `list(SERVICES)`; default unset, every backend |
| `app_path(path)` | marker | One positional path, relative to the test file's directory; default that directory |
| `board(request, dut, app_path, msg_queue, _meta)` | fixture, function scope | Yields an `Esp32Sim`; closes it at teardown |
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
| pytest-embedded's `embedded_services`, `app_path` | set per test by the parametrisation | pytest-embedded's fixtures |
| pytest-embedded's `--embedded-services`, `--app-path`, `--build-dir`, `--count` | must be unset (`--count` 1) when a selected test uses `board` | `pytest_collection_modifyitems` |

The build directory is not a setting: the backend reads `<app_path>/build` and never consults
pytest-embedded's `build_dir` fixture. The backend's own environment variables are in
[esp32sim-backend-technical.md](esp32sim-backend-technical.md#configuration).

## Failure modes

| Message | When | Outcome |
|---|---|---|
| `{metafunc.definition.nodeid}: the app_path marker takes one path, as in @pytest.mark.app_path("../firmware/hello_world")` | The marker has no argument, more than one, or a keyword argument | Collection error, exit code 2 |
| `--esp32tl-backend: invalid choice: 'NAME'` (argparse's wording around it varies with the Python version) | A name not in `SERVICES` | Usage error, exit code 4 |
| ``ERROR: --{option} is set: esp32tl chooses it for every test that uses `board` (the backend through --esp32tl-backend, the app through the app_path marker, the build in its build/ directory)`` | `--embedded-services`, `--app-path` or `--build-dir` set, and a selected test uses `board` | Usage error, exit code 4, no traceback |
| `ERROR: --count is set: esp32tl runs one board in a test` | `--count` other than 1, and a selected test uses `board` | Usage error, exit code 4 |
| `count is above 1: esp32tl runs one board in a test` | The test parametrises `count` above 1 itself, so `dut` is a tuple | Error at setup |
| `esp32sim: {error}` | `Esp32Sim(...)` raised a `BackendError` | Error at setup; messages in [esp32sim-backend-technical.md](esp32sim-backend-technical.md#failure-modes) |
| `esp32sim: the runner exited with code {code}; its output is in {self.dut.logfile}` | `Esp32Sim.close` raised, and no expect of the call reported the runner's exit | Error at teardown, also after a passing call |

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
- `test_backend_option_limits_the_parametrisation`: the id with `--esp32tl-backend esp32sim`, and
  the usage error for `qemu`.
- `test_pytest_embedded_options_are_refused_for_tests_with_a_board`: exit code 4, the message, no
  traceback. `test_more_than_one_dut_is_refused`: exit code 4 and the message.
- `test_options_are_allowed_when_no_selected_test_uses_board`: `-k` deselection before the
  refusal.
- `test_a_parametrised_count_above_one_is_refused`, `test_app_path_marker_needs_one_path`.
- `test_a_runner_that_fails_to_start_is_reported_once`: the stash keeps the teardown quiet after
  an expect reported the exit. `test_an_eof_from_elsewhere_does_not_hide_the_runner_s_exit`: an
  `EOF` that is not the runner's does not set it.

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
