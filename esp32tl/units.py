"""Quantities a test states with their unit: the board's time, speed, distance, heading, and where
on Earth.

Each is a value type made by a constructor named after its unit, `seconds(20)`, `km_h(50)`,
`miles(1)`, so that no number in a test leaves its unit to the reader. A `Duration` is also an
instant on the board's clock, the time since power-on, as `board.now` gives it. Positions are on a
sphere of the Earth's mean radius: great-circle paths and haversine distances, which differ from
the WGS 84 ellipsoid's geodesics by less than 0.6 %.
"""

import math
from dataclasses import dataclass
from functools import total_ordering
from typing import Any, overload

NS_PER_S = 1_000_000_000

# The Earth's mean radius, IUGG's R1.
EARTH_RADIUS_M = 6_371_008.8

M_PER_MILE = 1609.344
M_PER_FOOT = 0.3048
M_PER_NAUTICAL_MILE = 1852


def number(value: object, what: str) -> float:
    """`value`, checked to be a finite number: a `TypeError` for what is not a number, `True`
    included, and a `ValueError` for infinity and NaN."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{what}: {value!r} is not a number")
    if not math.isfinite(value):
        raise ValueError(f"{what}: {value!r} is not a finite number")
    return value


def shown(value: float) -> str:
    """`value` with up to six decimals, without trailing zeros: `1012.5`, `36`."""
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


def _same(self: Any, other: object) -> bool:
    return type(other) is type(self)


@total_ordering
@dataclass(frozen=True)
class Duration:
    """A span of the board's time, in whole nanoseconds, or an instant on its clock: the time
    since power-on. Durations add and subtract, scale by a number, and divide into a number."""

    ns: int

    def __post_init__(self) -> None:
        if isinstance(self.ns, bool) or not isinstance(self.ns, int):
            raise TypeError(f"Duration: {self.ns!r} is not a whole number of nanoseconds")

    @property
    def seconds(self) -> float:
        return self.ns / NS_PER_S

    def __add__(self, other: "Duration") -> "Duration":
        return Duration(self.ns + other.ns) if _same(self, other) else NotImplemented

    def __sub__(self, other: "Duration") -> "Duration":
        return Duration(self.ns - other.ns) if _same(self, other) else NotImplemented

    def __mul__(self, factor: float) -> "Duration":
        if isinstance(factor, bool) or not isinstance(factor, int | float):
            return NotImplemented
        return Duration(round(self.ns * number(factor, "Duration *")))

    __rmul__ = __mul__

    @overload
    def __truediv__(self, other: "Duration") -> float: ...

    @overload
    def __truediv__(self, other: float) -> "Duration": ...

    def __truediv__(self, other: "float | Duration") -> Any:
        if _same(self, other):
            assert isinstance(other, Duration)
            return self.ns / other.ns
        if isinstance(other, bool) or not isinstance(other, int | float):
            return NotImplemented
        return Duration(round(self.ns / number(other, "Duration /")))

    def __neg__(self) -> "Duration":
        return Duration(-self.ns)

    def __abs__(self) -> "Duration":
        return Duration(abs(self.ns))

    def __lt__(self, other: "Duration") -> bool:
        return self.ns < other.ns if _same(self, other) else NotImplemented

    def __str__(self) -> str:
        """Seconds without trailing zeros, to the nanosecond: `12.5 s`, `30 s`, `-0.1 s`."""
        whole, part = divmod(abs(self.ns), NS_PER_S)
        sign = "-" if self.ns < 0 else ""
        return sign + f"{whole}.{part:09d}".rstrip("0").rstrip(".") + " s"

    def __repr__(self) -> str:
        return f"seconds({str(self)[:-2]})"


def _duration(value: float, ns_per_unit: int, unit: str) -> Duration:
    value = number(value, unit)
    if isinstance(value, int):
        return Duration(value * ns_per_unit)
    return Duration(round(value * ns_per_unit))


def ms(value: float) -> Duration:
    """`value` milliseconds."""
    return _duration(value, 1_000_000, "ms")


def seconds(value: float) -> Duration:
    """`value` seconds."""
    return _duration(value, NS_PER_S, "seconds")


def minutes(value: float) -> Duration:
    """`value` minutes."""
    return _duration(value, 60 * NS_PER_S, "minutes")


def hours(value: float) -> Duration:
    """`value` hours."""
    return _duration(value, 3600 * NS_PER_S, "hours")


@total_ordering
@dataclass(frozen=True)
class Speed:
    """A speed, in metres per second; `km_h`, `mph` and `knots` give it in those units. A speed
    over a `Duration` is a `Distance`."""

    m_s: float

    def __post_init__(self) -> None:
        number(self.m_s, "Speed")

    @property
    def km_h(self) -> float:
        return self.m_s * 3.6

    @property
    def mph(self) -> float:
        return self.m_s * 3600 / M_PER_MILE

    @property
    def knots(self) -> float:
        return self.m_s * 3600 / M_PER_NAUTICAL_MILE

    def __add__(self, other: "Speed") -> "Speed":
        return Speed(self.m_s + other.m_s) if _same(self, other) else NotImplemented

    def __sub__(self, other: "Speed") -> "Speed":
        return Speed(self.m_s - other.m_s) if _same(self, other) else NotImplemented

    @overload
    def __mul__(self, other: "Duration") -> "Distance": ...

    @overload
    def __mul__(self, other: float) -> "Speed": ...

    def __mul__(self, other: "float | Duration") -> Any:
        if isinstance(other, Duration):
            return Distance(self.m_s * other.ns / NS_PER_S)
        if isinstance(other, bool) or not isinstance(other, int | float):
            return NotImplemented
        return Speed(self.m_s * number(other, "Speed *"))

    __rmul__ = __mul__

    def __neg__(self) -> "Speed":
        return Speed(-self.m_s)

    def __abs__(self) -> "Speed":
        return Speed(abs(self.m_s))

    def __lt__(self, other: "Speed") -> bool:
        return self.m_s < other.m_s if _same(self, other) else NotImplemented

    def __str__(self) -> str:
        return f"{shown(self.m_s)} m/s"

    def __repr__(self) -> str:
        return f"m_s({shown(self.m_s)})"


def m_s(value: float) -> Speed:
    """`value` metres per second."""
    return Speed(number(value, "m_s"))


def km_h(value: float) -> Speed:
    """`value` kilometres per hour."""
    return Speed(number(value, "km_h") / 3.6)


def mph(value: float) -> Speed:
    """`value` miles per hour."""
    return Speed(number(value, "mph") * M_PER_MILE / 3600)


def knots(value: float) -> Speed:
    """`value` knots: nautical miles, of 1852 m, per hour."""
    return Speed(number(value, "knots") * M_PER_NAUTICAL_MILE / 3600)


@total_ordering
@dataclass(frozen=True)
class Distance:
    """A distance, in metres; `km`, `miles` and `feet` give it in those units. A distance over a
    `Duration` is a `Speed`."""

    m: float

    def __post_init__(self) -> None:
        number(self.m, "Distance")

    @property
    def km(self) -> float:
        return self.m / 1000

    @property
    def miles(self) -> float:
        return self.m / M_PER_MILE

    @property
    def feet(self) -> float:
        return self.m / M_PER_FOOT

    def __add__(self, other: "Distance") -> "Distance":
        return Distance(self.m + other.m) if _same(self, other) else NotImplemented

    def __sub__(self, other: "Distance") -> "Distance":
        return Distance(self.m - other.m) if _same(self, other) else NotImplemented

    def __mul__(self, factor: float) -> "Distance":
        if isinstance(factor, bool) or not isinstance(factor, int | float):
            return NotImplemented
        return Distance(self.m * number(factor, "Distance *"))

    __rmul__ = __mul__

    @overload
    def __truediv__(self, other: "Duration") -> Speed: ...

    @overload
    def __truediv__(self, other: "Distance") -> float: ...

    @overload
    def __truediv__(self, other: float) -> "Distance": ...

    def __truediv__(self, other: "float | Distance | Duration") -> Any:
        if isinstance(other, Duration):
            if other.ns == 0:
                raise ZeroDivisionError("Distance / Duration: the duration is 0")
            return Speed(self.m * NS_PER_S / other.ns)
        if _same(self, other):
            assert isinstance(other, Distance)
            return self.m / other.m
        if isinstance(other, bool) or not isinstance(other, int | float):
            return NotImplemented
        return Distance(self.m / number(other, "Distance /"))

    def __neg__(self) -> "Distance":
        return Distance(-self.m)

    def __abs__(self) -> "Distance":
        return Distance(abs(self.m))

    def __lt__(self, other: "Distance") -> bool:
        return self.m < other.m if _same(self, other) else NotImplemented

    def __str__(self) -> str:
        return f"{shown(self.m)} m"

    def __repr__(self) -> str:
        return f"m({shown(self.m)})"


def m(value: float) -> Distance:
    """`value` metres."""
    return Distance(number(value, "m"))


def km(value: float) -> Distance:
    """`value` kilometres."""
    return Distance(number(value, "km") * 1000)


def miles(value: float) -> Distance:
    """`value` statute miles, of 1609.344 m."""
    return Distance(number(value, "miles") * M_PER_MILE)


def feet(value: float) -> Distance:
    """`value` international feet, of 0.3048 m."""
    return Distance(number(value, "feet") * M_PER_FOOT)


@dataclass(frozen=True)
class Heading:
    """A direction of travel, in degrees clockwise from true north, from 0 up to 360:
    `Heading(-90)` is `Heading(270)`, `WEST`."""

    degrees: float

    def __post_init__(self) -> None:
        # Frozen: the normalised value goes in through object.__setattr__.
        object.__setattr__(self, "degrees", number(self.degrees, "Heading") % 360.0)

    def __str__(self) -> str:
        return COMPASS.get(self.degrees, f"{shown(self.degrees)}°")

    def __repr__(self) -> str:
        name = COMPASS.get(self.degrees)
        return name.upper().replace("-", "_") if name else f"Heading({shown(self.degrees)})"


COMPASS = {
    0.0: "north",
    45.0: "north-east",
    90.0: "east",
    135.0: "south-east",
    180.0: "south",
    225.0: "south-west",
    270.0: "west",
    315.0: "north-west",
}
NORTH, NORTH_EAST, EAST, SOUTH_EAST, SOUTH, SOUTH_WEST, WEST, NORTH_WEST = (
    Heading(d) for d in COMPASS
)


@dataclass(frozen=True)
class Position:
    """A point on the Earth's surface: latitude and longitude in decimal degrees, north and east
    positive, latitude from -90 to 90 and longitude from -180 to 180."""

    lat: float
    lon: float

    def __post_init__(self) -> None:
        if not -90 <= number(self.lat, "Position: lat") <= 90:
            raise ValueError(f"Position: lat {self.lat!r} is not from -90 to 90 degrees")
        if not -180 <= number(self.lon, "Position: lon") <= 180:
            raise ValueError(f"Position: lon {self.lon!r} is not from -180 to 180 degrees")

    def moved(self, heading: Heading, distance: Distance) -> "Position":
        """Where the great circle that leaves here on `heading` is after `distance`. Its
        longitude is from -180 up to 180."""
        if not isinstance(heading, Heading):
            raise TypeError(f"moved: heading is {heading!r}, not a Heading, as NORTH")
        if not isinstance(distance, Distance):
            raise TypeError(f"moved: distance is {distance!r}, not a Distance, as km(1)")
        d = distance.m / EARTH_RADIUS_M
        theta = math.radians(heading.degrees)
        lat1, lon1 = math.radians(self.lat), math.radians(self.lon)
        sin_lat2 = math.sin(lat1) * math.cos(d) + math.cos(lat1) * math.sin(d) * math.cos(theta)
        lat2 = math.asin(max(-1.0, min(1.0, sin_lat2)))
        lon2 = lon1 + math.atan2(
            math.sin(theta) * math.sin(d) * math.cos(lat1),
            math.cos(d) - math.sin(lat1) * sin_lat2,
        )
        lon = (math.degrees(lon2) + 180) % 360 - 180
        return Position(math.degrees(lat2), lon)

    def distance_to(self, other: "Position") -> Distance:
        """The great-circle distance to `other`, by the haversine."""
        if not isinstance(other, Position):
            raise TypeError(f"distance_to: {other!r} is not a Position")
        lat1, lat2 = math.radians(self.lat), math.radians(other.lat)
        dlat = lat2 - lat1
        dlon = math.radians(other.lon - self.lon)
        a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        return Distance(2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a))))

    def bearing_to(self, other: "Position") -> Heading:
        """The heading the great circle from here to `other` leaves on; `NORTH` to the same
        point."""
        if not isinstance(other, Position):
            raise TypeError(f"bearing_to: {other!r} is not a Position")
        lat1, lat2 = math.radians(self.lat), math.radians(other.lat)
        dlon = math.radians(other.lon - self.lon)
        y = math.sin(dlon) * math.cos(lat2)
        x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
        return Heading(math.degrees(math.atan2(y, x)))

    def __str__(self) -> str:
        lat = f"{abs(self.lat):.6f}° {'S' if self.lat < 0 else 'N'}"
        lon = f"{abs(self.lon):.6f}° {'W' if self.lon < 0 else 'E'}"
        return f"{lat}, {lon}"


# What each quantity is kept in, for `about`.
def _base(value: "Duration | Speed | Distance") -> float:
    if isinstance(value, Duration):
        return value.ns
    if isinstance(value, Speed):
        return value.m_s
    return value.m


class about:
    """`assert x == about(value, within=tolerance)`: a duration, speed or distance no further
    from `value` than `tolerance`, both of `value`'s kind; or a `Position` no further from it than
    a `Distance`. A comparison with anything else is false."""

    def __init__(
        self,
        value: "Duration | Speed | Distance | Position",
        *,
        within: "Duration | Speed | Distance",
    ) -> None:
        if not isinstance(value, Duration | Speed | Distance | Position):
            raise TypeError(
                f"about: {value!r} is not a Duration, a Speed, a Distance or a Position"
            )
        kind = Distance if isinstance(value, Position) else type(value)
        if type(within) is not kind:
            raise TypeError(
                f"about: the tolerance around {value!r} is a {kind.__name__}, not {within!r}"
            )
        if _base(within) < 0:
            raise ValueError(f"about: the tolerance {within!r} is less than 0")
        self.value = value
        self.within = within

    def __eq__(self, other: object) -> bool:
        if isinstance(self.value, Position):
            if not isinstance(other, Position):
                return False
            return other.distance_to(self.value).m <= _base(self.within)
        if type(other) is not type(self.value):
            return False
        assert isinstance(other, Duration | Speed | Distance)
        return abs(_base(other) - _base(self.value)) <= _base(self.within)

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return f"about({self.value!r}, within={self.within!r})"
