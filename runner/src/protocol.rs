//! The link protocol's wire format (`docs/protocol.md`, version 1): time in nanoseconds, base64,
//! the messages the peer sends and the lines the runner writes. JSON is parsed with esp32sim's own
//! parser (`esp_soc::json`, which its Cooja NDJSON link uses), so the runner adds no JSON crate.
use crate::sd_card::{Action, Fault, Faulted, Op};
use esp_soc::json::{Json, parse_json};
use esp_soc::uart::UartRoute;
use std::fmt::Write;

pub const VERSION: i64 = 1;
pub const CHIP: &str = "esp32";
/// What the times of `uart_tx` mean (protocol 5.1): emulated time, at the FIFO write.
pub const CLOCK: &str = "emulated-fifo-write";
/// The same times under `--realtime`, paced to wall time (protocol 12.1).
pub const WALL: &str = "wall";
/// The esp32sim revision of the `[dependencies]` in Cargo.toml, for the recording header's
/// `tools`. A test checks that the two agree.
pub const ESP32SIM: &str = "3bdb9582a910974e4e025d274d6ec61e5c481be6";
/// The longest line either side may send (protocol §2).
pub const MAX_LINE: usize = 1 << 20;
/// An Ethernet frame without FCS, as esp32sim's relay takes and gives it (5.15): a header, and at
/// most 1504 bytes of payload. The station's frames end in 4 bytes more, libpp's guard word, which
/// the relay's limit counts: a 1500-byte datagram's frame is 1518 bytes with them.
pub const ETH_MIN: usize = 14;
pub const ETH_MAX: usize = 1518;
/// The longest H4 packet esp32sim's VHCI carries either way (5.22): its buffer, `BUFFER_SIZE`
/// 512 bytes, less the 16 before the packet, which hold its length word and the controller task's
/// name (`esp-soc/src/ble/vhci.rs`).
pub const HCI_MAX: usize = 496;
/// The longest delay an `sd_fault` may give (5.20): 10 emulated seconds, twice ESP-IDF's longest
/// SD timeout, its 5 s for a write.
pub const SD_DELAY_MAX: u64 = 10_000_000_000;

const CPU_HZ: u128 = esp32::periph::CPU_HZ as u128;
const NS_PER_S: u128 = 1_000_000_000;

/// Bus cycles to nanoseconds, rounded down (protocol §3): an event is reported at or before its
/// exact time.
pub fn ns(cycles: u64) -> u64 {
    (cycles as u128 * NS_PER_S / CPU_HZ) as u64
}

/// Nanoseconds to bus cycles, rounded up (protocol §3): an input takes effect at the first cycle
/// at or after its time.
pub fn cycles(ns: u64) -> u64 {
    (ns as u128 * CPU_HZ)
        .div_ceil(NS_PER_S)
        .min(u64::MAX as u128) as u64
}

/// One character on the wire at `baud`, 8N1, in nanoseconds: 10 bit times.
pub fn char_ns(k: u64, baud: u32) -> u64 {
    (k as u128 * 10 * NS_PER_S / baud as u128) as u64
}

const B64: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/// Base64 with padding (RFC 4648, section 4).
pub fn base64(data: &[u8]) -> String {
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let b = [
            chunk[0],
            *chunk.get(1).unwrap_or(&0),
            *chunk.get(2).unwrap_or(&0),
        ];
        let n = u32::from(b[0]) << 16 | u32::from(b[1]) << 8 | u32::from(b[2]);
        for i in 0..4 {
            if i <= chunk.len() {
                out.push(B64[(n >> (18 - 6 * i)) as usize & 63] as char);
            } else {
                out.push('=');
            }
        }
    }
    out
}

/// The inverse of [`base64`], strict: padding is required and nothing else is accepted.
pub fn unbase64(text: &str) -> Result<Vec<u8>, String> {
    let s = text.as_bytes();
    if !s.len().is_multiple_of(4) {
        return Err("base64 length is not a multiple of 4".into());
    }
    let value = |c: u8| B64.iter().position(|&b| b == c).map(|v| v as u32);
    let mut out = Vec::with_capacity(s.len() / 4 * 3);
    for (i, quad) in s.chunks(4).enumerate() {
        let last = i == s.len() / 4 - 1;
        let pad = quad.iter().rev().take_while(|&&c| c == b'=').count();
        if pad > 2 || (pad > 0 && !last) {
            return Err("misplaced base64 padding".into());
        }
        let mut n = 0u32;
        for &c in &quad[..4 - pad] {
            n = n << 6 | value(c).ok_or_else(|| format!("bad base64 character {:?}", c as char))?;
        }
        n <<= 6 * pad as u32;
        out.extend_from_slice(&n.to_be_bytes()[1..4 - pad]);
    }
    Ok(out)
}

