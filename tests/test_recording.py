"""The recording format, `esp32tl/recording.py` (docs/recording-format.md): reading, writing, the
errors that name the file and the line, and times that do not compare across clocks."""

import json
from pathlib import Path
from typing import Any

import pytest

from esp32tl import recording
from esp32tl.recording import ClockError, ClockSwitch, Event, Header, Recording, RecordingError

ROOT = Path(__file__).resolve().parent.parent
RECORDINGS = sorted((ROOT / "recordings").glob("*.jsonl"))

CLOCK = "emulated-fifo-write"
HEADER = {
    "esp32tl_recording": 1,
    "board": "esp32-devkitc",
    "interface": "uart.gnss",
    "source": "model:nmea",
    "clock": "emulated-fifo-write",
    "end": "hold",
    "app_sha256": "ab" * 32,
    "tools": {"esp32tl": "0.1.0", "esp32sim": "8c652151dd5a521249a785ca147f20a9f8eda68f"},
    "created": "2026-10-29T18:00:00Z",
}


def text(*lines: dict[str, Any]) -> str:
    return "".join(json.dumps(line) + "\n" for line in lines)


def event(t: object, data: str = "JEdQ", direction: str = "in") -> dict[str, Any]:
    return {"t": t, "dir": direction, "data": data}


@pytest.mark.parametrize("path", RECORDINGS, ids=lambda p: p.name)
def test_a_committed_recording_reads_and_writes_back_byte_for_byte(path: Path) -> None:
    read = recording.read(path)
    header = read.header
    # What scripts/generate-gnss-recordings.py writes: no app, no tools, its own field.
    assert (header.board, header.interface, header.source) == (
        "esp32-devkitc",
        "uart.gnss",
        "generated",
    )
    assert (header.clock, header.end, header.app_sha256, dict(header.tools)) == (
        "emulated-fifo-write",
        "hold",
        None,
        {},
    )
    assert header.extra["generator"]["baud"] == 9600
    assert len(read.events) == 60
    assert read.events[0].data.startswith(b"$GPGGA,")
    assert recording.dumps(read) == path.read_text()


def test_the_header_s_optional_fields_take_their_defaults() -> None:
    minimal = {k: v for k, v in HEADER.items() if k not in ("end", "app_sha256", "tools")}
    read = recording.loads(text(minimal))
    assert (read.header.end, read.header.app_sha256, dict(read.header.tools)) == ("hold", None, {})
    assert read.lines == ()
    # Written out, every field of the format is there.
    assert json.loads(recording.dumps(read)) == HEADER | {
        "end": "hold",
        "app_sha256": None,
        "tools": {},
    }


def test_a_clock_switch_puts_the_events_after_it_on_the_new_clock() -> None:
    source = text(
        HEADER,
        event(5, direction="out"),
        event(10),
        {"t": 0, "clock": "wall"},
        event(0),
        event(7, direction="out"),
    )
    read = recording.loads(source)
    assert [type(line).__name__ for line in read.lines] == [
        "Event",
        "Event",
        "ClockSwitch",
        "Event",
        "Event",
    ]
    assert [e.clock for e in read.events] == ["emulated-fifo-write"] * 2 + ["wall"] * 2
    assert read.events[0].data == b"$GP"
    assert recording.dumps(read) == source


def test_times_compare_on_one_clock_only() -> None:
    read = recording.loads(
        text(HEADER, event(5, direction="out"), event(12), {"t": 20, "clock": "wall"}, event(30))
    )
    request, answer, later = read.events
    assert answer.since(request) == 7
    with pytest.raises(ClockError) as raised:
        later.since(answer)
    assert str(raised.value) == (
        "the event at 30 ns is on the wall clock, the one at 12 ns on the emulated-fifo-write"
        " clock: their times do not compare"
    )


def test_write_and_read_go_through_a_file(tmp_path: Path) -> None:
    built = Recording(
        Header("esp32-devkitc", "uart.gnss", "model:nmea", "wall", "2026-10-29T18:00:00Z"),
        (Event(1, "in", b"\x00\xff", "wall"), ClockSwitch(2, "emulated-fifo-write")),
    )
    recording.write(tmp_path / "r.jsonl", built)
    assert recording.read(tmp_path / "r.jsonl") == built


