"""The esp32tl plugin's parametrisation and the `esp32sim` backend's command line (plan §3, M0).

The tests that run pytest inside pytest do it in a subprocess: pytest-embedded starts a
multiprocessing manager for every session.
"""

import gc
import hashlib
import json
import re
import shutil
import sys
import time
import warnings
from pathlib import Path
from typing import cast
from xml.etree import ElementTree

import pytest
from esptool.bin_image import LoadFirmwareImage
from pytest_embedded.log import MessageQueue, PexpectProcess
from pytest_embedded.utils import Meta

from esp32tl.backends import esp32sim
from esp32tl.backends.esp32sim import BackendError, RunnerExited

ROOT = Path(__file__).resolve().parent.parent
HELLO_WORLD_BUILD = ROOT / "firmware" / "hello_world" / "build"

# A `board` that shows what the plugin parametrised it with, in place of the real one.
SHOW_PARAMS = """
import pytest

@pytest.fixture
def board(request, embedded_services, app_path):
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


# SHOW_PARAMS, with a second backend that can give less than esp32sim: `serial`, which the
# capability declaration has and esp32tl.backends.SERVICES does not yet.
WITH_SERIAL = (
    SHOW_PARAMS
    + """
from esp32tl.backends import SERVICES

SERVICES["serial"] = "esp,idf"
"""
)


def test_a_backend_that_cannot_give_a_source_or_tap_skips_with_the_reason(
    pytester: pytest.Pytester,
) -> None:
    pytester.makeconftest(WITH_SERIAL)
    pytester.makepyfile(
        test_sources="""
        import pytest

        @pytest.mark.source("uart.gnss", "model")
        def test_model(board):
            assert board[0] == "esp32sim"

        @pytest.mark.tap("console", "uart.gnss")
        def test_tap(board):
            assert board[0] == "esp32sim"

        @pytest.mark.source("uart.gnss", "real")
        @pytest.mark.tap("console")
        def test_real(board):
            pass
        """
    )
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-ra")
    result.assert_outcomes(passed=4, skipped=2)
    result.stdout.fnmatch_lines_random(
        [
            "SKIPPED [[]1[]] test_sources.py:3: serial: uart.gnss takes no model source on this"
            " backend, only real",
            "SKIPPED [[]1[]] test_sources.py:7: serial: uart.gnss cannot be tapped on this backend",
        ]
    )
    # Skipped before setup: pytest-embedded's fixtures are set up for test_real[serial] only, not
    # for test_model[serial].
    result = pytester.runpytest_subprocess(
        "-p", "no:cacheprovider", "--setup-plan", "-k", "model or real"
    )
    assert result.stdout.str().count("SETUP    F embedded_services['esp,idf']") == 1


@pytest.mark.parametrize(
    ("marks", "message"),
    [
        ("@pytest.mark.board_profile", "the board_profile marker takes one name, as in *"),
        (
            '@pytest.mark.board_profile("esp32-wrover-kit")',
            "no board profile named 'esp32-wrover-kit';*",
        ),
        (
            '@pytest.mark.source("uart.gnss")',
            "the source marker takes an interface and a source, as in *",
        ),
        (
            '@pytest.mark.source("uart.gnss", "model")\n@pytest.mark.source("uart.gnss", "replay")',
            "two source markers on test_one name uart.gnss",
        ),
        ("@pytest.mark.tap", "the tap marker takes one or more interfaces, as in *"),
        (
            '@pytest.mark.source("uart.modem", "model")',
            "the board profile esp32-devkitc has no uart.modem;*",
        ),
        (
            '@pytest.mark.source("uart.gnss", "recorded")',
            "no backend takes a 'recorded' source on uart.gnss;*",
        ),
        (
            '@pytest.mark.board_profile("lilygo-t-a7670g-r2")\n@pytest.mark.tap("lines.modem")',
            "no backend can tap lines.modem",
        ),
    ],
)
def test_a_marker_no_backend_can_satisfy_is_a_collection_error(
    pytester: pytest.Pytester, marks: str, message: str
) -> None:
    pytester.makeconftest(WITH_SERIAL)
    pytester.makepyfile(test_marks=f"import pytest\n\n{marks}\ndef test_one(board):\n    pass\n")
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    assert result.ret == pytest.ExitCode.INTERRUPTED
    result.stdout.fnmatch_lines([f"*test_marks.py::test_one: {message}"])
    result.stdout.no_fnmatch_line("*During handling of the above exception*")


def test_markers_on_a_class_or_a_module_count(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(WITH_SERIAL)
    pytester.makepyfile(
        test_levels="""
        import pytest

        pytestmark = pytest.mark.tap("uart.modem")

        @pytest.mark.board_profile("lilygo-t-a7670g-r2")
        class TestModem:
            @pytest.mark.source("uart.modem", "real")
            def test_real(self, board):
                pass
        """
    )
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-ra")
    result.assert_outcomes(passed=1, skipped=1)
    result.stdout.fnmatch_lines(["SKIPPED *: serial: uart.modem cannot be tapped on this backend"])


def test_the_closest_source_marker_wins(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(WITH_SERIAL)
    pytester.makepyfile(
        test_override="""
        import pytest

        pytestmark = pytest.mark.source("uart.gnss", "model")

        def test_module(board):
            pass

        @pytest.mark.source("uart.gnss", "real")
        def test_own(board):
            pass
        """
    )
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-ra")
    # The module's model skips test_module on serial; test_own's real runs on both.
    result.assert_outcomes(passed=3, skipped=1)
    result.stdout.fnmatch_lines(["SKIPPED *test_override.py: serial: uart.gnss takes no model*"])


@pytest.mark.parametrize(
    "marker", ['board_profile("lilygo-t-a7670g-r2")', 'source("uart.gnss", "model")']
)
def test_a_board_marker_in_a_pytest_param_is_refused(
    pytester: pytest.Pytester, marker: str
) -> None:
    pytester.makeconftest(WITH_SERIAL)
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
    ("items", "node", "source", "late"),
    [
        ("items[:1]", "item", "model", "the source marker"),
        # An empty source is a source too, which no backend gives.
        ("items[:1]", "item", "", "the source marker"),
        # The class's node, whose markers are not in the class's own pytestmark.
        ("items[:1]", "item.parent", "model", "the source marker"),
        # The module's node, once per item: two markers on one level.
        ("items", "item.parent.parent", "model", "a source or tap marker"),
    ],
)
def test_a_board_marker_a_hook_adds_is_refused(
    pytester: pytest.Pytester, items: str, node: str, source: str, late: str
) -> None:
    pytester.makeconftest(
        WITH_SERIAL
        + f"""
