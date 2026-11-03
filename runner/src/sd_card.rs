//! An SD memory card in SPI mode on four of the chip's pins (plan M7), after the SD Physical Layer
//! Simplified Specification (SD spec below), chapter 7. The chip's SPI master hands each
//! transaction to the board with its routes (`BoardModel::spi_transfer_pins`); the card sees the
//! bytes when its SCLK is routed and its CS low, and its answer reaches the chip when the master's
//! MISO is routed from the card's pin. A transaction is full duplex: the card's byte for each byte
//! clocked in comes out on the same clock, so its answer to a command starts one byte after the
//! command's last.
//!
//! It is a high capacity card (CSD version 2.0, block addressing, 512-byte blocks) of the image's
//! size, as esp32sim's own card (`esp_periph::sdmmc::SdCard`) is; that one speaks the SD bus, not
//! SPI. It answers at once: no busy time after a write or an erase, no wait before a data block
//! beyond one byte. Without CMD6 (CCC without class 10) the host stays at default speed, and
//! there is no locking, write protection or CMD23.
//!
//! The peer's fault rules (`sd_fault`, protocol 5.19) change that for the reads, writes and erases
//! they match: a read answers with an error token, a write with a write error and leaves its block
//! as it was, and a delay holds the card's answer on the bus's clock, which the board hands the
//! card as `now`: a read sends 0xff until its data block is ready, a write or an erase holds MISO
//! low (busy) and takes nothing in until it is done. Each operation a rule acted on is kept in
//! `io` for the peer (`sd_io`).
use esp_periph::sdmmc::SdStore;
use esp_periph::sdmmc::card::BLOCK;
use esp_soc::board::{BoardModel, SpiPins};
use std::cell::RefCell;
use std::collections::VecDeque;
use std::rc::Rc;

/// The card's GPIOs.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Wiring {
    pub sclk: u8,
    pub mosi: u8,
    pub miso: u8,
    pub cs: u8,
}

impl std::fmt::Display for Wiring {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "SCLK GPIO {}, MOSI GPIO {}, MISO GPIO {}, CS GPIO {}",
            self.sclk, self.mosi, self.miso, self.cs
        )
    }
}

/// The capacity unit of CSD version 2.0's C_SIZE (SD spec §5.3.3).
pub const UNIT: u64 = 512 << 10;

/// R1 bits (SD spec §7.3.2.1).
const IDLE: u8 = 1 << 0;
const ILLEGAL: u8 = 1 << 2;
const COM_CRC: u8 = 1 << 3;
const ERASE_SEQ: u8 = 1 << 4;
const PARAMETER: u8 = 1 << 6;

/// Tokens (SD spec §7.3.3).
const START_BLOCK: u8 = 0xfe;
const START_MULTI_WRITE: u8 = 0xfc;
const STOP_TRAN: u8 = 0xfd;
const DATA_ACCEPTED: u8 = 0x05;
const DATA_CRC_ERROR: u8 = 0x0b;
const DATA_WRITE_ERROR: u8 = 0x0d;
/// Data error tokens: card controller error, card ECC failed (a fault rule's read error), out
/// of range.
const ERROR_CC: u8 = 0x02;
const ERROR_ECC: u8 = 0x04;
const ERROR_RANGE: u8 = 0x08;
/// What the card sends while it is busy (SD spec §7.2.4).
const BUSY: u8 = 0x00;

/// OCR (SD spec §5.1): 2.7-3.6 V, card capacity status, power-up done.
const OCR_VOLTAGE: u32 = 0x00ff_8000;
const OCR_CCS: u32 = 1 << 30;
const OCR_READY: u32 = 1 << 31;
/// CSD CCC: classes 0, 2, 4, 5 and 8 (basic, block read, block write, erase, application
/// specific), the ones this card implements.
const CCC: u128 = 0x135;
/// CSD TRAN_SPEED: 25 MHz.
const TRAN_SPEED: u128 = 0x32;
/// ACMD41s answered idle before power-up is done, as esp32sim's card has it.
const INIT_POLLS: u32 = 1;

