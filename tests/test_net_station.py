"""A synthetic SoftAP: DHCP server, ARP and TCP segments, without an emulator."""

import struct

import pytest
from test_net_ethernet import check, frame, ipv4, options, udp

from esp32tl.net import Internet
from esp32tl.net.client import NotReachable
from esp32tl.net.dhcp import Dhcp
from esp32tl.net.ip import ACK, ISN, PSH, SYN
from esp32tl.net.station import Station
from esp32tl.units import Duration

HOST = bytes.fromhex("025354410001")
AP = bytes.fromhex("246f28001123")
HOST_IP = bytes([192, 168, 4, 2])
AP_IP = bytes([192, 168, 4, 1])
BROADCAST = b"\xff" * 6
MS = 1_000_000


class SoftAP:
    def __init__(self) -> None:
        self.now = 0
        self.net = Internet(lambda: self.now, link="station")
        self.server = Dhcp(lambda: self.now, local="192.168.4.1", device="192.168.4.2")
        self.net.start(0)

    def reply(self, request: bytes, *, nak: bool = False) -> list[tuple[int, bytes]]:
        message = request[42:]
        assert request[:12] == BROADCAST + HOST and message[10:12] == b"\x80\x00"
        assert check(request[14:34]) == 0
        answer = self.server.on_message(message)
        assert answer is not None
        data = answer[0]
        if nak:
            data = data[:242] + b"\x06" + data[243:]
        self.now += MS
        return self.net.receive(
            self.now, frame(BROADCAST, 0x0800, udp(AP_IP, b"\xff" * 4, (67, 68), data), AP)
        )

    def up(self) -> "SoftAP":
        [(_, discover)] = self.net.poll(0)
        [(_, request)] = self.reply(discover)
        assert options(request[42:])[53] == b"\x03"
        assert self.reply(request) == []
        return self

    def arp(self, target: bytes, op: int = 1) -> list[tuple[int, bytes]]:
        body = struct.pack(">HHBBH", 1, 0x0800, 6, 4, op) + AP + AP_IP + HOST + target
        return self.net.receive(self.now, frame(BROADCAST, 0x0806, body, AP))

    def tcp(self, seq: int, ack: int, flags: int, data: bytes = b"") -> list[tuple[int, bytes]]:
        segment = struct.pack(">HHIIBBHHH", 80, 49152, seq, ack, 0x50, flags, 8192, 0, 0) + data
        pseudo = AP_IP + HOST_IP + struct.pack(">BBH", 0, 6, len(segment))
        segment = segment[:16] + struct.pack(">H", check(pseudo + segment)) + segment[18:]
        self.now += MS
        return self.net.receive(self.now, frame(HOST, 0x0800, ipv4(6, AP_IP, HOST_IP, segment), AP))


def test_lease_lost_offer_retries_nak_and_expiry() -> None:
    ap = SoftAP()
    condition = ap.net.when_leased()
    assert condition() is None
    with pytest.raises(TypeError):
        bool(condition)
    with pytest.raises(NotReachable, match=r"wait for network.when_leased"):
        ap.net.connect(80, address="192.168.4.1")
    [(_, first)] = ap.net.poll(0)
    assert ap.net.quiet_until() == 4_000 * MS
    ap.now = 3_999 * MS
    assert ap.net.poll(ap.now) == []
    ap.now = 4_000 * MS
    [(_, retry)] = ap.net.poll(ap.now)
    assert retry == first
    [(_, request)] = ap.reply(retry)
    [(_, restarted)] = ap.reply(request, nak=True)
    assert options(restarted[42:])[53] == b"\x01" and restarted[46:50] != first[46:50]
    [(_, request)] = ap.reply(restarted)
    ap.reply(request)
    lease = condition()
    assert lease is not None
    assert (lease.address, lease.device_address, lease.at) == (
        "192.168.4.2",
        "192.168.4.1",
        Duration(4_004 * MS),
    )
    ap.now = lease.expires.ns
    [(_, expired)] = ap.net.poll(ap.now)
    assert ap.net.lease is None and options(expired[42:])[53] == b"\x01"


