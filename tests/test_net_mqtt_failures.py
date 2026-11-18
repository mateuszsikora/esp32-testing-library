"""Broker faults over real PPP/TCP frames, with both plaintext and in-memory TLS clients."""

import contextlib
import ssl
import struct
from collections import deque

import pytest
from test_net_ip import DST, SRC, check, packet
from test_net_mqtt import CONNECT, DUPLICATE, PUBACK, PUBLISH, SUBSCRIBE
from test_net_ppp import control, frame, unframe

from esp32tl.link.boardlink import Output
from esp32tl.net import Internet
from esp32tl.net.tls import CertificateAuthority
from esp32tl.units import Duration


class Wire:
    """PPP opened at 0 on `net`, a network of its own unless given one whose link is up."""

    def __init__(self, net: Internet | None = None) -> None:
        self.now = 0
        self.net = Internet(lambda: self.now) if net is None else net
        self.peers: dict[int, Peer] = {}
        if net is None:
            self.net.start(0)
        self.open()

    def open(self) -> None:
        """LCP and IPCP, at `now`, on a link that has started."""
        self.net.poll(self.now)
        self.net.receive(self.now, control(0xC021, 1, 7))
        self.net.receive(self.now, control(0xC021, 2, 1))
        self.net.receive(self.now, control(0x8021, 2, 2, b"\x03\x06\x0a\x40\0\x01"))
        self.net.receive(self.now, control(0x8021, 1, 8, b"\x03\x06\x0a\x40\0\x02"))
        assert self.net.ppp.ipcp_open

    def pump(self, replies: list[Output]) -> None:
        queue = deque(replies)
        while queue:
            _, raw = queue.popleft()
            ip = unframe(raw)
            assert check(ip[:20]) == 0
            assert ip[9] == 6
            assert check(DST + SRC + struct.pack(">BBH", 0, 6, len(ip) - 20) + ip[20:]) == 0
            peer = self.peers[int.from_bytes(ip[22:24], "big")]
            data = ip[20 + (ip[32] >> 4) * 4 :]
            flags = ip[33]
            if data or flags & 3:
                peer.ack = int.from_bytes(ip[24:28], "big") + len(data) + bool(flags & 3)
                peer.received.extend(data)
                peer.closed |= bool(flags & 1)
                queue.extend(peer.segment(0x10))

    def poll(self) -> None:
        self.pump(self.net.poll(self.now))


class Peer:
    def __init__(self, wire: Wire, port: int, ca: CertificateAuthority | None = None) -> None:
        self.wire, self.port = wire, port
        self.destination = 8883 if ca else 1883
        self.seq, self.ack = 1, 0
        self.received = bytearray()
        self.closed = False
        self.tls: ssl.SSLObject | None = None
        self.incoming, self.outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
        wire.peers[port] = self
        wire.pump(self.segment(2))
        if ca:
            context = ssl.create_default_context(cadata=ca.ca_pem)
            context.maximum_version = ssl.TLSVersion.TLSv1_2
            self.tls = context.wrap_bio(self.incoming, self.outgoing, server_hostname="broker.test")
            for _ in range(20):
                self.incoming.write(bytes(self.received))
                self.received.clear()
                with contextlib.suppress(ssl.SSLWantReadError):
                    self.tls.do_handshake()
                data = self.outgoing.read()
                if data:
                    wire.pump(self.segment(0x18, data))
                if self.tls.version():
                    break
            else:
                pytest.fail("TLS handshake failed over PPP/TCP")

    def segment(self, flags: int, data: bytes = b"") -> list[Output]:
        head = struct.pack(
            ">HHIIBBHHH", self.port, self.destination, self.seq, self.ack, 0x50, flags, 8192, 0, 0
        )
        segment = head + data
        pseudo = SRC + DST + struct.pack(">BBH", 0, 6, len(segment))
        segment = segment[:16] + struct.pack(">H", check(pseudo + segment)) + segment[18:]
        self.seq += len(data) + bool(flags & 3)
        return self.wire.net.receive(self.wire.now, frame(0x21, packet(6, segment)))

    def send(self, data: bytes) -> None:
        if self.tls:
            self.tls.write(data)
            data = self.outgoing.read()
        self.wire.pump(self.segment(0x18, data))

    def read(self) -> bytes:
        data = bytes(self.received)
        self.received.clear()
        if not self.tls:
            return data
        self.incoming.write(data)
        plain = bytearray()
        with contextlib.suppress(ssl.SSLWantReadError):
            while chunk := self.tls.read():
                plain.extend(chunk)
        return bytes(plain)


