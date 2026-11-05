"""The chip's Wi-Fi station, `wifi`: the access point it can join, the networks around it, and the
simulated network behind the access point. The `wifi` fixture is it.

On the `esp32sim` backend the access point is esp32sim's, in the runner (`--wifi`), and the
networks around are its beacon-only ones (`--wifi-nearby`). What the station sends through the
access point reaches the harness as Ethernet frames on the link, where `esp32tl.net.Internet`
answers them on the board's clock; the test switches the access point off the air and back at the
board's current instant.
"""

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from esp32tl.boards.capabilities import Request
from esp32tl.interfaces import SourceError
from esp32tl.link.boardlink import Direction, Interface, Output
from esp32tl.link.protocol import Input, WifiAp
from esp32tl.net import LATENCY_NS, Internet
from esp32tl.report import RunLog

NAME = "wifi"


@dataclass(frozen=True)
class AccessPoint:
    """The access point the station can join: open without a `password`, WPA2-PSK with one."""

    ssid: str
    password: str | None
    channel: int
    bssid: str | None

    def spec(self) -> str:
        """`--wifi`'s value."""
        spec = f"ssid={self.ssid},chan={self.channel}"
        if self.password is not None:
            spec += f",psk={self.password}"
        if self.bssid is not None:
            spec += f",bssid={self.bssid}"
        return spec


@dataclass(frozen=True)
class Nearby:
    """A network around the station that a scan lists and nothing can join."""

    ssid: str
    channel: int
    rssi: int
    secure: bool
    bssid: str | None

    def spec(self) -> str:
        """`--wifi-nearby`'s value."""
        spec = f"ssid={self.ssid},chan={self.channel},rssi={self.rssi},secure={int(self.secure)}"
        return spec + (f",bssid={self.bssid}" if self.bssid is not None else "")


@dataclass(frozen=True)
class AirEvent:
    """The access point going off the air (`on` false) or back on it, at the board's `t`."""

    t: int
    on: bool


