"""The path between the link and the network: exact stamps from hand-made frames, on PPP and on
Ethernet, with the clock moved by hand."""

import struct

import pytest
from test_net_ip import check, packet
from test_net_mqtt import CONNECT, PUBACK, PUBLISH
from test_net_mqtt_failures import Peer, Wire
from test_net_ppp import control, frame, unframe

from esp32tl.link.boardlink import Output
from esp32tl.net import LATENCY_NS, Internet
from esp32tl.net.ethernet import GATEWAY_MAC
from esp32tl.units import Duration, kbit_s, mbit_s, ms

MS = 1_000_000
NTP = b"\x23" + b"\0" * 47
UDP = struct.pack(">HHHH", 5000, 123, 8 + len(NTP), 0) + NTP
REQUEST = frame(0x21, packet(17, UDP))  # An IPv4 datagram of 76 bytes.
ANSWER_SIZE = 76  # The answer, from NTP, is as long: 20 + 8 + 48.


def opened() -> Internet:
    return Wire().net


def answered(replies: list[Output]) -> list[int]:
    """The stamps of NTP answers, checked to be whole IPv4 datagrams of the expected size."""
    for _, raw in replies:
        assert len(unframe(raw)) == ANSWER_SIZE
    return [t for t, _ in replies]


def test_by_default_the_answer_is_stamped_1_ms_after_the_request_in_its_turn() -> None:
    net = opened()
    assert net.latency() == LATENCY_NS == MS
    assert answered(net.receive(5 * MS, REQUEST)) == [6 * MS]
    assert net.ntp.requests == 1
    assert net.quiet_until() is None


def test_a_stated_rtt_is_exact_from_the_request_to_the_answers_stamp() -> None:
    net = opened()
    net.set_path(rtt=ms(200))
    t = 5 * MS
    assert net.receive(t, REQUEST) == []
    assert net.ntp.requests == 0
    # Half of what is above the floor goes up, and the rest down: 99.5 ms and 100.5 ms.
    up = t + 99_500_000
    assert net.quiet_until() == up
    assert net.poll(up - 1) == []
    assert net.ntp.requests == 0
    assert net.quiet_until() == up
    assert net.poll(up) == []
    assert net.ntp.requests == 1
    assert net.quiet_until() == t + 200 * MS
    # Handed over within the latency before its stamp, like every answer; stamped the same.
    assert net.poll(t + 199 * MS - 1) == []
    assert net.quiet_until() == t + 200 * MS
    assert answered(net.poll(t + 200 * MS)) == [t + 200 * MS]
    assert net.quiet_until() is None


@pytest.mark.parametrize(
    ("rtt", "up", "down"),
    [
        (Duration(1_000_000), 0, 1_000_000),
        (Duration(1_000_001), 0, 1_000_001),
        (Duration(1_000_002), 1, 1_000_001),
        (Duration(3_000_001), 1_000_000, 2_000_001),
        (ms(600), 299_500_000, 300_500_000),
    ],
)
def test_the_two_directions_add_up_to_the_rtt(rtt: Duration, up: int, down: int) -> None:
    net = opened()
    net.set_path(rtt=rtt)
    t = 10 * MS
    replies = net.receive(t, REQUEST)
    if up:
        assert replies == [] and net.quiet_until() == t + up
        replies = net.poll(t + up)
    assert net.ntp.requests == 1
    if replies == []:
        assert net.quiet_until() == t + up + down
        replies = net.poll(t + up + down)
    assert answered(replies) == [t + rtt.ns]


def test_arrive_takes_what_has_come_up_and_leaves_the_answer_to_poll() -> None:
    net = opened()
    net.set_path(rtt=ms(3))  # 1 ms up, 2 ms down
    net.receive(0, REQUEST)
    net.arrive(MS - 1)
    assert net.ntp.requests == 0
    net.arrive(MS)
    assert net.ntp.requests == 1
    assert net.quiet_until() == 3 * MS
    assert answered(net.poll(MS)) == []
    assert answered(net.poll(3 * MS)) == [3 * MS]


