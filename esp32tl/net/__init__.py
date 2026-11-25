"""The simulated network behind a modem or either virtual Wi-Fi role, on the board's clock."""

import math
import struct
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from ipaddress import IPv4Address
from typing import Literal, cast, overload

from esp32tl.interfaces.condition import Condition, FirstFrom
from esp32tl.interfaces.history import History
from esp32tl.link.boardlink import Output
from esp32tl.net.bridge import Bridge, address
from esp32tl.net.client import ConnectionEnded, NotReachable, TcpConnection
from esp32tl.net.dhcp import Dhcp
from esp32tl.net.dns import Dns
from esp32tl.net.ethernet import Ethernet
from esp32tl.net.handlers import reraise
from esp32tl.net.http import HttpServer
from esp32tl.net.http_client import HttpClient
from esp32tl.net.ip import (
    ACK,
    SYN,
    TCP,
    UDP,
    Connection,
    Fragmented,
    Network,
    StreamService,
    ipv4,
)
from esp32tl.net.mqtt import Broker
from esp32tl.net.ntp import DEFAULT_UTC, Ntp
from esp32tl.net.ppp import Ppp
from esp32tl.net.station import DEFAULT_MAC, HostLease, Station
from esp32tl.net.tls import CertificateAuthority, Tls
from esp32tl.report import board_time
from esp32tl.units import NS_PER_S, Duration, Rate, shown

LATENCY_NS = 1_000_000
# How long `accepting` waits after a refusal before it connects again, and how many connections
# it opens at most.
ACCEPT_RETRY_NS = 1_000_000_000
ACCEPT_ATTEMPTS = 30
LOCAL = "10.64.0.1"  # the network's own address: its DNS, its router, the test's side of TCP
DEFAULT_RTT = Duration(LATENCY_NS)

Link = Literal["ppp", "ethernet", "station"]
# What brought the link up or ended it, as `LinkChange.cause` says.
LinkCause = Literal[
    "IPCP open",
    "station joined",
    "call ended",
    "far end terminated",
    "device terminated",
    "device renegotiated LCP",
    "device renegotiated IPCP",
    "device left data mode",
    "modem hung",
    "coverage lost",
    "modem off",
    "modem lost power",
    "modem restarted",
    "station left",
    "device reset",
    "device lost its power",
]


@dataclass(frozen=True)
class LinkChange:
    """The link between the device and the network came up or went down, on the board's time.

    Behind a modem the link is up from the instant IPCP opens; behind a virtual station, from the
    instant the station has joined the device's access point. `cause` says what brought it up,
    `IPCP open` or `station joined`, or what ended it: `call ended`, `far end terminated`,
    `device terminated`, `device renegotiated LCP`, `device renegotiated IPCP`,
    `device left data mode`, `modem hung`, `coverage lost`, `modem off`, `modem lost power`,
    `modem restarted`, `station left`, `device reset` or `device lost its power`."""

    at: Duration
    up: bool
    cause: LinkCause


