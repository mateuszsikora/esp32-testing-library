use super::*;
use esp_periph::sdmmc::MemStore;

const WIRING: Wiring = Wiring {
    sclk: 14,
    mosi: 15,
    miso: 2,
    cs: 13,
};

/// The routes of a transaction to the card, with its CS low or high.
fn pins(selected: bool) -> SpiPins {
    SpiPins {
        sclk: 1 << 14,
        mosi: 1 << 15,
        miso: Some(2),
        cs: if selected { 1 << 13 } else { 0 },
    }
}

/// A card holding `units` 512 KiB units, its blocks numbered: block n starts with n.
fn card(units: u64) -> SdCard {
    let mut image = vec![0; (units * UNIT) as usize];
    for (n, block) in image.chunks_mut(BLOCK).enumerate() {
        block[..8].copy_from_slice(&(n as u64).to_be_bytes());
    }
    SdCard::new(WIRING, Box::new(MemStore(image))).unwrap()
}

fn xfer(card: &mut SdCard, tx: &[u8]) -> Vec<u8> {
    card.transfer(pins(true), tx, tx.len())
}

fn frame(index: u8, arg: u32) -> [u8; 6] {
    let mut f = [0x40 | index, 0, 0, 0, 0, 0];
    f[1..5].copy_from_slice(&arg.to_be_bytes());
    f[5] = crc7(&f[..5]) << 1 | 1;
    f
}

/// A command and `answer` more bytes, as one transaction; what came back after the command.
fn command(card: &mut SdCard, index: u8, arg: u32, answer: usize) -> Vec<u8> {
    let mut tx = frame(index, arg).to_vec();
    tx.resize(6 + answer, 0xff);
    xfer(card, &tx).split_off(6)
}

/// The R1 in an answer: the first byte with its top bit clear.
fn r1(answer: &[u8]) -> (usize, u8) {
    let at = answer.iter().position(|b| b & 0x80 == 0).expect("no R1");
    (at, answer[at])
}

/// CMD0 twice, CMD8, CMD59 and ACMD41 until ready: what sdspi does.
fn init(card: &mut SdCard) {
    xfer(card, &[0xff; 10]);
    command(card, 0, 0, 8);
    assert_eq!(r1(&command(card, 0, 0, 8)), (1, IDLE));
    command(card, 8, 0x1aa, 8);
    assert_eq!(r1(&command(card, 59, 1, 8)).1, IDLE);
    for _ in 0..=INIT_POLLS {
        command(card, 55, 0, 8);
        command(card, 41, OCR_CCS, 8);
    }
    assert!(card.ready);
}

/// What follows the R1 of an answer.
fn after_r1(mut answer: Vec<u8>) -> Vec<u8> {
    let (at, _) = r1(&answer);
    answer.split_off(at + 1)
}

/// The data block that starts in `answer`, or later, polled for as sdspi does.
fn data_block(card: &mut SdCard, mut answer: Vec<u8>, len: usize) -> Vec<u8> {
    while !answer.contains(&START_BLOCK) {
        assert!(answer.iter().all(|&b| b == 0xff), "{answer:?}");
        answer = xfer(card, &[0xff; 8]);
    }
    let start = answer.iter().position(|&b| b == START_BLOCK).unwrap();
    let mut data = answer.split_off(start + 1);
    let rest = len + 2 - data.len().min(len + 2);
    data.extend(xfer(card, &vec![0xff; rest]));
    let crc = u16::from_be_bytes([data[len], data[len + 1]]);
    data.truncate(len);
    assert_eq!(crc16(&data), crc, "the block's CRC16");
    data
}

#[test]
fn an_image_is_whole_512_kib_units() {
    let made = |len| SdCard::new(WIRING, Box::new(MemStore(vec![0; len]))).map(|c| c.len());
    assert_eq!(made(UNIT as usize), Ok(UNIT));
    for len in [0, BLOCK, UNIT as usize + BLOCK] {
        let error = made(len).unwrap_err();
        assert!(error.contains("whole number of 512 KiB units"), "{error}");
    }
}

#[test]
fn the_crcs_are_the_sd_specifications() {
    // SD spec §4.5: CMD0's CRC7 is 0x4a, and 512 bytes of 0xff have CRC16 0x7fa1
    assert_eq!(crc7(&[0x40, 0, 0, 0, 0]), 0x4a);
    assert_eq!(crc16(&[0xff; 512]), 0x7fa1);
}

#[test]
fn a_card_not_selected_or_not_clocked_hears_nothing() {
    let mut card = card(2);
    let mut tx = frame(0, 0).to_vec();
    tx.resize(14, 0xff);
    assert_eq!(card.transfer(pins(false), &tx, 14), [0xff; 14]);
    let unclocked = SpiPins {
        sclk: 1 << 18,
        ..pins(true)
    };
    assert_eq!(card.transfer(unclocked, &tx, 14), [0xff; 14]);
    assert!(!card.spi);
    // selected, it enters SPI mode on that CMD0
    assert_eq!(r1(&xfer(&mut card, &tx)[6..]), (1, IDLE));
}

