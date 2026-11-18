"""A pytest killed without its teardown, in the middle of a long `board.wait`, leaves nothing
running: the runner reads end of file on `--parent-fd` and ends the run, the log forwarder finds
its parent gone and ends pytest-embedded's `MessageQueueManager`, and with it go the dut's listener
and Python's resource tracker (#258). The pieces are tested on their own first, with nothing but
the package; the killed session needs `make firmware` (console_repl), which sits at its prompt: no
event comes to end the long window of the wait."""

import json
import os
import signal
import subprocess
import sys
import time
from multiprocessing.managers import BaseManager
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest
from pytest_embedded.log import MessageQueue
from test_flash_size import board_on, profile, sized_build

from esp32tl.backends import esp32sim
from esp32tl.backends.esp32sim import BackendError

FIRMWARE = Path(__file__).parent.parent / "firmware" / "console_repl"

# How long, in wall seconds, what the killed session started has to end. The runner ends within a
# slice, the forwarder within LOG_POLL_SECONDS; a slow CI machine gets the rest.
BOUND = 5


def alive(pid: int) -> bool:
    """Whether `pid` is a process that has not exited: a zombie, which a container's init may be
    slow to reap, counts as gone."""
    stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    state = stat.stdout.strip()
    return state != "" and not state.startswith("Z")


def test_the_runner_gets_the_read_end_of_a_pipe_whose_write_end_the_backend_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sized_build(tmp_path)
    board = board_on(tmp_path, monkeypatch, profile(None))
    popen = Mock(side_effect=OSError(2, "No such file or directory"))
    monkeypatch.setattr(esp32sim, "RunnerPopen", popen)
    with pytest.raises(BackendError, match="cannot start"):
        board.power_on()
    command = popen.call_args.args[1]
    fd = int(command[command.index("--parent-fd") + 1])
    assert popen.call_args.kwargs == {"pass_fds": (fd,)}
    # a runner that could not start leaves neither end open
    assert board._parent is None
    with pytest.raises(OSError):
        os.fstat(fd)
    board.close()


def test_a_power_on_that_fails_before_the_start_closes_the_pipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not only `RunnerPopen`'s `OSError`: whatever fails once the pipe is made closes both ends."""
    sized_build(tmp_path)
    board = board_on(tmp_path, monkeypatch, profile(None))
    made: list[tuple[int, int]] = []
    pipe = os.pipe

    def recorded() -> tuple[int, int]:
        made.append(pipe())
        return made[-1]

    def broken(*args: object, **kwargs: object) -> list[str]:
        raise RuntimeError("no command")

    monkeypatch.setattr(os, "pipe", recorded)
    monkeypatch.setattr(esp32sim, "runner_command", broken)
    with pytest.raises(RuntimeError, match="no command"):
        board.power_on()
    [(read, write)] = made
    assert board._parent is None
    for fd in (read, write):
        with pytest.raises(OSError):
            os.fstat(fd)
    # and the link's directory, which `close` would not remove: the board has no runner
    assert not board._socket.parent.exists()
    board.close()


def test_a_forwarder_whose_parent_is_gone_reads_the_rest_ends_the_manager_and_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_forward_log` in this process, told that its parent is a pid other than its parent's: the
    first read that finds nothing new ends the loop, without a sleep, and after one read more it
    ends the manager of its queue."""
    log = tmp_path / "popen.log"
    log.write_bytes(b"first\n")
    put: list[bytes] = []
    ended: list[object] = []

    class Queue:
        def put(self, data: bytes) -> None:
            put.append(data)

    def sleep(seconds: float) -> None:
        raise AssertionError("an orphaned forwarder does not sleep")

    monkeypatch.setattr(esp32sim, "time", SimpleNamespace(sleep=sleep))
    monkeypatch.setattr(esp32sim, "end_manager", ended.append)
    queue = Queue()
    esp32sim._forward_log(cast(MessageQueue, queue), str(log), parent=os.getppid() + 1)
    assert put == [b"first\n"]
    assert ended == [queue]


class Manager(BaseManager):
    pass


Manager.register("Queue", Queue)


def test_end_manager_shuts_down_the_server_process_of_a_proxy_s_manager() -> None:
    """As pytest-embedded's `MessageQueueManager`: a `BaseManager` whose server is a process of its
    own, and a queue that is a proxy to it. A queue that is no proxy is left alone."""
    esp32sim.end_manager(cast(MessageQueue, Queue()))
    manager = Manager()
    manager.start()
    try:
        server = manager._process  # type: ignore[attr-defined]
        esp32sim.end_manager(cast(MessageQueue, manager.Queue()))  # type: ignore[attr-defined]
        server.join(timeout=10)
        assert server.exitcode == 0
    finally:
        manager.shutdown()


def test_a_killed_pytest_leaves_no_runner_forwarder_or_manager(pytester: pytest.Pytester) -> None:
    if not (FIRMWARE / "build" / "esp32tl-built").is_file():
        pytest.fail(f"{FIRMWARE}/build holds no finished build: run make firmware", pytrace=False)
    pids = pytester.path / "pids.json"
    pytester.makepyfile(
        test_long=f"""
        import json
        import multiprocessing.resource_tracker as tracker
        import os

        import pytest

        from esp32tl.units import seconds

        @pytest.mark.app_path({str(FIRMWARE)!r})
        def test_long(board, console, request):
            board.power_on()
            console.expect_exact("console_repl>")
            pids = {{
                "runner": board.process.pid,
                "forwarder": board.process._p.pid,
                "manager": request.getfixturevalue("_mp_manager")._process.pid,
                "listener": request.getfixturevalue("_listener").pid,
                "resource tracker": tracker._resource_tracker._pid,
            }}
            with open({str(pids)!r} + ".part", "w") as out:
                json.dump(pids, out)
            os.rename({str(pids)!r} + ".part", {str(pids)!r})
            board.wait(seconds(3600))
        """
    )
    inner = pytester.popen(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "test_long.py"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 120
        while not pids.exists():
            assert inner.poll() is None, "the inner pytest ended before its long wait"
            assert time.monotonic() < deadline, "the inner test did not reach its long wait"
            time.sleep(0.1)
        started = json.loads(pids.read_text())
        # well into the wait, the runner at full speed
        time.sleep(1)
        assert all(alive(pid) for pid in started.values()), started
    finally:
        inner.send_signal(signal.SIGKILL)
        inner.wait()
    killed = time.monotonic()
    left = dict(started)
    while left and time.monotonic() - killed < BOUND:
        left = {name: pid for name, pid in left.items() if alive(pid)}
        time.sleep(0.05)
    # what is left would run for good: end it, so the failure does not leak it too
    for pid in left.values():
        subprocess.run(["kill", "-9", str(pid)], check=False)
    assert left == {}, f"still running {BOUND} s after the kill"
