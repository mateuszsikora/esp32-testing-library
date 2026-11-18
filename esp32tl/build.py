"""The build a backend runs, in esp32tl's own terms: the files of the flash at their offsets, where
the partition table is, the application image, its ELF and the chip it is for.

`load` reads it from one of two kinds of build directory. An ESP-IDF build is what `idf.py build`
leaves, read through pytest-embedded-idf's `IdfApp`: its `flasher_args.json` gives the files and
their offsets, the `partition-table` and `app` entries, and `config/sdkconfig.json` the target. A
PlatformIO build is `.pio/build/<env>/`, which has no `flasher_args.json`: its files are read by
name, at the offsets pioarduino's espressif32 platform flashes them at, and the chip comes from
the images' own headers; whether it asks for flash encryption from its `config/sdkconfig.json`
when it has one. Nothing in either directory is written, renamed or converted.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from pytest_embedded_idf.app import IdfApp

from esp32tl import flash

ESP_IDF = "esp-idf"
PLATFORMIO = "platformio"

# The files of a PlatformIO build the backend reads, by name. firmware.factory.bin is the whole
# flash from 0x0, which pioarduino's builds have; boot_app0.bin, the otadata the platform flashes
# beside the others, is in the framework's package, not in the build.
FIRMWARE = "firmware.bin"
FACTORY = "firmware.factory.bin"
BOOTLOADER = "bootloader.bin"
PARTITIONS = "partitions.bin"
ELF = "firmware.elf"
# ESP-IDF's configuration as JSON, which ESP-IDF's build system writes into the build directory.
SDKCONFIG = "config/sdkconfig.json"

# Where the partition table is in the flash: ESP-IDF's default, and the offset the espressif32
# platform flashes partitions.bin at. The bootloader's offset is the chip's (BOOTLOADER_OFFSETS).
TABLE_OFFSET = 0x8000
# The longest partition table ESP-IDF writes, its MD5 record included.
TABLE_SIZE = 0xC00
TABLE_MAGIC = b"\xaa\x50"
# Where pioarduino flashes firmware.bin, and puts it in firmware.factory.bin: at the start of the
# table's ota_0 (type 0, app; subtype 0x10), or at 0x10000 in a table without one
# (_parse_partitions in its builder/main.py).
APP = 0
OTA_0_SUBTYPE = 0x10
APP_OFFSET = 0x10000

# An image header (esp_image_header_t): the magic byte, and the chip id at bytes 12 and 13 of its
# extended part, little-endian. The flash size is the high four bits of byte 3 (spi_size,
# esp_image_flash_size_t): 0 for 1 MB, 1 for 2 MB, and so on to 7 for 128 MB.
IMAGE_MAGIC = 0xE9
HEADER_SIZE = 24
CHIP_ID_AT = 12
FLASH_SIZE_AT = 3
LARGEST_FLASH_SIZE = 7
# ESP-IDF's esp_chip_id_t, by IDF target name.
CHIPS = {
    0: "esp32",
    2: "esp32s2",
    5: "esp32c3",
    9: "esp32s3",
    12: "esp32c2",
    13: "esp32c6",
    16: "esp32h2",
    18: "esp32p4",
    20: "esp32c61",
    23: "esp32c5",
    32: "esp32s31",
}
CLASSIC = "esp32"
# Where the second-stage bootloader sits in the flash: 0x1000 on the classic ESP32 and the S2,
# 0x2000 on the P4 and the S31, 0x0 on the others (ESP-IDF's CONFIG_BOOTLOADER_OFFSET_IN_FLASH).
BOOTLOADER_OFFSETS = {"esp32": 0x1000, "esp32s2": 0x1000, "esp32p4": 0x2000, "esp32s31": 0x2000}
# The lowest chip revision an image runs on, major * 100 + minor, little-endian at bytes 15 and 16
# of its header (min_chip_rev_full). An ESP32-P4 image for 3.0 or later runs on rev >= 3 only.
MIN_REV_AT = 15
P4_REV3 = 300


class BuildError(Exception):
    """What keeps a build from being run, said so that the reader knows what to do."""


@dataclass(frozen=True)
class FlashFile:
    """A file of the build, written into the flash at `offset`."""

    offset: int
    path: Path


@dataclass(frozen=True)
class Build:
    """A finished, unencrypted build for one of the chips esp32tl runs.

    `directory` is the build directory, absolute; `kind` is `ESP_IDF` or `PLATFORMIO`.
    `flash_files` go into the flash in order, a later one over an earlier one where they overlap.
    `partition_table` is the table's offset in the flash, `None` for an ESP-IDF build whose
    `flasher_args.json` names none. `app_image` is the application image whose SHA-256 a
    recording's `app_sha256` holds, `None` when the build names none; `elf` is the application's
    ELF, `None` when the build has none. `chip` is the IDF target name of the chip it is for.
    """

    directory: Path
    kind: str
    flash_files: tuple[FlashFile, ...]
    partition_table: int | None
    app_image: Path | None
    elf: Path | None
    chip: str

    def read(self, offset: int, size: int) -> bytes:
        """Up to `size` bytes of the flash at `offset`, from the last file that holds `offset`; to
        the end of that file at most. Raises `BuildError` when no file holds it, and `OSError` when
        the file cannot be read."""
        file = self.file_at(offset)
        if file is None:
            raise BuildError(f"no file of {self.directory} is flashed at {offset:#x}")
        with file.path.open("rb") as image:
            image.seek(offset - file.offset)
            return image.read(size)

    def file_at(self, offset: int) -> FlashFile | None:
        """The last file of the flash that holds `offset`, or `None`. Raises `OSError` when a file
        cannot be read."""
        for file in reversed(self.flash_files):
            if file.offset <= offset < file.offset + file.path.stat().st_size:
                return file
        return None

    def table(self) -> bytes:
        """The partition table's bytes, as the build flashes them."""
        if self.partition_table is None:
            raise BuildError(f"{self.directory}/flasher_args.json names no partition table")
        return self.read(self.partition_table, TABLE_SIZE)


