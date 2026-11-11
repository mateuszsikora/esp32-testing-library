"""Unmodified esp-protocols modem-v1.4.1, through the LilyGO modem UART and PPP."""

import contextlib
import socket
import threading

import pytest

from esp32tl import recording
from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.interfaces import SourceError
from esp32tl.interfaces.condition import Condition
from esp32tl.interfaces.console import Console
from esp32tl.interfaces.modem import Modem
from esp32tl.net.mqtt import Broker, Message
from esp32tl.sources.replay import ReplayError
from esp32tl.units import seconds

pytestmark = [
    pytest.mark.app_path("../firmware/pppos_client"),
    pytest.mark.board_profile("lilygo-t-a7670g-r2"),
]


def test_publishes_its_test_message(board: Esp32Sim, modem: Modem, broker: Broker) -> None:
    board.power_on()
    message = board.wait_until(broker.next_message(topic="/topic/esp-pppos"), within=seconds(60))
    assert message.text == "esp32-pppos"
    assert modem.network().dns.asked == ["mqtt.eclipseprojects.io"]
    assert board.link.warnings == []
    assert "ppp: IPCP open" in board.report()
    routes = board.properties()["routes"]
    assert isinstance(routes, dict) and routes["uart.modem"]["network"]["events"]


def test_shows_what_the_cloud_sends_it(board: Esp32Sim, broker: Broker, console: Console) -> None:
    board.power_on()
    board.wait_until(broker.subscribed("/topic/esp-pppos"), within=seconds(60))
    broker.publish("/topic/esp-pppos", "hello from the test")
    board.wait_until(console.line(r"DATA=hello from the test"), within=seconds(5))
    assert board.link.warnings == []


def test_broker_refuses_a_missing_route_or_late_selection(pytester: pytest.Pytester) -> None:
    from pathlib import Path

    app = Path(__file__).resolve().parents[1] / "firmware/pppos_client"
    pytester.makepyfile(f"""
        import pytest
        pytestmark = pytest.mark.app_path({str(app)!r})

        def test_missing(broker):
            pass

        @pytest.mark.board_profile("lilygo-t-a7670g-r2")
        def test_late(board, modem):
            modem.signal(dbm=-73)
            board.power_on()
            modem.network()
    """)
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-rA")
    result.assert_outcomes(errors=1, failed=1)
    result.stdout.fnmatch_lines_random(
        [
            "*the board profile esp32-devkitc has no uart.modem*",
            "*uart.modem: the board is on already: give an interface its source before"
            " board.power_on()*",
        ]
    )


class _HostBroker:
    """A real service for a bridge to reach: esp32tl's broker behind a loopback socket, read and
    written by threads of its own, on no board's clock."""

    def __init__(self) -> None:
        self.broker = Broker(lambda: 0)
        self._lock = threading.Lock()
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.address = ("127.0.0.1", self._listener.getsockname()[1])
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                connection, peer = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(connection, peer), daemon=True).start()

    def _serve(self, connection: socket.socket, peer: tuple[str, int]) -> None:
        def close() -> None:
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)

        with connection:
            with self._lock:
                session = self.broker.accept(peer, connection.sendall, close)
            try:
                while data := connection.recv(4096):
                    with self._lock:
                        session.on_data(data)
            except OSError:
                pass
            with self._lock:
                session.on_close()

    def received(self, topic: str) -> Condition[Message | None]:
        def find() -> Message | None:
            with self._lock:
                return self.broker.find_message(topic=topic)

        return Condition(find, f"a message on {topic!r} at the host's broker", "MQTT")

    def close(self) -> None:
        self._listener.close()


def test_publishes_to_a_real_broker_through_a_bridge(board: Esp32Sim, modem: Modem) -> None:
    host = _HostBroker()
    try:
        network = modem.network()
        network.bridge(1883, host.address)
        board.power_on()
        message = board.wait_until(host.received("/topic/esp-pppos"), within=seconds(60))
        assert message.text == "esp32-pppos"
        assert board.link.runner is not None and board.link.runner.clock == recording.WALL
        assert network.broker.messages == [], "the bridge took the broker's place"
        assert network.dns.asked == ["mqtt.eclipseprojects.io"]
        assert board.link.warnings == []
    finally:
        host.close()


def test_a_bridge_refuses_a_replay_given_before_it(board: Esp32Sim, modem: Modem) -> None:
    board.uart("gnss").replay("../recordings/gnss-generated-1.jsonl")
    network = modem.network()
    with pytest.raises(SourceError) as error:
        network.bridge(1883, ("127.0.0.1", 1883))
    assert str(error.value) == (
        "network.bridge(1883, ('127.0.0.1', 1883)): a bridge puts the run on the wall clock, and"
        " uart.gnss replays a recording on the emulated-fifo-write clock: replaying it would"
        " compare times across clocks"
    )
    assert network.bridges == {} and board.clock == recording.EMULATED
    board.power_on()


def test_a_replay_given_after_a_bridge_takes_the_wall_clock_only(
    board: Esp32Sim, modem: Modem
) -> None:
    # A port just closed, so that the run reaches no broker of the host's.
    unused = socket.create_server(("127.0.0.1", 0))
    port = unused.getsockname()[1]
    unused.close()
    modem.network().bridge(1883, ("127.0.0.1", port))
    with pytest.raises(ReplayError) as error:
        board.uart("gnss").replay("../recordings/gnss-generated-1.jsonl")
    assert str(error.value) == (
        "../recordings/gnss-generated-1.jsonl has times on the emulated-fifo-write clock, and"
        " uart.gnss runs on the wall clock: replaying it would compare times across clocks"
    )
    assert board.uart("gnss").source is None
    board.power_on()
