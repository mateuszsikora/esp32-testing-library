"""Control packets received by the MQTT broker, stamped at completion."""

import pytest
from test_net_mqtt_ends import DISCONNECT, MALFORMED, WILL_CONNECT

from esp32tl import Broker, ConnackCode, ReceivedPacket
from esp32tl.units import Duration, seconds

PING = b"\xc0\x00"
EMPTY_CONNECT = b"\x10\x0c\0\x04MQTT\x04\x02\0\x01\0\0"


def test_split_ping_and_partial_next_packet_at_keepalive_expiry() -> None:
    now = [0]
    broker = Broker(lambda: now[0])
    session = broker.accept(("peer", 100), lambda _: None, lambda: None)
    session.on_data(WILL_CONNECT)
    now[0] = 100
    session.on_data(PING[:1])
    assert len(broker.received_packets) == 1
    now[0] = 200
    session.on_data(PING[1:])
    last = ReceivedPacket(Duration(200), 1, "c", "PINGREQ")
    assert broker.received_packets == [ReceivedPacket(Duration(0), 1, "c", "CONNECT"), last]
    now[0] += 100
    session.on_data(PING[:1])
    now[0] = last.at.ns + 1_500_000_000
    broker.poll()
    (end,) = broker.connection_ends
    assert end.by == "keepalive" and end.at == last.at + seconds(1.5)
    assert broker.received_packets[-1] == last


def test_ping_received_during_connack_restarts_keepalive_from_its_arrival() -> None:
    now = [0]
    broker = Broker(lambda: now[0])

    def reply(packet: bytes) -> None:
        if packet == b"\x20\x02\x00\x00":  # accepted CONNACK
            now[0] = seconds(0.5).ns
            session.on_data(PING)
            now[0] = seconds(1).ns

    session = broker.accept(("peer", 100), reply, lambda: None)
    session.on_data(WILL_CONNECT)
    assert broker.received_packets == [
        ReceivedPacket(seconds(0), 1, "c", "CONNECT"),
        ReceivedPacket(seconds(0.5), 1, "c", "PINGREQ"),
    ]
    now[0] = seconds(1.5).ns
    broker.poll()
    assert not session.closed
    now[0] = seconds(2).ns
    broker.poll()
    (end,) = broker.connection_ends
    assert end.by == "keepalive" and end.at == seconds(2)


def test_disconnect_received_before_transport_close_suppresses_will() -> None:
    broker = Broker(lambda: 0)

    def reply(packet: bytes) -> None:
        if packet == b"\x20\x02\0\0":
            session.on_data(DISCONNECT)
            session.on_close()

    session = broker.accept(("peer", 100), reply, lambda: None)
    session.on_data(WILL_CONNECT)
    assert [p.kind for p in broker.received_packets] == ["CONNECT", "DISCONNECT"]
    (end,) = broker.connection_ends
    assert end.by == "disconnect" and not end.will_sent
    assert broker.messages == []


def test_poll_checks_keepalive_after_an_already_received_ping() -> None:
    now = [0]
    broker = Broker(lambda: now[0])
    first = broker.accept(("peer", 100), lambda _: None, lambda: None)
    first.on_data(WILL_CONNECT)

    def reply(packet: bytes) -> None:
        if packet == b"\x20\x02\0\0":
            now[0] = seconds(1.4).ns
            first.on_data(PING)
            now[0] = seconds(1.5).ns
            broker.poll()

    second = broker.accept(("peer", 101), reply, lambda: None)
    second.on_data(EMPTY_CONNECT)
    assert broker.received_packets[-1] == ReceivedPacket(seconds(1.4), 1, "c", "PINGREQ")
    assert not first.closed
    assert broker.quiet_until() == seconds(2.9).ns
    now[0] = seconds(2.9).ns
    broker.poll()
    end = broker.ended(1)
    assert end is not None and end.by == "keepalive" and end.at == seconds(2.9)


@pytest.mark.parametrize("bad", MALFORMED.values(), ids=MALFORMED.keys())
def test_malformed_packet_adds_no_record(bad: bytes) -> None:
    broker = Broker(lambda: 0)
    session = broker.accept(("peer", 100), lambda _: None, lambda: None)
    session.on_data(WILL_CONNECT + bad + PING)
    assert broker.received_packets == [ReceivedPacket(Duration(0), 1, "c", "CONNECT")]


def test_rejected_and_anonymous_connect_ids() -> None:
    broker = Broker(lambda: 0)
    broker.connection_policy = lambda _: ConnackCode.NOT_AUTHORIZED
    for connect in (WILL_CONNECT, EMPTY_CONNECT):
        broker.accept(("peer", 100), lambda _: None, lambda: None).on_data(connect)
    broker.connection_policy = None
    broker.accept(("peer", 100), lambda _: None, lambda: None).on_data(EMPTY_CONNECT)
    assert broker.received_packets == [
        ReceivedPacket(Duration(0), 1, "c", "CONNECT"),
        ReceivedPacket(Duration(0), 2, "", "CONNECT"),
        ReceivedPacket(Duration(0), 3, "esp32tl-anonymous-1", "CONNECT"),
    ]


def test_next_ping_filters_kind_and_client_across_connections() -> None:
    broker = Broker(lambda: 0)
    first = broker.accept(("peer", 100), lambda _: None, lambda: None)
    first.on_data(WILL_CONNECT + PING + DISCONNECT)
    assert str(broker.next_received_packet()) == "the next MQTT packet"
    ping = broker.next_received_packet("PINGREQ", client_id="c")
    assert str(ping) == "the next MQTT PINGREQ from 'c'"
    other = broker.accept(("peer", 101), lambda _: None, lambda: None)
    other.on_data(EMPTY_CONNECT + PING)
    second = broker.accept(("peer", 102), lambda _: None, lambda: None)
    second.on_data(WILL_CONNECT)
    assert ping() is None
    second.on_data(PING)
    assert ping() is broker.received_packets[-1]
    second.on_data(PING)
    assert ping() is broker.received_packets[-2]
    assert broker.received_packets[2] == ReceivedPacket(Duration(0), 1, "c", "DISCONNECT")
    assert ping() == ReceivedPacket(Duration(0), 3, "c", "PINGREQ")


def test_coalesced_packets_are_recorded_in_wire_order() -> None:
    broker = Broker(lambda: 0)
    session = broker.accept(("peer", 100), lambda _: None, lambda: None)
    session.on_data(
        WILL_CONNECT
        + b"\x82\x06\0\x01\0\x01a\x01"  # SUBSCRIBE, a at QoS 1
        + b"\x30\x04\0\x01ab"  # PUBLISH, b on a
        + b"\x40\x02\0\x01"  # PUBACK
        + b"\xa2\x05\0\x02\0\x01a"  # UNSUBSCRIBE from a
        + PING
        + DISCONNECT
    )
    assert [p.kind for p in broker.received_packets] == [
        "CONNECT",
        "SUBSCRIBE",
        "PUBLISH",
        "PUBACK",
        "UNSUBSCRIBE",
        "PINGREQ",
        "DISCONNECT",
    ]


def test_invalid_kind_names_supported_kinds() -> None:
    broker = Broker(lambda: 0)
    with pytest.raises(
        ValueError, match="CONNECT, PUBLISH, PUBACK, SUBSCRIBE, UNSUBSCRIBE, PINGREQ, DISCONNECT"
    ):
        broker.next_received_packet("CONNACK")  # type: ignore[arg-type]
