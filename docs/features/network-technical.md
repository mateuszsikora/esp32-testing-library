# Simulated network: implementation

`esp32tl/net/` owns the network. `Internet` is a UART `Source` attached to the modem model by
`interfaces.modem.Modem.network()`. The `broker` fixture calls it and returns its `Broker`.
All callbacks execute during link turns or while the test holds the board. There are no sockets,
worker threads or host-clock timers. `Internet` uses the current UART chunk timestamp inside
receive callbacks and the held board time for test calls. Responses carry a 1,000,000 ns latency;
the modem starts PPP after CONNECT, so LCP follows the AT response. `poll` drains queued replies
and evaluates broker keepalive deadlines; `quiet_until` bounds the next turn by those deadlines.

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
  and an MTU callback reading the current PPP peer MRU. Each TCP payload is limited by that MRU
  minus the 20-byte IPv4 and 20-byte TCP headers, as well as MSS and available window space.
  UDP replies are checked against MRU (including 28 header bytes) before packing; an oversized
  reply is dropped and logged without fragmentation. TCP also handles
  receive sequence wrap, duplicate-data acknowledgments, FIN and reset. It never initiates a
  connection or retransmits. Unbound TCP ports return RST; unbound UDP is logged and dropped.
  Connections receive `on_close` once, also on RST and PPP loss. There is no ICMP or IPv6.
- `dns.py`: one bounded question, including compressed names with loop detection; preserves the
  query ID and recursion-desired bit. IN A answers have TTL 60 and address `10.64.0.1`.
- `ntp.py`: accepts NTP v3/v4 client requests; returns a v4 stratum-1 server response. The
  origin timestamp copies the client's transmit field. Reference, receive and transmit stamps
  use world UTC plus emulated time, including fractions and the 32-bit NTP era wrap.
- `mqtt.py`: incremental MQTT 3.1.1 parsing (up to 1 MiB per packet); CONNECT, QoS 0/1 PUBLISH,
  PUBACK, SUBSCRIBE/SUBACK, UNSUBSCRIBE/UNSUBACK, PING and DISCONNECT. Retained state belongs to
  the test's broker; subscriptions belong to a connection. Empty clean-session client IDs are
  replaced before Will construction and takeover checks with `esp32tl-anonymous-<n>`. A per-broker
  counter and connection history prevent reuse or collision with an earlier supplied ID.
  Empty IDs without CleanSession get CONNACK `00 02` followed by close.
  Will is published once with the time
  of disconnect, not the time of CONNECT. A replacement client ID closes the old connection.
  Session persistence and QoS 2 publication are unsupported; credentials are observable only.
- `tls.py`: a generated RSA-2048 CA and server certificate, SHA-256 signatures, DNS/IP subject
  alternative names, CA key usage and server extended key usage. Temporary key files are removed
  after `SSLContext.load_cert_chain`. TLS uses `SSLObject` and two `MemoryBIO`s; plaintext reaches
  the inner service only after handshake completion. TLS errors close the connection and notify
  the inner service once. No deterministic replacement for OpenSSL randomness is attempted (#68).

These are a lossless test network, not a general Internet stack. TCP congestion control, packet
reassembly, retransmission, persistent MQTT sessions, MQTT QoS 2 and bridging to host services
are outside this implementation. Protocol references: [PPP](https://www.rfc-editor.org/rfc/rfc1661),
[HDLC framing](https://www.rfc-editor.org/rfc/rfc1662),
[MQTT 3.1.1](https://docs.oasis-open.org/mqtt/mqtt/v3.1.1/os/mqtt-v3.1.1-os.html).

## Lifecycle and observability

Modem dial resets and starts PPP. Supply, registration, functionality or power loss clears its
queued data, resets TCP and delivers connection-loss notifications to services. Link termination
also resets TCP. Network data mode guards `+++` with a second of idle time on each side, using
`quiet_until`/`poll`; a fourth plus or another byte cancels a pending escape. AT-only operation
retains its earlier immediate escape behavior.

`Interface.lossless` defaults false; a modem with an `Internet` source returns true. `BoardLink`
matches an `rxfifo_ovf` warning by RX pin and fails only its lossless owner. No wire-protocol version
change is needed. The report appends the network transcript alongside the AT command section,
independently of the rolling wire log. JUnit stores it in the existing `esp32sim_link` property.

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
`tests/test_modem_model.py` checks network selection, binary pluses, guarded escape, reset and the
route-specific overflow policy. `tests/test_pppos_client.py` exercises unmodified firmware in
both directions. `tests/test_determinism.py` includes plain PPP/MQTT with a raw tap and a TLS
handshake with a plaintext transcript. Firmware TLS/NTP validation is #72's tracker test.