#[test]
fn without_miso_the_chip_reads_high_but_the_card_listens() {
    let mut card = card(2);
    let mut tx = frame(0, 0).to_vec();
    tx.resize(14, 0xff);
    let elsewhere = SpiPins {
        miso: Some(12),
        ..pins(true)
    };
    assert_eq!(card.transfer(elsewhere, &tx, 14), [0xff; 14]);
    assert!(card.spi);
}

#[test]
fn initialises_as_an_sd_2_high_capacity_card() {
    let mut card = card(2);
    command(&mut card, 0, 0, 8);
    // CMD8 echoes its check pattern and accepts 2.7-3.6 V
    let answer = command(&mut card, 8, 0x1aa, 12);
    let (at, r) = r1(&answer);
    assert_eq!((at, r), (1, IDLE));
    assert_eq!(answer[at + 1..at + 5], [0, 0, 1, 0xaa]);
    // a command for later is illegal while the card initialises
    assert_eq!(r1(&command(&mut card, 17, 0, 8)).1, IDLE | ILLEGAL);
    // the OCR is not ready, and ACMD41 answers idle once, then ready
    let ocr = command(&mut card, 58, 0, 12);
    assert_eq!(ocr[2..6], OCR_VOLTAGE.to_be_bytes());
    let mut idle = vec![];
    for _ in 0..3 {
        command(&mut card, 55, 0, 8);
        idle.push(r1(&command(&mut card, 41, OCR_CCS, 8)).1 & IDLE != 0);
    }
    assert_eq!(idle, [true, false, false]);
    let ocr = command(&mut card, 58, 0, 12);
    assert_eq!(r1(&ocr).1, 0);
    assert_eq!(ocr[2..6], (OCR_READY | OCR_CCS | OCR_VOLTAGE).to_be_bytes());
}

#[test]
fn stays_idle_for_a_host_without_hcs_or_cmd8() {
    let mut card = card(2);
    command(&mut card, 0, 0, 8);
    for _ in 0..3 {
        command(&mut card, 55, 0, 8);
        assert_eq!(r1(&command(&mut card, 41, OCR_CCS, 8)).1, IDLE);
    }
    command(&mut card, 8, 0x1aa, 8);
    for _ in 0..3 {
        command(&mut card, 55, 0, 8);
        assert_eq!(r1(&command(&mut card, 41, 0, 8)).1, IDLE);
    }
}

#[test]
fn checks_crcs_of_cmd0_and_cmd8_always_and_of_the_rest_after_cmd59() {
    let mut card = card(2);
    let mut bad = frame(0, 0);
    bad[5] ^= 2;
    let mut tx = bad.to_vec();
    tx.resize(14, 0xff);
    // in SD mode a CMD0 with a bad CRC is not one
    assert_eq!(xfer(&mut card, &tx), [0xff; 14]);
    command(&mut card, 0, 0, 8);
    let mut bad = frame(8, 0x1aa);
    bad[5] ^= 2;
    let mut tx = bad.to_vec();
    tx.resize(14, 0xff);
    assert_eq!(r1(&xfer(&mut card, &tx)[6..]).1, IDLE | COM_CRC);
    let mut bad = frame(58, 0);
    bad[5] ^= 2;
    let mut tx = bad.to_vec();
    tx.resize(14, 0xff);
    assert_eq!(r1(&xfer(&mut card, &tx)[6..]).1, IDLE);
    command(&mut card, 59, 1, 8);
    assert_eq!(r1(&xfer(&mut card, &tx)[6..]).1, IDLE | COM_CRC);
}

#[test]
fn its_registers_say_its_size_and_name() {
    let mut card = card(64);
    init(&mut card);
    let answer = command(&mut card, 9, 0, 10);
    let csd = u128::from_be_bytes(
        data_block(&mut card, after_r1(answer), 16)
            .try_into()
            .unwrap(),
    );
    assert_eq!(csd >> 126, 1, "CSD version 2.0");
    assert_eq!(csd >> 48 & 0x3f_ffff, 63, "C_SIZE: 64 units");
    assert_eq!(csd >> 84 & 0xfff, CCC);
    assert_eq!(csd as u8 >> 1, crc7(&csd.to_be_bytes()[..15]));
    let answer = command(&mut card, 10, 0, 10);
    let cid = data_block(&mut card, after_r1(answer), 16);
    assert_eq!(cid[1..8], *b"ESSIMSD");
    command(&mut card, 55, 0, 8);
    let answer = command(&mut card, 51, 0, 10);
    assert_eq!(data_block(&mut card, after_r1(answer), 8), scr(false));
    // ACMD13 answers R2: R1, then a status byte
    command(&mut card, 55, 0, 8);
    let answer = command(&mut card, 13, 0, 10);
    assert_eq!(answer[1..3], [0, 0]);
    assert_eq!(data_block(&mut card, answer[3..].to_vec(), 64), ssr());
    assert_eq!(r1(&command(&mut card, 13, 0, 8)), (1, 0));
    assert_eq!(command(&mut card, 13, 0, 8)[2], 0);
}

