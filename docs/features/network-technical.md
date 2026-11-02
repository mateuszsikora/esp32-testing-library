# Simulated network: implementation

`esp32tl/net/` owns the network. `Internet` is either a UART `Source` attached to the modem model
by `interfaces.modem.Modem.network()`, or the network behind the Wi-Fi access point that
`interfaces.wifi.Wifi.access_point()` creates. The `broker` fixture calls `modem.network()` and
returns its `Broker`. All callbacks execute during link turns or while the test holds the board.
There are no sockets, worker threads or host-clock timers. `Internet` uses the current UART chunk's
or Ethernet frame's timestamp inside receive callbacks and the held board time for test calls.
Responses carry a 1,000,000 ns latency (`LATENCY_NS`); the modem starts PPP after CONNECT, so LCP
follows the AT response. `poll` drains queued replies and evaluates broker keepalive deadlines;
`quiet_until` bounds the next turn by those deadlines.

## The link

`Internet(now, world, *, link="ppp")` takes `link`, `"ppp"` or `"ethernet"` (the `Link` type),
and keeps it for its life. `_peer()` builds the link object from it: a `Ppp` whose `ended`
callback resets TCP, or an `Ethernet` given the network's `Dhcp`. Both send through the same
closure, which queues each frame or chunk at `now + LATENCY_NS`, and both hand IPv4 datagrams to
`Network.receive`. `Internet.link` is that object; `Internet.ppp` and `Internet.ethernet` return it
when it is of their kind and raise `AttributeError` otherwise. The IP layer's MTU callback reads
`Ppp.peer_mru` or `Ethernet.mtu` (1500).

Every `Internet` builds the same services whatever its link: `Dns("10.64.0.1")` on UDP 53, `Ntp` on
UDP 123, the `Broker` on TCP 1883, the `HttpServer` on TCP 80, each TCP one wrapped in `_Traced`
for the transcript, and one `Dhcp`. Only `Ethernet` calls the `Dhcp`; a network behind a modem has
one that nothing asks.

`start(t)` stops the network first, then marks it active and, for PPP only, starts LCP. `stop(at)`
marks it inactive, resets TCP (`Network.reset`, which notifies every connection once), drops the
queued replies and replaces the link with a fresh one from `_peer()`. The `Dhcp`, the services and
the transcript are not replaced.

Behind the access point, `Wifi` is the link's `Interface` whose `ethernet` property is true: the
`BoardLink` hands it each `eth_tx` frame as `receive(t, "out", frame)`, and sends each output it
returns as one `eth_rx` ([protocol](../protocol.md)). `Wifi.receive` and `Wifi.poll` return
nothing while the network is missing or inactive. `Wifi.inputs` returns a `WifiAp` for each switch
the test made, stamped at its instant or at the turn's `now`, whichever is later.

## Layers

- `ppp.py`: RFC 1661/1662 framing, escaping, table-based CRC-16/X-25 (good residue `0xf0b8`),
  LCP and IPCP (RFC 1332/1877). Default receive ACCM drops unescaped control characters; output
  escapes all control characters, FLAG and ESCAPE. Compressed protocol/address-control fields
  are accepted; output is uncompressed. Invalid FCS, truncated controls, invalid options and
  aborted/oversized frames are discarded. LCP requests no authentication and rejects it when
  requested. Configure-Ack must match our pending identifier and options. Both directions must
  be acknowledged before opening IPCP, and IPv4 is delivered only while IPCP is open.
  Renegotiation closes existing connections and invalidates the previous acknowledgments.
  IPCP NAK supplies the device and DNS addresses; echo and termination are answered.
  `Ppp.peer_mru` records an acknowledged MRU (default 1500), reset with LCP and on each new
  Configure-Request. Values below the implementation's minimum of 68 are NAKed with 68. The
  IPv4 send boundary also drops a datagram exceeding that limit, logging
  `ppp: dropped IPv4 packet of <n> bytes: exceeds peer MRU <mru>`.
- `ip.py`: checks IPv4 lengths/checksum and rejects fragments; validates TCP and nonzero UDP
  checksums. Every destination dispatches by port. TCP uses fixed initial sequence 65536,
  window 8192, peer MSS capped at 1400 (default 536), acknowledgment and window accounting,
  and an MTU callback reading the current PPP peer MRU, or the Ethernet MTU. Each TCP payload is
  limited by that MRU
  minus the 20-byte IPv4 and 20-byte TCP headers, as well as MSS and available window space.
  UDP replies are checked against MRU (including 28 header bytes) before packing; an oversized
  reply is dropped and logged without fragmentation. TCP also handles
  receive sequence wrap, duplicate-data acknowledgments, FIN and reset. It never initiates a
  connection or retransmits. Unbound TCP ports return RST; unbound UDP is logged and dropped.
  Connections receive `on_close` once, also on RST, PPP loss and `Internet.stop`. There is no
  ICMP or IPv6. The UDP limit's log line names it `peer MRU` behind the access point too.
