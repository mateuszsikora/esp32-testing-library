//! `SocketBoard`, the board on the runner's socket (protocol 6.1). Its callbacks get no access to
//! the bus, so they do no socket work: they record what the guest did, with its bus cycle, into a
//! log the run loop takes between slices. Received bytes do not go through the board either
//! (`uart_rx`): the run loop delivers them with `bus.periph.uart_pin_input`, where it can see the
//! route and the FIFO and report what is dropped. Nor does it need `next_deadline`: the run
//! loop's slices stop for the block point (protocol 11.10). GPIO inputs do go through it: the run
//! loop hands the peer's `gpio_in` and `release` to the board when they come due, and the chip
//! takes them through `take_edges` and `released_inputs`. With `--sd-rw`, the SD card is on its
//! pins as well: it answers the chip's SPI transactions in the board, and nothing of it goes on
//! the socket.
use crate::sd_card;
use esp_soc::board::{BoardEdge, BoardModel, SpiPins, VirtualCycle};
use esp_soc::uart::UartRoute;
use std::cell::RefCell;
use std::rc::Rc;

/// What a board callback saw, in the order it happened.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Recorded {
    /// A byte the guest wrote into a TX FIFO, with the route at the write. Every port, console
    /// included: what goes on the socket is the run loop's choice.
    UartTx {
        cycle: VirtualCycle,
        route: UartRoute,
        byte: u8,
    },
    /// An output change on a watched pin (`gpio_out`): the changes of watched pins, and the
    /// watched pins' output enable and latch after it.
    Gpio {
        cycle: VirtualCycle,
        changes: Vec<(u8, bool)>,
        enabled: u64,
        output: u64,
    },
}

/// The state the board and the run loop share.
#[derive(Debug, Default)]
pub struct Recorder {
    /// The peer's `watch`, as a mask.
    pub watch: u64,
    pub log: Vec<Recorded>,
    /// Watched pins' output enable and latch as last reported, to see enable-only changes. The
    /// run loop sets them after a chip reset, which calls no hook.
    pub reported: (u64, u64),
    /// Watched pins' pad levels as last reported in `changes`, an undriven pad at 0: what the
    /// peer last saw of each pin, for the report after a chip reset.
    pub levels_reported: u64,
    /// Levels the peer drives that the chip has not taken yet (`take_edges`).
    edges: Vec<BoardEdge>,
    /// Pins the peer released that the chip has not taken yet (`released_inputs`).
    released: Vec<u8>,
    /// The pins the peer drives, and their levels (`input_levels`).
    driving: u64,
    levels: u64,
}

impl Recorder {
    /// From now on the peer drives `pin` to `level` (`gpio_in`).
    pub fn drive(&mut self, cycle: VirtualCycle, pin: u8, level: bool) {
        // the chip takes the edges before the releases: a release this one follows is void
        self.released.retain(|&p| p != pin);
        self.edges.push(BoardEdge { cycle, pin, level });
        self.driving |= 1 << pin;
        self.levels = self.levels & !(1 << pin) | u64::from(level) << pin;
    }

    /// From now on the peer stops driving `pin` (`release`).
    pub fn release(&mut self, pin: u8) {
        self.released.push(pin);
        self.driving &= !(1 << pin);
        self.levels &= !(1 << pin);
    }
}

pub type Shared = Rc<RefCell<Recorder>>;

pub struct SocketBoard {
    shared: Shared,
    card: Option<sd_card::Shared>,
}

impl SocketBoard {
    pub fn new(shared: Shared, card: Option<sd_card::Shared>) -> Self {
        Self { shared, card }
    }
}

impl BoardModel for SocketBoard {
    fn name(&self) -> &'static str {
        "socket"
    }
    fn uses_gpio_edges(&self) -> bool {
        true
    }
    fn uses_uart_pins(&self) -> bool {
        true
    }
    fn uses_spi_pins(&self) -> bool {
        self.card.is_some()
    }
    fn spi_transfer_pins(&mut self, _host: u8, pins: SpiPins, tx: &[u8], rx_len: usize) -> Vec<u8> {
        match &self.card {
            Some(card) => card.borrow_mut().transfer(pins, tx, rx_len),
            None => vec![0xff; rx_len],
        }
    }
    fn uart_tx(&mut self, cycle: VirtualCycle, route: UartRoute, byte: u8) {
        self.shared
            .borrow_mut()
            .log
            .push(Recorded::UartTx { cycle, route, byte });
    }
    fn gpio_output_at(
        &mut self,
        cycle: VirtualCycle,
        changes: &[(u8, bool)],
        enabled: u64,
        output: u64,
    ) {
        let mut shared = self.shared.borrow_mut();
        let watch = shared.watch;
        let reported = (enabled & watch, output & watch);
        let changes: Vec<(u8, bool)> = changes
            .iter()
            .copied()
            .filter(|&(pin, _)| pin < 64 && watch & 1 << pin != 0)
            .collect();
        if changes.is_empty() && reported == shared.reported {
            return;
        }
        shared.reported = reported;
        for &(pin, level) in &changes {
            shared.levels_reported = shared.levels_reported & !(1 << pin) | u64::from(level) << pin;
        }
        shared.log.push(Recorded::Gpio {
            cycle,
            changes,
            enabled: reported.0,
            output: reported.1,
        });
    }
    /// What the peer drives, for the chip to take again after a reset.
    fn input_levels(&self) -> Vec<(u8, bool)> {
        let shared = self.shared.borrow();
        (0..64)
            .filter(|&pin| shared.driving & 1 << pin != 0)
            .map(|pin| (pin, shared.levels & 1 << pin != 0))
            .collect()
    }
    fn take_edges(&mut self) -> Vec<BoardEdge> {
        std::mem::take(&mut self.shared.borrow_mut().edges)
    }
    fn released_inputs(&mut self) -> Vec<u8> {
        std::mem::take(&mut self.shared.borrow_mut().released)
    }
}

// In a file of its own, so that `make docs-check` tells a test-only change by its path.
#[cfg(test)]
mod tests;