def pytest_collection_modifyitems(items):
    for item in {items}:
        {node}.add_marker(pytest.mark.source("uart.gnss", "{source}"))
"""
    )
    pytester.makepyfile(test_hook="class TestA:\n    def test_one(self, board):\n        pass\n")
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(
        [f"ERROR: test_hook.py::TestA::test_one[[]esp32sim[]]: {late} reached the test after*"]
    )


def test_a_subclass_s_source_overrides_its_base_s(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(WITH_SERIAL)
    pytester.makepyfile(
        test_classes="""
        import pytest

        @pytest.mark.source("uart.gnss", "model")
        class TestBase:
            def test_one(self, board):
                pass

        @pytest.mark.source("uart.gnss", "real")
        class TestSub(TestBase):
            pass
        """
    )
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-rs", "-v")
    # TestBase skips on serial; TestSub asks for real, which serial gives.
    result.assert_outcomes(passed=3, skipped=1)
    result.stdout.fnmatch_lines(["*TestBase::test_one[[]serial[]] SKIPPED*"])


def fake_build(app: Path, target: str = "esp32", encrypted: bool = False) -> Path:
    """An ESP-IDF build directory with what `IdfApp` reads, its images empty."""
    build = app / "build"
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


# $FAKE_EXIT KILL: killed by SIGKILL, as by the OOM killer.
FAKE_RUNNER = """#!/bin/sh
printf '%s' "$FAKE_OUTPUT"
[ "$FAKE_EXIT" != KILL ] || kill -KILL $$
exit "$FAKE_EXIT"
"""


@pytest.fixture
def fake_runner(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> pytest.Pytester:
    """A suite of one test, `console.expect("Hello world!")`, on an app with a fake build, run
    by a fake runner that prints $FAKE_OUTPUT and exits with $FAKE_EXIT."""
    runner = pytester.path / "runner"
    runner.write_text(FAKE_RUNNER)
    runner.chmod(0o755)
    (pytester.path / "rom.elf").touch()
    monkeypatch.setenv("ESP32TL_RUNNER", str(runner))
    monkeypatch.setenv("ESP32TL_ROM", str(pytester.path / "rom.elf"))
    fake_build(pytester.path)
    pytester.makepyfile(
        test_fake="""
        def test_hello(console):
            console.expect("Hello world!")
        """
    )
    return pytester


def test_expect_fails_at_once_when_the_runner_has_exited(
    fake_runner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_OUTPUT", "Goodbye\n")
    monkeypatch.setenv("FAKE_EXIT", "0")
    started = time.monotonic()
    result = fake_runner.runpytest_subprocess("-p", "no:cacheprovider")
    # pytest-embedded's expect waits 30 s by default.
    assert time.monotonic() - started < 15
    result.assert_outcomes(failed=1)
    # pexpect empties its buffer at EOF, so the runner's output comes with the EOF it caused.
    result.stdout.fnmatch_lines(
        [
            "E *EOF: the runner exited with code 0; its last output:",
            "E *Goodbye",
            'E *EOF: Not found "Hello world!"',
        ]
    )


def test_a_runner_that_fails_to_start_is_reported_once(
    fake_runner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_OUTPUT", "esp32tl-runner: rom.elf: not an ELF file\n")
    monkeypatch.setenv("FAKE_EXIT", "2")
    result = fake_runner.runpytest_subprocess("-p", "no:cacheprovider")
    # The expect's EOF names the exit code, so the teardown does not report it again.
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "E *EOF: the runner exited with code 2; its last output:",
            "E *esp32tl-runner: rom.elf: not an ELF file",
        ]
    )


def test_the_board_runs_the_profile_the_test_chose(
    fake_runner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_OUTPUT", "Hello world!\n")
    monkeypatch.setenv("FAKE_EXIT", "0")
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
    result.assert_outcomes(passed=2)


@pytest.mark.parametrize(("exit", "code"), [("1", "1"), ("KILL", "-9")])
def test_a_runner_that_exits_with_an_error_fails_the_test(
    fake_runner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, exit: str, code: str
) -> None:
    monkeypatch.setenv("FAKE_OUTPUT", "Hello world!\n")
    monkeypatch.setenv("FAKE_EXIT", exit)
    # Waits for the runner's end, so that it has exited before the teardown.
    fake_runner.makepyfile(
        test_fake="""
        import pexpect

        def test_hello(console):
            console.expect("Hello world!")
            console.expect(pexpect.EOF)
        """
    )
    result = fake_runner.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(
        [f"*esp32sim: the runner exited with code {code}; its output is in *"]
    )


def test_an_eof_from_elsewhere_does_not_hide_the_runner_s_exit(
    fake_runner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_OUTPUT", "Hello world!\n")
    monkeypatch.setenv("FAKE_EXIT", "1")
    fake_runner.makepyfile(
        test_fake="""
        import pexpect

        def test_hello(console):
            console.expect("Hello world!")
            console.expect(pexpect.EOF)
            raise pexpect.EOF("a host tool's end")
        """
    )
    result = fake_runner.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(failed=1, errors=1)
    result.stdout.fnmatch_lines(["*esp32sim: the runner exited with code 1; its output is in *"])


def test_the_eof_carries_the_runner_s_last_whole_lines_without_colour() -> None:
    output = b"".join(b"\x1b[0;32mline %d\x1b[0m\n" % i for i in range(200))
    head, tail = eof_of_stream(output + b"\nesp32tl: the runner exited with code 0\n").split(
        "\n", 1
    )
    assert head == "the runner exited with code 0; its last output:"
    lines = tail.splitlines()
    # 2 KiB of output, from the first line that begins in it
    assert lines[-1] == "line 199"
    assert all(re.fullmatch(r"line \d+", line) for line in lines)
    assert len(tail) < esp32sim.EOF_TAIL_BYTES


# The end-of-run line the backend appends, with code 0.
END = b"\nesp32tl: the runner exited with code 0\n"


def eof_of_stream(output: bytes, link_error: str | None = None) -> str:
    """The message of the EOF the stream raises after `output`, read in 100-byte chunks, with
    `link_error` what the harness found wrong."""
    chunks = [output[at : at + 100] for at in range(0, len(output), 100)]

    class Stream:
        def read_nonblocking(self, size: int = 1, timeout: int = -1) -> bytes:
            return chunks.pop(0)

    stream = Stream()
    esp32sim.end_stream_at_end_of_run(cast(PexpectProcess, stream), lambda: link_error)
    for _ in range(len(chunks)):
        stream.read_nonblocking()
    with pytest.raises(RunnerExited) as raised:
        stream.read_nonblocking()
    return str(raised.value)


# ESP-IDF ends its lines with \r\n by default.
@pytest.mark.parametrize("ending", [b"", b"\n", b"\r\n"])
def test_the_eof_keeps_a_long_last_line(ending: bytes) -> None:
    message = eof_of_stream(b"y" * 3000 + ending + b"\nesp32tl: the runner exited with code 1\n")
    head, tail = message.split("\n", 1)
    assert head == "the runner exited with code 1; its last output:"
    assert tail.strip().strip("y") == "" and len(tail) > 1000


def test_the_eof_names_the_link_s_error_from_the_link_not_from_the_log() -> None:
    # A guest line that imitates the backend's, and the backend's own with its leading newline cut
    # off by the 2 KiB tail.
    output = b"esp32tl: the link failed: imitated\n" + b"y" * 3000
    output += b"\nesp32tl: the link failed: real\n\nesp32tl: the runner exited with code 0\n"
    head = eof_of_stream(output, "real").split("\n", 1)[0]
    assert head == "the link failed: real; the runner exited with code 0; its last output:"
    head = eof_of_stream(b"esp32tl: the link failed: imitated\n" + END).split("\n", 1)[0]
    assert head == "the runner exited with code 0; its last output:"


def test_ended_on_runner_exit_stops_at_a_cycle() -> None:
    first, second = ValueError("first"), ValueError("second")
    first.__cause__, second.__cause__ = second, first
    assert not esp32sim.ended_on_runner_exit(first)
    second.__cause__ = RunnerExited("end")
    assert esp32sim.ended_on_runner_exit(first)


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


# A runner that speaks the link protocol, as the backend sees one, without an emulator: it
# connects once, shakes hands, takes turn 0, prints the console line and sends a warning. Then it
# waits for SIGTERM and ends the run with its stats, exiting 0, as protocol §9 has it. $FAKE_LINK
# makes it misbehave: `version` sends a hello of version 2, and exits 3 once it has the harness's
# answer; `seq` sends an event out of sequence and ends on the harness's stop; `late` sends one
# after turn 0, when no turn is due before --max-seconds; `deaf` ignores SIGTERM; `linger` leaves
# a child holding the link open after it exits; `slow` loads for half a second before it connects,
# and dies of SIGTERM until then, as the real runner does.
LINK_RUNNER = """#!{python}
import os
import signal
import socket
import subprocess
import sys
import time

