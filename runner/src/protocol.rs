//! The link protocol's wire format (`docs/protocol.md`, version 1): time in nanoseconds, base64,
//! the messages the peer sends and the lines the runner writes. JSON is parsed with esp32sim's own
//! parser (`esp_soc::json`, which its Cooja NDJSON link uses), so the runner adds no JSON crate.
use esp_soc::json::{Json, parse_json};
use esp_soc::uart::UartRoute;
use std::fmt::Write;

pub const VERSION: i64 = 1;
pub const CHIP: &str = "esp32";
/// What the times of `uart_tx` mean (protocol 5.1): emulated time, at the FIFO write.
pub const CLOCK: &str = "emulated-fifo-write";
/// The esp32sim revision of the `[dependencies]` in Cargo.toml, for the recording header's
/// `tools`. A test checks that the two agree.
pub const ESP32SIM: &str = "8c652151dd5a521249a785ca147f20a9f8eda68f";
/// The longest line either side may send (protocol §2).
pub const MAX_LINE: usize = 1 << 20;

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
    Wait {
        ack: u64,
        quiet_until: Option<u64>,
        latency: Option<u64>,
    },
    Stop,
    /// `gpio_in`, `release` or `adc`: valid messages the runner does not implement until #23.
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
        "stop" => Incoming::Stop,
        "gpio_in" => Incoming::NotYet("gpio_in"),
        "release" => Incoming::NotYet("release"),
        "adc" => Incoming::NotYet("adc"),
        _ => return Err(format!("unknown type {kind:?}")),
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
    fn pin(&self) -> Result<u8, String> {
        pin(self.int()?).ok_or_else(|| self.error("is not a GPIO number"))
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

pub fn hello() -> String {
    format!(
        r#"{{"type":"hello","version":{VERSION},"chip":"{CHIP}","clock":"{CLOCK}","esp32sim":"{ESP32SIM}"}}"#
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

pub fn reset(seq: u64, t: u64, cause: u32) -> String {
    format!(r#"{{"type":"reset","seq":{seq},"t":{t},"cause":{cause}}}"#)
}

pub fn console(t: u64, data: &[u8]) -> String {
    format!(r#"{{"type":"console","t":{t},"data":"{}"}}"#, base64(data))
}

/// What the emulator dropped (protocol 5.8).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum WarnKind {
    RxFifoOvf,
    FrmErr,
    UnroutedRx,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Warn {
    pub t: u64,
    pub kind: WarnKind,
    /// `None` for `unrouted_rx`: no UART received the bytes.
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
    };
    let mut out = format!(r#"{{"type":"warn","t":{},"kind":"{kind}""#, w.t);
    if let Some(port) = w.port {
        let _ = write!(out, r#","port":{port}"#);
    }
    let _ = write!(out, r#","pin":{}"#, w.pin);
    if w.kind == WarnKind::FrmErr {
        let _ = write!(out, r#","baud":{}"#, opt(w.baud));
    }
    let _ = write!(
        out,
        r#","bytes":{},"detail":"{}"}}"#,
        w.bytes,
        escape(&detail)
    );
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

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Stats {
    pub resets: u64,
    pub turns: u64,
    pub uart: [PortStats; 3],
    pub unrouted_rx_bytes: u64,
    pub inputs_not_applied: u64,
    pub rx_bytes_not_paced: u64,
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
        r#","resets":{},"turns":{},"uart":[{}],"unrouted_rx_bytes":{},"inputs_on_output":0,"inputs_not_applied":{},"rx_bytes_not_paced":{}}}"#,
        s.resets,
        s.turns,
        uart.join(","),
        s.unrouted_rx_bytes,
        s.inputs_not_applied,
        s.rx_bytes_not_paced
    );
    out
}

// In a file of its own, so that `make docs-check` tells a test-only change by its path.
#[cfg(test)]
mod tests;
