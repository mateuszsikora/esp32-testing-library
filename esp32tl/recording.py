"""The recording format: what crossed one interface's wire, and when (`docs/recording-format.md`).

A recording is a `.jsonl` file: a header line, then one line per event, `{t, dir, data}`, and a
`{t, clock}` line wherever the clock the times are on changes. Times on two clocks do not compare:
`Event.since` refuses to, with `ClockError`. HCI taps are btsnoop files with a sidecar header, and
are not read here.
"""

import base64
import binascii
import json
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from functools import cached_property
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

# The version of the format, the header's `esp32tl_recording`.
VERSION = 1

# The clocks a time can be on, in the vocabulary of the runner's `hello` (protocol 5.1):
# `emulated-fifo-write`, emulated nanoseconds since power-on, a transmitted byte stamped when the
# guest writes the UART FIFO; `wall`, a real board's or a real device's wall time.
EMULATED = "emulated-fifo-write"
WALL = "wall"
CLOCKS = (EMULATED, WALL)

# What happens after the last event: `hold`, nothing more; `loop`, the recording starts over.
ENDS = ("hold", "loop")

# `in` to the ESP32 or `out` of it.
Direction = Literal["in", "out"]
DIRECTIONS = ("in", "out")

# The header's fields, in the order they are written; the fields it may leave out, and what they
# are then.
HEADER_FIELDS = (
    "esp32tl_recording",
    "board",
    "interface",
    "source",
    "clock",
    "end",
    "app_sha256",
    "tools",
    "created",
)
HEADER_DEFAULTS: Mapping[str, Any] = MappingProxyType(
    {"end": "hold", "app_sha256": None, "tools": {}}
)

SHA256 = re.compile(r"[0-9a-f]{64}")
# `created`: a UTC time, seconds required, fractions allowed.
UTC_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z")


class RecordingError(Exception):
    """A recording that breaks the format, said with the file and the line at fault. `line` is
    the line's number when the error is about one, as `Recording` finds it; `what`, the error
    without it."""

    def __init__(self, what: str, line: int | None = None) -> None:
        super().__init__(what if line is None else f"line {line}: {what}")
        self.what = what
        self.line = line


class ClockError(Exception):
    """Times on two clocks compared: an emulated time and a wall time have nothing in common."""


@dataclass(frozen=True)
class Header:
    """A recording's first line. `extra` holds the fields the format does not define, such as
    the `generator` of `scripts/generate-gnss-recordings.py`, kept as they are and written after
    the format's own."""

    board: str
    interface: str
    source: str
    clock: str
    created: str
    end: str = "hold"
    app_sha256: str | None = None
    tools: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    extra: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        for name in ("board", "interface", "source"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise RecordingError(f"{name}: {value!r} is not a non-empty string")
        _clock(self.clock)
        if self.end not in ENDS:
            raise RecordingError(f"end: {self.end!r} is not one of {', '.join(ENDS)}")
        if self.app_sha256 is not None and not (
            isinstance(self.app_sha256, str) and SHA256.fullmatch(self.app_sha256)
        ):
            raise RecordingError(
                f"app_sha256: {self.app_sha256!r} is not null or 64 lowercase hex digits"
            )
        if not isinstance(self.tools, Mapping) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in self.tools.items()
        ):
            raise RecordingError(f"tools: {self.tools!r} is not an object of strings")
        if not isinstance(self.created, str) or not _utc(self.created):
            raise RecordingError(
                f"created: {self.created!r} is not a UTC time, as in 2026-10-29T18:00:00Z"
            )
        clash = sorted(set(self.extra) & set(HEADER_FIELDS))
        if clash:
            raise RecordingError(f"extra: {', '.join(clash)} is a field of the format")
        object.__setattr__(self, "tools", MappingProxyType(dict(self.tools)))
        object.__setattr__(self, "extra", MappingProxyType(dict(self.extra)))