/// A message from the peer.
#[derive(Debug, PartialEq)]
pub enum Incoming {
    Hello {
        version: i64,
        chip: String,
        watch: Vec<u8>,
    },
    UartRx {
        t: u64,
        pin: u8,
        baud: u32,
        data: Vec<u8>,
    },
    GpioIn {
        t: u64,
        levels: Vec<(u8, bool)>,
    },
    Release {
        t: u64,
        pins: Vec<u8>,
    },
    /// An Ethernet frame for the station, behind the access point of `--wifi` (5.15).
    EthRx {
        t: u64,
        data: Vec<u8>,
    },
    /// An H4 packet for the guest's Bluetooth host, from the controller of `--bt` (5.22).
    HciRx {
        t: u64,
        data: Vec<u8>,
    },
    /// The access point of `--wifi` on the air or off it (5.16).
    WifiAp {
        t: u64,
        on: bool,
    },
    /// The board's supply switched off or on (5.17).
    Power {
        t: u64,
        on: bool,
    },
    /// The chip's EN pin pulsed: a power-on reset of the chip alone (5.18).
    ChipReset {
        t: u64,
    },
    /// A fault rule for the SD card of `--sd-rw` (5.20).
    SdFault {
        t: u64,
        fault: Fault,
    },
    /// The SD card's rules with these ids, or all of them, gone (5.20).
    SdClear {
        t: u64,
        ids: Option<Vec<u64>>,
    },
    /// A card holding the image file `image` into the slot of `--sd-spi`, or, with `None`, the
    /// card out of it (5.19).
    SdCard {
        t: u64,
        image: Option<String>,
    },
    /// The whole flash into the file of `--flash-out`, now, in this turn (5.21).
    FlashDump,
    Wait {
        ack: u64,
        quiet_until: Option<u64>,
        latency: Option<u64>,
    },
    Stop,
    /// `adc`: a valid message the runner does not implement yet.
    NotYet(&'static str),
}

/// One line from the peer. An error is a protocol error, said for the stderr line.
pub fn parse(line: &str) -> Result<Incoming, String> {
    let msg = parse_json(line).map_err(|e| format!("not JSON: {e}"))?;
    if !matches!(msg, Json::Obj(_)) {
        return Err("not a JSON object".into());
    }
    let kind = msg
        .get("type")
        .ok_or("no \"type\"")?
        .as_str()
        .ok_or("\"type\" is not a string")?;
    let field = |key: &'static str| Field {
        msg: &msg,
        kind,
        key,
    };
    Ok(match kind {
        "hello" => Incoming::Hello {
            version: field("version").int()?,
            chip: field("chip").string()?,
            watch: field("watch").pins()?,
        },
        "uart_rx" => Incoming::UartRx {
            t: field("t").time()?,
            pin: field("pin").pin()?,
            baud: u32::try_from(field("baud").int()?)
                .map_err(|_| "uart_rx: \"baud\" out of range".to_string())?,
            data: unbase64(&field("data").string()?).map_err(|e| format!("uart_rx: {e}"))?,
        },
        "wait" => Incoming::Wait {
            ack: field("ack").time()?,
            quiet_until: field("quiet_until").optional()?,
            latency: field("latency").optional()?,
        },
        "gpio_in" => Incoming::GpioIn {
            t: field("t").time()?,
            levels: field("levels").levels()?,
        },
        "release" => Incoming::Release {
            t: field("t").time()?,
            pins: field("pins").pins()?,
        },
        "eth_rx" => {
            let data = unbase64(&field("data").string()?).map_err(|e| format!("eth_rx: {e}"))?;
            if !(ETH_MIN..=ETH_MAX).contains(&data.len()) {
                return Err(format!(
                    "eth_rx: an Ethernet frame of {} bytes; one without FCS has {ETH_MIN} to \
                     {ETH_MAX}",
                    data.len()
                ));
            }
            Incoming::EthRx {
                t: field("t").time()?,
                data,
            }
        }
        "hci_rx" => {
            let data = unbase64(&field("data").string()?).map_err(|e| format!("hci_rx: {e}"))?;
            if !(1..=HCI_MAX).contains(&data.len()) {
                return Err(format!(
                    "hci_rx: an H4 packet of {} bytes; esp32sim's VHCI carries 1 to {HCI_MAX}",
                    data.len()
                ));
            }
            Incoming::HciRx {
                t: field("t").time()?,
                data,
            }
        }
        "wifi_ap" => Incoming::WifiAp {
            t: field("t").time()?,
            on: field("on").boolean()?,
        },
        "power" => Incoming::Power {
            t: field("t").time()?,
            on: field("on").boolean()?,
        },
        "chip_reset" => Incoming::ChipReset {
            t: field("t").time()?,
        },
        "sd_fault" => Incoming::SdFault {
            t: field("t").time()?,
            fault: sd_fault(&field)?,
        },
        "sd_clear" => Incoming::SdClear {
            t: field("t").time()?,
            ids: field("ids").numbers()?,
        },
        "sd_card" => Incoming::SdCard {
            t: field("t").time()?,
            image: field("image").optional_string()?,
        },
        "flash_dump" => Incoming::FlashDump,
        "stop" => Incoming::Stop,
        "adc" => Incoming::NotYet("adc"),
        _ => return Err(format!("unknown type {kind:?}")),
    })
}