def test_arp_answers_only_our_address_and_resolves_before_tcp() -> None:
    ap = SoftAP().up()
    assert ap.arp(AP_IP) == []
    assert ap.arp(bytes([192, 168, 4, 9])) == []
    [(_, reply)] = ap.arp(HOST_IP)
    assert reply[:12] == AP + HOST and reply[20:22] == b"\x00\x02"
    ap = SoftAP().up()
    connection = ap.net.connect(80)
    [(_, request)] = ap.net.poll(ap.now)
    assert request[12:14] == b"\x08\x06" and request[38:42] == AP_IP
    [(_, syn)] = ap.arp(HOST_IP, op=2)
    assert syn[:12] == AP + HOST and syn[26:34] == HOST_IP + AP_IP
    assert check(syn[14:34]) == 0
    assert connection.local == "192.168.4.2"
    ap.tcp(1000, ISN + 1, SYN | ACK)
    connection.write(b"GET / HTTP/1.0\r\n\r\n")
    assert ap.net.poll(ap.now)
    ap.tcp(1001, ISN + 1, ACK | PSH, b"hello")
    assert connection.received == b"hello"
    ap.net.device_reset(ap.now, "the device reset")
    assert ap.net.lease is None and connection.state == "lost"
    with pytest.raises(NotReachable, match="leased no address"):
        ap.net.connect(80)
    [(_, discover)] = ap.net.poll(ap.now)
    assert options(discover[42:])[53] == b"\x01"


def test_new_arp_destination_does_not_postpone_pending_retries() -> None:
    ap = SoftAP().up()
    started = ap.now
    other = bytes([192, 168, 4, 9])
    assert isinstance(ap.net.link, Station)
    ap.net.link.send_ip(ipv4(6, HOST_IP, AP_IP, b""))
    [(_, first)] = ap.net.poll(ap.now)
    assert first[12:14] == b"\x08\x06" and first[38:42] == AP_IP
    ap.now = started + 500 * MS
    ap.net.link.send_ip(ipv4(6, HOST_IP, other, b""))
    [(_, second)] = ap.net.poll(ap.now)
    assert second[12:14] == b"\x08\x06" and second[38:42] == other
    assert ap.net.quiet_until() == started + 1_000 * MS
    for elapsed in (1_000, 2_000):
        ap.now = started + (elapsed - 1) * MS
        assert ap.net.poll(ap.now) == []
        ap.now += MS
        assert [packet for _, packet in ap.net.poll(ap.now)] == [first, second]
        assert ap.net.quiet_until() == ap.now + 1_000 * MS
    for address in (AP_IP, other):
        body = struct.pack(">HHBBH", 1, 0x0800, 6, 4, 2) + AP + address + HOST + HOST_IP
        [(_, datagram)] = ap.net.receive(ap.now, frame(HOST, 0x0806, body, AP))
        assert datagram[12:14] == b"\x08\x00" and datagram[30:34] == address
    assert ap.net.lease is not None
    assert ap.net.quiet_until() == ap.net.lease.expires.ns


def test_http_get_and_only_the_leased_host_receives_service_traffic() -> None:
    ap = SoftAP().up()
    exchange = ap.net.http_client.get("/status")
    ap.net.poll(ap.now)
    ap.arp(HOST_IP, op=2)
    sent = ap.tcp(1000, ISN + 1, SYN | ACK)
    payloads = [f[54:] for _, f in sent]
    request = b"".join(payloads)
    assert request.startswith(b"GET /status HTTP/1.1\r\n")
    body = b'{"status":"ok"}'
    response = b"HTTP/1.1 200 OK\r\nContent-Length: 15\r\n\r\n" + body
    ap.tcp(1001, ISN + 1 + len(request), ACK | PSH, response)
    parsed = exchange.response()
    assert parsed is not None
    assert parsed.json() == {"status": "ok"}
    assert isinstance(ap.net.link, Station)
    ap.net.receive(ap.now, frame(bytes.fromhex("333300000001"), 0x86DD, bytes(40), AP))
    assert ap.net.link.ignored == {0x86DD: 1}
    assert ap.net.dns.address == HOST_IP
    assert (
        ap.net.receive(
            ap.now,
            frame(HOST, 0x0800, udp(AP_IP, bytes([10, 64, 0, 1]), (1000, 53), bytes(12)), AP),
        )
        == []
    )


