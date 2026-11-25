"""An MQTT 3.1.1 broker the test runs: QoS 0 and 1, retained messages, the Will, wildcards, and
sessions kept by client ID. Every message it is given and every one it sends is kept, stamped with
the board's time, for the test to look at; the test can publish too.
"""

import struct
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from enum import IntEnum
from typing import Literal, TypeVar

from esp32tl.interfaces.condition import Condition, FirstFrom
from esp32tl.interfaces.history import History
from esp32tl.units import Duration

TEST_CLIENT = "(test)"
"""The `client` of every message `Broker.publish` records: the test's own publications."""

CONNECT, CONNACK, PUBLISH, PUBACK, PUBREC, PUBREL, PUBCOMP = 1, 2, 3, 4, 5, 6, 7
SUBSCRIBE, SUBACK, UNSUBSCRIBE, UNSUBACK, PINGREQ, PINGRESP, DISCONNECT = 8, 9, 10, 11, 12, 13, 14


@dataclass(frozen=True)
class Message:
    """One publication in the broker's history.

    `client` is the client ID it came from, `TEST_CLIENT` for the test's own. `will` is `True` for
    a Will the broker published when that client's connection was lost, `False` for a PUBLISH the
    client sent and for the test's publications.
    """

    at: Duration
    client: str
    topic: str
    payload: bytes
    qos: int
    retain: bool
    connection_id: int | None = None
    packet_id: int | None = None
    dup: bool = False
    publication: "Publication | None" = field(default=None, compare=False, repr=False)
    will: bool = False

    @property
    def text(self) -> str:
        return self.payload.decode(errors="replace")


@dataclass(frozen=True)
class MqttClient:
    at: Duration
    client_id: str
    username: str | None
    password: bytes | None
    keepalive: int
    will_topic: str | None
    connection_id: int = 0
    session_present: bool = False


PacketKind = Literal[
    "CONNECT", "PUBLISH", "PUBACK", "SUBSCRIBE", "UNSUBSCRIBE", "PINGREQ", "DISCONNECT"
]
_PACKET_KINDS: dict[int, PacketKind] = {
    CONNECT: "CONNECT",
    PUBLISH: "PUBLISH",
    PUBACK: "PUBACK",
    SUBSCRIBE: "SUBSCRIBE",
    UNSUBSCRIBE: "UNSUBSCRIBE",
    PINGREQ: "PINGREQ",
    DISCONNECT: "DISCONNECT",
}


@dataclass(frozen=True)
class ReceivedPacket:
    """A client control packet, stamped when its last byte reached the broker."""

    at: Duration
    connection_id: int
    client_id: str
    kind: PacketKind


EndCause = Literal["disconnect", "keepalive", "takeover", "closed", "malformed", "test"]


@dataclass(frozen=True)
class ConnectionEnd:
    """How and when this connection ended, on the board's time.

    One for each accepted connection, the one of `MqttClient` with the same `connection_id`, once it
    has ended. `by` says how: `"disconnect"` the client's DISCONNECT, `"keepalive"` 1.5 times its
    keepalive with nothing from it, `"takeover"` a CONNECT with the same client ID on another
    connection, `"closed"` its stream closing under it (the device's FIN or RST, `ip.reset`, the TLS
    layer under it closing, the network's TCP giving up after 15 retransmissions), `"malformed"` a
    packet the broker could not take, `"test"` `Broker.disconnect`. `will_sent` is whether the
    broker published the connection's Will then.
    """

    at: Duration
    client_id: str
    connection_id: int
    by: EndCause
    will_sent: bool


@dataclass
class Delivery:
    """One PUBLISH the broker wrote to a client's stream, a retransmission included.

    `acknowledged_at` is when this PUBLISH's PUBACK reached the broker: it stays `None` for QoS 0,
    and for a delivery whose connection closed first, whose retransmission has a record of its own.
    """

    at: Duration
    client: str
    connection_id: int
    topic: str
    payload: bytes
    qos: int
    packet_id: int | None
    dup: bool
    retain: bool
    acknowledged_at: Duration | None = None

    @property
    def text(self) -> str:
        return self.payload.decode(errors="replace")


@dataclass(frozen=True)
class SessionState:
    """What `Broker.session` reports: the state at the call, in copies of its collections. The
    `Delivery` records are the broker's own, so `acknowledged_at` still changes on them."""

    connected: bool
    subscriptions: dict[str, int]
    queued: tuple[Message, ...]
    unacknowledged: tuple[Delivery, ...]


Record = TypeVar("Record", Message, Delivery)


