"""The runner binary with `--board socket:PATH`, against a minimal peer written here: what
`main.rs` adds to the session the Rust tests cover, the connection, `--console socket`, the
signals and the exit codes of docs/protocol.md, section 9. hello_world runs on the board; its pins
are not watched, so its console is the only traffic."""

import base64
import json
import shutil
import signal
import socket
import subprocess
import tempfile
import time
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

    def hello(self, version: int = 1) -> dict[str, Any]:
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
    assert hello["type"] == "hello" and hello["version"] == 1 and hello["chip"] == "esp32"
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
    # the peer does not answer: the runner is blocked in its turn
    start = time.monotonic()
    peer.process.send_signal(signal.SIGTERM)
    rest = peer.rest()
    code, _, err = peer.wait()
    assert code == 0, err
    assert time.monotonic() - start < 2
    assert rest[-1]["type"] == "stats" and rest[-1]["end"] == "signal"
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
    peer.hello(version=2)
    rest = peer.rest()
    code, _, err = peer.wait()
    assert code == 3, err
    assert [m["type"] for m in rest] == ["stats"]
    assert "version 2" in rest[0]["detail"]


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
