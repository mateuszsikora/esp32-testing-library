"""A model the test writes with `Duration`s (`esp32tl.sources.model.Model`), on a UART route and
on an `SppPeer`: it does what the same model in nanoseconds does, at the same instants and with
the same bytes, and what it gets wrong is told as the model in nanoseconds is told it, in
`Duration`s."""

import asyncio
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest
from test_spp_peer import MS, Echo, Initiator, air, write_hello
from test_uart_model import AUX, HELLO, Wrapped, aux

from esp32tl import recording
from esp32tl.bluetooth.air import Air, AirError
from esp32tl.bluetooth.peer import PeerError, SppPeer
from esp32tl.interfaces.uart import Supplied, Uart
from esp32tl.link.boardlink import BoardLink, LinkError, Output, Wire
from esp32tl.link.protocol import Sync, UartTx
from esp32tl.sources.model import InNanoseconds, Model, Timed, in_nanoseconds
from esp32tl.units import Duration, ms, seconds

__all__ = ["air"]  # the fixture, which the SPP tests below take

# How long the part takes to answer a command line, and when it says it is ready, unprompted.
ANSWER = ms(1)
READY = ms(2)
NOW = ms(0)
# How long the SPP peer's echo takes, as `Echo`'s default.
ECHO = ms(50)


class AtInNanoseconds:
    """`OK` to every command line, `ANSWER` after its CR LF, and `READY` once, unprompted: in
    nanoseconds."""

    def __init__(self) -> None:
        self.heard: list[tuple[int, bytes]] = []
        self._buffer = b""
        self._ready = False

    def receive(self, t: int, data: bytes) -> Iterable[Output]:
        self.heard.append((t, data))
        self._buffer += data
        answers = []
        while b"\r\n" in self._buffer:
            _, _, self._buffer = self._buffer.partition(b"\r\n")
            answers.append((t + ANSWER.ns, b"\r\nOK\r\n"))
        return answers

    def poll(self, now: int) -> Iterable[Output]:
        if self._ready or now < READY.ns:
            return []
        self._ready = True
        return [(READY.ns, b"\r\nRDY\r\n")]

    def quiet_until(self) -> int | None:
        return None if self._ready else READY.ns

    def latency(self) -> int | None:
        return ANSWER.ns


class AtInDurations(Model):
    """`AtInNanoseconds`, with `Duration`s."""

    def __init__(self) -> None:
        self.heard: list[tuple[Duration, bytes]] = []
        self._buffer = b""
        self._ready = False

    def receive(self, at: Duration, data: bytes) -> Iterable[Timed]:
        self.heard.append((at, data))
        self._buffer += data
        answers = []
        while b"\r\n" in self._buffer:
            _, _, self._buffer = self._buffer.partition(b"\r\n")
            answers.append((at + ANSWER, b"\r\nOK\r\n"))
        return answers

    def poll(self, now: Duration) -> Iterable[Timed]:
        if self._ready or now < READY:
            return []
        self._ready = True
        return [(READY, b"\r\nRDY\r\n")]

    def quiet_until(self) -> Duration | None:
        return None if self._ready else READY

    def latency(self) -> Duration | None:
        return ANSWER


def exchange(uart: Uart) -> list[object]:
    """What the link sends the runner in a scripted exchange with the model on `uart`: two
    commands, one cut in two, and the unprompted `RDY` between them."""
    link = BoardLink("aux-uart", [uart])
    link.receive(HELLO)
    sent: list[object] = list(link.receive(Sync(0)))
    sent += link.receive(UartTx(1, 500, 1, (26,), 115200, b"AT\r\n"))
    sent += link.receive(Sync(500 + ANSWER.ns))
    sent += link.receive(Sync(READY.ns))
    sent += link.receive(UartTx(2, READY.ns + 10, 1, (26,), 115200, b"ATE"))
    sent += link.receive(UartTx(3, READY.ns + 20, 1, (26,), 115200, b"0\r\n"))
    sent += link.receive(Sync(READY.ns + 20 + ANSWER.ns))
    assert link.error is None
    return sent