/// What the run did with the card, for the line at the end of the run.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Stats {
    pub commands: u64,
    pub blocks_read: u64,
    pub blocks_written: u64,
    /// Reads and writes of the image that failed.
    pub errors: u64,
    /// Operations a fault rule failed, and operations one delayed.
    pub failed: u64,
    pub delayed: u64,
}

/// What a fault rule acts on: a block read (CMD17, each block of CMD18), a block write (CMD24,
/// each block of CMD25) or an erase (CMD38).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Op {
    Read,
    Write,
    Erase,
}

impl Op {
    pub fn parse(name: &str) -> Option<Op> {
        match name {
            "read" => Some(Op::Read),
            "write" => Some(Op::Write),
            "erase" => Some(Op::Erase),
            _ => None,
        }
    }

    pub fn name(self) -> &'static str {
        match self {
            Op::Read => "read",
            Op::Write => "write",
            Op::Erase => "erase",
        }
    }
}

/// The longest delay a rule may give, in nanoseconds: 10 s, twice ESP-IDF's write timeout.
pub const MAX_DELAY: u64 = 10_000_000_000;

/// A fault rule of the peer's (`sd_fault`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Rule {
    pub id: u64,
    pub op: Op,
    /// Whether the operations it acts on fail.
    pub fail: bool,
    /// How long the card holds its answer to them, in nanoseconds and in bus cycles.
    pub delay_ns: u64,
    pub delay: u64,
    /// The first and last block of the operations it matches; `None` for any block. An erase
    /// matches when its blocks overlap them.
    pub blocks: Option<(u64, u64)>,
    /// Matching operations it lets through before it acts.
    pub skip: u64,
    /// Operations it acts on before it is used up; `None` for every one until it is cleared.
    pub count: Option<u64>,
}

/// An operation one or more rules acted on, for `sd_io`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Io {
    /// When it began: the command (a read, an erase), the block's CRC (a write), or the end of
    /// the block before it (a later block of CMD18).
    pub cycle: u64,
    pub op: Op,
    pub block: u64,
    pub blocks: u64,
    /// The rules that acted on it, in the order they were given.
    pub rules: Vec<u64>,
    pub failed: bool,
    /// How long the card held its answer, in nanoseconds: the longest of the rules' delays.
    pub delay: u64,
    /// Whether the image was written: a write that went through, an erase.
    pub written: bool,
}

/// What the rules do to one operation.
#[derive(Debug, Default)]
struct Verdict {
    rules: Vec<u64>,
    fail: bool,
    delay_ns: u64,
    delay: u64,
}

/// One item of what goes out on MISO.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Out {
    Byte(u8),
    /// `fill` until the bus reaches `until`: a delay. While `fill` is [`BUSY`] the card takes in
    /// nothing.
    Hold {
        until: u64,
        fill: u8,
    },
}

/// What the card does with the bytes that come in.
#[derive(Debug, PartialEq, Eq)]
enum Phase {
    /// Looking for a command; the bytes already queued go out meanwhile.
    Command,
    /// A multiple block read (CMD18): the block at `next` follows the one queued.
    Reading { next: u64 },
    /// Waiting for a write's start token, the block to come at `lba`.
    Awaiting { lba: u64, multi: bool },
    /// A block and its CRC coming in.
    Receiving { lba: u64, multi: bool, buf: Vec<u8> },
}

