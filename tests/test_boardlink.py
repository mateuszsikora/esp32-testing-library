"""`BoardLink`'s turns and routing (`docs/protocol.md` §4 to §8), without an emulator: the
runner's messages are fed in, and what the harness answers is checked."""

import select
import socket
import threading
from collections.abc import Iterable, Iterator, Sequence

import pytest

from esp32tl.link import boardlink
from esp32tl.link.boardlink import BoardLink, Direction, Interface, Output, Wire
from esp32tl.link.protocol import (
    MAX_LINE,
    Adc,
    Console,
    GpioIn,
    GpioOut,
    HarnessHello,
    HarnessMessage,
    Input,
    Release,
    Reset,
    RunnerHello,
    RunnerMessage,
    Stats,
    Stop,
    Sync,
    UartCfg,
    UartRx,
    UartStats,
    UartTx,
    Wait,
    Warn,
    decode_harness,
    encode,
)

HELLO = RunnerHello(1, "esp32", "emulated-fifo-write", "8c652151dd5a521249a785ca147f20a9f8eda68f")
GNSS = Wire(tx=4, rx=5, baud=9600)
MODEM = Wire(tx=26, rx=27, baud=115200)
MS = 1_000_000


class Peer(Interface):
    """An interface that records what it is told, answers every chunk with `answer` after
    `latency`, and sends `schedule` unprompted, each output in the turn at its time."""

    def __init__(
        self,
        name: str = "gnss",
        wire: Wire = GNSS,
        *,
        latency: int | None = None,
        answer: bytes = b"",
        schedule: Iterable[Output] = (),
    ) -> None:
        super().__init__(name, wire)
        self.received: list[tuple[int, Direction, bytes]] = []
        self.routes: list[tuple[int, int | None, bool, bool]] = []
        self.drives: list[tuple[int, list[tuple[int, int]], frozenset[int]]] = []
        self.polls: list[int] = []
        self._latency = latency
        self._answer = answer
        self.schedule = sorted(schedule)

    def receive(self, t: int, direction: Direction, data: bytes) -> Iterable[Output]:
        self.received.append((t, direction, data))
        if self._latency is None or not self._answer:
            return []
        return [(t + self._latency, self._answer)]

    def poll(self, now: int) -> Iterable[Output]:
        self.polls.append(now)
        due = [output for output in self.schedule if output[0] <= now]
        self.schedule = self.schedule[len(due) :]
        return due

    def quiet_until(self) -> int | None:
        return self.schedule[0][0] if self.schedule else None

    def latency(self) -> int | None:
        return self._latency

    def configured(self, t: int, baud: int | None, transmits: bool, receives: bool) -> None:
        self.routes.append((t, baud, transmits, receives))

    def driven(self, t: int, changes: Sequence[tuple[int, int]], enabled: frozenset[int]) -> None:
        self.drives.append((t, list(changes), enabled))


class Scenario:
    def __init__(self, inputs: Iterable[Input], pins: Iterable[int] = (33,)) -> None:
        self.inputs = sorted(inputs, key=lambda message: message.t)
        self.pins = frozenset(pins)

    def due(self, now: int) -> Iterable[Input]:
        due = [message for message in self.inputs if message.t <= now]
        self.inputs = self.inputs[len(due) :]
        return due

    def quiet_until(self) -> int | None:
        return self.inputs[0].t if self.inputs else None


def started(*interfaces: Interface, timeline: Scenario | None = None) -> BoardLink:
    """A link past the handshake and turn 0."""
    link = BoardLink("esp32-devkitc", interfaces, timeline=timeline)
    link.receive(HELLO)
    link.receive(Sync(0))
    return link


def feed(link: BoardLink, *messages: RunnerMessage) -> list[HarnessMessage]:
    """Feeds `messages`, checking that the harness writes only after the last."""
    for message in messages[:-1]:
        assert link.receive(message) == [], message
    return link.receive(messages[-1])


def tx(seq: int, t: int, data: bytes, tx_pins: tuple[int, ...] = (4,)) -> UartTx:
    return UartTx(seq, t, 1, tx_pins, 9600, data)


def test_the_handshake_names_the_board_and_watches_the_interfaces_pins() -> None:
    link = BoardLink("esp32-devkitc", [Peer("modem", MODEM), Peer("gnss", GNSS)])
    assert link.receive(HELLO) == [HarnessHello(1, "esp32", "esp32-devkitc", (4, 5, 26, 27))]
    assert link.runner == HELLO
    assert link.error is None


