"""The esp32tl plugin's parametrisation, the `esp32sim` backend's command line, and the board's
power-on, clock and teardown against fake runners (plan §3, M0).

The tests that run pytest inside pytest do it in a subprocess: pytest-embedded starts a
multiprocessing manager for every session.
"""

import contextlib
import gc
import hashlib
import json
import os
import re
import shutil
import signal
import sys
import time
import warnings
from pathlib import Path
from typing import cast
from xml.etree import ElementTree

import pytest
from esptool.bin_image import LoadFirmwareImage
from pytest_embedded.log import MessageQueue
from pytest_embedded.utils import Meta

import esp32tl
from esp32tl import recording
from esp32tl.backends import esp32sim
from esp32tl.backends.esp32sim import BackendError
from esp32tl.boards import SdCardRoute
from esp32tl.interfaces import RunEnded
from esp32tl.interfaces.sd_card import SdCard
from esp32tl.interfaces.wifi import Wifi

ROOT = Path(__file__).resolve().parent.parent
HELLO_WORLD_BUILD = ROOT / "firmware" / "hello_world" / "build"

# A `board` that shows what the plugin parametrised it with, in place of the real one.
SHOW_PARAMS = """
import pytest

@pytest.fixture
def board(request, embedded_services, app_path, build_dir):
    return request.param, embedded_services, app_path
"""


def test_board_is_parametrised_with_the_backend_its_services_and_the_app(
    pytester: pytest.Pytester,
) -> None:
    pytester.makeconftest(SHOW_PARAMS)
    pytester.makepyfile(
        test_params="""
        from pathlib import Path

        import pytest

        HERE = Path(__file__).parent

        @pytest.mark.app_path("../firmware/app")
        def test_marked(board, request):
            assert request.node.name == "test_marked[esp32sim]"
            assert board == ("esp32sim", None, str(HERE.parent / "firmware" / "app"))

        def test_unmarked(board):
            assert board == ("esp32sim", None, str(HERE))

        def test_without_board(request):
            assert not hasattr(request.node, "callspec")
        """
    )
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=3)


def test_backend_option_takes_a_backend_name_only(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(SHOW_PARAMS)
    pytester.makepyfile("def test_one(board): pass")
    result = pytester.runpytest_subprocess(
        "-p", "no:cacheprovider", "--collect-only", "-q", "--esp32tl-backend", "esp32sim"
    )
    result.stdout.fnmatch_lines(
        ["test_backend_option_takes_a_backend_name_only.py::test_one[esp32sim]"]
    )
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "--esp32tl-backend", "qemu")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*--esp32tl-backend: invalid choice: 'qemu'*"])


@pytest.mark.parametrize(
    "option",
    [
        ("--embedded-services", "esp"),
        ("--app-path", "firmware/hello_world"),
        ("--build-dir", "elsewhere"),
    ],
)
def test_pytest_embedded_options_are_refused_for_tests_with_a_board(
    pytester: pytest.Pytester, option: tuple[str, str]
) -> None:
    pytester.makepyfile("def test_one(board): pass\n\ndef test_two(): pass")
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", *option)
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(
        [f"ERROR: {option[0]} is set: esp32tl chooses it for every test that uses `board`*"]
    )
    result.stdout.no_fnmatch_line("*Traceback*")


def test_more_than_one_dut_is_refused(pytester: pytest.Pytester) -> None:
    pytester.makepyfile("def test_one(board): pass")
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "--count", "2")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["ERROR: --count is set: esp32tl runs one board in a test"])


def test_a_parametrised_count_above_one_is_refused(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.parametrize("count", [2], indirect=True)
        def test_two_duts(board):
            pass
        """
    )
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*count is above 1: esp32tl runs one board in a test"])


def test_options_are_allowed_when_no_selected_test_uses_board(pytester: pytest.Pytester) -> None:
    pytester.makepyfile("def test_one(board): pass\n\ndef test_two(): pass")
    result = pytester.runpytest_subprocess(
        "-p", "no:cacheprovider", "-k", "test_two", "--app-path", "elsewhere"
    )
    result.assert_outcomes(passed=1, deselected=1)


def test_app_path_marker_needs_one_path(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.app_path
        def test_bare(board):
            pass
        """
    )
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(
        ["test_app_path_marker_needs_one_path.py::test_bare: the app_path marker takes one path,*"]
    )


@pytest.mark.parametrize(
    ("marks", "message"),
    [
        ("@pytest.mark.board_profile", "the board_profile marker takes one bundled name or *"),
        (
            '@pytest.mark.board_profile("esp32-wrover-kit")',
            "no board profile named 'esp32-wrover-kit';*",
        ),
    ],
)
def test_a_marker_no_backend_can_satisfy_is_a_collection_error(
    pytester: pytest.Pytester, marks: str, message: str
) -> None:
    pytester.makeconftest(SHOW_PARAMS)
    pytester.makepyfile(test_marks=f"import pytest\n\n{marks}\ndef test_one(board):\n    pass\n")
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    assert result.ret == pytest.ExitCode.INTERRUPTED
    result.stdout.fnmatch_lines([f"*test_marks.py::test_one: {message}"])
    result.stdout.no_fnmatch_line("*During handling of the above exception*")


def test_markers_on_a_class_or_a_module_count(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(SHOW_PARAMS)
    pytester.makepyfile(
        test_levels="""
        import pytest

        pytestmark = pytest.mark.board_profile("lilygo-t-a7670g-r2")

        @pytest.mark.board_profile("esp32-wrover-kit")
        class TestModem:
            def test_one(self, board):
                pass
        """
    )
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    assert result.ret == pytest.ExitCode.INTERRUPTED
    # The class's marker is closer than the module's.
    result.stdout.fnmatch_lines(
        ["*::TestModem::test_one: no board profile named 'esp32-wrover-kit'*"]
    )


@pytest.mark.parametrize(
    "arguments",
    [
        '"esp32-devkitc", path="board.toml"',
        'name="esp32-devkitc"',
        'path="board.toml", extra=True',
        '"one", "two"',
        "42",
        "Path('board.toml')",
        "path=None",
        "path=42",
        "path=''",
        "path=Path('')",
    ],
)
def test_board_profile_rejects_ambiguous_arguments(
    pytester: pytest.Pytester, arguments: str
) -> None:
    pytester.makepyfile(f"""
        from pathlib import Path
        import pytest
        @pytest.mark.board_profile({arguments})
        def test_profile(board): pass
    """)
    result = pytester.runpytest_subprocess("-q")
    assert result.ret == pytest.ExitCode.INTERRUPTED
    result.stdout.fnmatch_lines(
        [
            "*::test_profile: the board_profile marker takes one bundled name or path=*"
            "*do not combine them"
        ]
    )


@pytest.mark.parametrize("name", ["boards/local.toml", "local.toml"])
def test_a_bundled_name_that_looks_like_a_path_suggests_path(
    pytester: pytest.Pytester, name: str
) -> None:
    pytester.makepyfile(f"""
        import pytest
        @pytest.mark.board_profile("{name}")
        def test_profile(board): pass
    """)
    result = pytester.runpytest_subprocess("-q")
    assert result.ret == pytest.ExitCode.INTERRUPTED
    result.stdout.fnmatch_lines(
        [
            f"*::test_profile: no board profile named '{name}'; the profiles are *;"
            f' for a local file, use board_profile(path="{name}")'
        ]
    )


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        (None, "*No such file or directory*"),
        ("name = [", "*Invalid value*"),
        (b'name = "local"\nchip = "esp32"\n# \xff\n', "*'utf-8' codec can't decode byte*"),
        ('name = "wrong"\nchip = "esp32"', "*name: 'wrong' is not the file's name, 'local'*"),
        ('name = "local"', "*missing chip*"),
        ('name = "local"\nchip = "esp32s3"', "*chip: 'esp32s3' is not one of esp32*"),
        (
            'name = "local"\nchip = "esp32"\n[uart.gnss]\nport = 1\ntx = 4\nrx = 4\nbaud = 9600',
            "*GPIO 4 is uart.gnss.tx already*",
        ),
        (
            'name = "local"\nchip = "esp32"\n[uart.gnss]\nport = 0\ntx = 4\nrx = 5\nbaud = 9600',
            "*UART0 is console's*",
        ),
    ],
)
def test_invalid_local_profiles_fail_at_collection(
    pytester: pytest.Pytester, contents: str | bytes | None, message: str
) -> None:
    path = pytester.path / "local.toml"
    if contents is not None:
        path.write_bytes(contents.encode() if isinstance(contents, str) else contents)
    pytester.makepyfile("""
        import pytest
        @pytest.mark.board_profile(path="local.toml")
        def test_profile(board): pass
    """)
    result = pytester.runpytest_subprocess("-q")
    assert result.ret == pytest.ExitCode.INTERRUPTED
    result.stdout.fnmatch_lines([f"*::test_profile: {path}: {message}"])
    result.stdout.no_fnmatch_line("*Traceback*")
    result.stdout.no_fnmatch_line("*UnicodeDecodeError*")


