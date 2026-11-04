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
`test_rom_is_the_one_make_setup_fetches` checks that `ROM_RELEASE` and `ROM_ELF` match
`scripts/fetch-rom-elf`.

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

Status: postponed (#51)

`Console` has `expect` and `expect_exact`, which call the dut's own, and no `write`: the runner does
not read its stdin.

## The serial backend is postponed to M4

Status: superseded (#25)

Before #25, `esp32tl.backends.SERVICES` had only esp32sim, so tests had one backend parameter.
[Serial is now opt-in](#serial-is-opt-in-and-pytest-embedded-owns-flashing-and-the-port), with
pytest-embedded's `esp,idf` services. The default still runs esp32sim only.

## Prebuilt runner binaries are postponed to M10

Status: postponed (#40)

The backend finds the runner in the checkout, so from a wheel only `ESP32TL_RUNNER` and
`ESP32TL_ROM` find the runner and the ROM.

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
recordings still cannot be replayed into the console: the runner has no console-input route.

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
device an address and a DNS server), IPv4, UDP, and a TCP that only accepts. The runner paces
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

## A bridge from the simulated network to a real service is postponed

Status: postponed (#67)

A consumer may want its real backend at the far end, in a container or on the host, at the cost
of wall time and of repeatable runs, as with the `real` source. Nothing needs it yet: the services
in the harness are enough for every test written so far.

## A TLS stack with a seeded random source is postponed

Status: postponed (#68)

The device's side of a TLS session repeats: esp32sim's random number generator is a xorshift from a
fixed seed and the cycle count (`esp-periph/src/rng.rs`), so the firmware draws the same bytes in
every run. The harness's side does not: Python's `ssl` takes its keys and nonces from the system. On
the spike two runs were identical up to the TLS handshake and differed by 1 to 3 emulated
milliseconds after it, with the same messages in the same order. Why the times move was not
measured; the likely cause is the device's arithmetic on the server's fresh keys, which takes a
different time for different values. What would fix it is on the harness's side only: a TLS server
with a seeded random source, or fixed ephemeral keys. Until that proves worth its cost, the
determinism job (`make determinism`) is to compare what crossed the link in plain text and leave out
the times after a handshake; the simulated network brings the TLS part of the check (#30).

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

## The test makes the SD card fail or slow down with sd_card.fail and sd_card.delay

Status: decided (#107)

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

**Rejected:** a callback the harness runs on each block operation, because the card answers within
the transaction, and a callback a turn away could not; one `fault(op, action, ...)` with the
action as a string, because two methods say what they do and check their own arguments; a count
of 1 by default, because `fail("write")` reads as every write; and block offsets in bytes,
because the card and its driver count in blocks.

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
hold it, and it would have to be built or downloaded. Which controller the ESP32's side talks to
is #34's to choose, once esp32sim has an HCI transport for Classic. Also rejected: a delay per
packet on the link, because no test needs one and any value would be invented.

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

Status: decided (#33)

A second device that asks for the channel while one holds it, or has been given it, is refused,
as a device with one serial port refuses it. Unless the test says `secure=False`, the channel
opens only on a link the device has authenticated, with the PIN or a key, which the peer checks
when the device asks for the channel; the peer does not start the pairing itself. A firmware that
never pairs then fails against the peer, as it would against a device that requires pairing,
instead of passing. Encryption is not required: what a firmware that never pairs lacks is the
authentication, and whether ESP-IDF's Bluedroid encrypts a link to a device without Secure Simple
Pairing when SPP asks for `ESP_SPP_SEC_AUTHENTICATE` is not known; requiring it could fail
firmware that a real device accepts. #34 can find out on `bt_spp_initiator`.

**Rejected:** requiring encryption, for the reason above; giving the channel to the last device
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

Status: postponed (#129)

Peers pair with a PIN only, and the Bumble controller's own Secure Simple Pairing is used only
between two devices that both support it, which no `BluetoothDevice` does. What a firmware's stack
does with a device that has it can only be seen with firmware, so it waits for #34, whose checklist
holds it.


## A remote name request nobody answers ends at the page timeout

Status: decided (#129)

A device in the ESP32's place can ask a peer that left for its name, and Bumble then waits for an
answer that never comes. The controller ends the request in `PAGE_TIMEOUT` at the page timeout, the
same mechanism as a page's, and the error Bluetooth Core 5.4 names for this command (Vol 1, Part F,
2.4). The issue named only the page.

**Rejected:** documenting the hang, because a test would learn of it from a test that hangs.


## A page that respects page scan is postponed

Status: postponed (#135)

Bumble's controller answers a page whatever the target's page scan says, though an inquiry respects
inquiry scan, so `set_connectable(False)` does not stop a device that knows the address. It predates
#129, and is reachable only through `peer.device`, because the peer's API has no call that turns page
scan off.


## Connection_Accept_Timeout is postponed

Status: postponed (#34)

The page's timer runs until the target's host accepts, and a target's host has no time limit (Vol 4,
Part E, 6.7, is not modelled). A host that accepts after the page ended makes a link although the
pager reported `PAGE_TIMEOUT`, and when the pager has tried again, the first answer connects the
second page. Bumble's host accepts at the instant it hears, so this takes a slowed host, and firmware
behind #34 is the first that will be slow: what it needs can be seen then. Until then an answer with
nothing left to answer gets a failed `Connection_Complete`, and changes nothing else.

**Rejected:** ignoring the late answer on the pager's side, because the target would be left holding
a link with no other end, and adding the timeout now, because nothing would exercise it but its own
test.


## A bt fixture, an HCI tap and a replayed peer are postponed

Status: postponed (#33)

The peer is used from Python on an `Air`, and tested with a Bumble device in the ESP32's place,
because esp32sim has no Classic controller for the guest (plan finding 9, #34). The `bt` fixture,
the btsnoop tap and the validation with `bt_spp_initiator` come with #34; replaying a recorded SPP
exchange needs the matching engine (#20), and plan M8's replayed peer waits for it.

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
time, with polling, USB, driver and host scheduling delays. Both record only `out` events and
share the app-image hash helper. A console tap is a test step in `RunLog`; its path is visible in
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
binary PPP bytes. Power, registration and functionality loss reset PPP, queued replies and TCP
sessions; a fresh dial negotiates again. The broker retains its test-side history and retained
messages, while connection loss emits Wills once. No unsolicited `NO CARRIER` is emitted yet.

**Rejected:** enabling PPP for every AT-model call, because existing firmware tests deliberately
dial and escape without a PPP stack; and recognizing every binary `+++` as an escape, because
valid PPP/TLS payloads may contain it. Fully model-specific escape timing and unsolicited modem
notifications wait for a firmware test that needs them.

## The MQTT service implements ephemeral sessions and QoS zero and one

Status: decided (#30, #104)

The broker grants subscription QoS at most 1 and refuses QoS 2 publications. Subscriptions and
unacknowledged deliveries belong to a connection and are not restored on reconnect; CONNACK
reports no session present. Retained publications belong to the test's broker and survive modem
power cycles. Credentials are recorded without authentication by default; #103 adds an optional
test-controlled CONNECT policy (see below). The current validation example uses clean sessions.
Persistent session restoration still waits for a test that needs it, rather than pretending to
have full broker persistence.

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
CONNECT metadata, board timestamps and return codes. It is separate from accepted `clients`
to preserve existing assertions and `connected()` waits. `connection_attempt()` waits for a
future outcome. Malformed input and TLS failures have no CONNACK outcome and are not recorded
there. Policy exceptions are test errors, rather than hiding test bugs as authentication
failures or malformed firmware traffic. A directly used broker raises them from `on_data`; in a
board run the policy runs in the link's turn, so the link fails naming the exception and the run
ends, and the test's wait raises `RunEnded`. Both histories survive network reset.

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
delivery. Persistent sessions remain unsupported, including when CleanSession is zero.

## TLS determinism compares explicit plaintext transcripts

Status: decided (#30)

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
condition for a request, and every request is kept. It answers one request per connection and
closes it, and refuses chunked bodies and heads or bodies over its limits; firmware that fetches
over HTTP (plan M7's validation, ESP-IDF's `http_request`) needs nothing more yet.

**Rejected:** a server the test has to install with `network.listen(80, ...)` first, because a
test of a firmware that fetches would always do so; and keep-alive, chunked transfer and TLS by
default, until a firmware's test needs them (TLS is `network.listen(443, network.http, tls=ca)`
already).

## A Wi-Fi tap, signal changes and the esp32tl command's Wi-Fi are postponed

Status: postponed (#31)

What crosses the air is in the network's transcript, plain text after TLS, and nothing records the
Ethernet frames themselves, as a pcap could; the signal of the access point is esp32sim's fixed
-40 dBm and does not change during a run; `esp32tl run` and `esp32tl replay` pass the runner no
`--wifi`. Each comes when a test or a run by hand needs it.

## Explicit build paths are relative to the application and never fall back

Status: decided (#102)

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

Status: decided (#108, #116)

MQTT exposes immediate `find_message` / `find_connection` history queries and callable
`next_message` / `next_connection` conditions. The existing `message` / `connected` names keep
their future-only semantics. `subscribed` continues to describe current subscription state.
TLS retains cumulative `handshake_started` / `handshake_completed` properties and adds
`next_handshake_started()` / `next_handshake_completed()` factories. MQTT conditions now name
their expected event through `repr` and reject boolean coercion like TLS and report conditions.

Next-event construction captures the relevant list length or counter, not a timestamp or the
first polling time. Appends and counter increments order equal-time events. Polling consumes
nothing and never moves the boundary; a new wait for another event needs a new condition.
TLS completion means a completion event after construction, including an already-started
handshake. Tests must arm conditions before the action that can produce their event. Immediate
history queries find a fast publication even after waiting for its connection has returned.
Application payload schemas stay in the consumer's adapters. `connection_attempt()`, from #117,
takes the same condition object and boundary but keeps its name; it gets no `next_*` alias and
no `find_*` query beside the public `connection_attempts` history.

HTTP and DHCP (#116) take the same boundary, names and condition object: `next_request` /
`find_request` and `next_lease` / `find_lease`, with `requested` / `leased` kept as aliases of the
`next_*` names. `find_request` filters by path and method as `next_request` does; `find_lease`
reads the first lease, as `find_connection` reads the first CONNECT. The condition object lives in
`esp32tl/net/condition.py`, shared by MQTT, HTTP and DHCP, and its `TypeError` names the service.
TLS keeps `_HandshakeCondition`: it already names its event and rejects boolean coercion, so
folding it in would change code without changing behaviour. `next_request` and `find_request`
refuse a path filter holding `?` with `ValueError`, because `Request.path` has no query and
`assert find_request("/a?b") is None` would otherwise pass for a request the firmware did send.
`serve` refuses one too, since it could only ever lead to a `404`.

**Rejected:** changing existing MQTT defaults to include history, which could silently satisfy
reconnect waits with stale events; making every condition future-only, which loses useful
current-state and cumulative queries. A public shared cursor is unnecessary for these scopes:
explicit methods express them without requiring callers to compare timestamps, edit counters
or manage cursor ownership across brokers and TLS listeners. Also rejected: matching a filter
holding `?` against `Request.target`, which would give `path` two meanings depending on its
content.

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
the first `power_on()` still starts the runner, and sources, taps and `board.nvs()` are still
given before it, also when a later boot would find them.

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
