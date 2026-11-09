//! The command line. Flag names and value formats copy esp32sim's, so that switching to
//! `esp32sim --board socket:PATH` once mikroverk/esp32sim#325 lands is a rename (plan §4.2).
use crate::sd_card::Wiring;
use esp_soc::wifi::{ApConfig, BeaconAp};
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
  --sd-spi sclk=N,mosi=N,miso=N,cs=N   an SD card slot on these GPIOs, in SPI mode; empty
                        unless --sd-rw puts a card in it, and the peer's sd_card can
                        put one in or take it out while the guest runs
  --sd-rw IMAGE         an SD card holding IMAGE, a whole number of 512 KiB units, in the
                        slot of --sd-spi from power-on; the guest's writes go into the file
  --wifi ssid=…,psk=…,chan=…,bssid=…   esp32sim's virtual access point, every key
                        optional; the network behind it is the peer's, over the
                        socket (eth_tx, eth_rx): needs --board
  --wifi-nearby ssid=…,chan=…,rssi=…,secure=0|1,bssid=…   a network around the
                        station that a scan lists and nothing can join; repeatable,
                        needs --wifi
  --realtime            pace the run to wall time, for a real device at the peer's end
                        of a route (docs/protocol.md, section 12): needs --board
  --flash-out PATH      write the whole flash to PATH when the peer's flash_dump asks for
                        it, and at the end of the run (docs/protocol.md, section 5.21)
  --bt ELF              the guest's Bluetooth controller is the peer's, over the socket
                        (hci_tx, hci_rx): esp32sim's VHCI hooks take their symbols from
                        ELF, the application's; needs --board
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
    /// `--sd-spi`, and the image of `--sd-rw`.
    pub sd_card: Option<(Option<PathBuf>, Wiring)>,
    /// `--wifi` and `--wifi-nearby`.
    pub wifi: Option<Wifi>,
    /// `--realtime`.
    pub realtime: bool,
    /// `--flash-out`.
    pub flash_out: Option<PathBuf>,
    /// `--bt`: the application's ELF, whose symbols esp32sim's VHCI hooks take.
    pub bt: Option<PathBuf>,
}

/// esp32sim's virtual access point (`--wifi`), as esp32sim parses it, and the networks around
/// the station (`--wifi-nearby`). The spec is kept as given: `ApConfig` has no `PartialEq`, and
/// the run loop makes a fresh access point from it each time the peer puts it back on the air.
#[derive(Clone, Debug, PartialEq)]
pub struct Wifi {
    pub spec: String,
    pub nearby: Vec<BeaconAp>,
}

