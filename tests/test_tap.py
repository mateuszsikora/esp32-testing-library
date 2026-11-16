"""Taps, `esp32tl/tap.py`, and `Uart.tap`, without an emulator: the link's turns are fed in, and
the recording the tap makes is checked."""

import hashlib
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

import pytest

import esp32tl
from esp32tl import recording
from esp32tl.boards.capabilities import Request
from esp32tl.interfaces import SourceError
from esp32tl.interfaces.uart import Uart
from esp32tl.link.boardlink import BoardLink, Output, Wire
from esp32tl.link.protocol import RunnerHello, Sync, UartRx, UartTx, Wait
from esp32tl.report import RunLog
from esp32tl.sources.replay import ReplayError
from esp32tl.tap import Tap, sha256, utc_time

HELLO = RunnerHello(2, "esp32", "emulated-fifo-write", "357b0e542dfe97d4c62ec4a15c01c4969164448c")
GNSS = Wire(tx=4, rx=5, baud=9600)
CLOCK = "emulated-fifo-write"
S = 1_000_000_000
CREATED = datetime(2026, 10, 29, 18, 0, 0, tzinfo=UTC)


def test_a_tap_writes_its_events_in_time_order_with_the_header() -> None:
    tap = Tap("uart.modem", Path("modem.jsonl"))
    # An answer stamped later than a request the firmware sends after it.
    tap.record(10, "out", b"AT\r")
    tap.record(50, "in", b"\r\nOK\r\n")
    tap.record(20, "out", b"ATI\r")
    tap.record(20, "in", b"\r\n")
    tap.record(30, "in", b"")
    made = tap.recording(
        board="lilygo-t-a7670g-r2",
        source="replay",
        clock=CLOCK,
        app_sha256="0" * 64,
        tools={"esp32sim": "8c65"},
        created=CREATED,
    )
    assert made.header == recording.Header(
        board="lilygo-t-a7670g-r2",
        interface="uart.modem",
        source="replay",
        clock=CLOCK,
        created="2026-10-29T18:00:00Z",
        app_sha256="0" * 64,
        tools={"esp32tl": esp32tl.__version__, "esp32sim": "8c65"},
    )
    # In time order, those of one time as recorded; no event for no bytes.
    assert [(e.t, e.dir, e.data) for e in made.events] == [
        (10, "out", b"AT\r"),
        (20, "out", b"ATI\r"),
        (20, "in", b"\r\n"),
        (50, "in", b"\r\nOK\r\n"),
    ]
    assert recording.loads(recording.dumps(made)) == made


def test_created_is_utc_to_the_second_and_the_app_hash_is_the_file_s(tmp_path: Path) -> None:
    assert utc_time(datetime(2026, 1, 1, 13, 0, 0, 500, tzinfo=UTC)) == "2026-01-01T13:00:00Z"
    (tmp_path / "app.bin").write_bytes(b"\xe9" * 70000)
    assert sha256(tmp_path / "app.bin") == hashlib.sha256(b"\xe9" * 70000).hexdigest()


class Answering:
    """A source that answers every byte it hears with `ok`, `latency` later, and sends `fix` at
    `S` unprompted."""

    def __init__(self) -> None:
        self.sent = False

    def receive(self, t: int, data: bytes) -> Iterable[Output]:
        # A generator, as a source may return one: the UART reads it once.
        yield (t + 1000, b"ok")

    def poll(self, now: int) -> Iterable[Output]:
        if now >= S and not self.sent:
            self.sent = True
            return [(S, b"fix")]
        return []

    def quiet_until(self) -> int | None:
        return None if self.sent else S

    def latency(self) -> int | None:
        return 1000


def test_a_tapped_uart_records_both_ways_whatever_its_source(tmp_path: Path) -> None:
    log = RunLog()
    gnss = Uart("uart.gnss", GNSS, clock=CLOCK, directory=tmp_path, log=log)
    tap = gnss.tap("tap.jsonl")
    assert tap.path == tmp_path / "tap.jsonl"
    gnss.source = Answering()
    link = BoardLink("esp32-devkitc", [gnss])
    link.receive(HELLO)
    assert link.receive(Sync(0)) == [Wait(0, S, 1000)]
    assert link.receive(UartTx(1, 500, 1, (4,), 9600, b"$PMTK\r\n")) == []
    # What the source answered goes out, and is what the tap recorded.
    assert link.receive(Sync(1500)) == [UartRx(1500, 5, 9600, b"ok"), Wait(1, S, 1000)]
    assert link.receive(Sync(S)) == [UartRx(S, 5, 9600, b"fix"), Wait(1, None, 1000)]
    assert link.error is None
    assert tap.events == [(500, "out", b"$PMTK\r\n"), (1500, "in", b"ok"), (S, "in", b"fix")]
    assert log.traffic["uart.gnss"].bytes_in == 5
    assert log.traffic["uart.gnss"].bytes_out == 7


def test_a_uart_without_a_source_records_what_the_firmware_sent(tmp_path: Path) -> None:
    gnss = Uart("uart.gnss", GNSS, clock=CLOCK, directory=tmp_path)
    tap = gnss.tap(tmp_path / "tap.jsonl")
    assert list(gnss.receive(7, "out", b"x")) == []
    assert list(gnss.poll(7)) == []
    assert tap.events == [(7, "out", b"x")]


