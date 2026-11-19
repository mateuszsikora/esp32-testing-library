"""The recording format: what crossed one interface's wire, and when (`docs/recording-format.md`).

A recording is a `.jsonl` file: a header line, then one line per event, `{t, dir, data}`, and a
`{t, clock}` line wherever the clock the times are on changes. Times on two clocks do not compare:
`Event.since` refuses to, with `ClockError`.

An HCI tap is a btsnoop file of H4 packets, with the recording's header in a sidecar `.json` beside
it: `write_btsnoop` writes the two, `read_btsnoop` reads them back as a `Recording`.
"""

import base64
import binascii
import json
import os
import re
import struct
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
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
    """A recording that breaks the format, said with the file and the line at fault: `what`, the
    error alone; `file`, the file, `None` for a recording built in code; `line`, the line's number,
    `None` for an error about the whole file."""

    def __init__(self, what: str, line: int | None = None, file: str | None = None) -> None:
        where = ":".join(str(part) for part in (file, line) if part is not None)
        if file is None and line is not None:
            where = f"line {line}"
        super().__init__(f"{where}: {what}" if where else what)
        self.what = what
        self.line = line
        self.file = file


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
        raise RecordingError(str(error), file=str(path)) from None
    return loads(text, str(path))


def loads(text: str, name: str = "<recording>") -> Recording:
    """The recording in `text`, whose errors name `name` as the file."""
    lines = text.split("\n")
    if lines[-1]:
        raise RecordingError(
            "the last line does not end with a newline", line=len(lines), file=name
        )
    lines.pop()
    for number, text_line in enumerate(lines, start=1):
        if text_line.endswith("\r"):
            raise RecordingError("the line ends with \\r\\n, not \\n", line=number, file=name)
    if not lines:
        raise RecordingError("empty: a recording starts with its header", file=name)
    try:
        header = _header(_object(lines[0]))
    except RecordingError as error:
        raise RecordingError(error.what, line=1, file=name) from None
    clock = header.clock
    body: list[Line] = []
    for number, text_line in enumerate(lines[1:], start=2):
        try:
            line = _line(_object(text_line), clock)
        except RecordingError as error:
            raise RecordingError(error.what, line=number, file=name) from None
        if isinstance(line, ClockSwitch):
            clock = line.clock
        body.append(line)
    try:
        return Recording(header, tuple(body))
    except RecordingError as error:
        raise RecordingError(error.what, line=error.line, file=name) from None


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


def _header_line(header: Header) -> str:
    """`header` as its line: the format's fields in order, then the `extra` ones."""
    fields: dict[str, Any] = {"esp32tl_recording": VERSION}
    fields |= {f: getattr(header, f) for f in HEADER_FIELDS[1:]}
    fields["tools"] = dict(header.tools)
    fields |= header.extra
    return json.dumps(fields)


def dumps(recording: Recording) -> str:
    """`recording` as the text of its file. The header's fields come in the format's order, then
    its `extra` ones; `loads(dumps(r)) == r`."""
    lines = [_header_line(recording.header)]
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


# HCI taps: btsnoop version 1 (RFC 1761's snoop, as Bluetooth stacks write it), whose datalink 1002
# is HCI UART: each record is one H4 packet, its packet-type byte first.
BTSNOOP_MAGIC = b"btsnoop\0"
BTSNOOP_VERSION = 1
BTSNOOP_H4 = 1002
# The file's header, `magic, version, datalink`, and each record's, `original length, included
# length, flags, cumulative drops, timestamp`, all big-endian.
_BTSNOOP_HEADER = struct.Struct(">8sII")
_BTSNOOP_RECORD = struct.Struct(">IIIIq")
# A record's timestamp is in microseconds from midnight, 1 January of the year 0; this is
# 1970-01-01T00:00:00Z on that scale, the instant a time of 0 ns is written at, so that a reader
# such as Wireshark shows the time since power-on.
BTSNOOP_EPOCH_US = 0x00DCDDB30F2F8000
# The flags: bit 0, the packet went from the controller to the host (`in`), not from the host to
# the controller (`out`); bit 1, a command or an event, not data.
BTSNOOP_RECEIVED = 0x1
BTSNOOP_COMMAND_OR_EVENT = 0x2
# The H4 packet types of a command and an event (Vol 4, Part A, 2).
H4_COMMAND = 0x01
H4_EVENT = 0x04
# What the name of the sidecar adds to the btsnoop file's.
SIDECAR = ".json"


