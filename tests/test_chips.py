"""The validation images built for the ESP32-S3, ESP32-C3, ESP32-C6, ESP32-P4 and ESP32-S31
(`firmware/build.sh --chips`), unmodified, each on its chip's devkit profile: what esp32sim models
on these chips works through the runner and the harness as it does on the classic ESP32. What it
does not model (deep sleep, an SD card over SPI, a virtual station; on the P4 and the S31 UART
routes, Wi-Fi and Bluetooth) is skipped or left out; issue #229 lists it. Needs `make firmware`
(the `--chips` images)."""

import re
from pathlib import Path

import pytest

from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.flash import Namespaces, i8, i16, i32, i64, u8, u16, u32, u64
from esp32tl.interfaces.bluetooth import Bluetooth
from esp32tl.interfaces.board import POWERON_RESET
from esp32tl.interfaces.console import Console
from esp32tl.interfaces.gnss import Gnss
from esp32tl.interfaces.lines import Lines
from esp32tl.interfaces.wifi import Wifi
from esp32tl.units import Duration, ms, seconds

FIRMWARE = Path(__file__).parent.parent / "firmware"

# The chip, its devkit profile, and what ESP-IDF's hello_world says the chip is.
CHIPS = [
    ("esp32s3", "esp32-s3-devkitc", "esp32s3 chip with 2 CPU core(s), WiFi/BLE"),
    ("esp32c3", "esp32-c3-devkitm", "esp32c3 chip with 1 CPU core(s), WiFi/BLE"),
    ("esp32c6", "esp32-c6-devkitc", "esp32c6 chip with 1 CPU core(s), WiFi/BLE, 802.15.4"),
]


