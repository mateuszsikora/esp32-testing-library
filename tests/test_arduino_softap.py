"""The Arduino SoftAP, reached through the virtual station with raw TCP and HTTP."""

import json
from pathlib import Path

import pytest

from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.interfaces import NeverHappened
from esp32tl.interfaces.wifi import Wifi
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
    lease = board.wait_until(network.when_leased(), within=seconds(3))
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
        board.wait_until(network.when_leased(), within=seconds(3))
    assert network.lease is None and board.link.error is None


def test_raw_tcp_to_the_softap(board: Esp32Sim, wifi: Wifi) -> None:
    board.nvs({"shell": {"ap_ssid": SSID}})
    network = wifi.station(SSID)
    board.power_on()
    board.wait_until(network.when_leased(), within=seconds(3))
    connection = network.connect(80)
    connection.write(b"GET /status HTTP/1.0\r\nHost: 192.168.4.1\r\n\r\n")
    board.wait_until(connection.closed(), within=seconds(2))
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
    first = board.wait_until(network.when_leased(), within=seconds(3))
    boot = board.boot.number
    if power_cycle:
        board.power_off()
        board.power_on()
    else:
        board.reset()
    # reset() queues EN; wait until the runner reports the reset before inspecting its effects.
    board.wait_until(lambda: board.boot.number > boot, within=seconds(1))
    assert network.lease is None
    renewed = board.wait_until(network.when_leased(), within=seconds(3))
    assert renewed.at > first.at
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
            board.wait_until(network.when_leased(), within=seconds(2))
    """)
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-rA")
    result.assert_outcomes(failed=1)
    # Upstream reports the last failure while listening, between handshake attempts.
    result.stdout.fnmatch_lines(["*[[]emu] wifi-sta:*wrong passphrase?*"])