def sidecar(path: str | os.PathLike[str]) -> Path:
    """The sidecar of the btsnoop file at `path`: its name with `.json` added."""
    return Path(os.fspath(path) + SIDECAR)


def dumps_btsnoop(recording: Recording) -> tuple[bytes, str]:
    """`recording` as an HCI tap: the btsnoop file's bytes, every event a record, `out` sent by
    the host and `in` received by it, each time cut down to the microsecond; and the sidecar's
    text, the header's line. Refuses a recording with a clock switch: a btsnoop file has one
    clock."""
    switch = next((line for line in recording.lines if isinstance(line, ClockSwitch)), None)
    if switch is not None:
        raise RecordingError(
            f"a clock switch at {switch.t}: an HCI tap is on its header's clock alone"
        )
    out = bytearray(_BTSNOOP_HEADER.pack(BTSNOOP_MAGIC, BTSNOOP_VERSION, BTSNOOP_H4))
    for event in recording.events:
        flags = BTSNOOP_RECEIVED if event.dir == "in" else 0
        if event.data[0] in (H4_COMMAND, H4_EVENT):
            flags |= BTSNOOP_COMMAND_OR_EVENT
        length = len(event.data)
        out += _BTSNOOP_RECORD.pack(length, length, flags, 0, BTSNOOP_EPOCH_US + event.t // 1000)
        out += event.data
    return bytes(out), _header_line(recording.header) + "\n"


def write_btsnoop(path: str | os.PathLike[str], recording: Recording) -> None:
    """Writes `recording` as an HCI tap: the btsnoop file at `path`, and its sidecar."""
    data, header = dumps_btsnoop(recording)
    Path(path).write_bytes(data)
    sidecar(path).write_text(header, encoding="utf-8")


def loads_btsnoop(data: bytes, header: str, name: str = "<btsnoop>") -> Recording:
    """The HCI tap in the btsnoop file's `data`, with the sidecar's text `header`, whose errors
    name `name` as the file: each record an event at its timestamp's microsecond, in
    nanoseconds, on the header's clock."""
    try:
        head = _header(_object(header.strip("\n")))
    except RecordingError as error:
        raise RecordingError(error.what, file=str(sidecar(name))) from None
    if len(data) < _BTSNOOP_HEADER.size or data[:8] != BTSNOOP_MAGIC:
        raise RecordingError(
            "not a btsnoop file: it does not start with btsnoop and a zero byte", file=name
        )
    _, version, datalink = _BTSNOOP_HEADER.unpack_from(data)
    if version != BTSNOOP_VERSION:
        raise RecordingError(
            f"btsnoop version {version}: esp32tl reads version {BTSNOOP_VERSION}", file=name
        )
    if datalink != BTSNOOP_H4:
        raise RecordingError(
            f"datalink {datalink}: esp32tl reads {BTSNOOP_H4}, HCI UART (H4)", file=name
        )
    events: list[Line] = []
    at = _BTSNOOP_HEADER.size
    while at < len(data):
        number = len(events) + 1
        if len(data) - at < _BTSNOOP_RECORD.size:
            raise RecordingError(f"record {number}: the file ends in its header", file=name)
        original, included, flags, _, timestamp = _BTSNOOP_RECORD.unpack_from(data, at)
        at += _BTSNOOP_RECORD.size
        if included != original:
            raise RecordingError(
                f"record {number}: {included} of the packet's {original} bytes: a cut packet",
                file=name,
            )
        if len(data) - at < included:
            raise RecordingError(f"record {number}: the file ends in its packet", file=name)
        if timestamp < BTSNOOP_EPOCH_US:
            raise RecordingError(
                f"record {number}: timestamp {timestamp} is before {BTSNOOP_EPOCH_US}, the time"
                " of 0 ns",
                file=name,
            )
        packet = data[at : at + included]
        at += included
        direction: Direction = "in" if flags & BTSNOOP_RECEIVED else "out"
        t = (timestamp - BTSNOOP_EPOCH_US) * 1000
        try:
            events.append(Event(t, direction, packet, head.clock))
        except RecordingError as error:
            raise RecordingError(f"record {number}: {error.what}", file=name) from None
    try:
        return Recording(head, tuple(events))
    except RecordingError as error:
        what = error.what
        if error.line is not None:
            what = f"record {error.line - 1}: {what}"
        raise RecordingError(what, file=name) from None


def read_btsnoop(path: str | os.PathLike[str]) -> Recording:
    """The HCI tap in the btsnoop file at `path` and its sidecar."""
    try:
        data = Path(path).read_bytes()
    except OSError as error:
        raise RecordingError(str(error), file=os.fspath(path)) from None
    try:
        header = sidecar(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise RecordingError(str(error), file=str(sidecar(path))) from None
    return loads_btsnoop(data, header, os.fspath(path))


# Converting what a real device sent into a recording a run on the emulated clock replays: `retime`
# a recording on the wall clock, `from_nmea` a raw NMEA log. The first event goes in at `START_NS`
# unless asked otherwise: after nmea_reader has configured its UART, as the generated recordings.
START_NS = 1_000_000_000
# The header's field a conversion adds, saying what the recording was converted from.
CONVERTED = "converted"
NS_PER_S = 1_000_000_000


def retime(recording: Recording, *, start_ns: int = START_NS, end: str | None = None) -> Recording:
    """`recording`, on the wall clock, as a recording on the emulated clock that a replay takes:
    its `in` events at their times from the first of them, which goes in at `start_ns`, and none
    of its `out` events, so that it is a stream with nothing to answer. The header is the
    original's on the emulated clock, its `end` `end` when given, with a `converted` field of what
    the conversion did."""
    _time(start_ns)
    header = recording.header
    clocks = sorted({line.clock for line in recording.lines} | {header.clock})
    if clocks == [EMULATED]:
        raise RecordingError(
            f"the recording is on the {EMULATED} clock already: a replay takes it as it is"
        )
    if clocks != [WALL]:
        raise RecordingError(
            f"the recording has times on the {' and '.join(clocks)} clock: retime converts one"
            f" on the {WALL} clock alone"
        )
    ins = [event for event in recording.events if event.dir == "in"]
    if not ins:
        raise RecordingError("the recording has no in event: nothing to replay")
    first = ins[0].t
    converted = {
        "from": WALL,
        "first_t": first,
        "start_ns": start_ns,
        "dropped_out": len(recording.events) - len(ins),
    }
    retimed = Header(
        board=header.board,
        interface=header.interface,
        source=header.source,
        clock=EMULATED,
        created=header.created,
        end=header.end if end is None else end,
        app_sha256=header.app_sha256,
        tools=header.tools,
        extra={**header.extra, CONVERTED: converted},
    )
    return Recording(
        retimed, tuple(Event(e.t - first + start_ns, "in", e.data, EMULATED) for e in ins)
    )


# A line of a raw NMEA log: `$` and printable ASCII; after the `$`, the body, which holds no `$`,
# `!` or `*`, then `*` and the checksum, two hex digits, the XOR of the body's characters.
NMEA_SENTENCE = re.compile(rb"\$[\x20-\x7e]*")
NMEA_CHECKSUM = re.compile(rb"([^$!*]*)\*([0-9A-Fa-f]{2})")
# The sentences whose time tells the epochs apart, by their address without its talker: GPRMC,
# GNRMC and BDRMC are all RMC. A proprietary sentence, whose address starts with P, is none of
# them, Garmin's PGRMC among them.
NMEA_TIMED = ("RMC", "GGA")
# What a Windows editor may put before the first line: UTF-8's byte order mark.
BOM = b"\xef\xbb\xbf"
# hhmmss, then fractions of a second if any.
NMEA_TIME = re.compile(r"([01]\d|2[0-3])([0-5]\d)([0-5]\d)(?:\.(\d{1,9}))?")
SECONDS_PER_DAY = 86_400
# The bits a character takes on a UART at 8N1: a start bit, eight data bits, a stop bit.
BITS_PER_CHARACTER = 10


@dataclass
class _Epoch:
    """One epoch of a raw NMEA log: its sentences, from `line` on. `timed` says it has an RMC or
    a GGA, which only the sentences before the first of them lack; `addresses` are those RMC's
    and GGA's, and `time` their time in nanoseconds of the day, `None` when they have none, as in
    a cold start."""

    line: int
    sentences: list[bytes] = field(default_factory=list)
    timed: bool = False
    addresses: set[str] = field(default_factory=set)
    time: int | None = None


def from_nmea(
    log: str | bytes,
    *,
    board: str,
    interface: str,
    baud: int,
    period_ns: int = NS_PER_S,
    start_ns: int = START_NS,
    created: str | None = None,
    source: str = "real",
    end: str = "hold",
    name: str = "<nmea>",
) -> Recording:
    """The raw NMEA log `log`, a receiver's sentences one a line, as a recording on the emulated
    clock of `interface` on `board`, whose errors name `name` as the file and the line at fault.

    An epoch is the sentences from one RMC or GGA to the next with another time, or the next of an
    address the epoch has already: an RMC and a GGA of one time are one epoch, and so are the
    sentences without a time after them. Sentences before the first RMC or GGA are the end of an
    epoch the log came in on, an epoch of their own. Epoch k is one event at
    `start_ns + k * period_ns`, its sentences back to back, each ended by `\\r\\n`. The times of two
    epochs that both have one are as many periods apart as the epochs are; an epoch that does not
    fit in its period at `baud` is refused. `created` is the first RMC's date and time when not
    given, the time of the conversion when no RMC has a date."""
    _time(start_ns)
    if not isinstance(period_ns, int) or isinstance(period_ns, bool) or period_ns <= 0:
        raise RecordingError(f"period_ns: {period_ns!r} is not a time in nanoseconds above 0")
    if not isinstance(baud, int) or isinstance(baud, bool) or baud <= 0:
        raise RecordingError(f"baud: {baud!r} is not a baud rate above 0")
    data = log.encode("utf-8") if isinstance(log, str) else log
    epochs: list[_Epoch] = []
    first_rmc: str | None = None
    # The latest epoch with a time: its time in nanoseconds of the day, as the log says it, its
    # line and its index.
    timed: tuple[int, str, int, int] | None = None
    for number, raw in enumerate(data.split(b"\n"), start=1):
        # Spaces, tabs and the line ending after the sentence, and a byte order mark before the
        # first, are no part of it.
        line = raw.rstrip(b" \t\r")
        if number == 1:
            line = line.removeprefix(BOM)
        if not line.strip() or line.startswith(b"#"):
            continue
        try:
            address, fields = _nmea_sentence(line)
            text = None
            if _nmea_timed(address):
                if len(fields) < 2:
                    raise RecordingError(
                        f"{address} has no time field: an RMC or a GGA gives one, empty when the"
                        " receiver has no time"
                    )
                text = _nmea_time(fields[1])
        except RecordingError as error:
            raise RecordingError(error.what, line=number, file=name) from None
        if text is not None:
            t = _nmea_ns(text) if text else None
            epoch = epochs[-1] if epochs else None
            if epoch is None or not epoch.timed or epoch.time != t or address in epoch.addresses:
                # The first RMC or GGA, after nothing or after the end of an epoch the log came in
                # on, or another time, or a second sentence of one address: a new epoch.
                epoch = _Epoch(number, timed=True, time=t)
                epochs.append(epoch)
                if t is not None:
                    if timed is not None:
                        _nmea_step(timed, t, text, len(epochs) - 1, period_ns, number, name)
                    timed = (t, text, number, len(epochs) - 1)
            epoch.addresses.add(address)
            if first_rmc is None and address[2:] == "RMC":
                first_rmc = _nmea_created(fields)
        elif not epochs:
            epochs.append(_Epoch(number))
        epochs[-1].sentences.append(line + b"\r\n")
    if not epochs:
        raise RecordingError("no NMEA sentence: nothing to replay", file=name)
    if not any(epoch.timed for epoch in epochs):
        raise RecordingError(
            f"no {' or '.join(NMEA_TIMED)} sentence: the epochs cannot be told apart", file=name
        )
    for epoch in epochs:
        size = sum(len(sentence) for sentence in epoch.sentences)
        takes = size * BITS_PER_CHARACTER * NS_PER_S // baud
        if takes > period_ns:
            raise RecordingError(
                f"the epoch from this line is {size} bytes, {_seconds(takes)} s at {baud} baud,"
                f" longer than its period of {_seconds(period_ns)} s",
                line=epoch.line,
                file=name,
            )
    header = Header(
        board=board,
        interface=interface,
        source=source,
        clock=EMULATED,
        created=created or first_rmc or _now(),
        end=end,
        extra={
            CONVERTED: {
                "from": "nmea",
                "baud": baud,
                "period_ns": period_ns,
                "start_ns": start_ns,
            }
        },
    )
    return Recording(
        header,
        tuple(
            Event(start_ns + k * period_ns, "in", b"".join(epoch.sentences), EMULATED)
            for k, epoch in enumerate(epochs)
        ),
    )


def read_nmea(path: str | os.PathLike[str], **options: Any) -> Recording:
    """The raw NMEA log in the file at `path` as a recording, `options` those of `from_nmea`."""
    try:
        data = Path(path).read_bytes()
    except OSError as error:
        raise RecordingError(str(error), file=os.fspath(path)) from None
    return from_nmea(data, name=os.fspath(path), **options)


def _nmea_sentence(line: bytes) -> tuple[str, list[str]]:
    """The address and the fields of the sentence `line`, its checksum checked."""
    # The line's first 40 bytes as Python writes bytes, a byte outside printable ASCII as \xb5.
    shown = repr(line[:40])[1:]
    if not line.startswith(b"$"):
        raise RecordingError(f"{shown} is not an NMEA sentence: a sentence starts with $")
    if not NMEA_SENTENCE.fullmatch(line):
        raise RecordingError(f"{shown} is not an NMEA sentence: a sentence is printable ASCII")
    match = NMEA_CHECKSUM.fullmatch(line[1:])
    if match is None:
        raise RecordingError(f"{shown} has no checksum: a sentence ends with * and two hex digits")
    body, checksum = match.group(1), int(match.group(2), 16)
    actual = 0
    for character in body:
        actual ^= character
    if actual != checksum:
        raise RecordingError(
            f"{shown}: checksum {checksum:02X}, and the sentence's is {actual:02X}"
        )
    fields = body.decode("ascii").split(",")
    return fields[0], fields


def _nmea_timed(address: str) -> bool:
    """Whether the sentence of `address` is an RMC or a GGA, of any talker."""
    return len(address) == 5 and not address.startswith("P") and address[2:] in NMEA_TIMED


def _nmea_time(text: str) -> str:
    """`text`, the time field of an RMC or a GGA, when it is one or empty."""
    if text and not NMEA_TIME.fullmatch(text):
        raise RecordingError(f"time {text!r} is not hhmmss or hhmmss.ss")
    return text


def _nmea_ns(text: str) -> int:
    """The time of the day `text` (hhmmss.ss), in nanoseconds."""
    match = NMEA_TIME.fullmatch(text)
    assert match is not None
    hours, minutes, seconds, fraction = match.groups()
    whole = (int(hours) * 60 + int(minutes)) * 60 + int(seconds)
    return whole * NS_PER_S + int((fraction or "").ljust(9, "0"))


def _nmea_step(
    timed: tuple[int, str, int, int],
    t: int,
    text: str,
    index: int,
    period: int,
    line: int,
    name: str,
) -> None:
    """Refuses the time `t` (`text`) of epoch `index`, on `line`, unless it is as many periods
    after the latest time, `timed`, as the epochs are apart. A time just after midnight is after
    one just before it."""
    before, before_text, before_line, before_index = timed
    since = t - before
    if since < -SECONDS_PER_DAY * NS_PER_S // 2:
        since += SECONDS_PER_DAY * NS_PER_S
    periods = index - before_index
    if since <= 0:
        raise RecordingError(
            f"time {text} is not after {before_text}, the time on line {before_line}:"
            " the log goes back",
            line=line,
            file=name,
        )
    if since != periods * period:
        what = "one period" if periods == 1 else f"{periods} periods"
        raise RecordingError(
            f"time {text} is {_seconds(since)} s after {before_text} on line {before_line}, not"
            f" {what} of {_seconds(period)} s: the log skips epochs, or the receiver sends at"
            " another rate",
            line=line,
            file=name,
        )


def _nmea_created(fields: list[str]) -> str | None:
    """The date and time of the RMC `fields` as a header's `created`, `None` without both."""
    if len(fields) < 10 or len(fields[1]) < 6 or not re.fullmatch(r"\d{6}", fields[9]):
        return None
    day, month, year = fields[9][:2], fields[9][2:4], int(fields[9][4:])
    # NMEA 0183 gives the year in two digits; GPS began in 1980.
    year += 1900 if year >= 80 else 2000
    text = f"{year}-{month}-{day}T{fields[1][:2]}:{fields[1][2:4]}:{fields[1][4:6]}Z"
    return text if _utc(text) else None


def _seconds(ns: int) -> str:
    """`ns` nanoseconds as seconds, without trailing zeros: `1`, `0.2`, `1.05`."""
    whole, fraction = divmod(ns, NS_PER_S)
    return f"{whole}.{fraction:09d}".rstrip("0").rstrip(".")


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