def test_the_scenario_s_pins_are_watched_too() -> None:
    link = BoardLink("esp32-devkitc", [Peer()], timeline=Scenario([], pins=[33, 5]))
    assert link.receive(HELLO) == [HarnessHello(1, "esp32", "esp32-devkitc", (4, 5, 33))]


def test_two_interfaces_on_one_pin_are_refused() -> None:
    with pytest.raises(ValueError, match="GPIO 5 is on gnss and other"):
        BoardLink("esp32-devkitc", [Peer("gnss"), Peer("other", Wire(tx=None, rx=5, baud=9600))])


def test_a_mismatched_handshake_is_answered_then_only_the_end_of_the_run_is_kept() -> None:
    errors: list[str] = []
    link = BoardLink("esp32-devkitc", [Peer()], on_error=errors.append)
    assert link.receive(RunnerHello(2)) == [HarnessHello(1, "esp32", "esp32-devkitc", (4, 5))]
    assert link.error == "the runner speaks link protocol version 2, the harness 1"
    # The runner finds the mismatch itself: the backend need not end the run.
    assert errors == []
    link.connected = True  # as run() does
    assert "runner: protocol version 2\n" in link.report()
    stats = Stats(0, "protocol_error", 0, 0, (), 0, 0, 0, 0, detail="version")
    assert link.receive(stats) == []
    assert link.stats == stats
    assert link.error == "the runner speaks link protocol version 2, the harness 1"


def test_turn_0_gives_no_bound_when_nothing_is_scheduled() -> None:
    link = BoardLink("esp32-devkitc", [Peer()])
    link.receive(HELLO)
    assert link.receive(Sync(0)) == [Wait(0, None, None)]


def test_the_first_message_must_be_hello() -> None:
    link = BoardLink("esp32-devkitc")
    assert link.receive(Sync(0)) == [Stop()]
    assert link.error == "the runner's first message is sync, not hello"


def test_wait_acknowledges_the_last_event_before_the_sync() -> None:
    link = started()
    assert feed(
        link,
        tx(1, 10, b"A"),
        Warn(11, "unrouted_rx", "dropped", pin=5, nbytes=1),
        UartCfg(2, 12, 1, (4,), 5, 9600),
        Reset(3, 20, 12),
        Sync(30),
    ) == [Wait(3, None, None)]
    # A turn with no events acknowledges the same.
    assert link.receive(Sync(40)) == [Wait(3, None, None)]
    assert link.turns == 3


@pytest.mark.parametrize(
    ("messages", "error"),
    [
        ([tx(2, 10, b"A")], "the runner sent uart_tx with seq 2 after 0"),
        ([tx(1, 10, b"A"), tx(1, 11, b"B")], "the runner sent uart_tx with seq 1 after 1"),
        (
            [tx(1, 10, b"A"), Console(9, b"x")],
            "the runner sent console at 9 ns after a message at 10 ns",
        ),
        ([HELLO], "the runner sent a second hello"),
    ],
)
def test_a_runner_that_breaks_the_protocol_is_stopped_at_its_next_turn(
    messages: list[RunnerMessage], error: str
) -> None:
    errors: list[str] = []
    link = started(Peer())
    link.on_error = errors.append
    for message in messages:
        assert link.receive(message) == []
    assert link.error == error
    # Once, at the first error: the backend ends the run then, not at the next turn.
    assert errors == [error]
    assert link.receive(Sync(50)) == [Stop()]
    # Once only: the runner ends the run on stop.
    assert link.receive(Sync(60)) == []


def test_a_message_after_stats_is_an_error() -> None:
    link = started()
    link.receive(Stats(10, "max_seconds", 0, 1, (), 0, 0, 0, 0))
    link.receive(Sync(10))
    assert link.error == "the runner sent sync after its stats"


def test_uart_tx_reaches_the_interfaces_on_its_tx_pins_chunk_by_chunk() -> None:
    gnss, modem, other = Peer("gnss"), Peer("modem", MODEM), Peer("other", Wire(13, 14, 9600))
    link = started(gnss, modem, other)
    feed(
        link,
        tx(1, 10, b"$PMTK2"),
        # GPIO matrix fan-out: one UART's TX on two pins.
        tx(2, 20, b"20*", tx_pins=(4, 26)),
        tx(3, 30, b"2F\r\n"),
        Sync(40),
    )
    # In order and with each chunk's time: where a chunk ends means nothing (5.2).
    assert gnss.received == [(10, "out", b"$PMTK2"), (20, "out", b"20*"), (30, "out", b"2F\r\n")]
    assert modem.received == [(20, "out", b"20*")]
    assert other.received == []


