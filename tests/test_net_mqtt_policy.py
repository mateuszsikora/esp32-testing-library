"""CONNECT policy through TCP packets and a verified in-memory TLS client."""

import contextlib
import ssl
from collections.abc import Callable, Iterable
from dataclasses import FrozenInstanceError

import pytest
from test_boardlink import MODEM, feed, started
from test_net_ip import tcp
from test_net_mqtt import CONNECT, SUBSCRIBE

from esp32tl.link.boardlink import Direction, Interface, Output
from esp32tl.link.protocol import Stop, Sync, UartTx
from esp32tl.net import Internet
from esp32tl.net.ip import Connection, Network, StreamService
from esp32tl.net.mqtt import Broker, ConnackCode, Connect
from esp32tl.net.tls import CertificateAuthority, Tls

WILL_CONNECT = b"\x10\x18\0\x04MQTT\x04\x2e\0\x01\0\x01c\0\x03a/b\0\x04gone"
PUBLISH = b"\x30\x09\0\x03a/bdata"


class WireClient:
    """One device TCP connection, optionally carrying TLS; no host sockets or broker internals."""

    def __init__(self, service: StreamService, ca: CertificateAuthority | None) -> None:
        self.packets: list[bytes] = []
        self.network = Network(self.packets.append)
        self.network.tcp[1883] = service
        self.network.receive(tcp(1, 0, 2))
        synack = self.packets.pop()
        assert synack[33] == 0x12
        self.seq, self.ack = 2, int.from_bytes(synack[24:28], "big") + 1
        self.network.receive(tcp(self.seq, self.ack, 0x10))
        self.closed = False
        self.ssl: ssl.SSLObject | None = None
        self.incoming, self.outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
        if ca is not None:
            context = ssl.create_default_context(cadata=ca.ca_pem)
            self.ssl = context.wrap_bio(self.incoming, self.outgoing, server_hostname="broker.test")
            for _ in range(20):
                with contextlib.suppress(ssl.SSLWantReadError):
                    self.ssl.do_handshake()
                self.incoming.write(self.exchange(self.outgoing.read()))
                if self.ssl.version():
                    break
            else:
                pytest.fail("TLS handshake did not complete")

    def exchange(self, data: bytes) -> bytes:
        seq = self.seq
        self.seq += len(data)
        self.network.receive(tcp(seq, self.ack, 0x18, data))
        result = bytearray()
        for _ in range(30):
            if not self.packets:
                return bytes(result)
            packets = self.packets[:]
            self.packets.clear()
            for packet in packets:
                payload = packet[20 + (packet[32] >> 4) * 4 :]
                fin = packet[33] & 1
                if payload or fin:
                    assert int.from_bytes(packet[24:28], "big") == self.ack
                    result += payload
                    self.ack += len(payload) + fin
                    self.closed |= bool(fin)
            self.network.receive(tcp(self.seq, self.ack, 0x10))
        pytest.fail("TCP replies did not drain")

    def send(self, data: bytes) -> bytes:
        if self.ssl is None:
            return self.exchange(data)
        self.ssl.write(data)
        self.incoming.write(self.exchange(self.outgoing.read()))
        reply = bytearray()
        with contextlib.suppress(ssl.SSLWantReadError):
            while chunk := self.ssl.read():
                reply += chunk
        return bytes(reply)

    def lose_transport(self) -> None:
        self.network.reset()


@pytest.fixture(params=[False, True], ids=["tcp", "tls"])
def connect_client(request: pytest.FixtureRequest) -> Callable[[Broker], WireClient]:
    ca = CertificateAuthority(["broker.test"]) if request.param else None

    def connect(broker: Broker) -> WireClient:
        return WireClient(Tls(broker, ca) if ca else broker, ca)

    return connect


