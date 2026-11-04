"""modem_recovery through esp_modem and PPP on the LilyGO wires: real firmware at the pin."""

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
    modem.network()
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
    board.wait_until(console.line("modem_recovery: connected"), within=seconds(60))
    assert modem.data_mode and modem.registered

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
