# Harness decisions

Why the `esp32tl` package, its pytest plugin, the `esp32sim` backend, the test suite, and the
venv and pre-commit hooks around them are the way they are.

## The ruff and mypy hooks run from the worktree's .venv

Status: decided (#42)

The ruff and mypy pre-commit hooks are `local` hooks that run the tools through
`scripts/venv-tool`, from the `.venv` of the worktree being committed from, so their versions and
mypy's view of the dependencies come from `pyproject.toml` only. Git hooks are shared by every
worktree of a clone, so `scripts/venv-tool` says `run make setup in this worktree` when that
worktree has no `.venv`. The mypy hook runs on every commit over the files the configuration
lists, so the hook and `make lint` agree and a commit that changes only the configuration or the
dependencies is checked.

**Rejected:** hooks in environments that pre-commit manages, because their tool versions and the
packages mypy sees would be a second list, apart from `pyproject.toml`.

## make setup installs no git hooks

Status: decided (#42)

`pre-commit install` lives in its own target, `make hooks`, because CI wants no git hooks: its
jobs run `make lint` and `make .venv/.installed rom-elf`, the parts of `make setup` they need.

## Clippy is not a pre-commit hook

Status: superseded (#57)

Clippy ran in `make lint` and in CI's `lint` job only, because the hooks configuration took it to
build esp32sim, too slow for a hook. It does not build esp32sim: see
[Clippy runs as a pre-commit hook on the runner's files](#clippy-runs-as-a-pre-commit-hook-on-the-runners-files).

## Clippy runs as a pre-commit hook on the runner's files

Status: decided (#57)

The `cargo-clippy` hook runs `cargo clippy --locked --all-targets -- -D warnings` in `runner/`,
as `make lint` does, on commits that stage a `.rs` file under `runner/`, `runner/Cargo.toml`,
`runner/Cargo.lock` or `runner/rust-toolchain.toml`: a bump of the esp32sim pin or of the
toolchain is linted before it is committed, not only in CI. Clippy compiles esp32sim's crates to
metadata only (`--emit=dep-info,metadata`, plain `rustc`, `--cap-lints allow`): the API the
runner is checked against, with no code generation and no lints of esp32sim's own. Measured on an
M2 Pro: 0.16 to 0.17 s after a change to the runner, 2.3 to 3.2 s the first time in a worktree,
whose `runner/target/` has no metadata yet. A commit that changes no pin needs no network:
`make setup`, which the hooks need anyway, has fetched esp32sim into `~/.cargo/git` before. A
commit that bumps the esp32sim revision or the toolchain channel fetches the new one in the hook,
once, as `make setup` would, and compiles it cold. `make lint` and CI's `lint`
job still run clippy, so a commit made without the hooks is caught there.

**Rejected:** keeping clippy out of the hooks, because a lint failure then shows up only in CI,
after the push and the queue on hp600g1, for a saving of about 0.2 s per Rust commit.

## The board fixture is parametrised over board, embedded_services and app_path together

Status: decided (#51)

`pytest_generate_tests` parametrises every test that uses `board` in a single
`metafunc.parametrize` over `board`, `embedded_services` and `app_path`, `indirect=True`, with the
backend name as the shared id (`test_hello_world[esp32sim]`). `board` itself is one of the
indirect parameters, and the fixture reads the backend name from `request.param`.
`--esp32tl-backend NAME` limits the run to one backend.

**Rejected:** mapping the backend back from the `embedded_services` value, because two backends
could share a services value.

## The app comes from the app_path marker

Status: decided (#51)

`@pytest.mark.app_path(path)` names the app, relative to the test file's directory; without the
marker the app is that directory, pytest-embedded's default. A marker without exactly one path
is a collection error whose one-line message names the test.

**Rejected:** letting tests parametrise `app_path` themselves, because that would collide with the
plugin's own parametrisation of the same name.

## pytest-embedded's app and service options are refused for board tests

Status: decided (#51)

When a selected test uses `board`, `--embedded-services`, `--app-path` and `--build-dir` are
refused with a `pytest.UsageError`, and so is `--count` above 1; the `board` fixture also refuses a
`count` that a test parametrises above 1. pytest-embedded replaces an empty `embedded_services`
parameter with the command-line value, so `--embedded-services esp` would make the `esp32sim` case
open a serial port. The check is `pytest_collection_modifyitems` with `trylast=True`, after `-k`
and `-m` have deselected tests, so a session with no selected `board` test keeps the options, and
pytest reports the error as one line with exit code 4.

**Rejected:** ignoring the options, because that would let them change what the `esp32sim` case
runs without a sign. Also rejected: raising from `pytest_generate_tests`, because there both
`UsageError` and `pytest.exit` become collection errors with a pluggy traceback and exit code 2.

## The end of the run is a line appended to the runner's log

Status: decided (#51)

A thread in `Esp32Sim` waits for the runner and appends `esp32tl: the runner exited with code N`
to its log, behind everything the runner wrote. `end_stream_at_end_of_run` replaces
`read_nonblocking` on the dut's `pexpect_proc`, which then raises `EOF` once it has read that line,
so an expect that has not matched fails at once instead of waiting out its 30 s timeout. pexpect
empties its buffer at `EOF`, so pytest-embedded's "Not found" message would show nothing: the
`EOF` carries the exit code and up to 2 KiB of the runner's last output (`EOF_TAIL_BYTES`),
starting at a whole line and without colour codes. This relies on pexpect 4.9.0 and pytest-embedded
2.9.3 internals (`DuplicateStdoutPopen._logfile`, how pexpect re-raises `EOF`); only
pytest-embedded is pinned (`~=2.9.3`), and the fake-runner tests in `tests/test_pytest_plugin.py`
fail if either changes.

**Rejected:** closing the pexpect file when the runner exits, because output still in transit
through the log forwarder, the message queue and the listener would be dropped. Also rejected:
wrapping `console.expect` in short polling expects, because that breaks `expect_all`.

## Teardown recognises the end of the run by RunnerExited in the exception chain

Status: decided (#51)

The end-of-run `EOF` is a `RunnerExited`, a subclass of `pexpect.EOF`. pexpect raises its own
`EOF` from it, and pytest-embedded its "Not found" error from that, so `ended_on_runner_exit` walks
`__cause__` and `__context__` (stopping at a cycle) to find it. The `board` teardown skips its own
error only when the call failed that way, so the runner's exit is reported once.

**Rejected:** staying quiet after any `EOF`, because an `EOF` from a host tool the test drives
itself could then hide a runner that crashed in the same test.

## Teardown passes only the signals it sent itself

Status: decided (#51)

`Esp32Sim.close` sends `SIGTERM`, waits 5 s, then sends `SIGKILL`, and fails the test on any exit
code other than 0 and the signals it sent, even if every expect passed. It judges the code after
the runner has stopped, so a runner that crashed just before the signal keeps its own code. The
runner handles no signal, so `-SIGTERM` and `-SIGKILL` always mean a signal. Teardown signals the
runner before closing the log forwarder.

**Rejected:** passing any exit by `SIGTERM` or `SIGKILL`, because a runner killed from outside
during a test, by the OOM killer for one, would go unreported once its expects had passed.

## The build directory is passed to IdfApp as an absolute path

Status: decided (#51)

`load_app` builds `IdfApp(app_path, build_dir=str(Path(app_path) / "build"))`.

**Rejected:** a relative `build`, because pytest-embedded then falls back to `./build` in the
working directory when the app has none; in this repository that is CI's `build/ci`.

## The runner and ROM paths default to the checkout, with environment overrides

Status: decided (#51)

The backend runs `runner/target/release/esp32tl-runner` and passes
`--rom .cache/esp-rom-elfs/20241011/esp32_rev300_rom.elf`, both found from the editable install
`make setup` makes. `ESP32TL_RUNNER` and `ESP32TL_ROM` name others, for example a
`RUNNER_PROFILE=dev` build. The backend always passes `--rom`, because the runner's own lookup
prefers the newest release under `~/.espressif`
([runner.md](runner.md#the-runner-looks-for-the-rom-the-way-esp32sim-does)).
`test_rom_is_the_one_make_setup_fetches` checks that `ROM_RELEASE` and `ROM_ELF` match
`scripts/fetch-rom-elf`.

**Rejected:** new command-line options for the two paths, so that `--esp32tl-backend` stays the
plugin's only flag.

## A run lasts at most 300 emulated seconds

Status: decided (#51)

The backend starts the runner with `--max-seconds 300` (`MAX_SECONDS`). A test normally ends the
run earlier, at teardown; the limit also stops a runner left behind by a harness that died. On an
M2 Pro, `hello_world` runs 12 emulated seconds in 0.3 s of wall time, and 300 in 5.9 s.

## The negative hello_world test edits the app image in place

Status: decided (#51)

`test_hello_world_fails_on_an_image_that_prints_something_else` copies the build, swaps
`Hello world!` for `Hello WORLD!` in the app image, updates the XOR checksum with the difference,
recomputes the appended SHA-256 and has esptool's loader verify both. It then runs the real
`tests/test_hello_world.py` through `pytester` and requires one failure and `Hello WORLD!` in the
output, which shows the edited image booted.

**Rejected:** esptool's `save()`, because it lays the segments out again and raised
`AttributeError` on this image with esptool 5.6.0.

## Console.write is postponed until the runner reads input

Status: postponed (#51)

`Console` has `expect` and `expect_exact`, the dut's own, and no `write`: the runner does not read
its stdin.

## The serial backend is postponed to M4

Status: postponed (#25)

`esp32tl.backends.SERVICES` has the `esp32sim` entry only, so the parametrisation has a single
case. `serial` (`esp,idf`) is added with M4.

## Prebuilt runner binaries are postponed to M10

Status: postponed (#40)

The backend finds the runner in the checkout, so from a wheel only `ESP32TL_RUNNER` and
`ESP32TL_ROM` find the runner and the ROM.
