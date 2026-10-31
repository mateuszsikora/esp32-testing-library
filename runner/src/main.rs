//! esp32tl-runner: runs an unmodified ESP32 image in esp32sim with the esp32tl harness attached
//! (plan §4.2). For now the console only: the guest's UART0 goes to stdout, which
//! pytest-embedded's `DuplicateStdoutPopen` owns, so everything the runner says goes to stderr.
//!
//! A run the emulator ends prints one line on stderr,
//! `esp32tl-runner: stop: VARIANT at T s (W s wall, N resets)`, naming the `Stop` that ended it
//! (T: emulated seconds, six decimals; W: wall seconds, one decimal), and exits with one of these
//! codes:
//!
//! - 0: the run reached `--max-seconds` (`Stop::Halted`);
//! - 1: the emulator stopped for any other reason.
//!
//! A usage error, or an input that cannot be loaded, exits 2 before the run starts, without the
//! stop line (the code esp32sim uses for those).
//!
//! The runner installs no signal handler, so a signal ends the run where it is, without the stop
//! line, and the process dies of it: a shell sees 128 + the signal's number, Python's `Popen` its
//! negative. That is how a test run ends unless the test waited for the end of the run:
//! `Esp32Sim.close` (`esp32tl/backends/esp32sim.py`) sends a runner still running `SIGTERM` at
//! teardown, then `SIGKILL` after 5 s. Ending a run on `SIGTERM` with `stats` and exit 0 is what
//! the link protocol specifies (`docs/protocol.md`, section 9), not what this runner does.
mod cli;

use esp_soc::{Soc, SocBus, Stop};
use std::path::{Path, PathBuf};
use std::process::ExitCode;

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
    if opts.console != cli::Console::Stdout {
        return usage_error(
            "--console socket and both need --board socket:PATH, which this runner does not have yet",
        );
    }
    let mut m = match setup(&opts) {
        Ok(m) => m,
        Err(message) => return fail(&message),
    };
    let wall = std::time::Instant::now();
    let stop = run(&mut m);
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

/// The machine with its ROM and flash loaded, at the reset vector.
fn setup(opts: &cli::Opts) -> Result<esp32::Machine, String> {
    let flash_size = opts.flash_mb << 20;
    let mut m = esp32::machine(MAC, flash_size);
    // the JEDEC capacity the guest reads follows the size, as in esp32sim
    m.bus.set_flash_size(flash_size);
    if opts.psram_mb != 0 {
        m.bus.set_psram_size(opts.psram_mb << 20)?;
    }
    // esp32sim's `--board none`. The socket board takes its place in M1.
    m.bus.attach_board_devices();

    let rom_name = <esp32::Esp32 as Soc>::ROM_ELF;
    let rom = opts
        .rom
        .clone()
        .or_else(|| find_rom(rom_name))
        .ok_or_else(|| format!("no {rom_name} found: pass --rom, or set ESP32SIM_ROM_DIR"))?;
    m.load_rom(&read(&rom)?)
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
    m.console.mask = CONSOLE_UART0;
    m.max_cycles = (opts.max_seconds * <esp32::Esp32 as Soc>::CPU_HZ as f64) as u64;
    Ok(m)
}

fn read(path: &Path) -> Result<Vec<u8>, String> {
    std::fs::read(path).map_err(|e| format!("{}: {e}", path.display()))
}

/// esp32sim's ROM lookup: `$ESP32SIM_ROM_DIR`, then `~/.espressif/tools/esp-rom-elfs/*/`, newest
/// release first. Kept as esp32sim has it, for runs by hand. The plan pins release `20241011`
/// (M0), which this lookup does not prefer, so the esp32sim backend (#9) must pass `--rom`.
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
fn stop_name(stop: &Stop) -> &'static str {
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
