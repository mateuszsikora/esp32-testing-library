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
FIX_MAX_AGE = seconds(2.5)
# Allow for observing the first on state, completing its NMEA epoch and UART polling.
EXPIRY_TOLERANCE = seconds(0.2)


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
    buzzer.assert_off()

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

    on_since = board.now
    time_to_boundary = RADIUS / SPEED
    gnss.move(heading=EAST, speed=SPEED)
    board.wait(time_to_boundary - FIX_INTERVAL)
    buzzer.assert_stayed_on(since=on_since)
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
    off_since = board.now
    board.wait(seconds(3))
    buzzer.assert_stayed_off(since=off_since)


def test_silence_expires_the_last_fix(board: Esp32Sim, gnss: Gnss, buzzer: OutputLine) -> None:
    configure_alarm(board)
    gnss.fix(at=TARGET)
    board.power_on()
    board.wait_until(buzzer.is_on, within=REACTION_TIME)
    on_since = board.now
    gnss.go_silent()

    board.wait(FIX_MAX_AGE - EXPIRY_TOLERANCE)
    buzzer.assert_stayed_on(since=on_since)
    board.wait_until(buzzer.is_off, within=2 * EXPIRY_TOLERANCE)


def test_a_smaller_configured_radius_keeps_it_off(
    board: Esp32Sim, gnss: Gnss, buzzer: OutputLine
) -> None:
    # Southern/western coordinates also catch a hardcoded destination.
    target = Position(-34.6037, -58.3816)
    configure_alarm(board, target=target, radius=m(50))
    gnss.fix(at=target.moved(EAST, m(100)))
    board.power_on()
    board.wait(seconds(5))
    assert_never_buzzed(buzzer)


def test_a_larger_configured_radius_turns_it_on(
    board: Esp32Sim, gnss: Gnss, buzzer: OutputLine
) -> None:
    target = Position(-34.6037, -58.3816)
    configure_alarm(board, target=target, radius=m(200))
    gnss.fix(at=target.moved(EAST, m(100)))
    board.power_on()
    board.wait_until(buzzer.is_on, within=REACTION_TIME)
    on_since = board.now
    board.wait(seconds(3))
    buzzer.assert_stayed_on(since=on_since)


def test_missing_configuration_keeps_it_off(
    board: Esp32Sim, gnss: Gnss, buzzer: OutputLine, console: Console
) -> None:
    gnss.fix(at=TARGET)
    board.power_on()
    console.expect_exact("gps_alarm_clock: invalid or missing NVS alarm configuration")
    board.wait(seconds(3))
    assert_never_buzzed(buzzer)


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
    """Allow an unconfigured pin only before the first firmware-driven level."""
    initialized = False
    for event in buzzer.events():
        if event.by != "firmware":
            continue
        if event.level is None and not initialized:
            continue
        initialized = True
        if event.level != 1 - buzzer.active:
            observed = "undriven/unknown" if event.level is None else f"level {event.level}"
            raise AssertionError(
                f"{buzzer.name} expected off after initialization "
                f"(GPIO {buzzer.pin}, level {1 - buzzer.active}); "
                f"first violation at {event.t.seconds:g} s: {observed}"
            )
    buzzer.assert_off()
