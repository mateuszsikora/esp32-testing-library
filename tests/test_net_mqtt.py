"""MQTT wire dialogue, independent of MQTT packet builders in the implementation."""

import pytest
from conftest import CountingList

from esp32tl.net.mqtt import (
    TEST_CLIENT,
    Broker,
    Delivery,
    Message,
    RequestedFilter,
    SubscribeRequest,
    matches,
)
from esp32tl.units import Duration

CONNECT = b"\x10\x0d\x00\x04MQTT\x04\x02\x00\x0a\x00\x01c"
SUBSCRIBE = b"\x82\x08\x00\x01\x00\x03a/#\x01"


def test_split_connect_subscribe_qos_one_retained_and_unsubscribe() -> None:
    now = [20]
    broker = Broker(lambda: now[0])
    broker.publish("a/b", "old", qos=1, retain=True)
    sent: list[bytes] = []
    closed: list[bool] = []
    session = broker.accept(("10.0.0.2", 100), sent.append, lambda: closed.append(True))
    connected = broker.next_connection()
    for byte in CONNECT:
        session.on_data(bytes([byte]))
    assert sent == [b"\x20\x02\0\0"]
    assert connected() is not None and connected().client_id == "c"  # type: ignore[union-attr]
    assert connected().keepalive == 10  # type: ignore[union-attr]
    session.on_data(SUBSCRIBE)
    assert broker.subscribed("a/#")()
    assert sent[-2:] == [b"\x90\x03\0\x01\x01", b"\x33\x0a\0\x03a/b\0\x01old"]
    session.on_data(b"\x40\x02\0\x01")
    assert not session.unacked
    waiting = broker.next_message("a/+", "new")
    now[0] = 30
    session.on_data(b"\x32\x0a\0\x03a/b\0\x09new")
    assert sent[-1] == b"\x40\x02\0\x09"
    message = waiting()
    assert message is not None and message.text == "new" and message.at.ns == 30
    assert sent[-2][0] == 0x32  # RETAIN cleared for an established subscription
    session.on_data(b"\xa2\x07\0\x02\0\x03a/#")
    assert sent[-1] == b"\xb0\x02\0\x02"
    assert not broker.subscribed("a/#")()
    broker.publish("a/b", b"", retain=True)
    assert not broker.retained
    session.on_data(b"\xc0\0\xe0\0")
    assert sent[-1] == b"\xd0\0" and closed == [True]


@pytest.mark.parametrize("graceful", [False, True])
def test_will_on_transport_loss_once_and_never_on_disconnect(graceful: bool) -> None:
    now = [0]
    broker = Broker(lambda: now[0])
    session = broker.accept(("10.0.0.2", 100), lambda b: None, lambda: None)
    # Will flag, QoS 1, retained; topic a/b, payload gone.
    session.on_data(b"\x10\x18\0\x04MQTT\x04\x2e\0\x01\0\x01c\0\x03a/b\0\x04gone")
    now[0] = 1000
    if graceful:
        session.on_data(b"\xe0\0")
    session.on_close()
    session.on_close()
    assert len(broker.messages) == (0 if graceful else 1)
    if not graceful:
        assert broker.messages[0].at.ns == 1000
        assert broker.retained["a/b"].payload == b"gone"


def test_keepalive_expires_on_the_board_clock() -> None:
    now = [0]
    broker = Broker(lambda: now[0])
    closed: list[bool] = []
    session = broker.accept(("10.0.0.2", 100), lambda b: None, lambda: closed.append(True))
    session.on_data(CONNECT)
    assert broker.quiet_until() == 15_000_000_000
    now[0] = 10_000_000_000
    session.on_data(b"\xc0\0")
    assert broker.quiet_until() == 25_000_000_000
    broker.poll()
    assert closed == []
    now[0] = 25_000_000_000
    broker.poll()
    assert closed == [True] and broker.quiet_until() is None


@pytest.mark.parametrize(
    ("pattern", "topic", "answer"),
    [
        ("a/#", "a", True),
        ("a/+", "a/", True),
        ("a/+", "a/b/c", False),
        ("#", "$SYS/load", False),
        ("+/load", "$SYS/load", False),
        ("$SYS/#", "$SYS/load", True),
        ("a/#/b", "a/c", False),
    ],
)
def test_topic_filters(pattern: str, topic: str, answer: bool) -> None:
    assert matches(pattern, topic) is answer


