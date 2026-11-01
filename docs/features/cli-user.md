# The `esp32tl` command

Installing esp32tl puts an `esp32tl` command on the `PATH` (`[project.scripts]` in
`pyproject.toml`, `esp32tl = "esp32tl.cli:main"`; in this repository, `.venv/bin/esp32tl` after
`make setup`). It runs an app on the emulator outside pytest, with the runner, the ROM and the
build the [esp32sim backend](esp32sim-backend-user.md) uses for a test: `esp32tl run` with nothing
at the far end of the board's interfaces, `esp32tl replay` with a [recording](replay-user.md)
replayed into one of them. Unlike a test's run, which moves only while the test waits on the
board's clock, the command's runs free: there is no test body to wait for, so the run goes as fast
as the emulator does until `--max-seconds`, and the console goes to stdout only. Tests run through pytest ([pytest plugin](pytest-plugin-user.md)).
There is no `python -m esp32tl`, and no `esp32tl record` yet.

## Running an app

```sh
esp32tl run --app firmware/hello_world --max-seconds 2
```

runs the ESP-IDF build in `firmware/hello_world/build` on the emulator, prints the firmware's
console on stdout as it goes, and exits 0 after 2 emulated seconds.

```sh
esp32tl replay --app firmware/nmea_reader --interface gnss \
  --recording recordings/gnss-generated-1.jsonl --max-seconds 3.5
```

does the same with the recording replayed into the board profile's `uart.gnss` route, each event
at its recorded time, as `gnss.replay(path)` does in a test ([replay](replay-user.md)):
`nmea_reader` prints `fix acquired lat=52.229677 lon=21.012233` and the two fixes after it.

The board profile's GPIO lines are on the link in both commands, with nothing driving them but the
board's pulls, as in a test ([the `lines` fixture](pytest-plugin-user.md#the-fixtures)):

```sh
esp32tl run --app firmware/gpio_echo --board lilygo-t-a7670g-r2 --max-seconds 1
```

prints `in=0`, since RI is pulled down on that board, and the report watches GPIO 4 and 33. The
command drives no line otherwise.

