"""A GNSS receiver at the far end of a UART, described by what it sees: where it is, how it moves,
how good its fix is (docs/features/models-user.md).

The receiver sends one epoch a second, at every whole second of the world's time from the first
one at least `FIRST_EPOCH` after power-on: an RMC and then a GGA, NMEA 0183 with talker `GP`, with
the same UTC time. What the test says holds from the board's current instant until it says
something else, as the world does: `move()` takes no duration, and the receiver goes on moving
while the test waits. A move follows the great circle it starts on. Until the test gives it a fix,
the receiver talks but has none, as one does after a cold start. Of what the firmware sends it,
it hears CASIC binary frames, and answers each with ACK-ACK or ACK-NAK, as an L76K does; it acts on
none of them, and drops the rest.
"""

import math
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Literal

from esp32tl.interfaces.history import History
from esp32tl.link.boardlink import Output
from esp32tl.units import (
    EARTH_RADIUS_M,
    NORTH,
    NS_PER_S,
    Distance,
    Duration,
    Heading,
    Position,
    Speed,
    m_s,
    ms,
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
# The HDOP a receiver reports without a fix, as u-blox receivers do; and the lowest one the GGA's
# two decimals can say.
NO_FIX_HDOP = 99.99
MIN_HDOP = 0.01

# NMEA 0183's longest sentence, from `$` to the line ending included.
SENTENCE_MAX = 82

STILL = m_s(0)

# CASIC binary frames (ZKW's CASIC protocol specification, 2.2; Quectel's L76K GNSS protocol
# specification, 3.1): the header, the payload's length in 2 little-endian bytes, the class, the
# id, the payload and a 4-byte checksum.
CASIC_HEADER = b"\xba\xce"
CASIC_HEAD = 6
CASIC_CHECKSUM = 4
# The longest payload the receiver takes, "<2k bytes" in ZKW's specification: a header that says
# more is noise.
CASIC_MAX_PAYLOAD = 2047
ACK = 0x05
ACK_ACK = 0x01
ACK_NAK = 0x00
# How long after the frame's last byte the receiver starts its answer, unless the test says
# otherwise: 1.53 ms on an L76K at 9600 baud (tools/casic_probe, docs/stage0-findings.md).
ACK_AFTER = ms(1.5)

CasicAnswer = Literal["ack", "nak"]


@dataclass(frozen=True)
class CasicFrame:
    """A CASIC frame the receiver heard, stamped at the instant its last byte reached it: its
    class, its id, its payload and how the receiver chose to answer it, `"ack"` (ACK-ACK), `"nak"`
    (ACK-NAK) or `None`, no answer."""

    at: Duration
    cls: int
    id: int
    payload: bytes
    answer: CasicAnswer | None


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
    world's time at power-on, which the sentences are dated from, asked once the run has started;
    `baud` the route's rate, which the firmware's bytes take on the line to reach the receiver.
    The methods besides the test's are those of `Source`, for the link.
    """

    def __init__(self, now: Callable[[], int], world: Callable[[], datetime], *, baud: int) -> None:
        self._now = now
        self._world = world
        self._lock = threading.Lock()
        self._state = State(0, None, NORTH, STILL, False, NO_FIX_HDOP, 0, True)
        # The time of the next epoch, once the run has started.
        self._next: int | None = None
        # Whether the board's supply, which the receiver is on, is on.
        self._supplied = True
        # How many epochs the link has been given.
        self.sent = 0
        # What the receiver heard of a CASIC frame it has not finished hearing, the classes and
        # ids it refuses, how long it takes to answer, the frames it heard, those whose last byte
        # is still on the line, and the answers it has not sent yet, each at its time.
        self._baud = baud
        self._heard = bytearray()
        self._refused: set[tuple[int, int]] = set()
        self._answer_after = ACK_AFTER.ns
        self._frames: list[CasicFrame] = []
        self._arriving: list[CasicFrame] = []
        self._answers: list[Output] = []
        # When the last byte the firmware wrote to the receiver leaves the line.
        self._line_free = 0

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
        """Nothing sent from now on, until `resume`: no sentences, and no answer, not even one
        still due to a frame heard before; a frame still arriving is heard silent."""
        with self._lock:
            now = self._now()
            self._answers.clear()
            self._arriving = [
                frame if frame.at.ns <= now else replace(frame, answer=None)
                for frame in self._arriving
            ]
            self._change_locked(talking=False)

    def resume(self) -> None:
        """Sentences again, from the epoch at this instant on, if one falls here. The epochs of
        the silence are not sent: a silent receiver bounds no turn, so the link may not have
        polled it since they fell."""
        with self._lock:
            now = self._now()
            if not self._state.talking and self._next is not None:
                while self._next < now:
                    self._next += PERIOD.ns
            self._change_locked(talking=True)

    def supply(self, on: bool) -> None:
        """The board's supply went off, or came back. Without it the receiver is silent, as
        after `go_silent`; with it back it sends again from the epoch at this instant on, with
        the fix and the motion the test gave it, and no time to a first fix."""
        with self._lock:
            now = self._now()
            if on and not self._supplied and self._next is not None:
                while self._next < now:
                    self._next += PERIOD.ns
            if not on:
                # What it heard of a frame, a frame still arriving, and the answers it had not
                # sent, go with its power.
                self._hear(now)
                self._heard.clear()
                self._arriving.clear()
                self._answers.clear()
                self._line_free = 0
            self._supplied = on

    def refuse(self, cls: int, id: int) -> None:
        """ACK-NAK, from now on, for each CASIC frame of class `cls` and id `id`."""
        key = (_byte(cls, "gnss.refuse: cls"), _byte(id, "gnss.refuse: id"))
        with self._lock:
            self._refused.add(key)

    def answer_after(self, duration: Duration) -> None:
        """The answer to each CASIC frame from now on comes `duration` after its last byte."""
        if not isinstance(duration, Duration):
            raise TypeError(f"gnss.answer_after: {duration!r} is not a Duration, as ms(1.5)")
        if duration.ns < 0:
            raise ModelError(f"gnss.answer_after: {duration} is less than 0")
        with self._lock:
            self._answer_after = duration.ns

    @property
    def casic_frames(self) -> Sequence[CasicFrame]:
        """Each CASIC frame the receiver heard, with its answer, in the order it heard them: once
        its last byte has arrived, from the first turn at or after that, before the test's hold
        (`arrive`)."""
        return History(self._frames, lock=self._lock)

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
        """The firmware wrote `data` at `t`, which reaches the receiver a byte every character
        time at the route's baud, after what it wrote before: each CASIC frame it completes is
        heard when its last byte has arrived, and answered `answer_after` later, from `poll`.
        Without power the receiver hears nothing; silent, it hears the frames and answers none."""
        with self._lock:
            if not self._supplied:
                return ()
            start = max(t, self._line_free)
            self._line_free = start + _char_ns(len(data), self._baud)
            before = len(self._heard)
            self._heard += data
            for cls, id, payload, valid, end in self._frames_heard():
                at = start + _char_ns(end - before, self._baud)
                answer: CasicAnswer | None = None
                if self._state.talking:
                    answer = "nak" if not valid or (cls, id) in self._refused else "ack"
                    reply = ACK_ACK if answer == "ack" else ACK_NAK
                    frame = casic(ACK, reply, bytes((cls, id, 0, 0)))
                    self._answers.append((at + self._answer_after, frame))
                self._arriving.append(CasicFrame(Duration(at), cls, id, payload, answer))
            return ()

    def arrive(self, now: int) -> None:
        """The frames whose last byte has arrived by `now` go into `casic_frames`, at the start
        of the turn at `now`, before the test's hold, so that the test holding it sees them."""
        with self._lock:
            self._hear(now)

    def _hear(self, now: int) -> None:
        """The frames whose last byte has arrived by `now` go into `casic_frames`."""
        while self._arriving and self._arriving[0].at.ns <= now:
            self._frames.append(self._arriving.pop(0))

    def _frames_heard(self) -> list[tuple[int, int, bytes, bool, int]]:
        """The CASIC frames complete in what the receiver heard, each with whether its checksum
        holds and where it ends, counted in bytes from the start of `_heard` before this call,
        taken out of it; what lies before a frame's header is dropped, and so is a header whose
        length is more than `CASIC_MAX_PAYLOAD`, up to the next `0xBA`."""
        heard = self._heard
        frames: list[tuple[int, int, bytes, bool, int]] = []
        dropped = 0
        while True:
            start = heard.find(CASIC_HEADER)
            if start < 0:
                # A last 0xBA may be the start of a header.
                del heard[: len(heard) - 1 if heard.endswith(CASIC_HEADER[:1]) else len(heard)]
                return frames
            del heard[:start]
            dropped += start
            if len(heard) < CASIC_HEAD:
                return frames
            length = int.from_bytes(heard[2:4], "little")
            if length > CASIC_MAX_PAYLOAD:
                del heard[:1]
                dropped += 1
                continue
            end = CASIC_HEAD + length + CASIC_CHECKSUM
            if len(heard) < end:
                return frames
            cls, id = heard[4], heard[5]
            payload = bytes(heard[CASIC_HEAD : CASIC_HEAD + length])
            checksum = int.from_bytes(heard[end - CASIC_CHECKSUM : end], "little")
            valid = checksum == casic_checksum(cls, id, payload)
            frames.append((cls, id, payload, valid, dropped + end))
            del heard[:end]
            dropped += end

    def poll(self, now: int) -> Iterable[Output]:
        """The epochs and the answers due at `now`, each at its own time; none while the
        receiver is silent or without power."""
        with self._lock:
            outputs = []
            for t in self._due(now):
                if self._state.talking and self._supplied:
                    outputs.append((t, self.epoch(t)))
            self.sent += len(outputs)
            self._hear(now)
            due = [answer for answer in self._answers if answer[0] <= now]
            self._answers = [answer for answer in self._answers if answer[0] > now]
            return sorted(outputs + due, key=lambda output: output[0])

    def quiet_until(self) -> int | None:
        """The time of the next epoch or answer, whichever comes first; `None` while the receiver
        is silent or without power, which it can stop being only in a turn the test holds, before
        the link polls it, and `resume` and `supply` move the next epoch past the silence."""
        with self._lock:
            if not (self._state.talking and self._supplied):
                return None
            due = [answer[0] for answer in self._answers]
            due += [frame.at.ns for frame in self._arriving]
            if self._next is not None:
                due.append(self._next)
            return min(due, default=None)

    def latency(self) -> int | None:
        """`answer_after`, how soon the receiver answers a CASIC frame; `None` while it is silent
        or without power, when it answers none."""
        with self._lock:
            return self._answer_after if self._state.talking and self._supplied else None

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


def _char_ns(count: int, baud: int) -> int:
    """How long `count` characters take on the line at `baud`, 10 bits each, as 8N1, rounded
    down as the runner rounds them."""
    return count * 10 * NS_PER_S // baud


def casic_checksum(cls: int, id: int, payload: bytes) -> int:
    """A CASIC frame's checksum, as the specifications compute it: `(id << 24) + (cls << 16) +
    len(payload)`, plus each of the `len(payload) // 4` little-endian 32-bit words of the
    payload, modulo 2³². A payload is a multiple of 4 bytes; the bytes past the last whole word
    of one that is not count only in its length."""
    checksum = (id << 24) + (cls << 16) + len(payload)
    for i in range(0, len(payload) - 3, 4):
        checksum += int.from_bytes(payload[i : i + 4], "little")
    return checksum & 0xFFFF_FFFF


def casic(cls: int, id: int, payload: bytes) -> bytes:
    """The CASIC frame of class `cls` and id `id` carrying `payload`, with its checksum."""
    return (
        CASIC_HEADER
        + len(payload).to_bytes(2, "little")
        + bytes((cls, id))
        + payload
        + casic_checksum(cls, id, payload).to_bytes(4, "little")
    )


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
    if not MIN_HDOP <= hdop <= NO_FIX_HDOP:
        raise ModelError(f"{what}: hdop is {value!r}, not from {MIN_HDOP} to {NO_FIX_HDOP}")
    return hdop


def _byte(value: int, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{what} is {value!r}, not a whole number")
    if not 0 <= value <= 0xFF:
        raise ModelError(f"{what} is {value}, not from 0 to 255")
    return value


def _satellites(value: int, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{what}: satellites is {value!r}, not a whole number")
    if not 1 <= value <= 99:
        raise ModelError(f"{what}: satellites is {value}, not from 1 to 99: a fix has some")
    return value
