"""The `wifi` interface without the emulator: what it asks of the backend, what it gives the
runner, and what it sends on the link."""

import re
import struct
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from pytest_embedded.dut import Dut
from pytest_embedded.log import MessageQueue
from pytest_embedded.utils import Meta

from esp32tl import boards
from esp32tl.backends import esp32sim
from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.boards import CHIPS
from esp32tl.boards.capabilities import Request
from esp32tl.build import ESP_IDF, Build
from esp32tl.interfaces import SourceError
from esp32tl.interfaces.wifi import EMULATED_MACS, Wifi, emulated_macs
from esp32tl.link.protocol import WifiAp
from esp32tl.net.ntp import DEFAULT_UTC, NTP_EPOCH
from esp32tl.net.station import DEFAULT_MAC

ROOT = Path(__file__).resolve().parent.parent

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


def test_ntp_behind_an_access_point_tells_the_default_world() -> None:
    wifi = Wifi(now=lambda: 0)
    wifi.access_point("home")
    answer = wifi.network.ntp.on_datagram(b"\x23" + bytes(47))
    assert answer is not None
    told = struct.unpack(">I", answer[40:44])[0]
    assert NTP_EPOCH + timedelta(seconds=told) == DEFAULT_UTC


def test_station_flags_properties_and_exclusions(wifi: Wifi, asked: list[Request]) -> None:
    network = wifi.station("setup", password="password1")
    assert wifi.runner_args() == ["--wifi-sta", "ssid=setup,mac=02:53:54:41:00:01,psk=password1"]
    assert asked == [Request("wifi", source="model", role="station")]
    assert wifi.configuration is not None and wifi.network is network
    properties = wifi.properties()
    assert properties is not None
    assert properties["station"] == {"ssid": "setup", "mac": "02:53:54:41:00:01", "secure": True}
    calls: list[tuple[Callable[[], object], str]] = [
        (lambda: wifi.station("again"), "station already"),
        (lambda: wifi.access_point("ap"), "exclusive"),
        (lambda: wifi.nearby("near"), "requires a virtual access point"),
        (wifi.access_point_off, "no virtual access point"),
        (wifi.access_point_on, "no virtual access point"),
    ]
    for call, why in calls:
        with pytest.raises(SourceError, match=why):
            call()
    other = Wifi(now=lambda: 0)
    other.access_point("ap")
    with pytest.raises(SourceError, match="exclusive"):
        other.station("setup")
    other.nearby("near")
    with pytest.raises(SourceError, match="exclusive"):
        other.station("setup")


@pytest.mark.parametrize(
    "kwargs,why",
    [
        ({"ssid": ""}, "1 to 32 bytes"),
        ({"ssid": "a,b"}, "comma"),
        ({"ssid": "x" * 33}, "1 to 32 bytes"),
        ({"ssid": "x", "password": "short"}, "8 to 63 ASCII"),
        ({"ssid": "x", "mac": "ff:00:00:00:00:01"}, "unicast"),
        ({"ssid": "x", "mac": "bad"}, "six hexadecimal"),
    ],
)
def test_station_validation(kwargs: dict[str, str], why: str) -> None:
    with pytest.raises(ValueError, match=why):
        Wifi(now=lambda: 0).station(**kwargs)


# How many universally administered MACs ESP-IDF gives each chip with Wi-Fi by default:
# `CONFIG_<CHIP>_UNIVERSAL_MAC_ADDRESSES` in `components/esp_hw_support/port/<chip>/Kconfig.mac`,
# the same in v5.3.6, v5.5.4 and v6.1 (the S31's only in v6.1).
UNIVERSAL_MACS = {"esp32": 4, "esp32s3": 4, "esp32c3": 4, "esp32c6": 4, "esp32s31": 2}


def runner_mac(chip: str) -> bytes:
    """`MAC` in `runner/src/chip/<chip>.rs`, the base MAC the runner gives the chip's machine."""
    source = (ROOT / "runner" / "src" / "chip" / f"{chip}.rs").read_text()
    match = re.search(r"const MAC: \[u8; 6\] = \[([^\]]*)\];", source)
    assert match is not None, chip
    return bytes(int(octet, 16) for octet in match[1].split(","))