- `dns.py`: one bounded question, including compressed names with loop detection; preserves the
  query ID and recursion-desired bit. IN A answers have TTL 60 and address `10.64.0.1`.
- `ethernet.py`: Ethernet II without FCS, ARP and the hand-off to DHCP. `Ethernet.receive` drops
  frames under 14 bytes and frames from a multicast source, and learns `device_mac` from every
  other frame. ARP (`0x0806`): only Ethernet/IPv4 requests of 28 bytes or more are read; each is
  answered with `GATEWAY_MAC` (`02:53:49:4d:00:02`, esp32sim's own network's gateway in
  `esp-soc/src/net/packet.rs`) unless its target is the `Dhcp.device` address (lwIP's RFC 5227
  probe before it takes the lease) or its sender's own address (an announcement). IPv4 (`0x0800`)
  to `GATEWAY_MAC`, broadcast or a multicast address goes on; unicast to another MAC is dropped. A
  datagram that `ip.ipv4` and `ip.udp` take, the checks `Network.receive` makes of every datagram
  (header checksum, lengths, no fragment, the UDP checksum unless it is zero), from port 68 to 67,
  is DHCP: `Dhcp.on_message` answers it and nothing else sees it. Everything else goes to `on_ip`,
  where the IP layer drops what fails those checks. Other ethertypes are
  counted in `ignored` and the first of each logged as
  `ethernet: dropping frames of type 0x86dd (IPv6)` (`IPv6`, `VLAN`, `EAPOL`, else `unsupported`).
  `send_ip` frames a datagram from `GATEWAY_MAC` to `device_mac`; with no `device_mac` yet it logs
  `ethernet: dropped an IPv4 packet: the station has sent nothing yet`, and over `mtu` (1500)
  `ethernet: dropped an IPv4 packet of <n> bytes: over the MTU`. A DHCP reply goes from
  `10.64.0.1:67` to `10.64.0.2:68`, or to `255.255.255.255` and `ff:ff:ff:ff:ff:ff` when `Dhcp`
  says broadcast, with its UDP and IPv4 checksums computed here.
- `dhcp.py`: a server for one device (RFC 2131, options of RFC 2132).
  `Dhcp(now, log, *, local="10.64.0.1", device="10.64.0.2", dns="10.64.0.1", mask="255.255.255.0")`,
  `LEASE_SECONDS = 86400`. `on_message` reads a BOOTREQUEST of at least 240 bytes with Ethernet
  hardware and the magic cookie, and an option 53 of one byte; anything else gets no answer. A
  message whose server identifier (option 54) is not `local` is ignored. DISCOVER gets an OFFER;
  REQUEST gets an ACK when the requested address (option 50, else `ciaddr`) is `device` or absent,
  else a NAK; DECLINE and RELEASE are logged as `dhcp: DECLINE from <mac>`; other types are
  ignored. An answer carries options 53, 54, and but for a NAK 51, 1, 3 (`local`) and 6 (`dns`),
  `yiaddr` `device` and `siaddr` `local`, and is padded to 300 bytes (RFC 1542). It is broadcast
  when the client set the broadcast flag and for a NAK (RFC 2131, 4.1). Each answer logs
  `dhcp: <type> from <mac> → <OFFER|ACK|NAK> 10.64.0.2`; each ACK appends a
  `Lease(at, mac, address, hostname)` to `leases`, `hostname` from option 12. `leased()` returns a
  condition for the first lease after the index it was created at.
- `http.py`: `HttpServer(now, log)`, a `StreamService` on TCP 80 of every network (RFC 9110,
  9112). `serve(path, body, *, status, content_type, headers)` stores a `Response` per path, after
  checking that the path starts with `/`, the status is in `REASONS` and no header holds CR, LF
  or, in its name, `:`. Each connection is an `_Exchange` that reads one request: the head up to
  `\r\n\r\n` (`MAX_HEAD`, 64 KiB, else 431), parsed by `_head` (else 400), `Transfer-Encoding`
  refused with 501, the distinct `Content-Length` values (more than one, or not all ASCII digits, 400; over
  `MAX_BODY`, 1 MiB, 413, compared by the number of digits first, so that no value is too long
  for `int`), then the body. `_path` gives the path by the target's form (RFC 9112 3.2): an
  origin-form target, which starts with `/`, up to `?`; an absolute-form one, a scheme of letters,
  digits, `+`, `-` and `.` then `://`, what follows its authority up to `?`, or `/`; any other
  target as it is. `_answer` appends the `Request` to `requests`, logs
  `http: <method> <target> → <status>`, and returns the served `Response` or `404` with
  `not found\n`. A refusal logs `http: refused a request: <status> <reason>` and is not appended.
  `_respond` writes the status line, `Content-Type` and `Content-Length` (not for a status in
  `NO_CONTENT`, 204 and 304, whose response ends with its head, RFC 9112 6.3), `Connection: close`,
  the test's headers and the body (not for `HEAD`; `serve` refuses one for 204 and 304), then
  closes; later data on the connection is
  ignored. `requested(path, method=...)` returns a condition for the first matching request after
  the index it was created at.
