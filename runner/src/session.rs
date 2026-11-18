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
//! With `--bt` the guest's H4 packets go to the peer (`hci_tx`), each at the cycle the guest's
//! host sent it, merged into the board's log by time; the peer's packets go to the guest's host
//! when they come due (`hci_rx`), and it reads them at its controller task's next VHCI poll.
//!
//! The peer can also pulse the chip's EN pin (`chip_reset`) and switch the board's supply
//! (`power`). Either way the chip comes back through a power-on reset, which the peer sees as
//! `reset` with cause 1. While the supply is off the guest runs nothing and its time still passes:
//! the slices go on, without the guest, so that the peer's turns and inputs keep their times.
//!
//! With `--sd-rw` the peer's fault rules go to the SD card when they come due (`sd_fault`,
//! `sd_clear`), and what they did comes back through the board's log (`sd_op`).
//!
//! With `--sd-spi` the peer takes the card out of the slot and puts one in (`sd_card`) when its
//! time comes, at a slice boundary, between two of the chip's SPI transactions; the runner says
//! what the slot holds then (`sd_card_ack`).
//!
//! With `--flash-out` the peer's `flash_dump` writes the whole flash to a file in the turn it comes
//! in, which goes on, and so does the end of the run, however it ends (protocol 5.21).
//!
//! Under `--realtime` the run is paced to wall time (protocol 12): esp32sim paces `run` from the
//! first slice boundary at or after 2^16 bus cycles, and the runner keeps a wall clock of its own
//! from that moment, on which it waits for each block point before it takes the turn there.
//!
//! [`Guest`] and [`Link`] stand for the machine and the socket, so the tests run the loop
//! against a scripted guest and a fake peer.
use crate::cli;
use crate::hci;
use crate::protocol::{
    self, Incoming, Stats, Warn, WarnKind, WifiStats, char_ns, cycles, ns, ns_up,
};
use crate::sd_card;
use crate::socket_board::{Recorded, Shared};
use crate::transport::{Link, LinkError};
use esp_soc::Stop;
use esp_soc::uart::UartRoute;
use std::borrow::Cow;
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::convert::Infallible;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

