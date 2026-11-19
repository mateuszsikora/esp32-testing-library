"""The `esp32tl` command's `run` and `replay`: the arguments they refuse, in this process, and runs
of the validation images through the real runner, in a subprocess of their own. The runs need
`make firmware` (hello_world, nmea_reader, gpio_echo). `recording retime` and `recording nmea`, in
this process: tests/test_real_source.py replays what `retime` made of a capture."""

import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_pytest_plugin import fake_build

from esp32tl import __version__, cli, recording, runtime

ROOT = Path(__file__).resolve().parent.parent
HELLO_WORLD = ROOT / "firmware" / "hello_world"
NMEA_READER = ROOT / "firmware" / "nmea_reader"
GPIO_ECHO = ROOT / "firmware" / "gpio_echo"
RECORDING = ROOT / "recordings" / "gnss-generated-1.jsonl"
REPLAY = ("replay", "--app", "firmware/nmea_reader")

# The command, as `esp32tl` runs it, with what `PATCH` holds run first.
COMMAND = """
import sys
from esp32tl import cli
{patch}
sys.exit(cli.main(sys.argv[1:]))
"""


def esp32tl(
    *args: str,
    patch: str = "",
    env: dict[str, str] | None = None,
    apps: tuple[Path, ...] = (HELLO_WORLD, NMEA_READER, GPIO_ECHO),
) -> subprocess.Popen[str]:
    """`esp32tl ARGS` in a subprocess, from the repository's root, once `apps` are built."""
    for app in apps:
        if not (app / "build" / "esp32tl-built").is_file():
            pytest.fail(f"{app}/build holds no finished build: run make firmware", pytrace=False)
    return subprocess.Popen(
        [sys.executable, "-c", COMMAND.format(patch=patch), *args],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        env={**os.environ, **(env or {})},
    )


def stderr_until(process: subprocess.Popen[str], text: str, seconds: float = 30) -> str:
    """What `process` wrote on stderr up to and including `text`. Read from the descriptor, as
    `communicate` reads the rest, so that nothing stays in a buffer between the two."""
    assert process.stderr is not None
    fd = process.stderr.fileno()
    seen = b""
    deadline = time.monotonic() + seconds
    while text.encode() not in seen:
        left = deadline - time.monotonic()
        assert left > 0, f"no {text!r} on stderr in {seconds} s: {seen!r}"
        ready, _, _ = select.select([fd], [], [], left)
        if ready:
            chunk = os.read(fd, 4096)
            assert chunk, f"stderr ended before {text!r}: {seen!r}"
            seen += chunk
    return seen.decode(errors="replace")


def finished(process: subprocess.Popen[str]) -> tuple[int, str, str]:
    out, err = process.communicate(timeout=60)
    return process.returncode, out, err


def test_the_help_names_run_replay_and_recording(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # argparse wraps the help to the terminal's width, and Python 3.11 puts the help of a command
    # as long as `recording` on the line after it: the words, not the spaces, are checked.
    monkeypatch.setenv("COLUMNS", "80")
    assert cli.main([]) == 0
    words = " ".join(capsys.readouterr().out.split())
    assert "runtime prepare the downloaded runtime" in words
    assert "run run an app on the emulator, its console on stdout" in words
    assert "replay run an app with a recording replayed into one of its board's interfaces" in words
    assert (
        "recording convert what a real device sent into a recording the emulator replays" in words
    )


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (
            ["run", "--app", "firmware/hello_world", "--board", "nope"],
            "esp32tl: no board profile named 'nope'; the profiles are esp32-c3-devkitm,"
            " esp32-c6-devkitc, esp32-devkitc, esp32-p4-function-ev-board, esp32-s3-devkitc,"
            " esp32-s31-module, gps-alarm-clock, lilygo-t-a7670g-r2, sd-data-logger\n",
        ),
        (
            ["run", "--app", "nowhere"],
            f"esp32tl: {ROOT / 'nowhere' / 'build'} holds no ESP-IDF or PlatformIO build:"
            " no flasher_args.json and no firmware.bin\n",
        ),
        (
            [*REPLAY, "--interface", "modem", "--recording", str(RECORDING)],
            "esp32tl: the board profile esp32-devkitc has no uart.modem; its interfaces are"
            " console, uart.gnss, sd_card, wifi, bluetooth\n",
        ),
        (
            [*REPLAY, "--interface", "uart.gnss", "--recording", "recordings/none.jsonl"],
            f"esp32tl: {ROOT / 'recordings' / 'none.jsonl'}: [Errno 2] No such file or"
            f" directory: '{ROOT / 'recordings' / 'none.jsonl'}'\n",
        ),
    ],
)
def test_what_keeps_a_run_from_starting_exits_2(
    args: list[str],
    message: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(ROOT)
    assert cli.main(args) == 2
    assert capsys.readouterr().err == message


@pytest.mark.parametrize("seconds", ["0", "-1", "inf", "nan", "soon"])
def test_max_seconds_is_a_number_above_0(seconds: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["run", "--app", "firmware/hello_world", "--max-seconds", seconds])
    assert exit_info.value.code == 2
    assert f"argument --max-seconds: {seconds!r} is not a number of seconds above 0" in (
        capsys.readouterr().err
    )


def test_build_dir_is_not_empty(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["run", "--app", "firmware/hello_world", "--build-dir", ""])
    assert exit_info.value.code == 2
    assert "argument --build-dir: the build directory is an empty path" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["run", "replay"])
