"""The condition the network's services hand `board.wait_until`: called, it returns the event or
`None`; its `repr` names the event, and it refuses to be read as a boolean."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True, repr=False)
class Condition(Generic[T]):
    """`service` names the protocol in the `TypeError` of `bool()`, as `MQTT`."""

    find: Callable[[], T]
    description: str
    service: str

    def __call__(self) -> T:
        return self.find()

    def __repr__(self) -> str:
        return self.description

    def __bool__(self) -> bool:
        raise TypeError(
            f"call the {self.service} condition with (), or pass it to board.wait_until"
        )
