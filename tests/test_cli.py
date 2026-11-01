"""The `esp32tl` command's `run` and `replay`: the arguments they refuse, in this process, and runs
of the validation images through the real runner, in a subprocess of their own. The runs need
`make firmware` (hello_world, nmea_reader, gpio_echo)."""

import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from esp32tl import cli

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
    *args: str, patch: str = "", env: dict[str, str] | None = None
) -> subprocess.Popen[str]:
    for app in (HELLO_WORLD, NMEA_READER, GPIO_ECHO):
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


def test_the_help_names_run_and_replay(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # argparse wraps the help to the terminal's width.
    monkeypatch.setenv("COLUMNS", "80")
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert "run       run an app on the emulator, its console on stdout" in out
    assert "replay    run an app with a recording replayed into one of its board's" in out


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (
            ["run", "--app", "firmware/hello_world", "--board", "nope"],
            "esp32tl: no board profile named 'nope'; the profiles are esp32-devkitc,"
            " lilygo-t-a7670g-r2\n",
        ),
        (
            ["run", "--app", "nowhere"],
            f"esp32tl: {ROOT / 'nowhere' / 'build'} holds no ESP-IDF build: no flasher_args.json\n",
        ),
        (
            [*REPLAY, "--interface", "modem", "--recording", str(RECORDING)],
            "esp32tl: the board profile esp32-devkitc has no uart.modem; its interfaces are"
            " console, uart.gnss, sd_card\n",
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
    assert "esp32tl: board lilygo-t-a7670g-r2, watching GPIO 4, GPIO 33\n" in err


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


# A runner that never connects: it says it is loading, and sleeps.
LOADING_RUNNER = """#!/bin/sh
echo "fake runner: loading" >&2
exec sleep 30
"""


def test_ctrl_c_before_the_runner_connects_exits_130(tmp_path: Path) -> None:
    runner = tmp_path / "runner"
    runner.write_text(LOADING_RUNNER)
    runner.chmod(0o755)
    process = esp32tl("run", "--app", "firmware/hello_world", env={"ESP32TL_RUNNER": str(runner)})
    before = stderr_until(process, "fake runner: loading")
    started = time.monotonic()
    process.send_signal(signal.SIGINT)
    code, _, err = finished(process)
    err = before + err
    # The SIGTERM killed it, without stats, and the command did not wait for its sleep.
    assert time.monotonic() - started < 10
    assert code == 130, err
    assert "esp32tl: the runner never connected" in err
    assert "Traceback" not in err


# A runner that says what it was given and exits without connecting.
ARGS_RUNNER = """#!/bin/sh
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
