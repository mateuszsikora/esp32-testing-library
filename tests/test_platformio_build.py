"""A PlatformIO build directory, `.pio/build/<env>/`, loaded as it is: its files at the offsets
the espressif32 platform flashes them at, or `firmware.factory.bin` at 0x0, the partition table
where the build flashes it, the chip from the images' headers. The first tests make the directory
from synthetic images and need nothing beyond the package; the runs make it from the built
`nvs_reader` and `hello_world` images and need `make firmware` (nvs_reader, hello_world)."""

import gc
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path
from unittest.mock import Mock

import esptool
import pytest
from pytest_embedded.dut import Dut
from pytest_embedded.log import MessageQueue
from pytest_embedded.utils import Meta
from pytest_embedded_idf.serial import IdfSerial
from test_pytest_plugin import fake_build
from test_serial_backend import FAKE_SERIAL, run_serial

from esp32tl import boards, build, flash, recording
from esp32tl.backends import esp32sim
from esp32tl.backends.esp32sim import BackendError, Esp32Sim
from esp32tl.backends.serial import PlatformioApp, check_artifact
from esp32tl.build import FlashFile
from esp32tl.tap import Tap, app_image_sha256

ROOT = Path(__file__).resolve().parent.parent
NVS_READER = ROOT / "firmware" / "nvs_reader"
HELLO_WORLD = ROOT / "firmware" / "hello_world"

ERASED = 0xFF

# The esp32tl command, as its entry point runs it.
COMMAND = "import sys; from esp32tl import cli; sys.exit(cli.main(sys.argv[1:]))"


def image(chip_id: int = 0, size: int = 256) -> bytes:
    """An image header (esp_image_header_t) for the chip `chip_id`, padded to `size`."""
    header = bytes([0xE9, 1, 2, 0x20]) + (0x40080000).to_bytes(4, "little")
    extended = bytes([0xEE, 0, 0, 0]) + chip_id.to_bytes(2, "little") + bytes(10)
    return (header + extended).ljust(size, b"\0")


def entry(name: str, kind: int, subtype: int, offset: int, size: int) -> bytes:
    return flash.ENTRY.pack(flash.ENTRY_MAGIC, kind, subtype, offset, size, name.encode(), 0)


# Arduino-ESP32's default table but for the applications' offsets: the first one is not at 0x10000,
# so that its offset can only come from the table.
TABLE = (
    entry("nvs", 1, 2, 0x9000, 0x5000)
    + entry("otadata", 1, 0, 0xE000, 0x2000)
    + entry("app0", 0, 0x10, 0x20000, 0x140000)
    + entry("app1", 0, 0x11, 0x160000, 0x140000)
    + entry("spiffs", 1, 0x82, 0x2A0000, 0x160000)
).ljust(build.TABLE_SIZE, b"\xff")


# boot_app0.bin, the otadata pioarduino puts in firmware.factory.bin at 0xe000: in its first
# sector the entry of ota_seq 1, which selects ota_0, with its CRC.
BOOT_APP0 = (bytes.fromhex("01000000") + b"\xff" * 24 + bytes.fromhex("9a984347")).ljust(
    0x1000, b"\xff"
) + bytes(4).ljust(0x1000, b"\xff")


def merged(files: dict[int, bytes]) -> bytes:
    """What the platform's merge_bin makes of `files`: one image from 0x0, erased between them."""
    end = max(offset + len(data) for offset, data in files.items())
    contents = bytearray([ERASED]) * end
    for offset, data in files.items():
        contents[offset : offset + len(data)] = data
    return bytes(contents)


def pio_build(
    directory: Path,
    *,
    factory: bool = False,
    chip_id: int = 0,
    table: bytes = TABLE,
    bootloader_at: int = 0x1000,
    app_at: int = 0x20000,
    app_size: int = 1024,
) -> Path:
    """A PlatformIO build directory of synthetic images, with `firmware.factory.bin` if
    `factory`, as pioarduino's builds have it: the application at `app_at`, `TABLE`'s ota_0."""
    directory.mkdir(parents=True, exist_ok=True)
    bootloader, app = image(chip_id), image(chip_id, app_size)
    (directory / "bootloader.bin").write_bytes(bootloader)
    (directory / "partitions.bin").write_bytes(table)
    (directory / "firmware.bin").write_bytes(app)
    (directory / "firmware.elf").write_bytes(b"\x7fELF")
    if factory:
        layout = {bootloader_at: bootloader, 0x8000: table, 0xE000: BOOT_APP0, app_at: app}
        (directory / "firmware.factory.bin").write_bytes(merged(layout))
    return directory.resolve()


