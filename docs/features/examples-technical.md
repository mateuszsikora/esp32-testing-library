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
NVS keys before boot; the `buzzer` fixture returns an `OutputLine`, not a renamed board. Tests
move the model on emulated time and pass its `is_on`/`is_off` conditions to `board.wait_until`.
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
GPIO14/15/2/13 and `format_if_mount_failed = false`. GPIO18 starts high, becomes low only when
the mount succeeds, and latches high on any `fopen`, `fprintf` or `fclose` failure. The UART
driver is installed before mounting so it can buffer incoming sentences during card discovery.
The application continues its UART loop after storage failure and reports accepted fixes on
the console. It does not retry mounting or writing after failure.

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
the example promises neither atomic rows under power loss nor recovery from a damaged FAT.

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

`tests/test_tracker.py` keeps power-on and every wait explicit. Its `broker_ca` fixture creates
credentials; `tls_listener` registers TLS and returns its `Tls` handle. Tests request that
fixture explicitly, using `usefixtures` when only setup is needed. The independent `reports`
fixture wraps `broker` in the application-owned `Reports` adapter without registering a listener,
including in the positive certificate-date test with custom credentials. `configure_tracker` in
`tests/tracker_support.py` only translates the application's settings into NVS entries.
It does not create a CA, register a listener or start the firmware.

`Report.decode` validates the exact JSON fields and their types: a boolean fix, integer Unix
seconds, numeric coordinates within the position bounds for a fix, and null coordinates without
one. It returns an aware UTC datetime, a `Position` or `None`, and the message's `Duration`
timestamp. Invalid data raises `ValueError` with the received time.
`Reports.when_received(with_position=False)` captures the broker history length when constructed
and returns a condition observing only subsequent firmware publications on the exact topic.
Neither construction nor polling consumes messages or advances time. Position waits skip valid
no-fix reports, but reject malformed ones;
later invalid messages cannot mask an earlier match. The history view validates every matching
publication and filters out unrelated topics and the test publisher. `test_tracker_support.py`
checks these boundaries independently of firmware.

The connect test captures both conditions before power-on so observing the connection cannot
lose its initial report. Periodicity brackets three later deadlines with a 250 ms
transport/scheduling tolerance and checks report history for early or extra publications.
Movement uses southern/western coordinates and a 15 m tolerance for the receiver's one-second
sampling at 10 m/s. Movement and separate lost-fix/silence scenarios wait for a positioned report,
without relying on the first GNSS epoch's order relative to TLS startup. The no-fix scenario
waits two seconds for a receiver epoch before observing a subsequent report. Two world epochs,
including one past 2038, check whole-second UTC against message arrival without a GNSS source,
with 1.25 seconds for truncation and NTP/transport latency. Wrong CA, hostname and validity-date
cases wait for `Tls.handshakes_started > 0`, then advance the board by 30 seconds (covering the
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
responder, selected with `modem.network().ntp.go_silent()`, keeps the firmware from resolving or
connecting to the broker even with valid GNSS. Transport assertions (TLS, QoS and retention) are
separate from the immediate-report scenario.

These are functional TLS tests. The existing determinism checker still compares raw UART and
board-link statistics exactly; this example does not feed random TLS ciphertext to that check.
