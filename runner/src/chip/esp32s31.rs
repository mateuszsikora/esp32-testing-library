//! The ESP32-S31 (`--chip esp32s31`): esp32sim's `esp32s31` crate, two RISC-V cores at 320 MHz.
//!
//! esp32sim's S31 needs board inputs to boot ESP-IDF (`BoardInputs`: the flash part, the PLL's
//! input, the pads' idle levels), which its command line gives a devkit and [`Target::new`] gives
//! here. Its bus tells the board neither its UART bytes nor the cycle of a GPIO change (#229,
//! G7), so the machine is wrapped ([`S31`]) for what the run loop needs of them:
//!
//! - UART0 has no pins, as on the P4: the console comes from its TX FIFO as esp32sim drains it.
//!   esp32sim's S31 takes host bytes into the RX FIFO at once, drops what does not fit and
//!   refuses them while UART0's clock or receiver is off, so the peer's `console_rx` waits here
//!   and goes in one byte per character time at UART0's baud, once the FIFO has room and UART0
//!   takes it ([`S31::run_until`]).
//! - GPIO output is read from esp32sim's record of pad level changes, which has the bus cycle of
//!   each (`observe_gpio`), and reported to the board after each run, as the other chips' buses
//!   report it while they run (`gpio_output_at`). The output enable of a change is the pads'
//!   at the end of the run, which is exact unless a pin changes direction inside one slice.
use super::{Profile, Target};
use crate::cli;
use crate::hci;
use crate::session::Guest;
use esp_soc::board::BoardModel;
use esp_soc::uart::UartRoute;
use esp_soc::{Machine, SocBus, Stop};
use std::borrow::Cow;
use std::collections::VecDeque;

pub const PROFILE: Profile = Profile {
    name: "esp32s31",
    title: "the ESP32-S31",
    cpu_hz: esp32s31::periph::CPU_HZ,
    gpios: GPIOS,
    input_only: 0,
    // esp32sim models UART0 alone
    uarts: 1,
    uart_pins: false,
    sd_spi: false,
    wifi_station: false,
    wifi: false,
    bt: false,
    usb_serial_jtag: true,
    // the in-package PSRAM of the ESP32-S31NRV16, which esp32sim's S31 takes as a board input
    psram_mb: &[16],
};

/// GPIO 0 to 61 without 29 and 41 (ESP-IDF v6.1 `SOC_GPIO_VALID_GPIO_MASK`, esp32sim's
/// `esp32s31/src/pads.rs`).
const GPIOS: u64 = ((1 << 62) - 1) & !(1 << 29) & !(1 << 41);

/// The MAC esp32sim gives the S31 (`cli/src/lib.rs`, `setup_s31`).
pub const MAC: [u8; 6] = [0x10, 0x20, 0x30, 0x40, 0x50, 0x60];

/// The strapping esp32sim's command line gives the S31: SPI_FAST_FLASH_BOOT.
const STRAP: u32 = 0x10;

/// UART0's RX FIFO, as esp32sim's S31 has it (`esp-periph/src/uart.rs`).
const RX_FIFO: usize = 128;

/// The baud a byte of `console_rx` is paced at before the guest has set one.
const DEFAULT_BAUD: u64 = 115_200;

/// LP_SYSTEM_REG_LP_STORE6, `RTC_RESET_CAUSE_REG` of ESP-IDF's
/// `esp_rom/esp32s31/include/esp32s31/rom/rtc.h`: `DR_REG_LP_SYS_BASE + 0x44` (v6.1
/// `soc/esp32s31/register/soc/lp_system_reg.h`). esp32sim 357b0e5's reset makes the S31's bus
/// anew, the LP_STORE registers zero (`esp32s31/src/soc.rs`, `reboot`), where the chip keeps
/// them: `chip_reset` puts this one back.
pub(crate) const RESET_HINT: u32 = 0x2070_0044;

/// esp32sim's S31 machine, with what the run loop needs of it that esp32sim does not do: the
/// console's input paced into UART0, and GPIO output reported to the board with its cycle.
pub struct S31 {
    pub m: esp32s31::Machine,
    /// The peer's `console_rx` that UART0 has not taken yet.
    rx: VecDeque<u8>,
    /// The bus cycle the next byte of `rx` is due at.
    rx_due: u64,
    /// The pads' output enable and level as last reported to the board.
    drive: (u64, u64),
}

impl S31 {
    /// The pads the chip drives, and those of them it drives high.
    fn pads(&self) -> (u64, u64) {
        use esp32s31::pads::Drive;
        let pads = &self.m.bus.inner.pads;
        let (mut enable, mut out) = (0, 0);
        for pin in 0..62 {
            match pads.drive(pin) {
                Ok(Drive::High) => {
                    enable |= 1 << pin;
                    out |= 1 << pin;
                }
                Ok(Drive::Low) => enable |= 1 << pin,
                _ => {}
            }
        }
        (enable, out)
    }

