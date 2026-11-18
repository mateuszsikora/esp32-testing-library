//! The chips the runner emulates (`--chip`): each one is an esp32sim machine with a [`Guest`]
//! for the run loop and a [`Target`] for the setup before it. One process runs one chip, so what
//! the protocol says about it (its name in `hello`, its bus clock, its GPIOs) is the [`Profile`]
//! selected once at the start, before anything reads it.
pub mod esp32;
pub mod esp32c3;
pub mod esp32c6;
pub mod esp32p4;
pub mod esp32s3;
pub mod esp32s31;

use crate::cli;
use crate::hci;
use crate::session::Guest;
use esp_soc::board::BoardModel;
use esp_soc::{Machine, Soc, Stop};
use std::sync::OnceLock;

/// What the protocol and the run loop need to know of a chip.
#[derive(Debug, PartialEq, Eq)]
pub struct Profile {
    /// ESP-IDF's target name, `--chip` and `chip` in `hello` (protocol 5.1).
    pub name: &'static str,
    /// The chip in a message: "the classic ESP32".
    pub title: &'static str,
    /// The bus clock esp32sim counts cycles in, `Soc::CPU_HZ`: the protocol's times are
    /// nanoseconds of it (protocol §3).
    pub cpu_hz: u64,
    /// The GPIOs the chip has, as a mask: a pin of `watch` outside it is refused.
    pub gpios: u64,
    /// Its input-only GPIOs, as a mask: none of the chip's outputs may be one.
    pub input_only: u64,
    /// How many UARTs it has: routes of the others are never enabled.
    pub uarts: usize,
    /// Whether esp32sim tells the board every byte a UART sends, with its route, and the run
    /// loop reads every route (`uart_tx`, `uart_route`). Without them no UART has pins: the
    /// console on UART0 is the bytes esp32sim drains from its TX FIFO, stamped with the end of
    /// the slice they were sent in, and the peer's `console_rx` goes into its RX FIFO without
    /// being dropped: as far as the FIFO has room at once and the rest at UART0's baud rate on the
    /// P4, a byte per character time on the S31 (`Guest::take_uart0_console`,
    /// `Guest::uart0_console_input`).
    pub uart_pins: bool,
    /// Whether `--sd-spi`, an SD card slot on the board's SPI pins, is there: esp32sim tells
    /// the board the pins of each SPI transaction (`spi_transfer_pins`).
    pub sd_spi: bool,
    /// Whether `--wifi-sta`, a virtual station joining the guest's access point, is there.
    pub wifi_station: bool,
    /// Whether `--wifi`, a virtual access point, is there.
    pub wifi: bool,
    /// Whether `--bt`, esp32sim's VHCI hooks, is there.
    pub bt: bool,
    /// Whether the chip has a USB-Serial/JTAG controller, which `--console-port usb` reads and
    /// writes: esp32sim's `UsbSerialJtag`, the first of the streams `console_take` returns.
    pub usb_serial_jtag: bool,
    /// The sizes, in MiB, `--psram-mb` can give the chip besides 0; none if esp32sim gives it
    /// no PSRAM.
    pub psram_mb: &'static [usize],
}

/// Every chip `--chip` takes, in the order the usage lists them.
pub const ALL: [&Profile; 6] = [
    &esp32::PROFILE,
    &esp32s3::PROFILE,
    &esp32c3::PROFILE,
    &esp32c6::PROFILE,
    &esp32p4::PROFILE,
    &esp32s31::PROFILE,
];

/// The chip named `name`, in any case.
pub fn by_name(name: &str) -> Option<&'static Profile> {
    let name = name.to_ascii_lowercase();
    ALL.into_iter().find(|p| p.name == name)
}

static SELECTED: OnceLock<&'static Profile> = OnceLock::new();

/// Selects the chip of this run, once, before the run reads it; a second call changes nothing.
pub fn select(profile: &'static Profile) {
    let _ = SELECTED.set(profile);
}

/// The chip of this run: the classic ESP32 until [`select`] names another, as in the unit tests.
pub fn selected() -> &'static Profile {
    SELECTED.get().copied().unwrap_or(&esp32::PROFILE)
}

/// A chip's machine, as the setup before the run needs it: what differs between esp32sim's
/// chips. What they share is `esp_soc::Machine`, reached through [`Target::machine`].
pub trait Target: Guest + Sized {
    type Soc: Soc;
    const PROFILE: &'static Profile;
    /// A machine with `flash_size` bytes of flash and the MAC esp32sim gives the chip.
    fn new(flash_size: usize) -> Self;
    fn machine(&mut self) -> &mut Machine<Self::Soc>;
    fn machine_ref(&self) -> &Machine<Self::Soc>;
    fn set_psram_size(&mut self, _bytes: usize) -> Result<(), String> {
        Err(format!("{} has no PSRAM in esp32sim", Self::PROFILE.name))
    }
    /// Puts `board` on the chip's pins, or none, as esp32sim's `--board none`.
    fn install_board(&mut self, board: Option<Box<dyn BoardModel>>);
    /// `--wifi`: esp32sim's access point or virtual station, with the network behind it on the
    /// socket.
    fn attach_wifi(&mut self, wifi: &cli::Wifi) -> Result<(), String>;
    /// `--bt`: esp32sim's VHCI hooks over the symbols of `elf`, with the forwarder in place of
    /// esp32sim's virtual controller.
    fn forward_bt(&mut self, elf: &esp_soc::elf::Elf, hci: hci::Shared) -> Result<(), String>;
    /// esp32sim's `[emu] wifi` lines at the end of the run.
    fn wifi_report(&self) -> String {
        String::new()
    }
    /// What the chip's reset leaves wrong in esp32sim, put right after every reset.
    fn after_reset(&mut self) {}
}

