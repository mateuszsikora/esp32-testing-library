"""The board's clock, held by the test: emulated time moves only while the test waits.

The runner stops at every turn of the link and waits for the harness's `wait` (protocol §4).
`HeldClock` keeps the link's thread in each turn for as long as the test is not waiting, so the
guest stands still while the test body runs, and whatever the test changes lands at the emulated
instant the test saw. `wait` lets the run go to a time; `wait_until` lets it go in steps of at
most `STEP_NS`, and asks the test's condition at every turn, in the test's own thread, while the
link is held. Times here are nanoseconds on the board's clock, from power-on.
"""

import threading
from collections.abc import Callable
from typing import TypeVar

from esp32tl.interfaces import NeverHappened, RunEnded
from esp32tl.units import Duration

# How far the run may go between two looks at a condition, 100 emulated milliseconds: how late
# after what it waits for a `wait_until` returns, when no other turn of the link comes sooner.
STEP_NS = 100_000_000

T = TypeVar("T")


class HeldClock:
    """The link's `Hold` for a run on the test's clock, and the test's side of it.

    The link calls `turn` from its thread; the test calls `wait` and `wait_until` from its own.
    `stop` is the link's alone: from then on the link stops the run at the turn it is held at, or
    at its next one, as at the end of the test. `end` is for the backend once the run is over: a
    test that waits gets `RunEnded` with the reason.
    """

    def __init__(self, step: int = STEP_NS) -> None:
        self._step = step
        self._cv = threading.Condition()
        self._now = 0
        # The turns the link has been held at, so that the test can tell the next one.
        self._turns = 0
        # Whether the link's thread is in a turn, waiting for the test.
        self._held = False
        # How far the test lets the run go from the turn it is held at; None while it does not.
        self._until: int | None = None
        self._stopped = False
        self._ended: str | None = None
        # Where the run ended, when the runner said so in its stats.
        self._ended_at: int | None = None

    # The link's side.

    def turn(self, now: int) -> int | None:
        """Holds the link's turn at the block point `now` until the test waits; then returns
        the latest time of the next turn, or `None` once the test is over or the run has ended,
        for the link to stop the run."""
        with self._cv:
            self._now = now
            self._turns += 1
            self._held = True
            self._until = None
            self._cv.notify_all()
            while self._until is None and not self._stopped and self._ended is None:
                self._cv.wait()
            self._held = False
            return self._until if not (self._stopped or self._ended) else None

    def stop(self) -> bool:
        """The link stops the run at its turn, and holds no turn any more. Returns whether the
        link is held at one now, so that the stop goes out at once."""
        with self._cv:
            self._stopped = True
            self._cv.notify_all()
            return self._held

    def end(self, reason: str, at: int | None = None) -> None:
        """The run is over, for `reason`, at the board's time `at` when it is known: a test that
        waits, or waits later, gets `RunEnded`."""
        with self._cv:
            if self._ended is None:
                self._ended = reason
                self._ended_at = at
            self._cv.notify_all()

    # The test's side.

    def check(self, step: str) -> None:
        """Raises `RunEnded` once the run has ended: `step`, a call on the board, can no longer
        reach it."""
        with self._cv:
            if self._ended is not None:
                raise RunEnded(f"the run ended {self._when()} before {step}: {self._ended}")

    @property
    def now(self) -> int:
        """The block point of the turn the link was last held at: 0 until the first."""
        with self._cv:
            return self._now

    def wait(self, duration: int) -> None:
        """Lets the run go for `duration` from now, and returns at the turn at its end."""
        with self._cv:
            self._settle(f"until {seconds(self._now + duration)}")
            deadline = self._now + duration
            while self._now < deadline:
                self._advance(deadline, f"until {seconds(deadline)}")

    def wait_until(self, condition: Callable[[], T | None], within: int, description: str) -> T:
        """Lets the run go until `condition` returns something true, asked now and at every
        turn, for at most `within` from now; returns what it returned. `description` names it
        in `NeverHappened` and `RunEnded`."""
        what = f"for {description}"
        with self._cv:
            try:
                self._settle(what)
            except RunEnded as ended:
                over: RunEnded | None = ended
            else:
                over = None
            start = self._now
        if over is not None:
            # The run is over, and all it did is in: what it left may still hold.
            value = condition()
            if value:
                return value
            raise over
        deadline = start + within
        while True:
            # Outside the lock, in the test's thread: the link is held, or done.
            value = condition()
            if value:
                return value
            with self._cv:
                if self._now >= deadline:
                    raise NeverHappened(
                        f"{description} did not happen within {seconds(within)}: waited from"
                        f" {seconds(start)} to {seconds(self._now)} of the board's time"
                    )
                try:
                    self._advance(min(deadline, self._now + self._step), what)
                except RunEnded as ended:
                    error = ended
                else:
                    continue
            # What the run did last is in by the time it has ended.
            value = condition()
            if value:
                return value
            raise error

    def _settle(self, what: str) -> None:
        """Waits, holding the lock, until the link is held at a turn: from power-on to the
        first `sync`, while the runner loads the images."""
        while not self._held and self._ended is None:
            self._cv.wait()
        if not self._held:
            raise self._run_ended(what)

    def _advance(self, until: int, what: str) -> None:
        """Lets the run go to `until` at most, holding the lock, and returns at the next turn."""
        self._settle(what)
        turns = self._turns
        self._until = until
        self._cv.notify_all()
        while self._turns == turns and self._ended is None:
            self._cv.wait()
        if self._turns == turns:
            raise self._run_ended(what)

    def _run_ended(self, what: str) -> RunEnded:
        return RunEnded(f"the run ended {self._when()} while the test waited {what}: {self._ended}")

    def _when(self) -> str:
        # The last turn is no more than a bound on where the run ended: the runner may have run
        # on to its next block point, or have died anywhere before it.
        if self._ended_at is not None:
            return f"at {seconds(self._ended_at)}"
        if self._turns == 0:
            return "before its first turn"
        return f"after the turn at {seconds(self._now)}"


def seconds(ns: int) -> str:
    """`ns` as seconds, without trailing zeros: `12.5 s`, `30 s`."""
    return str(Duration(ns))


def nanoseconds(value: Duration, what: str) -> int:
    """`value`, a `Duration` of the board's time, 0 or more, in nanoseconds; a `TypeError` for
    anything else, which names `what` took it, and a `ValueError` for one below 0."""
    if not isinstance(value, Duration):
        plain = isinstance(value, int | float) and not isinstance(value, bool)
        example = f"seconds({value!r})" if plain else "seconds(5)"
        raise TypeError(f"{what} takes a Duration, as {example}, not {value!r}")
    if value.ns < 0:
        raise ValueError(f"{what} takes a Duration of 0 or more, not {value}")
    return value.ns