Block = Literal["drop", "refuse"]


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
    after it (`set_path`); the path may drop every datagram, and `block` one TCP port's. Public
    calls run while the board is held, and callbacks while it advances: neither uses host time or
    sockets, but for a bridge's. `bridging` is called with the call before a bridge is made, and
    refuses it by raising: the backend's say on the wall clock. `test_error` takes an exception of
    the test's code that a service ran while the board advanced, as a broker's `on_publish`
    handler's or an HTTP handler's (`esp32tl.net.handlers`); by default it is raised where the
    service was called.

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
        test_error: Callable[[BaseException], None] = reraise,
    ) -> None:
        self._now = now
        self._bridging = bridging
        self._at: int | None = None
        self._out: list[Output] = []
        self._kind = link
        self._mac = mac
        self._up = _Direction(0)
        self._down = _Direction(LATENCY_NS)
        self._drop = False  # the path drops every datagram entering it: `set_path(drop=True)`
        self._datagram: int | None = None  # the size of the IPv4 datagram the link is sending
        self._events: list[dict[str, object]] = []
        self.ip = Network(
            self._downlink, self.log, mtu=self._mtu, carries=self._carries, now=self.now
        )
        self.dns = Dns("10.64.0.1", self.now, self.log)
        self.ntp = Ntp(self.now, world, self.log)
        self.ip.udp.update({53: self.dns, 123: self.ntp})
        self.broker = Broker(self.now, self.log, test_error=test_error)
        self.listen(1883, self.broker)
        self.http_client = HttpClient(self.connect, self.log)
        self.http = HttpServer(self.now, self.log, test_error=test_error)
        self.listen(80, self.http)
        # The services a bridge may take the place of, by port.
        self._builtin: dict[int, str] = {1883: "the broker", 80: "the HTTP server"}
        self.bridges: dict[int, Bridge] = {}
        # The connections the test opened, in order, and how many it opened to each port.
        self._connections: list[TcpConnection] = []
        self._opened: dict[int, int] = {}
        # The `accepting` conditions, each connecting again after a refusal.
        self._accepting: list[_Accepting] = []
        # Behind the modem: how many times IPCP had opened when the device last reset, which the
        # address it gave is from; `None` when the link is newer than the reset.
        self._stale: int | None = None
        # Serves behind an access point only; its leases outlive the link's restarts.
        self.dhcp = Dhcp(self.now, self.log)
        # Behind a modem or a station: each time the link came up or went down, in order.
        self._link_changes: list[LinkChange] = []
        self._link_up = False
        self.link: Ppp | Ethernet | Station = self._peer()
        self.active = False

    @property
    def events(self) -> Sequence[dict[str, object]]:
        return History(self._events.__len__, lambda i: self._events[i].copy())

    @property
    def connections(self) -> Sequence[TcpConnection]:
        return History(self._connections)

    @property
    def link_changes(self) -> Sequence[LinkChange]:
        return History(self._link_changes)

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

    @property
    def is_leased(self) -> Condition[HostLease | None]:
        """A condition on current state: it returns the station's lease while it holds one, and
        `None` before DHCP acknowledges one and once the station lost it."""
        self._station("is_leased", "next_lease()")
        return Condition(lambda: self.lease, "the station has a DHCP lease", "DHCP station")

    @property
    def leases(self) -> Sequence[HostLease]:
        """Every lease the device's DHCP server acknowledged to the station, in order, renewed
        ones after a reset, a rejoin or an expiry included: the station's side of
        `network.dhcp.leases`."""
        return self._station("leases", "leases").leases

    def next_lease(self) -> Condition[HostLease | None]:
        """A condition for the first lease the station gets after this call, the next one in
        `leases`; it consumes nothing."""
        station = self._station("next_lease()", "next_lease()")
        start = len(station.leases)

        def find() -> HostLease | None:
            return station.leases[start] if len(station.leases) > start else None

        return Condition(find, "the station's next DHCP lease", "DHCP station")

    def find_lease(self) -> HostLease | None:
        """The first lease the station got, or `None` before one."""
        leases = self._station("find_lease()", "find_lease()").leases
        return leases[0] if leases else None

    def _station(self, member: str, dhcp: str) -> Station:
        """The station link, or `ValueError` behind another link, which leases no address to the
        harness: behind an access point the harness's DHCP server leases the device its."""
        if isinstance(self.link, Station):
            return self.link
        instead = (
            f"; behind an access point the harness leases the device its address:"
            f" network.dhcp.{dhcp}"
            if isinstance(self.link, Ethernet)
            else ""
        )
        raise ValueError(f"network.{member} requires wifi.station(){instead}")

    def next_link_up(self) -> Condition[LinkChange | None]:
        """A described condition for the first time the link comes up after this call: IPCP
        opening behind a modem, the station joining behind a virtual station. The boundary is the
        changes there are now, `link_changes` at this call; the condition consumes nothing, and
        returns the same change each time it is asked."""
        return self._next_link(True, "network.next_link_up", "wait for network.dhcp.next_lease()")

    def next_link_down(self) -> Condition[LinkChange | None]:
        """A described condition for the first time the link goes down after this call, whatever
        its cause. The boundary is the changes there are now, `link_changes` at this call; the
        condition consumes nothing, and returns the same change each time it is asked."""
        return self._next_link(
            False, "network.next_link_down", "the test's own switches are in wifi.events"
        )

    def _next_link(self, up: bool, call: str, instead: str) -> Condition[LinkChange | None]:
        if self._kind == "ethernet":
            raise ValueError(
                f"{call}: behind a Wi-Fi access point the network does not see the device join"
                f" or leave; {instead}"
            )
        find = FirstFrom(lambda: self.link_changes, len(self.link_changes), lambda c: c.up is up)
        return Condition(find, f"the next link {'up' if up else 'down'}", "network")

    def _link_change(self, up: bool, cause: LinkCause) -> None:
        """Record the link coming up or going down now; nothing when it already is so, or behind
        an access point, where the network sees neither."""
        if self._kind == "ethernet" or up is self._link_up:
            return
        self._link_up = up
        self._link_changes.append(LinkChange(Duration(self.now()), up, cause))

    def _station_changed(self) -> None:
        if self.lease is None:
            self.dns.address = bytes(4)
            self._out.clear()
            self._link_ended()
        else:
            self.dns.address = IPv4Address(self.lease.address).packed
            self.ip.resume(self.lease.address)

    def _carries(self) -> bool:
        """Whether the link carries IP now: on, behind a station with a lease, and the path not
        dropping every datagram. While it does not, what the network's TCP has to send waits."""
        if self._drop:
            return False
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
        self._events.append({"t_ns": self.now(), "event": event})

    def transcript(self) -> dict[str, object]:
        """Application traffic and network events, with no TLS keys: plaintext, but on a bridged
        port that carries TLS, where it is the ciphertext the bridge relays."""
        return {"esp32tl_network": 1, "events": self.events[:]}

    def report(self) -> list[str]:
        lines = ["network events:"]
        for event in self._events:
            detail = event.get("event")
            if detail is None:
                detail = f"stream {event['stream']} {event['direction']} {event['data']} (hex)"
            lines.append(f"  {board_time(cast(int, event['t_ns']))}  {detail}")
        if self.connections:
            lines.append("TCP connections the test opened:")
        for connection in self.connections:
            at = connection.ended_at or connection.established_at
            state = connection.state + (f" at {at}" if at is not None else "")
            why = connection.unanswered()
            lines.append(
                f"  {connection.identity}  {connection.name}  {state}"
                + (f": {why}" if why is not None else "")
            )
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
            self._link_change(False, "device lost its power" if "power" in why else "device reset")
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
        after the path's way down; while the device answers none, it goes again on the board's
        clock (`Network.poll`). `address` is the device's, `device_address` unless given, as
        for a device with a static address behind the access point. Returns the `TcpConnection`.
        Raises `NotReachable` while the link is down, before the device has an address, and
        behind the access point before the station has sent a frame."""
        return self._open(port, address, "connect")

    def accepting(
        self, port: int, *, address: str | None = None
    ) -> Condition[TcpConnection | None]:
        """A condition returning the first connection the device accepts on `port`: it connects
        now, as `connect` does, and again `ACCEPT_RETRY_NS` (1 s) after each refusal, on the
        board's clock, up to `ACCEPT_ATTEMPTS` (30) connections. Each is in `connections`, and
        the one returned is open: the test uses it or closes it. Raises `ConnectionEnded` once a
        connection ended otherwise (lost, timed out) or the device refused all 30, and
        `NotReachable`, at the call as `connect` does, or from the condition when a connection
        again finds the device out of reach."""
        accepting = _Accepting(self, port, address)
        self._accepting.append(accepting)
        return Condition(accepting.find, accepting.description, "TCP", accepting.waiting)

    def _open(self, port: int, address: str | None, name: str) -> TcpConnection:
        call = f"network.{name}({port!r}" + (
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
        if self._drop:
            raise NotReachable(
                f"{call}: the path drops every datagram (network.set_path(drop=True)):"
                " the SYN would reach no device"
            )
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
                    "wait for network.is_leased"
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
            self._events.append(
                {"t_ns": self.now(), "stream": identity, "direction": direction, "data": data.hex()}
            )

        local = self.lease.address if self.lease is not None else LOCAL
        connection = TcpConnection(device, port, local, identity, self.now, record)
        # Kept only once the network has taken it: it raises when it has no port left.
        connection._opened(*self.ip.connect(device, port, local, connection))
        self._opened[port] = number
        self._connections.append(connection)
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
        drop: bool = False,
    ) -> None:
        """The path between the link and the network, IPv4 only: what the device sends at `t` is
        answered at `t + rtt`, plus the time the datagrams take at `uplink` and `downlink`, `None`
        being no limit. With `drop`, the path drops every datagram entering it, either way, and
        the network's TCP holds what it has to send until a call without it. Each call states the
        whole path, and applies to datagrams entering after it; none overtakes another. These are
        the test's parameters, not a model of an operator.
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
        if not isinstance(drop, bool):
            raise TypeError(f"network.set_path: drop is {drop!r}, not True or False")
        if drop and (rtt != DEFAULT_RTT or downlink is not None or uplink is not None):
            raise ValueError(
                "network.set_path: drop=True drops every datagram: it takes no rtt, downlink or"
                " uplink"
            )
        # The floor stays on the way down, so that at the default a service takes a request at the
        # request's own time, and the rest is shared. The two always add up to `rtt`, exactly.
        self._up.delay = (rtt.ns - LATENCY_NS) // 2
        self._down.delay = rtt.ns - self._up.delay
        self._up.rate, self._down.rate = uplink, downlink
        lifted, self._drop = self._drop and not drop, drop
        if drop:
            self.log("net: path: drop every datagram")
            return
        self.log(f"net: path: rtt {rtt}, downlink {downlink or 'any'}, uplink {uplink or 'any'}")
        if lifted:
            # What the network's TCP held for the path goes now, onto it, and the ACKs and
            # windows the device sent into the black hole are asked for again.
            self.ip.resume(probe=True)

    def block(self, port: int, *, how: Block) -> None:
        """Block TCP `port` of the network, on every destination address, until `unblock`:
        `"drop"` swallows the device's segments to it, an established connection seeing nothing
        back, and its service's writes wait; `"refuse"` answers the device's SYN with RST, as when
        nothing listens there, and leaves the connections already open as they are. UDP, DNS and
        NTP included, is not affected. Blocking a blocked port again changes how."""
        call = f"network.block({port!r}, how={how!r})"
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError(f"{call}: the TCP port must be between 1 and 65535")
        if how not in ("drop", "refuse"):
            raise ValueError(f"{call}: how is 'drop' or 'refuse'")
        self.log(f"net: block: TCP {port}, {how}")
        self.ip.block(port, how)

    def unblock(self, port: int) -> None:
        """Lift `block` from TCP `port`: what its connections held goes now."""
        call = f"network.unblock({port!r})"
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError(f"{call}: the TCP port must be between 1 and 65535")
        if port not in self.ip.blocked:
            raise ValueError(f"{call}: TCP port {port} is not blocked")
        self.log(f"net: unblock: TCP {port}")
        self.ip.unblock(port)

    def _dropped(self, datagram: bytes, way: str) -> None:
        """Log a datagram the path dropped, going `way`, `up` or `down`."""
        what = f"{len(datagram)} bytes"
        try:
            parsed = ipv4(datagram)
        except Fragmented:
            parsed = None
        if parsed is not None:
            protocol, src, dst, payload = parsed
            if protocol in (TCP, UDP) and len(payload) >= 4:
                sport, dport = struct.unpack(">HH", payload[:4])
                name = "TCP" if protocol == TCP else "UDP"
                what = f"{name} {src}:{sport} → {dst}:{dport}, {what}"
            else:
                what = f"IPv4 {src} → {dst}, {what}"
        self.log(f"net: path dropped {what} going {way}")

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
        return Ppp(
            send,
            self._uplink,
            log=self.log,
            opened=lambda: self._link_change(True, "IPCP open"),
            ended=self._ppp_ended,
        )

    def _ppp_ended(self, cause: LinkCause) -> None:
        self._link_change(False, cause)
        self._link_ended()

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
        if self._drop:
            self._dropped(datagram, "up")
            return
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
        if self._drop:
            self._dropped(datagram, "down")
            return
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
            # A ready while joined is a join counted again: the station left and joined within the
            # runner's slice, so it is a down and an up at the same instant.
            self._link_change(False, "station left")
            self.link.stop()
            if ready and self.active:
                self._link_change(True, "station joined")
                self.link.start()
        finally:
            self._at = None

    def stop(self, at: int | None = None, cause: LinkCause = "call ended") -> None:
        """The link ends at `at`, the board's current instant unless given: a modem's call, or an
        access point off the air. Behind a modem `link_changes` records `cause` when IPCP was
        open."""
        previous = self._at
        self._at = self._now() if at is None else at
        try:
            self._link_change(False, cause)
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
            if self.ppp.lcp_open:
                self._link_change(False, "far end terminated")
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
        """Take in what has come up the path by `now`, then run the broker's deadlines, the HTTP
        server's delayed responses, the test's SYN timers and the TCP's retransmission timers due by
        it and hand over what bridged services sent, before the test looks at the turn at `now`: a
        record stamped `now`, a PUBACK, an expiry, a delayed response or a SYN sent again at `now`,
        is there when `board.now` says it. What this answers goes out in `poll`. With the link down
        the broker's deadlines run all the same, and `poll` has nothing to send."""
        self._at = now
        try:
            if self.active and isinstance(self.link, Station):
                self.link.poll()
            self._arrive(now)
            self.ip.poll()
            self._retry()
            self.broker.poll()
            self.http.poll()
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
            self.ip.poll()
            self._retry()
            self.broker.poll()
            self.http.poll()
            self._pump()
            return self._take()
        finally:
            self._at = None

    def _retry(self) -> None:
        """Connect again for each `accepting` condition whose last connection was refused
        `ACCEPT_RETRY_NS` ago or more; one that has done with connecting is let go."""
        now = self.now()
        for accepting in list(self._accepting):
            due = accepting.due()
            if due is None:
                if accepting.done():
                    self._accepting.remove(accepting)
            elif due <= now:
                accepting.again()

    def _take(self) -> list[Output]:
        self._release()
        out, self._out = self._out, []
        return out

    def quiet_until(self) -> int | None:
        """The earliest of the broker's deadlines, the HTTP server's next delayed response, the
        next SYN timer of a connection the test opened, the next retransmission timer of a TCP
        connection, the path's next datagram, either way, and the next whole millisecond while a
        bridged connection is open; with the link down, the broker's, the HTTP server's and the
        bridges', as its keepalive still ends the connections the link's end left open, a response
        still goes to them, into nothing, and a bridged service may still send or close."""
        bridged = (bridge.quiet_until(self._now()) for bridge in self.bridges.values())
        # A retry is due whether the link is up or not: one that finds it down says so.
        retries = (accepting.due() for accepting in self._accepting)
        if not self.active:
            deadlines = (self.broker.quiet_until(), self.http.quiet_until(), *bridged, *retries)
            return min((bound for bound in deadlines if bound is not None), default=None)
        bounds = (
            self.link.quiet_until() if isinstance(self.link, Station) else None,
            self.broker.quiet_until(),
            self.http.quiet_until(),
            self.ip.quiet_until(),
            self._up.head(),
            self._down.head(),
            *bridged,
            *retries,
        )
        return min((bound for bound in bounds if bound is not None), default=None)

    def latency(self) -> int:
        return LATENCY_NS