def test_local_profiles_are_test_relative_and_inherited(fake_runner: pytest.Pytester) -> None:
    suite = fake_runner.path / "suite"
    suite.mkdir()
    (suite / "local.toml").write_text('name = "local"\nchip = "esp32"\npsram_mb = 4')
    (suite / "test_local.py").write_text("""
from pathlib import Path
import pytest

pytestmark = [
    pytest.mark.app_path(".."),
    pytest.mark.board_profile(path="local.toml"),
]

def test_module(board):
    assert board.profile.name == "local"
    assert board.profile.psram_mb == 4

@pytest.mark.board_profile("lilygo-t-a7670g-r2")
class TestNamed:
    def test_class(self, board):
        assert board.profile.name == "lilygo-t-a7670g-r2"

    @pytest.mark.board_profile(path=Path(__file__).parent / "local.toml")
    def test_absolute_override(self, board):
        assert board.profile.name == "local"

@pytest.mark.board_profile(path=Path("local.toml"))
class TestLocal:
    def test_class(self, board):
        assert board.profile.name == "local"
""")
    # Invocation directory is the parent of suite; only the test-relative file exists.
    fake_runner.runpytest_subprocess("-q", str(suite)).assert_outcomes(passed=4)


def test_local_profiles_with_the_same_name_keep_separate_snapshots(
    fake_runner: pytest.Pytester,
) -> None:
    for directory, pin in (("one", 18), ("two", 19)):
        folder = fake_runner.path / directory
        folder.mkdir()
        (folder / "esp32-devkitc.toml").write_text(
            f'name = "esp32-devkitc"\nchip = "esp32"\n[lines.test]\noutput = {pin}\n'
        )
        (folder / "test_profile.py").write_text(f"""
import pytest

pytestmark = [pytest.mark.app_path(".."), pytest.mark.board_profile(path="esp32-devkitc.toml")]

def test_local(board):
    assert board.profile.name == "esp32-devkitc"
    assert board.profile.lines["test"]["output"].pin == {pin}
    assert board.profile.uart == {{}}

@pytest.mark.board_profile("esp32-devkitc")
def test_bundled(board):
    assert board.profile.uart["gnss"].tx == 4
    assert board.profile.lines == {{}}
""")
    fake_runner.makeconftest("""
def pytest_collection_modifyitems(items):
    # The validated snapshot must reach setup even if its file disappears after collection.
    for directory in ("one", "two"):
        (items[0].config.rootpath / directory / "esp32-devkitc.toml").unlink()
""")
    fake_runner.runpytest_subprocess("-q", "--import-mode=importlib", "one", "two").assert_outcomes(
        passed=4
    )


def test_local_profile_snapshot_survives_retargeting_a_symlink(
    fake_runner: pytest.Pytester,
) -> None:
    for directory, size in (("one", 4), ("two", 8)):
        folder = fake_runner.path / directory
        folder.mkdir()
        (folder / "local.toml").write_text(f'name = "local"\nchip = "esp32"\npsram_mb = {size}')
    (fake_runner.path / "local.toml").symlink_to("one/local.toml")
    fake_runner.makeconftest("""
def pytest_runtest_setup(item):
    link = item.path.parent / "local.toml"
    link.unlink()
    link.symlink_to("two/local.toml")
    (item.path.parent / "one/local.toml").unlink(missing_ok=True)
""")
    fake_runner.makepyfile(
        test_fake="""
        import pytest

        @pytest.mark.board_profile(path="local.toml")
        @pytest.mark.parametrize("case", [1, 2])
        def test_snapshot(board, case):
            assert board.profile.name == "local"
            assert board.profile.psram_mb == 4
    """
    )
    fake_runner.runpytest_subprocess("-q").assert_outcomes(passed=2)


@pytest.mark.parametrize("late", ["param", "hook"])
def test_late_local_profile_switch_with_the_same_name_is_refused(
    pytester: pytest.Pytester, late: str
) -> None:
    for directory in ("one", "two"):
        folder = pytester.path / directory
        folder.mkdir()
        (folder / "local.toml").write_text('name = "local"\nchip = "esp32"')
    pytester.makeconftest(
        SHOW_PARAMS
        + (
            """
def pytest_collection_modifyitems(items):
    items[0].add_marker(pytest.mark.board_profile(path="two/local.toml"), append=False)
"""
            if late == "hook"
            else ""
        )
    )
    pytester.makepyfile(
        """
import pytest
pytestmark = pytest.mark.board_profile(path="one/local.toml")
"""
        + (
            """
@pytest.mark.parametrize("x", [
    pytest.param(1, marks=pytest.mark.board_profile(path="two/local.toml"))
])
"""
            if late == "param"
            else '@pytest.mark.parametrize("x", [1])\n'
        )
        + """
def test_local(board, x): pass
"""
    )
    result = pytester.runpytest_subprocess("-q")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*the board_profile marker reached the test after*"])


@pytest.mark.parametrize(
    "marker",
    ['board_profile("lilygo-t-a7670g-r2")', 'app_path("elsewhere")', 'app_recording("r.jsonl")'],
)
def test_a_board_marker_in_a_pytest_param_is_refused(
    pytester: pytest.Pytester, marker: str
) -> None:
    pytester.makeconftest(SHOW_PARAMS)
    pytester.makepyfile(
        test_param=f"""
        import pytest

        @pytest.mark.parametrize("x", [1, pytest.param(2, marks=pytest.mark.{marker})])
        def test_p(board, x):
            pass
        """
    )
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    name = marker.partition("(")[0]
    result.stderr.fnmatch_lines(
        [
            f"ERROR: test_param.py::test_p[[]esp32sim-2[]]: the {name} marker reached the test"
            " after esp32tl read it, from a pytest.param or a hook: put it on the test, its class"
            " or its module"
        ]
    )


@pytest.mark.parametrize(
    ("items", "node", "profile"),
    [
        ("items[:1]", "item", '"lilygo-t-a7670g-r2"'),
        # One that names no profile at all.
        ("items[:1]", "item", ""),
        # The class's node, whose markers are not in the class's own pytestmark.
        ("items[:1]", "item.parent", '"lilygo-t-a7670g-r2"'),
        # The module's node, once per item.
        ("items", "item.parent.parent", '"lilygo-t-a7670g-r2"'),
    ],
)
def test_a_board_marker_a_hook_adds_is_refused(
    pytester: pytest.Pytester, items: str, node: str, profile: str
) -> None:
    pytester.makeconftest(
        SHOW_PARAMS
        + f"""
def pytest_collection_modifyitems(items):
    for item in {items}:
        {node}.add_marker(pytest.mark.board_profile({profile}))
"""
    )
    pytester.makepyfile(test_hook="class TestA:\n    def test_one(self, board):\n        pass\n")
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(
        [
            "ERROR: test_hook.py::TestA::test_one[[]esp32sim[]]: the board_profile marker reached"
            " the test after*"
        ]
    )


def fake_build(
    app: Path, target: str = "esp32", encrypted: bool = False, *, build_dir: str = "build"
) -> Path:
    """An ESP-IDF build directory with what `IdfApp` reads, its images empty."""
    build = app / build_dir
    files = {
        "0x1000": "bootloader/bootloader.bin",
        "0x8000": "partition_table/partition-table.bin",
        "0x10000": "app.bin",
    }
    for name in files.values():
        (build / name).parent.mkdir(parents=True, exist_ok=True)
        (build / name).touch()
    (build / "app.elf").touch()
    (build / "config").mkdir()
    (build / "config" / "sdkconfig.json").write_text(json.dumps({"IDF_TARGET": target}))
    flasher_args = {
        "flash_settings": {"flash_mode": "dio", "flash_size": "2MB", "flash_freq": "40m"},
        "flash_files": files,
        "app": {"offset": "0x10000", "file": "app.bin", "encrypted": str(encrypted).lower()},
    }
    (build / "flasher_args.json").write_text(json.dumps(flasher_args))
    return build


def test_runner_command_flashes_every_file_at_its_offset(tmp_path: Path) -> None:
    build = fake_build(tmp_path)
    app = esp32sim.load_app(str(tmp_path))
    command = esp32sim.runner_command(
        Path("/r/runner"), Path("/r/rom.elf"), app, 300, Path("/tmp/l/link.sock")
    )
    assert command == [
        "/r/runner",
        "--chip",
        "esp32",
        "--rom",
        "/r/rom.elf",
        "--max-seconds",
        "300",
        "--board",
        "socket:/tmp/l/link.sock",
        "--flash-at",
        f"0x1000={build.resolve()}/bootloader/bootloader.bin",
        "--flash-at",
        f"0x8000={build.resolve()}/partition_table/partition-table.bin",
        "--flash-at",
        f"0x10000={build.resolve()}/app.bin",
    ]


def test_runner_command_puts_a_card_in_the_slot_once_the_test_inserts_one(tmp_path: Path) -> None:
    fake_build(tmp_path)
    app = esp32sim.load_app(str(tmp_path))
    slot = SdCard(SdCardRoute(sclk=14, mosi=15, miso=2, cs=13), directory=tmp_path)
    command = esp32sim.runner_command(
        Path("/r/runner"), Path("/r/rom.elf"), app, 300, Path("/l.sock"), sd_card=slot
    )
    # the slot is there empty, so that a card can go in while the firmware runs
    assert command[-2:] == ["--sd-spi", "sclk=14,mosi=15,miso=2,cs=13"]
    (tmp_path / "card.img").write_bytes(bytes(512 << 10))
    # relative to the test file's directory
    slot.insert("card.img")
    command = esp32sim.runner_command(
        Path("/r/runner"), Path("/r/rom.elf"), app, 300, Path("/l.sock"), sd_card=slot
    )
    assert command[-4:] == [
        "--sd-spi",
        "sclk=14,mosi=15,miso=2,cs=13",
        "--sd-rw",
        str((tmp_path / "card.img").resolve()),
    ]


