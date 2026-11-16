"""The harness host behind esp32sim's virtual station: DHCP client and single-host ARP."""

import struct
from collections.abc import Callable
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network

from esp32tl.net.dhcp import ACK, COOKIE, DISCOVER, NAK, OFFER, REQUEST, _options
from esp32tl.net.ethernet import ARP, BROADCAST_MAC, ETHERTYPES, IPV4, _whole
from esp32tl.net.ip import UDP, Fragmented, checksum, ipv4, udp
from esp32tl.units import Duration

DEFAULT_MAC = "02:53:54:41:00:01"
SECOND = 1_000_000_000


@dataclass(frozen=True)
class HostLease:
    """The station's lease, its DHCP server, and its expiry, on the board's clock."""

    address: str
    device_address: str
    at: Duration
    expires: Duration
    mask: str
    router: str | None


class Station:
    def __init__(
        self,
        send: Callable[[bytes], None],
        on_ip: Callable[[bytes], None],
        now: Callable[[], int],
        *,
        mac: str = DEFAULT_MAC,
        log: Callable[[str], None] = lambda line: None,
        changed: Callable[[], None] = lambda: None,
    ) -> None:
        self._send, self._on_ip, self._now = send, on_ip, now
        self._log, self._changed = log, changed
        self.mac = bytes.fromhex(mac.replace(":", ""))
        self.mtu = 1500
        self.ignored: dict[int, int] = {}
        self.lease: HostLease | None = None
        self.leases: list[HostLease] = []
        self._xid = 0x53544100
        self._offered: bytes | None = None
        self._server: bytes | None = None
        self._deadline: int | None = None
        self._retry = 4 * SECOND
        self._arp_cache: dict[str, bytes] = {}
        self._pending: dict[str, list[bytes]] = {}
        self._arp_deadline: int | None = None

    @property
    def device_address(self) -> str | None:
        return None if self.lease is None else self.lease.device_address

    def start(self) -> None:
        self.lease = None
        self._offered = self._server = None
        self._arp_cache.clear()
        self._pending.clear()
        self._arp_deadline = None
        self._xid = (self._xid + 1) & 0xFFFFFFFF
        self._retry = 4 * SECOND
        self._changed()
        self._request()

    def _request(self) -> None:
        packet = bytearray(240)
        packet[:4] = b"\x01\x01\x06\x00"
        packet[4:8] = struct.pack(">I", self._xid)
        # Broadcast flag: lwIP dhcps sends OFFER/ACK without resolving an unbound host.
        packet[10:12] = b"\x80\x00"
        packet[28:34] = self.mac
        packet[236:240] = COOKIE
        kind = DISCOVER if self._offered is None else REQUEST
        packet += bytes([53, 1, kind, 61, 7, 1]) + self.mac
        packet += bytes([55, 4, 1, 3, 51, 54])
        if self._offered is not None:
            assert self._server is not None
            packet += bytes([50, 4]) + self._offered + bytes([54, 4]) + self._server
        packet += b"\xff"
        packet += bytes(max(0, 300 - len(packet)))
        segment = struct.pack(">HHHH", 68, 67, 8 + len(packet), 0) + packet
        header = struct.pack(
            ">BBHHHBBH4s4s", 0x45, 0, 20 + len(segment), 0, 0, 64, UDP, 0, bytes(4), b"\xff" * 4
        )
        header = header[:10] + struct.pack(">H", checksum(header)) + header[12:]
        self._frame(BROADCAST_MAC, IPV4, header + segment)
        self._log(f"dhcp station: {'DISCOVER' if kind == DISCOVER else 'REQUEST'}")
        self._deadline = self._now() + self._retry
        self._retry = min(64 * SECOND, self._retry * 2)

    def poll(self) -> None:
        now = self._now()
        if self.lease is not None and now >= self.lease.expires.ns:
            self._log("dhcp station: lease expired; acquiring a new address")
            self.start()
        elif self._deadline is not None and now >= self._deadline:
            self._request()
        if self._arp_deadline is not None and now >= self._arp_deadline:
            for address in self._pending:
                self._resolve(address)

    def quiet_until(self) -> int | None:
        deadlines = [self._deadline, self._arp_deadline]
        if self.lease is not None:
            deadlines.append(self.lease.expires.ns)
        return min((d for d in deadlines if d is not None), default=None)

    def _frame(self, to: bytes, kind: int, payload: bytes) -> None:
        self._send(to + self.mac + struct.pack(">H", kind) + payload)

    def receive(self, frame: bytes) -> None:
        if len(frame) < 14 or frame[6] & 1:
            return
        kind = int.from_bytes(frame[12:14], "big")
        if kind in (ARP, IPV4) and frame[:6] not in (self.mac, BROADCAST_MAC):
            return
        if kind == ARP:
            self._arp(frame[14:])
        elif kind == IPV4:
            data = _whole(frame[14:])
            try:
                parsed = ipv4(data)
            except Fragmented:
                return
            if parsed is None:
                return
            protocol, src, dst, payload = parsed
            ports = udp(src, dst, payload) if protocol == UDP else None
            if ports is not None and ports[:2] == (67, 68):
                self._dhcp(ports[2])
            elif self.lease is not None and dst == self.lease.address:
                self._on_ip(data)
        else:
            self.ignored[kind] = self.ignored.get(kind, 0) + 1
            if self.ignored[kind] == 1:
                self._log(
                    f"ethernet: dropping frames of type {kind:#06x} "
                    f"({ETHERTYPES.get(kind, 'unsupported')})"
                )

    def _dhcp(self, data: bytes) -> None:
        if (
            len(data) < 240
            or data[:3] != b"\x02\x01\x06"
            or data[236:240] != COOKIE
            or data[4:8] != struct.pack(">I", self._xid)
            or data[28:34] != self.mac
            or self.lease is not None
        ):
            return
        options = _options(data[240:])
        if options is None or len(options.get(54, b"")) != 4:
            return
        server = options[54]
        kind = options.get(53)
        if kind == bytes([OFFER]) and self._offered is None and data[16:20] != bytes(4):
            self._offered, self._server = data[16:20], server
            self._retry = 4 * SECOND
            self._request()
        elif self._server == server and kind == bytes([NAK]):
            self._log("dhcp station: NAK; acquiring a new address")
            self.start()
        elif self._server == server and kind == bytes([ACK]) and data[16:20] == self._offered:
            lifetime, mask = options.get(51, b""), options.get(1, b"")
            if len(lifetime) != 4 or len(mask) != 4 or int.from_bytes(lifetime) == 0:
                return
            address = str(IPv4Address(data[16:20]))
            try:
                IPv4Network(f"{address}/{IPv4Address(mask)}", strict=False)
            except ValueError:
                return
            router = options.get(3, b"")
            self.lease = HostLease(
                address,
                str(IPv4Address(server)),
                Duration(self._now()),
                Duration(self._now() + int.from_bytes(lifetime) * SECOND),
                str(IPv4Address(mask)),
                str(IPv4Address(router[:4])) if len(router) >= 4 else None,
            )
            self.leases.append(self.lease)
            self._deadline = None
            self._log(f"dhcp station: ACK {address}, device {self.lease.device_address}")
            self._changed()

    def send_ip(self, data: bytes) -> None:
        if self.lease is None or len(data) < 20 or len(data) > self.mtu:
            return
        target = str(IPv4Address(data[16:20]))
        subnet = IPv4Network(f"{self.lease.address}/{self.lease.mask}", strict=False)
        if IPv4Address(target) not in subnet:
            if self.lease.router is None:
                self._log(f"station: no router for {target}")
                return
            target = self.lease.router
        if target in self._arp_cache:
            self._frame(self._arp_cache[target], IPV4, data)
        else:
            first = target not in self._pending
            self._pending.setdefault(target, []).append(data)
            if first:
                self._resolve(target)

    def _resolve(self, target: str) -> None:
        if self.lease is None:
            return
        packet = struct.pack(">HHBBH", 1, IPV4, 6, 4, 1)
        packet += (
            self.mac
            + IPv4Address(self.lease.address).packed
            + bytes(6)
            + IPv4Address(target).packed
        )
        self._frame(BROADCAST_MAC, ARP, packet)
        self._arp_deadline = self._now() + SECOND

    def _arp(self, data: bytes) -> None:
        if self.lease is None or len(data) < 28 or data[:6] != struct.pack(">HHBB", 1, IPV4, 6, 4):
            return
        op = int.from_bytes(data[6:8], "big")
        sender, address, target = data[8:14], str(IPv4Address(data[14:18])), data[24:28]
        local = IPv4Address(self.lease.address).packed
        if op not in (1, 2) or sender[0] & 1 or target != local:
            return
        if data[14:18] != bytes(4):
            self._arp_cache[address] = sender
            for packet in self._pending.pop(address, []):
                self._frame(sender, IPV4, packet)
            if not self._pending:
                self._arp_deadline = None
        if op == 1:
            reply = struct.pack(">HHBBH", 1, IPV4, 6, 4, 2)
            self._frame(sender, ARP, reply + self.mac + local + sender + data[14:18])
