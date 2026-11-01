"""The units a test states its quantities in (`esp32tl/units.py`): the constructors, arithmetic
across kinds, great-circle movement and distance in both hemispheres and across the antimeridian
and a pole, and `about`."""

import math

import pytest

from esp32tl.units import (
    EAST,
    NORTH,
    NORTH_EAST,
    NORTH_WEST,
    SOUTH,
    SOUTH_WEST,
    WEST,
    Distance,
    Duration,
    Heading,
    Position,
    Speed,
    about,
    feet,
    hours,
    km,
    km_h,
    knots,
    m,
    m_s,
    miles,
    minutes,
    mph,
    ms,
    seconds,
)

WARSAW = Position(52.229677, 21.012233)
BUENOS_AIRES = Position(-34.6037, -58.3816)
# One degree of a great circle on the sphere of the Earth's mean radius, 6371008.8 m.
DEGREE = m(6_371_008.8 * math.pi / 180)


def test_durations_are_whole_nanoseconds_in_any_unit() -> None:
    assert seconds(1) == Duration(1_000_000_000)
    assert ms(1.5) == Duration(1_500_000)
    assert minutes(1) + seconds(40) == seconds(100)
    assert hours(1) == minutes(60) == seconds(3600)
    assert seconds(0.1) + seconds(0.2) == ms(300)
    assert seconds(1) - seconds(3) == -seconds(2) and abs(seconds(-2)) == seconds(2)
    assert seconds(2) * 1.5 == 3 * seconds(1) == seconds(3)
    assert seconds(3) / 2 == ms(1500) and seconds(3) / seconds(2) == 1.5
    assert seconds(1) < ms(1001) and sorted([seconds(2), ms(1)]) == [ms(1), seconds(2)]
    assert seconds(1.5).seconds == 1.5
    assert [str(d) for d in (seconds(30), ms(12_500), Duration(1), -ms(100))] == [
        "30 s",
        "12.5 s",
        "0.000000001 s",
        "-0.1 s",
    ]
    assert repr(ms(1500)) == "seconds(1.5)"


def test_speeds_and_distances_convert_between_units() -> None:
    assert km_h(36) == m_s(10)
    assert m_s(10).km_h == pytest.approx(36)
    assert mph(1).m_s == pytest.approx(0.44704)
    assert knots(1).m_s == pytest.approx(1852 / 3600)
    assert m_s(10).knots == pytest.approx(19.438445, abs=1e-6)
    assert m_s(1).mph == pytest.approx(2.236936, abs=1e-6)
    assert km(1) == m(1000) and km(1).km == 1
    assert miles(1) == m(1609.344) and m(1609.344).miles == 1
    assert feet(1) == m(0.3048) and m(0.3048).feet == pytest.approx(1)
    assert km(1) + m(500) - m(250) == m(1250)
    assert m(10) * 2 == 2 * m(10) == m(20) and m(10) / 4 == m(2.5) and km(1) / m(250) == 4
    assert m(1) < m(2) and abs(-m(3)) == m(3)
    assert str(m(1012.5)) == "1012.5 m" and repr(km_h(36)) == "m_s(10)"


def test_a_speed_over_a_time_is_a_distance_and_back() -> None:
    assert km_h(36) * (minutes(1) + seconds(40)) == km(1)
    assert seconds(100) * m_s(10) == km(1)
    assert km(1) / seconds(100) == m_s(10)
    assert m_s(1) + m_s(2) - m_s(0.5) == m_s(2.5) and m_s(2) * 3 == m_s(6)
    assert km_h(36) / 2 == km_h(18) and km_h(36) / km_h(18) == 2
    # How long a kilometre takes at 36 km/h.
    assert km(1) / km_h(36) == seconds(100)
    with pytest.raises(ZeroDivisionError, match="the duration is 0"):
        km(1) / seconds(0)
    with pytest.raises(ZeroDivisionError, match="the speed is 0"):
        km(1) / m_s(0)


def test_quantities_of_different_kinds_do_not_mix() -> None:
    with pytest.raises(TypeError):
        seconds(1) + m(1)  # type: ignore[operator]
    with pytest.raises(TypeError):
        assert m(1) < seconds(1)  # type: ignore[operator]
    assert seconds(1) != Duration(1) and m(1) != m_s(1)  # type: ignore[comparison-overlap]
    for wrong in (True, "1", None):
        with pytest.raises(TypeError, match="is not a number"):
            seconds(wrong)  # type: ignore[arg-type]
    for bad in (math.inf, math.nan):
        with pytest.raises(ValueError, match="is not a finite number"):
            km(bad)
    with pytest.raises(TypeError, match="is not a whole number of nanoseconds"):
        Duration(1.5)  # type: ignore[arg-type]


