"""Models of the devices at the far end of a route, each named after its device (`gnss.py`): they
speak the device's protocol and vocabulary, and know nothing of what the firmware does with them.

`Model` is the base of a model the test writes itself, whose times are `Duration`s: the far end of
a UART (`board.uart(role).model(...)`) and the source of an `SppPeer` take one as they take a
source whose times are nanoseconds, and `in_nanoseconds` is what they call in its place.
`ReactiveModel` is a `Model` that only answers, and `IdleFramedModel` one that also cuts what it
hears into frames at each silence, as Modbus RTU does."""

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from typing import Any, cast

from esp32tl.link.boardlink import is_baud
from esp32tl.units import Duration

# What a `Model` sends: the bytes, and the board's instant they go in at; on a UART, at the
# route's `baud`.
Timed = tuple[Duration, bytes]
# The same at a line rate of its own, in baud, a positive int: on a UART only, where it goes in at
# that rate. Each output has its own, so that a model answers a rate command at the old rate and
# sends what comes after at the new one.
TimedAtRate = tuple[Duration, bytes, int]


class Model(ABC):
    """A model the test writes, whose times are `Duration`s on the board's clock: the far end of a
    UART, or what answers an `SppPeer`'s channel. A subclass defines all four methods, as a source
    in nanoseconds does: one that lacks one, or misspells it, cannot be made. `ReactiveModel` and
    `IdleFramedModel` define all but one. One with a `supply(on)` method is a part on the board's
    supply, as a source in nanoseconds with one is.

    On a UART, an output may be a `TimedAtRate`, which goes in at its own rate, and the model may
    define, each found by its name as `supply` is: `transmitted(at, baud)`, the rate of the
    transmission the next `receive` gets, `None` with none set; `configured(at, baud, transmits,
    receives)`, each change of the firmware's routes on the wire; and `frame_errors(at, count,
    baud)`, which takes the frame errors its bytes met on the chip's RX pin, so that they do not
    fail the run."""

    @abstractmethod
    def receive(self, at: Duration, data: bytes) -> Iterable[Timed | TimedAtRate]:
        """The firmware sent `data`, a piece of what it sends cut anywhere. On a UART, `at` is
        the instant it wrote the first byte of `data` into the TX FIFO, and each byte after came
        within one character time of the one before; esp32sim does not pace the transmission at
        the baud, so what the firmware writes in one go comes at about one instant. Behind an
        SPP channel, `at` is the instant `data` arrived. Returns the answers, each `latency()`
        or more after `at`."""

    @abstractmethod
    def poll(self, now: Duration) -> Iterable[Timed | TimedAtRate]:
        """What the model sends unprompted, asked at every turn; none before `now`."""

    @abstractmethod
    def quiet_until(self) -> Duration | None:
        """Before this instant the model sends nothing unprompted, later than the last `poll`'s
        `now`; `None` with nothing scheduled."""

    @abstractmethod
    def latency(self) -> Duration | None:
        """The shortest time in which the model answers; `None` when it answers nothing."""


# What `InNanoseconds` returns: a `Timed` or a `TimedAtRate` in nanoseconds.
Output = tuple[int, bytes] | tuple[int, bytes, int]


class InNanoseconds:
    """`model`, a `Model`, as a source whose times are nanoseconds: what the route or the peer
    calls. It refuses what the model returns that is not of its kind with `fail(message)`, which
    names it `name`, as `uart.aux.receive returned 5, not a (Duration, bytes) pair`; what the
    model raises goes through as it is, for the caller to name. With `rates`, on a UART, an output
    may be a `TimedAtRate` too. `in_nanoseconds` makes one with the model's optional methods."""

    def __init__(
        self, model: Model, name: str, fail: Callable[[str], Exception], *, rates: bool = False
    ) -> None:
        self.model = model
        self.name = name
        self.fail = fail
        self.rates = rates

    def receive(self, t: int, data: bytes) -> list[Output]:
        return self._outputs("receive", self.model.receive(Duration(t), data))

    def poll(self, now: int) -> list[Output]:
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

    def _outputs(self, method: str, values: Iterable[Timed | TimedAtRate]) -> list[Output]:
        outputs: list[Output] = []
        for value in values:
            if not (
                isinstance(value, tuple)
                and len(value) in ((2, 3) if self.rates else (2,))
                and isinstance(value[0], Duration)
                and isinstance(value[1], bytes)
                and (len(value) == 2 or is_baud(value[2]))
            ):
                kinds = (
                    "a (Duration, bytes) pair or a (Duration, bytes, baud) triple"
                    if self.rates
                    else "a (Duration, bytes) pair"
                )
                raise self.fail(f"{self.name}.{method} returned {value!r:.80}, not {kinds}")
            if len(value) == 3:
                outputs.append((value[0].ns, value[1], value[2]))
            else:
                outputs.append((value[0].ns, value[1]))
        return outputs


