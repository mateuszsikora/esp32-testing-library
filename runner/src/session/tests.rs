//! The run loop against a scripted guest and a fake peer: the arithmetic of slices, block points
//! and pacing, the flush points, and the rules of `docs/protocol.md` the runner enforces.
use super::*;
use crate::chip::esp32::reset_console_rx;
use crate::chip::esp32::{SLEEP_STEP, WAKE_MARGIN_TICKS, rtc};
use crate::socket_board::{Recorder, SocketBoard};
use esp_soc::BoardModel;
use esp_soc::SocBus;
use esp_soc::ble::HciController;
use esp_soc::json::{Json, parse_json};
use esp_soc::wifi::{self, StaState};
use std::cell::RefCell;
use std::path::PathBuf;
use std::rc::Rc;

const GNSS_TX: u8 = 4;
const GNSS_RX: u8 = 5;
const MODEM_TX: u8 = 26;
const MODEM_RX: u8 = 27;
/// A modem's power key, which the guest drives, and its ring indicator, which the peer drives.
const PWRKEY: u8 = 13;
const RI: u8 = 33;
/// One emulated second, in bus cycles.
const SECOND: u64 = esp32::periph::CPU_HZ;
const MS_CYCLES: u64 = SECOND / 1000;
/// A data block's 512 bytes, its CRC16 and a byte to spare.
const BLOCK_AND_CRC: usize = 512 + 3;
const CARD: sd_card::Wiring = sd_card::Wiring {
    sclk: 14,
    mosi: 15,
    miso: 2,
    cs: 13,
};
/// The routes of a transaction to the card, its CS low.
const CARD_PINS: esp_soc::board::SpiPins = esp_soc::board::SpiPins {
    sclk: 1 << 14,
    mosi: 1 << 15,
    miso: Some(2),
    cs: 1 << 13,
};

fn route(port: usize, tx: Option<u8>, rx: Option<u8>, baud: Option<u32>) -> UartRoute {
    UartRoute::new(port, tx.map_or(0, |p| 1 << p), rx, baud)
}

fn modem(baud: u32) -> UartRoute {
    route(1, Some(MODEM_TX), Some(MODEM_RX), Some(baud))
}

#[derive(Clone, Debug)]
enum Act {
    Tx(usize, u8),
    Route(UartRoute),
    Reset(u32),
    /// The guest writes its reset-reason hint register (`Guest::reset_hint`).
    Hint(u32),
    /// The guest reads its RX FIFO empty.
    Drain(usize),
    /// The guest writes `GPIO_ENABLE` and `GPIO_OUT`, and the pads follow them.
    Drive(u64, u64),
    /// The hook reports these pad changes with `GPIO_ENABLE` and `GPIO_OUT` as given: a pad a
    /// peripheral drives through the GPIO matrix, whatever the two words say.
    Pads(Vec<(u8, bool)>, u64, u64),
    Fault,
    /// An SPI transaction to the SD card, at the bus's tick for that cycle.
    Spi(Vec<u8>),
    /// The guest programs a byte of its flash.
    Flash(usize, u8),
    /// The guest's Bluetooth host sends an H4 packet, through the forwarder of `--bt`.
    HciTx(Vec<u8>),
    /// The guest's controller task polls the VHCI, as esp32sim's `Poll` hook does.
    HciPoll,
    /// The guest writes these bytes to its USB-Serial/JTAG controller, which flushes them.
    UsbTx(Vec<u8>),
    /// The uptime counter is now as given; a reset stops it.
    Uptime(Uptime),
    /// Bytes UART0 sent, on a chip whose UARTs have no pins: no board callback sees them.
    Uart0Stream(Vec<u8>),
    /// The guest calls `esp_bt_controller_init`, which the stub of `--no-bt` answers.
    BtInit,
}

/// A guest that does what its script says at the cycles it says, and records UART bytes the way
/// `SocketBoard` does. Its GPIO goes through a real `SocketBoard`. `overshoot` is how far past a
/// slice's end `run` returns, as it does at clocks below 240 MHz (protocol 6.1).
struct FakeGuest {
    shared: Shared,
    board: SocketBoard,
    /// `GPIO_ENABLE` and `GPIO_OUT`.
    drive: (u64, u64),
    /// What the chip took from the board, with the cycle: a level, or `None` for a release.
    inputs: Vec<(u64, u8, Option<bool>)>,
    now: u64,
    overshoot: u64,
    script: VecDeque<(u64, Act)>,
    routes: [UartRoute; PORTS],
    /// What reached each RX FIFO, with the cycle.
    fifo: [Vec<(u64, u8)>; PORTS],
    /// The bytes each UART lost, with the cycle and why.
    lost: [Vec<(u64, Lost)>; PORTS],
    /// Where each slice was asked to end.
    slices: Vec<u64>,
    /// The cycles of each power-on reset.
    power_resets: Vec<u64>,
    /// Where each slice with the supply off ended.
    idles: Vec<u64>,
    /// What came back of each `Act::Spi`, with the cycle.
    spi: Vec<(u64, Vec<u8>)>,
    /// Where pacing started, and the wall instant it did.
    paced: Vec<(u64, Instant)>,
    /// The cycles esp32sim skips a lag at.
    lags: Vec<u64>,
    /// The flash.
    flash: Vec<u8>,
    /// The bus cycle of each `sync_flash`.
    flash_syncs: Vec<u64>,
    /// With `--bt`: the guest's Bluetooth controller, and what each `Act::HciPoll` read of it,
    /// with the cycle.
    hci: Option<hci::Shared>,
    hci_read: Vec<(u64, Option<Vec<u8>>)>,
    /// From when to when the chip is in deep sleep, if it ever is, and whether the run loop may
    /// step through it.
    sleep: Option<(u64, u64)>,
    steppable: bool,
    /// Where each `sleep_until` started and where it left the clock.
    skips: Vec<(u64, u64)>,
    /// With `--console-port usb`: what the guest wrote to its USB-Serial/JTAG controller and the
    /// runner has not taken yet, and what the host gave it, with the cycle.
    usb: Option<Vec<u8>>,
    usb_in: Vec<(u64, Vec<u8>)>,
    /// The uptime counter, as `Act::Uptime` last set it.
    uptime: Uptime,
    /// With its console on a UART0 without pins: what UART0 sent, not taken yet.
    uart0: Option<Vec<u8>>,
    uart0_in: Vec<(u64, Vec<u8>)>,
    /// With `--no-bt`: the guest's calls to `esp_bt_controller_init` so far.
    bt_inits: Option<u64>,
    /// The reset-reason hint register, as `Act::Hint` last wrote it.
    hint: u32,
}

/// Why `FakeGuest` lost a received byte.
#[derive(Clone, Copy, Debug, PartialEq)]
enum Lost {
    Overflow,
    FrameError,
}

impl FakeGuest {
    fn new(script: Vec<(u64, Act)>) -> Self {
        let mut script = script;
        script.sort_by_key(|(cycle, _)| *cycle);
        let shared = Rc::new(RefCell::new(Recorder::default()));
        Self {
            board: SocketBoard::new(shared.clone(), None),
            shared,
            drive: (0, 0),
            inputs: Vec::new(),
            now: 0,
            overshoot: 0,
            script: script.into(),
            routes: std::array::from_fn(|port| UartRoute::new(port, 0, None, None)),
            fifo: Default::default(),
            lost: Default::default(),
            slices: Vec::new(),
            power_resets: Vec::new(),
            idles: Vec::new(),
            spi: Vec::new(),
            paced: Vec::new(),
            lags: Vec::new(),
            flash: vec![0xff; 16],
            flash_syncs: Vec::new(),
            hci: None,
            hci_read: Vec::new(),
            sleep: None,
            steppable: true,
            skips: Vec::new(),
            usb: None,
            usb_in: Vec::new(),
            uptime: Uptime::Stopped,
            uart0: None,
            uart0_in: Vec::new(),
            bt_inits: None,
            hint: 0,
        }
    }

    /// With its console on a UART0 without pins (`Profile::uart_pins`).
    fn with_uart0_stream(mut self) -> Self {
        self.uart0 = Some(Vec::new());
        self
    }

    /// With its console on the USB-Serial/JTAG controller (`--console-port usb`).
    fn with_usb_console(mut self) -> Self {
        self.usb = Some(Vec::new());
        self
    }

    fn with_bt(mut self) -> Self {
        self.hci = Some(hci::Shared::default());
        self
    }

    /// With no Bluetooth controller (`--no-bt`).
    fn with_no_bt(mut self) -> Self {
        self.bt_inits = Some(0);
        self
    }

    fn forwarder(&self) -> hci::Forwarder {
        hci::Forwarder(self.hci.clone().expect("a guest with --bt"))
    }

    /// In deep sleep from `from` to `to`, where its script should wake it with a reset.
    fn asleep_between(mut self, from: u64, to: u64) -> Self {
        self.sleep = Some((from, to));
        self
    }

    /// Asleep, but with something in the RTC, as its watchdog, that keeps the sleep from being
    /// stepped through.
    fn not_steppable(mut self) -> Self {
        self.steppable = false;
        self
    }

    fn with_card(mut self, card: &sd_card::Shared) -> Self {
        self.board = SocketBoard::new(self.shared.clone(), Some(card.clone()));
        self
    }

    fn with_routes(mut self, routes: &[UartRoute]) -> Self {
        for r in routes {
            self.routes[r.port] = *r;
        }
        self
    }

    fn received(&self, port: usize) -> Vec<u8> {
        self.fifo[port].iter().map(|&(_, b)| b).collect()
    }
}

impl Guest for FakeGuest {
    fn cycles(&self) -> u64 {
        self.now
    }
    fn run_until(&mut self, cycle: u64) -> Stop {
        assert!(cycle > self.now, "a slice from {} to {cycle}", self.now);
        self.slices.push(cycle);
        let end = cycle + self.overshoot;
        while let Some(&(at, _)) = self.script.front() {
            if at >= end {
                break;
            }
            let (at, act) = self.script.pop_front().unwrap();
            self.now = at.max(self.now);
            match act {
                Act::Tx(port, byte) => self.shared.borrow_mut().log.push(Recorded::UartTx {
                    cycle: self.now,
                    route: self.routes[port],
                    byte,
                }),
                Act::Route(r) => self.routes[r.port] = r,
                Act::Hint(hint) => self.hint = hint,
                Act::Reset(cause) => {
                    self.script.push_front((at, Act::Reset(cause)));
                    return Stop::SwReset;
                }
                Act::Drain(port) => self.fifo[port].clear(),
                Act::Drive(enable, out) => {
                    let (was_enable, was_out) = self.drive;
                    let (old, new) = (was_enable & was_out, enable & out);
                    let changes: Vec<(u8, bool)> = (0..64)
                        .filter(|&pin| (old ^ new) & 1 << pin != 0)
                        .map(|pin| (pin, new & 1 << pin != 0))
                        .collect();
                    self.drive = (enable, out);
                    self.board.gpio_output_at(self.now, &changes, enable, out);
                }
                Act::Pads(changes, enable, out) => {
                    self.drive = (enable, out);
                    self.board.gpio_output_at(self.now, &changes, enable, out);
                }
                Act::Fault => return Stop::Exceptions(3),
                Act::Spi(tx) => {
                    self.board.advance_to(self.now);
                    let rx = self.board.spi_transfer_pins(2, CARD_PINS, &tx, tx.len());
                    self.spi.push((self.now, rx));
                }
                Act::Flash(offset, byte) => self.flash[offset] = byte,
                // as esp32sim's `Send` hook: the controller to the bus's cycle, then the packet
                Act::HciTx(packet) => {
                    let mut forwarder = self.forwarder();
                    forwarder.advance_to(self.now);
                    forwarder.send_h4(&packet);
                }
                Act::HciPoll => {
                    let packet = self.forwarder().poll_h4();
                    self.hci_read.push((self.now, packet));
                }
                Act::UsbTx(data) => self
                    .usb
                    .as_mut()
                    .expect("a guest with --console-port usb")
                    .extend(data),
                Act::Uptime(uptime) => self.uptime = uptime,
                Act::Uart0Stream(data) => self
                    .uart0
                    .as_mut()
                    .expect("a guest whose UART0 has no pins")
                    .extend(data),
                Act::BtInit => *self.bt_inits.as_mut().expect("a guest with --no-bt") += 1,
            }
        }
        self.now = end;
        Stop::Halted
    }
    fn chip_reset(&mut self) -> u32 {
        let Some((_, Act::Reset(cause))) = self.script.pop_front() else {
            panic!("a reset with none in the script");
        };
        if self.hci.is_some() {
            self.forwarder().reset();
        }
        self.routes = std::array::from_fn(|port| UartRoute::new(port, 0, None, None));
        self.fifo = Default::default();
        self.drive = (0, 0);
        self.uptime = Uptime::Stopped;
        cause
    }
    fn reset_hint(&mut self) -> u32 {
        self.hint
    }
    fn power_on_reset(&mut self, _rom: &[u8]) -> u32 {
        // the hint stays, so that a `reset` with none shows the run loop sent none
        self.power_resets.push(self.now);
        if self.hci.is_some() {
            self.forwarder().reset();
        }
        self.routes = std::array::from_fn(|port| UartRoute::new(port, 0, None, None));
        self.fifo = Default::default();
        self.drive = (0, 0);
        self.uptime = Uptime::Stopped;
        1
    }
    fn idle_until(&mut self, cycle: u64) {
        assert!(
            cycle > self.now,
            "an idle slice from {} to {cycle}",
            self.now
        );
        self.idles.push(cycle);
        self.now = cycle;
    }
    fn uart_route(&self, port: usize) -> UartRoute {
        self.routes[port]
    }
    fn rx_pending(&self, port: usize) -> usize {
        self.fifo[port].len()
    }
    fn uart_receive(&mut self, port: usize, byte: u8) {
        if self.fifo[port].len() < RX_FIFO {
            self.fifo[port].push((self.now, byte));
        } else {
            self.lost[port].push((self.now, Lost::Overflow));
        }
    }
    fn uart_frame_error(&mut self, port: usize) {
        self.lost[port].push((self.now, Lost::FrameError));
    }
    fn gpio_drive(&self) -> (u64, u64) {
        self.drive
    }
    fn take_usb_console(&mut self) -> Vec<u8> {
        self.usb.as_mut().map(std::mem::take).unwrap_or_default()
    }
    fn usb_console_input(&mut self, data: &[u8]) {
        self.usb_in.push((self.now, data.to_vec()));
    }
    fn take_uart0_console(&mut self) -> Vec<u8> {
        self.uart0.as_mut().map(std::mem::take).unwrap_or_default()
    }
    fn uart0_console_input(&mut self, data: &[u8]) {
        self.uart0_in.push((self.now, data.to_vec()));
    }
    fn take_gpio_inputs(&mut self) {
        for edge in self.board.take_edges() {
            self.inputs.push((self.now, edge.pin, Some(edge.level)));
        }
        for pin in self.board.released_inputs() {
            self.inputs.push((self.now, pin, None));
        }
    }
    fn start_pacing(&mut self) {
        self.paced.push((self.now, Instant::now()));
    }
    fn resyncs(&self) -> u64 {
        self.lags.iter().filter(|&&at| at <= self.now).count() as u64
    }
    fn flash(&self) -> Cow<'_, [u8]> {
        Cow::Borrowed(&self.flash)
    }
    fn sync_flash(&mut self) {
        self.flash_syncs.push(self.now);
    }
    fn write_flash(&mut self, offset: usize, data: &[u8]) -> Result<(), String> {
        crate::chip::fits(self.flash.len(), offset, data.len())?;
        self.flash[offset..offset + data.len()].copy_from_slice(data);
        Ok(())
    }
    fn uptime(&mut self) -> Uptime {
        self.uptime
    }
    fn bt_controller_inits(&self) -> u64 {
        self.bt_inits.unwrap_or(0)
    }
    fn sleeps_through(&self) -> bool {
        self.steppable
            && self
                .sleep
                .is_some_and(|(from, to)| (from..to).contains(&self.now))
    }
    fn sleep_until(&mut self, cycle: u64) {
        // as esp32sim's machine does: short of the wake, which its own rounds bring
        if let Some((_, to)) = self.sleep.filter(|_| self.sleeps_through()) {
            let from = self.now;
            self.now = cycle.min(to - 1);
            self.skips.push((from, self.now));
        }
    }
}

/// What the peer saw in one of its turns.
struct Turn<'a> {
    /// The `sync`'s `t`.
    t: u64,
    /// Every message since the previous `sync`.
    msgs: &'a [Json],
    /// The last `seq` seen so far.
    seq: u64,
}

type Respond = Box<dyn FnMut(&Turn) -> Vec<String>>;

/// A peer that answers each `sync` with what its function returns.
struct FakePeer {
    hello: String,
    respond: Respond,
    /// Every runner message, in order.
    msgs: Vec<Json>,
    turn_start: usize,
    seq: u64,
    queue: VecDeque<String>,
    /// Whether the runner called `closing`.
    closed: bool,
    /// How many messages had been sent at each `idle`.
    idles: Vec<usize>,
    /// What the runner waited for on its wall clock, each with the `sync`s sent by then.
    waits: Vec<(usize, Instant)>,
    /// What ends the first such wait, if anything does.
    wait_ends: Option<LinkError>,
    /// The deadline of every read of the peer.
    deadlines: Vec<Option<Instant>>,
}

const SIGNAL: &str = "SIGNAL";

impl FakePeer {
    fn new(watch: &[u8], respond: impl FnMut(&Turn) -> Vec<String> + 'static) -> Self {
        let watch: Vec<String> = watch.iter().map(u8::to_string).collect();
        Self::with_hello(
            &format!(
                r#"{{"type":"hello","version":2,"chip":"esp32","board":"esp32-devkitc","watch":[{}]}}"#,
                watch.join(",")
            ),
            respond,
        )
    }

    fn with_hello(hello: &str, respond: impl FnMut(&Turn) -> Vec<String> + 'static) -> Self {
        Self {
            hello: hello.into(),
            respond: Box::new(respond),
            msgs: Vec::new(),
            turn_start: 0,
            seq: 0,
            queue: VecDeque::new(),
            closed: false,
            idles: Vec::new(),
            waits: Vec::new(),
            wait_ends: None,
            deadlines: Vec::new(),
        }
    }

    fn types(&self) -> Vec<&str> {
        self.msgs.iter().map(kind).collect()
    }

    fn of(&self, kind_: &str) -> Vec<&Json> {
        self.msgs.iter().filter(|m| kind(m) == kind_).collect()
    }

    fn stats(&self) -> &Json {
        let last = self.msgs.last().expect("a message");
        assert_eq!(kind(last), "stats");
        last
    }
}

impl Link for FakePeer {
    fn send(&mut self, line: &str) -> Result<(), LinkError> {
        let msg = parse_json(line).unwrap_or_else(|e| panic!("{line}: {e}"));
        if let Some(seq) = msg.get("seq").and_then(Json::as_i64) {
            assert_eq!(seq as u64, self.seq + 1, "seq out of order: {line}");
            self.seq = seq as u64;
        }
        if let Some(t) = msg.get("t").and_then(Json::as_i64) {
            let last = self
                .msgs
                .iter()
                .rev()
                .find_map(|m| m.get("t").and_then(Json::as_i64));
            assert!(last.is_none_or(|l| l <= t), "t out of order: {line}");
        }
        let k = kind(&msg).to_string();
        self.msgs.push(msg);
        match k.as_str() {
            "hello" => self.queue.push_back(self.hello.clone()),
            "sync" => {
                let t = num(self.msgs.last().unwrap(), "t");
                let turn = Turn {
                    t,
                    msgs: &self.msgs[self.turn_start..],
                    seq: self.seq,
                };
                let lines = (self.respond)(&turn);
                self.turn_start = self.msgs.len();
                self.queue.extend(lines);
            }
            _ => {}
        }
        Ok(())
    }
    fn flush(&mut self) -> Result<(), LinkError> {
        Ok(())
    }
    fn idle(&mut self) -> Result<(), LinkError> {
        self.idles.push(self.msgs.len());
        Ok(())
    }
    fn closing(&mut self) {
        self.closed = true;
    }
    fn close(&mut self) {}
    /// A queued [`SIGNAL`] stands for `SIGTERM` arriving while the runner waits for the peer.
    fn recv(&mut self, deadline: Option<Instant>) -> Result<String, LinkError> {
        self.deadlines.push(deadline);
        match self.queue.pop_front() {
            Some(line) if line == SIGNAL => Err(LinkError::Signal),
            Some(line) => Ok(line),
            None => Err(LinkError::Gone(
                "the fake peer has nothing more to say".into(),
            )),
        }
    }
    /// Returns at once: the tests read the times the runner asks for.
    fn wait_until(&mut self, until: Instant) -> Result<(), LinkError> {
        let syncs = self.of("sync").len();
        self.waits.push((syncs, until));
        self.wait_ends.take().map_or(Ok(()), Err)
    }
}

fn kind(msg: &Json) -> &str {
    msg.get("type").and_then(Json::as_str).unwrap_or_default()
}

fn num(msg: &Json, key: &str) -> u64 {
    msg.get(key)
        .and_then(Json::as_i64)
        .unwrap_or_else(|| panic!("{msg:?} has no {key}")) as u64
}

fn data(msg: &Json) -> Vec<u8> {
    protocol::unbase64(msg.get("data").and_then(Json::as_str).unwrap()).unwrap()
}

fn wait(ack: u64, quiet_until: Option<u64>, latency: Option<u64>) -> String {
    let opt = |v: Option<u64>| v.map_or("null".into(), |v| v.to_string());
    format!(
        r#"{{"type":"wait","ack":{ack},"quiet_until":{},"latency":{}}}"#,
        opt(quiet_until),
        opt(latency)
    )
}

fn uart_rx(t: u64, pin: u8, baud: u32, bytes: &[u8]) -> String {
    format!(
        r#"{{"type":"uart_rx","t":{t},"pin":{pin},"baud":{baud},"data":"{}"}}"#,
        protocol::base64(bytes)
    )
}

/// A peer that only acknowledges, with the same `quiet_until` step and `latency` every turn.
fn idle(step: Option<u64>, latency: Option<u64>) -> impl FnMut(&Turn) -> Vec<String> {
    move |turn| vec![wait(turn.seq, step.map(|s| turn.t + s), latency)]
}

struct Run {
    end: End,
    stats: Stats,
    guest: FakeGuest,
    peer: FakePeer,
}

fn run_for(seconds: f64, guest: FakeGuest, peer: FakePeer, console: bool) -> Run {
    run_with_card(seconds, guest, peer, console, None)
}

fn run_with_card(
    seconds: f64,
    guest: FakeGuest,
    peer: FakePeer,
    console: bool,
    card: Option<&sd_card::Shared>,
) -> Run {
    run_with(seconds, guest, peer, console, card, None)
}

