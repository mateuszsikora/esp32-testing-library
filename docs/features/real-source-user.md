# Real source

A real source puts a device on a host serial port at the far end of an emulated route: a GNSS
receiver on a USB serial adapter talks to the firmware running in the emulator, in wall time. Use
it to check firmware against a device no recording or model covers yet, or to record what that
device really says. It runs on the `esp32sim` backend only, and its runs do not repeat: the suite's
deterministic reference stays the [replay](replay-user.md) of a generated recording.

## Connecting a device

```python
import pytest

from esp32tl.units import seconds

pytestmark = pytest.mark.app_path("../firmware/nmea_reader")


def test_reads_the_receiver(board, gnss, console):
    gnss.real("/dev/cu.usbserial-0001")
    gnss.tap("receiver.jsonl")
    board.power_on()
    console.expect(r"fix acquired lat=\S+ lon=\S+", timeout=300)
```

- `gnss.real(port)`, or `board.uart(role).real(port)` for any UART route of the board profile,
  opens the host serial port and returns the `Real`. Call it before `board.power_on()`.
- The port runs 8N1 at the baud of the profile's route: 9600 for `uart.gnss` on `esp32-devkitc`.
  Set the device to the same rate, or give the test a profile whose route has the device's.
- `port` is the whole path. Close whatever else has the port open first, `gpsd` or `cat` for
  example. esp32tl takes the advisory lock on it, which stops a second route and any program that
  asks for the same lock, not one that does not: such a program takes bytes from the firmware without
  a word, and `dropped` does not count them, since they never reached the source.
- Opening the port leaves its DTR and RTS lines at pyserial's defaults. A device with them wired to
  its reset may start again at `gnss.real(port)`. A T-A7670G, measured on its CH9102, does not.
- One source per route, as for a replay or a model.

## What changes in the run

- **The whole run is on the wall clock.** The runner starts with `--realtime`, its `hello` says
  `clock: wall`, and every recording of the run says the same, also the console's and a modelled
  route's. Emulated time keeps its meaning: nanoseconds since power-on, paced to wall time.
- **The test still holds the clock.** The run stands still while the test body runs, and
  `board.wait(seconds(1))` takes about a second of wall time. Any time the test spends between
  waits is time the device goes on sending and the firmware does not see, so keep the test body
  short while the device talks.
