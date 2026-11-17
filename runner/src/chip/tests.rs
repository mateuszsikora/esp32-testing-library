//! Each chip's machine, on the emulator itself: what the run loop and the setup rely on that
//! differs between esp32sim's chips.
use super::*;
use crate::session::Guest;
use emu_core::Bus;
use esp_soc::SocBus;

/// An ELF32 little-endian header with no program or section headers: enough for a power-on
/// reset, which loads the ROM again, in a test that runs nothing.
fn empty_rom() -> Vec<u8> {
    let mut rom = vec![0; 52];
    rom[..6].copy_from_slice(b"\x7fELF\x01\x01");
    rom
}

#[test]
fn every_chip_is_found_by_its_name_in_any_case() {
    for profile in ALL {
        assert_eq!(by_name(profile.name), Some(profile));
        assert_eq!(by_name(&profile.name.to_ascii_uppercase()), Some(profile));
    }
    assert_eq!(by_name("s3"), None);
    assert_eq!(by_name("esp32p4"), None);
}

#[test]
fn the_profiles_say_what_esp32sim_models() {
    let names: Vec<_> = ALL.iter().map(|p| p.name).collect();
    assert_eq!(names, ["esp32", "esp32s3", "esp32c3", "esp32c6"]);
    assert_eq!(esp32s3::PROFILE.cpu_hz, 240_000_000);
    assert_eq!(esp32c3::PROFILE.cpu_hz, 160_000_000);
    assert_eq!(esp32c6::PROFILE.cpu_hz, 160_000_000);
    for p in [&esp32s3::PROFILE, &esp32c3::PROFILE, &esp32c6::PROFILE] {
        assert!(!p.wifi_station && p.wifi && p.bt, "{}", p.name);
    }
    const { assert!(esp32::PROFILE.wifi_station) };
    assert_eq!(esp32s3::PROFILE.psram_mb, [2, 4, 8]);
    assert!(esp32c3::PROFILE.psram_mb.is_empty() && esp32c6::PROFILE.psram_mb.is_empty());
    // the GPIO pads, as esp32sim's own pin tables have them (`esp-soc/src/pins.rs`)
    assert_eq!(esp32s3::PROFILE.gpios, esp_soc::pins::ChipPins::S3.valid);
    assert_eq!(esp32c3::PROFILE.gpios, esp_soc::pins::ChipPins::C3.valid);
    assert_eq!(esp32c6::PROFILE.gpios, esp_soc::pins::ChipPins::C6.valid);
}

/// Without the runner's write esp32sim leaves U0RXD without its input, so the console has no RX
/// route; with it, the console's pad receives, after every kind of reset.
macro_rules! console_rx_test {
    ($name:ident, $machine:ty, $rx:expr) => {
        #[test]
        fn $name() {
            let mut m = <$machine as Target>::new(4 << 20);
            assert_eq!(
                Guest::uart_route(&m, 0).rx_pin,
                None,
                "esp32sim's own reset state"
            );
            m.after_reset();
            assert_eq!(Guest::uart_route(&m, 0).rx_pin, Some($rx));
            Guest::chip_reset(&mut m);
            assert_eq!(Guest::uart_route(&m, 0).rx_pin, Some($rx));
            Guest::power_on_reset(&mut m, &empty_rom());
            assert_eq!(Guest::uart_route(&m, 0).rx_pin, Some($rx));
        }
    };
}
console_rx_test!(the_s3_console_receives_on_gpio_44, ::esp32s3::Machine, 44);
console_rx_test!(the_c3_console_receives_on_gpio_20, ::esp32c3::Machine, 20);
console_rx_test!(the_c6_console_receives_on_gpio_17, ::esp32c6::Machine, 17);