def test_a_tap_is_asked_of_the_backend_once_and_needs_its_directory(tmp_path: Path) -> None:
    asked: list[Request] = []
    gnss = Uart("uart.gnss", GNSS, clock=CLOCK, directory=tmp_path, asked=asked.append)
    with pytest.raises(FileNotFoundError, match=r"^uart\.gnss: cannot tap into none/t\.jsonl: "):
        gnss.tap("none/t.jsonl")
    assert gnss.tapped is None
    gnss.tap("t.jsonl")
    with pytest.raises(SourceError, match=r"^uart\.gnss is tapped already: one tap per test$"):
        gnss.tap("u.jsonl")
    assert asked == [Request("uart.gnss", tap=True)] * 2


def test_a_tap_the_backend_refuses_is_not_made(tmp_path: Path) -> None:
    def refuse(request: Request) -> None:
        raise SourceError(f"{request.interface} cannot be tapped")

    gnss = Uart("uart.gnss", GNSS, clock=CLOCK, directory=tmp_path, asked=refuse)
    with pytest.raises(SourceError, match=r"^uart\.gnss cannot be tapped$"):
        gnss.tap("t.jsonl")
    assert gnss.tapped is None


class Broken:
    """A source whose outputs the link refuses: a time that is a bool, and one before 0."""

    def receive(self, t: int, data: bytes) -> Iterable[Output]:
        return [(True, b"x"), (-1, b"y"), (t, "z")]  # type: ignore[list-item]

    def poll(self, now: int) -> Iterable[Output]:
        return []

    def quiet_until(self) -> int | None:
        return None

    def latency(self) -> int | None:
        return 0


def test_what_is_no_output_the_link_takes_is_not_recorded(tmp_path: Path) -> None:
    log = RunLog()
    gnss = Uart("uart.gnss", GNSS, clock=CLOCK, directory=tmp_path, log=log)
    tap = gnss.tap("tap.jsonl")
    gnss.source = Broken()
    assert len(list(gnss.receive(5, "out", b"a"))) == 3
    assert tap.events == [(5, "out", b"a")]
    # So the recording is one the format takes.
    tap.recording(board="esp32-devkitc", source="replay", clock=CLOCK)
    assert "uart.gnss" in log.traffic and log.traffic["uart.gnss"].bytes_in == 0


def test_a_tap_never_writes_over_a_file_the_run_reads_or_writes(tmp_path: Path) -> None:
    (tmp_path / "r.jsonl").write_text(
        json.dumps(
            {
                "esp32tl_recording": 1,
                "board": "esp32-devkitc",
                "interface": "uart.gnss",
                "source": "generated",
                "clock": CLOCK,
                "created": "2026-10-29T18:00:00Z",
            }
        )
        + "\n"
    )
    files: dict[Path, tuple[str, str]] = {}
    gnss = Uart("uart.gnss", GNSS, clock=CLOCK, directory=tmp_path, files=files)
    modem = Uart("uart.modem", Wire(26, 27, 115200), clock=CLOCK, directory=tmp_path, files=files)
    gnss.replay("r.jsonl")
    with pytest.raises(
        SourceError,
        match=r"^uart\.gnss: cannot tap into \./r\.jsonl: it is the replay of uart\.gnss$",
    ):
        gnss.tap("./r.jsonl")
    # One file on macOS's file system, and refused on any.
    with pytest.raises(
        SourceError, match=r"^uart\.gnss: cannot tap into R\.jsonl: it is the replay"
    ):
        gnss.tap("R.jsonl")
    modem.tap("t.jsonl")
    with pytest.raises(
        SourceError,
        match=r"^uart\.gnss: cannot tap into .*/T\.jsonl: it is the tap of uart\.modem$",
    ):
        gnss.tap(tmp_path / "T.jsonl")
    assert gnss.tapped is None
    with pytest.raises(
        SourceError, match=r"^uart\.modem: cannot replay t\.jsonl: it is the tap of uart\.modem$"
    ):
        modem.replay("t.jsonl")
    # Two replays of one file harm nothing: the replay itself says what is wrong with this one.
    with pytest.raises(
        ReplayError, match=r"^r\.jsonl is a recording of uart\.gnss, not of uart\.modem"
    ):
        modem.replay("r.jsonl")


def test_the_test_s_steps_on_a_uart_go_into_the_log_at_0(tmp_path: Path) -> None:
    (tmp_path / "r.jsonl").write_text(
        json.dumps(
            {
                "esp32tl_recording": 1,
                "board": "esp32-devkitc",
                "interface": "uart.gnss",
                "source": "generated",
                "clock": CLOCK,
                "created": "2026-10-29T18:00:00Z",
            }
        )
        + "\n"
    )
    log = RunLog()
    gnss = Uart("uart.gnss", GNSS, clock=CLOCK, directory=tmp_path, log=log)
    gnss.replay("r.jsonl")
    gnss.tap("t.jsonl")
    assert (gnss.source_kind, gnss.source_detail) == ("replay", "r.jsonl")
    assert log.lines()[1:] == [
        "  0.000000000 s  test  uart.gnss.replay('r.jsonl')",
        "  0.000000000 s  test  uart.gnss.tap('t.jsonl')",
    ]
