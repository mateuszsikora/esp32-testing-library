"""The console on the chip's USB-Serial/JTAG controller (`console = "usb-serial-jtag"` in the board
profile): `firmware/console_repl_usb` on the ESP32-S3, ESP32-C3 and ESP32-C6, and `arduino_shell`'s
`esp32s3_usb` environment, Arduino's HWCDC, on the ESP32-S3, each on a local profile of
`tests/boards/` whose only change from a UART0 board is that line. The test bodies are those of a
console on UART0: moving a test to USB-Serial/JTAG is a line in the profile and the build.
Needs the `console_repl_usb` and `arduino_shell` images of `make firmware`, the runner and the ROM
ELFs."""

from pathlib import Path

import pytest

from esp32tl import recording
from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.interfaces.board import POWERON_RESET, Board
from esp32tl.interfaces.console import Console
from esp32tl.units import ms

# The chip, its local profile, and the first line its ROM prints, on USB and on UART0 alike.
CHIPS = [
    ("esp32s3", "boards/esp32-s3-usb.toml", "ESP-ROM:esp32s3-20210327"),
    ("esp32c3", "boards/esp32-c3-usb.toml", "ESP-ROM:esp32c3-api1-20210207"),
    ("esp32c6", "boards/esp32-c6-usb.toml", "ESP-ROM:esp32c6-20220919"),
]


def on_chip(chip: str, profile: str, rom: str) -> type:
    """The tests of one chip, as a class, with the profile on the class and the build on each
    test, where esp32tl reads the markers at collection."""

    @pytest.mark.board_profile(path=profile)
    @pytest.mark.app_path("../firmware/console_repl_usb", build_dir=f"build-{chip}")
    class Tests:
        def test_the_usb_console_takes_a_command_and_comes_back_after_a_reset(
            self, board: Board, console: Console
        ) -> None:
            # On both backends: on a real board the port stays open across the chip's reset, and
            # the console holds the whole boot, from the ROM's banner on.
            board.power_on()
            console.expect_exact(rom, timeout=10)
            console.expect_exact("2nd stage bootloader")
            console.expect_exact("console_repl>", timeout=10)
            console.sendline("help")
            console.expect_exact("Print the summary of all registered commands")
            board.reset()
            console.expect_exact(rom, timeout=10)
            console.expect_exact("console_repl>", timeout=10)
            console.sendline("help")
            console.expect_exact("Print the summary of all registered commands")
            if isinstance(board, Esp32Sim):
                assert board.link.warnings == [] and board.link.error is None

        def test_the_usb_console_holds_the_rom_bootloader_and_app_and_uart0_the_rom_alone(
            self, board: Esp32Sim, console: Console, tmp_path: Path
        ) -> None:
            uart0 = board.uart("uart0").tap(tmp_path / "uart0.jsonl")
            board.power_on()
            console.expect_exact("console_repl>")
            board.wait(ms(100))
            printed = bytes(board.console_output)
            # the ROM writes its banner to both; the bootloader and the app to USB only
            assert printed.startswith(rom.encode())
            assert b"2nd stage bootloader" in printed
            out = b"".join(data for _, direction, data in uart0.events if direction == "out")
            assert out.startswith(rom.encode()) and b"entry 0x" in out
            assert b"2nd stage bootloader" not in out and b"console_repl>" not in out
            assert [boot.cause for boot in board.boots] == [POWERON_RESET]

        def test_a_tap_of_the_usb_console_records_both_ways(
            self, board: Board, console: Console, tmp_path: Path
        ) -> None:
            tap = console.tap(tmp_path / "console.jsonl")
            board.power_on()
            console.expect_exact("console_repl>", timeout=10)
            # what the tap holds by now is the boot, whose prompt may share the board's time
            start = len(tap.events)
            at = board.now.ns
            console.sendline("help")
            console.expect_exact("Print the summary of all registered commands")
            # the prompt after the answer, which may come in a later batch than the answer
            console.expect_exact("console_repl>", timeout=10)
            # at the board's time the test wrote: emulated, or wall time on a real board
            assert any(t >= at and d == "in" and data == b"help\n" for t, d, data in tap.events)
            # one batch may hold part of the prompt
            out = b"".join(data for t, d, data in tap.events[start:] if t >= at and d == "out")
            assert b"console_repl>" in out
            board.close()
            made = recording.read(tmp_path / "console.jsonl")
            assert made.header.interface == "console"
            assert b"help\n" in b"".join(e.data for e in made.events if e.dir == "in")
            if isinstance(board, Esp32Sim):
                report = board.link.report()
                assert "USB-Serial/JTAG console: 0 bytes of console_rx lost" in report
                properties = board.properties()["console"]
                assert isinstance(properties, dict)
                assert properties["bytes_out"] == len(board.console_output)

        def test_what_the_test_writes_with_the_supply_off_is_lost_and_counted(
            self, board: Esp32Sim, console: Console
        ) -> None:
            board.power_on()
            console.expect_exact("console_repl>")
            board.power_off()
            # no USB device is on the bus to take it: the runner counts it in stats
            console.sendline("lost")
            board.wait(ms(10))
            board.power_on()
            console.expect_exact("console_repl>", timeout=5)
            console.sendline("help")
            console.expect_exact("Print the summary of all registered commands")
            board.close()
            assert board.link.stats is not None and board.link.stats.console_rx_lost == 5
            assert [boot.cause for boot in board.boots] == [POWERON_RESET] * 2

    Tests.__name__ = Tests.__qualname__ = f"TestUsbConsole_{chip}"
    return Tests


for _chip, _profile, _rom in CHIPS:
    globals()[f"TestUsbConsole_{_chip}"] = on_chip(_chip, _profile, _rom)


@pytest.mark.board_profile(path="boards/esp32-s3-usb.toml")
@pytest.mark.app_path("../firmware/arduino_shell", build_dir=".pio/build/esp32s3_usb")
def test_arduino_hwcdc_serial_is_the_usb_console(board: Board, console: Console) -> None:
    board.power_on()
    console.expect_exact("arduino_shell: ready; type help", timeout=10)
    console.sendline("help")
    console.expect_exact("arduino_shell: commands: help, greeting, uptime, wifi, ap-off, ap-on")
    # a line longer than one USB packet, 64 bytes, arrives whole: HWCDC drains a packet per
    # interrupt (esp32sim's `UsbSerialJtag::host_input`); the sketch keeps 64 bytes of a line
    console.sendline("x" * 70)
    console.expect_exact("arduino_shell: unknown command '" + "x" * 64 + "'; try help")
    if isinstance(board, Esp32Sim):
        assert board.link.warnings == [] and board.link.error is None