@dataclass(frozen=True)
class Event:
    """`data` crossed the wire at `t` nanoseconds, `in` to the ESP32 or `out` of it. `clock` is
    the clock `t` is on: the header's, or the last clock switch's before the event."""

    t: int
    dir: Direction
    data: bytes
    clock: str

    def __post_init__(self) -> None:
        _time(self.t)
        if self.dir not in DIRECTIONS:
            raise RecordingError(f"dir: {self.dir!r} is not one of {', '.join(DIRECTIONS)}")
        if not isinstance(self.data, bytes) or not self.data:
            raise RecordingError("data: an event carries at least one byte")
        _clock(self.clock)

    def since(self, earlier: "Event") -> int:
        """The time from `earlier` to this event, in nanoseconds. Raises `ClockError` when the
        two are on different clocks."""
        if earlier.clock != self.clock:
            raise ClockError(
                f"the event at {self.t} ns is on the {self.clock} clock, the one at {earlier.t} ns"
                f" on the {earlier.clock} clock: their times do not compare"
            )
        return self.t - earlier.t


@dataclass(frozen=True)
class ClockSwitch:
    """From this line on, times are on `clock`, the first of them `t`, the time of the switch on
    the new clock: a modem session after `CONNECT` (plan M6)."""

    t: int
    clock: str

    def __post_init__(self) -> None:
        _time(self.t)
        _clock(self.clock)


Line = Event | ClockSwitch


@dataclass(frozen=True)
class Recording:
    """A header and the lines after it, in order. Within one clock, times never go back; a clock
    switch changes the clock, which every event after it carries, up to the next one."""

    header: Header
    lines: tuple[Line, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "lines", tuple(self.lines))
        at = _check(self.header, self.lines)
        if at is not None:
            index, what = at
            raise RecordingError(what, line=index + 2)

    @cached_property
    def events(self) -> tuple[Event, ...]:
        return tuple(line for line in self.lines if isinstance(line, Event))


def _check(header: Header, lines: Iterable[Line]) -> tuple[int, str] | None:
    """The first line that breaks the order of the format, by index, and what it breaks. Times
    never go back on one clock, also across a switch away from it and back."""
    clock = header.clock
    # The latest time on each clock so far.
    last = {clock: 0}
    for index, line in enumerate(lines):
        if isinstance(line, ClockSwitch):
            if line.clock == clock:
                return index, f"switches to the {clock} clock, which the recording is on"
            clock = line.clock
        elif isinstance(line, Event):
            if line.clock != clock:
                return index, f"an event on the {line.clock} clock, in a recording on {clock}"
        else:
            return index, f"{line!r} is not an event or a clock switch"
        since = last.get(clock, 0)
        if line.t < since:
            return index, f"t {line.t} is before {since}, a time before it on the {clock} clock"
        last[clock] = line.t
    return None


def _time(t: object) -> None:
    # bool is an int to Python, not to JSON.
    if not isinstance(t, int) or isinstance(t, bool) or t < 0:
        raise RecordingError(f"t: {t!r} is not a time in nanoseconds, an integer from 0")


def _clock(clock: object) -> None:
    if clock not in CLOCKS:
        raise RecordingError(f"clock: {clock!r} is not one of {', '.join(CLOCKS)}")


def _utc(text: str) -> bool:
    if not UTC_TIME.fullmatch(text):
        return False
    try:
        datetime.fromisoformat(text)
    except ValueError:
        return False
    return True


