"""esp-mqtt of the pinned ESP-IDF with CleanSession=0, against the broker's stored session."""

from itertools import pairwise
from typing import cast

import pytest

from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.interfaces.condition import Condition
from esp32tl.interfaces.console import Console
from esp32tl.interfaces.modem import Modem
from esp32tl.net import Internet
from esp32tl.net.mqtt import Broker, Message
from esp32tl.units import Duration, about, kbit_s, ms, seconds

pytestmark = [
    pytest.mark.app_path("../firmware/mqtt_session"),
    pytest.mark.board_profile("lilygo-t-a7670g-r2"),
]

CLIENT = "session-client"
COMMANDS = "session/commands"
REPORTS = "session/reports"
STATUS = "session/status"  # the firmware's Will: a retained "offline"
COUNT = 10


def connected(board: Esp32Sim, broker: Broker) -> None:
    board.power_on()
    board.wait_until(broker.is_subscribed(COMMANDS), within=seconds(60))


@pytest.mark.parametrize("loss", ["power_off", "lose_registration"])
def test_a_lost_link_publishes_the_will_at_the_keepalive_not_at_the_loss(
    board: Esp32Sim, modem: Modem, broker: Broker, loss: str
) -> None:
    connected(board, broker)
    board.wait(seconds(1))
    if loss == "power_off":
        board.power_off()
    else:
        modem.lose_registration()
    lost = board.now
    board.wait(seconds(1))
    assert broker.find_message(STATUS) is None
    will = board.wait_until(broker.next_message(STATUS, "offline"), within=seconds(100))
    # The keepalive is 60 s: the broker gives up 90 s after the device's last packet.
    connection_id = broker.clients[-1].connection_id
    last = next(p for p in reversed(broker.received_packets) if p.connection_id == connection_id)
    assert will.at == last.at + seconds(90)
    assert will.at - lost > seconds(80)
    assert broker.retained[STATUS].text == "offline"
    assert board.link.warnings == []


def test_a_device_back_before_its_keepalive_takes_over_its_session_and_no_will_goes(
    board: Esp32Sim, modem: Modem, broker: Broker, console: Console
) -> None:
    connected(board, broker)
    board.wait(seconds(1))  # past the answers on their way: the chip off, they would be dropped
    board.power_off()
    board.wait(seconds(5))
    reconnected = broker.next_connection()
    again = console.line(r"mqtt_session: connected session_present=1")
    board.power_on()
    modem.power_on()  # the firmware pulses no PWRKEY
    client = board.wait_until(reconnected, within=seconds(60))
    assert client.session_present and client.connection_id == 2
    board.wait_until(again, within=seconds(5))
    # Past the old connection's keepalive: it went with the takeover, and its Will with it.
    board.wait(seconds(100))
    assert broker.find_message(STATUS) is None and STATUS not in broker.retained
    state = broker.session(CLIENT)
    assert state is not None and state.connected
    assert board.link.warnings == []


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


def test_an_empty_backlog_is_done_at_once(
    board: Esp32Sim, broker: Broker, console: Console
) -> None:
    connected(board, broker)
    done = console.line(r"mqtt_session: backlog done")
    broker.publish(COMMANDS, "backlog 0", qos=1)
    board.wait_until(done, within=seconds(5))
    assert broker.find_message(REPORTS) is None


def test_a_record_is_there_when_the_board_says_its_time(
    board: Esp32Sim, modem: Modem, broker: Broker, console: Console
) -> None:
    network = modem.network()
    connected(board, broker)
    network.set_path(rtt=ms(601))
    report = broker.next_message(REPORTS)
    broker.publish(COMMANDS, "backlog 1", qos=1)
    message = board.wait_until(report, within=seconds(10))
    assert board.now == message.at
    # And what the test does at that instant comes after what the network took at it.
    assert broker.find_message(REPORTS) is message


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


def logged(network: Internet, text: str, start: int) -> Condition[dict[str, object] | None]:
    """The first line of the network's transcript from event `start` on that contains `text`."""

    def find() -> dict[str, object] | None:
        return next((e for e in network.events[start:] if text in str(e.get("event", ""))), None)

    return Condition(find, f"a network event with {text!r}")


