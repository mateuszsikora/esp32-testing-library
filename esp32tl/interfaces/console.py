"""The console: the firmware's UART0, or its USB-Serial/JTAG controller when the board profile's
`console` says so, read on the board's clock.

The backend gathers what the guest prints from the link's `console` messages, which arrive in
emulated-time order with the turns; serial stamps its read batches on wall time. `expect` and
`expect_exact` keep pytest-embedded's names, its search from the end of the last match, its return
values and `return_what_before_match`. The matching is esp32tl's and waits on the test's clock:
`timeout` is a `Duration` of the board's time, or a number of seconds of it. On esp32sim the
board runs only while they wait. `line` and
`lines` read the console as lines, each stamped with the board's time it ended at: `line(pattern)`
is a condition for `board.wait_until`, `lines()` what has been printed.
"""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TypeVar, cast, runtime_checkable

import pexpect

from esp32tl.interfaces import RunEnded, SourceError
from esp32tl.interfaces.uart import Uart
from esp32tl.link.boardlink import Output
from esp32tl.tap import Tap
from esp32tl.units import Duration, number, seconds

# pytest-embedded's default, in seconds of the board's time here.
DEFAULT_TIMEOUT = 30

# The colour codes of ESP-IDF's log, which a line's text leaves out.
COLOUR = re.compile(r"\x1b\[[0-9;]*m")

T = TypeVar("T")

# What expect takes, alone or in a list: a regular expression, as text, bytes or compiled, or
# pexpect.EOF, the end of the run.
Pattern = str | bytes | re.Pattern[str] | re.Pattern[bytes] | type[pexpect.EOF]


@dataclass(frozen=True)
class ConsoleLine:
    """A line the firmware printed on its console: the board's time its line ending came at, its
    text without the line ending and without colour codes, and the match of the pattern that
    found it, if one did. `line["lat"]` and `line[1]` are the match's groups."""

    at: Duration
    text: str
    # Matches compare by identity: two lines are equal by their time and text.
    match: re.Match[str] | None = field(default=None, compare=False)

    def __getitem__(self, group: int | str) -> Any:
        if self.match is None:
            raise TypeError(f"{self.text!r} was found by no pattern, and has no groups")
        return self.match[group]


class ConsoleLines:
    """What the guest printed, cut into `ConsoleLine`s as the link brings it: `feed` is called
    with the board's time and the bytes of each `console` message."""

    def __init__(self) -> None:
        self.lines: list[ConsoleLine] = []
        self._partial = bytearray()

    def feed(self, t: int, data: bytes) -> None:
        for piece in data.splitlines(keepends=True):
            self._partial += piece
            if piece.endswith(b"\n"):
                text = self._partial.decode(errors="replace").rstrip("\r\n")
                self.lines.append(ConsoleLine(Duration(t), COLOUR.sub("", text)))
                self._partial.clear()


class LineCondition:
    """`console.line(pattern)`: the first line that ends after it was made and matches
    `pattern`, or `None` while there is none. Once it has found one it looks after it, so the
    same condition asked again finds the next."""

    def __init__(self, lines: ConsoleLines, pattern: str | re.Pattern[str]) -> None:
        self._lines = lines
        self._pattern = line_pattern(pattern)
        self._start = len(lines.lines)

    def __call__(self) -> ConsoleLine | None:
        lines = self._lines.lines
        for index in range(self._start, len(lines)):
            line = lines[index]
            match = self._pattern.search(line.text)
            if match is not None:
                self._start = index + 1
                return ConsoleLine(line.at, line.text, match)
        # The next look starts after the lines this one saw.
        self._start = len(self._lines.lines)
        return None

    def __repr__(self) -> str:
        return f"a console line matching {self._pattern.pattern!r}"


class Clocked(Protocol):
    """The board as the console needs it: what the guest has printed, as bytes and as lines, and
    its clock."""

    @property
    def console_output(self) -> bytearray: ...

    @property
    def console_lines(self) -> ConsoleLines: ...

    def wait_on_clock(
        self, condition: Callable[[], T | None], within: Duration, description: str
    ) -> T: ...


@runtime_checkable
class Tappable(Protocol):
    def tap_console(self, path: str | Path) -> Tap: ...