| Option | Commands | Default | Accepts |
|---|---|---|---|
| `--app APP` | both | none, required | the ESP-IDF app directory, its build in `APP/build`, relative to the working directory or absolute. The build must be one the backend runs: for the `esp32` target, finished, without flash encryption |
| `--board BOARD` | both | `esp32-devkitc` | the name of a [board profile](board-profiles-user.md#the-profiles-esp32tl-ships); the chip gets the profile's PSRAM, 4 MB on `lilygo-t-a7670g-r2` and none on `esp32-devkitc` |
| `--max-seconds S` | both | `300` | a finite number of emulated seconds above 0; fractions are fine |
| `--interface INTERFACE` | `replay`, required | none | a UART route of the profile: `uart.gnss`, or its role alone, `gnss` |
| `--recording RECORDING` | `replay`, required | none | a recording file, relative to the working directory or absolute |

`esp32tl replay` takes one recording. The runner and the ROM ELF are the backend's, from the
checkout or from `ESP32TL_RUNNER` and `ESP32TL_ROM`
([what the backend needs](esp32sim-backend-user.md#what-it-needs)).

## What it prints

stdout is the firmware's console, UART0, as the runner prints it. stderr has the runner's own
lines (`esp32tl-runner: ...`, [the runner](runner-user.md)) and, once the run has ended, the
[link's report](esp32sim-backend-user.md#the-links-report), each line starting with `esp32tl: `:

```
esp32tl: board esp32-devkitc, watching GPIO 4, GPIO 5
esp32tl: runner: protocol version 1, esp32sim 8c652151dd5a521249a785ca147f20a9f8eda68f, clock emulated-fifo-write; 4 turns taken by the harness
esp32tl: stats: the run ended at 3.500000000 s (max_seconds); 4 turns, 0 resets
esp32tl:   UART1: 0 bytes sent, 208 received, 0 rxfifo_ovf, 0 frm_err
esp32tl:   unrouted_rx_bytes 0, inputs_on_output 0, inputs_not_applied 0, rx_bytes_not_paced 0
```

When the harness found the link wrong, the report's last line is `esp32tl: error: {reason}`, as
for a baud mismatch:

```
esp32tl: error: uart.gnss sends at 115200 baud on GPIO 5, and the firmware's UART1 receives there at 9600 baud: 72 bytes dropped as frame errors (frm_err)
```

## How a run ends

A run ends at `--max-seconds`, or earlier when the harness finds the link wrong: the runner gets
`SIGTERM` at once, and the command exits 1 ([when the run fails](#exit-codes)). A frame error on a
replayed route is such a fault: the firmware configured its UART at another baud than the
profile's, and lost the bytes ([replay](replay-user.md#what-a-replay-does)).

Ctrl-C ends the run: the runner gets one `SIGTERM`, sends its statistics, the report says
`(signal)`, and the command exits 130. Before the runner has connected, while it loads the images,
the `SIGTERM` kills it instead (protocol §9): the report then says `the runner never connected`,
and the command still exits 130. A Ctrl-C before the runner starts keeps it from starting, and
the command exits 130 without a report. A second Ctrl-C kills the runner. The runner does not see
the terminal's Ctrl-C itself: it runs in a session of its own, so that one Ctrl-C never reaches it
as two signals, which would kill it.

## Exit codes

| Code | When | What stderr shows last |
|---|---|---|
| 0 | the run reached `--max-seconds` | the report, `(max_seconds)` |
| 1 | the harness found the link wrong, and the runner exited 0 | `esp32tl: error: {reason}` |
| 2 | the run could not start: see below; or a usage error from argparse | `esp32tl: {message}`, or argparse's usage and error |
| 130 | Ctrl-C | the report, `(signal)`; `the runner never connected` when it came before the runner connected; nothing when it came before the runner started |
| any other | the runner's own exit code, when it is not 0 ([how a run ends](runner-user.md#how-a-run-ends)); 128 + N when a signal N killed it | the runner's last line, then the report |

What keeps a run from starting is checked before the runner starts, in this order, and printed
as `esp32tl: {message}`:

| Message | Cause |
|---|---|
| `no board profile named '{board}'; the profiles are esp32-devkitc, lilygo-t-a7670g-r2` | `--board` names no profile esp32tl ships |
| `the board profile {board} has no {interface}; its interfaces are {interfaces}` | `--interface` names no UART route of the profile |
| `{path}: [Errno 2] No such file or directory: '{path}'`, and every other `RecordingError` | the recording cannot be read, or breaks the [format](../recording-format.md#errors) |
| `{path} is a recording of {recorded}, not of {interface}`, and every other `ReplayError` | the recording cannot be replayed into that interface ([replay](replay-user.md#what-a-replay-refuses)); `{path}` is the path as given |
| `{app}/build holds no ESP-IDF build: no flasher_args.json`, and the backend's other build errors | `--app` holds no build the backend runs ([when the backend cannot start](esp32sim-backend-user.md#when-the-backend-cannot-start)) |
| `no runner at {path}: run make setup, or set ESP32TL_RUNNER` | no runner |
| `no ROM ELF at {path}: run make setup, or set ESP32TL_ROM` | no ROM ELF |
| `cannot listen on {path}: {reason}` | the link's Unix socket could not be bound, in a new directory under the system's temporary directory, or under `/tmp` when that path would be too long |
| `cannot start {runner}: {reason}` | the runner could not be executed |

## The version and the help

```sh
esp32tl --version
```

prints `esp32tl <version>`, the version of the installed package (`esp32tl 0.1.0` now), and exits
0.

`esp32tl` with no arguments, `esp32tl -h` and `esp32tl --help` print the help and exit 0:

```
usage: esp32tl [-h] [--version] COMMAND ...

Test unmodified ESP32 firmware through its wires.

positional arguments:
  COMMAND
    run       run an app on the emulator, its console on stdout
    replay    run an app with a recording replayed into one of its board's
              interfaces

options:
  -h, --help  show this help message and exit
  --version   show program's version number and exit
```

`esp32tl run -h` and `esp32tl replay -h` print each command's options. Any other argument is an
error from argparse, with exit code 2:

```
usage: esp32tl [-h] [--version] COMMAND ...
esp32tl: error: argument COMMAND: invalid choice: 'foo' (choose from 'run', 'replay')
```

## See also

- [pytest plugin](pytest-plugin-user.md)
- [Replay](replay-user.md)
- [esp32sim backend](esp32sim-backend-user.md)