def flash_at(command: list[str]) -> list[str]:
    return [command[i + 1] for i, value in enumerate(command) if value == "--flash-at"]


def test_a_build_without_a_factory_image_is_flashed_as_pioarduino_flashes_it(
    tmp_path: Path,
) -> None:
    directory = pio_build(tmp_path / ".pio/build/esp32")
    app = esp32sim.load_app(str(tmp_path), build_dir=".pio/build/esp32")
    assert app.kind == build.PLATFORMIO
    assert app.directory == directory
    # firmware.bin in ota_0, which the bootloader boots in a table without factory; otadata erased
    assert app.flash_files == (
        FlashFile(0x1000, directory / "bootloader.bin"),
        FlashFile(0x8000, directory / "partitions.bin"),
        FlashFile(0x20000, directory / "firmware.bin"),
    )
    assert app.partition_table == 0x8000
    assert app.app_image == directory / "firmware.bin"
    assert app.elf == directory / "firmware.elf"
    assert app.chip == "esp32"
    assert flash.partitions(app.table()) == flash.partitions(TABLE)
    command = esp32sim.runner_command(Path("runner"), Path("rom"), app, 1, Path("link"))
    assert flash_at(command) == [
        f"0x1000={directory}/bootloader.bin",
        f"0x8000={directory}/partitions.bin",
        f"0x20000={directory}/firmware.bin",
    ]


def test_a_factory_image_is_flashed_whole_from_0x0(tmp_path: Path) -> None:
    directory = pio_build(tmp_path / "env", factory=True)
    files = {path.name: path.read_bytes() for path in directory.iterdir()}
    app = esp32sim.load_app(str(tmp_path), build_dir="env")
    assert app.flash_files == (FlashFile(0, directory / "firmware.factory.bin"),)
    # the table is the one inside the factory image, at 0x8000
    assert flash.partitions(app.table()) == flash.partitions(TABLE)
    # the otadata of boot_app0.bin, ota_seq 1, which selects ota_0
    assert app.read(0xE000, 4) == bytes.fromhex("01000000")
    assert app.app_image == directory / "firmware.bin"
    command = esp32sim.runner_command(Path("runner"), Path("rom"), app, 1, Path("link"))
    assert flash_at(command) == [f"0x0={directory}/firmware.factory.bin"]
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == files


# Arduino-ESP32's app3M_fat9M_fact512k_16MB.csv: the application in ota_0, a launcher's factory
# partition of 512 KiB at the end, both kinds the bootloader can boot.
FACT512K = (
    entry("nvs", 1, 2, 0x9000, 0x5000)
    + entry("otadata", 1, 0, 0xE000, 0x2000)
    + entry("app0", 0, 0x10, 0x10000, 0x300000)
    + entry("app1", 0, 0x11, 0x310000, 0x300000)
    + entry("ffat", 1, 0x81, 0x610000, 0x960000)
    + entry("factory", 0, 0x00, 0xF70000, 0x80000)
    + entry("coredump", 1, 3, 0xFF0000, 0x10000)
).ljust(build.TABLE_SIZE, b"\xff")


@pytest.mark.parametrize("factory", [False, True])
def test_firmware_bin_goes_at_ota_0_as_pioarduino_flashes_it_even_with_a_factory_partition(
    tmp_path: Path, factory: bool
) -> None:
    directory = pio_build(tmp_path, table=FACT512K, factory=factory, app_at=0x10000)
    app = esp32sim.load_app(str(tmp_path), build_dir=directory)
    if factory:
        assert app.flash_files == (FlashFile(0, directory / "firmware.factory.bin"),)
        assert app.read(0x10000, 1024) == (directory / "firmware.bin").read_bytes()
    else:
        assert app.flash_files[-1] == FlashFile(0x10000, directory / "firmware.bin")