def test_a_model_in_durations_says_on_a_uart_what_one_in_nanoseconds_says(
    tmp_path: Path,
) -> None:
    in_ns, _ = aux(tmp_path)
    in_durations, _ = aux(tmp_path)
    by_ns = in_ns.model(AtInNanoseconds())
    by_durations = in_durations.model(AtInDurations())
    assert exchange(in_durations) == exchange(in_ns)
    assert by_durations.heard == [(Duration(t), data) for t, data in by_ns.heard]
    assert in_durations.source is by_durations
    assert in_durations.recorded_source() == "model:AtInDurations"


class Misspelt(Model):
    def receive(self, at: Duration, data: bytes) -> Iterable[Timed]:
        return []

    def pol(self, now: Duration) -> Iterable[Timed]:
        return []

    def latency(self) -> Duration | None:
        return None


def test_a_model_without_one_of_the_four_methods_cannot_be_made() -> None:
    # Python 3.11 lists them as `poll, quiet_until`, 3.12 on as `'poll', 'quiet_until'`.
    with pytest.raises(TypeError, match=r"^Can't instantiate abstract class Misspelt .*poll"):
        Misspelt()  # type: ignore[abstract]


def test_a_model_without_supply_is_on_no_supply() -> None:
    assert not isinstance(in_nanoseconds(AtInDurations(), "uart.aux", LinkError), Supplied)


class Powered(AtInDurations):
    def __init__(self) -> None:
        super().__init__()
        self.supplied: list[bool] = []

    def supply(self, on: bool) -> None:
        self.supplied.append(on)


def test_a_model_in_durations_with_supply_is_on_the_board_s_supply(tmp_path: Path) -> None:
    uart, _ = aux(tmp_path)
    model = uart.model(Powered())
    uart.supply(False)
    uart.supply(True)
    assert model.supplied == [False, True]
    assert isinstance(in_nanoseconds(model, "uart.aux", LinkError), Supplied)


class Wrong(Model):
    """A model in `Duration`s that gets one thing wrong, `how`."""

    def __init__(self, how: str) -> None:
        self.how = how

    def receive(self, at: Duration, data: bytes) -> Iterable[Timed]:
        if self.how == "pair":
            return [(at.ns, b"OK")]  # type: ignore[list-item]
        if self.how == "raises":
            raise KeyError("model bug")
        if self.how == "early":
            return [(at, b"OK")]
        return []

    def poll(self, now: Duration) -> Iterable[Timed]:
        if self.how == "past":
            return [(now - ms(1), b"late")]
        return []

    def quiet_until(self) -> Duration | None:
        if self.how == "float":
            return 1.5  # type: ignore[return-value]
        if self.how == "now":
            return READY
        if self.how == "quiet-raises":
            raise KeyError("model bug")
        return None

    def latency(self) -> Duration | None:
        if self.how == "latency-raises":
            raise KeyError("model bug")
        if self.how == "latency-int":
            return 1_000_000  # type: ignore[return-value]
        if self.how == "latency-negative":
            return -ms(1)
        return ANSWER


@pytest.mark.parametrize(
    ("how", "message"),
    [
        ("pair", "uart.aux.receive returned (2000500, b'OK'), not a (Duration, bytes) pair"),
        ("raises", "uart.aux.receive raised KeyError: 'model bug'"),
        ("quiet-raises", "uart.aux.quiet_until raised KeyError: 'model bug'"),
        ("latency-raises", "uart.aux.latency raised KeyError: 'model bug'"),
        (
            "early",
            "uart.aux sent bytes stamped 0.0020005 s in the turn at 0.0030005 s: it answered"
            " sooner than its latency, or sent past its quiet_until",
        ),
        (
            "past",
            "uart.aux sent bytes stamped 0.001 s in the turn at 0.002 s: it answered sooner than"
            " its latency, or sent past its quiet_until",
        ),
        ("float", "uart.aux.quiet_until returned 1.5, not a Duration"),
        (
            "now",
            "uart.aux is quiet until 0.002 s, not after the turn at 0.002 s: what it sends by"
            " then belongs in this turn",
        ),
        ("latency-int", "uart.aux declares a latency of 1000000, not a Duration"),
        ("latency-negative", "uart.aux declares a latency of -0.001 s"),
    ],
)
def test_what_a_model_in_durations_gets_wrong_on_a_uart_ends_the_run_in_durations(
    tmp_path: Path, how: str, message: str
) -> None:
    uart = Uart("uart.aux", AUX, clock=recording.EMULATED, directory=tmp_path)
    uart.model(Wrong(how))
    link = BoardLink("aux-uart", [uart])
    link.receive(HELLO)
    link.receive(Sync(READY.ns))
    if link.error is None:
        link.receive(UartTx(1, READY.ns + 500, 1, (26,), 115200, b"AT\r\n"))
        link.receive(Sync(READY.ns + 500 + ANSWER.ns))
    assert link.error == message