#[test]
fn reads_a_block_by_its_block_address() {
    let mut card = card(2);
    init(&mut card);
    let answer = command(&mut card, 17, 1000, 10);
    let block = data_block(&mut card, after_r1(answer), BLOCK);
    assert_eq!(block[..8], 1000u64.to_be_bytes());
    assert_eq!(card.stats.blocks_read, 1);
    // nothing follows
    assert_eq!(xfer(&mut card, &[0xff; 8]), [0xff; 8]);
    // out of range
    assert_eq!(r1(&command(&mut card, 17, 2048, 8)).1, PARAMETER);
}

#[test]
fn reads_blocks_until_cmd12_as_sdspi_does() {
    let mut card = card(2);
    init(&mut card);
    let answer = command(&mut card, 18, 7, 16);
    let mut blocks = vec![data_block(&mut card, after_r1(answer), BLOCK)];
    for _ in 0..2 {
        let answer = xfer(&mut card, &[0xff; 2]);
        blocks.push(data_block(&mut card, answer, BLOCK));
    }
    let firsts: Vec<_> = blocks.iter().map(|b| b[7]).collect();
    assert_eq!(firsts, [7, 8, 9]);
    // CMD12: sdspi ignores the byte where R1 would be and finds it after the stuff byte
    let answer = command(&mut card, 12, 0, 9);
    assert_eq!(answer[..3], [0xff, 0xff, 0]);
    assert_eq!(xfer(&mut card, &[0xff; 4]), [0xff; 4]);
    assert_eq!(card.stats.blocks_read, 3, "no block begun for the CMD12");
}

#[test]
fn a_read_past_the_end_ends_with_an_out_of_range_token() {
    let mut card = card(1);
    init(&mut card);
    let answer = command(&mut card, 18, 1023, 16);
    data_block(&mut card, after_r1(answer), BLOCK);
    assert_eq!(xfer(&mut card, &[0xff; 4]), [0xff, ERROR_RANGE, 0xff, 0xff]);
}

/// A block as sdspi writes it: the token, the data alone, then the CRC and the data response.
fn write_block(card: &mut SdCard, token: u8, data: &[u8], crc: u16) -> u8 {
    card.transfer(pins(true), &[token], 0);
    card.transfer(pins(true), data, 0);
    let [hi, lo] = crc.to_be_bytes();
    let response = xfer(card, &[hi, lo, 0xff]);
    assert_eq!(response[..2], [0xff, 0xff]);
    response[2]
}

#[test]
fn writes_a_block_and_checks_its_crc() {
    let mut card = card(2);
    init(&mut card);
    let data = [0x5a; BLOCK];
    assert_eq!(r1(&command(&mut card, 24, 3, 10)).1, 0);
    assert_eq!(
        write_block(&mut card, START_BLOCK, &data, 0),
        DATA_CRC_ERROR
    );
    assert_eq!(r1(&command(&mut card, 24, 3, 10)).1, 0);
    assert_eq!(
        write_block(&mut card, START_BLOCK, &data, crc16(&data)),
        DATA_ACCEPTED
    );
    // not busy
    assert_eq!(xfer(&mut card, &[0xff; 2]), [0xff; 2]);
    let answer = command(&mut card, 17, 3, 10);
    assert_eq!(data_block(&mut card, after_r1(answer), BLOCK), data);
    assert_eq!(card.stats.blocks_written, 1);
}

#[test]
fn writes_blocks_until_the_stop_token() {
    let mut card = card(2);
    init(&mut card);
    assert_eq!(r1(&command(&mut card, 25, 10, 10)).1, 0);
    for n in 0..3u8 {
        let data = [n; BLOCK];
        let response = write_block(&mut card, START_MULTI_WRITE, &data, crc16(&data));
        assert_eq!(response, DATA_ACCEPTED);
    }
    card.transfer(pins(true), &[STOP_TRAN, 0xff], 0);
    command(&mut card, 55, 0, 8);
    let answer = command(&mut card, 22, 0, 10);
    assert_eq!(
        data_block(&mut card, after_r1(answer), 4),
        3u32.to_be_bytes()
    );
    for n in 0..3u8 {
        let answer = command(&mut card, 17, 10 + u32::from(n), 10);
        assert_eq!(data_block(&mut card, after_r1(answer), BLOCK), [n; BLOCK]);
    }
}