def test_a_service_is_stamped_at_the_datagrams_time_when_its_turn_comes_later() -> None:
    net = opened()
    net.set_path(rtt=ms(1.5))
    t = 10 * MS
    net.receive(t, REQUEST)
    # The runner may take its turn up to the 1 ms latency after the request, past the 0.25 ms up.
    assert net.quiet_until() == t + 250_000
    replies = net.poll(t + MS)
    assert answered(replies) == [t + 1_500_000]
    assert net.ntp.requests == 1


def test_mqtt_receipt_and_puback_each_have_their_directions_time() -> None:
    wire = Wire()
    net, broker = wire.net, wire.net.broker
    peer = Peer(wire, 4000)
    peer.send(CONNECT)
    peer.read()
    net.set_path(rtt=ms(200))
    t = 5 * MS
    wire.now = t
    # Device side, by hand: no answer may come before its time.
    assert peer.segment(0x18, PUBLISH) == []
    assert broker.messages == []
    up = t + 99_500_000
    wire.now = up
    assert net.poll(up) == []
    [message] = broker.messages
    assert message.at == Duration(up)
    assert message.publication is not None
    assert message.publication.puback_at == Duration(up)
    assert net.quiet_until() == t + 200 * MS
    replies = net.poll(t + 200 * MS)
    # The TCP acknowledgement and the PUBACK, both exactly one RTT after the request.
    assert [stamp for stamp, _ in replies] == [t + 200 * MS, t + 200 * MS]
    wire.now = t + 200 * MS
    wire.pump(replies)
    assert peer.read() == PUBACK


def test_a_test_publication_takes_the_way_down_only() -> None:
    wire = Wire()
    net, broker = wire.net, wire.net.broker
    peer = Peer(wire, 4000)
    peer.send(CONNECT + b"\x82\x08\0\x01\0\x03a/#\x00")
    peer.read()
    net.set_path(rtt=ms(200))
    wire.now = 7 * MS
    broker.publish("a/b", "x")
    assert broker.deliveries[0].at == Duration(7 * MS)
    assert net.poll(7 * MS) == []
    assert net.quiet_until() == 7 * MS + 100_500_000
    replies = net.poll(7 * MS + 100_500_000)
    assert [t for t, _ in replies] == [7 * MS + 100_500_000]


@pytest.mark.parametrize(
    ("rtt", "shown"),
    [(ms(0.5), "0.5"), (Duration(999_999), "0.999999"), (Duration(0), "0"), (ms(-1), "-1")],
)
def test_an_rtt_below_the_floor_is_refused(rtt: Duration, shown: str) -> None:
    with pytest.raises(ValueError) as refused:
        opened().set_path(rtt=rtt)
    assert str(refused.value) == (
        f"network.set_path: rtt is {shown} ms; the network answers 1 ms after a request at the"
        " least"
    )


def test_the_floor_itself_is_allowed_and_a_refusal_changes_nothing() -> None:
    net = opened()
    net.set_path(rtt=ms(1))
    assert answered(net.receive(0, REQUEST)) == [MS]
    net.set_path(rtt=ms(10))
    with pytest.raises(ValueError):
        net.set_path(rtt=ms(0.5), uplink=kbit_s(1))
    assert net.receive(MS, REQUEST) == []
    assert net.quiet_until() == MS + 4_500_000


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"rtt": 200}, "network.set_path: rtt is 200, not a Duration, as ms(200)"),
        ({"downlink": 512}, "network.set_path: downlink is 512, not a Rate, as kbit_s(512)"),
        ({"uplink": 128.0}, "network.set_path: uplink is 128.0, not a Rate, as kbit_s(512)"),
    ],
)
def test_what_is_not_a_duration_or_a_rate_is_refused(
    arguments: dict[str, object], message: str
) -> None:
    with pytest.raises(TypeError) as refused:
        opened().set_path(**arguments)  # type: ignore[arg-type]
    assert str(refused.value) == message


