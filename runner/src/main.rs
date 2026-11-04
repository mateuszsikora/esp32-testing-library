//! esp32tl-runner: runs an unmodified ESP32 image in esp32sim with the esp32tl harness attached
//! (plan §4.2). The guest's UART0 goes to stdout, and only it, so that whoever reads stdout gets
//! the guest's output as the guest wrote it; everything the runner says goes to stderr. Under the
//! backend the two meet again: pytest-embedded's `DuplicateStdoutPopen` writes both into one log.
//! With `--board socket:PATH` or `tcp:HOST:PORT` the chip's pins are wired to a peer on that
//! socket, which speaks the link protocol of `docs/protocol.md`; `--console socket` or `both`
//! sends the console to the peer as well, and `socket` keeps it off stdout.
//!
//! Without `--board`, a run the emulator ends prints one line on stderr,
//! `esp32tl-runner: stop: VARIANT at T s (W s wall, N resets)`, naming the `Stop` that ended it
//! (T: emulated seconds, six decimals; W: wall seconds, one decimal). With `--board`, every end of
//! a run that starts, a failed connection included, prints
//! `esp32tl-runner: end: CAUSE at T s (W s wall, N resets, K turns)` instead. The exit codes
//! (protocol §9):
//!
//! - 0: the run reached `--max-seconds` (`Stop::Halted`); with `--board` also a `stop` from the
//!   peer, or `SIGTERM` or `SIGINT`;
//! - 1: the emulator stopped for any other reason; without `--board` a second line gives the stop
//!   with its fields, with it the end line does;
//! - 3: a protocol error by the peer;
//! - 4: the peer is gone, or never answered the connection.
//!
//! A usage error, or an input that cannot be loaded, exits 2 before the run starts, without the
//! stop line. esp32sim exits 2 on a usage error too, but not on every bad input: it panics on a
//! ROM ELF it cannot load.
//!
//! A panic, in the runner or in esp32sim during the run, exits 101 with Rust's panic message and
//! no stop line.
//!
//! Without `--board` the runner installs no signal handler, so a signal ends the run where it is,
//! without the stop line, and the process dies of it: a shell sees 128 + the signal's number,
//! Python's `Popen` its negative. That is how a test run ends unless the test waited for the end
//! of the run: `Esp32Sim.close` (`esp32tl/backends/esp32sim.py`) sends a runner still running
//! `SIGTERM` at teardown, then `SIGKILL` after 5 s. With `--board`, once the connection is made,
//! `SIGTERM` and `SIGINT` end the run with `stats` and exit 0 (protocol §9), and a second one ends
//! the process at once, with 128 + the signal's number.
mod cli;
mod protocol;
mod sd_card;
mod session;
mod socket_board;
mod transport;

use esp_soc::{Soc, SocBus, Stop};
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::sync::Arc;
use std::sync::atomic::AtomicBool;

/// The MAC esp32sim gives the classic chip, so a run here and the same run in esp32sim agree.
const MAC: [u8; 6] = [0x24, 0x6f, 0x28, 0x00, 0x11, 0x22];

/// `Console::mask` bit for UART0.
const CONSOLE_UART0: u32 = 2;

fn main() -> ExitCode {
    let opts = match cli::parse(std::env::args().skip(1)) {
        Ok(opts) => opts,
        Err(cli::Error::Help) => {
            eprint!("{}", cli::USAGE);
            return ExitCode::SUCCESS;
        }
        Err(cli::Error::Usage(message)) => return usage_error(&message),
    };
    let shared = opts.board.as_ref().map(|_| socket_board::Shared::default());
    let card = match opts
        .sd_card
        .as_ref()
        .map(|(image, wiring)| slot(image.as_deref(), *wiring))
    {
        Some(Ok(card)) => Some(card),
        Some(Err(message)) => return fail(&message),
        None => None,
    };
    let (mut m, rom) = match setup(&opts, shared.clone(), card.clone()) {
        Ok(loaded) => loaded,
        Err(message) => return fail(&message),
    };
    if let (Some(board), Some(shared)) = (&opts.board, shared) {
        return run_with_board(&opts, board, shared, card, &mut m, &rom);
    }
    let wall = std::time::Instant::now();
    let stop = run(&mut m);
    report_card(card.as_ref());
    eprintln!(
        "esp32tl-runner: stop: {} at {:.6} s ({:.1} s wall, {} resets)",
        stop_name(&stop),
        m.seconds(),
        wall.elapsed().as_secs_f64(),
        m.reboots
    );
    match stop {
        Stop::Halted => ExitCode::SUCCESS,
        stop => {
            eprintln!("esp32tl-runner: the emulator stopped before --max-seconds: {stop:?}");
            ExitCode::from(1)
        }
    }
}

