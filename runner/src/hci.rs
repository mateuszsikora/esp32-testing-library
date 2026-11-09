//! `--bt`: the guest's Bluetooth controller is the peer's. esp32sim's VHCI hooks hand the guest's
//! H4 packets to the session's boxed `HciController` (`esp-soc/src/ble.rs`, `ble/peer.rs`); the
//! [`Forwarder`] put there keeps each with the bus cycle it was sent at, for the run loop to send as
//! `hci_tx`, and hands the guest's host the packets of the peer's `hci_rx` that have come due, one
//! at each VHCI poll of the guest's controller task (protocol 5.22).
use esp_soc::ble::HciController;
use std::cell::RefCell;
use std::collections::VecDeque;
use std::rc::Rc;

#[derive(Default)]
pub struct Hci {
    /// What the guest's host sent since the run loop last took it, in order, with the bus cycle
    /// of each.
    pub tx: Vec<(u64, Vec<u8>)>,
    /// What has come due for the guest's host and it has not read yet, in order.
    pub rx: VecDeque<Vec<u8>>,
    /// The bus cycle the VHCI hooks last moved the controller to.
    cycles: u64,
}

/// What the forwarder in the machine and the run loop share.
pub type Shared = Rc<RefCell<Hci>>;

pub struct Forwarder(pub Shared);

impl HciController for Forwarder {
    /// esp32sim's `Send` hook moves the controller to the bus's cycle first (`intercept`,
    /// `esp-soc/src/ble/vhci.rs`), so the cycle is the packet's own.
    fn send_h4(&mut self, packet: &[u8]) {
        let mut hci = self.0.borrow_mut();
        let cycle = hci.cycles;
        hci.tx.push((cycle, packet.to_vec()));
    }

    fn poll_h4(&mut self) -> Option<Vec<u8>> {
        self.0.borrow_mut().rx.pop_front()
    }

    /// A chip reset, or the guest's `esp_bt_controller_deinit`: what the guest's host has not
    /// read is gone with it. What it sent before is not: it goes out as `hci_tx` before the
    /// `reset`.
    fn reset(&mut self) {
        self.0.borrow_mut().rx.clear();
    }

    fn advance_to(&mut self, cycles: u64) {
        self.0.borrow_mut().cycles = cycles;
    }

    /// The peer's packets come due at slice boundaries, not at a deadline of the controller's.
    fn next_deadline(&self) -> Option<u64> {
        None
    }
}

// In a file of its own, so that `make docs-check` tells a test-only change by its path.
#[cfg(test)]
mod tests;