#[test]
fn erases_blocks_to_zeros() {
    let mut card = card(2);
    init(&mut card);
    assert_eq!(r1(&command(&mut card, 38, 0, 8)).1, ERASE_SEQ);
    command(&mut card, 32, 5, 8);
    command(&mut card, 33, 6, 8);
    assert_eq!(r1(&command(&mut card, 38, 0, 8)).1, 0);
    for (lba, first) in [(4, 4), (5, 0), (6, 0), (7, 7)] {
        let answer = command(&mut card, 17, lba, 10);
        assert_eq!(data_block(&mut card, after_r1(answer), BLOCK)[7], first);
    }
}

#[test]
fn a_command_after_a_chip_reset_finds_the_card_where_it_was() {
    let mut card = card(2);
    init(&mut card);
    // the card keeps its power, and with it CRC checking and its state, until CMD0
    let mut bad = frame(58, 0);
    bad[5] ^= 2;
    let mut tx = bad.to_vec();
    tx.resize(14, 0xff);
    assert_eq!(r1(&xfer(&mut card, &tx)[6..]).1, COM_CRC);
    assert_eq!(r1(&command(&mut card, 52, 0, 8)).1, ILLEGAL);
    init(&mut card);
}

#[test]
fn after_a_power_cut_the_card_is_uninitialised_and_keeps_its_whole_blocks() {
    let mut card = card(2);
    init(&mut card);
    let data = [0xa5; BLOCK];
    assert_eq!(r1(&command(&mut card, 25, 7, 10)).1, 0);
    let response = write_block(&mut card, START_MULTI_WRITE, &data, crc16(&data));
    assert_eq!(response, DATA_ACCEPTED);
    // the next block is cut short by the power going off
    card.transfer(pins(true), &[START_MULTI_WRITE], 0);
    card.transfer(pins(true), &[0x3c; 100], 0);
    card.power_off();
    assert!(!card.ready);
    // out of SPI mode: nothing answers until CMD0 with CS low
    assert_eq!(command(&mut card, 58, 0, 8), [0xff; 8]);
    init(&mut card);
    let answer = command(&mut card, 17, 7, 10);
    assert_eq!(data_block(&mut card, after_r1(answer), BLOCK), data);
    let answer = command(&mut card, 17, 8, 10);
    assert_eq!(data_block(&mut card, after_r1(answer), BLOCK)[7], 8);
}

/// An image the test still sees once the card holding it is out of the slot.
struct SharedStore(Rc<RefCell<Vec<u8>>>);

impl SdStore for SharedStore {
    fn len(&self) -> u64 {
        self.0.borrow().len() as u64
    }
    fn read(&mut self, lba: u64, buf: &mut [u8; BLOCK]) -> std::io::Result<()> {
        let at = lba as usize * BLOCK;
        buf.copy_from_slice(&self.0.borrow()[at..at + BLOCK]);
        Ok(())
    }
    fn write(&mut self, lba: u64, data: &[u8; BLOCK]) -> std::io::Result<()> {
        let at = lba as usize * BLOCK;
        self.0.borrow_mut()[at..at + BLOCK].copy_from_slice(data);
        Ok(())
    }
}

#[test]
fn a_power_cut_reaches_the_card_in_the_slot_and_nothing_else() {
    let mut slot = Slot::new(WIRING);
    slot.power_off();
    assert_eq!(slot.image(), None, "an empty slot stays empty");
    slot.insert("a", Box::new(MemStore(vec![0; UNIT as usize])))
        .unwrap();
    let mut reset = frame(0, 0).to_vec();
    reset.resize(14, 0xff);
    assert_eq!(
        r1(&slot.transfer(pins(true), &reset, 14)[6..]),
        (1, IDLE),
        "in SPI mode"
    );
    slot.power_off();
    assert_eq!(slot.image(), Some("a"), "the card stays in the slot");
    let mut status = frame(13, 0).to_vec();
    status.resize(14, 0xff);
    assert_eq!(
        slot.transfer(pins(true), &status, 14),
        [0xff; 14],
        "back in SD mode: nothing answers until CMD0"
    );
}