pub struct SdCard {
    pub wiring: Wiring,
    store: Box<dyn SdStore>,
    blocks: u64,
    serial: u32,
    /// In SPI mode: a CMD0 came with CS low (SD spec §7.2.1). Until then the card answers nothing.
    spi: bool,
    /// Initialisation (ACMD41) is done.
    ready: bool,
    init_left: u32,
    /// CMD8 was accepted since the last CMD0: the host may ask for a high capacity card.
    if_cond: bool,
    /// The next command is an application command (CMD55).
    app_cmd: bool,
    /// CRC checking, on with CMD59 (SD spec §7.2.2).
    crc: bool,
    erase: (Option<u64>, Option<u64>),
    /// Blocks written by the last CMD24 or CMD25 (ACMD22).
    written: u32,
    phase: Phase,
    /// The command coming in, from its start byte.
    command: Vec<u8>,
    /// What goes out on MISO next; 0xff when empty.
    out: VecDeque<Out>,
    /// The bus cycle, as the board last saw it: the time of the transaction coming in.
    pub now: u64,
    /// The peer's fault rules in force, in the order given.
    rules: Vec<Rule>,
    /// The operations the rules acted on, until the board takes them.
    pub io: Vec<Io>,
    pub stats: Stats,
}

pub type Shared = Rc<RefCell<SdCard>>;

impl SdCard {
    /// A card on `wiring` holding `store`, which must be a whole number of 512 KiB units, from 1
    /// to 2^22 (2 TiB).
    pub fn new(wiring: Wiring, store: Box<dyn SdStore>) -> Result<Self, String> {
        let len = store.len();
        if len == 0 || !len.is_multiple_of(UNIT) {
            return Err(format!(
                "an SD card image is a whole number of 512 KiB units (CSD C_SIZE), this one has \
                 {len} bytes"
            ));
        }
        if len / UNIT > 1 << 22 {
            return Err("an SD card image is at most 2 TiB (CSD C_SIZE)".into());
        }
        let blocks = len / BLOCK as u64;
        Ok(SdCard {
            wiring,
            store,
            blocks,
            // esp32sim's card's: from the image's size, so cards of two sizes read as two cards
            serial: (blocks as u32).wrapping_mul(0x9e37_79b9) ^ 0x5d1c_0b17,
            spi: false,
            ready: false,
            init_left: INIT_POLLS,
            if_cond: false,
            app_cmd: false,
            crc: false,
            erase: (None, None),
            written: 0,
            phase: Phase::Command,
            command: Vec::with_capacity(6),
            out: VecDeque::new(),
            now: 0,
            rules: Vec::new(),
            io: Vec::new(),
            stats: Stats::default(),
        })
    }

    /// The image's size in bytes.
    pub fn len(&self) -> u64 {
        self.blocks * BLOCK as u64
    }

    /// The board's supply went off: the card is back where it was when it was inserted, out of
    /// SPI mode and uninitialised, and a block it had not received whole is lost. What it wrote
    /// is in the image already: a block goes there whole once its CRC is in.
    pub fn power_off(&mut self) {
        self.spi = false;
        self.ready = false;
        self.init_left = INIT_POLLS;
        self.if_cond = false;
        self.app_cmd = false;
        self.crc = false;
        self.erase = (None, None);
        self.written = 0;
        self.phase = Phase::Command;
        self.command.clear();
        self.out.clear();
    }

    /// The number of blocks, for the peer's rules.
    pub fn blocks(&self) -> u64 {
        self.blocks
    }

    /// From now on `rule` acts on the operations that begin, after those it skips.
    pub fn add_rule(&mut self, rule: Rule) {
        self.rules.push(rule);
    }

    /// The rules in force, for the run loop's tests.
    #[cfg(test)]
    pub fn rules(&self) -> &[Rule] {
        &self.rules
    }

    /// Clears the rules of `ids`, or every rule. A delay already running runs out.
    pub fn clear_rules(&mut self, ids: Option<&[u64]>) {
        match ids {
            Some(ids) => self.rules.retain(|r| !ids.contains(&r.id)),
            None => self.rules.clear(),
        }
    }