def test_a_black_hole_runs_out_the_keepalive_with_ppp_up_and_the_restore_answers_at_the_rtt(
    board: Esp32Sim, modem: Modem, broker: Broker, console: Console
) -> None:
    network = modem.network()
    connected(board, broker)
    network.set_path(rtt=ms(200))
    board.wait(seconds(1))
    no_pingresp = console.line(r"mqtt_client: No PING_RESP, disconnected")
    gone = console.line(r"mqtt_session: disconnected")
    network.set_path(drop=True)
    # esp-mqtt's keepalive of 60 s runs out: no PINGRESP came back; the PPP link never ended.
    board.wait_until(no_pingresp, within=seconds(200))
    board.wait_until(gone, within=seconds(1))
    assert modem.data_mode and network.ppp.lcp_open and network.ppp.ipcp_open
    lines = [str(e.get("event", "")) for e in network.events]
    assert any(line.startswith("net: path dropped") for line in lines)
    ends = ("ppp: the device ended the link", "ppp: the far end ended the link")
    assert not [line for line in lines if line in ends]
    start = len(network.events)
    reconnected = broker.next_connection()
    again = console.line(r"mqtt_session: connected session_present=1")
    network.set_path(rtt=ms(200))
    accepted = board.wait_until(
        logged(network, "→ 10.64.0.1:1883 accepted", start), within=seconds(30)
    )
    client = board.wait_until(reconnected, within=seconds(5))
    board.wait_until(again, within=seconds(5))
    # Nothing waits ahead of the SYN on the path: the device's CONNECT, sent once the SYN-ACK
    # came back, reaches the broker one RTT and some 7.5 ms of UART and firmware after the SYN.
    assert client.at - Duration(cast(int, accepted["t_ns"])) == about(ms(207), within=ms(5))
    assert board.link.warnings == []


def test_a_blocked_broker_port_drops_or_refuses_while_dns_answers(
    board: Esp32Sim, modem: Modem, broker: Broker, console: Console
) -> None:
    network = modem.network()
    connected(board, broker)
    board.wait(seconds(1))
    gone = console.line(r"mqtt_session: disconnected")
    network.block(1883, how="drop")
    board.wait_until(gone, within=seconds(200))
    asked = len(network.dns.asked)
    start = len(network.events)
    # Its next tries: DNS answers once lwIP's cache of the answer, whose TTL `Dns` gives as
    # 60 s, is over; its SYNs go nowhere.
    board.wait_until(
        Condition(lambda: len(network.dns.asked) > asked or None, "a DNS question"),
        within=seconds(120),
    )
    board.wait_until(logged(network, "dropped: port 1883 is blocked", start), within=seconds(30))
    assert len(broker.connection_attempts) == 1
    start = len(network.events)
    network.block(1883, how="refuse")
    board.wait_until(logged(network, "refused, port 1883 is blocked", start), within=seconds(30))
    assert len(broker.connection_attempts) == 1
    reconnected = broker.next_connection()
    network.unblock(1883)
    client = board.wait_until(reconnected, within=seconds(30))
    assert client.connection_id == 2
    assert board.link.warnings == []


# 8 times the 16 messages a consumer saw stall esp-mqtt over TLS. Queued while the device was
# offline, the burst goes right after CONNACK, each message a segment of its own, as far as the
# device's window allows. On this route it never stalled, up to 256 messages of 900 bytes, and in
# those trials the network's TCP sent nothing again: the modem's UART, at 115200 baud, takes about
# 11.5 kB/s to the device, slower than the firmware reads and prints, so lwIP's recvmbox never
# filled.
BURST, BURST_SIZE = 128, 200


def test_a_burst_of_queued_qos1_messages_is_delivered_and_acknowledged(
    board: Esp32Sim, modem: Modem, broker: Broker, console: Console
) -> None:
    connected(board, broker)
    reconnected = broker.next_connection()
    assert broker.disconnect(CLIENT)
    payloads = [f"burst {n:03d} ".ljust(BURST_SIZE, "x") for n in range(1, BURST + 1)]
    retained = "retained".ljust(BURST_SIZE, "r")
    broker.publish(COMMANDS, retained, qos=1, retain=True)
    for payload in payloads:
        broker.publish(COMMANDS, payload, qos=1)
    last = console.line(rf"data dup=0 qos=1 id=\d+ topic=session/commands payload={payloads[-1]}")
    board.wait_until(reconnected, within=seconds(30))
    board.wait_until(last, within=seconds(60))
    board.wait(seconds(2))
    for payload in payloads:
        [delivery] = [d for d in broker.deliveries if d.text == payload]
        assert not delivery.dup and delivery.acknowledged_at is not None
    # Queued, and again as the retained message each SUBSCRIBE matches: every one acknowledged.
    again = [d for d in broker.deliveries if d.text == retained and d.connection_id == 2]
    assert again and all(not d.dup and d.acknowledged_at is not None for d in again)
    state = broker.session(CLIENT)
    assert state is not None and state.queued == () and state.unacknowledged == ()
    assert [end.by for end in broker.connection_ends] == ["test"]
    assert board.link.warnings == []
