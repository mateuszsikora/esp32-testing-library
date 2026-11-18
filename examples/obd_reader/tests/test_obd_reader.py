"""An OBD-II reader over Bluetooth Classic: it searches for its ELM327 adapter by name, pairs with
its PIN, opens SPP and asks the vehicle what the vehicle's answers let it ask."""

from itertools import pairwise
from typing import Any

import pytest
from elm327 import Elm327, Vehicle

from esp32tl import BluetoothDevice, Board, SppPeer
from esp32tl.units import ms, seconds

pytestmark = pytest.mark.app_path("..")

NAME = "OBDII"
PIN = "1234"
ADDRESS = "00:00:00:00:5B:01"
# One search: an inquiry of 4 units of 1.28 s.
SEARCH = seconds(4 * 1.28)
# From the adapter found to the session open: SDP, the page, the pairing, RFCOMM.
CONNECT_TIME = seconds(1)
# The reader's patience with an adapter; its pauses between two rounds of readings, between two
# asks of a vehicle that does not answer, and after a failed attempt before the next search.
ANSWER_TIMEOUT = seconds(2)
READ_PERIOD = seconds(1)
RETRY = seconds(2)
SEARCH_PAUSE = seconds(5)
# The firmware counts its waits in FreeRTOS ticks.
TICK = ms(10)
# The commands of a session up to the first reading.
OPENING = ["ATZ", "ATE0", "ATSP0", "0100"]
READING = r"^obd_reader:(?P<values>( \w+=\S+)+)$"


def adapter_on_the_air(board: Board, adapter: Elm327, *, pin: str = PIN) -> SppPeer:
    return SppPeer(board.bluetooth.air, name=NAME, pin=pin, source=adapter, address=ADDRESS)


async def hang_up(peer: SppPeer) -> None:
    """The adapter ends its links itself, as one that drops its connection does."""
    for connection in list(peer.device.connections.values()):
        await connection.disconnect()


def next_reading(board: Board) -> dict[str, str]:
    line = board.wait_until(board.console.line(READING), within=READ_PERIOD + seconds(1))
    return dict(value.split("=") for value in line["values"].split())


def test_without_the_adapter_the_reader_keeps_searching(board: Board) -> None:
    bt = board.bluetooth
    # Found by every search, and not the adapter.
    BluetoothDevice(bt.air, name="phone", class_of_device=0x5A020C, address="00:00:00:00:5C:01")
    searching = board.console.line(f"obd_reader: searching for {NAME}")
    board.power_on()
    starts = [board.wait_until(searching, within=seconds(5)).at]
    for _ in range(3):
        starts.append(board.wait_until(searching, within=SEARCH + seconds(0.1)).at)
    for before, after in pairwise(starts):
        assert SEARCH <= after - before <= SEARCH + seconds(0.1)
    assert board.console.lines("obd_reader: found") == []
    assert bt.pairings() == []

    # Plugged in during a search: the next one finds it.
    adapter = Elm327()
    peer = adapter_on_the_air(board, adapter)
    plugged = board.now
    found = board.wait_until(
        board.console.line(f"obd_reader: found {NAME} at {ADDRESS.lower()}"),
        within=SEARCH + seconds(0.1),
    )
    assert found.at - plugged <= SEARCH
    board.wait_until(board.console.line("obd_reader: connected"), within=CONNECT_TIME)
    assert peer.channel_open


def test_with_the_adapter_the_session_opens(board: Board) -> None:
    bt = board.bluetooth
    adapter = Elm327()
    peer = adapter_on_the_air(board, adapter)
    board.power_on()
    board.wait_until(board.console.line("obd_reader: connected"), within=seconds(5))
    assert peer.channel_open and peer.refused == []
    [pairing] = bt.pairings()
    assert (pairing.method, pairing.peer, pairing.succeeded) == ("legacy", ADDRESS, True)
    assert pairing.initiator

    board.wait_until(board.console.line("obd_reader: adapter ELM327 v1.5"), within=seconds(1))
    assert next_reading(board) == {"rpm": "850", "speed": "0", "coolant": "90"}
    assert adapter.commands()[: len(OPENING)] == OPENING
    # The first 0100 came back after SEARCHING..., and the reader took the answer below it.
    assert adapter.commands()[len(OPENING)] == "010C"
    assert board.console.lines("no answer from the vehicle") == []