# Each case: the lines after the header, or a whole text when it is a str, and the message, after
# "<recording>:<line>: ".
BAD = [
    ("", "<recording>: empty: a recording starts with its header"),
    (json.dumps(HEADER), "<recording>:1: the last line does not end with a newline"),
    ("[]\n", "<recording>:1: not a JSON object"),
    ("{\n", "<recording>:1: not JSON: Expecting property name enclosed in double quotes:"),
    (
        text(event(0)),
        "<recording>:1: no esp32tl_recording: the first line is not a recording's header",
    ),
    (
        text(HEADER | {"esp32tl_recording": 2}),
        "<recording>:1: esp32tl_recording: 2: esp32tl reads version 1 of the format",
    ),
    (
        text(HEADER | {"esp32tl_recording": True}),
        "<recording>:1: esp32tl_recording: True: esp32tl reads version 1 of the format",
    ),
    (
        text({k: v for k, v in HEADER.items() if k not in ("board", "created")}),
        "<recording>:1: missing board, created",
    ),
    (text(HEADER | {"board": ""}), "<recording>:1: board: '' is not a non-empty string"),
    (
        text(HEADER | {"clock": "emulated"}),
        "<recording>:1: clock: 'emulated' is not one of emulated-fifo-write, wall",
    ),
    (text(HEADER | {"end": "stop"}), "<recording>:1: end: 'stop' is not one of hold, loop"),
    (
        text(HEADER | {"app_sha256": "AB" * 32}),
        "<recording>:1: app_sha256: '" + "AB" * 32 + "' is not null or 64 lowercase hex digits",
    ),
    (
        text(HEADER | {"tools": {"esp32tl": 1}}),
        "<recording>:1: tools: {'esp32tl': 1} is not an object of strings",
    ),
    (
        text(HEADER | {"created": "2026-10-29 18:00"}),
        "<recording>:1: created: '2026-10-29 18:00' is not a UTC time, as in 2026-10-29T18:00:00Z",
    ),
    (
        text(HEADER | {"created": "2026-10-29T19:00:00+01:00"}),
        "<recording>:1: created: '2026-10-29T19:00:00+01:00' is not a UTC time",
    ),
    (text(HEADER).replace("\n", "\r\n"), "<recording>:1: the line ends with \\r\\n, not \\n"),
    (text(HEADER) + "\n", "<recording>:2: not JSON: Expecting value: line 1 column 1 (char 0)"),
    (
        text(HEADER, event(-1)),
        "<recording>:2: t: -1 is not a time in nanoseconds, an integer from 0",
    ),
    (text(HEADER, event(1.5)), "<recording>:2: t: 1.5 is not a time in nanoseconds"),
    (text(HEADER, event(True)), "<recording>:2: t: True is not a time in nanoseconds"),
    (text(HEADER, event(0, direction="to")), "<recording>:2: dir: 'to' is not one of in, out"),
    (text(HEADER, event(0, data="")), "<recording>:2: data: an event carries at least one byte"),
    (text(HEADER, event(0, data="JEd")), "<recording>:2: data: 'JEd' is not base64"),
    (
        text(HEADER, {"t": 0, "dir": "in", "data": 1}),
        "<recording>:2: data: 1 is not a base64 string",
    ),
    (
        text(HEADER, {"t": 0, "data": "JEdQ", "len": 3}),
        "<recording>:2: missing dir; unknown key len: an event is {t, dir, data}, a clock switch"
        " {t, clock}",
    ),
    (
        text(HEADER, event(10), event(9)),
        "<recording>:3: t 9 is before 10, a time before it on the emulated-fifo-write clock",
    ),
    (
        text(HEADER, event(10), {"t": 20, "clock": "wall"}, event(19)),
        "<recording>:4: t 19 is before 20, a time before it on the wall clock",
    ),
    (
        # Back on a clock it left: its times go on from where they were.
        text(HEADER, event(100), {"t": 5, "clock": "wall"}, {"t": 50, "clock": CLOCK}),
        "<recording>:4: t 50 is before 100, a time before it on the emulated-fifo-write clock",
    ),
    (
        text(HEADER | {"created": "20261029T180000Z"}),
        "<recording>:1: created: '20261029T180000Z' is not a UTC time",
    ),
    (
        text(HEADER | {"created": "2026-10-29T18Z"}),
        "<recording>:1: created: '2026-10-29T18Z' is not a UTC time",
    ),
    (
        text(HEADER, {"t": 0, "clock": "emulated-fifo-write"}),
        "<recording>:2: switches to the emulated-fifo-write clock, which the recording is on",
    ),
    (
        text(HEADER, {"t": 0, "clock": "sidereal"}),
        "<recording>:2: clock: 'sidereal' is not one of emulated-fifo-write, wall",
    ),
]


@pytest.mark.parametrize(("source", "message"), BAD)
def test_a_recording_that_breaks_the_format_is_refused_with_its_line(
    source: str, message: str
) -> None:
    with pytest.raises(RecordingError) as raised:
        recording.loads(source)
    assert str(raised.value).startswith(message)


def test_the_errors_name_the_file(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text(text(HEADER, event(10), event(9)))
    with pytest.raises(RecordingError, match=rf"^{path}:3: t 9 is before 10") as raised:
        recording.read(path)
    # The same as attributes, for code.
    assert (raised.value.file, raised.value.line) == (str(path), 3)
    assert raised.value.what.startswith("t 9 is before 10")
    path.write_text(text(HEADER, event(-1)))
    with pytest.raises(RecordingError, match=rf"^{path}:2: t: -1 ") as raised:
        recording.read(path)
    assert raised.value.line == 2
    with pytest.raises(RecordingError, match=rf"^{tmp_path / 'none.jsonl'}: \[Errno 2\]"):
        recording.read(tmp_path / "none.jsonl")


def test_a_recording_built_in_code_is_checked_as_a_read_one() -> None:
    header = Header("esp32-devkitc", "uart.gnss", "model:nmea", "wall", "2026-10-29T18:00:00Z")
    with pytest.raises(RecordingError, match=r"^line 2: an event on the emulated-fifo-write clock"):
        Recording(header, (Event(0, "in", b"x", "emulated-fifo-write"),))
    with pytest.raises(RecordingError, match=r"^line 3: t 1 is before 2"):
        Recording(header, (Event(2, "in", b"x", "wall"), Event(1, "in", b"x", "wall")))
    with pytest.raises(RecordingError, match=r"^extra: clock is a field of the format"):
        Header(
            "esp32-devkitc",
            "uart.gnss",
            "model:nmea",
            "wall",
            "2026-10-29T18:00:00Z",
            extra={"clock": "wall"},
        )
