# esp32sim backend: how it works

The backend is `esp32tl/backends/esp32sim.py`. The `board` fixture of the
[pytest plugin](pytest-plugin-technical.md) constructs it at setup and closes it at teardown. What
the user sees is in [esp32sim-backend-user.md](esp32sim-backend-user.md).

pytest-embedded runs no service for this backend (`SERVICES["esp32sim"]` is `None`), so the dut
is pytest-embedded's base `Dut`. The runner is spawned the way `pytest-embedded-qemu` spawns QEMU:
a `DuplicateStdoutPopen` on the dut's `msg_queue`.

## Data flow

`Esp32Sim.__init__(dut, app_path, msg_queue, meta)`, in order:

1. **`load_app(app_path)`** checks that `<app_path>/build/flasher_args.json` is a file, then builds
   `IdfApp(app_path, build_dir=str(build))`. The build directory is absolute on purpose: given
   `build`, `IdfApp` falls back to a `build` directory in the working directory when the app has
   none. It then refuses a target other than `esp32` (`IdfApp.target`: `IDF_TARGET` in
   `config/sdkconfig.json`; without that file, `extra_esptool_args.chip` in `flasher_args.json`;
   `esp32` when neither names one, so a build with neither passes the check),
   `flash_settings["encrypt"]` (any file marked `"encrypted": "true"`), and any of `app.flash_files`
   that is not a file. The result is `Esp32Sim.app`.
