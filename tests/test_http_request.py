"""ESP-IDF's unmodified http_request example joins the test's access point and fetches from the
test's HTTP server (plan M7), with ESP-IDF's default SSID and passphrase."""

from pathlib import Path

import pytest

from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.interfaces import NeverHappened
from esp32tl.interfaces.console import Console
from esp32tl.interfaces.wifi import Wifi
from esp32tl.units import seconds

pytestmark = pytest.mark.app_path("../firmware/http_request")

SSID, PASSWORD = "myssid", "mypassword"


def test_joins_and_fetches_from_the_tests_server(
    board: Esp32Sim, wifi: Wifi, console: Console
) -> None:
    network = wifi.access_point(SSID, password=PASSWORD)
    network.http.serve("/", "hello from the test\n")
    printed = console.line(r"^hello from the test$")
    done = console.line(r"done reading from socket\. Last read return=0")
    board.power_on()
    request = board.wait_until(network.http.next_request("/"), within=seconds(30))
    assert (request.method, request.version) == ("GET", "HTTP/1.0")
    assert request.header("Host") == "example.com:80"
    board.wait_until(printed, within=seconds(5))
    board.wait_until(done, within=seconds(5))
    assert network.dns.asked == ["example.com"]
    [lease] = network.dhcp.leases
    assert (lease.mac, lease.address) == ("24:6f:28:00:11:22", "10.64.0.2")
    assert board.link.warnings == [] and board.link.error is None
    assert "http: GET / → 200" in board.report()


def test_rejoins_once_the_access_point_is_back(
    board: Esp32Sim, wifi: Wifi, console: Console
) -> None:
    network = wifi.access_point(SSID, password=PASSWORD)
    network.http.serve("/", "first\n")
    board.power_on()
    board.wait_until(console.line(r"^first$"), within=seconds(30))
    wifi.access_point_off()
    # the access point deauthenticates the station as it leaves: reason 3, leaving
    board.wait_until(console.line(r"Wi-Fi disconnected 3"), within=seconds(1))
    rejoined = network.dhcp.next_lease()
    board.wait(seconds(2))
    assert rejoined() is None, "nothing to join while the access point is off"
    network.http.serve("/", "second\n")
    wifi.access_point_on()
    board.wait_until(rejoined, within=seconds(30))
    board.wait_until(console.line(r"^second$"), within=seconds(30))
    events = [e.get("event") for e in network.events]
    assert events.index("wifi: access point off the air") < events.index(
        "wifi: access point back on the air"
    )


def test_rejoins_an_access_point_switched_off_and_on_at_one_instant(
    board: Esp32Sim, wifi: Wifi, console: Console
) -> None:
    network = wifi.access_point(SSID, password=PASSWORD)
    network.http.serve("/", "first\n")
    board.power_on()
    board.wait_until(console.line(r"^first$"), within=seconds(30))
    disconnected = console.line(r"Wi-Fi disconnected 3")
    rejoined = network.dhcp.next_lease()
    # the station still gets its deauthentication: the fresh one waits for the old one to leave
    wifi.access_point_off()
    wifi.access_point_on()
    network.http.serve("/", "second\n")
    board.wait_until(disconnected, within=seconds(1))
    board.wait_until(rejoined, within=seconds(30))
    board.wait_until(console.line(r"^second$"), within=seconds(30))


@pytest.mark.parametrize(
    ("service", "expected"),
    [("http", "the next HTTP request for '/'"), ("dhcp", "the next DHCP lease")],
)
def test_a_timeout_names_the_expected_request_or_lease(
    board: Esp32Sim, wifi: Wifi, service: str, expected: str
) -> None:
    network = wifi.access_point(SSID, password=PASSWORD)
    board.power_on()
    condition = network.http.next_request("/") if service == "http" else network.dhcp.next_lease()
    # the station joins at about 2.8 s: nothing is leased or requested in the first 0.1 s
    with pytest.raises(NeverHappened, match=f"{expected} did not happen within"):
        board.wait_until(condition, within=seconds(0.1))
    assert f"waits up to 0.1 s for {expected}" in board.report(run=True)


def test_without_an_access_point_the_station_finds_nothing_to_join(
    board: Esp32Sim, wifi: Wifi, console: Console
) -> None:
    board.power_on()
    board.wait_until(console.line(r"Wi-Fi disconnected"), within=seconds(30))
    assert wifi.runner_args() == []


def test_a_wrong_password_never_gets_an_address(
    board: Esp32Sim, wifi: Wifi, console: Console
) -> None:
    network = wifi.access_point(SSID, password="not-the-password")
    board.power_on()
    board.wait_until(console.line(r"Wi-Fi disconnected"), within=seconds(30))
    board.wait(seconds(5))
    assert network.dhcp.leases == []


def test_the_report_and_properties_carry_the_access_point_and_its_network(
    board: Esp32Sim, wifi: Wifi
) -> None:
    network = wifi.access_point(SSID, password=PASSWORD)
    wifi.nearby("cafe", channel=3, rssi=-70, secure=False)
    board.power_on()
    board.wait_until(network.dhcp.next_lease(), within=seconds(30))
    report = board.report()
    assert "wifi: access point 'myssid', channel 6, WPA2-PSK" in report
    assert "  around: 'cafe', channel 3, -70 dBm, open" in report
    assert "dhcp: REQUEST from 24:6f:28:00:11:22 → ACK 10.64.0.2" in report
    properties = board.properties()["wifi"]
    assert isinstance(properties, dict)
    assert properties["access_point"] == {"ssid": SSID, "channel": 6, "secure": True}
    assert properties["network"]["esp32tl_network"] == 1


def test_wifi_after_power_on_is_refused(pytester: pytest.Pytester) -> None:
    app = Path(__file__).resolve().parents[1] / "firmware/http_request"
    pytester.makepyfile(f"""
        import pytest
        pytestmark = pytest.mark.app_path({str(app)!r})

        def test_late(board, wifi):
            board.power_on()
            wifi.access_point("myssid", password="mypassword")
    """)
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-rA")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        ["*wifi: the board is on already: give an interface its source before board.power_on()*"]
    )


@pytest.mark.parametrize("how", ["reset", "power cycle"])
def test_joins_again_after_a_reset_or_a_power_cycle(
    board: Esp32Sim, wifi: Wifi, console: Console, how: str
) -> None:
    network = wifi.access_point(SSID, password=PASSWORD)
    network.http.serve("/", "first\n")
    board.power_on()
    board.wait_until(console.line(r"^first$"), within=seconds(30))
    rejoined = network.dhcp.next_lease()
    network.http.serve("/", "second\n")
    if how == "reset":
        board.reset()
    else:
        board.power_off()
        board.wait(seconds(5))
        assert rejoined() is None, "a station without power joins nothing"
        board.power_on()
    board.wait_until(rejoined, within=seconds(30))
    board.wait_until(console.line(r"^second$"), within=seconds(30))
    assert board.link.error is None
