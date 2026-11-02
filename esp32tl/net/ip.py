"""The network the device reaches through its link: IPv4, UDP and a TCP that only ever accepts.

Every destination address answers. A TCP connection goes to the service listening on its
destination port, a UDP datagram to the one on its port, whatever the address: the device keeps
the names and addresses it was built with, and the test decides what answers there. The link to
the device never loses or reorders bytes, so TCP needs no retransmission, no congestion control
and no timers: a segment is acknowledged in the turn it arrives in.
"""

import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

TCP, UDP, ICMP = 6, 17, 1
FIN, SYN, RST, PSH, ACK = 0x01, 0x02, 0x04, 0x08, 0x10
WINDOW = 8192
ISN = 0x10000  # our initial sequence number: fixed, so runs repeat


def checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    total = int(sum(struct.unpack(f">{len(data) // 2}H", data)))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def addr(raw: bytes) -> str:
    return ".".join(str(b) for b in raw)


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

    def receive(self, datagram: bytes) -> None:
        if len(datagram) < 20 or datagram[0] >> 4 != 4:
            return
        version_ihl, _, total = struct.unpack(">BBH", datagram[:4])
        ihl = (version_ihl & 0x0F) * 4
        if not 20 <= ihl <= total <= len(datagram) or checksum(datagram[:ihl]):
            return
        if int.from_bytes(datagram[6:8], "big") & 0x3FFF:
            self._log("net: fragmented IPv4 is unsupported")
            return
        protocol = datagram[9]
        src, dst = addr(datagram[12:16]), addr(datagram[16:20])
        payload = datagram[ihl:total]
        if protocol == TCP:
            if len(payload) < 20 or checksum(self._pseudo(src, dst, TCP, len(payload)) + payload):
                return
            self._tcp(src, dst, payload)
        elif protocol == UDP:
            if len(payload) < 8:
                return
            sport, dport, length, check = struct.unpack(">HHHH", payload[:8])
            if not 8 <= length <= len(payload):
                return
            payload = payload[:length]
            if check and checksum(self._pseudo(src, dst, UDP, length) + payload):
                return
            service = self.udp.get(dport)
            if service is None:
                self._log(f"net: UDP {src}:{sport} → {dst}:{dport}, nothing listens there")
                return
            answer = service.on_datagram(payload[8:])
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
        return (
            bytes(int(p) for p in src.split("."))
            + bytes(int(p) for p in dst.split("."))
            + struct.pack(">BBH", 0, protocol, length)
        )

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

    def _tcp(self, src: str, dst: str, segment: bytes) -> None:
        sport, dport, seq, ack, offset, flags, window = struct.unpack(">HHIIBBH", segment[:16])
        header_length = (offset >> 4) * 4
        if not 20 <= header_length <= len(segment):
            return
        data = segment[header_length:]
        key = (src, sport, dst, dport)
        flow = self._flows.get(key)
        if flags & RST:
            if flow is not None:
                self._drop(flow, "reset by the device")
            return
        if flags & SYN and flow is None:
            mss = 536
            options = segment[20:header_length]
            while options:
                kind = options[0]
                if kind == 0:
                    break
                if kind == 1:
                    options = options[1:]
                    continue
                if len(options) < 2 or not 2 <= options[1] <= len(options):
                    return
                if kind == 2 and options[1] == 4:
                    mss = struct.unpack(">H", options[2:4])[0]
                options = options[options[1] :]
            flow = _Flow(key, (seq + 1) & 0xFFFFFFFF, ISN, ISN, window, max(1, min(mss, 1400)))
            service = self.tcp.get(dport)
            if service is None:
                self._log(f"net: TCP {src}:{sport} → {dst}:{dport} refused, nothing listens there")
                flow.snd_nxt = 0
                self._segment(flow, RST | ACK)
                return
            self._flows[key] = flow
            self._segment(flow, SYN | ACK, options=struct.pack(">BBH", 2, 4, 1400))
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

    def _write(self, flow: _Flow, data: bytes) -> None:
        if flow.fin_pending or flow.key not in self._flows:
            return
        flow.pending += data
        self._flush(flow)

    def _close(self, flow: _Flow) -> None:
        flow.fin_pending = True
        self._flush(flow)

    def _flush(self, flow: _Flow) -> None:
        if flow.key not in self._flows or not flow.established:
            return
        while flow.pending:
            room = flow.peer_window - (flow.snd_nxt - flow.snd_una)
            size = min(len(flow.pending), flow.mss, room, self._mtu() - 40)
            if size <= 0:
                return
            chunk = bytes(flow.pending[:size])
            del flow.pending[:size]
            self._segment(flow, ACK | PSH, chunk)
        if (
            flow.fin_pending
            and not flow.fin_sent
            and flow.peer_window > flow.snd_nxt - flow.snd_una
        ):
            flow.fin_sent = True
            self._segment(flow, FIN | ACK)

    def _drop(self, flow: _Flow, why: str) -> None:
        self._flows.pop(flow.key, None)
        self._notify(flow)
        device, dport, ours, oport = flow.key
        self._log(f"net: TCP {device}:{dport} → {ours}:{oport} {why}")

    def _notify(self, flow: _Flow) -> None:
        if not flow.notified and flow.connection is not None:
            flow.notified = True
            flow.connection.on_close()

    def reset(self) -> None:
        """Lose every TCP connection; notify services once, including MQTT Wills."""
        for flow in list(self._flows.values()):
            self._drop(flow, "link lost")
        self._ident = 0
