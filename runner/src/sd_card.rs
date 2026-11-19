//! An SD memory card in SPI mode on four of the chip's pins (plan M7), after the SD Physical Layer
//! Simplified Specification (SD spec below), chapter 7. The chip's SPI master hands each
//! transaction to the board with its routes (`BoardModel::spi_transfer_pins`); the card sees the
//! bytes when its SCLK is routed and its CS low, and its answer reaches the chip when the master's
//! MISO is routed from the card's pin. A transaction is full duplex: the card's byte for each byte
//! clocked in comes out on the same clock, so its answer to a command starts one byte after the
//! command's last. The card sits in a [`Slot`], which the peer can empty and fill again while the
//! guest runs (`sd_card`, protocol 5.19).
//!
//! It is a high capacity card (CSD version 2.0, block addressing, 512-byte blocks) of the image's
//! size, as esp32sim's own card (`esp_periph::sdmmc::SdCard`) is; that one speaks the SD bus, not
//! SPI. It answers at once: no busy time after a write or an erase, no wait before a data block
//! beyond one byte. Without CMD6 (CCC without class 10) the host stays at default speed, and
//! there is no locking, write protection or CMD23.
//!
//! The peer's fault rules (`sd_fault`, protocol 5.20) change that for the block operations they
//! apply to: a read answers an error token, a write the data response "write error" and leaves the
//! image as it was, and a delay holds a read's data block back, or keeps the card busy after a
//! write or an erase, for that long on the bus's clock. A rule that observes changes nothing: it
//! only reports the operations it meets. The board tells the card the bus cycle at
//! every tick (`advance`), so a transaction sees the time it happens at, and a firmware that polls
//! sees time pass between its transactions.
use crate::protocol::cycles;
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
/// Data error tokens: card controller error, card ECC failed, out of range.
const ERROR_CC: u8 = 0x02;
const ERROR_ECC: u8 = 0x04;
const ERROR_RANGE: u8 = 0x08;
/// What a card sends while it is busy programming (SD spec §7.2.4).
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
    /// Block operations a rule that fails or delays applied to.
    pub faults: u64,
    /// Block operations one or more rules that observe met.
    pub observed: u64,
}

/// The block operations a fault rule applies to (`sd_fault`'s `op`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Op {
    /// A block read, of CMD17 or of each block of CMD18 the host clocks on for.
    Read,
    /// A block written, of CMD24 or of each block of CMD25.
    Write,
    /// A CMD38, over its whole range.
    Erase,
}

impl Op {
    pub fn name(self) -> &'static str {
        match self {
            Op::Read => "read",
            Op::Write => "write",
            Op::Erase => "erase",
        }
    }
}

/// What a fault rule does to an operation it applies to (`sd_fault`'s `action`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Action {
    /// A read answers the data error token "card ECC failed", a write the data response "write
    /// error"; neither touches the image. Not for an erase.
    Error,
    /// So many nanoseconds more: before a read's data block, or busy after a write's data
    /// response or an erase's R1.
    Delay(u64),
    /// Nothing: the operation goes on as without the rule, which only reports it.
    Observe,
}

/// A rule of the peer's (`sd_fault`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Fault {
    pub id: u64,
    pub op: Op,
    pub action: Action,
    /// The first and last block it applies to; `None` for any.
    pub blocks: Option<(u64, u64)>,
    /// The operations it applies to before it is gone; `None` until it is cleared.
    pub count: Option<u64>,
}

/// An operation a rule applied to (`sd_op`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Faulted {
    /// The bus cycle it started at: a read's command, or for a next block of CMD18 the first
    /// byte clocked for it, a write's data response, an erase's CMD38.
    pub cycle: u64,
    /// The rule's `id`.
    pub fault: u64,
    pub op: Op,
    /// The first and last block of the operation.
    pub blocks: (u64, u64),
    pub action: Action,
    /// Whether the card wrote the image: a write or erase it carried out, delayed or observed.
    pub written: bool,
}

/// The rules an operation met (`SdCard::meet`): every rule that observes it, and the one that
/// fails or delays it, if any, each with its id and action, in the order the peer gave them.
#[derive(Debug)]
struct Met {
    rules: Vec<(u64, Action)>,
}

