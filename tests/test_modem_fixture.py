"""modem_at on the LilyGO UART and GPIOs: real firmware, no PPP or external network."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.interfaces.console import Console
from esp32tl.interfaces.lines import Lines
from esp32tl.interfaces.modem import Modem
from esp32tl.units import hours, seconds

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "firmware" / "modem_at"


@pytest.mark.app_path("../firmware/modem_at")
@pytest.mark.board_profile("lilygo-t-a7670g-r2")
@pytest.mark.parametrize("initial", ["powered", "off", "restored"])
def test_modem_at_dialogue_and_changes_on_the_test_clock(
    board: Esp32Sim, modem: Modem, lines: Lines, console: Console, initial: str
) -> None:
    modem.signal(dbm=-73)
    if initial == "off":
        modem.power_off()
    elif initial == "restored":
        modem.lose_power()
        modem.restore_power()
    board.power_on()
    if initial == "powered":
        console.expect_exact("modem_at: powered off RI=0")
    console.expect_exact("modem_at: powered on RI=1")
    console.expect_exact("modem_at: dialogue complete")
    assert [c.text for c in modem.commands[:17]] == [
        "AT",
        "ATE0",
        "AT+CPIN?",
        "AT+CREG?",
        "AT+CGREG?",
        "AT+CEREG?",
        "AT+CSQ",
        'AT+CGDCONT=1,"IP","internet"',
        "AT+CFUN=1",
        "AT+CTZU=1",
        "AT+COPS?",
        "AT+COPS=3,2",
        "AT+COPS?",
        "AT+CCLK?",
        "AT+UNIMPLEMENTED",
        "ATD*99#",
        'AT+CGDATA="PPP",1',
    ]
    assert all(c.response.endswith(b"OK\r\n") for c in modem.commands[:15])
    assert b"+CEREG: 0,1" in modem.commands[5].response
    assert modem.commands[14].fallback
    assert not modem.data_mode
    expected = [1, 0, 1] if initial == "powered" else [0, 1]
    assert [(e.by, e.level) for e in lines.events() if e.name == "modem.ri"] == [
        ("model", level) for level in expected
    ]
    if initial == "powered":
        pwrkey = [e for e in lines.events() if e.name == "modem.pwrkey"]
        off_start, off_end, on_start, on_end = pwrkey[-4:]
        assert [e.level for e in (off_start, off_end, on_start, on_end)] == [1, 0, 1, 0]
        assert on_start.t - off_end.t >= seconds(2)
    modem.signal(dbm=-101)
    modem.lose_registration()
    console.expect_exact("+CSQ: 6,99", timeout=seconds(2))
    console.expect_exact("+CEREG: 0,0", timeout=seconds(2))
    modem.register()
    console.expect_exact("+CEREG: 0,1", timeout=seconds(2))
    modem.lose_power()
    console.expect_exact("modem_at: RI=0", timeout=seconds(3))
    console.expect_exact("modem_at: AT+CSQ => TIMEOUT", timeout=seconds(3))
    assert not modem.powered
    assert board.link.warnings == []
    commands = board.properties()["routes"]  # the structured report keeps every command
    assert isinstance(commands, dict)
    assert len(commands["uart.modem"]["commands"]) == len(modem.commands)
    assert "AT+UNIMPLEMENTED' (fallback OK)" in board.report()


@pytest.mark.app_path("../firmware/modem_at")
@pytest.mark.board_profile("lilygo-t-a7670g-r2")
def test_the_firmware_reads_the_five_digit_code_and_the_network_s_time(
    board: Esp32Sim, modem: Modem, console: Console
) -> None:
    modem.operator(name="Example Mobile", numeric="26001", short="EXM", act=7)
    board.power_on()
    board.wait_until(console.line(r'\+COPS: 0,0,"Example Mobile",7'), within=seconds(15))
    board.wait_until(console.line(r'\+COPS: 0,2,"26001",7'), within=seconds(1))
    line = board.wait_until(console.line(r'\+CCLK: "'), within=seconds(1))
    command = next(c for c in modem.commands if c.text == "AT+CCLK?")
    assert f'+CCLK: "26/01/01,12:00:{int(command.at.seconds):02d}+00"' in line.text
    assert not command.fallback


@pytest.mark.app_path("../firmware/modem_at")
@pytest.mark.board_profile("lilygo-t-a7670g-r2")
def test_the_firmware_reads_a_wrong_clock_and_then_the_network_s(
    board: Esp32Sim, modem: Modem, console: Console
) -> None:
    modem.clock(wrong_by=hours(-1), zone=hours(2))
    board.power_on()
    line = board.wait_until(console.line(r'\+CCLK: "'), within=seconds(15))
    first = next(c for c in modem.commands if c.text == "AT+CCLK?")
    assert f'+CCLK: "26/01/01,13:00:{int(first.at.seconds):02d}+08"' in line.text
    board.wait_until(console.line("modem_at: dialogue complete"), within=seconds(1))
    modem.clock_set_by_network()
    board.wait_until(console.line(r'\+CCLK: "26/01/01,12:00:\d\d\+00"'), within=seconds(2))


@pytest.mark.app_path("../firmware/modem_at")
@pytest.mark.board_profile("lilygo-t-a7670g-r2")
def test_the_firmware_reads_an_unset_clock_with_no_zone_and_it_runs_on(
    board: Esp32Sim, modem: Modem, console: Console
) -> None:
    modem.clock_not_set(reads=datetime(2000, 1, 1, tzinfo=UTC))
    board.power_on()
    line = board.wait_until(console.line(r'\+CCLK: "'), within=seconds(15))
    first = next(c for c in modem.commands if c.text == "AT+CCLK?")
    assert f'+CCLK: "00/01/01,00:00:{int(first.at.seconds):02d}"' in line.text
    board.wait_until(console.line("modem_at: dialogue complete"), within=seconds(1))
    modem.clock_set_by_network()
    board.wait_until(console.line(r'\+CCLK: "26/01/01,12:00:\d\d\+00"'), within=seconds(2))


def test_two_firmware_runs_produce_identical_modem_taps(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(f"""
        import pytest
        pytestmark = [pytest.mark.app_path({str(APP)!r}),
                      pytest.mark.board_profile("lilygo-t-a7670g-r2")]

        @pytest.mark.parametrize("run", [0, 1])
        def test_dialogue(board, modem, console, run):
            modem.signal(dbm=-73)
            modem.tap(f"modem-{{run}}.jsonl")
            board.power_on()
            console.expect_exact("modem_at: dialogue complete")
    """)
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=2)
    recordings = [(pytester.path / f"modem-{i}.jsonl").read_text().splitlines() for i in range(2)]
    assert recordings[0][1:] == recordings[1][1:]
    header = json.loads(recordings[0][0])
    assert (header["source"], header["interface"]) == ("model:modem", "uart.modem")


def test_fixture_refuses_a_missing_route_and_model_selection_after_power_on(
    pytester: pytest.Pytester,
) -> None:
    pytester.makepyfile(f"""
        import pytest
        pytestmark = pytest.mark.app_path({str(APP)!r})

        def test_missing(modem):
            pass

        @pytest.mark.board_profile("lilygo-t-a7670g-r2")
        def test_late(board, modem):
            board.power_on()
            modem.signal(dbm=-73)
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


