"""A bridge from the simulated network to a real service on the host: services on loopback
sockets, driven through the network's turns. The waits are in wall time, as the bridge's are."""

import re
import socket
import struct
import threading
import time
from collections.abc import Callable, Iterator

import pytest

from esp32tl.net import LATENCY_NS, Internet
from esp32tl.net.bridge import MS
from esp32tl.net.ethernet import GATEWAY_MAC

DEVICE = ("10.64.0.2", 40000)
WAIT_SECONDS = 5


class Service:
    """A TCP service on loopback that runs `serve` on each connection, in a thread of its own."""

    def __init__(self, serve: Callable[[socket.socket], None]) -> None:
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port = self._listener.getsockname()[1]
        self.address = ("127.0.0.1", self.port)
        self._serve = serve
        self._threads: list[threading.Thread] = []
        self._accepting = threading.Thread(target=self._accept, daemon=True)
        self._accepting.start()

    def _accept(self) -> None:
        while True:
            try:
                connection, _ = self._listener.accept()
            except OSError:
                return
            thread = threading.Thread(target=self._run, args=(connection,), daemon=True)
            thread.start()
            self._threads.append(thread)

    def _run(self, connection: socket.socket) -> None:
        with connection:
            self._serve(connection)

    def close(self) -> None:
        self._listener.close()
        for thread in self._threads:
            thread.join(WAIT_SECONDS)


@pytest.fixture
def network() -> Iterator[Internet]:
    network = Internet(lambda: 0, link="ethernet")
    network.start(0)
    yield network
    network.close()


def upper(connection: socket.socket) -> None:
    """Answers each read in capitals, until the end of the stream."""
    while data := connection.recv(1024):
        connection.sendall(data.upper())


class Device:
    """The device's end of one connection to `port`, as a service sees it, without TCP."""

    def __init__(self, network: Internet, port: int) -> None:
        self.network = network
        self.received = bytearray()
        self.closed = 0
        self.connection = network.ip.tcp[port].accept(DEVICE, self.received.extend, self._close)

    def _close(self, *, reset: bool = False) -> None:
        self.closed += 1

    def until(self, done: Callable[[], bool], now: int = 0) -> None:
        """Turns at `now` until `done`, for at most WAIT_SECONDS of wall time."""
        deadline = time.monotonic() + WAIT_SECONDS
        while not done():
            assert time.monotonic() < deadline, "nothing came from the service"
            self.network.arrive(now)
            self.network.poll(now)
            time.sleep(0.001)


def events(network: Internet) -> list[object]:
    return [e["event"] for e in network.events if "event" in e]


def test_a_connection_goes_to_the_service_and_its_answer_comes_back(network: Internet) -> None:
    service = Service(upper)
    try:
        bridge = network.bridge(7000, service.address)
        device = Device(network, 7000)
        assert bridge.connections == 1
        device.connection.on_data(b"hello")
        device.until(lambda: device.received == b"HELLO")
        assert device.closed == 0
        assert events(network)[-2:] == [
            f"bridge: TCP 7000 → 127.0.0.1:{service.port}",
            f"bridge: 7000/1 connected to 127.0.0.1:{service.port}",
        ]
        streams = [(e["direction"], e["data"]) for e in network.events if "stream" in e]
        assert streams == [("out", b"hello".hex()), ("in", b"HELLO".hex())]
    finally:
        network.close()
        service.close()


def test_the_service_closing_closes_the_devices_end_after_its_last_bytes(
    network: Internet,
) -> None:
    def bye(connection: socket.socket) -> None:
        connection.sendall(b"bye")

    service = Service(bye)
    try:
        network.bridge(7000, service.address)
        device = Device(network, 7000)
        device.until(lambda: device.closed > 0)
        assert device.received == b"bye" and device.closed == 1
        assert events(network)[-1] == f"bridge: 7000/1 closed by 127.0.0.1:{service.port}"
        # Nothing is open: the bridge asks for no more turns.
        assert network.quiet_until() is None
    finally:
        network.close()
        service.close()


def test_a_service_that_refuses_closes_the_devices_end(network: Internet) -> None:
    # A port just closed. Another process could take it before the connect; a socket bound and
    # not listening would not do instead: macOS does not refuse a connection to it, it times out.
    unused = socket.create_server(("127.0.0.1", 0))
    port = unused.getsockname()[1]
    unused.close()
    network.bridge(7000, ("127.0.0.1", port))
    device = Device(network, 7000)
    device.until(lambda: device.closed > 0)
    assert device.received == b""
    [line] = [e for e in events(network) if str(e).startswith("bridge: 7000/1")]
    assert line == f"bridge: 7000/1 cannot connect to 127.0.0.1:{port}: Connection refused"


