"""modem_recovery through esp_modem and PPP on the LilyGO wires: real firmware at the pin."""

from pathlib import Path

import pytest

from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.interfaces.console import Console
from esp32tl.interfaces.lines import LineEvent, Lines
from esp32tl.interfaces.modem import Modem
from esp32tl.units import Duration, ms, seconds

pytestmark = [
    pytest.mark.app_path("../firmware/modem_recovery"),
    pytest.mark.board_profile("lilygo-t-a7670g-r2"),
]


def firmware_pulses(lines: Lines, name: str, since: Duration) -> list[tuple[int, Duration]]:
    """Each (level, start) the firmware drove `name` to from `since` on."""
    changes: list[LineEvent] = [
        e for e in lines.events() if e.name == name and e.by == "firmware" and e.t >= since
    ]
    return [(e.level or 0, e.t) for e in changes]


@pytest.mark.parametrize("pwrkey", [True, False])
def test_recovers_a_hung_modem(
    board: Esp32Sim, modem: Modem, lines: Lines, console: Console, pwrkey: bool
) -> None:
    network = modem.network()
    board.power_on()
    board.wait_until(console.line("modem_recovery: connected"), within=seconds(60))
    assert modem.data_mode and modem.restarts == ()

    modem.hang(pwrkey=pwrkey)
    hung_at = board.now
    assert modem.hung and not modem.data_mode and modem.powered
    board.wait_until(console.line("modem_recovery: no answer"), within=seconds(60))
    high = board.wait_until(console.line(r"modem_recovery: hung RI=(\d)"), within=seconds(60))
    assert high[1] == "1"  # RI stays high on a hung modem
    assert modem.hung
    # the firmware's AT commands stay in the history, with no answer
    silent = [c for c in modem.commands if c.at >= hung_at]
    assert "AT" in [c.text for c in silent]
    assert all(c.response == b"" for c in silent)
    assert "'AT' (no answer)" in board.report()

    after = board.wait_until(
        console.line(r"modem_recovery: after PWRKEY RI=(\d)"), within=seconds(10)
    )
    assert after[1] == ("0" if pwrkey else "1")
    recovered = board.wait_until(
        console.line(r"modem_recovery: recovered RI=1"), within=seconds(30)
    )
    assert not modem.hung and modem.powered and modem.registered
    assert [r.cause for r in modem.restarts] == (["pwrkey"] if pwrkey else ["reset"])
    requests = network.ntp.requests
    board.wait_until(console.line("modem_recovery: connected"), within=seconds(60))
    assert modem.data_mode and modem.registered and network.ppp.ipcp_open
    board.wait_until(lambda: network.ntp.requests > requests, within=seconds(5))

    # the procedure's pulses, on the board's clock
    key = firmware_pulses(lines, "modem.pwrkey", hung_at)
    assert [level for level, _ in key] == [1, 0, 1, 0]
    assert key[1][1] - key[0][1] >= ms(2999)
    enable = firmware_pulses(lines, "board.power_enable", hung_at)
    assert [level for level, _ in enable] == [0, 1]
    assert ms(999) <= enable[1][1] - enable[0][1] <= ms(1001)
    reset = firmware_pulses(lines, "modem.reset", hung_at)
    assert [level for level, _ in reset] == [1, 0]
    assert ms(2599) <= reset[1][1] - reset[0][1] <= ms(2601)
    assert ms(9999) <= key[2][1] - reset[1][1] <= ms(10001)
    assert ms(99) <= key[3][1] - key[2][1] <= ms(101)
    assert key[0][1] < enable[0][1] < reset[0][1] < key[2][1] <= recovered.at
    assert modem.restarts[0].at == (key[1][1] if pwrkey else reset[1][1]) + ms(1)


@pytest.mark.parametrize("pwrkey", [True, False])
def test_recovers_a_modem_that_hangs_while_the_firmware_dials(
    board: Esp32Sim, modem: Modem, console: Console, pwrkey: bool
) -> None:
    network = modem.network()
    board.power_on()
    board.wait_until(lambda: network.ppp.lcp_open, within=seconds(60))
    assert not network.ppp.ipcp_open
    modem.hang(pwrkey=pwrkey)

    board.wait_until(console.line("modem_recovery: recovered RI=1"), within=seconds(120))
    assert not modem.hung and modem.registered
    board.wait_until(console.line("modem_recovery: connected"), within=seconds(60))
    assert modem.data_mode and network.ppp.ipcp_open
    requests = network.ntp.requests
    board.wait_until(lambda: network.ntp.requests > requests, within=seconds(5))
    # one recovery: the modem answers from then on, and the firmware does not restart it again
    board.wait(seconds(30))
    assert [r.cause for r in modem.restarts] == (["pwrkey"] if pwrkey else ["reset"])
    assert len(console.lines("modem_recovery: hung")) == 1


