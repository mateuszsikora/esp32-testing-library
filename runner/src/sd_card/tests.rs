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
    assert_eq!(card.stats.blocks_read, 4, "the block the card had begun");
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
