# Simulated network: implementation

`esp32tl/net/` owns the network. `Internet` is either a UART `Source` attached to the modem model
by `interfaces.modem.Modem.network()`, or the network behind the Wi-Fi access point that
`interfaces.wifi.Wifi.access_point()` creates. The `broker` fixture calls `modem.network()` and
returns its `Broker`. All callbacks execute during link turns or while the test holds the board.
There are no sockets, worker threads or host-clock timers. `Internet` uses the current UART chunk's
or Ethernet frame's timestamp inside receive callbacks and the held board time for test calls.
Responses carry a 1,000,000 ns latency (`LATENCY_NS`), the path's RTT when the test sets one; the
modem starts PPP after CONNECT, so LCP follows the AT response. `poll` takes what has come up the
path, evaluates broker keepalive and PUBACK deadlines and drains queued replies; `quiet_until`
bounds the next turn by the earliest of those deadlines and the path's next datagram.

## The link

`Internet(now, world, *, link="ppp")` takes `link`, `"ppp"` or `"ethernet"` (the `Link` type),
and keeps it for its life. `_peer()` builds the link object from it: a `Ppp` whose `ended`
callback is `_link_ended`, which resets TCP and clears the path, or an `Ethernet` given the
network's `Dhcp`. Both send through the same closure, which queues each frame or chunk of the link's
own at `now + LATENCY_NS` and takes an IPv4 datagram down the path, and both hand IPv4 datagrams to
`_uplink`, up the path to `Network.receive`. `Internet.link` is that object; `Internet.ppp` and `Internet.ethernet` return it
when it is of their kind and raise `AttributeError` otherwise. The IP layer's MTU callback reads
`Ppp.peer_mru` or `Ethernet.mtu` (1500).

Every `Internet` builds the same services whatever its link: `Dns("10.64.0.1")` on UDP 53, `Ntp` on
UDP 123, the `Broker` on TCP 1883, the `HttpServer` on TCP 80, each TCP one wrapped in `_Traced`
for the transcript, and one `Dhcp`. Only `Ethernet` calls the `Dhcp`; a network behind a modem has
one that nothing asks.

`start(t)` stops the network first, then marks it active and, for PPP only, starts LCP. `stop(at)`
marks it inactive, resets TCP (`Network.reset`, which notifies every connection once), clears the
path, drops the queued replies and replaces the link with a fresh one from `_peer()`. The `Dhcp`, the services and
the transcript are not replaced.

Behind the access point, `Wifi` is the link's `Interface` whose `ethernet` property is true: the
`BoardLink` hands it each `eth_tx` frame as `receive(t, "out", frame)`, and sends each output it
returns as one `eth_rx` ([protocol](../protocol.md)). `Wifi.receive` and `Wifi.poll` return
nothing while the network is missing or inactive. `Wifi.inputs` returns a `WifiAp` for each switch
the test made, stamped at its instant or at the turn's `now`, whichever is later.

## The path

`Internet` holds a `_Direction` each way, `_up` and `_down`: a `delay`, a `rate` (`None` for no
limit), a FIFO `queue` of `(leaves, data)`, and the times its line is free (`_free`) and its last
datagram leaves (`_last`). `leaves(at, size)` starts a datagram at `max(at, _free)`, adds
`ceil(size * 8e9 / rate.bit_s)` nanoseconds to `_free`, and returns
`_last = max(_last, _free + delay)`, so that none overtakes another when `set_path` shortens the
delay. `set_path` sets `_up.delay` to `(rtt - LATENCY_NS) // 2` and `_down.delay` to the rest. The
two add up to `rtt` to the nanosecond, and at the default `_up.delay` is 0.