def test_headings_are_degrees_clockwise_from_north() -> None:
    assert [h.degrees for h in (NORTH, NORTH_EAST, EAST, SOUTH, WEST, NORTH_WEST)] == [
        0,
        45,
        90,
        180,
        270,
        315,
    ]
    assert Heading(-90) == WEST and Heading(360) == NORTH and Heading(585) == SOUTH_WEST
    # -1e-20 % 360 is 360.0 in floating point: still north, and below 360.
    assert Heading(-1e-20) == NORTH and str(Heading(-1e-20)) == "north"
    assert str(EAST) == "east" and repr(NORTH_WEST) == "NORTH_WEST"
    assert str(Heading(30)) == "30°" and repr(Heading(30.5)) == "Heading(30.5)"


def test_positions_are_checked_and_shown_with_their_hemispheres() -> None:
    assert str(WARSAW) == "52.229677° N, 21.012233° E"
    assert str(BUENOS_AIRES) == "34.603700° S, 58.381600° W"
    for lat, lon in ((90.5, 0), (0, -180.5)):
        with pytest.raises(ValueError, match="is not from"):
            Position(lat, lon)


@pytest.mark.parametrize("start", [WARSAW, BUENOS_AIRES], ids=["north-east", "south-west"])
def test_a_move_follows_the_great_circle_and_measures_its_distance(start: Position) -> None:
    # Along a meridian, a degree of latitude per degree of the great circle.
    north = start.moved(NORTH, DEGREE)
    assert north.lat == pytest.approx(start.lat + 1, abs=1e-9)
    assert north.lon == pytest.approx(start.lon, abs=1e-9)
    assert start.moved(SOUTH, DEGREE).lat == pytest.approx(start.lat - 1, abs=1e-9)
    for heading in (NORTH, EAST, SOUTH_WEST, Heading(123.4)):
        there = start.moved(heading, km(25))
        assert start.distance_to(there) == about(km(25), within=m(0.001))
        assert start.bearing_to(there).degrees == pytest.approx(heading.degrees, abs=1e-6)
    # East on a parallel away from the equator is no great circle: the path bends towards the
    # equator, and the bearing back is not west.
    east = start.moved(EAST, km(100))
    assert abs(east.lat) < abs(start.lat)
    assert east.bearing_to(start).degrees != pytest.approx(270, abs=0.1)
    assert start.distance_to(start) == m(0)
    assert start.moved(EAST, -km(1)) == about(start.moved(WEST, km(1)), within=m(0.001))


def test_a_move_crosses_the_antimeridian_and_a_pole() -> None:
    east = Position(0, 179.995).moved(EAST, km(1))
    assert east.lat == pytest.approx(0, abs=1e-9)
    assert east.lon == pytest.approx(-179.995 + 1000 / DEGREE.m - 0.01, abs=1e-9)
    assert Position(0, 179.995).distance_to(east) == about(km(1), within=m(0.001))
    # A kilometre north of a point 0.001° short of the pole, on the far side of it.
    over = Position(89.999, 10).moved(NORTH, km(1))
    assert over.lon == pytest.approx(-170, abs=1e-6)
    assert over.lat == pytest.approx(90 - (1000 / DEGREE.m - 0.001), abs=1e-9)


def test_about_compares_within_a_tolerance_of_the_same_kind() -> None:
    assert seconds(1.05) == about(seconds(1), within=ms(50))
    assert seconds(1.06) != about(seconds(1), within=ms(50))
    assert m(1012) == about(km(1), within=m(15))
    assert km_h(50) == about(mph(31.07), within=km_h(0.01))
    assert WARSAW.moved(EAST, m(10)) == about(WARSAW, within=m(10.001))
    assert WARSAW.moved(EAST, m(10)) != about(WARSAW, within=m(9.999))
    # Another kind is never about it.
    assert m(1) != about(seconds(1), within=seconds(1))
    assert about(km(1), within=m(1)) != WARSAW
    assert repr(about(km(1), within=m(15))) == "about(m(1000), within=m(15))"
    with pytest.raises(TypeError, match=r"the tolerance around m\(1000\) is a Distance, not"):
        about(km(1), within=seconds(1))
    with pytest.raises(TypeError, match="the tolerance around Position"):
        about(WARSAW, within=seconds(1))
    with pytest.raises(TypeError, match="is not a Duration, a Speed, a Distance or a Position"):
        about(1, within=m(1))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="less than 0"):
        about(km(1), within=m(-1))


def test_a_speed_and_a_distance_are_kept_in_si_units() -> None:
    assert Speed(10) == m_s(10) and Distance(5) == m(5)
