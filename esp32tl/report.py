"""The run on the test's clock, for the report of a test that failed: what the test did, what the
firmware printed, and the bytes that crossed each route, in the order of the board's time.

The backend fills a `RunLog` as the run goes: the test's steps from the test's thread, the console
and the routes' bytes from the link's. It keeps every step of the test and the last `ENTRIES` of
the console and the routes, and counts every byte. It keeps what it is given as it is, and writes
it as text only when the report asks for its lines.
"""

import re
import threading
from collections import deque
from dataclasses import dataclass

from esp32tl.recording import Direction

# How many entries of the console and the routes the log keeps, the last ones: what led to the
# failure is at the end. The test's steps are few, and all kept.
ENTRIES = 200

# How many bytes of an event an entry shows.
SHOWN_BYTES = 100

# Who an entry is from, besides a route's name and direction.
TEST = "test"
CONSOLE = "console"

# The colour codes of ESP-IDF's log lines.
COLOUR = re.compile(rb"\x1b\[[0-9;]*m")

# The control characters of a console line that would move a terminal's cursor: all but the tab.
CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")

# An entry: its time, its place in the order entries came in, who it is from, and what it says,
# as text (a step of the test) or as the bytes of a console line or of a route's event.
Entry = tuple[int, int, str, str | bytes]


@dataclass
class Traffic:
    """The bytes that crossed one route each way, and in how many events."""

    bytes_in: int = 0
    bytes_out: int = 0
    events_in: int = 0
    events_out: int = 0


class RunLog:
    """The entries of one run, each a time on the board's clock, who it is from and what it says.

    Times are nanoseconds since power-on; what the test does before power-on is at 0. Entries are
    shown in time order, those of one time in the order they came. The console's bytes are cut
    into lines, each at the time of its first byte.
    """

    def __init__(self, entries: int = ENTRIES) -> None:
        self._lock = threading.Lock()
        self._steps: list[Entry] = []
        self._entries: deque[Entry] = deque(maxlen=entries)
        self._count = 0
        # How many entries of the console and the routes came, the dropped ones included.
        self._wire_count = 0
        # The console's line in progress, and the time of its first byte.
        self._line = bytearray()
        self._line_t = 0
        self.traffic: dict[str, Traffic] = {}

    def test(self, t: int, text: str) -> None:
        """The test did `text` at `t`."""
        with self._lock:
            self._steps.append((t, self._count, TEST, text))
            self._count += 1

    def console(self, t: int, data: bytes) -> None:
        """The firmware printed `data` at `t`."""
        with self._lock:
            for piece in data.splitlines(keepends=True):
                if not self._line:
                    self._line_t = t
                self._line += piece
                if piece.endswith(b"\n"):
                    self._add(self._line_t, CONSOLE, bytes(self._line))
                    self._line.clear()

    def route(self, t: int, interface: str, direction: Direction, data: bytes) -> None:
        """`data` crossed the wire of `interface` at `t`, `in` to the ESP32 or `out` of it."""
        if not data:
            return
        with self._lock:
            traffic = self.traffic.setdefault(interface, Traffic())
            if direction == "in":
                traffic.bytes_in += len(data)
                traffic.events_in += 1
            else:
                traffic.bytes_out += len(data)
                traffic.events_out += 1
            self._add(t, f"{interface} {direction}", data)

    def _add(self, t: int, who: str, what: bytes) -> None:
        self._entries.append((t, self._count, who, what))
        self._count += 1
        self._wire_count += 1

    def lines(self) -> list[str]:
        """The entries, one line each, after a line that says how many there are."""
        with self._lock:
            entries = self._steps + list(self._entries)
            wire = self._wire_count
            if self._line:
                entries.append((self._line_t, self._count, CONSOLE, bytes(self._line)))
                wire += 1
            kept = len(entries) - len(self._steps)
        if not entries:
            return ["the run on the board's clock: nothing happened"]
        entries.sort(key=lambda entry: entry[:2])
        count = len(entries) - kept + wire
        heading = f"{count} {'entry' if count == 1 else 'entries'}"
        if kept < wire:
            heading += (
                f": every step of the test, and the last {kept} of the console and the routes"
            )
        width = max(len(who) for _, _, who, _ in entries)
        return [f"the run on the board's clock, {heading}:"] + [
            f"  {board_time(t)}  {who:<{width}}  {text(who, what)}".rstrip()
            for t, _, who, what in entries
        ]


def text(who: str, what: str | bytes) -> str:
    """What an entry says, as the log shows it."""
    if isinstance(what, str):
        return what
    return console_line(what) if who == CONSOLE else shown(what)


def console_line(line: bytes | bytearray) -> str:
    """A line of the console as the log shows it: without its line ending or colour codes, and
    with any other control character but the tab escaped, so that it cannot move the cursor over
    the line's time."""
    decoded = COLOUR.sub(b"", bytes(line).rstrip(b"\r\n")).decode(errors="backslashreplace")
    return CONTROL.sub(lambda match: escaped(ord(match.group())), decoded)


# How `shown` writes the bytes that are not printable as they are.
ESCAPES = {ord("\r"): "\\r", ord("\n"): "\\n", ord("\t"): "\\t", ord("\\"): "\\\\"}


def escaped(byte: int) -> str:
    return ESCAPES.get(byte) or f"\\x{byte:02x}"


def shown(data: bytes, limit: int = SHOWN_BYTES) -> str:
    """`data` as text: printable ASCII as it is, `\\r`, `\\n`, `\\t` and `\\\\` escaped, any other
    byte as `\\xNN`; cut after `limit` bytes, with the length."""
    text = "".join(
        chr(byte) if 0x20 <= byte < 0x7F and byte != 0x5C else escaped(byte)
        for byte in data[:limit]
    )
    return text if len(data) <= limit else f"{text}... ({len(data)} bytes)"


def board_time(t: int) -> str:
    """`t` in seconds, to the nanosecond, as the link's report shows times."""
    return f"{t // 10**9}.{t % 10**9:09d} s"