class EarlyInNanoseconds(AtInNanoseconds):
    """`Wrong("early")` in nanoseconds: answers at the instant it hears."""

    def receive(self, t: int, data: bytes) -> Iterable[Output]:
        return [(t, b"OK")]


class FailsToPoll(AtInNanoseconds):
    def poll(self, now: int) -> Iterable[Output]:
        raise KeyError("model bug")


@pytest.mark.parametrize("early", [EarlyInNanoseconds, lambda: Wrong("early")])
def test_with_two_faults_in_one_turn_both_kinds_report_the_same_one(
    tmp_path: Path, early: Callable[[], AtInNanoseconds | Model]
) -> None:
    route = Uart("uart.aux", AUX, clock=recording.EMULATED, directory=tmp_path)
    route.model(early())
    gnss = Uart(
        "uart.gnss", Wire(tx=4, rx=5, baud=9600), clock=recording.EMULATED, directory=tmp_path
    )
    gnss.model(FailsToPoll())
    link = BoardLink("aux-uart", [route, gnss])
    link.receive(HELLO)
    link.receive(UartTx(1, 500, 1, (26,), 115200, b"AT\r\n"))
    link.receive(Sync(500 + ANSWER.ns))
    # The link polls every interface before it checks a stamp.
    assert link.error == "uart.gnss.poll raised KeyError: 'model bug'"


class Proxy(Wrapped):
    """`Wrapped`, which also says it is of its model's class, as `isinstance` asks."""

    @property  # type: ignore[misc]
    def __class__(self) -> type:
        return type(self._inner)


def test_a_model_behind_a_proxy_is_called_in_nanoseconds_unless_it_says_its_class(
    tmp_path: Path,
) -> None:
    hidden, _ = aux(tmp_path)
    hidden.model(Wrapped(AtInDurations()))  # type: ignore[type-var]
    link = BoardLink("aux-uart", [hidden])
    link.receive(HELLO)
    link.receive(UartTx(1, 500, 1, (26,), 115200, b"AT\r\n"))
    assert link.error == (
        "uart.aux.receive raised TypeError: unsupported operand type(s) for +: 'int' and 'Duration'"
    )
    seen, _ = aux(tmp_path)
    seen.model(Proxy(AtInDurations()))  # type: ignore[type-var]
    assert list(seen.receive(500, "out", b"AT\r\n")) == [(500 + ANSWER.ns, b"\r\nOK\r\n")]


def test_the_adapter_names_what_it_refuses_with_the_caller_s_error() -> None:
    model = InNanoseconds(Wrong("pair"), "bench: source", PeerError)
    with pytest.raises(PeerError, match=r"^bench: source\.receive returned \(5, b'OK'\)"):
        model.receive(5, b"AT\r\n")


class EchoInDurations(Model):
    """`Echo` of the SPP tests, with `Duration`s."""

    def __init__(self, delay: Duration = ECHO, greeting: bytes = b"", at: Duration = NOW) -> None:
        self.delay = delay
        self.greeting = greeting
        self.at = at
        self.heard: list[tuple[Duration, bytes]] = []
        self._greeted = not greeting

    def receive(self, at: Duration, data: bytes) -> Iterable[Timed]:
        self.heard.append((at, data))
        return [(at + self.delay, data.rstrip(b"\r") + b"\r\n> ")]

    def poll(self, now: Duration) -> Iterable[Timed]:
        if self._greeted or now < self.at:
            return ()
        self._greeted = True
        return [(self.at, self.greeting)]

    def quiet_until(self) -> Duration | None:
        return None if self._greeted else self.at

    def latency(self) -> Duration | None:
        return self.delay


