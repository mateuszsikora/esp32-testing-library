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

The `cargo-clippy` hook runs `cargo clippy --locked --all-targets -- -D warnings` in `runner/`, as
`make lint` does, on commits that stage a `.rs` file under `runner/`, `runner/Cargo.toml`,
`runner/Cargo.lock` or `runner/rust-toolchain.toml`: a bump of the esp32sim pin or of the toolchain
is linted before it is committed, not only in CI. Clippy compiles esp32sim's crates to metadata only
(`--emit=dep-info,metadata`, plain `rustc`, `--cap-lints allow`): the API the runner is checked
against, with no code generation and no lints of esp32sim's own. Measured on an M2 Pro: 0.16 to
0.17 s after a change to the runner, 2.3 to 3.2 s the first time in a worktree, whose
`runner/target/` has no metadata yet. A commit that changes no pin needs no network: `make setup`,
which the hooks need anyway, has fetched esp32sim into `~/.cargo/git` before. A commit that bumps
the esp32sim revision or the toolchain channel fetches the new one in the hook, once, as
`make setup` would, and compiles it cold. `make lint` and CI's `lint` job still run clippy, so a
commit made without the hooks is caught there.

**Rejected:** keeping clippy out of the hooks, because a lint failure then shows up only in CI,
after the push and the queue on hp600g1, for a saving of about 0.2 s per Rust commit.

## The board fixture is parametrised over board, embedded_services and app_path together

