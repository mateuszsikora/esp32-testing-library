# Recording format, version 1

A recording is what crossed one interface's wire, and when: the bytes a GNSS receiver sent the
ESP32, or a modem's answers and the requests they answered. esp32tl replays a recording into the
firmware ([replay](features/replay-user.md)), and a tap writes one of what crossed a route during a
run ([taps](features/run-report-user.md#tapping-a-route));
`scripts/generate-gnss-recordings.py` writes the committed ones in `recordings/`
([validation firmware](features/validation-firmware-user.md#the-gnss-recordings)).
This document is the format; `esp32tl/recording.py` reads and writes it. An HCI tap, of the
firmware's Bluetooth, is a btsnoop file with this format's header beside it
([HCI taps](#hci-taps)).

## The file

A recording is a `.jsonl` file: UTF-8 text, one JSON object per line, each line ended by `\n`
(not `\r\n`), no blank lines. The first line is the header. Every line after it is an event or a
clock switch, in the order they happened.

```json
{"esp32tl_recording": 1, "board": "esp32-devkitc", "interface": "uart.gnss", "source": "model:nmea", "clock": "emulated-fifo-write", "end": "hold", "app_sha256": null, "tools": {"esp32tl": "0.1.0", "esp32sim": "357b0e542dfe97d4c62ec4a15c01c4969164448c"}, "created": "2026-10-29T18:00:00Z"}
{"t": 1000000000, "dir": "in", "data": "JEdQR0dBLDEyMDAwMC4wMCw1MjEzLjc4MDYsTiwwMjEwMC43MzQwLEUsMSwwOCwwLjksMTEwLjAsTSwzNC4wLE0sLCo2Ng0K"}
{"t": 2000000000, "dir": "in", "data": "JEdQUk1DLDEyMDAwMS4wMCxBLDUyMTMuNzc5NCxOLDAyMTAwLjczNTYsRSwwLjAsLDI5MTAyNiwsLEEqNzANCg=="}
```

## Header

| Field | Type | Required | Meaning |
|---|---|---|---|
| `esp32tl_recording` | integer | yes | The format's version, `1`. Any other value is refused |
| `board` | non-empty string | yes | The board profile the recording was made on, as in `esp32-devkitc`. Not checked against the profiles esp32tl ships |
| `interface` | non-empty string | yes | The interface of that profile: `console`, `uart.gnss`, `uart.modem`. A replay refuses a recording of another interface |
| `source` | non-empty string | yes | What was at the far end: `model:<name>` for a model, `real` for a real device, `generated` for a script's output; a tap writes `replay` for a replay, `model:gnss` for the GNSS receiver model, `model:modem` for the modem model, `model:<class>` for a model the test gave a route (`Uart.model`), by its class's name, and `none` for a route with nothing at its far end. Any non-empty string is read |
| `clock` | `emulated-fifo-write` or `wall` | yes | The clock the times are on, up to the first clock switch ([Clocks](#clocks)) |
| `end` | `hold` or `loop` | no, `hold` | Unprompted events: `hold`, stop after the last; `loop`, repeat with the final timestamp as period. Request matching continues in either case ([matching](features/matching-user.md)) |
| `app_sha256` | 64 lowercase hex digits, or `null` | no, `null` | The SHA-256 of the `app.bin` that ran when the recording was made, the app image only, so that a bootloader or partition-table change does not set it apart; `null` when no app made it. The `esp32sim` backend does not compare it |
| `tools` | object of strings | no, `{}` | The versions of the tools that made it, by name: `esp32tl`, `esp32sim` (the full revision the runner's `hello` names) |
| `created` | string | yes | When it was made, in UTC: `YYYY-MM-DDTHH:MM:SSZ`, fractions of a second allowed (`.5Z`), nothing else |

The header may carry fields of its own beyond these, as the generated recordings carry
`generator` (script, seed, baud). They are read, kept as they are, and written back after the
format's own fields; a replay ignores them. A field of this table is never an extra.

Console taps use `interface: console`, `source: none` on esp32sim or `real` on serial, and
`out` events for guest output and `in` events for test writes. Input timestamps are the board
time of `write`/`sendline`, before the runner paces individual bytes. This is still version 1:
the interface and directions already accept these values. On serial,
`app_sha256` describes the selected local artifact; under skip-autoflash it cannot prove which
app is actually in flash. `app_recording(path)` compares the reference hash against that artifact
before opening the port ([serial backend](features/serial-backend-user.md#builds-and-flashing)).

## Events

```json
{"t": 1000000000, "dir": "in", "data": "JEdQ"}
```

- `t`: when the bytes crossed the wire, in nanoseconds, an integer from 0, on the clock in effect
  ([Clocks](#clocks)). Serial console taps stamp a whole read batch after a poll; polling (10 ms
  nominal interval), USB, driver and scheduling delays are included, not removed.
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
refused. Clock switches describe sessions that cross between emulated and wall-clock sources;
nothing esp32tl writes makes one yet. The modem and its simulated PPP network stay on the
emulated clock throughout a run, including after `CONNECT`. Nor does the `real` source on the
emulator make one: a run with a real device at the far end of a route is on the `wall`
clock from power-on ([link protocol](protocol.md#124-the-backend-and-recordings)).

## Clocks

The vocabulary is that of the runner's `hello` ([link protocol](protocol.md#51-hello)):

- `emulated-fifo-write`: emulated nanoseconds since power-on, a byte the firmware transmits stamped
  when it writes the UART FIFO, not when it would leave the wire. This is what the `esp32sim`
  backend runs on.
- `wall`: wall-clock nanoseconds, from a real board or a real device. The format fixes no origin
  for them: only their differences mean anything. A run on the emulator with a real device at the
  far end of a route is on this clock too: its times are emulated nanoseconds since
  power-on, paced to wall time, less the wall time the run was held in a turn or skipped when it
  fell behind ([link protocol](protocol.md#12-wall-time---realtime-and-the-real-source-m5)).

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
| `write_btsnoop(path, recording)`, `dumps_btsnoop(recording)` | An HCI tap: the btsnoop file at `path` and its sidecar; `dumps_btsnoop` gives the file's bytes and the sidecar's text |
| `read_btsnoop(path)`, `loads_btsnoop(data, header, name="<btsnoop>")` | The `Recording` in a btsnoop file and its sidecar, or in their contents |
| `sidecar(path)` | The sidecar of the btsnoop file at `path`: `path` with `.json` added |

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

## HCI taps

`bt.tap(path)` ([taps](features/run-report-user.md#tapping-the-hci)) writes what crossed the HCI
between the firmware's Bluetooth host and its controller as two files: `path`, a btsnoop file, and
`path` with `.json` added, the sidecar, which holds the header.

The sidecar is one line, a header of this format with its `\n`, as the first line of a recording:
`interface` is `bluetooth`, `source` is `model:bluetooth` (esp32tl's controller), and a tap adds a
field of its own, `devices`, a list of `{"name": ..., "address": ...}`, the devices the test put on
the air.

```json
{"esp32tl_recording": 1, "board": "esp32-devkitc", "interface": "bluetooth", "source": "model:bluetooth", "clock": "emulated-fifo-write", "end": "hold", "app_sha256": "9a1c…", "tools": {"esp32tl": "0.1.0", "esp32sim": "357b0e542dfe97d4c62ec4a15c01c4969164448c"}, "created": "2026-11-10T08:00:00Z", "devices": [{"name": "ESP_SPP_ACCEPTOR", "address": "00:00:00:00:5B:01"}]}
```

The btsnoop file is version 1 with datalink 1002, HCI UART (H4), as Bumble's `BtSnooper` and
Android write it, all integers big-endian:

- the file's header: `btsnoop` and a zero byte, the version (4 bytes, `1`), the datalink (4
  bytes, `1002`);
- then a record per packet: its length on the HCI and its length in the file (4 bytes each, the
  same: no packet is cut), the flags (4 bytes), the cumulative drops (4 bytes, `0`), the timestamp
  (8 bytes), and the packet, its H4 packet-type byte first.
- flags: bit 0 set for a packet the host received (`in`, an event or ACL data from the
  controller), clear for one it sent (`out`, a command or ACL data); bit 1 set for a command or an
  event.
- timestamp: microseconds from midnight, 1 January of the year 0, as btsnoop counts them; a time
  of 0 ns is written as `0x00DCDDB30F2F8000`, 1970-01-01T00:00:00Z on that scale. So a reader
  such as Wireshark shows the time since power-on as a time after 1970-01-01 00:00:00 UTC, on the
  header's clock, not the wall clock's. `created` in the sidecar is when the file was written.
  The writer cuts each time down to the microsecond, and the reader gives it back in
  nanoseconds: `read_btsnoop` of what `write_btsnoop` wrote is the recording, but for the
  nanoseconds below the microsecond.

`read_btsnoop` gives a `Recording` with the sidecar's header and an event per record, at its
time on the header's clock, `in` or `out` by bit 0; bit 1 is not read. A btsnoop file has no clock
switch, and `dumps_btsnoop` refuses a recording with one:
`a clock switch at {t}: an HCI tap is on its header's clock alone`. The JSON Lines format is
unchanged by HCI taps, and its version is still 1.

`read_btsnoop` raises `RecordingError` with the file: `{file}: {what}`, where `{file}` is the
btsnoop file, or the sidecar for an error in the sidecar.

| `{what}` | Cause |
|---|---|
| the `OSError`'s text, as `[Errno 2] No such file or directory: ...` | Either file cannot be read, or the sidecar is not UTF-8 |
| the [errors](#errors) of a header line | The sidecar's line is not a header of this format |
| `not a btsnoop file: it does not start with btsnoop and a zero byte` | Another file |
| `btsnoop version {n}: esp32tl reads version 1` | |
| `datalink {n}: esp32tl reads 1002, HCI UART (H4)` | Another datalink, such as 1001, packets without their type byte |
| `record {n}: the file ends in its header`, `record {n}: the file ends in its packet` | A cut file |
| `record {n}: {included} of the packet's {original} bytes: a cut packet` | A record whose two lengths differ |
| `record {n}: timestamp {ts} is before 62168256000000000, the time of 0 ns` | A time before power-on |
| `record {n}: data: an event carries at least one byte` | An empty record |
| `record {n}: t {t} is before {since}, ...` | Times going back, as in a recording |

## What the format leaves out

- The baud of a UART recording: the bytes go in at the line rate of the board profile's route,
  and a firmware configured otherwise sees the frame errors it would see on a wire.

## Tests

`tests/test_recording.py`: every committed recording reads, with the generator's header fields,
and writes back byte for byte; the optional header fields' defaults; a clock switch putting the
events after it on the new clock, and round-tripping; `Event.since` within a clock and its
`ClockError` across; `write` then `read` through a file; each error of the table above (but the
non-UTF-8 file) with its line; the file named in the errors; and a `Recording` and a `Header`
built in code checked as a read one.

HCI taps, in the same file: a recording written as btsnoop and read back whole, the sidecar the
header's line, the file's header and first record byte for byte; the file as Bumble's `BtSnooper`
writes it, but for the timestamps, and Bumble's `SnoopPacketReader` reading each packet with its
direction at its time after 1970-01-01; a time cut to the microsecond; a clock switch refused;
each error of the table above but the unreadable sidecar's encoding, a missing sidecar, one that is
no header, and a missing file. `tests/test_bt_spp_initiator.py` reads the tap of a run of the
firmware.

## See also

- [Replay](features/replay-user.md): what replays a recording, and what it refuses
- [Plan section 4.3](implementation-plan.md#43-recording-format), where the format was sketched
- [Link protocol](protocol.md): the clocks' vocabulary, from the runner's `hello`
- [Decisions on the harness](decisions/harness.md)
