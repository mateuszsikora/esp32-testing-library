"""The esp32tl plugin's parametrisation and the `esp32sim` backend's command line (plan §3, M0).

The tests that run pytest inside pytest do it in a subprocess: pytest-embedded starts a
multiprocessing manager for every session.
"""

import hashlib
import json
import re
import shutil
import time
from pathlib import Path
from typing import cast

import pytest
from esptool.bin_image import LoadFirmwareImage
from pytest_embedded.log import PexpectProcess

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


def test_backend_option_limits_the_parametrisation(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(SHOW_PARAMS)
    pytester.makepyfile("def test_one(board): pass")
    result = pytester.runpytest_subprocess(
        "-p", "no:cacheprovider", "--collect-only", "-q", "--esp32tl-backend", "esp32sim"
    )
    result.stdout.fnmatch_lines(
        ["test_backend_option_limits_the_parametrisation.py::test_one[esp32sim]"]
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
    command = esp32sim.runner_command(Path("/r/runner"), Path("/r/rom.elf"), app, 300)
    assert command == [
        "/r/runner",
        "--chip",
        "esp32",
        "--rom",
        "/r/rom.elf",
        "--max-seconds",
        "300",
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


def eof_of_stream(output: bytes) -> str:
    """The message of the EOF the stream raises after `output`, read in 100-byte chunks."""
    chunks = [output[at : at + 100] for at in range(0, len(output), 100)]

    class Stream:
        def read_nonblocking(self, size: int = 1, timeout: int = -1) -> bytes:
            return chunks.pop(0)

    stream = Stream()
    esp32sim.end_stream_at_end_of_run(cast(PexpectProcess, stream))
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


def test_ended_on_runner_exit_stops_at_a_cycle() -> None:
    first, second = ValueError("first"), ValueError("second")
    first.__cause__, second.__cause__ = second, first
    assert not esp32sim.ended_on_runner_exit(first)
    second.__cause__ = RunnerExited("end")
    assert esp32sim.ended_on_runner_exit(first)


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
