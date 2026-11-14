"""The `http_server` image serves HTTP on port 80 behind the test's access point, and the test is
its client: `network.connect(80)` opens the connection, the request is written by hand and the
response read as bytes."""

import json

import pytest

from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.interfaces.console import Console
from esp32tl.interfaces.wifi import Wifi
from esp32tl.net import Internet
from esp32tl.net.client import ConnectionEnded, NotReachable, TcpConnection
from esp32tl.units import ms, seconds

pytestmark = pytest.mark.app_path("../firmware/http_server")

SSID, PASSWORD = "myssid", "mypassword"


def listening(board: Esp32Sim, wifi: Wifi, console: Console) -> Internet:
    network = wifi.access_point(SSID, password=PASSWORD)
    ready = console.line(r"^http_server: listening on port 80$")
    board.power_on()
    board.wait_until(ready, within=seconds(30))
    return network


def response(
    board: Esp32Sim, connection: TcpConnection, start: int
) -> tuple[bytes, dict[str, str], bytes]:
    """The status line, the header fields and the body of the response that starts at `start` in
    what the device sent, the body read by its Content-Length."""
    head = board.wait_until(connection.next_data(until=b"\r\n\r\n", start=start), within=seconds(5))
    status, *fields = head[:-4].decode().split("\r\n")
    headers = {name.lower(): value.strip() for name, _, value in (f.partition(":") for f in fields)}
    size = int(headers["content-length"])
    body = board.wait_until(
        connection.next_data(size=size, start=start + len(head)), within=seconds(5)
    )
    return status.encode(), headers, body


def test_answers_a_get_and_closes_when_asked(board: Esp32Sim, wifi: Wifi, console: Console) -> None:
    network = listening(board, wifi, console)
    connection = network.connect(80)
    assert (connection.address, connection.local_port) == ("10.64.0.2", 49152)
    connection.write(b"GET /status HTTP/1.1\r\nHost: device\r\nConnection: close\r\n\r\n")
    established = board.wait_until(connection.established(), within=seconds(1))
    closed = board.wait_until(connection.closed(), within=seconds(5))
    assert established < closed
    head, _, body = connection.received.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200 OK\r\n")
    assert b"Content-Type: application/json" in head
    status = json.loads(body)
    assert status["status"] == "ok" and status["requests"] == 1
    connection.close()
    board.wait(ms(100))
    assert connection.state == "closed"
    events = [e.get("event") for e in network.events]
    assert "net: TCP 10.64.0.1:49152 → 10.64.0.2:80 connecting" in events
    assert "net: TCP 10.64.0.1:49152 → 10.64.0.2:80 established" in events
    assert "net: TCP 10.64.0.1:49152 → 10.64.0.2:80 closed" in events
    assert board.link.warnings == [] and board.link.error is None
    report = board.report()
    assert "stream device:80/1 in 474554202f737461747573" in report


def test_echoes_json_on_a_connection_kept_open(
    board: Esp32Sim, wifi: Wifi, console: Console
) -> None:
    network = listening(board, wifi, console)
    connection = network.connect(80)
    board.wait_until(connection.established(), within=seconds(1))
    # Over one MSS each way, under the firmware's 2048 bytes.
    body = json.dumps({"id": 7, "text": "x" * 1800}).encode()
    connection.write(
        b"POST /echo HTTP/1.1\r\nHost: device\r\nContent-Type: application/json\r\n"
        + f"Content-Length: {len(body)}\r\n\r\n".encode()
        + body
    )
    status, headers, answer = response(board, connection, 0)
    assert status == b"HTTP/1.1 200 OK"
    assert headers["content-type"] == "application/json"
    assert json.loads(answer) == {"echo": {"id": 7, "text": "x" * 1800}, "bytes": len(body)}
    second = len(connection.received)
    # The same connection takes a second request.
    connection.write(b"POST /echo HTTP/1.1\r\nHost: device\r\nContent-Length: 3\r\n\r\n{]}")
    status, _, answer = response(board, connection, second)
    assert status == b"HTTP/1.1 400 Bad Request"
    assert json.loads(answer) == {"error": "invalid JSON"}
    connection.close()
    board.wait_until(connection.closed(), within=seconds(5))
    assert board.link.warnings == [] and board.link.error is None


def test_a_port_nobody_listens_on_refuses(board: Esp32Sim, wifi: Wifi, console: Console) -> None:
    network = listening(board, wifi, console)
    connection = network.connect(8080)
    board.wait_until(connection.refused(), within=seconds(1))
    assert connection.state == "refused"
    with pytest.raises(ConnectionEnded, match=r"refused by the device before it was established"):
        connection.established()()
    assert "net: TCP 10.64.0.1:49152 → 10.64.0.2:8080 refused by the device" in [
        e.get("event") for e in network.events
    ]


def test_connecting_before_the_device_has_an_address_says_so(board: Esp32Sim, wifi: Wifi) -> None:
    network = wifi.access_point(SSID, password=PASSWORD)
    board.power_on()
    with pytest.raises(NotReachable) as error:
        network.connect(80)
    assert str(error.value) == (
        "network.connect(80): the device has no address yet: DHCP has acknowledged none on this"
        " link; wait for network.dhcp.next_lease(), or give address= for a static one"
    )
    assert network.connections == []


def test_a_body_over_its_limit_is_refused_without_being_read(
    board: Esp32Sim, wifi: Wifi, console: Console
) -> None:
    network = listening(board, wifi, console)
    connection = network.connect(80)
    body = b'"' + b"x" * 2100 + b'"'
    connection.write(
        b"POST /echo HTTP/1.1\r\nHost: device\r\nConnection: close\r\n"
        + f"Content-Length: {len(body)}\r\n\r\n".encode()
        + body
    )
    status, _, answer = response(board, connection, 0)
    assert status == b"HTTP/1.1 413 Content Too Large"
    assert json.loads(answer) == {"error": "body over 2048 bytes"}
    # The firmware prints its answer once it has sent it, before the test reads it.
    assert console.lines(r"^http_server: POST /echo -> 413$")


@pytest.mark.parametrize("how", ["reset", "power cycle"])
def test_a_reset_loses_the_tests_connections_until_the_device_listens_again(
    board: Esp32Sim, wifi: Wifi, console: Console, how: str
) -> None:
    network = listening(board, wifi, console)
    before = network.connect(80)
    board.wait_until(before.established(), within=seconds(1))
    closed = before.closed()
    if how == "reset":
        board.reset()
    else:
        board.power_off()
    with pytest.raises(ConnectionEnded, match=r"was lost when the device (reset|lost its power)"):
        board.wait_until(closed, within=seconds(1))
    assert before.state == "lost" and network.device_address is None
    with pytest.raises(NotReachable, match="the device has no address yet"):
        network.connect(80)
    listens = console.line(r"^http_server: listening on port 80$")
    if how == "power cycle":
        board.power_on()
    board.wait_until(listens, within=seconds(30))
    after = network.connect(80)
    after.write(b"GET /status HTTP/1.1\r\nHost: device\r\nConnection: close\r\n\r\n")
    board.wait_until(after.closed(), within=seconds(5))
    assert (
        after.local_port == 49153
        and json.loads(after.text.partition("\r\n\r\n")[2])["requests"] == 1
    )
    assert board.link.error is None
