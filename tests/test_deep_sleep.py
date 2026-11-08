"""ESP-IDF's system/deep_sleep example, unmodified: it arms a 20 s timer wake, sleeps a second
after it boots, and after each wake prints how long its sleep took by the RTC. The test runs the
board through the sleep with `board.wait_for_wake` and asserts on the line the firmware prints
after the wake, so a chip that never wakes fails it rather than halting at the end of the run.
Needs `make firmware` (deep_sleep)."""

import pytest

from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.interfaces.board import DEEPSLEEP_RESET
from esp32tl.interfaces.console import Console
from esp32tl.units import ms, seconds

pytestmark = pytest.mark.app_path("../firmware/deep_sleep")

SLEEP = seconds(20)
# From the chip's wake to the line that reports it: the ROM, the bootloader and app_main at the
# example's 80 MHz, which calibrates two touch pads first; 143 ms on the pin.
BOOT_TIME = seconds(0.2)
# The chip's RTC runs some 200 to 250 ppm slow of the board's time, measured: the 20 s timer
# wakes it 4 ms late, and the sleep it reports on that clock is 5 ms short of the board's time to
# its line, whose bytes are stamped as the firmware writes them (protocol, emulated-fifo-write).
# Both grow with the sleep.
MARGIN = ms(10)
WOKE = r"^Wake up from timer\. Time spent in deep sleep: (?P<ms>\d+)ms$"


def test_the_deep_sleep_example_wakes_on_its_timer_and_says_how_long_it_slept(
    board: Esp32Sim, console: Console
) -> None:
    board.power_on()
    console.expect_exact("Enabling timer wakeup, 20s", timeout=1)
    console.expect_exact("Not a deep sleep reset", timeout=1)
    for number in (2, 3):
        asleep = board.wait_until(console.line("^Entering deep sleep$"), within=seconds(2)).at
        turns = board.link.turns
        wake = board.wait_for_wake(within=SLEEP + MARGIN)
        # The turn the reset brings, and none in the 20 s of sleep: wait_until would take 200.
        assert board.link.turns - turns <= 2
        assert (wake.number, wake.cause) == (number, DEEPSLEEP_RESET)
        assert SLEEP <= wake.at - asleep <= SLEEP + MARGIN
        # The firmware's own measure, from its clock before the sleep to its clock after the
        # wake, on the RTC, which runs on through the sleep: the board's time, to the line.
        woke = board.wait_until(console.line(WOKE), within=BOOT_TIME)
        assert woke.at - wake.at <= BOOT_TIME
        slept = ms(int(woke["ms"]))
        assert woke.at - asleep - MARGIN <= slept <= woke.at - asleep