fn usage_error(message: &str) -> ExitCode {
    eprintln!("esp32tl-runner: {message}");
    eprintln!("run esp32tl-runner --help for the usage");
    ExitCode::from(2)
}

fn fail(message: &str) -> ExitCode {
    eprintln!("esp32tl-runner: {message}");
    ExitCode::from(2)
}

/// The run with a peer on the socket: connect, then the session of `docs/protocol.md`.
fn run_with_board(
    opts: &cli::Opts,
    board: &cli::Board,
    shared: socket_board::Shared,
    card: Option<sd_card::Shared>,
    m: &mut esp32::Machine,
    rom: &[u8],
) -> ExitCode {
    let signal = Arc::new(AtomicBool::new(false));
    let link = match board {
        cli::Board::Socket(path) => transport::SocketLink::unix(path, signal.clone()),
        cli::Board::Tcp(address) => {
            transport::SocketLink::tcp(address, opts.peer_timeout, signal.clone())
        }
    };
    let mut link = match link {
        Ok(link) => link,
        Err(e) => {
            eprintln!("esp32tl-runner: end: cannot connect to the peer at {board}: {e}");
            return ExitCode::from(4);
        }
    };
    // From here SIGTERM and SIGINT end the run with `stats` (protocol §9): the handlers only set
    // the flag, which the run loop reads between slices and the socket between short blocking
    // waits. Until the connection is made, they kill the process as usual. A second signal kills
    // it all the same, with 128 + the signal's number, as a shell reports it.
    for sig in [signal_hook::consts::SIGTERM, signal_hook::consts::SIGINT] {
        let registered =
            signal_hook::flag::register_conditional_shutdown(sig, 128 + sig, signal.clone())
                .and_then(|_| signal_hook::flag::register(sig, signal.clone()));
        if let Err(e) = registered {
            return fail(&format!("cannot handle signal {sig}: {e}"));
        }
    }
    link.set_write_timeout(opts.peer_timeout);
    let wall = std::time::Instant::now();
    let session = session::Session::new(
        m,
        &mut link,
        shared,
        session::Options {
            end_cycle: end_cycle(opts),
            console: opts.console != cli::Console::Stdout,
            peer_timeout: opts.peer_timeout,
            signal: &signal,
            wifi: opts.wifi.as_ref(),
            rom,
            card: card.clone(),
        },
    );
    let (end, stats) = session.run();
    // whatever the guest printed before the end comes out before what the runner says
    m.drain_console();
    report_card(card.as_ref());
    let cause = match &end {
        session::End::MaxSeconds => "max_seconds".to_string(),
        session::End::Stop => "stop from the peer".to_string(),
        session::End::Signal => "signal".to_string(),
        session::End::Emulator(stop) => {
            format!("the emulator stopped before --max-seconds: {stop:?}")
        }
        session::End::Protocol(detail) => {
            format!("protocol error by the peer at {board}: {detail}")
        }
        session::End::PeerGone(detail) => format!("the peer at {board} is gone: {detail}"),
    };
    eprintln!(
        "esp32tl-runner: end: {cause} at {:.6} s ({:.1} s wall, {} resets, {} turns)",
        m.seconds(),
        wall.elapsed().as_secs_f64(),
        stats.resets,
        stats.turns
    );
    ExitCode::from(end.exit_code())
}

/// `--max-seconds` in bus cycles.
fn end_cycle(opts: &cli::Opts) -> u64 {
    (opts.max_seconds * <esp32::Esp32 as Soc>::CPU_HZ as f64) as u64
}