def esp_idf_softap(base: bytes, universal: int) -> bytes:
    """`esp_read_mac(…, ESP_MAC_WIFI_SOFTAP)` in ESP-IDF's `mac_addr.c`: with four universal MACs
    the base with one added to its last octet, which does not carry; with two
    `esp_derive_local_mac`, the base with its locally administered bit set (or, on a base that
    has it already, bit 2 of its first octet flipped)."""
    if universal == 4:
        return base[:5] + bytes([(base[5] + 1) & 0xFF])
    first = base[0] | 0x02
    if first == base[0]:
        first ^= 0x04
    return bytes([first]) + base[1:]


def test_every_chip_esp32tl_runs_has_its_macs_or_none() -> None:
    assert set(EMULATED_MACS) == set(CHIPS)
    assert [chip for chip, macs in EMULATED_MACS.items() if macs is None] == ["esp32p4"]


@pytest.mark.parametrize("chip", sorted(UNIVERSAL_MACS))
def test_the_device_s_macs_are_the_runner_s_base_mac_and_the_softap_mac_esp_idf_derives(
    chip: str,
) -> None:
    base = runner_mac(chip)
    macs = emulated_macs(chip)
    assert macs == EMULATED_MACS[chip]
    assert macs.station == base.hex(":")
    assert macs.softap == esp_idf_softap(base, UNIVERSAL_MACS[chip]).hex(":")
    # The virtual station's default is neither of them.
    assert DEFAULT_MAC not in (macs.station, macs.softap)


def test_esp_idf_s_derivations_on_bases_at_their_edges() -> None:
    assert esp_idf_softap(bytes.fromhex("2462ab0000ff"), 4).hex() == "2462ab000000"
    assert esp_idf_softap(bytes.fromhex("102030405060"), 2).hex() == "122030405060"
    assert esp_idf_softap(bytes.fromhex("122030405060"), 2).hex() == "162030405060"


@pytest.mark.parametrize(
    "chip,why",
    [
        ("esp32p4", "^emulated_macs: the ESP32-P4 has no Wi-Fi$"),
        ("esp32s2", "^emulated_macs: 'esp32s2' is not one of esp32, esp32s3, esp32c3"),
    ],
)
def test_a_chip_without_wifi_or_unknown_has_no_emulated_macs(chip: str, why: str) -> None:
    with pytest.raises(ValueError, match=why):
        emulated_macs(chip)


@pytest.mark.parametrize("chip", sorted(UNIVERSAL_MACS))
@pytest.mark.parametrize("which", ["station", "softap"])
def test_the_virtual_station_refuses_the_board_s_chip_s_macs(chip: str, which: str) -> None:
    macs = emulated_macs(chip)
    mac = getattr(macs, which)
    with pytest.raises(ValueError) as refused:
        Wifi(now=lambda: 0, chip=chip).station("x", mac=mac.upper())
    assert str(refused.value) == (
        "wifi.station: mac must differ from the device's station and SoftAP MACs,"
        f" {macs.station} and {macs.softap}"
    )


def test_the_virtual_station_takes_another_chip_s_macs_and_any_without_a_chip() -> None:
    classic = emulated_macs("esp32")
    Wifi(now=lambda: 0, chip="esp32s3").station("x", mac=classic.station)
    Wifi(now=lambda: 0).station("x", mac=classic.softap)
    Wifi(now=lambda: 0, chip="esp32p4").station("x", mac=classic.station)


def test_a_wifi_on_a_chip_the_table_does_not_name_is_refused() -> None:
    with pytest.raises(KeyError, match="esp32s2"):
        Wifi(now=lambda: 0, chip="esp32s2")


def test_the_emulated_board_s_wifi_refuses_its_own_chip_s_macs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = Build(
        directory=tmp_path,
        kind=ESP_IDF,
        flash_files=(),
        partition_table=None,
        app_image=None,
        elf=None,
        chip="esp32s3",
    )
    monkeypatch.setattr(esp32sim, "load_app", lambda _, *, build_dir=None, chip="esp32": app)
    monkeypatch.setattr(esp32sim, "runner_path", lambda: Path("runner"))
    monkeypatch.setattr(esp32sim, "rom_path", lambda chip="esp32", app=None: Path("rom"))
    run = tmp_path / "run"
    run.mkdir()
    board = Esp32Sim(
        Mock(spec=Dut, logfile=str(run / "dut.log")),
        "app",
        Mock(spec=MessageQueue),
        Meta(str(run), {}, {}),
        boards.profile("esp32-s3-devkitc"),
    )
    with pytest.raises(ValueError, match=r"44:1b:f6:75:dc:e0 and 44:1b:f6:75:dc:e1$"):
        board.wifi.station("x", mac="44:1b:f6:75:dc:e1")