@pytest.mark.parametrize("factory", [False, True])
def test_without_ota_0_firmware_bin_goes_at_0x10000(tmp_path: Path, factory: bool) -> None:
    table = (
        entry("nvs", 1, 2, 0x9000, 0x6000)
        + entry("phy_init", 1, 1, 0xF000, 0x1000)
        + entry("factory", 0, 0x00, 0x10000, 0x100000)
    ).ljust(build.TABLE_SIZE, b"\xff")
    directory = pio_build(tmp_path, table=table, factory=factory, app_at=0x10000)
    app = esp32sim.load_app(str(tmp_path), build_dir=directory)
    if factory:
        assert app.flash_files == (FlashFile(0, directory / "firmware.factory.bin"),)
        assert app.read(0x10000, 1024) == (directory / "firmware.bin").read_bytes()
    else:
        assert app.flash_files[-1] == FlashFile(0x10000, directory / "firmware.bin")


@pytest.mark.parametrize("factory", [False, True])
def test_where_firmware_bin_goes_must_be_an_application_partition_that_holds_it(
    tmp_path: Path, factory: bool
) -> None:
    where = "firmware.factory.bin" if factory else "partitions.bin"
    # no ota_0, and the factory partition not at 0x10000
    table = (entry("nvs", 1, 2, 0x9000, 0x5000) + entry("factory", 0, 0, 0x20000, 0x100000)).ljust(
        build.TABLE_SIZE, b"\xff"
    )
    directory = pio_build(tmp_path / "offset", table=table, factory=factory)
    with pytest.raises(BackendError) as refused:
        esp32sim.load_app(str(directory), build_dir=directory)
    assert str(refused.value) == (
        f"{directory}/{where}: firmware.bin goes at 0x10000, where the table has no application"
        " partition"
    )
    # ota_0 smaller than firmware.bin
    table = (entry("nvs", 1, 2, 0x9000, 0x5000) + entry("app0", 0, 0x10, 0x20000, 0x1000)).ljust(
        build.TABLE_SIZE, b"\xff"
    )
    directory = pio_build(tmp_path / "size", table=table, factory=factory, app_size=0x1001)
    with pytest.raises(BackendError) as refused:
        esp32sim.load_app(str(directory), build_dir=directory)
    assert str(refused.value) == (
        f"{directory}/firmware.bin is 4097 bytes, more than the 4096 of the partition 'app0' at"
        " 0x20000"
    )


def test_a_factory_image_must_hold_firmware_bin_where_pioarduino_put_it(tmp_path: Path) -> None:
    directory = pio_build(tmp_path, factory=True)
    # a firmware.bin from another build than the factory image's
    (directory / "firmware.bin").write_bytes(image(0, 1024)[:-1] + b"\x01")
    with pytest.raises(BackendError) as refused:
        esp32sim.load_app(str(tmp_path), build_dir=directory)
    assert str(refused.value) == (
        f"{directory}/firmware.factory.bin does not hold firmware.bin at 0x20000, in the"
        " partition 'app0': build again"
    )


def test_the_application_image_s_hash_is_firmware_bin_s(tmp_path: Path) -> None:
    for factory in (False, True):
        directory = pio_build(tmp_path / str(factory), factory=factory)
        app = esp32sim.load_app(str(directory), build_dir=directory)
        digest = hashlib.sha256((directory / "firmware.bin").read_bytes()).hexdigest()
        assert app_image_sha256(app) == digest


def test_without_firmware_elf_the_build_has_no_elf(tmp_path: Path) -> None:
    directory = pio_build(tmp_path)
    (directory / "firmware.elf").unlink()
    assert esp32sim.load_app(str(tmp_path), build_dir=directory).elf is None


@pytest.mark.parametrize("factory", [False, True])
@pytest.mark.parametrize(
    "chip_id,chip", [(2, "esp32s2"), (5, "esp32c3"), (9, "esp32s3"), (99, "an unknown chip")]
)
def test_an_image_for_another_chip_is_refused_by_its_name(
    tmp_path: Path, factory: bool, chip_id: int, chip: str
) -> None:
    # The S3's and the C3's bootloader is at 0x0: the application's header names the chip all the
    # same, since it is at the start of firmware.bin on every chip.
    directory = pio_build(tmp_path, factory=factory, chip_id=chip_id, bootloader_at=0)
    with pytest.raises(BackendError) as refused:
        esp32sim.load_app(str(tmp_path), build_dir=directory)
    assert str(refused.value).startswith(f"{directory} is built for {chip}")
    assert str(refused.value).endswith("; esp32sim runs the classic esp32")
    with pytest.raises(BackendError, match=f"is built for {chip}.*; serial runs the classic"):
        check_artifact(str(tmp_path), build_dir=directory)


