"""The flash the runner gives the chip: the board profile's `flash_mb`, else the size the build's
image headers give, passed as `--flash-mb`; and the refusals at setup, before any run, of a size
the runner does not have and of a build whose header gives more flash than the runner would.
The first tests make PlatformIO build directories from synthetic images and need nothing beyond
the package; the runs boot `hello_world_8mb`, ESP-IDF's hello_world built for an 8 MB flash, on
every chip, and need `make firmware` (hello_world_8mb, and its `--chips` images)."""

import dataclasses
import subprocess
import textwrap
from pathlib import Path
from unittest.mock import Mock

import pytest
from pytest_embedded.dut import Dut
from pytest_embedded.log import MessageQueue
from pytest_embedded.utils import Meta
from test_platformio_build import pio_build
from test_pytest_plugin import fake_build

from esp32tl import boards, build, cli
from esp32tl.backends import esp32sim
from esp32tl.backends.esp32sim import BackendError, Esp32Sim
from esp32tl.boards import BoardProfile

ROOT = Path(__file__).resolve().parent.parent
HELLO_WORLD_8MB = ROOT / "firmware" / "hello_world_8mb"
PIO = ".pio/build/esp32"

# ESP-IDF's esp_image_flash_size_t, in the high four bits of an image header's byte 3.
SIZE_CODES = {1: 0, 2: 1, 4: 2, 8: 3, 16: 4, 32: 5, 64: 6, 128: 7}


def with_flash(path: Path, mb: int) -> None:
    """Puts the flash size `mb` into the image header at the start of the file at `path`, keeping
    the low four bits of byte 3, the SPI frequency."""
    data = bytearray(path.read_bytes())
    data[3] = SIZE_CODES[mb] << 4 | data[3] & 0x0F
    path.write_bytes(bytes(data))


def sized_build(directory: Path, *, bootloader: int = 4, app: int = 4) -> Path:
    """A PlatformIO build of synthetic images whose headers give `bootloader` and `app` MB."""
    built = pio_build(directory / PIO)
    with_flash(built / "bootloader.bin", bootloader)
    with_flash(built / "firmware.bin", app)
    return built


def profile(flash_mb: int | None, name: str = "esp32-devkitc") -> BoardProfile:
    return dataclasses.replace(boards.profile(name), flash_mb=flash_mb)


def flash_mb_of(command: list[str]) -> str | None:
    return command[command.index("--flash-mb") + 1] if "--flash-mb" in command else None


@pytest.mark.parametrize(("mb", "code"), SIZE_CODES.items())
def test_an_image_header_gives_its_flash_size_in_the_high_bits_of_byte_3(
    mb: int, code: int
) -> None:
    header = bytes([0xE9, 4, 2, code << 4 | 0xF]).ljust(build.HEADER_SIZE, b"\0")
    assert build.flash_size(header) == mb


@pytest.mark.parametrize(
    "header",
    [
        # esp_image_flash_size_t ends at 128 MB: 8 is ESP_IMAGE_FLASH_SIZE_MAX
        bytes([0xE9, 4, 2, 0x80]).ljust(build.HEADER_SIZE, b"\0"),
        bytes([0xFF, 4, 2, 0x20]).ljust(build.HEADER_SIZE, b"\0"),
        bytes([0xE9, 4, 2, 0x20]),
    ],
    ids=["size code 8", "no magic", "short"],
)
def test_a_header_that_gives_no_size_of_esp_idf_s_gives_none(header: bytes) -> None:
    assert build.flash_size(header) is None


def test_the_larger_header_wins_and_the_bootloader_s_on_a_tie(tmp_path: Path) -> None:
    built = sized_build(tmp_path / "a", bootloader=8, app=2)
    app = esp32sim.load_app(str(tmp_path / "a"), build_dir=PIO)
    assert build.header_flash(app) == build.HeaderFlash(
        8, built / "bootloader.bin", "bootloader at 0x1000"
    )
    built = sized_build(tmp_path / "b", bootloader=2, app=16)
    app = esp32sim.load_app(str(tmp_path / "b"), build_dir=PIO)
    assert build.header_flash(app) == build.HeaderFlash(
        16, built / "firmware.bin", "application image"
    )
    built = sized_build(tmp_path / "c", bootloader=8, app=8)
    app = esp32sim.load_app(str(tmp_path / "c"), build_dir=PIO)
    assert build.header_flash(app) == build.HeaderFlash(
        8, built / "bootloader.bin", "bootloader at 0x1000"
    )


def test_a_factory_image_gives_the_bootloader_s_header_in_it(tmp_path: Path) -> None:
    built = pio_build(tmp_path / PIO, factory=True)
    factory = built / "firmware.factory.bin"
    data = bytearray(factory.read_bytes())
    data[0x1000 + 3] = SIZE_CODES[16] << 4
    factory.write_bytes(bytes(data))
    app = esp32sim.load_app(str(tmp_path), build_dir=PIO)
    assert build.header_flash(app) == build.HeaderFlash(16, factory, "bootloader at 0x1000")


