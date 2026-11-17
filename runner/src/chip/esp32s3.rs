//! The ESP32-S3 (`--chip esp32s3`): esp32sim's `esp32s3` crate, two LX7 cores at 240 MHz.
//!
//! The S3's bus defers device time (`tick_pending`, delivered by `flush_ticks`), so whatever the
//! run loop reads or changes of the peripherals between slices goes through `flush_ticks` first,
//! and whatever it hands them is followed by `refresh_tick_budget`, as esp32sim's own host inputs
//! are (`esp32s3/src/soc.rs`, `uart_input`, `gpio_set_input`).
use super::{Profile, Target};
use crate::cli;
use crate::hci;
use crate::session::Guest;
use esp_soc::board::BoardModel;
use esp_soc::uart::UartRoute;
use esp_soc::{Machine, SocBus, Stop};
use std::borrow::Cow;

pub const PROFILE: Profile = Profile {
    name: "esp32s3",
    title: "the ESP32-S3",
    cpu_hz: esp32s3::periph::CPU_HZ,
    // GPIO 0 to 21 and 26 to 48 (ESP32-S3 datasheet, section 2.3.1): 22 to 25 are not bonded.
    gpios: ((1 << 49) - 1) & !(0xf << 22),
    input_only: 0,
    uarts: 3,
    deep_sleep: false,
    wifi_station: false,
    wifi: true,
    bt: true,
    // quad or octal PSRAM; esp32sim's MR2 reports the density (`--psram-mb`)
    psram_mb: &[2, 4, 8],
};

/// The MAC esp32sim gives the S3 (`cli/src/lib.rs`, `setup_s3`).
pub const MAC: [u8; 6] = [0x44, 0x1b, 0xf6, 0x75, 0xdc, 0xe0];

