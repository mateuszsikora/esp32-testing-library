# Replay: how it works

The replay is `Replay` in `esp32tl/sources/replay.py`, plan §4.4's stream case. It reads nothing
itself: it takes a `Recording` from `esp32tl/recording.py` ([recording format](../recording-format.md)).
The `Uart` interface of `esp32tl/interfaces/uart.py` puts it on the link, and the `gnss` fixture
hands that interface to the test ([pytest plugin](pytest-plugin-technical.md)). What the user
sees is in [replay-user.md](replay-user.md).

## Data flow

1. **Collection.** A `source("uart.gnss", "replay")` marker becomes a `Request` that
   `pytest_generate_tests` checks against the capability declaration
   ([board profiles](board-profiles-technical.md#data-flow)).
2. **`board` fixture.** It passes the test's sources, `{interface: source}` from
   `board_requests`, and the test file's directory to `Esp32Sim`, which makes a `Uart` for each
   declared route and holds the link's first turn until each has its source
   ([esp32sim backend](esp32sim-backend-technical.md#data-flow)).
3. **`gnss.replay(path)`**, in the test body: `Uart.replay` refuses a UART whose `declared` is not
   `"replay"` (`SourceError`), reads `directory / path` with `recording.read`, builds
   `Replay(recording, self.name, self.clock, name=str(path))`, and gives it: under a lock, a UART
   that has a source already raises `SourceError`; otherwise `Uart.source` is set and
   `on_source(name)` tells the backend, which starts the link once no declared UART waits.
4. **`Replay.__init__`** refuses, in this order, a recording of another interface, one with a
   clock other than `clock` anywhere (in its header, an event or a clock switch), one with an
   `out` event, and one whose `end` is not `hold` (`ReplayError`). Then it keeps the events as `(t, data)` pairs, in
   order, in `Replay.events`, and `sent = 0`.
5. **The link's turns.** `BoardLink` calls the `Uart` from its thread, which hands each call to
   its source ([link protocol](../protocol.md#7-latency-is-bound-to-the-source)):
   - `poll(now)` returns every event from `sent` on whose `t` is at or before `now`, and moves
     `sent` past them. Events of one time go out together, in recorded order.
   - `quiet_until()` is the `t` of `events[sent]`, `None` once all are sent. After `poll(now)`
     it is later than `now`, so the runner's next block point is the next event's time: each
     event goes out in the turn at its own `t`, stamped `t`.
   - `latency()` is `None`: a stream answers no request (protocol §7), so the firmware's
     transmissions never bring a turn forward.
   - `receive(t, data)` returns nothing.

   `BoardLink` turns each output into a `uart_rx` on the wire's RX pin at its baud, the board
   profile's `[uart.gnss]` `rx` and `baud`; the runner paces the bytes (protocol §6, rule 5).

Events are sent in their turn rather than all at once in the first one, which the runner would
queue (protocol §8): `quiet_until` then comes from the recording, as plan §4.4 and protocol §7
have it. The cost is one turn per distinct event time
([decision](../decisions/harness.md#a-stream-replay-sends-each-event-in-the-turn-at-its-time)).

## Interfaces

| Name | What |
|---|---|
| `Replay(recording, interface, clock, name="")` | The stream replay of `recording` on `interface`, whose times are on `clock`; `name` is the recording in the errors, `the recording` when empty. Attributes `events` (the `(t, data)` pairs) and `sent` |
| `Replay.receive(t, data)`, `poll(now)`, `quiet_until()`, `latency()` | The source's side of the `Interface` methods of `esp32tl/link/boardlink.py`, without the wire |
| `ReplayError` | A recording that cannot be replayed there |
| `Output` | `tuple[int, bytes]`: from `t`, these bytes |

## Failure modes

`ReplayError`, from `Replay.__init__`, `{name}` the path the test gave:

| Message | Cause |
|---|---|
| `{name} is a recording of {header.interface}, not of {interface}` | Another interface |
| `{name} has times on the {clocks} clock, and {interface} runs on the {clock} clock: replaying it would compare times across clocks` | `{clocks}`: every clock of the header, the events and the clock switches, sorted, joined by ` and `; refused unless it is `clock` alone, so a recording that leaves the clock and comes back is refused too |
| `{name} has events out of the ESP32, the first at {t} ns: it holds requests, and replay answers none yet, only streams with events in` | An `out` event |
| `{name} ends with {end}: replay holds at the end only` | `end: loop` |

An event stamped before the turn it is polled in cannot come from a `Replay` the link drives,
since its `quiet_until` stops the runner at the next event; were it to, `BoardLink` would fail the
link with `uart.gnss sent bytes stamped {t} ns in the turn at {now} ns: ...`.

## Tests

- `tests/test_replay.py`, without an emulator: the events going out at their times, together when
  they share one, and `quiet_until` before, between and after them; a stream not listening;
  each `ReplayError`; a `Uart` with a `Replay` on a held `BoardLink`, from `replay("r.jsonl")`
  relative to its directory to the `uart_rx` and `wait` of each turn; the `SourceError`s of
  `Uart.replay`; a recording that cannot be read or replayed leaving the UART without a source;
  and `gnss-generated-1.jsonl` replayed through the turns the runner would take, one event a
  second from 1 s to 60 s.
- `tests/test_gnss_fixture.py`, on the real runner and `nmea_reader`:
  `test_a_replay_reaches_nmea_reader_on_the_gnss_route` expects the first two fixes of
  `gnss-generated-1.jsonl` and no warning;
  `test_a_source_given_otherwise_than_declared_fails_the_test` runs, through `pytester`, an
  expect before the replay (failing at once), a replay without the marker, a declared replay never
  given (passing, its run ended with `stop` at 0 s), a `model` source and a profile without
  `uart.gnss` (errors at setup).

Not covered: a recording whose events reach the UART before the firmware has configured it (the
generated ones start at 1 s), and two declared UARTs on one board, since no profile has two
`uart.*` routes that a test can replay into.

## See also

- [Replay](replay-user.md)
- [Recording format](../recording-format.md)
- [Plan section 4.4](../implementation-plan.md#44-matching-engine), the matching engine whose
  stream case this is
- [Link protocol, section 7](../protocol.md#7-latency-is-bound-to-the-source)
