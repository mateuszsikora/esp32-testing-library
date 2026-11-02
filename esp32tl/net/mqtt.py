"""An MQTT 3.1.1 broker the test runs: QoS 0 and 1, retained messages, the Will, wildcards. Every
message it is given is kept, stamped with the board's time, for the test to look at; the test can
publish too.
"""

import struct
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import IntEnum
from typing import Generic, TypeVar

from esp32tl.units import Duration

CONNECT, CONNACK, PUBLISH, PUBACK, PUBREC, PUBREL, PUBCOMP = 1, 2, 3, 4, 5, 6, 7
SUBSCRIBE, SUBACK, UNSUBSCRIBE, UNSUBACK, PINGREQ, PINGRESP, DISCONNECT = 8, 9, 10, 11, 12, 13, 14

T = TypeVar("T")


@dataclass(frozen=True, repr=False)
class _Condition(Generic[T]):
    find: Callable[[], T]
    description: str

    def __call__(self) -> T:
        return self.find()

    def __repr__(self) -> str:
        return self.description

    def __bool__(self) -> bool:
        raise TypeError("call the MQTT condition with (), or pass it to board.wait_until")


@dataclass(frozen=True)
class Message:
    at: Duration
    client: str
    topic: str
    payload: bytes
    qos: int
    retain: bool

    @property
    def text(self) -> str:
        return self.payload.decode(errors="replace")


@dataclass(frozen=True)
class Client:
    at: Duration
    client_id: str
    username: str | None
    password: bytes | None
    keepalive: int
    will_topic: str | None


class ConnackCode(IntEnum):
    """MQTT 3.1.1 CONNECT return codes, not MQTT 5 reason codes."""

    ACCEPTED = 0
    UNACCEPTABLE_PROTOCOL_VERSION = 1
    IDENTIFIER_REJECTED = 2
    SERVER_UNAVAILABLE = 3
    BAD_USERNAME_OR_PASSWORD = 4
    NOT_AUTHORIZED = 5


@dataclass(frozen=True)
class Connect:
    """A parsed CONNECT, with the client ID exactly as sent by the device."""

    at: Duration
    peer: tuple[str, int]
    client_id: str
    clean_session: bool
    username: str | None
    password: bytes | None
    keepalive: int
    will_topic: str | None
    will_payload: bytes | None
    will_qos: int
    will_retain: bool


@dataclass(frozen=True)
class ConnectionAttempt:
    connect: Connect
    return_code: ConnackCode

    @property
    def at(self) -> Duration:
        return self.connect.at

    @property
    def accepted(self) -> bool:
        return self.return_code == ConnackCode.ACCEPTED


def matches(pattern: str, topic: str) -> bool:
    if topic.startswith("$") and pattern[:1] in ("#", "+"):
        return False
    p, t = pattern.split("/"), topic.split("/")
    for i, part in enumerate(p):
        if part == "#":
            return i == len(p) - 1
        if i >= len(t) or (part != "+" and part != t[i]):
            return False
    return len(p) == len(t)


def _string(data: bytes, at: int) -> tuple[str, int]:
    raw, end = _blob(data, at)
    text = raw.decode("utf-8")
    if "\0" in text:
        raise ValueError("NUL in MQTT string")
    return text, end


def _blob(data: bytes, at: int) -> tuple[bytes, int]:
    (length,) = struct.unpack(">H", data[at : at + 2])
    if at + 2 + length > len(data):
        raise ValueError("truncated MQTT string")
    return data[at + 2 : at + 2 + length], at + 2 + length


def _packet(kind: int, flags: int, body: bytes) -> bytes:
    length, encoded = len(body), bytearray()
    while True:
        byte, length = length % 128, length // 128
        encoded.append(byte | (0x80 if length else 0))
        if not length:
            break
    return bytes([(kind << 4) | flags]) + bytes(encoded) + body


