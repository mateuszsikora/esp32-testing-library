"""Sessions kept by client ID, in raw MQTT 3.1.1 packets written out by hand."""

import pytest
import test_net_mqtt_failures as failures

from esp32tl.net.mqtt import Broker, ConnackCode, Connect, Delivery, SessionState
from esp32tl.units import Duration

KEEP = b"\x10\x0d\0\x04MQTT\x04\x00\0\x0a\0\x01c"  # CleanSession=0, client ID c, keepalive 10
CLEAN = b"\x10\x0d\0\x04MQTT\x04\x02\0\x0a\0\x01c"  # CleanSession=1
SUBSCRIBE_QOS1 = b"\x82\x08\0\x01\0\x03a/#\x01"
SUBSCRIBE_QOS0 = b"\x82\x08\0\x01\0\x03a/#\x00"
SUBACK_QOS1 = b"\x90\x03\0\x01\x01"
NO_SESSION = b"\x20\x02\0\0"
SESSION_PRESENT = b"\x20\x02\x01\0"


class Wire:
    """One connection's raw bytes; `close` is the broker closing it."""

    def __init__(self, broker: Broker, *data: bytes) -> None:
        self.sent: list[bytes] = []
        self.closed = False
        self.session = broker.accept(("10.64.0.2", 4000), self.sent.append, self._close)
        for packet in data:
            self.session.on_data(packet)

    def _close(self) -> None:
        self.closed = True

    def take(self) -> list[bytes]:
        sent = self.sent[:]
        self.sent.clear()
        return sent

    def send(self, data: bytes) -> list[bytes]:
        self.session.on_data(data)
        return self.take()


@pytest.fixture
def now() -> list[int]:
    return [1000]


@pytest.fixture
def broker(now: list[int]) -> Broker:
    return Broker(lambda: now[0])


def offline(broker: Broker) -> None:
    """A client with a stored session and a QoS 1 subscription, disconnected."""
    Wire(broker, KEEP, SUBSCRIBE_QOS1)
    assert broker.disconnect("c")


def test_first_clean_session_zero_has_no_session_and_the_second_does(broker: Broker) -> None:
    first = Wire(broker)
    assert first.send(KEEP) == [NO_SESSION]
    assert first.send(SUBSCRIBE_QOS1) == [SUBACK_QOS1]
    assert broker.connection_attempts[0].session_present is False
    assert broker.clients[0].session_present is False
    assert broker.disconnect("c")

    second = Wire(broker)
    assert second.send(KEEP) == [SESSION_PRESENT]
    assert broker.connection_attempts[1].session_present is True
    assert broker.clients[1].session_present is True


def test_subscriptions_survive_but_subscribed_speaks_of_active_clients(broker: Broker) -> None:
    Wire(broker, KEEP, SUBSCRIBE_QOS1)
    live = broker.subscribed("a/#")
    assert live()
    broker.disconnect("c")
    assert not live()
    state = broker.session("c")
    assert state is not None and state.subscriptions == {"a/#": 1} and not state.connected
    Wire(broker, KEEP)
    assert live()  # Restored without a SUBSCRIBE.
    state = broker.session("c")
    assert state is not None and state.connected


def test_offline_qos1_is_queued_in_order_and_qos0_is_dropped(broker: Broker) -> None:
    offline(broker)
    broker.publish("a/1", "x", qos=1)
    broker.publish("a/2", "lost", qos=0)
    broker.publish("a/3", "y", qos=1)
    broker.publish("other", "unmatched", qos=1)
    state = broker.session("c")
    assert state is not None
    assert [m.payload for m in state.queued] == [b"x", b"y"]
    wire = Wire(broker, KEEP)
    assert wire.take() == [
        SESSION_PRESENT,
        b"\x32\x08\0\x03a/1\0\x01x",
        b"\x32\x08\0\x03a/3\0\x02y",
    ]
    state = broker.session("c")
    assert state is not None and not state.queued
    assert [d.packet_id for d in state.unacknowledged] == [1, 2]


def test_the_queue_has_no_limit(broker: Broker) -> None:
    offline(broker)
    for n in range(2000):
        broker.publish("a/b", str(n), qos=1)
    wire = Wire(broker, KEEP)
    sent = wire.take()
    assert len(sent) == 2001
    assert [p[7:9] for p in sent[1:4]] == [b"\0\x01", b"\0\x02", b"\0\x03"]
    assert sent[-1][9:] == b"1999"