def test_a_build_whose_images_have_no_header_gives_no_size(tmp_path: Path) -> None:
    # fake_build's images are empty files
    fake_build(tmp_path)
    app = esp32sim.load_app(str(tmp_path))
    assert build.header_flash(app) is None
    assert esp32sim.runner_flash_mb(profile(None), app) is None
    assert esp32sim.runner_flash_mb(profile(2), app) == 2


@pytest.mark.parametrize(
    ("flash_mb", "header", "given"),
    [
        # the profile's, also over a header that gives less: a real board boots that too
        (8, 4, 8),
        (16, 8, 16),
        (4, 4, 4),
        # a profile that does not know its flash: the header's
        (None, 8, 8),
        (None, 1, 1),
    ],
)
def test_the_runner_gets_the_profile_s_flash_else_the_header_s(
    tmp_path: Path, flash_mb: int | None, header: int, given: int
) -> None:
    sized_build(tmp_path, bootloader=header, app=header)
    app = esp32sim.load_app(str(tmp_path), build_dir=PIO)
    assert esp32sim.runner_flash_mb(profile(flash_mb), app) == given
    command = esp32sim.runner_command(
        Path("runner"), Path("rom"), app, 1, Path("link"), flash_mb=given, psram_mb=0
    )
    assert flash_mb_of(command) == str(given)
    # before --psram-mb, after the build's files
    assert command.index("--flash-mb") == command.index("--psram-mb") - 2


def test_without_a_size_the_command_has_no_flash_mb(tmp_path: Path) -> None:
    sized_build(tmp_path)
    app = esp32sim.load_app(str(tmp_path), build_dir=PIO)
    assert flash_mb_of(esp32sim.runner_command(Path("r"), Path("rom"), app, 1, Path("s"))) is None


@pytest.mark.parametrize("flash_mb", [0, 3, 32])
def test_a_profile_size_the_runner_does_not_have_is_refused(tmp_path: Path, flash_mb: int) -> None:
    sized_build(tmp_path)
    app = esp32sim.load_app(str(tmp_path), build_dir=PIO)
    with pytest.raises(BackendError) as refused:
        esp32sim.runner_flash_mb(profile(flash_mb), app)
    assert str(refused.value) == (
        f"board profile 'esp32-devkitc' has flash_mb = {flash_mb}; the runner's flash is"
        " 1, 2, 4, 8 or 16 MB: set one of these in the profile"
    )


def test_a_header_that_gives_more_than_the_profile_is_refused(tmp_path: Path) -> None:
    built = sized_build(tmp_path, bootloader=8, app=8)
    app = esp32sim.load_app(str(tmp_path), build_dir=PIO)
    with pytest.raises(BackendError) as refused:
        esp32sim.runner_flash_mb(profile(4), app)
    assert str(refused.value) == (
        f"{built}/bootloader.bin: the image header of the bootloader at 0x1000 gives 8 MB of"
        " flash, more than the 4 MB of board profile 'esp32-devkitc' (flash_mb = 4): the"
        " application would stop at start-up; set flash_mb = 8 in the profile, or build for"
        " 4 MB of flash"
    )


# Whatever the profile says: no flash_mb would let the build run, so none is asked for.
@pytest.mark.parametrize("flash_mb", [None, 4, 16])
def test_a_header_that_gives_more_than_the_runner_has_is_refused(
    tmp_path: Path, flash_mb: int | None
) -> None:
    built = sized_build(tmp_path, bootloader=4, app=32)
    app = esp32sim.load_app(str(tmp_path), build_dir=PIO)
    with pytest.raises(BackendError) as refused:
        esp32sim.runner_flash_mb(profile(flash_mb), app)
    assert str(refused.value) == (
        f"{built}/firmware.bin: the image header of the application image gives 32 MB of flash,"
        " more than the runner's largest, 16 MB: build for 16 MB of flash or less"
    )


def board_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, board_profile: BoardProfile
) -> Esp32Sim:
    monkeypatch.setattr(esp32sim, "runner_path", lambda: Path("runner"))
    monkeypatch.setattr(esp32sim, "rom_path", lambda chip="esp32", app=None: Path("rom"))
    run = tmp_path / "run"
    run.mkdir(exist_ok=True)
    return Esp32Sim(
        Mock(spec=Dut, logfile=str(run / "dut.log")),
        str(tmp_path),
        Mock(spec=MessageQueue),
        Meta(str(run), {}, {}),
        board_profile,
        build_dir=PIO,
    )


def test_the_backend_refuses_at_setup_and_passes_the_size_at_power_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sized_build(tmp_path, bootloader=8, app=8)
    # at setup, before anything is started: the board fixture's own construction
    with pytest.raises(BackendError, match="gives 8 MB of flash, more than the 4 MB"):
        board_on(tmp_path, monkeypatch, profile(4))
    with pytest.raises(BackendError, match="has flash_mb = 32"):
        board_on(tmp_path, monkeypatch, profile(32))
    board = board_on(tmp_path, monkeypatch, profile(None))
    assert board.flash_mb == 8
    popen = Mock(side_effect=OSError("no executable"))
    monkeypatch.setattr(esp32sim, "RunnerPopen", popen)
    with pytest.raises(BackendError, match="cannot start"):
        board.power_on()
    assert flash_mb_of(popen.call_args.args[1]) == "8"
    board.close()


