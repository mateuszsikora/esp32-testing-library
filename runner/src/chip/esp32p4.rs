//! The ESP32-P4 (`--chip esp32p4`): esp32sim's `esp32p4` crate, two RISC-V cores at 360 MHz.
//!
//! esp32sim models two silicon revisions, as two machines: rev < 3 (v0.x, v1.x,
//! `Machine<Esp32p4<RevLt3>>`) and rev ≥ 3 (v3.x, `Machine<Esp32p4<RevGe3>>`), which differ in
//! ISA, interrupt controller, memory layout and mask ROM. An image runs only on the revision it
//! was built for, so the run takes the revision from the images' headers, as esp32sim's own
//! command line does, by a rule of its own ([`rev3`]).
//!
//! esp32sim's P4 tells the board its GPIO edges but none of its UART bytes or routes (#229, G6):
//! UART0, the console, has no pins here. The console comes from UART0's TX FIFO as esp32sim
//! drains it, and the peer's `console_rx` goes into UART0's RX FIFO as far as it has room, the
//! rest at UART0's baud rate ([`super::Profile::uart_pins`]).
use super::{Profile, Target};
use crate::cli;
use crate::hci;
use crate::session::Guest;
use esp_soc::board::BoardModel;
use esp_soc::uart::UartRoute;
use esp_soc::{Machine, SocBus, Stop};
use std::borrow::Cow;

pub const PROFILE: Profile = Profile {
    name: "esp32p4",
    title: "the ESP32-P4",
    cpu_hz: esp32p4::periph::CPU_HZ,
    // GPIO 0 to 54 (ESP32-P4 datasheet, section 2.3).
    gpios: (1 << 55) - 1,
    input_only: 0,
    uarts: 2,
    uart_pins: false,
    sd_spi: false,
    wifi_station: false,
    wifi: false,
    bt: false,
    usb_serial_jtag: true,
    // the in-package PSRAM of the ESP32-P4NRW16X and P4NRW32X; esp32sim's AP Memory part has
    // the size the bus is given
    psram_mb: &[16, 32],
};

/// The MAC esp32sim gives the P4 (`cli/src/lib.rs`, `setup_p4`).
pub const MAC: [u8; 6] = [0x30, 0xed, 0xa0, 0x00, 0x11, 0x22];

/// LP_SYSTEM_REG_LP_STORE6, `RTC_RESET_CAUSE_REG` of ESP-IDF's
/// `esp_rom/esp32p4/include/esp32p4/rom/rtc.h`: `DR_REG_LP_SYS_BASE + 0x44` (v5.5.4
/// `soc/esp32p4/register/hw_ver3/soc/lp_system_reg.h`, the same in `hw_ver1`). esp32sim's reset
/// keeps it with the rest of LP_SYS (`esp32p4/src/soc.rs`, `reboot`).
pub(crate) const RESET_HINT: u32 = 0x5011_0044;

/// The P4 machine of revision `R`.
pub type P4<R> = Machine<esp32p4::Esp32p4<R>>;

/// Whether the images, as `--flash-at` places them, are built for rev ≥ 3
/// (`esp32p4::built_for_rev3`). `bootloader` is the header at the flash's bootloader offset,
/// 0x2000, as the last file that covers it put it there: a bootloader image of its own, or one
/// inside an image of the whole flash from 0x0. When it is a P4 image it decides; otherwise the
/// first of `others`, each file's own first bytes, that is a P4 image does; none counts as rev < 3.
/// esp32sim's command line asks its `--bootloader` or the image `--flash-at` places at 0x2000,
/// then `--app`, then the other images, then the bootloader inside `--flash-image`, and takes the
/// first file's answer even when it is no P4 image (`cli/src/lib.rs`, `p4_rev3`); the runner
/// passes over a file that is none.
pub fn rev3(bootloader: Option<&[u8]>, others: &[Vec<u8>]) -> bool {
    bootloader
        .filter(|data| is_p4_image(data))
        .or_else(|| {
            others
                .iter()
                .map(Vec::as_slice)
                .find(|data| is_p4_image(data))
        })
        .is_some_and(esp32p4::built_for_rev3)
}

