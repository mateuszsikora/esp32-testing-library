"""Models of the devices at the far end of a route, each named after its device (`gnss.py`): they
speak the device's protocol and vocabulary, and know nothing of what the firmware does with them.

`Model` is the base of a model the test writes itself, whose times are `Duration`s: the far end of
a UART (`board.uart(role).model(...)`) and the source of an `SppPeer` take one as they take a
source whose times are nanoseconds, and `in_nanoseconds` is what they call in its place.
`ReactiveModel` is a `Model` that only answers, and `IdleFramedModel` one that also cuts what it
hears into frames at each silence, as Modbus RTU does."""

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from typing import Any

from esp32tl.units import Duration

# What a `Model` sends: the bytes, and the board's instant they go in at.
Timed = tuple[Duration, bytes]


class Model(ABC):
    """A model the test writes, whose times are `Duration`s on the board's clock: the far end of a
    UART, or what answers an `SppPeer`'s channel. A subclass defines all four methods, as a source
    in nanoseconds does: one that lacks one, or misspells it, cannot be made. `ReactiveModel` and
    `IdleFramedModel` define all but one. One with a `supply(on)` method is a part on the board's
    supply, as a source in nanoseconds with one is."""

    @abstractmethod
    def receive(self, at: Duration, data: bytes) -> Iterable[Timed]:
        """The firmware sent `data`, a piece of what it sends cut anywhere. On a UART, `at` is
        the instant it wrote the first byte of `data` into the TX FIFO, and each byte after came
        within one character time of the one before; esp32sim does not pace the transmission at
        the baud, so what the firmware writes in one go comes at about one instant. Behind an
        SPP channel, `at` is the instant `data` arrived. Returns the answers, each `latency()`
        or more after `at`."""

    @abstractmethod
    def poll(self, now: Duration) -> Iterable[Timed]:
        """What the model sends unprompted, asked at every turn; none before `now`."""

    @abstractmethod
    def quiet_until(self) -> Duration | None:
        """Before this instant the model sends nothing unprompted, later than the last `poll`'s
        `now`; `None` with nothing scheduled."""

    @abstractmethod
    def latency(self) -> Duration | None:
        """The shortest time in which the model answers; `None` when it answers nothing."""


class InNanoseconds:
    """`model`, a `Model`, as a source whose times are nanoseconds: what the route or the peer
    calls. It refuses what the model returns that is not of its kind with `fail(message)`, which
    names it `name`, as `uart.aux.receive returned 5, not a (Duration, bytes) pair`; what the
    model raises goes through as it is, for the caller to name."""

    def __init__(self, model: Model, name: str, fail: Callable[[str], Exception]) -> None:
        self.model = model
        self.name = name
        self.fail = fail

    def receive(self, t: int, data: bytes) -> list[tuple[int, bytes]]:
        return self._outputs("receive", self.model.receive(Duration(t), data))

    def poll(self, now: int) -> list[tuple[int, bytes]]:
        return self._outputs("poll", self.model.poll(Duration(now)))

    def quiet_until(self) -> int | None:
        bound = self.model.quiet_until()
        if bound is None:
            return None
        if not isinstance(bound, Duration):
            raise self.fail(f"{self.name}.quiet_until returned {bound!r}, not a Duration")
        return bound.ns

    def latency(self) -> int | None:
        bound = self.model.latency()
        if bound is None:
            return None
        if not isinstance(bound, Duration):
            raise self.fail(f"{self.name} declares a latency of {bound!r}, not a Duration")
        if bound.ns < 0:
            raise self.fail(f"{self.name} declares a latency of {bound}")
        return bound.ns

    def _outputs(self, method: str, values: Iterable[Timed]) -> list[tuple[int, bytes]]:
        outputs = []
        for value in values:
            if not (
                isinstance(value, tuple)
                and len(value) == 2
                and isinstance(value[0], Duration)
                and isinstance(value[1], bytes)
            ):
                raise self.fail(
                    f"{self.name}.{method} returned {value!r:.80}, not a (Duration, bytes) pair"
                )
            outputs.append((value[0].ns, value[1]))
        return outputs


class _Supplied(InNanoseconds):
    """`InNanoseconds` of a model with `supply(on)`, which the route and the peer find by it."""

    def supply(self, on: bool) -> None:
        self.model.supply(on)  # type: ignore[attr-defined]


def in_nanoseconds(model: Model, name: str, fail: Callable[[str], Exception]) -> InNanoseconds:
    """`model` as a source in nanoseconds, `Supplied` when it has a `supply` method."""
    kind = _Supplied if callable(getattr(model, "supply", None)) else InNanoseconds
    return kind(model, name, fail)