def test_unacknowledged_go_again_with_dup_and_their_ids_before_the_queue(broker: Broker) -> None:
    wire = Wire(broker, KEEP, SUBSCRIBE_QOS1)
    wire.take()
    broker.publish("a/1", "one", qos=1)
    broker.publish("a/2", "two", qos=1)
    broker.publish("a/3", "three", qos=1)
    assert wire.take() == [
        b"\x32\x0a\0\x03a/1\0\x01one",
        b"\x32\x0a\0\x03a/2\0\x02two",
        b"\x32\x0c\0\x03a/3\0\x03three",
    ]
    wire.session.on_data(b"\x40\x02\0\x02")  # Only the second is acknowledged.
    broker.disconnect("c")
    broker.publish("a/4", "four", qos=1)

    again = Wire(broker, KEEP)
    assert again.take() == [
        SESSION_PRESENT,
        b"\x3a\x0a\0\x03a/1\0\x01one",
        b"\x3a\x0c\0\x03a/3\0\x03three",
        b"\x32\x0b\0\x03a/4\0\x04four",
    ]
    # A second reconnect without any PUBACK resends all three, in the order first sent.
    broker.disconnect("c")
    third = Wire(broker, KEEP)
    assert third.take() == [
        SESSION_PRESENT,
        b"\x3a\x0a\0\x03a/1\0\x01one",
        b"\x3a\x0c\0\x03a/3\0\x03three",
        b"\x3a\x0b\0\x03a/4\0\x04four",
    ]


def test_a_puback_ends_the_exchange_and_the_next_id_does_not_collide(broker: Broker) -> None:
    wire = Wire(broker, KEEP, SUBSCRIBE_QOS1)
    wire.take()
    broker.publish("a/b", "x", qos=1)
    broker.publish("a/b", "y", qos=1)
    wire.take()
    broker.disconnect("c")
    again = Wire(broker, KEEP)
    assert again.take()[1:] == [b"\x3a\x08\0\x03a/b\0\x01x", b"\x3a\x08\0\x03a/b\0\x02y"]
    again.session.on_data(b"\x40\x02\0\x01")
    broker.publish("a/b", "z", qos=1)
    assert again.take() == [b"\x32\x08\0\x03a/b\0\x03z"]
    broker.disconnect("c")
    third = Wire(broker, KEEP)
    assert third.take() == [
        SESSION_PRESENT,
        b"\x3a\x08\0\x03a/b\0\x02y",
        b"\x3a\x08\0\x03a/b\0\x03z",
    ]


def test_clean_session_discards_a_stored_session_at_connect(broker: Broker) -> None:
    wire = Wire(broker, KEEP, SUBSCRIBE_QOS1)
    wire.take()
    broker.publish("a/b", "unacknowledged", qos=1)
    broker.disconnect("c")
    broker.publish("a/b", "queued", qos=1)
    clean = Wire(broker, CLEAN)
    assert clean.take() == [NO_SESSION]
    assert not broker.subscribed("a/#")()
    broker.publish("a/b", "not routed", qos=1)
    assert clean.take() == []
    state = broker.session("c")
    assert state == SessionState(True, {}, (), ())


def test_clean_session_discards_its_state_at_disconnection(broker: Broker) -> None:
    Wire(broker, CLEAN, SUBSCRIBE_QOS1)
    assert broker.session("c") is not None
    broker.disconnect("c")
    assert broker.session("c") is None
    broker.publish("a/b", "not kept", qos=1)
    wire = Wire(broker)
    assert wire.send(KEEP) == [NO_SESSION]
    state = broker.session("c")
    assert state == SessionState(True, {}, (), ())


@pytest.mark.parametrize("graceful", [False, True])
def test_either_end_of_a_connection_keeps_a_stored_session(broker: Broker, graceful: bool) -> None:
    wire = Wire(broker, KEEP, SUBSCRIBE_QOS1)
    if graceful:
        wire.session.on_data(b"\xe0\0")
    else:
        wire.session.on_close()
    assert wire.session.closed
    broker.publish("a/b", "x", qos=1)
    assert Wire(broker, KEEP).take() == [SESSION_PRESENT, b"\x32\x08\0\x03a/b\0\x01x"]


def test_keepalive_expiry_keeps_a_stored_session(broker: Broker, now: list[int]) -> None:
    Wire(broker, KEEP, SUBSCRIBE_QOS1)
    now[0] += 15_000_000_000
    broker.poll()
    state = broker.session("c")
    assert state is not None and not state.connected and state.subscriptions == {"a/#": 1}