def test_runner_command_puts_the_tests_access_point_on_the_air(tmp_path: Path) -> None:
    fake_build(tmp_path)
    app = esp32sim.load_app(str(tmp_path))
    wifi = Wifi(now=lambda: 0)
    command = esp32sim.runner_command(
        Path("/r/runner"), Path("/r/rom.elf"), app, 300, Path("/l.sock"), wifi=wifi
    )
    assert "--wifi" not in command
    wifi.access_point("myssid", password="mypassword")
    wifi.nearby("cafe", secure=False)
    command = esp32sim.runner_command(
        Path("/r/runner"), Path("/r/rom.elf"), app, 300, Path("/l.sock"), wifi=wifi
    )
    assert command[-4:] == [
        "--wifi",
        "ssid=myssid,chan=6,psk=mypassword",
        "--wifi-nearby",
        "ssid=cafe,chan=1,rssi=-60,secure=0",
    ]


def test_runner_command_gives_the_chip_the_profile_s_psram(tmp_path: Path) -> None:
    fake_build(tmp_path)
    app = esp32sim.load_app(str(tmp_path))
    command = esp32sim.runner_command(
        Path("/r/runner"), Path("/r/rom.elf"), app, 300, Path("/l.sock"), psram_mb=4
    )
    assert command[-2:] == ["--psram-mb", "4"]
    # A profile that does not say leaves the runner's default.
    command = esp32sim.runner_command(
        Path("/r/runner"), Path("/r/rom.elf"), app, 300, Path("/l.sock")
    )
    assert "--psram-mb" not in command


def test_load_app_refuses_what_the_runner_cannot_run(tmp_path: Path) -> None:
    with pytest.raises(BackendError, match=r"holds no ESP-IDF build: no flasher_args\.json"):
        esp32sim.load_app(str(tmp_path / "none"))

    fake_build(tmp_path / "s3", target="esp32s3")
    with pytest.raises(BackendError, match="is built for esp32s3; esp32sim runs the classic esp32"):
        esp32sim.load_app(str(tmp_path / "s3"))

    fake_build(tmp_path / "encrypted", encrypted=True)
    with pytest.raises(BackendError, match="asks for flash encryption"):
        esp32sim.load_app(str(tmp_path / "encrypted"))

    build = fake_build(tmp_path / "unfinished")
    (build / "app.bin").unlink()
    with pytest.raises(BackendError, match=r"is not a finished build, missing .*/app\.bin$"):
        esp32sim.load_app(str(tmp_path / "unfinished"))


@pytest.mark.parametrize("absolute", [False, True])
def test_load_app_selects_only_the_explicit_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, absolute: bool
) -> None:
    app_path = tmp_path / "app source"
    default = fake_build(app_path)
    selected = fake_build(app_path, build_dir="../CI builds/debug").resolve()
    (default / "app.bin").write_bytes(b"default")
    (selected / "app.bin").write_bytes(b"debug")
    # A third build in cwd must never win, even when the selected one is missing.
    cwd = tmp_path / "unrelated"
    fake_build(cwd)
    monkeypatch.chdir(cwd)
    selector = selected if absolute else Path("../CI builds/debug")
    app = esp32sim.load_app("../app source", build_dir=selector)
    assert app.binary_path is not None
    assert app.bin_file is not None
    assert Path(app.binary_path) == selected
    assert Path(app.bin_file).read_bytes() == b"debug"
    command = esp32sim.runner_command(Path("runner"), Path("rom"), app, 1, Path("link"))
    assert [command[i + 1] for i, value in enumerate(command) if value == "--flash-at"] == [
        f"0x1000={selected}/bootloader/bootloader.bin",
        f"0x8000={selected}/partition_table/partition-table.bin",
        f"0x10000={selected}/app.bin",
    ]
    (selected / "flasher_args.json").unlink()
    with pytest.raises(BackendError, match=re.escape(str(selected)) + " holds no ESP-IDF build"):
        esp32sim.load_app("../app source", build_dir=selector)
    assert (default / "app.bin").read_bytes() == b"default"


@pytest.mark.parametrize("problem", ["target", "encryption", "missing"])
def test_selected_build_keeps_validation(tmp_path: Path, problem: str) -> None:
    fake_build(tmp_path)
    selected = fake_build(
        tmp_path,
        target="esp32s3" if problem == "target" else "esp32",
        encrypted=problem == "encryption",
        build_dir="external build",
    )
    if problem == "missing":
        (selected / "bootloader/bootloader.bin").unlink()
    with pytest.raises(BackendError, match=re.escape(str(selected))):
        esp32sim.load_app(str(tmp_path), build_dir="external build")


@pytest.mark.parametrize(
    "marker",
    [
        'app_path(".", build_dir=None)',
        'app_path(".", build_dir=42)',
        'app_path(".", build_dir="")',
        'app_path(".", unexpected="build")',
        "app_path(42)",
    ],
)
def test_app_path_refuses_invalid_build_selectors(pytester: pytest.Pytester, marker: str) -> None:
    pytester.makepyfile(f"import pytest\n@pytest.mark.{marker}\ndef test_one(board): pass")
    result = pytester.runpytest_subprocess("-q")
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*the app_path marker takes one path,*"])


