//! The ESP32-C6 (`--chip esp32c6`): esp32sim's `esp32c6` crate, one RISC-V core at 160 MHz.
use super::{Profile, Target};
use crate::cli;
use crate::hci;
use crate::session::Guest;
use esp_soc::board::BoardModel;
use esp_soc::uart::UartRoute;
use esp_soc::{Machine, SocBus, Stop};
use std::borrow::Cow;

pub const PROFILE: Profile = Profile {
    name: "esp32c6",
    title: "the ESP32-C6",
    cpu_hz: esp32c6::periph::CPU_HZ,
    // GPIO 0 to 30 (ESP32-C6 datasheet, section 2.3).
    gpios: (1 << 31) - 1,
    input_only: 0,
    uarts: 2,
    uart_pins: true,
    sd_spi: true,
    wifi_station: false,
    wifi: true,
    bt: true,
    usb_serial_jtag: true,
    psram_mb: &[],
};

/// The MAC esp32sim gives the C6 (`cli/src/lib.rs`, `setup_c6`).
pub const MAC: [u8; 6] = [0xdc, 0x1e, 0xd5, 0x6e, 0x8c, 0xdc];

impl Target for esp32c6::Machine {
    type Soc = esp32c6::C6;
    const PROFILE: &'static Profile = &PROFILE;
    fn new(flash_size: usize) -> Self {
        let mut m = esp32c6::machine(MAC, flash_size);
        // the JEDEC capacity the guest reads follows the size, as in esp32sim
        m.bus.set_flash_size(flash_size);
        m
    }
    fn machine(&mut self) -> &mut Machine<esp32c6::C6> {
        self
    }
    fn machine_ref(&self) -> &Machine<esp32c6::C6> {
        self
    }
    fn install_board(&mut self, board: Option<Box<dyn BoardModel>>) {
        if let Some(board) = board {
            self.bus.board = board;
        }
        self.bus.attach_board_devices();
    }
    fn attach_wifi(&mut self, wifi: &cli::Wifi) -> Result<(), String> {
        if wifi.station().is_some() {
            return Err("--wifi-sta: the ESP32-C6 has no virtual station in esp32sim".into());
        }
        Guest::wifi_access_point(self, wifi);
        SocBus::set_ethernet_relay(&mut self.bus, true)
    }
    fn forward_bt(&mut self, elf: &esp_soc::elf::Elf, hci: hci::Shared) -> Result<(), String> {
        SocBus::enable_ble(&mut self.bus, elf)?;
        self.bus.ble.session = esp_soc::ble::peer::Session::new(
            Box::new(hci::Forwarder(hci)),
            esp_soc::ble::Link::default(),
        );
        Ok(())
    }
    fn wifi_report(&self) -> String {
        self.bus.periph.wifi_mac.link.report()
    }
    fn after_reset(&mut self) {
        reset_console_rx(self);
    }
}

/// Work around esp32sim 357b0e5's zeroed IO_MUX reset state on U0RXD, GPIO 17, as on the
/// classic chip (`chip::esp32::reset_console_rx`): the ESP32-C6 TRM, table 7.12-1, gives it function 0, U0RXD, input and
/// pull-up enabled, drive strength 2. Set only at reset; what the firmware writes afterwards stays.
pub(crate) fn reset_console_rx(machine: &mut esp32c6::Machine) {
    use emu_core::Bus;
    machine
        .bus
        .write32(0x6009_0000 + 4 + 4 * 17, (2 << 10) | (1 << 9) | (1 << 8))
        .expect("U0RXD IO_MUX is mapped");
}

/// Bus cycles in one tick of the 16 MHz SYSTIMER, `ClockDomain::Systimer` of esp32sim's clock
/// tree.
const SYSTIMER_TICK: u64 = super::clock_divider(
    &esp32c6::periph::Peripherals::CLOCKS,
    emu_core::ClockDomain::Systimer,
);