def test_a_new_connection_with_the_same_id_takes_over_and_resumes(broker: Broker) -> None:
    old = Wire(broker, KEEP, SUBSCRIBE_QOS1)
    old.take()
    broker.publish("a/b", "x", qos=1)
    old.take()
    new = Wire(broker, KEEP)
    assert old.closed
    assert new.take() == [SESSION_PRESENT, b"\x3a\x08\0\x03a/b\0\x01x"]


def test_taking_over_a_clean_connection_finds_no_session(broker: Broker) -> None:
    old = Wire(broker, CLEAN, SUBSCRIBE_QOS1)
    new = Wire(broker, KEEP)
    assert old.closed
    assert new.take() == [NO_SESSION]
    assert not broker.subscribed("a/#")()


def test_a_will_matching_the_clients_own_subscription_waits_for_it(broker: Broker) -> None:
    will = b"\x10\x18\0\x04MQTT\x04\x0c\0\x0a\0\x01c\0\x03a/w\0\x04gone"  # Will QoS 1, stored
    Wire(broker, will, SUBSCRIBE_QOS1)
    broker.disconnect("c")
    assert Wire(broker, KEEP).take() == [SESSION_PRESENT, b"\x32\x0b\0\x03a/w\0\x01gone"]


@pytest.mark.parametrize("attempt", [KEEP, CLEAN], ids=["keep", "clean"])
def test_a_refused_connect_says_no_session_and_touches_none(broker: Broker, attempt: bytes) -> None:
    offline(broker)
    broker.publish("a/b", "x", qos=1)
    broker.connection_policy = lambda connect: ConnackCode.NOT_AUTHORIZED
    refused = Wire(broker, attempt)
    assert refused.take() == [b"\x20\x02\0\x05"]
    assert refused.closed
    assert broker.connection_attempts[-1].session_present is False
    state = broker.session("c")
    assert state is not None and [m.payload for m in state.queued] == [b"x"]
    assert state.subscriptions == {"a/#": 1}
    broker.connection_policy = None
    assert Wire(broker, KEEP).take() == [SESSION_PRESENT, b"\x32\x08\0\x03a/b\0\x01x"]


def test_the_policy_runs_before_session_state_is_installed(broker: Broker) -> None:
    seen: list[SessionState | None] = []

    def policy(connect: Connect) -> ConnackCode:
        seen.append(broker.session(connect.client_id))
        return ConnackCode.ACCEPTED

    broker.connection_policy = policy
    Wire(broker, KEEP, SUBSCRIBE_QOS1)
    assert seen == [None]
    broker.disconnect("c")
    Wire(broker, CLEAN)
    # The stored session is still there when the policy is asked, and is not yet connected.
    assert seen[1] == SessionState(False, {"a/#": 1}, (), ())


def test_a_refused_connect_does_not_disconnect_the_client_it_names(broker: Broker) -> None:
    connected = Wire(broker, KEEP, SUBSCRIBE_QOS1)
    broker.connection_policy = lambda connect: ConnackCode.SERVER_UNAVAILABLE
    Wire(broker, CLEAN)
    assert not connected.closed
    state = broker.session("c")
    assert state is not None and state.connected


def test_an_incoming_publication_belongs_to_its_connection(broker: Broker) -> None:
    broker.puback = "hold"
    wire = Wire(broker, KEEP)
    wire.take()
    wire.session.on_data(b"\x32\x08\0\x03a/b\0\x09x")
    held = broker.pending[0]
    broker.disconnect("c")
    assert held.status == "cancelled" and not held.release()
    again = Wire(broker, KEEP)
    assert again.take() == [SESSION_PRESENT]  # No PUBACK comes back with the session.
    assert broker.pending == () and held.status == "cancelled"


def test_resubscribing_replaces_the_subscription_without_interrupting_the_flow(
    broker: Broker,
) -> None:
    broker.publish("a/kept", "retained", retain=True, qos=1)
    wire = Wire(broker, KEEP, SUBSCRIBE_QOS1)
    assert wire.take() == [NO_SESSION, SUBACK_QOS1, b"\x33\x12\0\x06a/kept\0\x01retained"]
    wire.session.on_data(b"\x40\x02\0\x01")
    broker.disconnect("c")
    broker.publish("a/b", "x", qos=1)

    again = Wire(broker, KEEP)
    assert again.take() == [SESSION_PRESENT, b"\x32\x08\0\x03a/b\0\x02x"]
    # MQTT-3.8.4-3: the retained message goes again, and what is in flight stays in flight.
    assert again.send(SUBSCRIBE_QOS1) == [SUBACK_QOS1, b"\x33\x12\0\x06a/kept\0\x03retained"]
    state = broker.session("c")
    assert state is not None and [d.packet_id for d in state.unacknowledged] == [2, 3]
    assert state.subscriptions == {"a/#": 1}