class ConsoleInput(Uart):
    """The console's input on the link: UART0 RX on the wire's RX pin, as `uart_rx`, or, with
    `usb` and no wire, the USB-Serial/JTAG controller's, as `console_rx`. Output arrives
    separately in console messages."""

    def __init__(self, *args: Any, usb: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._usb = usb
        self._pending: list[Output] = []

    @property
    def usb_console(self) -> bool:
        return self._usb

    def write(self, t: int, data: bytes) -> None:
        if data:
            self._pending.append((t, data))
            self._crossed(t, "in", data)

    def poll(self, now: int) -> list[Output]:
        pending, self._pending = self._pending, []
        return pending


@runtime_checkable
class Writable(Protocol):
    def write_console(self, data: bytes) -> None: ...


class Console:
    """The firmware's console, UART0 or USB-Serial/JTAG, read and written on the board's clock."""

    def __init__(self, board: Clocked) -> None:
        self._board = board
        # Where the last match ended: the next expect searches from there, as pexpect does.
        self._cursor = 0

    def write(self, data: bytes | str) -> None:
        """Writes bytes (text encoded as UTF-8) at the board's current instant."""
        if isinstance(data, str):
            data = data.encode()
        if not isinstance(data, bytes):
            raise TypeError("console.write takes bytes or str")
        if not isinstance(self._board, Writable):
            raise SourceError("this board cannot write its console")
        self._board.write_console(data)

    def sendline(self, line: bytes | str) -> None:
        """Writes a line followed by LF; text is encoded as UTF-8."""
        if isinstance(line, str):
            line = line.encode()
        if not isinstance(line, bytes):
            raise TypeError("console.sendline takes bytes or str")
        self.write(line + b"\n")

    def tap(self, path: str | Path) -> Tap:
        """Records guest output and test input from power-on, on the board's clock."""
        if not isinstance(self._board, Tappable):
            raise SourceError("this board cannot tap its console")
        return self._board.tap_console(path)

    def expect(
        self,
        pattern: Pattern | Sequence[Pattern],
        timeout: Duration | float = DEFAULT_TIMEOUT,
        *,
        return_what_before_match: bool = False,
    ) -> Any:
        """Lets the board run until `pattern`, a regular expression as text, bytes or compiled,
        appears on the console after the last match, for at most `timeout` of the board's time, a
        `Duration` or a number of seconds. A list matches whichever of its patterns appears
        first. Returns the `re.Match`, on bytes, or with `return_what_before_match` the bytes
        between the last match and this one. `pexpect.EOF` matches the end of the run, and
        returns what the console printed after the last match, without trailing whitespace.

        Raises `NeverHappened` when the pattern does not appear within `timeout`, and
        `RunEnded` when the run ends before it does."""
        return self._expect(pattern, timeout, return_what_before_match, exact=False)

    def expect_exact(
        self,
        pattern: str | bytes | type[pexpect.EOF] | Sequence[str | bytes | type[pexpect.EOF]],
        timeout: Duration | float = DEFAULT_TIMEOUT,
        *,
        return_what_before_match: bool = False,
    ) -> Any:
        """`expect`, with `pattern` taken as text to find as it is, not as a regular
        expression."""
        return self._expect(pattern, timeout, return_what_before_match, exact=True)

    def _expect(
        self,
        pattern: Pattern | Sequence[Pattern],
        timeout: Duration | float,
        return_what_before_match: bool,
        exact: bool,
    ) -> Any:
        within = timeout_of(timeout)
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
            result = self._board.wait_on_clock(found, within, description)
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

    def line(self, pattern: str | re.Pattern[str]) -> LineCondition:
        """A condition for `board.wait_until`: the first line that ends from now on and matches
        `pattern`, a regular expression on the line's text, as a string or compiled. It returns
        the `ConsoleLine`, with the match, or `None` while there is none. Independent of `expect`'s
        search."""
        return LineCondition(self._board.console_lines, pattern)

    def lines(
        self, pattern: str | re.Pattern[str] | None = None, *, since: Duration | None = None
    ) -> list[ConsoleLine]:
        """The lines printed so far, those that ended at `since` or later when it is given, and
        those that match `pattern` when it is given, with their match. A line not ended yet is
        not one."""
        if since is not None and not isinstance(since, Duration):
            raise TypeError(f"console.lines: since is {since!r}, not a Duration, as seconds(5)")
        regex = None if pattern is None else line_pattern(pattern)
        found = []
        for line in self._board.console_lines.lines:
            if since is not None and line.at < since:
                continue
            if regex is None:
                found.append(line)
            elif (match := regex.search(line.text)) is not None:
                found.append(ConsoleLine(line.at, line.text, match))
        return found


def timeout_of(timeout: Duration | float) -> Duration:
    """An expect's `timeout`: a `Duration`, or a number of seconds, pytest-embedded's unit; 0 or
    more."""
    if not isinstance(timeout, Duration):
        try:
            timeout = seconds(number(timeout, "expect: timeout"))
        except (TypeError, ValueError) as error:
            raise type(error)(
                f"expect: timeout is {timeout!r}, not a Duration or a number of seconds"
            ) from None
    if timeout.ns < 0:
        raise ValueError(f"expect: timeout is {timeout}, less than 0")
    return timeout


def line_pattern(pattern: str | re.Pattern[str]) -> re.Pattern[str]:
    """`pattern`, a regular expression on a line's text, compiled."""
    if isinstance(pattern, re.Pattern) and isinstance(pattern.pattern, str):
        return pattern
    if isinstance(pattern, str):
        return re.compile(pattern)
    raise TypeError(f"console.line: {pattern!r} is not a regular expression on text")


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
