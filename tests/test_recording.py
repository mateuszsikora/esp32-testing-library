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
# What scripts/generate-gnss-recordings.py writes, and what `esp32tl recording nmea` made of a log
# beside it.
GENERATED = sorted((ROOT / "recordings").glob("gnss-generated-*.jsonl"))
NEO_M8N = ROOT / "recordings" / "gnss-neo-m8n"

CLOCK = "emulated-fifo-write"
HEADER = {
    "esp32tl_recording": 1,
    "board": "esp32-devkitc",
    "interface": "uart.gnss",
    "source": "model:nmea",
    "clock": "emulated-fifo-write",
    "end": "hold",
    "app_sha256": "ab" * 32,
    "tools": {"esp32tl": "0.1.0", "esp32sim": "357b0e542dfe97d4c62ec4a15c01c4969164448c"},
    "created": "2026-10-29T18:00:00Z",
}


def text(*lines: dict[str, Any]) -> str:
    return "".join(json.dumps(line) + "\n" for line in lines)


def event(t: object, data: str = "JEdQ", direction: str = "in") -> dict[str, Any]:
    return {"t": t, "dir": direction, "data": data}


def test_the_committed_recordings_are_the_generated_ones_and_the_converted_one() -> None:
    assert sorted([*GENERATED, NEO_M8N.with_suffix(".jsonl")]) == RECORDINGS


@pytest.mark.parametrize("path", RECORDINGS, ids=lambda p: p.name)
def test_a_committed_recording_reads_and_writes_back_byte_for_byte(path: Path) -> None:
    read = recording.read(path)
    header = read.header
    # For nmea_reader on the default profile's GNSS route, made without an app or a tool's run.
    assert (header.board, header.interface) == ("esp32-devkitc", "uart.gnss")
    assert (header.clock, header.end, header.app_sha256, dict(header.tools)) == (
        "emulated-fifo-write",
        "hold",
        None,
        {},
    )
    assert recording.dumps(read) == path.read_text()


@pytest.mark.parametrize("path", GENERATED, ids=lambda p: p.name)
def test_a_generated_recording_has_the_generator_s_field(path: Path) -> None:
    read = recording.read(path)
    # What scripts/generate-gnss-recordings.py writes: its own field.
    assert read.header.source == "generated"
    assert read.header.extra["generator"]["baud"] == 9600
    assert len(read.events) == 60
    assert read.events[0].data.startswith(b"$GPGGA,")


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


# Converting what a real device sent: a recording on the wall clock, and a raw NMEA log.

WALL_HEADER = Header(
    "esp32-devkitc",
    "uart.gnss",
    "real",
    recording.WALL,
    "2026-11-19T10:00:00Z",
    app_sha256="cd" * 32,
    tools={"esp32tl": "0.1.0"},
    extra={"note": "kept"},
)


def wall(*events: tuple[int, recording.Direction, bytes]) -> Recording:
    return Recording(
        WALL_HEADER,
        tuple(Event(t, d, data, recording.WALL) for t, d, data in events),
    )


def test_retime_keeps_the_in_events_from_the_first_at_the_start_and_drops_the_out_ones() -> None:
    captured = wall(
        (40_000_000, "out", b"$PMTK220,1000*1F\r\n"),
        (52_000_000, "in", b"$GPGGA,1"),
        (52_000_000, "in", b"20000*00\r\n"),
        (1_052_500_000, "in", b"$GPRMC"),
        (1_100_000_000, "out", b"AT"),
    )
    retimed = recording.retime(captured)
    assert retimed.events == (
        Event(1_000_000_000, "in", b"$GPGGA,1", recording.EMULATED),
        Event(1_000_000_000, "in", b"20000*00\r\n", recording.EMULATED),
        Event(2_000_500_000, "in", b"$GPRMC", recording.EMULATED),
    )
    header = retimed.header
    assert header.clock == recording.EMULATED
    # The rest of the header is the capture's, with what the conversion did beside its fields.
    assert (header.board, header.interface, header.source, header.created, header.end) == (
        "esp32-devkitc",
        "uart.gnss",
        "real",
        "2026-11-19T10:00:00Z",
        "hold",
    )
    assert (header.app_sha256, dict(header.tools)) == ("cd" * 32, {"esp32tl": "0.1.0"})
    assert dict(header.extra) == {
        "note": "kept",
        "converted": {
            "from": "wall",
            "first_t": 52_000_000,
            "start_ns": 1_000_000_000,
            "dropped_out": 2,
        },
    }
    # It reads back as written: a replay on the emulated clock takes it.
    assert recording.loads(recording.dumps(retimed)) == retimed