def test_an_answer_at_the_event_plus_latency_goes_out_before_the_wait() -> None:
    gnss = Peer(latency=5 * MS, answer=b"$PMTK001,220,3*30\r\n")
    link = started(gnss)
    replies = feed(link, tx(1, 100, b"$PMTK220,1000*1F\r\n"), Sync(100 + 5 * MS))
    assert replies == [
        UartRx(100 + 5 * MS, 5, 9600, b"$PMTK001,220,3*30\r\n"),
        Wait(1, None, 5 * MS),
    ]


def test_an_input_stamped_before_the_turn_is_an_error_naming_the_interface() -> None:
    gnss = Peer(latency=5 * MS, answer=b"OK")
    link = started(gnss)
    assert feed(link, tx(1, 100, b"AT\r"), Sync(100 + 6 * MS)) == [Stop()]
    assert link.error is not None
    assert link.error.startswith(f"gnss sent bytes stamped {100 + 5 * MS} ns in the turn at")


def test_unprompted_outputs_go_out_in_their_turn_and_bound_the_next() -> None:
    gnss = Peer(schedule=[(1000 * MS, b"$GPGGA\r\n"), (2000 * MS, b"$GPRMC\r\n")])
    link = BoardLink("esp32-devkitc", [gnss])
    link.receive(HELLO)
    assert link.receive(Sync(0)) == [Wait(0, 1000 * MS, None)]
    assert link.receive(Sync(1000 * MS)) == [
        UartRx(1000 * MS, 5, 9600, b"$GPGGA\r\n"),
        Wait(0, 2000 * MS, None),
    ]
    assert link.receive(Sync(2000 * MS)) == [
        UartRx(2000 * MS, 5, 9600, b"$GPRMC\r\n"),
        Wait(0, None, None),
    ]
    assert gnss.polls == [0, 1000 * MS, 2000 * MS]


def test_future_outputs_go_out_at_once_for_the_runner_to_queue() -> None:
    class Stream(Peer):
        def poll(self, now: int) -> Iterable[Output]:
            due, self.schedule = self.schedule, []
            return due

    link = BoardLink("esp32-devkitc", [Stream(schedule=[(5 * MS, b"a"), (7 * MS, b"b")])])
    link.receive(HELLO)
    assert link.receive(Sync(0)) == [
        UartRx(5 * MS, 5, 9600, b"a"),
        UartRx(7 * MS, 5, 9600, b"b"),
        Wait(0, None, None),
    ]


def test_quiet_until_and_latency_are_the_minimum_none_being_no_bound() -> None:
    link = started(
        Peer("a", Wire(4, 5, 9600), latency=None, schedule=[(300, b"x")]),
        Peer("b", Wire(12, 13, 9600), latency=7),
        Peer("c", Wire(14, 15, 9600), latency=3, schedule=[(200, b"y")]),
        timeline=Scenario([GpioIn(250, ((33, 1),))]),
    )
    assert link.receive(Sync(100)) == [Wait(0, 200, 3)]


def test_the_scenario_s_inputs_go_out_in_their_turn() -> None:
    link = started(timeline=Scenario([GpioIn(250, ((33, 1),)), GpioIn(400, ((33, 0),))]))
    assert link.receive(Sync(250)) == [GpioIn(250, ((33, 1),)), Wait(0, 400, None)]


def test_a_quiet_until_not_after_the_turn_is_an_error() -> None:
    class Stuck(Peer):
        def quiet_until(self) -> int | None:
            return 100

    link = started(Stuck())
    assert link.receive(Sync(100)) == [Stop()]
    assert link.error == (
        "gnss is quiet until 100 ns, not after the turn at 100 ns: what it sends by then belongs"
        " in this turn"
    )