def load(
    app_path: str, directory: Path, *, backend: str = "esp32sim", chip: str = CLASSIC
) -> Build:
    """The build in `directory`, absolute, of the application in `app_path`: ESP-IDF's when it
    holds `flasher_args.json`, else PlatformIO's when it holds `firmware.bin` or
    `firmware.factory.bin`. `backend` names the backend in the errors."""
    if (directory / "flasher_args.json").is_file():
        return esp_idf(app_path, directory, backend, chip)
    if (directory / FIRMWARE).is_file() or (directory / FACTORY).is_file():
        return platformio(directory, backend, chip)
    raise BuildError(
        f"{directory} holds no ESP-IDF or PlatformIO build: no flasher_args.json and no {FIRMWARE}"
    )


def esp_idf(app_path: str, directory: Path, backend: str, chip: str) -> Build:
    """An ESP-IDF build, through `IdfApp`. The directory is given as an absolute path: given
    `build`, `IdfApp` would fall back to a `build` directory in the working directory."""
    app = IdfApp(app_path, build_dir=str(directory))
    if app.target != chip:
        raise wrong_chip(directory, app.target, chip)
    if app.flash_settings.get("encrypt"):
        raise encryption(directory, backend)
    missing = [f.file_path for f in app.flash_files if not Path(f.file_path).is_file()]
    if missing:
        raise BuildError(f"{directory} is not a finished build, missing {', '.join(missing)}")
    image = app.flash_args.get("app", {}).get("file")
    table = app.flash_args.get("partition-table", {}).get("offset")
    return Build(
        directory=directory,
        kind=ESP_IDF,
        flash_files=tuple(FlashFile(f.offset, Path(f.file_path)) for f in app.flash_files),
        partition_table=int(table, 0) if table is not None else None,
        app_image=directory / image if image else None,
        elf=Path(app.elf_file) if app.elf_file else None,
        chip=app.target,
    )