def test_station_path_delays_ip_after_arp_resolution() -> None:
    from esp32tl.units import ms

    ap = SoftAP().up()
    ap.net.set_path(rtt=ms(100))
    ap.net.connect(80)
    [(_, arp_request)] = ap.net.poll(ap.now)
    assert arp_request[12:14] == b"\x08\x06"
    assert ap.arp(HOST_IP, op=2) == []
    due = ap.net.quiet_until()
    assert due is not None and due > ap.now + MS
    [(_, syn)] = ap.net.poll(due)
    assert syn[12:14] == b"\x08\x00" and syn[47] & SYN


def test_foreign_dhcp_replies_do_not_install_a_lease_and_lost_ack_retries() -> None:
    ap = SoftAP()
    [(_, discover)] = ap.net.poll(0)
    [(_, request)] = ap.reply(discover)
    answer = ap.server.on_message(request[42:])
    assert answer is not None
    ack = answer[0]
    for start, value in [(4, b"\x99" * 4), (28, b"\x99" * 6), (236, bytes(4))]:
        bad = ack[:start] + value + ack[start + len(value) :]
        assert (
            ap.net.receive(
                ap.now, frame(BROADCAST, 0x0800, udp(AP_IP, b"\xff" * 4, (67, 68), bad), AP)
            )
            == []
        )
        assert ap.net.lease is None
    ap.now += 4_000 * MS
    [(_, repeated)] = ap.net.poll(ap.now)
    assert repeated == request
    ap.reply(repeated)
    assert ap.net.lease is not None


def test_services_answer_only_on_the_host_address() -> None:
    ap = SoftAP().up()
    ap.arp(HOST_IP)
    query = struct.pack(">HHHHHH", 1, 0x100, 1, 0, 0, 0) + b"\x03api\x04test\0\0\x01\0\x01"
    [(_, reply)] = ap.net.receive(
        ap.now, frame(HOST, 0x0800, udp(AP_IP, HOST_IP, (1000, 53), query), AP)
    )
    assert reply[:12] == AP + HOST and reply[26:34] == HOST_IP + AP_IP
    assert reply.endswith(HOST_IP)
    assert (
        ap.net.receive(
            ap.now, frame(HOST, 0x0800, udp(AP_IP, bytes([192, 168, 4, 9]), (1000, 53), query), AP)
        )
        == []
    )
    assert ap.net.dns.asked == ["api.test"]


def test_server_identifier_is_the_default_destination_and_address_overrides_it() -> None:
    ap = SoftAP()
    [(_, discover)] = ap.net.poll(0)
    [(_, request)] = ap.reply(discover)
    answer = ap.server.on_message(request[42:])
    assert answer is not None
    router = bytes([192, 168, 4, 254])
    message = answer[0].replace(bytes([3, 4]) + AP_IP, bytes([3, 4]) + router)
    ap.net.receive(ap.now, frame(BROADCAST, 0x0800, udp(AP_IP, b"\xff" * 4, (67, 68), message), AP))
    assert ap.net.lease is not None and ap.net.lease.router == "192.168.4.254"
    assert ap.net.connect(80).address == "192.168.4.1"
    assert ap.net.connect(80, address="192.168.4.9").address == "192.168.4.9"
    assert ap.net.connect(80, address="10.1.2.3").address == "10.1.2.3"
    targets = [packet[38:42] for _, packet in ap.net.poll(ap.now)]
    assert targets == [AP_IP, bytes([192, 168, 4, 9]), router]
