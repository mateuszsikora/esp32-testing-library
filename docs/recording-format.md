# Recording format, version 1

A recording is what crossed one interface's wire, and when: the bytes a GNSS receiver sent the
ESP32, or a modem's answers and the requests they answered. esp32tl replays a recording into the
firmware ([replay](features/replay-user.md)); `scripts/generate-gnss-recordings.py` writes the
committed ones in `recordings/` ([validation firmware](features/validation-firmware-user.md#the-gnss-recordings)).
This document is the format; `esp32tl/recording.py` reads and writes it.

## The file

A recording is a `.jsonl` file: UTF-8 text, one JSON object per line, each line ended by `\n`
(not `\r\n`), no blank lines. The first line is the header. Every line after it is an event or a clock switch,
in the order they happened.

```json
{"esp32tl_recording": 1, "board": "esp32-devkitc", "interface": "uart.gnss", "source": "model:nmea", "clock": "emulated-fifo-write", "end": "hold", "app_sha256": null, "tools": {"esp32tl": "0.1.0", "esp32sim": "8c652151dd5a521249a785ca147f20a9f8eda68f"}, "created": "2026-10-29T18:00:00Z"}
{"t": 1000000000, "dir": "in", "data": "JEdQR0dBLDEyMDAwMC4wMCw1MjEzLjc4MDYsTiwwMjEwMC43MzQwLEUsMSwwOCwwLjksMTEwLjAsTSwzNC4wLE0sLCo2Ng0K"}
{"t": 2000000000, "dir": "in", "data": "JEdQUk1DLDEyMDAwMS4wMCxBLDUyMTMuNzc5NCxOLDAyMTAwLjczNTYsRSwwLjAsLDI5MTAyNiwsLEEqNzANCg=="}
```

## Header

| Field | Type | Required | Meaning |
|---|---|---|---|
| `esp32tl_recording` | integer | yes | The format's version, `1`. Any other value is refused |
| `board` | non-empty string | yes | The board profile the recording was made on, as in `esp32-devkitc`. Not checked against the profiles esp32tl ships |
| `interface` | non-empty string | yes | The interface of that profile: `uart.gnss`, `uart.modem`. A replay refuses a recording of another interface |
| `source` | non-empty string | yes | What was at the far end: `model:<name>` for a model, `real` for a real device, `generated` for a script's output. Any non-empty string is read |
| `clock` | `emulated-fifo-write` or `wall` | yes | The clock the times are on, up to the first clock switch ([Clocks](#clocks)) |
| `end` | `hold` or `loop` | no, `hold` | After the last event: `hold`, nothing more; `loop`, the recording starts over |
| `app_sha256` | 64 lowercase hex digits, or `null` | no, `null` | The SHA-256 of the `app.bin` that ran when the recording was made, the app image only, so that a bootloader or partition-table change does not set it apart; `null` when no app made it. The `esp32sim` backend does not compare it |
| `tools` | object of strings | no, `{}` | The versions of the tools that made it, by name: `esp32tl`, `esp32sim` (the full revision the runner's `hello` names) |
| `created` | string | yes | When it was made, in UTC: `YYYY-MM-DDTHH:MM:SSZ`, fractions of a second allowed (`.5Z`), nothing else |

The header may carry fields of its own beyond these, as the generated recordings carry
`generator` (script, seed, baud). They are read, kept as they are, and written back after the
format's own fields; a replay ignores them. A field of this table is never an extra.

## Events

```json
{"t": 1000000000, "dir": "in", "data": "JEdQ"}
```

- `t`: when the bytes crossed the wire, in nanoseconds, an integer from 0, on the clock in effect
  ([Clocks](#clocks)).
- `dir`: `in`, into the ESP32 (what the GNSS receiver sent), or `out`, out of it (what the firmware
  transmitted).
- `data`: the bytes, base64 with padding, at least one byte. Where one event's bytes end and the
  next one's begin carries no meaning beyond their times.

An event has these three keys and no other.

## Clock switches

```json
{"t": 4100000000, "clock": "wall"}
```

From this line on the times are on `clock`, the clock of every event up to the next switch. `t` is
the time of the switch on the new clock. A switch to the clock the recording is on already is
refused. It is meant for a session that leaves the emulated clock part-way, a modem after
`CONNECT` (plan M6); nothing esp32tl writes makes one yet.

## Clocks

The vocabulary is that of the runner's `hello` ([link protocol](protocol.md#51-hello)):

- `emulated-fifo-write`: emulated nanoseconds since power-on, a byte the firmware transmits stamped
  when it writes the UART FIFO, not when it would leave the wire. This is what the `esp32sim`
  backend runs on.
- `wall`: wall-clock nanoseconds, from a real board or a real device. The format fixes no origin
  for them: only their differences mean anything.

Within one clock, times never go back: each event's `t` is at or after the line before it, the
first after a switch at or after the switch's `t`, and a switch back to a clock the recording has
left at or after the latest time it had on that clock. Two events at one time are in the order
written.

Times on two clocks do not compare: an emulated time and a wall time have nothing in common, and
timing of an emulator run read as wire timing, or the reverse, is the mistake the `clock` field is
there to prevent. In code, `Event.since(earlier)` gives the time between two events and raises
`ClockError` when they are on different clocks; a replay refuses a recording with a time on a
clock other than its backend's ([replay](features/replay-user.md#what-a-replay-refuses)).

## Reading and writing it in code

`esp32tl/recording.py`:

| Name | What |
|---|---|
| `read(path)`, `loads(text, name="<recording>")` | The `Recording` in a file or a string; `name` is the file in the errors |
| `write(path, recording)`, `dumps(recording)` | The file's text: the header's fields in the order of the table above, then its extras; `json.dumps`' default separators. `loads(dumps(r)) == r`, and the committed recordings read and write back byte for byte |
| `Recording(header, lines)` | `lines` is the events and clock switches after the header, in order; `Recording.events` the events alone. Checked as a file is: the order, and each event's `clock` against the switches |
| `Header(board, interface, source, clock, created, end="hold", app_sha256=None, tools={}, extra={})` | The header; `extra` holds the fields the format does not define |
| `Event(t, dir, data, clock)` | `data` is `bytes`; `clock` is the clock in effect, which the reader fills in. `Event.since(earlier)` |
| `ClockSwitch(t, clock)` | A clock switch |
| `RecordingError` | A recording that breaks the format. Attributes: `what`, the error alone; `file`, the file, `None` for a recording built in code; `line`, the line's number, `None` for an error about the whole file |
| `ClockError` | Times compared across clocks |
| `VERSION`, `CLOCKS`, `ENDS`, `DIRECTIONS` | `1`, `("emulated-fifo-write", "wall")`, `("hold", "loop")`, `("in", "out")`; `EMULATED` and `WALL` are the two clocks |

## Errors

`read` and `loads` raise `RecordingError` with the file and the line: `{file}:{line}: {what}`, or
`{file}: {what}` for the file as a whole. `{file}` is the path as given to `read`.

| `{what}` | Cause |
|---|---|
| `[Errno 2] No such file or directory: ...` (the `OSError`'s text), after `{file}: ` | The file cannot be read, or is not UTF-8 |
| `empty: a recording starts with its header`, after `{file}: ` | An empty file |
| `the last line does not end with a newline` | The text does not end with `\n` |
| `the line ends with \r\n, not \n` | A line ended by `\r\n`, as a checkout with `core.autocrlf` leaves it |
| `not JSON: {json's message}` | A line that is not JSON, a blank one included |
| `not a JSON object` | A line that is JSON but not an object |
| `no esp32tl_recording: the first line is not a recording's header` | The first line has no `esp32tl_recording` |
| `esp32tl_recording: {value}: esp32tl reads version 1 of the format` | Another version, or a value that is not the integer `1` |
| `missing {fields}` | Required header fields missing, comma-separated |
| `board: {value} is not a non-empty string`, the same for `interface` and `source` | |
| `clock: {value} is not one of emulated-fifo-write, wall` | In the header or a clock switch |
| `end: {value} is not one of hold, loop` | |
| `app_sha256: {value} is not null or 64 lowercase hex digits` | |
| `tools: {value} is not an object of strings` | |
| `created: {value} is not a UTC time, as in 2026-10-29T18:00:00Z` | Not of the form above, or not a real date and time |
| `t: {value} is not a time in nanoseconds, an integer from 0` | Negative, a float, `true` |
| `dir: {value} is not one of in, out` | |
| `data: {value} is not a base64 string` | Not a string |
| `data: {first 40 characters} is not base64` | Not valid base64 |
| `data: an event carries at least one byte` | Empty `data` |
| `missing {keys}; unknown key {keys}: an event is {t, dir, data}, a clock switch {t, clock}` | A line with other keys; either half is left out when empty |
| `t {t} is before {since}, a time before it on the {clock} clock` | Time going back on one clock: an event or a switch earlier than the latest time on its clock, also one on a clock the recording left and came back to |
| `switches to the {clock} clock, which the recording is on` | A switch to the current clock |

A `Recording` or `Header` built in code is checked the same way, without the file:
`line {n}: {what}`, where `n` is the line the entry would be on, or `{field}: {what}`. Two more
messages come only from code: `line {n}: an event on the {clock} clock, in a recording on {clock}`
for an `Event` whose `clock` is not the one in effect, and `extra: {fields} is a field of the
format`.

## What the format leaves out

- The baud of a UART recording: the bytes go in at the line rate of the board profile's route,
  and a firmware configured otherwise sees the frame errors it would see on a wire.
- HCI taps: plan §4.3 records them as btsnoop files with the header in a sidecar `.json`.
  `esp32tl/recording.py` reads neither.

## Tests

`tests/test_recording.py`: every committed recording reads, with the generator's header fields,
and writes back byte for byte; the optional header fields' defaults; a clock switch putting the
events after it on the new clock, and round-tripping; `Event.since` within a clock and its
`ClockError` across; `write` then `read` through a file; each error of the table above (but the
non-UTF-8 file) with its line; the file named in the errors; and a `Recording` and a `Header`
built in code checked as a read one.

## See also

- [Replay](features/replay-user.md): what replays a recording, and what it refuses
- [Plan section 4.3](implementation-plan.md#43-recording-format), where the format was sketched
- [Link protocol](protocol.md): the clocks' vocabulary, from the runner's `hello`
- [Decisions on the harness](decisions/harness.md)