@pytest.mark.parametrize("secure", [False, True], ids=["tcp", "tls"])
def test_puback_faults_reconnect_and_other_network_activity(secure: bool) -> None:
    wire = Wire()
    net, broker = wire.net, wire.net.broker
    ca = CertificateAuthority(["broker.test"]) if secure else None
    if ca:
        net.listen(8883, broker, tls=ca)
    broker.puback = "hold"
    peer = Peer(wire, 4000, ca)
    keep_session = CONNECT[:9] + b"\0" + CONNECT[10:]  # CleanSession=0
    peer.send(keep_session + SUBSCRIBE)
    assert peer.read() == b"\x20\x02\0\0\x90\x03\0\x01\x01"
    observer = Peer(wire, 4001, ca)
    observer.send(CONNECT[:-1] + b"d")
    assert observer.read() == b"\x20\x02\0\0"
    ppp = net.ppp

    peer.send(PUBLISH)
    held = broker.pending[0]
    assert held.status == "pending" and held.puback_at is None
    assert peer.read() == b"\x32\x0a\0\x03a/b\0\x01new"  # Routed without PUBACK.
    wire.now = 100
    assert held.release()
    assert held.puback_at == Duration(100)
    # Emission queues network bytes; the client has not read the PUBACK yet.
    assert peer.read() == b""
    wire.poll()
    assert peer.read() == PUBACK

    broker.puback = Duration(100)
    peer.send(PUBLISH)
    delayed = broker.pending[0]
    peer.read()  # Routed copy.
    assert net.quiet_until() == 200
    wire.now = 199
    wire.poll()
    assert peer.read() == b"" and delayed.puback_at is None
    wire.now = 200
    wire.poll()
    assert peer.read() == PUBACK and delayed.puback_at == Duration(200)

    broker.puback = "hold"
    peer.send(PUBLISH)
    stale = broker.pending[0]
    peer.read()
    next_client = broker.next_connection()
    assert broker.disconnect("c")
    wire.poll()
    assert peer.closed and not observer.closed
    assert stale.status == "cancelled" and stale.puback_at is None
    assert not broker.subscribed("a/#")()
    assert not stale.release()
    # A different live stream still responds, before the MQTT client reconnects.
    observer.send(b"\xc0\0")
    assert observer.read() == b"\xd0\0"
    # A fresh NTP operation traverses the SAME open PPP link during the outage.
    request = b"\x23" + b"\0" * 47
    udp = struct.pack(">HHHH", 5000, 123, 8 + len(request), 0) + request
    replies = net.receive(wire.now, frame(0x21, packet(17, udp)))
    assert len(replies) == 1 and len(unframe(replies[0][1])[28:]) == 48
    assert net.ntp.requests == 1 and net.ppp is ppp and ppp.ipcp_open

    replacement = Peer(wire, 4002, ca)
    # CleanSession=0 resumes: Session Present, and the routed copies that were never acknowledged
    # again with DUP and their packet IDs, in the order first sent.
    replacement.send(keep_session)
    assert replacement.read() == (
        b"\x20\x02\x01\0"
        b"\x3a\x0a\0\x03a/b\0\x01new"
        b"\x3a\x0a\0\x03a/b\0\x02new"
        b"\x3a\x0a\0\x03a/b\0\x03new"
    )
    assert broker.subscribed("a/#")()
    assert next_client() is not None
    assert next_client().connection_id != stale.connection_id  # type: ignore[union-attr]
    replacement.send(DUPLICATE)
    retried = broker.pending[0]
    assert retried.packet_id == stale.packet_id and retried.connection_id != stale.connection_id
    assert retried.messages[0].dup and retried.messages[0].payload == b"new"
    # The restored subscription routes it; its own handle holds the PUBACK.
    assert replacement.read() == b"\x32\x0a\0\x03a/b\0\x04new"
    assert not stale.release() and retried.puback_at is None
    assert retried.release()
    wire.poll()
    assert replacement.read() == PUBACK
    assert len([m for m in broker.messages if m.payload == b"new"]) == 4
    assert net.ppp is ppp and ppp.ipcp_open


def test_a_lost_link_leaves_a_delayed_puback_to_its_deadline_and_keeps_retained_receipt() -> None:
    wire = Wire()
    broker = wire.net.broker
    broker.puback = Duration(100)
    peer = Peer(wire, 4000)
    peer.send(CONNECT + b"\x33" + PUBLISH[1:])  # RETAIN and QoS 1.
    handle = broker.pending[0]
    assert broker.retained["a/b"].publication is handle
    assert wire.net.quiet_until() == 100
    wire.net.stop()
    # The broker knows nothing of the loss: the PUBACK goes at its time, to a link that is down.
    assert wire.net.quiet_until() == 100
    assert (handle.status, handle.puback_at) == ("pending", None)
    assert wire.net.poll(100) == []
    assert (handle.status, handle.puback_at) == ("emitted", Duration(100))
    assert broker.retained["a/b"].payload == b"new"
    assert not broker.pending