impl Guest for esp32c6::Machine {
    fn cycles(&self) -> u64 {
        self.bus.cycles
    }
    fn run_until(&mut self, cycle: u64) -> Stop {
        self.max_cycles = cycle;
        self.run(u64::MAX)
    }
    fn chip_reset(&mut self) -> u32 {
        let cause = self.reboot();
        reset_console_rx(self);
        cause
    }
    fn power_on_reset(&mut self, rom: &[u8]) -> u32 {
        use esp32c6::bus::*;
        self.reboot();
        reset_console_rx(self);
        // SRAM and the LP SRAM, through the bus, so that the code decoded from them is decoded
        // again
        for (low, high) in [(SRAM_LOW, SRAM_HIGH), (LP_SRAM_LOW, LP_SRAM_HIGH)] {
            self.bus
                .load_bytes(low, &vec![0; (high - low) as usize])
                .expect("the chip's memories are mapped");
        }
        self.load_rom(rom)
            .expect("the ROM loaded at the start of the run");
        // the LP_SYS registers and timer, and the reset cause, as a new chip has them: POWERON
        self.bus.periph.lpsys = esp32c6::periph::LpSys::new();
        esp_periph::rtc_cntl::RST_POWERON
    }
    fn idle_until(&mut self, cycle: u64) {
        self.bus.cycles = cycle;
    }
    fn uart_route(&self, port: usize) -> UartRoute {
        if port >= PROFILE.uarts {
            return super::no_uart(port);
        }
        self.bus.periph.uart_route(port)
    }
    fn rx_pending(&self, port: usize) -> usize {
        self.bus.periph.uart.get(port).map_or(0, |u| u.rx_pending())
    }
    fn uart_receive(&mut self, port: usize, byte: u8) {
        // straight into the RX FIFO, which drops what does not fit and raises RXFIFO_OVF; not
        // `uart_input`, which queues it and releases it at the baud rate
        if let Some(uart) = self.bus.periph.uart.get_mut(port) {
            uart.host_input(&[byte]);
            self.bus.irq_dirty = true;
        }
    }
    fn uart_frame_error(&mut self, port: usize) {
        if let Some(uart) = self.bus.periph.uart.get_mut(port) {
            uart.int_raw |= esp_periph::uart::INT_FRM_ERR;
            self.bus.irq_dirty = true;
        }
    }
    fn take_usb_console(&mut self) -> Vec<u8> {
        // esp32sim's `drain_console`, inside `run`, has moved what the controller flushed there
        std::mem::take(&mut self.console.usb)
    }
    fn usb_console_input(&mut self, data: &[u8]) {
        SocBus::serial_input(&mut self.bus, data);
    }
    fn bt_controller_inits(&self) -> u64 {
        // the stub of `--no-bt` is the only one the runner puts in
        self.stub_hits
    }
    fn uptime(&mut self) -> crate::session::Uptime {
        super::systimer_uptime(&self.bus.periph.systimer, self.cycles(), SYSTIMER_TICK)
    }
    fn gpio_drive(&self) -> (u64, u64) {
        let gpio = &self.bus.periph.gpio;
        (gpio.enable, gpio.out)
    }
    fn take_gpio_inputs(&mut self) {
        for edge in self.bus.board.take_edges() {
            SocBus::gpio_set_input(&mut self.bus, edge.pin, edge.level);
        }
        for pin in self.bus.board.released_inputs() {
            SocBus::gpio_release_input(&mut self.bus, pin);
        }
    }
    fn take_eth_frames(&mut self) -> Vec<Vec<u8>> {
        SocBus::take_ethernet_frames(&mut self.bus)
    }
    fn eth_input(&mut self, frame: &[u8]) -> bool {
        SocBus::receive_ethernet_frame(&mut self.bus, frame).is_ok()
    }
    fn wifi_access_point(&mut self, wifi: &cli::Wifi) {
        super::ap_on_air(&mut self.bus.periph.wifi_mac.link, wifi, &self.bus.debug);
    }
    fn wifi_leave(&mut self) -> bool {
        let now_us = self.bus.cycles / (esp32c6::periph::CPU_HZ / 1_000_000);
        super::ap_leave(&mut self.bus.periph.wifi_mac.link, now_us)
    }
    fn wifi_drops(&self) -> (u64, u64) {
        let link = &self.bus.periph.wifi_mac.link;
        (link.rx_dropped(), link.tx_dropped())
    }
    fn wifi_forget(&mut self) {
        self.bus.periph.wifi_mac.link.attach(None, None);
    }
    fn start_pacing(&mut self) {
        self.rt.enabled = true;
    }
    fn resyncs(&self) -> u64 {
        self.rt.resyncs
    }
    fn flash(&self) -> Cow<'_, [u8]> {
        // esp32sim keeps the C6's copy of the code under its trampoline to itself
        // (`esp32c6/src/ble.rs`, `original_flash`): with `--bt` the dump holds the trampoline
        Cow::Borrowed(&self.bus.flash)
    }
}
