"""The air's loop on the board's clock (`esp32tl/bluetooth/air.py`), without an emulator or a
controller: asyncio's timers fire at their board time, time moves only when the test moves it, a
party is polled at every instant and stops the air at its own, and what fails on the air is raised
to the test."""

import asyncio
import time
from collections.abc import Iterator

import pytest

from esp32tl.bluetooth.air import Air, AirError
from esp32tl.interfaces import NeverHappened
from esp32tl.units import hours, ms, seconds

MS = 1_000_000


@pytest.fixture
def air() -> Iterator[Air]:
    air = Air()
    yield air
    air.close()


def test_a_timer_set_at_any_instant_falls_on_its_exact_nanosecond(air: Air) -> None:
    """A float of seconds can land a hair above a nanosecond: at 49 ms, 5.12 s later is
    5.169000000000001 s. The air stops on the nanosecond itself, not on the next one."""
    air.advance(ms(49))
    fired: list[int] = []
    air.call(lambda: air.loop.call_later(5.12, lambda: fired.append(air.loop.now)))
    assert air.quiet_until() == 5_169 * MS
    air.advance(seconds(6))
    assert fired == [5_169 * MS]


def test_timers_fire_at_their_board_time_and_only_while_the_test_moves_it(air: Air) -> None:
    fired: list[int] = []

    async def ticks() -> str:
        for _ in range(3):
            await asyncio.sleep(0.25)
            fired.append(air.loop.now)
        return "done"

    task = air.start(ticks())
    air.advance(ms(600))
    assert fired == [250 * MS, 500 * MS]
    assert air.now == ms(600)
    assert air.quiet_until() == 750 * MS
    assert air.run(task, within=seconds(1)) == "done"
    assert fired == [250 * MS, 500 * MS, 750 * MS]
    assert air.now == ms(750)


def test_an_hour_on_the_air_takes_no_hour_of_the_host_s(air: Air) -> None:
    started = time.monotonic()
    air.run(asyncio.sleep(3600), within=hours(2))
    assert air.now == hours(1)
    assert time.monotonic() - started < 60


def test_a_task_that_does_not_complete_within_its_limit_never_happened(air: Air) -> None:
    async def forever() -> None:
        await asyncio.sleep(10)

    with pytest.raises(NeverHappened) as raised:
        air.run(forever(), within=seconds(2), description="the long sleep")
    assert str(raised.value) == (
        "the long sleep did not complete within 2 s: waited from 0 s to 2 s of the board's time"
    )
    assert air.now == seconds(2)
    # The task is cancelled: nothing is left due.
    assert air.quiet_until() is None


def test_the_air_does_not_go_back(air: Air) -> None:
    air.wait(seconds(1))
    with pytest.raises(ValueError, match=r"cannot go back to 0\.5 s"):
        air.advance(ms(500))
    with pytest.raises(TypeError, match=r"air\.advance takes a Duration, as seconds\(1\), not 1"):
        air.advance(1)  # type: ignore[arg-type]


class Ticker:
    """A party with something to do every `period`, from `period` on."""

    def __init__(self, period: int) -> None:
        self.period = period
        self.polled: list[int] = []

    def poll(self, now: int) -> None:
        self.polled.append(now)

    def quiet_until(self) -> int | None:
        last = self.polled[-1] if self.polled else 0
        return (last // self.period + 1) * self.period


def test_a_party_is_polled_at_every_instant_the_air_stops_at(air: Air) -> None:
    ticker = Ticker(300 * MS)
    air.join(ticker)
    air.start(asyncio.sleep(0.5))
    air.advance(seconds(1))
    # Its own instants, the loop's timer at 0.5 s, and the end of the advance; an instant may be
    # polled more than once.
    assert sorted(set(ticker.polled)) == [0, 300 * MS, 500 * MS, 600 * MS, 900 * MS, 1000 * MS]


def test_a_party_due_in_the_past_fails_the_air(air: Air) -> None:
    class Stuck(Ticker):
        def quiet_until(self) -> int | None:
            return 0

    air.wait(seconds(1))
    air.join(Stuck(1))
    with pytest.raises(AirError, match="due at 0 s, and the air is at 1 s already"):
        air.wait(seconds(1))


def test_a_callback_that_raises_fails_the_call_that_moved_the_time(air: Air) -> None:
    def broken() -> None:
        raise KeyError("lost")

    air.loop.call_later(0.1, broken)
    with pytest.raises(AirError) as raised:
        air.wait(seconds(1))
    assert isinstance(raised.value.__cause__, KeyError)
    assert air.now == ms(100)
