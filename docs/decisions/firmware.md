# Firmware decisions

Why the validation firmware in `firmware/` and the ESP32 mask ROM ELF are built, fetched and
checked the way they are.

## Validation images are built in the pinned ESP-IDF container

Status: decided (#47)

`firmware/build.sh` builds each image in `espressif/idf:v5.3.6`, pinned by the digest stage 0
recorded (`sha256:cee52f0…`, a multi-arch index, so arm64 Macs run it natively). That is the
ESP-IDF stage 0 measured, not whatever a developer has installed. `IDF_LOCAL=1` builds with the
ESP-IDF of the calling shell instead, for iteration; it need not be v5.3.6, so goldens come from
the container build only. Every build directory records how it was built in `esp32tl-build.json`
(`idf_version`, `container`, `null` for a local build, and `project`). A build directory with a
different record is removed before building. ESP-IDF refuses some of those directories itself
(another project directory or Python, as a switch between a local and a container build brings),
but would reuse one from another version or container that changes neither, with the old build's
`sdkconfig`: the image would not be what a build from scratch makes.

## A local build passes with a warning, a missing or foreign one fails

Status: decided (#47)

`tests/test_firmware_images.py` fails with `run make firmware` when `firmware/hello_world/build`
holds no finished build, or one from a container other than the one `build.sh` pins now (read
from the script). A build with `"container": null` passes with a `UserWarning`.

**Rejected:** skipping the test, because a skip would go unnoticed in CI; and failing a local
build, because that is what `IDF_LOCAL=1` is for.

## Images land in `firmware/<image>/build/`

Status: decided (#47)

`IdfApp(app_path)` reads `app_path/build/`, so a test passes the image's directory as its app,
`firmware/hello_world`, and needs no build directory of its own. `make firmware` and `make test`
always pass an empty `FIRMWARE_OUT`, which means `firmware/`, whatever the calling shell exports.

## The example is built in place from ESP-IDF's examples directory

Status: decided (#47)

`hello_world` is built from `$IDF_PATH/examples/get-started/hello_world`, unmodified, with
`-DSDKCONFIG` in the build directory. A fixed source path keeps incremental builds working: 4.8 s
against 1:47 for a clean build. Neither the container's v5.3.6 (a root-owned tree, built as uid
501) nor a local v5.5.5 writes into the example directory.

**Rejected:** copying the example out of `$IDF_PATH` before building, as stage 0 did, because a new
source path each time breaks incremental builds.

## A separate marker file marks a finished build

Status: decided (#47)

`build.sh` removes `esp32tl-built` before `idf.py` runs and writes it after a successful build.
`make test` depends on `firmware/<image>/build/esp32tl-built`, so it builds the images that are
missing or unfinished and nothing else.

**Rejected:** `flasher_args.json` as the sign of a finished build, because CMake writes it while
configuring, so an interrupted or failed build would count as built. Also rejected: writing
`esp32tl-build.json` only on success, because a failed compile would then lose the incremental
build.

## pytest never builds firmware

Status: decided (#47)

`make test` and `make firmware` build the images; the test suite only reads them. CI builds them
once, in the `firmware` job
([ci.md](ci.md#builds-reach-the-test-jobs-through-the-actions-cache-not-artifacts)).
`build.sh` itself is tested against a fake `docker` and a fake `idf.py` that record their
arguments, in milliseconds and without Docker (`tests/test_firmware_build.py`).

**Rejected:** a `requires_docker` test that built `hello_world` from scratch, because it added two
to four minutes to every `make test` with Docker and would have built the firmware a second time
in the CI test jobs on Linux. What it checked beyond the build running is covered by the fakes and
by the provenance check on the real image.

## The firmware build supports Docker Desktop and rootful Docker Engine only

Status: decided (#47)

The container runs as the host's uid (`HOME=/tmp` for that uid, unknown to the image), with the
repository mounted read-only and the output directory as `/out`, so nothing is written outside the
output directory and the host's uid owns what is. Under rootless Docker or Podman the output belongs
to another uid, and SELinux in enforcing mode would need `:z` on the mounts.
`docs/features/validation-firmware-user.md` says so. An image that takes components from the
component manager writes `dependencies.lock` and `managed_components/` into its project directory,
which the read-only mount refuses; the comment at the mount in `build.sh` says so.

## The ROM ELF is pinned by its checksum, not by its release directory

Status: decided (#46)

`scripts/fetch-rom-elf` accepts `esp32_rev300_rom.elf` from any release under
`${IDF_TOOLS_PATH:-~/.espressif}/tools/esp-rom-elfs/*/` (`20241011` first) whose SHA-256 is the
pinned `920b7063…ca31`, and names each copy it skips on stderr. `docs/stage0-findings.md` recorded
the same hash for the copy in `20260528`, so a newer install with the same bytes counts. With no
match it downloads `esp-rom-elfs-20241011.tar.gz`.

**Rejected:** accepting only the `20241011` directory, because it would download a byte-identical
file next to an install that already has it.

## The ROM ELF is copied into .cache/esp-rom-elfs/20241011/

Status: decided (#46)

The script copies the file (857,500 bytes) to `.cache/esp-rom-elfs/20241011/esp32_rev300_rom.elf`,
from any working directory. The backend gets one path whether or not ESP-IDF is installed, and an
ESP-IDF upgrade that removes the old release cannot break it. The release directory stays in the
path, so `ESP32SIM_ROM_DIR=.cache/esp-rom-elfs/20241011` also works for runs by hand. `make clean`
keeps `.cache/`.

**Rejected:** a symlink, or using the file in place under the ESP-IDF tools path, because an
ESP-IDF upgrade can remove the release it points to, and a checkout without ESP-IDF would have no
path at all.

## The ROM ELF is checked on every make setup

Status: decided (#46)

`rom-elf` is a phony target, and the script hashes the file in place every time; that takes a few
milliseconds, and the script prints nothing when the file is good.

**Rejected:** a stamp file, because it would not notice a truncated or replaced ELF.

## The ROM checksums are pinned in the script

Status: decided (#46)

`fetch-rom-elf` pins both the ELF's SHA-256 and the tarball's (`921f0001…e9a9`, the value the
release's own `esp-rom-elfs-20241011-checksum.sha256` lists), and checks the tarball and the
extracted ELF against them.

**Rejected:** fetching the release's checksum file at run time, because that would only prove the
download matches whatever GitHub serves now.

## nmea_reader converts coordinates in integers

Status: decided (#15)

`nmea.c` reads the minutes as millionths of a minute and computes microdegrees with
`(minutes_e6 + 30) / 60`, rounded half up, and `main.c` prints them with integer formatting. The
printed value is then a function of the sentence's digits alone, which a test in Python works out
the same way (`tests/test_nmea_reader.py`), and the code can be checked by reading it.

**Rejected:** `float` and `printf("%.6f")`, because single precision holds about seven significant
digits and `52.229677` needs eight; and `double`, because the last digit would depend on
newlib's rounding of a binary value, which a test would have to reproduce rather than compute.

## nmea_reader prints one line per epoch

Status: decided (#15)

A fix whose UTC time field is the same as the last printed fix's prints nothing, so a receiver
that sends a `GGA` and an `RMC` every second prints one line a second, as plan M1's "one line per
later fix" reads. The talker is not checked: `$GPGGA` and `$GNGGA` both count.

**Rejected:** one line per sentence, because a real receiver would print two lines per fix; and
only the `$GP` talker, because receivers that combine constellations send `$GN`.

## The generated recordings send one sentence a second, GGA and RMC in turn

Status: decided (#15)

Plan M1 asks for one sentence per emulated second. Alternating the two sentence types gives one fix
per second, which is what `test_time_is_emulated` (ten fixes, #16) counts on, and feeds both
parsers of `nmea_reader` from the same file. 60 sentences, the first at 1 s, well after the
firmware configures UART1 (about 50 ms after power-on).

**Rejected:** a `GGA` and an `RMC` every second, because that is two sentences per second, which
plan M1 does not ask for; and `GGA` only, because the `RMC` parser would then run on no recording.

## Generated recordings carry no app, no tool versions and a fixed creation time

Status: decided (#15)

The header has `"source": "generated"`, `"app_sha256": null`, `"tools": {}`, `created` fixed in
the script, and a `generator` object (script, seed, baud) that plan §4.3 does not define. Running
the script again writes the same bytes, so `--check` and `tests/test_gnss_recordings.py` can
require the committed files to be what the script writes.

**Rejected:** the esp32tl version in `tools` and the time of the run in `created`, because either
would change the file without a change to its content, and the check would fail at every release.

**Postponed:** whether the recording reader of #14 accepts `"source": "generated"`, a `null`
`app_sha256` and the extra `generator` field, or the format gains a field for the baud; decided
with the reader: [it accepts all three, and no baud field](harness.md#the-recording-reader-takes-the-generated-recordings-header-as-it-is).

## The GNSS recording generator lives in scripts/ with a rule of its own

Status: decided (#15)

`scripts/generate-gnss-recordings.py` is owned by rule C12, which sends a change to it to the
validation firmware documents, where the recordings are described next to the firmware that reads
them.

**Rejected:** keeping it under `firmware/nmea_reader/`, which F11 would own without a new rule,
because the recordings are the `uart.gnss` interface's, not one firmware's, and every change to the
script would change the CI firmware cache key and rebuild the images; and inside the `esp32tl`
package, because no part of the package uses it.

## nmea_reader is tested through a bare socket peer until the gnss fixture exists

Status: superseded (#16)

`tests/test_nmea_reader.py` spoke the link protocol to the runner itself, with every input sent
in the first turn, so the firmware and the recordings were checked from the change that added
them. The M1 tests replaced it: see
[nmea_reader is tested through the gnss fixture](#nmea_reader-is-tested-through-the-gnss-fixture).

**Rejected:** leaving `nmea_reader` untested until #14, because a bug in the firmware would then
show up first as a failure of the harness.

## nmea_reader is tested through the gnss fixture

Status: decided (#16)

`tests/test_nmea_reader.py` holds the first milestone's tests, `test_reports_a_fix`,
`test_fix_follows_the_recording` and `test_time_is_emulated`, and keeps the sentences `nmea_reader`
must ignore as a recording the test writes, all replayed with `gnss.replay`. The firmware is now
tested the way a user's test would test it, and the replay's timing, one turn per sentence, is
the one the tests measure. The `uart_cfg` the bare peer checked is covered by the console's
`nmea_reader: UART1 RX GPIO5 TX GPIO4 9600 baud` and by a baud mismatch failing the test.

**Rejected:** keeping the bare peer beside the fixture's tests, because it sends every sentence in
the first turn, a timing no test of a user's firmware has, and would be a second peer to keep in
step with the protocol.

## gpio_echo reads RI and drives PWRKEY, on the LilyGO profile

Status: decided (#24)

`firmware/gpio_echo` reads one GPIO and drives another to its level, with the chip's pulls off on
the input, and its defaults are the `[lines.modem]` set of `lilygo-t-a7670g-r2`: RI on GPIO 33,
which the board pulls down, and PWRKEY on GPIO 4. Its tests run on that profile, the only one with
lines and the one whose pull the harness must honour. It sees a change through an interrupt on
either edge, and reads the level again in its task.

**Rejected:** a `[lines.*]` set in `esp32-devkitc`, which carries every other validation firmware,
because it would invent a wiring and a pull that a devkit does not have, and every devkit test
would watch lines it does not use; and polling the input, because the echo would then lag by up to
a FreeRTOS tick, 10 ms at ESP-IDF's default, where the interrupt takes microseconds.

## The SD card is validated on ESP-IDF's sdspi example, unmodified

Status: decided (#32)

`sdspi` is ESP-IDF's `examples/storage/sd_card/sdspi` built as it is, like `hello_world`, and the
`esp32-devkitc` profile's slot takes the example's default pins for the ESP32 (SCLK 14, MOSI 15,
MISO 2, CS 13), which `tests/test_firmware_images.py` compares with its `sdkconfig`. The example
mounts, writes, renames and reads, which covers the driver's initialisation, single and multiple
block reads and writes, CRC and DMA.

**Rejected:** a firmware of this repository's for the card, because the plan validates on public
firmware and the example already exercises what the card has to answer; and other pins in its
`sdkconfig.defaults`, because the example would then not be unmodified.

## Examples share the pinned builder and have their own assembly profiles

Status: decided (#71)

`firmware/build.sh --examples` discovers projects in `examples/` and uses the validation
builder's container, provenance and completion checks. CI builds and caches both sets together.
The GPS alarm reuses `nmea_reader`'s parser and has a `gps-alarm-clock` profile for its devkit,
GNSS and buzzer assembly.

**Rejected:** another container pin and build script for examples, which could drift from the
validation builds; repurposing the LilyGO modem's PWRKEY as a buzzer, which misrepresents the
wiring; and adding a buzzer to the generic devkit profile, which has no such peripheral.

## The SD logger uses each RMC's date and closes every appended row

Status: decided (#73)

The `sd_data_logger` example reuses the checksum/position parser and locally validates the
RMC date/time. It writes `YYYYMMDD.CSV` with one row per accepted RMC, closing the file after
each append. Its own `sd-data-logger` assembly profile adds a storage-error GPIO to the devkit's
GNSS/SD wiring. A mount or write failure latches that output high while the UART loop continues.

**Rejected:** logging both RMC and GGA, which duplicates the model's fixes and requires assigning
a date to a sentence with none; deriving filenames from the ESP32 clock, which need not know UTC;
and keeping a file open across fixes, which hides buffered rows from a test reading the card.
The example uses the SD interface's existing boot-time insertion contract; hot-plug and retry
behaviour remain outside its scope. It never formats an unmountable card.

## A card that comes and goes is validated on a firmware of this repository's

Status: decided (#106)

`sd_remount` (`firmware/sd_remount`) appends a line to a file every 100 ms, closing it each time,
unmounts the card on the first failure and mounts it again every 200 ms, on the `esp32-devkitc`
profile's slot. `tests/test_sd_remount.py` shows on it what a removal does to a firmware and that
a card put in is mounted again, the same image or another.

**Rejected:** making `sd_data_logger` retry, because its latched error output is its own decision
([above](#the-sd-logger-uses-each-rmcs-date-and-closes-every-appended-row)); and ESP-IDF's `sdspi`
example, which mounts once and stops.

## The position tracker reuses the pinned modem component and reports without a fix

Status: decided (#72)

The position tracker fetches `esp_modem` with the validation image's existing commit/checksum
helper into its own build directory. CMake disables component-manager resolution because
ESP-IDF and that download provide all its dependencies. The example reuses the LilyGO profile
and the checksum-checking NMEA parser. Its configuration is application-owned NVS data.

It publishes on MQTT connection and every 10 monotonic seconds, after NTP synchronization.
Without a recent valid fix it publishes null coordinates and `fix: false`, retaining the report's
UTC time. CA and hostname verification stay enabled. The example assumes a powered, registered
modem; modem power sequencing and PPP redial are left outside its scope.

**Rejected:** a separate floating registry dependency, which could drift from the validated
modem version; waiting indefinitely for GNSS before the connect report, which would hide a
receiver outage; and carrying old coordinates as a current fix after GNSS silence. The report
period is fixed in firmware, rather than adding configuration unrelated to the demonstration.

## Images of one container share their ccache hits

Status: decided (#146)

The image turns ccache on, and one container builds a whole family, yet each image compiled as long
as the first: its build directory is in every compile line and in the working directory ccache
hashes, so `gpio_echo` after `hello_world` hit 0 of 874 compilations. `CCACHE_BASEDIR=/out` and
`CCACHE_NOHASHDIR=1` on the `docker run` make that 870 of 874, measured on an aarch64 Mac with the
pinned image: 44 s against 165 s for `gpio_echo`, 28 s for `nvs_reader` after it. The flash image
is the same but for its build time and SHA-256: of the 168832 bytes of `gpio_echo.bin`, against a
build with no hits, only bytes 116 to 120 (the time in the app description), 177 to 208 (the ELF's
SHA-256) and the last 32 (the image's) differ, as they differ between two builds with no hits.

The cost is in the debug information: an object from the cache keeps the working directory of the
image that compiled it, so `gpio_echo.elf` also names `/out/hello_world/build`. The harness reads no
debug information.

**Rejected:** a cache kept between runs, on a volume or in the Actions cache, for now, because it
needs a directory on hp600g1 or a cache entry to manage, and hits within a container may be enough;
it measured 872 of 874 for a rebuild of one image in a new container, and can be added to this.