from esp32tl.link import protocol as p

if os.environ.get("FAKE_LINK") == "slow":
    time.sleep(0.5)
signal.pthread_sigmask(signal.SIG_BLOCK, {{signal.SIGTERM}})
args = sys.argv[1:]
board = args[args.index("--board") + 1]
conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
conn.connect(board.removeprefix("socket:"))
lines = conn.makefile("rb")
mode = os.environ.get("FAKE_LINK", "")


def send(*messages):
    conn.sendall(b"".join(p.encode(message) for message in messages))


def end(how, code, detail=None):
    send(p.Stats(1000, how, 0, 1, (), 72, 0, 0, 0, detail=detail))
    if mode == "linger":
        os.set_inheritable(conn.fileno(), True)
        subprocess.Popen(["sleep", "3"], close_fds=False)
    conn.close()
    print(f"esp32tl-runner: the run ended: {{how}}", file=sys.stderr)
    sys.exit(code)


send(p.RunnerHello(2 if mode == "version" else 1, "esp32", "emulated-fifo-write", "0" * 40))
hello = p.decode_harness(lines.readline())
board_profile = os.environ.get("FAKE_BOARD", "esp32-devkitc")
assert (hello.board, hello.chip, hello.watch) == (board_profile, "esp32", ()), hello
if mode == "version":
    end("protocol_error", 3, detail="version 1 from the peer, the runner speaks 2")
