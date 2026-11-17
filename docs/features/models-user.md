# Models and units

A test describes the world around the board in that world's own terms: a GNSS receiver that has a
fix at a place, moves north at 36 km/h, loses its fix and finds it again. esp32tl turns that into
what the receiver would send the firmware. Every quantity the test states carries its unit, so
`seconds(10)`, `km_h(36)` and `m(15)` say what they are, and `about(km(1), within=m(15))` compares
with a tolerance.

## Units

Everything below is in `esp32tl.units`.

| Kind | Made by | Reads as | Kept in |
|---|---|---|---|
| `Duration` | `ms(x)`, `seconds(x)`, `minutes(x)`, `hours(x)` | `.seconds`, `.ns` | whole nanoseconds |
| `Speed` | `m_s(x)`, `km_h(x)`, `mph(x)`, `knots(x)` | `.m_s`, `.km_h`, `.mph`, `.knots` | metres per second |
| `Rate` | `kbit_s(x)`, `mbit_s(x)`, of 1000 and 1000000 bits per second | `.bit_s`, `.kbit_s`, `.mbit_s` | bits per second; above 0, else `ValueError`. It compares and has no arithmetic: [`network.set_path`](network-user.md#give-the-network-a-round-trip-and-a-rate) is what it is for |
| `Distance` | `m(x)`, `km(x)`, `miles(x)`, `feet(x)` | `.m`, `.km`, `.miles`, `.feet` | metres |
| `Heading` | `NORTH`, `NORTH_EAST`, `EAST`, `SOUTH_EAST`, `SOUTH`, `SOUTH_WEST`, `WEST`, `NORTH_WEST`, `Heading(degrees)` | `.degrees`, from 0 up to 360 | degrees clockwise from true north |
| `Position` | `Position(lat, lon)`, decimal degrees, north and east positive | `.lat`, `.lon` | degrees |

`x` is an `int` or a `float`, finite; anything else raises `TypeError` or `ValueError`, as in
`seconds: '1' is not a number`. A mile is the statute mile, 1609.344 m; a knot is 1852 m an hour;
a foot is 0.3048 m. `Heading(-90)` is `WEST`.

- A `Duration` is also an instant on the board's clock: `board.now` is one, the time since
  power-on, and so is a console line's `at`. `line.at - back_at` is a `Duration`.
- Durations, speeds and distances of one kind add, subtract, compare and sort; a number scales
  them, as in `seconds(2) * 1.5` and `km_h(36) / 2`; one divided by another of its kind is a
  number. Kinds do not mix, so `seconds(1) + m(1)` raises `TypeError`, except where physics says
  what comes out: `km_h(36) * seconds(100) == km(1)`, `km(1) / seconds(100) == m_s(10)`, and
  `km(1) / km_h(36) == seconds(100)`. Headings and positions only compare equal.
- They print in their base unit: `str(ms(12_500))` is `12.5 s`, `str(km(1))` is `1000 m`, and
  `repr` writes the constructor, `seconds(12.5)`, `m(1000)`, which is what pytest shows when an
  assertion fails.
- `position.moved(heading, distance)` is where the great circle that leaves `position` on
  `heading` is after `distance`; `position.distance_to(other)` is the great-circle distance, by the
  haversine; `position.bearing_to(other)` the heading the great circle to `other` leaves on.
  Positions are on a sphere of the Earth's mean radius, 6371008.8 m: distances differ from the
  WGS 84 ellipsoid's by less than 0.6 %.

### Comparing with a tolerance

`about(value, within=tolerance)` equals what is no further from `value` than `tolerance`:

```python
assert line.at - back_at == about(ms(70), within=ms(50))
assert reported.distance_to(WARSAW) == about(km(1), within=m(15))
assert reported == about(WARSAW.moved(NORTH, km(1)), within=m(0.2))
```

`value` is a `Duration`, a `Speed`, a `Distance`, or a `Position`, whose tolerance is a
`Distance`; the tolerance is of `value`'s kind otherwise. A tolerance of another kind raises
`TypeError: about: the tolerance around m(1000) is a Distance, not seconds(1)`, one below 0
`ValueError`. Anything of another kind is never about it: `m(1) == about(seconds(1), within=seconds(1))`
is false.

## A model of the test's, in Durations

A test that needs a part esp32tl has no model of writes one: at the far end of a UART, with
`board.uart(role).model(...)` ([a UART's own model](pytest-plugin-user.md#a-uarts-own-model)), or
behind an SPP channel, as an `SppPeer`'s `source`
([the model that answers](bluetooth-peer-user.md#the-model-that-answers)). Subclassing
`esp32tl.sources.model.Model` lets it state every time as a `Duration`, as the rest of a test does:

```python
from esp32tl.sources.model import Model
from esp32tl.units import ms


class Echo(Model):
    def receive(self, at, data):
        return [(at + ms(50), data)]

    def poll(self, now):
        return ()

    def quiet_until(self):
        return None

    def latency(self):
        return ms(50)
```

- `receive(at, data)`, `poll(now)`, `quiet_until()` and `latency()` take and return `Duration`s:
  `at` and `now` are instants on the board's clock, each answer is an `(instant, bytes)` pair,
  `Timed` in `esp32tl.sources.model`, and `quiet_until` and `latency` return a `Duration` or `None`.
  All four are abstract: a subclass that lacks one, or misspells it, raises `TypeError` when it is
  made.
- On a UART, `receive`'s `at` is the instant the firmware wrote the first byte of `data` into the
  UART's TX FIFO; each byte after it came within one character time of the one before, at the
  route's baud. esp32sim does not pace a UART's transmission at the baud: what the firmware writes
  in one go comes at about one instant, not one character time per byte, and `data` may be cut
  anywhere, so a model finds a request by its protocol's terminator or by the silence after it
  ([`uart_tx`](../protocol.md#52-uart_tx)). Behind an SPP channel, `at` is the instant `data`
  arrived.
- A `supply(on)` method puts it on the board's supply, as for any source.
- The route and the peer call it in nanoseconds, converting at the edge: it puts the same bytes on
  the wire at the same instants as the same model written with `int` nanoseconds, which both
  accept as well, and is checked the same way, with the messages in `Duration`s, as
  `uart.aux.quiet_until returned 1.5, not a Duration`.

### A model that only answers

Most parts answer what they hear and send nothing unprompted, and their `poll`, `quiet_until` and
`latency` say only that. `esp32tl.sources.model.ReactiveModel` says it for them: a subclass
defines `receive` and sets `answer_after`, the shortest time it takes to answer, as a class
attribute or in `__init__`:

```python
from esp32tl.sources.model import ReactiveModel
from esp32tl.units import ms


class Echo(ReactiveModel):
    answer_after = ms(50)

    def receive(self, at, data):
        return [(at + self.answer_after, data)]
```

`latency()` returns `answer_after`, `poll` returns nothing and `quiet_until()` `None`.
`answer_after = None` is a model that answers nothing. On a UART, one without `answer_after` fails
the run at its first turn, with `uart.aux.latency raised TypeError: Echo has no answer_after: set
it to the shortest time it takes to answer, as ms(1)`. The OBD-II reader's `Elm327` is one
([OBD-II reader](examples-user.md#obd-ii-reader-over-bluetooth-classic)).

### A model of a part that frames by silence

A Modbus RTU slave, and many binary sensors, take the bytes up to a silence on the line as one
frame: 3.5 character times for RTU. `esp32tl.sources.model.IdleFramedModel` cuts what the model
hears into such frames: a subclass sets `gap`, the silence, and defines `on_frame(at, frame)`,
which returns the answers, each at `at` or later:

```python
from esp32tl.sources.model import IdleFramedModel
from esp32tl.units import ms


class Slave(IdleFramedModel):
    gap = ms(3.65)  # 3.5 characters of 10 bits (8N1) at 9600 baud

    def __init__(self):
        self.frames = []

    def on_frame(self, at, frame):
        self.frames.append((at, frame))
        return [(at + ms(5), answer(frame))]
```

- `on_frame` is called at the turn that comes once `gap` has passed since the frame's last piece
  came: `at` is that piece's `at` plus `gap`, or less than a CPU cycle later. A piece that comes
  within `gap` of the one before goes into the same frame, however the link cut what the firmware
  wrote; so does one that comes exactly `gap` after it, which the link delivers before the turn
  at that instant. One that comes later starts the next frame.
- `at` is when the silence showed the frame had ended, not when its data came. While `on_frame`
  runs, the model also has the frame's data instants: `self.frame_first_at`, the `at` of the
  frame's first piece, and `self.frame_last_at`, the `at` of its last piece. For a frame of one
  piece they are equal. They are what `receive` got as `at`, so they mean what it does there: on
  a UART, the instant the firmware wrote the piece's first byte into the TX FIFO; behind an
  `SppPeer` channel, the instant the piece arrived, which says nothing of when the firmware wrote
  it. On a UART, a test of when the firmware starts a request, as "the first poll starts 2 s
  after power-on", reads `frame_first_at`; `at` would add `gap` and the time the firmware took to
  write the frame. Neither is the instant of any byte after a piece's first: the link stamps a
  piece once, at its first byte, and `frame_last_at` is not when the frame's last byte was
  written. Both keep the
  frame's values until the next frame ends; before the first frame the model has neither, and
  reading one is an `AttributeError`. A subclass that defines only `on_frame(at, frame)` works as
  before and need not read them.
- The gap counts from the `at` of the last piece, the instant the firmware wrote its first byte:
  what it writes in one go comes at about one instant (above), so the gap is the firmware's own
  pause between writes. A frame the firmware writes in pieces less than a character time apart,
  but over more than `gap` in all, ends `gap` after its first byte: the link reports such bytes as
  one piece, at the first one's instant, so the model sees the pieces, not the line. `gap` is a
  `Duration` the test computes from the route's baud: the model is not told the baud. 3.5
  characters are `ms(3.65)` at 9600 baud with 10 bits a character, as 8N1, and `ms(4.02)` with
  the 11 bits (parity, or two stop bits) the Modbus specification asks for.
- `latency()` is `gap`, so that the board stops when a frame may have ended, and `quiet_until()`
  the end of the frame heard so far; the model sends nothing else unprompted. On a UART, a model
  without `gap` fails the run at its first turn, with `uart.aux.latency raised TypeError: Slave has
  no gap: set it to the silence that ends a frame, as ms(3.65)`.
- A frame not ended when the board's supply goes off is not dropped: it ends, and `on_frame` gets
  it, `gap` after its last piece, as any other. A model of a part that loses what it heard with its
  power calls `drop()` from its `supply(on)` when `on` is false: the frame heard so far is
  forgotten, and `on_frame` never gets it.

## The GNSS receiver

`gnss` becomes a receiver the test describes at the first of these calls that succeeds, which must
come before `board.power_on()`; one that is refused leaves `gnss` as it was. Every call after it but
`position`, before or after power-on, changes the receiver from the board's current instant,
`board.now`, until the test says something else, as the world does: time passes only while the test
waits.

```python
from esp32tl.units import NORTH, Position, km_h, minutes, seconds

WARSAW = Position(52.229677, 21.012233)


def test_follows_the_receiver_as_it_moves(board):
    board.gnss.fix(at=WARSAW)
    board.power_on()
    board.wait(seconds(10))

    board.gnss.move(heading=NORTH, speed=km_h(36))  # 10 m/s
    board.wait(minutes(1) + seconds(40))  # 1 km
    ...
```

| Call | What the receiver does from now on |
|---|---|
| `gnss.fix(at=position, *, hdop=0.9, satellites=8)` | Has a fix at `position`, standing still, of that HDOP with that many satellites in use |
| `gnss.move(*, heading, speed)` | Moves from where it is now, on the great circle that leaves on `heading`, at `speed`: a `Heading` and a `Speed` of 0 or more. It needs a position, from a `fix` before; it moves with or without a fix |
| `gnss.stand_still()` | Stands where it is |
| `gnss.degrade(*, hdop=None, satellites=None)` | Keeps its fix with another HDOP, another number of satellites, or both: worse, or better. It needs a fix |
| `gnss.lose_fix()` | Has no fix, until the next `fix`; it goes on moving as it did |
| `gnss.go_silent()` | Sends nothing, until `resume` |
| `gnss.resume()` | Sends again, from the epoch at this instant on, if one falls here, or else from the next |
| `gnss.position` | Where it is now, fix or not, a `Position`; `None` until its first fix. Reading it changes nothing and makes no receiver: before any other call it raises `SourceError` |

`hdop` is a number from 0.01 to 99.99, which the GGA writes to two decimals; `satellites` a whole
number from 1 to 99. Until its first `fix`, as after a `lose_fix`, the receiver talks but has no
fix, as one does after a cold start. A `fix` puts the receiver at `at` and stops it: to get the fix
back where a moving receiver is, and keep it moving, say `gnss.fix(at=gnss.position)` and
`gnss.move(...)` again.

### What it sends

One epoch a second, at every whole second of the world's time, the first one at least a second
after power-on: an RMC, then a GGA, with the same UTC time, NMEA 0183 with the talker `GP`, each
sentence `$…*hh` and CR LF, at the route's baud. With the default world's time, which starts on a
whole second, the epochs are at 1 s, 2 s, 3 s of the board's time. A fix at Warsaw at 1 s, on
1 January 2026:

```
$GPRMC,120001.00,A,5213.78062,N,02100.73398,E,0.00,,010126,,,A*4D
$GPGGA,120001.00,5213.78062,N,02100.73398,E,1,08,0.90,0.0,M,0.0,M,,*64
```

and without a fix:

```
$GPRMC,120001.00,V,,,,,,,010126,,,N*7B
$GPGGA,120001.00,,,,,0,00,99.99,,,,,,*64
```

- Positions are in degrees and minutes to five decimals of a minute, under 2 cm.
- The RMC's speed over ground is in knots, two decimals; its course is the heading along the
  great circle where the receiver is now, to 0.1°, and empty while it stands. Heading east from
  52° N, the course is 91.2° after 100 km, as the great circle turns towards the equator.
- The GGA has fix quality 1 and the HDOP to two decimals with a fix; quality 0, `00` satellites
  and an HDOP of `99.99` without one. Its altitude and geoid separation are `0.0`: the model has
  no altitude.
- Times and dates come from the world's time: `board.power_on(utc=datetime(...))`, a `datetime`
  with a timezone, `2026-01-01T12:00:00Z` unless given, plus the board's time. A power-on at
  12:00:00.25 has its first epoch at 1.75 s of the board's time, at 12:00:02.
- A change at an epoch's instant is in that epoch: a test that waits 25 s and then calls
  `gnss.fix(...)` has the fix in the epoch at 25 s.
- While silent, the receiver skips its epochs; it does not catch up when it resumes.
- The receiver is on the board's supply. While the board is off (`board.power_off()`) it sends
  nothing, as if silent, and from `board.power_on()` it sends again, from the epoch at that instant
  on, with the fix, the motion and the silence the test gave it: there is no time to a first fix.
  `go_silent` and `resume` and the supply are apart, so a receiver the test silenced stays silent
  when the power comes back. `board.reset()` does not touch it
  ([resets and power cycles](pytest-plugin-user.md#resets-and-power-cycles)).
- It hears what the firmware sends it and answers nothing: a firmware that configures its
  receiver gets no acknowledgement.

## When the test gets it wrong

| Message | Exception | When |
|---|---|---|
| `uart.gnss: the board is on already: give an interface its source before board.power_on()` | `SourceError` | The first model call comes after `board.power_on()`; `the board has been on` once `board.power_off()` has switched it off again |
| `uart.gnss replays a recording: it has no receiver to describe; one source per test` | `SourceError` | A model call after `gnss.replay` |
| `uart.gnss has its source already: one per test` | `SourceError` | `gnss.replay` after a model call |
| `uart.gnss has no receiver: give it one with gnss.fix(at=...)` | `SourceError` | `gnss.position` before any model call |
| `gnss.move: the receiver has no position to move from: give it a fix first, with gnss.fix(at=...)` | `ModelError` | `move` before any `fix` |
| `gnss.degrade: the receiver has no fix to degrade: give it one first, with gnss.fix(at=...)` | `ModelError` | `degrade` without a fix |
| `gnss.move: speed is -1 m/s, less than 0` | `ModelError` | A negative speed |
| `gnss.fix: hdop is 0, not from 0.01 to 99.99` | `ModelError` | An HDOP out of range, also from `degrade` |
| `gnss.degrade: satellites is 0, not from 1 to 99: a fix has some` | `ModelError` | A number of satellites out of range, also from `fix` |
| `gnss.move: heading is 90, not a Heading, as NORTH`, `gnss.move: speed is 10, not a Speed, as km_h(50)`, `gnss.fix: at is (52, 21), not a Position` | `TypeError` | A plain number or tuple where a unit belongs |
| `gnss.degrade: give it hdop=, satellites=, or both` | `TypeError` | `degrade()` with neither |
| `esp32sim: the test set up the model of uart.gnss and never called board.power_on(): it ran no firmware` | error at teardown | A test that described a receiver and never powered on |

`ModelError` is a `ValueError`, in `esp32tl.sources.model.gnss`; `SourceError` is in
`esp32tl.interfaces`. A call that is refused changes nothing. A backend that cannot give a model
skips the test at the first model call, as for a replay
([board profiles](board-profiles-user.md#when-a-backend-cannot-give-it)); `esp32sim` gives it.

## See also

- [Models and units: how they work](models-technical.md)
- [pytest plugin](pytest-plugin-user.md): `board.wait`, `console.line`, the `gnss` fixture
- [Replay](replay-user.md): a recorded receiver instead of a described one
- [Validation firmware](validation-firmware-user.md): `nmea_reader`, which the model is tested on
