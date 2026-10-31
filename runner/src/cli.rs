//! The command line. Flag names and value formats copy esp32sim's, so that switching to
//! `esp32sim --board socket:PATH` once mikroverk/esp32sim#325 lands is a rename (plan §4.2).
use std::path::PathBuf;
use std::time::Duration;

pub const USAGE: &str = "\
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
";

/// Where the peer is (protocol §1).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Board {
    Socket(PathBuf),
    /// `HOST:PORT`, as `TcpStream::connect` takes it.
    Tcp(String),
}

impl std::fmt::Display for Board {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Board::Socket(path) => write!(f, "socket:{}", path.display()),
            Board::Tcp(address) => write!(f, "tcp:{address}"),
        }
    }
}

/// Where the guest's UART0 console goes.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Console {
    /// To stdout only, where pytest-embedded's `DuplicateStdoutPopen` reads it.
    Stdout,
    /// As `console` messages on the board socket only.
    Socket,
    /// Both.
    Both,
}

#[derive(Debug, PartialEq)]
pub struct Opts {
    pub rom: Option<PathBuf>,
    pub flash_at: Vec<(usize, PathBuf)>,
    pub flash_mb: usize,
    pub psram_mb: usize,
    pub max_seconds: f64,
    pub console: Console,
    pub board: Option<Board>,
    /// `None`: wait for ever.
    pub peer_timeout: Option<Duration>,
}

#[derive(Debug, PartialEq)]
pub enum Error {
    /// `-h` or `--help`: print the usage and exit 0.
    Help,
    Usage(String),
}

fn usage(message: impl Into<String>) -> Error {
    Error::Usage(message.into())
}

/// The arguments after the program name.
pub fn parse(args: impl IntoIterator<Item = String>) -> Result<Opts, Error> {
    let mut args = args.into_iter();
    let mut chip = None;
    let mut max_seconds = None;
    let mut opts = Opts {
        rom: None,
        flash_at: Vec::new(),
        flash_mb: 4,
        psram_mb: 0,
        max_seconds: 0.0,
        console: Console::Stdout,
        board: None,
        peer_timeout: Some(Duration::from_secs(60)),
    };
    while let Some(flag) = args.next() {
        let mut value = || {
            args.next()
                .ok_or_else(|| usage(format!("{flag} needs a value")))
        };
        match flag.as_str() {
            "--chip" => chip = Some(value()?.to_ascii_lowercase()),
            "--flash-at" => opts.flash_at.push(flash_at(&value()?)?),
            "--max-seconds" => {
                let v = value()?;
                match v.parse::<f64>() {
                    Ok(s) if s.is_finite() && s > 0.0 => max_seconds = Some(s),
                    _ => {
                        return Err(usage(format!(
                            "--max-seconds: expected a positive number of seconds, got {v:?}"
                        )));
                    }
                }
            }
            "--rom" => opts.rom = Some(PathBuf::from(value()?)),
            "--flash-mb" => {
                let v = value()?;
                // The guest reads the capacity as a power of two (esp32sim's JEDEC ID), so any
                // other size boots into a flash-size mismatch instead of failing here.
                opts.flash_mb = match v.parse::<usize>() {
                    Ok(mb @ (1 | 2 | 4 | 8 | 16)) => mb,
                    _ => {
                        return Err(usage(format!(
                            "--flash-mb: the classic ESP32 takes 1, 2, 4, 8 or 16 MiB, got {v:?}"
                        )));
                    }
                };
            }
            "--psram-mb" => {
                let v = value()?;
                opts.psram_mb = match v.as_str() {
                    "0" => 0,
                    "4" => 4,
                    _ => {
                        return Err(usage(format!(
                            "--psram-mb: the classic ESP32 supports only 0 or 4 MiB, got {v:?}"
                        )));
                    }
                };
            }
            "--console" => {
                let v = value()?;
                opts.console = match v.as_str() {
                    "stdout" => Console::Stdout,
                    "socket" => Console::Socket,
                    "both" => Console::Both,
                    _ => {
                        return Err(usage(format!(
                            "--console: expected stdout, socket or both, got {v:?}"
                        )));
                    }
                };
            }
            "--board" => opts.board = Some(board(&value()?)?),
            "--peer-timeout" => {
                let v = value()?;
                // negative, not finite, too large for a Duration, or so small it rounds to 0,
                // which would mean the opposite: a usage error
                let seconds = v.parse::<f64>().ok();
                opts.peer_timeout = match seconds.map(|s| (s, Duration::try_from_secs_f64(s))) {
                    Some((0.0, _)) => None,
                    Some((_, Ok(timeout))) if timeout > Duration::ZERO => Some(timeout),
                    _ => {
                        return Err(usage(format!(
                            "--peer-timeout: expected a number of seconds, 0 for none, got {v:?}"
                        )));
                    }
                };
            }
            "-h" | "--help" => return Err(Error::Help),
            _ => return Err(usage(format!("unknown argument {flag:?}"))),
        }
    }
    match chip.as_deref() {
        Some("esp32") => {}
        Some(c) => return Err(usage(format!("--chip {c}: only esp32 is supported"))),
        None => return Err(usage("--chip is required (esp32)")),
    }
    // esp32sim also requires it in practice ("always give one: the run returns"); here the end
    // of the run is what a test waits for.
    opts.max_seconds = max_seconds.ok_or_else(|| usage("--max-seconds is required"))?;
    if opts.flash_at.is_empty() {
        return Err(usage(
            "nothing to run: give the images with --flash-at OFFSET=FILE",
        ));
    }
    if opts.console != Console::Stdout && opts.board.is_none() {
        return Err(usage(
            "--console socket and both send the console to the peer: they need --board",
        ));
    }
    Ok(opts)
}

/// `socket:PATH` or `tcp:HOST:PORT`.
fn board(spec: &str) -> Result<Board, Error> {
    let bad = || {
        usage(format!(
            "--board {spec}: expected socket:PATH or tcp:HOST:PORT"
        ))
    };
    if let Some(path) = spec.strip_prefix("socket:") {
        return if path.is_empty() {
            Err(bad())
        } else {
            Ok(Board::Socket(PathBuf::from(path)))
        };
    }
    let address = spec.strip_prefix("tcp:").ok_or_else(bad)?;
    match address.rsplit_once(':') {
        Some((host, port)) if !host.is_empty() && port.parse::<u16>().is_ok() => {
            Ok(Board::Tcp(address.to_string()))
        }
        _ => Err(bad()),
    }
}

/// `OFFSET=FILE`, the offset in hex with or without `0x`, as esp32sim reads it.
fn flash_at(spec: &str) -> Result<(usize, PathBuf), Error> {
    let (offset, path) = spec
        .split_once('=')
        .ok_or_else(|| usage(format!("--flash-at {spec}: expected OFFSET=FILE")))?;
    let offset = usize::from_str_radix(offset.trim_start_matches("0x"), 16)
        .map_err(|_| usage(format!("--flash-at {spec}: bad hex offset {offset:?}")))?;
    if path.is_empty() {
        return Err(usage(format!("--flash-at {spec}: expected OFFSET=FILE")));
    }
    Ok((offset, PathBuf::from(path)))
}

// In a file of its own, so that `make docs-check` tells a test-only change by its path.
#[cfg(test)]
mod tests;
