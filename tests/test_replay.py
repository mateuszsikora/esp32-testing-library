"""Replay of a stream recording, `esp32tl/sources/replay.py`, and the `Uart` interface that puts
it on the link, `esp32tl/interfaces/uart.py`, without an emulator: the link's turns are fed in
and what the harness answers is checked."""

import json
from pathlib import Path
from typing import Any

import pytest

from esp32tl import recording
from esp32tl.boards.capabilities import Request
from esp32tl.interfaces import SourceError
from esp32tl.interfaces.uart import Uart
from esp32tl.interfaces.wifi import Wifi
from esp32tl.link.boardlink import BoardLink, Wire
from esp32tl.link.protocol import RunnerHello, Sync, UartRx, UartTx, Wait
from esp32tl.sources.replay import Replay, ReplayError

ROOT = Path(__file__).resolve().parent.parent
HELLO = RunnerHello(1, "esp32", "emulated-fifo-write", "afcfba65da0da30a35fb0a7c381854c62b92e338")
GNSS = Wire(tx=4, rx=5, baud=9600)
CLOCK = "emulated-fifo-write"
S = 1_000_000_000

HEADER = {
    "esp32tl_recording": 1,
    "board": "esp32-devkitc",
    "interface": "uart.gnss",
    "source": "real",
    "clock": CLOCK,
    "created": "2026-10-29T18:00:00Z",
}


def stream(*lines: dict[str, Any], **header: Any) -> recording.Recording:
    text = "".join(json.dumps(line) + "\n" for line in (HEADER | header, *lines))
    return recording.loads(text)


def event(t: int, data: str = "JEdQ", direction: str = "in") -> dict[str, Any]:
    return {"t": t, "dir": direction, "data": data}


def test_each_event_goes_out_at_its_time_and_bounds_the_turn_before() -> None:
    replay = Replay(stream(event(0), event(S), event(S), event(3 * S)), "uart.gnss", CLOCK)
    assert replay.latency() is None
    assert list(replay.poll(0)) == [(0, b"$GP")]
    assert replay.quiet_until() == S
    # A turn before the next event's time gives nothing.
    assert list(replay.poll(S - 1)) == []
    assert replay.quiet_until() == S
    # Two events of one time go out together.
    assert list(replay.poll(S)) == [(S, b"$GP"), (S, b"$GP")]
    assert replay.quiet_until() == 3 * S
    assert list(replay.poll(3 * S)) == [(3 * S, b"$GP")]
    # After the last event: silence (end: hold).
    assert replay.quiet_until() is None
    assert list(replay.poll(10 * S)) == []
    assert replay.sent == 4


def test_a_stream_does_not_listen() -> None:
    replay = Replay(stream(event(S)), "uart.gnss", CLOCK)
    assert list(replay.receive(10, b"$PMTK220,1000*1F\r\n")) == []
    assert replay.quiet_until() == S


@pytest.mark.parametrize(
    ("lines", "header", "message"),
    [
        (
            [event(0)],
            {"interface": "uart.modem"},
            "r.jsonl is a recording of uart.modem, not of uart.gnss",
        ),
        (
            [event(0)],
            {"clock": "wall"},
            "r.jsonl has times on the wall clock, and uart.gnss runs on the emulated-fifo-write"
            " clock: replaying it would compare times across clocks",
        ),
        (
            [event(0), {"t": 5, "clock": "wall"}, event(6)],
            {},
            "r.jsonl has times on the emulated-fifo-write and wall clock, and uart.gnss runs on"
            " the emulated-fifo-write clock",
        ),
        (
            # A clock left and come back to, with no event on the other.
            [event(0), {"t": 5, "clock": "wall"}, {"t": 6, "clock": CLOCK}, event(7)],
            {},
            "r.jsonl has times on the emulated-fifo-write and wall clock",
        ),
        (
            [event(0), event(7, direction="out")],
            {},
            "r.jsonl: the recording ends with an incomplete request",
        ),
        (
            [event(0)],
            {"end": "loop"},
            "r.jsonl: a looping recording with unprompted events must have positive duration",
        ),
    ],
)
def test_what_a_stream_replay_cannot_replay_is_refused(
    lines: list[dict[str, Any]], header: dict[str, Any], message: str
) -> None:
    with pytest.raises(ReplayError) as raised:
        Replay(stream(*lines, **header), "uart.gnss", CLOCK, name="r.jsonl")
    assert str(raised.value).startswith(message)