/// The most UARTs a chip has: a chip with fewer reports the others' routes as disabled.
pub const PORTS: usize = 3;
/// The classic RX FIFO (`esp-periph/src/uart.rs:9`): what does not fit is dropped.
const RX_FIFO: usize = 128;
/// The longest `data` of one message, so that a line stays well under the 1 MiB limit.
const MAX_DATA: usize = 48 << 10;
const MS: u64 = 1_000_000;
/// esp32sim's first pacing check comes in the first round, with pacing on, at or after this many
/// bus cycles since power-on (`rt.last_check` starts at 0): pacing starts no sooner (protocol 12.2).
const PACING_FROM: u64 = 1 << 16;

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
    /// One received byte into `port`'s RX FIFO, at a baud its route matches; with the FIFO full
    /// the UART drops it and raises `UART_RXFIFO_OVF_INT`, and it reaches the chip neither now nor
    /// later.
    fn uart_receive(&mut self, port: usize, byte: u8);
    /// A received byte at a baud `port`'s route does not match: the UART drops it and raises
    /// `UART_FRM_ERR_INT`.
    fn uart_frame_error(&mut self, port: usize);
    /// `GPIO_ENABLE` and `GPIO_OUT`, the words `gpio_output_at` reports.
    fn gpio_drive(&self) -> (u64, u64);
    /// Has the chip take the levels and releases the run loop handed the board, now rather than
    /// at the bus's next tick.
    fn take_gpio_inputs(&mut self);
    // The USB-Serial/JTAG console, asked only with `--console-port usb`: the defaults are a chip
    // without the controller.

    /// What the guest wrote to its USB-Serial/JTAG controller since the last call, as the host
    /// read it: the bytes of every packet the controller flushed.
    fn take_usb_console(&mut self) -> Vec<u8> {
        Vec::new()
    }
    /// Bytes from the host into the USB-Serial/JTAG controller, as USB packets of at most 64
    /// bytes, each taken into the RX FIFO once the guest has read the one before.
    fn usb_console_input(&mut self, _data: &[u8]) {}
    // Wi-Fi, asked with `--wifi` or `--wifi-sta`: the defaults are a chip without it.

    /// Data readiness and completed joins; absent outside virtual station mode.
    fn station_state(&self) -> Option<(bool, u64)> {
        None
    }
    fn clear_station_relay(&mut self) {}
    /// The Ethernet frames since the last call, in relay order.
    fn take_eth_frames(&mut self) -> Vec<Vec<u8>> {
        Vec::new()
    }
    /// Hands the station's link one Ethernet frame for the station; `false` when its queue is
    /// full and the frame was not taken.
    fn eth_input(&mut self, _frame: &[u8]) -> bool {
        false
    }
    /// Puts the selected virtual Wi-Fi role on the air afresh. The relay
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
    // Pacing, asked only with `--realtime`: the defaults are a guest that never falls behind.

    /// Turns esp32sim's pacing to wall time on.
    fn start_pacing(&mut self) {}
    /// How many times esp32sim has skipped a lag behind wall time.
    fn resyncs(&self) -> u64 {
        0
    }
    // The flash, asked only with `--flash-out`: the default is a chip without any.

    /// The whole flash, as the guest has left it.
    fn flash(&self) -> Cow<'_, [u8]> {
        Cow::Borrowed(&[])
    }
    // Deep sleep: the defaults are a chip that never sleeps.

    /// Whether the chip is in a deep sleep the run loop steps through: its digital domain off,
    /// nothing running in it, so that nothing but its wake can happen there, and nothing in the
    /// RTC that would act later in a long step than in the chip's own rounds.
    fn sleeps_through(&self) -> bool {
        false
    }
    /// While `sleeps_through`: the bus's clock moves on towards `cycle` without running the cores,
    /// in long steps, and stops short of the RTC timer's alarm, so that esp32sim's own rounds wake
    /// the chip at its tick. Does nothing otherwise.
    fn sleep_until(&mut self, _cycle: u64) {}
    // The uptime counter: the default is a chip without one the runner can read.

    /// The counter `esp_timer_get_time()` reads, as the chip has it now (protocol 5.24).
    fn uptime(&mut self) -> Uptime {
        Uptime::Stopped
    }
}

/// What the chip's uptime counter, the one ESP-IDF's `esp_timer` reads, is doing at a slice
/// boundary (protocol 5.24).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Uptime {
    /// Not counting.
    Stopped,
    /// Counting, from `load`, the count it was last loaded with, at `tick` bus cycles a count:
    /// what the runner reads tells it that the counter runs, not where it is now.
    Counting { load: u64, tick: u64 },
    /// Counting, and it read zero no sooner than `earliest` and no later than `latest`, bus
    /// cycles: a count read between two of its ticks says where it is to within one.
    Zero { earliest: u64, latest: u64 },
    /// Counting, and it read zero before power-on, which no time on the socket names: it was
    /// loaded with more than the run has lasted.
    BeforePowerOn,
}

/// Where the uptime counter of the boot that is running started, as far as the slice boundaries
/// have shown it (protocol 5.24).
#[derive(Clone, Copy, Debug)]
struct UptimeWatch {
    /// The last boundary, the reset's cycle at first: a start or a load the next boundary sees
    /// came after it.
    since: u64,
    /// The load the last boundary read the counter counting from, if it read it counting.
    load: Option<u64>,
    /// A load seen while the counter counted, and the boundary before the one that saw it: it
    /// is placed at the next boundary, once the guest has surely written LACTLOAD.
    reload: Option<(u64, u64)>,
    /// What the boot's last `uptime` said.
    sent: Sent,
}

/// What the boot's last `uptime` said.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Sent {
    /// Nothing: no `uptime` in this boot yet.
    Nothing,
    /// Its counter read zero between these two cycles.
    Zero(u64, u64),
    /// Where its counter reads zero is no longer known: `earliest` and `latest` were `null`.
    Unknown,
}

impl UptimeWatch {
    fn new(cycle: u64) -> Self {
        Self {
            since: cycle,
            load: None,
            reload: None,
            sent: Sent::Nothing,
        }
    }
}

