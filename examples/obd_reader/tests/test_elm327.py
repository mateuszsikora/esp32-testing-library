"""The adapter model answers as an ELM327 does, without a board: echo, prompt, PIDs and timing."""

from elm327 import VERSION, Elm327, Vehicle

from esp32tl import Timed
from esp32tl.units import Duration, ms, seconds

START = ms(0)


def ask(adapter: Elm327, at: Duration, command: str) -> list[Timed]:
    return list(adapter.receive(at, command.encode() + b"\r"))


def test_echo_is_on_after_a_reset_and_off_after_ate0() -> None:
    adapter = Elm327()
    assert ask(adapter, START, "ATZ") == [(ms(500), b"ATZ\r" + VERSION.encode() + b"\r\r>")]
    assert ask(adapter, START, "ATE0") == [(ms(50), b"ATE0\rOK\r\r>")]
    assert ask(adapter, START, "ATSP0") == [(ms(50), b"OK\r\r>")]
    assert ask(adapter, START, "AT NONSENSE") == [(ms(50), b"?\r\r>")]
    assert adapter.commands() == ["ATZ", "ATE0", "ATSP0", "ATNONSENSE"]


def test_0100_lists_the_pids_the_vehicle_answers() -> None:
    adapter = Elm327(Vehicle(rpm=1000.0, speed=None, coolant=None), echo=False)
    # PID 0C alone: bit 32 - 12 = 20, the third byte's 0x10.
    [(_, answer)] = ask(adapter, START, "0100")
    assert answer == b"SEARCHING...\r41 00 00 10 00 00\r\r>"
    [(_, answer)] = ask(adapter, START, "010C")
    assert answer == b"41 0C 0F A0\r\r>"
    [(_, answer)] = ask(adapter, START, "010D")
    assert answer == b"NO DATA\r\r>"


def test_the_readings_are_encoded_as_obd_ii_has_them() -> None:
    vehicle = Vehicle(rpm=3000.25, speed=255, coolant=-40)
    assert vehicle.supported() == {0x0C: bytes([0x2E, 0xE1]), 0x0D: b"\xff", 0x05: b"\x00"}
    adapter = Elm327(Vehicle(), echo=False)
    assert ask(adapter, START, "0100")[0][1] == b"SEARCHING...\r41 00 08 18 00 00\r\r>"


def test_the_adapter_searches_for_the_protocol_after_atz_and_atsp() -> None:
    adapter = Elm327(echo=False)
    assert ask(adapter, START, "010D")[0][1] == b"SEARCHING...\r41 0D 00\r\r>"
    assert ask(adapter, START, "010D")[0][1] == b"41 0D 00\r\r>"
    ask(adapter, START, "ATSP0")
    assert ask(adapter, START, "0100")[0][1].startswith(b"SEARCHING...\r41 00 ")
    ask(adapter, START, "ATZ")
    ask(adapter, START, "ATE0")
    assert ask(adapter, START, "0100")[0][1].startswith(b"SEARCHING...\r41 00 ")
    assert ask(adapter, START, "0100")[0][1].startswith(b"41 00 ")


def test_another_adapter_names_itself_otherwise() -> None:
    adapter = Elm327(version="OK", echo=False)
    assert ask(adapter, START, "ATZ")[0][1] == b"OK\r\r>"


def test_with_the_ignition_off_the_vehicle_does_not_answer() -> None:
    vehicle = Vehicle(ignition=False)
    adapter = Elm327(vehicle, echo=False)
    for _ in range(2):
        [(_, answer)] = ask(adapter, START, "0100")
        assert answer == b"SEARCHING...\rUNABLE TO CONNECT\r\r>"
    # Found at the first request once the ignition is on.
    vehicle.ignition = True
    assert ask(adapter, START, "0100")[0][1].startswith(b"SEARCHING...\r41 00 ")


def test_commands_split_across_writes_and_a_silent_adapter() -> None:
    adapter = Elm327(echo=False)
    assert list(adapter.receive(START, b"AT")) == []
    assert list(adapter.receive(seconds(1), b"E0\r")) == [(seconds(1.05), b"OK\r\r>")]
    adapter.silent = True
    assert ask(adapter, seconds(2), "0100") == []
    assert [(r.at, r.command) for r in adapter.requests] == [
        (seconds(1), "ATE0"),
        (seconds(2), "0100"),
    ]
