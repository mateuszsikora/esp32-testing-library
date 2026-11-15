"""The `arduino_shell` image, an Arduino-ESP32 sketch built by PlatformIO, run from its
`.pio/build/esp32dev/` directory as `pio run` left it: the core's `Serial` prints its banner,
`Preferences` reads what the test wrote with `board.nvs(...)`, and `WiFi` and `WebServer` answer
`GET /status` once the settings name a network."""

import json

import pytest

from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.interfaces.console import Console
from esp32tl.interfaces.wifi import Wifi
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
    connection = network.connect(80)
    connection.write(b"GET /status HTTP/1.1\r\nHost: device\r\nConnection: close\r\n\r\n")
    board.wait_until(connection.closed(), within=seconds(5))
    head, _, body = connection.received.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200 OK\r\n")
    assert b"Content-Type: application/json" in head
    status = json.loads(body)
    assert status["status"] == "ok" and status["requests"] == 1
    console.expect_exact("arduino_shell: GET /status -> 200")


def test_console_help(board: Esp32Sim) -> None:
    board.power_on()
    board.console.expect_exact("arduino_shell: ready; type help")
    board.console.sendline("help")
    board.console.expect_exact("arduino_shell: commands: help, greeting, uptime, wifi")
