"""The bases a model of the test's can start from instead of `Model`: `ReactiveModel`, which only
answers, and `IdleFramedModel`, which also cuts what it hears into frames at each silence, as a
Modbus RTU slave does. Each is driven through the link with the runner's messages scripted, as
`test_uart_model.py` drives `Model`, so the instants are the protocol's own: what `receive` gets
as `at` is the `t` of the `uart_tx`, the instant the firmware wrote the first byte of it."""

from collections.abc import Iterable
from pathlib import Path

import pytest
from test_uart_model import AUX, HELLO, aux

from esp32tl import Board, recording
from esp32tl.interfaces.uart import Uart
from esp32tl.link.boardlink import BoardLink, Wire
from esp32tl.link.protocol import Sync, UartRx, UartTx, Wait
from esp32tl.report import RunLog
from esp32tl.sources.model import IdleFramedModel, ReactiveModel, Timed
from esp32tl.units import Duration, ms, seconds

ANSWER = ms(1)
# An RS485 route at 9600 baud: a character is 10 bits, 1.0417 ms, and RTU's silence between frames
# is 3.5 of them.
RS485 = Wire(tx=17, rx=16, baud=9600)
GAP = ms(3.65)
TURNAROUND = ms(5)
# Two Modbus RTU requests, protocol only: read 2 holding registers from 0, of slaves 1 and 2, each
# with its CRC, low byte first.
READ_1 = bytes.fromhex("010300000002c40b")
READ_2 = bytes.fromhex("020300000002c438")


class AtResponder(ReactiveModel):
    """`OK` to every command line, `ANSWER` after its CR LF."""

    answer_after = ANSWER

    def __init__(self) -> None:
        self.heard: list[tuple[Duration, bytes]] = []
        self._buffer = b""

    def receive(self, at: Duration, data: bytes) -> Iterable[Timed]:
        self.heard.append((at, data))
        self._buffer += data
        answers = []
        while b"\r\n" in self._buffer:
            _, _, self._buffer = self._buffer.partition(b"\r\n")
            answers.append((at + self.answer_after, b"\r\nOK\r\n"))
        return answers


class Deaf(ReactiveModel):
    """Hears and answers nothing."""

    answer_after = None

    def receive(self, at: Duration, data: bytes) -> Iterable[Timed]:
        return ()


class Unset(ReactiveModel):
    """Forgot to say how soon it answers."""

    def receive(self, at: Duration, data: bytes) -> Iterable[Timed]:
        return ()


class Slave(IdleFramedModel):
    """Answers every frame with the frame itself, `TURNAROUND` after the silence that ended it,
    and keeps each frame with the instant it was told of it."""

    def __init__(self, gap: Duration = GAP) -> None:
        self.gap = gap
        self.frames: list[tuple[Duration, bytes]] = []

    def on_frame(self, at: Duration, frame: bytes) -> Iterable[Timed]:
        self.frames.append((at, frame))
        return [(at + TURNAROUND, frame)]


class NoGap(IdleFramedModel):
    def on_frame(self, at: Duration, frame: bytes) -> Iterable[Timed]:
        return ()


def rs485(tmp_path: Path) -> Uart:
    return Uart(
        "uart.rs485",
        RS485,
        clock=recording.EMULATED,
        directory=tmp_path,
        log=RunLog(),
    )


def tx(seq: int, at: Duration, data: bytes) -> UartTx:
    return UartTx(seq, at.ns, 1, (17,), 9600, data)


def test_a_reactive_model_answers_after_answer_after_and_sends_nothing_else(
    tmp_path: Path,
) -> None:
    uart, _ = aux(tmp_path)
    model = uart.model(AtResponder())
    link = BoardLink("aux-uart", [uart])
    link.receive(HELLO)
    assert link.receive(Sync(0)) == [Wait(0, None, ANSWER.ns)]
    assert link.receive(UartTx(1, 500, 1, (26,), AUX.baud, b"AT\r\n")) == []
    assert link.receive(Sync(500 + ANSWER.ns)) == [
        UartRx(500 + ANSWER.ns, 27, AUX.baud, b"\r\nOK\r\n"),
        Wait(1, None, ANSWER.ns),
    ]
    assert link.error is None
    # `at` is the uart_tx's `t`, the instant the firmware wrote the first byte of what it carries.
    assert model.heard == [(Duration(500), b"AT\r\n")]
    assert list(model.poll(ms(10))) == []
    assert model.quiet_until() is None


def test_a_reactive_model_without_answer_after_says_what_to_set(tmp_path: Path) -> None:
    assert Deaf().latency() is None
    with pytest.raises(
        TypeError,
        match=r"^Unset has no answer_after: set it to the shortest time it takes to answer,"
        r" as ms\(1\)$",
    ):
        Unset().latency()
    uart, _ = aux(tmp_path)
    uart.model(Unset())
    link = BoardLink("aux-uart", [uart])
    link.receive(HELLO)
    link.receive(Sync(0))
    assert link.error is not None
    assert "uart.aux.latency raised TypeError: Unset has no answer_after" in str(link.error)


