"""Handmade IPv4, UDP and TCP packets, including sequence wrap and connection loss."""

import struct
from collections.abc import Callable

from esp32tl.net.ip import Network

SRC, DST = b"\x0a\x40\0\x02", b"\xcb\0\x71\x99"


def check(data: bytes) -> int:
    padded = data + b"\0" * (len(data) % 2)
    total = sum(int.from_bytes(padded[i : i + 2], "big") for i in range(0, len(padded), 2))
    total = (total & 65535) + (total >> 16)
    total = (total & 65535) + (total >> 16)
    return total ^ 65535


def packet(protocol: int, payload: bytes, *, fragment: int = 0) -> bytes:
    head = (
        struct.pack(">BBHHHBBH", 0x45, 0, 20 + len(payload), 0, fragment, 64, protocol, 0)
        + SRC
        + DST
    )
    return head[:10] + struct.pack(">H", check(head)) + head[12:] + payload


def tcp(
    seq: int, ack: int, flags: int, data: bytes = b"", window: int = 8192, options: bytes = b""
) -> bytes:
    segment = (
        struct.pack(
            ">HHIIBBHHH", 4000, 1883, seq, ack, (5 + len(options) // 4) << 4, flags, window, 0, 0
        )
        + options
        + data
    )
    pseudo = SRC + DST + struct.pack(">BBH", 0, 6, len(segment))
    return packet(6, segment[:16] + struct.pack(">H", check(pseudo + segment)) + segment[18:])


class Echo:
    def __init__(self) -> None:
        self.received: list[bytes] = []
        self.closed = 0
        self.write: Callable[[bytes], None] = lambda b: None
        self.close: Callable[[], None] = lambda: None

    def accept(
        self, peer: tuple[str, int], write: Callable[[bytes], None], close: Callable[[], None]
    ) -> "Echo":
        self.write, self.close = write, close
        return self

    def on_data(self, data: bytes) -> None:
        self.received.append(data)
        self.write(data)

    def on_close(self) -> None:
        self.closed += 1


def test_tcp_handshake_data_duplicate_flow_control_fin_and_wrap() -> None:
    sent: list[bytes] = []
    net = Network(sent.append)
    echo = Echo()
    net.tcp[1883] = echo
    net.receive(tcp(0xFFFFFFFD, 0, 2, options=b"\x02\x04\0\x02"))  # MSS 2
    assert sent[-1][33] == 0x12
    assert int.from_bytes(sent[-1][24:28], "big") == 65536
    net.receive(tcp(0xFFFFFFFE, 65537, 0x10, window=0))
    net.receive(tcp(0xFFFFFFFE, 65537, 0x18, b"abcd", window=0))
    assert echo.received == [b"abcd"]
    assert int.from_bytes(sent[-1][28:32], "big") == 2
    before = len(sent)
    net.receive(tcp(2, 65537, 0x10, window=2))
    assert len(sent) == before + 1 and sent[-1][40:] == b"ab"
    net.receive(tcp(2, 65539, 0x10, window=2))
    assert sent[-1][40:] == b"cd"
    net.receive(tcp(0xFFFFFFFE, 65541, 0x18, b"abcd"))
    assert echo.received == [b"abcd"]
    net.receive(tcp(2, 65541, 0x11))
    assert echo.closed == 1
    assert sent[-1][33] == 0x11
    net.receive(tcp(3, 65542, 0x10))
    assert not net._flows and echo.closed == 1
    for datagram in sent:
        assert check(datagram[:20]) == 0
        pseudo = DST + SRC + struct.pack(">BBH", 0, 6, len(datagram) - 20)
        assert check(pseudo + datagram[20:]) == 0


def test_reset_notifies_once_and_unbound_port_refuses() -> None:
    sent: list[bytes] = []
    net = Network(sent.append)
    net.receive(tcp(1, 0, 2))
    assert sent[-1][33] == 0x14
    echo = Echo()
    net.tcp[1883] = echo
    net.receive(tcp(1, 0, 2))
    net.receive(tcp(2, 65537, 0x10))
    net.receive(tcp(2, 65537, 4))
    net.reset()
    assert echo.closed == 1 and not net._flows


def test_udp_length_checks_checksum_and_every_destination() -> None:
    class Udp:
        def on_datagram(self, data: bytes) -> bytes:
            assert data == b"test"
            return b"answer"

    sent: list[bytes] = []
    net = Network(sent.append)
    net.udp[53] = Udp()
    datagram = packet(17, struct.pack(">HHHH", 4000, 53, 12, 0) + b"test")
    net.receive(datagram)
    assert sent[0][12:20] == DST + SRC and sent[0][28:] == b"answer"
    assert check(DST + SRC + struct.pack(">BBH", 0, 17, len(sent[0]) - 20) + sent[0][20:]) == 0
    for end in range(len(datagram)):
        net.receive(datagram[:end])
    net.receive(datagram[:10] + b"\xff\xff" + datagram[12:])
    net.receive(packet(17, datagram[20:], fragment=0x2000))
    net.receive(packet(17, b"\0" * 8))
    net.receive(tcp(1, 0, 2, options=b"\x02\0\0\0"))
    assert len(sent) == 1


def test_modem_link_loss_closes_tcp_and_stamps_the_will_at_the_loss() -> None:
    from esp32tl.net import Internet

    net = Internet(lambda: 10)
    net.ip.receive(tcp(1, 0, 2))
    net.ip.receive(tcp(2, 65537, 0x10))
    connect = b"\x10\x18\0\x04MQTT\x04\x2e\0\x01\0\x01c\0\x03a/b\0\x04gone"
    net.ip.receive(tcp(2, 65537, 0x18, connect))
    assert len(net.broker.clients) == 1
    net.stop(500)
    net.stop(600)
    assert len(net.broker.messages) == 1
    assert net.broker.messages[0].payload == b"gone"
    assert net.broker.messages[0].at.ns == 500
    assert not net.ip._flows
    assert not net.ppp.started and net.poll(600) == []