- `ntp.py`: accepts NTP v3/v4 client requests; returns a v4 stratum-1 server response. The
  origin timestamp copies the client's transmit field. Reference, receive and transmit stamps
  use world UTC plus emulated time, including fractions and the 32-bit NTP era wrap.
  `go_silent` / `resume` toggle whether new datagrams receive replies, without changing the
  UDP binding, the clock or already queued packets. `requests` continues to count replies only;
  each state transition is logged once, and resuming never replays unanswered datagrams.
- `mqtt.py`: incremental MQTT 3.1.1 parsing (up to 1 MiB per packet); CONNECT, QoS 0/1 PUBLISH,
  PUBACK, SUBSCRIBE/SUBACK, UNSUBSCRIBE/UNSUBACK, PING and DISCONNECT. Retained state belongs to
  the test's broker; subscriptions belong to a connection. Empty clean-session client IDs are
  replaced before Will construction and takeover checks with `esp32tl-anonymous-<n>`. A per-broker
  counter and the accepted-client history prevent reuse or collision with an earlier accepted ID;
  an ID supplied only in a rejected attempt may still be allocated.
  CONNECT parsing first constructs immutable public `Connect` metadata, retaining the wire ID
  and the board timestamp. Empty IDs without CleanSession get CONNACK `00 02` followed by close;
  other supported requests pass through the current `connection_policy` (or accept with `None`).
  The callable must return `ConnackCode`, the six MQTT 3.1.1 values 0–5. It executes outside the
  malformed-packet exception handler: exceptions and invalid return types close the transport
  and leave `on_data` without CONNACK or an outcome. In a board run `on_data` is called from the
  link's `modem.receive` or `wifi.receive`, so `boardlink.calling` turns the error into the
  `LinkError` `modem.receive raised <Type>: <message>`; the link fails with its traceback, ends
  the run, and the test's wait raises `RunEnded` naming it. A successfully selected code appends
  a `ConnectionAttempt` to `connection_attempts` before sending CONNACK. This history is separate
  from accepted `clients`; `connection_attempt()` captures a future cursor like `connected()`.
  Rejections log `mqtt: '<client-id>' rejected: <CODE_NAME> (<value>)`, send CONNACK with Session
  Present zero and close before allocating an anonymous ID, installing a Will, setting keepalive
  or taking over an existing client. The parser stops processing buffered packets on closure.
  Only accepted connections install session state and enter `clients`; replacing the policy
  affects subsequent CONNECTs only. Network reset retains both histories and the policy.
  Will is published once with the time
  of disconnect, not the time of CONNECT. A replacement client ID closes the old connection.
  Session persistence and QoS 2 publication are unsupported. Credentials are observed by default;
  authentication is performed only by an installed test policy, with no external auth service.
  `next_message` and `next_connection` capture the length of their respective append-only
  histories at construction; `message` and `connected` delegate to them, and
  `connection_attempt` captures `connection_attempts` the same way. Matching uses append order,
  not timestamps, and polling does not change the captured offset. `find_message` scans
  from index zero with the same topic/payload filters; `find_connection` reads the first CONNECT.
  These are immediate queries returning records or `None`. `_Condition` wraps the MQTT
  callables with useful `repr` output and rejects boolean coercion, including for the live
  subscription query. Subscriptions continue to read active sessions, without latching matches.
