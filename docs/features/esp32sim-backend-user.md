# esp32sim backend

The `esp32sim` backend runs the firmware of a test in `esp32tl-runner`, the program in `runner/`
that runs the [esp32sim](https://github.com/mikroverk/esp32sim) emulator, on the emulated clock.
It is the backend behind the `board` and `console` fixtures, and the only one: every test that uses
them runs on it, with `[esp32sim]` in its id ([pytest plugin](pytest-plugin-user.md)). It runs an
unmodified ESP-IDF build for the classic ESP32.

## What it needs

- **The runner.** By default `runner/target/release/esp32tl-runner`, which `make setup` (or
  `make build-runner`) builds. `ESP32TL_RUNNER=PATH` names another, for example the
  `RUNNER_PROFILE=dev` build in `runner/target/debug/esp32tl-runner`. It must be an executable
  file.
- **The ROM ELF.** By default `.cache/esp-rom-elfs/20241011/esp32_rev300_rom.elf`, which
  `make setup` (or `make rom-elf`) puts there. `ESP32TL_ROM=PATH` names another. The backend
  always hands this file to the runner, so the runner's own ROM lookup is not used
  ([where the runner looks for it](runner-user.md#where-the-rom-comes-from)).
- **An ESP-IDF build** in the `build/` directory of the test's app (the `app_path` marker): built
  for the `esp32` target, finished, every file `flasher_args.json` lists present, and without flash
  encryption.

The two defaults are paths in the checkout esp32tl is installed from, which holds only with an
editable install; `make setup` makes one. Installed any other way, from a wheel for instance,
esp32tl finds the runner and the ROM ELF only through `ESP32TL_RUNNER` and `ESP32TL_ROM`.
A relative path in either resolves against pytest's working directory, not the test file's.

## How a run goes

Each test that uses `board` starts its own runner when the fixture is set up, with every file of
`flasher_args.json` at its offset, and stops it at teardown. The firmware's UART0 and the runner's
own lines (`esp32tl-runner: ...`) go into one stream: the console the test's expects read.
pytest-embedded writes it to the test's `dut.log`, in a directory under the system's temporary
directory unless `--root-logdir` names another, and prints it with a timestamp on each line, which
pytest shows as the test's captured stdout when the test fails.

A run stops after 300 emulated seconds if the test has not ended it before. That is time on the
emulated clock, not the wall clock: a test that waits for anything later fails. The runner exits
with code 0 when it reaches that limit, its normal end.

## When the runner exits

Once the runner has exited, the console ends with the line

```
esp32tl: the runner exited with code N
```

where `N` is the exit code, negative for a signal: `-9` when the runner was killed by `SIGKILL`.

An expect that has not matched by then fails at once with pexpect's `EOF`, instead of waiting out
its timeout; so does every expect after it, unless it matches the end-of-run line itself. The `EOF`
names the exit code and carries the runner's last output, up to 2048 bytes of it, from the first
whole line, without colour codes; part of it may be output that earlier expects already read:

```
E   pexpect.exceptions.EOF: the runner exited with code 0; its last output:
E   Goodbye
...
E   pexpect.exceptions.EOF: Not found "Hello world!"
E   Bytes in current buffer (color code eliminated):
```

pytest-embedded's "Bytes in current buffer" is empty there, because pexpect empties its buffer at
EOF: the runner's output is in the first `EOF` instead.

To wait for the end of the run on purpose, `console.expect(pexpect.EOF)`. Its timeout is wall
time, 30 s by default, so an image that runs slower than real time may need a larger `timeout=` to
reach the 300 emulated seconds.

## When the runner exits with an error

At teardown the backend stops the runner if it is still running, with `SIGTERM`, then `SIGKILL`
if it has not stopped after 5 s. The run passes when the runner exited with code 0, or because of
the signal the backend sent it. Any other code is an error at teardown, even when every expect of
the test passed (pytest reports `1 passed, 1 error`):

```
esp32sim: the runner exited with code {code}; its output is in {logfile}
```

`{logfile}` is the path of the test's `dut.log`.

The teardown says nothing when the test has already failed on an expect that read the runner's
exit: that `EOF` names the code already. A test that failed for any other reason, an `EOF` raised
by something else included, still gets the teardown error.

The runner's exit codes are in [how a run ends](runner-user.md#how-a-run-ends).

## When the backend cannot start

The test gets an error at setup and its body does not run. Every message starts with `esp32sim: `:

| Message | What to do |
|---|---|
| `{build} holds no ESP-IDF build: no flasher_args.json` | Build the app (`make firmware` for this repository's firmware), or fix the `app_path` marker |
| `{build} is built for {target}; esp32sim runs the classic esp32` | Build for `esp32` |
| `{build} asks for flash encryption, which the runner does not do` | Build without flash encryption |
| `{build} is not a finished build, missing {files}` | Finish the build; `{files}` is a comma-separated list |
| `no runner at {path}: run make setup, or set ESP32TL_RUNNER` | Build the runner, or point `ESP32TL_RUNNER` at an executable one |
| `no ROM ELF at {path}: run make setup, or set ESP32TL_ROM` | Fetch the ROM ELF, or point `ESP32TL_ROM` at it |
| `cannot start {runner}: {reason}` | The runner exists but the system cannot run it, for example `Exec format error` |

`{build}` is the app's `build/` directory. The build is checked first, then the runner, then the
ROM ELF, so only the first problem is reported.

## See also

- [esp32sim backend: how it works](esp32sim-backend-technical.md)
- [pytest plugin](pytest-plugin-user.md)
- [Runner](runner-user.md)
- [Development setup](../guides/development.md)
