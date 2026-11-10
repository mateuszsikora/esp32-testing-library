"""Deep sleep with a timer wake: a pulse on a wire every 15 minutes, a count in RTC memory."""

import time
from itertools import pairwise

import pytest

from esp32tl import Board
from esp32tl.interfaces.board import DEEPSLEEP_RESET, POWERON_RESET
from esp32tl.interfaces.lines import OutputLine
from esp32tl.units import Duration, hours, minutes, ms, seconds

pytestmark = [
    pytest.mark.app_path(".."),
    pytest.mark.board_profile(path="boards/periodic-wake.toml"),
]
INTERVAL = minutes(15)
# The firmware sleeps 15 minutes after each pulse; its boot and pulse add about 42 ms to that,
# and on esp32sim the sleep lasts about 0.22 s longer than asked. The device promises a second.
TOLERANCE = seconds(1)
PULSE = ms(20)
# From the wake to the count on the console: the bootloader, the pulse and app_main.
BOOT_TIME = seconds(0.1)
WAKES = r"^periodic_wake: wakes=(?P<count>\d+)$"
# A day of the board's time with a pulse every 15 minutes: the one at power-on and 95 wakes.
DAY = hours(24)
PULSES_A_DAY = 96


def test_the_pin_pulses_every_15_minutes(board: Board) -> None:
    pin = board.lines.output("wake.pulse")
    board.power_on()
    assert next_count(board) == 0
    for count in (1, 2, 3):
        wake = board.wait_for_wake(within=INTERVAL + TOLERANCE)
        assert wake.cause == DEEPSLEEP_RESET
        assert next_count(board) == count

    starts = pulse_starts(pin)
    assert len(starts) == 4
    assert_every_interval(starts)


def test_a_day_of_wakes_spends_less_wall_time_asleep_than_booting(board: Board) -> None:
    pin = board.lines.output("wake.pulse")
    board.power_on()
    assert next_count(board) == 0
    asleep = booting = 0.0
    for count in range(1, PULSES_A_DAY):
        started = time.monotonic()
        board.wait_for_wake(within=INTERVAL + TOLERANCE)
        woke = time.monotonic()
        assert next_count(board) == count
        asleep += woke - started
        booting += time.monotonic() - woke
    assert board.now < DAY
    # Wall time, measured against the same run's boots, which a busy host slows down as much as the
    # sleeps. The runner steps through each 15-minute sleep in microseconds, and the day's wall
    # time goes into the boots: on an M2 Pro 0.4 s asleep and 6.6 s booting. Without the steps
    # esp32sim runs each sleep in idle rounds, about 3.4 s of wall time, and the day takes 330 s.
    assert asleep < booting, f"{asleep:.1f} s of wall time asleep, {booting:.1f} s booting"

    starts = pulse_starts(pin)
    assert len(starts) == PULSES_A_DAY
    assert_every_interval(starts)


def test_the_count_survives_deep_sleep_but_not_a_reset_or_a_power_cycle(board: Board) -> None:
    board.power_on()
    assert next_count(board) == 0
    for count in (1, 2):
        board.wait_for_wake(within=INTERVAL + TOLERANCE)
        assert next_count(board) == count

    # EN pulled while the chip sleeps: RTC memory starts again.
    board.wait(minutes(5))
    board.reset()
    assert next_count(board) == 0
    assert board.boot.cause == POWERON_RESET
    board.wait_for_wake(within=INTERVAL + TOLERANCE)
    assert next_count(board) == 1

    board.power_off()
    board.power_on()
    assert next_count(board) == 0
    assert board.boot.cause == POWERON_RESET


def next_count(board: Board) -> int:
    """The count the firmware prints after this boot's pulse."""
    line = board.wait_until(board.console.line(WAKES), within=BOOT_TIME)
    return int(line["count"])


def assert_every_interval(starts: list[Duration]) -> None:
    for number, (before, after) in enumerate(pairwise(starts), start=2):
        assert INTERVAL <= after - before <= INTERVAL + TOLERANCE, (
            f"pulse {number} came {after - before} after the one before it"
        )


def pulse_starts(pin: OutputLine) -> list[Duration]:
    """When the firmware drove the pin high, each pulse checked for its 20 ms."""
    starts = []
    high_since = None
    for event in pin.events():
        if event.by != "firmware":
            continue
        if event.level == pin.active:
            high_since = event.t
            starts.append(event.t)
        elif high_since is not None:
            assert PULSE <= event.t - high_since <= PULSE + ms(1)
            high_since = None
    assert high_since is None, f"{pin.name} still high since {high_since}"
    return starts
