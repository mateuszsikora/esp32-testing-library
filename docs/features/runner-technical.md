# esp32tl-runner: how it works

The crate is `runner/`, package `esp32tl-runner`: `runner/src/main.rs` (setup and run loop) and
`runner/src/cli.rs` (the command line). Its unit tests are `runner/src/tests.rs` and
`runner/src/cli/tests.rs`. It drives esp32sim through two of its crates, `esp32` (the classic
chip) and `esp-soc` (the machine, `Soc`, `Stop`). esp32sim lines below are at its pin, `8c65215`.

## Data flow

`main` runs these steps in order:

1. **Parse.** `cli::parse` reads the arguments after the program name into `cli::Opts`, or
   returns `cli::Error::Help` (usage on stderr, exit 0) or `cli::Error::Usage` (exit 2 through
   `usage_error`). Flags are read in order; the first bad one is reported. After the loop it
   checks, in this order, that `--chip` was given and is `esp32`, that `--max-seconds` was given,
   and that there is at least one `--flash-at`.
2. **Refuse the socket console.** `--console socket` or `both` parse, then `main` refuses them
   before anything is loaded: the runner has no board socket.
3. **Set up** (`setup`, which returns the `esp32::Machine` at the reset vector):
   - `esp32::machine(MAC, flash_size)` with `MAC` = `24:6f:28:00:11:22`, the MAC esp32sim's CLI
     gives the classic chip (`cli/src/lib.rs`), so a run here and the same run in esp32sim agree.
     `esp32::machine` also reads esp32sim's debug switches from the environment.
   - `m.bus.set_flash_size(flash_mb << 20)`: the JEDEC capacity the guest reads follows the size,
     as in esp32sim. The CLI allows only powers of two, because the guest reads the capacity as
     one (`esp32/src/soc.rs`, `set_flash_size` stores `trailing_zeros`); any other size would boot
     into a flash-size mismatch instead of failing at the command line.
   - `m.bus.set_psram_size(psram_mb << 20)` when `--psram-mb` is not 0.
   - `m.bus.attach_board_devices()` with the machine's default board: esp32sim's `--board none`.
   - The ROM: `--rom`, else `find_rom` (below). `m.load_rom`, then the `ROM` line on stderr.
   - Each `--flash-at` file, in command-line order: read whole, `m.write_flash(offset, data)`,
     then the `flash` line. Nothing checks that files overlap; a later one overwrites.
   - `m.boot_rom()` (core 0 to the reset vector); `m.console.mask = CONSOLE_UART0` (`2`, bit 1 of
     `Console::mask`: UART0 only to stdout, not USB-CDC or UART1/2); and
     `m.max_cycles = (max_seconds * <esp32::Esp32 as Soc>::CPU_HZ as f64) as u64`. `CPU_HZ` is
     240 000 000 (`esp32/src/periph.rs`), the fixed reference `bus.cycles()` counts at, so
     `max_cycles` is emulated time whatever clock the guest switches to.
4. **Run** (`run`). The wall clock starts here, after setup. `m.run(u64::MAX)` runs until a
   `Stop`; `m.drain_console()` then flushes what the guest printed, so it comes out before the
   runner's own line. On `Stop::SwReset` the runner calls `m.reboot()`, prints the `chip reset`
   line with the cause `reboot()` returns, and runs again, from the reset vector as the chip
   would. Any other `Stop` returns. `Stop::Halted` is what `Machine::run` returns once
   `bus.cycles() >= max_cycles` (`esp-soc/src/machine.rs`).
5. **Report.** The stop line, with `stop_name(&stop)`, `m.seconds()` (bus cycles / `CPU_HZ`),
   the wall time and `m.reboots`. `Halted` exits 0; anything else prints the stop's `Debug` and
   exits 1.

`find_rom(name)` follows esp32sim's lookup (`cli/src/lib.rs`, `find_rom`), with two differences: a
candidate must be a file, where esp32sim takes any path that exists, and `HOME` is read with
`var_os`, so it need not be valid UTF-8:
`$ESP32SIM_ROM_DIR/name` if that is a file; otherwise every entry of
`$HOME/.espressif/tools/esp-rom-elfs/`, sorted by path and tried last first, the first one with
`name` in it as a file. No `HOME`, or no such directory,
is no ROM. `name` is `<esp32::Esp32 as Soc>::ROM_ELF`, `esp32_rev300_rom.elf`.

`stop_name` matches every `Stop` variant explicitly, with no catch-all arm, so a variant added
upstream fails the build instead of being reported under another name.

## Interfaces