2. **`runner_path()`** and **`rom_path()`** resolve the runner and the ROM ELF
   ([Configuration](#configuration)).
3. **`runner_command(runner, rom, app, MAX_SECONDS)`** builds
   `[runner, "--chip", "esp32", "--rom", rom, "--max-seconds", "300", "--flash-at", "0x1000=<path>", ...]`,
   one `--flash-at {offset:#x}={file_path}` per entry of `app.flash_files`, which `IdfApp` sorts by
   offset; the paths are absolute. The runner's options are in [runner-user.md](runner-user.md).
4. **`RunnerPopen(msg_queue, command, meta)`** is `DuplicateStdoutPopen` with one change: if the
   process cannot start (`OSError`), it closes the log file the parent opened before the fork,
   which nothing else would close. `DuplicateStdoutPopen` writes the runner's stdout and stderr into
   one log file (`popen-<uuid>.log` in the test's log directory), and starts a forwarder process
   that reads that file in a loop and puts what it reads on `msg_queue`. pytest-embedded's listener
   process takes it off the queue, prints it to stdout and appends it to `dut.log`
   (`dut.logfile`), which the dut's `pexpect_proc` reads for `dut.expect`.
5. **`end_stream_at_end_of_run(dut.pexpect_proc)`** wraps `read_nonblocking` (below).
6. A daemon thread runs **`_mark_end_of_run`**: `self.process.wait()`, then appends
   `\nesp32tl: the runner exited with code {code}\n` to the runner's log through a file object of
   its own, opened in append mode. The forwarder sends it on after everything the runner wrote. It
   is the only thread that waits for the runner: `Popen.poll()` from another thread can miss an
   exit while this one is in `waitpid()`.

**End of stream.** `pexpect_proc` reads a file that nothing closes, so it never reads an end of
file: on its own, an expect after the runner's exit would wait out its timeout. The wrapped
`read_nonblocking` keeps the last `EOF_TAIL_BYTES` (2048) bytes it has read and searches them for
`END_OF_RUN`. When it finds
the line, it records the message `the runner exited with code {N}; its last output:\n{tail}` and
returns the data it read; every later call raises `RunnerExited(message)`. `tail` is what came
before the end-of-run line, with colour codes removed (`remove_asci_color_code`). If the window was
cut, the tail starts after its first `\n`, unless nothing but whitespace follows it: a single long
line ending in `\r\n` is kept, cut at its start, rather than dropped.

pexpect turns `RunnerExited` into an `EOF` of its own whose message begins with
`str(RunnerExited)` (`Expecter.eof`), with `RunnerExited` as its `__context__`, and
pytest-embedded's expect wrapper raises its `Not found "..."` `EOF` from that.
`ended_on_runner_exit(error)` walks `__cause__`, else `__context__`, from `error`, stopping at a
cycle, and says whether it meets a `RunnerExited`. The plugin's `pytest_runtest_makereport` uses
it to keep the teardown quiet. pexpect also empties its buffer at EOF, which is why the message
carries the tail: pytest-embedded's "Bytes in current buffer" is empty by then.

**`Esp32Sim.close()`**, from the `board` fixture's teardown:

1. `passing = {0}`. If `self.process.returncode is None`, it sends `SIGTERM` with `send_signal`
   (not `DuplicateStdoutPopen.terminate()`, which closes the log before the signal) and adds
   `-SIGTERM`.
2. Joins the waiter thread for 5 s. If it is still alive, `kill()` (`SIGKILL`), adds `-SIGKILL`,
   and joins again without a timeout.
3. `self.process.close()`: stops the forwarder and closes the log. The forwarder is not waited
   for, so the runner's last lines may be missing from `dut.log`; the result does not depend on
   them.
4. Raises `BackendError` if `returncode` is not in `passing`. The runner handles no signal, so a
   negative code is a signal's, and passes only when this method sent it. The code is judged after
   the runner has stopped: a runner that crashed just before the signal keeps its own code.

## Schema

The end-of-run line the backend appends to the runner's log, and the pattern that finds it:

```python
END_OF_RUN = re.compile(rb"\nesp32tl: the runner exited with code (-?\d+)\n")
```

The code is `Popen.returncode`: the exit status, or minus the signal number.

From the build, through `IdfApp`: `flasher_args.json` (`flash_files`, offset to path relative to
`build/`; the `encrypted` field of the entries that name a file; `extra_esptool_args.chip` when
there is no `sdkconfig.json`), `config/sdkconfig.json` (`IDF_TARGET`).
[validation-firmware-technical.md](validation-firmware-technical.md) describes the builds this
repository makes.

## Interfaces

| Name | What |
|---|---|
| `Esp32Sim(dut, app_path, msg_queue, meta)` | Starts the run. Attributes: `name = "esp32sim"`, `dut`, `app` (the `IdfApp`), `process` (the `RunnerPopen`) |
| `Esp32Sim.close()` | Stops the runner; raises `BackendError` for a failing exit code |
| `BackendError` | What keeps the backend from starting, or a failing exit |
| `runner_path()`, `rom_path()` | The runner and the ROM ELF, or `BackendError` |
| `load_app(app_path)` | The checked `IdfApp` of `<app_path>/build` |
| `runner_command(runner, rom, app, max_seconds)` | The runner's argument list |
| `RunnerPopen(msg_queue, cmd, meta)` | `DuplicateStdoutPopen` that closes its log when the start fails |
| `end_stream_at_end_of_run(proc)` | Wraps `proc.read_nonblocking` to raise `RunnerExited` after the end-of-run line |
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

The backend always passes `--rom`: the runner's own lookup prefers the newest release under
`~/.espressif` instead of the pinned one. `MAX_SECONDS` also bounds a runner left behind by a
harness that died; the comment on it records that hello_world runs 12 emulated seconds in 0.3 s of
wall time on an M2 Pro, and that an image that never idles runs slower.

## Failure modes

`BackendError` messages, prefixed `esp32sim: ` by the `board` fixture. All but the last are raised
at setup, in the order below, so only the first problem shows:

| Message | Raised by | Cause |
|---|---|---|
| `{build} holds no ESP-IDF build: no flasher_args.json` | `load_app` | No `build/flasher_args.json` in the app |
| `{build} is built for {app.target}; esp32sim runs the classic esp32` | `load_app` | Target other than `esp32` |
| `{build} asks for flash encryption, which the runner does not do` | `load_app` | A flash file marked encrypted |
| `{build} is not a finished build, missing {', '.join(missing)}` | `load_app` | A file `flasher_args.json` lists does not exist |
| `no runner at {path}: run make setup, or set ESP32TL_RUNNER` | `runner_path` | Not a file, or not executable |
| `no ROM ELF at {path}: run make setup, or set ESP32TL_ROM` | `rom_path` | Not a file |
| `cannot start {command[0]}: {error.strerror}` | `Esp32Sim.__init__` | `OSError` from the start, for example `Exec format error` |
| `the runner exited with code {code}; its output is in {self.dut.logfile}` | `Esp32Sim.close` | Exit code not in `passing`; error at teardown, unless an expect already reported the exit |

The `EOF` an expect raises after the end of the run has the message
`the runner exited with code {N}; its last output:` followed by the tail. Anything else `IdfApp`
raises, a `flasher_args.json` that is not JSON or has no `flash_files` for instance, is not a
`BackendError` and fails the setup with its own traceback.

## Tests

`tests/test_pytest_plugin.py`:

- `test_runner_command_flashes_every_file_at_its_offset`: the exact command line for a fake build
  (`fake_build`).
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
  in 100-byte chunks.
- `test_ended_on_runner_exit_stops_at_a_cycle`.
- `test_a_runner_that_cannot_start_leaves_no_log_open`: no `ResourceWarning` after an
  `Exec format error`.
- `test_hello_world_fails_on_an_image_that_prints_something_else`: the real runner on a copy of
  `firmware/hello_world/build` whose app image prints `Hello WORLD!`, so the test can fail. It
  fails when `make firmware` has not run.

`tests/test_hello_world.py` runs the real image on the real runner and ROM.

Not covered: the `SIGTERM` path of `close` on a runner still running is exercised by every passing
real run but not asserted on its own; the `SIGKILL` after 5 s is not tested; the
`cannot start` message is not checked through the fixture, only `RunnerPopen`'s `OSError`.

## See also

- [esp32sim backend](esp32sim-backend-user.md)
- [pytest plugin: how it works](pytest-plugin-technical.md)
- [Runner: how it works](runner-technical.md)
- [Plan section 3, architecture](../implementation-plan.md#3-architecture) and
  [M0](../implementation-plan.md#m0-bootstrap-console-only)
- [Link protocol](../protocol.md): specified, not implemented; the backend speaks no protocol to
  the runner, it reads its output only
