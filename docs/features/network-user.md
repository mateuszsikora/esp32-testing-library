# Simulated network

The network the device reaches lives in the test process and on the board's emulated clock. It is
the same network behind a modem over PPP, a virtual Wi-Fi access point over Ethernet, or a
[virtual station joining the device's SoftAP](#joining-the-devices-own-access-point).
All provide DNS, NTP, an MQTT broker on TCP 1883, an HTTP server on TCP 80 and custom TCP services;
behind the virtual station these listen only on its leased host address. On every link the test can
[open a TCP connection to the device](#open-a-tcp-connection-to-the-device). The firmware keeps its Wi-Fi,
PPP, IP, DNS, MQTT and HTTP stacks and its configured destination names. There is no host network
connection, external broker or web server, `pppd`, real-time mode or clock switch, unless the test
[bridges a port to a real service](#bridge-a-port-to-a-real-service), which puts the run on the
wall clock.

## Exchange MQTT messages behind the modem

Build the unmodified esp-protocols example with `firmware/build.sh pppos_client`. Its sources
are pinned to `modem-v1.4.1`; only sdkconfig changes, to select SIM7600 AT commands and the
LilyGO modem UART. The `broker` fixture selects the modem model and network before power-on:

```python
import pytest
from esp32tl.units import seconds

pytestmark = [
    pytest.mark.app_path("../firmware/pppos_client"),
    pytest.mark.board_profile("lilygo-t-a7670g-r2"),
]


def test_publishes(board, broker):
    board.power_on()
    message = board.wait_until(broker.next_message("/topic/esp-pppos"), within=seconds(60))
    assert message.text == "esp32-pppos"


def test_receives(board, broker, console):
    board.power_on()
    board.wait_until(broker.subscribed("/topic/esp-pppos"), within=seconds(60))
    broker.publish("/topic/esp-pppos", "hello from the test")
    board.wait_until(console.line(r"DATA=hello from the test"), within=seconds(5))
```

`modem.network()` selects the same network explicitly, or returns it if already selected.
It can extend an AT model selected earlier, but must first be called before `board.power_on()`.
A replay and a model remain mutually exclusive. Requesting `modem` alone still leaves the UART
silent; an AT-only model still works without PPP. The network's broker is `modem.network().broker`.

| Broker API | Meaning |
|---|---|
| `next_message(topic="#", payload=None, *, will=None)` | Condition returning the first matching message recorded **after this method call**; MQTT topic filters are accepted, payload is bytes or text; `will=True` keeps only Wills, `will=False` only the rest ([Wills](#a-will-in-the-history)) |
| `find_message(topic="#", payload=None, *, will=None)` | Immediate history query returning the first recorded match, or `None`; the same filters as `next_message` |
| `mark()` | A `Mark`: an opaque position at the end of `messages` now, for `find_messages(since=...)` ([every message since a point](#every-message-since-a-point)) |
| `find_messages(topic="#", payload=None, *, since=None, client=None, will=None)` | Immediate history query returning a new list of every recorded match after `since` (from the start when `None`), in append order; `client` keeps one client ID's messages, `will` filters as in `next_message` |
| `subscribed(topic)` | Condition true while an active client has this exact subscription filter, including an existing subscription |
| `next_connection()` | Condition returning the first accepted MQTT CONNECT recorded **after this method call** |
| `find_connection()` | Immediate history query returning the first accepted CONNECT, or `None`, even if that connection has closed |
| `next_connection_attempt()` | Condition returning the first accepted or rejected CONNECT outcome recorded **after this method call** |
| `next_subscribe(filter=None)` | Condition returning the first SUBSCRIBE recorded **after this method call**; with `filter`, the first naming exactly that topic filter ([every SUBSCRIBE and UNSUBSCRIBE](#every-subscribe-and-unsubscribe)) |
| `connection_policy` | Callable receiving `Connect` and returning `ConnackCode`; `None` (default) accepts supported requests |
| `publish(topic, payload, retain=False, qos=0)` | Publish from the test at the board's current time; bytes or UTF-8 text, QoS 0 or 1. The message's `client` is `TEST_CLIENT` |
| `messages`, `clients`, `retained` | Message history, accepted-client history and currently retained messages |
| `connection_attempts` | CONNECT outcome history, including accepted and rejected attempts |
| `subscribe_requests` | Every SUBSCRIBE and UNSUBSCRIBE received, each a `SubscribeRequest` |
| `puback` | Policy for new incoming QoS 1 exchanges: `"immediate"` (default), `"hold"`, or a nonnegative `Duration` |
| `pending` | Tuple of pending `Publication` handles, grouped by connection, then first receipt |
| `disconnect(client_id)` | Close that client's stream as a connection loss; return `True` if connected, otherwise `False` |
| `session(client_id)` | The session the broker has for this client ID now, a `SessionState`, or `None` ([sessions](#keep-a-session-across-connections)) |
| `forget_session(client_id)` | Discard a stored session; return `True` if there was one, otherwise `False`. A connected client raises `ValueError` (`mqtt: c is connected: disconnect it first`) |
| `deliveries` | Every PUBLISH the broker wrote to a client, each a `Delivery` |
| `next_delivery(topic="#", payload=None)` | Condition returning the first matching delivery recorded **after this method call**; the same filters as `next_message` |
| `find_delivery(topic="#", payload=None)` | Immediate history query returning the first recorded match, or `None` |

A message has `at` (`Duration`), `client`, `topic`, `payload` (bytes), `qos`, `retain`, `will`
and `text` (UTF-8 with replacement). `will` is `True` only for a Will the broker published.
Incoming publications also expose `connection_id`, `packet_id` (`None` for QoS 0), `dup` and
`publication` (a `Publication` handle for QoS 1, otherwise `None`).
Test publications use client `TEST_CLIENT` (`"(test)"`, exported by `esp32tl`) and
have no connection, packet ID or handle. Wills carry their original connection ID but have no
packet ID or handle. A client record has `at`,
`client_id`, `username`, `password`, `keepalive`, `will_topic`, `connection_id` and
`session_present`, what its CONNACK said. Connection
IDs are increasing integers local to this broker, allocated when streams are accepted (so a
rejected CONNECT also uses one), shared across plain and TLS listeners and never reused,
including after a network reset. By default, credentials are observed, not authenticated;
install a connection policy to check them. Treat reports and histories as test data: payloads,
usernames and passwords can be sensitive.

### History, next events and current state

Construct a next-event condition **before** the action or wait that may produce its event.
The boundary is the end of the corresponding history at the method call, not the first poll
or the start of `board.wait_until`. Events already recorded are excluded; events appended
afterwards are included even if their board timestamps equal an earlier event's timestamp.
Within each history, append order breaks timestamp ties and the first matching entry wins.
There is no timestamp cursor shared across MQTT, TLS, HTTP and DHCP, nor a cross-history
ordering API; the MQTT message history has [a mark of its own](#every-message-since-a-point).

For example, a device may connect and publish within one run slice. Arm both conditions before
power-on so waiting for the connection cannot miss the immediate publication:

```python
connected = broker.next_connection()
first_report = broker.next_message("device/report")
board.power_on()
board.wait_until(connected, within=seconds(60))
report = board.wait_until(first_report, within=seconds(10))
assert broker.find_message("device/report") is report
later_report = broker.next_message("device/report")  # Excludes that already recorded report.
```

`find_message`, `find_connection` and `find_delivery` return records now, not conditions; they never wait or
advance time. They include all history, including closed connections, test publications and
Wills. The public `messages` and `clients` lists expose all records in append order; treat them
as read-only histories. Network resets do not clear them. Payload decoding and application
schema assertions belong in consumer code, as in the position tracker's `Reports` adapter.

### Every message since a point

`next_message` and `find_message` give the first match only. A check over a stream of
publications (every batch a closing message names arrived, nothing of a kind after a wake,
sequence numbers without a gap across an outage) takes a mark before the action and reads every
match after it:

```python
import json

from esp32tl import TEST_CLIENT
from esp32tl.units import seconds

DEVICE = "logger-1"  # The client ID the firmware connects with.

mark = broker.mark()
broker.publish("device/config", "interval=10", retain=True)
board.wait(seconds(60))
published = broker.find_messages("device/telemetry/#", since=mark, client=DEVICE)
assert [json.loads(m.payload)["seq"] for m in published] == list(range(1, 7))
assert broker.find_messages(since=mark, client=TEST_CLIENT)[0].topic == "device/config"
```

The mark is the end of `messages` at the `mark()` call, as a `next_*` boundary is: a message
appended after it is included even when its board time equals the mark's, or that of a message
before it, and a message appended before it is excluded. The list is in append order, a new list
each call, and reading it consumes nothing; take a new mark for a new point. `client` is a client
ID, compared exactly: a Will carries its client's ID, so `client=DEVICE` keeps the device's Will
too, unless `will=False` leaves it out, and `client=TEST_CLIENT` keeps the test's own publications.
Without `client`, every client's messages are included, test publications too, as in `find_message`.
A QoS 1 retransmission is a record of its own, with `dup` set. A mark belongs to the broker that
made it: another broker's `find_messages` raises `ValueError`
(`mqtt: this mark belongs to another broker`), and a `since` that is not a mark, an index into
`messages` for one, raises `TypeError` (`mqtt: since takes a mark from broker.mark()`).
`next_message` takes no mark, and the mark is the message history's own: it orders nothing against
`clients`, `deliveries` or another service.

### A Will in the history

When a connection with a Will closes without a DISCONNECT (keepalive expiry, a transport loss, a new
connection taking over its client ID unless both keep the session, a malformed packet or
`broker.disconnect`), the broker publishes the Will and records it in `messages` at the time of the
loss. A lost link, `wifi.access_point_off()` among them, closes nothing by itself: the Will goes at
the keepalive expiry ([a lost link](#a-lost-link-and-the-will)). Its `client` is the ID of the client that set it, as for that client's own publications,
and its `will` is `True`; a PUBLISH the client sent and a test publication have `will` `False`, even
when the topic, payload, QoS and retain flag are the Will's own. A retained Will in `retained` has
`will` set too. A check over what the device published itself leaves the Will out, and a test can
ask whether the device sent its offline message before it slept or the broker sent it after:

```python
from esp32tl.units import seconds

DEVICE = "logger-1"  # The client ID the firmware connects with.

mark = broker.mark()
board.wait(seconds(120))
own = broker.find_messages("device/#", since=mark, client=DEVICE, will=False)
assert all(len(m.payload) <= 256 for m in own)
# It went to sleep with a DISCONNECT after its own "offline": the broker published no Will.
assert broker.find_messages("device/status", "offline", since=mark, client=DEVICE, will=False)
assert broker.find_messages("device/status", since=mark, will=True) == []
```

`will=None`, the default, keeps both. A `will` other than `True`, `False` or `None` raises
`TypeError` (`mqtt: will takes True, False or None`). The condition names say which it waits for:
`the next MQTT Will on 'device/status'` and `the next MQTT message other than a Will on
'device/status'`. A `Delivery` of a Will to a subscriber carries no such flag: it is a PUBLISH
the broker wrote, as every delivery is.

Polling any MQTT event condition does **not** consume a match or move its boundary. Repeated
calls and waits return the same first match; construct a new `next_*` condition for a new wait.
`subscribed` instead reads current state each time: unsubscribe or connection closure can make
a previously true condition false.

The names say which is which, on every service of the network: a condition that sees only what
happens after the call that makes it is `next_*` (`next_message`, `next_connection`,
`next_connection_attempt`, `next_delivery`, `next_subscribe`, HTTP's `next_request`, DHCP's
`next_lease`, TLS's `next_handshake_started` and `next_handshake_completed`, a TCP connection's
`next_data`); `find_*` reads the history now and returns a record or `None`; a plural attribute
(`messages`, `clients`, `subscribe_requests`, `leases`) is the history itself; and a condition
named for a state (`subscribed`, TLS's `handshake_started` and `handshake_completed`, a TCP
connection's `established`, `closed`, `refused` and `reset`) is true while that state holds, or
once it has.

All MQTT conditions have timeout representations such as `the next MQTT message on 'a/+' saying
b'new'`, `the next MQTT client connection`, `the next MQTT connection attempt`,
`the next MQTT delivery on 'a/+' saying b'new'`, `the next MQTT SUBSCRIBE to 'a/#'` and
`an active MQTT subscription to 'a/#'`. Call a condition with `()` or pass it to
`board.wait_until`. Boolean coercion of any MQTT condition, including those from `next_message`,
`next_connection`, `next_connection_attempt`, `next_delivery`, `next_subscribe` and `subscribed`,
raises `TypeError("call the MQTT condition with (), or pass it to board.wait_until")`, before and
after a match, so an unevaluated `assert broker.next_connection()` fails. Use
`assert broker.find_connection() is not None` to inspect past connections.

Subscriptions support `+` and final `#`, including empty levels. Wildcards beginning with `+`
or `#` do not match `$` topics. QoS 0 and 1, PUBACK, retained delivery, empty retained-message
deletion, unsubscribe, PING and Will on connection loss are supported. Keepalive expires after
1.5 times its advertised interval on the board's clock. DISCONNECT suppresses the Will.
QoS 2 publications are refused, and so is a QoS 0 PUBLISH with the DUP flag set (MQTT 3.1.1
requires DUP 0 for QoS 0): the broker logs `mqtt: malformed or unsupported packet, closing` and
closes the connection.

A CONNECT with an empty client ID and `CleanSession=1` receives a generated ID such as
`esp32tl-anonymous-1`, visible in accepted-client history, messages and Wills. IDs use a
per-broker counter and skip IDs already in its accepted-client history, so anonymous clients
remain independent and repeated test runs assign the same IDs. An empty ID with
`CleanSession=0` receives CONNACK return code 2 (`Identifier rejected`) and is disconnected.

### Every SUBSCRIBE and UNSUBSCRIBE

The broker grants every filter it is asked for, so whether the device asks for no more than a
production broker's ACL would grant is for the test to check. `subscribed(topic)` and
`session(client_id).subscriptions` say what holds now; `subscribe_requests` keeps every SUBSCRIBE
and UNSUBSCRIBE packet the broker accepted, in the order they arrived, also after the filter is
unsubscribed, the connection closes, a CleanSession=1 CONNECT discards the stored session, the
test forgets it, or the network resets:

```python
from esp32tl.units import seconds

DEVICE = "logger-1"  # The client ID the firmware connects with.
ALLOWED = {"device/logger-1/config", "device/logger-1/cmd/#"}

first = broker.next_subscribe("device/logger-1/cmd/#")
board.power_on()
before = board.wait_until(first, within=seconds(60))
board.wait(seconds(120))  # Let it reconnect, unsubscribe, resubscribe.
asked = {
    topic_filter
    for request in broker.subscribe_requests
    if request.kind == "SUBSCRIBE" and request.client == DEVICE
    for topic_filter in request.topic_filters
}
assert asked <= ALLOWED
```

A `SubscribeRequest` is immutable and has `at` (`Duration`, when the packet arrived), `client`
(the client ID, a generated anonymous one included), `connection_id`, `packet_id`, `kind`
(`"SUBSCRIBE"` or `"UNSUBSCRIBE"`), `filters` (a tuple of `RequestedFilter`, in the packet's
order) and `topic_filters` (those filters' strings). A `RequestedFilter` has `filter`,
`requested_qos`, the QoS the device asked for (0, 1 or 2), and `granted_qos`, the one SUBACK gave
(the lower of that and 1); both QoS fields are `None` in an UNSUBSCRIBE. A SUBSCRIBE again to a
filter already held is a record of its own. An UNSUBSCRIBE of a filter that was never subscribed is
recorded as sent. A malformed SUBSCRIBE or UNSUBSCRIBE, for example one with no filter, a zero
packet ID, a string that is not UTF-8 or holds a NUL, or, in a SUBSCRIBE, an invalid filter or a
QoS above 2, is refused whole: it records nothing, changes no subscription, even of the filters
before the bad one, and closes the connection, logging
`mqtt: malformed or unsupported packet, closing`. `RequestedFilter` and `SubscribeRequest` are
imported from `esp32tl`.

`next_subscribe()` returns the first SUBSCRIBE appended after the call, with the boundary of the
other `next_*` conditions; `next_subscribe(filter)` the first that names exactly that filter, as
`subscribed(topic)` compares it, not by wildcard matching, so `next_subscribe("a/#")` does not
match a SUBSCRIBE to `a/b`. An UNSUBSCRIBE never matches it. To wait for the device to subscribe
again after a reconnect, make the condition before the action that disconnects it:

```python
resubscribed = broker.next_subscribe("device/logger-1/cmd/#")
broker.disconnect(DEVICE)
request = board.wait_until(resubscribed, within=seconds(60))
assert request.connection_id != before.connection_id
```

## Reject MQTT connections and check credentials

Set `broker.connection_policy` before power-on or between board advances. It receives an
immutable `Connect` record and must return a member of `ConnackCode`, both imported from
`esp32tl`. The same policy applies to plain TCP and TLS listeners using this broker.
It runs once per supported, fully parsed CONNECT; it must not block or wait on the board.
Changing it affects future attempts only, leaving existing clients connected. Assign `None`
to restore the accepting default. Behind a Wi-Fi access point the same policy is set on
`network.broker`. A directly constructed `Broker` also accepts the keyword
`connection_policy=`.

For firmware configured to retry, first refuse connections, then enable credential checking:

```python
from esp32tl import ConnackCode, Connect
from esp32tl.units import seconds

broker.connection_policy = lambda connect: ConnackCode.SERVER_UNAVAILABLE
attempt = broker.next_connection_attempt()
board.power_on()
refused = board.wait_until(attempt, within=seconds(60))
assert refused.return_code is ConnackCode.SERVER_UNAVAILABLE
assert not broker.clients


def authenticate(connect: Connect) -> ConnackCode:
    if (connect.username, connect.password) == ("device", b"secret"):
        return ConnackCode.ACCEPTED
    return ConnackCode.BAD_USERNAME_OR_PASSWORD


# Provision those credentials through the firmware's own configuration interface.
connected = broker.next_connection()
broker.connection_policy = authenticate
client = board.wait_until(connected, within=seconds(60))
assert client.username == "device"
```

A stateful policy can reject just the next attempt, without relying on elapsed host time:

```python
outcomes = iter([ConnackCode.SERVER_UNAVAILABLE])
broker.connection_policy = lambda connect: next(outcomes, ConnackCode.ACCEPTED)
```

These are MQTT 3.1.1 CONNACK return codes, not MQTT 5 reason codes:

| `ConnackCode` member | Wire value |
|---|---|
| `ACCEPTED` | 0 |
| `UNACCEPTABLE_PROTOCOL_VERSION` | 1 |
| `IDENTIFIER_REJECTED` | 2 |
| `SERVER_UNAVAILABLE` | 3 |
| `BAD_USERNAME_OR_PASSWORD` | 4 |
| `NOT_AUTHORIZED` | 5 |

The policy can simulate any of these responses to a supported MQTT 3.1.1 request. Unsupported
protocol versions and malformed packets still close without invoking it or sending CONNACK.
An empty client ID without CleanSession is rejected with `IDENTIFIER_REJECTED` before the policy;
the policy cannot override that rule. Every rejection sends CONNACK with Session Present zero,
then closes. It cannot take over another client, route pipelined publications, subscribe, emit a
Will or touch a stored session, even with CleanSession=1: the policy runs before any session state
is installed or discarded.

`Connect` contains `at` (`Duration`, on the board clock), `peer` (IP string and source port),
`client_id`, `clean_session`, `username` (text or `None`), `password` (bytes or `None`),
`keepalive` (seconds), `will_topic`, `will_payload`, `will_qos` and `will_retain`. Absent Will
topic/payload are `None`; QoS and retain are zero/false. The client ID is exactly what the device
sent, including an empty string; an anonymous ID is allocated only after acceptance and appears
in `clients`.

Each immutable `ConnectionAttempt` in `connection_attempts` has `connect` (the metadata),
`return_code`, `session_present` (what its CONNACK said), `at` (the CONNECT timestamp) and
`accepted` (boolean). This includes the built-in empty-ID rejection. Histories survive connection loss and network reset. `clients` and
`next_connection()` include accepted connections only. `next_connection_attempt()` observes future
outcomes without consuming them; a timeout names `the next MQTT connection attempt`. Malformed or
incomplete CONNECTs, TCP accepts and TLS handshake failures do not create outcomes.

A policy that raises, or returns anything other than a `ConnackCode` (including a bare integer
or boolean, which raises `TypeError("MQTT connection_policy must return a ConnackCode")`), is a
test error. It sends no CONNACK, records no outcome and does not become a simulated
authentication failure. In a board test the policy runs in the link, not in the test: the link
fails and the run ends, so the waiting `board.wait_until(...)` raises `RunEnded`, not the
policy's exception. Its message names the error, as in
`the link failed: uart.modem.receive raised TypeError: MQTT connection_policy must return a ConnackCode`
(`wifi.receive` behind an access point), and the traceback is in the link's report. Only a
`Broker` driven directly, without a board, raises the policy's own exception from `on_data`, after
closing that connection.

## Keep a session across connections

A client that connects with CleanSession=0 keeps its session on the broker, by client ID, from one
connection to the next. Build the validation firmware with `firmware/build.sh mqtt_session`
([validation firmware](validation-firmware-user.md#mqtt_session)): esp-mqtt with
`disable_clean_session`, which subscribes on every connection and prints Session Present and each
message's DUP. A test publishes while the client is offline, and it arrives after it reconnects:

```python
import pytest
from esp32tl.units import seconds

pytestmark = [
    pytest.mark.app_path("../firmware/mqtt_session"),
    pytest.mark.board_profile("lilygo-t-a7670g-r2"),
]


def test_receives_what_was_published_while_it_was_offline(board, broker, console):
    board.power_on()
    board.wait_until(broker.subscribed("session/commands"), within=seconds(60))
    reconnected = broker.next_connection()
    received = console.line(r"data dup=0 qos=1 .* payload=while offline")
    broker.disconnect("session-client")
    broker.publish("session/commands", "while offline", qos=1)
    client = board.wait_until(reconnected, within=seconds(30))
    assert client.session_present
    board.wait_until(received, within=seconds(5))
```

To lose a PUBACK, publish and disconnect in the same instant. The PUBLISH and the close travel in
that order, so the firmware receives the message and answers it; the PUBACK comes to a connection
already closed, which counts for nothing, and the message comes again on the next connection:

```python
broker.publish("session/commands", "in flight", qos=1)
broker.disconnect("session-client")
first = console.line(r"data dup=0 qos=1 id=1 .* payload=in flight")
again = console.line(r"data dup=1 qos=1 id=1 .* payload=in flight")
```

What the broker does on each CONNECT it accepts ([MQTT 3.1.1](https://docs.oasis-open.org/mqtt/mqtt/v3.1.1/os/mqtt-v3.1.1-os.html)):

| CONNECT | Session Present | What happens to the session |
|---|---|---|
| CleanSession=0, a stored session | 1 | Resumed: after CONNACK, first what the client has not acknowledged, in the order first sent, with DUP=1 and the same packet IDs; then the queue, in order, with DUP=0 and new packet IDs (MQTT-3.1.2-4, MQTT-3.2.2-2, MQTT-4.4.0-1, MQTT-4.6.0-6) |
| CleanSession=0, none stored | 0 | A new session, kept after the connection ends (MQTT-3.2.2-3) |
| CleanSession=1 | 0 | Any stored session discarded, and a new one that ends with the connection (MQTT-3.1.2-6, MQTT-3.2.2-1) |

A client still connected with the same ID is disconnected first (MQTT-3.1.4-2), and the log says
`mqtt: c (connection 2) took over connection 1`. If that connection had CleanSession=1 its session
ends with it, so a CleanSession=0 CONNECT finds none. Its Will is published at the takeover when
either connection has CleanSession=1; when both have CleanSession=0 the session goes on and the
Will is discarded unsent, as mosquitto does, and the line ends `, its Will discarded`: a device back
before its keepalive ran out [has not gone](#a-lost-link-and-the-will).

While a client with a stored session is offline:

- a QoS 1 publication that matches one of its subscriptions is queued, in order, whatever QoS the
  subscription has (MQTT-3.1.2-5), and goes at the subscription's QoS when it reconnects. The queue
  has no limit, because the specification sets none. What packet IDs limit is what is unacknowledged
  at once: see [below](#a-client-with-no-free-packet-id);
- a QoS 0 publication is dropped, logged as
  `mqtt: session-client is offline: dropped a QoS 0 message on session/commands`;
- `subscribed` is false: it speaks of active clients. It is true again from the CONNECT on, without
  a SUBSCRIBE.

A queued message goes with RETAIN 0, since it matches an established subscription (MQTT-3.3.1-9),
and DUP 0, since it is a first attempt (MQTT-3.3.1-3). A retained message that a SUBSCRIBE matched
and that is waiting for a packet ID keeps RETAIN 1 (MQTT-3.3.1-8). A message sent again keeps the
RETAIN it had.
A SUBSCRIBE that repeats a filter replaces its QoS and sends matching retained messages again,
leaving what is unacknowledged as it is (MQTT-3.8.4-3). It leaves the queue as it is too, unless
no packet ID is free: then a matching retained message published at QoS 1 can join the queue, but
one still waiting from an earlier SUBSCRIBE is not queued twice
([below](#a-client-with-no-free-packet-id)).

A session lasts until a CleanSession=1 CONNECT with that ID or `forget_session`. It survives a
disconnection of any kind, keepalive expiry, and the network's reset: the broker is the test's,
not the link's, like its retained messages. A session never expires, and a refused CONNECT changes
nothing. An incoming QoS 1 `Publication` stays with its connection and is not part of the session:
a pending one is cancelled when the connection closes and does not come back
([below](#withhold-puback-and-disconnect-a-client)).

### A client with no free packet ID

A client has at most 65535 QoS 1 messages unacknowledged at once, one per packet ID (MQTT-2.3.1-2).
When all of them are in use, a connected client is not disconnected: a message that would go to it
at QoS 1 waits in its session's queue, and each PUBACK sends the next one, in order. This is the
same whether the message is published now, is a retained one that a SUBSCRIBE matched, or was
queued while the client was offline and its session was resumed. A message published at QoS 1 while
the queue waits goes behind it, even to a QoS 0 subscription, to keep its place in the order; it
takes no packet ID, so it goes as soon as what is in front of it has gone. A
message published at QoS 0 needs no packet ID and goes at once, and so does one published at QoS 1
to a QoS 0 subscription when nothing is waiting yet. A retained message that a SUBSCRIBE matches
follows the same rules, except that one already waiting from an earlier SUBSCRIBE is not queued
again: that copy is the one sent again, so a client that subscribes on every connection gets it
once when it acknowledges, not once per connection. A retained message that has changed since is
another message, and joins the queue. The broker logs the start of each wait once:

```text
mqtt: session-client has no free packet identifier: waiting for a PUBACK, 1 queued
```

With CleanSession=1 the queue ends with the connection, like the rest of the session; with
CleanSession=0 it stays, and waits on the next connection until a PUBACK frees an ID. A client that
never acknowledges keeps its connection and its queue grows without limit; a test sees it in that
log line and in `broker.session(client_id).queued`.

`broker.session(client_id)` returns the state now, a frozen `SessionState`, or `None`. Its dict and
tuples are copies, so they stay as they were at the call; the `Delivery` records in `unacknowledged`
are the broker's own, so a later PUBACK still sets `acknowledged_at` on them:

| `SessionState` | Meaning |
|---|---|
| `connected` | Whether a connection holds this session now |
| `subscriptions` | Filter to granted QoS, a `dict` |
| `queued` | The `Message`s waiting for the client, oldest first: while it is offline, or while it is connected with no free packet ID. Each is the `Message` as published, so its `retain` is the publisher's flag, not the one it will go with |
| `unacknowledged` | The `Delivery`s the client has not acknowledged, in the order first sent |

A `Delivery` in `broker.deliveries` is one PUBLISH written to a client's stream, stamped when the
broker wrote it: `at`, `client`, `connection_id`, `topic`, `payload`, `qos`, `packet_id` (`None` for
QoS 0), `dup`, `retain`, `text` and `acknowledged_at`. The last is when the PUBACK for this PUBLISH
reached the broker on this connection, or `None`: always for QoS 0, and for a delivery whose
connection closed first, whose retransmission has a record of its own. A `next_delivery` condition
has the boundary, polling and naming of the
[MQTT next-event conditions](#history-next-events-and-current-state).

## Give the network a round trip and a rate

```python
from esp32tl.units import kbit_s, ms

network = modem.network()  # Or what wifi.access_point(...) returned.
network.set_path(rtt=ms(200), downlink=kbit_s(512), uplink=kbit_s(128))
```

`set_path` puts a path between the link and the network, for every IPv4 datagram either way: DNS,
NTP, TCP and so MQTT and HTTP. What the device sends at time `t` is answered at exactly `t + rtt`,
plus the time the datagrams take at the rates. LCP, IPCP, ARP and DHCP stay on the link and are not
delayed, so the device still comes up in the same time; each such answer comes 1 ms after its
request, even past datagrams on their way. These values are the test's parameters, not a model of
an operator: no vendor source stands behind them.

| Argument | Default | Meaning |
|---|---|---|
| `rtt` | `ms(1)`, the network without a path | From the device's request to the stamp of the answer. A `Duration`, 1 ms at the least |
| `downlink` | `None`, no limit | A `Rate` towards the device: each datagram takes its size in bits at this rate, one after another |
| `uplink` | `None`, no limit | The same from the device |

`Rate`, `kbit_s` and `mbit_s` are [units](models-user.md#units). Each call states the whole path:
what it leaves out goes back to its default. It works before power-on and between board advances,
and it does not advance time. It applies to datagrams entering after it, and none overtakes another:
a datagram behind one that left under a longer RTT leaves with it, not before it.

An RTT below the floor raises `ValueError`:
`network.set_path: rtt is 0.5 ms; the network answers 1 ms after a request at the least`.
Something else raises `TypeError`: `network.set_path: rtt is 200, not a Duration, as ms(200)`,
`network.set_path: downlink is 512, not a Rate, as kbit_s(512)`. The transcript logs each call, as
`net: path: rtt 0.2 s, downlink 512 kbit/s, uplink 128 kbit/s`, with `any` for no limit.

A broker's records have the times of the network's end: `message.at` is `t` plus what the datagram
took going up, half of what the RTT has above 1 ms, and `puback_at`, `delivery.at` and the test's
`broker.publish` are when the answer sets off down. A publication from the test reaches the device
one way down later, half of what the RTT has above 1 ms, and 1 ms. A record is there when `board.now`
reaches its stamp: `board.wait_until(broker.next_message("a/b"), ...)` returns with `board.now`
equal to `message.at`, and what the test does at that instant comes after it. The broker's
deadlines are the same: a delayed PUBACK and a keepalive expiry are there at the instant they are
due ([Withhold PUBACK](#withhold-puback-and-disconnect-a-client)). On a path, a
`connection_policy` that raises fails the link as `uart.modem.arrive raised`
(`wifi.arrive` behind an access point) when its CONNECT took time going up.

Behind the modem the runner already paces received bytes at the UART's baud, about 11.5 kB/s (92
kbit/s) at 115200: a `downlink` above that adds its time per datagram and is no limit on the rate
of what arrives. Only a lower one is. The path has no queue limit and loses nothing. A reset, of the
link or of the network, drops what is on the path and keeps the path's values.

What the firmware does with the time is its own. With `firmware/mqtt_session`, publishing a QoS 1
backlog one message at a time, each waiting for its PUBACK, on the modem's route:

| `rtt` | From one report to the next, measured |
|---|---|
| default | 16.3 ms |
| `ms(200)` | 215.3 ms |
| `ms(600)` | 615.2 ms |
| `ms(200)`, 8 kbit/s each way | 361.9 ms |

The 15 ms above the RTT are the modem's UART and the firmware. In that run the first report comes
two round trips after the command, not one: lwIP's Nagle algorithm holds it behind the command's
PUBACK until that is acknowledged. At 600 ms that also passes esp-mqtt's 1 s
`message_retransmit_timeout`, so the first report comes twice, the second time with DUP.

The firmware's own timers can outrun a path: lwIP retransmitted segments at an RTT of 1.5 s, and
the network answers each with an acknowledgement. Over a path whose rate is too low for what it
carries, a client's timeouts can fire before a message has got through: the path has no queue limit
and loses nothing, so each retransmission only adds to what it has to carry. esp-mqtt's
`network.timeout_ms` is 10 s by default.

## Fetch a page behind a Wi-Fi access point

Build ESP-IDF's unmodified `http_request` example with `firmware/build.sh http_request`
([validation firmware](validation-firmware-user.md#http_request)). It joins the access point with
ESP-IDF's default SSID `myssid` and passphrase `mypassword`, resolves `example.com` and sends
`GET / HTTP/1.0` to it on port 80. The `wifi` fixture puts that access point on the air before
power-on and returns the network behind it ([the wifi fixture](pytest-plugin-user.md#the-wifi-fixture)):

```python
import pytest
from esp32tl.units import seconds

pytestmark = pytest.mark.app_path("../firmware/http_request")


def test_joins_and_fetches_from_the_tests_server(board):
    network = board.wifi.access_point("myssid", password="mypassword")
    network.http.serve("/", "hello from the test\n")
    printed = board.console.line(r"^hello from the test$")
    board.power_on()
    request = board.wait_until(network.http.next_request("/"), within=seconds(30))
    assert (request.method, request.version) == ("GET", "HTTP/1.0")
    assert request.header("Host") == "example.com:80"
    board.wait_until(printed, within=seconds(5))
    assert network.dns.asked == ["example.com"]
    [lease] = network.dhcp.leases
    assert lease.address == "10.64.0.2"
```

In `tests/test_http_request.py` the station joins at about 2.79 emulated seconds, takes its DHCP
lease at 2.80 s and sends its request at 4.13 s; the whole test takes under a second of wall time
on an M-series Mac. Without an access point the station finds nothing to join, and with a wrong
password it never gets a lease.

`wifi.access_point(...)` returns an `Internet`, the same class `modem.network()` returns, so the
broker, DNS, NTP, `listen` and TLS below work the same behind the access point. The `broker`
fixture is the modem's; behind the access point the broker is `network.broker`. `network.ppp`
exists only behind the modem and `network.ethernet` only behind the access point; the other
raises `AttributeError` (`this network is behind a Wi-Fi access point: it has no PPP`,
`this network is behind a modem: it has no Ethernet`).

## The HTTP server

Every network, behind the modem too, runs an HTTP/1.1 server on TCP 80, at every destination
address, as `network.http`. Until the test serves a path, every request gets `404 Not Found`.

| HTTP server API | Meaning |
|---|---|
| `serve(path, body=b"", *, status=200, content_type=None, headers=None)` | Answer every request for `path` from now on, replacing what was served there before. A `str` body goes as UTF-8 with `text/plain; charset=utf-8`, `bytes` with `application/octet-stream`, unless `content_type` names another; `headers` (a dict) are added as given |
| `next_request(path=None, *, method=None)` | Condition returning the first request, for `path` and with `method` when given, recorded **after this method call** |
| `find_request(path=None, *, method=None)` | Immediate history query returning the first recorded request with the same filters, or `None` |
| `requests` | Every request recorded, in order; refused requests are not |

A request (`HttpRequest`) has `at` (`Duration`), `method`, `target` (as sent), `path` (an
origin-form target such as `/a?b` without its query, `://` in it or not; an absolute-form one such
as `http://host/x?b` without its scheme, host and query, `/x`; `*` and `host:port` as they are), `version` (`HTTP/1.0` or
`HTTP/1.1`, as sent), `headers` (name and value pairs, in order), `body` (bytes) and `text` (the
body as UTF-8 with replacement). `header(name)` returns the first field of that name, whatever its
case, or `None`.

`path` is compared with the request's `path` and `method` with its method exactly as sent, so
`method="get"` does not match `GET`. A `next_request` condition has the boundary, polling and
naming of the [MQTT next-event conditions](#history-next-events-and-current-state): construct it
before the action that makes the firmware send the request; requests already recorded are
excluded, even at an equal board time; polling consumes nothing, so repeated calls return the same
first match. A timeout names it: its message begins
`the next HTTP request for '/' with method 'GET' did not happen within 30 s`, and the run report
logs `waits up to 30 s for the next HTTP request for '/' with method 'GET'`. `find_request`
returns a record now, not a condition, and never waits. Boolean coercion of a condition from
`next_request` raises
`TypeError("call the HTTP condition with (), or pass it to board.wait_until")`, before and after a
match, so an unevaluated `assert network.http.next_request("/")` fails. Use
`assert network.http.find_request("/") is not None` to inspect past requests.

A `path` filter does not see the query, which is in the request's `target`. `next_request` and
`find_request` raise `ValueError` for a filter that holds `?`
(`http.find_request: '/update?v=2' holds a query, which a path does not: filter by the path and read the request's target`):
filter by `/update` and compare `request.target` with `/update?v=2`. The refused requests listed
below are not recorded, so no condition or query sees them: a timeout says the request
did not happen although the firmware sent it, and only the transcript's
`http: refused a request: <status> <reason>` shows it.

The server takes one request per connection. It answers `HTTP/1.1 <status> <reason>`,
`Content-Type`, `Content-Length`, `Connection: close` and the test's headers, then the body (none
for `HEAD`, whose `Content-Length` is still the body's), and closes the connection. A `204` or
`304` ends with its head: no body, and no `Content-Type` or `Content-Length` (RFC 9112 6.3). A path nobody
serves gets `404 Not Found` with the body `not found\n`, and is kept in `requests`. The transcript
logs each answer as `http: GET / → 200`. For HTTPS, put the same server behind TLS on another port:
`network.listen(443, network.http, tls=ca)` ([TLS](#dns-time-and-services)).

These requests are refused, logged as `http: refused a request: 400 Bad Request` (with their own
status), answered with the reason in lower case as the body, and not kept in `requests`:

| Status | Request |
|---|---|
| `400 Bad Request` | A malformed request line (not three parts, a method that is not letters, a version that does not start `HTTP/1.`) or header field; `Content-Length` fields that disagree, or one that is not ASCII digits |
| `413 Content Too Large` | A body over 1 MiB |
| `431 Request Header Fields Too Large` | A request head over 64 KiB |
| `501 Not Implemented` | Any `Transfer-Encoding` |

`serve` raises `ValueError` for a path that does not start with `/`
(`http.serve: 'index.html' is not a path, which starts with /`) or that holds a query
(`http.serve: '/a?b' holds a query, which a path does not: serving the path answers it whatever the query`), a status the server has no reason
for (`http.serve: status 418 is not one of [200, 201, 204, 301, 302, 304, 400, 401, 403, 404, 413, 431, 500, 501, 503]`),
a header whose name is empty or holds `:`, or whose name or value holds a line break
(`http.serve: 'X-A:B': '1' is not a header field`), and a body with a `204` or `304`
(`http.serve: a 204 response has no body`).

## Addresses behind the access point

The DHCP server, at `10.64.0.1`, gives the device `10.64.0.2/24` (mask `255.255.255.0`), router
`10.64.0.1` and DNS `10.64.0.1`, with a lease of 86400 s. It offers that address to every DISCOVER,
acknowledges a REQUEST for it (or for no address), and answers a REQUEST for another address with a
NAK. A REQUEST that names another server is left unanswered; DECLINE and RELEASE are logged and not
answered. A DISCOVER, a REQUEST not acknowledged, a RELEASE or a DECLINE takes away the address the
device had (an INFORM, or a type the server does not take, changes nothing): the connections the
test opened to it are lost, `lost when the device gave its address up`, and the device's own, which
lwIP drops with the address, end without a FIN when their service closes them, as [after a lost
link](#a-lost-link-and-the-will). A SYN the device sent before it gave the address up, still on the
network's path, never arrives. The transcript shows `dhcp: DISCOVER from 24:6f:28:00:11:22 → OFFER
10.64.0.2` and `dhcp: REQUEST from 24:6f:28:00:11:22 → ACK 10.64.0.2`.

`network.dhcp.leases` lists every acknowledged lease, each a `Lease` with `at` (`Duration`), `mac`,
`address` and `hostname` (the name the device sent, `espressif` for ESP-IDF's default, or `None`).
`network.dhcp.next_lease()` is a condition returning the first lease recorded **after this method
call**: it is how a test waits for the station to join again. `network.dhcp.find_lease()` is an
immediate history query returning the first recorded lease, or `None`. A `next_lease` condition has
the boundary and polling of the [MQTT next-event
conditions](#history-next-events-and-current-state); its timeout names `the next DHCP lease`, and
boolean coercion raises
`TypeError("call the DHCP condition with (), or pass it to board.wait_until")`, before and after a
lease. The leases outlive the access point switching off and on. `network.dhcp` exists behind the
modem too, where nothing asks it.

ARP for any address but the device's own is answered with the network's MAC,
`02:53:49:4d:00:02`, so every address on the subnet and off it reaches the network. ARP for
`10.64.0.2` itself gets no answer: lwIP probes that address before taking the lease, and an answer
would make it decline. The network learns the device's MAC from the frames it sends.

The network speaks IPv4 only. Frames of any other type are dropped, and the first of each type is
logged: ESP-IDF's IPv6 link-local traffic gives
`ethernet: dropping frames of type 0x86dd (IPv6)`. The MTU is 1500 bytes; a packet for the device
over it is dropped with `ethernet: dropped an IPv4 packet of <n> bytes: over the MTU`.

## Open a TCP connection to the device

Firmware that serves HTTP, WebSocket or a TCP protocol of its own is tested from its client's
side: `network.connect(port)` opens a TCP connection to the device's `port`: its SYN enters the
network's path at the board's current instant and reaches the device after the path's way down,
1 ms by default ([the path](#give-the-network-a-round-trip-and-a-rate)). It works the same behind the access point and behind the modem.
Build `firmware/build.sh http_server` ([validation firmware](validation-firmware-user.md#http_server)):
a Wi-Fi station that serves `GET /status` and `POST /echo` on port 80 with ESP-IDF's
`esp_http_server`.

```python
import json

import pytest
from esp32tl.units import seconds

pytestmark = pytest.mark.app_path("../firmware/http_server")


def test_answers_a_get(board, wifi, console):
    network = wifi.access_point("myssid", password="mypassword")
    listening = console.line(r"^http_server: listening on port 80$")
    board.power_on()
    board.wait_until(listening, within=seconds(30))
    connection = network.connect(80)
    connection.write(b"GET /status HTTP/1.1\r\nHost: device\r\nConnection: close\r\n\r\n")
    board.wait_until(connection.closed(), within=seconds(5))
    head, _, body = connection.received.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200 OK\r\n")
    assert json.loads(body)["status"] == "ok"
```

Wait for the firmware to say its server listens, as its own client would wait for it: a DHCP lease
or IPCP gives the device its address, not its server, and a SYN that comes first is refused. In
`tests/test_http_server.py` the station takes its address at about 2.03 emulated seconds and the
server listens less than a millisecond later (measured with ESP-IDF v5.3.6 at esp32sim's pin in
`runner/Cargo.toml`; a bump of either can move it). What the test writes before the device accepts the connection
waits for the handshake.

| Network API | Meaning |
|---|---|
| `connect(port, *, address=None)` | Open a TCP connection to the device's `port` (1 to 65535) and return its `TcpConnection`. `address` is the device's, `device_address` unless given, as for firmware with a static address behind the access point |
| `device_address` | The device's address on the link now: the one IPCP gave it behind the modem, the one this link's DHCP acknowledged behind the access point, `10.64.0.2` either way; `None` before it has one, from the device's chip reset or power loss until IPCP opens again or DHCP acknowledges it again, and from a DISCOVER, a REQUEST not acknowledged, a RELEASE or a DECLINE of the device's until the next ACK |
| `connections` | Every `TcpConnection` the test opened, in order |

| `TcpConnection` API | Meaning |
|---|---|
| `write(data)` | Send bytes, or `str` as UTF-8: now, once the connection is established and the device's window has room, else as soon as it does |
| `close()` | Close the test's end, a FIN after what was written; a second call, or one after the connection ended, does nothing |
| `received`, `text` | Every byte the device sent so far, in order; the same as UTF-8 with replacement |
| `state` | `"connecting"`, `"established"`, then `"closed"` (both ends closed), `"refused"` (a RST answered the SYN), `"reset"` (a RST later) or `"lost"` (the link went down, or the device's chip reset or lost its power) |
| `established()` | Condition returning when the device accepted the connection, a `Duration` |
| `closed()` | Condition returning when the device closed its end (its FIN), whoever closed first |
| `refused()` | Condition returning when the device answered the SYN with a RST |
| `reset()` | Condition returning when the device reset the established connection |
| `next_data(until=None, *, size=None, start=None)` | Condition on the bytes the device sends after `start`, an offset into `received`, which is where `received` ends at the call unless given: with `until` (bytes or text), returns them up to and including the first `until` among them; with `size`, the first `size` of them; with neither, whatever has arrived, once anything has |
| `address`, `port`, `local`, `local_port` | The device's address and port, and the test's: `10.64.0.1` and the connection's ephemeral port |
| `at`, `established_at`, `closed_at`, `ended_at` | When the test connected, the device accepted, the device closed its end and the connection ended, each a `Duration` or `None` |
| `identity` | `device:<port>/<n>`, the connection's stream in the transcript |

A connection is established, closed, refused or reset once at most, so those four conditions read
its state: one returns its event's time however long ago it happened. `next_data` is a next-event
condition, with the boundary and polling of the [MQTT next-event
conditions](#history-next-events-and-current-state) at `start`. Every condition fails loudly: once it
can no longer hold, it raises `ConnectionEnded` (an `AssertionError`, from `esp32tl`)
instead of waiting out its time. Waiting for `established()` on a connection the device refused
raises `TCP 10.64.0.1:49152 → 10.64.0.2:8080 was refused by the device before it was established`;
`closed()` on a reset one, `... was reset by the device, not closed by the device`; `refused()` on
an accepted one, `... was established, not refused by the device`; `next_data(until=b"!")` once
the device closed without sending it, `... was closed by the device before the device sent b'!'`;
and once the connection is lost, `... was lost with the link`, `... was lost when the device
reset` or `... was lost when the device lost its power`, then what can no longer happen, as
`before it was established`. A timeout names the condition: `TCP 10.64.0.1:49152 → 10.64.0.2:80
established did not happen within 1 s`, and for `next_data`, `the next data on TCP
10.64.0.1:49152 → 10.64.0.2:80 up to b'\r\n\r\n'`, with `after byte <start>` when `start` is
not 0 and `, <size> bytes` for `size`. Boolean coercion raises
`TypeError("call the TCP condition with (), or pass it to board.wait_until")`.

A response read by its length takes `start`, because the segment that brought a head may bring the
body's first bytes with it, and a new condition's boundary would leave those out:

```python
start = len(connection.received)
connection.write(b"GET /status HTTP/1.1\r\nHost: device\r\n\r\n")
head = board.wait_until(connection.next_data(until=b"\r\n\r\n", start=start), within=seconds(5))
length = int(head.split(b"Content-Length: ")[1].split(b"\r\n")[0])
body = board.wait_until(
    connection.next_data(size=length, start=start + len(head)), within=seconds(5)
)
```

`write` raises `ConnectionEnded` once the connection ended
(`... was reset by the device: nothing more reaches the device`) or the device closed its end,
which closes the test's end too (`... was closed by the device, which closed the test's end too`):
there is no half-close. It raises `ValueError` after `close()` (`...: written after close()`) and
`TypeError` for what is not bytes or text. `next_data` raises `ValueError` for an empty `until`, a
`size` under 1, both at once (`...: next_data takes until or size, not both`), and a `start` beyond
`received`.

`connect` raises `NotReachable` (a `RuntimeError`, from `esp32tl`) when the network
cannot reach the device yet, and `ValueError` for a port out of range
(`network.connect(0): the TCP port must be between 1 and 65535`) or an address that is not IPv4
(`network.connect(80, address='device'): 'device' is not an IPv4 address`):

| When | `NotReachable` message |
|---|---|
| The access point is off the air | `network.connect(80): the access point is off the air: the network reaches no device` |
| The modem has not dialled | `network.connect(80): the modem has not dialled: the network reaches no device` |
| Behind the modem, before IPCP is open | `network.connect(80): the device has no address yet: PPP has not opened IPCP` |
| Behind the modem, IPCP still open from before the device reset | `network.connect(80): the device has no address yet: PPP has not opened IPCP since the device reset` |
| Behind the access point, no DHCP ACK on this link since the device's last reset or DHCP message | `network.connect(80): the device has no address yet: DHCP has acknowledged none on this link; wait for network.dhcp.next_lease(), or give address= for a static one`, with ` since the device last reset or gave its address up` after `this link` once DHCP has acknowledged an address on this link: not on a fresh one after `wifi.access_point_on()` |
| Behind the access point with `address=`, before the station sent a frame | `network.connect(80, address='10.64.0.9'): the station has sent no frame yet: the network has no MAC to send to` |

The test's side is `10.64.0.1`, from ephemeral ports counted from 49152 and never reused in the
network's life, and its initial sequence number is fixed, so runs repeat. It offers an MSS of 1400
and no other option, takes the device's MSS up to 1400 (536 when it offers none), and respects the
device's window: a zero window holds what the test wrote until the device announces room, and
nothing probes it. It does not retransmit and keeps no timers, and has no TIME_WAIT. A segment
from the device ahead of its stream, which a link that loses nothing cannot produce, fails the run:
the transcript logs `net: TCP 10.64.0.1:49152 → 10.64.0.2:80: the device sent sequence 5, 4 bytes
ahead of 1, which a link that loses nothing cannot produce`, and behind the access point the
link's error is `wifi.receive raised OutOfOrder: ` and that line. Losing the link, as `wifi.access_point_off()` or
the modem's loss of registration does, ends every connection as `lost`, and so does the device's
chip reset, whatever caused it (`board.reset()`, its own restart, a wake from deep sleep), and
`board.power_off()`: the device's TCP is gone, and nothing would tell the test otherwise until it
wrote. These are the connections the test opened; the device's own [stay
open](#a-lost-link-and-the-will). After `board.reset()` the device has no address until it takes one
again, so wait for its
server to listen again before connecting. Deep sleep ends them at the wake, the reset the
runner reports, not when the chip goes to sleep: a `closed()` or `next_data()` waited for with a
`within` shorter than the sleep times out. The connections the device opened are not ended by its
reset, as no network would know of it, nor by the link's loss: they go on until a RST from the
device's new stack, which answers what their service writes, or the service's end, as the broker's
keepalive. A SYN the new stack sends from the same port to the same service resets the old
connection, logged `reset: the device opened a new connection from its port`, and opens a new one,
as the RST a challenge ACK draws from the new stack would (RFC 9293, 3.10.7.4). Why it is this
TCP:
[the test opens TCP connections to the
device](../decisions/harness.md#the-test-opens-tcp-connections-to-the-device-on-the-same-tcp-with-no-timers).

The transcript and the report carry the connection as they carry the device's: its bytes as stream
`device:80/1`, `out` from the device as they arrive and `in` to it as each piece goes onto the
network's path, so what waits for the handshake or for the device's window is there when it goes,
and what never goes is not there; and the lines
`net: TCP 10.64.0.1:49152 → 10.64.0.2:80 connecting`, `... established`, then `... closed`,
`... refused by the device`, `... reset by the device`, `... link lost`, `... lost: the device
reset` or `... lost: the device lost its power`.

## Withhold PUBACK and disconnect a client

Set `broker.puback` before the publication arrives. Holding an acknowledgement still records,
routes and, when requested, retains the publication immediately. For firmware publishing QoS 1:

```python
broker.puback = "hold"
received = broker.next_message("telemetry/position")
board.power_on()
message = board.wait_until(received, within=seconds(60))
ack = message.publication
assert ack is not None and ack.status == "pending"
assert ack.puback_at is None
board.wait(seconds(2))
assert ack.release()  # Emit this exchange's PUBACK at the held board time.
assert ack.status == "emitted" and ack.puback_at == board.now
```

`broker.puback = seconds(2)` instead schedules PUBACK two emulated seconds after the first
receipt. `seconds(0)` behaves like `"immediate"`. The delay participates in the network's next
deadline; waiting advances it, host time does not. When `board.now` reaches the deadline the ACK is
emitted, `puback_at` equal to it, so a `board.wait_until` whose window ends exactly there finds it.
Manual `release()` can emit a scheduled ACK early; at the deadline's instant or after, it returns
`False`. A policy change applies only to new exchanges: it neither releases existing held handles
nor reschedules delayed ones. Negative durations raise `PUBACK delay must be nonnegative`;
other values raise `PUBACK policy must be 'immediate', 'hold', or a Duration`.

Each `Publication` exposes `connection_id`, `packet_id`, `messages` (a tuple of its receipts),
`status` (`"pending"`, `"emitted"` or `"cancelled"`), `puback_at` (`Duration` or `None`) and
`release()`. Obtain it from `message.publication` or select one from `broker.pending`; for
type annotations, import `Publication`, like `Message` and `MqttClient`, from `esp32tl`.
Successful release of a pending handle returns `True` once. If the stream closes during the
write (for example on a TLS write error), the handle is cancelled and release returns `False`.
Releasing an emitted or cancelled handle also returns `False` and sends nothing. These
observations and controls do not advance time. Every emission, immediate, delayed or released,
adds `mqtt: CLIENT_ID (connection N) PUBACK PACKET_ID emitted` to the network log.

Every incoming PUBLISH is recorded and routed, including duplicates. Receipts using the same
packet ID on the same connection **while its PUBACK is pending** share one handle and the first
receipt's deadline. Their individual `at` and `dup` remain visible in `messages`; DUP is an
observation, not proof of an earlier receipt. After emission, the same ID starts a new handle,
even with DUP set. A client must not reuse a pending ID for a different exchange: the broker
cannot distinguish that misuse from a retransmission, so it still joins the receipt to the
pending handle. When the topic or payload differs from the first receipt, it also logs
`mqtt: CLIENT_ID (connection N) reused pending packet ID PACKET_ID for a different publication`.
This model permits duplicate application messages; it promises no exactly-once delivery or
content deduplication.

`broker.disconnect(message.client)` closes only that client's active stream, including when it uses
TLS. It publishes the Will once, cancels pending ACKs, makes the client's subscriptions inactive
(with CleanSession=1, removes them) and leaves PPP, other streams and DNS/NTP usable. Graceful MQTT
DISCONNECT cancels ACKs but suppresses the Will. The connection's end, by transport loss, keepalive
expiry or client-ID takeover, also cancels pending ACKs. A lost link does not by itself: the
connection stands until its keepalive, and a delayed PUBACK due before then is emitted, to a link
that is down ([the Will](#a-lost-link-and-the-will)). A keepalive expires at 1.5 times the keepalive
after the client's last packet; when `board.now` reaches that instant the connection is closed, its
Will, when the client set one, is in `messages`, and `broker.disconnect` at that instant returns
`False`. Expiry wins if keepalive and PUBACK have the same deadline. An already emitted ACK stays
emitted; closing does not retract bytes already queued in the transport.

A reconnect gets a new `connection_id`. Reusing a client ID and packet ID cannot make an old
handle valid again. Retained messages and receipt history survive; a pending exchange does not,
even with CleanSession=0, whose [session](#keep-a-session-across-connections) holds what the broker
sends the client, not what the client sent. The client can retransmit, producing a new receipt and
handle.

Keep three observations separate: `message.at` proves broker receipt; `puback_at` proves that
PUBACK was handed to the stream (before TCP buffering, simulated link latency and device
scheduling); neither proves firmware processing or durable delivery bookkeeping. After emission,
wait for a device-visible result appropriate to that firmware to establish processing. The
broker cannot assert that the device committed delivery. The shipped `pppos_client` and position
tracker publish QoS 0; `mqtt_session` subscribes at QoS 1, so it answers a PUBACK for each message.

## A lost link and the Will

A device that loses its power or its coverage sends no FIN and no RST, so a broker on the internet
goes on holding the connection: it publishes the Will only when the keepalive runs out, 1.5 times
the keepalive after the device's last packet (MQTT 3.1.1, 3.1.2.10). The network does the same.
`board.power_off()`, `modem.lose_registration()`, `modem.lose_power()`, `modem.hang()`,
`modem.drop_call()`, PPP ended by the device and `wifi.access_point_off()` end the link; the
broker's end of each connection the device opened stays open and silent,
`broker.session(...).connected` stays true, and the keepalive expiry closes it and publishes the
Will, when the client set one, with the log lines `mqtt: session-client keepalive expired` and `net:
TCP 10.64.0.2:<port> → 10.64.0.1:1883 closed by its service with no device to answer`: no stack on
the device holds the connection any more, so the broker's end goes with no FIN. A connection whose
handshake the link's end cut short, before the device's ACK, goes at the link's end, `lost before
the device answered its SYN-ACK`, since no CONNECT, and so no keepalive, came on it. The broker's
deadlines run while the link is down, the board off included. What it writes in the meantime, a
PUBACK or a message to a subscription, goes nowhere, as on a connection whose far end is gone; a QoS
1 delivery so lost goes again with DUP=1 when the session resumes. A client with a keepalive of 0
has no deadline: its connection stays and its Will is not published until it is taken over or the
test calls `broker.disconnect`, where a real broker would end it at its own TCP keepalive.

```python
from esp32tl.units import seconds


def test_the_will_goes_at_the_keepalive(board, broker):  # keepalive 60 s
    board.power_on()
    board.wait_until(broker.subscribed("session/commands"), within=seconds(60))
    board.power_off()
    assert broker.find_message("session/status", "offline") is None
    will = board.wait_until(broker.next_message("session/status", "offline"), within=seconds(100))
    # will.at is 90 s after the device's last packet, not the instant of the cut.
```

A device back before then, with the same client ID and CleanSession=0 on both the old connection and
the new, takes its session over (MQTT-3.1.4-2): the old connection closes, its Will discarded, and
none is published, so a retained status the firmware published on connecting stays the last word.
With CleanSession=1 on either connection the Will goes at the takeover, and so it does when the
CONNECT comes at the instant the old keepalive runs out, though the session is resumed. The old
connection goes with no FIN, `closed by its service with no device to answer`. A firmware whose new
stack connects from the same port as before resets the old connection with its SYN: the Will goes
then, as at any reset, and the new connection opens ([TCP](#open-a-tcp-connection-to-the-device));
esp-mqtt in `mqtt_session` connects from another. `broker.disconnect(client_id)` ends a connection
at once, its Will published, for a test that wants the broker to give up sooner. The connections the
test opened with `network.connect` are lost at the link's end, as before: the device's end of them
is gone. Why there is no option for the old behaviour: [the
decision](../decisions/harness.md#a-lost-link-leaves-the-devices-connections-half-open-until-their-keepalive).

## DNS, time and services

Behind the modem, PPP gives the device `10.64.0.2` and DNS `10.64.0.1`; the peer is `10.64.0.1`.
Behind the access point, DHCP gives the same addresses ([above](#addresses-behind-the-access-point)).
Every destination IP answers, so TCP and UDP dispatch by destination port, regardless of the
address. DNS on UDP 53 answers every IN A query with `10.64.0.1`; other query types receive an empty answer.
`network.dns.asked` records queried names. NTP on UDP 123 tells `board.power_on(utc=...)` plus
elapsed emulated time; `network.ntp.requests` counts replies. The default UTC is the board's.
`network.ntp.go_silent()` stops answering new requests; `network.ntp.resume()` answers new
requests again using the current world time. Both work before power-on and during a run,
without advancing time. Silence does not count as a reply, queue requests for later, or cancel
replies already scheduled for delivery. Transitions appear as `ntp: silent` and `ntp: resumed`
in the network transcript; repeating either call in the same state adds no event.

PPP honors the device's negotiated maximum receive unit (MRU), default 1500 bytes, for the whole
outgoing IPv4 packet. TCP segmentation also respects the peer's MSS and receive window. The
smallest supported MRU is 68 bytes; a smaller request gets a Configure-Nak proposing 68.
A UDP reply exceeding MRU is dropped without fragmentation, with a transcript entry such as
`net: dropped UDP reply of 577 bytes: exceeds peer MRU 576 (fragmentation unsupported)`.
Renegotiation, LCP termination and modem redial clear the old MRU; omission of the option uses 1500.
Behind the access point the limit is the Ethernet MTU, 1500 bytes; an oversized UDP reply gets the
same transcript entry, which names the limit `peer MRU 1500` there too.

`network.listen(port, service)` installs one TCP service on a port (1883 already holds the
broker and 80 the HTTP server; another service there raises
`TCP port 80 already has a service`). A service implements `accept(peer, write, close)`, returning
a connection with
`on_data(bytes)` and `on_close()`. `peer` is the device's `(address, port)`; `write(bytes)` sends
back and `close()` closes. `network.ip.udp[port]` accepts a datagram service implementing
`on_datagram(bytes) -> bytes | None`. These callbacks must not block or use wall-clock waits.

Wrap any stream service in TLS by supplying a per-test CA:

```python
from esp32tl import CertificateAuthority

network = modem.network()
ca = CertificateAuthority(["broker.example"])
listener = network.listen(8883, network.broker, tls=ca)
# Provision ca.ca_pem as the firmware's trusted CA using that firmware's configuration interface.
```

With `tls=ca`, `listen` returns the `Tls` listener; without TLS it returns `None`.
`listener.handshakes_started` counts connections that have supplied their first nonempty input.
It includes incomplete, malformed and failed handshakes; it does not prove TLS success. Reading
it does not advance time, and closing a connection or resetting the network does not clear the
count. After powering on the board, a test can wait for an attempt without matching log text:

```python
board.wait_until(listener.handshake_started, within=seconds(60))
board.wait_until(listener.handshake_completed, within=seconds(60))
```

`handshake_started` and `handshake_completed` are named callable conditions on this listener's
entire history, including events before the condition was obtained. Saved conditions read the
live counts, without consuming events or advancing time. Call them directly, for example
`assert listener.handshake_completed()`, or pass them to `board.wait_until`. They stay true after
connection closure or network reset. Their timeouts name `a TLS handshake to start` or
`a TLS handshake to complete`.
`handshakes_completed` counts server-side handshake completions, once per connection; it does
not prove an MQTT connection or publication. Started input alone does not imply completion.

For reconnects, explicitly select the next event and arm it before triggering the reconnect.
Here the modem's [coverage controls](modem-user.md) take the link down and restore it; whether
and when the firmware reconnects is up to the firmware:

```python
next_started = listener.next_handshake_started()
next_completed = listener.next_handshake_completed()
modem.lose_registration()  # Ends data mode.
board.wait(seconds(5))  # Long enough for the firmware to notice the lost link.
modem.register()
board.wait_until(next_started, within=seconds(60))
board.wait_until(next_completed, within=seconds(60))
assert listener.handshake_completed()  # Cumulative history query remains available.
```

TODO(owner): the 5 seconds is a placeholder; how long the outage must last depends on how
quickly the firmware notices the lost link.

Each `next_handshake_*()` call captures its corresponding counter immediately and returns a
condition that becomes true only when that counter increases. Existing successes cannot satisfy
a new completion condition, even after a disconnect/reset. The boundary is event occurrence,
independent of timestamps; equal-time events count in processing order. A handshake already
in progress when `next_handshake_completed()` is called can complete afterwards and satisfy it.
It is not restricted to connections accepted or started after creation, nor tied to a particular
peer. Arm both conditions before reconnecting when both events matter.

Next-event TLS conditions also consume nothing: once true, repeated polls remain true. Construct
a new condition for another event. Their timeouts name `the next TLS handshake to start` or
`the next TLS handshake to complete`. `listener.handshake_started()` and
`listener.handshake_completed()` answer whether the event has ever happened on this listener.

Treating any TLS condition itself as a boolean raises `TypeError("call the TLS condition with (), or pass it to board.wait_until")`,
preventing an unevaluated `assert listener.handshake_completed` from passing silently.

By default, CA and server certificates are valid from `2000-01-01T00:00:00Z` to
`2099-12-31T00:00:00Z`. To test an expired or not-yet-valid **server** certificate while keeping
the CA valid, pass `server_not_before` and/or `server_not_after` to `CertificateAuthority`:

```python
from datetime import UTC, datetime

ca = CertificateAuthority(
    ["broker.example"],
    server_not_before=datetime(2020, 1, 1, tzinfo=UTC),
    server_not_after=datetime(2030, 1, 1, tzinfo=UTC),
)
network.listen(8883, network.broker, tls=ca)
# Provision ca.ca_pem and power on the device in a world dated after 2030.
```

These arguments require timezone-aware datetimes, converted to UTC; naive values raise
`TLS server_not_before must be a timezone-aware datetime` (or the `server_not_after` equivalent).
An empty or reversed interval raises `TLS server_not_before must be earlier than server_not_after`.
The CA's own dates stay unchanged. Firmware must enable certificate date verification and
synchronize its clock for the validity check to take effect.

Names may be DNS names or IP addresses. Provisioning firmware credentials is separate from this API.

The server's side of TLS repeats from run to run, so a test over TLS gets the same times in every
run, after the handshake too. The keys of a CA follow from `ca.number`: the plugin numbers the CAs
from 0 again before each test, and a new CA takes the lowest number that neither the test nor a
CA still held elsewhere has. So the first CA of every test has the same keys, a second CA in a
test (an untrusted one, say) has keys of its own, and so does one made while a CA from an earlier
test is still held, by a `scope="module"` fixture or a failed test's traceback: it never gets the
keys of the CA it should not be. A test whose CAs are its own gets the same keys alone as in the
suite; numbering skips held CAs in the same way in two runs of the same tests. Each connection's
server random and ECDHE key follow from the CA's number and the connection's place on its
listener. These keys are anyone's: never
trust such a CA outside a test.

The server speaks TLS 1.2 only, with ECDHE-RSA and AES-128-GCM, AES-256-GCM or
ChaCha20-Poly1305 (the client's order picks one), over x25519, secp256r1 or secp384r1, signing with
RSA PKCS #1 v1.5 and SHA-512, SHA-384, SHA-256 or SHA-1. It takes the extended master secret,
secure renegotiation's empty extension and a maximum fragment length, resumes no session and
refuses a renegotiation with a `no_renegotiation` warning. A handshake that fails logs why, as in
`tls: handshake failed: the client sent the alert unknown_ca` (the device did not trust the CA),
`tls: handshake failed: the client does not offer TLS 1.2, the only version served` or
`tls: handshake failed: no cipher suite in common; the server has ECDHE-RSA-AES128-GCM-SHA256, ECDHE-RSA-AES256-GCM-SHA384, ECDHE-RSA-CHACHA20-POLY1305`.
Why the server is the harness's own: [the decision](../decisions/harness.md#the-harnesss-tls-server-draws-every-random-value-from-a-seed). `pppos_client` validates
plain MQTT; [the position tracker](examples-user.md#position-tracker-over-a-cellular-modem)
validates firmware PPP, NTP and MQTT over TLS, with its broker CA provisioned through NVS.

## Bridge a port to a real service

A test that needs its own backend's behaviour, rather than a service it reads in the test, can put
that backend at the far end of one TCP port: a server in a container or on the host.
`network.bridge(port, to)` relays the port, on every destination address, to the host and port
`to`. Each connection the device opens there is one the harness opens to `to`, and the bytes go
both ways as they are:

```python
def test_publishes_to_our_broker(board):
    network = board.modem.network()
    network.bridge(1883, ("127.0.0.1", 1883))  # a broker started by the test or by CI
    board.power_on()
    ...
```

The same call works behind the access point, on the network `wifi.access_point()` returns.
`tests/test_http_request.py` has unmodified `http_request` fetch a page from Python's
`http.server` on the host this way, and `tests/test_pppos_client.py` has `pppos_client` publish, over
PPP, to a broker listening on a host socket.

What changes with a bridge:

- **The whole run is on the wall clock,** as with a [real source](real-source-user.md): the runner
  starts with `--realtime`, every recording says `clock: wall`, and a second of the board's time
  takes a second of wall time. The service answers when it answers, so runs do not repeat, and
  such a test does not belong in the determinism job. The test still holds the clock while its
  body runs; `board.wait` and `board.wait_until` let it go.
- **Give the bridge before `board.power_on()`.** After it the call raises
  `network.bridge(1883, ('127.0.0.1', 1883)): the board is on already: a bridge puts the run on the
  wall clock, from power-on, so give it before board.power_on()`. A replay given before the bridge
  is refused as a real source refuses it: `network.bridge(1883, ('127.0.0.1', 1883)): a bridge puts
  the run on the wall clock, and uart.gnss replays a recording on the emulated-fifo-write clock:
  replaying it would compare times across clocks`. A replay given after it takes only a recording
  on the wall clock.
- **It takes the place of the broker on 1883 or the HTTP server on 80.** Their objects stay, and
  see nothing on that port; `network.broker` and the `broker` fixture then describe a broker the
  device does not reach. Any other port must be free: `network.bridge(8883, ('127.0.0.1', 8883)):
  TCP port 8883 already has a service`, and `network.listen` refuses a bridged port. `to` is a
  tuple, `("127.0.0.1", 1883)` or `("localhost", 8080)`; a string raises `TypeError`, a port out of
  1 to 65535 `ValueError`.
- **DNS still answers every name with `10.64.0.1`.** The device keeps its configured names; only the
  port picks the service. The bridge connects to `to`, not to the name the device asked for.
- **TLS goes through as it is.** The bridge does not end TLS: the device must trust the real
  service's certificate, and the transcript holds ciphertext for that port. `tls=` is for
  `listen`, not for a bridge.

`network.bridge` returns the `Bridge`: `port`, `to`, and `connections`, the connections the device
opened. `network.bridges` holds them by port. The network's transcript and the report log each step:

```text
bridge: TCP 80 → 127.0.0.1:8080, in place of the HTTP server
net: TCP 10.64.0.2:61332 → 10.64.0.1:80 accepted
bridge: 80/1 connected to 127.0.0.1:8080
bridge: 80/1 closed by 127.0.0.1:8080
```

The device's connection is accepted before the harness reaches the service. When the service cannot
be reached within 5 wall seconds, or refuses, the device sees its connection accepted and then
closed with no data, and the transcript says why:
`bridge: 80/1 cannot connect to 127.0.0.1:8080: Connection refused`. A service that closes its end
closes the device's after the bytes it sent (`closed by`); one that fails mid-stream logs
`bridge: 80/1 lost 127.0.0.1:8080: <reason>`, and a write the service does not take logs
`bridge: 80/1 cannot write to 127.0.0.1:8080: <reason>`; either closes the device's end. The device
closing its end and the end of the test end the stream at the service: it reads the end of the
stream, as from a device that sent its FIN. A lost link (power or registration loss,
`wifi.access_point_off()`) does not: the service hears nothing of it, as a real one on the
internet would not, and the stream stays open until the service gives up or the test ends. None of
these fails the test by itself; the firmware's reaction is what the test asserts.

The device's FIN closes the connection both ways: the network's TCP has no half-close, and closes
its own end as soon as the device closes its. A service that answers only after it has read the
end of the stream, as for a client that sends its request and then `shutdown(SHUT_WR)`, never
reaches the device. What the service sends after the device closed is dropped, and the transcript
counts it: `bridge: 80/1 dropped 512 bytes 127.0.0.1:8080 sent after the device closed its end`.

Only TCP is bridged. DNS, NTP and DHCP stay the network's own, and UDP to any other port is
answered by what `network.ip.udp` holds. The [path](#give-the-network-a-round-trip-and-a-rate)
still applies between the device and the bridge, on top of whatever the real service's own network
takes.

## Failures and reports

The test's `esp32sim link` report includes every network event, with emulated times, and the
modem's complete AT command history. `board.properties()["routes"]["uart.modem"]["network"]`
contains the same structured transcript; `network.transcript()` returns it directly.
Application stream bytes are hex-encoded plaintext, both directions, with connection identifiers,
except on a [bridged](#bridge-a-port-to-a-real-service) port that carries TLS, where they are the
ciphertext the bridge relays.
A raw `modem.tap(path)` still records UART/PPP bytes, including ciphertext for TLS.

Behind the access point the report has `wifi: access point 'myssid', channel 6, WPA2-PSK` (or
`open`), one `  around: 'cafe', channel 3, -70 dBm, open` line per network around, then the
network's events; `board.properties()["wifi"]["network"]` holds the transcript
([the wifi fixture](pytest-plugin-user.md#the-wifi-fixture)). A run of `http_request` logs, among
others:

```text
wifi: access point 'myssid' on the air, channel 6
dhcp: DISCOVER from 24:6f:28:00:11:22 → OFFER 10.64.0.2
dhcp: REQUEST from 24:6f:28:00:11:22 → ACK 10.64.0.2
ethernet: dropping frames of type 0x86dd (IPv6)
dns: example.com → 10.64.0.1
net: TCP 10.64.0.2:61332 → 10.64.0.1:80 accepted
http: GET / → 200
```

`wifi.access_point_off()` stops the network at the board's current instant, as power loss does
behind the modem: every TCP connection the test opened is lost (`net: TCP 10.64.0.1:<port> →
10.64.0.2:<port> link lost`), the device's stay open until their keepalive ([the
Will](#a-lost-link-and-the-will)), queued replies are dropped, and the transcript logs
`wifi: access point off the air`. Nothing the station sends is answered until
`wifi.access_point_on()`, which logs `wifi: access point back on the air`; the station then joins
the fresh access point from the start, DHCP included. The leases stay in `network.dhcp.leases`,
and a `network.dhcp.next_lease()` condition created after the last lease waits for the new one, as
`tests/test_http_request.py` does. ESP-IDF's examples
print `Wi-Fi disconnected 3, trying to reconnect...` when the access point leaves.

A receive FIFO overflow on the modem's route immediately fails with
`uart.modem: UART receive overflow (rxfifo_ovf) on GPIO 27: the simulated network requires a lossless route`.
Other routes retain their existing overflow policy; frame errors remain fatal everywhere.
The simulated TCP accepts connections and [opens the test's](#open-a-tcp-connection-to-the-device),
but does not retransmit or recover packet loss. The path
loses nothing ([packet loss is postponed](../decisions/harness.md#packet-loss-on-the-network-path-is-postponed)).
Malformed packets are discarded or their application connection is closed; events help explain
why the firmware stopped progressing. The test's `within=` and wall-time timeout still bound waits.

Registration or power loss discards PPP state, queued replies and TCP sessions, and emits MQTT
Wills once. The modem emits no unsolicited `NO CARRIER` for that loss;
[`modem.drop_call()`](modem-user.md#a-dropped-data-call) does, and stays registered, and with
`terminate=True` offers an LCP Terminate-Request first. A new dial starts a
fresh PPP negotiation. In network data mode, `+++` requires at least one second of silence before
and after it; pluses inside ordinary PPP payload remain bytes. The AT-only escape behavior is
unchanged. See [the modem](modem-user.md) and [determinism](../guides/testing.md#determinism).

## See also

- [The wifi fixture](pytest-plugin-user.md#the-wifi-fixture): the access point, the networks
  around it and switching it off the air.
- [The modem](modem-user.md): the AT model the network behind the modem sits on.
- [network-technical.md](network-technical.md): the layers and their lifecycle.
- [Validation firmware](validation-firmware-user.md): `pppos_client`, `mqtt_session`,
  `http_request` and `http_server`.

## Call the device's HTTP API

After the device has an address and its server is listening, `network.http_client` sends HTTP/1.1
requests through the same simulated TCP path as `network.connect`. For example, behind
`network = wifi.access_point("myssid", password="mypassword")`, once the firmware is ready:

```python
exchange = network.http_client.get("/status", headers={"Accept": "application/json"})
response = board.wait_until(exchange.response, within=seconds(5))
assert response.status == 200
assert response.headers["content-type"] == "application/json"
assert response.json()["status"] == "ok"
echo = network.http_client.post("/echo", json={"id": 7})
response = board.wait_until(echo.response, within=seconds(5))
assert response.json()["echo"] == {"id": 7}
```

`get(path, *, port=80, headers=None)` and
`post(path, body=None, *, json=None, port=80, headers=None)` return an exchange. `body` is bytes or
UTF-8 text; `json` serializes a Python value and defaults Content-Type to `application/json`.
Passing both a body and a non-None JSON value raises `ValueError`. `json=None` means no JSON body;
use `body="null"` with a JSON Content-Type to send JSON null. Paths start with `/`, may include a
query, and must be ASCII with no whitespace or fragment (percent-encode such characters).
Headers are a dictionary. Host defaults to the device address and port. The client controls
Content-Length, Transfer-Encoding and Connection; supplying those request headers is refused.

`exchange.response` is a reusable condition, without parentheses when passed to `wait_until`.
The response has integer `status`, `headers` with lowercase keys (repeated values joined with
`, `), bytes `body`, UTF-8 `text` with replacement, and `json()`. `exchange.connection` exposes
the underlying TCP connection. `network.http_client.exchanges` retains requests in call order.

Each request opens its own connection and sends `Connection: close`. Content-Length,
read-to-close and chunked response bodies are supported; chunk extensions and trailers are
accepted, but trailers are not added to response headers. Up to 16 informational responses are skipped;
204 and 304 have no body. Explicit keep-alive responses and protocol switching are refused.
There is no TLS, connection reuse or redirect following. Malformed or truncated responses raise
`ValueError` from the condition with the reason; refused, reset or lost connections raise
`ConnectionEnded`. Heads, chunk size lines and trailers are limited to 64 KiB excluding their terminating
separators, decoded bodies to 1 MiB. Limits are independent of TCP packet boundaries.

The transcript and report's network section include request and result events such as
`http client: device:80/1 GET /status requested` and
`http client: device:80/1 GET /status → 200`, plus the TCP stream bytes and failures. Results are
recorded on receipt even when the test never polls the condition.

## Joining the device's own access point

`network = wifi.station("device-setup", password="provisioning")`, before power-on, joins a
SoftAP started by the device. Omit `password` for an open AP. This is one host on the device's
subnet, with MAC `02:53:54:41:00:01` unless `mac=` names another unicast address. The device's
DHCP server assigns its address:

```python
network = wifi.station("device-setup", password="provisioning")
board.power_on()
lease = board.wait_until(network.when_leased(), within=seconds(3))
assert lease.device_address == "192.168.4.1"
exchange = network.http_client.get("/status")
response = board.wait_until(exchange.response, within=seconds(2))
assert response.status == 200
```

`seconds` comes from `esp32tl.units`. `network.when_leased()` describes current state and returns
`HostLease` while the host holds a lease. `network.lease` reads that same lease without waiting,
or `None`. Its fields are `address`, `device_address`, `at` and `expires` (board-clock
`Duration`s), `mask` and optional `router`. The default device address is the DHCP server
identifier, option 54, not necessarily the router. `connect(port, address=...)` overrides the
destination and uses the leased host address as its source. Raw TCP, HTTP, path configuration,
transcripts and reports work as on the other links. ARP resolves a destination before sending IP.

Before a lease, even with `address=`, `connect` raises `NotReachable`:
`network.connect(80): the device has leased no address to the station yet; wait for network.when_leased()`.
A wrong Wi-Fi password leaves this condition unsatisfied; its timeout says
`the station has a DHCP lease did not happen within 3 s`. The runner remains usable.

The host's DNS, NTP, broker, HTTP server, custom listeners and bridges listen **only on the leased
host address**. Firmware in AP mode must explicitly contact that address to use them. DNS answers
with the host's address. The host answers ARP only for its own address and drops IPv4 addressed
elsewhere. Unsupported EtherTypes, including IPv6, are counted and dropped. This network has no
PPP link; `network.dhcp` is the existing access-point DHCP server and does not issue this host's
lease: use `network.lease` and `when_leased()` here.

DHCP retries after 4, 8, 16, 32 and then 64 seconds between attempts, on the board's clock.
The first attempt starts at data-link readiness; no retry deadline runs before association.
Device reset loses connections, clears the lease and ARP cache, and waits for data-link readiness before starting DHCP again.
`board.reset()` queues the reset; the current lease remains until the runner reports it. To wait
for a fresh lease after a reset, first observe the new boot:

```python
boot = board.boot.number
board.reset()
board.wait_until(lambda: board.boot.number > boot, within=seconds(1))
lease = board.wait_until(network.when_leased(), within=seconds(3))
```

Not yet: renewal at T1. At expiry the host loses its connections and acquires a fresh lease;
it never uses the expired address. There are no conflict probes or gratuitous ARP announcements.
There are no keep-alive frames: an idle ESP-IDF AP may disassociate the host after about 300 s,
and esp32sim rejoins. Explicit station leave/rejoin, simultaneous virtual AP and station, other
chips, WPA3-SAE, required PMF and hidden SSIDs are not supported. Renewal needs a client state
machine; the radio features need esp32sim support. See the
[decision](../decisions/harness.md#a-test-joins-the-devices-access-point-through-esp32sims-virtual-station-with-the-harness-as-the-host-behind-it).

A virtual station waits for the Wi-Fi data link before starting DHCP. No DHCP retries run while disconnected. Rejoining starts a fresh exchange with the initial 4 s retry interval; it does not inherit the power-on backoff. Disconnection invalidates `network.lease`, ARP and queued traffic and marks the TCP connections the test opened lost. The device, the access point here, keeps its own stack when the station leaves: its connections stay as they were, and what a service writes or closes while the station is away waits for it, and reaches the device in order, its FIN last, once the station holds a lease again; one still in its handshake is dropped, as its SYN-ACK is not sent again, and so is one with bytes or a FIN the device had not acknowledged when the station left, or whose FIN it acknowledged without sending its own, as this TCP sends nothing again and has no timer. Reset and power cycling follow the same rule for the test's connections, and leave the device's open until their service gives up, as [behind the modem](#a-lost-link-and-the-will).
