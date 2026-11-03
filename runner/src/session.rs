//! The run loop with a peer on the socket (`docs/protocol.md`, sections 4 to 9 and 6.1).
//!
//! The guest runs in slices: `max_cycles` is set to the slice's end and `run` returns
//! `Stop::Halted` there. Between slices, where the runner has the bus, it turns what the board
//! recorded into messages, compares the UART routes, takes the turn at the block point `B`,
//! applies the inputs now due (GPIO levels through the board) and paces received bytes into the
//! chip. A slice ends at the earliest of `B`, the next paced byte, the next queued input, the
//! next whole emulated millisecond and the end of the run.
//!
//! One more bound keeps an event from carrying the guest past its own block point: while no event
//! is unacknowledged, a slice is at most `latency` long, because an event in it would set
//! `B = t + latency`, and the board callback that records it cannot stop `run`.
//!
//! With `--wifi` the station's Ethernet frames go to the peer too, taken from esp32sim's relay at
//! each slice boundary (`eth_tx`), and the peer's frames go to the station's link when they come
//! due (`eth_rx`), as does the access point going off the air and coming back (`wifi_ap`).
//!
//! The peer can also pulse the chip's EN pin (`chip_reset`) and switch the board's supply
//! (`power`). Either way the chip comes back through a power-on reset, which the peer sees as
//! `reset` with cause 1. While the supply is off the guest runs nothing and its time still passes:
//! the slices go on, without the guest, so that the peer's turns and inputs keep their times.
//!
//! [`Guest`] and [`Link`] stand for the machine and the socket, so the tests run the loop
//! against a scripted guest and a fake peer.
use crate::cli;
use crate::protocol::{self, Incoming, Stats, Warn, WarnKind, WifiStats, char_ns, cycles, ns};
use crate::sd_card;
use crate::socket_board::{Recorded, Shared};
use crate::transport::{Link, LinkError};
use esp_soc::uart::{UartInput, UartRoute};
use esp_soc::wifi::{self, StaState};
use esp_soc::{SocBus, Stop};
use std::collections::{BTreeMap, VecDeque};
use std::convert::Infallible;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

/// The classic chip's UARTs.
pub const PORTS: usize = 3;
/// The classic chip's GPIOs, as a mask: 0 to 39 but 24 and 28 to 31, as the board profiles have
/// them (`esp32tl/boards`, `GPIOS`). esp32sim's `gpio_state` also leaves out GPIO 20, which only
/// some packages bond (`esp32/src/soc.rs:191`), but its `set_input` takes a level there; it
/// ignores one on 40 and above (`esp32/src/periph.rs:373-376`).
const GPIOS: u64 = ((1 << 40) - 1) & !(1 << 24 | 0xf << 28);
/// The classic RX FIFO (`esp-periph/src/uart.rs`): what does not fit is dropped.
const RX_FIFO: usize = 128;
/// The longest `data` of one message, so that a line stays well under the 1 MiB limit.
const MAX_DATA: usize = 48 << 10;
const MS: u64 = 1_000_000;

/// The machine, as the run loop needs it.
pub trait Guest {
    fn cycles(&self) -> u64;
    /// Runs until the bus reaches `cycle` or something stops the guest first.
    fn run_until(&mut self, cycle: u64) -> Stop;
    /// After `Stop::SwReset`: the chip reset, returning the cause the ROM will report.
    fn chip_reset(&mut self) -> u32;
    /// The chip's power-on reset, from EN or from the board's supply coming back: SRAM, the RTC
    /// memories and the RTC registers start again as at the run's first power-on, and the flash
    /// keeps what the guest wrote to it. `rom` is the mask ROM's ELF. Returns the cause the ROM
    /// will report, `POWERON_RESET`.
    fn power_on_reset(&mut self, rom: &[u8]) -> u32;
    /// With the board's supply off: the bus's clock moves on to `cycle`, and the chip runs
    /// nothing.
    fn idle_until(&mut self, cycle: u64);
    fn uart_route(&self, port: usize) -> UartRoute;
    fn rx_pending(&self, port: usize) -> usize;
    /// One received byte on `pin`, into every UART whose RX pin it is.
    fn uart_input(&mut self, pin: u8, baud: u32, byte: u8);
    /// `GPIO_ENABLE` and `GPIO_OUT`, the words `gpio_output_at` reports.
    fn gpio_drive(&self) -> (u64, u64);
    /// Has the chip take the levels and releases the run loop handed the board, now rather than
    /// at the bus's next tick.
    fn take_gpio_inputs(&mut self);
    // Wi-Fi, asked only with `--wifi`: the defaults are a chip without it.

    /// The Ethernet frames the station sent since the last call, in order: esp32sim's relay.
    fn take_eth_frames(&mut self) -> Vec<Vec<u8>> {
        Vec::new()
    }
    /// Hands the station's link one Ethernet frame for the station; `false` when its queue is
    /// full and the frame was not taken.
    fn eth_input(&mut self, _frame: &[u8]) -> bool {
        false
    }
    /// Puts the access point of `--wifi`, and the networks around, on the air afresh. The relay
    /// to the peer stays.
    fn wifi_access_point(&mut self, _wifi: &cli::Wifi) {}
    /// Takes the access point, and the networks around, off the air, as one that shuts down: it
    /// deauthenticates a station that has joined it, and leaves once the station has had the
    /// frame. `true` once it has left; asked again at every slice boundary until then.
    fn wifi_leave(&mut self) -> bool {
        true
    }
    /// The station link's drops, `(received, sent)`, since the access point last went on the air
    /// or the chip last reset, which both start the counts again.
    fn wifi_drops(&self) -> (u64, u64) {
        (0, 0)
    }
    /// Takes the access point, and the networks around, off the station's air at once, without
    /// a word: the station has lost its power, and with it what it knew of them.
    fn wifi_forget(&mut self) {}
}