@pytest.mark.parametrize(
    "bad",
    [
        b"\x30\x03\0\x01a",
        b"\x10\x01\0",
        b"\x10\xff\xff\xff\xff\x00",
        b"\x10\x0d\0\x04MQTT\x05\x02\0\0\0\x01c",
    ],
)
def test_bad_packets_close_without_an_exception(bad: bytes) -> None:
    closed: list[bool] = []
    broker = Broker(lambda: 0)
    session = broker.accept(("10.0.0.2", 100), lambda b: None, lambda: closed.append(True))
    session.on_data(bad)
    assert closed == [True]
    assert not broker.clients and not broker.messages


def test_invalid_connect_does_not_publish_an_unaccepted_will() -> None:
    broker = Broker(lambda: 0)
    session = broker.accept(("10.0.0.2", 100), lambda b: None, lambda: None)
    # A valid-looking Will, but trailing garbage makes the CONNECT invalid.
    session.on_data(b"\x10\x19\0\x04MQTT\x04\x2e\0\x01\0\x01c\0\x03a/b\0\x04gone!")
    assert session.closed and not broker.clients and not broker.messages


def test_qos_one_with_no_free_id_waits_for_a_puback_instead_of_reusing_one() -> None:
    lines: list[str] = []
    broker = Broker(lambda: 0, lines.append)
    sent: list[bytes] = []
    session = broker.accept(("10.0.0.2", 100), sent.append, lambda: None)
    session.on_data(CONNECT + SUBSCRIBE)  # CleanSession=1
    session.unacked.update(range(1, 65536))
    sent.clear()
    broker.publish("a/b", "one", qos=1)
    broker.publish("a/b", "two", qos=1)
    assert not session.closed and sent == []
    state = broker.session("c")
    assert state is not None and [m.text for m in state.queued] == ["one", "two"]
    waits = [line for line in lines if "no free packet identifier" in line]
    assert waits == ["mqtt: c has no free packet identifier: waiting for a PUBACK, 1 queued"]
    session.on_data(b"\x40\x02\0\x05")
    assert sent == [b"\x32\x0a\0\x03a/b\0\x05one"]
    session.on_data(b"\x40\x02\0\x07")
    assert sent[1:] == [b"\x32\x0a\0\x03a/b\0\x07two"]
    state = broker.session("c")
    assert state is not None and state.queued == ()
    # A new wait is logged again, and the queue ends with the connection that holds it.
    broker.publish("a/b", "three", qos=1)
    assert len([line for line in lines if "no free packet identifier" in line]) == 2
    assert broker.disconnect("c") and broker.session("c") is None


@pytest.mark.parametrize("will", [False, True])
def test_anonymous_clients_have_independent_ids_and_wills(will: bool) -> None:
    broker = Broker(lambda: 123)
    sent: list[bytes] = []
    closed: list[int] = []
    sessions = []
    connect = (
        b"\x10\x17\0\x04MQTT\x04\x2e\0\0\0\0\0\x03a/b\0\x04gone"
        if will
        else b"\x10\x0c\0\x04MQTT\x04\x02\0\0\0\0"
    )
    for port in (4000, 4001):

        def on_close(port: int = port) -> None:
            closed.append(port)

        session = broker.accept(("10.64.0.2", port), sent.append, on_close)
        session.on_data(connect)
        sessions.append(session)
    assert closed == [] and broker.messages == []
    assert sent == [b"\x20\x02\0\0"] * 2
    assert [c.client_id for c in broker.clients] == ["esp32tl-anonymous-1", "esp32tl-anonymous-2"]
    for session in sessions:
        session.on_close()
        session.on_close()
    assert closed == [4000, 4001]
    assert [m.client for m in broker.messages] == (
        [c.client_id for c in broker.clients] if will else []
    )
    assert all(m.payload == b"gone" and m.at.ns == 123 for m in broker.messages)


def test_anonymous_id_skips_an_id_already_supplied_by_a_client() -> None:
    broker = Broker(lambda: 0)
    closed: list[bool] = []
    named = broker.accept(("10.64.0.2", 4000), lambda b: None, lambda: closed.append(True))
    name = b"esp32tl-anonymous-1"
    named.on_data(
        bytes([0x10, 12 + len(name)]) + b"\0\x04MQTT\x04\x02\0\0" + bytes([0, len(name)]) + name
    )
    anonymous = broker.accept(("10.64.0.2", 4001), lambda b: None, lambda: None)
    anonymous.on_data(b"\x10\x0c\0\x04MQTT\x04\x02\0\0\0\0")
    assert not closed
    assert [c.client_id for c in broker.clients] == ["esp32tl-anonymous-1", "esp32tl-anonymous-2"]
    # An explicitly repeated ID still takes over its old connection.
    replacement = broker.accept(("10.64.0.2", 4002), lambda b: None, lambda: None)
    replacement.on_data(
        bytes([0x10, 12 + len(name)]) + b"\0\x04MQTT\x04\x02\0\0" + bytes([0, len(name)]) + name
    )
    assert closed == [True]


