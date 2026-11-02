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
The boot-window helper allows an initially unconfigured pin but rejects any active pulse and
requires the final state to be off; strict interval assertions require a known starting state.
The separate `gps-alarm-clock` profile gives this assembly's wiring without changing the generic devkit.

`scripts/check-docs.py` owns `examples/` through F24; Python test files under each example's
`tests/` are exempt just like the root suite. `tests/test_check_docs.py` checks both behaviours.
`tests/test_firmware_build.py` exercises example discovery, refusal of unknown names, container
forwarding and failed-build completion markers against fake tools.