/// The rule of an `sd_fault`, checked as far as it can be without the card (5.20).
fn sd_fault<'a>(field: &impl Fn(&'static str) -> Field<'a>) -> Result<Fault, String> {
    let op = match field("op").string()?.as_str() {
        "read" => Op::Read,
        "write" => Op::Write,
        "erase" => Op::Erase,
        op => {
            return Err(format!(
                "sd_fault: \"op\" {op:?} is not read, write or erase"
            ));
        }
    };
    let delay = field("delay").optional()?;
    let action = match (field("action").string()?.as_str(), delay) {
        ("error", None) if op == Op::Erase => {
            return Err("sd_fault: an erase cannot fail, only be delayed".into());
        }
        ("error", None) => Action::Error,
        ("error", Some(_)) => return Err("sd_fault: an error takes no \"delay\"".into()),
        ("delay", Some(ns @ 1..=SD_DELAY_MAX)) => Action::Delay(ns),
        ("delay", Some(ns)) => {
            return Err(format!(
                "sd_fault: a delay of {ns} ns; one is 1 ns to {SD_DELAY_MAX} ns (10 s)"
            ));
        }
        ("delay", None) => return Err("sd_fault: a delay takes \"delay\"".into()),
        (action, _) => {
            return Err(format!(
                "sd_fault: \"action\" {action:?} is not error or delay"
            ));
        }
    };
    let count = match field("count").optional()? {
        Some(0) => return Err("sd_fault: a \"count\" of 0".into()),
        count => count,
    };
    let blocks = match field("blocks").numbers()?.as_deref() {
        None => None,
        Some(&[first, last]) if first <= last => Some((first, last)),
        Some(&[first, last]) => {
            return Err(format!(
                "sd_fault: \"blocks\" {first} to {last}: the last before the first"
            ));
        }
        Some(_) => return Err("sd_fault: \"blocks\" is not [first, last]".into()),
    };
    Ok(Fault {
        id: field("id").time()?,
        op,
        action,
        blocks,
        count,
    })
}

struct Field<'a> {
    msg: &'a Json,
    kind: &'a str,
    key: &'a str,
}

