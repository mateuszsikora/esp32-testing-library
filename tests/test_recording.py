"""The recording format, `esp32tl/recording.py` (docs/recording-format.md): reading, writing, the
errors that name the file and the line, and times that do not compare across clocks."""

import datetime
import io
import json
import re
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
    "tools": {"esp32tl": "0.1.0", "esp32sim": "c99fb286c4378fcfa6d304b754d31e542ba3035a"},
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


# HCI taps: btsnoop files with the header in a sidecar.

HCI_HEADER = Header(
    board="esp32-devkitc",
    interface="bluetooth",
    source="model:bluetooth",
    clock=CLOCK,
    created="2026-11-10T08:00:00Z",
    extra={"devices": [{"name": "ESP_SPP_ACCEPTOR", "address": "00:00:00:00:5B:01"}]},
)
# HCI_Reset, its Command_Complete, ACL data each way.
RESET = bytes.fromhex("01030c00")
COMPLETE = bytes.fromhex("040e0401030c00")
ACL_OUT = bytes.fromhex("0201200500010002aa")
ACL_IN = bytes.fromhex("0201200400000001bb")
HCI_EVENTS = (
    Event(1_234_000, "out", RESET, CLOCK),
    Event(1_244_000, "in", COMPLETE, CLOCK),
    Event(2_000_000_000, "out", ACL_OUT, CLOCK),
    Event(2_000_010_000, "in", ACL_IN, CLOCK),
)


def test_an_hci_tap_writes_a_btsnoop_file_and_its_header_beside_it(tmp_path: Path) -> None:
    tap = Recording(HCI_HEADER, HCI_EVENTS)
    recording.write_btsnoop(tmp_path / "hci.btsnoop", tap)
    assert recording.sidecar(tmp_path / "hci.btsnoop") == tmp_path / "hci.btsnoop.json"
    assert recording.read_btsnoop(tmp_path / "hci.btsnoop") == tap
    sidecar = (tmp_path / "hci.btsnoop.json").read_text()
    assert sidecar == recording.dumps(Recording(HCI_HEADER, ()))
    data = (tmp_path / "hci.btsnoop").read_bytes()
    assert data[:16] == b"btsnoop\0" + (1).to_bytes(4, "big") + (1002).to_bytes(4, "big")
    # The first record: lengths, flags (sent, a command), no drops, 1234 µs after 1970.
    record = data[16:40]
    assert record[:16] == bytes.fromhex("00000004000000040000000200000000")
    assert int.from_bytes(record[16:], "big") == 0x00DCDDB30F2F8000 + 1234
    assert data[40:44] == RESET


