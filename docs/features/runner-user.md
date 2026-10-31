# esp32tl-runner

`esp32tl-runner` runs an unmodified image for the classic ESP32 in the esp32sim emulator, on the
emulated clock, and prints the guest's UART0 console on stdout. The `esp32sim` backend of the
pytest plugin starts it for every test that uses `board` or `console`; this document is for
running it by hand, to watch an image boot or to see why a test run ended the way it did. It has
the console only: the board socket of [the link protocol](../protocol.md) is not in this runner.

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
  --console stdout|socket|both   where UART0 goes (default stdout); socket and both need
                        --board socket:, which comes later
```

| Flag | Default | Accepts |
|---|---|---|
| `--chip` | none, required | `esp32`, in any case |
| `--flash-at OFFSET=FILE` | none, at least one required | `OFFSET` in hex, with or without a lowercase `0x`; `FILE` is everything after the first `=`. Repeatable; files are written in the order given |
| `--max-seconds S` | none, required | a finite number above 0; fractions are fine |
| `--rom F` | the lookup below | a path to the mask ROM ELF |
| `--flash-mb N` | `4` | `1`, `2`, `4`, `8` or `16` |
| `--psram-mb N` | `0` | `0` or `4` |
| `--console` | `stdout` | `stdout`; `socket` and `both` are parsed, then refused |

A flag given twice keeps its last value, except `--flash-at`, which adds a file each time. Flags
take their value as the next argument: `--flash-mb=8` is an unknown argument.

### Where the ROM comes from

Without `--rom`, the runner looks for `esp32_rev300_rom.elf` in `$ESP32SIM_ROM_DIR`, then in
`~/.espressif/tools/esp-rom-elfs/*/`, the last directory in name order first, which for the dated
release directories is the newest. That is esp32sim's own lookup. It does not prefer release
`20241011`, the one the project pins and `make setup` puts in `.cache/esp-rom-elfs/20241011/`, and
it does not read `$IDF_TOOLS_PATH`. Pass `--rom` when the result has to match a test run: the
backend always does.

## What it prints

stdout carries the guest's UART0 output and nothing else. Everything the runner says goes to
stderr, on lines that start with `esp32tl-runner: `:

- `esp32tl-runner: ROM PATH` once the ROM ELF is loaded;
- `esp32tl-runner: flash 0x1000: PATH (N bytes)` for each `--flash-at` file, offset in hex;
- `esp32tl-runner: chip reset at T s, cause CAUSE` each time the firmware resets the chip, as
  `esp_restart()` does: T in emulated seconds with six decimals, CAUSE the reset cause the ROM
  reports, in hex (`0x...`). The runner restarts the guest from the ROM, as the chip would, and
  the run goes on.

esp32sim can print lines of its own on stderr; it does with its debug switches set
([technical side](runner-technical.md#configuration)).

## How a run ends

A run that gets past loading and that the emulator ends, not a signal, ends with one line on
stderr:

```text
esp32tl-runner: stop: VARIANT at T s (W s wall, N resets)
```

`VARIANT` is the name of the esp32sim `Stop` that ended it, T the emulated seconds (six decimals),
W the wall-clock seconds the run took after loading (one decimal), N the number of chip resets.

| Exit code | Meaning | What stderr shows last |
|---|---|---|
| 0 | the run reached `--max-seconds`: `stop: Halted`, the normal end | the stop line |
| 1 | the emulator stopped for any other reason | the stop line, then `esp32tl-runner: the emulator stopped before --max-seconds: STOP`, the stop with its fields |
| 2 | a usage error | `esp32tl-runner: MESSAGE`, then `run esp32tl-runner --help for the usage` |
| 2 | an input that cannot be loaded: no ROM found, a file that cannot be read, a ROM that is not an ELF, an image that does not fit in flash | `esp32tl-runner: MESSAGE`, no stop line |

The variants other than `Halted` are `MaxInsns`, `Breakpoint`, `Unimplemented`, `SwReset`,
`Simcall`, `Ebreak`, `Watch`, `Exceptions`, `CostModel` and `CostModelLifecycle`. `SwReset` is
handled as a chip reset and never ends a run.

`--console socket` and `--console both` exit 2 with
`esp32tl-runner: --console socket and both need --board socket:PATH, which this runner does not have yet`,
and `--board` itself is an unknown argument. Every message, with its cause, is in
[Failure modes](runner-technical.md#failure-modes).

### Ended by a signal

The runner handles no signal. `SIGINT` (Ctrl-C) or `SIGTERM` ends it at once, wherever the run is,
without the stop line and with none of the exit codes above: the process dies of the signal, so a
shell reports 128 plus its number (130 for `SIGINT`, 143 for `SIGTERM`) and Python's `Popen` its
negative. A test run ends this way unless the test waited for the end of the run: the backend
starts the runner with `--max-seconds 300`, and at teardown sends one still running `SIGTERM`,
then `SIGKILL` after 5 s
([esp32sim backend](esp32sim-backend-user.md#when-the-runner-exits-with-an-error)). Ending on
`SIGTERM` with a final `stats` message and exit 0 is what
[the link protocol, section 9](../protocol.md#9-ending-a-run) specifies, not what this runner does.

## See also

- [runner-technical.md](runner-technical.md): how it works, every message, the pins and profiles.
- [esp32sim-backend-user.md](esp32sim-backend-user.md): how tests start the runner, and
  `ESP32TL_RUNNER` / `ESP32TL_ROM` to use another build.
- [Stage 0 findings, Speed](../stage0-findings.md#speed): how emulated time relates to wall time.
- [Link protocol](../protocol.md): the board socket the runner does not implement.