/// Where the classic chip's SRAM and RTC memories are on the data bus, besides the DRAM, which
/// ends at `esp32::bus::DRAM_HIGH` (`esp32/src/bus.rs`, `SocBus::memory`).
const CACHE_RAM: u32 = 0x4007_0000;
const RTC_FAST: u32 = 0x3ff8_0000;
const RTC_SLOW: u32 = 0x5000_0000;

impl Guest for esp32::Machine {
    fn cycles(&self) -> u64 {
        self.bus.cycles
    }
    fn run_until(&mut self, cycle: u64) -> Stop {
        self.max_cycles = cycle;
        self.run(u64::MAX)
    }
    fn chip_reset(&mut self) -> u32 {
        self.reboot()
    }
    fn power_on_reset(&mut self, rom: &[u8]) -> u32 {
        // the cores, the digital peripherals and PSRAM, as at any reset
        self.reboot();
        // what keeps its contents through `reboot` and not without power: through the bus, so
        // that the code decoded from it is decoded again
        let bus = &mut self.bus;
        let memories = [
            (
                esp32::bus::DRAM_HIGH - bus.dram.len() as u32,
                bus.dram.len(),
            ),
            (esp32::bus::IRAM_LOW, bus.iram.len()),
            (CACHE_RAM, bus.cache_ram.len()),
            (RTC_FAST, bus.rtc_fast.len()),
            (RTC_SLOW, bus.rtc_slow.len()),
        ];
        for (address, len) in memories {
            bus.load_bytes(address, &vec![0; len])
                .expect("the chip's memories are mapped");
        }
        // the ROM's data in DRAM, as its reset handler finds it after the first power-on
        self.load_rom(rom)
            .expect("the ROM loaded at the start of the run");
        // the RTC registers, the RTC timer and RESET_STATE as a new chip has them: POWERON
        self.bus.periph.rtc = esp32::periph::Peripherals::new(self.mac).rtc;
        esp_periph::rtc_cntl::RST_POWERON
    }
    fn idle_until(&mut self, cycle: u64) {
        self.bus.cycles = cycle;
    }
    fn uart_route(&self, port: usize) -> UartRoute {
        self.bus.periph.uart_route(port)
    }
    fn rx_pending(&self, port: usize) -> usize {
        self.bus.periph.uart[port].rx_pending()
    }
    fn uart_input(&mut self, pin: u8, baud: u32, byte: u8) {
        self.bus
            .periph
            .uart_pin_input(&UartInput::new(pin, baud, vec![byte]));
        self.bus.irq_dirty = true;
    }
    fn gpio_drive(&self) -> (u64, u64) {
        let gpio = &self.bus.periph.gpio.gpio;
        (gpio.enable, gpio.out)
    }
    fn take_gpio_inputs(&mut self) {
        // what the bus does with them at its next tick (`deliver_board_inputs`)
        for edge in self.bus.board.take_edges() {
            SocBus::gpio_set_input(&mut self.bus, edge.pin, edge.level);
        }
        for pin in self.bus.board.released_inputs() {
            SocBus::gpio_release_input(&mut self.bus, pin);
        }
    }
    fn take_eth_frames(&mut self) -> Vec<Vec<u8>> {
        SocBus::take_ethernet_frames(&mut self.bus)
    }
    fn eth_input(&mut self, frame: &[u8]) -> bool {
        // also refused without an access point, which the run loop never asks for
        SocBus::receive_ethernet_frame(&mut self.bus, frame).is_ok()
    }
    fn wifi_access_point(&mut self, wifi: &cli::Wifi) {
        // a fresh access point, as at power-on: the station must join it again
        let link = &mut self.bus.periph.wifi.link;
        link.attach_new(wifi.access_point(), None, &self.bus.debug);
        link.set_beacons(wifi.nearby.clone());
    }
    fn wifi_leave(&mut self) -> bool {
        let now_us = self.bus.cycles / (esp32::periph::CPU_HZ / 1_000_000);
        let link = &mut self.bus.periph.wifi.link;
        let Some(ap) = link.ap_mut() else {
            return true;
        };
        if ap.state != StaState::Idle {
            // esp32sim's station does not notice beacons that stop coming, so the access point
            // tells it it is leaving, as one that shuts down does, ahead of anything it had
            // queued but beacons; then it beacons no more. Until it has left it still answers a
            // station that tries again, which the next boundary deauthenticates again
            let deauth = deauthentication(ap.sta, ap.cfg.bssid, ap.seq);
            ap.seq = ap.seq.wrapping_add(1);
            ap.queue.retain(|f| wifi::is_beacon(&f.frame));
            ap.queue.push(wifi::AirFrame {
                at_us: now_us,
                frame: deauth,
            });
            ap.state = StaState::Idle;
            ap.next_beacon_us = u64::MAX;
            return false;
        }
        // the deauthentication, or what the access point answered before it, is on its way
        if ap.queue.iter().any(|f| !wifi::is_beacon(&f.frame)) {
            return false;
        }
        link.attach(None, None);
        true
    }
    fn wifi_drops(&self) -> (u64, u64) {
        let link = &self.bus.periph.wifi.link;
        (link.rx_dropped(), link.tx_dropped())
    }
    fn wifi_forget(&mut self) {
        self.bus.periph.wifi.link.attach(None, None);
    }
}

pub struct Options<'a> {
    /// `--max-seconds`, in bus cycles.
    pub end_cycle: u64,
    /// Whether UART0 also goes out as `console` (`--console socket` or `both`).
    pub console: bool,
    /// `--peer-timeout`; `None` waits for ever.
    pub peer_timeout: Option<Duration>,
    /// Set by `SIGTERM` and `SIGINT`.
    pub signal: &'a AtomicBool,
    /// `--wifi`: the access point is on the air from power-on, and the station's frames go to
    /// the peer.
    pub wifi: Option<&'a cli::Wifi>,
    /// The mask ROM's ELF, for a power-on reset.
    pub rom: &'a [u8],
    /// The SD card of `--sd-rw`, which loses its power with the board's.
    pub card: Option<sd_card::Shared>,
}

