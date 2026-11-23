"""Read-only views of an owner's append-only records."""

from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, nullcontext
from typing import TypeVar, overload

T = TypeVar("T")


class History(Sequence[T]):
    """A live sequence, optionally projecting a record at the index being read."""

    @overload
    def __init__(
        self, records: Sequence[T], *, lock: AbstractContextManager[object] | None = None
    ): ...

    @overload
    def __init__(
        self,
        records: Callable[[], int],
        at: Callable[[int], T],
        *,
        lock: AbstractContextManager[object] | None = None,
    ): ...

    def __init__(
        self,
        records: Sequence[T] | Callable[[], int],
        at: Callable[[int], T] | None = None,
        *,
        lock: AbstractContextManager[object] | None = None,
    ) -> None:
        if callable(records):
            assert at is not None
            self._length = records
            self._at = at
        else:
            self._length = records.__len__
            self._at = records.__getitem__
        self._lock = nullcontext() if lock is None else lock

    def __len__(self) -> int:
        with self._lock:
            return self._length()

    @overload
    def __getitem__(self, key: int) -> T: ...

    @overload
    def __getitem__(self, key: slice) -> list[T]: ...

    def __getitem__(self, key: int | slice) -> T | list[T]:
        with self._lock:
            indices = range(self._length())
            if isinstance(key, slice):
                return [self._at(i) for i in indices[key]]
            return self._at(indices[key])

    def __iter__(self) -> Iterator[T]:
        return iter(self[:])

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Sequence) or isinstance(other, (str, bytes)):
            return False
        return self[:] == list(other)

    def __repr__(self) -> str:
        return repr(self[:])