    /// What the rules do to an operation `op` on blocks `first` to `last`: every rule that
    /// matches it skips it while it has some to skip, else acts on it and counts it.
    fn verdict(&mut self, op: Op, first: u64, last: u64) -> Verdict {
        let mut v = Verdict::default();
        for rule in &mut self.rules {
            let matches =
                rule.op == op && rule.blocks.is_none_or(|(lo, hi)| first <= hi && lo <= last);
            if !matches {
                continue;
            }
            if rule.skip > 0 {
                rule.skip -= 1;
                continue;
            }
            v.rules.push(rule.id);
            v.fail |= rule.fail;
            if rule.delay > v.delay {
                v.delay = rule.delay;
                v.delay_ns = rule.delay_ns;
            }
            if let Some(count) = &mut rule.count {
                *count -= 1;
            }
        }
        self.rules.retain(|r| r.count != Some(0));
        v
    }

    /// Keeps an operation the rules acted on for the peer; `cycle` is when it began.
    fn report(
        &mut self,
        cycle: u64,
        op: Op,
        (block, blocks): (u64, u64),
        v: &Verdict,
        written: bool,
    ) {
        if v.rules.is_empty() {
            return;
        }
        if v.fail {
            self.stats.failed += 1;
        }
        if v.delay > 0 {
            self.stats.delayed += 1;
        }
        self.io.push(Io {
            cycle,
            op,
            block,
            blocks,
            rules: v.rules.clone(),
            failed: v.fail,
            delay: v.delay_ns,
            written,
        });
    }

    fn push(&mut self, byte: u8) {
        self.out.push_back(Out::Byte(byte));
    }

    fn extend(&mut self, bytes: impl IntoIterator<Item = u8>) {
        self.out.extend(bytes.into_iter().map(Out::Byte));
    }

    /// `fill` on MISO for `delay` bus cycles from now, before what is queued after it.
    fn hold(&mut self, delay: u64, fill: u8) {
        if delay > 0 {
            self.out.push_back(Out::Hold {
                until: self.now.saturating_add(delay),
                fill,
            });
        }
    }

    /// One SPI transaction on the chip's routes `pins`: `tx` out on MOSI, `rx_len` bytes back
    /// from MISO, all 0xff from a card that is not selected or not on the master's MISO.
    pub fn transfer(&mut self, pins: SpiPins, tx: &[u8], rx_len: usize) -> Vec<u8> {
        let w = self.wiring;
        let mut rx = vec![0xff; rx_len];
        if pins.sclk & 1 << w.sclk == 0 || pins.cs & 1 << w.cs == 0 {
            return rx;
        }
        // MOSI not routed to the card: its line idles high
        let mosi = pins.mosi & 1 << w.mosi != 0;
        let miso = pins.miso == Some(w.miso);
        for i in 0..tx.len().max(rx_len) {
            let byte = if mosi {
                tx.get(i).copied().unwrap_or(0xff)
            } else {
                0xff
            };
            let answer = self.exchange(byte);
            if let Some(slot) = rx.get_mut(i).filter(|_| miso) {
                *slot = answer;
            }
        }
        rx
    }

    /// One byte each way: what the card sends while `mosi` comes in.
    fn exchange(&mut self, mosi: u8) -> u8 {
        if self.out.is_empty() {
            self.next_block();
        }
        let miso = loop {
            match self.out.front() {
                None => break 0xff,
                Some(&Out::Byte(byte)) => {
                    self.out.pop_front();
                    break byte;
                }
                Some(&Out::Hold { until, fill }) if self.now < until => {
                    // busy, the card rejects whatever comes in (SD spec §7.2.4)
                    if fill == BUSY {
                        return fill;
                    }
                    break fill;
                }
                Some(Out::Hold { .. }) => {
                    self.out.pop_front();
                    if self.out.is_empty() {
                        self.next_block();
                    }
                }
            }
        };
        match &mut self.phase {
            Phase::Awaiting { lba, multi } => {
                let (lba, multi) = (*lba, *multi);
                // a token's top bits are 11, a command's 01
                match mosi {
                    START_BLOCK if !multi => self.receive(lba, multi),
                    START_MULTI_WRITE if multi => self.receive(lba, multi),
                    STOP_TRAN if multi => self.phase = Phase::Command,
                    _ => self.command_byte(mosi),
                }
            }
            Phase::Receiving { lba, multi, buf } => {
                buf.push(mosi);
                if buf.len() == BLOCK + 2 {
                    let (lba, multi, buf) = (*lba, *multi, std::mem::take(buf));
                    self.block_in(lba, multi, &buf);
                }
            }
            Phase::Command | Phase::Reading { .. } => self.command_byte(mosi),
        }
        miso
    }

