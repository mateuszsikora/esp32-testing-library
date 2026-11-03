"""Unmodified esp-protocols modem-v1.4.1, through the LilyGO modem UART and PPP."""

import pytest

from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.interfaces.console import Console
from esp32tl.interfaces.modem import Modem
from esp32tl.net.mqtt import Broker
from esp32tl.units import seconds

pytestmark = [
    pytest.mark.app_path("../firmware/pppos_client"),
    pytest.mark.board_profile("lilygo-t-a7670g-r2"),
]


def test_publishes_its_test_message(board: Esp32Sim, modem: Modem, broker: Broker) -> None:
    board.power_on()
    message = board.wait_until(broker.next_message(topic="/topic/esp-pppos"), within=seconds(60))
    assert message.text == "esp32-pppos"
    assert modem.network().dns.asked == ["mqtt.eclipseprojects.io"]
    assert board.link.warnings == []
    assert "ppp: IPCP open" in board.report()
    routes = board.properties()["routes"]
    assert isinstance(routes, dict) and routes["uart.modem"]["network"]["events"]


def test_shows_what_the_cloud_sends_it(board: Esp32Sim, broker: Broker, console: Console) -> None:
    board.power_on()
    board.wait_until(broker.subscribed("/topic/esp-pppos"), within=seconds(60))
    broker.publish("/topic/esp-pppos", "hello from the test")
    board.wait_until(console.line(r"DATA=hello from the test"), within=seconds(5))
    assert board.link.warnings == []


def test_broker_refuses_a_missing_route_or_late_selection(pytester: pytest.Pytester) -> None:
    from pathlib import Path

    app = Path(__file__).resolve().parents[1] / "firmware/pppos_client"
    pytester.makepyfile(f"""
        import pytest
        pytestmark = pytest.mark.app_path({str(app)!r})

        def test_missing(broker):
            pass

        @pytest.mark.board_profile("lilygo-t-a7670g-r2")
        def test_late(board, modem):
            modem.signal(dbm=-73)
            board.power_on()
            modem.network()
    """)
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-rA")
    result.assert_outcomes(errors=1, failed=1)
    result.stdout.fnmatch_lines_random(
        [
            "*the board profile esp32-devkitc has no uart.modem*",
            "*uart.modem: the board is on already: give an interface its source before"
            " board.power_on()*",
        ]
    )
