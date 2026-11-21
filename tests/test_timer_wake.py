"""firmware/timer_wake sleeps twice with a 2 s timer wake: each wake is a boot of its own, after a
DEEPSLEEP_RESET, with the timer as its wake cause and the boot count in RTC memory going on. A run
in which the chip never wakes fails here rather than halting at its end. `board.wait_for_wake`
runs the board through each sleep to that boot. Needs `make firmware` (timer_wake, and
hello_world for a boot that is no wake)."""

from itertools import pairwise

import pytest

from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.interfaces import BoardOff, NeverHappened
from esp32tl.interfaces.board import DEEPSLEEP_RESET, POWERON_RESET
from esp32tl.interfaces.console import Console
from esp32tl.units import ms, seconds

pytestmark = pytest.mark.app_path("../firmware/timer_wake")

BOOT = rb"timer_wake: boot=(\d+) rom=(\d+) wake=(\d+)"
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


def test_wait_for_wake_runs_the_board_through_its_sleep_to_the_wake(
    board: Esp32Sim, console: Console
) -> None:
    board.power_on()
    assert boot(console) == (1, POWERON_RESET, UNDEFINED)
    console.expect_exact("timer_wake: sleeping 2000 ms", timeout=1)
    turns = board.link.turns
    wake = board.wait_for_wake(within=SLEEP + BOOT_TIME)
    assert wake == board.boot and (wake.number, wake.cause) == (2, DEEPSLEEP_RESET)
    assert wake.reason == "DEEPSLEEP_RESET"
    assert SLEEP <= wake.at - board.boots[0].at <= SLEEP + BOOT_TIME
    # The clock stands at the turn the reset brought, at most 1 ms after it, and the sleep took
    # no turn of its own: wait_until would have looked 20 times.
    assert wake.at <= board.now <= wake.at + ms(1)
    assert board.link.turns - turns <= 2
    # What the firmware prints after the wake is still to come.
    assert boot(console) == (2, DEEPSLEEP_RESET, TIMER)
    console.expect_exact("timer_wake: sleeping 2000 ms", timeout=1)
    assert board.wait_for_wake(within=SLEEP + BOOT_TIME).number == 3
    assert boot(console) == (3, DEEPSLEEP_RESET, TIMER)


def test_a_boot_that_sleeps_keeps_its_uptime_zero_through_the_wake(
    board: Esp32Sim, console: Console
) -> None:
    board.power_on()
    assert boot(console) == (1, POWERON_RESET, UNDEFINED)
    console.expect_exact("timer_wake: sleeping 2000 ms", timeout=1)
    first = board.boot
    zero = board.uptime_zero(first)
    # the classic chip's LACT, started by the application some tens of milliseconds into the boot
    assert zero is not None
    assert first.at < zero.earliest <= zero.latest <= zero.earliest + ms(1)
    wake = board.wait_for_wake(within=SLEEP + BOOT_TIME)
    # The sleep stopped the counter before the wake's reset: the zero still dates every stamp the
    # boot took, in a record read now and through the lookup.
    assert board.boots[0].uptime_zero == zero == board.uptime_zero(first)
    assert boot(console) == (2, DEEPSLEEP_RESET, TIMER)
    console.expect_exact("timer_wake: sleeping 2000 ms", timeout=1)
    woken = board.uptime_zero(wake)
    assert woken is not None
    assert wake.at < woken.earliest <= woken.latest <= woken.earliest + ms(1)
    assert [b.uptime_zero for b in board.boots] == [zero, woken]


def test_wait_for_wake_raises_never_happened_for_a_board_that_does_not_sleep(
    board: Esp32Sim, console: Console
) -> None:
    board.power_on()
    # Two sleeps, two wakes; the third boot returns from app_main.
    for _ in range(2):
        board.wait_for_wake(within=SLEEP + BOOT_TIME)
    console.expect("timer_wake: done", timeout=1)
    start = board.now
    with pytest.raises(NeverHappened) as raised:
        board.wait_for_wake(within=seconds(5))
    assert str(raised.value) == (
        f"a wake from deep sleep did not happen within 5 s: waited from {start} to"
        f" {start + seconds(5)} of the board's time"
    )
    assert board.now == start + seconds(5) and len(board.boots) == 3


def test_wait_for_wake_needs_a_board_that_is_on(board: Esp32Sim) -> None:
    with pytest.raises(BoardOff, match=r"call board.power_on\(\) first"):
        board.wait_for_wake(within=SLEEP)
    board.power_on()
    with pytest.raises(TypeError, match=r"board.wait_for_wake: within takes a Duration"):
        board.wait_for_wake(within=2)  # type: ignore[arg-type]
    board.power_off()
    with pytest.raises(BoardOff, match=r"waits for a board that is on"):
        board.wait_for_wake(within=SLEEP)


@pytest.mark.app_path("../firmware/hello_world")
def test_wait_for_wake_fails_on_a_boot_that_is_no_wake(board: Esp32Sim) -> None:
    # hello_world restarts with esp_restart() 10 s after it boots.
    board.power_on()
    with pytest.raises(AssertionError) as raised:
        board.wait_for_wake(within=seconds(15))
    restart = board.boot
    assert (restart.number, restart.reason) == (2, "SW_CPU_RESET")
    assert str(raised.value) == (
        f"board.wait_for_wake: boot 2 at {restart.at} is SW_CPU_RESET (reset cause 12), not a"
        " wake from deep sleep (DEEPSLEEP_RESET)"
    )
    # The run's log says it too, as a step of the test.
    assert "a wake from deep sleep: boot 2 is SW_CPU_RESET, not a wake" in board.report(run=True)
