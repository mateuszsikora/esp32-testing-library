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
    message = board.wait_until(broker.message("/topic/esp-pppos"), within=seconds(60))
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
| `message(topic="#", payload=None)` | Condition returning the first matching message received **after the condition was created**; MQTT topic filters are accepted, payload is bytes or text |
| `subscribed(topic)` | Condition true while an active client has this exact subscription filter, including an existing subscription |
| `connected()` | Condition returning the next client connecting after the condition was created |
| `connection_attempt()` | Condition returning the next accepted or rejected CONNECT outcome after the condition was created |
| `connection_policy` | Callable receiving `Connect` and returning `ConnackCode`; `None` (default) accepts supported requests |
| `publish(topic, payload, retain=False, qos=0)` | Publish from the test at the board's current time; bytes or UTF-8 text, QoS 0 or 1 |
| `messages`, `clients`, `retained` | Message history, accepted-client history and currently retained messages |
| `connection_attempts` | CONNECT outcome history, including accepted and rejected attempts |

A message has `at` (`Duration`), `client`, `topic`, `payload` (bytes), `qos`, `retain`, and
`text` (UTF-8 with replacement). Test publications use client `(test)`. A client record has
`at`, `client_id`, `username`, `password`, `keepalive` and `will_topic`. By default, credentials
are observed, not authenticated; install a connection policy to check them. Treat reports and
histories as test data: payloads, usernames and passwords can be sensitive.

Subscriptions support `+` and final `#`, including empty levels. Wildcards beginning with `+`
or `#` do not match `$` topics. QoS 0 and 1, PUBACK, retained delivery, empty retained-message
deletion, unsubscribe, PING and Will on connection loss are supported. Keepalive expires after
1.5 times its advertised interval on the board's clock. DISCONNECT suppresses the Will.
QoS 2 publications are refused. Sessions are not persisted across connections.

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
connected = broker.connected()
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
then closes. It cannot take over another client, route pipelined publications, subscribe or
emit a Will.

`Connect` contains `at` (`Duration`, on the board clock), `peer` (IP string and source port),
`client_id`, `clean_session`, `username` (text or `None`), `password` (bytes or `None`),
`keepalive` (seconds), `will_topic`, `will_payload`, `will_qos` and `will_retain`. Absent Will
topic/payload are `None`; QoS and retain are zero/false. The client ID is exactly what the device
sent, including an empty string; an anonymous ID is allocated only after acceptance and appears
in `clients`.

Each immutable `ConnectionAttempt` in `connection_attempts` has `connect` (the metadata),
`return_code`, `at` (the CONNECT timestamp) and `accepted` (boolean). This includes the built-in
empty-ID rejection. Histories survive connection loss and network reset. `clients` and
`connected()` include accepted connections only. `connection_attempt()` observes future outcomes
without consuming them; a timeout names `an MQTT connection attempt`. Malformed or incomplete
CONNECTs, TCP accepts and TLS handshake failures do not create outcomes.

A policy that raises, or returns anything other than a `ConnackCode` (including a bare integer
or boolean, which raises `TypeError("MQTT connection_policy must return a ConnackCode")`), is a
test error. It sends no CONNACK, records no outcome and does not become a simulated
authentication failure. In a board test the policy runs in the link, not in the test: the link
fails and the run ends, so the waiting `board.wait_until(...)` raises `RunEnded`, not the
policy's exception. Its message names the error, as in
`the link failed: modem.receive raised TypeError: MQTT connection_policy must return a ConnackCode`
(`wifi.receive` behind an access point), and the traceback is in the link's report. Only a
`Broker` driven directly, without a board, raises the policy's own exception from `on_data`, after
closing that connection.

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
    request = board.wait_until(network.http.requested("/"), within=seconds(30))
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
| `requested(path=None, *, method=None)` | Condition returning the first request, for `path` and with `method` when given, received **after the condition was created** |
| `requests` | Every request answered, in order |

A request (`Request`) has `at` (`Duration`), `method`, `target` (as sent), `path` (an
origin-form target such as `/a?b` without its query, `://` in it or not; an absolute-form one such
as `http://host/x?b` without its scheme, host and query, `/x`; `*` and `host:port` as they are), `version` (`HTTP/1.0` or
`HTTP/1.1`, as sent), `headers` (name and value pairs, in order), `body` (bytes) and `text` (the
body as UTF-8 with replacement). `header(name)` returns the first field of that name, whatever its
case, or `None`.

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
(`http.serve: 'index.html' is not a path, which starts with /`), a status the server has no reason
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
`network.dhcp.leased()` is a condition returning the next lease after the condition was created:
it is how a test waits for the station to join again. The leases outlive the access point
switching off and on. `network.dhcp` exists behind the modem too, where nothing asks it.

ARP for any address but the device's own is answered with the network's MAC,
`02:53:49:4d:00:02`, so every address on the subnet and off it reaches the network. ARP for
`10.64.0.2` itself gets no answer: lwIP probes that address before taking the lease, and an answer
would make it decline. The network learns the device's MAC from the frames it sends.

The network speaks IPv4 only. Frames of any other type are dropped, and the first of each type is
logged: ESP-IDF's IPv6 link-local traffic gives
`ethernet: dropping frames of type 0x86dd (IPv6)`. The MTU is 1500 bytes; a packet for the device
over it is dropped with `ethernet: dropped an IPv4 packet of <n> bytes: over the MTU`.

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
connection closure or network reset. Use a fresh listener when a scenario requires fresh history.
`handshakes_completed` counts server-side handshake completions, once per connection; it does
not prove an MQTT connection or publication. Started input alone does not imply completion.

Timeouts name `a TLS handshake to start` or `a TLS handshake to complete`. Treating a condition
itself as a boolean raises `TypeError("call the TLS condition with (), or pass it to board.wait_until")`,
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
and a `network.dhcp.leased()` condition created after the last lease waits for the new one, as
`tests/test_http_request.py` does. ESP-IDF's examples
print `Wi-Fi disconnected 3, trying to reconnect...` when the access point leaves.

A receive FIFO overflow on the modem's route immediately fails with
`uart.modem: UART receive overflow (rxfifo_ovf) on GPIO 27: the simulated network requires a lossless route`.
Other routes retain their existing overflow policy; frame errors remain fatal everywhere.
The simulated TCP accepts connections but does not retransmit or recover packet loss.
Malformed packets are discarded or their application connection is closed; events help explain
why the firmware stopped progressing. The test's `within=` and wall-time timeout still bound waits.

Registration or power loss discards PPP state, queued replies and TCP sessions, and emits MQTT
Wills once. The modem still emits no unsolicited `NO CARRIER` for that loss. A new dial starts a
fresh PPP negotiation. In network data mode, `+++` requires at least one second of silence before
and after it; pluses inside ordinary PPP payload remain bytes. The AT-only escape behavior is
unchanged. See [the modem](modem-user.md) and [determinism](../guides/testing.md#determinism).

## See also

- [The wifi fixture](pytest-plugin-user.md#the-wifi-fixture): the access point, the networks
  around it and switching it off the air.
- [The modem](modem-user.md): the AT model the network behind the modem sits on.
- [network-technical.md](network-technical.md): the layers and their lifecycle.
- [Validation firmware](validation-firmware-user.md): `pppos_client` and `http_request`.