def test_uart_cfg_tells_the_interfaces_on_the_route_how_it_is_configured() -> None:
    gnss, modem = Peer("gnss"), Peer("modem", MODEM)
    link = started(gnss, modem)
    feed(
        link,
        # Receive-only: the firmware never transmits to a GNSS receiver.
        UartCfg(1, 10, 1, (), 5, 9600),
        UartCfg(2, 20, 1, (4,), 5, 115200),
        UartCfg(3, 30, 2, (26,), None, None),
        # Port 1 moves to other pins: the GNSS wire is left unrouted.
        UartCfg(4, 40, 1, (13,), 14, 115200),
        Sync(50),
    )
    assert gnss.routes == [
        (10, 9600, False, True),
        (20, 115200, True, True),
        (40, None, False, False),
    ]
    assert modem.routes == [(30, None, True, False)]


def test_gpio_out_reaches_the_interfaces_on_its_pins() -> None:
    gnss, modem = Peer("gnss"), Peer("modem", MODEM)
    link = started(gnss, modem)
    feed(
        link,
        GpioOut(1, 10, ((4, 1),), (4,), (4,)),
        # An enable-only change: the line released.
        GpioOut(2, 20, (), (), ()),
        GpioOut(3, 30, ((26, 0),), (26,), ()),
        Sync(40),
    )
    assert gnss.drives == [(10, [(4, 1)], frozenset({4})), (20, [], frozenset())]
    assert modem.drives == [(30, [(26, 0)], frozenset({26}))]


def test_an_interface_that_raises_fails_the_link_with_its_traceback() -> None:
    class Broken(Peer):
        def receive(self, t: int, direction: Direction, data: bytes) -> Iterator[Output]:
            raise RuntimeError("no parser for that")
            yield  # a generator: it raises only once read

    link = started(Broken())
    assert feed(link, tx(1, 10, b"?"), Sync(20)) == [Stop()]
    assert link.error == "gnss.receive raised RuntimeError: no parser for that"
    assert link.error_traceback is not None
    assert 'raise RuntimeError("no parser for that")' in link.error_traceback
    assert "Traceback" in link.report()


def test_warnings_and_stats_go_into_the_report() -> None:
    link = started(Peer())
    warning = Warn(
        1_000_000_000, "unrouted_rx", "uart_rx on GPIO 5: 72 bytes dropped", pin=5, nbytes=72
    )
    link.receive(warning)
    link.receive(
        Stats(12_000_000_000, "signal", 0, 13, (UartStats(1, 0, 864, 0, 0),), 72, 0, 2, 40)
    )
    assert link.warnings == [warning]
    report = link.report()
    assert "1.000000000 s  unrouted_rx: uart_rx on GPIO 5: 72 bytes dropped" in report
    assert "stats: the run ended at 12.000000000 s (signal); 13 turns, 0 resets" in report
    assert "UART1: 0 bytes sent, 864 received, 0 rxfifo_ovf, 0 frm_err" in report
    assert "inputs_not_applied 2, rx_bytes_not_paced 40" in report
    properties = link.properties()
    assert properties["warnings"][0]["bytes"] == 72
    assert properties["stats"]["inputs_not_applied"] == 2
    assert properties["watch"] == [4, 5]
    assert properties["error"] is None


@pytest.mark.parametrize(
    ("warning", "error"),
    [
        (
            Warn(MS, "frm_err", "baud", port=1, pin=5, baud=115200, nbytes=70),
            "gnss sends at 9600 baud on GPIO 5, and the firmware's UART1 receives there at"
            " 115200 baud: 70 bytes dropped as frame errors (frm_err)",
        ),
        (
            # A route whose baud the firmware has not set, without its port and count.
            Warn(MS, "frm_err", "baud", pin=5),
            "gnss sends at 9600 baud on GPIO 5, and the firmware's UART receives there with"
            " no baud set: bytes dropped as frame errors (frm_err)",
        ),
        (
            # No pin either: no interface is named, not even one without an RX pin.
            Warn(MS, "frm_err", "baud", port=1, baud=9600, nbytes=2),
            "bytes on a pin the runner did not name, and the firmware's UART1 receives there"
            " at 9600 baud: 2 bytes dropped as frame errors (frm_err)",
        ),
        (
            # Bytes the scenario sent, on a pin of its own.
            Warn(MS, "frm_err", "baud", port=2, pin=33, baud=9600, nbytes=1),
            "bytes on GPIO 33, and the firmware's UART2 receives there at 9600 baud:"
            " 1 byte dropped as frame errors (frm_err)",
        ),
    ],
)
def test_a_frame_error_fails_the_run_at_once(warning: Warn, error: str) -> None:
    errors: list[str] = []
    # The modem's wire has no RX pin: a warning without a pin must not name it.
    link = started(
        Peer(), Peer("modem", Wire(tx=26, rx=None, baud=115200)), timeline=Scenario([], pins=[33])
    )
    link.on_error = errors.append
    assert link.receive(warning) == []
    # Kept for the report as any warning, and the run ended without waiting for its next turn.
    assert link.warnings == [warning]
    assert link.error == error
    assert errors == [error]
    assert link.receive(Sync(2 * MS)) == [Stop()]


