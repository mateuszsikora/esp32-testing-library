# esp32sim backend: how it works

The backend is `esp32tl/backends/esp32sim.py`. The `board` fixture of the
[pytest plugin](pytest-plugin-technical.md) constructs it at setup and closes it at teardown. What
the user sees is in [esp32sim-backend-user.md](esp32sim-backend-user.md).

pytest-embedded runs no service for this backend (`SERVICES["esp32sim"]` is `None`), so the dut
is pytest-embedded's base `Dut`. The runner is spawned the way `pytest-embedded-qemu` spawns QEMU:
a `DuplicateStdoutPopen` on the dut's `msg_queue`.

## Data flow

`Esp32Sim.__init__(dut, app_path, msg_queue, meta, profile, sources=None, directory=None)` keeps
`profile`, the test's [board profile](board-profiles-technical.md), as `Esp32Sim.profile`, and
`directory`, the test file's (the working directory when `None`), as `Esp32Sim.directory`, then,
in order:

0. **The UARTs.** For each `interface: source` of `sources`, what the test's `source` markers
   declare, it refuses a source not in `IMPLEMENTED_SOURCES`, `("replay",)`, and makes the route's
   `Uart` (`esp32tl/interfaces/uart.py`): `Wire(tx, rx, baud)` from the profile's
   `[uart.<role>]`, `declared=source`, `clock=Esp32Sim.clock`, `directory`, and
   `on_source=self._source_given`. An interface that is not a `uart.*` route of the profile is a
   `BackendError`. The names of these UARTs are the run's `_waiting`.
1. **`load_app(app_path)`** checks that `<app_path>/build/flasher_args.json` is a file, then builds
   `IdfApp(app_path, build_dir=str(build))`. The build directory is absolute on purpose: given
   `build`, `IdfApp` falls back to a `build` directory in the working directory when the app has
   none. It then refuses a target other than `esp32` (`IdfApp.target`: `IDF_TARGET` in
   `config/sdkconfig.json`; without that file, `extra_esptool_args.chip` in `flasher_args.json`;
   `esp32` when neither names one, so a build with neither passes the check),
   `flash_settings["encrypt"]` (any file marked `"encrypted": "true"`), and any of `app.flash_files`
   that is not a file. The result is `Esp32Sim.app`.
2. **`BoardLink(profile.name, uarts, chip=profile.chip, on_error=self._end_run, hold=bool(_waiting), clock=self.clock)`**
   (`esp32tl/link/boardlink.py`) is the harness's end of the link: `Esp32Sim.link`. The harness's
   `hello` names the profile and its chip; the profile's routes reach the link only as the UARTs
   of step 0. It checks that no two interfaces share a pin; `watch` is their pins, none for a test
   without a `source` marker. With `hold`, the link takes its first turn only on `link.start()`
   (below).