def test_empty_id_without_clean_session_gets_identifier_rejected() -> None:
    broker = Broker(lambda: 0)
    sent: list[bytes] = []
    closed: list[bool] = []
    session = broker.accept(("10.64.0.2", 4000), sent.append, lambda: closed.append(True))
    session.on_data(b"\x10\x0c\0\x04MQTT\x04\0\0\0\0\0")
    assert sent == [b"\x20\x02\0\x02"] and closed == [True]
    assert not broker.clients and not broker.messages


def test_history_and_next_events_use_append_order_without_consuming() -> None:
    broker = Broker(lambda: 123)
    assert broker.find_message() is None and broker.find_connection() is None
    waiting = broker.next_message("a/+", "new")
    connected = broker.next_connection()
    assert waiting() is None and connected() is None
    session = broker.accept(("10.64.0.2", 4000), lambda data: None, lambda: None)
    # CONNECT and the immediate publication arrive together, before another test poll.
    session.on_data(CONNECT + b"\x30\x08\0\x03a/bnew")
    first_message, first_client = broker.messages[0], broker.clients[0]
    for _ in range(2):
        assert waiting() is first_message
        assert connected() is first_client
        assert broker.find_message("a/#", b"new") is first_message
        assert broker.find_connection() is first_client
    assert broker.find_message("other/#") is None
    assert broker.find_message("a/b", "absent") is None
    next_message, next_client = broker.next_message("a/+", "new"), broker.next_connection()
    assert next_message() is None and next_client() is None
    # The timestamp does not move. Unrelated and mismatched messages do not satisfy the wait.
    broker.publish("other/b", "new")
    broker.publish("a/b", "different")
    assert next_message() is None
    session.on_close()
    replacement = broker.accept(("10.64.0.2", 4001), lambda data: None, lambda: None)
    replacement.on_data(CONNECT + b"\x30\x08\0\x03a/bnew")
    for _ in range(2):
        assert next_message() is broker.messages[-1]
        assert next_client() is broker.clients[-1]
        assert waiting() is first_message and connected() is first_client
    assert first_message.at == broker.messages[-1].at
    assert first_client.at == broker.clients[-1].at
    assert broker.find_message("a/b", "new") is first_message
    assert broker.find_connection() is first_client


def test_find_messages_reads_every_match_after_a_mark_by_topic_payload_and_client() -> None:
    broker = Broker(lambda: 123)
    assert broker.find_messages() == []
    session = broker.accept(("10.64.0.2", 4000), lambda data: None, lambda: None)
    session.on_data(CONNECT + b"\x30\x08\0\x03a/bold")
    before = broker.mark()
    assert broker.find_messages(since=before) == []
    # Appended after the mark at the mark's own board time: included, in append order.
    broker.publish("a/b", "config", retain=True)
    session.on_data(b"\x30\x08\0\x03a/bnew" + b"\x30\x08\0\x03x/ynew" + b"\x30\x08\0\x03a/cnew")
    after = broker.mark()
    session.on_data(b"\x30\x0a\0\x03a/blater")
    old, config, new_b, new_y, new_c, later = broker.messages
    assert later.payload == b"later" and old.at == later.at
    assert broker.find_messages() == list(broker.messages)
    assert broker.find_messages(since=before) == [config, new_b, new_y, new_c, later]
    assert broker.find_messages("a/+", since=before, client="c") == [new_b, new_c, later]
    assert broker.find_messages("a/+", "new", since=before) == [new_b, new_c]
    assert broker.find_messages("a/b", b"new", since=before, client="c") == [new_b]
    assert broker.find_messages(since=before, client=TEST_CLIENT) == [config]
    assert config.client == TEST_CLIENT == "(test)"
    assert broker.find_messages(since=after) == [later]
    assert broker.find_messages(client="absent") == []
    # Reading consumes nothing; the returned list is the caller's.
    broker.find_messages(since=before).clear()
    assert len(broker.find_messages(since=before)) == 5 and len(broker.messages) == 6


def test_find_messages_refuses_a_mark_from_another_broker_and_an_index() -> None:
    mark, broker = Broker(lambda: 0).mark(), Broker(lambda: 0)
    with pytest.raises(ValueError, match="mqtt: this mark belongs to another broker"):
        broker.find_messages(since=mark)
    with pytest.raises(TypeError, match=r"mqtt: since takes a mark from broker\.mark\(\)"):
        broker.find_messages(since=0)  # type: ignore[arg-type]


