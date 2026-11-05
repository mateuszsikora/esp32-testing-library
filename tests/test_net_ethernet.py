"""Handmade Ethernet frames behind the access point: ARP, DHCP and IPv4 to the network's
services, with independent checksums."""

import struct

import pytest

from esp32tl.net import LATENCY_NS, Internet
from esp32tl.net.dhcp import Dhcp
from esp32tl.net.ethernet import GATEWAY_MAC, Ethernet

STATION = bytes.fromhex("246f28001122")
BROADCAST = b"\xff" * 6
DEVICE, GATEWAY = bytes([10, 64, 0, 2]), bytes([10, 64, 0, 1])


def check(data: bytes) -> int:
    padded = data + b"\0" * (len(data) % 2)
    total = sum(int.from_bytes(padded[i : i + 2], "big") for i in range(0, len(padded), 2))
    total = (total & 65535) + (total >> 16)
    total = (total & 65535) + (total >> 16)
    return total ^ 65535


def ipv4(protocol: int, source: bytes, destination: bytes, payload: bytes) -> bytes:
    head = struct.pack(">BBHHHBBH", 0x45, 0, 20 + len(payload), 0, 0, 64, protocol, 0)
    head += source + destination
    return head[:10] + struct.pack(">H", check(head)) + head[12:] + payload


def udp(source: bytes, destination: bytes, ports: tuple[int, int], data: bytes) -> bytes:
    segment = struct.pack(">HHHH", *ports, 8 + len(data), 0) + data
    pseudo = source + destination + struct.pack(">BBH", 0, 17, len(segment))
    segment = segment[:6] + struct.pack(">H", check(pseudo + segment) or 0xFFFF) + segment[8:]
    return ipv4(17, source, destination, segment)


def frame(destination: bytes, kind: int, payload: bytes, source: bytes = STATION) -> bytes:
    return destination + source + struct.pack(">H", kind) + payload


def dhcp(kind: int, *, flags: int = 0, options: bytes = b"", ciaddr: bytes = bytes(4)) -> bytes:
    """A client's DHCP message in its frame, broadcast, from 0.0.0.0."""
    message = bytearray(240)
    message[0:4] = b"\x01\x01\x06\x00"
    message[4:8] = b"\x12\x34\x56\x78"
    message[10:12] = struct.pack(">H", flags)
    message[12:16] = ciaddr
    message[28:34] = STATION
    message[236:240] = b"\x63\x82\x53\x63"
    message += bytes([53, 1, kind]) + options + b"\xff"
    return frame(BROADCAST, 0x0800, udp(bytes(4), b"\xff" * 4, (68, 67), bytes(message)))


def arp(target: bytes, sender: bytes = DEVICE, op: int = 1) -> bytes:
    body = struct.pack(">HHBBH", 1, 0x0800, 6, 4, op) + STATION + sender + bytes(6) + target
    return frame(BROADCAST, 0x0806, body)


def options(message: bytes) -> dict[int, bytes]:
    found, at = {}, 240
    while message[at] != 255:
        found[message[at]] = message[at + 2 : at + 2 + message[at + 1]]
        at += 2 + message[at + 1]
    return found


def answer(sent: list[bytes]) -> tuple[bytes, bytes, bytes]:
    """The destination MAC, IP and DHCP message of the last frame, checksums checked."""
    reply = sent[-1]
    assert reply[6:12] == GATEWAY_MAC and reply[12:14] == b"\x08\x00"
    ip = reply[14:]
    assert check(ip[:20]) == 0 and ip[9] == 17 and ip[12:16] == GATEWAY
    pseudo = ip[12:20] + struct.pack(">BBH", 0, 17, len(ip) - 20)
    assert check(pseudo + ip[20:]) == 0
    assert struct.unpack(">HH", ip[20:24]) == (67, 68)
    return reply[:6], ip[16:20], ip[28:]


Link = tuple[Ethernet, list[bytes], list[bytes], list[str]]


@pytest.fixture
def link() -> Link:
    sent: list[bytes] = []
    ip: list[bytes] = []
    log: list[str] = []
    return (
        Ethernet(sent.append, ip.append, Dhcp(lambda: 7, log.append), log=log.append),
        sent,
        ip,
        log,
    )