class Publication:
    """One incoming QoS 1 exchange. Repeated receipts share this handle until PUBACK.

    `puback_at` records emission to the stream, never processing by the device. Closing the
    connection cancels a pending handle; releasing it then returns False, even after reconnect.
    """

    def __init__(self, session: "_Session", packet_id: int, due: int | None) -> None:
        self._session = session
        self._packet_id = packet_id
        self._due = due
        self._messages: list[Message] = []
        self._status: Literal["pending", "emitted", "cancelled"] = "pending"
        self._puback_at: Duration | None = None

    @property
    def messages(self) -> tuple[Message, ...]:
        return tuple(self._messages)

    @property
    def connection_id(self) -> int:
        return self._session.connection_id

    @property
    def packet_id(self) -> int:
        return self._packet_id

    @property
    def status(self) -> Literal["pending", "emitted", "cancelled"]:
        return self._status

    @property
    def puback_at(self) -> Duration | None:
        return self._puback_at

    def release(self) -> bool:
        """Emit one PUBACK now. Return False for an emitted or cancelled handle."""
        if self._status != "pending":
            return False
        session = self._session
        session.write(_packet(PUBACK, 0, struct.pack(">H", self.packet_id)))
        # A TLS write failure can close the transport before its queued close is handled.
        if session._transport_closed or self._status != "pending":
            return False
        self._status = "emitted"
        self._puback_at = Duration(session.broker._now())
        del session.incoming[self.packet_id]
        session.broker._log(
            f"mqtt: {session.client_id} (connection {self.connection_id}) "
            f"PUBACK {self.packet_id} emitted"
        )
        return True


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
    session_present: bool = False

    @property
    def at(self) -> Duration:
        return self.connect.at

    @property
    def accepted(self) -> bool:
        return self.return_code == ConnackCode.ACCEPTED


@dataclass(frozen=True)
class RequestedFilter:
    """One topic filter of a SUBSCRIBE or UNSUBSCRIBE. `requested_qos` is the QoS the client
    asked for and `granted_qos` the one SUBACK gave; both are `None` in an UNSUBSCRIBE."""

    filter: str
    requested_qos: int | None = None
    granted_qos: int | None = None


@dataclass(frozen=True)
class SubscribeRequest:
    """One SUBSCRIBE or UNSUBSCRIBE packet, with its filters in the order the packet gave them."""

    at: Duration
    client: str
    connection_id: int
    packet_id: int
    kind: Literal["SUBSCRIBE", "UNSUBSCRIBE"]
    filters: tuple[RequestedFilter, ...]

    @property
    def topic_filters(self) -> tuple[str, ...]:
        return tuple(f.filter for f in self.filters)


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


def _wants(topic: str, wanted: bytes | None) -> Callable[[Message | Delivery], bool]:
    """Whether a record is on this topic filter, with this payload if one is wanted."""
    return lambda record: (
        matches(topic, record.topic) and (wanted is None or record.payload == wanted)
    )


def _wants_message(
    topic: str, wanted: bytes | None, will: bool | None
) -> Callable[[Message], bool]:
    """`_wants` over the messages: only Wills when `will` is True, only the rest when False."""
    on_topic = _wants(topic, wanted)
    return lambda message: on_topic(message) and (will is None or message.will is will)


def _first(history: Sequence[Record], topic: str, wanted: bytes | None) -> Record | None:
    """The first record of `history` on this topic filter, with this payload if one is wanted."""
    return next(filter(_wants(topic, wanted), history), None)


def _messages(
    history: Sequence[Message], topic: str, wanted: bytes | None, start: int, will: bool | None
) -> Iterator[Message]:
    """The messages from `start` that `_wants_message` keeps, read by index, without a copy."""
    keep = _wants_message(topic, wanted, will)
    return filter(keep, map(history.__getitem__, range(start, len(history))))


def _will_check(will: bool | None) -> None:
    if will is not None and not isinstance(will, bool):
        raise TypeError("mqtt: will takes True, False or None")