/// Half bus cycles in one tick of the P4's 16 MHz SYSTIMER: esp32sim's `SystimerP4` takes the
/// XTAL domain of its clock tree and counts 2 ticks for every 5 of it (`esp32p4/src/periph.rs`),
/// so a tick is 22.5 bus cycles at 360 MHz.
const SYSTIMER_HALF_TICK: u64 = super::clock_divider(
    &esp32p4::periph::Peripherals::CLOCKS,
    emu_core::ClockDomain::Xtal,
) * 5;

/// `Guest::uptime` on the P4, as `chip::systimer_uptime` has it on the other SYSTIMER chips, with
/// a tick that is no whole number of bus cycles: unit 0 read zero `unit[0]` ticks, rounded to bus
/// cycles, before `now`, less the part of a tick it has counted since its last.
fn systimer_uptime(systimer: &esp_periph::Systimer, now: u64) -> crate::session::Uptime {
    use crate::session::Uptime;
    if systimer.conf & 1 << 30 == 0 {
        return Uptime::Stopped;
    }
    match systimer.unit[0]
        .checked_mul(SYSTIMER_HALF_TICK)
        .map(|half| half / 2)
        .and_then(|counted| now.checked_sub(counted))
    {
        Some(latest) => Uptime::Zero {
            earliest: latest.saturating_sub(SYSTIMER_HALF_TICK.div_ceil(2)),
            latest,
        },
        None => Uptime::BeforePowerOn,
    }
}

/// The P4's second-stage bootloader offset (`Soc::BOOTLOADER_OFFSET`).
pub const BOOTLOADER_OFFSET: usize = 0x2000;

/// An image header (`esp_image_header_t`) for the P4: magic 0xE9, chip ID 0x0012 at byte 12.
fn is_p4_image(data: &[u8]) -> bool {
    data.len() >= 24 && data[0] == 0xE9 && data[12..14] == [0x12, 0x00]
}