def test_subscription_condition_tracks_current_state_without_latching() -> None:
    broker = Broker(lambda: 0)
    subscribed = broker.subscribed("a/#")
    session = broker.accept(("10.64.0.2", 4000), lambda data: None, lambda: None)
    assert not subscribed()
    session.on_data(CONNECT + SUBSCRIBE)
    assert subscribed() and subscribed()
    session.on_data(b"\xa2\x07\0\x02\0\x03a/#")
    assert not subscribed()
    session.on_data(SUBSCRIBE)
    assert subscribed()
    session.on_close()
    assert not subscribed()


@pytest.mark.parametrize(
    ("method", "args", "description"),
    [
        ("next_message", ("a/+", "new"), "the next MQTT message on 'a/+' saying b'new'"),
        ("next_message", (), "the next MQTT message on '#'"),
        ("next_connection", (), "the next MQTT client connection"),
        ("next_connection_attempt", (), "the next MQTT connection attempt"),
        ("subscribed", ("a/#",), "an active MQTT subscription to 'a/#'"),
        ("next_subscribe", (), "the next MQTT SUBSCRIBE"),
        ("next_subscribe", ("a/#",), "the next MQTT SUBSCRIBE to 'a/#'"),
    ],
)
def test_mqtt_conditions_are_named_and_reject_truth_testing(
    method: str, args: tuple[str, ...], description: str
) -> None:
    broker = Broker(lambda: 0)
    condition = getattr(broker, method)(*args)
    assert repr(condition) == description
    assert not condition()
    with pytest.raises(TypeError, match="call the MQTT condition"):
        bool(condition)
    session = broker.accept(("10.64.0.2", 4000), lambda data: None, lambda: None)
    session.on_data(CONNECT + SUBSCRIBE + b"\x30\x08\0\x03a/bnew")
    assert condition()
    with pytest.raises(TypeError, match="call the MQTT condition"):
        bool(condition)


PUBLISH = b"\x32\x0a\0\x03a/b\0\x09new"
DUPLICATE = b"\x3a" + PUBLISH[1:]
PUBACK = b"\x40\x02\0\x09"


def test_default_puback_records_emission_separately_from_receipt() -> None:
    broker = Broker(lambda: 20)
    sent: list[bytes] = []
    session = broker.accept(("10.0.0.2", 100), sent.append, lambda: None)
    session.on_data(CONNECT + PUBLISH)
    message = broker.messages[0]
    assert message.connection_id == broker.clients[0].connection_id
    assert message.packet_id == 9 and not message.dup
    handle = message.publication
    assert handle is not None and handle.messages == (message,)
    assert handle.status == "emitted" and handle.puback_at == message.at
    assert sent[-1] == PUBACK and broker.pending == ()
    assert not handle.release()
    # QoS 0, test publications and Wills have no incoming PUBACK handle.
    session.on_data(b"\x30\x08\0\x03a/bnew")
    broker.publish("a/b", "test", qos=1)
    assert all(m.publication is None and m.packet_id is None for m in broker.messages[1:])
    assert broker.messages[-1].connection_id is None


def test_rejected_connect_uses_a_connection_id_and_accepted_records_carry_theirs() -> None:
    from esp32tl.net.mqtt import ConnackCode

    broker = Broker(lambda: 0, connection_policy=lambda c: ConnackCode.NOT_AUTHORIZED)
    rejected = broker.accept(("10.0.0.2", 100), lambda b: None, lambda: None)
    rejected.on_data(CONNECT)
    assert rejected.closed and not broker.clients
    broker.connection_policy = None
    session = broker.accept(("10.0.0.2", 101), lambda b: None, lambda: None)
    # A Will on topic a/w, then a QoS 1 publication.
    session.on_data(b"\x10\x16\0\x04MQTT\x04\x06\0\x0a\0\x01c\0\x03a/w\0\x02by" + PUBLISH)
    assert session.connection_id > rejected.connection_id
    assert broker.clients[0].connection_id == session.connection_id
    assert broker.messages[0].connection_id == session.connection_id
    broker.disconnect("c")
    assert broker.messages[-1].topic == "a/w"
    assert broker.messages[-1].connection_id == session.connection_id