#[test]
fn an_empty_slot_answers_nothing_and_takes_one_card_at_a_time() {
    let mut slot = Slot::new(WIRING);
    let mut tx = frame(0, 0).to_vec();
    tx.resize(14, 0xff);
    assert_eq!(slot.transfer(pins(true), &tx, 14), [0xff; 14]);
    assert_eq!(slot.image(), None);
    assert!(!slot.remove(), "nothing to take out");
    let error = slot
        .insert("short", Box::new(MemStore(vec![0; BLOCK])))
        .unwrap_err();
    assert!(error.contains("whole number of 512 KiB units"), "{error}");
    assert_eq!(slot.image(), None, "a card it refused is not in it");
    let len = slot.insert("a", Box::new(MemStore(vec![0; UNIT as usize])));
    assert_eq!(len, Ok(UNIT));
    let error = slot
        .insert("b", Box::new(MemStore(vec![0; UNIT as usize])))
        .unwrap_err();
    assert_eq!(error, "the slot holds a already");
    assert_eq!(slot.image(), Some("a"));
}

#[test]
fn a_card_taken_out_mid_write_keeps_the_blocks_it_accepted_and_comes_back_unpowered() {
    let image = Rc::new(RefCell::new(vec![0u8; 2 * UNIT as usize]));
    let mut slot = Slot::new(WIRING);
    slot.insert("card", Box::new(SharedStore(image.clone())))
        .unwrap();
    let x = |slot: &mut Slot, tx: &[u8]| slot.transfer(pins(true), tx, tx.len());
    // initialise and start a multiple block write at block 8, as sdspi does
    x(&mut slot, &[0xff; 10]);
    for (index, arg) in [
        (0, 0),
        (8, 0x1aa),
        (55, 0),
        (41, OCR_CCS),
        (55, 0),
        (41, OCR_CCS),
    ] {
        let mut tx = frame(index, arg).to_vec();
        tx.resize(14, 0xff);
        x(&mut slot, &tx);
    }
    let mut tx = frame(25, 8).to_vec();
    tx.resize(16, 0xff);
    assert_eq!(r1(&x(&mut slot, &tx)[6..]).1, 0);
    let first = [0x11; BLOCK];
    x(&mut slot, &[START_MULTI_WRITE]);
    x(&mut slot, &first);
    let [hi, lo] = crc16(&first).to_be_bytes();
    assert_eq!(x(&mut slot, &[hi, lo, 0xff])[2], DATA_ACCEPTED);
    // the second block is on its way when the card goes
    x(&mut slot, &[START_MULTI_WRITE]);
    x(&mut slot, &[0x22; BLOCK]);
    let commands = slot.stats().commands;
    assert!(slot.remove());
    // the data response, and the end of the busy time, read as the pull-up's 0xff
    assert_eq!(x(&mut slot, &[0, 0, 0xff, 0xff]), [0xff; 4]);
    {
        let image = image.borrow();
        assert_eq!(image[8 * BLOCK..9 * BLOCK], first, "accepted: in the image");
        assert_eq!(
            image[9 * BLOCK..10 * BLOCK],
            [0; BLOCK],
            "not finished: never written"
        );
    }
    // the same image again: a card just powered, in SD mode until CMD0, its work counted on
    slot.insert("card", Box::new(SharedStore(image.clone())))
        .unwrap();
    let mut read = frame(17, 8).to_vec();
    read.resize(16, 0xff);
    assert_eq!(x(&mut slot, &read), [0xff; 16]);
    let mut reset = frame(0, 0).to_vec();
    reset.resize(14, 0xff);
    assert_eq!(r1(&x(&mut slot, &reset)[6..]), (1, IDLE));
    assert_eq!(r1(&x(&mut slot, &read)[6..]).1, IDLE | ILLEGAL);
    assert_eq!(slot.stats().commands, commands + 2);
}

fn rule(id: u64, op: Op, action: Action, blocks: Option<(u64, u64)>, count: Option<u64>) -> Fault {
    Fault {
        id,
        op,
        action,
        blocks,
        count,
    }
}

/// One bus millisecond.
const MS: u64 = esp32::periph::CPU_HZ / 1000;

#[test]
fn without_a_rule_nothing_is_faulted_and_the_card_never_waits() {
    let mut card = card(2);
    init(&mut card);
    card.advance(5 * MS);
    let data = [0x11; BLOCK];
    command(&mut card, 24, 4, 10);
    assert_eq!(
        write_block(&mut card, START_BLOCK, &data, crc16(&data)),
        DATA_ACCEPTED
    );
    assert_eq!(xfer(&mut card, &[0xff; 2]), [0xff; 2], "not busy");
    let answer = after_r1(command(&mut card, 17, 4, 10));
    assert_eq!(answer[..2], [0xff, START_BLOCK], "one byte of access time");
    assert!(card.take_faulted().is_empty());
    assert_eq!(card.stats.faults, 0);
}