def read(path: str | os.PathLike[str]) -> Recording:
    """The recording in the file at `path`."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise RecordingError(f"{path}: {error}") from None
    return loads(text, str(path))


def loads(text: str, name: str = "<recording>") -> Recording:
    """The recording in `text`, whose errors name `name` as the file."""
    lines = text.split("\n")
    if lines[-1]:
        raise RecordingError(f"{name}:{len(lines)}: the last line does not end with a newline")
    lines.pop()
    for number, text_line in enumerate(lines, start=1):
        if text_line.endswith("\r"):
            raise RecordingError(f"{name}:{number}: the line ends with \\r\\n, not \\n")
    if not lines:
        raise RecordingError(f"{name}: empty: a recording starts with its header")
    try:
        header = _header(_object(lines[0]))
    except RecordingError as error:
        raise RecordingError(f"{name}:1: {error}") from None
    clock = header.clock
    body: list[Line] = []
    for number, text_line in enumerate(lines[1:], start=2):
        try:
            line = _line(_object(text_line), clock)
        except RecordingError as error:
            raise RecordingError(f"{name}:{number}: {error}") from None
        if isinstance(line, ClockSwitch):
            clock = line.clock
        body.append(line)
    try:
        return Recording(header, tuple(body))
    except RecordingError as error:
        raise RecordingError(f"{name}:{error.line}: {error.what}") from None


def _object(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except ValueError as error:
        raise RecordingError(f"not JSON: {error}") from None
    if not isinstance(value, dict):
        raise RecordingError("not a JSON object")
    return value


def _header(data: dict[str, Any]) -> Header:
    if "esp32tl_recording" not in data:
        raise RecordingError("no esp32tl_recording: the first line is not a recording's header")
    version = data["esp32tl_recording"]
    if type(version) is not int or version != VERSION:
        raise RecordingError(
            f"esp32tl_recording: {version!r}: esp32tl reads version {VERSION} of the format"
        )
    missing = [f for f in HEADER_FIELDS if f not in data and f not in HEADER_DEFAULTS]
    if missing:
        raise RecordingError(f"missing {', '.join(missing)}")
    known: dict[str, Any] = {f: data.get(f, HEADER_DEFAULTS.get(f)) for f in HEADER_FIELDS[1:]}
    extra = {k: v for k, v in data.items() if k not in HEADER_FIELDS}
    return Header(**known, extra=extra)


def _line(data: dict[str, Any], clock: str) -> Line:
    if set(data) == {"t", "clock"}:
        return ClockSwitch(data["t"], data["clock"])
    keys = set(data)
    if keys != {"t", "dir", "data"}:
        missing = sorted({"t", "dir", "data"} - keys)
        unknown = sorted(keys - {"t", "dir", "data"})
        what = [f"missing {', '.join(missing)}"] if missing else []
        what += [f"unknown key {', '.join(unknown)}"] if unknown else []
        raise RecordingError(
            "; ".join(what) + ": an event is {t, dir, data}, a clock switch {t, clock}"
        )
    encoded = data["data"]
    if not isinstance(encoded, str):
        raise RecordingError(f"data: {encoded!r} is not a base64 string")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except binascii.Error:
        raise RecordingError(f"data: {encoded[:40]!r} is not base64") from None
    return Event(data["t"], data["dir"], decoded, clock)


def dumps(recording: Recording) -> str:
    """`recording` as the text of its file. The header's fields come in the format's order, then
    its `extra` ones; `loads(dumps(r)) == r`."""
    header = recording.header
    fields: dict[str, Any] = {"esp32tl_recording": VERSION}
    fields |= {f: getattr(header, f) for f in HEADER_FIELDS[1:]}
    fields["tools"] = dict(header.tools)
    fields |= header.extra
    lines = [json.dumps(fields)]
    for line in recording.lines:
        if isinstance(line, ClockSwitch):
            lines.append(json.dumps({"t": line.t, "clock": line.clock}))
        else:
            data = base64.b64encode(line.data).decode()
            lines.append(json.dumps({"t": line.t, "dir": line.dir, "data": data}))
    return "\n".join(lines) + "\n"


def write(path: str | os.PathLike[str], recording: Recording) -> None:
    """Writes `recording` to the file at `path`."""
    Path(path).write_text(dumps(recording), encoding="utf-8")
