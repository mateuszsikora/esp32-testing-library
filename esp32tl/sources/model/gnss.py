"""A GNSS receiver at the far end of a UART, described by what it sees: where it is, how it moves,
how good its fix is (docs/features/models-user.md).

The receiver sends one epoch a second, at every whole second of the world's time from the first
one at least `FIRST_EPOCH` after power-on: an RMC and then a GGA, NMEA 0183 with talker `GP`, with
the same UTC time. What the test says holds from the board's current instant until it says
something else, as the world does: `move()` takes no duration, and the receiver goes on moving
while the test waits. A move follows the great circle it starts on. Until the test gives it a fix,
the receiver talks but has none, as one does after a cold start. It hears what the firmware sends
it, and answers nothing.
"""

import math
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from esp32tl.link.boardlink import Output
from esp32tl.units import (
    EARTH_RADIUS_M,
    NORTH,
    Distance,
    Duration,
    Heading,
    Position,
    Speed,
    m_s,
    number,
    seconds,
)

# The first epoch is the first whole second of the world's time at least this long after
# power-on: 1 s with the default world's time, whose power-on is on a whole second.
FIRST_EPOCH = seconds(1)
PERIOD = seconds(1)

TALKER = "GP"

# What a fix has unless the test says otherwise.
HDOP = 0.9
SATELLITES = 8
# The HDOP a receiver reports without a fix, as u-blox receivers do.
NO_FIX_HDOP = 99.99

# NMEA 0183's longest sentence, from `$` to the line ending included.
SENTENCE_MAX = 82

STILL = m_s(0)


class ModelError(ValueError):
    """What the test tells the receiver that it cannot be: a move with no position to start from,
    a speed below 0, a fix without satellites."""


@dataclass(frozen=True)
class State:
    """What the receiver sees from `since`, in nanoseconds of the board's time, on: where it was
    then (`None` until its first fix), how it moves, its fix, and whether it talks."""

    since: int
    position: Position | None
    heading: Heading
    speed: Speed
    fixed: bool
    hdop: float
    satellites: int
    talking: bool

    def travelled(self, t: int) -> Distance:
        return self.speed * Duration(t - self.since)

    def at(self, t: int) -> Position | None:
        """Where the receiver is at `t`."""
        if self.position is None or self.speed.m_s == 0:
            return self.position
        return self.position.moved(self.heading, self.travelled(t))

    def course(self, t: int) -> Heading:
        """The heading at `t` along the great circle the move started on."""
        if self.position is None or self.speed.m_s == 0:
            return self.heading
        d = self.travelled(t).m / EARTH_RADIUS_M
        lat = math.radians(self.position.lat)
        theta = math.radians(self.heading.degrees)
        return Heading(
            math.degrees(
                math.atan2(
                    math.sin(theta) * math.cos(lat),
                    math.cos(d) * math.cos(lat) * math.cos(theta) - math.sin(lat) * math.sin(d),
                )
            )
        )


