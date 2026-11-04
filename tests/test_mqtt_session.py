"""esp-mqtt of the pinned ESP-IDF with CleanSession=0, against the broker's stored session."""

from itertools import pairwise

import pytest

from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.interfaces.console import Console
from esp32tl.interfaces.modem import Modem
from esp32tl.net.mqtt import Broker, Message
from esp32tl.units import Duration, about, kbit_s, ms, seconds

pytestmark = [
    pytest.mark.app_path("../firmware/mqtt_session"),
    pytest.mark.board_profile("lilygo-t-a7670g-r2"),
]

CLIENT = "session-client"
COMMANDS = "session/commands"
REPORTS = "session/reports"
COUNT = 10


def connected(board: Esp32Sim, broker: Broker) -> None:
    board.power_on()
    board.wait_until(broker.subscribed(COMMANDS), within=seconds(60))


def test_first_connection_finds_no_session_and_the_next_one_does(
    board: Esp32Sim, broker: Broker, console: Console
) -> None:
    first = console.line(r"mqtt_session: connected session_present=0")
    connected(board, broker)
    board.wait_until(first, within=seconds(5))
    assert broker.clients[0].session_present is False

    again = console.line(r"mqtt_session: connected session_present=1")
    reconnected = broker.next_connection()
    assert broker.disconnect(CLIENT)
    board.wait_until(reconnected, within=seconds(30))
    board.wait_until(again, within=seconds(5))
    assert broker.clients[1].session_present is True
    assert broker.connection_attempts[1].session_present is True
    assert board.link.warnings == []


def test_receives_a_qos1_message_published_while_it_was_offline(
    board: Esp32Sim, broker: Broker, console: Console
) -> None:
    connected(board, broker)
    reconnected = broker.next_connection()
    received = console.line(r"data dup=0 qos=1 id=\d+ topic=session/commands payload=while offline")
    assert broker.disconnect(CLIENT)
    broker.publish(COMMANDS, "while offline", qos=1)
    broker.publish(COMMANDS, "lost", qos=0)
    state = broker.session(CLIENT)
    assert state is not None and [m.text for m in state.queued] == ["while offline"]
    board.wait_until(reconnected, within=seconds(30))
    board.wait_until(received, within=seconds(5))
    delivery = broker.find_delivery(COMMANDS, "while offline")
    assert delivery is not None and not delivery.dup and delivery.qos == 1
    # The firmware's PUBACK comes back on the new connection.
    board.wait(seconds(1))
    assert delivery.acknowledged_at is not None
    assert broker.find_delivery(COMMANDS, "lost") is None
    state = broker.session(CLIENT)
    assert state is not None and state.queued == () and state.unacknowledged == ()
    assert board.link.warnings == []


def test_a_message_whose_puback_is_lost_comes_again_with_dup(
    board: Esp32Sim, modem: Modem, broker: Broker, console: Console
) -> None:
    connected(board, broker)
    first = console.line(r"data dup=0 qos=1 id=1 topic=session/commands payload=in flight")
    again = console.line(r"data dup=1 qos=1 id=1 topic=session/commands payload=in flight")
    # In one instant: the PUBLISH reaches the firmware, but the connection has closed before its
    # PUBACK reaches the broker.
    broker.publish(COMMANDS, "in flight", qos=1)
    assert broker.disconnect(CLIENT)
    board.wait_until(first, within=seconds(5))
    board.wait_until(again, within=seconds(30))
    original, redelivered = [d for d in broker.deliveries if d.text == "in flight"]
    assert (original.dup, redelivered.dup) == (False, True)
    assert original.packet_id == redelivered.packet_id == 1
    assert original.connection_id != redelivered.connection_id
    board.wait(seconds(1))
    assert original.acknowledged_at is None
    assert redelivered.acknowledged_at is not None
    # The firmware answered both: the first PUBACK came to a connection already closed.
    events = modem.network().transcript()["events"]
    assert isinstance(events, list)
    pubacks = [e for e in events if e.get("direction") == "out" and e["data"] == "40020001"]
    assert [e["stream"] for e in pubacks] == ["1883/1", "1883/2"]
    state = broker.session(CLIENT)
    assert state is not None and state.unacknowledged == ()
    assert board.link.warnings == []


def backlog(board: Esp32Sim, broker: Broker, console: Console) -> list[Message]:
    """Has the firmware publish its backlog; the reports' first attempts, in order."""
    done = console.line(r"mqtt_session: backlog done")
    start = len(broker.messages)
    broker.publish(COMMANDS, f"backlog {COUNT}", qos=1)
    board.wait_until(done, within=seconds(60))
    reports = [m for m in broker.messages[start:] if m.topic == REPORTS and not m.dup]
    assert [m.text for m in reports] == [f"report {n}" for n in range(1, COUNT + 1)]
    return reports


def spacing(reports: list[Message]) -> list[Duration]:
    """From one exchange to the next, leaving out the first, which waits a round trip behind the
    PUBACK of the command: lwIP's Nagle holds it until that is acknowledged."""
    return [b.at - a.at for a, b in pairwise(reports[1:])]


def test_a_qos1_backlog_follows_the_rtt(
    board: Esp32Sim, modem: Modem, broker: Broker, console: Console
) -> None:
    network = modem.network()
    connected(board, broker)
    network.set_path(rtt=ms(200))
    at_200 = backlog(board, broker, console)
    network.set_path(rtt=ms(600))
    at_600 = backlog(board, broker, console)
    # One exchange at a time: the RTT, and some 15 ms on the modem's UART and in the firmware.
    assert spacing(at_200) == [about(ms(215), within=ms(3))] * (COUNT - 2)
    assert spacing(at_600) == [about(ms(615), within=ms(3))] * (COUNT - 2)
    ratio = (at_600[-1].at - at_600[1].at) / (at_200[-1].at - at_200[1].at)
    assert 2.7 < ratio < 3.1
    assert board.link.warnings == []


def test_a_qos1_backlog_takes_the_rate_in_both_directions(
    board: Esp32Sim, modem: Modem, broker: Broker, console: Console
) -> None:
    network = modem.network()
    connected(board, broker)
    network.set_path(rtt=ms(200), downlink=kbit_s(8), uplink=kbit_s(8))
    # 215 ms, and at 8 kbit/s the PUBLISH going up and the ACK and PUBACK coming down.
    assert spacing(backlog(board, broker, console)) == [about(ms(362), within=ms(3))] * (COUNT - 2)
    assert board.link.warnings == []


def test_sessions_work_across_a_long_path(
    board: Esp32Sim, modem: Modem, broker: Broker, console: Console
) -> None:
    network = modem.network()
    connected(board, broker)
    network.set_path(rtt=ms(600), downlink=kbit_s(512), uplink=kbit_s(128))
    reconnected = broker.next_connection()
    first = console.line(r"data dup=0 qos=1 id=\d+ topic=session/commands payload=in flight")
    again = console.line(r"data dup=1 qos=1 id=\d+ topic=session/commands payload=in flight")
    broker.publish(COMMANDS, "in flight", qos=1)
    assert broker.disconnect(CLIENT)
    board.wait_until(first, within=seconds(5))
    client = board.wait_until(reconnected, within=seconds(30))
    assert client.session_present
    board.wait_until(again, within=seconds(5))
    assert board.link.warnings == []