@pytest.mark.app_path("../firmware/modem_at")
@pytest.mark.board_profile("lilygo-t-a7670g-r2")
def test_the_modem_keeps_its_power_through_a_reset_and_loses_it_with_the_board(
    board: Esp32Sim, modem: Modem, console: Console
) -> None:
    modem.signal(dbm=-73)
    board.power_on()
    console.expect_exact("modem_at: dialogue complete", timeout=seconds(15))
    # EN resets the chip alone: the firmware finds the modem on, and switches it off and on
    board.reset()
    console.expect_exact("modem_at: powered off RI=0", timeout=seconds(10))
    console.expect_exact("modem_at: powered on RI=1", timeout=seconds(10))
    console.expect_exact("modem_at: dialogue complete", timeout=seconds(15))
    # with the board's supply the modem goes off, and comes back off: it waits for PWRKEY
    board.power_off()
    assert not modem.powered
    board.wait(seconds(1))
    board.power_on()
    assert not modem.powered
    console.expect_exact("modem_at: powered on RI=1", timeout=seconds(10))
    console.expect_exact("modem_at: dialogue complete", timeout=seconds(15))
    assert modem.powered
    # the modem's answers on their way when the chip reset and when it lost its power: no UART
    # took them
    assert {(w.kind, w.pin) for w in board.link.warnings} == {("unrouted_rx", 27)}
