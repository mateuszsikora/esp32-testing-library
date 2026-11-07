"""A model the test writes, at the far end of a UART esp32tl ships no model for:
`board.uart(role).model(source)`, first on `Uart` alone, then with modem_at, unmodified, on a
profile whose UART1 is `uart.aux`. The firmware writes its AT commands there and reads the
answers, as it would from any part that speaks AT."""

from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_modem_model import route

from esp32tl import Board, recording
from esp32tl.boards.capabilities import Request
from esp32tl.interfaces import SourceError
from esp32tl.interfaces.gnss import Gnss
from esp32tl.interfaces.uart import Uart
from esp32tl.link.boardlink import BoardLink, Output, Wire
from esp32tl.link.protocol import RunnerHello, Sync, UartRx, UartTx, Wait
from esp32tl.report import RunLog
from esp32tl.units import ms, seconds

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "tests" / "boards" / "aux-uart.toml"
AUX = Wire(tx=26, rx=27, baud=115200)
HELLO = RunnerHello(1, "esp32", recording.EMULATED, "8c652151dd5a521249a785ca147f20a9f8eda68f")
# How long the part takes to answer a command, from the end of its line.
ANSWER = ms(1).ns


class AtResponder:
    """A part that answers AT: `OK` to every command line, and a signal to `AT+CSQ`, `ANSWER`
    after the line's CR LF; nothing while it has no power. `lines` is every line it heard."""

    def __init__(self) -> None:
        self.lines: list[bytes] = []
        self.supplied: list[bool] = []
        self._on = True
        self._heard = b""

    def receive(self, t: int, data: bytes) -> Iterable[Output]:
        if not self._on:
            return []
        self._heard += data
        answers = []
        while b"\r\n" in self._heard:
            line, _, self._heard = self._heard.partition(b"\r\n")
            self.lines.append(line)
            signal = b"\r\n+CSQ: 21,99\r\n" if line == b"AT+CSQ" else b""
            answers.append((t + ANSWER, signal + b"\r\nOK\r\n"))
        return answers

    def poll(self, now: int) -> Iterable[Output]:
        return []

    def quiet_until(self) -> int | None:
        return None

    def latency(self) -> int | None:
        return ANSWER

    def supply(self, on: bool) -> None:
        self.supplied.append(on)
        self._on = on
        self._heard = b""


class OnlyReceives:
    def receive(self, t: int, data: bytes) -> Iterable[Output]:
        return []


class Silent:
    """Hears everything and says nothing; it is on no supply."""

    def receive(self, t: int, data: bytes) -> Iterable[Output]:
        return []

    def poll(self, now: int) -> Iterable[Output]:
        return []

    def quiet_until(self) -> int | None:
        return None

    def latency(self) -> int | None:
        return None


class NotCallable(Silent):
    poll = 3  # type: ignore[assignment]


class Wrapped:
    """A model behind a proxy, as a wrapper that logs every call would put it."""

    def __init__(self, inner: object) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


def aux(tmp_path: Path, asked: list[Request] | None = None) -> tuple[Uart, RunLog]:
    log = RunLog()
    uart = Uart(
        "uart.aux",
        AUX,
        clock=recording.EMULATED,
        directory=tmp_path,
        asked=(asked if asked is not None else []).append,
        log=log,
    )
    return uart, log


def test_a_model_is_the_route_s_source_and_answers_through_the_link(tmp_path: Path) -> None:
    asked: list[Request] = []
    uart, log = aux(tmp_path, asked)
    model = AtResponder()
    assert uart.model(model) is model
    assert asked == [Request("uart.aux", source="model")]
    assert (uart.source, uart.source_kind, uart.source_detail) == (model, "model", "AtResponder")
    assert uart.recorded_source() == "model:AtResponder"
    assert log.lines()[1:] == ["  0.000000000 s  test  uart.aux.model(AtResponder)"]
    link = BoardLink("aux-uart", [uart])
    link.receive(HELLO)
    assert link.receive(Sync(0)) == [Wait(0, None, ANSWER)]
    assert link.receive(UartTx(1, 500, 1, (26,), 115200, b"AT\r\n")) == []
    assert link.receive(Sync(500 + ANSWER)) == [
        UartRx(500 + ANSWER, 27, 115200, b"\r\nOK\r\n"),
        Wait(1, None, ANSWER),
    ]
    assert link.error is None
    assert model.lines == [b"AT"]