impl Field<'_> {
    fn error(&self, what: &str) -> String {
        format!("{}: \"{}\" {what}", self.kind, self.key)
    }
    /// `None` for an absent field or `null` (protocol §2: the two mean the same).
    fn value(&self) -> Option<&Json> {
        self.msg.get(self.key).filter(|v| **v != Json::Null)
    }
    fn int(&self) -> Result<i64, String> {
        match self.value() {
            Some(Json::Int(v)) => Ok(*v),
            Some(_) => Err(self.error("is not an integer")),
            None => Err(self.error("is missing")),
        }
    }
    fn boolean(&self) -> Result<bool, String> {
        match self.value() {
            Some(Json::Bool(v)) => Ok(*v),
            Some(_) => Err(self.error("is not true or false")),
            None => Err(self.error("is missing")),
        }
    }
    fn time(&self) -> Result<u64, String> {
        u64::try_from(self.int()?).map_err(|_| self.error("is negative"))
    }
    fn optional(&self) -> Result<Option<u64>, String> {
        self.value().map(|_| self.time()).transpose()
    }
    fn string(&self) -> Result<String, String> {
        match self.value() {
            Some(Json::Str(s)) => Ok(s.clone()),
            Some(_) => Err(self.error("is not a string")),
            None => Err(self.error("is missing")),
        }
    }
    /// `None` for an absent field or `null`.
    fn optional_string(&self) -> Result<Option<String>, String> {
        self.value().map(|_| self.string()).transpose()
    }
    fn pin(&self) -> Result<u8, String> {
        pin(self.int()?).ok_or_else(|| self.error("is not a GPIO number"))
    }
    /// An array of non-negative integers; `None` when absent or `null`.
    fn numbers(&self) -> Result<Option<Vec<u64>>, String> {
        let Some(value) = self.value() else {
            return Ok(None);
        };
        let items = value
            .as_arr()
            .ok_or_else(|| self.error("is not an array"))?;
        items
            .iter()
            .map(|v| match v {
                Json::Int(n) => u64::try_from(*n).ok(),
                _ => None,
            })
            .collect::<Option<_>>()
            .map(Some)
            .ok_or_else(|| self.error("holds something other than non-negative integers"))
    }
    /// A pin set; absent or `null` is the empty set.
    fn pins(&self) -> Result<Vec<u8>, String> {
        let Some(value) = self.value() else {
            return Ok(Vec::new());
        };
        let items = value
            .as_arr()
            .ok_or_else(|| self.error("is not an array"))?;
        items
            .iter()
            .map(|v| match v {
                Json::Int(p) => pin(*p),
                _ => None,
            })
            .collect::<Option<_>>()
            .ok_or_else(|| self.error("holds something other than GPIO numbers"))
    }
    /// `[pin, level]` pairs, a level being 0 or 1.
    fn levels(&self) -> Result<Vec<(u8, bool)>, String> {
        let items = match self.value() {
            Some(value) => value
                .as_arr()
                .ok_or_else(|| self.error("is not an array"))?,
            None => return Err(self.error("is missing")),
        };
        items
            .iter()
            .map(|pair| match pair.as_arr() {
                Some([Json::Int(p), Json::Int(level @ (0 | 1))]) => {
                    pin(*p).map(|p| (p, *level == 1))
                }
                _ => None,
            })
            .collect::<Option<_>>()
            .ok_or_else(|| self.error("holds something other than [pin, level] pairs"))
    }
}

fn pin(v: i64) -> Option<u8> {
    u8::try_from(v).ok().filter(|&p| p < 64)
}

/// A pin set as a JSON array, lowest pin first.
fn pins(mask: u64) -> String {
    let list: Vec<String> = (0..64)
        .filter(|p| mask & 1 << p != 0)
        .map(|p| p.to_string())
        .collect();
    format!("[{}]", list.join(","))
}

fn opt<T: std::fmt::Display>(v: Option<T>) -> String {
    v.map_or("null".into(), |v| v.to_string())
}

fn escape(s: &str) -> String {
    esp_soc::web::json_escape(s)
}

pub fn hello(clock: &str) -> String {
    format!(
        r#"{{"type":"hello","version":{VERSION},"chip":"{CHIP}","clock":"{clock}","esp32sim":"{ESP32SIM}"}}"#
    )
}