#[test]
fn a_failed_read_is_an_ecc_error_token_and_a_failed_write_leaves_the_block() {
    let mut card = card(2);
    init(&mut card);
    card.install(rule(1, Op::Read, Action::Error, None, Some(1)));
    card.install(rule(2, Op::Write, Action::Error, Some((3, 3)), None));
    card.advance(7 * MS);
    let answer = after_r1(command(&mut card, 17, 9, 10));
    assert_eq!(answer[..2], [0xff, ERROR_ECC]);
    assert_eq!(
        xfer(&mut card, &[0xff; 4]),
        [0xff; 4],
        "nothing after the token"
    );
    // one-shot: the next read is the block
    let answer = command(&mut card, 17, 9, 10);
    assert_eq!(data_block(&mut card, after_r1(answer), BLOCK)[7], 9);

    let data = [0x5a; BLOCK];
    command(&mut card, 24, 3, 10);
    assert_eq!(
        write_block(&mut card, START_BLOCK, &data, crc16(&data)),
        DATA_WRITE_ERROR
    );
    let answer = command(&mut card, 17, 3, 10);
    assert_eq!(
        data_block(&mut card, after_r1(answer), BLOCK)[7],
        3,
        "as it was"
    );
    // the rule is on block 3 only
    command(&mut card, 24, 4, 10);
    assert_eq!(
        write_block(&mut card, START_BLOCK, &data, crc16(&data)),
        DATA_ACCEPTED
    );
    assert_eq!(
        card.take_faulted(),
        [
            Faulted {
                cycle: 7 * MS,
                fault: 1,
                op: Op::Read,
                blocks: (9, 9),
                action: Action::Error,
                written: false,
            },
            Faulted {
                cycle: 7 * MS,
                fault: 2,
                op: Op::Write,
                blocks: (3, 3),
                action: Action::Error,
                written: false,
            },
        ]
    );
    assert_eq!((card.stats.faults, card.stats.blocks_written), (2, 1));
}

#[test]
fn a_delayed_read_holds_its_block_back_on_the_bus_clock() {
    let mut card = card(2);
    init(&mut card);
    card.install(rule(1, Op::Read, Action::Delay(2_000_000), None, None));
    card.advance(10 * MS);
    let answer = after_r1(command(&mut card, 17, 5, 10));
    assert!(answer.iter().all(|&b| b == 0xff), "{answer:?}");
    // however many bytes the host clocks meanwhile
    assert_eq!(xfer(&mut card, &[0xff; 600]), vec![0xff; 600]);
    card.advance(11 * MS);
    assert_eq!(xfer(&mut card, &[0xff; 8]), [0xff; 8]);
    card.advance(12 * MS);
    let answer = xfer(&mut card, &[0xff; 8]);
    assert_eq!(answer[..2], [0xff, START_BLOCK]);
    assert_eq!(data_block(&mut card, answer, BLOCK)[7], 5);
    // a command ends a read that waits, as it ends one under way
    command(&mut card, 17, 6, 10);
    card.advance(20 * MS);
    let answer = command(&mut card, 13, 0, 4);
    assert_eq!(r1(&answer), (1, 0));
    assert_eq!(xfer(&mut card, &[0xff; 8]), [0xff; 8]);
}

#[test]
fn each_block_of_a_multiple_block_read_is_one_operation() {
    let mut card = card(2);
    init(&mut card);
    // the second block fails, then the rule is spent
    card.install(rule(1, Op::Read, Action::Error, Some((8, 8)), None));
    let answer = command(&mut card, 18, 7, 16);
    assert_eq!(data_block(&mut card, after_r1(answer), BLOCK)[7], 7);
    assert_eq!(xfer(&mut card, &[0xff; 4]), [0xff, ERROR_ECC, 0xff, 0xff]);
    let faulted = card.take_faulted();
    assert_eq!(faulted.len(), 1);
    assert_eq!(faulted[0].blocks, (8, 8));
}

/// What `sdspi` clocks for `blocks` blocks of a CMD18: the last block's CRC with no byte after it,
/// the others' with the two bytes where it looks for the next start token.
fn read_blocks(card: &mut SdCard, lba: u32, blocks: usize) -> Vec<Vec<u8>> {
    let mut answer = after_r1(command(card, 18, lba, 8));
    (0..blocks)
        .map(|i| {
            let block = data_block(card, std::mem::take(&mut answer), BLOCK);
            if i + 1 < blocks {
                answer = xfer(card, &[0xff; 2]);
            }
            block
        })
        .collect()
}