impl Met {
    /// What the operation does: the action of the rule that fails or delays it, if one does.
    fn action(&self) -> Option<Action> {
        self.rules
            .iter()
            .map(|&(_, action)| action)
            .find(|&action| action != Action::Observe)
    }
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
    out: VecDeque<u8>,
    /// The bus cycle, as the board last said it (`advance`).
    now: u64,
    /// The peer's rules, in the order it gave them: of those that fail or delay, the last one
    /// applies; every one that observes reports.
    faults: Vec<Fault>,
    /// What the rules did, for the run loop (`take_faulted`).
    faulted: Vec<Faulted>,
    /// A delayed read: the cycle its data block may go out at, and the block.
    access: Option<(u64, u64)>,
    /// The cycle a delayed write or erase is done at: until then the card is busy.
    busy_until: Option<u64>,
    pub stats: Stats,
}

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
            faults: Vec::new(),
            faulted: Vec::new(),
            access: None,
            busy_until: None,
            stats: Stats::default(),
        })
    }

    /// The image's size in bytes.
    pub fn len(&self) -> u64 {
        self.blocks * BLOCK as u64
    }

    /// The board's supply went off: the card is back where it was when it was inserted, out of
    /// SPI mode and uninitialised, and a block it had not received whole is lost. What it wrote
    /// is in the image already: a block goes there whole once its CRC is in. A delayed read and a
    /// busy time end with the power; the peer's rules are the test's, and stay.
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
        self.access = None;
        self.busy_until = None;
    }

    /// The bus has reached `cycle`: the transactions from now on happen there.
    pub fn advance(&mut self, cycle: u64) {
        self.now = cycle;
    }

    /// Adds a rule, before the others: of the rules that fail or delay an operation matches, the
    /// one given last applies, and every rule that observes it matches reports it. The operations
    /// that start from now on may meet it; one already under way keeps what it met when it
    /// started.
    pub fn install(&mut self, fault: Fault) {
        self.faults.push(fault);
    }

    /// Removes the rules with these ids, or every rule; an id no rule has, one that has run
    /// out among them, is no error.
    pub fn clear(&mut self, ids: Option<&[u64]>) {
        match ids {
            Some(ids) => self.faults.retain(|f| !ids.contains(&f.id)),
            None => self.faults.clear(),
        }
    }

    /// What the rules did since the last call, in order.
    pub fn take_faulted(&mut self) -> Vec<Faulted> {
        std::mem::take(&mut self.faulted)
    }

    /// The rules an operation `op` on the blocks from `lo` to `hi` meets, each of which counts
    /// it: every rule that observes it, and the rule given last of those that fail or delay it.
    fn meet(&mut self, op: Op, lo: u64, hi: u64) -> Met {
        let matches = |f: &Fault| {
            f.op == op
                && f.blocks
                    .is_none_or(|(first, last)| first <= hi && lo <= last)
        };
        let acting = self
            .faults
            .iter()
            .rposition(|f| f.action != Action::Observe && matches(f));
        let met: Vec<usize> = (0..self.faults.len())
            .filter(|&i| {
                let f = &self.faults[i];
                Some(i) == acting || (f.action == Action::Observe && matches(f))
            })
            .collect();
        let rules = met
            .iter()
            .map(|&i| (self.faults[i].id, self.faults[i].action))
            .collect();
        // from the last, so that the indices before it stay where they are
        for &i in met.iter().rev() {
            if let Some(left) = &mut self.faults[i].count {
                *left -= 1;
                if *left == 0 {
                    self.faults.remove(i);
                }
            }
        }
        if acting.is_some() {
            self.stats.faults += 1;
        }
        if met.len() > usize::from(acting.is_some()) {
            self.stats.observed += 1;
        }
        Met { rules }
    }

    /// One `sd_op` for each rule the operation met, in the order the peer gave them.
    fn report(&mut self, met: Met, op: Op, blocks: (u64, u64), written: bool) {
        for (fault, action) in met.rules {
            self.faulted.push(Faulted {
                cycle: self.now,
                fault,
                op,
                blocks,
                action,
                written,
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
            if self.busy_until.is_some_and(|until| self.now < until) {
                // programming: MISO held low, and what comes in is not heard
                return BUSY;
            }
            self.busy_until = None;
            // a command coming in ends a read: no block is begun, and no rule met, for it
            if self.command.is_empty() && mosi & 0xc0 != 0x40 {
                self.next_block();
            }
        }
        let miso = self.out.pop_front().unwrap_or(0xff);
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

    /// A block written, with its CRC: the data response, then the next block of a CMD25. A rule
    /// meets a block that came in whole and in range: an error leaves the image as it was, a
    /// delay keeps the card busy after the data response of a block it wrote.
    fn block_in(&mut self, lba: u64, multi: bool, buf: &[u8]) {
        let (data, crc) = buf.split_at(BLOCK);
        let response = if self.crc && crc16(data) != u16::from_be_bytes([crc[0], crc[1]]) {
            DATA_CRC_ERROR
        } else if lba >= self.blocks {
            DATA_WRITE_ERROR
        } else {
            let met = self.meet(Op::Write, lba, lba);
            let response = if met.action() == Some(Action::Error) {
                DATA_WRITE_ERROR
            } else if let Err(e) = self.store.write(lba, data.try_into().unwrap()) {
                self.stats.errors += 1;
                eprintln!("esp32tl-runner: SD card: writing block {lba}: {e}");
                DATA_WRITE_ERROR
            } else {
                self.stats.blocks_written += 1;
                self.written += 1;
                DATA_ACCEPTED
            };
            if let (Some(Action::Delay(ns)), DATA_ACCEPTED) = (met.action(), response) {
                self.busy_until = Some(self.now + cycles(ns));
            }
            self.report(met, Op::Write, (lba, lba), response == DATA_ACCEPTED);
            response
        };
        self.out.push_back(response);
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

    /// A delayed block once its time has come, else the next block of a CMD18, once the one
    /// before is out.
    fn next_block(&mut self) {
        if let Some((at, lba)) = self.access {
            // until then, 0xff: the access time goes on (N_AC)
            if self.now >= at {
                self.access = None;
                self.read_block(lba);
            }
            return;
        }
        let Phase::Reading { next } = self.phase else {
            return;
        };
        if next >= self.blocks {
            self.out.extend([0xff, ERROR_RANGE]);
            self.phase = Phase::Command;
            return;
        }
        self.phase = Phase::Reading { next: next + 1 };
        self.start_read(next);
    }

    /// The read of the block at `lba`, as a rule has it: an error token, the block later, or the
    /// block now.
    fn start_read(&mut self, lba: u64) {
        let met = self.meet(Op::Read, lba, lba);
        let action = met.action();
        self.report(met, Op::Read, (lba, lba), false);
        match action {
            Some(Action::Error) => {
                self.out.extend([0xff, ERROR_ECC]);
                self.phase = Phase::Command;
            }
            Some(Action::Delay(ns)) => self.access = Some((self.now + cycles(ns), lba)),
            Some(Action::Observe) | None => self.read_block(lba),
        }
    }

    /// The block at `lba` as a data block, after one byte of access time; an error token when
    /// the image cannot be read.
    fn read_block(&mut self, lba: u64) {
        let mut block = [0; BLOCK];
        match self.store.read(lba, &mut block) {
            Ok(()) => {
                self.stats.blocks_read += 1;
                self.data_block(&block);
            }
            Err(e) => {
                self.stats.errors += 1;
                eprintln!("esp32tl-runner: SD card: reading block {lba}: {e}");
                self.out.extend([0xff, ERROR_CC]);
                self.phase = Phase::Command;
            }
        }
    }

    /// `data` as a data block: a byte of access time, the start token, the data, its CRC16.
    fn data_block(&mut self, data: &[u8]) {
        self.out.extend([0xff, START_BLOCK]);
        self.out.extend(data);
        self.out.extend(crc16(data).to_be_bytes());
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
        // §7.2.3), a delayed one too, and a write that never got its token.
        self.out.clear();
        self.access = None;
        self.phase = Phase::Command;
        // the command response time, one byte (SD spec §7.5.4)
        self.out.push_back(0xff);
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
        self.out.push_back(bits | idle);
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
                self.out.push_back(0);
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
                self.out.extend([0, 0, vca, arg as u8]);
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
                self.out.extend(ocr.to_be_bytes());
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
                self.out.push_back(0xff);
                self.r1(0);
            }
            // CMD13 SEND_STATUS: R2
            13 => {
                self.r1(0);
                self.out.push_back(0);
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
                self.start_read(lba);
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
            // CMD38 ERASE: erased blocks read 0 (SCR DATA_STAT_AFTER_ERASE 0); R1b
            38 => {
                let (Some(lo), Some(hi)) = std::mem::take(&mut self.erase) else {
                    self.r1(ERASE_SEQ);
                    return;
                };
                if hi < lo {
                    self.r1(PARAMETER);
                    return;
                }
                let met = self.meet(Op::Erase, lo, hi);
                let erased = match self.store.erase(lo, hi) {
                    Ok(()) => true,
                    Err(e) => {
                        self.stats.errors += 1;
                        eprintln!("esp32tl-runner: SD card: erasing blocks {lo} to {hi}: {e}");
                        false
                    }
                };
                if let (Some(Action::Delay(ns)), true) = (met.action(), erased) {
                    // R1b: busy after the R1
                    self.busy_until = Some(self.now + cycles(ns));
                }
                self.report(met, Op::Erase, (lo, hi), erased);
                self.r1(0);
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

/// The slot of `--sd-spi`, and the card in it, if any (protocol 5.19). With no card in it, or
/// with its card not selected, the chip reads every MISO byte as 0xff: the line's pull-up, which
/// is what a host waiting for an answer, a data token or the end of a busy time sees. The card
/// comes and goes between two SPI transactions, never inside one; a removal drops what the card
/// had not finished, a block coming in included, and closes the image. Every block it accepted
/// before is in the image already: it writes through. A card that goes in starts as a card that
/// was just powered: in SD mode until a CMD0 with its CS low, and not initialised.
pub struct Slot {
    pub wiring: Wiring,
    card: Option<SdCard>,
    /// The image of the card in the slot, as the run named it.
    image: Option<String>,
    /// What the cards that have left the slot did.
    retired: Stats,
    /// The bus cycle, for the card that goes in next.
    now: u64,
    /// The peer's rules while the slot is empty: they are the test's, and go on to the next card.
    faults: Vec<Fault>,
    /// Cards put in and taken out while the guest ran.
    pub inserted: u64,
    pub removed: u64,
}

pub type Shared = Rc<RefCell<Slot>>;

impl Slot {
    /// An empty slot on `wiring`.
    pub fn new(wiring: Wiring) -> Self {
        Slot {
            wiring,
            card: None,
            image: None,
            retired: Stats::default(),
            now: 0,
            faults: Vec::new(),
            inserted: 0,
            removed: 0,
        }
    }

    /// Puts a card holding `store`, named `image`, in the empty slot; returns the card's size in
    /// bytes.
    pub fn insert(&mut self, image: &str, store: Box<dyn SdStore>) -> Result<u64, String> {
        if let Some(held) = &self.image {
            return Err(format!("the slot holds {held} already"));
        }
        let mut card = SdCard::new(self.wiring, store)?;
        let len = card.len();
        card.now = self.now;
        card.faults = std::mem::take(&mut self.faults);
        self.card = Some(card);
        self.image = Some(image.to_string());
        Ok(len)
    }

    /// Puts a card holding the image file `image` in the empty slot, writing through to it;
    /// returns the card's size in bytes.
    pub fn insert_file(&mut self, image: &str) -> Result<u64, String> {
        if let Some(held) = &self.image {
            return Err(format!("the slot holds {held} already"));
        }
        let store =
            esp_periph::sdmmc::FileStore::write_through(image).map_err(|e| e.to_string())?;
        self.insert(image, Box::new(store))
    }

    /// Takes the card out, closing its image; `false` for an empty slot.
    pub fn remove(&mut self) -> bool {
        let Some(mut card) = self.card.take() else {
            return false;
        };
        self.image = None;
        self.faults = std::mem::take(&mut card.faults);
        let s = card.stats;
        self.retired.commands += s.commands;
        self.retired.blocks_read += s.blocks_read;
        self.retired.blocks_written += s.blocks_written;
        self.retired.errors += s.errors;
        self.retired.faults += s.faults;
        self.retired.observed += s.observed;
        true
    }

    /// The board's supply went off: the card in the slot, if any, loses its power.
    pub fn power_off(&mut self) {
        if let Some(card) = &mut self.card {
            card.power_off();
        }
    }

    /// The image of the card in the slot; `None` when it is empty.
    pub fn image(&self) -> Option<&str> {
        self.image.as_deref()
    }

    /// What every card the slot has held did.
    pub fn stats(&self) -> Stats {
        let mut s = self.retired;
        if let Some(card) = &self.card {
            s.commands += card.stats.commands;
            s.blocks_read += card.stats.blocks_read;
            s.blocks_written += card.stats.blocks_written;
            s.errors += card.stats.errors;
            s.faults += card.stats.faults;
            s.observed += card.stats.observed;
        }
        s
    }

    /// The bus has reached `cycle` (`SdCard::advance`).
    pub fn advance(&mut self, cycle: u64) {
        self.now = cycle;
        if let Some(card) = &mut self.card {
            card.advance(cycle);
        }
    }

    /// Adds a rule (`SdCard::install`), for the card in the slot and the ones after it.
    pub fn install(&mut self, fault: Fault) {
        match &mut self.card {
            Some(card) => card.install(fault),
            None => self.faults.push(fault),
        }
    }

    /// Removes rules (`SdCard::clear`).
    pub fn clear(&mut self, ids: Option<&[u64]>) {
        match &mut self.card {
            Some(card) => card.clear(ids),
            None => match ids {
                Some(ids) => self.faults.retain(|f| !ids.contains(&f.id)),
                None => self.faults.clear(),
            },
        }
    }

    /// What the rules did since the last call (`SdCard::take_faulted`).
    pub fn take_faulted(&mut self) -> Vec<Faulted> {
        match &mut self.card {
            Some(card) => card.take_faulted(),
            None => Vec::new(),
        }
    }

    /// One SPI transaction (`SdCard::transfer`); all 0xff back from an empty slot.
    pub fn transfer(&mut self, pins: SpiPins, tx: &[u8], rx_len: usize) -> Vec<u8> {
        match &mut self.card {
            Some(card) => card.transfer(pins, tx, rx_len),
            None => vec![0xff; rx_len],
        }
    }
}

/// The board of a run without `--board` that has a slot: nothing on the chip's pins but it.
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