impl<R: esp32p4::Revision> Target for P4<R> {
    type Soc = esp32p4::Esp32p4<R>;
    const PROFILE: &'static Profile = &PROFILE;
    fn new(flash_size: usize) -> Self {
        let mut m = esp32p4::machine_rev::<R>(MAC, flash_size);
        // the JEDEC capacity the guest reads follows the size, as in esp32sim
        SocBus::set_flash_size(&mut m.bus, flash_size);
        m
    }
    fn machine(&mut self) -> &mut Machine<esp32p4::Esp32p4<R>> {
        self
    }
    fn machine_ref(&self) -> &Machine<esp32p4::Esp32p4<R>> {
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
    fn attach_wifi(&mut self, _wifi: &cli::Wifi) -> Result<(), String> {
        Err("--wifi: the ESP32-P4 has no Wi-Fi in esp32sim".into())
    }
    fn forward_bt(&mut self, _elf: &esp_soc::elf::Elf, _hci: hci::Shared) -> Result<(), String> {
        Err("--bt: the ESP32-P4 has no Bluetooth in esp32sim".into())
    }
}

impl<R: esp32p4::Revision> Guest for P4<R> {
    fn cycles(&self) -> u64 {
        self.bus.cycles
    }
    fn run_until(&mut self, cycle: u64) -> Stop {
        self.max_cycles = cycle;
        let stop = self.run(u64::MAX);
        // esp32sim's P4 tells the board a change of a pin's driven level, at its cycle, but not
        // an output enabled at the level it already had: the pads as they are now, which the
        // board takes only if they differ from what it last reported
        let gpio = &self.bus.periph.gpio.g;
        let (enable, out) = (gpio.enable, gpio.out);
        let now = self.bus.cycles;
        self.bus.board.gpio_output_at(now, &[], enable, out);
        stop
    }
    fn chip_reset(&mut self) -> u32 {
        self.reboot()
    }
    fn reset_hint(&mut self) -> u32 {
        super::read_reset_hint(&mut self.bus, RESET_HINT)
    }
    fn power_on_reset(&mut self, rom: &[u8]) -> u32 {
        use esp32p4::bus::*;
        self.reboot();
        // L2MEM, the TCM and the LP RAM, through the bus, so that the code decoded from them is
        // decoded again; esp32sim's chip reset keeps all three
        for (low, high) in [
            (L2MEM_LOW, L2MEM_HIGH),
            (TCM_LOW, TCM_HIGH),
            (LP_RAM_LOW, LP_RAM_HIGH),
        ] {
            self.bus
                .load_bytes(low, &vec![0; (high - low) as usize])
                .expect("the chip's memories are mapped");
        }
        self.load_rom(rom)
            .expect("the ROM loaded at the start of the run");
        // LP_SYSTEM, LP_CLKRST and LP_TIMER, which esp32sim's chip reset keeps, as a new chip
        // has them: the reset cause POWERON
        self.bus.periph.lp = esp32p4::periph::LpAon::new();
        esp_periph::rtc_cntl::RST_POWERON
    }
    fn idle_until(&mut self, cycle: u64) {
        self.bus.cycles = cycle;
    }
    fn uart_route(&self, port: usize) -> UartRoute {
        // no route esp32sim tells (#229, G6): the console is read and written without pins
        super::no_uart(port)
    }
    fn rx_pending(&self, port: usize) -> usize {
        self.bus.periph.uart.get(port).map_or(0, |u| u.rx_pending())
    }
    fn uart_receive(&mut self, port: usize, byte: u8) {
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
    fn take_uart0_console(&mut self) -> Vec<u8> {
        // esp32sim's `drain_console`, inside `run`, has moved what UART0 sent there
        std::mem::take(&mut self.console.uart0)
    }
    fn uart0_console_input(&mut self, data: &[u8]) {
        // into the RX FIFO as far as it has room, the rest queued and released at UART0's baud
        // rate by the bus (`Uart::host_input_queued`)
        SocBus::uart_input(&mut self.bus, 0, data);
    }
    fn take_usb_console(&mut self) -> Vec<u8> {
        std::mem::take(&mut self.console.usb)
    }
    fn uptime(&mut self) -> crate::session::Uptime {
        systimer_uptime(&self.bus.periph.systimer.st, self.bus.cycles)
    }
    fn usb_console_input(&mut self, data: &[u8]) {
        SocBus::serial_input(&mut self.bus, data);
    }
    fn gpio_drive(&self) -> (u64, u64) {
        let gpio = &self.bus.periph.gpio.g;
        (gpio.enable, gpio.out)
    }
    fn take_gpio_inputs(&mut self) {
        for edge in self.bus.board.take_edges() {
            SocBus::gpio_set_input(&mut self.bus, edge.pin, edge.level);
        }
        // esp32sim's P4 leaves `SocBus::gpio_release_input` to the trait's default, which does
        // nothing: its GPIO's own release, as the other chips' buses call it
        for pin in self.bus.board.released_inputs() {
            self.bus.irq_dirty |= self.bus.periph.gpio.g.release_input(pin);
        }
    }
    fn start_pacing(&mut self) {
        self.rt.enabled = true;
    }
    fn resyncs(&self) -> u64 {
        self.rt.resyncs
    }
    fn flash(&self) -> Cow<'_, [u8]> {
        Cow::Borrowed(&self.bus.flash)
    }
    fn write_flash(&mut self, offset: usize, data: &[u8]) -> Result<(), String> {
        super::fits(self.bus.flash.len(), offset, data.len())?;
        esp_soc::SocBus::write_flash(&mut self.bus, offset, data)
    }
}