#[test]
fn a_rule_is_not_met_by_the_block_a_cmd12_ends_before_it_begins() {
    let mut card = card(2);
    init(&mut card);
    // on the block right after the read, which the host never asks for
    card.install(rule(1, Op::Read, Action::Error, Some((8, 8)), Some(1)));
    let blocks = read_blocks(&mut card, 6, 2);
    assert_eq!((blocks[0][7], blocks[1][7]), (6, 7));
    assert_eq!(r1(&command(&mut card, 12, 0, 9)).1, 0);
    assert!(card.take_faulted().is_empty());
    assert_eq!(card.stats.faults, 0);
    // it is still there for the read of that block
    let answer = after_r1(command(&mut card, 17, 8, 10));
    assert_eq!(answer[..2], [0xff, ERROR_ECC]);
    assert_eq!(card.take_faulted()[0].blocks, (8, 8));

    // and a lasting one meets as many blocks as the host read
    card.install(rule(2, Op::Read, Action::Delay(1), None, None));
    card.advance(MS);
    let blocks = {
        let mut answer = after_r1(command(&mut card, 18, 3, 8));
        card.advance(2 * MS);
        answer.extend(xfer(&mut card, &[0xff; 8]));
        let start = answer.iter().position(|&b| b == START_BLOCK).unwrap();
        data_block(&mut card, answer.split_off(start), BLOCK)
    };
    assert_eq!(blocks[7], 3);
    command(&mut card, 12, 0, 9);
    let faulted = card.take_faulted();
    assert_eq!(faulted.len(), 1, "{faulted:?}");
    assert_eq!(faulted[0].blocks, (3, 3));
}

#[test]
fn a_delayed_write_keeps_the_card_busy_and_deaf_until_it_is_done() {
    let mut card = card(2);
    init(&mut card);
    card.install(rule(4, Op::Write, Action::Delay(3_000_000), None, Some(1)));
    card.advance(MS);
    let data = [0x77; BLOCK];
    command(&mut card, 24, 2, 10);
    assert_eq!(
        write_block(&mut card, START_BLOCK, &data, crc16(&data)),
        DATA_ACCEPTED
    );
    // written at once: the busy is time only
    assert_eq!(card.stats.blocks_written, 1);
    assert_eq!(xfer(&mut card, &[0xff; 3]), [BUSY; 3]);
    // a command meanwhile is not heard, and MISO stays low
    let busy = card.transfer(pins(true), &frame(13, 0), 6);
    assert_eq!(busy, [BUSY; 6]);
    // deselected, the card lets go of MISO
    assert_eq!(card.transfer(pins(false), &[0xff; 2], 2), [0xff; 2]);
    card.advance(4 * MS - 1);
    assert_eq!(xfer(&mut card, &[0xff]), [BUSY]);
    card.advance(4 * MS);
    assert_eq!(xfer(&mut card, &[0xff; 2]), [0xff; 2]);
    let answer = command(&mut card, 17, 2, 10);
    assert_eq!(data_block(&mut card, after_r1(answer), BLOCK), data);
    let faulted = card.take_faulted();
    assert_eq!(faulted.len(), 1);
    assert_eq!(
        (faulted[0].cycle, faulted[0].action, faulted[0].written),
        (MS, Action::Delay(3_000_000), true)
    );
    // spent: the next write is not busy
    command(&mut card, 24, 2, 10);
    write_block(&mut card, START_BLOCK, &data, crc16(&data));
    assert_eq!(xfer(&mut card, &[0xff; 2]), [0xff; 2]);
}

#[test]
fn a_failed_block_of_a_multiple_block_write_waits_for_the_stop_token() {
    let mut card = card(2);
    init(&mut card);
    card.install(rule(1, Op::Write, Action::Error, Some((11, 11)), Some(1)));
    command(&mut card, 25, 10, 10);
    let responses: Vec<u8> = (0..3u8)
        .map(|n| {
            let data = [n + 1; BLOCK];
            write_block(&mut card, START_MULTI_WRITE, &data, crc16(&data))
        })
        .collect();
    // the card stays at block 11 after the error, as for an image it cannot write
    assert_eq!(responses, [DATA_ACCEPTED, DATA_WRITE_ERROR, DATA_ACCEPTED]);
    card.transfer(pins(true), &[STOP_TRAN, 0xff], 0);
    for (lba, first) in [(10, 1), (11, 3)] {
        let answer = command(&mut card, 17, lba, 10);
        assert_eq!(data_block(&mut card, after_r1(answer), BLOCK)[0], first);
    }
}

#[test]
fn an_erase_meets_a_rule_on_any_block_of_its_range_and_is_busy_after_its_r1() {
    let mut card = card(2);
    init(&mut card);
    card.install(rule(
        1,
        Op::Erase,
        Action::Delay(1_000_000),
        Some((6, 9)),
        None,
    ));
    command(&mut card, 32, 2, 8);
    command(&mut card, 33, 6, 8);
    let answer = command(&mut card, 38, 0, 8);
    let (at, r1) = r1(&answer);
    assert_eq!(r1, 0);
    assert!(answer[at + 1..].iter().all(|&b| b == BUSY), "{answer:?}");
    card.advance(MS);
    assert_eq!(xfer(&mut card, &[0xff; 2]), [0xff; 2]);
    let faulted = card.take_faulted();
    assert_eq!(
        (faulted[0].fault, faulted[0].blocks, faulted[0].written),
        (1, (2, 6), true)
    );
    let answer = command(&mut card, 17, 2, 10);
    assert_eq!(
        data_block(&mut card, after_r1(answer), BLOCK)[7],
        0,
        "erased"
    );
}

