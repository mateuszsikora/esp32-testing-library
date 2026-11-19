//! The command line. Flag names and value formats copy esp32sim's, so that switching to
//! `esp32sim --board socket:PATH` once mikroverk/esp32sim#325 lands is a rename (plan §4.2).
use crate::chip::Profile;
use crate::sd_card::Wiring;
use esp_soc::wifi::{ApConfig, BeaconAp, sta::StaConfig};
use std::path::PathBuf;
use std::time::Duration;

pub const USAGE: &str = "\
usage: esp32tl-runner --chip CHIP --flash-at OFFSET=FILE... --max-seconds S [options]

  --chip CHIP           the chip: esp32 (the classic ESP32), esp32s3, esp32c3, esp32c6,
                        esp32p4 (rev < 3 or rev >= 3, as the images are built) or
                        esp32s31
  --flash-at OFFSET=FILE   write FILE into flash at OFFSET (hex, 0x optional); repeatable
  --max-seconds S       stop after S emulated seconds: the normal end of a run
  --rom F               the mask ROM ELF; default, as esp32sim's: $ESP32SIM_ROM_DIR,
                        then ~/.espressif/tools/esp-rom-elfs/*/ (newest release first,
                        not the pinned 20260528: whoever runs the tests passes --rom)
  --flash-mb N          flash size in MiB: 1, 2, 4, 8 or 16 (default 4)
  --psram-mb N          PSRAM size in MiB (default 0): 0 or 4 on the classic ESP32,
                        0, 2, 4 or 8 on the ESP32-S3, 0, 16 or 32 on the ESP32-P4,
                        0 or 16 on the ESP32-S31, 0 on the ESP32-C3 and ESP32-C6
  --board socket:PATH   connect to the peer on the Unix socket PATH (docs/protocol.md);
  --board tcp:HOST:PORT   or over TCP. Without --board the chip has nothing on its pins
  --peer-timeout S      how long, in wall seconds, a turn of the peer may take before the
                        run fails with exit code 4 (default 60; 0 waits for ever)
  --parent-fd N         the read end of a pipe, inherited as file descriptor N, whose write
                        end the process that started the runner holds: at its end of file
                        the run ends as with the peer gone (exit code 4); needs --board
  --console stdout|socket|both   where the console goes (default stdout); socket and both
                        need --board
  --console-port uart0|usb   which of the guest's outputs is the console (default uart0):
                        UART0, or the USB-Serial/JTAG controller of the ESP32-S3,
                        ESP32-C3, ESP32-C6, ESP32-P4 and ESP32-S31, whose input is the
                        peer's console_rx; so is UART0's on the ESP32-P4 and ESP32-S31,
                        whose UARTs have no pins in esp32sim
  --sd-spi sclk=N,mosi=N,miso=N,cs=N   an SD card slot on these GPIOs, in SPI mode; empty
                        unless --sd-rw puts a card in it, and the peer's sd_card can
                        put one in or take it out while the guest runs
  --sd-rw IMAGE         an SD card holding IMAGE, a whole number of 512 KiB units, in the
                        slot of --sd-spi from power-on; the guest's writes go into the file
  --wifi ssid=…,psk=…,chan=…,bssid=…   esp32sim's virtual access point, every key
                        optional; the network behind it is the peer's, over the
                        socket (eth_tx, eth_rx): needs --board
  --wifi-sta ssid=…[,psk=…][,mac=…]   a virtual station joining the guest's
                        access point; needs --board; exclusive with --wifi and --wifi-nearby
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
  --no-bt ELF           the guest has no Bluetooth controller: its call to
                        esp_bt_controller_init, at that symbol of ELF, the application's,
                        ends the run (end bt_controller, exit code 5); needs --board,
                        exclusive with --bt
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

/// Which of the guest's outputs is the console (`--console-port`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ConsolePort {
    /// UART0, on its pins: what the socket board records of its TX pin.
    Uart0,
    /// The USB-Serial/JTAG controller: esp32sim's `UsbSerialJtag`, which has no pins.
    Usb,
}

/// Where the guest's console goes.
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
    /// `--chip`.
    pub chip: &'static Profile,
    pub rom: Option<PathBuf>,
    pub flash_at: Vec<(usize, PathBuf)>,
    pub flash_mb: usize,
    pub psram_mb: usize,
    pub max_seconds: f64,
    pub console: Console,
    /// `--console-port`.
    pub console_port: ConsolePort,
    pub board: Option<Board>,
    /// `None`: wait for ever.
    pub peer_timeout: Option<Duration>,
    /// `--parent-fd`: the descriptor whose end of file says the process that started the
    /// runner is gone.
    pub parent_fd: Option<i32>,
    /// `--sd-spi`, and the image of `--sd-rw`.
    pub sd_card: Option<(Option<PathBuf>, Wiring)>,
    /// `--wifi` with `--wifi-nearby`, or `--wifi-sta`.
    pub wifi: Option<Wifi>,
    /// `--realtime`.
    pub realtime: bool,
    /// `--flash-out`.
    pub flash_out: Option<PathBuf>,
    /// `--bt`: the application's ELF, whose symbols esp32sim's VHCI hooks take.
    pub bt: Option<PathBuf>,
    /// `--no-bt`: the application's ELF, whose `esp_bt_controller_init` ends the run.
    pub no_bt: Option<PathBuf>,
}

/// One virtual radio role: an access point with nearby beacons, or a station joining the guest.
/// Specs are checked by esp32sim at parse time and retained for reset-time reattachment.
#[derive(Clone, Debug, PartialEq)]
pub enum Wifi {
    AccessPoint { spec: String, nearby: Vec<BeaconAp> },
    Station { spec: String },
}

impl Wifi {
    pub fn station(&self) -> Option<StaConfig> {
        match self {
            Self::Station { spec } => Some(StaConfig::parse(spec).expect("checked by cli::parse")),
            Self::AccessPoint { .. } => None,
        }
    }

    pub fn nearby(&self) -> &[BeaconAp] {
        match self {
            Self::AccessPoint { nearby, .. } => nearby,
            Self::Station { .. } => &[],
        }
    }
    /// The access point's configuration; the spec was checked when it was parsed.
    pub fn access_point(&self) -> ApConfig {
        let Self::AccessPoint { spec, .. } = self else {
            panic!("station is not an access point")
        };
        ApConfig::parse(spec).expect("checked by cli::parse")
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
    let mut psram = None;
    let mut sd_spec: Option<String> = None;
    let mut max_seconds = None;
    let mut opts = Opts {
        chip: &crate::chip::esp32::PROFILE,
        rom: None,
        flash_at: Vec::new(),
        flash_mb: 4,
        psram_mb: 0,
        max_seconds: 0.0,
        console: Console::Stdout,
        console_port: ConsolePort::Uart0,
        board: None,
        peer_timeout: Some(Duration::from_secs(60)),
        parent_fd: None,
        sd_card: None,
        wifi: None,
        realtime: false,
        flash_out: None,
        bt: None,
        no_bt: None,
    };
    let mut sd_image = None;
    let mut wifi = None;
    let mut station = None;
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
                            "--flash-mb: the flash takes 1, 2, 4, 8 or 16 MiB, got {v:?}"
                        )));
                    }
                };
            }
            "--psram-mb" => psram = Some(value()?),
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
            "--console-port" => {
                let v = value()?;
                opts.console_port = match v.as_str() {
                    "uart0" => ConsolePort::Uart0,
                    "usb" => ConsolePort::Usb,
                    _ => {
                        return Err(usage(format!(
                            "--console-port: expected uart0 or usb, got {v:?}"
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
            "--parent-fd" => {
                let v = value()?;
                opts.parent_fd = match v.parse::<i32>() {
                    Ok(fd) if fd >= 0 => Some(fd),
                    _ => {
                        return Err(usage(format!(
                            "--parent-fd: expected a file descriptor, a number from 0, got {v:?}"
                        )));
                    }
                };
            }
            "--sd-rw" => sd_image = Some(PathBuf::from(value()?)),
            "--sd-spi" => sd_spec = Some(value()?),
            "--wifi" => {
                let spec = value()?;
                access_point(&spec)?;
                wifi = Some(spec);
            }
            "--wifi-sta" => {
                if station.is_some() {
                    return Err(usage(
                        "--wifi-sta is given twice: one virtual station per run",
                    ));
                }
                let spec = value()?;
                StaConfig::parse(&spec).map_err(|e| usage(format!("--wifi-sta: {e}")))?;
                station = Some(spec);
            }
            "--wifi-nearby" => nearby.push(value()?),
            "--realtime" => opts.realtime = true,
            "--flash-out" => opts.flash_out = Some(PathBuf::from(value()?)),
            "--bt" => opts.bt = Some(PathBuf::from(value()?)),
            "--no-bt" => opts.no_bt = Some(PathBuf::from(value()?)),
            "-h" | "--help" => return Err(Error::Help),
            _ => return Err(usage(format!("unknown argument {flag:?}"))),
        }
    }
    let names = || {
        crate::chip::ALL
            .iter()
            .map(|p| p.name)
            .collect::<Vec<_>>()
            .join(", ")
    };
    opts.chip = match chip.as_deref() {
        Some(c) => crate::chip::by_name(c).ok_or_else(|| {
            usage(format!(
                "--chip {}: not one of {}",
                c.to_ascii_lowercase(),
                names()
            ))
        })?,
        None => return Err(usage(format!("--chip is required ({})", names()))),
    };
    // esp32sim also requires it in practice ("always give one: the run returns"); here the end
    // of the run is what a test waits for.
    opts.max_seconds = max_seconds.ok_or_else(|| usage("--max-seconds is required"))?;
    if opts.flash_at.is_empty() {
        return Err(usage(
            "nothing to run: give the images with --flash-at OFFSET=FILE",
        ));
    }
    let sd_wiring = sd_spec
        .as_deref()
        .map(|spec| wiring(spec, opts.chip))
        .transpose()?;
    opts.sd_card = match (sd_image, sd_wiring) {
        (image, Some(wiring)) => Some((image, wiring)),
        (None, None) => None,
        (Some(_), None) => return Err(usage("--sd-rw needs --sd-spi: the card's pins")),
    };
    if station.is_some() {
        if wifi.is_some() {
            return Err(usage(
                "--wifi-sta with --wifi: a virtual access point and station at once are not modelled",
            ));
        }
        if !nearby.is_empty() {
            return Err(usage(
                "--wifi-sta with --wifi-nearby: nearby networks need a virtual access point",
            ));
        }
        if opts.board.is_none() {
            return Err(usage(
                "--wifi-sta puts the peer behind the virtual station: it needs --board",
            ));
        }
    }
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
            Some(Wifi::AccessPoint { spec, nearby })
        }
        None if !nearby.is_empty() => {
            return Err(usage("--wifi-nearby needs the access point of --wifi"));
        }
        None => station.map(|spec| Wifi::Station { spec }),
    };
    if opts.bt.is_some() && opts.board.is_none() {
        return Err(usage(
            "--bt makes the peer the guest's Bluetooth controller: it needs --board",
        ));
    }
    if opts.bt.is_some() && opts.no_bt.is_some() {
        return Err(usage(
            "--bt with --no-bt: the guest's controller is the peer's, or there is none",
        ));
    }
    if opts.no_bt.is_some() && opts.board.is_none() {
        return Err(usage(
            "--no-bt ends the run for the peer when the guest starts its Bluetooth controller: it needs --board",
        ));
    }
    if opts.realtime && opts.board.is_none() {
        return Err(usage(
            "--realtime paces a run for the peer's real device: it needs --board",
        ));
    }
    if opts.parent_fd.is_some() && opts.board.is_none() {
        return Err(usage(
            "--parent-fd ends a run with a peer when the process that started it is gone: it \
             needs --board",
        ));
    }
    if opts.console != Console::Stdout && opts.board.is_none() {
        return Err(usage(
            "--console socket and both send the console to the peer: they need --board",
        ));
    }
    if let Some(v) = psram {
        let sizes = opts.chip.psram_mb;
        opts.psram_mb = match v.parse::<usize>() {
            Ok(mb) if mb == 0 || sizes.contains(&mb) => mb,
            _ => {
                let mut all: Vec<String> = sizes.iter().map(|mb| mb.to_string()).collect();
                all.insert(0, "0".into());
                let last = all.pop().expect("0 is there");
                let list = if all.is_empty() {
                    last
                } else {
                    format!("{} or {last}", all.join(", "))
                };
                return Err(usage(format!(
                    "--psram-mb: {} supports only {list} MiB, got {v:?}",
                    opts.chip.title
                )));
            }
        };
    }
    unsupported(&opts)?;
    Ok(opts)
}

/// What the command line asks of the chip that esp32sim does not model on it, or that the chip
/// does not have. The ESP32-P4 and the ESP32-S31 have neither `--wifi` nor `--bt` in esp32sim,
/// nor an SD card slot on the board's SPI pins (`--sd-spi`). The classic ESP32 has no
/// USB-Serial/JTAG controller, so `--console-port usb` is refused on it.
fn unsupported(opts: &Opts) -> Result<(), Error> {
    let chip = opts.chip;
    let asked = [
        (
            "--wifi",
            matches!(opts.wifi, Some(Wifi::AccessPoint { .. })) && !chip.wifi,
            "has no Wi-Fi in esp32sim",
        ),
        (
            "--wifi-sta",
            matches!(opts.wifi, Some(Wifi::Station { .. })) && !chip.wifi_station,
            "has no virtual station for a SoftAP in esp32sim",
        ),
        (
            "--bt",
            opts.bt.is_some() && !chip.bt,
            "has no VHCI hooks in esp32sim",
        ),
        (
            "--no-bt",
            opts.no_bt.is_some() && !chip.bt,
            "has no Bluetooth in esp32sim",
        ),
        (
            "--sd-spi",
            opts.sd_card.is_some() && !chip.sd_spi,
            "tells the board no SPI pins in esp32sim",
        ),
        (
            "--console-port usb",
            opts.console_port == ConsolePort::Usb && !chip.usb_serial_jtag,
            "has no USB-Serial/JTAG controller",
        ),
    ];
    match asked.into_iter().find(|(_, refused, _)| *refused) {
        Some((flag, _, why)) => Err(usage(format!("{flag}: {} {why}", chip.name))),
        None => Ok(()),
    }
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

/// `sclk=N,mosi=N,miso=N,cs=N`, in any order: four distinct GPIOs of `chip`, those the chip
/// drives (all but MISO) not input-only.
fn wiring(spec: &str, chip: &Profile) -> Result<Wiring, Error> {
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
            .filter(|&p| p < 64 && chip.gpios & 1 << p != 0)
            .ok_or_else(|| bad(format!("{pin:?} is not a GPIO of {}", chip.title)))?;
        if name != "miso" && chip.input_only & 1 << pin != 0 {
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
