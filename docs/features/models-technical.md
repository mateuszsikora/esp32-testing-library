# Models and units: how they work

The units are `esp32tl/units.py`; the GNSS receiver model is `esp32tl/sources/model/gnss.py`, the
first of `esp32tl/sources/model/`. The `gnss` fixture's side, `Gnss` in
`esp32tl/interfaces/gnss.py`, is in [pytest-plugin-technical.md](pytest-plugin-technical.md#data-flow);
the backend that gives the receiver its clock in
[esp32sim-backend-technical.md](esp32sim-backend-technical.md#data-flow). What the user sees is in
[models-user.md](models-user.md).

## Data flow

1. **The receiver is made** by `Gnss._tell` at the first model call, after the backend's
   `asked("uart.gnss", "model")` has let it through:
   `GnssReceiver(now, world)`, where `now` returns `HeldClock.now`, the block point of the turn
   the link was last held at (0 before power-on), and `world` returns `Esp32Sim._utc`, the world's
   time at power-on. The call is made on it, and only when the call returns does it become the
   `Uart`'s `source`, of kind `model`: a refused first call leaves the route without a source.
   Reading `Gnss.position` or `Gnss.receiver()` makes no receiver.
2. **The test tells it** something: each call checks its arguments, then, under the receiver's
   lock, replaces its `State` with one whose `since` is `now()`, whose `position` and `heading`
   are where it is and which way it goes at that instant (`State.at(now)`,
   `State.course(now)`), and which carries the change. A refused call raises before it replaces
   anything, and the route keeps the source it had. The test calls it while the link is held in a turn, or before power-on, so the link
   never polls the receiver in the middle of a change; the lock is there all the same.
3. **The link polls it** in every turn, after the hold has returned (`BoardLink._turn`): so a
   change the test made in the turn at `now` is in what `poll(now)` sends. `poll` computes the
   first epoch on its first call, once the run has started and `world()` is known: the first
   whole second of the world's time at least `FIRST_EPOCH`, 1 s, after power-on, from the
   microseconds of the world's time at power-on. It then takes every epoch due by `now`, in
   order, advancing `_next` by `PERIOD`, 1 s, and sends each one at its own time, `(t, epoch(t))`,
   when the receiver talks. `sent` counts the epochs sent.
4. **`quiet_until()`** returns the next epoch's time while the receiver talks, so the runner stops
   there and the epoch goes in at its time; `None` while it is silent. A silent receiver bounds no
   turn, and the run may go past several epoch times to the test's next turn without the link
   polling it; the test can resume it only in a held turn, before the link polls, and `resume`
   moves `_next` to the first epoch at or after `now`, so the epochs of the silence stay unsent
   and one at `now` goes. A talking receiver bounds every turn by its next epoch, so a runner that
   ran past it would make the link fail with `sent past its quiet_until`, not lose the epoch.
5. **`epoch(t)`** formats an RMC and a GGA from the state at `t`: `State.at(t)` moves the position
   by `speed * Duration(t - since)` along the great circle (`Position.moved`), and
   `State.course(t)` is the azimuth at that point of the great circle that left `position` on
   `heading`, `atan2(sin θ cos φ, cos d cos φ cos θ − sin φ sin d)`, with `d` the angle travelled,
   `φ` the starting latitude and `θ` the starting heading. The UTC time is `world() +
   timedelta(microseconds=t // 1000)`.
6. **`receive(t, data)`** returns nothing: the receiver hears what the firmware sends and drops
   it. **`latency()`** is `None`.

## Schema

The sentences, field by field, `sentence(body)` adding `$`, `*`, the XOR of the body's characters
in two upper-case hex digits, and CR LF; a sentence longer than `SENTENCE_MAX`, 82 characters, CR
LF included, raises `ModelError`. `TALKER` is `GP`.

RMC with a fix: `GPRMC`, `hhmmss.cc`, `A`, latitude, `N`/`S`, longitude, `E`/`W`, speed over
ground in knots (`.2f`), course over ground in degrees (`.1f`, rounded and then taken modulo 360,
so 359.96 is `0.0`; empty while the speed is 0), `ddmmyy`, two empty magnetic-variation fields,
mode `A`. Without a fix: `GPRMC`, the time, `V`, six empty fields, the date, two empty fields,
mode `N`.

GGA with a fix: `GPGGA`, the time, latitude, `N`/`S`, longitude, `E`/`W`, quality `1`, the
satellites (`02d`), the HDOP (`.2f`), altitude `0.0`, `M`, geoid separation `0.0`, `M`, two empty
DGPS fields. Without a fix: the time, four empty fields, quality `0`, `00`, `99.99`
(`NO_FIX_HDOP`), six empty fields.

`angle(degrees, digits, positive, negative)` writes a latitude (`digits` 2) or a longitude (3):
`round(abs(degrees) * 60 * 100_000)` units of 10⁻⁵ minute, split into whole degrees, minutes and
five decimals, so a value that rounds up to 60 minutes carries into the next degree, then the
hemisphere's letter, `negative` for a value below 0.

## Interfaces

| Name | What |
|---|---|
| `Duration(ns)` | `esp32tl/units.py`: frozen dataclass, `ns` an `int` (`TypeError` otherwise); `+`, `-` with a `Duration`; `*`, `/` by a number (rounded to the nanosecond); `/` by a `Duration` is a `float`; `-d`, `abs`, ordering; `seconds`; `str` `12.5 s`, `repr` `seconds(12.5)` |
| `Speed(m_s)`, `Distance(m)` | Frozen dataclasses of a finite number; `+`, `-`, `*` and `/` by a number, ordering, `-`, `abs`; `Speed * Duration` is a `Distance`, `Distance / Duration` a `Speed`, `Distance / Speed` a `Duration` (`ZeroDivisionError` for 0), `Speed / Speed` and `Distance / Distance` a `float`; `km_h`, `mph`, `knots`; `km`, `miles`, `feet`; `str` `10 m/s`, `1000 m`, `repr` `m_s(10)`, `m(1000)` |
| `Heading(degrees)` | Frozen dataclass, `degrees` taken modulo 360, a result of 360.0 (`Heading(-1e-20)`) made 0; `str` `east` or `30°`, `repr` `EAST` or `Heading(30)`; `COMPASS` names the eight constants |
| `Position(lat, lon)` | Frozen dataclass, `ValueError` outside −90…90 and −180…180; `moved(heading, distance)` (longitude from −180 up to 180), `distance_to(other)`, `bearing_to(other)`; `str` `52.229677° N, 21.012233° E` |
| `about(value, *, within)` | `__eq__` true for the same kind within the tolerance, a `Position` within a `Distance`; false for any other kind; unhashable; `repr` `about(m(1000), within=m(15))` |
| `ms`, `seconds`, `minutes`, `hours`, `m_s`, `km_h`, `mph`, `knots`, `m`, `km`, `miles`, `feet` | Constructors; an `int` duration is scaled exactly, a `float` one rounded to the nanosecond |
| `number(value, what)` | A finite `int` or `float`, not a `bool`: `TypeError` `{what}: {value!r} is not a number`, `ValueError` `{what}: {value!r} is not a finite number` |
| `shown(value)` | A number with up to six decimals, without trailing zeros, as the `str` and `repr` above write it |
| `NS_PER_S`, `EARTH_RADIUS_M`, `M_PER_MILE`, `M_PER_FOOT`, `M_PER_NAUTICAL_MILE` | `1_000_000_000`; 6371008.8, IUGG's mean radius R1; 1609.344; 0.3048; 1852 |
| `GnssReceiver(now, world)` | `esp32tl/sources/model/gnss.py`: the test's `fix`, `move`, `stand_still`, `degrade`, `lose_fix`, `go_silent`, `resume`, `position`; the link's `receive`, `poll`, `quiet_until`, `latency`; `epoch(t)`; `sent` |
| `State` | Frozen dataclass: `since`, `position`, `heading`, `speed`, `fixed`, `hdop`, `satellites`, `talking`; `at(t)`, `course(t)`, `travelled(t)`. The first is `State(0, None, NORTH, m_s(0), False, 99.99, 0, True)` |
| `FIRST_EPOCH`, `PERIOD` | `seconds(1)` each |
| `HDOP`, `SATELLITES`, `NO_FIX_HDOP`, `MIN_HDOP`, `SENTENCE_MAX`, `TALKER` | `0.9`, `8`: a `fix`'s defaults; `99.99`; `0.01`, the lowest HDOP the GGA's two decimals say; `82`; `"GP"` |
| `sentence(body)`, `angle(degrees, digits, positive, negative)` | The sentence and coordinate formatting above |
| `ModelError` | A `ValueError`: what the receiver cannot be told |

## Configuration

None: the receiver's rate, talker, sentences and altitude are constants of the module. The
world's time at power-on is `board.power_on(utc=...)`'s, `POWER_ON_UTC` in
`esp32tl/backends/esp32sim.py` by default.

## Failure modes

| Message | Exception | Cause |
|---|---|---|
| `gnss.move: the receiver has no position to move from: give it a fix first, with gnss.fix(at=...)` | `ModelError` | `move` while `State.position` is `None` |
| `gnss.degrade: the receiver has no fix to degrade: give it one first, with gnss.fix(at=...)` | `ModelError` | `degrade` while `State.fixed` is false |
| `gnss.move: speed is {speed}, less than 0` | `ModelError` | A `Speed` below 0 |
| `{what}: hdop is {value!r}, not from 0.01 to 99.99` | `ModelError` | `fix` or `degrade` |
| `{what}: satellites is {value}, not from 1 to 99: a fix has some` | `ModelError` | `fix` or `degrade` |
| `{what}: satellites is {value!r}, not a whole number` | `TypeError` | Not an `int`, or a `bool` |
| `{what}: hdop: {value!r} is not a number`, `… is not a finite number` | `TypeError`, `ValueError` | From `number` |
| `gnss.fix: at is {at!r}, not a Position`, `gnss.move: heading is {heading!r}, not a Heading, as NORTH`, `gnss.move: speed is {speed!r}, not a Speed, as km_h(50)` | `TypeError` | A value without its unit |
| `gnss.degrade: give it hdop=, satellites=, or both` | `TypeError` | `degrade()` |
| `{line!r} is longer than NMEA's 82 characters` | `ModelError` | `sentence`; the model's own RMC is 74 characters at 999.99 knots, with both coordinates' longest fields |
| `Position: lat {lat!r} is not from -90 to 90 degrees`, `Position: lon {lon!r} is not from -180 to 180 degrees` | `ValueError` | `Position(...)` |
| `moved: heading is {heading!r}, not a Heading, as NORTH`, `moved: distance is {distance!r}, not a Distance, as km(1)`, `distance_to: {other!r} is not a Position`, `bearing_to: {other!r} is not a Position` | `TypeError` | |
| `about: {value!r} is not a Duration, a Speed, a Distance or a Position`, `about: the tolerance around {value!r} is a {kind}, not {within!r}`, `about: the tolerance {within!r} is less than 0` | `TypeError`, `ValueError` | |
| `Duration: {ns!r} is not a whole number of nanoseconds` | `TypeError` | `Duration(1.5)` |

The `SourceError`s of the first model call are the backend's and `Gnss`'s
([pytest-plugin-technical.md](pytest-plugin-technical.md#failure-modes)).

## Tests

- `tests/test_units.py`: the constructors and their conversions, arithmetic within a kind and
  across kinds where it is defined, kinds that do not mix, headings, positions and their bounds,
  great-circle movement and distance along a meridian and off it in the north-east and
  south-west quadrants, across the antimeridian and over a pole, and `about`.
- `tests/test_gnss_model.py`: the receiver on a clock of the test's own. The sentences against
  NMEA worked out by hand, with and without a fix, in the southern and western hemispheres and
  across a new year; checksums checked independently; the epochs on whole seconds of the world's
  time; a move of 1 km north and its speed and course; the course along a great circle that
  turns; a course that rounds to 360; a lost fix that keeps moving and comes back; `degrade`;
  silence, and the epochs of a silence with no turn in it; a change at an instant, and at an
  epoch's instant; a move across the antimeridian and over the north pole, with its course;
  what is refused; the rounding of coordinates into the next degree; the 82-character limit;
  and `Gnss`, off the link: a refused first call that leaves the route without a source, and
  `position` that makes no receiver.
- `tests/test_nmea_reader_model.py`: `nmea_reader` on the real runner, fed by the model: the first
  fix and its time, the southern and western hemispheres, 1 km north at 36 km/h, no fix line while
  the fix is lost, the fix coming back at the epoch of its instant, and silence. Needs
  `make firmware`.
- `tests/test_pytest_plugin.py`: the model's place among the sources (see
  [pytest-plugin-technical.md](pytest-plugin-technical.md#tests)).

Not covered: a receiver moving across a pole or the antimeridian on the real firmware, and a
`power_on(utc=...)` off a whole second on the real runner; `tests/test_gnss_model.py` tests the
model's side of each (`test_the_receiver_moves_across_the_antimeridian_and_over_a_pole`,
`test_epochs_fall_on_whole_seconds_of_the_world_s_time`). A receiver that starts exactly at a
pole is not tested.

## See also

- [Models and units](models-user.md)
- [Decisions: models describe the world in its own terms, in units](../decisions/harness.md#models-describe-the-world-in-its-own-terms-in-units)
- [Replay: how it works](replay-technical.md)