def test_dhcp_offers_and_acknowledges_the_devices_address_with_router_and_dns(link: Link) -> None:
    ethernet, sent, ip, log = link
    ethernet.receive(dhcp(1))
    mac, address, offer = answer(sent)
    # unicast, under the station's pairwise key, unless it asks for broadcast
    assert (mac, address) == (STATION, DEVICE)
    assert offer[0] == 2 and offer[4:8] == b"\x12\x34\x56\x78" and offer[16:20] == DEVICE
    assert offer[28:34] == STATION and len(offer) >= 300
    found = options(offer)
    assert found[53] == b"\x02" and found[54] == GATEWAY
    assert (found[1], found[3], found[6]) == (bytes([255, 255, 255, 0]), GATEWAY, GATEWAY)
    assert found[51] == struct.pack(">I", 86400)
    ethernet.receive(dhcp(3, flags=0x8000, options=b"\x32\x04" + DEVICE + b"\x0c\x03esp"))
    mac, address, ack = answer(sent)
    assert (mac, address) == (BROADCAST, b"\xff" * 4)
    assert options(ack)[53] == b"\x05"
    lease = ethernet.dhcp.leases[-1]
    assert (lease.at.ns, lease.mac, lease.address, lease.hostname) == (
        7,
        "24:6f:28:00:11:22",
        "10.64.0.2",
        "esp",
    )
    assert log == [
        "dhcp: DISCOVER from 24:6f:28:00:11:22 → OFFER 10.64.0.2",
        "dhcp: REQUEST from 24:6f:28:00:11:22 → ACK 10.64.0.2",
    ]
    assert ip == [], "DHCP goes no further"


def test_dhcp_naks_another_address_and_ignores_another_server(link: Link) -> None:
    ethernet, sent, _, _ = link
    ethernet.receive(dhcp(3, options=b"\x32\x04" + bytes([10, 0, 2, 15])))
    mac, _, nak = answer(sent)
    assert mac == BROADCAST and options(nak)[53] == b"\x06" and nak[16:20] == bytes(4)
    count = len(sent)
    ethernet.receive(dhcp(3, options=b"\x36\x04" + bytes([10, 0, 2, 2])))
    ethernet.receive(dhcp(4))  # DECLINE: logged, unanswered
    ethernet.receive(dhcp(3, options=b"\x32\x09"))  # an option past the end
    assert len(sent) == count and ethernet.dhcp.leases == []
    # renewing from its address
    ethernet.receive(dhcp(3, ciaddr=DEVICE))
    assert options(answer(sent)[2])[53] == b"\x05"


@pytest.mark.parametrize("legacy", [False, True])
def test_dhcp_next_lease_uses_append_order_without_consuming(link: Link, legacy: bool) -> None:
    ethernet, _, _, _ = link
    condition = ethernet.dhcp.leased if legacy else ethernet.dhcp.next_lease
    assert ethernet.dhcp.find_lease() is None
    first_condition = condition()
    ethernet.receive(dhcp(3))
    first = ethernet.dhcp.leases[0]
    leased = condition()
    assert leased() is None
    # the same board time as the first lease: append order, not the timestamp, is the boundary
    ethernet.receive(dhcp(3))
    second = ethernet.dhcp.leases[1]
    assert first.at == second.at
    for _ in range(2):
        assert leased() is second
        assert first_condition() is first
        assert ethernet.dhcp.find_lease() is first


@pytest.mark.parametrize("method", ["leased", "next_lease"])
def test_dhcp_conditions_are_named_and_reject_truth_testing(link: Link, method: str) -> None:
    ethernet, _, _, _ = link
    condition = getattr(ethernet.dhcp, method)()
    assert repr(condition) == "the next DHCP lease"
    assert condition() is None
    with pytest.raises(TypeError, match=r"call the DHCP condition with \(\), or pass it to"):
        bool(condition)
    ethernet.receive(dhcp(3))
    assert condition() is not None
    with pytest.raises(TypeError, match="call the DHCP condition"):
        bool(condition)


def test_arp_answers_every_address_but_the_devices_own(link: Link) -> None:
    ethernet, sent, _, _ = link
    for target in (GATEWAY, bytes([93, 184, 216, 34])):
        ethernet.receive(arp(target))
        reply = sent[-1]
        assert reply[:6] == STATION and reply[6:12] == GATEWAY_MAC and reply[12:14] == b"\x08\x06"
        assert struct.unpack(">HHBBH", reply[14:22]) == (1, 0x0800, 6, 4, 2)
        assert reply[22:28] == GATEWAY_MAC and reply[28:32] == target
        assert reply[32:38] == STATION and reply[38:42] == DEVICE
    count = len(sent)
    ethernet.receive(arp(DEVICE, sender=bytes(4)))  # the probe before the lease (RFC 5227)
    ethernet.receive(arp(DEVICE))  # its announcement
    ethernet.receive(arp(GATEWAY, op=2))  # a reply is no question
    assert len(sent) == count