@pytest.mark.parametrize(
    ("vehicle", "asked", "reading"),
    [
        (Vehicle(), ["010C", "010D", "0105"], {"rpm": "850", "speed": "0", "coolant": "90"}),
        (Vehicle(rpm=None, coolant=None, speed=42), ["010D"], {"speed": "42"}),
        (Vehicle(speed=None, rpm=3000.25), ["010C", "0105"], {"rpm": "3000", "coolant": "90"}),
    ],
    ids=["everything", "speed only", "no speed"],
)
def test_the_vehicle_s_answer_decides_what_the_reader_asks_next(
    board: Board, vehicle: Vehicle, asked: list[str], reading: dict[str, str]
) -> None:
    adapter = Elm327(vehicle)
    adapter_on_the_air(board, adapter)
    board.power_on()
    supports = board.wait_until(
        board.console.line(r"^obd_reader: the vehicle supports(?P<names>( \w+)*)$"),
        within=seconds(5),
    )
    assert supports["names"].split() == list(reading)
    for _ in range(3):
        assert next_reading(board) == reading
    # At the adapter: the opening, then a round of the PIDs the vehicle has, three times.
    assert adapter.commands()[: len(OPENING) + 3 * len(asked)] == OPENING + 3 * asked


def test_the_readings_follow_the_vehicle(board: Board) -> None:
    vehicle = Vehicle()
    adapter_on_the_air(board, Elm327(vehicle))
    board.power_on()
    assert next_reading(board)["speed"] == "0"
    # Between two rounds: the next one reads the new values.
    vehicle.rpm, vehicle.speed, vehicle.coolant = 2400.0, 87, -12
    assert next_reading(board) == {"rpm": "2400", "speed": "87", "coolant": "-12"}


def test_with_the_ignition_off_the_reader_asks_again_until_the_vehicle_answers(
    board: Board,
) -> None:
    vehicle = Vehicle(ignition=False)
    adapter = Elm327(vehicle)
    adapter_on_the_air(board, adapter)
    no_answer = board.console.line("obd_reader: no answer from the vehicle: UNABLE TO CONNECT")
    board.power_on()
    first = board.wait_until(no_answer, within=seconds(5))
    for _ in range(2):
        again = board.wait_until(no_answer, within=RETRY + seconds(0.2))
        assert RETRY <= again.at - first.at <= RETRY + seconds(0.2)
        first = again
    # Nothing but the opening, then 0100 again and again.
    assert adapter.commands() == [*OPENING, "0100", "0100"]

    vehicle.ignition = True
    board.wait_until(
        board.console.line("obd_reader: the vehicle supports"), within=RETRY + seconds(1)
    )
    assert next_reading(board) == {"rpm": "850", "speed": "0", "coolant": "90"}
    assert adapter.commands()[:6] == [*OPENING, "0100", "0100"]
    assert adapter.commands()[6:10] == ["0100", "010C", "010D", "0105"]


def test_with_another_pin_the_reader_opens_no_session_and_tries_again_later(
    board: Board,
) -> None:
    bt = board.bluetooth
    adapter = Elm327()
    peer = adapter_on_the_air(board, adapter, pin="0000")
    failed = board.console.line("obd_reader: could not connect")
    searching = board.console.line(f"obd_reader: searching for {NAME}")
    board.power_on()
    board.wait_until(searching, within=seconds(5))
    attempts = [board.wait_until(failed, within=seconds(5)).at]
    for _ in range(2):
        again = board.wait_until(searching, within=SEARCH_PAUSE + seconds(0.1))
        assert abs(again.at - attempts[-1] - SEARCH_PAUSE) <= TICK
        attempts.append(board.wait_until(failed, within=CONNECT_TIME).at)
    # Each attempt is one legacy pairing that failed: AUTHENTICATION_FAILURE, which Bluedroid
    # gives the firmware as ESP_BT_STATUS_AUTH_FAILURE (9).
    pairings = bt.pairings()
    assert [(p.method, p.status) for p in pairings] == [("legacy", 0x05)] * len(attempts)
    assert len(board.console.lines(r"^obd_reader: pairing failed \(status 9\)$")) == len(attempts)
    assert board.console.lines("obd_reader: connected") == []
    assert not peer.channel_open and adapter.requests == []


