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

The tests use only public GNSS, board, SD and GPIO fixtures. Their local `card_image` fixture
checks for mtools and formats an empty 32 MiB image; it skips only tests requiring that image.
It does not insert the card: each scenario calls `sd_card.insert(card_image)` before power-on.
Local `write_file`, `read_file` and `list_files` helpers hide mtools arguments, temporary host
files and the `::/` drive prefix. Scenarios use ordinary filenames and compare exact CSV text;
these helpers belong to the example, not to the library's SD interface.
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