def test_ipv4_reaches_the_network_and_other_types_are_dropped_and_logged_once(link: Link) -> None:
    ethernet, sent, ip, log = link
    datagram = udp(DEVICE, GATEWAY, (5000, 53), b"query")
    ethernet.receive(frame(GATEWAY_MAC, 0x0800, datagram))
    ethernet.receive(frame(bytes.fromhex("020000000099"), 0x0800, datagram))  # not ours
    assert ip == [datagram]
    ethernet.receive(frame(bytes.fromhex("333300000001"), 0x86DD, bytes(40)))
    ethernet.receive(frame(bytes.fromhex("333300000001"), 0x86DD, bytes(40)))
    assert ethernet.ignored == {0x86DD: 2}
    assert log == ["ethernet: dropping frames of type 0x86dd (IPv6)"]
    ethernet.send_ip(b"\x45" + bytes(19))
    assert sent[-1] == STATION + GATEWAY_MAC + b"\x08\x00\x45" + bytes(19)


def test_nothing_goes_to_a_station_that_has_sent_nothing() -> None:
    sent: list[bytes] = []
    log: list[str] = []
    ethernet = Ethernet(sent.append, lambda d: None, Dhcp(lambda: 0), log=log.append)
    ethernet.send_ip(b"\x45" + bytes(19))
    ethernet.receive(frame(GATEWAY_MAC, 0x0800, b"", source=b"\x01" + bytes(5)))  # multicast
    assert sent == [] and log == [
        "ethernet: dropped an IPv4 packet: the station has sent nothing yet"
    ]


def test_a_network_behind_an_access_point_serves_http_over_ethernet() -> None:
    network = Internet(lambda: 0, link="ethernet")
    network.start(0)
    network.http.serve("/", "hello")
    with pytest.raises(AttributeError, match="no PPP"):
        _ = network.ppp
    syn = struct.pack(">HHIIBBHHH", 40000, 80, 100, 0, 5 << 4, 0x02, 8192, 0, 0)
    pseudo = DEVICE + GATEWAY + struct.pack(">BBH", 0, 6, len(syn))
    syn = syn[:16] + struct.pack(">H", check(pseudo + syn)) + syn[18:]
    outputs = network.receive(5, frame(GATEWAY_MAC, 0x0800, ipv4(6, DEVICE, GATEWAY, syn)))
    [(t, reply)] = outputs
    assert t == 5 + LATENCY_NS and reply[:6] == STATION
    assert reply[14 + 33] == 0x12, "SYN-ACK"
    assert network.ethernet.device_mac == STATION


def test_dhcp_checks_the_datagram_as_the_ip_layer_does(link: Link) -> None:
    ethernet, sent, ip, _ = link
    corrupt = bytearray(dhcp(3))
    corrupt[14 + 20 + 8 + 4] ^= 1  # the xid, under the UDP checksum
    ethernet.receive(bytes(corrupt))
    # not DHCP: it goes on to the IP layer, which drops it for its checksum
    assert sent == [] and ethernet.dhcp.leases == [] and ip == [bytes(corrupt[14:])]
    # a zero checksum is no checksum in IPv4 (RFC 768)
    unchecked = bytearray(dhcp(3))
    unchecked[14 + 20 + 6 : 14 + 20 + 8] = b"\0\0"
    ethernet.receive(bytes(unchecked))
    assert len(ethernet.dhcp.leases) == 1
    # a fragment is no DHCP, and goes to the IP layer, which logs it
    fragment = bytearray(dhcp(1))
    fragment[14 + 6] = 0x20
    fragment[14 + 10 : 14 + 12] = b"\0\0"
    fragment[14 + 10 : 14 + 12] = check(bytes(fragment[14 : 14 + 20])).to_bytes(2, "big")
    ethernet.receive(bytes(fragment))
    assert len(sent) == 1 and ip[-1] == bytes(fragment[14:])


def test_what_follows_an_ipv4_datagram_in_its_frame_is_not_part_of_it() -> None:
    seen: list[bytes] = []
    ethernet = Ethernet(lambda data: None, seen.append, Dhcp(lambda: 0))
    source = bytes.fromhex("246f28001122")
    head = GATEWAY_MAC + source + b"\x08\x00"
    datagram = bytes([0x45, 0]) + (24).to_bytes(2, "big") + bytes(20)
    ethernet.receive(head + datagram + b"\xef\xbe\xad\xde")
    assert seen == [datagram]
    # A total length that cannot be stays as it is: the IP layer drops such a datagram.
    too_long = bytes([0x45, 0]) + (99).to_bytes(2, "big") + bytes(20)
    too_short = bytes([0x45, 0]) + (19).to_bytes(2, "big") + bytes(20)
    ethernet.receive(head + too_long)
    ethernet.receive(head + too_short)
    assert seen[1:] == [too_long, too_short]
