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

The runner's board is a link to the harness: a Unix socket the backend binds before it starts the
runner, in a new directory under the system's temporary directory (under `/tmp` when that path
would be too long for a socket), passed as `--board socket:PATH`. The backend reads the link in a
thread for the whole run, speaking the [link protocol](../protocol.md). No interface is wired to
the board yet, so the link carries the handshake, one turn at the start, the runner's warnings and
its statistics; the console still comes from the runner's stdout.

A run stops after 300 emulated seconds if the test has not ended it before. That is time on the
emulated clock, not the wall clock: a test that waits for anything later fails. The runner exits
with code 0 when it reaches that limit, its normal end.

## When the runner exits

Once the runner has exited, the console ends with the line

```
esp32tl: the runner exited with code N
```

where `N` is the exit code, negative for a signal: `-9` when the runner was killed by `SIGKILL`.
When the harness found the link wrong, the line before it is

```
esp32tl: the link failed: {reason}
```

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

When the harness found the link wrong, the `EOF` starts with that:
`the link failed: {reason}; the runner exited with code N; its last output:`.

pytest-embedded's "Bytes in current buffer" is empty there, because pexpect empties its buffer at
EOF: the runner's output is in the first `EOF` instead.

To wait for the end of the run on purpose, `console.expect(pexpect.EOF)`. Its timeout is wall
time, 30 s by default, so an image that runs slower than real time may need a larger `timeout=` to
reach the 300 emulated seconds.

## How the run ends

At teardown the backend ends the run with `SIGTERM`, once the runner has connected and said hello:
the runner handles the signal only from then on, and before that it would die of it. A test that
ends while the runner is still loading the images waits up to 5 s for that. On `SIGTERM` the
runner sends its last warnings and its statistics over the link and exits 0. The backend reads the
link to its end and waits for the runner; one that has not exited 5 s after the signal is killed
with `SIGKILL`. It sends `SIGTERM` once only: a second one would kill the runner, with exit code
143.

When the harness finds the link wrong (a message out of protocol, an interface that broke its
promises or raised), it ends the run at once with `SIGTERM`, rather than wait for the runner's next
turn, which may come only after 300 emulated seconds. An expect that is waiting then fails with the
`EOF` above, the reason first. A handshake the two sides disagree on is the exception: the runner
finds the mismatch itself and exits with code 3.

## When the run fails

The run passes when the runner exited with code 0 and the harness found nothing wrong with the
link. Anything else is an error at teardown, even when every expect of the test passed (pytest
reports `1 passed, 1 error`). The message names every problem, separated by `; `:

```
esp32sim: the runner exited with code {code}; its output is in {logfile}
esp32sim: the runner did not exit within 5 s of SIGTERM, and was killed; its output is in {logfile}
esp32sim: the link failed: {reason}; its output is in {logfile}
esp32sim: the runner exited with code 3; the link failed: {reason}; its output is in {logfile}
```

`{logfile}` is the path of the test's `dut.log`.

The teardown says nothing when the test has already failed on an expect that read the runner's
exit: that `EOF` names the code and the link's reason already. A test that failed for any other
reason, an `EOF` raised by something else included, still gets the teardown error.

The runner's exit codes are in [how a run ends](runner-user.md#how-a-run-ends).

## The link's report

Every test that uses `board` gets a report section `esp32sim link`, which pytest prints with a
failure, or for every test with `-rA`:

```
----------------------- Captured esp32sim link teardown ------------------------
board esp32-devkitc, watching no pins
runner: protocol version 1, esp32sim 8c652151dd5a521249a785ca147f20a9f8eda68f, clock emulated-fifo-write; 1 turn taken by the harness
stats: the run ended at 4.250000000 s (signal); 1 turn, 0 resets
  unrouted_rx_bytes 0, inputs_on_output 0, inputs_not_applied 0, rx_bytes_not_paced 0
```

The first line names the test's [board profile](board-profiles-user.md#choosing-a-profile) and
the pins the link watches. It lists the runner's warnings, one line each with its emulated time, the statistics per UART
port, and the harness's error with its traceback when an interface raised. `the runner never
connected` and `no stats: the link ended without them (the runner was killed, or died)` say what
is missing. The same data is in the test's `esp32sim_link` user property, a JSON string with
`board`, `watch`, `warnings`, `stats` and `error`, which `--junitxml` writes as a `<property>`.

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
| `cannot listen on {path}: {reason}` | The backend cannot bind the link's socket in a new directory under the system's temporary directory or `/tmp`; check that it is writable |
| `cannot start {runner}: {reason}` | The runner exists but the system cannot run it, for example `Exec format error` |

`{build}` is the app's `build/` directory. The build is checked first, then the runner, the ROM ELF
and the socket, so only the first problem is reported.

## See also

- [esp32sim backend: how it works](esp32sim-backend-technical.md)
- [pytest plugin](pytest-plugin-user.md)
- [Runner](runner-user.md)
- [Development setup](../guides/development.md)
