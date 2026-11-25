"""The network the device reaches through its link: IPv4, UDP and TCP.

Every destination address answers. A TCP connection goes to the service listening on its
destination port, a UDP datagram to the one on its port, whatever the address: the device keeps the
names and addresses it was built with, and the test decides what answers there. The test opens
connections to the device too (`Network.connect`). The link to the device never loses or reorders
bytes, but the device's stack drops segments it has no room for: lwIP drops every segment with data
while its application has not taken the last one (`refused_data`), and sends no duplicate ACK for
it. So TCP keeps what it sent until the device acknowledges it, and when an RTO runs out with data
or a FIN unacknowledged, it sends one segment again from `snd_una`, doubles the RTO, and with each
ACK that moves `snd_una` adds a segment to what of the rest may go again, as slow start does after
an RTO (RFC 5681, 3.1); what the service writes meanwhile goes as the window allows. The RTO is RFC
6298's, on the board's clock: 1 s before the first sample, 3 s when the handshake went twice and
gave none (5.7), then SRTT + 4 RTTVAR from one segment timed at a time and never a retransmitted
one (Karn), at least 1 s (2.4: lwIP delays an ACK by up to 250 ms) and at most 120 s (Linux's
`TCP_RTO_MAX`). After `RETRIES` (15, Linux's `tcp_retries2`) retransmissions unacknowledged, the
RTO after the last ends the connection. There is no fast retransmit, no congestion control beyond
that recovery and no SACK: lwIP's drops draw no duplicate ACK, so the RTO is what recovers them. A
zero window waits for the device's update. The SYN of a connection the test opened has its timer
too: the device's stack drops a SYN to an address it has not taken yet, silently, so the SYN goes
again on the board's clock, 1 s after the first and each time twice as long after the last (RFC
6298's initial RTO doubling), 6 times as Linux's `tcp_syn_retries` does, and the connection times
out 64 s after the last, 127 s after the first. While the link is down, the segments of a
connection its end left open go nowhere, and the device's stack, which lost the connection too,
never asks for them again: an orphaned flow keeps no retransmission timer. A port the test blocked
(`Network.block`) drops the device's segments, or refuses its SYNs, and what a connection on a
dropped port has to send waits for `unblock` rather than be lost. While a flow is held so, by a
blocked port, a path that drops every datagram or a link that carries no IP, its retransmission
timer stops, and it starts again, with the RTO it had, when the hold ends.
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
SYN_RTO_NS = 1_000_000_000  # how long the first SYN waits for an answer (RFC 6298, 2.1)
SYN_RETRIES = 6  # how many times a SYN goes again before the connection times out (Linux's)
RTO_NS = 1_000_000_000  # the RTO before the first RTT sample (RFC 6298, 2.1)
RTO_MIN_NS = 1_000_000_000  # the least RTO (RFC 6298, 2.4), above lwIP's delayed ACK of 250 ms
RTO_MAX_NS = 120_000_000_000  # the most an RTO doubles to (Linux's TCP_RTO_MAX)
RETRIES = 15  # retransmissions unacknowledged before the connection times out (tcp_retries2)
# The RTO data starts with when the handshake gave no sample because its SYN or SYN-ACK went again
# (RFC 6298, 5.7; Linux's TCP_TIMEOUT_FALLBACK).
RTO_AFTER_SYN_NS = 3_000_000_000


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


class TcpClient(Protocol):
    """The test's side of a connection it opened: told of each SYN as it goes onto the link
    (`on_syn`), when the device accepts it, each piece of what the test wrote as it goes onto the
    link (`on_sent`), each in-order byte the device sends, the device's FIN (`on_close`), and the
    connection's end, once, with its kind, `"closed"`, `"refused"`, `"reset"`, `"lost"` or
    `"timed out"`, and for `"lost"` why, as `the link went down`."""

    def on_syn(self) -> None: ...

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
    "renewed": "reset: the device opened a new connection from its port",
    "gapped": "lost with what it had on its way when the link went",
    "timed out": f"timed out: no answer to {1 + SYN_RETRIES} SYNs",
    "unacknowledged": f"timed out: {RETRIES} retransmissions unacknowledged",
}
# Why a connection the test opened was lost when its retransmissions went unacknowledged.
UNACKNOWLEDGED = f"its {RETRIES} retransmissions went unacknowledged"


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
    client: TcpClient | None = None  # set on a connection the test opened
    # How many SYNs a client's flow has sent, and when the last went: its client keeps them all.
    syns: int = 0
    syn_at: int = 0
    # The device's stack that held it is gone, by a chip reset or the link's end: no FIN to it
    # would ever be answered.
    orphaned: bool = False
    # What its service wrote, orphaned, that the device's window had no room for: not kept.
    discarded: int = 0
    # What went and the device has not acknowledged, from `snd_una`, its FIN apart.
    unacked: bytearray = field(default_factory=bytearray)
    # The retransmission timer: when it runs out, `None` while stopped; the RTO it runs for;
    # how many times what is unacknowledged went again.
    rto_at: int | None = None
    rto: int = RTO_NS
    retries: int = 0
    # RFC 6298's estimate, `None` before the first sample, and the segment being timed: the
    # sequence its ACK reaches, and when it went.
    srtt: int | None = None
    rttvar: int = 0
    timing: tuple[int, int] | None = None
    # Whether the device's SYN-ACK went again (`SYN again, answered again`).
    syn_ack_again: bool = False
    # Recovery after an RTO, as slow start (RFC 5681, 3.1): `None` outside it, else how many bytes
    # sent again may be unacknowledged, one segment at the RTO and one more with each ACK that
    # moves `snd_una`. What goes again is what was unacknowledged at the RTO, up to `recover`,
    # its FIN behind it when `recover_fin`; `rtx_nxt` is the next sequence to send again.
    cwnd: int | None = None
    recover: int = 0
    recover_fin: bool = False
    rtx_nxt: int = 0
    # How many RTOs in a row found the device's window zero and sent their byte into it.
    probes: int = 0


class Network:
    """`send_ip` puts a datagram on the device's link; `log` gets a line per connection event."""

    def __init__(
        self,
        send_ip: Callable[[bytes], None],
        log: Callable[[str], None] = lambda line: None,
        *,
        mtu: Callable[[], int] = lambda: 1500,
        carries: Callable[[], bool] = lambda: True,
        now: Callable[[], int] = lambda: 0,
    ) -> None:
        self._send_ip = send_ip
        self._now = now
        self._mtu = mtu
        # Whether the link carries IP to the device now: while it does not, what a flow that is
        # not orphaned has to send waits for it, since nothing would send it again.
        self._carries = carries
        self._log = log
        self.tcp: dict[int, StreamService] = {}
        self.udp: dict[int, DatagramService] = {}
        self._flows: dict[tuple[str, int, str, int], _Flow] = {}
        # The TCP ports of this network the test blocked, and how: `drop` or `refuse`.
        self.blocked: dict[int, str] = {}
        # A probe was due while the link carried no IP: it goes when `resume` brings the link back.
        self._owed = False
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

    def _segment(
        self,
        flow: _Flow,
        flags: int,
        data: bytes = b"",
        options: bytes = b"",
        *,
        seq: int | None = None,
    ) -> None:
        """Send a segment from `snd_nxt`, which it moves on; from `seq`, a retransmission's, when
        given, which moves nothing."""
        device, dport, ours, oport = flow.key
        offset = (20 + len(options)) // 4
        header = struct.pack(
            ">HHIIBBHHH",
            oport,
            dport,
            (flow.snd_nxt if seq is None else seq) & 0xFFFFFFFF,
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
        if seq is None:
            flow.snd_nxt += len(data) + (1 if flags & (SYN | FIN) else 0)

    def connect(
        self, device: str, port: int, ours: str, client: TcpClient
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
        self._syn(flow)
        return local, lambda out: self._write(flow, out), lambda: self._close(flow)

    def _syn(self, flow: _Flow) -> None:
        """Send a client's SYN, the first or again: the same segment each time."""
        assert flow.client is not None
        flow.snd_nxt = ISN
        flow.syns += 1
        flow.syn_at = self._now()
        # Only the first SYN is timed: the answer to one sent again could be to either (Karn).
        flow.timing = (ISN + 1, flow.syn_at) if flow.syns == 1 else None
        self._segment(flow, SYN, options=struct.pack(">BBH", 2, 4, MSS))
        flow.client.on_syn()

    @staticmethod
    def _syn_due(flow: _Flow) -> int:
        """When a client's SYN goes again, or after the last, when its connection times out: one
        RTO after the last SYN, the RTO doubling from `SYN_RTO_NS` with each one sent."""
        return flow.syn_at + (SYN_RTO_NS << (flow.syns - 1))

    def _connecting(self) -> list[_Flow]:
        return [f for f in self._flows.values() if f.client is not None and not f.established]

    def poll(self) -> None:
        """Send again each SYN of the test's whose RTO has run out by now, and time out the
        connection whose last one has; then the same for what established connections sent and
        the device has not acknowledged (`_retransmit`)."""
        now = self._now()
        for flow in list(self._flows.values()):
            due = self._rto_due(flow)
            if due is not None and due <= now:
                self._retransmit(flow)
        for flow in self._connecting():
            if self._syn_due(flow) > now:
                continue
            device, dport, ours, oport = flow.key
            if flow.syns > SYN_RETRIES:
                self._drop(flow, "timed out")
                continue
            self._log(
                f"net: TCP {ours}:{oport} → {device}:{dport} SYN again,"
                f" {flow.syns + 1} of {1 + SYN_RETRIES}"
            )
            self._syn(flow)

    def quiet_until(self) -> int | None:
        """When the next SYN of the test's goes again, or its connection times out, or the next
        retransmission timer runs out."""
        syns = (self._syn_due(flow) for flow in self._connecting())
        rtos = (self._rto_due(flow) for flow in self._flows.values())
        return min((due for due in (*syns, *rtos) if due is not None), default=None)

    def _stalled(self, flow: _Flow) -> bool:
        """Whether what `flow` has to send waits: its port is blocked with `drop`, or the link
        carries no IP. An orphaned flow sends into the link all the same."""
        return self._held(flow) or (not flow.orphaned and not self._carries())

    def _rto_due(self, flow: _Flow) -> int | None:
        """When `flow`'s retransmission timer runs out; `None` while it is stopped: nothing is
        unacknowledged, the flow is held, or orphaned, as nothing would acknowledge it."""
        if flow.rto_at is None or flow.orphaned or not flow.established:
            return None
        if not self._current(flow) or self._stalled(flow):
            return None
        return flow.rto_at

    def _retransmit(self, flow: _Flow) -> None:
        """The RTO ran out: after `RETRIES` retransmissions the connection times out; else send
        again one segment from `snd_una`, or its FIN, double the RTO (RFC 6298, 5.4 to 5.6), and
        recover the rest as the device acknowledges it (`_resend`). What goes again is not timed
        (Karn), not told to a client's `on_sent`, and not recorded again."""
        if flow.retries >= RETRIES:
            if flow.client is not None:
                self._drop(flow, "lost", UNACKNOWLEDGED)
            else:
                self._drop(flow, "unacknowledged")
            return
        rto = flow.rto
        flow.retries += 1
        flow.rto = min(2 * flow.rto, RTO_MAX_NS)
        flow.timing = None
        flow.cwnd = self._segment_size(flow)
        flow.recover = flow.snd_una + len(flow.unacked)
        flow.recover_fin = flow.fin_sent
        flow.rtx_nxt = flow.snd_una
        if flow.peer_window:
            flow.probes = 0
            count = f"the {_ordinal(flow.retries)} time"
        else:
            # A device that answers keeps `retries` at 1: the probes are counted apart.
            flow.probes += 1
            count = f"the {_ordinal(flow.probes)} probe of a zero window"
        self._resend(flow, f"{count}, after an RTO of {_seconds(rto)}")
        flow.rto_at = self._now() + flow.rto

    def _segment_size(self, flow: _Flow) -> int:
        return max(1, min(flow.mss, self._mtu() - 40))

    def _resend(self, flow: _Flow, why: str) -> None:
        """Send again from `rtx_nxt` what `unacked` holds there, up to `recover`, while what went
        again and is unacknowledged stays within `cwnd` and the device's window, or 1 byte, which
        probes a zero window; then the FIN, once every byte before it went again. One line in the
        log says what went, and `why`."""
        assert flow.cwnd is not None
        if self._stalled(flow):
            return
        limit = min(flow.cwnd, max(flow.peer_window, 1))
        start, sent = flow.rtx_nxt, 0
        while flow.rtx_nxt < flow.recover:
            offset = flow.rtx_nxt - flow.snd_una
            size = min(flow.recover - flow.rtx_nxt, self._segment_size(flow), limit - offset)
            if size <= 0:
                break
            chunk = bytes(flow.unacked[offset : offset + size])
            self._segment(flow, ACK | PSH, chunk, seq=flow.rtx_nxt)
            flow.rtx_nxt += size
            sent += size
        fin = flow.recover_fin and flow.rtx_nxt == flow.recover
        if fin:
            self._segment(flow, FIN | ACK, seq=flow.rtx_nxt)
            flow.rtx_nxt += 1
        if not sent and not fin:
            return
        what = f"{sent} byte{'' if sent == 1 else 's'}" + (" and its FIN" if fin and sent else "")
        what = "its FIN" if fin and not sent else what
        device, dport, ours, oport = flow.key
        self._log(
            f"net: TCP {ours}:{oport} → {device}:{dport}: sent again {what} from sequence"
            f" {start & 0xFFFFFFFF}, {why}"
        )

    def _sample(self, flow: _Flow, ack: int) -> None:
        """Take the RTT of the segment being timed once `ack` covers it, and set the RTO from it
        (RFC 6298, 2.2 and 2.3), which ends a backoff."""
        if flow.timing is None or ack < flow.timing[0]:
            return
        r = self._now() - flow.timing[1]
        flow.timing = None
        if flow.srtt is None:
            flow.srtt, flow.rttvar = r, r // 2
        else:
            flow.rttvar = (3 * flow.rttvar + abs(flow.srtt - r)) // 4
            flow.srtt = (7 * flow.srtt + r) // 8
        flow.rto = min(max(flow.srtt + 4 * flow.rttvar, RTO_MIN_NS), RTO_MAX_NS)

    def _acked(self, flow: _Flow, ack: int) -> None:
        """The device acknowledged up to `ack`, above `snd_una`: drop what it covers from
        `unacked`, and restart the timer, or stop it when nothing is left (RFC 6298, 5.2, 5.3)."""
        self._sample(flow, ack)
        flow.snd_una = ack
        data_end = flow.snd_nxt - (1 if flow.fin_sent else 0)
        keep = max(0, data_end - ack)
        del flow.unacked[: len(flow.unacked) - keep]
        flow.retries = 0
        flow.probes = 0
        flow.rto_at = None if ack >= flow.snd_nxt else self._now() + flow.rto
        if flow.cwnd is None:
            return
        if ack >= flow.recover + flow.recover_fin:
            flow.cwnd = None  # recovered
            return
        flow.cwnd += self._segment_size(flow)
        flow.rtx_nxt = max(flow.rtx_nxt, ack)
        self._resend(flow, f"after the device's ACK of sequence {ack & 0xFFFFFFFF}")

    def _established(self, flow: _Flow) -> None:
        """The handshake is over: when its SYN or SYN-ACK went again and it gave no RTT sample,
        data starts with an RTO of `RTO_AFTER_SYN_NS` (RFC 6298, 5.7)."""
        flow.established = True
        again = flow.syns > 1 if flow.client is not None else flow.syn_ack_again
        if again and flow.srtt is None:
            flow.rto = max(flow.rto, RTO_AFTER_SYN_NS)

    def _restart(self, flow: _Flow) -> None:
        """A hold ended: the timer of what is unacknowledged starts again with the RTO it had,
        and the segment being timed, which waited through the hold, is not."""
        flow.timing = None
        if flow.rto_at is not None:
            flow.rto_at = self._now() + flow.rto

    def _tcp(self, src: str, dst: str, segment: bytes) -> None:
        sport, dport, seq, ack, offset, flags, window = struct.unpack(">HHIIBBH", segment[:16])
        header_length = (offset >> 4) * 4
        if not 20 <= header_length <= len(segment):
            return
        data = segment[header_length:]
        key = (src, sport, dst, dport)
        flow = self._flows.get(key)
        how = self.blocked.get(dport) if flow is None or flow.client is None else None
        if how == "drop":
            self._log(f"net: TCP {src}:{sport} → {dst}:{dport} dropped: port {dport} is blocked")
            return
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
            self._sample(flow, ack)
            flow.snd_una = ack
            flow.peer_window = window
            self._established(flow)
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
        if (
            flags & SYN
            and not flags & ACK
            and flow is not None
            and flow.orphaned
            and _mss(segment[20:header_length]) is not None
        ):
            # A new stack's SYN on a tuple only a gone stack held: a challenge ACK (RFC 9293,
            # 3.10.7.4) would draw its RST, which resets the old connection, and its SYN again.
            self._drop(flow, "renewed")
            flow = None
        if (
            flags & SYN
            and not flags & ACK
            and flow is not None
            and flow.client is None
            and not flow.established
            and (seq + 1) & 0xFFFFFFFF == flow.rcv_nxt
        ):
            # The device's SYN again, on a flow still waiting for the ACK of its SYN-ACK: that
            # SYN-ACK was lost, as in a black hole. Answered again, as in SYN-RECEIVED (RFC 9293,
            # 3.10.7.4); nothing else does, since the SYN-ACK has no timer.
            self._log(f"net: TCP {src}:{sport} → {dst}:{dport} SYN again, answered again")
            flow.snd_nxt = ISN
            flow.timing = None  # its ACK could answer either SYN-ACK (Karn)
            flow.syn_ack_again = True
            self._segment(flow, SYN | ACK, options=struct.pack(">BBH", 2, 4, MSS))
            return
        if flags & SYN and flow is None:
            mss = _mss(segment[20:header_length])
            if mss is None:
                return
            flow = _Flow(key, (seq + 1) & 0xFFFFFFFF, ISN, ISN, window, max(1, min(mss, MSS)))
            service = self.tcp.get(dport)
            if service is None or how == "refuse":
                why = "nothing listens there" if how is None else f"port {dport} is blocked"
                self._log(f"net: TCP {src}:{sport} → {dst}:{dport} refused, {why}")
                flow.snd_nxt = 0
                self._segment(flow, RST | ACK)
                return
            self._flows[key] = flow
            self._log(f"net: TCP {src}:{sport} → {dst}:{dport} accepted")
            flow.timing = (ISN + 1, self._now())
            self._segment(flow, SYN | ACK, options=struct.pack(">BBH", 2, 4, MSS))
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
            flow.peer_window = window
            if ack > flow.snd_una:
                self._acked(flow, ack)
            elif not window and flow.snd_una < flow.snd_nxt:
                # The device answers with its zero window, as to the RTO's byte: alive, it is
                # owed no end for want of an ACK, as Linux's persist state counts none.
                flow.retries = 0
            if ack >= ISN + 1 and not flow.established:
                self._established(flow)
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
        if flow.orphaned and flow.pending:
            # Nothing will acknowledge what went: what the window has no room for would only
            # pile up until the service closes.
            flow.discarded += len(flow.pending)
            flow.pending.clear()

    def _close(self, flow: _Flow) -> None:
        if not self._current(flow):
            return
        if flow.orphaned:
            # Its FIN would reach no stack that holds the connection, and nothing would answer
            # it: kept, the flow would wait for that answer.
            self._drop(flow, "abandoned")
            return
        flow.fin_pending = True
        self._flush(flow)

    def _held(self, flow: _Flow) -> bool:
        """Whether `flow` is a device's on a port blocked with `drop`."""
        return flow.client is None and self.blocked.get(flow.key[3]) == "drop"

    def _flush(self, flow: _Flow) -> None:
        if not self._current(flow) or not flow.established:
            return
        if self._held(flow):
            return  # it waits for `unblock`, as what a real network loses is sent again
        if not flow.orphaned and not self._carries():
            return  # it waits for the link, in order, its FIN behind it
        while flow.pending:
            room = flow.peer_window - (flow.snd_nxt - flow.snd_una)
            size = min(len(flow.pending), flow.mss, room, self._mtu() - 40)
            if size <= 0:
                return
            chunk = bytes(flow.pending[:size])
            del flow.pending[:size]
            self._sending(flow, len(chunk))
            flow.unacked += chunk
            self._segment(flow, ACK | PSH, chunk)
            if flow.client is not None:
                flow.client.on_sent(chunk)
        if (
            flow.fin_pending
            and not flow.fin_sent
            and flow.peer_window > flow.snd_nxt - flow.snd_una
        ):
            flow.fin_sent = True
            self._sending(flow, 1)
            self._segment(flow, FIN | ACK)

    def _sending(self, flow: _Flow, length: int) -> None:
        """New data or a FIN of `length` goes from `snd_nxt`: time it unless a segment is being
        timed already, and start the timer unless it runs (RFC 6298, 5.1)."""
        now = self._now()
        if flow.timing is None:
            flow.timing = (flow.snd_nxt + length, now)
        if flow.rto_at is None:
            flow.rto_at = now + flow.rto

    def _drop(self, flow: _Flow, kind: str, why: str | None = None) -> None:
        """End `flow`: `kind` is `"closed"`, `"refused"`, `"reset"` or `"lost"`, and for a client's
        lost flow `why` says why, the link going down unless given; `"timed out"` is for the
        test's flows only, and `"abandoned"`, `"unanswered"` and `"unacknowledged"` for the
        device's only."""
        if self._current(flow):
            del self._flows[flow.key]
        device, dport, ours, oport = flow.key
        if flow.discarded:
            self._log(
                f"net: TCP {device}:{dport} → {ours}:{oport} discarded {flow.discarded} bytes its"
                " service wrote with no device to answer"
            )
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
        Drop the device's own that cannot finish (`_unfinished`): one still in its handshake,
        one its service had closed. The rest stay open and silent, as no network on the
        internet hears of it, and orphaned: their service's close ends them at once, since no
        FIN would be answered."""
        if address is None:
            self._owed = False  # what it was owed to went with the device's stack
        for flow in list(self._flows.values()):
            if address is not None and flow.key[0] != address:
                continue
            if flow.client is not None:
                self._drop(flow, "lost", why)
            elif not self._unfinished(flow):
                flow.orphaned = True
                # What waited for the window: nothing will acknowledge what went before it.
                flow.discarded += len(flow.pending)
                flow.pending.clear()

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

    def block(self, port: int, how: str) -> None:
        """Block TCP `port` of this network: `drop` swallows every segment the device sends to it
        and holds what its connections have to send; `refuse` answers each new SYN to it with a
        RST, the connections already open going on. The test's own connections, from the ports
        it connects from, are not affected."""
        was = self.blocked.get(port)
        self.blocked[port] = how
        self._release(port, probe=was == "drop")

    def unblock(self, port: int) -> None:
        """Lift the block of `port`: what its connections held goes now, and after `drop`, a
        probe asks for the ACKs and windows the block swallowed."""
        was = self.blocked.pop(port)
        self._release(port, probe=was == "drop")

    def _release(self, port: int, *, probe: bool) -> None:
        flows = [f for f in self._flows.values() if f.client is None and f.key[3] == port]
        for flow in flows:
            if probe and not self._stalled(flow):
                self._restart(flow)
            self._flush(flow)
        if probe:
            self._probe(flows)

    def probe(self) -> None:
        """`_probe` every flow."""
        self._probe(None)

    def _probe(self, flows: list[_Flow] | None) -> None:
        """Ask the device for its ACK and window again on each flow, every flow unless `flows`
        are given, that has bytes or a FIN unacknowledged or waiting: what the device sent while
        the path dropped, or while its port was blocked, never came, and with a full or a zero
        window nothing else moves the flow on. The probe is a keepalive's (RFC 9293, 3.8.4), an
        ACK from `snd_nxt - 1` with no data, which the device answers with its ACK and window;
        the answer's `_flush` sends what waited. While the link carries no IP, the probe is owed
        to every flow, and `resume` sends it when the link carries IP again."""
        if not self._carries():
            self._owed = True
            return
        for flow in list(self._flows.values()) if flows is None else flows:
            if not self._current(flow) or not flow.established or flow.orphaned:
                continue
            if self._held(flow):
                continue
            waiting = flow.pending or (flow.fin_pending and not flow.fin_sent)
            if flow.snd_una < flow.snd_nxt or waiting:
                self._segment(flow, ACK, seq=flow.snd_nxt - 1)

    def resume(self, local: str | None = None, *, probe: bool = False) -> None:
        """The link carries IP again: send what waited for it, on every flow from our `local`
        address, when given; one from another, which that end no longer holds, is lost. Then
        `probe` every flow, with `probe` or when a probe was owed."""
        for flow in list(self._flows.values()):
            if local is not None and flow.key[2] != local:
                self._drop(flow, "lost")
            else:
                if not self._stalled(flow):
                    self._restart(flow)
                self._flush(flow)
        if probe or self._owed:
            self._owed = False
            self.probe()

    def lose_link(self, *, orphan: bool = True) -> None:
        """The link ended. With `orphan`, the device's stack went with it: `forget_device`.
        Without, as when a virtual station leaves the device's access point, the device keeps
        its connections: lose the test's, and drop those of the device's that this TCP does not
        finish: one still in its handshake, whose SYN-ACK has no timer; one with bytes or a FIN
        the device has not acknowledged, whose RTO the station's absence stops (keeping it for
        the RTO after the station's return is postponed); and one whose FIN was sent already,
        which waits for the device's FIN with no timer to give up. What the
        rest have to send waits for the link (`carries`), their FIN behind it, and goes in
        `resume`."""
        if orphan:
            self.forget_device()
            return
        for flow in list(self._flows.values()):
            if flow.client is not None:
                self._drop(flow, "lost")
            elif not flow.established:
                self._drop(flow, "unanswered")
            elif flow.snd_una < flow.snd_nxt:
                # What it sent and the device has not acknowledged may be on the cleared path or
                # in the air, and would go again only on an RTO after the station's return.
                self._drop(flow, "gapped")
            elif flow.fin_sent:
                self._drop(flow, "abandoned")

    def reset(self) -> None:
        """Lose every TCP connection, the device's included; notify services once, MQTT Wills
        included. No end of a link does this: see `Internet`."""
        for flow in list(self._flows.values()):
            self._drop(flow, "lost")
        self._ident = 0


def _ordinal(n: int) -> str:
    """`1st`, `2nd`, `3rd`, `4th`, `11th`, `12th`, `13th`, `15th`."""
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _seconds(ns: int) -> str:
    """`1 s`, `2.5 s`, `0.4 s`: to the nanosecond, without trailing zeros."""
    whole, part = divmod(ns, 1_000_000_000)
    return f"{whole}.{part:09d}".rstrip("0").rstrip(".") + " s"


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
