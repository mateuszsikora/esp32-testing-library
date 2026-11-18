# Builds: how they are read

## Data flow

1. `esp32tl.backends.esp32sim.load_app(app_path, *, build_dir=None, backend="esp32sim")` resolves
   the build directory, `Path(app_path) / build_dir` (`build` when `None`), to an absolute path and
   calls `esp32tl.build.load(app_path, directory, backend=backend, chip=chip)`, with the board
   profile's chip on the esp32sim backend and `esp32` on the serial one. A `BuildError` becomes a
   `BackendError` with the same message. `Esp32Sim.__init__`, `serial.check_artifact` and
   `esp32tl.cli` all load through it ([esp32sim backend](esp32sim-backend-technical.md#data-flow)).
2. `load` chooses the kind of build by its files, in this order: `flasher_args.json` is a file,
   `esp_idf`; `firmware.bin` or `firmware.factory.bin` is a file, `platformio`; otherwise
   `BuildError`.
3. **`esp_idf(app_path, directory, backend, chip)`** builds `IdfApp(app_path, build_dir=str(directory))`.
   The directory is absolute on purpose: given `build`, `IdfApp` falls back to a `build` directory
   in the working directory when the app has none. It refuses a target other than `chip`, the board
   profile's (`load`'s `chip`, `CLASSIC`, `esp32`, by default)
   (`IdfApp.target`: `IDF_TARGET` in `config/sdkconfig.json`; without that file,
   `extra_esptool_args.chip` in `flasher_args.json`; `esp32` when neither names one, so a build with
   neither passes the check), `flash_settings["encrypt"]` (any file marked `"encrypted": "true"`),
   and any of `IdfApp.flash_files` that is not a file. The `Build` takes `IdfApp.flash_files` in
   their order, sorted by offset; `partition_table` from the `offset` of the `partition-table`
   entry of `flasher_args.json`, read with `int(offset, 0)`; `app_image` the `file` of its `app`
   entry, relative to the directory; `elf` `IdfApp.elf_file`, the first `.elf` file that
   `os.listdir` gives; `chip` `IdfApp.target`.
4. **`platformio(directory, backend, chip)`** needs `firmware.bin`, and, without `firmware.factory.bin`,
   `bootloader.bin` and `partitions.bin`. `check_encryption` then reads `config/sdkconfig.json`
   (`SDKCONFIG`) when it is a file, and refuses a build that sets `SECURE_FLASH_ENC_ENABLED`, with
   the message `esp_idf` gives an encrypted build (`encryption`), or `SECURE_BOOT`, with
   `asks for secure boot`; a build without the file is not checked. It checks the header of
   `firmware.bin` next: it is at the start of the file on every chip, while an S3's or a C3's
   bootloader is at `0x0`, so such a build is refused by the name of its chip and not for a
   bootloader missing at `0x1000`. Then:
   - with `firmware.factory.bin`: one flash file, `firmware.factory.bin` at `0x0`;
   - without it: `app_partition` reads `partitions.bin` (its first `0xC00` bytes), and the flash
     files are `bootloader.bin` at the chip's bootloader offset (`BOOTLOADER_OFFSETS`: `0x1000` for
     `esp32` and `esp32s2`, `0x2000` for `esp32p4` and `esp32s31`, `0x0` for every other chip), `partitions.bin` at `0x8000` and
     `firmware.bin` at the
     offset of that partition. Nothing is written at the otadata's offset, which stays as the
     runner's flash starts, every byte `0xFF`: erased.

   **`app_partition(path, table, firmware)`** is where pioarduino flashes `firmware.bin`
   (`_parse_partitions` in its `builder/main.py`), and where its `merge-bin` puts it in
   `firmware.factory.bin`: the application partition (type 0) that starts at the offset of the
   table's first `ota_0` (subtype `0x10`), or at `APP_OFFSET`, `0x10000`, in a table without one.
   It refuses a table with no application partition at that offset, and a `firmware.bin` larger
   than the partition. On the flash the files make, read through `flash_bytes` (`Build.read`, or
   no bytes where no file holds the offset), `platformio` checks the bootloader's header at
   the chip's bootloader offset, then the table's magic `0xAA50` at `0x8000`, each with its own message. With
   `firmware.factory.bin` it then finds `app_partition` in the factory image's own table and
   compares the bytes there with the whole of `firmware.bin`. `partition_table` is `0x8000`,
   `app_image` `firmware.bin`, `elf` `firmware.elf` when it is a file, `chip` the board's.

   What the chip then boots, with the ESP-IDF bootloader (`bootloader_utility_load_boot_image`):
   with `boot_app0.bin`'s otadata, which an Arduino build's `firmware.factory.bin` holds at `0xe000`
   (an entry of `ota_seq` 1 and its CRC in the first sector), `ota_0`. With the otadata erased, as
   without `firmware.factory.bin` or in a `framework = espidf` build's, which holds an erased
   `ota_data_initial.bin` there, it starts from `factory` when the table has one, and, finding no
   image there, goes on to the OTA slots, `ota_0` first; in a table without `factory` it boots
   `ota_0`. Either way the application runs from the partition it runs from on a board flashed by
   pioarduino: for an Arduino build on any board, for a `framework = espidf` build on one whose
   `factory` partition holds no image, since its upload leaves `factory` as it was ([serial
   backend](serial-backend-user.md#builds-and-flashing)). The otadata differs in one case: without
   `firmware.factory.bin`, on a table with `factory`, the bootloader sets `ota_has_initial_contents`
   only on its way without `factory`, so it writes no otadata, and `esp_ota_get_boot_partition()`
   falls back to `factory`, where a board has `boot_app0.bin`'s `ota_0`. platformio's own
   espressif32 flashes `firmware.bin` at the board's `upload.offset_address`, `0x10000` by default,
   which is not in the build directory. pioarduino finds `ota_0` by the text of the CSV's subtype
   column, `ota_0`, and esp32tl by the subtype `0x10` in `partitions.bin`: a CSV that writes the
   subtype as `0x10` gets `firmware.bin` at `0x10000` from pioarduino and at `ota_0` from esp32tl.
5. The backend reads the `Build`: `runner_command` passes one `--flash-at {offset:#x}={path}` per
   flash file, in order; `Esp32Sim._partition_table` parses `Build.table()` with
   `flash.partitions`, for `board.nvs` and `board.flash`; `tap.app_image_sha256` hashes
   `app_image`; `Esp32Sim.bluetooth` passes `elf` to `--bt`. `serial.check_artifact` returns the
   `kind` with the hash, and for a `PLATFORMIO` build the plugin gives pytest-embedded
   `serial.PlatformioApp`, an `IdfApp` of the `Build`'s files, which `IdfSerial` flashes
   ([serial backend](serial-backend-technical.md#data-flow)).
6. **`header_flash(build)`** gives the esp32sim backend the flash size the build is for
   (`esp32sim.runner_flash_mb`, [esp32sim backend](esp32sim-backend-technical.md#data-flow)). It
   reads two image headers: the bootloader's, at the chip's bootloader offset (`BOOTLOADER_OFFSETS`)
   of the flash the files make, through `Build.file_at` and `flash_bytes`; and the application
   image's, the first 24 bytes of `app_image`. `flash_size` decodes each, and the larger is a
   `HeaderFlash(mb, path, what)`, the bootloader's when they are equal; a header that is not there,
   lacks the magic or gives no size of ESP-IDF's is left out, and with neither the result is `None`.
   ESP-IDF's bootloader sets the flash size from its own header and the application from its own
   with `CONFIG_SPI_FLASH_SIZE_OVERRIDE`, so either may be the one the application checks the chip's
   flash against. An `OSError` from reading becomes `BuildError`.

No file of the build directory is written, renamed or created.

## Schema

What `esp32tl.build` reads of the files:

| What | Where | Read as |
|---|---|---|
| Image header | The first 24 bytes of `firmware.bin` (PlatformIO) or of `app_image` (both kinds, for the flash size), and at the chip's bootloader offset of the flash (`BOOTLOADER_OFFSETS`: `0x1000` on `esp32` and `esp32s2`, `0x2000` on `esp32p4` and `esp32s31`, `0x0` on the others) for the bootloader; on the P4 also bytes 15 and 16, `min_chip_rev_full` (`p4_rev3`) | `esp_image_header_t`: byte 0 the magic, `0xE9`; bytes 12 and 13 the chip id, little-endian; the high four bits of byte 3 (`FLASH_SIZE_AT`) the flash size, `spi_size` |
| Flash size | High four bits of byte 3 of a header | ESP-IDF's `esp_image_flash_size_t`: `n` from 0 to 7 (`LARGEST_FLASH_SIZE`) is `1 << n` MB, 1 to 128; 8 and above give no size |
| Partition table | At `0x8000` of the flash, up to `0xC00` bytes (`TABLE_SIZE`) | Bytes 0 and 1 `0xAA 0x50`; then `flash.partitions` ([flash contents](flash-contents-technical.md#schema)) |
| Chip id | Bytes 12 and 13 of a header | ESP-IDF's `esp_chip_id_t`: `0` `esp32`, `2` `esp32s2`, `5` `esp32c3`, `9` `esp32s3`, `12` `esp32c2`, `13` `esp32c6`, `16` `esp32h2`, `18` `esp32p4`, `20` `esp32c61`, `23` `esp32c5`, `32` `esp32s31` (`CHIPS`); anything but the board's chip is refused |

The PlatformIO file names are constants of `esp32tl/build.py`: `FIRMWARE` (`firmware.bin`),
`FACTORY` (`firmware.factory.bin`), `BOOTLOADER` (`bootloader.bin`), `PARTITIONS`
(`partitions.bin`), `ELF` (`firmware.elf`); the offsets `BOOTLOADER_OFFSETS` (by chip) and
`TABLE_OFFSET` (`0x8000`).

`firmware.factory.bin`, as pioarduino 55.03.39 writes it for an Arduino build for the classic
ESP32, is the whole flash from `0x0`: `bootloader.bin` at `0x1000`, `partitions.bin` at `0x8000`,
the otadata of `boot_app0.bin` at `0xe000` and `firmware.bin` at `0x10000`, checked byte for byte
on one consumer's build (#192). For a `framework = espidf` build pioarduino's ESP-IDF builder puts
an erased `ota_data_initial.bin` of its own at the otadata instead (`frameworks/espidf.py`,
read in pioarduino 55.03.312, not on a build). `boot_app0.bin` is in the framework's package, not
in the build directory, which is why a build without `firmware.factory.bin` gets no otadata.

## Interfaces

| Name | What it is |
|---|---|
| `load(app_path, directory, *, backend="esp32sim", chip="esp32")` | The `Build` in `directory`, which must be absolute, for `chip`, the board profile's; `backend` names the backend in the errors, and an encrypted ESP-IDF build gets `which the runner does not do` for `esp32sim`, `which {backend} does not support` otherwise |
| `Build` | A frozen dataclass: `directory` (`Path`), `kind` (`ESP_IDF`, `"esp-idf"`, or `PLATFORMIO`, `"platformio"`), `flash_files` (a tuple of `FlashFile`, a later one over an earlier one where they overlap), `partition_table` (`int`, or `None` for an ESP-IDF build whose `flasher_args.json` names none), `app_image` (`Path` or `None`), `elf` (`Path` or `None`), `chip` (`str`) |
| `Build.read(offset, size)` | Up to `size` bytes of the flash at `offset`, from the last flash file that holds `offset`, to that file's end at most; `BuildError` when no file holds it, `OSError` when the file cannot be read |
| `Build.table()` | `read(partition_table, TABLE_SIZE)`; `BuildError` when `partition_table` is `None` |
| `Build.file_at(offset)` | The last flash file that holds `offset`, or `None`; `OSError` when a file's size cannot be read. `read` uses it |
| `header_flash(build)` | The largest flash size the bootloader's and the application's image headers give, as a `HeaderFlash`, or `None`; `BuildError` when a file cannot be read |
| `HeaderFlash(mb, path, what)` | A frozen dataclass: the size in MB, the file of the header, and `bootloader at {offset:#x}` or `application image` |
| `flash_size(data)` | The flash size in MB of the image header `data`, or `None` when `data` is shorter than 24 bytes, lacks the magic, or has a size code above 7 |
| `p4_rev3(build)` | Whether an ESP32-P4 build is for rev ≥ 3: the `min_chip_rev_full` (bytes 15 and 16, `MIN_REV_AT`) of the header at flash address `0x2000` (`flash_bytes`, so also inside `firmware.factory.bin` from `0x0`), or else of the first other flash file whose own header is a P4 image's, is 300 (`P4_REV3`) or more; `False` without a P4 image, as the runner reads it (`chip::esp32p4::rev3`). `BuildError` when a file cannot be read. The esp32sim backend gives the runner the ROM of that revision |
| `FlashFile(offset, path)` | A frozen dataclass: a file written into the flash at `offset` |
| `BuildError` | What keeps a build from loading or its table from being read |
| `esp_idf(app_path, directory, backend, chip)`, `platformio(directory, backend, chip)` | The two readers `load` chooses between, each checking the build against `chip` |
| `check_encryption(directory, backend)`, `encryption(directory, backend, what="flash encryption")` | The refusal of a PlatformIO build whose `config/sdkconfig.json` enables flash encryption or secure boot; the `BuildError` for a build that asks for `what` |
| `app_partition(path, table, firmware)` | The application partition of `table` at `ota_0`'s offset, else at `0x10000`; `BuildError` without one there, for a `firmware` larger than it, or for a table `flash.partitions` refuses |
| `header(path, offset)`, `check_image(path, data, what, directory, chip)`, `wrong_chip(directory, built, chip)`, `flash_bytes(build, offset, size)` | The 24 bytes of an image header in a file; the check of its magic and chip id against `chip`; the error for a build for another chip; `Build.read` with no bytes where no file holds the offset |

`Esp32Sim.app` and the result of `load_app` are a `Build`. pytest-embedded's own `dut.app` stays
its base `App` on esp32sim, which reads no file of the build.

## Failure modes

`load` raises `BuildError`; through `load_app` the test sees it as a `BackendError`, after
`esp32sim: ` or `serial: `.

| Message | Raised by | Cause |
|---|---|---|
| `{directory} holds no ESP-IDF or PlatformIO build: no flasher_args.json and no firmware.bin` | `load` | None of `flasher_args.json`, `firmware.bin` and `firmware.factory.bin` is a file |
| `{directory} is built for {target}; the board's chip is {chip}: build for {chip} or choose a board profile with that chip` | `esp_idf`, through `wrong_chip` | `IdfApp.target` is not the board's chip |
| `{directory} asks for flash encryption, which the runner does not do` | `esp_idf`, `check_encryption` | A flash file marked encrypted, or `SECURE_FLASH_ENC_ENABLED` in a PlatformIO build's `config/sdkconfig.json`; `which {backend} does not support` for another backend than `esp32sim` |
| `{directory} asks for secure boot, which the runner does not do` | `check_encryption` | `SECURE_BOOT` in a PlatformIO build's `config/sdkconfig.json`; `which {backend} does not support` for another backend than `esp32sim` |
| `{directory}/config/sdkconfig.json is not JSON: {error}` | `check_encryption` | The file is not UTF-8 JSON; `{error}` is the decoder's |
| `{directory}/config/sdkconfig.json is not a JSON object` | `check_encryption` | The JSON is an array, a string or a number |
| `{directory} is not a finished build, missing {paths}` | `esp_idf`, `platformio` | A file `flasher_args.json` lists, or a PlatformIO file the build needs, is not a file; `{paths}` comma-separated |
| `{directory} is built for {built}; the board's chip is {chip}: build for {chip} or choose a board profile with that chip` | `check_image`, through `wrong_chip` | A chip id other than the board chip's in the header of `firmware.bin` or of the bootloader; `{built}` from `CHIPS`, or `an unknown chip (chip id {id})` |
| `{directory}/firmware.bin holds no application image: no 0xE9 there` | `check_image` | `firmware.bin` shorter than 24 bytes, or without the magic |
| `{file} holds no bootloader at {offset}: no 0xE9 there` | `check_image` | No header at the chip's bootloader offset of the flash, `0x1000` on the classic ESP32: `{file}` is `firmware.factory.bin`, or `bootloader.bin` |
| `{directory}/firmware.factory.bin holds no partition table at 0x8000: no 0xAA50 there` | `platformio` | The factory image has no table at `0x8000`; a separate `partitions.bin` is checked by `flash.partitions` first |
| `{file}: {reason}` | `app_partition` | `flash.partitions` refuses the table of `partitions.bin` or of `firmware.factory.bin`, as `partition table: invalid magic 0x0 at 0x0` |
| `{file}: firmware.bin goes at {offset}, where the table has no application partition` | `app_partition` | No application partition starts at `ota_0`'s offset, or at `0x10000` without `ota_0`; `{file}` is `partitions.bin` or `firmware.factory.bin` |
| `{directory}/firmware.bin is {size} bytes, more than the {partition size} of the partition '{name}' at {offset}` | `app_partition` | `firmware.bin` is larger than its partition |
| `{directory}/firmware.factory.bin does not hold firmware.bin at {offset}, in the partition '{name}': build again` | `platformio` | The bytes of the factory image at that partition's offset are not those of `firmware.bin` |
| `{directory}/flasher_args.json names no partition table` | `Build.table` | An ESP-IDF build without a `partition-table` entry; `board.nvs` and `board.flash` prefix their name |
| `no file of {directory} is flashed at {offset}` | `Build.read` | No flash file holds the offset; `board.nvs` and `board.flash` prefix their name |
| `cannot read the image headers of {directory}: {error.strerror}` | `header_flash` | A flash file or `app_image` cannot be read; the esp32sim backend's refusals of the size the headers give are [its own](esp32sim-backend-technical.md#failure-modes) |

Anything else `IdfApp` raises, a `flasher_args.json` that is not JSON or has no `flash_files` for
instance, is not a `BuildError` and fails with its own traceback. A PlatformIO build without
`config/sdkconfig.json` is not checked for flash encryption or secure boot: nothing else in its
directory says.

## Tests

`tests/test_platformio_build.py` makes PlatformIO build directories of synthetic images, and of the
built `nvs_reader` and `hello_world` images:

- `test_a_build_without_a_factory_image_is_flashed_as_pioarduino_flashes_it`: the three files at
  `0x1000`, `0x8000` and `ota_0`'s offset, `0x20000` in its table, the runner's `--flash-at`
  arguments, the table, the application image, the ELF and the chip.
- `test_firmware_bin_goes_at_ota_0_as_pioarduino_flashes_it_even_with_a_factory_partition`: on
  Arduino's `app3M_fat9M_fact512k_16MB.csv` table, `firmware.bin` at `ota_0`'s `0x10000`, not in
  `factory`; `test_without_ota_0_firmware_bin_goes_at_0x10000`; both with and without a factory
  image.
- `test_where_firmware_bin_goes_must_be_an_application_partition_that_holds_it`: no application
  partition at `0x10000`, and an `ota_0` smaller than `firmware.bin`, with and without a factory
  image.
- `test_a_factory_image_must_hold_firmware_bin_where_pioarduino_put_it`: a `firmware.bin` of
  another build than the factory image is refused.
- `test_a_factory_image_is_flashed_whole_from_0x0`: one `--flash-at 0x0=…/firmware.factory.bin`,
  the table and the otadata of `boot_app0.bin` read from inside it, and the directory unchanged.
  The synthetic factory images hold that otadata (`BOOT_APP0`), as pioarduino's do.
- `test_the_application_image_s_hash_is_firmware_bin_s`, `test_without_firmware_elf_the_build_has_no_elf`.
- `test_an_image_for_another_chip_is_refused_by_its_name`: the S2, the C3, the S3 and an unknown
  chip id, with and without a factory image, the bootloader at `0x0`, on esp32sim and on serial;
  `test_a_bootloader_for_another_chip_is_refused`;
  `test_a_build_for_the_board_s_chip_loads_with_its_bootloader_at_0`: an S3, C3 or C6 build on
  that chip, its `bootloader.bin` at `0x0`.
- `test_what_is_not_an_image_or_a_table_where_the_flash_needs_one_is_refused`,
  `test_a_build_missing_what_it_flashes_is_unfinished`: the other messages.
- `test_an_esp_idf_build_beside_platformio_s_files_is_read_as_esp_idf`.
- `test_a_build_that_enables_flash_encryption_or_secure_boot_is_refused`: `SECURE_FLASH_ENC_ENABLED`
  and `SECURE_BOOT` in `config/sdkconfig.json`, on both backends, and the two messages of a file
  esp32tl cannot read.
- The serial backend's flashing of a PlatformIO build, listed in
  [its tests](serial-backend-technical.md#tests).
- `test_board_nvs_and_board_flash_find_the_table_of_either_build`: `board.nvs` and the table of
  `board.flash` on both kinds, and the test's NVS after the build's files on the command line.
- `test_nvs_reader_built_by_platformio_runs_as_it_is`: `nvs_reader` as a PlatformIO build, with and
  without `firmware.factory.bin`, selected by `@pytest.mark.app_path(".", build_dir=".pio/build/esp32")`,
  reads the test's NVS, `board.flash()` reads it back, and the directory's files are unchanged. Its
  table is ESP-IDF's, the application in `factory` at `0x10000`; one of Arduino's kind
  (`ARDUINO_TABLE`), without `factory`, the application in `ota_0` at `0x20000`; or a launcher's
  kind (`LAUNCHER_TABLE`), the application in `ota_0` at `0x20000` and an empty `factory` after it.
  The factory images hold `boot_app0.bin`'s otadata. The console shows `boot: Loaded app from
  partition at offset` the application's offset, and the chip boots once: from `ota_0` with that
  otadata, and with the otadata erased after the empty `factory` too. `snapshot.ota().boot_partition`
  is `app0` but in the launcher's table without a factory image, where it is `factory`.
- `test_esp32tl_run_runs_a_platformio_build`: `esp32tl run --build-dir .pio/build/esp32` prints
  `Hello world!`, and the directory's files are unchanged.

`tests/test_flash_size.py` covers the headers' flash size:
`test_an_image_header_gives_its_flash_size_in_the_high_bits_of_byte_3` every size code,
`test_a_header_that_gives_no_size_of_esp_idf_s_gives_none` a code of 8, no magic and a short header,
`test_the_larger_header_wins_and_the_bootloader_s_on_a_tie`,
`test_a_factory_image_gives_the_bootloader_s_header_in_it` and
`test_a_build_whose_images_have_no_header_gives_no_size`.

`tests/test_pytest_plugin.py` covers the ESP-IDF reader: `test_load_app_refuses_what_the_runner_cannot_run`,
`test_load_app_selects_only_the_explicit_build` and `test_selected_build_keeps_validation`;
`tests/test_flash.py` the table's errors on an ESP-IDF `Build`. No image built by PlatformIO itself
is in the suite: the directories are made from ESP-IDF images, so what pioarduino writes is known
from one consumer's build, not from a test.

## See also

- [User guide](builds-user.md)
- [esp32sim backend](esp32sim-backend-technical.md)
- [Flash contents](flash-contents-technical.md)
- [Decision: a PlatformIO build is read by its file names](../decisions/harness.md#a-platformio-build-is-read-by-its-file-names-and-image-headers)