#[test]
fn the_rule_given_last_applies_and_cleared_rules_apply_no_more() {
    let mut card = card(2);
    init(&mut card);
    card.install(rule(1, Op::Read, Action::Delay(1), None, None));
    card.install(rule(2, Op::Read, Action::Error, Some((0, 3)), Some(1)));
    let read = |card: &mut SdCard, lba| {
        command(card, 17, lba, 10);
        xfer(card, &[0xff; BLOCK + 8]);
        card.take_faulted()[0].fault
    };
    assert_eq!(read(&mut card, 2), 2, "the later rule");
    assert_eq!(read(&mut card, 2), 1, "the later one is spent");
    assert_eq!(read(&mut card, 9), 1);
    card.clear(Some(&[1, 7]));
    command(&mut card, 17, 2, 10);
    assert!(card.take_faulted().is_empty());
    card.install(rule(3, Op::Read, Action::Error, None, None));
    card.install(rule(4, Op::Write, Action::Error, None, None));
    card.clear(None);
    command(&mut card, 17, 2, 10);
    assert!(card.take_faulted().is_empty());
}

#[test]
fn a_power_cut_ends_a_delay_and_leaves_the_rules() {
    let mut card = card(2);
    init(&mut card);
    card.install(rule(1, Op::Write, Action::Delay(3_000_000), None, Some(2)));
    card.install(rule(
        2,
        Op::Read,
        Action::Delay(3_000_000),
        Some((5, 5)),
        Some(2),
    ));
    let data = [0x77; BLOCK];
    command(&mut card, 24, 2, 10);
    write_block(&mut card, START_BLOCK, &data, crc16(&data));
    assert_eq!(xfer(&mut card, &[0xff]), [BUSY]);
    card.power_off();
    // no longer busy at the same instant: the card answers its CMD0 and is initialised again
    init(&mut card);
    command(&mut card, 17, 5, 10);
    assert_eq!(card.take_faulted().len(), 2);
    card.power_off();
    init(&mut card);
    // the read that waited is gone with the power, and the rules are as they were, one use left
    assert_eq!(xfer(&mut card, &[0xff; 8]), [0xff; 8]);
    command(&mut card, 17, 5, 10);
    command(&mut card, 24, 2, 10);
    write_block(&mut card, START_BLOCK, &data, crc16(&data));
    let faulted: Vec<_> = card.take_faulted().iter().map(|f| f.fault).collect();
    assert_eq!(faulted, [2, 1]);
    assert!(card.faults.is_empty(), "both spent");
}

#[test]
fn rules_stay_through_a_change_of_card_and_a_delay_goes_with_the_card() {
    let mut slot = Slot::new(WIRING);
    let store = || Box::new(MemStore(vec![0; UNIT as usize]));
    slot.insert("a", store()).unwrap();
    slot.advance(MS);
    slot.install(rule(1, Op::Write, Action::Delay(3_000_000), None, Some(1)));
    slot.install(rule(2, Op::Read, Action::Error, None, None));
    let card = slot.card.as_mut().unwrap();
    init(card);
    let data = [0x77; BLOCK];
    command(card, 24, 2, 10);
    write_block(card, START_BLOCK, &data, crc16(&data));
    assert_eq!(xfer(card, &[0xff]), [BUSY]);
    assert_eq!(slot.take_faulted().len(), 1);
    assert!(slot.remove());
    // a rule given with the slot empty, and one cleared then, reach the next card too
    slot.install(rule(3, Op::Erase, Action::Delay(1), None, None));
    slot.clear(Some(&[3]));
    assert!(slot.take_faulted().is_empty());
    slot.advance(2 * MS);
    slot.insert("b", store()).unwrap();
    let card = slot.card.as_mut().unwrap();
    // not busy, though the delay had 2 ms to go: that card is out
    init(card);
    let answer = after_r1(command(card, 17, 5, 10));
    assert_eq!(answer[..2], [0xff, ERROR_ECC], "rule 2, on the new card");
    let faulted = slot.take_faulted();
    assert_eq!(
        (faulted[0].fault, faulted[0].cycle),
        (2, 2 * MS),
        "on the slot's clock"
    );
    assert_eq!(
        slot.card.as_ref().unwrap().faults.len(),
        1,
        "rule 1 spent, rule 3 cleared"
    );
    assert_eq!(slot.stats().faults, 2, "counted over both cards");
}
