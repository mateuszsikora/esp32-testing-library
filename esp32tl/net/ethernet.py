"""Ethernet behind a Wi-Fi access point, the network's side: Ethernet II framing, ARP and the DHCP
server of `esp32tl.net.dhcp`. IPv4 datagrams go to `on_ip`, the device's own DHCP aside.

Every address but the device's answers ARP with the gateway's MAC, so whatever the device sends
reaches the network, on its subnet or off it, as `esp32tl.net.ip` wants: every destination
answers. The device's own address gets no answer: lwIP probes it before it takes a lease (RFC
5227), and an answer would make it decline. Frames of another type (IPv6, the device's link-local
traffic among them) are counted and dropped, and the first of each type is logged.
"""

import struct
from collections.abc import Callable
from ipaddress import IPv4Address

from esp32tl.net.dhcp import Dhcp
from esp32tl.net.ip import UDP, Fragmented, checksum, ipv4, udp

# The network's MAC: esp32sim's own network's gateway (`esp-soc/src/net/packet.rs`), a locally
# administered address.
GATEWAY_MAC = bytes.fromhex("0253494d0002")
BROADCAST_MAC = b"\xff" * 6
IPV4, ARP = 0x0800, 0x0806
ETHERTYPES = {0x86DD: "IPv6", 0x8100: "VLAN", 0x888E: "EAPOL"}
HEADER = 14


def mac(raw: bytes) -> str:
    return ":".join(f"{b:02x}" for b in raw)


def _whole(payload: bytes) -> bytes:
    """The IPv4 datagram of a frame, without what follows it. In esp32sim the station's frames
    carry 4 bytes more, libpp's guard word `ef be ad de`: libpp counts 4 bytes more than the frame,
    taken to be its FCS, and a CCMP MIC in its transmit descriptor's length, and esp32sim cuts the
    MIC only (protocol 5.15). One whose total length is not possible stays as it is, for the IP
    layer to drop."""
    total = int.from_bytes(payload[2:4], "big")
    return payload[:total] if 20 <= total <= len(payload) else payload


class Ethernet:
    """The access point's side of the station's link. `send` takes each Ethernet frame for the
    station; `on_ip` gets each IPv4 datagram the station sends, but its DHCP; `log` gets a line per
    event worth seeing. `dhcp` hands out the device's address, which `local` routes and `dns`
    resolves for it."""

    def __init__(
        self,
        send: Callable[[bytes], None],
        on_ip: Callable[[bytes], None],
        dhcp: Dhcp,
        *,
        log: Callable[[str], None] = lambda line: None,
    ) -> None:
        self._send = send
        self._on_ip = on_ip
        self._log = log
        self.dhcp = dhcp
        # The station's MAC, from the frames it sends; frames for it go there.
        self.device_mac: bytes | None = None
        self.mtu = 1500
        # Frames dropped by ethertype, and those not even Ethernet.
        self.ignored: dict[int, int] = {}

    def receive(self, frame: bytes) -> None:
        """One Ethernet frame the station sent, without FCS, and what follows it: in esp32sim, 4
        bytes of libpp's guard word (protocol 5.15). ARP reads its first 28 bytes and IPv4 its
        total length, so neither sees them."""
        if len(frame) < HEADER:
            return
        destination, source, kind = frame[:6], frame[6:12], struct.unpack(">H", frame[12:14])[0]
        payload = frame[HEADER:]
        if source[0] & 1:
            return
        self.device_mac = source
        if kind == ARP:
            self._arp(payload)
        elif kind == IPV4:
            if destination not in (GATEWAY_MAC, BROADCAST_MAC) and not destination[0] & 1:
                return
            datagram = _whole(payload)
            if not self._dhcp(datagram):
                self._on_ip(datagram)
        else:
            self.ignored[kind] = self.ignored.get(kind, 0) + 1
            if self.ignored[kind] == 1:
                name = ETHERTYPES.get(kind, "unsupported")
                self._log(f"ethernet: dropping frames of type {kind:#06x} ({name})")

    def send_ip(self, datagram: bytes, to: bytes | None = None) -> None:
        """An IPv4 datagram for the station, to its MAC unless `to` says another."""
        destination = to or self.device_mac
        if destination is None:
            self._log("ethernet: dropped an IPv4 packet: the station has sent nothing yet")
            return
        if len(datagram) > self.mtu:
            self._log(f"ethernet: dropped an IPv4 packet of {len(datagram)} bytes: over the MTU")
            return
        self._send(destination + GATEWAY_MAC + struct.pack(">H", IPV4) + datagram)

    def _arp(self, packet: bytes) -> None:
        if len(packet) < 28:
            return
        hardware, protocol, hlen, plen, op = struct.unpack(">HHBBH", packet[:8])
        if (hardware, protocol, hlen, plen, op) != (1, IPV4, 6, 4, 1):
            return
        sender_mac, sender_ip, target_ip = packet[8:14], packet[14:18], packet[24:28]
        if target_ip == IPv4Address(self.dhcp.device).packed or target_ip == sender_ip:
            # a probe of the address the device is about to take, or an announcement of it
            return
        reply = struct.pack(">HHBBH", 1, IPV4, 6, 4, 2)
        reply += GATEWAY_MAC + target_ip + sender_mac + sender_ip
        self._send(sender_mac + GATEWAY_MAC + struct.pack(">H", ARP) + reply)

    def _dhcp(self, datagram: bytes) -> bool:
        """Whether `datagram` was the station's DHCP, which goes no further: answered here."""
        # checked as the IP layer checks every datagram, which then logs what it drops
        try:
            parsed = ipv4(datagram)
        except Fragmented:
            return False
        if parsed is None or parsed[0] != UDP:
            return False
        ports = udp(*parsed[1:])
        if ports is None or ports[:2] != (68, 67):
            return False
        reply = self.dhcp.on_message(ports[2])
        if reply is None:
            return True
        message, broadcast = reply
        server = IPv4Address(self.dhcp.local).packed
        address = b"\xff" * 4 if broadcast else IPv4Address(self.dhcp.device).packed
        segment = struct.pack(">HHHH", 67, 68, 8 + len(message), 0) + message
        pseudo = server + address + struct.pack(">BBH", 0, UDP, len(segment))
        sum_ = checksum(pseudo + segment) or 0xFFFF
        segment = segment[:6] + struct.pack(">H", sum_) + segment[8:]
        header = struct.pack(
            ">BBHHHBBH4s4s", 0x45, 0, 20 + len(segment), 0, 0, 64, UDP, 0, server, address
        )
        header = header[:10] + struct.pack(">H", checksum(header)) + header[12:]
        self.send_ip(header + segment, BROADCAST_MAC if broadcast else None)
        return True
