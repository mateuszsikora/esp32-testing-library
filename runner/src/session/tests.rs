//! The run loop against a scripted guest and a fake peer: the arithmetic of slices, block points
//! and pacing, the flush points, and the rules of `docs/protocol.md` the runner enforces.
use super::*;
use crate::socket_board::{Recorder, SocketBoard};
use esp_soc::BoardModel;
use esp_soc::json::{Json, parse_json};
use std::cell::RefCell;
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
    /// The guest reads its RX FIFO empty.
    Drain(usize),
    /// The guest writes `GPIO_ENABLE` and `GPIO_OUT`, and the pads follow them.
    Drive(u64, u64),
    /// The hook reports these pad changes with `GPIO_ENABLE` and `GPIO_OUT` as given: a pad a
    /// peripheral drives through the GPIO matrix, whatever the two words say.
    Pads(Vec<(u8, bool)>, u64, u64),
    Fault,
    /// The SD card reports an operation its rules acted on, as `SocketBoard` logs it.
    SdIo(sd_card::Io),
    /// The ids of the SD card's rules in force are these.
    SdRules(Vec<u64>),
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
    /// Where each slice was asked to end.
    slices: Vec<u64>,
    /// The cycles of each power-on reset.
    power_resets: Vec<u64>,
    /// Where each slice with the supply off ended.
    idles: Vec<u64>,
    /// The SD card of `--sd-rw`.
    card: Option<sd_card::Shared>,
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
            slices: Vec::new(),
            power_resets: Vec::new(),
            idles: Vec::new(),
            card: None,
        }
    }

    /// With an SD card of 1 MiB, 2048 blocks.
    fn with_card(mut self) -> Self {
        let wiring = sd_card::Wiring {
            sclk: 14,
            mosi: 15,
            miso: 2,
            cs: 13,
        };
        let store = esp_periph::sdmmc::MemStore(vec![0; 2 * sd_card::UNIT as usize]);
        let card = sd_card::SdCard::new(wiring, Box::new(store)).unwrap();
        self.card = Some(Rc::new(RefCell::new(card)));
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
                Act::SdIo(io) => self
                    .shared
                    .borrow_mut()
                    .log
                    .push(Recorded::SdIo(sd_card::Io { cycle: at, ..io })),
                Act::SdRules(ids) => {
                    let card = self.card.as_ref().expect("a card").borrow();
                    let rules: Vec<u64> = card.rules().iter().map(|r| r.id).collect();
                    assert_eq!(rules, ids, "the rules at cycle {at}");
                }
            }
        }
        self.now = end;
        Stop::Halted
    }
    fn chip_reset(&mut self) -> u32 {
        let Some((_, Act::Reset(cause))) = self.script.pop_front() else {
            panic!("a reset with none in the script");
        };
        self.routes = std::array::from_fn(|port| UartRoute::new(port, 0, None, None));
        self.fifo = Default::default();
        self.drive = (0, 0);
        cause
    }
    fn power_on_reset(&mut self, _rom: &[u8]) -> u32 {
        self.power_resets.push(self.now);
        self.routes = std::array::from_fn(|port| UartRoute::new(port, 0, None, None));
        self.fifo = Default::default();
        self.drive = (0, 0);
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
    fn uart_input(&mut self, pin: u8, baud: u32, byte: u8) {
        for port in 0..PORTS {
            let r = self.routes[port];
            if r.rx_pin == Some(pin) && r.matches_baud(baud) && self.fifo[port].len() < RX_FIFO {
                self.fifo[port].push((self.now, byte));
            }
        }
    }
    fn gpio_drive(&self) -> (u64, u64) {
        self.drive
    }
    fn take_gpio_inputs(&mut self) {
        for edge in self.board.take_edges() {
            self.inputs.push((self.now, edge.pin, Some(edge.level)));
        }
        for pin in self.board.released_inputs() {
            self.inputs.push((self.now, pin, None));
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
}

const SIGNAL: &str = "SIGNAL";

impl FakePeer {
    fn new(watch: &[u8], respond: impl FnMut(&Turn) -> Vec<String> + 'static) -> Self {
        let watch: Vec<String> = watch.iter().map(u8::to_string).collect();
        Self::with_hello(
            &format!(
                r#"{{"type":"hello","version":1,"chip":"esp32","board":"esp32-devkitc","watch":[{}]}}"#,
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
    fn recv(&mut self, _deadline: Option<Instant>) -> Result<String, LinkError> {
        match self.queue.pop_front() {
            Some(line) if line == SIGNAL => Err(LinkError::Signal),
            Some(line) => Ok(line),
            None => Err(LinkError::Gone(
                "the fake peer has nothing more to say".into(),
            )),
        }
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

fn run_for(seconds: f64, mut guest: FakeGuest, mut peer: FakePeer, console: bool) -> Run {
    let signal = AtomicBool::new(false);
    let shared = guest.shared.clone();
    let card = guest.card.clone();
    let opts = Options {
        end_cycle: (seconds * SECOND as f64) as u64,
        console,
        peer_timeout: None,
        signal: &signal,
        wifi: None,
        rom: &[],
        card,
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
            r#"{"type":"hello","version":2,"chip":"esp32"}"#,
            "version 2",
        ),
        (
            r#"{"type":"hello","version":1,"chip":"esp32s3"}"#,
            "chip \"esp32s3\"",
        ),
        (r#"{"type":"wait","ack":0}"#, "expected hello"),
        // pins the classic chip does not have, though the parser takes any below 64
        (
            r#"{"type":"hello","version":1,"chip":"esp32","watch":[4,24]}"#,
            "watch holds GPIO 24, which the esp32 does not have",
        ),
        (
            r#"{"type":"hello","version":1,"chip":"esp32","watch":[28]}"#,
            "GPIO 28",
        ),
        (
            r#"{"type":"hello","version":1,"chip":"esp32","watch":[40]}"#,
            "GPIO 40",
        ),
        (
            r#"{"type":"hello","version":1,"chip":"esp32","watch":[63]}"#,
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
fn a_reset_is_forwarded_with_its_cause_and_the_reset_routes() {
    let at = cycles(2 * MS) + 5;
    let guest = FakeGuest::new(vec![
        (cycles(MS), Act::Tx(1, b'x')),
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
    let after_reset = r.peer.of("uart_cfg")[1];
    assert_eq!(after_reset.get("baud"), Some(&Json::Null));
    assert_eq!(after_reset.get("rx_pin"), Some(&Json::Null));
    assert_eq!(r.stats.resets, 1);
    assert_eq!(num(r.peer.stats(), "resets"), 1);
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
            vec![r#"{"type":"hello","version":1,"chip":"esp32"}"#.to_string()],
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
        fn uart_input(&mut self, pin: u8, baud: u32, byte: u8) {
            self.0.uart_input(pin, baud, byte)
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
        peer_timeout: None,
        signal: &signal,
        wifi: None,
        rom: &[],
        card: None,
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
        fn uart_input(&mut self, pin: u8, baud: u32, byte: u8) {
            Guest::uart_input(&mut self.m, pin, baud, byte)
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
        peer_timeout: None,
        signal: &signal,
        wifi: None,
        rom: &[],
        card: None,
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
        peer_timeout: None,
        signal: &signal,
        wifi: None,
        rom: &[],
        card: None,
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

/// A guest with a Wi-Fi station: it sends the scripted Ethernet frames at their cycles into the
/// relay, and its link takes at most `room` frames between two slices, as esp32sim's queue of 64
/// does, handing one to the station per slice.
struct WifiGuest {
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
    fn uart_input(&mut self, pin: u8, baud: u32, byte: u8) {
        self.inner.uart_input(pin, baud, byte)
    }
    fn gpio_drive(&self) -> (u64, u64) {
        self.inner.gpio_drive()
    }
    fn take_gpio_inputs(&mut self) {
        self.inner.take_gpio_inputs()
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
    let signal = AtomicBool::new(false);
    let wifi = cli::Wifi {
        spec: "ssid=test".into(),
        nearby: Vec::new(),
    };
    let opts = Options {
        end_cycle: end,
        console: false,
        peer_timeout: None,
        signal: &signal,
        wifi: Some(&wifi),
        rom: &[],
        card: None,
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
    let wifi = cli::Wifi {
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
    let guest = FakeGuest::new(vec![]).with_routes(&[modem(115_200)]);
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
    assert_eq!(r.guest.power_resets, [on]);
    assert_eq!(r.stats.resets, 1);
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

fn sd_fault(t: u64, id: u64, extra: &str) -> String {
    format!(r#"{{"type":"sd_fault","t":{t},"id":{id},"op":"write","fail":true{extra}}}"#)
}

#[test]
fn sd_faults_reach_the_card_when_due_and_what_they_did_goes_out_in_t_order() {
    let mut first = true;
    let peer = FakePeer::new(&[GNSS_TX], move |turn| {
        let mut out = Vec::new();
        if std::mem::take(&mut first) {
            out.push(sd_fault(2 * MS, 1, ""));
            out.push(sd_fault(
                2 * MS,
                2,
                r#","op":"read","fail":false,"delay":5000"#,
            ));
            out.push(r#"{"type":"sd_clear","t":4000000,"ids":[1]}"#.to_string());
            out.push(r#"{"type":"sd_clear","t":6000000}"#.to_string());
        }
        out.push(wait(turn.seq, Some(turn.t + MS), None));
        out
    });
    let io = sd_card::Io {
        cycle: 0,
        began: cycles(2 * MS),
        op: sd_card::Op::Write,
        block: 8,
        blocks: 1,
        rules: vec![1],
        failed: true,
        delay: 0,
        written: false,
    };
    let mut script = vec![
        (cycles(MS), Act::SdRules(vec![])),
        (cycles(3 * MS), Act::SdRules(vec![1, 2])),
        (cycles(3 * MS) + 1, Act::SdIo(io)),
        (cycles(5 * MS), Act::SdRules(vec![2])),
        (cycles(7 * MS), Act::SdRules(vec![])),
    ];
    script.extend(tx(cycles(3 * MS), 1, b"a", 1));
    script.extend(tx(cycles(3 * MS) + 2, 1, b"b", 1));
    let guest = FakeGuest::new(script)
        .with_routes(&[route(1, Some(GNSS_TX), None, Some(9600))])
        .with_card();
    let r = run(0.01, guest, peer);
    assert!(matches!(r.end, End::MaxSeconds), "{:?}", r.end);
    let sd_io = r.peer.of("sd_io");
    assert_eq!(sd_io.len(), 1);
    let line = sd_io[0];
    assert_eq!(num(line, "t"), ns(cycles(3 * MS) + 1));
    assert_eq!(num(line, "began"), 2 * MS);
    assert_eq!(line.get("op").and_then(Json::as_str), Some("write"));
    assert_eq!((num(line, "block"), num(line, "blocks")), (8, 1));
    assert_eq!(line.get("failed"), Some(&Json::Bool(true)));
    assert_eq!(line.get("written"), Some(&Json::Bool(false)));
    // not an event: no seq, and it ends the uart_tx it lands in, which goes out first
    assert!(line.get("seq").is_none());
    let uart: Vec<Vec<u8>> = r.peer.of("uart_tx").iter().map(|m| data(m)).collect();
    assert_eq!(uart, [b"a".to_vec(), b"b".to_vec()]);
}

#[test]
fn the_runner_refuses_sd_faults_it_cannot_take() {
    for (lines, card, found) in [
        (vec![sd_fault(0, 1, "")], false, "sd_fault without --sd-rw"),
        (
            vec![r#"{"type":"sd_clear","t":0}"#.to_string()],
            false,
            "sd_clear without --sd-rw",
        ),
        (
            vec![sd_fault(0, 2, ""), sd_fault(0, 2, "")],
            true,
            "id 2 is not above 2",
        ),
        (
            vec![sd_fault(0, 1, r#","first":0,"last":2048"#)],
            true,
            "block 2048 is past the card's last, 2047",
        ),
        (
            vec![r#"{"type":"sd_clear","t":0,"ids":[1]}"#.to_string()],
            true,
            "id 1 is no sd_fault's",
        ),
    ] {
        let mut lines = Some(lines);
        let peer = FakePeer::new(&[GNSS_RX], move |turn| {
            let mut out = lines.take().expect("one turn");
            out.push(wait(turn.seq, None, None));
            out
        });
        let guest = FakeGuest::new(vec![]);
        let guest = if card { guest.with_card() } else { guest };
        let r = run(0.01, guest, peer);
        let End::Protocol(detail) = &r.end else {
            panic!("{found}: {:?}", r.end)
        };
        assert!(detail.contains(found), "{detail}");
    }
}