# What the nvs_reader tests write into the NVS before power-on: every key the image reads.
NVS_CONFIG: Namespaces = {
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
            board.nvs(NVS_CONFIG)
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

            def test_without_bt_the_controller_s_start_ends_the_run_naming_board_bluetooth(
                self, pytester: pytest.Pytester
            ) -> None:
                # the runner's stub at esp_bt_controller_init (--no-bt) returns through this
                # chip's own core: the S3's LX7, the C3's RISC-V
                build = FIRMWARE / "gatt_server" / f"build-{chip}"
                if not (build / "esp32tl-built").is_file():
                    pytest.fail(
                        f"{build} holds no finished build: run make firmware", pytrace=False
                    )
                pytester.makepyfile(
                    test_no_bt=f"""
                    import pytest

                    from esp32tl.units import seconds

                    @pytest.mark.board_profile({profile!r})
                    @pytest.mark.app_path({str(build.parent)!r}, build_dir={build.name!r})
                    def test_without_bluetooth(board, console):
                        board.power_on()
                        try:
                            board.wait(seconds(5))
                        finally:
                            print("boots:", len(board.boots))
                    """
                )
                result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
                result.assert_outcomes(failed=1)
                result.stdout.fnmatch_lines(
                    [
                        "*RunEnded: the run ended at 0.* s while the test waited until 5 s: the"
                        " firmware started its Bluetooth controller (esp_bt_controller_init) by"
                        " 0.* s, and the test gave the board no Bluetooth: look at"
                        " board.bluetooth*",
                        "boots: 1",
                    ]
                )
                assert (
                    "GATTS_DEMO: app_main initialize controller failed: ESP_ERR_NOT_SUPPORTED"
                    in result.stdout.str()
                )

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


# The ESP32-P4 and the ESP32-S31: the chip, its devkit profile, the first line its ROM prints, and
# what ESP-IDF's hello_world says the chip is. The P4's images are built for rev >= 3, ESP-IDF's
# default, and the runner runs them on esp32sim's rev >= 3 model.
NEW_CHIPS = [
    (
        "esp32p4",
        "esp32-p4-function-ev-board",
        "ESP-ROM:esp32p4-eco5-20250430",
        "esp32p4 chip with 2 CPU core(s), , silicon revision v3.1",
    ),
    (
        "esp32s31",
        "esp32-s31-module",
        "ESP-ROM:esp32s31-20251218",
        "esp32s31 chip with 2 CPU core(s), WiFi/BLE, 802.15.4 (Zigbee/Thread), silicon revision"
        " v0.0",
    ),
]


def on_new_chip(chip: str, profile: str, rom: str, banner: str) -> type:
    """The tests of the ESP32-P4 or the ESP32-S31, as `on_chip` has them for the other chips: what
    esp32sim models of them. Their UARTs have no pins in esp32sim, so the console on UART0 is
    read and written without them, and a UART route is skipped (#229, G6 and G7)."""

    def image(name: str) -> pytest.MarkDecorator:
        return pytest.mark.app_path(f"../firmware/{name}", build_dir=f"build-{chip}")

    @pytest.mark.board_profile(profile)
    class Tests:
        @image("hello_world")
        def test_hello_world_boots_and_restarts_through_the_rom(
            self, board: Esp32Sim, console: Console
        ) -> None:
            board.power_on()
            console.expect_exact(rom)
            console.expect_exact("Hello world!")
            console.expect_exact(f"This is {banner}")
            console.expect_exact("Restarting now.", timeout=12)
            console.expect_exact(rom, timeout=2)
            console.expect_exact("Hello world!", timeout=2)
            assert [boot.cause for boot in board.boots] == [POWERON_RESET, 12]
            assert board.link.warnings == [] and board.link.error is None

        @image("console_repl")
        def test_the_console_on_uart0_takes_a_command(self, board: Esp32Sim) -> None:
            board.power_on()
            board.console.expect_exact("console_repl>")
            board.console.sendline("help")
            board.console.expect_exact("Print the summary of all registered commands")
            board.console.sendline("help")
            board.console.expect_exact("Print the summary of all registered commands")
            assert board.link.warnings == []
            assert board.link.stats is None or board.link.stats.console_rx_lost == 0

        @image("console_repl_usb")
        @pytest.mark.board_profile(path=f"boards/{profile_name(chip)}-usb.toml")
        def test_the_console_on_usb_serial_jtag_takes_a_command(self, board: Esp32Sim) -> None:
            board.power_on()
            board.console.expect_exact(rom, timeout=10)
            board.console.expect_exact("console_repl>", timeout=10)
            board.console.sendline("help")
            board.console.expect_exact("Print the summary of all registered commands")
            assert board.link.warnings == []

        @image("gpio_echo")
        def test_gpio_echo_follows_the_line_the_test_drives(
            self, board: Esp32Sim, lines: Lines, console: Console
        ) -> None:
            board.power_on()
            console.expect_exact("gpio_echo: in GPIO6 out GPIO7")
            console.expect_exact("in=0")
            assert board.wait_until(lambda: lines.level("echo.out") == 0, within=seconds(1))
            lines.drive("echo.in", 1)
            console.expect_exact("in=1", timeout=1)
            assert board.wait_until(lambda: lines.level("echo.out") == 1, within=ms(10))
            lines.release("echo.in")
            console.expect_exact("in=0", timeout=1)
            assert board.wait_until(lambda: lines.level("echo.out") == 0, within=ms(10))

        @image("nvs_reader")
        def test_nvs_written_before_power_on_survives_en_and_the_supply(
            self, board: Esp32Sim, console: Console
        ) -> None:
            board.nvs(NVS_CONFIG)
            printed = "nvs_reader: host=broker.test port=8883 enabled=1 count=2 delta=-3"
            board.power_on()
            console.expect_exact(printed)
            board.wait(ms(200))
            board.reset()
            console.expect_exact(rom, timeout=5)
            console.expect_exact(printed, timeout=5)
            board.wait(ms(200))
            board.power_off()
            board.wait(ms(200))
            board.power_on()
            console.expect_exact(printed, timeout=5)
            assert [boot.cause for boot in board.boots] == [POWERON_RESET] * 3
            config = board.flash().nvs()["config"]
            assert (config["broker_port"], config["delta"]) == (u16(8883), i32(-3))

        @image("hello_world_psram")
        def test_the_psram_of_the_profile_is_found_and_used(
            self, board: Esp32Sim, console: Console
        ) -> None:
            board.power_on()
            found = console.expect(re.compile(rb"esp_psram: Found (\d+)MB PSRAM device"))
            assert int(found.group(1)) == board.profile.psram_mb
            console.expect_exact("Hello world!")

        @image("hello_world")
        @pytest.mark.board_profile(path=f"boards/{profile_name(chip)}-gnss.toml")
        def test_a_uart_route_skips_the_test_with_the_gap(
            self, board: Esp32Sim, gnss: Gnss
        ) -> None:
            gap = "G6" if chip == "esp32p4" else "G7"
            with pytest.raises(pytest.skip.Exception, match=rf"uart.gnss on {chip}: .*#229, {gap}"):
                gnss.replay("../recordings/gnss-generated-1.jsonl")

    Tests.__name__ = Tests.__qualname__ = f"TestOn{chip.upper()}"
    return Tests


def profile_name(chip: str) -> str:
    """`esp32-p4` for `esp32p4`: the local profiles' names in tests/boards/."""
    return f"esp32-{chip.removeprefix('esp32')}"


TestOnESP32P4, TestOnESP32S31 = (on_new_chip(*chip) for chip in NEW_CHIPS)


@pytest.mark.app_path("../firmware/hello_world_rev1", build_dir="build-esp32p4")
@pytest.mark.board_profile("esp32-p4-function-ev-board")
def test_a_p4_image_for_rev_below_3_runs_on_the_rev_below_3_model(
    board: Esp32Sim, console: Console
) -> None:
    # the runner takes the revision from the bootloader's header, the backend the ROM
    board.power_on()
    console.expect_exact("ESP-ROM:esp32p4-20230811")
    console.expect_exact("This is esp32p4 chip with 2 CPU core(s), , silicon revision v1.0")
