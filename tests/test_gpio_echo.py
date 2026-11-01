"""gpio_echo, unmodified, on the LilyGO profile's `[lines.modem]` through the `lines` fixture
(plan M3): the firmware reads RI, which the board pulls down, and drives PWRKEY to RI's level. The
test drives and releases RI at the board's instant and reads PWRKEY as a condition of the board's
clock."""

import pytest

from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.interfaces.console import Console
from esp32tl.interfaces.lines import LineEvent, Lines

pytestmark = [
    pytest.mark.app_path("../firmware/gpio_echo"),
    pytest.mark.board_profile("lilygo-t-a7670g-r2"),
]

# How soon after RI changes gpio_echo has PWRKEY at its level, in seconds of the board's time:
# the interrupt, the task's wake-up and a GPIO write take microseconds, not a FreeRTOS tick.
ECHO_SECONDS = 0.001


def test_ri_reads_the_board_s_pull_down_from_power_on(
    board: Esp32Sim, lines: Lines, console: Console
) -> None:
    board.power_on()
    console.expect_exact("gpio_echo: in GPIO33 out GPIO4")
    # Without the pull, an undriven pad reads 1 in the emulator: the firmware would see a modem.
    console.expect_exact("in=0")
    assert lines.level("modem.pwrkey") == 0
    assert [(e.name, e.by, e.level) for e in lines.events()] == [("modem.pwrkey", "firmware", 0)]


def test_the_pull_holds_in_a_test_that_does_not_use_lines(
    board: Esp32Sim, console: Console
) -> None:
    board.power_on()
    console.expect_exact("in=0")


def test_the_firmware_follows_ri_and_drives_pwrkey(
    board: Esp32Sim, lines: Lines, console: Console
) -> None:
    board.power_on()
    console.expect_exact("in=0")
    board.wait(1)
    lines.drive("modem.ri", 1)
    console.expect_exact("in=1", timeout=1)
    assert board.wait_until(lambda: lines.level("modem.pwrkey") == 1, within=1)
    board.wait(1)
    released = board.now
    # Back to the board's pull-down.
    lines.release("modem.ri")
    assert board.wait_until(lambda: lines.level("modem.pwrkey") == 0, within=1)
    console.expect_exact("in=0", timeout=0)
    events = lines.events()
    assert [(e.name, e.by, e.level) for e in events] == [
        ("modem.pwrkey", "firmware", 0),
        ("modem.ri", "test", 1),
        ("modem.pwrkey", "firmware", 1),
        ("modem.ri", "test", None),
        ("modem.pwrkey", "firmware", 0),
    ]
    _, driven, followed, release, back = events
    assert release.t == released
    assert 0 < followed.t - driven.t < ECHO_SECONDS
    assert 0 < back.t - release.t < ECHO_SECONDS
    assert board.link.warnings == []


def test_a_level_driven_before_power_on_holds_from_the_start(
    board: Esp32Sim, lines: Lines, console: Console
) -> None:
    lines.drive("modem.ri", 1)
    board.power_on()
    console.expect_exact("gpio_echo: in GPIO33 out GPIO4")
    console.expect_exact("in=1")
    assert lines.level("modem.pwrkey") == 1
    assert lines.events()[0] == LineEvent(0.0, "modem.ri", 33, "test", 1)
