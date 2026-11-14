"""A DHCP server for one device (RFC 2131, options of RFC 2132): it offers the device its one
address, with the network's router and DNS server, and acknowledges a request for it. Every lease
is kept, stamped with the board's time, for the test to look at.
"""

import struct
from collections.abc import Callable
from dataclasses import dataclass
from ipaddress import IPv4Address

from esp32tl.interfaces.condition import Condition
from esp32tl.units import Duration

COOKIE = b"\x63\x82\x53\x63"
DISCOVER, OFFER, REQUEST, DECLINE, ACK, NAK, RELEASE, INFORM = range(1, 9)
NAMES = {DISCOVER: "DISCOVER", REQUEST: "REQUEST", DECLINE: "DECLINE", RELEASE: "RELEASE"}
LEASE_SECONDS = 86400
# A BOOTP message is at least 300 bytes (RFC 1542, section 3.1.1).
MIN_MESSAGE = 300


@dataclass(frozen=True)
class Lease:
    """An address the server acknowledged: when, to which MAC, and the host name the device gave
    (option 12), if any."""

    at: Duration
    mac: str
    address: str
    hostname: str | None


class Dhcp:
    """The server at `local`, handing `device` its address on a `/24`, with `local` as its router
    and `dns` as its DNS server. `now` is the board's clock."""

    def __init__(
        self,
        now: Callable[[], int],
        log: Callable[[str], None] = lambda line: None,
        *,
        local: str = "10.64.0.1",
        device: str = "10.64.0.2",
        dns: str = "10.64.0.1",
        mask: str = "255.255.255.0",
    ) -> None:
        self._now = now
        self._log = log
        self.local, self.device, self.dns, self.mask = local, device, dns, mask
        self.leases: list[Lease] = []

    def next_lease(self) -> Callable[[], Lease | None]:
        """A non-consuming condition for the first lease appended after this call."""
        start = len(self.leases)

        def find() -> Lease | None:
            return self.leases[start] if len(self.leases) > start else None

        return Condition(find, "the next DHCP lease", "DHCP")

    def find_lease(self) -> Lease | None:
        """Return the first lease acknowledged so far, or None."""
        return self.leases[0] if self.leases else None

    def on_message(self, data: bytes) -> tuple[bytes, bool] | None:
        """The answer to one message from a client, and whether it goes to the broadcast address
        rather than to the device's; `None` for none."""
        if len(data) < 240 or data[0] != 1 or data[1:3] != b"\x01\x06" or data[236:240] != COOKIE:
            return None
        options = _options(data[240:])
        if options is None or len(options.get(53, b"")) != 1:
            return None
        kind = options[53][0]
        chaddr = data[28:34]
        name = NAMES.get(kind, f"type {kind}")
        server = options.get(54)
        if server is not None and server != IPv4Address(self.local).packed:
            # the device chose another server: there is none, but it is not this one's to answer
            return None
        requested = options.get(50) or (data[12:16] if data[12:16] != bytes(4) else None)
        if kind == DISCOVER:
            reply = OFFER
        elif kind == REQUEST:
            reply = ACK if requested in (None, IPv4Address(self.device).packed) else NAK
        else:
            if kind in (DECLINE, RELEASE):
                self._log(f"dhcp: {name} from {_mac(chaddr)}")
            return None
        answered = "OFFER" if reply == OFFER else "ACK" if reply == ACK else "NAK"
        self._log(f"dhcp: {name} from {_mac(chaddr)} → {answered} {self.device}")
        if reply == ACK:
            hostname = options.get(12)
            self.leases.append(
                Lease(
                    Duration(self._now()),
                    _mac(chaddr),
                    self.device,
                    hostname.decode(errors="replace") if hostname else None,
                )
            )
        message = bytearray(240)
        message[0:4] = b"\x02\x01\x06\x00"
        message[4:8] = data[4:8]  # xid
        message[10:12] = data[10:12]  # flags
        if reply != NAK:
            message[16:20] = IPv4Address(self.device).packed  # yiaddr
            message[20:24] = IPv4Address(self.local).packed  # siaddr
        message[28:44] = data[28:44]  # chaddr
        message[236:240] = COOKIE
        message += bytes([53, 1, reply, 54, 4]) + IPv4Address(self.local).packed
        if reply != NAK:
            message += bytes([51, 4]) + struct.pack(">I", LEASE_SECONDS)
            message += bytes([1, 4]) + IPv4Address(self.mask).packed
            message += bytes([3, 4]) + IPv4Address(self.local).packed
            message += bytes([6, 4]) + IPv4Address(self.dns).packed
        message.append(255)
        message += bytes(max(0, MIN_MESSAGE - len(message)))
        # Broadcast when the client asks for it (flags bit 15) and for a NAK (RFC 2131, 4.1);
        # otherwise to the address offered, under the station's pairwise key.
        broadcast = bool(data[10] & 0x80) or reply == NAK
        return bytes(message), broadcast


def message_type(data: bytes) -> int | None:
    """The type (option 53) of a client's message that `Dhcp.on_message` reads, `None` for one
    it does not."""
    if len(data) < 240 or data[0] != 1 or data[1:3] != b"\x01\x06" or data[236:240] != COOKIE:
        return None
    options = _options(data[240:])
    if options is None or len(options.get(53, b"")) != 1:
        return None
    return options[53][0]


def _options(data: bytes) -> dict[int, bytes] | None:
    options: dict[int, bytes] = {}
    at = 0
    while at < len(data):
        code = data[at]
        if code == 255:
            return options
        if code == 0:
            at += 1
            continue
        if at + 2 > len(data) or at + 2 + data[at + 1] > len(data):
            return None
        options.setdefault(code, data[at + 2 : at + 2 + data[at + 1]])
        at += 2 + data[at + 1]
    return options


def _mac(raw: bytes) -> str:
    return ":".join(f"{b:02x}" for b in raw)