    fn receive(&mut self, lba: u64, multi: bool) {
        self.phase = Phase::Receiving {
            lba,
            multi,
            buf: Vec::with_capacity(BLOCK + 2),
        };
    }

    /// A block written, with its CRC: the data response, the busy time of a rule's delay, then
    /// the next block of a CMD25. A block a rule fails is not written.
    fn block_in(&mut self, lba: u64, multi: bool, buf: &[u8]) {
        let (data, crc) = buf.split_at(BLOCK);
        let mut verdict = Verdict::default();
        let response = if self.crc && crc16(data) != u16::from_be_bytes([crc[0], crc[1]]) {
            DATA_CRC_ERROR
        } else if lba >= self.blocks {
            DATA_WRITE_ERROR
        } else {
            verdict = self.verdict(Op::Write, lba, lba);
            if verdict.fail {
                DATA_WRITE_ERROR
            } else if let Err(e) = self.store.write(lba, data.try_into().unwrap()) {
                self.stats.errors += 1;
                eprintln!("esp32tl-runner: SD card: writing block {lba}: {e}");
                DATA_WRITE_ERROR
            } else {
                self.stats.blocks_written += 1;
                self.written += 1;
                DATA_ACCEPTED
            }
        };
        let written = response == DATA_ACCEPTED;
        self.report(self.now, Op::Write, (lba, 1), &verdict, written);
        self.push(response);
        self.hold(verdict.delay, BUSY);
        // after an error a multiple block write waits for its stop token (SD spec §7.3.3.1)
        self.phase = match (multi, response) {
            (true, DATA_ACCEPTED) => Phase::Awaiting {
                lba: lba + 1,
                multi,
            },
            (true, _) => Phase::Awaiting { lba, multi },
            (false, _) => Phase::Command,
        };
    }

    /// The next block of a CMD18, once the one before is out.
    fn next_block(&mut self) {
        let Phase::Reading { next } = self.phase else {
            return;
        };
        if next >= self.blocks {
            self.extend([0xff, ERROR_RANGE]);
            self.phase = Phase::Command;
            return;
        }
        self.phase = Phase::Reading { next: next + 1 };
        self.read_block(next);
    }

    /// The block at `lba` as a data block, after one byte of access time and a rule's delay; an
    /// error token when a rule fails the read or the image cannot be read.
    fn read_block(&mut self, lba: u64) {
        let verdict = self.verdict(Op::Read, lba, lba);
        self.report(self.now, Op::Read, (lba, 1), &verdict, false);
        self.hold(verdict.delay, 0xff);
        if verdict.fail {
            self.extend([0xff, ERROR_ECC]);
            self.phase = Phase::Command;
            return;
        }
        let mut block = [0; BLOCK];
        match self.store.read(lba, &mut block) {
            Ok(()) => {
                self.stats.blocks_read += 1;
                self.data_block(&block);
            }
            Err(e) => {
                self.stats.errors += 1;
                eprintln!("esp32tl-runner: SD card: reading block {lba}: {e}");
                self.extend([0xff, ERROR_CC]);
                self.phase = Phase::Command;
            }
        }
    }

    /// `data` as a data block: a byte of access time, the start token, the data, its CRC16.
    fn data_block(&mut self, data: &[u8]) {
        self.extend([0xff, START_BLOCK]);
        self.extend(data.iter().copied());
        self.extend(crc16(data).to_be_bytes());
    }

