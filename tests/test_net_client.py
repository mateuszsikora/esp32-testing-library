"""Connections the test opens to the device, behind PPP and behind Ethernet, against a device made
of handmade segments: the handshake, data both ways, both closes, refused, reset, the device's
zero window, a segment out of order, and connecting before the device has an address."""

import struct
from collections.abc import Callable, Iterator

import pytest
from test_net_ethernet import dhcp
from test_net_ip import check
from test_net_ppp import control, frame, unframe

from esp32tl.net import Internet, Link
from esp32tl.net.client import ConnectionEnded, NotReachable
from esp32tl.net.ethernet import GATEWAY_MAC
from esp32tl.net.ip import ACK, FIN, ISN, PSH, RST, SYN, OutOfOrder
from esp32tl.units import Duration

DEVICE, NETWORK = bytes([10, 64, 0, 2]), bytes([10, 64, 0, 1])
STATION = bytes.fromhex("246f28001122")
GUARD = b"\xef\xbe\xad\xde"
MS = 1_000_000


class Segment:
    """One TCP segment the network sent, its checksums checked."""

    def __init__(self, datagram: bytes) -> None:
        assert check(datagram[:20]) == 0 and datagram[9] == 6
        assert datagram[12:16] == NETWORK and datagram[16:20] == DEVICE
        tcp = datagram[20:]
        assert check(NETWORK + DEVICE + struct.pack(">BBH", 0, 6, len(tcp)) + tcp) == 0
        self.sport, self.dport, self.seq, self.ack, offset, self.flags, self.window = struct.unpack(
            ">HHIIBBH", tcp[:16]
        )
        self.options = tcp[20 : (offset >> 4) * 4]
        self.data = tcp[(offset >> 4) * 4 :]