def test_a_late_build_keyword_is_refused(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(
        SHOW_PARAMS
        + """
def pytest_collection_modifyitems(items):
    items[0].get_closest_marker("app_path").kwargs["build_dir"] = "other"
"""
    )
    pytester.makepyfile("""
        import pytest
        @pytest.mark.app_path(".", build_dir="build")
        def test_one(board): pass
    """)
    result = pytester.runpytest_subprocess("-q")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*the app_path marker reached the test after*"])


def test_board_loads_each_selected_build_from_another_directory(
    fake_runner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = fake_runner.path / "app source"
    for name in ("build", "../CI builds/debug", "../CI builds/release"):
        build = fake_build(app, build_dir=name)
        (build / "app.bin").write_bytes(name.encode())
    suite = fake_runner.path / "suite"
    suite.mkdir()
    (suite / "test_builds.py").write_text("""
from pathlib import Path
import pytest

pytestmark = pytest.mark.app_path("../app source", build_dir="../CI builds/debug")

def check(board, build_dir, expected):
    assert Path(board.app.binary_path) == Path(build_dir)
    assert Path(build_dir).is_absolute()
    assert Path(board.dut.app.binary_path) == Path(build_dir)
    assert Path(board.app.bin_file).read_bytes() == expected.encode()

def test_debug(board, build_dir):
    check(board, build_dir, "../CI builds/debug")

@pytest.mark.app_path("../app source", build_dir=Path("../CI builds/release"))
class TestRelease:
    def test_release(self, board, build_dir):
        check(board, build_dir, "../CI builds/release")

    @pytest.mark.app_path("../app source")
    def test_default(self, board, build_dir):
        check(board, build_dir, "build")
""")
    cwd = fake_runner.path / "other cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    fake_runner.runpytest_subprocess(str(suite), "-q").assert_outcomes(passed=3)


def test_rom_is_the_one_make_setup_fetches() -> None:
    script = (ROOT / "scripts" / "fetch-rom-elf").read_text()
    assert f"\nRELEASE={esp32sim.ROM_RELEASE}\n" in script
    assert f"\nROM={esp32sim.ROM_ELF}\n" in script


def test_runner_and_rom_paths_name_what_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ESP32TL_RUNNER", str(tmp_path / "runner"))
    monkeypatch.setenv("ESP32TL_ROM", str(tmp_path / "rom.elf"))
    with pytest.raises(
        BackendError, match=r"no runner at .*: run make setup, or set ESP32TL_RUNNER"
    ):
        esp32sim.runner_path()
    with pytest.raises(BackendError, match=r"no ROM ELF at .*: run make setup, or set ESP32TL_ROM"):
        esp32sim.rom_path()
    (tmp_path / "runner").touch()
    (tmp_path / "rom.elf").touch()
    with pytest.raises(BackendError, match="no runner at"):
        esp32sim.runner_path()
    (tmp_path / "runner").chmod(0o755)
    assert esp32sim.runner_path() == tmp_path / "runner"
    assert esp32sim.rom_path() == tmp_path / "rom.elf"


# $FAKE_EXIT KILL: killed by SIGKILL, as by the OOM killer. $FAKE_RAN, when set, is a file the
# runner appends to when it starts.
FAKE_RUNNER = """#!/bin/sh
[ -z "$FAKE_RAN" ] || echo started >> "$FAKE_RAN"
printf '%s' "$FAKE_OUTPUT"
[ "$FAKE_EXIT" != KILL ] || kill -KILL $$
exit "$FAKE_EXIT"
"""


@pytest.fixture
def fake_runner(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> pytest.Pytester:
    """A suite of one test, which powers the board on and expects `Hello world!`, on an app with
    a fake build, run by a fake runner that prints $FAKE_OUTPUT and exits with $FAKE_EXIT
    without connecting. The generated GNSS recording is in the suite's directory."""
    runner = pytester.path / "runner"
    runner.write_text(FAKE_RUNNER)
    runner.chmod(0o755)
    (pytester.path / "rom.elf").touch()
    monkeypatch.setenv("ESP32TL_RUNNER", str(runner))
    monkeypatch.setenv("ESP32TL_ROM", str(pytester.path / "rom.elf"))
    fake_build(pytester.path)
    shutil.copy(ROOT / "recordings" / "gnss-generated-1.jsonl", pytester.path / "gnss.jsonl")
    pytester.makepyfile(
        test_fake="""
        def test_hello(board, console):
            board.power_on()
            console.expect("Hello world!")
        """
    )
    return pytester


def test_a_wait_fails_at_once_when_the_runner_has_exited(
    fake_runner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_OUTPUT", "Goodbye\n")
    monkeypatch.setenv("FAKE_EXIT", "0")
    started = time.monotonic()
    result = fake_runner.runpytest_subprocess("-p", "no:cacheprovider")
    assert time.monotonic() - started < 15
    result.assert_outcomes(failed=1)
    # The runner never took a turn: the board's clock is still at 0.
    result.stdout.fnmatch_lines(
        [
            "E *RunEnded: the run ended before its first turn while the test waited for"
            " 'Hello world!' on the console: the runner exited with code 0; its last output:",
            "E *Goodbye",
        ]
    )


def test_the_board_s_power_and_reset_after_the_run_ended_raise_run_ended_and_log_nothing(
    fake_runner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_EXIT", "0")
    fake_runner.makepyfile(
        test_fake="""
        import pytest
        from esp32tl.interfaces import RunEnded

        def test_after_the_end(board, console):
            board.power_on()
            with pytest.raises(RunEnded):
                console.expect("Hello world!")
            for step in (board.reset, board.power_off):
                with pytest.raises(RunEnded, match=r"before board\\.(reset|power_off)\\(\\)"):
                    step()
            assert board.powered
            assert "board.power_off()" not in board.report(run=True)
            assert "board.reset()" not in board.report(run=True)
        """
    )
    result = fake_runner.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)


def test_a_runner_that_fails_to_start_is_reported_once(
    fake_runner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_OUTPUT", "esp32tl-runner: rom.elf: not an ELF file\n")
    monkeypatch.setenv("FAKE_EXIT", "2")
    result = fake_runner.runpytest_subprocess("-p", "no:cacheprovider")
    # The RunEnded names the exit code, so the teardown does not report it again.
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "E *RunEnded: * the runner exited with code 2; its last output:",
            "E *esp32tl-runner: rom.elf: not an ELF file",
        ]
    )


def test_the_board_runs_the_profile_the_test_chose_and_starts_no_runner_unpowered(
    fake_runner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_EXIT", "0")
    monkeypatch.setenv("FAKE_RAN", str(fake_runner.path / "ran"))
    fake_runner.makepyfile(
        test_profiles="""
        import pytest

        def test_default(board):
            assert board.profile.name == "esp32-devkitc"

        @pytest.mark.board_profile("lilygo-t-a7670g-r2")
        def test_chosen(board):
            assert board.profile.uart["modem"].baud == 115200
        """
    )
    result = fake_runner.runpytest_subprocess("-p", "no:cacheprovider", "test_profiles.py")
    # Neither set anything up for a run, so neither needs one.
    result.assert_outcomes(passed=2)
    assert not (fake_runner.path / "ran").exists()


def test_the_runner_starts_at_power_on_on_the_test_s_clock(
    fake_runner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_EXIT", "0")
    (fake_runner.path / "runner").write_text(
        FAKE_RUNNER.replace('echo started >> "$FAKE_RAN"', 'echo "$@" >> "$FAKE_RAN"')
    )
    monkeypatch.setenv("FAKE_RAN", str(fake_runner.path / "ran"))
    fake_runner.makepyfile(
        test_fake="""
        import pexpect

        def test_power_on(board, console):
            board.power_on()
            assert board.powered
            console.expect(pexpect.EOF)
        """
    )
    result = fake_runner.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    (line,) = (fake_runner.path / "ran").read_text().splitlines()
    assert " --max-seconds 86400 --console both --peer-timeout 0 --board socket:" in line


@pytest.mark.parametrize(("exit", "code"), [("1", "1"), ("KILL", "-9")])
def test_a_runner_that_exits_with_an_error_fails_the_test(
    fake_runner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, exit: str, code: str
) -> None:
    monkeypatch.setenv("FAKE_EXIT", exit)
    # Waits for the runner's end, so that it has exited before the teardown.
    fake_runner.makepyfile(
        test_fake="""
        import pexpect

        def test_end(board, console):
            board.power_on()
            console.expect(pexpect.EOF)
        """
    )
    result = fake_runner.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(
        [f"*esp32sim: the runner exited with code {code}; its output is in *"]
    )


def test_a_failure_of_another_kind_does_not_hide_the_runner_s_exit(
    fake_runner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_EXIT", "1")
    fake_runner.makepyfile(
        test_fake="""
        import pexpect

        def test_end(board, console):
            board.power_on()
            console.expect(pexpect.EOF)
            raise pexpect.EOF("a host tool's end")
        """
    )
    result = fake_runner.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(failed=1, errors=1)
    result.stdout.fnmatch_lines(["*esp32sim: the runner exited with code 1; its output is in *"])


def test_the_board_s_clock_and_sources_go_in_order(
    fake_runner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_EXIT", "0")
    fake_runner.makepyfile(
        test_fake="""
        from esp32tl.units import seconds

        def test_wait_while_off(board):
            board.wait(seconds(1))

        def test_expect_while_off(board, console):
            console.expect("x")

        def test_now_while_off(board):
            board.now

        def test_power_on_twice(board):
            board.power_on()
            board.power_on()

        def test_replay_after_power_on(board, gnss):
            board.power_on()
            gnss.replay("gnss.jsonl")

        def test_two_replays(gnss):
            gnss.replay("gnss.jsonl")
            gnss.replay("gnss.jsonl")

        def test_tap_after_power_on(board, gnss):
            board.power_on()
            gnss.tap("tap.jsonl")

        def test_two_taps(gnss):
            gnss.tap("tap.jsonl")
            gnss.tap("tap.jsonl")

        def test_tap_into_no_directory(gnss):
            gnss.tap("none/tap.jsonl")

        def test_model_after_power_on(board, gnss):
            board.power_on()
            gnss.lose_fix()

        def test_model_after_a_replay(gnss):
            gnss.replay("gnss.jsonl")
            gnss.go_silent()

        def test_replay_after_a_model(gnss):
            gnss.go_silent()
            gnss.replay("gnss.jsonl")
        """
    )
    result = fake_runner.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(failed=12)
    result.stdout.fnmatch_lines_random(
        [
            "E *esp32tl.interfaces.BoardOff: the board is off; call board.power_on() first",
            "E *esp32tl.interfaces.BoardOff: the board is on already: call board.power_off() first",
            "E *esp32tl.interfaces.SourceError: uart.gnss: the board is on already: give an"
            " interface its source before board.power_on()",
            "E *esp32tl.interfaces.SourceError: uart.gnss has its source already: one per test",
            "E *esp32tl.interfaces.SourceError: uart.gnss: the board is on already: tap a route"
            " before board.power_on()",
            "E *esp32tl.interfaces.SourceError: uart.gnss is tapped already: one tap per test",
            "E *FileNotFoundError: uart.gnss: cannot tap into none/tap.jsonl: */none is not a"
            " directory",
            "E *esp32tl.interfaces.SourceError: uart.gnss replays a recording: it has no receiver"
            " to describe; one source per test",
        ]
    )
    # The replay and the model after power-on.
    late = re.findall(
        r"^E +.*SourceError: uart.gnss: the board is on already: give", result.stdout.str(), re.M
    )
    assert len(late) == 2
    # The second replay and the replay after a model.
    second = re.findall(r"^E +.*SourceError: uart.gnss has its source", result.stdout.str(), re.M)
    assert len(second) == 2
    # The wait, the expect and board.now.
    off = re.findall(
        r"^E +esp32tl\.interfaces\.BoardOff: the board is off;", result.stdout.str(), re.M
    )
    assert len(off) == 3


# esp32sim with a model source only on its UARTs, and a backend that takes replays.
NO_REPLAY = """
[esp32sim]
uart = {{ sources = ["model"] }}
{other}
"""


@pytest.mark.parametrize(
    ("other", "outcome", "lines"),
    [
        (
            '[serial]\nuart = { sources = ["replay"], tap = true }',
            {"skipped": 2},
            # At the test, not at the line of esp32tl's that skipped it.
            [
                "SKIPPED [[]1[]] test_fake.py:1: esp32sim: uart.gnss takes no replay source on"
                " this backend, only model",
                "SKIPPED [[]1[]] test_fake.py:5: esp32sim: uart.gnss cannot be tapped on this"
                " backend",
            ],
        ),
        # A source or a tap no backend gives is wrong, not a skip everywhere.
        (
            "",
            {"failed": 2},
            [
                "E *esp32tl.interfaces.SourceError: no backend takes a 'replay' source on"
                " uart.gnss; the sources there are model",
                "E *esp32tl.interfaces.SourceError: no backend can tap uart.gnss",
            ],
        ),
    ],
)
def test_a_source_or_a_tap_the_backend_cannot_give_skips_at_the_call(
    fake_runner: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    other: str,
    outcome: dict[str, int],
    lines: list[str],
) -> None:
    monkeypatch.setenv("FAKE_EXIT", "0")
    (fake_runner.path / "capabilities.toml").write_text(NO_REPLAY.format(other=other))
    fake_runner.makeconftest(
        """
        from pathlib import Path

        from esp32tl.boards import capabilities

        DECLARATION = Path(__file__).parent / "capabilities.toml"
        capabilities.declared = lambda: capabilities.load(DECLARATION)
        """
    )
    fake_runner.makepyfile(
        test_fake="""
        def test_replay(board, gnss):
            gnss.replay("gnss.jsonl")
            board.power_on()

        def test_tap(board, gnss):
            gnss.tap("tap.jsonl")
            board.power_on()
        """
    )
    result = fake_runner.runpytest_subprocess("-p", "no:cacheprovider", "-rA")
    result.assert_outcomes(**outcome)
    result.stdout.fnmatch_lines_random(lines)


def test_a_model_the_backend_cannot_give_skips_at_the_call(
    fake_runner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_EXIT", "0")
    (fake_runner.path / "capabilities.toml").write_text(
        '[esp32sim]\nuart = { sources = ["replay"] }\n[serial]\nuart = { sources = ["model"] }\n'
    )
    fake_runner.makeconftest(
        """
        from pathlib import Path

        from esp32tl.boards import capabilities

        DECLARATION = Path(__file__).parent / "capabilities.toml"
        capabilities.declared = lambda: capabilities.load(DECLARATION)
        """
    )
    fake_runner.makepyfile(
        test_fake="""
        from esp32tl.units import Position

        def test_model(board, gnss):
            gnss.fix(at=Position(52.2, 21.0))
            board.power_on()
        """
    )
    result = fake_runner.runpytest_subprocess("-p", "no:cacheprovider", "-rA")
    result.assert_outcomes(skipped=1)
    result.stdout.fnmatch_lines(
        [
            "SKIPPED [[]1[]] test_fake.py:3: esp32sim: uart.gnss takes no model source on this"
            " backend, only replay"
        ]
    )


def test_a_test_that_sets_up_a_source_and_never_powers_on_fails_at_teardown(
    fake_runner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_EXIT", "0")
    monkeypatch.setenv("FAKE_RAN", str(fake_runner.path / "ran"))
    fake_runner.makepyfile(
        test_fake="""
        import pytest

        @pytest.fixture
        def replayed(gnss):
            gnss.replay("gnss.jsonl")

        def test_never_powered(replayed):
            pass

        def test_failed_first(replayed):
            raise AssertionError("the test's own failure")

        def test_skipped(replayed):
            pytest.skip("not on this firmware")

        @pytest.mark.xfail(reason="known")
        def test_xfailed(replayed):
            raise AssertionError("expected")

        @pytest.fixture
        def broken():
            raise RuntimeError("a fixture of the test's own")

        def test_broken_setup(replayed, broken):
            pass

        def test_modelled(gnss):
            gnss.lose_fix()
        """
    )
    result = fake_runner.runpytest_subprocess(
        "-p", "no:cacheprovider", "-rA", "--junitxml", "report.xml"
    )
    # Only the test that passed is reported: the failed, skipped, xfailed and broken ones not a
    # second time, each with its one outcome.
    result.assert_outcomes(passed=2, failed=1, skipped=1, xfailed=1, errors=3)
    result.stdout.fnmatch_lines_random(
        [
            "ERROR test_fake.py::test_never_powered[[]esp32sim[]] - Failed: esp32sim*",
            "ERROR test_fake.py::test_broken_setup[[]esp32sim[]] - RuntimeError*",
            "ERROR test_fake.py::test_modelled[[]esp32sim[]] - Failed: esp32sim*",
        ]
    )
    result.stdout.fnmatch_lines_random(
        [
            "*esp32sim: the test set up the replay of uart.gnss and never called"
            " board.power_on(): it ran no firmware",
            "*esp32sim: the test set up the model of uart.gnss and never called"
            " board.power_on(): it ran no firmware",
        ]
    )
    # No runner ran, and every link report says so.
    assert not (fake_runner.path / "ran").exists()
    stats = [
        json.loads(prop.get("value", ""))["stats"]
        for prop in ElementTree.parse(fake_runner.path / "report.xml").iter("property")
        if prop.get("name") == "esp32sim_link"
    ]
    assert stats == [None] * 6


def test_the_lines_fixture_needs_a_profile_with_lines_and_a_drive_needs_power_on(
    fake_runner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_EXIT", "0")
    monkeypatch.setenv("FAKE_RAN", str(fake_runner.path / "ran"))
    fake_runner.makepyfile(
        test_fake="""
        import pytest

        def test_no_lines(lines):
            pass

        @pytest.mark.board_profile("lilygo-t-a7670g-r2")
        def test_read_only(board, lines):
            assert lines.level("modem.pwrkey") is None
            assert lines.events() == []

        @pytest.mark.board_profile("lilygo-t-a7670g-r2")
        def test_driven_never_powered(board, lines):
            lines.drive("modem.ri", 1)
            lines.drive(4, 0)
        """
    )
    result = fake_runner.runpytest_subprocess("-p", "no:cacheprovider", "-rA")
    result.assert_outcomes(passed=2, errors=2)
    result.stdout.fnmatch_lines_random(
        [
            "*esp32sim: the board profile esp32-devkitc has no lines;"
            " its interfaces are console, uart.gnss, sd_card, wifi",
            "*esp32sim: the test set up the level of modem.ri and the level of modem.pwrkey and"
            " never called board.power_on(): it ran no firmware",
        ]
    )
    assert not (fake_runner.path / "ran").exists()


def test_a_test_that_taps_a_route_and_never_powers_on_fails_at_teardown(
    fake_runner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_EXIT", "0")
    fake_runner.makepyfile(test_fake="def test_tapped(gnss):\n    gnss.tap('tap.jsonl')\n")
    result = fake_runner.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(
        [
            "*esp32sim: the test set up the tap of uart.gnss and never called board.power_on():"
            " it ran no firmware",
        ]
    )
    # Nothing ran, so nothing is written.
    assert not (fake_runner.path / "tap.jsonl").exists()


def test_a_test_that_uses_board_gets_a_wall_time_timeout_unless_one_is_set(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makeconftest(SHOW_PARAMS)
    pytester.makepyfile(
        test_timeouts="""
        import os

        import pytest

        def timeout(request):
            marker = request.node.get_closest_marker("timeout")
            return marker.args if marker else None

        def test_board(board, request):
            expected = os.environ["EXPECTED"]
            assert timeout(request) == ((int(expected),) if expected else None)

        @pytest.mark.timeout(3)
        def test_own(board, request):
            assert timeout(request) == (3,)

        def test_without_board(request):
            assert timeout(request) is None
        """
    )
    monkeypatch.setenv("EXPECTED", "600")
    pytester.runpytest_subprocess("-p", "no:cacheprovider").assert_outcomes(passed=3)
    # Set for the session: the user's choice, 0 included.
    monkeypatch.setenv("EXPECTED", "")
    for options in (("--timeout", "5"), ("-o", "timeout=0"), ("-p", "no:timeout")):
        result = pytester.runpytest_subprocess("-p", "no:cacheprovider", *options)
        result.assert_outcomes(passed=3)
    monkeypatch.setenv("PYTEST_TIMEOUT", "0")
    pytester.runpytest_subprocess("-p", "no:cacheprovider").assert_outcomes(passed=3)


def test_the_eof_tail_is_the_runner_s_last_whole_lines_without_colour(tmp_path: Path) -> None:
    log = tmp_path / "runner.log"
    log.write_bytes(b"".join(b"\x1b[0;32mline %d\x1b[0m\n" % i for i in range(200)))
    lines = esp32sim.last_output(log).splitlines()
    # 2 KiB of output, from the first line that begins in it
    assert lines[-1] == "line 199"
    assert all(re.fullmatch(r"line \d+", line) for line in lines)
    assert len("\n".join(lines)) < esp32sim.LAST_OUTPUT_BYTES


# ESP-IDF ends its lines with \r\n by default.
@pytest.mark.parametrize("ending", [b"", b"\n", b"\r\n"])
def test_the_eof_tail_keeps_a_long_last_line(tmp_path: Path, ending: bytes) -> None:
    log = tmp_path / "runner.log"
    log.write_bytes(b"x\n" + b"y" * 3000 + ending)
    tail = esp32sim.last_output(log)
    assert tail.strip().strip("y") == "" and len(tail) > 1000
    assert esp32sim.last_output(tmp_path / "none.log").startswith("(the log ")


def test_ended_while_waiting_stops_at_a_cycle() -> None:
    first, second = ValueError("first"), ValueError("second")
    first.__cause__, second.__cause__ = second, first
    assert not esp32sim.ended_while_waiting(first)
    second.__cause__ = RunEnded("end")
    assert esp32sim.ended_while_waiting(first)


def test_a_runner_that_cannot_start_leaves_no_log_open(tmp_path: Path) -> None:
    # Executable, but not a program: exec fails with ENOEXEC.
    runner = tmp_path / "runner"
    runner.write_text("not a program\n")
    runner.chmod(0o755)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(OSError, match="Exec format error"):
            esp32sim.RunnerPopen(cast(MessageQueue, None), [str(runner)], cast(Meta, None))
        gc.collect()
    assert [w for w in caught if issubclass(w.category, ResourceWarning)] == []


def copy_build(build: Path, to: Path) -> None:
    """What the runner and `IdfApp` read from `build`: what CI's firmware tarball carries."""
    flasher_args = json.loads((build / "flasher_args.json").read_text())
    description = json.loads((build / "project_description.json").read_text())
    names = ["flasher_args.json", "config/sdkconfig.json", description["app_elf"]]
    for name in [*names, *flasher_args["flash_files"].values()]:
        (to / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(build / name, to / name)


def replace_in_app_image(path: Path, old: bytes, new: bytes) -> None:
    """Replaces `old` with `new`, of the same length, in an app image with an appended SHA-256,
    and makes its checksum and SHA-256 again, so that the bootloader still boots it.

    The checksum is the XOR of every segment byte, so the change XORs into the stored one. esptool
    checks the result: its own `save()` lays the segments out again rather than keeping them.
    """
    data = bytearray(path.read_bytes())
    assert data.count(old) == 1, f"{old!r} is in {path} {data.count(old)} times"
    at = data.index(old)
    data[at : at + len(old)] = new
    # [... segments, padding, checksum byte][SHA-256 of all that]
    for byte in old + new:
        data[-33] ^= byte
    data[-32:] = hashlib.sha256(data[:-32]).digest()
    path.write_bytes(data)
    image = LoadFirmwareImage("esp32", str(path))
    assert image.append_digest
    assert image.checksum == image.calculate_checksum()
    assert image.stored_digest == image.calc_digest


def test_hello_world_fails_on_an_image_that_prints_something_else(
    pytester: pytest.Pytester,
) -> None:
    if not (HELLO_WORLD_BUILD / "esp32tl-built").is_file():
        pytest.fail(
            f"{HELLO_WORLD_BUILD} holds no finished build: run make firmware", pytrace=False
        )
    build = pytester.path / "firmware" / "hello_world" / "build"
    copy_build(HELLO_WORLD_BUILD, build)
    app = json.loads((build / "flasher_args.json").read_text())["app"]["file"]
    replace_in_app_image(build / app, b"Hello world!", b"Hello WORLD!")
    (pytester.path / "tests").mkdir()
    shutil.copy(ROOT / "tests" / "test_hello_world.py", pytester.path / "tests")
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "tests")
    result.assert_outcomes(failed=1)
    # The image ran, and printed what it was changed to print.
    result.stdout.fnmatch_lines(["*Hello WORLD!*"])


def test_two_builds_of_one_app_run_the_selected_firmware(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not (HELLO_WORLD_BUILD / "esp32tl-built").is_file():
        pytest.fail(
            f"{HELLO_WORLD_BUILD} holds no finished build: run make firmware", pytrace=False
        )
    app = pytester.path / "app source"
    copy_build(HELLO_WORLD_BUILD, app / "build")
    debug = pytester.path / "CI builds" / "debug"
    release = pytester.path / "CI builds" / "release"
    for build in (debug, release):
        copy_build(HELLO_WORLD_BUILD, build)
    image = json.loads((debug / "flasher_args.json").read_text())["app"]["file"]
    replace_in_app_image(debug / image, b"Hello world!", b"Hello DEBUG!")
    replace_in_app_image(release / image, b"Hello world!", b"Hello FINAL!")
    suite = pytester.path / "suite"
    suite.mkdir()
    (suite / "test_builds.py").write_text(f"""
import pytest
from pathlib import Path

@pytest.mark.app_path("../app source", build_dir="../CI builds/debug")
def test_debug(board, console):
    console.tap("debug.jsonl")
    board.power_on()
    console.expect("Hello DEBUG!")

@pytest.mark.app_path(Path({str(app)!r}), build_dir=Path({str(release)!r}))
def test_release(board, console):
    console.tap("release.jsonl")
    board.power_on()
    console.expect("Hello FINAL!")
""")
    cwd = pytester.path / "other cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    pytester.runpytest_subprocess(str(suite), "-q").assert_outcomes(passed=2)
    for build in (debug, release):
        tapped = recording.read(suite / f"{build.name}.jsonl")
        assert tapped.header.app_sha256 == hashlib.sha256((build / image).read_bytes()).hexdigest()
    assert b"Hello world!" in (app / "build" / image).read_bytes()


# A runner that speaks the link protocol, as the backend sees one, without an emulator: it
# connects once, shakes hands, takes turn 0, prints the console line, as the console message and on
# stdout, sends a warning and takes the next turn at the harness's quiet_until, and so on until the
# harness's stop or SIGTERM, on which it ends the run with its stats and exits 0, as protocol §9 has
# it. It drops the inputs the harness sends in its turns. $FAKE_LINK changes that: `tx` also has
# the firmware transmit a PMTK command on the GNSS route's TX pin after the console line; the
# others make it misbehave: `version` sends a hello of version 2, and exits 3 once it has
# the harness's answer; `late` sends an event out of sequence after the console line; `deaf`
# ignores the harness and SIGTERM after that line; `stall` takes no turn after it until SIGTERM;
# `linger` leaves a child holding the link open for 30 s after it exits, and writes the child's pid
# to `linger.pid` beside the runner; `slow` loads for half a second before it connects, and dies of
# SIGTERM until then, as the real runner does; `gone` exits 4 in turn 0 without reading it, as the
# real runner does when the harness is gone. $FAKE_WATCH is the pins the harness's hello must
# watch, comma-separated: the default profile's GNSS route's. $FAKE_PSRAM is the --psram-mb the
# runner must be given: the default profile's 0. $FAKE_SD is the --sd-spi and --sd-rw it must be
# given, one per line: by default the default profile's empty slot, its --sd-spi alone.
LINK_RUNNER = """#!{python}
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from esp32tl.link import protocol as p

if os.environ.get("FAKE_LINK") == "slow":
    time.sleep(0.5)
signal.pthread_sigmask(signal.SIG_BLOCK, {{signal.SIGTERM}})
args = sys.argv[1:]
assert args[args.index("--psram-mb") + 1] == os.environ.get("FAKE_PSRAM", "0"), args
sd = [args[args.index(flag) + 1] for flag in ("--sd-spi", "--sd-rw") if flag in args]
assert "\\n".join(sd) == os.environ.get("FAKE_SD", "sclk=14,mosi=15,miso=2,cs=13"), args
board = args[args.index("--board") + 1]
conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
conn.connect(board.removeprefix("socket:"))
lines = conn.makefile("rb")
mode = os.environ.get("FAKE_LINK", "")
# The time of the last message sent, which the stats carry.
now = 0


def send(*messages):
    global now
    now = max([now, *(getattr(message, "t", 0) for message in messages)])
    conn.sendall(b"".join(p.encode(message) for message in messages))


def end(how, code, detail=None):
    send(p.Stats(now, how, 0, 1, (), 72, 0, 0, 0, detail=detail))
    if mode == "linger":
        os.set_inheritable(conn.fileno(), True)
        child = subprocess.Popen(["sleep", "30"], close_fds=False)
        Path(sys.argv[0]).with_name("linger.pid").write_text(str(child.pid))
    conn.close()
    print(f"esp32tl-runner: the run ended: {{how}}", file=sys.stderr)
    sys.exit(code)


def turn():
    message = None
    while not isinstance(message, p.Wait):
        message = p.decode_harness(lines.readline())
        if message == p.Stop():
            end("stop", 0)
    assert message.quiet_until is not None, message
    return message.quiet_until


send(p.RunnerHello(2 if mode == "version" else 1, "esp32", "emulated-fifo-write", "0" * 40))
hello = p.decode_harness(lines.readline())
board_profile = os.environ.get("FAKE_BOARD", "esp32-devkitc")
watch = tuple(int(pin) for pin in os.environ.get("FAKE_WATCH", "4,5").split(",") if pin)
assert (hello.board, hello.chip, hello.watch) == (board_profile, "esp32", watch), hello
if mode == "version":
    end("protocol_error", 3, detail="version 1 from the peer, the runner speaks 2")
send(p.Sync(0))
if mode == "gone":
    print("esp32tl-runner: the peer is gone", file=sys.stderr)
    conn.close()
    sys.exit(4)
t = turn()
print("Hello world!", flush=True)
send(p.Console(1000, b"Hello world!\\r\\n"))
send(p.Warn(1000, "unrouted_rx", "uart_rx on GPIO 5: 72 bytes dropped", pin=5, nbytes=72))
if mode == "late":
    send(p.Reset(5, 2000, 12))
if mode == "tx":
    send(p.UartTx(1, 1000, 1, (4,), 9600, b"$PMTK314,0*28\\r\\n"))
if mode == "stall":
    signal.sigwait({{signal.SIGTERM}})
    end("signal", 0)
send(p.Sync(t))
if mode == "deaf":
    time.sleep(60)
while True:
    send(p.Sync(turn()))
"""


@pytest.fixture
def link_runner(fake_runner: pytest.Pytester) -> pytest.Pytester:
    """`fake_runner`'s suite, run by `LINK_RUNNER`."""
    runner = fake_runner.path / "runner"
    runner.write_text(LINK_RUNNER.format(python=sys.executable))
    return fake_runner


def test_the_backend_serves_the_link_and_ends_the_run_with_stop(
    link_runner: pytest.Pytester,
) -> None:
    result = link_runner.runpytest_subprocess(
        "-p", "no:cacheprovider", "-rA", "--junitxml", "report.xml"
    )
    result.assert_outcomes(passed=1)
    # The user property is JSON, for code to read.
    (prop,) = ElementTree.parse(link_runner.path / "report.xml").iter("property")
    assert prop.get("name") == "esp32sim_link"
    data = json.loads(prop.get("value", ""))
    assert data["stats"]["end"] == "stop"
    assert data["warnings"][0]["bytes"] == 72
    # The warning and the stats are in the report: the run stopped at the turn the expect
    # matched at, the second.
    result.stdout.fnmatch_lines(
        [
            "*Captured esp32sim link teardown*",
            "board esp32-devkitc, watching GPIO 4, GPIO 5",
            "runner: protocol version 1, esp32sim 0000*, clock emulated-fifo-write; 2 turns taken*",
            "warnings (1):",
            "  0.000001000 s  unrouted_rx: uart_rx on GPIO 5: 72 bytes dropped",
            "stats: the run ended at 0.100000000 s (stop); 1 turn, 0 resets",
            "  unrouted_rx_bytes 72, *",
        ]
    )


@pytest.mark.parametrize("with_card", [False, True])
def test_every_route_and_line_of_the_profile_is_on_the_link(
    link_runner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, with_card: bool
) -> None:
    monkeypatch.setenv("FAKE_BOARD", "lilygo-t-a7670g-r2")
    monkeypatch.setenv("FAKE_WATCH", "4,5,12,19,21,22,23,25,26,27,33")
    monkeypatch.setenv("FAKE_PSRAM", "4")
    image = link_runner.path / "card.img"
    image.write_bytes(bytes(512 << 10))
    if with_card:
        monkeypatch.setenv("FAKE_SD", f"sclk=14,mosi=15,miso=2,cs=13\n{image}")
    link_runner.makepyfile(
        test_lilygo=f"""
        import pytest

        @pytest.mark.board_profile("lilygo-t-a7670g-r2")
        def test_hello(board, console, sd_card, gnss, modem):
            assert sd_card.image_path is None
            if {with_card!r}:
                sd_card.insert("card.img")
            gnss.lose_fix()
            modem.register()
            board.power_on()
            console.expect("Hello world!")
        """
    )
    # The fake runner asserts its --psram-mb and SD slot and the hello's board and pins; a failed
    # assertion ends it with an error.
    result = link_runner.runpytest_subprocess("-p", "no:cacheprovider", "-rA", "test_lilygo.py")
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(
        [
            "board lilygo-t-a7670g-r2, watching GPIO 4, GPIO 5, GPIO 12, GPIO 19, "
            "GPIO 21, GPIO 22, GPIO 23, GPIO 25, GPIO 26, GPIO 27, GPIO 33"
        ]
    )


def test_the_test_s_waits_move_the_board_s_clock(link_runner: pytest.Pytester) -> None:
    link_runner.makepyfile(
        test_fake="""
        import pytest

        from esp32tl.units import ms, seconds

        def test_clock(board, console):
            board.power_on()
            assert board.now == seconds(0)
            board.wait(seconds(2.5))
            assert board.now == ms(2500)
            assert board.wait_until(lambda: b"Hello" in board.console_output, within=seconds(1))
            assert board.now == seconds(2.5)
            assert board.utc.isoformat() == "2026-01-01T12:00:02.500000+00:00"
            # The board's durations carry their unit.
            with pytest.raises(TypeError, match="as seconds.2.5., not 2.5$"):
                board.wait(2.5)
            with pytest.raises(TypeError, match="^board.wait_until: within takes a Duration"):
                board.wait_until(lambda: True, within=1)
            with pytest.raises(ValueError, match="takes a Duration of 0 or more, not -1 s$"):
                board.wait(seconds(-1))
        """
    )
    result = link_runner.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)


# A test that taps the GNSS route: two sentences of the replay go in, at 1 s and 2 s, and the PMTK
# command comes out, at 1 µs.
TAPPED = """
from esp32tl.units import seconds

def test_tapped(board, gnss, console):
    gnss.replay("gnss.jsonl")
    gnss.tap("tap.jsonl")
    board.power_on()
    board.wait(seconds(2.5))
    console.expect("Hello world!")
    {end}
"""


@pytest.mark.parametrize("local", [False, True])
def test_a_tap_records_what_crossed_the_route_both_ways(
    link_runner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, local: bool
) -> None:
    monkeypatch.setenv("FAKE_LINK", "tx")
    profile = "local" if local else "esp32-devkitc"
    marker = ""
    if local:
        (link_runner.path / "local.toml").write_text(
            (ROOT / "esp32tl/boards/esp32-devkitc.toml")
            .read_text()
            .replace("esp32-devkitc", profile)
        )
        marker = 'import pytest\npytestmark = pytest.mark.board_profile(path="local.toml")\n'
        monkeypatch.setenv("FAKE_BOARD", profile)
    link_runner.makepyfile(test_fake=marker + TAPPED.format(end=""))
    result = link_runner.runpytest_subprocess("-p", "no:cacheprovider", "-rA")
    result.assert_outcomes(passed=1)
    tapped = recording.read(link_runner.path / "tap.jsonl")
    replayed = recording.read(link_runner.path / "gnss.jsonl")
    header = tapped.header
    assert (header.board, header.interface, header.source, header.clock, header.end) == (
        profile,
        "uart.gnss",
        "replay",
        "emulated-fifo-write",
        "hold",
    )
    # The fake build's app.bin is empty.
    assert header.app_sha256 == hashlib.sha256(b"").hexdigest()
    assert header.tools == {"esp32tl": esp32tl.__version__, "esp32sim": "0" * 40}
    assert [(e.t, e.dir, e.data) for e in tapped.events] == [
        (1000, "out", b"$PMTK314,0*28\r\n"),
        *((e.t, "in", e.data) for e in replayed.events[:2]),
    ]
    # A passing test's report has the routes, not the run.
    result.stdout.fnmatch_lines(
        [
            "routes:",
            "  uart.gnss: the replay of gnss.jsonl; * bytes in, 15 out; tapped into */tap.jsonl",
        ]
    )
    result.stdout.no_fnmatch_line("the run on the board's clock*")


@pytest.mark.parametrize("end", ["", "assert False, 'after capture'"])
def test_console_tap_preserves_emulated_output(link_runner: pytest.Pytester, end: str) -> None:
    link_runner.makepyfile(
        test_fake=TAPPED.replace('gnss.tap("tap.jsonl")', 'console.tap("console.jsonl")').format(
            end=end
        )
    )
    result = link_runner.runpytest_subprocess(
        "-p", "no:cacheprovider", "-rA", "--junitxml=report.xml"
    )
    result.assert_outcomes(**({"failed": 1} if end else {"passed": 1}))
    captured = recording.read(link_runner.path / "console.jsonl")
    assert captured.header.interface == "console"
    assert captured.header.clock == "emulated-fifo-write"
    assert captured.header.app_sha256 == hashlib.sha256(b"").hexdigest()
    assert b"Hello world!" in b"".join(event.data for event in captured.events)
    assert all(event.dir == "out" for event in captured.events)

    result.stdout.fnmatch_lines(["*console: * bytes out; tapped into */console.jsonl*"])
    if end:
        result.stdout.fnmatch_lines(["*console.tap('console.jsonl')*"])
    root = ElementTree.parse(link_runner.path / "report.xml")
    prop = root.find(".//property[@name='esp32sim_link']")
    assert prop is not None
    console = json.loads(prop.attrib["value"])["console"]
    assert console["tap"] == str(link_runner.path / "console.jsonl")
    assert console["bytes_out"] == sum(len(event.data) for event in captured.events)


def test_a_tap_on_a_modelled_route_records_the_receiver(
    link_runner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_LINK", "tx")
    link_runner.makepyfile(
        test_fake="""
        from esp32tl.units import Position, seconds

        def test_modelled(board, gnss, console):
            gnss.tap("tap.jsonl")
            gnss.fix(at=Position(52.229677, 21.012233))
            board.power_on()
            board.wait(seconds(2.5))
            console.expect("Hello world!")
        """
    )
    result = link_runner.runpytest_subprocess("-p", "no:cacheprovider", "-rA")
    result.assert_outcomes(passed=1)
    tapped = recording.read(link_runner.path / "tap.jsonl")
    # The recording format names a model model:<name>.
    assert tapped.header.source == "model:gnss"
    # The PMTK command out, then the epochs at 1 s and 2 s in, an RMC and a GGA each.
    assert [(e.t, e.dir) for e in tapped.events] == [
        (1000, "out"),
        (10**9, "in"),
        (2 * 10**9, "in"),
    ]
    assert tapped.events[1].data.startswith(b"$GPRMC,120001.00,A,5213.78062,N,")
    result.stdout.fnmatch_lines(
        [
            "routes:",
            "  uart.gnss: the model of a GNSS receiver; * bytes in, 15 out;"
            " tapped into */tap.jsonl",
        ]
    )


def test_the_report_of_a_failing_test_shows_the_run_on_the_board_s_clock(
    link_runner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_LINK", "tx")
    link_runner.makepyfile(
        test_fake=TAPPED.format(end='console.expect("Goodbye", timeout=0.5)'),
    )
    result = link_runner.runpytest_subprocess("-p", "no:cacheprovider", "--junitxml", "report.xml")
    result.assert_outcomes(failed=1)
    # The run, in the order of the board's time: the test's steps, the console, both ways of the
    # route, and the failed expect last.
    result.stdout.fnmatch_lines(
        [
            "*Captured esp32sim link teardown*",
            "board esp32-devkitc, watching GPIO 4, GPIO 5",
            "stats: the run ended at 3.000000000 s (stop); *",
            "routes:",
            "  uart.gnss: the replay of gnss.jsonl; * bytes in, 15 out; tapped into */tap.jsonl",
            "the run on the board's clock, 12 entries:",
            "  0.000000000 s  test           uart.gnss.replay('gnss.jsonl')",
            "  0.000000000 s  test           uart.gnss.tap('tap.jsonl')",
            "  0.000000000 s  test           board.power_on(), the world's time"
            " 2026-01-01T12:00:00Z",
            "  0.000000000 s  test           board.wait(2.5 s)",
            "  0.000001000 s  console        Hello world!",
            "  0.000001000 s  uart.gnss out  $PMTK314,0*28\\r\\n",
            "  1.000000000 s  uart.gnss in   $GPGGA,*\\r\\n",
            "  2.000000000 s  uart.gnss in   $GPRMC,*\\r\\n",
            "  2.500000000 s  test           waits up to 30 s for 'Hello world!' on the console",
            "  2.500000000 s  test           'Hello world!' on the console: happened",
            "  2.500000000 s  test           waits up to 0.5 s for 'Goodbye' on the console",
            # The test failed in the turn at 3 s, before the sentence of 3 s went in.
            "  3.000000000 s  test           'Goodbye' on the console: did not happen within 0.5 s",
        ]
    )
    # The routes are in the user property too.
    (prop,) = ElementTree.parse(link_runner.path / "report.xml").iter("property")
    routes = json.loads(prop.get("value", ""))["routes"]
    assert routes["uart.gnss"] | {"bytes_in": None} == {
        "source": "replay",
        "detail": "gnss.jsonl",
        "bytes_in": None,
        "bytes_out": 15,
        "tap": str(link_runner.path / "tap.jsonl"),
    }
    assert routes["uart.gnss"]["bytes_in"] > 0


def test_a_tap_that_cannot_be_written_fails_the_test_at_teardown(
    link_runner: pytest.Pytester,
) -> None:
    link_runner.makepyfile(
        test_fake="""
        from pathlib import Path

        def test_tapped(board, gnss, console):
            gnss.tap("tap.jsonl")
            (Path(__file__).parent / "tap.jsonl").mkdir()
            board.power_on()
            console.expect("Hello world!")
        """
    )
    result = link_runner.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(
        [
            "*esp32sim: the tap of uart.gnss cannot be written: *tap.jsonl*; its output is in *",
            # The teardown failed, so the report shows the run.
            "the run on the board's clock, * entries:",
        ]
    )


def test_a_mismatched_handshake_fails_the_test_with_both_sides_reasons(
    link_runner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_LINK", "version")
    result = link_runner.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(failed=1)
    # The runner had the harness's hello: it ended the run with its stats, which the harness read.
    result.stdout.fnmatch_lines(
        [
            "E *RunEnded: the run ended at 0 s while the test waited for 'Hello world!' on the"
            " console: the link failed: the runner speaks link protocol version 2, the harness 1;"
            " the runner exited with code 3; its last output:",
            "E *esp32tl-runner: the run ended: protocol_error",
            "*stats: the run ended at 0.000000000 s (protocol_error, version 1 from the peer*",
        ]
    )


def test_a_link_the_harness_finds_wrong_fails_the_test_after_its_expects(
    link_runner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_LINK", "late")
    result = link_runner.runpytest_subprocess("-p", "no:cacheprovider")
    # The harness stopped the run at its next turn; the expect saw the line printed before the
    # end, the runner exited 0, and the test fails anyway.
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(
        ["*esp32sim: the link failed: the runner sent reset with seq 5 after 0; its output is in *"]
    )


def test_a_link_error_ends_the_run_at_once_for_a_waiting_test(
    link_runner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_LINK", "late")
    link_runner.makepyfile(
        test_fake="""
        def test_hello(board, console):
            board.power_on()
            console.expect("Hello world!")
            console.expect("Goodbye")
        """
    )
    result = link_runner.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            # Where the stats say: at the sync that followed the bad event, which the harness
            # answered with stop.
            "E *RunEnded: the run ended at 0.1 s while the test waited for 'Goodbye' on the"
            " console: the link failed: the runner sent reset with seq 5 after 0; the runner"
            " exited with code 0; its last output:",
        ]
    )


def test_a_runner_that_ignores_stop_and_sigterm_is_killed_and_fails_the_test(
    link_runner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_LINK", "deaf")
    link_runner.makeconftest(
        """
        from esp32tl.backends import esp32sim

        esp32sim.EXIT_SECONDS = 0.5
        """
    )
    result = link_runner.runpytest_subprocess("-p", "no:cacheprovider", "-rA")
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(
        [
            "*esp32sim: the runner did not exit within 0.5 s of SIGTERM, and was killed;*",
            "*no stats: the link ended without them (the runner was killed, or died)*",
        ]
    )


def test_a_hung_run_is_ended_by_the_test_s_timeout(
    link_runner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_LINK", "stall")
    link_runner.makepyfile(
        test_fake="""
        import pytest

        from esp32tl.units import hours, ms


        @pytest.fixture
        def running(board):
            board.power_on()
            # Returns at the first turn, once the runner has connected; no time passes.
            board.wait(ms(0))


        def test_hung(board, running):
            board.wait(hours(1))
        """
    )
    started = time.monotonic()
    # The timeout counts the hang alone: the runner starting is in the fixture, which on a slow
    # machine took longer than the 2 s before the fake runner had connected.
    result = link_runner.runpytest_subprocess(
        "-p", "no:cacheprovider", "-rA", "--timeout", "2", "-o", "timeout_func_only=true"
    )
    assert time.monotonic() - started < 15
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "*Failed: Timeout (>2.0s) from pytest-timeout.",
            # The teardown ended the runner, with SIGTERM: it was in no turn the test held.
            "stats: the run ended at 0.000001000 s (signal); *",
        ]
    )


def test_a_link_left_open_after_the_runner_exits_is_shut_down(
    link_runner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_LINK", "linger")
    link_runner.makeconftest(
        """
        from esp32tl.backends import esp32sim

        esp32sim.EXIT_SECONDS = 0.5
        """
    )
    child = link_runner.path / "linger.pid"
    started = time.monotonic()
    try:
        result = link_runner.runpytest_subprocess("-p", "no:cacheprovider", "-rA")
        elapsed = time.monotonic() - started
        # A child of the runner holds the link for 30 s; the teardown does not wait for it. The
        # whole run, pytest's start-up included, takes a few seconds: 15 leaves room for a loaded
        # machine, and is still half the hold.
        assert elapsed < 15, f"the run took {elapsed:.1f} s: the teardown waited for the child"
        # Nothing ended the child early: it held the link through the teardown.
        try:
            os.kill(int(child.read_text()), 0)
        except ProcessLookupError:
            pytest.fail("the child holding the link was gone before its 30 s were up")
    finally:
        # Nothing else ends it before its 30 s are up; past them, its pid may be another's.
        if time.monotonic() - started < 30:
            with contextlib.suppress(FileNotFoundError, ProcessLookupError):
                os.kill(int(child.read_text()), signal.SIGKILL)
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["stats: the run ended at 0.100000000 s (stop); *"])


def test_a_test_that_ends_while_the_runner_loads_waits_for_its_hello(
    link_runner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_LINK", "slow")
    link_runner.makepyfile(test_fake="def test_nothing(board):\n    board.power_on()\n")
    result = link_runner.runpytest_subprocess("-p", "no:cacheprovider", "-rA")
    # SIGTERM before the hello would have killed it, with -15.
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["stats: the run ended at 0.000000000 s (stop); *"])


def test_a_held_run_whose_runner_is_gone_releases_the_link(
    link_runner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_LINK", "gone")
    link_runner.makepyfile(
        test_fake="""
        def test_held(board):
            board.power_on()
            assert board._exited.wait(10)
            # The waiter let the held turn go: the link thread read to end of file.
            board._linker.join(5)
            assert not board._linker.is_alive()
        """
    )
    started = time.monotonic()
    result = link_runner.runpytest_subprocess("-p", "no:cacheprovider", "-rA")
    assert time.monotonic() - started < 15
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*esp32sim: the runner exited with code 4; its output is in *"])


def test_reference_recording_is_not_read_on_esp32sim(link_runner: pytest.Pytester) -> None:
    link_runner.makepyfile(
        test_fake="""
        import pytest
        @pytest.mark.app_recording("does-not-exist.jsonl")
        def test_reference(board, console):
            board.power_on()
            console.expect("Hello world!")
    """
    )
    link_runner.runpytest_subprocess("-p", "no:cacheprovider").assert_outcomes(passed=1)


def test_nvs_without_power_on_fails_teardown(fake_runner: pytest.Pytester) -> None:
    import struct

    table = fake_runner.path / "build/partition_table/partition-table.bin"
    table.write_bytes(struct.pack("<HBBII16sI", 0x50AA, 1, 2, 0x9000, 0x6000, b"nvs", 0))
    fake_runner.makepyfile(
        test_fake='def test_config(board): board.nvs({"config": {"host": "test"}})'
    )
    result = fake_runner.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(
        [
            "*the test set up the NVS partition 'nvs' and never called"
            " board.power_on(): it ran no firmware*"
        ]
    )