    /// A byte towards a command: a command starts with 01 in its top bits (SD spec §7.3.1.1).
    fn command_byte(&mut self, mosi: u8) {
        if self.command.is_empty() && mosi & 0xc0 != 0x40 {
            return;
        }
        self.command.push(mosi);
        if self.command.len() == 6 {
            let command: [u8; 6] = std::mem::take(&mut self.command).try_into().unwrap();
            self.command(command);
        }
    }

    fn command(&mut self, frame: [u8; 6]) {
        let index = frame[0] & 0x3f;
        let arg = u32::from_be_bytes([frame[1], frame[2], frame[3], frame[4]]);
        let crc_ok = crc7(&frame[..5]) == frame[5] >> 1;
        // In SD mode until a CMD0, whose CRC is always checked.
        if !self.spi {
            if index == 0 && crc_ok {
                self.spi = true;
            } else {
                return;
            }
        }
        self.stats.commands += 1;
        let app = std::mem::take(&mut self.app_cmd);
        // A command ends what the card was sending, a read in progress included (SD spec
        // §7.2.3), and a write that never got its token.
        self.out.clear();
        self.phase = Phase::Command;
        // the command response time, one byte (SD spec §7.5.4)
        self.push(0xff);
        // CMD0 and CMD8 are always checked, the others once CMD59 turns checking on.
        if !crc_ok && (self.crc || index == 0 || index == 8) {
            self.r1(COM_CRC);
            return;
        }
        if app && self.app(index, arg) {
            return;
        }
        self.standard(index, arg);
    }

    /// R1 with the idle bit while the card initialises.
    fn r1(&mut self, bits: u8) {
        let idle = if self.ready { 0 } else { IDLE };
        self.push(bits | idle);
    }

    /// An application command, after CMD55; false for an index the card does not define, which
    /// is taken as the standard command (SD spec §4.3.9.1).
    fn app(&mut self, index: u8, arg: u32) -> bool {
        match index {
            // ACMD41 SD_SEND_OP_COND: a high capacity card stays idle for a host without HCS
            // or without CMD8 (SD spec §4.2.3)
            41 => {
                if self.if_cond && arg & OCR_CCS != 0 && !self.ready {
                    if self.init_left == 0 {
                        self.ready = true;
                    } else {
                        self.init_left -= 1;
                    }
                }
                self.r1(0);
            }
            _ if !self.ready => self.r1(ILLEGAL),
            // ACMD13 SD_STATUS: R2, then the status as a data block
            13 => {
                self.r1(0);
                self.push(0);
                self.data_block(&ssr());
            }
            // ACMD22 SEND_NUM_WR_BLOCKS
            22 => {
                self.r1(0);
                self.data_block(&self.written.to_be_bytes());
            }
            // ACMD23 SET_WR_BLK_ERASE_COUNT (a pre-erase hint), ACMD42 SET_CLR_CARD_DETECT
            23 | 42 => self.r1(0),
            // ACMD51 SEND_SCR
            51 => {
                self.r1(0);
                self.data_block(&scr(self.sdxc()));
            }
            _ => return false,
        }
        true
    }

