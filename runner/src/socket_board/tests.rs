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