def test_the_device_closing_ends_the_stream_at_the_service(network: Internet) -> None:
    read = bytearray()
    ended = threading.Event()

    def reads(connection: socket.socket) -> None:
        while data := connection.recv(1024):
            read.extend(data)
        ended.set()

    service = Service(reads)
    try:
        network.bridge(7000, service.address)
        device = Device(network, 7000)
        device.connection.on_data(b"last words")
        device.connection.on_close()
        assert ended.wait(WAIT_SECONDS)
        assert read == b"last words"
        assert network.quiet_until() is None
    finally:
        network.close()
        service.close()


def test_what_the_service_sends_after_the_device_closed_is_dropped_and_logged(
    network: Internet,
) -> None:
    def answers_at_the_end(connection: socket.socket) -> None:
        while connection.recv(1024):
            pass
        connection.sendall(b"too late")

    service = Service(answers_at_the_end)
    try:
        network.bridge(7000, service.address)
        device = Device(network, 7000)
        device.connection.on_data(b"request")
        # The network's TCP closes both ways on the device's FIN: there is no half-close.
        device.connection.on_close()
        # Both ends closed and everything logged: the turns no longer look at it. A turn can come
        # between the service's bytes and its end of the stream, so the lines' order and the
        # number of drop lines depend on the threads: only what they add up to is fixed.
        device.until(lambda: network.bridges[7000]._open == [])
        assert device.received == b"" and device.closed == 0
        lines = [str(e) for e in events(network) if str(e).startswith("bridge: 7000/1 ")]
        assert f"bridge: 7000/1 closed by 127.0.0.1:{service.port}" in lines
        dropped = [
            re.fullmatch(
                rf"bridge: 7000/1 dropped (\d+) bytes 127\.0\.0\.1:{service.port} sent after"
                " the device closed its end",
                line,
            )
            for line in lines
            if " dropped " in line
        ]
        assert dropped and all(dropped)
        assert sum(int(match[1]) for match in dropped if match) == len(b"too late")
    finally:
        network.close()
        service.close()


def test_while_a_connection_is_open_the_network_asks_for_a_turn_every_millisecond() -> None:
    service = Service(upper)
    now = [2_500_000]
    timed = Internet(lambda: now[0], link="ethernet")
    try:
        timed.start(0)
        timed.bridge(7000, service.address)
        assert timed.quiet_until() is None
        Device(timed, 7000)
        assert timed.quiet_until() == 3 * MS
        now[0] = 3 * MS
        assert timed.quiet_until() == 4 * MS
    finally:
        timed.close()
        service.close()


def test_close_ends_a_connection_the_service_keeps_open(network: Internet) -> None:
    held = threading.Event()
    ended = threading.Event()

    def holds(connection: socket.socket) -> None:
        held.set()
        while connection.recv(1024):
            pass
        ended.set()

    service = Service(holds)
    try:
        bridge = network.bridge(7000, service.address)
        Device(network, 7000)
        assert held.wait(WAIT_SECONDS)
        # The service never ends the connection: a close that waited for it would not return.
        closing = threading.Thread(target=network.close, daemon=True)
        closing.start()
        closing.join(WAIT_SECONDS)
        assert not closing.is_alive(), "close waited for the service to end the connection"
        assert ended.wait(WAIT_SECONDS), "close left the service's connection open"
        assert all(not r._writer.is_alive() for r in bridge._all)
        network.close()  # twice does nothing
    finally:
        service.close()


def test_a_bridge_takes_the_place_of_the_broker_or_the_http_server(network: Internet) -> None:
    network.bridge(1883, ("127.0.0.1", 11883))
    network.bridge(80, ("localhost", 8080))
    assert set(network.bridges) == {1883, 80}
    assert events(network)[-2:] == [
        "bridge: TCP 1883 → 127.0.0.1:11883, in place of the broker",
        "bridge: TCP 80 → localhost:8080, in place of the HTTP server",
    ]
    with pytest.raises(ValueError, match="TCP port 1883 already has a service"):
        network.listen(1883, network.broker)
    with pytest.raises(ValueError, match=r"TCP port 80 already has a service"):
        network.bridge(80, ("127.0.0.1", 8081))