def test_retime_starts_where_it_is_asked_and_ends_as_asked() -> None:
    retimed = recording.retime(wall((7, "in", b"$")), start_ns=0)
    assert [event.t for event in retimed.events] == [0]
    assert retimed.header.extra["converted"]["start_ns"] == 0
    assert retimed.header.end == "hold"
    assert recording.retime(wall((7, "in", b"$")), end="loop").header.end == "loop"


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (
            lambda: Recording(
                Header("b", "uart.gnss", "replay", recording.EMULATED, "2026-11-19T10:00:00Z"),
                (),
            ),
            "the recording is on the emulated-fifo-write clock already: a replay takes it as it is",
        ),
        (
            lambda: Recording(
                WALL_HEADER,
                (ClockSwitch(5, recording.EMULATED), Event(5, "in", b"x", recording.EMULATED)),
            ),
            "the recording has times on the emulated-fifo-write and wall clock: retime converts"
            " one on the wall clock alone",
        ),
        (
            lambda: wall((1, "out", b"AT")),
            "the recording has no in event: nothing to replay",
        ),
    ],
)
def test_retime_refuses(make: Any, message: str) -> None:
    with pytest.raises(RecordingError) as error:
        recording.retime(make())
    assert str(error.value) == message


def sentence(body: str) -> str:
    check = 0
    for character in body.encode("ascii"):
        check ^= character
    return f"${body}*{check:02X}"


def nmea_log(*bodies: str, ending: str = "\r\n") -> bytes:
    return "".join(sentence(body) + ending for body in bodies).encode("ascii")


def from_nmea(log: bytes, **options: Any) -> Recording:
    defaults: dict[str, Any] = {
        "board": "esp32-devkitc",
        "interface": "uart.gnss",
        "baud": 9600,
        "name": "log.nmea",
    }
    return recording.from_nmea(log, **defaults | options)


# A cold start, as a receiver at rest goes through one: no time, then a time and no fix, then a
# fix whose speed is a few tenths of a knot.
COLD_START = (
    "GPRMC,,V,,,,,,,,,,N",
    "GPGGA,,,,,,0,00,99.99,,,,,,",
    "GPRMC,,V,,,,,,,,,,N",
    "GPGGA,,,,,,0,00,99.99,,,,,,",
    "GPRMC,120002.00,V,,,,,,,191126,,,N",
    "GPGGA,120002.00,,,,,0,03,4.10,,,,,,",
    "GPRMC,120003.00,A,5213.78060,N,02100.73400,E,0.312,,191126,,,A",
    "GPVTG,,T,,M,0.312,N,0.578,K,A",
    "GPGGA,120003.00,5213.78060,N,02100.73400,E,1,05,2.10,110.0,M,34.0,M,,",
    "GPGSA,A,3,01,11,31,14,22,,,,,,,,3.1,2.1,2.3",
    "GPRMC,120004.00,A,5213.78071,N,02100.73388,E,0.271,,191126,,,A",
    "GPGGA,120004.00,5213.78071,N,02100.73388,E,1,06,1.90,109.6,M,34.0,M,,",
)


def test_a_log_is_an_epoch_an_event_a_period_apart() -> None:
    converted = from_nmea(nmea_log(*COLD_START))
    lines = [sentence(body).encode() + b"\r\n" for body in COLD_START]
    # A repeated RMC starts an epoch also without a time; an RMC and a GGA of one time are one,
    # with the sentences without a time after them.
    assert converted.events == tuple(
        Event(t, "in", b"".join(lines[a:b]), recording.EMULATED)
        for t, (a, b) in zip(
            range(1_000_000_000, 6_000_000_000, 1_000_000_000),
            [(0, 2), (2, 4), (4, 6), (6, 10), (10, 12)],
            strict=True,
        )
    )
    header = converted.header
    assert (header.board, header.interface, header.source, header.clock, header.end) == (
        "esp32-devkitc",
        "uart.gnss",
        "real",
        "emulated-fifo-write",
        "hold",
    )
    # The first RMC with a date gives `created`.
    assert header.created == "2026-11-19T12:00:02Z"
    assert (header.app_sha256, dict(header.tools)) == (None, {})
    assert dict(header.extra) == {
        "converted": {
            "from": "nmea",
            "baud": 9600,
            "period_ns": 1_000_000_000,
            "start_ns": 1_000_000_000,
        }
    }
    assert recording.loads(recording.dumps(converted)) == converted


def test_what_comes_before_the_first_rmc_or_gga_is_an_epoch_of_its_own() -> None:
    log = nmea_log("GPGSV,1,1,00", "GPGLL,,,,,120000.00,V,N", "GPRMC,120001.00,V,,,,,,,,,,N")
    converted = from_nmea(log, start_ns=0)
    assert [(event.t, event.data.count(b"$")) for event in converted.events] == [
        (0, 2),
        (1_000_000_000, 1),
    ]


