# Simulated network

The network the device reaches lives in the test process and on the board's emulated clock. It is
the same network in two places: behind the modem, over PPP on the modem's UART, and behind a Wi-Fi
access point, over Ethernet frames with ARP and DHCP. Both answer DNS, NTP, an MQTT broker on TCP
1883, an HTTP server on TCP 80 and the TCP services the test adds. The firmware keeps its Wi-Fi,
PPP, IP, DNS, MQTT and HTTP stacks and its configured destination names. There is no host network
connection, external broker or web server, `pppd`, real-time mode or clock switch.

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
| `next_message(topic="#", payload=None)` | Condition returning the first matching message recorded **after this method call**; MQTT topic filters are accepted, payload is bytes or text |
| `find_message(topic="#", payload=None)` | Immediate history query returning the first recorded match, or `None`; the same filters as `next_message` |
| `message(topic="#", payload=None)` | Alias of `next_message` |
| `subscribed(topic)` | Condition true while an active client has this exact subscription filter, including an existing subscription |
| `next_connection()` | Condition returning the first accepted MQTT CONNECT recorded **after this method call** |
| `find_connection()` | Immediate history query returning the first accepted CONNECT, or `None`, even if that connection has closed |
| `connected()` | Alias of `next_connection` |
| `connection_attempt()` | Condition returning the first accepted or rejected CONNECT outcome recorded **after this method call** |
| `connection_policy` | Callable receiving `Connect` and returning `ConnackCode`; `None` (default) accepts supported requests |
| `publish(topic, payload, retain=False, qos=0)` | Publish from the test at the board's current time; bytes or UTF-8 text, QoS 0 or 1 |
| `messages`, `clients`, `retained` | Message history, accepted-client history and currently retained messages |
| `connection_attempts` | CONNECT outcome history, including accepted and rejected attempts |
| `puback` | Policy for new incoming QoS 1 exchanges: `"immediate"` (default), `"hold"`, or a nonnegative `Duration` |
| `pending` | Tuple of pending `Publication` handles, grouped by connection, then first receipt |
| `disconnect(client_id)` | Close that client's stream as a connection loss; return `True` if connected, otherwise `False` |
| `session(client_id)` | The session the broker has for this client ID now, a `SessionState`, or `None` ([sessions](#keep-a-session-across-connections)) |
| `forget_session(client_id)` | Discard a stored session; return `True` if there was one, otherwise `False`. A connected client raises `ValueError` (`mqtt: c is connected: disconnect it first`) |
| `deliveries` | Every PUBLISH the broker wrote to a client, each a `Delivery` |
| `next_delivery(topic="#", payload=None)` | Condition returning the first matching delivery recorded **after this method call**; the same filters as `next_message` |
| `find_delivery(topic="#", payload=None)` | Immediate history query returning the first recorded match, or `None` |

A message has `at` (`Duration`), `client`, `topic`, `payload` (bytes), `qos`, `retain`, and
`text` (UTF-8 with replacement). Incoming publications also expose `connection_id`, `packet_id`
(`None` for QoS 0), `dup` and `publication` (a `Publication` handle for QoS 1, otherwise `None`).
Test publications use client `(test)` and have no connection, packet ID or handle. Wills carry
their original connection ID but have no packet ID or handle. A client record has `at`,
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
ordering API.

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

Polling any MQTT event condition does **not** consume a match or move its boundary. Repeated
calls and waits return the same first match; construct a new `next_*` condition for a new wait.
`subscribed` instead reads current state each time: unsubscribe or connection closure can make
a previously true condition false.

All MQTT conditions have timeout representations such as `the next MQTT message on 'a/+' saying
b'new'`, `the next MQTT client connection`, `the next MQTT connection attempt`,
`the next MQTT delivery on 'a/+' saying b'new'` and
`an active MQTT subscription to 'a/#'`. Call a condition with `()` or pass it to
`board.wait_until`. Boolean coercion of any MQTT condition, including those from `message`,
`connected`, `connection_attempt`, `next_delivery` and `subscribed`, raises
`TypeError("call the MQTT condition with (), or pass it to board.wait_until")`, before and after
a match, so an unevaluated `assert broker.connected()` fails. Use
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

## Reject MQTT connections and check credentials

Set `broker.connection_policy` before power-on or between board advances. It receives an
immutable `Connect` record and must return a member of `ConnackCode`, both imported from
`esp32tl.net.mqtt`. The same policy applies to plain TCP and TLS listeners using this broker.
It runs once per supported, fully parsed CONNECT; it must not block or wait on the board.
Changing it affects future attempts only, leaving existing clients connected. Assign `None`
to restore the accepting default. Behind a Wi-Fi access point the same policy is set on
`network.broker`. A directly constructed `Broker` also accepts the keyword
`connection_policy=`.

For firmware configured to retry, first refuse connections, then enable credential checking:

```python
from esp32tl.net.mqtt import ConnackCode, Connect
from esp32tl.units import seconds

broker.connection_policy = lambda connect: ConnackCode.SERVER_UNAVAILABLE
attempt = broker.connection_attempt()
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
`next_connection()` include accepted connections only. `connection_attempt()` observes future
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

A client still connected with the same ID is disconnected first (MQTT-3.1.4-2). If that connection
had CleanSession=1 its session ends with it, so a CleanSession=0 CONNECT finds none.

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


def test_joins_and_fetches_from_the_tests_server(board, wifi, console):
    network = wifi.access_point("myssid", password="mypassword")
    network.http.serve("/", "hello from the test\n")
    printed = console.line(r"^hello from the test$")
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
| `requested(path=None, *, method=None)` | Alias of `next_request` |
| `requests` | Every request recorded, in order; refused requests are not |

A request (`Request`) has `at` (`Duration`), `method`, `target` (as sent), `path` (an
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
`next_request` or `requested` raises
`TypeError("call the HTTP condition with (), or pass it to board.wait_until")`, before and after a
match, so an unevaluated `assert network.http.requested("/")` fails. Use
`assert network.http.find_request("/") is not None` to inspect past requests.

A `path` filter does not see the query, which is in the request's `target`. `next_request`,
`requested` and `find_request` raise `ValueError` for a filter that holds `?`
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
`10.64.0.1` and DNS `10.64.0.1`, with a lease of 86400 s. It offers that address to every
DISCOVER, acknowledges a REQUEST for it (or for no address), and answers a REQUEST for another
address with a NAK. A REQUEST that names another server is left unanswered; DECLINE and RELEASE
are logged and not answered. The transcript shows
`dhcp: DISCOVER from 24:6f:28:00:11:22 → OFFER 10.64.0.2` and
`dhcp: REQUEST from 24:6f:28:00:11:22 → ACK 10.64.0.2`.

`network.dhcp.leases` lists every acknowledged lease, each a `Lease` with `at` (`Duration`), `mac`,
`address` and `hostname` (the name the device sent, `espressif` for ESP-IDF's default, or `None`).
`network.dhcp.next_lease()` is a condition returning the first lease recorded **after this method
call**: it is how a test waits for the station to join again. `network.dhcp.leased()` is its
alias. `network.dhcp.find_lease()` is an immediate history query returning the first recorded
lease, or `None`. A `next_lease` condition has the boundary and polling of the
[MQTT next-event conditions](#history-next-events-and-current-state); its timeout names
`the next DHCP lease`, and boolean coercion raises
`TypeError("call the DHCP condition with (), or pass it to board.wait_until")`, before and after
a lease. The leases outlive the access point switching off and on. `network.dhcp` exists behind
the modem too, where nothing asks it.

ARP for any address but the device's own is answered with the network's MAC,
`02:53:49:4d:00:02`, so every address on the subnet and off it reaches the network. ARP for
`10.64.0.2` itself gets no answer: lwIP probes that address before taking the lease, and an answer
would make it decline. The network learns the device's MAC from the frames it sends.

The network speaks IPv4 only. Frames of any other type are dropped, and the first of each type is
logged: ESP-IDF's IPv6 link-local traffic gives
`ethernet: dropping frames of type 0x86dd (IPv6)`. The MTU is 1500 bytes; a packet for the device
over it is dropped with `ethernet: dropped an IPv4 packet of <n> bytes: over the MTU`.

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
type annotations, import `Publication`, like `Message` and `Client`, from `esp32tl.net.mqtt`.
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

`broker.disconnect(message.client)` closes only that client's active stream, including when it
uses TLS. It publishes the Will once, cancels pending ACKs, makes the client's subscriptions
inactive (with CleanSession=1, removes them) and leaves PPP, other streams and DNS/NTP usable. Graceful MQTT DISCONNECT cancels ACKs but suppresses the Will.
Transport loss, keepalive expiry, client-ID takeover and network reset also cancel pending ACKs.
A keepalive expires at 1.5 times the keepalive after the client's last packet; when `board.now`
reaches that instant the connection is closed, its Will is in `messages`, and `broker.disconnect`
at that instant returns `False`. Expiry wins if keepalive and PUBACK have the same deadline. An
already emitted ACK stays emitted; closing does not retract bytes already queued in the transport.

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
from esp32tl.net.tls import CertificateAuthority

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

Names may be DNS names or IP addresses. TLS 1.2 and later use Python's `ssl` in memory, with
system randomness. The harness sees application plaintext; TLS ciphertext and timing do not
repeat. Provisioning firmware credentials is separate from this API. `pppos_client` validates
plain MQTT; [the position tracker](examples-user.md#position-tracker-over-a-cellular-modem)
validates firmware PPP, NTP and MQTT over TLS, with its broker CA provisioned through NVS.

## Failures and reports

The test's `esp32sim link` report includes every network event, with emulated times, and the
modem's complete AT command history. `board.properties()["routes"]["uart.modem"]["network"]`
contains the same structured transcript; `network.transcript()` returns it directly.
Application stream bytes are hex-encoded plaintext, both directions, with connection identifiers.
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
behind the modem: every TCP connection is lost (`net: TCP 10.64.0.2:<port> → 10.64.0.1:<port> link lost`),
queued replies are dropped, MQTT Wills are published once, and the transcript logs
`wifi: access point off the air`. Nothing the station sends is answered until
`wifi.access_point_on()`, which logs `wifi: access point back on the air`; the station then joins
the fresh access point from the start, DHCP included. The leases stay in `network.dhcp.leases`,
and a `network.dhcp.next_lease()` condition created after the last lease waits for the new one, as
`tests/test_http_request.py` does. ESP-IDF's examples
print `Wi-Fi disconnected 3, trying to reconnect...` when the access point leaves.

A receive FIFO overflow on the modem's route immediately fails with
`uart.modem: UART receive overflow (rxfifo_ovf) on GPIO 27: the simulated network requires a lossless route`.
Other routes retain their existing overflow policy; frame errors remain fatal everywhere.
The simulated TCP accepts connections but does not retransmit or recover packet loss. The path
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
- [Validation firmware](validation-firmware-user.md): `pppos_client`, `mqtt_session` and
  `http_request`.
