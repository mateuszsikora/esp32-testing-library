# Simulated network

The network behind the modem lives in the test process and on the board's emulated clock.
The firmware keeps its PPP, IP, DNS and MQTT stacks and its configured destination names.
There is no host network connection, external broker, `pppd`, real-time mode or clock switch.

## Exchange MQTT messages

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
| `publish(topic, payload, retain=False, qos=0)` | Publish from the test at the board's current time; bytes or UTF-8 text, QoS 0 or 1 |
| `messages`, `clients`, `retained` | Message history, connection history and currently retained messages |

A message has `at` (`Duration`), `client`, `topic`, `payload` (bytes), `qos`, `retain`, and
`text` (UTF-8 with replacement). Test publications use client `(test)`. A client record has
`at`, `client_id`, `username`, `password`, `keepalive` and `will_topic`. Credentials are recorded,
not authenticated. Treat reports as test data: payloads and usernames can be sensitive.

Subscriptions support `+` and final `#`, including empty levels. Wildcards beginning with `+`
or `#` do not match `$` topics. QoS 0 and 1, PUBACK, retained delivery, empty retained-message
deletion, unsubscribe, PING and Will on connection loss are supported. Keepalive expires after
1.5 times its advertised interval on the board's clock. DISCONNECT suppresses the Will.
QoS 2 publications are refused. Sessions are not persisted across connections.

A CONNECT with an empty client ID and `CleanSession=1` receives a generated ID such as
`esp32tl-anonymous-1`, visible in connection history, messages and Wills. IDs use a per-broker
counter and skip IDs already in its connection history, so anonymous clients remain independent
and repeated test runs assign the same IDs. An empty ID with `CleanSession=0` receives CONNACK
return code 2 (`Identifier rejected`) and is disconnected.

## DNS, time and services

PPP gives the device `10.64.0.2` and DNS `10.64.0.1`; the peer is `10.64.0.1`. Every destination
IP answers, so TCP and UDP dispatch by destination port, regardless of the address. DNS on
UDP 53 answers every IN A query with `10.64.0.1`; other query types receive an empty answer.
`network.dns.asked` records queried names. NTP on UDP 123 tells `board.power_on(utc=...)` plus
elapsed emulated time; `network.ntp.requests` counts replies. The default UTC is the board's.

PPP honors the device's negotiated maximum receive unit (MRU), default 1500 bytes, for the whole
outgoing IPv4 packet. TCP segmentation also respects the peer's MSS and receive window. The
smallest supported MRU is 68 bytes; a smaller request gets a Configure-Nak proposing 68.
A UDP reply exceeding MRU is dropped without fragmentation, with a transcript entry such as
`net: dropped UDP reply of 577 bytes: exceeds peer MRU 576 (fragmentation unsupported)`.
Renegotiation, LCP termination and modem redial clear the old MRU; omission of the option uses 1500.

`network.listen(port, service)` installs one TCP service on a port (1883 already holds the
broker). A service implements `accept(peer, write, close)`, returning a connection with
`on_data(bytes)` and `on_close()`. `peer` is the device's `(address, port)`; `write(bytes)` sends
back and `close()` closes. `network.ip.udp[port]` accepts a datagram service implementing
`on_datagram(bytes) -> bytes | None`. These callbacks must not block or use wall-clock waits.

Wrap any stream service in TLS by supplying a per-test CA:

```python
from esp32tl.net.tls import CertificateAuthority

network = modem.network()
ca = CertificateAuthority(["broker.example"])
network.listen(8883, network.broker, tls=ca)
# Provision ca.ca_pem as the firmware's trusted CA using that firmware's configuration interface.
```

CA and server certificates are valid from `2000-01-01T00:00:00Z` to `2099-12-31T00:00:00Z`.
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

A receive FIFO overflow on this route immediately fails with
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
