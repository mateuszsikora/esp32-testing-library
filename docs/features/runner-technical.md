# esp32tl-runner: how it works

The crate is `runner/`, package `esp32tl-runner`:

| Module | What it does |
|---|---|
| `runner/src/main.rs` | setup, the run without a peer, and the start and report of the run with one |
| `runner/src/cli.rs` | the command line |
| `runner/src/socket_board.rs` | `SocketBoard`, the `BoardModel` on the chip's pins with `--board`: it records what the guest does, with its bus cycle, for the run loop, holds the GPIO levels the peer drives for the chip to take, and hands SPI transactions to the SD card |
| `runner/src/sd_card.rs` | `SdCard`, the SD card of `--sd-rw` in SPI mode, and `CardBoard`, the board that holds it alone without `--board` |
| `runner/src/session.rs` | `Session`, the run loop with a peer: slices, turns, coalescing, pacing, warnings, `stats` |
| `runner/src/protocol.rs` | the wire format: time conversion, base64, parsing the peer's messages and writing the runner's |
| `runner/src/transport.rs` | `SocketLink`, the Unix or TCP socket: buffered NDJSON writes, line reads, the signal flag and the time limits |

Each module's unit tests are in a `tests.rs` of its own: `runner/src/tests.rs` for `main.rs`,
`runner/src/<module>/tests.rs` for the others. It drives esp32sim through two of its crates,
`esp32` (the classic chip) and `esp-soc` (the machine, `Soc`, `Stop`, `BoardModel`, `SpiPins`,
`UartRoute`, and the JSON parser `esp_soc::json` the protocol module reads with), and takes the
SD card's image store from a third, `esp-periph` (`esp_periph::sdmmc::{SdStore, FileStore}`). esp32sim lines below are at
its pin, `8c65215`.

## Data flow

`main` runs these steps in order:

1. **Parse.** `cli::parse` reads the arguments after the program name into `cli::Opts`, or
   returns `cli::Error::Help` (usage on stderr, exit 0) or `cli::Error::Usage` (exit 2 through
   `usage_error`). Flags are read in order; the first bad one is reported. After the loop it
   checks, in this order, that `--chip` was given and is `esp32`, that `--max-seconds` was given,
   that there is at least one `--flash-at`, that `--sd-rw` and `--sd-spi` come together, and that
   `--console socket` or `both` comes with `--board`.
2. **Open the card**, with `--sd-rw` (`card`): `FileStore::write_through` opens the image for
   reading and writing, `SdCard::new` checks its size, and the `SD card on` line goes to stderr.
   The card is a `sd_card::Shared` (`Rc<RefCell<SdCard>>`), which the board and `main` hold.
3. **Set up** (`setup`, which returns the `esp32::Machine` at the reset vector):
   - `esp32::machine(MAC, flash_size)` with `MAC` = `24:6f:28:00:11:22`, the MAC esp32sim's CLI
     gives the classic chip (`cli/src/lib.rs`), so a run here and the same run in esp32sim agree.
     `esp32::machine` also reads esp32sim's debug switches from the environment.
   - `m.bus.set_flash_size(flash_mb << 20)`: the JEDEC capacity the guest reads follows the size,
     as in esp32sim. The CLI allows only powers of two, because the guest reads the capacity as
     one (`esp32/src/soc.rs`, `set_flash_size` stores `trailing_zeros`); any other size would boot
     into a flash-size mismatch instead of failing at the command line.
   - `m.bus.set_psram_size(psram_mb << 20)` when `--psram-mb` is not 0.
   - With `--board`, `m.bus.board` becomes a `SocketBoard` holding a `socket_board::Shared`
     (`Rc<RefCell<Recorder>>`) that the run loop holds too, and the card if there is one; without
     `--board` it becomes a `CardBoard` holding the card, or with no card stays the machine's
     default board, esp32sim's `--board none`. Then `m.bus.attach_board_devices()`, which reads
     `uses_uart_pins` and so makes the bus call `uart_tx` on every TX FIFO write.
   - The ROM: `--rom`, else `find_rom` (below). `m.load_rom`, then the `ROM` line on stderr.
   - Each `--flash-at` file, in command-line order: read whole, `m.write_flash(offset, data)`,
     then the `flash` line. Nothing checks that files overlap; a later one overwrites.
   - `m.boot_rom()` (core 0 to the reset vector); `m.console.mask = CONSOLE_UART0` (`2`, bit 1 of
     `Console::mask`: UART0 only to stdout, not USB-CDC or UART1/2), or `0` with
     `--console socket`; and
     `m.max_cycles = (max_seconds * <esp32::Esp32 as Soc>::CPU_HZ as f64) as u64`. `CPU_HZ` is
     240 000 000 (`esp32/src/periph.rs`), the fixed reference `bus.cycles()` counts at, so
     `max_cycles` is emulated time whatever clock the guest switches to.