/// Why the run ended (protocol §9).
#[derive(Debug)]
pub enum End {
    MaxSeconds,
    /// `stop` from the peer.
    Stop,
    Signal,
    /// Any `Stop` other than `Halted` and `SwReset`.
    Emulator(Stop),
    /// A protocol error by the peer, with what it was and the offending line.
    Protocol(String),
    /// The peer is gone.
    PeerGone(String),
}

impl End {
    pub fn exit_code(&self) -> u8 {
        match self {
            End::MaxSeconds | End::Stop | End::Signal => 0,
            End::Emulator(_) => 1,
            End::Protocol(_) => 3,
            End::PeerGone(_) => 4,
        }
    }
}

/// Something that happened in the guest, waiting to become messages.
#[derive(Debug)]
enum Item {
    Board(Recorded),
    Reset {
        cycle: u64,
        cause: u32,
    },
    /// The routes as read at a slice boundary or right after a reset.
    Routes {
        cycle: u64,
        routes: [UartRoute; PORTS],
    },
    /// An Ethernet frame the station sent, as taken from the relay at a slice boundary or right
    /// before a reset.
    Eth {
        cycle: u64,
        data: Vec<u8>,
    },
}

impl Item {
    fn cycle(&self) -> u64 {
        match self {
            Item::Board(Recorded::UartTx { cycle, .. } | Recorded::Gpio { cycle, .. })
            | Item::Reset { cycle, .. }
            | Item::Routes { cycle, .. }
            | Item::Eth { cycle, .. } => *cycle,
        }
    }
}

/// Bytes of one route being coalesced into a message (protocol 5.2).
struct Coalesced {
    t: u64,
    last: u64,
    route: UartRoute,
    data: Vec<u8>,
}

impl Coalesced {
    fn new(t: u64, route: UartRoute, byte: u8) -> Self {
        Self {
            t,
            last: t,
            route,
            data: vec![byte],
        }
    }

    /// Takes the next byte, unless it starts a new message.
    fn extend(&mut self, t: u64, route: UartRoute, byte: u8) -> bool {
        if self.route != route || gap(self.last, t, route.baud) || self.data.len() >= MAX_DATA {
            return false;
        }
        self.data.push(byte);
        self.last = t;
        true
    }
}

/// The message being coalesced or merged: it goes out when any other message is due.
enum Open {
    Uart(Coalesced),
    Warn(Warn),
}

/// A queued input (protocol §8).
enum Input {
    Uart { pin: u8, baud: u32, data: Vec<u8> },
    Gpio(Vec<(u8, bool)>),
    Release(Vec<u8>),
    Eth(Vec<u8>),
    WifiAp(bool),
    Power(bool),
    ChipReset,
}

struct Paced {
    due: u64,
    baud: u32,
    byte: u8,
}

/// One received line: the bytes paced onto it, and when it is free again.
#[derive(Default)]
struct Line {
    free: u64,
    bytes: VecDeque<Paced>,
}

pub struct Session<'a, G: Guest, L: Link> {
    guest: &'a mut G,
    link: &'a mut L,
    shared: Shared,
    opts: Options<'a>,
    watch: u64,
    /// The `seq` of the last event sent.
    seq: u64,
    /// The last `sync`'s `t`, and the `seq` its `wait` must acknowledge.
    sync_t: u64,
    acked: u64,
    quiet_until: Option<u64>,
    latency: Option<u64>,
    /// The `t` of the first event after the last turn.
    first_unacked: Option<u64>,
    /// What the guest did that has not become messages yet: at a turn, what lies after `B`.
    pending: VecDeque<Item>,
    open: Option<Open>,
    /// The `console` message being coalesced. Apart from `open` so that with UART0 watched, the
    /// same bytes in `uart_tx` and `console` do not end each other's message at every byte; the
    /// two end together.
    console: Option<Coalesced>,
    /// Per port, the route of the last `uart_cfg`.
    sent_routes: [UartRoute; PORTS],
    /// Future-dated inputs by `t`, then by arrival (protocol §8).
    inputs: BTreeMap<(u64, u64), Input>,
    arrivals: u64,
    lines: BTreeMap<u8, Line>,
    /// Whether the access point of `--wifi` is on the air, as the peer last switched it.
    ap_on: bool,
    /// Whether the access point is on its way off the air (`Guest::wifi_leave`).
    leaving: bool,
    /// Frames for the station that came due and that its link has not taken yet: its queue
    /// holds 64.
    eth: VecDeque<Vec<u8>>,
    /// The board's supply is off, as the peer last switched it (`power`).
    off: bool,
    stats: Stats,
}

impl<'a, G: Guest, L: Link> Session<'a, G, L> {
    pub fn new(guest: &'a mut G, link: &'a mut L, shared: Shared, opts: Options<'a>) -> Self {
        Self {
            guest,
            link,
            shared,
            watch: 0,
            seq: 0,
            sync_t: 0,
            acked: 0,
            quiet_until: None,
            latency: None,
            first_unacked: None,
            pending: VecDeque::new(),
            open: None,
            console: None,
            // before the first `uart_cfg`, every port counts as having no pins and no baud
            sent_routes: std::array::from_fn(|port| UartRoute::new(port, 0, None, None)),
            inputs: BTreeMap::new(),
            arrivals: 0,
            lines: BTreeMap::new(),
            ap_on: opts.wifi.is_some(),
            leaving: false,
            eth: VecDeque::new(),
            off: false,
            stats: Stats {
                wifi: opts.wifi.map(|_| WifiStats::default()),
                ..Stats::default()
            },
            opts,
        }
    }