fn run_with(
    seconds: f64,
    mut guest: FakeGuest,
    mut peer: FakePeer,
    console: bool,
    card: Option<&sd_card::Shared>,
    flash_out: Option<&Path>,
) -> Run {
    let signal = AtomicBool::new(false);
    let shared = guest.shared.clone();
    let opts = Options {
        end_cycle: (seconds * SECOND as f64) as u64,
        console,
        usb_console: guest.usb.is_some(),
        uart0_stream: guest.uart0.is_some(),
        peer_timeout: None,
        signal: &signal,
        parent_gone: None,
        wifi: None,
        rom: &[],
        card: card.cloned(),
        realtime: false,
        flash_out,
        hci: guest.hci.clone(),
        no_bt: guest.bt_inits.is_some(),
    };
    let (end, stats) = Session::new(&mut guest, &mut peer, shared, opts).run();
    Run {
        end,
        stats,
        guest,
        peer,
    }
}

fn run(seconds: f64, guest: FakeGuest, peer: FakePeer) -> Run {
    run_for(seconds, guest, peer, false)
}

fn tx(at: u64, port: usize, bytes: &[u8], spacing: u64) -> Vec<(u64, Act)> {
    bytes
        .iter()
        .enumerate()
        .map(|(i, &b)| (at + i as u64 * spacing, Act::Tx(port, b)))
        .collect()
}

#[test]
fn the_first_sync_comes_before_the_guest_runs_and_its_inputs_are_in_place() {
    // rule 4: inputs stamped 0 reach the FIFO before the first instruction
    let guest = FakeGuest::new(vec![]).with_routes(&[modem(115_200)]);
    let mut first = true;
    let peer = FakePeer::new(&[MODEM_TX, MODEM_RX], move |turn| {
        if std::mem::take(&mut first) {
            assert_eq!(turn.t, 0);
            assert_eq!(
                turn.msgs.iter().map(kind).collect::<Vec<_>>(),
                ["hello", "uart_cfg", "sync"]
            );
            vec![uart_rx(0, MODEM_RX, 115_200, b"R"), wait(1, None, None)]
        } else {
            vec![wait(turn.seq, None, None)]
        }
    });
    let r = run(0.01, guest, peer);
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    assert_eq!(r.guest.fifo[1], vec![(0, b'R')]);
    assert_eq!(
        r.guest.slices[0],
        cycles(MS),
        "the first slice starts at cycle 0"
    );
    assert_eq!(r.peer.types()[..3], ["hello", "uart_cfg", "sync"]);
    let cfg = r.peer.of("uart_cfg")[0].clone();
    assert_eq!(num(&cfg, "seq"), 1);
    assert_eq!(num(&cfg, "t"), 0);
}

