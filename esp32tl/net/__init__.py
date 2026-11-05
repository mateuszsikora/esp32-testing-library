"""The simulated network behind a modem or a Wi-Fi access point, driven only by the board's
turns."""

import math
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal, cast, overload

from esp32tl.link.boardlink import Output
from esp32tl.net.dhcp import Dhcp
from esp32tl.net.dns import Dns
from esp32tl.net.ethernet import Ethernet
from esp32tl.net.http import HttpServer
from esp32tl.net.ip import Connection, Network, StreamService
from esp32tl.net.mqtt import Broker
from esp32tl.net.ntp import Ntp
from esp32tl.net.ppp import Ppp
from esp32tl.net.tls import CertificateAuthority, Tls
from esp32tl.report import board_time
from esp32tl.units import NS_PER_S, Duration, Rate, shown

LATENCY_NS = 1_000_000
DEFAULT_RTT = Duration(LATENCY_NS)

Link = Literal["ppp", "ethernet"]


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
    """One per modem, over PPP, or per Wi-Fi access point, over Ethernet with ARP and DHCP. DNS
    and NTP always listen, the broker on TCP 1883 and the HTTP server on 80; the test registers
    other TCP services by port.

    Responses are stamped 1 ms after the request's UART chunk or Ethernet frame, or the path's RTT
    after it (`set_path`). Public calls run while the board is held, and callbacks while it
    advances: neither uses host time or sockets.
    """

    def __init__(
        self,
        now: Callable[[], int],
        world: Callable[[], datetime] = lambda: datetime(2026, 1, 1, tzinfo=UTC),
        *,
        link: Link = "ppp",
    ) -> None:
        self._now = now
        self._at: int | None = None
        self._out: list[Output] = []
        self._kind = link
        self._up = _Direction(0)
        self._down = _Direction(LATENCY_NS)
        self._datagram: int | None = None  # the size of the IPv4 datagram the link is sending
        self.events: list[dict[str, object]] = []
        self.ip = Network(self._downlink, self.log, mtu=self._mtu)
        self.dns = Dns("10.64.0.1", self.log)
        self.ntp = Ntp(self.now, world, self.log)
        self.ip.udp.update({53: self.dns, 123: self.ntp})
        self.broker = Broker(self.now, self.log)
        self.listen(1883, self.broker)
        self.http = HttpServer(self.now, self.log)
        self.listen(80, self.http)
        # Serves behind an access point only; its leases outlive the link's restarts.
        self.dhcp = Dhcp(self.now, self.log)
        self.link: Ppp | Ethernet = self._peer()
        self.active = False

    @property
    def ppp(self) -> Ppp:
        """The PPP link of a network behind a modem."""
        if not isinstance(self.link, Ppp):
            raise AttributeError("this network is behind a Wi-Fi access point: it has no PPP")
        return self.link

    @property
    def ethernet(self) -> Ethernet:
        """The Ethernet of a network behind a Wi-Fi access point."""
        if not isinstance(self.link, Ethernet):
            raise AttributeError("this network is behind a modem: it has no Ethernet")
        return self.link

    def _mtu(self) -> int:
        return self.link.peer_mru if isinstance(self.link, Ppp) else self.link.mtu

    def now(self) -> int:
        return self._now() if self._at is None else self._at

    def log(self, event: str) -> None:
        self.events.append({"t_ns": self.now(), "event": event})

    def transcript(self) -> dict[str, object]:
        """Plain application traffic and network events, with no TLS keys or ciphertext."""
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
        traced = _Traced(self, port, service)
        if tls is not None:
            listener = Tls(traced, tls, self.log)
            self.ip.tcp[port] = listener
            return listener
        self.ip.tcp[port] = traced
        return None

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

    def _peer(self) -> Ppp | Ethernet:
        def send(data: bytes) -> None:
            if self._datagram is None:  # the link's own: LCP, IPCP, ARP and DHCP
                self._out.append((self.now() + LATENCY_NS, data))
            else:
                leaves = self._down.leaves(self.now(), self._datagram)
                self._down.queue.append((leaves, data))
                self._release()

        if self._kind == "ethernet":
            return Ethernet(send, self._uplink, self.dhcp, log=self.log)
        return Ppp(send, self._uplink, log=self.log, ended=self._link_ended)

    def _link_ended(self) -> None:
        self.ip.reset()
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

    def stop(self, at: int | None = None) -> None:
        previous = self._at
        self._at = self._now() if at is None else at
        try:
            self.active = False
            self._link_ended()
            self._out.clear()
            self.link = self._peer()
        finally:
            self._at = previous

    def receive(self, t: int, data: bytes) -> list[Output]:
        """What the device sent at `t`: bytes of the modem's line, or one Ethernet frame."""
        self._at = t
        try:
            self._arrive(t)
            self.link.receive(data)
            return self._take()
        finally:
            self._at = None

    def arrive(self, now: int) -> None:
        """Take in what has come up the path by `now`, before the test looks at the turn at
        `now`: a record stamped `now` is there when `board.now` says it. What this answers goes out
        in `poll`."""
        self._at = now
        try:
            self._arrive(now)
        finally:
            self._at = None

    def poll(self, now: int) -> list[Output]:
        self._at = now
        try:
            self._arrive(now)
            self.broker.poll()
            return self._take()
        finally:
            self._at = None

    def _take(self) -> list[Output]:
        self._release()
        out, self._out = self._out, []
        return out

    def quiet_until(self) -> int | None:
        """The earliest of the broker's deadlines and the path's next datagram, either way."""
        if not self.active:
            return None
        bounds = (self.broker.quiet_until(), self._up.head(), self._down.head())
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