def platformio(directory: Path, backend: str, chip: str) -> Build:
    """A PlatformIO build: `firmware.factory.bin` at 0x0 when it is there; else `bootloader.bin` at
    0x1000, `partitions.bin` at 0x8000 and `firmware.bin` where pioarduino flashes it
    (`app_partition`), with the otadata left erased."""
    firmware, factory = directory / FIRMWARE, directory / FACTORY
    if factory.is_file():
        needed = [firmware]
    else:
        needed = [directory / BOOTLOADER, directory / PARTITIONS, firmware]
    missing = [str(path) for path in needed if not path.is_file()]
    if missing:
        raise BuildError(f"{directory} is not a finished build, missing {', '.join(missing)}")
    check_encryption(directory, backend)
    # The application's header first: it is at the start of firmware.bin for every chip, while an
    # S3's or a C3's bootloader is at 0x0, so this names the chip rather than a bootloader missing
    # at 0x1000.
    check_image(firmware, header(firmware, 0), "application image", directory, chip)
    bootloader_offset = BOOTLOADER_OFFSETS.get(chip, 0)
    if factory.is_file():
        files: tuple[FlashFile, ...] = (FlashFile(0, factory),)
        table_file = factory
    else:
        table_file = directory / PARTITIONS
        app = app_partition(table_file, table_file.read_bytes()[:TABLE_SIZE], firmware)
        files = (
            FlashFile(bootloader_offset, directory / BOOTLOADER),
            FlashFile(TABLE_OFFSET, table_file),
            FlashFile(app.offset, firmware),
        )
    elf = directory / ELF
    build = Build(
        directory=directory,
        kind=PLATFORMIO,
        flash_files=files,
        partition_table=TABLE_OFFSET,
        app_image=firmware,
        elf=elf if elf.is_file() else None,
        chip=chip,
    )
    bootloader = files[0].path if factory.is_file() else directory / BOOTLOADER
    check_image(
        bootloader,
        flash_bytes(build, bootloader_offset, HEADER_SIZE),
        f"bootloader at {bootloader_offset:#x}",
        directory,
        chip,
    )
    if flash_bytes(build, TABLE_OFFSET, len(TABLE_MAGIC)) != TABLE_MAGIC:
        raise BuildError(
            f"{table_file} holds no partition table at {TABLE_OFFSET:#x}: no 0xAA50 there"
        )
    if factory.is_file():
        # firmware.bin is what a recording's app_sha256 names: the factory image must hold it
        # where pioarduino's merge put it, or the run would be of another application than that.
        app = app_partition(factory, build.table(), firmware)
        image = firmware.read_bytes()
        if flash_bytes(build, app.offset, len(image)) != image:
            raise BuildError(
                f"{factory} does not hold {FIRMWARE} at {app.offset:#x}, in the partition"
                f" {app.name!r}: build again"
            )
    return build


@dataclass(frozen=True)
class HeaderFlash:
    """The flash size, in MB, that the image header of `what` in the file at `path` gives."""

    mb: int
    path: Path
    what: str


def header_flash(build: Build) -> HeaderFlash | None:
    """The largest flash size that the build's image headers give, with the header it is in; `None`
    when the build has neither header. Two headers are read. The second-stage bootloader's, at the
    chip's offset in the flash as the build writes it: the bootloader sets the flash size from it,
    and the application's spi_flash refuses to start on a chip whose flash is smaller. The
    application image's: with `CONFIG_SPI_FLASH_SIZE_OVERRIDE` the application sets the size
    from its own header instead. A header whose size field is not one of ESP-IDF's sizes is left
    out."""
    found: list[HeaderFlash] = []
    offset = BOOTLOADER_OFFSETS.get(build.chip, 0)
    try:
        file = build.file_at(offset)
        if file is not None:
            mb = flash_size(flash_bytes(build, offset, HEADER_SIZE))
            if mb is not None:
                found.append(HeaderFlash(mb, file.path, f"bootloader at {offset:#x}"))
        if build.app_image is not None and build.app_image.is_file():
            mb = flash_size(header(build.app_image, 0))
            if mb is not None:
                found.append(HeaderFlash(mb, build.app_image, "application image"))
    except OSError as error:
        raise BuildError(
            f"cannot read the image headers of {build.directory}: {error.strerror}"
        ) from error
    # The bootloader's first when both give the same size: it is the one that sets it by default.
    return max(found, key=lambda h: h.mb, default=None)


def p4_rev3(build: Build) -> bool:
    """Whether an ESP32-P4 build is for rev >= 3, by the runner's rule (`chip::esp32p4::rev3`),
    which takes the silicon revision from it: the header at the bootloader's offset in the flash,
    0x2000, as the last file that holds it writes it there (a bootloader of its own, or the one
    inside PlatformIO's `firmware.factory.bin` from 0x0), when it is a P4 image; else the first
    other file whose own header is one. A build without a P4 image counts as rev < 3, as there.
    Raises `BuildError` when a file cannot be read."""
    offset = BOOTLOADER_OFFSETS["esp32p4"]
    try:
        headers = [flash_bytes(build, offset, HEADER_SIZE)]
        headers += [header(f.path, 0) for f in build.flash_files if f.offset != offset]
    except OSError as error:
        raise BuildError(
            f"cannot read the image headers of {build.directory}: {error.strerror}"
        ) from error
    for data in headers:
        if (
            len(data) == HEADER_SIZE
            and data[0] == IMAGE_MAGIC
            and CHIPS.get(int.from_bytes(data[CHIP_ID_AT : CHIP_ID_AT + 2], "little")) == "esp32p4"
        ):
            return int.from_bytes(data[MIN_REV_AT : MIN_REV_AT + 2], "little") >= P4_REV3
    return False