def test_a_bootloader_for_another_chip_is_refused(tmp_path: Path) -> None:
    directory = pio_build(tmp_path)
    (directory / "bootloader.bin").write_bytes(image(9))
    with pytest.raises(BackendError, match="is built for esp32s3; esp32sim runs the classic esp32"):
        esp32sim.load_app(str(tmp_path), build_dir=directory)


def test_what_is_not_an_image_or_a_table_where_the_flash_needs_one_is_refused(
    tmp_path: Path,
) -> None:
    directory = pio_build(tmp_path / "app")
    (directory / "firmware.bin").write_bytes(b"\0" * 64)
    with pytest.raises(BackendError) as refused:
        esp32sim.load_app(str(directory), build_dir=directory)
    assert (
        str(refused.value) == f"{directory}/firmware.bin holds no application image: no 0xE9 there"
    )

    directory = pio_build(tmp_path / "bootloader")
    (directory / "bootloader.bin").write_bytes(b"\0" * 64)
    with pytest.raises(BackendError) as refused:
        esp32sim.load_app(str(directory), build_dir=directory)
    assert str(refused.value) == (
        f"{directory}/bootloader.bin holds no bootloader at 0x1000: no 0xE9 there"
    )

    directory = pio_build(tmp_path / "factory", factory=True, bootloader_at=0)
    with pytest.raises(BackendError) as refused:
        esp32sim.load_app(str(directory), build_dir=directory)
    assert str(refused.value) == (
        f"{directory}/firmware.factory.bin holds no bootloader at 0x1000: no 0xE9 there"
    )

    directory = pio_build(tmp_path / "table", factory=True, table=b"\xff" * 0xC00)
    with pytest.raises(BackendError) as refused:
        esp32sim.load_app(str(directory), build_dir=directory)
    assert str(refused.value) == (
        f"{directory}/firmware.factory.bin holds no partition table at 0x8000: no 0xAA50 there"
    )

    directory = pio_build(tmp_path / "partitions")
    (directory / "partitions.bin").write_bytes(b"\0" * 0xC00)
    with pytest.raises(BackendError, match=r"partitions\.bin: partition table: invalid magic 0x0"):
        esp32sim.load_app(str(directory), build_dir=directory)

    # a factory image that ends before the table: the bootloader in it is fine
    directory = pio_build(tmp_path / "short", factory=True)
    factory_image = directory / "firmware.factory.bin"
    factory_image.write_bytes(factory_image.read_bytes()[:0x2000])
    with pytest.raises(BackendError) as refused:
        esp32sim.load_app(str(directory), build_dir=directory)
    assert str(refused.value) == (
        f"{factory_image} holds no partition table at 0x8000: no 0xAA50 there"
    )


def test_a_build_missing_what_it_flashes_is_unfinished(tmp_path: Path) -> None:
    directory = pio_build(tmp_path / "split")
    (directory / "bootloader.bin").unlink()
    with pytest.raises(BackendError) as refused:
        esp32sim.load_app(str(directory), build_dir=directory)
    assert str(refused.value) == (
        f"{directory} is not a finished build, missing {directory}/bootloader.bin"
    )
    # with the factory image, firmware.bin is still needed: a recording's app_sha256 is its hash
    directory = pio_build(tmp_path / "factory", factory=True)
    (directory / "firmware.bin").unlink()
    (directory / "bootloader.bin").unlink()
    with pytest.raises(BackendError) as refused:
        esp32sim.load_app(str(directory), build_dir=directory)
    assert str(refused.value) == (
        f"{directory} is not a finished build, missing {directory}/firmware.bin"
    )


def test_an_esp_idf_build_beside_platformio_s_files_is_read_as_esp_idf(tmp_path: Path) -> None:
    directory = fake_build(tmp_path)
    pio_build(directory)
    app = esp32sim.load_app(str(tmp_path))
    assert app.kind == build.ESP_IDF
    assert [f.path.name for f in app.flash_files] == [
        "bootloader.bin",
        "partition-table.bin",
        "app.bin",
    ]
    assert app.app_image == directory.resolve() / "app.bin"
    assert app.partition_table == 0x8000