- `tls.py`: a generated RSA-2048 CA and server certificate, SHA-256 signatures, DNS/IP subject
  alternative names, CA key usage and server extended key usage. Optional `server_not_before`
  and `server_not_after` apply only to the leaf certificate; the CA retains its fixed validity.
  Inputs must be timezone-aware, are normalized to UTC, and must form a forward interval;
  validation happens before key generation. Temporary key files are removed
  after `SSLContext.load_cert_chain`. TLS uses `SSLObject` and two `MemoryBIO`s; plaintext reaches
  the inner service only after handshake completion. TLS errors close the connection and notify
  the inner service once. `Internet.listen(..., tls=ca)` returns the installed `Tls` object;
  plain listeners return `None`. Its cumulative `handshakes_started` counter increments on the
  first nonempty `on_data` per session, before TLS parsing. Empty input, TCP accept alone,
  subsequent fragments and data after closure do not increment it. Invalid first input does.
  `handshakes_completed` increments once when `SSLObject.do_handshake()` returns successfully,
  before handing the plaintext stream to the inner service. Session closure and network reset
  leave both counters intact. Callable `handshake_started` / `handshake_completed` properties
  observe whether the corresponding live count is positive; they do not capture a future cursor.
  `next_handshake_started()` / `next_handshake_completed()` instead capture the corresponding
  count in `_HandshakeCondition.after` and compare the live count strictly against it. Event
  processing order distinguishes equal-time events; no timestamp filtering or match consumption
  occurs. Completion after construction includes handshakes already in progress. These counters
  describe events across the listener, not the lifecycle of a selected connection.
  Their representations name the desired event, and `__bool__` rejects unevaluated truth tests.
  No deterministic replacement for OpenSSL randomness is attempted (#68).

These are a lossless test network, not a general Internet stack. TCP congestion control, packet
reassembly, retransmission, persistent MQTT sessions, MQTT QoS 2 and bridging to host services
are outside this implementation, and so are IPv6, VLANs, DHCP for more than one device, lease
expiry and HTTP keep-alive or chunked bodies. Protocol references:
[PPP](https://www.rfc-editor.org/rfc/rfc1661),
[HDLC framing](https://www.rfc-editor.org/rfc/rfc1662),
[MQTT 3.1.1](https://docs.oasis-open.org/mqtt/mqtt/v3.1.1/os/mqtt-v3.1.1-os.html),
[DHCP](https://www.rfc-editor.org/rfc/rfc2131), [HTTP/1.1](https://www.rfc-editor.org/rfc/rfc9112).

## Lifecycle and observability

Modem dial resets and starts PPP. Supply, registration, functionality or power loss clears its
queued data, resets TCP and delivers connection-loss notifications to services. Link termination
also resets TCP. Network data mode guards `+++` with a second of idle time on each side, using
`quiet_until`/`poll`; a fourth plus or another byte cancels a pending escape. AT-only operation
retains its earlier immediate escape behavior.

Behind the access point, `Wifi.access_point()` creates the `Internet` with `link="ethernet"`,
calls `start(0)` and logs `wifi: access point '<ssid>' on the air, channel <n>`: the network is
active from power-on. `Wifi.access_point_off()` calls `stop(now)` at the board's current instant
and logs `wifi: access point off the air`; TCP connections end with
`net: TCP <device>:<port> → <address>:<port> link lost`, their services' `on_close` runs once
(MQTT Wills), and queued replies are dropped. `Wifi.access_point_on()` calls `start(now)` and logs
`wifi: access point back on the air`. Each switch also queues a `WifiAp` input for the runner. The
fresh `Ethernet` has no `device_mac` until the station sends again. The `Dhcp` and its `leases`
persist across the switches, so a test sees every lease of the run.

`Interface.lossless` defaults false; a modem with an `Internet` source returns true. `BoardLink`
matches an `rxfifo_ovf` warning by RX pin and fails only its lossless owner. No wire-protocol version
change is needed. The report appends the network transcript alongside the AT command section,
independently of the rolling wire log. JUnit stores it in the existing `esp32sim_link` property.
`Wifi` is not lossless: Ethernet frames go on the link as messages, not through a UART FIFO.
`Wifi.report()` gives the access point, the networks around and `Internet.report()`; the backend
appends it when an access point was given, and `Wifi.properties()` puts the transcript in the
`wifi` property's `network`.

`Internet.transcript()` is `{"esp32tl_network": 1, "events": [...]}`. Events have `t_ns` and either
`event` text or `stream`, `direction` (`in` to the device, `out` from it), and hex `data`. A stream
identifier is `<port>/<connection-number>`. A service wrapper records plaintext after TLS is
removed; TLS logs `tls: handshake started` and `tls: handshake done, TLSv1.2` (or the negotiated
version). This is a diagnostic transcript, distinct from the version-1 UART recording format.
The determinism checker recognizes this explicit header, compares everything before the first
handshake starts exactly, and thereafter compares event order and each connection/direction's
plaintext stream without times or chunk boundaries. Raw taps and link properties are not silently
relaxed. See [testing](../guides/testing.md#determinism).

## Validation

`tests/test_net_ppp.py`, `test_net_mru.py`, `test_net_ip.py`, `test_net_services.py`, `test_net_mqtt.py` and
`test_net_tls.py` use handmade packets, independent checksum/framing calculations and a real TLS
client in memory. They check malformed inputs, partial frames, flow control, sequence wrap,
connection loss, keepalive, retained delivery, topic filters, Wills, UTC and certificate trust.
`tests/test_net_mqtt_policy.py` drives TCP packets with both plain MQTT and a verified MemoryBIO
TLS client. It checks all CONNACK codes, retries after credential/policy changes, accepted-client
and attempt histories, rejection before takeover, pipelined packet isolation and Will suppression.
It also checks immutable wire metadata, anonymous-ID allocation only on acceptance, built-in
empty-ID rejection precedence, malformed CONNECT exclusion and propagation of policy errors,
both out of `on_data` and as a link failure in a `BoardLink` turn. A test through `Internet`
covers its traced plain and TLS listeners and keeps the policy and both histories across reset.
MQTT temporal tests send CONNECT and PUBLISH in one callback, check both legacy and explicit
next-event APIs, history filtering, equal timestamps, repeated polls and boolean protection.
`tests/test_clock.py::test_mqtt_immediate_response_is_visible_after_waiting_for_connection`
delivers both events in one runner slice, waits for CONNECT, then retrieves the pre-armed
publication without advancing time; a newly created condition times out with its MQTT name.
TLS reconnect tests complete real MemoryBIO handshakes around a failed attempt and reset,
checking that old successes never satisfy a newly armed completion condition and that a
handshake already in progress can satisfy a later-created completion condition.
NTP tests cover silence from startup, idempotent transitions and resumed replies using elapsed
world time. A MemoryBIO handshake with verification disabled solely for certificate inspection
checks independent leaf validity dates, UTC normalization and a signature from the unchanged CA;
firmware tests enforce those dates against its NTP clock.
`tests/test_modem_model.py` checks network selection, binary pluses, guarded escape, reset and the
route-specific overflow policy. `tests/test_pppos_client.py` exercises unmodified firmware in
both directions. `tests/test_determinism.py` includes plain PPP/MQTT with a raw tap, a TLS
handshake with a plaintext transcript, and `test_wifi_http`, `http_request` fetching from the HTTP
server behind the access point with its `network.json` transcript. Firmware TLS/NTP validation lives in
`examples/position_tracker/tests/test_tracker.py`, using NVS credentials and the same in-process
services.

`tests/test_net_ethernet.py` builds Ethernet frames by hand, with independent checksums: DHCP
DISCOVER/OFFER and REQUEST/ACK with router and DNS, a NAK for another address and silence for
another server, `leased()`, ARP answered for every address but the device's, IPv4 reaching the
services and other ethertypes logged once, nothing sent to a station that has sent nothing, and a
SYN to port 80 through `Internet(link="ethernet")` answered with a SYN-ACK to the station's MAC one
`LATENCY_NS` later. `tests/test_net_http.py` drives
`HttpServer` connections without TCP: serving and keeping a request, a body by `Content-Length`,
bytes, `HEAD` and absolute form, 404 and `requested`'s filters, the refused requests, what
`serve` refuses, the path of each form of target (`/proxy/http://example.com/fw.bin` among them),
and `204` and `304` ending with their head. `tests/test_wifi.py` checks the `Wifi` interface without the emulator: the
runner's flags, the refusals, a backend that refuses the access point, and the `WifiAp` inputs at
the instants of the switches. `tests/test_http_request.py` runs ESP-IDF's unmodified
`http_request` in the runner: the fetch, a rejoin after `access_point_off()` and `access_point_on()`
(with `Wi-Fi disconnected 3` printed), no access point, a wrong password that gets no lease, the
report and properties, and `wifi` refused after power-on. Not covered: MQTT, NTP and TLS behind
the access point; the layer tests cover them without a link or over PPP.

## See also

- [network-user.md](network-user.md): using the network behind the modem and the access point.
- [Protocol](../protocol.md): the `eth_tx`, `eth_rx` and `wifi_ap` messages.
- Decisions: [the network is simulated in the harness, in emulated
  time](../decisions/harness.md#the-network-the-device-reaches-is-simulated-in-the-harness-in-emulated-time),
  [behind the access point it speaks Ethernet, ARP and
  DHCP](../decisions/harness.md#behind-the-access-point-the-network-speaks-ethernet-arp-and-dhcp),
  [an HTTP server on port 80 of every
  network](../decisions/harness.md#an-http-server-answers-on-port-80-of-every-simulated-network).