class Broker:
    """`now` is the board's clock. The `StreamService` of `esp32tl.net.ip`."""

    def __init__(
        self,
        now: Callable[[], int],
        log: Callable[[str], None] = lambda line: None,
        *,
        connection_policy: Callable[[Connect], ConnackCode] | None = None,
    ) -> None:
        self._now = now
        self._log = log
        self.messages: list[Message] = []
        self.clients: list[Client] = []
        self.connection_attempts: list[ConnectionAttempt] = []
        self.connection_policy = connection_policy
        self.retained: dict[str, Message] = {}
        self._sessions: list[_Session] = []
        self._anonymous = 0

    def accept(
        self, peer: tuple[str, int], write: Callable[[bytes], None], close: Callable[[], None]
    ) -> "_Session":
        session = _Session(self, peer, write, close)
        self._sessions.append(session)
        return session

    def _anonymous_id(self) -> str:
        """Allocate a repeatable ID that no earlier accepted CONNECT in this broker used."""
        used = {client.client_id for client in self.clients}
        while True:
            self._anonymous += 1
            candidate = f"esp32tl-anonymous-{self._anonymous}"
            if candidate not in used:
                return candidate

    # The test's side.

    def message(
        self, topic: str = "#", payload: bytes | str | None = None
    ) -> Callable[[], Message | None]:
        """Compatibility name for `next_message`; earlier messages remain excluded."""
        return self.next_message(topic, payload)

    def next_message(
        self, topic: str = "#", payload: bytes | str | None = None
    ) -> Callable[[], Message | None]:
        """A non-consuming condition for the first match appended after this call.

        `topic` accepts MQTT wildcards; `payload` is bytes or UTF-8 text. The boundary is
        append order, even at equal timestamps. Polling never advances that boundary.
        """
        start = len(self.messages)
        wanted = payload.encode() if isinstance(payload, str) else payload
        description = f"the next MQTT message on {topic!r}" + (
            f" saying {wanted!r}" if wanted is not None else ""
        )
        return _Condition(lambda: self._find_message(topic, wanted, start), description)

    def find_message(self, topic: str = "#", payload: bytes | str | None = None) -> Message | None:
        """Return the first match in recorded history now, or None; consume nothing."""
        wanted = payload.encode() if isinstance(payload, str) else payload
        return self._find_message(topic, wanted, 0)

    def _find_message(self, topic: str, wanted: bytes | None, start: int) -> Message | None:
        for message in self.messages[start:]:
            if matches(topic, message.topic) and (wanted is None or message.payload == wanted):
                return message
        return None

    def subscribed(self, topic: str) -> Callable[[], bool]:
        """A live condition: an active client currently subscribes to exactly `topic`."""

        def find() -> bool:
            return any(topic in session.subscriptions for session in self._sessions)

        return _Condition(find, f"an active MQTT subscription to {topic!r}")

    def connected(self) -> Callable[[], Client | None]:
        """Compatibility name for `next_connection`; earlier connections remain excluded."""
        return self.next_connection()

    def next_connection(self) -> Callable[[], Client | None]:
        """A non-consuming condition for the first CONNECT recorded after this call."""
        start = len(self.clients)

        def find() -> Client | None:
            return self.clients[start] if len(self.clients) > start else None

        return _Condition(find, "the next MQTT client connection")

    def find_connection(self) -> Client | None:
        """Return the first recorded CONNECT, even if it has closed, or None."""
        return self.clients[0] if self.clients else None

    def connection_attempt(self) -> Callable[[], ConnectionAttempt | None]:
        """A non-consuming condition for the first CONNECT outcome recorded after this call,
        accepted or rejected."""
        start = len(self.connection_attempts)

        def find() -> ConnectionAttempt | None:
            return (
                self.connection_attempts[start] if len(self.connection_attempts) > start else None
            )

        return _Condition(find, "the next MQTT connection attempt")

    def publish(
        self, topic: str, payload: bytes | str, *, retain: bool = False, qos: int = 0
    ) -> None:
        if not topic or "+" in topic or "#" in topic or "\0" in topic:
            raise ValueError("MQTT publish needs a topic without wildcards or NUL")
        if qos not in (0, 1):
            raise ValueError("MQTT supports QoS 0 and 1")
        data = payload.encode() if isinstance(payload, str) else payload
        self._route(Message(Duration(self._now()), "(test)", topic, data, qos, retain))

    def poll(self) -> None:
        for session in list(self._sessions):
            if session.deadline is not None and self._now() >= session.deadline:
                self._log(f"mqtt: {session.client_id} keepalive expired")
                session.on_close()

    def quiet_until(self) -> int | None:
        deadlines = [s.deadline for s in self._sessions if s.deadline is not None]
        return min(deadlines, default=None)

    # Routing.

    def _route(self, message: Message) -> None:
        self.messages.append(message)
        self._log(f"mqtt: {message.client} → {message.topic} {message.payload[:60]!r}")
        if message.retain:
            if message.payload:
                self.retained[message.topic] = message
            else:
                self.retained.pop(message.topic, None)
        for session in list(self._sessions):
            session.deliver(message)


