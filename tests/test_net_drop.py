"""A path that drops every datagram, and a TCP port blocked by dropping or refusing, from
hand-made PPP frames, with the clock moved by hand."""

import struct
from collections.abc import Callable
from typing import Literal

import pytest
from test_net_ip import DST, SRC, check, packet
from test_net_mqtt import CONNECT, SUBSCRIBE
from test_net_mqtt_failures import Peer, Wire
from test_net_path import REQUEST, answered
from test_net_ppp import control, frame, unframe
from test_net_station import HOST_IP, SoftAP, device_tcp, station_service

from esp32tl.link.boardlink import Output
from esp32tl.net import LATENCY_NS, Internet
from esp32tl.net.client import NotReachable
from esp32tl.net.ip import ACK, ISN
from esp32tl.units import kbit_s, ms

MS = 1_000_000
PINGREQ, PINGRESP = b"\xc0\x00", b"\xd0\x00"
QUERY = (
    struct.pack(">HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0)
    + b"\x06broker\x04test\x00"
    + struct.pack(">HH", 1, 1)
)
DNS = frame(0x21, packet(17, struct.pack(">HHHH", 5001, 53, 8 + len(QUERY), 0) + QUERY))
ECHO = control(0xC021, 9, 3, b"\0\0\0\0")  # LCP Echo-Request


def lines(net: Internet) -> list[str]:
    return [str(e["event"]) for e in net.events if "event" in e]


def syn(wire: Wire, port: int) -> list[Output]:
    """A SYN from the device's `port` to the broker's, with no options, and what answers it."""
    head = struct.pack(">HHIIBBHHH", port, 1883, 0, 0, 0x50, 0x02, 8192, 0, 0)
    pseudo = SRC + DST + struct.pack(">BBH", 0, 6, len(head))
    segment = head[:16] + struct.pack(">H", check(pseudo + head)) + head[18:]
    return wire.net.receive(wire.now, frame(0x21, packet(6, segment)))


def subscribed() -> tuple[Wire, Peer]:
    """A client on the broker, subscribed to `a/#`, its answers read."""
    wire = Wire()
    peer = Peer(wire, 4000)
    peer.send(CONNECT + SUBSCRIBE)
    peer.read()
    return wire, peer


# The path drops every datagram.


def test_a_black_hole_drops_every_datagram_up_and_ppp_control_still_answers() -> None:
    net = Wire().net
    net.set_path(drop=True)
    assert net.receive(5 * MS, REQUEST) == []
    assert net.ntp.requests == 0
    assert net.quiet_until() is None
    [(t, raw)] = net.receive(6 * MS, ECHO)
    assert t == 6 * MS + LATENCY_NS
    assert unframe(raw)[0] == 10  # Echo-Reply
    assert net.ppp.lcp_open and net.ppp.ipcp_open
    assert lines(net)[-2:] == [
        "net: path: drop every datagram",
        "net: path dropped UDP 10.64.0.2:5000 → 203.0.113.153:123, 76 bytes going up",
    ]


def test_an_answer_made_in_a_black_hole_is_dropped_going_down() -> None:
    net = Wire().net
    net.set_path(rtt=ms(201))  # 100 ms up, 101 ms down
    net.receive(0, REQUEST)
    net.set_path(drop=True)
    # What was on the path still arrives; its answer enters the path in the black hole.
    assert net.poll(100 * MS) == []
    assert net.ntp.requests == 1
    assert net.quiet_until() is None
    assert lines(net)[-1] == (
        "net: path dropped UDP 203.0.113.153:123 → 10.64.0.2:5000, 76 bytes going down"
    )


def test_after_the_restore_the_next_datagram_is_answered_at_the_paths_rtt() -> None:
    net = Wire().net
    net.set_path(drop=True)
    for at in range(0, 50 * MS, 5 * MS):
        assert net.receive(at, REQUEST) == []
    net.set_path(rtt=ms(200), uplink=kbit_s(128))
    # Nothing dropped waits ahead of it: 4.75 ms for its 76 bytes at 128 kbit/s, and the RTT.
    t = 60 * MS
    assert net.receive(t, REQUEST) == []
    up = t + 4_750_000 + 99_500_000
    assert net.quiet_until() == up
    assert net.poll(up) == []
    assert net.ntp.requests == 1
    assert answered(net.poll(t + 4_750_000 + 200 * MS)) == [t + 4_750_000 + 200 * MS]
    assert net.quiet_until() is None


def test_set_path_with_no_arguments_lifts_the_black_hole() -> None:
    net = Wire().net
    net.set_path(drop=True)
    net.set_path()
    assert lines(net)[-1] == "net: path: rtt 0.001 s, downlink any, uplink any"
    assert answered(net.receive(0, REQUEST)) == [MS]


def test_the_network_s_tcp_holds_what_it_has_to_send_until_the_restore() -> None:
    wire, peer = subscribed()
    net = wire.net
    net.set_path(drop=True)
    wire.now = 10 * MS
    net.broker.publish("a/b", "x")
    wire.poll()
    assert peer.read() == b""
    wire.now = 20 * MS
    net.set_path(rtt=ms(3))  # 1 ms up, 2 ms down
    replies = net.poll(wire.now)
    assert replies == []
    assert net.quiet_until() == 22 * MS
    wire.now = 22 * MS
    wire.poll()
    assert peer.read() == b"\x30\x06\x00\x03a/bx"


def test_the_broker_s_keepalive_runs_out_in_a_black_hole_and_ppp_stays_open() -> None:
    wire, peer = subscribed()
    net = wire.net
    net.set_path(drop=True)
    wire.now = 5 * MS
    assert peer.segment(0x18, PINGREQ) == []
    # Keepalive 10 s: the broker closes 15 s after the last packet that reached it, at 0.
    wire.now = 15_000 * MS
    wire.poll()
    assert "mqtt: c keepalive expired" in lines(net)
    assert peer.read() == b"" and not peer.closed
    [(_, raw)] = net.receive(wire.now, ECHO)
    assert unframe(raw)[0] == 10
    assert net.ppp.lcp_open
    # Its FIN waited for the path, and goes once the path takes datagrams again.
    net.set_path()
    wire.poll()
    assert peer.closed


def test_the_test_cannot_connect_through_a_black_hole() -> None:
    net = Wire().net
    net.set_path(drop=True)
    with pytest.raises(NotReachable) as refused:
        net.connect(80)
    assert str(refused.value) == (
        "network.connect(80): the path drops every datagram (network.set_path(drop=True)):"
        " the SYN would reach no device"
    )


def test_a_black_hole_takes_no_rtt_or_rate() -> None:
    net = Wire().net
    with pytest.raises(ValueError) as refused:
        net.set_path(rtt=ms(200), drop=True)
    assert str(refused.value) == (
        "network.set_path: drop=True drops every datagram: it takes no rtt, downlink or uplink"
    )
    with pytest.raises(ValueError):
        net.set_path(uplink=kbit_s(8), drop=True)
    with pytest.raises(TypeError) as wrong:
        net.set_path(drop=1)  # type: ignore[arg-type]
    assert str(wrong.value) == "network.set_path: drop is 1, not True or False"
    assert answered(net.receive(0, REQUEST)) == [MS]


def test_a_syn_whose_syn_ack_the_black_hole_dropped_is_answered_again_after_the_restore() -> None:
    wire = Wire()
    net = wire.net
    net.set_path(rtt=ms(201))  # 100 ms up, 101 ms down
    assert syn(wire, 4001) == []
    net.set_path(drop=True)
    # The SYN was on its way up: the broker accepts it, and its SYN-ACK goes into the black hole.
    assert net.poll(100 * MS) == []
    net.set_path(rtt=ms(201))
    wire.now = 300 * MS
    assert syn(wire, 4001) == []  # lwIP's SYN again, from the same port
    assert net.poll(400 * MS) == []
    [(t, raw)] = net.poll(501 * MS)
    ip = unframe(raw)
    assert t == 501 * MS and ip[33] == 0x12  # SYN|ACK
    assert int.from_bytes(ip[24:28], "big") == 0x10000  # its ISN again
    assert int.from_bytes(ip[28:32], "big") == 1
    events = lines(net)
    assert events.count("net: TCP 10.64.0.2:4001 → 203.0.113.153:1883 accepted") == 1
    assert "net: TCP 10.64.0.2:4001 → 203.0.113.153:1883 SYN again, answered again" in events


@pytest.mark.parametrize("how", ["path", "port"])
def test_a_full_window_moves_on_once_the_device_answers_the_probe(how: str) -> None:
    wire, peer = subscribed()
    net = wire.net
    [flow] = [f for f in net.ip._flows.values() if f.key[1] == 4000]
    flow.peer_window = 37  # The device's window: one PUBLISH of 37 bytes.
    net.broker.publish("a/b", "x" * 30)
    out = net.poll(wire.now)
    if how == "path":
        net.set_path(drop=True)
    else:
        net.block(1883, how="drop")
    wire.pump(out)  # The device takes the PUBLISH; its ACK, with its window, is dropped.
    net.broker.publish("a/b", "y" * 30)
    wire.now = 10 * MS
    wire.poll()
    if how == "path":
        net.set_path()
    else:
        net.unblock(1883)
    # The window is full as far as the network knows: it asks the device for its ACK again.
    [(_, raw)] = net.poll(wire.now)
    ip = unframe(raw)
    assert len(ip) == 40 and ip[33] == 0x10  # an ACK with no data
    assert int.from_bytes(ip[24:28], "big") == flow.snd_nxt - 1
    assert b"y" not in peer.received
    wire.pump(peer.segment(0x10))  # The device's answer: its ACK and its window.
    assert bytes(peer.received).endswith(b"a/b" + b"y" * 30)


@pytest.mark.parametrize("before", [None, "refuse"])
def test_a_block_or_unblock_with_nothing_dropped_sends_no_probe(
    before: Literal["refuse"] | None,
) -> None:
    wire, _ = subscribed()
    net = wire.net
    if before is not None:
        net.block(1883, how=before)
    net.broker.publish("a/b", "x")
    # The PUBLISH, on its way and not acknowledged yet: nothing the device sent was swallowed.
    if before is None:
        net.block(1883, how="refuse")
    else:
        net.unblock(1883)
    [(_, raw)] = net.poll(wire.now)
    assert unframe(raw)[20 + 13] == 0x18  # the PUBLISH alone, PSH|ACK


def test_a_probe_due_while_the_station_is_away_goes_when_it_has_a_lease_again() -> None:
    ap = SoftAP().up()
    net = ap.net
    writes, _ = station_service(ap)
    net.poll(ap.now)
    ap.arp(HOST_IP, op=2)
    [flow] = net.ip._flows.values()
    flow.peer_window = 0  # The device's window is shut; its update will go into the black hole.
    net.set_path(drop=True)
    [write] = writes
    write(b"x")
    net._station_link(ap.now, False)
    net.set_path()  # No lease: the probe is owed.
    assert net.poll(ap.now) == []
    net._station_link(ap.now, True)
    [(_, discover)] = net.poll(ap.now)
    [(_, request)] = ap.reply(discover)
    ap.reply(request)
    net.poll(ap.now)
    [(_, probe)] = ap.arp(HOST_IP, op=2)
    segment = probe[14 + 20 :]
    assert len(segment) == 20 and segment[13] == 0x10  # an ACK with no data
    assert int.from_bytes(segment[4:8], "big") == ISN
    device_tcp(ap, 101, ISN + 1, ACK)  # its answer, with its window open
    # What waited went in answer to it.
    assert not flow.pending and flow.snd_nxt == ISN + 2


def test_a_reset_keeps_the_black_hole() -> None:
    wire = Wire()
    net = wire.net
    net.set_path(drop=True)
    net.stop(10 * MS)
    net.start(20 * MS)
    wire.now = 20 * MS
    wire.open()
    assert net.receive(wire.now, REQUEST) == []
    assert net.ntp.requests == 0


# A blocked TCP port.


def test_a_port_blocked_with_drop_swallows_the_device_s_segments_and_dns_and_ntp_answer() -> None:
    wire, peer = subscribed()
    net = wire.net
    net.block(1883, how="drop")
    wire.now = 5 * MS
    assert peer.segment(0x18, PINGREQ) == []
    assert peer.read() == b""
    assert answered(net.receive(wire.now, REQUEST)) == [6 * MS]
    [(t, raw)] = net.receive(wire.now, DNS)
    assert t == 6 * MS and unframe(raw)[9] == 17
    assert net.dns.asked[-1] == "broker.test"
    events = lines(net)
    assert "net: block: TCP 1883, drop" in events
    assert "net: TCP 10.64.0.2:4000 → 203.0.113.153:1883 dropped: port 1883 is blocked" in events


def test_a_new_connection_to_a_dropped_port_gets_no_answer() -> None:
    wire = Wire()
    net = wire.net
    net.block(1883, how="drop")
    assert syn(wire, 4001) == []
    assert net.broker.connection_attempts == []


def test_a_dropped_port_holds_its_service_s_writes_until_unblock() -> None:
    wire, peer = subscribed()
    net = wire.net
    net.block(1883, how="drop")
    wire.now = 10 * MS
    net.broker.publish("a/b", "x")
    wire.poll()
    assert peer.read() == b""
    wire.now = 20 * MS
    net.unblock(1883)
    wire.poll()
    assert peer.read() == b"\x30\x06\x00\x03a/bx"
    # And the device's segments reach the service again.
    peer.send(PINGREQ)
    assert peer.read() == PINGRESP
    assert lines(net)[-6:].count("net: unblock: TCP 1883") == 1


def test_a_refused_port_answers_a_syn_with_rst_and_leaves_open_connections() -> None:
    wire, peer = subscribed()
    net = wire.net
    net.block(1883, how="refuse")
    [(_, raw)] = syn(wire, 4001)
    ip = unframe(raw)
    assert ip[9] == 6 and ip[33] == 0x14  # RST|ACK
    assert int.from_bytes(ip[28:32], "big") == 1  # acknowledging the SYN
    assert len(net.broker.connection_attempts) == 1
    assert "net: TCP 10.64.0.2:4001 → 203.0.113.153:1883 refused, port 1883 is blocked" in lines(
        net
    )
    # The connection open before the block goes on.
    peer.send(PINGREQ)
    assert peer.read() == PINGRESP
    net.unblock(1883)
    again = Peer(wire, 4002)
    again.send(CONNECT)
    assert again.read()[:1] == b"\x20"  # CONNACK


def test_blocking_a_blocked_port_changes_how() -> None:
    wire, peer = subscribed()
    net = wire.net
    net.block(1883, how="drop")
    net.broker.publish("a/b", "x")
    wire.poll()
    assert peer.read() == b""
    net.block(1883, how="refuse")
    wire.poll()
    assert peer.read() == b"\x30\x06\x00\x03a/bx"


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (
            lambda net: net.block(0, how="drop"),
            "network.block(0, how='drop'): the TCP port must be between 1 and 65535",
        ),
        (
            lambda net: net.block(True, how="drop"),
            "network.block(True, how='drop'): the TCP port must be between 1 and 65535",
        ),
        (
            lambda net: net.block(1883, how="reject"),
            "network.block(1883, how='reject'): how is 'drop' or 'refuse'",
        ),
        (
            lambda net: net.unblock(1883),
            "network.unblock(1883): TCP port 1883 is not blocked",
        ),
        (
            lambda net: net.unblock(True),
            "network.unblock(True): the TCP port must be between 1 and 65535",
        ),
        (
            lambda net: net.unblock([1883]),
            "network.unblock([1883]): the TCP port must be between 1 and 65535",
        ),
    ],
)
def test_a_wrong_block_is_refused_and_changes_nothing(
    call: Callable[[Internet], None], message: str
) -> None:
    net = Wire().net
    with pytest.raises(ValueError) as refused:
        call(net)
    assert str(refused.value) == message
    assert net.ip.blocked == {}


# The RTO of what the device's stack dropped, held by the path or a blocked port.

S = 1_000_000_000


@pytest.mark.parametrize("hold", ["black hole", "blocked port"])
def test_the_rto_stops_in_a_hold_and_starts_again_at_its_end(hold: str) -> None:
    wire, peer = subscribed()
    net = wire.net
    net.broker.publish("a/b", "x")
    net.poll(wire.now)  # the PUBLISH goes, and the device's stack drops it
    assert net.ip.quiet_until() == wire.now + S
    if hold == "black hole":
        net.set_path(drop=True)
    else:
        net.block(1883, how="drop")
    assert net.ip.quiet_until() is None
    wire.now = 10 * S
    wire.poll()
    assert peer.read() == b""
    if hold == "black hole":
        net.set_path()
    else:
        net.unblock(1883)
    wire.poll()  # the probe, which Peer does not answer
    assert net.ip.quiet_until() == 11 * S
    wire.now = 11 * S
    wire.poll()
    assert peer.read() == b"\x30\x06\x00\x03a/bx"
    again = [line for line in lines(net) if "sent again" in line]
    assert len(again) == 1 and again[0].endswith("the 1st time, after an RTO of 1 s")