class Wifi(Interface):
    """The station's air, as the link drives it. `access_point(...)` before power-on puts an
    access point on the air from power-on and returns the network behind it; `nearby(...)` adds a
    network around. Until the test gives an access point there is none, and the station finds
    nothing to join. `now` is the board's clock, `world` the world's time at power-on, which the
    network's NTP tells. `asked` is called with the request before the access point is made, and
    refuses it by raising, as for a UART's source."""

    def __init__(
        self,
        *,
        now: Callable[[], int],
        world: Callable[[], datetime] = lambda: datetime(2026, 1, 1, tzinfo=UTC),
        asked: Callable[[Request], None] = lambda request: None,
        log: RunLog | None = None,
    ) -> None:
        super().__init__(NAME, None)
        self._now = now
        self._world = world
        self.asked = asked
        self.log = log
        self._lock = threading.Lock()
        self._access_point: AccessPoint | None = None
        self._network: Internet | None = None
        self._nearby: list[Nearby] = []
        self._on = False
        # The switches the test asked for and the link has not sent.
        self._pending: list[AirEvent] = []
        self.events: list[AirEvent] = []

    @property
    def ethernet(self) -> bool:
        return True

    # The test's side.

    def access_point(
        self,
        ssid: str,
        *,
        password: str | None = None,
        channel: int = 6,
        bssid: str | None = None,
    ) -> Internet:
        """Puts an access point on the air from power-on: open, or WPA2-PSK with `password` (8
        to 63 bytes), on `channel` (1 to 14), at `bssid` (`02:53:49:4d:00:01` unless
        given). Returns the simulated network behind it, which gives the station its address by
        DHCP. Once per test, before `board.power_on()`."""
        _ssid(ssid, "wifi.access_point")
        if password is not None:
            _text(password, "wifi.access_point: password")
            if not 8 <= len(password.encode()) <= 63:
                raise ValueError("wifi.access_point: a WPA2 password has 8 to 63 bytes")
        _channel(channel, "wifi.access_point")
        _bssid(bssid, "wifi.access_point")
        with self._lock:
            if self._access_point is not None:
                raise SourceError("wifi has its access point already: one per test")
        self.asked(Request(NAME, source="model"))
        if any(n.ssid == ssid or (bssid and n.bssid == bssid) for n in self._nearby):
            raise ValueError(f"wifi.access_point: {ssid!r} is a network around already")
        network = Internet(self._now, self._world, link="ethernet")
        with self._lock:
            if self._access_point is not None:
                raise SourceError("wifi has its access point already: one per test")
            self._access_point = AccessPoint(ssid, password, channel, bssid)
            self._network = network
            self._on = True
        network.start(0)
        network.log(f"wifi: access point {ssid!r} on the air, channel {channel}")
        if self.log is not None:
            self.log.test(0, f"wifi.access_point({ssid!r}, channel={channel})")
        return network

    def nearby(
        self,
        ssid: str,
        *,
        channel: int = 1,
        rssi: int = -60,
        secure: bool = True,
        bssid: str | None = None,
    ) -> None:
        """A network around the station, which a scan lists on `channel` at `rssi` dBm, WPA2-PSK
        when `secure`, and which nothing can join. Before `board.power_on()`, and after
        `access_point`: the networks around are on its air."""
        _ssid(ssid, "wifi.nearby")
        _channel(channel, "wifi.nearby")
        _bssid(bssid, "wifi.nearby")
        if isinstance(rssi, bool) or not isinstance(rssi, int) or not -128 <= rssi <= 127:
            raise ValueError(f"wifi.nearby: rssi {rssi!r} is not -128 to 127 dBm")
        self.asked(Request(NAME, source="model"))
        with self._lock:
            ap = self._access_point
            if ap is None:
                raise SourceError("wifi.nearby: the networks around need wifi.access_point() first")
            if ssid == ap.ssid or (bssid is not None and bssid == ap.bssid):
                raise ValueError(f"wifi.nearby: {ssid!r} would be the access point")
            if any(n.ssid == ssid and n.bssid == bssid for n in self._nearby):
                raise ValueError(f"wifi.nearby: {ssid!r} is around already")
            if len(self._nearby) >= 32:
                raise ValueError("wifi.nearby: at most 32 networks around")
            self._nearby.append(Nearby(ssid, channel, rssi, bool(secure), bssid))
        if self.log is not None:
            self.log.test(0, f"wifi.nearby({ssid!r}, channel={channel}, rssi={rssi})")

    def access_point_off(self) -> None:
        """Takes the access point, and the networks around, off the air from the board's current
        instant on: the station hears no beacon and finds out as on a real network. The network
        behind it loses its connections."""
        self._switch(False, "wifi.access_point_off()")

    def access_point_on(self) -> None:
        """Puts the access point back on the air from the board's current instant on, as a fresh
        one: the station must join it again."""
        self._switch(True, "wifi.access_point_on()")

    @property
    def network(self) -> Internet:
        """The simulated network behind the access point."""
        if self._network is None:
            raise SourceError("wifi has no access point: give it with wifi.access_point()")
        return self._network

    @property
    def on_air(self) -> bool:
        """Whether the access point is on the air."""
        return self._on

    @property
    def configuration(self) -> AccessPoint | None:
        """The access point the test gave, if any."""
        return self._access_point

    @property
    def around(self) -> tuple[Nearby, ...]:
        """The networks around the station."""
        return tuple(self._nearby)

    def runner_args(self) -> list[str]:
        """The runner's `--wifi` and `--wifi-nearby`, none without an access point."""
        if self._access_point is None:
            return []
        args = ["--wifi", self._access_point.spec()]
        for nearby in self._nearby:
            args += ["--wifi-nearby", nearby.spec()]
        return args

    def _switch(self, on: bool, step: str) -> None:
        network = self.network
        now = self._now()
        if self.log is not None:
            self.log.test(now, step)
        with self._lock:
            if self._on == on:
                return
            self._on = on
            event = AirEvent(now, on)
            self._pending.append(event)
            self.events.append(event)
        if on:
            network.start(now)
            network.log("wifi: access point back on the air")
        else:
            network.stop(now)
            network.log("wifi: access point off the air")

    # The link's side.

    def receive(self, t: int, direction: Direction, data: bytes) -> Iterable[Output]:
        network = self._network
        if network is None or not network.active:
            return ()
        return network.receive(t, data)

    def arrive(self, now: int) -> None:
        network = self._network
        if network is not None and network.active:
            network.arrive(now)

    def poll(self, now: int) -> Iterable[Output]:
        network = self._network
        if network is None or not network.active:
            return ()
        return network.poll(now)

    def inputs(self, now: int) -> Iterable[Input]:
        with self._lock:
            pending, self._pending = self._pending, []
        return [WifiAp(max(event.t, now), event.on) for event in pending]

    def quiet_until(self) -> int | None:
        network = self._network
        return None if network is None else network.quiet_until()

    def latency(self) -> int | None:
        return None if self._network is None else LATENCY_NS

    def report(self) -> list[str]:
        """The access point, the networks around, the switches and the network's events."""
        ap = self._access_point
        if ap is None:
            return ["wifi: no access point"]
        security = "WPA2-PSK" if ap.password is not None else "open"
        lines = [f"wifi: access point {ap.ssid!r}, channel {ap.channel}, {security}"]
        lines += [
            f"  around: {n.ssid!r}, channel {n.channel}, {n.rssi} dBm,"
            f" {'WPA2-PSK' if n.secure else 'open'}"
            for n in self._nearby
        ]
        return lines + self.network.report()

    def properties(self) -> dict[str, object] | None:
        ap = self._access_point
        if ap is None:
            return None
        return {
            "access_point": {
                "ssid": ap.ssid,
                "channel": ap.channel,
                "secure": ap.password is not None,
            },
            "nearby": [
                {"ssid": n.ssid, "channel": n.channel, "rssi": n.rssi, "secure": n.secure}
                for n in self._nearby
            ],
            "switches": [{"t_ns": e.t, "on": e.on} for e in self.events],
            "network": self.network.transcript(),
        }


def _text(value: object, what: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{what} is {value!r}, not text")
    # esp32sim's spec is key=value pairs between commas
    if "," in value:
        raise ValueError(f"{what} {value!r}: a comma cannot reach esp32sim's access point")


def _ssid(ssid: str, what: str) -> None:
    _text(ssid, f"{what}: ssid")
    if not 1 <= len(ssid.encode()) <= 32:
        raise ValueError(f"{what}: an SSID has 1 to 32 bytes, {ssid!r} has {len(ssid.encode())}")


def _channel(channel: int, what: str) -> None:
    if isinstance(channel, bool) or not isinstance(channel, int) or not 1 <= channel <= 14:
        raise ValueError(f"{what}: channel {channel!r} is not 1 to 14")


def _bssid(bssid: str | None, what: str) -> None:
    if bssid is None:
        return
    parts = bssid.split(":") if isinstance(bssid, str) else []
    if len(parts) != 6 or not all(
        len(p) == 2 and all(c in "0123456789abcdefABCDEF" for c in p) for p in parts
    ):
        raise ValueError(f"{what}: bssid {bssid!r} is not six hexadecimal octets")