def uart(tmp_path: Path) -> tuple[Uart, list[Request]]:
    """A `Uart` on the GNSS route, and what it was asked for."""
    asked: list[Request] = []
    interface = Uart("uart.gnss", GNSS, clock=CLOCK, directory=tmp_path, asked=asked.append)
    return interface, asked


def write(path: Path, *lines: dict[str, Any]) -> None:
    path.write_text("".join(json.dumps(line) + "\n" for line in (HEADER, *lines)))


def test_a_replay_through_the_link_sends_each_event_in_the_turn_at_its_time(
    tmp_path: Path,
) -> None:
    write(tmp_path / "r.jsonl", event(S), event(2 * S, "JE5N"))
    gnss, given = uart(tmp_path)
    link = BoardLink("esp32-devkitc", [gnss])
    link.receive(HELLO)
    # Relative to the directory it was given, the test file's.
    replay = gnss.replay("r.jsonl")
    assert given == [Request("uart.gnss", source="replay")]
    assert gnss.source_kind == "replay"
    assert link.receive(Sync(0)) == [Wait(0, S, None)]
    # What the firmware transmits is heard and answered with nothing.
    assert link.receive(UartTx(1, S // 2, 1, (4,), 9600, b"$PMTK\r\n")) == []
    assert link.receive(Sync(S)) == [UartRx(S, 5, 9600, b"$GP"), Wait(1, 2 * S, None)]
    assert link.receive(Sync(2 * S)) == [UartRx(2 * S, 5, 9600, b"$NM"), Wait(1, None, None)]
    assert replay.sent == 2
    assert link.error is None


def test_a_replay_the_backend_refuses_gives_no_source(tmp_path: Path) -> None:
    def refuse(request: Request) -> None:
        raise SourceError(f"{request.interface}: no {request.source} here")

    write(tmp_path / "r.jsonl", event(S))
    gnss = Uart("uart.gnss", GNSS, clock=CLOCK, directory=tmp_path, asked=refuse)
    with pytest.raises(SourceError, match=r"^uart\.gnss: no replay here$"):
        gnss.replay("r.jsonl")
    assert gnss.source is None and gnss.source_kind is None


def test_a_uart_takes_one_source(tmp_path: Path) -> None:
    write(tmp_path / "r.jsonl", event(S))
    gnss, given = uart(tmp_path)
    gnss.replay(tmp_path / "r.jsonl")
    with pytest.raises(SourceError, match=r"^uart\.gnss has its source already: one per test$"):
        gnss.replay("r.jsonl")
    # Refused before the backend is asked again.
    assert given == [Request("uart.gnss", source="replay")]


def test_a_recording_that_cannot_be_replayed_gives_no_source(tmp_path: Path) -> None:
    gnss, _ = uart(tmp_path)
    with pytest.raises(recording.RecordingError, match=r"none\.jsonl: \[Errno 2\]"):
        gnss.replay("none.jsonl")
    (tmp_path / "wall.jsonl").write_text(json.dumps(HEADER | {"clock": "wall"}) + "\n")
    with pytest.raises(ReplayError, match=r"^wall\.jsonl has times on the wall clock"):
        gnss.replay("wall.jsonl")
    assert gnss.source is None


def test_a_uart_without_a_source_hears_everything_and_says_nothing(tmp_path: Path) -> None:
    gnss, _ = uart(tmp_path)
    assert list(gnss.receive(0, "out", b"x")) == []
    assert list(gnss.poll(0)) == []
    assert (gnss.quiet_until(), gnss.latency()) == (None, None)


def test_the_committed_recording_replays_one_sentence_a_second() -> None:
    replay = Replay(
        recording.read(ROOT / "recordings" / "gnss-generated-1.jsonl"), "uart.gnss", CLOCK
    )
    # The turns the runner takes: 0, then each quiet_until.
    times = [t for t, _ in replay.poll(0)]
    while (bound := replay.quiet_until()) is not None:
        times += [t for t, _ in replay.poll(bound)]
    assert times == [(1 + i) * S for i in range(60)]


@pytest.mark.parametrize("binary", [False, True])
def test_gnss_replay_answers_configuration_commands_through_the_link(
    tmp_path: Path,
    binary: bool,
) -> None:
    from dataclasses import replace

    from esp32tl.recording import Event

    command = bytes.fromhex("b56206010200010a1447") if binary else b"$PMTK220,1000*1F\r\n"
    answer = bytes.fromhex("b5620501020006010f38") if binary else b"$PMTK001,220,3*30\r\n"
    recording.write(
        tmp_path / "commands.jsonl",
        recording.Recording(
            replace(stream().header, end="hold"),
            (Event(10, "out", command, CLOCK), Event(30, "in", answer, CLOCK)),
        ),
    )
    gnss, _ = uart(tmp_path)
    replay = gnss.replay("commands.jsonl")
    link = BoardLink("esp32-devkitc", [gnss])
    link.receive(HELLO)
    assert link.receive(Sync(0)) == [Wait(0, None, 20)]
    # Fragmentation on the live wire differs from the recording.
    link.receive(UartTx(1, 100, 1, (4,), 9600, command[:3]))
    assert link.receive(Sync(120)) == [Wait(1, None, 20)]
    link.receive(UartTx(2, 130, 1, (4,), 9600, command[3:]))
    assert link.receive(Sync(140)) == [Wait(2, 150, 20)]
    assert link.receive(Sync(150)) == [UartRx(150, 5, 9600, answer), Wait(2, None, 20)]
    assert replay.sent == 1
    assert link.error is None


def test_unseen_requests_are_in_the_backend_report_and_json_properties(tmp_path: Path) -> None:
    from esp32tl.backends.esp32sim import Esp32Sim
    from esp32tl.matching import AT
    from esp32tl.report import RunLog

    write(tmp_path / "r.jsonl", event(S))
    gnss, _ = uart(tmp_path)
    gnss.replay("r.jsonl", protocol=AT)
    backend = Esp32Sim.__new__(Esp32Sim)
    backend._console = Uart("console", Wire(1, 3, 115200), clock=CLOCK, directory=tmp_path)
    backend._uarts = {"uart.gnss": gnss}
    backend._sd_card = None
    backend._wifi = Wifi(now=lambda: 0)
    backend.log = RunLog(entries=1)
    gnss.log = backend.log
    backend.link = BoardLink("esp32-devkitc", [gnss])
    backend.link.receive(HELLO)
    backend.link.receive(Sync(0))
    routes = backend.properties()["routes"]
    assert isinstance(routes, dict)
    assert "unseen_requests" not in routes["uart.gnss"]
    assert "unseen_request_events" not in routes["uart.gnss"]
    backend.link.receive(UartTx(1, 90, 1, (4,), 9600, b" at +unknown "))
    backend.link.receive(UartTx(2, 100, 1, (4,), 9600, b"\rAT+UNKNOWN\r"))
    assert backend.link.receive(Sync(100)) == [
        UartRx(100, 5, 9600, b"\r\nERROR\r\n"),
        UartRx(100, 5, 9600, b"\r\nERROR\r\n"),
        Wait(2, S, 0),
    ]
    long_request = b"AT+" + b"x" * 110 + b"\x00\x1b\r"
    backend.link.receive(UartTx(3, 120, 1, (4,), 9600, long_request))
    backend.link.receive(Sync(120))
    # Raw requests survive log rollover, normalisation and the summary's abbreviation.
    assert "UNKNOWN" not in "\n".join(backend.log.lines())
    report = backend.report()
    assert "unseen requests: 3\n      2 x AT+UNKNOWN" in report
    assert "    unseen request events:" in report
    assert report.split("    unseen request events:\n")[1].splitlines() == [
        "      0.000000100 s   at +unknown \\r",
        "      0.000000100 s  AT+UNKNOWN\\r",
        "      0.000000120 s  AT+" + "x" * 110 + "\\x00\\x1b\\r",
    ]
    routes = backend.properties()["routes"]
    assert isinstance(routes, dict)
    assert routes["uart.gnss"]["unseen_requests"] == [
        {"request_hex": b"AT+UNKNOWN".hex(), "count": 2},
        {"request_hex": (b"AT+" + b"X" * 110 + b"\x00\x1b").hex(), "count": 1},
    ]
    assert routes["uart.gnss"]["unseen_request_events"] == [
        {"t_ns": 100, "request_hex": b" at +unknown \r".hex()},
        {"t_ns": 100, "request_hex": b"AT+UNKNOWN\r".hex()},
        {"t_ns": 120, "request_hex": long_request.hex()},
    ]
    assert backend.link.error is None
