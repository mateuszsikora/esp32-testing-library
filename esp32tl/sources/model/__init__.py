"""Models of the devices at the far end of a route, each named after its device (`gnss.py`): they
speak the device's protocol and vocabulary, and know nothing of what the firmware does with them.

`Model` is the base of a model the test writes itself, whose times are `Duration`s: the far end of
a UART (`board.uart(role).model(...)`) and the source of an `SppPeer` take one as they take a
source whose times are nanoseconds, and `in_nanoseconds` is what they call in its place."""

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable

from esp32tl.units import Duration

# What a `Model` sends: the bytes, and the board's instant they go in at.
Timed = tuple[Duration, bytes]


class Model(ABC):
    """A model the test writes, whose times are `Duration`s on the board's clock: the far end of a
    UART, or what answers an `SppPeer`'s channel. A subclass defines all four methods, as a source
    in nanoseconds does: one that lacks one, or misspells it, cannot be made. One with a
    `supply(on)` method is a part on the board's supply, as a source in nanoseconds with one is."""

    @abstractmethod
    def receive(self, at: Duration, data: bytes) -> Iterable[Timed]:
        """`data` crossed to the model at `at`. Returns the answers, each `latency()` or more
        after `at`."""

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
