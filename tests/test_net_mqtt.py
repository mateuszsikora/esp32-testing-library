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
    connected = broker.connected()
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
    waiting = broker.message("a/+", "new")
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


def test_qos_one_id_exhaustion_closes_instead_of_reusing_an_unacknowledged_id() -> None:
    broker = Broker(lambda: 0)
    session = broker.accept(("10.0.0.2", 100), lambda b: None, lambda: None)
    session.on_data(CONNECT + SUBSCRIBE)
    session.unacked.update(range(1, 65536))
    broker.publish("a/b", "test", qos=1)
    assert session.closed


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