@pytest.mark.parametrize(
    ("code", "wire_code"),
    [
        (ConnackCode.ACCEPTED, 0),
        (ConnackCode.UNACCEPTABLE_PROTOCOL_VERSION, 1),
        (ConnackCode.IDENTIFIER_REJECTED, 2),
        (ConnackCode.SERVER_UNAVAILABLE, 3),
        (ConnackCode.BAD_USERNAME_OR_PASSWORD, 4),
        (ConnackCode.NOT_AUTHORIZED, 5),
    ],
)
def test_connack_codes_and_rejection_isolation(
    connect_client: Callable[[Broker], WireClient], code: ConnackCode, wire_code: int
) -> None:
    broker = Broker(lambda: 42, connection_policy=lambda connect: code)
    attempt, connected = broker.next_connection_attempt(), broker.next_connection()
    assert attempt() is None and connected() is None
    client = connect_client(broker)
    # Rejection must also ignore packets pipelined after CONNECT, and a retained Will.
    reply = client.send(WILL_CONNECT + SUBSCRIBE + PUBLISH)
    assert reply[:4] == bytes([0x20, 2, 0, wire_code])
    result = attempt()
    assert result is not None and result == broker.connection_attempts[0]
    assert result.at.ns == 42 and result.return_code is code
    assert result.accepted is (code == ConnackCode.ACCEPTED)
    assert attempt() is result  # Polling does not consume the outcome.
    if result.accepted:
        assert connected() is not None and not client.closed
        assert broker.is_subscribed("a/#")()
    else:
        assert reply == bytes([0x20, 2, 0, wire_code]) and client.closed
        assert connected() is None and not broker.clients
        assert not broker.is_subscribed("a/#")() and broker.quiet_until() is None
        assert not broker.messages and not broker.retained
        client.send(PUBLISH)
        client.lose_transport()
        client.lose_transport()
        broker.poll()
        assert not broker.messages and not broker.retained
        assert len(broker.connection_attempts) == 1


def credential_connect(password: bytes) -> bytes:
    body = b"\0\x04MQTT\x04\xc2\0\x0a\0\x01c\0\x04user" + len(password).to_bytes(2) + password
    return bytes([0x10, len(body)]) + body


def test_retries_credentials_policy_changes_and_success(
    connect_client: Callable[[Broker], WireClient],
) -> None:
    now = [10]
    broker = Broker(lambda: now[0])
    seen: list[Connect] = []

    def policy(connect: Connect) -> ConnackCode:
        seen.append(connect)
        if len(seen) == 1:
            return ConnackCode.SERVER_UNAVAILABLE
        if (connect.username, connect.password) != ("user", b"right"):
            return ConnackCode.BAD_USERNAME_OR_PASSWORD
        return ConnackCode.NOT_AUTHORIZED

    broker.connection_policy = policy
    connected = broker.next_connection()
    for password, code in [
        (b"wrong", ConnackCode.SERVER_UNAVAILABLE),
        (b"wrong", ConnackCode.BAD_USERNAME_OR_PASSWORD),
        (b"right", ConnackCode.NOT_AUTHORIZED),
    ]:
        client = connect_client(broker)
        assert client.send(credential_connect(password)) == bytes([0x20, 2, 0, code])
        assert client.closed and connected() is None
        now[0] += 10
    assert not broker.clients and not broker.messages
    assert [(c.username, c.password) for c in seen] == [
        ("user", b"wrong"),
        ("user", b"wrong"),
        ("user", b"right"),
    ]
    next_attempt = broker.next_connection_attempt()
    broker.connection_policy = lambda connect: ConnackCode.ACCEPTED
    client = connect_client(broker)
    assert client.send(credential_connect(b"right")) == b"\x20\x02\0\0"
    assert connected() is broker.clients[0] and not client.closed
    assert next_attempt() is broker.connection_attempts[-1]
    assert [a.at.ns for a in broker.connection_attempts] == [10, 20, 30, 40]
    assert [a.return_code for a in broker.connection_attempts] == [
        ConnackCode.SERVER_UNAVAILABLE,
        ConnackCode.BAD_USERNAME_OR_PASSWORD,
        ConnackCode.NOT_AUTHORIZED,
        ConnackCode.ACCEPTED,
    ]
    assert client.send(SUBSCRIBE) == b"\x90\x03\0\x01\x01"
    assert client.send(PUBLISH) == PUBLISH
    assert broker.messages[-1].payload == b"data"
    # Changing the policy affects future CONNECTs, not this accepted session.
    broker.connection_policy = lambda connect: ConnackCode.NOT_AUTHORIZED
    assert client.send(b"\xc0\0") == b"\xd0\0"
    client.send(b"\xe0\0")
    broker.connection_policy = None
    default_client = connect_client(broker)
    assert default_client.send(credential_connect(b"unchecked")) == b"\x20\x02\0\0"
    assert broker.clients[-1].password == b"unchecked"


