# Builds

A test runs the build in the directory it selects, as `idf.py build` or `pio run` left it: esp32tl
reads an ESP-IDF build directory or a PlatformIO one, `.pio/build/<env>/`, and writes, renames or
converts nothing in it. The [esp32sim backend](esp32sim-backend-user.md) runs both kinds, and the
[serial backend](serial-backend-user.md) flashes both, the same files at the same offsets.

## Select the build

The `app_path` marker names the app, relative to the test file's directory, and its `build_dir`
keyword the build directory, relative to the app or absolute, `build` by default
([pytest plugin](pytest-plugin-user.md#writing-a-test)). `esp32tl run` and `esp32tl
replay` take the same two as `--app` and `--build-dir` ([esp32tl command](cli-user.md)).

For a PlatformIO project whose tests are in its `tests/` directory:

```python
import pytest

pytestmark = pytest.mark.app_path("..", build_dir=".pio/build/esp32")
```

```sh
pio run -e esp32
pytest tests/
esp32tl run --app . --build-dir .pio/build/esp32
```

A directory that holds `flasher_args.json` is read as an ESP-IDF build; otherwise one that holds
`firmware.bin` or `firmware.factory.bin` is read as a PlatformIO build. No other directory is
tried.

## What each kind of build needs

**An ESP-IDF build** needs `flasher_args.json`, every file it lists under `flash_files`, and
`config/sdkconfig.json` with `IDF_TARGET` `esp32` (without `config/sdkconfig.json`, the `chip` of
`extra_esptool_args` in `flasher_args.json`). It must not ask for flash encryption. The runner
gets every file of `flash_files` at its offset. The partition table is the file at the offset of
the `partition-table` entry, `0x8000` in an ESP-IDF build that keeps the default; the
application image is the `app` entry's file, and the application's ELF the first `.elf` file in
the directory.

**A PlatformIO build** for the classic ESP32 needs
`firmware.bin`, and then one of these:

- `firmware.factory.bin`, which pioarduino's builds have: the whole flash from `0x0`, with the
  bootloader at `0x1000`, the partition table at `0x8000`, the otadata at `0xe000` (in an Arduino
  build `boot_app0.bin`'s, which selects `ota_0`; in a `framework = espidf` build an erased
  `ota_data_initial.bin`), and the application. The runner gets this one file at `0x0`.
  It must hold `firmware.bin` where pioarduino puts it (below): that is the application a
  recording's `app_sha256` names.
- Otherwise `bootloader.bin` and `partitions.bin`. The runner gets `bootloader.bin` at `0x1000`,
  `partitions.bin` at `0x8000` and `firmware.bin` where pioarduino flashes it (below). The otadata
  stays erased, since `boot_app0.bin` is in the framework's package and not in the build
  directory.

`firmware.bin` goes where pioarduino's espressif32 platform flashes it: at the start of the
table's `ota_0`, or at `0x10000` in a table without one. There must be an application partition
there, as large as `firmware.bin` at least. On Arduino's default tables that is `app0`, the
partition the chip boots from with `boot_app0.bin`'s otadata. platformio's own espressif32
flashes it at the board's `upload.offset_address` instead, `0x10000` by default; esp32tl cannot
read that setting from the build directory, so a board that changes it is not followed.

Without `firmware.factory.bin`, on a table that also has a `factory` partition, a launcher's
kind of table, as Arduino's `app3M_fat9M_fact512k_16MB.csv` is, the bootloader finds the otadata
erased, tries `factory` first, finds nothing there and boots `ota_0`. The console then shows,
before the application starts, on a 2 MB table of that kind:

```
E (5) esp_image: image at 0x120000 has invalid magic byte (nothing flashed here?)
E (5) boot: Factory app partition is not bootable
I (32) boot: Loaded app from partition at offset 0x20000
```

with the offsets of the build's table. The otadata stays erased in that run: the bootloader
writes none after trying `factory`, so `esp_ota_get_boot_partition()` in the firmware, and
`snapshot.ota().boot_partition` in the test ([flash contents](flash-contents-user.md#read-the-flash-of-a-run)),
name `factory`, while the application runs from `ota_0`. On a board flashed by pioarduino both
name `ota_0`, from `boot_app0.bin`'s otadata. A test that relies on the boot partition runs
`firmware.factory.bin`, which holds that otadata. On a table without `factory` the bootloader
writes the otadata itself, selecting `ota_0`.

The runner's flash is 4 MB whatever the build and the board profile say
([esp32sim backend](esp32sim-backend-user.md#how-a-run-goes)): a table with partitions past
`0x400000`, as the 16 MB tables have, does not fit it.

The partition table is the one at `0x8000`, the application image `firmware.bin`, and the
application's ELF `firmware.elf` when it is there. The chip is read from the headers of
`firmware.bin` and of the bootloader: a build for another chip is refused with the name of the
chip it was built for. A build directory with `config/sdkconfig.json`, which ESP-IDF's build system
writes, is refused when it sets `SECURE_FLASH_ENC_ENABLED` or `SECURE_BOOT`; without that file,
neither is checked. pioarduino's ESP-IDF builder reads that file from the build directory, so a
`framework = espidf` build has it. An Arduino build has none, also with `custom_sdkconfig`: built
with `CONFIG_SECURE_FLASH_ENC_ENABLED=y` there (pioarduino 55.03.312, Arduino-ESP32 3.3.12), the
ESP-IDF builder compiled the framework's libraries and wrote the configuration to
`sdkconfig.esp32dev` in the project directory, not to the build directory, and esp32tl did not
refuse the build. Its `bootloader.bin`, which on the classic ESP32 is what turns flash encryption
on, was byte for byte the one of the same build without `custom_sdkconfig`: the framework's
precompiled bootloader.

What else comes from the build:

- `board.nvs(...)` and `board.flash()` read the partition table where the build flashes it
  ([flash contents](flash-contents-user.md)).
- A tap's `app_sha256` is the SHA-256 of the application image: the `app` entry's file of an
  ESP-IDF build, `firmware.bin` of a PlatformIO one ([taps](run-report-user.md)).
- The `bt` fixture needs the application's ELF ([esp32sim backend](esp32sim-backend-user.md#bluetooth)).

## When the build cannot be loaded

The test gets an error at setup and its body does not run; the message starts with the backend's
name, `esp32sim: ` or `serial: `. `esp32tl run` prints it after `esp32tl: ` and exits with code 2.
`{build}` is the selected build directory, `{backend}` the backend's name.

| Message | What to do |
|---|---|
| `{build} holds no ESP-IDF or PlatformIO build: no flasher_args.json and no firmware.bin` | Build the app (`make firmware` for this repository's firmware, `pio run` for a PlatformIO project), or fix the `app_path` marker and its `build_dir` keyword |
| `{build} is built for {chip}; {backend} runs the classic esp32` | Build for the classic ESP32. `{chip}` is the IDF target name, as `esp32s3`, or `an unknown chip (chip id {id})` for a PlatformIO image whose chip id esp32tl does not know |
| `{build} asks for flash encryption, which the runner does not do` | Build it without flash encryption. On serial: `which serial does not support` |
| `{build} asks for secure boot, which the runner does not do` | A PlatformIO build; build it without secure boot. On serial: `which serial does not support` |
| `{build}/config/sdkconfig.json is not JSON: {error}`, `{build}/config/sdkconfig.json is not a JSON object` | A PlatformIO build whose ESP-IDF configuration esp32tl cannot read; build again |
| `{build} is not a finished build, missing {files}` | Finish the build; `{files}` is a comma-separated list of paths |
| `{build}/firmware.bin holds no application image: no 0xE9 there` | `firmware.bin` does not start with an image header; build again |
| `{file} holds no bootloader at 0x1000: no 0xE9 there` | `{file}` is `firmware.factory.bin`, or `bootloader.bin` without it; build again, for the classic ESP32 |
| `{build}/firmware.factory.bin holds no partition table at 0x8000: no 0xAA50 there` | The factory image is not the whole flash from `0x0`; build again |
| `{file}: partition table: {reason}` | `{file}`, `partitions.bin` or `firmware.factory.bin`, holds no partition table esp32tl reads ([flash contents](flash-contents-technical.md#failure-modes) lists the reasons) |
| `{file}: firmware.bin goes at {offset}, where the table has no application partition` | `{file}` is `partitions.bin`, or `firmware.factory.bin`; the table has no `ota_0` and no application partition at `0x10000`. Such a build does not boot on a board either |
| `{build}/firmware.bin is {size} bytes, more than the {partition size} of the partition '{name}' at {offset}` | The application does not fit its partition; choose a larger partition table |
| `{build}/firmware.factory.bin does not hold firmware.bin at {offset}, in the partition '{name}': build again` | The factory image is of another build than `firmware.bin`; build again, or copy both from one build |

## See also

- [How builds are read](builds-technical.md)
- [esp32sim backend](esp32sim-backend-user.md)
- [Serial backend](serial-backend-user.md)
- [Flash contents](flash-contents-user.md)