def test_a_will_is_told_apart_from_the_same_bytes_the_client_published_itself() -> None:
    now = [5]
    broker = Broker(lambda: now[0])
    session = broker.accept(("10.0.0.2", 100), lambda b: None, lambda: None)
    # A retained Will on a/w saying "by", then the same message as the client's own PUBLISH.
    session.on_data(b"\x10\x16\0\x04MQTT\x04\x26\0\x0a\0\x01c\0\x03a/w\0\x02by")
    any_will = broker.next_message("a/w", "by", will=True)
    not_will = broker.next_message("a/w", will=False)
    session.on_data(b"\x31\x07\0\x03a/wby")
    broker.publish("a/w", "by")
    assert any_will() is None
    now[0] = 9
    broker.disconnect("c")
    own, test, will = broker.messages
    # Same client, topic, payload, QoS and retain: only `will` and the time tell them apart.
    assert (own.client, own.topic, own.payload, own.qos, own.retain) == (
        will.client,
        will.topic,
        will.payload,
        will.qos,
        will.retain,
    )
    assert own.client == will.client == "c" and test.client == TEST_CLIENT
    assert will.will and not own.will and not test.will and will.at.ns == 9
    assert own != will and broker.retained["a/w"] is will
    assert any_will() is will and not_will() is own
    assert broker.find_message(will=True) is will and broker.find_message("a/w", will=False) is own
    assert broker.find_messages(will=True) == [will]
    assert broker.find_messages(will=False) == [own, test]
    assert broker.find_messages(client="c", will=False) == [own]
    assert broker.find_messages(client="c") == [own, will]
    assert broker.find_messages(client=TEST_CLIENT, will=True) == []
    assert repr(any_will) == "the next MQTT Will on 'a/w' saying b'by'"
    assert repr(not_will) == "the next MQTT message other than a Will on 'a/w'"
    assert repr(broker.next_message("a/w")) == "the next MQTT message on 'a/w'"
    for refused in (
        lambda: broker.find_messages(will=1),  # type: ignore[arg-type]
        lambda: broker.find_message(will="yes"),  # type: ignore[arg-type]
        lambda: broker.next_message(will=0),  # type: ignore[arg-type]
    ):
        with pytest.raises(TypeError, match="mqtt: will takes True, False or None"):
            refused()


def test_qos_zero_publish_with_dup_closes_as_malformed() -> None:
    log: list[str] = []
    broker = Broker(lambda: 0, log.append)
    session = broker.accept(("10.0.0.2", 100), lambda b: None, lambda: None)
    session.on_data(CONNECT + b"\x38\x08\0\x03a/bnew")
    assert session.closed and not broker.messages
    assert log[-1] == "mqtt: malformed or unsupported packet, closing"


def test_pending_id_reused_for_different_content_is_merged_and_logged() -> None:
    log: list[str] = []
    broker = Broker(lambda: 0, log.append)
    broker.puback = "hold"
    session = broker.accept(("10.0.0.2", 100), lambda b: None, lambda: None)
    session.on_data(CONNECT + PUBLISH + DUPLICATE)
    connection = broker.clients[0].connection_id
    reuse = (
        f"mqtt: c (connection {connection}) reused pending packet ID 9 for a different publication"
    )
    assert reuse not in log
    session.on_data(DUPLICATE[:-3] + b"XYZ")
    (handle,) = broker.pending
    assert [m.payload for m in handle.messages] == [b"new", b"new", b"XYZ"]
    assert log.count(reuse) == 1
    assert handle.release()
    assert log[-1] == f"mqtt: c (connection {connection}) PUBACK 9 emitted"


def test_hold_routes_duplicates_and_release_acknowledges_only_the_selected_exchange() -> None:
    from esp32tl.units import Duration

    now = [20]
    broker = Broker(lambda: now[0])
    broker.puback = "hold"
    sent: list[bytes] = []
    session = broker.accept(("10.0.0.2", 100), sent.append, lambda: None)
    session.on_data(CONNECT + SUBSCRIBE + PUBLISH)
    first = broker.pending[0]
    # Another packet ID, and a retransmission of the first, all reach subscribers.
    session.on_data(PUBLISH[:8] + b"\x0a" + PUBLISH[9:] + DUPLICATE)
    assert len(broker.pending) == 2 and len(broker.messages) == 3
    assert len([p for p in sent if p[0] == 0x32]) == 3
    assert [m.dup for m in broker.messages] == [False, False, True]
    assert first.messages == (broker.messages[0], broker.messages[2])
    assert broker.messages[2].publication is first
    assert first.puback_at is None and PUBACK not in sent
    now[0] = 50
    assert first.release()
    assert first.status == "emitted" and first.puback_at == Duration(50)
    assert sent.count(PUBACK) == 1 and len(broker.pending) == 1
    assert not first.release()
    # The same ID after emission is a new exchange, even if DUP remains set.
    session.on_data(DUPLICATE)
    reused = broker.messages[-1].publication
    assert reused is not None and reused is not first and reused.status == "pending"
    assert not first.release() and reused.puback_at is None
    assert reused.release() and sent.count(PUBACK) == 2