    /// Reports the pad changes of the last run to the board at their cycles, those of the pins
    /// the chip drove before it or drives now; then the output enable, if it changed.
    fn report_gpio(&mut self) {
        let events = SocBus::take_gpio_events(&mut self.m.bus);
        let (enable, out) = self.pads();
        let outputs = enable | self.drive.0;
        let mut level = self.drive.1;
        let mut i = 0;
        while i < events.len() {
            let cycle = events[i].0;
            let mut changes = Vec::new();
            while i < events.len() && events[i].0 == cycle {
                let (_, pin, high) = events[i];
                if pin < 64 && outputs & 1 << pin != 0 {
                    changes.push((pin, high));
                    level = level & !(1 << pin) | u64::from(high) << pin;
                }
                i += 1;
            }
            if !changes.is_empty() {
                self.m
                    .bus
                    .board
                    .gpio_output_at(cycle, &changes, enable, level & enable);
            }
        }
        if (enable, out) != self.drive {
            let now = self.m.bus.cycles;
            self.m.bus.board.gpio_output_at(now, &[], enable, out);
        }
        self.drive = (enable, out);
    }

    /// After a reset, which the run loop reports itself: what the pads did in it is no change
    /// to report, and the console's input not yet taken is gone with UART0's.
    fn after_any_reset(&mut self) {
        SocBus::take_gpio_events(&mut self.m.bus);
        self.drive = self.pads();
        self.rx.clear();
    }

    /// One byte of the console's input into UART0's RX FIFO, if the FIFO has room and UART0
    /// takes it: esp32sim's S31 refuses host bytes while UART0's clock or receiver is off
    /// (`S31Bus::uart_input`), and the byte then waits for the next character time.
    pub(crate) fn feed_console(&mut self) {
        let Some(&byte) = self.rx.front() else { return };
        let uart = &self.m.bus.inner.periph.uart0;
        if uart.rx_pending() >= RX_FIFO {
            return;
        }
        let before = uart.irq();
        if self.m.bus.inner.uart_input(&[byte]).is_ok() {
            self.rx.pop_front();
            self.m.bus.irq_dirty |= before != self.m.bus.inner.periph.uart0.irq();
        }
    }

    /// The console's input that UART0 has not taken yet.
    #[cfg(test)]
    pub(crate) fn console_queued(&self) -> usize {
        self.rx.len()
    }

    /// Bus cycles of one character, ten bits, at UART0's baud.
    fn char_cycles(&mut self) -> u64 {
        let baud = self
            .m
            .bus
            .inner
            .uart_baud()
            .filter(|&b| b != 0)
            .unwrap_or(DEFAULT_BAUD);
        (PROFILE.cpu_hz * 10).div_ceil(baud)
    }
}

impl Target for S31 {
    type Soc = esp32s31::S31;
    const PROFILE: &'static Profile = &PROFILE;
    fn new(flash_size: usize) -> Self {
        let mut m = esp32s31::machine(MAC, flash_size);
        SocBus::set_flash_size(&mut m.bus, flash_size);
        // without them the flash clock has no source and ESP-IDF aborts at start-up
        let inputs = esp32s31::soc::BoardInputs::devkit_sized(flash_size)
            .expect("the command line allows only the sizes of a W25Q part");
        m.bus
            .set_board_inputs(inputs)
            .expect("the devkit's inputs apply");
        SocBus::set_strap(&mut m.bus, STRAP);
        SocBus::observe_gpio(&mut m.bus, true);
        S31 {
            m,
            rx: VecDeque::new(),
            rx_due: 0,
            drive: (0, 0),
        }
    }
    fn machine(&mut self) -> &mut Machine<esp32s31::S31> {
        &mut self.m
    }
    fn machine_ref(&self) -> &Machine<esp32s31::S31> {
        &self.m
    }
    fn set_psram_size(&mut self, bytes: usize) -> Result<(), String> {
        // a board input, which esp32sim applies again after every chip reset
        let mut inputs = self.m.bus.board_inputs.clone().expect("set in Target::new");
        inputs.psram_bytes = bytes;
        self.m.bus.inner.set_psram_size(bytes)?;
        self.m.bus.board_inputs = Some(inputs);
        Ok(())
    }
    fn install_board(&mut self, board: Option<Box<dyn BoardModel>>) {
        if let Some(board) = board {
            self.m.bus.board = board;
        }
        self.m.bus.attach_board_devices();
    }
    fn attach_wifi(&mut self, _wifi: &cli::Wifi) -> Result<(), String> {
        Err("--wifi: the ESP32-S31 has no Ethernet relay in esp32sim".into())
    }
    fn forward_bt(&mut self, _elf: &esp_soc::elf::Elf, _hci: hci::Shared) -> Result<(), String> {
        Err("--bt: the ESP32-S31 has no Bluetooth in esp32sim".into())
    }
    fn after_reset(&mut self) {
        self.after_any_reset();
    }
}

