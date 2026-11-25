"""A source's own line rate, the firmware's rates told to a model, and frame errors a model takes
(issue #334): first on the link alone, then with firmware/uart_rate_switch, unmodified, which
switches a part on its UART1 from 9600 to 115200 baud with `AT+IPR` and finds it again after a
power loss has put the part back at 9600."""

from collections.abc import Iterable
from pathlib import Path

import pytest
from rate_switching import DEFAULT_BAUD, RateSwitchingModem, RateSwitchingPart

from esp32tl import Board, recording
from esp32tl.interfaces.uart import Configured, RateBound, Supplied, TakesFrameErrors, Uart
from esp32tl.link.boardlink import BoardLink, Interface, LinkError, Output, OutputAtRate, Wire
from esp32tl.link.protocol import RunnerHello, Stop, Sync, UartCfg, UartRx, UartTx, Wait, Warn
from esp32tl.report import RunLog
from esp32tl.sources.model import ReactiveModel, Timed, TimedAtRate, in_nanoseconds
from esp32tl.sources.model.modem import Modem
from esp32tl.units import Duration, ms, seconds

ROOT = Path(__file__).resolve().parent.parent
RATE_SWITCH = ROOT / "firmware" / "uart_rate_switch"
PROFILE = ROOT / "tests" / "boards" / "aux-uart-9600.toml"
AUX = Wire(tx=26, rx=27, baud=DEFAULT_BAUD)
HELLO = RunnerHello(2, "esp32", recording.EMULATED, "357b0e542dfe97d4c62ec4a15c01c4969164448c")
ANSWER = RateSwitchingPart.answer_after
FAST = 115200
# What the firmware's UART1 runs at when told 115200, as the runner reports it: its clock divider,
# 80 MHz over 694 7/16, gives 115201 baud.
DIVIDED = 115201


def aux(tmp_path: Path) -> Uart:
    return Uart("uart.aux", AUX, clock=recording.EMULATED, directory=tmp_path, log=RunLog())


def linked(*interfaces: Interface) -> BoardLink:
    link = BoardLink("aux-uart-9600", interfaces)
    link.receive(HELLO)
    link.receive(Sync(0))
    return link


def written(seq: int, t: int, baud: int | None, data: bytes) -> UartTx:
    """What the firmware's UART1 transmits on GPIO 26 at `baud`."""
    return UartTx(seq, t, 1, (26,), baud, data)


def test_a_model_answers_a_rate_command_at_the_old_rate_and_what_follows_at_the_new_one(
    tmp_path: Path,
) -> None:
    uart = aux(tmp_path)
    part = uart.model(RateSwitchingModem())
    link = linked(uart)
    assert link.receive(written(1, 500, DEFAULT_BAUD, b"AT+IPR=115200\r\n")) == []
    at = 500 + ANSWER.ns
    assert link.receive(Sync(at)) == [
        UartRx(at, 27, DEFAULT_BAUD, b"AT+IPR=115200\r\n"),
        UartRx(at, 27, DEFAULT_BAUD, b"\r\nOK\r\n"),
        Wait(1, None, ANSWER.ns),
    ]
    assert link.receive(written(2, ms(5).ns, FAST, b"AT\r\n")) == []
    at = ms(5).ns + ANSWER.ns
    assert link.receive(Sync(at)) == [
        UartRx(at, 27, FAST, b"AT\r\n"),
        UartRx(at, 27, FAST, b"\r\nOK\r\n"),
        Wait(2, None, ANSWER.ns),
    ]
    assert link.error is None
    assert part.lines == [(DEFAULT_BAUD, b"AT+IPR=115200"), (FAST, b"AT")]
    assert part.rate == FAST


class Rated:
    """A source in nanoseconds that sends `A` at the profile's rate and `B` at 115200, each a
    microsecond after what it hears."""

    def receive(self, t: int, data: bytes) -> Iterable[Output | OutputAtRate]:
        return [(t + 1000, b"A"), (t + 1000, b"B", FAST)]

    def poll(self, now: int) -> Iterable[Output | OutputAtRate]:
        return []

    def quiet_until(self) -> int | None:
        return None

    def latency(self) -> int | None:
        return 1000


def test_a_pair_goes_in_at_the_profile_s_rate_and_a_triple_at_its_own(tmp_path: Path) -> None:
    uart = aux(tmp_path)
    uart.model(Rated())
    tap = uart.tap("aux.jsonl")
    link = linked(uart)
    link.receive(written(1, 500, DEFAULT_BAUD, b"x"))
    assert link.receive(Sync(1500)) == [
        UartRx(1500, 27, DEFAULT_BAUD, b"A"),
        UartRx(1500, 27, FAST, b"B"),
        Wait(1, None, 1000),
    ]
    # The tap keeps the bytes, not their rates: a recording carries none.
    assert list(tap.events) == [(500, "out", b"x"), (1500, "in", b"A"), (1500, "in", b"B")]


