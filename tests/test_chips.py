"""The validation images built for the ESP32-S3, ESP32-C3 and ESP32-C6 (`firmware/build.sh
--chips`), unmodified, each on its chip's devkit profile: what esp32sim models on these chips
works through the runner and the harness as it does on the classic ESP32. What it does not model
(deep sleep, an SD card over SPI, a virtual station) is skipped or left out; issue #229 lists it.
Needs `make firmware` (the `--chips` images)."""

import re
from pathlib import Path

import pytest

from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.flash import i8, i16, i32, i64, u8, u16, u32, u64
from esp32tl.interfaces.bluetooth import Bluetooth
from esp32tl.interfaces.board import POWERON_RESET
from esp32tl.interfaces.console import Console
from esp32tl.interfaces.gnss import Gnss
from esp32tl.interfaces.lines import Lines
from esp32tl.interfaces.wifi import Wifi
from esp32tl.units import Duration, ms, seconds

# The chip, its devkit profile, and what ESP-IDF's hello_world says the chip is.
CHIPS = [
    ("esp32s3", "esp32-s3-devkitc", "esp32s3 chip with 2 CPU core(s), WiFi/BLE"),
    ("esp32c3", "esp32-c3-devkitm", "esp32c3 chip with 1 CPU core(s), WiFi/BLE"),
    ("esp32c6", "esp32-c6-devkitc", "esp32c6 chip with 1 CPU core(s), WiFi/BLE, 802.15.4"),
]


