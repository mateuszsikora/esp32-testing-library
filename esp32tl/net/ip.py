"""The network the device reaches through its link: IPv4, UDP and TCP.

Every destination address answers. A TCP connection goes to the service listening on its
destination port, a UDP datagram to the one on its port, whatever the address: the device keeps
the names and addresses it was built with, and the test decides what answers there. The test
opens connections to the device too (`Network.connect`). The link to the device never loses or
reorders bytes, so TCP needs no retransmission, no congestion control and no timers: a segment is
acknowledged in the turn it arrives in, and a zero window waits for the device's update. While
the link is down, the segments of a connection its end left open go nowhere, and the device's
stack, which lost the connection too, never asks for them again.
"""

import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

TCP, UDP, ICMP = 6, 17, 1
FIN, SYN, RST, PSH, ACK = 0x01, 0x02, 0x04, 0x08, 0x10
WINDOW = 8192
ISN = 0x10000  # our initial sequence number: fixed, so runs repeat
MSS = 1400  # what we offer, and the most we take from the device's offer
EPHEMERAL = 49152  # the first port of the connections the test opens (RFC 6335)


def checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    total = int(sum(struct.unpack(f">{len(data) // 2}H", data)))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def addr(raw: bytes) -> str:
    return ".".join(str(b) for b in raw)


class Fragmented(Exception):
    """An IPv4 fragment, which this network does not reassemble."""


def ipv4(datagram: bytes) -> tuple[int, str, str, bytes] | None:
    """The protocol, source, destination and payload of a whole IPv4 datagram, its header
    checksum and lengths checked; `None` for anything else. Raises `Fragmented` for a fragment."""
    if len(datagram) < 20 or datagram[0] >> 4 != 4:
        return None
    version_ihl, _, total = struct.unpack(">BBH", datagram[:4])
    ihl = (version_ihl & 0x0F) * 4
    if not 20 <= ihl <= total <= len(datagram) or checksum(datagram[:ihl]):
        return None
    if int.from_bytes(datagram[6:8], "big") & 0x3FFF:
        raise Fragmented
    return datagram[9], addr(datagram[12:16]), addr(datagram[16:20]), datagram[ihl:total]


def udp(src: str, dst: str, payload: bytes) -> tuple[int, int, bytes] | None:
    """The ports and data of a UDP datagram from `src` to `dst`, its length and its checksum,
    unless zero, checked; `None` for anything else."""
    if len(payload) < 8:
        return None
    sport, dport, length, check = struct.unpack(">HHHH", payload[:8])
    if not 8 <= length <= len(payload):
        return None
    payload = payload[:length]
    if check and checksum(_pseudo(src, dst, UDP, length) + payload):
        return None
    return sport, dport, payload[8:]


def _pseudo(src: str, dst: str, protocol: int, length: int) -> bytes:
    return (
        bytes(int(p) for p in src.split("."))
        + bytes(int(p) for p in dst.split("."))
        + struct.pack(">BBH", 0, protocol, length)
    )


class Connection(Protocol):
    """A service's side of one TCP connection."""

    def on_data(self, data: bytes) -> None: ...

    def on_close(self) -> None: ...


class StreamService(Protocol):
    """What listens on a TCP port: makes a `Connection` for each one accepted. `write(bytes)` and
    `close()` are the connection's way back to the device."""

    def accept(
        self, peer: tuple[str, int], write: Callable[[bytes], None], close: Callable[[], None]
    ) -> Connection: ...


class DatagramService(Protocol):
    def on_datagram(self, data: bytes) -> bytes | None: ...


class Client(Protocol):
    """The test's side of a connection it opened: told when the device accepts it, each piece of
    what the test wrote as it goes onto the link (`on_sent`), each in-order byte the device sends,
    the device's FIN (`on_close`), and the connection's end, once, with its kind, `"closed"`,
    `"refused"`, `"reset"` or `"lost"`, and for `"lost"` why, as `the link went down`."""

    def on_established(self) -> None: ...

    def on_sent(self, data: bytes) -> None: ...

    def on_data(self, data: bytes) -> None: ...

    def on_close(self) -> None: ...

    def on_end(self, kind: str, why: str | None) -> None: ...


class OutOfOrder(Exception):
    """A segment from the device ahead of what it has sent: this link loses and reorders nothing,
    so it cannot happen, and the run fails rather than wait for a byte that never comes."""


_ENDS = {
    "closed": "closed",
    "refused": "refused by the device",
    "reset": "reset by the device",
    "lost": "link lost",
    "abandoned": "closed by its service with no device to answer",
    "unanswered": "lost before the device answered its SYN-ACK",
}