class Polls(ReactiveModel):
    """A model whose every poll returns `output`."""

    answer_after = ms(1)

    def __init__(self, output: object) -> None:
        self.output = output

    def receive(self, at: Duration, data: bytes) -> Iterable[Timed | TimedAtRate]:
        return []

    def poll(self, now: Duration) -> Iterable[Timed | TimedAtRate]:
        return [self.output]  # type: ignore[list-item]


@pytest.mark.parametrize(
    "output",
    [
        (ms(1), b"OK", 0),
        (ms(1), b"OK", -9600),
        (ms(1), b"OK", True),
        (ms(1), b"OK", 9600.0),
        (ms(1), b"OK", "9600"),
        (ms(1), b"OK", None),
        (ms(1), b"OK", 9600, 1),
        (1_000_000, b"OK", 9600),
    ],
)
def test_a_model_s_output_that_is_neither_a_pair_nor_a_triple_is_refused(
    tmp_path: Path, output: object
) -> None:
    uart = aux(tmp_path)
    uart.model(Polls(output))
    link = BoardLink("aux-uart-9600", [uart])
    link.receive(HELLO)
    assert link.receive(Sync(0)) == [Stop()]
    assert link.error == (
        f"uart.aux.poll returned {output!r}, not a (Duration, bytes) pair or a"
        " (Duration, bytes, baud) triple"
    )


def test_a_model_is_told_the_firmware_s_rates_in_durations(tmp_path: Path) -> None:
    uart = aux(tmp_path)
    part = uart.model(RateSwitchingPart())
    link = linked(uart)
    link.receive(UartCfg(1, 100, 1, (26,), 27, DEFAULT_BAUD))
    link.receive(UartCfg(2, 200, 1, (26,), 27, FAST))
    assert part.routes == [
        (Duration(100), DEFAULT_BAUD, True, True),
        (Duration(200), FAST, True, True),
    ]
    # Heard at 115200 by a part at 9600: garbage, echoed at the part's rate, and no command.
    assert link.receive(written(3, 300, FAST, b"AT\r\n")) == []
    assert part.garbled == [(Duration(300), FAST)]
    assert part.lines == []
    at = 300 + ANSWER.ns
    assert link.receive(Sync(at)) == [
        UartRx(at, 27, DEFAULT_BAUD, b"AT\r\n"),
        Wait(3, None, ANSWER.ns),
    ]
    # With no rate set, too.
    link.receive(written(4, ms(2).ns, None, b"AT\r\n"))
    assert part.garbled[-1] == (Duration(ms(2).ns), None)
    assert link.error is None


def test_the_adapter_of_a_model_has_the_optional_methods_the_model_has() -> None:
    adapter = in_nanoseconds(RateSwitchingModem(), "uart.aux", LinkError, rates=True)
    for protocol in (Supplied, RateBound, Configured, TakesFrameErrors):
        assert isinstance(adapter, protocol), protocol
    adapter = in_nanoseconds(RateSwitchingPart(), "uart.aux", LinkError, rates=True)
    assert not isinstance(adapter, TakesFrameErrors)
    plain = in_nanoseconds(Polls(None), "uart.aux", LinkError)
    for protocol in (Supplied, RateBound, Configured, TakesFrameErrors):
        assert not isinstance(plain, protocol), protocol
    # One class for each set of methods, made once.
    assert type(in_nanoseconds(RateSwitchingModem(), "x", LinkError)) is type(
        in_nanoseconds(RateSwitchingModem(), "y", LinkError)
    )


FRM_ERR = Warn(ms(3).ns, "frm_err", "baud", port=1, pin=27, baud=FAST, nbytes=4)


def test_a_model_that_takes_frame_errors_gets_them_and_the_run_goes_on(tmp_path: Path) -> None:
    uart = aux(tmp_path)
    part = uart.model(RateSwitchingModem())
    link = linked(uart)
    link.receive(FRM_ERR)
    assert link.error is None
    assert part.dropped == [(ms(3), 4, FAST)]
    assert link.receive(Sync(ms(4).ns)) == [Wait(0, None, ANSWER.ns)]
    # The warning stays in the test report.
    assert link.warnings == [FRM_ERR]
    assert "frm_err: baud" in link.report()


class TakesInNanoseconds(Rated):
    def __init__(self) -> None:
        self.dropped: list[tuple[int, int, int | None]] = []

    def frame_errors(self, t: int, count: int, baud: int | None) -> None:
        self.dropped.append((t, count, baud))


