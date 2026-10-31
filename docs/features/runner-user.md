# esp32tl-runner

`esp32tl-runner` runs an unmodified image for the classic ESP32 in the esp32sim emulator, on the
emulated clock, and prints the guest's UART0 console on stdout. With `--board`, it connects to a
peer, the harness or any program that speaks [the link protocol](../protocol.md), and wires the
chip's pins to it: the peer sees what the guest sends on its UARTs and sends bytes back into them.
The `esp32sim` backend of the pytest plugin starts it, with `--board socket:PATH` and itself as
the peer, for every test that uses `board` or `console`. This document is for running it by hand, to watch an image boot,
to drive it from a peer of your own, or to see why a run ended the way it did.

## Build it

`make setup` builds it, and `make build-runner` builds it alone
([development guide](../guides/development.md)). The binary is
`runner/target/release/esp32tl-runner`; `make build-runner RUNNER_PROFILE=dev` builds it without
fat LTO, quicker to link, into `runner/target/debug/esp32tl-runner`. The first build needs
network access to GitHub, where cargo fetches esp32sim.

## Run an image

Every file of the image's `flasher_args.json` goes in with `--flash-at`, at its offset. For the
`hello_world` validation image ([validation firmware](validation-firmware-user.md)), with the ROM
ELF that `make setup` fetches:

```sh
B=firmware/hello_world/build
runner/target/release/esp32tl-runner --chip esp32 \
  --rom .cache/esp-rom-elfs/20241011/esp32_rev300_rom.elf \
  --flash-at 0x1000=$B/bootloader/bootloader.bin \
  --flash-at 0x8000=$B/partition_table/partition-table.bin \
  --flash-at 0x10000=$B/hello_world.bin \
  --max-seconds 5
```

`esp32tl-runner --help` (or `-h`) prints the usage on stderr and exits 0:

```text
usage: esp32tl-runner --chip esp32 --flash-at OFFSET=FILE... --max-seconds S [options]

  --chip esp32          the chip; only the classic ESP32 for now
  --flash-at OFFSET=FILE   write FILE into flash at OFFSET (hex, 0x optional); repeatable
  --max-seconds S       stop after S emulated seconds: the normal end of a run
  --rom F               the mask ROM ELF; default, as esp32sim's: $ESP32SIM_ROM_DIR,
                        then ~/.espressif/tools/esp-rom-elfs/*/ (newest release first,
                        not the pinned 20241011: whoever runs the tests passes --rom)
  --flash-mb N          flash size in MiB: 1, 2, 4, 8 or 16 (default 4)
  --psram-mb N          PSRAM size in MiB, 0 or 4 (default 0)
  --board socket:PATH   connect to the peer on the Unix socket PATH (docs/protocol.md);
  --board tcp:HOST:PORT   or over TCP. Without --board the chip has nothing on its pins
  --peer-timeout S      how long, in wall seconds, a turn of the peer may take before the
                        run fails with exit code 4 (default 60; 0 waits for ever)
  --console stdout|socket|both   where UART0 goes (default stdout); socket and both need
                        --board
```

| Flag | Default | Accepts |
|---|---|---|
| `--chip` | none, required | `esp32`, in any case |
| `--flash-at OFFSET=FILE` | none, at least one required | `OFFSET` in hex, with or without a lowercase `0x`; `FILE` is everything after the first `=`. Repeatable; files are written in the order given |
| `--max-seconds S` | none, required | a finite number above 0; fractions are fine |
| `--rom F` | the lookup below | a path to the mask ROM ELF |
| `--flash-mb N` | `4` | `1`, `2`, `4`, `8` or `16` |
| `--psram-mb N` | `0` | `0` or `4` |
| `--board` | none: nothing on the chip's pins | `socket:PATH`, a Unix socket path that is not empty, or `tcp:HOST:PORT`, with a host that is not empty and a port from 0 to 65535 (`tcp:[::1]:7000` for IPv6) |
| `--peer-timeout S` | `60` | `0`, to wait for ever, or a positive number of seconds; fractions are fine. A value that rounds to 0 ns, or is too large for a Rust `Duration` (`1e20`), is a usage error |
| `--console` | `stdout` | `stdout`; `socket` and `both` only with `--board` |