def test_bumble_reads_the_tap_as_it_writes_one(tmp_path: Path) -> None:
    """The format is the one Bumble's `BtSnooper` writes, and its reader takes it: each packet
    with its direction, at the time it was written at."""
    from bumble.apps.show import SnoopPacketReader
    from bumble.snoop import BtSnooper, Snooper

    data, _ = recording.dumps_btsnoop(Recording(HCI_HEADER, HCI_EVENTS))
    theirs = tmp_path / "bumble.btsnoop"
    with theirs.open("wb") as output:
        snooper = BtSnooper(output)
        for event in HCI_EVENTS:
            direction = Snooper.Direction.CONTROLLER_TO_HOST if event.dir == "in" else 0
            snooper.snoop(event.data, Snooper.Direction(direction))
    mine, other = data, theirs.read_bytes()
    assert mine[:16] == other[:16]
    # Record by record, all but the timestamps, which are Bumble's wall time.
    at = 16
    for event in HCI_EVENTS:
        length = 24 + len(event.data)
        a, b = mine[at : at + length], other[at : at + length]
        assert a[:16] == b[:16] and a[24:] == b[24:]
        at += length
    reader = SnoopPacketReader(io.BytesIO(data))  # type: ignore[no-untyped-call]
    read = []
    while True:
        at_time, direction, packet = reader.next_packet()  # type: ignore[no-untyped-call]
        if reader.at_end:
            break
        read.append((at_time, direction, bytes(packet)))
    # Bumble shows each time as one after 1970-01-01, the time since power-on.
    epoch = datetime.datetime(1970, 1, 1)
    assert read == [
        (epoch + datetime.timedelta(microseconds=e.t // 1000), 1 if e.dir == "in" else 0, e.data)
        for e in HCI_EVENTS
    ]


def test_an_hci_tap_keeps_times_to_the_microsecond(tmp_path: Path) -> None:
    tap = Recording(HCI_HEADER, (Event(1_234_999, "out", RESET, CLOCK),))
    recording.write_btsnoop(tmp_path / "hci.btsnoop", tap)
    assert recording.read_btsnoop(tmp_path / "hci.btsnoop").events[0].t == 1_234_000


def test_an_hci_tap_has_one_clock() -> None:
    switched = Recording(HCI_HEADER, (*HCI_EVENTS[:2], ClockSwitch(5, "wall")))
    with pytest.raises(RecordingError, match="a clock switch at 5: an HCI tap is on its header"):
        recording.dumps_btsnoop(switched)


def btsnoop(*records: tuple[int, int, int, int, bytes], head: bytes | None = None) -> bytes:
    """A btsnoop file of `(original length, flags, timestamp in µs from 1970, drops, packet)`."""
    data = head if head is not None else b"btsnoop\0" + bytes.fromhex("00000001000003ea")
    for original, flags, timestamp, drops, packet in records:
        data += original.to_bytes(4, "big") + len(packet).to_bytes(4, "big")
        data += flags.to_bytes(4, "big") + drops.to_bytes(4, "big")
        data += (0x00DCDDB30F2F8000 + timestamp).to_bytes(8, "big", signed=True) + packet
    return data


@pytest.mark.parametrize(
    ("data", "what"),
    [
        (b"pcap", "not a btsnoop file: it does not start with btsnoop and a zero byte"),
        (btsnoop(head=b"btsnoop\0" + bytes.fromhex("00000002000003ea")), "btsnoop version 2"),
        (btsnoop(head=b"btsnoop\0" + bytes.fromhex("00000001000003e9")), "datalink 1001"),
        (btsnoop((4, 2, 0, 0, RESET))[:30], "record 1: the file ends in its header"),
        (btsnoop((4, 2, 0, 0, RESET))[:-1], "record 1: the file ends in its packet"),
        (btsnoop((9, 2, 0, 0, RESET)), "record 1: 4 of the packet's 9 bytes: a cut packet"),
        (btsnoop((4, 2, -1, 0, RESET)), "record 1: timestamp .* is before"),
        (btsnoop((0, 2, 0, 0, b"")), "record 1: data: an event carries at least one byte"),
        (
            btsnoop((4, 2, 5, 0, RESET), (4, 2, 4, 0, RESET)),
            "record 2: t 4000 is before 5000",
        ),
    ],
)
def test_a_btsnoop_file_the_reader_cannot_take_is_refused_naming_it(
    tmp_path: Path, data: bytes, what: str
) -> None:
    path = tmp_path / "hci.btsnoop"
    path.write_bytes(data)
    recording.sidecar(path).write_text(recording.dumps(Recording(HCI_HEADER, ())))
    with pytest.raises(RecordingError, match=f"^{re.escape(str(path))}: {what}"):
        recording.read_btsnoop(path)


def test_an_hci_tap_without_its_sidecar_or_with_a_wrong_one_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "hci.btsnoop"
    path.write_bytes(btsnoop((4, 2, 0, 0, RESET)))
    with pytest.raises(RecordingError, match=r"hci\.btsnoop\.json: \[Errno 2\]"):
        recording.read_btsnoop(path)
    recording.sidecar(path).write_text('{"board": "x"}\n')
    with pytest.raises(RecordingError, match=r"hci\.btsnoop\.json: no esp32tl_recording"):
        recording.read_btsnoop(path)
    with pytest.raises(RecordingError, match=r"missing\.btsnoop: \[Errno 2\]"):
        recording.read_btsnoop(tmp_path / "missing.btsnoop")