#[test]
fn a_handshake_mismatch_is_a_protocol_error_with_stats() {
    for (hello, found) in [
        (
            r#"{"type":"hello","version":99,"chip":"esp32"}"#,
            "version 99",
        ),
        (
            r#"{"type":"hello","version":2,"chip":"esp32s3"}"#,
            "chip \"esp32s3\"",
        ),
        (r#"{"type":"wait","ack":0}"#, "expected hello"),
        // pins the classic chip does not have, though the parser takes any below 64
        (
            r#"{"type":"hello","version":2,"chip":"esp32","watch":[4,24]}"#,
            "watch holds GPIO 24, which the esp32 does not have",
        ),
        (
            r#"{"type":"hello","version":2,"chip":"esp32","watch":[28]}"#,
            "GPIO 28",
        ),
        (
            r#"{"type":"hello","version":2,"chip":"esp32","watch":[40]}"#,
            "GPIO 40",
        ),
        (
            r#"{"type":"hello","version":2,"chip":"esp32","watch":[63]}"#,
            "GPIO 63",
        ),
    ] {
        let peer = FakePeer::with_hello(hello, |_| panic!("no turn after a bad hello"));
        let r = run(0.01, FakeGuest::new(vec![]), peer);
        let End::Protocol(detail) = &r.end else {
            panic!("{:?}", r.end)
        };
        assert!(detail.contains(found), "{detail}");
        assert_eq!(r.end.exit_code(), 3);
        assert_eq!(r.peer.types(), ["hello", "stats"]);
        assert_eq!(r.peer.stats().str_or("end", ""), "protocol_error");
        assert!(r.guest.slices.is_empty(), "the guest never ran");
    }
}

#[test]
fn bytes_are_paced_one_character_apart_at_the_message_baud() {
    let guest = FakeGuest::new(vec![]).with_routes(&[route(1, None, Some(GNSS_RX), Some(9600))]);
    let t0 = 2_000_000;
    let mut turns = 0;
    let peer = FakePeer::new(&[GNSS_TX, GNSS_RX], move |turn| {
        turns += 1;
        match turns {
            // sent early, at turn 0, stamped in the future
            1 => vec![
                uart_rx(t0, GNSS_RX, 9600, b"ABC"),
                wait(turn.seq, None, None),
            ],
            _ => vec![wait(turn.seq, None, None)],
        }
    });
    let r = run(0.02, guest, peer);
    assert!(matches!(r.end, End::MaxSeconds));
    let expected: Vec<(u64, u8)> = (0..3)
        .map(|k| (cycles(t0 + char_ns(k, 9600)), b"ABC"[k as usize]))
        .collect();
    assert_eq!(r.guest.fifo[1], expected);
    // each byte at its own slice boundary
    for &(cycle, _) in &expected {
        assert!(r.guest.slices.contains(&cycle), "no slice ends at {cycle}");
    }
    assert_eq!(r.stats.uart[1].rx_bytes, 3);
    assert_eq!(r.stats.turns, 1);
}

#[test]
fn a_busy_line_queues_the_next_message_and_the_input_queue_orders_by_t() {
    let guest = FakeGuest::new(vec![]).with_routes(&[route(1, None, Some(GNSS_RX), Some(9600))]);
    let mut turns = 0;
    let peer = FakePeer::new(&[GNSS_RX], move |turn| {
        turns += 1;
        match turns {
            // the later message arrives first, and quiet_until brings a turn before either is due
            1 => vec![
                uart_rx(1_000_000, GNSS_RX, 9600, b"cd"),
                wait(turn.seq, Some(500_000), None),
            ],
            // the earlier one arrives a turn later, its t still ahead
            2 => {
                assert_eq!(turn.t, 500_000);
                vec![
                    uart_rx(990_000, GNSS_RX, 9600, b"ab"),
                    wait(turn.seq, None, None),
                ]
            }
            _ => vec![wait(turn.seq, None, None)],
        }
    });
    let r = run(0.02, guest, peer);
    assert_eq!(r.guest.received(1), b"abcd");
    let d0 = 990_000;
    // "cd" comes due at 1 ms, while "ab" still holds the line: it starts where "ab" ends
    let free = d0 + char_ns(2, 9600);
    let at: Vec<u64> = r.guest.fifo[1].iter().map(|&(c, _)| c).collect();
    assert_eq!(
        at,
        [
            cycles(d0),
            cycles(d0 + char_ns(1, 9600)),
            cycles(free),
            cycles(free + char_ns(1, 9600)),
        ]
    );
}

#[test]
fn the_block_point_is_the_earliest_of_quiet_until_and_the_first_event_plus_latency() {
    // a request at 1 ms; latency 300 µs; quiet_until at 1.2 ms comes first, then t + latency
    let at = cycles(1_000_000);
    let guest = FakeGuest::new(tx(at, 1, b"AT\r", 100)).with_routes(&[modem(115_200)]);
    let t_req = ns(at);
    let mut syncs = Vec::new();
    let peer = FakePeer::new(&[MODEM_TX, MODEM_RX], move |turn| {
        syncs.push(turn.t);
        match syncs.len() {
            1 => vec![wait(turn.seq, Some(1_200_000), Some(300_000))],
            2 => {
                assert_eq!(turn.t, 1_200_000, "quiet_until");
                // the request went out before the sync, not yet acknowledged by this turn
                vec![wait(turn.seq, Some(5_000_000), Some(300_000))]
            }
            3 => {
                // the turn the request's own block point brings: there was no event after
                // the last turn, so the next block point is quiet_until
                assert_eq!(turn.t, 5_000_000);
                vec![wait(turn.seq, None, Some(300_000))]
            }
            _ => vec![wait(turn.seq, None, None)],
        }
    });
    let r = run(0.01, guest, peer);
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    let syncs: Vec<u64> = r.peer.of("sync").iter().map(|m| num(m, "t")).collect();
    assert_eq!(syncs, [0, 1_200_000, 5_000_000]);
    let tx = r.peer.of("uart_tx");
    assert_eq!(tx.len(), 1);
    assert_eq!(num(tx[0], "t"), t_req);
    assert!(r.guest.slices.contains(&cycles(1_200_000)));
}

#[test]
fn an_event_with_latency_sets_the_block_point_even_mid_slice() {
    // nothing scheduled by the peer, so the slices run a millisecond at a time; the request at
    // 0.5 ms with a latency of 100 µs must still stop the guest at t + latency, not at 1 ms
    let at = cycles(500_000) + 3;
    let guest = FakeGuest::new(tx(at, 1, b"AT\r", 50)).with_routes(&[modem(115_200)]);
    let t_req = ns(at);
    let latency = 100_000;
    let mut turns = 0;
    let peer = FakePeer::new(&[MODEM_TX, MODEM_RX], move |turn| {
        turns += 1;
        match turns {
            1 => vec![wait(turn.seq, None, Some(latency))],
            2 => {
                assert_eq!(turn.t, t_req + latency);
                assert_eq!(kind(&turn.msgs[0]), "uart_tx");
                // answered exactly at t + latency: on time (rule 2)
                vec![
                    uart_rx(t_req + latency, MODEM_RX, 115_200, b"OK"),
                    wait(turn.seq, None, Some(latency)),
                ]
            }
            _ => vec![wait(turn.seq, None, Some(latency))],
        }
    });
    let r = run(0.01, guest, peer);
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    assert_eq!(r.guest.received(1), b"OK");
    // the answer's first byte enters the FIFO at the first cycle at or after its t
    assert_eq!(r.guest.fifo[1][0].0, cycles(t_req + latency));
    // no slice ran past the block point
    assert!(r.guest.slices.contains(&cycles(t_req + latency)));
}

#[test]
fn an_answer_at_t_plus_latency_passes_with_the_guest_idle_in_wfi() {
    // the request, then nothing until the answer: the guest sleeps, and only the slices stop it
    // at the block point (protocol 6.1, 11.10). At 160 MHz `run` returns a bus cycle late.
    for overshoot in [0, 1] {
        let at = cycles(3_000_000) + 1;
        let mut guest = FakeGuest::new(tx(at, 1, b"AT\r", 0)).with_routes(&[modem(115_200)]);
        guest.overshoot = overshoot;
        let t_req = ns(at);
        let latency = 250_000 + 7;
        let mut turns = 0;
        let peer = FakePeer::new(&[MODEM_TX, MODEM_RX], move |turn| {
            turns += 1;
            match turns {
                1 => vec![wait(turn.seq, None, Some(latency))],
                2 => {
                    assert_eq!(turn.t, t_req + latency);
                    vec![
                        uart_rx(t_req + latency, MODEM_RX, 115_200, b"OK\r\n"),
                        wait(turn.seq, None, Some(latency)),
                    ]
                }
                _ => vec![wait(turn.seq, None, Some(latency))],
            }
        });
        let r = run(0.01, guest, peer);
        assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
        assert_eq!(r.guest.received(1), b"OK\r\n");
        let first = r.guest.fifo[1][0].0;
        let b = cycles(t_req + latency);
        assert!(first >= b && first <= b + overshoot, "{first} for B at {b}");
    }
}

#[test]
fn an_answer_before_t_plus_latency_is_late_and_ends_the_run() {
    // rule 2: the peer promised a latency and broke it
    let at = cycles(1_000_000);
    let guest = FakeGuest::new(tx(at, 1, b"AT\r", 0)).with_routes(&[modem(115_200)]);
    let t_req = ns(at);
    let mut turns = 0;
    let peer = FakePeer::new(&[MODEM_TX, MODEM_RX], move |turn| {
        turns += 1;
        match turns {
            1 => vec![wait(turn.seq, None, Some(1000))],
            _ => vec![
                uart_rx(t_req, MODEM_RX, 115_200, b"OK"),
                wait(turn.seq, None, Some(1000)),
            ],
        }
    });
    let r = run(0.01, guest, peer);
    let End::Protocol(detail) = &r.end else {
        panic!("{:?}", r.end)
    };
    assert!(detail.contains("rule 2"), "{detail}");
    assert_eq!(r.end.exit_code(), 3);
    let stats = r.peer.stats();
    assert_eq!(stats.str_or("end", ""), "protocol_error");
    assert_eq!(num(stats, "t"), t_req + 1000, "the turn's sync");
    assert!(r.guest.received(1).is_empty());
}

#[test]
fn events_after_the_block_point_wait_for_the_next_turn() {
    // at 160 MHz `run` returns a bus cycle after B: a byte written in that cycle is after B, so
    // it goes out after the peer's wait, with the next seq
    let b = 1_500_001;
    let mut guest = FakeGuest::new(vec![
        (cycles(b) - 1, Act::Tx(1, b'x')),
        (cycles(b), Act::Tx(1, b'y')),
    ])
    .with_routes(&[modem(115_200)]);
    guest.overshoot = 1;
    let mut turns = 0;
    let peer = FakePeer::new(&[MODEM_TX, MODEM_RX], move |turn| {
        turns += 1;
        match turns {
            1 => vec![wait(turn.seq, Some(b), None)],
            2 => {
                assert_eq!(turn.t, b);
                let tx: Vec<&Json> = turn.msgs.iter().filter(|m| kind(m) == "uart_tx").collect();
                assert_eq!(tx.len(), 1);
                assert_eq!(data(tx[0]), b"x", "only the byte at or before B");
                vec![wait(turn.seq, None, None)]
            }
            _ => vec![wait(turn.seq, None, None)],
        }
    });
    let r = run(0.01, guest, peer);
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    let types = r.peer.types();
    let sync_at_b = types.iter().rposition(|&k| k == "sync").unwrap();
    let tx = r.peer.of("uart_tx");
    assert_eq!(tx.len(), 2);
    assert_eq!(data(tx[1]), b"y");
    assert!(num(tx[1], "t") > b);
    assert_eq!(
        types[sync_at_b + 1],
        "uart_tx",
        "the held byte follows the wait"
    );
}

#[test]
fn a_block_point_already_behind_the_guest_takes_the_turn_at_once() {
    // run returns a bus cycle after B (160 MHz); the peer's next quiet_until falls in between,
    // 1 ns after B: the runner takes that turn too, without running the guest
    let b = 1_000_000;
    let mut guest = FakeGuest::new(vec![]);
    guest.overshoot = 1;
    let mut syncs = Vec::new();
    let peer = FakePeer::new(&[], move |turn| {
        syncs.push(turn.t);
        match syncs.len() {
            1 => vec![wait(0, Some(b), None)],
            2 => vec![wait(0, Some(b + 1), None)],
            _ => vec![wait(0, None, None)],
        }
    });
    let r = run(0.01, guest, peer);
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    let syncs: Vec<u64> = r.peer.of("sync").iter().map(|m| num(m, "t")).collect();
    assert_eq!(syncs, [0, b, b + 1]);
    assert!(ns(cycles(b) + 1) > b + 1, "B + 1 is behind the guest");
    // the slice to B, then on to the next millisecond: none between the two turns
    let after_b: Vec<u64> = r
        .guest
        .slices
        .iter()
        .copied()
        .filter(|&s| s > cycles(b))
        .collect();
    assert_eq!(after_b[0], cycles(2 * MS));
}

#[test]
fn uart_tx_coalesces_and_ends_on_a_gap_a_route_change_and_another_message() {
    // 115200 baud: a character is 86.8 µs, about 20 833 cycles
    let char_cycles = cycles(char_ns(1, 115_200));
    // back to back, a character time apart
    let mut script = tx(cycles(MS), 1, b"AT", char_cycles - 1);
    // a gap of more than one character time: a new message
    script.extend(tx(cycles(MS) + 4 * char_cycles, 1, b"+C", char_cycles - 1));
    // the route changes baud: a uart_cfg, then a new message
    script.push((cycles(MS) + 6 * char_cycles, Act::Route(modem(9600))));
    script.extend(tx(cycles(MS) + 7 * char_cycles, 1, b"Q", 0));
    // console output in between (--console socket) ends a message too
    script.extend(tx(cycles(MS) + 8 * char_cycles, 1, b"1", 0));
    script.extend(tx(cycles(MS) + 8 * char_cycles + 10, 0, b"log\n", 10));
    script.extend(tx(cycles(MS) + 8 * char_cycles + 100, 1, b"2", 0));
    let guest = FakeGuest::new(script)
        .with_routes(&[modem(115_200), route(0, Some(1), Some(3), Some(115_200))]);
    let peer = FakePeer::new(&[MODEM_TX, MODEM_RX], idle(None, None));
    let r = run_for(0.01, guest, peer, true);
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    let sent: Vec<(String, Vec<u8>)> = r
        .peer
        .msgs
        .iter()
        .filter(|m| matches!(kind(m), "uart_tx" | "console" | "uart_cfg"))
        .map(|m| {
            let k = kind(m).to_string();
            let d = if k == "uart_cfg" { vec![] } else { data(m) };
            (k, d)
        })
        .collect();
    let expect = |k: &str, d: &[u8]| (k.to_string(), d.to_vec());
    assert_eq!(
        sent,
        [
            expect("uart_cfg", b""),
            expect("uart_tx", b"AT"),
            expect("uart_tx", b"+C"),
            expect("uart_cfg", b""),
            expect("uart_tx", b"Q1"),
            expect("console", b"log\n"),
            expect("uart_tx", b"2"),
        ]
    );
    let cfg = r.peer.of("uart_cfg");
    assert_eq!(num(cfg[1], "baud"), 9600);
    assert_eq!(r.stats.uart[1].tx_bytes, 7);
    assert_eq!(r.stats.turns, 1, "no latency, no quiet_until: one turn");
}

#[test]
fn the_console_ends_a_message_after_a_newline_and_unwatched_routes_stay_off_the_socket() {
    let script = tx(cycles(MS), 0, b"ab\ncd\n", 100);
    let guest = FakeGuest::new(script).with_routes(&[route(0, Some(1), Some(3), Some(115_200))]);
    let r = run_for(
        0.01,
        guest,
        FakePeer::new(&[GNSS_TX], idle(None, None)),
        true,
    );
    let console: Vec<Vec<u8>> = r.peer.of("console").iter().map(|m| data(m)).collect();
    assert_eq!(console, [b"ab\n".to_vec(), b"cd\n".to_vec()]);
    assert!(r.peer.of("uart_tx").is_empty());
    assert!(r.peer.of("uart_cfg").is_empty());
    // without --console socket, nothing
    let script = tx(cycles(MS), 0, b"ab\n", 100);
    let guest = FakeGuest::new(script).with_routes(&[route(0, Some(1), Some(3), Some(115_200))]);
    let r = run(0.01, guest, FakePeer::new(&[GNSS_TX], idle(None, None)));
    assert_eq!(r.peer.types(), ["hello", "sync", "stats"]);
}

#[test]
fn bytes_before_the_block_point_go_out_before_the_sync() {
    // a stream of bytes one cycle apart across quiet_until: the message is cut at B. The slice
    // ends at B's cycle, so a byte written in that cycle comes after the runner has taken
    // control: it goes out after the sync, at t = B (protocol 5.6: a sync's t can repeat)
    let b = 1_000_000;
    let start = cycles(b) - 3;
    let guest = FakeGuest::new(tx(start, 1, b"abcdef", 1)).with_routes(&[modem(115_200)]);
    let peer = FakePeer::new(&[MODEM_TX], move |turn| {
        vec![wait(turn.seq, (turn.t == 0).then_some(b), None)]
    });
    let r = run(0.01, guest, peer);
    let types = r.peer.types();
    let tx = r.peer.of("uart_tx");
    assert_eq!(data(tx[0]), b"abc");
    assert!(num(tx[0], "t") < b);
    assert_eq!(data(tx[1]), b"def");
    assert_eq!(num(tx[1], "t"), b);
    let first_tx = types.iter().position(|&k| k == "uart_tx").unwrap();
    assert_eq!(
        &types[first_tx..first_tx + 3],
        ["uart_tx", "sync", "uart_tx"]
    );
}

#[test]
fn a_reset_is_forwarded_with_its_cause_its_hint_and_the_reset_routes() {
    let at = cycles(2 * MS) + 5;
    let guest = FakeGuest::new(vec![
        (cycles(MS), Act::Tx(1, b'x')),
        // ESP_RST_PANIC, as `esp_reset_reason_set_hint` writes it
        (at - 1, Act::Hint(0x8004_0004)),
        (at, Act::Reset(12)),
        (at + 100, Act::Route(modem(115_200))),
    ])
    .with_routes(&[modem(115_200)]);
    let r = run(
        0.01,
        guest,
        FakePeer::new(&[MODEM_TX, MODEM_RX], idle(None, None)),
    );
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    let events: Vec<(String, u64)> = r
        .peer
        .msgs
        .iter()
        .filter(|m| m.get("seq").is_some())
        .map(|m| (kind(m).to_string(), num(m, "t")))
        .collect();
    let t = ns(at);
    let first_cfg = ns(0);
    // power-on route, the byte, the reset, the reset routes and pins, and the route the firmware
    // sets up again: the next slice boundary sees it, at most a millisecond later
    assert_eq!(
        events,
        [
            ("uart_cfg".into(), first_cfg),
            ("uart_tx".into(), ns(cycles(MS))),
            ("reset".into(), t),
            ("uart_cfg".into(), t),
            ("gpio_out".into(), t),
            ("uart_cfg".into(), ns(cycles(3 * MS))),
        ]
    );
    assert_eq!(num(r.peer.of("reset")[0], "cause"), 12);
    assert_eq!(num(r.peer.of("reset")[0], "hint"), 0x8004_0004);
    let after_reset = r.peer.of("uart_cfg")[1];
    assert_eq!(after_reset.get("baud"), Some(&Json::Null));
    assert_eq!(after_reset.get("rx_pin"), Some(&Json::Null));
    assert_eq!(r.stats.resets, 1);
    assert_eq!(num(r.peer.stats(), "resets"), 1);
}

/// The `uptime` messages of a run, as `t` and `(earliest, latest)`, `None` for `null`.
fn uptimes(peer: &FakePeer) -> Vec<(u64, Option<(u64, u64)>)> {
    peer.of("uptime")
        .iter()
        .map(|m| {
            let zero = match (m.get("earliest"), m.get("latest")) {
                (Some(Json::Null), Some(Json::Null)) => None,
                _ => Some((num(m, "earliest"), num(m, "latest"))),
            };
            (num(m, "t"), zero)
        })
        .collect()
}

/// The classic chip's LACT counting from `load` at ESP-IDF's rate: 2 counts a microsecond, 120
/// bus cycles a count at 240 MHz.
fn lact(load: u64) -> Act {
    Act::Uptime(Uptime::Counting { load, tick: 120 })
}

#[test]
fn a_counter_seen_counting_is_placed_between_the_boundary_before_and_the_one_that_saw_it() {
    // the classic chip's LACT: the runner sees that it counts, from what it was loaded with
    let load = cycles(MS / 4) / 120;
    let loaded = load * 120;
    let at = |ms: u64| cycles(ms * MS);
    let guest = FakeGuest::new(vec![
        (cycles(2 * MS + MS / 2), lact(0)),
        (at(4) + 5, Act::Reset(12)),
        (cycles(6 * MS + MS / 3), lact(load)),
        // stopped and started again in one boot: the zero sent holds until the new start
        (cycles(7 * MS + MS / 2), Act::Uptime(Uptime::Stopped)),
        (cycles(8 * MS + MS / 2), lact(0)),
        // loaded anew while it counts, as `esp_timer_impl_set` does after a light sleep: placed
        // a boundary later, once LACTLOAD has surely taken it
        (cycles(10 * MS + MS / 2), lact(load)),
        // another rate and the same load: the APB clock or the divider changed, no new zero
        (
            cycles(13 * MS + MS / 2),
            Act::Uptime(Uptime::Counting { load, tick: 240 }),
        ),
    ]);
    let r = run(0.015, guest, FakePeer::new(&[], idle(None, None)));
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    assert_eq!(
        uptimes(&r.peer),
        [
            (ns(at(3)), Some((ns(at(2)), ns(at(3))))),
            // from the reset at the earliest, which stopped it, less what the load counts
            (ns(at(7)), Some((ns(at(6) - loaded), ns(at(7) - loaded)))),
            (ns(at(9)), Some((ns(at(8)), ns(at(9))))),
            (ns(at(12)), Some((ns(at(10) - loaded), ns(at(12) - loaded)))),
        ]
    );
    // not an event: the stream's seq counts the others only
    assert!(r.peer.of("uptime").iter().all(|m| m.get("seq").is_none()));
}

#[test]
fn a_load_that_counts_from_before_power_on_has_no_zero_on_the_classic_chip() {
    // loaded with 10 ms of counts 2.5 ms into the run: its zero is before power-on
    let past_the_run = cycles(10 * MS) / 120;
    let guest = FakeGuest::new(vec![
        (cycles(MS + MS / 2), lact(0)),
        (cycles(2 * MS + MS / 2), lact(past_the_run)),
        // started from it after a stop: no zero, and nothing to say twice
        (cycles(4 * MS + MS / 2), Act::Uptime(Uptime::Stopped)),
        (cycles(5 * MS + MS / 2), lact(past_the_run)),
    ]);
    let r = run(0.007, guest, FakePeer::new(&[], idle(None, None)));
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    let at = |ms: u64| ns(cycles(ms * MS));
    assert_eq!(
        uptimes(&r.peer),
        [(at(2), Some((at(1), at(2)))), (at(4), None)]
    );
}

#[test]
fn a_counter_read_at_its_zero_is_sent_again_only_when_its_zero_moves() {
    // a SYSTIMER: the runner reads where it counted zero, to within one of its ticks
    let zero = |earliest: u64, latest: u64| Uptime::Zero { earliest, latest };
    let first = (cycles(MS), cycles(MS) + 14);
    let reloaded = (cycles(3 * MS) + 11, cycles(3 * MS) + 25);
    let guest = FakeGuest::new(vec![
        (cycles(MS) + 7, Act::Uptime(zero(first.0, first.1))),
        // the same zero, read at another part of a tick
        (
            cycles(2 * MS + MS / 2),
            Act::Uptime(zero(first.0 + 9, first.1 + 9)),
        ),
        // loaded anew
        (
            cycles(3 * MS + MS / 2),
            Act::Uptime(zero(reloaded.0, reloaded.1)),
        ),
        // loaded past what the run has lasted: the zero sent no longer holds, once
        (cycles(5 * MS + MS / 2), Act::Uptime(Uptime::BeforePowerOn)),
        (cycles(6 * MS + MS / 2), Act::Uptime(Uptime::Stopped)),
    ]);
    let r = run(0.008, guest, FakePeer::new(&[], idle(None, None)));
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    assert_eq!(
        uptimes(&r.peer),
        [
            // `earliest` rounded down, `latest` up
            (ns(cycles(2 * MS)), Some((ns(first.0), ns_up(first.1)))),
            (
                ns(cycles(4 * MS)),
                Some((ns(reloaded.0), ns_up(reloaded.1)))
            ),
            (ns(cycles(6 * MS)), None),
        ]
    );
    // 25 cycles past a whole millisecond is no whole nanosecond
    assert_eq!(ns_up(reloaded.1), ns(reloaded.1) + 1);
}

#[test]
fn a_counter_that_stops_keeps_the_zero_sent() {
    // a counter that stops, as the classic chip's LACT does in deep sleep: the zero sent dates
    // every stamp taken before the stop; a SYSTIMER's reads, compared with it after each stop
    let zero = |earliest: u64, latest: u64| Uptime::Zero { earliest, latest };
    let first = (cycles(MS), cycles(MS) + 14);
    let moved = (cycles(5 * MS) + 3, cycles(5 * MS) + 17);
    let guest = FakeGuest::new(vec![
        (cycles(MS) + 7, Act::Uptime(zero(first.0, first.1))),
        (cycles(2 * MS + MS / 2), Act::Uptime(Uptime::Stopped)),
        // counting again from the same zero: what was sent before the stop holds
        (cycles(3 * MS + MS / 2), Act::Uptime(zero(first.0, first.1))),
        (cycles(4 * MS + MS / 2), Act::Uptime(Uptime::Stopped)),
        // counting again from another: sent
        (cycles(5 * MS + MS / 2), Act::Uptime(zero(moved.0, moved.1))),
        (cycles(6 * MS + MS / 2), Act::Uptime(Uptime::Stopped)),
        (cycles(7 * MS + MS / 2), Act::Reset(5)),
    ]);
    let r = run(0.009, guest, FakePeer::new(&[], idle(None, None)));
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    assert_eq!(
        uptimes(&r.peer),
        [
            (ns(cycles(2 * MS)), Some((ns(first.0), ns_up(first.1)))),
            (ns(cycles(6 * MS)), Some((ns(moved.0), ns_up(moved.1)))),
        ]
    );
    assert_eq!(r.peer.of("reset").len(), 1);
}

#[test]
fn a_lact_stopped_by_a_deep_sleep_leaves_the_boot_its_zero_until_the_wake() {
    // esp32sim's `enter_deep_sleep` replaces the timer groups, which clears LACT_EN, before the
    // wake's reset: the boot that slept gets no `null`, the wake's boot a zero of its own
    let at = |ms: u64| cycles(ms * MS);
    let guest = FakeGuest::new(vec![
        (cycles(MS + MS / 2), lact(0)),
        (cycles(2 * MS + MS / 2), Act::Uptime(Uptime::Stopped)),
        (cycles(4 * MS + MS / 2), Act::Reset(5)),
        (cycles(5 * MS + MS / 2), lact(0)),
    ]);
    let r = run(0.007, guest, FakePeer::new(&[], idle(None, None)));
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    let reset = num(r.peer.of("reset")[0], "t");
    assert_eq!(
        uptimes(&r.peer),
        [
            (ns(at(2)), Some((ns(at(1)), ns(at(2))))),
            (ns(at(6)), Some((ns(at(5)), ns(at(6))))),
        ]
    );
    assert!(ns(at(2)) < reset && reset < ns(at(6)));
}

#[test]
fn bytes_after_a_reset_are_dropped_until_the_firmware_routes_its_uart_again() {
    let at = cycles(MS);
    let guest = FakeGuest::new(vec![
        (at, Act::Reset(12)),
        (
            cycles(6 * MS),
            Act::Route(route(1, None, Some(GNSS_RX), Some(9600))),
        ),
    ])
    .with_routes(&[route(1, None, Some(GNSS_RX), Some(9600))]);
    let mut turns = 0;
    let peer = FakePeer::new(&[GNSS_RX], move |turn| {
        turns += 1;
        match turns {
            1 => vec![
                uart_rx(1_500_000, GNSS_RX, 9600, b"lost"),
                uart_rx(8 * MS, GNSS_RX, 9600, b"kept"),
                wait(turn.seq, None, None),
            ],
            _ => vec![wait(turn.seq, None, None)],
        }
    });
    let r = run(0.02, guest, peer);
    assert_eq!(r.guest.received(1), b"kept");
    assert_eq!(r.stats.unrouted_rx_bytes, 4);
    let warns = r.peer.of("warn");
    assert_eq!(warns.len(), 1, "{warns:?}");
    assert_eq!(warns[0].str_or("kind", ""), "unrouted_rx");
    assert_eq!(num(warns[0], "bytes"), 4);
    assert_eq!(num(warns[0], "t"), ns(cycles(1_500_000)));
}

#[test]
fn a_baud_mismatch_is_one_merged_frm_err_warning() {
    // the firmware runs UART1 at 115200, the GNSS sends at 9600: every byte is a frame error
    let guest = FakeGuest::new(vec![]).with_routes(&[route(1, None, Some(GNSS_RX), Some(115_200))]);
    let peer = FakePeer::new(&[GNSS_RX], {
        let mut first = true;
        move |turn| {
            let mut out = Vec::new();
            if std::mem::take(&mut first) {
                out.push(uart_rx(MS, GNSS_RX, 9600, b"$GPGGA*6E\r\n"));
            }
            out.push(wait(turn.seq, None, None));
            out
        }
    });
    let r = run(0.05, guest, peer);
    assert!(r.guest.received(1).is_empty());
    assert_eq!(r.guest.lost[1].len(), 11);
    assert!(r.guest.lost[1].iter().all(|&(_, l)| l == Lost::FrameError));
    let warns = r.peer.of("warn");
    assert_eq!(warns.len(), 1);
    let w = warns[0];
    assert_eq!(w.str_or("kind", ""), "frm_err");
    assert_eq!(
        (num(w, "port"), num(w, "pin"), num(w, "baud")),
        (1, 5, 115_200)
    );
    assert_eq!(num(w, "bytes"), 11);
    assert_eq!(r.stats.uart[1].frm_err, 11);
    let stats = r.peer.stats();
    let uart = stats.get("uart").and_then(Json::as_arr).unwrap();
    assert_eq!(uart.len(), 1);
    assert_eq!(num(&uart[0], "frm_err"), 11);
    // a warning is the last message before stats: nothing held it back
    let types = r.peer.types();
    assert_eq!(types[types.len() - 2], "warn");
}

#[test]
fn a_full_rx_fifo_drops_bytes_and_a_warning_is_cut_by_other_messages() {
    let guest = FakeGuest::new(vec![
        // console output between the drops ends the first warning
        (cycles(2 * MS) - 1, Act::Tx(1, b'!')),
        (cycles(3 * MS), Act::Drain(1)),
    ])
    .with_routes(&[route(1, Some(MODEM_TX), Some(MODEM_RX), Some(921_600))]);
    let peer = FakePeer::new(&[MODEM_TX, MODEM_RX], {
        let mut first = true;
        move |turn| {
            let mut out = Vec::new();
            if std::mem::take(&mut first) {
                // 130 bytes at 921600 baud take 1.41 ms: two overflow before the tx at 2 ms,
                // the rest of the stream after it
                out.push(uart_rx(MS / 2, MODEM_RX, 921_600, &[b'x'; 128 + 2]));
                out.push(uart_rx(MS * 2, MODEM_RX, 921_600, &[b'y'; 10]));
            }
            out.push(wait(turn.seq, None, None));
            out
        }
    });
    let r = run(0.01, guest, peer);
    let warns: Vec<(u64, u64)> = r
        .peer
        .of("warn")
        .iter()
        .map(|w| {
            assert_eq!(w.str_or("kind", ""), "rxfifo_ovf");
            (num(w, "t"), num(w, "bytes"))
        })
        .collect();
    // 2 of the first message, then 10 of the second until the guest drains its FIFO at 3 ms
    assert_eq!(warns.iter().map(|w| w.1).collect::<Vec<_>>(), [2, 10]);
    assert!(warns[0].0 < 2 * MS && warns[1].0 >= 2 * MS);
    assert_eq!(r.stats.uart[1].rxfifo_ovf, 12);
    assert_eq!(r.stats.uart[1].rx_bytes, 128);
    // each lost byte raised the overflow interrupt, and none reached the FIFO after the drain
    assert_eq!(r.guest.lost[1].len(), 12);
    assert!(r.guest.lost[1].iter().all(|&(_, l)| l == Lost::Overflow));
    assert!(r.guest.received(1).is_empty());
}

#[test]
fn the_runner_refuses_what_the_protocol_forbids() {
    for (turn1, found) in [
        (vec![wait(5, None, None)], "ack 5"),
        (vec![wait(0, Some(0), None)], "quiet_until 0 is not after"),
        (
            vec![uart_rx(0, 6, 9600, b"x"), wait(0, None, None)],
            "GPIO 6 is not in",
        ),
        (
            vec![uart_rx(0, GNSS_RX, 0, b"x"), wait(0, None, None)],
            "baud of 0",
        ),
        (
            vec![r#"{"type":"gpio_in","t":0,"levels":[[6,1]]}"#.to_string()],
            "GPIO 6 is not in",
        ),
        (
            vec![r#"{"type":"release","t":0,"pins":[5,6]}"#.to_string()],
            "GPIO 6 is not in",
        ),
        (
            vec![r#"{"type":"adc","t":0,"pin":5,"volts":1.0}"#.to_string()],
            "adc is not implemented yet: ",
        ),
        (vec![r#"{"type":"bogus"}"#.to_string()], "unknown type"),
        (
            vec![r#"{"type":"hello","version":2,"chip":"esp32"}"#.to_string()],
            "hello after",
        ),
    ] {
        let mut lines = Some(turn1);
        let peer = FakePeer::new(&[GNSS_RX], move |_| lines.take().expect("one turn"));
        let r = run(0.01, FakeGuest::new(vec![]), peer);
        let End::Protocol(detail) = &r.end else {
            panic!("{found}: {:?}", r.end)
        };
        assert!(detail.contains(found), "{detail}");
        assert_eq!(r.peer.stats().str_or("end", ""), "protocol_error");
        assert!(r.guest.slices.is_empty());
    }
}

#[test]
fn stop_ends_the_run_and_drops_what_came_after_the_block_point() {
    // B between two cycles: the byte at cycles(b) is after it
    let b = 1_500_001;
    let mut guest = FakeGuest::new(vec![(cycles(b), Act::Tx(1, b'y'))]).with_routes(&[modem(9600)]);
    guest.overshoot = 1;
    let mut turns = 0;
    let peer = FakePeer::new(&[MODEM_TX], move |turn| {
        turns += 1;
        match turns {
            1 => vec![
                uart_rx(5 * MS, MODEM_TX, 9600, b"ignored"),
                wait(turn.seq, Some(b), None),
            ],
            // inputs before a stop in the same turn are discarded
            _ => vec![
                uart_rx(5 * MS, MODEM_TX, 9600, b"discarded"),
                r#"{"type":"stop"}"#.into(),
            ],
        }
    });
    let r = run(0.01, guest, peer);
    assert!(matches!(r.end, End::Stop), "{:?}", r.end);
    assert_eq!(r.end.exit_code(), 0);
    assert!(
        r.peer.of("uart_tx").is_empty(),
        "the byte after B is dropped"
    );
    let stats = r.peer.stats();
    assert_eq!(stats.str_or("end", ""), "stop");
    assert_eq!(num(stats, "t"), b);
    assert_eq!(num(stats, "inputs_not_applied"), 1);
}

#[test]
fn the_end_of_the_run_sends_what_is_left_and_counts_what_never_reached_the_chip() {
    let end = 10 * MS;
    let guest = FakeGuest::new(tx(cycles(end) - 10, 1, b"bye", 1)).with_routes(&[route(
        1,
        Some(MODEM_TX),
        Some(MODEM_RX),
        Some(9600),
    )]);
    let peer = FakePeer::new(&[MODEM_TX, MODEM_RX], {
        let mut first = true;
        move |turn| {
            let mut out = Vec::new();
            if std::mem::take(&mut first) {
                // the last bytes of this one are still on the line at the end
                out.push(uart_rx(end - 2 * MS, MODEM_RX, 9600, b"12345"));
                out.push(uart_rx(end + MS, MODEM_RX, 9600, b"later"));
            }
            // a block point after the end is not taken
            out.push(wait(turn.seq, Some(end + 1), Some(5 * MS)));
            out
        }
    });
    let r = run(0.01, guest, peer);
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    assert_eq!(data(r.peer.of("uart_tx")[0]), b"bye");
    let stats = r.peer.stats();
    assert_eq!(stats.str_or("end", ""), "max_seconds");
    assert_eq!(num(stats, "t"), ns(cycles(end)));
    assert_eq!(num(stats, "turns"), 1);
    assert_eq!(num(stats, "inputs_not_applied"), 1);
    // two bytes of "12345" fit before the end: 2 ms at 9600 baud
    assert_eq!(r.guest.received(1), b"12");
    assert_eq!(num(stats, "rx_bytes_not_paced"), 3);
}

#[test]
fn an_emulator_stop_ends_the_run_with_its_variant_in_stats() {
    let guest = FakeGuest::new(vec![
        (cycles(MS), Act::Tx(1, b'x')),
        (cycles(MS) + 1, Act::Fault),
    ])
    .with_routes(&[modem(9600)]);
    let r = run(0.01, guest, FakePeer::new(&[MODEM_TX], idle(None, None)));
    assert!(
        matches!(r.end, End::Emulator(Stop::Exceptions(3))),
        "{:?}",
        r.end
    );
    assert_eq!(r.end.exit_code(), 1);
    assert_eq!(data(r.peer.of("uart_tx")[0]), b"x");
    let stats = r.peer.stats();
    assert_eq!(stats.str_or("end", ""), "emulator");
    assert_eq!(stats.str_or("stop", ""), "Exceptions");
    assert_eq!(stats.str_or("detail", ""), "Exceptions(3)");
}

#[test]
fn a_signal_ends_the_slice_in_progress_with_stats() {
    struct Signalling<'a>(FakeGuest, &'a AtomicBool);
    impl Guest for Signalling<'_> {
        fn cycles(&self) -> u64 {
            self.0.cycles()
        }
        fn run_until(&mut self, cycle: u64) -> Stop {
            self.1.store(true, Ordering::Relaxed);
            self.0.run_until(cycle)
        }
        fn chip_reset(&mut self) -> u32 {
            self.0.chip_reset()
        }
        fn power_on_reset(&mut self, rom: &[u8]) -> u32 {
            self.0.power_on_reset(rom)
        }
        fn idle_until(&mut self, cycle: u64) {
            self.0.idle_until(cycle)
        }
        fn uart_route(&self, port: usize) -> UartRoute {
            self.0.uart_route(port)
        }
        fn rx_pending(&self, port: usize) -> usize {
            self.0.rx_pending(port)
        }
        fn uart_receive(&mut self, port: usize, byte: u8) {
            self.0.uart_receive(port, byte)
        }
        fn uart_frame_error(&mut self, port: usize) {
            self.0.uart_frame_error(port)
        }
        fn gpio_drive(&self) -> (u64, u64) {
            self.0.gpio_drive()
        }
        fn take_gpio_inputs(&mut self) {
            self.0.take_gpio_inputs()
        }
    }
    let signal = AtomicBool::new(false);
    let inner = FakeGuest::new(tx(10, 1, b"x", 0)).with_routes(&[modem(9600)]);
    let shared = inner.shared.clone();
    let mut guest = Signalling(inner, &signal);
    let mut peer = FakePeer::new(&[MODEM_TX], idle(None, None));
    let opts = Options {
        end_cycle: 10 * SECOND,
        console: false,
        usb_console: false,
        uart0_stream: false,
        peer_timeout: None,
        signal: &signal,
        parent_gone: None,
        wifi: None,
        rom: &[],
        card: None,
        realtime: false,
        flash_out: None,
        hci: None,
        no_bt: false,
    };
    let (end, _) = Session::new(&mut guest, &mut peer, shared, opts).run();
    assert!(matches!(end, End::Signal), "{end:?}");
    assert_eq!(end.exit_code(), 0);
    assert_eq!(
        guest.0.slices,
        [cycles(MS)],
        "one slice, at most a millisecond"
    );
    assert_eq!(data(peer.of("uart_tx")[0]), b"x");
    let stats = peer.stats();
    assert_eq!(stats.str_or("end", ""), "signal");
    assert_eq!(num(stats, "t"), ns(cycles(MS)));
}

#[test]
fn a_peer_that_goes_away_ends_the_run_with_nothing_more() {
    let mut turns = 0;
    let peer = FakePeer::new(&[], move |_| {
        turns += 1;
        if turns == 1 {
            vec![wait(0, Some(MS), None)]
        } else {
            vec![]
        }
    });
    let r = run(0.01, FakeGuest::new(vec![]), peer);
    assert!(matches!(r.end, End::PeerGone(_)), "{:?}", r.end);
    assert_eq!(r.end.exit_code(), 4);
    assert_eq!(r.peer.types(), ["hello", "sync", "sync"]);
}

#[test]
fn latency_zero_costs_a_turn_per_event() {
    let char_cycles = cycles(char_ns(1, 115_200));
    let guest =
        FakeGuest::new(tx(cycles(MS), 1, b"ab", 2 * char_cycles)).with_routes(&[modem(115_200)]);
    let r = run(
        0.005,
        guest,
        FakePeer::new(&[MODEM_TX], idle(None, Some(0))),
    );
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    let syncs: Vec<u64> = r.peer.of("sync").iter().map(|m| num(m, "t")).collect();
    let tx: Vec<u64> = r.peer.of("uart_tx").iter().map(|m| num(m, "t")).collect();
    assert_eq!(tx.len(), 2);
    assert_eq!(syncs, [0, tx[0], tx[1]]);
}

/// On the emulator itself, no ROM and no firmware: a few instructions in IRAM write one byte
/// into UART1's TX FIFO and sleep in `waiti`. The core runs from the 40 MHz XTAL, so a CPU
/// cycle is 6 bus cycles and `run` returns up to 5 bus cycles after a slice's end. The slices
/// must still stop the sleeping guest at the block point, and the answer reach the RX FIFO.
#[test]
fn on_the_emulator_the_slices_stop_a_guest_asleep_in_waiti_at_the_block_point() {
    use emu_core::Bus;
    use esp_soc::Soc;
    // assembled with xtensa-esp32-elf-as --no-transform from:
    //   movi a2, 0x3ff; slli a2, a2, 20; movi a3, 0x50; slli a3, a3, 12; add a2, a2, a3
    //   movi a4, 1000; 1: addi a4, a4, -1; bnez a4, 1b      (a few thousand bus cycles)
    //   movi a5, 'A'; s32i a5, a2, 0; memw                 (UART1 FIFO at 0x3ff50000)
    //   2: waiti 0; j 2b
    const PROGRAM: [u8; 39] = [
        0x22, 0xa3, 0xff, 0xc0, 0x22, 0x01, 0x32, 0xa0, 0x50, 0x40, 0x33, 0x11, 0x30, 0x22, 0x80,
        0x42, 0xa3, 0xe8, 0x42, 0xc4, 0xff, 0x56, 0x94, 0xff, 0x52, 0xa0, 0x41, 0x52, 0x62, 0x00,
        0xc0, 0x20, 0x00, 0x00, 0x70, 0x00, 0x46, 0xfe, 0xff,
    ];
    const ENTRY: u32 = 0x4008_0000;

    /// The machine, with where each slice was asked to end, where it did, and whether the
    /// core was asleep there.
    struct Watched {
        m: esp32::Machine,
        slices: Vec<(u64, u64, bool)>,
    }
    impl Guest for Watched {
        fn cycles(&self) -> u64 {
            Guest::cycles(&self.m)
        }
        fn run_until(&mut self, cycle: u64) -> Stop {
            let stop = Guest::run_until(&mut self.m, cycle);
            self.slices
                .push((cycle, Guest::cycles(&self.m), self.m.cores[0].waiting));
            stop
        }
        fn chip_reset(&mut self) -> u32 {
            self.m.chip_reset()
        }
        fn power_on_reset(&mut self, rom: &[u8]) -> u32 {
            self.m.power_on_reset(rom)
        }
        fn idle_until(&mut self, cycle: u64) {
            self.m.idle_until(cycle)
        }
        fn uart_route(&self, port: usize) -> UartRoute {
            Guest::uart_route(&self.m, port)
        }
        fn rx_pending(&self, port: usize) -> usize {
            Guest::rx_pending(&self.m, port)
        }
        fn uart_receive(&mut self, port: usize, byte: u8) {
            Guest::uart_receive(&mut self.m, port, byte)
        }
        fn uart_frame_error(&mut self, port: usize) {
            Guest::uart_frame_error(&mut self.m, port)
        }
        fn gpio_drive(&self) -> (u64, u64) {
            Guest::gpio_drive(&self.m)
        }
        fn take_gpio_inputs(&mut self) {
            Guest::take_gpio_inputs(&mut self.m)
        }
    }

    let shared = Shared::default();
    let mut m = esp32::machine([0x24, 0x6f, 0x28, 0x00, 0x11, 0x22], 4 << 20);
    m.bus.board = Box::new(crate::socket_board::SocketBoard::new(shared.clone(), None));
    m.bus.attach_board_devices();
    // UART1 TX on GPIO 26 and RX on GPIO 27 through the GPIO matrix, 115200 baud from APB
    for (addr, value) in [
        (0x3ff4_9028, 2 << 12 | 1 << 9), // IO_MUX GPIO26: GPIO function, input enabled
        (0x3ff4_902c, 2 << 12 | 1 << 9), // IO_MUX GPIO27
        (0x3ff4_4530 + 4 * 26, 17),      // GPIO_FUNC26_OUT_SEL_CFG: U1TXD_OUT
        (0x3ff4_4024, 1 << 26),          // GPIO_ENABLE_W1TS
        (0x3ff4_4130 + 4 * 17, 0x80 | 27), // GPIO_FUNC17_IN_SEL_CFG: U1RXD_IN from GPIO27
        (0x3ff5_0014, 694),              // UART1 CLKDIV
    ] {
        m.bus.write32(addr, value).unwrap();
    }
    let route = Guest::uart_route(&m, 1);
    assert_eq!(
        (route.tx_pins, route.rx_pin),
        (1 << MODEM_TX, Some(MODEM_RX))
    );
    let baud = route.baud.unwrap();
    m.bus.load_bytes(ENTRY, &PROGRAM).unwrap();
    <esp32::Esp32 as Soc>::boot_core(&mut m.cores[0], ENTRY);

    let latency = 300_000;
    let mut turns = 0;
    let peer = FakePeer::new(&[MODEM_TX, MODEM_RX], move |turn| {
        turns += 1;
        match turns {
            1 => vec![wait(turn.seq, None, Some(latency))],
            2 => {
                let tx = turn
                    .msgs
                    .iter()
                    .find(|m| kind(m) == "uart_tx")
                    .expect("the request");
                assert_eq!(data(tx), b"A");
                let t_req = num(tx, "t");
                assert_eq!(turn.t, t_req + latency);
                vec![
                    uart_rx(t_req + latency, MODEM_RX, baud, b"OK"),
                    wait(turn.seq, None, None),
                ]
            }
            _ => vec![wait(turn.seq, None, None)],
        }
    });
    let mut guest = Watched {
        m,
        slices: Vec::new(),
    };
    let mut peer = peer;
    let signal = AtomicBool::new(false);
    let opts = Options {
        end_cycle: cycles(5 * MS),
        console: false,
        usb_console: false,
        uart0_stream: false,
        peer_timeout: None,
        signal: &signal,
        parent_gone: None,
        wifi: None,
        rom: &[],
        card: None,
        realtime: false,
        flash_out: None,
        hci: None,
        no_bt: false,
    };
    let (end, stats) = Session::new(&mut guest, &mut peer, shared, opts).run();
    assert!(matches!(end, End::MaxSeconds), "{end:?}");
    assert_eq!(stats.turns, 2);

    let tx = peer.of("uart_tx")[0].clone();
    assert_eq!(peer.of("uart_tx").len(), 1);
    assert_eq!(num(&tx, "port"), 1);
    let b = cycles(num(&tx, "t") + latency);
    // the slice that ended at the block point: asleep, and back less than one CPU cycle late
    let &(_, at, asleep) = guest
        .slices
        .iter()
        .find(|s| s.0 == b)
        .expect("a slice ends at the block point");
    assert!(asleep, "the core sleeps in waiti");
    assert!((b..b + 6).contains(&at), "returned at {at} for B at {b}");
    // both bytes reached the FIFO, paced, and nobody read them
    assert_eq!(Guest::rx_pending(&guest.m, 1), 2);
    assert_eq!(stats.uart[1].rx_bytes, 2);
    assert!(peer.of("warn").is_empty());
}

/// On the emulator: a byte that finds the RX FIFO full is lost, as on the chip, and raises
/// `UART_RXFIFO_OVF_INT`. esp32sim's `ClassicUart::host_input` queues what does not fit and
/// releases it at the baud rate once the firmware reads (mikroverk/esp32sim#344), so the runner
/// hands bytes to the `Uart` it wraps, which drops them: the two that overflow here never reach the
/// FIFO, not even after the firmware has drained it and the line has had the time to bring them.
#[test]
fn on_the_emulator_a_byte_into_a_full_rx_fifo_is_lost_and_raises_the_overflow_interrupt() {
    use emu_core::Bus;
    use esp_soc::Soc;
    const ENTRY: u32 = 0x4008_0000;
    const UART1: u32 = 0x3ff5_0000;

    let shared = Shared::default();
    let mut m = esp32::machine([0x24, 0x6f, 0x28, 0x00, 0x11, 0x22], 4 << 20);
    m.bus.board = Box::new(SocketBoard::new(shared.clone(), None));
    m.bus.attach_board_devices();
    // UART1 RX on GPIO 27 through the GPIO matrix, 115200 baud from APB, as above
    for (addr, value) in [
        (0x3ff4_902c, 2 << 12 | 1 << 9), // IO_MUX GPIO27: GPIO function, input enabled
        (0x3ff4_4130 + 4 * 17, 0x80 | 27), // GPIO_FUNC17_IN_SEL_CFG: U1RXD_IN from GPIO27
        (UART1 + 0x14, 694),             // UART1 CLKDIV
    ] {
        m.bus.write32(addr, value).unwrap();
    }
    let baud = Guest::uart_route(&m, 1).baud.unwrap();
    // `j .`: the firmware never reads its FIFO
    m.bus.load_bytes(ENTRY, &[0x06, 0xff, 0xff]).unwrap();
    <esp32::Esp32 as Soc>::boot_core(&mut m.cores[0], ENTRY);

    let mut first = true;
    let mut peer = FakePeer::new(&[MODEM_RX], move |turn| {
        let mut out = Vec::new();
        if std::mem::take(&mut first) {
            // 130 bytes at 115200 baud take 11.3 ms
            let data: Vec<u8> = (0..130).collect();
            out.push(uart_rx(0, MODEM_RX, baud, &data));
        }
        out.push(wait(turn.seq, None, None));
        out
    });
    let signal = AtomicBool::new(false);
    let opts = Options {
        end_cycle: cycles(20 * MS),
        console: false,
        usb_console: false,
        uart0_stream: false,
        peer_timeout: None,
        signal: &signal,
        parent_gone: None,
        wifi: None,
        rom: &[],
        card: None,
        realtime: false,
        flash_out: None,
        hci: None,
        no_bt: false,
    };
    let (end, stats) = Session::new(&mut m, &mut peer, shared, opts).run();
    assert!(matches!(end, End::MaxSeconds), "{end:?}");
    assert_eq!((stats.uart[1].rx_bytes, stats.uart[1].rxfifo_ovf), (128, 2));
    let warns = peer.of("warn");
    assert_eq!(warns.len(), 1, "{warns:?}");
    assert_eq!(warns[0].str_or("kind", ""), "rxfifo_ovf");
    assert_eq!(num(warns[0], "bytes"), 2);

    let uart = &m.bus.periph.uart[1];
    assert_eq!(uart.rx_pending(), 128);
    assert!(!uart.has_queued(), "nothing waits behind the FIFO");
    // UART_RXFIFO_OVF_INT_RAW, bit 4 (ESP-IDF v5.3.6, soc/esp32/include/soc/uart_reg.h:132)
    assert_ne!(uart.int_raw & 1 << 4, 0, "UART_RXFIFO_OVF_INT_RAW");
    // the firmware drains the FIFO, and the line has 10 ms to bring anything more
    let fifo: Vec<u8> = (0..128)
        .map(|_| m.bus.read32(UART1).unwrap() as u8)
        .collect();
    assert_eq!(fifo, (0..128).collect::<Vec<u8>>());
    let later = Guest::cycles(&m) + cycles(10 * MS);
    Guest::run_until(&mut m, later);
    assert_eq!(
        m.bus.periph.uart[1].rx_pending(),
        0,
        "the lost bytes stay lost"
    );
}

#[test]
fn a_signal_in_the_peers_turn_sends_what_came_after_the_block_point_then_stats() {
    let b = 1_500_001;
    let mut guest = FakeGuest::new(vec![(cycles(b), Act::Tx(1, b'y'))]).with_routes(&[modem(9600)]);
    guest.overshoot = 1;
    let mut turns = 0;
    let peer = FakePeer::new(&[MODEM_TX], move |turn| {
        turns += 1;
        match turns {
            1 => vec![wait(turn.seq, Some(b), None)],
            _ => vec![SIGNAL.into()],
        }
    });
    let r = run(0.01, guest, peer);
    assert!(matches!(r.end, End::Signal), "{:?}", r.end);
    assert!(r.peer.closed);
    let types = r.peer.types();
    assert_eq!(types[types.len() - 3..], ["sync", "uart_tx", "stats"]);
    assert_eq!(data(r.peer.of("uart_tx")[0]), b"y");
    let stats = r.peer.stats();
    assert_eq!(stats.str_or("end", ""), "signal");
    assert_eq!(num(stats, "t"), ns(cycles(b) + 1));
}

/// A run with `--parent-fd` whose signal flag is set from the start, as `parent::watch` or a
/// `SIGTERM` leaves it, with `parent_gone` as given: it ends at the first check of the flag, after
/// turn 0.
fn signalled(parent_gone: bool) -> (End, FakePeer) {
    let mut guest = FakeGuest::new(vec![]).with_routes(&[modem(9600)]);
    let mut peer = FakePeer::new(&[MODEM_TX], idle(None, None));
    let (gone, signal) = (AtomicBool::new(parent_gone), AtomicBool::new(true));
    let shared = guest.shared.clone();
    let opts = Options {
        end_cycle: cycles(10 * MS),
        console: false,
        usb_console: false,
        uart0_stream: false,
        peer_timeout: None,
        signal: &signal,
        parent_gone: Some(&gone),
        wifi: None,
        rom: &[],
        card: None,
        realtime: false,
        flash_out: None,
        hci: None,
        no_bt: false,
    };
    let (end, _) = Session::new(&mut guest, &mut peer, shared, opts).run();
    (end, peer)
}

#[test]
fn the_parent_s_end_ends_the_run_as_the_peer_gone_without_stats() {
    let (end, peer) = signalled(true);
    match &end {
        End::PeerGone(detail) => assert_eq!(detail, crate::parent::GONE),
        end => panic!("{end:?}"),
    }
    assert_eq!(end.exit_code(), 4);
    assert!(peer.of("stats").is_empty(), "nobody is left to read stats");
}

#[test]
fn a_signal_while_the_parent_lives_ends_the_run_with_stats() {
    // the backend always passes --parent-fd, and sends SIGTERM to a runner in no held turn
    let (end, peer) = signalled(false);
    assert!(matches!(end, End::Signal), "{end:?}");
    assert_eq!(end.exit_code(), 0);
    assert_eq!(peer.of("stats").len(), 1);
    assert_eq!(peer.stats().str_or("end", ""), "signal");
}

#[test]
fn with_uart0_watched_the_console_and_uart_tx_end_together() {
    let guest = FakeGuest::new(tx(cycles(MS), 0, b"ab\ncd\n", 100)).with_routes(&[route(
        0,
        Some(1),
        Some(3),
        Some(115_200),
    )]);
    let r = run_for(0.01, guest, FakePeer::new(&[1], idle(None, None)), true);
    let sent: Vec<(&str, Vec<u8>)> = r
        .peer
        .msgs
        .iter()
        .filter(|m| matches!(kind(m), "uart_tx" | "console"))
        .map(|m| (kind(m), data(m)))
        .collect();
    assert_eq!(
        sent,
        [
            ("console", b"ab\n".to_vec()),
            ("uart_tx", b"ab\n".to_vec()),
            ("console", b"cd\n".to_vec()),
            ("uart_tx", b"cd\n".to_vec()),
        ]
    );
}

#[test]
fn a_message_goes_out_once_a_character_time_has_passed_without_its_next_byte() {
    // a request and then silence: no event, sync or other message would end it before the end
    // of the run, but the next slice boundary is a character time later
    let guest = FakeGuest::new(tx(cycles(MS), 1, b"AT\r", 100)).with_routes(&[modem(115_200)]);
    let r = run(0.01, guest, FakePeer::new(&[MODEM_TX], idle(None, None)));
    let at = r
        .peer
        .msgs
        .iter()
        .position(|m| kind(m) == "uart_tx")
        .unwrap();
    assert_eq!(data(&r.peer.msgs[at]), b"AT\r");
    // out by the boundary at 2 ms, whose idle() writes it to the socket
    assert!(
        r.peer.idles.iter().any(|&n| n == at + 1),
        "{:?}",
        r.peer.idles
    );
    assert!(r.guest.slices.contains(&cycles(2 * MS)));
}

#[test]
fn nothing_reaches_the_guest_after_the_end_of_the_run() {
    // run returns a few cycles past the end; a byte due in those cycles stays undelivered
    let end = 10 * MS;
    let mut guest =
        FakeGuest::new(vec![]).with_routes(&[route(1, None, Some(GNSS_RX), Some(9600))]);
    guest.overshoot = 3;
    let peer = FakePeer::new(&[GNSS_RX], {
        let mut first = true;
        move |turn| {
            let mut out = Vec::new();
            if std::mem::take(&mut first) {
                out.push(uart_rx(ns(cycles(end) + 1), GNSS_RX, 9600, b"x"));
            }
            out.push(wait(turn.seq, None, None));
            out
        }
    });
    let r = run(0.01, guest, peer);
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    assert!(r.guest.received(1).is_empty());
    assert_eq!(num(r.peer.stats(), "inputs_not_applied"), 1);
}

fn gpio_in(t: u64, levels: &[(u8, u8)]) -> String {
    let levels: Vec<String> = levels.iter().map(|(p, l)| format!("[{p},{l}]")).collect();
    format!(
        r#"{{"type":"gpio_in","t":{t},"levels":[{}]}}"#,
        levels.join(",")
    )
}

fn release(t: u64, pins: &[u8]) -> String {
    let pins: Vec<String> = pins.iter().map(u8::to_string).collect();
    format!(
        r#"{{"type":"release","t":{t},"pins":[{}]}}"#,
        pins.join(",")
    )
}

fn pins_of(msg: &Json, key: &str) -> Vec<u64> {
    msg.get(key)
        .and_then(Json::as_arr)
        .unwrap()
        .iter()
        .map(|p| p.as_i64().unwrap() as u64)
        .collect()
}

/// A `gpio_out`'s `t`, `changes`, `enabled` and `output`.
type GpioOut = (u64, Vec<(u64, u64)>, Vec<u64>, Vec<u64>);

fn changes_of(msg: &Json) -> Vec<(u64, u64)> {
    msg.get("changes")
        .and_then(Json::as_arr)
        .unwrap()
        .iter()
        .map(|c| {
            let c = c.as_arr().unwrap();
            (c[0].as_i64().unwrap() as u64, c[1].as_i64().unwrap() as u64)
        })
        .collect()
}

#[test]
fn gpio_out_reports_watched_pins_with_their_enable_and_latch_as_an_event() {
    let latency = 100_000;
    let guest = FakeGuest::new(vec![
        // an unwatched pin alone: nothing on the socket
        (cycles(MS), Act::Drive(1 << 2, 1 << 2)),
        (
            cycles(2 * MS),
            Act::Drive(1 << 2 | 1 << PWRKEY, 1 << 2 | 1 << PWRKEY),
        ),
        (cycles(3 * MS), Act::Drive(1 << PWRKEY, 0)),
        // the line released: an enable-only change, with no level change
        (cycles(4 * MS), Act::Drive(0, 0)),
    ]);
    let r = run(
        0.01,
        guest,
        FakePeer::new(&[PWRKEY], idle(None, Some(latency))),
    );
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    let out = r.peer.of("gpio_out");
    let seen: Vec<GpioOut> = out
        .iter()
        .map(|m| {
            (
                num(m, "t"),
                changes_of(m),
                pins_of(m, "enabled"),
                pins_of(m, "output"),
            )
        })
        .collect();
    let pwrkey = u64::from(PWRKEY);
    assert_eq!(
        seen,
        [
            (
                ns(cycles(2 * MS)),
                vec![(pwrkey, 1)],
                vec![pwrkey],
                vec![pwrkey]
            ),
            (ns(cycles(3 * MS)), vec![(pwrkey, 0)], vec![pwrkey], vec![]),
            (ns(cycles(4 * MS)), vec![], vec![], vec![]),
        ]
    );
    // each is an event: the peer gets a turn at t + latency to answer it
    let syncs: Vec<u64> = r.peer.of("sync").iter().map(|m| num(m, "t")).collect();
    for (t, ..) in &seen {
        assert!(syncs.contains(&(t + latency)), "{syncs:?}");
    }
}

#[test]
fn gpio_in_and_release_reach_the_chip_through_the_board_at_their_time() {
    let mut turns = 0;
    let peer = FakePeer::new(&[RI], move |turn| {
        turns += 1;
        match turns {
            // initial level, in place at the first instruction, then a pulse queued ahead
            1 => vec![
                gpio_in(0, &[(RI, 1)]),
                release(3 * MS, &[RI]),
                gpio_in(MS + 500_000, &[(RI, 0)]),
                wait(turn.seq, Some(4 * MS), None),
            ],
            // a release and a level at the same t: applied in the order they came
            2 => vec![
                release(5 * MS, &[RI]),
                gpio_in(5 * MS, &[(RI, 1)]),
                // past the end of the run: never applied
                gpio_in(20 * MS, &[(RI, 0)]),
                wait(turn.seq, None, None),
            ],
            _ => vec![wait(turn.seq, None, None)],
        }
    });
    let r = run(0.01, FakeGuest::new(vec![]), peer);
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    assert_eq!(
        r.guest.inputs,
        [
            (0, RI, Some(true)),
            (cycles(MS + 500_000), RI, Some(false)),
            (cycles(3 * MS), RI, None),
            (cycles(5 * MS), RI, Some(true)),
        ]
    );
    // before the guest's first instruction
    assert!(r.guest.slices[0] > 0);
    // queued inputs end slices at their own time
    for t in [MS + 500_000, 3 * MS, 5 * MS] {
        assert!(r.guest.slices.contains(&cycles(t)), "{t}");
    }
    // what the peer drives, for the chip to take again after a reset
    assert_eq!(r.guest.board.input_levels(), [(RI, true)]);
    assert_eq!(r.stats.inputs_not_applied, 1);
    assert!(r.peer.of("warn").is_empty());
    assert_eq!(num(r.peer.stats(), "inputs_on_output"), 0);
}

#[test]
fn gpio_in_on_a_pin_the_guest_drives_is_applied_with_a_warning_each_time() {
    let guest = FakeGuest::new(vec![(cycles(MS / 2), Act::Drive(1 << RI, 0))]);
    let mut first = true;
    let peer = FakePeer::new(&[RI], move |turn| {
        let mut out = Vec::new();
        if std::mem::take(&mut first) {
            out.push(gpio_in(MS, &[(RI, 1)]));
            out.push(gpio_in(MS, &[(RI, 0)]));
        }
        out.push(wait(turn.seq, None, None));
        out
    });
    let r = run(0.01, guest, peer);
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    assert_eq!(
        r.guest.inputs,
        [(cycles(MS), RI, Some(true)), (cycles(MS), RI, Some(false))]
    );
    let warns = r.peer.of("warn");
    assert_eq!(warns.len(), 2, "one per message, never merged");
    for w in &warns {
        assert_eq!(w.str_or("kind", ""), "input_on_output");
        assert_eq!(num(w, "pin"), u64::from(RI));
        assert_eq!(num(w, "t"), ns(cycles(MS)));
        assert!(w.get("bytes").is_none() && w.get("port").is_none());
    }
    assert_eq!(r.stats.inputs_on_output, 2);
    assert_eq!(num(r.peer.stats(), "inputs_on_output"), 2);
}

#[test]
fn after_a_reset_gpio_out_reports_the_pins_as_read_from_the_chip() {
    let at = cycles(2 * MS) + 5;
    let guest = FakeGuest::new(vec![
        (cycles(MS), Act::Drive(1 << PWRKEY, 1 << PWRKEY)),
        (at, Act::Reset(12)),
    ]);
    let mut first = true;
    let peer = FakePeer::new(&[PWRKEY, RI], move |turn| {
        let mut out = Vec::new();
        if std::mem::take(&mut first) {
            out.push(gpio_in(0, &[(RI, 1)]));
        }
        out.push(wait(turn.seq, None, None));
        out
    });
    let r = run(0.01, guest, peer);
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    let types = r.peer.types();
    let reset = types.iter().position(|&k| k == "reset").unwrap();
    assert_eq!(types[reset + 1], "gpio_out", "{types:?}");
    let after = &r.peer.msgs[reset + 1];
    assert_eq!(num(after, "t"), ns(at));
    assert_eq!(changes_of(after), [(u64::from(PWRKEY), 0)]);
    assert!(pins_of(after, "enabled").is_empty() && pins_of(after, "output").is_empty());
    // the board persists: what the peer drives stays driven
    assert_eq!(r.guest.board.input_levels(), [(RI, true)]);
    assert_eq!(r.peer.of("gpio_out").len(), 2);
}

#[test]
fn a_reset_with_nothing_watched_sends_no_gpio_out() {
    let guest = FakeGuest::new(vec![(cycles(MS), Act::Reset(12))]);
    let r = run(0.01, guest, FakePeer::new(&[], idle(None, None)));
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    assert_eq!(r.peer.of("reset").len(), 1);
    assert!(r.peer.of("gpio_out").is_empty());
}

/// On the emulator itself: a loop in IRAM copies GPIO 33, which the peer drives, to GPIO 4. The
/// levels go through the board into the chip at their time, and the guest's answer comes back as
/// `gpio_out` within a few instructions.
#[test]
fn on_the_emulator_gpio_in_reaches_the_guest_and_its_output_comes_back_as_gpio_out() {
    use emu_core::Bus;
    use esp_soc::Soc;
    // assembled with xtensa-esp32-elf-as --no-transform from:
    //   movi a2, 0x3ff; slli a2, a2, 20; movi a3, 0x44; slli a3, a3, 12; add a2, a2, a3
    //   movi a6, 0x10                                     (GPIO at 0x3ff44000; bit 4)
    //   1: l32i a4, a2, 0x40; extui a5, a4, 1, 1; beqz a5, 2f   (GPIO_IN1, bit 1: GPIO 33)
    //   s32i a6, a2, 0x08; j 1b                           (GPIO_OUT_W1TS)
    //   2: s32i a6, a2, 0x0c; j 1b                        (GPIO_OUT_W1TC)
    const PROGRAM: [u8; 39] = [
        0x22, 0xa3, 0xff, 0xc0, 0x22, 0x01, 0x32, 0xa0, 0x44, 0x40, 0x33, 0x11, 0x30, 0x22, 0x80,
        0x62, 0xa0, 0x10, 0x42, 0x22, 0x10, 0x40, 0x51, 0x04, 0x16, 0x55, 0x00, 0x62, 0x62, 0x02,
        0x06, 0xfc, 0xff, 0x62, 0x62, 0x03, 0x86, 0xfa, 0xff,
    ];
    const ENTRY: u32 = 0x4008_0000;
    const OUT: u8 = 4;

    let shared = Shared::default();
    let mut m = esp32::machine([0x24, 0x6f, 0x28, 0x00, 0x11, 0x22], 4 << 20);
    m.bus.board = Box::new(SocketBoard::new(shared.clone(), None));
    m.bus.attach_board_devices();
    for (addr, value) in [
        (0x3ff4_9020, 2 << 12 | 1 << 9), // IO_MUX GPIO33: GPIO function, input enabled
        (0x3ff4_9048, 2 << 12),          // IO_MUX GPIO4: GPIO function
        (0x3ff4_4024, 1 << OUT),         // GPIO_ENABLE_W1TS
    ] {
        m.bus.write32(addr, value).unwrap();
    }
    m.bus.load_bytes(ENTRY, &PROGRAM).unwrap();
    <esp32::Esp32 as Soc>::boot_core(&mut m.cores[0], ENTRY);

    let mut first = true;
    let peer = FakePeer::new(&[OUT, RI], move |turn| {
        let mut out = Vec::new();
        if std::mem::take(&mut first) {
            // an undriven pad reads 1: the level at t = 0 is in place at the first instruction,
            // so the guest never sets GPIO 4 before the pulse
            out.push(gpio_in(0, &[(RI, 0)]));
            out.push(gpio_in(MS, &[(RI, 1)]));
            out.push(gpio_in(2 * MS, &[(RI, 0)]));
        }
        out.push(wait(turn.seq, None, None));
        out
    });
    let mut peer = peer;
    let signal = AtomicBool::new(false);
    let opts = Options {
        end_cycle: cycles(3 * MS),
        console: false,
        usb_console: false,
        uart0_stream: false,
        peer_timeout: None,
        signal: &signal,
        parent_gone: None,
        wifi: None,
        rom: &[],
        card: None,
        realtime: false,
        flash_out: None,
        hci: None,
        no_bt: false,
    };
    let (end, stats) = Session::new(&mut m, &mut peer, shared, opts).run();
    assert!(matches!(end, End::MaxSeconds), "{end:?}");
    let seen: Vec<(u64, Vec<(u64, u64)>)> = peer
        .of("gpio_out")
        .iter()
        .map(|m| (num(m, "t"), changes_of(m)))
        .collect();
    assert_eq!(seen.len(), 2, "{seen:?}");
    let out = u64::from(OUT);
    for ((t, changes), (at, level)) in seen.iter().zip([(MS, 1), (2 * MS, 0)]) {
        assert_eq!(changes, &[(out, level)]);
        assert!((at..at + 1000).contains(t), "{t} for an input at {at}");
    }
    assert_eq!(stats.inputs_not_applied, 0);
    assert!(peer.of("warn").is_empty());
}

#[test]
fn after_a_reset_gpio_out_changes_what_the_peer_last_saw_of_each_pad() {
    let at = cycles(2 * MS);
    let guest = FakeGuest::new(vec![
        // UART1 TX idles high on its pad, with GPIO_OUT at 0; the power key's pad is low under a
        // peripheral signal, with GPIO_ENABLE and GPIO_OUT both 1
        (
            cycles(MS),
            Act::Pads(
                vec![(MODEM_TX, true), (PWRKEY, false)],
                1 << PWRKEY,
                1 << PWRKEY,
            ),
        ),
        (at, Act::Reset(12)),
    ]);
    let r = run(
        0.01,
        guest,
        FakePeer::new(&[MODEM_TX, PWRKEY], idle(None, None)),
    );
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    let out = r.peer.of("gpio_out");
    assert_eq!(out.len(), 2);
    assert_eq!(
        changes_of(out[0]),
        [(u64::from(MODEM_TX), 1), (u64::from(PWRKEY), 0)]
    );
    // the pad the peer saw high goes low; the one it saw low stays unmentioned
    assert_eq!(num(out[1], "t"), ns(at));
    assert_eq!(changes_of(out[1]), [(u64::from(MODEM_TX), 0)]);
    assert!(pins_of(out[1], "enabled").is_empty() && pins_of(out[1], "output").is_empty());
}

/// On the emulator: what `reset_gpio` relies on. Before the reset GPIO 4 is driven high as a
/// plain GPIO and GPIO 26 by UART1's TX signal; after `reboot()` no pad is driven at all, through
/// the GPIO matrix or an IO_MUX function, and `GPIO_ENABLE` and `GPIO_OUT` are 0.
#[test]
fn on_the_emulator_no_pad_is_driven_after_a_chip_reset() {
    use emu_core::Bus;
    let mut m = esp32::machine([0x24, 0x6f, 0x28, 0x00, 0x11, 0x22], 4 << 20);
    for (addr, value) in [
        (0x3ff4_9048, 2 << 12),               // IO_MUX GPIO4: GPIO function
        (0x3ff4_9028, 2 << 12),               // IO_MUX GPIO26
        (0x3ff4_4530 + 4 * 26, 17 | 1 << 10), // GPIO_FUNC26_OUT_SEL_CFG: U1TXD_OUT, OE from GPIO_ENABLE
        (0x3ff4_4024, 1 << 4 | 1 << 26),      // GPIO_ENABLE_W1TS
        (0x3ff4_4008, 1 << 4),                // GPIO_OUT_W1TS
    ] {
        m.bus.write32(addr, value).unwrap();
    }
    // every pad any output signal, plain GPIO (256) included, drives, inverted or not
    let driven = |m: &esp32::Machine| {
        (0..=256)
            .flat_map(|signal| [signal, signal | 1 << 9])
            .fold(0, |mask, signal| {
                mask | m.bus.periph.gpio.output_pins(signal)
            })
    };
    assert_eq!(driven(&m), 1 << 4 | 1 << 26);
    assert_eq!(Guest::gpio_drive(&m), (1 << 4 | 1 << 26, 1 << 4));
    Guest::chip_reset(&mut m);
    assert_eq!(driven(&m), 0);
    assert_eq!(Guest::gpio_drive(&m), (0, 0));
}

/// A machine whose core 0 waits in `waiti 0` (`2: waiti 0; j 2b`) and, with `alarm`, a chip in
/// deep sleep with its RTC timer armed for that many slow ticks from power-on, as ESP-IDF arms it
/// (esp32sim's `esp32/src/sleep.rs` tests); the first millisecond parks the cores.
fn waiting_machine(alarm: Option<u64>) -> esp32::Machine {
    use emu_core::Bus;
    use esp_soc::Soc;
    const WAIT: [u8; 6] = [0x00, 0x70, 0x00, 0x46, 0xfe, 0xff];
    const ENTRY: u32 = 0x4008_0000;
    let mut m = esp32::machine([0x24, 0x6f, 0x28, 0x00, 0x11, 0x22], 4 << 20);
    m.bus.load_bytes(ENTRY, &WAIT).unwrap();
    <esp32::Esp32 as Soc>::boot_core(&mut m.cores[0], ENTRY);
    if let Some(target) = alarm {
        for (addr, value) in [
            (0x3ff4_8004, target as u32),         // RTC_CNTL_SLP_TIMER0_REG
            (0x3ff4_8008, (target >> 32) as u32), // RTC_CNTL_SLP_TIMER1_REG
            (0x3ff4_8038, 1 << (11 + 3)),         // WAKEUP_ENA: RTC_TIMER_TRIG_EN
            (0x3ff4_8084, 1 << 31),               // DIG_PWC: DG_WRAP_PD_EN
            (0x3ff4_8018, 1 << 31),               // STATE0: SLEEP_EN
        ] {
            m.bus.write32(addr, value).unwrap();
        }
    }
    assert!(matches!(Guest::run_until(&mut m, MS_CYCLES), Stop::Halted));
    m
}

/// On the emulator: `sleep_until` stops short of the RTC alarm, and the wake that esp32sim's own
/// rounds bring after it comes, as it does without the skip, at the end of the first round at or
/// after the alarm's slow tick: the two runs end their rounds at other cycles, so the wakes are
/// at most a round apart.
#[test]
fn on_the_emulator_a_skipped_sleep_wakes_at_the_tick_of_its_alarm() {
    // 20 s of the 150 kHz RTC slow clock: more than one step of `sleep_until`
    let alarm = 3_000_000;
    assert!(alarm * rtc::SLOW_TICK > 2 * SLEEP_STEP);
    let mut through = waiting_machine(Some(alarm));
    assert!(through.sleeps_through());
    assert!(matches!(
        Guest::run_until(&mut through, 21 * SECOND),
        Stop::SwReset
    ));
    let woke = through.bus.cycles;
    assert_eq!(through.bus.periph.rtc.0.reset_cause, 5);
    // esp32sim's idle round, 512 CPU cycles, in bus cycles at the 40 MHz the CPU has from reset
    let round = 512 * 6;
    let due = alarm * rtc::SLOW_TICK;
    assert!((due..due + round).contains(&woke), "{woke}");

    let mut skipped = waiting_machine(Some(alarm));
    skipped.sleep_until(21 * SECOND);
    let short = woke - skipped.bus.cycles;
    assert!(
        short > 0 && short <= (WAKE_MARGIN_TICKS + 1) * rtc::SLOW_TICK,
        "{short}"
    );
    assert!(skipped.sleeps_through());
    assert!(matches!(
        Guest::run_until(&mut skipped, 21 * SECOND),
        Stop::SwReset
    ));
    assert!(
        (due..due + round).contains(&skipped.bus.cycles),
        "{}",
        skipped.bus.cycles
    );
}

/// On the emulator: an awake chip, and one asleep with its RTC watchdog on, are not skipped; one
/// asleep without an alarm is, to the end it is given.
#[test]
fn on_the_emulator_only_a_deep_sleep_is_skipped() {
    use emu_core::Bus;
    let mut awake = waiting_machine(None);
    assert!(!awake.sleeps_through());
    awake.sleep_until(SECOND);
    assert_eq!(awake.bus.cycles, MS_CYCLES);

    let mut watched = waiting_machine(Some(300_000));
    watched.bus.write32(0x3ff4_80a4, 0x50d8_3aa1).unwrap(); // WDTWPROTECT: unlocked
    watched.bus.write32(0x3ff4_808c, 1 << 31).unwrap(); // WDTCONFIG0: enabled
    assert!(!watched.sleeps_through());
    watched.sleep_until(SECOND);
    assert_eq!(watched.bus.cycles, MS_CYCLES);

    // a target never written wakes nothing (`sleep_ticks_left`)
    let mut forever = waiting_machine(Some(0));
    assert!(forever.sleeps_through());
    forever.sleep_until(60 * SECOND);
    assert!(forever.bus.cycles >= 60 * SECOND && forever.bus.cycles < 60 * SECOND + 16);
    assert!(forever.sleeps_through());
}

/// A guest with a Wi-Fi station: it sends the scripted Ethernet frames at their cycles into the
/// relay, and its link takes at most `room` frames between two slices, as esp32sim's queue of 64
/// does, handing one to the station per slice.
struct WifiGuest {
    station_states: Vec<(u64, bool, u64)>,
    inner: FakeGuest,
    sends: VecDeque<(u64, Vec<u8>)>,
    relay: Vec<Vec<u8>>,
    room: usize,
    queued: usize,
    /// What the link took, with the cycle.
    taken: Vec<(u64, Vec<u8>)>,
    /// The access point's switches, with the cycle: `true` on the air, `false` once it has
    /// left.
    switches: Vec<(u64, bool)>,
    /// How many times the access point is asked to leave before it has.
    leave_after: usize,
    asked_to_leave: usize,
}

impl WifiGuest {
    fn new(sends: Vec<(u64, Vec<u8>)>, room: usize) -> Self {
        Self {
            inner: FakeGuest::new(Vec::new()),
            station_states: Vec::new(),
            sends: sends.into(),
            relay: Vec::new(),
            room,
            queued: 0,
            taken: Vec::new(),
            switches: Vec::new(),
            leave_after: 1,
            asked_to_leave: 0,
        }
    }
}

impl Guest for WifiGuest {
    fn cycles(&self) -> u64 {
        self.inner.cycles()
    }
    fn run_until(&mut self, cycle: u64) -> Stop {
        let stop = self.inner.run_until(cycle);
        if self.station_state() == Some((true, 2)) {
            self.room = 64;
        }
        while self
            .sends
            .front()
            .is_some_and(|(at, _)| *at < self.inner.now)
        {
            let (_, frame) = self.sends.pop_front().unwrap();
            self.relay.push(frame);
        }
        self.queued = self.queued.saturating_sub(1);
        stop
    }
    fn chip_reset(&mut self) -> u32 {
        self.inner.chip_reset()
    }
    fn power_on_reset(&mut self, rom: &[u8]) -> u32 {
        self.inner.power_on_reset(rom)
    }
    fn idle_until(&mut self, cycle: u64) {
        self.inner.idle_until(cycle)
    }
    fn uart_route(&self, port: usize) -> UartRoute {
        self.inner.uart_route(port)
    }
    fn rx_pending(&self, port: usize) -> usize {
        self.inner.rx_pending(port)
    }
    fn uart_receive(&mut self, port: usize, byte: u8) {
        self.inner.uart_receive(port, byte)
    }
    fn uart_frame_error(&mut self, port: usize) {
        self.inner.uart_frame_error(port)
    }
    fn gpio_drive(&self) -> (u64, u64) {
        self.inner.gpio_drive()
    }
    fn take_gpio_inputs(&mut self) {
        self.inner.take_gpio_inputs()
    }
    fn station_state(&self) -> Option<(bool, u64)> {
        self.station_states
            .iter()
            .rev()
            .find(|s| s.0 <= self.inner.now)
            .map(|s| (s.1, s.2))
    }
    fn clear_station_relay(&mut self) {
        self.relay.clear();
        self.queued = 0;
    }
    fn take_eth_frames(&mut self) -> Vec<Vec<u8>> {
        std::mem::take(&mut self.relay)
    }
    fn eth_input(&mut self, frame: &[u8]) -> bool {
        if self.queued >= self.room {
            return false;
        }
        self.queued += 1;
        self.taken.push((self.inner.now, frame.to_vec()));
        true
    }
    fn wifi_access_point(&mut self, _wifi: &cli::Wifi) {
        self.switches.push((self.inner.now, true));
        self.asked_to_leave = 0;
    }
    fn wifi_leave(&mut self) -> bool {
        self.asked_to_leave += 1;
        if self.asked_to_leave < self.leave_after {
            return false;
        }
        self.switches.push((self.inner.now, false));
        self.queued = 0;
        true
    }
    fn wifi_drops(&self) -> (u64, u64) {
        (2, 1)
    }
    fn wifi_forget(&mut self) {
        self.switches.push((self.inner.now, false));
        self.queued = 0;
    }
}

fn eth_rx(t: u64, frame: &[u8]) -> String {
    format!(
        r#"{{"type":"eth_rx","t":{t},"data":"{}"}}"#,
        protocol::base64(frame)
    )
}

fn wifi_ap(t: u64, on: bool) -> String {
    format!(r#"{{"type":"wifi_ap","t":{t},"on":{on}}}"#)
}

/// An Ethernet frame of the minimum length, told apart by its first byte.
fn frame(n: u8) -> Vec<u8> {
    let mut f = vec![0; protocol::ETH_MIN];
    f[0] = n;
    f
}

fn run_wifi(end: u64, guest: &mut WifiGuest, peer: &mut FakePeer) -> (End, Stats) {
    let wifi = cli::Wifi::AccessPoint {
        spec: "ssid=test".into(),
        nearby: Vec::new(),
    };
    run_wifi_mode(end, guest, peer, &wifi)
}

fn run_wifi_mode(
    end: u64,
    guest: &mut WifiGuest,
    peer: &mut FakePeer,
    wifi: &cli::Wifi,
) -> (End, Stats) {
    let signal = AtomicBool::new(false);
    let opts = Options {
        end_cycle: end,
        console: false,
        usb_console: false,
        uart0_stream: false,
        peer_timeout: None,
        signal: &signal,
        parent_gone: None,
        wifi: Some(wifi),
        rom: &[],
        card: None,
        realtime: false,
        flash_out: None,
        hci: None,
        no_bt: false,
    };
    let shared = guest.inner.shared.clone();
    Session::new(guest, peer, shared, opts).run()
}

#[test]
fn the_station_frames_go_out_as_eth_tx_events_stamped_at_the_slice_boundary() {
    let latency = 100_000;
    let mut guest = WifiGuest::new(
        vec![(cycles(250_000), frame(1)), (cycles(260_000), frame(2))],
        64,
    );
    let mut answered = false;
    let mut peer = FakePeer::new(&[], move |turn| {
        let mut out = Vec::new();
        if !answered && turn.msgs.iter().any(|m| kind(m) == "eth_tx") {
            answered = true;
            out.push(eth_rx(turn.t, &frame(9)));
        }
        out.push(wait(turn.seq, None, Some(latency)));
        out
    });
    let (end, stats) = run_wifi(cycles(MS), &mut guest, &mut peer);
    assert!(matches!(end, End::MaxSeconds), "{end:?}");
    let sent = peer.of("eth_tx");
    assert_eq!(sent.len(), 2);
    // with no event unacknowledged a slice lasts at most the latency: taken at 300 µs
    for (msg, n) in sent.iter().zip([1, 2]) {
        assert_eq!(num(msg, "t"), ns(cycles(300_000)));
        assert_eq!(data(msg), frame(n));
    }
    assert_eq!(num(sent[1], "seq"), num(sent[0], "seq") + 1);
    // the turn at t + latency, and the answer taken there
    assert_eq!(guest.taken, [(cycles(400_000), frame(9))]);
    assert_eq!(
        stats.wifi,
        Some(WifiStats {
            eth_tx: 2,
            eth_rx: 1,
            eth_rx_dropped: 0,
            rx_dropped: 2,
            eth_tx_dropped: 1,
        })
    );
    assert!(
        peer.stats()
            .get("wifi")
            .is_some_and(|w| num(w, "eth_tx") == 2 && num(w, "rx_dropped") == 2)
    );
}

#[test]
fn eth_rx_waits_for_room_in_the_link_and_goes_off_the_air_with_the_access_point() {
    let mut first = true;
    let mut peer = FakePeer::new(&[], move |turn| {
        let mut out = Vec::new();
        if std::mem::take(&mut first) {
            out.extend([1, 2, 3].map(|n| eth_rx(MS, &frame(n))));
            out.push(wifi_ap(3 * MS, false));
            out.push(eth_rx(4 * MS, &frame(4)));
            out.push(wifi_ap(5 * MS, true));
            out.push(wifi_ap(5 * MS, true));
            out.push(eth_rx(5 * MS, &frame(5)));
        }
        out.push(wait(turn.seq, None, None));
        out
    });
    let mut guest = WifiGuest::new(Vec::new(), 2);
    // the station has joined: the access point leaves at the next slice boundary
    guest.leave_after = 2;
    let (end, stats) = run_wifi(cycles(6 * MS), &mut guest, &mut peer);
    assert!(matches!(end, End::MaxSeconds), "{end:?}");
    // two fit at 1 ms; the third waits for the slice after, which the link drains
    assert_eq!(
        guest.taken,
        [
            (cycles(MS), frame(1)),
            (cycles(MS), frame(2)),
            (cycles(2 * MS), frame(3)),
            (cycles(5 * MS), frame(5)),
        ]
    );
    // asked to leave at 3 ms, gone by the boundary after; the second "on" is no switch
    assert_eq!(
        guest.switches,
        [(cycles(4 * MS), false), (cycles(5 * MS), true)]
    );
    let wifi = stats.wifi.unwrap();
    assert_eq!((wifi.eth_rx, wifi.eth_rx_dropped), (4, 1));
    // counted as the access point left, and at the end: a fresh one starts from none
    assert_eq!(wifi.rx_dropped, 4);
}

#[test]
fn eth_rx_and_wifi_ap_without_wifi_are_protocol_errors() {
    for line in [eth_rx(0, &frame(1)), wifi_ap(0, false)] {
        let peer = FakePeer::new(&[], move |turn| {
            vec![line.clone(), wait(turn.seq, None, None)]
        });
        let r = run(1.0, FakeGuest::new(Vec::new()), peer);
        assert!(
            matches!(&r.end, End::Protocol(m) if m.contains("without --wifi")),
            "{:?}",
            r.end
        );
        assert!(r.peer.stats().get("wifi").is_none());
    }
}

/// On the emulator itself: the access point that leaves deauthenticates a joined station, waits
/// until the frame is off its queue, and only then leaves the air; one with no station leaves at
/// once, and a fresh one comes back joinable.
#[test]
fn on_the_emulator_the_access_point_deauthenticates_the_station_before_it_leaves() {
    let mut m = esp32::machine([0x24, 0x6f, 0x28, 0x00, 0x11, 0x22], 4 << 20);
    let wifi = cli::Wifi::AccessPoint {
        spec: "ssid=home,bssid=02:00:00:00:00:01".into(),
        nearby: Vec::new(),
    };
    m.wifi_access_point(&wifi);
    assert!(m.wifi_leave(), "nobody joined: gone at once");
    assert!(m.bus.periph.wifi.link.ap().is_none());

    m.wifi_access_point(&wifi);
    let sta = [0x24, 0x6f, 0x28, 0x00, 0x11, 0x22];
    let ap = m.bus.periph.wifi.link.ap_mut().unwrap();
    ap.sta = sta;
    ap.state = StaState::Associated;
    assert!(!m.wifi_leave());
    let ap = m.bus.periph.wifi.link.ap_mut().unwrap();
    assert_eq!(ap.state, StaState::Idle);
    assert_eq!(ap.next_beacon_us, u64::MAX, "no more beacons");
    let deauth = &ap.queue.last().unwrap().frame;
    assert_eq!(&deauth[..2], &[0xc0, 0]);
    assert_eq!(&deauth[4..10], &sta);
    assert_eq!(&deauth[10..16], &[2, 0, 0, 0, 0, 1]);
    // after the sequence control, reason 3: the access point is leaving
    assert_eq!(&deauth[24..], &[3, 0]);
    assert!(!m.wifi_leave(), "the station has not had it yet");
    m.bus.periph.wifi.link.ap_mut().unwrap().queue.clear();
    assert!(m.wifi_leave());
    assert!(m.bus.periph.wifi.link.ap().is_none());
    m.wifi_access_point(&wifi);
    let ap = m.bus.periph.wifi.link.ap().unwrap();
    assert_eq!((ap.cfg.ssid.as_str(), ap.state), ("home", StaState::Idle));
}

#[test]
fn an_access_point_put_back_while_it_is_leaving_comes_back_once_it_has_left() {
    let mut first = true;
    let mut peer = FakePeer::new(&[], move |turn| {
        let mut out = Vec::new();
        if std::mem::take(&mut first) {
            // off and on again at one instant, then a frame while it is leaving
            out.push(wifi_ap(MS, false));
            out.push(wifi_ap(MS, true));
            out.push(eth_rx(MS, &frame(1)));
            out.push(eth_rx(3 * MS, &frame(2)));
        }
        out.push(wait(turn.seq, None, None));
        out
    });
    let mut guest = WifiGuest::new(Vec::new(), 64);
    // a station has joined: the deauthentication takes a slice to reach it
    guest.leave_after = 2;
    let (end, stats) = run_wifi(cycles(4 * MS), &mut guest, &mut peer);
    assert!(matches!(end, End::MaxSeconds), "{end:?}");
    assert_eq!(
        guest.switches,
        [(cycles(2 * MS), false), (cycles(2 * MS), true)],
        "the fresh one only once the old one has left"
    );
    assert_eq!(guest.taken, [(cycles(3 * MS), frame(2))]);
    let wifi = stats.wifi.unwrap();
    assert_eq!((wifi.eth_rx, wifi.eth_rx_dropped), (1, 1));
}

fn power(t: u64, on: bool) -> String {
    format!(r#"{{"type":"power","t":{t},"on":{on}}}"#)
}

fn chip_reset(t: u64) -> String {
    format!(r#"{{"type":"chip_reset","t":{t}}}"#)
}

/// A peer that sends `script`'s messages, each in the first turn at or after the time it names,
/// and a turn at each of those times.
fn scripted(script: Vec<(u64, String)>) -> impl FnMut(&Turn) -> Vec<String> {
    let mut script: VecDeque<(u64, String)> = script.into();
    move |turn| {
        let mut out = Vec::new();
        while script.front().is_some_and(|(at, _)| *at <= turn.t) {
            out.push(script.pop_front().unwrap().1);
        }
        let next = script.front().map(|(at, _)| *at);
        out.push(wait(turn.seq, next, None));
        out
    }
}

#[test]
fn with_the_supply_off_the_guest_runs_nothing_and_its_time_goes_on() {
    let guest = FakeGuest::new(vec![(10, Act::Hint(0x8004_0004))]).with_routes(&[modem(115_200)]);
    let peer = FakePeer::new(
        &[MODEM_TX, MODEM_RX],
        scripted(vec![
            (MS, power(MS, false)),
            // bytes the chip cannot hear: no UART is routed without power
            (2 * MS, uart_rx(2 * MS, MODEM_RX, 115_200, b"R")),
            (40 * MS, power(40 * MS, true)),
        ]),
    );
    let r = run(0.05, guest, peer);
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    let (off, on) = (cycles(MS), cycles(40 * MS));
    assert!(
        r.guest.slices.iter().all(|&end| end <= off || end > on),
        "the guest ran with the supply off: {:?}",
        r.guest.slices
    );
    assert!(r.guest.idles.iter().all(|&end| end > off && end <= on));
    // one slice to each turn and to the paced byte, not one per millisecond
    assert!(r.guest.idles.len() <= 4, "{:?}", r.guest.idles);
    assert!(r.guest.fifo[1].is_empty());
    assert_eq!(r.stats.unrouted_rx_bytes, 1);
    // the routes go with the power, and the chip comes back through a power-on reset
    let cfg = r.peer.of("uart_cfg");
    assert_eq!(cfg.len(), 2, "{cfg:?}");
    assert_eq!(num(cfg[1], "t"), MS);
    assert_eq!(cfg[1].get("rx_pin"), Some(&Json::Null));
    let reset = r.peer.of("reset");
    assert_eq!(reset.len(), 1);
    assert_eq!((num(reset[0], "t"), num(reset[0], "cause")), (40 * MS, 1));
    // the power-on clears the RTC registers: no hint is left for the new boot
    assert_eq!(num(reset[0], "hint"), 0);
    assert_eq!(r.guest.power_resets, [on]);
    assert_eq!(r.stats.resets, 1);
}

/// What `board.wait_for_wake` asks for: a turn at the end of its time, and one a millisecond after
/// each event.
fn waking(until: u64) -> impl FnMut(&Turn) -> Vec<String> {
    move |turn| vec![wait(turn.seq, Some(until), Some(MS))]
}

#[test]
fn a_deep_sleep_is_one_slice_to_its_wake() {
    let wake = 10 * SECOND;
    let guest = FakeGuest::new(vec![(wake, Act::Reset(5))]).asleep_between(MS_CYCLES, wake);
    let r = run(12.0, guest, FakePeer::new(&[], waking(20_000 * MS)));
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    // neither the next millisecond nor the latency ends a slice in the sleep
    assert_eq!(r.guest.skips, [(MS_CYCLES, wake - 1)]);
    let asleep = r
        .guest
        .slices
        .iter()
        .filter(|&&end| end > MS_CYCLES && end <= wake)
        .count();
    assert_eq!(asleep, 0, "{:?}", r.guest.slices);
    // the wake still comes at its cycle, and the turn a latency after it
    let reset = r.peer.of("reset");
    assert_eq!(reset.len(), 1);
    assert_eq!((num(reset[0], "t"), num(reset[0], "cause")), (ns(wake), 5));
    let syncs: Vec<u64> = r.peer.of("sync").iter().map(|m| num(m, "t")).collect();
    assert!(syncs.contains(&(ns(wake) + MS)), "{syncs:?}");
}

#[test]
fn a_deep_sleep_the_run_loop_cannot_step_through_keeps_its_slices() {
    let wake = 10 * MS_CYCLES;
    let guest = FakeGuest::new(vec![(wake, Act::Reset(5))])
        .asleep_between(MS_CYCLES, wake)
        .not_steppable();
    let r = run(0.02, guest, FakePeer::new(&[], waking(20 * MS)));
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    assert!(r.guest.skips.is_empty(), "{:?}", r.guest.skips);
    // a slice to each millisecond of the sleep, as awake
    for t in 2..10 {
        assert!(
            r.guest.slices.contains(&cycles(t * MS)),
            "{:?}",
            r.guest.slices
        );
    }
    let reset = r.peer.of("reset");
    assert_eq!(num(reset[0], "t"), ns(wake));
}

#[test]
fn a_deep_sleep_without_a_wake_goes_on_to_the_block_point() {
    let guest = FakeGuest::new(vec![]).asleep_between(MS_CYCLES, 60 * SECOND);
    // a turn every 5 s, as a wait that never sees the wake it waits for
    let r = run(
        10.0,
        guest,
        FakePeer::new(&[], idle(Some(5_000 * MS), Some(MS))),
    );
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    assert_eq!(
        r.guest.skips,
        [(MS_CYCLES, 5 * SECOND), (5 * SECOND, 10 * SECOND)]
    );
    let syncs: Vec<u64> = r.peer.of("sync").iter().map(|m| num(m, "t")).collect();
    assert!(syncs.contains(&(5_000 * MS)), "{syncs:?}");
}

#[test]
fn en_resets_the_chip_with_power_on_as_its_cause_and_does_nothing_without_power() {
    let guest = FakeGuest::new(vec![]);
    let peer = FakePeer::new(
        &[],
        scripted(vec![
            (MS, chip_reset(MS)),
            (2 * MS, power(2 * MS, false)),
            (3 * MS, chip_reset(3 * MS)),
            // a supply that is off already stays off
            (4 * MS, power(4 * MS, false)),
            (5 * MS, power(5 * MS, true)),
            (6 * MS, power(6 * MS, true)),
        ]),
    );
    let r = run(0.01, guest, peer);
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    let resets: Vec<(u64, u64)> = r
        .peer
        .of("reset")
        .iter()
        .map(|m| (num(m, "t"), num(m, "cause")))
        .collect();
    assert_eq!(resets, [(MS, 1), (5 * MS, 1)]);
    assert_eq!(r.guest.power_resets, [cycles(MS), cycles(5 * MS)]);
}

#[test]
fn the_reset_from_the_peer_goes_out_before_the_guest_runs_again() {
    // with a latency of 0 the reset is answered in a turn at its own time, as one the guest
    // asked for is
    let guest = FakeGuest::new(vec![]);
    let mut sent = false;
    let peer = FakePeer::new(&[], move |turn| {
        let mut out = Vec::new();
        if turn.t == MS && !sent {
            sent = true;
            out.push(chip_reset(MS));
        }
        out.push(wait(turn.seq, (turn.t < MS).then_some(MS), Some(0)));
        out
    });
    let r = run(0.002, guest, peer);
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    let types = r.peer.types();
    let reset = types.iter().position(|&k| k == "reset").unwrap();
    assert_eq!(types[reset + 1], "sync", "{types:?}");
    assert_eq!(num(&r.peer.msgs[reset + 1], "t"), MS);
}

#[test]
fn with_the_supply_off_no_pad_is_driven() {
    let guest = FakeGuest::new(vec![(cycles(MS / 2), Act::Drive(1 << PWRKEY, 1 << PWRKEY))]);
    let peer = FakePeer::new(
        &[PWRKEY, RI],
        scripted(vec![
            (MS, power(MS, false)),
            // what the peer drives stays driven, and the chip has it when it comes back
            (2 * MS, gpio_in(2 * MS, &[(RI, 1)])),
            (3 * MS, power(3 * MS, true)),
        ]),
    );
    let r = run(0.005, guest, peer);
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    let gpio = r.peer.of("gpio_out");
    assert_eq!(gpio.len(), 3, "{gpio:?}");
    assert_eq!(num(gpio[1], "t"), MS);
    assert_eq!(changes_of(gpio[1]), [(u64::from(PWRKEY), 0)]);
    assert!(pins_of(gpio[1], "enabled").is_empty());
    // at the power-on reset nothing changed since
    assert_eq!(num(gpio[2], "t"), 3 * MS);
    assert!(changes_of(gpio[2]).is_empty());
    assert!(r.peer.of("warn").is_empty());
    assert_eq!(r.guest.board.input_levels(), [(RI, true)]);
}

#[test]
fn the_station_loses_its_access_point_with_the_power_and_finds_a_fresh_one_after() {
    let mut guest = WifiGuest::new(Vec::new(), 64);
    let mut peer = FakePeer::new(
        &[],
        scripted(vec![
            (MS, power(MS, false)),
            (2 * MS, eth_rx(2 * MS, &frame(1))),
            (3 * MS, power(3 * MS, true)),
            (4 * MS, eth_rx(4 * MS, &frame(2))),
        ]),
    );
    let (end, stats) = run_wifi(cycles(6 * MS), &mut guest, &mut peer);
    assert!(matches!(end, End::MaxSeconds), "{end:?}");
    assert_eq!(
        guest.switches,
        [(cycles(MS), false), (cycles(3 * MS), true)]
    );
    let wifi = stats.wifi.unwrap();
    assert_eq!((wifi.eth_rx, wifi.eth_rx_dropped), (1, 1));
    assert_eq!(guest.taken.len(), 1);
    assert_eq!(guest.taken[0].1, frame(2));
}

#[test]
fn an_access_point_switched_with_the_supply_off_is_as_the_peer_left_it_at_power_on() {
    let mut guest = WifiGuest::new(Vec::new(), 64);
    let mut peer = FakePeer::new(
        &[],
        scripted(vec![
            (MS, power(MS, false)),
            (2 * MS, wifi_ap(2 * MS, false)),
            (3 * MS, power(3 * MS, true)),
            (4 * MS, power(4 * MS, false)),
            (5 * MS, wifi_ap(5 * MS, true)),
            (6 * MS, power(6 * MS, true)),
        ]),
    );
    let (end, _) = run_wifi(cycles(8 * MS), &mut guest, &mut peer);
    assert!(matches!(end, End::MaxSeconds), "{end:?}");
    // nothing reaches the station's link while it has no power, and the power-on reset
    // finds the access point as the peer last switched it
    assert_eq!(
        guest.switches,
        [
            (cycles(MS), false),
            (cycles(3 * MS), false),
            (cycles(4 * MS), false),
            (cycles(6 * MS), true),
        ]
    );
    assert_eq!(guest.asked_to_leave, 0);
}

/// On the emulator itself: what `power_on_reset` leaves of the chip. A ROM without segments is
/// enough here: the test looks at the memories, and runs nothing.
#[test]
fn on_the_emulator_a_power_on_reset_clears_sram_and_the_rtc_and_keeps_the_flash() {
    use emu_core::Bus;
    // an ELF32 little-endian header with no program or section headers
    let mut rom = vec![0; 52];
    rom[..6].copy_from_slice(b"\x7fELF\x01\x01");
    let mut m = esp32::machine([0x24, 0x6f, 0x28, 0x00, 0x11, 0x22], 4 << 20);
    let memories = [
        0x3ffb_0000,
        0x4008_0000,
        0x4007_0000,
        0x3ff8_0000,
        0x5000_0000,
    ];
    for address in memories {
        m.bus.load_bytes(address, &[0xa5; 4]).unwrap();
    }
    m.write_flash(0x9000, b"NVS!").unwrap();
    m.bus.cycles = 7 * SECOND;
    m.bus.periph.rtc.0.slow_ticks = 12_345;
    Guest::idle_until(&mut m, 8 * SECOND);
    m.bus.request_reset(12);
    assert_eq!(Guest::power_on_reset(&mut m, &rom), 1);
    for address in memories {
        assert_eq!(m.bus.read32(address).unwrap(), 0, "{address:#x}");
    }
    assert_eq!(&m.bus.flash[0x9000..0x9004], b"NVS!");
    assert_eq!(m.bus.cycles, 8 * SECOND, "the clock runs on");
    let rtc = &m.bus.periph.rtc.0;
    assert_eq!((rtc.reset_cause, rtc.slow_ticks), (1, 0));
    assert_eq!(
        rtc.ram.read(0x38),
        1 | 1 << 6,
        "RESET_STATE: POWERON on both cores"
    );
}

fn sd_card(t: u64, image: Option<&std::path::Path>) -> String {
    let image = image.map_or("null".into(), |p| format!(r#""{}""#, p.display()));
    format!(r#"{{"type":"sd_card","t":{t},"image":{image}}}"#)
}

/// A file of `len` bytes, its own for this test, under the system's temporary directory, removed
/// when it is dropped.
struct ImageFile(std::path::PathBuf);

impl ImageFile {
    fn new(name: &str, len: usize) -> Self {
        let path = std::env::temp_dir().join(format!("esp32tl-{}-{name}", std::process::id()));
        std::fs::write(&path, vec![0; len]).unwrap();
        Self(path)
    }
}

impl Drop for ImageFile {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}

fn run_with_slot(seconds: f64, peer: FakePeer, slot: &sd_card::Shared) -> Run {
    let signal = AtomicBool::new(false);
    let mut guest = FakeGuest::new(Vec::new());
    let shared = guest.shared.clone();
    let mut peer = peer;
    let opts = Options {
        end_cycle: (seconds * SECOND as f64) as u64,
        console: false,
        usb_console: false,
        uart0_stream: false,
        peer_timeout: None,
        signal: &signal,
        parent_gone: None,
        wifi: None,
        rom: &[],
        card: Some(slot.clone()),
        realtime: false,
        flash_out: None,
        hci: None,
        no_bt: false,
    };
    let (end, stats) = Session::new(&mut guest, &mut peer, shared, opts).run();
    Run {
        end,
        stats,
        guest,
        peer,
    }
}

fn slot() -> sd_card::Shared {
    Rc::new(RefCell::new(sd_card::Slot::new(sd_card::Wiring {
        sclk: 14,
        mosi: 15,
        miso: 2,
        cs: 13,
    })))
}

fn acks(peer: &FakePeer) -> Vec<(u64, Option<String>, Option<String>)> {
    let text = |m: &Json, key| m.get(key).and_then(Json::as_str).map(str::to_string);
    peer.of("sd_card_ack")
        .iter()
        .map(|m| (num(m, "t"), text(m, "image"), text(m, "error")))
        .collect()
}

#[test]
fn the_peer_takes_the_card_out_and_puts_cards_in_at_their_times() {
    let (a_file, b_file) = (
        ImageFile::new("a.img", sd_card::UNIT as usize),
        ImageFile::new("b.img", 2 * sd_card::UNIT as usize),
    );
    let (a, b) = (a_file.0.clone(), b_file.0.clone());
    let s = slot();
    s.borrow_mut().insert_file(a.to_str().unwrap()).unwrap();
    let (a2, b2) = (a.clone(), b.clone());
    let mut turns = 0;
    let peer = FakePeer::new(&[], move |turn| {
        turns += 1;
        let mut out = Vec::new();
        if turns == 1 {
            // out at 1 ms; another in at 2 ms; the first again at the same instant as the second
            // goes out, in the order sent
            out.push(sd_card(MS, None));
            out.push(sd_card(2 * MS, Some(&b2)));
            out.push(sd_card(3 * MS, None));
            out.push(sd_card(3 * MS, Some(&a2)));
        }
        out.push(wait(turn.seq, None, None));
        out
    });
    let r = run_with_slot(0.005, peer, &s);
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    let (a, b) = (a.display().to_string(), b.display().to_string());
    assert_eq!(
        acks(&r.peer),
        [
            (MS, None, None),
            (2 * MS, Some(b.clone()), None),
            (3 * MS, None, None),
            (3 * MS, Some(a.clone()), None),
        ]
    );
    let slot = s.borrow();
    assert_eq!(slot.image(), Some(a.as_str()));
    assert_eq!((slot.inserted, slot.removed), (2, 2));
    // not events: nothing to acknowledge, no turn of their own
    assert!(
        r.peer
            .of("sd_card_ack")
            .iter()
            .all(|m| m.get("seq").is_none())
    );
    assert_eq!(r.stats.turns, 1);
}

#[test]
fn a_card_the_slot_cannot_take_is_refused_and_the_slot_stays_as_it_was() {
    let (good_file, short_file) = (
        ImageFile::new("good.img", sd_card::UNIT as usize),
        ImageFile::new("short.img", 1000),
    );
    let (good, short) = (good_file.0.clone(), short_file.0.clone());
    let missing = std::env::temp_dir().join(format!("esp32tl-{}-missing.img", std::process::id()));
    let s = slot();
    let (good2, short2, missing2) = (good.clone(), short.clone(), missing.clone());
    let mut first = true;
    let peer = FakePeer::new(&[], move |turn| {
        let mut out = Vec::new();
        if std::mem::take(&mut first) {
            out.push(sd_card(0, None));
            out.push(sd_card(0, Some(&missing2)));
            out.push(sd_card(0, Some(&short2)));
            out.push(sd_card(MS, Some(&good2)));
            out.push(sd_card(MS, Some(&good2)));
        }
        out.push(wait(turn.seq, None, None));
        out
    });
    let r = run_with_slot(0.002, peer, &s);
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    let acks = acks(&r.peer);
    let good = good.display().to_string();
    // taking the card out of an empty slot changes nothing, and is no error
    assert_eq!(acks[0], (0, None, None));
    let error = |i: usize| acks[i].2.clone().unwrap();
    assert!(acks[1].1.is_none() && error(1).starts_with(&missing.display().to_string()));
    assert!(
        error(2).contains("whole number of 512 KiB units"),
        "{}",
        error(2)
    );
    assert_eq!(acks[3], (MS, Some(good.clone()), None));
    assert_eq!(acks[4].1.as_deref(), Some(good.as_str()));
    assert!(error(4).contains("the slot holds"), "{}", error(4));
    assert_eq!((s.borrow().inserted, s.borrow().removed), (1, 0));
}

#[test]
fn the_card_changes_with_the_supply_off_too() {
    let (a_file, b_file) = (
        ImageFile::new("off-a.img", sd_card::UNIT as usize),
        ImageFile::new("off-b.img", sd_card::UNIT as usize),
    );
    let (a, b) = (a_file.0.clone(), b_file.0.clone());
    let s = slot();
    s.borrow_mut().insert_file(a.to_str().unwrap()).unwrap();
    let b2 = b.clone();
    let peer = FakePeer::new(
        &[],
        scripted(vec![
            (MS, power(MS, false)),
            (2 * MS, sd_card(2 * MS, None)),
            (2 * MS, sd_card(2 * MS, Some(&b2))),
            (3 * MS, power(3 * MS, true)),
        ]),
    );
    let r = run_with_slot(0.005, peer, &s);
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    let b = b.display().to_string();
    assert_eq!(
        acks(&r.peer),
        [(2 * MS, None, None), (2 * MS, Some(b.clone()), None)]
    );
    assert_eq!(s.borrow().image(), Some(b.as_str()));
}

#[test]
fn sd_card_without_a_slot_is_a_protocol_error() {
    let peer = FakePeer::new(&[], |turn| {
        vec![sd_card(0, None), wait(turn.seq, None, None)]
    });
    let r = run(1.0, FakeGuest::new(Vec::new()), peer);
    assert!(
        matches!(&r.end, End::Protocol(m) if m.contains("sd_card without --sd-spi")),
        "{:?}",
        r.end
    );
}

/// A command to the SD card, with its CRC7, and `answer` bytes of 0xff after it.
fn sd_command(index: u8, arg: u32, answer: usize) -> Vec<u8> {
    let mut f = vec![0x40 | index, 0, 0, 0, 0, 0];
    f[1..5].copy_from_slice(&arg.to_be_bytes());
    f[5] = sd_card::crc7(&f[..5]) << 1 | 1;
    f.resize(6 + answer, 0xff);
    f
}

/// A 1 MiB card, initialised as sdspi does it.
fn ready_card() -> sd_card::Shared {
    let store = esp_periph::sdmmc::MemStore(vec![0; 2 * sd_card::UNIT as usize]);
    let mut slot = sd_card::Slot::new(CARD);
    slot.insert("card", Box::new(store)).unwrap();
    for (index, arg) in [
        (0, 0),
        (8, 0x1aa),
        (55, 0),
        (41, 1 << 30),
        (55, 0),
        (41, 1 << 30),
    ] {
        slot.transfer(CARD_PINS, &sd_command(index, arg, 8), 14);
    }
    let answer = slot.transfer(CARD_PINS, &sd_command(58, 0, 8), 14);
    assert_eq!(answer[8] >> 7, 1, "initialised: {answer:?}");
    Rc::new(RefCell::new(slot))
}

fn sd_fault(t: u64, id: u64, rest: &str) -> String {
    format!(r#"{{"type":"sd_fault","t":{t},"id":{id},{rest}}}"#)
}

#[test]
fn an_sd_fault_comes_due_on_the_card_and_what_it_did_goes_out_as_sd_op() {
    let card = ready_card();
    // a read of block 5 at 0.5, 2 and 3 ms, the bytes of its data block clocked with it
    let read = || Act::Spi(sd_command(17, 5, 8 + BLOCK_AND_CRC));
    let guest = FakeGuest::new(vec![
        (MS_CYCLES / 2, read()),
        (2 * MS_CYCLES, read()),
        (3 * MS_CYCLES, read()),
    ])
    .with_card(&card);
    let mut first = true;
    let peer = FakePeer::new(&[], move |turn| {
        let mut out = Vec::new();
        if std::mem::take(&mut first) {
            // from 1 ms, one read fails
            out.push(sd_fault(MS, 1, r#""op":"read","action":"error","count":1"#));
        }
        out.push(wait(turn.seq, None, None));
        out
    });
    let r = run_with_card(0.01, guest, peer, false, Some(&card));
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    let ops = r.peer.of("sd_op");
    assert_eq!(ops.len(), 1, "{:?}", r.peer.types());
    let op = ops[0];
    assert_eq!(num(op, "t"), ns(2 * MS_CYCLES));
    assert_eq!(num(op, "fault"), 1);
    assert_eq!(op.get("op").and_then(Json::as_str), Some("read"));
    assert_eq!(op.get("action").and_then(Json::as_str), Some("error"));
    assert_eq!(op.get("written"), Some(&Json::Bool(false)));
    assert!(op.get("seq").is_none(), "not an event");
    // the card's answers: the block, the error token, the block again
    let tokens: Vec<u8> = r
        .guest
        .spi
        .iter()
        .map(|(_, rx)| *rx[8..].iter().find(|&&b| b != 0xff).unwrap())
        .collect();
    assert_eq!(tokens, [0xfe, 0x04, 0xfe]);
    assert_eq!(card.borrow().stats().faults, 1);
}

#[test]
fn sd_fault_and_sd_clear_need_the_slot_and_an_id_is_used_once() {
    for (line, error) in [
        (
            sd_fault(0, 1, r#""op":"read","action":"error""#),
            "sd_fault without --sd-spi",
        ),
        (
            r#"{"type":"sd_clear","t":0}"#.to_string(),
            "sd_clear without --sd-spi",
        ),
    ] {
        let peer = FakePeer::new(&[], move |turn| {
            vec![line.clone(), wait(turn.seq, None, None)]
        });
        let r = run(0.01, FakeGuest::new(Vec::new()), peer);
        assert!(
            matches!(&r.end, End::Protocol(m) if m.contains(error)),
            "{:?}",
            r.end
        );
    }
    // an id is used once, a rule spent or cleared included
    let card = ready_card();
    let mut turns = 0;
    let peer = FakePeer::new(&[], move |turn| {
        turns += 1;
        let mut out = match turns {
            1 => vec![
                sd_fault(0, 7, r#""op":"read","action":"error""#),
                r#"{"type":"sd_clear","t":0,"ids":[7]}"#.to_string(),
            ],
            _ => vec![sd_fault(turn.t, 7, r#""op":"write","action":"error""#)],
        };
        out.push(wait(turn.seq, Some(turn.t + MS), None));
        out
    });
    let guest = FakeGuest::new(Vec::new()).with_card(&card);
    let r = run_with_card(0.01, guest, peer, false, Some(&card));
    assert!(
        matches!(&r.end, End::Protocol(m) if m.contains("sd_fault: id 7 is in use already")),
        "{:?}",
        r.end
    );
}

fn run_realtime(seconds: f64, mut guest: FakeGuest, mut peer: FakePeer) -> Run {
    let signal = AtomicBool::new(false);
    let shared = guest.shared.clone();
    let opts = Options {
        end_cycle: (seconds * SECOND as f64) as u64,
        console: false,
        usb_console: false,
        uart0_stream: false,
        peer_timeout: None,
        signal: &signal,
        parent_gone: None,
        wifi: None,
        rom: &[],
        card: None,
        realtime: true,
        flash_out: None,
        hci: None,
        no_bt: false,
    };
    let (end, stats) = Session::new(&mut guest, &mut peer, shared, opts).run();
    Run {
        end,
        stats,
        guest,
        peer,
    }
}

/// `quiet_until` every `step` nanoseconds, as the `real` source renews its turn.
fn every(step: u64) -> impl FnMut(&Turn) -> Vec<String> {
    move |turn| vec![wait(turn.seq, Some(turn.t + step), None)]
}

#[test]
fn realtime_paces_a_deep_sleep_like_any_other_stretch() {
    let guest = FakeGuest::new(vec![]).asleep_between(MS_CYCLES, SECOND);
    let run = run_realtime(0.004, guest, FakePeer::new(&[], waking(SECOND)));
    assert!(run.guest.skips.is_empty(), "{:?}", run.guest.skips);
    // a slice to each emulated millisecond, the sleep's as well
    for t in 2..4 {
        assert!(
            run.guest.slices.contains(&cycles(t * MS)),
            "{:?}",
            run.guest.slices
        );
    }
}

#[test]
fn realtime_says_wall_in_hello_and_rt_resyncs_in_stats() {
    let run = run_realtime(0.002, FakeGuest::new(vec![]), FakePeer::new(&[], every(MS)));
    let hello = &run.peer.msgs[0];
    assert_eq!(hello.get("clock").and_then(Json::as_str), Some("wall"));
    assert_eq!(num(run.peer.stats(), "rt_resyncs"), 0);
}

#[test]
fn without_realtime_the_clock_is_emulated_and_nothing_waits_for_wall_time() {
    let run = run_for(
        0.002,
        FakeGuest::new(vec![]),
        FakePeer::new(&[], every(MS)),
        false,
    );
    let hello = &run.peer.msgs[0];
    assert_eq!(
        hello.get("clock").and_then(Json::as_str),
        Some("emulated-fifo-write")
    );
    assert!(run.peer.stats().get("rt_resyncs").is_none());
    assert!(run.guest.paced.is_empty());
    assert!(run.peer.waits.is_empty());
}

#[test]
fn pacing_starts_at_the_first_boundary_past_2_16_cycles_after_the_turn_there() {
    // turns every 100 us, 24 000 cycles: the boundary at 300 us is the first at or after 65 536
    let step = 100_000;
    let run = run_realtime(
        0.001,
        FakeGuest::new(vec![]),
        FakePeer::new(&[], every(step)),
    );
    let (cycle, paced) = run.guest.paced[0];
    assert_eq!(run.guest.paced.len(), 1);
    assert_eq!(cycle, cycles(3 * step));
    // nothing waited before: the turn at 0 and those up to 300 us had gone out by the first wait
    let (syncs, first) = run.peer.waits[0];
    assert_eq!(syncs, 4);
    // emulated time 0 is when pacing started, so the turn at 400 us waits for 400 us from there
    let left = first.duration_since(paced);
    assert!(
        left >= Duration::from_nanos(4 * step) && left < Duration::from_secs(5),
        "{left:?}"
    );
    // and every later turn for another step
    let later: Vec<Duration> = run
        .peer
        .waits
        .windows(2)
        .map(|w| w[1].1.duration_since(w[0].1))
        .collect();
    assert!(!later.is_empty());
    assert!(later.iter().all(|&d| d == Duration::from_nanos(step)));
}

#[test]
fn a_turn_held_before_pacing_starts_does_not_count_against_the_wall_clock() {
    let held = Duration::from_millis(30);
    let mut first = true;
    let peer = FakePeer::new(&[], move |turn| {
        if std::mem::take(&mut first) {
            std::thread::sleep(held);
        }
        vec![wait(turn.seq, Some(turn.t + 100_000), None)]
    });
    let before = Instant::now();
    let run = run_realtime(0.001, FakeGuest::new(vec![]), peer);
    let (_, paced) = run.guest.paced[0];
    assert!(paced.duration_since(before) >= held);
    // measured from after the hold: the runner still waits for the whole of the next block point
    let (_, first_wait) = run.peer.waits[0];
    assert!(first_wait.duration_since(paced) >= Duration::from_nanos(400_000));
}

#[test]
fn a_resync_moves_the_wall_clock_to_the_boundary_it_is_seen_at() {
    let step = MS;
    // a turn held for 30 ms at 3 ms, and the lag skipped in the slice after it
    let held = Duration::from_millis(30);
    let peer = FakePeer::new(&[], move |turn| {
        if turn.t == 3 * step {
            std::thread::sleep(held);
        }
        vec![wait(turn.seq, Some(turn.t + step), None)]
    });
    let mut guest = FakeGuest::new(vec![]);
    guest.lags = vec![cycles(3 * step) + 1];
    let run = run_realtime(0.006, guest, peer);
    assert_eq!(num(run.peer.stats(), "rt_resyncs"), 1);
    let at = |t: u64| {
        let syncs = run.peer.of("sync");
        let index = syncs.iter().position(|m| num(m, "t") == t).unwrap();
        run.peer.waits.iter().find(|(n, _)| *n == index).unwrap().1
    };
    // the wait at 4 ms is the first after the anchor moved, and comes after the hold: on the old
    // anchor it would be 4 ms from where pacing started
    let (_, paced) = run.guest.paced[0];
    assert!(at(4 * step).duration_since(paced) >= held);
    assert_eq!(
        at(5 * step).duration_since(at(4 * step)),
        Duration::from_nanos(step)
    );
}

#[test]
fn a_board_switched_off_before_pacing_starts_starts_it_when_the_guest_runs_again() {
    let peer = FakePeer::new(&[], |turn| {
        let mut lines = vec![];
        if turn.t == 0 {
            lines.push(power(0, false));
        }
        if turn.t == 2 * MS {
            lines.push(power(turn.t, true));
        }
        lines.push(wait(turn.seq, Some(turn.t + MS), None));
        lines
    });
    let run = run_realtime(0.004, FakeGuest::new(vec![]), peer);
    assert_eq!(run.guest.paced.len(), 1);
    assert_eq!(run.guest.paced[0].0, cycles(2 * MS));
}

#[test]
fn a_signal_or_the_peer_gone_ends_the_wait_for_wall_time() {
    let mut peer = FakePeer::new(&[], every(100_000));
    peer.wait_ends = Some(LinkError::Signal);
    let run = run_realtime(0.001, FakeGuest::new(vec![]), peer);
    assert!(matches!(run.end, End::Signal));
    assert_eq!(
        run.peer.stats().get("end").and_then(Json::as_str),
        Some("signal")
    );
    assert_eq!(num(run.peer.stats(), "rt_resyncs"), 0);

    let mut peer = FakePeer::new(&[], every(100_000));
    peer.wait_ends = Some(LinkError::Gone("the peer closed the socket".into()));
    let run = run_realtime(0.001, FakeGuest::new(vec![]), peer);
    assert!(matches!(run.end, End::PeerGone(_)));
}

const FLASH_DUMP: &str = r#"{"type":"flash_dump"}"#;

/// A file for `--flash-out`, in a directory of its own.
struct FlashOut(PathBuf);

impl FlashOut {
    fn new(name: &str) -> Self {
        let directory =
            std::env::temp_dir().join(format!("esp32tl-flash-{}-{name}", std::process::id()));
        std::fs::create_dir_all(&directory).unwrap();
        Self(directory.join("flash.bin"))
    }
    fn read(&self) -> Vec<u8> {
        std::fs::read(&self.0).unwrap()
    }
}

impl Drop for FlashOut {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(self.0.parent().unwrap());
    }
}

fn flash_with(bytes: &[(usize, u8)]) -> Vec<u8> {
    let mut flash = vec![0xff; 16];
    for &(at, byte) in bytes {
        flash[at] = byte;
    }
    flash
}

#[test]
fn a_flash_dump_writes_the_flash_of_the_turn_and_the_turn_goes_on() {
    let out = FlashOut::new("turn");
    let path = out.0.clone();
    let dumps = Rc::new(RefCell::new(Vec::new()));
    let seen = dumps.clone();
    let mut turns = 0;
    let peer = FakePeer::new(&[], move |turn| {
        turns += 1;
        // what the turn before asked for is in the file by now, before this one asks again
        if turns > 1 {
            seen.borrow_mut().push(std::fs::read(&path).unwrap());
        }
        vec![
            FLASH_DUMP.to_string(),
            wait(turn.seq, Some(turn.t + 10 * MS), None),
        ]
    });
    let script = vec![(5 * MS_CYCLES, Act::Flash(3, 0x42))];
    let r = run_with(
        0.025,
        FakeGuest::new(script),
        peer,
        false,
        None,
        Some(out.0.as_path()),
    );
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    let dumps = dumps.borrow();
    // not the flash the run started with: the current one, at each turn
    assert_eq!(dumps[0], vec![0xff; 16]);
    assert_eq!(dumps[1], flash_with(&[(3, 0x42)]));
    assert_eq!(
        r.peer
            .msgs
            .iter()
            .filter(|m| matches!(kind(m), "sync" | "flash_dump_ack"))
            .map(|m| (kind(m).to_string(), num(m, "t")))
            .take(4)
            .collect::<Vec<_>>(),
        [
            ("sync".to_string(), 0),
            ("flash_dump_ack".to_string(), 0),
            ("sync".to_string(), 10 * MS),
            ("flash_dump_ack".to_string(), 10 * MS),
        ]
    );
    let ack = r.peer.of("flash_dump_ack")[0];
    assert_eq!(ack.get("error"), Some(&Json::Null));
    assert_eq!(r.stats.turns, 3, "the dumps took no turn of their own");
}

#[test]
fn a_flash_dump_moves_no_time_and_runs_no_guest() {
    let out = FlashOut::new("still");
    let mut first = true;
    let peer = FakePeer::new(&[], move |turn| {
        let mut lines = Vec::new();
        if std::mem::take(&mut first) {
            lines = vec![FLASH_DUMP.to_string(); 3];
        }
        lines.push(wait(turn.seq, None, None));
        lines
    });
    let r = run_with(
        0.001,
        FakeGuest::new(vec![]),
        peer,
        false,
        None,
        Some(out.0.as_path()),
    );
    assert_eq!(r.peer.of("flash_dump_ack").len(), 3);
    assert_eq!(r.stats.turns, 1);
    assert_eq!(
        r.guest.slices[0], MS_CYCLES,
        "the first slice as without them"
    );
}

#[test]
fn a_flash_dump_that_cannot_be_written_says_why_and_the_run_goes_on() {
    let out = FlashOut::new("unwritable");
    let path = out.0.join("no-such-directory").join("flash.bin");
    let mut first = true;
    let peer = FakePeer::new(&[], move |turn| {
        let mut lines = Vec::new();
        if std::mem::take(&mut first) {
            lines.push(FLASH_DUMP.to_string());
        }
        lines.push(wait(turn.seq, None, None));
        lines
    });
    let r = run_with(
        0.001,
        FakeGuest::new(vec![]),
        peer,
        false,
        None,
        Some(path.as_path()),
    );
    let ack = r.peer.of("flash_dump_ack")[0];
    let error = ack.get("error").and_then(Json::as_str).unwrap();
    assert!(error.contains("no-such-directory"), "{error}");
    // nor does a file that cannot be written at the end change how the run ended
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
}

#[test]
fn each_flash_dump_ack_starts_the_peer_timeout_again() {
    let out = FlashOut::new("timeout");
    let signal = AtomicBool::new(false);
    let mut guest = FakeGuest::new(vec![]);
    let shared = guest.shared.clone();
    let mut first = true;
    let mut peer = FakePeer::new(&[GNSS_RX], move |turn| {
        let mut lines = Vec::new();
        if std::mem::take(&mut first) {
            lines = vec![FLASH_DUMP.to_string(); 2];
            lines.push(uart_rx(0, GNSS_RX, 9600, b"x"));
        }
        lines.push(wait(turn.seq, None, None));
        lines
    });
    let opts = Options {
        end_cycle: MS_CYCLES,
        console: false,
        usb_console: false,
        uart0_stream: false,
        peer_timeout: Some(Duration::from_secs(3600)),
        signal: &signal,
        parent_gone: None,
        wifi: None,
        rom: &[],
        card: None,
        realtime: false,
        flash_out: Some(out.0.as_path()),
        hci: None,
        no_bt: false,
    };
    Session::new(&mut guest, &mut peer, shared, opts).run();
    // the handshake's, the turn's first, then one after each answer; an input starts nothing
    let d: Vec<Instant> = peer.deadlines.iter().map(|d| d.unwrap()).collect();
    assert!(d[1] < d[2], "a write of the flash is not the peer's time");
    assert!(d[2] < d[3]);
    assert_eq!(d[3], d[4], "only an answer starts it again");
}

#[test]
fn a_flash_dump_without_flash_out_is_a_protocol_error() {
    let peer = FakePeer::new(&[], |_| vec![FLASH_DUMP.to_string()]);
    let r = run(0.01, FakeGuest::new(vec![]), peer);
    let End::Protocol(detail) = &r.end else {
        panic!("{:?}", r.end)
    };
    assert!(
        detail.contains("flash_dump without --flash-out"),
        "{detail}"
    );
}

/// A file for `flash_write`, in a directory of its own.
fn flash_file(name: &str, bytes: &[u8]) -> FlashOut {
    let file = FlashOut::new(name);
    std::fs::write(&file.0, bytes).unwrap();
    file
}

fn flash_write(offset: usize, path: &Path) -> String {
    format!(
        r#"{{"type":"flash_write","offset":{offset},"path":"{}"}}"#,
        path.display()
    )
}

#[test]
fn a_flash_write_with_the_supply_off_is_in_the_flash_at_the_next_boot() {
    let first = flash_file("off-first", b"ab");
    let second = flash_file("off-second", b"cd");
    let peer = FakePeer::new(
        &[],
        scripted(vec![
            // in the turn the supply goes off in, before the switch has been applied
            (MS, power(MS, false)),
            (MS, flash_write(4, &first.0)),
            // in a later one, with the supply off since
            (3 * MS, flash_write(8, &second.0)),
            (5 * MS, power(5 * MS, true)),
        ]),
    );
    let r = run(0.01, FakeGuest::new(vec![]), peer);
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    assert_eq!(
        r.guest.flash,
        flash_with(&[(4, b'a'), (5, b'b'), (8, b'c'), (9, b'd')])
    );
    let acks = r.peer.of("flash_write_ack");
    assert_eq!(
        acks.iter().map(|m| num(m, "t")).collect::<Vec<_>>(),
        [MS, 3 * MS]
    );
    assert!(acks.iter().all(|m| m.get("error") == Some(&Json::Null)));
    assert_eq!(r.guest.power_resets, [cycles(5 * MS)]);
    assert_eq!(r.stats.turns, 4, "the writes took no turn of their own");
}

#[test]
fn the_flash_is_synced_for_a_dump_or_a_write_only_with_the_supply_off_for_good() {
    let out = FlashOut::new("sync");
    let file = flash_file("sync-file", b"ab");
    let peer = FakePeer::new(
        &[],
        scripted(vec![
            // on: the guest runs on after the dump, so nothing of it is synced
            (0, FLASH_DUMP.to_string()),
            (MS, power(MS, false)),
            (MS, FLASH_DUMP.to_string()),
            (2 * MS, flash_write(0, &file.0)),
        ]),
    );
    let r = run_with(
        0.005,
        FakeGuest::new(vec![]),
        peer,
        false,
        None,
        Some(out.0.as_path()),
    );
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    assert_eq!(r.guest.flash_syncs.len(), 2, "{:?}", r.guest.flash_syncs);
    assert!(r.guest.flash_syncs[0] >= cycles(MS));
}

#[test]
fn a_flash_write_with_the_supply_on_is_a_protocol_error() {
    let file = flash_file("on", b"ab");
    let path = file.0.clone();
    // on from the start, and on again in the turn it comes back in
    for script in [
        vec![(0, flash_write(0, &path))],
        vec![
            (MS, power(MS, false)),
            (2 * MS, power(2 * MS, true)),
            (2 * MS, flash_write(0, &path)),
        ],
    ] {
        let r = run(
            0.01,
            FakeGuest::new(vec![]),
            FakePeer::new(&[], scripted(script)),
        );
        let End::Protocol(detail) = &r.end else {
            panic!("{:?}", r.end)
        };
        assert!(
            detail.contains("flash_write with the supply on"),
            "{detail}"
        );
        assert_eq!(r.guest.flash, vec![0xff; 16]);
    }
}

#[test]
fn a_flash_write_that_cannot_be_done_says_why_and_the_run_goes_on() {
    let file = flash_file("refused", b"abc");
    let missing = file.0.with_file_name("missing.bin");
    let peer = FakePeer::new(
        &[],
        scripted(vec![
            (0, power(0, false)),
            (0, flash_write(0, &missing)),
            (0, flash_write(14, &file.0)),
        ]),
    );
    let r = run(0.01, FakeGuest::new(vec![]), peer);
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    let errors: Vec<&str> = r
        .peer
        .of("flash_write_ack")
        .iter()
        .map(|m| m.get("error").and_then(Json::as_str).unwrap())
        .collect();
    assert!(
        errors[0].starts_with(&format!("{}: ", missing.display())),
        "{}",
        errors[0]
    );
    assert_eq!(
        errors[1],
        format!(
            "{}: 3 bytes at 0xe end past the flash, of 16 bytes",
            file.0.display()
        )
    );
    assert_eq!(r.guest.flash, vec![0xff; 16], "nothing of either written");
}

/// A flash byte, then one of the ways a run can end, by its exit code.
#[test]
fn every_end_of_a_run_leaves_the_flash_in_the_file() {
    let written = (MS_CYCLES / 2, Act::Flash(1, 0x17));
    let ends = [
        (
            0,
            vec![written.clone()],
            FakePeer::new(&[], idle(None, None)),
        ),
        (
            1,
            vec![written.clone(), (MS_CYCLES, Act::Fault)],
            FakePeer::new(&[], idle(None, None)),
        ),
        // the peer's second turn breaks the rules
        (3, vec![written.clone()], {
            let mut turns = 0;
            FakePeer::new(&[], move |turn| {
                turns += 1;
                if turns == 1 {
                    vec![wait(turn.seq, Some(MS), None)]
                } else {
                    vec![r#"{"type":"bogus"}"#.to_string()]
                }
            })
        }),
        // a stop in the second turn
        (0, vec![written.clone()], {
            let mut turns = 0;
            FakePeer::new(&[], move |turn| {
                turns += 1;
                if turns == 1 {
                    vec![wait(turn.seq, Some(MS), None)]
                } else {
                    vec![r#"{"type":"stop"}"#.to_string()]
                }
            })
        }),
        // gone after its first turn: nothing to say at the second
        (4, vec![written.clone()], {
            let mut turns = 0;
            FakePeer::new(&[], move |turn| {
                turns += 1;
                if turns == 1 {
                    vec![wait(turn.seq, Some(MS), None)]
                } else {
                    vec![]
                }
            })
        }),
    ];
    for (index, (code, script, peer)) in ends.into_iter().enumerate() {
        let out = FlashOut::new(&format!("end-{index}"));
        let r = run_with(
            0.01,
            FakeGuest::new(script),
            peer,
            false,
            None,
            Some(out.0.as_path()),
        );
        assert_eq!(r.end.exit_code(), code, "{:?}", r.end);
        assert_eq!(out.read(), flash_with(&[(1, 0x17)]), "exit code {code}");
    }
}

#[test]
fn a_run_without_flash_out_writes_no_flash() {
    let peer = FakePeer::new(&[], idle(None, None));
    let r = run(0.001, FakeGuest::new(vec![]), peer);
    assert!(matches!(r.end, End::MaxSeconds));
    assert!(r.peer.of("flash_dump_ack").is_empty());
}

fn hci_rx(t: u64, packet: &[u8]) -> String {
    format!(
        r#"{{"type":"hci_rx","t":{t},"data":"{}"}}"#,
        protocol::base64(packet)
    )
}

const HCI_RESET: [u8; 4] = [0x01, 0x03, 0x0c, 0x00];
/// The Command_Complete for HCI_Reset.
const RESET_DONE: [u8; 7] = [0x04, 0x0e, 0x04, 0x01, 0x03, 0x0c, 0x00];

#[test]
fn each_hci_packet_goes_out_as_an_hci_tx_at_its_own_cycle_in_order_of_t() {
    let ms = cycles(MS);
    let mut script = tx(ms, 0, b"ab\n", 100);
    script.extend([
        (ms + 50, Act::HciTx(HCI_RESET.to_vec())),
        // at the cycle of the byte before it in the board's log: the byte goes first
        (ms + 100, Act::HciTx(vec![0x01, 0x01, 0x10, 0x00])),
        (ms + 150, Act::Tx(1, b'X')),
        (2 * ms, Act::HciTx(vec![0x02, 0x01, 0x20])),
    ]);
    let guest = FakeGuest::new(script)
        .with_routes(&[route(0, Some(1), Some(3), Some(115_200)), modem(115_200)])
        .with_bt();
    let r = run_for(
        0.01,
        guest,
        FakePeer::new(&[MODEM_TX], idle(None, None)),
        true,
    );
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    let sent: Vec<(&str, u64, Vec<u8>)> = r
        .peer
        .msgs
        .iter()
        .filter(|m| matches!(kind(m), "uart_tx" | "console" | "hci_tx"))
        .map(|m| (kind(m), num(m, "t"), data(m)))
        .collect();
    assert_eq!(
        sent,
        [
            ("console", ns(ms), b"a".to_vec()),
            ("hci_tx", ns(ms + 50), HCI_RESET.to_vec()),
            ("console", ns(ms + 100), b"b".to_vec()),
            ("hci_tx", ns(ms + 100), vec![0x01, 0x01, 0x10, 0x00]),
            ("uart_tx", ns(ms + 150), b"X".to_vec()),
            ("console", ns(ms + 200), b"\n".to_vec()),
            ("hci_tx", ns(2 * ms), vec![0x02, 0x01, 0x20]),
        ]
    );
    // events: each has its seq, which the fake peer checks is the next one
    assert!(r.peer.of("hci_tx").iter().all(|m| m.get("seq").is_some()));
}

#[test]
fn an_hci_tx_sets_the_block_point_at_its_t_plus_latency() {
    let at = cycles(MS) + 12_345;
    let guest = FakeGuest::new(vec![(at, Act::HciTx(HCI_RESET.to_vec()))]).with_bt();
    let latency = 300_000;
    let r = run(0.01, guest, FakePeer::new(&[], idle(None, Some(latency))));
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    let syncs: Vec<u64> = r.peer.of("sync").iter().map(|m| num(m, "t")).collect();
    assert_eq!(syncs, [0, ns(at) + latency]);
    assert!(r.guest.slices.contains(&cycles(ns(at) + latency)));
}

#[test]
fn an_hci_rx_reaches_the_guests_host_at_its_time_and_not_before() {
    let due = cycles(5 * MS);
    let guest = FakeGuest::new(vec![
        (cycles(2 * MS), Act::HciPoll),
        (due - 1, Act::HciPoll),
        (due, Act::HciPoll),
        (due, Act::HciPoll),
        (cycles(6 * MS), Act::HciPoll),
    ])
    .with_bt();
    let peer = FakePeer::new(
        &[],
        scripted(vec![
            (0, hci_rx(5 * MS, &RESET_DONE)),
            (0, hci_rx(5 * MS, &[0x04, 0x0f])),
        ]),
    );
    let r = run(0.01, guest, peer);
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    // one packet a poll, in the order the peer sent them
    assert_eq!(
        r.guest.hci_read,
        [
            (cycles(2 * MS), None),
            (due - 1, None),
            (due, Some(RESET_DONE.to_vec())),
            (due, Some(vec![0x04, 0x0f])),
            (cycles(6 * MS), None),
        ]
    );
    assert!(r.guest.slices.contains(&due));
}

#[test]
fn a_chip_reset_loses_the_hci_packets_the_host_has_not_read() {
    let guest = FakeGuest::new(vec![
        (cycles(MS) + 7, Act::HciTx(HCI_RESET.to_vec())),
        (cycles(2 * MS), Act::Reset(0xc)),
        (cycles(3 * MS), Act::HciPoll),
        (cycles(5 * MS), Act::HciPoll),
    ])
    .with_bt();
    let peer = FakePeer::new(
        &[],
        scripted(vec![
            (0, hci_rx(MS, &RESET_DONE)),
            (0, hci_rx(4 * MS, &[0x04, 0x0f])),
        ]),
    );
    let r = run(0.01, guest, peer);
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    // the packet due before the reset is gone; the one due after it is read
    assert_eq!(
        r.guest.hci_read,
        [
            (cycles(3 * MS), None),
            (cycles(5 * MS), Some(vec![0x04, 0x0f]))
        ]
    );
    // what the host sent before the reset goes out before it
    let order: Vec<&str> = r
        .peer
        .types()
        .into_iter()
        .filter(|k| matches!(*k, "hci_tx" | "reset"))
        .collect();
    assert_eq!(order, ["hci_tx", "reset"]);
}

#[test]
fn a_power_on_reset_loses_the_hci_packets_the_host_has_not_read() {
    let guest = FakeGuest::new(vec![(cycles(3 * MS), Act::HciPoll)]).with_bt();
    let peer = FakePeer::new(
        &[],
        scripted(vec![(0, hci_rx(MS, &RESET_DONE)), (0, chip_reset(2 * MS))]),
    );
    let r = run(0.01, guest, peer);
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    assert_eq!(r.guest.power_resets, [cycles(2 * MS)]);
    assert_eq!(r.guest.hci_read, [(cycles(3 * MS), None)]);
}

#[test]
fn hci_rx_needs_bt_and_fits_the_vhci() {
    let peer = |line: String| {
        FakePeer::new(&[], move |turn| {
            vec![line.clone(), wait(turn.seq, None, None)]
        })
    };
    let r = run(
        1.0,
        FakeGuest::new(Vec::new()),
        peer(hci_rx(0, &RESET_DONE)),
    );
    assert!(
        matches!(&r.end, End::Protocol(m) if m.starts_with("hci_rx without --bt: ")),
        "{:?}",
        r.end
    );
    let r = run(
        1.0,
        FakeGuest::new(Vec::new()).with_bt(),
        peer(hci_rx(0, &[0x02; protocol::HCI_MAX + 1])),
    );
    assert!(
        matches!(&r.end, End::Protocol(m) if m.starts_with(
            "hci_rx: an H4 packet of 497 bytes; esp32sim's VHCI carries 1 to 496: "
        )),
        "{:?}",
        r.end
    );
    let r = run(
        0.01,
        FakeGuest::new(Vec::new()).with_bt(),
        peer(hci_rx(0, &[0x02; protocol::HCI_MAX])),
    );
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
}

#[test]
fn on_the_emulator_the_flash_read_has_the_code_esp32sims_trampoline_covers() {
    let mut m = esp32::machine([0x24, 0x6f, 0x28, 0x00, 0x11, 0x22], 4 << 20);
    assert!(matches!(Guest::flash(&m), Cow::Borrowed(_)));
    // as `install` leaves it: the trampoline in the flash, the code it covers in `original_flash`
    m.bus.flash[0x10020..0x10024].copy_from_slice(&[0x36, 0x41, 0x00, 0x90]);
    m.bus.ble.original_flash = Some((0x10020, vec![1, 2, 3, 4]));
    let flash = Guest::flash(&m);
    assert_eq!(flash.len(), m.bus.flash.len());
    assert_eq!(flash[0x10020..0x10024], [1, 2, 3, 4]);
    assert_eq!(flash[..0x10020], m.bus.flash[..0x10020]);
    assert_eq!(flash[0x10024..], m.bus.flash[0x10024..]);
    // the guest still runs the trampoline
    assert_eq!(m.bus.flash[0x10020..0x10024], [0x36, 0x41, 0x00, 0x90]);
}

#[test]
fn console_rx_reset_restores_native_input_but_firmware_can_disable_or_reroute_it() {
    use emu_core::Bus;
    let mut m = esp32::machine([0x24, 0x6f, 0x28, 0, 0x11, 0x22], 4 << 20);
    reset_console_rx(&mut m);
    assert_eq!(Guest::uart_route(&m, 0).rx_pin, Some(3));
    // A firmware disabling the input must not receive bytes through a fabricated route.
    m.bus.write32(0x3ff4_9084, 0).unwrap();
    assert_eq!(Guest::uart_route(&m, 0).rx_pin, None);
    Guest::chip_reset(&mut m);
    assert_eq!(Guest::uart_route(&m, 0).rx_pin, Some(3));
    // uart_set_pin may use the GPIO matrix instead of the native IO_MUX function.
    m.bus.write32(0x3ff4_9084, (2 << 12) | (1 << 9)).unwrap();
    m.bus.write32(0x3ff4_4130 + 4 * 14, 0x80 | 3).unwrap();
    assert_eq!(Guest::uart_route(&m, 0).rx_pin, Some(3));
    // An explicitly selected other RX pin is respected too.
    m.bus.write32(0x3ff4_9048, (2 << 12) | (1 << 9)).unwrap();
    m.bus.write32(0x3ff4_4130 + 4 * 14, 0x80 | 4).unwrap();
    assert_eq!(Guest::uart_route(&m, 0).rx_pin, Some(4));
}

#[test]
fn virtual_station_relays_frames_and_refuses_access_point_switches() {
    let wifi = cli::Wifi::Station {
        spec: "ssid=setup".into(),
    };
    let mut first = true;
    let mut peer = FakePeer::new(&[], move |turn| {
        let mut out = Vec::new();
        if std::mem::take(&mut first) {
            out.push(eth_rx(MS, &frame(7)));
        }
        out.push(wait(turn.seq, None, Some(MS)));
        out
    });
    let mut guest = WifiGuest::new(vec![(cycles(MS), frame(8))], 64);
    let (end, stats) = run_wifi_mode(cycles(3 * MS), &mut guest, &mut peer, &wifi);
    assert!(matches!(end, End::MaxSeconds), "{end:?}");
    assert_eq!(guest.taken, [(cycles(MS), frame(7))]);
    assert_eq!(data(peer.of("eth_tx")[0]), frame(8));
    let stats = stats.wifi.unwrap();
    assert_eq!((stats.eth_rx, stats.eth_tx), (1, 1));
    let mut peer = FakePeer::new(&[], |turn| {
        vec![wifi_ap(turn.t, false), wait(turn.seq, None, None)]
    });
    let (end, _) = run_wifi_mode(cycles(MS), &mut guest, &mut peer, &wifi);
    assert!(
        matches!(end, End::Protocol(ref m) if m.contains("wifi_ap with --wifi-sta")),
        "{end:?}"
    );
}

#[test]
fn emulator_station_uses_relay_and_waits_until_joined() {
    let mut m = esp32::machine([0x24, 0x6f, 0x28, 0, 0x11, 0x22], 4 << 20);
    let wifi = cli::Wifi::Station {
        spec: "ssid=setup".into(),
    };
    m.wifi_access_point(&wifi);
    m.bus.set_ethernet_relay(true).unwrap();
    assert!(!m.bus.periph.wifi.link.station().unwrap().host);
    assert!(!m.eth_input(&frame(1)));
    assert!(m.bus.periph.wifi.link.ap().is_none());
}

#[test]
fn station_dhcp_retries_do_not_accumulate_behind_association_backpressure() {
    let wifi = cli::Wifi::Station {
        spec: "ssid=setup".into(),
    };
    let mut dhcp = vec![0; 342];
    dhcp[12..14].copy_from_slice(&[8, 0]);
    dhcp[14] = 0x45;
    dhcp[23] = 17;
    dhcp[34..38].copy_from_slice(&[0, 68, 0, 67]);
    let expected = dhcp.clone();
    let mut first = true;
    let mut peer = FakePeer::new(&[], move |turn| {
        let mut out = Vec::new();
        if std::mem::take(&mut first) {
            for _ in 0..10 {
                out.push(eth_rx(0, &dhcp));
            }
        }
        out.push(wait(turn.seq, None, Some(MS)));
        out
    });
    let mut guest = WifiGuest::new(Vec::new(), 1);
    guest.queued = 1; // the first copy cannot enter the link yet
    let (end, stats) = run_wifi_mode(cycles(3 * MS), &mut guest, &mut peer, &wifi);
    assert!(matches!(end, End::MaxSeconds), "{end:?}");
    assert_eq!(guest.taken.len(), 1);
    assert_eq!(guest.taken[0].1, expected);
    assert_eq!(stats.wifi.unwrap().eth_rx, 1);
}

#[test]
fn station_lifecycle_reads_handshake_state_and_flushes_relay() {
    let mut m = esp32::machine([0x24, 0x6f, 0x28, 0, 0x11, 0x22], 4 << 20);
    let wifi = cli::Wifi::Station {
        spec: "ssid=setup".into(),
    };
    m.wifi_access_point(&wifi);
    m.bus.set_ethernet_relay(true).unwrap();
    assert_eq!(m.station_state(), Some((false, 0)));
    // The public state differentiates association/handshake from data readiness.
    // Exercise the real relay queue via a joined station made outside the bus.
    let mut sta = wifi::sta::VirtualSta::new(wifi.station().unwrap(), false);
    sta.state = wifi::sta::StaLink::Handshake;
    m.bus.periph.wifi.link.attach_station(sta).unwrap();
    assert_eq!(m.station_state(), Some((false, 0)));
    assert!(!m.eth_input(&frame(1)));
    let mut sta = wifi::sta::VirtualSta::new(wifi.station().unwrap(), false);
    sta.state = wifi::sta::StaLink::Joined;
    sta.stats.0 = 1;
    m.bus.periph.wifi.link.attach_station(sta).unwrap();
    assert_eq!(m.station_state(), Some((true, 1)));
    assert!(m.eth_input(&frame(2)));
    m.clear_station_relay();
    assert!(!m.bus.periph.wifi.link.station().unwrap().host);
    // A full ring of new frames fits: no previous frame remains after the flush.
    for _ in 0..64 {
        assert!(m.eth_input(&frame(3)));
    }
    assert!(!m.eth_input(&frame(4)));
}

#[test]
fn station_disconnect_drops_backpressure_and_future_ethernet() {
    let wifi = cli::Wifi::Station {
        spec: "ssid=setup".into(),
    };
    let mut guest = WifiGuest::new(Vec::new(), 0);
    guest.station_states = vec![
        (0, true, 1),
        (cycles(2 * MS), false, 1),
        (cycles(4 * MS), true, 2),
    ];
    let mut first = true;
    let mut peer = FakePeer::new(&[], move |turn| {
        let mut out = Vec::new();
        if std::mem::take(&mut first) {
            out.push(eth_rx(0, &frame(1))); // blocked in runner's queue
            out.push(eth_rx(5 * MS, &frame(2))); // scheduled beyond reconnect
        }
        out.push(wait(turn.seq, None, Some(MS)));
        out
    });
    let (end, stats) = run_wifi_mode(cycles(6 * MS), &mut guest, &mut peer, &wifi);
    assert_eq!(stats.wifi.unwrap().eth_rx_dropped, 2);
    assert!(matches!(end, End::MaxSeconds));
    assert!(guest.taken.is_empty());
    let events = peer.of("wifi_link");
    assert_eq!(events.len(), 3);
    assert_eq!(num(events[0], "t"), 0);
    assert_eq!(num(events[1], "t"), 2 * MS);
    assert_eq!(num(events[2], "t"), 4 * MS);
}

fn console_rx(t: u64, bytes: &[u8]) -> String {
    format!(
        r#"{{"type":"console_rx","t":{t},"data":"{}"}}"#,
        protocol::base64(bytes)
    )
}

#[test]
fn with_the_console_on_usb_its_output_goes_out_as_console_and_uart0_stays_off_it() {
    // protocol 5.7 and 5.23: what the guest flushed in a slice, stamped with the slice's end, a
    // message per line; UART0's bytes are no console with --console-port usb
    let mut script = tx(cycles(MS / 2), 0, b"rom\n", 100);
    script.push((cycles(MS / 2), Act::UsbTx(b"boot\nhel".to_vec())));
    script.push((cycles(3 * MS / 2), Act::UsbTx(b"lo\n".to_vec())));
    let guest = FakeGuest::new(script)
        .with_routes(&[route(0, Some(1), Some(3), Some(115_200))])
        .with_usb_console();
    let r = run_for(0.01, guest, FakePeer::new(&[], idle(None, None)), true);
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    let console: Vec<(u64, Vec<u8>)> = r
        .peer
        .of("console")
        .iter()
        .map(|m| (num(m, "t"), data(m)))
        .collect();
    assert_eq!(
        console,
        [
            (MS, b"boot\n".to_vec()),
            (MS, b"hel".to_vec()),
            (2 * MS, b"lo\n".to_vec())
        ]
    );
    assert!(r.peer.of("uart_tx").is_empty());
    assert_eq!(num(r.peer.stats(), "console_rx_lost"), 0);
    // without --console socket, nothing, and the guest's output stays where it was
    let guest =
        FakeGuest::new(vec![(cycles(MS / 2), Act::UsbTx(b"x\n".to_vec()))]).with_usb_console();
    let r = run(0.01, guest, FakePeer::new(&[], idle(None, None)));
    assert_eq!(r.peer.types(), ["hello", "sync", "stats"]);
}

#[test]
fn console_rx_reaches_the_usb_console_when_it_comes_due_and_is_lost_with_the_supply_off() {
    let guest = FakeGuest::new(Vec::new()).with_usb_console();
    let peer = FakePeer::new(
        &[],
        scripted(vec![
            (0, console_rx(MS + 1, b"help\n")),
            (2 * MS, power(2 * MS, false)),
            (2 * MS, console_rx(3 * MS, b"lost\n")),
            (4 * MS, power(4 * MS, true)),
        ]),
    );
    let r = run_for(0.01, guest, peer, true);
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    assert_eq!(r.guest.usb_in, [(cycles(MS + 1), b"help\n".to_vec())]);
    assert_eq!(r.stats.console_rx_lost, Some(5));
    assert_eq!(num(r.peer.stats(), "console_rx_lost"), 5);
}

#[test]
fn on_a_uart0_without_pins_the_console_goes_out_per_slice_and_console_rx_goes_into_uart0() {
    // the P4's and the S31's: what UART0 sent in a slice, stamped with its end, as on USB; the
    // peer's console_rx into UART0 when it comes due, and lost with the supply off
    let script = vec![
        (cycles(MS / 2), Act::Uart0Stream(b"rom\nhel".to_vec())),
        (cycles(3 * MS / 2), Act::Uart0Stream(b"lo\n".to_vec())),
    ];
    let guest = FakeGuest::new(script).with_uart0_stream();
    let peer = FakePeer::new(
        &[],
        scripted(vec![
            (0, console_rx(MS + 1, b"help\n")),
            (2 * MS, power(2 * MS, false)),
            (2 * MS, console_rx(3 * MS, b"lost\n")),
            (4 * MS, power(4 * MS, true)),
        ]),
    );
    let r = run_for(0.01, guest, peer, true);
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    let console: Vec<(u64, Vec<u8>)> = r
        .peer
        .of("console")
        .iter()
        .map(|m| (num(m, "t"), data(m)))
        .collect();
    assert_eq!(
        console,
        [
            (MS, b"rom\n".to_vec()),
            (MS, b"hel".to_vec()),
            (2 * MS, b"lo\n".to_vec())
        ]
    );
    assert_eq!(r.guest.uart0_in, [(cycles(MS + 1), b"help\n".to_vec())]);
    assert!(r.guest.usb_in.is_empty());
    assert_eq!(num(r.peer.stats(), "console_rx_lost"), 5);
}

#[test]
fn console_rx_needs_the_console_on_usb() {
    let peer = FakePeer::new(&[], move |turn| {
        vec![console_rx(0, b"help\n"), wait(turn.seq, None, None)]
    });
    let r = run_for(1.0, FakeGuest::new(Vec::new()), peer, true);
    assert!(
        matches!(&r.end, End::Protocol(m) if m.starts_with("console_rx without --console-port usb, on a UART0 with pins: ")),
        "{:?}",
        r.end
    );
    // and no console_rx_lost in stats without it
    assert!(r.peer.stats().get("console_rx_lost").is_none());
}

#[test]
fn under_no_bt_the_guests_call_to_its_bluetooth_controller_ends_the_run_at_the_slice_end() {
    let ms = cycles(MS);
    let mut script = tx(ms, 0, b"ab", 100);
    script.extend([
        (ms + 300, Act::BtInit),
        // what the guest does after the call, in the same slice, still goes out; nothing later
        (ms + 400, Act::Tx(0, b'c')),
        (3 * ms, Act::Tx(0, b'd')),
    ]);
    let guest = FakeGuest::new(script)
        .with_routes(&[route(0, Some(1), Some(3), Some(115_200))])
        .with_no_bt();
    let r = run_for(1.0, guest, FakePeer::new(&[], idle(None, None)), true);
    assert!(matches!(r.end, End::BtController), "{:?}", r.end);
    assert_eq!(r.end.exit_code(), 5);
    let console: Vec<u8> = r.peer.of("console").iter().flat_map(|m| data(m)).collect();
    assert_eq!(console, b"abc");
    let stats = r.peer.msgs.last().expect("stats");
    assert_eq!(kind(stats), "stats");
    assert_eq!(
        stats.get("end").and_then(Json::as_str),
        Some("bt_controller")
    );
    // the end of the slice the call came in: the next whole millisecond
    assert_eq!(num(stats, "t"), ns(2 * ms));
}

#[test]
fn under_no_bt_a_guest_that_never_starts_its_controller_runs_to_the_end() {
    let guest = FakeGuest::new(tx(cycles(MS), 0, b"a", 100))
        .with_routes(&[route(0, Some(1), Some(3), Some(115_200))])
        .with_no_bt();
    let r = run_for(0.01, guest, FakePeer::new(&[], idle(None, None)), true);
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
}