pub struct Options<'a> {
    /// `--max-seconds`, in bus cycles.
    pub end_cycle: u64,
    /// Whether the console also goes out as `console` (`--console socket` or `both`).
    pub console: bool,
    /// `--console-port usb`: the console is the USB-Serial/JTAG controller, not UART0, and the
    /// peer's `console_rx` is its input.
    pub usb_console: bool,
    /// `--peer-timeout`; `None` waits for ever.
    pub peer_timeout: Option<Duration>,
    /// Set by `SIGTERM` and `SIGINT`.
    pub signal: &'a AtomicBool,
    /// `--wifi`: the access point is on the air from power-on, and the station's frames go to
    /// the peer.
    pub wifi: Option<&'a cli::Wifi>,
    /// The mask ROM's ELF, for a power-on reset.
    pub rom: &'a [u8],
    /// `--sd-spi`: the SD card slot, which the peer's `sd_card` empties and fills, and whose card
    /// loses its power with the board's.
    pub card: Option<sd_card::Shared>,
    /// `--realtime`: the run is paced to wall time.
    pub realtime: bool,
    /// `--flash-out`: where `flash_dump` and the end of the run write the flash.
    pub flash_out: Option<&'a Path>,
    /// `--bt`: what the forwarder, the guest's Bluetooth controller, shares with the run loop.
    pub hci: Option<hci::Shared>,
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
    /// The virtual station data link, sampled at a slice boundary.
    WifiLink {
        cycle: u64,
        ready: bool,
    },
    /// An Ethernet frame taken from the relay at a slice boundary.
    Eth {
        cycle: u64,
        data: Vec<u8>,
    },
    /// An H4 packet the guest's Bluetooth host sent, at the cycle it sent it.
    Hci {
        cycle: u64,
        data: Vec<u8>,
    },
    /// What the guest wrote to its USB-Serial/JTAG console in a slice, taken at its end.
    UsbConsole {
        cycle: u64,
        data: Vec<u8>,
    },
    /// What the slot holds after the peer's `sd_card`, applied at `cycle`, and why it was
    /// refused, if it was.
    SdCard {
        cycle: u64,
        image: Option<String>,
        error: Option<String>,
    },
    /// Where the uptime counter counted zero, seen at the slice boundary at `cycle`: no sooner
    /// than the first cycle, no later than the second; `None` when that is no longer known.
    Uptime {
        cycle: u64,
        zero: Option<(u64, u64)>,
    },
}