Up: `_uplink`, the link's `on_ip`, queues the datagram with `_up.leaves` at `now()` and calls
`_arrive(now)`, which pops every entry whose time has come and calls `Network.receive` with `_at`
set to that entry's time, not the turn's. A datagram can come due before the turn that takes it: the
runner's turn comes up to `latency` after the request, so with an RTT of 1.5 ms a datagram 0.25 ms up
is taken up to 0.75 ms late, stamped as it should be. Its answer leaves at `t + rtt`, which is at or
after that turn, because `rtt >= latency`: that is the floor, and rule 2 of the
[protocol](../protocol.md#6-rules) is why the check refuses less. `receive` and `poll` call
`_arrive` first; at the default it runs inside `link.receive`, at the chunk's own `t`, as if there
were no path.

The test must see what has come up by the turn it holds. `BoardLink._turn` holds the turn before it
polls, so a datagram taken in `poll` would be there only at the next step of the test's clock, with a
stamp already behind `board.now`. So `_turn` first calls `Interface.arrive(now)` on each interface:
`Uart.arrive` passes it to a source that is `Arriving`, `ModemModel.arrive` to its `Internet` in data
mode, `Wifi.arrive` to its active network, and `Internet.arrive` runs `_arrive(now)`. Answers made
there stay in `_out` until `poll`, after the hold, so what the test does in the turn comes after what
the network took in it. The broker's own deadlines, keepalive and a delayed PUBACK, still run in
`poll`, after the hold, as they did.

Down: `Network`'s `send_ip` is `_downlink`, which notes the datagram's size in `_datagram` around
`link.send_ip`. The link's `send` closure finds `_datagram` set, puts the framed bytes in `_down`
with `_down.leaves`, and calls `_release`, which moves to `_out`, with their own stamps, those that
leave within `LATENCY_NS` of `now()`. The framing and the link's checks (the MRU, the MTU, no station
yet) happen when the datagram enters, and a dropped one uses none of the rate. What stays in `_down`
can still be dropped by a reset, which matters at a large RTT: after the modem loses registration,
nothing on the path reaches the firmware. At the default every datagram is released in the call that
made it, so `receive` returns it stamped `t + LATENCY_NS`, in the order it was made.

`quiet_until` is the minimum of the broker's and the two heads: one turn per datagram. Each head is
past `now` after `poll`, since `_arrive` and `_release` took all up to `now` and `now + LATENCY_NS`,
as `BoardLink._turn` requires. `latency()` stays `LATENCY_NS`: PPP control answers in 1 ms, and a
larger promise would break rule 2. The runner does not change. On the modem's route it paces bytes
as it does (protocol, rule 5), and the rate is on top of that.

