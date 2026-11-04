"""The board profile's GPIO lines, `[lines.<role>]`: what the test drives on them, and what the
firmware drives. The `lines` fixture is the one of every line set of the profile."""

import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from esp32tl.boards import Line
from esp32tl.interfaces import SourceError
from esp32tl.link.boardlink import Interface
from esp32tl.link.protocol import GpioIn, Input, Release
from esp32tl.report import RunLog
from esp32tl.units import Duration

# The level a board's pull gives a line that neither end drives.
PULLED = {"up": 1, "down": 0}


@dataclass(frozen=True)
class LineEvent:
    """A change on a line: at `t`, an instant of the board's time, `by` the firmware, test or model,
    the line `name` (`modem.ri`) on GPIO `pin` driven to `level`, or with `None` no longer
    driven: the firmware's output off, or the test's `release`."""

    t: Duration
    name: str
    pin: int
    by: Literal["firmware", "test", "model"]
    level: int | None


@dataclass(frozen=True)
class OutputCondition:
    """A live firmware-output condition, callable directly or passed to board.wait_until."""

    output: "OutputLine"
    on: bool

    @property
    def _level(self) -> int:
        return self.output.active if self.on else 1 - self.output.active

    def __call__(self) -> bool:
        return self.output._lines.level(self.output.pin) == self._level

    def __repr__(self) -> str:
        state = "on" if self.on else "off"
        return f"{self.output.name} is {state} (GPIO {self.output.pin}, level {self._level})"

    def __bool__(self) -> bool:
        raise TypeError("call the output condition with (), or pass it to board.wait_until")


@dataclass(frozen=True)
class OutputLine:
    """A read-only view of one firmware output; creating it configures no GPIO."""

    _lines: "Lines" = field(repr=False)
    name: str
    pin: int
    active: int

    @property
    def is_on(self) -> OutputCondition:
        """A condition for the active level; false while the firmware leaves it undriven."""
        return OutputCondition(self, on=True)

    @property
    def is_off(self) -> OutputCondition:
        """A condition for the inactive level; false while the firmware leaves it undriven."""
        return OutputCondition(self, on=False)

    def events(self) -> list[LineEvent]:
        """This line's changes by firmware, test and model, on the board's clock."""
        return [event for event in self._lines.events() if event.pin == self.pin]

    def assert_on(self) -> None:
        """Assert the current firmware output is active, without advancing time."""
        self._assert_level(self._lines.level(self.pin), on=True, when="now")

    def assert_off(self) -> None:
        """Assert the current firmware output is inactive, without advancing time."""
        self._assert_level(self._lines.level(self.pin), on=False, when="now")

    def assert_stayed_on(self, *, since: Duration) -> None:
        """Assert the output stayed active from `since` through now, including short pulses."""
        self._assert_stayed(on=True, since=since)

    def assert_stayed_off(self, *, since: Duration) -> None:
        """Assert the output stayed inactive from `since` through now, including short pulses."""
        self._assert_stayed(on=False, since=since)

    def _assert_level(self, level: int | None, *, on: bool, when: str) -> None:
        expected = self.active if on else 1 - self.active
        if level != expected:
            state = "on" if on else "off"
            actual = "undriven/unknown" if level is None else f"level {level}"
            raise AssertionError(
                f"{self.name} expected {state} (GPIO {self.pin}, level {expected}) "
                f"{when}; observed {actual}"
            )

    def _assert_stayed(self, *, on: bool, since: Duration) -> None:
        if not isinstance(since, Duration):
            raise TypeError("since must be a Duration on the board's clock")
        now = Duration(self._lines._now())
        if not 0 <= since.ns <= now.ns:
            raise ValueError("since must be between zero and board.now")
        events = [event for event in self.events() if event.by == "firmware"]
        level = None
        for event in events:
            if event.t > since:
                break
            level = event.level
        window = f"during [{since.seconds:g}, {now.seconds:g}] s"
        self._assert_level(level, on=on, when=f"at {since.seconds:g} s {window}")
        for event in events:
            if since < event.t <= now:
                self._assert_level(event.level, on=on, when=f"at {event.t.seconds:g} s {window}")