def test_other_warnings_fail_nothing() -> None:
    link = started(Peer())
    for kind in ("rxfifo_ovf", "unrouted_rx", "input_on_output", "a kind of a later version"):
        link.receive(Warn(MS, kind, "dropped", port=1, pin=5, nbytes=1))
    assert link.error is None
    assert link.receive(Sync(2 * MS)) == [Wait(0, None, None)]


def test_the_report_says_when_the_runner_never_connected_or_left_no_stats() -> None:
    link = BoardLink("esp32-devkitc")
    assert "the runner never connected" in link.report()
    link.connected = True
    assert "no stats: the link ended without them" in link.report()


class FakeRunner:
    """The runner's end of a socket pair, with `BoardLink.run` on the other end in a thread."""

    def __init__(self, link: BoardLink) -> None:
        self.link = link
        self.sock, harness = socket.socketpair()
        self.harness = harness
        self.lines = self.sock.makefile("rb")
        self.thread = threading.Thread(target=link.run, args=(harness,))
        self.thread.start()

    def send(self, *messages: RunnerMessage) -> None:
        self.sock.sendall(b"".join(encode(message) for message in messages))

    def read(self) -> HarnessMessage:
        return decode_harness(self.lines.readline())

    def silent(self) -> bool:
        """Whether the harness has written nothing more, a tenth of a second on."""
        return not select.select([self.sock], [], [], 0.1)[0]

    def close(self) -> None:
        """Closes the runner's end, and checks that the harness has read to end of file and
        written nothing more."""
        self.sock.shutdown(socket.SHUT_WR)
        self.thread.join(5)
        assert not self.thread.is_alive()
        self.harness.shutdown(socket.SHUT_WR)
        assert self.lines.read() == b""
        self.lines.close()
        self.sock.close()
        self.harness.close()


def test_run_writes_only_in_the_harness_s_turns_and_reads_to_end_of_file() -> None:
    gnss = Peer(latency=MS, answer=b"OK\r\n")
    runner = FakeRunner(BoardLink("esp32-devkitc", [gnss]))
    runner.send(HELLO)
    assert runner.read() == HarnessHello(1, "esp32", "esp32-devkitc", (4, 5))
    assert runner.silent()
    runner.send(Sync(0))
    assert runner.read() == Wait(0, None, MS)
    runner.send(tx(1, 10, b"AT\r\n"))
    # The answer waits for the harness's turn.
    assert runner.silent()
    runner.send(Sync(10 + MS))
    assert runner.read() == UartRx(10 + MS, 5, 9600, b"OK\r\n")
    assert runner.read() == Wait(1, None, MS)
    runner.send(Stats(10 + MS, "signal", 0, 2, (), 0, 0, 0, 0))
    runner.close()
    assert runner.link.stats is not None
    assert runner.link.error is None


def test_run_reads_past_a_malformed_line_and_stops_the_run() -> None:
    runner = FakeRunner(BoardLink("esp32-devkitc"))
    runner.send(HELLO)
    runner.read()
    runner.sock.sendall(b'{"type":"sync"}\n' + b"x" * (MAX_LINE + 10) + b"\n")
    runner.send(Sync(0))
    assert runner.read() == Stop()
    runner.send(Stats(0, "stop", 0, 1, (), 0, 0, 0, 0))
    runner.close()
    assert runner.link.error == "the runner sent a malformed message: sync: no t"
    assert runner.link.stats is not None


def test_run_reads_a_line_too_long_to_its_end() -> None:
    runner = FakeRunner(BoardLink("esp32-devkitc"))
    runner.send(HELLO)
    runner.read()
    runner.sock.sendall(b"x" * (2 * MAX_LINE + 10) + b"\n")
    runner.send(Sync(0))
    assert runner.read() == Stop()
    runner.close()
    assert runner.link.error == f"the runner sent a line of more than {MAX_LINE} bytes"


