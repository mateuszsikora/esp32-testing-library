"""firmware/persistence through the board's resets and power cycles: what a boot finds of the boots
before it in NVS, on the SD card, in RTC memory and in RAM, the reset reason it reads, and the
board's clock and boots the test reads. Needs `make firmware` (persistence) and, for the card,
mtools."""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.interfaces import BoardOff, SourceError
from esp32tl.interfaces.board import Boot
from esp32tl.interfaces.console import Console
from esp32tl.interfaces.sd_card import SdCard
from esp32tl.units import Duration, ms, seconds

pytestmark = pytest.mark.app_path("../firmware/persistence")

BOOT = (
    rb"persistence: boot=(\d+) reason=(\d+) rom=(\d+) rtc=(\d+) ram=(\d+) sd=(-?\d+)"
    rb" uptime_ms=(\d+)"
)

# esp_reset_reason(): ESP_RST_POWERON and ESP_RST_SW (esp_system.h).
ESP_RST_POWERON, ESP_RST_SW = 1, 3
# The ROM's reasons (RESET_REASON in esp32/rom/rtc.h): a power-on, and esp_restart's.
POWERON_RESET, SW_CPU_RESET = 1, 12


@dataclass(frozen=True)
class Printed:
    """What one boot of the firmware printed."""

    boot: int
    reason: int
    rom: int
    rtc: int
    ram: int
    sd: int
    uptime_ms: int


def booted(console: Console) -> Printed:
    match = console.expect(BOOT, timeout=10)
    return Printed(*(int(group) for group in match.groups()))


def state(printed: Printed) -> tuple[int, int, int, int, int, int]:
    """What a boot printed but its uptime."""
    return printed.boot, printed.reason, printed.rom, printed.rtc, printed.ram, printed.sd


def digests(board: Esp32Sim) -> dict[str, str]:
    """The build's flash files, by name, as hashes of their contents."""
    return {
        Path(f.file_path).name: hashlib.sha256(Path(f.file_path).read_bytes()).hexdigest()
        for f in board.app.flash_files
    }


def test_a_restart_keeps_ram_and_rtc_memory_and_board_reset_clears_them(
    board: Esp32Sim, console: Console
) -> None:
    board.power_on()
    assert state(booted(console)) == (1, ESP_RST_POWERON, POWERON_RESET, 1, 1, -1)
    console.expect_exact("persistence: restarting")
    second = booted(console)
    assert (second.boot, second.reason, second.rom) == (2, ESP_RST_SW, SW_CPU_RESET)
    # esp_restart is a reset of the chip alone: its memories keep what the boot before wrote
    assert (second.rtc, second.ram) == (2, 2)
    console.expect_exact("persistence: done")
    restarted = board.boots[1]
    assert board.boots == (Boot(1, Duration(0), POWERON_RESET), restarted)
    assert (restarted.number, restarted.cause, restarted.reason) == (2, 12, "SW_CPU_RESET")

    board.wait(seconds(2))
    pressed = board.now
    board.reset()
    # the reset reaches the board as it runs: until then the boot is the one before
    assert board.boot == restarted
    board.wait(ms(1))
    assert board.boot == Boot(3, pressed, POWERON_RESET)
    third = booted(console)
    # a power-on reset of the chip: what is not in flash starts again, NVS keeps its count
    assert state(third) == (3, ESP_RST_POWERON, POWERON_RESET, 1, 1, -1)
    assert board.boot == Boot(3, pressed, POWERON_RESET)
    # the board's clock runs on through the reset, the firmware's starts again
    assert board.now - pressed < seconds(1)
    assert third.uptime_ms < 1000 < pressed.ns // 1_000_000
    console.expect_exact("persistence: done")
    assert "boot 3: POWERON_RESET (reset cause 1)" in board.report(run=True)


@pytest.mark.requires_mtools
def test_the_card_keeps_its_power_and_its_files_through_board_reset(
    board: Esp32Sim, console: Console, sd_card: SdCard
) -> None:
    card = sd_card.image().build()
    sd_card.insert(card)
    board.power_on()
    assert [booted(console).sd, booted(console).sd] == [1, 2]
    console.expect_exact("persistence: done")
    board.reset()
    assert state(booted(console)) == (3, ESP_RST_POWERON, POWERON_RESET, 1, 1, 3)
    console.expect_exact("persistence: done")
    assert card.read_text("BOOTS.TXT") == "boot 1\nboot 2\nboot 3\n"


@pytest.mark.requires_mtools
def test_a_power_cycle_keeps_nvs_and_the_card_and_the_clock_runs_on_while_off(
    board: Esp32Sim, console: Console, sd_card: SdCard
) -> None:
    card = sd_card.image().build()
    sd_card.insert(card)
    built = digests(board)
    with pytest.raises(BoardOff, match=r"call board.power_on\(\) first"):
        board.power_off()
    board.power_on()
    assert booted(console).sd == 1
    assert booted(console).sd == 2
    console.expect_exact("persistence: done")
    with pytest.raises(BoardOff, match=r"on already: call board.power_off\(\) first"):
        board.power_on()

    off = board.now
    board.power_off()
    assert not board.powered and board.started
    with pytest.raises(BoardOff, match="off already"):
        board.power_off()
    with pytest.raises(BoardOff, match=r"board.reset\(\) resets a board that is on"):
        board.reset()
    with pytest.raises(SourceError, match="the board has been on"):
        console.tap("console.jsonl")
    printed = len(board.console_output)
    board.wait(seconds(30))
    assert board.now == off + seconds(30)
    assert len(board.console_output) == printed, "a board without power prints nothing"
    with pytest.raises(ValueError, match="the world's time at the first power-on"):
        board.power_on(utc=board.utc)

    board.power_on()
    after = booted(console)
    assert state(after) == (3, ESP_RST_POWERON, POWERON_RESET, 1, 1, 3)
    assert board.boot == Boot(3, off + seconds(30), POWERON_RESET)
    console.expect_exact("persistence: done")
    assert card.read_text("BOOTS.TXT") == "boot 1\nboot 2\nboot 3\n"
    # what the firmware wrote to flash stays in the run: the build is as it was
    assert digests(board) == built
    run = board.report(run=True)
    assert re.search(r"board\.power_off\(\)", run) and re.search(r"board\.power_on\(\)", run)


def test_the_board_cannot_be_reset_or_read_for_boots_before_power_on(board: Esp32Sim) -> None:
    with pytest.raises(BoardOff, match=r"board.reset\(\) resets a board that is on"):
        board.reset()
    with pytest.raises(BoardOff, match=r"call board.power_on\(\) first"):
        assert board.boots