The command line and the usage text: [runner-user.md](runner-user.md#run-an-image).

- stdin: not read.
- stdout: the guest's UART0 and nothing else, written by esp-soc's `Machine::drain_console`, so
  that whoever reads stdout alone gets the guest's output as the guest wrote it. The runner's own
  lines go to stderr. Under the backend the two meet again: pytest-embedded's
  `DuplicateStdoutPopen` writes stdout and stderr into one log, which `dut.expect` reads.
- stderr, from the runner (formats from `main.rs`):
  - `esp32tl-runner: ROM {path}`
  - `esp32tl-runner: flash {offset:#x}: {path} ({len} bytes)`
  - `esp32tl-runner: chip reset at {seconds:.6} s, cause {cause:#x}`
  - `esp32tl-runner: stop: {VARIANT} at {seconds:.6} s ({wall:.1} s wall, {reboots} resets)`
  - `esp32tl-runner: the emulator stopped before --max-seconds: {stop:?}`
- Exit codes: 0 for `Stop::Halted`; 1 for any other stop; 2 for usage errors and inputs that
  cannot be loaded. esp32sim also exits 2 on a usage error, but panics (exit 101) on a ROM ELF it
  cannot load. 0 and 1 follow the stop line; 2 comes
  before the run starts, without it.
- Signals: none handled; `main` and `run` install no handler. A signal ends the process with its
  default action, wherever the run is, with no stop line and none of the codes above:
  `Popen.returncode` is minus the signal's number (`-15` for `SIGTERM`). `Esp32Sim.close` relies
  on that: it sends `SIGTERM` to a runner still running at teardown, `SIGKILL` 5 s later, and
  accepts the matching negative code
  ([esp32sim-backend-technical.md](esp32sim-backend-technical.md)). The `SIGTERM` end with
  `stats` and exit 0 in [the protocol, section 9](../protocol.md#9-ending-a-run) is not
  implemented.

`cli::Console` already has `Stdout`, `Socket` and `Both`, and the flag names copy esp32sim's, so
that the change to the board socket of [the protocol](../protocol.md) is a rename
([plan, 4.2](../implementation-plan.md#42-runner-crate)).

## Configuration

| Name | Read by | Effect |
|---|---|---|
| `ESP32SIM_ROM_DIR` | `find_rom` | first place to look for `esp32_rev300_rom.elf` when `--rom` is absent |
| `HOME` | `find_rom` | `$HOME/.espressif/tools/esp-rom-elfs/*/` is the second place |
| `ESP_EMU_DEBUG`, `ESP_EMU_DEBUG_<AREA>`, `ESP_EMU_RT_LOG`, `ESP_EMU_LOG_ALL` | `esp32::machine`, through esp-soc's `DebugFlags::from_env` (`esp-soc/src/debug.rs`) | esp32sim's debug output on stderr |

`$IDF_TOOLS_PATH` is not read: `scripts/fetch-rom-elf` honours it, `find_rom` does not.

**The esp32sim pin.** esp32sim is not on crates.io, so `runner/Cargo.toml` takes `esp32` and
`esp-soc` (and `emu-core`, a dev-dependency for `Stop::CostModelLifecycle` in a test) from
`https://github.com/mikroverk/esp32sim`, each at
`rev = "8c652151dd5a521249a785ca147f20a9f8eda68f"`: the merge of mikroverk/esp32sim#322, from
which images built for chip revision v3.0 boot
([plan, findings 3 and 5](../implementation-plan.md#1-findings-that-amend-the-specification)).
Every esp32sim crate is pinned to the same revision, and `runner/Cargo.lock` resolves all of them
from that one source; the Makefile builds with `--locked`. This revision is the project's esp32sim
pin: changing it here changes it everywhere.

**Toolchain.** `runner/rust-toolchain.toml` pins channel `1.99.1`, profile `minimal`, with
`clippy` and `rustfmt`: the release the runner was first built with
([plan, section 8](../implementation-plan.md#8-environment-checked-on-this-machine-2026-10-29)).
`rust-version = "1.95"` in `Cargo.toml` is the real minimum, for a toolchain that ignores
`rust-toolchain.toml` (one not from rustup): at the pin, `esp-soc/src/nat.rs` calls
`AtomicUsize::try_update`, stable since 1.95. rustup reads `rust-toolchain.toml` from the
directory cargo runs in, not from `--manifest-path`, so the Makefile runs cargo in `runner/`.

**Profiles.** `make build-runner` and `make test-runner` take `RUNNER_PROFILE` (default `release`;
[development guide](../guides/development.md)). Dependencies take their profile from the top
crate, not from esp32sim's workspace, so the profiles here decide how fast the emulator is:

| Profile | Settings | Why |
|---|---|---|
| `release` | `opt-level = 3`, `debug = 1`, `lto = "fat"`, `codegen-units = 1` | a copy of esp32sim's release profile: what tests run, into `target/release/` |
| `dev` | `opt-level = 3`, `debug-assertions = false`, `overflow-checks = false` | local iteration, into `target/debug/`. After a change to the runner, a release build links everything again with fat LTO, about 10 s on an M2 Pro against 0.3 s in dev. Optimised all the same, because an unoptimised emulator is too slow to boot an image; checks off as upstream, which builds and tests only in release |

`make test-runner` uses the same profile as `make build-runner`, so the tests reuse its build of
esp32sim; in release the test binary still costs a fat-LTO link, not a rebuild.

## Failure modes

Every message starts with `esp32tl-runner: `. Usage errors exit 2 and add a second line,
`run esp32tl-runner --help for the usage`:

| Message | Cause |
|---|---|
| `{flag} needs a value` | a flag that takes a value is the last argument |
| `unknown argument {flag:?}` | anything not in the usage, `--board` and `--flash-mb=8` included |
| `--chip is required (esp32)` | no `--chip` |
| `--chip {c}: only esp32 is supported` | another chip; `c` is lowercased |
| `--max-seconds is required` | no `--max-seconds` |
| `--max-seconds: expected a positive number of seconds, got {v:?}` | not a number, 0 or less, `inf` or `NaN` |
| `nothing to run: give the images with --flash-at OFFSET=FILE` | no `--flash-at` |
| `--flash-at {spec}: expected OFFSET=FILE` | no `=`, or a valid offset with nothing after it (`zz=` gets the next message) |
| `--flash-at {spec}: bad hex offset {offset:?}` | the offset is not hex after every leading `0x` is trimmed (`0x0x1000` is `0x1000`; `0X` is not trimmed) |
| `--flash-mb: the classic ESP32 takes 1, 2, 4, 8 or 16 MiB, got {v:?}` | any other value |
| `--psram-mb: the classic ESP32 supports only 0 or 4 MiB, got {v:?}` | any other value |
| `--console: expected stdout, socket or both, got {v:?}` | any other value |
| `--console socket and both need --board socket:PATH, which this runner does not have yet` | `--console socket` or `both` (from `main`, after parsing) |

Inputs that cannot be loaded exit 2 through `fail`, with no second line and no stop line:

| Message | Cause |
|---|---|
| `no esp32_rev300_rom.elf found: pass --rom, or set ESP32SIM_ROM_DIR` | no `--rom` and `find_rom` found nothing |
| `{path}: {error}` | the ROM or a flash file cannot be read; `error` is the OS error, e.g. `No such file or directory (os error 2)` |
| `{rom}: {error}` | esp32sim cannot load the ROM ELF, e.g. `not an ELF file`, `need ELF32 little-endian` (`esp-soc/src/elf.rs`) |
| `--flash-at {offset:#x}={path}: flash image too large` | the file does not fit in `--flash-mb` from its offset (`esp32/src/bus.rs`, `write_flash`) |
| `classic PSRAM supports 0 or 4194304 bytes` | esp32sim's own check (`esp32/src/psram.rs`); the CLI refuses those sizes first, so it is not reached |

Exit 1: the stop line, then `the emulator stopped before --max-seconds: {stop:?}`, for every
`Stop` but `Halted` and `SwReset`.

## Tests

`make test-runner` runs both files (`cargo test --locked`).

- `runner/src/cli/tests.rs`: `cli::parse` only. `minimal_command_line` checks the defaults
  (`rom: None`, flash 4 MiB, PSRAM 0, console `Stdout`);
  `flash_at_offsets_are_hex_with_or_without_prefix` checks hex offsets with and without `0x`,
  repeated `--flash-at` in order, a path that contains `=`, `--chip ESP32` in upper case and a
  fractional `--max-seconds`; `options` checks `--rom`, `--flash-mb`, `--psram-mb` and
  `--console both`; `help` checks that `--help` returns `Error::Help`; `usage_errors` checks that
  17 bad command lines return `Error::Usage`, among them a missing `--chip`, `--max-seconds` or
  `--flash-at`, `--board none`, `--max-seconds 0` and `inf`, and the flash sizes 0, 3, 32 and
  17592186044416 (which would overflow `<< 20`).
- `runner/src/tests.rs`: `stop_names_are_the_variant_names` builds every `Stop` variant and
  checks that `stop_name` is the start of its `Debug` text, followed by nothing, a space or `(`,
  so that `CostModelLifecycle` reported as `CostModel` fails.

Not covered by the Rust tests: the text of any message (`usage_errors` checks only the error
kind), `main`, `setup`, `find_rom`, `run`, the exit codes and the format of the stderr lines. The
pytest suite runs the real binary on `hello_world` (`tests/test_hello_world.py`, and
`test_hello_world_fails_on_an_image_that_prints_something_else` in `tests/test_pytest_plugin.py`),
which shows that a run boots and prints, but checks no runner line and no exit code. No test
runs the real runner through the `SwReset` path or to an exit 1;
`tests/test_pytest_plugin.py::test_a_runner_that_exits_with_an_error_fails_the_test` checks how
the backend handles an exit 1, from a stand-in runner.

## See also

- [runner-user.md](runner-user.md): running it by hand.
- [esp32sim-backend-technical.md](esp32sim-backend-technical.md): the command line the backend
  builds and how it reads the exit.
- [Implementation plan, 4.2 Runner crate](../implementation-plan.md#42-runner-crate) and
  [Link protocol](../protocol.md): the board socket that is not implemented here.
- [CI](../cicd.md): the `runner` job.
