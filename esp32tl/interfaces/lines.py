"""The board profile's GPIO lines, `[lines.<role>]`: what the test drives on them, and what the
firmware drives. The `lines` fixture is the one of every line set of the profile."""

import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from esp32tl.boards import Line
from esp32tl.link.boardlink import Interface
from esp32tl.link.protocol import GpioIn, Input, Release

# The level a board's pull gives a line that neither end drives.
PULLED = {"up": 1, "down": 0}


@dataclass(frozen=True)
class LineEvent:
    """A change on a line: at `t`, seconds of the board's time, `by` the firmware or the test,
    the line `name` (`modem.ri`) on GPIO `pin` driven to `level`, or with `None` no longer
    driven: the firmware's output off, or the test's `release`."""

    t: float
    name: str
    pin: int
    by: Literal["firmware", "test"]
    level: int | None


class Lines(Interface):
    """The `[lines.<role>]` sets of the board profile `board`, one interface on the link, `lines`.

    A line is named by its GPIO number or as `<role>.<line>`, `modem.ri`. `drive` and `release`
    act at the board's current instant, `now()` in nanoseconds, which is 0 before power-on: from
    the start of the run. A line the board pulls (`pull = "down"` in the profile) is at its pull
    while the test does not drive it, from power-on and after a `release`; on the chip the pull is
    the weaker side, so the firmware's own output wins over it. `level` says what the firmware
    drives, and `events` every change of either end.

    The test calls it from its own thread while the link is held, the link from the link's.
    """

    def __init__(
        self, board: str, lines: Mapping[str, Mapping[str, Line]], now: Callable[[], int]
    ) -> None:
        super().__init__("lines", None)
        self.board = board
        self._now = now
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

    @property
    def pins(self) -> frozenset[int]:
        return frozenset(self._names)

    def drive(self, line: int | str, level: int) -> None:
        """Drives `line` to `level`, 0 or 1, from the board's current instant on, until the test
        drives it again or releases it."""
        pin = self._pin(line)
        if isinstance(level, bool):
            level = int(level)
        if not isinstance(level, int):
            raise TypeError(f"lines.drive: level {level!r} is not 0 or 1")
        if level not in (0, 1):
            raise ValueError(f"lines.drive: level {level!r} is not 0 or 1")
        now = self._now()
        with self._lock:
            if self._driving.get(pin) == level:
                return
            self._driving[pin] = level
            self._change(now, pin, level)

    def release(self, line: int | str) -> None:
        """Stops driving `line` from the board's current instant on: it goes back to the board's
        pull, if it has one, or to what the chip makes of an undriven pad."""
        pin = self._pin(line)
        now = self._now()
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

    def events(self) -> list[LineEvent]:
        """Every change on the lines so far, by either end, in the order of the board's time."""
        with self._lock:
            return list(self._events)

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

    def _change(self, now: int, pin: int, level: int | None) -> None:
        self._pending.append((now, pin, level))
        self._events.append(LineEvent(now / 10**9, self._names[pin], pin, "test", level))

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
            return messages

    def driven(self, t: int, changes: Sequence[tuple[int, int]], enabled: frozenset[int]) -> None:
        """The firmware drives a line where its output is enabled, or where its pad is reported
        high: a pad a peripheral drives through the GPIO matrix may have no bit in `enabled`, and
        an undriven pad is reported low (protocol 5.4)."""
        with self._lock:
            for pin, level in changes:
                self._pads[pin] = level
            for pin in sorted(self._names):
                pad = self._pads.get(pin, 0)
                state = pad if pin in enabled or pad == 1 else None
                if state != self._levels.get(pin):
                    self._levels[pin] = state
                    self._events.append(
                        LineEvent(t / 10**9, self._names[pin], pin, "firmware", state)
                    )