def test_a_build_that_enables_flash_encryption_or_secure_boot_is_refused(tmp_path: Path) -> None:
    directory = pio_build(tmp_path / "encrypted", factory=True)
    (directory / "config").mkdir()
    (directory / "config/sdkconfig.json").write_text('{"SECURE_FLASH_ENC_ENABLED": true}')
    with pytest.raises(BackendError) as refused:
        esp32sim.load_app(str(directory), build_dir=directory)
    assert str(refused.value) == (
        f"{directory} asks for flash encryption, which the runner does not do"
    )
    with pytest.raises(BackendError) as refused:
        check_artifact(str(directory), build_dir=directory)
    assert str(refused.value) == (
        f"{directory} asks for flash encryption, which serial does not support"
    )
    # ESP-IDF's configuration without it, or none at all, is not a refusal
    (directory / "config/sdkconfig.json").write_text('{"SECURE_FLASH_ENC_ENABLED": false}')
    assert check_artifact(str(directory), build_dir=directory).kind == build.PLATFORMIO
    (directory / "config/sdkconfig.json").write_text('{"SECURE_BOOT": true}')
    with pytest.raises(BackendError) as refused:
        check_artifact(str(directory), build_dir=directory)
    assert str(refused.value) == f"{directory} asks for secure boot, which serial does not support"
    with pytest.raises(BackendError, match=r"asks for secure boot, which the runner does not do$"):
        esp32sim.load_app(str(directory), build_dir=directory)
    (directory / "config/sdkconfig.json").write_text("not json")
    with pytest.raises(BackendError, match=r"config/sdkconfig\.json is not JSON: "):
        check_artifact(str(directory), build_dir=directory)
    (directory / "config/sdkconfig.json").write_text("[]")
    with pytest.raises(BackendError, match=r"config/sdkconfig\.json is not a JSON object$"):
        check_artifact(str(directory), build_dir=directory)


def written(
    app: PlatformioApp, monkeypatch: pytest.MonkeyPatch, *, erase_nvs: bool = False
) -> list[tuple[list[str], dict[str, bytes]]]:
    """The esptool commands pytest-embedded's IdfSerial.flash runs for `app`, with the bytes of
    each file it writes as they were when it ran, on a port that is not there."""
    commands: list[tuple[list[str], dict[str, bytes]]] = []

    def main(args: list[str], esp: object) -> None:
        files = {arg: Path(arg).read_bytes() for arg in args if Path(arg).is_file()}
        commands.append((list(args), files))

    monkeypatch.setattr(esptool, "main", main)
    monkeypatch.delenv("ESPBAUD", raising=False)
    port = IdfSerial.__new__(IdfSerial)
    port.app, port.esp, port.proc = app, Mock(), Mock()
    port._q, port._redirect_thread, port._meta = io.StringIO(), None, None
    port.erase_nvs, port.esp_flash_force = erase_nvs, False
    port.flash()
    return commands


KEEP = ["--flash-mode", "keep", "--flash-freq", "keep", "--flash-size", "keep"]
# IdfSerial.flash() passes `after` as hard_reset whatever flash_args say.
RESETS = ["--before", "default-reset", "--after", "hard_reset", "--chip", "esp32"]