def test_a_supplied_model_loses_its_power_with_the_board(tmp_path: Path) -> None:
    uart, _ = aux(tmp_path)
    model = uart.model(AtResponder())
    uart.supply(False)
    assert list(uart.receive(0, "out", b"AT\r\n")) == []
    uart.supply(True)
    assert list(uart.receive(1, "out", b"AT\r\n")) == [(1 + ANSWER, b"\r\nOK\r\n")]
    assert model.supplied == [False, True]


def test_what_is_no_source_is_refused_and_leaves_the_route_as_it_was(tmp_path: Path) -> None:
    asked: list[Request] = []
    uart, log = aux(tmp_path, asked)
    with pytest.raises(
        TypeError,
        match=r"^uart\.aux\.model: <.*OnlyReceives.*> is not a Source: it has no poll,"
        r" quiet_until, latency$",
    ):
        uart.model(OnlyReceives())  # type: ignore[type-var]
    with pytest.raises(
        TypeError, match=r"^uart\.aux\.model: <.*NotCallable.*> is not a Source: it has no poll$"
    ):
        uart.model(NotCallable())  # type: ignore[type-var]
    assert uart.source is None and asked == [] and log.lines()[1:] == []


def test_a_model_behind_a_proxy_is_a_source(tmp_path: Path) -> None:
    uart, _ = aux(tmp_path)
    model = AtResponder()
    uart.model(Wrapped(model))  # type: ignore[type-var]
    assert list(uart.receive(0, "out", b"AT\r\n")) == [(ANSWER, b"\r\nOK\r\n")]
    assert uart.source_detail == "Wrapped" and model.lines == [b"AT"]


def test_a_model_on_no_supply_keeps_going_through_a_power_cycle(tmp_path: Path) -> None:
    uart, _ = aux(tmp_path)
    model = uart.model(Silent())
    uart.supply(False)
    uart.supply(True)
    assert uart.source is model


def test_one_source_per_test_whichever_comes_first(tmp_path: Path) -> None:
    uart, _ = aux(tmp_path)
    uart.model(AtResponder())
    with pytest.raises(SourceError, match=r"^uart\.aux has its source already: one per test$"):
        uart.model(AtResponder())
    with pytest.raises(SourceError, match=r"^uart\.aux has its source already: one per test$"):
        uart.real("/dev/not-opened")
    assert uart.source_detail == "AtResponder"


def test_a_model_the_backend_refuses_is_not_given(tmp_path: Path) -> None:
    def refuse(request: Request) -> None:
        raise SourceError(f"{request.interface}: the board is on already")

    uart = Uart("uart.aux", AUX, clock=recording.EMULATED, directory=tmp_path, asked=refuse)
    with pytest.raises(SourceError, match=r"^uart\.aux: the board is on already$"):
        uart.model(AtResponder())
    assert uart.source is None


def test_a_model_of_the_test_s_on_the_gnss_route_is_no_receiver(tmp_path: Path) -> None:
    gnss = Gnss(
        "uart.gnss",
        Wire(tx=4, rx=5, baud=9600),
        clock=recording.EMULATED,
        directory=tmp_path,
        now=lambda: 0,
        world=lambda: datetime(2026, 10, 29, 18, 0, 0, tzinfo=UTC),
    )
    gnss.model(AtResponder())
    assert gnss.recorded_source() == "model:AtResponder"
    with pytest.raises(
        SourceError,
        match=r"^uart\.gnss has a model of the test's: it has no receiver to describe;"
        r" one source per test$",
    ):
        gnss.lose_fix()