def test_a_source_in_nanoseconds_takes_frame_errors_in_nanoseconds(tmp_path: Path) -> None:
    uart = aux(tmp_path)
    source = uart.model(TakesInNanoseconds())
    link = linked(uart)
    link.receive(FRM_ERR)
    assert link.error is None
    assert source.dropped == [(ms(3).ns, 4, FAST)]


def test_without_frame_errors_a_frm_err_fails_the_run_as_before(tmp_path: Path) -> None:
    uart = aux(tmp_path)
    uart.model(RateSwitchingPart())
    link = linked(uart)
    link.receive(FRM_ERR)
    assert link.error == (
        "uart.aux sends at 9600 baud on GPIO 27, and the firmware's UART1 receives there at"
        " 115200 baud: 4 bytes dropped as frame errors (frm_err)"
    )


@pytest.mark.parametrize(
    ("route", "sent"),
    [
        # `B` went in at 115200, after `A` at 9600: the route at 9600 dropped `B`.
        (DEFAULT_BAUD, FAST),
        # The route at 115201 reads `B`, at 115200: what it dropped is `A`, at 9600.
        (DIVIDED, DEFAULT_BAUD),
    ],
)
def test_a_frm_err_names_the_last_rate_the_source_sent_at_that_the_route_does_not_read(
    tmp_path: Path, route: int, sent: int
) -> None:
    uart = aux(tmp_path)
    uart.model(Rated())
    link = linked(uart)
    link.receive(written(1, 500, DEFAULT_BAUD, b"x"))
    link.receive(Sync(1500))
    link.receive(Warn(1600, "frm_err", "baud", port=1, pin=27, baud=route, nbytes=1))
    assert link.error == (
        f"uart.aux sends at {sent} baud on GPIO 27, and the firmware's UART1 receives there at"
        f" {route} baud: 1 byte dropped as frame errors (frm_err)"
    )


def test_a_frm_err_without_its_count_fails_the_run_whatever_the_source(tmp_path: Path) -> None:
    uart = aux(tmp_path)
    part = uart.model(RateSwitchingModem())
    link = linked(uart)
    link.receive(Warn(ms(3).ns, "frm_err", "baud", port=1, pin=27, baud=FAST))
    assert part.dropped == []
    assert link.error == (
        "uart.aux sends at 9600 baud on GPIO 27, and the firmware's UART1 receives there at"
        " 115200 baud: bytes dropped as frame errors (frm_err)"
    )


class Refuses(RateSwitchingModem):
    def frame_errors(self, at: Duration, count: int, baud: int | None) -> None:
        raise ValueError(f"{count} bytes lost")


def test_a_model_that_raises_on_its_frame_errors_fails_the_run(tmp_path: Path) -> None:
    uart = aux(tmp_path)
    uart.model(Refuses())
    link = linked(uart)
    link.receive(FRM_ERR)
    assert link.error == "uart.aux.frame_errors raised ValueError: 4 bytes lost"


class RaisesOnTransmitted(RateSwitchingPart):
    def transmitted(self, at: Duration, baud: int | None) -> None:
        raise ValueError(f"not at {baud}")


class RaisesOnConfigured(RateSwitchingPart):
    def configured(self, at: Duration, baud: int | None, transmits: bool, receives: bool) -> None:
        raise ValueError(f"not at {baud}")


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        (RaisesOnTransmitted, "uart.aux.transmitted raised ValueError: not at 115200"),
        (RaisesOnConfigured, "uart.aux.configured raised ValueError: not at 115200"),
    ],
)
def test_a_model_that_raises_on_the_firmware_s_rate_fails_the_run(
    tmp_path: Path, kind: type[RateSwitchingPart], message: str
) -> None:
    uart = aux(tmp_path)
    part = uart.model(kind())
    link = linked(uart)
    link.receive(UartCfg(1, 100, 1, (26,), 27, FAST))
    link.receive(written(2, 200, FAST, b"AT\r\n"))
    assert link.error == message
    assert part.lines == []