3. **`runner_path()`** and **`rom_path()`** resolve the runner and the ROM ELF
   ([Configuration](#configuration)).
4. **`listen()`** binds a Unix stream socket at `link.sock` in a new directory from
   `tempfile.mkdtemp(prefix="esp32tl-")`, or from `mkdtemp(..., dir="/tmp")` when the first path
   is `SUN_PATH_MAX` (104) bytes or longer, and listens with a backlog of 1. It binds before the
   runner starts, because the runner connects once (protocol §1).
5. **`runner_command(runner, rom, app, MAX_SECONDS, link)`** builds
   `[runner, "--chip", "esp32", "--rom", rom, "--max-seconds", "300", "--board", "socket:<link>", "--flash-at", "0x1000=<path>", ...]`,
   one `--flash-at {offset:#x}={file_path}` per entry of `app.flash_files`, which `IdfApp` sorts by
   offset; the paths are absolute. The runner's options are in [runner-user.md](runner-user.md).
6. **`RunnerPopen(msg_queue, command, meta)`** is `DuplicateStdoutPopen` with one change: if the
   process cannot start (`OSError`), it closes the log file the parent opened before the fork,
   which nothing else would close. `DuplicateStdoutPopen` writes the runner's stdout and stderr into
   one log file (`popen-<uuid>.log` in the test's log directory), and starts a forwarder process
   that reads that file in a loop and puts what it reads on `msg_queue`. pytest-embedded's listener
   process takes it off the queue, prints it to stdout and appends it to `dut.log`
   (`dut.logfile`), which the dut's `pexpect_proc` reads for `dut.expect`. If the start fails, the
   socket and its directory are removed before the `BackendError`.
7. **`end_stream_at_end_of_run(dut.pexpect_proc, lambda: self.link.error)`** wraps
   `read_nonblocking` (below).
8. A daemon thread runs **`_serve_link`**: `accept()` with a 0.05 s timeout, in a loop that reads
   `_exited` before each attempt and gives up after a timeout that began with the runner already
   gone, so a runner that connected and then exited is still accepted, and one that exits without
   connecting (a usage error) ends the loop. Then `BoardLink.run(conn)` reads the link to end of
   file and writes the harness's turns.
9. A daemon thread runs **`_mark_end_of_run`**: `self.process.wait()`, sets `_exited`, joins the
   link thread for up to `EXIT_SECONDS`, then appends to the runner's log, through a file object
   of its own opened in append mode, `\nesp32tl: the link failed: {link.error}\n` when the link
   has an error, and `\nesp32tl: the runner exited with code {code}\n`. The forwarder sends them on
   after everything the runner wrote. It is the only thread that waits for the runner:
   `Popen.poll()` from another thread can miss an exit while this one is in `waitpid()`.

**The link.** `BoardLink` keeps the run's `RunnerHello` (`link.runner`), its warnings, its `stats`,
the number of turns and the first `error` with its traceback. When it finds the run wrong it calls
`on_error`, once, from the link thread; `Esp32Sim._end_run` then calls `_terminate()`, which sends
`SIGTERM` unless the runner has exited or has had one already, under a lock, because a second
`SIGTERM` kills the runner with 128 + 15. A mismatched handshake does not call `on_error`: the
runner finds the mismatch in the harness's `hello` and exits 3 by itself. What `BoardLink` checks
is in [the link protocol](../protocol.md) and in the docstrings of `esp32tl/link/`.

**The held start.** With a declared source, the link holds the runner in its first turn, at
`t = 0` before the guest's first instruction ([protocol §4](../protocol.md#4-session)), so
emulated time does not move until the test body gives the source. `Uart.replay` calls
`on_source(name)`; `_source_given` removes the name from `_waiting` under a lock and calls
`link.start()` once none is left. `waiting()` returns `{interface: declared source}` for the
names left, sorted, and `{}` once the link is not held: the `console` fixture's expects read it
([pytest plugin](pytest-plugin-technical.md#data-flow)). `uart(role)` returns the `Uart` of
`uart.<role>` from step 0, or a new one with `declared=None`, on no link, for a route the test
declared nothing on. `close` calls `link.start(boot=False)` first, and `_mark_end_of_run` once
the runner has exited. `link.start(boot=False)` does nothing, and returns `False`, once the link
has started; otherwise a run still held at teardown, or whose runner is gone, gets `stop` at its
first turn instead of a boot, and the link thread is not left waiting. Before that call, `close`
keeps in `never_started` what `_waiting` still holds, `{interface: declared source}`, empty when
every source was given; the `board` fixture reports it
([pytest plugin](pytest-plugin-technical.md#data-flow)), not `close`, which raises no error for it.

**The clock.** `Esp32Sim.clock` is `recording.EMULATED`, `"emulated-fifo-write"`, and goes to
`BoardLink` as `clock`: a runner whose `hello` names another clock fails the link
(`the runner's clock is {clock}, the backend's emulated-fifo-write`), since the UARTs check their
recordings against `Esp32Sim.clock`.

**End of stream.** `pexpect_proc` reads a file that nothing closes, so it never reads an end of
file: on its own, an expect after the runner's exit would wait out its timeout. The wrapped
`read_nonblocking` keeps the last `EOF_TAIL_BYTES` (2048) bytes it has read and searches them for
`END_OF_RUN`. When it finds
the line, it records the message `the runner exited with code {N}; its last output:\n{tail}`, prefixed with
`the link failed: {error}; ` when `link_error()` returns one, and
returns the data it read; every later call raises `RunnerExited(message)`. `tail` is what came
before the end-of-run line, with colour codes removed (`remove_asci_color_code`). If the window was
cut, the tail starts after its first `\n`, unless nothing but whitespace follows it: a single long
line ending in `\r\n` is kept, cut at its start, rather than dropped. The link's error comes from
`link_error()`, not from the line in the log, which the guest's output could cut or imitate; the
end-of-run line is written after the link thread is joined, so the error is final by then unless
something else holds the link open for longer than `EXIT_SECONDS`.

pexpect turns `RunnerExited` into an `EOF` of its own whose message begins with
`str(RunnerExited)` (`Expecter.eof`), with `RunnerExited` as its `__context__`, and
pytest-embedded's expect wrapper raises its `Not found "..."` `EOF` from that.
`ended_on_runner_exit(error)` walks `__cause__`, else `__context__`, from `error`, stopping at a
cycle, and says whether it meets a `RunnerExited`. The plugin's `pytest_runtest_makereport` uses
it to keep the teardown quiet. pexpect also empties its buffer at EOF, which is why the message
carries the tail: pytest-embedded's "Bytes in current buffer" is empty by then.

**`Esp32Sim.close()`**, from the `board` fixture's teardown:

0. `link.start(boot=False)`: a run still held for a source the test did not give gets `stop` at
   its first turn, before the guest boots. When this call is the one that released the link, it
   then waits up to `EXIT_SECONDS` for the runner to exit, which it does with code 0, so that the
   `SIGTERM` of step 2 does not race the `stop` for the run's end; a runner that is gone by then
   gets no signal.
1. Waits, for up to `EXIT_SECONDS`, until `link.runner` is set (the runner's `hello` has arrived)
   or the runner has exited. The runner registers its `SIGTERM` handler after it connects and
   before its `hello`; a signal before that kills it, so a test that ends while the runner loads
   the images would fail on `-15`.
2. `_terminate()`: `SIGTERM` with `send_signal`, not `DuplicateStdoutPopen.terminate()`, which
   closes the log before the signal; nothing if `_end_run` has sent it already.
3. Joins the link thread, then waits for `_exited`, both within `EXIT_SECONDS` of the signal. If
   the runner has not exited, `kill()` (`SIGKILL`) and waits for `_exited` without a timeout. It
   waits on `_exited` rather than on the waiter thread, which also waits for the link.
4. `_close_link()`: joins the link thread for up to `EXIT_SECONDS`; if it still reads (a process
   other than the runner holds the socket), `shutdown(SHUT_RDWR)` ends the read and the thread is
   joined. Then closes the connection and the listener and removes the socket's directory. Then
   joins the waiter thread.
5. `self.process.close()`: stops the forwarder and closes the log. The forwarder is not waited
   for, so the runner's last lines may be missing from `dut.log`; the result does not depend on
   them. Then closes the stdin pipe `DuplicateStdoutPopen` opens and never closes, which the runner
   does not read.
6. Raises one `BackendError` naming every problem, joined by `; `: the runner was killed, or its
   exit code is not 0, and the link has an error. The code is judged after the runner has
   stopped: a runner that crashed just before the signal keeps its own code. With `--board` the
   runner exits 0 on `SIGTERM`, so a signal's code never passes.

## Schema

The end-of-run line the backend appends to the runner's log, and the pattern that finds it:

```python
END_OF_RUN = re.compile(rb"\nesp32tl: the runner exited with code (-?\d+)\n")
```

The code is `Popen.returncode`: the exit status, or minus the signal number. Before it, when the
link has an error:

```
\nesp32tl: the link failed: {error}\n
```

The link's messages are those of [the link protocol](../protocol.md), version 1, encoded and
decoded by `esp32tl/link/protocol.py`.

From the build, through `IdfApp`: `flasher_args.json` (`flash_files`, offset to path relative to
`build/`; the `encrypted` field of the entries that name a file; `extra_esptool_args.chip` when
there is no `sdkconfig.json`), `config/sdkconfig.json` (`IDF_TARGET`).
[validation-firmware-technical.md](validation-firmware-technical.md) describes the builds this
repository makes.

## Interfaces

| Name | What |
|---|---|
| `Esp32Sim(dut, app_path, msg_queue, meta, profile, sources=None, directory=None)` | Starts the run. Attributes: `name = "esp32sim"`, `clock = "emulated-fifo-write"` (the clock the runner's `hello` must name, and a replayed recording must be on), `never_started` (after `close`, what a run held to the end waited for), `dut`, `profile` (the `BoardProfile`), `directory`, `app` (the `IdfApp`), `process` (the `RunnerPopen`), `link` (the `BoardLink`, complete once `close` returns) |
| `Esp32Sim.uart(role)` | The `Uart` of `uart.<role>`; `BackendError` when the profile has no such route |
| `Esp32Sim.waiting()` | The interfaces whose declared source the run waits for, with the source; `{}` once started |
| `Esp32Sim.close()` | Ends the run; raises `BackendError` for a failing exit code, a killed runner or a link error |
| `BackendError` | What keeps the backend from starting, or a failing end |
| `listen()` | The listening socket and its path, or `BackendError` |
| `runner_path()`, `rom_path()` | The runner and the ROM ELF, or `BackendError` |
| `load_app(app_path)` | The checked `IdfApp` of `<app_path>/build` |
| `runner_command(runner, rom, app, max_seconds, link)` | The runner's argument list |
| `RunnerPopen(msg_queue, cmd, meta)` | `DuplicateStdoutPopen` that closes its log when the start fails |
| `end_stream_at_end_of_run(proc, link_error=lambda: None)` | Wraps `proc.read_nonblocking` to raise `RunnerExited` after the end-of-run line, with `link_error()` first in its message |
| `RunnerExited` | Subclass of `pexpect.EOF` |
| `ended_on_runner_exit(error)` | Whether `error`'s chain holds a `RunnerExited` |

## Configuration

| Name | Value | Used by |
|---|---|---|
| `ESP32TL_RUNNER` | Path of the runner; unset or empty: `ROOT / "runner/target/release/esp32tl-runner"` | `runner_path()`; must be a file with execute permission |
| `ESP32TL_ROM` | Path of the ROM ELF; unset or empty: `ROOT / ".cache/esp-rom-elfs" / ROM_RELEASE / ROM_ELF` | `rom_path()`; must be a file |
| `ROOT` | `Path(__file__).resolve().parents[2]`: the checkout with an editable install, `site-packages` when installed from a wheel | the two defaults |
| `ROM_RELEASE` | `"20241011"`, the esp-rom-elfs release `scripts/fetch-rom-elf` pins | `rom_path()` |
| `ROM_ELF` | `"esp32_rev300_rom.elf"` | `rom_path()` |
| `MAX_SECONDS` | `300`, emulated seconds, passed as `--max-seconds` | `Esp32Sim.__init__` |
| `EOF_TAIL_BYTES` | `2048` | `end_stream_at_end_of_run` |
| `EXIT_SECONDS` | `5`, wall seconds: for the runner's `hello` at teardown, then for its exit after `SIGTERM`, and for each join of the link thread | `close`, `_mark_end_of_run`, `_close_link` |
| `SUN_PATH_MAX` | `104`, the longest Unix socket path on macOS with its NUL; Linux takes 108 | `listen` |
| `IMPLEMENTED_SOURCES` | `("replay",)`: the sources the backend gives, of those `capabilities.toml` declares for it | `Esp32Sim.__init__` |

The backend always passes `--rom`: the runner's own lookup prefers the newest release under
`~/.espressif` instead of the pinned one. `MAX_SECONDS` also bounds a runner left behind by a
harness that died; the comment on it records that hello_world runs 12 emulated seconds in 0.3 s of
wall time on an M2 Pro, and that an image that never idles runs slower.

## Failure modes

`BackendError` messages, prefixed `esp32sim: ` by the `board` fixture. All but the last are raised
at setup, in the order below, so only the first problem shows:

| Message | Raised by | Cause |
|---|---|---|
| `{interface}: a {source} source is not implemented yet; the backend gives replay` | `Esp32Sim.__init__` | A `source` marker names `model` or `real` |
| `the board profile {name} has no {interface}; its interfaces are {interfaces}` | `Esp32Sim.__init__`, `Esp32Sim.uart` | A source, or the `gnss` fixture, on a route the profile does not have; `{interfaces}` is `BoardProfile.interfaces`, comma-separated |
| `{build} holds no ESP-IDF build: no flasher_args.json` | `load_app` | No `build/flasher_args.json` in the app |
| `{build} is built for {app.target}; esp32sim runs the classic esp32` | `load_app` | Target other than `esp32` |
| `{build} asks for flash encryption, which the runner does not do` | `load_app` | A flash file marked encrypted |
| `{build} is not a finished build, missing {', '.join(missing)}` | `load_app` | A file `flasher_args.json` lists does not exist |
| `no runner at {path}: run make setup, or set ESP32TL_RUNNER` | `runner_path` | Not a file, or not executable |
| `no ROM ELF at {path}: run make setup, or set ESP32TL_ROM` | `rom_path` | Not a file |
| `cannot listen on {path}: {error.strerror}` | `listen` | The socket cannot be bound |
| `cannot start {command[0]}: {error.strerror}` | `Esp32Sim.__init__` | `OSError` from the start, for example `Exec format error` |
| `the runner exited with code {code}; its output is in {self.dut.logfile}` | `Esp32Sim.close` | Exit code not 0; error at teardown, unless an expect already reported the exit |
| `the runner did not exit within {EXIT_SECONDS} s of SIGTERM, and was killed; its output is in {self.dut.logfile}` | `Esp32Sim.close` | No exit within `EXIT_SECONDS` of the signal |
| `the link failed: {link.error}; its output is in {self.dut.logfile}` | `Esp32Sim.close` | `BoardLink` found the run wrong; after an exit-code problem when there is one |

The `EOF` an expect raises after the end of the run has the message
`the runner exited with code {N}; its last output:` followed by the tail, with
`the link failed: {error}; ` before it when the link has an error. Anything else `IdfApp`
raises, a `flasher_args.json` that is not JSON or has no `flash_files` for instance, is not a
`BackendError` and fails the setup with its own traceback.

## Tests

`tests/test_pytest_plugin.py`:

- `test_runner_command_flashes_every_file_at_its_offset`: the exact command line for a fake build
  (`fake_build`), `--board socket:PATH` included.
- `test_load_app_refuses_what_the_runner_cannot_run`: the four `load_app` messages.
- `test_runner_and_rom_paths_name_what_is_missing`: both messages, a runner that is not
  executable, and the environment variables taking effect.
- `test_rom_is_the_one_make_setup_fetches`: `ROM_RELEASE` and `ROM_ELF` equal `RELEASE=` and
  `ROM=` in `scripts/fetch-rom-elf`.
- With a shell script as the runner (`FAKE_RUNNER`, through `ESP32TL_RUNNER`):
  `test_expect_fails_at_once_when_the_runner_has_exited` (under 15 s, against the 30 s timeout,
  with the tail in the `EOF`), `test_a_runner_that_fails_to_start_is_reported_once` (exit 2),
  `test_a_runner_that_exits_with_an_error_fails_the_test` (exit 1 and `SIGKILL`, `-9`: the call
  passes, the teardown errors), `test_an_eof_from_elsewhere_does_not_hide_the_runner_s_exit`.
- `test_the_eof_carries_the_runner_s_last_whole_lines_without_colour`,
  `test_the_eof_keeps_a_long_last_line` (no ending, `\n`, `\r\n`): the tail, on a stub stream read
  in 100-byte chunks. `test_the_eof_names_the_link_s_error_from_the_link_not_from_the_log`: the
  reason from `link_error`, with a guest line that imitates the backend's and the backend's own
  with its leading newline cut off.
- `test_ended_on_runner_exit_stops_at_a_cycle`.
- `test_a_runner_that_cannot_start_leaves_no_log_open`: no `ResourceWarning` after an
  `Exec format error`.
- `test_hello_world_fails_on_an_image_that_prints_something_else`: the real runner on a copy of
  `firmware/hello_world/build` whose app image prints `Hello WORLD!`, so the test can fail. It
  fails when `make firmware` has not run.
- With a Python script that speaks the link protocol as the runner (`LINK_RUNNER`, through
  `link_runner`; `$FAKE_LINK` picks how it misbehaves):
  `test_the_backend_serves_the_link_and_ends_the_run_with_sigterm` (the report section and the
  JSON user property in `--junitxml`), `test_a_mismatched_handshake_fails_the_test_with_both_sides_reasons`
  (exit 3, both reasons in the `EOF`), `test_a_link_the_harness_finds_wrong_fails_the_test_after_its_expects`,
  `test_a_runner_that_ignores_sigterm_is_killed_and_fails_the_test` (`EXIT_SECONDS` set to 0.5),
  `test_a_link_error_ends_the_run_at_once_for_a_waiting_expect` (under 15 s),
  `test_a_link_left_open_after_the_runner_exits_is_shut_down` (a child holds the socket; under
  3 s) and `test_a_test_that_ends_while_the_runner_loads_waits_for_its_hello` (it connects after
  0.5 s and dies of `SIGTERM` until then).

`tests/test_hello_world.py` runs the real image on the real runner and ROM.
`tests/test_gnss_fixture.py` runs `nmea_reader` with a declared replay, given and not, and the
two setup errors above ([replay-technical.md](replay-technical.md#tests)).

`tests/test_boardlink.py` and `tests/test_link_protocol.py` test `BoardLink` and the codec without
a runner; `tests/test_runner_link.py` runs the real runner against a peer of its own, with the
command line from `runner_command`.

Not covered: that `_end_run` and `close` send `SIGTERM` once only is not asserted, because the
fake runner exits on the first; `listen()`'s fallback to `/tmp` and its `cannot listen` message
are not tested; the `cannot start` message is not checked through the fixture, only
`RunnerPopen`'s `OSError`.

## See also

- [esp32sim backend](esp32sim-backend-user.md)
- [pytest plugin: how it works](pytest-plugin-technical.md)
- [Runner: how it works](runner-technical.md)
- [Plan section 3, architecture](../implementation-plan.md#3-architecture) and
  [M0](../implementation-plan.md#m0-bootstrap-console-only)
- [Link protocol](../protocol.md): what the backend's `BoardLink` and the runner speak
