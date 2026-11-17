"""The runner binary with `--board socket:PATH`, against a minimal peer written here: what
`main.rs` adds to the session the Rust tests cover, the connection, `--console socket`, the
signals and the exit codes of docs/protocol.md, section 9. hello_world runs on the board; its pins
are not watched, so its console is the only traffic. Without a peer, which console `--console-port`
puts on stdout, on `firmware/console_repl_usb`'s images for the S3, C3 and C6."""

import base64
import json
import shutil
import signal
import socket
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from esp32tl.backends import esp32sim

FIRMWARE = Path(__file__).resolve().parent.parent / "firmware"


@pytest.fixture
def socket_path() -> Iterator[Path]:
    # short: sun_path holds 104 bytes on macOS, and pytest's tmp_path can be longer
    directory = Path(tempfile.mkdtemp(prefix="e32tl-", dir="/tmp"))
    yield directory / "board.sock"
    shutil.rmtree(directory)


def command(path: Path, *options: str, max_seconds: float = 0.5) -> list[str]:
    app = esp32sim.load_app(str(FIRMWARE / "hello_world"))
    runner, rom = esp32sim.runner_path(), esp32sim.rom_path()
    return [*esp32sim.runner_command(runner, rom, app, max_seconds, path), *options]


class Peer:
    """The harness's end: listens before the runner starts, then reads and writes lines."""

    def __init__(self, path: Path, *options: str, max_seconds: float = 0.5) -> None:
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(str(path))
        self.server.listen(1)
        self.process = subprocess.Popen(
            command(path, *options, max_seconds=max_seconds),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.server.settimeout(30)
        self.connection, _ = self.server.accept()
        self.connection.settimeout(30)
        self.file = self.connection.makefile("rwb")
        self.seen: list[dict[str, Any]] = []

    def read(self) -> dict[str, Any]:
        line = self.file.readline()
        assert line, f"end of file after {[m['type'] for m in self.seen]}"
        message: dict[str, Any] = json.loads(line)
        self.seen.append(message)
        return message

    def read_until(self, kind: str) -> dict[str, Any]:
        while (message := self.read())["type"] != kind:
            pass
        return message

    def write(self, **message: Any) -> None:
        self.file.write(json.dumps(message).encode() + b"\n")
        self.file.flush()

    def hello(self, version: int = 2) -> dict[str, Any]:
        runner = self.read()
        self.write(type="hello", version=version, chip="esp32", board="esp32-devkitc", watch=[5])
        return runner

    def rest(self) -> list[dict[str, Any]]:
        """Everything up to end of file, which is what a harness reads at the end of a run."""
        messages = [json.loads(line) for line in self.file.read().splitlines()]
        self.seen += messages
        return messages

    def wait(self) -> tuple[int, str, str]:
        out, err = self.process.communicate(timeout=60)
        self.connection.close()
        self.server.close()
        return self.process.returncode, out.decode(), err.decode()


def test_a_run_to_max_seconds_with_the_console_on_the_socket(socket_path: Path) -> None:
    peer = Peer(socket_path, "--console", "socket")
    hello = peer.hello()
    assert hello["type"] == "hello" and hello["version"] == 2 and hello["chip"] == "esp32"
    # turn 0 comes before the guest's first instruction
    assert peer.read() == {"type": "sync", "t": 0}
    peer.write(type="wait", ack=0, quiet_until=None, latency=None)
    rest = peer.rest()
    code, out, err = peer.wait()
    assert code == 0, err
    console = b"".join(base64.b64decode(m["data"]) for m in rest if m["type"] == "console")
    assert b"Hello world!" in console
    assert "Hello world!" not in out, "--console socket keeps the console off stdout"
    stats = rest[-1]
    assert stats["type"] == "stats" and stats["end"] == "max_seconds"
    # where the last slice ended: --max-seconds, or less than one CPU cycle (6.1) after it
    assert 500_000_000 <= stats["t"] < 500_000_025 and stats["turns"] == 1
    assert "esp32tl-runner: end: max_seconds at 0.5000" in err.splitlines()[-1]


def test_sigterm_while_the_runner_waits_for_the_peer_ends_the_run_with_stats(
    socket_path: Path,
) -> None:
    peer = Peer(socket_path, "--peer-timeout", "0", max_seconds=10)
    peer.hello()
    peer.read_until("sync")
    peer.write(type="wait", ack=0, quiet_until=200_000_000, latency=None)
    assert peer.read_until("sync")["t"] == 200_000_000
    # the peer does not answer: the runner is blocked in its turn, which --peer-timeout 0 never
    # ends, and only the signal ends the run, at that turn, within the 30 s the peer reads for
    peer.process.send_signal(signal.SIGTERM)
    rest = peer.rest()
    code, _, err = peer.wait()
    assert code == 0, err
    assert rest[-1]["type"] == "stats" and rest[-1]["end"] == "signal"
    assert rest[-1]["t"] == 200_000_000
    assert "end: signal" in err.splitlines()[-1]


def test_a_late_input_is_a_protocol_error_with_exit_code_3(socket_path: Path) -> None:
    peer = Peer(socket_path)
    peer.hello()
    peer.read_until("sync")
    peer.write(type="wait", ack=0, quiet_until=5_000_000, latency=None)
    assert peer.read_until("sync")["t"] == 5_000_000
    data = base64.b64encode(b"$GP").decode()
    peer.write(type="uart_rx", t=4_999_999, pin=5, baud=9600, data=data)
    peer.write(type="wait", ack=0)
    rest = peer.rest()
    code, _, err = peer.wait()
    assert code == 3
    assert rest[-1]["type"] == "stats" and rest[-1]["end"] == "protocol_error"
    assert "rule 2" in rest[-1]["detail"]
    last = err.splitlines()[-1]
    assert f"protocol error by the peer at socket:{socket_path}" in last and "4999999" in last


def test_a_version_mismatch_ends_the_run_with_stats_and_exit_code_3(socket_path: Path) -> None:
    peer = Peer(socket_path)
    peer.hello(version=99)
    rest = peer.rest()
    code, _, err = peer.wait()
    assert code == 3, err
    assert [m["type"] for m in rest] == ["stats"]
    assert "version 99" in rest[0]["detail"]


def test_a_peer_that_does_not_answer_in_time_is_gone_with_exit_code_4(socket_path: Path) -> None:
    peer = Peer(socket_path, "--peer-timeout", "0.2")
    peer.hello()
    peer.read_until("sync")
    rest = peer.rest()
    code, _, err = peer.wait()
    assert code == 4, err
    assert rest == [], "nothing more after the peer is gone, not even stats"
    assert "--peer-timeout" in err.splitlines()[-1]


def test_no_peer_listening_is_exit_code_4(socket_path: Path) -> None:
    run = subprocess.run(command(socket_path), capture_output=True, timeout=60)
    assert run.returncode == 4
    assert f"cannot connect to the peer at socket:{socket_path}" in run.stderr.decode()


def test_a_flash_dump_writes_the_whole_flash_in_a_turn_and_the_run_goes_on(
    socket_path: Path,
) -> None:
    out = socket_path.parent / "flash.bin"
    peer = Peer(socket_path, "--flash-out", str(out), "--flash-mb", "2")
    peer.hello()
    peer.read_until("sync")
    peer.write(type="flash_dump")
    assert peer.read_until("flash_dump_ack") == {"type": "flash_dump_ack", "t": 0, "error": None}
    flash = out.read_bytes()
    app = esp32sim.load_app(str(FIRMWARE / "hello_world"))
    assert len(flash) == 2 << 20
    for file in app.flash_files:
        image = file.path.read_bytes()
        assert flash[file.offset : file.offset + len(image)] == image
    assert not (socket_path.parent / "flash.bin.partial").exists()
    out.unlink()
    peer.write(type="wait", ack=0, quiet_until=None, latency=None)
    assert peer.rest()[-1]["end"] == "max_seconds"
    assert peer.wait()[0] == 0
    assert out.read_bytes() == flash, "hello_world writes nothing to its flash"


def test_the_file_is_there_once_stats_have_come_whatever_ended_the_run(
    socket_path: Path,
) -> None:
    out = socket_path.parent / "flash.bin"
    peer = Peer(socket_path, "--flash-out", str(out))
    peer.hello()
    peer.read_until("sync")
    peer.write(type="wait", ack=5)
    assert peer.read_until("stats")["end"] == "protocol_error"
    assert len(out.read_bytes()) == 4 << 20
    assert peer.wait()[0] == 3


def test_a_flash_dump_without_flash_out_is_a_protocol_error_with_exit_code_3(
    socket_path: Path,
) -> None:
    peer = Peer(socket_path)
    peer.hello()
    peer.read_until("sync")
    peer.write(type="flash_dump")
    assert "flash_dump without --flash-out" in peer.read_until("stats")["detail"]
    assert peer.wait()[0] == 3


def test_flash_out_without_a_board_writes_the_flash_at_the_end_of_the_run(
    socket_path: Path,
) -> None:
    out = socket_path.parent / "flash.bin"
    app = esp32sim.load_app(str(FIRMWARE / "hello_world"))
    command = [str(esp32sim.runner_path()), "--chip", "esp32", "--rom", str(esp32sim.rom_path())]
    command += ["--max-seconds", "0.01", "--flash-out", str(out)]
    for file in app.flash_files:
        command += ["--flash-at", f"{file.offset:#x}={file.path}"]
    subprocess.run(command, check=True, capture_output=True, timeout=60)
    assert len(out.read_bytes()) == 4 << 20


@pytest.mark.parametrize("chip", ["esp32s3", "esp32c3", "esp32c6"])
def test_the_runner_prints_the_console_port_it_is_given_on_stdout(chip: str) -> None:
    # without a peer, stdout is the console's alone: the USB-Serial/JTAG controller's with
    # --console-port usb (esp32sim's mask bit 0), UART0's, the ROM's banner alone, without it
    app = esp32sim.load_app(
        str(FIRMWARE / "console_repl_usb"), build_dir=f"build-{chip}", chip=chip
    )
    command = [
        str(esp32sim.runner_path()),
        *("--chip", chip, "--rom", str(esp32sim.rom_path(chip)), "--max-seconds", "2"),
        *(f for file in app.flash_files for f in ("--flash-at", f"{file.offset:#x}={file.path}")),
    ]
    usb = subprocess.run([*command, "--console-port", "usb"], capture_output=True, timeout=120)
    uart0 = subprocess.run([*command, "--console-port", "uart0"], capture_output=True, timeout=120)
    assert usb.returncode == uart0.returncode == 0, usb.stderr + uart0.stderr
    assert b"2nd stage bootloader" in usb.stdout and b"console_repl>" in usb.stdout
    assert uart0.stdout.startswith(b"ESP-ROM:") and b"entry 0x" in uart0.stdout
    assert b"2nd stage bootloader" not in uart0.stdout
