//! `SocketBoard`, the board on the runner's socket (protocol 6.1). Its callbacks get no access to
//! the bus, so they do no socket work: they record what the guest did, with its bus cycle, into a
//! log the run loop takes between slices. Received bytes do not go through the board either
//! (`uart_rx`): the run loop delivers them with `bus.periph.uart_pin_input`, where it can see the
//! route and the FIFO and report what is dropped. Nor does it need `next_deadline`: the run
//! loop's slices stop for the block point (protocol 11.10).
use esp_soc::board::{BoardModel, VirtualCycle};
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
    /// An output change on a watched pin (`gpio_out`, sent from #23 on).
    Gpio {
        cycle: VirtualCycle,
        changes: Vec<(u8, bool)>,
        enabled: u64,
        output: u64,
    },
}

/// The log the board and the run loop share.
#[derive(Debug, Default)]
pub struct Recorder {
    /// The peer's `watch`, as a mask.
    pub watch: u64,
    pub log: Vec<Recorded>,
}

pub type Shared = Rc<RefCell<Recorder>>;

pub struct SocketBoard {
    shared: Shared,
    /// Watched pins' output enable and latch at the last report, to see enable-only changes.
    driven: (u64, u64),
}

impl SocketBoard {
    pub fn new(shared: Shared) -> Self {
        Self {
            shared,
            driven: (0, 0),
        }
    }
}

impl BoardModel for SocketBoard {
    fn name(&self) -> &'static str {
        "socket"
    }
    // No GPIO inputs before #23: nothing to poll.
    fn uses_gpio_edges(&self) -> bool {
        false
    }
    fn uses_uart_pins(&self) -> bool {
        true
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
        let driven = (enabled & watch, output & watch);
        let changes: Vec<(u8, bool)> = changes
            .iter()
            .copied()
            .filter(|&(pin, _)| pin < 64 && watch & 1 << pin != 0)
            .collect();
        if changes.is_empty() && driven == self.driven {
            return;
        }
        self.driven = driven;
        shared.log.push(Recorded::Gpio {
            cycle,
            changes,
            enabled: driven.0,
            output: driven.1,
        });
    }
}

// In a file of its own, so that `make docs-check` tells a test-only change by its path.
#[cfg(test)]
mod tests;