class Mark:
    """A position in one broker's message history, at its end when `Broker.mark` was called.

    Opaque: pass it to `Broker.find_messages(since=...)` of the broker that made it.
    """

    __slots__ = ("_broker", "_index")

    def __init__(self, broker: "Broker", index: int) -> None:
        self._broker = broker
        self._index = index

    def __repr__(self) -> str:
        return f"<MQTT message history mark at {self._index}>"


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
        self._messages: list[Message] = []
        self._clients: list[MqttClient] = []
        self._received_packets: list[ReceivedPacket] = []
        self._connection_ends: list[ConnectionEnd] = []
        self._connection_attempts: list[ConnectionAttempt] = []
        self._subscribe_requests: list[SubscribeRequest] = []
        self._deliveries: list[Delivery] = []
        self.connection_policy = connection_policy
        self.retained: dict[str, Message] = {}
        self._sessions: list[_Session] = []
        self._events: deque[Callable[[], None]] = deque()
        self._states: dict[str, _State] = {}
        self._anonymous = 0
        self._connections = 0
        self._puback: Literal["immediate", "hold"] | Duration = "immediate"

    @property
    def messages(self) -> Sequence[Message]:
        return History(self._messages)

    @property
    def clients(self) -> Sequence[MqttClient]:
        return History(self._clients)

    @property
    def received_packets(self) -> Sequence[ReceivedPacket]:
        return History(self._received_packets)

    @property
    def connection_ends(self) -> Sequence[ConnectionEnd]:
        return History(self._connection_ends)

    @property
    def connection_attempts(self) -> Sequence[ConnectionAttempt]:
        return History(self._connection_attempts)

    @property
    def subscribe_requests(self) -> Sequence[SubscribeRequest]:
        return History(self._subscribe_requests)

    @property
    def deliveries(self) -> Sequence[Delivery]:
        return History(self._deliveries)

    def accept(
        self, peer: tuple[str, int], write: Callable[[bytes], None], close: Callable[[], None]
    ) -> "_Session":
        self._connections += 1
        session = _Session(self, peer, write, close, self._connections)
        self._sessions.append(session)
        return session

    def _dispatch(self, event: Callable[[], None]) -> None:
        # Writes can synchronously trigger input, transport closure or a timer poll.
        # Finish the active event before processing callbacks in their arrival order.
        self._events.append(event)
        if len(self._events) > 1:
            return
        try:
            while self._events:
                self._events[0]()
                self._events.popleft()
        finally:
            self._events.clear()

    def _anonymous_id(self) -> str:
        """Allocate a repeatable ID that no earlier accepted CONNECT in this broker used."""
        used = {client.client_id for client in self.clients}
        while True:
            self._anonymous += 1
            candidate = f"esp32tl-anonymous-{self._anonymous}"
            if candidate not in used:
                return candidate

    # The test's side.

    @property
    def puback(self) -> Literal["immediate", "hold"] | Duration:
        """Policy for new incoming QoS 1 exchanges; existing pending handles keep theirs."""
        return self._puback

    @puback.setter
    def puback(self, policy: Literal["immediate", "hold"] | Duration) -> None:
        if isinstance(policy, Duration):
            if policy.ns < 0:
                raise ValueError("PUBACK delay must be nonnegative")
        elif policy not in ("immediate", "hold"):
            raise ValueError("PUBACK policy must be 'immediate', 'hold', or a Duration")
        self._puback = policy

    @property
    def pending(self) -> tuple[Publication, ...]:
        """Pending handles, grouped by connection and then by first receipt."""
        return tuple(p for session in self._sessions for p in session.incoming.values())

    def disconnect(self, client_id: str) -> bool:
        """Close this client's stream as a connection loss, including its Will, if connected."""
        for session in list(self._sessions):
            if session.connected and session.client_id == client_id:
                self._log(f"mqtt: {client_id} disconnected by the test")
                session._lose("test", Duration(self._now()))
                return True
        return False

    def session(self, client_id: str) -> SessionState | None:
        """The state the broker has for this client ID now, or None."""
        state = self._states.get(client_id)
        if state is None:
            return None
        return SessionState(
            state.connection is not None,
            dict(state.subscriptions),
            tuple(message for message, _ in state.queue),
            tuple(state.sent.values()),
        )

    def forget_session(self, client_id: str) -> bool:
        """Discard a stored session, as if it had never been; return whether there was one."""
        state = self._states.get(client_id)
        if state is None:
            return False
        if state.connection is not None:
            raise ValueError(f"mqtt: {client_id} is connected: disconnect it first")
        del self._states[client_id]
        self._log(f"mqtt: {client_id} session forgotten by the test")
        return True

    def next_message(
        self, topic: str = "#", payload: bytes | str | None = None, *, will: bool | None = None
    ) -> Condition[Message | None]:
        """A non-consuming condition for the first match appended after this call.

        `topic` accepts MQTT wildcards; `payload` is bytes or UTF-8 text. `will=True` keeps only
        the Wills the broker published, `will=False` only the rest; `None` keeps both. The
        boundary is append order, even at equal timestamps. Polling never advances that boundary.
        """
        _will_check(will)
        start = len(self.messages)
        wanted = payload.encode() if isinstance(payload, str) else payload
        description = (
            "the next MQTT "
            + {None: "message", True: "Will", False: "message other than a Will"}[will]
            + f" on {topic!r}"
            + (f" saying {wanted!r}" if wanted is not None else "")
        )
        return Condition(
            FirstFrom(lambda: self.messages, start, _wants_message(topic, wanted, will)),
            description,
            "MQTT",
        )

    def find_message(
        self, topic: str = "#", payload: bytes | str | None = None, *, will: bool | None = None
    ) -> Message | None:
        """Return the first match in recorded history now, or None; consume nothing.

        `topic`, `payload` and `will` filter as in `next_message`.
        """
        _will_check(will)
        wanted = payload.encode() if isinstance(payload, str) else payload
        return next(_messages(self.messages, topic, wanted, 0, will), None)

    def mark(self) -> Mark:
        """A position at the end of `messages` now, for `find_messages(since=...)`."""
        return Mark(self, len(self.messages))

    def find_messages(
        self,
        topic: str = "#",
        payload: bytes | str | None = None,
        *,
        since: Mark | None = None,
        client: str | None = None,
        will: bool | None = None,
    ) -> list[Message]:
        """Every match in recorded history now, in append order; consume nothing.

        `since` keeps the messages appended after that mark was made, even at its board time;
        `None` reads from the start. `client` keeps one client ID's messages: `TEST_CLIENT` for
        the test's own publications. `will` filters as in `next_message`.
        """
        _will_check(will)
        if since is not None and not isinstance(since, Mark):
            raise TypeError("mqtt: since takes a mark from broker.mark()")
        if since is not None and since._broker is not self:
            raise ValueError("mqtt: this mark belongs to another broker")
        wanted = payload.encode() if isinstance(payload, str) else payload
        start = 0 if since is None else since._index
        found = _messages(self.messages, topic, wanted, start, will)
        return [m for m in found if client is None or m.client == client]

    def next_delivery(
        self, topic: str = "#", payload: bytes | str | None = None
    ) -> Condition[Delivery | None]:
        """A non-consuming condition for the first PUBLISH written to a client after this call,
        with the filters and the boundary of `next_message`."""
        start = len(self.deliveries)
        wanted = payload.encode() if isinstance(payload, str) else payload
        description = f"the next MQTT delivery on {topic!r}" + (
            f" saying {wanted!r}" if wanted is not None else ""
        )
        return Condition(
            FirstFrom(lambda: self.deliveries, start, _wants(topic, wanted)), description, "MQTT"
        )

    def find_delivery(
        self, topic: str = "#", payload: bytes | str | None = None
    ) -> Delivery | None:
        """Return the first match in the delivery history now, or None; consume nothing."""
        wanted = payload.encode() if isinstance(payload, str) else payload
        return _first(self._deliveries, topic, wanted)

    def is_subscribed(self, topic: str) -> Condition[bool]:
        """A live condition: an active client currently subscribes to exactly `topic`."""

        def find() -> bool:
            return any(topic in session.subscriptions for session in self._sessions)

        return Condition(find, f"an active MQTT subscription to {topic!r}", "MQTT")

    def next_connection(self) -> Condition[MqttClient | None]:
        """A non-consuming condition for the first CONNECT recorded after this call."""
        start = len(self.clients)

        def find() -> MqttClient | None:
            return self.clients[start] if len(self.clients) > start else None

        return Condition(find, "the next MQTT client connection", "MQTT")

    def find_connection(self) -> MqttClient | None:
        """Return the first recorded CONNECT, even if it has closed, or None."""
        return self.clients[0] if self.clients else None

    def next_connection_attempt(self) -> Condition[ConnectionAttempt | None]:
        """A non-consuming condition for the first CONNECT outcome recorded after this call,
        accepted or rejected."""
        start = len(self.connection_attempts)

        def find() -> ConnectionAttempt | None:
            return (
                self.connection_attempts[start] if len(self.connection_attempts) > start else None
            )

        return Condition(find, "the next MQTT connection attempt", "MQTT")

    def ended(self, connection_id: int) -> ConnectionEnd | None:
        """How and when the connection `connection_id` ended, or None while it has not."""
        return next((e for e in self._connection_ends if e.connection_id == connection_id), None)

    def next_received_packet(
        self, kind: PacketKind | None = None, *, client_id: str | None = None
    ) -> Condition[ReceivedPacket | None]:
        """A non-consuming condition for the first matching packet recorded after this call."""
        if kind is not None and kind not in _PACKET_KINDS.values():
            raise ValueError(f"kind must be one of {', '.join(_PACKET_KINDS.values())}")
        find = FirstFrom(
            lambda: self.received_packets,
            len(self.received_packets),
            lambda packet: (
                (kind is None or packet.kind == kind)
                and (client_id is None or packet.client_id == client_id)
            ),
        )
        description = f"the next MQTT {kind or 'packet'}"
        if client_id is not None:
            description += f" from {client_id!r}"
        return Condition(find, description, "MQTT")

    def next_end(self, client_id: str | None = None) -> Condition[ConnectionEnd | None]:
        """A non-consuming condition for the first connection end recorded after this call; with
        `client_id`, the first of a connection with that client ID."""
        find = FirstFrom(
            lambda: self.connection_ends,
            len(self.connection_ends),
            lambda end: client_id is None or end.client_id == client_id,
        )
        description = "the next MQTT connection end" + (
            f" of {client_id!r}" if client_id is not None else ""
        )
        return Condition(find, description, "MQTT")

    def next_subscribe(self, filter: str | None = None) -> Condition[SubscribeRequest | None]:
        """A non-consuming condition for the first SUBSCRIBE recorded after this call; with
        `filter`, the first that names exactly that topic filter. UNSUBSCRIBE never matches."""
        find = FirstFrom(
            lambda: self.subscribe_requests,
            len(self.subscribe_requests),
            lambda request: (
                request.kind == "SUBSCRIBE" and (filter is None or filter in request.topic_filters)
            ),
        )
        description = "the next MQTT SUBSCRIBE" + (f" to {filter!r}" if filter is not None else "")
        return Condition(find, description, "MQTT")

    def publish(
        self, topic: str, payload: bytes | str, *, retain: bool = False, qos: int = 0
    ) -> None:
        if not topic or "+" in topic or "#" in topic or "\0" in topic:
            raise ValueError("MQTT publish needs a topic without wildcards or NUL")
        if qos not in (0, 1):
            raise ValueError("MQTT supports QoS 0 and 1")
        data = payload.encode() if isinstance(payload, str) else payload
        self._route(Message(Duration(self._now()), TEST_CLIENT, topic, data, qos, retain))

    def poll(self) -> None:
        now = self._now()
        self._dispatch(lambda: self._poll(now))

    def _poll(self, now: int) -> None:
        for session in list(self._sessions):
            if session.deadline is not None and now >= session.deadline:
                self._log(f"mqtt: {session.client_id} keepalive expired")
                session._lose("keepalive", Duration(now))
        # Connection expiry wins a tie with a PUBACK deadline.
        for publication in self.pending:
            if publication._due is not None and now >= publication._due:
                publication.release()

    def quiet_until(self) -> int | None:
        deadlines = [s.deadline for s in self._sessions if s.deadline is not None]
        deadlines.extend(p._due for p in self.pending if p._due is not None)
        return min(deadlines, default=None)

    # Routing.

    def _route(self, message: Message) -> None:
        self._messages.append(message)
        self._log(f"mqtt: {message.client} → {message.topic} {message.payload[:60]!r}")
        if message.retain:
            if message.payload:
                self.retained[message.topic] = message
            else:
                self.retained.pop(message.topic, None)
        for client_id, state in list(self._states.items()):
            self._offer(client_id, state, message)

    def _offer(self, client_id: str, state: "_State", message: Message) -> None:
        if not any(matches(f, message.topic) for f in state.subscriptions):
            return
        if state.connection is not None and not state.connection._transport_closed:
            state.connection.deliver(message)
        elif state.clean:
            return  # closed while this message was being routed: its state is gone
        elif message.qos:
            # MQTT-3.1.2-5 stores by the message's QoS; the subscription's lowers it when it goes.
            state.queue.append((message, False))
            self._log(f"mqtt: {client_id} is offline: queued a message on {message.topic}")
        else:
            self._log(f"mqtt: {client_id} is offline: dropped a QoS 0 message on {message.topic}")