    fn standard(&mut self, index: u8, arg: u32) {
        match index {
            // CMD0 GO_IDLE_STATE
            0 => {
                self.reset();
                self.r1(0);
            }
            // CMD8 SEND_IF_COND: R7, 2.7-3.6 V accepted, the check pattern echoed
            8 => {
                let accepted = arg >> 8 & 0xf == 1;
                self.if_cond = accepted;
                self.r1(0);
                let vca = if accepted { 1 } else { 0 };
                self.extend([0, 0, vca, arg as u8]);
            }
            // CMD55 APP_CMD
            55 => {
                self.app_cmd = true;
                self.r1(0);
            }
            // CMD58 READ_OCR: R3
            58 => {
                self.r1(0);
                let ocr = if self.ready {
                    OCR_READY | OCR_CCS | OCR_VOLTAGE
                } else {
                    OCR_VOLTAGE
                };
                self.extend(ocr.to_be_bytes());
            }
            // CMD59 CRC_ON_OFF
            59 => {
                self.crc = arg & 1 != 0;
                self.r1(0);
            }
            // Only the commands above while the card initialises (SD spec §7.2.1).
            _ if !self.ready => self.r1(ILLEGAL),
            // CMD9 SEND_CSD, CMD10 SEND_CID: R1, then the register as a data block
            9 => {
                self.r1(0);
                self.data_block(&self.csd().to_be_bytes());
            }
            10 => {
                self.r1(0);
                self.data_block(&self.cid().to_be_bytes());
            }
            // CMD12 STOP_TRANSMISSION: a stuff byte, then R1b (SD spec §7.3.1.3)
            12 => {
                self.push(0xff);
                self.r1(0);
            }
            // CMD13 SEND_STATUS: R2
            13 => {
                self.r1(0);
                self.push(0);
            }
            // CMD16 SET_BLOCKLEN: blocks are 512 bytes on a high capacity card regardless
            16 => self.r1(0),
            // CMD17 READ_SINGLE_BLOCK, CMD18 READ_MULTIPLE_BLOCK: block addresses
            17 | 18 => {
                let lba = u64::from(arg);
                if lba >= self.blocks {
                    self.r1(PARAMETER);
                    return;
                }
                self.r1(0);
                if index == 18 {
                    self.phase = Phase::Reading { next: lba + 1 };
                }
                self.read_block(lba);
            }
            // CMD24 WRITE_BLOCK, CMD25 WRITE_MULTIPLE_BLOCK
            24 | 25 => {
                let lba = u64::from(arg);
                if lba >= self.blocks {
                    self.r1(PARAMETER);
                    return;
                }
                self.r1(0);
                self.written = 0;
                self.phase = Phase::Awaiting {
                    lba,
                    multi: index == 25,
                };
            }
            // CMD32 ERASE_WR_BLK_START_ADDR, CMD33 ERASE_WR_BLK_END_ADDR
            32 | 33 => {
                let lba = u64::from(arg);
                if lba >= self.blocks {
                    self.r1(PARAMETER);
                    return;
                }
                if index == 32 {
                    self.erase = (Some(lba), None);
                } else {
                    self.erase.1 = Some(lba);
                }
                self.r1(0);
            }
            // CMD38 ERASE: erased blocks read 0 (SCR DATA_STAT_AFTER_ERASE 0); R1b, busy for a
            // rule's delay
            38 => {
                let (Some(lo), Some(hi)) = std::mem::take(&mut self.erase) else {
                    self.r1(ERASE_SEQ);
                    return;
                };
                if hi < lo {
                    self.r1(PARAMETER);
                    return;
                }
                let verdict = self.verdict(Op::Erase, lo, hi);
                let written = match self.store.erase(lo, hi) {
                    Ok(()) => true,
                    Err(e) => {
                        self.stats.errors += 1;
                        eprintln!("esp32tl-runner: SD card: erasing blocks {lo} to {hi}: {e}");
                        false
                    }
                };
                self.report(self.now, Op::Erase, (lo, hi - lo + 1), &verdict, written);
                self.r1(0);
                self.hold(verdict.delay, BUSY);
            }
            _ => self.r1(ILLEGAL),
        }
    }

    /// CMD0: back to idle; SPI mode stays.
    fn reset(&mut self) {
        self.ready = false;
        self.init_left = INIT_POLLS;
        self.if_cond = false;
        self.crc = false;
        self.erase = (None, None);
    }

    /// Above 32 GiB the card is SDXC, below it SDHC.
    fn sdxc(&self) -> bool {
        self.blocks > 32 << 21
    }