Status: decided (#51)

*Extended in #102 by [Explicit build paths are relative to the application and never fall
back](#explicit-build-paths-are-relative-to-the-application-and-never-fall-back).*

`pytest_generate_tests` parametrises every test that uses `board` in a single
`metafunc.parametrize` over `board`, `embedded_services` and `app_path`, `indirect=True`, with the
backend name as the shared id (`test_hello_world[esp32sim]`). `board` itself is one of the
indirect parameters, and the fixture reads the backend name from `request.param`.
`--esp32tl-backend NAME` limits the run to one backend.

**Rejected:** mapping the backend back from the `embedded_services` value, because two backends
could share a services value.

## The app comes from the app_path marker

Status: decided (#51)

*Extended in #102 by [Explicit build paths are relative to the application and never fall
back](#explicit-build-paths-are-relative-to-the-application-and-never-fall-back).*
*Extended in #237 by [Project defaults are esp32tl ini options that the markers
override](#project-defaults-are-esp32tl-ini-options-that-the-markers-override).*

`@pytest.mark.app_path(path)` names the app, relative to the test file's directory; without the
marker the app is that directory, pytest-embedded's default. A marker without exactly one path
is a collection error whose one-line message names the test.

**Rejected:** letting tests parametrise `app_path` themselves, because that would collide with the
plugin's own parametrisation of the same name.

## pytest-embedded's app and service options are refused for board tests

Status: decided (#51)

*Extended in #102 by [Explicit build paths are relative to the application and never fall
back](#explicit-build-paths-are-relative-to-the-application-and-never-fall-back).*

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

Status: superseded (#65)

A thread in `Esp32Sim` appended `esp32tl: the runner exited with code N` to the runner's log, and
`end_stream_at_end_of_run` made the dut's `pexpect_proc` raise `EOF` once it had read that line,
with the exit code and the runner's last output, so that an expect that had not matched failed at
once rather than after its 30 s. The line is still appended, for whoever reads `dut.log`, but the
console no longer reads the pexpect stream: see [a run that ends while the test waits raises RunEnded](#a-run-that-ends-while-the-test-waits-raises-runended-with-the-runners-last-output).

## Teardown recognises the end of the run by RunnerExited in the exception chain

Status: superseded (#65)

The end-of-run `EOF` was a `RunnerExited`, a subclass of `pexpect.EOF`, which
`ended_on_runner_exit` found in the call's exception chain, so that the `board` teardown reported
the runner's exit once. With the console on the test's clock the end of the run is `RunEnded`: see
[a run that ends while the test waits raises RunEnded](#a-run-that-ends-while-the-test-waits-raises-runended-with-the-runners-last-output).

## Teardown passes only the signals it sent itself

Status: superseded (#12)

`Esp32Sim.close` sent `SIGTERM`, waited 5 s, then sent `SIGKILL`, and passed exit code 0 and the
signals it had sent, because the runner, started without `--board`, handled no signal. Since the
backend starts it with `--board`, see
[Teardown waits for the runner's hello, sends one SIGTERM and passes exit code 0 only](#teardown-waits-for-the-runners-hello-sends-one-sigterm-and-passes-exit-code-0-only).

## The runner's log reaches dut.log through a forwarder of esp32tl's that waits when it is idle

Status: decided

`RunnerPopen` replaces pytest-embedded's `_PopenRedirectProcess` (pytest-embedded 2.9.3,
`pytest_embedded/log.py`) with `LogForwarder`, which sleeps `LOG_POLL_SECONDS`, 20 ms, when the
runner's log has nothing new. The original reads the file again at once, and every empty read is a
call to the manager process that holds `msg_queue`, so a test ran two busy processes beside the
runner. Measured on an M2 Pro over 300 emulated seconds of an awake image: 56 s of CPU in the two
against 88 s in the runner; with `LogForwarder`, 0.06 s. Wherever there are fewer free CPUs than
busy processes, as under `pytest -n`, the two take CPU from the runner; on CI this was not
measured.

The runner's lines reach the queue up to 20 ms after they are written. The test does not wait for
them: the console reaches it through the link, not through the log. Teardown does: after `SIGTERM`
the forwarder finishes its sleep, reads the file once more and ends, and `RunnerPopen.close` waits
for it, so the
runner's last lines are on the queue before the backend's teardown goes on, as they nearly always
were with the busy loop. pytest-embedded's listener, stopped right after the dut, can still miss
them, as it could before. The forwarder costs about 0.3 s of CPU at each `power_on`, to import the
`esp32tl` package it lives in; a `SIGTERM` in that time would end it before its handler is in place
and without the last read, so it sets a `ready` event once the handler is there, and
`RunnerPopen.close` waits up to `EXIT_SECONDS` for that before the signal.

**Rejected:** a `SIGTERM` handler that wakes the sleep through a `threading.Event`, because
`Event.set` takes the event's lock, which the interrupted `Event.wait` may hold: the forwarder would
then wait for ever, and the interpreter's exit, which joins daemon processes without a timeout, with
it. A wakeup pipe (`signal.set_wakeup_fd`) would end the sleep at once without a lock, but would save
at most 20 ms of teardown for the code it takes. Also rejected: waiting for a fix in pytest-embedded,
which esp32tl pins at `~=2.9.3` and which every test pays for until then; and a
`DuplicateStdoutPopen` without a forwarder (`REDIRECT_CLS = None`), because `dut.log` would then
lose the runner's own lines.

## Teardown waits for the runner's hello, sends one SIGTERM and passes exit code 0 only

Status: decided (#12)

*Since #65 the `SIGTERM` is for a runner the test's clock does not hold in a turn: [teardown stops
the run at the turn the test left it at](#teardown-stops-the-run-at-the-turn-the-test-left-it-at)
comes first.*

`Esp32Sim.close` waits up to 5 s for the runner's `hello`, sends `SIGTERM` once, reads the link
to end of file, and sends `SIGKILL` only when the runner has not exited 5 s after the signal. The
runner registers its signal handlers after it connects and before its `hello`; before that a
`SIGTERM` kills it, which made every test that ended while the runner loaded its images fail on
`-15`. A second `SIGTERM` kills it with 143, so the signal that `BoardLink`'s error sends and the
teardown's are one. With `--board` the runner exits 0 on `SIGTERM`, so only 0 passes, and a
killed runner fails the test even when its expects passed. It judges the code after the runner
has stopped, so a runner that crashed just before the signal keeps its own code. Teardown signals
the runner before closing the log forwarder.

**Rejected:** passing `-SIGTERM` when the signal came before the `hello`, because that run has no
`stats` and its code cannot be told from a kill from outside. Also rejected: a fixed delay before
the signal, because loading the images takes as long as it takes. Also rejected, as in #51:
passing any exit by a signal, because a runner killed from outside during a test, by the OOM
killer for one, would go unreported once its expects had passed.

## A link error ends the run with SIGTERM at once

Status: decided (#12)

When `BoardLink` finds the run wrong, it calls `on_error`, and the backend sends the runner
`SIGTERM` then and there; the runner sends its `stats` and exits 0, and an expect that is waiting
fails on the end of the run with the link's reason first. A mismatched handshake is the exception:
the runner finds it in the harness's `hello` and exits 3 by itself.

**Rejected:** waiting for the runner's next turn to send `stop`, because with `quiet_until` and
`latency` both `null` that turn may come only at `--max-seconds`, 300 emulated seconds, and the
expect would time out after 30 s with no word of the cause. Since #65 the next turn comes at the
end of the test's wait, or at `--max-seconds 86400`, and the waiting test gets `RunEnded` with the
link's reason first; the reason to signal at once stands.

## The EOF takes the link's error from BoardLink, not from the log

Status: superseded (#65)

`end_stream_at_end_of_run` took the link's error for its `EOF` from `BoardLink.error`, not from the
`esp32tl: the link failed: ...` line in the log, which the guest's output could cut or imitate.
`RunEnded` takes it from `BoardLink.error` the same way: see [a run that ends while the test waits raises RunEnded](#a-run-that-ends-while-the-test-waits-raises-runended-with-the-runners-last-output).

## BoardLink checks every input through the codec before it sends it

Status: decided (#12)

*For the scenario, superseded in #24 by [GPIO lines are an interface of the link, and the scenario
timeline is gone](#gpio-lines-are-an-interface-of-the-link-and-the-scenario-timeline-is-gone):
inputs come from the interfaces only, each on its own pins, and the errors name the interface.*

What the interfaces and the scenario return is checked to be `(t_ns, bytes)` pairs or input
messages, encoded and decoded again, checked against the turn's `sync` and against `watch`; a
failure is the link's error, naming the interface or the scenario, and ends the run with `stop`.
An output longer than one line goes out as several `uart_rx` of the same `t` (`UART_RX_CHUNK`,
512 KiB), which the runner paces one after another. Any other exception in the harness also fails
the link rather than end its thread.

**Rejected:** letting the runner refuse what is wrong, because it ends the run with exit code 3
naming the peer, not which interface or scenario sent it; and an exception that ended the link's
thread left the runner waiting for a turn until `--peer-timeout`, 60 s, then exit code 4.

## uart_tx reaches the interfaces chunk by chunk, with each chunk's time

Status: decided (#12)

`BoardLink` hands every `uart_tx` to the interfaces on its TX pins as it arrives, with its own
`t`, and interfaces find requests by their protocol's terminator.

**Rejected:** joining consecutive `uart_tx` before handing them on, because a boundary may come
from a gap of more than one character time, which keeps two requests apart with their own times,
and the link cannot tell those boundaries from the others (protocol 5.2).

## The link's report is a report section and a JSON user property

Status: decided (#12)

Every test that uses `board` gets the `esp32sim link` report section, for people, and the
`esp32sim_link` user property, a JSON string with the board, the watched pins, the warnings, the
`stats` and the error, for code. Since #21 both carry the routes too, and the section of a test
that failed the run on the board's clock
([decision](#the-run-on-the-boards-clock-goes-into-the-link-section-of-a-test-that-failed)).

**Rejected:** the property as a `dict`, because JUnit XML writes a property's `str()`, which is
Python's `repr`, not JSON. Also rejected: pytest warnings for the runner's `warn` messages, because
a run may send many, and they belong to the run, not to the code that triggered a warning.

## The build directory is passed to IdfApp as an absolute path

Status: decided (#51)

*Extended in #102 by [Explicit build paths are relative to the application and never fall
back](#explicit-build-paths-are-relative-to-the-application-and-never-fall-back).*

`load_app` passes `IdfApp` the build directory as an absolute path, the app's `build` by default.

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
`test_rom_is_the_one_make_setup_fetches` checks that `ROM_RELEASE`, and since the other chips'
ROMs every ELF and SHA-256 of `runtime.ROMS`, match `scripts/fetch-rom-elf`.

**Rejected:** new command-line options for the two paths, so that `--esp32tl-backend` stays the
plugin's only flag.

## A run lasts at most 300 emulated seconds

Status: superseded (#65)

The backend started the runner with `--max-seconds 300` (`MAX_SECONDS`), which also stopped a
runner left behind by a harness that died. A run on the test's clock lasts as long as the test
waits, and the backend passes `--max-seconds 86400`: see [the board's clock is the test's](#the-boards-clock-is-the-tests-emulated-time-moves-only-while-the-test-waits). `MAX_SECONDS`, 300, stays the
default of the `esp32tl` command, whose clock runs free.

## The negative hello_world test edits the app image in place

Status: decided (#51)

`test_hello_world_fails_on_an_image_that_prints_something_else` copies the build, swaps
`Hello world!` for `Hello WORLD!` in the app image, updates the XOR checksum with the difference,
recomputes the appended SHA-256 and has esptool's loader verify both. It then runs the real
`tests/test_hello_world.py` through `pytester` and requires one failure and `Hello WORLD!` in the
output, which shows the edited image booted.

**Rejected:** esptool's `save()`, because it lays the segments out again and raised
`AttributeError` on this image with esptool 5.6.0.

## Board profiles are TOML files in the package, checked key by key

Status: decided (#13)

A profile is `esp32tl/boards/<name>.toml`, read with the standard library's `tomllib` and checked
by `esp32tl.boards.load`: required and unknown keys, integer ranges, the classic ESP32's GPIOs
(a UART's TX not on the input-only 34 to 39), one route per UART and one use per GPIO. Every error
names the file and the key. The profiles ship in the wheel, and a test names one with the
`board_profile` marker; `esp32-devkitc` is the default because it carries every validation
firmware (plan finding 12).

**Rejected:** a schema library, because it would be a dependency for six keys, and its messages
would not name the key the way a person writes it (`uart.gnss.tx`). Local paths were deferred
until a consumer needed them; #101 adds them in
[the local profile decision](#local-board-profiles-use-an-explicit-path-relative-to-the-test-file).

## Local board profiles use an explicit path relative to the test file

Status: decided (#101)

*Extended in #237 by [Project defaults are esp32tl ini options that the markers
override](#project-defaults-are-esp32tl-ini-options-that-the-markers-override).*

`board_profile("name")` keeps selecting a bundled profile. `board_profile(path=...)` selects a
project-local file, with a string or `pathlib.Path` relative to the test file's directory, also
for inherited markers. Absolute paths work too. This matches `app_path` and gives the same
selection regardless of pytest's invocation directory. The marker rejects combined name/path
arguments and uses the existing `boards.load` validation.

Local profiles are cached in the pytest config stash by resolved absolute path for the session.
Collection, backend setup and capability checks use the same validated snapshot. Each test
definition retains the loaded object in `BoardMarkers`, so setup uses it even if a symlink's
target changes after collection's marker checks. Late-marker
checks compare the bundled name or resolved path, so a switch between files with identical names
or contents is detected. A local file does not enter the bundled registry. The handshake,
reports and recording headers keep the TOML name, with recordings retaining the route's wire
metadata; no machine-specific path is added to either format.

**Rejected:** guessing whether a positional string is a name or path; resolving against the
working directory or marker declaration, whose provenance is not retained by pytest for marks
imported from conftest; caching by TOML name, which aliases different files; reloading a profile
at setup, or resolving its symlink again there, which could use data different from what
collection validated.

## The LilyGO profile leaves out what the specification does not give

Status: superseded (#66)

`lilygo-t-a7670g-r2.toml` held the chip, the modem's UART (UART1, GPIO 26/27, 115200 baud) and its
`pwrkey` and `ri` lines, all from the specification, and the pull-down on `ri` that stage 0 found
([gap 6](../stage0-findings.md#gaps)); `flash_mb` and `psram_mb`, optional in the schema, were
absent, and so was a GNSS route. `test_the_lilygo_profile_holds_only_what_the_specification_gives`
compared the file's data whole. The profile now takes its GNSS route, flash and PSRAM from
LilyGO's documentation: see [the LilyGO profile takes its GNSS route, flash and PSRAM from
LilyGO's
documentation](#the-lilygo-profile-takes-its-gnss-route-flash-and-psram-from-lilygos-documentation).

**Rejected:** `flash_mb = 4` and `psram_mb = 4`, as the plan's §4.5 sketch has them, because the
specification gives neither and they would have been written from memory, which it rules out.

## The capability declaration is a TOML file beside the profiles, per interface kind

Status: decided (#13)

`esp32tl/boards/capabilities.toml` declares, per backend and per interface kind (`console`,
`uart`), the sources a backend takes and whether it can tap. It declares `serial` with the
specification's table (real only, only the console tapped). At the time of #13, `serial` was
not yet in `SERVICES`; the declaration gave skips data to act on before #25 added the backend.

**Rejected:** a dict beside `SERVICES` in `esp32tl/backends/__init__.py`, the "capabilities per
interface" of plan §3's `Backend`, because the plan's §4.5 and issue #13 place the declaration
beside the profiles, as data. Also rejected for now: declaring per interface of each profile
(`uart.gnss`). The specification decides by where the part is: "only what is off the board can
be modelled" on a real board, and the GNSS receiver of `esp32-devkitc` is wired to GPIO 4/5, not
soldered on. Per kind, `serial` treats every UART as on-board, which matches plan M4 (the GNSS
test skipped on `serial`): modelling an off-board part on a real board needs host-side hardware,
which the specification puts "later"
([postponed](#telling-on-board-from-off-board-interfaces-is-postponed)).

## Telling on-board from off-board interfaces is postponed

Status: postponed (#25)

A profile does not say whether the part at the end of a route is soldered on or wired to the
board, and the declaration does not distinguish them: on `serial`, every UART is real only. The
distinction comes with a way to drive an off-board interface of a real board from the host.

## A source or tap a backend cannot give skips its parameter at collection

Status: superseded (#21)

Superseded for the `source` marker in #65 by [what is at a route's far end is what the test does
with it](#what-is-at-a-routes-far-end-is-what-the-test-does-with-it-before-power-on-not-a-marker),
and for the `tap` marker in #21 by [a test taps a route by calling tap(path) before power-on](#a-test-taps-a-route-by-calling-tappath-before-power-on-not-with-a-marker): both skip at the call now.

The `source` and `tap` markers named what a test needs; `pytest_generate_tests` marks the
parameter of each backend that could not give it with `pytest.mark.skip(reason=...)`. pytest
skipped such an item before setting up any fixture.

**Rejected:** `pytest.skip` in the `board` fixture, because `board` depends on pytest-embedded's
`dut`, which for a backend running `esp,idf` opens the serial port before `board` runs. Also
rejected: deciding from what the test body calls (`gnss.replay(...)`), because the body runs after
the backend has started.

## The closest source marker wins per interface

Status: superseded (#65)

For each interface the `source` marker closest to the test decided, each class of a test class's
MRO a level of its own, and two on one level were a collection error. The `source` marker is gone:
what is at a route's far end is what the test does with it before power-on, see [what is at a route's far end is what the test does with it](#what-is-at-a-routes-far-end-is-what-the-test-does-with-it-before-power-on-not-a-marker).

## Board markers that reach a test after collection read it are refused

Status: decided (#13)

*For the `source` marker, superseded in #65 by [what is at a route's far end is what the test does
with it](#what-is-at-a-routes-far-end-is-what-the-test-does-with-it-before-power-on-not-a-marker),
and for the `tap` marker in #21 by [a test taps a route by calling tap(path) before power-on](#a-test-taps-a-route-by-calling-tappath-before-power-on-not-with-a-marker): the plugin reads `app_path` and `board_profile`, and a
`board_profile` there would reach the board unchecked the same way.*

`pytest_generate_tests` keeps what it read from each definition's `app_path`, `board_profile`,
`source` and `tap` markers; `pytest_collection_modifyitems`, `trylast`, reads them again from
each item and stops the run with a usage error where they differ. Marks of a
`pytest.param(..., marks=...)`, and those a hook adds with `add_marker` to the item, its class or
its module (marking tests by directory in a conftest is common), reach the item but not the definition, so a `source` there
would never skip its backend: on a real board the test would run against the real part without a
word, which the specification forbids. A marker a fixture adds at setup is too late for any check.

**Rejected:** looking for the marker names in `item.callspec.marks` only, because that misses the
hooks. Also rejected: reading the markers per item in the `board` fixture, or failing there,
because by then pytest-embedded's fixtures have run, and for `serial` the port is open.

## A profile reserves the console's UART and pins, not the flash pins

Status: decided (#13)

Every board has the `console` interface, UART0 on GPIO 1 and 3, so a route on UART0, or a route
or a line on GPIO 1 or 3, is refused as a second use. GPIO 6 to 11 are accepted.

**Rejected:** refusing GPIO 6 to 11, because which of them the SPI flash takes depends on the
module and its flash mode, and a profile does not name its module.

## A request no backend can give is a collection error

Status: superseded (#21)

Superseded for the `source` marker in #65 by [what is at a route's far end is what the test does
with it](#what-is-at-a-routes-far-end-is-what-the-test-does-with-it-before-power-on-not-a-marker),
and for the `tap` marker in #21 by [a test taps a route by calling tap(path) before power-on](#a-test-taps-a-route-by-calling-tappath-before-power-on-not-with-a-marker): a source or a tap no backend gives is a `SourceError` at
the call, which fails the test, still not a skip.

A `source` or `tap` marker that named an interface the profile lacks, or a source or tap that no
declared backend gives, failed collection with the test's id.

**Rejected:** skipping such a test on every backend, because a misspelt source (`"replya"`) would
then pass the run as skipped, never as wrong.

## The run waits before the guest boots until the test has given its declared sources

Status: superseded (#65)

The runner started at the `board` fixture's setup, and `BoardLink` held its first turn, at `t = 0`,
until the test body had given every source a `source` marker declared, bounded by the runner's
60 s `--peer-timeout`; a `console.expect` before then raised `SourceError`, and a run never
started was stopped at teardown and failed a test that had passed. The test now gives its sources
before `board.power_on()`, which starts the runner, and every turn is held until the test waits:
see [the board's clock is the test's](#the-boards-clock-is-the-tests-emulated-time-moves-only-while-the-test-waits) and [a run starts at board.power_on()](#a-run-starts-at-boardpower_on-and-so-does-the-runner).

## A replay needs the source marker that declares it

Status: superseded (#65)

`gnss.replay(path)` was refused without `@pytest.mark.source("uart.gnss", "replay")`, and only a
declared route was on the link, so that the board knew at setup which sources to wait for. Every
UART route of the profile is on the link now, and the call alone gives the source: see [what is at a route's far end is what the test does with it](#what-is-at-a-routes-far-end-is-what-the-test-does-with-it-before-power-on-not-a-marker).

## The esp32sim backend refuses a model or real source at setup until it gives one

Status: superseded (#65)

A `source` marker that named `model` or `real` failed the test at setup on `esp32sim`, which
declares both and gave neither. Without the marker a source is asked for by the call that gives
it, and there is no call for a `model` or a `real` source yet: see [what is at a route's far end is what the test does with it](#what-is-at-a-routes-far-end-is-what-the-test-does-with-it-before-power-on-not-a-marker).

## A replay path is relative to the test file's directory

Status: decided (#14)

`gnss.replay("../recordings/gnss-generated-1.jsonl")` from `tests/` reads `recordings/` at the
root, whatever directory pytest runs in, as the `app_path` marker does.

**Rejected:** the working directory, as `open()` has it, because the same test would then find its
recording or not depending on where pytest was started.

## A stream replay sends each event in the turn at its time

Status: decided (#14)

`Replay.poll(now)` returns the events due at `now`, and `quiet_until` is the next event's time, so
the runner stops at each recorded time and the event goes out stamped with it. This is plan §4.4's
`quiet_until` and the `replay` row of protocol §7; it costs one turn per distinct event time, 61
turns for a generated GNSS recording.

**Rejected:** sending every event in the first turn, as `tests/test_nmea_reader.py`'s bare peer
did until #16, for the runner to queue (protocol §8), because the replay's `quiet_until` would then come
from nothing in the recording, and the replay would hand the runner a whole recording's bytes at
once.

## The recording reader takes the generated recordings' header as it is

Status: decided (#14)

The [postponed question](firmware.md#generated-recordings-carry-no-app-no-tool-versions-and-a-fixed-creation-time)
is decided for the reader: `source` is any non-empty string, so `generated` is read; `app_sha256`
may be `null`, for a recording no app made; and fields the format does not define, such as
`generator`, are kept as they are and written back after the format's own. The format gains no
baud field: the bytes go in at the board profile's line rate, and a firmware configured otherwise
sees the frame errors the runner reports as `frm_err`, which
[fail the run](#a-frame-error-fails-the-run-at-once) since #16. Event and clock-switch lines take their
keys and no other.

**Rejected:** refusing unknown header fields, as board profiles refuse unknown keys, because a
tool that records where a recording came from would need a new format version for each field;
a closed vocabulary for `source`, because the models and real devices it would name do not exist
yet; and a baud in the header, because it would say again what the profile says, and could
contradict it.

## The esp32sim backend does not compare a recording's app_sha256 with its app

Status: decided (#14)

`app_sha256` says which app ran when a recording was made. A replay into another app is what a
test of a changed firmware does with an old recording, so the `esp32sim` backend does not compare
the two. Plan §4.3 compares them on the `serial` backend, against what the harness flashed.

**Rejected:** refusing a mismatch, because a recording belongs to the wire, not to the app, and
the generated recordings belong to no app at all.

## Console.write is postponed until the runner reads input

Status: superseded (#195)

The original premise was that console input required the runner to read stdin. It does not:
`uart_rx` on watched GPIO 3 feeds UART0 through its native or matrix RX route. `Console.write`
and `sendline` now use that route; no runner message or stdin reader is needed.

## The serial backend is postponed to M4

Status: superseded (#25)

Before #25, `esp32tl.backends.SERVICES` had only esp32sim, so tests had one backend parameter.
[Serial is now opt-in](#serial-is-opt-in-and-pytest-embedded-owns-flashing-and-the-port), with
pytest-embedded's `esp,idf` services. The default still runs esp32sim only.

## Prebuilt runner binaries are postponed to M10

Status: superseded (explicit runtime distribution)

The backend finds the runner in the checkout, so from a wheel only `ESP32TL_RUNNER` and
`ESP32TL_ROM` find the runner and the ROM.

What a consumer pays for it today, measured on 2026-11-14 on an Apple M2 Pro (10 cores) on a fast
connection, at the esp32sim pin 3bdb958: rustup installs the pinned toolchain, Rust 1.99.1 with
clippy and rustfmt, in 12.5 s, 463 MB on disk; a cold `cargo build --locked --profile release` in
`runner/`, with an empty `CARGO_HOME` and target directory, so with every crate and esp32sim
fetched from git, takes 28.4 s of wall time (26.3 s user), 141 MB of `CARGO_HOME` and 56 MB of
target. `make rom-elf` adds a download of the ROM ELF from GitHub. CI's runner job builds it
cold too, in under a minute ([CI/CD](../cicd.md)). The cost is under a minute of a consumer's CI
and a Rust toolchain on its machines, not a build that dominates a run, so the postponement holds.

It should move earlier when the first of these comes: the repository goes public and a consumer
installs esp32tl from a wheel, which has no `runner/` to build; a consumer's CI cannot have Rust,
or reach GitHub, on its machines; or a cold build of the runner, measured as above, takes more
than about five minutes on a CI machine.

## Replaying requests, looping and wall-clock recordings is postponed

Status: superseded (#20)

The replay replays streams only, and refuses the rest with a `ReplayError`. A recording with `out`
events holds requests, whose answers need the matching engine of plan §4.4 (#20). `end: loop` on a
stream needs a period, which the format does not record: the time from the last event to the
start again is nowhere in the file. A recording on the `wall` clock, or one that switches to it,
would put wall times on the emulated clock, which needs the stamping rule of the `real` source on
the emulator (#27).

## A frame error fails the run at once

Status: decided (#16)

`BoardLink` takes a `frm_err` warning as an error of the run, naming the interface whose RX pin
it is and both bauds, and the backend ends the run at once, as for any link error. A frame error
means the bytes went in at another baud than the guest's UART receives at, and the firmware lost
them; kept as a warning, it failed a test only by a timeout on what the firmware never printed,
and the reason was in the link's report, not in the failure. Plan M1 asks for a mismatch to be a
test failure "rather than silence".

**Rejected:** failing on the `uart_cfg` that sets a route's baud to another than the interface's,
because a firmware may configure the UART at one baud and switch before the first byte, as a
GNSS driver that negotiates the receiver's rate does, and only the bytes that reach a mismatched
UART are lost; failing on `rxfifo_ovf` and `unrouted_rx` too, because those are what the firmware
does with its UART (reading too slowly, configuring it late), which a wire does to a real board
as well, and a test may look for exactly that.

**Postponed:** a way for a test to expect frame errors, a marker or an option on the source, for a
firmware that detects a wrong baud or finds the right one by itself. Until a test needs it, such
a firmware cannot be tested through a replay at the wrong baud. A route whose RX pin the firmware
sets before its baud gets the same error, since the runner reports bytes into it as `frm_err`.

## The esp32tl command serves the link itself, without pytest-embedded

Status: decided (#16)

`esp32tl run` and `esp32tl replay` start the runner with `subprocess.Popen`, its stdout and stderr
the command's own, and serve the link with `BoardLink` in a thread, using the backend's
`load_app`, `runner_command`, `listen` and `profile_uart`. The replay is given before the runner
starts, so the link is not held. The link's report goes to stderr when the run ends.

**Rejected:** running the `Esp32Sim` backend itself, because it is built on a pytest-embedded
`Dut`, whose log file, `pexpect` process and message queue only a pytest session sets up, and the
console then reaches the terminal through a log file instead of directly.

## The esp32tl command runs the runner in a session of its own

Status: decided (#16)

A terminal's Ctrl-C sends `SIGINT` to the whole foreground process group. Were the runner in it,
the command's own `SIGTERM` on Ctrl-C would be the runner's second signal, which kills it with
exit code 143 and no statistics (protocol §9). With `start_new_session=True`, Ctrl-C reaches the
command only; it sends one `SIGTERM`, and a second Ctrl-C sends `SIGKILL`. It then exits 130, as
a shell reports `SIGINT`. A `SIGINT` handler takes Ctrl-C for the whole run, from before the
runner starts to the report, so that none ends the command with a `KeyboardInterrupt` traceback
and no report.

**Rejected:** leaving Ctrl-C to the runner, with the command ignoring `SIGINT`, because the
command also sends `SIGTERM` when the link fails, and a Ctrl-C after that would still be the
runner's second signal.

## esp32tl replay takes one recording, and esp32tl record waits for the matching engine

Status: postponed (#20)

`esp32tl replay` takes one `--interface` and one `--recording`: no profile has two UART routes a
recording can go into yet. `tap(path)` came with #21, under pytest only; `esp32tl record`, which
would write what a tap records from the command line, comes back with the matching engine (#20),
together with the engine's list of the requests it has not seen.

## The board's clock is the test's: emulated time moves only while the test waits

Status: decided (#64)

Under pytest, every run is held in the harness's turn of the link for as long as the test body
runs, and moves on only inside `board.wait(duration)` and `board.wait_until(condition,
within=duration)`; `board.now` is the time since power-on. A test can therefore describe the
world around the board step by step (a receiver starts moving, the cloud publishes) and each
change lands at the exact emulated instant the test saw. `wait(d)` bounds the next turn at
`now + d`. `wait_until` lets the run go in steps of at most 100 emulated milliseconds, or to the
next event of the link, asks the condition at each turn and returns what the condition returned
at the first turn it holds; when `within` runs out it raises `NeverHappened`, an
`AssertionError` that names the condition and the time it waited from and to, and when the run
ends first it raises `RunEnded`, with the reason. The `esp32tl` command has no test body, and
keeps running its clock free.

The console is read from the link's `console` messages (`--console both`), which arrive in
emulated-time order with the turns; the runner's stdout still feeds pytest-embedded's `dut.log`.
`console.expect` and `expect_exact` keep pytest-embedded's names, its search from the end of the
last match, its return values and `return_what_before_match`, but the matching is esp32tl's and
waits on the held clock: `timeout`, 30 by default as before, is in seconds of the board's clock,
and a pattern that does not appear raises `NeverHappened` instead of pexpect's `TIMEOUT`.
`expect_all` and `not_matching` are not carried over until a test needs them.

A run has no length of its own any more: it lasts as long as the test waits. The backend starts
the runner with `--max-seconds 86400`, a day of emulated time, only because the flag is required;
a test that reaches it gets `RunEnded`. A runner whose harness has died does not need the limit
either: it sees the socket close and exits with code 4 (protocol §9).

Measured on a spike on 2026-10-31, on the protocol as it is, with no runner change, only
`--console both`: `nmea_reader` and the production release image of
[stage 0](../stage0-findings.md#inputs) ran their tests on the held clock, 222 emulated seconds of
the release image in 8 s of wall time, and two runs of the same test printed the same console line
at the same emulated millisecond. `hello_world` and the four
`nmea_reader` tests of `tests/` needed `board.power_on()` added, and the `nmea_reader` ones lost
their `source` marker ([below](#what-is-at-a-routes-far-end-is-what-the-test-does-with-it-before-power-on-not-a-marker)).

**Rejected:** keeping a free-running clock beside the held one, for tests that only expect on the
console, because a test would then have two models of time to keep apart, and the free-running
one measures its timeouts in wall time, which [stage 0](../stage0-findings.md#speed) showed
cannot be relied on. Also rejected: an `async` test API, because pytest and pytest-embedded are
synchronous and a held clock gives the same reading order without it; and keeping the 300 s
limit, because a scenario's length would then be capped by a safety net that no longer has
anything to catch.

## A run starts at board.power_on(), and so does the runner

Status: decided (#64)

The test body sets up what is around the board and in its flash, then calls `board.power_on()`,
which starts the runner and lets the guest boot. A test that waits before it gets
`the board is off; call board.power_on() first`. A test that set up something only a run can use
(a source on a route, a modem or a network service, flash contents) and never called
`power_on()` gets an error at teardown, as a test that never gave its declared source did until #65:
it ran no firmware, and would pass whatever the firmware does. A test that touched none of these,
such as one that only reads `board.profile`, passes without a runner. Starting the runner at
power-on rather than at the `board` fixture's setup lets the body still choose what the flash
holds besides the build.

**Rejected:** booting at the first `wait` or `expect`, as
[the decision on held starts](#the-run-waits-before-the-guest-boots-until-the-test-has-given-its-declared-sources)
already rejected for an implicit trigger; starting the runner at setup and the guest at
`power_on()`, because the flash images are fixed when the runner starts; and passing every test
that never powers on, because a test that set up a replay and asserts that nothing was published
would pass with no firmware run.

## The test's own timeout catches a hung run, not the runner's peer timeout

Status: decided (#64)

With the clock held by the test, the runner waits in its turn for as long as the test body runs, so
the backend starts it with `--peer-timeout 0`. What ends a run that hangs, in the harness or in the
emulator, is the test's timeout in wall time, through pytest-timeout, which becomes a dependency of
esp32tl. The plugin gives every test that uses `board` a default of 600 s unless the test's own
configuration sets one: a `timeout` ini option, `--timeout`, the `PYTEST_TIMEOUT` environment
variable or a `timeout` marker. A configured `timeout = 0` is the user's choice and is kept; their
CI's job timeout is then the only limit. pytest-timeout turns itself off when it detects a debugger
such as pdb, unless `--timeout-disable-debugger-detection` is given. The backend's teardown ends the
runner as for any other failed test; on the spike a test with a 2 s timeout that waited 4:50 of the
board's time failed with `Timeout (>2.0s) from pytest-timeout` and left no runner behind. The limits
a test states, `within=` and `timeout=`, are in the board's time and are part of what it tests; the
wall-time timeout only says the harness or the emulator is stuck.

600 s leaves room for a test that keeps the CPU busy: stage 0 measured CPU-bound code at 0.3 to
1.7 times real time, so ten minutes of wall time covers at least three minutes of the board's
busiest work, and far more of an idle board.

**Rejected:** keeping the 60 s default of `--peer-timeout`, because a test stopped in a debugger
for a minute would lose its run, and it is a second limit, hidden in the runner, on top of the
test's own; and no default, because a run that hangs would then hang a CI job until the job's own
limit.

## What is at a route's far end is what the test does with it before power-on, not a marker

Status: decided (#64)

Every UART route of the board profile is on the link from the start, listening and silent. The
`gnss` fixture becomes a replay when the test calls `gnss.replay(path)` and a modelled receiver
when it calls `gnss.fix(...)` or another model method, before `board.power_on()`; the `source`
marker is no longer needed to say it again. A backend whose capability declaration does not give
what the test asked for skips the test at that call, with the reason, as `serial` does for a
model: the test still runs on `esp32sim`. A source that no backend gives is an error at the call,
not a skip, as it is a collection error now. The `real` source has no call yet, and gets one with
M5. The `tap` marker was not part of this decision; #21 dropped it the same way: [a test taps a route by calling tap(path) before power-on](#a-test-taps-a-route-by-calling-tappath-before-power-on-not-with-a-marker).

**Rejected:** keeping the marker, because the test states the source twice, once in a decorator
far from the body; the reason it was needed, that the board had to know at setup whether to wait,
is gone once the run starts at `power_on()`. The cost accepted: on the `serial` backend a test is
skipped in its body rather than before its setup, after pytest-embedded has opened the board's
port, which [the skip at collection](#a-source-or-tap-a-backend-cannot-give-skips-its-parameter-at-collection)
was made to avoid.

## The board's durations are numbers of seconds until the units land

Status: superseded (#69)

`board.wait(duration)`, `wait_until`'s `within` and an expect's `timeout` took an `int` or a
`float` of seconds of the board's time, finite and 0 or more, and `board.now` was a `float` of
seconds. An expect's `timeout` was a number of seconds before, and stayed one, so a test read the
same way with every limit it stated. The units have landed: see [the board's durations are
Durations, and an expect's timeout also takes seconds](#the-boards-durations-are-durations-and-an-expects-timeout-also-takes-seconds).

**Rejected:** a `Duration` type in this change, because the units module comes with the GNSS model
(#69), with its own feature documents, and a type of its own here would be replaced there. Also
rejected: `datetime.timedelta`, because its resolution is a microsecond, not the link's
nanosecond, and the units decision gives the API esp32tl's own quantities.

## A wait asks its condition in the test's thread, while the link is held

Status: decided (#65)

`HeldClock.turn` holds the link's thread at the start of every turn and wakes the test, which asks
its condition, in its own thread, and then lets the run go to its next bound or returns. The
console's patterns are such conditions. The link reads nothing while it is held, so the condition
reads the console and anything else of the link without a race. It costs two thread switches per
turn: at the 100 ms step, ten per emulated second.

**Rejected:** asking the condition on the link's thread, inside `turn`, as the spike did, because
an exception from the test's condition would fail the link as the harness's own fault instead of
failing the test where it was written, and a condition would read the test's state from another
thread.

## A run that ends while the test waits raises RunEnded, with the runner's last output

Status: decided (#65)

The backend's waiter thread ends the clock once the runner has exited and the link has been read
to its end, with `the runner exited with code N; its last output:` and up to 2 KiB of the runner's
log (`LAST_OUTPUT_BYTES`), from the first whole line and without colour codes, after
`the link failed: {error}; ` when `BoardLink.error` is set. Every wait from then on raises
`RunEnded`, which names the board's time and what the test waited for; a wait first asks its
condition once more, since what the guest printed last is in by then. The plugin finds `RunEnded`
in the call's `__cause__`/`__context__` chain, stopping at a cycle, and keeps the teardown quiet
about the exit it reported. The error comes from `BoardLink`, not from the log, where the guest's
output could cut or imitate it.

**Rejected:** keeping the pexpect stream and its end-of-run `EOF`, because the console reads the
link now, and the stream's timeouts are wall time. Also rejected: turning `RunEnded` into
`NeverHappened` inside an expect, as the spike did, because the end of the run is a different
failure, whose exit code and output are what the reader needs first; and staying quiet at teardown
after any failure, because a failure of the test's own could then hide a runner that crashed in
the same test.

## Teardown stops the run at the turn the test left it at

Status: decided (#65)

`Esp32Sim.close` stops the clock first: the link answers the turn it is held at with `stop`, and the
backend waits up to 5 s for the runner to exit 0, before the `SIGTERM` of [teardown waits for the
runner's hello](#teardown-waits-for-the-runners-hello-sends-one-sigterm-and-passes-exit-code-0-only),
which is left for a runner in no held turn: one still loading its images, or one whose test's
wall-time timeout broke into a wait. The run then ends at the board's time the test last saw, and
two runs of a test end alike.

**Rejected:** letting the run go and sending `SIGTERM`, as the spike did, because the run would
run free between the two, and where it ended would depend on wall time.

## The default timeout is a marker the plugin adds to each test that uses board

Status: decided (#65)

When the session sets no timeout (`--timeout`, `PYTEST_TIMEOUT`, the `timeout` ini option, read
through pytest-timeout's own `get_env_settings`), `pytest_collection_modifyitems` adds
`pytest.mark.timeout(600)` to every test that uses `board` and has no `timeout` marker of its own.

**Rejected:** setting pytest-timeout's session default, because tests that do not use `board`
would get it too; and checking for a configured timeout with private attributes of the config,
because `get_env_settings` is what pytest-timeout reads them with.

## A source the backend cannot give skips the test at the test's line

Status: decided (#65)

The `board` fixture gives the backend `skip_at_the_test`, which raises
`pytest.skip.Exception(reason, _use_item_location=True)`, so `-ra` names the test's file and line,
as for a skip marker. `_use_item_location` is pytest's own argument, used by its skip marker, and
not part of its documented API; `tests/test_pytest_plugin.py::test_a_source_or_a_tap_the_backend_cannot_give_skips_at_the_call`
fails if it changes.

**Rejected:** `pytest.skip(reason)`, because the report names the line of `esp32tl/backends/esp32sim.py`
that raised it, the same for every test.

## A leftover source marker is not refused

Status: decided (#65)

The plugin no longer registers the `source` marker. With `--strict-markers`, as this repository
runs, a test that still carries one is a collection error,
``'source' not found in `markers` configuration option``; without it, pytest warns of an unknown
marker (`PytestUnknownMarkWarning`) and the test runs.

**Rejected:** a message of esp32tl's own for the marker, because no project outside this repository
uses it yet.

## A test taps a route by calling tap(path) before power-on, not with a marker

Status: decided (#21)

`gnss.tap(path)`, or `board.uart(role).tap(path)`, before `board.power_on()`, records what crosses
the route from power-on and writes it to `path` once the run has ended. The backend checks the
request the way it checks a source's: after power-on it is a `SourceError`, a tap no backend
declares is a `SourceError` at the call, and one this backend does not declare skips the test
there. The `tap` marker is gone, and with it the skip before setup, the collection error for a tap
no backend gives, and the marker among those refused when they reach a test late. The tap wraps
whatever is at the far end, a replay or nothing, and later a model, so a test can record a route
it gave no source.

**Rejected:** keeping the marker, for the reason the `source` marker went in #65: the test would
state the tap twice, in a decorator and in the call that names the file, and a marker cannot name
the file. Also rejected: the marker alone, writing to a path esp32tl chooses, because the test
then has to find its recording, and a fixed name collides between tests. The cost accepted is the
one #65 accepted for sources: on `serial`, which taps no UART, the skip comes after
pytest-embedded has opened the port.

## A tap writes its recording once the run has ended, from what the far end gave

Status: decided (#21)

A `Tap` keeps every event in memory, `out` at the time the firmware wrote the bytes and `in` at
the time the source stamped them, and the backend writes it in `close`, after the link has been
read to its end, sorted by time, whatever the run's outcome. Its header's `source` is the kind of
source, `replay`, or `none` for a route without one. An `in` event is what the far end gave: an
output the link refuses, stamped before its turn or on a route without an RX pin, is in the tap
although it never reached the chip, and the run has failed on it. An output that is no
`(t, bytes)` pair with `t` from 0 is not recorded, so a broken source never makes a recording the
format refuses, and the link's error is what the test reports. A tap refuses a file the run
replays from or another route taps into, and a replay refuses a file a route taps into.

**Rejected:** writing as the run goes, because events reach the tap out of time order (an answer
stamped `t_req + latency` comes before a byte the firmware writes in between) and the format wants
time order; holding them back to the turn at which they are final costs code for a file nothing
reads before the end. Also rejected: stamping `in` events with the time the runner put them in,
because the link does not report it, and a replay tapped would no longer give back its own events;
and recording in `BoardLink` after its checks, so that a refused output is not in the tap, because
the run has failed by then, and the tap wraps the source, which the link knows nothing of. Also
rejected: writing over a recording the test replays from, as `gnss.tap` on the replay's own path
would, because the committed recording would change at teardown without a sign in the run.

## The run on the board's clock goes into the link section of a test that failed

Status: decided (#21)

The `esp32sim link` section ends with `routes:`, a line per UART route with its source, its bytes
each way and its tap, for every test; for a test whose setup, call or teardown failed it also ends
with the run on the board's clock: the test's steps, the console's lines and every route's bytes,
in the order of the board's time. The log keeps every step of the test and the last 200 entries of
the console and the routes, as the bytes they came as, and turns them into text only for the
report. The user property gets the routes, not the run.

**Rejected:** a section of its own, because issue #21 asks for one report per test with the
runner's statistics in it, and pytest shows the teardown sections of a failed test together;
showing the run for a passing test, because `-rA` would print it for every test of a suite;
keeping every entry, because a report of an hour of NMEA would bury its end, where a failure is,
and the tap keeps everything for whoever needs it; and keeping the last 200 of the steps too,
because a long replay would push out `board.power_on()` and the start of the wait that failed,
which say what the end means. Also rejected: formatting each event as it comes, because a passing
test, whose run nobody reads, would pay for it on every byte. Also rejected: the run in the user property,
because JUnit XML holds it as one string, and a tap is the run's data for code.

## Tapping the console is postponed

Status: superseded (#25)

At #21, no feature consumed a console recording, so the console remained only in `dut.log` and
the run report.
[Console taps now use both backend clocks](#console-taps-use-the-same-api-on-both-backends):
`app_recording` can check their app hash on serial and recording tools can read the output. These
recordings still cannot be replayed through the console API. Console input is now available
through `write` and `sendline` (#195).

## Models describe the world in its own terms, in units

Status: decided (#64)

A model speaks the vocabulary of the device it stands for and nothing more, and its file is named
after the device: `sources/model/gnss.py`, `sources/model/modem.py`. The GNSS receiver model is
told `fix(at=position)`, `move(heading=..., speed=...)`, `stand_still()`, `lose_fix()`,
`degrade(hdop=..., satellites=...)` and `go_silent()`; each change holds from the board's current
instant until the next one, and it sends an RMC and a GGA with the same UTC time once a second.
Quantities are values with their unit, built by `seconds()`, `minutes()`, `km_h()`, `mph()`,
`knots()`, `m()`, `km()`, `miles()` and `Position(lat, lon)`, and compared with
`about(value, within=tolerance)`, so no number in a test leaves its unit to the reader. Words of a
product, a car that drives or a bike that is ridden, belong in that product's own tests, built on
the model. On the spike the model drove the production release image of
[stage 0](../stage0-findings.md#inputs) to the same power decision as a generated recording of the
same trip, one second earlier because its first epoch is one second earlier.

The models share one world time: a UTC instant at power-on, `2026-01-01T12:00:00Z` unless the test
gives another as `board.power_on(utc=datetime(...))`, plus the board's clock. The GNSS receiver
dates its sentences from it, and the network's NTP server tells it, so runs repeat whatever the
host's clock says.

**Rejected:** vehicle verbs in esp32tl, because the same receiver sits in products that are not
vehicles; plain numbers in a unit the API implies, because kilometres, miles and knots all occur
in the same domain; and the host's clock as the world's time, because runs would then not repeat.

## Tests observe the device from outside, and product knowledge stays in the consumer's tests

Status: decided (#64)

A test asserts on what the firmware does on its wires and through them: its console, its pins,
the bytes on its UARTs, and what reaches the services it talks to. A consumer interprets those in
its own vocabulary in its own tests (which topic is its status, what its report means). esp32tl
knows protocols (NMEA, AT, PPP, MQTT) and the ESP-IDF platform (its partition table, NVS), never a
product.

**Rejected:** a log level of its own for tests, because it needs the firmware changed and the
tests would then check something that does not ship; and reading the guest's memory by ELF symbol,
because it ties a test to the firmware's internals and cannot work on a real board.

## The network the device reaches is simulated in the harness, in emulated time

Status: decided (#64)

When a modem model is dialled, the harness ends the link itself: PPP (LCP and IPCP, handing the
device an address and a DNS server), IPv4, UDP, and a TCP that accepts and, since #196, opens
connections too ([the test opens TCP connections to the
device](#the-test-opens-tcp-connections-to-the-device-on-the-same-tcp-with-no-timers)). The runner paces
what goes into the device and reports every byte the device sends, so as long as the firmware
drains its receive FIFO the link loses and reorders nothing, and that TCP needs no retransmission,
congestion control or timers. A byte the device's FIFO drops is reported as an `rxfifo_ovf`
warning; on a route that carries the network it fails the run, as a `frm_err` does, since PPP
would drop the frame and the connection would stall until the test's timeout with nothing to say
why.

Every destination answers: a connection goes to the service on its destination port, whatever
the address, and a DNS query for any name returns the network's one address, so the device keeps
the names and addresses it was built with. One service listens per port; telling two names apart
on one port, by TLS's server name, is left until a test needs it. The services are the test's: a
DNS server, an NTP server telling [the world's time](#models-describe-the-world-in-its-own-terms-in-units),
TLS in front of any service with a certificate authority made for the test, its certificates
valid from 2000 to 2099 so that any plausible device clock accepts them, and an MQTT 3.1.1 broker
whose messages the test waits for and publishes to. All of it runs in the harness's turns, on the
board's clock, in about 1 040 lines of Python with the modem model.

Measured on the spike: esp-protocols' `pppos_client` (esp_modem 1.4.1, SIM7600 commands, changed in
its sdkconfig only) dialled, got its address, resolved its broker, connected and published in 0.18
emulated seconds, and received what the test published; two runs gave the same transcript. The
production release image of [stage 0](../stage0-findings.md#inputs), given its credentials and the
test's CA, dialled, asked NTP and DNS, completed a TLS 1.2 handshake (ECDHE-RSA-AES256-GCM-SHA384),
logged in to the broker, subscribed and published, and the test read what it published.

**Rejected:** `pppd` in a Linux container with NAT to the host and the data phase under
`--realtime`, as plan M6 had it, because the far end then lives in wall time: runs do not repeat,
the test cannot hold the clock, Docker becomes a requirement, and what the device sends ends up
in the host's network rather than in front of the test. Also rejected: the TCP/IP stack in the
runner, with smoltcp or a stack like esp32sim's user-mode NAT, because the modem's bytes already
reach the harness and a stack there would only add messages to the protocol; and a bridge to a
broker such as mosquitto as the default, which brings wall time back.

## The test opens TCP connections to the device, on the same TCP, with no timers

Status: decided (#196)

*Superseded in part by [the test's TCP sends a SYN the device does not answer again](#the-tests-tcp-sends-a-syn-the-device-does-not-answer-again-on-the-boards-clock)
(#289): for the SYN only. The rest of this decision stands.*

Firmware that serves HTTP, WebSocket or a protocol of its own is tested from its client's side:
`network.connect(port, *, address=None)` opens a connection to the device behind the modem and
behind the access point alike, and returns a `TcpConnection` that writes, closes and keeps what the
device sent. It is the TCP that accepts the device's connections, with an active open added, so what
[the network the device reaches](#the-network-the-device-reaches-is-simulated-in-the-harness-in-emulated-time)
holds about a link that loses and reorders nothing holds here too, and decides what the client
side supports:

- no retransmission and no timers: the link loses nothing, and a segment is acknowledged in the
  turn it arrives in; since #289 the SYN is the exception, [sent again](#the-tests-tcp-sends-a-syn-the-device-does-not-answer-again-on-the-boards-clock) because the
  device's stack drops it;
- the device's window is respected, and a zero window waits for the device's window update, with no
  persist timer;
- only the MSS option, 1400 offered and the device's taken up to 1400; no window scaling, SACK or
  timestamps, which an 8192-byte window and a link that loses nothing do not need;
- no TIME_WAIT on the test's side: its ports count up from 49152 and are never reused, so a segment
  of an old connection cannot reach a new one;
- no half-close: the device's FIN closes the test's end after what it has written, as it does for
  the device's own connections.

The test's side is `10.64.0.1`, the network's own address, and its initial sequence number is the
fixed one, so runs repeat. A segment from the device ahead of its stream, which this link cannot
produce, fails the turn with `OutOfOrder` rather than wait for a byte that never comes. Connecting
before the device has an address, or, behind the access point, before the station has sent a frame
to learn its MAC from, raises `NotReachable` saying which. The address is IPCP's behind the modem,
and behind the access point the one this link's DHCP acknowledged, forgotten at the device's next
DHCP message that is not acknowledged, a DISCOVER or a RELEASE among them: an earlier lease in
`dhcp.leases` does not mean the device has its address now.

The device's chip reset, whatever caused it, and `board.power_off()` take its TCP and its address
with them, while the access point, the modem and their links stay. The backend tells every
network, which loses the test's connections, as `lost` with the reason (`the device reset`, `the
device lost its power`), and forgets the address until IPCP opens again or DHCP acknowledges one.
Without that, a connection open across a reset would wait out its time for an answer from a stack
that no longer exists, and `connect` right after `board.reset()` would send a SYN to an address the
device no longer has, both timeouts that name the wrong cause. The connections the device opened
are left as they are, since a network does not know of a reboot: a RST of the new stack, in answer
to what their service writes, ends them, or the service's end, or the link's; MQTT's keepalive is
the deadline the tests of the broker check. A SYN of the new stack from the same port to the same
service falls on the old connection and gets no answer, where a real TCP would send a challenge ACK
(RFC 9293, 3.10.7.4) and take the RST that follows: that is how the device's connections were before
#196, and changing it is not part of it.

The conditions follow [the rule of #108, #116 and
#182](#mqtt-tls-http-and-dhcp-event-apis-name-history-queries-and-next-event-conditions-explicitly).
A connection is established, closed by the device, refused or reset once at most, so
`established()`, `closed()`, `refused()` and `reset()` are conditions on its state, each named for
its event, and `received` is its history. `next_data(until=None, *, size=None, start=None)` is the
next-event condition, its boundary the end of `received` at the call. Every one of them raises
`ConnectionEnded` once it can no longer hold, a connection refused while the test waits for it to
be established among them, so that a RST shows in the wait at once, not as a timeout.

**Rejected:** a host socket the test connects to, relayed to the device, because it brings wall time
back as a bridge does; retransmission and a persist timer, because the link loses nothing and
timers would add turns that change nothing (for the SYN, #289 found that the device drops it, and
superseded this); a client that opens from another address per
connection, because one address and fresh ports already keep connections apart. Also rejected:
conditions that return `None` for ever once their event cannot happen, which turn a refused
connection into a timeout that names the wrong cause.

**Rejected:** leaving a reset to the test, documented as a limit (wait for
`network.dhcp.next_lease()` after `board.reset()`, and expect the connections from before it to
wait out their time), because a reset is a step the tests take, and the issue's rule is that what
cannot happen fails at once; and ending the device's own connections at its reset too, which a
network cannot know of and which would publish an MQTT Will that a real broker publishes only at
its keepalive. Also rejected: recording what the test writes in the transcript at `write`, as
`_Traced` records a service's writes, because a connection's first bytes are written before the
handshake, and a refused connection would show bytes that never reached the device; the test's
side records each piece as it goes.

**Rejected:** `next_data` reading from a cursor that moves as the test reads, because polling a
condition must consume nothing; and without `start`, because the bytes after a head that one
segment brought with it are already in `received` when the head's condition returns, and a new
condition's boundary would leave them out. `start` is an offset into the bytes received, the
stream's own coordinate, not the `since=` on `next_message` that #182 rejected: a message list has
no positions a test should count, a byte stream has nothing else.

## The test's TCP sends a SYN the device does not answer again, on the board's clock

Status: decided (#289)

A connection the test opened when `network.dhcp.next_lease()` held stayed `connecting` for good. The
decision of #196 assumed that a link that loses nothing needs no retransmission. The device's stack
drops the SYN, though, not the link. ESP-IDF's DHCP client takes a leased address only after its ARP
check (`CONFIG_LWIP_DHCP_DOES_ARP_CHECK`, the default). Until then lwIP's `ip4_input` drops a
segment to that address without answering. Measured on `http_server` (ESP-IDF v5.3.6, the esp32sim
pin of the time): DHCP acknowledged at 1.031 s, the station took the address at 2.027 s and the
server listened 0.6 ms later. A SYN at 1.032 s and SYNs up to 1.832 s got nothing, on port 80 and on
a port nothing listens on. From 2.032 s on, port 80 answered with a SYN-ACK and the other port with
a RST. Arduino-ESP32 3.3's `WebServer`, in `arduino_shell`, behaves the same: ACK at 0.956 s,
address taken and `WebServer` listening between 1.938 and 1.943 s. So the SYN of a connection the
test opened goes again, as a real client's does: 1 s after the first (RFC 6298's initial RTO), each
time twice as long after the last, 6 times (Linux's default `tcp_syn_retries`). 64 s after the
seventh SYN, 127 s after the first, the connection ends in a state of its own, `"timed out"`, with
the condition `timed_out()`. The name is apart from `"unanswered"`, which is the end of a device
connection whose SYN-ACK got no ACK. While the connection is still connecting, a wait on any of its
conditions that times out says so, as `the SYNs at 1 s and 2 s got no answer (no SYN-ACK, no RST),
and the SYN at 4 s has just gone`: a SYN sent again says that the one before got no answer in its
RTO, while the last one has only had no answer so far, and may have just gone. The waits of the HTTP
client say so too, through the `waiting` of `Condition`, and the run report lists every connection
the test opened with its state. Data segments, FINs and the device's own connections keep #196's
rules: nothing sends them again.

**Rejected:** a shorter limit than Linux's, because a test bounds its own wait with `within=`, and
a limit of the test's TCP that ends a connection sooner than a real client would only make a
second deadline. Also rejected: sending the SYN again only while the device has no address, as
the harness sees from DHCP, because the harness cannot see when the device's stack takes the
address. A real client does not know either.

Sending the SYN again covers the time before the device has its address, not the time between
the address and the server's `listen()`: a SYN then draws a RST, as a real client's does. The
firmware of #289 started its `WebServer` about 0.2 s after its address, so its SYN sent again 1 s
after the lease most likely came in that gap. For such firmware `network.accepting(port)` is a
condition for the port accepting a connection: it connects now, and again 1 s after each refusal,
on the board's clock, up to 30 connections, and returns the first one the device accepts, open,
for the test to use or close. `arduino_shell` with `listen_delay_ms` reproduces the case.

**Rejected:** connecting again after a refusal in every connection, `connect` and the HTTP
client's included, because a refusal is an answer the test may be waiting for (`refused()`), and a
real client does not connect again either. Also rejected: waiting for the firmware's console line
as the only way, because it ties the test to a line the firmware may not print, and the test
then waits on what the firmware says rather than on what its server does; and connecting again
when the test polls the condition, because polling must not act, so the retries are timers of
the network, due whether a wait polls or not. The limit of 30 connections, one a second, keeps a
condition the test stopped waiting on from connecting for the rest of the run; the connection
such a condition gets is left open, documented, rather than closed by another timer.

## A bridge from the simulated network to a real service is postponed

Status: superseded (#67)

A consumer may want its real backend at the far end, in a container or on the host, at the cost
of wall time and of repeatable runs, as with the `real` source. Nothing needed it then: the
services in the harness were enough for every test written so far. See [a bridge relays a TCP port
to a real service, and puts the run on the wall
clock](#a-bridge-relays-a-tcp-port-to-a-real-service-and-puts-the-run-on-the-wall-clock).

## A bridge relays a TCP port to a real service, and puts the run on the wall clock

Status: decided (#67)

`network.bridge(port, to)` makes the service at a host address the far end of one TCP port of a
simulated network, behind the modem or the access point. The harness's own TCP still ends the
device's connection; the bridge opens one connection to `to` for each, and relays the stream both
ways, bytes as they are, TLS included. Threads connect, read and write, so that no turn waits on
the service, and each turn hands over what came since the last one, stamped with the turn's time,
renewing its turn every emulated millisecond while a connection is open, as the real source does.
The service lives in wall time, so a bridge puts the whole run on the wall clock from power-on,
under the rules of a run with a real source: given before `board.power_on()`, refused in front of
a replay given before it. It may take the place of the broker on 1883 and the HTTP server on 80,
since unmodified firmware talks to the ports it was built with; any other port must be free. Built
at the owner's request, before a consumer's test named the need.

**Rejected:** holding the emulated clock while a request waits for the service, because the
service also sends unprompted, as a broker does, and the firmware's own timeouts would run on in
emulated time against an answer that takes wall time. Also rejected: connecting to the service
before the harness accepts the device's connection, so that a refusal reaches the device as a
reset, because the accept would wait on the host in a turn or need a TCP that accepts later than
the SYN; the device sees its connection accepted and closed instead, and the transcript says why.
And ending TLS at the bridge, because the device would then have to trust the test's CA in front
of a service that has a certificate of its own. UDP is not bridged: DNS, NTP and DHCP stay the
network's, which every test so far needs repeatable, and no consumer has a UDP service of its own.

## A TLS stack with a seeded random source is postponed

Status: superseded (#68)

The harness's side of a TLS session took its keys and nonces from the system, and the determinism
job compared what crossed the link in plain text, without the times after a handshake. See
[The harness's TLS server draws every random value from a seed](#the-harnesss-tls-server-draws-every-random-value-from-a-seed).

## The harness's TLS server draws every random value from a seed

Status: decided (#68)

`esp32tl/net/tls.py` is a TLS 1.2 server of its own on `cryptography`'s primitives, in place of
Python's `ssl`. The CA's and the server's RSA-2048 keys come from primes found in a SHAKE-256 stream
named after the CA's number in the test; the server random and the ephemeral ECDHE key of each
connection from a stream named after the CA and the connection's number; AES-GCM's explicit nonce is
the record's sequence number. Two runs put the same bytes on the wire, and the determinism job
compares every time exactly, the position tracker's TLS session over PPP included.

Measured on the position tracker over the modem with Python's `ssl` (two runs on an M2 Pro): the
runs were identical up to the server's first flight, then 1.25 ms apart at `tls: handshake done`,
1.77 ms at most and 1.42 ms for the rest of the run. Two causes, both in the server's bytes. PPP
escapes control characters, so random bytes take a random number of UART bytes: the server's first
flight was 2645 bytes on the UART in one run and 2653 in the other, and the device's next frame came
0.694 ms later, the time of 8 bytes at 115200 baud. Then another 0.555 ms appeared while the device
worked on the server's keys and signature before its key exchange, with its own frames the same
length in both runs: its arithmetic on the server's fresh values. Every encrypted record after the
handshake moved the times again (6 UART bytes more, 0.521 ms later; 4 fewer, 0.347 ms sooner). With
the seeded server, the two runs' modem taps, network transcripts and consoles are identical.

What it costs: 723 lines in place of 258, a handshake checked against mbedTLS (the position
tracker) and OpenSSL (`tests/test_net_tls.py`, every suite and group), and about 0.4 s of wall
time per key on an M2 Pro (0.25 to 0.67 s over ten keys) the first time a process needs it: two
keys per CA number, so about 1.6 s for a session whose tests make two CAs at most.
What it serves is narrower than OpenSSL: TLS 1.2 only, ECDHE-RSA with AES-128-GCM, AES-256-GCM or
ChaCha20-Poly1305, groups x25519, secp256r1 and secp384r1, RSA PKCS #1 v1.5 signatures, no
session resumption. A firmware that needs more (TLS 1.3, CBC suites, an ECDSA certificate) brings
it.

**Rejected:** seeding OpenSSL's random source under Python's `ssl`, because Python has no call for
it, and replacing libcrypto's random method through `ctypes` is process-wide and depends on which
OpenSSL `_ssl` links; fixed ephemeral keys under Python's `ssl`, because it has no call for those
either, and the server random alone changes every key of the session, so the ciphertext and its PPP
escapes would still differ; `tlslite-ng`, because it takes its randomness from `os.urandom` in
module after module, which only patching could seed, and is a dependency for one module; and
comparing the plain text without the times after a handshake, what the determinism job did, because
it leaves a change of timing after a handshake on the device unseen.

## Flash contents besides the build go into a partition of the build's own table, by name

Status: decided (#64)

`board.nvs({namespace: {key: value}})` writes an NVS partition, named `nvs` unless the call names
another, from the partition table of the build under test, before `board.power_on()`. Strings are
`string`, bytes `binary`, and integers are spelled with their width, `u8()`, `u16()`, `u32()`,
`i32()`, since the firmware reads them by type. The image is made with ESP-IDF's own generator,
`esp-idf-nvs-partition-gen`. Other formats, a FAT or LittleFS image from a directory, come when a
test needs one. Where a firmware keeps its configuration, and what it is, is the consumer's: on
the spike, this one call wrote the production release image's credentials, broker and CA.

**Rejected:** helpers that know a product's configuration, because that is product knowledge;
and an offset in the test, because it repeats the partition table and breaks when it changes.

## The LilyGO profile takes its GNSS route, flash and PSRAM from LilyGO's documentation

Status: decided (#64)

*Superseded in one part, what reaches the runner: the backend now passes `flash_mb` too, see [the
runner's flash is the profile's
flash_mb](#the-runners-flash-is-the-profiles-flash_mb-else-the-builds-headers-refused-at-setup-when-too-small).
The profile's values stand.*

The profile gets `[uart.gnss]`, UART2 with TX on GPIO 21 and RX on GPIO 22 at 9600 baud,
`flash_mb = 4` and `psram_mb = 4`; the backend passes `psram_mb` to the runner as `--psram-mb`.
LilyGO's [pin table for the T-A7670X ESP32](https://github.com/Xinyuan-LilyGO/LilyGO-T-A76XX/blob/63af963038e93c91baf39d89a3a736d6bc26ff13/docs/en/esp32/a7670-esp32/README.MD)
gives GNSS TX and RX on GPIO 21 and 22 for the A7670G with GPS, whose L76K receiver is an external
module, and its
[`ExternalGPS_A7670G_Only`](https://github.com/Xinyuan-LilyGO/LilyGO-T-A76XX/blob/63af963038e93c91baf39d89a3a736d6bc26ff13/examples/ExternalGPS_A7670G_Only/ExternalGPS_A7670G_Only.ino)
example opens it as `Serial2` at 9600 baud. The same document's product table gives an
ESP32-WROVER-E with 4 MB of flash and 8 MB of PSRAM, of which the emulator models 4, the most the
runner accepts. Firmware for the board may turn PSRAM on, as the production release image of
[stage 0](../stage0-findings.md#inputs) does, and without PSRAM such an image runs degraded and logs
`PSRAM not found`. `test_the_lilygo_profile_holds_the_specification_and_lilygo_s_documentation`
compares the file's data whole, as the test it replaces did.

**Rejected:** waiting for the vendor schematic, as plan finding 12 did, because LilyGO's pin table
and example already give these values.

## The LilyGO SD slot and additional controls declare wiring without new model effects

Status: decided (#100)

The profile adds SPI SD on GPIO14/15/2/13 (SCLK/MOSI/MISO/CS), modem DTR on 25 and reset on 5,
GNSS wake on 19 and PPS on 23, and board peripheral power enable on 12. The
[manufacturer audit](../features/board-profiles-technical.md#lilygo-wiring-audit) pins the sources,
resolves the README's contradictory SD CS warning against its pin table, example and schematic,
and records the unresolved R2-to-PCB revision mapping. The existing UART routes, PWRKEY timing,
RI pull-down and 4 MB emulated PSRAM stay as they were; the physical module has 8 MB PSRAM.

**Rejected:** leaving the slot out while the schema and runner already support it; copying the
README warning's GPIO2 CS assignment instead of the consistent wiring sources; inferring pulls,
reset timing or polarity from a pin name or one example's startup sequence.

**Postponed:** DTR sleep, reset, GNSS wake/standby, PPS generation and supply gating. These pins
are named GPIOs, accessible through `Lines`, with no automatic peripheral-state effects. Only
the existing modem PWRKEY observer and RI driver have model behavior. Electrical behavior must
be verified for the supported hardware revision before adding these effects.

## The board's durations are Durations, and an expect's timeout also takes seconds

Status: decided (#69)

`board.wait(duration)` and `wait_until`'s `within` take a `Duration` of `esp32tl.units`, 0 or more,
and `board.now` is one, as a console line's `at` and a `LineEvent`'s `t` are. A plain number raises
`TypeError` with the call that would say it, `board.wait takes a Duration, as seconds(2.5), not
2.5`. An expect's `timeout` takes a `Duration` or a number of seconds: pytest-embedded's `expect`
takes seconds, and its default of 30 and the tests written against it keep working.

**Rejected:** numbers of seconds beside `Duration`s in `wait` and `wait_until`, because a number
would leave its unit to the reader, which [the units decision](#models-describe-the-world-in-its-own-terms-in-units)
rules out; and a `Duration` only, in an expect too, because a test ported from pytest-embedded
would fail on its first `timeout=`.

## Headings are a type of their own, and positions are on a sphere

Status: decided (#69)

A heading is a `Heading`, degrees clockwise from true north taken modulo 360, with the eight
compass points as constants (`NORTH`, `SOUTH_WEST`), so that `move(heading=..., speed=...)` takes
no plain number. Positions are on a sphere of the Earth's mean radius, 6371008.8 m:
`Position.moved` follows the great circle that leaves on the heading, `distance_to` is the
haversine, and the GNSS model's course is the great circle's azimuth where the receiver is. The
sphere's distances differ from the WGS 84 ellipsoid's geodesics by less than 0.6 %, 6 m in a
kilometre at most, and are close to what firmware that computes a distance with the haversine gets.

**Rejected:** a heading as a number of degrees, because it is the one quantity of the model that
would have no unit; a rhumb line, a constant compass course, because the plan asks for great-circle
movement and a receiver reports the course it is on; and the ellipsoid's geodesics, because they
need an iterative solution or a library for a precision that no test's tolerance yet asks for.

## The GNSS receiver's epochs fall on whole seconds of the world's time

Status: decided (#69)

The receiver sends an RMC and then a GGA at every whole second of the world's time, the first one
at least a second after power-on; with the default world's time, on a whole second, that is 1 s,
2 s, 3 s of the board's time. A change the test makes at an epoch's instant is in that epoch,
since the link polls the receiver after the test's turn. A silent receiver bounds no turn, and
`resume` moves its next epoch past the silence, so the epochs of the silence are not sent; one at
the instant of `resume` is.

**Rejected:** epochs on whole seconds of the board's time whatever the world's time says, because
a receiver's epochs are on whole seconds of UTC and its sentences would then carry fractions of a
second; and a change held to the next epoch, because the receiver would need to know whether the
link had polled it at that instant already.

## A lost fix keeps the receiver moving, and a fix puts it at a place, standing

Status: decided (#69)

`gnss.lose_fix()` takes the fix away and nothing else: the receiver goes on moving as it did, as
one in a tunnel does, and `gnss.position` says where it is. `gnss.fix(at=position)` puts it at
`position`, standing, so that one call says where the receiver is and what it does; a test that
wants the fix back on a moving receiver says `gnss.fix(at=gnss.position)` and `gnss.move(...)`.
`move` needs a position from an earlier fix, and `degrade` a fix.

**Rejected:** stopping the receiver at `lose_fix`, as the spike did, because the world does not
stop when the receiver loses its satellites; and a `fix` that keeps the motion it had, because
`fix(at=...)` would then not say alone where the receiver is a second later.

## Console lines are stamped when they end, and console.line looks from when it was made

Status: decided (#69)

`console.line(pattern)` and `console.lines(...)` read the console as lines, each stamped with the
board's time of the `console` message that carried its line ending, without the line ending and
without ESP-IDF's colour codes; a line not ended is not one yet. `console.line` is a condition for
`board.wait_until`: the first matching line among those that end after it was made, by count, not
by time, so that a line that ends at the very instant the condition is made is not taken for one
before it. Neither moves `expect`'s cursor.

**Rejected:** a line's start as its time, because a line cannot be matched before it ends; lines
from a time rather than from a count, for the instant above; and lines that share `expect`'s
cursor, because a test that mixes the two would have one consume what the other waits for.

## The console's expect returns a match on bytes, and line returns text

Status: decided (#198)

`console.expect` and `console.expect_exact` return an `re.Match` on bytes, for a pattern given as
text too, and `console.line` and `console.lines` a `Line`, whose text and groups are `str`. A
first consumer wrote `match.group(1).decode()` after `expect` and did not expect to. The two stay
as they are, and the documentation says so where each is described: `expect` keeps
pytest-embedded's names, search and return values, whose `Dut.expect` hands the pattern to pexpect
on a bytes buffer and so returns a match on bytes for a `str` pattern as well; a test written for
pytest-embedded, and ESP-IDF's own tests, decode its groups. `line` is esp32tl's, reads the console
as text with ESP-IDF's colour codes removed, and is the way to get a value as text. Both backends
share the one `Console`, so the serial backend returns the same.

**Rejected:** matching a `str` pattern on text and returning a `str` match, as `re` does, because a
test moved from pytest-embedded would then get `str` where it decodes bytes and fail with an
`AttributeError`, and because a console is bytes that need not be UTF-8 up to the next match,
which a text match would have to decode or refuse. A second `expect_text`, because `line` already
gives the text, with the line's time.

## The GNSS receiver sends RMC and GGA only, at sea level

Status: postponed (#69)

The model sends no GSA, GSV or VTG, has no altitude (the GGA says `0.0`), talks only as `GP`, at
one epoch a second, and ignores what the firmware sends it. Each comes when a firmware under test
reads it, or configures its receiver and waits for an answer.

## GPIO lines are an interface of the link, and the scenario timeline is gone

Status: decided (#24)

The `lines` fixture is one `Lines`, a `BoardLink` `Interface` without a UART, for every
`[lines.<role>]` set of the profile; it sends `gpio_in` and `release` from `inputs(now)` in the
link's turns, and only on its own pins. `BoardLink`'s `Timeline`, which plan M3's `scenario`
fixture was to fill, is removed with the link's `timeline=` argument: a test reaches an instant by
waiting on the board's clock, then drives the line there, so `lines.drive` takes no `at=` and no
input reaches the link but from an interface. Two interfaces on one line are refused, and so is an
input on a pin of another interface's, so a model that drives a profile's line, the modem's RI
(#29), cannot be an interface beside `Lines`: either `Lines` takes inputs scheduled ahead from the
model, with a `quiet_until` of its own, or the backend leaves the model's lines out of `Lines`.
#29 uses the first arrangement: see
[the modem drives RI through Lines](#the-modem-drives-ri-through-lines-and-observes-pwrkey-through-it).

**Rejected:** keeping `Timeline` for the lines, because it would be a second path for inputs that
one interface uses, and its errors would name a scenario that no longer exists; and `at=` on
`drive`, because it would be a second model of time beside the test's clock, which the decision
that [the board's clock is the test's](#the-boards-clock-is-the-tests-emulated-time-moves-only-while-the-test-waits)
rules out.

## A board's pull is a level the harness drives on the line

Status: decided (#24)

A line the profile pulls, `ri = { pin = 33, pull = "down" }`, goes on the link as a `gpio_in` at
the pull's level in the run's first turn, at `t = 0`, and again in place of each `release` of it.
The chip's own `release` puts a pad back to the pulls the firmware set in IO_MUX, which know
nothing of a resistor on the board, so a `release` of RI would read 1, the level stage 0 found
([gap 6](../stage0-findings.md#gaps)). The pull holds in every test on the profile, also one
that never asks for `lines`, because it is the board's wiring, not something a test sets up.
Without it, `gpio_echo` on the LilyGO profile prints `in=1` at boot; with it, `in=0`.

**Rejected:** the pull in the runner, because the runner does not read the board profile (the
`board` of the harness's `hello` is a name it does not interpret), and it would need a new message;
and the pull in esp32sim, because the harness gives it with the messages of version 1, and an
upstream change is asked for only once it is the only way. The cost accepted: the release of a
pulled line that the firmware drives as an output brings an `input_on_output` warning, which a
`release` would not.

## The lines fixture names a line by its GPIO or its role, and reads the firmware's side

Status: decided (#24)

`lines.drive`, `release` and `level` take a line as its GPIO number, `33`, or as
`<role>.<line>`, `modem.ri`, as the profile names it. `lines.level(line)` is what the firmware
drives the line to, or `None` while its output is off; what the test drives is the test's own, and
`lines.events()` holds both ends' changes, `by` `"firmware"` or `"test"`, with the board's time.
A line counts as the firmware's while its bit is in `gpio_out`'s `enabled` or its pad is reported
high.

**Rejected:** a fixture per set (`modem_lines`), because the sets are the profile's data and one
fixture covers them all; and a level of the wire that folds the test's drive and the board's pull
in, because a test that waits for the firmware to raise a line would then return on its own drive.

## The lines declare no latency, and a wait sees a line's change at its next look

Status: decided (#24)

`Lines` declares no `latency` and no `quiet_until`, so a `gpio_out` on a line takes no turn of its
own: `board.wait_until(lambda: lines.level(...) == 1, ...)` returns at the next turn the link takes
after the change, at most 100 emulated milliseconds later, and `lines.events()` carries the
change's own time. In `gpio_echo`, PWRKEY follows RI 8.8 emulated microseconds after it.

**Rejected:** a `latency` of 0, or of a microsecond, so that the wait would return at the change,
because the link's `latency` is the minimum over its interfaces and bounds every event of the run,
the UARTs' too: `hello_world` runs at 30 wall seconds per emulated second with 0, 0.3 with 1 µs and
0.1 with none ([protocol 6.1](../protocol.md#61-how-the-runner-meets-the-block-point)).

## Whether a backend can drive a board's lines is not declared yet

Status: postponed (#25)

`capabilities.toml` gives no source on `lines.*`, and `lines.drive` asks the declaration nothing:
on `esp32sim` the test drives the line itself. Whether a real board's lines can be driven from the
host, and how a test that drives them is skipped where they cannot, comes with the `serial`
backend.

## An SD card is inserted by the test, as a model source holding an image file

Status: decided (#32)

A board profile has at most one `[sd_card]` slot, with its four SPI pins, and the interface is
`sd_card`, with no role. `sd_card.insert(image)` before `board.power_on()` is a `model` source
(and, since #106, also while the firmware runs:
[the card comes and goes at the board's current instant](#the-test-takes-the-sd-card-out-and-puts-one-in-at-the-boards-current-instant)):
the runner's card model holding that file, which writes through, so the test reads what the
firmware wrote with the same tools it prepared the image with, mtools, as the specification says.
On `serial` the slot takes only `real`, the card in it. An empty slot is a board without a card.

**Rejected:** a copy of the image for the run, because the test would then need a second call to
get the result, and copying first is one line of the test's when it wants the original kept;
initially FAT helpers in esp32tl, because mtools already makes and reads images (this part is
superseded by [library-owned image builders](#board-owns-interfaces-and-sd-image-builders)); and naming the slot `[spi.<role>]`, because a profile
names what is at the end of the wires, and the card is not any SPI device.

Since #132 the builder takes `size_mb` up to 1 TiB and `cluster_kb`, and `insert` stays at 2 TiB:
#132 saw an assertion in mformat on nearly 2 TiB with `-c 128`, which later runs have not reproduced
([what was measured](../features/pytest-plugin-technical.md#shared-board-and-fat-image-api)), and a
test that needs more can still format its own image. A cluster the card's FAT32 cannot have is refused at `image()`, rather than
left to a firmware that would fail to mount. This decision stands for a card from
[the card cache](#prepared-cards-are-kept-in-a-cache-of-the-tests-choosing): the image in the slot
writes through, and a clone is the test's explicit choice.

## Prepared cards are kept in a cache of the test's choosing

Status: decided (#132)

Firmware that prepares a card on its first mount took 56 s of board time and 20 s of wall time on a
2048 MiB card, on every fresh card. The session fixture `card_cache` keeps a card a test prepared,
in `config.cache`, keyed by the test's name for it and the app image's hash. `get` returns a clone
in the test's directory or `None`, and `put` keeps a copy. A clone is the file system's where it
can (APFS, reflink on Linux) and otherwise a copy that skips zeros. The limit,
`esp32tl_card_cache_mib`, is 4096 by default, the tool's choice with no source behind it, counted
from `st_blocks`, and evicts the card least recently used. Writes are atomic. On a miss the test has
one boot more, which a test that counts boots allows for.

**Rejected:** preparing the card on a separate board in a session fixture, for the cost of one more
run in every session, and because no such board could be the test's; relying on test order, so that
an earlier test prepares what a later one reads, for the dependency it creates between tests;
`os.link` rather than a clone, because writes would reach the cache; a limit by the files' size,
because a 32 GiB card takes 8 MiB; a key without the application, because a new build may prepare
the card differently; and a function-scoped fixture, because the limit and the directory are the
session's, though the operations do work on the running test's board.

## The test makes the SD card fail, slow down or only be watched with sd_card.fail, sd_card.delay and sd_card.observe

Status: decided (#107), observe added (#260)

`sd_card.fail(op, *, count=None, blocks=None)` and `sd_card.delay(op, by, *, count=None,
blocks=None)` give the runner's card a rule from the board's current instant on, before power-on
from the first instruction; each returns a `CardFault` with `hits`, `active` and `clear()`, and
`sd_card.clear_faults()` removes them all. `op` is `read` or `write`, and for `delay` also `erase`;
`by` is a `Duration`, at most 10 s; `count` is how many operations, `None` every one until cleared;
`blocks` a block number or a `range`. What the rules did is `sd_card.faulted`, each a
`FaultedOperation` with its time on the board's clock, its blocks and whether the card wrote the
image. A rule needs a card in the slot when it is given, and stays through a change of card; on `serial` the test never gets that far, because
`insert` skips there. The slot is on the link as the interface whose rules go out as `sd_fault`
and `sd_clear` and whose `sd_op`s come back.

`sd_card.observe(op, *, count=None, blocks=None)` (#260), `op` `read`, `write` or `erase`, is a
rule of the same kind whose action, `observe`, changes nothing: the firmware sees the card, its
answers and their timing as without it, and its `hits` are the operations it met. It goes on the
link as an `sd_fault` with `action: "observe"` and no `delay`, and comes back as `sd_op` with the
same action. An operation meets every rule that observes it beside the one rule that fails or
delays it, the one given last of those: each rule gets a hit of its own and counts it, in the order
the test gave them. So a rule that observes never takes an operation away from one that acts on it,
which would change what the firmware sees, and the report and the run log say `observed`.

**Rejected:** a callback the harness runs on each block operation, because the card answers within
the transaction, and a callback a turn away could not; one `fault(op, action, ...)` with the
action as a string, because two methods say what they do and check their own arguments; a count
of 1 by default, because `fail("write")` reads as every write; and block offsets in bytes,
because the card and its driver count in blocks. For watching (#260): `delay(op, Duration(1))`
and its hits, which the test used before, because it keeps the card busy 1 ns longer and reads as
a delay; the names `watch` and `record`, because `observe` is the action on the wire and in the
report as well; letting a rule that observes take part in "the one given last applies", because
then one given after a `fail` would stop the failure; and giving an operation's hit to the first
rule alone, because each rule's `hits` and `count` would then depend on the others.

## Inserting and removing the SD card while the firmware runs is postponed

Status: superseded (#106)

The specification puts the card's insertion and removal on the scenario timeline. The card was in
from power-on to the end of the run, and a removal was taken to need the slot's card-detect pin,
which no profile has. It does not: a firmware learns of a removal from its I/O errors, as on a
board without the line. See
[the test takes the SD card out and puts one in at the board's current instant](#the-test-takes-the-sd-card-out-and-puts-one-in-at-the-boards-current-instant).

## The test takes the SD card out and puts one in at the board's current instant

Status: decided (#106)

`sd_card.remove()` and `sd_card.insert(image)` with the board on change the card at `board.now`,
as `wifi.access_point_off()` switches the access point: the slot queues an `sd_card` input for
the link's next turn, the runner applies it at the first slice boundary at or after that instant,
between two SPI transactions, and acknowledges it with `sd_card_ack`, which the slot keeps in
`changes` and fails the link on when the runner refused the card or names another image. Both
calls return at once. While the test does not wait the firmware does not run, so the image does
not change under the test's reads, and the runner closes a removed image before the firmware runs
again: the test may change or delete it at once. Every insertion checks the image and its owners
as before power-on; a removal gives the image back, since after power-on no route can claim it.
A card built after power-on is allowed, for the card to put in.

**Rejected:** `remove(at=...)` and other future-dated changes, because the test reaches an
instant by waiting for it, as for every other interface; a `remove()` that blocks until the runner
has acknowledged it, because that needs the clock to move, and a test that has not waited has not
let the firmware run; finishing the write in progress on removal, because a test of what the
firmware does with a pulled card must see what was on the card at that instant; and requiring a
card-detect line, because the firmware sees the removal through its I/O errors anyway.

## The backend always gives the runner the profile's SD card slot

Status: decided (#106)

On a profile with an `[sd_card]` slot the backend passes `--sd-spi` at power-on whether or not a
card is in it, and `SdCard` is on the link, so that a test can put a card into a slot that was
empty at power-on. An empty slot answers every byte with `0xff`, as no slot does, so the firmware
of a test that never touches the slot sees nothing new; the runner's stderr gains its slot line
and its `SD card:` count.

**Rejected:** passing `--sd-spi` only when the test asked for `sd_card` before power-on, because a
test that takes the fixture later could then not insert a card, and the difference would show only
there.

## The SD card slot has no card-detect line yet

Status: postponed (#106)

`[sd_card]` takes no `cd` key, and the slot drives no line when its card comes and goes: no
profile of esp32tl's has a card-detect pin wired, and a firmware without one learns of a removal
from its I/O errors. A `cd` key comes with a board that has the line and a firmware that reads
it; the slot would then drive it through `Lines` at the same instant as the change.

## The SD card tests skip without mtools, and CI makes sure it is there

Status: decided (#32)

`tests/test_sdspi.py` carries `requires_mtools`, which skips it where `mformat`, `mcopy`, `mdir`,
`mtype` or `mmd` is missing, as `requires_docker` does for Docker. The CI test job installs mtools on
macOS and fails on a runner without it, so that the tests cannot skip there unnoticed.

**Rejected:** failing the tests without mtools, because a checkout without it could run nothing
else of the suite cleanly; and a FAT library in Python as a dependency, because the
specification names ordinary tools, and a user's test uses the same ones.

## The esp32tl command takes no SD card yet

Status: postponed (#32)

`esp32tl run` and `esp32tl replay` pass the runner no `--sd-rw`: the card is for tests now, and
a run by hand can call the runner with `--sd-rw` and `--sd-spi` itself
([runner](../features/runner-user.md#put-an-sd-card-in)). An option comes when someone runs a card
outside pytest.

## The Bluetooth peer runs Bumble in the harness, on the board's clock

Status: decided (#33)

A Bluetooth peer is a [Bumble](https://github.com/google/bumble) device on Bumble's virtual link,
in the harness's process, and its controllers, hosts and timers run on an asyncio loop whose time
is the board's: the harness moves it from one instant something is due at to the next, as the link
goes from turn to turn, and the loop never waits on the host's clock. What the link carries
arrives at the instant it is sent, so discovery, pairing and connection take no time of the
board's; only what something waits for does, a model's answer at its time, a Bumble timer, an
inquiry's length. Two runs then exchange the same bytes at the same instants. Measured on
2026-11-01: an inquiry, legacy pairing, encryption, SDP and an RFCOMM exchange with 1.05 s of the
board's time in it took about 20 ms of wall time
(`test_the_device_finds_the_peer_by_its_name_and_its_answer_comes_on_the_boards_clock`).

**Rejected:** Bumble on asyncio's own loop, because its timers and the peer's answers would then
fall in wall time, and runs would not repeat; Root Canal, Android's virtual controller, for the
peer, because it is a separate binary that keeps its own time in wall time, so the test could not
hold it, and it would have to be built or downloaded. Which controller the ESP32's side talks to was
#34's to choose; #34 chose the board socket, with no HCI transport in esp32sim
([decision](runner.md#the-guests-hci-goes-over-the-board-socket-on-the-boards-clock)). Also
rejected: a delay per packet on the link, because no test needs one and any value would be invented.

## The air's timers are put on the nanosecond, not judged by the selector

Status: decided (#142)

`BoardLoop.call_at` rounds every timer's `when` to the nearest nanosecond, the float `time()` gives
there, and `_clock_resolution` is half a nanosecond. A timer due at the instant then leaves asyncio
no wait at all, and one at the next nanosecond is never fired early, so what a timer sets off runs
at its instant wherever the sum of floats behind it lands.

**Rejected:** a selector that takes a timeout under 1 ns as 0, the direction the issue's probe
showed, because it leaves two judgements of one float: the selector's `when - time()` against
asyncio's `when < time() + _clock_resolution`. Where they disagree, a timer the selector counts as
due that asyncio does not fire spins the loop at one instant, and one asyncio fires that the selector
counts as later makes its `call_soon` late again. Only the timer's time on the nanosecond grid
gives both the same answer. The grid holds up to 2^22 s, about 48 days of the air's time, where
`round(when * 1e9)` stops finding a timer's nanosecond; from 2^23 s a float of seconds no longer
tells nanoseconds apart and the loop can spin. No test goes that far, so nothing refuses a time
past it.

## esp32tl adds inquiry, legacy pairing, key checks and encryption to Bumble's virtual controller

Status: decided (#33)

Bumble 0.0.236's `Controller` on a `LocalLink` answers `HCI_Inquiry` with
`UNKNOWN_HCI_COMMAND_ERROR`, has no legacy pairing with a PIN (it goes to Secure Simple Pairing
whatever the hosts support), takes a stored link key without asking the peer to prove it, and has
no encryption on a Classic link. A peer has to be discovered by its name and pair with a PIN, as
Bluetooth 2.0 SPP devices do, so `esp32tl/bluetooth/controller.py` subclasses Bumble's controller
and adds them, in the order the specification gives the HCI events, for links whose controllers
are all of that class. Its keys and responses are SHA-256 over the specification's inputs instead
of SAFER+: a PIN that differs still gives another key and fails the check, which is what a test
sees, and no key leaves the link but through the hosts. The peer pairs with a PIN only, as the
devices it stands for do.

**Rejected:** contributing the additions to Bumble first, because they are proven only by these
tests so far, and with stand-in keys they are not what Bumble would take; Bumble's controller as
it is, because the peer could then be neither discovered nor paired with a PIN; and the
specification's E1, E21 and E22, because SAFER+ would be code nobody outside the link can tell
from the stand-in.

Bumble also has neither a Classic page timeout nor link supervision: a page nobody answers never
ends, and a link never falls. (#129) The controller adds both, with the defaults and the commands
of Bluetooth Core 5.4 (Vol 4, Part E, 7.3.16 and 7.3.42), so that a host's values take effect. No
packet goes round on this link unless something sends one, so supervision does not watch for
packets: contact is lost when a device leaves, and the timer starts there.

**Rejected:** a poll on the link that the timer watches, because it would add instants and packets
to every run and change nothing a test sees.

## A model answers the SPP channel as the far end of a UART does

Status: decided (#33)

What answers the bytes on a peer's SPP channel is a `Source`, the protocol of the far end of a
UART (`esp32tl/interfaces/uart.py`): `receive(t, data)` returns the answers with their times,
`poll(now)` what it sends unprompted, `quiet_until()` when it next has something. A replay, the
matching engine (#20) and the models of devices that sit on either a UART or SPP then fit both.
What the source sends while no device holds the channel is lost and counted, as on the air. The
peer checks what the source returns as `BoardLink` checks a UART's source, with messages that name
the peer.

**Rejected:** a callback that returns the answer's bytes, because an answer would then have no
time of its own, and a delay the model chooses is part of what it models; and queueing what the
source sends until a channel opens, because a device on the air does not keep what nobody
received.

## The SPP peer opens its channel to one device, on a link that device authenticated

Status: decided (#33, #34)

A second device that asks for the channel while one holds it, or has been given it, is refused,
as a device with one serial port refuses it. Unless the test says `secure=False`, the channel
opens only on a link the device has authenticated, with the PIN or a key, which the peer checks
when the device opens the channel, at its RFCOMM SABM; who holds the channel is decided earlier,
when the device asks for it, at its PN. The peer does not start the pairing itself. A firmware
that never pairs then fails against the peer, as it would against a device that requires pairing,
instead of passing. (#34) The check was at PN until `bt_spp_initiator` showed Bluedroid's order as
initiator: PN, then authentication, then SABM, so a device that would have authenticated was
refused. Encryption is not required: what a firmware that never pairs lacks is the authentication,
and `bt_spp_initiator`, which asks for `ESP_SPP_SEC_AUTHENTICATE`, sent `Authentication_Requested`,
a negative link-key reply and the PIN, and no `Set_Connection_Encryption`: the link stayed
unencrypted (seen on 2026-11-08, ESP-IDF v5.3.6), so requiring encryption would fail firmware that a
real device without Secure Simple Pairing accepts.

**Rejected:** the check at PN, for the reason above; requiring encryption, for the reason above;
giving the channel to the last device
that asked, because the source's answers to
the first device would go to the second; opening to any device by default, because a firmware
that skips pairing would pass a test that a real device fails; and the peer starting
authentication itself when the device asks for the channel, or when the link comes up (security
mode 3), because Bumble's RFCOMM acceptor answers at once, without waiting, and authentication at
link setup would need the controller to pair before `Connection_Complete`. Until a firmware is
seen to rely on the peer starting it, the device must.

## A peer leaves without a word, keeps its keys, and its old link stays dead until it falls

Status: decided (#129)

`switch_off` and `out_of_range` look the same to a device: no `LMP_detach`, no answer to an inquiry
or a page, and each end's link falling at its supervision timeout. They differ in the source, which
gets `supply` on a power loss when it is `Supplied`, and runs on out of range, with what it sends
counted in `lost`. The peer's own end goes at once on a power loss, because nothing is left to hold
it, and at its timeout out of range. Link keys stay in both cases, and `forget_keys` is the only way
to lose them: no specification or vendor document says what a given device keeps after losing
power, so this is the model's assumption, and a test states the other case by calling it. Coming
back before the timeout does not revive the old link: the peer can be discovered again, and the
link still falls at its timeout. Delivery is refused in one place, `Link` in
`esp32tl/bluetooth/air.py`, so that the mechanism can move if #34 chooses another controller while
the API stays on the peer. A page is answered or not at the instant it starts, so a peer that
returns during a page answers only the next one, which is later than a real controller's repeated
page would find it.

**Rejected:** `remove_controller`, because `LocalLink.send_lmp_packet` then raises
`InvalidArgumentError` for the address, which fails the air, and neither end times out; resuming the
link when the peer returns in time, because it would need baseband retransmission for what was lost
meanwhile; dropping the keys on a power loss, or one call that does both, because the two are
separate facts about a device; repeating a page until its timeout, because it is the same baseband
repetition, for a difference of when a retry connects, until a firmware is seen to depend on it,
and for now also having `Controller.returned` send `LmpHostConnectionReq` again to whoever is paging
that address, which adds no instants to other runs but has not been tried, for the same reason;
and the API on the air (`air.remove(peer)`), because leaving is what a peer does, and the mechanism
under it may change.


## Devices on the air have a base class without SPP, and an RSSI of their own

Status: decided (#129)

`BluetoothDevice` is a device on the air with a name, a class of device, an RSSI, an optional PIN
and the calls by which it leaves; `SppPeer` adds the channel and the source, so a test can have a
device that offers no SPP among those an inquiry finds. RSSI replaces the fixed value of the
controller's inquiry results, and stays -50 unless given: nothing in the air depends on it, so it
says only what a firmware reads. It is a plain number that the test sets, with no model of distance
or of a value that moves by itself.

**Rejected:** a flag on `SppPeer` that turns SPP off, because a device without SPP then has to be
given a source and a channel it does not use; and tying RSSI to `out_of_range`, because no source
says at what strength a device goes out of reach.


## Secure Simple Pairing on the peer's side is postponed

Status: superseded (#34)

Peers paired with a PIN only, and the Bumble controller's own Secure Simple Pairing was used only
between two devices that both support it, which no `BluetoothDevice` did. What a firmware's stack
does with a device that has it can only be seen with firmware, so it waited for #34: see
[Secure Simple Pairing is Bumble's, with esp32tl's numbers, key types and failures](#secure-simple-pairing-is-bumbles-with-esp32tls-numbers-key-types-and-failures).

## Secure Simple Pairing is Bumble's, with esp32tl's numbers, key types and failures

Status: decided (#34)

A `BluetoothDevice` or an `SppPeer` given an `io_capability` has Secure Simple Pairing, and its
user's answers are arguments: `confirms` for a number or a pairing to allow, `passkey` for a
keyboard. The pairing itself is Bumble's controller's, which has the association models and the LMP
exchange, with what a test run needs changed in `Controller`: the nonces and the passkey come from
the controller's seeded generator, so that two runs show the same numbers, where Bumble draws them
from `secrets`; a pairing starts with a fresh state, where Bumble reuses the nonce of the one
before; the key's type is P-192's, authenticated or not by the specification's table, since the
controller has no Secure Connections, where Bumble names an authenticated P-256 key whatever the
model, which tells the host that a key Just Works made is protected from a man in the middle; only the
initiator's host gets `Authentication_Complete`, as for legacy pairing and as Vol 2, Part F, 4.2
has it, where Bumble sends both; and a DHKey check that fails is told to the peer with
`LMP_not_accepted`, where Bumble tells only its own host and the other side waits for ever. Every
pairing that ends goes into `pairings`, on the guest's controller too (`bt.pairings()`), with its
model and the number shown or typed, since the number is what a test checks and a pairing under
way is visible as `pairing`. The DHKey stays Bumble's all-zero stand-in: the link has no attacker,
and both ends are this class, as for legacy pairing's SHA-256 keys.

**Rejected:** a delegate the test writes, Bumble's `PairingDelegate`, because each test would
write the same few answers again; patching `secrets` in Bumble's module, which would change every Bumble device in
the process, the test's stand-ins included; the P-256 Diffie-Hellman exchange of the
specification, which nothing on this air can tell from the stand-in; and a peer that starts
pairing itself, as `SppPeer` does not for legacy pairing either.


## A remote name request nobody answers ends at the page timeout

Status: decided (#129)

A device in the ESP32's place can ask a peer that left for its name, and Bumble then waits for an
answer that never comes. The controller ends the request in `PAGE_TIMEOUT` at the page timeout, the
same mechanism as a page's, and the error Bluetooth Core 5.4 names for this command (Vol 1, Part F,
2.4). The issue named only the page.

**Rejected:** documenting the hang, because a test would learn of it from a test that hangs.


## A host that rejects a page gets the reason too

Status: decided (#137)

Bluetooth Core 5.4, Vol 4, Part E, 7.1.9, gives the status of `Connection_Complete` only for the
device attempting the connection, which carries the reason, and says only that the local controller
sends one to its host. Here that one carries the reason as well: it says why no link came, and a
host's answer to a page that has ended keeps `CONNECTION_TIMEOUT` for a page that went some other
way. What ESP32's controller reports only a real board shows: the `bt` fixture puts this controller
in its place (#34).

**Rejected:** `SUCCESS` on the rejecting side, because Bumble's host would take it for a link with
handle 0; and `CONNECTION_TIMEOUT`, because nothing timed out.



## Rejecting a synchronous link is postponed

Status: postponed (#34)

Vol 4, Part E, 7.1.9 also lets `Reject_Connection_Request` decline a synchronous link. Bumble's host
does not send it for one, so nothing here asks for it but a command written by hand. Whether
firmware does can be seen behind the `bt` fixture, once firmware runs there.



## A page of this side that fails leaves the entry of a page it heard

Status: decided (#137)

Two pages can cross: until #144 the entry of the page a device hears replaced the entry of its own,
which has the same address (see
[A page a device hears is kept apart from its own](#a-page-a-device-hears-is-kept-apart-from-its-own)).
Its own page used to take that entry with it when it failed, so an answer owed to
the other page found nothing, was taken as late, and the pager waited for `PAGE_TIMEOUT` although
the host had answered in time. Now the entry stays until the answer comes or a timer of link
supervision ends it. That takes each page a host hears to get one answer, or its connection accept
timeout, which ends the entry of one that gets none in time (#166,
[below](#a-page-its-host-does-not-answer-in-time-ends-at-the-connection-accept-timeout)).

**Rejected:** keeping the old behaviour and narrowing the promise that a pager hears the reason,
because a page still waiting got exactly the symptom #137 removes. Also rejected: telling a late
answer by a counter alone, set whenever a link is made as well as when a heard page's entry goes,
because an answer a host never gives then takes the answer to the next page, whose pager gets
`PAGE_TIMEOUT`.



## Keeping a heard page apart from this side's own is postponed

Status: superseded (#144)

Bumble writes a page a device hears over its own entry for that address, so who an entry belongs to
was inferred from its handle, its role and the counts of answers. Keeping heard pages apart replaces
Bumble's handling of a page heard and of its answer, which was too much to land with the rejection
of a page (#137). It landed with #144: see
[A page a device hears is kept apart from its own](#a-page-a-device-hears-is-kept-apart-from-its-own).



## A page a device hears is kept apart from its own

Status: decided (#144)

The controller keeps a page it hears in `_heard`, a dictionary of its own, and moves it to
`classic_connections` when its host accepts. Until then this side's own page to that address keeps
its entry, with the `allow_role_switch` its `Create_Connection` asked, so a rejection of the page
heard takes nothing of it, and an entry in `classic_connections` is this side's own page while
`_paging` holds the peer, and a link otherwise. Who an entry belongs to is no longer inferred from
its handle, its role and the counts of answers. The counts stay: a page tried again gets two
answers for one address, and an answer names only the address.

When one of two crossing pages is accepted, the link takes the place of this side's own page, which
ends with it, as before: Bumble's host gets one `Connection_Complete` for the address, which ends
both in its eyes. When the other side accepts this side's own page instead, the page heard goes, and
the answers owed for it are late: it can make no link, and kept, it would refuse this side's next
page after the link ended. An acceptance as the central keeps the page in `_heard`, marked
`switching`, until the pager answers the role switch, so that losing contact with the pager still
reaches it; a failure of this side's own page meanwhile ends it too, since the host cannot tell the
two apart, and a refused switch ends this side's own page for the same reason.

**Rejected:** giving the page heard to Bumble's `on_hci_accept_connection_request_command` by
writing it into `classic_connections` at the moment of the answer, because during a role switch it
would stand over this side's own page again, which is the inference this removes; and a mark on
Bumble's `Connection` saying whose entry it is, because Bumble's handler would still write the page
heard over this side's own.



## A page respects page scan, read when the page starts

Status: decided (#135)

Bumble's controller answered a page whatever the target's page scan said, though an inquiry respects
inquiry scan, so `set_connectable(False)` did not stop a device that knew the address. Now a device
whose page scan is off does not hear a page, nor a remote name request with no link to go over, and
either ends at the page timeout (Bluetooth Core 5.4, Vol 4, Part E, 7.3.18 and 6.6). Page scan is
read at the instant the page starts, as a device's presence is: one turned on during a page answers
only the next. The peer's API still has no call for it; `peer.device.set_connectable` reaches it.

**Rejected:** hearing the pages still under way when page scan comes on, for the reason a peer that
returns during a page does not hear it: it is the baseband's repeated page, a difference of when a
retry connects, until a firmware is seen to depend on it. Also rejected, for now: a call on the peer for
page scan, because no test needs one yet, and `peer.device` reaches it meanwhile.


## Connection_Accept_Timeout is postponed

Status: superseded (#166)

It landed with #166: see
[A page its host does not answer in time ends at the connection accept timeout](#a-page-its-host-does-not-answer-in-time-ends-at-the-connection-accept-timeout). What
follows is what the controller did until then.

The page's timer runs until the target's host accepts, and a target's host has no time limit (Vol 4,
Part E, 6.7, is not modelled). A host that accepts after the page ended makes a link although the
pager reported `PAGE_TIMEOUT`, and when the pager has tried again, the first answer connects the
second page. Bumble's host accepts at the instant it hears, so this takes a slowed host, and firmware
behind the `bt` fixture is the first that will be slow: what it needs can be seen there, in #166.
`HCI_Reset`, which Bluedroid sends at every start, clears the entries and the answers owed meanwhile
([below](#hci_reset-takes-the-controller-off-the-air-for-an-instant)). Until then an answer with
nothing left to answer changes nothing, and its host gets a failed `Connection_Complete` unless its
side holds an entry for that address, a link or its own page, which the event would end in the
host's eyes (#141).

**Rejected:** ignoring the late answer on the pager's side, because the target would be left holding
a link with no other end, and adding the timeout now, because nothing would exercise it but its own
test.


## A page its host does not answer in time ends at the connection accept timeout

Status: decided (#166)

Each `Connection_Request` starts a timer of `Connection_Accept_Timeout` (Bluetooth Core 5.4, Vol 4,
Part E, 6.7), 5 s unless the host writes another, up to 29 s. When it runs out before the host
answers, the controller refuses the page with `CONNECTION_ACCEPT_TIMEOUT_EXCEEDED`, which both hosts
hear, as they hear a host's rejection, and the answer is owed no longer: one that comes later gets
`UNKNOWN_CONNECTION_IDENTIFIER`, or is taken for a page the same peer made meanwhile, since an
answer names only the address. An answer counted late, whose page went before it came, ends at its
own timeout too, and the host is told. So a host that never answers holds the entry, or takes the
next page's answer, until its timeout and no longer until `HCI_Reset`. The page's own timer still
runs until the target's host accepts: with the defaults, 5 s against 5.12 s, the target's timeout
ends a page its host leaves unanswered first, and the pager reports its reason; a host that writes
a timeout longer than the pager's page timeout meets what the postponed decision described.

**Rejected:** stopping the pager's timer when the target's baseband answers, as the specification
has it, because a page whose target then left would need supervision of its own, and nothing seen
yet depends on it; keeping a late answer owed until the host gives it, because a host that never
does then takes the answer to every later page from that peer; marking the answers owed for a page
its pager gave up late when it pages again, because a host that skipped the first page would then
lose its answer to the second; and answering an acceptance after the timeout as a late answer is
answered, with `SUCCESS` and a failed `Connection_Complete`, because the host has heard that page
end already.


## Two pages at one instant make one link, the page accepted first

Status: decided (#141)

When two devices paged each other at one instant and both hosts accepted, each controller made the
page it heard its link and then completed its own page as well, so each host had two handles for one
link. Now the acceptance that runs first makes the link. A host that answers the page it heard after
the peer's host has accepted this side's own page, by making the link or by asking for the role
switch first, gives a late answer, and this side's own page makes the link, with the roles the
peer's host asked for: one handle on each side, and one central. At that instant the `LMP_accepted`
or `LMP_switch_req` is still on its way, so the controller reads the peer's controller to know, as
it does for page scan. A role switch that this side's own page does not allow would end both pages,
so then this side's answer makes the link, and the switch, which reaches that link afterwards, is
refused as belonging to a page that is over. When this side's answer asks for the central's role
too, its switch can pass where the first could not: the refusal of the first then ends nothing,
neither this side's own page nor the peer's, since the peer accepted that page, which makes the
link; the refused acceptance goes as a late answer. This is the one case where a refused switch
does not end this side's own page. When neither page allows a switch, both acceptances fail, and
there is no link.

**Rejected:** ignoring a completion when the entry already has a handle, the direction #141 gave,
because it leaves one handle but each side keeps the page it heard, so both are the peripheral, and
with a role switch on either side both can be the central.


## A role switch asked of a pager whose page ended is refused

Status: decided (#141)

A host that accepts as the central after the pager's page ended sends `LMP_switch_req` to a pager
that has no entry for it, and Bumble's handler asserted one, which failed the air. The pager now
refuses with `ROLE_CHANGE_NOT_ALLOWED`: the target's host gets `Connection_Complete` with it, and no
link comes of the acceptance. A plain acceptance at that point still makes a link, when it comes
within the target's connection accept timeout, as
[A page its host does not answer in time ends at the connection accept timeout](#a-page-its-host-does-not-answer-in-time-ends-at-the-connection-accept-timeout) says.

**Rejected:** making a link of it, because the pager would need an entry with an
`allow_role_switch` no `Create_Connection` asked for; and `CONNECTION_TIMEOUT` or
`LMP_RESPONSE_TIMEOUT`, because the pager is there and answers.


## A peer's own call that ends its link is postponed

Status: postponed (#75)

A peer leaves with `switch_off` or `out_of_range`, after which nothing is sent and each end's link
falls at its supervision timeout ([above](#a-peer-leaves-without-a-word-keeps-its-keys-and-its-old-link-stays-dead-until-it-falls)).
A peer that ends a link itself, as an adapter that drops its connection and is there again at once,
has no call on `SppPeer` or `BluetoothDevice`: the OBD-II reader's test ends it through Bumble,
with `Connection.disconnect()` on each of `peer.device.connections`, in a coroutine started on the
air. One example needs it so far; a call of the peer's own, the counterpart of the SD card's
`remove()`, waits until a second test does, so that what it ends (the RFCOMM channel or the link)
and with which reason is chosen from more than one case.

## A replayed peer is postponed

Status: postponed (#33, #34)

The `bt` fixture came with #34
([below](#the-bt-fixture-gives-the-air-and-the-test-makes-its-devices-on-it)), the validation
with `bt_spp_initiator` with the pin at mikroverk/esp32sim#347
([validation firmware](../features/validation-firmware-user.md#bt_spp_initiator)), and the tap of
the HCI between the firmware and its controller
([below](#the-hci-tap-is-a-btsnoop-file-on-the-boards-time-with-its-header-beside-it)).
Replaying a recorded SPP exchange needs the matching engine (#20), and plan M8's replayed peer
waits for it; nor does any source play an HCI tap back.

## The HCI tap is a btsnoop file on the board's time, with its header beside it

Status: decided (#34)

`bt.tap(path)` writes the HCI between the firmware's host and its controller as btsnoop version 1
with datalink 1002 (H4), the format Bumble's `BtSnooper` and Android write, which Wireshark,
and `bumble-show` read, and puts the recording's header, the same fields
as a UART tap's, in `path` with `.json` added, as plan §4.3 has it. A record's time is the board's
since power-on, written from btsnoop's 1970-01-01, so that a reader shows the time since power-on
and two runs write the same file; `created` in the header says when it was written. The packets
are recorded where the link carries them, in the `Bluetooth` interface, at the `hci_tx` and
`hci_rx` times, so the tap holds what the firmware's host sent and read, not what the controller
made and a reset threw away.

**Rejected:** a recording of this format with an event per packet, which no Bluetooth tool reads;
pcap with the link type of Bluetooth H4 with a direction header, which Wireshark reads too, and
`bumble-show` does not; timestamps on the wall clock at the moment written, as `BtSnooper` writes
them, which would make two runs' files differ and say nothing of the board's time; a time since
the year 2000, Bumble's anchor, under which a time since power-on reads as a date in 2000; and
nanoseconds in the sidecar beside the microseconds of the file, a second copy of every time.

## The firmware's controller is a controller of the air, with the guest's features and buffer

Status: decided (#34)

The controller the firmware's Bluedroid talks to through the runner's `--bt` is a
`GuestController`, an esp32tl `Controller` on the air of the test's devices: it pages, pairs and
leaves as their controllers do, so a device does not tell the board from another device on the air.
What Bluedroid needs of a controller and Bumble 0.0.236 lacks goes into `Controller`, so the
devices' controllers have it too: the commands with no handler in Bumble, an answer to an unknown
command, `HCI_Reset`, ACL data cut to the host's buffer, page 0 with the BR/EDR features. Only what
is the classic ESP32's stays in `GuestController`: three pages of features, no LE, and an ACL buffer
of 491 bytes, what esp32sim's VHCI carries in a packet of 496 less the 5 of its header, both ways.
Bumble's 27 bytes gave about 21 kbit/s of SPP on the emulator, 491 about 259 kbit/s.

**Rejected:** esp32sim's own virtual controller, which is BLE only; a controller of the guest's own
beside the air, because the peers would then need a second link to reach it; Bumble's controller
for the guest with only the guest's patches, because a fix the firmware needs would then be missing
on the devices, where the same host commands reach it from Bumble's host; and copying what the
classic ESP32's controller reports, which is not known yet: the values are esp32tl's, marked
TODO(owner) where the ESP32's would differ.

## The air of the bt fixture is moved by the board's link alone

Status: decided (#34)

On the emulator the air's time is the board's, and the run goes from turn to turn only while the
test waits on the board: an `Air(board_clock=True)` is moved by `follow`, which the `Bluetooth`
interface calls at every packet and every turn, and its `advance`, `wait` and `run` raise `AirError`
with a message that names `board.wait()` and `board.wait_until()`. The test reaches the air from its
own thread, as it reaches the Wi-Fi network, while the board holds the link in a turn, or before
the first one: what it does there, a device made, switched off or given another RSSI, happens at
the board's current instant, which the air is at, and goes out in that turn. The air's calls hold a
lock, so that the link's thread, at the first turn, and the test's do not meet on it.

**Rejected:** letting `air.wait` wait on the board, because the air would then have two ways to
move the same clock, and a test written for an `Air` of its own would run differently on a board;
and a `bt.wait`, for the same reason: the board's waits are the test's clock.

## An answer of the firmware's controller goes out 100 µs after it is made, and never before its turn

Status: decided (#34)

What the firmware's controller sends its host goes out `LATENCY_NS`, 100 µs, after the instant it
made it, which is also the interface's `latency`: of the order a controller takes to answer a
command, and far below the 10 ms the host sleeps between two polls of the VHCI when it has nothing
to read, so the answer is due long before the host would poll again. The host reads it at a later
poll, behind the packets before it: at most one packet a poll, and none at the poll after one
(protocol 5.22).

The latency also bounds every slice the runner runs while no event is unacknowledged (protocol 6.1),
because the board's callbacks cannot stop esp32sim's `run`. With the controller on the link it is
the smallest latency of the interfaces, so it sets how often an awake run stops. Measured on the
classic ESP32 with an awake image publishing over the modem and driving an SPP peer, 330 emulated
seconds, the three runs side by side on an M2 Pro: 10 µs cut the run into 33.1 million slices and
took 84.6 s of the runner's CPU for 300 emulated seconds; 100 µs took 3.4 million slices and 70.7 s
(−16 %); 1 ms took 0.5 million and 69.2 s (−18 %). The firmware published the same 641 records in
each. By rule 1 of the protocol the turn's
block point is at most the first unacknowledged event plus the latency, and every event of the turn
is at or after that event, so an answer is never due before the turn it goes out in; one that would
be is a `BluetoothError`, which fails the link.

**Rejected:** 10 µs, the first value, because of the slices above; 1 ms, because it gains another
2 % for an answer ten times slower than a controller's; a latency of 0, because every packet would
then take a turn of its own; and stamping
an answer due before the turn at the turn's time, as the spike did, because it would hide a runner
or a harness that broke the rule.

## HCI_Reset takes the controller off the air for an instant

Status: decided (#34)

Bumble 0.0.236's `HCI_Reset` keeps everything, so after a reboot of the firmware its pages to the
peer got `CONNECTION_ALREADY_EXISTS`, and an answer its host owed before the reboot took the answer
to the next page. Bluetooth Core 5.4, Vol 4, Part E, 7.3.2 says "the current operational state will
be lost, the Controller will enter standby mode and the Controller will automatically revert to the
default values". The controller now does that as a device that leaves and comes back at the same
instant: each peer loses contact and its end falls at its supervision timeout, this side ends what
it had under way and holds nothing, the answers owed are gone, and the host's settings are back at
their defaults. Its host hears nothing but the `Command_Complete`, which is all 7.3.2 gives it. A
chip reset and the board's supply do the same to the firmware's controller, without the command: the
controller is part of the chip. For `board.reset()` the backend restarts it in the held turn, before
it sends the reset, so that nothing made for the old host goes out with the reset to reach the new
one.

**Rejected:** clearing the controller's state alone, because a peer would keep a link to a
controller that forgot it, and what it sent would reach no entry; an `LMP_detach` to each peer,
because a controller that resets does not send one, and the peer's link falls at its timeout as
after a power loss; and `Disconnection_Complete` to the host, which asked for the reset.

## A command the controller does not know is answered in the event its host waits for

Status: decided (#34)

Vol 4, Part E, 4.5: "If the Controller does not support an issued command, it shall return the
error code Unknown HCI command (0x01) in the Status parameter of either an HCI_Command_Complete
event or an HCI_Command_Status event; which event is used is vendor-specific." Bumble 0.0.236
answers nothing to an asynchronous command it has no handler for, and the host waits for ever. The
controller answers `Command_Status` for a command the specification answers so before its
completion, and `Command_Complete` for any other, also one Bumble has no class for, and keeps each
in `unknown_commands`, which the report shows.

**Rejected:** `Command_Complete` for all, which 4.5 allows too, because a host that waits for a
status would wait on; and failing the run on an unknown command, because a real controller answers
and the firmware goes on.

## A setting that would change what is not modelled is refused, one that changes nothing is taken

Status: decided (#34)

Bluedroid writes settings Bumble has no handler for: the link policies, the scan activities and
types, the event filter, the inquiry access codes. The link has no radio, so the scan activities and
types and the link policies change nothing: they are kept and answered with `SUCCESS`. The event
filter is taken when it filters nothing, as Bluedroid's clearing of the inquiry filter does (its
discovery fails without it), and refused with `UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE` when it would
filter, since no event is filtered here. The inquiry access codes are modelled: an inquiry is
answered only by a device that scans for its code.

**Rejected:** taking every setting, because a firmware that filters inquiry results by class would
then get results its filter excludes, and a test would not see why.

## Controller-to-host flow control is postponed

Status: postponed (#34)

Bluedroid turns controller-to-host flow control on and gives its buffers with `Host_Buffer_Size`,
20 ACL packets of 1691 bytes on `bt_spp_initiator`. The controller cuts what it sends to the
packet length, as 7.3.39 requires, and takes `Host_Number_Of_Completed_Packets`, which gets no
event (7.3.40), but does not hold data back when the host's buffers are all in use: esp32sim's VHCI
delivers one packet per poll of the host anyway. A firmware that relies on it can be seen behind the
`bt` fixture.

## The bt fixture gives the air, and the test makes its devices on it

Status: decided (#34)

`bt` is the chip's Bluetooth, `board.bluetooth`, as `wifi` is its station: the test makes its
devices with `SppPeer(bt.air, ...)` and `BluetoothDevice(bt.air, ...)`, the same calls as on an
`Air` of its own. Asking for it is what makes the run forward the firmware's HCI (`--bt` with the
application's ELF), so it comes before power-on, as an access point does. The serial backend's
capability declaration gives no `bluetooth`, so the fixture skips there.

**Rejected:** factories on the fixture, `bt.spp_peer(...)`, because they would repeat the
devices' arguments; and `--bt` on every run, because a firmware without Bluetooth has no
`esp_bt_controller_init`, and the runner refuses its ELF.

## The SPP peer has a service name and offers basic mode only

Status: decided (#34)

ESP-IDF's `bt_spp_initiator` prints the service name it finds in the SDP record with `%s`, and
newlib's `printf` faults on the NULL of a record without one, so the peer's record names its
service, `Serial Port` unless the test gives another or `None`. Bumble's default L2CAP features name
Enhanced Retransmission Mode, which its RFCOMM server, in basic mode, does not take: it disconnects
when Bluedroid asks for it. The peer's devices name fixed channels and the FCS option only, and
Bluedroid stays in basic mode.

**Rejected:** no name by default, because the firmware a test is most likely to start from would
fault; and Enhanced Retransmission Mode in the peer, which is Bumble's RFCOMM server's to do.

## The firmware's controller does not follow a deep sleep or a disabled controller

Status: postponed (#34)

On a real chip the radio stops in a deep sleep and after `esp_bt_controller_disable`, and a peer's
link loses contact there. The runner sends nothing at either, nor at `esp_bt_controller_deinit`,
which resets esp32sim's VHCI session: the harness knows of a deep sleep only at the wake, which is
a reset. The firmware's controller therefore stays on the air until the next chip reset, its
scans and links as they were. A message from the runner for each would be needed, a change of the
runner and the protocol; the first test that depends on what a peer sees during a sleep or after
`disable` is what would bring that message.

**Rejected, for now:** taking the controller off the air when the host stops polling the VHCI,
because a host idle for a while polls rarely too, and the controller would leave the air under a
firmware that only waits.

## bumble is a dependency of esp32tl, pinned exactly

Status: decided (#33)

`pyproject.toml` requires `bumble==0.0.236`. Bumble is at 0.0.x, where any release may change
the internals esp32tl reaches into: the controller's, which `esp32tl/bluetooth/controller.py`
overrides, the RFCOMM multiplexer's acceptor and DLC's DISC handling, which
`esp32tl/bluetooth/peer.py` replaces, and `LocalLink`'s delivery, which `esp32tl/bluetooth/air.py`
overrides. A new version is taken by changing the pin, running the
tests, and checking whether each of those is still needed.

**Rejected:** an optional extra, because the Bluetooth fixture will be the plugin's and the plugin
has no optional dependencies; and a compatible release, `~=0.0.236`, which admits every 0.0.x.

## Serial is opt-in and pytest-embedded owns flashing and the port

Status: decided (#25)

`SERVICES` has `esp32sim: None` and `serial: esp,idf`, but the default selection is esp32sim only.
Serial needs `--esp32tl-backend serial` and an exact `--port` (or a parametrised `port`).
The collection hook refuses missing, blank or wildcard ports; validation before `app` also checks
the resolved port fixture. `ESPPORT` must be unset, empty or equal to the explicit port: the
plugin checks both the environment and pytest-embedded's cached port options. A separate
`flash_port` is refused; flashing, console and reset use one port. pytest-embedded's
`IdfSerial._start` owns autoflash and skip-autoflash.
`power_on()` resets the board and starts a fresh console capture; it neither opens a second
port nor flashes again. The real board runs
continuously, using monotonic host time; it cannot implement the emulator's held clock or set
its UTC. Serial takes over the redirect reader during capture to stamp batches after read polls,
then returns it to pytest-embedded after healthy teardown. Only the test thread updates console
buffers.

**Rejected:** adding esp32tl port/flash options, flashing twice, enabling hardware automatically,
or pretending the real device stops while the test body runs. Also rejected: permitting
autodetection, including a wildcard port, or accepting `--port-location` / `--port-mac` alone:
pytest-embedded 2.9.3 may probe other ports before resolving them. An exact port avoids claiming
an unrelated connected ESP32. Also rejected: silently accepting `ESPPORT` overriding that
selection, or allowing a separate flash port without a reset transport design.

## Reference recordings check the serial artifact before opening the port

Status: decided (#25)

`app_recording(path)` is an optional reference marker, separate from UART replay (unsupported on
serial). It requires `app_sha256` to equal the hash of the app binary named in `flasher_args.json`.
The plugin validates before pytest-embedded's `app` fixture, including when a test explicitly
asks for `dut`, and before `board` constructs its backend. Missing hashes and mismatches fail.
The check applies both with autoflash and skip-autoflash, and is ignored on esp32sim, preserving
replay into other applications. Console taps record that artifact hash on serial with clock
`wall`; comparing it cannot prove what a board carries under skip-autoflash.

**Rejected:** using the ELF hash as the recording hash, requiring a reference recording for tests
with none, checking only inside an unsupported UART replay, or claiming this performs flash
readback. Readback verification remains outside this milestone.

## Console taps use the same API on both backends

Status: decided (#25)

`console.tap(path)` remains in #25 on both serial and esp32sim. The console already declares
`tap = true` on both, and a console test can record either run without backend-specific code.
The emulator stamps output with the link time; serial stamps it after a reader poll, on wall
time, with polling, USB, driver and host scheduling delays. At #25 both recorded only `out` events;
#195 adds test writes as `in` events. They share the app-image hash helper. A console tap is a test step in `RunLog`; its path is visible in
the report and JSON property. This resolves the console-tap part postponed in #21, not replay
of console input. The recording schema already accepts `console`, so its version remains 1.

**Rejected:** splitting the emulator's small console adapter into a separate change while making
`console.tap` backend-dependent. Recording on both backends makes reference hashes usable across
them; it does not imply that console output can be replayed as UART input.

## Serial capture failures leave no successful power-on and have bounded reader teardown

Status: decided (#25)

A failed reset rolls back the clock origin, leaves `powered` false, and restores pytest-embedded's
redirect thread. It writes no recording of a successful run. A test may retry the reset. During
close, the backend waits at most one second for its reader; a still-running reader is a
`BackendError`. The reader is a daemon and discards bytes returned after the stop signal.
The redirect thread restarts only after a healthy reader stopped, avoiding concurrent readers
and pytest-embedded's reconnect loop on a disconnected device. The fixture owner closes the port.

**Rejected:** an unbounded join up to the test's 600 s timeout, claiming a failed reset powered the
board on, or restarting another reader on a dead port. This bounds esp32tl's join, not arbitrary
blocking calls in the USB driver or pytest-embedded's own connection and reset operations.

## Serial validation accepts unencrypted classic ESP32 builds only

Status: decided (#25)

*Extended in #102 by [Explicit build paths are relative to the application and never fall
back](#explicit-build-paths-are-relative-to-the-application-and-never-fall-back).*

`load_app(app_path, backend="serial")` shares the finished-build checks and gives serial-specific
messages. Like the first emulator backend, M4 is scoped to classic ESP32 and unencrypted builds.
Flash encryption is physically possible but requires provisioning and key/efuse management that
this harness neither implements nor validates. The app hash refers to the local plaintext file,
not ciphertext on flash. Encrypted builds are refused before port setup even with skip-autoflash.

**Rejected:** implying that the serial restriction comes from a runner, or silently accepting
an unvalidated encryption/provisioning workflow. Supporting such boards needs its own design.

## The modem drives RI through Lines and observes PWRKEY through it

Status: decided (#29)

`Lines` remains the single GPIO interface on the link. The modem model reserves RI as its output,
observes firmware PWRKEY changes through callbacks and queues GPIO responses through
`Lines.model_drive`. These stay in Lines until due, with `quiet_until` bounding the next turn;
a new model change replaces a pending one on the same pin. Sending future GPIO changes straight
to the runner was rejected because a test can remove power before the planned response and must
be able to cancel it. The lines fixture retains every line for reading and event inspection,
but rejects test drive/release of a model-owned output. Model changes carry `by="model"`.
Callbacks run outside the line lock. A model selects the minimum response latency of `Lines`,
1 ms for the modem, so a PWRKEY edge cannot arrive too late to schedule RI.

**Rejected:** excluding model pins from `Lines`, because requesting the modem would then change
which lines the test can inspect; and letting two interfaces own RI, which BoardLink forbids.
No separate scenario or wall clock is introduced.

## The modem begins powered and registered and keeps every AT command

Status: decided (#29)

A first model call selects an initially powered, registered modem with -73 dBm signal and echo
off, matching the spike's immediately useful AT peer. `power_off` selects an initially off modem
when firmware must start it with PWRKEY. Power pulses use the profile's ESP32-pad active level
and separate on/off thresholds: high at the ESP32 pad, 50 ms and 2500 ms. The polarity comes
from LilyGO's ATdebug example; SIMCom's Hardware Design V1.03, tables 10–11, gives 50 ms as the
typical on pulse and 2.5 s as the minimum off pulse
([sources](../features/modem-user.md#pwrkey-and-reporting)).
The model uses 50 ms as a threshold, not as a claimed manufacturer-guaranteed minimum. Using
LilyGO's nominal 100/3000 ms delays as thresholds was rejected: a tick-based delay
can end nearly one tick early, so unmodified example firmware would fail to start or stop the
model. The validation firmware uses those nominal delays without adding a tick. The pulse
is evaluated at release; the model has no boot delay. Unknown AT commands get `OK` and are marked
as fallbacks in the complete, timestamped command report, so the subset can grow from firmware.
Noncommands, including command-mode `+++`, never get `OK`.

**Rejected:** failing unknown AT commands by default, because the spike showed that a permissive
peer can run firmware whose modem initialization includes unrelated settings; and relying on the
rolling wire log for the command list, because earlier setup commands would disappear.

## The modem's unset clock reads what the test says, with no zone

Status: decided (#128)

`modem.clock_not_set(reads=...)` has a required argument. No document gives the value `AT+CCLK?`
returns before the network sets the clock, so any default would look like a fact about the modem.
The manuals say only that the factory time zone is invalid and that `+CCLK?` returns the zone when
it is valid: leaving the zone out is an inference, and the
[guide](../features/modem-user.md#clock-and-operator) says so. The clock runs on from the value
given. The default operator's numeric code is `26000`, which matches the model's IMSI,
`260000000000000`. `AT+CCLK=` stays a fallback until a firmware needs it.

**Rejected:** a default such as 2000-01-01, because no source for this modem
gives it; `00101` as the default code, because it would not match the IMSI.

## A hung modem keeps its commands, and RESET restarts it while power_enable stays observed

Status: decided (#128)

`modem.hang(pwrkey=)` stops answers, data and RI, keeps every AT command with an empty response,
and ends data mode for the model so that the parser sees what the firmware sends next. The test
chooses whether PWRKEY still works, because no document says what PWRKEY does to a hung modem. A
RESET pulse, active high at the pad with SIMCom's 2 s minimum as its threshold, restarts a running
modem in command mode, and does nothing to one that is off. The pulse is judged at release, as
PWRKEY's is; no document gives the state during a pulse, and the modem goes on as it was.
`modem.restarts` records what ended each run, at the instant it did, so `supply` has a meaning and
a start from off is no restart.

`lines.board.power_enable` (GPIO 12) stays an observed line with no effect on the modem.

**Rejected:** modelling GPIO 12's power gating, because a recovery can be validated without it and
it would have to invent what the gate does; LilyGO's 2600 ms as the threshold, because it is a
firmware's delay, not the document's minimum, and a tick-based delay can end nearly a tick early;
recording a restart at each start, because a modem's first start is no restart and a loss of supply
would have no entry until PWRKEY started it again.

## The modem data peer and realistic escape timing wait for network integration

Status: superseded (#30)

This AT-only model accepts an optional `Source` as its data peer, but creates no PPP or host
network. Without one, dial still returns `CONNECT` and data bytes are retained without answers.
The validation firmware exercises dial and escape without a network. Registration and supply
loss end data mode silently, without an unsolicited `NO CARRIER`; disconnect notification is
left with the peer lifecycle in #30 and documented as a current limitation. Resetting the eventual
PPP peer belongs with its lifecycle in #30.

`+++` escapes across any UART chunk boundaries, without guard-time enforcement; one or two
trailing pluses wait for the next byte. A timed escape recognizer, unsolicited registration
notifications, realistic boot/shutdown delays (including `Toff-on` and `Ton(uart)`) and detailed
APN/operator configuration wait until validation firmware needs them. Commands accepted without
simulating their settings and unknown fallbacks are documented, so they do not imply a complete
cellular modem.

## NVS generation treats every key and namespace literally

Status: decided (#70)

`board.nvs()` uses `esp-idf-nvs-partition-gen`'s `nvs_open` and the `NVS.write_namespace` and
`NVS.write_entry` methods directly. The dependency is pinned exactly; upgrades must verify
literal names, actual values, the complete entry set and deterministic image bytes. The API
takes the usable size excluding the final reserved page and adds that page when the writer
closes.

The adapter validates values and passes strings as `string`, bytes as `binary` and integers
with their explicit width directly to the writer, without CSV or temporary files. This
preserves CRLF, multiline certificates and lines beginning with `#`, and supports blobs larger
than 64 KiB whenever the NVS partition has room. A successful second call for a partition
replaces its contents; failed validation or generation leaves the preceding image intact.

Names reach the pinned generator as `_NvsKey`, a private `str` subclass whose length is its
UTF-8 byte count. Version 0.3.0's primitive and variable-length writers use `len(key)` to size
a bytearray slice receiving `key.encode()`. Ordinary Unicode strings can enlarge the entry
header, corrupt neighbours and extend the final page. The adapter preserves the original
text, UTF-8 bytes and namespace identity while making that slice length correct. Output-size
validation remains mandatory; tests independently check entry contents and CRCs at page
boundaries.

**Rejected:** the generator's CLI and module-level `write_entry` wrapper. In both 0.1.9 and
0.3.0 they interpret names such as `sta.ssid` and `sta.pswd` even in a namespace called `config`,
write temporary file paths instead of string contents, and add unrequested Wi-Fi keys. They
also interpret namespace names as keys before checking the CSV row type. Inlining strings
would not prevent the extra keys, and the CLI filters comment-looking lines before CSV
parsing. An open dependency range could silently restore these transformations. Implicitly
merging successive calls is also rejected: each call describes a whole partition and must not
retain keys absent from the new configuration.

Retaining intermediate CSV is rejected: its default 131072-character field limit rejects a
hex-encoded blob above 64 KiB even when it fits in NVS. Raising a process-global CSV limit
would only preserve unnecessary serialisation and shared mutable state. Rejecting non-ASCII
names would narrow the promised UTF-8 API; copying the generator's page encoder or patching
its global classes would carry more code and affect other callers. The length adapter is
local to the names passed to this writer and is rechecked when upgrading the pinned package.

## Request answers wrap independently of the unprompted timeline

Status: decided (#20)

The matching engine keeps one sequence of occurrences per normalised request, including empty
answers for recorded timeouts. That sequence always wraps; `end: hold` does not disable commands
once the unprompted timeline is exhausted. `loop` repeats only the unprompted events (those before
the first complete request), with a period equal to the last recording line's time, measured from
zero. There is no invented inter-cycle pause or new format field. An event at zero in the next
cycle follows the preceding cycle's final event at the same instant. A nonempty, zero-duration
unprompted loop is refused; an empty loop stays silent.

The format has no correlation metadata: every input after a request and before the next complete
request belongs to that request. The engine does not guess which of those bytes are unsolicited
notifications, or which response belongs to an earlier pipelined request. Such traffic needs a
protocol model or a recording split appropriately for this rule.

## Replay detects protocols from requests and leaves pure streams passive

Status: decided (#20)

Requests beginning with `$` or UBX sync use the combined GNSS framer (PMTK lines and UBX frames
on one wire); other requests use AT. `protocol=` overrides that choice, including for recordings
with no requests. A pure stream without that argument retains `latency=None` and ignores firmware
transmissions. An explicitly configured protocol answers unseen requests even without examples;
when there is no recorded answer delay its latency is zero, since no positive bound is known.
Wall-clock recordings still cannot replay on an emulated-clock route (#27).

The final request fragment supplies its timestamp, both while compiling and receiving. Starting
at the first byte could put a short answer before the request's terminator arrives. UBX framing
uses its length field; payload and checksum bytes remain opaque, permitting fault recordings.
AT case and whitespace normalisation preserves quoted values and prompted Ctrl-Z payloads.


## AT separators end before the response and prompts belong at line boundaries

Status: decided (#20)

A CR-terminated command consumes one optional LF, including in a separate outgoing chunk before
the response. The first response byte closes this separator window. Stripping whitespace from
prompted payloads was rejected: a payload's leading LF, spaces and CR/LF must stay literal.
A LF sent after the response starts is therefore data, not a delayed command separator.

A prompt must begin the response or follow CR/LF and be its current suffix. Every response
fragment recomputes that candidate until payload bytes start; permanently latching a temporary
suffix would make framing depend on chunk boundaries. Once the payload starts, Ctrl-Z framing
stays active until it completes. The same framer handles the recording and live wire.

## Unseen replay requests retain raw occurrences alongside normalised counts

Status: decided (#20)

The normalised `unseen` counter remains the summary. `unseen_events` additionally stores every
unknown request's completion time and full raw frame, including repeats and silent fallbacks.
Text and JSON reports expose both representations. The list is independent of the rolling wire
log and retained for the whole run, with the memory and report-size cost documented.

**Rejected:** retaining only counters, which merges distinct wire spellings and loses timing;
recovering occurrences from the rolling log, which can discard precisely the request needed
for diagnosis. Existing summary fields stay intact for tests and report consumers.

## Serial adapters preserve skips for modem models and NVS replacement

Status: decided (#25)

The serial modem route uses the same `Modem` adapter as esp32sim so model, replay and tap
requests reach the capability check. Merely requesting it leaves the physical peripheral
untouched and does not skip a console test. Its local `Lines` object has no transport and no
model can attach to it. `board.nvs()` skips on serial before generating or writing an image.

**Rejected:** exposing the new emulator APIs as missing attributes on serial, or flashing a
test-specific NVS partition after pytest-embedded's autoflash without a hardware lifecycle
design. Replacement of physical NVS remains outside this serial milestone.


## The network is selected explicitly, and resets with its modem

Status: decided (#30)

`modem.network()` selects the AT model and its PPP peer before power-on; `broker` calls it as a
fixture. Existing AT-only tests retain their immediate `+++` escape and unanswered data phase.
Network data mode instead requires a second of idle time before and after `+++`, preserving
binary PPP bytes. Power and registration loss reset PPP and queued replies; a fresh
dial negotiates again. They reset the TCP sessions too, until #244 left the device's half-open
([a lost link](#a-lost-link-leaves-the-devices-connections-half-open-until-their-keepalive)). The
broker retains its test-side history and retained messages, while a connection's end emits its
Will once. Registration and power loss emit
no unsolicited `NO CARRIER`; [`drop_call`](#a-dropped-call-says-no-carrier-and-offers-a-terminate-request-only-if-asked)
does. Functionality loss was on both lists until #276, which found that the model reads `AT+CFUN=`
only in command mode, with the call already ended
([an escape is a mode change](#an-escape-is-a-mode-change-not-a-modem-command)).

**Rejected:** enabling PPP for every AT-model call, because existing firmware tests deliberately
dial and escape without a PPP stack; and recognizing every binary `+++` as an escape, because
valid PPP/TLS payloads may contain it. Fully model-specific escape timing and other unsolicited
modem notifications wait for a firmware test that needs them.

## A dropped call says NO CARRIER, and offers a Terminate-Request only if asked

Status: decided (#128)

`modem.drop_call()` ends a data call with the modem registered: it sends `NO CARRIER` and returns to
command mode. `terminate=True` first offers an LCP Terminate-Request. No vendor document says whether
a carrier or SIMCom sends one, so it is the test's choice, off by default. It matters to the
firmware: with esp_modem modem-v1.4.1 and ESP-IDF v5.3.6, `NO CARRIER` in data mode goes unnoticed
and the firmware learns from its own timeout, while a Terminate-Request gives
`IP_EVENT_PPP_LOST_IP` 6 s later. `lose_registration()` stays silent. The command parser did not
change: `+++` without an answer in command mode costs `esp_modem` 5 s, and its `\n` before `AT`
clears PPP bytes left on the line. A command stuck to those bytes, with no newline between, is lost
whole, with no answer and no entry in `commands`. The A76XX manual, section 1.5.2, requires the prefix
at the beginning of each command line and does not say what happens to bytes before it, so this is
the model's assumption, and no firmware here needs otherwise: `modem_recovery` leaves data mode
through `esp_modem`, which writes the newline when `+++` gets no answer. A call dropped so late that
`NO CARRIER` answers `+++` leaves no newline, and nothing on the line for `AT` to stick to
([measured](../features/modem-user.md#a-dropped-data-call), with what esp32sim's byte-sized reads
change).

**Rejected:** a `NO CARRIER` from `lose_registration()` too, because no source says a modem sends one
and existing tests rely on the silence; a Terminate-Request by default, because it would make every
dropped call prompt and hide the firmware's timeout path; waiting for the Terminate-Ack before
`NO CARRIER`, because no document gives that order and the firmware's behaviour does not depend on it;
looking for `AT` at the end of a noisy line, because PPP's binary bytes could then match as a command.

## The MQTT service implements ephemeral sessions and QoS zero and one

Status: decided (#30, #104), sessions amended (#130)

The broker grants subscription QoS at most 1 and refuses QoS 2 publications. Retained publications
belong to the test's broker and survive modem power cycles. Credentials are recorded without
authentication by default; #103 adds an optional test-controlled CONNECT policy (see below). Since
#130 [the broker keeps a session by client ID when CleanSession is
0](#the-broker-keeps-a-session-by-client-id-when-cleansession-is-0): sessions are no longer
ephemeral for such a client.

## The broker keeps a session by client ID when CleanSession is 0

Status: decided (#130)

Firmware that connects with CleanSession=0, subscribes on every connection and receives non-retained
QoS 1 messages could not be tested: Session Present was always 0, and a QoS 1 message for an offline
client was lost. Session state moves out of the connection and is keyed by client ID: subscriptions,
the QoS 1 messages waiting for the client, and what was sent to it unacknowledged. A CleanSession=0
CONNECT resumes it, first what is unacknowledged with DUP and the original packet IDs (MQTT-4.4.0-1,
MQTT-3.3.1-1), then the queue in order (MQTT-4.6.0-6). CleanSession=1 discards it at CONNECT and at
disconnection (MQTT-3.1.2-6). MQTT 3.1.1 writes its re-send order as a rule for the client
(MQTT-4.6.0-1); MQTT-4.6.0-6 applies the same rules to a server delivering an Ordered Topic.

The queue has no limit, because the specification sets none, and no session expires. A resumed
session whose queue outruns the 65535 packet IDs waits for PUBACKs rather than closing, because a
client's PUBACKs could not reach a closed connection, and it would get the same messages on every
connection. Since #140 [a connected client with no free ID waits
too](#a-connected-client-with-no-free-packet-id-waits-for-a-puback). A QoS 0
message for an offline client is dropped: MQTT-3.1.2-5 lets a server store it and does not ask it
to. The test is what ends a stored session, with a CleanSession=1 CONNECT or `forget_session`,
which refuses a connected client instead of disconnecting it, so that one call does not do two
things. The rule for a queued message goes by the message's QoS, as MQTT-3.1.2-5 does: a QoS 1
message that matches a QoS 0 subscription is stored, and goes at QoS 0.

A stored session outlives the network's reset, since the broker is the test's and not the link's,
like its retained messages and histories. `subscribed()` keeps speaking of active clients, because
tests use it to wait until a client is ready to receive. An incoming `Publication` keeps belonging
to its connection: it is the test's control over one exchange on one stream, not state the client
asked the broker to keep. `connection_policy` still runs before any state is installed or discarded,
so that a refused CleanSession=1 CONNECT leaves a stored session alone.

`deliveries`, `next_delivery` and `find_delivery` take the names, boundary and condition object of
[the other event APIs](#mqtt-tls-http-and-dhcp-event-apis-name-history-queries-and-next-event-conditions-explicitly).
A `Delivery` is mutable where the other records are frozen, because its `acknowledged_at` is learned
later, and a test holding the record should see it. It belongs to one PUBLISH: a redelivery has its
own, and the first keeps `None`.

**Rejected:** keeping sessions ephemeral and having the test publish again after a reconnect,
because the firmware's own handling of Session Present and DUP would then go unexercised, which is
what such a test is for. Also rejected: a limit on the queue with a chosen overflow, which would be
a number with no source; storing QoS 0 for an offline client, which the specification leaves open
and nothing here needs; and bringing a pending `Publication` back with
its session, because the client retransmits what it published, and a new handle says so truthfully.

## A connected client with no free packet ID waits for a PUBACK

Status: decided (#140)

When a connected client has 65535 QoS 1 messages unacknowledged, a QoS 1 message for it goes into
its session's queue, and the next PUBACK sends it, as a resumed session's queue already did. MQTT
3.1.1 requires a packet ID that is not in use (MQTT-2.3.1-2, applied to a server by MQTT-2.3.1-4)
and says nothing of what a server does when none is free. Waiting is what a client can recover
from, since its PUBACKs still reach the broker; closing sent a client that subscribes on every
connection the same retained messages on every connection, and its queue never drained. One rule
now covers a new message, a retained one that a SUBSCRIBE matches and a resumed queue.

- CleanSession=1 keeps the queue in its `_State` for the life of the connection, and loses it with
  the session, as it loses everything else.
- `broker.session(client_id).queued` is every message waiting for the client: offline, or online
  with no free ID.
- A retained message that a SUBSCRIBE matched keeps RETAIN 1 in the queue (MQTT-3.3.1-8), across a
  reconnection too; the rest of the queue goes with RETAIN 0 (MQTT-3.3.1-9).
- A message published at QoS 0, which needs no ID, goes at once, as it did behind a resumed queue;
  one published at QoS 1 waits behind a queue that is waiting even for a QoS 0 subscription, to
  keep its place in the order, and goes at once to it when nothing is waiting yet. Once at the
  head of the queue it goes without waiting for a PUBACK, since it takes no ID.
- A SUBSCRIBE while the queue waits does not queue a retained message that already waits from an
  earlier SUBSCRIBE: MQTT-3.8.4-3 asks for the retained message to be sent again, and the copy
  waiting is that send. Otherwise a client that subscribes on every connection before its first
  PUBACK would get one copy per connection. A retained message that has changed since is a
  different message and joins the queue, behind what is there.
- A client that never acknowledges keeps its connection and an unlimited queue. The sign a test
  has is the log line `mqtt: <client-id> has no free packet identifier: waiting for a PUBACK, <n>
  queued`, once each time a connection starts to wait, and `queued` growing.

**Rejected:** closing, the behaviour before #140, as a clear signal in a test that the client has
stopped acknowledging. A test that wants that signal can watch `queued` or the log, while a client
that does acknowledge, only slowly, could not get past a closed connection. Also rejected: closing
on a live connection and waiting on a resumed one, as #138 left it, two rules for one state. And
for a SUBSCRIBE while waiting, queueing every matching retained message again, which grows the
queue by one copy per SUBSCRIBE, and replacing a waiting retained entry of the same topic with the
newest, which would move a message to another place in the order.

## A path with RTT and rate sits between the link and the network

Status: decided (#130)

`network.set_path(rtt=, downlink=, uplink=)` is a FIFO each way between the link and `Network`, in
`esp32tl/net/__init__.py`. Without it a QoS 1 backlog drains at the same speed whatever the link. A
stated `rtt` is exact, from the device's request to the stamp of the answer. The values are the
test's parameters: no vendor source stands behind them, and nothing models an operator.

LCP, IPCP, ARP and DHCP stay on the link: they are between the device and its modem or access point,
and a test of a slow network should not also change how long the link takes to come up. The default
of 1 ms is the network as it was. The RTT's excess over that floor is shared between the directions,
and the floor stays on the way down, so that the default takes a request at its own time and an
answer 1 ms later. `latency()` stays 1 ms, since PPP control answers in 1 ms, and a larger promise
would break rule 2 of the protocol; that is why an RTT below it is refused. `quiet_until` takes the
queue's head, one turn per datagram. The runner does not change.

A datagram going up is taken in at the start of the turn, through `Interface.arrive`, before the
test holds it, so that a record is there when `board.now` reaches its stamp. Rule 1 lets the peer
stop the runner at the datagram's time, and without this the test would stand at that time and not
see it. The alternative was to call `poll` before the hold as well: that would also move what
replays and models do, and the broker's deadlines, past what this path needs.

A change applies to datagrams entering after it, and none overtakes another, because the link
beneath TCP never reorders. A datagram waits in the network until its time, not in the runner's input
queue as a future-dated `uart_rx` or `eth_rx`, so that a reset can still drop it: with an RTT of 600
ms, a modem that loses registration would otherwise go on receiving for 300 ms.

**Rejected:** a larger `latency()`, which would save turns but make PPP control late and break rule 2;
future-dating answers in the runner's queue, which a reset cannot take back; a delay in one direction
only, which would stamp a broker's receipt a whole RTT away from the device's request or from its
answer; and delaying the link's own control, as above.

## A broker deadline runs before the test holds its turn

Status: decided (#139)

`Internet.arrive` runs `Broker.poll()` after taking in the path, so a delayed PUBACK and a keepalive
expiry, with its Will, are there when `board.now` reaches their deadline, as a datagram from the
path is. Before, they ran in `poll`, after the hold: the test standing at the deadline saw nothing,
and saw it only one step of its clock later, so a `wait_until` whose window ended on the deadline
raised `NeverHappened`. What the deadline writes, a PUBACK, still goes out in `poll`.

What moves: a `Publication.release()` at the deadline's instant returns `False`, since the
deadline came first, and a `broker.publish` or `broker.disconnect` at that instant comes after an
expiry. Events with equal stamps may be in another order in a network transcript than before this
change; two runs of one version still agree. Expiry still wins a tie with a PUBACK deadline.

**Rejected:** documenting the order as it was, which left the broker the one part of the network a
test sees a step late, against the rule that a record is there when `board.now` reaches its stamp.

## Packet loss on the network path is postponed

Status: postponed (#130)

The path loses nothing. Losing downlink datagrams needs retransmission timers in
`esp32tl/net/ip.py`, whose TCP sends nothing again but the test's SYN (#289) because the link never
loses a byte, and losing
uplink only would be half a model: the firmware's retransmission would be exercised and the network's
never. It waits for a test that needs it.

## MQTT CONNECT policy runs before accepted session state is installed

Status: decided (#103)

`Broker.connection_policy` is a replaceable callable from immutable public `Connect` metadata
to a named MQTT 3.1.1 `ConnackCode`; `None` preserves the accepting default. A test can compare
credentials, refuse the next attempt with a stateful callable, or change policy between board
advances. No credential store or external authentication service is needed. MQTT 5 reason codes,
bare integers and booleans are not accepted as policy results.

Parsing finishes before the policy runs. An empty ID without CleanSession remains a built-in
rejection that policy cannot override. Other malformed or unsupported requests close without
policy evaluation. Metadata retains the submitted client ID; allocation of anonymous IDs,
Will installation and takeover happen only on acceptance, so refusal cannot affect an existing
client or publish an unaccepted Will. Both TCP and TLS use the same broker policy.

`connection_attempts` records selected outcomes, including built-in identifier rejection, with
CONNECT metadata, board timestamps and return codes. It is separate from accepted `clients` to
preserve existing assertions and `connected()` waits. `connection_attempt()` waits for a future
outcome. The two are `next_connection()` and `next_connection_attempt()` now ([Only a condition for
the next event has the next prefix](#only-a-condition-for-the-next-event-has-the-next-prefix)).
Malformed input and TLS failures have no CONNACK outcome and are not recorded there. Policy
exceptions are test errors, rather than hiding test bugs as authentication failures or malformed
firmware traffic. A directly used broker raises them from `on_data`; in a board run the policy runs
in the link's turn, so the link fails naming the exception and the run ends, and the test's wait
raises `RunEnded`. Both histories survive network reset.

Incoming QoS 1 PUBACK timing belongs to the broker: `puback` selects immediate (the default),
held or emulated-clock-delayed emission for each new exchange. Each receipt carries a
connection ID, packet ID and DUP bit, and references a public `Publication` handle. Pending
receipts with the same connection/packet ID share that handle and its original deadline; every
receipt is routed, since QoS 1 permits duplicates. After emission or reconnect, reuse creates a
new handle. `release()` is idempotent; closure cancels pending handles permanently. `disconnect`
closes one client's stream through the Will path, leaving PPP and unrelated traffic intact.
The model records receipt and PUBACK emission separately and does not infer device processing
or durable delivery from either. Protocol tests cover plain TCP and TLS over PPP, including NTP
and a second connection while the first reconnects. Existing firmware examples use QoS 0.

**Rejected:** resetting the modem to simulate a broker fault, because it destroys the independent
network path under test; using client ID plus packet ID as a permanent publication identity,
because both can be reused; hiding duplicate receipts or treating PUBACK emission as exactly-once
delivery.

## TLS determinism compares explicit plaintext transcripts

Status: superseded (#68)

Once the TLS server's randomness is seeded, network transcripts compare exactly, times after a
handshake included: see
[The harness's TLS server draws every random value from a seed](#the-harnesss-tls-server-draws-every-random-value-from-a-seed).

The network records events and application stream bytes after TLS termination. Its diagnostic
`esp32tl_network: 1` transcript is compared exactly before the first handshake starts, then by
ordered events and bytes per connection/direction without times or chunk boundaries. System TLS
randomness starts affecting the run during handshake, so the boundary is its start, not completion.
The TLS regression uses a real MemoryBIO client, CA verification and bidirectional MQTT. Raw
UART recordings and link reports remain exact; firmware TLS/NTP validation belongs to #72.

**Rejected:** stripping times from every recording or dropping every link statistic whenever TLS
appears, because either can hide regressions on unrelated routes; comparing encrypted TLS bytes,
because system keys and nonces differ even when application traffic is identical.

## pppos_client is fetched by pinned commit and checksum

Status: decided (#30)

The build downloads esp-protocols `modem-v1.4.1` at its fixed commit and checks its archive digest.
The whole source tree is retained under the build output so the unmodified example's relative
component override uses that revision's esp_modem. Only sdkconfig selects SIM7600 and the LilyGO
UART; application and component sources remain unchanged.

**Rejected:** copying the example into this repository or resolving its component from the latest
registry release, because the former obscures whether validation still uses upstream unchanged,
and the latter stops testing the version demonstrated by the spike.


## Peer MRU limits TCP segmentation and oversized UDP replies are dropped

Status: decided (#30)

The PPP peer records the MRU it acknowledges, defaulting to 1500 when omitted and resetting on
renegotiation, termination or redial. TCP limits each payload by MRU minus its IPv4/TCP headers,
MSS and the receive window. This stack supports MRU values from 68; smaller requests get a
Configure-Nak proposing 68, enough for its fixed control headers. UDP responses above MRU are
dropped before packet construction and logged, without fragmentation. PPP also checks outgoing
IPv4 packet size at its boundary.

**Rejected:** acknowledging MRU without applying it, because a device may then discard TCP or TLS
responses it negotiated to be smaller; adding outgoing fragmentation here, because this network
has no fragmentation/reassembly support and the existing DNS/NTP services fit ordinary MRUs.

## Anonymous MQTT clients get deterministic distinct identifiers

Status: decided (#30)

An empty client ID with CleanSession set gets `esp32tl-anonymous-<n>` before Will construction,
client takeover and connection history. The broker increments its own counter and skips every
ID in its connection history, including supplied IDs. Empty IDs without CleanSession receive
CONNACK code 2 before closure. Explicitly repeating an ID still replaces its prior connection.

**Rejected:** treating every empty ID as the same client, which disconnects unrelated firmware
connections and may emit their Wills; random IDs, which make otherwise identical tests differ.


## Output handles provide live named conditions without changing GPIO ownership

Status: decided (#71)

`lines.output(line, active=1)` exposes `is_on` and `is_off` callable conditions on a read-only
handle. Their representation names the line, desired state, pin and level, so the existing
`board.wait_until` produces useful diagnostics without special handling. Both conditions are
false while the firmware's output is undriven. Polarity is explicit: `active=0` means low is on.

**Rejected:** treating an undriven output as off, which could make a test pass before firmware
configured the pin; a separate clock or waiting API on the handle, which would duplicate the
board's clock; and putting the GPS alarm's NVS schema into the library. Its unit-aware
`configure_alarm` helper stays in the example.

Current-state assertions and history assertions are explicit separate operations:
`assert_on` / `assert_off` inspect now, while `assert_stayed_on(since=...)` /
`assert_stayed_off(since=...)` inspect the starting state and all subsequent firmware events
through now. They never advance the clock. Unknown states fail. At the starting timestamp the
last reported state wins; subsequent events are all checked, including same-timestamp pulses.

**Rejected:** polling to prove an output stayed off, which misses short pulses; silently
accepting an unknown starting state, which hides unconfigured pins. The example's boot helper
separately permits initial setup while still rejecting every active pulse.


## Board owns interfaces and SD image builders

Status: decided (#73)

Tests can request one `board: Board` and access `console`, `gnss`, `modem`, `lines` and `sd_card`
through it. Both backends implement the shared public base, exported from
`esp32tl`. Existing fixtures return the same objects, including the console matching cursor.
This avoids a growing fixture parameter list as a test uses more interfaces without adding
another context object or hiding backend capabilities.

The SD logger exposed repeated image preparation and filesystem helpers in application tests.
`board.sd_card.image(size_mb=32).with_text(...).with_bytes(...).build()` now owns that work in
`esp32tl/interfaces/sd_card.py`, superseding the earlier decision to keep FAT helpers local.
Building prepares initial contents; inserting the resulting `CardImage` remains explicit. Reads
reopen the current image so firmware writes are visible. Temporary images live until board
close, including those never inserted, while external paths retain their original ownership.

**Rejected:** adding a second context fixture, which would duplicate `board`; replacing existing
fixture names, which would force unrelated tests to migrate; a single ambiguous `write` accepting
text and bytes, since encoding belongs to text only; automatic insertion by `build`, which hides
scenario setup; and a Python FAT dependency, since existing mtools already handles the image
formats and avoids a second filesystem implementation. There is no host mutation API on a built
image: declare initial files through the builder and let the firmware write during the run.


## Tests reach the interfaces through board, and the fixtures stay as shortcuts

Status: decided (#198)

Every snippet in the documentation and the guide, and every example, writes `board.console`,
`board.gnss`, `board.modem`, `board.lines`, `board.sd_card`, `board.wifi`, `board.bluetooth` and
`board.modem.network().broker`, and a test asks for `board` alone; the GPS alarm clock's tests and
the README's snippets were the last that took the `gnss`, `console` and `lines` fixtures. The user
documentation and the guide name the Bluetooth `board.bluetooth` in their prose as well, and write
`bt` for it only after saying so; the technical documents, which describe this repository's tests,
keep the fixtures' names. A first consumer could not tell which of the two to write. The fixtures
`console`, `gnss`, `modem`, `lines`, `sd_card`, `wifi`, `bt` and `broker` stay, as shortcuts to the
same objects, and are not deprecated: this repository's own tests use them throughout; the `gnss`,
`modem`, `lines`, `sd_card` and `bt` fixtures turn a `BackendError` of a missing interface into a
failure at setup that names the backend; and removing them would buy only fewer names. The plugin's
documentation says once which style is the recommended one, and where the two differ: through
`board.<interface>` the same `BackendError` fails the test in its body, with a traceback and no
prefix. `card_cache` is a fixture of its own, session-scoped, with no `board` equivalent, and is not
a shortcut.

**Rejected:** deprecating the fixtures with a warning, because nothing would get simpler for a
test that already uses `board`, and every test in `tests/` would need rewriting or a filter for no
change in behaviour. Recommending the fixtures instead, because `board.uart(role)` for a route of a
project's own profile has no fixture, so a test would mix the two styles anyway, and because the
broker fixture hides the `network()` call that selects the modem's network.

**Postponed:** the plugin turning a `BackendError` of a missing interface that the test body
raises into the failure the fixtures give, `pytest.fail` with the backend's name and no traceback,
so that both styles end with the same message; it changes what the plugin does, and is for a
change of its own.

## Network selection stays explicit and FAT paths name one entry

Status: decided (#73)

`board.modem.network().broker` selects a modem network explicitly. The existing `broker` fixture
remains available, but the proposed `board.broker` property is removed: unlike other interface
properties it selected a source as a side effect, and its transport would be ambiguous with
future network interfaces.

Card readers expose `FileNotFoundError`, `NotADirectoryError` and `IsADirectoryError` for path
errors. Directory-entry inspection establishes those conditions, while tool failures remain
`SourceError`. All builder/reader paths reject brackets along with `*` and `?`: mtools expands
bracket character classes even without a shell, potentially concatenating unrelated file data.
This also rejects literal bracket names created outside the API.

**Rejected:** parsing mtools stderr to classify missing files, because its wording can depend on
locale; and accepting patterns in single-file reads, because an assertion must inspect the one
file it names. Supporting literal bracket names through escaping is deferred; explicit rejection
keeps the currently documented no-pattern contract uniform across all commands.

## Network failure controls stay in the models and tracker reports stay in the example

Status: decided (#72)

NTP exposes `go_silent()` and `resume()` so tests describe an unavailable time server without
editing the UDP service table. TLS credentials accept `server_not_before` / `server_not_after`
as aware datetimes, changing only the leaf certificate while keeping the CA valid. The default
network behavior and certificate dates remain unchanged.

The position tracker interprets its JSON through a local `Report`/`Reports` adapter with future
message conditions and topic-specific history. Its scenarios now follow the SD logger's public
`Board` API: interfaces come from `board`, and network setup is an explicit call in the test.
The local `serve_broker(board, ca=None)` helper groups the repeated network selection, CA creation
and TLS registration, returning the existing network and listener. Custom CAs remain explicit.
It lives in the example, not the library, and does not provision NVS, power on or wait.
`Reports(network.broker)` is constructed separately. This supersedes both the example's setup
fixtures and its repeated three-line network setup. Firmware provisioning remains
a separate NVS-only operation. `when_received(with_position=...)` names condition construction,
not dequeueing.
Power-on and every wait remain explicit calls on `board`. Report conditions are callable
objects whose representation names the expected report, using the board's existing diagnostic
path, as GPIO conditions do. They do not rely on an unused function `description` attribute.
Report and TLS conditions reject boolean coercion, so asserting on a condition object cannot
silently pass without evaluating it.

The NTP outage scenario observes a fixed interval after time-server resolution, then restores
NTP and checks a positioned report against current world UTC. Stopping at the first waiting-log
line would miss a later attempt to connect without synchronization and would not test recovery.

TLS listeners expose cumulative started/completed counts and named callable conditions over
those counts, and are returned by `network.listen(..., tls=ca)`. Completion is the server's TLS
handshake success, distinct from MQTT activity; conditions retain history after closure.
Rejection tests wait for actual input and inspect broker history after a bounded interval; they do not infer absence from a firmware error string.

**Rejected:** a second world/context object or fixture graph hiding network selection and TLS
registration, because the board already owns interfaces and the scenario should show what it
connects; a reports fixture that implicitly installs TLS, because it conflicts with custom
certificate setups; matching certificate-error log wording as the rejection oracle, because it
couples behavior tests to diagnostics and says nothing about later reconnects or fallback.

**Rejected:** implementing cryptographic certificate construction separately in each consumer
or removing service-map entries to simulate an NTP outage, because these hide the intended world
state behind protocol details; teaching esp32tl about the tracker's fields, topic or NVS keys,
because product vocabulary belongs in the consumer's tests; and a wrapper that owns another
clock or implicitly powers on the board, because the scenarios should show when firmware runs.


## A run with a real source is on the wall clock from power-on, and stays on the test's clock

Status: decided (#27)

A test that gives a route a `real` source before `board.power_on()` gets a run under `--realtime`
whose `BoardLink` and taps are on the `wall` clock, every route and the console included, with no
clock switch ([protocol 12.4](../protocol.md#124-the-backend-and-recordings)). The run is still
held while the test body runs, as [the board's clock is the test's](#the-boards-clock-is-the-tests-emulated-time-moves-only-while-the-test-waits)
has it, and a replay in it takes only a `wall` recording, under the existing rule that a replay
refuses another clock than its backend's.

**Rejected:** a clock switch at the first byte from the device, which plan §4.3 and §11 kept the
switch for, because the run is paced from power-on and every time in it is a wall time already.
Also rejected: the `wall` clock on the real route alone, because when the firmware sends on every
other route depends on when the device's bytes came; and letting the run go while the test body
runs, as on `serial`, because a test would then have two models of time, which #64 rejected, while
the held clock costs only a catch-up of under half a second, or a skipped lag past that.

## The real source stamps what the port gave with the turn it hands it over in, every emulated millisecond

Status: decided (#27)

At each turn the `real` source sends what its host port gave since the last one as one `uart_rx`
stamped with the turn's `sync` `t`, declares no `latency`, and is quiet only until the next whole
emulated millisecond ([protocol 12.3](../protocol.md#123-the-real-source)). A byte thus gets the
emulated time current when it arrived, at most a millisecond after it while the guest keeps up
with wall time, and never a time before the turn (rule 2).

**Rejected:** stamping each byte with its own arrival time through a wall clock in the harness,
because the anchor is the runner's, a copy would drift from it, and a time computed from a copy
that runs ahead of the runner's falls before the turn, which is a protocol error. Also rejected: a
`latency` in place of `quiet_until`, because `latency` gives a turn only after the firmware
transmits, and a GNSS receiver sends unprompted to a firmware that never does; a renewal longer
than a millisecond, because each millisecond more is a millisecond later on every stamp; and a
message per byte, because the runner paces a message's bytes on the line from its `t` anyway.

## A real source's port keeps the wire's baud, and a transmit at another rate fails the run

Status: decided (#27)

The `real` source opens its host port at the wire's baud, the board profile's line rate, and
keeps it for the run. A `uart_tx` at another rate, beyond the 3 % `matches_baud` allows, or with
no rate set, fails the run, as a `frm_err` does in the other direction: the port would put intact
bytes on the wire where a real one would carry garbage.

**Rejected:** following the route's baud from `uart_cfg`, because the port would then read the
device at the firmware's rate instead of its own: a firmware at the wrong rate would hear garbage,
the `uart_rx` would carry the route's baud, and the runner could no longer report the `frm_err`
that names both rates.

**Postponed:** firmware that negotiates its device's rate, as a GNSS driver that switches a
receiver to 115200 baud does, until an example needs it.

## A model answers at the profile's baud, not at the rate the firmware set

Status: postponed (#198)

A model on a route, esp32tl's or the test's, hears whatever the firmware transmits, at any rate, and
answers at the board profile's `baud` (`Wire.baud`); a firmware that receives at another rate, more
than 3 % off, loses the answers as frame errors, and the run fails on them
([what a profile's baud does](../features/board-profiles-user.md#what-a-routes-baud-does)). A
firmware that takes its rate from NVS is tested at a rate the test has a profile for: one local
profile per rate. That is the way round until a consumer needs a model to follow the firmware's
rate. The link already learns the firmware's receiving rate from `uart_cfg`
([protocol 5.3](../protocol.md#53-uart_cfg-ext)), so a model's route could put its bytes in at
that rate instead of the wire's; left to decide then is whether the model or the profile asks for
it, and what a model that times its frames by a silence worked out for one rate does at another. A
replay and a real device keep the profile's rate: a recording carries none, and the port runs at
it.

**Rejected:** making it the default for every model, because a model that answers at whatever rate
the firmware set would hide a firmware that sets the wrong one, which the device it stands for
would not understand and the frame errors now catch.

## A real source keeps at most 50 ms of bytes waiting on its line, and drops the oldest

Status: decided (#27)

*Its 50 ms changed in #28: [the real source's bound is at least 128 bytes on the line](#the-real-sources-bound-is-at-least-128-bytes-on-the-line-not-50-ms-at-any-rate).*

The `real` source works out from its own messages where its line will be free as the runner paces
it, and at each turn sends no more of what the port gave than the line carries in the next 50 ms,
the newest bytes, dropping the older ones and counting them for the test report
([protocol 12.3](../protocol.md#123-the-real-source)). A device's bytes from a held turn, or from
a stretch the guest fell behind in, cannot all reach the guest when they should: emulated time does
not move during the hold, and the wire carries no more than its line rate.

**Rejected:** keeping every byte, because the backlog stays on the line whatever the clock does: a
device sending at its full line rate, 9600 baud, through a 10 s hold leaves its bytes about 10 s
behind for the rest of the run, and an answer to a new command waits behind them until the
firmware times out. Also rejected: ending the run past a backlog, because a test body that takes a
second, or a test stopped in a debugger, would lose its run; dropping in the runner, because it
cannot tell a real source's line from a replay's, whose one message of a GNSS epoch can hold half a
second of line time on its own; dropping the newest, as a full RX FIFO does, because the guest
would go on reading the past while the device's answers are lost; and a bound of 500 ms, as long
as esp32sim lets a lag be caught up, because at full line rate it would hold every later answer
half a second late.

## The Wi-Fi station is an interface of every profile, and the access point a model source

Status: decided (#31)

Every classic ESP32 has the radio, so every board profile has `wifi` without listing it, last in
its interfaces, and the `wifi` fixture is `board.wifi`. What is on the air is what the test gives
before power-on: `wifi.access_point(...)` returns the network behind it, `wifi.nearby(...)` adds a
network a scan lists; both ask the capability declaration for a `model` source on the new kind
`wifi`, which `esp32sim` gives and `serial` does not, so a real board skips. Without an access
point the station finds nothing, as a board with none in range. The access point goes off the air
and comes back at the board's current instant, through `wifi.access_point_off()` and
`wifi.access_point_on()`, the way the test drives a line.

**Rejected:** a `[wifi]` table in the profiles, because nothing in it would differ from one board to
another; and the access point from the profile, because what is in range is the test's world, not
the board's wiring.

## Behind the access point the network speaks Ethernet, ARP and DHCP

Status: decided (#31)

The network behind the access point is `Internet` with an Ethernet link instead of PPP: the same
addresses as behind the modem (the device `10.64.0.2`, router and DNS `10.64.0.1`), handed out by
a DHCP server, ARP answered for every address but the device's own, and the same IPv4, TCP, UDP,
DNS, NTP and broker on top, so a firmware's test reads the same whichever link it uses. IPv6 frames
are dropped and counted: ESP-IDF's examples configure a link-local address, which needs no answer.

**Rejected:** answering ARP for the device's own address, because lwIP probes it before it takes a
lease (RFC 5227) and declines an address something answers for; and a subnet of its own for the
Wi-Fi network, which would make tests of one firmware differ by its link.

## An HTTP server answers on port 80 of every simulated network

Status: decided (#31)

`network.http` is an HTTP/1.1 server on TCP 80, behind the modem and behind the access point alike,
as the broker is on 1883: `serve(path, body)` says what a path answers, `requested(...)` is a
condition for a request (`next_request(...)` now, [Only a condition for the next event has the next
prefix](#only-a-condition-for-the-next-event-has-the-next-prefix)), and every request is kept. It
answers one request per connection and closes it, and refuses chunked bodies and heads or bodies
over its limits; firmware that fetches over HTTP (plan M7's validation, ESP-IDF's `http_request`)
needs nothing more yet.

**Rejected:** a server the test has to install with `network.listen(80, ...)` first, because a
test of a firmware that fetches would always do so; and keep-alive, chunked transfer and TLS by
default, until a firmware's test needs them (TLS is `network.listen(443, network.http, tls=ca)`
already).

## The HTTP client opens one plain connection per request

Status: decided (#197)

`network.http_client` sits beside `network.http` and uses the TCP client to call a device's
server. GET and POST return an exchange with a response condition; receipt records the result
in the existing network transcript and report even when that condition is never polled.
Responses support Content-Length, EOF framing and chunked transfer because ESP-IDF and Arduino
servers use them. The request always sends `Connection: close`.

**Rejected:** keep-alive and TLS client support until a firmware test needs them. Explicit
keep-alive requests/responses and HTTPS targets are refused. Raw `network.connect` remains
available for tests of persistent TCP connections. Redirects are left to the test so each
exchange describes exactly the requested path.

## A Wi-Fi tap, signal changes and the esp32tl command's Wi-Fi are postponed

Status: postponed (#31)

What crosses the air is in the network's transcript, plain text after TLS, and nothing records the
Ethernet frames themselves, as a pcap could; the signal of the access point is esp32sim's fixed
-40 dBm and does not change during a run; `esp32tl run` and `esp32tl replay` pass the runner no
`--wifi`. Each comes when a test or a run by hand needs it.

## Explicit build paths are relative to the application and never fall back

Status: decided (#102)

*Extended in #192 by [A PlatformIO build is read by its file names and image
headers](#a-platformio-build-is-read-by-its-file-names-and-image-headers).*

`@pytest.mark.app_path(path, build_dir="../builds/debug")` selects an existing directory containing
`flasher_args.json`. The build defaults to `build`; explicit paths may be absolute or relative to
the application. `esp32tl run` and `esp32tl replay` offer `--build-dir` with the same semantics.
The app path itself remains relative to the test file in pytest and to the working directory in
the CLI. Resolving the build to an absolute path before passing it to pytest-embedded prevents
its cwd fallback. Both backends retain target, encryption and missing-file validation; serial
hashes the selected artifact before opening a port. Existing repository firmware provenance checks
remain separate and unchanged; external builds do not need repository build stamps.

It extends earlier decisions, which otherwise hold. The single parametrisation of
[board, embedded_services and app_path](#the-board-fixture-is-parametrised-over-board-embedded_services-and-app_path-together)
takes `build_dir` as its fourth name. [The `app_path` marker](#the-app-comes-from-the-app_path-marker)
gets an optional `build_dir` keyword; the closest marker supplies both values, without inheriting
keywords from more distant markers. That keyword is the supported pytest selector:
[pytest-embedded's `--build-dir`](#pytest-embeddeds-app-and-service-options-are-refused-for-board-tests)
stays refused, even when it agrees with the marker. `load_app` builds
`IdfApp(app_path, build_dir=str(build))` with `build` the resolved selected directory, still
[an absolute path](#the-build-directory-is-passed-to-idfapp-as-an-absolute-path); the plugin passes
the same absolute `build_dir` fixture to simulator loading, serial preflight and pytest-embedded's
app and flash chain, and the CLI resolves its selector through the same loader. Serial's
[`load_app(app_path, build_dir=build_dir, backend="serial")`](#serial-validation-accepts-unencrypted-classic-esp32-builds-only)
applies its checks to the selected build.

**Rejected:** resolving build paths against the test directory or cwd, because app-relative paths
keep pytest and the CLI consistent and preserve the meaning of the default `build`.
Also rejected: falling back to another build, rebuilding, relocating artifacts or modifying
sources, because a test or CI job must run exactly the artifact it selected. Pytest's raw
`--build-dir` remains refused; allowing two competing selectors would obscure that selection.

## MQTT, TLS, HTTP and DHCP event APIs name history queries and next-event conditions explicitly

Status: decided (#108, #116, #182)

MQTT exposes immediate `find_message` / `find_connection` history queries and callable
`next_message` / `next_connection` conditions. The existing `message` / `connected` names kept their
future-only semantics, until they were removed ([Only a condition for the next event has the next
prefix](#only-a-condition-for-the-next-event-has-the-next-prefix)). `subscribed` continues to
describe current subscription state. TLS retains cumulative `handshake_started` /
`handshake_completed` properties and adds `next_handshake_started()` / `next_handshake_completed()`
factories. MQTT conditions now name their expected event through `repr` and reject boolean coercion
like TLS and report conditions.

Next-event construction captures the relevant list length or counter, not a timestamp or the
first polling time. Appends and counter increments order equal-time events. Polling consumes
nothing and never moves the boundary; a new wait for another event needs a new condition.
TLS completion means a completion event after construction, including an already-started
handshake. Tests must arm conditions before the action that can produce their event. Immediate
history queries find a fast publication even after waiting for its connection has returned.
Application payload schemas stay in the consumer's adapters. `connection_attempt()`, from #117,
took the same condition object and boundary but kept its name; it got no `next_*` alias and
no `find_*` query beside the public `connection_attempts` history. It is
`next_connection_attempt()` now ([Only a condition for the next event has the next
prefix](#only-a-condition-for-the-next-event-has-the-next-prefix)).

HTTP and DHCP (#116) take the same boundary, names and condition object: `next_request` /
`find_request` and `next_lease` / `find_lease`, with `requested` / `leased` kept as aliases of the
`next_*` names, until they were removed with MQTT's `message` / `connected` ([Only a condition for
the next event has the next prefix](#only-a-condition-for-the-next-event-has-the-next-prefix)).
`find_request` filters by path and method as `next_request` does; `find_lease` reads the first
lease, as `find_connection` reads the first CONNECT. The condition object, shared by MQTT, HTTP and
DHCP, names the service in its `TypeError`; since #181 it is the public one of [A consumer names a
condition with esp32tl.condition, not with a description on the
wait](#a-consumer-names-a-condition-with-esp32tlcondition-not-with-a-description-on-the-wait), which
TLS uses too. `next_request` and `find_request` refuse a path filter holding `?` with `ValueError`,
because `Request.path` has no query and `assert find_request("/a?b") is None` would otherwise pass
for a request the firmware did send. `serve` refuses one too, since it could only ever lead to a
`404`.

A check over a stream of MQTT publications, not one, needs every match after a point (#182).
`broker.mark()` returns an opaque `Mark` at the end of `messages`, and `find_messages(topic="#",
payload=None, *, since=None, client=None)` returns every match after it in append order. The mark
is the message history's own boundary, taken as `next_message` takes its own: a message appended
after the mark is included even at the mark's board time. A mark from another broker raises
`ValueError`, and a `since` that is not a mark, such as the `len(broker.messages)` consumers took
by hand, raises `TypeError` naming `broker.mark()`. `client=` takes a client ID, and the test's own
client name is exported as `esp32tl.net.mqtt.TEST_CLIENT`, so neither the device-only case nor the
test-only case needs index arithmetic or the `"(test)"` literal. `messages` stays the read-only
history it was; the position tracker's `Reports` reads through `mark` and `find_messages`.

**Rejected:** changing existing MQTT defaults to include history, which could silently satisfy
reconnect waits with stale events; making every condition future-only, which loses useful
current-state and cumulative queries. A public shared cursor is unnecessary for these scopes:
explicit methods express them without requiring callers to compare timestamps, edit counters
or manage cursor ownership across brokers and TLS listeners. Also rejected: matching a filter
holding `?` against `Request.target`, which would give `path` two meanings depending on its
content.

**Rejected (#182):** a `from_test: bool` filter, because a client ID covers both the device-only
and the test-only case and a boolean would cover neither when two devices publish; a timestamp
as the mark, because equal-time records would fall on either side of it; a public index, which
is the arithmetic consumers copied; and a `since=` on `next_message`, because a condition already
takes its boundary at construction. The mark is not shared with `clients`, `deliveries` or the
other services, so the shared cursor rejected above stays rejected.

## Only a condition for the next event has the next prefix

Status: decided (#200)

A condition that sees only what happens after the call that makes it is named `next_*`, and nothing
else is: the aliases MQTT's `message` and `connected`, HTTP's `requested` and DHCP's `leased` are
gone, and MQTT's `connection_attempt()` is `next_connection_attempt()`. A first consumer used
`network.dhcp.leased()` as if it returned the lease, beside `network.dhcp.leases`, the list, and
printed `the next DHCP lease` where it wanted an address: a name in the past tense or a noun reads
as a state or a value that is there already, which `leased()` was not, since it saw only what came
after it. With no users yet, the aliases go without a deprecation period. The names that stay:
`find_*`, which returns a record now; the plural histories (`messages`, `clients`, `leases`,
`connection_attempts`); and the conditions named for a state, which are true while it holds or once
it has: MQTT's `subscribed(topic)`, TLS's `handshake_started` and `handshake_completed`, a TCP
connection's `established`, `closed`, `refused` and `reset` (#199), which return their event's time
however long ago it happened, and the output lines' `is_on` and `is_off`. A TCP connection's
`next_data` is a condition for the next bytes and has the prefix. `console.line(pattern)` keeps its
name: it is a condition for the next line, but it reads as one, and a `next_line` beside
`lines()` would split the console's vocabulary from pytest-embedded's `expect`.

**Rejected:** keeping the aliases for one release with a `DeprecationWarning`, because nobody
depends on them and every alias is one more name to explain beside the one the documentation
recommends. Renaming `subscribed` to `next_subscription`, because it reads current state, not the
next event, and the rename would make it say the wrong thing.

## A reset and a power cycle go on in the run they happen in, on its clock

Status: decided (#105)

`board.reset()`, `board.power_off()` and a `board.power_on()` after it are inputs of the run the
first `power_on()` started: the backend sends them to the runner at the board's time the test
calls them, and the runner resets or idles the chip in the machine it has. One runner, one link
and one clock serve the whole test, so `board.now`, `board.utc`, the console's lines, the run's
log and every tap's recording go on through every boot, and the models and simulated services on
the link see the time between two boots pass in turns, as any other. The flash the firmware
writes to is the runner's copy, which a power cycle keeps; the build's files are only read. What
the firmware counts from its own boot starts again: a test reads it as
`board.now - board.boot.at`. `board.boots` comes from the link's `reset` messages, the first boot
at 0 with cause 1, so it holds every reset, the firmware's own and its watchdogs' included.

This extends [a run starts at board.power_on(), and so does the runner](#a-run-starts-at-boardpower_on-and-so-does-the-runner):
the first `power_on()` still starts the runner, and sources and taps are still given before it,
also when a later boot would find them. `board.nvs()` was too, until #278 let it write while the
board is off between two boots ([board.nvs merges by values, before power-on and between
boots](#boardnvs-merges-by-values-before-power-on-and-between-boots)).

**Rejected:** ending the runner at `power_off()` and starting a new one at `power_on()`, with the
flash dumped to a file in between, because the new run's clock and turns would start again from 0,
every model and the network would see a gap that is no time at all, and the runner would need a
way to write its flash out that the protocol does not have; and restarting `board.now` at each
boot, because the world's time and the services' history would then have to be kept apart from
the board's clock, which is what every other time of the run is on.

## board.reset() is EN, a power-on reset of the chip; power_off() is the board's supply

Status: decided (#105)

`board.reset()` pulses the chip's EN pin, as a devkit's reset button does: on the ESP32, EN low
holds the whole chip, the RTC domain included, in reset, and the ROM reports `POWERON_RESET`. So
the chip starts with zeroed SRAM and RTC memory, as at the first power-on, and the rest of the
board keeps its power: the SD card stays initialised, the modem and the receiver go on. The
pulse takes no time. `board.power_off()` switches the board's supply off: the chip and every part
on the supply lose their power, and `board.power_on()` brings them back, the chip through the same
power-on reset. The board's clock runs on while it is off, and the test can wait.

**Rejected:** a reset that keeps SRAM and RTC memory, as `reboot()` does for `esp_restart()`,
because that is the firmware's own restart, which a test can already cause through the firmware,
and the chip does not do it at EN; one call with a domain argument, `board.power(domain=...)`,
because two names say which supply each one acts on without a parameter to get wrong; and a
`hold` for how long EN stays low, which no firmware needs yet: the chip does nothing while held.

## Every interface of a profile is on the board's supply

Status: postponed (#105)

A power cycle takes the supply of every interface a board profile lists: the GNSS receiver and
modem models lose their power (the receiver is silent, the modem off until its PWRKEY pulse), and
so does the SD card in the slot. On the profiles esp32tl ships that is the board as it is: the
LilyGO board's modem, receiver and slot are on it, and a devkit assembly wires its receiver and
card to the devkit's 3V3. What is beyond the board, the broker, the access point and the network,
keeps its power. A replay or a real device at the far end of a route is not a part of the board
and keeps sending. A profile key that puts a part on a supply of its own, and a model of
`lines.board.power_enable` gating one, wait for a board that needs them, like
[telling on-board from off-board interfaces](#telling-on-board-from-off-board-interfaces-is-postponed).

## Torn flash writes and what RAM holds after a reset are not modelled

Status: postponed (#105)

esp32sim carries out each flash program and erase command whole when the guest issues it, so a
power cut falls between two commands, and the power-on reset zeroes SRAM and RTC memory. A real
chip can lose power in the middle of a page program or a sector erase, and its RAM holds what it
holds after a reset. The documentation states both, and the tests rely on neither. A model of
interrupted flash operations needs flash timing esp32sim does not have, and comes with a firmware
test that needs it.

## On serial, reset() pulses EN and power_off() and boots skip

Status: decided (#105)

The serial backend's `board.reset()` is pytest-embedded's `hard_reset()`, EN through the USB
adapter's RTS, as its `power_on()` is. It cannot switch a real board's supply, and it sees the
chip's resets only as console text: `board.power_off()`, `board.boots` and `board.boot` skip the
test at the line that asks, as a source the backend cannot give does.

**Rejected:** reading the ROM's `rst:0x..` banner from the console as the serial backend's boots,
because a firmware that silences UART0, or a reset during the banner, would leave the list wrong
without a sign.

## real(port) skips on serial and leaves the port closed

Status: decided (#28)

`gnss.real(port)` on the `serial` backend skips the test at the call, with `serial: uart.gnss has
the device wired to the board at its far end: a host serial port goes there on the emulator only`, and
opens nothing. `real` stays in the backend's capability declaration, where it means the device wired
to the board, what a console-only test runs against.

**Rejected:** running the test against the board's own device and leaving `port` closed, because the
test named a port and would be told it had talked to a device on it. Also rejected: taking `real` out
of the declaration, which would change `serial: uart.gnss takes no replay source on this backend, only
real`, a true sentence about the board, and which still leaves no way to say what `real(port)` needs.

## A real source refuses a replay given before it, and a replay after it takes the wall clock

Status: decided (#28)

A real source puts the backend on the wall clock at the call, so a replay after it is checked against
that clock under the existing rule. A replay given before it was checked against the emulated clock,
so the real source refuses it, with `{route}: a real source puts the run on the wall clock, and {other}
replays a recording on the emulated-fifo-write clock`. A test that wants both gives the real source
first.

**Rejected:** checking replays again at `board.power_on()`, which would accept a wall recording given
first, because the call that is wrong would then not be the call that raises, and a recording's clock
is refused at the call everywhere else. Also rejected: a flag on the test that says it is on the wall
clock, a second statement of what `gnss.real` already says.

## The last stretch of the wait for wall time is a sleep, not a read with a timeout

Status: decided (#28)

The runner reads the socket while more than a 20 ms step of the wait is left, then sleeps the rest
([protocol 12.2](../protocol.md#122-the-runner-waits-for-wall-time-at-the-block-point)).

**Rejected:** a read with the time left as its timeout throughout, as #27 designed it, because
`SO_RCVTIMEO` is rounded up to the kernel's tick, 4 ms at Linux's usual 250 Hz, on a wait of under
2.3 ms: every turn would then be late by a tick, and the guest would spend the run catching up. What
the sleep costs is that a signal or a closed socket waits for the turn that follows, at most 20 ms on.
Also rejected: `poll(2)` through `libc`, a dependency for the same behaviour.

## A device that delivers faster than the route's line rate loses bytes to the bound

Status: decided (#28)

The real source keeps the bound of [protocol 12.3](../protocol.md#123-the-real-source) for every
device. A burst larger than what the line carries in the bound loses its older bytes, also while the
guest keeps up: a receiver with native USB at 9600 baud loses most of each epoch. The report's `dropped`
shows it, and the user's document says what to do.

**Postponed:** a bound the test can set, or one that grows with a burst, until a device needs it; a
line kept further behind is what the bound exists to prevent, so each such device has to be weighed
against it.

## The real source's bound is at least 128 bytes on the line, not 50 ms at any rate

Status: decided (#28)

The bound is 50 ms, or as long as 128 bytes take on the line if that is longer. This changes
[the 50 ms of #27](#a-real-source-keeps-at-most-50-ms-of-bytes-waiting-on-its-line-and-drops-the-oldest),
which took an FTDI adapter's 16 ms burst as the largest. Validating on a T-A7670G, whose CH9102 hands
over 64 bytes at once at 9600 baud, every 67 ms, 50 ms of that line carried 48 of them: 3396 of about
14900 bytes were dropped in 30 s, and most sentences cut, with no resync. With the 128 bytes, the same
30 s on the same receiver dropped none, and all 359 sentences had a valid checksum.

**Rejected:** room for one packet, 64 bytes, because the next can come a moment before the line is free
of the last, and would then lose a byte or more; and a longer bound at every rate, because it would keep
a fast line further behind after a hold than it needs. What 128 bytes cost is an answer after a hold
up to 133 ms late at 9600 baud, instead of 50.

## The real source writes its port from a thread of its own, and limits only a port that takes nothing

Status: decided (#28)

`Real.receive` queues what the firmware transmits, and a thread writes the queue into the port's
non-blocking descriptor with `os.write`. A port that takes no byte for 5 wall seconds, or fails, fails the
run at its next turn. esp32sim's UART reads as idle, so a firmware can send up to the runner's 48 KiB
`uart_tx` far faster than a line carries it; the run does not wait for the line.

**Rejected:** writing in the link's thread with a limit on progress, because every turn would then
stand for as long as the port takes, the guest would fall behind wall time, and the reading thread would
cut the device's bytes at the bound meanwhile. Also rejected: pyserial's `write` with its timeout. The
timeout covers the whole of a write, so that a burst of 8 KiB at 9600 baud fails a run whose device is
reading; and writing small pieces does not save it, because `write` waits in `select` after each, and
Linux says a tty is writable only once under 256 characters are left. A limit then has to outlast the
whole buffer draining, about 19 KiB on a pty; a driver that holds 4 KiB takes about 4 s at 9600 baud and twice that at 4800. Also
rejected: holding the firmware to the line's rate, which would take a model of the transmit FIFO that the
emulator does not have, and which would be esp32sim's to give.

## The real source's port lock is the advisory one, not TIOCEXCL

Status: decided (#28)

`Real` opens the port with pyserial's `exclusive=True`, an advisory `flock`, which stops a second route
and a program that asks for the same lock. The user's document says to close anything else that holds
the port, since a program without the lock takes bytes from the firmware and `dropped` cannot count them.

**Rejected:** adding `TIOCEXCL`, because it blocks later opens only: a program that already holds the
port, the case that loses bytes in silence, goes unseen, and the lock already stops the later open that
matters, a second route's.

## A real board does not read its flash: the test skips

Status: decided (#131)

`board.flash()` on the serial backend skips the test, with
`serial: flash contents cannot be read on this backend`, as `board.nvs` does.

**Rejected:** reading through esptool, because it puts the chip in download mode, which stops the
firmware and breaks the run the test is observing, and pytest-embedded owns the port. Having the
firmware print its own state is a test's own choice, and reaches any backend through `console`.

## The reader reads NVS itself, from the format

Status: decided (#131)

`esp32tl.flash.nvs_values` reads pages, the state bitmap, repeated entries and blobs by ESP-IDF's
format and loader, on the same `FlashSnapshot` a test reads partitions from.

**Rejected:** ESP-IDF's `nvs_partition_tool/nvs_parser.py` as a dependency: it is not in the pip
package esp32tl installs, which has the generator only, and needs an ESP-IDF checkout. Its
`NVS_Partition` also lists entries and leaves deciding what counts to the reader, so the rules would
still be here. It served as a check while this was written.

## A snapshot's errors are ValueError, not BackendError

Status: decided (#131)

`FlashSnapshot` reports an unknown partition and one that is not unencrypted NVS as `ValueError`, with
the message `board.nvs` uses for a missing name, and `board.flash()` before power-on as `BoardOff`.

**Rejected:** `BackendError`, because `FlashSnapshot` lives in `esp32tl/flash.py`, which the backends
import, and a snapshot outlives its run; a class of its own would give a test nothing to catch.

## The backend comparison compares outcomes of one run on each backend

Status: decided (#26)

`scripts/compare-backends.py` runs pytest once with `--esp32tl-backend=serial`, then once with
`--esp32tl-backend=esp32sim`, and compares each test's outcome in the two JUnit XML files: passed,
failed or skipped. Serial goes first, so that a port the plugin refuses ends the script before the
esp32sim run. It matches a test's two cases by their id without the backend's parameter, and
leaves out an id that is the same in both runs, which is a test without `board`. A test that failed
on both agrees and still fails the script; so does one that ran on serial and was skipped on
esp32sim, which leaves the real board's result with nothing to be compared with. The
specification (issue #1) says "Every test that a real board can run, runs on one too, and the two
results are compared".

**Rejected:** one pytest session on both backends, because `--esp32tl-backend` names one backend
and serial stays opt-in: a session without the option runs esp32sim alone. Comparing console
output or times, because esp32sim stamps the console on the emulated clock and serial after a
reader poll on wall time, with USB, driver and host scheduling delays. A property the plugin
records with the backend of each case, because the id already carries it, and the properties the
`board` fixture records are missing from a test skipped before the fixture runs. Checking the port
in the script before the first run, because the plugin's checks would then live in two places, and
the script uses the standard library only; running serial first gets the plugin's answer before
any test has run.

## A UART takes a model of the test's as the four-method Source, through Uart.model

Status: decided (#161)

`board.uart(role).model(source)` gives a route a model the test wrote, with the `Source` protocol
the route's other sources and the SPP peer's already use: `receive(t, data)`, `poll(now)`,
`quiet_until()` and `latency()`, and `supply(on)` when the part is on the board's supply. It is a
source of kind `model`, asked as one, so the backends' capability checks apply and `serial` skips
it; before power-on, one per route. The report and a tap name it by its class
(`model:AtResponder`), which is what the test called it. An object that lacks one of the four
methods, or has one that cannot be called, is a `TypeError` at the call, naming them, before the
backend is asked anything; a method a proxy's `__getattr__` gives counts. The check is
`callable(getattr(...))` per method, not `isinstance` on a `runtime_checkable` protocol, which
since Python 3.12 asks only whether the attributes exist.
The call returns the source, typed as given, so the test reads its own model's state without a
cast. `Modem.model()`, which returned the modem model, became `Modem.modem_model()`: `Uart.model`
is the call that parallels `replay(path)` and `real(port)`, and a subclass cannot take the name
for something else.

**Rejected:** a smaller protocol for a model that only answers, `answer(t, data)` with a fixed
latency, because the link needs `latency()` and `quiet_until()` to know how far the board may run
ahead, and a fixed default would be wrong for a part that answers slowly or sends unprompted; the
four methods cost such a model two one-line returns. Calling a model that lacks `poll`,
`quiet_until` or `latency` with defaults filled in, because a misspelt method would then be a model
that silently does nothing. Refusing a `Replay` or a `Real` given to `model`, because
`replay(path)` and `real(port)` are the calls a test has for them, and building one by hand is not
a mistake a test makes by accident.
A name of the test's for the report (`model(source, name=...)`), because no test needs one yet and
the class's name says what it is.

## A model of the test's may state its times in Durations, as a subclass of Model

Status: decided (#183)

`esp32tl.sources.model.Model` is the base of a model whose `receive(at, data)`, `poll(now)`,
`quiet_until()` and `latency()` take and return `Duration`s, as everything else a test states
does. `Uart.model` and `SppPeer(source=...)` take one, or a source in nanoseconds as before: they
tell them apart by `isinstance(source, Model)`, keep `source` as the test gave it, and call a
`Model` through `in_nanoseconds`, which converts each time at the edge. The link and its protocol
see nanoseconds either way, so the two kinds put the same bytes in at the same instants. What a
`Model` gets wrong is checked as a source in nanoseconds is, with the messages in `Duration`s: the
adapter refuses what is not a `Duration`, and the link keeps its checks of order, one set for both
kinds, writing the times of its errors with the interface's `instant(t)`, which `Uart` makes a
`Duration` while its source is a `Model`; `SppPeer`'s checks of order already spoke in seconds.
The four methods are abstract, so a `Model` that lacks one, or misspells it, cannot be made.

**Rejected:** changing `Source` itself to `Duration`s, because every existing model would break,
for what the adapter gives as well. Defaults for the four methods on `Model`, for the reason the
`Uart.model` decision above gives against filling them in: a misspelt method would be a model that
silently does nothing. Telling a model in `Duration`s by what it returns, because a model that
answers nothing has nothing to tell by, and the first wrong guess would be a confusing error. A
proxy of a `Model` taken as one, because `isinstance` does not see through `__getattr__`; a proxy
of a `Model` is called in nanoseconds, as any other proxy is, and the documentation says so; one
whose `__class__` is the model's is a `Model`. Copying the link's checks of order into `Uart` for a
`Model`, with the answers and the turn the link keeps, because the copy drifts when the link's
checks change, and reports another fault first when two interfaces fail in one turn: the link
polls every interface before it checks a stamp.

## A model that only answers, or frames by silence, starts from a base that fills in the rest

Status: decided (#202)

*Extended in #237 by [A model that frames by silence tells on_frame when the frame's
data came](#a-model-that-frames-by-silence-tells-on_frame-when-the-frames-data-came).*

`ReactiveModel` and `IdleFramedModel`, in `esp32tl.sources.model`, are `Model`s with all but one
method defined. `ReactiveModel` sends nothing unprompted and takes `latency()` from its
`answer_after`; a subclass defines `receive`. `IdleFramedModel` keeps what it hears until `gap` has
passed since the last piece came, through `quiet_until` and `poll`, and hands each frame to
`on_frame(at, frame)`, the method a subclass defines; `latency()` is `gap`, so that the board stops
when a frame may have ended; `drop()` forgets the frame heard so far, for a subclass whose part
loses it with its power. `Model` stays as it was, with its four abstract methods, for a model that
sends unprompted. The gap counts from the `at` of the last piece, the instant the firmware wrote its
first byte into the TX FIFO: esp32sim does not pace a UART's transmission, so what the firmware
writes in one go comes at about one instant, and the gaps between pieces are the firmware's own
pauses (`tests/test_model_bases.py` shows it with `modem_at`). The one case it gets wrong: a frame
the firmware writes byte by byte, each less than a character time after the one before but over more
than `gap` in all, is one piece to the link, at its first byte, and ends `gap` after that byte, cut
in two, where a real line would carry it without a silence. In the emulator a loop's writes are
microseconds apart, so the case is rare, and the model cannot see it without the instant of the
piece's last byte, which the link does not give.

This narrows the rejection, in the decision above, of defaults for the four methods on `Model`: a
misspelt `receive` or `on_frame` still cannot be made, and only a subclass that chose a base which
sends nothing unprompted gets the empty `poll`. A model of a part that speaks unprompted
subclasses `Model`, as before.

**Rejected:** `answer_after` as an argument of the base's `__init__`, because a subclass with an
`__init__` of its own would have to call it, and a dataclass subclass, as `Elm327`, would lose it;
a class attribute or a field is read where `latency()` needs it, and its absence is a `TypeError`
that says what to set. Telling the model the route's baud and computing the gap from it, because
`Model` gets no baud today and RTU's gap is not always 3.5 characters (1.75 ms above 19200 baud):
the test states the `Duration`. Counting the gap from the estimated end of a piece, `at` plus its
length in character times, because the emulator does not put the bytes on the wire at the baud:
a firmware that waits 3.5 characters after its transmitter is done would see its next frame merged
into the last. Delivering a frame from `receive`, when the next piece shows the silence was long
enough, because the last frame before a long pause would wait for the firmware's next request.

## The wait for a wake from deep sleep is a board call that asks for a turn after each event

Status: decided (#36)

`board.wait_for_wake(within=...)` runs the board to the chip's next boot and returns it; the boot
must be a `DEEPSLEEP_RESET`. It is a call on the test's clock, beside `wait` and `wait_until`
([the board's clock is the test's](#the-boards-clock-is-the-tests-emulated-time-moves-only-while-the-test-waits)),
not a `scenario` call as plan M9 named it: there is no `scenario` fixture since #64. The test's
clock lets the run go to the end of `within` at once and returns a `RunTo` with a `latency` of
1 ms to the link, so the runner, which sets the block point at most `latency` after each event,
takes a turn right after the `reset` and none in the sleep. The runner already ends a slice at
every emulated millisecond while the chip runs, and in a deep sleep ends none for the latency
([decision](runner.md#a-deep-sleep-runs-in-long-steps-of-the-rtc-to-just-short-of-its-alarm)),
so 1 ms costs it nothing. The wire is version 1 as it was: `latency` is the `wait`'s own field.

**Rejected:** `board.wait_until(lambda: len(board.boots) > n, within=...)`, which a test can
already write, because its 100 ms steps cost a turn of the link each, 20 000 for the 2000 s sleep
the production image takes, and return up to 100 ms after the wake. A runner change that makes
every `reset` a block point, because a hold's latency gets the same turn without a new rule in the
protocol, and only while the test asks for it: other waits keep their turns, and a determinism
check's turn counts stay the same. A latency of 0 or of a microsecond, because while no event is
unacknowledged the runner's slices are at most `latency` long (protocol §6.1), so a smaller one
multiplies the slices of the whole sleep. Returning any boot, a restart or a watchdog's included,
because a test that waits for a wake and gets a crash should fail at the call that assumed the
sleep, not at a later expect that cannot say why. Skipping the sleep's emulated time without
running it, because the chip's RTC timer is what wakes it and what the firmware reads after the
wake; the runner runs the RTC through the sleep in long steps instead
([decision](runner.md#a-deep-sleep-runs-in-long-steps-of-the-rtc-to-just-short-of-its-alarm)).

## A test's wall-time bound proves what was not waited for, and a paced one holds only while the host kept up

Status: decided (#176)

In the CI run of #174 one shard on hp600g1 took 353 s and 440 s on Python 3.13, against 169 s and
185 s for the same 487 tests on 3.11 and 3.14 in the same run: the host was busy with something
else, and every test file in the shard was 2.1 to 3.4 times slower. What failed were wall-clock
bounds only: a day of periodic wakes against 60 s, and the long-hold test of the `real` source,
whose answer came 856 and 1210 ms after the hold against 500 ms. A test in the suite reads the wall
clock for one of three reasons, and each has its rule
([testing](../guides/testing.md#wall-time-in-the-tests)):

- A hang guard, a deadline or a timeout that fails only when something never happens, keeps a
  generous margin. The inner run of `test_a_hung_run_is_ended_by_the_test_s_timeout` took 11.2 s
  of its 15 s under load, and gets 60 s.
- A speed bound that proves something is skipped or not waited for gives way to what the run leaves
  behind: the board's time at which the run ended, the runner's clock, the child still holding
  the link, the reader still stuck, the runner process gone. Where nothing is left to show it, the
  bound is relative to what is avoided, and says so: half of `WRITE_SECONDS`, a hang guard of a
  fifth of the plugin's 600 s or of the fake runner's 300 s sleep, and a day of wakes whose sleeps
  take less wall time than its boots in the same run, about 0.06 of it on an M2 Pro, 0.15 under
  80 busy loops, and some 47 times it without the runner's steps through the sleeps.
- Under `--realtime` wall time is the feature. The bound on stamps holds only while the guest kept
  up at the turns that stamped the bytes, which the test measures from the wall time at which the
  harness took each turn: the runner sends no turn before its wall clock reaches it (protocol 12.2),
  so the earliest turn, by wall time less the board's, is one that kept up. The test skips when half
  or more of the bytes' turns are more than 5 ms behind it and none more than 500 ms, the lag a
  paced guest can have, and on a resync as before; a lag at fewer of them moves none of the stamps
  its median holds. The skip cannot tell a busy host from a harness slow to take its turns: its
  reason, with the lags, is in the short summary of every run (`-ra`). The long hold's bound was on
  the board's clock all along: the excess was the test's own device thread, slow to say the answer
  on a busy host, and the bound now runs from the stamp of the answer's line ending, which no host
  speed moves.

Lower bounds, at least `AT_LEAST_S` of wall time for a second of the board's, only grow on a slow
host, and stay.

**Rejected:** raising the numbers, because the next heavier load fails them again, and a bound far
above the cost stops proving anything. pytest-rerunfailures or `flaky` markers, because a real
regression would pass on the second try. Moving the tests out of CI, or behind a marker CI does not
select, because they are what shows the properties hold. Skipping the stamps' test on a resync
alone, because a lag under the 500 ms esp32sim skips leaves none: under 80 busy loops a run with no
resync had the guest 273 ms behind at a turn that stamped a byte, and failed. Taking the measured
lag out of each stamp, because in a run that is not paced it would explain the stamps away too.
A skip on host lag for the long hold, because its bound is on the board's clock once it starts at
the stamp. A count of the sleeps the runner stepped through in `stats`, which would make the day of
wakes exact, because it changes the wire for one test, and the runner's tests already prove the
step for a sleep it recognises (`a_deep_sleep_is_one_slice_to_its_wake`,
`on_the_emulator_a_skipped_sleep_wakes_at_the_tick_of_its_alarm`); what the example adds is that
ESP-IDF's own deep sleep is one, which the ratio shows. Ten fixes in five wall seconds for
`test_time_is_emulated`, because that is the speed of one host: the runner's `hello` naming the
emulated clock, and no `rt_resyncs` in its `stats`, show that the runner did not pace the run, and
the ten emulated seconds in under ten wall seconds, what a paced run takes, that nothing else
waited for wall time either.

## A consumer names a condition with esp32tl.condition, not with a description on the wait

Status: decided (#181)

A condition that a test or its helper writes is a function, and `board.wait_until` names a
function by its `__qualname__`: it times out as `the condition test_drive.<locals>.<lambda> did
not happen within 180 s`, never in the consumer's words, and `assert reports.when_received()`
passes on the truth of the function without asking it. The network's services already had the
object that avoids both, `Condition(find, description, service)`; `esp32tl.condition(find,
description, *, service=None)` now makes it public, and `esp32tl.Condition` is its type. With no
`service`, the `TypeError` of `bool()` names the description:
`call the condition 'a trip/end from the device' with (), or pass it to board.wait_until`.

The class moved from `esp32tl/net/condition.py` to `esp32tl/interfaces/condition.py`: nothing in it
is about a network, and at `esp32tl/condition.py` the function and a submodule of the same name
would shadow each other: `import esp32tl.condition as c` would give the function. The MQTT, HTTP and
DHCP conditions keep their messages, `service` still naming the protocol. TLS's
`_HandshakeCondition` and the GPIO `OutputCondition` are folded into it too, with their descriptions
and messages unchanged; only their equality changes, since each property call makes a new `find`, so
`out.is_on == out.is_on` is now false, which nothing relied on. With a public constructor, a class
that repeats it is a second way of doing the same thing, which the earlier choice to keep
`_HandshakeCondition` (#116) did not have to weigh. The position tracker's `_ReportCondition` became
`condition(find, description)`, the way a consumer writes one. `console.line` keeps `LineCondition`,
which moves its cursor after each match.

**Rejected:** a `description=` argument on `board.wait_until`. It names the wait, but the name
stays at the call: a helper that returns a condition, or an adapter that turns a `find_*` query
into one, cannot carry it to the wait, and a condition made that way still passes `assert`
unasked. Also rejected: keeping the class in `esp32tl.net` and re-exporting it, which leaves a
general object in a module about networks; and naming the condition through `__qualname__`, the
consumer's workaround, which is an attribute of the function, not a description of the event.

## A PlatformIO build is read by its file names and image headers

Status: decided (#192)

*For the serial backend, extended in #194 by [The serial backend flashes a PlatformIO build
through an IdfApp of esp32tl's](#the-serial-backend-flashes-a-platformio-build-through-an-idfapp-of-esp32tls):
serial no longer refuses a PlatformIO build.*

`load_app` reads a build into `esp32tl.build.Build`, esp32tl's own description of it: the flash
files at their offsets, the partition table's offset, the application image, the ELF and the chip.
An ESP-IDF build is read from `flasher_args.json` through `IdfApp`, as before. A directory without
`flasher_args.json` that holds `firmware.bin` or `firmware.factory.bin` is a PlatformIO build,
`.pio/build/<env>/`: `firmware.factory.bin` at `0x0` when it is there, else `bootloader.bin` at
`0x1000`, `partitions.bin` at `0x8000` and `firmware.bin` where pioarduino's espressif32 platform
flashes it: at the table's `ota_0`, else at `0x10000`, which must be the start of an application
partition large enough for it. With the otadata left erased, the ESP-IDF bootloader tries a
`factory` partition first, finds no image there, and goes on to `ota_0`, so the application runs
from the partition it runs from on a board pioarduino flashed, where `boot_app0.bin`'s otadata
selects `ota_0`; only the otadata differs there, erased where the board has `boot_app0.bin`'s, so
that `esp_ota_get_boot_partition()` names `factory`. A test that relies on it runs
`firmware.factory.bin`, which holds that otadata. A factory image must hold `firmware.bin` at that same offset, where pioarduino's
`merge-bin` puts it, since a recording's `app_sha256` is the hash of `firmware.bin`, and a factory
image of another build would run an application the recording does not name. The chip comes from
the chip id at bytes 12 and 13 of
the images' extended headers, since a PlatformIO build directory has no `config/sdkconfig.json`;
a build for another chip is refused with its name. `board.nvs` and `board.flash` read the partition
table at the offset the build names, from whichever flash file holds it, no longer by the file name
`partition-table.bin`, which a PlatformIO build does not have and a factory image hides inside
itself.

The description is a module of its own, `esp32tl/build.py`, with its own feature documents, rather
than more of `esp32tl/backends/esp32sim.py`: both backends, the taps and the `esp32tl` command read
it, and none of them needs to know which kind of build it came from. The serial backend refused a
PlatformIO build until #194, since pytest-embedded's `IdfSerial` flashes the files of
`flasher_args.json`.

**Rejected:** `pio run -t envdump` or `pio run -t idedata` for the offsets and the chip, because
they need PlatformIO in the test environment, are slow, and may reach the network to install a
platform. Also rejected: looking up `boot_app0.bin` in `~/.platformio/packages` to write the
otadata, because what is found there depends on the machine and the platform's version, so two
hosts would run different flashes from one build; and writing a `flasher_args.json` into the
PlatformIO build, a converter, which
[explicit build paths](#explicit-build-paths-are-relative-to-the-application-and-never-fall-back)
rule out: the test runs the artifact it selected, unmodified. Also rejected: `firmware.bin` in
`factory`, else in `ota_0`, the partition the bootloader starts from with the otadata erased,
because a real build runs from `ota_0`: Arduino-ESP32's `app3M_fat9M_fact512k_16MB.csv` and the
m5stack factory tables keep a launcher in a `factory` partition, 512 KiB in the first, and the
application in `ota_0`, so that rule would run the application from another partition than a
board does, with another running partition and OTA target, and refuse the factory image
pioarduino built; and at the first application partition of the table, which no platform does.
platformio's own espressif32 flashes `firmware.bin` at the board's `upload.offset_address`,
`0x10000` by default; that setting is not in the build directory, so esp32tl follows pioarduino.

## The serial backend flashes a PlatformIO build through an IdfApp of esp32tl's

Status: decided (#194)

pytest-embedded's `IdfSerial.flash()` writes the `flash_files` of the `IdfApp` it is given, with
the `extra_esptool_args` and `write_flash_args` of its `flash_args`; only `IdfApp.__init__` reads
`flasher_args.json`. So a PlatformIO build reaches the board through `PlatformioApp`
(`esp32tl/backends/serial.py`), an `IdfApp` whose files are those of the `esp32tl.build.Build`
esp32sim runs: `firmware.factory.bin` at `0x0`, or `bootloader.bin`, `partitions.bin` and
`firmware.bin` at their offsets. The plugin puts it in the place of `IdfApp` in pytest-embedded's
`_fixture_classes_and_options` for a serial test whose build is a PlatformIO one, after
`check_artifact` has validated it and before the `app` fixture is made; an ESP-IDF build keeps
`IdfApp`. Flashing, `--skip-autoflash`, the session cache, `--erase-all`, `--erase-nvs` and
`ESPBAUD` stay pytest-embedded's, the same for both kinds of build. The checks before the port
opens stay those of `check_artifact`: the chip, the files, and the hash of `firmware.bin` against
an `app_recording`.

esptool keeps the flash mode, frequency and size of the images (`keep`), so the board gets the
bytes the emulator runs and a recording's hash names. Without `firmware.factory.bin`, two parts of
the flash that esp32sim has erased are written erased, from files in a temporary directory, where
no file of the build covers them: the otadata partition, and the first sector of a `factory`
partition. Left alone, the otadata an earlier firmware wrote could boot another slot, and with the
otadata erased the bootloader tries `factory` before `ota_0`, so a launcher or an older
application left there would run instead of the one the test selected, while the hash check
passes on the local file. The first sector holds the image header, which is all the bootloader
needs to find the partition not bootable, as on esp32sim; a launcher on the board is lost. An
ESP-IDF build erases the otadata the same way, with `ota_data_initial.bin`, and so does a
`framework = espidf` build's `pio run -t upload`. Whether `factory` is left alone is decided by
the build's otadata bytes, not by a file covering them: an Arduino build's `firmware.factory.bin`
holds `boot_app0.bin`'s, which selects `ota_0`, so the bootloader does not try `factory`; a
`framework = espidf` build's holds an erased `ota_data_initial.bin`, which selects nothing, so
`factory` gets its erased first sector. A selecting otadata is told from an erased one by its
bytes not being all `0xFF`, since those are the two otadata pioarduino writes; checking the CRC of
an `ota_select` entry would tell more than is needed.

A PlatformIO build whose `config/sdkconfig.json` sets `SECURE_FLASH_ENC_ENABLED` or `SECURE_BOOT`
is refused on both backends, as an ESP-IDF build that asks for encryption is: its bootloader would
burn the board's eFuses at the first boot, which cannot be undone, and with `SECURE_BOOT`
`IdfSerial` passes esptool `--force`. A build without that file is not checked: the build
directory has nothing else that says so. An Arduino build has none, also with `custom_sdkconfig`,
whose configuration pioarduino writes to the project directory; such a build's `bootloader.bin`
was the framework's precompiled one, so its encryption setting does not reach the bootloader.

**Rejected:** flashing with esptool from esp32tl and running pytest-embedded with skip-autoflash,
because esp32tl would then own a second flashing path, with its own port handling, baud rate and
cache, next to the one ESP-IDF builds take. Also rejected: a subclass of `IdfSerial` that erases the
otadata with `erase-region`, because a test's `IdfSerial` is pytest-embedded's choice, and the
suite's stand-in replaces it; an erased file among the flash files goes through
`IdfSerial.flash()` unchanged. Also rejected: writing `boot_app0.bin`'s otadata, as pioarduino's
upload does, for the reason the decision above gives: it is in the framework's package, not in the
build; and an otadata of esp32tl's selecting `ota_0`, which keeps a launcher in `factory` but
gives the board another otadata, and another boot partition, than esp32sim's flash has. Also
rejected: erasing the whole `factory` partition, slower for no other effect on the bootloader;
and refusing such a build on serial, since it can be flashed. Also rejected: refusing the build
as postponed, because the build is flashable with what pytest-embedded has.

## Console input uses GPIO 3 at a fixed 115200 baud

Status: decided (#195)

The console is a receive-only link route (GPIO 3), with guest output arriving through `console`
messages. Test writes are queued at the board's current instant and drained in the next poll;
UART0 receives bytes paced by the runner. Watching GPIO 1 would duplicate console output in
`uart_tx`, so only GPIO 3 is watched. Console taps record input at write time and output at
receipt time, with the existing version 1 directions.

The simulated console's line rate is fixed at 115200; no `[console] baud` profile key is added.
A guest using a different rate gets the existing `frm_err` failure. Serial uses the selected
port's baud, as it already does for output. A profile rate override is postponed until a
consumer needs a non-default simulated console. Automatically following `uart_cfg` is rejected:
it would hide the framing errors of a physical host port at the wrong rate.

## A test joins the device's access point through esp32sim's virtual station, with the harness as the host behind it

Status: decided (#215)

`wifi.station(ssid, password=..., mac=...)` configures a virtual station before power-on and
returns `Internet` with a third link, `Station`, beside PPP and access-point Ethernet. The radio
interface describes both directions now: its original station-only wording in #31 is extended,
not a restriction on the chip's mode. The pin audit of #212 deliberately left this API for a
separate change; this is that change. The runner attaches `StaConfig` through
`SocBus::attach_wifi_station`, with Ethernet relay on. The harness uses the virtual station's
MAC: there are no four-address frames, and the emulator takes no lease of its own in relay mode.

The host gets its address by DHCP and uses option 54, the selected server's identifier, as the
default device address. A router in option 3 can be another host; it is used for off-subnet
routing only. `address=` still overrides the TCP destination. `network.lease` exposes the current
lease and `network.when_leased()` is a current-state condition, following #108/#116/#182 rather
than giving a history query an event name. DNS, NTP, the broker, HTTP server and registered or
bridged services remain usable, but only on the host's leased address; DNS answers with that
address. ARP answers only for that address. IPv4 destined elsewhere is dropped before services.

DHCP uses the broadcast flag, which ESP-IDF v5.5.4's
`components/lwip/apps/dhcpserver/dhcpserver.c:dhcps_response_ip_set` explicitly handles, even with
static ARP entries enabled. Retries use board time, starting at 4 s and doubling up to 64 s,
with deterministic transaction IDs and no random jitter. Before protocol v2 (superseded by the data-link readiness decision below), identical requests waiting in the
runner were coalesced in station mode: an unjoined station's first DISCOVER stays queued, without
an ever-growing backlog. Once a frame has entered esp32sim, later retries are independent frames.
There is no gratuitous ARP or RFC 5227 conflict probe: this isolated subnet has one harness host,
and its address comes from the device. Ordinary ARP resolves destinations before queued IP leaves.

**Rejected:** the station taking a lease inside esp32sim and host forwarding into the guest's AP:
#362 has no such forwarding; it belongs to mikroverk/esp32sim#361. A host IP stack in the runner
would duplicate the harness's deterministic TCP and HTTP. A separate client-only network class
would duplicate connections, path configuration, transcripts and reporting; the third `Internet`
link keeps those. Promiscuous service addresses, as behind a virtual AP, would make this host
answer for other machines on the device's subnet.

**Not yet:** T1 renewal, pending a DHCP renewal/rebinding state machine and tests for long runs.
At lease expiry the host drops its connections and ARP cache and starts DHCP again; it never
continues sending from an expired address. Device reset invalidates the lease immediately and waits for Wi-Fi readiness before restarting DHCP. No explicit
station leave/rejoin command, pending a radio control protocol and upstream support. No keep-alive
frames, pending esp32sim support: an ESP-IDF AP may disassociate an idle host after about 300 s
and the virtual station rejoins. No virtual AP and virtual station together, pending esp32sim's
AP+STA model ([the case and when to come back](#a-virtual-access-point-and-a-virtual-station-together-wait-for-esp32sims-apsta)); no other chips, pending their station attachment. WPA3-SAE, required PMF and hidden
SSIDs wait for upstream authentication and probe support.

## Runtime preparation is explicit and version-matched

Status: decided

The wheel use case now requires no repository or Rust. `esp32tl runtime prepare` installs a
runner and ROM into a version/platform user cache. Exact package and esp32sim identity prevents
silent use of stale executables. Explicit paths retain priority, followed by checkout builds,
then cache. No import or pytest path downloads anything. Raw runner binaries avoid an archive
parser; the ROM stays upstream with the existing pins, not redistributed under an assumed licence.

Rejected: bundled platform wheels (duplicate Python packages and ROM distribution), automatic
first-test downloads (hidden network requirement), PATH lookup (ambiguous version), an updater
(unneeded scope), and support for untested architectures. Linux glibc x86_64 and macOS arm64 are
the release candidates; every published release is gated by native wheel/firmware tests on both.
Checksums come from the same HTTPS release authority, not an independent signing system.


## Backend runtime validation follows selection per file

Status: decided

The backend validates only the selected cache resource and its manifest. `ESP32TL_RUNNER`
replaces all use of the cached runner, including its identity check; `ESP32TL_ROM` replaces all
use of the cached ROM. Full-installation validation belongs to `runtime.prepare()`, which still
checks both files before reuse and before exposing an installation. Calling that full validation
from either backend lookup was rejected: an unused, replaced file could incorrectly block a
valid explicit path combined with the other file from cache.


## An install from a commit or a directory gets no release runner

Status: decided (17 November 2026)

`esp32tl runtime prepare` and the backend's cached runner refuse an esp32tl that pip installed
from a git commit or a source directory, as its `direct_url.json` (PEP 610) records, with
`unreleased esp32tl: …` and how to build the commit's runner
([runtime installation](../features/runtime-user.md#test-an-unreleased-commit)). Such an install
carries the last release's version and esp32sim revision, so the identity check passed with the
release's runner, which then refused `--chip esp32s3` at the first turn after the ESP32-S3,
ESP32-C3 and ESP32-C6 landed on `main`. A git install of the release's tag `v<version>`, and an
install from PyPI or from a wheel or sdist file, count as the release. A cached ROM still serves
while the cache's manifest matches the install, as for a release; when the cache cannot give it,
the message names the ROM's variable instead of `prepare`, which installs nothing here.

**Rejected:** a development version on `main` (`0.2.0.dev0`) raised right after each release, with
`prepare` refusing it, because every release would take two version changes, and the release
workflow recognises a release by a version-only change to `MAJOR.MINOR.PATCH`
([prepare versions in a PR](ci.md#prepare-versions-in-a-pr-and-build-releases-after-merge)).
Documentation alone, because the failure came at the first turn with nothing pointing to the
install. Checking a cached ROM by its checksum alone, without the manifest, for an unreleased
install, because it would loosen the version match of
[runtime preparation](#runtime-preparation-is-explicit-and-version-matched) for one case.

**Postponed:** comparing what the runner can do, its `--chip` values or its link protocol, in
`--runtime-version` or the release manifest. It would also catch a runner from another commit set
through `ESP32TL_RUNNER`, a wheel built from an unreleased commit, and an archive of a commit
such as GitHub's `/archive/<commit>.tar.gz`, which this check counts as releases; the reported
case is caught without it.


## Final radio diagnostics come from the completed runner log

Status: decided (#223)

The backend reads `[emu] wifi` lines from the raw runner log after the runner exits and the
waiter joins, and includes them verbatim in its teardown report. The runner emits these lines
at shutdown; pytest-embedded's asynchronous forwarder can be stopped before it reads them.
This caused `test_station_diagnostics_are_in_captured_output` to fail on macOS in CI run
43858823712 despite the expected wrong-password timeout. The full source log is streamed,
rather than a bounded tail, so subsequent output cannot displace the diagnostic.

**Rejected:** sleeps or longer DHCP timeouts, which do not synchronize log delivery. Replacing
pytest-embedded's forwarder and queue listener is unnecessary for this report: the raw log is
already complete when the backend closes. Live capture and `dut.log` retain their existing
asynchronous behavior; the teardown report no longer depends on it for radio diagnostics.

## DHCP starts at virtual station data-link readiness

Status: decided

The runner reports the pinned emulator's Joined state and completed-join counter, with sequenced protocol v2 events. DHCP remains in Python. WPA2 Joined follows validated message 3 and key installation; esp32sim sends its queued message 4 before relay data. Polling at slice boundaries is sufficient with this pin: leaving imposes a one-second rejoin delay. The counter additionally distinguishes another join. Link loss clears queues and transport state and each acquisition uses a fresh transaction ID, including across device reset.

Rejected: console parsing, boot delays, first-DHCP-response inference and shorter retries. The old client started at power-on; however its first DISCOVER remained queued in the runner until Joined. Thus its retry schedule alone does not establish a 28 s lease delay. No new station control API or simultaneous AP+STA support is introduced.

## Filter the XML attribute warning as an overridable default

Status: decided (#226)

`pytest_configure` prepends an ignore filter to `config.getini("filterwarnings")` for only
`^record_xml_attribute is an experimental feature$` and `pytest.PytestExperimentalApiWarning`.
pytest applies ini filters in order, giving later matches priority, then applies command-line
`-W` filters; per-test warning markers take priority over both. Prepending keeps consumer ini
settings (including `error`), command-line options and markers authoritative.

pytest emits this warning while setting up its `record_xml_attribute` fixture, before returning
the callable. Both pytest-embedded's `app_path` and a consumer's direct fixture request use that
same setup; message, category and source cannot distinguish them, and setup happens once per
test. The default therefore suppresses both. Other messages and warning categories remain
visible. Revisit when pytest-embedded no longer requests the fixture or pytest stabilises it.

**Rejected:** appending with `addinivalue_line`, which would override consumer ini filters;
filtering the whole experimental-warning category, which would hide unrelated API warnings;
wrapping or replacing pytest's fixture to infer callers, which adds fragile coupling for a
warning with a consumer-controlled override.

## Bluetooth histories expose Duration timestamps

Status: decided (#224)

Channel events and refusals use `ChannelEvent(at, direction, data)` and `Refusal(at, why)`
named tuples. `UnknownCommand(t, op_code, name)` is also a named tuple; `Pairing` stays a
frozen dataclass. Every timestamp is a `Duration` on the owning air's clock, the board's for
`board.bluetooth.air`. This deliberately breaks the former public integer API so histories
can be compared directly with GPIO and MQTT histories. Named tuples retain positional access
and unpacking while naming the fields. Internal scheduling remains in nanoseconds; report
formatting and JSON `t_ns` explicitly extract `.ns` and keep their existing formats.

**Rejected:** keeping integer times and adding `*_at` accessors beside them would preserve two
representations of the same instant and leave the unit-mixing hazard in the primary API.
Plain tuples would preserve unpacking but lose discoverable field names unnecessarily.

## Parallel simulation uses pytest-xdist installed separately

Status: decided (#227)

Support independent esp32sim board tests with pytest-xdist and cover them in the normal native
CI shards. Keep xdist in development dependencies; consumers install it explicitly. An extra
would only alias one optional test runner dependency, so it adds no useful configuration.
Reject serial distributed execution during configuration rather than trying to serialize a
physical board across workers. Cross-session hardware locking is outside this guard.
Do not promise a fixed memory budget or linear speedup without workload measurements.
Runtime preparation stays explicit: workers never perform automatic first-use downloads.

## SPP conditions describe channel state and the next device write

Status: decided (#225)

`SppPeer.is_open` and `is_closed` describe the current channel state; `channel_open` stays a
boolean. `next_write(data=None)` follows the `next_*` convention and MQTT's append-order,
non-consuming boundary. It returns the existing `ChannelEvent`, whose time is a `Duration`
since #224. These APIs belong only to `SppPeer`: `BluetoothDevice` has no channel to observe.

The bytes filter means literal containment within one received chunk. Unlike MQTT's exact
payload match, SPP is a byte stream, often with command terminators in the same chunk.
Unlike the console's regex line matcher, this API does not frame lines, join chunks or accept
regexes. The name `next_write` describes the device's direction, and the description says
`containing` explicitly. Consumers needing framing still use a source model.

**Rejected:** leaving consumers to compose `condition()` over `events`, which repeats boundary
bookkeeping and loses consistent peer-specific descriptions. Also rejected: timestamp
boundaries, which miss distinct writes in one run slice at the same time.

**Postponed:** `next_open()` and `next_close()`. State conditions cannot observe a close and
reopen between polls. Reliable transition conditions need a separate retained lifecycle history;
this change observes the existing byte history only. Tests can wait for closure before initiating
reconnection, or wait for a new device write after reset. Add transition history when a consumer
needs to prove both transitions occurred without an intermediate wait.

## The device's SPP writes are read as lines at a separator the test chooses

Status: decided (#246)

`SppPeer.written_lines(separator=b"\r")` and `next_written_line(data=None, separator=b"\r")` join
the device's writes and cut them into `WrittenLine(at, data, opening, complete)`s, so that a test
that only checks what a device sent in a line-based protocol needs no model and no framing of its
own. They follow the console's `line()` and `lines()` where the two agree: a line's time is the
time its ending came at, a line not ended on an open channel is not one, and the condition finds
the first line that ends after it was made. They differ where SPP differs from a console:

- **Separator.** The console ends a line at `\n` and drops a `\r` before it, because ESP-IDF's
  log does. SPP carries any protocol; the default is `b"\r"`, which ends an AT command
  (ITU-T V.250, S3's default), the case that asked for this, and the test gives any other non-empty
  bytes, such as `b"\r\n"`. No `\r` is dropped: the line is the bytes before the separator.
- **Bytes, not text.** The console decodes and matches regular expressions; the peer's lines stay
  bytes, and `data` is literal containment, as in `next_write`.
- **Names.** `written_lines` and `next_written_line`, not `lines` and `line`: the channel carries
  both directions, `written` names the device's, as `next_write` does, and `next_*` is the
  convention of the boundary conditions.
- **Openings.** A partial line at a channel's close is a line of its own, `complete=False`, and
  `opening` says which opening of the channel each line was written on, so a partial line before a
  reconnect is never joined with the first line after it, and is not lost either.
- **No filter in `written_lines`.** `console.lines(pattern, *, since=)` filters; `written_lines`
  takes neither a pattern nor `since`, as a list comprehension over `line.at` or `line.data` says
  the same. To be added when tests repeat such a filter.

Lines are cut when read, from `events` and the openings' boundaries, one cache per separator;
`ChannelEvent` keeps its three fields, so `events` compares with plain tuples as before.

**Rejected:** cutting lines in the peer's receive path at one separator given to `SppPeer`, which
fixes the protocol at construction and would need a second list for a second separator; dropping
an incomplete line at a close, which hides what the device wrote; and adding the opening to
`ChannelEvent`, which breaks `events` compared with three-field tuples.

## A board profile's chip decides its pads, UARTs and console pins

Status: decided (#236)

`esp32tl/boards` keeps one `Chip` per chip a profile may name, `esp32`, `esp32s3`, `esp32c3` and
`esp32c6`: the GPIO pads, the input-only ones, the number of UARTs and UART0's pins, which the
`console` takes on every board. A profile's routes, lines and slot are checked against its chip's
`Chip`, and the esp32sim backend's console wire is on the chip's U0RXD. esp32tl ships one devkit
profile per chip, `esp32-s3-devkitc` (N8R8: 8 MB flash, 8 MB PSRAM), `esp32-c3-devkitm` and
`esp32-c6-devkitc`, with the same `[uart.gnss]` as `esp32-devkitc` and a `[lines.echo]` set for
`gpio_echo`.

**Rejected:** listing the pads in each profile, because every profile of a chip would repeat
them and a typo would make a board with pads the chip does not have; leaving the console's pins
at GPIO 1 and 3, which on the S3 are ordinary pads a route may take while the console is on 43
and 44.

## esp32sim's gaps on a chip skip the test that asks for them

Status: decided (#236)

The esp32sim backend's `CHIP_GAPS` names, per chip, what esp32sim does not model there, by the
`Request` a test makes: `sd_card` (no GP-SPI RX DMA on the S3, C3 and C6, which ESP-IDF's SD SPI
driver needs) and `wifi.station` (no virtual station). `Wifi.station` asks with
`Request("wifi", source="model", role="station")` so that it is told apart from the access
point. A match skips the test with the reason and the entry of
[#229](https://github.com/mateuszsikora/esp32-testing-library/issues/229) that tracks it,
as a backend skips what it does not declare.

**Rejected:** a chip dimension in `capabilities.toml`, because the declaration says what a
backend can put at the far end of an interface, the same for every chip, and the gaps are
esp32sim's, with an end date when the pin moves; letting the run fail, because the runner would
refuse `--wifi-sta` with exit 2 and an SD card test would time out in the firmware's driver, both
reading as a broken test rather than as a model esp32sim lacks.

**Not yet:** deep sleep on the S3, C3 and C6 cannot be skipped: nothing asks for it before the
firmware sleeps. `board.wait_for_wake` raises when its time is up.

## Boot reasons keep the classic ESP32's names on every chip

Status: decided (#236)

`Boot.reason` names a reset cause from `RESET_REASONS`, the classic ESP32's table, whatever the
board's chip. The causes the emulated chips report, 1, 3, 5 and 12, mean the same on the S3, the C3
and the C6 (power-on, software system reset, wake from deep sleep, software CPU reset); only the
ROM's names for them differ (`RTC_SW_CPU_RESET` for 12 on the S3).

**Rejected:** a table per chip, because `Boot` would need the profile and the four chips' common
causes would read differently in reports for the same event.

## A boot's ESP-IDF reason comes from each chip's own table

Status: decided (#261)

`Boot.idf_reason` names a boot as ESP-IDF's `esp_reset_reason()` does, from a table per chip of
`get_reset_reason()` and the hint the runner reads with each `reset`. The backend computes it and
puts it in `Boot`, a field that takes no part in comparing two boots, so `Boot` needs neither the
profile nor the hint. `Boot.reason` keeps the ROM's names of the classic ESP32
([above](#boot-reasons-keep-the-classic-esp32s-names-on-every-chip)): the IDF names are the
same strings on every chip, and where a chip's table gives another name for a cause, that is what
its firmware reports.

**Rejected:** naming the hint's reason from `Boot.cause` alone, because the ROM reports 12
(`SW_CPU_RESET`) for `esp_restart()` and for a panic alike; a `hint` field on `Boot`, because a
test would then have to name a register's raw value to build a boot to compare, and the field
would mean nothing to the serial backend; and a table per ESP-IDF version, because v5.3.6, v5.5.4
and v6.1, every version the validation images are built with, have the same `get_reset_reason()`
on each chip.

**Not yet:** the serial backend has no `idf_reason`: it does not see the chip's resets, and skips
`board.boots`. The P4's eFuse CRC reset is `ESP_RST_DEEPSLEEP` on chip revision v0.0, which the
table does not tell apart; esp32sim models none of them.

## The serial backend runs the classic ESP32 only

Status: postponed (#236)

A board profile for another chip is refused on `--esp32tl-backend serial` before the port is
opened. A real S3, C3 or C6 board would need the chip's bootloader offset and `esptool --chip` in
the flashing path, and on many devkits a console on USB-Serial/JTAG, which the serial backend's
`Wire` on UART0 does not read.

The ESP32-S3 with its console on USB-Serial/JTAG has since been measured and runs
([the serial backend runs an ESP32-S3 through its USB-Serial/JTAG port](#the-serial-backend-runs-an-esp32-s3-through-its-usb-serialjtag-port));
a C3 or C6 with that console skips. The refusal stays for a console on UART0 on the S3, C3 and C6:
`serial: the board profile {name} carries an {title} with its console on UART0; the serial backend
runs the classic ESP32, and the ESP32-S3 with its console on USB-Serial/JTAG`.

**Not yet:** pending a real board of each chip, and of each console, to validate on.

## Project defaults are esp32tl ini options that the markers override

Status: decided (#237)

A project sets the app, the build and the board profile of its `board` tests once, with four
string ini options: `esp32tl_app_path`, `esp32tl_build_dir`, and `esp32tl_board_profile` or
`esp32tl_board_profile_path`. They go where pytest reads its configuration, `pyproject.toml`'s
`[tool.pytest.ini_options]` among them, and `-o` overrides them as any ini option. pytest ignores
a `pytestmark` in `conftest.py`, silently, so before this a project repeated the markers in every
file, or re-exported a `pytestmark` from a helper module, which works but is not something a
newcomer finds. A value in quotes is a usage error: an ini file keeps the quotes TOML takes off,
and a quoted path would only fail later, at setup, as a missing app.

Paths resolve against the configuration file's directory (`config.inipath.parent`), the base
pytest uses for its own `paths` options, or the invocation directory when there is no
configuration file; so the options select the same files from any working directory.
`esp32tl_build_dir` is relative to the app, as the marker's `build_dir` is
([explicit build paths](#explicit-build-paths-are-relative-to-the-application-and-never-fall-back)).
The options sit below every marker: the closest `app_path` marker supplies the app and its build
together, as it already did against a more distant marker, and the closest `board_profile` marker
the profile. A test that keeps its markers runs what it ran before. The name and the file are two
options, as the marker's positional name and `path=` are, and setting both is a usage error.

This extends [the app from the app_path marker](#the-app-comes-from-the-app_path-marker) and
[local board profiles](#local-board-profiles-use-an-explicit-path-relative-to-the-test-file):
the markers' paths stay relative to the test file, and their checks, snapshots and late-marker
refusal are unchanged.

**Rejected:** pytest-embedded's `--app-path` and `--build-dir` through `addopts`. They are
command-line options whose app path is used relative to the working directory, they carry
pytest-embedded's multi-DUT `|` syntax, nothing of theirs selects a board profile, and
[allowing them beside the marker](#pytest-embeddeds-app-and-service-options-are-refused-for-board-tests)
would make two competing selectors; they stay refused. Also rejected: pairing `esp32tl_build_dir`
with an app a marker names, because the project's build directory describes the project's app,
and the closest marker already supplies both values; one profile option that guesses whether its
value is a name or a path, rejected for the marker for the same reason; resolving against pytest's
`rootdir`, which `--rootdir` can move away from the file the values are written in, or against the
working directory, which changes with where pytest is started; pytest's `paths` type for a single
value, a list in native TOML and split at spaces in an ini file; a plugin hook or fixture of the
project's that sets markers, which is what the options spare it.

## A model that frames by silence tells on_frame when the frame's data came

Status: decided (#237)

`IdleFramedModel` sets `frame_first_at` and `frame_last_at` before each `on_frame(at, frame)`: the
`at` of the frame's first piece and that of its last piece, as `receive` got them: on a UART the
instant the firmware wrote the piece's first byte, behind an SPP channel the instant it arrived.
`at` stays what it was, the instant the silence of `gap` ended the frame. A test of the firmware's
timing on a UART, as when its first request starts, wants the first instant; `at` adds `gap` and the
time the frame took to write. Behind an SPP channel the first instant is when the frame's first
piece arrived, which does not say when the firmware wrote it. Intervals between frames were already
exact. The attributes keep the frame's values until the next frame ends and are absent before the
first one. This extends [the bases a model starts
from](#a-model-that-only-answers-or-frames-by-silence-starts-from-a-base-that-fills-in-the-rest):
nothing else of `IdleFramedModel` changes, and a subclass written for `on_frame(at, frame)` needs no
change.

The link gives each piece one instant, that of its first byte, and no instant of a later byte; so
`frame_last_at` is the last piece's instant, not the frame's last byte's, and nothing claims more.
A piece exactly `gap` after the last, which the link delivers before the turn at that instant,
continues the frame, as before.

**Rejected:** a third argument of `on_frame`, required or keyword, because every subclass
overrides `on_frame` with two arguments, and calling it with a third breaks them; inspecting the
subclass's signature to pass a keyword only to one that takes it, because a decorated or `*args`
method hides it and the call would depend on how the method is written; a `Frame` object passed to
a second hook, because `on_frame` is the abstract method that makes a misspelt one impossible to
create, and two hooks would make one of them optional; times per byte, which the transport does
not give.

## The emulated device's Wi-Fi MACs are constants of the wifi interface

Status: decided (#237)

`esp32tl.interfaces.wifi` exports `ESP32SIM_STATION_MAC`, `24:6f:28:00:11:22`, and
`ESP32SIM_SOFTAP_MAC`, `24:6f:28:00:11:23`, which `wifi.station`'s check of its own `mac` uses. A
firmware that names its SoftAP after its MAC needs the value before power-on, to give
`wifi.station` the SSID, and copying the literal into a test hides where it came from. The names
say whose they are: the emulated classic ESP32's on esp32sim, the runner's base MAC and the
SoftAP's derived from it as ESP-IDF does, not a physical board's and not the harness's virtual
station's (`esp32tl.net.station.DEFAULT_MAC`) or virtual access point's BSSID. The runner's `MAC`
is in Rust, in `runner/src/chip/esp32.rs`; a test compares the two, as the runtime revision pins
are compared.

**Rejected:** `esp32tl.backends.esp32sim`, which imports the `wifi` interface, whose check needs
the constants: the interface would have to import the backend back. A property of the `wifi`
fixture, because the serial backend's fixture is a `Wifi` too, on a board whose MACs come from its
eFuses and which esp32tl does not read. Reading the MACs from the runner at run time, because a
test needs them before power-on, before the runner starts. Constants for the ESP32-S3, C3 and
C6, which have MACs of their own but no virtual station attachment yet (#229), so no test of theirs
can join a SoftAP named after one.

## A missing release manifest names possible causes, not a diagnosis or a fallback

Status: decided (#237)

When `esp32tl runtime prepare` gets HTTP 404 for the release's runtime manifest, the first file it
downloads, `download failed` keeps the URL and the HTTP error and adds the possible causes, the
release not published yet or published without a runtime for this platform, and the alternatives,
`ESP32TL_RUNNER` with `ESP32TL_ROM`, or a source checkout's `make setup`. The status says only
that the file is not there, so the message says "possible causes". Other statuses, network errors
and a 404 for a later file keep their message: the manifest found means the release exists, and a
hint there would mislead.

**Rejected:** querying the GitHub API to tell an unpublished release from a missing asset, a
second request to another service for a message; trying the previous version or another release,
which would install a runtime that the version match exists to refuse
([runtime preparation](#runtime-preparation-is-explicit-and-version-matched)).

## A virtual access point and a virtual station together wait for esp32sim's AP+STA

Status: postponed (#237)

`wifi.access_point` and `wifi.station` stay exclusive in one test, with
`wifi.access_point and wifi.station are exclusive: one virtual Wi-Fi role per test`. The pinned
esp32sim, `357b0e5`, refuses the combination itself: `StationLink::attach_station` in
`esp-soc/src/wifi.rs` returns `a virtual station needs no access point attached: AP+STA is not
modelled yet`, `add_ap` refuses an access point beside a station, and the link has one Ethernet
relay, whose frames carry no sign of which of the device's interfaces they belong to.
[The decision on joining the device's access point](#a-test-joins-the-devices-access-point-through-esp32sims-virtual-station-with-the-harness-as-the-host-behind-it)
already postponed it; this records a case that needs it and when to come back.

The case: a firmware in `WIFI_AP_STA` that, without its configured network, starts a fallback
SoftAP, and retries the configured network only while no client is on that SoftAP. A test of that
rule needs, in one run: the device's fallback SoftAP up, a virtual client joined to it, the
configured network appearing later as a virtual access point (off the air with `access_point_off()`,
then on with `access_point_on()`), and the device's station not trying it, or trying it, by whether
the client is still there. Neither role alone can show the rule.

What is missing upstream: one esp32sim radio that carries a virtual access point for the guest's
station interface and a virtual station on the guest's SoftAP at once, on one channel as the chip
has, with each relayed Ethernet frame marked by the interface it belongs to, and the attachment
calls that allow both. The runner then needs `--wifi` with `--wifi-sta` and per-interface frames
on the link, and the harness two networks in one `Wifi`.

Come back when an esp32sim revision accepts both attachments on the classic ESP32 and relays the
two interfaces' frames apart; then bump the pin, lift the exclusivity check, and test the case
above. No upstream issue has been opened for it.

## The runner's flash is the profile's flash_mb, else the build's headers, refused at setup when too small

Status: decided (#240)

The esp32sim backend and `esp32tl run` give the runner `--flash-mb` from the board profile's
`flash_mb`, and, for a profile without it, the largest size that the build's bootloader and
application image headers give; a build with neither runs on the runner's default, 4 MB. Until
now the runner always had 4 MB, so an Arduino-ESP32 3.x firmware built with PlatformIO for an
8 MB ESP32-S3 module, unchanged, rebooted until the test's timeout: its application stopped in
`spi_flash` (`Detected size(4096k) smaller than the size in the binary image header(8192k)`,
then `assert failed: __esp_system_init_fn_init_flash`). At setup, before a runner starts, the
backend refuses a `flash_mb` outside 1, 2, 4, 8 and 16, the sizes `--flash-mb` takes, and a
header that gives more flash than the runner would get, naming both sizes and the `flash_mb` to
set. A header that gives less runs, as on a board, where ESP-IDF warns and uses the header's
size. Both headers count because ESP-IDF's bootloader sets the size from its own header, and the
application from its own with `CONFIG_SPI_FLASH_SIZE_OVERRIDE`. The profile schema still takes
any size from 0, which the serial backend never reads; the refusal is the esp32sim backend's.
The serial backend flashes with `flash_size: keep` and is unchanged.

The end-to-end test boots `hello_world_8mb`, ESP-IDF's `hello_world` built by `firmware/build.sh`
with `CONFIG_ESPTOOLPY_FLASHSIZE_8MB=y`, on the four chips: on `flash_mb` 8 and 16 it boots and
`board.flash()` is that size, on 4 the board fixture refuses it. Measured locally on the runner
at esp32sim 357b0e5 with an ESP-IDF v5.5.5 build of it (`IDF_LOCAL=1`, no Docker), 1 emulated
second each: with `--flash-mb 4` the classic ESP32 reset 33 times, the S3 97, the C3 60 and the
C6 57, each time on that assert; with 8 and 16 each printed `Hello world!` once with no reset,
and at 16 `Detected size(16384k) larger than the size in the binary image header(8192k)`. The
image the repository pins, built in the ESP-IDF v5.3.6 container, is first checked by CI's
firmware job and the tests after it.

**Rejected:** patching byte 3 of a built image's header in the test, because the bootloader
carries an appended SHA-256 that would have to be computed again as esptool does, and the test
would then check esp32tl's patch rather than what ESP-IDF builds. Refusing the sizes in the
profile schema, because a profile also describes a real board for the serial backend, where a
32 MB module is real. Reading `flash_size` from `flasher_args.json`, because a PlatformIO build
has none and the headers are what the chip reads. Always passing the headers' size and ignoring
`flash_mb`, because a board's flash is a fact about the board, and a build for less flash runs
on it. Defaulting a profile without `flash_mb` to 4 MB, because the runner's default is already
that and the build's own size boots where 4 does not. Reading `CONFIG_SPI_FLASH_SIZE_OVERRIDE`
from `config/sdkconfig.json` to know which header the application uses, because a PlatformIO
Arduino build has no such file; a bootloader that gives less flash than its application, which
only mixed files of two builds make, is then refused although it would boot.

**Postponed:** ending a run early when the console shows a boot loop, that assert or a series of
`rst:0xc (RTC_SW_CPU_RST)` before the application starts, instead of waiting for the test's
timeout. The refusal at setup catches the cause known today, and a console pattern would have to
tell a loop from firmware that restarts on purpose, as ESP-IDF's `hello_world` does.

## The next boot is a condition the test makes; reset and power_on return nothing

Status: decided (#241)

`Board.next_boot(cause=None)` returns a `Condition[Boot | None]` for the first boot after the
call, of any cause or of `cause` only, with the boundary and non-consumption of the other
`next_*` conditions: the boots there are at the call. It is defined once on `Board`, over
`boots`, so the serial backend skips it as it skips `boots`. A boot of another cause is passed
over rather than failing the condition, as `next_message(topic)` passes over other topics; a test
that must know the very next boot's cause waits on `next_boot()` and asserts on its `reason`.
`wait_for_wake` stays as it is: it runs to the next boot without polling and fails on any boot
but `DEEPSLEEP_RESET`.

`board.reset()` and `board.power_on()` keep returning `None`. The test arms `next_boot` before the
call, as it arms `next_handshake_completed()` before a reconnect.

**Rejected:** `reset()` and `power_on()` returning the condition, made before they act. It would
give two ways to wait for the same boot, of which only one covers a restart the firmware or a
watchdog makes; the first `power_on()` has no boot after it to return, the first boot being there
already; and on serial `reset()` works while the boots cannot be seen, so its condition would
have to skip the test only when asked, unlike every other result of a call that succeeded.

**Rejected:** leaving tests to compose `condition()` over `boots`, because that is the boundary
bookkeeping the issue's tests repeated, and got wrong when they took the boundary after the call.

## The broker keeps every SUBSCRIBE and UNSUBSCRIBE in subscribe_requests, beside connection_attempts

Status: decided (#242)

The MQTT broker grants every filter, so a test is the only place that can check a device asks for
no more than a production broker's ACL would grant. Until now it could read only the subscriptions
that hold now, through `subscribed(topic)` and `session(client_id).subscriptions`: a filter
unsubscribed later, or lost when a CleanSession=1 CONNECT discarded the stored session, left no
trace, and neither the QoS asked for and granted nor the time was kept, so "subscribed before it
published" or "subscribed again after the reconnect" could not be asserted.

`broker.subscribe_requests` is a list of frozen `SubscribeRequest` records, one per SUBSCRIBE and
UNSUBSCRIBE packet, in arrival order: `at`, `client`, `connection_id`, `packet_id`, `kind`
(`"SUBSCRIBE"` or `"UNSUBSCRIBE"`) and `filters`, a tuple of `RequestedFilter` with `filter`,
`requested_qos` and `granted_qos`, both `None` in an UNSUBSCRIBE. Its name and shape follow
`connection_attempts`: a plural history on the broker, not on the session, appended before the
acknowledgement is written, with no `find_*` query beside it. The record fields follow `Message`
and `Delivery` (`at`, `client`, `connection_id`, `packet_id`). One list holds both packet kinds
because the order between them is what a test of "unsubscribed, then subscribed again" reads.
`next_subscribe(filter=None)` waits for the next SUBSCRIBE, optionally naming one filter, compared
exactly as `subscribed(topic)` compares it; its name is the packet it waits for, which is why it
is not `next_subscribe_request`: that would read as if an UNSUBSCRIBE matched too. A SUBSCRIBE or
UNSUBSCRIBE is now parsed whole before it changes the session, so one refused part-way, which
used to leave its earlier filters subscribed, or unsubscribed, on a stored session, changes
nothing and is not recorded.

**Rejected:** a history on `_State` or in `SessionState`, because a clean session discards it with
the evidence; one list per packet kind, which loses the order between them; matching
`next_subscribe`'s filter by MQTT wildcards, because the device's filter is the value under test,
and `a/#` would then match a SUBSCRIBE to `a/b`; recording only the filters and not the packet,
because the packet ID and connection tell a resubscription on a new connection from a repeated
SUBSCRIBE on the same one.

**Postponed:** `next_unsubscribe` and a `find_*` query over `subscribe_requests`; a test reads the
list until one needs a condition for UNSUBSCRIBE.

## A board profile's console is UART0 or the chip's USB-Serial/JTAG controller

Status: decided (#248)

A profile says where the firmware's console is with one top-level key, `console = "uart0"`, the
default, or `"usb-serial-jtag"`: an Arduino-ESP32 3.x firmware for the ESP32-S3 with
`ARDUINO_USB_CDC_ON_BOOT=1` and `ARDUINO_USB_MODE=1` (HWCDC), or an ESP-IDF one with
`CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y`, as on many S3 and C3 boards without a USB-to-UART bridge.
`usb-serial-jtag` is refused on the classic ESP32, which has no such controller (`console: the
ESP32 has no USB-Serial/JTAG controller; use uart0`), and allowed on the S3, C3 and C6, whose
controller esp32sim models (`UsbSerialJtag` in each chip's `periph.rs`, the first stream of
`console_take`). The test's `console` stays the same interface: `board.console` reads and writes
the controller, the backend passes `--console-port usb` to the runner, and the input goes as
`console_rx`. Moving a test from UART0 to USB-Serial/JTAG is that line in the profile and a build
with the USB console.

With the console on USB-Serial/JTAG, UART0 and its IO MUX pins are free: a `[uart.<role>]` route
may take port 0, and a route or a line its pins. The ROM prints its banner on UART0 as well
(measured below), so a route on UART0's pins is how a test sees that copy. The controller's USB
D- and D+ pads are taken instead, as UART0's pins are with a UART0 console: a route or a line there
would take them from the controller, and with them the console of a real board, which ESP-IDF's
USB-Serial/JTAG console guide warns of.

**Rejected:** a `[console]` table (`port = "usb-serial-jtag"`, pins, baud), which would add keys
that mean nothing for the controller and repeat UART0's IO MUX pins, which the chip fixes; and
keeping UART0 reserved with a USB console, which would leave a test no way to watch it.

## On the S3, C3 and C6 the ROM prints on UART0 and on USB-Serial/JTAG

Status: decided (#248)

Measured in esp32sim at `357b0e5`, running the runner without a peer once with `--console-port usb`
and once with `--console-port uart0`, both to stdout, for 2 emulated seconds:

| Image | Chip | Lines on USB-Serial/JTAG | Lines on UART0 |
|---|---|---|---|
| `console_repl_usb` (`CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y`) | S3 | 68 | 11, the ROM's banner |
| `console_repl_usb` | C3 | 65 | 9, the ROM's banner |
| `console_repl_usb` | C6 | 64 | 9, the ROM's banner |
| `console_repl` (UART0, ESP-IDF's default) | S3 | 68 | 68 |
| `console_repl` | C3 | 65 | 65 |
| `console_repl` | C6 | 64 | 66 |

The ROM's banner, from `ESP-ROM:` to `entry 0x…`, comes out on both on every chip. With the
console on USB-Serial/JTAG, everything after it, the second-stage bootloader and the application,
comes out on the controller only. With the console on UART0, ESP-IDF v5.3's default secondary
console (`CONFIG_ESP_CONSOLE_SECONDARY_USB_SERIAL_JTAG=y` in both builds' `sdkconfig`) copies the
bootloader's and the application's output to the controller too, though the REPL reads UART0. So
`console_output` with a USB console starts with the ROM's banner and holds the whole boot, and the
banner's copy on UART0 is there for a profile route on UART0's pins
(`test_the_usb_console_holds_the_rom_bootloader_and_app_and_uart0_the_rom_alone`). On the real
ESP32-S3 below, the console read through its USB-Serial/JTAG port starts with the ROM's banner too
(`ESP-ROM:esp32s3-20210327`, `rst:0x15 (USB_UART_CHIP_RESET)`); its UART0 was not wired and was not
measured.

## The serial backend runs an ESP32-S3 through its USB-Serial/JTAG port

Status: decided (#248)

`--esp32tl-backend serial` takes a board profile whose chip is `esp32s3` and whose console is
`usb-serial-jtag`; the port is the chip's own USB device. Measured on 2026-11-17 on an ESP32-S3
(QFN56) rev v0.2 with 16 MB of flash on macOS, port `/dev/cu.usbmodem1101` (USB 303a:1001), esptool
5.6.0 and pytest-embedded 2.9.3, with `console_repl_usb` built for the S3:

- Flashing: pytest-embedded's `IdfSerial.flash` runs `esptool.main` on its connected `ESPLoader`.
  esptool picks its USB-Serial/JTAG reset itself when it connects, from the port's PID
  (`USBJTAGSerialReset`, `esptool/loader.py:850`), and prints `USB mode: USB-Serial/JTAG`; the
  backend passes nothing of its own. After writing, esptool's `--after hard-reset` and
  pytest-embedded's `EspSerial.hard_reset` both pulse RTS with `HardReset(uses_usb=False)`
  (`esptool/reset.py:122-148`): 0.1 s held, no wait after; esptool 5.6.0 passes `uses_usb` from
  nowhere. Two runs of the pytest-embedded test flashed 21 392, 3 072 and 235 728 bytes and passed.
- Reset to the application: the RTS pulse resets the chip (`rst:0x15 (USB_UART_CHIP_RESET)`), and
  the port stays: no re-enumeration, the same path, and the descriptor opened before the reset
  reads on. One pulse on a port opened by the measuring script gave two resets, the first banner
  cut off at `rst:0x15 (USB_UART_CHIP`.
- What is lost at boot: nothing on this path. The port is open across the reset, and the console
  of both pytest-embedded runs held the whole boot, from the ROM's banner on. After a reset in the
  test, the ROM's banner stopped for 3 s after its first line in both runs, and the prompt came
  4.1 s after the reset instead of 1.0 s.
- `expect` and `sendline`: `help` through the port got the REPL's answer, before and after a
  reset, in the esp32tl tests `test_the_usb_console_takes_a_command_and_comes_back_after_a_reset`
  and `test_a_tap_of_the_usb_console_records_both_ways` of `tests/test_usb_console.py` on
  `--esp32tl-backend serial --port /dev/cu.usbmodem1101`.
- EN and power: `board.reset()` is the same RTS pulse; the backend switches no power, as on the
  classic chip.

What cannot be guaranteed: twice, after the measuring script closed the port and opened it
again, with DTR and RTS asserted by pyserial's default the first time and released before opening
the second, the chip stopped answering: the USB device stayed (`USB JTAG/serial debug unit`), but
esptool's connection failed with `No serial data received`, also with `--before usb-reset` and
`--before no-reset`, and the port read nothing after an RTS pulse, until the board was unplugged
and plugged in again. pytest-embedded opens the port once per test, through esptool, and the
pytest-embedded and esp32tl runs did not meet it, but a test session that opens the port many times
may. Opening the port also reset the chip once. A board whose console is USB-Serial/JTAG and whose
UART0 has a bridge was not measured on UART0.

**Not yet:** the C3 and the C6 with a USB-Serial/JTAG console skip on the serial backend
(`serial: the board profile {name} has its console on the {title}'s USB-Serial/JTAG controller,
which the serial backend has been validated on with the ESP32-S3 only`), until a board of each is
measured as above; the S3, C3 and C6 with a console on UART0 are still refused, as
[the serial backend runs the classic ESP32 only](#the-serial-backend-runs-the-classic-esp32-only)
says.

**Rejected:** skipping the S3 with a USB console too, which the measurement does not call for; and
reopening the port after each reset to follow a re-enumeration, which the S3 does not do.

## A USB console through TinyUSB is not a console the profile can name

Status: postponed (#248)

`ARDUINO_USB_MODE=0`, Arduino-ESP32's USB OTG CDC through TinyUSB, and ESP-IDF's
`CONFIG_ESP_CONSOLE_USB_CDC=y` put the console on the S3's USB OTG peripheral, not on the
USB-Serial/JTAG controller. esp32sim's USB OTG model (`esp-periph/src/usb_otg.rs`) is not a
console stream of `console_take`, and the runner has no way to read or write it.

**Not yet:** until esp32tl needs such a firmware, and esp32sim gives a host's end of its USB OTG
device; then the profile's `console` would take a third value.

## A message the broker published as a Will says so in Message.will

Status: decided (#243)

A Will the broker publishes on a connection loss is recorded in `messages` with `client` set to
the client that set it, as that client's own publications are, so a test that checks everything a
device published found the Will among them, and one asking whether the device sent its offline
message itself or the broker sent its Will had to compare bytes with `connection_attempts`.
`Message.will` is `True` for that Will and `False` for every other message; `client` keeps its
meaning. `next_message`, `find_message` and `find_messages` take a keyword `will=None`: `True`
keeps only Wills, `False` only the rest, `None` both. `find_message` takes it too, though the
issue named only the other two, because the docs say it filters as `next_message` does; only
`find_messages` also takes `since` and `client`, as before. A `will`
that is not a bool or `None` raises `TypeError`, because an `int` or a string compared by identity
would match nothing without saying so.

**Rejected:** `origin: Literal["client", "will", "test"]`. The test's publications are already
told apart by `client == TEST_CLIENT`, so `origin` would say that twice, and a client that
connects with client ID `(test)` would then be told apart by `origin` but not by `client`, two
fields that could disagree about the same message; a bool says the one thing `client` cannot.

**Postponed:** the flag on `Delivery`. A delivery is a PUBLISH the broker wrote, of a Will or
not, and a subscriber cannot tell one from the other on the wire. A test that needs it matches
the delivery to `messages` by topic, payload and time, which cannot tell a Will from a PUBLISH of
the same topic and payload the client sent at the same board time; the flag waits for a test that
needs that case.

## The P4's ROM is the one of its build's revision

Status: decided (#233)

The runner runs a P4 build on esp32sim's model of the revision the build is for
([runner: the P4 runs on the model of its images' revision](runner.md#the-p4-runs-on-the-esp32sim-model-of-the-revision-its-images-are-built-for)),
and each model boots its own ROM. `runtime.ROMS` keys the P4's two: `esp32p4`, the rev ≥ 3 ROM,
for ESP-IDF's default build, and `esp32p4-rev0` (`runtime.P4_REV0`), the rev < 3 one, with its
variable `ESP32TL_ROM_ESP32P4_REV0`. `esp32sim.rom_path(chip, app)` asks `build.p4_rev3`, which
reads the build's image headers by the runner's rule (the header at flash address 0x2000 first,
whichever file holds it, `firmware.factory.bin` from 0x0 included, else the first other file's
own header that is a P4 image's; its `min_chip_rev_full`), and the backend and the `esp32tl` command pass that
ROM with `--rom`.

**Rejected:** a ROM per board profile, because the profile names the chip and the build names
the revision, and a v1.x board runs a rev < 3 build the profile cannot know of; the runner
choosing the ROM itself from `ESP32SIM_ROM_DIR`, which would give up `--rom`, the one path the
backend checks and the runtime cache pins.

## On the P4 and the S31 the console has no pins, and a UART route skips the test

Status: decided (#233)

`boards.Chip.uart_pins` is false for the ESP32-P4 and the ESP32-S31, whose UARTs have no pins in
esp32sim ([runner: UART0's console has no pins](runner.md#on-the-p4-and-the-s31-uart0s-console-has-no-pins)).
On such a chip the backend's console has no wire: its input goes on the link as `console_rx`, as
on a USB-Serial/JTAG console (`Interface.pinless_console`, formerly `usb_console`), and the
console's output comes as `console` messages. `CHIP_GAPS` skips a test that gives a `[uart.<role>]`
route a source or a tap (#229 G6 on the P4, G7 on the S31), an `sd_card` (no SPI pin hook), `wifi`
(the P4 has no radio, and esp32sim's ESP32-C6 co-processor is not in the runner; the S31 has no
Ethernet relay, G7) and `bluetooth` (none modelled on either). Their devkit profiles,
`esp32-p4-function-ev-board` (16 MB flash, 32 MB PSRAM, the P4NRW32) and `esp32-s31-module`
(16 MB flash, 16 MB PSRAM, the S31NRV16; Espressif names no S31 devkit yet), carry the
`[lines.echo]` set of the other devkits and no `[uart.gnss]`, which could not run there.

**Rejected:** refusing such a profile at load time, because the pin table is the board's and true
of it, and the skip names the gap at the test that needs the route; a profile without
`console` pins for these chips, since UART0 is on GPIO 37/38 and 58/59 on the boards all the same,
and a route must not take them.

## The package root exports every name a test uses

Status: decided (#247)

A test named its helpers' types from wherever the package kept them: `Boot` from
`esp32tl.interfaces.board`, `Message` and `TEST_CLIENT` from `esp32tl.net.mqtt`, `ChannelEvent`
from `esp32tl.bluetooth.peer`, `u16` from `esp32tl.flash`. That tied a test suite to the
package's layout, and left the consumer to guess which names were public. `esp32tl` now exports
every class, exception and constant a test names, and `__all__` lists them; `esp32tl.units` and
`esp32tl.recording` stay public modules of their own, and every other module is internal.
`tests/test_public_api.py` walks the public methods, properties, attributes and fields of every
exported class and fails on a type that none of the three exports, so a new public method cannot
return a type a test has to fetch from inside the package.

One flat namespace needs names that say what they belong to, which the module said before. A
class whose bare name could mean two things in esp32tl was renamed: the board profile's `Line` to
`LineRoute`, as its `UartRoute` and `SdCardRoute`; the console's `Line` to `ConsoleLine`, beside
the GPIO `Lines`, `OutputLine` and `LineEvent`; the HTTP server's `Request` to `HttpRequest`,
beside the capability declaration's `Request`, and its `Response`, which never leaves the server
and is not exported, to `HttpResponse` with it; the broker's
`Client` to `MqttClient` and the IP stack's `Client` protocol to `TcpClient`; the modem model's
`Command` and `Restart` to `ModemCommand` and `ModemRestart`. The model itself, a class named
`Modem` beside the modem interface, is exported as `ModemModel`, the name the package already
imported it under. `Mark`, `Delivery`, `Publication`, `Connection` and `Source` keep their names:
each means one thing in esp32tl.

What only the backend reaches stays out. The walk leaves out the members the link's `Interface`
declares, on an interface, the package's services' `accept`, which the network calls, and the
constructors of what the backend makes; the protocols a test implements, `Model`, `Source`,
`StreamService` and the rest, keep all their methods, since the test writes their types. `Air`
kept its loop, its Bumble link and `join(party)` public, for the package's peers and controllers,
which made `BoardLoop`, `Link` and `Party` types a test meets; they are `_loop`, `_link` and
`_join` now, and none of the three is exported.

**Rejected:** a separate `esp32tl.types` module, which the issue offered: a second public module
for the same names, where `from esp32tl import Board` already worked and `Board` is a type too.
Re-exporting the units from `esp32tl` as well: `esp32tl.units` was already the one place every
document and example imported them from, and `m`, `km` or `ms` at the package root would read as
anything but a unit. Exporting the recording format's `Recording`, `Header`, `Event` and
`ClockSwitch` at the root: `esp32tl.recording` is public, and a test uses it through the module,
`recording.read(path)`, where its names keep their context. Excluding members from the walk by
name alone, in every class: `close`, `start` or `transcript` is the test's on `Air` or `Internet`,
and `receive` and `poll` are what the test writes on a `Model`. Constants for the harness's own
virtual station's MAC and access point's BSSID: a test that checks either names it, through
`wifi.station(mac=...)` or `wifi.access_point(bssid=...)`.

## A lost link leaves the device's connections half-open until their keepalive

Status: decided (#244)

The end of the device's link, `board.power_off()`, `modem.lose_registration()`,
`modem.lose_power()`, `modem.hang()`, `modem.drop_call()`, PPP ended by the device or
`wifi.access_point_off()`, used to drop every TCP connection at once through `Network.reset`, so a
broker published the device's Will at the cut. A broker on the internet hears nothing from a device
that loses its power or its coverage: no FIN, no RST. Its end of the connection stays open until the
keepalive runs out, 1.5 times the keepalive after the device's last packet (MQTT 3.1.1, 3.1.2.10),
and the Will goes then. `device_reset` already kept the device's connections through a chip reset
for that reason. Now the link's end calls `Network.lose_link()`: the connections the test opened are
lost, because the device's end of them is gone, and the device's stay, silent. The broker's
deadlines run while the link is down, the board off included, so `ModemModel` and `Wifi` reach their
`Internet` in command mode and off the air, and its `quiet_until` is the broker's and the
bridges' then. What a service writes in the meantime goes nowhere. A bridged connection stays open
at the real service too, for the same reason.

A connection the link's end or a chip reset left open is marked orphaned: the stack on the device
that held it is gone, and lwIP drops a netif's connections when it loses its address. When its
service closes it, as the broker does at its keepalive or a takeover, it is dropped at once, with no
FIN: none would be answered, whatever link the device is on by then, and kept, the flow would wait
for that answer. A DHCP lease the device gives up on a link that stays orphans the connections on
that address too, since lwIP drops them with it. The link's end and a chip reset also drop the
device's connections still in their handshake, which no service deadline would end. A virtual
station that leaves the device's own access point orphans nothing: the device keeps its stack, and
what a service writes or closes while the station is away waits for the station's next lease and
then goes in order, since this TCP sends nothing again. What a service writes on an orphaned
connection beyond the window is discarded and counted, so it cannot pile up. A chip reset or a lease
given up takes the gone stack's SYNs off the network's path, as each would open a connection left in
its handshake.

A device back with the same client ID before that takes its connection over (MQTT-3.1.4-2). With
CleanSession=0 on both connections the session goes on and the old Will is discarded unsent; with
CleanSession=1 on either it is published at the takeover, and so it is when the old keepalive's
deadline has come by the CONNECT, though the broker has not expired it yet. That is mosquitto's
rule
(`connect__on_authorised` in `src/handle_connect.c` sends the old Will only when either side starts
clean), and the one a device's backend sees in production: a device that came back inside its
keepalive has not gone. MQTT 3.1.1 itself says the Will is published whenever the connection closes
without DISCONNECT (MQTT-3.1.2-8), which before this change the broker did at every takeover.

**Rejected:** deciding at the service's close whether a FIN would reach the device, from the
address IPCP or DHCP gave on the current link or the one the device last sent from, because a
lease the device gives up leaves that last address behind, a FIN may wait for a window that what
went nowhere has filled, and no stack holds the old connection whatever link the device is on.
Also rejected: taking all of the gone stack's datagrams off the path at a chip reset or a lease
given up, because what it sent before is already on the internet, and a broker would still get
its last PUBLISH; and retransmission, to deliver what a service wrote while a station was away,
because holding it until the station is back delivers the same bytes with no timers. Also
rejected: an option on the network, `link_loss="reset" | "silent"`, to keep the immediate Will,
because no broker a device meets behaves that way, and a test that wants the broker to give up at
once has `broker.disconnect(client_id)`, which publishes the Will; a second way to say it would be
a default someone has to pick per test. Also rejected: publishing the old Will at every takeover,
as MQTT-3.1.2-8 read alone would, because the device's backend would then hear a device go offline
that never went, which is what the test of a short outage wants to rule out; and losing the test's
own connections only at their keepalive too, because the test's `TcpConnection` has no keepalive,
and the device's end of them is gone with its stack. Also rejected: a retained `online` that
`mqtt_session` publishes on `session/status` at each connection, to show the device's own message
outliving a takeover, because with it at QoS 0 after each SUBSCRIBE one exchange of
`test_a_qos1_backlog_takes_the_rate_in_both_directions` came 39 ms late (401 ms against 362 ms),
and the PUBACK `test_a_message_whose_puback_is_lost_comes_again_with_dup` looks for went in one TCP
segment with it; the takeover test reads that no Will is retained instead.

A new stack's SYN from the same port as a connection the old stack held resets that connection
and opens a new one: what a challenge ACK (RFC 9293, 3.10.7.4) comes to, with the new stack's RST
and its SYN again taken in one step. The broker publishes the old Will then, as at any reset. Kept
until a keepalive, such a connection would take the port for good when no keepalive comes, on a
connection whose CONNECT never came. esp-mqtt over lwIP connected from another port after a power
cycle in `tests/test_mqtt_session.py` (60660, then 55574). A station that leaves the device's
access point with bytes the device has not acknowledged ends that connection, as what was on the
path is gone; one that comes back with another address loses the connections on the old one.

**Rejected:** a bounded lifetime for an orphaned connection that never saw a CONNECT, because the
network does not know what its service waits for, and the new stack's SYN on the same port is
the event that shows the old connection gone.

## A firmware that starts its Bluetooth controller without board.bluetooth ends the run

Status: decided (#259)

The backend gives the runner a Bluetooth controller, `--bt` with the application's ELF, only once
the test looks at `board.bluetooth` or takes the `bt` fixture. Without `--bt` esp32sim installs no
VHCI hooks, so a firmware that calls `esp_bt_controller_init` runs ESP-IDF's own controller against
Bluetooth hardware esp32sim does not model. `bt_spp_initiator` on the classic ESP32 panicked there
(`StoreProhibited`, `EXCVADDR: 0x0000019c`) and boot-looped, 129 boots in 15 s; `gatt_server` on
the ESP32-S3 and the ESP32-C3 stopped after `BLE_INIT: Bluetooth MAC: …` and printed nothing more,
the run spending about 2.4 wall seconds for each emulated one (M2 Pro). Either way a test of the
console, Wi-Fi or the SD card passed, and nothing said why the firmware did not run.

Now the backend passes `--no-bt` with the same ELF whenever the test has not looked at
`board.bluetooth`, on every chip esp32sim models Bluetooth on, for a build whose ELF is a file (one
without it gets neither flag, and the call runs as before). The runner puts an esp32sim stub at
`esp_bt_controller_init`, the entry of ESP-IDF's controller on the classic ESP32, the S3, the C3 and
the C6 alike, which returns `ESP_ERR_NOT_SUPPORTED`, and ends the run at the end of the slice the
call came in, at most one emulated millisecond later, with `end: bt_controller` and exit code 5.
The test fails with `the firmware started its Bluetooth controller (esp_bt_controller_init) by
0.117 s, and the test gave the board no Bluetooth: look at board.bluetooth (or take the bt fixture)
before board.power_on(); board.bluetooth.use() gives the firmware the harness's controller with no
device on the air`. A firmware that links the controller and never starts it runs on. The run's
cost stays where the test chose it: a test without Bluetooth gets no controller and none of the
turns its 100 µs `latency` brings. One stub is one bucket of esp32sim's 64-bit bloom
of hooked PCs, each of whose addresses ends a block: on `bt_spp_initiator`'s boot loop, a stub at
an address the guest never runs took the median of six free runs of 15 emulated s from 13.95 to
14.85 wall seconds (M2 Pro). A firmware that starts its controller pays that until the call, about
0.1 s; one that links the controller and never starts it pays it for the whole run.

**Rejected:** giving every such firmware the harness's controller, `--bt` whenever the ELF has
`esp_bt_controller_init`, with `board.bluetooth` only adding devices, because every test of the
firmware would then pay for a controller it did not ask for: esp32sim's thirteen VHCI hooks in place
of one stub, the controller task's VHCI polls, a tick apart while nothing waits, and the
interface's 100 µs `latency`, which puts a turn of the harness 100 µs after every event the runner
sends (protocol §7). A firmware that never calls the controller would pay
it as well, and a test of that firmware's console would depend on the build's ELF. Also rejected:
only documenting that `board.bluetooth.use()` is needed, because the run that boot-loops or hangs
passes in silence, which is what the issue was. Also rejected: letting the stub answer and the
firmware run on without a controller, which an ESP-IDF example takes as a failed init
(`app_main initialize controller failed: ESP_ERR_NOT_SUPPORTED`) and returns from `app_main`: the
test would pass on a firmware doing something other than in production. Also rejected: an exact
time for the call, from an esp32sim observer, because one that stops at a PC asks for every
instruction and runs the guest single-stepped; the slice's end bounds the call to a millisecond.

## History conditions scan from where their last poll ended

Status: decided (#262)

A "next …" condition over an append-only history, `board.next_boot`, MQTT's `next_message`,
`next_delivery` and `next_subscribe`, HTTP's `next_request`, and `SppPeer.next_write` and
`next_written_line`, used to scan `history[start:]`, a copy of everything since the condition was
made, at every poll. A wait polls at every turn of the link and at least every 100 emulated ms, so
a long wait on a busy history cost time quadratic in its records: one poll of `_messages` over
5 000 records that do not match took 1.8 ms (M2 Pro, Python 3.14), and a wait of an emulated hour
with a message a second about 65 M record checks, some 24 s of the test's CPU. Now each takes as
its `find` a `FirstFrom` of `esp32tl/interfaces/condition.py`, which reads by index from where its
last poll ended and, once it has a match, only the match: a poll reads the records appended since
the last one. `next_boot` still pays for `Esp32Sim.boots`, which builds every boot at each read;
boots are few, and a cheaper `boots` is a change of the backend, not of the condition. Records are
only appended, so a record it refused never matches later. `next_data`'s search for `until`
likewise starts `len(until) - 1` bytes before where the last one ended. The documented semantics
stay: the boundary is append order, polling never moves it, and the condition consumes nothing and
returns the same record at every later poll.

`FirstFrom` keeps the index of the match, not the record, and reads it again at each poll: `boots`
is built anew at every read, and a `Boot` gains its `uptime_zero`, which takes no part in comparing
boots, after the boot has begun, so a kept record would never show it.

**Rejected:** a cursor that moves past a match, as `LineCondition`'s does, because that consumes:
the same condition asked again would find the next record, and the MQTT, HTTP and SPP conditions say
it finds the same one. Also rejected: an index per topic filter in the broker, or per path in the
HTTP server, because it adds state to every service for every filter a test arms, while a poll that
reads only the new records already costs as much as what the firmware sent since the last poll, not
as much as the wait has lasted. Also rejected: tests that bound the time of a long wait, because
their margin depends on the runner; the tests count the records a poll reads with `CountingList` of
`tests/conftest.py`, the same on every machine.

**Postponed:** exporting `FirstFrom` from `esp32tl` for a test's own conditions over a history: the
public API grows only with a test that needs it, and `condition(find, description)` with a `find`
of the test's takes any history meanwhile.

## A line's next change is an on/off condition on the output handle and a pin-level one on `lines`

Status: decided (#279)

`is_on` and `is_off` hold as long as the level does, so a wait on one returns at once while the
level already holds, and the firmware's next press of a pin needed a condition of the test's over
`lines.events()`. `OutputLine.next_change(*, on=None, by="firmware")` and
`Lines.next_change(line, level="any", *, by="firmware")` are "next …" conditions over the line's
changes, with the semantics of the others: the boundary is the call, polling never moves it, and
the same `LineEvent` comes back at every poll. The handle speaks in on and off through its
`active`, as `is_on` and `assert_on` do, so that an active-low output reads the same as an
active-high one; `lines` has no polarity and speaks in pin levels, with `None` for undriven. `by`
defaults to the firmware, since a test waits for what the firmware does, and `None` takes any
origin. The history is `Lines._events` in the order its events were appended, through `FirstFrom`.

**Rejected:** `level=` on `OutputLine`, the signature the issue sketched, because it would say again
in pin levels what the handle's `active` already says, and a test reading `next_change(level=0)` on
an active-low output would have to know which of on and off that is. Also rejected: `events()` as
the history, because it sorts a new copy at every call, and `FirstFrom` needs a history whose
records up to its old end stay where they are.

## A real receiver's capture or raw NMEA log is converted to the emulated clock, not modelled

Status: decided (#263)

A test plays what a real GNSS receiver sent by converting it into a recording on the emulated
clock and replaying that with `gnss.replay(path)`. `esp32tl recording retime IN OUT`
(`recording.retime`) converts a recording on the wall clock, such as `gnss.tap` writes beside
`gnss.real`. It keeps the `in` events at their times from the first of them, puts the first at 1 s
(`--start`), drops the `out` events and adds a `converted` field to the header.
`esp32tl recording nmea IN OUT` (`recording.from_nmea`) turns a raw NMEA log, which has no times,
into a recording. It
sends one event an epoch, an epoch a period apart (`--period`, 1 s), and checks every checksum, the
times of the RMC and GGA sentences against the period, and each epoch's bytes against the route's
baud. Both live in `esp32tl/recording.py`, the format's module, and write version 1 of the format,
which a header field of its own does not change. A replay of either is a pure stream with nothing
to answer: the timing of a receiver's answers to the firmware's commands is lost, and a stream of
NMEA sentences has none anyway. The committed example is `recordings/gnss-neo-m8n.nmea`, gpsd's
`test/daemon/neo-m8n.log` (BSD-2-Clause), a u-blox NEO-M8N at rest, converted to
`recordings/gnss-neo-m8n.jsonl`.

An NMEA log epoch starts at an RMC or a GGA with a new time, and also at a second sentence of one
address. That second rule is there because a cold start sends RMC and GGA without a time, and
the time alone would make those epochs one.

**Rejected:** replaying a wall-clock recording on the emulated clock as it is, because the clock
field is there to stop times on one clock being read as times on the other
([invariant 31](../invariants.md#recordings-and-replay)); the conversion says it was made, in the
header. Also rejected: keeping the `out` events, because a recording with requests answers the
firmware's commands by matching, and a capture's commands then go unmatched or match what the
receiver sent unprompted. Also rejected: an epoch's sentences timed one by one at the route's baud,
because the replay already sends an event's bytes back to back at the route's baud from its time.
Also rejected: taking the period from the log's own times, because a log that skips epochs then
gets the wrong rate where it should be refused.

**Postponed:** noise in the GNSS model, `stand_still(jitter=…)` or a cold-start phase, seeded so
that two runs send the same bytes. A model of a receiver's noise is a guess where a capture is
the device's own output. A converted capture covers a speed of a few tenths of a knot at rest and
the epochs of a cold start without a fix. If a model ever does it, the converted recordings are
what to compare it with.

## Bluetooth LE peers on Bumble's LE link layer, with esp32tl's fixes

Status: decided (#234)

The LE devices beside the board, `BlePeripheral` and `BleCentral`, are Bumble `Device`s with LE on
and BR/EDR off, on the same `LocalLink` and the same kind of controller as the Classic peers. Their
GATT server and client, their Security Manager (LE Secure Connections, Just Works, passkey entry and
numeric comparison, bonding with identity resolution) and their advertising and scanning commands
are Bumble's, unchanged. What Bumble 0.0.236's controller and link get wrong for a firmware's
Bluedroid is fixed in `esp32tl/bluetooth/link_layer.py`, a `LinkLayer` between Bumble's controller
and esp32tl's `Controller`, and in `Link`: the advertising interval in units of 0.625 ms, which
Bumble reads as milliseconds; the legacy advertising types beside `ADV_IND`; the scan response, which
Bumble replaces with the advertising data; the initiator's parameters in the peripheral's
`LE_Connection_Complete`; `LE_Create_Connection_Cancel`, which Bumble answers and does nothing for;
the filter accept list and the resolving list, which it does not keep; the resolvable private
address a controller makes for own address type `0x02`, which Bluedroid's local privacy asks for,
where Bumble advertises from an all-zero random address; `LE_Enhanced_Connection_Complete`, without
which Bluedroid's SMP pairs with an address the central never saw, and the DHKey check fails; LE
data sent from the connection's address, where Bumble's link sends from the controller's random
address whatever the connection's; LE link supervision, which Bumble does not have; `LE_Rand` from
the controller's own generator; `LE_Set_Data_Length`, which Bumble lists and has no handler for. With
them, ESP-IDF's `gatt_server`, `gatt_client` and `gatt_security_server`, unmodified, ran on the
ESP32-S3 and the ESP32-C3 with `bt.unknown_commands() == []` on 2026-11-19.

The central's operations over a connection return a `Condition` for `board.wait_until`, as
`HttpClient` returns an exchange whose `response` is one: on `bt.air` only the board's link moves
the time, so a call cannot wait itself. A security request from the peer is answered as a phone
answers it, by encrypting with the keys kept for the peer, or pairing without. A pairing is a bond
when both sides asked for one and it gave a long term key, of LE Secure Connections or of legacy
pairing; without a bond the device keeps no key, though Bumble 0.0.236 stores them whatever was
asked. The "next …" conditions read only the records appended since their last poll, as the
others since #262 do.

**Rejected:** a link layer of esp32tl's own, below Bumble's hosts, with connection events and their
timing, because nothing the issue's tests ask for depends on the timing, Bumble's handlers answer the
rest of what Bluedroid sends, and the Classic link carries data at the instant it is sent too. Also
rejected: asking Bumble upstream for the fixes first, because each was found by running Bluedroid
against it here, and esp32tl pins an exact Bumble; they can go upstream once settled. Also
rejected: blocking calls on the central that let the board run until they are done, because a peer
would then hold the board, which the air does not know of, and a test could not start two things at
one instant. Also rejected: Bumble's own `Advertisement` for the central's `advertisements`, because the one
its accumulator makes of an advertising event and its scan response keeps the scan response's bytes
alone in `data_bytes`, and the two merged in `data`: a test that asks what went where cannot tell.

## An LE-only chip's controller reports LE alone and takes Espressif's report flow control

Status: decided (#234)

On the ESP32-S3, the ESP32-C3 and the ESP32-C6 (`LE_CHIPS`), the guest's controller reports what
an ESP32-S3 (QFN56, revision v0.2) reported on 2026-11-19 to `tools/hci_probe`, an ESP-IDF v5.3.6
image that asks the chip's own controller through VHCI, with Bluedroid configured as the BLE 4.2
examples are and never started: page 0 alone, `LE Supported (Controller)` and `BR/EDR Not
Supported`; version 5.0 and Espressif's company identifier; LE features `0xFF`; an LE buffer of 251
bytes and 12 packets; its list sizes, data lengths, advertising power and supported commands; 5
command packets; and `Command_Complete` with `UNKNOWN_HCI_COMMAND` for every command it does not
know, `Inquiry` among them. The classic ESP32's is unchanged: BR/EDR, pages 0 to 2, no LE. The
interface learns the chip from the board profile's `chip`. The probe asked in Bluedroid's order
where it matters: a first run, with the controller built for BLE 5.0 as a controller-only image
builds it (`BLE_50:1`), got `UNKNOWN_HCI_COMMAND` for `0xFD09` and LE features `0x080001F9FF`, and
the unmodified `gatt_client` on the same board logged no error for `0xFD09`; the controller's mode
follows the host's configuration at build time (`BT_CTRL_50_FEATURE_SUPPORT`,
`components/bt/include/esp32c3/include/esp_bt.h`), so the values are those of a BLE 4.2 build,
which all the BLE validation images are. The C3's controller is built from the same sources and the
C6's is not; neither was measured, and both take the S3's values until they are (`TODO(owner)`).

It also takes Espressif's vendor commands `0xFD09` and `0xFD0A`, the flow control of advertising
reports Bluedroid turns on for an LE controller, and keeps to the credits: Bluedroid asserts in
`hci_adv_credits_consumed` on a report it has no credit for, and sends `0xFD0A` without waiting for
an event, so the controller answers it with none. Past the lost threshold, the next credit given
back first brings the vendor LE Meta event `0xF0` with the number of reports dropped, as the S3
sent it (104, when 2 credits came back after 2 reports and a threshold of 3).

**Rejected:** leaving the vendor commands to the unknown-command answer, because `0xFD0A`'s
`Command_Complete` reaches a host that waits for none (`filter_incoming_event command complete event
with no matching command`), the list of unknown commands a test asserts empty would never be, and a
scan longer than the credits would stop the firmware. Also rejected: reporting the LE features of the
S3's controller built for BLE 5.0, with extended advertising, because the harness's link does not
carry extended advertising, and the validation images run it built for BLE 4.2. Also rejected:
keeping Bumble's answers to the commands the S3 does not list, `Read_Buffer_Size` and `Inquiry`
among them, because a host that sends one would then work here and not on the chip.

## Extended advertising, LE Audio, the coded PHY and several connections wait

Status: postponed (#234)

Left out of the LE link layer and the peers: extended and periodic advertising and scanning, the
coded PHY, isochronous channels and LE Audio, directed advertising, more than one connection on a
`BleCentral`, the connection's events and their timing, the resolution of a peer's private address
in the firmware's controller (Bluedroid resolves it itself), and out of band pairing. Bluedroid's
examples on the S3 and the C3 are built with `CONFIG_BT_BLE_42_FEATURES_SUPPORTED` and use none of
them. Each is for a firmware that needs it; a firmware built with
`CONFIG_BT_BLE_50_FEATURES_SUPPORTED` uses extended advertising, and the controller's LE features
say it has none.

## An escape is a mode change, not a modem command

Status: decided (#276)

`modem.mode_changes` records each dial the modem answered and each return to command mode as a
`ModeChange(at, mode, cause)`, so that a test counts a firmware's PPP attempts by its dials.
Inferring them from `commands` meant grouping unanswered `AT`s by the gaps between them: ESP-IDF's
`esp_modem` sync step sends `AT` up to three times, and in data mode those are PPP payload. A dial
refused with `NO CARRIER` is an entry into command mode, cause `"no carrier"`, so every answered
dial has one entry with its result. An escape is recorded only once it takes effect, as an entry
of cause `"escape"`.

**Rejected:** recording `+++` in `commands` as well. `commands` holds AT command lines, each stamped
at the chunk with its terminator and answered 1 ms later; an escape has no terminator, with a
network its `OK` is due a second after its last plus, and one that does not take effect is PPP
payload, not a command. A `ModemCommand` for it would be a line the firmware never sent, and its
`at` would not be the time the mode changed.

**Rejected:** a `"cfun"` cause. The model reads `AT+CFUN=` only in command mode: `+++` ends the
call and there is no online command mode, so functionality never ends a call, and the cause could
never be recorded. `AT+CFUN=0` and `=4` no longer call `_end_data`, which in command mode only
stopped an `Internet` already stopped.

## The broker keeps how each connection ended in connection_ends, not on MqttClient

Status: decided (#274)

A `_Session` ended in six ways, by a DISCONNECT, its keepalive, a takeover, its stream closing,
a malformed packet or `broker.disconnect`, and only the transcript's lines told which, and the
DISCONNECT had no line at all. A test asking whether the device said goodbye before its link went
reassembled MQTT packets from `network.events` hex. Each accepted connection now gets one frozen
`ConnectionEnd` (`at`, `client_id`, `connection_id`, `by`, `will_sent`) in
`broker.connection_ends`, in the order they ended, and a DISCONNECT is logged
`mqtt: <id> sent DISCONNECT`. `broker.ended(connection_id)` reads one connection's end, and
`broker.next_end(client_id=None)` is a `FirstFrom` condition over the list, as `next_subscribe` is
over `subscribe_requests`.

A history, because `MqttClient` is frozen and appended at CONNECT, when its end is unknown, and a
`next_*` condition needs an append-only list to scan from where its last poll ended; ends come in
another order than connections, so the list is its own. The record carries `client_id`, as
`MqttClient` does, since `next_end` filters by it, and `connection_id`, which ties it to its
`MqttClient`. A connection never accepted records no end: a rejected CONNECT's outcome is in
`connection_attempts` already, and a stream broken before its CONNECT has no client ID to name.

**Rejected:** a mutable `ended` field on `MqttClient`, because the record would change after a test
read it, and every other history record of the broker but `Delivery.acknowledged_at` is frozen; a
`Literal` cause `"lost"` for a lost link, because the broker cannot tell one, which is the point of
[the half-open connection](#a-lost-link-leaves-the-devices-connections-half-open-until-their-keepalive):
it ends as `"keepalive"`.

**Postponed:** a per-connection history of control packets, `broker.packets(connection_id)` of
CONNECT, PINGREQ, DISCONNECT and the rest. The end's cause answers the question the issue asked,
whether a DISCONNECT came before the link went, and the transcript keeps every byte for a test that
needs the PINGREQ times; the history waits for such a test.

## NTP and DNS keep a history of what the device asked, with board times

Status: decided (#273)

`Ntp.exchanges` keeps every well-formed client request as an `NtpExchange(at, answered)`, silent
or not, and `Dns.questions` every question it parsed as a `DnsQuestion(at, name, qtype)`, each at
the board's time it reached the service. A firmware's SNTP client retries on its own schedule and
resolves its server's name again when its DNS cache expires; a test checks either from these
histories, or waits for the next record with `next_exchange(answered=...)` or
`next_question(name)`, which follow the `next_*` rules of the MQTT conditions through `FirstFrom`.
`Dns` takes the board's clock as `now`, as `Ntp` does.

`Ntp.requests` and `Dns.asked` stay, as read-only properties derived from the histories: the
number of answered exchanges and the list of the questions' names. They are the short form of the
most common assertions, that the device synchronized and that it resolved a name, and a second
record that the services kept apart from the history could disagree with it.

**Rejected:** removing `requests` and `asked`, because every check of them would become a
comprehension over the history saying the same thing. Also rejected: a test's own wrapper of
`network.ip.udp[53]` and `network.ip.udp[123]`, the only way before, because that table is the
network's, not the test's.

## Behind a Wi-Fi access point the network records no link changes

Status: decided (#275)

`network.link_changes` records the link coming up and going down behind a modem, at IPCP open and
at each way the call or PPP ends, and behind a virtual station, at the runner's `wifi_link`
transitions. Behind the virtual access point it records nothing, and `next_link_up` and
`next_link_down` raise `ValueError` there. The network behind the access point sees only the
Ethernet frames the device sends: the runner sends `wifi_link` for its own virtual station alone
(`Session` reads `station_state()`, which is that station's `StaLink::Joined`), and no event says
when the device's station associates with the access point or leaves it. So the network knows of no
down that the device's firmware causes itself, as `esp_wifi_disconnect()`, and an up recorded at
the next DHCP lease would come after no down. `network.dhcp.leases` keeps every lease the device was given, each stamped `at`,
with `next_lease()`, and `wifi.events` the test's own switches of the access point; together they
are what the network can know.

**Rejected:** the DHCP acknowledgment as up, with `wifi.access_point_off()`, a device reset and the
device giving its address up as down, because a down the network cannot see would leave the record
up across a reconnect, and the reconnect a test measures would have no up. Also rejected: an empty
list behind the access point with conditions that wait without raising, because such a wait can
only time out. A record of association needs a `wifi_link`-like event from the runner for the
access point's side; it comes with a test that needs it.

## A black hole is a value of the path, and a port is blocked by dropping or refusing

Status: decided (#277)

`network.set_path(drop=True)` makes the path drop every IPv4 datagram that enters it, either way,
and `set_path()`, or a call with an RTT or rates, restores it. A black hole is one more value of the
path, so it follows the rule the path already has: each call states the whole path, and what it
leaves out goes back to its default. `drop=True` takes no `rtt`, `downlink` or `uplink`, since it
delays nothing. A dropped datagram is gone: it takes no time at a rate and no place in the FIFO, so
the first datagram after the restore leaves at the path's RTT. A long RTT could not do this: nothing
overtakes the datagrams sent into it. LCP, IPCP, ARP and DHCP stay on the link, as they do for the
RTT, so the link stays up and the device's link keepalives are still answered.

The network's TCP has no retransmission, since the link it was written for loses nothing. A segment
the network sends into the black hole would leave a gap that nothing fills, and the device's stream
would stall after the restore. So while the path drops, what the network's TCP has to send waits, as
it does while the link carries no IP, and goes at the restore: that is when a real stack's
retransmission would get through. The other way, the device's ACKs and window updates that the
black hole swallowed, the restore asks for again with one keepalive probe (RFC 9293, 3.8.4) on each
connection with something unacknowledged or waiting, since with a full window nothing else would
move it on; and a SYN-ACK lost in the black hole is sent again when the device's SYN comes again,
as in SYN-RECEIVED. Both stand in for the retransmission timer this TCP does not have, at the one
moment the harness knows something was lost. A connection the test opens would have its SYN dropped
and never sent again, so `network.connect` raises `NotReachable` in a black hole.

`network.block(port, how="drop" | "refuse")` and `network.unblock(port)` act on one TCP port of the
network, on every address, where the device's segments arrive: what was on the path before the call
is blocked too, as a firewall in front of the service would block it. `drop` swallows the device's
segments and holds what the port's connections have to send, for the same reason as the black hole,
and `unblock` probes them as the restore does. A probe goes only where something may have been
swallowed: after a black hole or a `"drop"` block, not after `"refuse"`, and one due while the link
carries no IP waits for it.
`refuse` answers a SYN with RST, as when nothing listens, and leaves open connections alone, as a
service that stopped accepting does. The test's own connections, whose network port is the one they
come from, are not blocked.

**Rejected:** a retransmission timer in the network's TCP, because the black hole and a dropped
port are the only losses, and their end is known: a probe at that instant does what the timer
would, with no timer to schedule, keep deterministic and show in `quiet_until`. Also rejected:
`network.black_hole()` and `network.restore()`, because they would be path state that
`set_path` does not state: a later `set_path(rtt=…)` would either keep the black hole, against the
rule that each call states the whole path, or lift it unasked. Also rejected: sending the network's
segments into the black hole, because with no retransmission the device's stream would have a gap
after the restore that a real network does not leave. Also rejected: `refuse` answering every
segment of an open connection with RST too, because that ends a connection the test did not ask to
end; `drop`, then `unblock`, or the broker's `disconnect`, does that. Also rejected: blocking UDP
ports, because what the scenarios need is a service on TCP that stops answering while DNS and NTP
answer; a UDP block can come with a firmware that needs it.

## board.nvs merges by values, before power-on and between boots

Status: decided (#278)

`board.nvs(..., merge=True)` reads the partition's values as they are now, with `nvs_values`, puts
the test's keys over them, and writes the whole partition anew with `nvs_image`: before the first
power-on over the build's image and the test's earlier calls, after a `power_off()` over the
runner's flash. With the board off after its first power-on, the write goes to the runner in the
turn the test holds ([the peer writes the flash in its turn, with the supply off](runner.md#the-peer-writes-the-flash-in-its-turn-with-the-supply-off)).
With the board on, `board.nvs` raises `BoardOff`, as `power_on()` on a board that is on does.

**Rejected:** a merge that keeps the firmware's layout of its NVS items, editing the pages in place.
What the firmware reads back is the same, and a second NVS writer in esp32tl, beside Espressif's
generator, would be a second place for the format to go wrong.

**Rejected:** `BackendError` for a board that is on, as before #278, because the cause is the
board's state, which the test changes with `power_off()`, not the backend's setup.
