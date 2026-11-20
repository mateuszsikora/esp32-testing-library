"""The `arduino_shell` image, an Arduino-ESP32 sketch built by PlatformIO, run from its
`.pio/build/esp32dev/` directory as `pio run` left it: the core's `Serial` prints its banner,
`Preferences` reads what the test wrote with `board.nvs(...)`, and `WiFi` and `WebServer` answer
`GET /status` once the settings name a network."""

import pytest

from esp32tl import u32
from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.interfaces.console import Console
from esp32tl.interfaces.wifi import Wifi
from esp32tl.net.client import ConnectionEnded
from esp32tl.units import seconds

pytestmark = pytest.mark.app_path("../firmware/arduino_shell", build_dir=".pio/build/esp32dev")

SSID, PASSWORD = "myssid", "mypassword"


def test_boots_and_prints_its_banner(board: Esp32Sim, console: Console) -> None:
    board.power_on()
    console.expect(r"arduino_shell: Arduino-ESP32 3\.\d+\.\d+\r\n")
    # the flash holds no namespace "shell": the core logs it, and the greeting is empty
    console.expect_exact("begin(): nvs_open failed: NOT_FOUND\r\n")
    console.expect_exact("arduino_shell: greeting=\r\n")
    console.expect_exact("arduino_shell: ready; type help\r\n")
    assert board.link.warnings == [] and board.link.error is None


def test_reads_a_value_the_test_wrote_with_board_nvs(board: Esp32Sim, console: Console) -> None:
    board.nvs({"shell": {"greeting": "hello from the test"}})
    board.power_on()
    console.expect_exact("arduino_shell: greeting=hello from the test")
    console.expect_exact("arduino_shell: ready; type help")


def test_serves_status_once_it_joins_the_network(
    board: Esp32Sim, wifi: Wifi, console: Console
) -> None:
    board.nvs({"shell": {"ssid": SSID, "password": PASSWORD}})
    network = wifi.access_point(SSID, password=PASSWORD)
    listening = console.line(r"^arduino_shell: listening on 10\.64\.0\.2 port 80$")
    board.power_on()
    board.wait_until(listening, within=seconds(30))
    exchange = network.http_client.get("/status")
    response = board.wait_until(exchange.response, within=seconds(5))
    assert response.status == 200
    assert response.headers["content-type"] == "application/json"
    status = response.json()
    assert status["status"] == "ok" and status["requests"] == 1
    console.expect_exact("arduino_shell: GET /status -> 200")


def test_serves_a_get_sent_at_the_lease_once_its_syn_goes_again(
    board: Esp32Sim, wifi: Wifi
) -> None:
    # WebServer starts with the address, which the station takes about 1 s after DHCP's ACK: the
    # SYN sent at the lease gets no answer, the one sent again 1 s later is accepted.
    board.nvs({"shell": {"ssid": SSID, "password": PASSWORD}})
    network = wifi.access_point(SSID, password=PASSWORD)
    lease = network.dhcp.next_lease()
    board.power_on()
    board.wait_until(lease, within=seconds(30))
    get = network.http_client.get("/status")
    assert board.wait_until(get.response, within=seconds(3)).status == 200
    first, again = get.connection.syns
    assert again.ns - first.ns == 1_000_000_000
    assert get.connection.established_at is not None and get.connection.established_at > again


def test_a_server_that_listens_after_its_address_is_waited_for_with_accepting(
    board: Esp32Sim, wifi: Wifi
) -> None:
    # WebServer starts 200 ms after the address: the SYN sent again finds the address taken and
    # nothing listening, and the device refuses it; `accepting` connects again until it listens.
    board.nvs({"shell": {"ssid": SSID, "password": PASSWORD, "listen_delay_ms": u32(200)}})
    network = wifi.access_point(SSID, password=PASSWORD)
    lease = network.dhcp.next_lease()
    board.power_on()
    board.wait_until(lease, within=seconds(30))
    get = network.http_client.get("/status")
    with pytest.raises(ConnectionEnded, match=r": refused before HTTP response completed$"):
        board.wait_until(get.response, within=seconds(3))
    assert len(get.connection.syns) == 2
    connection = board.wait_until(network.accepting(80), within=seconds(3))
    assert connection.state == "established"
    assert [c.state for c in network.connections] == ["refused", "refused", "established"]
    connection.close()
    status = board.wait_until(network.http_client.get("/status").response, within=seconds(5))
    assert status.status == 200 and status.json()["requests"] == 1
    assert board.link.warnings == [] and board.link.error is None


def test_console_help(board: Esp32Sim) -> None:
    board.power_on()
    board.console.expect_exact("arduino_shell: ready; type help")
    board.console.sendline("help")
    board.console.expect_exact(
        "arduino_shell: commands: help, greeting, uptime, wifi, ap-off, ap-on"
    )
