"""nmea_reader, unmodified, fed by a GNSS receiver the test describes: a fix, a move over a
kilometre, the fix lost and coming back, in both hemispheres, on the test's clock. What it prints
is read as console lines and compared, in units, with where the receiver is."""

import pytest

from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.interfaces.console import Console, Line
from esp32tl.interfaces.gnss import Gnss
from esp32tl.units import (
    EAST,
    NORTH,
    Position,
    about,
    km,
    km_h,
    m,
    minutes,
    ms,
    seconds,
)

pytestmark = pytest.mark.app_path("../firmware/nmea_reader")

WARSAW = Position(52.229677, 21.012233)
BUENOS_AIRES = Position(-34.6037, -58.3816)

# What nmea_reader prints for a fix: "fix acquired lat=… lon=…" for the first, "fix lat=… lon=…"
# for every later one (validation-firmware-user.md).
FIRST_FIX = r"^fix acquired lat=(?P<lat>\S+) lon=(?P<lon>\S+)$"
FIX = r"^fix lat=(?P<lat>\S+) lon=(?P<lon>\S+)$"


def reported(line: Line) -> Position:
    """The position a fix line reports."""
    return Position(float(line["lat"]), float(line["lon"]))


def test_reports_where_the_receiver_is(board: Esp32Sim, gnss: Gnss, console: Console) -> None:
    gnss.fix(at=WARSAW)
    board.power_on()

    first = board.wait_until(console.line(FIRST_FIX), within=seconds(5))

    assert first.text == "fix acquired lat=52.229677 lon=21.012233"
    # The first epoch goes in at 1 s; its RMC takes 70 ms at 9600 baud.
    assert first.at == about(seconds(1), within=ms(200))
    assert board.link.warnings == []


def test_reports_the_southern_and_western_hemispheres(
    board: Esp32Sim, gnss: Gnss, console: Console
) -> None:
    gnss.fix(at=BUENOS_AIRES)
    board.power_on()

    first = board.wait_until(console.line(FIRST_FIX), within=seconds(5))

    assert first.text == "fix acquired lat=-34.603700 lon=-58.381600"


def test_follows_the_receiver_as_it_moves(board: Esp32Sim, gnss: Gnss, console: Console) -> None:
    gnss.fix(at=WARSAW)
    board.power_on()
    board.wait(seconds(10))

    gnss.move(heading=NORTH, speed=km_h(36))  # 10 m/s
    board.wait(minutes(1) + seconds(40))  # 1 km
    assert gnss.position == about(WARSAW.moved(NORTH, km(1)), within=m(0.001))

    latest = board.wait_until(console.line(FIX), within=seconds(2))
    assert reported(latest).distance_to(WARSAW) == about(km(1), within=m(15))
    # The epoch at 110 s, to the microdegree the firmware prints: 11 cm of latitude.
    assert reported(latest) == about(WARSAW.moved(NORTH, km(1)), within=m(0.2))


def test_says_nothing_while_the_receiver_has_no_fix(
    board: Esp32Sim, gnss: Gnss, console: Console
) -> None:
    gnss.fix(at=WARSAW)
    board.power_on()
    board.wait_until(console.line(FIRST_FIX), within=seconds(5))

    gnss.lose_fix()
    lost_at = board.now
    any_fix = console.line("^fix")
    board.wait(seconds(30))

    assert any_fix() is None
    assert console.lines(since=lost_at) == []
    # The receiver went on talking: 30 epochs without a fix.
    assert gnss.receiver().sent >= 31


def test_reports_again_when_the_fix_comes_back(
    board: Esp32Sim, gnss: Gnss, console: Console
) -> None:
    gnss.fix(at=WARSAW)
    board.power_on()
    board.wait(seconds(5))
    gnss.lose_fix()
    board.wait(seconds(20))

    elsewhere = WARSAW.moved(EAST, km(2))
    gnss.fix(at=elsewhere)
    back_at = board.now

    line = board.wait_until(console.line(FIX), within=seconds(5))
    # 25 s is an epoch's instant, and the fix holds from it: that epoch has it, its RMC in after
    # 70 ms at 9600 baud.
    assert back_at == seconds(25)
    assert line.at - back_at == about(ms(70), within=ms(50))
    assert reported(line) == about(elsewhere, within=m(0.2))


def test_hears_nothing_while_the_receiver_is_silent(
    board: Esp32Sim, gnss: Gnss, console: Console
) -> None:
    gnss.fix(at=WARSAW)
    board.power_on()
    board.wait_until(console.line(FIRST_FIX), within=seconds(5))

    gnss.go_silent()
    silent = console.line(FIX)
    board.wait(seconds(10))
    assert silent() is None

    gnss.resume()
    assert board.wait_until(console.line(FIX), within=seconds(2)).text == (
        "fix lat=52.229677 lon=21.012233"
    )
