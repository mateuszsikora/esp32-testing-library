"""The test's code that a service of the network calls while the board advances, as the broker's
`on_publish` handler or its `connection_policy`, or the HTTP server's handler: it runs in the
link's thread, at the board's instant of what it answers, and must not wait on the board, whose
run is what called it. `handling` marks where one runs, and `refuse_wait` is what a wait on the
board asks first; `refuse_board` is what the board's other calls that reach the runner ask, its
flash and its power, which the link's thread cannot carry while it runs a handler, and the
console's writes, the access point's switches and the lines' drives, which it would stamp with a
turn the run is past. An exception
of a handler, `pytest.fail`'s included, goes to the network's `test_error`: in a board test the
waiting test raises it at the next turn of the link, and without a board `reraise` raises it
where the service was called. `Registration` is what registering a handler returns.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

# The handler that runs now, in this thread, by what names it in an error and what kind of code
# of the test's it is, `handler` or `policy`; `None` outside one.
_running: ContextVar[tuple[str, str] | None] = ContextVar("esp32tl_handler", default=None)


@contextmanager
def handling(name: str, kind: str = "handler") -> Iterator[None]:
    """Runs the block as the handler `name`, as `the broker's on_publish handler for 'dev/+/req'`;
    `kind` names it in the rule an error states, as `a policy must not wait`."""
    token = _running.set((name, kind))
    try:
        yield
    finally:
        _running.reset(token)


def refuse_wait(call: str) -> None:
    """Raises `RuntimeError` when `call`, a wait on the board as `board.wait_until`, comes from a
    handler: the link that would run the board is the thread that waits."""
    running = _running.get()
    if running is not None:
        name, kind = running
        raise RuntimeError(f"{call}: called from {name}: a {kind} must not wait")


def refuse_board(call: str) -> None:
    """Raises `RuntimeError` when `call`, a call on the board that reaches the runner, as
    `board.flash` or `board.reset`, comes from a handler: the link's thread, which would carry it,
    is running the handler, between two turns."""
    running = _running.get()
    if running is not None:
        name, kind = running
        raise RuntimeError(f"{call}: called from {name}: a {kind} must not call the board")


def reraise(error: BaseException) -> None:
    """The `test_error` of a network without a board: the error goes up from the call that ran
    the handler, as `on_data`."""
    raise error


class Registration:
    """What registering a handler returns. `remove()` takes the handler away: it is called for
    nothing more, even for a publication whose earlier handlers are running. Removing it again
    does nothing."""

    def __init__(self, name: str, removed: Callable[["Registration"], None]) -> None:
        self._name = name
        self._removed = removed
        self._active = True

    @property
    def name(self) -> str:
        """What the transcript calls the handler, as `on_publish 'dev/+/req'`."""
        return self._name

    @property
    def active(self) -> bool:
        """Whether the handler is still registered."""
        return self._active

    def remove(self) -> None:
        if self._active:
            self._active = False
            self._removed(self)

    def __repr__(self) -> str:
        return f"<{self._name}{'' if self._active else ', removed'}>"