    /// The whole run, from `hello` to `stats`.
    pub fn run(mut self) -> (End, Stats) {
        let end = match self.drive() {
            Err(end) => end,
            Ok(never) => match never {},
        };
        let end = self.finish(end);
        (end, self.stats)
    }

    fn drive(&mut self) -> Result<Infallible, End> {
        self.handshake()?;
        // turn 0, before the guest's first instruction (rule 4), after the power-on routes
        let now = self.guest.cycles();
        let routes = self.routes();
        self.pending.push_back(Item::Routes { cycle: now, routes });
        self.process_pending(true)?;
        self.take_turn(0)?;
        loop {
            if self.opts.signal.load(Ordering::Relaxed) {
                return Err(End::Signal);
            }
            self.boundary()?;
            let now = self.guest.cycles();
            if now >= self.opts.end_cycle {
                return Err(End::MaxSeconds);
            }
            if self.off {
                self.guest.idle_until(self.slice_end(now));
                continue;
            }
            match self.guest.run_until(self.slice_end(now)) {
                Stop::Halted => {}
                Stop::SwReset => {
                    // what the guest did before the reset goes out before it
                    self.collect();
                    self.count_wifi_drops();
                    let cause = self.guest.chip_reset();
                    self.reset(cause);
                }
                stop => return Err(End::Emulator(stop)),
            }
        }
    }

    fn handshake(&mut self) -> Result<(), End> {
        self.send(&protocol::hello())?;
        self.flush()?;
        let deadline = self.deadline();
        let line = self.recv(deadline)?;
        match protocol::parse(&line) {
            Ok(Incoming::Hello {
                version,
                chip,
                watch,
            }) => {
                if version != protocol::VERSION {
                    return Err(protocol_error(
                        &format!(
                            "version {version}, the runner speaks version {}",
                            protocol::VERSION
                        ),
                        &line,
                    ));
                }
                if chip != protocol::CHIP {
                    return Err(protocol_error(
                        &format!("chip {chip:?}, the runner runs {:?}", protocol::CHIP),
                        &line,
                    ));
                }
                if let Some(pin) = watch.iter().find(|&&pin| GPIOS & 1 << pin == 0) {
                    return Err(protocol_error(
                        &format!("watch holds GPIO {pin}, which the esp32 does not have"),
                        &line,
                    ));
                }
                self.watch = watch.iter().fold(0, |mask, &pin| mask | 1 << pin);
                self.shared.borrow_mut().watch = self.watch;
                Ok(())
            }
            Ok(_) => Err(protocol_error("expected hello", &line)),
            Err(e) => Err(protocol_error(&e, &line)),
        }
    }

    /// Between two slices, at the bus's current cycle.
    fn boundary(&mut self) -> Result<(), End> {
        let now = self.guest.cycles();
        self.collect();
        let mut routes = self.routes();
        self.pending.push_back(Item::Routes { cycle: now, routes });
        loop {
            loop {
                self.process_pending(false)?;
                match self.block_point() {
                    // a block point already behind the guest is taken at once, without running
                    // it; one at or after the end of the run is not taken
                    Some(b) if cycles(b) <= now && cycles(b) < self.opts.end_cycle => {
                        self.take_turn(b)?;
                    }
                    _ => break,
                }
            }
            if now >= self.opts.end_cycle {
                // nothing more reaches the guest
                return Ok(());
            }
            self.close_after_gap(ns(now))?;
            self.leave();
            if !self.apply_inputs(now)? {
                break;
            }
            // the chip reset or lost its power: what that changed, its routes included, goes
            // out, and may call for a turn, before the guest runs again
            routes = self.routes();
        }
        self.deliver(now, &routes)?;
        self.deliver_eth();
        self.link.idle().map_err(link_end)
    }

    /// Ends a coalesced message once a character time has passed without its next byte, which
    /// would start a new message anyway: so the peer gets it without waiting for the next one.
    fn close_after_gap(&mut self, t: u64) -> Result<(), End> {
        let ended = |m: &Coalesced| gap(m.last, t, m.route.baud);
        if matches!(&self.open, Some(Open::Uart(m)) if ended(m))
            || self.console.as_ref().is_some_and(ended)
        {
            self.close_open()?;
        }
        Ok(())
    }

    /// Where the next slice ends.
    fn slice_end(&self, now: u64) -> u64 {
        let t = ns(now);
        let mut end = self.opts.end_cycle;
        if let Some(b) = self.block_point() {
            end = end.min(cycles(b));
        }
        if self.first_unacked.is_none()
            && let Some(latency) = self.latency
        {
            end = end.min(cycles(t.saturating_add(latency)));
        }
        // a slice without the guest takes no wall time, so nothing waits for its end
        if !self.off {
            end = end.min(cycles((t / MS + 1) * MS));
        }
        if let Some((&(t, _), _)) = self.inputs.first_key_value() {
            end = end.min(cycles(t));
        }
        if let Some(due) = self.next_paced() {
            end = end.min(cycles(due));
        }
        // a latency of 0 costs a turn per event, and a slice of one cycle each
        end.max(now + 1)
    }

    /// `B = min(quiet_until, t_first_unacked + latency)` (rule 1); `None` without either term.
    fn block_point(&self) -> Option<u64> {
        let answer = self
            .first_unacked
            .zip(self.latency)
            .map(|(t, l)| t.saturating_add(l));
        match (self.quiet_until, answer) {
            (Some(q), Some(a)) => Some(q.min(a)),
            (q, a) => q.or(a),
        }
    }