impl Wifi {
    /// The access point's configuration; the spec was checked when it was parsed.
    pub fn access_point(&self) -> ApConfig {
        ApConfig::parse(&self.spec).expect("checked by cli::parse")
    }
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
        sd_card: None,
        wifi: None,
        realtime: false,
        flash_out: None,
        bt: None,
    };
    let mut sd_image = None;
    let mut sd_wiring = None;
    let mut wifi = None;
    let mut nearby = Vec::new();
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
            "--sd-rw" => sd_image = Some(PathBuf::from(value()?)),
            "--sd-spi" => sd_wiring = Some(wiring(&value()?)?),
            "--wifi" => {
                let spec = value()?;
                access_point(&spec)?;
                wifi = Some(spec);
            }
            "--wifi-nearby" => nearby.push(value()?),
            "--realtime" => opts.realtime = true,
            "--flash-out" => opts.flash_out = Some(PathBuf::from(value()?)),
            "--bt" => opts.bt = Some(PathBuf::from(value()?)),
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
    opts.sd_card = match (sd_image, sd_wiring) {
        (image, Some(wiring)) => Some((image, wiring)),
        (None, None) => None,
        (Some(_), None) => return Err(usage("--sd-rw needs --sd-spi: the card's pins")),
    };
    opts.wifi = match wifi {
        Some(spec) => {
            if opts.board.is_none() {
                return Err(usage(
                    "--wifi puts the peer's network behind the access point: it needs --board",
                ));
            }
            let nearby =
                BeaconAp::parse_all(&nearby).map_err(|e| usage(format!("--wifi-nearby: {e}")))?;
            let ap = access_point(&spec)?;
            // esp32sim leaves such a network out with a line on stderr (`set_beacons`); here the
            // harness asked for it, so it is the harness's mistake
            if let Some(b) = nearby
                .iter()
                .find(|b| b.ssid == ap.ssid || b.bssid == ap.bssid)
            {
                return Err(usage(format!(
                    "--wifi-nearby ssid={}: a network around with the access point's {} would be \
                     the access point",
                    b.ssid,
                    if b.ssid == ap.ssid { "SSID" } else { "BSSID" }
                )));
            }
            Some(Wifi { spec, nearby })
        }
        None if !nearby.is_empty() => {
            return Err(usage("--wifi-nearby needs the access point of --wifi"));
        }
        None => None,
    };
    if opts.bt.is_some() && opts.board.is_none() {
        return Err(usage(
            "--bt makes the peer the guest's Bluetooth controller: it needs --board",
        ));
    }
    if opts.realtime && opts.board.is_none() {
        return Err(usage(
            "--realtime paces a run for the peer's real device: it needs --board",
        ));
    }
    if opts.console != Console::Stdout && opts.board.is_none() {
        return Err(usage(
            "--console socket and both send the console to the peer: they need --board",
        ));
    }
    Ok(opts)
}

/// `--wifi`'s spec, as esp32sim reads it, without `epoch`: the network behind the access point is
/// the peer's, and so is the time it tells.
fn access_point(spec: &str) -> Result<ApConfig, Error> {
    let ap = ApConfig::parse(spec).map_err(|e| usage(format!("--wifi: {e}")))?;
    if ap.epoch_s.is_some() {
        return Err(usage(
            "--wifi: epoch sets esp32sim's own network's clock; the network here is the peer's",
        ));
    }
    Ok(ap)
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

/// `sclk=N,mosi=N,miso=N,cs=N`, in any order: four distinct GPIOs of the classic chip, those the
/// chip drives (all but MISO) not input-only.
fn wiring(spec: &str) -> Result<Wiring, Error> {
    let bad = |why: String| usage(format!("--sd-spi {spec}: {why}"));
    let mut pins: [Option<u8>; 4] = [None; 4];
    for part in spec.split(',') {
        let (name, pin) = part
            .split_once('=')
            .ok_or_else(|| bad("expected sclk=N,mosi=N,miso=N,cs=N".into()))?;
        let at = ["sclk", "mosi", "miso", "cs"]
            .iter()
            .position(|&n| n == name)
            .ok_or_else(|| bad(format!("{name:?} is not sclk, mosi, miso or cs")))?;
        let pin = pin
            .parse::<u8>()
            .ok()
            .filter(|&p| p < 40 && !matches!(p, 24 | 28..=31))
            .ok_or_else(|| bad(format!("{pin:?} is not a GPIO of the ESP32")))?;
        if name != "miso" && pin >= 34 {
            return Err(bad(format!("GPIO {pin} is input only")));
        }
        if pins[at].replace(pin).is_some() {
            return Err(bad(format!("{name} is given twice")));
        }
    }
    let [Some(sclk), Some(mosi), Some(miso), Some(cs)] = pins else {
        return Err(bad("expected sclk=N,mosi=N,miso=N,cs=N".into()));
    };
    let mut seen = [sclk, mosi, miso, cs];
    seen.sort_unstable();
    if seen.windows(2).any(|w| w[0] == w[1]) {
        return Err(bad("the four pins must differ".into()));
    }
    Ok(Wiring {
        sclk,
        mosi,
        miso,
        cs,
    })
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