def test_serial_flashes_the_files_esp32sim_runs_with_the_otadata_erased(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = pio_build(tmp_path / ".pio/build/esp32")
    app = PlatformioApp(str(tmp_path), str(directory))
    assert app.binary_path == str(directory)
    assert app.target == "esp32"
    assert app.bin_file == str(directory / "firmware.bin")
    assert app.elf_file == str(directory / "firmware.elf")
    assert app.partition_table["nvs"] == {
        "type": 1,
        "subtype": 2,
        "offset": 0x9000,
        "size": 0x5000,
        "flags": 0,
    }
    [(command, files)] = written(app, monkeypatch)
    otadata = command[command.index("0xe000") + 1]
    assert command == [
        *RESETS,
        "--baud",
        "921600",
        "write-flash",
        "0x1000",
        f"{directory}/bootloader.bin",
        "0x8000",
        f"{directory}/partitions.bin",
        "0xe000",
        otadata,
        "0x20000",
        f"{directory}/firmware.bin",
        *KEEP,
    ]
    # the otadata partition, all of it erased, from a file outside the build
    assert files[otadata] == b"\xff" * 0x2000
    assert not Path(otadata).is_relative_to(directory)
    # removed with the app, without a ResourceWarning
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        del app, files
        gc.collect()
    assert not Path(otadata).parent.exists()


def test_serial_erases_nvs_from_the_build_s_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = pio_build(tmp_path)
    app = PlatformioApp(str(tmp_path), str(directory))
    [(command, _)] = written(app, monkeypatch)
    # --erase-nvs erases the table's nvs before writing
    [(erase, _), (again, _)] = written(app, monkeypatch, erase_nvs=True)
    assert erase == ["erase-region", str(0x9000), str(0x5000)]
    assert again == command


def test_serial_flashes_a_factory_image_whole_from_0x0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = pio_build(tmp_path, factory=True)

    def no_directory(**kwargs: object) -> str:
        raise AssertionError("nothing is erased: no directory for it")

    with monkeypatch.context() as patched:
        patched.setattr(tempfile, "mkdtemp", no_directory)
        app = PlatformioApp(str(tmp_path), str(directory))
    # the otadata is the factory image's, boot_app0.bin's: nothing else is written there
    [(command, _)] = written(app, monkeypatch)
    assert command == [
        *RESETS,
        "--baud",
        "921600",
        "write-flash",
        "0x0",
        f"{directory}/firmware.factory.bin",
        *KEEP,
    ]
    assert app.bin_file == str(directory / "firmware.bin")


def test_a_table_without_otadata_gets_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    table = (entry("nvs", 1, 2, 0x9000, 0x6000) + entry("factory", 0, 0, 0x10000, 0x100000)).ljust(
        build.TABLE_SIZE, b"\xff"
    )
    directory = pio_build(tmp_path, table=table, app_at=0x10000)
    [(command, _)] = written(PlatformioApp(str(tmp_path), str(directory)), monkeypatch)
    # firmware.bin is the factory partition's image: nothing is erased there
    assert offsets(command) == ["0x1000", "0x8000", "0x10000"]


def offsets(command: list[str]) -> list[str]:
    """The offsets of a write-flash command's files."""
    files = command[command.index("write-flash") + 1 : command.index("--flash-mode")]
    return files[::2]


@pytest.mark.parametrize("factory", [False, True])
def test_a_factory_partition_the_build_does_not_fill_is_not_booted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, factory: bool
) -> None:
    # A launcher's table: firmware.bin in ota_0 and a factory partition after it, where a board
    # may hold a launcher, or an older application, that an erased otadata would boot first.
    directory = pio_build(tmp_path, table=FACT512K, factory=factory, app_at=0x10000)
    [(command, files)] = written(PlatformioApp(str(tmp_path), str(directory)), monkeypatch)
    if factory:
        # boot_app0.bin's otadata selects ota_0: the factory partition is not tried
        assert offsets(command) == ["0x0"]
        # a framework = espidf build's factory image holds an erased ota_data_initial.bin, which
        # selects nothing: the bootloader tries factory first
        image = directory / "firmware.factory.bin"
        contents = bytearray(image.read_bytes())
        contents[0xE000:0x10000] = b"\xff" * 0x2000
        image.write_bytes(bytes(contents))
        [(command, files)] = written(PlatformioApp(str(tmp_path), str(directory)), monkeypatch)
        assert offsets(command) == ["0x0", "0xf70000"]
        assert files[command[command.index("0xf70000") + 1]] == b"\xff" * 0x1000
    else:
        assert offsets(command) == ["0x1000", "0x8000", "0xe000", "0x10000", "0xf70000"]
        erased = command[command.index("0xf70000") + 1]
        assert Path(erased).name == "factory-erased.bin"
        # its first sector, where an image's header is
        assert files[erased] == b"\xff" * 0x1000


SERIAL_PLATFORMIO = "import json\n" + FAKE_SERIAL.replace(
    "    def flash(self):\n",
    "    def flash(self):\n"
    "        Path('flashed').write_text(json.dumps([(hex(f.offset), Path(f.file_path).name)"
    " for f in self.app.flash_files]))\n",
)


