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
    assert_eq!(by_name("esp32h2"), None);
}

#[test]
fn the_profiles_say_what_esp32sim_models() {
    let names: Vec<_> = ALL.iter().map(|p| p.name).collect();
    assert_eq!(
        names,
        [
            "esp32", "esp32s3", "esp32c3", "esp32c6", "esp32p4", "esp32s31"
        ]
    );
    assert_eq!(esp32p4::PROFILE.cpu_hz, 360_000_000);
    assert_eq!(esp32s31::PROFILE.cpu_hz, 320_000_000);
    for p in [&esp32p4::PROFILE, &esp32s31::PROFILE] {
        assert!(!p.wifi_station && !p.wifi && !p.bt, "{}", p.name);
        assert!(!p.uart_pins && !p.sd_spi && p.usb_serial_jtag, "{}", p.name);
    }
    for p in &ALL[..4] {
        assert!(p.uart_pins && p.sd_spi, "{}", p.name);
    }
    assert_eq!(esp32p4::PROFILE.gpios.count_ones(), 55);
    assert_eq!(esp32s31::PROFILE.gpios.count_ones(), 60);
    assert_eq!(esp32s31::PROFILE.gpios & (1 << 29 | 1 << 41), 0);
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

/// TIMG0's LACT on the classic chip's bus (`DR_REG_TIMERGROUP0_BASE`).
const TIMG0: u32 = 0x3ff5_f000;

#[test]
fn the_classic_lact_reads_as_counting_from_its_load_once_enabled_and_a_reset_stops_it() {
    use crate::session::Uptime;
    let mut m = <::esp32::Machine as Target>::new(4 << 20);
    assert_eq!(Guest::uptime(&mut m), Uptime::Stopped, "a new chip's");
    // what `esp_timer_impl_early_init` writes, with a load of 5 instead of 0
    m.bus.write32(TIMG0 + esp32::lact::LOAD_LO, 5).unwrap();
    m.bus.write32(TIMG0 + 0x94, 1).unwrap();
    m.bus
        .write32(
            TIMG0 + esp32::lact::CONFIG,
            esp32::lact::EN | 1 << 30 | 20 << 13,
        )
        .unwrap();
    // on the XTAL, before the ROM's switch to the PLL: APB at 40 MHz, 6 bus cycles a tick
    assert_eq!(
        Guest::uptime(&mut m),
        Uptime::Counting {
            load: 5,
            tick: 20 * 6
        }
    );
    Guest::chip_reset(&mut m);
    assert_eq!(Guest::uptime(&mut m), Uptime::Stopped);
}

/// SYSTIMER unit 0 counts from every reset, and reads zero where it was loaded.
macro_rules! systimer_uptime_test {
    ($name:ident, $machine:ty, $base:expr, $tick:expr) => {
        #[test]
        fn $name() {
            use crate::session::Uptime;
            let mut m = <$machine as Target>::new(4 << 20);
            // a count read between two ticks: the zero to within one
            let at = |latest: u64| Uptime::Zero {
                earliest: latest.saturating_sub($tick - 1),
                latest,
            };
            assert_eq!(Guest::uptime(&mut m), at(0), "counting from power-on");
            // a millisecond of the bus's time, a whole number of ticks
            Bus::tick(&mut m.bus, (1000 * $tick) as u32);
            let now = Guest::cycles(&m);
            assert_eq!(now, 1000 * $tick);
            assert_eq!(Guest::uptime(&mut m), at(0), "after 1000 ticks");
            // UNIT0_LOAD_HI and _LO, then UNIT0_LOAD: 400 ticks now
            m.bus.write32($base + 0x0c, 0).unwrap();
            m.bus.write32($base + 0x10, 400).unwrap();
            m.bus.write32($base + 0x5c, 1).unwrap();
            assert_eq!(Guest::uptime(&mut m), at(now - 400 * $tick));
            m.bus.write32($base + 0x10, 2000).unwrap();
            m.bus.write32($base + 0x5c, 1).unwrap();
            assert_eq!(
                Guest::uptime(&mut m),
                Uptime::BeforePowerOn,
                "2000 ticks in 1000"
            );
            Guest::chip_reset(&mut m);
            let reset = Guest::cycles(&m);
            let Uptime::Zero { earliest, latest } = Guest::uptime(&mut m) else {
                panic!("counting from the reset");
            };
            assert!(earliest <= reset && reset <= latest && latest - earliest < $tick);
        }
    };
}
systimer_uptime_test!(
    the_s3_systimer_reads_zero_at_its_reset_and_where_it_is_loaded,
    ::esp32s3::Machine,
    0x6002_3000,
    15
);
systimer_uptime_test!(
    the_c3_systimer_reads_zero_at_its_reset_and_where_it_is_loaded,
    ::esp32c3::Machine,
    0x6002_3000,
    10
);
systimer_uptime_test!(
    the_c6_systimer_reads_zero_at_its_reset_and_where_it_is_loaded,
    ::esp32c6::Machine,
    0x6000_a000,
    10
);
/// The P4's SYSTIMER unit 0 counts from every reset at 22.5 bus cycles a tick, and reads zero
/// where it was loaded, to within one tick.
#[test]
fn the_p4_systimer_reads_zero_at_its_reset_and_where_it_is_loaded() {
    use crate::session::Uptime;
    const BASE: u32 = 0x500e_2000;
    let mut m = <esp32p4::P4<::esp32p4::RevGe3> as Target>::new(4 << 20);
    let at = |latest: u64| Uptime::Zero {
        earliest: latest.saturating_sub(23),
        latest,
    };
    assert_eq!(Guest::uptime(&mut m), at(0), "counting from power-on");
    // 1000 ticks of 22.5 bus cycles
    Bus::tick(&mut m.bus, 22_500);
    let now = Guest::cycles(&m);
    assert_eq!(now, 22_500);
    assert_eq!(Guest::uptime(&mut m), at(0), "after 1000 ticks");
    m.bus.write32(BASE + 0x0c, 0).unwrap();
    m.bus.write32(BASE + 0x10, 400).unwrap();
    m.bus.write32(BASE + 0x5c, 1).unwrap();
    assert_eq!(Guest::uptime(&mut m), at(now - 9_000));
    m.bus.write32(BASE + 0x10, 2000).unwrap();
    m.bus.write32(BASE + 0x5c, 1).unwrap();
    assert_eq!(Guest::uptime(&mut m), Uptime::BeforePowerOn);
    Guest::chip_reset(&mut m);
    let reset = Guest::cycles(&m);
    let Uptime::Zero { earliest, latest } = Guest::uptime(&mut m) else {
        panic!("counting from the reset");
    };
    assert!(earliest <= reset && reset <= latest && latest - earliest <= 23);
}

/// The S31's SYSTIMER counts at a rate esp32sim keeps to itself: no uptime.
#[test]
fn the_s31_reports_no_uptime() {
    let mut s = <esp32s31::S31 as Target>::new(4 << 20);
    assert_eq!(Guest::uptime(&mut s), crate::session::Uptime::Stopped);
}

/// A power-on reset of the P4 clears L2MEM, the TCM and the LP RAM, which esp32sim's chip reset
/// keeps, and the LP registers with the reset cause.
macro_rules! p4_power_on_test {
    ($name:ident, $rev:ty) => {
        power_on_test!(
            $name,
            esp32p4::P4<$rev>,
            [
                ::esp32p4::bus::L2MEM_LOW,
                ::esp32p4::bus::L2MEM_HIGH - 4,
                ::esp32p4::bus::TCM_LOW,
                ::esp32p4::bus::LP_RAM_LOW
            ]
        );
    };
}
p4_power_on_test!(
    on_the_p4_a_power_on_reset_clears_l2mem_the_tcm_and_the_lp_ram,
    ::esp32p4::RevLt3
);
p4_power_on_test!(
    on_the_p4_rev3_a_power_on_reset_clears_l2mem_the_tcm_and_the_lp_ram,
    ::esp32p4::RevGe3
);

#[test]
fn a_p4_chip_reset_keeps_l2mem_and_the_cause_it_was_asked_for() {
    let mut m = <esp32p4::P4<::esp32p4::RevGe3> as Target>::new(4 << 20);
    m.bus
        .load_bytes(::esp32p4::bus::L2MEM_LOW, &[0xa5; 4])
        .unwrap();
    m.bus.request_reset(12);
    assert_eq!(Guest::chip_reset(&mut m), 12);
    assert_eq!(
        m.bus.read32(::esp32p4::bus::L2MEM_LOW).unwrap(),
        0xa5a5_a5a5
    );
}

/// The P4 has two UARTs, and esp32sim tells the board no route of either.
#[test]
fn the_p4_reports_no_uart_route_and_has_no_uart2() {
    let mut m = <esp32p4::P4<::esp32p4::RevLt3> as Target>::new(4 << 20);
    for port in 0..3 {
        let route = Guest::uart_route(&m, port);
        assert_eq!(
            (route.port, route.tx_pins, route.rx_pin, route.baud),
            (port, 0, None, None)
        );
    }
    Guest::uart_receive(&mut m, 2, b'x');
    Guest::uart_frame_error(&mut m, 2);
    assert_eq!(Guest::rx_pending(&m, 2), 0);
    Guest::uart_receive(&mut m, 1, b'a');
    assert_eq!(Guest::rx_pending(&m, 1), 1);
}

/// The console's input on the P4 goes into UART0's RX FIFO as far as it has room, and the rest
/// waits in esp32sim's queue for the baud rate: none of it is dropped.
#[test]
fn the_p4_console_input_fills_the_fifo_and_queues_the_rest() {
    let mut m = <esp32p4::P4<::esp32p4::RevLt3> as Target>::new(4 << 20);
    Guest::uart0_console_input(&mut m, b"help\n");
    assert_eq!(Guest::rx_pending(&m, 0), 5);
    assert!(!m.bus.periph.uart[0].has_queued());
    Guest::uart0_console_input(&mut m, &[b'x'; 200]);
    assert_eq!(Guest::rx_pending(&m, 0), 128);
    assert!(m.bus.periph.uart[0].has_queued());
}

/// What UART0 sent comes out of the machine's console stream, once, on the P4 and the S31.
#[test]
fn the_uart0_console_is_taken_from_the_console_stream() {
    let mut p4 = <esp32p4::P4<::esp32p4::RevGe3> as Target>::new(4 << 20);
    p4.console.uart0.extend_from_slice(b"Hello world!\n");
    assert_eq!(Guest::take_uart0_console(&mut p4), b"Hello world!\n");
    assert!(Guest::take_uart0_console(&mut p4).is_empty());
    let mut s31 = <esp32s31::S31 as Target>::new(4 << 20);
    s31.m.console.uart0.extend_from_slice(b"ok\n");
    assert_eq!(Guest::take_uart0_console(&mut s31), b"ok\n");
}

/// The P4's revision is the one the header at the bootloader's offset asks for, whether a
/// bootloader image or an image of the whole flash put it there, else the first other P4 image's;
/// anything that is no P4 image counts as rev < 3.
#[test]
fn the_p4_revision_comes_from_the_image_headers() {
    let header = |min: u16| {
        let mut h = vec![0; 24];
        h[0] = 0xE9;
        h[12] = 0x12;
        h[15..17].copy_from_slice(&min.to_le_bytes());
        h
    };
    let table = vec![0xaa, 0x50, 0, 0];
    let erased = vec![0xff; 24];
    assert!(esp32p4::rev3(Some(&header(301)), &[header(100)]));
    assert!(!esp32p4::rev3(Some(&header(100)), &[header(301)]));
    // an image of the whole flash from 0x0: erased at its start, its bootloader at 0x2000
    assert!(esp32p4::rev3(
        Some(&header(301)),
        std::slice::from_ref(&erased)
    ));
    assert!(esp32p4::rev3(Some(&erased), &[table.clone(), header(301)]));
    assert!(!esp32p4::rev3(None, &[table]));
    let mut c6 = header(301);
    c6[12] = 0x0d;
    assert!(!esp32p4::rev3(None, &[c6]));
}

/// On the S31 a power-on reset leaves POWERON as the cause the ROM reports, with esp32sim's chip
/// reset making SRAM and the LP memory again, and keeps the flash and the clock.
#[test]
fn on_the_s31_a_power_on_reset_clears_sram_and_the_lp_memory() {
    use ::esp32s31::bus::{LP_BASE, SRAM_BASE};
    let mut s = <esp32s31::S31 as Target>::new(4 << 20);
    for address in [SRAM_BASE, LP_BASE] {
        s.m.bus.load_bytes(address, &[0xa5; 4]).unwrap();
    }
    s.m.write_flash(0x9000, b"NVS!").unwrap();
    Guest::idle_until(&mut s, 8 * esp32s31::PROFILE.cpu_hz);
    s.m.bus.request_reset(12);
    assert_eq!(Guest::power_on_reset(&mut s, &empty_rom()), 1);
    for address in [SRAM_BASE, LP_BASE] {
        assert_eq!(s.m.bus.read32(address).unwrap(), 0, "{address:#x}");
    }
    assert_eq!(&s.m.bus.flash[0x9000..0x9004], b"NVS!");
    assert_eq!(Guest::cycles(&s), 8 * esp32s31::PROFILE.cpu_hz);
    assert_eq!(SocBus::reset_cause(&s.m.bus), 1);
    assert!(!SocBus::sw_reset(&s.m.bus));
}

/// The S31 has UART0 alone in esp32sim, without a route; the board's PSRAM is a board input,
/// which esp32sim applies again to the bus it makes at a chip reset (`BoardInputs::apply`).
#[test]
fn the_s31_has_uart0_alone_and_its_psram_stays_a_board_input_through_a_reset() {
    let mut s = <esp32s31::S31 as Target>::new(16 << 20);
    for port in 0..3 {
        assert_eq!(Guest::uart_route(&s, port).rx_pin, None);
    }
    Guest::uart_receive(&mut s, 1, b'x');
    assert_eq!(Guest::rx_pending(&s, 1), 0);
    s.set_psram_size(16 << 20).unwrap();
    Guest::chip_reset(&mut s);
    let inputs = s.m.bus.board_inputs.as_ref().unwrap();
    assert_eq!(inputs.psram_bytes, 16 << 20);
}

/// The console's input on the S31 waits in the runner until UART0 takes it: a byte UART0 refuses,
/// its receiver off (`S31Bus::uart_input` reads UART_CONF0 at 0x88), stays queued and is not
/// lost, and goes in once UART0 takes it; a reset empties the queue, as it does UART0.
#[test]
fn the_s31_console_input_waits_until_uart0_takes_it() {
    let mut s = <esp32s31::S31 as Target>::new(4 << 20);
    let conf = s.m.bus.inner.periph.uart0.read(0x88);
    Guest::uart0_console_input(&mut s, b"help\n");
    s.m.bus.inner.periph.uart0.write(0x88, conf & !(1 << 25));
    s.feed_console();
    assert_eq!(Guest::rx_pending(&s, 0), 0);
    assert_eq!(s.console_queued(), 5, "the byte waits");
    s.m.bus.inner.periph.uart0.write(0x88, conf);
    s.feed_console();
    assert_eq!(Guest::rx_pending(&s, 0), 1);
    assert_eq!(s.console_queued(), 4);
    Guest::chip_reset(&mut s);
    assert_eq!(s.console_queued(), 0);
}

/// The reset-reason hint the guest leaves in `RTC_RESET_CAUSE_REG` is what `reset_hint` reads,
/// before and after a chip reset, which keeps it, through esp32sim or the runner putting it back;
/// a power-on clears it.
macro_rules! reset_hint_test {
    ($name:ident, $machine:ty, $m:ident => $bus:expr, $address:expr) => {
        #[test]
        fn $name() {
            let mut $m = <$machine as Target>::new(4 << 20);
            // ESP_RST_PANIC, as `esp_reset_reason_set_hint` writes it
            let hint = 0x8004_0004;
            $bus.write32($address, hint).unwrap();
            assert_eq!(Guest::reset_hint(&mut $m), hint);
            $bus.request_reset(12);
            assert_eq!(Guest::chip_reset(&mut $m), 12);
            assert_eq!(
                Guest::reset_hint(&mut $m),
                hint,
                "kept through the chip reset"
            );
            Guest::power_on_reset(&mut $m, &empty_rom());
            assert_eq!(Guest::reset_hint(&mut $m), 0, "cleared by the power-on");
        }
    };
}
reset_hint_test!(
    the_classic_chip_keeps_its_reset_hint,
    ::esp32::Machine,
    m => m.bus,
    esp32::RESET_HINT
);
reset_hint_test!(
    the_s3_keeps_its_reset_hint,
    ::esp32s3::Machine,
    m => m.bus,
    esp32s3::RESET_HINT
);
reset_hint_test!(
    the_c3_keeps_its_reset_hint,
    ::esp32c3::Machine,
    m => m.bus,
    esp32c3::RESET_HINT
);
reset_hint_test!(
    the_c6_keeps_its_reset_hint,
    ::esp32c6::Machine,
    m => m.bus,
    esp32c6::RESET_HINT
);
reset_hint_test!(
    the_p4_keeps_its_reset_hint,
    esp32p4::P4<::esp32p4::RevGe3>,
    m => m.bus,
    esp32p4::RESET_HINT
);
reset_hint_test!(
    the_s31_keeps_its_reset_hint,
    esp32s31::S31,
    s => s.m.bus,
    esp32s31::RESET_HINT
);

/// A warm reset the peer asks for (`Guest::request_reset`, then `chip_reset`) gives the ROM its
/// cause and ESP-IDF its hint, in place of the one the guest left, and keeps the RTC memory
/// (`[]` on the S31, whose LP memory esp32sim's chip reset makes again, as for the guest's own).
macro_rules! warm_reset_test {
    ($name:ident, $machine:ty, $m:ident => $bus:expr, $hint_at:expr, [$($memory:expr),*]) => {
        #[test]
        fn $name() {
            let mut $m = <$machine as Target>::new(4 << 20);
            let memories: &[u32] = &[$($memory),*];
            for &address in memories {
                $bus.load_bytes(address, &[0xa5; 4]).unwrap();
            }
            // ESP_RST_SW, the guest's, then ESP_RST_PANIC, the peer's
            $bus.write32($hint_at, 0x8003_0003).unwrap();
            let panic = 0x8004_0004;
            Guest::request_reset(&mut $m, 12, panic);
            assert_eq!(Guest::reset_hint(&mut $m), panic);
            assert_eq!(Guest::chip_reset(&mut $m), 12);
            assert_eq!(Guest::reset_hint(&mut $m), panic, "kept through the reset");
            assert_eq!(SocBus::reset_cause(&$bus), 12);
            assert!(!SocBus::sw_reset(&$bus), "the reset asked for is done");
            for &address in memories {
                assert_eq!($bus.read32(address).unwrap(), 0xa5a5_a5a5, "{address:#x}");
            }
            // the RTC watchdog's, with no hint
            Guest::request_reset(&mut $m, 16, 0);
            assert_eq!(Guest::chip_reset(&mut $m), 16);
            assert_eq!(Guest::reset_hint(&mut $m), 0);
        }
    };
}
warm_reset_test!(
    the_classic_chip_resets_warm_with_the_peers_cause_and_hint,
    ::esp32::Machine,
    m => m.bus,
    esp32::RESET_HINT,
    [esp32::RTC_SLOW]
);
warm_reset_test!(
    the_s3_resets_warm_with_the_peers_cause_and_hint,
    ::esp32s3::Machine,
    m => m.bus,
    esp32s3::RESET_HINT,
    [::esp32s3::bus::RTC_SLOW_LOW]
);
warm_reset_test!(
    the_c3_resets_warm_with_the_peers_cause_and_hint,
    ::esp32c3::Machine,
    m => m.bus,
    esp32c3::RESET_HINT,
    [::esp32c3::bus::RTC_SLOW_LOW]
);
warm_reset_test!(
    the_c6_resets_warm_with_the_peers_cause_and_hint,
    ::esp32c6::Machine,
    m => m.bus,
    esp32c6::RESET_HINT,
    [::esp32c6::bus::LP_SRAM_LOW]
);
warm_reset_test!(
    the_p4_resets_warm_with_the_peers_cause_and_hint,
    esp32p4::P4<::esp32p4::RevGe3>,
    m => m.bus,
    esp32p4::RESET_HINT,
    [::esp32p4::bus::LP_RAM_LOW]
);
warm_reset_test!(
    the_s31_resets_warm_with_the_peers_cause_and_hint,
    esp32s31::S31,
    s => s.m.bus,
    esp32s31::RESET_HINT,
    []
);

/// The causes a warm `chip_reset` may give are the ROM's but a power-on's, and every chip has
/// those of the resets esp32tl injects: a CPU's software reset (12), the RTC watchdog's system
/// reset (16) and the digital core's software reset (3).
#[test]
fn every_chip_takes_the_causes_of_a_panic_a_watchdog_and_a_brownout() {
    for p in ALL {
        assert!(!p.reset_causes.contains(&1), "{}", p.name);
        for cause in [3, 12, 16] {
            assert!(p.reset_causes.contains(&cause), "{}: {cause}", p.name);
        }
    }
}

/// Without the runner, esp32sim's chip reset forgets the C3's and the S31's hint register, which
/// the chip keeps: what `chip_reset` puts back there.
#[test]
fn esp32sim_forgets_the_c3_and_s31_reset_hint() {
    let mut c3 = <::esp32c3::Machine as Target>::new(4 << 20);
    c3.bus.write32(esp32c3::RESET_HINT, 0x8004_0004).unwrap();
    c3.reboot();
    assert_eq!(c3.bus.read32(esp32c3::RESET_HINT).unwrap(), 0);
    let mut s31 = <esp32s31::S31 as Target>::new(4 << 20);
    s31.m
        .bus
        .write32(esp32s31::RESET_HINT, 0x8004_0004)
        .unwrap();
    s31.m.reboot();
    assert_eq!(s31.m.bus.read32(esp32s31::RESET_HINT).unwrap(), 0);
}

/// `flash_write` (protocol 5.25): the bytes are in the flash the guest reads and `flash` gives;
/// bytes that do not fit are refused whole, and say where.
macro_rules! write_flash_test {
    ($name:ident, $machine:ty) => {
        #[test]
        fn $name() {
            let mut m = <$machine as Target>::new(4 << 20);
            Guest::write_flash(&mut m, 0x9000, b"NVS!").unwrap();
            assert_eq!(&Guest::flash(&m)[0x9000..0x9004], b"NVS!");
            let before = Guest::flash(&m).into_owned();
            assert_eq!(
                Guest::write_flash(&mut m, (4 << 20) - 2, b"NVS!"),
                Err("4 bytes at 0x3ffffe end past the flash, of 4194304 bytes".into())
            );
            assert_eq!(Guest::flash(&m), before, "nothing of it written");
            assert!(Guest::write_flash(&mut m, usize::MAX, b"x").is_err());
        }
    };
}
write_flash_test!(the_esp32_writes_its_flash_for_the_peer, ::esp32::Machine);
write_flash_test!(the_s3_writes_its_flash_for_the_peer, ::esp32s3::Machine);
write_flash_test!(the_c3_writes_its_flash_for_the_peer, ::esp32c3::Machine);
write_flash_test!(the_c6_writes_its_flash_for_the_peer, ::esp32c6::Machine);
write_flash_test!(
    the_p4_writes_its_flash_for_the_peer,
    esp32p4::P4<::esp32p4::RevGe3>
);
write_flash_test!(the_s31_writes_its_flash_for_the_peer, esp32s31::S31);

/// A write over esp32sim's Bluetooth trampoline goes into the code `reboot()` puts back too, so
/// that the next boot, and the dump before it, find the peer's bytes.
#[test]
fn a_flash_write_over_the_trampoline_lasts_through_the_reboot() {
    let mut m = <::esp32::Machine as Target>::new(4 << 20);
    m.bus.flash[0x10000..0x10008].copy_from_slice(b"TRAMPOLI");
    m.bus.ble.original_flash = Some((0x10000, b"original".to_vec()));
    Guest::write_flash(&mut m, 0x0fffe, b"peer").unwrap();
    assert_eq!(&Guest::flash(&m)[0x0fffe..0x10008], b"peeriginal");
    assert_eq!(
        m.bus.ble.original_flash,
        Some((0x10000, b"eriginal".to_vec()))
    );
    // one that misses it leaves the copy alone
    Guest::write_flash(&mut m, 0x10008, b"next").unwrap();
    assert_eq!(&Guest::flash(&m)[0x0fffe..0x1000c], b"peeriginalnext");
    m.reboot();
    assert_eq!(m.bus.ble.original_flash, None);
    assert_eq!(&m.bus.flash[0x0fffe..0x1000c], b"peeriginalnext");
}

/// On the S31 what the guest programs stays in esp32sim's cache NOR array until a reset: with the
/// supply off, `sync_flash` has `flash` give it, and a `write_flash` beside it keeps it.
#[test]
fn the_s31_flash_has_what_the_guest_wrote_once_synced() {
    let mut s = <esp32s31::S31 as Target>::new(4 << 20);
    // what the guest's SPI1 program leaves in the cache's array, which the outer flash lacks
    let mut nor = Guest::flash(&s).into_owned();
    nor[0x11000..0x11004].copy_from_slice(b"FW!!");
    s.m.bus.inner.cache.set_flash(&nor).unwrap();
    assert_eq!(
        &Guest::flash(&s)[0x11000..0x11004],
        &[0xff; 4],
        "esp32sim's outer copy"
    );
    Guest::sync_flash(&mut s);
    assert_eq!(&Guest::flash(&s)[0x11000..0x11004], b"FW!!");
    Guest::write_flash(&mut s, 0x9000, b"NVS!").unwrap();
    let flash = Guest::flash(&s);
    assert_eq!(&flash[0x9000..0x9004], b"NVS!");
    assert_eq!(
        &flash[0x11000..0x11004],
        b"FW!!",
        "the guest's bytes outside the write stay"
    );
    // and the next boot finds both
    Guest::power_on_reset(&mut s, &[]);
    let flash = Guest::flash(&s);
    assert_eq!(
        (&flash[0x9000..0x9004], &flash[0x11000..0x11004]),
        (&b"NVS!"[..], &b"FW!!"[..])
    );
}
