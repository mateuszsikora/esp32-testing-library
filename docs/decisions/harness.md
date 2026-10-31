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

Status: superseded (#12)

`Esp32Sim.close` sent `SIGTERM`, waited 5 s, then sent `SIGKILL`, and passed exit code 0 and the
signals it had sent, because the runner, started without `--board`, handled no signal. Since the
backend starts it with `--board`, see
[Teardown waits for the runner's hello, sends one SIGTERM and passes exit code 0 only](#teardown-waits-for-the-runners-hello-sends-one-sigterm-and-passes-exit-code-0-only).

## Teardown waits for the runner's hello, sends one SIGTERM and passes exit code 0 only

Status: decided (#12)

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
expect would time out after 30 s with no word of the cause.

## The EOF takes the link's error from BoardLink, not from the log

Status: decided (#12)

`end_stream_at_end_of_run` gets the link's error from `link_error()`, which reads `BoardLink`'s
`error` once the end-of-run line has been read; the backend writes that line after the link has
been read to its end. The `esp32tl: the link failed: ...` line stays in the log for whoever reads
`dut.log`.

**Rejected:** finding the reason in the log with a pattern, because the guest's output shares the
log: a long last line without a newline cuts the pattern's leading `\n` off at the 2 KiB tail, and
the guest can print the same words.

## BoardLink checks every input through the codec before it sends it

Status: decided (#12)

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
`stats` and the error, for code.

**Rejected:** the property as a `dict`, because JUnit XML writes a property's `str()`, which is
Python's `repr`, not JSON. Also rejected: pytest warnings for the runner's `warn` messages, because
a run may send many, and they belong to the run, not to the code that triggered a warning.

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

*Decided in #64 to give way to [the board's clock is the
test's](#the-boards-clock-is-the-tests-emulated-time-moves-only-while-the-test-waits); this is what
the code does until that lands.*

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

## Board profiles are TOML files in the package, checked key by key

Status: decided (#13)

A profile is `esp32tl/boards/<name>.toml`, read with the standard library's `tomllib` and checked
by `esp32tl.boards.load`: required and unknown keys, integer ranges, the classic ESP32's GPIOs
(a UART's TX not on the input-only 34 to 39), one route per UART and one use per GPIO. Every error
names the file and the key. The profiles ship in the wheel, and a test names one with the
`board_profile` marker; `esp32-devkitc` is the default because it carries every validation
firmware (plan finding 12).

**Rejected:** a schema library, because it would be a dependency for six keys, and its messages
would not name the key the way a person writes it (`uart.gnss.tx`). Also rejected: a path to a
profile file of the project's own in the marker, because no consumer needs it yet and it would fix
how a relative path resolves before anyone has asked.

## The LilyGO profile leaves out what the specification does not give

Status: decided (#13)

*Decided in #64 to give way to [the LilyGO profile takes its GNSS route, flash and PSRAM from the
first consumer's
firmware](#the-lilygo-profile-takes-its-gnss-route-flash-and-psram-from-the-first-consumers-firmware);
this is what the code does until that lands.*

`lilygo-t-a7670g-r2.toml` holds the chip, the modem's UART (UART1, GPIO 26/27, 115200 baud) and its
`pwrkey` and `ri` lines, all from the specification, and the pull-down on `ri` that stage 0 found
([gap 6](../stage0-findings.md#gaps)); `flash_mb` and `psram_mb` are optional in the schema and
absent here. `test_the_lilygo_profile_holds_only_what_the_specification_gives` compares the
file's data whole.

**Rejected:** `flash_mb = 4` and `psram_mb = 4`, as the plan's §4.5 sketch has them, because the
specification gives neither and they would be written from memory, which it rules out.

## The capability declaration is a TOML file beside the profiles, per interface kind

Status: decided (#13)

`esp32tl/boards/capabilities.toml` declares, per backend and per interface kind (`console`,
`uart`), the sources a backend takes and whether it can tap. It declares `serial` with the
specification's table (real only, only the console tapped) although `serial` is not in `SERVICES`
yet, so that the skip has data to act on and #25 adds only the backend.

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

Status: decided (#13)

*For the `source` marker, decided in #64 to give way to [what is at a route's far end is what the
test does with
it](#what-is-at-a-routes-far-end-is-what-the-test-does-with-it-before-power-on-not-a-marker); this
is what the code does until that lands. The `tap` marker keeps it until #21.*

The `source` and `tap` markers name what a test needs; `pytest_generate_tests` marks the
parameter of each backend that cannot give it with `pytest.mark.skip(reason=...)`. pytest skips
such an item before setting up any fixture.

**Rejected:** `pytest.skip` in the `board` fixture, because `board` depends on pytest-embedded's
`dut`, which for a backend running `esp,idf` opens the serial port before `board` runs. Also
rejected: deciding from what the test body calls (`gnss.replay(...)`), because the body runs after
the backend has started.

## The closest source marker wins per interface

Status: decided (#13)

*Decided in #64 to give way to [what is at a route's far end is what the test does with
it](#what-is-at-a-routes-far-end-is-what-the-test-does-with-it-before-power-on-not-a-marker); this
is what the code does until that lands.*

For each interface, the `source` marker closest to the test decides, as the closest `app_path`
and `board_profile` do, so a test can override its module's source. Each class of a test class's
MRO counts as a level of its own, the class before its bases: pytest puts a class's and its
bases' markers on one node, bases first, which would make a subclass's `source` clash with its
base's. Two `source` markers on the
same test, class or module for one interface are a collection error, since neither is closer.
`tap` markers add up over every level: a tap asks for more, never for something else.

**Rejected:** refusing two `source` markers for one interface on any levels, because a module
could then not set a default that one test changes.

## Board markers that reach a test after collection read it are refused

Status: decided (#13)

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

Status: decided (#13)

*For the `source` marker, decided in #64 to give way to [what is at a route's far end is what the
test does with
it](#what-is-at-a-routes-far-end-is-what-the-test-does-with-it-before-power-on-not-a-marker); this
is what the code does until that lands. The `tap` marker keeps it until #21.*

A `source` or `tap` marker that names an interface the profile lacks, or a source or tap that no
declared backend gives, fails collection with the test's id.

**Rejected:** skipping such a test on every backend, because a misspelt source (`"replya"`) would
then pass the run as skipped, never as wrong.

## The run waits before the guest boots until the test has given its declared sources

Status: decided (#14)

*Decided in #64 to give way to [the board's clock is the
test's](#the-boards-clock-is-the-tests-emulated-time-moves-only-while-the-test-waits) and [a run
starts at board.power_on()](#a-run-starts-at-boardpower_on-and-so-does-the-runner); this is what the
code does until that lands.*

The specification's test calls `gnss.replay(path)` in its body, after the `board` fixture has
started the runner. The runner sends its first `sync` at `t = 0` and boots the guest only after
the harness's first `wait` (protocol §6, rule 4), so `BoardLink` holds that first turn until every
interface a `source` marker declares has its source; emulated time does not move meanwhile. A
recording replayed from power-on then starts where it was recorded, and two runs of it give the
same console. The wait is wall time in the runner's turn, so `--peer-timeout`, 60 s by default,
bounds it: a body that takes longer before its replay ends the run with exit code 4. A
`console.expect` while the run still waits raises `SourceError` at once, since the run
could not reach what it waits for. A run still waiting at teardown is ended with `stop`, and a
test whose setup and body passed all the same gets an error at teardown: it ran no firmware, and
would pass whatever the firmware does.

**Rejected:** starting the run at once and the replay when it is called, because with nothing on
the link the runner runs free to the end of the run before the body starts, and the replay's start
would depend on wall time; starting the run at the first expect, because a test that waits on
nothing else would never run its firmware, and the start would hang on an implicit trigger; and
giving the source in a fixture before `board`, because the specification puts `gnss.replay` in
the body. Also rejected: letting a test that never gave its declared source pass, because a
module-wide `source` marker would then pass every test of the module that checks nothing but what
the run left behind, with no firmware run; and only a warning in the link's report, which pytest
shows only with a failure or `-rA`.

## A replay needs the source marker that declares it

Status: decided (#14)

*Decided in #64 to give way to [what is at a route's far end is what the test does with
it](#what-is-at-a-routes-far-end-is-what-the-test-does-with-it-before-power-on-not-a-marker); this
is what the code does until that lands.*

`gnss.replay(path)` is refused without `@pytest.mark.source("uart.gnss", "replay")`. Only a
declared route is on the link, so the board knows at setup which sources to wait for, and the
marker is what skips the test on a backend that cannot replay (`serial`).

**Rejected:** putting every route of the profile on the link and starting the run once the body
gives a source, because the board would not know whether to wait; and taking the source from the
body alone, which [the skip decision](#a-source-or-tap-a-backend-cannot-give-skips-its-parameter-at-collection)
already rejects.

## The esp32sim backend refuses a model or real source at setup until it gives one

Status: decided (#14)

*Decided in #64 to give way to [what is at a route's far end is what the test does with
it](#what-is-at-a-routes-far-end-is-what-the-test-does-with-it-before-power-on-not-a-marker); this
is what the code does until that lands.*

The capability declaration gives `esp32sim` `model`, `replay` and `real` on every UART, which is
what the backend is to give; it gives `replay` only so far. A test that declares another gets
`esp32sim: uart.gnss: a model source is not implemented yet; the backend gives replay` at setup.

**Rejected:** skipping, because the test would then run nowhere without failing; holding the run,
because nothing could give the source and the test would wait for its expect's timeout; and
narrowing the declaration, because it says what the backend is for, and a skip on `esp32sim` would
read as "this backend cannot", which is not so.

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

Status: postponed (#25)

`esp32tl.backends.SERVICES` has the `esp32sim` entry only, so the parametrisation has a single
case. `serial` (`esp,idf`) is added with M4.

## Prebuilt runner binaries are postponed to M10

Status: postponed (#40)

The backend finds the runner in the checkout, so from a wheel only `ESP32TL_RUNNER` and
`ESP32TL_ROM` find the runner and the ROM.

## Replaying requests, looping and wall-clock recordings is postponed

Status: postponed (#20)

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

## esp32tl replay takes one recording, and esp32tl record waits for the tap

Status: postponed (#21)

`esp32tl replay` takes one `--interface` and one `--recording`: no profile has two UART routes a
recording can go into yet. `esp32tl record` comes with `tap(path)` (#21), which writes what it
records.

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
either: it sees the socket close and exits with code 4 (protocol §9). Until it is implemented,
[the run waits before the guest boots until the test has given its declared sources](#the-run-waits-before-the-guest-boots-until-the-test-has-given-its-declared-sources)
and [a run lasts at most 300 emulated seconds](#a-run-lasts-at-most-300-emulated-seconds) describe
the code.

Measured on a spike on 2026-10-31, on the protocol as it is, with no runner change, only
`--console both`: `nmea_reader` and the first consumer's release image ran their tests on the held
clock, 222 emulated seconds of the first consumer in 8 s of wall time, and two runs of the same
test printed the same console line at the same emulated millisecond. `hello_world` and the four
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
`power_on()` gets an error at teardown, as a test that never gave its declared source does now:
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

With the clock held by the test, the runner waits in its turn for as long as the test body runs,
so the backend starts it with `--peer-timeout 0`. What ends a run that hangs, in the harness or in
the emulator, is the test's timeout in wall time, through pytest-timeout, which becomes a
dependency of esp32tl. The plugin gives every test that uses `board` a default of 600 s unless the
test's own configuration sets one: a `timeout` ini option, `--timeout`, the `PYTEST_TIMEOUT`
environment variable or a `timeout` marker. A
configured `timeout = 0` is the user's choice and is kept; their CI's job timeout is then the only
limit. pytest-timeout turns itself off when it detects a debugger such as pdb, unless
`--timeout-disable-debugger-detection` is given. The backend's teardown ends the runner as for
any other failed test; on the spike a test with a 2 s timeout that waited 4:50 of the board's
time failed with `Timeout (>2.0s) from pytest-timeout` and left no runner behind. The limits a
test states, `within=` and `timeout=`, are in the board's time and are part of what it tests; the
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
M5. The `tap` marker is not part of this decision: taps come with #21, and so does the decision on
how a test asks for one.

Until it is implemented, these describe the code:
[a replay needs the source marker that declares it](#a-replay-needs-the-source-marker-that-declares-it),
[a source or tap a backend cannot give skips its parameter at collection](#a-source-or-tap-a-backend-cannot-give-skips-its-parameter-at-collection),
[the closest source marker wins per interface](#the-closest-source-marker-wins-per-interface),
[a request no backend can give is a collection error](#a-request-no-backend-can-give-is-a-collection-error)
and [the esp32sim backend refuses a model or real source at setup until it gives one](#the-esp32sim-backend-refuses-a-model-or-real-source-at-setup-until-it-gives-one).

**Rejected:** keeping the marker, because the test states the source twice, once in a decorator
far from the body; the reason it was needed, that the board had to know at setup whether to wait,
is gone once the run starts at `power_on()`. The cost accepted: on the `serial` backend a test is
skipped in its body rather than before its setup, after pytest-embedded has opened the board's
port, which [the skip at collection](#a-source-or-tap-a-backend-cannot-give-skips-its-parameter-at-collection)
was made to avoid.

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
the model. On the spike the model drove the first consumer's release image to the same power
decision as a generated recording of the same trip, one second earlier because its first epoch
is one second earlier.

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
its own vocabulary in its own tests (which topic is its status, what its report means), the way
the first consumer's spike wraps its status reports in a `Status` value. esp32tl knows protocols
(NMEA, AT, PPP, MQTT) and the ESP-IDF platform (its partition table, NVS), never a product.

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

Measured on the spike: esp-protocols' `pppos_client` (esp_modem 1.4.1, SIM7600 commands, changed
in its sdkconfig only) dialled, got its address, resolved its broker, connected and published in
0.18 emulated seconds, and received what the test published; two runs gave the same transcript.
The first consumer's release image, given its credentials and the test's CA, dialled, asked NTP
and DNS, completed a TLS 1.2 handshake (ECDHE-RSA-AES256-GCM-SHA384), logged in, subscribed to
its four topics and published its status reports, which the test read.

**Rejected:** `pppd` in a Linux container with NAT to the host and the data phase under
`--realtime`, as plan M6 had it, because the far end then lives in wall time: runs do not repeat,
the test cannot hold the clock, Docker becomes a requirement, and what the device sends ends up
in the host's network rather than in front of the test. Also rejected: the TCP/IP stack in the
runner, with smoltcp or a stack like esp32sim's user-mode NAT, because the modem's bytes already
reach the harness and a stack there would only add messages to the protocol; and a bridge to a
broker such as mosquitto as the default, which brings wall time back.

## A bridge from the simulated network to a real service is postponed

Status: postponed (#64)

A consumer may want its real backend at the far end, in a container or on the host, at the cost
of wall time and of repeatable runs, as with the `real` source. Nothing needs it yet: the services
in the harness are enough for every test written so far.

## A TLS stack with a seeded random source is postponed

Status: postponed (#64)

The device's side of a TLS session repeats: esp32sim's random number generator is a xorshift from
a fixed seed and the cycle count (`esp-periph/src/rng.rs`), so the firmware draws the same bytes
in every run. The harness's side does not: Python's `ssl` takes its keys and nonces from the
system. On the spike two runs were identical up to the TLS handshake and differed by 1 to 3
emulated milliseconds after it, with the same messages in the same order. Why the times move was
not measured; the likely cause is the device's arithmetic on the server's fresh keys, which takes
a different time for different values. What would fix it is on the harness's side only: a TLS
server with a seeded random source, or fixed ephemeral keys. Until that proves
worth its cost, the determinism job (#22) is to compare what crossed the link in plain text and
leave out the times after a handshake.

## Flash contents besides the build go into a partition of the build's own table, by name

Status: decided (#64)

`board.nvs({namespace: {key: value}})` writes an NVS partition, named `nvs` unless the call names
another, from the partition table of the build under test, before `board.power_on()`. Strings are
`string`, bytes `binary`, and integers are spelled with their width, `u8()`, `u16()`, `u32()`,
`i32()`, since the firmware reads them by type. The image is made with ESP-IDF's own generator,
`esp-idf-nvs-partition-gen`. Other formats, a FAT or LittleFS image from a directory, come when a
test needs one. Where a firmware keeps its configuration, and what it is, is the consumer's: the
first consumer's spike writes its credentials, broker and CA with this one call.

**Rejected:** helpers that know a product's configuration, because that is product knowledge;
and an offset in the test, because it repeats the partition table and breaks when it changes.

## The LilyGO profile takes its GNSS route, flash and PSRAM from the first consumer's firmware

Status: decided (#64)

The profile gets `[uart.gnss]`, UART2 with TX on GPIO 21 and RX on GPIO 22 at 9600 baud, the
first consumer's defaults for its L76K receiver, `flash_mb = 4` from its `sdkconfig`, and
`psram_mb = 4`; the backend passes `psram_mb` to the runner as `--psram-mb`. The first consumer's
`sdkconfig` turns PSRAM on, and without it its release image runs degraded and logs
`PSRAM not found`. The WROVER-E carries 8 MB of PSRAM, of which the emulator models 4, the most the
runner accepts. Until it is implemented,
[the LilyGO profile leaves out what the specification does not give](#the-lilygo-profile-leaves-out-what-the-specification-does-not-give)
describes the profile.

**Rejected:** waiting for the vendor schematic, as plan finding 12 did, because the first consumer's
firmware is the source the modem route already comes from, and its build fixes these values.
