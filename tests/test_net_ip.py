"""Handmade IPv4, UDP and TCP packets, including sequence wrap and connection loss."""

import struct
from collections.abc import Callable

from esp32tl.net.ip import ISN, Network, StreamClose

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
        self.close: StreamClose = lambda *, reset=False: None

    def accept(
        self, peer: tuple[str, int], write: Callable[[bytes], None], close: StreamClose
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


def test_modem_link_loss_leaves_tcp_open_and_the_will_to_the_keepalive() -> None:
    from esp32tl.net import Internet

    now = [10]
    net = Internet(lambda: now[0])
    net.ip.receive(tcp(1, 0, 2))
    net.ip.receive(tcp(2, 65537, 0x10))
    connect = b"\x10\x18\0\x04MQTT\x04\x2e\0\x01\0\x01c\0\x03a/b\0\x04gone"
    net.ip.receive(tcp(2, 65537, 0x18, connect))
    assert len(net.broker.clients) == 1
    net.stop(500)
    net.stop(600)
    # No FIN or RST reaches the broker: its end stays open, and silent, until the keepalive of
    # 1 s runs out 1.5 s after the device's last packet.
    assert net.broker.messages == [] and len(net.ip._flows) == 1
    assert not net.ppp.started and net.poll(600) == []
    expiry = 10 + 1_500_000_000
    assert net.quiet_until() == expiry
    net.arrive(expiry - 1)
    assert net.broker.messages == []
    assert net.poll(expiry) == []
    assert [m.payload for m in net.broker.messages] == [b"gone"]
    assert net.broker.messages[0].at.ns == expiry
    # Its FIN would go nowhere, and nothing would answer it: the flow goes with the session.
    assert not net.ip._flows and net.quiet_until() is None
    lines = [str(e["event"]) for e in net.events if "event" in e]
    assert any(
        line.endswith("1883 closed by its service with no device to answer") for line in lines
    )


# Retransmission: what the device's stack drops goes again on the RTO, on the board's clock.

S = 1_000_000_000
MSS_1400 = b"\x02\x04\x05\x78"


class Device:
    """A device on port 4000 connected to a `Network`'s `Echo` on 1883 at `now` 0, the clock and
    what the network sent in the test's hands."""

    def __init__(self, *, carries: bool = True) -> None:
        self.now = 0
        self.sent: list[bytes] = []
        self.lines: list[str] = []
        self.carries = carries
        self.net = Network(
            self.sent.append,
            self.lines.append,
            now=lambda: self.now,
            carries=lambda: self.carries,
        )
        self.echo = Echo()
        self.net.tcp[1883] = self.echo
        self.net.receive(tcp(0, 0, 2, options=MSS_1400))
        self.seq = 1  # the device's next sequence number

    def established(self, at: int = 0) -> "Device":
        self.now = at
        self.net.receive(tcp(1, ISN + 1, 0x10))
        return self

    def ack(self, ack: int, window: int = 8192) -> None:
        self.net.receive(tcp(self.seq, ack, 0x10, window=window))

    def segments(self, start: int = 0) -> list[tuple[int, int, bytes]]:
        """The sequence number, the flags and the data of each segment sent from `start` on."""
        return [
            (int.from_bytes(d[24:28], "big"), d[33], d[20 + (d[32] >> 4) * 4 :])
            for d in self.sent[start:]
        ]

    def poll(self, at: int) -> list[tuple[int, int, bytes]]:
        self.now = at
        start = len(self.sent)
        self.net.poll()
        return self.segments(start)


def test_a_segment_the_device_drops_goes_again_one_rto_later_and_the_stream_goes_on() -> None:
    device = Device().established()
    device.echo.write(b"first")
    device.ack(ISN + 1 + 5)
    device.now = 10_000_000
    device.echo.write(b"second")  # the device drops it, as lwIP does with refused_data
    device.echo.write(b"third")  # and this one, behind the hole
    assert device.net.quiet_until() == 10_000_000 + S
    assert device.poll(10_000_000 + S - 1) == []
    again = device.poll(10_000_000 + S)
    # From snd_una, as much as the window and the MSS allow: both, in one segment.
    assert again == [(ISN + 6, 0x18, b"secondthird")]
    assert device.lines[-1] == (
        f"net: TCP 203.0.113.153:1883 → 10.64.0.2:4000: sent again 11 bytes from sequence"
        f" {ISN + 6}, the 1st time, after an RTO of 1 s"
    )
    device.ack(ISN + 6 + 11)
    assert device.net.quiet_until() is None
    start = len(device.sent)
    device.echo.write(b"fourth")
    assert device.segments(start) == [(ISN + 17, 0x18, b"fourth")]


def test_an_ack_of_everything_stops_the_timer_and_nothing_goes_again() -> None:
    device = Device().established()
    device.echo.write(b"hello")
    assert device.net.quiet_until() == S
    device.now = 5_000_000
    device.ack(ISN + 6)
    assert device.net.quiet_until() is None
    assert device.poll(1000 * S) == []
    assert not [line for line in device.lines if "sent again" in line]


def test_an_ack_of_part_restarts_the_timer_from_the_ack() -> None:
    device = Device().established()
    device.echo.write(b"one")
    device.echo.write(b"two")
    device.now = 300_000_000
    device.ack(ISN + 4)  # "one" only
    assert device.net.quiet_until() == 300_000_000 + S
    assert device.poll(300_000_000 + S) == [(ISN + 4, 0x18, b"two")]


def test_the_rto_doubles_with_each_retransmission_up_to_its_limit() -> None:
    device = Device().established()
    device.echo.write(b"x" * 10)
    at, deadlines = 0, []
    for _ in range(9):
        due = device.net.quiet_until()
        assert due is not None
        deadlines.append(due - at)
        at = due
        assert device.poll(due) == [(ISN + 1, 0x18, b"x" * 10)]
    assert deadlines == [S, 2 * S, 4 * S, 8 * S, 16 * S, 32 * S, 64 * S, 120 * S, 120 * S]
    again = [line for line in device.lines if "sent again" in line]
    assert again[1].endswith("the 2nd time, after an RTO of 2 s")
    assert again[2].endswith("the 3rd time, after an RTO of 4 s")
    assert again[8].endswith("the 9th time, after an RTO of 120 s")


def test_a_device_that_never_acknowledges_times_out_after_the_last_retransmission() -> None:
    device = Device().established()
    device.echo.write(b"unheard")
    retransmissions = 0
    while (due := device.net.quiet_until()) is not None:
        retransmissions += len(device.poll(due))
    assert retransmissions == 15
    # 1 + 2 + ... + 64 s, then eight RTOs of 120 s, and the one after the 15th.
    assert device.now == (127 + 9 * 120) * S
    assert device.lines[-1] == (
        "net: TCP 10.64.0.2:4000 → 203.0.113.153:1883 timed out: 15 retransmissions unacknowledged"
    )
    assert device.echo.closed == 1 and not device.net._flows


def test_a_fin_unacknowledged_goes_again_behind_the_data() -> None:
    device = Device().established()
    device.echo.write(b"bye")
    device.echo.close()
    assert device.segments()[-2:] == [(ISN + 1, 0x18, b"bye"), (ISN + 4, 0x11, b"")]
    assert device.poll(S) == [(ISN + 1, 0x18, b"bye"), (ISN + 4, 0x11, b"")]
    assert "sent again 3 bytes and its FIN from sequence" in device.lines[-1]
    device.ack(ISN + 4)  # the data, not the FIN
    assert device.poll(S + 2 * S) == [(ISN + 4, 0x11, b"")]
    assert "sent again its FIN from sequence" in device.lines[-1]


def test_the_rto_follows_the_measured_rtt_and_a_retransmission_is_not_measured() -> None:
    # The handshake's ACK 3 s after the SYN-ACK: SRTT 3 s, RTTVAR 1.5 s, RTO 3 + 4 x 1.5 s.
    device = Device().established(at=3 * S)
    device.echo.write(b"a")
    assert device.net.quiet_until() == 3 * S + 9 * S
    device.now = 3 * S + 100_000_000
    device.ack(ISN + 2)  # R = 0.1 s: RTTVAR 1.85 s, SRTT 2.6375 s
    device.echo.write(b"b")
    assert device.net.quiet_until() == device.now + 2_637_500_000 + 4 * 1_850_000_000
    # Sent again: its ACK, whichever it answers, is no sample, and the RTO stays doubled.
    first = device.net.quiet_until()
    assert first is not None
    device.poll(first)
    device.now = first + 50_000_000
    device.ack(ISN + 3)
    device.echo.write(b"c")
    assert device.net.quiet_until() == device.now + 2 * (2_637_500_000 + 4 * 1_850_000_000)


def test_a_held_flow_retransmits_nothing_and_its_timer_starts_again_at_the_release() -> None:
    device = Device().established()
    device.echo.write(b"held")
    device.net.block(1883, "drop")
    assert device.net.quiet_until() is None
    assert device.poll(10 * S) == []
    start = len(device.sent)
    device.net.unblock(1883)
    # The probe, which is no retransmission, and the timer from the release.
    assert device.segments(start) == [(ISN + 4, 0x10, b"")]
    assert device.net.quiet_until() == 11 * S
    assert device.poll(11 * S) == [(ISN + 1, 0x18, b"held")]
    assert device.lines[-1].endswith("the 1st time, after an RTO of 1 s")


def test_a_black_hole_stops_the_timer_until_the_link_carries_again() -> None:
    device = Device().established()
    device.echo.write(b"held")
    device.carries = False
    device.echo.write(b" too")  # waits for the link
    assert device.net.quiet_until() is None
    assert device.poll(30 * S) == []
    start = len(device.sent)
    device.carries = True
    device.net.resume(probe=True)
    assert device.segments(start) == [(ISN + 5, 0x18, b" too"), (ISN + 8, 0x10, b"")]
    assert device.net.quiet_until() == 31 * S
    assert device.poll(31 * S) == [(ISN + 1, 0x18, b"held too")]


def test_an_orphaned_flow_keeps_no_timer() -> None:
    device = Device().established()
    device.echo.write(b"gone")
    device.net.forget_device("the device reset")
    assert device.net.quiet_until() is None
    assert device.poll(100 * S) == []


class Client:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.ends: list[tuple[str, str | None]] = []

    def on_syn(self) -> None: ...

    def on_established(self) -> None: ...

    def on_sent(self, data: bytes) -> None:
        self.sent.append(data)

    def on_data(self, data: bytes) -> None: ...

    def on_close(self) -> None: ...

    def on_end(self, kind: str, why: str | None) -> None:
        self.ends.append((kind, why))


def test_a_connection_the_test_opened_goes_again_once_and_is_lost_when_never_acknowledged() -> None:
    now = [0]
    lines: list[str] = []
    net = Network(lambda datagram: None, lines.append, now=lambda: now[0])
    client = Client()
    local, write, _ = net.connect("10.64.0.2", 1883, "203.0.113.153", client)
    segment = struct.pack(">HHIIBBHHH", 1883, local, 0, ISN + 1, 0x60, 0x12, 8192, 0, 0) + MSS_1400
    pseudo = SRC + DST + struct.pack(">BBH", 0, 6, len(segment))
    net.receive(packet(6, segment[:16] + struct.pack(">H", check(pseudo + segment)) + segment[18:]))
    write(b"request")
    while (due := net.quiet_until()) is not None:
        now[0] = due
        net.poll()
    # Told once of what it wrote, however many times it went.
    assert client.sent == [b"request"]
    assert client.ends == [("lost", "its 15 retransmissions went unacknowledged")]
    assert lines[-1] == (
        f"net: TCP 203.0.113.153:{local} → 10.64.0.2:1883 lost: its 15 retransmissions went"
        " unacknowledged"
    )


def test_two_runs_retransmit_the_same_segments_at_the_same_times() -> None:
    def run() -> tuple[list[bytes], list[str], list[int]]:
        device = Device().established(at=2_000_000)
        times = []
        device.echo.write(b"a" * 3000)
        device.now = 50_000_000
        device.ack(ISN + 1 + 1400)
        for _ in range(3):
            due = device.net.quiet_until()
            assert due is not None
            times.append(due)
            device.poll(due)
        return device.sent, device.lines, times

    assert run() == run()


def test_after_an_rto_each_ack_lets_go_what_it_acknowledged_and_a_segment_more() -> None:
    device = Device().established()
    device.echo.write(b"a" * 1400 + b"b" * 1400 + b"c" * 1400)
    device.echo.write(b"new")  # behind them, in the window
    # Slow start (RFC 5681, 3.1): one segment at the RTO, not the window.
    assert device.poll(S) == [(ISN + 1, 0x18, b"a" * 1400)]
    device.now = S + 10_000_000
    start = len(device.sent)
    device.ack(ISN + 1 + 1400)
    assert device.segments(start) == [
        (ISN + 1401, 0x18, b"b" * 1400),
        (ISN + 2801, 0x18, b"c" * 1400),
    ]
    start = len(device.sent)
    device.ack(ISN + 2801)
    assert device.segments(start) == [(ISN + 4201, 0x18, b"new")]
    assert device.lines[-1] == (
        "net: TCP 203.0.113.153:1883 → 10.64.0.2:4000: sent again 3 bytes from sequence"
        f" {ISN + 4201}, after the device's ACK of sequence {ISN + 2801}"
    )
    start = len(device.sent)
    device.ack(ISN + 4204)
    assert device.net.quiet_until() is None
    device.echo.write(b"next")
    assert device.segments(start) == [(ISN + 4204, 0x18, b"next")]


def test_what_the_service_writes_in_a_recovery_goes_at_once_and_is_not_sent_again_in_it() -> None:
    device = Device().established()
    device.echo.write(b"a" * 1400 + b"b" * 1400)
    device.poll(S)
    start = len(device.sent)
    device.echo.write(b"later")
    assert device.segments(start) == [(ISN + 2801, 0x18, b"later")]
    start = len(device.sent)
    device.ack(ISN + 1 + 1400)  # one more goes again: what was unacknowledged at the RTO only
    assert device.segments(start) == [(ISN + 1401, 0x18, b"b" * 1400)]
    device.ack(ISN + 2801)
    assert device.net._flows[next(iter(device.net._flows))].cwnd is None


def test_an_rto_on_a_zero_window_sends_one_byte() -> None:
    device = Device().established()
    device.echo.write(b"abcd")
    device.ack(ISN + 1, window=0)
    assert device.poll(S) == [(ISN + 1, 0x18, b"a")]


def test_a_fin_goes_again_only_once_every_byte_before_it_has() -> None:
    device = Device().established()
    device.echo.write(b"x" * 3000)
    device.echo.close()
    assert device.poll(S) == [(ISN + 1, 0x18, b"x" * 1400)]
    start = len(device.sent)
    device.ack(ISN + 1401)
    assert device.segments(start) == [
        (ISN + 1401, 0x18, b"x" * 1400),
        (ISN + 2801, 0x18, b"x" * 200),
        (ISN + 3001, 0x11, b""),
    ]
    assert "sent again 1600 bytes and its FIN from sequence" in device.lines[-1]


def test_data_starts_with_an_rto_of_3_s_when_the_handshake_went_twice() -> None:
    # RFC 6298, 5.7: no sample, as the SYN went again.
    now = [0]
    net = Network(lambda datagram: None, now=lambda: now[0])
    client = Client()
    local, write, _ = net.connect("10.64.0.2", 1883, "203.0.113.153", client)
    now[0] = S
    net.poll()  # the SYN again
    now[0] = 1_500_000_000
    segment = struct.pack(">HHIIBBHHH", 1883, local, 0, ISN + 1, 0x60, 0x12, 8192, 0, 0) + MSS_1400
    pseudo = SRC + DST + struct.pack(">BBH", 0, 6, len(segment))
    net.receive(packet(6, segment[:16] + struct.pack(">H", check(pseudo + segment)) + segment[18:]))
    write(b"request")
    assert net.quiet_until() == 1_500_000_000 + 3 * S
    # The device's SYN again, whose SYN-ACK went again: the same.
    device = Device()
    device.now = S
    device.net.receive(tcp(0, 0, 2, options=MSS_1400))
    device.established(at=1_500_000_000)
    device.echo.write(b"hello")
    assert device.net.quiet_until() == 1_500_000_000 + 3 * S


def test_a_device_that_answers_each_rto_with_a_zero_window_keeps_its_connection() -> None:
    device = Device().established()
    device.echo.write(b"abcd")
    device.ack(ISN + 1, window=0)
    for _ in range(20):
        due = device.net.quiet_until()
        assert due is not None
        assert device.poll(due) == [(ISN + 1, 0x18, b"a")]
        device.ack(ISN + 1, window=0)  # lwIP's answer to a byte outside its window
    assert device.net._flows and device.echo.closed == 0
    assert device.lines[-1] == (
        "net: TCP 203.0.113.153:1883 → 10.64.0.2:4000: sent again 1 byte from sequence"
        f" {ISN + 1}, the 20th probe of a zero window, after an RTO of 120 s"
    )


# A service's reset: `close(reset=True)`.


def test_a_reset_drops_what_is_queued_and_sends_a_rst_from_snd_nxt_at_once() -> None:
    device = Device().established()
    device.net.receive(tcp(1, ISN + 1, 0x18, b"hi"))  # echoed: 2 bytes from ISN + 1
    device.seq = 3
    device.ack(ISN + 1, window=2)  # nothing acknowledged, and no room for more
    device.echo.write(b"queued")
    start = len(device.sent)
    device.echo.close(reset=True)
    [rst] = device.sent[start:]
    # RST|ACK from snd_nxt, past what went unacknowledged, acknowledging all the device sent.
    assert device.segments(start) == [(ISN + 3, 0x14, b"")]
    assert int.from_bytes(rst[28:32], "big") == 3
    assert device.lines[-2:] == [
        "net: TCP 10.64.0.2:4000 → 203.0.113.153:1883 dropped 8 bytes the device had not"
        " acknowledged",
        "net: TCP 10.64.0.2:4000 → 203.0.113.153:1883 reset by its service",
    ]
    assert not device.net._flows and device.echo.closed == 1
    # Nothing goes again, and the flow's write and close are no more.
    assert device.net.quiet_until() is None and device.poll(100 * S) == []
    device.echo.write(b"late")
    device.echo.close(reset=True)
    device.echo.close()
    assert len(device.sent) == start + 1


def test_a_reset_after_a_close_still_resets() -> None:
    device = Device().established()
    device.echo.close()
    assert device.segments()[-1] == (ISN + 1, 0x11, b"")
    start = len(device.sent)
    device.echo.close(reset=True)
    # The FIN took a sequence number: the RST follows it.
    assert device.segments(start) == [(ISN + 2, 0x14, b"")]
    assert not device.net._flows


def test_a_reset_on_a_dropped_port_is_lost_and_an_orphaned_flow_s_is_abandoned() -> None:
    held = Device().established()
    held.net.block(1883, "drop")
    start = len(held.sent)
    held.echo.close(reset=True)
    assert held.sent[start:] == [] and not held.net._flows
    assert held.lines[-2:] == [
        "net: TCP 10.64.0.2:4000 → 203.0.113.153:1883 RST dropped: port 1883 is blocked",
        "net: TCP 10.64.0.2:4000 → 203.0.113.153:1883 reset by its service",
    ]
    orphaned = Device().established()
    orphaned.net.forget_device()
    start = len(orphaned.sent)
    orphaned.echo.close(reset=True)
    assert orphaned.sent[start:] == [] and not orphaned.net._flows
    assert orphaned.lines[-1] == (
        "net: TCP 10.64.0.2:4000 → 203.0.113.153:1883 closed by its service with no device to"
        " answer"
    )


def test_a_reset_in_answer_to_data_with_a_fin_is_the_last_segment() -> None:
    device = Device().established()

    def reset(data: bytes) -> None:
        device.echo.close(reset=True)

    device.echo.on_data = reset  # type: ignore[method-assign]
    start = len(device.sent)
    device.net.receive(tcp(1, ISN + 1, 0x19, b"GET"))  # FIN|PSH|ACK
    # The ACK of the data, then the RST, which acknowledges the data but not the FIN.
    assert device.segments(start) == [(ISN + 1, 0x10, b""), (ISN + 1, 0x14, b"")]
    assert int.from_bytes(device.sent[-1][28:32], "big") == 4
    assert not device.net._flows


def test_a_reset_once_both_fins_have_crossed_sends_nothing_and_ends_closed() -> None:
    device = Device().established()
    device.echo.close()  # our FIN goes
    device.ack(ISN + 2)  # and the device acknowledges it

    def on_close() -> None:
        device.echo.closed += 1
        device.echo.close(reset=True)  # in answer to the device's FIN

    device.echo.on_close = on_close  # type: ignore[method-assign]
    start = len(device.sent)
    device.net.receive(tcp(1, ISN + 2, 0x11))  # the device's FIN|ACK
    # The ACK of its FIN and no RST: ABORT only deletes the connection now (RFC 9293, 3.10.5).
    assert device.segments(start) == [(ISN + 2, 0x10, b"")]
    assert not device.net._flows and device.echo.closed == 1
    ends = [line for line in device.lines if "closed" in line or "reset" in line]
    assert ends == ["net: TCP 10.64.0.2:4000 → 203.0.113.153:1883 closed"]


def test_a_reset_from_on_close_at_the_device_s_fin_resets_and_ends_once() -> None:
    device = Device().established()

    def on_close() -> None:
        device.echo.closed += 1
        device.echo.close(reset=True)

    device.echo.on_close = on_close  # type: ignore[method-assign]
    start = len(device.sent)
    device.net.receive(tcp(1, ISN + 1, 0x11))  # the device's FIN, before ours
    # CLOSE-WAIT: the ACK of its FIN, then the RST, and no FIN of ours.
    assert device.segments(start) == [(ISN + 1, 0x10, b""), (ISN + 1, 0x14, b"")]
    assert not device.net._flows and device.echo.closed == 1
    assert device.lines[-1] == "net: TCP 10.64.0.2:4000 → 203.0.113.153:1883 reset by its service"
    assert not [line for line in device.lines if line.endswith(" closed")]