/// The SD card slot of `--sd-spi`, with a card writing through to `image` in it when `--sd-rw`
/// gives one.
fn slot(image: Option<&Path>, wiring: sd_card::Wiring) -> Result<sd_card::Shared, String> {
    let mut slot = sd_card::Slot::new(wiring);
    match image {
        Some(image) => {
            let name = image
                .to_str()
                .ok_or_else(|| format!("--sd-rw {}: not a UTF-8 path", image.display()))?;
            let len = slot
                .insert_file(name)
                .map_err(|e| format!("--sd-rw {name}: {e}"))?;
            eprintln!(
                "esp32tl-runner: SD card on {wiring}: {name} ({} KiB, write-through)",
                len >> 10
            );
        }
        None => eprintln!("esp32tl-runner: SD card slot on {wiring}: empty"),
    }
    Ok(std::rc::Rc::new(std::cell::RefCell::new(slot)))
}

/// What the run did with the cards in the slot, before the line that ends the run.
fn report_card(card: Option<&sd_card::Shared>) {
    let Some(card) = card else { return };
    let slot = card.borrow();
    let s = slot.stats();
    let errors = if s.errors > 0 {
        format!(", {} image errors", s.errors)
    } else {
        String::new()
    };
    let faults = if s.faults > 0 {
        format!(", {} operations faulted", s.faults)
    } else {
        String::new()
    };
    let changes = if slot.inserted + slot.removed > 0 {
        format!(
            "; while it ran, cards put in: {}, taken out: {}",
            slot.inserted, slot.removed
        )
    } else {
        String::new()
    };
    eprintln!(
        "esp32tl-runner: SD card: {} commands, {} blocks read, {} blocks written{errors}{faults}{changes}",
        s.commands, s.blocks_read, s.blocks_written
    );
}

/// The machine with its ROM and flash loaded, at the reset vector, and the ROM's ELF, which a
/// power-on reset loads again. With `shared`, the socket board is on its pins; with `card`, the
/// SD card slot, on the socket board or on a board of its own.
fn setup(
    opts: &cli::Opts,
    shared: Option<socket_board::Shared>,
    card: Option<sd_card::Shared>,
) -> Result<(esp32::Machine, Vec<u8>), String> {
    let flash_size = opts.flash_mb << 20;
    let mut m = esp32::machine(MAC, flash_size);
    // the JEDEC capacity the guest reads follows the size, as in esp32sim
    m.bus.set_flash_size(flash_size);
    if opts.psram_mb != 0 {
        m.bus.set_psram_size(opts.psram_mb << 20)?;
    }
    // without a board or a card, esp32sim's `--board none`
    match (shared, card) {
        (Some(shared), card) => {
            m.bus.board = Box::new(socket_board::SocketBoard::new(shared, card));
        }
        (None, Some(card)) => m.bus.board = Box::new(sd_card::CardBoard(card)),
        (None, None) => {}
    }
    m.bus.attach_board_devices();
    if let Some(wifi) = &opts.wifi {
        // esp32sim's access point, with the network behind it on the socket: the relay hands the
        // run loop the station's Ethernet frames instead of esp32sim's own network
        session::Guest::wifi_access_point(&mut m, wifi);
        m.bus.set_ethernet_relay(true)?;
        let ap = wifi.access_point();
        eprintln!(
            "esp32tl-runner: Wi-Fi access point '{}' bssid {} channel {} ({}), its network on the socket",
            ap.ssid,
            esp_soc::wifi::mac_str(&ap.bssid),
            ap.channel,
            if ap.psk.is_some() { "WPA2-PSK" } else { "open" }
        );
        for b in &wifi.nearby {
            eprintln!(
                "esp32tl-runner: Wi-Fi network around: '{}' bssid {} channel {} {} dBm ({})",
                b.ssid,
                esp_soc::wifi::mac_str(&b.bssid),
                b.channel,
                b.rssi,
                if b.secure { "WPA2-PSK" } else { "open" }
            );
        }
    }

    let rom_name = <esp32::Esp32 as Soc>::ROM_ELF;
    let rom = opts
        .rom
        .clone()
        .or_else(|| find_rom(rom_name))
        .ok_or_else(|| format!("no {rom_name} found: pass --rom, or set ESP32SIM_ROM_DIR"))?;
    let elf = read(&rom)?;
    m.load_rom(&elf)
        .map_err(|e| format!("{}: {e}", rom.display()))?;
    eprintln!("esp32tl-runner: ROM {}", rom.display());
    for (offset, path) in &opts.flash_at {
        let data = read(path)?;
        m.write_flash(*offset, &data)
            .map_err(|e| format!("--flash-at {offset:#x}={}: {e}", path.display()))?;
        eprintln!(
            "esp32tl-runner: flash {offset:#x}: {} ({} bytes)",
            path.display(),
            data.len()
        );
    }

    m.boot_rom();
    m.console.mask = if opts.console == cli::Console::Socket {
        0
    } else {
        CONSOLE_UART0
    };
    m.max_cycles = end_cycle(opts);
    Ok((m, elf))
}