class Lines(Interface):
    """The `[lines.<role>]` sets of the board profile `board`, one interface on the link, `lines`.

    A line is named by its GPIO number or as `<role>.<line>`, `modem.ri`. `drive` and `release`
    act at the board's current instant, `now()` in nanoseconds, which is 0 before power-on: from
    the start of the run. A line the board pulls (`pull = "down"` in the profile) is at its pull
    while the test does not drive it, from power-on and after a `release`; on the chip the pull is
    the weaker side, so the firmware's own output wins over it. `level` says what the firmware
    drives, and `events` every change of either end. With a `log`, each call of the test's is a step
    of the run, and each change of the firmware's an entry.

    A model can reserve an output and observe a firmware input through `attach`; its changes
    use `model_drive`. The test calls from its own thread while the link is held, the link
    from the link's.
    """

    def __init__(
        self,
        board: str,
        lines: Mapping[str, Mapping[str, Line]],
        now: Callable[[], int],
        log: RunLog | None = None,
    ) -> None:
        super().__init__("lines", None)
        self.board = board
        self._now = now
        self.log = log
        self._lines = {
            f"{role}.{name}": line
            for role, members in lines.items()
            for name, line in members.items()
        }
        self._names = {line.pin: name for name, line in self._lines.items()}
        self._lock = threading.Lock()
        # What the test asked for and the link has not sent: (t_ns, pin, level, None to release).
        self._pending: list[tuple[int, int, int | None]] = []
        # Whether the pulls have gone out, in the run's first turn.
        self._pulled = False
        # What the test drives each line to.
        self._driving: dict[int, int] = {}
        # The firmware's side, from the link's gpio_out: each pin's last reported pad level, and
        # what the firmware drives each line to, None while it does not.
        self._pads: dict[int, int] = {}
        self._levels: dict[int, int | None] = {}
        self._events: list[LineEvent] = []
        self._owners: dict[int, str] = {}
        self._observers: dict[int, list[Callable[[int, int | None], None]]] = {}
        self._latency: int | None = None
        # Hold model responses until due: a test may change the model before its response.
        self._model_pending: dict[int, tuple[int, int]] = {}
        self._model_levels: dict[int, int] = {}

    @property
    def pins(self) -> frozenset[int]:
        return frozenset(self._names)

    def drive(self, line: int | str, level: int) -> None:
        """Drives `line` to `level`, 0 or 1, from the board's current instant on, until the test
        drives it again or releases it."""
        pin = self._pin(line)
        self._unowned(pin)
        if isinstance(level, bool):
            level = int(level)
        if not isinstance(level, int):
            raise TypeError(f"lines.drive: level {level!r} is not 0 or 1")
        if level not in (0, 1):
            raise ValueError(f"lines.drive: level {level!r} is not 0 or 1")
        now = self._now()
        if self.log is not None:
            self.log.test(now, f"lines.drive({line!r}, {level})")
        with self._lock:
            if self._driving.get(pin) == level:
                return
            self._driving[pin] = level
            self._change(now, pin, level)

    def release(self, line: int | str) -> None:
        """Stops driving `line` from the board's current instant on: it goes back to the board's
        pull, if it has one, or to what the chip makes of an undriven pad."""
        pin = self._pin(line)
        self._unowned(pin)
        now = self._now()
        if self.log is not None:
            self.log.test(now, f"lines.release({line!r})")
        with self._lock:
            if self._driving.pop(pin, None) is None:
                return
            self._change(now, pin, None)

    def level(self, line: int | str) -> int | None:
        """What the firmware drives `line` to, 0 or 1; `None` while it does not drive it. A
        condition for `board.wait_until`, as in `lambda: lines.level("modem.pwrkey") == 1`."""
        pin = self._pin(line)
        with self._lock:
            return self._levels.get(pin)

    def output(self, line: int | str, *, active: int = 1) -> OutputLine:
        """Observe one firmware output with named is_on/is_off conditions.

        active=1 means high is on; active=0 means low is on. This polarity is explicit,
        independent of any profile model's active field. Neither condition accepts an
        undriven output. The name is validated now, before the test starts waiting.
        """
        pin = self._pin(line)
        if isinstance(active, bool):
            active = int(active)
        if not isinstance(active, int):
            raise TypeError(f"lines.output: active {active!r} is not 0 or 1")
        if active not in (0, 1):
            raise ValueError(f"lines.output: active {active!r} is not 0 or 1")
        return OutputLine(self, self._names[pin], pin, active)

    def events(self) -> list[LineEvent]:
        """Every change on the lines so far, by either end, in the order of the board's time."""
        with self._lock:
            return sorted(self._events, key=lambda event: event.t.ns)

    def _pin(self, line: int | str) -> int:
        if isinstance(line, str) and line in self._lines:
            return self._lines[line].pin
        if isinstance(line, int) and not isinstance(line, bool) and line in self._names:
            return line
        if not self._lines:
            raise ValueError(f"{line!r}: the board profile {self.board} has no lines")
        known = ", ".join(f"{name} (GPIO {ln.pin})" for name, ln in self._lines.items())
        what = (
            f"GPIO {line}" if isinstance(line, int) and not isinstance(line, bool) else repr(line)
        )
        raise ValueError(
            f"{what} is no line of the board profile {self.board}; its lines are {known}"
        )

    def _unowned(self, pin: int) -> None:
        if pin in self._owners:
            raise SourceError(f"{self._names[pin]} is driven by {self._owners[pin]}")

    def attach(
        self,
        owner: str,
        *,
        output: str,
        watch: Mapping[str, Callable[[int, int | None], None]],
        latency: int,
    ) -> None:
        """Give a model one output and the firmware changes of each line in `watch`, before
        power-on.

        Lines stays the GPIO owner on BoardLink. A test cannot also drive the model's output.
        Callbacks run outside the lock, so a response may enqueue a model_drive.
        """
        pin = self._pin(output)
        observers = {self._pin(line): driven for line, driven in watch.items()}
        with self._lock:
            self._unowned(pin)
            if pin in self._driving:
                raise SourceError(f"{output} is driven by the test already")
            self._owners[pin] = owner
            for observed, driven in observers.items():
                self._observers.setdefault(observed, []).append(driven)
            self._latency = latency if self._latency is None else min(latency, self._latency)

    def model_drive(self, owner: str, line: str, t: int, level: int) -> None:
        """Replace the model's pending response on this line; hold it until its due time."""
        pin = self._pin(line)
        with self._lock:
            if self._owners.get(pin) != owner:
                raise SourceError(f"{owner} does not own {line}")
            self._model_pending[pin] = (t, level)

    def quiet_until(self) -> int | None:
        """The earliest pending model response; none after all responses have been sent."""
        with self._lock:
            return min((t for t, _ in self._model_pending.values()), default=None)

    def latency(self) -> int | None:
        """The shortest response latency among attached models, or none without models."""
        return self._latency

    def _change(self, now: int, pin: int, level: int | None) -> None:
        self._pending.append((now, pin, level))
        self._events.append(LineEvent(Duration(now), self._names[pin], pin, "test", level))

    # The link's side.

    def inputs(self, now: int) -> Iterable[Input]:
        """The board's pulls in the run's first turn, then what the test has asked for since the
        last turn: at its time, or at `now` for what came in after the turn it was asked at. A
        release of a pulled line drives it to its pull rather than releasing it: the chip knows
        only its own pulls (protocol 5.11)."""
        with self._lock:
            messages: list[Input] = []
            if not self._pulled:
                self._pulled = True
                pulls = tuple(
                    (line.pin, PULLED[line.pull]) for line in self._lines.values() if line.pull
                )
                if pulls:
                    messages.append(GpioIn(now, pulls))
            for t, pin, level in self._pending:
                t = max(t, now)
                pull = self._lines[self._names[pin]].pull
                if level is not None:
                    messages.append(GpioIn(t, ((pin, level),)))
                elif pull is not None:
                    messages.append(GpioIn(t, ((pin, PULLED[pull]),)))
                else:
                    messages.append(Release(t, (pin,)))
            self._pending.clear()
            for pin, (t, level) in list(self._model_pending.items()):
                if t > now:
                    continue
                del self._model_pending[pin]
                if self._model_levels.get(pin) != level:
                    self._model_levels[pin] = level
                    messages.append(GpioIn(now, ((pin, level),)))
                    self._events.append(
                        LineEvent(Duration(now), self._names[pin], pin, "model", level)
                    )
                    if self.log is not None:
                        self.log.line(now, self._names[pin], level, direction="in")
            return messages

    def driven(self, t: int, changes: Sequence[tuple[int, int]], enabled: frozenset[int]) -> None:
        """The firmware drives a line where its output is enabled, or where its pad is reported
        high: a pad a peripheral drives through the GPIO matrix may have no bit in `enabled`, and
        an undriven pad is reported low (protocol 5.4)."""
        notifications: list[tuple[Callable[[int, int | None], None], int | None]] = []
        with self._lock:
            for pin, level in changes:
                self._pads[pin] = level
            for pin in sorted(self._names):
                pad = self._pads.get(pin, 0)
                state = pad if pin in enabled or pad == 1 else None
                if state != self._levels.get(pin):
                    self._levels[pin] = state
                    self._events.append(
                        LineEvent(Duration(t), self._names[pin], pin, "firmware", state)
                    )
                    if self.log is not None:
                        self.log.line(t, self._names[pin], state)
                    notifications.extend(
                        (callback, state) for callback in self._observers.get(pin, ())
                    )
        for callback, state in notifications:
            callback(t, state)