Nothing here reads the host's clock, and the arithmetic is on integers but for the rate's division,
so two runs give the same stamps (`tests/test_determinism.py::test_mqtt_session_on_a_path`).

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
  other frame. `_whole` cuts an IPv4 payload at its total length when that is possible (ESP-IDF's
  frames carry 4 bytes more, [protocol 5.15](../protocol.md#515-eth_tx-and-eth_rx-ext-m7)), so that
  the rate counts the datagram only. ARP (`0x0806`): only Ethernet/IPv4 requests of 28 bytes or more are read; each is
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
- `condition.py`: `Condition(find, description, service)`, the frozen object that the MQTT,
  HTTP and DHCP condition factories return. Calling it calls `find`; `repr` is `description`,
  which `board.wait_until` takes because the object has no `__qualname__`; `__bool__` raises
  `TypeError("call the <service> condition with (), or pass it to board.wait_until")`, `service`
  being `MQTT`, `HTTP` or `DHCP`. TLS keeps its own `_HandshakeCondition`, which behaves the same
  way and computes its `repr` from its fields.
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
  `Lease(at, mac, address, hostname)` to `leases`, `hostname` from option 12. `next_lease()`
  captures the length of `leases` at construction and returns a `Condition` reading the entry at
  that index, named `the next DHCP lease`; `leased()` delegates to it. `find_lease()` reads the
  first lease.
- `http.py`: `HttpServer(now, log)`, a `StreamService` on TCP 80 of every network (RFC 9110,
  9112). `serve(path, body, *, status, content_type, headers)` stores a `Response` per path, after
  checking that the path starts with `/` and holds no `?`, since a request is looked up by its
  path without the query, that the status is in `REASONS` and that no header holds CR, LF
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
  ignored. `next_request(path, method=...)` captures the length of `requests` at construction and
  returns a `Condition` scanning from that index for an exact `path` and `method` match, named
  `the next HTTP request`, then ` for '<path>'` and ` with method '<method>'` for the filters
  given; `requested` delegates to it. `find_request` scans from index zero with the same filters.
  Both refuse a `path` holding `?` with `ValueError`, through `_refuse_query`: `_path` strips the
  query of origin-form and absolute-form targets, so such a filter would match nothing. A target
  of no form `_path` knows, as `foo?bar`, keeps its `?` in `path`; only `requests` reaches it.
- `ntp.py`: accepts NTP v3/v4 client requests; returns a v4 stratum-1 server response. The
  origin timestamp copies the client's transmit field. Reference, receive and transmit stamps
  use world UTC plus emulated time, including fractions and the 32-bit NTP era wrap.
  `go_silent` / `resume` toggle whether new datagrams receive replies, without changing the
  UDP binding, the clock or already queued packets. `requests` continues to count replies only;
  each state transition is logged once, and resuming never replays unanswered datagrams.
- `mqtt.py`: incremental MQTT 3.1.1 parsing (up to 1 MiB per packet); CONNECT, QoS 0/1 PUBLISH,
  PUBACK, SUBSCRIBE/SUBACK, UNSUBSCRIBE/UNSUBACK, PING and DISCONNECT. Retained state belongs to
  the test's broker; session state (`_State`: subscriptions, the queue, the packet IDs in use and
  what was sent unacknowledged) to a client ID, in `Broker._states`, and `_Session` is one
  connection. Empty clean-session client IDs are
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
  affects subsequent CONNECTs only. `session_present` is worked out before the attempt is appended:
  CleanSession=0 and a stored `_State` that is not `clean`. A clean one exists only while its
  connection does, and the takeover ends it. After the takeover, CleanSession=1 replaces any
  `_State`, and CleanSession=0 makes one if there is none; `_connect` sets `state.connection`, writes
  CONNACK and, with Session Present, calls `_resume`: every entry of `state.sent`, in insertion
  order with DUP set, then `_drain`, which sends `state.queue` through `deliver` while fewer than
  65535 IDs are in use. Each PUBACK calls `_drain` again, so a queue longer than the IDs goes out as
  they are freed, on a connection that stays open. `_route` calls `Broker._offer` for each `_State`: a connected one goes to
  `deliver`, or for QoS 1 behind a queue still waiting for IDs, a stored one queues a message by the message's QoS or logs dropping it, and a clean
  one closed while routing takes nothing. `deliver` allocates from `state.packet_id`, skipping
  `state.unacked`, and `_publish` appends a `Delivery` and fills `state.sent` before it writes.
  When 65535 IDs are in use `_packet_id` logs `mqtt: <id> exhausted packet identifiers` and closes;
  this is what a QoS 1 message for a connected client with no ID free and no queue meets, and
  `deliver` returns `False`, so that it stays queued for a stored session. PUBACK discards the
  ID, pops `state.sent` and stamps that `Delivery`'s `acknowledged_at`; a connection already closed
  ignores its data, PUBACK included, in `on_data`. `_end` clears `state.connection` and removes a
  `clean` state from `_states`. `session` copies a `_State`'s collections into a frozen
  `SessionState`, whose `Delivery` records stay the broker's;
  `forget_session` refuses a connected one. Network reset retains both histories and the policy.
  Will is published once with the time
  of disconnect, not the time of CONNECT. A replacement client ID closes the old connection.
  QoS 2 publication is unsupported. Credentials are observed by default;
  authentication is performed only by an installed test policy, with no external auth service.
  `next_message` and `next_connection` capture the length of their respective append-only
  histories at construction; `message` and `connected` delegate to them, and
  `connection_attempt` captures `connection_attempts` the same way, and `next_delivery` captures
  `deliveries`, filtering as `next_message` does. Matching uses append order,
  not timestamps, and polling does not change the captured offset. `find_message` scans
  from index zero with the same topic/payload filters; `find_connection` reads the first CONNECT.
  These are immediate queries returning records or `None`. Every MQTT condition, the live
  subscription query included, is a `Condition` of `condition.py` with service `MQTT`.
  Subscriptions continue to read active sessions, without latching matches.
  Each accepted stream receives a broker-local increasing `connection_id`, independent of the
  client ID and listener. Every incoming PUBLISH records its packet ID and DUP bit; `_handle`
  rejects DUP on QoS 0 as malformed. QoS 1 receipts reference a public `Publication`; the
  session's `incoming` map groups pending receipts by packet ID. Each receipt still reaches
  `_route`, including duplicates; one whose topic or payload differs from the handle's first
  receipt is merged as well, with a log line. PUBACK emission removes that map entry; later use
  of the ID creates a new handle. The handle's `messages` tuple exposes the receipts, including
  their original timestamps and DUP bits.
  `Broker.puback` is sampled when a handle is created: immediate and zero delays release it
  after routing, `"hold"` has no deadline, and a `Duration` supplies an absolute deadline from
  the first receipt. Duplicates and policy changes leave that deadline unchanged. `poll`
  expires keepalives first, then releases due handles in connection/receipt order. A late poll
  stamps emission at its actual current time, never backdates it to the deadline.
  `Publication.release()` writes PUBACK to the stream, marks it `"emitted"` and records
  `puback_at`; a synchronous TLS write failure closes the session and leaves it `"cancelled"`
  instead. Releasing a finished handle returns `False` without writing. `_end` cancels and
  clears pending handles before closing the stream, so closure through TLS cannot resurrect
  them. `Broker.disconnect(client_id)` uses the same loss/Will path as transport loss and
  never resets the network. An emitted handle survives closure as an emission record, not
  evidence of device processing. Histories, retained messages and stored sessions survive
  reconnect; incoming ACK state does not.
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
reassembly, retransmission, MQTT QoS 2 and bridging to host services
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
MQTT temporal tests send CONNECT and PUBLISH in one callback, check both the `message` /
`connected` aliases and the `next_*` methods, history filtering, equal timestamps, repeated
polls and boolean protection.
`tests/test_clock.py::test_mqtt_immediate_response_is_visible_after_waiting_for_connection`
delivers both events in one runner slice, waits for CONNECT, then retrieves the pre-armed
publication without advancing time; a newly created condition times out with its MQTT name.
TLS reconnect tests complete real MemoryBIO handshakes around a failed attempt and reset,
checking that old successes never satisfy a newly armed completion condition and that a
handshake already in progress can satisfy a later-created completion condition.
`test_net_mqtt.py` also checks held/delayed/default PUBACK, duplicate routing, identifier reuse
(including the log for a pending ID reused with other content), DUP on QoS 0, connection IDs
taken by rejected CONNECTs, policy changes, early release and cancellation on every
session-ending path. These tests use the clock directly to check exact deadlines and release
timestamps.
`tests/test_net_mqtt_failures.py` negotiates PPP and drives real TCP frames with independent
checksums, both with plaintext and a verifying MemoryBIO TLS client. It receives without ACK,
releases and delays PUBACK, disconnects before ACK, then reconnects with CleanSession=0 and
retransmits with DUP. During the outage another stream still answers PING and a new NTP request
traverses the same open PPP session. It also checks that CONNACK has Session Present, that the
routed copies come again in order and that the subscription is restored. These are
protocol-client tests.
`tests/test_net_path.py` asserts the path's stamps exactly, on PPP frames and Ethernet frames made by
hand: the RTT from a request to its answer at odd nanoseconds, each direction's share, the rates
each way, one datagram after another, a change that overtakes nothing, the floor's message, control
traffic passing the queue, what a reset and the end of the link drop, and `quiet_until`.
`tests/test_clock.py` holds real turns on `HeldClock` with a datagram on the path: a wait that ends at
its time finds it taken, and `wait_until` returns at it. `tests/test_boardlink.py` checks
`arrive`, the hold and `poll` in that order.
`tests/test_mqtt_session.py` also runs esp-mqtt on a path: the spacing of a backlog's exchanges at
200 ms and 600 ms and their ratio, the rate, and a redelivery over 600 ms.
`tests/test_net_mqtt_sessions.py` asserts the broker's sessions in exact bytes, flags and packet
IDs from raw packets: Session Present, the queue and its order, QoS 0 dropped, DUP and original IDs
on redelivery before the queue, CleanSession=1 at CONNECT and at disconnection, takeover, a refused
CONNECT and the policy leaving a session alone, a `Publication` staying with its connection, retained
messages and RETAIN, running out of packet IDs, `session`, `forget_session`, `deliveries` and
`next_delivery`. `tests/test_mqtt_session.py` runs esp-mqtt: Session Present, a QoS 1 message
published while offline, and one redelivered with DUP after a `broker.disconnect` in the same
instant as the publication, with both PUBACKs in the transcript.
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
another server, `next_lease()` and its `leased()` alias with append order at equal timestamps,
non-consuming polls, `find_lease()`, the condition's name and its refusal of `bool()`, ARP
answered for every address but the device's, IPv4 reaching the services and other ethertypes
logged once, nothing sent to a station that has sent nothing, and a SYN to port 80 through
`Internet(link="ethernet")` answered with a SYN-ACK to the station's MAC one `LATENCY_NS` later.
`tests/test_net_http.py` drives `HttpServer` connections without TCP: serving and keeping a
request, a body by `Content-Length`, bytes, `HEAD` and absolute form, 404 and `next_request`'s
filters, `find_request`, `next_request` and its `requested` alias with append order at equal
timestamps and non-consuming polls, each condition's name and its refusal of `bool()`, a filter
holding a query refused while the query stays in `target`, `serve` refusing a path with a query,
the refused requests, unseen by the conditions and queries, what `serve` refuses, the path of each form of target
(`/proxy/http://example.com/fw.bin` among them), and `204` and `304` ending with their head.
`tests/test_wifi.py` checks the `Wifi` interface without the emulator: the runner's flags, the
refusals, a backend that refuses the access point, and the `WifiAp` inputs at the instants of the
switches. `tests/test_http_request.py` runs ESP-IDF's unmodified `http_request` in the runner:
the fetch, a rejoin after `access_point_off()` and `access_point_on()` (with
`Wi-Fi disconnected 3` printed), a `board.wait_until` timeout and run report naming
`the next HTTP request for '/'` and `the next DHCP lease`, no access point, a wrong password that
gets no lease, the report and properties, and `wifi` refused after power-on. Not covered: MQTT,
NTP and TLS behind the access point; the layer tests cover them without a link or over PPP.

## See also

- [network-user.md](network-user.md): using the network behind the modem and the access point.
- [Protocol](../protocol.md): the `eth_tx`, `eth_rx` and `wifi_ap` messages.
- Decisions: [the network is simulated in the harness, in emulated
  time](../decisions/harness.md#the-network-the-device-reaches-is-simulated-in-the-harness-in-emulated-time),
  [behind the access point it speaks Ethernet, ARP and
  DHCP](../decisions/harness.md#behind-the-access-point-the-network-speaks-ethernet-arp-and-dhcp),
  [an HTTP server on port 80 of every
  network](../decisions/harness.md#an-http-server-answers-on-port-80-of-every-simulated-network),
  [the broker keeps a session by client ID when CleanSession is
  0](../decisions/harness.md#the-broker-keeps-a-session-by-client-id-when-cleansession-is-0),
  [a path with RTT and rate sits between the link and the
  network](../decisions/harness.md#a-path-with-rtt-and-rate-sits-between-the-link-and-the-network),
  [packet loss on the path is postponed](../decisions/harness.md#packet-loss-on-the-network-path-is-postponed).
