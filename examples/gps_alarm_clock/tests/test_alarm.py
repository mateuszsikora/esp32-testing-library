"""A destination in flash, a moving receiver on the UART, and a buzzer on a wire."""

import pytest

from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.flash import i32, u32
from esp32tl.interfaces.console import Console
from esp32tl.interfaces.gnss import Gnss
from esp32tl.interfaces.lines import Lines, OutputLine
from esp32tl.units import EAST, WEST, Distance, Position, km_h, m, seconds

pytestmark = [pytest.mark.app_path(".."), pytest.mark.board_profile("gps-alarm-clock")]
TARGET = Position(52.229677, 21.012233)
RADIUS = m(100)
SPEED = km_h(36)
FIX_INTERVAL = seconds(1)
REACTION_TIME = 2 * FIX_INTERVAL


def configure_alarm(
    board: Esp32Sim, *, target: Position = TARGET, radius: Distance = RADIUS
) -> None:
    """Translate this application's configuration to its NVS schema, before power-on."""
    if radius.m <= 0 or radius.m != int(radius.m):
        raise ValueError("the alarm radius must be a positive whole number of metres")
    board.nvs(
        {
            "alarm": {
                "lat_e6": i32(round(target.lat * 1_000_000)),
                "lon_e6": i32(round(target.lon * 1_000_000)),
                "radius_m": u32(int(radius.m)),
            }
        }
    )


@pytest.fixture
def buzzer(lines: Lines) -> OutputLine:
    return lines.output("alarm.buzzer")


def assert_never_buzzed(buzzer: OutputLine) -> None:
    """Check the entire observation window, including pulses between the test's waits."""
    assert buzzer.is_off()
    assert not any(
        event.by == "firmware" and event.level == buzzer.active for event in buzzer.events()
    )


def test_far_away_the_buzzer_stays_off(board: Esp32Sim, gnss: Gnss, buzzer: OutputLine) -> None:
    configure_alarm(board)
    gnss.fix(at=TARGET.moved(EAST, m(1000)))
    board.power_on()
    board.wait(seconds(5))
    assert_never_buzzed(buzzer)


def test_moving_into_the_radius_turns_it_on(
    board: Esp32Sim, gnss: Gnss, buzzer: OutputLine
) -> None:
    configure_alarm(board)
    start_distance = m(150)
    gnss.fix(at=TARGET.moved(EAST, start_distance))
    board.power_on()
    board.wait(seconds(3))
    assert buzzer.is_off()

    time_to_boundary = (start_distance - RADIUS) / SPEED
    gnss.move(heading=WEST, speed=SPEED)
    board.wait(time_to_boundary - FIX_INTERVAL)
    assert_never_buzzed(buzzer)
    board.wait(FIX_INTERVAL)  # Reach the boundary before starting the reaction deadline.
    board.wait_until(buzzer.is_on, within=REACTION_TIME)


def test_moving_out_turns_it_off(board: Esp32Sim, gnss: Gnss, buzzer: OutputLine) -> None:
    configure_alarm(board)
    gnss.fix(at=TARGET)
    board.power_on()
    board.wait_until(buzzer.is_on, within=REACTION_TIME)

    time_to_boundary = RADIUS / SPEED
    gnss.move(heading=EAST, speed=SPEED)
    board.wait(time_to_boundary - FIX_INTERVAL)
    assert buzzer.is_on()
    board.wait(FIX_INTERVAL)  # The same two-fix deadline, now measured from the exit.
    board.wait_until(buzzer.is_off, within=REACTION_TIME)


def test_without_a_fix_it_stays_off(board: Esp32Sim, gnss: Gnss, buzzer: OutputLine) -> None:
    configure_alarm(board)
    gnss.lose_fix()
    board.power_on()
    board.wait(seconds(5))
    assert_never_buzzed(buzzer)


def test_losing_the_fix_turns_it_off(board: Esp32Sim, gnss: Gnss, buzzer: OutputLine) -> None:
    configure_alarm(board)
    gnss.fix(at=TARGET)
    board.power_on()
    board.wait_until(buzzer.is_on, within=REACTION_TIME)
    gnss.lose_fix()
    board.wait_until(buzzer.is_off, within=REACTION_TIME)
    board.wait(seconds(3))
    assert buzzer.is_off()


def test_silence_expires_the_last_fix(board: Esp32Sim, gnss: Gnss, buzzer: OutputLine) -> None:
    configure_alarm(board)
    gnss.fix(at=TARGET)
    board.power_on()
    board.wait_until(buzzer.is_on, within=REACTION_TIME)
    gnss.go_silent()
    board.wait_until(buzzer.is_off, within=seconds(3))


@pytest.mark.parametrize("radius,expected_on", [(m(50), False), (m(200), True)])
def test_the_radius_comes_from_nvs(
    board: Esp32Sim, gnss: Gnss, buzzer: OutputLine, radius: Distance, expected_on: bool
) -> None:
    # Another destination, in the southern and western hemispheres, catches hardcoded targets.
    target = Position(-34.6037, -58.3816)
    configure_alarm(board, target=target, radius=radius)
    gnss.fix(at=target.moved(EAST, m(100)))
    board.power_on()
    expected = buzzer.is_on if expected_on else buzzer.is_off
    board.wait_until(expected, within=REACTION_TIME)
    board.wait(seconds(3))
    assert expected()


def test_missing_configuration_keeps_it_off(
    board: Esp32Sim, gnss: Gnss, buzzer: OutputLine, console: Console
) -> None:
    gnss.fix(at=TARGET)
    board.power_on()
    console.expect_exact("gps_alarm_clock: invalid or missing NVS alarm configuration")
    board.wait(seconds(3))
    assert_never_buzzed(buzzer)
