"""A described condition for `board.wait_until`: called, it returns what it finds or `None`; its
`repr` is its description, which a wait names in `NeverHappened` and in the run report; and it
refuses to be read as a boolean, so `assert broker.next_message(topic)` cannot pass unasked."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True, repr=False)
class Condition(Generic[T]):
    """`find`, called with no arguments, is the condition; `description` names the event, as
    `a trip/end from the device`; `service`, when given, names the condition in the `TypeError`
    of `bool()` in place of the description, as `MQTT`."""

    find: Callable[[], T]
    description: str
    service: str | None = None

    def __post_init__(self) -> None:
        if not callable(self.find):
            raise TypeError(f"condition: find is {self.find!r}, not a function of no arguments")
        if not isinstance(self.description, str) or not self.description:
            raise TypeError(
                f"condition: description is {self.description!r}, not text naming the event"
            )
        if self.service is not None and (not isinstance(self.service, str) or not self.service):
            raise TypeError(f"condition: service is {self.service!r}, not text naming it")

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


def condition(
    find: Callable[[], T], description: str, *, service: str | None = None
) -> Condition[T]:
    """A condition for `board.wait_until` that calls `find` and is named by `description`:
    `board.wait_until(condition(find, "a trip/end from the device"), within=minutes(3))` times out
    as `a trip/end from the device did not happen within 180 s`, and `bool()` of it raises
    `TypeError`, before and after a match."""
    return Condition(find, description, service)