pub fn sync(t: u64) -> String {
    format!(r#"{{"type":"sync","t":{t}}}"#)
}

pub fn uart_tx(seq: u64, t: u64, route: &UartRoute, data: &[u8]) -> String {
    format!(
        r#"{{"type":"uart_tx","seq":{seq},"t":{t},"port":{},"tx_pins":{},"baud":{},"data":"{}"}}"#,
        route.port,
        pins(route.tx_pins),
        opt(route.baud),
        base64(data)
    )
}

pub fn uart_cfg(seq: u64, t: u64, route: &UartRoute) -> String {
    format!(
        r#"{{"type":"uart_cfg","seq":{seq},"t":{t},"port":{},"tx_pins":{},"rx_pin":{},"baud":{}}}"#,
        route.port,
        pins(route.tx_pins),
        opt(route.rx_pin),
        opt(route.baud)
    )
}

pub fn gpio_out(seq: u64, t: u64, changes: &[(u8, bool)], enabled: u64, output: u64) -> String {
    let changes: Vec<String> = changes
        .iter()
        .map(|&(pin, level)| format!("[{pin},{}]", u8::from(level)))
        .collect();
    format!(
        r#"{{"type":"gpio_out","seq":{seq},"t":{t},"changes":[{}],"enabled":{},"output":{}}}"#,
        changes.join(","),
        pins(enabled),
        pins(output)
    )
}

pub fn reset(seq: u64, t: u64, cause: u32) -> String {
    format!(r#"{{"type":"reset","seq":{seq},"t":{t},"cause":{cause}}}"#)
}

pub fn eth_tx(seq: u64, t: u64, data: &[u8]) -> String {
    format!(
        r#"{{"type":"eth_tx","seq":{seq},"t":{t},"data":"{}"}}"#,
        base64(data)
    )
}

/// An H4 packet the guest's Bluetooth host sent its controller, with its packet-type byte (5.22).
pub fn hci_tx(seq: u64, t: u64, data: &[u8]) -> String {
    format!(
        r#"{{"type":"hci_tx","seq":{seq},"t":{t},"data":"{}"}}"#,
        base64(data)
    )
}

/// What one of the peer's rules did to the SD card (5.20): not an event.
pub fn sd_op(t: u64, f: &Faulted) -> String {
    let (action, delay) = match f.action {
        Action::Error => ("error", "null".to_string()),
        Action::Delay(ns) => ("delay", ns.to_string()),
    };
    format!(
        r#"{{"type":"sd_op","t":{t},"fault":{},"op":"{}","blocks":[{},{}],"action":"{action}","delay":{delay},"written":{}}}"#,
        f.fault,
        f.op.name(),
        f.blocks.0,
        f.blocks.1,
        f.written
    )
}

/// What the slot holds after the peer's `sd_card`, and why the change was refused, if it was
/// (5.19).
pub fn sd_card_ack(t: u64, image: Option<&str>, error: Option<&str>) -> String {
    let quoted = |v: Option<&str>| v.map_or("null".into(), |v| format!(r#""{}""#, escape(v)));
    format!(
        r#"{{"type":"sd_card_ack","t":{t},"image":{},"error":{}}}"#,
        quoted(image),
        quoted(error)
    )
}

/// The file of `--flash-out` holds the flash of the turn at `t`, unless `error` says why it does
/// not (5.21). Not an event: the turn it answers is still going on.
pub fn flash_dump_ack(t: u64, error: Option<&str>) -> String {
    let error = error.map_or("null".into(), |e| format!(r#""{}""#, escape(e)));
    format!(r#"{{"type":"flash_dump_ack","t":{t},"error":{error}}}"#)
}

pub fn console(t: u64, data: &[u8]) -> String {
    format!(r#"{{"type":"console","t":{t},"data":"{}"}}"#, base64(data))
}

/// What the emulator dropped or refused (protocol 5.8).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum WarnKind {
    RxFifoOvf,
    FrmErr,
    UnroutedRx,
    /// `gpio_in` on a pin the guest drives: one per pin and message, never merged, no `bytes`.
    InputOnOutput,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Warn {
    pub t: u64,
    pub kind: WarnKind,
    /// `None` for `unrouted_rx`, where no UART received the bytes, and for `input_on_output`.
    pub port: Option<usize>,
    pub pin: u8,
    /// The route's baud, for `frm_err`.
    pub baud: Option<u32>,
    pub bytes: u64,
}

pub fn warn(w: &Warn) -> String {
    let (kind, detail) = match w.kind {
        WarnKind::RxFifoOvf => (
            "rxfifo_ovf",
            format!(
                "UART{} RX FIFO full (128 bytes) on GPIO {}: {} bytes dropped",
                w.port.unwrap_or_default(),
                w.pin,
                w.bytes
            ),
        ),
        WarnKind::FrmErr => (
            "frm_err",
            format!(
                "UART{} on GPIO {} runs at {}, not at the sender's baud: {} bytes dropped as frame errors",
                w.port.unwrap_or_default(),
                w.pin,
                w.baud.map_or("no baud".into(), |b| format!("{b} baud")),
                w.bytes
            ),
        ),
        WarnKind::UnroutedRx => (
            "unrouted_rx",
            format!(
                "uart_rx on GPIO {}, which no UART receives on: {} bytes dropped",
                w.pin, w.bytes
            ),
        ),
        WarnKind::InputOnOutput => (
            "input_on_output",
            format!(
                "gpio_in on GPIO {}, which the guest drives as an output: the level is applied all the same",
                w.pin
            ),
        ),
    };
    let mut out = format!(r#"{{"type":"warn","t":{},"kind":"{kind}""#, w.t);
    if let Some(port) = w.port {
        let _ = write!(out, r#","port":{port}"#);
    }
    let _ = write!(out, r#","pin":{}"#, w.pin);
    if w.kind == WarnKind::FrmErr {
        let _ = write!(out, r#","baud":{}"#, opt(w.baud));
    }
    if w.kind != WarnKind::InputOnOutput {
        let _ = write!(out, r#","bytes":{}"#, w.bytes);
    }
    let _ = write!(out, r#","detail":"{}"}}"#, escape(&detail));
    out
}

/// Per UART port, for `stats`.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct PortStats {
    /// Whether the port carried a watched route during the run: only those are reported.
    pub watched: bool,
    pub tx_bytes: u64,
    pub rx_bytes: u64,
    pub rxfifo_ovf: u64,
    pub frm_err: u64,
}

/// The Wi-Fi station's link, for `stats` with `--wifi` (5.9).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct WifiStats {
    /// `eth_tx` messages: Ethernet frames the station sent.
    pub eth_tx: u64,
    /// `eth_rx` frames the station's link took.
    pub eth_rx: u64,
    /// `eth_rx` frames that came due while the access point was off the air, or that it still
    /// held when it went off.
    pub eth_rx_dropped: u64,
    /// Frames from the air the station's receive ring had no descriptor for (esp32sim's
    /// `StationLink::rx_dropped`), beacons included.
    pub rx_dropped: u64,
    /// Frames the station sent that the relay dropped: longer than 1518 bytes, or more than 64
    /// in one slice (`StationLink::tx_dropped`).
    pub eth_tx_dropped: u64,
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Stats {
    pub resets: u64,
    pub turns: u64,
    pub uart: [PortStats; 3],
    pub unrouted_rx_bytes: u64,
    /// `gpio_in` levels on a pin the guest drove as an output, one per `input_on_output` warning.
    pub inputs_on_output: u64,
    pub inputs_not_applied: u64,
    pub rx_bytes_not_paced: u64,
    /// With `--wifi` only.
    pub wifi: Option<WifiStats>,
    /// With `--realtime` only: how many times esp32sim skipped a lag behind wall time.
    pub rt_resyncs: Option<u64>,
}

/// `end` and its extra fields: `stop` and `detail` for `emulator`, `detail` for `protocol_error`.
pub fn stats(t: u64, end: &str, extra: &[(&str, &str)], s: &Stats) -> String {
    let mut out = format!(r#"{{"type":"stats","t":{t},"end":"{end}""#);
    for (key, value) in extra {
        let _ = write!(out, r#","{key}":"{}""#, escape(value));
    }
    let uart: Vec<String> = (0..3)
        .filter(|&port| s.uart[port].watched)
        .map(|port| {
            let p = &s.uart[port];
            format!(
                r#"{{"port":{port},"tx_bytes":{},"rx_bytes":{},"rxfifo_ovf":{},"frm_err":{}}}"#,
                p.tx_bytes, p.rx_bytes, p.rxfifo_ovf, p.frm_err
            )
        })
        .collect();
    let _ = write!(
        out,
        r#","resets":{},"turns":{},"uart":[{}],"unrouted_rx_bytes":{},"inputs_on_output":{},"inputs_not_applied":{},"rx_bytes_not_paced":{}"#,
        s.resets,
        s.turns,
        uart.join(","),
        s.unrouted_rx_bytes,
        s.inputs_on_output,
        s.inputs_not_applied,
        s.rx_bytes_not_paced
    );
    if let Some(w) = &s.wifi {
        let _ = write!(
            out,
            r#","wifi":{{"eth_tx":{},"eth_tx_dropped":{},"eth_rx":{},"eth_rx_dropped":{},"rx_dropped":{}}}"#,
            w.eth_tx, w.eth_tx_dropped, w.eth_rx, w.eth_rx_dropped, w.rx_dropped
        );
    }
    if let Some(resyncs) = s.rt_resyncs {
        let _ = write!(out, r#","rt_resyncs":{resyncs}"#);
    }
    out.push('}');
    out
}

// In a file of its own, so that `make docs-check` tells a test-only change by its path.
#[cfg(test)]
mod tests;