def test_a_frame_ends_at_the_first_silence_of_gap_and_is_answered_then(tmp_path: Path) -> None:
    uart = rs485(tmp_path)
    slave = uart.model(Slave())
    link = BoardLink("rs485", [uart])
    link.receive(HELLO)
    # Nothing heard: nothing scheduled, and a turn GAP after whatever the firmware sends.
    assert link.receive(Sync(0)) == [Wait(0, None, GAP.ns)]
    # The request in two pieces 2 ms apart, less than the gap: one frame. The runner's turn comes
    # GAP after the first piece, when the frame has not ended yet: it ends GAP after the second.
    first, second = ms(10), ms(12)
    assert link.receive(tx(1, first, READ_1[:4])) == []
    assert link.receive(tx(2, second, READ_1[4:])) == []
    assert link.receive(Sync((first + GAP).ns)) == [Wait(2, (second + GAP).ns, GAP.ns)]
    assert slave.frames == []
    end = second + GAP
    assert link.receive(Sync(end.ns)) == [
        UartRx((end + TURNAROUND).ns, 16, RS485.baud, READ_1),
        Wait(2, None, GAP.ns),
    ]
    assert slave.frames == [(end, READ_1)]
    assert link.error is None


def test_two_requests_a_silence_apart_are_two_frames(tmp_path: Path) -> None:
    uart = rs485(tmp_path)
    slave = uart.model(Slave())
    link = BoardLink("rs485", [uart])
    link.receive(HELLO)
    link.receive(Sync(0))
    # The second request comes 4 ms after the first, more than the gap: the runner's turn at
    # first + GAP ends the first frame before the second is heard.
    first, second = ms(10), ms(14)
    link.receive(tx(1, first, READ_1))
    sent = link.receive(Sync((first + GAP).ns))
    assert sent[0] == UartRx((first + GAP + TURNAROUND).ns, 16, RS485.baud, READ_1)
    link.receive(tx(2, second, READ_2))
    link.receive(Sync((second + GAP).ns))
    assert slave.frames == [(first + GAP, READ_1), (second + GAP, READ_2)]
    assert link.error is None


def test_the_frame_ends_at_the_turn_that_comes_after_the_gap(tmp_path: Path) -> None:
    # A turn later than the gap's end, as one the runner takes at once when the block point is
    # already behind the guest: the frame ends at that turn, and the answers are not in its past.
    slave = Slave()
    assert list(slave.receive(ms(10), READ_1)) == []
    assert slave.quiet_until() == ms(10) + GAP
    assert list(slave.poll(ms(10) + GAP - Duration(1))) == []
    late = ms(10) + GAP + Duration(3)
    assert list(slave.poll(late)) == [(late + TURNAROUND, READ_1)]
    assert slave.quiet_until() is None
    assert list(slave.poll(late + ms(1))) == []


def test_a_dropped_frame_never_reaches_on_frame() -> None:
    # A subclass that loses its power with the board's supply forgets what it had heard.
    model = Slave()
    model.receive(ms(0), READ_1[:3])
    assert model.quiet_until() == GAP
    model.drop()
    assert model.quiet_until() is None
    assert list(model.poll(ms(10))) == []
    model.receive(ms(10), READ_2)
    assert list(model.poll(ms(10) + GAP)) == [(ms(10) + GAP + TURNAROUND, READ_2)]
    assert model.frames == [(ms(10) + GAP, READ_2)]


def test_an_idle_framed_model_without_gap_says_what_to_set() -> None:
    model = NoGap()
    assert model.quiet_until() is None
    with pytest.raises(
        TypeError,
        match=r"^NoGap has no gap: set it to the silence that ends a frame, as ms\(3\.65\)$",
    ):
        model.latency()


class LineSlave(IdleFramedModel):
    """`OK` to every frame that ends in CR LF, `ANSWER` after the silence that ended it; nothing
    to one that does not, as the escape `+++`. At 115200 baud, 3.5 characters are 0.30 ms."""

    gap = ms(0.304)

    def __init__(self) -> None:
        self.frames: list[tuple[Duration, bytes]] = []

    def on_frame(self, at: Duration, frame: bytes) -> Iterable[Timed]:
        self.frames.append((at, frame))
        return [(at + ANSWER, b"\r\nOK\r\n")] if frame.endswith(b"\r\n") else []


@pytest.mark.app_path("../firmware/modem_at")
@pytest.mark.board_profile(path="boards/aux-uart.toml")
def test_the_firmware_s_writes_frame_by_their_silences(board: Board) -> None:
    # modem_at writes each command and its CR LF with two uart_write_bytes calls, which come
    # within the gap, as one frame; and waits 20 ms after the escape it writes first, which
    # `AtResponder`, finding lines by their CR LF, hears joined to the first command, `+++AT`.
    part = board.uart("aux").model(LineSlave())
    board.power_on()
    board.console.expect_exact("modem_at: dialogue complete", timeout=seconds(5))
    frames = [frame for _, frame in part.frames]
    assert frames[:4] == [b"+++", b"AT\r\n", b"ATE0\r\n", b"AT+CPIN?\r\n"]
    (escape, _), (first, _) = part.frames[:2]
    assert first - escape >= ms(10)
