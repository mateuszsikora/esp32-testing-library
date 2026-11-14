# Taps and the run report

A tap records what crosses one of the board's routes during a run, both ways: what the firmware
transmitted, and what the far end sent it, whether that is a replay or nothing at all. It writes a
[recording](../recording-format.md), in the format a replay reads. The run report is what a failing
test shows besides its error: the run on the board's clock, the test's steps, the console's lines
and the bytes on every route side by side, in the order they happened.

## Tapping a route

```python
import pytest

pytestmark = pytest.mark.app_path("../firmware/nmea_reader")


def test_reports_a_fix(board, gnss, console, tmp_path):
    gnss.replay("../recordings/gnss-generated-1.jsonl")
    gnss.tap(tmp_path / "gnss.jsonl")
    board.power_on()
    console.expect_exact("fix acquired lat=52.229677 lon=21.012233")
```

- `gnss.tap(path)` taps `uart.gnss`; `board.uart(role).tap(path)` taps any other UART route of the
  profile. Call it before `board.power_on()`, before or after the route's source: it records from
  power-on.
- The path is relative to the test file's directory, as a replay's is, or absolute: pytest's
  `tmp_path` keeps the file out of the source tree. Its directory must exist. A file already there
  is replaced, but not one the run replays from or another route is tapped into: those are
  refused at the call, also when the name differs only in case (`R.jsonl` for `r.jsonl`).
- The file is written once the run has ended, at the test's teardown, also when the test failed.
  A test that taps a route and never calls `board.power_on()` ran no firmware: nothing is written,
  and the test gets an error at teardown
  (`esp32sim: the test set up the tap of uart.gnss and never called board.power_on(): it ran no firmware`).
- `gnss.tap` returns the `Tap`. During the test its `events` list holds what the tap has recorded
  so far, as `(t, dir, data)` tuples in the order they reached the route, and `path` is the file.
- One tap per route in a test.

## Console taps

`console.tap(path)` records firmware console output on both backends, from `board.power_on()`
through teardown. It has the same path, duplicate and file collision checks as UART taps. On
esp32sim it writes `interface: console`, `source: none` and `clock: emulated-fifo-write`, or
`clock: wall` in a run with a [real source](real-source-user.md). On
serial it writes `interface: console`, `source: real` and `clock: wall`, stamping batches
after the serial reader polls, with up to 10 ms polling delay plus USB, driver and host
scheduling delays. Every event is `out`; setup and flash-tool messages are excluded. Serial
writes the selected artifact's app hash, which does not prove the device's flash contents under
`--skip-autoflash y`. See [serial](serial-backend-user.md) for reference-recording checks and the
`serial console` report / `serial_console` JSON property.

## Tapping the HCI

```python
def test_the_firmware_opens_spp(board, bt, console, tmp_path):
    SppPeer(bt.air, name="ESP_SPP_ACCEPTOR", pin="1234", source=model)
    bt.tap(tmp_path / "hci.btsnoop")
    board.power_on()
    board.wait_until(console.line("ESP_SPP_OPEN_EVT"), within=seconds(5))
```

