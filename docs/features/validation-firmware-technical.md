# Validation firmware: how it works

`firmware/build.sh` builds the ESP-IDF validation images; the `Makefile` targets `firmware` and
`firmware/%/build/esp32tl-built` call it. `firmware/build-platformio.sh` builds the PlatformIO
image, `arduino_shell` ([The PlatformIO build](#the-platformio-build)); `firmware` and
`firmware/%/.pio/build/esp32dev/esp32tl-built` call it. `tests/test_firmware_build.py` and
`tests/test_platformio_firmware_build.py` test the scripts against fakes (`--chips` in
`test_chip_images_build_their_projects_for_the_chip` and
`test_chip_build_forwards_the_mode_into_the_pinned_container`),
`tests/test_firmware_images.py` checks the images they built, the chips' among them
(`test_a_chip_s_image_is_built_for_it_with_the_bootloader_at_0`: the target, `target` in
`esp32tl-build.json` and the bootloader at `0x0`; `test_the_s3_s_psram_image_asks_for_octal_psram`;
`test_gpio_echo_takes_the_devkit_echo_lines_from_sdkconfig`). `firmware/nmea_reader/` and
`firmware/gpio_echo/` are the images whose source lives here ([nmea_reader](#nmea_reader),
[gpio_echo](#gpio_echo)), and
`scripts/generate-gnss-recordings.py` writes the recordings it is tested with
([The GNSS recording generator](#the-gnss-recording-generator)).

## Data flow

`firmware/build.sh [IMAGE...]`, `firmware/build.sh --examples [NAME...]` and
`firmware/build.sh --chips [IMAGE@CHIP...]` run twice for a container build: once on the host, which starts
the container, and once inside it, which builds. With `IDF_LOCAL=1` it runs once, on the host.

1. **Image names and sharding.** `--shard K/N` after the optional `--examples` or `--chips` selects
   every Nth image of the validated argument list (or the whole default family), starting at K.
   K and N are positive decimal integers without leading zeros, with `1 <= K <= N`.
   An invalid or empty selection fails before Docker or output writes; an empty shard reports
   `firmware/build.sh: shard K/N selects no images`. The host passes only selected names to
   the container, so they are not sharded again. Without the option, every image is selected.
   CI uses four validation shards, three example shards and three chip shards; adding a project
   automatically assigns it to one shard. Each shard still shares compiler-cache hits within its container.

   **Image names.** Each argument must be in `IMAGES`, or the script dies before anything is
   written. With no arguments, the list is all of `IMAGES`. With `--chips`, `IMAGES` is
   `CHIP_IMAGES`: `IMAGE@CHIP` for each of `hello_world`, `console_repl`, `gpio_echo`,
   `nmea_reader`, `nvs_reader` and `http_request` on each of `esp32s3`, `esp32c3` and `esp32c6`,
   then `hello_world_psram@esp32s3`, `gatt_server@esp32s3` and `gatt_server@esp32c3`
   (`project_of`: `$IDF_PATH/examples/bluetooth/bluedroid/ble/gatt_server`), then
   `hello_world_8mb` on each of `esp32s3`, `esp32c3` and `esp32c6`; `gpio_echo@esp32` is
   unknown there. The family's output tree
   is `firmware/` for `--chips` as without a family, `examples/` for `--examples`.
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
   default above, as arguments, `--examples` or `--chips` first when given:

   ```sh
   docker run --rm --init \
     --user "$(id -u):$(id -g)" \
     --env HOME=/tmp \
     --env CCACHE_BASEDIR=/out \
     --env CCACHE_NOHASHDIR=1 \
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
     directory made writable first. ESP-IDF's `http_request` example is such a project, which is
     why it is built from a copy under `/out` ([`http_request`](#http_request)).
   - `--volume "$out:/out"` with `FIRMWARE_OUT=/out`: the output directory, the only writable
     mount.
   - `--user "$(id -u):$(id -g)"`: the host's uid owns what the build writes. Under rootless
     Docker or Podman that uid maps to another one on the host, which is why they are not
     supported. Neither mount carries an SELinux label option, so in enforcing mode the container
     cannot read the repository.
   - `--env HOME=/tmp`: the host's uid is unknown to the image; this gives it a writable home.
   - `--env CCACHE_BASEDIR=/out`, `--env CCACHE_NOHASHDIR=1`: the image turns ccache on
     (`IDF_CCACHE_ENABLE=1`), and one container builds a whole family, so its images share a cache
     in `/tmp/.cache/ccache`. Every compile line and working directory names the image's build
     directory, which without these makes every image miss: 0 hits of 874 compilations for
     `gpio_echo` after `hello_world`, against 870 of 874 with them. The cache dies with the
     container, and `firmware/build.sh --examples` runs in a container of its own. So does each
     image `make test` builds: its rules name one image at a time, and they get no hits. Objects that
     came from the cache keep the working directory of the image that compiled them in their debug
     information, so `gpio_echo.elf` also names `/out/hello_world/build`; `gpio_echo.bin` differs
     only in its build time and SHA-256s
     ([decisions/firmware.md](../decisions/firmware.md#images-of-one-container-share-their-ccache-hits)).
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
     `$IDF_PATH/examples/storage/sd_card/sdspi`, `http_request` is a copy of
     `$IDF_PATH/examples/protocols/http_request` at `$out/.http_request-$idf_version`
     ([`http_request`](#http_request)), `pppos_client` what `firmware/pppos_client/prepare.py`
     prints ([Pinned esp-protocols source](#pinned-esp-protocols-source)), `nmea_reader` is `$PWD/firmware/nmea_reader`,
     `gpio_echo` `$PWD/firmware/gpio_echo`, `sd_remount` `$PWD/firmware/sd_remount`, `modem_at`
     `$PWD/firmware/modem_at`, `modem_recovery` `$PWD/firmware/modem_recovery`, and `nvs_reader` `$PWD/firmware/nvs_reader`, the working directory being the repository's
     root: in
     the container `/project/firmware/nmea_reader`, in a local build the checkout's own path), the
     build directory `$out/IMAGE/build`, or `$out/IMAGE/build-CHIP` for `IMAGE@CHIP`, whose
     target is `CHIP` (`esp32` otherwise). If
     that directory exists and its `esp32tl-build.json` is missing or, compared as text, is not
     the record this build would write (another `idf_version`, `container`, `project` or `target`), the
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
     idf.py -C "$project" -B "$build" -DSDKCONFIG="$build/sdkconfig" -DIDF_TARGET="$target" build
     ```

     runs, with `-DSDKCONFIG_DEFAULTS=...` before `build` for the images that add options of their
     own: `pppos_client` and `bt_spp_initiator_psram` (the project's `sdkconfig.defaults`, then
     `firmware/IMAGE/sdkconfig.defaults`), `hello_world_psram` (`firmware/hello_world_psram/sdkconfig.defaults`
     alone: ESP-IDF's `hello_world` has none) and `hello_world_8mb`, on every chip
     (`firmware/hello_world_8mb/sdkconfig.defaults` alone). `sdkconfig` goes in the build directory because the project directory may be
     ESP-IDF's own examples, or read-only. After a successful `idf.py`, the script checks that
     `flasher_args.json` exists, creates `esp32tl-built`, and prints the `built in` line.

The record is written before `idf.py` runs, so a failed compile keeps the build directory and the
next build is still incremental; the marker, removed first, is what says it finished.

The Makefile's `firmware` target runs `FIRMWARE_OUT= firmware/build.sh`; the pattern rule
`firmware/%/build/esp32tl-built` runs `FIRMWARE_OUT= firmware/build.sh $*`, and `test` depends on
`$(FIRMWARE_IMAGES:%=firmware/%/build/esp32tl-built)` with `FIRMWARE_IMAGES := hello_world
nmea_reader gpio_echo sdspi sd_remount modem_at modem_recovery nvs_reader pppos_client mqtt_session http_request persistence ota_state timer_wake deep_sleep bt_spp_initiator bt_spp_initiator_psram`. `determinism` depends on
the markers of `nmea_reader`, `pppos_client`, `mqtt_session`, `http_request` and `bt_spp_initiator`,
and of the `position_tracker` example. The rule has no prerequisites, so make rebuilds
an image only when its marker is missing: an edit to `firmware/nmea_reader/` needs `make firmware`, or
`firmware/build.sh nmea_reader`, before `make test` sees it.

The chips' images have one pattern rule per chip, `firmware/%/build-esp32s3/esp32tl-built` running
`FIRMWARE_OUT= firmware/build.sh --chips $*@esp32s3`, and likewise for `esp32c3` and `esp32c6`;
`test` also depends on `$(CHIP_IMAGES:%=%/esp32tl-built)`, `CHIP_IMAGES` being the build
directories `firmware/IMAGE/build-CHIP` of `firmware/build.sh --chips`. GNU make 3.81, macOS's,
takes the rule whose pattern matches: `firmware/%/build/esp32tl-built` never matches a
`build-CHIP` directory, nor the reverse. `make firmware` runs `firmware/build.sh --chips` between
the classic chip's images and the PlatformIO ones.

`nmea_reader`, `gpio_echo`, `sd_remount`, `modem_at`, `mqtt_session` and `nvs_reader` are built from the read-only `/project`
mount like any other project. They take no components from the component manager, and with `sdkconfig` and the
build in the build directory ESP-IDF v5.3.6 writes nothing into their directories.

The `firmware` target then runs `FIRMWARE_OUT= firmware/build-platformio.sh`; the pattern rule
`firmware/%/.pio/build/esp32dev/esp32tl-built` runs `FIRMWARE_OUT= firmware/build-platformio.sh $*`,
and `test` also depends on `$(PLATFORMIO_IMAGES:%=firmware/%/.pio/build/esp32dev/esp32tl-built)`
with `PLATFORMIO_IMAGES := arduino_shell`. The two rules cannot match one target: the ESP-IDF one
needs `/build/esp32tl-built` at the end.

## The PlatformIO build

`firmware/build-platformio.sh [IMAGE...]` follows `build.sh`'s steps, with PlatformIO in place of
ESP-IDF ([decisions/firmware.md](../decisions/firmware.md#the-arduino-image-is-built-with-pioarduino-in-a-pinned-python-container)):

1. **Image names**, in `IMAGES` (`arduino_shell`), checked before anything is written.
2. **Mode**: `ESP32TL_CONTAINER` set, with `/.dockerenv`, is the container side, and `pio` is
   `$PLATFORMIO_CORE_DIR/penv/bin/pio`; else `PIO_LOCAL=1`, a local build with the `pio` on `PATH`,
   `container` `null`; else the host side, which needs `docker` and `docker info`, as in `build.sh`.
3. **Output directory**: `FIRMWARE_OUT`, or the script's directory, `firmware/`, made absolute; the
   script then changes to the repository's root.
4. **In place or a copy**: on the host and in a local build, `in_place` is `1` when the output
   directory is, by name, the repository's `firmware/` (`$(pwd -P)/firmware`), else `0`. The
   container side takes it from `ESP32TL_IN_PLACE` and dies when that is neither `0` nor `1`: in
   there `/out` and `/project` are two mounts of one directory, and the host, which sees both as
   its own paths, is the side that can tell
   ([decisions/firmware.md](../decisions/firmware.md#pio-runs-in-the-arduino-images-own-directory-through-the-writable-out)).
5. **Host side**: creates `.cache/platformio/` and runs

   ```sh
   docker run --rm --init \
     --user "$(id -u):$(id -g)" \
     --env HOME=/tmp \
     --env PLATFORMIO_CORE_DIR=/platformio \
     --env ESP32TL_CONTAINER="$CONTAINER" \
     --env FIRMWARE_OUT=/out \
     --env ESP32TL_HOST_OUT="$out" \
     --env ESP32TL_IN_PLACE="$in_place" \
     --volume "$PWD:/project:ro" \
     --volume "$out:/out" \
     --volume "$PWD/.cache/platformio:/platformio" \
     --workdir /project \
     "$CONTAINER" firmware/build-platformio.sh "$@"
   ```

   The mounts and the user are `build.sh`'s, with PlatformIO's core directory as a third, writable
   mount ([decisions/firmware.md](../decisions/firmware.md#platformios-core-directory-is-cacheplatformio)).
   There is no ccache: PlatformIO's SCons keeps its objects in the build directory, and the
   framework's ESP-IDF libraries come precompiled.
6. **PlatformIO Core** (container side): when `pio --version` is not
   `PlatformIO Core, version $PIOARDUINO_VERSION`, the script prints `installing pioarduino`,
   removes `penv/`, makes it again with `python3 -m venv` and runs
   `pip install --quiet --disable-pip-version-check "pioarduino==$PIOARDUINO_VERSION"` in it. At its
   first build the platform installs its own Python packages into the same `penv/`, and downloads
   the framework (`framework-arduinoespressif32` and `framework-arduinoespressif32-libs`), the
   Xtensa toolchain and its other tools into `packages/`, through `dist/` and `tools/`.
7. **Build side**: `pio --version` gives the version, the text after `PlatformIO Core, version `; a
   local build says on stderr that it is not for goldens. For each image the project is
   `$PWD/firmware/IMAGE`, the platform the value of its `platformio.ini`'s `platform =` line, and
   `pio` runs in `$out/IMAGE`: the project itself when `in_place` is `1`, else a directory into
   which the project's `platformio.ini` and `src/` are copied, `src/` removed first.
   If `$out/IMAGE/.pio` exists and `.pio/build/esp32dev/esp32tl-build.json` is missing or, compared
   as text, not the record this build would write, the script prints `was built another way` and
   removes `.pio/`. Then `esp32tl-built` is removed, the record written,

   ```sh
   pio run --project-dir "$out/IMAGE" --environment esp32dev
   ```

   runs, and after it the script checks that `firmware.bin` exists, writes the record again (pio
   removes `.pio/build/` when `platformio.ini` changed, the record with it), creates `esp32tl-built`
   and prints the `built in` line.

pioarduino's build of `esp32dev` writes `bootloader.bin`, `partitions.bin` (the core's default
table), `firmware.bin` and `firmware.elf`, and merges the bootloader at `0x1000`, the table at
`0x8000`, the platform's `boot_app0.bin` at `0xe000` and the application at `0x10000` into
`firmware.factory.bin`, which `esp32tl.build` flashes from `0x0`
([builds](builds-technical.md)).

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
(`set_input`, `esp32/src/periph.rs:386`). Its pulls disabled, the input reads whatever level the
harness drives on it: the board's pull from the profile, or the test's.

## Modem recovery validation

`firmware/modem_recovery/` is an ESP-IDF project on `esp_modem`, `esp_netif` and lwIP, with the
SIM7600 commands, UART1 on TX 26 and RX 27 without flow control and the APN `internet`. Its
`CMakeLists.txt` runs `firmware/pppos_client/prepare.py` with `dependencies/` in the build directory
as the output, as `examples/position_tracker` does, and takes `esp_modem` from there, with
`IDF_COMPONENT_MANAGER=0`; so `firmware/build.sh` is enough and the read-only `/project` stays
unwritten; a build from scratch downloads the archive again, one copy per build tree, as
`examples/position_tracker`'s does; `pppos_client`'s copy is the one in `$FIRMWARE_OUT`. Its `sdkconfig.defaults` enables PPP. It has no firmware-specific Kconfig.

`IP_EVENT` handling sets `GOT_IP` and `LOST_IP` bits. `dial` clears both first: leaving data mode
posts no `IP_EVENT_PPP_LOST_IP`, so the last address's `GOT_IP` may still be set. After 30 s with no
address it calls `esp_modem_set_mode(..., ESP_MODEM_MODE_COMMAND)`, because `esp_modem` stays in data
mode until told, refuses another dial and hands the next answer to PPP. `wait_for_loss` ends after `SILENT_PROBES`,
10, requests without an answer; that is longer than lwIP's `FSM_DEFTIMEOUT`, 6 s, so
`IP_EVENT_PPP_LOST_IP` is heard first when it comes. RI is read 20 ms after a pulse's end, as
`modem_at` does, because the model's RI follows 1 ms after the release.

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

## `nvs_reader`

`firmware/nvs_reader/main/main.c` depends on `nvs_flash`. It calls `nvs_flash_init`, opens
`config` with `NVS_READONLY`, and reads `broker_host`, `broker_port`, `enabled`, `count`,
`delta`, `small`, `medium`, `total`, `offset` and `broker_ca` with `nvs_get_str`, `nvs_get_u16`,
`nvs_get_u8`, `nvs_get_u32`, `nvs_get_i32`, `nvs_get_i8`, `nvs_get_i16`, `nvs_get_u64`,
`nvs_get_i64` and `nvs_get_blob`. Buffers for the string and blob are 256 bytes. Every call
is checked with `ESP_ERROR_CHECK`; after printing the values it closes the handle and returns.
The default ESP-IDF partition table puts `nvs` at `0x9000`, size `0x6000`.

`tests/test_nvs_reader.py` boots this image with two configurations supplied by `board.nvs()`;
`tests/test_firmware_images.py::test_nvs_reader_has_a_writable_nvs_partition` checks the real
partition table and build provenance. `make firmware` and `make test` include the image;
CI's existing firmware glob includes its build in the cache.

## `persistence`

`firmware/persistence/main/main.c` depends on `nvs_flash`, `esp_timer`, `esp_driver_spi`, `fatfs`
and `sdmmc`. `count_boot` calls `nvs_flash_init`, opens `persistence` with `NVS_READWRITE`, reads
`boots` (absent is 0), writes it back one higher and commits. `next` keeps a count beside a marker
word, `0x5e5510f1`, in `.rtc_noinit` (`RTC_NOINIT_ATTR`, RTC slow memory) and in `.noinit`
(`__NOINIT_ATTR`, DRAM): a marker that is not there, as after a power-on, restarts the count at 1.
`log_boot` mounts the card as `examples/sd_data_logger` does (`spi_bus_initialize` on
`SDSPI_HOST_DEFAULT`, `esp_vfs_fat_sdspi_mount` at `/sdcard`, no formatting), appends a line to
`BOOTS.TXT`, counts the newlines of the whole file, unmounts and frees the bus; a mount that fails
frees the bus and gives -1. The image uses ESP-IDF's default `sdkconfig` and partition table:
`nvs` at `0x9000`, size `0x6000`.

What it proves on esp32sim, measured on the runs of `tests/test_power.py`: `esp_restart()` keeps
both memory counts (2 on the second boot) and gives `reason=3 rom=12`; `board.reset()` and a power
cycle give `reason=1 rom=1` with both counts back at 1, the NVS count and the card's lines going on,
and `uptime_ms` under a second at the print after a reset, while `board.now` goes on.

## `ota_state`

`firmware/ota_state/main/main.c` depends on `app_update`, `bootloader_support`, `spi_flash` and
`esp_partition`. `firmware/ota_state/sdkconfig.defaults` sets `CONFIG_PARTITION_TABLE_TWO_OTA`,
`CONFIG_ESPTOOLPY_FLASHSIZE_4MB`, which that table needs, and `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE`.
`build.sh` passes nothing for it: ESP-IDF takes `sdkconfig.defaults` from the project directory.

`app_main` decides by what it runs, with no state of its own:

| Running | State | What it does |
|---|---|---|
| `factory` | | `copy_app_to(esp_ota_get_next_update_partition(NULL))`, `esp_restart()` |
| `ota_0` | `pending_verify` | `esp_ota_mark_app_valid_cancel_rollback()`, then the same copy, to `ota_1`, and restart |
| `ota_1` | `pending_verify` | `esp_ota_mark_app_invalid_rollback_and_reboot()`, which restarts into `ota_0` |
| any other | any other | Done |

`copy_app_to` takes the image's length from `esp_image_verify` of the running partition, and copies
that many bytes, 4 KiB at a time, through `esp_ota_begin`, `esp_partition_read`, `esp_ota_write` and
`esp_ota_end`, then `esp_ota_set_boot_partition`. The bootloader moves the entry from `new` to
`pending_verify`, which is why the first line of a new app already says so.

What it proved on esp32sim at the pin, the first firmware to try: ESP-IDF's OTA works there with no
workaround. The bootloader reads the OTA data, boots the slot it selects through the flash cache, and
writes the entry's state back; `esp_ota_write` programs the slot; `esp_restart` and the rollback's
reboot come back through it. The whole sequence takes about 2 emulated seconds, 200 ms of each step
the firmware's own wait.

## `timer_wake`

`firmware/timer_wake/main/main.c` depends on `esp_hw_support` (`esp_sleep.h`) and uses ESP-IDF's
default `sdkconfig` and partition table. `boots`, `RTC_DATA_ATTR`, is in `.rtc.data`: the
bootloader loads it from the image, as 0, at every boot but a wake from deep sleep, so the count
goes on through the sleeps and starts again at a power-on or a reset. `BOOTS` (3) and `SLEEP_US`
(2 000 000) set the number of boots and the timer: `esp_sleep_enable_timer_wakeup(SLEEP_US)` and
`esp_deep_sleep_start()`, with no other wake source armed.

What it proves on esp32sim, measured on the runs of `tests/test_timer_wake.py`: from the pin
`afcfba6` on, each sleep ends in a `reset` with cause 5 (`DEEPSLEEP_RESET`), the boots 2.045 and
2.046 s apart on the board's clock, and the image reports `rom=5 wake=4` with the count going on.
On the previous pin, `8c65215`, the chip never wakes, and the test fails with `NeverHappened` on
the second boot.

## `deep_sleep`

`project_of` maps `deep_sleep` to `$IDF_PATH/examples/system/deep_sleep`, built in place as
`hello_world` and `sdspi` are: the example takes no component from the component manager, so it
writes nothing into its read-only project directory. Its `sdkconfig.defaults` in v5.3.6 sets
`CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ_80`, `CONFIG_ULP_COPROC_ENABLED` with 512 bytes reserved,
`CONFIG_NEWLIB_TIME_SYSCALL_USE_RTC_HRT`, `CONFIG_RTC_CLK_SRC_INT_RC` and
`CONFIG_BOOTLOADER_SKIP_VALIDATE_IN_DEEP_SLEEP`; its Kconfig leaves `CONFIG_EXAMPLE_TOUCH_WAKEUP`
and `CONFIG_EXAMPLE_EXT1_WAKEUP` (pins 2 and 4, no internal pull-ups) on, and ext0 off on the
classic chip. It has no partition table of its own in v5.3.6, so it flashes the default three
files. `sleep_enter_time` is `RTC_SLOW_ATTR`, and the time it prints is `gettimeofday` after the
wake minus `gettimeofday` before the sleep, both on the RTC (`RTC_HRT`).

What it proves on esp32sim, measured on the runs of `tests/test_deep_sleep.py` at the pin
`afcfba6`: the touch pads read an average of 1000 and arm a threshold of 900, the EXT1 pins
neither wake the chip nor keep it from sleeping, and the timer wakes it 20.004 s after
`Entering deep sleep`; the wake's line comes 143 ms after the reset, and its measure, 20 142 ms,
is the board's time from `Entering deep sleep` to that line, to within 10 ms. Both differences,
the 4 ms of the wake and the 5 ms by which the measure falls short of the board's 20.147 s, are
the chip's RTC running some 200 to 250 ppm slow of the board's time; they grow with the sleep.
The line's bytes take no time of their own: they are stamped as the firmware writes them
(`emulated-fifo-write`, [protocol 5.1](../protocol.md#51-hello)). Two sleeps take 42.5 s of the
board's time, 29 turns of the link and 0.2 s of wall time on an M2 Pro.

## `bt_spp_initiator`

`project_of` maps `bt_spp_initiator` to
`$IDF_PATH/examples/bluetooth/bluedroid/classic_bt/bt_spp_initiator`, built in place as
`deep_sleep` is: it takes no component from the component manager. Its `sdkconfig.defaults` in
v5.3.6 sets `CONFIG_BT_ENABLED`, `CONFIG_BTDM_CTRL_MODE_BR_EDR_ONLY` (and the other two modes
off), `CONFIG_BT_CLASSIC_ENABLED`, `CONFIG_BT_SPP_ENABLED` and `CONFIG_BT_BLE_ENABLED=n`; its
Kconfig leaves `CONFIG_EXAMPLE_PEER_DEVICE_NAME` at `ESP_SPP_ACCEPTOR` and
`CONFIG_EXAMPLE_SSP_ENABLED` on. Its project is `bt_spp_initiator_demo`, and it flashes the default
three files. `main/main.c` asks for `ESP_SPP_SEC_AUTHENTICATE`, `ESP_SPP_ROLE_MASTER` and L2CAP
ERTM (`esp_spp_enable_l2cap_ertm`), inquires with `inq_len` 30 (38.4 s), compares the name in each
result's extended inquiry response with the peer's, and writes `ESP_SPP_MAX_MTU` (990) bytes of
`spp_data`, `spp_data[i] = i`, from each `ESP_SPP_WRITE_EVT` to the next; in `SPP_SHOW_SPEED` mode,
the default, it prints `speed(...)` when 3 s or more have passed since the last.

What it proves on esp32sim at the pin `c99fb28`, through the `bt` fixture's controller in the
harness ([Bluetooth peer](bluetooth-peer-technical.md)), measured on the runs of
`tests/test_bt_spp_initiator.py`, and at `3bdb958` with the same authentication, channel and
`speed(...)` lines and the same 423 packets from the host and 432 to it:
`esp_bt_controller_enable` takes mode 2, which esp32sim's hook refused before
mikroverk/esp32sim#347; the inquiry finds an `SppPeer` 20 ms after it starts, and the firmware
connects, finds the service by SDP, falls back from ERTM to L2CAP's basic mode (`BT_L2CAP:
l2c_fcr_adj_our_req_options (mode 3): ERROR: No FCR options set using BASIC mode`), asks for the
channel, pairs with the PIN after RFCOMM's PN and opens the channel at SABM, 1.374 s after power-on,
with the peer's `secure=True`. It then writes about 299 kbit/s, which is what the peer's source
hears in the same window, to within one block. With another PIN on the peer it fails the
authentication with HCI's `AUTHENTICATION_FAILURE` (`Authentication fail reason 5`, then
`authentication failed, status:9`, `ESP_BT_STATUS_AUTH_FAILURE`) and the link falls before SABM, so
the peer refuses nothing. With nobody of the name on the air, the inquiry's two
`ESP_BT_GAP_DISC_STATE_CHANGED_EVT` are 38.4 s apart, and nothing follows. With Secure Simple
Pairing on the peer, `main.c`'s IO capability, `ESP_BT_IO_CAP_IN`, a keyboard only, decides: a peer
with no input and output pairs by Just Works, which ESP-IDF's Bluedroid confirms without telling
the example, and the channel opens 1.414 s after power-on; a peer with a display gets
`ESP_BT_GAP_KEY_REQ_EVT` at 1.354 s, which the example answers only with `spp key …;` on its
console, and Bluedroid's security timer ends the pairing 35.0 s later with
`User_Passkey_Request_Negative_Reply` (`Authentication fail reason 5`, `authentication failed,
status:9`). The controller knows every command the firmware's Bluedroid sends:
`bt.unknown_commands()` is empty in each run. A run
to the first `speed(...)` takes 4.4 s of the board's time and about 1 s of wall time on an M2 Pro,
the 39.4 s of the inquiry with nobody 2.7 s; esp32sim prints `[ble] the virtual controller is LE-only:
enabled in mode 2 (BR/EDR), its commands get Unknown HCI Command` on the runner's stderr at the
enable, which with `--bt` does not hold: the controller is the harness's
([runner](runner-technical.md#bluetooth-the-guests-hci-on-the-socket)).

## `bt_spp_initiator_psram`

`project_of` maps `bt_spp_initiator_psram` to the same
`$IDF_PATH/examples/bluetooth/bluedroid/classic_bt/bt_spp_initiator`, built in place into its own
build directory. As for `pppos_client`, `build.sh` passes the project's `sdkconfig.defaults`
followed by `firmware/<image>/sdkconfig.defaults` through `SDKCONFIG_DEFAULTS`; for this image the
second sets `CONFIG_SPIRAM` and `CONFIG_BT_ALLOCATION_FROM_SPIRAM_FIRST`, and Kconfig leaves the
rest of PSRAM at v5.3.6's defaults, among them `CONFIG_SPIRAM_USE_MALLOC`. Its provenance record
names the example's directory, as the plain image's does.

In ESP-IDF v5.3.6 the option makes `osi_malloc_base` and `osi_calloc_base`
`heap_caps_malloc_prefer` and `heap_caps_calloc_prefer` with `MALLOC_CAP_SPIRAM` first and
`MALLOC_CAP_INTERNAL` second (`components/bt/common/osi/include/osi/allocator.h:38`). These ask
the heap for those capabilities directly, so `CONFIG_SPIRAM_MALLOC_ALWAYSINTERNAL`, which applies
to `malloc()`, does not keep small buffers internal. `make_command`
(`components/bt/host/bluedroid/hci/hci_packet_factory.c:252`), which makes `HCI_Reset` and the
other commands, takes its buffer from `HCI_GET_CMD_BUF`, `hci_get_cmd_buf`, an `osi_calloc`
(`components/bt/host/bluedroid/stack/include/stack/hcimsgs.h:56`); `transmit_data` in
`components/bt/host/bluedroid/hci/hci_hal_h4.c` hands a pointer into it to
`hci_host_send_packet`, which calls `esp_vhci_host_send_packet`
(`components/bt/host/bluedroid/api/esp_bluedroid_hci.c:106`). On the classic ESP32 the data bus
sees PSRAM at `SOC_EXTRAM_DATA_LOW` to `SOC_EXTRAM_DATA_HIGH`, `0x3F80_0000` to `0x3FC0_0000`
(`components/soc/esp32/include/soc/soc.h:193`).

Measured on 2026-11-10 at the previous pin, `c99fb28`, on `lilygo-t-a7670g-r2`, with a temporary
print in esp32sim's `Send` hook: the firmware's one packet is `HCI_Reset`, 4 bytes at `0x3f8010cf`,
which the hook refuses; the plain image's `HCI_Reset` is at `0x3ffce0eb`, in DRAM, and all 423 of
its packets pass. The firmware finds the PSRAM, runs its cache in low/high (2-core) mode and adds
4096K of PSRAM to the heap, then times out waiting for the answer at 8.176 s. The classic ESP32's
`valid_packet_range` also taking PSRAM that is mapped and readable is
[mikroverk/esp32sim#353](https://github.com/mikroverk/esp32sim/pull/353), merged as `3bdb958`, the
pin since #180. Measured there on 2026-11-13, the same test passes: the authentication at 1.418 s,
the channel at 1.437 s, `speed(1.272723s ~ 4.292856s): 298.953674 kbit/s`, and 423 packets from the
host, 432 to it, the figures of the local build of that change the week before.

## `sd_remount`

`firmware/sd_remount/main/main.c` depends on `esp_driver_spi`, `fatfs` and `sdmmc`, with the
default `sdkconfig`. `app_main` initialises SPI2 (`SDSPI_HOST_DEFAULT()`'s slot) once, with
`SDSPI_DEFAULT_DMA` and `max_transfer_sz` 4000, and never frees it. Then it loops:

- With no card (`card` `NULL`), `esp_vfs_fat_sdspi_mount("/sdcard", ...)` with CS on GPIO 13,
  `format_if_mount_failed` false, `max_files` 1 and a 16 KiB allocation unit. On success it prints
  `sd_remount: mounted K`, K counting the mounts since boot; on failure it prints
  `sd_remount: no card (ERROR)`, `esp_err_to_name` of the result, unless the last failure had the
  same result, and sleeps 200 ms.
- With a card, it opens `/sdcard/LOG.TXT` for appending, writes the line number `N` and `\n`, and
  closes it. When all three succeed it prints `sd_remount: wrote N` and counts on; otherwise it
  prints `sd_remount: write failed (ERROR)`, `strerror(errno)`, calls
  `esp_vfs_fat_sdcard_unmount`, which removes the card's device from the bus, and keeps `N` for
  the next card. Then it sleeps 100 ms.

With the runner's slot empty, ESP-IDF's driver gets no R1 and returns `ESP_ERR_TIMEOUT`
(`0x107`); FatFs makes that `FR_DISK_ERR`, which the VFS reports as `EIO`, `I/O error` in
newlib's `strerror`.

`tests/test_firmware_images.py::test_sd_remount_has_the_classic_esp32_flash_layout` checks its
flash files and ELF, and `test_sd_remount_takes_the_devkit_sd_card_slot` that the pins in its
source are the `esp32-devkitc` profile's slot. `tests/test_sd_remount.py` runs it in the real
runner, through the `sd_card` fixture, and needs mtools (`requires_mtools`):
`test_writes_fail_once_the_card_is_out_and_go_on_on_the_card_put_in` takes the card out after
`wrote 3`, waits for `write failed (I/O error)` and checks that `LOG.TXT` on that image holds
every line announced before and nothing after; puts in a second, 64 MiB image built after
power-on, on which the lines go on from the one that failed; takes that out and puts the first
back, on which `LOG.TXT` goes on after its old lines; checks that `sd_card.changes` holds the
four, each within a millisecond after the test made it; and takes the card out once more without
a wait, which the report and `board.properties()["sd_card"]` show as not reached, the run still
holding the first card. `test_a_slot_empty_at_power_on_takes_a_card_later` powers on with an
empty slot, waits for `no card`, and inserts one.
`test_a_card_swapped_with_the_supply_off_is_the_one_the_next_boot_finds` swaps the card between
`board.power_off()` and `board.power_on()`. `test_a_card_the_runner_cannot_open_ends_the_run`
runs, through `pytester`, a test that inserts a card, deletes its image and waits: the wait raises
`RunEnded` with `sd_card: the runner refused the card at T ns: PATH: No such file or directory
(os error 2)`.

## `mqtt_session`

`firmware/mqtt_session/` is an ESP-IDF project that needs `esp_modem`, `mqtt` and `esp_netif`. Its
`CMakeLists.txt` takes the esp_modem component as `examples/position_tracker` does: it runs
`firmware/pppos_client/prepare.py` on `dependencies/` in the build directory, adds the
`components/` directory that holds esp_modem to `EXTRA_COMPONENT_DIRS` and sets
`IDF_COMPONENT_MANAGER` to `0`, since ESP-IDF and that download provide every dependency.
`project_of` maps it to `$PWD/firmware/mqtt_session`, and `/project` stays read-only.
`sdkconfig.defaults` enables PPP, as the position tracker's does, and sets 4 MB of flash.

`main.c` uses fixed LilyGO pins (TX 26, RX 27, no flow control), `ESP_MODEM_DCE_SIM7600` and the APN
`internet`. `esp_modem_set_mode(ESP_MODEM_MODE_DATA)` goes to data mode and `app_main` waits for
`IP_EVENT_PPP_GOT_IP`. `mqtt_event` subscribes in `MQTT_EVENT_CONNECTED` and prints from the event
structure. esp-mqtt writes its PUBACK after it has handed the message to `MQTT_EVENT_DATA`, in
`mqtt_process_receive`, so a printed line and the PUBACK come in that order.

`command` reads `backlog <n>` from a message without DUP into a queue of four, which `report_backlogs`,
a task of its own, takes: it calls `esp_mqtt_client_publish` for each report and blocks in
`xTaskNotifyWait` until `mqtt_event`, on `MQTT_EVENT_PUBLISHED`, notifies it with the same message
ID. A publish that returns an error prints `mqtt_session: publish failed: <code>` and ends that
backlog without `backlog done`. It is not done in the event handler, because that is esp-mqtt's task, and the PUBACK has to be
read there.

`tests/test_firmware_images.py::test_mqtt_session_has_the_classic_layout` checks the layout,
`tests/test_firmware_build.py` where it is built from, and `tests/test_mqtt_session.py` what it does
against the broker.

## `http_request`

ESP-IDF's `examples/protocols/http_request`, built as it is: no file of this repository goes into
it, and no `SDKCONFIG_DEFAULTS` is passed, so its sdkconfig is ESP-IDF v5.3.6's defaults for the
example (`EXAMPLE_CONNECT_WIFI=y`, `EXAMPLE_WIFI_SSID="myssid"`, `EXAMPLE_WIFI_PASSWORD="mypassword"`,
from `protocol_examples_common`'s Kconfig). Its `main/idf_component.yml` takes
`protocol_examples_common` from `$IDF_PATH/examples/common_components` through the component
manager, which writes `dependencies.lock` into the project directory. In the container that
directory is in the image's read-only `IDF_PATH`, so `project_of http_request` copies it first:

```sh
source=$out/.http_request-$idf_version
if [ ! -d "$source" ]; then
	rm -rf "$source.partial"
	cp -Rp "$IDF_PATH/examples/protocols/http_request" "$source.partial"
	mv "$source.partial" "$source"
fi
```

The copy is made once per ESP-IDF version and then reused; one interrupted halfway leaves only the
`.partial` directory, which the next build removes. The copy's path is the `project` that
`esp32tl-build.json` records, so a build of another ESP-IDF version has another project and builds
from scratch. `.gitignore` ignores `firmware/.http_request-*/`.

`tests/test_firmware_images.py::test_http_request_is_esp_idfs_example_with_its_default_wifi` checks
target `esp32`, the flash files `0x1000` `bootloader/bootloader.bin`, `0x8000`
`partition_table/partition-table.bin` and `0x10000` `http_request.bin`, all present, and the three
Wi-Fi options in `config/sdkconfig.json`. `tests/test_http_request.py` and
`tests/test_determinism.py::test_wifi_http` run it against the access point of the `wifi` fixture
([network](network-technical.md)). `tests/test_firmware_build.py` gives the fake `IDF_PATH` an
`examples/protocols/http_request` and checks that the local build builds it from
`.http_request-v9.9.9` under the output directory, a copy of the fake example.

## `http_server`

`firmware/http_server/main/main.c`, one file; `main/CMakeLists.txt` asks for `esp_wifi`,
`esp_netif`, `esp_event`, `esp_timer`, `nvs_flash`, `esp_http_server` and `json` (cJSON), and the
project has no `sdkconfig.defaults`. `app_main` initialises NVS (erasing it when it has no free pages
or a new version), the default event loop and the Wi-Fi station netif, registers `on_event` for
every `WIFI_EVENT` and for `IP_EVENT_STA_GOT_IP`, and starts the station with `SSID` `myssid`,
`PASSWORD` `mypassword` and an auth mode threshold of `WIFI_AUTH_WPA2_PSK`. `on_event` calls
`esp_wifi_connect()` at `WIFI_EVENT_STA_START` and after every `WIFI_EVENT_STA_DISCONNECTED`, and at
the first `IP_EVENT_STA_GOT_IP` calls `start_server`, which registers `/status` (`HTTP_GET`) and
`/echo` (`HTTP_POST`) on `HTTPD_DEFAULT_CONFIG()`: port 80, seven sockets, no LRU purge and no TCP
keep-alive, so the device sends nothing the test did not ask for.

`respond` counts the request in `served`, sets the status and `application/json`, and when the
request's `Connection` header is `close` (case-insensitive, read into 16 bytes) sets the same
header and, after `httpd_resp_sendstr`, calls `httpd_sess_trigger_close`, which closes the socket
once the handler returns. `echo` refuses a `content_len` over `MAX_BODY` (2048) before reading,
then reads the body with `httpd_req_recv` until it has `content_len` bytes, retrying on
`HTTPD_SOCK_ERR_TIMEOUT`, parses it with `cJSON_ParseWithLength` and answers it inside a new object
printed unformatted. `uptime_ms` is `esp_timer_get_time() / 1000`, which is emulated time in the
emulator.

`tests/test_firmware_images.py::test_http_server_is_a_wifi_station_with_esp_http_server` checks
target `esp32`, the flash files `0x1000` `bootloader/bootloader.bin`, `0x8000`
`partition_table/partition-table.bin` and `0x10000` `http_server.bin`, all present, and ESP-IDF's
default `HTTPD_MAX_REQ_HDR_LEN` of 512. `tests/test_http_server.py` runs it against the access
point of the `wifi` fixture ([network](network-technical.md#connections-the-test-opens)).
`tests/test_firmware_build.py` checks that the local build builds it from `firmware/http_server`.

## `arduino_shell`

`firmware/arduino_shell/platformio.ini` has one environment, `[env:esp32dev]`: the pinned platform
URL, `board = esp32dev`, `framework = arduino`, `monitor_speed = 115200`, and no build flags, so the
core's log level is its default, errors only. `src/main.cpp`, one file, includes `Arduino.h`,
`Preferences.h`, `WebServer.h` and `WiFi.h`.

`setup()` starts `Serial` at 115200 baud, prints the banner with `ESP_ARDUINO_VERSION_STR`, and
opens the namespace `shell` read-only with `preferences.begin("shell", true)`, so that the sketch
writes nothing to NVS. On a flash without the namespace `nvs_open` fails, the core logs
`begin(): nvs_open failed: NOT_FOUND`, and `getString` returns its default, `""`, for `greeting`,
`ssid` and `password`. With an `ssid`, `WiFi.mode(WIFI_STA)` and
`WiFi.begin(ssid, password)`; `WiFi`'s auto-reconnect, on by default, is left as it is, and no
test takes the access point away. Every line goes through `Serial.print` and
`Serial.println`.

`loop()` feeds every byte `Serial` has to `take`, which ends a line at `\n`, drops a `\r` before it,
and keeps at most `MAX_LINE` (64) bytes of a line, then hands it to `run`; an empty line prints
nothing. Once `WiFi.status()` is `WL_CONNECTED` it registers `status` for `GET /status` on a
`WebServer` on port 80, calls `server.begin()` and prints the `listening on` line, once; from then on
every `loop()` calls `server.handleClient()`, then `delay(1)`. `status` counts the request in
`requests` and sends `200` with `application/json`; `uptime_ms` is `millis()`, which is emulated
time in the emulator.

`tests/test_arduino_shell.py` runs it through `pytest.mark.app_path("../firmware/arduino_shell",
build_dir=".pio/build/esp32dev")`, so the backend loads the PlatformIO build directory as it is.

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

**The PlatformIO record**, in `.pio/build/esp32dev/esp32tl-build.json`, the same way:

```json
{"platformio": "6.2.0", "platform": "https://github.com/pioarduino/platform-espressif32/releases/download/55.03.312-1/platform-espressif32.zip", "container": "python:3.13-bookworm@sha256:cc55e4ded9c95f6ce2133b3d096db43f214d280c2a8af65d83e246c8fa7d168c", "project": "/project/firmware/arduino_shell"}
```

| Field | Type | Value |
|---|---|---|
| `platformio` | string | what `pio --version` printed after `PlatformIO Core, version ` |
| `platform` | string | the value of the project's `platform =` line |
| `container` | string or `null` | as in the ESP-IDF record; `null` for a `PIO_LOCAL=1` build |
| `project` | string | the absolute path of the project under `firmware/`, as seen where the build ran |

Its `esp32tl-built` is written after `pio run` succeeded and left `firmware.bin`.

Everything else in the build directory is ESP-IDF's (`flasher_args.json`, the ELF, the binaries,
`config/sdkconfig.json`, `project_description.json`). The CI `firmware` job packs the files at the
top of each build directory, `config/sdkconfig.json`, and the `.bin` and `.elf` files of
`bootloader/` and `partition_table/` ([CI](../cicd.md)).

## Interfaces

- `firmware/build-platformio.sh [IMAGE...]`: the same, with `pio` in place of `idf.py`; its
  messages are in the [user document](validation-firmware-user.md#build-the-arduino-image).
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
| `IMAGES` | script constant | `hello_world nmea_reader gpio_echo sdspi sd_remount modem_at modem_recovery nvs_reader pppos_client mqtt_session http_request persistence ota_state timer_wake deep_sleep bt_spp_initiator bt_spp_initiator_psram` | the images, in build order; `project_of` maps each to its project |
| `FIRMWARE_OUT` | environment | `firmware/` | where `IMAGE/build/` goes; empty means the default; `/out` inside the container |
| `IDF_LOCAL` | environment | unset | `1` builds with the ESP-IDF of the calling shell |
| `ESP32TL_CONTAINER` | environment, set by the host side | unset | marks the container side and names the image it records |
| `ESP32TL_HOST_OUT` | environment, set by the host side | unset | the host's path of `/out`, shown in messages |
| `IDF_PATH` | environment | from `export.sh`, or the image | the ESP-IDF tree the projects come from |
| `PIOARDUINO_VERSION` | constant of `build-platformio.sh` | `6.2.0` | the pioarduino PlatformIO Core the container installs |
| `CONTAINER` | constant of `build-platformio.sh` | `python:3.13-bookworm@sha256:cc55e4ded9c95f6ce2133b3d096db43f214d280c2a8af65d83e246c8fa7d168c` | the image the PlatformIO build runs in, pinned by digest (a multi-arch index) |
| `IMAGES`, `ENV` | constants of `build-platformio.sh` | `arduino_shell`, `esp32dev` | the PlatformIO images, and the environment of each `platformio.ini` |
| `platform` | `firmware/<image>/platformio.ini` | `https://github.com/pioarduino/platform-espressif32/releases/download/55.03.312-1/platform-espressif32.zip` | pioarduino's espressif32 platform, which pins the framework and the toolchains |
| `PIO_LOCAL` | environment | unset | `1` builds with the `pio` of the calling shell |
| `ESP32TL_IN_PLACE` | environment, set by the host side | unset | `1` when `/out` is the repository's `firmware/`, so that pio runs in the project itself; `0` for a copy |
| `PLATFORMIO_CORE_DIR` | environment, set by the host side | `/platformio`, the repository's `.cache/platformio/` | PlatformIO's core directory in the container |

`tests/test_firmware_images.py` reads `IDF_VERSION=` and `CONTAINER=` from `build.sh`, and
`PIOARDUINO_VERSION=` and `CONTAINER=` from `build-platformio.sh` and `platform =` from
`platformio.ini`, with regular expressions, so each has to stay on a line of its own; the CI key
of PlatformIO's core directory hashes the same lines. Changing any of them, or anything under
`firmware/`, changes the CI cache key of the firmware job.

## Failure modes

`build.sh` dies with `firmware/build.sh: MESSAGE` on stderr and exit 1:

| Message | Cause |
|---|---|
| `unknown image 'NAME'; the images are: hello_world nmea_reader gpio_echo sdspi sd_remount modem_at modem_recovery nvs_reader pppos_client mqtt_session http_request persistence ota_state timer_wake deep_sleep bt_spp_initiator bt_spp_initiator_psram` | an argument not in `IMAGES`; nothing written |
| `ESP32TL_CONTAINER is set outside a container; unset it` | `ESP32TL_CONTAINER` set and no `/.dockerenv`; nothing written |
| `IDF_LOCAL=1 needs ESP-IDF in the environment: source $IDF_PATH/export.sh first` | `IDF_LOCAL=1` without `idf.py` on `PATH` or without `IDF_PATH`; nothing written |
| `needs Docker, or IDF_LOCAL=1 to build with a local ESP-IDF` | no `docker` on `PATH`; nothing written |
| `the Docker daemon is not reachable ('docker info' failed); is Docker running?` | `docker info` failed; nothing written |
| `'idf.py --version' printed no ESP-IDF version` | no line starting `ESP-IDF ` in its output |
| `IMAGE: the build wrote no flasher_args.json` | `idf.py` succeeded but left no `flasher_args.json`; no marker |

`build-platformio.sh` dies with `firmware/build-platformio.sh: MESSAGE` on stderr and exit 1:

| Message | Cause |
|---|---|
| `unknown image 'NAME'; the images are: arduino_shell` | an argument not in `IMAGES`; nothing written |
| `ESP32TL_CONTAINER is set outside a container; unset it` | `ESP32TL_CONTAINER` set and no `/.dockerenv`; nothing written |
| `ESP32TL_IN_PLACE is 'VALUE', not 0 or 1; run the script on the host` | the container side started without the host's `ESP32TL_IN_PLACE`; nothing written |
| `PIO_LOCAL=1 needs PlatformIO's pio on PATH` | `PIO_LOCAL=1` without `pio` on `PATH`; nothing written |
| `needs Docker, or PIO_LOCAL=1 to build with a local PlatformIO` | no `docker` on `PATH`; nothing written |
| `the Docker daemon is not reachable ('docker info' failed); is Docker running?` | `docker info` failed; nothing written |
| `'pio --version' printed no PlatformIO Core version` | no line starting `PlatformIO Core, version ` in its output |
| `IMAGE: PROJECT/platformio.ini sets no platform` | no `platform =` line at the start of a line |
| `IMAGE: the build wrote no firmware.bin` | `pio run` succeeded but left no `firmware.bin`; no marker |

A failing `pip install` or `pio run` ends it with its exit code, no marker and the record kept, if
pio did not remove it.

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
CONTAINER`. For a PlatformIO image, `platformio_build` fails with `DIR holds no finished build: run
make firmware`, `IMAGE was built with PLATFORM, not PINNED_PLATFORM: run make firmware` (a local
build too), `IMAGE was built in CONTAINER (PlatformIO Core VERSION), not in PINNED (PINNED_VERSION):
run make firmware` or `firmware/build-platformio.sh no longer sets PIOARDUINO_VERSION= and
CONTAINER= on lines of their own`, and passes a local build with the warning `IMAGE was built with a
local PlatformIO Core VERSION, not in CONTAINER`.

## Tests

`tests/test_firmware_build.py` runs `build.sh` against a fake `docker` and a fake `idf.py` that
write their arguments to `$FAKE_LOG`, with a `PATH` holding only them and `cat`, `dirname`, `id`,
`mkdir`, `rm` and `sed`. It needs neither Docker nor ESP-IDF and takes milliseconds:

- the four early refusals (unknown image, no Docker, unreachable daemon, `IDF_LOCAL=1` without
  ESP-IDF) and `ESP32TL_CONTAINER` on the host (skipped when the suite itself runs in Docker):
  each message verbatim, exit 1, and no output directory created;
- `test_build_starts_the_pinned_container`: the `docker run` arguments, exactly;
- `test_local_build_runs_idf_py_and_records_how`: with no image named, every image in order,
  `hello_world` and `sdspi` from the fake `IDF_PATH`, `nmea_reader`, `gpio_echo`, `sd_remount`,
  `modem_at`, `modem_recovery` and `nvs_reader` from `firmware/`, `pppos_client` from what a fake `python3` prints,
  `mqtt_session`, `persistence`, `ota_state` and `timer_wake` from `firmware/`, `http_request`
  from its copy, `.http_request-v9.9.9` in the output directory, which holds the fake example's
  `CMakeLists.txt`, and `deep_sleep`, `bt_spp_initiator`, `bt_spp_initiator_psram` and
  `hello_world_8mb` from the fake `IDF_PATH`; the `idf.py` arguments of each,
  with `SDKCONFIG_DEFAULTS` naming the project's `sdkconfig.defaults` and then the image's own for
  `pppos_client` and `bt_spp_initiator_psram`, and the image's own alone for `hello_world_8mb`;
  `test_chip_images_build_their_projects_for_the_chip` the same of `--chips`, `hello_world_8mb`
  on the three chips included,
  the records with `"container": null`, the markers, the "not for goldens" warning and the
  `built in` lines;
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
`test_persistence_has_a_writable_nvs_partition_where_its_boots_are_counted` checks that
`persistence` has the default `nvs` partition at `0x9000`, writable, and that no file of the build
is flashed there. `test_ota_state_has_a_factory_app_two_slots_and_the_initial_ota_data` checks
`ota_state`'s table, that its `ota_data_initial.bin` is erased, and that rollback is on in its
`sdkconfig`. `test_bt_spp_initiator_has_the_classic_esp32_flash_layout` checks the three files of
`bt_spp_initiator`, with `bt_spp_initiator_demo.bin` and `bt_spp_initiator_demo.elf`, and
`test_bt_spp_initiator_runs_its_controller_for_classic_alone` that its `sdkconfig` has
`BTDM_CTRL_MODE_BR_EDR_ONLY`, Classic and SPP on, and the peer's name `ESP_SPP_ACCEPTOR`.
`test_bt_spp_initiator_psram_is_the_example_with_bluedroid_allocating_from_psram` checks that
`bt_spp_initiator_psram` has the same options of Classic and SPP, `SPIRAM`, `SPIRAM_USE_MALLOC`
and `BT_ALLOCATION_FROM_SPIRAM_FIRST` on, and the same ELF name.
`test_the_8_mb_image_s_headers_give_8_mb_on_every_chip` checks that `hello_world_8mb`, in `build`
and in `build-esp32s3`, `build-esp32c3` and `build-esp32c6`, has `ESPTOOLPY_FLASHSIZE` `8MB` and
that the headers of its `bootloader/bootloader.bin` and `hello_world.bin` give 8 MB
(`build.flash_size`). `tests/test_flash_size.py` boots it
([esp32sim backend](esp32sim-backend-technical.md#tests)).

`tests/test_platformio_firmware_build.py` runs `build-platformio.sh` the same way, against a fake
`docker` and a fake `pio` whose `run --project-dir DIR --environment ENV` writes
`DIR/.pio/build/ENV/firmware.bin`: the early refusals (unknown image, no Docker, `PIO_LOCAL=1`
without `pio`, `ESP32TL_CONTAINER` on the host), the `docker run` arguments exactly, with
`ESP32TL_IN_PLACE=0` for another output directory and `ESP32TL_IN_PLACE=1` for the repository's
`firmware/` (the fake builds nothing, so `firmware/` is left as it is), a local build
into another `FIRMWARE_OUT` (the `pio` arguments, the copied `platformio.ini` and `src/main.cpp`, the
record, the marker, the warning and the `built in` line), reuse of a build with the same record,
removal of a `.pio/` with another, the record written again after the fake removed the build
directory as pio does, no marker without `firmware.bin`, and a failed build removing an earlier
marker. The host-side tests leave the repository's `.cache/platformio/` behind, empty if it was not
there: the script creates it before `docker run`.

`tests/test_firmware_images.py::test_arduino_shell_is_a_platformio_build_of_the_whole_flash` loads
the real `arduino_shell` build with `esp32tl.build.load`, after the provenance check: kind
`platformio`, chip `esp32`, `firmware.factory.bin` alone at `0x0`, the table at `0x8000`, the app
image `firmware.bin` and the ELF `firmware.elf`.
`test_arduino_shell_has_the_arduino_default_table_with_its_nvs` reads the table: `nvs` at `0x9000`,
`0x5000` bytes, and `app0` an `ota_0` at `0x10000`. `tests/test_arduino_shell.py` runs it in the
real runner: the banner, the core's `nvs_open failed: NOT_FOUND`, an empty greeting and `ready` on a
flash with no settings, with no warning or error on the link; `greeting=hello from the test` after
`board.nvs({"shell": {"greeting": ...}})`; and with `ssid` and `password` for the `wifi` fixture's
access point, `listening on 10.64.0.2 port 80` within 30 s of the board's time, then a `GET /status`
with `Connection: close` answered `200 OK` with `application/json` and `"requests": 1`, the
connection closed by the device within 5 s, and `GET /status -> 200` on the console.

`tests/test_timer_wake.py` runs the real `timer_wake` in the real runner: the three boots it
prints, with their ROM reset reason and wake cause, `board.boots` with causes 1, 5 and 5, and
between two boots at least the 2 s sleep and at most 0.1 s more; then the same sleeps through
`board.wait_for_wake` ([esp32sim backend](esp32sim-backend-technical.md#tests)).
`tests/test_deep_sleep.py` runs the real `deep_sleep`: for each of two sleeps, the wake 20 s after
`Entering deep sleep` with at most 10 ms more, as boot 2 and 3 with cause 5, at most two turns of
the link in `board.wait_for_wake`, the line
`Wake up from timer. Time spent in deep sleep: <ms>ms` within 0.2 s of the wake, and `<ms>` within
10 ms of the board's time from `Entering deep sleep` to that line.

`tests/test_bt_spp_initiator.py` runs the real `bt_spp_initiator` with the `bt` fixture: against
an `SppPeer` named `ESP_SPP_ACCEPTOR` with the PIN `1234` and `secure=True`, the console's
`ESP_SPP_DISCOVERY_COMP_EVT scn_num:1`, the service name `Serial Port`, `Input pin code: 1234`,
`authentication success: ESP_SPP_ACCEPTOR` and `ESP_SPP_OPEN_EVT` with the peer's address, the
channel open and nothing refused, then the first `speed(...)` line, its rate times its window within
one block of what the peer's source heard between the open and that line, and at least 100 blocks
in all, each the firmware's `spp_data`; against a peer with the PIN `0000`, `authentication
failed, status:9`, `ESP_SPP_CLOSE_EVT` with `close_by_remote:1`, and no channel two seconds later;
with no device on the air, and with a `BluetoothDevice` named `phone`, which it finds and logs by
name, an inquiry 38.4 s long, to within 0.1 s, and no other in the 2 s after it, no SDP and no
channel. Each run checks that `bt.unknown_commands()` is empty where it gets that far.
`tests/test_determinism.py::test_spp_to_a_peer` runs the first case to 5 s and writes its console,
each write the peer heard, with its time, and the tap of the HCI, for the determinism job.
`test_the_tap_writes_the_hci_both_ways_as_btsnoop` runs it to the channel's opening with the HCI
tapped, and reads the btsnoop file back.

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
  with `board.now` just past 10 s, on a runner whose `hello` names the emulated clock and whose
  `stats` carry no `rt_resyncs`, in under the ten wall seconds a run paced to wall time takes:
  nothing waited for wall time, in the runner or the harness. The bound is that, not the host's
  speed
  ([decision](../decisions/harness.md#a-tests-wall-time-bound-proves-what-was-not-waited-for-and-a-paced-one-holds-only-while-the-host-kept-up));
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
foreign-container branch of the provenance check, which only a stale real build reaches. Of the
PlatformIO build: its container side (the `penv/` install, `ESP32TL_IN_PLACE` and `pio` there),
the `'pio --version'` and `sets no platform` failures, a build in place with a local `pio`, and
the `help`, `greeting`, `uptime` and `wifi` commands of `arduino_shell`, which no test can send
while the console has no `write`. The
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


## Pinned esp-protocols source

`firmware/pppos_client/prepare.py` downloads the archive for commit
`2cc7c99664caf9705064affba6ea829137d4e1e8` (`modem-v1.4.1`) from GitHub codeload and checks SHA-256
`7cdcf43c876e0bebbfeafe8aaa543c603df5a035d6e465f2cd743e5214041c32` before extraction.
It extracts through a temporary directory into `$FIRMWARE_OUT/.esp-protocols-<commit>/`, reusing
that tree on later builds. The upstream component manifest's relative `override_path` resolves
to the pinned esp_modem in that tree. No C/C++ source or manifest is patched. `build.sh` passes
upstream `sdkconfig.defaults` followed by `firmware/pppos_client/sdkconfig.defaults` through
`SDKCONFIG_DEFAULTS`; the project path containing the revision is recorded in provenance.
Only the built image is packed into CI's existing firmware cache, as for the other examples.

### Arduino access-point path

`firmware/arduino_shell/src/main.cpp` reads `ap_ssid` and `ap_password` before closing Preferences.
When `ssid` is empty and `ap_ssid` is not, it registers `WiFi.onEvent` for
`ARDUINO_EVENT_WIFI_AP_STACONNECTED` and `ARDUINO_EVENT_WIFI_AP_STADISCONNECTED`, selects `WIFI_AP`
and calls `WiFi.softAP`. On success it registers the existing `/status` handler and starts the
same `WebServer`; the loop's `serving` path handles both modes. Join/leave logs use the event's
MAC rather than an assumed client address. `access_point` distinguishes the console's AP status
from its original off/joining/connected states. The PlatformIO image layout and partition table
stay the same, so `tests/test_firmware_images.py` needs no new image constants.
