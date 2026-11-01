# Validation firmware: how it works

`firmware/build.sh` builds the validation images; the `Makefile` targets `firmware` and
`firmware/%/build/esp32tl-built` call it. `tests/test_firmware_build.py` tests the script against
fakes, `tests/test_firmware_images.py` checks the images it built. `firmware/nmea_reader/` and
`firmware/gpio_echo/` are the images whose source lives here ([nmea_reader](#nmea_reader),
[gpio_echo](#gpio_echo)), and
`scripts/generate-gnss-recordings.py` writes the recordings it is tested with
([The GNSS recording generator](#the-gnss-recording-generator)).

## Data flow

`firmware/build.sh [IMAGE...]` runs twice for a container build: once on the host, which starts
the container, and once inside it, which builds. With `IDF_LOCAL=1` it runs once, on the host.

1. **Image names.** Each argument must be in `IMAGES`, or the script dies before anything is
   written. With no arguments, the list is all of `IMAGES`.
2. **Mode**, decided before anything is written:
   - `ESP32TL_CONTAINER` set: the container side. `/.dockerenv`, which Docker creates in every
     container, must exist; without it the variable was left set on the host, and a build that
     took it for the container's would record a container it never ran in. `container` is the
     variable's value as a JSON string.
   - else `IDF_LOCAL=1`: a local build. `idf.py` must be on `PATH` and `IDF_PATH` set.
     `container` is `null`.
   - else the host side of a container build: `docker` must be on `PATH` and `docker info` must
     succeed.
3. **Output directory.** `FIRMWARE_OUT`, or the script's own directory (`firmware/`) when it is
   unset or empty, resolved before the script changes directory (so a relative `FIRMWARE_OUT` is
   the caller's), created, and made absolute with `pwd -P`. The script then changes to the
   repository root.
4. **Host side** (container build only): `exec docker run` with the image names, after the
   default above, as arguments:

   ```sh
   docker run --rm --init \
     --user "$(id -u):$(id -g)" \
     --env HOME=/tmp \
     --env ESP32TL_CONTAINER="$CONTAINER" \
     --env FIRMWARE_OUT=/out \
     --env ESP32TL_HOST_OUT="$out" \
     --volume "$PWD:/project:ro" \
     --volume "$out:/out" \
     --workdir /project \
     "$CONTAINER" firmware/build.sh "$@"
   ```

   - `--volume "$PWD:/project:ro"`: the repository read-only, so the build cannot write into it
     except through `/out`. `hello_world` writes nothing into its project directory. A project
     under `firmware/` that takes components from the component manager writes
     `dependencies.lock` and `managed_components/` into its own directory, and needs that
     directory made writable first.
   - `--volume "$out:/out"` with `FIRMWARE_OUT=/out`: the output directory, the only writable
     mount.
   - `--user "$(id -u):$(id -g)"`: the host's uid owns what the build writes. Under rootless
     Docker or Podman that uid maps to another one on the host, which is why they are not
     supported. Neither mount carries an SELinux label option, so in enforcing mode the container
     cannot read the repository.
   - `--env HOME=/tmp`: the host's uid is unknown to the image; this gives it a writable home.
   - `--env ESP32TL_CONTAINER`: the pinned image reference, which the container side records.
   - `--env ESP32TL_HOST_OUT`: the host's path of `/out`, for the messages.
   - `--rm`: the container is removed when it exits. `--init`: Docker runs its init process as
     PID 1, which forwards signals to the build and reaps its children. TODO(owner): build.sh
     does not say why it passes `--init`; confirm that signal forwarding is the reason.
5. **Build side** (in the container, or the local build):
   - `idf.py --version` gives the ESP-IDF version, the text after `ESP-IDF `. A local build says
     on stderr that it is not for goldens.
   - For each image, in the order of `IMAGES`: the project is `project_of IMAGE`
     (`hello_world` is `$IDF_PATH/examples/get-started/hello_world`, `sdspi` is
     `$IDF_PATH/examples/storage/sd_card/sdspi`, `nmea_reader` is `$PWD/firmware/nmea_reader` and
     `gpio_echo` `$PWD/firmware/gpio_echo`, the working directory being the repository's root: in
     the container `/project/firmware/nmea_reader`, in a local build the checkout's own path), the
     build directory `$out/IMAGE/build`. If
     that directory exists and its `esp32tl-build.json` is missing or, compared as text, is not
     the record this build would write (another `idf_version`, `container` or `project`), the
     script prints `was built another way` and removes the directory with `rm -rf`. ESP-IDF
     refuses only some of those directories itself. In v5.3.6, `idf.py` refuses one whose CMake
     cache names another project directory or another Python (`_ensure_build_directory` in
     `tools/idf_py_actions/tools.py`: `Build directory '...' configured for project '...' not
     '...'. Run 'idf.py fullclean' to start again.`), and `tools/cmake/tool_version_check.cmake`
     a cached compiler the new version does not support. A switch between a local and a
     container build changes the project directory and the Python. Another version or container
     that changes none of the three, a local ESP-IDF updated in place to a release with the same
     toolchain say, would be reused with the `sdkconfig` the old build wrote, and the image would
     not be the one a build from scratch makes. Then `esp32tl-built` is removed,
     `esp32tl-build.json` written, and

     ```sh
     idf.py -C "$project" -B "$build" -DSDKCONFIG="$build/sdkconfig" -DIDF_TARGET=esp32 build
     ```

     runs. `sdkconfig` goes in the build directory because the project directory may be
     ESP-IDF's own examples, or read-only. After a successful `idf.py`, the script checks that
     `flasher_args.json` exists, creates `esp32tl-built`, and prints the `built in` line.

The record is written before `idf.py` runs, so a failed compile keeps the build directory and the
next build is still incremental; the marker, removed first, is what says it finished.

The Makefile's `firmware` target runs `FIRMWARE_OUT= firmware/build.sh`; the pattern rule
`firmware/%/build/esp32tl-built` runs `FIRMWARE_OUT= firmware/build.sh $*`, and `test` depends on
`$(FIRMWARE_IMAGES:%=firmware/%/build/esp32tl-built)` with `FIRMWARE_IMAGES := hello_world
nmea_reader gpio_echo sdspi modem_at`. The rule has no prerequisites, so make rebuilds an image
only when its marker is missing: an edit to `firmware/nmea_reader/` needs `make firmware`, or
`firmware/build.sh nmea_reader`, before `make test` sees it.

`nmea_reader`, `gpio_echo` and `modem_at` are built from the read-only `/project` mount like any
other project. They take no components from the component manager, and with `sdkconfig` and the
build in the build directory ESP-IDF v5.3.6 writes nothing into their directories.

## `nmea_reader`

An ESP-IDF project of three files in `firmware/nmea_reader/main/`, about 200 lines in all:
`main.c` (the UART and the console), `nmea.c` and `nmea.h` (the parser, with no ESP-IDF
dependency). `Kconfig.projbuild` declares the four options of the user document; the component
needs `esp_driver_uart` only.

**Start.** `app_main` installs the UART driver on `CONFIG_NMEA_READER_UART_PORT` with a 1024-byte
receive buffer (larger than the 128-byte hardware FIFO, as the driver requires) and no transmit
buffer, configures 8N1 at `CONFIG_NMEA_READER_UART_BAUD` with no flow control, sets the TX and
RX pins, and prints `nmea_reader: UART%d RX GPIO%d TX GPIO%d %d baud`. A failure of any of the
three calls is `ESP_ERROR_CHECK`'s abort.

**Reading.** One byte at a time, `uart_read_bytes(..., 1, portMAX_DELAY)`, into a sentence
buffer:

- `$` starts a sentence, dropping any partial one;
- `\r` or `\n` ends it, and it goes to the parser; a line ending outside a sentence is ignored;
- any other byte is appended inside a sentence; one that would make the sentence longer than 80
  characters (`NMEA_SENTENCE_MAX`, 82, less the line ending) drops the sentence;
- bytes outside a sentence are ignored.

**Parsing**, `nmea_parse_fix(sentence, &fix)`, true only when all of these hold:

1. at most 80 characters; `$`, a body, `*` and two hex digits (either case) that equal the XOR of
   the body's bytes, and nothing after them;
2. field 0, the address, is five characters ending in `GGA` or `RMC`; the talker is not checked;
3. `GGA`: field 6, the fix quality, is one character other than `0`. `RMC`: field 2, the status, is
   `A`;
4. field 1, the UTC time, is 6 to 15 characters whose first six are digits;
5. the position, fields 2 to 5 of `GGA` and 3 to 6 of `RMC`: latitude `ddmm[.m...]` and `N` or
   `S`, longitude `dddmm[.m...]` and `E` or `W`, with exactly two or three degree digits and two
   minute digits before the point, at most six decimals, minutes under 60, at most 90° or 180°
   (and no minutes beyond it).

A coordinate is converted in integers: minutes in millionths, `e6 = degrees × 10⁶ + (minutes_e6 +
30) / 60`, microdegrees rounded half up, negated for `S` and `W`. Fields end at `,` or `*`; a field
too long for its buffer fails the sentence.

**Printing.** A fix whose time is the same string as the last printed fix's is dropped: a `GGA`
and an `RMC` of one epoch print one line. Otherwise `main.c` prints `fix acquired` the first time
and `fix` after, with ` lat=` and ` lon=` and each value as `[-]D.DDDDDD`, then flushes stdout.
`nmea_reader` keeps running; a lost fix prints nothing, and the next fix prints `fix`.

## `gpio_echo`

One file, `firmware/gpio_echo/main/main.c`, with the component's `CMakeLists.txt`
(`PRIV_REQUIRES esp_driver_gpio`) and `Kconfig.projbuild` beside it. `app_main` creates a queue of
16 one-byte items, configures `CONFIG_GPIO_ECHO_OUT_PIN` as an output and
`CONFIG_GPIO_ECHO_IN_PIN` as an input, both with `pull_up_en` and `pull_down_en` disabled, the
input with `GPIO_INTR_ANYEDGE`; installs the GPIO ISR service and adds `on_edge` for the input,
which puts a byte on the queue from the interrupt; prints the `gpio_echo:` line; then reads the
input, which it does after the interrupt is on, so that a change from then on is seen by this read
or by the next. `echo(level)` sets the output with `gpio_set_level` and then prints `in=<level>`.
The loop blocks on the queue, reads the input again, and calls `echo` only when the level differs
from the last one it printed.

`gpio_config` on GPIO 33, an RTC-capable pad, hands it from the RTC mux back to the digital GPIO
matrix (`rtc_gpio_deinit`, `components/esp_driver_gpio/src/gpio.c:372` in ESP-IDF v5.3.6), so its
interrupt reaches the CPU in esp32sim, where a level on an RTC pad raises no GPIO interrupt
(`set_input`, `esp32/src/periph.rs:373`). Its pulls disabled, the input reads whatever level the
harness drives on it: the board's pull from the profile, or the test's.

## Modem AT validation

`firmware/modem_at/` is an ESP-IDF project with `esp_driver_uart` and `esp_driver_gpio`.
`project_of` maps `modem_at` to `$PWD/firmware/modem_at`; its build produces `modem_at.bin` and
`modem_at.elf`. `make test` includes its completion marker among `FIRMWARE_IMAGES`.
The CI firmware tarball already includes every `firmware/*/build`, so it includes this image.

The firmware uses fixed LilyGO UART and GPIO pins. Its blocking reader accumulates up to 255
bytes until `OK`, `ERROR`, `CONNECT` or `NO CARRIER` framed by CRLF, or a one-second read
timeout. PWRKEY pulses use the nominal 100/3000 ms FreeRTOS delays without an extra tick,
matching LilyGO. The actual pulse can be almost one tick shorter. A 20 ms wait after release is
used before reading RI. After an off pulse, an additional 2000 ms wait keeps the interval before
the on pulse above SIMCom's minimum 2 s `Toff-on`, including tick truncation. The model does not
enforce this interval or the typical 8 s `Ton(uart)` startup time; the firmware starts its AT
dialogue without that UART startup wait. It sends no PPP data. `tests/test_modem_fixture.py`
checks its dialogue, GPIO power cycle, changes made on the test clock and identical taps from two
runs. The command subset is the one this firmware sends; new firmware commands should extend that
validation and the model's documented answers together.

## The GNSS recording generator

`scripts/generate-gnss-recordings.py` holds the recordings in `RECORDINGS`, each a name, a seed
and a start position in 1/10000 minutes of arc (NMEA's own resolution, so the position needs no
floating point). For each:

1. `random.Random(seed)`; the position starts at the start position;
2. 60 sentences, `i` from 0: from the second one on, latitude and longitude each move by
   `randint(-20, 20)` (`STEP`, under 4 m); sentence `i` is at `t = 1 s + i × 1 s`, UTC
   `2026-10-29 12:00:00` plus `i` seconds, a `GGA` for even `i` and an `RMC` for odd `i`;
3. the line is the header, then one event per sentence; the file ends in a newline.

The sentences, `CS` being the checksum and `ddmmyy` the date:

```text
$GPGGA,hhmmss.00,ddmm.mmmm,N,dddmm.mmmm,E,1,08,0.9,110.0,M,34.0,M,,*CS\r\n
$GPRMC,hhmmss.00,A,ddmm.mmmm,N,dddmm.mmmm,E,0.0,,ddmmyy,,,A*CS\r\n
```

An assertion in the script holds every sentence under half a period on the wire at `BAUD`. Only
`random.Random(int).randint` decides the walk, which gives the same numbers on every Python from
3.9 on, and nothing in the output depends on the clock or the machine, so a run writes the same
bytes everywhere. `--check` compares each file as text with what it would write.

**Header**, one line of JSON, fields in this order:

```json
{"esp32tl_recording": 1, "board": "esp32-devkitc", "interface": "uart.gnss", "source": "generated", "clock": "emulated-fifo-write", "end": "hold", "app_sha256": null, "tools": {}, "created": "2026-10-31T00:00:00Z", "generator": {"script": "scripts/generate-gnss-recordings.py", "seed": 1, "baud": 9600}}
```

| Field | Value | Why |
|---|---|---|
| `esp32tl_recording`, `board`, `interface`, `clock`, `end` | as the [recording format](../recording-format.md#header) has them | `t` is emulated nanoseconds since power-on; `hold`: nothing after the last event |
| `source` | `generated` | no model or device produced it |
| `app_sha256` | `null` | the recording belongs to no app; no `app.bin` produced it |
| `tools` | `{}` | neither esp32tl nor esp32sim produced it, and a version here would change the file at every release |
| `created` | `CREATED`, `2026-10-31T00:00:00Z` | when the parameters were fixed, so the output depends on the script alone |
| `generator` | the script's path, the seed, `BAUD` | not a field of the format, which keeps it as an extra: how to make the file again, and the baud its timing assumes, which the format has no field for |

**Events:** `{"t": T, "dir": "in", "data": "BASE64"}`, one sentence each, `t` in emulated
nanoseconds.

| Constant | Value | Effect |
|---|---|---|
| `BAUD` | `9600` | the line rate the timing is checked against, and `generator.baud` |
| `FIRST_NS` | `1_000_000_000` | the first sentence, well after `nmea_reader` sets up its UART (about 50 ms) |
| `PERIOD_NS` | `1_000_000_000` | one sentence per second |
| `SENTENCES` | `60` | sentences per recording |
| `START` | `2026-10-29 12:00:00` UTC | the time and date in the sentences |
| `STEP` | `20` | the largest move per second, in 1/10000 minutes |

## Schema

**`esp32tl-build.json`**, in each build directory: one line of JSON and a newline, written with
`printf`, fields in this order:

```json
{"idf_version": "v5.3.6", "container": "espressif/idf:v5.3.6@sha256:cee52f0c4fdaf088992a7a739f5993154e95cf1d3ab61c24f5b40e6d56a3a070", "project": "<IDF_PATH>/examples/get-started/hello_world"}
```

| Field | Type | Value |
|---|---|---|
| `idf_version` | string | what `idf.py --version` printed after `ESP-IDF ` |
| `container` | string or `null` | the image reference `ESP32TL_CONTAINER` named, which the host side sets to `CONTAINER`; `null` for an `IDF_LOCAL=1` build |
| `project` | string | the absolute path of the ESP-IDF project, as seen where the build ran (inside the container, under the image's `IDF_PATH`) |

`idf_version` and `project` are escaped for `\` and `"` only; `container` is inserted as it is.
`build.sh` compares the file as text, so any change to the format counts as "built another way"
and rebuilds from scratch. `tests/test_firmware_images.py` reads it as JSON, `container` and
`idf_version` only.

**`esp32tl-built`**: an empty file, present only after a build finished: removed before `idf.py`
runs, created after it succeeded and wrote `flasher_args.json`. `flasher_args.json` cannot be the
marker, because CMake writes it while configuring, before anything is compiled: a failed or
interrupted build would count as built. Read by the Makefile pattern rule,
`tests/test_firmware_images.py` and `tests/test_pytest_plugin.py`.

Everything else in the build directory is ESP-IDF's (`flasher_args.json`, the ELF, the binaries,
`config/sdkconfig.json`, `project_description.json`). The CI `firmware` job packs the files at the
top of each build directory, `config/sdkconfig.json`, and the `.bin` and `.elf` files of
`bootloader/` and `partition_table/` ([CI](../cicd.md)).

## Interfaces

- `firmware/build.sh [IMAGE...]`: exits 0 when every image built, 1 with a message (below), or
  with the exit code of `docker run` or `idf.py` when those fail. Messages go to stderr, except
  the `was built another way` and `built in` lines, which go to stdout.
- `make firmware`, `make test`: [development guide](../guides/development.md).
- `python3 scripts/generate-gnss-recordings.py [--check]`: exits 0, or 1 with `--check` when a
  recording differs from what the script writes or is missing; the messages are in the
  [user document](validation-firmware-user.md#the-gnss-recordings).
- `nmea_reader`: UART1 in, the console out, as the [user
  document](validation-firmware-user.md#nmea_reader) says.
- `gpio_echo`: one GPIO in, one out, the console, as the [user
  document](validation-firmware-user.md#gpio_echo) says.

## Configuration

| Name | Kind | Default | Effect |
|---|---|---|---|
| `IDF_VERSION` | script constant | `v5.3.6` | the ESP-IDF tag of the container |
| `CONTAINER` | script constant | `espressif/idf:$IDF_VERSION@sha256:cee52f0c4fdaf088992a7a739f5993154e95cf1d3ab61c24f5b40e6d56a3a070` | the image the build runs in, pinned by digest (a multi-arch index) |
| `IMAGES` | script constant | `hello_world nmea_reader gpio_echo sdspi modem_at` | the images, in build order; `project_of` maps each to its project |
| `FIRMWARE_OUT` | environment | `firmware/` | where `IMAGE/build/` goes; empty means the default; `/out` inside the container |
| `IDF_LOCAL` | environment | unset | `1` builds with the ESP-IDF of the calling shell |
| `ESP32TL_CONTAINER` | environment, set by the host side | unset | marks the container side and names the image it records |
| `ESP32TL_HOST_OUT` | environment, set by the host side | unset | the host's path of `/out`, shown in messages |
| `IDF_PATH` | environment | from `export.sh`, or the image | the ESP-IDF tree the projects come from |

`tests/test_firmware_images.py` reads `IDF_VERSION=` and `CONTAINER=` from `build.sh` with
regular expressions, so each has to stay on a line of its own. Changing either, or anything under
`firmware/`, changes the CI cache key of the firmware job.

## Failure modes

`build.sh` dies with `firmware/build.sh: MESSAGE` on stderr and exit 1:

| Message | Cause |
|---|---|
| `unknown image 'NAME'; the images are: hello_world nmea_reader gpio_echo sdspi modem_at` | an argument not in `IMAGES`; nothing written |
| `ESP32TL_CONTAINER is set outside a container; unset it` | `ESP32TL_CONTAINER` set and no `/.dockerenv`; nothing written |
| `IDF_LOCAL=1 needs ESP-IDF in the environment: source $IDF_PATH/export.sh first` | `IDF_LOCAL=1` without `idf.py` on `PATH` or without `IDF_PATH`; nothing written |
| `needs Docker, or IDF_LOCAL=1 to build with a local ESP-IDF` | no `docker` on `PATH`; nothing written |
| `the Docker daemon is not reachable ('docker info' failed); is Docker running?` | `docker info` failed; nothing written |
| `'idf.py --version' printed no ESP-IDF version` | no line starting `ESP-IDF ` in its output |
| `IMAGE: the build wrote no flasher_args.json` | `idf.py` succeeded but left no `flasher_args.json`; no marker |

A failing `idf.py` ends the script with its exit code (`set -e`), its own output, no marker and the
record kept. A failing `docker run` (a pull that fails, a mount refused) ends it with Docker's exit
code.

`tests/test_firmware_images.py` fails the test with:

- `DIR holds no finished build: run make firmware` when `esp32tl-built` is missing;
- `IMAGE was built in CONTAINER (ESP-IDF VERSION), not in PINNED (PINNED_VERSION): run make firmware`
  when the record names another container or version;
- `firmware/build.sh no longer sets IDF_VERSION= and CONTAINER= on lines of their own` when it
  cannot read the pin.

A local build passes with the `UserWarning` `IMAGE was built with a local ESP-IDF VERSION, not in
CONTAINER`.

## Tests

`tests/test_firmware_build.py` runs `build.sh` against a fake `docker` and a fake `idf.py` that
write their arguments to `$FAKE_LOG`, with a `PATH` holding only them and `cat`, `dirname`, `id`,
`mkdir`, `rm` and `sed`. It needs neither Docker nor ESP-IDF and takes milliseconds:

- the four early refusals (unknown image, no Docker, unreachable daemon, `IDF_LOCAL=1` without
  ESP-IDF) and `ESP32TL_CONTAINER` on the host (skipped when the suite itself runs in Docker):
  each message verbatim, exit 1, and no output directory created;
- `test_build_starts_the_pinned_container`: the `docker run` arguments, exactly;
- `test_local_build_runs_idf_py_and_records_how`: with no image named, every image in order,
  `hello_world` and `sdspi` from the fake `IDF_PATH`, `nmea_reader`, `gpio_echo` and `modem_at` from
  `firmware/`; the `idf.py` arguments of each, the records with `"container": null`, the markers,
  the "not for goldens" warning and the `built in` lines;
- reuse of a build directory with the same record, and removal of one with another record;
- no marker when `flasher_args.json` is missing, and a failed build removing the marker of an
  earlier one (exit code 2, the fake's).

The same file tests the `requires_docker` marker of `tests/conftest.py`, which skips a marked
test with `requires_docker: no docker on PATH`, `requires_docker: the Docker daemon is not
reachable ('docker info' failed)` or `requires_docker: 'docker info' did not answer in 30 s`, and
runs it when `docker info` succeeds. No test in `tests/` carries the marker at present.

`tests/test_firmware_images.py::test_hello_world_has_the_classic_esp32_flash_layout` reads the
real `hello_world` build through `IdfApp`, after the provenance check above: target `esp32`,
flash files `0x1000` `bootloader/bootloader.bin`, `0x8000` `partition_table/partition-table.bin`
and `0x10000` `hello_world.bin`, all present, and the ELF `hello_world.elf`.
`test_nmea_reader_has_the_classic_esp32_flash_layout` checks the same of `nmea_reader`, and
`test_nmea_reader_takes_the_devkit_gnss_route_from_sdkconfig` its four `NMEA_READER_UART_*`
values in `config/sdkconfig.json`. `test_gpio_echo_has_the_classic_esp32_flash_layout` and
`test_gpio_echo_takes_the_lilygo_modem_lines_from_sdkconfig` do the same for `gpio_echo`, with
`GPIO_ECHO_IN_PIN` 33 and `GPIO_ECHO_OUT_PIN` 4; `test_sdspi_has_the_classic_esp32_flash_layout`
checks the same of `sdspi`, with `sd_card.bin` and `sd_card.elf`, and
`test_sdspi_takes_the_devkit_sd_card_slot_from_sdkconfig` that its four `EXAMPLE_PIN_*` values are
the `esp32-devkitc` profile's slot and that both of its formatting options are off.

`tests/test_gpio_echo.py` runs the real `gpio_echo` in the real runner on the
`lilygo-t-a7670g-r2` profile, all but one through the `lines` fixture:
`test_ri_reads_the_board_s_pull_down_from_power_on` (`in=0` with no level driven, which reads `in=1`
without the profile's pull, and PWRKEY driven low),
`test_the_pull_holds_in_a_test_that_does_not_use_lines` (`in=0` with only `board` and `console`),
`test_the_firmware_follows_ri_and_drives_pwrkey` (RI driven high, then released to its pull-down,
PWRKEY following each within `ECHO_SECONDS`, a millisecond of the board's time, the five events in
order and no warning on the link) and `test_a_level_driven_before_power_on_holds_from_the_start`
(`in=1` first, and the drive's event at 0 s).

`tests/test_sdspi.py` runs the real `sdspi` in the real runner with the runner's SD card in the
slot, through the `sd_card` fixture, and needs mtools (`requires_mtools`): on a 32 MiB FAT image
`mformat` made, holding a `foo.txt` of the test's, the console prints the card's name, type, size
and bus width, both files read back, and `Card unmounted`, and the image then holds `FOO.TXT` with
`Hello SIMSD!` and `NIHAO.TXT` with `Nihao SIMSD!` and nothing else; a 1 MiB image of zeros ends
in `Failed to mount filesystem.` and is not written; an empty slot ends in
`Failed to initialize the card (ESP_ERR_TIMEOUT)`. The 512-byte blocks the example reads and
writes go through SPI DMA: the W registers hold 64 bytes, so the files come back whole only
because esp32sim models the classic SPI DMA path the example's `SDSPI_DEFAULT_DMA` takes.

`tests/test_nmea_reader.py` is the first milestone's test (plan M1): the real `nmea_reader` in
the real runner, on the `esp32-devkitc` profile, fed through the `gnss` fixture's
`gnss.replay(path)` ([replay](replay-user.md)). Where it expects fix lines, it works them out from
the recording's sentences itself (its own conversion, not the firmware's), and reads each as the
next fix line on the console, so none can come in between:

- `test_reports_a_fix`: the specification's example, `gnss-generated-1.jsonl` printing
  `fix acquired lat=52.229677 lon=21.012233`;
- `test_fix_follows_the_recording`: `gnss-generated-2.jsonl` prints its own first five fixes, the
  first `fix acquired lat=-34.603700 lon=-58.381600`, after the line
  `nmea_reader: UART1 RX GPIO5 TX GPIO4 9600 baud`, with no warning on the link;
- `test_time_is_emulated`: the first ten fixes of `gnss-generated-1.jsonl`, ten emulated seconds,
  arrive in under five wall seconds from the replay (`TEN_FIXES_WALL_SECONDS`); the margin is
  deliberate, since each sentence costs an exchange with the harness in wall time (plan §7);
- `test_only_well_formed_fixes_count_and_an_epoch_prints_once`, from a recording the test writes:
  a wrong checksum, `GGA` quality 0, `RMC` status `V`, an empty position, a `GSV`, an `RMC` with
  the time of the `GGA` before it, and a sentence over 82 characters print nothing; `$GN`
  sentences, a `$` starting a sentence over, minutes with two and six decimals (rounded half up)
  and 90°N 180°W print as expected.

`tests/test_gnss_fixture.py` runs a copy of `test_reports_a_fix` through `pytester` twice, and it
must fail both times: on `gnss-generated-1.jsonl` with one digit of its first latitude changed
(and its checksum fixed), where the firmware prints `fix acquired lat=52.229843 lon=21.012233`;
and with the profile's `uart.gnss` at 115200 baud, where the run ends on the first `frm_err`,
well before the expect's 30 s.

`tests/test_gnss_recordings.py` runs the generator with `--check` on the committed files, runs a
copy of it with `--check` on an edited recording and requires the stale message verbatim and exit
1, and reads each recording back: header fields, 60 events at 1 s to 60 s, all `in`, `$GPGGA` and
`$GPRMC` in turn, each checksum, at most 82 characters, under half a second on the wire at 9600
baud; and the first position of each.

Not covered: the build inside the real container (only `make firmware` and the CI `firmware` job
run it), the `nmea_reader` and `gpio_echo` Kconfig options other than their defaults,
a `GGA` or `RMC` whose time field is shorter than six digits, the container side of the script
(`ESP32TL_CONTAINER` set inside a container), the `'idf.py --version'` failure, and the
foreign-container branch of the provenance check, which only a stale real build reaches. The
timeout reason of `requires_docker` is untested.

## See also

- [validation-firmware-user.md](validation-firmware-user.md): building the images.
- [Firmware decisions](../decisions/firmware.md): why the build works this way.
- [CI](../cicd.md): the `firmware` job and its tarball.
- [Implementation plan, M0](../implementation-plan.md#m0-bootstrap-console-only) and
  [section 7](../implementation-plan.md#7-risks-and-what-we-do-about-them): the pinned ESP-IDF and
  goldens.
- [Stage 0 findings, Inputs](../stage0-findings.md#inputs): the ESP-IDF image and digest stage 0
  measured with.