    /// The watched pins' output state after a chip reset, read from the bus, as a `gpio_out`
    /// (protocol 5.5). `reboot()` leaves `GPIO_ENABLE` and `GPIO_OUT` 0 and IO_MUX at function 0,
    /// through which esp32sim drives no pad: no pad is driven, and taking a pad as driven high
    /// where both words have its bit agrees with that. `changes` holds every watched pin whose
    /// level differs from the one last reported in `changes`. With the supply off nothing drives
    /// a pad either.
    fn reset_gpio(&mut self, cycle: u64) -> Recorded {
        let (enable, out) = self.pads();
        let mut shared = self.shared.borrow_mut();
        let now = (enable & self.watch, out & self.watch);
        let level = now.0 & now.1;
        let changed = level ^ shared.levels_reported;
        shared.reported = now;
        shared.levels_reported = level;
        Recorded::Gpio {
            cycle,
            changes: (0..64)
                .filter(|&pin| changed & 1 << pin != 0)
                .map(|pin| (pin, level & 1 << pin != 0))
                .collect(),
            enabled: now.0,
            output: now.1,
        }
    }

    /// The chip's UART routes; none while the supply is off.
    fn routes(&self) -> [UartRoute; PORTS] {
        if self.off {
            return std::array::from_fn(|port| UartRoute::new(port, 0, None, None));
        }
        std::array::from_fn(|port| self.guest.uart_route(port))
    }

    /// `GPIO_ENABLE` and `GPIO_OUT`; nothing drives a pad while the supply is off.
    fn pads(&self) -> (u64, u64) {
        if self.off {
            (0, 0)
        } else {
            self.guest.gpio_drive()
        }
    }

    /// What follows every reset of the chip, at the bus's cycle: the `reset` with its `cause`, and
    /// the routes and pins the reset changed without the hooks that would report them.
    fn reset(&mut self, cause: u32) {
        let cycle = self.guest.cycles();
        eprintln!(
            "esp32tl-runner: chip reset at {:.6} s, cause {cause:#x}",
            ns(cycle) as f64 / 1e9
        );
        self.stats.resets += 1;
        self.pending.push_back(Item::Reset { cycle, cause });
        let routes = self.routes();
        self.pending.push_back(Item::Routes { cycle, routes });
        if self.watch != 0 {
            let state = self.reset_gpio(cycle);
            self.pending.push_back(Item::Board(state));
        }
    }

    /// The chip's power-on reset, from EN (`chip_reset`) or from the supply coming back
    /// (`power`). The station comes back to a fresh access point, as at the start of the run,
    /// which it must join again.
    fn power_on_reset(&mut self) {
        if !std::mem::replace(&mut self.off, false) {
            // what the guest did before the reset goes out before it
            self.collect();
            self.count_wifi_drops();
        }
        let cause = self.guest.power_on_reset(self.opts.rom);
        if let Some(wifi) = self.opts.wifi {
            self.leaving = false;
            if self.ap_on {
                self.guest.wifi_access_point(wifi);
            } else {
                self.guest.wifi_forget();
            }
        }
        self.reset(cause);
    }

    /// The board's supply goes off: the guest stops where it is, no pad is driven and no UART
    /// routed, the SD card loses its power, and the station leaves the air without a word.
    fn power_off(&mut self) {
        self.collect();
        self.count_wifi_drops();
        self.off = true;
        let cycle = self.guest.cycles();
        eprintln!(
            "esp32tl-runner: power off at {:.6} s",
            ns(cycle) as f64 / 1e9
        );
        if let Some(card) = &self.opts.card {
            card.borrow_mut().power_off();
        }
        if self.opts.wifi.is_some() {
            self.leaving = false;
            self.guest.wifi_forget();
        }
        let routes = self.routes();
        self.pending.push_back(Item::Routes { cycle, routes });
        if self.watch != 0 {
            let state = self.reset_gpio(cycle);
            self.pending.push_back(Item::Board(state));
        }
    }

    fn watched(&self, pin: u8) -> bool {
        pin < 64 && self.watch & 1 << pin != 0
    }

    fn touches_watch(&self, route: &UartRoute) -> bool {
        route.tx_pins & self.watch != 0 || route.rx_pin.is_some_and(|p| self.watched(p))
    }

    /// Takes what the board recorded, then the frames the station sent, which the relay holds
    /// without their time: they are stamped with the cycle they are taken at.
    fn collect(&mut self) {
        let log = std::mem::take(&mut self.shared.borrow_mut().log);
        self.pending.extend(log.into_iter().map(Item::Board));
        if self.opts.wifi.is_some() {
            let cycle = self.guest.cycles();
            let frames = self.guest.take_eth_frames();
            self.pending
                .extend(frames.into_iter().map(|data| Item::Eth { cycle, data }));
        }
    }

    /// Adds the station link's drops to the stats before something starts its counts again.
    fn count_wifi_drops(&mut self) {
        if let Some(wifi) = &mut self.stats.wifi {
            let (rx, tx) = self.guest.wifi_drops();
            wifi.rx_dropped += rx;
            wifi.eth_tx_dropped += tx;
        }
    }

    /// Turns pending items into messages, up to the block point unless `all`.
    fn process_pending(&mut self, all: bool) -> Result<(), End> {
        while let Some(item) = self.pending.front() {
            let t = ns(item.cycle());
            if !all && self.block_point().is_some_and(|b| t > b) {
                break;
            }
            let item = self.pending.pop_front().expect("front exists");
            match item {
                Item::Board(Recorded::UartTx { route, byte, .. }) => {
                    self.route_seen(route, t)?;
                    let console = route.port == 0 && self.opts.console;
                    if console {
                        self.console_byte(t, route, byte)?;
                    }
                    if route.tx_pins & self.watch != 0 {
                        self.uart_byte(t, route, byte)?;
                    }
                    // a console message ends after a newline (protocol 5.7)
                    if console && byte == b'\n' {
                        self.close_open()?;
                    }
                }
                Item::Board(Recorded::Gpio {
                    changes,
                    enabled,
                    output,
                    ..
                }) => {
                    self.event(t, |seq| {
                        protocol::gpio_out(seq, t, &changes, enabled, output)
                    })?;
                }
                Item::Reset { cause, .. } => {
                    self.event(t, |seq| protocol::reset(seq, t, cause))?;
                }
                Item::Routes { routes, .. } => {
                    for route in routes {
                        self.route_seen(route, t)?;
                    }
                }
                Item::Eth { data, .. } => {
                    if let Some(wifi) = &mut self.stats.wifi {
                        wifi.eth_tx += 1;
                    }
                    self.event(t, |seq| protocol::eth_tx(seq, t, &data))?;
                }
            }
        }
        Ok(())
    }