4. **Run.** Without `--board`, `run`: the wall clock starts here, after setup. `m.run(u64::MAX)`
   runs until a `Stop`; `m.drain_console()` then flushes what the guest printed, so it comes out
   before the runner's own line. On `Stop::SwReset` the runner calls `m.reboot()`, prints the
   `chip reset` line with the cause `reboot()` returns, and runs again, from the reset vector as
   the chip would. Any other `Stop` returns. `Stop::Halted` is what `Machine::run` returns once
   `bus.cycles() >= max_cycles` (`esp-soc/src/machine.rs`).

   With `--board`, `run_with_board`: it connects with `SocketLink::unix(path)` or
   `SocketLink::tcp(address, --peer-timeout)`, which tries every address the name resolves to
   with `connect_timeout` and sets `TCP_NODELAY`; a failure prints the `end: cannot connect` line
   and exits 4. Only then does it install the signal handlers (see Interfaces) and make
   `--peer-timeout` the write limit, and the wall clock starts. Then `session::Session::run`
   ([below](#the-session)), and `m.drain_console()`.
5. **Report.** With a card, its `SD card:` line first. Without `--board`, the stop line, with `stop_name(&stop)`, `m.seconds()` (bus
   cycles / `CPU_HZ`), the wall time and `m.reboots`; `Halted` exits 0, anything else prints the
   stop's `Debug` and exits 1. With `--board`, the `end` line, with the cause from `session::End`,
   `m.seconds()`, the wall time, `stats.resets` and `stats.turns`, and the exit code of
   `End::exit_code`.

`find_rom(name)` follows esp32sim's lookup (`cli/src/lib.rs`, `find_rom`), with two differences: an
entry of `esp-rom-elfs/` must hold `name` as a file, where esp32sim takes any path there that
exists, and `HOME` is read with `var_os`, so it need not be valid UTF-8. The lookup:
`$ESP32SIM_ROM_DIR/name` if that is a file, as in esp32sim; otherwise every entry of
`$HOME/.espressif/tools/esp-rom-elfs/`, sorted by path and tried last first, the first one with
`name` in it as a file. No `HOME`, or no such directory, is no ROM. `name` is
`<esp32::Esp32 as Soc>::ROM_ELF`, `esp32_rev300_rom.elf`.

`stop_name` matches every `Stop` variant explicitly, with no catch-all arm, so a variant added
upstream fails the build instead of being reported under another name.

### The SD card

esp32sim's classic SPI masters hand each transaction to the board: once its command, address and
MOSI bytes are known, from the W registers or the DMA out-link, `run_gp_spi`
(`esp32/src/bus.rs:504-568`) calls `spi_transfer_pins(host, pins, tx, rx_len)` on a board whose
`uses_spi_pins` is true, and puts the bytes it returns into the W registers or the DMA in-link.
`SpiPins` carries the routes at that moment: the pads the master's SCLK and MOSI signals drive,
the pad its MISO signal is read from, and as `cs` every low, enabled output among GPIO 0 to 33,
which is how a software CS, a GPIO the driver sets low, shows (`esp32/src/periph.rs:414-427`).
`SocketBoard` and `CardBoard` answer `uses_spi_pins` with true when they hold a card and pass the
transaction to `SdCard::transfer`.

`SdCard::transfer` returns 0xff for every byte unless the card's SCLK pin is in `sclk` and its CS
pin in `cs`. It then clocks `max(tx.len(), rx_len)` bytes through `exchange`, MOSI 0xff past the
end of `tx` or when the card's MOSI pin is not in `mosi`, and keeps the card's bytes only when
`miso` is the card's MISO pin. Byte `i` of the answer is the card's byte while byte `i` of `tx`
comes in: a transaction is taken as full duplex, as ESP-IDF's `spi_master` makes one by default.
A half-duplex read (MOSI phase, then MISO phase) would see the card's bytes one phase early; no
SD driver uses one.

`exchange` sends the next byte of `out`, the card's queue, 0xff when it is empty, then takes the
byte that came in according to `phase`:

| Phase | What an incoming byte does |
|---|---|
| `Command` | Starts or continues a 6-byte command; a first byte must have `01` in its top bits. A whole command runs `command` |
| `Reading { next }` | As `Command`; when `out` runs dry, block `next` is queued (CMD18) |
| `Awaiting { lba, multi }` | 0xfe (CMD24) or 0xfc (CMD25) starts a block, 0xfd ends a CMD25; a command byte as in `Command` |
| `Receiving { lba, multi, buf }` | Collects 512 data bytes and 2 CRC bytes, then writes the block and queues the data response |

`command` checks the CRC7 of CMD0 and CMD8 always and of every command once CMD59 has turned
checking on, and answers a bad one with R1 `COM_CRC_ERROR`. Until the first CMD0 with a good CRC
the card is in SD mode and answers nothing. Every command clears `out` and returns the phase to
`Command`, which ends a read in progress (CMD12) and a write still waiting for its token. The
answer starts with one 0xff (N<sub>CR</sub>), then R1, with `IDLE` set until ACMD41 has finished.

| Command | Answer |
|---|---|
| CMD0 | R1 `IDLE`; initialisation, CRC checking and the erase range start over |
| CMD8 | R7: R1, then `00 00 VCA PATTERN`, VCA 1 when VHS is 1 (2.7-3.6 V), else 0 |
| CMD55, then ACMD41 | R1; with CMD8 accepted and HCS (bit 30) set, the first ACMD41 answers `IDLE` and the next one finishes initialisation (`INIT_POLLS` = 1, as esp32sim's card); without them the card stays idle |
| CMD58 | R3: R1, then the OCR, `0x00FF8000` before initialisation and `0xC0FF8000` (ready, CCS) after |
| CMD59 | R1; bit 0 of the argument turns CRC checking on or off |
| Any other command before initialisation | R1 `IDLE \| ILLEGAL_COMMAND` |
| CMD9, CMD10 | R1, then the CSD or the CID as a data block |
| CMD12 | a stuff byte, then R1 (ESP-IDF looks for R1 after the stuff byte) |
| CMD13 | R2: R1, then 0 |
| CMD16 | R1: blocks are 512 bytes whatever the argument |
| CMD17, CMD18 | R1, then the block at the argument (a block address) as a data block; CMD18 goes on to the next block until a command comes, and a block past the end is an error token `0x08`; a start past the end is R1 `PARAMETER_ERROR` |
| CMD24, CMD25 | R1, then `Awaiting`; a start past the end is R1 `PARAMETER_ERROR` |
| CMD32, CMD33, CMD38 | R1; CMD38 erases the range to zeros (`SdStore::erase`), or answers `ERASE_SEQ_ERROR` without one, `PARAMETER_ERROR` for an end before the start |
| ACMD13 | R2, then the SD status as a 64-byte data block |
| ACMD22 | R1, then the number of blocks the last CMD24 or CMD25 wrote |
| ACMD23, ACMD42 | R1 |
| ACMD51 | R1, then the SCR as an 8-byte data block |
| Anything else, CMD5, CMD52 and CMD6 among them | R1 `ILLEGAL_COMMAND` |

A data block is one 0xff (N<sub>AC</sub>), the token 0xfe, the data and its CRC16, big-endian.
An application command index the card does not define is taken as the standard command of that
index. A block that comes in is answered with its data response, `0x05` accepted, `0x0b` with a
bad CRC16 while checking is on, `0x0d` past the end or when the image cannot be written, on the
byte after its CRC; the card is never busy after it. A CMD25 waits for the next token after any
of them.

The registers (SD Physical Layer Simplified Specification, chapter 5):

| Register | Contents |
|---|---|
| CID | MID 0, OID `ES`, PNM `SIMSD`, PRV 1.0, PSN from the image's size (esp32sim's formula), MDT 2024-01, CRC7 |
| CSD | version 2.0: TAAC 1 ms, NSAC 0, TRAN_SPEED 25 MHz, CCC `0x135` (classes 0, 2, 4, 5 and 8), READ_BL_LEN 9, C_SIZE from the image, ERASE_BLK_EN 1, SECTOR_SIZE 127, R2W_FACTOR 4, WRITE_BL_LEN 9, CRC7 |
| SCR | structure 1.0, SD_SPEC 2 with SD_SPEC3, DATA_STAT_AFTER_ERASE 0, security 2.00 (SDHC) or 3.00 (SDXC, above 32 GiB), bus widths 1 and 4, no CMD23 |
| SD status | bus width 1, speed class 10, AU 4 MiB, erase fields 0 |

Without class 10 in CCC, ESP-IDF's `sdmmc_enable_hs_mode` does not send CMD6, and the card stays at
default speed. The image goes through esp32sim's `FileStore::write_through`: each 512-byte block
is a seek and a read or write of the file, with no cache, so the file holds every accepted block
at once.

### The session

`Session` runs the protocol over two traits, `Guest` (the machine: cycles, `run_until`,
`chip_reset`, `uart_route`, `rx_pending`, `uart_input`, `gpio_drive`, `take_gpio_inputs`) and
`Link` (the socket: `send`, `flush`, `idle`, `closing`, `recv`), so that its tests drive it with a
scripted guest and a fake peer. `impl Guest for esp32::Machine` sets `max_cycles` and calls
`run(u64::MAX)`, calls `reboot()` for `chip_reset`, reads `bus.periph.uart_route(port)` and
`uart[port].rx_pending()`, delivers a byte with `bus.periph.uart_pin_input` and
`irq_dirty = true`, reads `GPIO_ENABLE` and `GPIO_OUT` (`bus.periph.gpio.gpio.enable` and `.out`)
for `gpio_drive`, and for `take_gpio_inputs` does what the bus's `deliver_board_inputs` does at
its tick: `SocBus::gpio_set_input` for each of the board's `take_edges`, then
`SocBus::gpio_release_input` for each of its `released_inputs`.

`SocketBoard` and `Session` share a `socket_board::Recorder`: the peer's `watch`, the log of what
the guest did, the watched pins' enable and latch as last reported (`reported`) and their pad
levels as last reported in `changes` (`levels_reported`), both of which the run loop sets after
a chip reset, and the peer's GPIO levels. `Recorder::drive` and `Recorder::release`
queue a level or a release for `take_edges` and `released_inputs` and keep the driven levels that
`input_levels` returns; since the chip takes edges before releases, a level drops a release of the
same pin queued before it. `uses_gpio_edges` is `true`; it changes nothing on the classic bus,
where `uses_uart_pins` already turns the board's callbacks on (`esp32/src/bus.rs:418`).

The order of a run, as [the protocol, section 6.1](../protocol.md#61-how-the-runner-meets-the-block-point)
specifies it:

1. `hello` out, the peer's `hello` in (version, chip, `watch`), the power-on routes, then turn 0
   at `t = 0` before the guest's first instruction.
2. A loop of slices. At each boundary `boundary()` takes the board's log (`collect`), reads the
   three UART routes, turns what was recorded into messages up to the block point
   (`process_pending`), takes every turn whose block point is already behind the guest
   (`take_turn`), and, before the end of the run, closes a coalesced message whose character time
   has passed (`close_after_gap`), applies the inputs now due (`apply_inputs`: received bytes onto
   their lines; GPIO levels and releases to the `Recorder`, then `take_gpio_inputs`, with an
   `input_on_output` warning for a level on a pin set in `GPIO_ENABLE`),
   delivers the paced bytes now due (`deliver`, which counts and warns about drops against the
   routes it read), and lets the link write what has waited (`Link::idle`). Then `slice_end`
   picks the earliest of the block point, `latency` after now while no event is unacknowledged,
   the next whole emulated millisecond, the next queued input, the next paced byte and the end of
   the run, and `run_until` runs to it. `Stop::SwReset` queues a `reset` with the cause from
   `chip_reset`, the routes after it and, while `watch` is not empty, a `gpio_out` of the watched
   pins as `gpio_drive` reads them, with a change for every pin whose `GPIO_ENABLE & GPIO_OUT` bit
   differs from `levels_reported` (`reset_gpio`); any other stop but `Halted` ends the run.
3. `finish` calls `Link::closing`, sends what is left where the end calls for it, and `stats`,
   then `Link::close`.

Times on the socket are nanoseconds, from `protocol::ns` (cycles to ns, rounded down) and
`protocol::cycles` (ns to cycles, rounded up), in 128-bit arithmetic.

### The socket

`SocketLink` sets a 20 ms read and write timeout on the socket (`POLL`), so that no read or write
blocks longer before it looks at the signal flag and the time limits. Lines are queued in a write
buffer and written out at every `sync`, when the buffer reaches 64 KiB (`WRITE_BUFFER`), and from
`idle` once the oldest queued line has waited 20 ms (`LIVE`). A write that stops part way keeps
what is left for the next one. A read past 1 MiB without a newline, or a line that is not UTF-8,
is a protocol error. After `closing`, a write no longer gives up on a signal and has 2 s
(`TEARDOWN`) to finish. `close` shuts the write side, then reads and drops whatever the peer sent
that the runner has not read, until a read would block or the peer closes its end. A run that ends
in the peer's turn, on a protocol error or a `stop`, leaves the rest of that turn unread, and on
Linux a Unix socket closed with unread input resets the connection: the peer's read to end of file
then fails with `ECONNRESET` instead of returning `stats`.

## Interfaces

The command line and the usage text: [runner-user.md](runner-user.md#run-an-image).

- stdin: not read.
- stdout: the guest's UART0 and nothing else, written by esp-soc's `Machine::drain_console`, so
  that whoever reads stdout alone gets the guest's output as the guest wrote it; nothing with
  `--console socket`. The runner's own lines go to stderr. Under the backend the two meet again: pytest-embedded's
  `DuplicateStdoutPopen` writes stdout and stderr into one log, `dut.log`; the test's console
  expects read the `console` messages of `--console both` instead.
- stderr, from the runner (formats from `main.rs` and `sd_card.rs`):
  - `esp32tl-runner: SD card on SCLK GPIO {sclk}, MOSI GPIO {mosi}, MISO GPIO {miso}, CS GPIO
    {cs}: {image} ({KiB} KiB, write-through)`
  - `esp32tl-runner: ROM {path}`
  - `esp32tl-runner: flash {offset:#x}: {path} ({len} bytes)`
  - `esp32tl-runner: chip reset at {seconds:.6} s, cause {cause:#x}`
  - `esp32tl-runner: SD card: reading block {lba}: {error}`, `writing block {lba}: {error}` and
    `erasing blocks {lo} to {hi}: {error}`, when the image file fails
  - `esp32tl-runner: SD card: {commands} commands, {read} blocks read, {written} blocks
    written`, with `, {errors} image errors` when there were any, before the stop or end line
  - `esp32tl-runner: stop: {VARIANT} at {seconds:.6} s ({wall:.1} s wall, {reboots} resets)`
  - `esp32tl-runner: the emulator stopped before --max-seconds: {stop:?}`
  - with `--board`, instead of the last two: `esp32tl-runner: end: {cause} at {seconds:.6} s
    ({wall:.1} s wall, {resets} resets, {turns} turns)`, and
    `esp32tl-runner: end: cannot connect to the peer at {board}: {error}` when the connection
    fails
- The socket, with `--board`: NDJSON both ways, as [the protocol](../protocol.md) specifies;
  `protocol.rs` writes and parses every message, `session.rs` decides when each goes.
- Exit codes: 0 for `Stop::Halted`, and with `--board` for `stop` from the peer and for
  `SIGTERM` or `SIGINT`; 1 for any other stop; 2 for usage errors and inputs that cannot be
  loaded; 3 for a protocol error by the peer; 4 when the connection fails or the peer is gone;
  `128 + N` for a second signal with `--board`. esp32sim also exits 2 on a usage error, but panics (exit 101) on a ROM ELF it
  cannot load. 0 and 1 follow the stop line; 2 comes before the run starts, without it. A panic,
  in the runner or in esp32sim during the run, exits 101 with Rust's panic message and no stop
  line: `[profile.release]` does not set `panic = "abort"`. The backend reports it as any other
  exit code.
- Signals, without `--board`: none handled; `main` and `run` install no handler. A signal ends
  the process with its default action, wherever the run is, with no stop line and none of the
  codes above: `Popen.returncode` is minus the signal's number (`-15` for `SIGTERM`).
- Signals, with `--board`: once connected, `run_with_board` registers, for `SIGTERM` and
  `SIGINT`, signal-hook's `flag::register_conditional_shutdown` with status `128 + N` and then
  `flag::register` on one `AtomicBool`. The first signal only sets the flag, which the run loop
  reads before each slice and `SocketLink` after each blocked read or write of at most 20 ms;
  the run then ends as [the protocol, section 9](../protocol.md#9-ending-a-run) says, with
  `stats` and exit 0. A second one finds the flag set and exits the process at once with
  `128 + N`, without `stats`. Before the connection, signals keep their default action. The
  handlers are registered before the session sends its `hello`, so `Esp32Sim.close`, which starts
  the runner with `--board`, waits for that `hello` before its `SIGTERM`, sends one only, and
  passes exit code 0 alone ([esp32sim-backend-technical.md](esp32sim-backend-technical.md)).

The flag names copy esp32sim's, so that the change to `esp32sim --board socket:` once
mikroverk/esp32sim#325 lands is a rename ([plan, 4.2](../implementation-plan.md#42-runner-crate)).

## Configuration

| Name | Read by | Effect |
|---|---|---|
| `ESP32SIM_ROM_DIR` | `find_rom` | first place to look for `esp32_rev300_rom.elf` when `--rom` is absent |
| `HOME` | `find_rom` | `$HOME/.espressif/tools/esp-rom-elfs/*/` is the second place |
| `ESP_EMU_DEBUG`, `ESP_EMU_DEBUG_<AREA>`, `ESP_EMU_RT_LOG`, `ESP_EMU_LOG_ALL` | `esp32::machine`, through esp-soc's `DebugFlags::from_env` (`esp-soc/src/debug.rs`) | esp32sim's debug output on stderr |

`$IDF_TOOLS_PATH` is not read: `scripts/fetch-rom-elf` honours it, `find_rom` does not.

The socket's limits are constants, not options:

| Constant | Value | Effect |
|---|---|---|
| `transport::POLL` | 20 ms | longest a read or write blocks before the signal flag and the time limits are looked at |
| `transport::WRITE_BUFFER` | 64 KiB | queued output at which a `send` writes out |
| `transport::LIVE` | 20 ms (wall) | how long a queued line waits before `idle` writes it out between slices |
| `transport::TEARDOWN` | 2 s (wall) | how long the last messages of a run may take to write |
| `protocol::MAX_LINE` | 1 MiB | the longest line from the peer; a longer one is a protocol error |
| `session::MAX_DATA` | 48 KiB | the most bytes one `uart_tx` or `console` message carries |
| `session::RX_FIFO` | 128 | the classic RX FIFO's size, against which `rxfifo_ovf` is counted |

**The SD card's image** comes from `esp-periph`, the esp32sim crate of the peripheral models,
pinned like the others: its `SdStore` trait and `FileStore`, the store of esp32sim's own
`--sd-rw`; the unit tests use its `MemStore`.

**Dependencies besides esp32sim.** `signal-hook` 0.4 (`default-features = false`) for the signal
handlers: std cannot install one. JSON goes through esp-soc's own parser (`esp_soc::json`, which
its Cooja link uses) and `esp_soc::web::json_escape`, so the crate adds no JSON crate.

**The esp32sim pin.** esp32sim is not on crates.io, so `runner/Cargo.toml` takes `esp32` and
`esp-soc` (and `emu-core`, a dev-dependency for `Stop::CostModelLifecycle` in a test) from
`https://github.com/mikroverk/esp32sim`, with `esp-periph` for the SD card's image store, each at
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
| `unknown argument {flag:?}` | anything not in the usage, `--flash-mb=8` included |
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
| `--console socket and both send the console to the peer: they need --board` | `--console socket` or `both` without `--board` |
| `--board {spec}: expected socket:PATH or tcp:HOST:PORT` | neither prefix (`--board none`), an empty path, an empty host, or a port that is not a number from 0 to 65535 |
| `--peer-timeout: expected a number of seconds, 0 for none, got {v:?}` | not a number, negative, `inf` or `NaN`, too large for a `Duration` (`1e20`), or above 0 but rounding to 0 ns (`1e-10`) |
| `--sd-rw needs --sd-spi: the card's pins`, `--sd-spi needs --sd-rw: the card's image` | one of the two without the other |
| `--sd-spi {spec}: expected sclk=N,mosi=N,miso=N,cs=N` | a part without `=`, or one of the four missing |
| `--sd-spi {spec}: {name:?} is not sclk, mosi, miso or cs` | another name |
| `--sd-spi {spec}: {pin:?} is not a GPIO of the ESP32` | not a number from 0 to 39, or 24, or 28 to 31 |
| `--sd-spi {spec}: GPIO {pin} is input only` | `sclk`, `mosi` or `cs` on 34 to 39 |
| `--sd-spi {spec}: {name} is given twice` | a name repeated |
| `--sd-spi {spec}: the four pins must differ` | two names on one GPIO |

Inputs that cannot be loaded exit 2 through `fail`, with no second line and no stop line:

| Message | Cause |
|---|---|
| `no esp32_rev300_rom.elf found: pass --rom, or set ESP32SIM_ROM_DIR` | no `--rom` and `find_rom` found nothing |
| `{path}: {error}` | the ROM or a flash file cannot be read; `error` is the OS error, e.g. `No such file or directory (os error 2)` |
| `{rom}: {error}` | esp32sim cannot load the ROM ELF, e.g. `not an ELF file`, `need ELF32 little-endian` (`esp-soc/src/elf.rs`) |
| `--flash-at {offset:#x}={path}: flash image too large` | the file does not fit in `--flash-mb` from its offset (`esp32/src/bus.rs`, `write_flash`) |
| `--sd-rw {image}: not a UTF-8 path` | esp32sim's `FileStore` takes a `&str` |
| `--sd-rw {image}: {error}` | the image cannot be opened for reading and writing, e.g. `No such file or directory (os error 2)` |
| `--sd-rw {image}: an SD card image is a whole number of 512 KiB units (CSD C_SIZE), this one has {len} bytes` | an empty image, or one whose size is not a multiple of 512 KiB |
| `--sd-rw {image}: an SD card image is at most 2 TiB (CSD C_SIZE)` | above 2^22 units |
| `classic PSRAM supports 0 or 4194304 bytes` | esp32sim's own check (`esp32/src/psram.rs`); the CLI refuses those sizes first, so it is not reached |

`cannot handle signal {sig}: {error}` also exits 2 through `fail`, if signal-hook cannot install
a handler after the connection.

Exit 1: the stop line, then `the emulator stopped before --max-seconds: {stop:?}`, for every
`Stop` but `Halted` and `SwReset`.

With `--board`, the `end` line's `{cause}` names how the run ended. Protocol errors and a vanished
peer come from `session.rs` and `transport.rs`:

| Exit | `{cause}` | When |
|---|---|---|
| 0 | `max_seconds` | the run reached `--max-seconds` |
| 0 | `stop from the peer` | the peer's turn ended with `stop` |
| 0 | `signal` | the first `SIGTERM` or `SIGINT` |
| 1 | `the emulator stopped before --max-seconds: {stop:?}` | any `Stop` but `Halted` and `SwReset` |
| 3 | `protocol error by the peer at {board}: {what}: {line}` | `{what}` is one of: `version {v}, the runner speaks version 1`, `chip {chip:?}, the runner runs "esp32"`, `watch holds GPIO {pin}, which the esp32 does not have` (any pin but 0 to 39 without 24 and 28 to 31, `session::GPIOS`), `expected hello`, `hello after the handshake`, a parse error of `protocol::parse` (`not JSON: …`, `not a JSON object`, `no "type"`, `unknown type …`, `{type}: "{field}" is missing`, `… is not an integer`, `… is negative`, `… is not a GPIO number`, `uart_rx: bad base64 character …`, and the like), `t {t} is before this turn's sync at {b} (rule 2)`, `GPIO {pin} is not in the peer's watch`, `a baud of 0`, `ack {ack}, but the last event before this sync was seq {seq}`, `quiet_until {q} is not after this turn's sync at {b}`, `adc is not implemented yet`. `{line}` is the peer's line, cut after 200 characters with `...` |
| 3 | `protocol error by the peer at {board}: a line longer than 1 MiB` or `…: a line that is not UTF-8` | from the socket, without the line |
| 4 | `the peer at {board} is gone: {why}` | `{why}` is `the peer closed the socket`, `read failed: {error}`, `write failed: {error}`, `the socket accepts no more data`, `the peer's turn took longer than --peer-timeout`, `the peer read nothing for longer than --peer-timeout` or, while the last messages are written, `the peer did not read the last messages in time` |

A failure to write the last messages does not change the exit code: the first cause decides it.

## Tests

`make test-runner` runs the Rust tests (`cargo test --locked`), 80 of them:

- `runner/src/cli/tests.rs`: `cli::parse` only. `minimal_command_line` checks the defaults
  (`rom: None`, flash 4 MiB, PSRAM 0, console `Stdout`, no board, `--peer-timeout` 60 s);
  `flash_at_offsets_are_hex_with_or_without_prefix` checks hex offsets with and without `0x`,
  repeated `--flash-at` in order, a path that contains `=`, `--chip ESP32` in upper case and a
  fractional `--max-seconds`; `options` checks `--rom`, `--flash-mb`, `--psram-mb` and
  `--console both`; `boards_and_peer_timeout` checks `socket:`, `tcp:` with IPv6 and a host name,
  and `--peer-timeout` 2.5 and 0; `help` checks that `--help` returns `Error::Help`;
  `usage_errors` checks that 28 bad command lines return `Error::Usage`, among them a missing
  `--chip`, `--max-seconds` or `--flash-at`, `--max-seconds 0` and `inf`, the flash sizes 0, 3, 32
  and 17592186044416 (which would overflow `<< 20`), `--board none`, `socket:`, `tcp:host`,
  `tcp::7000` and `tcp:h:99999`, `--peer-timeout -1`, `nan`, `1e20`, `inf` and `1e-10`, and
  `--console socket` and `both` without `--board`.
  `an_sd_card_takes_its_image_and_its_pins_in_any_order` checks `--sd-rw` with `--sd-spi` in
  either order and an input-only MISO; `sd_card_usage_errors` checks the text of each `--sd-rw`
  and `--sd-spi` usage error.
- `runner/src/tests.rs`: `stop_names_are_the_variant_names` builds every `Stop` variant and
  checks that `stop_name` is the start of its `Debug` text, followed by nothing, a space or `(`,
  so that `CostModelLifecycle` reported as `CostModel` fails.
- `runner/src/protocol/tests.rs`: time conversion in both directions and past 76 emulated
  seconds, character times, base64 against RFC 4648's vectors and malformed input, parsing every
  message the peer may send and refusing malformed ones, the exact text of the runner's messages,
  and `the_esp32sim_revision_is_the_one_cargo_builds`, which checks that the revision `hello`
  reports is the one in `runner/Cargo.toml`.
- `runner/src/sd_card/tests.rs`: `SdCard` through `transfer`, against a host in the test that
  sends what ESP-IDF's `sdspi` sends: the image size rule, CRC7 and CRC16 against the SD
  specification's examples, a card not selected or not clocked, a MISO routed elsewhere, the
  initialisation (CMD0, CMD8, ACMD41 idle once, the OCR before and after) and a host without HCS
  or CMD8, the CRC checks before and after CMD59, the CSD, CID, SCR and SD status, a single and a
  multiple block read ended by CMD12 as `sdspi` reads it, a read past the end, a single block
  write with a bad and a good CRC, a multiple block write with its stop token and ACMD22, an
  erase, and the state a chip reset finds.
- `runner/src/socket_board/tests.rs`: what `SocketBoard` records: every UART byte, and only the
  GPIO changes of watched pins, enable-only changes included; and the peer's levels and releases
  it hands the chip, in order, with `input_levels` after them.
- `runner/src/transport/tests.rs`: lines both ways over a socket pair, a line over 1 MiB, the
  `--peer-timeout` on a read and on a write the peer does not read, a signal ending a blocked
  read, a write cut short by a signal that goes on once `closing` is called, the teardown limit,
  `stats` still read to end of file after a close that leaves part of a turn unread,
  a line written out by `idle` after `LIVE`, and TCP.
- `runner/src/session/tests.rs`: the run loop against `FakeGuest`, a scripted guest that can
  return a few cycles past a slice's end as `run` does below 240 MHz, and `FakePeer`, which checks
  that `seq` grows by one and `t` never decreases. It covers turn 0 before the first instruction,
  the handshake errors, pacing and the busy line, the block point and its `latency` bound mid
  slice and with the guest idle, a late answer, events held after the block point, a block point
  already behind the guest, coalescing and its ends, the console, resets and the reset routes,
  each kind of `warn`, the protocol errors of a turn, each end of a run and its `stats`,
  `latency` 0, `gpio_out` for watched pins as an event, `gpio_in` and `release` queued and applied
  at their time in arrival order, a `watch` with a pin the chip does not have refused at the
  handshake, `input_on_output`, and the
  `gpio_out` after a reset, also for a pad a peripheral drove high with `GPIO_OUT` at 0.
  `on_the_emulator_the_slices_stop_a_guest_asleep_in_waiti_at_the_block_point` runs the real
  `esp32::Machine` on a few instructions in IRAM that write one byte to UART1 and sleep in
  `waiti`, at the 40 MHz the core starts at;
  `on_the_emulator_gpio_in_reaches_the_guest_and_its_output_comes_back_as_gpio_out` runs a loop
  that copies GPIO 33, which the fake peer drives, to GPIO 4, and checks the two `gpio_out`s;
  `on_the_emulator_no_pad_is_driven_after_a_chip_reset` drives GPIO 4 as a plain GPIO and GPIO 26
  from UART1's TX signal, and checks that after `reboot()` no output signal drives any pad and
  `GPIO_ENABLE` and `GPIO_OUT` are 0, which `reset_gpio` relies on.

`tests/test_runner_link.py` runs the real binary on `hello_world` with `--board socket:`, against
a minimal peer in Python: a run to `--max-seconds` with `--console socket` (the console on the
socket and not on stdout, `stats`, the `end` line), `SIGTERM` while the runner waits for the
peer (exit 0 with `stats` within 2 s), a late input and a version mismatch (exit 3 with
`stats`), a peer that does not answer within `--peer-timeout 0.2` (exit 4, nothing more), and no
peer listening (exit 4).

Not covered: the text of the usage messages but those of the SD card (`usage_errors` checks only
the error kind),
`setup`, `find_rom`, the `SwReset` path and an exit 1 of the real runner, a second signal,
`--board tcp:` and `--console both` on the real binary, GPIO on a real image (no validation image
uses a pin yet), and on the real machine a whole run through a reset: the `gpio_out` after it and
the peer's levels kept across it. Without `--board`, the pytest suite runs
the real binary on `hello_world` (`tests/test_hello_world.py`, and
`test_hello_world_fails_on_an_image_that_prints_something_else` in `tests/test_pytest_plugin.py`),
which shows that a run boots and prints, but checks no runner line and no exit code;
`tests/test_pytest_plugin.py::test_a_runner_that_exits_with_an_error_fails_the_test` checks how
the backend handles an exit 1, from a stand-in runner. `tests/test_sdspi.py` runs the real binary
with the card on ESP-IDF's `sdspi` example, through the backend: a FAT image the example mounts,
writes and reads, one without a file system, and an empty slot. It checks no runner line either;
the card's image errors and its lines on stderr are not covered.

## See also

- [runner-user.md](runner-user.md): running it by hand.
- [esp32sim-backend-technical.md](esp32sim-backend-technical.md): the command line the backend
  builds and how it reads the exit.
- [Link protocol](../protocol.md): the contract the session implements, and in section 6.1 how
  the runner meets the block point.
- [Implementation plan, 4.2 Runner crate](../implementation-plan.md#42-runner-crate).
- [CI](../cicd.md): the `runner` job.