def test_the_rate_takes_its_time_each_way_from_the_size_of_the_datagram() -> None:
    net = opened()
    net.set_path(rtt=ms(1), downlink=kbit_s(512), uplink=kbit_s(128))
    t = 3 * MS
    assert net.receive(t, REQUEST) == []
    # 76 bytes at 128 kbit/s are 4.75 ms; back at 512 kbit/s, 1.1875 ms, and the 1 ms of the RTT.
    up = t + 4_750_000
    assert net.quiet_until() == up
    assert net.poll(up) == []
    assert net.ntp.requests == 1
    down = up + 1_187_500 + MS
    assert net.quiet_until() == down
    assert answered(net.poll(down)) == [down]


def test_a_rate_is_on_top_of_the_rtt_in_each_direction() -> None:
    net = opened()
    net.set_path(rtt=ms(201), downlink=mbit_s(1), uplink=mbit_s(2))
    net.receive(0, REQUEST)
    # 76 bytes: 304 us up at 2 Mbit/s, 608 us down at 1 Mbit/s, and 100 ms and 101 ms.
    assert net.quiet_until() == 304_000 + 100 * MS
    net.poll(304_000 + 100 * MS)
    assert net.quiet_until() == 304_000 + 201 * MS + 608_000


def test_a_rate_rounds_up_to_a_whole_nanosecond() -> None:
    net = opened()
    net.set_path(uplink=kbit_s(3))
    net.receive(0, REQUEST)
    assert net.quiet_until() == 202_666_667  # 76 * 8 / 3000 s is 202666666.67 ns.


def test_datagrams_entering_together_go_one_after_another() -> None:
    net = opened()
    net.set_path(uplink=kbit_s(128))
    net.receive(0, REQUEST + REQUEST)
    assert net.quiet_until() == 4_750_000
    # The first is answered; the second, serialised behind it, comes 4.75 ms later.
    assert answered(net.poll(4_750_000)) == [4_750_000 + MS]
    assert net.ntp.requests == 1
    assert net.quiet_until() == 9_500_000
    assert answered(net.poll(9_500_000)) == [9_500_000 + MS]
    assert net.ntp.requests == 2


def test_a_datagram_entering_after_the_line_is_free_does_not_wait() -> None:
    net = opened()
    net.set_path(uplink=kbit_s(128))
    net.receive(0, REQUEST)
    net.poll(4_750_000)
    net.receive(20 * MS, REQUEST)
    assert net.quiet_until() == 20 * MS + 4_750_000


def test_answers_entering_together_go_one_after_another() -> None:
    net = opened()
    net.set_path(downlink=kbit_s(512))
    assert net.receive(0, REQUEST + REQUEST) == []
    assert net.ntp.requests == 2
    assert net.quiet_until() == 1_187_500 + MS
    assert answered(net.poll(1_187_500 + MS)) == [1_187_500 + MS]
    assert net.quiet_until() == 2 * 1_187_500 + MS
    assert answered(net.poll(2 * 1_187_500 + MS)) == [2 * 1_187_500 + MS]


def test_a_smaller_rtt_applies_to_what_enters_after_it_and_overtakes_nothing_going_up() -> None:
    net = opened()
    net.set_path(rtt=ms(201))
    net.receive(0, REQUEST)
    assert net.quiet_until() == 100 * MS
    net.set_path(rtt=ms(1))
    # This one would be at the network at once. It waits for the one ahead of it.
    assert net.receive(10 * MS, REQUEST) == []
    assert net.ntp.requests == 0
    assert net.quiet_until() == 100 * MS
    assert answered(net.poll(100 * MS)) == [101 * MS, 101 * MS]
    assert net.ntp.requests == 2


def test_the_network_takes_a_datagram_no_earlier_than_the_one_ahead_of_it() -> None:
    net = opened()
    taken: list[int] = []

    class Stamped:
        def on_datagram(self, data: bytes) -> bytes | None:
            taken.append(net.now())
            return None

    net.ip.udp[123] = Stamped()
    net.set_path(rtt=ms(201))
    net.receive(0, REQUEST)
    net.set_path(rtt=ms(1))
    net.receive(10 * MS, REQUEST)
    net.poll(100 * MS)
    assert taken == [100 * MS, 100 * MS]