- **A test can take longer.** Under `--realtime` the board's time costs its own length in wall time.
  The [test's timeout](pytest-plugin-user.md#the-tests-timeout), 600 wall seconds by default, then
  bounds the board's time too.
- **Do not keep the board off from the start.** A board switched off before it has run about an
  emulated millisecond stalls the run when it comes back, for as long as it was off: after `board.power_on(); board.power_off(); board.wait(seconds(15)); board.power_on()`, the
  next wait takes 15 s of wall time, however short it is, and neither a `timeout` on the board's
  clock nor a signal ends it. Let the board run first, with a short `board.wait`, and this does not
  happen.
- **A replay on another route** takes only a recording on the wall clock, and must be given after
  the real source. A replay given before it is refused
  ([below](#when-the-test-gets-it-wrong)).

## How the bytes arrive

A byte reaches the firmware within about a millisecond of the emulated time at which it reached the
host, while the guest keeps up with wall time. What the USB adapter holds before that is not
removed: an FTDI adapter holds received bytes for up to its latency timer, 16 ms by default, and a
CH9102 at 9600 baud, the adapter on a T-A7670G, until it has a 64-byte USB packet, 67 ms of that line.

The route never falls further behind than a bound: 50 ms, or as long as the line takes to carry 128
bytes if that is longer, 133 ms at 9600 baud and 50 ms from about 25600 baud up. What the device sends
before the run's first turn, which comes when the test first waits, is not the run's: as much of the
end of it as the bound carries goes in with that turn, the rest is discarded, and none of it is
counted. After that, bytes that do not fit are lost, the oldest
first, and counted:

- A turn held for long, by a test that does not wait: the device goes on sending, and only the last
  of it, as much as the bound carries, reaches the firmware.
- A guest that cannot keep up with wall time. CPU-bound code runs at 0.3 to 1.7 times real time
  ([speed](../stage0-findings.md#speed)), a boot among it, so a `dropped` from the boot says nothing
  of the device.
- **A device that delivers faster than the route's line rate.** A receiver with native USB, on
  `/dev/ttyACM0` for one, hands its whole epoch to the host at once, while a route at 9600 baud
  carries 128 bytes in the bound: the rest is lost, every epoch. Give such a device a profile whose route
  runs faster than its bursts, or use a receiver behind a UART adapter. A non-zero
  [`dropped`](#what-the-report-says) is the sign.

The cut falls wherever the bound does, so it can split a sentence, as bytes lost on a wire do.

What the firmware transmits leaves through the port within about a millisecond of its time, as long
as it transmits no faster than the line. Nothing holds it back to that rate as a real UART's FIFO
does, since the emulated UART always reads as idle: a burst, a kilobyte of assistance data for one,
leaves over as long as the line takes to carry it, while the firmware has already gone on. The run
does not wait for it. What the port has not sent when the run ends is not sent, also what its driver
held. The port's rate does not change during the run.

## Recording what crosses the route

`gnss.tap(path)` works as on any route ([taps](run-report-user.md#tapping-a-route)). Its recording
says `source: real` and `clock: wall`; its `in` events are at the source's stamps, without the bytes
it dropped; its `out` events are at the firmware's times.

Such a recording replays only on a route of a run on the wall clock, since a replay refuses another
clock than its backend's ([replay](replay-user.md#what-a-replay-refuses)).

## What the report says

The [link's report](esp32sim-backend-user.md#the-links-report) names the port, the bytes each way
and those dropped, and how many lags the run skipped:

```text
stats: the run ended at 2.549000000 s (stop); 2550 turns, 0 resets
  0 resyncs: stretches of wall time the guest did not see
routes:
  uart.gnss: the real device on /dev/ttys001; 48 bytes in, 0 out, 19 dropped; tapped into t.jsonl
```

- `dropped`, also `real.dropped` and the route's `dropped` in the `esp32sim_link` property: the bytes
  that went over the bound, from the run's first turn to its last. What the device sends once no
  turn is left is no loss of the firmware's, and is not counted.
- `resyncs`, `stats.rt_resyncs`: each is a stretch of over half a second of wall time the guest did
  not see, from a held turn or a guest that fell behind.

## When the test gets it wrong

| What | Happens | When |
|---|---|---|
| The port cannot be opened, or another route or a program with the advisory lock has it | `SourceError: uart.gnss: cannot open the serial port {port}: {why}` | At `gnss.real` |
| `port` is empty or not a string | `TypeError: uart.gnss.real takes the path of a serial port, not {port!r}` | At `gnss.real` |
| The route has its source already | `SourceError: uart.gnss has its source already: one per test` | At `gnss.real` |
| A model call on a route with a real device | `SourceError: uart.gnss has a real device: it has no receiver to describe; one source per test` | At the call |
| After `board.power_on()` | `SourceError: uart.gnss: the board is on already: give an interface its source before board.power_on()` | At `gnss.real` |
| Another route already replays a recording | `SourceError: uart.gnss: a real source puts the run on the wall clock, and {route} replays a recording on the emulated-fifo-write clock: replaying it would compare times across clocks` | At `gnss.real` |
| A replay of an emulated recording after it | `ReplayError: {path} has times on the emulated-fifo-write clock, and {route} runs on the wall clock: ...` | At the replay |
| The port fails during the run: the adapter pulled out | `uart.gnss.poll raised SourceError: uart.gnss: the serial port {port}: {why}` as the link's error; a waiting test gets `RunEnded`, and the test fails | At the next turn |
| The firmware transmits at another baud than the port's, more than 3 % off | `uart.gnss.transmitted raised SourceError: uart.gnss: the firmware transmits at {baud} baud, and the serial port {port} runs at {rate} baud` as the link's error | At that byte |
| The firmware transmits with no baud set | The same, with `the firmware transmits with no baud set` | At that byte |
| The port takes nothing for 5 wall seconds | `uart.gnss.poll raised SourceError: uart.gnss: the serial port {port}: Write timeout` as the link's error | At the next turn |
| The port fails after the run's last turn | `esp32sim: uart.gnss: the serial port {port}: {why}`, which fails the test | At teardown |
| The firmware receives at another baud than the port's | Fails the run as `frm_err`, as for any source ([protocol 5.8](../protocol.md#58-warn-ext)). A device that may send at any moment makes the order of the firmware's calls matter: a byte that comes between setting the RX pin and setting the baud fails the run, in some runs and not others. Set the baud first, as `firmware/nmea_reader` does | At that byte |

A port opened by a test that never powers the board on is closed at teardown, and the test fails
with `the test set up the real device of uart.gnss and never called board.power_on()`.

## On the serial backend

Not attempted: the test skips at the call, and the port stays closed. The device at the far end of
a route on a real board is the one wired to it, which no host port reaches:

```text
serial: uart.gnss has the device wired to the board at its far end: a host serial port goes there on the emulator only
```

## What it does not do

- It does not make a run repeat: run tests with a real source by hand or on a machine with the
  device, and keep them out of the determinism job.
- It does not reconfigure the port's baud when the firmware changes its own: it fails the run.
- It does not model the adapter's delay or remove it.
- It does not drive the device's other lines: a receiver's PPS, for one, is not on the link.

## See also

- [Real source, technical](real-source-technical.md)
- [Link protocol, section 12](../protocol.md#12-wall-time---realtime-and-the-real-source-m5)
- [Replay](replay-user.md), [taps and the run report](run-report-user.md)
