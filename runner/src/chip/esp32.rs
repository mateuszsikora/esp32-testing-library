//! The classic ESP32 (`--chip esp32`): esp32sim's `esp32` crate, two LX6 cores at 240 MHz.
use super::{Profile, Target};
use crate::cli;
use crate::hci;
use crate::session::Guest;
use esp_soc::board::BoardModel;
use esp_soc::uart::UartRoute;
use esp_soc::wifi;
use esp_soc::{Machine, SocBus, Stop};
use std::borrow::Cow;

pub const PROFILE: Profile = Profile {
    name: "esp32",
    title: "the classic ESP32",
    cpu_hz: esp32::periph::CPU_HZ,
    // 0 to 39 but 24 and 28 to 31, as the board profiles have them (`esp32tl/boards`, `GPIOS`).
    // esp32sim's `gpio_state` also leaves out GPIO 20, which only some packages bond
    // (`esp32/src/soc.rs:212`), but its `set_input` takes a level there; it ignores one on 40
    // and above (`esp32/src/periph.rs:386-389`).
    gpios: ((1 << 40) - 1) & !(1 << 24 | 0xf << 28),
    // GPIO 34 to 39
    input_only: 0xfc << 32,
    uarts: 3,
    uart_pins: true,
    sd_spi: true,
    wifi_station: true,
    wifi: true,
    bt: true,
    // no USB-Serial/JTAG on the classic chip
    usb_serial_jtag: false,
    psram_mb: &[4],
};

/// The MAC esp32sim gives the classic chip, so a run here and the same run in esp32sim agree.
pub const MAC: [u8; 6] = [0x24, 0x6f, 0x28, 0x00, 0x11, 0x22];