`bt.tap(path)` records the HCI between the firmware's Bluetooth host and its controller on
esp32sim ([the bt fixture](pytest-plugin-user.md#the-bt-fixture)): every H4 packet the host sent,
commands and ACL data, and every one it received, events and ACL data, from power-on. It writes two
files at teardown, as a UART tap does: `path`, a btsnoop file, which Wireshark and Bumble's
`bumble-show --format snoop` open, and `path` with `.json` added (`hci.btsnoop.json`), the recording's header.
The same rules hold as for `gnss.tap`: before `board.power_on()`, relative to the test file's
directory or absolute, its directory must exist, one per test, and neither file may be one the run
replays from or another tap writes. On serial the `bt` fixture skips the test first.

- In the btsnoop file each packet is `sent` (host to controller) or `received` (controller to
  host), and its time is the board's since power-on, to the microsecond: Wireshark shows it as a
  time after 1970-01-01 00:00:00 UTC. A packet reaches the controller at the instant the host
  wrote it to the VHCI, and an answer reaches the host 10 µs after the controller made it.
- The header has `interface: bluetooth`, `source: model:bluetooth`, the board's `clock`, and a
  field of its own, `devices`: the devices the test made on the air, as `{"name": ..., "address":
  ...}`, so that a reader can tell whom a packet names by its address.
- `bt.tap` returns the `Tap`, whose `events` hold the packets as `(t, dir, data)`, `out` from the
  host, `in` to it, each with its packet-type byte; times there are in nanoseconds.
- The report's `bluetooth:` line ends with `; tapped into {path}`, and the `bluetooth` property has
  `tap`.

## What a tap writes

A recording, [version 1](../recording-format.md):

```json
{"esp32tl_recording": 1, "board": "esp32-devkitc", "interface": "uart.gnss", "source": "replay", "clock": "emulated-fifo-write", "end": "hold", "app_sha256": "4f0c…", "tools": {"esp32tl": "0.1.0", "esp32sim": "3bdb9582a910974e4e025d274d6ec61e5c481be6"}, "created": "2026-11-01T08:00:54Z"}
{"t": 1000000000, "dir": "in", "data": "JEdQR0dB..."}
```

- `source` is what was at the far end: `replay`, `model:modem` for the modem, `model:gnss` for the
  GNSS receiver the test described ([models and units](models-user.md)), `model:<class>` for a
  model the test wrote, by its class's name, as `model:AtResponder`
  ([a UART's own model](pytest-plugin-user.md#a-uarts-own-model)), `real` for a
  [device on a host serial port](real-source-user.md), or `none` for a route the test gave nothing.
- `clock` is the backend's, `emulated-fifo-write`, or `wall` in a run with a real source: times are
  the board's nanoseconds since power-on.
- `app_sha256` is the SHA-256 of the app image the run flashed, the file `flasher_args.json`
  names under `app`; `tools` the esp32tl version and the esp32sim revision the runner reported;
  `created` when the file was written, in UTC.
- An `out` event is bytes the firmware wrote to the route's UART, at the time it wrote them. An
  `in` event is bytes the far end gave, at the time it stamped them in the current run. Unprompted
  replay events keep their recorded absolute times on the first pass; `loop` shifts later passes
  by the recording's period. Matched answers keep their recorded bytes and delays relative to
  the current request's completion time, not their original absolute times. Repeated requests
  or a different request order can repeat or reorder answers, and unknown requests can add
  fallback replies. A tap therefore records the current exchange, not necessarily a prefix of
  the original recording ([matching](matching-user.md#timing-and-diagnostics)). Bytes the harness
  refused to send, because they were stamped too early, are in the tap as well; the run has
  failed on them.
- Events come in the order of their times; two of one time in the order they reached the route.

## The run on the board's clock

A test that fails, in its body, at setup or at teardown, gets the run at the end of its
`esp32sim link` report section, after the link's report and the routes
([the link's report](esp32sim-backend-user.md#the-links-report)):

```
routes:
  uart.gnss: the replay of gnss.jsonl; 136 bytes in, 15 out; tapped into /tmp/…/tap.jsonl
sd_card: empty
the run on the board's clock, 12 entries:
  0.000000000 s  test           uart.gnss.replay('gnss.jsonl')
  0.000000000 s  test           uart.gnss.tap('tap.jsonl')
  0.000000000 s  test           board.power_on(), the world's time 2026-01-01T12:00:00Z
  0.000000000 s  test           board.wait(2.5 s)
  0.000001000 s  console        Hello world!
  0.000001000 s  uart.gnss out  $PMTK314,0*28\r\n
  1.000000000 s  uart.gnss in   $GPGGA,120000.00,5213.7806,N,02100.7340,E,1,08,0.9,110.0,M,34.0,M,,*66\r\n
  2.000000000 s  uart.gnss in   $GPRMC,120001.00,A,5213.7794,N,02100.7356,E,0.0,,291026,,,A*70\r\n
  2.500000000 s  test           waits up to 30 s for 'Hello world!' on the console
  2.500000000 s  test           'Hello world!' on the console: happened
  2.500000000 s  test           waits up to 0.5 s for 'Goodbye' on the console
  3.000000000 s  test           'Goodbye' on the console: did not happen within 0.5 s
```

This is `tests/test_pytest_plugin.py`'s fake runner, which prints one line and transmits one
command. Each line is the board's time, who, and what:

- `test`: what the test did. Before power-on everything is at 0. A call of the receiver model at
  the board's time it was made, as `uart.gnss.move(heading=NORTH, speed=m_s(10))`; its route's line
  says `the model of a GNSS receiver`. `board.wait(d)` at the time it started from; a wait for a
  condition or a console pattern when it started, and again when it ended, with `happened`,
  `did not happen within {time}` or `the run ended first`.
- `console`: a line the firmware printed, without its line ending and colour codes, at the time of
  its first byte. Any other control character but the tab is escaped, as `\r` or `\x1b`, so that
  the line cannot write over the report. A line the run did not finish is shown too.
- `{route} out`, `{route} in`: the bytes the firmware transmitted on the route, and those the far
  end sent in, at the time they crossed, whether the route is tapped or not. Printable ASCII is
  shown as it is, `\r`, `\n`, `\t` and `\\` escaped, any other byte as `\xNN`; after 100 bytes the
  line is cut, with the length: `... (512 bytes)`.
- `chip`: the chip reset, at the time of the reset, with the boot it starts and why:
  `boot 2: SW_CPU_RESET (reset cause 12)` after `esp_restart()`, `boot 3: POWERON_RESET (reset
  cause 1)` after `board.reset()` or the `board.power_on()` that follows a `board.power_off()`,
  which are `test` steps of their own ([resets and power cycles](pytest-plugin-user.md#resets-and-power-cycles)).
  Kept as the test's steps are, however long the console.
- `{line} out`, as `modem.pwrkey out`: a level the firmware drove a line of the profile's
  `[lines.<role>]` sets to, `0` or `1`, or `not driven` once its output is off, at the time it
  changed. What the test drives on a line is a `test` step: `lines.drive('modem.ri', 1)`,
  `lines.release('modem.ri')`, every call, also one that changes nothing.

Entries are in the order of the board's time, those of one time in the order they came. The
report keeps every step of the test, and the last 200 lines of the console, events of the
routes and changes of the lines; when it has left some out, its heading says so:
`the run on the board's clock, {n} entries: every step of the test, and the last 200 of the console and the routes:`.
A run with no entry says `the run on the board's clock: nothing happened`.

On a profile with an SD card slot, as `esp32-devkitc`, a line after the routes names the card the
run had in the slot at its end: `sd_card: a card holding PATH`, or `sd_card: empty`. A line under
it for each time the card came out or went in while the board was on gives the board's time the
runner did it at: `  0.400000000 s  taken out`, `  0.600000000 s  PATH put in`. A change the test
made that the run ended before, with no wait after it, follows with the time the test made it:
`  0.900000000 s  not reached: taken out`; a card the runner refused, which fails the run, with
the time it refused it: `  0.900000000 s  refused: PATH put in`. The test's
`sd_card.insert(PATH)` and `sd_card.remove()` are among its steps
([pytest plugin](pytest-plugin-user.md#taking-the-card-out-and-putting-one-in)).

Under those lines comes each fault rule the test gave the card, in the words of its call, when it
gave it, when it cleared it if it did, and how many operations the rule met, then one line for each
of them, at the board's time it started:

```text
  fault 1: sd_card.fail('write', count=1) at 2.5 s; 1 operation
    3.0681844 s  write of block 541 failed, not written
  fault 2: sd_card.delay('read', seconds(0.1)) at 3.5 s, cleared at 5 s; 1 operation
    4.067868737 s  read of block 541 delayed 0.1 s, not written
```

`sd_card.fail`, `sd_card.delay`, `clear()` and `sd_card.clear_faults()` are among the test's steps
too ([pytest plugin](pytest-plugin-user.md#making-the-card-fail-or-slow-down)).

A test that passed gets the `routes:` lines and the slot's, without the run; `pytest -rA` shows
them.

### Unseen replay request history

Replay routes retain the normalised request counts and also list every unknown occurrence under
`unseen request events:`, with its completion time and full escaped raw frame. The JSON route's
`unseen_request_events` entries carry `t_ns` and `request_hex`; repeated requests keep separate
entries in arrival order. This history survives the rolling log dropping old entries and has no
length limit. See [the backend report](esp32sim-backend-user.md#requests-absent-from-a-replay)
for the exact representation and the memory and report-size cost.

### Modem command history

A modem model keeps a complete command history outside the rolling wire log. See
[modem reporting](modem-user.md#pwrkey-and-reporting) for its fields and the memory and JUnit size
cost of long runs. The run log also shows `<line> in`, such as `modem.ri in`, for model-driven
GPIO changes when they reach the link. A pending change cancelled by the test is not logged.

## When the test gets it wrong

In the test body:

| Message | Exception | Cause |
|---|---|---|
| `uart.gnss: the board is on already: tap a route before board.power_on()` | `SourceError` | `gnss.tap` after `board.power_on()`; `the board has been on` once `board.power_off()` has switched it off |
| `uart.gnss is tapped already: one tap per test` | `SourceError` | A second `gnss.tap` in one test |
| `uart.gnss: cannot tap into {path}: {directory} is not a directory` | `FileNotFoundError` | The file's directory does not exist |
| `uart.gnss: cannot tap into {path}: it is the replay of uart.gnss` | `SourceError` | The file is the recording a route replays: the tap would write over it |
| `uart.gnss: cannot tap into {path}: it is the tap of uart.modem` | `SourceError` | Another route is tapped into the file |
| `uart.gnss: cannot tap into {path}: it is the card of sd_card` | `SourceError` | The file is the image of the card in the SD card slot, which the firmware writes |
| `sd_card: cannot hold {path}: it is the tap of uart.gnss` | `SourceError` | `sd_card.insert` of a file a route replays from or is tapped into: the card would write over it |
| `uart.gnss: cannot replay {path}: it is the tap of uart.gnss` | `SourceError` | `gnss.replay` of a file a route is tapped into |
| `uart.gnss: cannot replay {path}: it is the card of sd_card` | `SourceError` | `gnss.replay` of the SD card's image, which the firmware writes |
| `no backend can tap uart.gnss` | `SourceError` | No backend declares a tap of the route |

A backend that does not declare a tap of the route skips the test at the call, with
`esp32sim: uart.gnss cannot be tapped on this backend`
([board profiles](board-profiles-user.md#when-a-backend-cannot-give-it)); `esp32sim` taps every
UART route.

A tap that cannot be written at teardown is an error there:
`esp32sim: the tap of uart.gnss cannot be written: {reason}; its output is in {logfile}`.
For the HCI tap: `esp32sim: the tap of bluetooth cannot be written: {reason}; its output is in
{logfile}`.

## Replaying a tap

`gnss.replay(path)` can replay both passive streams and taps with outgoing commands and incoming
answers. The [matching engine](matching-user.md) groups complete `out` requests with the `in`
events that follow them. Use `protocol=` when automatic AT or GNSS detection is unsuitable;
the tap must use a supported framing and the replay route's clock and interface. A tap ending
part-way through a request is refused ([replay errors](replay-user.md#what-a-replay-refuses)).

## What it does not do

- The `esp32tl` command does not tap: there is no `esp32tl record`
  ([the esp32tl command](cli-user.md)).
- The run report has a line per UART route; esp32tl simulates no network service yet, so there
  are none of those.
- An HCI tap is not replayed: no source plays a btsnoop file back to the firmware.

## See also

- [Taps and the run report: how they work](run-report-technical.md)
- [Recording format](../recording-format.md)
- [Replay](replay-user.md)
- [pytest plugin](pytest-plugin-user.md)
- [esp32sim backend](esp32sim-backend-user.md#the-links-report)