# The optional methods of a `Model`, each forwarded by an adapter class of its own, with its times
# as `Duration`s: the route and the peer find them on the adapter by `isinstance`, so that an
# adapter has a method only when its model has it.


class _Supply(InNanoseconds):
    def supply(self, on: bool) -> None:
        self.model.supply(on)  # type: ignore[attr-defined]


class _Transmitted(InNanoseconds):
    def transmitted(self, t: int, baud: int | None) -> None:
        self.model.transmitted(Duration(t), baud)  # type: ignore[attr-defined]


class _Configured(InNanoseconds):
    def configured(self, t: int, baud: int | None, transmits: bool, receives: bool) -> None:
        self.model.configured(Duration(t), baud, transmits, receives)  # type: ignore[attr-defined]


class _FrameErrors(InNanoseconds):
    def frame_errors(self, t: int, count: int, baud: int | None) -> None:
        self.model.frame_errors(Duration(t), count, baud)  # type: ignore[attr-defined]


OPTIONAL_METHODS: dict[str, type[InNanoseconds]] = {
    "supply": _Supply,
    "transmitted": _Transmitted,
    "configured": _Configured,
    "frame_errors": _FrameErrors,
}

# The adapter class of each set of optional methods, made once.
_KINDS: dict[tuple[str, ...], type[InNanoseconds]] = {(): InNanoseconds}


def in_nanoseconds(
    model: Model, name: str, fail: Callable[[str], Exception], *, rates: bool = False
) -> InNanoseconds:
    """`model` as a source in nanoseconds, with each of the `OPTIONAL_METHODS` the model has, as
    a callable attribute: `Supplied` when it has a `supply` method, and so on. `rates` lets an
    output give its own line rate, on a UART."""
    methods = tuple(m for m in OPTIONAL_METHODS if callable(getattr(model, m, None)))
    kind = _KINDS.get(methods)
    if kind is None:
        bases = tuple(OPTIONAL_METHODS[m] for m in methods)
        kind = _KINDS[methods] = cast(
            type[InNanoseconds],
            type(f"InNanoseconds[{','.join(methods)}]", bases, {}),
        )
    return kind(model, name, fail, rates=rates)


class ReactiveModel(Model):
    """A `Model` that only answers what it hears: a subclass defines `receive`, and sets
    `answer_after`, the shortest time it takes to answer, a `Duration`, as a class attribute or
    in `__init__`. `latency()` returns it, and the model sends nothing unprompted: `poll`
    returns nothing and `quiet_until` `None`. `answer_after = None` is a model that answers
    nothing."""

    answer_after: Duration | None

    def poll(self, now: Duration) -> Iterable[Timed | TimedAtRate]:
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
    def on_frame(self, at: Duration, frame: bytes) -> Iterable[Timed | TimedAtRate]:
        """`frame` ended: `gap` has passed since its last piece came, and `at` is the instant the
        model saw it. `frame_first_at` and `frame_last_at` say when its first and last pieces
        came. Returns the answers, each at `at` or later."""

    def receive(self, at: Duration, data: bytes) -> Iterable[Timed | TimedAtRate]:
        if not self._frame:
            self._first = at
        self._frame += data
        self._last = at
        return ()

    def poll(self, now: Duration) -> Iterable[Timed | TimedAtRate]:
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
