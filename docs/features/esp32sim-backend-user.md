# esp32sim backend

The `esp32sim` backend runs the firmware of a test in `esp32tl-runner`, the program in `runner/`
that runs the [esp32sim](https://github.com/mikroverk/esp32sim) emulator, on the emulated clock. It
is the default backend behind the `board`, `console`, `gnss`, `lines` and `sd_card` fixtures,
with `[esp32sim]` in each test id ([pytest plugin](pytest-plugin-user.md)). A real board is selected
explicitly through the [serial backend](serial-backend-user.md). The emulator runs an unmodified
ESP-IDF build for the classic ESP32.

## What it needs

- **The runner.** By default `runner/target/release/esp32tl-runner`, which `make setup` (or
  `make build-runner`) builds. `ESP32TL_RUNNER=PATH` names another, for example the
  `RUNNER_PROFILE=dev` build in `runner/target/debug/esp32tl-runner`. It must be an executable
  file.
- **The ROM ELF.** By default `.cache/esp-rom-elfs/20241011/esp32_rev300_rom.elf`, which
  `make setup` (or `make rom-elf`) puts there. `ESP32TL_ROM=PATH` names another. The backend
  always hands this file to the runner, so the runner's own ROM lookup is not used
  ([where the runner looks for it](runner-user.md#where-the-rom-comes-from)).
- **An ESP-IDF build** in the directory selected by `app_path(..., build_dir=...)`, defaulting
  to the app's `build/`. Relative build paths are relative to the app; absolute paths work too.
  Built for the `esp32` target, finished, every file `flasher_args.json` lists present, and without
  flash encryption.

The two defaults are paths in the checkout esp32tl is installed from, which holds only with an
editable install; `make setup` makes one. Installed any other way, from a wheel for instance,
esp32tl finds the runner and the ROM ELF only through `ESP32TL_RUNNER` and `ESP32TL_ROM`.
A relative path in either resolves against pytest's working directory, not the test file's.

## How a run goes

Each test that uses `board` gets a board that is off. The test body sets up what is around it, as
`gnss.replay(path)` or `gnss.fix(at=...)` does, then calls `board.power_on()`, which starts the test's own runner with
every file of `flasher_args.json` at its offset; teardown stops it. A test that never calls
`power_on()` starts no runner. The chip gets the board profile's PSRAM, passed to the runner as
`--psram-mb`: 4 MB on `lilygo-t-a7670g-r2`, whose module carries 8, and none on `esp32-devkitc`
([board profiles](board-profiles-user.md#the-profiles-esp32tl-ships)). Its flash is the runner's
4 MB, whatever the profile says.

From power-on the run is on the test's clock: the firmware runs only while the test waits, in
`board.wait`, `board.wait_until` or a console expect, and stands still in between, however long
the test body takes. Every limit a test states, an expect's `timeout` included, is a time on
that clock ([pytest plugin](pytest-plugin-user.md#the-fixtures)). The runner waits for the test
without a limit of its own (`--peer-timeout 0`); a run that hangs is ended by the test's
wall-time timeout, 600 s by default ([the test's timeout](pytest-plugin-user.md#the-tests-timeout)).
A run lasts as long as the test waits, up to a day of emulated time (`--max-seconds 86400`), at
which the runner ends it and a waiting test gets `RunEnded`.

One runner serves the whole test, from the first `power_on()` to teardown. `board.reset()`,
`board.power_off()` and a `power_on()` after it go to it over the link, at the board's time the test
calls them, and the run goes on through them on the same clock
([resets and power cycles](pytest-plugin-user.md#resets-and-power-cycles)). The flash the firmware
writes to, NVS and OTA data among them, is the runner's copy of the build's files: a power cycle
keeps it, and the build's files are never written. The card in the slot is the test's image file,
which keeps what the firmware wrote through every reset and power cycle; with the supply off the
card loses its power, and the firmware's driver has to mount it again. Each reset goes into the
run's log as `boot N: REASON (reset cause C)`, and into the link's statistics as one of its
`resets`.

The console reaches the test over the link, in emulated-time order with the runner's turns
(`--console both`). The runner's stdout gets it too, and with it the runner's own lines
(`esp32tl-runner: ...`): pytest-embedded writes that stream to the test's `dut.log`, in a directory
under the system's temporary directory unless `--root-logdir` names another, and prints it with a
timestamp on each line, which pytest shows as the test's captured stdout when the test fails.

The runner's board is a link to the harness: a Unix socket the backend binds at power-on, before it
starts the runner, in a new directory under the system's temporary directory (under `/tmp` when
that path would be too long for a socket), passed as `--board socket:PATH`. The backend reads the
link in a thread for the whole run, speaking the [link protocol](../protocol.md). Every UART route
of the board profile is wired to the board from the start: the link watches its pins and carries
what the firmware transmits there and, once the test has given the route a source, what the source
sends in ([replay](replay-user.md)). A route without a source listens and says nothing. So is every
GPIO line of the profile's `[lines.<role>]` sets: the link watches it, holds a line the board pulls
at its pull from power-on, as RI on the LilyGO profile reads 0, and carries what the firmware
drives on the lines and what the test drives through `lines`
([pytest plugin](pytest-plugin-user.md#the-fixtures)).

The board profile's SD card slot is not on the link. A card the test inserts
(`sd_card.insert(image)`) is the runner's own SD card model, on the slot's pins, which the backend
passes as `--sd-spi sclk=N,mosi=N,miso=N,cs=N --sd-rw IMAGE`; the card writes into the image file
as the firmware writes ([runner](runner-user.md#put-an-sd-card-in)). An empty slot gives the
runner no card.

The chip's Wi-Fi station is on the link once the test gives it an access point
(`wifi.access_point(...)`, [pytest plugin](pytest-plugin-user.md#the-wifi-fixture)): the backend
passes it to the runner as `--wifi ssid=…,chan=…,psk=…` and each network around as
`--wifi-nearby ssid=…,chan=…,rssi=…,secure=0|1`, and the link carries the station's Ethernet frames
to the simulated network behind the access point, on the test's clock, and that network's frames
back ([network](network-user.md)). Without an access point the runner gets neither flag and the
station finds nothing to join.

## Console recordings

`console.tap(path)` captures link console messages with their emulated times and writes them at
teardown as `interface: console`, `source: none`, with the app hash and runner revision, just as
UART taps do. It shares their file collision checks and no-power-on guard.

The `console.tap(...)` call appears in the run log. A tapped console also gets
`console: <n> bytes out; tapped into <path>` in the report and a `console` object containing
`bytes_out` and `tap` in the `esp32sim_link` JSON property.

## When the runner exits

Once the runner has exited, the console in `dut.log` ends with the line

```
esp32tl: the runner exited with code N
```

where `N` is the exit code, negative for a signal: `-9` when the runner was killed by `SIGKILL`.
When the harness found the link wrong, the line before it is

```
esp32tl: the link failed: {reason}
```

A test waiting on the board then gets `RunEnded` at once, and so does every wait after it. It names
the board's time the run ended at, from the runner's statistics (without them, `after the turn
at` the last turn the test saw, or `before its first turn`), what the test waited for, the exit
code, and the runner's last
output, up to 2048 bytes of its log, from the first whole line, without colour codes; part of it
may be output that earlier expects already read:

```
E   esp32tl.interfaces.RunEnded: the run ended before its first turn while the test waited for 'Hello world!' on the console: the runner exited with code 2; its last output:
E   esp32tl-runner: rom.elf: not an ELF file
```

When the harness found the link wrong, the reason starts with that:
`the link failed: {reason}; the runner exited with code N; its last output:`. A wait whose
condition holds at the run's last turn, or on what the firmware printed after it, still returns,
also when it starts after the end: the backend ends the clock only once the link has been read to
its end.

To wait for the end of the run on purpose, `console.expect(pexpect.EOF)`, with a `timeout` of the
board's time.

## How the run ends

At teardown the backend stops the run at the turn the test left it at: it answers the runner's
held turn with `stop`, waits up to 5 s for the runner to exit 0, and the report's statistics say
`(stop)` at the board's time the test last saw. A runner that is not in a held turn, because it is
still loading the images or the test's wall-time timeout broke into a wait, or that has not exited
by then, gets `SIGTERM`, once it has connected and said hello: the runner handles the signal only
from then on, and before that it would die of it. A test that ends while the runner is still
loading the images waits up to 5 s for that. On `SIGTERM` the runner sends its last warnings and
its statistics over the link and exits 0. The backend reads the link to its end and waits for the
runner; one that has not exited 5 s after the signal is killed with `SIGKILL`. It sends `SIGTERM`
once only: a second one would kill the runner, with exit code 143. Then it writes the recording of
every route the test tapped ([taps](run-report-user.md#tapping-a-route)), whatever the run's
outcome.

When the harness finds the link wrong (a message out of protocol, an interface that broke its
promises or raised, a frame error from a baud mismatch on a replayed route), it ends the run at
once with `SIGTERM`, rather than wait for the runner's next turn, which may come only at the end of
the test's wait. A test that is waiting then gets the `RunEnded` above, the reason first. A
handshake the two sides disagree on is the exception: the runner finds the mismatch itself and
exits with code 3.

## When the run fails

The run passes when the runner exited with code 0, the harness found nothing wrong with the
link, and every tap was written. Anything else is an error at teardown, even when every expect of
the test passed (pytest reports `1 passed, 1 error`). The message names every problem, separated
by `; `:

```
esp32sim: the tap of uart.gnss cannot be written: {reason}; its output is in {logfile}
esp32sim: the runner exited with code {code}; its output is in {logfile}
esp32sim: the runner did not exit within 5 s of SIGTERM, and was killed; its output is in {logfile}
esp32sim: the link failed: {reason}; its output is in {logfile}
esp32sim: the runner exited with code 3; the link failed: {reason}; its output is in {logfile}
```

`{logfile}` is the path of the test's `dut.log`.

The teardown says nothing when the test has already failed on `RunEnded`: it names the code and the
link's reason already. A test that failed for any other reason still gets the teardown error.

The runner's exit codes are in [how a run ends](runner-user.md#how-a-run-ends).

## The link's report

Every test that uses `board` gets a report section `esp32sim link`, which pytest prints with a
failure, or for every test with `-rA`:

```
----------------------- Captured esp32sim link teardown ------------------------
board esp32-devkitc, watching GPIO 4, GPIO 5
runner: protocol version 1, esp32sim 8c652151dd5a521249a785ca147f20a9f8eda68f, clock emulated-fifo-write; 2 turns taken by the harness
stats: the run ended at 0.100000000 s (stop); 2 turns, 0 resets
  unrouted_rx_bytes 0, inputs_on_output 0, inputs_not_applied 0, rx_bytes_not_paced 0
routes:
  uart.gnss: nothing at the far end; no bytes
```

This is `tests/test_hello_world.py`'s. The first line names the test's
[board profile](board-profiles-user.md#choosing-a-profile) and the pins the link watches, those of
every UART route of the profile. It lists the runner's warnings, one line each with its emulated time, the statistics per UART
port, and the harness's error with its traceback when an interface raised. `the runner never
connected`, which is what a test that never powered on gets, and `no stats: the link ended
without them (the runner was killed, or died)` say what is missing. The statistics are those the
runner sends as the run ends, one set per test. `routes:` gives, for every UART route of the
profile, what the test put at its far end, the bytes that went in and came out, and the file it
was tapped into. With an access point, the statistics have a `Wi-Fi:` line, the frames the
station sent and received, and the report ends with the access point
(`wifi: access point 'myssid', channel 6, WPA2-PSK`), a line per network around and the network's
events (`network events:`, as for the modem's network). A test that failed gets the run on the
board's clock after it
([the run on the board's clock](run-report-user.md#the-run-on-the-boards-clock)). The same data,
but the run, is in the test's `esp32sim_link` user property, a JSON string with `board`, `watch`,
`warnings`, `stats`, `error`, `routes`, `sd_card` and `wifi`, which `--junitxml` writes as a
`<property>`; `wifi` is `null` without an access point.

## When the backend cannot start

Unless the table says otherwise, the test gets an error at setup and its body does not run, and
the message starts with `esp32sim: `. The two that come at `board.power_on()` fail the test in
its body instead, as `BackendError: cannot ...`, without the prefix:

| Message | What to do |
|---|---|
| `the board profile {profile} has no {interface}; its interfaces are {interfaces}` | The test uses `gnss` on a profile without `uart.gnss`, or `sd_card` on one without a slot; choose a profile that has it |
| `{build} holds no ESP-IDF build: no flasher_args.json` | Build the app (`make firmware` for this repository's firmware), or fix the `app_path` marker and its `build_dir` keyword; no other build is tried |
| `{build} is built for {target}; esp32sim runs the classic esp32` | Build for `esp32` |
| `{build} asks for flash encryption, which the runner does not do` | Build without flash encryption |
| `{build} is not a finished build, missing {files}` | Finish the build; `{files}` is a comma-separated list |
| `no runner at {path}: run make setup, or set ESP32TL_RUNNER` | Build the runner, or point `ESP32TL_RUNNER` at an executable one |
| `no ROM ELF at {path}: run make setup, or set ESP32TL_ROM` | Fetch the ROM ELF, or point `ESP32TL_ROM` at it |
| `cannot listen on {path}: {reason}` | At `board.power_on()`, which raises `BackendError` in the test body: the backend cannot bind the link's socket in a new directory under the system's temporary directory or `/tmp`; check that it is writable |
| `cannot start {runner}: {reason}` | At `board.power_on()`, which raises `BackendError` in the test body: the runner exists but the system cannot run it, for example `Exec format error` |

`{build}` is the app's `build/` directory. The build is checked first, then the runner and the ROM
ELF, so only the first problem is reported; the socket and the runner's start come at power-on.
The profile's routes are checked when the test asks for `gnss`, and its slot when it asks for
`sd_card`. `sd_card.insert` checks the image; one that changes after it so that the runner's
card cannot open it, removed or made read-only, stops the runner at power-on with exit code 2,
which the test sees as `RunEnded` or at teardown ([when the runner exits](#when-the-runner-exits)).

## See also

- [esp32sim backend: how it works](esp32sim-backend-technical.md)
- [pytest plugin](pytest-plugin-user.md)
- [Runner](runner-user.md)
- [Development setup](../guides/development.md)


### Requests absent from a replay

A replay with unknown requests adds `unseen requests: {count}` under its route in the report,
then one `  {count} x {request}` line per normalised request, in first-seen order. Bytes are
escaped and long requests abbreviated as in the run log. The route in the JSON properties also
gets `unseen_requests`, a list of `{"request_hex": "...", "count": 2}` objects; hex preserves
all normalised bytes.

After the counts, `unseen request events:` lists every occurrence in arrival order as
`      0.000000100 s  AT+UNKNOWN\r`, with its request-completion time and full escaped raw frame.
These lines are not abbreviated; case, whitespace, terminators and binary bytes are retained.
The route's JSON adds `unseen_request_events`, a list of
`{"t_ns": 100, "request_hex": "41542b554e4b4e4f574e0d"}` objects. Repeated requests remain separate
entries even at the same time. Both JSON fields are absent when there are no unknown requests.

The occurrence history is independent of the rolling log and is retained for the whole run;
long runs with many unknown requests therefore use more memory and produce larger reports.
Unknown commands alone do not fail the test; their protocol supplies a fallback response
([matching](matching-user.md)), and a silent fallback still creates a history entry.