def test_delayed_puback_uses_first_receipt_and_policy_changes_only_new_exchanges() -> None:
    from esp32tl.units import Duration

    now = [20]
    broker = Broker(lambda: now[0])
    broker.puback = Duration(100)
    sent: list[bytes] = []
    session = broker.accept(("10.0.0.2", 100), sent.append, lambda: None)
    session.on_data(CONNECT + PUBLISH)
    handle = broker.pending[0]
    assert broker.quiet_until() == 120
    now[0] = 100
    broker.puback = "immediate"
    session.on_data(DUPLICATE)
    broker.poll()
    assert handle.puback_at is None and broker.quiet_until() == 120
    now[0] = 119
    broker.poll()
    assert PUBACK not in sent
    now[0] = 120
    broker.poll()
    assert handle.puback_at == Duration(120) and sent.count(PUBACK) == 1
    assert broker.quiet_until() == 15_000_000_100
    session.on_data(PUBLISH)
    assert broker.messages[-1].publication is not handle
    assert sent.count(PUBACK) == 2


@pytest.mark.parametrize("policy", ["immediate", "zero", "delay"])
def test_manual_release_cancels_a_scheduled_puback(policy: str) -> None:
    from esp32tl.units import Duration

    now = [0]
    broker = Broker(lambda: now[0])
    broker.puback = (
        "immediate" if policy == "immediate" else Duration(0 if policy == "zero" else 100)
    )
    sent: list[bytes] = []
    session = broker.accept(("10.0.0.2", 100), sent.append, lambda: None)
    session.on_data(CONNECT + PUBLISH)
    handle = broker.messages[0].publication
    assert handle is not None
    now[0] = 50
    assert handle.release() is (policy == "delay")
    now[0] = 100
    broker.poll()
    assert sent.count(PUBACK) == 1
    assert handle.puback_at == Duration(50 if policy == "delay" else 0)


@pytest.mark.parametrize("ending", ["disconnect", "graceful", "loss", "takeover", "keepalive"])
def test_connection_end_cancels_pending_ack_and_stale_release_cannot_ack_reconnect(
    ending: str,
) -> None:
    from esp32tl.units import seconds

    now = [0]
    broker = Broker(lambda: now[0])
    # The one-second keepalive expires at the same instant as the ACK deadline.
    broker.puback = seconds(1.5)
    sent: list[bytes] = []
    closed: list[bool] = []
    session = broker.accept(("10.0.0.2", 100), sent.append, lambda: closed.append(True))
    session.on_data(b"\x10\x18\0\x04MQTT\x04\x2e\0\x01\0\x01c\0\x03a/b\0\x04gone")
    session.on_data(PUBLISH)
    old = broker.pending[0]
    now[0] = 1_500_000_000
    replacement = None
    if ending == "disconnect":
        assert broker.disconnect("c")
        assert not broker.disconnect("c")
    elif ending == "graceful":
        session.on_data(b"\xe0\0")
    elif ending == "loss":
        session.on_close()
    elif ending == "takeover":
        replacement = broker.accept(("10.0.0.2", 101), sent.append, lambda: None)
        replacement.on_data(CONNECT)
    else:
        broker.poll()
    assert old.status == "cancelled" and old.puback_at is None
    pending = broker.pending
    assert pending == () and not old.release() and PUBACK not in sent
    assert closed == [True]
    wills = [m for m in broker.messages if m.payload == b"gone"]
    assert len(wills) == (0 if ending == "graceful" else 1)
    if wills:
        assert wills[0].at == seconds(1.5) and wills[0].publication is None
        assert wills[0].connection_id == old.connection_id
    if replacement is None:
        replacement = broker.accept(("10.0.0.2", 101), sent.append, lambda: None)
        replacement.on_data(CONNECT)
    broker.puback = "hold"
    replacement.on_data(DUPLICATE)
    new = broker.pending[0]
    assert new.connection_id != old.connection_id and new.packet_id == old.packet_id
    assert broker.clients[-1].connection_id == new.connection_id
    assert not old.release() and new.puback_at is None
    now[0] += 1
    assert new.release() and sent.count(PUBACK) == 1
    broker.poll()
    assert sent.count(PUBACK) == 1