@pytest.mark.parametrize(("header", "given"), [(2, "4"), (4, "4")])
def test_esp32tl_run_passes_the_profile_s_flash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, header: int, given: str
) -> None:
    sized_build(tmp_path, bootloader=header, app=header)
    monkeypatch.setattr(esp32sim, "runner_path", lambda: Path("runner"))
    monkeypatch.setattr(esp32sim, "rom_path", lambda chip="esp32", app=None: Path("rom"))
    popen = Mock(side_effect=OSError(2, "No such file or directory"))
    monkeypatch.setattr(subprocess, "Popen", popen)
    args = ["run", "--app", str(tmp_path), "--build-dir", PIO, "--board", "esp32-devkitc"]
    assert cli.main(args) == cli.CANNOT_START
    assert flash_mb_of(popen.call_args.args[0]) == given


def test_esp32tl_run_refuses_a_build_for_more_flash_than_the_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    built = sized_build(tmp_path, bootloader=8, app=8)
    monkeypatch.setattr(esp32sim, "runner_path", lambda: Path("runner"))
    monkeypatch.setattr(esp32sim, "rom_path", lambda chip="esp32", app=None: Path("rom"))
    popen = Mock()
    monkeypatch.setattr(subprocess, "Popen", popen)
    args = ["run", "--app", str(tmp_path), "--build-dir", PIO, "--board", "esp32-devkitc"]
    assert cli.main(args) == cli.CANNOT_START
    assert capsys.readouterr().err == (
        f"esp32tl: {built}/bootloader.bin: the image header of the bootloader at 0x1000 gives"
        " 8 MB of flash, more than the 4 MB of board profile 'esp32-devkitc' (flash_mb = 4):"
        " the application would stop at start-up; set flash_mb = 8 in the profile, or build"
        " for 4 MB of flash\n"
    )
    popen.assert_not_called()


# The chip, and the build directory of hello_world_8mb for it.
CHIPS = [
    ("esp32", "build"),
    ("esp32s3", "build-esp32s3"),
    ("esp32c3", "build-esp32c3"),
    ("esp32c6", "build-esp32c6"),
]


@pytest.mark.parametrize(("chip", "build_dir"), CHIPS, ids=[chip for chip, _ in CHIPS])
def test_an_8_mb_build_boots_on_8_and_16_mb_and_is_refused_on_4(
    pytester: pytest.Pytester, chip: str, build_dir: str
) -> None:
    built = HELLO_WORLD_8MB / build_dir
    if not (built / "esp32tl-built").is_file():
        pytest.fail(f"{built} holds no finished build: run make firmware", pytrace=False)
    for mb in (4, 8, 16):
        pytester.path.joinpath("boards").mkdir(exist_ok=True)
        pytester.path.joinpath("boards", f"flash-{mb}.toml").write_text(
            f'name = "flash-{mb}"\nchip = "{chip}"\nflash_mb = {mb}\n'
        )
    pytester.makepyfile(
        textwrap.dedent(f"""
        import pytest

        APP = {str(HELLO_WORLD_8MB)!r}

        @pytest.mark.app_path(APP, build_dir={build_dir!r})
        @pytest.mark.board_profile(path="boards/flash-8.toml")
        def test_boots_on_8(board, console):
            board.power_on()
            console.expect_exact("8MB external flash")
            # the file of --flash-out holds the whole flash, as big as the runner's
            assert len(board.flash().contents) == 8 << 20

        @pytest.mark.app_path(APP, build_dir={build_dir!r})
        @pytest.mark.board_profile(path="boards/flash-16.toml")
        def test_boots_on_16(board, console):
            board.power_on()
            # the application keeps its header's 8 MB, and spi_flash says the chip has more
            console.expect_exact("Detected size(16384k) larger than the size in the binary"
                                 " image header(8192k)")
            console.expect_exact("8MB external flash")
            assert len(board.flash().contents) == 16 << 20

        @pytest.mark.app_path(APP, build_dir={build_dir!r})
        @pytest.mark.board_profile(path="boards/flash-4.toml")
        def test_refused_on_4(board):
            raise AssertionError("the board fixture refuses this build")
        """)
    )
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-rE")
    result.assert_outcomes(passed=2, errors=1)
    bootloader = built / "bootloader" / "bootloader.bin"
    offset = "0x1000" if chip == "esp32" else "0x0"
    result.stdout.fnmatch_lines(
        [
            f"esp32sim: {bootloader}: the image header of the bootloader at {offset} gives"
            " 8 MB of flash, more than the 4 MB of board profile 'flash-4' (flash_mb = 4): the"
            " application would stop at start-up; set flash_mb = 8 in the profile, or build for"
            " 4 MB of flash"
        ]
    )