fn read(path: &Path) -> Result<Vec<u8>, String> {
    std::fs::read(path).map_err(|e| format!("{}: {e}", path.display()))
}

/// esp32sim's ROM lookup: `$ESP32SIM_ROM_DIR`, then `~/.espressif/tools/esp-rom-elfs/*/`, newest
/// release first, for runs by hand. As esp32sim has it, except that an entry of `esp-rom-elfs/`
/// must hold the ROM as a file (esp32sim takes any path there that exists) and `HOME` need not be
/// valid UTF-8. The plan pins release `20241011` (M0), which this lookup does not prefer, so the
/// esp32sim backend (#9) must pass `--rom`.
fn find_rom(name: &str) -> Option<PathBuf> {
    if let Some(p) = std::env::var_os("ESP32SIM_ROM_DIR")
        .map(|d| PathBuf::from(d).join(name))
        .filter(|p| p.is_file())
    {
        return Some(p);
    }
    let home = std::env::var_os("HOME")?;
    let mut dirs: Vec<PathBuf> =
        std::fs::read_dir(Path::new(&home).join(".espressif/tools/esp-rom-elfs"))
            .ok()?
            .flatten()
            .map(|e| e.path())
            .collect();
    dirs.sort();
    dirs.into_iter()
        .rev()
        .map(|d| d.join(name))
        .find(|p| p.is_file())
}

/// Runs to the end: through every chip reset the firmware asks for, as the ROM would after one,
/// until `--max-seconds` or a stop of any other kind.
fn run(m: &mut esp32::Machine) -> Stop {
    loop {
        let stop = m.run(u64::MAX);
        // whatever the guest printed before the stop comes out before what the runner says
        m.drain_console();
        match stop {
            Stop::SwReset => {
                // M1 forwards the cause to the peer as `reset` (plan §1, finding 4).
                let cause = m.reboot();
                eprintln!(
                    "esp32tl-runner: chip reset at {:.6} s, cause {cause:#x}",
                    m.seconds()
                );
            }
            stop => return stop,
        }
    }
}

/// The variant's name alone, without its fields. Exhaustive, so that a variant added upstream
/// fails the build here instead of being reported under another name.
pub(crate) fn stop_name(stop: &Stop) -> &'static str {
    match stop {
        Stop::MaxInsns => "MaxInsns",
        Stop::Halted => "Halted",
        Stop::Breakpoint(..) => "Breakpoint",
        Stop::Unimplemented(..) => "Unimplemented",
        Stop::SwReset => "SwReset",
        Stop::Simcall(..) => "Simcall",
        Stop::Ebreak(..) => "Ebreak",
        Stop::Watch(..) => "Watch",
        Stop::Exceptions(..) => "Exceptions",
        Stop::CostModel { .. } => "CostModel",
        Stop::CostModelLifecycle { .. } => "CostModelLifecycle",
    }
}

// In a file of its own, so that `make docs-check` tells a test-only change by its path.
#[cfg(test)]
mod tests;
