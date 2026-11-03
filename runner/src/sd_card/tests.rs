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

/// The data block that starts in `answer`, or later, polled for as sdspi does, each poll 10 bus
/// cycles after the one before.
fn data_block(card: &mut SdCard, mut answer: Vec<u8>, len: usize) -> Vec<u8> {
    for _ in 0..10_000 {
        if answer.contains(&START_BLOCK) {
            break;
        }
        assert!(answer.iter().all(|&b| b == 0xff), "{answer:?}");
        card.now += 10;
        answer = xfer(card, &[0xff; 8]);
    }
    assert!(answer.contains(&START_BLOCK), "no data block");
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

/// A rule of `id` on `op`, every block, until cleared; `delay` in bus cycles, `delay * 10` ns.
fn rule(id: u64, op: Op, fail: bool, delay: u64) -> Rule {
    Rule {
        id,
        op,
        fail,
        delay_ns: delay * 10,
        delay,
        blocks: None,
        skip: 0,
        count: None,
    }
}

/// The first byte of block `lba` as the card has it now.
fn first_byte(card: &mut SdCard, lba: u32) -> u8 {
    let answer = command(card, 17, lba, 10);
    data_block(card, after_r1(answer), BLOCK)[7]
}

#[test]
fn without_rules_the_card_reports_nothing() {
    let mut card = card(2);
    init(&mut card);
    first_byte(&mut card, 3);
    assert_eq!(r1(&command(&mut card, 24, 3, 10)).1, 0);
    let data = [1; BLOCK];
    let response = write_block(&mut card, START_BLOCK, &data, crc16(&data));
    assert_eq!(response, DATA_ACCEPTED);
    assert!(card.io.is_empty());
    assert_eq!((card.stats.failed, card.stats.delayed), (0, 0));
}

#[test]
fn a_write_a_rule_fails_leaves_its_block_as_it_was() {
    let mut card = card(2);
    init(&mut card);
    card.now = 500;
    card.add_rule(Rule {
        count: Some(1),
        ..rule(1, Op::Write, true, 0)
    });
    let data = [0x5a; BLOCK];
    assert_eq!(r1(&command(&mut card, 24, 3, 10)).1, 0);
    let response = write_block(&mut card, START_BLOCK, &data, crc16(&data));
    assert_eq!(response, DATA_WRITE_ERROR);
    assert_eq!(first_byte(&mut card, 3), 3, "the block is as it was");
    assert_eq!(
        card.io,
        [Io {
            cycle: 500,
            began: 500,
            op: Op::Write,
            block: 3,
            blocks: 1,
            rules: vec![1],
            failed: true,
            delay: 0,
            written: false,
        }]
    );
    // a one-shot rule: the next write goes through, and nobody hears of it
    card.io.clear();
    assert_eq!(r1(&command(&mut card, 24, 3, 10)).1, 0);
    let response = write_block(&mut card, START_BLOCK, &data, crc16(&data));
    assert_eq!(response, DATA_ACCEPTED);
    assert_eq!(first_byte(&mut card, 3), 0x5a);
    assert!(card.io.is_empty());
    assert_eq!((card.stats.failed, card.stats.blocks_written), (1, 1));
}

#[test]
fn a_failed_block_of_cmd25_ends_what_it_writes() {
    let mut card = card(2);
    init(&mut card);
    card.add_rule(Rule {
        blocks: Some((11, 11)),
        ..rule(1, Op::Write, true, 0)
    });
    assert_eq!(r1(&command(&mut card, 25, 10, 10)).1, 0);
    let mut responses = Vec::new();
    for n in 0..3u8 {
        let data = [0xa0 | n; BLOCK];
        responses.push(write_block(
            &mut card,
            START_MULTI_WRITE,
            &data,
            crc16(&data),
        ));
    }
    // the card waits for the stop token after an error (SD spec §7.3.3.1)
    assert_eq!(
        responses,
        [DATA_ACCEPTED, DATA_WRITE_ERROR, DATA_WRITE_ERROR]
    );
    card.transfer(pins(true), &[STOP_TRAN, 0xff], 0);
    command(&mut card, 55, 0, 8);
    let answer = command(&mut card, 22, 0, 10);
    assert_eq!(
        data_block(&mut card, after_r1(answer), 4),
        1u32.to_be_bytes()
    );
    let firsts: Vec<u8> = (10..13).map(|lba| first_byte(&mut card, lba)).collect();
    assert_eq!(firsts, [0xa0, 11, 12]);
    let blocks: Vec<u64> = card.io.iter().map(|io| io.block).collect();
    assert_eq!(
        blocks,
        [11, 11],
        "the rule takes the block again after the error"
    );
}

#[test]
fn a_read_a_rule_fails_ends_with_an_ecc_error_token() {
    let mut card = card(2);
    init(&mut card);
    card.add_rule(Rule {
        blocks: Some((6, 6)),
        ..rule(4, Op::Read, true, 0)
    });
    assert_eq!(first_byte(&mut card, 5), 5);
    let answer = after_r1(command(&mut card, 17, 6, 10));
    assert_eq!(answer[..2], [0xff, ERROR_ECC]);
    // CMD18 sends block 5, then the error token in place of block 6, and stops
    let answer = command(&mut card, 18, 5, 16);
    assert_eq!(data_block(&mut card, after_r1(answer), BLOCK)[7], 5);
    assert_eq!(xfer(&mut card, &[0xff; 4]), [0xff, ERROR_ECC, 0xff, 0xff]);
    // the host goes on with another command: it took the error token
    command(&mut card, 13, 0, 8);
    let reads: Vec<(u64, bool, bool)> = card
        .io
        .iter()
        .map(|io| (io.block, io.failed, io.written))
        .collect();
    assert_eq!(reads, [(6, true, false), (6, true, false)]);
    assert_eq!(card.stats.blocks_read, 2);
}

#[test]
fn skip_and_count_pick_the_operations_a_rule_acts_on() {
    let mut card = card(2);
    init(&mut card);
    card.add_rule(Rule {
        skip: 2,
        count: Some(2),
        ..rule(1, Op::Read, true, 0)
    });
    // a write is no read: it neither counts nor skips
    assert_eq!(r1(&command(&mut card, 24, 3, 10)).1, 0);
    let data = [7; BLOCK];
    write_block(&mut card, START_BLOCK, &data, crc16(&data));
    let failed: Vec<bool> = (0..6)
        .map(|lba| after_r1(command(&mut card, 17, lba, 10))[1] == ERROR_ECC)
        .collect();
    assert_eq!(failed, [false, false, true, true, false, false]);
    assert_eq!(card.rules, [], "a rule that is used up is gone");
}

#[test]
fn every_rule_that_matches_acts_and_the_longest_delay_holds() {
    let mut card = card(2);
    init(&mut card);
    card.add_rule(rule(1, Op::Read, false, 300));
    card.add_rule(Rule {
        blocks: Some((0, 3)),
        ..rule(2, Op::Read, false, 700)
    });
    card.add_rule(Rule {
        blocks: Some((8, 9)),
        ..rule(3, Op::Read, true, 0)
    });
    first_byte(&mut card, 2);
    let io = card.io.pop().unwrap();
    assert_eq!((io.rules, io.delay, io.failed), (vec![1, 2], 7000, false));
}

#[test]
fn a_read_delay_holds_the_data_block_until_the_bus_reaches_it() {
    let mut card = card(2);
    init(&mut card);
    card.add_rule(rule(1, Op::Read, false, 1000));
    card.now = 100;
    let answer = after_r1(command(&mut card, 17, 4, 10));
    assert!(answer.iter().all(|&b| b == 0xff), "{answer:?}");
    // a host that polls for the token: all 0xff until cycle 1100
    card.now = 1099;
    assert_eq!(xfer(&mut card, &[0xff; 8]), [0xff; 8]);
    card.now = 1100;
    let answer = xfer(&mut card, &[0xff; 4]);
    assert_eq!(answer[..2], [0xff, START_BLOCK]);
    assert_eq!(card.io[0].cycle, 100);
    assert_eq!(card.io[0].delay, 10_000);
    assert_eq!(card.stats.delayed, 1);
}

#[test]
fn each_block_of_cmd18_waits_from_the_end_of_the_one_before() {
    let mut card = card(2);
    init(&mut card);
    card.add_rule(rule(1, Op::Read, false, 50));
    let answer = command(&mut card, 18, 7, 2);
    assert!(after_r1(answer).iter().all(|&b| b == 0xff));
    card.now = 50;
    let answer = xfer(&mut card, &[0xff; 2]);
    let first = data_block(&mut card, answer, BLOCK);
    assert_eq!(first[7], 7);
    card.now = 60;
    assert_eq!(
        xfer(&mut card, &[0xff; 4]),
        [0xff; 4],
        "block 8 is not ready"
    );
    card.now = 110;
    let answer = xfer(&mut card, &[0xff; 2]);
    let second = data_block(&mut card, answer, BLOCK);
    assert_eq!(second[7], 8);
    // CMD12 ends the read, and the block the card had begun with it; block 8 counts from where
    // the card began block 9, at the CMD12's first byte
    command(&mut card, 12, 0, 9);
    assert_eq!(xfer(&mut card, &[0xff; 4]), [0xff; 4]);
    let cycles: Vec<(u64, u64, u64)> = card
        .io
        .iter()
        .map(|io| (io.block, io.began, io.cycle))
        .collect();
    assert_eq!(cycles, [(7, 0, 0), (8, 60, 110)]);
}

#[test]
fn a_write_delay_keeps_the_card_busy_and_deaf_until_it_is_done() {
    let mut card = card(2);
    init(&mut card);
    card.add_rule(rule(1, Op::Write, false, 1000));
    card.now = 200;
    assert_eq!(r1(&command(&mut card, 24, 3, 10)).1, 0);
    let data = [0x33; BLOCK];
    let response = write_block(&mut card, START_BLOCK, &data, crc16(&data));
    assert_eq!(response, DATA_ACCEPTED);
    assert_eq!(xfer(&mut card, &[0xff; 4]), [BUSY; 4]);
    // a command while the card is busy is not taken (SD spec §7.2.4)
    card.now = 1199;
    let mut tx = frame(13, 0).to_vec();
    tx.resize(14, 0xff);
    assert_eq!(xfer(&mut card, &tx), [BUSY; 14]);
    card.now = 1200;
    assert_eq!(xfer(&mut card, &[0xff; 2]), [0xff; 2]);
    assert_eq!(r1(&command(&mut card, 13, 0, 8)), (1, 0));
    assert_eq!(first_byte(&mut card, 3), 0x33);
    assert!(card.io[0].written);
}

#[test]
fn an_erase_delay_is_busy_after_r1_and_matches_overlapping_blocks() {
    let mut card = card(2);
    init(&mut card);
    card.add_rule(Rule {
        blocks: Some((6, 9)),
        ..rule(1, Op::Erase, false, 100)
    });
    command(&mut card, 32, 4, 8);
    command(&mut card, 33, 6, 8);
    let answer = command(&mut card, 38, 0, 8);
    assert_eq!(answer[..4], [0xff, 0, BUSY, BUSY]);
    card.now = 100;
    assert_eq!(xfer(&mut card, &[0xff; 2]), [0xff; 2]);
    let io = &card.io[0];
    assert_eq!(
        (io.op, io.block, io.blocks, io.written),
        (Op::Erase, 4, 3, true)
    );
    // blocks 1 to 2 do not overlap 6 to 9
    command(&mut card, 32, 1, 8);
    command(&mut card, 33, 2, 8);
    assert_eq!(command(&mut card, 38, 0, 4), [0xff, 0, 0xff, 0xff]);
    assert_eq!(card.io.len(), 1);
}

#[test]
fn cleared_rules_act_no_more_but_a_running_delay_runs_out() {
    let mut card = card(2);
    init(&mut card);
    card.add_rule(rule(1, Op::Write, false, 100));
    card.add_rule(rule(2, Op::Read, true, 0));
    card.add_rule(rule(3, Op::Read, false, 100));
    assert_eq!(r1(&command(&mut card, 24, 3, 10)).1, 0);
    let data = [1; BLOCK];
    write_block(&mut card, START_BLOCK, &data, crc16(&data));
    card.clear_rules(Some(&[1, 2]));
    assert_eq!(xfer(&mut card, &[0xff; 2]), [BUSY; 2]);
    card.now = 100;
    first_byte(&mut card, 3);
    let rules: Vec<Vec<u64>> = card.io.iter().map(|io| io.rules.clone()).collect();
    assert_eq!(rules, [vec![1], vec![3]]);
    card.clear_rules(None);
    card.io.clear();
    first_byte(&mut card, 3);
    assert!(card.io.is_empty());
}

#[test]
fn a_block_cmd12_stops_before_the_host_takes_it_uses_up_no_rule() {
    let mut card = card(2);
    init(&mut card);
    card.add_rule(Rule {
        blocks: Some((9, 9)),
        count: Some(1),
        ..rule(1, Op::Read, true, 0)
    });
    // the host reads blocks 7 and 8; the card has begun block 9 when the CMD12 comes, after a
    // poll for busy as ESP-IDF's
    let answer = command(&mut card, 18, 7, 16);
    data_block(&mut card, after_r1(answer), BLOCK);
    let answer = xfer(&mut card, &[0xff; 2]);
    data_block(&mut card, answer, BLOCK);
    assert_eq!(xfer(&mut card, &[0xff; 3]), [0xff, ERROR_ECC, 0xff]);
    command(&mut card, 12, 0, 9);
    assert!(card.io.is_empty(), "{:?}", card.io);
    assert_eq!(card.stats.failed, 0);
    let ids: Vec<u64> = card.rules.iter().map(|r| r.id).collect();
    assert_eq!(ids, [1], "the rule is as it was");
    // the read the firmware asks for is the one that fails
    assert_eq!(after_r1(command(&mut card, 17, 9, 10))[1], ERROR_ECC);
    assert_eq!(card.io.len(), 1);
    assert!(card.rules.is_empty());
}

#[test]
fn a_later_block_of_cmd18_counts_once_the_host_goes_on_past_it() {
    let mut card = card(2);
    init(&mut card);
    card.add_rule(Rule {
        skip: 1,
        ..rule(1, Op::Read, false, 20)
    });
    let answer = command(&mut card, 18, 7, 16);
    data_block(&mut card, after_r1(answer), BLOCK);
    // block 8 is begun, and its rule's skip used, but nothing is reported yet
    card.now = 100;
    let answer = xfer(&mut card, &[0xff; 2]);
    data_block(&mut card, answer, BLOCK);
    assert!(card.io.is_empty());
    // the card begins block 9: the host took block 8, at that time; then a CMD12 stops block 9
    card.now = 500;
    command(&mut card, 12, 0, 9);
    let reported: Vec<(u64, u64, u64)> = card
        .io
        .iter()
        .map(|io| (io.block, io.began, io.cycle))
        .collect();
    assert_eq!(reported, [(8, 100, 500)]);
    assert_eq!(
        card.rules[0].skip, 0,
        "block 7 used the skip; block 9's verdict is undone"
    );
    // another command, not CMD12, goes on past a block too: here a failed one
    card.clear_rules(None);
    card.add_rule(Rule {
        blocks: Some((12, 12)),
        ..rule(2, Op::Read, true, 0)
    });
    let answer = command(&mut card, 18, 11, 16);
    data_block(&mut card, after_r1(answer), BLOCK);
    card.now = 900;
    let tail = xfer(&mut card, &[0xff; 64]);
    assert!(tail.contains(&ERROR_ECC), "{tail:?}");
    card.now = 1000;
    command(&mut card, 13, 0, 8);
    let last = card.io.last().unwrap();
    assert_eq!(
        (last.block, last.failed, last.began, last.cycle),
        (12, true, 900, 1000)
    );
}

#[test]
fn a_rule_cleared_while_a_block_is_begun_is_not_put_back_by_cmd12() {
    let mut card = card(2);
    init(&mut card);
    card.add_rule(Rule {
        count: Some(5),
        ..rule(1, Op::Read, false, 10)
    });
    let answer = command(&mut card, 18, 7, 16);
    data_block(&mut card, after_r1(answer), BLOCK);
    card.clear_rules(Some(&[1]));
    command(&mut card, 12, 0, 9);
    assert!(card.rules.is_empty());
}

#[test]
fn a_rule_cleared_after_a_block_began_still_owns_it_at_the_time_it_began() {
    let mut card = card(2);
    init(&mut card);
    card.add_rule(Rule {
        blocks: Some((8, 8)),
        ..rule(1, Op::Read, true, 0)
    });
    let answer = command(&mut card, 18, 7, 16);
    data_block(&mut card, after_r1(answer), BLOCK);
    card.now = 100;
    assert_eq!(xfer(&mut card, &[0xff; 2]), [0xff, ERROR_ECC]);
    // the rule goes at 200, and the host goes on at 300: the rule did act, when it began
    card.now = 200;
    card.clear_rules(None);
    card.now = 300;
    command(&mut card, 13, 0, 8);
    let io = &card.io[0];
    assert_eq!((io.block, io.began, io.cycle), (8, 100, 300));
    assert!(card.rules.is_empty());
}

#[test]
fn a_power_cut_keeps_the_rules_ends_a_delay_and_undoes_a_begun_block() {
    let mut card = card(2);
    init(&mut card);
    card.add_rule(Rule {
        blocks: Some((8, 8)),
        count: Some(1),
        ..rule(1, Op::Read, true, 0)
    });
    card.add_rule(rule(2, Op::Write, false, 1000));
    // the card has begun block 8, and it is busy with a write
    let answer = command(&mut card, 18, 7, 16);
    data_block(&mut card, after_r1(answer), BLOCK);
    assert_eq!(xfer(&mut card, &[0xff; 2]), [0xff, ERROR_ECC]);
    card.power_off();
    assert!(card.io.is_empty(), "{:?}", card.io);
    let ids: Vec<u64> = card.rules.iter().map(|r| r.id).collect();
    assert_eq!(
        ids,
        [1, 2],
        "block 8's rule is back, and neither is the card's to lose"
    );
    init(&mut card);
    assert_eq!(after_r1(command(&mut card, 17, 8, 10))[1], ERROR_ECC);
    assert_eq!(r1(&command(&mut card, 24, 3, 10)).1, 0);
    let data = [9; BLOCK];
    write_block(&mut card, START_BLOCK, &data, crc16(&data));
    assert_eq!(xfer(&mut card, &[0xff; 2]), [BUSY; 2]);
    card.power_off();
    init(&mut card);
    assert_eq!(
        first_byte(&mut card, 3),
        9,
        "no busy time is left, and the block is there"
    );
}