if mode == "seq":
    send(p.UartTx(2, 0, 1, (4,), 9600, b"x"))
send(p.Sync(0))
turn = p.decode_harness(lines.readline())
print("Hello world!", flush=True)
if turn == p.Stop():
    end("stop", 0)
assert turn == p.Wait(1 if mode == "seq" else 0, None, None), turn
send(p.Warn(1000, "unrouted_rx", "uart_rx on GPIO 5: 72 bytes dropped", pin=5, nbytes=72))
if mode == "late":
    send(p.Reset(5, 2000, 12))
if mode == "deaf":
    time.sleep(60)
signal.sigwait({{signal.SIGTERM}})
end("signal", 0)
"""


@pytest.fixture
def link_runner(fake_runner: pytest.Pytester) -> pytest.Pytester:
    """`fake_runner`'s suite, run by `LINK_RUNNER`."""
    runner = fake_runner.path / "runner"
    runner.write_text(LINK_RUNNER.format(python=sys.executable))
    return fake_runner


def test_the_backend_serves_the_link_and_ends_the_run_with_sigterm(
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
    assert data["stats"]["end"] == "signal"
    assert data["warnings"][0]["bytes"] == 72
    # The warning and the stats are in the report.
    result.stdout.fnmatch_lines(
        [
            "*Captured esp32sim link teardown*",
            "board esp32-devkitc, watching no pins",
            "runner: protocol version 1, esp32sim 0000*, clock emulated-fifo-write; 1 turn taken*",
            "warnings (1):",
            "  0.000001000 s  unrouted_rx: uart_rx on GPIO 5: 72 bytes dropped",
            "stats: the run ended at 0.000001000 s (signal); 1 turn, 0 resets",
            "  unrouted_rx_bytes 72, *",
        ]
    )


def test_the_harness_hello_names_the_profile_the_test_chose(
    link_runner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_BOARD", "lilygo-t-a7670g-r2")
    link_runner.makepyfile(
        test_lilygo="""
        import pytest

        @pytest.mark.board_profile("lilygo-t-a7670g-r2")
        def test_hello(console):
            console.expect("Hello world!")
        """
    )
    # The fake runner asserts the hello's board; a failed assertion ends it with an error.
    result = link_runner.runpytest_subprocess("-p", "no:cacheprovider", "-rA", "test_lilygo.py")
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["board lilygo-t-a7670g-r2, watching no pins"])


def test_a_mismatched_handshake_fails_the_test_with_both_sides_reasons(
    link_runner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_LINK", "version")
    result = link_runner.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(failed=1)
    # The runner had the harness's hello: it ended the run with its stats, which the harness read.
    result.stdout.fnmatch_lines(
        [
            "E *EOF: the link failed: the runner speaks link protocol version 2, the harness 1;"
            " the runner exited with code 3; its last output:",
            "E *esp32tl-runner: the run ended: protocol_error",
            "E *esp32tl: the link failed: the runner speaks link protocol version 2, the harness 1",
            "*stats: the run ended at 0.000001000 s (protocol_error, version 1 from the peer*",
        ]
    )


def test_a_link_the_harness_finds_wrong_fails_the_test_after_its_expects(
    link_runner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_LINK", "seq")
    result = link_runner.runpytest_subprocess("-p", "no:cacheprovider")
    # The harness stopped the run at its turn; the runner exited 0, and the test fails anyway.
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(
        [
            "*esp32sim: the link failed: the runner sent uart_tx with seq 2 after 0;"
            " its output is in *",
        ]
    )


def test_a_runner_that_ignores_sigterm_is_killed_and_fails_the_test(
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


def test_a_link_error_ends_the_run_at_once_for_a_waiting_expect(
    link_runner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_LINK", "late")
    link_runner.makepyfile(
        test_fake="""
        def test_hello(console):
            console.expect("Hello world!")
            console.expect("Goodbye")
        """
    )
    started = time.monotonic()
    result = link_runner.runpytest_subprocess("-p", "no:cacheprovider")
    # The runner takes no turn after the bad event: the harness ends the run with SIGTERM rather
    # than leave the expect to its 30 s timeout.
    assert time.monotonic() - started < 15
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "E *EOF: the link failed: the runner sent reset with seq 5 after 0;"
            " the runner exited with code 0; its last output:",
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
    started = time.monotonic()
    result = link_runner.runpytest_subprocess("-p", "no:cacheprovider", "-rA")
    # A child of the runner holds the link for 3 s; the teardown does not wait for it.
    assert time.monotonic() - started < 3
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["stats: the run ended at 0.000001000 s (signal); *"])


def test_a_test_that_ends_while_the_runner_loads_waits_for_its_hello(
    link_runner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_LINK", "slow")
    link_runner.makepyfile(test_fake="def test_nothing(board): pass")
    result = link_runner.runpytest_subprocess("-p", "no:cacheprovider", "-rA")
    # SIGTERM before the hello would have killed it, with -15.
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["stats: the run ended at 0.000001000 s (signal); *"])