def test_a_smaller_rtt_overtakes_nothing_going_down() -> None:
    net = opened()
    net.set_path(rtt=ms(201))  # 100 ms up, 101 ms down
    net.receive(0, REQUEST)
    net.poll(100 * MS)
    assert net.quiet_until() == 201 * MS
    net.set_path(rtt=ms(1))
    # Its answer would leave at 151 ms; the one ahead of it leaves at 201 ms.
    assert net.receive(150 * MS, REQUEST) == []
    assert net.ntp.requests == 2
    assert net.quiet_until() == 201 * MS
    assert answered(net.poll(201 * MS)) == [201 * MS, 201 * MS]


def test_a_larger_rtt_applies_to_what_enters_after_it() -> None:
    net = opened()
    assert answered(net.receive(0, REQUEST)) == [MS]
    net.set_path(rtt=ms(201))
    assert net.receive(MS, REQUEST) == []
    assert net.quiet_until() == 101 * MS


def test_ppp_control_is_not_delayed() -> None:
    net = opened()
    net.set_path(rtt=ms(600), downlink=kbit_s(1), uplink=kbit_s(1))
    [(t, raw)] = net.receive(5 * MS, control(0xC021, 9, 3, b"\0\0\0\0"))  # LCP Echo-Request
    assert t == 5 * MS + LATENCY_NS
    assert unframe(raw)[0] == 10  # Echo-Reply
    assert net.quiet_until() is None


def test_the_answer_to_a_datagram_is_held_while_ppp_control_passes_it() -> None:
    net = opened()
    net.set_path(rtt=ms(601))  # 300 ms up, 301 ms down
    net.receive(0, REQUEST)
    net.poll(300 * MS)
    [(t, raw)] = net.receive(400 * MS, control(0xC021, 9, 3, b"\0\0\0\0"))
    assert t == 401 * MS
    assert unframe(raw)[0] == 10
    assert net.quiet_until() == 601 * MS
    assert answered(net.poll(601 * MS)) == [601 * MS]


# The access point's link, in hand-made frames: ARP and DHCP pass, IPv4 takes the path. The
# station's frames end in libpp's guard word, as esp32sim hands them over (protocol 5.15).
DEVICE_MAC = bytes.fromhex("246f28001122")
GUARD = b"\xef\xbe\xad\xde"


def ethernet_opened() -> Internet:
    net = Internet(lambda: 0, link="ethernet")
    net.start(0)
    return net


def eth_ip(datagram: bytes) -> bytes:
    return GATEWAY_MAC + DEVICE_MAC + b"\x08\x00" + datagram + GUARD


def test_ethernet_datagrams_take_the_path() -> None:
    net = ethernet_opened()
    net.set_path(rtt=ms(200))
    assert net.receive(0, eth_ip(packet(17, UDP))) == []
    assert net.quiet_until() == 99_500_000
    assert net.poll(99_500_000) == []
    assert net.ntp.requests == 1
    [(t, raw)] = net.poll(200 * MS)
    assert t == 200 * MS
    assert raw[:6] == DEVICE_MAC and raw[12:14] == b"\x08\x00"


def test_what_follows_a_datagram_in_its_frame_takes_no_time_at_the_rate() -> None:
    net = ethernet_opened()
    net.set_path(uplink=kbit_s(128))
    assert net.receive(0, eth_ip(packet(17, UDP))) == []
    assert net.quiet_until() == 4_750_000  # The 76 bytes of the datagram, not 80.


def test_arp_and_dhcp_are_not_delayed() -> None:
    net = ethernet_opened()
    net.set_path(rtt=ms(600), downlink=kbit_s(1), uplink=kbit_s(1))
    arp = (
        b"\xff" * 6
        + DEVICE_MAC
        + b"\x08\x06"
        + struct.pack(">HHBBH", 1, 0x0800, 6, 4, 1)
        + DEVICE_MAC
        + bytes([10, 64, 0, 2])
        + b"\0" * 6
        + bytes([10, 64, 0, 1])
        + GUARD
    )
    [(t, reply)] = net.receive(5 * MS, arp)
    assert t == 6 * MS and reply[12:14] == b"\x08\x06"
    dhcp = bytearray(240)
    dhcp[0:3] = bytes([1, 1, 6])
    dhcp[28:34] = DEVICE_MAC
    dhcp[236:240] = b"\x63\x82\x53\x63"
    body = bytes(dhcp) + bytes([53, 1, 1, 255])
    udp = struct.pack(">HHHH", 68, 67, 8 + len(body), 0) + body
    [(t, reply)] = net.receive(10 * MS, eth_ip(udp_datagram(udp)))
    assert t == 11 * MS
    assert net.dhcp.leases == [] and net.quiet_until() is None