class _Session:
    def __init__(
        self,
        broker: Broker,
        peer: tuple[str, int],
        write: Callable[[bytes], None],
        close: Callable[[], None],
    ) -> None:
        self.broker = broker
        self.peer = peer
        self.write = write
        self.close = close
        self.buffer = bytearray()
        self.client_id = "?"
        self.subscriptions: dict[str, int] = {}
        self.will: Message | None = None
        self.packet_id = 0
        self.closed = False
        self.connected = False
        self.keepalive = 0
        self.deadline: int | None = None
        self.unacked: set[int] = set()

    def deliver(self, message: Message, retained: bool = False) -> None:
        granted = [q for f, q in self.subscriptions.items() if matches(f, message.topic)]
        if not granted or self.closed:
            return
        qos = min(max(granted), message.qos)
        body = struct.pack(">H", len(message.topic.encode())) + message.topic.encode()
        if qos:
            if len(self.unacked) == 65535:
                self.broker._log(f"mqtt: {self.client_id} exhausted packet identifiers")
                self.on_close()
                return
            self.packet_id = self.packet_id % 0xFFFF + 1
            while self.packet_id in self.unacked:
                self.packet_id = self.packet_id % 0xFFFF + 1
            self.unacked.add(self.packet_id)
            body += struct.pack(">H", self.packet_id)
        flags = (qos << 1) | (1 if retained else 0)
        self.write(_packet(PUBLISH, flags, body + message.payload))

    def on_data(self, data: bytes) -> None:
        if self.closed:
            return
        self.buffer += data
        while True:
            if len(self.buffer) < 2:
                return
            length, multiplier, at = 0, 1, 1
            while True:
                if at > 4:
                    self.on_close()
                    return
                if at >= len(self.buffer):
                    return
                byte = self.buffer[at]
                length += (byte & 0x7F) * multiplier
                multiplier *= 128
                at += 1
                if not byte & 0x80:
                    break
            if length > 1024 * 1024:
                self.on_close()
                return
            if len(self.buffer) < at + length:
                return
            first, body = self.buffer[0], bytes(self.buffer[at : at + length])
            del self.buffer[: at + length]
            connect = None
            try:
                if first >> 4 == CONNECT:
                    connect = self._parse_connect(first & 0x0F, body)
                else:
                    self._handle(first >> 4, first & 0x0F, body)
            except (ValueError, IndexError, struct.error):
                self.broker._log("mqtt: malformed or unsupported packet, closing")
                self.on_close()
            if connect is not None:
                # Test policy errors must reach the test, not become malformed wire input.
                self._connect(connect)
            if self.closed:
                return
            if self.keepalive:
                self.deadline = self.broker._now() + self.keepalive * 1_500_000_000

    def _parse_connect(self, flags: int, body: bytes) -> Connect:
        protocol, at = _string(body, 0)
        if flags or self.connected or protocol != "MQTT" or body[at] != 4:
            raise ValueError("expected one MQTT 3.1.1 CONNECT")
        connect_flags = body[at + 1]
        (keepalive,) = struct.unpack(">H", body[at + 2 : at + 4])
        if connect_flags & 1 or ((connect_flags >> 3) & 3) > 1:
            raise ValueError("unsupported CONNECT flags or Will QoS")
        if not connect_flags & 4 and connect_flags & 0x38:
            raise ValueError("Will flags without a Will")
        client_id, at = _string(body, at + 4)
        will_topic = will_payload = None
        if connect_flags & 0x04:
            will_topic, at = _string(body, at)
            if not will_topic or "+" in will_topic or "#" in will_topic:
                raise ValueError("invalid Will topic")
            will_payload, at = _blob(body, at)
        username = password = None
        if connect_flags & 0x80:
            username, at = _string(body, at)
        if connect_flags & 0x40:
            password, at = _blob(body, at)
        if at != len(body):
            raise ValueError("trailing CONNECT data")
        return Connect(
            at=Duration(self.broker._now()),
            peer=self.peer,
            client_id=client_id,
            clean_session=bool(connect_flags & 2),
            username=username,
            password=password,
            keepalive=keepalive,
            will_topic=will_topic,
            will_payload=will_payload,
            will_qos=(connect_flags >> 3) & 3,
            will_retain=bool(connect_flags & 0x20),
        )

    def _connect(self, connect: Connect) -> None:
        broker = self.broker
        code = ConnackCode.ACCEPTED
        if not connect.client_id and not connect.clean_session:
            code = ConnackCode.IDENTIFIER_REJECTED
        elif broker.connection_policy is not None:
            try:
                code = broker.connection_policy(connect)
                if not isinstance(code, ConnackCode):
                    raise TypeError("MQTT connection_policy must return a ConnackCode")
            except Exception:
                self._end()
                raise
        broker.connection_attempts.append(ConnectionAttempt(connect, code))
        if code != ConnackCode.ACCEPTED:
            broker._log(f"mqtt: {connect.client_id!r} rejected: {code.name} ({code.value})")
            self.write(_packet(CONNACK, 0, bytes([0, code])))
            self._end()
            return

        self.client_id = connect.client_id or broker._anonymous_id()
        if connect.will_topic is not None:
            assert connect.will_payload is not None
            self.will = Message(
                connect.at,
                self.client_id,
                connect.will_topic,
                connect.will_payload,
                connect.will_qos,
                connect.will_retain,
            )
        for session in list(broker._sessions):
            if session is not self and session.connected and session.client_id == self.client_id:
                session.on_close()
        self.connected = True
        self.keepalive = connect.keepalive
        broker.clients.append(
            Client(
                connect.at,
                self.client_id,
                connect.username,
                connect.password,
                connect.keepalive,
                connect.will_topic,
            )
        )
        broker._log(f"mqtt: {self.client_id} connected")
        self.write(_packet(CONNACK, 0, bytes([0, code])))

    def _handle(self, kind: int, flags: int, body: bytes) -> None:
        if kind != PUBLISH and flags != (2 if kind in (SUBSCRIBE, UNSUBSCRIBE) else 0):
            raise ValueError("invalid MQTT flags")
        if not self.connected:
            raise ValueError("CONNECT must come first")
        broker = self.broker
        now = Duration(broker._now())
        if kind == PUBLISH:
            qos, retain = (flags >> 1) & 3, bool(flags & 1)
            topic, at = _string(body, 0)
            if not topic or "+" in topic or "#" in topic or qos > 1:
                raise ValueError("unsupported PUBLISH topic or QoS")
            packet_id = None
            if qos:
                (packet_id,) = struct.unpack(">H", body[at : at + 2])
                at += 2
                if not packet_id:
                    raise ValueError("zero packet id")
            broker._route(Message(now, self.client_id, topic, body[at:], qos, retain))
            if qos == 1:
                self.write(_packet(PUBACK, 0, struct.pack(">H", packet_id)))

        elif kind == SUBSCRIBE:
            packet_id, at = body[:2], 2
            if len(body) < 5 or packet_id == b"\0\0":
                raise ValueError("empty SUBSCRIBE or zero packet id")
            granted = bytearray()
            filters = []
            while at < len(body):
                topic, at = _string(body, at)
                parts = topic.split("/")
                if not topic or any(
                    ("#" in part and (part != "#" or i != len(parts) - 1))
                    or ("+" in part and part != "+")
                    for i, part in enumerate(parts)
                ):
                    raise ValueError("invalid topic filter")
                qos = body[at]
                if qos > 2:
                    raise ValueError("invalid subscription QoS")
                qos = min(qos, 1)
                at += 1
                self.subscriptions[topic] = qos
                granted.append(qos)
                filters.append(topic)
            broker._log(f"mqtt: {self.client_id} subscribed to {', '.join(filters)}")
            self.write(_packet(SUBACK, 0, packet_id + bytes(granted)))
            for message in broker.retained.values():
                if any(matches(f, message.topic) for f in filters):
                    self.deliver(message, retained=True)
        elif kind == UNSUBSCRIBE:
            if len(body) < 5 or body[:2] == b"\0\0":
                raise ValueError("empty UNSUBSCRIBE or zero packet id")
            at = 2
            while at < len(body):
                topic, at = _string(body, at)
                self.subscriptions.pop(topic, None)
            self.write(_packet(UNSUBACK, 0, body[:2]))
        elif kind == PINGREQ:
            if body:
                raise ValueError("PINGREQ payload")
            self.write(_packet(PINGRESP, 0, b""))
        elif kind == DISCONNECT:
            if body:
                raise ValueError("DISCONNECT payload")
            self.will = None
            self._end()
        elif kind == PUBACK:
            if len(body) != 2:
                raise ValueError("invalid PUBACK")
            self.unacked.discard(int.from_bytes(body, "big"))
        else:
            raise ValueError("unsupported MQTT packet")

    def on_close(self) -> None:
        will, self.will = self.will, None
        self._end()
        if will is not None and self.connected:
            self.broker._route(replace(will, at=Duration(self.broker._now())))

    def _end(self) -> None:
        if not self.closed:
            self.closed = True
            self.deadline = None
            if self in self.broker._sessions:
                self.broker._sessions.remove(self)
            self.close()