class Device:
    """The device's end of the link, its TCP by hand: `send` puts one segment from it on the link
    and returns the segments the network answers with, in order."""

    def __init__(self, link: Link) -> None:
        self.now = MS
        self.link = link
        self.net = Internet(lambda: self.now, link=link)
        self.net.start(0)

    def up(self) -> "Device":
        """The device's address taken: IPCP open, or DHCP acknowledged."""
        if self.link == "ppp":
            self.net.poll(0)
            self.net.receive(0, control(0xC021, 1, 7))
            self.net.receive(0, control(0xC021, 2, 1))
            self.net.receive(0, control(0x8021, 2, 2, b"\x03\x06\x0a\x40\0\x01"))
            self.net.receive(0, control(0x8021, 1, 8, b"\x03\x06\x0a\x40\0\x02"))
            assert self.net.ppp.ipcp_open
        else:
            self.net.receive(0, dhcp(1))
            self.net.receive(0, dhcp(3, options=bytes([50, 4]) + DEVICE))
            assert self.net.ethernet.device_address == "10.64.0.2"
        return self

    def _wrap(self, datagram: bytes) -> bytes:
        if self.link == "ppp":
            return frame(0x21, datagram)
        return GATEWAY_MAC + STATION + b"\x08\x00" + datagram + GUARD

    def _unwrap(self, raw: bytes) -> bytes | None:
        if self.link == "ppp":
            return unframe(raw)
        assert raw[:6] == STATION and raw[6:12] == GATEWAY_MAC
        return raw[14:] if raw[12:14] == b"\x08\x00" else None

    def _segments(self, outputs: list[tuple[int, bytes]]) -> list[Segment]:
        datagrams = (self._unwrap(raw) for _, raw in outputs)
        return [Segment(d) for d in datagrams if d is not None]

    def poll(self) -> list[Segment]:
        return self._segments(self.net.poll(self.now))

    def send(
        self,
        sport: int,
        dport: int,
        seq: int,
        ack: int,
        flags: int,
        data: bytes = b"",
        *,
        window: int = 8192,
        options: bytes = b"",
    ) -> list[Segment]:
        tcp = (
            struct.pack(
                ">HHIIBBHHH",
                sport,
                dport,
                seq,
                ack,
                (5 + len(options) // 4) << 4,
                flags,
                window,
                0,
                0,
            )
            + options
            + data
        )
        pseudo = DEVICE + NETWORK + struct.pack(">BBH", 0, 6, len(tcp))
        tcp = tcp[:16] + struct.pack(">H", check(pseudo + tcp)) + tcp[18:]
        head = struct.pack(">BBHHHBBH", 0x45, 0, 40 + len(tcp) - 20, 0, 0, 64, 6, 0)
        head += DEVICE + NETWORK
        datagram = head[:10] + struct.pack(">H", check(head)) + head[12:] + tcp
        return self._segments(self.net.receive(self.now, self._wrap(datagram)))

    def events(self) -> list[object]:
        return [e.get("event") for e in self.net.events]


class SearchedBytes(bytearray):
    """`received` that counts in `searched` the bytes its `find`s have looked through."""

    searched = 0

    def find(self, sub: object, start: object = None, *args: object) -> int:
        assert isinstance(start, int)
        self.searched += len(self) - start
        return super().find(sub, start, *args)  # type: ignore[arg-type]


def mss(value: int) -> bytes:
    return struct.pack(">BBH", 2, 4, value)


@pytest.fixture(params=["ppp", "ethernet"])
def device(request: pytest.FixtureRequest) -> Iterator[Device]:
    yield Device(request.param).up()


def test_the_test_opens_exchanges_and_closes_a_connection(device: Device) -> None:
    connection = device.net.connect(80)
    [syn] = device.poll()
    assert (syn.sport, syn.dport, syn.seq, syn.flags) == (49152, 80, ISN, SYN)
    assert syn.options == mss(1400) and syn.window == 8192
    assert (connection.address, connection.port, connection.local_port) == ("10.64.0.2", 80, 49152)
    assert connection.state == "connecting" and connection.established() is None
    # Written before the device answers: it waits for the handshake, then goes in MSS pieces.
    connection.write(b"GET /")
    assert device.poll() == []
    device.now = 2 * MS
    ack, first, second, third = device.send(80, 49152, 1000, ISN + 1, SYN | ACK, options=mss(2))
    assert (ack.flags, ack.seq, ack.ack) == (ACK, ISN + 1, 1001)
    assert [s.data for s in (first, second, third)] == [b"GE", b"T ", b"/"]
    assert first.flags == ACK | PSH
    assert connection.established() == Duration(2 * MS)
    assert connection.state == "established"
    # The device's answer, in two segments.
    head = connection.next_data(until=b"\r\n")
    whole = connection.next_data(size=7, start=0)
    device.now = 3 * MS
    [acked] = device.send(80, 49152, 1001, ISN + 6, ACK | PSH, b"ok\r")
    assert acked.ack == 1004 and head() is None and whole() is None
    [acked] = device.send(80, 49152, 1004, ISN + 6, ACK | PSH, b"\nbody")
    assert acked.ack == 1009
    assert connection.text == "ok\r\nbody"
    assert head() == b"ok\r\n" and whole() == b"ok\r\nbod" and connection.received == b"ok\r\nbody"
    assert connection.next_data()() is None
    # A retransmission is acknowledged again, and nothing is received twice.
    [again] = device.send(80, 49152, 1004, ISN + 6, ACK | PSH, b"\nbody")
    assert again.ack == 1009 and connection.received == b"ok\r\nbody"
    # The test closes first, then the device.
    connection.close()
    connection.close()
    [fin] = device.poll()
    assert (fin.flags, fin.seq) == (FIN | ACK, ISN + 6)
    with pytest.raises(ValueError, match=r"written after close\(\)"):
        connection.write(b"more")
    device.now = 4 * MS
    assert device.send(80, 49152, 1009, ISN + 7, ACK) == []
    assert connection.closed() is None
    [last] = device.send(80, 49152, 1009, ISN + 7, FIN | ACK)
    assert (last.flags, last.ack) == (ACK, 1010)
    assert connection.closed() == Duration(4 * MS)
    assert connection.state == "closed" and connection.ended_at == Duration(4 * MS)
    assert device.net.connections == [connection]
    name = "net: TCP 10.64.0.1:49152 → 10.64.0.2:80"
    events = device.events()
    assert [f"{name} connecting", f"{name} established", f"{name} closed"] == [
        e for e in events if isinstance(e, str) and e.startswith(name)
    ]
    streams = [e for e in device.net.events if "stream" in e]
    # What the test wrote is in the transcript as it went: in the MSS pieces, once established.
    assert [(e["t_ns"], e["stream"], e["direction"], e["data"]) for e in streams] == [
        (2 * MS, "device:80/1", "in", b"GE".hex()),
        (2 * MS, "device:80/1", "in", b"T ".hex()),
        (2 * MS, "device:80/1", "in", b"/".hex()),
        (3 * MS, "device:80/1", "out", b"ok\r".hex()),
        (3 * MS, "device:80/1", "out", b"\nbody".hex()),
    ]
    assert any(line.endswith("stream device:80/1 in 4745 (hex)") for line in device.net.report())


def test_the_device_closes_first_and_the_tests_end_follows(device: Device) -> None:
    connection = device.net.connect(8080)
    device.poll()
    device.send(8080, 49152, 0, ISN + 1, SYN | ACK)
    data = connection.next_data(until=b"!")
    # The data acknowledged, then the FIN, then the test's end closed in the same turn.
    data_ack, fin_ack, fin = device.send(8080, 49152, 1, ISN + 1, FIN | ACK | PSH, b"bye")
    assert (data_ack.ack, fin_ack.ack) == (4, 5)
    assert (fin.flags, fin.seq, fin.ack) == (FIN | ACK, ISN + 1, 5)
    assert connection.closed() == Duration(MS) and connection.received == b"bye"
    with pytest.raises(ConnectionEnded, match=r"closed by the device before the device sent b'!'"):
        data()
    with pytest.raises(ConnectionEnded, match=r"closed by the device, which closed the test's"):
        connection.write(b"late")
    device.send(8080, 49152, 5, ISN + 2, ACK)
    assert connection.state == "closed"
    assert connection.closed() == Duration(MS)


def test_a_rst_to_the_syn_is_refused_in_the_condition_and_the_log(device: Device) -> None:
    connection = device.net.connect(8080)
    connection.write("never sent")
    device.poll()
    refused = connection.refused
    # A RST that does not acknowledge the SYN is not the device's answer to it.
    assert device.send(8080, 49152, 0, ISN, RST | ACK) == [] and refused() is None
    assert device.send(8080, 49152, 0, ISN + 1, RST | ACK) == []
    assert refused() == Duration(MS) and connection.state == "refused"
    with pytest.raises(ConnectionEnded) as error:
        connection.established()
    assert str(error.value) == (
        "TCP 10.64.0.1:49152 → 10.64.0.2:8080 was refused by the device before it was established"
    )
    with pytest.raises(ConnectionEnded, match=r"was refused by the device, not reset by the"):
        connection.reset()
    with pytest.raises(ConnectionEnded, match=r"refused by the device: nothing more reaches"):
        connection.write(b"x")
    connection.close()  # nothing to close
    assert "net: TCP 10.64.0.1:49152 → 10.64.0.2:8080 refused by the device" in device.events()
    assert not [e for e in device.net.events if "stream" in e], "nothing reached the device"


def test_a_rst_on_an_established_connection_is_a_reset(device: Device) -> None:
    connection = device.net.connect(1234)
    device.poll()
    device.send(1234, 49152, 0, ISN + 1, SYN | ACK)
    with pytest.raises(ConnectionEnded, match=r"was established, not refused by the device"):
        connection.refused()
    data = connection.next_data()
    device.now = 5 * MS
    device.send(1234, 49152, 1, ISN + 1, RST)
    assert connection.reset() == Duration(5 * MS) and connection.state == "reset"
    assert connection.established() == Duration(MS)
    with pytest.raises(ConnectionEnded, match=r"was reset by the device, not closed by the device"):
        connection.closed()
    with pytest.raises(
        ConnectionEnded, match=r"reset by the device before the device sent anything"
    ):
        data()
    assert "net: TCP 10.64.0.1:49152 → 10.64.0.2:1234 reset by the device" in device.events()


def test_a_zero_window_holds_the_data_until_the_devices_update(device: Device) -> None:
    connection = device.net.connect(80)
    device.poll()
    [ack] = device.send(80, 49152, 0, ISN + 1, SYN | ACK, window=0)
    assert ack.ack == 1
    connection.write(b"abcdef")
    connection.close()
    assert device.poll() == [], "nothing goes into a zero window, and no probe"
    [piece] = device.send(80, 49152, 1, ISN + 1, ACK, window=4)
    assert piece.data == b"abcd"
    assert device.send(80, 49152, 1, ISN + 5, ACK, window=0) == []
    rest, fin = device.send(80, 49152, 1, ISN + 5, ACK, window=8)
    assert (rest.data, fin.flags, fin.seq) == (b"ef", FIN | ACK, ISN + 7)


def test_a_segment_ahead_of_the_stream_fails_the_turn(device: Device) -> None:
    connection = device.net.connect(80)
    device.poll()
    device.send(80, 49152, 0, ISN + 1, SYN | ACK)
    line = (
        "net: TCP 10.64.0.1:49152 → 10.64.0.2:80: the device sent sequence 5, 4 bytes ahead of 1,"
        " which a link that loses nothing cannot produce"
    )
    with pytest.raises(OutOfOrder) as error:
        device.send(80, 49152, 5, ISN + 1, ACK | PSH, b"late")
    assert str(error.value) == line and line in device.events()
    assert connection.received == b""


def test_ports_count_up_from_49152_and_a_lost_link_ends_every_connection(device: Device) -> None:
    first, second = device.net.connect(80), device.net.connect(80)
    assert (first.local_port, second.local_port) == (49152, 49153)
    assert (first.identity, second.identity) == ("device:80/1", "device:80/2")
    device.poll()
    device.send(80, 49152, 0, ISN + 1, SYN | ACK)
    device.net.stop(7 * MS)
    for connection in (first, second):
        assert connection.state == "lost" and connection.ended_at == Duration(7 * MS)
    with pytest.raises(ConnectionEnded, match=r"lost with the link before it was established"):
        second.established()
    assert "net: TCP 10.64.0.1:49152 → 10.64.0.2:80 link lost" in device.events()
    device.net.start(8 * MS)
    device.up()
    assert device.net.connect(80).local_port == 49154, "ports are never reused"


def test_conditions_are_named_and_refuse_to_be_read_as_booleans(device: Device) -> None:
    connection = device.net.connect(80)
    name = "TCP 10.64.0.1:49152 → 10.64.0.2:80"
    assert repr(connection.established) == f"{name} established"
    assert repr(connection.closed) == f"{name} closed by the device"
    assert repr(connection.refused) == f"{name} refused by the device"
    assert repr(connection.reset) == f"{name} reset by the device"
    assert repr(connection.next_data()) == f"the next data on {name}"
    assert repr(connection.next_data("\r\n")) == f"the next data on {name} up to b'\\r\\n'"
    assert repr(connection.next_data(size=3)) == f"the next data on {name}, 3 bytes"
    with pytest.raises(TypeError, match=r"call the TCP condition with \(\)"):
        bool(connection.established)
    with pytest.raises(ValueError, match="takes until or size, not both"):
        connection.next_data(b"x", size=1)
    with pytest.raises(ValueError, match="size is 0, not a count of bytes"):
        connection.next_data(size=0)
    with pytest.raises(ValueError, match="start is 1, outside the 0 bytes received"):
        connection.next_data(start=1)
    with pytest.raises(TypeError, match="write takes bytes or str"):
        connection.write(3)  # type: ignore[arg-type]
    assert repr(connection) == f"<{name}, connecting>"


def test_connecting_before_the_device_has_an_address_says_so() -> None:
    ppp = Device("ppp")
    with pytest.raises(NotReachable) as error:
        ppp.net.connect(80)
    assert str(error.value) == (
        "network.connect(80): the device has no address yet: PPP has not opened IPCP"
    )
    ethernet = Device("ethernet")
    with pytest.raises(NotReachable) as error:
        ethernet.net.connect(80)
    assert str(error.value) == (
        "network.connect(80): the device has no address yet: DHCP has acknowledged none on this"
        " link; wait for network.dhcp.next_lease(), or give address= for a static one"
    )
    with pytest.raises(NotReachable) as error:
        ethernet.net.connect(80, address="10.64.0.9")
    assert str(error.value) == (
        "network.connect(80, address='10.64.0.9'): the station has sent no frame yet: the network"
        " has no MAC to send to"
    )
    # An offer is not an address, and a device that asks again has none until it is acknowledged.
    ethernet.net.receive(0, dhcp(1))
    with pytest.raises(NotReachable, match="DHCP has acknowledged none"):
        ethernet.net.connect(80)
    assert ethernet.net.device_address is None
    # A static address needs only the station's MAC.
    assert ethernet.net.connect(80, address="10.64.0.9").address == "10.64.0.9"
    ethernet.up()
    assert ethernet.net.device_address == "10.64.0.2"
    ethernet.net.stop(MS)
    ethernet.net.start(MS)
    with pytest.raises(NotReachable) as error:
        ethernet.net.connect(80)
    assert "on this link; wait for" in str(error.value), "a fresh link has had nothing to lose"
    ethernet.net.stop(MS)
    with pytest.raises(NotReachable) as error:
        ethernet.net.connect(80)
    assert str(error.value) == (
        "network.connect(80): the access point is off the air: the network reaches no device"
    )
    assert Device("ppp").up().net.device_address == "10.64.0.2"
    assert ppp.net.connections == []


@pytest.mark.parametrize(
    ("port", "address", "message"),
    [
        (0, None, r"network.connect\(0\): the TCP port must be between 1 and 65535"),
        (True, None, r"network.connect\(True\): the TCP port"),
        (80, "device", r"network.connect\(80, address='device'\): 'device' is not an IPv4"),
    ],
)
def test_what_is_not_a_port_or_an_address_is_refused(
    port: int, address: str | None, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        Device("ethernet").up().net.connect(port, address=address)


def test_text_is_written_as_utf_8_and_received_text_is_decoded(device: Device) -> None:
    connection = device.net.connect(80)
    device.poll()
    device.send(80, 49152, 0, ISN + 1, SYN | ACK)
    assert device.send(80, 49152, 1, ISN + 1, ACK) == [], "nothing to send yet"
    connection.write("zażółć")
    [segment] = device.poll()
    assert segment.data == "zażółć".encode()
    device.send(80, 49152, 1, ISN + 1 + len(segment.data), ACK | PSH, b"\xc5\xbc\xff")
    assert connection.received == b"\xc5\xbc\xff" and connection.text == "ż\ufffd"


@pytest.mark.parametrize(
    ("why", "how"),
    [
        ("the device reset", "when the device reset"),
        ("the device lost its power", "when the device lost its power"),
    ],
)
def test_a_device_that_resets_loses_the_tests_connections_and_its_address(
    device: Device, why: str, how: str
) -> None:
    from test_net_ip import Echo

    echo = Echo()
    device.net.ip.tcp[9000] = echo
    # The device's own connection to a service, and the test's to the device.
    device.send(4000, 9000, 1, 0, SYN)
    device.send(4000, 9000, 2, ISN + 1, ACK)
    connection = device.net.connect(80)
    device.poll()
    device.send(80, 49152, 0, ISN + 1, SYN | ACK)
    closed = connection.closed
    device.net.device_reset(9 * MS, why)
    assert connection.state == "lost" and connection.ended_at == Duration(9 * MS)
    with pytest.raises(ConnectionEnded) as error:
        closed()
    assert str(error.value) == (
        f"TCP 10.64.0.1:49152 → 10.64.0.2:80 was lost {how}, not closed by the device"
    )
    assert f"net: TCP 10.64.0.1:49152 → 10.64.0.2:80 lost: {why}" in device.events()
    assert echo.closed == 0 and len(device.net.ip._flows) == 1, "the device's own stays"
    assert device.net.device_address is None
    with pytest.raises(NotReachable) as refused:
        device.net.connect(80)
    if device.link == "ppp":
        assert str(refused.value) == (
            "network.connect(80): the device has no address yet: PPP has not opened IPCP since"
            " the device reset"
        )
    else:
        assert "DHCP has acknowledged none on this link since the device last reset" in str(
            refused.value
        )
    # The device takes its address again: IPCP opens afresh, or DHCP acknowledges it.
    if device.link == "ppp":
        device.net.receive(MS, control(0xC021, 1, 9))
        device.net.receive(MS, control(0xC021, 2, device.net.ppp._id))
        ident = device.net.ppp._requests[0x8021][0]
        device.net.receive(MS, control(0x8021, 2, ident, b"\x03\x06\x0a\x40\0\x01"))
        device.net.receive(MS, control(0x8021, 1, 10, b"\x03\x06\x0a\x40\0\x02"))
    else:
        device.net.receive(MS, dhcp(3, options=bytes([50, 4]) + DEVICE))
    assert device.net.device_address == "10.64.0.2"
    assert device.net.connect(80).local_port == 49153


def test_a_dhcp_release_gives_the_address_up() -> None:
    device = Device("ethernet").up()
    # An INFORM is from a client with an address of its own: it changes nothing.
    device.net.receive(MS, dhcp(8, ciaddr=DEVICE))
    assert device.net.device_address == "10.64.0.2"
    device.net.receive(MS, dhcp(7, ciaddr=DEVICE, options=bytes([54, 4]) + NETWORK))
    assert device.net.device_address is None
    assert "dhcp: RELEASE from 24:6f:28:00:11:22" in device.events()


@pytest.mark.parametrize("framing", ["length", "chunked", "close"])
def test_http_client_over_the_network_path(device: Device, framing: str) -> None:
    exchange = device.net.http_client.post("/echo", json={"ok": True}, port=8080)
    [syn] = device.poll()
    assert syn.dport == 8080
    device.now += MS
    sent = device.send(8080, syn.sport, 1000, ISN + 1, SYN | ACK)
    request = b"".join(segment.data for segment in sent)
    assert request.startswith(b"POST /echo HTTP/1.1\r\nHost: 10.64.0.2:8080\r\n")
    assert request.endswith(b'{"ok": true}')
    ack = ISN + 1 + len(request)
    response = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
    if framing == "length":
        response += b'Content-Length: 11\r\n\r\n{"ok":true}'
    elif framing == "chunked":
        response += b'Transfer-Encoding: chunked\r\n\r\nb\r\n{"ok":true}\r\n0\r\n\r\n'
    else:
        response += b'\r\n{"ok":true}'
    device.now += MS
    device.send(8080, syn.sport, 1001, ack, ACK | PSH | FIN, response)
    result = exchange.response()
    assert result is not None and result.json() == {"ok": True}
    device.now += MS
    device.send(8080, syn.sport, 1002 + len(response), ack + 1, ACK)
    assert exchange.connection.state == "closed"
    assert "http client: device:8080/1 POST /echo → 200" in device.events()
    streams = [event for event in device.net.events if event.get("stream") == "device:8080/1"]
    assert streams[0]["direction"] == "in" and streams[0]["data"] == request.hex()
    assert streams[1]["direction"] == "out" and streams[1]["data"] == response.hex()


class Closing:
    """A TCP service that keeps each connection's `write` and `close`, for the test to call, and
    what the device sent."""

    def __init__(self) -> None:
        self.writes: list[Callable[[bytes], None]] = []
        self.closes: list[Callable[[], None]] = []
        self.received = bytearray()

    def accept(
        self, peer: tuple[str, int], write: Callable[[bytes], None], close: Callable[[], None]
    ) -> "Closing":
        self.writes.append(write)
        self.closes.append(close)
        return self

    def on_data(self, data: bytes) -> None:
        self.received += data

    def on_close(self) -> None:
        pass


def test_a_chip_reset_drops_a_device_connection_still_in_its_handshake(device: Device) -> None:
    [syn_ack] = device.send(40000, 1883, 100, 0, SYN)
    assert syn_ack.flags == SYN | ACK and len(device.net.ip._flows) == 1
    device.net.device_reset(device.now, "the device reset")
    assert not device.net.ip._flows
    line = "net: TCP 10.64.0.2:40000 → 10.64.0.1:1883 lost before the device answered its SYN-ACK"
    assert line in device.events()
    # The new stack's SYN from the same port is answered.
    device.now += MS
    [again] = device.send(40000, 1883, 500, 0, SYN)
    assert again.flags == SYN | ACK


def test_a_released_lease_orphans_the_devices_connections_on_its_address() -> None:
    device = Device("ethernet").up()
    service = Closing()
    device.net.listen(7000, service)
    device.send(40000, 7000, 100, 0, SYN)
    device.send(40000, 7000, 101, ISN + 1, ACK)
    test_side = device.net.connect(80)
    device.net.receive(MS, dhcp(7, ciaddr=DEVICE, options=bytes([54, 4]) + NETWORK))
    assert device.net.device_address is None
    # lwIP drops a netif's connections when it loses the address: the test's are lost, the
    # device's orphaned, so its service's close sends no FIN and ends the flow at once.
    assert test_side.state == "lost"
    assert "net: TCP 10.64.0.1:49152 → 10.64.0.2:80 lost: the device gave its address up" in (
        device.events()
    )
    [flow] = device.net.ip._flows.values()
    assert flow.established and flow.orphaned
    [close] = service.closes
    close()
    assert device.poll() == [] and not device.net.ip._flows
    line = "net: TCP 10.64.0.2:40000 → 10.64.0.1:7000 closed by its service with no device"
    assert line + " to answer" in device.events()


def test_an_unsupported_dhcp_message_leaves_the_address_and_the_connections() -> None:
    device = Device("ethernet").up()
    test_side = device.net.connect(80)
    device.net.receive(MS, dhcp(10, ciaddr=DEVICE))  # a type the server does not take
    assert device.net.device_address == "10.64.0.2"
    assert test_side.state == "connecting"


def test_what_an_orphaned_connection_cannot_take_is_discarded_and_counted() -> None:
    device = Device("ethernet").up()
    service = Closing()
    device.net.listen(7000, service)
    device.send(40000, 7000, 100, 0, SYN)
    device.send(40000, 7000, 101, ISN + 1, ACK, window=1000)
    [write] = service.writes
    write(b"x" * 5000)  # 1000 go, 4000 wait for the window
    [flow] = device.net.ip._flows.values()
    assert len(flow.pending) == 4000
    device.net.device_reset(device.now, "the device reset")
    # What waited is dropped with the device's stack, and nothing piles up behind it.
    assert flow.orphaned and not flow.pending and flow.discarded == 4000
    for _ in range(100):
        write(b"x" * 1000)
    assert not flow.pending and flow.discarded == 104_000
    [close] = service.closes
    close()
    line = "net: TCP 10.64.0.2:40000 → 10.64.0.1:7000 discarded 104000 bytes its service wrote"
    assert line + " with no device to answer" in device.events()


@pytest.mark.parametrize("loss", ["reset", "release"])
def test_the_gone_stacks_syn_on_the_path_never_arrives_but_its_data_does(
    device: Device, loss: str
) -> None:
    if loss == "release" and device.link == "ppp":
        pytest.skip("a lease is given up behind the access point only")
    from esp32tl.units import ms

    service = Closing()
    device.net.listen(7000, service)
    device.send(40000, 7000, 100, 0, SYN)
    device.send(40000, 7000, 101, ISN + 1, ACK)
    device.net.set_path(rtt=ms(201))  # 100 ms up
    device.send(40000, 7000, 101, ISN + 1, ACK | PSH, b"last words")
    device.send(40001, 1883, 900, 0, SYN)
    if loss == "reset":
        device.net.device_reset(device.now, "the device reset")
    else:
        device.net.receive(device.now, dhcp(7, ciaddr=DEVICE, options=bytes([54, 4]) + NETWORK))
    device.net.arrive(device.now + 200 * MS)
    # What the old stack sent arrives, as at a service on the internet; its new connection's
    # SYN would only open a flow nothing ends, so a new stack's SYN from 40001 finds none.
    assert bytes(service.received) == b"last words"
    assert [key[1] for key in device.net.ip._flows] == [40000]


def test_a_malformed_datagram_on_the_path_survives_a_reset() -> None:
    from esp32tl.units import ms

    device = Device("ethernet").up()
    device.net.set_path(rtt=ms(201))
    bad = bytearray(34)
    bad[0], bad[9] = 0x4F, 6  # a 60-byte header in 34 bytes, TCP
    device.net.receive(device.now, device._wrap(bytes(bad)))
    # With its length impossible, it keeps the guard word esp32sim's frames carry.
    [(_, queued)] = device.net._up.queue
    assert queued.startswith(bytes(bad))
    device.net.device_reset(device.now, "the device reset")
    # Not a SYN that parses: it stays on the path, for the IP layer to drop, as before.
    assert [datagram for _, datagram in device.net._up.queue] == [queued]
    device.net.arrive(device.now + 200 * MS)
    assert not device.net._up.queue and not device.net.ip._flows


def test_a_malformed_syn_leaves_the_orphaned_connection_and_its_will() -> None:
    device = Device("ppp").up()
    service = Closing()
    device.net.listen(7000, service)
    device.send(40000, 7000, 100, 0, SYN)
    device.send(40000, 7000, 101, ISN + 1, ACK)
    device.net.device_reset(device.now, "the device reset")
    [flow] = device.net.ip._flows.values()
    # An option that does not parse: the SYN is refused before it could reset anything.
    assert device.send(40000, 7000, 500, 0, SYN, options=bytes([2, 9, 0, 0])) == []
    assert list(device.net.ip._flows.values()) == [flow] and flow.orphaned


def test_a_next_data_poll_searches_only_the_bytes_since_the_last_one(device: Device) -> None:
    # A long stream without the `until`, one poll after each chunk: a search from `start` at
    # every poll would look through N * (N + 1) / 2 chunks, 50 M of them.
    n, chunk = 10_000, b"temp 21.5\r"
    connection = device.net.connect(80)
    device.poll()
    device.send(80, 49152, 1000, ISN + 1, SYN | ACK)
    connection._received = received = SearchedBytes()
    line = connection.next_data(until=b"\r\n")
    for _ in range(n):
        connection.on_data(chunk)
        assert line() is None
    # Each poll looks through its chunk, and the byte before it, where a split `until` begins.
    assert received.searched <= n * (len(chunk) + 1)
    connection.on_data(b"\nnext")
    assert line() == chunk * n + b"\n" and line() == chunk * n + b"\n"
    assert received.searched <= (n + 3) * (len(chunk) + 1)


S = 1000 * MS


def test_an_unanswered_syn_goes_again_on_a_doubling_rto_then_times_out(device: Device) -> None:
    connection = device.net.connect(80)
    [first] = device.poll()
    established, timed_out = connection.established, connection.timed_out
    assert connection.unanswered() == "the SYN at 0.001 s has just gone"
    device.now = 501 * MS
    assert connection.unanswered() == (
        "the SYN at 0.001 s has had no answer in 0.5 s (no SYN-ACK, no RST)"
    )
    assert established.waiting is not None and established.waiting() == connection.unanswered()
    # 1 s after the first, then each time twice as long after the last: 6 times, as Linux does.
    sent = [MS]
    for gap in (1, 2, 4, 8, 16, 32):
        due = sent[-1] + gap * S
        assert device.net.quiet_until() == due
        device.now = due - 1
        assert device.poll() == []
        device.now = due
        [again] = device.poll()
        assert (again.sport, again.seq, again.flags, again.options) == (
            first.sport,
            ISN,
            SYN,
            mss(1400),
        )
        sent.append(due)
    assert connection.syns == [Duration(t) for t in sent]
    assert connection.unanswered() == (
        "the SYNs at 0.001 s, 1.001 s, 3.001 s, 7.001 s, 15.001 s and 31.001 s got no answer (no"
        " SYN-ACK, no RST), and the SYN at 63.001 s has just gone"
    )
    device.now = 64 * S + MS
    assert str(connection.unanswered()).endswith("and the SYN at 63.001 s has had none in 1 s")
    # 64 s after the last, 127 s after the first, the connection times out, with no SYN more.
    assert device.net.quiet_until() == 127 * S + MS
    device.now = 127 * S + MS
    assert device.poll() == []
    assert connection.state == "timed out" and connection.unanswered() is None
    assert timed_out() == Duration(127 * S + MS) == connection.ended_at
    assert device.net.quiet_until() is None
    with pytest.raises(ConnectionEnded) as error:
        established()
    assert str(error.value) == (
        "TCP 10.64.0.1:49152 → 10.64.0.2:80 was timed out with no answer to its 7 SYNs before it"
        " was established"
    )
    with pytest.raises(ConnectionEnded, match=r"timed out with no answer to its 7 SYNs: nothing"):
        connection.write(b"late")
    name = "net: TCP 10.64.0.1:49152 → 10.64.0.2:80"
    assert [e for e in device.events() if isinstance(e, str) and e.startswith(name)] == [
        f"{name} connecting",
        *(f"{name} SYN again, {n} of 7" for n in range(2, 8)),
        f"{name} timed out: no answer to 7 SYNs",
    ]
    assert device.net.report()[-2:] == [
        "TCP connections the test opened:",
        "  device:80/1  TCP 10.64.0.1:49152 → 10.64.0.2:80  timed out at 127.001 s",
    ]


def test_the_devices_answer_to_a_syn_sent_again_ends_its_timer(device: Device) -> None:
    accepted, refused = device.net.connect(80), device.net.connect(81)
    device.poll()
    device.now = S + MS
    syns = device.poll()
    assert [(s.sport, s.flags) for s in syns] == [(49152, SYN), (49153, SYN)]
    report = device.net.report()
    assert report[-2:] == [
        "  device:80/1  TCP 10.64.0.1:49152 → 10.64.0.2:80  connecting: the SYN at 0.001 s got no"
        " answer (no SYN-ACK, no RST), and the SYN at 1.001 s has just gone",
        "  device:81/1  TCP 10.64.0.1:49153 → 10.64.0.2:81  connecting: the SYN at 0.001 s got no"
        " answer (no SYN-ACK, no RST), and the SYN at 1.001 s has just gone",
    ]
    device.now += MS
    [ack] = device.send(80, 49152, 1000, ISN + 1, SYN | ACK)
    assert ack.ack == 1001 and accepted.state == "established"
    assert accepted.established() == Duration(S + 2 * MS) and accepted.unanswered() is None
    assert device.send(81, 49153, 0, ISN + 1, RST | ACK) == []
    assert refused.refused() == Duration(S + 2 * MS) and refused.unanswered() is None
    with pytest.raises(ConnectionEnded, match=r"was refused by the device, not timed out"):
        refused.timed_out()
    with pytest.raises(ConnectionEnded, match=r"was established, not timed out"):
        accepted.timed_out()
    # No timer is left, and nothing goes again.
    assert device.net.quiet_until() is None
    device.now = 10 * S
    assert device.poll() == []
    assert device.net.report()[-1] == (
        "  device:81/1  TCP 10.64.0.1:49153 → 10.64.0.2:81  refused at 1.002 s"
    )


def test_a_connection_lost_while_connecting_keeps_no_timer() -> None:
    device = Device("ppp").up()
    connection = device.net.connect(80)
    device.poll()
    device.net.device_reset(MS, "the device reset")
    assert connection.state == "lost" and connection.unanswered() is None
    assert device.net.ip.quiet_until() is None


def test_a_http_response_waited_for_before_the_syn_is_answered_says_so(device: Device) -> None:
    exchange = device.net.http_client.get("/status")
    device.poll()
    device.now = S + MS
    device.poll()
    assert exchange.response.waiting is not None
    device.now = S + 251 * MS
    assert exchange.response.waiting() == (
        "the SYN at 0.001 s got no answer (no SYN-ACK, no RST), and the SYN at 1.001 s has had"
        " none in 0.25 s"
    )
    while (due := device.net.quiet_until()) is not None:
        device.now = due
        device.poll()
    assert exchange.connection.state == "timed out"
    assert exchange.connection.ended_at == Duration(127 * S + MS)
    with pytest.raises(ConnectionEnded) as error:
        exchange.response()
    assert str(error.value) == (
        "HTTP GET /status: TCP 10.64.0.1:49152 → 10.64.0.2:80: timed out before HTTP response"
        " completed"
    )


def test_accepting_connects_again_a_second_after_each_refusal(device: Device) -> None:
    accepting = device.net.accepting(80)
    assert repr(accepting) == "TCP 10.64.0.2:80 accepting a connection"
    [first] = device.poll()
    assert accepting() is None and accepting.waiting is not None
    device.now = 2 * MS
    assert device.send(80, first.sport, 0, ISN + 1, RST | ACK) == []
    assert accepting() is None
    assert accepting.waiting() == (
        "the device refused 1 connection, the last at 0.002 s; the next goes at 1.002 s"
    )
    assert device.net.quiet_until() == S + 2 * MS
    device.now = S + 2 * MS - 1
    assert device.poll() == []
    device.now = S + 2 * MS
    [second] = device.poll()
    assert (second.sport, second.flags) == (49153, SYN)
    assert accepting.waiting() == (
        "the device refused 1 connection, the last at 0.002 s; the SYN at 1.002 s has just gone"
    )
    device.now += MS
    [ack] = device.send(80, second.sport, 1000, ISN + 1, SYN | ACK)
    assert ack.flags == ACK
    connection = accepting()
    assert connection is not None and connection.local_port == 49153
    assert connection.state == "established" and accepting() is connection
    assert [c.identity for c in device.net.connections] == ["device:80/1", "device:80/2"]
    assert [c.state for c in device.net.connections] == ["refused", "established"]
    # Done: nothing more goes, and the connection is the test's to use.
    assert device.net.quiet_until() is None
    device.now = 5 * S
    assert device.poll() == []
    connection.write(b"hi")
    [data] = device.poll()
    assert data.data == b"hi"


def test_accepting_gives_up_after_30_refusals(device: Device) -> None:
    accepting = device.net.accepting(80)
    for n in range(30):
        [syn] = device.poll()
        assert syn.sport == 49152 + n
        device.send(80, syn.sport, 0, ISN + 1, RST | ACK)
        if n < 29:
            due = device.net.quiet_until()
            assert due == device.now + S
            device.now = due
    assert device.net.quiet_until() is None
    with pytest.raises(ConnectionEnded) as error:
        accepting()
    assert str(error.value) == (
        "TCP 10.64.0.2:80 accepting a connection: the device refused 30 connections, the last at"
        " 29.001 s, and no more go"
    )


def test_accepting_raises_for_a_lost_connection_and_an_unreachable_device() -> None:
    lost = Device("ppp").up()
    accepting = lost.net.accepting(80)
    lost.poll()
    lost.net.device_reset(MS, "the device reset")
    with pytest.raises(ConnectionEnded, match=r"was lost when the device reset before it was"):
        accepting()
    gone = Device("ethernet").up()
    accepting = gone.net.accepting(8080)
    [syn] = gone.poll()
    gone.send(8080, syn.sport, 0, ISN + 1, RST | ACK)
    gone.net.stop(MS)
    # The retry is due with the link down, and says so.
    assert gone.net.quiet_until() == S + MS
    gone.now = S + MS
    gone.net.poll(gone.now)
    with pytest.raises(NotReachable) as error:
        accepting()
    assert str(error.value) == (
        "network.accepting(8080): the access point is off the air: the network reaches no device"
    )
    assert gone.net.quiet_until() is None
    with pytest.raises(NotReachable, match=r"^network\.accepting\(80\): the access point"):
        gone.net.accepting(80)


def test_a_syn_ack_slower_than_the_rto_meets_the_syn_sent_again_once(device: Device) -> None:
    # 1.5 s each way and back: the SYN goes again at 1 s, before the device's SYN-ACK arrives.
    device.net.set_path(rtt=Duration(1500 * MS))
    connection = device.net.connect(80)
    sent: list[tuple[int, int]] = []

    def until(t: int) -> None:
        while (due := device.net.quiet_until()) is not None and due <= t:
            device.now = due
            sent.extend((device.now, s.flags) for s in device.poll())
        device.now = t
        sent.extend((device.now, s.flags) for s in device.poll())

    until(1501 * MS // 2)  # the SYN reaches the device at 751.5 ms
    device.now = 1503 * MS // 2
    assert device.send(80, 49152, 1000, ISN + 1, SYN | ACK) == []
    until(1501 * MS)
    assert connection.established_at == Duration(1501 * MS)
    until(3503 * MS // 2)  # the SYN sent again at 1.001 s reaches it at 1.7515 s
    assert device.send(80, 49152, 1000, ISN + 1, SYN | ACK) == []
    until(4 * S)
    # The second SYN-ACK, at 2.501 s, changes nothing and draws nothing.
    assert [flags for _, flags in sent] == [SYN, SYN, ACK]
    assert connection.state == "established" and connection.established_at == Duration(1501 * MS)
    assert len(connection.syns) == 2 and device.net.quiet_until() is None


def test_accepting_refuses_a_connection_that_ended_before_it_was_seen(device: Device) -> None:
    accepting = device.net.accepting(80)
    device.poll()
    # Accepted and reset by the device in one turn, before any poll of the condition.
    device.send(80, 49152, 1000, ISN + 1, SYN | ACK)
    device.send(80, 49152, 1001, ISN + 1, RST | ACK)
    with pytest.raises(ConnectionEnded) as error:
        accepting()
    assert str(error.value) == (
        "TCP 10.64.0.1:49152 → 10.64.0.2:80 was accepted, then reset by the device, before TCP"
        " 10.64.0.2:80 accepting a connection returned it"
    )
    # One the condition returned stays returned, whatever the device does to it later.
    later = device.net.accepting(81)
    device.poll()
    device.send(81, 49153, 1000, ISN + 1, SYN | ACK)
    connection = later()
    assert connection is not None
    device.send(81, 49153, 1001, ISN + 1, FIN | ACK)
    assert connection.closed_at is not None and later() is connection