/// Runs to the end without a peer: through every chip reset the firmware asks for, as the ROM
/// would after one, until `--max-seconds` or a stop of any other kind.
pub fn run_free<T: Target>(t: &mut T) -> Stop {
    loop {
        let stop = t.machine().run(u64::MAX);
        // whatever the guest printed before the stop comes out before what the runner says
        t.machine().drain_console();
        match stop {
            Stop::SwReset => {
                let cause = t.machine().reboot();
                t.after_reset();
                eprintln!(
                    "esp32tl-runner: chip reset at {:.6} s, cause {cause:#x}",
                    t.machine_ref().seconds()
                );
            }
            stop => return stop,
        }
    }
}

/// `Guest::wifi_access_point` on a chip whose Wi-Fi is esp32sim's shared `StationLink`: a fresh
/// access point, as at power-on, with the networks around it.
pub(crate) fn ap_on_air(
    link: &mut esp_soc::wifi::StationLink,
    wifi: &cli::Wifi,
    debug: &esp_soc::DebugFlags,
) {
    link.attach_new(wifi.access_point(), None, debug);
    link.set_beacons(wifi.nearby().to_vec());
}

/// `Guest::wifi_leave` on such a chip, at `now_us` of the bus's time. esp32sim's station does not
/// notice beacons that stop coming, so the access point tells it it is leaving, as one that shuts
/// down does: a deauthentication ahead of anything it had queued but beacons, then no more
/// beacons. Until it has left it still answers a station that tries again, which the next slice
/// boundary deauthenticates again; it goes off the air once the deauthentication, or what it
/// answered before it, is on its way.
pub(crate) fn ap_leave(link: &mut esp_soc::wifi::StationLink, now_us: u64) -> bool {
    use esp_soc::wifi::{self, StaState};
    let Some(ap) = link.ap_mut() else {
        return true;
    };
    if ap.state != StaState::Idle {
        let deauth = crate::session::deauthentication(ap.sta, ap.cfg.bssid, ap.seq);
        ap.seq = ap.seq.wrapping_add(1);
        ap.queue.retain(|f| wifi::is_beacon(&f.frame));
        ap.queue.push(wifi::AirFrame {
            at_us: now_us,
            frame: deauth,
        });
        ap.state = StaState::Idle;
        ap.next_beacon_us = u64::MAX;
        return false;
    }
    if ap.queue.iter().any(|f| !wifi::is_beacon(&f.frame)) {
        return false;
    }
    link.attach(None, None);
    true
}

/// `Guest::flash` on a chip whose Bluetooth is esp32sim's VHCI: with `--bt`, from the guest's
/// `esp_bt_controller_init` until the next `reboot()`, esp32sim's trampoline sits in the flash
/// over the function's code (`install`, `esp-soc/src/ble/vhci.rs:177-200`), which the guest never
/// wrote: the copy has the code.
pub(crate) fn flash_without_trampoline<'a>(
    flash: &'a [u8],
    original: &Option<(usize, Vec<u8>)>,
) -> std::borrow::Cow<'a, [u8]> {
    let Some((offset, code)) = original else {
        return std::borrow::Cow::Borrowed(flash);
    };
    let mut copy = flash.to_vec();
    copy[*offset..*offset + code.len()].copy_from_slice(code);
    std::borrow::Cow::Owned(copy)
}

/// The bus cycles in one tick of `domain`, as a chip's clock tree divides them
/// (`Peripherals::CLOCKS` of its `device_set!`). A pin without the domain fails the build where
/// a constant asks for it.
pub(crate) const fn clock_divider(
    clocks: &[(emu_core::ClockDomain, u64)],
    domain: emu_core::ClockDomain,
) -> u64 {
    let mut i = 0;
    loop {
        if clocks[i].0 as u8 == domain as u8 {
            return clocks[i].1;
        }
        i += 1;
    }
}

/// `Guest::uptime` on a chip whose `esp_timer` counts on SYSTIMER unit 0
/// (`SYSTIMER_COUNTER_ESPTIMER` in ESP-IDF's `esp_timer_impl_systimer.c`), at `now`, with `tick`
/// bus cycles to one of its 16 MHz ticks: the unit counts while TIMER_UNIT0_WORK_EN, bit 30 of
/// CONF, is set (`esp-periph/src/systimer.rs`), and read zero `unit[0]` ticks before now, less
/// the part of a tick it has counted since its last, which esp32sim's clock tree keeps to itself.
pub(crate) fn systimer_uptime(
    systimer: &esp_periph::Systimer,
    now: u64,
    tick: u64,
) -> crate::session::Uptime {
    use crate::session::Uptime;
    if systimer.conf & 1 << 30 == 0 {
        return Uptime::Stopped;
    }
    match systimer.unit[0]
        .checked_mul(tick)
        .and_then(|counted| now.checked_sub(counted))
    {
        Some(latest) => Uptime::Zero {
            earliest: latest.saturating_sub(tick - 1),
            latest,
        },
        // loaded with more than the run has lasted: a zero before power-on, which has no time
        None => Uptime::BeforePowerOn,
    }
}

/// The route of a UART the chip does not have: no pins, no baud.
pub(crate) fn no_uart(port: usize) -> esp_soc::uart::UartRoute {
    esp_soc::uart::UartRoute::new(port, 0, None, None)
}

// In a file of its own, so that `make docs-check` tells a test-only change by its path.
#[cfg(test)]
mod tests;