@pytest.mark.parametrize("absolute", [False, True])
def test_cli_passes_only_the_selected_build_to_the_runner(
    tmp_path: Path, command: str, absolute: bool
) -> None:
    app = tmp_path / "app source"
    default = fake_build(app)
    selected = fake_build(app, build_dir="../CI builds/debug").resolve()
    (default / "app.bin").write_bytes(b"default")
    (selected / "app.bin").write_bytes(b"selected")
    cwd = tmp_path / "other directory"
    fake_build(cwd)
    runner = tmp_path / "fake runner"
    runner.write_text(f"""#!{sys.executable}
import json
import sys
from pathlib import Path
from esp32tl import __version__, runtime
if sys.argv[1:] == ["--runtime-version"]:
    print(__version__, runtime.SIM_REV)
    sys.exit(0)
args = sys.argv[1:]
files = [args[i + 1] for i, arg in enumerate(args) if arg == "--flash-at"]
Path({str(tmp_path / "loaded.json")!r}).write_text(json.dumps([
    [file, Path(file.split("=", 1)[1]).read_text()] for file in files
]))
""")
    runner.chmod(0o755)
    rom = tmp_path / "rom.elf"
    rom.write_bytes((ROOT / ".cache/esp-rom-elfs/20260528/esp32_rev300_rom.elf").read_bytes())
    args = [
        command,
        "--app",
        "../app source",
        "--build-dir",
        str(selected) if absolute else "../CI builds/debug",
    ]
    if command == "replay":
        args += ["--interface", "gnss", "--recording", str(RECORDING)]
    env = {**os.environ, "ESP32TL_RUNNER": str(runner), "ESP32TL_ROM": str(rom)}
    result = subprocess.run(
        [sys.executable, "-c", COMMAND.format(patch=""), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    # This fake runner loads the files and exits without connecting to the link.
    assert result.returncode == 0, result.stderr
    assert json.loads((tmp_path / "loaded.json").read_text()) == [
        [f"0x1000={selected}/bootloader/bootloader.bin", ""],
        [f"0x8000={selected}/partition_table/partition-table.bin", ""],
        [f"0x10000={selected}/app.bin", "selected"],
    ]
    (selected / "flasher_args.json").unlink()
    (tmp_path / "loaded.json").unlink()
    result = subprocess.run(
        [sys.executable, "-c", COMMAND.format(patch=""), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert (
        f"{selected} holds no ESP-IDF or PlatformIO build: no flasher_args.json and no firmware.bin"
        in result.stderr
    )
    assert not (tmp_path / "loaded.json").exists()
    assert (default / "app.bin").read_bytes() == b"default"


def test_run_prints_the_console_and_the_link_s_report() -> None:
    code, out, err = finished(esp32tl("run", "--app", "firmware/hello_world", "--max-seconds", "2"))
    assert code == 0, err
    assert "Hello world!" in out
    assert "esp32tl: board esp32-devkitc, watching no pins\n" in err
    assert "esp32tl: stats: the run ended at 2.000000000 s (max_seconds); 1 turn, 0 resets" in err


def test_run_holds_the_board_s_pulls_on_its_lines() -> None:
    code, out, err = finished(
        esp32tl(
            "run",
            "--app",
            "firmware/gpio_echo",
            "--board",
            "lilygo-t-a7670g-r2",
            "--max-seconds",
            "1",
        )
    )
    assert code == 0, err
    # RI, pulled down on the board, reads low: undriven, the emulator's pad would read 1.
    assert [line.rstrip("\r") for line in out.splitlines() if line.startswith("in=")] == ["in=0"]
    assert (
        "esp32tl: board lilygo-t-a7670g-r2, watching GPIO 4, GPIO 5, GPIO 12, "
        "GPIO 19, GPIO 23, GPIO 25, GPIO 33\n" in err
    )


def test_replay_reaches_the_app_at_the_recorded_times() -> None:
    code, out, err = finished(
        esp32tl(
            "replay",
            "--app",
            "firmware/nmea_reader",
            "--interface",
            "gnss",
            "--recording",
            "recordings/gnss-generated-1.jsonl",
            "--max-seconds",
            "3.5",
        )
    )
    assert code == 0, err
    # One sentence per emulated second from 1 s: three by 3.5 s.
    assert [line.rstrip("\r") for line in out.splitlines() if line.startswith("fix")] == [
        "fix acquired lat=52.229677 lon=21.012233",
        "fix lat=52.229657 lon=21.012260",
        "fix lat=52.229630 lon=21.012253",
    ]
    assert "esp32tl: board esp32-devkitc, watching GPIO 4, GPIO 5\n" in err
    assert "esp32tl: stats: the run ended at 3.500000000 s (max_seconds); 4 turns, 0 resets" in err


def test_a_baud_mismatch_exits_1_with_the_link_s_error() -> None:
    # The profile's GNSS route at 115200 baud, and nmea_reader at 9600.
    patch = """
import dataclasses
from esp32tl import boards
profile = boards.profile("esp32-devkitc")
route = dataclasses.replace(profile.uart["gnss"], baud=115200)
mismatched = dataclasses.replace(profile, uart={**profile.uart, "gnss": route})
boards.profile = lambda name: mismatched
"""
    code, out, err = finished(
        esp32tl(
            "replay",
            "--app",
            "firmware/nmea_reader",
            "--interface",
            "gnss",
            "--recording",
            "recordings/gnss-generated-1.jsonl",
            patch=patch,
        )
    )
    assert code == 1, err
    assert "fix" not in out
    # The run ended on the first frame errors, at the turn after the first sentence.
    assert "esp32tl: stats: the run ended at 2.000000000 s (" in err
    assert err.endswith(" bytes dropped as frame errors (frm_err)\n"), err
    assert (
        "esp32tl: error: uart.gnss sends at 115200 baud on GPIO 5, and the firmware's UART1"
        " receives there at 9600 baud: " in err
    )


def test_ctrl_c_ends_the_run_and_exits_130() -> None:
    process = esp32tl("run", "--app", "firmware/hello_world", "--max-seconds", "100000")
    # Once the runner has connected, it ends the run on SIGTERM rather than dying of it: it
    # connects after it has loaded the images.
    before = stderr_until(process, "flash 0x10000")
    time.sleep(0.5)
    process.send_signal(signal.SIGINT)
    code, _, err = finished(process)
    err = before + err
    assert code == 130, err
    assert "esp32tl: stats: the run ended at " in err
    assert " s (signal); 1 turn, " in err
    assert "Traceback" not in err


# A runner that never connects: it writes its pid beside itself, says it is loading, and sleeps
# for longer than `finished` waits for the command.
LOADING_RUNNER = f"""#!/bin/sh
if [ "$1" = "--runtime-version" ]; then
    echo "{__version__} {runtime.SIM_REV}"
    exit 0
fi
echo $$ > "$0.pid"
echo "fake runner: loading" >&2
exec sleep 300
"""


def test_ctrl_c_before_the_runner_connects_exits_130(tmp_path: Path) -> None:
    runner = tmp_path / "runner"
    runner.write_text(LOADING_RUNNER)
    runner.chmod(0o755)
    process = esp32tl("run", "--app", "firmware/hello_world", env={"ESP32TL_RUNNER": str(runner)})
    before = stderr_until(process, "fake runner: loading")
    process.send_signal(signal.SIGINT)
    # The command did not wait for the runner's sleep, which outlasts `finished`'s 60 s, and the
    # SIGTERM killed the runner, without stats.
    code, _, err = finished(process)
    err = before + err
    with pytest.raises(ProcessLookupError):
        os.kill(int(Path(f"{runner}.pid").read_text()), 0)
    assert code == 130, err
    assert "esp32tl: the runner never connected" in err
    assert "Traceback" not in err


# A runner that says what it was given and exits without connecting.
ARGS_RUNNER = f"""#!/bin/sh
if [ "$1" = "--runtime-version" ]; then
    echo "{__version__} {runtime.SIM_REV}"
    exit 0
fi
echo "fake runner: $*" >&2
"""


# Not `board`: esp32tl's plugin parametrizes that name.
@pytest.mark.parametrize(
    ("profile", "psram"), [("esp32-devkitc", "0"), ("lilygo-t-a7670g-r2", "4")]
)
def test_run_gives_the_chip_the_profile_s_psram(tmp_path: Path, profile: str, psram: str) -> None:
    runner = tmp_path / "runner"
    runner.write_text(ARGS_RUNNER)
    runner.chmod(0o755)
    _, _, err = finished(
        esp32tl(
            "run",
            "--app",
            "firmware/hello_world",
            "--board",
            profile,
            env={"ESP32TL_RUNNER": str(runner)},
        )
    )
    (args,) = [line for line in err.splitlines() if line.startswith("fake runner: ")]
    assert args.endswith(f" --psram-mb {psram}"), err


NEO_M8N = ROOT / "recordings" / "gnss-neo-m8n"


def log_line(start: str) -> int:
    """The number of the committed log's first line that starts with `start`."""
    lines = NEO_M8N.with_suffix(".nmea").read_text().splitlines()
    return next(n for n, line in enumerate(lines, start=1) if line.startswith(start))


def test_recording_nmea_writes_the_committed_recording_of_the_committed_log(
    tmp_path: Path,
) -> None:
    out = tmp_path / "out.jsonl"
    assert cli.main(["recording", "nmea", str(NEO_M8N.with_suffix(".nmea")), str(out)]) == 0
    assert out.read_text() == NEO_M8N.with_suffix(".jsonl").read_text()


def test_recording_nmea_takes_the_route_s_baud_the_period_the_start_and_the_end(
    tmp_path: Path,
) -> None:
    log = tmp_path / "log.nmea"
    log.write_text("$GNRMC,120000.00,V,,,,,,,,,,N*60\r\n$GNRMC,120000.20,V,,,,,,,,,,N*62\r\n")
    out = tmp_path / "out.jsonl"
    args = ["--board", "lilygo-t-a7670g-r2", "--interface", "modem", "--period", "0.2"]
    assert (
        cli.main(["recording", "nmea", str(log), str(out), *args, "--start", "0", "--end", "loop"])
        == 0
    )
    made = recording.read(out)
    assert [event.t for event in made.events] == [0, 200_000_000]
    assert (made.header.board, made.header.interface, made.header.end) == (
        "lilygo-t-a7670g-r2",
        "uart.modem",
        "loop",
    )
    assert made.header.extra["converted"] == {
        "from": "nmea",
        "baud": 115200,
        "period_ns": 200_000_000,
        "start_ns": 0,
    }


def test_recording_retime_writes_the_capture_on_the_emulated_clock(tmp_path: Path) -> None:
    header = recording.Header(
        "esp32-devkitc", "uart.gnss", "real", recording.WALL, "2026-11-19T10:00:00Z"
    )
    captured = tmp_path / "capture.jsonl"
    recording.write(
        captured,
        recording.Recording(
            header,
            (
                recording.Event(5_000, "out", b"AT", recording.WALL),
                recording.Event(9_000, "in", b"$GP", recording.WALL),
            ),
        ),
    )
    out = tmp_path / "out.jsonl"
    assert cli.main(["recording", "retime", str(captured), str(out), "--start", "2.5"]) == 0
    made = recording.read(out)
    assert made.events == (recording.Event(2_500_000_000, "in", b"$GP", recording.EMULATED),)
    assert made.header.end == "hold"
    assert cli.main(["recording", "retime", str(captured), str(out), "--end", "loop"]) == 0
    assert recording.read(out).header.end == "loop"


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (
            ["retime", str(RECORDING), "out.jsonl"],
            "esp32tl: the recording is on the emulated-fifo-write clock already: a replay takes"
            " it as it is\n",
        ),
        (
            ["retime", "recordings/none.jsonl", "out.jsonl"],
            "esp32tl: recordings/none.jsonl: [Errno 2] No such file or directory:"
            " 'recordings/none.jsonl'\n",
        ),
        (
            ["nmea", str(RECORDING), "out.jsonl"],
            f'esp32tl: {RECORDING}:1: \'{{"esp32tl_recording": 1, "board": "esp32\''
            " is not an NMEA sentence: a sentence starts with $\n",
        ),
        (
            ["nmea", str(NEO_M8N.with_suffix(".nmea")), "out.jsonl", "--interface", "modem"],
            "esp32tl: the board profile esp32-devkitc has no uart.modem; its interfaces are"
            " console, uart.gnss, sd_card, wifi, bluetooth\n",
        ),
        (
            ["nmea", str(NEO_M8N.with_suffix(".nmea")), "out.jsonl", "--period", "0.5"],
            f"esp32tl: {NEO_M8N.with_suffix('.nmea')}:{log_line('$GNRMC,171927.00,')}: time"
            f" 171927.00 is 1 s after 171926.00 on line {log_line('$GNRMC,171926.00,')}, not one"
            " period of 0.5 s: the log skips epochs, or the receiver sends at another rate\n",
        ),
    ],
)
def test_a_conversion_refused_exits_2_and_writes_nothing(
    args: list[str],
    message: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(ROOT)
    args = [arg if arg != "out.jsonl" else str(tmp_path / arg) for arg in args]
    assert cli.main(["recording", *args]) == 2
    assert capsys.readouterr().err == message
    assert not (tmp_path / "out.jsonl").exists()


def test_a_write_that_fails_leaves_the_recording_it_would_replace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    header = recording.Header(
        "esp32-devkitc", "uart.gnss", "real", recording.WALL, "2026-11-19T10:00:00Z"
    )
    captured = tmp_path / "capture.jsonl"
    recording.write(
        captured,
        recording.Recording(header, (recording.Event(9_000, "in", b"$GP", recording.WALL),)),
    )
    before = captured.read_bytes()

    def full(source: object, destination: object) -> None:
        raise OSError(28, "No space left on device")

    # The capture converted into itself, and the disk full before the rename.
    monkeypatch.setattr(os, "replace", full)
    assert cli.main(["recording", "retime", str(captured), str(captured)]) == 2
    assert capsys.readouterr().err == "esp32tl: [Errno 28] No space left on device\n"
    assert captured.read_bytes() == before
    assert sorted(path.name for path in tmp_path.iterdir()) == ["capture.jsonl"]
    monkeypatch.undo()
    # Into itself it works, and the file keeps the mode a new one gets.
    assert cli.main(["recording", "retime", str(captured), str(captured)]) == 0
    assert recording.read(captured).header.clock == recording.EMULATED
    assert sorted(path.name for path in tmp_path.iterdir()) == ["capture.jsonl"]
    umask = os.umask(0)
    os.umask(umask)
    assert captured.stat().st_mode & 0o777 == 0o666 & ~umask


@pytest.mark.parametrize("start", ["-1", "inf", "soon"])
def test_start_is_a_number_from_0(start: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["recording", "retime", "in", "out", "--start", start])
    assert exit_info.value.code == 2
    assert (
        f"argument --start: {start!r} is not a number of seconds from 0" in capsys.readouterr().err
    )
