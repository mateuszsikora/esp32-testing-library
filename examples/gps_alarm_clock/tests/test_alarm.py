"""A destination in flash, a moving receiver on the UART, and a buzzer on a wire."""

import pytest

from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.flash import i32, u32
from esp32tl.interfaces.console import Console
from esp32tl.interfaces.gnss import Gnss
from esp32tl.interfaces.lines import Lines
from esp32tl.units import EAST, WEST, Position, km_h, m, seconds

pytestmark = [pytest.mark.app_path(".."), pytest.mark.board_profile("gps-alarm-clock")]
TARGET = Position(52.229677, 21.012233)
BUZZER = "alarm.buzzer"


@pytest.fixture
def alarm(board: Esp32Sim) -> Esp32Sim:
    board.nvs({"alarm": {"lat_e6": i32(52229677), "lon_e6": i32(21012233), "radius_m": u32(100)}})
    return board


def test_far_away_the_buzzer_stays_off(alarm: Esp32Sim, gnss: Gnss, lines: Lines) -> None:
    gnss.fix(at=TARGET.moved(EAST, m(1000)))
    alarm.power_on()
    alarm.wait(seconds(5))
    assert lines.level(BUZZER) == 0
    assert not any(event.level == 1 for event in lines.events())


def test_moving_into_the_radius_turns_it_on(alarm: Esp32Sim, gnss: Gnss, lines: Lines) -> None:
    gnss.fix(at=TARGET.moved(EAST, m(150)))
    alarm.power_on()
    alarm.wait(seconds(3))
    assert lines.level(BUZZER) == 0

    gnss.move(heading=WEST, speed=km_h(36))  # 10 m/s; reach the 100 m radius in 5 s.
    alarm.wait(seconds(4))
    assert lines.level(BUZZER) == 0
    alarm.wait_until(lambda: lines.level(BUZZER) == 1, within=seconds(3))
    assert gnss.position is not None
    assert gnss.position.distance_to(TARGET) <= m(100)


def test_moving_out_turns_it_off(alarm: Esp32Sim, gnss: Gnss, lines: Lines) -> None:
    gnss.fix(at=TARGET)
    alarm.power_on()
    alarm.wait_until(lambda: lines.level(BUZZER) == 1, within=seconds(2))

    gnss.move(heading=EAST, speed=km_h(36))  # Reach the edge in 10 s.
    alarm.wait(seconds(9))
    assert lines.level(BUZZER) == 1
    alarm.wait_until(lambda: lines.level(BUZZER) == 0, within=seconds(3))
    assert gnss.position is not None
    assert gnss.position.distance_to(TARGET) > m(100)


def test_without_a_fix_it_stays_off(alarm: Esp32Sim, gnss: Gnss, lines: Lines) -> None:
    gnss.lose_fix()
    alarm.power_on()
    alarm.wait(seconds(5))
    assert lines.level(BUZZER) == 0
    assert not any(event.level == 1 for event in lines.events())


def test_losing_the_fix_turns_it_off(alarm: Esp32Sim, gnss: Gnss, lines: Lines) -> None:
    gnss.fix(at=TARGET)
    alarm.power_on()
    alarm.wait_until(lambda: lines.level(BUZZER) == 1, within=seconds(2))
    gnss.lose_fix()
    alarm.wait_until(lambda: lines.level(BUZZER) == 0, within=seconds(2))
    alarm.wait(seconds(3))
    assert lines.level(BUZZER) == 0


def test_silence_expires_the_last_fix(alarm: Esp32Sim, gnss: Gnss, lines: Lines) -> None:
    gnss.fix(at=TARGET)
    alarm.power_on()
    alarm.wait_until(lambda: lines.level(BUZZER) == 1, within=seconds(2))
    gnss.go_silent()
    alarm.wait_until(lambda: lines.level(BUZZER) == 0, within=seconds(3))


@pytest.mark.parametrize("radius", [50, 200])
def test_the_radius_comes_from_nvs(board: Esp32Sim, gnss: Gnss, lines: Lines, radius: int) -> None:
    # Another destination, in the southern and western hemispheres, catches hardcoded targets.
    target = Position(-34.6037, -58.3816)
    board.nvs(
        {"alarm": {"lat_e6": i32(-34603700), "lon_e6": i32(-58381600), "radius_m": u32(radius)}}
    )
    gnss.fix(at=target.moved(EAST, m(100)))
    board.power_on()
    board.wait(seconds(3))
    assert lines.level(BUZZER) == int(radius > 100)


def test_missing_configuration_keeps_it_off(
    board: Esp32Sim, gnss: Gnss, lines: Lines, console: Console
) -> None:
    gnss.fix(at=TARGET)
    board.power_on()
    console.expect_exact("gps_alarm_clock: invalid or missing NVS alarm configuration")
    board.wait(seconds(3))
    assert lines.level(BUZZER) == 0
    assert not any(event.level == 1 for event in lines.events())
