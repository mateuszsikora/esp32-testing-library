"""The broker's `on_publish` handlers on the emulator, with `firmware/mqtt_session` unchanged: a
handler answers each report the firmware publishes as it is routed, during a plain `board.wait`,
and what a handler does wrong fails the test's wait."""

import pytest

from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.interfaces.console import Console
from esp32tl.interfaces.modem import Modem
from esp32tl.net.mqtt import Broker, Message
from esp32tl.units import Duration, ms, seconds

pytestmark = [
    pytest.mark.app_path("../firmware/mqtt_session"),
    pytest.mark.board_profile("lilygo-t-a7670g-r2"),
]

CLIENT = "session-client"
COMMANDS = "session/commands"
REPORTS = "session/reports"
COUNT = 5


def connected(board: Esp32Sim, broker: Broker) -> None:
    board.power_on()
    board.wait_until(broker.is_subscribed(COMMANDS), within=seconds(60))


@pytest.mark.parametrize("after", [None, ms(50)], ids=["at-once", "after-50ms"])
def test_a_handler_answers_each_report_as_it_is_routed_during_a_plain_wait(
    board: Esp32Sim, modem: Modem, broker: Broker, console: Console, after: Duration | None
) -> None:
    connected(board, broker)
    modem.network().set_path(rtt=ms(200))

    def answer(report: Message) -> None:
        if not report.dup:
            broker.publish(COMMANDS, f"ack {report.text}", qos=1, after=after)

    broker.on_publish(REPORTS, answer)
    # The firmware prints each message it gets, and ignores what is not a backlog.
    printed = [
        console.line(rf"data dup=0 qos=1 id=\d+ topic={COMMANDS} payload=ack report {n}$")
        for n in range(1, COUNT + 1)
    ]
    done = console.line(r"mqtt_session: backlog done")
    broker.publish(COMMANDS, f"backlog {COUNT}", qos=1)
    # A wait that asks no condition: nothing of the test's runs but the handler.
    board.wait(seconds(10))
    assert done() is not None and all(line() is not None for line in printed)
    reports = [m for m in broker.messages if m.topic == REPORTS and not m.dup]
    assert [m.text for m in reports] == [f"report {n}" for n in range(1, COUNT + 1)]
    for report in reports:
        delivery = broker.find_delivery(COMMANDS, f"ack {report.text}")
        assert delivery is not None and delivery.acknowledged_at is not None
        # The broker's end of the path: the answer sets off as the report is routed, or the
        # server's own delay later, not at a condition's next look.
        assert delivery.at == report.at + (after or Duration(0))
    assert board.link.warnings == []


def test_a_handler_that_waits_on_the_board_fails_the_wait_naming_the_handler(
    board: Esp32Sim, broker: Broker
) -> None:
    connected(board, broker)

    def waits(report: Message) -> None:
        board.wait(ms(1))

    broker.on_publish(REPORTS, waits)
    broker.publish(COMMANDS, "backlog 1", qos=1)
    with pytest.raises(RuntimeError) as raised:
        board.wait(seconds(5))
    assert str(raised.value) == (
        "board.wait: called from the broker's on_publish handler for 'session/reports':"
        " a handler must not wait"
    )
    state = broker.session(CLIENT)
    assert state is not None and state.connected


def test_a_handler_that_raises_fails_the_wait_it_interrupted_and_the_device_stays_connected(
    board: Esp32Sim, broker: Broker, console: Console
) -> None:
    connected(board, broker)

    def fails(report: Message) -> None:
        raise ValueError(f"cannot answer {report.text}")

    registration = broker.on_publish(REPORTS, fails)
    done = console.line(r"mqtt_session: backlog done")
    broker.publish(COMMANDS, "backlog 2", qos=1)
    with pytest.raises(ValueError, match="cannot answer report 1") as raised:
        board.wait_until(done, within=seconds(10))
    assert (
        "raised by the broker's on_publish handler for 'session/reports'"
        in (raised.value.__notes__[0])
    )
    registration.remove()
    # The device's connection stays: nothing of the device's caused the error.
    board.wait_until(done, within=seconds(10))
    assert [m.text for m in broker.find_messages(REPORTS)] == ["report 1", "report 2"]
    assert len(broker.clients) == 1 and broker.connection_ends == []
    assert board.link.warnings == []