def on_chip(chip: str, profile: str, banner: str) -> type:
    """The tests of one chip, as a class: its devkit profile on the class, and on each test the
    image built for the chip. The markers go on the test and its class, where esp32tl reads them
    at collection, not through `pytest.param`."""

    def image(name: str) -> pytest.MarkDecorator:
        return pytest.mark.app_path(f"../firmware/{name}", build_dir=f"build-{chip}")

    @pytest.mark.board_profile(profile)
    class Tests:
        @image("hello_world")
        def test_hello_world_boots_and_restarts_through_the_rom(
            self, board: Esp32Sim, console: Console
        ) -> None:
            board.power_on()
            console.expect_exact("Hello world!")
            console.expect_exact(f"This is {banner}")
            # esp_restart() after the countdown: a software reset, the ROM boots the app again
            console.expect_exact("Restarting now.", timeout=12)
            console.expect_exact("Hello world!", timeout=2)
            assert [boot.cause for boot in board.boots] == [POWERON_RESET, 12]
            # esp32sim's SYSTIMER counts from every reset, and does not model the application's
            # reset of it: esp_timer counts from the boot's start, read to within a 16 MHz tick
            for boot in board.boots:
                zero = boot.uptime_zero
                assert zero is not None, boot
                assert zero.earliest <= boot.at <= zero.latest < zero.earliest + Duration(63)
            assert board.link.warnings == [] and board.link.error is None

        @image("console_repl")
        def test_the_console_takes_a_command(self, board: Esp32Sim) -> None:
            board.power_on()
            board.console.expect_exact("console_repl>")
            board.console.sendline("help")
            board.console.expect_exact("Print the summary of all registered commands")
            assert board.link.warnings == []

        @image("gpio_echo")
        def test_gpio_echo_follows_the_line_the_test_drives(
            self, board: Esp32Sim, lines: Lines, console: Console
        ) -> None:
            board.power_on()
            console.expect_exact("gpio_echo: in GPIO6 out GPIO7")
            # the board's pull-down holds the line low until the test drives it
            console.expect_exact("in=0")
            assert board.wait_until(lambda: lines.level("echo.out") == 0, within=seconds(1))
            lines.drive("echo.in", 1)
            console.expect_exact("in=1", timeout=1)
            assert board.wait_until(lambda: lines.level("echo.out") == 1, within=ms(10))
            lines.release("echo.in")
            assert board.wait_until(lambda: lines.level("echo.out") == 0, within=ms(10))

        @image("nmea_reader")
        def test_nmea_reader_reports_the_replayed_fix(
            self, board: Esp32Sim, gnss: Gnss, console: Console
        ) -> None:
            gnss.replay("../recordings/gnss-generated-1.jsonl")
            board.power_on()
            console.expect_exact("nmea_reader: UART1 RX GPIO5 TX GPIO4 9600 baud")
            console.expect_exact("fix acquired lat=52.229677 lon=21.012233", timeout=30)

        @image("nvs_reader")
        def test_nvs_written_before_power_on_survives_en_and_the_supply(
            self, board: Esp32Sim, console: Console
        ) -> None:
            board.nvs(
                {
                    "config": {
                        "broker_host": "broker.test",
                        "broker_port": u16(8883),
                        "broker_ca": b"CA",
                        "enabled": u8(1),
                        "count": u32(2),
                        "delta": i32(-3),
                        "small": i8(-4),
                        "medium": i16(-5),
                        "total": u64(6),
                        "offset": i64(-7),
                    }
                }
            )
            printed = "nvs_reader: host=broker.test port=8883 enabled=1 count=2 delta=-3"
            board.power_on()
            console.expect_exact(printed)
            board.wait(ms(200))
            board.reset()
            console.expect_exact(printed, timeout=5)
            board.wait(ms(200))
            board.power_off()
            board.wait(ms(200))
            board.power_on()
            console.expect_exact(printed, timeout=5)
            assert [boot.cause for boot in board.boots] == [POWERON_RESET] * 3
            # the runner's copy of the flash, through the chip's Guest::flash
            config = board.flash().nvs()["config"]
            assert (config["broker_port"], config["delta"]) == (u16(8883), i32(-3))

        @image("http_request")
        def test_http_request_joins_and_fetches_from_the_tests_server(
            self, board: Esp32Sim, wifi: Wifi, console: Console
        ) -> None:
            network = wifi.access_point("myssid", password="mypassword")
            network.http.serve("/", "hello from the test\n")
            printed = console.line(r"^hello from the test$")
            board.power_on()
            request = board.wait_until(network.http.next_request("/"), within=seconds(30))
            assert request.header("Host") == "example.com:80"
            board.wait_until(printed, within=seconds(5))
            assert network.dns.asked == ["example.com"]
            assert board.link.warnings == [] and board.link.error is None

        if chip != "esp32c6":  # esp32sim's C6 hooks want an image of ESP-IDF v5.5.x (#229, G5)

            @image("gatt_server")
            def test_a_ble_host_gets_through_its_controller_on_the_air(
                self, board: Esp32Sim, bt: Bluetooth, console: Console, tmp_path: Path
            ) -> None:
                # Bluedroid's own HCI goes through esp32sim's VHCI to the harness's controller,
                # which answers every command it sends; there is no LE peer yet (#234)
                tap = bt.tap(tmp_path / "hci.btsnoop")
                board.power_on()
                console.expect_exact("GATTS_DEMO: Advertising start successfully", timeout=5)
                assert bt.packets_from_host > 0 and bt.packets_to_host > 0
                assert bt.unknown_commands() == []
                # the tap has every packet, Bluedroid's HCI_Reset first
                assert len(tap.events) == bt.packets_from_host + bt.packets_to_host
                _, direction, first = tap.events[0]
                assert (direction, first) == ("out", bytes.fromhex("01030c00"))
                # the flash holds the application as built, not esp32sim's trampoline over
                # esp_bt_controller_init (chip::flash_without_trampoline)
                assert board.app.app_image is not None
                built = board.app.app_image.read_bytes()
                assert board.flash().partition("factory")[: len(built)] == built

    Tests.__name__ = Tests.__qualname__ = f"TestOn{chip.upper()}"
    return Tests


TestOnESP32S3, TestOnESP32C3, TestOnESP32C6 = (on_chip(*chip) for chip in CHIPS)


@pytest.mark.app_path("../firmware/hello_world_psram", build_dir="build-esp32s3")
@pytest.mark.board_profile("esp32-s3-devkitc")
def test_the_s3_finds_and_uses_the_devkit_s_octal_psram(board: Esp32Sim, console: Console) -> None:
    board.power_on()
    found = console.expect(re.compile(rb"esp_psram: Found (\d+)MB PSRAM device"))
    assert found.group(1) == b"8"
    console.expect_exact("esp_psram: SPI SRAM memory test OK")
    console.expect_exact("Hello world!")