@pytest.mark.parametrize("factory", [False, True])
def test_serial_runs_a_platformio_build(pytester: pytest.Pytester, factory: bool) -> None:
    directory = pio_build(pytester.path / ".pio/build/esp32", factory=factory)
    digest = hashlib.sha256((directory / "firmware.bin").read_bytes()).hexdigest()
    reference = Tap("console", pytester.path / "reference.jsonl")
    recording.write(
        reference.path,
        reference.recording(board="esp32-devkitc", source="real", clock="wall", app_sha256=digest),
    )
    pytester.makeconftest(SERIAL_PLATFORMIO)
    pytester.makepyfile(f"""
        import pytest
        from esp32tl.backends.serial import PlatformioApp

        @pytest.mark.app_path(".", build_dir=".pio/build/esp32")
        @pytest.mark.app_recording("reference.jsonl")
        def test_runs(dut, board, console):
            assert type(dut.app) is PlatformioApp
            assert dut.serial.flashes == 1
            assert board.app_sha256 == {digest!r}
            console.tap("run.jsonl")
            board.power_on()
            console.expect("Hello world!")
    """)
    run_serial(pytester).assert_outcomes(passed=1)
    flashed = json.loads((pytester.path / "flashed").read_text())
    if factory:
        assert flashed == [["0x0", "firmware.factory.bin"]]
    else:
        assert flashed == [
            ["0x1000", "bootloader.bin"],
            ["0x8000", "partitions.bin"],
            ["0xe000", "otadata-erased.bin"],
            ["0x20000", "firmware.bin"],
        ]
    assert recording.read(pytester.path / "run.jsonl").header.app_sha256 == digest


def test_an_esp_idf_build_keeps_pytest_embedded_s_app(pytester: pytest.Pytester) -> None:
    fake_build(pytester.path)
    pytester.makeconftest(FAKE_SERIAL)
    pytester.makepyfile("""
        from pytest_embedded_idf.app import IdfApp

        def test_runs(dut, board):
            assert type(dut.app) is IdfApp
    """)
    run_serial(pytester).assert_outcomes(passed=1)


@pytest.mark.parametrize("factory", [False, True])
def test_board_nvs_and_board_flash_find_the_table_of_either_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, factory: bool
) -> None:
    pio_build(tmp_path / ".pio/build/esp32", factory=factory)
    monkeypatch.setattr(esp32sim, "runner_path", lambda: Path("runner"))
    monkeypatch.setattr(esp32sim, "rom_path", lambda: Path("rom"))
    run = tmp_path / "run"
    run.mkdir()
    board = Esp32Sim(
        Mock(spec=Dut, logfile=str(run / "dut.log")),
        str(tmp_path),
        Mock(spec=MessageQueue),
        Meta(str(run), {}, {}),
        boards.profile("esp32-devkitc"),
        build_dir=".pio/build/esp32",
    )
    board.nvs({"config": {"host": "broker.test"}})
    image = run / "nvs-9000.bin"
    assert b"broker.test\0" in image.read_bytes()
    with pytest.raises(BackendError, match="no partition 'settings': it has nvs, otadata, app0"):
        board.nvs({}, partition="settings")
    assert board._partition_table("board.flash") == flash.partitions(TABLE)
    popen = Mock(side_effect=OSError("no executable"))
    monkeypatch.setattr(esp32sim, "RunnerPopen", popen)
    with pytest.raises(BackendError, match="cannot start"):
        board.power_on()
    # the test's NVS after the build's files, over them
    assert flash_at(popen.call_args.args[1])[-1] == f"0x9000={image}"
    board.close()


def with_md5(entries: bytes) -> bytes:
    """A partition table of `entries` with the MD5 record ESP-IDF writes after them."""
    md5 = b"\xeb\xeb" + b"\xff" * 14 + hashlib.md5(entries).digest()
    return (entries + md5).ljust(build.TABLE_SIZE, b"\xff")


# Arduino-ESP32's kind of table, without a factory partition, within the 2 MB nvs_reader is built
# for, its first application at 0x20000 rather than 0x10000: the bootloader boots ota_0 there.
ARDUINO_TABLE = with_md5(
    entry("nvs", 1, 2, 0x9000, 0x5000)
    + entry("otadata", 1, 0, 0xE000, 0x2000)
    + entry("app0", 0, 0x10, 0x20000, 0x100000)
    + entry("app1", 0, 0x11, 0x120000, 0xE0000)
)

# A launcher's kind of table, as Arduino-ESP32's fact512k ones are, within 2 MB: the application in
# ota_0, a factory partition after it that the build leaves empty.
LAUNCHER_TABLE = with_md5(
    entry("nvs", 1, 2, 0x9000, 0x5000)
    + entry("otadata", 1, 0, 0xE000, 0x2000)
    + entry("app0", 0, 0x10, 0x20000, 0x100000)
    + entry("factory", 0, 0x00, 0x120000, 0xE0000)
)