def test_what_a_bridge_refuses(network: Internet) -> None:
    network.listen(8883, network.broker)
    with pytest.raises(ValueError) as error:
        network.bridge(8883, ("127.0.0.1", 8883))
    assert str(error.value) == (
        "network.bridge(8883, ('127.0.0.1', 8883)): TCP port 8883 already has a service"
    )
    with pytest.raises(ValueError, match=r"the TCP port must be between 1 and 65535"):
        network.bridge(0, ("127.0.0.1", 1))
    with pytest.raises(TypeError) as type_error:
        network.bridge(7000, "127.0.0.1:1883")  # type: ignore[arg-type]
    assert str(type_error.value) == (
        "network.bridge(7000, '127.0.0.1:1883'): to is '127.0.0.1:1883', not a host and a port,"
        " as ('127.0.0.1', 1883)"
    )
    with pytest.raises(ValueError, match=r"the port of \('127.0.0.1', 70000\) is not 1 to 65535"):
        network.bridge(7000, ("127.0.0.1", 70000))
    assert network.bridges == {}


def test_the_backend_may_refuse_a_bridge() -> None:
    def refuse(call: str) -> None:
        raise RuntimeError(f"refused {call}")

    network = Internet(lambda: 0, bridging=refuse)
    with pytest.raises(RuntimeError, match=r"refused network\.bridge\(1883, \('h', 1\)\)"):
        network.bridge(1883, ("h", 1))
    assert network.bridges == {}
    # The broker stays.
    assert network.ip.tcp[1883].service is network.broker  # type: ignore[attr-defined]


# Through TCP: frames from the station, behind the access point.

STATION = bytes.fromhex("246f28001122")
SOURCE, DESTINATION = bytes([10, 64, 0, 2]), bytes([10, 64, 0, 1])


def check(data: bytes) -> int:
    padded = data + b"\0" * (len(data) % 2)
    total = sum(int.from_bytes(padded[i : i + 2], "big") for i in range(0, len(padded), 2))
    total = (total & 65535) + (total >> 16)
    total = (total & 65535) + (total >> 16)
    return total ^ 65535


def segment(seq: int, ack: int, flags: int, data: bytes = b"") -> bytes:
    """A TCP segment from the station's port 40000 to port 7000, in its IPv4 datagram and frame."""
    tcp = struct.pack(">HHIIBBHHH", 40000, 7000, seq, ack, 5 << 4, flags, 8192, 0, 0) + data
    pseudo = SOURCE + DESTINATION + struct.pack(">BBH", 0, 6, len(tcp))
    tcp = tcp[:16] + struct.pack(">H", check(pseudo + tcp)) + tcp[18:]
    head = struct.pack(">BBHHHBBH", 0x45, 0, 20 + len(tcp), 0, 0, 64, 6, 0) + SOURCE + DESTINATION
    ip = head[:10] + struct.pack(">H", check(head)) + head[12:] + tcp
    return GATEWAY_MAC + STATION + b"\x08\x00" + ip


def test_the_services_answer_reaches_the_station_in_a_tcp_segment_stamped_at_the_turn() -> None:
    service = Service(upper)
    now = [5]
    network = Internet(lambda: now[0], link="ethernet")
    try:
        network.start(0)
        network.bridge(7000, service.address)
        [(_, syn_ack)] = network.receive(5, segment(100, 0, 0x02))
        ours = int.from_bytes(syn_ack[14 + 24 : 14 + 28], "big")
        network.receive(10, segment(101, ours + 1, 0x10))
        network.receive(20, segment(101, ours + 1, 0x18, b"ping"))
        now[0] = MS
        deadline = time.monotonic() + WAIT_SECONDS
        answers: list[tuple[int, bytes]] = []
        while not answers:
            assert time.monotonic() < deadline, "nothing came from the service"
            network.arrive(now[0])
            answers = network.poll(now[0])
            time.sleep(0.001)
        [(t, frame)] = answers
        assert t == MS + LATENCY_NS
        assert frame[:6] == STATION and frame[14 + 40 :] == b"PING"
    finally:
        network.close()
        service.close()


def test_a_lost_link_leaves_the_stream_at_the_service_open_until_the_run_ends() -> None:
    ended = threading.Event()

    def reads(connection: socket.socket) -> None:
        while connection.recv(1024):
            pass
        ended.set()

    service = Service(reads)
    network = Internet(lambda: 0, link="ethernet")
    try:
        network.start(0)
        network.bridge(7000, service.address)
        [(_, syn_ack)] = network.receive(5, segment(100, 0, 0x02))
        ours = int.from_bytes(syn_ack[14 + 24 : 14 + 28], "big")
        network.receive(10, segment(101, ours + 1, 0x10))
        network.stop(20)
        # The service hears nothing of the loss, as a real one on the internet does not, and its
        # relay keeps its turns: what it sends or a close is taken in the next millisecond.
        assert network.quiet_until() == MS
        assert not ended.wait(0.2)
        assert not any("link lost" in str(event) for event in events(network))
        network.close()  # the run's end, here rather than in `finally`, to see the stream end
        assert ended.wait(WAIT_SECONDS)
    finally:
        network.close()
        service.close()