    /// `uart_cfg` when a port's route differs from the last one sent and either touches `watch`.
    fn route_seen(&mut self, route: UartRoute, t: u64) -> Result<(), End> {
        let port = route.port;
        let last = self.sent_routes[port];
        if route == last || !(self.touches_watch(&route) || self.touches_watch(&last)) {
            return Ok(());
        }
        if self.touches_watch(&route) {
            self.stats.uart[port].watched = true;
        }
        self.sent_routes[port] = route;
        self.event(t, |seq| protocol::uart_cfg(seq, t, &route))
    }

    /// Sends an event other than `uart_tx`.
    fn event(&mut self, t: u64, line: impl FnOnce(u64) -> String) -> Result<(), End> {
        self.close_open()?;
        self.seq += 1;
        self.first_unacked.get_or_insert(t);
        let line = line(self.seq);
        self.send(&line)
    }

    fn uart_byte(&mut self, t: u64, route: UartRoute, byte: u8) -> Result<(), End> {
        self.stats.uart[route.port].tx_bytes += 1;
        if let Some(Open::Uart(m)) = &mut self.open
            && m.extend(t, route, byte)
        {
            return Ok(());
        }
        // a console message with the same bytes has ended on this one already
        if !matches!(&self.console, Some(c) if c.t == t) {
            self.close_open()?;
        } else if let Some(open) = self.open.take() {
            self.write_open(open)?;
        }
        // the event exists from its first byte, whenever the message goes out
        self.first_unacked.get_or_insert(t);
        self.open = Some(Open::Uart(Coalesced::new(t, route, byte)));
        Ok(())
    }

    fn console_byte(&mut self, t: u64, route: UartRoute, byte: u8) -> Result<(), End> {
        if let Some(c) = &mut self.console
            && c.extend(t, route, byte)
        {
            return Ok(());
        }
        self.close_open()?;
        self.console = Some(Coalesced::new(t, route, byte));
        Ok(())
    }

    /// A dropped byte: merged into the open `warn` of the same kind, port and pin, if any.
    fn warn(
        &mut self,
        t: u64,
        kind: WarnKind,
        port: Option<usize>,
        pin: u8,
        baud: Option<u32>,
    ) -> Result<(), End> {
        if let Some(Open::Warn(w)) = &mut self.open
            && kind != WarnKind::InputOnOutput
            && w.kind == kind
            && w.port == port
            && w.pin == pin
        {
            w.bytes += 1;
            return Ok(());
        }
        self.close_open()?;
        self.open = Some(Open::Warn(Warn {
            t,
            kind,
            port,
            pin,
            baud,
            bytes: 1,
        }));
        Ok(())
    }

    /// Sends the open messages, the earlier first.
    fn close_open(&mut self) -> Result<(), End> {
        let console = self.console.take();
        let open = self.open.take();
        let open_t = match &open {
            Some(Open::Uart(m)) => m.t,
            Some(Open::Warn(w)) => w.t,
            None => u64::MAX,
        };
        let console_first = console.as_ref().is_some_and(|c| c.t <= open_t);
        if console_first && let Some(c) = &console {
            self.send(&protocol::console(c.t, &c.data))?;
        }
        if let Some(open) = open {
            self.write_open(open)?;
        }
        if !console_first && let Some(c) = &console {
            self.send(&protocol::console(c.t, &c.data))?;
        }
        Ok(())
    }

    fn write_open(&mut self, open: Open) -> Result<(), End> {
        let line = match open {
            Open::Uart(m) => {
                self.seq += 1;
                protocol::uart_tx(self.seq, m.t, &m.route, &m.data)
            }
            Open::Warn(w) => protocol::warn(&w),
        };
        self.send(&line)
    }

