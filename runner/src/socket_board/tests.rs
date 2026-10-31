use super::*;

#[test]
fn records_every_uart_byte_and_watched_gpio_changes_only() {
    let shared = Shared::default();
    shared.borrow_mut().watch = 1 << 4;
    let mut board = SocketBoard::new(shared.clone());
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
    let mut board = SocketBoard::new(shared.clone());
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