def test_a_log_takes_lf_line_endings_comments_and_blank_lines_and_sends_crlf() -> None:
    log = b"# a receiver on a desk\n\n" + nmea_log(
        "GPRMC,120000.00,V,,,,,,,,,,N", "GPRMC,120001.00,V,,,,,,,,,,N", ending="\n"
    )
    converted = from_nmea(log)
    assert [event.data for event in converted.events] == [
        sentence("GPRMC,120000.00,V,,,,,,,,,,N").encode() + b"\r\n",
        sentence("GPRMC,120001.00,V,,,,,,,,,,N").encode() + b"\r\n",
    ]
    # A time and no date: `created` is the time of the conversion.
    assert recording._utc(converted.header.created)


def test_an_rmc_and_a_gga_of_one_time_are_one_epoch_however_many_decimals_they_give() -> None:
    log = nmea_log(
        "GPGGA,120000.000,,,,,0,00,,,,,,,",
        "GPRMC,120000.00,V,,,,,,,,,,N",
        "GPGGA,120001,,,,,0,00,,,,,,,",
        "GPRMC,120001.0,V,,,,,,,,,,N",
    )
    assert [(event.t, event.data.count(b"$")) for event in from_nmea(log).events] == [
        (1_000_000_000, 2),
        (2_000_000_000, 2),
    ]


def test_a_proprietary_sentence_is_no_rmc_whatever_its_address_ends_with() -> None:
    # Garmin's PGRMC carries its configuration where an RMC has its time.
    log = nmea_log(
        "GPRMC,120000.00,V,,,,,,,,,,N",
        "PGRMC,A,218.8,100,,,,,,A,3,1,1,4,30",
        "GPRMC,120001.00,V,,,,,,,,,,N",
    )
    assert [event.data.count(b"$") for event in from_nmea(log).events] == [2, 1]


def test_a_byte_order_mark_and_spaces_after_a_sentence_are_no_part_of_it() -> None:
    first, second = (
        sentence("GPRMC,120000.00,V,,,,,,,,,,N"),
        sentence("GPRMC,120001.00,V,,,,,,,,,,N"),
    )
    log = recording.BOM + f"{first} \t\r\n{second}  \n".encode()
    assert [event.data for event in from_nmea(log).events] == [
        first.encode() + b"\r\n",
        second.encode() + b"\r\n",
    ]


def test_a_log_at_10_hz_needs_its_period_and_crosses_midnight() -> None:
    log = nmea_log(
        "GNRMC,235959.80,V,,,,,,,,,,N",
        "GNRMC,235959.90,V,,,,,,,,,,N",
        "GNRMC,000000.00,V,,,,,,,,,,N",
        "GNRMC,000000.10,V,,,,,,,,,,N",
    )
    converted = from_nmea(log, period_ns=100_000_000, baud=115200, end="loop")
    assert [event.t for event in converted.events] == [
        1_000_000_000,
        1_100_000_000,
        1_200_000_000,
        1_300_000_000,
    ]
    assert converted.header.end == "loop"
    with pytest.raises(RecordingError) as error:
        from_nmea(log)
    assert str(error.value) == (
        "log.nmea:2: time 235959.90 is 0.1 s after 235959.80 on line 1, not one period of 1 s:"
        " the log skips epochs, or the receiver sends at another rate"
    )


def test_epochs_without_a_time_count_between_two_with_one() -> None:
    log = nmea_log(
        "GPRMC,120000.00,A,5213.78060,N,02100.73400,E,0.1,,191126,,,A",
        "GPRMC,,V,,,,,,,,,,N",
        "GPRMC,120002.00,A,5213.78060,N,02100.73400,E,0.1,,191126,,,A",
    )
    assert [event.t for event in from_nmea(log).events] == [
        1_000_000_000,
        2_000_000_000,
        3_000_000_000,
    ]


TIMED = sentence("GPRMC,120000.00,V,,,,,,,,,,N")