@dataclass
class _Flow:
    key: tuple[str, int, str, int]  # device address, device port, our address, our port
    rcv_nxt: int
    snd_nxt: int
    snd_una: int
    peer_window: int
    mss: int
    connection: Connection | None = None
    pending: bytearray = field(default_factory=bytearray)
    fin_pending: bool = False
    fin_sent: bool = False
    peer_closed: bool = False
    established: bool = False
    notified: bool = False
    client: Client | None = None  # set on a connection the test opened
    # The device's stack that held it is gone, by a chip reset or the link's end: no FIN to it
    # would ever be answered.
    orphaned: bool = False


class Network:
    """`send_ip` puts a datagram on the device's link; `log` gets a line per connection event."""

    def __init__(
        self,
        send_ip: Callable[[bytes], None],
        log: Callable[[str], None] = lambda line: None,
        *,
        mtu: Callable[[], int] = lambda: 1500,
    ) -> None:
        self._send_ip = send_ip
        self._mtu = mtu
        self._log = log
        self.tcp: dict[int, StreamService] = {}
        self.udp: dict[int, DatagramService] = {}
        self._flows: dict[tuple[str, int, str, int], _Flow] = {}
        self._ident = 0
        # The next port the test connects from: never reused, so no TIME_WAIT is needed.
        self._port = EPHEMERAL

    def receive(self, datagram: bytes) -> None:
        try:
            parsed = ipv4(datagram)
        except Fragmented:
            self._log("net: fragmented IPv4 is unsupported")
            return
        if parsed is None:
            return
        protocol, src, dst, payload = parsed
        if protocol == TCP:
            if len(payload) < 20 or checksum(self._pseudo(src, dst, TCP, len(payload)) + payload):
                return
            self._tcp(src, dst, payload)
        elif protocol == UDP:
            datagram_ = udp(src, dst, payload)
            if datagram_ is None:
                return
            sport, dport, data = datagram_
            service = self.udp.get(dport)
            if service is None:
                self._log(f"net: UDP {src}:{sport} → {dst}:{dport}, nothing listens there")
                return
            answer = service.on_datagram(data)
            if answer is not None:
                self._udp_send(dst, dport, src, sport, answer)

    # IPv4 out.

    def _ip_send(self, src: str, dst: str, protocol: int, payload: bytes) -> None:
        self._ident = (self._ident + 1) & 0xFFFF
        header = struct.pack(
            ">BBHHHBBH4s4s",
            0x45,
            0,
            20 + len(payload),
            self._ident,
            0,
            64,
            protocol,
            0,
            bytes(int(p) for p in src.split(".")),
            bytes(int(p) for p in dst.split(".")),
        )
        header = header[:10] + struct.pack(">H", checksum(header)) + header[12:]
        self._send_ip(header + payload)

    def _pseudo(self, src: str, dst: str, protocol: int, length: int) -> bytes:
        return _pseudo(src, dst, protocol, length)

    def _udp_send(self, src: str, sport: int, dst: str, dport: int, data: bytes) -> None:
        if len(data) + 28 > self._mtu():
            self._log(
                f"net: dropped UDP reply of {len(data) + 28} bytes: "
                f"exceeds peer MRU {self._mtu()} (fragmentation unsupported)"
            )
            return
        segment = struct.pack(">HHHH", sport, dport, 8 + len(data), 0) + data
        sum_ = checksum(self._pseudo(src, dst, UDP, len(segment)) + segment) or 0xFFFF
        segment = segment[:6] + struct.pack(">H", sum_) + segment[8:]
        self._ip_send(src, dst, UDP, segment)

    # TCP.

    def _segment(self, flow: _Flow, flags: int, data: bytes = b"", options: bytes = b"") -> None:
        device, dport, ours, oport = flow.key
        offset = (20 + len(options)) // 4
        header = struct.pack(
            ">HHIIBBHHH",
            oport,
            dport,
            flow.snd_nxt & 0xFFFFFFFF,
            flow.rcv_nxt & 0xFFFFFFFF,
            offset << 4,
            flags,
            WINDOW,
            0,
            0,
        )
        segment = header + options + data
        sum_ = checksum(self._pseudo(ours, device, TCP, len(segment)) + segment)
        segment = segment[:16] + struct.pack(">H", sum_) + segment[18:]
        self._ip_send(ours, device, TCP, segment)
        flow.snd_nxt += len(data) + (1 if flags & (SYN | FIN) else 0)

    def connect(
        self, device: str, port: int, ours: str, client: Client
    ) -> tuple[int, Callable[[bytes], None], Callable[[], None]]:
        """Open a connection from `ours` to `device`:`port`: the SYN enters the path now. Returns
        the port it comes from, the next ephemeral one, and the `write` and `close` of the test's
        side."""
        if self._port > 65535:
            raise RuntimeError("net: the test has used every ephemeral port, 49152 to 65535")
        local, self._port = self._port, self._port + 1
        flow = _Flow((device, port, ours, local), 0, ISN, ISN, 0, 536, connection=client)
        flow.client = client
        self._flows[flow.key] = flow
        self._log(f"net: TCP {ours}:{local} → {device}:{port} connecting")
        self._segment(flow, SYN, options=struct.pack(">BBH", 2, 4, MSS))
        return local, lambda out: self._write(flow, out), lambda: self._close(flow)

    def _tcp(self, src: str, dst: str, segment: bytes) -> None:
        sport, dport, seq, ack, offset, flags, window = struct.unpack(">HHIIBBH", segment[:16])
        header_length = (offset >> 4) * 4
        if not 20 <= header_length <= len(segment):
            return
        data = segment[header_length:]
        key = (src, sport, dst, dport)
        flow = self._flows.get(key)
        if flow is not None and flow.client is not None and not flow.established:
            # Our SYN is out: only the device's answer to it counts (RFC 9293, 3.10.7.3).
            if not flags & ACK or ack != (ISN + 1) & 0xFFFFFFFF:
                return
            if flags & RST:
                self._drop(flow, "refused")
                return
            if not flags & SYN:
                return
            mss = _mss(segment[20:header_length])
            if mss is None:
                return
            flow.mss = max(1, min(mss, MSS))
            flow.rcv_nxt = (seq + 1) & 0xFFFFFFFF
            flow.snd_una = ack
            flow.peer_window = window
            flow.established = True
            self._segment(flow, ACK)
            self._log(f"net: TCP {dst}:{dport} → {src}:{sport} established")
            flow.client.on_established()
            # What else the segment carries follows its SYN.
            seq = (seq + 1) & 0xFFFFFFFF
            flags &= ~SYN
        if flags & RST:
            if flow is not None:
                self._drop(flow, "reset")
            return
        if flags & SYN and flow is None:
            mss = _mss(segment[20:header_length])
            if mss is None:
                return
            flow = _Flow(key, (seq + 1) & 0xFFFFFFFF, ISN, ISN, window, max(1, min(mss, MSS)))
            service = self.tcp.get(dport)
            if service is None:
                self._log(f"net: TCP {src}:{sport} → {dst}:{dport} refused, nothing listens there")
                flow.snd_nxt = 0
                self._segment(flow, RST | ACK)
                return
            self._flows[key] = flow
            self._segment(flow, SYN | ACK, options=struct.pack(">BBH", 2, 4, MSS))
            self._log(f"net: TCP {src}:{sport} → {dst}:{dport} accepted")
            flow.connection = service.accept(
                (src, sport), lambda out: self._write(flow, out), lambda: self._close(flow)
            )
            return
        if flow is None:
            return
        if flags & ACK:
            # Lift the wire sequence into our unbounded send counter, including wraparound.
            ack = flow.snd_una + ((ack - flow.snd_una) & 0xFFFFFFFF)
            if ack > flow.snd_nxt:
                return
            flow.snd_una = ack
            if ack >= ISN + 1:
                flow.established = True
            flow.peer_window = window
        if not flow.established:
            return
        ahead = (seq - flow.rcv_nxt) & 0xFFFFFFFF
        if flow.client is not None and (data or flags & FIN) and 0 < ahead < 1 << 31:
            line = (
                f"net: TCP {dst}:{dport} → {src}:{sport}: the device sent sequence {seq},"
                f" {ahead} bytes ahead of {flow.rcv_nxt}, which a link that loses nothing"
                " cannot produce"
            )
            self._log(line)
            raise OutOfOrder(line)
        if data and seq == flow.rcv_nxt:
            flow.rcv_nxt = (flow.rcv_nxt + len(data)) & 0xFFFFFFFF
            self._segment(flow, ACK)
            assert flow.connection is not None
            flow.connection.on_data(data)
        elif data:
            self._segment(flow, ACK)  # a retransmission: acknowledged again
        if flags & FIN and (seq + len(data)) & 0xFFFFFFFF == flow.rcv_nxt:
            flow.rcv_nxt = (flow.rcv_nxt + 1) & 0xFFFFFFFF
            flow.peer_closed = True
            self._segment(flow, ACK)
            self._notify(flow)
            self._close(flow)
        self._flush(flow)
        if flow.fin_sent and flow.peer_closed and flow.snd_una >= flow.snd_nxt:
            self._drop(flow, "closed")

    def _current(self, flow: _Flow) -> bool:
        """Whether `flow` is still the one the table holds for its key: a service may call the
        `write` or `close` of a flow that has ended, after the device opened another on the same
        addresses and ports."""
        return self._flows.get(flow.key) is flow

    def _write(self, flow: _Flow, data: bytes) -> None:
        if flow.fin_pending or not self._current(flow):
            return
        flow.pending += data
        self._flush(flow)

    def _close(self, flow: _Flow) -> None:
        if not self._current(flow):
            return
        if flow.orphaned:
            # Its FIN would reach no stack that holds the connection: kept, the flow would take
            # the SYN of a new stack from the same port for the rest of the run.
            self._drop(flow, "abandoned")
            return
        flow.fin_pending = True
        self._flush(flow)

    def _flush(self, flow: _Flow) -> None:
        if not self._current(flow) or not flow.established:
            return
        while flow.pending:
            room = flow.peer_window - (flow.snd_nxt - flow.snd_una)
            size = min(len(flow.pending), flow.mss, room, self._mtu() - 40)
            if size <= 0:
                return
            chunk = bytes(flow.pending[:size])
            del flow.pending[:size]
            self._segment(flow, ACK | PSH, chunk)
            if flow.client is not None:
                flow.client.on_sent(chunk)
        if (
            flow.fin_pending
            and not flow.fin_sent
            and flow.peer_window > flow.snd_nxt - flow.snd_una
        ):
            flow.fin_sent = True
            self._segment(flow, FIN | ACK)

    def _drop(self, flow: _Flow, kind: str, why: str | None = None) -> None:
        """End `flow`: `kind` is `"closed"`, `"refused"`, `"reset"` or `"lost"`, and for a client's
        lost flow `why` says why, the link going down unless given; `"abandoned"` and
        `"unanswered"` are for the device's flows only."""
        if self._current(flow):
            del self._flows[flow.key]
        device, dport, ours, oport = flow.key
        if flow.client is not None:
            # Before `_notify`, so that the client's `on_close` stands for a FIN only.
            flow.client.on_end(kind, why)
            self._notify(flow)
            end = f"lost: {why}" if why is not None else _ENDS[kind]
            self._log(f"net: TCP {ours}:{oport} → {device}:{dport} {end}")
        else:
            self._notify(flow)
            self._log(f"net: TCP {device}:{dport} → {ours}:{oport} {_ENDS[kind]}")

    def _notify(self, flow: _Flow) -> None:
        if not flow.notified and flow.connection is not None:
            flow.notified = True
            flow.connection.on_close()

    def forget_device(self, why: str | None = None, *, address: str | None = None) -> None:
        """The device's stack is gone, `why` saying how, as `the device reset`, the link going
        down unless given; only the connections on the device's `address`, when given, as for a
        lease it gave up. Lose every connection the test opened, whose device end went with it.
        The device's own stay open and silent, as no network on the internet hears of it, and
        orphaned: their service's close ends them at once, since no FIN would be answered."""
        for flow in list(self._flows.values()):
            if address is not None and flow.key[0] != address:
                continue
            if flow.client is not None:
                self._drop(flow, "lost", why)
            elif not self._unfinished(flow):
                flow.orphaned = True

    def _unfinished(self, flow: _Flow) -> bool:
        """Drop a device flow that cannot finish now that nothing will answer what the network
        sent it: one its service had closed, whose FIN went unanswered, and one still waiting
        for the ACK of its SYN-ACK, which has no service deadline to end it. Kept, either would
        take a new stack's SYN from the same port. Whether it was dropped."""
        if flow.fin_pending:
            self._drop(flow, "abandoned")
        elif not flow.established:
            self._drop(flow, "unanswered")
        else:
            return False
        return True

    def lose_link(self, *, orphan: bool = True) -> None:
        """The link ended. With `orphan`, the device's stack went with it: `forget_device`.
        Without, as when a virtual station leaves the device's access point, the device keeps
        its connections: lose the test's, and drop only those of the device's that cannot
        finish, since what went to them in the meantime is not sent again."""
        if orphan:
            self.forget_device()
            return
        for flow in list(self._flows.values()):
            if flow.client is not None:
                self._drop(flow, "lost")
            else:
                self._unfinished(flow)

    def reset(self) -> None:
        """Lose every TCP connection, the device's included; notify services once, MQTT Wills
        included. No end of a link does this: see `Internet`."""
        for flow in list(self._flows.values()):
            self._drop(flow, "lost")
        self._ident = 0


def _mss(options: bytes) -> int | None:
    """The MSS a SYN's options offer, 536 when they offer none (RFC 9293, 3.7.1); `None` for
    options that do not parse."""
    mss = 536
    while options:
        kind = options[0]
        if kind == 0:
            break
        if kind == 1:
            options = options[1:]
            continue
        if len(options) < 2 or not 2 <= options[1] <= len(options):
            return None
        if kind == 2 and options[1] == 4:
            mss = struct.unpack(">H", options[2:4])[0]
        options = options[options[1] :]
    return mss