class _Accepting:
    """What `Internet.accepting` returns the condition of: the connections it opened to one port,
    the last of which is the one that counts, and the `NotReachable` a connection again raised."""

    def __init__(self, network: Internet, port: int, address: str | None) -> None:
        self.network, self.port, self.address = network, port, address
        self.attempts = [network._open(port, address, "accepting")]
        self.error: NotReachable | None = None
        # Whether `find` has returned the accepted connection: from then on it returns it whatever
        # becomes of it, as the test's to use or close.
        self.returned = False
        self.description = f"TCP {self.attempts[0].address}:{port} accepting a connection"

    def due(self) -> int | None:
        """When the next connection goes: `ACCEPT_RETRY_NS` after the last was refused, while
        fewer than `ACCEPT_ATTEMPTS` went and none raised; else `None`."""
        last = self.attempts[-1]
        if self.error is not None or last.state != "refused" or last.ended_at is None:
            return None
        if len(self.attempts) >= ACCEPT_ATTEMPTS:
            return None
        return last.ended_at.ns + ACCEPT_RETRY_NS

    def done(self) -> bool:
        """Whether no connection will go again: one was not refused, or the last raised, or all
        were refused."""
        return self.attempts[-1].state != "connecting" or self.error is not None

    def again(self) -> None:
        try:
            self.attempts.append(self.network._open(self.port, self.address, "accepting"))
        except NotReachable as error:
            self.error = error

    def _refusals(self) -> str:
        refused = [c for c in self.attempts if c.state == "refused"]
        if not refused:
            return ""
        last = refused[-1].ended_at
        count = "1 connection" if len(refused) == 1 else f"{len(refused)} connections"
        return f"the device refused {count}, the last at {last}"

    def find(self) -> TcpConnection | None:
        last = self.attempts[-1]
        if last.established_at is not None:
            if not self.returned and (last.state != "established" or last.closed_at is not None):
                # Accepted, then ended or closed by the device before a poll saw it: not open.
                how = last._how if last.state != "established" else "closed by the device"
                raise ConnectionEnded(
                    f"{last.name} was accepted, then {how}, before {self.description} returned it"
                )
            self.returned = True
            return last
        if self.error is not None:
            raise self.error
        if last.state == "refused":
            if len(self.attempts) >= ACCEPT_ATTEMPTS:
                raise ConnectionEnded(f"{self.description}: {self._refusals()}, and no more go")
            return None
        # Lost or timed out: it raises as `established` does.
        return None if last.established() is None else last

    def waiting(self) -> str | None:
        last = self.attempts[-1]
        refused = self._refusals()
        if last.state == "refused":
            due = self.due()
            return refused + (f"; the next goes at {Duration(due)}" if due is not None else "")
        unanswered = last.unanswered()
        if unanswered is None:
            return refused or None
        return f"{refused}; {unanswered}" if refused else unanswered


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
            self.network._events.append(
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