def test_a_retained_publication_queued_offline_goes_with_retain_zero(broker: Broker) -> None:
    offline(broker)
    broker.publish("a/b", "x", retain=True, qos=1)
    assert Wire(broker, KEEP).take() == [SESSION_PRESENT, b"\x32\x08\0\x03a/b\0\x01x"]


def test_a_retained_delivery_goes_again_with_its_retain_and_dup(broker: Broker) -> None:
    broker.publish("a/b", "x", retain=True, qos=1)
    Wire(broker, KEEP, SUBSCRIBE_QOS1)
    broker.disconnect("c")
    assert Wire(broker, KEEP).take() == [SESSION_PRESENT, b"\x3b\x08\0\x03a/b\0\x01x"]


def test_qos1_on_a_qos0_subscription_is_stored_and_goes_at_qos0(broker: Broker) -> None:
    Wire(broker, KEEP, SUBSCRIBE_QOS0)
    broker.disconnect("c")
    broker.publish("a/b", "kept", qos=1)  # MQTT-3.1.2-5 stores a QoS 1 message that matches.
    broker.publish("a/b", "dropped", qos=0)
    wire = Wire(broker, KEEP)
    assert wire.take() == [SESSION_PRESENT, b"\x30\x09\0\x03a/bkept"]
    state = broker.session("c")
    assert state == SessionState(True, {"a/#": 0}, (), ())
    # QoS 0 needs no acknowledgement, so nothing goes again.
    broker.disconnect("c")
    assert Wire(broker, KEEP).take() == [SESSION_PRESENT]


def test_dup_of_an_incoming_publish_is_not_propagated(broker: Broker) -> None:
    wire = Wire(broker, KEEP, SUBSCRIBE_QOS1)
    wire.take()
    assert wire.send(b"\x3a\x08\0\x03a/b\0\x09x") == [
        b"\x32\x08\0\x03a/b\0\x01x",
        b"\x40\x02\0\x09",
    ]


def test_running_out_of_packet_ids_keeps_the_message_for_a_stored_session(broker: Broker) -> None:
    wire = Wire(broker, KEEP, SUBSCRIBE_QOS1)
    wire.session.unacked.update(range(1, 65536))
    broker.publish("a/b", "x", qos=1)
    assert wire.closed
    state = broker.session("c")
    assert state is not None and [m.payload for m in state.queued] == [b"x"]


def test_running_out_of_packet_ids_on_resuming_keeps_the_queue_in_order(broker: Broker) -> None:
    wire = Wire(broker, KEEP, SUBSCRIBE_QOS1)
    wire.session.unacked.update(range(2, 65536))
    broker.disconnect("c")
    broker.publish("a/b", "first", qos=1)
    broker.publish("a/b", "second", qos=1)
    again = Wire(broker, KEEP)
    assert again.closed
    state = broker.session("c")
    assert state is not None and [m.payload for m in state.queued] == [b"second"]
    assert [d.payload for d in state.unacknowledged] == [b"first"]


def test_session_reports_a_copy_and_none_for_an_unknown_client(broker: Broker) -> None:
    assert broker.session("nobody") is None
    wire = Wire(broker, KEEP, SUBSCRIBE_QOS1)
    wire.take()
    broker.publish("a/b", "x", qos=1)
    state = broker.session("c")
    assert state is not None
    assert state.connected
    assert state.subscriptions == {"a/#": 1}
    assert state.queued == ()
    assert [d.payload for d in state.unacknowledged] == [b"x"]
    state.subscriptions.clear()
    assert broker.subscribed("a/#")()


def test_forget_session(broker: Broker) -> None:
    assert broker.forget_session("nobody") is False
    Wire(broker, KEEP, SUBSCRIBE_QOS1)
    with pytest.raises(ValueError, match=r"^mqtt: c is connected: disconnect it first$"):
        broker.forget_session("c")
    assert broker.session("c") is not None
    broker.disconnect("c")
    broker.publish("a/b", "x", qos=1)
    assert broker.forget_session("c") is True
    assert broker.session("c") is None
    assert broker.forget_session("c") is False
    wire = Wire(broker)
    assert wire.send(KEEP) == [NO_SESSION]