class _State:
    """A session's state, kept by client ID. CleanSession=1 keeps it for one connection,
    CleanSession=0 until the client connects with CleanSession=1 or the test forgets it."""

    def __init__(self, clean: bool) -> None:
        self.clean = clean
        self.connection: _Session | None = None
        self.subscriptions: dict[str, int] = {}
        # Each message with the RETAIN it goes with: 1 only for a retained one a SUBSCRIBE matched.
        self.queue: deque[tuple[Message, bool]] = deque()
        # `id` of each queued message with RETAIN 1: the queue holds it, so the `id` stays unique.
        self.queued_retained: set[int] = set()
        # Every packet ID in use; `sent` holds, in the order first sent, what to send again.
        self.unacked: set[int] = set()
        self.sent: dict[int, Delivery] = {}
        self.packet_id = 0


class _Session:
    def __init__(
        self,
        broker: Broker,
        peer: tuple[str, int],
        write: Callable[[bytes], None],
        close: Callable[[], None],
        connection_id: int,
    ) -> None:
        self.broker = broker
        self.peer = peer
        self._write = write
        self._transport_closed = False
        self.close = close
        self.buffer = bytearray()
        self.client_id = "?"
        self.connection_id = connection_id
        self.incoming: dict[int, Publication] = {}
        self.state: _State | None = None
        self.will: Message | None = None
        self.closed = False
        self.connected = False
        self.waiting = False
        self.keepalive = 0
        self.deadline: int | None = None

    @property
    def subscriptions(self) -> dict[str, int]:
        return {} if self.state is None else self.state.subscriptions

    @property
    def unacked(self) -> set[int]:
        return set() if self.state is None else self.state.unacked

    def deliver(self, message: Message, retained: bool = False) -> None:
        """Send a message that matches a subscription, or queue it while no packet ID is free.
        QoS 1 goes behind a queue that is waiting, in order (MQTT-4.6.0-6), and QoS 0, which needs
        no ID, goes at once. `retained` is RETAIN 1, for a retained message a SUBSCRIBE matched."""
        qos = self._qos(message)
        if qos is None:
            return
        state = self.state
        assert state is not None
        if (state.queue and message.qos) or (qos and len(state.unacked) == 65535):
            # A SUBSCRIBE again while this retained message still waits from the last one: the
            # copy waiting is the re-send (MQTT-3.8.4-3), so the queue does not grow with each.
            if not (retained and id(message) in state.queued_retained):
                state.queue.append((message, retained))
                if retained:
                    state.queued_retained.add(id(message))
            self._wait()
            return
        self._send(message, qos, retained)

    def _qos(self, message: Message) -> int | None:
        """The QoS this message goes at, or `None` if no subscription matches or it has closed."""
        granted = [q for f, q in self.subscriptions.items() if matches(f, message.topic)]
        if not granted or self.closed or self._transport_closed:
            return None
        return min(max(granted), message.qos)

    def _send(self, message: Message, qos: int, retained: bool) -> None:
        packet_id = self._packet_id() if qos else None
        self._publish(message.topic, message.payload, qos, packet_id, False, retained)

    def _wait(self) -> None:
        """Log the start of a wait for a packet ID, once until the queue is empty again."""
        if not self.waiting:
            self.waiting = True
            assert self.state is not None
            self.broker._log(
                f"mqtt: {self.client_id} has no free packet identifier:"
                f" waiting for a PUBACK, {len(self.state.queue)} queued"
            )

    def _packet_id(self) -> int:
        """The next packet ID free in this session; the caller has made sure that one is."""
        state = self.state
        assert state is not None and len(state.unacked) < 65535
        state.packet_id = state.packet_id % 0xFFFF + 1
        while state.packet_id in state.unacked:
            state.packet_id = state.packet_id % 0xFFFF + 1
        state.unacked.add(state.packet_id)
        return state.packet_id

    def _publish(
        self, topic: str, payload: bytes, qos: int, packet_id: int | None, dup: bool, retain: bool
    ) -> None:
        state = self.state
        assert state is not None
        delivery = Delivery(
            Duration(self.broker._now()),
            self.client_id,
            self.connection_id,
            topic,
            payload,
            qos,
            packet_id,
            dup,
            retain,
        )
        self.broker._deliveries.append(delivery)
        body = struct.pack(">H", len(topic.encode())) + topic.encode()
        if packet_id is not None:
            state.sent[packet_id] = delivery
            body += struct.pack(">H", packet_id)
        flags = (8 if dup else 0) | (qos << 1) | (1 if retain else 0)
        self.write(_packet(PUBLISH, flags, body + payload))

    def _resume(self) -> None:
        """What the session owes the client, after CONNACK: what it never acknowledged, with its
        packet IDs and DUP (MQTT-4.4.0-1, MQTT-3.3.1-1), then the queue, in order (MQTT-4.6.0-6),
        as far as packet IDs allow.
        The queue goes out as a first attempt, DUP 0 (MQTT-3.3.1-3), and RETAIN 0 to established
        subscriptions (MQTT-3.3.1-9) but 1 for a retained message a SUBSCRIBE matched
        (MQTT-3.3.1-8); what is sent again keeps the RETAIN it had."""
        state = self.state
        assert state is not None
        for packet_id, sent in list(state.sent.items()):
            if self.closed or self._transport_closed:
                return
            self._publish(sent.topic, sent.payload, sent.qos, packet_id, True, sent.retain)
        self._drain()

    def _drain(self) -> None:
        """Send the queue in order until the head would go at QoS 1 with no packet ID free: each
        PUBACK frees one for the next, and one going at QoS 0 takes none.
        Waiting, not closing: the client's PUBACKs could not reach a closed connection, and it
        would get the same messages again on every connection."""
        state = self.state
        assert state is not None
        while state.queue and not self.closed and not self._transport_closed:
            message, retained = state.queue[0]
            qos = self._qos(message)
            if qos and len(state.unacked) == 65535:
                self._wait()
                return
            state.queue.popleft()
            if retained:
                state.queued_retained.discard(id(message))
            if qos is not None:
                self._send(message, qos, retained)
        if not state.queue:
            self.waiting = False

    def write(self, data: bytes) -> None:
        if not self._transport_closed:
            self._write(data)

    def on_data(self, data: bytes) -> None:
        if not self.closed and not self._transport_closed:
            received_at = Duration(self.broker._now())
            self.broker._dispatch(lambda: self._read_packets(data, received_at))

    def _read_packets(self, data: bytes, received_at: Duration) -> None:
        if self.closed:
            return
        self.buffer += data
        while True:
            if len(self.buffer) < 2:
                return
            length, multiplier, at = 0, 1, 1
            while True:
                if at > 4:
                    self._lose("malformed", received_at)
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
                self._lose("malformed", received_at)
                return
            if len(self.buffer) < at + length:
                return
            first, body = self.buffer[0], bytes(self.buffer[at : at + length])
            del self.buffer[: at + length]
            kind = first >> 4
            connect = None
            try:
                if kind == CONNECT:
                    connect = self._parse_connect(first & 0x0F, body, received_at)
                else:
                    self._handle(kind, first & 0x0F, body, received_at)
            except (ValueError, IndexError, struct.error):
                self.broker._log("mqtt: malformed or unsupported packet, closing")
                self._lose("malformed", received_at)
                return
            if connect is not None:
                # Test policy errors must reach the test, not become malformed wire input.
                self._connect(connect)
            self.broker._received_packets.append(
                ReceivedPacket(received_at, self.connection_id, self.client_id, _PACKET_KINDS[kind])
            )
            if self.closed:
                return
            if self.keepalive:
                self.deadline = received_at.ns + self.keepalive * 1_500_000_000

    def _parse_connect(self, flags: int, body: bytes, now: Duration) -> Connect:
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
            at=now,
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
        stored = broker._states.get(connect.client_id)
        session_present = (
            code == ConnackCode.ACCEPTED
            and not connect.clean_session
            and stored is not None
            and not stored.clean
        )
        broker._connection_attempts.append(ConnectionAttempt(connect, code, session_present))
        if code != ConnackCode.ACCEPTED:
            self.client_id = connect.client_id
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
                connection_id=self.connection_id,
                will=True,
            )
        for session in list(broker._sessions):
            if session is not self and session.connected and session.client_id == self.client_id:
                # MQTT-3.1.4-2. A session that both connections keep takes its old connection's
                # Will away unsent, as mosquitto does: a device back before its keepalive ran out
                # has not gone. Either side with CleanSession=1, the Will goes, and so it does
                # when the old keepalive has run out by now, though `poll` has not yet expired it.
                kept = session.state is not None and not session.state.clean
                alive = session.deadline is None or connect.at.ns < session.deadline
                discarded = (
                    kept and alive and not connect.clean_session and session.will is not None
                )
                if discarded:
                    session.will = None
                broker._log(
                    f"mqtt: {self.client_id} (connection {self.connection_id}) took over"
                    f" connection {session.connection_id}"
                    + (", its Will discarded" if discarded else "")
                )
                session._lose("takeover", connect.at)
        if connect.clean_session or self.client_id not in broker._states:
            if self.client_id in broker._states:
                broker._log(f"mqtt: {self.client_id} CleanSession=1 discarded the stored session")
            broker._states[self.client_id] = _State(connect.clean_session)
        self.state = broker._states[self.client_id]
        self.state.connection = self
        self.connected = True
        self.keepalive = connect.keepalive
        broker._clients.append(
            MqttClient(
                connect.at,
                self.client_id,
                connect.username,
                connect.password,
                connect.keepalive,
                connect.will_topic,
                self.connection_id,
                session_present,
            )
        )
        broker._log(f"mqtt: {self.client_id} connected")
        self.write(_packet(CONNACK, 0, bytes([session_present, code])))
        if session_present:
            broker._log(
                f"mqtt: {self.client_id} resumed its session: {len(self.state.sent)} to send"
                f" again, {len(self.state.queue)} queued"
            )
            self._resume()

    def _handle(self, kind: int, flags: int, body: bytes, now: Duration) -> None:
        if kind != PUBLISH and flags != (2 if kind in (SUBSCRIBE, UNSUBSCRIBE) else 0):
            raise ValueError("invalid MQTT flags")
        if not self.connected:
            raise ValueError("CONNECT must come first")
        broker = self.broker
        if kind == PUBLISH:
            qos, retain = (flags >> 1) & 3, bool(flags & 1)
            topic, at = _string(body, 0)
            if not topic or "+" in topic or "#" in topic or qos > 1:
                raise ValueError("unsupported PUBLISH topic or QoS")
            if not qos and flags & 8:
                raise ValueError("DUP set on a QoS 0 PUBLISH")
            packet_id = None
            if qos:
                (packet_id,) = struct.unpack(">H", body[at : at + 2])
                at += 2
                if not packet_id:
                    raise ValueError("zero packet id")
            publication = None
            if packet_id is not None:
                publication = self.incoming.get(packet_id)
                if publication is not None:
                    first = publication._messages[0]
                    if (first.topic, first.payload) != (topic, body[at:]):
                        broker._log(
                            f"mqtt: {self.client_id} (connection {self.connection_id}) "
                            f"reused pending packet ID {packet_id} for a different publication"
                        )
                else:
                    policy = broker.puback
                    due = (
                        None
                        if policy == "hold"
                        else now.ns + (policy.ns if isinstance(policy, Duration) else 0)
                    )
                    publication = Publication(self, packet_id, due)
                    self.incoming[packet_id] = publication
            message = Message(
                now,
                self.client_id,
                topic,
                body[at:],
                qos,
                retain,
                self.connection_id,
                packet_id,
                bool(flags & 8),
                publication,
            )
            if publication is not None:
                publication._messages.append(message)
            broker._route(message)
            if (
                publication is not None
                and publication._due is not None
                and publication._due <= now.ns
            ):
                publication.release()

        elif kind == SUBSCRIBE:
            packet_id, at = body[:2], 2
            if len(body) < 5 or packet_id == b"\0\0":
                raise ValueError("empty SUBSCRIBE or zero packet id")
            requested, filters, granted = [], [], bytearray()
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
                at += 1
                requested.append(RequestedFilter(topic, qos, min(qos, 1)))
                filters.append(topic)
                granted.append(min(qos, 1))
            # The whole packet is parsed first: a malformed one subscribes to nothing.
            self._record("SUBSCRIBE", now, packet_id, requested)
            self.subscriptions.update(zip(filters, granted, strict=True))
            broker._log(f"mqtt: {self.client_id} subscribed to {', '.join(filters)}")
            self.write(_packet(SUBACK, 0, packet_id + bytes(granted)))
            # A copy: a TLS write that fails closes, and the Will can change what is retained.
            for message in list(broker.retained.values()):
                if any(matches(f, message.topic) for f in filters):
                    self.deliver(message, retained=True)
        elif kind == UNSUBSCRIBE:
            if len(body) < 5 or body[:2] == b"\0\0":
                raise ValueError("empty UNSUBSCRIBE or zero packet id")
            at = 2
            requested = []
            while at < len(body):
                topic, at = _string(body, at)
                requested.append(RequestedFilter(topic))
            self._record("UNSUBSCRIBE", now, body[:2], requested)
            for request in requested:
                self.subscriptions.pop(request.filter, None)
            self.write(_packet(UNSUBACK, 0, body[:2]))
        elif kind == PINGREQ:
            if body:
                raise ValueError("PINGREQ payload")
            self.write(_packet(PINGRESP, 0, b""))
        elif kind == DISCONNECT:
            if body:
                raise ValueError("DISCONNECT payload")
            broker._log(f"mqtt: {self.client_id} sent DISCONNECT")
            self.will = None
            self._end("disconnect", at=now)
        elif kind == PUBACK:
            if len(body) != 2:
                raise ValueError("invalid PUBACK")
            assert self.state is not None
            packet_id = int.from_bytes(body, "big")
            self.state.unacked.discard(packet_id)
            sent = self.state.sent.pop(packet_id, None)
            if sent is not None:
                sent.acknowledged_at = now
            self._drain()
        else:
            raise ValueError("unsupported MQTT packet")

    def _record(
        self,
        kind: Literal["SUBSCRIBE", "UNSUBSCRIBE"],
        now: Duration,
        packet_id: bytes,
        filters: list[RequestedFilter],
    ) -> None:
        self.broker._subscribe_requests.append(
            SubscribeRequest(
                now,
                self.client_id,
                self.connection_id,
                int.from_bytes(packet_id, "big"),
                kind,
                tuple(filters),
            )
        )

    def on_close(self) -> None:
        if not self.closed and not self._transport_closed:
            self._transport_closed = True
            at = Duration(self.broker._now())
            self.broker._dispatch(lambda: self._lose("closed", at))

    def _lose(self, by: EndCause, at: Duration) -> None:
        """End as a connection lost `by` this: with its Will, if it was accepted with one."""
        will, self.will = self.will, None
        if self.closed or not self.connected:
            will = None
        self._end(by, at=at, will_sent=will is not None)
        if will is not None:
            self.broker._route(replace(will, at=at))

    def _end(
        self, by: EndCause | None = None, *, at: Duration | None = None, will_sent: bool = False
    ) -> None:
        """Close once. An accepted connection records how, `by`, in `Broker.connection_ends`,
        before its Will is published; one never accepted, closed with no `by`, records nothing,
        its CONNECT's outcome being in `Broker.connection_attempts`."""
        if not self.closed:
            self.closed = True
            self.deadline = None
            for publication in self.incoming.values():
                publication._status = "cancelled"
            self.incoming.clear()
            if self in self.broker._sessions:
                self.broker._sessions.remove(self)
            if self.state is not None:
                self.state.connection = None
                if self.state.clean:
                    del self.broker._states[self.client_id]
            if self.connected:
                assert by is not None
                assert at is not None
                self.broker._connection_ends.append(
                    ConnectionEnd(at, self.client_id, self.connection_id, by, will_sent)
                )
            self.close()