def test_an_adapter_that_stops_answering_is_dropped_and_found_again(board: Board) -> None:
    bt = board.bluetooth
    adapter = Elm327()
    peer = adapter_on_the_air(board, adapter)
    board.power_on()
    next_reading(board)
    adapter.silent = True
    disconnected = board.console.line("obd_reader: disconnected")
    searching = board.console.line(f"obd_reader: searching for {NAME}")
    stopped = board.wait_until(
        board.console.line("obd_reader: the adapter stopped answering"), within=seconds(4)
    )
    # Counted from the instant it wrote the command the adapter heard last.
    last = adapter.requests[-1]
    assert abs(stopped.at - last.at - ANSWER_TIMEOUT) <= TICK
    assert board.wait_until(disconnected, within=seconds(1)).at - stopped.at < ms(100)
    assert board.wait_until(searching, within=seconds(1)).at - stopped.at < ms(100)
    assert not peer.channel_open

    # Answering again: found by the next search, with the key of the first pairing.
    adapter.silent = False
    asked = len(adapter.requests)
    board.wait_until(board.console.line("obd_reader: connected"), within=seconds(5))
    assert next_reading(board) == {"rpm": "850", "speed": "0", "coolant": "90"}
    assert adapter.commands()[asked : asked + len(OPENING)] == OPENING
    assert len(bt.pairings()) == 1
    assert board.console.lines("pairing failed") == []


def test_an_adapter_that_hangs_up_between_two_rounds_gets_a_new_session(board: Board) -> None:
    adapter = Elm327()
    peer = adapter_on_the_air(board, adapter)
    board.power_on()
    last = board.wait_until(board.console.line(READING), within=seconds(5))
    disconnected = board.console.line("obd_reader: disconnected")
    connected = board.console.line("obd_reader: connected")
    asked = len(adapter.requests)
    board.bluetooth.air.start(hang_up(peer))
    board.wait_until(disconnected, within=seconds(1))
    # Found and connected again, with its key, before the reader's pause after `last` is over.
    again = board.wait_until(connected, within=seconds(1))
    assert again.at - last.at < READ_PERIOD
    assert next_reading(board) == {"rpm": "850", "speed": "0", "coolant": "90"}
    # The new session starts from ATZ: nothing of the old one is carried over.
    assert adapter.commands()[asked : asked + len(OPENING)] == OPENING
    assert len(board.bluetooth.pairings()) == 1
    assert board.console.lines("stopped answering") == []


@pytest.mark.parametrize(
    ("adapter", "gives_up"),
    [
        ({"version": "OK"}, "obd_reader: not an ELM327 adapter: OK"),
        (
            {"vehicle": Vehicle(rpm=None, speed=None, coolant=None)},
            "obd_reader: the vehicle supports none of rpm speed coolant",
        ),
    ],
    ids=["no ELM327", "nothing the reader reads"],
)
def test_with_nothing_to_read_the_reader_disconnects_and_searches_again_later(
    board: Board, adapter: dict[str, Any], gives_up: str
) -> None:
    adapter_on_the_air(board, Elm327(**adapter))
    gave_up = board.console.line(gives_up)
    disconnected = board.console.line("obd_reader: disconnected")
    searching = board.console.line(f"obd_reader: searching for {NAME}")
    board.power_on()
    board.wait_until(searching, within=seconds(5))
    for _ in range(2):
        board.wait_until(gave_up, within=seconds(5))
        closed = board.wait_until(disconnected, within=seconds(0.1))
        again = board.wait_until(searching, within=SEARCH_PAUSE + seconds(0.1))
        assert abs(again.at - closed.at - SEARCH_PAUSE) <= TICK