    /// The turn at block point `b`: `sync`, then the peer's inputs up to its `wait`.
    fn take_turn(&mut self, b: u64) -> Result<(), End> {
        self.close_open()?;
        self.send(&protocol::sync(b))?;
        self.flush()?;
        self.sync_t = b;
        self.acked = self.seq;
        self.first_unacked = None;
        self.stats.turns += 1;
        let deadline = self.deadline();
        let mut inputs = Vec::new();
        loop {
            let line = self.recv(deadline)?;
            let msg = protocol::parse(&line).map_err(|e| protocol_error(&e, &line))?;
            // an input's time and pins (rule 2, protocol 5.1)
            let check = |t: u64, mut pins: Vec<u8>| {
                if t < b {
                    return Err(protocol_error(
                        &format!("t {t} is before this turn's sync at {b} (rule 2)"),
                        &line,
                    ));
                }
                pins.retain(|&pin| !self.watched(pin));
                match pins.first() {
                    Some(pin) => Err(protocol_error(
                        &format!("GPIO {pin} is not in the peer's watch"),
                        &line,
                    )),
                    None => Ok(()),
                }
            };
            match msg {
                Incoming::UartRx { t, pin, baud, data } => {
                    check(t, vec![pin])?;
                    if baud == 0 {
                        return Err(protocol_error("a baud of 0", &line));
                    }
                    inputs.push((t, Input::Uart { pin, baud, data }));
                }
                Incoming::GpioIn { t, levels } => {
                    check(t, levels.iter().map(|&(pin, _)| pin).collect())?;
                    inputs.push((t, Input::Gpio(levels)));
                }
                Incoming::Release { t, pins } => {
                    check(t, pins.clone())?;
                    inputs.push((t, Input::Release(pins)));
                }
                Incoming::EthRx { t, data } => {
                    check(t, Vec::new())?;
                    if self.opts.wifi.is_none() {
                        return Err(protocol_error("eth_rx without --wifi", &line));
                    }
                    inputs.push((t, Input::Eth(data)));
                }
                Incoming::WifiAp { t, on } => {
                    check(t, Vec::new())?;
                    if self.opts.wifi.is_none() {
                        return Err(protocol_error("wifi_ap without --wifi", &line));
                    }
                    inputs.push((t, Input::WifiAp(on)));
                }
                Incoming::Power { t, on } => {
                    check(t, Vec::new())?;
                    inputs.push((t, Input::Power(on)));
                }
                Incoming::ChipReset { t } => {
                    check(t, Vec::new())?;
                    inputs.push((t, Input::ChipReset));
                }
                Incoming::Wait {
                    ack,
                    quiet_until,
                    latency,
                } => {
                    if ack != self.acked {
                        return Err(protocol_error(
                            &format!(
                                "ack {ack}, but the last event before this sync was seq {}",
                                self.acked
                            ),
                            &line,
                        ));
                    }
                    if let Some(q) = quiet_until
                        && q <= b
                    {
                        return Err(protocol_error(
                            &format!("quiet_until {q} is not after this turn's sync at {b}"),
                            &line,
                        ));
                    }
                    self.quiet_until = quiet_until;
                    self.latency = latency;
                    for (t, input) in inputs {
                        self.arrivals += 1;
                        self.inputs.insert((t, self.arrivals), input);
                    }
                    return Ok(());
                }
                Incoming::Stop => return Err(End::Stop),
                Incoming::Hello { .. } => {
                    return Err(protocol_error("hello after the handshake", &line));
                }
                Incoming::NotYet(kind) => {
                    return Err(protocol_error(
                        &format!("{kind} is not implemented yet"),
                        &line,
                    ));
                }
            }
        }
    }

    /// Applies every input now due, in the queue's order: received bytes go on their lines
    /// (rule 5), GPIO levels and releases to the board, which hands them to the chip at once.
    /// Returns whether the chip reset or lost its power. EN pulsed while the supply is off, and
    /// a `power` that leaves the supply as it was, change nothing.
    fn apply_inputs(&mut self, now: u64) -> Result<bool, End> {
        let mut gpio = false;
        let mut reset = false;
        while let Some(entry) = self.inputs.first_entry() {
            let t = entry.key().0;
            if cycles(t) > now {
                break;
            }
            match entry.remove() {
                Input::Uart { pin, baud, data } => {
                    let line = self.lines.entry(pin).or_default();
                    let d0 = t.max(line.free);
                    for (k, &byte) in data.iter().enumerate() {
                        line.bytes.push_back(Paced {
                            due: d0 + char_ns(k as u64, baud),
                            baud,
                            byte,
                        });
                    }
                    line.free = d0 + char_ns(data.len() as u64, baud);
                }
                Input::Gpio(levels) => {
                    let enabled = self.pads().0;
                    for (pin, level) in levels {
                        if enabled & 1 << pin != 0 {
                            self.stats.inputs_on_output += 1;
                            self.warn(ns(now), WarnKind::InputOnOutput, None, pin, None)?;
                        }
                        self.shared.borrow_mut().drive(now, pin, level);
                    }
                    gpio = true;
                }
                Input::Release(pins) => {
                    for pin in pins {
                        self.shared.borrow_mut().release(pin);
                    }
                    gpio = true;
                }
                Input::Eth(data) => {
                    self.eth.push_back(data);
                    self.deliver_eth();
                }
                Input::WifiAp(on) => self.switch_access_point(on),
                Input::Power(on) if on == self.off => {
                    if on {
                        self.power_on_reset();
                    } else {
                        self.power_off();
                    }
                    reset = true;
                }
                Input::ChipReset if !self.off => {
                    self.power_on_reset();
                    reset = true;
                }
                Input::Power(_) | Input::ChipReset => {}
            }
        }
        if gpio {
            self.guest.take_gpio_inputs();
        }
        Ok(reset)
    }

    /// Hands the station's link the frames that came due, in order, as many as its queue takes;
    /// the rest wait for the next slice boundary. Without the access point on the air, or while
    /// it is leaving, they are dropped: the station cannot hear them.
    fn deliver_eth(&mut self) {
        let Some(wifi) = &mut self.stats.wifi else {
            return;
        };
        while let Some(frame) = self.eth.front() {
            if !self.ap_on || self.leaving || self.off {
                wifi.eth_rx_dropped += 1;
            } else if self.guest.eth_input(frame) {
                wifi.eth_rx += 1;
            } else {
                break;
            }
            self.eth.pop_front();
        }
    }

    /// The access point goes off the air (`Guest::wifi_leave`), or comes back as a fresh one. One
    /// that comes back while the old one is still leaving waits for it to have left, so that the
    /// station has had its deauthentication: a fresh access point would take it off the queue.
    fn switch_access_point(&mut self, on: bool) {
        let Some(wifi) = self.opts.wifi else {
            return;
        };
        if on == self.ap_on {
            return;
        }
        // whatever came due before the switch goes first
        self.deliver_eth();
        self.ap_on = on;
        if on {
            if self.leaving {
                // `leave` puts it back once the old one has left
                return;
            }
            // the old one has left already, and its counts with it
            self.guest.wifi_access_point(wifi);
        } else {
            self.leaving = true;
            self.leave();
        }
        // frames the link held go off the air with it
        self.deliver_eth();
    }

    /// Asks a leaving access point whether it has left, with the link's counts read just before.
    fn leave(&mut self) {
        if !self.leaving {
            return;
        }
        let (rx, tx) = self.guest.wifi_drops();
        if self.guest.wifi_leave() {
            self.leaving = false;
            if let Some(wifi) = &mut self.stats.wifi {
                wifi.rx_dropped += rx;
                wifi.eth_tx_dropped += tx;
            }
            // the peer put it back on the air while it was leaving
            if self.ap_on
                && let Some(wifi) = self.opts.wifi
            {
                self.guest.wifi_access_point(wifi);
            }
        }
    }

