# esp32sim backend: how it works

The backend is `esp32tl/backends/esp32sim.py`, with the test's clock in
`esp32tl/backends/clock.py`. It keeps the run's log and writes the taps of `esp32tl/report.py`
and `esp32tl/tap.py` ([run-report-technical.md](run-report-technical.md)). The `board` fixture of the
[pytest plugin](pytest-plugin-technical.md) constructs it at setup and closes it at teardown. What
the user sees is in [esp32sim-backend-user.md](esp32sim-backend-user.md).

pytest-embedded runs no service for this backend (`SERVICES["esp32sim"]` is `None`), so the dut
is pytest-embedded's base `Dut`. The runner is spawned the way `pytest-embedded-qemu` spawns QEMU,
a `DuplicateStdoutPopen` on the dut's `msg_queue`, but only at `power_on()`.

## Data flow

`Esp32Sim.__init__(dut, app_path, msg_queue, meta, profile, directory=None, skip=None,
*, build_dir=None)` keeps `profile`, the test's [board profile](board-profiles-technical.md), as
`Esp32Sim.profile`, `directory`, the test file's (the working directory when `None`), as
`Esp32Sim.directory`, and `skip`, what to call for a source this backend does not give (`refuse`,
which raises `SourceError`, when `None`; the `board` fixture passes pytest's skip). Then, in order,
without starting anything:

1. **`load_app(app_path, build_dir=build_dir)`** resolves the selected directory against the app
   (`build` when omitted), checks its `flasher_args.json` is a file, then builds
   `IdfApp(app_path, build_dir=str(build))`. The build directory is absolute on purpose: given
   `build`, `IdfApp` falls back to a `build` directory in the working directory when the app has
   none. It then refuses a target other than `esp32` (`IdfApp.target`: `IDF_TARGET` in
   `config/sdkconfig.json`; without that file, `extra_esptool_args.chip` in `flasher_args.json`;
   `esp32` when neither names one, so a build with neither passes the check),
   `flash_settings["encrypt"]` (any file marked `"encrypted": "true"`), and any of `app.flash_files`
   that is not a file. The result is `Esp32Sim.app`.
2. **`runner_path()`** and **`rom_path()`** resolve the runner and the ROM ELF
   ([Configuration](#configuration)).
3. **The clock, the console and the log.** A `HeldClock` (`esp32tl/backends/clock.py`, below);
   `console_output`, a `bytearray` that gets the bytes of every `console` message of the link
   until the link fails: after an error `BoardLink` reads on, but hands nothing more on;
   `console_lines`, a `ConsoleLines` (`esp32tl/interfaces/console.py`) fed the same messages with
   their times, which `console.line` and `console.lines` read; and `Esp32Sim.log`, a `RunLog`
   (`esp32tl/report.py`).
4. **The UARTs.** For every `[uart.<role>]` route of the profile, `_route` makes the route's
   `Uart` (`esp32tl/interfaces/uart.py`): `Wire(tx, rx, baud)` from the route (`route_wire`),
   `clock=Esp32Sim.clock`, `directory`, `asked=self._asked`, `log=self.log`, and `files`, one
   mapping for all of them of the files the run replays from and taps into. `uart.gnss` gets a
   `Gnss` (`esp32tl/interfaces/gnss.py`), with `now=lambda: hold.now`, the held clock's time in
   nanoseconds, and `world=lambda: self._utc`, the world's time at power-on, for the receiver
   model ([models-technical.md](models-technical.md#data-flow)). An `[sd_card]` slot becomes an
   empty `SdCard` (`esp32tl/interfaces/sd_card.py`) on the slot's `SdCardRoute`, with the same
   `directory`, `log` and `files`, `asked=self._declared` (the capability check without the
   before-power-on one), `now=lambda: hold.now`, `started=lambda: self.started` and
   `ended=lambda: hold.ended`, the `HeldClock`'s word that the run is over, after which it refuses
   fault rules; it is on the link, last, as the one interface whose `card_slot` is true. The chip's
   station becomes a
   `Wifi(now=lambda: hold.now, world=lambda: self._utc, asked=self._asked, log=self.log)`
   (`esp32tl/interfaces/wifi.py`), on every profile.
5. **The lines.** One `Lines(profile.name, profile.lines, now, log=self.log)`
   (`esp32tl/interfaces/lines.py`) for every `[lines.<role>]` set, also on a profile with none,
   with `now` the `HeldClock`'s, so that what the test drives before power-on holds from `t = 0`
   ([pytest-plugin-technical.md](pytest-plugin-technical.md#data-flow)).
6. **`BoardLink(profile.name, [*uarts, lines, wifi, sd_card], chip=profile.chip, on_error=self._end_run, hold=clock, console=self._printed, clock=self.clock, on_reset=self._reset)`**, `sd_card` only with a slot,
   (`esp32tl/link/boardlink.py`) is the harness's end of the link: `Esp32Sim.link`. The harness's
   `hello` names the profile and its chip; `watch` is the pins of every UART route and every
   line; the `Wifi` has no pins, and is the link's one interface whose `ethernet` is true, which
   gets the station's `eth_tx` frames; the `SdCard` has none either, and is the one whose
   `sd_card` is true, which sends the fault rules as `sd_fault` and `sd_clear` and gets the
   runner's `sd_op` ([pytest-plugin-technical.md](pytest-plugin-technical.md#the-sd-cards-fault-rules)).
   It checks that no two interfaces share a pin. `hold` holds
   every turn until the test
   waits; `_printed` appends each console message's bytes to `console_output`, feeds them to
   `console_lines` and hands them to `log.console`, in the link's thread. `_reset` puts each
   `reset` the runner sends into the run's log, `boot N: REASON (reset cause C)`, through
   `RunLog.chip`, also in the link's thread.

**`Esp32Sim.nvs(namespaces, partition="nvs")`** reads the build's `partition-table.bin`
from `app.flash_files`, resolves the name and generates a whole NVS image through
`esp32tl.flash`. It writes `nvs-<offset in hex>.bin` beside `dut.logfile`, retaining it after
teardown. `_nvs` keeps one `(offset, path)` per partition; a later successful call replaces it.
`runner_command(..., extra_flash=())` appends those `--flash-at` arguments after every build file,
so the test overrides any NVS image the build supplies. The call adds
`board.nvs(partition='NAME')` to the run log, without values, and `set_up()` includes
`the NVS partition 'NAME'` so a passing test that never powers on fails teardown.
[Flash contents](flash-contents-technical.md) owns the formats and failures.

**`Esp32Sim.power_on(utc=None)`** raises `BoardOff` when the supply is on
(`the board is on already: call board.power_off() first`). After a `power_off`, with the runner
started, it switches the supply back on (below), and refuses a `utc` with a `ValueError`. The
first time it raises a `TypeError` for a `utc` that is not a `datetime`, and a `ValueError` for
one without a timezone; it keeps `utc` converted to UTC,
or `POWER_ON_UTC`, for `Esp32Sim.utc` and the GNSS receiver's dates, and logs
`board.power_on(), the world's time {utc}` at 0 once the runner has started. Then:

1. **`listen()`** binds a Unix stream socket at `link.sock` in a new directory from
   `tempfile.mkdtemp(prefix="esp32tl-")`, or from `mkdtemp(..., dir="/tmp")` when the first path
   is `SUN_PATH_MAX` (104) bytes or longer, and listens with a backlog of 1. It binds before the
   runner starts, because the runner connects once (protocol §1).
2. **`runner_command(runner, rom, app, HELD_MAX_SECONDS, link, held=True, psram_mb=profile.psram_mb)`** builds
   `[runner, "--chip", "esp32", "--rom", rom, "--max-seconds", "86400", "--console", "both", "--peer-timeout", "0", "--board", "socket:<link>", "--flash-at", "0x1000=<path>", ..., "--psram-mb", "<psram_mb>"]`,
   one `--flash-at {offset:#x}={file_path}` per entry of `app.flash_files`, which `IdfApp` sorts by
   offset; the paths are absolute. `--psram-mb` is the board profile's `psram_mb`, `0` on
   `esp32-devkitc` and `4` on `lilygo-t-a7670g-r2`; a profile without `psram_mb` leaves the pair
   out, and the runner gives the chip no PSRAM, its default. The profile's `flash_mb` is not
   passed: the runner's flash is its default, 4 MiB. With a slot, `sd_card=` adds
   `--sd-spi sclk=N,mosi=N,miso=N,cs=N` from the slot's route at the end, and with a card in it
   `--sd-rw <image>`, the image's absolute path, after that: the slot is there empty, so that a
   card can go in while the firmware runs. With an access point, `wifi=` adds `wifi.runner_args()` after that:
   `--wifi ssid=<ssid>,chan=<channel>[,psk=<password>][,bssid=<bssid>]` and one
   `--wifi-nearby ssid=<ssid>,chan=<channel>,rssi=<dBm>,secure=<0|1>[,bssid=<bssid>]` per network
   around. Without `held`, as the [`esp32tl` command](cli-user.md) calls
   it, the `--console` and `--peer-timeout` pairs are left out, and so is the card; the command
   passes its profile's `psram_mb` too. The runner's options are in
   [runner-user.md](runner-user.md).
3. **`RunnerPopen(msg_queue, command, meta)`** is `DuplicateStdoutPopen` with one change: if the
   process cannot start (`OSError`), it closes the log file the parent opened before the fork,
   which nothing else would close. `DuplicateStdoutPopen` writes the runner's stdout and stderr into
   one log file (`popen-<uuid>.log` in the test's log directory), and starts a forwarder process
   that reads that file in a loop and puts what it reads on `msg_queue`. pytest-embedded's listener
   process takes it off the queue, prints it to stdout and appends it to `dut.log`
   (`dut.logfile`). If the start fails, the socket and its directory are removed before the
   `BackendError`. `Esp32Sim.process` is `None` until it is set here, and `started` says whether
   it is; `powered` says whether it is and the supply, `_supply`, set here, is on.
4. A daemon thread runs **`_serve_link`**: `accept()` with a 0.05 s timeout, in a loop that reads
   `_exited` before each attempt and gives up after a timeout that began with the runner already
   gone, so a runner that connected and then exited is still accepted, and one that exits without
   connecting (a usage error) ends the loop. Then `BoardLink.run(conn)` reads the link to end of
   file and writes the harness's turns.
5. A daemon thread runs **`_mark_end_of_run`**: `self.process.wait()`, sets `_exited`, calls the
   clock's `stop()`, so that a link held in a turn of a runner that is gone lets go, and joins the
   link thread for up to `EXIT_SECONDS`. It then reads `last_output(log)`, the end of the runner's
   log, and appends to the log, through a file object of its own opened in append mode,
   `\nesp32tl: the link failed: {link.error}\n` when the link has an error, and
   `\nesp32tl: the runner exited with code {code}\n`; the forwarder sends them on after everything
   the runner wrote. Last, it ends the clock with
   `the runner exited with code {code}; its last output:\n{output}`, prefixed with
   `the link failed: {error}; ` when the link has an error: a waiting test gets `RunEnded` with
   that reason, and with `stats.t`, where the runner says the run ended, when it sent its stats.
   Because the clock ends only once the link has been read to its end, the reason holds the
   link's final error, and a test's last look at the console sees everything the guest printed
   that the link handed on: all of it, unless the link failed. It is the only thread that waits for the runner: `Popen.poll()` from another thread
   can miss an exit while this one is in `waitpid()`.

**Resets and the supply.** The runner runs from the first `power_on` to teardown, whatever the
board does; the test's calls go to it stamped at the held clock's `now`, through
`BoardLink.board_input`, which sends them in the harness's answer to the turn the link is held at
(the first turn, `t = 0`, when the runner has not reached it yet), when the test next waits,
first, before what the interfaces send ([protocol 5.17 and 5.18](../protocol.md#517-power-ext-105)).
Their `t` is that turn's, so `BoardLink`'s check that a board input is not stamped before its turn
cannot fail with this backend; it is there for another caller:

- `reset()`, with the supply on, queues `ChipReset(now)` and logs `board.reset()`; otherwise
  `BoardOff` (`the board is off: board.reset() resets a board that is on`).
- `power_off()`, once started and with the supply on, and `power_on()` after it, go through
  `_switch(on)`: `Power(now, on)`, `_supply`, then `supply(on)` on every UART of the profile,
  which hands it to a source that is `Supplied` (`esp32tl/interfaces/uart.py`): the GNSS receiver
  model and the modem model ([models](models-technical.md), [modem](modem-technical.md)). A replay
  or a real source is not, and keeps sending. The step goes into the run's log as
  `board.power_off()` or `board.power_on()`. `power_off()` before the runner has started raises
  `BoardOff: the board is off; call board.power_on() first`, and on a supply that is off
  `BoardOff: the board is off already: call board.power_on() to switch it on`.
- `boots` is `Boot(1, Duration(0), 1)`, then one `Boot(n, Duration(reset.t), reset.cause)` per
  `reset` in `BoardLink.resets`, numbered from 2; `boot` is the last (`esp32tl/interfaces/board.py`,
  which also names the causes, `RESET_REASONS`).

`_on()`, which `now`, `utc`, `boots` and every wait go through, asks only that the runner has
started, so the test can wait on a board that is off: the runner idles the guest and still takes
the turns the clock bounds. `_asked` refuses a source or a tap once the runner has started, with
or without the supply: `the board is on already` while it is on, `the board has been on` after.
`nvs` refuses then too, always with `BackendError: board.nvs: write the flash before
board.power_on()`.

A `reset()`, `power_off()` or `power_on()` once the run has ended (the runner exited, or the
link failed, and the backend has ended the clock) raises `RunEnded` at once, through
`HeldClock.check`, and changes nothing: no step in the log, no model switched, nothing queued.
`_mark_end_of_run` ends the clock only after the runner's exit and the link's last messages, so a
call in between still goes through; the next wait raises `RunEnded`.

`boots` changes only as the link reads the runner's `reset`, which comes in the turn after the
reset: right after `board.reset()`, or the `power_on()` after a `power_off()`, `boot` is still
the boot before it, until the test waits.

**The test's clock.** `HeldClock` is the link's `Hold`: `BoardLink` calls `turn(now)` at the start
of every turn until the link fails, before it polls the interfaces, from the link thread; after
an error the link answers the next `sync` with `stop` without asking. `turn` records `now`, counts
the turn, sets `_held`, wakes the test, and waits on a condition variable until the test sets
`_until`, how far the run may go, or the clock is stopped or ended. It returns `_until`, which the
link adds to the turn's bounds as `the test's clock`, or `None`, on which the link answers the
turn with `stop`. So the runner stands still in every turn for as long as the test does not wait.
The test's side runs in the test's thread:

- `wait(duration)` first waits until the link is held at a turn (from power-on to the runner's
  first `sync` the runner loads its images), then, until `now` reaches `now + duration`, sets
  `_until` to that time and waits for the next turn. The runner takes its next turn at the block
  point the `wait` message bounds (protocol §6, rule 1), so the last turn is at that time exactly.
- `wait_until(condition, within, description)` asks `condition()` in the test's thread, outside the
  lock, while the link is held: at once, then after each step to `min(deadline, now + STEP_NS)`,
  `STEP_NS` being 100 emulated milliseconds. A turn the link takes sooner for another reason, at a
  replayed event's time or an interface's latency, is asked too. It returns the condition's first
  true value, and at the deadline raises `NeverHappened`. When the run ends while it waits, or has
  ended before, it asks the condition once more and raises the `RunEnded` unless it holds. An exception from the
  condition propagates to the test, and the link stays held.
- `now` is the block point of the last held turn, 0 until the first.
- `stop()` is the link's side only: the held turn, or the next, answers `stop`, and no turn is held
  after it. It returns whether the link was held at that moment. `end(reason, at=None)` is the
  run's end: the first reason is kept, with `at`, the board's time it ended at when known, and
  every wait from then on raises `RunEnded`, which says `at {at}`, else `after the turn at {now}`,
  since the runner may have gone on past its last turn, or `before its first turn`.

`Esp32Sim` takes the test's durations with `nanoseconds(value, what)`, which takes a `Duration`
(`esp32tl/units.py`) of 0 or more and returns its `ns`, and refuses anything else, naming `what`
(`board.wait`, `board.wait_until: within`, and `within` for `wait_on_clock`, whose callers pass a
`Duration` they have checked). `now` is the clock's time as a `Duration`; `utc` adds
it, in microseconds, to the UTC instant at power-on. `wait`,
`wait_until`, `wait_on_clock`, `now` and `utc` raise `BoardOff` before `power_on()`. `wait_until`
describes its condition as `the condition {condition.__qualname__}`, or by its `repr` when it has
no qualified name; `wait_on_clock(condition, within, description)` takes the description from the
caller, as the console does. `wait` logs `board.wait({seconds})` at the board's time it starts
from, and `the run ended while the test waited` when it raises `RunEnded`; `wait_on_clock`, and
with it `wait_until` and the console's expects, logs `waits up to {within} for {description}`, then
`{description}: happened`, `{description}: did not happen within {within}` or
`{description}: the run ended first`, at the board's time it returns or raises at.

**Sources and taps.** `Uart.replay` calls `asked(Request(interface, source="replay"))` before it
reads the recording, `Gnss`'s first model call `asked(Request(interface, source="model"))` before
it makes the receiver, `Uart.tap` calls `asked(Request(interface, tap=True))` before it makes the
`Tap`, and `SdCard.insert`, `SdCard.remove` and the card builder call
`asked(Request("sd_card", source="model"))` before they act. `_asked` refuses, with
`SourceError`, a request once the board is on
(`{interface}: the board is on already: give an interface its source before board.power_on()`,
and `... tap a route before board.power_on()` for a tap), then does what `_declared` does, which
is all the slot asks, since a card goes in and out with the board on: it refuses one that
`capabilities.check` finds no backend gives, with that error's message, and otherwise, when
`capabilities.refusal(self.name, ...)` gives a reason, calls `skip(f"esp32sim: {reason}")`. It reads the declaration through
`capabilities.declared()` at each call, so a test can swap it. `set_up()` lists
`the {source_kind} of {interface}` for every `Uart` with a source (`the replay of uart.gnss`, `the
model of uart.gnss`) and `the tap of {interface}` for every tapped one, then `the level of {line}`
for every line the test has driven (a `LineEvent` by `"test"`; a release of a line the test does
not drive makes none), then `a card in sd_card` when the slot holds one. This is what the `board`
fixture reports for a test that never powered on.

**The link.** `BoardLink` keeps the run's `RunnerHello` (`link.runner`), its warnings, its `stats`,
the number of turns and the first `error` with its traceback. When it finds the run wrong it calls
`on_error`, once, from the link thread; `Esp32Sim._end_run` then calls `_terminate()`, which sends
`SIGTERM` unless the runner never started, has exited or has had one already, under a lock,
because a second `SIGTERM` kills the runner with 128 + 15. A mismatched handshake does not call
`on_error`: the runner finds the mismatch in the harness's `hello` and exits 3 by itself. What
`BoardLink` checks is in [the link protocol](../protocol.md) and in the docstrings of
`esp32tl/link/`.

**The recordings' clock.** `Esp32Sim.clock` is `recording.EMULATED`, `"emulated-fifo-write"`, unless
a route has a real source ([below](#a-run-with-a-real-source)), and `BoardLink` asks it as its `clock`
when the runner says hello: a runner whose `hello` names another clock fails the link
(`the runner's clock is {clock}, the backend's emulated-fifo-write`), since the UARTs check their
recordings against `Esp32Sim.clock`.

**`ended_while_waiting(error)`** walks `__cause__`, else `__context__`, from `error`, stopping at a
cycle, and says whether it meets a `RunEnded`. The plugin's `pytest_runtest_makereport` uses it to
keep the teardown quiet after a test that has reported the run's end.

**`last_output(log)`** reads the last `LAST_OUTPUT_BYTES` (2048) bytes of the runner's log and
removes colour codes (`remove_asci_color_code`). When the log is longer, the tail starts after its
first `\n`, unless nothing but whitespace follows it: a single long line ending in `\r\n` is kept,
cut at its start, rather than dropped. A log it cannot read gives
`(the log {log} cannot be read: {strerror})`.

**`Esp32Sim.close()`**, from the `board` fixture's teardown:

0. The clock's `stop()`. A board never powered on has no runner, and `close` returns. When the
   link was held at a turn, which is where a test leaves it once it stops waiting, the link answers
   that turn with `stop` at once, and `close` waits up to `EXIT_SECONDS` for the runner to exit,
   which it does with code 0, so that the `SIGTERM` of step 2 does not race the `stop` for the
   run's end.
1. Waits, for up to `EXIT_SECONDS`, until `link.runner` is set (the runner's `hello` has arrived)
   or the runner has exited. The runner registers its `SIGTERM` handler after it connects and
   before its `hello`; a signal before that kills it, so a test that ends while the runner loads
   the images would fail on `-15`.
2. `_terminate()`: `SIGTERM` with `send_signal`, not `DuplicateStdoutPopen.terminate()`, which
   closes the log before the signal; nothing if the runner has exited or `_end_run` has sent it
   already. This ends a run the link was not held in: one still loading its images, or one whose
   test's wall-time timeout broke into a wait.
3. Joins the link thread, then waits for `_exited`, both within `EXIT_SECONDS` of the signal. If
   the runner has not exited, `kill()` (`SIGKILL`) and waits for `_exited` without a timeout. It
   waits on `_exited` rather than on the waiter thread, which also waits for the link.
4. `_close_link()`: joins the link thread for up to `EXIT_SECONDS`; if it still reads (a process
   other than the runner holds the socket), `shutdown(SHUT_RDWR)` ends the read and the thread is
   joined. Then closes the connection and the listener and removes the socket's directory. Then
   joins the waiter thread.
5. `self.process.close()`: stops the forwarder and closes the log. The forwarder is not waited
   for, so the runner's last lines may be missing from `dut.log`; the result does not depend on
   them. Then closes the stdin pipe `DuplicateStdoutPopen` opens and never closes, which the runner
   does not read.
6. `_write_taps()`: for every `Uart` whose `tapped` is set, `Tap.recording(board=profile.name,
   source=uart.source_kind or "none", clock=self.clock, app_sha256=..., tools=...)` written with
   `recording.write` to the tap's path. `app_sha256` is the SHA-256 of the file `flasher_args.json`
   names under `app`, `None` when it names none or the file cannot be read; `tools` holds
   `esp32sim`, the runner's `hello`'s revision, when the runner said one, and `Tap.recording` adds
   `esp32tl`. An `OSError` from the write, or a `RecordingError` from a recording the format
   refuses, is a problem of its own, `the tap of {interface} cannot be written: {error}`, and the
   other taps are still written; the link's error and the exit code still come after it. The
   recording is written whatever the run's outcome, so a failed run leaves what crossed the wire.
7. Raises one `BackendError` naming every problem, joined by `; `: a tap that cannot be written,
   the runner was killed, or its exit code is not 0, and the link has an error. The code is judged
   after the runner has stopped: a runner that crashed just before the signal keeps its own code.
   With `--board` the runner exits 0 on `stop` and on `SIGTERM`, so a signal's code never passes.

**The report.** `Esp32Sim.report(run=False)` is `link.report()`, then `routes:` and one line per
UART route of the profile, `  {interface}: {source}; {bytes}{tap}`: `{source}` is
`the {kind} of {detail}` (`the replay of gnss.jsonl`) or `nothing at the far end`, `{bytes}` is
`{in} bytes in, {out} out` from `log.traffic`, or `no bytes`, and `{tap}` is
`; tapped into {path}` for a tapped route. With `run`, `log.lines()` follows: the plugin passes it
for a test that failed. `Esp32Sim.properties()` is `link.properties()` with `routes`, by
interface: `source` (the kind, or `None`), `detail` (what the source was made from, as the test
named it), `bytes_in`, `bytes_out` and `tap` (the path, or `None`); then `sd_card`, `None`
without a slot, else `image` (`SdCard.held`, the card the run had at its end, or `None`),
`changes`, one `{"t_ns", "image"}` per `SdCard.changes` entry, `refused` and `not_reached`, the
same per `SdCard.refused` and `SdCard.not_reached` entry, and `faults`, `SdCard.properties()`: per
rule `id`, `call`, `t_ns`, `cleared_t_ns` and `operations` (`t_ns`, `op`, `blocks`, `action`,
`delay_ns`, `written` each); and `wifi`:
`None` without an access point, else `Wifi.properties()`, `access_point` (`ssid`, `channel`,
`secure`), `nearby` (`ssid`, `channel`, `rssi`, `secure` each), `switches` (`t_ns`, `on` each) and
`network`, the network's `esp32tl_network: 1` transcript. With an access point `report` ends with
`Wifi.report()`: `wifi: access point '<ssid>', channel <n>, <WPA2-PSK|open>`, a line
`  around: '<ssid>', channel <n>, <rssi> dBm, <WPA2-PSK|open>` per network around, then the
network's `network events:`. With a slot, the `sd_card: a card holding <path>` or
`sd_card: empty` line comes before it, followed by `SdCard.report()`: per rule
`  fault <id>: <call> at <t>[, cleared at <t>]; <n> operation(s)`, then per operation
`    <t>  <op> of block <n> failed, not written` or `<op> of blocks <first> to <last> delayed <delay>, written`.

### Console taps

`Esp32Sim._console` is a `Uart("console", Wire(1, 3, 115200), ...)` used only for tap/path handling;
it is not registered in `BoardLink`. It shares `_files`, `_asked` and `log` with UART routes.
`tap_console(path)` delegates to its `tap`, which logs `console.tap(...)` at 0. `_printed` records
`(t, "out", data)` on its tap before feeding the console buffers and run log.
`set_up` includes `the tap of console`; `_write_taps` includes `_console` alongside `_uarts`,
with source `none`, the run's clock, and the shared `app_image_sha256` helper's hash.

When tapped, `report` adds `console: {n} bytes out; tapped into {path}` and `properties` adds
`console: {"bytes_out": n, "tap": path}`. With no console tap these fields are absent.
`report_section = "esp32sim link"` and `report_property = "esp32sim_link"` are read by the plugin.

### Modem route

The backend builds `Lines` before its UARTs and supplies it, the held clock and `lines.modem`
timing to `interfaces.modem.Modem` on `uart.modem`. A replay does not claim RI. Model selection,
GPIO responses and the command report are specified in [modem-technical.md](modem-technical.md).

### A run with a real source

`Esp32Sim.clock` is a property, `recording.WALL` while any route's `source_kind` is `real`, else
`recording.EMULATED`, so a `real` call that failed moves nothing. `_asked` calls `_refuse_replays` for
a request with `source="real"`, which raises `SourceError` if the run is still on the emulated clock
and a route has a `Replay`. Every `Uart` and the `BoardLink` get `clock=self._run_clock`, and ask
when a replay is given and when the runner says hello; a tap's recording takes it at the end of the
run. `power_on` passes `realtime=self.clock == recording.WALL` to `runner_command`, which appends
`--realtime`. `close` closes
every `Real` in its `finally`, through `_release`, also for a board never powered on. `report` and `properties` add the
port and `dropped`, and `set_up` calls the source `the real device of {route}`.
[real-source-technical.md](real-source-technical.md) has the rest.

## Schema

The end-of-run lines the backend appends to the runner's log, for whoever reads `dut.log`: when
the link has an error,

```
\nesp32tl: the link failed: {error}\n
```

then

```
\nesp32tl: the runner exited with code {code}\n
```

The code is `Popen.returncode`: the exit status, or minus the signal number.

The link's messages are those of [the link protocol](../protocol.md), version 1, encoded and
decoded by `esp32tl/link/protocol.py`.

From the build, through `IdfApp`: `flasher_args.json` (`flash_files`, offset to path relative to
`build/`; the `encrypted` field of the entries that name a file; `extra_esptool_args.chip` when
there is no `sdkconfig.json`), `config/sdkconfig.json` (`IDF_TARGET`).
[validation-firmware-technical.md](validation-firmware-technical.md) describes the builds this
repository makes.

## Interfaces

| Name | What |
|---|---|
| `Esp32Sim(dut, app_path, msg_queue, meta, profile, directory=None, skip=None, *, build_dir=None)` | The board, off. Attributes: `name = "esp32sim"`, `clock` (`"emulated-fifo-write"`, or `"wall"` with a real source on a route: the clock the runner's `hello` must name, a replayed recording must be on, and a tap's recording is on), `dut`, `profile` (the `BoardProfile`), `directory`, `app` (the `IdfApp`), `console_output` (a `bytearray`), `console_lines` (a `ConsoleLines`), `log` (the `RunLog`), `process` (the `RunnerPopen`, `None` until power-on), `link` (the `BoardLink`, complete once `close` returns) |
| `Esp32Sim.power_on(utc=None)` | The first time, starts the runner; after `power_off`, switches the supply back on (no `utc`); `BoardOff` with the supply on |
| `Esp32Sim.power_off()`, `Esp32Sim.reset()` | The supply off; a pulse of EN. Both at the held clock's `now`, sent with the turn the link is held at; `BoardOff` when the board is not in the state they need, `RunEnded` once the run has ended |
| `Esp32Sim.powered`, `Esp32Sim.started` | Whether the supply is on, from `power_on` to `power_off`; whether `power_on` has started the runner |
| `Esp32Sim.boots`, `Esp32Sim.boot` | The chip's boots as `Boot`s, the first at 0 with cause 1 and one per `reset` the link has read; the last. `BoardOff` before the first `power_on` |
| `Esp32Sim.wait(duration)`, `Esp32Sim.wait_until(condition, *, within)`, `Esp32Sim.wait_on_clock(condition, within, description)` | The test's waits, each a `Duration` of the board's time, logged as steps of the test |
| `Esp32Sim.now`, `Esp32Sim.utc` | The board's time since the first power-on, a `Duration`, through every reset and while the board is off; the world's time, a `datetime` in UTC |
| `Esp32Sim.uart(role)` | The `Uart` of `uart.<role>` on the link, a `Gnss` for `gnss`; `BackendError` when the profile has no such route |
| `Esp32Sim.lines` | The `Lines` of the profile's `[lines.<role>]` sets on the link; `BackendError` when the profile has none |
| `Esp32Sim.sd_card` | The `SdCard` of the profile's slot; `BackendError` when the profile has none |
| `Esp32Sim.set_up()` | What the test gave that only a run uses: `the replay of uart.gnss` or `the model of uart.gnss` per route with a source, `the tap of uart.gnss` per tapped route, `the level of modem.ri` per line the test drove, `a card in sd_card`, `the access point of wifi`, `the tap of console`, `the NVS partition 'nvs'` |
| `Esp32Sim.tap_console(path)` | The console `Tap`; before power-on only, shares file collision checks with UARTs and the SD card |
| `Esp32Sim.close()` | Ends the run and writes the taps; raises `BackendError` for a tap that cannot be written, a failing exit code, a killed runner or a link error |
| `Esp32Sim.report(run=False)`, `Esp32Sim.properties()` | The `esp32sim link` report section, with the run on the board's clock when `run`; the `esp32sim_link` user property's data |
| `BackendError` | What keeps the backend from starting, or a failing end |
| `HeldClock(step=STEP_NS)` | `esp32tl/backends/clock.py`: the link's `Hold`, `turn(now)`; `stop()`, `end(reason, at=None)`; the test's `now`, `wait(ns)`, `wait_until(condition, within_ns, description)`, and `check(step)`, which raises `RunEnded` (`the run ended {when} before {step}: {reason}`) once the run has ended |
| `nanoseconds(value, what)`, `seconds(ns)` | `esp32tl/backends/clock.py`: a `Duration` of the board's time to nanoseconds, checked; nanoseconds shown as `12.5 s`, as `str(Duration(ns))` |
| `listen()` | The listening socket and its path, or `BackendError` |
| `runner_path()`, `rom_path()` | The runner and the ROM ELF, or `BackendError` |
| `load_app(app_path, *, build_dir=None, backend="esp32sim")` | The checked `IdfApp` of the selected directory (default `<app_path>/build`); `backend` chooses target/encryption error wording |
| `profile_uart(profile, interface, *, clock, directory, asked=..., log=None, files=None)` | The `Uart` on the route the profile gives `interface` (`uart.gnss`), at its pins and baud; `BackendError` when the profile has no such route. The [`esp32tl` command](cli-user.md) makes its UARTs with it |
| `route_wire(profile, interface)` | The `Wire` of that route, or the same `BackendError`; `Esp32Sim` makes its `Uart`s and its `Gnss` on it |
| `runner_command(runner, rom, app, max_seconds, link, *, held=False, psram_mb=None, sd_card=None, extra_flash=(), wifi=None)` | The runner's argument list; with `psram_mb`, `--psram-mb` of that size; with an `SdCard` holding a card, the runner's card on its pins; with a `Wifi` holding an access point, `--wifi` and `--wifi-nearby` |
| `Esp32Sim.wifi` | The chip's `Wifi`, on the link from construction |
| `RunnerPopen(msg_queue, cmd, meta)` | `DuplicateStdoutPopen` that closes its log when the start fails |
| `refuse(reason)` | Raises `SourceError(reason)`: `Esp32Sim`'s `skip` outside pytest |
| `ended_while_waiting(error)` | Whether `error`'s chain holds a `RunEnded` |
| `last_output(log)` | The end of the runner's log, whole lines without colour codes |

## Configuration

| Name | Value | Used by |
|---|---|---|
| `ESP32TL_RUNNER` | Path of the runner; unset or empty: `ROOT / "runner/target/release/esp32tl-runner"` | `runner_path()`; must be a file with execute permission |
| `ESP32TL_ROM` | Path of the ROM ELF; unset or empty: `ROOT / ".cache/esp-rom-elfs" / ROM_RELEASE / ROM_ELF` | `rom_path()`; must be a file |
| `ROOT` | `Path(__file__).resolve().parents[2]`: the checkout with an editable install, `site-packages` when installed from a wheel | the two defaults |
| `ROM_RELEASE` | `"20241011"`, the esp-rom-elfs release `scripts/fetch-rom-elf` pins | `rom_path()` |
| `ROM_ELF` | `"esp32_rev300_rom.elf"` | `rom_path()` |
| `HELD_MAX_SECONDS` | `86400`, emulated seconds, passed as `--max-seconds` | `Esp32Sim.power_on` |
| `MAX_SECONDS` | `300`, emulated seconds: the default `--max-seconds` of the `esp32tl` command, whose clock runs free | `esp32tl/cli.py` |
| `POWER_ON_UTC` | `datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)`: the world's time at power-on unless the test gives one | `Esp32Sim.power_on` |
| `STEP_NS` | `100_000_000`, 100 emulated milliseconds: the longest step of `wait_until` | `HeldClock` |
| `LAST_OUTPUT_BYTES` | `2048` | `last_output` |
| `EXIT_SECONDS` | `5`, wall seconds: for the runner's exit after `stop` at teardown, for its `hello`, then for its exit after `SIGTERM`, and for each join of the link thread | `close`, `_mark_end_of_run`, `_close_link` |
| `SUN_PATH_MAX` | `104`, the longest Unix socket path on macOS with its NUL; Linux takes 108 | `listen` |

The backend always passes `--rom`: the runner's own lookup prefers the newest release under
`~/.espressif` instead of the pinned one. `--peer-timeout 0` lets the runner wait in a held turn
for as long as the test body runs. A runner left behind by a harness that died sees the socket
close and exits with code 4 (protocol §9), so `HELD_MAX_SECONDS` is there only because the flag is
required. The comment on `MAX_SECONDS` records that hello_world runs 12 emulated seconds in 0.3 s
of wall time on an M2 Pro, and that an image that never idles runs slower.

## Failure modes

`BackendError` messages, prefixed `esp32sim: ` by the `board` fixture when they come at setup or
teardown. The build, the runner and the ROM ELF are checked at setup, in the order below, so only
the first problem shows; the socket and the start come at `power_on()`, in the test body, where the
`BackendError` fails the test with its own traceback:

| Message | Raised by | Cause |
|---|---|---|
| `the board profile {name} has no {interface}; its interfaces are {interfaces}` | `Esp32Sim.uart` | The `gnss` fixture on a route the profile does not have; `{interfaces}` is `BoardProfile.interfaces`, comma-separated |
| `the board profile {name} has no lines; its interfaces are {interfaces}` | `Esp32Sim.lines` | The `lines` fixture on a profile without `[lines.<role>]` sets |
| `{build} holds no ESP-IDF build: no flasher_args.json` | `load_app` | No `build/flasher_args.json` in the app |
| `{build} is built for {app.target}; esp32sim runs the classic esp32` | `load_app` | Target other than `esp32` |
| `{build} asks for flash encryption, which the runner does not do` | `load_app` | A flash file marked encrypted |
| `{build} is not a finished build, missing {', '.join(missing)}` | `load_app` | A file `flasher_args.json` lists does not exist |
| `no runner at {path}: run make setup, or set ESP32TL_RUNNER` | `runner_path` | Not a file, or not executable |
| `no ROM ELF at {path}: run make setup, or set ESP32TL_ROM` | `rom_path` | Not a file |
| `cannot listen on {path}: {error.strerror}` | `listen`, from `Esp32Sim.power_on` | The socket cannot be bound |
| `cannot start {command[0]}: {error.strerror}` | `Esp32Sim.power_on` | `OSError` from the start, for example `Exec format error` |
| `the runner exited with code {code}; its output is in {self.dut.logfile}` | `Esp32Sim.close` | Exit code not 0; error at teardown, unless the call already failed on `RunEnded` |
| `the runner did not exit within {EXIT_SECONDS} s of SIGTERM, and was killed; its output is in {self.dut.logfile}` | `Esp32Sim.close` | No exit within `EXIT_SECONDS` of the signal |
| `the link failed: {link.error}; its output is in {self.dut.logfile}` | `Esp32Sim.close` | `BoardLink` found the run wrong; after an exit-code problem when there is one |
| `the tap of {interface} cannot be written: {error}; its output is in {self.dut.logfile}` | `Esp32Sim.close` | An `OSError` from writing the tap's recording, or a `RecordingError` from making it; first, before any other problem |

In the test body:

| Message | Exception | Cause |
|---|---|---|
| `the board is off; call board.power_on() first` | `BoardOff` | A wait, an expect, `now`, `utc`, `boots` or `power_off()` before the first `power_on()` |
| `the board is on already: call board.power_off() first` | `BoardOff` | `power_on()` with the supply on |
| `the board is off already: call board.power_on() to switch it on` | `BoardOff` | `power_off()` with the supply off |
| `the board is off: board.reset() resets a board that is on` | `BoardOff` | `reset()` before power-on or with the supply off |
| `power_on: utc is {utc!r}, not a datetime with a timezone` | `TypeError`, `ValueError` | Not a `datetime`; a naive `datetime` |
| `power_on: utc is the world's time at the first power-on; it has run on since, and the board's clock with it` | `ValueError` | A `utc` given to a `power_on()` after `power_off()` |
| `{interface}: the board is on already: give an interface its source before board.power_on()` | `SourceError` | A source after power-on, `the board has been on` once the supply is off; not a card, which goes in and out once the run has started |
| `{interface}: the board is on already: tap a route before board.power_on()` | `SourceError` | A tap after power-on; `the board has been on` once the supply is off |
| the message of `capabilities.check`, as `no backend takes a 'replay' source on uart.gnss; the sources there are model` or `no backend can tap uart.gnss` | `SourceError` | A source or a tap no backend declares |
| `esp32sim: {reason}`, as `esp32sim: uart.gnss takes no replay source on this backend, only model` or `esp32sim: uart.gnss cannot be tapped on this backend` | the `skip` given: pytest's skip under the plugin | A source or a tap this backend does not declare |
| `{what} takes a Duration, as seconds({value!r}), not {value!r}` (`as seconds(5)` for what is not a number), `{what} takes a Duration of 0 or more, not {duration}` | `TypeError`, `ValueError` | A duration that is not a `Duration`, or is below 0 |
| `{description} did not happen within {within}: waited from {start} to {end} of the board's time` | `NeverHappened` | `wait_until`'s deadline |
| `the run ended {when} while the test waited {what}: {reason}` | `RunEnded` | The run ended during, or before, a wait. `{when}` is `at {time}` from the runner's `stats`, `after the turn at {time}` without them, the last held turn being only a bound, or `before its first turn`. `{what}` is `until {time}` for `wait`, `for {description}` for `wait_until`; `{reason}` is `[the link failed: {error}; ]the runner exited with code {code}; its last output:` and the tail |

Times in these messages are `seconds(ns)`: seconds without trailing zeros, `1.5 s`, `0 s`.
Anything else `IdfApp` raises, a `flasher_args.json` that is not JSON or has no `flash_files` for
instance, is not a `BackendError` and fails the setup with its own traceback.

`load_app(app_path, build_dir=build_dir, backend="esp32sim")` is also used by serial with
`backend="serial"`: its target and encryption errors name that backend. The default retains the
runner-specific messages above. `app_image_sha256(app)` in `esp32tl/tap.py` hashes the app entry for
both backends; no app entry returns `None`, and read failures raise `OSError` (tap writing catches
those).

## Tests

`tests/test_real_source.py` covers the run with a real source
([real-source-technical.md](real-source-technical.md#tests)).
`tests/test_pytest_plugin.py::test_console_tap_preserves_emulated_output` checks the console
recording after passing and failing assertions, its path and byte count in the report and JUnit
property, and its `console.tap` step in the failure log.
`test_reference_recording_is_not_read_on_esp32sim` powers on with a nonexistent reference marker,
proving serial-only validation does not reach the simulator.

`tests/test_pytest_plugin.py`:

- `test_runner_command_flashes_every_file_at_its_offset`: the exact command line for a fake build
  (`fake_build`), `--board socket:PATH` included.
  `test_runner_command_puts_a_card_in_the_slot_once_the_test_inserts_one`: `--sd-spi` alone for
  an empty slot, and `--sd-spi`, `--sd-rw` with the resolved path once a card is in.
  `test_runner_command_gives_the_chip_the_profile_s_psram`: `--psram-mb 4` for `psram_mb=4`, and
  no `--psram-mb` without it. `test_runner_command_puts_the_tests_access_point_on_the_air`: no
  `--wifi` without an access point, then `--wifi` and `--wifi-nearby` as `Wifi.runner_args()`
  gives them.
- `test_load_app_refuses_what_the_runner_cannot_run`: the four `load_app` messages.
- `test_runner_and_rom_paths_name_what_is_missing`: both messages, a runner that is not
  executable, and the environment variables taking effect.
- `test_rom_is_the_one_make_setup_fetches`: `ROM_RELEASE` and `ROM_ELF` equal `RELEASE=` and
  `ROM=` in `scripts/fetch-rom-elf`.
- With a shell script as the runner (`FAKE_RUNNER`, through `ESP32TL_RUNNER`), which exits without
  connecting: `test_a_wait_fails_at_once_when_the_runner_has_exited` (under 15 s, with the tail in
  the `RunEnded`), `test_a_runner_that_fails_to_start_is_reported_once` (exit 2),
  `test_the_runner_starts_at_power_on_on_the_test_s_clock`
  (`--max-seconds 86400 --console both --peer-timeout 0`),
  `test_the_board_runs_the_profile_the_test_chose_and_starts_no_runner_unpowered`,
  `test_a_runner_that_exits_with_an_error_fails_the_test` (exit 1 and `SIGKILL`, `-9`: the call
  passes, the teardown errors), `test_a_failure_of_another_kind_does_not_hide_the_runner_s_exit`,
  `test_the_board_s_clock_and_sources_go_in_order`,
  `test_a_source_or_a_tap_the_backend_cannot_give_skips_at_the_call`,
  `test_a_test_that_sets_up_a_source_and_never_powers_on_fails_at_teardown`,
  `test_a_test_that_taps_a_route_and_never_powers_on_fails_at_teardown` and
  `test_the_lines_fixture_needs_a_profile_with_lines_and_a_drive_needs_power_on`.
- `test_the_eof_tail_is_the_runner_s_last_whole_lines_without_colour`,
  `test_the_eof_tail_keeps_a_long_last_line` (no ending, `\n`, `\r\n`, and a log that cannot be
  read): `last_output`.
- `test_ended_while_waiting_stops_at_a_cycle`.
- `test_a_runner_that_cannot_start_leaves_no_log_open`: no `ResourceWarning` after an
  `Exec format error`.
- `test_hello_world_fails_on_an_image_that_prints_something_else`: the real runner on a copy of
  `firmware/hello_world/build` whose app image prints `Hello WORLD!`, so the test can fail. It
  fails when `make firmware` has not run.
- With a Python script that speaks the link protocol as the runner (`LINK_RUNNER`, through
  `link_runner`; `$FAKE_LINK` picks how it misbehaves):
  `test_the_backend_serves_the_link_and_ends_the_run_with_stop` (the report section and the
  JSON user property in `--junitxml`), `test_every_route_and_line_of_the_profile_is_on_the_link`
  (the LilyGO profile's `watch` and its `--psram-mb 4`, both of which the script asserts, with
  `$FAKE_WATCH` and `$FAKE_PSRAM`; its pull-down in the first turn is checked in
  `tests/test_lines.py` and `tests/test_gpio_echo.py`),
  `test_the_test_s_waits_move_the_board_s_clock` (`now`, `wait`, `wait_until`, `utc`),
  `test_a_tap_records_what_crossed_the_route_both_ways` (the tap's file, header and events, with
  `FAKE_LINK=tx`, and the routes in a passing test's report),
  `test_the_report_of_a_failing_test_shows_the_run_on_the_board_s_clock` (the run, line by line,
  and the routes in the user property),
  `test_a_tap_that_cannot_be_written_fails_the_test_at_teardown`,
  `test_a_mismatched_handshake_fails_the_test_with_both_sides_reasons` (exit 3, both reasons in
  the `RunEnded`), `test_a_link_the_harness_finds_wrong_fails_the_test_after_its_expects`,
  `test_a_link_error_ends_the_run_at_once_for_a_waiting_test`,
  `test_a_runner_that_ignores_stop_and_sigterm_is_killed_and_fails_the_test` (`EXIT_SECONDS` set
  to 0.5), `test_a_hung_run_is_ended_by_the_test_s_timeout` (`--timeout 2`, and the teardown's
  `SIGTERM`), `test_a_link_left_open_after_the_runner_exits_is_shut_down` (a child holds the
  socket for 30 s; the whole run under 15 s, the child still there, then killed),
  `test_a_test_that_ends_while_the_runner_loads_waits_for_its_hello` (it connects after 0.5 s and
  dies of `SIGTERM` until then) and
  `test_a_held_run_whose_runner_is_gone_releases_the_link` (exit 4 in a held turn).

`tests/test_clock.py` tests `HeldClock`, `nanoseconds` and `seconds` against a runner thread that
feeds `BoardLink`. `tests/test_hello_world.py`, `tests/test_nmea_reader.py` and
`tests/test_gpio_echo.py` run the real images on the real runner and ROM, `test_time_is_emulated`
with `board.now` after the tenth fix, `gpio_echo` with the LilyGO profile's pulled RI.
`tests/test_gnss_fixture.py` runs `nmea_reader` with a replay, and the ordering errors above
([replay-technical.md](replay-technical.md#tests)).

`tests/test_power.py` runs `firmware/persistence` through `esp_restart` and `board.reset()`
without a card, through `board.reset()` with one, and through a power cycle with one: the reset
causes, NVS, RTC and RAM counts and the card's
lines the firmware prints, `boots`, `now` across the power-off, every `BoardOff` and the `utc`
refusal, and that the build's flash files are unchanged.
`tests/test_modem_fixture.py::test_the_modem_keeps_its_power_through_a_reset_and_loses_it_with_the_board`,
`tests/test_nmea_reader_model.py::test_the_receiver_loses_its_power_with_the_board_and_keeps_its_fix`
and `tests/test_http_request.py::test_joins_again_after_a_reset_or_a_power_cycle` check what the
modem model, the receiver model and the station do across them.

`tests/test_boardlink.py` and `tests/test_link_protocol.py` test `BoardLink` and the codec without
a runner, `board_input` and `resets` among them; `tests/test_runner_link.py` runs the real runner against a peer of its own, with the
command line from `runner_command`.

Not covered: that `_end_run` and `close` send `SIGTERM` once only is not asserted, because the
fake runner exits on the first; `listen()`'s fallback to `/tmp` and its `cannot listen` message
are not tested; the `cannot start` message is not checked through the fixture, only
`RunnerPopen`'s `OSError`; nor is a run that reaches `HELD_MAX_SECONDS`.

## See also

- [esp32sim backend](esp32sim-backend-user.md)
- [pytest plugin: how it works](pytest-plugin-technical.md)
- [Runner: how it works](runner-technical.md)
- [Plan section 3, architecture](../implementation-plan.md#3-architecture) and
  [M0](../implementation-plan.md#m0-bootstrap-console-only)
- [Link protocol](../protocol.md): what the backend's `BoardLink` and the runner speak


## Modem network reporting

The modem route receives the same lazy world-UTC callback as GNSS, so NTP uses the value supplied
at `board.power_on(utc=...)`, even when the network was selected earlier. With a selected network,
`report()` appends its full event transcript after AT history. `properties()` adds `network` to
`routes["uart.modem"]`, an `esp32tl_network: 1` transcript containing events and application
plaintext, as described in [network internals](network-technical.md). Raw UART taps still contain
PPP and TLS wire bytes. The route advertises `lossless=True`; its receive FIFO overflows fail
through `BoardLink` immediately.


`Esp32Sim` implements the public `Board` base. Its `lines`, `sd_card` and `wifi` properties
delegate to `_get_lines()`, `_get_sd_card()` and `_get_wifi()`; the first two replace the former
callable backend accessors. `set_up()` lists `the access point of wifi` once the test has given
one, so that a test that gives one and never powers on fails teardown, as for a source. The
runner command, setup guard and reports read the slot's `image_path`. `close()` stops and joins
the runner before closing the SD slot in a `finally` block, so owned images are removed on both
successful and failed teardown. An uninserted built image is also cleaned; merely building does
not count as firmware setup for the no-power-on guard.
