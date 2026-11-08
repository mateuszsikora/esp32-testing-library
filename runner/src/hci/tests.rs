use super::*;
use esp_soc::ble::peer::Session;

fn session() -> (Shared, Session) {
    let hci = Shared::default();
    let session = Session::new(
        Box::new(Forwarder(hci.clone())),
        esp_soc::ble::Link::default(),
    );
    (hci, session)
}

#[test]
fn a_packet_the_host_sends_keeps_the_cycle_the_hook_moved_the_controller_to() {
    let (hci, mut session) = session();
    // as the `Send` hook does: the controller to the bus's cycle, then the packet
    session.advance_to(1_000);
    session.send(&[0x01, 0x03, 0x0c, 0x00]);
    session.advance_to(2_500);
    session.send(&[0x02, 0x01, 0x20]);
    assert_eq!(
        hci.borrow().tx,
        vec![
            (1_000, vec![0x01, 0x03, 0x0c, 0x00]),
            (2_500, vec![0x02, 0x01, 0x20])
        ]
    );
    assert_eq!(session.controller.next_deadline(), None);
}

#[test]
fn the_host_reads_what_came_due_one_packet_a_poll_in_order() {
    let (hci, mut session) = session();
    assert_eq!(session.pop_packet(), None);
    hci.borrow_mut().rx.push_back(vec![0x04, 0x0e]);
    hci.borrow_mut().rx.push_back(vec![0x04, 0x0f]);
    assert_eq!(session.pop_packet(), Some(vec![0x04, 0x0e]));
    assert_eq!(session.pop_packet(), Some(vec![0x04, 0x0f]));
    assert_eq!(session.pop_packet(), None);
}

/// `reboot()`, which both a chip reset and the runner's power-on reset go through, keeps
/// esp32sim's session, and with it the forwarder, and resets it (`Ble::reset`).
#[test]
fn a_chip_reset_loses_what_the_host_has_not_read_and_keeps_the_forwarder() {
    let (hci, session) = session();
    let mut m = esp32::machine([0x24, 0x6f, 0x28, 0x00, 0x11, 0x22], 4 << 20);
    m.bus.ble.session = session;
    hci.borrow_mut().rx.push_back(vec![0x04, 0x0e]);
    hci.borrow_mut().tx.push((7, vec![0x01, 0x03, 0x0c, 0x00]));
    m.reboot();
    assert!(hci.borrow().rx.is_empty());
    // what the host sent before the reset still goes out
    assert_eq!(hci.borrow().tx, vec![(7, vec![0x01, 0x03, 0x0c, 0x00])]);
    // the session's controller is still the forwarder
    m.bus.ble.session.advance_to(9);
    m.bus.ble.session.send(&[0x01]);
    assert_eq!(hci.borrow().tx.last(), Some(&(9, vec![0x01])));
}