def flash_size(data: bytes) -> int | None:
    """The flash size, in MB, that the image header `data` gives; `None` when `data` is no image
    header or its size field is none of ESP-IDF's."""
    if len(data) < HEADER_SIZE or data[0] != IMAGE_MAGIC:
        return None
    code = data[FLASH_SIZE_AT] >> 4
    return 1 << code if code <= LARGEST_FLASH_SIZE else None


def encryption(directory: Path, backend: str, what: str = "flash encryption") -> BuildError:
    """The refusal of a build in `directory` that asks for `what`."""
    reason = "the runner does not do" if backend == "esp32sim" else f"{backend} does not support"
    return BuildError(f"{directory} asks for {what}, which {reason}")


def check_encryption(directory: Path, backend: str) -> None:
    """Refuses a PlatformIO build whose `config/sdkconfig.json` enables flash encryption or
    secure boot: its bootloader would burn a board's eFuses on the first boot, and with secure
    boot IdfSerial would flash it with esptool's `--force`. A build without that file is not
    checked."""
    config = directory / SDKCONFIG
    if not config.is_file():
        return
    try:
        settings = json.loads(config.read_text())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BuildError(f"{config} is not JSON: {error}") from error
    if not isinstance(settings, dict):
        raise BuildError(f"{config} is not a JSON object")
    if settings.get("SECURE_FLASH_ENC_ENABLED"):
        raise encryption(directory, backend)
    if settings.get("SECURE_BOOT"):
        raise encryption(directory, backend, "secure boot")


def app_partition(path: Path, table: bytes, firmware: Path) -> flash.Partition:
    """The application partition of `table`, read from the file at `path`, that `firmware` goes
    in: the one at the start of the table's first `ota_0`, or at 0x10000 without one, where
    pioarduino flashes it. With the otadata `boot_app0.bin` writes, the bootloader boots `ota_0`;
    with it erased, it tries `factory` first and goes on to the OTA slots when that holds no
    image. The partition must hold the whole of `firmware`."""
    try:
        found = flash.partitions(table)
    except ValueError as error:
        raise BuildError(f"{path}: {error}") from error
    apps = [p for p in found.values() if p.type == APP]
    ota_0 = next((p for p in apps if p.subtype == OTA_0_SUBTYPE), None)
    offset = ota_0.offset if ota_0 is not None else APP_OFFSET
    app = next((p for p in apps if p.offset == offset), None)
    if app is None:
        raise BuildError(
            f"{path}: {FIRMWARE} goes at {offset:#x}, where the table has no application partition"
        )
    size = firmware.stat().st_size
    if size > app.size:
        raise BuildError(
            f"{firmware} is {size} bytes, more than the {app.size} of the partition {app.name!r}"
            f" at {app.offset:#x}"
        )
    return app


def flash_bytes(build: Build, offset: int, size: int) -> bytes:
    """`build.read(offset, size)`, or no bytes when no file of the build holds `offset`."""
    try:
        return build.read(offset, size)
    except BuildError:
        return b""


def header(path: Path, offset: int) -> bytes:
    """The bytes of an image header at `offset` of the file at `path`."""
    with path.open("rb") as image:
        image.seek(offset)
        return image.read(HEADER_SIZE)


def check_image(path: Path, data: bytes, what: str, directory: Path, chip: str) -> None:
    """Refuses `data`, which should be the header of `what` in the file at `path`, when it is
    none, or one for another chip than `chip`."""
    if len(data) < HEADER_SIZE or data[0] != IMAGE_MAGIC:
        raise BuildError(f"{path} holds no {what}: no 0xE9 there")
    chip_id = int.from_bytes(data[CHIP_ID_AT : CHIP_ID_AT + 2], "little")
    built = CHIPS.get(chip_id, f"an unknown chip (chip id {chip_id})")
    if built != chip:
        raise wrong_chip(directory, built, chip)


def wrong_chip(directory: Path, built: str, chip: str) -> BuildError:
    """A build for `built`, an IDF target name or what stands for an unknown one, run on a board
    whose chip is `chip`."""
    return BuildError(
        f"{directory} is built for {built}; the board's chip is {chip}: build for {chip}"
        " or choose a board profile with that chip"
    )
