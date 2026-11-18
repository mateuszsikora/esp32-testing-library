"""The simulated network behind a modem or either virtual Wi-Fi role, on the board's clock."""

import math
from collections import deque
from collections.abc import Callable
from datetime import datetime
from ipaddress import IPv4Address
from typing import Literal, cast, overload

from esp32tl.interfaces.condition import Condition
from esp32tl.link.boardlink import Output
from esp32tl.net.bridge import Bridge, address
from esp32tl.net.client import NotReachable, TcpConnection
from esp32tl.net.dhcp import Dhcp
from esp32tl.net.dns import Dns
from esp32tl.net.ethernet import Ethernet
from esp32tl.net.http import HttpServer
from esp32tl.net.http_client import HttpClient
from esp32tl.net.ip import ACK, SYN, TCP, Connection, Fragmented, Network, StreamService, ipv4
from esp32tl.net.mqtt import Broker
from esp32tl.net.ntp import DEFAULT_UTC, Ntp
from esp32tl.net.ppp import Ppp
from esp32tl.net.station import DEFAULT_MAC, HostLease, Station
from esp32tl.net.tls import CertificateAuthority, Tls
from esp32tl.report import board_time
from esp32tl.units import NS_PER_S, Duration, Rate, shown

LATENCY_NS = 1_000_000
LOCAL = "10.64.0.1"  # the network's own address: its DNS, its router, the test's side of TCP
DEFAULT_RTT = Duration(LATENCY_NS)

Link = Literal["ppp", "ethernet", "station"]


class _Direction:
    """One direction of the path: a FIFO of datagrams, each with the time it leaves. `rate` spaces
    them, one after another, then each takes `delay`; none leaves before the one ahead of it."""

    def __init__(self, delay: int) -> None:
        self.delay = delay
        self.rate: Rate | None = None
        self.queue: deque[tuple[int, bytes]] = deque()
        self._free = 0
        self._last = 0

    def leaves(self, at: int, size: int) -> int:
        """When a datagram of `size` bytes that enters at `at` leaves."""
        self._free = max(at, self._free)
        if self.rate is not None:
            self._free += math.ceil(size * 8 * NS_PER_S / self.rate.bit_s)
        self._last = max(self._last, self._free + self.delay)
        return self._last

    def clear(self) -> None:
        self.queue.clear()
        self._free = self._last = 0

    def head(self) -> int | None:
        return self.queue[0][0] if self.queue else None