impl Guest for S31 {
    fn cycles(&self) -> u64 {
        self.m.bus.cycles
    }
    /// Runs to `cycle` in pieces that end where the next byte of the console's input is due,
    /// which goes into UART0's RX FIFO there if it has room, and waits a character time if not.
    fn run_until(&mut self, cycle: u64) -> Stop {
        loop {
            let now = self.m.bus.cycles;
            if !self.rx.is_empty() && self.rx_due <= now {
                self.feed_console();
                self.rx_due = now + self.char_cycles();
            }
            if now >= cycle {
                return Stop::Halted;
            }
            let to = if self.rx.is_empty() {
                cycle
            } else {
                self.rx_due.min(cycle)
            };
            self.m.max_cycles = to;
            let stop = self.m.run(u64::MAX);
            self.report_gpio();
            if !matches!(stop, Stop::Halted) || self.m.bus.cycles < to {
                return stop;
            }
        }
    }
    fn chip_reset(&mut self) -> u32 {
        let hint = self.reset_hint();
        let cause = self.m.reboot();
        self.after_any_reset();
        super::keep_reset_hint(&mut self.m.bus, RESET_HINT, hint);
        cause
    }
    fn reset_hint(&mut self) -> u32 {
        super::read_reset_hint(&mut self.m.bus, RESET_HINT)
    }
    fn power_on_reset(&mut self, _rom: &[u8]) -> u32 {
        // esp32sim's chip reset makes the S31's whole digital side again, its SRAM and LP memory
        // among them, and keeps the ROM it was given; the cause is a power-on's
        self.m.reboot();
        SocBus::set_reset_cause(&mut self.m.bus, esp_periph::rtc_cntl::RST_POWERON);
        self.after_any_reset();
        esp_periph::rtc_cntl::RST_POWERON
    }
    fn idle_until(&mut self, cycle: u64) {
        self.m.bus.cycles = cycle;
    }
    fn uart_route(&self, port: usize) -> UartRoute {
        // no route esp32sim tells (#229, G7): the console is read and written without pins
        super::no_uart(port)
    }
    fn rx_pending(&self, port: usize) -> usize {
        if port == 0 {
            self.m.bus.inner.periph.uart0.rx_pending()
        } else {
            0
        }
    }
    fn uart_receive(&mut self, port: usize, byte: u8) {
        if port == 0 {
            SocBus::uart_input(&mut self.m.bus, 0, &[byte]);
        }
    }
    fn uart_frame_error(&mut self, port: usize) {
        if port == 0 {
            self.m.bus.inner.periph.uart0.int_raw |= esp_periph::uart::INT_FRM_ERR;
            self.m.bus.irq_dirty = true;
        }
    }
    fn take_uart0_console(&mut self) -> Vec<u8> {
        std::mem::take(&mut self.m.console.uart0)
    }
    fn uart0_console_input(&mut self, data: &[u8]) {
        if self.rx.is_empty() {
            self.rx_due = self.rx_due.max(self.m.bus.cycles);
        }
        self.rx.extend(data);
    }
    fn take_usb_console(&mut self) -> Vec<u8> {
        std::mem::take(&mut self.m.console.usb)
    }
    fn usb_console_input(&mut self, data: &[u8]) {
        SocBus::serial_input(&mut self.m.bus, data);
    }
    fn gpio_drive(&self) -> (u64, u64) {
        self.pads()
    }
    fn take_gpio_inputs(&mut self) {
        for edge in self.m.bus.board.take_edges() {
            SocBus::gpio_set_input(&mut self.m.bus, edge.pin, edge.level);
        }
        for pin in self.m.bus.board.released_inputs() {
            SocBus::gpio_release_input(&mut self.m.bus, pin);
        }
        // what the peer drives is no output of the chip's
        SocBus::take_gpio_events(&mut self.m.bus);
    }
    fn start_pacing(&mut self) {
        self.m.rt.enabled = true;
    }
    fn resyncs(&self) -> u64 {
        self.m.rt.resyncs
    }
    fn flash(&self) -> Cow<'_, [u8]> {
        Cow::Borrowed(&self.m.bus.flash)
    }
    fn write_flash(&mut self, offset: usize, data: &[u8]) -> Result<(), String> {
        super::fits(self.m.bus.flash.len(), offset, data.len())?;
        esp_soc::SocBus::write_flash(&mut self.m.bus, offset, data)
    }
}
