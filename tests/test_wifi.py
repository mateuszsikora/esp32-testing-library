"""The `wifi` interface without the emulator: what it asks of the backend, what it gives the
runner, and what it sends on the link."""

from collections.abc import Callable

import pytest

from esp32tl.boards.capabilities import Request
from esp32tl.interfaces import SourceError
from esp32tl.interfaces.wifi import Wifi
from esp32tl.link.protocol import WifiAp

MS = 1_000_000


class Clock:
    def __init__(self) -> None:
        self.now = 0


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def asked() -> list[Request]:
    return []


@pytest.fixture
def wifi(clock: Clock, asked: list[Request]) -> Wifi:
    return Wifi(now=lambda: clock.now, asked=asked.append)


def test_the_access_point_and_the_networks_around_become_the_runners_flags(
    wifi: Wifi, asked: list[Request]
) -> None:
    assert wifi.runner_args() == [] and wifi.latency() is None
    network = wifi.access_point("myssid", password="mypassword", channel=11)
    wifi.nearby("cafe", channel=3, rssi=-70, secure=False)
    wifi.nearby("office", bssid="02:00:00:00:00:09")
    assert wifi.runner_args() == [
        "--wifi",
        "ssid=myssid,chan=11,psk=mypassword",
        "--wifi-nearby",
        "ssid=cafe,chan=3,rssi=-70,secure=0",
        "--wifi-nearby",
        "ssid=office,chan=1,rssi=-60,secure=1,bssid=02:00:00:00:00:09",
    ]
    assert asked == [Request("wifi", source="model")] * 3
    assert wifi.network is network and network.active and wifi.on_air
    assert network.dhcp.device == "10.64.0.2" and wifi.latency() == MS
    open_ = Wifi(now=lambda: 0)
    open_.access_point("guest", bssid="02:53:49:4d:00:07")
    assert open_.runner_args() == ["--wifi", "ssid=guest,chan=6,bssid=02:53:49:4d:00:07"]


@pytest.mark.parametrize(
    ("call", "error"),
    [
        (lambda w: w.access_point(""), "an SSID has 1 to 32 bytes"),
        (lambda w: w.access_point("x" * 33), "an SSID has 1 to 32 bytes"),
        (lambda w: w.access_point("a,b"), "a comma cannot reach"),
        (lambda w: w.access_point("x", password="short"), "8 to 63 bytes"),
        (lambda w: w.access_point("x", password="p" * 64), "8 to 63 bytes"),
        (lambda w: w.access_point("x", channel=15), "channel 15 is not 1 to 14"),
        (lambda w: w.access_point("x", channel=True), "channel True is not 1 to 14"),
        (lambda w: w.access_point("x", bssid="02:00:00:00:00"), "not six hexadecimal octets"),
        (lambda w: w.nearby("y", rssi=-129), "rssi -129 is not -128 to 127 dBm"),
    ],
)
def test_what_esp32sim_cannot_take_is_refused(
    wifi: Wifi, call: Callable[[Wifi], object], error: str
) -> None:
    with pytest.raises(ValueError, match=error):
        call(wifi)


def test_one_access_point_and_networks_around_it_that_are_not_it(wifi: Wifi) -> None:
    with pytest.raises(SourceError, match=r"need wifi\.access_point"):
        wifi.nearby("cafe")
    with pytest.raises(SourceError, match="no access point"):
        _ = wifi.network
    wifi.access_point("home", bssid="02:00:00:00:00:01")
    with pytest.raises(SourceError, match="one per test"):
        wifi.access_point("other")
    with pytest.raises(ValueError, match="would be the access point"):
        wifi.nearby("home")
    with pytest.raises(ValueError, match="would be the access point"):
        wifi.nearby("cafe", bssid="02:00:00:00:00:01")
    wifi.nearby("cafe")
    with pytest.raises(ValueError, match="around already"):
        wifi.nearby("cafe")


def test_a_backend_that_refuses_the_access_point_leaves_nothing_behind() -> None:
    def refuse(request: Request) -> None:
        raise SourceError("refused")

    wifi = Wifi(now=lambda: 0, asked=refuse)
    with pytest.raises(SourceError, match="refused"):
        wifi.access_point("home")
    assert wifi.configuration is None and wifi.runner_args() == []


def test_switches_go_out_at_the_instant_the_test_made_them(wifi: Wifi, clock: Clock) -> None:
    with pytest.raises(SourceError, match="no access point"):
        wifi.access_point_off()
    network = wifi.access_point("home")
    clock.now = 2 * MS
    wifi.access_point_off()
    wifi.access_point_off()  # already off: no switch
    assert not wifi.on_air and not network.active
    # off the air, the station's frames reach nothing
    assert list(wifi.receive(3 * MS, "out", bytes(14))) == []
    clock.now = 4 * MS
    wifi.access_point_on()
    assert list(wifi.inputs(5 * MS)) == [WifiAp(5 * MS, False), WifiAp(5 * MS, True)]
    assert list(wifi.inputs(6 * MS)) == []
    assert [(e.t, e.on) for e in wifi.events] == [(2 * MS, False), (4 * MS, True)]
    assert [e["event"] for e in network.events] == [
        "wifi: access point 'home' on the air, channel 6",
        "wifi: access point off the air",
        "wifi: access point back on the air",
    ]
    properties = wifi.properties()
    assert properties is not None
    assert properties["switches"] == [{"t_ns": 2 * MS, "on": False}, {"t_ns": 4 * MS, "on": True}]