def test_a_model_of_the_test_s_on_the_modem_route_is_no_modem(tmp_path: Path) -> None:
    modem, lines, _ = route(tmp_path, [0])
    modem.model(AtResponder())
    assert modem.recorded_source() == "model:AtResponder"
    assert modem.command_report() == []
    refused = (
        r"^uart\.modem has a model of the test's: it has no modem to describe; one source per test$"
    )
    with pytest.raises(SourceError, match=refused):
        modem.signal(dbm=-73)
    with pytest.raises(SourceError, match=refused):
        modem.network()
    with pytest.raises(SourceError, match=refused):
        assert modem.powered
    assert lines.events() == []


@pytest.mark.app_path("../firmware/modem_at")
@pytest.mark.board_profile(path="boards/aux-uart.toml")
def test_the_firmware_talks_to_the_test_s_model_on_its_own_uart(board: Board) -> None:
    part = board.uart("aux").model(AtResponder())
    board.power_on()
    board.console.expect_exact("modem_at: powered on RI=0")
    board.console.expect_exact("modem_at: dialogue complete", timeout=seconds(5))
    # The escape the firmware sends before its first command has no CR LF: it is the AT's.
    assert part.lines[:4] == [b"+++AT", b"ATE0", b"AT+CPIN?", b"AT+CREG?"]
    assert b"AT+UNIMPLEMENTED" in part.lines
    board.console.expect_exact("modem_at: AT+CSQ =>", timeout=seconds(2))
    board.console.expect_exact("+CSQ: 21,99", timeout=seconds(1))
    assert "  uart.aux: the model of AtResponder; " in board.report()
    routes = board.properties()["routes"]
    assert isinstance(routes, dict)
    assert routes["uart.aux"]["source"] == "model"
    assert routes["uart.aux"]["bytes_out"] > 0 and routes["uart.aux"]["bytes_in"] > 0


@pytest.mark.app_path("../firmware/modem_at")
@pytest.mark.board_profile(path="boards/aux-uart.toml")
def test_the_test_s_model_loses_its_power_with_the_board(board: Board) -> None:
    part = board.uart("aux").model(AtResponder())
    board.power_on()
    board.console.expect_exact("modem_at: dialogue complete", timeout=seconds(5))
    board.power_off()
    assert part.supplied == [False]
    board.wait(seconds(1))
    heard = len(part.lines)
    board.power_on()
    assert part.supplied == [False, True]
    board.console.expect_exact("modem_at: dialogue complete", timeout=seconds(5))
    assert part.lines[heard : heard + 2] == [b"+++AT", b"ATE0"]


@pytest.mark.app_path("../firmware/modem_at")
@pytest.mark.board_profile(path="boards/aux-uart.toml")
def test_a_model_comes_before_power_on(board: Board) -> None:
    board.power_on()
    with pytest.raises(
        SourceError,
        match=r"^uart\.aux: the board is on already: give an interface its source before"
        r" board\.power_on\(\)$",
    ):
        board.uart("aux").model(AtResponder())
    assert board.uart("aux").source is None


def test_a_tap_records_the_test_s_model_by_its_class(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(f"""
        import pytest
        from esp32tl.units import seconds
        pytestmark = [pytest.mark.app_path({str(ROOT / "firmware" / "modem_at")!r}),
                      pytest.mark.board_profile(path={str(PROFILE)!r})]

        class Ok:
            def receive(self, t, data):
                return [(t + 1_000_000, b"\\r\\nOK\\r\\n")] if data.endswith(b"\\n") else []
            def poll(self, now):
                return []
            def quiet_until(self):
                return None
            def latency(self):
                return 1_000_000

        def test_dialogue(board):
            board.uart("aux").model(Ok())
            board.uart("aux").tap("aux.jsonl")
            board.power_on()
            board.console.expect_exact("modem_at: dialogue complete", timeout=seconds(5))
    """)
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    made = recording.read(pytester.path / "aux.jsonl")
    assert (made.header.source, made.header.interface) == ("model:Ok", "uart.aux")
    assert {event.dir for event in made.events} == {"in", "out"}