impl Target for esp32::Machine {
    type Soc = esp32::Esp32;
    const PROFILE: &'static Profile = &PROFILE;
    fn new(flash_size: usize) -> Self {
        let mut m = esp32::machine(MAC, flash_size);
        // the JEDEC capacity the guest reads follows the size, as in esp32sim
        m.bus.set_flash_size(flash_size);
        m
    }
    fn machine(&mut self) -> &mut Machine<esp32::Esp32> {
        self
    }
    fn machine_ref(&self) -> &Machine<esp32::Esp32> {
        self
    }
    fn set_psram_size(&mut self, bytes: usize) -> Result<(), String> {
        self.bus.set_psram_size(bytes)
    }
    fn install_board(&mut self, board: Option<Box<dyn BoardModel>>) {
        if let Some(board) = board {
            self.bus.board = board;
        }
        self.bus.attach_board_devices();
    }
    fn attach_wifi(&mut self, wifi: &cli::Wifi) -> Result<(), String> {
        // esp32sim's access point, with the network behind it on the socket: the relay hands the
        // run loop the station's Ethernet frames instead of esp32sim's own network
        Guest::wifi_access_point(self, wifi);
        self.bus.set_ethernet_relay(true)
    }
    fn forward_bt(&mut self, elf: &esp_soc::elf::Elf, hci: hci::Shared) -> Result<(), String> {
        // `enable_ble` replaces the whole `Ble`, its session included, so the forwarder goes in
        // after it; `reboot()` keeps the session from then on, and resets it (`Ble::reset`)
        self.bus.enable_ble(elf)?;
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

/// Where the classic chip's SRAM and RTC memories are on the data bus, besides the DRAM, which
/// ends at `esp32::bus::DRAM_HIGH` (`esp32/src/bus.rs`, `SocBus::memory`).
const CACHE_RAM: u32 = 0x4007_0000;
const RTC_FAST: u32 = 0x3ff8_0000;
const RTC_SLOW: u32 = 0x5000_0000;

/// RTC_CNTL, as esp32sim keeps it: offsets into its register RAM (`esp32/src/periph.rs`,
/// `ClassicRtc`), which keeps WAKEUP_STATE, at 0x38 on the bus, at 0x34. The registers are ESP-IDF
/// v5.3's `components/soc/esp32/register/soc/rtc_cntl_reg.h`.
pub(crate) mod rtc {
    /// SLP_TIMER0 and SLP_TIMER1: the sleep timer's target, 48 bits of RTC slow ticks.
    pub const SLP_TIMER0: u32 = 0x04;
    pub const SLP_TIMER1: u32 = 0x08;
    /// STATE0: SLEEP_EN, bit 31, set to sleep; esp32sim clears it at the wake.
    pub const STATE0: u32 = 0x18;
    /// WAKEUP_STATE, whose WAKEUP_ENA, `[21:11]`, has RTC_TIMER_TRIG_EN as its bit 3.
    pub const WAKEUP_ENA: u32 = 0x34;
    pub const TIMER_TRIG_EN: u32 = 1 << (11 + 3);
    /// DIG_PWC: DG_WRAP_PD_EN, bit 31, the digital domain powered down, which makes the sleep a
    /// deep one (`esp32/src/sleep.rs`, `enter_deep_sleep`).
    pub const DIG_PWC: u32 = 0x84;
    /// WDTCONFIG0: the RTC watchdog, enabled with bit 31.
    pub const WDTCONFIG0: u32 = 0x8c;
    /// Bus cycles in one RTC slow tick: the divider of esp32sim's `RtcSlow` clock domain, 1600 at
    /// the pin, for 150 kHz (`esp32/src/periph.rs`, `device_set!`). A pin without the domain fails
    /// the build here.
    pub const SLOW_TICK: u64 = {
        let clocks = esp32::periph::Peripherals::CLOCKS;
        let mut i = 0;
        loop {
            if matches!(clocks[i].0, emu_core::ClockDomain::RtcSlow) {
                break clocks[i].1;
            }
            i += 1;
        }
    };
}
/// TIMG0's LACT, the counter ESP-IDF's `esp_timer` counts on with `CONFIG_ESP_TIMER_IMPL_TG0_LAC`,
/// the classic chip's only implementation (`esp_timer_impl_lac.c`): its registers as offsets into
/// the timer group (ESP-IDF v5.5 `components/soc/esp32/register/soc/timer_group_reg.h`), which
/// esp32sim's `ClassicTimer` reads without side effects (`esp32/src/timers.rs`).
pub(crate) mod lact {
    /// LACTCONFIG: LACT_EN, bit 31, and LACT_DIVIDER, `[28:13]`.
    pub const CONFIG: u32 = 0x70;
    pub const EN: u32 = 1 << 31;
    /// LACTLOADLO and LACTLOADHI: the count LACTLOAD loads.
    pub const LOAD_LO: u32 = 0x8c;
    pub const LOAD_HI: u32 = 0x90;
    /// Bus cycles in one APB tick, the clock the LACT divides, as esp32sim has it
    /// (`Peripherals::apb_divider`, `esp32/src/periph.rs`): 3 with the CPU on the PLL, for 80 MHz,
    /// and 6 otherwise, for 40 MHz. RTC_CNTL_CLK_CONF is at 0x70 of its register RAM; SOC_CLK_SEL,
    /// `[28:27]`, is 1 for the PLL.
    pub fn apb_tick(rtc: &esp_periph::RegRam) -> u64 {
        if (rtc.read(0x70) >> 27) & 3 == 1 {
            3
        } else {
            6
        }
    }
    /// The divider LACT_DIVIDER sets: 0 is 65536 and 1 is 2, as esp32sim counts it
    /// (`Timer::divisor`, `esp-periph/src/timg.rs`).
    pub fn divider(config: u32) -> u64 {
        match (config >> 13) & 0xffff {
            0 => 65536,
            1 => 2,
            d => u64::from(d),
        }
    }
}

/// How many RTC slow ticks before the alarm `sleep_until` stops: esp32sim's clock tree carries a
/// phase between bus cycles and slow ticks that the runner cannot read, so the last ticks go
/// through esp32sim's own idle rounds, which wake the chip at the tick its alarm is due.
pub(crate) const WAKE_MARGIN_TICKS: u64 = 2;
/// The longest step of `sleep_until`, in bus cycles, about 9 s: `Bus::tick` takes a `u32` of CPU
/// cycles, and the CPU's clock is at most the bus's.
pub(crate) const SLEEP_STEP: u64 = 1 << 31;

/// Work around esp32sim 357b0e5's zeroed IO_MUX reset state on U0RXD.
/// ESP32 TRM table 6.10-1 specifies reset configuration 3: input and pull-up enabled.
/// Set it only at reset; subsequent firmware routing and input-disable writes remain effective.
pub(crate) fn reset_console_rx(machine: &mut esp32::Machine) {
    use emu_core::Bus;
    machine
        .bus
        .write32(0x3ff4_9084, (2 << 10) | (1 << 9) | (1 << 8))
        .expect("U0RXD IO_MUX is mapped");
}

impl Guest for esp32::Machine {
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
        // the cores, the digital peripherals and PSRAM, as at any reset
        self.reboot();
        reset_console_rx(self);
        // what keeps its contents through `reboot` and not without power: through the bus, so
        // that the code decoded from it is decoded again
        let bus = &mut self.bus;
        let memories = [
            (
                esp32::bus::DRAM_HIGH - bus.dram.len() as u32,
                bus.dram.len(),
            ),
            (esp32::bus::IRAM_LOW, bus.iram.len()),
            (CACHE_RAM, bus.cache_ram.len()),
            (RTC_FAST, bus.rtc_fast.len()),
            (RTC_SLOW, bus.rtc_slow.len()),
        ];
        for (address, len) in memories {
            bus.load_bytes(address, &vec![0; len])
                .expect("the chip's memories are mapped");
        }
        // the ROM's data in DRAM, as its reset handler finds it after the first power-on
        self.load_rom(rom)
            .expect("the ROM loaded at the start of the run");
        // the RTC registers, the RTC timer and RESET_STATE as a new chip has them: POWERON
        self.bus.periph.rtc = esp32::periph::Peripherals::new(self.mac).rtc;
        esp_periph::rtc_cntl::RST_POWERON
    }
    fn idle_until(&mut self, cycle: u64) {
        self.bus.cycles = cycle;
    }
    fn uart_route(&self, port: usize) -> UartRoute {
        self.bus.periph.uart_route(port)
    }
    fn rx_pending(&self, port: usize) -> usize {
        self.bus.periph.uart[port].rx_pending()
    }
    fn uart_receive(&mut self, port: usize, byte: u8) {
        // the shared `Uart` that `ClassicUart` wraps, which drops what does not fit and raises
        // RXFIFO_OVF (`esp-periph/src/uart.rs:50-58`), and queues nothing; `ClassicUart::host_input`
        // would queue it and release it at the baud rate, and count it as an autobaud edge
        // (`esp32/src/periph.rs:596-601`)
        self.bus.periph.uart[port].0.host_input(&[byte]);
        self.bus.irq_dirty = true;
    }
    fn uart_frame_error(&mut self, port: usize) {
        self.bus.periph.uart[port].0.int_raw |= esp_periph::uart::INT_FRM_ERR;
        self.bus.irq_dirty = true;
    }
    fn gpio_drive(&self) -> (u64, u64) {
        let gpio = &self.bus.periph.gpio.gpio;
        (gpio.enable, gpio.out)
    }
    fn take_gpio_inputs(&mut self) {
        // what the bus does with them at its next tick (`deliver_board_inputs`)
        for edge in self.bus.board.take_edges() {
            SocBus::gpio_set_input(&mut self.bus, edge.pin, edge.level);
        }
        for pin in self.bus.board.released_inputs() {
            SocBus::gpio_release_input(&mut self.bus, pin);
        }
    }
    fn station_state(&self) -> Option<(bool, u64)> {
        self.bus
            .periph
            .wifi
            .link
            .station()
            .map(|s| (s.state == wifi::sta::StaLink::Joined, s.stats.0))
    }
    fn clear_station_relay(&mut self) {
        let link = &mut self.bus.periph.wifi.link;
        link.set_relay(false);
        link.set_relay(true);
    }
    fn take_eth_frames(&mut self) -> Vec<Vec<u8>> {
        SocBus::take_ethernet_frames(&mut self.bus)
    }
    fn eth_input(&mut self, frame: &[u8]) -> bool {
        // also refused without an access point, which the run loop never asks for
        SocBus::receive_ethernet_frame(&mut self.bus, frame).is_ok()
    }
    fn wifi_access_point(&mut self, wifi: &cli::Wifi) {
        if let Some(cfg) = wifi.station() {
            SocBus::attach_wifi_station(&mut self.bus, cfg)
                .expect("exclusive Wi-Fi mode checked by CLI");
            return;
        }
        // a fresh access point, as at power-on: the station must join it again
        super::ap_on_air(&mut self.bus.periph.wifi.link, wifi, &self.bus.debug);
    }
    fn wifi_leave(&mut self) -> bool {
        let now_us = self.bus.cycles / (esp32::periph::CPU_HZ / 1_000_000);
        super::ap_leave(&mut self.bus.periph.wifi.link, now_us)
    }
    fn wifi_drops(&self) -> (u64, u64) {
        let link = &self.bus.periph.wifi.link;
        (link.rx_dropped(), link.tx_dropped())
    }
    fn wifi_forget(&mut self) {
        self.bus.periph.wifi.link.attach(None, None);
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
    fn sleeps_through(&self) -> bool {
        // what esp32sim's `enter_deep_sleep` asks of the RTC registers, and what its
        // `peripheral_event` does to the cores then (`esp32/src/soc.rs`); a reset not yet taken
        // ends the sleep, and the RTC watchdog counts through it, so a stage due in a long step
        // would fire at the step's end, up to 9 s late
        let rtc = &self.bus.periph.rtc.0;
        rtc.ram.read(rtc::STATE0) & 1 << 31 != 0
            && rtc.ram.read(rtc::DIG_PWC) & 1 << 31 != 0
            && self.cores.iter().all(|c| c.waiting && c.intenable == 0)
            && !rtc.sw_reset
            && rtc.ram.read(rtc::WDTCONFIG0) & 1 << 31 == 0
    }
    fn sleep_until(&mut self, cycle: u64) {
        use emu_core::Bus;
        while self.sleeps_through() {
            let rtc = &self.bus.periph.rtc.0;
            let mut stop = cycle;
            if rtc.ram.read(rtc::WAKEUP_ENA) & rtc::TIMER_TRIG_EN != 0 {
                let target = u64::from(rtc.ram.read(rtc::SLP_TIMER1) & 0xffff) << 32
                    | u64::from(rtc.ram.read(rtc::SLP_TIMER0));
                // a target of 0 was never written, and wakes nothing (`sleep_ticks_left`)
                if target != 0 {
                    let left = target.saturating_sub(rtc.slow_ticks);
                    if left <= WAKE_MARGIN_TICKS {
                        return;
                    }
                    stop = stop.min(self.bus.cycles + (left - WAKE_MARGIN_TICKS) * rtc::SLOW_TICK);
                }
            }
            if self.bus.cycles >= stop {
                return;
            }
            let step = (stop - self.bus.cycles).min(SLEEP_STEP);
            let cpu = SocBus::cpu_cycles_for_bus(&self.bus, step);
            Bus::tick(&mut self.bus, cpu as u32);
        }
    }
    fn bt_controller_inits(&self) -> u64 {
        // the stub of `--no-bt` is the only one the runner puts in
        self.stub_hits
    }
    fn uptime(&mut self) -> crate::session::Uptime {
        use crate::session::Uptime;
        use esp_periph::Device;
        // esp32sim keeps the LACT's count to itself: what the registers say is whether it
        // counts, and from what
        let apb_tick = lact::apb_tick(&self.bus.periph.rtc.0.ram);
        let timer = &mut self.bus.periph.timg[0];
        let config = timer.read(lact::CONFIG);
        if config & lact::EN == 0 {
            return Uptime::Stopped;
        }
        let load =
            u64::from(timer.read(lact::LOAD_HI)) << 32 | u64::from(timer.read(lact::LOAD_LO));
        Uptime::Counting {
            load,
            tick: lact::divider(config) * apb_tick,
        }
    }
}
