"""firmware/timer_wake sleeps twice with a 2 s timer wake: each wake is a boot of its own, after a
DEEPSLEEP_RESET, with the timer as its wake cause and the boot count in RTC memory going on. A run
in which the chip never wakes fails here rather than halting at its end. Needs `make firmware`
(timer_wake)."""

from itertools import pairwise

import pytest

from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.interfaces.board import POWERON_RESET
from esp32tl.interfaces.console import Console
from esp32tl.units import seconds

pytestmark = pytest.mark.app_path("../firmware/timer_wake")

BOOT = rb"timer_wake: boot=(\d+) rom=(\d+) wake=(\d+)"
DEEPSLEEP_RESET = 5
# esp_sleep_wakeup_cause_t in ESP-IDF v5.3: ESP_SLEEP_WAKEUP_UNDEFINED and ESP_SLEEP_WAKEUP_TIMER
UNDEFINED, TIMER = 0, 4
SLEEP = seconds(2)
# From a boot's start to its sleep's: the boot of the image and app_main, 45 ms on the pin.
BOOT_TIME = seconds(0.1)


def boot(console: Console) -> tuple[int, int, int]:
    number, rom, wake = console.expect(BOOT, timeout=SLEEP + BOOT_TIME).groups()
    return int(number), int(rom), int(wake)


def test_a_timer_wake_boots_the_chip_again_after_its_sleep(
    board: Esp32Sim, console: Console
) -> None:
    board.power_on()
    assert boot(console) == (1, POWERON_RESET, UNDEFINED)
    assert boot(console) == (2, DEEPSLEEP_RESET, TIMER)
    assert boot(console) == (3, DEEPSLEEP_RESET, TIMER)
    console.expect("timer_wake: done", timeout=1)

    boots = board.boots
    assert [b.cause for b in boots] == [POWERON_RESET, DEEPSLEEP_RESET, DEEPSLEEP_RESET]
    for before, after in pairwise(boots):
        assert SLEEP <= after.at - before.at <= SLEEP + BOOT_TIME