def udp_datagram(udp: bytes) -> bytes:
    """A UDP datagram from the unconfigured station to the broadcast address."""
    header = struct.pack(
        ">BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 1, 0, 64, 17, 0, b"\0" * 4, b"\xff" * 4
    )
    return header[:10] + struct.pack(">H", check(header)) + header[12:] + udp


def test_a_reset_drops_what_is_on_the_path_and_keeps_the_path() -> None:
    wire = Wire()
    net = wire.net
    net.set_path(rtt=ms(201))
    net.receive(0, REQUEST)
    net.receive(0, REQUEST)
    net.poll(100 * MS)
    net.receive(150 * MS, REQUEST)
    assert net.ntp.requests == 2 and net.quiet_until() is not None
    net.stop(160 * MS)
    assert net.quiet_until() is None
    # Nothing that was on its way comes back after the network starts again.
    net.start(200 * MS)
    assert net.quiet_until() is None
    assert net.ntp.requests == 2
    # The path outlives the reset: what enters next takes the same 100 ms up.
    assert_path_kept(net, 300 * MS)


def assert_path_kept(net: Internet, t: int) -> None:
    net.receive(t, control(0xC021, 1, 7))
    net.receive(t, control(0xC021, 2, 1))
    net.receive(t, control(0x8021, 2, 2, b"\x03\x06\x0a\x40\0\x01"))
    net.receive(t, control(0x8021, 1, 8, b"\x03\x06\x0a\x40\0\x02"))
    assert net.ppp.ipcp_open
    assert net.receive(t, REQUEST) == []
    assert net.quiet_until() == t + 100 * MS


def test_the_end_of_the_ppp_link_drops_what_is_on_the_path() -> None:
    net = opened()
    net.set_path(rtt=ms(201))
    net.receive(0, REQUEST)
    net.receive(5 * MS, control(0xC021, 5, 9))  # LCP Terminate-Request
    assert not net.ppp.lcp_open
    assert net.quiet_until() is None
    assert net.poll(100 * MS) == []
    assert net.ntp.requests == 0


def test_a_stopped_network_asks_for_no_turn() -> None:
    net = opened()
    net.set_path(rtt=ms(200))
    net.receive(0, REQUEST)
    net.active = False
    assert net.quiet_until() is None


def test_setting_the_path_is_logged_with_its_values() -> None:
    net = opened()
    net.set_path(rtt=ms(200), downlink=kbit_s(512))
    assert net.events[-1]["event"] == "net: path: rtt 0.2 s, downlink 512 kbit/s, uplink any"


def _run() -> list[Output]:
    net = opened()
    net.set_path(rtt=ms(137), downlink=kbit_s(77), uplink=kbit_s(33))
    out = list(net.receive(0, REQUEST + REQUEST))
    while (t := net.quiet_until()) is not None:
        out += net.poll(t)
    return out


def test_two_runs_give_the_same_stamps() -> None:
    assert _run() == _run()
    assert [t for t, _ in _run()] == sorted(t for t, _ in _run())


def test_the_network_never_stamps_an_answer_before_the_turn_that_hands_it_over() -> None:
    # What BoardLink refuses, and what the runner would end the run for (protocol, rule 2).
    net = opened()
    net.set_path(rtt=ms(1.25), downlink=kbit_s(900), uplink=kbit_s(700))
    turn = 0
    sent = [(0, REQUEST), (400_000, REQUEST), (900_000, REQUEST)]
    for at, data in sent:
        for t, _ in net.receive(at, data):
            assert t >= at + LATENCY_NS
    while (bound := net.quiet_until()) is not None:
        assert bound > turn
        turn = bound
        for t, _ in net.poll(turn):
            assert t >= turn