def platformio_from(
    esp_idf: Path, directory: Path, *, factory: bool, table: bytes | None = None
) -> Path:
    """The PlatformIO build of the ESP-IDF build in `esp_idf`: its bootloader, table, application
    and ELF under PlatformIO's names, and, if `factory`, all of it merged from 0x0, as pioarduino
    merges it. A `table` takes the place of the build's, the application at its `ota_0` at 0x20000
    and `boot_app0.bin`'s otadata in the factory image."""
    if not (esp_idf / "esp32tl-built").is_file():
        pytest.fail(f"{esp_idf} holds no finished build: run make firmware", pytrace=False)
    app = esp32sim.load_app(str(esp_idf.parent))
    assert app.app_image is not None and app.elf is not None
    directory.mkdir(parents=True)
    by_offset = {f.offset: f.path.read_bytes() for f in app.flash_files}
    assert sorted(by_offset) == [0x1000, 0x8000, 0x10000]
    if table is not None:
        by_offset = {0x1000: by_offset[0x1000], 0x8000: table, 0x20000: by_offset[0x10000]}
    bootloader, partitions, firmware = (by_offset[offset] for offset in sorted(by_offset))
    if table is not None:
        by_offset[0xE000] = BOOT_APP0
    (directory / "bootloader.bin").write_bytes(bootloader)
    (directory / "partitions.bin").write_bytes(partitions)
    (directory / "firmware.bin").write_bytes(firmware)
    (directory / "firmware.elf").write_bytes(app.elf.read_bytes())
    if factory:
        (directory / "firmware.factory.bin").write_bytes(merged(by_offset))
    return directory


def contents(directory: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in directory.iterdir()
    }


@pytest.mark.parametrize("factory", [False, True], ids=["split", "factory-image"])
@pytest.mark.parametrize(
    "table,app_at",
    [(None, 0x10000), (ARDUINO_TABLE, 0x20000), (LAUNCHER_TABLE, 0x20000)],
    ids=["factory-partition", "ota_0", "ota_0-before-factory"],
)
def test_nvs_reader_built_by_platformio_runs_as_it_is(
    pytester: pytest.Pytester, factory: bool, table: bytes | None, app_at: int
) -> None:
    directory = platformio_from(
        NVS_READER / "build", pytester.path / ".pio/build/esp32", factory=factory, table=table
    )
    before = contents(directory)
    # What esp_ota_get_boot_partition() says: boot_app0.bin's otadata selects app0; with it erased,
    # the bootloader writes ota_seq 1 when the table has no factory partition, and nothing when it
    # tried factory first, which then reads as the boot partition. ESP-IDF's table has no otadata.
    ota_check = ""
    if table is not None:
        expected = "factory" if table is LAUNCHER_TABLE and not factory else "app0"
        ota_check = f"assert board.flash().ota().boot_partition == {expected!r}"
    pytester.makepyfile(
        test_pio=f"""
        import pytest

        from esp32tl.flash import i8, i16, i32, i64, u8, u16, u32, u64

        CONFIG = {{
            "broker_host": "pio.test",
            "broker_port": u16(1883),
            "broker_ca": b"ca",
            "enabled": u8(1),
            "count": u32(2),
            "delta": i32(-3),
            "small": i8(-4),
            "medium": i16(-5),
            "total": u64(6),
            "offset": i64(-7),
        }}

        @pytest.mark.app_path(".", build_dir=".pio/build/esp32")
        def test_reads_the_tests_nvs(board, console):
            board.nvs({{"config": CONFIG}})
            board.power_on()
            console.expect_exact("boot: Loaded app from partition at offset {app_at:#x}")
            console.expect_exact("nvs_reader: host=pio.test port=1883 enabled=1 count=2 delta=-3")
            console.expect_exact("nvs_reader: blob=6361")
            assert board.flash().nvs()["config"]["broker_host"] == "pio.test"
            assert board.boots[-1].number == 1
            {ota_check}
        """
    )
    result = pytester.runpytest_subprocess("-q")
    result.assert_outcomes(passed=1)
    # nothing written into the build, renamed or added
    assert contents(directory) == before


def test_esp32tl_run_runs_a_platformio_build(tmp_path: Path) -> None:
    directory = platformio_from(HELLO_WORLD / "build", tmp_path / ".pio/build/esp32", factory=True)
    before = contents(directory)
    arguments = ["run", "--app", str(tmp_path), "--build-dir", ".pio/build/esp32"]
    result = subprocess.run(
        [sys.executable, "-c", COMMAND, *arguments, "--max-seconds", "2"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "Hello world!" in result.stdout
    assert contents(directory) == before