def test_run_takes_a_link_closed_inside_a_line_too_long_for_a_dead_runner() -> None:
    errors: list[str] = []
    runner = FakeRunner(BoardLink("esp32-devkitc", on_error=errors.append))
    runner.send(HELLO)
    runner.read()
    runner.sock.sendall(b"x" * (2 * MAX_LINE + 10))
    runner.close()
    assert errors == [f"the runner sent a line of more than {MAX_LINE} bytes"]
    assert runner.link.error == errors[0]


def test_run_takes_a_link_closed_inside_a_line_for_a_dead_runner() -> None:
    runner = FakeRunner(BoardLink("esp32-devkitc"))
    runner.send(HELLO)
    runner.read()
    runner.sock.sendall(b'{"type":"sy')
    runner.close()
    assert runner.link.error == "the link ended inside a line: the runner died mid-message"


def test_run_keeps_reading_when_the_runner_has_closed_its_reading_end() -> None:
    runner = FakeRunner(BoardLink("esp32-devkitc"))
    runner.send(HELLO)
    runner.read()
    # As on SIGTERM in the harness's turn: the runner sends stats and closes.
    runner.sock.shutdown(socket.SHUT_RD)
    runner.send(Sync(0), Stats(0, "signal", 0, 1, (), 0, 0, 0, 0))
    runner.sock.shutdown(socket.SHUT_WR)
    runner.thread.join(5)
    assert not runner.thread.is_alive()
    assert runner.link.stats is not None
    assert runner.link.error is None


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (b"OK", "gnss.receive returned b'OK', not a (t_ns, bytes) pair"),
        ((1.5, b"OK"), "gnss.receive returned (1.5, b'OK'), not a (t_ns, bytes) pair"),
        ((10, "OK"), "gnss.receive returned (10, 'OK'), not a (t_ns, bytes) pair"),
    ],
)
def test_an_answer_that_is_not_a_time_and_bytes_names_the_interface(
    answer: object, error: str
) -> None:
    class Wrong(Peer):
        def receive(self, t: int, direction: Direction, data: bytes) -> Iterable[Output]:
            return [answer]  # type: ignore[list-item]

    link = started(Wrong())
    assert feed(link, tx(1, 10, b"AT\r"), Sync(20)) == [Stop()]
    assert link.error == error


def test_an_output_longer_than_a_line_goes_out_in_messages_of_one_time() -> None:
    data = bytes(range(256)) * 3600
    link = started(Peer(schedule=[(100, data)]))
    replies = link.receive(Sync(100))
    assert link.error is None
    chunks = replies[:-1]
    assert [len(chunk.data) for chunk in chunks] == [512 * 1024, len(data) - 512 * 1024]  # type: ignore[union-attr]
    assert chunks == [
        UartRx(100, 5, 9600, data[: 512 * 1024]),
        UartRx(100, 5, 9600, data[512 * 1024 :]),
    ]
    assert all(len(encode(chunk)) <= MAX_LINE for chunk in chunks)


def test_an_interface_without_rx_may_send_nothing() -> None:
    link = started(Peer(wire=Wire(tx=4, rx=None, baud=9600), schedule=[(100, b"")]))
    assert link.receive(Sync(100)) == [Wait(0, None, None)]


@pytest.mark.parametrize(
    ("scheduled", "error"),
    [
        (
            Adc(100, 33, float("nan")),
            "the scenario sent a adc the link cannot carry: Out of range float values",
        ),
        (
            GpioIn(100, ((33, 2),)),
            "the scenario sent a gpio_in the link cannot carry: gpio_in: levels holds level 2",
        ),
        (
            GpioIn(100, ((34, 1),)),
            "the scenario sent gpio_in on GPIO 34, which the link does not watch",
        ),
        (
            Release(90, (33,)),
            "the scenario sent release stamped 90 ns in the turn at 100 ns: it held it back past"
            " its quiet_until",
        ),
        (Sync(100), "the scenario sent Sync(t=100), which is not an input"),
    ],
)
def test_a_scenario_input_the_runner_would_refuse_is_refused_naming_the_scenario(
    scheduled: Input, error: str
) -> None:
    link = started(timeline=Scenario([]))
    link.timeline.inputs = [scheduled]  # type: ignore[union-attr]
    assert link.receive(Sync(100)) == [Stop()]
    assert link.error is not None and link.error.startswith(error), link.error


