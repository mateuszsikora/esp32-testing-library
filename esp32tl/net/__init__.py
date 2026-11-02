"""The simulated network behind a modem or a Wi-Fi access point, driven only by the board's
turns."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal, cast

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

LATENCY_NS = 1_000_000

Link = Literal["ppp", "ethernet"]


class Internet:
    """One per modem, over PPP, or per Wi-Fi access point, over Ethernet with ARP and DHCP. DNS
    and NTP always listen, the broker on TCP 1883 and the HTTP server on 80; the test registers
    other TCP services by port.

    Responses are stamped 1 ms after the request's UART chunk or Ethernet frame. Public calls run
    while the board is held, and callbacks while it advances: neither uses host time or sockets.
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
        self.events: list[dict[str, object]] = []
        self.ip = Network(lambda data: self.link.send_ip(data), self.log, mtu=self._mtu)
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

    def listen(
        self, port: int, service: StreamService, *, tls: CertificateAuthority | None = None
    ) -> None:
        """Install a TCP service on any destination's port; optionally terminate TLS first."""
        if not 1 <= port <= 65535:
            raise ValueError("TCP port must be between 1 and 65535")
        if port in self.ip.tcp:
            raise ValueError(f"TCP port {port} already has a service")
        traced = _Traced(self, port, service)
        self.ip.tcp[port] = Tls(traced, tls, self.log) if tls is not None else traced

    def _peer(self) -> Ppp | Ethernet:
        def send(data: bytes) -> None:
            self._out.append((self.now() + LATENCY_NS, data))

        if self._kind == "ethernet":
            return Ethernet(send, self.ip.receive, self.dhcp, log=self.log)
        return Ppp(send, self.ip.receive, log=self.log, ended=self.ip.reset)

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
            self.ip.reset()
            self._out.clear()
            self.link = self._peer()
        finally:
            self._at = previous

    def receive(self, t: int, data: bytes) -> list[Output]:
        """What the device sent at `t`: bytes of the modem's line, or one Ethernet frame."""
        self._at = t
        try:
            self.link.receive(data)
            return self._take()
        finally:
            self._at = None

    def poll(self, now: int) -> list[Output]:
        self._at = now
        try:
            self.broker.poll()
            return self._take()
        finally:
            self._at = None

    def _take(self) -> list[Output]:
        out, self._out = self._out, []
        return out

    def quiet_until(self) -> int | None:
        return self.broker.quiet_until() if self.active else None

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
