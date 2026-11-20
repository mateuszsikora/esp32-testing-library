"""The Arduino SoftAP, reached through the virtual station with raw TCP and HTTP."""

import json
from pathlib import Path
from typing import cast

import pytest

from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.flash import u32
from esp32tl.interfaces import NeverHappened
from esp32tl.interfaces.wifi import Wifi
from esp32tl.net.station import Station
from esp32tl.units import seconds

pytestmark = pytest.mark.app_path("../firmware/arduino_shell", build_dir=".pio/build/esp32dev")
SSID, PASSWORD = "device-setup", "provisioning"
MAC = "02:53:54:41:00:09"


@pytest.mark.parametrize("password", [None, PASSWORD], ids=["open", "wpa2"])
def test_station_gets_status(
    board: Esp32Sim, wifi: Wifi, password: str | None, tmp_path: Path
) -> None:
    board.nvs({"shell": {"ap_ssid": SSID, "ap_password": password or ""}})
    network = wifi.station(SSID, password=password, mac=MAC)
    board.power_on()
    lease = board.wait_until(network.is_leased, within=seconds(3))
    assert (lease.address, lease.device_address) == ("192.168.4.2", "192.168.4.1")
    exchange = network.http_client.get("/status")
    response = board.wait_until(exchange.response, within=seconds(2))
    assert response.status == 200 and response.json()["status"] == "ok"
    assert response.json()["requests"] == 1
    board.console.expect_exact("arduino_shell: access point device-setup at 192.168.4.1")
    board.console.expect_exact(f"arduino_shell: station {MAC} joined")
    board.console.expect_exact("arduino_shell: GET /status -> 200")
    assert board.link.error is None
    (tmp_path / "network.json").write_text(json.dumps(network.transcript(), sort_keys=True))
    (tmp_path / "lease.txt").write_text(str(lease))


def test_wrong_password_times_out_without_breaking_the_runner(board: Esp32Sim, wifi: Wifi) -> None:
    board.nvs({"shell": {"ap_ssid": SSID, "ap_password": PASSWORD}})
    network = wifi.station(SSID, password="wrong-password")
    board.power_on()
    with pytest.raises(NeverHappened, match="the station has a DHCP lease"):
        board.wait_until(network.is_leased, within=seconds(3))
    assert network.lease is None and board.link.error is None
    assert not [e for e in network.events if str(e["event"]).startswith("dhcp station:")]


def test_raw_tcp_to_the_softap(board: Esp32Sim, wifi: Wifi) -> None:
    board.nvs({"shell": {"ap_ssid": SSID}})
    network = wifi.station(SSID)
    board.power_on()
    board.wait_until(network.is_leased, within=seconds(3))
    connection = network.connect(80)
    connection.write(b"GET /status HTTP/1.0\r\nHost: 192.168.4.1\r\n\r\n")
    board.wait_until(connection.closed, within=seconds(2))
    assert b"200 OK\r\n" in connection.received
    assert b'"status":"ok"' in connection.received
    board.console.sendline("wifi")
    board.console.expect_exact("arduino_shell: wifi=access-point ip=192.168.4.1")


@pytest.mark.parametrize("power_cycle", [False, True], ids=["reset", "power-cycle"])
def test_station_reacquires_after_device_reset(
    board: Esp32Sim, wifi: Wifi, power_cycle: bool
) -> None:
    board.nvs({"shell": {"ap_ssid": SSID, "ap_password": PASSWORD}})
    network = wifi.station(SSID, password=PASSWORD)
    board.power_on()
    first = board.wait_until(network.is_leased, within=seconds(3))
    boot = board.boot.number
    fresh = network.next_lease()
    if power_cycle:
        board.power_off()
        board.power_on()
    else:
        board.reset()
    # reset() queues EN; wait until the runner reports the reset before inspecting its effects.
    board.wait_until(lambda: board.boot.number > boot, within=seconds(1))
    assert network.lease is None
    renewed = board.wait_until(network.is_leased, within=seconds(3))
    assert renewed.at > first.at
    assert fresh() is renewed and network.leases == [first, renewed]
    exchange = network.http_client.get("/status")
    assert board.wait_until(exchange.response, within=seconds(2)).status == 200