@pytest.mark.parametrize(
    ("log", "message"),
    [
        (
            f"{TIMED}\r\nGPRMC,120001.00,V*00\r\n".encode(),
            "log.nmea:2: 'GPRMC,120001.00,V*00' is not an NMEA sentence: a sentence starts with $",
        ),
        (
            f"{TIMED}\r\n$GPRMC,120001.00,\xb0V*00\r\n".encode("latin-1"),
            "log.nmea:2: '$GPRMC,120001.00,\\xb0V*00' is not an NMEA sentence: a sentence is"
            " printable ASCII",
        ),
        (
            b"\x00\xb5b\x01\x07\x5c\x00\r\n",
            "log.nmea:1: '\\x00\\xb5b\\x01\\x07\\\\\\x00' is not an NMEA sentence: a"
            " sentence starts with $",
        ),
        (
            f"{TIMED}\r\n$GPRMC,120001.00,V\r\n".encode(),
            "log.nmea:2: '$GPRMC,120001.00,V' has no checksum: a sentence ends with * and two hex"
            " digits",
        ),
        (
            b"$GPRMC,120000.00,V,,,,,,,,,,N*00\r\n",
            "log.nmea:1: '$GPRMC,120000.00,V,,,,,,,,,,N*00': checksum 00, and the sentence's is 7E",
        ),
        (
            nmea_log("GPGGA,12:00:00,,,,,0,00,,,,,,,"),
            "log.nmea:1: time '12:00:00' is not hhmmss or hhmmss.ss",
        ),
        (
            nmea_log("GPRMC,120000.00,V,,,,,,,,,,N", "GPRMC,115959.00,V,,,,,,,,,,N"),
            "log.nmea:2: time 115959.00 is not after 120000.00, the time on line 1: the log goes"
            " back",
        ),
        (
            nmea_log("GPRMC,120000.00,V,,,,,,,,,,N", "GPRMC,120000.00,V,,,,,,,,,,N"),
            "log.nmea:2: time 120000.00 is not after 120000.00, the time on line 1: the log goes"
            " back",
        ),
        (
            nmea_log("GPRMC,120000.00,V,,,,,,,,,,N", "GPRMC,120005.00,V,,,,,,,,,,N"),
            "log.nmea:2: time 120005.00 is 5 s after 120000.00 on line 1, not one period of 1 s:"
            " the log skips epochs, or the receiver sends at another rate",
        ),
        (
            nmea_log(
                "GPRMC,120000.00,V,,,,,,,,,,N",
                "GPRMC,,V,,,,,,,,,,N",
                "GPRMC,120001.00,V,,,,,,,,,,N",
            ),
            "log.nmea:3: time 120001.00 is 1 s after 120000.00 on line 1, not 2 periods of 1 s:"
            " the log skips epochs, or the receiver sends at another rate",
        ),
        (
            nmea_log("GPRMC,120000.00,V,,,,,,,,,,N", *["GPGSV,4,1,16" + ",01,02,003,04" * 4] * 20),
            "log.nmea:1: the epoch from this line is 1434 bytes, 1.49375 s at 9600 baud,"
            " longer than its period of 1 s",
        ),
        (
            nmea_log("GPRMC", "GPRMC,120000.00,V,,,,,,,,,,N"),
            "log.nmea:1: GPRMC has no time field: an RMC or a GGA gives one, empty when the"
            " receiver has no time",
        ),
        (
            nmea_log("GPGSV,1,1,00"),
            "log.nmea: no RMC or GGA sentence: the epochs cannot be told apart",
        ),
        (b"# nothing\r\n\r\n", "log.nmea: no NMEA sentence: nothing to replay"),
    ],
)
def test_a_malformed_log_is_refused_with_its_line(log: bytes, message: str) -> None:
    with pytest.raises(RecordingError) as error:
        from_nmea(log)
    assert str(error.value) == message


def test_read_nmea_names_the_file(tmp_path: Path) -> None:
    path = tmp_path / "log.nmea"
    path.write_bytes(nmea_log("GPRMC,120000.00,V,,,,,,,,,,N") + b"$GPGSV\r\n")
    with pytest.raises(RecordingError) as error:
        recording.read_nmea(path, board="esp32-devkitc", interface="uart.gnss", baud=9600)
    assert str(error.value) == (
        f"{path}:2: '$GPGSV' has no checksum: a sentence ends with * and two hex digits"
    )
    with pytest.raises(RecordingError) as error:
        recording.read_nmea(tmp_path / "none", board="b", interface="uart.gnss", baud=9600)
    assert str(error.value) == (
        f"{tmp_path / 'none'}: [Errno 2] No such file or directory: '{tmp_path / 'none'}'"
    )


def test_the_committed_log_converts_to_the_committed_recording() -> None:
    converted = recording.read_nmea(
        NEO_M8N.with_suffix(".nmea"), board="esp32-devkitc", interface="uart.gnss", baud=9600
    )
    assert recording.dumps(converted) == NEO_M8N.with_suffix(".jsonl").read_text()
    # The log came in on the end of an epoch: four sentences on their own at 1 s, then an epoch a
    # second, the last of them cut short.
    events = converted.events
    assert [event.t for event in events] == [i * 1_000_000_000 for i in range(1, 25)]
    assert events[0].data.startswith(b"$GLGSV,3,1,10,") and events[0].data.count(b"$") == 4
    assert all(event.data.startswith(b"$GNRMC,1719") for event in events[1:])
    assert converted.header.created == "2015-03-18T17:19:26Z"