class Lineless(Interface):
    """An interface without a line, `kind` (`ethernet`, `bluetooth` or `pinless_console`), which
    sends one output at a line rate it does not have."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind, None)
        self.kind = kind

    @property
    def ethernet(self) -> bool:
        return self.kind == "ethernet"

    @property
    def bluetooth(self) -> bool:
        return self.kind == "bluetooth"

    @property
    def pinless_console(self) -> bool:
        return self.kind == "pinless_console"

    def poll(self, now: int) -> Iterable[Output | OutputAtRate]:
        return [(now, b"bytes", 9600)]


@pytest.mark.parametrize("kind", ["ethernet", "bluetooth", "pinless_console"])
def test_an_interface_without_a_line_refuses_an_output_at_a_rate(kind: str) -> None:
    link = BoardLink("aux-uart-9600", [Lineless(kind)])
    link.receive(HELLO)
    assert link.receive(Sync(0)) == [Stop()]
    assert link.error == (
        f"{kind} sent bytes at 9600 baud, and has no line rate to send them at: its outputs are"
        " (t_ns, bytes) pairs"
    )


def test_what_goes_through_the_modem_s_data_mode_has_no_rate_of_its_own() -> None:
    modem = Modem(lambda: 0, data=Rated())
    list(modem.receive(0, b"ATD*99#\r\n"))
    assert modem.data_mode
    with pytest.raises(TypeError, match=r"^the modem's data peer returned \(11000, b'B', 115200\)"):
        list(modem.receive(10_000, b"~"))


# On the emulator, with firmware/uart_rate_switch.


@pytest.mark.app_path("../firmware/uart_rate_switch")
@pytest.mark.board_profile(path="boards/aux-uart-9600.toml")
def test_the_firmware_switches_the_part_to_115200_and_follows_it(board: Board) -> None:
    part = board.uart("aux").model(RateSwitchingModem())
    board.power_on()
    board.console.expect_exact("rate_switch: trying 9600", timeout=seconds(5))
    board.console.expect_exact("rate_switch: found at 9600", timeout=seconds(1))
    board.console.expect_exact("rate_switch: AT+IPR=115200 ok", timeout=seconds(1))
    board.console.expect_exact("rate_switch: 115200 ok", timeout=seconds(1))
    board.console.expect_exact("rate_switch: done", timeout=seconds(1))
    assert part.lines == [(9600, b"AT"), (9600, b"AT+IPR=115200"), (FAST, b"AT")]
    assert (part.rate, part.garbled, part.dropped) == (FAST, [], [])
    # The firmware's receiving route, as the part was told it: 9600, then 115200.
    assert [baud for _, baud, _, receives in part.routes if receives] == [9600, DIVIDED]


@pytest.mark.app_path("../firmware/uart_rate_switch")
@pytest.mark.board_profile(path="boards/aux-uart-9600.toml")
def test_after_a_power_loss_the_firmware_searches_for_the_part_s_rate(board: Board) -> None:
    part = board.uart("aux").model(RateSwitchingModem())
    board.power_on()
    board.console.expect_exact("rate_switch: done", timeout=seconds(5))
    board.power_off()
    assert part.rate == DEFAULT_BAUD
    board.power_on()
    board.console.expect_exact("rate_switch: trying 115200", timeout=seconds(5))
    board.console.expect_exact("rate_switch: no answer at 115200", timeout=seconds(1))
    board.console.expect_exact("rate_switch: trying 9600", timeout=seconds(1))
    board.console.expect_exact("rate_switch: found at 9600", timeout=seconds(1))
    board.console.expect_exact("rate_switch: 115200 ok", timeout=seconds(1))
    board.console.expect_exact("rate_switch: done", timeout=seconds(1))
    # The AT at 115200 came to a part at 9600, whose echo the firmware's UART dropped.
    # However the runner cuts and merges them: the 4 bytes of `AT\r\n`, all at 115201.
    assert {baud for _, baud in part.garbled} == {DIVIDED}
    assert {baud for _, _, baud in part.dropped} == {DIVIDED}
    assert sum(count for _, count, _ in part.dropped) == 4
    assert part.lines[-3:] == [(9600, b"AT"), (9600, b"AT+IPR=115200"), (FAST, b"AT")]


def test_the_same_part_without_frame_errors_fails_the_search(pytester: pytest.Pytester) -> None:
    if not (RATE_SWITCH / "build" / "esp32tl-built").is_file():
        pytest.fail(
            f"{RATE_SWITCH}/build holds no finished build: run make firmware", pytrace=False
        )
    pytester.makepyfile(
        rate_switching=(ROOT / "tests" / "rate_switching.py").read_text(),
        test_search=f"""
        import pytest
        from rate_switching import RateSwitchingPart
        from esp32tl.units import seconds
        pytestmark = [pytest.mark.app_path({str(RATE_SWITCH)!r}),
                      pytest.mark.board_profile(path={str(PROFILE)!r})]

        def test_search(board):
            board.uart("aux").model(RateSwitchingPart())
            board.power_on()
            board.console.expect_exact("rate_switch: done", timeout=seconds(5))
            board.power_off()
            board.power_on()
            board.console.expect_exact("rate_switch: found at 9600", timeout=seconds(5))
        """,
    )
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "*RunEnded: the run ended at * while the test waited for 'rate_switch: found at 9600'"
            "*: the link failed: uart.aux sends at 9600 baud on GPIO 27, and the firmware's UART1"
            " receives there at 115201 baud: * dropped as frame errors (frm_err)*"
        ]
    )
