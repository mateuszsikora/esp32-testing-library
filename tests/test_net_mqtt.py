"""MQTT wire dialogue, independent of MQTT packet builders in the implementation."""

import pytest

from esp32tl.net.mqtt import Broker, matches

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


@pytest.mark.parametrize("legacy", [False, True])
def test_history_and_next_events_use_append_order_without_consuming(legacy: bool) -> None:
    broker = Broker(lambda: 123)
    message_condition = broker.message if legacy else broker.next_message
    connection_condition = broker.connected if legacy else broker.next_connection
    assert broker.find_message() is None and broker.find_connection() is None
    waiting = message_condition("a/+", "new")
    connected = connection_condition()
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
    next_message, next_client = message_condition("a/+", "new"), connection_condition()
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
        ("message", ("a/+", "new"), "the next MQTT message on 'a/+' saying b'new'"),
        ("next_message", (), "the next MQTT message on '#'"),
        ("connected", (), "the next MQTT client connection"),
        ("next_connection", (), "the next MQTT client connection"),
        ("connection_attempt", (), "the next MQTT connection attempt"),
        ("subscribed", ("a/#",), "an active MQTT subscription to 'a/#'"),
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