/// A power-on reset clears SRAM and the RTC or LP memories, keeps the flash, lets the clock run
/// on, and leaves POWERON as the cause the ROM reports.
macro_rules! power_on_test {
    ($name:ident, $machine:ty, [$($memory:expr),+]) => {
        #[test]
        fn $name() {
            let mut m = <$machine as Target>::new(4 << 20);
            let memories = [$($memory),+];
            for address in memories {
                m.bus.load_bytes(address, &[0xa5; 4]).unwrap();
            }
            m.write_flash(0x9000, b"NVS!").unwrap();
            let second = <$machine as Target>::PROFILE.cpu_hz;
            Guest::idle_until(&mut m, 8 * second);
            m.bus.request_reset(12);
            assert_eq!(Guest::power_on_reset(&mut m, &empty_rom()), 1);
            for address in memories {
                assert_eq!(m.bus.read32(address).unwrap(), 0, "{address:#x}");
            }
            assert_eq!(&m.bus.flash[0x9000..0x9004], b"NVS!");
            assert_eq!(Guest::cycles(&m), 8 * second, "the clock runs on");
            assert_eq!(SocBus::reset_cause(&m.bus), 1);
            assert!(!SocBus::sw_reset(&m.bus), "the reset asked for before is gone");
        }
    };
}
power_on_test!(
    on_the_s3_a_power_on_reset_clears_sram_and_the_rtc_memories,
    ::esp32s3::Machine,
    [
        ::esp32s3::bus::DRAM_LOW,
        ::esp32s3::bus::IRAM_LOW,
        ::esp32s3::bus::RTC_FAST_LOW,
        ::esp32s3::bus::RTC_SLOW_LOW
    ]
);
power_on_test!(
    on_the_c3_a_power_on_reset_clears_sram_and_the_rtc_memory,
    ::esp32c3::Machine,
    [
        ::esp32c3::bus::DRAM_LOW,
        ::esp32c3::bus::IRAM_LOW,
        ::esp32c3::bus::RTC_SLOW_LOW
    ]
);
power_on_test!(
    on_the_c6_a_power_on_reset_clears_sram_and_the_lp_memory,
    ::esp32c6::Machine,
    [::esp32c6::bus::SRAM_LOW, ::esp32c6::bus::LP_SRAM_LOW]
);

/// The C3 and the C6 have two UARTs; the run loop asks for three routes.
macro_rules! missing_uart_test {
    ($name:ident, $machine:ty) => {
        #[test]
        fn $name() {
            let mut m = <$machine as Target>::new(4 << 20);
            let route = Guest::uart_route(&m, 2);
            assert_eq!(
                (route.port, route.tx_pins, route.rx_pin, route.baud),
                (2, 0, None, None)
            );
            assert_eq!(Guest::rx_pending(&m, 2), 0);
            Guest::uart_receive(&mut m, 2, b'x');
            Guest::uart_frame_error(&mut m, 2);
            assert_eq!(Guest::rx_pending(&m, 2), 0);
        }
    };
}
missing_uart_test!(the_c3_has_no_uart2, ::esp32c3::Machine);
missing_uart_test!(the_c6_has_no_uart2, ::esp32c6::Machine);

/// A board without PSRAM has none on the S3, whose esp32sim default is 2 MiB; `--psram-mb` gives
/// it the size asked for.
#[test]
fn the_s3_has_the_psram_the_board_has() {
    let mut m = <::esp32s3::Machine as Target>::new(8 << 20);
    assert!(m.bus.psram.is_empty());
    assert_eq!(m.bus.flash.len(), 8 << 20);
    m.set_psram_size(8 << 20).unwrap();
    assert_eq!(m.bus.psram.len(), 8 << 20);
}

#[test]
fn the_c3_and_the_c6_have_no_psram() {
    assert!(
        <::esp32c3::Machine as Target>::new(4 << 20)
            .set_psram_size(2 << 20)
            .is_err()
    );
    assert!(
        <::esp32c6::Machine as Target>::new(4 << 20)
            .set_psram_size(2 << 20)
            .is_err()
    );
}

/// A byte for a matching route goes into the RX FIFO at once, as on the classic chip.
macro_rules! receive_test {
    ($name:ident, $machine:ty) => {
        #[test]
        fn $name() {
            let mut m = <$machine as Target>::new(4 << 20);
            Guest::uart_receive(&mut m, 1, b'a');
            Guest::uart_receive(&mut m, 1, b'b');
            assert_eq!(Guest::rx_pending(&m, 1), 2);
        }
    };
}
receive_test!(an_s3_uart_takes_a_byte_into_its_fifo, ::esp32s3::Machine);
receive_test!(a_c3_uart_takes_a_byte_into_its_fifo, ::esp32c3::Machine);
receive_test!(a_c6_uart_takes_a_byte_into_its_fifo, ::esp32c6::Machine);