class Internet:
    """One per modem over PPP, or per virtual Wi-Fi role over Ethernet with ARP and DHCP. DNS
    and NTP always listen, the broker on TCP 1883 and the HTTP server on 80; the test registers
    other TCP services by port, bridges a port to a real service with `bridge`, and opens a
    connection to the device with `connect`.

    Responses are stamped 1 ms after the request's UART chunk or Ethernet frame, or the path's RTT
    after it (`set_path`). Public calls run while the board is held, and callbacks while it
    advances: neither uses host time or sockets, but for a bridge's. `bridging` is called with the
    call before a bridge is made, and refuses it by raising: the backend's say on the wall clock.

    The end of the link, a modem's call or an access point off the air, loses the connections the
    test opened and leaves the device's open and silent, as the far end of a device that lost its
    power or its coverage is: no FIN or RST reaches their service, which ends them by its own
    timeout, as the broker does at its keepalive. The broker's deadlines run with the link down.
    """

    def __init__(
        self,
        now: Callable[[], int],
        world: Callable[[], datetime] = lambda: DEFAULT_UTC,
        *,
        link: Link = "ppp",
        mac: str = DEFAULT_MAC,
        bridging: Callable[[str], None] = lambda call: None,
    ) -> None:
        self._now = now
        self._bridging = bridging
        self._at: int | None = None
        self._out: list[Output] = []
        self._kind = link
        self._mac = mac
        self._up = _Direction(0)
        self._down = _Direction(LATENCY_NS)
        self._datagram: int | None = None  # the size of the IPv4 datagram the link is sending
        self.events: list[dict[str, object]] = []
        self.ip = Network(self._downlink, self.log, mtu=self._mtu, carries=self._carries)
        self.dns = Dns("10.64.0.1", self.log)
        self.ntp = Ntp(self.now, world, self.log)
        self.ip.udp.update({53: self.dns, 123: self.ntp})
        self.broker = Broker(self.now, self.log)
        self.listen(1883, self.broker)
        self.http_client = HttpClient(self.connect, self.log)
        self.http = HttpServer(self.now, self.log)
        self.listen(80, self.http)
        # The services a bridge may take the place of, by port.
        self._builtin: dict[int, str] = {1883: "the broker", 80: "the HTTP server"}
        self.bridges: dict[int, Bridge] = {}
        # The connections the test opened, in order, and how many it opened to each port.
        self.connections: list[TcpConnection] = []
        self._opened: dict[int, int] = {}
        # Behind the modem: how many times IPCP had opened when the device last reset, which the
        # address it gave is from; `None` when the link is newer than the reset.
        self._stale: int | None = None
        # Serves behind an access point only; its leases outlive the link's restarts.
        self.dhcp = Dhcp(self.now, self.log)
        self.link: Ppp | Ethernet | Station = self._peer()
        self.active = False

    @property
    def ppp(self) -> Ppp:
        """The PPP link of a network behind a modem."""
        if not isinstance(self.link, Ppp):
            if isinstance(self.link, Station):
                raise AttributeError("this network is behind a Wi-Fi station: it has no PPP")
            raise AttributeError("this network is behind a Wi-Fi access point: it has no PPP")
        return self.link

    @property
    def ethernet(self) -> Ethernet:
        """The Ethernet of a network behind a Wi-Fi access point."""
        if not isinstance(self.link, Ethernet):
            if isinstance(self.link, Station):
                raise AttributeError(
                    "this network has a station link, not an access-point Ethernet link"
                )
            raise AttributeError("this network is behind a modem: it has no Ethernet")
        return self.link

    @property
    def lease(self) -> HostLease | None:
        """The station's current lease, or None before DHCP acknowledges it."""
        return self.link.lease if isinstance(self.link, Station) else None

    def when_leased(self) -> Callable[[], HostLease | None]:
        """Current-state condition: the virtual station holds a DHCP lease."""
        if not isinstance(self.link, Station):
            raise ValueError("network.when_leased requires wifi.station()")
        return Condition(lambda: self.lease, "the station has a DHCP lease", "DHCP station")

    def _station_changed(self) -> None:
        if self.lease is None:
            self.dns.address = bytes(4)
            self._out.clear()
            self._link_ended()
        else:
            self.dns.address = IPv4Address(self.lease.address).packed
            self.ip.resume(self.lease.address)

    def _carries(self) -> bool:
        """Whether the link carries IP now: on, and behind a station, with a lease."""
        return self.active and (not isinstance(self.link, Station) or self.lease is not None)

    def _forget_syns(self, address: str | None = None) -> None:
        """Drop the SYNs on the way up from a device stack that is gone, from `address` or from
        any: arriving after `forget_device`, each would open a flow left in its handshake, which
        nothing ends. The rest of what that stack sent before still arrives, as it would at a
        service on the internet."""

        def stale(datagram: bytes) -> bool:
            try:
                parsed = ipv4(datagram)
            except Fragmented:
                return False
            if parsed is None or parsed[0] != TCP or len(parsed[3]) < 20:
                return False
            if address is not None and parsed[1] != address:
                return False
            flags = parsed[3][13]
            return bool(flags & SYN) and not flags & ACK

        self._up.queue = deque(entry for entry in self._up.queue if not stale(entry[1]))

    def _mtu(self) -> int:
        return self.link.peer_mru if isinstance(self.link, Ppp) else self.link.mtu

    def now(self) -> int:
        return self._now() if self._at is None else self._at

    def log(self, event: str) -> None:
        self.events.append({"t_ns": self.now(), "event": event})

    def transcript(self) -> dict[str, object]:
        """Application traffic and network events, with no TLS keys: plaintext, but on a bridged
        port that carries TLS, where it is the ciphertext the bridge relays."""
        return {"esp32tl_network": 1, "events": [dict(e) for e in self.events]}

    def report(self) -> list[str]:
        lines = ["network events:"]
        for event in self.events:
            detail = event.get("event")
            if detail is None:
                detail = f"stream {event['stream']} {event['direction']} {event['data']} (hex)"
            lines.append(f"  {board_time(cast(int, event['t_ns']))}  {detail}")
        return lines

    @overload
    def listen(self, port: int, service: StreamService, *, tls: CertificateAuthority) -> Tls: ...

    @overload
    def listen(self, port: int, service: StreamService, *, tls: None = None) -> None: ...

    @overload
    def listen(
        self, port: int, service: StreamService, *, tls: CertificateAuthority | None
    ) -> Tls | None: ...

    def listen(
        self, port: int, service: StreamService, *, tls: CertificateAuthority | None = None
    ) -> Tls | None:
        """Install a TCP service, returning its TLS listener when `tls` is supplied.

        Plain listeners return None. The port applies to every destination address.
        """
        if not 1 <= port <= 65535:
            raise ValueError("TCP port must be between 1 and 65535")
        if port in self.ip.tcp:
            raise ValueError(f"TCP port {port} already has a service")
        return self._install(port, service, tls)

    def _install(
        self, port: int, service: StreamService, tls: CertificateAuthority | None
    ) -> Tls | None:
        traced = _Traced(self, port, service)
        if tls is not None:
            listener = Tls(traced, tls, self.log)
            self.ip.tcp[port] = listener
            return listener
        self.ip.tcp[port] = traced
        return None

    def bridge(self, port: int, to: tuple[str, int]) -> Bridge:
        """Relays TCP `port`, on every destination address, to the real service at `to`, a host
        and a port: each connection the device opens there is one the harness opens to `to`, and
        the bytes go both ways as they are, TLS included. Takes the place of the broker on 1883 or
        the HTTP server on 80; another port must be free. The whole run is then on the wall
        clock, so a bridge is given before `board.power_on()`. Returns the `Bridge`."""
        call = f"network.bridge({port!r}, {to!r})"
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError(f"{call}: the TCP port must be between 1 and 65535")
        host, host_port = address(to, call)
        replaced = self._builtin.get(port)
        if port in self.ip.tcp and replaced is None:
            raise ValueError(f"{call}: TCP port {port} already has a service")
        self._bridging(call)
        bridge = Bridge(port, (host, host_port), self.log)
        self._builtin.pop(port, None)
        self.ip.tcp.pop(port, None)
        self._install(port, bridge, None)
        self.bridges[port] = bridge
        instead = f", in place of {replaced}" if replaced is not None else ""
        self.log(f"bridge: TCP {port} → {host}:{host_port}{instead}")
        return bridge

    @property
    def device_address(self) -> str | None:
        """The device's address on the link now: the one IPCP gave it behind the modem, the one
        this link's DHCP acknowledged behind the access point; `None` before it has one."""
        if isinstance(self.link, Ppp):
            fresh = self.link.ipcp_open and self.link.ipcp_opened != self._stale
            return self.link.peer if fresh else None
        return self.link.device_address

    def device_reset(self, t: int, why: str) -> None:
        """The device's chip reset, or its power went, at `t`, `why` saying which, as `the device
        reset`: its TCP and its address are gone. Every connection the test opened is lost, and
        the device has no address until IPCP opens again or DHCP acknowledges one. The
        connections the device opened stay, orphaned, as no network knows of a reboot, but for
        one still in its handshake or one its service had closed, which go now, and the old
        stack's SYNs still on the way up, which never arrive. The rest outlive the link's end
        too: a RST of the new stack, in answer to what their service writes, ends them, or the
        service's end, as the broker's keepalive, with no FIN; a SYN of the new stack on one of
        them gets no answer."""
        previous = self._at
        self._at = t
        try:
            self.ip.forget_device(why)
            self._forget_syns()
            if isinstance(self.link, Ppp):
                self._stale = self.link.ipcp_opened
            elif isinstance(self.link, Station):
                self._out.clear()
                self.link.stop()
            else:
                self.link.device_address = None
        finally:
            self._at = previous

    def connect(self, port: int, *, address: str | None = None) -> TcpConnection:
        """Open TCP to the device, from the station's leased address or otherwise `10.64.0.1`.
        The SYN enters the network's path at the board's current instant, and reaches the device
        after the path's way down. `address` is the device's, `device_address` unless given, as
        for a device with a static address behind the access point. Returns the `TcpConnection`.
        Raises `NotReachable` while the link is down, before the device has an address, and
        behind the access point before the station has sent a frame."""
        call = f"network.connect({port!r}" + (
            f", address={address!r})" if address is not None else ")"
        )
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError(f"{call}: the TCP port must be between 1 and 65535")
        if address is not None:
            try:
                address = str(IPv4Address(address))
            except ValueError:
                raise ValueError(f"{call}: {address!r} is not an IPv4 address") from None
        if not self.active:
            down = (
                "the access point is off the air"
                if self._kind == "ethernet"
                else "the station network is down"
                if self._kind == "station"
                else "the modem has not dialled"
            )
            raise NotReachable(f"{call}: {down}: the network reaches no device")
        if isinstance(self.link, Ppp):
            if self.device_address is None:
                raise NotReachable(
                    f"{call}: the device has no address yet: PPP has not opened IPCP"
                    + (" since the device reset" if self.link.ipcp_open else "")
                )
        elif isinstance(self.link, Station):
            if self.lease is None:
                raise NotReachable(
                    f"{call}: the device has leased no address to the station yet; "
                    "wait for network.when_leased()"
                )
        elif address is None and self.link.device_address is None:
            # Only once this link has leased an address: a fresh link has had none to lose.
            since = (
                " since the device last reset or gave its address up" if self.link.leased else ""
            )
            raise NotReachable(
                f"{call}: the device has no address yet: DHCP has acknowledged none on this"
                f" link{since}; wait for network.dhcp.next_lease(), or give address= for a"
                " static one"
            )
        elif self.link.device_mac is None:
            raise NotReachable(
                f"{call}: the station has sent no frame yet: the network has no MAC to send to"
            )
        device = address or self.device_address
        assert device is not None
        number = self._opened.get(port, 0) + 1
        identity = f"device:{port}/{number}"

        def record(direction: str, data: bytes) -> None:
            self.events.append(
                {"t_ns": self.now(), "stream": identity, "direction": direction, "data": data.hex()}
            )

        local = self.lease.address if self.lease is not None else LOCAL
        connection = TcpConnection(device, port, local, identity, self.now, record)
        # Kept only once the network has taken it: it raises when it has no port left.
        connection._opened(*self.ip.connect(device, port, local, connection))
        self._opened[port] = number
        self.connections.append(connection)
        return connection

    def _pump(self) -> None:
        for bridge in self.bridges.values():
            bridge.pump()

    def close(self) -> None:
        """Ends every bridge's connections to its service: the run is over."""
        for bridge in self.bridges.values():
            bridge.close()

    def set_path(
        self,
        *,
        rtt: Duration = DEFAULT_RTT,
        downlink: Rate | None = None,
        uplink: Rate | None = None,
    ) -> None:
        """The path between the link and the network, IPv4 only: what the device sends at `t` is
        answered at `t + rtt`, plus the time the datagrams take at `uplink` and `downlink`, `None`
        being no limit. Each call states the whole path, and applies to datagrams entering after
        it; none overtakes another. These are the test's parameters, not a model of an operator.
        """
        if not isinstance(rtt, Duration):
            raise TypeError(f"network.set_path: rtt is {rtt!r}, not a Duration, as ms(200)")
        if rtt.ns < LATENCY_NS:
            raise ValueError(
                f"network.set_path: rtt is {shown(rtt.ns / 1_000_000)} ms;"
                " the network answers 1 ms after a request at the least"
            )
        for name, rate in (("downlink", downlink), ("uplink", uplink)):
            if rate is not None and not isinstance(rate, Rate):
                raise TypeError(f"network.set_path: {name} is {rate!r}, not a Rate, as kbit_s(512)")
        # The floor stays on the way down, so that at the default a service takes a request at the
        # request's own time, and the rest is shared. The two always add up to `rtt`, exactly.
        self._up.delay = (rtt.ns - LATENCY_NS) // 2
        self._down.delay = rtt.ns - self._up.delay
        self._up.rate, self._down.rate = uplink, downlink
        self.log(f"net: path: rtt {rtt}, downlink {downlink or 'any'}, uplink {uplink or 'any'}")

    def _peer(self) -> Ppp | Ethernet | Station:
        def send(data: bytes) -> None:
            size = self._datagram
            if self._kind == "station":
                # ARP resolution can defer IP beyond _downlink's call. Classify the emitted
                # frame here: controls have fixed latency, IP still takes the configured path.
                size = (
                    len(data) - 14
                    if data[12:14] == b"\x08\x00" and data[26:30] != bytes(4)
                    else None
                )
            if size is None:  # the link's own: LCP, IPCP, ARP and DHCP
                self._out.append((self.now() + LATENCY_NS, data))
            else:
                leaves = self._down.leaves(self.now(), size)
                self._down.queue.append((leaves, data))
                self._release()

        if self._kind == "station":
            return Station(
                send,
                self._uplink,
                self.now,
                mac=self._mac,
                log=self.log,
                changed=self._station_changed,
            )
        if self._kind == "ethernet":
            return Ethernet(send, self._uplink, self.dhcp, log=self.log, gave_up=self._gave_up)
        return Ppp(send, self._uplink, log=self.log, ended=self._link_ended)

    def _gave_up(self, address: str) -> None:
        self.ip.forget_device("the device gave its address up", address=address)
        self._forget_syns(address)

    def _link_ended(self) -> None:
        # Behind a station the device is the access point, and keeps its stack when the station
        # leaves; behind its own link, the device's stack goes with the link.
        self.ip.lose_link(orphan=not isinstance(self.link, Station))
        self._up.clear()
        self._down.clear()

    def _uplink(self, datagram: bytes) -> None:
        at = self.now()
        self._up.queue.append((self._up.leaves(at, len(datagram)), datagram))
        self._arrive(at)

    def _arrive(self, now: int) -> None:
        """The network takes what has come up the path by `now`, each datagram at its own time."""
        previous = self._at
        try:
            while self._up.queue and self._up.queue[0][0] <= now:
                self._at, datagram = self._up.queue.popleft()
                self.ip.receive(datagram)
        finally:
            self._at = previous

    def _downlink(self, datagram: bytes) -> None:
        if not self.active:
            return  # what a service writes on a connection whose link has ended goes nowhere
        self._datagram = len(datagram)
        try:
            self.link.send_ip(datagram)
        finally:
            self._datagram = None

    def _release(self) -> None:
        """Hand over what leaves within the latency the network declares: an answer in a turn at
        most that long after its request is never late, and the rest stays ours to drop on a
        reset until `quiet_until` brings the turn of its time."""
        horizon = self.now() + LATENCY_NS
        while self._down.queue and self._down.queue[0][0] <= horizon:
            self._out.append(self._down.queue.popleft())

    def start(self, t: int) -> None:
        """The link comes up at `t`: a modem's dial, which starts PPP, or an access point on the
        air."""
        self.stop(t)
        self.active = True
        self._at = t
        try:
            if isinstance(self.link, Ppp):
                self.link.start()
        finally:
            self._at = None

    def _station_link(self, t: int, ready: bool) -> None:
        """Apply the runner's data-link transition on the emulated clock."""
        if not isinstance(self.link, Station):
            return
        self._at = t
        try:
            self.log(f"wifi station: {'ready' if ready else 'disconnected'}")
            self.link.stop()
            if ready and self.active:
                self.link.start()
        finally:
            self._at = None

    def stop(self, at: int | None = None) -> None:
        previous = self._at
        self._at = self._now() if at is None else at
        try:
            self.active = False
            self._link_ended()
            self._out.clear()
            if isinstance(self.link, Station):
                self.link.stop()
            else:
                self.link = self._peer()
            self._stale = None
        finally:
            self._at = previous

    def terminate(self, t: int) -> list[Output]:
        """What the far end sends at `t` to end the link behind a modem."""
        self._at = t
        try:
            self.ppp.terminate()
            return self._take()
        finally:
            self._at = None

    def receive(self, t: int, data: bytes) -> list[Output]:
        """What the device sent at `t`: bytes of the modem's line, or one Ethernet frame."""
        self._at = t
        try:
            if isinstance(self.link, Station):
                self.link.poll()
            self._arrive(t)
            self.link.receive(data)
            return self._take()
        finally:
            self._at = None

    def arrive(self, now: int) -> None:
        """Take in what has come up the path by `now`, then run the broker's deadlines due by it
        and hand over what bridged services sent, before the test looks at the turn at `now`: a
        record stamped `now`, a PUBACK or an expiry due at `now`, is there when `board.now` says
        it. What this answers goes out in `poll`. With the link down the broker's deadlines run
        all the same, and `poll` has nothing to send."""
        self._at = now
        try:
            if self.active and isinstance(self.link, Station):
                self.link.poll()
            self._arrive(now)
            self.broker.poll()
            self._pump()
        finally:
            self._at = None

    def poll(self, now: int) -> list[Output]:
        # Finds nothing left after `arrive` at the same `now`: what calls `poll` alone still gets
        # the same answers, at the same stamps.
        self._at = now
        try:
            if self.active and isinstance(self.link, Station):
                self.link.poll()
            self._arrive(now)
            self.broker.poll()
            self._pump()
            return self._take()
        finally:
            self._at = None

    def _take(self) -> list[Output]:
        self._release()
        out, self._out = self._out, []
        return out

    def quiet_until(self) -> int | None:
        """The earliest of the broker's deadlines, the path's next datagram, either way, and the
        next whole millisecond while a bridged connection is open; with the link down, the
        broker's and the bridges', as its keepalive still ends the connections the link's end
        left open, and a bridged service may still send or close."""
        bridged = (bridge.quiet_until(self._now()) for bridge in self.bridges.values())
        if not self.active:
            deadlines = (self.broker.quiet_until(), *bridged)
            return min((bound for bound in deadlines if bound is not None), default=None)
        bounds = (
            self.link.quiet_until() if isinstance(self.link, Station) else None,
            self.broker.quiet_until(),
            self._up.head(),
            self._down.head(),
            *bridged,
        )
        return min((bound for bound in bounds if bound is not None), default=None)

    def latency(self) -> int:
        return LATENCY_NS


class _Traced:
    def __init__(self, network: Internet, port: int, service: StreamService) -> None:
        self.network, self.port, self.service = network, port, service
        self._connections = 0

    def accept(
        self, peer: tuple[str, int], write: Callable[[bytes], None], close: Callable[[], None]
    ) -> Connection:
        self._connections += 1
        identity = f"{self.port}/{self._connections}"

        def record(direction: str, data: bytes) -> None:
            self.network.events.append(
                {
                    "t_ns": self.network.now(),
                    "stream": identity,
                    "direction": direction,
                    "data": data.hex(),
                }
            )

        def send(data: bytes) -> None:
            record("in", data)
            write(data)

        inner = self.service.accept(peer, send, close)

        class TracedConnection:
            def on_data(self, data: bytes) -> None:
                record("out", data)
                inner.on_data(data)

            def on_close(self) -> None:
                inner.on_close()

        return TracedConnection()
