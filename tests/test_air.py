"""The air's loop on the board's clock (`esp32tl/bluetooth/air.py`), without an emulator or a
controller: asyncio's timers fire at their board time, time moves only when the test moves it, a
party is polled at every instant and stops the air at its own, and what fails on the air is raised
to the test."""

import asyncio
import math
import time
from collections.abc import Iterator

import pytest

from esp32tl.bluetooth.air import Air, AirError
from esp32tl.interfaces import NeverHappened
from esp32tl.units import Duration, hours, ms, seconds

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


@pytest.mark.parametrize("start", [1_000, 6_000])
def test_what_a_timer_sets_off_runs_at_the_timer_s_instant(air: Air, start: int) -> None:
    """At 6 s, 5.12 s later is 11.120000000000001 s, a hair above the instant the air stops at;
    at 1 s it is 6.12 s exactly. Either way, what the timer schedules runs where it fires."""
    air.advance(ms(start))
    log: list[int] = []

    def timer() -> None:
        log.append(air.loop.now)
        air.loop.call_soon(lambda: log.append(air.loop.now))

    air.call(lambda: air.loop.call_later(5.12, timer))
    air.advance(ms(start + 6_000))
    assert log == [(start + 5_120) * MS] * 2


def test_a_timer_a_nanosecond_away_fires_at_the_next_nanosecond(air: Air) -> None:
    """At 269 272 418 ns, `time()` and 1 ns come to a hair above the timer's float: a loop that
    fired what is due within 1 ns would fire it at once."""
    air.advance(Duration(269_272_418))
    fired: list[int] = []
    air.call(lambda: air.loop.call_later(1e-9, lambda: fired.append(air.loop.now)))
    assert fired == []
    air.advance(ms(300))
    assert fired == [269_272_419]


@pytest.mark.parametrize("limit", [math.inf, 1e300], ids=["infinite", "huge"])
def test_a_timer_with_no_nanosecond_never_fires(air: Air, limit: float) -> None:
    """A timeout of `math.inf`, or one whose nanoseconds overflow a float, is asyncio's own: the
    awaitable completes, and the timer never fires. Like any timer more than 24 hours away, it
    stops the air at an empty instant 24 hours on, asyncio's longest wait."""

    async def bounded() -> str:
        return await asyncio.wait_for(asyncio.sleep(1, "slept"), timeout=limit)

    assert air.run(bounded(), within=seconds(2)) == "slept"
    assert air.now == seconds(1)
    fired: list[int] = []
    air.call(lambda: air.loop.call_at(limit, lambda: fired.append(air.loop.now)))
    air.advance(seconds(3))
    assert air.quiet_until() == (seconds(3) + hours(24)).ns
    air.advance(hours(49))
    assert fired == []


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
    # A sixtieth of the hour a loop on the host's clock would take; the run takes milliseconds.
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