def chat(source: Echo | EchoInDurations) -> tuple[list[object], list[object]]:
    """What crossed the channel of a peer that `source` answers, as the peer and the device saw
    it: a greeting at 1.5 s, unprompted, and three lines."""
    air = Air()
    try:
        peer = SppPeer(air, name="peer", pin="1234", source=source)
        esp32 = Initiator(air)

        async def talk() -> None:
            dlc = await esp32.session()
            await asyncio.sleep(1)
            for command in (b"one\r", b"two\r", b"three\r"):
                dlc.write(command)
                await asyncio.sleep(0.02)
            await asyncio.sleep(1)

        air.run(talk(), within=seconds(10))
        return list(peer.events), list(esp32.received)
    finally:
        air.close()


def test_a_model_in_durations_says_on_an_spp_peer_what_one_in_nanoseconds_says() -> None:
    by_ns = Echo(delay=7 * MS, greeting=b"ready\r", at=1500 * MS)
    by_durations = EchoInDurations(delay=ms(7), greeting=b"ready\r", at=ms(1500))
    expected = chat(by_ns)
    assert (1500 * MS, "in", b"ready\r") in expected[0]
    assert chat(by_durations) == expected
    assert by_durations.heard == [(Duration(t), data) for t, data in by_ns.heard]


class WrongOnTheAir(EchoInDurations):
    def __init__(self, how: str) -> None:
        super().__init__()
        self.how = how

    def receive(self, at: Duration, data: bytes) -> Iterable[Timed]:
        if self.how == "pair":
            return [(at, "text")]  # type: ignore[list-item]
        if self.how == "past":
            return [(at - Duration(1), b"too early")]
        if self.how == "raises":
            raise KeyError("model bug")
        return super().receive(at, data)

    def quiet_until(self) -> Duration | None:
        if self.how == "float":
            return 1.5  # type: ignore[return-value]
        if self.how == "now":
            return ms(0)
        return super().quiet_until()


@pytest.mark.parametrize(
    ("how", "message"),
    [
        (
            "pair",
            "bench: source.receive returned (seconds(0), 'text'), not a (Duration, bytes) pair",
        ),
        (
            "past",
            "bench: source.receive sent b'too early' at -0.000000001 s, and the air is at 0 s:"
            " it cannot send into the past",
        ),
        ("raises", "bench: source.receive raised KeyError: 'model bug'"),
    ],
)
def test_what_a_model_in_durations_answers_wrong_fails_the_air_in_durations(
    air: Air, how: str, message: str
) -> None:
    SppPeer(air, name="bench", pin="1234", source=WrongOnTheAir(how))
    with pytest.raises(AirError) as raised:
        write_hello(air, Initiator(air), "bench")
    assert str(raised.value) == message
    assert isinstance(raised.value.__cause__, PeerError)


@pytest.mark.parametrize(
    ("how", "message"),
    [
        ("float", "bench: source.quiet_until returned 1.5, not a Duration"),
        (
            "now",
            "bench: source.quiet_until returned 0 s, not after the air's 0 s: what it sends by"
            " then belongs in the poll at 0 s",
        ),
    ],
)
def test_a_model_in_durations_whose_quiet_until_is_wrong_fails_the_air(
    air: Air, how: str, message: str
) -> None:
    SppPeer(air, name="bench", pin="1234", source=WrongOnTheAir(how))
    with pytest.raises(AirError) as raised:
        air.wait(seconds(1))
    assert str(raised.value) == message
    assert isinstance(raised.value.__cause__, PeerError)


class PoweredEcho(EchoInDurations):
    def __init__(self) -> None:
        super().__init__()
        self.supplied: list[bool] = []

    def supply(self, on: bool) -> None:
        self.supplied.append(on)


def test_a_model_in_durations_with_supply_is_on_the_peer_s_supply(air: Air) -> None:
    source = PoweredEcho()
    peer = SppPeer(air, name="peer", pin="1234", source=source)
    peer.switch_off()
    peer.switch_on()
    assert source.supplied == [False, True]
