"""A described condition for `board.wait_until`: called, it returns what it finds or `None`; its
`repr` is its description, which a wait names in `NeverHappened` and in the run report, with what
it still waits for when it says (`waiting`); and it refuses to be read as a boolean, so
`assert broker.next_message(topic)` cannot pass unasked."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True, repr=False)
class Condition(Generic[T]):
    """`find`, called with no arguments, is the condition; `description` names the event, as
    `a trip/end from the device`; `service`, when given, names the condition in the `TypeError`
    of `bool()` in place of the description, as `MQTT`; `waiting`, when given, is called when a
    wait on the condition times out, and returns why the event has not happened, in a clause, as
    `the SYN at 0.957 s got no answer (no SYN-ACK, no RST)`, or `None` for nothing to add."""

    find: Callable[[], T]
    description: str
    service: str | None = None
    waiting: Callable[[], str | None] | None = None

    def __post_init__(self) -> None:
        if not callable(self.find):
            raise TypeError(f"condition: find is {self.find!r}, not a function of no arguments")
        if not isinstance(self.description, str) or not self.description:
            raise TypeError(
                f"condition: description is {self.description!r}, not text naming the event"
            )
        if self.service is not None and (not isinstance(self.service, str) or not self.service):
            raise TypeError(f"condition: service is {self.service!r}, not text naming it")
        if self.waiting is not None and not callable(self.waiting):
            raise TypeError(
                f"condition: waiting is {self.waiting!r}, not a function of no arguments"
            )

    def __call__(self) -> T:
        return self.find()

    def __repr__(self) -> str:
        return self.description

    def __bool__(self) -> bool:
        named = (
            f"the {self.service} condition"
            if self.service
            else f"the condition {self.description!r}"
        )
        raise TypeError(f"call {named} with (), or pass it to board.wait_until")


def waiting(condition: object) -> str | None:
    """Why `condition`'s event has not happened, from its `waiting`, for a wait that timed out;
    `None` for a condition that does not say, or any other callable."""
    why = condition.waiting if isinstance(condition, Condition) else None
    return why() if why is not None else None


def condition(
    find: Callable[[], T],
    description: str,
    *,
    service: str | None = None,
    waiting: Callable[[], str | None] | None = None,
) -> Condition[T]:
    """A condition for `board.wait_until` that calls `find` and is named by `description`:
    `board.wait_until(condition(find, "a trip/end from the device"), within=minutes(3))` times out
    as `a trip/end from the device did not happen within 180 s`, followed by `: ` and what
    `waiting` returns, when given and not `None`, and `bool()` of it raises `TypeError`, before
    and after a match."""
    return Condition(find, description, service, waiting)


class FirstFrom(Generic[T]):
    """The `find` of a "next …" condition over an append-only history: called, the first record of
    `history()` from index `start` that `wanted` accepts, or `None` while there is none.

    A call looks only at the records appended since the last call ended, by index, without copying
    the history: a record it has looked at and refused stays refused, since records are only
    appended. Once it has found one it looks no further, and every later call returns the record at
    that index, read again from `history()`, so the condition consumes nothing and its boundary,
    `start`, never moves. `history` is called at each call: it may bring the history up to date
    first, or build it anew, as long as what it holds up to its old end does not change."""

    __slots__ = ("_found", "_history", "_scanned", "_wanted")

    def __init__(
        self, history: Callable[[], Sequence[T]], start: int, wanted: Callable[[T], bool]
    ) -> None:
        self._history = history
        self._wanted = wanted
        # How far the calls have looked, and the index of the match, once there is one.
        self._scanned = start
        self._found: int | None = None

    def __call__(self) -> T | None:
        history = self._history()
        if self._found is not None:
            return history[self._found]
        wanted = self._wanted
        for index in range(self._scanned, len(history)):
            record = history[index]
            if wanted(record):
                self._found = index
                return record
        self._scanned = max(self._scanned, len(history))
        return None