def test_rejected_takeover_keeps_old_client_and_will(
    connect_client: Callable[[Broker], WireClient],
) -> None:
    now = [10]
    broker = Broker(lambda: now[0])
    old = connect_client(broker)
    old.send(WILL_CONNECT + SUBSCRIBE)
    broker.connection_policy = lambda connect: ConnackCode.NOT_AUTHORIZED
    rejected = connect_client(broker)
    assert rejected.send(WILL_CONNECT) == b"\x20\x02\0\x05"
    rejected.lose_transport()
    assert not broker.messages and len(broker.clients) == 1
    assert broker.is_subscribed("a/#")()
    assert old.send(b"\xc0\0") == b"\xd0\0"
    now[0] = 20
    broker.connection_policy = None
    replacement = connect_client(broker)
    assert replacement.send(CONNECT) == b"\x20\x02\0\0"
    assert len(broker.clients) == 2
    assert [(m.payload, m.at.ns) for m in broker.messages] == [(b"gone", 20)]
    old.lose_transport()
    rejected.lose_transport()
    assert len(broker.messages) == 1


def test_policy_gets_immutable_wire_metadata_before_anonymous_id_allocation() -> None:
    seen: list[Connect] = []

    def policy(connect: Connect) -> ConnackCode:
        seen.append(connect)
        return ConnackCode.NOT_AUTHORIZED if len(seen) == 1 else ConnackCode.ACCEPTED

    broker = Broker(lambda: 123, connection_policy=policy)
    # Empty ID, CleanSession, retained QoS 1 Will and binary credentials.
    connect = b"\x10\x22\0\x04MQTT\x04\xee\0\x01\0\0\0\x03a/b\0\x04gone\0\x04user\0\x03\xff\0x"
    for port in (100, 101):
        session = broker.accept(("10.64.0.2", port), lambda b: None, lambda: None)
        for byte in connect:
            session.on_data(bytes([byte]))
    first = seen[0]
    assert first.at.ns == 123 and first.peer == ("10.64.0.2", 100)
    assert first.client_id == "" and first.clean_session and first.keepalive == 1
    assert first.username == "user" and first.password == b"\xff\0x"
    assert (first.will_topic, first.will_payload, first.will_qos, first.will_retain) == (
        "a/b",
        b"gone",
        1,
        True,
    )
    with pytest.raises(FrozenInstanceError):
        first.client_id = "changed"  # type: ignore[misc]
    assert len(broker.clients) == 1 and broker.clients[0].client_id == "esp32tl-anonymous-1"
    assert all(a.connect.client_id == "" for a in broker.connection_attempts)


def test_invalid_connect_never_calls_policy_or_records_an_outcome() -> None:
    def policy(connect: Connect) -> ConnackCode:
        pytest.fail("malformed CONNECT reached the policy")

    broker = Broker(lambda: 0, connection_policy=policy)
    malformed = WILL_CONNECT[:1] + b"\x19" + WILL_CONNECT[2:] + b"!"
    session = broker.accept(("10.64.0.2", 100), lambda b: None, lambda: None)
    session.on_data(malformed)
    assert not broker.connection_attempts and not broker.clients and not broker.messages


def test_empty_id_rejection_precedes_policy_and_has_an_outcome() -> None:
    def policy(connect: Connect) -> ConnackCode:
        pytest.fail("empty non-clean ID reached the policy")

    broker = Broker(lambda: 77, connection_policy=policy)
    sent: list[bytes] = []
    session = broker.accept(("10.64.0.2", 100), sent.append, lambda: None)
    session.on_data(b"\x10\x0c\0\x04MQTT\x04\0\0\0\0\0")
    assert sent == [b"\x20\x02\0\x02"]
    assert broker.connection_attempts[0].return_code is ConnackCode.IDENTIFIER_REJECTED
    assert broker.connection_attempts[0].at.ns == 77
    assert not broker.clients