@pytest.mark.parametrize("bad", ["later", None, 2, -1])
def test_puback_policy_refuses_invalid_values(bad: object) -> None:
    broker = Broker(lambda: 0)
    with pytest.raises(ValueError, match="PUBACK policy must be"):
        broker.puback = bad  # type: ignore[assignment]
    assert broker.puback == "immediate"


def test_puback_delay_refuses_negative_duration() -> None:
    from esp32tl.units import Duration

    broker = Broker(lambda: 0)
    with pytest.raises(ValueError, match="PUBACK delay must be nonnegative"):
        broker.puback = Duration(-1)


def test_disconnect_leaves_other_clients_pending_and_emitted_history_intact() -> None:
    from esp32tl.units import Duration

    now = [0]
    broker = Broker(lambda: now[0])
    broker.puback = "hold"
    first_sent: list[bytes] = []
    first = broker.accept(("10.0.0.2", 100), first_sent.append, lambda: None)
    first.on_data(CONNECT + PUBLISH)
    held = broker.pending[0]
    second_sent: list[bytes] = []
    second = broker.accept(("10.0.0.2", 101), second_sent.append, lambda: None)
    second.on_data(CONNECT[:-1] + b"d" + PUBLISH)
    other = broker.messages[-1].publication
    assert other is not None and other is not held
    assert not broker.disconnect("unknown")
    assert list(broker.pending) == [held, other]
    assert broker.disconnect("c")
    assert broker.pending == (other,)
    assert not held.release() and PUBACK not in first_sent
    now[0] = 30
    assert other.release() and second_sent[-1] == PUBACK
    assert broker.disconnect("d")
    assert other.status == "emitted" and other.puback_at == Duration(30)
    assert not other.release() and broker.quiet_until() is None


def test_failed_stream_write_cancels_instead_of_claiming_puback_emission() -> None:
    broker = Broker(lambda: 0)
    broker.puback = "hold"

    def write(data: bytes) -> None:
        if data == PUBACK:
            session.on_close()  # The TLS adapter closes synchronously on an SSL write error.

    session = broker.accept(("10.0.0.2", 100), write, lambda: None)
    session.on_data(CONNECT + PUBLISH)
    handle = broker.pending[0]
    assert not handle.release()
    assert handle.status == "cancelled" and handle.puback_at is None
    assert not handle.release() and not broker.pending


def test_late_poll_stamps_actual_emission_time_and_does_not_backdate() -> None:
    from esp32tl.units import Duration

    now = [0]
    broker = Broker(lambda: now[0])
    broker.puback = Duration(100)
    session = broker.accept(("10.0.0.2", 100), lambda data: None, lambda: None)
    session.on_data(CONNECT + PUBLISH)
    handle = broker.pending[0]
    now[0] = 200
    broker.poll()
    assert handle.puback_at == Duration(200)


KEEP = b"\x10\x0d\0\x04MQTT\x04\x00\0\x0a\0\x01c"  # CleanSession=0, client ID c
SUBSCRIBE_TWO = b"\x82\x0c\0\x05\0\x03a/#\x02\0\x01b\x00"  # a/# at QoS 2, b at QoS 0
UNSUBSCRIBE = b"\xa2\x07\0\x06\0\x03a/#"


def test_subscribe_requests_keep_every_filter_after_unsubscribe_and_clean_session() -> None:
    now = [10]
    broker = Broker(lambda: now[0])
    sent: list[bytes] = []
    first = broker.accept(("10.64.0.2", 4000), sent.append, lambda: None)
    first.on_data(KEEP + SUBSCRIBE_TWO)
    assert sent[-1] == b"\x90\x04\0\x05\x01\x00"
    now[0] = 20
    first.on_data(UNSUBSCRIBE)
    assert broker.disconnect("c")
    now[0] = 30
    second = broker.accept(("10.64.0.2", 4001), sent.append, lambda: None)
    second.on_data(CONNECT)  # CleanSession=1 discards the stored subscription to b.
    state = broker.session("c")
    assert state is not None and state.subscriptions == {}
    assert broker.subscribe_requests == [
        SubscribeRequest(
            Duration(10),
            "c",
            1,
            5,
            "SUBSCRIBE",
            (RequestedFilter("a/#", 2, 1), RequestedFilter("b", 0, 0)),
        ),
        SubscribeRequest(Duration(20), "c", 1, 6, "UNSUBSCRIBE", (RequestedFilter("a/#"),)),
    ]
    ever = {
        topic_filter
        for request in broker.subscribe_requests
        if request.kind == "SUBSCRIBE" and request.client == "c"
        for topic_filter in request.topic_filters
    }
    assert ever == {"a/#", "b"}