class ReactiveModel(Model):
    """A `Model` that only answers what it hears: a subclass defines `receive`, and sets
    `answer_after`, the shortest time it takes to answer, a `Duration`, as a class attribute or
    in `__init__`. `latency()` returns it, and the model sends nothing unprompted: `poll`
    returns nothing and `quiet_until` `None`. `answer_after = None` is a model that answers
    nothing."""

    answer_after: Duration | None

    def poll(self, now: Duration) -> Iterable[Timed]:
        return ()

    def quiet_until(self) -> Duration | None:
        return None

    def latency(self) -> Duration | None:
        answer_after: Duration | None = _attribute(
            self, "answer_after", "the shortest time it takes to answer, as ms(1)"
        )
        return answer_after


class IdleFramedModel(Model):
    """A `Model` of a part that frames what it hears by silence, as a Modbus RTU slave does:
    the bytes up to a silence of `gap` are one frame. A subclass sets `gap`, a `Duration`, as a
    class attribute or in `__init__` (3.5 character times for RTU: `ms(3.65)` at 9600 baud with
    10 bits a character, as 8N1; `ms(4.02)` with the 11 the Modbus specification asks for), and
    defines `on_frame(at, frame)`, called at the turn that comes once `gap` has passed since the
    frame's last piece came: `at` is that piece's `at` plus `gap`, or less than a CPU cycle
    later, the instant the silence showed the frame had ended, not the instant its data came.
    It returns the answers, each at `at` or later, and the model sends nothing else unprompted.
    The gap counts from that piece's `at`, as `receive` says: on a UART the instant the firmware
    wrote its first byte, and what the firmware writes in one go comes at about one instant;
    behind an SPP channel the instant the piece arrived. A piece that comes
    exactly `gap` after the one before continues the frame. `drop()` forgets the frame heard so
    far, as a part that loses its power does.

    While `on_frame` runs, and until the next frame ends, `frame_first_at` is the `at` of the
    frame's first piece and `frame_last_at` that of its last piece, as `receive` got them: on a
    UART, the instant the firmware wrote the piece's first byte into the TX FIFO; behind an SPP
    channel, the instant the piece arrived. Before the first frame the model has neither. The
    link gives a piece one instant, so neither says when a byte after a piece's first was
    written: `frame_last_at` is not the instant of the frame's last byte."""

    gap: Duration
    # Set before each `on_frame`; no class default, so that reading one before the first frame
    # is an AttributeError rather than an instant.
    frame_first_at: Duration
    frame_last_at: Duration
    # The frame heard so far, and the `at` of its first and last pieces; class defaults, so that
    # a subclass's `__init__` need not call this one's.
    _frame: bytes = b""
    _first: Duration | None = None
    _last: Duration | None = None

    @abstractmethod
    def on_frame(self, at: Duration, frame: bytes) -> Iterable[Timed]:
        """`frame` ended: `gap` has passed since its last piece came, and `at` is the instant the
        model saw it. `frame_first_at` and `frame_last_at` say when its first and last pieces
        came. Returns the answers, each at `at` or later."""

    def receive(self, at: Duration, data: bytes) -> Iterable[Timed]:
        if not self._frame:
            self._first = at
        self._frame += data
        self._last = at
        return ()

    def poll(self, now: Duration) -> Iterable[Timed]:
        end = self._end()
        if end is None or now < end:
            return ()
        # `_end` found a last piece; a first one is there unless a subclass set the frame itself.
        last = self._last
        assert last is not None
        self.frame_first_at = last if self._first is None else self._first
        self.frame_last_at = last
        frame, self._frame, self._first, self._last = self._frame, b"", None, None
        return self.on_frame(now, frame)

    def quiet_until(self) -> Duration | None:
        return self._end()

    def drop(self) -> None:
        """Forget the frame heard so far: `on_frame` never gets it, and the next piece starts
        a new one."""
        self._frame, self._first, self._last = b"", None, None

    def latency(self) -> Duration | None:
        # The turn that comes `gap` after the first piece is the one that ends the frame, if
        # nothing came after it; the answers are on_frame's, from poll.
        return self._gap()

    def _gap(self) -> Duration:
        gap: Duration = _attribute(self, "gap", "the silence that ends a frame, as ms(3.65)")
        return gap

    def _end(self) -> Duration | None:
        if not self._frame or self._last is None:
            return None
        return self._last + self._gap()


def _attribute(model: Model, name: str, what: str) -> Any:
    """`model.name`, or a `TypeError` that says what to set it to."""
    try:
        return getattr(model, name)
    except AttributeError:
        raise TypeError(f"{type(model).__name__} has no {name}: set it to {what}") from None