A flag given twice keeps its last value, except `--flash-at`, which adds a file each time. Flags
take their value as the next argument: `--flash-mb=8` is an unknown argument.

### Where the ROM comes from

Without `--rom`, the runner looks for `esp32_rev300_rom.elf` in `$ESP32SIM_ROM_DIR`, then in
`~/.espressif/tools/esp-rom-elfs/*/`, the last directory in name order first, which for the dated
release directories is the newest. That is esp32sim's own lookup. It does not prefer release
`20241011`, the one the project pins and `make setup` puts in `.cache/esp-rom-elfs/20241011/`, and
it does not read `$IDF_TOOLS_PATH`. Pass `--rom` when the result has to match a test run: the
backend always does.

## Connect a peer

The peer listens first and the runner connects once, at start: bind the socket, then start the
runner with `--board socket:PATH`. Keep `PATH` short: `sun_path` holds 104 bytes on macOS and
108 on Linux. `--board tcp:HOST:PORT` is for a runner in a Linux container, where a Unix socket
does not cross Docker Desktop's file sharing on macOS.

What goes over the socket, and the rules the peer has to keep, are in
[the link protocol](../protocol.md): the handshake, the turns, the messages and how a run ends. In
short, the runner sends `hello`, the peer answers with its own `hello` and the pins it is wired to
(`watch`), and the runner sends `sync` with `t = 0` before the guest's first instruction. From
then on the peer answers each `sync` with its inputs and one `wait`; between its turns it only
reads.

With `--console socket` the guest's UART0 goes to the peer as `console` messages and not to
stdout; with `--console both` it goes to both. Messages written outside the peer's turn reach the
socket within about 20 wall milliseconds, so a peer can read the console as it comes.

`--peer-timeout` bounds, in wall time, how long the runner waits for the peer's turn to end and
how long a write to the socket may make no progress because the peer does not read; for `tcp:` it
also bounds each connection attempt, though not the name lookup before it. A peer that hangs then
fails the run with exit code 4 instead of hanging a CI job.

## What it prints

stdout carries the guest's UART0 output and nothing else, unless `--console socket` sends it to
the peer instead. Everything the runner says goes to stderr, on lines that start with
`esp32tl-runner: `:

- `esp32tl-runner: ROM PATH` once the ROM ELF is loaded;
- `esp32tl-runner: flash 0x1000: PATH (N bytes)` for each `--flash-at` file, offset in hex;
- `esp32tl-runner: chip reset at T s, cause CAUSE` each time the firmware resets the chip, as
  `esp_restart()` does: T in emulated seconds with six decimals, CAUSE the reset cause the ROM
  reports, in hex (`0x...`). The runner restarts the guest from the ROM, as the chip would, and
  the run goes on. With `--board`, the peer gets a `reset` message as well.