@pytest.mark.parametrize("invalid", [None, True, 0, 5, 6, 0x87, "ACCEPTED"])
def test_invalid_policy_return_is_a_test_error(invalid: object) -> None:
    broker = Broker(lambda: 0, connection_policy=lambda connect: invalid)  # type: ignore[arg-type, return-value]
    sent: list[bytes] = []
    closed: list[bool] = []
    session = broker.accept(("10.64.0.2", 100), sent.append, lambda: closed.append(True))
    with pytest.raises(TypeError, match="MQTT connection_policy must return a ConnackCode"):
        session.on_data(WILL_CONNECT)
    assert closed == [True] and not sent
    assert not broker.connection_attempts and not broker.clients and not broker.messages


@pytest.mark.parametrize("error", [ValueError("policy failed"), RuntimeError("policy failed")])
def test_policy_exceptions_reach_the_test(error: Exception) -> None:
    def policy(connect: Connect) -> ConnackCode:
        raise error

    broker = Broker(lambda: 0, connection_policy=policy)
    closed: list[bool] = []
    session = broker.accept(("10.64.0.2", 100), lambda b: None, lambda: closed.append(True))
    with pytest.raises(type(error), match="policy failed"):
        session.on_data(WILL_CONNECT)
    assert closed == [True] and not broker.connection_attempts
    assert not broker.clients and not broker.messages


def test_pytest_fail_in_a_policy_closes_the_connection_and_raises_as_an_exception() -> None:
    broker = Broker(lambda: 0, connection_policy=lambda connect: pytest.fail("bad credentials"))
    closed: list[bool] = []
    session = broker.accept(("10.64.0.2", 100), lambda b: None, lambda: closed.append(True))
    with pytest.raises(RuntimeError) as raised:
        session.on_data(WILL_CONNECT)
    assert str(raised.value) == "MQTT connection_policy raised Failed: bad credentials"
    assert isinstance(raised.value.__cause__, pytest.fail.Exception)
    assert closed == [True] and not broker.connection_attempts and not broker.clients


class MqttWire(Interface):
    """A modem whose UART carries one MQTT connection, so a policy runs in the link's turn."""

    def __init__(self, session: Connection) -> None:
        super().__init__("modem", MODEM)
        self.session = session

    def receive(self, t: int, direction: Direction, data: bytes) -> Iterable[Output]:
        self.session.on_data(data)
        return []


@pytest.mark.parametrize(
    ("policy", "error"),
    [
        (lambda connect: 3, "TypeError: MQTT connection_policy must return a ConnackCode"),
        (lambda connect: 1 / 0, "ZeroDivisionError: division by zero"),
        (
            lambda connect: pytest.fail("bad credentials"),
            "RuntimeError: MQTT connection_policy raised Failed: bad credentials",
        ),
    ],
)
def test_policy_errors_in_a_board_run_fail_the_link_naming_them(
    policy: Callable[[Connect], ConnackCode], error: str
) -> None:
    broker = Broker(lambda: 0, connection_policy=policy)
    link = started(MqttWire(broker.accept(("10.64.0.2", 100), lambda b: None, lambda: None)))
    connect = UartTx(1, 10, 1, (26,), 115200, WILL_CONNECT)
    assert feed(link, connect, Sync(20)) == [Stop()]
    assert link.error == f"modem.receive raised {error}"
    assert link.error_traceback is not None and "connection_policy" in link.error_traceback
    assert not broker.connection_attempts and not broker.clients


def test_internet_reset_keeps_the_policy_and_both_histories() -> None:
    internet = Internet(lambda: 5)
    broker = internet.broker
    ca = CertificateAuthority(["broker.test"])
    tls = internet.listen(8883, broker, tls=ca)

    def policy(connect: Connect) -> ConnackCode:
        return ConnackCode.NOT_AUTHORIZED

    broker.connection_policy = policy
    assert WireClient(internet.ip.tcp[1883], None).send(CONNECT) == b"\x20\x02\0\x05"
    internet.start(10)
    internet.stop(20)
    assert broker.connection_policy is policy
    assert WireClient(tls, ca).send(CONNECT) == b"\x20\x02\0\x05"
    broker.connection_policy = None
    assert WireClient(internet.ip.tcp[1883], None).send(CONNECT) == b"\x20\x02\0\0"
    internet.stop(30)
    assert broker.connection_policy is None
    assert [a.accepted for a in broker.connection_attempts] == [False, False, True]
    assert [c.client_id for c in broker.clients] == ["c"]
    assert any(e.get("stream") == "1883/1" for e in internet.events)