    /// CID (SD spec §5.2), esp32sim's card's: manufacturer 0, OEM "ES", product "SIMSD",
    /// revision 1.0, made 2024-01.
    fn cid(&self) -> u128 {
        let mut v = u128::from(u16::from_be_bytes(*b"ES")) << 104;
        for (i, &c) in b"SIMSD".iter().enumerate() {
            v |= u128::from(c) << (96 - 8 * i);
        }
        let date = (2024 - 2000) << 4 | 1;
        v |= 0x10 << 56 | u128::from(self.serial) << 24 | date << 8;
        with_crc7(v)
    }

    /// CSD version 2.0 (SD spec §5.3.3): TAAC 1 ms, NSAC 0, 25 MHz, 512-byte blocks, C_SIZE
    /// from the image, erase by block, R2W_FACTOR 4.
    fn csd(&self) -> u128 {
        let c_size = u128::from(self.blocks / 1024 - 1);
        let v = 1 << 126
            | 0x0e << 112
            | TRAN_SPEED << 96
            | CCC << 84
            | 9 << 80
            | c_size << 48
            | 1 << 46
            | 0x7f << 39
            | 2 << 26
            | 9 << 22;
        with_crc7(v)
    }
}

/// The board of a run without `--board` that has a card: nothing on the chip's pins but it.
pub struct CardBoard(pub Shared);

impl BoardModel for CardBoard {
    fn name(&self) -> &'static str {
        "sd-card"
    }
    /// As esp32sim's `NoBoard`: no timed GPIO input.
    fn uses_gpio_edges(&self) -> bool {
        false
    }
    fn uses_spi_pins(&self) -> bool {
        true
    }
    fn spi_transfer_pins(&mut self, _host: u8, pins: SpiPins, tx: &[u8], rx_len: usize) -> Vec<u8> {
        self.0.borrow_mut().transfer(pins, tx, rx_len)
    }
}

/// SCR (SD spec §5.6): structure 1.0, SD_SPEC 2 with SD_SPEC3 (version 3.0x), security 2.00
/// (SDHC) or 3.00 (SDXC), 1- and 4-bit, no CMD23.
fn scr(sdxc: bool) -> [u8; 8] {
    let security = if sdxc { 4 } else { 3 };
    [0x02, security << 4 | 0x5, 0x80, 0, 0, 0, 0, 0]
}

/// SD status (SD spec §4.10.2): one line, speed class 10, AU 4 MiB; the erase estimate fields 0,
/// which tells the host to use its own timeout.
fn ssr() -> [u8; 64] {
    let mut s = [0; 64];
    // SPEED_CLASS [447:440], AU_SIZE [431:428]
    s[(511 - 447) / 8] = 0x04;
    s[(511 - 431) / 8] = 0x9 << 4;
    s
}

/// CID and CSD carry a CRC7 over bits 127:8 in bits 7:1 and a 1 in bit 0 (SD spec §5.2).
fn with_crc7(v: u128) -> u128 {
    (v & !0xff) | u128::from(crc7(&v.to_be_bytes()[..15])) << 1 | 1
}

/// x^7 + x^3 + 1 (SD spec §4.5).
pub fn crc7(data: &[u8]) -> u8 {
    let mut crc = 0u8;
    for &b in data {
        for i in (0..8).rev() {
            let bit = (b >> i & 1) ^ (crc >> 6 & 1);
            crc = (crc << 1) & 0x7f;
            if bit != 0 {
                crc ^= 0x09;
            }
        }
    }
    crc
}

/// x^16 + x^12 + x^5 + 1, from 0 (SD spec §4.5).
pub fn crc16(data: &[u8]) -> u16 {
    let mut crc = 0u16;
    for &b in data {
        crc ^= u16::from(b) << 8;
        for _ in 0..8 {
            crc = if crc & 0x8000 != 0 {
                crc << 1 ^ 0x1021
            } else {
                crc << 1
            };
        }
    }
    crc
}

// In a file of its own, so that `make docs-check` tells a test-only change by its path.
#[cfg(test)]
mod tests;