esp32sim can print lines of its own on stderr; it does with its debug switches set
([technical side](runner-technical.md#configuration)).

## How a run ends

Without `--board`, a run that gets past loading and that the emulator ends, not a signal, ends
with one line on stderr:

```text
esp32tl-runner: stop: VARIANT at T s (W s wall, N resets)
```

`VARIANT` is the name of the esp32sim `Stop` that ended it, T the emulated seconds (six decimals),
W the wall-clock seconds the run took after loading (one decimal), N the number of chip resets.

With `--board`, every run that gets past loading ends with one line naming its cause instead,
whatever ended it but a second signal:

```text
esp32tl-runner: end: CAUSE at T s (W s wall, N resets, K turns)
```

W counts from the connection, K is the number of turns the peer had. A run that cannot connect
prints `esp32tl-runner: end: cannot connect to the peer at BOARD: ERROR`, with `BOARD` as given
to `--board`, and nothing after it.

| Exit code | Meaning | What stderr shows last |
|---|---|---|
| 0 | the run reached `--max-seconds`: `stop: Halted`, the normal end | the stop line, or `end: max_seconds at ...` |
| 0 | with `--board`: the peer sent `stop` | `end: stop from the peer at ...` |
| 0 | with `--board`: `SIGTERM` or `SIGINT` | `end: signal at ...` |
| 1 | the emulator stopped for any other reason | the stop line, then `esp32tl-runner: the emulator stopped before --max-seconds: STOP`, the stop with its fields; with `--board`, `end: the emulator stopped before --max-seconds: STOP at ...` |
| 2 | a usage error | `esp32tl-runner: MESSAGE`, then `run esp32tl-runner --help for the usage` |
| 2 | an input that cannot be loaded: no ROM found, a file that cannot be read, a ROM that is not an ELF, an image that does not fit in flash | `esp32tl-runner: MESSAGE`, no stop line |
| 3 | with `--board`: a protocol error by the peer, such as a malformed line, a version or chip mismatch, an input stamped before its turn's `sync` | `end: protocol error by the peer at BOARD: WHAT: LINE at ...`, `LINE` cut after 200 characters |
| 4 | with `--board`: the connection failed, or the peer is gone: it closed the socket, a read or write failed, or `--peer-timeout` ran out | `end: cannot connect to the peer at ...`, or `end: the peer at BOARD is gone: WHY at ...` |
| 101 | a panic, in the runner or in esp32sim during the run | Rust's panic message, no stop line |
| 128 + N | with `--board`: a second `SIGTERM` or `SIGINT` (signal number N) | whatever came before it |

The variants other than `Halted` are `MaxInsns`, `Breakpoint`, `Unimplemented`, `SwReset`,
`Simcall`, `Ebreak`, `Watch`, `Exceptions`, `CostModel` and `CostModelLifecycle`. `SwReset` is
handled as a chip reset and never ends a run. With `--board`, the peer also gets a final `stats`
message for every end but a vanished peer and a second signal
([protocol, section 9](../protocol.md#9-ending-a-run)). Every message, with its cause, is in
[Failure modes](runner-technical.md#failure-modes).

### Ended by a signal

Without `--board`, the runner handles no signal. `SIGINT` (Ctrl-C) or `SIGTERM` ends it at once,
wherever the run is, without the stop line and with none of the exit codes above: the process dies
of the signal, so a shell reports 128 plus its number (130 for `SIGINT`, 143 for `SIGTERM`) and
Python's `Popen` its negative.

With `--board`, once the runner has connected, `SIGTERM` or `SIGINT` ends the run at the end of
the slice in progress, at most one emulated millisecond, or at once if the runner is waiting for
the peer. The peer gets the events not yet sent and `stats` with `end: signal`, and the runner
exits 0. Those last messages get two wall seconds; a peer that does not read them by then is given
up on, and the exit code stays 0. A second `SIGTERM` or `SIGINT` ends the process at once, with
128 plus the signal's number. Before the connection is made, a signal ends the process as it does
without `--board`. A test run usually ends otherwise: the backend starts the runner with
`--max-seconds 86400 --console both --peer-timeout 0` at `board.power_on()`, holds it in a turn
whenever the test is not waiting, and at teardown answers that turn with `stop`. A runner in no
held turn then, or still running 5 s later, gets one `SIGTERM` once it has said hello, then
`SIGKILL` 5 s after that ([esp32sim backend](esp32sim-backend-user.md#how-the-run-ends)).

## See also

- [runner-technical.md](runner-technical.md): how it works, every message, the pins and profiles.
- [Link protocol](../protocol.md): what goes over the socket, for whoever writes a peer.
- [esp32sim-backend-user.md](esp32sim-backend-user.md): how tests start the runner, and
  `ESP32TL_RUNNER` / `ESP32TL_ROM` to use another build.
- [Stage 0 findings, Speed](../stage0-findings.md#speed): how emulated time relates to wall time.