impl Item {
    fn cycle(&self) -> u64 {
        match self {
            Item::Board(Recorded::Sd(sd_card::Faulted { cycle, .. }))
            | Item::Board(Recorded::UartTx { cycle, .. } | Recorded::Gpio { cycle, .. })
            | Item::Reset { cycle, .. }
            | Item::Routes { cycle, .. }
            | Item::WifiLink { cycle, .. }
            | Item::Eth { cycle, .. }
            | Item::Hci { cycle, .. }
            | Item::UsbConsole { cycle, .. }
            | Item::SdCard { cycle, .. }
            | Item::Uptime { cycle, .. } => *cycle,
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
    Hci(Vec<u8>),
    UsbConsole(Vec<u8>),
    WifiAp(bool),
    Power(bool),
    ChipReset,
    SdFault(sd_card::Fault),
    SdClear(Option<Vec<u64>>),
    SdCard(Option<String>),
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
    station_state: Option<(bool, u64)>,
    /// The board's supply is off, as the peer last switched it (`power`).
    off: bool,
    /// Where the uptime counter of this boot started (`uptime`).
    uptime: UptimeWatch,
    /// Every `sd_fault` id the peer has given, queued rules' included: an id is used once.
    sd_ids: BTreeSet<u64>,
    /// Under `--realtime`, once pacing has started: this wall instant is this emulated time, in
    /// nanoseconds.
    anchor: Option<(Instant, u64)>,
    /// The resyncs the anchor has followed.
    resyncs: u64,
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
            station_state: None,
            off: false,
            uptime: UptimeWatch::new(0),
            sd_ids: BTreeSet::new(),
            anchor: None,
            resyncs: 0,
            stats: Stats {
                wifi: opts.wifi.map(|_| WifiStats::default()),
                console_rx_lost: opts.usb_console.then_some(0),
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
        // before `stats`: a peer that has them finds the file in place
        if let Some(path) = self.opts.flash_out {
            write_flash_at_end(path, &self.guest.flash());
        }
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
            self.start_pacing(now);
            let end = self.slice_end(now);
            if self.skips_sleep() {
                self.guest.sleep_until(end);
                if self.guest.cycles() >= end {
                    continue;
                }
            }
            match self.guest.run_until(end) {
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
        let clock = if self.opts.realtime {
            protocol::WALL
        } else {
            protocol::CLOCK
        };
        self.send(&protocol::hello(clock))?;
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
                let profile = crate::chip::selected();
                if chip != profile.name {
                    return Err(protocol_error(
                        &format!("chip {chip:?}, the runner runs {:?}", profile.name),
                        &line,
                    ));
                }
                if let Some(pin) = watch.iter().find(|&&pin| profile.gpios & 1 << pin == 0) {
                    return Err(protocol_error(
                        &format!(
                            "watch holds GPIO {pin}, which the {} does not have",
                            profile.name
                        ),
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
        self.follow_resyncs(now);
        self.collect();
        let mut routes = self.routes();
        self.pending.push_back(Item::Routes { cycle: now, routes });
        self.watch_uptime(now);
        loop {
            loop {
                self.process_pending(false)?;
                match self.block_point() {
                    // a block point already behind the guest is taken at once, without running
                    // it; one at or after the end of the run is not taken
                    Some(b) if cycles(b) <= now && cycles(b) < self.opts.end_cycle => {
                        self.wait_for_wall_time(b)?;
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

    /// Under `--realtime`, about to run the guest: at the first slice boundary at or after
    /// [`PACING_FROM`], turns esp32sim's pacing on and starts the runner's wall clock with its
    /// convention, that emulated time 0 is now. esp32sim's own anchor is private, and its first
    /// check comes in the first round of the `run` that follows, so the two agree to within that,
    /// whatever turns were held before (protocol 12.2).
    fn start_pacing(&mut self, now: u64) {
        if self.opts.realtime && self.anchor.is_none() && now >= PACING_FROM {
            self.guest.start_pacing();
            self.anchor = Some((Instant::now(), 0));
        }
    }

    /// When esp32sim has skipped a lag since the last slice boundary, `now` is now, as it moved
    /// its own anchor; the two then differ by less than the wall time of one slice.
    fn follow_resyncs(&mut self, now: u64) {
        if self.anchor.is_none() {
            return;
        }
        let resyncs = self.guest.resyncs();
        if resyncs != self.resyncs {
            self.resyncs = resyncs;
            self.anchor = Some((Instant::now(), ns(now)));
        }
    }

    /// Waits until the wall clock reaches the block point `b`, so that what the peer's device sent
    /// up to `b` has come when the peer takes the turn there. Not part of the peer's turn, so
    /// `--peer-timeout` does not count it. A guest behind wall time does not wait.
    fn wait_for_wall_time(&mut self, b: u64) -> Result<(), End> {
        let Some((anchor, at)) = self.anchor else {
            return Ok(());
        };
        let until = anchor + Duration::from_nanos(b.saturating_sub(at));
        self.link.wait_until(until).map_err(link_end)
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

    /// Whether the run skips a deep sleep in long steps: not under `--realtime`, which paces the
    /// sleep to wall time like any other stretch of the run.
    fn skips_sleep(&self) -> bool {
        !self.opts.realtime
    }

    /// Where the next slice ends.
    fn slice_end(&self, now: u64) -> u64 {
        let t = ns(now);
        let mut end = self.opts.end_cycle;
        if let Some(b) = self.block_point() {
            end = end.min(cycles(b));
        }
        // in deep sleep the only event is the wake, which ends the slice at its cycle, and the
        // routes cannot change: the slice goes on to the next input, turn or end of the run
        let asleep = !self.off && self.skips_sleep() && self.guest.sleeps_through();
        if self.first_unacked.is_none()
            && !asleep
            && let Some(latency) = self.latency
        {
            end = end.min(cycles(t.saturating_add(latency)));
        }
        // a slice without the guest takes no wall time, so nothing waits for its end
        if !self.off && !asleep {
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
    /// GPIO with its output disabled on some pads and driving nothing on the others: no pad is
    /// driven, and taking a pad as driven high where both words have its bit agrees with that.
    /// `changes` holds every watched pin whose level differs from the one last reported in
    /// `changes`. With the supply off nothing drives a pad either.
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
        // the reset stops the counter: the new boot's starts from it at the earliest
        self.uptime = UptimeWatch::new(cycle);
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

    /// At the slice boundary at `now`, where the uptime counter of this boot counted zero, when
    /// the boundary shows something new of it: `uptime` when the counter is first seen counting,
    /// again when it starts anew, is loaded anew or reads a zero outside the one sent, and with
    /// no zero when it stops or reads a zero before power-on after a zero was sent. While the
    /// supply is off the chip has no counter.
    fn watch_uptime(&mut self, now: u64) {
        if self.off {
            return;
        }
        let since = std::mem::replace(&mut self.uptime.since, now);
        let read = self.guest.uptime();
        let last_load = match read {
            Uptime::Counting { load, .. } => self.uptime.load.replace(load),
            _ => self.uptime.load.take(),
        };
        if !matches!(read, Uptime::Counting { .. }) {
            self.uptime.reload = None;
        }
        let zero = match read {
            Uptime::Counting { load, tick } => {
                let from = match (last_load, self.uptime.reload) {
                    // started after the last boundary, which read it stopped: LACT_EN comes
                    // after the load, so the load the boundary reads is the one it counts from
                    (None, _) => since,
                    // loaded while it counted, after the boundary before the last: the last
                    // may have read LACTLOADLO and LACTLOADHI before LACTLOAD took them, so
                    // the load is placed now, when it has surely been taken
                    (Some(last), Some((from, pending))) if last == load && pending == load => {
                        self.uptime.reload = None;
                        from
                    }
                    (Some(last), _) if last == load => return,
                    (Some(_), _) => {
                        self.uptime.reload = Some((since, load));
                        return;
                    }
                };
                // a count loaded with more than the run had lasted at `from` may count from
                // before power-on, which has no time
                let counted = load.saturating_mul(tick);
                from.checked_sub(counted)
                    .map(|earliest| (earliest, now - counted))
            }
            Uptime::Zero { earliest, latest } => {
                // the same zero, read at another part of a tick: what was sent holds
                if let Sent::Zero(e, l) = self.uptime.sent
                    && earliest <= l
                    && e <= latest
                {
                    return;
                }
                Some((earliest, latest))
            }
            Uptime::Stopped | Uptime::BeforePowerOn => None,
        };
        // no zero: one sent before no longer holds; nothing sent, or `null` sent, says so already
        if zero.is_none() && !matches!(self.uptime.sent, Sent::Zero(..)) {
            return;
        }
        self.uptime.sent = zero.map_or(Sent::Unknown, |(e, l)| Sent::Zero(e, l));
        self.pending.push_back(Item::Uptime { cycle: now, zero });
    }

    fn watched(&self, pin: u8) -> bool {
        pin < 64 && self.watch & 1 << pin != 0
    }

    fn touches_watch(&self, route: &UartRoute) -> bool {
        route.tx_pins & self.watch != 0 || route.rx_pin.is_some_and(|p| self.watched(p))
    }

    /// Takes what the board recorded, with the H4 packets the guest's host sent merged in by
    /// their cycles, then the frames the station sent, which the relay holds without their time:
    /// they are stamped with the cycle they are taken at.
    fn collect(&mut self) {
        let log = std::mem::take(&mut self.shared.borrow_mut().log);
        let mut items: Vec<Item> = log.into_iter().map(Item::Board).collect();
        if let Some(hci) = &self.opts.hci {
            let packets = std::mem::take(&mut hci.borrow_mut().tx);
            if !packets.is_empty() {
                // both in order already: a stable sort keeps each one's order, and the messages
                // stay in order of `t` (protocol §4)
                items.extend(
                    packets
                        .into_iter()
                        .map(|(cycle, data)| Item::Hci { cycle, data }),
                );
                items.sort_by_key(Item::cycle);
            }
        }
        self.pending.extend(items);
        // esp32sim's USB-Serial/JTAG has no pins and keeps no time of its own: what the guest
        // wrote in the slice is stamped with its end
        if self.opts.usb_console && self.opts.console {
            let data = self.guest.take_usb_console();
            if !data.is_empty() {
                let cycle = self.guest.cycles();
                self.pending.push_back(Item::UsbConsole { cycle, data });
            }
        }
        if self.opts.wifi.is_some() {
            let cycle = self.guest.cycles();
            let state = self.guest.station_state();
            if state != self.station_state {
                if let Some((ready, _)) = state {
                    let queued = self.eth.len();
                    let scheduled = self
                        .inputs
                        .values()
                        .filter(|i| matches!(i, Input::Eth(_)))
                        .count();
                    if let Some(wifi) = &mut self.stats.wifi {
                        wifi.eth_rx_dropped += (queued + scheduled) as u64;
                    }
                    self.eth.clear();
                    self.inputs
                        .retain(|_, input| !matches!(input, Input::Eth(_)));
                    self.guest.clear_station_relay();
                    self.pending.push_back(Item::WifiLink { cycle, ready });
                }
                self.station_state = state;
            }
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
                    let console = route.port == 0 && self.opts.console && !self.opts.usb_console;
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
                Item::WifiLink { ready, .. } => {
                    self.event(t, |seq| protocol::wifi_link(seq, t, ready))?;
                }
                Item::Eth { data, .. } => {
                    if let Some(wifi) = &mut self.stats.wifi {
                        wifi.eth_tx += 1;
                    }
                    self.event(t, |seq| protocol::eth_tx(seq, t, &data))?;
                }
                Item::Hci { data, .. } => {
                    self.event(t, |seq| protocol::hci_tx(seq, t, &data))?;
                }
                // not an event: one `console` message per line, as UART0's end after a newline
                Item::UsbConsole { data, .. } => {
                    self.close_open()?;
                    for piece in data.split_inclusive(|&b| b == b'\n') {
                        for chunk in piece.chunks(MAX_DATA) {
                            self.send(&protocol::console(t, chunk))?;
                        }
                    }
                }
                // not an event: the peer answers nothing
                Item::Board(Recorded::Sd(faulted)) => {
                    self.close_open()?;
                    self.send(&protocol::sd_op(t, &faulted))?;
                }
                Item::SdCard { image, error, .. } => {
                    // not an event: nothing answers it
                    self.close_open()?;
                    let line = protocol::sd_card_ack(t, image.as_deref(), error.as_deref());
                    self.send(&line)?;
                }
                Item::Uptime { zero, .. } => {
                    // not an event: nothing answers it
                    self.close_open()?;
                    // rounded outwards, so that the two still hold the zero
                    let zero = zero.map(|(earliest, latest)| (ns(earliest), ns_up(latest)));
                    self.send(&protocol::uptime(t, zero))?;
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
        let mut deadline = self.deadline();
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
                        return Err(protocol_error("eth_rx without --wifi or --wifi-sta", &line));
                    }
                    inputs.push((t, Input::Eth(data)));
                }
                Incoming::ConsoleRx { t, data } => {
                    check(t, Vec::new())?;
                    if !self.opts.usb_console {
                        return Err(protocol_error(
                            "console_rx without --console-port usb",
                            &line,
                        ));
                    }
                    inputs.push((t, Input::UsbConsole(data)));
                }
                Incoming::HciRx { t, data } => {
                    check(t, Vec::new())?;
                    if self.opts.hci.is_none() {
                        return Err(protocol_error("hci_rx without --bt", &line));
                    }
                    inputs.push((t, Input::Hci(data)));
                }
                Incoming::WifiAp { t, on } => {
                    check(t, Vec::new())?;
                    if self.opts.wifi.is_none() {
                        return Err(protocol_error("wifi_ap without --wifi", &line));
                    }
                    if self
                        .opts
                        .wifi
                        .is_some_and(|w| matches!(w, cli::Wifi::Station { .. }))
                    {
                        return Err(protocol_error(
                            "wifi_ap with --wifi-sta: there is no virtual access point to switch",
                            &line,
                        ));
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
                Incoming::SdFault { t, fault } => {
                    check(t, Vec::new())?;
                    if self.opts.card.is_none() {
                        return Err(protocol_error("sd_fault without --sd-spi", &line));
                    }
                    if !self.sd_ids.insert(fault.id) {
                        return Err(protocol_error(
                            &format!("sd_fault: id {} is in use already", fault.id),
                            &line,
                        ));
                    }
                    inputs.push((t, Input::SdFault(fault)));
                }
                Incoming::SdClear { t, ids } => {
                    check(t, Vec::new())?;
                    if self.opts.card.is_none() {
                        return Err(protocol_error("sd_clear without --sd-spi", &line));
                    }
                    inputs.push((t, Input::SdClear(ids)));
                }
                Incoming::SdCard { t, image } => {
                    check(t, Vec::new())?;
                    if self.opts.card.is_none() {
                        return Err(protocol_error("sd_card without --sd-spi", &line));
                    }
                    inputs.push((t, Input::SdCard(image)));
                }
                Incoming::FlashDump => {
                    let Some(path) = self.opts.flash_out else {
                        return Err(protocol_error("flash_dump without --flash-out", &line));
                    };
                    let error = write_flash(path, &self.guest.flash())
                        .err()
                        .map(|e| format!("{}: {e}", path.display()));
                    // not an event, and the turn goes on: no input of it has been applied, and
                    // the bus's clock has not moved
                    self.send(&protocol::flash_dump_ack(b, error.as_deref()))?;
                    self.flush()?;
                    // the write was the runner's time, not the peer's
                    deadline = self.deadline();
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
                    // A DHCP retry can arrive while the first copy is held until association.
                    // Keep one identical pending request, never a queue of stale retries.
                    let duplicate = self
                        .opts
                        .wifi
                        .is_some_and(|w| matches!(w, cli::Wifi::Station { .. }))
                        && dhcp_client_frame(&data)
                        && self.eth.contains(&data);
                    if !duplicate {
                        self.eth.push_back(data);
                    }
                    self.deliver_eth();
                }
                // into the controller's packet queue at once: USB carries it at no line rate the
                // runner paces; with the supply off there is no device on the bus to take it
                Input::UsbConsole(data) => {
                    if self.off {
                        *self.stats.console_rx_lost.get_or_insert(0) += data.len() as u64;
                    } else {
                        self.guest.usb_console_input(&data);
                    }
                }
                // for the guest's host, at its next VHCI poll; a reset before it loses the packet
                Input::Hci(data) => {
                    if let Some(hci) = &self.opts.hci {
                        hci.borrow_mut().rx.push_back(data);
                    }
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
                // the transactions from here on meet them (take_turn checked for the card)
                Input::SdFault(fault) => {
                    if let Some(card) = &self.opts.card {
                        card.borrow_mut().install(fault);
                    }
                }
                Input::SdClear(ids) => {
                    if let Some(card) = &self.opts.card {
                        card.borrow_mut().clear(ids.as_deref());
                    }
                }
                Input::SdCard(image) => self.change_card(now, image),
            }
        }
        if gpio {
            self.guest.take_gpio_inputs();
        }
        Ok(reset)
    }

    /// Takes the card out of the slot, or puts one holding `image` in, at `now`; what the slot
    /// holds then goes to the peer in its order (`sd_card_ack`). A card for a slot that holds one
    /// already, or one whose image cannot be opened or is not a card's size, is refused, and the
    /// slot stays as it was; taking the card out of an empty slot changes nothing.
    fn change_card(&mut self, now: u64, image: Option<String>) {
        let Some(slot) = &self.opts.card else {
            return;
        };
        let mut slot = slot.borrow_mut();
        let at = ns(now) as f64 / 1e9;
        let error = match &image {
            Some(image) => match slot.insert_file(image) {
                Ok(len) => {
                    slot.inserted += 1;
                    eprintln!(
                        "esp32tl-runner: SD card in at {at:.6} s: {image} ({} KiB, write-through)",
                        len >> 10
                    );
                    None
                }
                Err(e) => {
                    eprintln!("esp32tl-runner: SD card refused at {at:.6} s: {image}: {e}");
                    Some(format!("{image}: {e}"))
                }
            },
            None => {
                let held = slot.image().map(str::to_string);
                if slot.remove() {
                    slot.removed += 1;
                    eprintln!(
                        "esp32tl-runner: SD card out at {at:.6} s: {}",
                        held.unwrap_or_default()
                    );
                }
                None
            }
        };
        let image = slot.image().map(str::to_string);
        drop(slot);
        self.pending.push_back(Item::SdCard {
            cycle: now,
            image,
            error,
        });
    }

    /// Hands the station's link the frames that came due, in order, as many as its queue takes;
    /// the rest wait for the next slice boundary. Without the access point on the air, or while
    /// it is leaving, they are dropped: the station cannot hear them.
    fn deliver_eth(&mut self) {
        let Some(wifi) = &mut self.stats.wifi else {
            return;
        };
        while let Some(frame) = self.eth.front() {
            if !self.ap_on
                || self.leaving
                || self.off
                || self.guest.station_state().is_some_and(|s| !s.0)
            {
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
        if self.off {
            // no station to tell: the power-on reset puts the access point on the air as it
            // is then
            self.ap_on = on;
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
            // into every UART whose RX pin it is, each on its own
            let mut routed = false;
            for (port, route) in routes.iter().enumerate() {
                if route.rx_pin != Some(pin) {
                    continue;
                }
                routed = true;
                let matches = route.matches_baud(paced.baud);
                if !matches {
                    self.stats.uart[port].frm_err += 1;
                    self.warn(t, WarnKind::FrmErr, Some(port), pin, route.baud)?;
                } else if self.guest.rx_pending(port) >= RX_FIFO {
                    self.stats.uart[port].rxfifo_ovf += 1;
                    self.warn(t, WarnKind::RxFifoOvf, Some(port), pin, None)?;
                } else {
                    self.stats.uart[port].rx_bytes += 1;
                }
                if self.off {
                    continue;
                }
                if matches {
                    self.guest.uart_receive(port, paced.byte);
                } else {
                    self.guest.uart_frame_error(port);
                }
            }
            if !routed {
                self.stats.unrouted_rx_bytes += 1;
                self.warn(t, WarnKind::UnroutedRx, None, pin, None)?;
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
        self.stats.rt_resyncs = self.opts.realtime.then(|| self.guest.resyncs());
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

/// Writes the whole flash to `path`, through a file beside it and a rename: whoever reads `path`
/// finds one instant's flash, or none (protocol 5.21).
fn write_flash(path: &Path, flash: &[u8]) -> std::io::Result<()> {
    let mut partial = path.as_os_str().to_owned();
    partial.push(".partial");
    std::fs::write(&partial, flash)?;
    std::fs::rename(&partial, path)
}

/// The flash at the end of a run. A file that cannot be written changes nothing of how the run
/// ended: the first cause decides the exit code.
pub fn write_flash_at_end(path: &Path, flash: &[u8]) {
    if let Err(e) = write_flash(path, flash) {
        eprintln!(
            "esp32tl-runner: cannot write the flash: {}: {e}",
            path.display()
        );
    }
}

/// A deauthentication from the access point at `bssid` to the station `sta`, reason 3: "the
/// sending station is leaving" (IEEE 802.11-2020 9.3.3.12 and Table 9-49).
pub(crate) fn deauthentication(sta: [u8; 6], bssid: [u8; 6], seq: u16) -> Vec<u8> {
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

/// Ethernet/IPv4 UDP 68 -> 67; only exact pending DHCP copies are coalesced.
fn dhcp_client_frame(frame: &[u8]) -> bool {
    if frame.len() < 42 || frame[12..14] != [8, 0] || frame[14] >> 4 != 4 || frame[23] != 17 {
        return false;
    }
    let ihl = usize::from(frame[14] & 15) * 4;
    ihl >= 20 && frame.get(14 + ihl..18 + ihl) == Some(&[0, 68, 0, 67])
}
