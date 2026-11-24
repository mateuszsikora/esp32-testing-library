# How examples are built and tested

`firmware/build.sh --examples [NAME...]` discovers `examples/*/CMakeLists.txt` and validates
names against those directories before starting Docker or writing output. Without names it
builds every discovered project. The flag is forwarded into the container. The build uses the
same digest, uid, read-only repository mount, writable `/out` mount, provenance JSON and
`esp32tl-built` completion marker as validation firmware; the default output root is `examples/`
in this mode. Without the flag the existing validation-image list and output root are unchanged.

The Makefile discovers the same projects through `wildcard`. `make examples` rebuilds all;
`make test` depends on their completion markers. pytest's `testpaths` includes `examples`, and
mypy checks its Python tests. CI hashes `examples/**` as well as `firmware/**`, builds both in
its firmware job and packs their runtime files into one tarball. Every test matrix member runs
the examples from that restored build, without Docker. See [CI](../cicd.md).

The examples' tests and helpers import from the public modules only, `esp32tl`, `esp32tl.units`
and `esp32tl.recording` ([the public API](pytest-plugin-user.md#the-public-api)), and annotate
the board as `Board`, never as a backend's class, as a project of its own would.

## GPS alarm implementation

`examples/gps_alarm_clock/main/main.c` sets GPIO18 low before enabling its output and reads
three typed NVS keys once at boot. An incomplete or invalid configuration returns from
`app_main` with the buzzer off; it does not erase NVS or substitute a destination.

The example compiles `firmware/nmea_reader/main/nmea.c` through its component CMakeLists, using
its existing checksum, GGA/RMC and integer-microdegree parsing. It therefore builds from this
checkout, not from a standalone copy of its directory. The sentence buffer is bounded to NMEA's
82-character limit, including line endings. `$` restarts reception and oversized sentences are
dropped. The parser's refusal of a recognised GGA/RMC sentence clears the buzzer; unrelated
sentence types leave the current state alone. The fix expires after 2.5 s without a valid
position, including when the receiver sends only malformed data. Expiry uses `esp_timer` and a
100 ms UART timeout on the firmware clock.

Distance uses the haversine formula with a 6371000 m spherical Earth and `double`, clamping
roundoff at antipodes before the square roots. The comparison is inclusive (`distance <=
radius`). GPIO is updated on every parsed GGA/RMC, so the example does not deduplicate an epoch's
two sentences or add hysteresis. It sends no configuration commands to the receiver.

`examples/gps_alarm_clock/tests/test_alarm.py` boots the image through the public pytest plugin.
Its local `configure_alarm` helper translates `Position`/`Distance` to the firmware's typed
NVS keys before boot; the `buzzer` fixture returns `board.lines.output("alarm.buzzer")`, an
`OutputLine`, and the tests reach the receiver as `board.gnss`. Tests move the model on emulated
time and pass its `is_on`/`is_off` conditions to `board.wait_until`.
They calculate travel time to the radius separately from the two-fix response budget and check
the output's event history for unwanted pulses. Its non-default target/radius cases catch a firmware that ignores
NVS, with separate tests for smaller and larger configured radii. Its silence and loss cases
catch a buzzer left on by a stale position; loss also checks three subsequent seconds of
firmware history for reactivation. Departure checks that the buzzer stays on before the boundary.
The boot-window helper skips unknown firmware levels only before the first driven level.
Thereafter it rejects any active or undriven event, even if the output recovers, and reports the
first offending timestamp and level. It requires the current state to be off, so an output
that never initializes fails too. `tests/test_alarm_example.py` checks recovery after pulses
and releases, a pulse at the initialization timestamp, input filtering and an uninitialized pin.
Strict interval assertions require a known starting state.

The silence case stops GNSS immediately after observing the first on state, avoiding an
arbitrarily old last fix. `FIX_MAX_AGE = seconds(2.5)` and `EXPIRY_TOLERANCE = seconds(0.2)`
require continuous on history for 2.3 s and off within the following 0.4 s. The tolerance covers
the wait's 100 ms observation granularity, completion of the first NMEA epoch already queued,
and the firmware's 100 ms UART polling. This is an externally observed expiry window, not
an assertion about the exact internal `last_fix` timestamp.
The separate `gps-alarm-clock` profile gives this assembly's wiring without changing the generic devkit.

## SD logger implementation

`examples/sd_data_logger/main/main.c` uses ESP-IDF's SDSPI host and FAT VFS, with SPI2 on
GPIO14/15/2/13 and `format_if_mount_failed = false`. GPIO18 starts high and becomes low only when
the mount succeeds. After that, `failing` follows the last `append_fix`: an `fopen`, `fprintf` or
`fclose` failure sets it and GPIO18, a success clears both, and each change prints its line
(`SD card write failed`, `SD card writes again`). The UART driver is installed before mounting so
it can buffer incoming sentences during card discovery, and while a slow card keeps a write
waiting. The application continues its UART loop after storage failure and reports accepted
fixes on the console. It does not retry mounting, nor the row that failed: the next fix is the
next attempt.

Like the alarm, this project compiles `firmware/nmea_reader/main/nmea.c` from the checkout.
`dated_fix` accepts RMC only, delegates checksum/status/coordinate parsing, and validates its
date and time locally. The date must be six digits and a real calendar day in 2000–2099;
the time is six digits (00–23 hours, 00–59 minutes and seconds), optionally followed by a dot
and at least one decimal digit. Leap seconds are rejected. The NMEA buffer is bounded to
82 characters including CRLF; `$` restarts reception and overflow drops the sentence.
Only validated digits enter the 8.3 filename `YYYYMMDD.CSV`, avoiding long-filename settings
and preventing the receiver from selecting an arbitrary path.

`append_fix` opens the day's file with `"a"`, writes the original NMEA time and decimal
coordinates rounded to microdegrees, and closes it before printing `(saved)`. Closing flushes
stdio and synchronizes the FAT file. There is no cached open file across dates, no GGA-derived
date carryover and no duplicate-sentence suppression. A write failure can leave a partial row;
the example promises neither atomic rows under power loss nor recovery from a damaged FAT. With
the runner's card failing one block write, the block that fails is the row's data block (block
541 on the tests' 32 MiB image, then the directory entry's, 509, is not written), the file reads
as it was before the row, and the next fix's row is appended after the last one saved.

The tests use the public `Board` API, with `board.gnss`, `board.sd_card`, `board.lines` and
`board.console`. They prepare 32 MiB FAT images through `board.sd_card.image(...).build()`;
`with_text(...)` seeds the append scenario. Every scenario inserts its card explicitly before
power-on. `CardImage.read_text` and `list_files` inspect results with ordinary relative names.
FAT commands and temporary-file ownership live in the library; there is no local card fixture
or filesystem helper. The malformed-recording scenario still uses pytest's `tmp_path` for its
application-specific input recording.
Model scenarios boot on a whole UTC second, so the first fix arrives at 1 s and subsequent
fixes at `FIX_INTERVAL = seconds(1)`. Their first read follows the expected number of intervals
plus `WRITE_MARGIN = seconds(0.5)` for UART reception and closing the FAT row. Later waits add
whole intervals, preserving the margin before the next epoch. This is a fixed processing budget,
not a wait until the contents happen to match: exact row counts and deadlines remain assertions.
`board.wait` stops at those chosen times before mtools reads the image. Exact row counts catch double logging of RMC/GGA;
pre-existing rows catch truncation. The rollover tests check the first file both before and
after midnight. The empty-slot test supplies a fix after the mount has failed, observes two
distinct later timestamps and requires continuous error-pin history between them.
`next_unsaved_fix_time` encapsulates the console pattern, waits at most two fix intervals for
an unsaved fix, and returns its UTC `datetime`; the scenario compares dates and ordering.
The malformed-input scenario keeps its raw NMEA bytes visible, including deliberately broken
checksums. `write_gnss_recording` packages them into a recording without modifying the bytes;
the scenario specifies the first send time and spacing in units, then waits until the last
sentence's scheduled time plus the same write margin.

The storage-failure scenarios give the card a rule after the first rows are on the image, then
wait for the firmware's console line with `board.wait_until(board.console.line(...))`, whose
`ConsoleLine.at` they compare with the rule's `hits`. The failed-write scenario uses
`fail("write", count=1)` and checks the hit (`write`, `error`, not written), the `(not saved)`
fix, GPIO18 high, the image unchanged, then `SD card writes again` at the next fix, GPIO18 low,
the rule spent and the next row appended. The failed-read scenario uses `fail("read", count=1)`:
appending reads the row's sector first, the driver misses the error token and times out, so the
failure comes 1 s after the hit, with `sdmmc_read_blocks failed (0x107)` on the console. The next
fix arrived during that second, so GPIO18 goes high and low again within milliseconds, and the
scenario checks the pin's events, not its level. The slow-write scenario uses `delay("write", ms(300))`,
lasting: a row writes two blocks, its data and its directory entry, so its `(saved)` line comes
at least 300 ms after the last hit; then `clear()` and one more row with no more hits. The
timeout scenario uses `delay("write", seconds(6), count=1)` and checks that the failure comes 5 to
6 s after the hit, the hit `written`, and that `SD card writes again` comes no earlier than 6 s
after it; the rows saved from then on are on the image. It takes about 10 wall seconds: ESP-IDF
polls the busy card one byte per transaction for 5 emulated seconds.

## Periodic wake implementation

`examples/periodic_wake/main/main.c` depends on `esp_driver_gpio` and `esp_hw_support`. `wakes`,
`RTC_DATA_ATTR`, is in `.rtc.data`: the bootloader loads it from the image, as 0, at every boot but
a wake from deep sleep, so the count starts again at a power-on and a reset, and `app_main` adds
one when `esp_sleep_get_wakeup_cause()` is `ESP_SLEEP_WAKEUP_TIMER`. The firmware never sets it to
0 itself: the reset test shows what RTC memory does, not what the firmware does. The pulse resets
GPIO18, sets it low, enables the output, then holds it high for `PULSE_US`, 20 000, with
`esp_rom_delay_us`, a busy wait rather than a FreeRTOS delay so that its length is not rounded to a
10 ms tick. `SLEEP_US`, 15 minutes, goes to `esp_sleep_enable_timer_wakeup` after the count's line,
with no other wake source armed. `sdkconfig.defaults` adds
`CONFIG_BOOTLOADER_SKIP_VALIDATE_IN_DEEP_SLEEP=y` to the 4 MB flash: a wake boots the image it slept
in, so the bootloader does not check it again.

Measured on esp32sim at the pin `afcfba6`, and the same at `c99fb28`, on an M2 Pro: the first pulse starts 45 ms after
power-on, and each later one 21.6 ms after its wake. An interval from pulse to pulse is 900.266 s:
the 900 s the firmware asks, the boot and the 20 ms pulse, and about 0.22 s, 0.025 % of the sleep,
by which the sleep lasts longer than asked, as `deep_sleep`'s 20 s sleep wakes 20.004 s after its
line ([validation firmware](validation-firmware-technical.md#deep_sleep)). The day, 95 wakes, ends
85 525 s into the run, 875 s short of the backend's `--max-seconds 86400`, and takes 192 turns of
the link (`board.link.turns`, on both pins) and about 7 s of wall time, 0.4 s of it in the waits
for the wakes and 6.6 s in the boots; before the runner stepped through the sleeps
([decision](../decisions/runner.md#a-deep-sleep-runs-in-long-steps-of-the-rtc-to-just-short-of-its-alarm))
it took 330 s.

`tests/test_periodic_wake.py` uses the public `Board`, with `board.lines.output("wake.pulse")`
and `board.console.line`. `next_count` waits up to `BOOT_TIME` for the count's line, the first
that ends after the call, so it reads each boot's line once. `pulse_starts` walks the line's
firmware events: each high level starts a pulse, and the next low or undriven level ends it, which
must come 20 to 21 ms later; a pin still high at the end fails it. `assert_every_interval` checks
each pair of pulse starts, naming the pulse that broke the interval.
`test_a_change_made_while_asleep_finds_the_pulse_of_the_next_wake` makes each
`board.next_sleep()` before the sleep it waits for: right after `board.power_on()`, and before
`wait_for_wake`. One made after `next_count` waits for the sleep after the next wake: the wait for
the count's line returns at 0.1 s of the board's time, and the chip already sleeps then. It makes
`pin.next_change(on=True)` once the first sleep has begun, after the power-on pulse and the pin's
change to undriven, and asks it only after the second sleep has begun, when the wake's pulse is
over. The day's test,
`test_a_day_of_wakes_spends_less_wall_time_asleep_than_booting`, reads `time.monotonic()` around
each `wait_for_wake`, the sleep, and each `next_count` after it, the boot, and requires the
sleeps' sum to be less than the boots': a busy host slows both down alike, the runner's skip
takes each sleep to microseconds, and without it each costs about 3.4 s of wall time against a
boot's 70 ms. The reset test calls `board.reset()` five minutes into the
second sleep, and reads `board.boot.cause` only after the count's line, since the reset reaches the
runner at the next wait. The profile is a file beside the tests, not one esp32tl ships
([decision](../decisions/firmware.md#the-periodic-wake-sleeps-a-fixed-15-minutes-and-brings-its-own-profile)).

## OBD-II reader implementation

`examples/obd_reader/main/main.c` depends on `bt` and `nvs_flash`. Its `sdkconfig.defaults` are
those of ESP-IDF's `bt_spp_initiator` (`CONFIG_BT_ENABLED`, `CONFIG_BTDM_CTRL_MODE_BR_EDR_ONLY` and
the other two modes off, `CONFIG_BT_CLASSIC_ENABLED`, `CONFIG_BT_SPP_ENABLED`,
`CONFIG_BT_BLE_ENABLED=n`), with the 4 MB flash. ESP-IDF v5.3.6 has no `CONFIG_BT_SSP_ENABLED`
symbol, so Secure Simple Pairing is turned off in code, with `ssp_en = false` in the
`esp_bluedroid_config_t` given to `esp_bluedroid_init_with_cfg`. `app_main` initialises NVS,
releases the LE memory, enables the controller in `ESP_BT_MODE_CLASSIC_BT`, sets a variable PIN
type, and starts SPP in callback mode (`ESP_SPP_MODE_CB`) with L2CAP ERTM off, so the channel is in
L2CAP's basic mode, which is all an `SppPeer` offers.

Bluedroid runs the GAP and SPP callbacks in its own task, one at a time, and they keep the state:
`IDLE`, `SEARCHING`, `CONNECTING`, `CONNECTED` and `PAUSED`. `ESP_SPP_INIT_EVT` sets the scan mode
to neither connectable nor discoverable and starts the first search. `search()` starts an inquiry
(`ESP_BT_INQ_MODE_GENERAL_INQUIRY`, length 4, no limit on responses) unless one is still under way,
which it knows from `ESP_BT_GAP_DISC_STATE_CHANGED_EVT`; when the inquiry stops while the state is
`SEARCHING`, the next one starts. `ESP_BT_GAP_DISC_RES_EVT` compares the complete local name in
the extended inquiry response (or the short one) with `ADAPTER_NAME`, byte for byte. On a match it
keeps the address, cancels the inquiry and starts the SDP search at once, as `bt_spp_initiator`
does. `ESP_SPP_DISCOVERY_COMP_EVT` connects to the first channel found. `ESP_BT_GAP_PIN_REQ_EVT`
answers with `ADAPTER_PIN`. A failed pairing ends in `ESP_SPP_CLOSE_EVT` while the state is still
`CONNECTING`; that close, a failed `ESP_SPP_OPEN_EVT` and an SDP search with no channel all call
`search_later()`, which sets `PAUSED` and the event group's `PAUSE` bit. The reader task then waits
`SEARCH_PAUSE_MS`, 5000, and calls `search()`: it changes the state only while it is `PAUSED`,
when no callback changes it.

Each session that opens gets a number: `ESP_SPP_OPEN_EVT` counts it in `sessions`, puts it in
`session`, keeps its `handle`, clears `pause_after_close`, so that the session's close starts a
search at once unless the reader sets it again, and sets `OPENED`. A close while `CONNECTED` sets
`session` to 0, then the `CLOSED` bit, and starts the next search: at once, or through
`search_later()` when the reader set `pause_after_close` before it disconnected. The reader takes
`OPENED`, keeps the session's number as `mine`, and runs `talk(mine)`; every `ask()` and every pause
in it checks that `session` is still `mine`. A session that closed, and another that opened in its
place during one of the reader's waits, therefore ends the old `talk()` at its next check, and the
reader starts the new one from `ATZ` when it takes the `OPENED` the new one left. `rest()` waits on
`CLOSED` with the pause as its timeout, so a close wakes it at once; a `CLOSED` from an earlier
session wakes it too, and it waits on to the end of the pause.

`ESP_SPP_DATA_IND_EVT` copies the bytes into a stream buffer of 256 bytes, without waiting: what
does not fit is dropped. `ask()` empties the stream buffer, writes the command and its `\r` with
`esp_spp_write`, and reads in steps of at most 50 ms until the last byte is `>`, until
`ANSWER_TIMEOUT_MS`, 2000, has passed in FreeRTOS ticks since the write, until 255 bytes have come
without a prompt, or until the session has closed. The steps let it see a session that closed
during a wait. It then splits what it read at `\r` and `\n`, drops empty pieces and the echo of
the command, and joins the rest with `\n`. A failure other than the close names itself in
`failure`: `stopped answering`, `sent an answer too long to read` or `could not be written to`.
`mode01_answer` reads each line in turn as hex bytes, with or without spaces, and takes the first
that starts with `41` and the PID, so `SEARCHING...` and other lines before it are skipped. Bit
32 − n of the four bytes answering `0100` says whether PID n is supported. `talk()` ends with
`LOST` when an `ask()` fails or the session closed, and with `NOTHING_TO_READ` after
`not an ELM327 adapter` or a vehicle with none of the three readings. While the session is still
open, the reader prints `the adapter` and the `failure` for `LOST`, sets `pause_after_close` for
`NOTHING_TO_READ`, calls `esp_spp_disconnect` and waits on `CLOSED` until `session` is no longer
`mine`. The firmware's times are whole 10 ms ticks, counted from the instant it wrote or the
instant the session closed.

Measured on esp32sim at the pin `c99fb28`, on an M2 Pro, with the adapter on the air at power-on:
the first search starts 0.281 s after power-on, the adapter is found 0.1 s later, the pairing ends
at 0.752 s, the channel opens at 0.871 s and the adapter hears `ATZ` at 0.901 s. With the model's
50 ms answers and 0.5 s reset, the first reading is printed at 1.771 s, and each round of three
readings takes 0.18 s plus the 1 s pause, so their lines are 1.18 s apart. With another PIN, each
attempt takes about 0.4 s, from the search to `could not connect`, and the attempts are 5.4 s
apart. In the hang-up test's steps, `board.wait_until` returns on the board's next poll after the
reading line, at 1.871 s, 0.1 s after the line at 1.771 s, and the adapter ends its link then; the
firmware prints `disconnected` 11 ms later, finds the adapter 29 ms after that, and is connected
again at 2.332 s, 0.56 s after the reading, inside the 1 s pause.
The nineteen tests, twelve on the board and seven of the model alone, take about 10.5 s of wall
time.

`tests/elm327.py` is a dataclass with no board and no waits, a subclass of
`esp32tl.sources.model.ReactiveModel`: `receive(at, data)` gets the instant as a `Duration`, and
every time it returns or keeps is one. `receive` buffers what the reader writes up to each `\r`,
records the command and `at` as a `Request`, and returns the answer at `at + answer_after`
(`reset_time` for `ATZ`). The base takes `latency()` from the `answer_after` field and sends
nothing unprompted, as the adapter never speaks unprompted, so `receive` is the only method of the
four the model defines. `Vehicle.supported()` encodes the readings it has, by
PID. The echo uses the setting from before the command, so `ATE0` is still echoed, as an ELM327
does. `protocol_found` is false after `ATZ` and `ATSP`; the first mode 01 request with the
ignition on prefixes `SEARCHING...` and sets it, and with the ignition off it stays false.

`tests/test_obd_reader.py` uses the public `Board`, `board.bluetooth` and `board.console`. Each
console condition is made before the action that causes its line: `disconnected` and `searching`
are made before the silent adapter's timeout, since the firmware prints `disconnected` and
`searching` at the same instant. The timeouts and the pause are checked to within `TICK`, 10 ms,
from the time the adapter heard the command (the time a line came, for the pause). The searches are
checked to within 0.1 s and the ignition retries to within 0.2 s, since each retry also waits for an
answer. `next_reading` waits at most one pause and a second for the next reading line and returns
its `name=value` pairs. The tests of a silent adapter and of one that ends its link count
`bt.pairings()`: the reconnection adds none. The test of the adapter that ends its link waits
for `peer.disconnect()`'s condition, within 100 ms, which ends every link of the peer with reason
`0x13`; it checks that the new connection comes less than `READ_PERIOD` after the last reading,
inside the reader's pause, which is where the old session's `talk()` would otherwise carry on. The
two endings with nothing to read share one parametrised test, with the model's keyword arguments
as its parameter.

## Documentation and builder checks

`scripts/check-docs.py` owns `examples/` through F24; Python test files under each example's
`tests/` are exempt just like the root suite. `tests/test_check_docs.py` checks both behaviours.
`tests/test_firmware_build.py` exercises example discovery, refusal of unknown names, container
forwarding and failed-build completion markers against fake tools.

## Position tracker implementation

`examples/position_tracker` uses the LilyGO profile's UART2 GNSS and UART1 modem. Its CMake
configuration calls `firmware/pppos_client/prepare.py` to fetch the same SHA-256-verified
esp-protocols `modem-v1.4.1` revision as the validation image. The source tree lives under
`build/dependencies/`; `EXTRA_COMPONENT_DIRS` points to its `esp_modem`. Component-manager
resolution is disabled for this project because all components are supplied by that tree or
ESP-IDF. Configuration therefore writes neither a lockfile nor managed components into the
read-only project mount. The usual example discovery, build provenance and CI cache apply.

`main/main.c` reads the five required `config` NVS keys before creating tasks or connecting.
The host buffers accept up to 253 characters, APN up to 63; CA storage is allocated from the NVS
string size, limited to 8192 bytes including its terminator. Missing, empty, oversized or mistyped
values, or a zero port, stop startup without erasing flash. PEM syntax and certificate trust are
checked by the TLS client when it connects.

A dedicated GNSS task uses the shared `firmware/nmea_reader/main/nmea.c` parser. A mutex guards
its latest position, validity and monotonic reception timestamp. Recognized invalid GGA/RMC
sentences clear validity; unrelated sentences are ignored. A bounded sentence buffer restarts
at `$` and drops oversized input. A report uses a snapshot under the mutex, marking a fix older
than 2.5 seconds invalid. NMEA supplies position only; the report's timestamp comes from NTP.

`esp_modem` uses SIM7600 commands, 115200 baud and no hardware flow control, with the APN from
NVS. After PPP supplies IP and DNS, `esp_netif_sntp` requests the configured NTP hostname. Startup
waits for successful synchronization, logging `position_tracker: waiting for NTP` on each
10-second wait timeout. MQTT then connects using `MQTT_TRANSPORT_OVER_SSL`, the provisioned
CA and hostname verification; there is no plaintext fallback. `sdkconfig.defaults` explicitly
enables `CONFIG_MBEDTLS_HAVE_TIME_DATE=y` so mbedTLS also checks certificate validity periods
against the NTP-synchronized system clock. The client ID is `position-tracker`.

The MQTT connected callback notifies the main task. That task publishes immediately, and resets
its monotonic 10-second report deadline on every MQTT connection. Subsequent deadlines use
`esp_timer`, independent of UTC adjustments. Disconnected deadlines produce no report and no
offline backlog; MQTT handles its own reconnects while PPP remains available. The example does
not implement modem power sequencing or redial after PPP loss. The modem must already be powered
and registered (the model's initial state). All objects and configuration buffers live for the
application's lifetime.

Reports use QoS 0, retain false, on `tracker/position`. `time` is integer Unix UTC seconds;
`lat`/`lon` are decimal degrees with six places, or both JSON null when `fix` is false. Publishing
failure logs `position_tracker: publish failed`. The no-fix report still carries synchronized time.

`tests/test_tracker.py` requests only the public `Board` plus parametrized scenario data.
Each scenario explicitly calls the local `serve_broker(board, ca=None)` helper. It selects
`board.modem.network()`, creates default credentials only when none are supplied, registers the
broker's TLS listener and returns `(Internet, Tls)`. This groups the repeated three-line setup
without adding a library convenience API or another context object. A caller-provided CA goes
unchanged into the listener, including custom validity dates. The helper never writes NVS,
powers on or waits. `Reports(network.broker)` remains an independent application adapter.
GNSS and console access use `board.gnss` and `board.console`; there are no setup fixtures or
decorators. `configure_tracker(board: Board, ...)` only writes NVS. Power-on and every wait remain
explicit. The transport test checks `tls.handshake_completed()` and retains the simulator's
empty-warning assertion through `board.properties()["warnings"]`, without accessing `board.link`.
Using the shared board type does not add hardware network modeling support.

`Report.decode` validates the exact JSON fields and their types: a boolean fix, integer Unix
seconds, numeric coordinates within the position bounds for a fix, and null coordinates without
one. It returns an aware UTC datetime, a `Position` or `None`, and the message's `Duration`
timestamp. Invalid data raises `ValueError` with the received time.
`Reports.when_received(with_position=False)` takes `broker.mark()` when constructed and returns a
condition reading `broker.find_messages(TOPIC, since=mark, client=CLIENT_ID)`: only subsequent
publications on the exact topic by the firmware's client ID, `CLIENT_ID = "position-tracker"`.
Neither construction nor polling consumes messages or advances time. Position waits skip valid
no-fix reports, but reject malformed ones;
later invalid messages cannot mask an earlier match. The history view validates every matching
publication, reading `find_messages` with the same topic and client from the start, so unrelated
topics, the test's publications and any other client are left out. The
condition is `esp32tl.condition(find, description)`, a
[described condition](pytest-plugin-user.md#described-conditions): `find` closes over the
captured mark, and the description is `a tracker report` or `a tracker report with a position`,
which both board backends name the wait by. Its `bool()` raises
`TypeError("call the condition 'a tracker report' with (), or pass it to board.wait_until")`, or
the same with the other description, both before and after a match, to reject assertions on an
unevaluated condition. `test_tracker_support.py` checks report
boundaries independently of firmware and exercises both timeout descriptions and their run-report
entries through the simulated board.

The connect test captures both conditions, `network.broker.next_connection()` and
`reports.when_received()`, before power-on so observing the connection cannot lose its initial
report. `test_tracker_support.py` also powers on unprovisioned firmware and checks that board
timeouts name `the next MQTT client connection`, `a TLS handshake to start` and
`a TLS handshake to complete`. Periodicity brackets three later deadlines with a 250 ms
transport/scheduling tolerance and checks report history for early or extra publications.
Movement uses southern/western coordinates and a 15 m tolerance for the receiver's one-second
sampling at 10 m/s. Movement and separate lost-fix/silence scenarios wait for a positioned report,
without relying on the first GNSS epoch's order relative to TLS startup. The no-fix scenario
waits two seconds for a receiver epoch before observing a subsequent report. Two world epochs,
including one past 2038, check whole-second UTC against message arrival without a GNSS source,
with 1.25 seconds for truncation and NTP/transport latency. Wrong CA, hostname and validity-date
cases wait on `tls.handshake_started`, then advance the board by 30 seconds (covering the
MQTT client's default 10-second reconnect delay). The broker's cumulative connection and message
histories must remain empty, so even a temporary fallback fails. This proves absence within the
observation window, not indefinitely, and uses no firmware error text. The attempt count proves
input reached TLS, not successful certificate verification or a particular rejection reason.
Certificate-date regressions keep the CA valid from 2000 through 2099 while changing only the
server certificate: one expired in 2030, one starts in 2032, and a positive control is valid
from 2030 to 2032. At the 2031 world epoch only the control may connect and publish. The tests
use the public `server_not_before` / `server_not_after` constructor arguments, with no local
certificate builder or TLS subclass.
No-fix, lost-fix, silence and missing-NVS cases cover startup and stale data. A missing NTP
responder, selected with `network.ntp.go_silent()`, is observed for 30 seconds after the
firmware resolves `time.test`, with valid GNSS throughout. The test checks that every NTP request
in `network.ntp.exchanges` went unanswered, no broker DNS lookup,
zero TLS attempts and empty cumulative MQTT connection/message histories, without matching NTP
log wording. It then resumes NTP and waits up to 60 seconds for a positioned report, allowing
SNTP's retry delay. The recovered report must contain Warsaw's position and current world UTC
within the same 1.25-second tolerance; an NTP reply and TLS attempt must have occurred.
`test_retries_ntp_further_apart_and_resolves_its_name_again` keeps NTP silent for 160 seconds
from power-on. ESP-IDF v5.3's lwIP SNTP waits 15 s for an answer, then retries after an interval
of 15 s that doubles at each retry, so the four requests come at about 0, 30, 75 and 150 s: the
test compares the gaps with 30, 45 and 75 s within `JITTER` (0.25 s). With the DNS answer's TTL of
60 s, the firmware asks for `time.test` before the first, third and fourth request, within
`JITTER` before each, and not before the second. After `resume()` it waits up to 150 s for the next
answered request, the fifth at about 285 s, and then for a report, and `requests` is 1.
Transport assertions (TLS, QoS and retention) are separate from the immediate-report scenario.

These are functional TLS tests. `tests/test_determinism.py::test_tls_on_the_device` runs this
example, with the same NVS keys, through `make determinism`, which compares its raw modem tap,
console and network transcript exactly: the harness's TLS server draws its keys and nonces from
a seed.
