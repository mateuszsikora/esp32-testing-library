use super::*;

#[test]
fn records_every_uart_byte_and_watched_gpio_changes_only() {
    let shared = Shared::default();
    shared.borrow_mut().watch = 1 << 4;
    let mut board = SocketBoard::new(shared.clone(), None);
    let route = UartRoute::new(0, 1 << 1, Some(3), Some(115_200));
    board.uart_tx(10, route, b'x');
    board.gpio_output_at(11, &[(2, true)], 1 << 2, 1 << 2);
    board.gpio_output_at(12, &[(4, true), (2, false)], 1 << 4 | 1 << 2, 1 << 4);
    // an enable-only change on a watched pin: an open-drain line released
    board.gpio_output_at(13, &[], 1 << 2, 0);
    board.gpio_output_at(14, &[], 1 << 2, 0);
    assert_eq!(
        shared.borrow().log,
        vec![
            Recorded::UartTx {
                cycle: 10,
                route,
                byte: b'x'
            },
            Recorded::Gpio {
                cycle: 12,
                changes: vec![(4, true)],
                enabled: 1 << 4,
                output: 1 << 4
            },
            Recorded::Gpio {
                cycle: 13,
                changes: vec![],
                enabled: 0,
                output: 0
            },
        ]
    );
}

#[test]
fn hands_the_peers_levels_to_the_chip_in_order_and_remembers_them() {
    let shared = Shared::default();
    let mut board = SocketBoard::new(shared.clone(), None);
    assert!(board.uses_gpio_edges());
    shared.borrow_mut().drive(5, 33, true);
    shared.borrow_mut().drive(5, 4, false);
    assert_eq!(board.input_levels(), [(4, false), (33, true)]);
    assert_eq!(
        board.take_edges(),
        [
            BoardEdge {
                cycle: 5,
                pin: 33,
                level: true
            },
            BoardEdge {
                cycle: 5,
                pin: 4,
                level: false
            },
        ]
    );
    assert!(board.take_edges().is_empty());
    // the chip takes edges before releases, so a level after a release voids it
    shared.borrow_mut().release(33);
    shared.borrow_mut().release(4);
    shared.borrow_mut().drive(6, 33, false);
    assert_eq!(board.released_inputs(), [4]);
    assert_eq!(board.take_edges().len(), 1);
    assert_eq!(board.input_levels(), [(33, false)]);
}

/// A command to the card on the board's SPI hook, with `answer` more bytes clocked out.
fn command(board: &mut SocketBoard, index: u8, arg: u32, answer: usize) -> Vec<u8> {
    let mut tx = vec![0x40 | index];
    tx.extend(arg.to_be_bytes());
    tx.push(sd_card::crc7(&tx) << 1 | 1);
    tx.resize(6 + answer, 0xff);
    let pins = SpiPins {
        sclk: 1 << 14,
        mosi: 1 << 15,
        miso: Some(2),
        cs: 1 << 13,
    };
    board.spi_transfer_pins(2, pins, &tx, tx.len()).split_off(6)
}

#[test]
fn the_card_hears_the_bus_cycle_and_its_reports_go_into_the_log() {
    let wiring = sd_card::Wiring {
        sclk: 14,
        mosi: 15,
        miso: 2,
        cs: 13,
    };
    let store = esp_periph::sdmmc::MemStore(vec![0; sd_card::UNIT as usize]);
    let card = Rc::new(RefCell::new(
        sd_card::SdCard::new(wiring, Box::new(store)).unwrap(),
    ));
    let shared = Shared::default();
    let mut board = SocketBoard::new(shared.clone(), Some(card.clone()));
    assert!(board.uses_spi_pins());
    command(&mut board, 0, 0, 2);
    command(&mut board, 8, 0x1aa, 6);
    for _ in 0..2 {
        command(&mut board, 55, 0, 2);
        command(&mut board, 41, 1 << 30, 2);
    }
    card.borrow_mut().add_rule(sd_card::Rule {
        id: 1,
        op: sd_card::Op::Read,
        fail: true,
        delay_ns: 0,
        delay: 0,
        blocks: None,
        skip: 0,
        count: Some(1),
    });
    // the latest cycle any hook was given: the tick's, or a later output's
    board.advance_to(500);
    board.gpio_output_at(700, &[], 0, 0);
    board.advance_to(600);
    command(&mut board, 17, 3, 4);
    assert_eq!(card.borrow().now, 700);
    let log = &shared.borrow().log;
    let [Recorded::SdIo(io)] = &log[..] else {
        panic!("{log:?}")
    };
    assert_eq!((io.cycle, io.block, io.failed), (700, 3, true));
    assert!(card.borrow().io.is_empty(), "the board took it");
}