def test_next_subscribe_sees_a_later_subscribe_to_the_filter_without_consuming() -> None:
    broker = Broker(lambda: 0)
    session = broker.accept(("10.64.0.2", 4000), lambda data: None, lambda: None)
    session.on_data(CONNECT + SUBSCRIBE)
    later, to_b, to_c, to_all = (
        broker.next_subscribe(),
        broker.next_subscribe("b"),
        broker.next_subscribe("c"),
        broker.next_subscribe("#"),
    )
    assert later() is None and to_b() is None
    session.on_data(UNSUBSCRIBE)
    assert later() is None and to_b() is None
    session.on_data(SUBSCRIBE + SUBSCRIBE_TWO)
    assert later() is broker.subscribe_requests[2] and later() is broker.subscribe_requests[2]
    assert to_b() is broker.subscribe_requests[3]
    assert to_c() is None
    assert to_all() is None  # The filter is compared exactly: "#" would match "a/#" and "b".
    assert broker.next_subscribe()() is None
    to_a = broker.next_subscribe("a/#")
    session.on_data(b"\x82\x08\0\x07\0\x03a/b\x01")
    assert to_a() is None and broker.subscribe_requests[-1].topic_filters == ("a/b",)


@pytest.mark.parametrize(
    ("packet", "subscriptions"),
    [
        # a/b is valid, a# is not.
        (b"\x82\x0d\0\x07\0\x03a/b\x01\0\x02a#\x00", {"a/#": 1}),
        # a/# is valid, the byte 0xff is not UTF-8.
        (b"\xa2\x0a\0\x07\0\x03a/#\0\x01\xff", {"a/#": 1}),
    ],
    ids=["subscribe", "unsubscribe"],
)
def test_a_malformed_subscribe_or_unsubscribe_is_refused_whole(
    packet: bytes, subscriptions: dict[str, int]
) -> None:
    broker = Broker(lambda: 0)
    closed: list[bool] = []
    session = broker.accept(("10.64.0.2", 4000), lambda data: None, lambda: closed.append(True))
    session.on_data(KEEP + SUBSCRIBE + packet)
    assert closed == [True]
    assert [request.kind for request in broker.subscribe_requests] == ["SUBSCRIBE"]
    state = broker.session("c")
    assert state is not None and state.subscriptions == subscriptions


# A long wait on a busy history: as many records as polls, one poll after each. A poll that looked
# again at everything since the condition was made would read N * (N + 1) / 2 records, 50 M.
N = 10_000


def test_a_next_message_poll_reads_only_the_messages_since_the_last_one() -> None:
    broker = Broker(lambda: 0)
    broker.messages = history = CountingList[Message]()
    alarm = broker.next_message("dev/+/alarm", will=False)
    for n in range(N):
        broker.publish(f"dev/{n}/telemetry", "21.5")
        assert alarm() is None
    assert history.reads == N
    broker.publish("dev/7/alarm", "fire")
    found = alarm()
    assert found is not None and found.payload == b"fire"
    # Found, the condition reads only its record again, at every later poll.
    for _ in range(N):
        broker.publish("dev/8/alarm", "later")
        assert alarm() is found
    assert history.reads == 2 * N + 1
    assert broker.next_message("dev/8/alarm")() is None


def test_a_next_delivery_poll_reads_only_the_deliveries_since_the_last_one() -> None:
    broker = Broker(lambda: 0)
    broker.deliveries = history = CountingList[Delivery]()
    broker.accept(("10.64.0.2", 4000), lambda data: None, lambda: None).on_data(CONNECT + SUBSCRIBE)
    alarm = broker.next_delivery("a/alarm", "fire")
    for n in range(N):
        broker.publish("a/alarm" if n % 2 else "a/telemetry", "21.5")
        assert alarm() is None
    assert history.reads == N
    broker.publish("a/alarm", "fire")
    delivery = history[-1]
    assert alarm() is delivery and alarm() is delivery
    assert history.reads == N + 1 + 2


def test_a_next_subscribe_poll_reads_only_the_requests_since_the_last_one() -> None:
    broker = Broker(lambda: 0)
    broker.subscribe_requests = history = CountingList[SubscribeRequest]()
    session = broker.accept(("10.64.0.2", 4000), lambda data: None, lambda: None)
    session.on_data(CONNECT)
    to_b = broker.next_subscribe("b")
    for _ in range(N):
        session.on_data(SUBSCRIBE)
        assert to_b() is None
    assert history.reads == N
    session.on_data(b"\x82\x06\x00\x02\x00\x01b\x00")
    request = history[-1]
    assert to_b() is request and to_b() is request
    assert history.reads == N + 1 + 2
