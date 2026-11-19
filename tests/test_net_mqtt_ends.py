"""How and when each MQTT connection ended (issue #274), in raw MQTT 3.1.1 packets."""

import pytest
import test_net_mqtt_failures as failures
from test_net_ip import tcp

from esp32tl.net import Internet
from esp32tl.net.ip import Connection
from esp32tl.net.mqtt import Broker, ConnectionEnd
from esp32tl.net.tls import CertificateAuthority
from esp32tl.units import Duration

# Client ID c, CleanSession=1, keepalive 1 s, a Will of `gone`, QoS 1 and retained, on a/b.
WILL_CONNECT = b"\x10\x18\0\x04MQTT\x04\x2e\0\x01\0\x01c\0\x03a/b\0\x04gone"
DISCONNECT = b"\xe0\0"
S = 1_000_000_000


def connected(broker: Broker, port: int = 100) -> tuple[Connection, list[bool]]:
    closed: list[bool] = []
    session = broker.accept(("10.0.0.2", port), lambda b: None, lambda: closed.append(True))
    session.on_data(WILL_CONNECT)
    assert broker.clients[-1].connection_id == session.connection_id
    return session, closed


MALFORMED = {
    "refused": b"\xc0\x01\0",  # a PINGREQ with a payload
    "five-byte-length": b"\x30\xff\xff\xff\xff\x01",
    "length-over-1-mib": b"\x30\x81\x80\x80\x01",
}


@pytest.mark.parametrize(
    ("case", "by", "will_sent"),
    [
        ("disconnect", "disconnect", False),
        ("keepalive", "keepalive", True),
        ("takeover", "takeover", True),
        ("closed", "closed", True),
        ("refused", "malformed", True),
        ("five-byte-length", "malformed", True),
        ("length-over-1-mib", "malformed", True),
        ("test", "test", True),
    ],
)
def test_each_way_a_connection_ends_has_its_own_cause(case: str, by: str, will_sent: bool) -> None:
    now = [0]
    lines: list[str] = []
    broker = Broker(lambda: now[0], lines.append)
    session, closed = connected(broker)
    ending = broker.next_end()
    assert ending() is None and broker.ended(1) is None
    now[0] = S
    if case == "disconnect":
        session.on_data(DISCONNECT)
    elif case == "keepalive":
        now[0] = 3 * S // 2
        broker.poll()
    elif case == "takeover":
        broker.accept(("10.0.0.2", 101), lambda b: None, lambda: None).on_data(WILL_CONNECT)
    elif case == "closed":
        session.on_close()
    elif case in MALFORMED:
        session.on_data(MALFORMED[case])
    else:
        assert broker.disconnect("c")
    at = Duration(3 * S // 2 if case == "keepalive" else S)
    end = ConnectionEnd(at, "c", 1, by, will_sent)  # type: ignore[arg-type]
    assert ending() == end and broker.ended(1) == end
    assert broker.connection_ends == [end] and closed == [True]
    assert [m.at for m in broker.messages if m.will] == ([at] if will_sent else [])
    # The stream's own close, which follows the broker's, changes nothing.
    session.on_close()
    assert broker.connection_ends == [end]
    if case == "disconnect":
        assert "mqtt: c sent DISCONNECT" in lines


def test_the_end_is_recorded_before_the_will_is_published() -> None:
    seen: list[tuple[str, int]] = []
    broker = Broker(lambda: 0, lambda line: seen.append((line, len(broker.connection_ends))))
    session, _ = connected(broker)
    session.on_close()
    # The Will's routing logs its line: the end is in the history by then.
    assert ("mqtt: c → a/b b'gone'", 1) in seen


def test_the_tls_layer_closing_under_a_session_ends_it_as_closed() -> None:
    wire = failures.Wire()
    net, broker = wire.net, wire.net.broker
    ca = CertificateAuthority(["broker.test"])
    net.listen(8883, broker, tls=ca)
    peer = failures.Peer(wire, 4000, ca)
    peer.send(WILL_CONNECT)
    assert peer.read() == b"\x20\x02\0\0"
    # An application record that does not decrypt: a fatal alert, and TLS closes under MQTT.
    wire.pump(peer.segment(0x18, b"\x17\x03\x03\x00\x20" + bytes(32)))
    assert broker.connection_ends == [ConnectionEnd(Duration(0), "c", 1, "closed", True)]


def test_a_takeover_that_discards_the_will_says_so() -> None:
    broker = Broker(lambda: 0)
    keep = WILL_CONNECT[:9] + b"\x2c" + WILL_CONNECT[10:]  # CleanSession=0
    broker.accept(("10.0.0.2", 100), lambda b: None, lambda: None).on_data(keep)
    broker.accept(("10.0.0.2", 101), lambda b: None, lambda: None).on_data(keep)
    assert broker.connection_ends == [ConnectionEnd(Duration(0), "c", 1, "takeover", False)]
    assert broker.messages == []


def test_a_connection_never_accepted_records_no_end() -> None:
    broker = Broker(lambda: 0)
    for data in (b"\x10\x01\0", b"\x10\x0c\0\x04MQTT\x04\x00\0\x0a\0\0"):  # bad; empty ID, kept
        session = broker.accept(("10.0.0.2", 100), lambda b: None, lambda: None)
        session.on_data(data)
        assert session.closed
    assert broker.connection_ends == [] and not broker.clients
    assert len(broker.connection_attempts) == 1


def test_next_end_keeps_one_client_id_and_consumes_nothing() -> None:
    now = [0]
    broker = Broker(lambda: now[0])
    first, _ = connected(broker)
    other = broker.accept(("10.0.0.2", 101), lambda b: None, lambda: None)
    other.on_data(WILL_CONNECT[:14] + b"d" + WILL_CONNECT[15:])
    of_c, of_d = broker.next_end("c"), broker.next_end("d")
    assert "of 'd'" in str(of_d) and str(broker.next_end()) == "the next MQTT connection end"
    other.on_data(DISCONNECT)
    assert of_c() is None
    ended = of_d()
    assert ended is not None and ended.by == "disconnect" and ended.connection_id == 2
    now[0] = 5
    first.on_close()
    assert of_d() is ended
    found = of_c()
    assert found is not None and found.by == "closed" and found.at == Duration(5)
    assert of_c() is found


def test_a_lost_link_ends_the_connection_at_its_keepalive_with_the_will() -> None:
    now = [10]
    net = Internet(lambda: now[0])
    net.ip.receive(tcp(1, 0, 2))
    net.ip.receive(tcp(2, 65537, 0x10))
    net.ip.receive(tcp(2, 65537, 0x18, WILL_CONNECT))
    net.stop(500)
    assert net.broker.connection_ends == []
    expiry = 10 + 3 * S // 2
    net.poll(expiry)
    end = net.broker.ended(1)
    assert end == ConnectionEnd(Duration(expiry), "c", 1, "keepalive", True)


def test_a_reset_of_the_network_ends_the_connection_as_closed() -> None:
    now = [10]
    net = Internet(lambda: now[0])
    net.ip.receive(tcp(1, 0, 2))
    net.ip.receive(tcp(2, 65537, 0x10))
    net.ip.receive(tcp(2, 65537, 0x18, WILL_CONNECT))
    now[0] = 20
    net.ip.reset()
    assert net.broker.connection_ends == [ConnectionEnd(Duration(20), "c", 1, "closed", True)]
