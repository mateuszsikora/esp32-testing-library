"""The board's clock, held by the test: emulated time moves only while the test waits.

The runner stops at every turn of the link and waits for the harness's `wait` (protocol §4).
`HeldClock` keeps the link's thread in each turn for as long as the test is not waiting, so the
guest stands still while the test body runs, and whatever the test changes lands at the emulated
instant the test saw. `wait` lets the run go to a time; `wait_until` lets it go in steps of at
most `STEP_NS`, and asks the test's condition at every turn, in the test's own thread, while the
link is held. `wait_for_event` lets it go to its deadline at once, with a `latency` that has the
runner take a turn soon after each event, and asks its condition at those turns only: the wait
through a deep sleep, in which nothing happens until the chip wakes. `dump_flash` has the link
ask the runner for the flash in the turn it is held at, on the link's thread, and `write_flash`
has it write a file into the flash, with the board's supply off; the turn goes on: the board's
time does not move. `fail` takes an exception of the test's code that the link's thread ran while
the run went, as a broker's or the HTTP server's handler: the waiting test raises it once the run
reaches its next turn, and the run goes on. Times here are nanoseconds on the board's clock, from
power-on.
"""

import threading
from collections.abc import Callable
from typing import TypeVar

from esp32tl.interfaces import NeverHappened, RunEnded
from esp32tl.interfaces.condition import waiting
from esp32tl.link.boardlink import RunTo
from esp32tl.link.protocol import FlashDump, FlashDumpAck, FlashWrite, FlashWriteAck
from esp32tl.net.handlers import refuse_wait
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
    test that waits gets `RunEnded` with the reason. `fail` is for what the link's thread ran of
    the test's: the test's wait raises the first such error at the next turn.
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
        # How soon after an event the test wants the next turn; None for no sooner than _until.
        self._latency: int | None = None
        # What the test asks of the runner's flash in the turn; the runner's answer, once it has
        # come.
        self._asked: FlashDump | FlashWrite | None = None
        self._answer: FlashDumpAck | FlashWriteAck | None = None
        self._stopped = False
        self._ended: str | None = None
        # Where the run ended, when the runner said so in its stats.
        self._ended_at: int | None = None
        # The first exception of the test's code that the link's thread ran, for the test's wait.
        self._failure: BaseException | None = None

    # The link's side.

    def turn(self, now: int) -> int | RunTo | FlashDump | FlashWrite | None:
        """Holds the link's turn at the block point `now` until the test waits; then returns
        the latest time of the next turn, as a `RunTo` when the wait asks for a turn soon after
        each event, or `None` once the test is over or the run has ended, for the link to stop
        the run. Returns `FlashDump` when the test asks for the flash, and `FlashWrite` when it
        writes into it."""
        with self._cv:
            self._now = now
            self._turns += 1
            self._until = None
            self._latency = None
            return self._hold_turn()

    def answered(
        self, ack: FlashDumpAck | FlashWriteAck
    ) -> int | RunTo | FlashDump | FlashWrite | None:
        """The runner's answer goes to the test, and the same turn is held on: as `turn`."""
        with self._cv:
            self._answer = ack
            return self._hold_turn()

    def _hold_turn(self) -> int | RunTo | FlashDump | FlashWrite | None:
        self._held = True
        self._cv.notify_all()
        while (
            self._until is None
            and self._asked is None
            and not self._stopped
            and self._ended is None
        ):
            self._cv.wait()
        self._held = False
        if self._stopped or self._ended:
            return None
        if self._asked is not None:
            asked, self._asked = self._asked, None
            return asked
        if self._latency is not None and self._until is not None:
            return RunTo(self._until, self._latency)
        return self._until

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

    def fail(self, error: BaseException) -> None:
        """`error`, raised by the test's code that the link's thread ran, as a broker's or the HTTP
        server's handler, goes to the test's wait, which raises it at the next turn; only the first
        one before the wait raises. A `RunEnded` goes as a `RuntimeError` that names it, with it
        as its cause: a wait, and the console's end of file, take a `RunEnded` for the end of the
        run."""
        if isinstance(error, RunEnded):
            wrapped = RuntimeError(f"a handler of the test's raised RunEnded: {error}")
            wrapped.__cause__ = error
            error = wrapped
        with self._cv:
            if self._failure is None:
                self._failure = error
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

    @property
    def ended(self) -> bool:
        """Whether the run is over: no turn is coming, and nothing more reaches the runner."""
        with self._cv:
            return self._ended is not None

    def dump_flash(self) -> FlashDumpAck | None:
        """Has the runner write the flash in the turn the link is held at, and returns its answer;
        `None` once the run is over, when the runner has written it for the last time. Emulated
        time does not move."""
        answer = self._ask(FlashDump())
        assert answer is None or isinstance(answer, FlashDumpAck)
        return answer

    def write_flash(self, offset: int, path: str) -> FlashWriteAck | None:
        """Has the runner write the file at `path` into the flash at `offset` in the turn the link
        is held at, with the board's supply off, and returns its answer; `None` once the run is
        over, and nothing written. Emulated time does not move."""
        answer = self._ask(FlashWrite(offset, path))
        assert answer is None or isinstance(answer, FlashWriteAck)
        return answer

    def _ask(self, request: FlashDump | FlashWrite) -> FlashDumpAck | FlashWriteAck | None:
        """`request` to the runner in the turn the link is held at, once it is held at one, and
        the runner's answer; `None` once the run is over."""
        # From the link's thread, which alone holds a turn, this would wait for itself.
        refuse_wait("the board's clock")
        with self._cv:
            while not self._held and self._ended is None:
                self._cv.wait()
            if self._ended is not None:
                return None
            self._answer = None
            self._asked = request
            self._cv.notify_all()
            while self._answer is None and self._ended is None:
                self._cv.wait()
            self._asked = None
            return self._answer

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
        return self._wait(condition, within, description, self._step, None)

    def wait_for_event(
        self, condition: Callable[[], T | None], within: int, description: str, latency: int
    ) -> T:
        """`wait_until` without its steps: lets the run go to `within` from now, and asks
        `condition` now and at every turn the link takes, which comes no later than `latency`
        after each event the runner sends. For a condition only an event can make true, as a
        chip's reset; a wait through a stretch without events takes no turn."""
        return self._wait(condition, within, description, None, latency)

    def _wait(
        self,
        condition: Callable[[], T | None],
        within: int,
        description: str,
        step: int | None,
        latency: int | None,
    ) -> T:
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
                end = self._now
                if end < deadline:
                    until = deadline if step is None else min(deadline, end + step)
            if end >= deadline:
                # Outside the lock too, once: the condition's own words on why it did not hold.
                why = waiting(condition)
                raise NeverHappened(
                    f"{description} did not happen within {seconds(within)}: "
                    + (f"{why}; " if why is not None else "")
                    + f"waited from {seconds(start)} to {seconds(end)} of the board's time",
                    why,
                )
            with self._cv:
                try:
                    self._advance(until, what, latency)
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
        first `sync`, while the runner loads the images. Refused from a handler on the link's
        thread, whatever the board's call that came here, which names itself first."""
        refuse_wait("the board's clock")
        while not self._held and self._ended is None:
            self._cv.wait()
        if not self._held:
            raise self._run_ended(what)

    def _advance(self, until: int, what: str, latency: int | None = None) -> None:
        """Lets the run go to `until` at most, with a turn no later than `latency` after each
        event when it is given, holding the lock, and returns at the next turn."""
        self._settle(what)
        turns = self._turns
        self._until = until
        self._latency = latency
        self._cv.notify_all()
        while self._turns == turns and self._ended is None:
            self._cv.wait()
        if self._failure is not None:
            # In the test's thread, with the traceback of where the link's thread raised it.
            failure, self._failure = self._failure, None
            raise failure
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