@pytest.mark.parametrize(
    ("terminate", "noticed"),
    [(False, "modem_recovery: no answer"), (True, "modem_recovery: lost ip")],
)
def test_redials_after_a_dropped_call(
    board: Esp32Sim, modem: Modem, console: Console, tmp_path: Path, terminate: bool, noticed: str
) -> None:
    network = modem.network()
    tap = modem.tap(tmp_path / "modem.jsonl")
    board.power_on()
    board.wait_until(console.line("modem_recovery: connected"), within=seconds(60))
    board.wait_until(lambda: network.ntp.requests > 0, within=seconds(5))

    dropped_at = board.now
    modem.drop_call(terminate=terminate)
    assert modem.registered and not modem.data_mode and modem.powered
    line = board.wait_until(console.line("modem_recovery: (no answer|lost ip)"), within=seconds(60))
    assert line.text == noticed
    assert modem.registered and not modem.data_mode
    if terminate:
        # lwIP's FSM_DEFTIMEOUT at the pin, between the Terminate-Request and IP_EVENT_PPP_LOST_IP
        assert seconds(6) <= line.at - dropped_at < seconds(7)
    else:
        # NO CARRIER alone goes unnoticed: the firmware's own requests time out
        assert line.at - dropped_at > seconds(10)

    back = board.wait_until(console.line(r"modem_recovery: command mode"), within=seconds(60))
    assert "+CEREG: 0,1" in "\n".join(entry.text for entry in console.lines(since=back.at))
    assert modem.registered
    requests = network.ntp.requests
    board.wait_until(console.line("modem_recovery: connected"), within=seconds(60))
    assert modem.data_mode and modem.registered and network.ppp.ipcp_open
    board.wait_until(lambda: network.ntp.requests > requests, within=seconds(5))

    # nothing took the modem down: every command in its history has an answer, and no restart
    assert not modem.restarts
    assert all(c.response for c in modem.commands)
    dials = [c.response for c in modem.commands if c.text == "ATD*99#"]
    assert dials == [b"\r\nCONNECT\r\n", b"\r\nCONNECT\r\n"]
    # what crossed the wire: lwIP's Terminate-Ack (code 6, escaped) only to a Terminate-Request
    after = [(d, data) for t, d, data in tap.events if t >= dropped_at.ns]
    assert b"".join(data for d, data in after if d == "in").find(b"\r\nNO CARRIER\r\n") >= 0
    assert any(d == "out" and b"\xc0!}&" in data for d, data in after) is terminate


@pytest.mark.parametrize("delay", [None, 0, 5, 13])
def test_a_call_dropped_while_the_firmware_leaves_data_mode_restarts_nothing(
    board: Esp32Sim, modem: Modem, console: Console, tmp_path: Path, delay: int | None
) -> None:
    network = modem.network()
    tap = modem.tap(tmp_path / "modem.jsonl")
    board.power_on()
    board.wait_until(console.line("modem_recovery: connected"), within=seconds(60))
    board.wait_until(lambda: network.ntp.requests > 0, within=seconds(5))
    # NTP goes silent, so the firmware leaves data mode with the call still up
    network.ntp.go_silent()
    board.wait_until(console.line("modem_recovery: no answer"), within=seconds(60))
    leaving = board.now
    if delay is not None:
        board.wait(seconds(delay))
        assert modem.data_mode
        modem.drop_call()
    board.wait_until(console.line("modem_recovery: command mode"), within=seconds(60))
    assert not modem.restarts and not console.lines("modem_recovery: hung")
    sent = [data for t, d, data in tap.events if t >= leaving.ns and d == "out"]
    # with a call to escape from, +++ gets its OK, and AT needs no newline before it
    assert (b"\nAT\r" in sent) is (delay is not None)
    assert b"AT\r" in sent