impl Target for esp32s3::Machine {
    type Soc = esp32s3::S3;
    const PROFILE: &'static Profile = &PROFILE;
    fn new(flash_size: usize) -> Self {
        // esp32sim's S3 starts with 8 MiB of flash and 2 MiB of PSRAM; the board's sizes replace
        // both, and a board without PSRAM has none
        let mut m = esp32s3::machine(MAC);
        m.bus.set_flash_size(flash_size);
        SocBus::set_psram_size(&mut m.bus, 0).expect("the S3 has PSRAM");
        m
    }
    fn machine(&mut self) -> &mut Machine<esp32s3::S3> {
        self
    }
    fn machine_ref(&self) -> &Machine<esp32s3::S3> {
        self
    }
    fn set_psram_size(&mut self, bytes: usize) -> Result<(), String> {
        SocBus::set_psram_size(&mut self.bus, bytes)
    }
    fn install_board(&mut self, board: Option<Box<dyn BoardModel>>) {
        if let Some(board) = board {
            self.bus.board = board;
        }
        self.bus.attach_board_devices();
    }
    fn attach_wifi(&mut self, wifi: &cli::Wifi) -> Result<(), String> {
        if wifi.station().is_some() {
            return Err("--wifi-sta: the ESP32-S3 has no virtual station in esp32sim".into());
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
        self.bus.periph.wifi.link.report()
    }
    fn after_reset(&mut self) {
        reset_console_rx(self);
    }
}

/// Work around esp32sim 357b0e5's zeroed IO_MUX reset state on U0RXD, GPIO 44, as on the classic
/// chip (`chip::esp32::reset_console_rx`): the ESP32-S3 TRM, table 6.13-1, gives it function 0,
/// U0RXD, input and pull-up enabled, drive strength 2. Set only at reset; what the firmware writes
/// afterwards stays.
pub(crate) fn reset_console_rx(machine: &mut esp32s3::Machine) {
    use emu_core::Bus;
    machine
        .bus
        .write32(0x6000_9004 + 4 * 44, (2 << 10) | (1 << 9) | (1 << 8))
        .expect("U0RXD IO_MUX is mapped");
}

impl Guest for esp32s3::Machine {
    fn cycles(&self) -> u64 {
        self.bus.cycles
    }
    fn run_until(&mut self, cycle: u64) -> Stop {
        self.max_cycles = cycle;
        let stop = self.run(u64::MAX);
        // the devices' time up to the bus's, so that what the run loop reads now is current
        self.bus.flush_ticks();
        stop
    }
    fn chip_reset(&mut self) -> u32 {
        let cause = self.reboot();
        reset_console_rx(self);
        cause
    }
    fn power_on_reset(&mut self, rom: &[u8]) -> u32 {
        use esp32s3::bus::*;
        self.reboot();
        reset_console_rx(self);
        // SRAM, one array behind its instruction and its data windows, and the RTC memories,
        // through the bus, so that the code decoded from them is decoded again
        let bus = &mut self.bus;
        for (low, high) in [
            (DRAM_LOW, DRAM_HIGH),
            (IRAM_LOW, IRAM_HIGH),
            (RTC_FAST_LOW, RTC_FAST_HIGH),
            (RTC_SLOW_LOW, RTC_SLOW_HIGH),
        ] {
            bus.load_bytes(low, &vec![0; (high - low) as usize])
                .expect("the chip's memories are mapped");
        }
        self.load_rom(rom)
            .expect("the ROM loaded at the start of the run");
        // the RTC registers, the RTC timer and RESET_STATE as a new chip has them: POWERON;
        // the analog inputs are the board's, not the chip's
        let mut rtc = esp32s3::periph::Peripherals::new(self.mac).rtc;
        std::mem::swap(&mut rtc.analog, &mut self.bus.periph.rtc.analog);
        self.bus.periph.rtc = rtc;
        self.bus.refresh_tick_budget();
        esp_periph::rtc_cntl::RST_POWERON
    }
    fn idle_until(&mut self, cycle: u64) {
        self.bus.flush_ticks();
        self.bus.cycles = cycle;
        self.bus.refresh_tick_budget();
    }
    fn uart_route(&self, port: usize) -> UartRoute {
        self.bus.periph.uart_route(port)
    }
    fn rx_pending(&self, port: usize) -> usize {
        self.bus.periph.uart[port].rx_pending()
    }
    fn uart_receive(&mut self, port: usize, byte: u8) {
        // straight into the RX FIFO, which drops what does not fit and raises RXFIFO_OVF; not
        // `uart_input`, which queues it and releases it at the baud rate
        let uart = &mut self.bus.periph.uart[port];
        uart.host_input(&[byte]);
        self.bus.irq_dirty = true;
        self.bus.refresh_tick_budget();
    }
    fn uart_frame_error(&mut self, port: usize) {
        self.bus.periph.uart[port].int_raw |= esp_periph::uart::INT_FRM_ERR;
        self.bus.irq_dirty = true;
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
        let taken = SocBus::receive_ethernet_frame(&mut self.bus, frame).is_ok();
        self.bus.refresh_tick_budget();
        taken
    }
    fn wifi_access_point(&mut self, wifi: &cli::Wifi) {
        self.bus.flush_ticks();
        super::ap_on_air(&mut self.bus.periph.wifi.link, wifi, &self.bus.debug);
        self.bus.refresh_tick_budget();
    }
    fn wifi_leave(&mut self) -> bool {
        self.bus.flush_ticks();
        let now_us = self.bus.cycles / (esp32s3::periph::CPU_HZ / 1_000_000);
        let left = super::ap_leave(&mut self.bus.periph.wifi.link, now_us);
        self.bus.refresh_tick_budget();
        left
    }
    fn wifi_drops(&self) -> (u64, u64) {
        let link = &self.bus.periph.wifi.link;
        (link.rx_dropped(), link.tx_dropped())
    }
    fn wifi_forget(&mut self) {
        self.bus.flush_ticks();
        self.bus.periph.wifi.link.attach(None, None);
        self.bus.refresh_tick_budget();
    }
    fn start_pacing(&mut self) {
        self.rt.enabled = true;
    }
    fn resyncs(&self) -> u64 {
        self.rt.resyncs
    }
    fn flash(&self) -> Cow<'_, [u8]> {
        super::flash_without_trampoline(&self.bus.flash, &self.bus.ble.original_flash)
    }
}