def test_a_fault_of_the_harness_s_own_still_stops_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    link = started()

    def broken(now: int) -> list[HarnessMessage]:
        raise RuntimeError("bug")

    monkeypatch.setattr(link, "_turn", broken)
    assert link.receive(Sync(10)) == [Stop()]
    assert link.error == "the harness failed on sync: RuntimeError: bug"


def test_a_turn_that_cannot_be_encoded_still_stops_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    errors: list[str] = []
    runner = FakeRunner(BoardLink("esp32-devkitc", on_error=errors.append))
    runner.send(HELLO)
    runner.read()

    def encode_no_wait(message: HarnessMessage) -> bytes:
        if isinstance(message, Wait):
            raise ValueError("unencodable")
        return encode(message)

    monkeypatch.setattr(boardlink, "encode", encode_no_wait)
    runner.send(Sync(0))
    assert runner.read() == Stop()
    runner.close()
    assert errors == ["the harness cannot encode its turn: ValueError: unencodable"]


def test_uart_cfg_takes_every_port_on_the_wire_into_account() -> None:
    gnss = Peer()
    link = started(gnss)
    feed(
        link,
        UartCfg(1, 10, 1, (4,), None, 9600),
        UartCfg(2, 20, 2, (), 5, 9600),
        # Port 1 moves off the wire: port 2 still receives on it.
        UartCfg(3, 30, 1, (13,), None, 9600),
        # A change that leaves the wire as it was tells the interface nothing.
        UartCfg(4, 40, 1, (12,), None, 115200),
        Sync(50),
    )
    assert gnss.routes == [(10, 9600, True, False), (20, 9600, True, True), (30, 9600, False, True)]


def test_scenario_pins_that_are_not_gpio_numbers_are_refused() -> None:
    with pytest.raises(ValueError, match="the scenario's pins hold 'RI', not a GPIO number"):
        BoardLink("esp32-devkitc", timeline=Scenario([], pins=["RI"]))  # type: ignore[list-item]


def test_a_held_link_takes_its_first_turn_once_started() -> None:
    link = BoardLink("esp32-devkitc", [Peer(schedule=[(0, b"$GPGGA\r\n")])], hold=True)
    link.receive(HELLO)
    assert link.held
    replies: list[HarnessMessage] = []
    turn = threading.Thread(target=lambda: replies.extend(link.receive(Sync(0))))
    turn.start()
    turn.join(0.2)
    # The runner waits in turn 0, before the guest's first instruction.
    assert turn.is_alive() and replies == []
    assert link.start()
    turn.join(5)
    assert not link.held
    assert replies == [UartRx(0, 5, 9600, b"$GPGGA\r\n"), Wait(0, None, None)]
    # Only the first start counts.
    assert not link.start(boot=False)
    assert link.receive(Sync(10)) == [Wait(0, None, None)]


def test_a_held_link_started_without_boot_stops_the_run_at_turn_0() -> None:
    gnss = Peer(schedule=[(0, b"$GPGGA\r\n")])
    link = BoardLink("esp32-devkitc", [gnss], hold=True)
    link.receive(HELLO)
    link.start(boot=False)
    assert link.receive(Sync(0)) == [Stop()]
    assert gnss.polls == [] and link.turns == 0
    assert link.error is None


def test_a_link_made_without_hold_is_not_held() -> None:
    link = BoardLink("esp32-devkitc", [Peer()])
    assert not link.held
    assert not link.start(boot=False)
    link.receive(HELLO)
    assert link.receive(Sync(0)) == [Wait(0, None, None)]


def test_a_runner_on_another_clock_than_the_backend_s_fails_the_link() -> None:
    errors: list[str] = []
    link = BoardLink("esp32-devkitc", clock="emulated-fifo-write", on_error=errors.append)
    assert link.receive(RunnerHello(1, "esp32", "wall", "0" * 40)) == [
        HarnessHello(1, "esp32", "esp32-devkitc", ())
    ]
    assert errors == ["the runner's clock is wall, the backend's emulated-fifo-write"]
    assert link.receive(Sync(0)) == [Stop()]
    # The same clock is no error.
    link = BoardLink("esp32-devkitc", clock="emulated-fifo-write")
    link.receive(HELLO)
    assert link.error is None