def test_station_diagnostics_are_in_captured_output(pytester: pytest.Pytester) -> None:
    app = Path(__file__).resolve().parents[1] / "firmware/arduino_shell"
    pytester.makepyfile(f"""
        import pytest
        from esp32tl.units import seconds
        pytestmark = pytest.mark.app_path({str(app)!r}, build_dir=".pio/build/esp32dev")
        def test_wrong_password(board, wifi):
            board.nvs({{"shell": {{"ap_ssid": "setup", "ap_password": "password1"}}}})
            network = wifi.station("setup", password="password2")
            board.power_on()
            board.wait_until(network.is_leased, within=seconds(2))
    """)
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-rA")
    result.assert_outcomes(failed=1)
    # Upstream reports the last failure while listening, between handshake attempts.
    result.stdout.fnmatch_lines(["*[[]emu] wifi-sta:*wrong passphrase?*"])


@pytest.mark.parametrize("password", [None, PASSWORD], ids=["open", "wpa2"])
def test_delayed_softap_starts_dhcp_at_link_ready(
    board: Esp32Sim, wifi: Wifi, password: str | None, tmp_path: Path
) -> None:
    board.nvs(
        {"shell": {"ap_ssid": SSID, "ap_password": password or "", "ap_delay_ms": u32(15300)}}
    )
    network = wifi.station(SSID, password=password)
    board.power_on()
    board.wait(seconds(15))
    assert not [e for e in network.events if str(e["event"]).startswith("dhcp station:")]
    assert isinstance(network.link, Station)
    assert network.link.quiet_until() is None
    board.wait_until(network.is_leased, within=seconds(10))
    ready = [e for e in network.events if e["event"] == "wifi station: ready"]
    discover = [e for e in network.events if e["event"] == "dhcp station: DISCOVER"]
    assert len(ready) == len(discover) == 1
    assert discover[0]["t_ns"] == ready[0]["t_ns"]
    (tmp_path / "lifecycle.json").write_text(json.dumps(network.events, sort_keys=True))


@pytest.mark.parametrize("password", [None, PASSWORD], ids=["open", "wpa2"])
def test_softap_leaves_and_rejoins(board: Esp32Sim, wifi: Wifi, password: str | None) -> None:
    board.nvs({"shell": {"ap_ssid": SSID, "ap_password": password or ""}})
    network = wifi.station(SSID, password=password)
    board.power_on()
    first = board.wait_until(network.is_leased, within=seconds(3))
    connection = network.connect(80)
    board.wait_until(connection.established, within=seconds(2))
    board.console.sendline("ap-off")
    board.wait_until(lambda: network.lease is None or None, within=seconds(5))
    assert connection.state == "lost"
    board.wait(seconds(5))
    assert isinstance(network.link, Station)
    assert network.link.quiet_until() is None
    board.console.sendline("ap-on")
    second = board.wait_until(network.is_leased, within=seconds(5))
    assert second.at > first.at and network.leases == [first, second]
    ready = [cast(int, e["t_ns"]) for e in network.events if e["event"] == "wifi station: ready"]
    discover = [e["t_ns"] for e in network.events if e["event"] == "dhcp station: DISCOVER"]
    assert ready == discover
    # The station joined, left with the access point and joined again, at the data link's times.
    assert [(c.up, c.cause) for c in network.link_changes] == [
        (True, "station joined"),
        (False, "station left"),
        (True, "station joined"),
    ]
    disconnected = [
        cast(int, e["t_ns"]) for e in network.events if e["event"] == "wifi station: disconnected"
    ]
    # A disconnected before the first join changes nothing: the link was not up.
    left = next(t for t in disconnected if t > ready[0])
    assert [c.at.ns for c in network.link_changes] == [ready[0], left, ready[1]]
    assert first.at.ns >= ready[0] and ready[1] <= second.at.ns
