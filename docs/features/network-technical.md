# Simulated network: implementation

`esp32tl/net/` owns the network. `Internet` is either a UART `Source` attached to the modem model
by `interfaces.modem.Modem.network()`, or the network behind either virtual Wi-Fi role created by
`interfaces.wifi.Wifi.access_point()` or `Wifi.station()`. The `broker` fixture calls `modem.network()` and
returns its `Broker`. All callbacks execute during link turns or while the test holds the board.
There are no sockets, worker threads or host-clock timers, but for a bridge's
([below](#the-bridge)). `Internet` uses the current UART chunk's
or Ethernet frame's timestamp inside receive callbacks and the held board time for test calls.
Responses carry a 1,000,000 ns latency (`LATENCY_NS`), the path's RTT when the test sets one; the
modem starts PPP after CONNECT, so LCP follows the AT response. `arrive`, before the test holds the
turn, takes what has come up the path and evaluates broker keepalive and PUBACK deadlines and the
SYN timers of the connections the test opened (`Network.poll`); `poll`, after the hold, does the
same and drains queued replies; `quiet_until` bounds the next turn by the earliest of those
deadlines and the path's next datagram.

## The link

`Internet(now, world, *, link="ppp", mac="02:53:54:41:00:01", bridging=...)`, whose `world` defaults to `esp32tl.net.ntp.DEFAULT_UTC`, as the modem's and `Wifi`'s do, takes `link`, `"ppp"`, `"ethernet"` or `"station"` (the `Link` type),
and keeps it for its life. `bridging(call)` is called by `bridge` before it makes a bridge, and
refuses it by raising; it does nothing by default, and the `esp32sim` backend's refuses after
power-on and in front of a replay ([backend](esp32sim-backend-technical.md#a-run-with-a-real-source)). `_peer()` builds the link object from it: a `Ppp` whose `opened`
callback records the link up ([link changes](#link-changes)) and whose `ended` callback,
`_ppp_ended`, records the down with the cause `Ppp` gives and calls `_link_ended`, which calls
`Network.lose_link` and clears the path, an `Ethernet` given
the network's `Dhcp`, or a host-side `Station` with its own DHCP client. All send through the same
closure, which queues link control traffic at `now + LATENCY_NS` and takes IPv4 datagrams down
the path, and all hand IPv4 datagrams to
`_uplink`, up the path to `Network.receive`. `Internet.link` is that object; `Internet.ppp` and `Internet.ethernet` return it
when it is of their kind and raise `AttributeError` otherwise. The IP layer's MTU callback reads
`Ppp.peer_mru`, `Ethernet.mtu` or `Station.mtu` (1500 for either Ethernet role).

Every `Internet` builds the same services whatever its link: `Dns("10.64.0.1", now, log)` on UDP
53, `Ntp(now, world, log)` on UDP 123, both with `Internet.now`, which is the time a datagram
reaches the network while `_arrive` hands it over, the `Broker` on TCP 1883, the `HttpServer` on
TCP 80, each TCP one wrapped in `_Traced` for the transcript, and one `Dhcp`. Only `Ethernet` calls
the `Dhcp`; a network behind a modem has one that nothing asks, as does a network behind a virtual
station. That station uses its own DHCP client and updates the DNS answer to its leased address.

`start(t)` stops the network first, then marks it active and, for PPP only, starts LCP.
`stop(at=None, cause="call ended")` records the down with `cause` when the link was up, marks it
inactive, calls `_link_ended`, which loses the connections the test opened and leaves the
device's half-open (`Network.lose_link`), clears the path, drops the queued replies and replaces
the link with a fresh one from `_peer()`. The `Dhcp`, the services and
the transcript are not replaced, and neither are the bridges.

Behind the access point, `Wifi` is the link's `Interface` whose `ethernet` property is true: the
`BoardLink` hands it each `eth_tx` frame as `receive(t, "out", frame)`, and sends each output it
returns as one `eth_rx` ([protocol](../protocol.md)). `Wifi.receive` returns nothing while the
network is missing or inactive. `Wifi.arrive` and `Wifi.poll` reach an inactive network too, so its
broker's deadlines run off the air; its `poll` then returns nothing. `Wifi.inputs` returns a
`WifiAp` for each switch
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
`Uart.arrive` passes it to a source that is `Arriving`, `ModemModel.arrive` to its `Internet`,
`Wifi.arrive` to its network, in command mode and off the air too, and `Internet.arrive` runs
`_arrive(now)`, then
`Broker.poll()`, with `_at` set to `now`. Answers made there stay in `_out` until `poll`, after the
hold, so what the test does in the turn comes after what the network took in it. The broker's own
deadlines, keepalive and a delayed PUBACK, run there too, for the same reason: the runner stops at
the deadline, which `quiet_until` gives, and the test holding that turn sees the PUBACK emitted, the
connection closed and its Will in `messages`. A PUBACK goes out in `poll`, like any answer. `poll`
still runs `_arrive(now)` and `Broker.poll()` before it takes `_out`: after `arrive` at the same
`now` they find nothing, and a caller that polls alone, as the network's unit tests do, gets the
same answers at the same stamps. `SppPeer` passes `poll` to its source and not `arrive`, so a
modem model with a network as the source of a Bluetooth peer would take datagrams in `poll`; nothing
makes that pair today.

Down: `Network`'s `send_ip` is `_downlink`, which notes the datagram's size in `_datagram` around
`link.send_ip`. The link's `send` closure finds `_datagram` set, puts the framed bytes in `_down`
with `_down.leaves`, and calls `_release`, which moves to `_out`, with their own stamps, those that
leave within `LATENCY_NS` of `now()`. The framing and the link's checks (the MRU, the MTU, no station
yet) happen when the datagram enters, and a dropped one uses none of the rate. What stays in `_down`
can still be dropped by a reset, which matters at a large RTT: after the modem loses registration,
nothing on the path reaches the firmware. While the network is not `active`, `_downlink` drops every
datagram before the link sees it: what a service writes on a connection the link's end left open
goes nowhere, and logs nothing. At the default every datagram is released in the call that
made it, so `receive` returns it stamped `t + LATENCY_NS`, in the order it was made.

`quiet_until` is the minimum of the broker's and the two heads: one turn per datagram. While the
network is not `active` it is the broker's and the bridges', so the keepalive of a connection the
link's end left open still gets its turn, and a bridged service that sends or closes then is taken
in the next millisecond, as with the link up; `ModemModel` and `Wifi` ask for it then too. Each head
is past `now` after `poll`, since `_arrive` and `_release` took all up to `now` and `now +
LATENCY_NS`, as `BoardLink._turn` requires. `latency()` stays `LATENCY_NS`: PPP control answers in 1
ms, and a larger promise would break rule 2. The runner does not change. On the modem's route it
paces bytes as it does (protocol, rule 5), and the rate is on top of that.

Nothing here reads the host's clock, and the arithmetic is on integers but for the rate's division,
so two runs give the same stamps (`tests/test_determinism.py::test_mqtt_session_on_a_path`).

### The black hole

`set_path(drop=True)` sets `Internet._drop`, and every other call clears it; the values of the two
directions are set as for the default path, and the call logs `net: path: drop every datagram`.
After the checks of `rtt` and the rates, `drop` not a `bool` raises `TypeError`, and `drop=True`
with an `rtt` other than `DEFAULT_RTT` or a rate raises `ValueError`; a refusal changes nothing.
While `_drop` is set, `_uplink` hands the datagram to `_dropped(datagram, "up")` instead of
`_up.leaves`, and `_downlink` hands it to `_dropped(datagram, "down")` before the link sees it,
after the check of `active`. A dropped datagram so touches neither `_free` nor `_last`, which is why
the first datagram after the restore leaves at its own RTT. `_dropped` parses the datagram with
`ipv4` for the log line, `net: path dropped <what> going <up|down>`; a fragment or a datagram that
does not parse is `<n> bytes`. The link's own frames never pass through `_uplink` or `_downlink`:
LCP, IPCP, ARP, DHCP and the virtual station's own DHCP go on as before.

`_carries` is false while `_drop` is set, so `Network._flush` holds what a flow that is not orphaned
has to send in `pending`, its FIN behind it, as it does while the link carries no IP, and its
retransmission timer is stopped ([Retransmission](#retransmission)): nothing goes into the black
hole on an RTO, and no retransmission is counted there; an orphaned
flow's output reaches `_downlink` and is dropped, as it would reach no stack. The other segments
`Network` sends are answers to the device's segments: in a black hole only to those that were on the
uplink when the path began to drop, an ACK, a UDP answer or a SYN-ACK, which `_downlink` drops; and
the SYN of `Network.connect`, which `Internet.connect` refuses first, with `NotReachable`. A lost
SYN-ACK leaves a flow of the device's that is not `established`; the device's SYN again on that
tuple, with `seq + 1 == rcv_nxt`, is answered with the SYN-ACK again, from `ISN`, and logged
`net: TCP <src>:<sport> → <dst>:<dport> SYN again, answered again`, as SYN-RECEIVED does
(RFC 9293, 3.10.7.4). The service's `accept` ran once, at the first SYN, and is not run again.

A call that clears `_drop` after it was set calls `Network.resume(probe=True)` after logging the
path, which restarts the retransmission timer of every flow (`_restart`) and flushes it onto the
new path at the board's instant; `_flush` still checks
`_carries`, so a link that is down or a station with no lease keeps holding. Then `resume` calls
`Network.probe()`, `_probe` on every flow: the device's ACKs and window updates sent into the black
hole are gone, so `snd_una` and `peer_window` may be stale, and with a full or a zero window
`_flush` sends nothing and the device has nothing to send. For every established flow that is not
orphaned nor on a `"drop"` port, the test's own included, with `snd_una < snd_nxt`, `pending`, or a
FIN pending and not sent, `_probe` sends one ACK with no data from `snd_nxt - 1`, a keepalive's (RFC
9293, 3.8.4). lwIP answers a segment outside its window at once with its ACK and window, and the ACK
handling in `_tcp` then calls `_flush`. The path is a FIFO, so what was sent before the probe
reaches the device before it. While `_carries` is false, as behind a virtual station with no lease,
`_probe` sends nothing and sets `_owed`; the next `resume`, which `_station_changed` calls when the
station has a lease again, probes every flow then and clears it. `forget_device` for the whole
stack, at the end of a PPP link among others, clears it too: no flow it was owed to is left.
`_link_ended` does not touch `_drop`: like the rest of the path's values it outlives a reset.

### Blocked ports

`Network.blocked` maps a TCP port of the network to `"drop"` or `"refuse"`.
`Internet.block(port, how=)` checks the port as `bridge` does and `how`, logs
`net: block: TCP <port>, <how>` and calls `Network.block`. `Internet.unblock(port)` checks the port
the same way first, so `unblock(True)` does not reach port 1, raises unless the port is in
`blocked`, logs `net: unblock: TCP <port>` and calls `Network.unblock`.

Both end in `Network._release(port, probe=)`, which flushes every flow of the device's whose network
port is `port`, so a change from `"drop"` sends what was held; when the port was `"drop"` it first
restarts their retransmission timers (`_restart`), which the block had stopped. Only when the port was `"drop"`
before the call does it then call `_probe` with those flows, for the ACKs and windows the block
swallowed: a `block(port, how="refuse")` of a port that was not blocked, or an `unblock` of a
`"refuse"` port, swallowed nothing and sends no probe.

`Network._tcp` looks the port up once the segment's flow is known, for a flow of the device's or no
flow; a flow the test opened, whose network port is its ephemeral one, is never blocked. With
`"drop"` it logs `net: TCP <src>:<sport> → <dst>:<dport> dropped: port <dport> is blocked` and
returns, before any state changes; with `"refuse"`, a SYN that would make a new flow is answered as
for a port with no service, RST|ACK from sequence 0 acknowledging the SYN, and logged
`net: TCP <src>:<sport> → <dst>:<dport> refused, port <dport> is blocked`. Every other segment of
`"refuse"` goes on as before, so the connections already open stay. `_flush` returns at once for a
device's flow on a `"drop"` port, orphaned or not: what its service writes stays in `pending`, or is
discarded by `_write` for an orphaned flow, as it is when the window has no room. The check is where
the network takes the segment, after the path, so a segment already on its way when the port is
blocked is blocked too.

## The bridge

`esp32tl/net/bridge.py`. `Internet.bridge(port, to)` checks `port` (an `int` from 1 to 65535, else
`ValueError` `<call>: the TCP port must be between 1 and 65535`) and `to` with `address(to, call)`
(a tuple of a non-empty `str` and an `int`, else `TypeError`; a port out of range, `ValueError`),
`call` being `network.bridge(<port!r>, <to!r>)`. A port in `ip.tcp` that is not in `_builtin`
(`{1883: "the broker", 80: "the HTTP server"}`) raises `<call>: TCP port <port> already has a
service`. Then it calls `bridging(call)`, and only then takes the port out of `_builtin` and
`ip.tcp`, installs the `Bridge` through `_install`, the same `_Traced` wrapper `listen` uses, keeps
it in `bridges` by port, and logs `bridge: TCP <port> → <host>:<port>`, with
`, in place of <the broker|the HTTP server>` when it replaced one. A refusal changes nothing.

`Bridge(port, to, log)` is a `StreamService`. `accept` counts the connection in `connections` and
makes a `_Relay` named `<port>/<n>`, the same identifier as the transcript's stream. Each relay has
a thread that connects with `socket.create_connection(to, timeout=CONNECT_SECONDS)` (5 wall
seconds), then starts a reader thread and writes, with `sendall`, what `on_data` puts in a
`SimpleQueue`, in order; data sent before the connection is up waits there. The reader reads up to
`READ_BYTES` (65536) at a time. The threads never touch the network: they append to the relay's
`_arrived` list, under its lock, each read's bytes and each line (`connected to`, `cannot connect
to`, `closed by`, `lost`, `cannot write to`); every line but `connected to` sets `_ended`, after
which nothing more is appended. Once the device's end is closed, the reader appends no bytes: it
adds their count to `_dropped`. A writer whose `sendall` failed clears `_writing`, and `on_data`
drops what the device sends from then on, so that nothing fills a queue nobody reads. Failures carry `OSError.strerror`, else its text, else its type's name; one that
comes after `end` is not noted.

`Internet.arrive` and `Internet.poll` call `_pump` after `broker.poll()`, with `_at` set to the
turn's `now`: each bridge's `pump` takes every relay's `_arrived` in order, logs its lines, writes
its bytes to the device's connection, which `Network` segments and the path stamps from `now`, and
closes the device's end once `_ended` is set and its bytes are written. Bytes taken after the
device's end closed, and `_dropped`, make one line,
`bridge: <port>/<n> dropped <count> bytes <host>:<port> sent after the device closed its end`,
after the turn's other lines of that relay. Bytes thus carry the turn
they are handed over in, never the instant they came, as the real source's do
([decision](../decisions/harness.md#the-real-source-stamps-what-the-port-gave-with-the-turn-it-hands-it-over-in-every-emulated-millisecond)).
`on_close`, from the device's FIN, an RST, `Network.reset` or the relay's own close, marks the
device's end closed, under the lock, and puts `None` in the queue: the writer sends what is left,
then `shutdown(SHUT_WR)`, and what the service sends after that is read and dropped. `Network._tcp`
closes its own end on the device's FIN, so a bridged connection has no half-close. A relay leaves
the turns' list once it is `done`: its device end closed, `_ended` set, and nothing left to log. `Bridge.quiet_until(now)` is the next whole emulated
millisecond while any relay's device end is open, else `None`, and `Internet.quiet_until` takes
the minimum with its other bounds; `latency()` stays `LATENCY_NS`.

`Internet.close()` calls each bridge's `close`, which sets every relay's `_closing`, ends its
writer with `None`, shuts the socket both ways so that the reader's `recv` returns, and joins both
threads; a connect in progress ends within `CONNECT_SECONDS`. The backend calls it from
`_release` at teardown. Calling it twice does nothing.

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
  receive sequence wrap, duplicate-data acknowledgments, FIN and reset. It opens connections
  only for the test ([below](#connections-the-test-opens)), and sends again what the device has
  not acknowledged when its RTO runs out ([Retransmission](#retransmission)), and the SYN of a
  connection the test opened. Unbound TCP
  ports return RST; unbound UDP is logged and dropped.
  Connections receive `on_close` once, also on RST, PPP loss and `Internet.stop`. There is no
  ICMP or IPv6. The UDP limit's log line names it `peer MRU` behind the access point too.
- `dns.py`: one bounded question, including compressed names with loop detection; preserves the
  query ID and recursion-desired bit. IN A answers have TTL 60 and address `10.64.0.1`,
  except behind the virtual station, where they use the host's leased address. Every question it
  parses, whatever its QTYPE, is appended to `questions` as a `DnsQuestion(at, name, qtype)`, `at`
  being `Duration(now())`, before the answer is built; `asked` is a property that lists their
  names. A query it refuses (a response, an opcode other than QUERY, other than one question, a
  bad label, a name over 255 bytes or a truncated question) returns `None` before anything is
  recorded.
- `ethernet.py`: Ethernet II without FCS, ARP and the hand-off to DHCP. `Ethernet.receive` drops
  frames under 14 bytes and frames from a multicast source, and learns `device_mac` from every other
  frame. In esp32sim every frame from the station ends in 4 bytes more, libpp's guard word
  `ef be ad de`, because libpp counts 4 bytes more than the frame, taken to be its FCS, in its
  transmit descriptor's length ([protocol 5.15](../protocol.md#515-eth_tx-and-eth_rx-ext-m7)).
  `_whole` cuts an IPv4 payload at its total length when that is possible, so that the rate counts
  the datagram only; a frame without the 4 bytes gives the same datagram. A 1500-byte datagram's
  frame is 1518 bytes with them, the longest the relay and the codec take. ARP (`0x0806`): only
  Ethernet/IPv4 requests of 28 bytes or more are read; each is answered with `GATEWAY_MAC`
  (`02:53:49:4d:00:02`, esp32sim's own network's gateway in `esp-soc/src/net/packet.rs`) unless its
  target is the `Dhcp.device` address (lwIP's RFC 5227 probe before it takes the lease) or its
  sender's own address (an announcement). IPv4 (`0x0800`) to `GATEWAY_MAC`, broadcast or a multicast
  address goes on; unicast to another MAC is dropped.
  A datagram that `ip.ipv4` and `ip.udp` take, the checks `Network.receive` makes of every datagram
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
- The conditions that the MQTT, HTTP, DHCP, TLS, NTP and DNS condition factories,
  `network.is_leased`, `network.next_lease()` and `network.accepting(port)` return are
  `Condition(find, description, service)` of `esp32tl/interfaces/condition.py`, `accepting`'s
  with its `waiting` too ([below](#connections-the-test-opens)), the public
  [described condition](pytest-plugin-user.md#described-conditions), not a class of `esp32tl/net/`.
  Each method is annotated as returning `Condition[T | None]` (`Condition[bool]` for
  `is_subscribed` and the four TLS conditions), not `Callable[[], …]`, so a type checker in a test
  project keeps the `description` and accepts the condition where a helper asks for a
  `Condition`; `tests/test_public_api.py` fails for a public method of an exported class
  annotated `-> Callable[[], …]`, alone or in a union (invariant 285), and for a condition whose
  shape breaks the rule of [writing tests](../guides/writing-tests.md#which-shape-a-condition-has)
  (invariant 291): `test_every_condition_has_the_shape_of_its_kind` finds every public method,
  property and attribute of an exported class annotated as a `Condition` or a `LineCondition`,
  on the class that defines it, and checks that a `next_*` one and an action in `ACTIONS` are
  methods, that a state condition is a property with no parameter and a method with one, and
  that its name takes `is_` unless `HAPPENED` lists it; `NAMED_FOR_IT` holds `console.line` and
  `HttpExchange.response`, which are named for what they return. A new condition outside those
  shapes fails it until it is renamed or listed, and an entry that names no condition fails it
  too.
  Calling it calls `find`; `repr` is `description`, which `board.wait_until` takes because the
  object has no `__qualname__`; `__bool__` raises
  `TypeError("call the <service> condition with (), or pass it to board.wait_until")`, `service`
  being `MQTT`, `HTTP`, `DHCP`, `TLS`, `NTP`, `DNS`, `DHCP station` for `is_leased` and the
  station's `next_lease()`, or `TCP` for `accepting`.
- How a "next …" condition scans: MQTT's `next_message`, `next_delivery` and `next_subscribe`,
  HTTP's `next_request`, NTP's `next_exchange` and DNS's `next_question` take as their `find` a
  `FirstFrom` of `esp32tl/interfaces/condition.py`
  ([described conditions](pytest-plugin-technical.md#described-conditions)) over their history,
  `messages`, `deliveries`, `subscribe_requests`, `requests`, `exchanges` or `questions`. A poll
  reads, by index and without copying the list, only the records appended since the last poll
  ended; a record it refused is not read again, since the histories are only appended to. Once one
  matches, every later poll returns that record and reads nothing else. So a wait of N polls over
  N records reads N of them, not N × (N + 1) / 2, which `tests/test_net_mqtt.py`,
  `tests/test_net_http.py` and `tests/test_net_services.py` count on 10 000.
  `next_connection`, `next_connection_attempt` and `next_lease` read one entry at their index, and
  the TLS conditions compare a count, so each of their polls costs the same at any length.
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
  that index, named `the next DHCP lease`. `find_lease()` reads the first lease.
- `http.py`: `HttpServer(now, log)`, a `StreamService` on TCP 80 of every network (RFC 9110,
  9112). `serve(path, body, *, status, content_type, headers)` stores an `HttpResponse` per path, after
  checking that the path starts with `/` and holds no `?`, since a request is looked up by its
  path without the query, that the status is in `REASONS` and that no header holds CR, LF
  or, in its name, `:`. Each connection is an `_Exchange` that reads one request: the head up to
  `\r\n\r\n` (`MAX_HEAD`, 64 KiB, else 431), parsed by `_head` (else 400), `Transfer-Encoding`
  refused with 501, the distinct `Content-Length` values (more than one, or not all ASCII digits, 400; over
  `MAX_BODY`, 1 MiB, 413, compared by the number of digits first, so that no value is too long
  for `int`), then the body. `_path` gives the path by the target's form (RFC 9112 3.2): an
  origin-form target, which starts with `/`, up to `?`; an absolute-form one, a scheme of letters,
  digits, `+`, `-` and `.` then `://`, what follows its authority up to `?`, or `/`; any other
  target as it is. `_answer` appends the `HttpRequest` to `requests`, logs
  `http: <method> <target> → <status>`, and returns the served `HttpResponse` or `404` with
  `not found\n`. A refusal logs `http: refused a request: <status> <reason>` and is not appended.
  `_respond` writes the status line, `Content-Type` and `Content-Length` (not for a status in
  `NO_CONTENT`, 204 and 304, whose response ends with its head, RFC 9112 6.3), `Connection: close`,
  the test's headers and the body (not for `HEAD`; `serve` refuses one for 204 and 304), then
  closes; later data on the connection is
  ignored. `next_request(path, method=...)` captures the length of `requests` at construction and
  returns a `Condition` whose `FirstFrom` scans from that index for an exact `path` and `method`
  match, `_wants(path, method)`, each poll from where the last one ended, named
  `the next HTTP request`, then ` for '<path>'` and ` with method '<method>'` for the filters
  given. `find_request` scans from index zero with the same filters.
  Both refuse a `path` holding `?` with `ValueError`, through `_refuse_query`: `_path` strips the
  query of origin-form and absolute-form targets, so such a filter would match nothing. A target
  of no form `_path` knows, as `foo?bar`, keeps its `?` in `path`; only `requests` reaches it.
- `ntp.py`: accepts NTP v3/v4 client requests; returns a v4 stratum-1 server response. The
  origin timestamp copies the client's transmit field. Reference, receive and transmit stamps
  use world UTC plus emulated time, including fractions and the 32-bit NTP era wrap.
  `go_silent` / `resume` toggle whether new datagrams receive replies, without changing the
  UDP binding, the clock or already queued packets. Every client request, silent or not, is
  appended to `exchanges` as an `NtpExchange(at, answered)` before the silence is checked, `at`
  being `Duration(now())` and `answered` `not _silent`; a datagram under 48 bytes, not in client
  mode or of a version other than 3 or 4 returns `None` before anything is recorded. `requests`
  is a property that counts the answered exchanges. Each state transition is logged once, a
  request while silent is not logged, and resuming never replays unanswered datagrams.
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
  `LinkError` `uart.modem.receive raised <Type>: <message>`; the link fails with its traceback, ends
  the run, and the test's wait raises `RunEnded` naming it. A successfully selected code appends
  a `ConnectionAttempt` to `connection_attempts` before sending CONNACK. This history is separate
  from accepted `clients`; `next_connection_attempt()` captures a future cursor like
  `next_connection()`. Rejections log `mqtt: '<client-id>' rejected: <CODE_NAME> (<value>)`, send
  CONNACK with Session Present zero and close before allocating an anonymous ID, installing a Will,
  setting keepalive or taking over an existing client. The parser stops processing buffered packets
  on closure. Only accepted connections install session state and enter `clients`; replacing the
  policy affects subsequent CONNECTs only. `session_present` is worked out before the attempt is
  appended: CleanSession=0 and a stored `_State` that is not `clean`. A clean one exists only while
  its connection does, and the takeover ends it. After the takeover, CleanSession=1 replaces any
  `_State`, and CleanSession=0 makes one if there is none; `_connect` sets `state.connection`,
  writes CONNACK and, with Session Present, calls `_resume`: every entry of `state.sent`, in
  insertion order with DUP set, then `_drain`, which pops `state.queue` and sends each entry through
  `_send` until the head would go at QoS 1 with 65535 IDs in use, so an entry going at QoS 0 does
  not wait for an ID, and skips one that no subscription matches any more. Each PUBACK calls
  `_drain` again, so a queue longer than the IDs goes out as they are freed, on a connection that
  stays open. `state.queue` holds `(Message, retained)` pairs: `retained` is the RETAIN the entry
  goes with, `True` only for a retained message a SUBSCRIBE matched, and `session` reports the
  `Message`s alone. `_route` calls `Broker._offer` for each `_State`: a connected one goes to
  `deliver`, a stored one queues a message by the message's QoS or logs dropping it, and a clean one
  closed while routing takes nothing. `deliver` works out the QoS in `_qos` and queues the message,
  never closing, when it is QoS 1 behind a queue that is waiting or goes at QoS 1 with 65535 IDs in
  use; otherwise `_send` allocates from `state.packet_id`, skipping `state.unacked`, and `_publish`
  appends a `Delivery` and fills `state.sent` before it writes. SUBSCRIBE sends its retained
  messages through `deliver` with `retained=True`, so one that waits keeps RETAIN 1, and `deliver`
  does not queue one that is already in the queue as the same object with `retained` set, which
  `state.queued_retained` holds by `id` while the queue holds the object; a retained message
  replaced since is another object. SUBSCRIBE iterates over a copy of `broker.retained`, because a
  write that fails, as a TLS one can, closes the session and its Will can change the dict. A
  CleanSession=1 `_State` holds its queue for its one connection. `_wait` logs
  `mqtt: <id> has no free packet identifier: waiting for a PUBACK, <n> queued` when a connection
  starts to wait, and `_Session.waiting` keeps it from logging again until `_drain` empties the
  queue. If the transport closes during CONNACK on resume, the queued messages wait for the
  next connection. Only packet ID exhaustion triggers the wait diagnostic in `_drain`.
  PUBACK discards the ID, pops `state.sent` and stamps that `Delivery`'s `acknowledged_at`; a connection already closed
  ignores its data, PUBACK included, in `on_data`. `_end` clears `state.connection` and removes a
  `clean` state from `_states`. `session` copies a `_State`'s collections into a frozen
  `SessionState`, whose `Delivery` records stay the broker's;
  `forget_session` refuses a connected one. Network reset retains both histories and the policy.
  Will is published once with the time
  of disconnect, not the time of CONNECT. A replacement client ID closes the old connection
  (MQTT-3.1.4-2) and logs `mqtt: <id> (connection <new>) took over connection <old>`. When the
  old connection's `_State` is not `clean` and the new CONNECT has CleanSession=0, `_connect`
  sets the old session's `will` to `None` before closing it, and the line ends `, its Will
  discarded`: mosquitto's rule in `connect__on_authorised`, which sends the old Will only when
  either side starts clean. MQTT 3.1.1 (MQTT-3.1.2-8) would publish it at every close but a
  DISCONNECT; a device back inside its keepalive has not gone, and its backend should not hear it
  had. A keepalive deadline that has come by then, though `poll` has not yet expired it (a CONNECT
  in the turn of the deadline comes first), keeps the Will: it goes at the takeover, and the
  session is resumed all the same.
  Every way a `_Session` ends goes through `_end(by, will_sent=...)`, whose first call closes it:
  a DISCONNECT calls it directly with `"disconnect"`, after the log line `mqtt: <id> sent
  DISCONNECT` and with the Will cleared; the rest call `_lose(by)`, which takes the Will, keeps it
  only for an accepted connection not yet closed, calls `_end` and then routes the Will at the
  board's time. `Connection.on_close`, which the TCP flow (`_notify`) and the TLS layer call, is
  `_lose("closed")`; `Broker.poll` passes `"keepalive"`, `Broker.disconnect` `"test"`, `_connect`
  `"takeover"` for the old connection, and `on_data` `"malformed"` for a refused packet and for a
  remaining length over 1 MiB or longer than four bytes; a rejected CONNECT and a policy that
  raises close with `_end()` and no cause. `_end` appends a frozen `ConnectionEnd`
  (board time, client ID, connection ID, `by`, `will_sent`) to `Broker.connection_ends` only when
  `connected` is set, so a stream refused before or at its CONNECT records none. It appends once
  the session is cleaned up and before it closes the stream; `_lose` routes the Will after that,
  so the end is in `connection_ends` before the Will is in `messages`. The stream's own close
  that follows calls `on_close` on a closed session, which records nothing.
  `ended(connection_id)` scans `connection_ends`; `next_end(client_id)` is a `FirstFrom` over it
  from its length at the call, filtered by client ID.
  QoS 2 publication is unsupported. Credentials are observed by default;
  authentication is performed only by an installed test policy, with no external auth service.
  SUBSCRIBE and UNSUBSCRIBE parse every filter of the packet into a `RequestedFilter` before they
  touch the session, so a packet that `_handle` refuses part-way subscribes and unsubscribes
  nothing; `_Session._record` then appends a frozen `SubscribeRequest` (board time, client ID,
  connection ID, packet ID, `kind` and the filters in packet order) to
  `Broker.subscribe_requests`, before the subscriptions change and SUBACK or UNSUBACK is written.
  `granted_qos` is `min(requested_qos, 1)`, the byte SUBACK carries. The list is the broker's,
  like `connection_attempts`, not the `_State`'s, so neither UNSUBSCRIBE, `_end` of a clean
  session, a CleanSession=1 CONNECT nor `forget_session` removes an entry.
  `next_message`, `next_connection` and `next_connection_attempt` capture the length of
  `messages`, `clients` and `connection_attempts` at construction, `next_subscribe` that of
  `subscribe_requests`, and `next_delivery` captures `deliveries`, filtering as `next_message`
  does. `next_subscribe(filter)` skips UNSUBSCRIBE entries and, with a filter, keeps the first
  entry whose `topic_filters` hold that string exactly. Matching uses append order,
  not timestamps, and polling does not change the captured offset; each poll reads only the
  records since the last one, through `FirstFrom`, its filter `_wants(topic, payload)` or, for
  messages, `_wants_message(topic, payload, will)`. `find_message` scans
  from index zero with the same topic/payload filters; `find_connection` reads the first CONNECT.
  `mark()` returns a `Mark` holding the broker and `len(messages)` at the call;
  `find_messages(topic, payload, since=, client=, will=)` walks `messages` from that index (zero for
  `None`) through `_messages`, keeps `message.client == client` when a client is given, and
  returns a new list. `_messages` reads `messages` by index from that start and keeps what
  `_wants_message` keeps: `_wants`, the topic filter and the payload `_first` filters deliveries
  by, and `message.will is will` when `will` is not `None`. `find_message` takes its first record
  from `_messages` and `next_message` filters with `_wants_message`, so the three filter Wills
  alike, and `_will_check` refuses any other `will` than `True`, `False` or `None` with `TypeError`
  before anything is read. `_Session._connect` builds the session's Will as a `Message` with
  `will=True`; `on_close` routes a copy restamped with the time of the loss, so the flag reaches
  `messages` and `retained`, and nothing else sets it. A `since` that is not
  a `Mark` raises `TypeError` before anything is read, and a `Mark` made by another broker raises
  `ValueError` rather than reading an index into a history it was not taken from. The index is a
  valid boundary because `messages` is append-only and never cleared, network reset included.
  `publish` stamps its messages with `TEST_CLIENT`, `"(test)"`, a name the broker does not reserve:
  a client that connects with client ID `(test)` is indistinguishable from the test by `client`
  alone (its messages have a `connection_id`). These are immediate queries returning records, a
  list of them, or `None`. Every MQTT condition, the live subscription query included, is a
  `Condition` with service `MQTT`. Subscriptions continue to read active sessions, without
  latching matches.
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
  the first receipt. Duplicates and policy changes leave that deadline unchanged. `Broker.poll`,
  which `Internet.arrive` calls before the hold, expires keepalives first, then releases due
  handles in connection/receipt order. A late poll stamps emission at its actual current time,
  never backdates it to the deadline.
  `Publication.release()` writes PUBACK to the stream, marks it `"emitted"` and records
  `puback_at`; a synchronous TLS write failure closes the session and leaves it `"cancelled"`
  instead. Releasing a finished handle returns `False` without writing. `_end` cancels and
  clears pending handles before closing the stream, so closure through TLS cannot resurrect
  them. `Broker.disconnect(client_id)` uses the same loss/Will path as transport loss and
  never resets the network. An emitted handle survives closure as an emission record, not
  evidence of device processing. Histories, retained messages and stored sessions survive
  reconnect; incoming ACK state does not.
- `tls.py`: an RSA-2048 CA and server certificate, SHA-256 signatures, DNS/IP subject
  alternative names, CA key usage and server extended key usage. Optional `server_not_before`
  and `server_not_after` apply only to the leaf certificate; the CA retains its fixed validity.
  Inputs must be timezone-aware, are normalized to UTC, and must form a forward interval;
  validation happens before the CA takes a number. `CertificateAuthority.number` is the first
  number of a module counter that is not a key of `_held`, a `WeakValueDictionary` of every CA of
  the process by number; `number_authorities_from_zero()` (the plugin's `pytest_runtest_setup`)
  restarts the counter, after `gc.collect()` when `_held` is not empty, since a CA in a reference
  cycle (through its `Tls` and `Internet`) is gone only after a collection. Over the whole suite
  (1860 tests) that is 70 collections, 2.6 s in all on an M2 Pro. `_rsa_key(label)`, cached per process, takes its two 1024-bit primes,
  top two bits set, from `_Stream("rsa " + label)`, SHAKE-256 over `esp32tl tls: <label> <block>`:
  candidates sharing a factor with the primes below 4000, or with `p - 1` divisible by 65537, are
  skipped, then Miller-Rabin with base 2 and four bases from the stream; `private_key()`
  validates the result. The labels are `authority <number> ca` and `authority <number> server`;
  a key costs about 0.4 s the first time on an M2 Pro. `chain` holds the server's and the CA's DER, sent in that
  order.
  The server is TLS 1.2 (RFC 5246) over the record layer of its own: `_Session` reassembles
  records and handshake messages split anywhere, answers a ClientHello with ServerHello,
  Certificate, ServerKeyExchange and ServerHelloDone in one write, then takes ClientKeyExchange,
  ChangeCipherSpec and Finished and sends its own. Each connection's `_Stream` is
  `authority <number> connection <n>`, `n` counting the listener's accepts: 32 bytes of server
  random, then the X25519 private key (32 bytes) or a NIST curve's scalar (64 bytes reduced into
  `[1, order - 1]`). `SUITES`, `GROUPS` and `SIGNATURES` list what is served; the first of the
  client's that is listed wins. Without `supported_groups` the group is secp256r1; without
  `signature_algorithms`, SHA-1. The master secret is the extended one (RFC 7627) when the
  client offers it; PRF and keys follow the suite's hash. AES-GCM's explicit nonce is the
  sequence number (RFC 5288); ChaCha20-Poly1305 XORs it into the IV (RFC 7905). The ServerHello
  echoes `renegotiation_info` (empty, on the extension or the SCSV), `extended_master_secret`,
  `ec_point_formats` (uncompressed) and `max_fragment_length`, which then bounds every record the
  server writes. Its session ID is empty, so no session resumes, and it sends no ticket. Any
  error is a `_Fatal(alert, message)`: the server sends the fatal alert, encrypted once its
  ChangeCipherSpec is out, logs `tls: handshake failed: <message>` before completion, and closes.
  A fatal alert from the client logs `tls: handshake failed: the client sent the alert <name>`
  before completion and closes, after completion it closes without a log line; a `close_notify`
  closes; other warnings are ignored. Plaintext reaches the inner service only
  after handshake completion. TLS errors close the connection and notify the inner service once. `Internet.listen(..., tls=ca)` returns the installed `Tls` object;
  plain listeners return `None`. Its cumulative `handshakes_started` counter increments on the
  first nonempty `on_data` per session, before TLS parsing. Empty input, TCP accept alone,
  subsequent fragments and data after closure do not increment it. Invalid first input does.
  `handshakes_completed` increments once when the client's Finished verifies,
  before handing the plaintext stream to the inner service. Session closure and network reset
  leave both counters intact. Callable `handshake_started` / `handshake_completed` properties
  observe whether the corresponding live count is positive; they do not capture a future cursor.
  `next_handshake_started()` / `next_handshake_completed()` instead capture the corresponding
  count when they are made, in the closure that `Tls._handshake` gives the `Condition`, and
  compare the live count strictly against it. Event
  processing order distinguishes equal-time events; no timestamp filtering or match consumption
  occurs. Completion after construction includes handshakes already in progress. These counters
  describe events across the listener, not the lifecycle of a selected connection.
  Their representations name the desired event, and `__bool__` rejects unevaluated truth tests.

These are a test network, not a general Internet stack: its path loses nothing the test does not
drop, and its TCP sends again only on the RTO. TCP congestion control, fast retransmit, SACK, packet
reassembly, MQTT QoS 2 and bridging UDP to host services
are outside this implementation, and so are IPv6, VLANs, DHCP for more than one device, lease
expiry and HTTP keep-alive or chunked bodies. Protocol references:
[PPP](https://www.rfc-editor.org/rfc/rfc1661),
[HDLC framing](https://www.rfc-editor.org/rfc/rfc1662),
[MQTT 3.1.1](https://docs.oasis-open.org/mqtt/mqtt/v3.1.1/os/mqtt-v3.1.1-os.html),
[DHCP](https://www.rfc-editor.org/rfc/rfc2131), [HTTP/1.1](https://www.rfc-editor.org/rfc/rfc9112).

## Retransmission

The link to the device loses and reorders nothing, but the device's stack drops segments that reach
it. In ESP-IDF v5.3.6 (lwIP `392707e`), `recv_tcp` posts each received pbuf to the netconn's
`recvmbox`, `CONFIG_LWIP_TCP_RECVMBOX_SIZE` (6) deep, and returns `ERR_MEM` when it is full
(`components/lwip/lwip/src/api/api_msg.c`). `tcp_input` then keeps that pbuf as the pcb's
`refused_data` and, while it is there, drops every further segment that carries data, sending no
ACK unless the window is zero (`components/lwip/lwip/src/core/tcp_in.c`). Before TCP,
`pppos_input_tcpip` drops what `pbuf_alloc(PBUF_POOL)` has no buffer for, and `tcpip_inpkt` what
the tcpip mailbox (`CONFIG_LWIP_TCPIP_RECVMBOX_SIZE`, 32) has no room for
(`components/lwip/lwip/src/netif/ppp/pppos.c`). None of these draws a duplicate ACK, so `Network`
keeps what it sent until the device acknowledges it, and sends it again when its retransmission
timeout (RTO) runs out, as RFC 6298 says. Fast retransmit, congestion control and SACK are outside
it: the RTO is what recovers a segment lwIP dropped.

| Constant in `ip.py` | Value | What it is |
|---|---|---|
| `RTO_NS` | 1 s | The RTO before the first RTT sample (RFC 6298, 2.1) |
| `RTO_MIN_NS` | 1 s | The least RTO (RFC 6298, 2.4). Linux's 200 ms (`TCP_RTO_MIN`) is below lwIP's delayed ACK, which waits for its fast timer (`TCP_TMR_INTERVAL`, 250 ms), so it would send again segments the device only acknowledged late |
| `RTO_AFTER_SYN_NS` | 3 s | The RTO data starts with when the SYN or SYN-ACK went again and the handshake gave no sample (RFC 6298, 5.7) |
| `RTO_MAX_NS` | 120 s | The most a doubling RTO reaches (Linux's `TCP_RTO_MAX`; RFC 6298, 2.5, asks for at least 60 s) |
| `RETRIES` | 15 | Retransmissions unacknowledged before the connection times out (Linux's `tcp_retries2`) |

`_Flow.unacked` holds what went and the device has not acknowledged, from `snd_una`, the FIN
apart: `_flush` appends each piece as it sends it. `_sending`, before each new piece and before the
FIN, times it unless a segment is timed already (`timing`, the sequence its ACK reaches and when it
went) and starts the timer, `rto_at = now + rto`, unless it runs (RFC 6298, 5.1). The SYN-ACK of a
device flow and the first SYN of a client flow are timed too; the SYN-ACK sent again (`SYN again,
answered again`) and a SYN sent again leave nothing timed, as their answer could be to either
(Karn's algorithm).

An ACK above `snd_una`, in `_tcp`, goes through `_acked`: `_sample` takes the RTT when the ACK
covers `timing`, the ACK's arrival less the time the segment went, then what the ACK covers leaves
`unacked`, `retries` goes back to 0, and the timer restarts from the ACK, or stops (`rto_at =
None`) once nothing is left to acknowledge (RFC 6298, 5.2 and 5.3). The client flow's SYN-ACK
takes its sample in the handshake branch. `_sample` follows RFC 6298, 2.2 and 2.3, in integer
nanoseconds: the first sample `R` sets SRTT to `R` and RTTVAR to `R / 2`; each later one sets RTTVAR
to `(3 RTTVAR + |SRTT − R|) / 4`, then SRTT to `(7 SRTT + R) / 8`; the RTO is SRTT + 4 RTTVAR, at
least `RTO_MIN_NS` and at most `RTO_MAX_NS`. A sample sets the RTO afresh, which ends a backoff.
The path's `rtt` and rates so reach the RTO once they exceed the 1 s floor: a handshake answered
3 s after the SYN-ACK gives an RTO of 9 s.

`_rto_due(flow)` is `rto_at`, or `None` while the timer is stopped, while the flow is not
established, not `_current` or `orphaned`, since no stack that holds it would acknowledge, and
while it is `_stalled`: on a `"drop"` port (`_held`), or not orphaned while `_carries` is false.
`Network.quiet_until()` is the earliest of these and of the SYNs' `_syn_due`, and
`Network.poll()` calls `_retransmit` on each flow whose `_rto_due` has come, before the SYNs.
`_retransmit` drops the flow once `retries` is already `RETRIES`: a device flow as
`"unacknowledged"`, logged `net: TCP <device>:<port> → <ours>:<port> timed out: 15 retransmissions
unacknowledged`, its service told by `on_close`, which the broker records as `"closed"`; a client
flow as `"lost"` with the reason `UNACKNOWLEDGED`, `its 15 retransmissions went unacknowledged`.
Otherwise it counts the retransmission, doubles `rto` up to `RTO_MAX_NS` (RFC 6298, 5.5), leaves
nothing timed, and starts a recovery, as slow start does after an RTO (RFC 5681, 3.1): `cwnd`
becomes one segment (`_segment_size`, the MSS or the MTU less 40, whichever is less), `recover` the
end of what `unacked` holds and `recover_fin` whether the FIN went, and `rtx_nxt` goes back to
`snd_una`. `_resend` then sends again from `rtx_nxt`, through `_segment(..., seq=)`, which moves no
`snd_nxt`, up to `recover`, while what went again and is unacknowledged stays within `cwnd` and the
device's window, or 1 byte when that window is zero, which probes it. The FIN goes again once every
byte before it has gone again in the recovery, so behind a long `unacked` it waits for the ACKs.
Each ACK that moves `snd_una` during the recovery adds a segment to `cwnd`, moves `rtx_nxt` up to
the ACK and calls `_resend` again. One ACK so lets out what it acknowledged and a segment more,
and what was unacknowledged comes back doubling per round trip: what lwIP dropped behind a hole
comes back in a few round trips, not one RTO at a time. When the RTO was spurious and the ACKs are
only late, as over a slow path, what was unacknowledged at the RTO still goes again, but paced by
the device's ACKs rather than at once, and an RTO that runs out again behind those duplicates sends
one segment, not the window, so the queue on the path does not grow in a cascade. There is no
F-RTO (RFC 5682), which Linux uses to tell a spurious RTO apart. The recovery ends (`cwnd = None`) once the device acknowledges `recover` and,
with `recover_fin`, the FIN. What the service writes in the meantime goes in `_flush` as the window
allows, as before: `cwnd` limits only what goes again.

Each `_resend` that sends anything logs one line,
`net: TCP <ours>:<port> → <device>:<port>: sent again <n> bytes from sequence <seq>, the <nth>
time, after an RTO of <rto>` at the RTO, `<rto>` being the RTO that ran out, and `…, after the
device's ACK of sequence <ack>` for one an ACK let go, and `…, the <nth> probe of a zero window,
after an RTO of <rto>` at an RTO that found the device's window zero and sent its 1 byte, counted in
`probes`, apart from `retries`, which the device's answer sets back to 0, and from 0 again once an
ACK moves `snd_una`; `<n> bytes and its FIN` or `its FIN` when the
FIN went too, `<seq>` being where it started, on the wire. `_retransmit` then starts the timer with
the doubled RTO. `retries`, and the 15, count RTOs only: what an ACK lets go counts toward none,
and an ACK from the device that moves nothing while its window is zero, as its answer to the RTO's
byte, sets `retries` back to 0, as Linux's persist state does, so a device that is alive but reads
nothing keeps its connection. From an RTO of 1 s, the retransmissions go 1, 3, 7, 15, 31, 63, 127, 247 s and on
every 120 s after the first transmission, and the connection times out at 1207 s. What goes again
was handed to a client's `on_sent`, and recorded in `events`, once, when it first went: by the
service's `_Traced` or by `TcpConnection.on_sent`, never by `_downlink`, and is neither again.

When the handshake gave no sample because its SYN went again (a client flow's `syns` above 1) or
its SYN-ACK did (`syn_ack_again`, set at `SYN again, answered again`), `_established` raises the
RTO to `RTO_AFTER_SYN_NS`, 3 s, as RFC 6298, 5.7, says and Linux's `TCP_TIMEOUT_FALLBACK` does:
with an RTT above 1 s, an RTO of 1 s would send the first data again before its ACK could come.

On `firmware/mqtt_session` with `set_path(rtt=ms(600))`, in
`tests/test_mqtt_session.py::test_a_qos1_backlog_follows_the_rtt`, lwIP acknowledged a PUBACK more
than 1 s after it went, while Nagle held its next report: the network sent it again once at the RTO,
and the PUBACK behind it after the ACK of the first, both spurious, as a server without F-RTO's
would be. The rest of that file's runs, and the bursts of the [decision](../decisions/harness.md#the-networks-tcp-sends-again-what-the-device-does-not-acknowledge-on-its-rto),
sent nothing again, measured with a probe on `_resend` that no test keeps.

A hold stops the timer: `_rto_due` is `None` while the flow is `_stalled`. `_restart(flow)` starts
it again from the instant, with the RTO it had, and leaves nothing timed, since the hold's time is
no RTT. `Network.resume` calls it for every flow it keeps that is no longer stalled, before its
`_flush`, and `_release` for each flow of a port that was `"drop"`. The probe of `_probe` is an ACK
from `snd_nxt - 1` through `_segment(..., seq=)`: it counts no retransmission and changes no RTO.
An orphaned flow keeps no timer: what its service writes still goes, and a RST of the new stack
ends it, as before.

## Connections the test opens

`esp32tl/net/client.py` holds `TcpConnection`, `ConnectionEnded` (an `AssertionError`) and
`NotReachable` (a `RuntimeError`); `Network.connect` in `ip.py` is the active open, and
`Internet.connect` in `__init__.py` joins them.

`Internet.connect(port, *, address=None)` names its call `network.connect(<port!r>)`, with
`, address=<address!r>` when given, in every error. It checks `port` (an `int`, not a `bool`, from 1
to 65535, else `ValueError`) and `address` (`ipaddress.IPv4Address`, else `ValueError`), then raises
`NotReachable` while `active` is false (`the access point is off the air` or `the modem has not
dialled`), behind PPP while `device_address` is `None` (with ` since the device reset` when IPCP is
open from before the reset), and behind Ethernet when no address is given and
`Ethernet.device_address` is `None` (with ` since the device last reset or gave its address up`
once `Ethernet.leased`, set at the link's first ACK, is true; a fresh link starts without it), or else when `Ethernet.device_mac` is `None`. The address is
`address`, else `device_address`: behind PPP `Ppp.peer` while IPCP is open and
`Ppp.ipcp_opened`, which counts IPCP's openings on the link, is not `_stale`, the count at the
device's last reset; behind Ethernet `Ethernet.device_address`. `Ethernet._dhcp` sets that around
each DHCP message of the device's: to the `Dhcp.device` address when `Dhcp.on_message` appended a
lease, which it does for an ACK only, and otherwise to `None` for any message
`dhcp.message_type` reads but an INFORM, which a client with an address of its own sends (so a
DISCOVER, a REQUEST not acknowledged, a NAK's or one to another server, a DECLINE, a RELEASE). Each
`Ethernet` starts with `None`, and `Internet.stop` sets `_stale` to `None` with the fresh link, so
a new link has no address from before it, while `dhcp.leases` keeps every lease. `connect`
numbers the connection per device port, names it `device:<port>/<n>`, makes the `TcpConnection`
with the station's leased address, or otherwise `LOCAL` (`10.64.0.1`), `now` and a `record` that appends `{t_ns, stream, direction, data}` to
`events` as `_Traced` does for the device's connections, and calls `Network.connect`; only once
that has returned does it count the connection in `_opened` and append it to `connections`, so a
`RuntimeError` for want of a port leaves neither.

`Network.connect(device, port, ours, client)` takes `_port`, which starts at `EPHEMERAL` (49152)
and only goes up: `Network.reset` leaves it, and past 65535 it raises `RuntimeError`. It makes a
`_Flow` keyed `(device, port, ours, local)` like the device's flows, with `snd_nxt = snd_una = ISN`,
`rcv_nxt = 0`, `peer_window = 0`, `mss = 536`, and `client` and `connection` both the
`TcpConnection`, which is the `TcpClient` protocol of `esp32tl/net/ip.py` (`on_syn`,
`on_established`, `on_sent`, `on_data`, `on_close`, `on_end`), logs `net: TCP <ours>:<local> →
<device>:<port> connecting`, sends a SYN with the MSS option (`MSS`, 1400) through `_syn`, and
returns the local port with the flow's `write` and `close`, which `TcpConnection._opened` keeps.
Both go through `_write` and `_close`, which, as for the device's
connections, buffer in `pending` and `fin_pending` and leave `_flush` to send: it sends nothing
until `established`, then pieces no larger than the device's MSS, its window less what is in
flight, and the MTU less 40 bytes, and for a client flow hands each piece to `on_sent` as it
goes. A zero window leaves `pending` as it is until an ACK from the
device raises `peer_window`; nothing probes it, but the restore of the path after a black hole
([The black hole](#the-black-hole)) and, while bytes the device has not acknowledged are out, the
RTO's 1 byte ([Retransmission](#retransmission)).

`_syn` sets `snd_nxt` back to `ISN`, so every SYN is the same segment, counts it in the flow's
`syns` and keeps its time, `now()`, in `syn_at`, sends it and calls `on_syn`: the
`TcpConnection` keeps the times of them all. `Network` takes `now` from `Internet.now`, the
segment's or the held turn's time.

The SYN has a timer of its own, beside the RTO of [Retransmission](#retransmission), because the
device's stack, not the link, drops it before there is a connection to time: lwIP's `ip4_input` drops a segment to an address its netif has not taken yet, and ESP-IDF's
DHCP client takes a leased address only after its ARP check, about 1 s after the ACK. While a
client flow is not `established`, `_syn_due(flow)` is `syn_at` plus `SYN_RTO_NS` (1 s, RFC 6298,
2.1) shifted left by `syns - 1`: 1, 2, 4, 8, 16, 32 and, after the seventh SYN, 64 s.
`Network.quiet_until()` is the earliest `_syn_due` of the flows `_connecting` returns, `None` when
there is none, and `Internet.quiet_until` takes it while the link is active. `Network.poll()`,
which `Internet.arrive` and `Internet.poll` call after `_arrive`, so that a SYN-ACK arriving at
the instant of a `_syn_due` is taken first, sends the SYN again on each flow whose `_syn_due` has
come, logging `net: TCP <ours>:<local> → <device>:<port> SYN again, <n> of 7`, until `syns`
exceeds `SYN_RETRIES` (6, Linux's `tcp_syn_retries`), and then drops the flow as `"timed out"`,
127 s after the first SYN. The device's answer to any of them is taken as the answer to the
first, as all are the same segment. A flow that is established, refused or lost leaves
`_connecting`, and with it the timer. With a path RTT over 1 s, the SYN goes again before the
device's SYN-ACK arrives, as a real client's does; a second SYN-ACK on an established flow draws
nothing and changes nothing, as
`tests/test_net_client.py::test_a_syn_ack_slower_than_the_rto_meets_the_syn_sent_again_once`
checks with an RTT of 1.5 s.

`Internet.accepting(port, *, address=None)` makes an `_Accepting`, which opens its first connection
at once through `Internet._open`, the body of `connect`, its errors naming
`network.accepting(<port!r>)`, and appends it to `_accepting`. `_Accepting.due()` is the last
connection's `ended_at` plus `ACCEPT_RETRY_NS` (1 s) while that connection is `refused`, fewer than
`ACCEPT_ATTEMPTS` (30) were opened and none raised, else `None`. `Internet._retry()`, called in
`arrive` and `poll` after `Network.poll`, opens the next connection for each one due and lets go of
one that is done: its last connection not `connecting` and no more due, or a `NotReachable` kept
from `_open`. `Internet.quiet_until` takes every `due()`, with the link up or down, so that a retry
due while the link is down raises then, kept for the condition. The condition's `find` returns the
last connection once it is established and, the first time, still open: one the device already
reset, or closed its end of, raises `ConnectionEnded` instead; `returned` then keeps it returning
that connection whatever becomes of it. Otherwise `find` raises the kept `NotReachable`, raises
`ConnectionEnded` when all 30 were refused, and otherwise reads the last connection's
`established`, which raises for a lost or timed-out one. Its description is `TCP <address>:<port>
accepting a connection`, and its `waiting` is the refusals so far, `the device refused <n>
connection(s), the last at <t>`, with `; the next goes at <t>` while one is due, or `; ` and the
last connection's `unanswered()` while its SYN is out.

In `_tcp`, a segment for a client flow that is not established is the device's answer to the SYN
only when it carries ACK with `ISN + 1` (RFC 9293, 3.10.7.3); anything else is ignored. With RST
it is `_drop(flow, "refused")`. With SYN, its options go through `_mss` (malformed ones drop the
segment), `rcv_nxt` becomes its sequence plus one, the window and the MSS, capped at 1400, are
taken, the flow is `established`, an ACK goes, `net: TCP <ours>:<local> → <device>:<port>
established` is logged and `on_established` is called; what else the segment carries is handled
by the established path below with its sequence one past the SYN. From there a client flow takes
the same path as the device's: ACKs move `snd_una` and `peer_window` and call `_flush`, data at
`rcv_nxt` is acknowledged and handed to `on_data`, data behind it is acknowledged again, a FIN calls
`_notify` (`on_close` once) and `_close`, so the test's end follows the device's, and the flow is
dropped as `closed` once both FINs are acknowledged. A client flow's data or FIN ahead of `rcv_nxt`
(less than 2³¹ ahead, modulo 2³²) logs `net: TCP <ours>:<local> → <device>:<port>: the device sent
sequence <seq>, <n> bytes ahead of <rcv_nxt>, which a link that loses nothing cannot produce` and
raises `OutOfOrder` with that line; the link's `calling` turns it into the link's error, and the run
ends. The device's own flows keep acknowledging such a segment again, as before.

`_drop(flow, kind, why=None)` takes `"closed"`, `"refused"`, `"reset"`, `"lost"` (`Network.reset`,
which no link calls and the unit tests use as a transport loss, and
`Network.forget_device(why=None, *, address=None)`, and `Network._retransmit` with `why`), for a
client flow only `"timed out"` (`Network.poll`), or, for a device flow only, `"abandoned"`
(`Network._close` on an orphaned flow, and `Network._unfinished`), `"unanswered"`
(`Network._unfinished`) and `"unacknowledged"` (`Network._retransmit`), and logs it
through `_ENDS`: `closed`, `refused by the device`, `reset by the device`, `link lost`, `closed by
its service with no device to answer`, `lost before the device answered its SYN-ACK`, `timed out:
no answer to 7 SYNs`, `timed out: 15 retransmissions unacknowledged`, or `lost: <why>` when `why`
is given. `_drop` removes the flow from `_flows` only while it is the one
there for its key, and `_write`, `_close` and `_flush` do nothing for a flow that is not
(`_current`): a service may call the `close` of a flow that has ended after the device opened
another on the same addresses and ports, and that must not end the new one. A client flow's line
names the test's end first, `net: TCP <ours>:<local> → <device>:<port> <end>`, and
`on_end(kind, why)` comes before `_notify`, so that `TcpConnection.on_close` sets `closed_at` for a
FIN only, never for a RST or a loss. The device's flows log after `_notify`, as before.

`Internet.device_reset(t, why)`, with `_at` set to `t`, calls `Network.forget_device(why)`, which
drops every client flow as `lost`, drops the device's flows that cannot finish (`_unfinished`,
below), and leaves the rest of the device's own, marked `orphaned`, then, behind PPP, sets `_stale`
to `Ppp.ipcp_opened`, and behind Ethernet sets `Ethernet.device_address` to `None`. The `esp32sim`
backend calls it on every network the test gave, by `_networks()`: from `_reset`, on each `reset`
message the link takes, with `the device reset`, and from `_switch(False)`, at `board.power_off()`,
with `the device lost its power`, before the models lose their power and the modem's network with it
([backend](esp32sim-backend-technical.md)). A power-on brings a `reset` message too, which finds
nothing left to lose. The device's own flows stay, because a network does not know of a reboot: they
end at a RST from the device's new stack, which comes in answer to what their service writes, or at
the service's end, as the broker's keepalive expiry, which drops an orphaned flow at once; the
link's end leaves them too. A SYN of the new stack, without ACK, from the same port to the same
service falls on the old flow: when that flow is orphaned and the SYN's options parse (`_mss`),
`_tcp` drops it as `"renewed"`, logged `reset: the device opened a new connection from its port`,
its service hearing `on_close` as at a RST, and takes the SYN as a new connection. That is what a
challenge ACK (RFC 9293, 3.10.7.4) would come to: the new stack answers it with a RST, which resets
the old connection, and sends its SYN again. A flow that is not orphaned takes only ACK, data and
FIN, and drops such a SYN without an answer, as before.

`TcpConnection` keeps `received` in a `bytearray`, `syns`, `established_at`, `closed_at` and
`ended_at` as `Duration`s from `now()` at the callback, which is the segment's own time, `_end`, the
kind, and `_why`, a loss's reason, which `_how` turns into `lost when <why>`, else takes `_HOW`'s
sentence for the kind (`timed out with no answer to its 7 SYNs` for `"timed out"`). `write` hands
the bytes on; `on_syn` appends to `syns`, `on_sent` records `in`, at the time the piece goes, and
`on_data` records `out`. `unanswered()` is `None` unless `state` is `"connecting"`. Otherwise it
splits `syns` into the ones sent again after, each of which got no answer in its RTO, and the last,
whose age is `now()` less its time: `the SYN at <t> has had no answer in <age> (no SYN-ACK, no RST)`
for one SYN, and for more `the SYN(s) at <t>, … and <t> got no answer (no SYN-ACK, no RST), and the
SYN at <t> has had none in <age>`; an age of 0, a SYN sent at that instant, reads `has just gone`.
Its conditions, the properties `established`, `closed`, `refused`, `reset` and `timed_out`, each
building a new one at every read, and `next_data(...)`, are `Condition(find, description, "TCP",
self.unanswered)`, so that `board.wait_until` puts that clause in a timeout's `NeverHappened` ([the
backend](esp32sim-backend-technical.md)); each `find` returns the time or bytes it is after, `None`
while it may still come, and raises `ConnectionEnded` once `_end` (or, for `closed` and `next_data`,
`closed_at`) rules it out, its message built from `_how`; `refused` and `timed_out` raise it once
`established_at` is set too. `next_data` validates its arguments at the call and captures `start`,
`len(received)` unless given; each poll searches the `bytearray` in place (`find(until, searched)`,
or its length) and copies only what it returns, so it consumes nothing, a repeated poll returns the
same bytes, and a long stream is not copied at every turn. `searched` starts at `start`; a poll that
does not find `until` moves it to `len(until) - 1` bytes before the end, where an `until` split
across two arrivals begins, and one that finds it to the match. So a poll searches only the bytes
since the last one, not all since `start`, as
`tests/test_net_client.py::test_a_next_data_poll_searches_only_the_bytes_since_the_last_one` counts
over 10 000 chunks.

## Lifecycle and observability

Modem dial resets and starts PPP. Supply, registration or power loss clears its
queued data and ends the link: `Internet.stop` and the PPP peer's `ended` callback, at a
Terminate-Request or a `Station` losing its lease, call `_link_ended`, which calls
`Network.lose_link()` and clears both directions of the path. The test's flows are lost, as
`link lost`; the device's stay, half-open, and their services hear nothing, since no FIN or RST
reaches a broker on the internet from a device that lost its power or its coverage. The broker
ends them at keepalive expiry, `mqtt: <id> keepalive expired`, and publishes the Will then, 1.5
times the keepalive after the client's last packet.

`_Session.on_data` in `esp32tl/net/mqtt.py` passes each chunk to the broker, which queues it with its
arrival time. A synchronous write callback can supply more bytes on any connection. The broker
processes incoming chunks, transport close notifications and timer polls in arrival order.
A DISCONNECT delivered before transport closure suppresses the Will. A poll checks the deadline
set by packets delivered before that poll. Closing the transport stops outgoing writes immediately.
From that point, `Broker._offer` uses the offline routing rules even while the close
notification is queued. A matching QoS 1 publication waits in the persistent session and
follows any unacknowledged retransmissions when the subscriber reconnects.
A packet takes the timestamp of the chunk containing its final byte. After the handler has
produced its records, the session appends a `ReceivedPacket` to
`Broker.received_packets`, including for a rejected CONNECT or a graceful DISCONNECT. A handler
error or incomplete frame adds nothing. An open session with keepalive enabled sets its deadline
from that same timestamp.

`Network.lose_link(orphan=True)` calls `forget_device`, which marks every device flow `orphaned`:
the stack on the device that held it is gone, by the power, by the coverage or because lwIP drops a
netif's connections when it loses its address. When the service closes an orphaned flow, as the
broker does at a keepalive expiry or a takeover, `_close` drops it at once, logged `net: TCP
<device>:<port> → <address>:<port> closed by its service with no device to answer`, and sends no
FIN: none would be answered, whatever link the device is on by then, and the window a FIN waits for
may be full of what went nowhere. Kept, the flow would wait for an answer that never comes.
`forget_device` drops at once, through `_unfinished`, the device flows whose service had closed them
already (`fin_pending`), as `abandoned`, and those still waiting for the ACK of their SYN-ACK, as
`unanswered`: no MQTT CONNECT has come on them, so no service deadline would end them. What a
service writes on an orphaned flow still goes, and a RST of the new stack in answer drops it as
`reset by the device`.

Behind a virtual station the device is the access point, and keeps its stack when the station
leaves: `_link_ended` calls `lose_link(orphan=False)` there, which loses the test's flows and drops
the device's still in their handshake, as `unanswered`, since their SYN-ACK is not sent again, and
drops those with bytes or a FIN the device has not acknowledged, as `"gapped"`, logged `lost with
what it had on its way when the link went`, since what was on the path or in the air is gone and
the retransmission timer is stopped while the link carries nothing (keeping such a flow for the RTO
after the station's return is postponed), drops those whose FIN the device acknowledged with no FIN back, as
`abandoned`, since they wait for that FIN with no timer to give up, and leaves the rest as they
were. `Network` takes `carries`, `Internet._carries`: the network active and, behind a station, a
lease. While it is false, `_flush` sends nothing on a flow that is not orphaned: what its service
writes, and its FIN behind it, wait in `pending`, since what went into a link that carries nothing
would go again only on an RTO the hold has stopped, and
`_station_changed` calls `Network.resume(lease.address)` when the station has a lease again, which
flushes every flow from that address, so the bytes and the FIN reach the device in order, and drops
as `lost` a flow from another, which the station no longer holds. Behind the access point,
`Ethernet` calls `gave_up(address)`, `Internet._gave_up`, when a DHCP message of the device's (a
DISCOVER, a REQUEST not acknowledged, a RELEASE or a DECLINE; not an INFORM, nor a type the server
does not take) takes away an address it had acknowledged: `forget_device("the device gave its
address up", address=address)` loses the test's flows to that address and orphans the device's, as
lwIP drops a netif's connections when its address goes.

On an orphaned flow `_write` sends what the device's window has room for and discards the rest,
counting it in `discarded`, since nothing will acknowledge what went: kept, it would pile up until
the service closes. `_drop` logs the count, `net: TCP <device>:<port> → <address>:<port> discarded
<n> bytes its service wrote with no device to answer`. `forget_device` discards and counts what was
already waiting for the window when it orphans a flow. `device_reset` and `_gave_up` also take out
of `_up` the gone stack's SYNs still on the way up, from any address or from the one given up
(`Internet._forget_syns`, which parses each with `ipv4` and keeps what does not parse, or is a
fragment): each would open a flow left in its handshake, which nothing ends. The rest of what that
stack sent before still arrives, as at a service on the internet: data on a connection it held, its
last PUBLISH among them. The link's end clears the path whole, and so does a reset behind a virtual
station, whose `Station.stop` ends the link.

`Internet.terminate(t)` calls `Ppp.terminate`, which sends an LCP Terminate-Request on an open link
and changes nothing else, and returns the output for
[`modem.drop_call(terminate=True)`](modem-technical.md#interfaces), which stops the network next.
`Internet.ppp` raises behind an access point. Network data mode guards `+++` with a second of idle
time on each side, using `quiet_until`/`poll`; a fourth plus or another byte cancels a pending
escape. AT-only operation retains its earlier immediate escape behavior.

Behind the access point, `Wifi.access_point()` creates the `Internet` with `link="ethernet"`,
calls `start(0)` and logs `wifi: access point '<ssid>' on the air, channel <n>`: the network is
active from power-on. `Wifi.access_point_off()` calls `stop(now)` at the board's current instant
and logs `wifi: access point off the air`; the connections the test opened end with
`net: TCP <address>:<port> → <device>:<port> link lost`, the device's stay open until their
service ends them, and queued replies are dropped. `Wifi.access_point_on()` calls `start(now)` and
logs
`wifi: access point back on the air`. Each switch also queues a `WifiAp` input for the runner. The
fresh `Ethernet` has no `device_mac` until the station sends again. The `Dhcp` and its `leases`
persist across the switches, so a test sees every lease of the run.

### Link changes

`Internet.link_changes` is a `Sequence[LinkChange]`. `LinkChange.cause` is a `LinkCause`, a `Literal`
of the sixteen causes; `Ppp`'s `ended` callback takes a `PppCause`, the `Literal` of the three
causes `Ppp` gives. `_link_change(up, cause)` appends to the list: it stamps
`Duration(self.now())`, the instant the callback or the held board runs at, and keeps
`_link_up`: it appends only when `up` differs from it, and never on an `"ethernet"` link. Behind a
modem `Ppp` calls `opened` after `ipcp_opened` counts an IPCP open, and `Internet` records `IPCP
open`. `Ppp` calls `ended(cause)` from `_reset_lcp` at the device's LCP Terminate-Request,
`device terminated`, and at its Configure-Request on an open LCP, `device renegotiated LCP`, and
from `_ipcp` at the device's IPCP Terminate-Request, `device terminated`, and at its
Configure-Request on an open IPCP, `device renegotiated IPCP`. `Internet.terminate(t)` records `far
end terminated` before it sends the network's Terminate-Request on an open LCP, so the
`Internet.stop` of `drop_call(terminate=True)` that follows finds the link down. The modem model
gives `Internet.stop` the cause of each way it leaves data mode: `_end_data(at, cause)`
([modem internals](modem-technical.md)). `device_reset(t, why)` records `device lost its power`
when `why` names the power, `device reset` otherwise, on either link that records, before it
forgets the device's flows: behind a modem the far end's PPP still has IPCP open, and the device's
new negotiation on the same call, which `_reset_lcp` sees, finds the link down already, so the next
IPCP open is its up. Behind a station `_station_link(t, ready)` records `station left` at every
transition, which changes nothing on a link that is down, and `station joined` after it when
`ready` is true and the network active, before `Station.start`. A `ready` while joined is thus a
down and an up at the same `t`: the runner sends `wifi_link` when the station's `Joined` changes or
its count of completed joins does, so a second `ready` is a station that left and joined again
within one slice, and `Station` restarts DHCP for it. A lease that expires or a NAK restarts DHCP
and records nothing, since the data link stays.

`next_link_up()` and `next_link_down()` are `Condition`s whose `find` is
`FirstFrom(lambda: self.link_changes, len(self.link_changes), lambda c: c.up is up)`, described
`the next link up` and `the next link down`, service `network`. On an `"ethernet"` link
`_next_link` raises `ValueError` instead, pointing up to `network.dhcp.next_lease()` and down to
`wifi.events`: the runner sends `wifi_link` for its own virtual station only, so nothing tells the
network when the device's station associates or leaves ([the
decision](../decisions/harness.md#behind-a-wi-fi-access-point-the-network-records-no-link-changes)).
`tests/test_net_link_changes.py` checks each cause with hand-made PPP frames, the modem model and
`_station_link`; `tests/test_modem_recovery.py` and `tests/test_arduino_softap.py` check them on
`modem_recovery`'s redial and hung modem and on `arduino_shell`'s SoftAP leaving and coming back,
and `tests/test_determinism.py` writes them in `link_changes.txt` for two runs to compare.

`Interface.lossless` defaults false; a modem with an `Internet` source returns true. `BoardLink`
matches an `rxfifo_ovf` warning by RX pin and fails only its lossless owner. No wire-protocol version
change is needed. The report appends the network transcript alongside the AT command section,
independently of the rolling wire log. JUnit stores it in the existing `esp32sim_link` property.
`Wifi` is not lossless: Ethernet frames go on the link as messages, not through a UART FIFO.
`Wifi.report()` gives the access point, the networks around and `Internet.report()`; the backend
appends it when an access point was given, and `Wifi.properties()` puts the transcript in the
`wifi` property's `network`. `Internet.report()` is `network events:` and a line per event, then,
when `connections` is not empty, `TCP connections the test opened:` and a line per connection:
`  <identity>  <name>  <state>`, with ` at <ended_at or established_at>` when either is set and
`: <unanswered()>` while it is connecting. The transcript has no such lines: its events already
hold every SYN and end.

`Internet.transcript()` is `{"esp32tl_network": 1, "events": [...]}`. Events have `t_ns` and either
`event` text or `stream`, `direction` (`in` to the device, `out` from it), and hex `data`. A stream
identifier is `<port>/<connection-number>`, `<port>` the network's, for a connection the device opened, and `device:<port>/<connection-number>`, `<port>` the device's, for one the test opened. A service wrapper records plaintext after TLS is
removed, and a bridge's wrapper what it relays, ciphertext when the device speaks TLS to the real
service; TLS logs `tls: handshake started` and `tls: handshake done, TLSv1.2`. This is a
diagnostic transcript, distinct from the version-1 UART recording format.
The determinism checker compares it exactly, times after a TLS handshake included. See
[testing](../guides/testing.md#determinism).

## Validation

`tests/test_net_ppp.py`, `test_net_mru.py`, `test_net_ip.py`, `test_net_services.py`, `test_net_mqtt.py` and
`test_net_tls.py` use handmade packets, independent checksum/framing calculations and a real TLS
client in memory. They check malformed inputs, partial frames, flow control, sequence wrap,
connection loss, keepalive, retained delivery, topic filters, Wills, UTC and certificate trust;
`test_net_mqtt.py` also tells a Will from the same bytes the client published itself by `will`.
`test_net_tls.py` also completes the handshake with OpenSSL's client over every suite and group,
with and without the extended master secret, and carries data both ways; it checks that the
server writes the same bytes for the same ClientHello after the CAs are numbered from 0 again,
other bytes on another connection or CA, and the alert and log line of each ClientHello it
cannot serve, a record that does not decrypt and a maximum fragment length.
`tests/test_net_mqtt_policy.py` drives TCP packets with both plain MQTT and a verified MemoryBIO
TLS client. It checks all CONNACK codes, retries after credential/policy changes, accepted-client
and attempt histories, rejection before takeover, pipelined packet isolation and Will suppression.
It also checks immutable wire metadata, anonymous-ID allocation only on acceptance, built-in
empty-ID rejection precedence, malformed CONNECT exclusion and propagation of policy errors,
both out of `on_data` and as a link failure in a `BoardLink` turn. A test through `Internet`
covers its traced plain and TLS listeners and keeps the policy and both histories across reset.
MQTT temporal tests send CONNECT and PUBLISH in one callback, check the `next_*` methods,
history filtering, equal timestamps, repeated polls and boolean protection. `tests/test_net_mqtt.py`
counts, through `CountingList` of `tests/conftest.py`, the records `next_message`, `next_delivery`
and `next_subscribe` read in a wait of a poll after each of 10 000 records: each once, then only the
match.
`tests/test_clock.py::test_mqtt_immediate_response_is_visible_after_waiting_for_connection`
delivers both events in one runner slice, waits for CONNECT, then retrieves the pre-armed
publication without advancing time; a newly created condition times out with its MQTT name.
TLS reconnect tests complete real MemoryBIO handshakes around a failed attempt and reset,
checking that old successes never satisfy a newly armed completion condition and that a
handshake already in progress can satisfy a later-created completion condition.
`test_net_mqtt.py` also checks held/delayed/default PUBACK, duplicate routing, identifier reuse
(including the log for a pending ID reused with other content), DUP on QoS 0, connection IDs
taken by rejected CONNECTs, policy changes, early release and cancellation on every
session-ending path, and a CleanSession=1 client with no free packet ID waiting for PUBACKs in
exact bytes, its log line once per wait and its queue ending with the connection. These tests use
the clock directly to check exact deadlines and release
timestamps.
`tests/test_net_mqtt_failures.py` negotiates PPP and drives real TCP frames with independent
checksums, both with plaintext and a verifying MemoryBIO TLS client. It receives without ACK,
releases and delays PUBACK, disconnects before ACK, then reconnects with CleanSession=0 and
retransmits with DUP. During the outage another stream still answers PING and a new NTP request
traverses the same open PPP session. It also checks that CONNACK has Session Present, that the
routed copies come again in order and that the subscription is restored, and that a delayed
PUBACK on a connection a lost link left open is emitted at its deadline, to a link that is down,
with the retained receipt kept. These are protocol-client tests.
`tests/test_net_path.py` asserts the path's stamps exactly, on PPP frames and Ethernet frames made by
hand: the RTT from a request to its answer at odd nanoseconds, each direction's share, the rates
each way, one datagram after another, a change that overtakes nothing, the floor's message, control
traffic passing the queue, what a reset and the end of the link drop, and `quiet_until`.
`tests/test_clock.py` holds real turns on `HeldClock` with a datagram on the path: a wait that ends at
its time finds it taken, and `wait_until` returns at it. It does the same for the broker's deadlines,
a PUBACK delayed by `ms(100)` and a keepalive of 10 s, with the Will at expiry, behind an access
point and behind a modem in data mode, and checks that `release()` and `disconnect` at that instant
come after them and that expiry wins a tie with a PUBACK due at the same time.
`tests/test_boardlink.py` checks `arrive`, the hold and `poll` in that order.
`tests/test_mqtt_session.py` also runs esp-mqtt on a path: the spacing of a backlog's exchanges at
200 ms and 600 ms and their ratio, the rate, and a redelivery over 600 ms.
`tests/test_net_mqtt_sessions.py` asserts the broker's sessions in exact bytes, flags and packet
IDs from raw packets: Session Present, the queue and its order, QoS 0 dropped, DUP and original IDs
on redelivery before the queue, CleanSession=1 at CONNECT and at disconnection, takeover, a refused
CONNECT and the policy leaving a session alone, a `Publication` staying with its connection, retained
messages and RETAIN, a connected client and a resumed session with no free packet ID waiting for
PUBACKs with RETAIN kept, a SUBSCRIBE on every connection while waiting queueing its retained
message once and again once it is sent, QoS 1 to a QoS 0 subscription going at once with no ID free, and
behind a queue that waits, then with the entry in front of it, a retained Will from a write that fails during SUBSCRIBE, `session`, `forget_session`, `deliveries` and
`next_delivery`; over PPP, a lost link that leaves a stored session connected until its keepalive,
the Will at 1.5 times the keepalive after the last packet and not at the loss, and a client back on
a new link before then taking its connection over, its Will discarded with CleanSession=0 on both
and published at the takeover with CleanSession=1. `tests/test_mqtt_session.py` runs esp-mqtt:
Session Present, a QoS 1 message published while offline, and one redelivered with DUP after a
`broker.disconnect` in the same instant as the publication, with both PUBACKs in the transcript;
with its 60 s keepalive, the Will 90 s after the device's last packet after `board.power_off()` and
after `modem.lose_registration()`, and none after a power cycle of 5 s that takes the session over
with Session Present.
NTP tests cover silence from startup, idempotent transitions and resumed replies using elapsed
world time; `tests/test_net_services.py` also checks the `exchanges` and `questions` records, the
`next_exchange` and `next_question` conditions, and `tests/test_net_path.py` that an exchange is
stamped when the request comes off the path, not when the link carried it. A MemoryBIO handshake
with verification disabled solely for certificate inspection checks independent leaf validity
dates, UTC normalization and a signature from the unchanged CA; firmware tests enforce those dates
against its NTP clock.
`tests/test_modem_model.py` checks network selection, binary pluses, guarded escape, reset, the
keepalive expiry run by the modem out of data mode, and the route-specific overflow policy.
`tests/test_pppos_client.py` exercises unmodified firmware in
both directions. `tests/test_determinism.py` includes plain PPP/MQTT with a raw tap, a TLS
handshake with a plaintext transcript, the position tracker's TLS session over PPP
(`test_tls_on_the_device`), and `test_wifi_http`, `http_request` fetching from the HTTP
server behind the access point with its `network.json` transcript. Firmware TLS/NTP validation lives in
`examples/position_tracker/tests/test_tracker.py`, using NVS credentials and the same in-process
services.

`tests/test_net_bridge.py` runs the bridge against services on loopback sockets, in wall time: an
answer coming back and both directions in the transcript, the service closing after its last bytes,
a refused connection, the device's close reaching the service as the end of the stream, a lost
link not reaching it until `close`, what the service sends after the device closed dropped and
counted in the transcript, the
millisecond renewal while a connection is open and none once it is closed, `close`
ending a connection the service holds open, the broker's and the HTTP server's ports taken, what
`bridge` refuses, a refusal by `bridging`, and an answer reaching the station as a TCP segment
stamped `LATENCY_NS` after the turn. `tests/test_http_request.py` and `tests/test_pppos_client.py`
run unmodified firmware through a bridge, behind the access point to `http.server` and behind the
modem to esp32tl's broker on a host socket.

`tests/test_net_ethernet.py` builds Ethernet frames by hand, with independent checksums: DHCP
DISCOVER/OFFER and REQUEST/ACK with router and DNS, a NAK for another address and silence for
another server, `next_lease()` with append order at equal timestamps, non-consuming polls,
`find_lease()`, the condition's name and its refusal of `bool()`, ARP answered for every address
but the device's, IPv4 reaching the services and other ethertypes logged once, nothing sent to a
station that has sent nothing, a SYN to port 80 through `Internet(link="ethernet")` answered
with a SYN-ACK to the station's MAC one `LATENCY_NS` later, and a keepalive that runs out after the
access point is back on the air, but before the device has sent from its address, dropping the
connection so that a SYN from the same port opens a new one.
`tests/test_net_http.py` drives `HttpServer` connections without TCP: serving and keeping a
request, a body by `Content-Length`, bytes, `HEAD` and absolute form, 404 and `next_request`'s
filters, `find_request`, `next_request` with append order at equal timestamps and non-consuming
polls, a poll after each of 10 000 requests reading each once, each condition's name and its
refusal of `bool()`, a filter holding a query refused while
the query stays in `target`, `serve` refusing a path with a query, the refused requests, unseen by
the conditions and queries, what `serve` refuses, the path of each form of target
(`/proxy/http://example.com/fw.bin` among them), and `204` and `304` ending with their head.
`tests/test_wifi.py` checks the `Wifi` interface without the emulator: the runner's flags, the
refusals, a backend that refuses the access point, and the `WifiAp` inputs at the instants of the
switches. `tests/test_http_request.py` runs ESP-IDF's unmodified `http_request` in the runner:
the fetch, a rejoin after `access_point_off()` and `access_point_on()` (with
`Wi-Fi disconnected 3` printed), a `board.wait_until` timeout and run report naming
`the next HTTP request for '/'` and `the next DHCP lease`, no access point, a wrong password that
gets no lease, the report and properties, and `wifi` refused after power-on. Not covered: MQTT,
NTP and TLS behind the access point; the layer tests cover them without a link or over PPP.
`tests/test_net_client.py` drives the connections the test opens through `Internet`, behind PPP
and behind Ethernet each, against a device made of handmade segments with checksums computed
independently: the SYN and its MSS, data written before the handshake going in MSS pieces, data
both ways, a retransmission received once, both orders of closing, refused (with nothing of what
was written in the transcript), reset, a zero window and its updates, a segment ahead of the
stream, ports counting up across a lost link, the conditions' names and refusals, text written as
UTF-8 and decoded with replacement, the SYN sent again on its schedule and then timed out, a
SYN-ACK and a RST to a SYN sent again ending its timer, a SYN-ACK slower than the RTO, the
clauses of `unanswered()` and the report's connection lines, `accepting` connecting again after a
refusal, giving up after 30 and raising for a lost connection and an unreachable device, `device_reset` for a reset and a power loss (the test's
connections lost, the device's own kept, the address gone until IPCP or DHCP gives it again), a
RELEASE giving the address up, and `NotReachable` before IPCP, before a DHCP ACK, after an OFFER
alone, without a frame from the station and off the air. `tests/test_http_server.py` runs the
`http_server` image behind the access point: a `GET` with `Connection: close`, two `POST`s on one
connection read by their `Content-Length`, a body over the firmware's limit, a refused port,
connecting before the lease, and a connection lost at `board.reset()` and at `board.power_off()`,
then a new one once the server listens again; and, with no wait for a console line, a `GET` sent
when the lease holds, whose SYN gets no answer and is accepted when sent again 1 s later, a wait on
it that times out before then naming the SYN, with the report's connection lines the user guide
quotes, and a port nothing listens on, refused at the SYN sent again.
`tests/test_arduino_shell.py` does the same on Arduino-ESP32's `WebServer`: a `GET` sent at the
lease answered after the SYN sent again, and, with `listen_delay_ms` 200, that `GET` refused and
`accepting(80)` refused once, then accepted, before a `GET` that is answered. `tests/test_clock.py::test_a_tcp_wait_that_times_out_names_the_syns_sent_again_on_the_clock`
checks that the held clock takes a turn at each `_syn_due` and the `NeverHappened` message. Not
covered: a connection to real firmware behind the modem; `test_net_client.py` covers PPP with
segments.

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
  [packet loss on the path is postponed](../decisions/harness.md#packet-loss-on-the-network-path-is-postponed),
  [a bridge relays a TCP port to a real service, on the wall
  clock](../decisions/harness.md#a-bridge-relays-a-tcp-port-to-a-real-service-and-puts-the-run-on-the-wall-clock),
  [the test opens TCP connections to the device, with no
  timers](../decisions/harness.md#the-test-opens-tcp-connections-to-the-device-on-the-same-tcp-with-no-timers),
  [the test's TCP sends an unanswered SYN
  again](../decisions/harness.md#the-tests-tcp-sends-a-syn-the-device-does-not-answer-again-on-the-boards-clock).

## HTTP client

`esp32tl/net/http_client.py` implements `HttpClient`, `HttpExchange` and `ClientResponse`.
`Internet.http_client` uses `Internet.connect`, so address discovery, latency, rate limiting,
TCP stream identities and lifecycle errors are shared with raw connections. `TcpConnection`
notifies internal observers after received bytes, peer FIN and terminal state updates. The
exchange incrementally parses the TCP receive buffer on those notifications, caches a response
or error, logs once, and closes its end on completion or parsing failure. Its `response`, a `Condition[ClientResponse | None]`, only reads the
cached outcome; polling has no network effects. Its `waiting` is the connection's `unanswered`, so a
timed-out wait names the SYNs that got no answer. A connection whose `state` is `refused`,
`reset`, `lost`, `closed` or `timed out` before the response completed caches `ConnectionEnded`
(`<connection name>: <state> before HTTP response completed`).

The parser accepts HTTP/1.0 and HTTP/1.1 responses to HTTP/1.1 requests, skips informational
heads except 101, validates status/header syntax and framing, decodes chunked bodies (including
extensions and validated, discarded trailers), and uses peer FIN for unframed bodies. A reset
is not an EOF. Content-Length together with Transfer-Encoding is rejected. A completed framed
response remains readable after later connection loss. Request validation and header encoding
happen before connecting. No host sockets or wall clock are used.

`tests/test_net_http_client.py` covers framing at every byte boundary, malformed and truncated
responses, request validation, JSON, transport failure and report events. `tests/test_http_server.py`
validates GET `/status` and POST `/echo` through the access point on the ESP-IDF image;
`tests/test_arduino_shell.py` uses the client for Arduino WebServer's `/status`.

The response parser retains its framing state, receive-buffer offset, delimiter search offset
and decoded chunks between notifications. It reads the shared TCP bytearray without taking a
full `received` snapshot and copies each completed chunk once into the decoded body. Searches
resume at the new bytes with only delimiter overlap; neither old chunks nor the remaining
buffer suffix are repeatedly decoded or copied. The 64 KiB metadata limits exclude their
terminating separator, including a possible incomplete separator while waiting for more bytes.
Boundary tests split separators at the limit and one byte over it for heads, chunk size lines
and trailers. A 32,000 one-byte-chunk regression test bounds callback time with a generous
5-second timeout.

## Host link behind a virtual station

`Internet(link="station", mac=...)` constructs `net.station.Station`. It implements Ethernet
framing, a DHCP client, a host-only ARP cache and pending datagrams awaiting resolution. DHCP
validates BOOTP hardware fields, transaction ID, client MAC, cookie and server identifier; OFFER
selects a server and sends REQUEST, ACK installs a lease, and NAK restarts with a new deterministic
transaction ID. It requires a nonzero lease lifetime and a valid subnet mask. Duplicate or foreign
replies do not replace a bound lease. A lost OFFER or ACK causes the same request to be retried.

`poll` and `quiet_until` schedule DHCP retries, lease expiry and one-second ARP retries on board
time. DHCP intervals double from 4 s to 64 s, without host-clock jitter. The broadcast flag is set;
ESP-IDF v5.5.4's `dhcps_response_ip_set` honors it. The runner coalesces identical pending UDP
68-to-67 frames in station mode while the relay applies backpressure.
In protocol v2, `wifi_link` gates DHCP; no requests are submitted before Joined.

IPv4 total length and ARP's fixed 28-byte payload trim any trailing guard bytes, as in `Ethernet`.
Only IPv4 to the leased host reaches `Network`, apart from DHCP replies. Services therefore retain
their existing implementation and listen at that address alone; DNS's answer is updated on ACK.
`Internet.connect` passes the leased host address to both `TcpConnection` and `Network.connect`.
Option 54 supplies its default destination; option 3 routes off-subnet traffic. ARP resolves the
next hop before queued datagrams leave. A newly unresolved destination keeps the earliest pending
ARP retry deadline. Each due batch retries all unresolved destinations and schedules the next
batch one second later; resolving every pending destination clears the deadline.
ARP replies and requests addressed to the host populate
the cache, and the host answers requests only for itself.

`Internet.device_reset` and lease expiry clear the address, ARP cache and pending datagrams, reset
IP connections and path queues. Reset waits for data-link readiness before acquisition; lease expiry
restarts acquisition on the current link. `is_leased` reads `Internet.lease` at each call.
The station appends each acknowledged `HostLease` to its private `_leases` list, which survives
`Station.stop` and `start`. `Internet.leases` exposes the live, read-only view from `Station.leases`.
A saved view sees later acknowledgements. `next_lease()` captures the history's length at the call
and returns the record at that index once it arrives. `find_lease()` returns the first lease. `Internet._station(member, dhcp)` returns the `Station`, or
raises the `ValueError` naming `network.<member>` behind another link, with the pointer to
`network.dhcp.<dhcp>` behind an access point. There is no renewal/rebinding, conflict detection, or
idle keep-alive. `tests/test_net_station.py` exercises the host against handmade packets;
`tests/test_arduino_softap.py` runs the actual Arduino DHCP server and HTTP endpoint.

Station lifecycle is driven by protocol v2 `wifi_link` events, using the emulated slice-boundary time. The DHCP transaction ID advances on every acquisition and survives network stop/reset. Disconnected stations have no DHCP or ARP deadline and ignore incoming Ethernet. Link loss resets the IP transports and path queues. Lease history remains available, but only `lease` denotes the current lease.

### Delayed SoftAP reproduction

With Arduino shell `shell/ap_delay_ms=u32(15300)`, the unmodified `origin/main`
(34f2c49) host logged DISCOVER at 0, 4 and 12 s. The runner retained one identical
request while esp32sim rejected relay input before Joined. The first OFFER arrived
at 15.576 s and ACK at 15.578 s, without waiting for the 28 s retry. Thus this generic
firmware reproduces premature DHCP/backoff, but not the reported 28 s lease delay.
An OFFER at 15.576 s, before the next client retry, confirms delivery of the queued
request. Inside esp32sim, `receive_relay_frame` rejects an unjoined station; once joined,
`next_rx` drains pending management/EAPOL frames before taking Ethernet from `eth_rx`.

With protocol v2, the open-network readiness event and first DISCOVER were both
at 15.575 s; OFFER/REQUEST at 15.577 s and ACK at 15.579 s. WPA2 readiness and DISCOVER
were both at 15.716 s; REQUEST at 15.718 s and ACK at 15.720 s. Two runs produced
identical event lists and nanosecond timestamps for each security mode. The regression
asserts the DISCOVER/readiness timestamp equality, not these firmware-dependent values
or a subsecond lease bound. These measurements use the pinned container firmware and
the development-profile runner on macOS; they do not establish cross-platform timing.