def test_deliveries_record_every_publish_with_its_flags(broker: Broker, now: list[int]) -> None:
    wire = Wire(broker, KEEP, SUBSCRIBE_QOS1)
    first_connection = broker.clients[0].connection_id
    broker.publish("a/q0", "zero", qos=0)
    now[0] = 2000
    broker.publish("a/q1", "one", qos=1, retain=True)
    wire.session.on_data(b"")
    zero, one = broker.deliveries
    assert zero == Delivery(
        Duration(1000), "c", first_connection, "a/q0", b"zero", 0, None, False, False, None
    )
    assert one == Delivery(
        Duration(2000), "c", first_connection, "a/q1", b"one", 1, 1, False, False, None
    )
    assert one.text == "one"
    now[0] = 3000
    broker.disconnect("c")
    now[0] = 4000
    again = Wire(broker, KEEP)
    assert broker.deliveries[2:] == [
        Delivery(
            Duration(4000),
            "c",
            broker.clients[1].connection_id,
            "a/q1",
            b"one",
            1,
            1,
            True,
            False,
            None,
        )
    ]
    now[0] = 5000
    again.session.on_data(b"\x40\x02\0\x01")
    redelivered = broker.deliveries[2]
    assert redelivered.acknowledged_at == Duration(5000)
    # The first attempt's connection closed before any PUBACK: its record says so.
    assert one.acknowledged_at is None
    assert zero.acknowledged_at is None
    state = broker.session("c")
    assert state is not None and state.unacknowledged == ()
    # An unknown PUBACK changes nothing.
    again.session.on_data(b"\x40\x02\0\x01")
    assert redelivered.acknowledged_at == Duration(5000)


def test_a_retained_delivery_records_retain(broker: Broker) -> None:
    broker.publish("a/b", "x", qos=1, retain=True)
    Wire(broker, KEEP, SUBSCRIBE_QOS1)
    [delivery] = broker.deliveries
    assert delivery.retain and not delivery.dup and delivery.packet_id == 1


def test_next_delivery_uses_append_order_without_consuming(broker: Broker, now: list[int]) -> None:
    Wire(broker, KEEP, SUBSCRIBE_QOS1)
    assert broker.find_delivery() is None
    any_delivery = broker.next_delivery()
    saying = broker.next_delivery("a/+", "new")
    assert any_delivery() is None and saying() is None
    broker.publish("a/b", "old", qos=1)
    old = broker.deliveries[0]
    broker.publish("a/b", "new", qos=1)
    new = broker.deliveries[1]
    for _ in range(2):
        assert any_delivery() is old
        assert saying() is new
    assert broker.find_delivery("a/#") is old
    assert broker.find_delivery("a/b", b"new") is new
    assert broker.find_delivery("a/b", "absent") is None
    assert broker.find_delivery("other/#") is None
    # The boundary is the call: what is recorded already is excluded, at an equal time too.
    later = broker.next_delivery("a/#")
    assert later() is None
    broker.publish("a/c", "third", qos=1)
    assert later() is broker.deliveries[2]


def test_next_delivery_is_named_and_refuses_bool(broker: Broker) -> None:
    assert repr(broker.next_delivery()) == "the next MQTT delivery on '#'"
    condition = broker.next_delivery("a/+", "new")
    assert repr(condition) == "the next MQTT delivery on 'a/+' saying b'new'"
    with pytest.raises(TypeError, match=r"call the MQTT condition with \(\), or pass it to"):
        bool(condition)


def test_an_anonymous_client_has_a_session_while_it_is_connected(broker: Broker) -> None:
    Wire(broker, b"\x10\x0c\0\x04MQTT\x04\x02\0\0\0\0")
    state = broker.session("esp32tl-anonymous-1")
    assert state is not None and state.connected
    broker.disconnect("esp32tl-anonymous-1")
    assert broker.session("esp32tl-anonymous-1") is None


def test_a_stored_session_outlives_a_network_reset() -> None:
    wire = failures.Wire()
    broker = wire.net.broker
    peer = failures.Peer(wire, 4000)
    peer.send(KEEP + SUBSCRIBE_QOS1)
    assert peer.read() == NO_SESSION + SUBACK_QOS1
    wire.net.stop()
    assert not broker.subscribed("a/#")()
    broker.publish("a/b", "x", qos=1)
    state = broker.session("c")
    assert state is not None and not state.connected
    assert state.subscriptions == {"a/#": 1}
    assert [m.payload for m in state.queued] == [b"x"]
