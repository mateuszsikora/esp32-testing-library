# Runnable examples

Each directory under `examples/` is a small ESP-IDF project with readable tests in `tests/`.
The tests exercise the compiled firmware through esp32tl, using the same API as an application's
own tests. CI builds and runs every example.

## Build and run

From the repository root, with Python, Rust and Docker installed
([development setup](../guides/development.md)):

```sh
make setup
make examples
.venv/bin/pytest examples
```

`make examples` builds every `examples/<name>/CMakeLists.txt` project in the same pinned
ESP-IDF v5.3.6 container as `make firmware`, into `examples/<name>/build/`. To build and run only
the GPS alarm:

```sh
firmware/build.sh --examples gps_alarm_clock
.venv/bin/pytest examples/gps_alarm_clock/tests
```

`IDF_LOCAL=1 make examples` uses the shell's ESP-IDF for iteration. `FIRMWARE_OUT` overrides the
output root for direct `firmware/build.sh --examples` calls; `make examples` clears it so the
tests find their builds. The builder's provenance and completion markers work just like the
[validation firmware's](validation-firmware-user.md).

`make test` builds missing validation images and examples, then runs both suites. A direct
`.venv/bin/pytest` also collects both, but never builds firmware. Rebuild with `make examples`
after changing example C code; the completion marker only detects a missing or unfinished
build. After changing `sdkconfig.defaults`, remove that example's `build/` directory first so
the saved `sdkconfig` does not retain the old values.

## GPS alarm clock

Read [the firmware](../../examples/gps_alarm_clock/main/main.c) and
[its tests](../../examples/gps_alarm_clock/tests/test_alarm.py). This is a location alarm: it
sounds while a valid GNSS position is inside a configured radius. It has no wall-clock alarm time.

The `gps-alarm-clock` board profile describes this example's assembly:

| Connection | ESP32 side |
|---|---|
| GNSS UART | UART1, TX GPIO4, RX GPIO5, 9600 baud, 8N1 |
| Active buzzer control | GPIO18, high means on |
| Console | UART0, the default ESP-IDF console |

On real hardware use a suitable buzzer driver; the example supplies a steady logic level, not
an audio waveform. The test observes GPIO18 through `buzzer = lines.output("alarm.buzzer")`, with
`buzzer.is_on` and `buzzer.is_off` conditions for `board.wait_until`.

Before `board.power_on()`, write the destination to the default NVS partition with `board.nvs`:

| Namespace | Key | NVS type | Meaning |
|---|---|---|---|
| `alarm` | `lat_e6` | `i32` | Latitude in microdegrees, from -90000000 to 90000000 |
| `alarm` | `lon_e6` | `i32` | Longitude in microdegrees, from -180000000 to 180000000 |
| `alarm` | `radius_m` | `u32` | Positive radius in metres |

The example's `configure_alarm(board, target=TARGET, radius=m(100))` helper accepts a
`Position` and a `Distance`, converts coordinates to the nearest microdegree and requires a
positive whole-metre radius. It writes `i32(52229677)`, `i32(21012233)` and `u32(100)` for a
100 m radius around Warsaw. The helper belongs to this application; the library's `board.nvs`
continues to accept typed NVS values. Each configured test calls the helper explicitly and uses
`board.power_on()`; there is no second board object named `alarm`. There is no default destination. Missing, mistyped or invalid values
leave the buzzer off and print `gps_alarm_clock: invalid or missing NVS alarm configuration`.
A configured image prints `gps_alarm_clock: ready`.

A valid GGA or RMC sentence turns the buzzer on when the great-circle distance is at most the
radius, and off outside it. A GGA/RMC without a fix or with an invalid checksum/position turns
it off. Other sentence types are ignored. With no valid fix for 2.5 s it turns off too; the
UART loop checks this at least every 100 ms. This prevents a silent receiver leaving the alarm on.

The movement tests calculate `time_to_boundary` from distance divided by speed. They check
the old state one fix interval before the crossing, advance to the boundary, then wait for the
new state with `REACTION_TIME = 2 * FIX_INTERVAL` (`FIX_INTERVAL = seconds(1)`). The response
budget starts at the boundary, independent of travel time. `assert_never_buzzed` checks both
the current off state and the output's event history, so short pulses cannot pass unnoticed
from boot, while allowing the initial unconfigured pin. Once a state is established, tests use
`buzzer.assert_stayed_on(since=...)` or `buzzer.assert_stayed_off(since=...)` to require that
state throughout an interval. In particular, loss of fix must keep the buzzer off for three
seconds after it first turns off. `assert_on()` / `assert_off()` check only the current state.
The scenarios come first in the file; the NVS helper and fixture follow them. Separate smaller-
and larger-radius cases show the expected output directly, without conditional assertions.

The tests demonstrate:

- A far-away fix leaves the buzzer off throughout the observation window.
- Moving inward at 10 m/s turns it on within two seconds of crossing the radius; moving out
  turns it off within two seconds of crossing.
- No fix at boot leaves it off; losing a fix or going silent turns an active buzzer off.
- NVS controls both the destination and radius, including southern/western coordinates.
- Missing configuration leaves it off even when the receiver has a fix at the example target.

## Adding an example

Add `examples/<name>/CMakeLists.txt`, `main/`, `sdkconfig.defaults` and `tests/test_*.py`.
Use `app_path("..")` in tests directly under `tests/`, with the board profile that describes
the assembly. Keep assertions on the public fixtures and observed wires. The builder discovers
projects, pytest collects their tests, and CI caches their builds automatically; no image list
needs updating. Update these documents in the same change (ownership rule F24).
