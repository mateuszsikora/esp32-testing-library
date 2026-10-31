"""The console: the firmware's UART0, read on the board's clock.

The backend gathers what the guest prints from the link's `console` messages, which arrive in
emulated-time order with the turns. `expect` and `expect_exact` keep pytest-embedded's names, its
search from the end of the last match, its return values and `return_what_before_match`, but the
matching is esp32tl's and waits on the test's clock: `timeout` is in seconds of the board's time,
and the board runs only while they wait.
"""

import re
from collections.abc import Callable, Sequence
from typing import Any, Protocol, TypeVar, cast

import pexpect

from esp32tl.interfaces import RunEnded

# pytest-embedded's default, in seconds of the board's time here.
DEFAULT_TIMEOUT = 30

T = TypeVar("T")

# What expect takes, alone or in a list: a regular expression, as text, bytes or compiled, or
# pexpect.EOF, the end of the run.
Pattern = str | bytes | re.Pattern[str] | re.Pattern[bytes] | type[pexpect.EOF]


class Clocked(Protocol):
    """The board as the console needs it: what the guest has printed, and its clock."""

    console_output: bytearray

    def wait_on_clock(self, condition: Callable[[], T], within: float, description: str) -> T: ...


class Console:
    """The console of `board`. There is no `write`: the runner does not read input for UART0."""

    def __init__(self, board: Clocked) -> None:
        self._board = board
        # Where the last match ended: the next expect searches from there, as pexpect does.
        self._cursor = 0

    def expect(
        self,
        pattern: Pattern | Sequence[Pattern],
        timeout: float = DEFAULT_TIMEOUT,
        *,
        return_what_before_match: bool = False,
    ) -> Any:
        """Lets the board run until `pattern`, a regular expression as text, bytes or compiled,
        appears on the console after the last match, for at most `timeout` seconds of the
        board's time. A list matches whichever of its patterns appears first. Returns the
        `re.Match`, on bytes, or with `return_what_before_match` the bytes between the last
        match and this one. `pexpect.EOF` matches the end of the run, and returns what the
        console printed after the last match, without trailing whitespace.

        Raises `NeverHappened` when the pattern does not appear within `timeout`, and
        `RunEnded` when the run ends before it does."""
        return self._expect(pattern, timeout, return_what_before_match, exact=False)

    def expect_exact(
        self,
        pattern: str | bytes | type[pexpect.EOF] | Sequence[str | bytes | type[pexpect.EOF]],
        timeout: float = DEFAULT_TIMEOUT,
        *,
        return_what_before_match: bool = False,
    ) -> Any:
        """`expect`, with `pattern` taken as text to find as it is, not as a regular
        expression."""
        return self._expect(pattern, timeout, return_what_before_match, exact=True)

    def _expect(
        self,
        pattern: Pattern | Sequence[Pattern],
        timeout: float,
        return_what_before_match: bool,
        exact: bool,
    ) -> Any:
        patterns: list[Pattern] = (
            list(pattern) if isinstance(pattern, list | tuple) else [cast(Pattern, pattern)]
        )
        if not patterns:
            raise ValueError("expect: no pattern to wait for")
        regexes = [compiled(p, exact) for p in patterns if p is not pexpect.EOF]
        eof = len(regexes) < len(patterns)
        output = self._board.console_output
        # How much of the console the last look saw: a turn that brought nothing new is not
        # searched again.
        seen = -1

        def earliest(window: bytes | memoryview) -> re.Match[Any] | None:
            # The earliest match wins, the first pattern of those that start at the same byte.
            best = None
            for regex in regexes:
                match = regex.search(window)
                if match is not None and (best is None or match.start() < best.start()):
                    best = match
            return best

        def found() -> tuple[bytes, re.Match[bytes]] | None:
            nonlocal seen
            if len(output) == seen:
                return None
            seen = len(output)
            # From the last match, as pexpect's buffer starts there. Searched through a view, not
            # a copy, which the link may grow again once it is released; the window is copied
            # only for a match, which then refers to bytes of its own.
            with memoryview(output) as view, view[self._cursor :] as window:
                hit = earliest(window) is not None
            if not hit:
                return None
            copy = bytes(output[self._cursor :])
            match = earliest(copy)
            assert match is not None
            return copy, match

        description = " or ".join(shown(p) for p in patterns) + " on the console"
        try:
            result = self._board.wait_on_clock(found, timeout, description)
        except RunEnded:
            if not eof:
                raise
            rest = bytes(output[self._cursor :])
            self._cursor = len(output)
            return rest if return_what_before_match else rest.rstrip()
        # Something true is what wait_on_clock returns.
        assert result is not None
        window, match = result
        self._cursor += match.end()
        return window[: match.start()] if return_what_before_match else match


def compiled(pattern: Pattern, exact: bool) -> re.Pattern[bytes]:
    """`pattern` as a regular expression on bytes. Text and bytes compile with `re.DOTALL`, as
    pexpect compiles them, so that `.` matches across lines; text is encoded in UTF-8. A
    compiled pattern keeps its own flags, but `re.UNICODE`, which bytes do not take: pexpect drops
    them all from a compiled text pattern, and the test's `re.IGNORECASE` would be lost."""
    if isinstance(pattern, re.Pattern):
        if exact:
            raise TypeError("expect_exact takes text or bytes, not a compiled pattern")
        source = pattern.pattern
        if isinstance(source, str):
            return re.compile(source.encode(), pattern.flags & ~re.UNICODE)
        return cast(re.Pattern[bytes], pattern)
    if isinstance(pattern, str):
        pattern = pattern.encode()
    if not isinstance(pattern, bytes):
        raise TypeError(f"expect: {pattern!r} is not a pattern")
    return re.compile(re.escape(pattern) if exact else pattern, re.DOTALL)


def shown(pattern: Pattern) -> str:
    """How messages name `pattern`."""
    if pattern is pexpect.EOF:
        return "the end of the run"
    if isinstance(pattern, re.Pattern):
        pattern = pattern.pattern
    if isinstance(pattern, bytes):
        pattern = pattern.decode(errors="backslashreplace")
    return repr(pattern)