class GnssReceiver:
    """The receiver the `gnss` fixture becomes at the first model call before power-on.

    `now` gives the board's time, in nanoseconds, which each change takes effect at; `world` the
    world's time at power-on, which the sentences are dated from, asked once the run has started.
    The methods besides the test's are those of `Source`, for the link.
    """

    def __init__(self, now: Callable[[], int], world: Callable[[], datetime]) -> None:
        self._now = now
        self._world = world
        self._lock = threading.Lock()
        self._state = State(0, None, NORTH, STILL, False, NO_FIX_HDOP, 0, True)
        # The time of the next epoch, once the run has started.
        self._next: int | None = None
        # How many epochs the link has been given.
        self.sent = 0

    # What the test tells it.

    def fix(self, at: Position, *, hdop: float = HDOP, satellites: int = SATELLITES) -> None:
        """A fix at `at`, standing still, of `hdop` with `satellites` in use."""
        if not isinstance(at, Position):
            raise TypeError(f"gnss.fix: at is {at!r}, not a Position")
        self._change(
            position=at,
            speed=STILL,
            fixed=True,
            hdop=_hdop(hdop, "gnss.fix"),
            satellites=_satellites(satellites, "gnss.fix"),
        )

    def move(self, *, heading: Heading, speed: Speed) -> None:
        """Moving from where it is now, on `heading`, at `speed`; with or without a fix."""
        if not isinstance(heading, Heading):
            raise TypeError(f"gnss.move: heading is {heading!r}, not a Heading, as NORTH")
        if not isinstance(speed, Speed):
            raise TypeError(f"gnss.move: speed is {speed!r}, not a Speed, as km_h(50)")
        if speed.m_s < 0:
            raise ModelError(f"gnss.move: speed is {speed}, less than 0")
        with self._lock:
            if self._state.position is None:
                raise ModelError(
                    "gnss.move: the receiver has no position to move from: give it a fix first,"
                    " with gnss.fix(at=...)"
                )
            self._change_locked(heading=heading, speed=speed)

    def stand_still(self) -> None:
        """Standing where it is now."""
        self._change(speed=STILL)

    def degrade(self, *, hdop: float | None = None, satellites: int | None = None) -> None:
        """The fix it has, of another HDOP, another number of satellites, or both: worse, or
        better."""
        if hdop is None and satellites is None:
            raise TypeError("gnss.degrade: give it hdop=, satellites=, or both")
        changes: dict[str, Any] = {}
        if hdop is not None:
            changes["hdop"] = _hdop(hdop, "gnss.degrade")
        if satellites is not None:
            changes["satellites"] = _satellites(satellites, "gnss.degrade")
        with self._lock:
            if not self._state.fixed:
                raise ModelError(
                    "gnss.degrade: the receiver has no fix to degrade: give it one first,"
                    " with gnss.fix(at=...)"
                )
            self._change_locked(**changes)

    def lose_fix(self) -> None:
        """No fix from now on, until the next `fix`. The receiver goes on moving as it did."""
        self._change(fixed=False, hdop=NO_FIX_HDOP, satellites=0)

    def go_silent(self) -> None:
        """No sentences from now on, until `resume`."""
        self._change(talking=False)

    def resume(self) -> None:
        """Sentences again, from the next epoch on."""
        self._change(talking=True)

    @property
    def position(self) -> Position | None:
        """Where the receiver is now, fix or not; `None` until its first fix."""
        with self._lock:
            return self._state.at(self._now())

    def _change(self, **changes: Any) -> None:
        with self._lock:
            self._change_locked(**changes)

    def _change_locked(self, **changes: Any) -> None:
        now = self._now()
        state = self._state
        # Where it is and which way it goes now, for the move from here on.
        here = {"position": state.at(now), "heading": state.course(now)}
        self._state = replace(state, **{**here, **changes, "since": now})

    # The link's side: the `Source` of `Uart`.

    def receive(self, t: int, data: bytes) -> Iterable[Output]:
        """The firmware sent `data` at `t`: the receiver hears it, and answers nothing."""
        return ()

    def poll(self, now: int) -> Iterable[Output]:
        """The epochs due at `now`, each at its own time; none while the receiver is silent."""
        with self._lock:
            outputs = []
            for t in self._due(now):
                # The receiver talks, it bounds every turn by its next epoch, so an epoch from
                # before the last change is one it skipped while it was silent.
                if self._state.talking and t >= self._state.since:
                    outputs.append((t, self.epoch(t)))
            self.sent += len(outputs)
            return outputs

    def quiet_until(self) -> int | None:
        """The time of the next epoch; `None` while the receiver is silent, which it can stop
        being only in a turn the test holds, before the link polls it."""
        with self._lock:
            return self._next if self._state.talking else None

    def latency(self) -> int | None:
        """`None`: the receiver answers nothing."""
        return None

    def _due(self, now: int) -> list[int]:
        if self._next is None:
            # The world's time is known once the run has started.
            offset = self._world().microsecond * 1000
            first = -(-(offset + FIRST_EPOCH.ns) // PERIOD.ns) * PERIOD.ns - offset
            self._next = first
        due = []
        while self._next <= now:
            due.append(self._next)
            self._next += PERIOD.ns
        return due

    def epoch(self, t: int) -> bytes:
        """The epoch at the board's time `t`: an RMC and a GGA, as the state has them then."""
        state = self._state
        when = self._world() + timedelta(microseconds=t // 1000)
        time = when.strftime("%H%M%S") + f".{when.microsecond // 10_000:02d}"
        date = when.strftime("%d%m%y")
        position = state.at(t)
        if not state.fixed or position is None:
            rmc = f"{TALKER}RMC,{time},V,,,,,,,{date},,,N"
            gga = f"{TALKER}GGA,{time},,,,,0,00,{NO_FIX_HDOP:.2f},,,,,,"
        else:
            where = f"{angle(position.lat, 2, 'N', 'S')},{angle(position.lon, 3, 'E', 'W')}"
            if state.speed.m_s == 0:
                motion = "0.00,"
            else:
                course = round(state.course(t).degrees, 1) % 360
                motion = f"{state.speed.knots:.2f},{course:.1f}"
            rmc = f"{TALKER}RMC,{time},A,{where},{motion},{date},,,A"
            gga = (
                f"{TALKER}GGA,{time},{where},1,{state.satellites:02d},{state.hdop:.2f},"
                "0.0,M,0.0,M,,"
            )
        return sentence(rmc) + sentence(gga)


def sentence(body: str) -> bytes:
    """`$`, `body`, `*`, the checksum, the XOR of `body`'s characters in two hex digits, and CR
    LF. A `ModelError` for one longer than NMEA 0183 allows."""
    checksum = 0
    for char in body.encode("ascii"):
        checksum ^= char
    line = f"${body}*{checksum:02X}\r\n".encode("ascii")
    if len(line) > SENTENCE_MAX:
        raise ModelError(f"{line!r} is longer than NMEA's {SENTENCE_MAX} characters")
    return line


def angle(degrees: float, digits: int, positive: str, negative: str) -> str:
    """`degrees` as NMEA's ddmm.mmmmm, or dddmm.mmmmm with `digits` 3, then its hemisphere's
    letter: minutes to five decimals, rounded half to even, a centimetre or two on the ground."""
    units = round(abs(degrees) * 60 * 100_000)
    whole, rest = divmod(units, 60 * 100_000)
    minutes, fraction = divmod(rest, 100_000)
    hemisphere = negative if degrees < 0 else positive
    return f"{whole:0{digits}d}{minutes:02d}.{fraction:05d},{hemisphere}"


def _hdop(value: float, what: str) -> float:
    hdop = number(value, f"{what}: hdop")
    if not 0 < hdop <= NO_FIX_HDOP:
        raise ModelError(f"{what}: hdop is {value!r}, not above 0 and up to {NO_FIX_HDOP}")
    return hdop


def _satellites(value: int, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{what}: satellites is {value!r}, not a whole number")
    if not 1 <= value <= 99:
        raise ModelError(f"{what}: satellites is {value}, not from 1 to 99: a fix has some")
    return value