    fn next_paced(&self) -> Option<u64> {
        self.lines
            .values()
            .filter_map(|line| line.bytes.front().map(|p| p.due))
            .min()
    }

    /// Delivers the paced bytes now due, in time order, and reports what the chip drops.
    /// `routes` are the routes at `now`: delivering a byte does not change them.
    fn deliver(&mut self, now: u64, routes: &[UartRoute; PORTS]) -> Result<(), End> {
        let t = ns(now);
        loop {
            let next = self
                .lines
                .iter()
                .filter_map(|(&pin, line)| line.bytes.front().map(|p| (p.due, pin)))
                .min();
            let Some((due, pin)) = next else { break };
            if cycles(due) > now {
                break;
            }
            let paced = self
                .lines
                .get_mut(&pin)
                .and_then(|line| line.bytes.pop_front())
                .expect("front exists");
            let mut routed = false;
            for (port, route) in routes.iter().enumerate() {
                if route.rx_pin != Some(pin) {
                    continue;
                }
                routed = true;
                if !route.matches_baud(paced.baud) {
                    self.stats.uart[port].frm_err += 1;
                    self.warn(t, WarnKind::FrmErr, Some(port), pin, route.baud)?;
                } else if self.guest.rx_pending(port) >= RX_FIFO {
                    self.stats.uart[port].rxfifo_ovf += 1;
                    self.warn(t, WarnKind::RxFifoOvf, Some(port), pin, None)?;
                } else {
                    self.stats.uart[port].rx_bytes += 1;
                }
            }
            if !routed {
                self.stats.unrouted_rx_bytes += 1;
                self.warn(t, WarnKind::UnroutedRx, None, pin, None)?;
            }
            if !self.off {
                self.guest.uart_input(pin, paced.baud, paced.byte);
            }
        }
        Ok(())
    }

    /// The last messages: what is not sent yet where the end calls for it, then `stats`. A write
    /// that fails here changes nothing: the first cause decides the exit code.
    fn finish(&mut self, end: End) -> End {
        self.link.closing();
        let t = match &end {
            End::PeerGone(_) => return end,
            End::MaxSeconds | End::Signal | End::Emulator(_) => {
                self.collect();
                if self
                    .process_pending(true)
                    .and_then(|()| self.close_open())
                    .is_err()
                {
                    return end;
                }
                ns(self.guest.cycles())
            }
            // a stop drops what the guest did after B; a protocol error ends the run in a turn
            End::Stop | End::Protocol(_) => self.sync_t,
        };
        self.count_wifi_drops();
        self.stats.inputs_not_applied = self.inputs.len() as u64;
        self.stats.rx_bytes_not_paced = self.lines.values().map(|l| l.bytes.len() as u64).sum();
        let line = match &end {
            End::MaxSeconds => protocol::stats(t, "max_seconds", &[], &self.stats),
            End::Stop => protocol::stats(t, "stop", &[], &self.stats),
            End::Signal => protocol::stats(t, "signal", &[], &self.stats),
            End::Emulator(stop) => protocol::stats(
                t,
                "emulator",
                &[
                    ("stop", crate::stop_name(stop)),
                    ("detail", &format!("{stop:?}")),
                ],
                &self.stats,
            ),
            End::Protocol(detail) => {
                protocol::stats(t, "protocol_error", &[("detail", detail)], &self.stats)
            }
            End::PeerGone(_) => unreachable!("returned above"),
        };
        if self.send(&line).and_then(|()| self.flush()).is_ok() {
            self.link.close();
        }
        end
    }

    fn deadline(&self) -> Option<Instant> {
        // a deadline past what an Instant holds is none
        self.opts
            .peer_timeout
            .and_then(|d| Instant::now().checked_add(d))
    }

    fn send(&mut self, line: &str) -> Result<(), End> {
        self.link.send(line).map_err(link_end)
    }

    fn flush(&mut self) -> Result<(), End> {
        self.link.flush().map_err(link_end)
    }

    fn recv(&mut self, deadline: Option<Instant>) -> Result<String, End> {
        self.link.recv(deadline).map_err(link_end)
    }
}

/// A deauthentication from the access point at `bssid` to the station `sta`, reason 3: "the
/// sending station is leaving" (IEEE 802.11-2020 9.3.3.12 and Table 9-49).
fn deauthentication(sta: [u8; 6], bssid: [u8; 6], seq: u16) -> Vec<u8> {
    let mut frame = vec![0xc0, 0, 0, 0];
    frame.extend_from_slice(&sta);
    frame.extend_from_slice(&bssid);
    frame.extend_from_slice(&bssid);
    frame.extend_from_slice(&(seq << 4).to_le_bytes());
    frame.extend_from_slice(&3u16.to_le_bytes());
    frame
}

/// Whether the next byte comes more than one character time after the previous one.
fn gap(last: u64, t: u64, baud: Option<u32>) -> bool {
    baud.is_some_and(|b| u128::from(t - last) * u128::from(b) > 10_000_000_000)
}

fn link_end(e: LinkError) -> End {
    match e {
        LinkError::Gone(m) => End::PeerGone(m),
        LinkError::Protocol(m) => End::Protocol(m),
        LinkError::Signal => End::Signal,
    }
}

/// What was wrong, and the line, cut short if it is long.
fn protocol_error(what: &str, line: &str) -> End {
    const SHOWN: usize = 200;
    let shown = match line.char_indices().nth(SHOWN) {
        Some((i, _)) => format!("{}...", &line[..i]),
        None => line.to_string(),
    };
    End::Protocol(format!("{what}: {shown}"))
}

// In a file of its own, so that `make docs-check` tells a test-only change by its path.
#[cfg(test)]
mod tests;
