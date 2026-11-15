"""The CI wrapper must really overlap shards and propagate either shard's failure."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / ".github/scripts/test-pair.py"


@pytest.mark.parametrize("codes", [(0, 0), (1, 0), (0, 1), (0, 5)])
def test_pair_overlaps_isolates_and_reports_both_shards(
    tmp_path: Path, codes: tuple[int, int]
) -> None:
    (tmp_path / "pytest.py").write_text("""
import json, os, sys, time
from pathlib import Path
root = Path(os.environ["PAIR_RECORD"])
shard = next(a for a in sys.argv if a.startswith("--shard="))
number = shard.split("=")[1].split("/")[0]
(root / (number + ".json")).write_text(json.dumps({
    "args": sys.argv, "tmp": os.environ["TMPDIR"]
}))
other = "4" if number == "1" else "1"
deadline = time.monotonic() + 10
while not (root / (other + ".json")).exists():
    if time.monotonic() > deadline:
        raise RuntimeError("the other shard did not start concurrently")
    time.sleep(0.01)
print("finished shard " + number)
sys.exit(int(os.environ["PAIR_CODE_" + number]))
""")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "1/4", "4/4"],
        env={
            **os.environ,
            "PYTHONPATH": str(tmp_path),
            "PAIR_RECORD": str(tmp_path),
            "PAIR_CODE_1": str(codes[0]),
            "PAIR_CODE_4": str(codes[1]),
        },
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == (0 if codes == (0, 0) else 1), result.stdout + result.stderr
    records = [json.loads((tmp_path / f"{k}.json").read_text()) for k in (1, 4)]
    assert records[0]["tmp"] != records[1]["tmp"]
    for k, record, code in zip((1, 4), records, codes, strict=True):
        assert f"--basetemp={record['tmp']}/tests" in record["args"]
        assert f"cache_dir={record['tmp']}/cache" in record["args"]
        assert f"finished shard {k}" in result.stdout
        assert f"shard {k}/4: exit {code}" in result.stdout


@pytest.mark.parametrize("arguments", [[], ["1/4"], ["1/4", "1/4"], ["0/4", "2/4"]])
def test_pair_rejects_incomplete_or_duplicate_selection(arguments: list[str]) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *arguments], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 2


def test_pair_cancellation_stops_active_shards(tmp_path: Path) -> None:
    import signal
    import time

    (tmp_path / "pytest.py").write_text("""
import os, sys, time
from pathlib import Path
number = next(a for a in sys.argv if a.startswith("--shard=")).split("=")[1][0]
Path(os.environ["PAIR_RECORD"], number + ".pid").write_text(str(os.getpid()))
print("running " + number, flush=True)
while True:
    time.sleep(0.1)
""")
    process = subprocess.Popen(
        [sys.executable, str(SCRIPT), "1/4", "4/4"],
        env={**os.environ, "PYTHONPATH": str(tmp_path), "PAIR_RECORD": str(tmp_path)},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not all((tmp_path / f"{k}.pid").exists() for k in (1, 4)):
            assert time.monotonic() < deadline, "shards did not start"
            time.sleep(0.01)
        process.send_signal(signal.SIGTERM)
        output, _ = process.communicate(timeout=10)
        assert process.returncode == 130, output
        for k in (1, 4):
            pid = int((tmp_path / f"{k}.pid").read_text())
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)


@pytest.mark.parametrize("first_leader_exits", [False, True])
def test_pair_cancellation_kills_descendants_ignoring_sigterm(
    tmp_path: Path, first_leader_exits: bool
) -> None:
    import signal
    import time
    from contextlib import suppress

    (tmp_path / "child.py").write_text("""
import os, signal, sys, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(sys.argv[1]).write_text(str(os.getpid()))
while True:
    time.sleep(0.1)
""")
    (tmp_path / "pytest.py").write_text("""
import os, subprocess, sys, time
from pathlib import Path
root = Path(os.environ["PAIR_RECORD"])
number = next(a for a in sys.argv if a.startswith("--shard=")).split("=")[1][0]
child_pid = root / (number + ".child.pid")
subprocess.Popen([sys.executable, str(root / "child.py"), str(child_pid)])
while not child_pid.exists():
    time.sleep(0.01)
(root / (number + ".leader.pid")).write_text(str(os.getpid()))
if number == "1" and os.environ["FIRST_LEADER_EXITS"] == "1":
    sys.exit(0)
while True:
    time.sleep(0.1)
""")

    def running(pid: int) -> bool:
        # A killed orphan may briefly remain a zombie until the host's init reaps it.
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True, check=False
        )
        state = result.stdout.strip()
        return bool(state) and not state.startswith("Z")

    process = subprocess.Popen(
        [sys.executable, str(SCRIPT), "1/4", "4/4"],
        env={
            **os.environ,
            "PYTHONPATH": str(tmp_path),
            "PAIR_RECORD": str(tmp_path),
            "FIRST_LEADER_EXITS": str(int(first_leader_exits)),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not all((tmp_path / f"{k}.leader.pid").exists() for k in (1, 4)):
            assert time.monotonic() < deadline, "shard descendants did not start"
            time.sleep(0.01)
        if first_leader_exits:
            pid = int((tmp_path / "1.leader.pid").read_text())
            while running(pid):
                assert time.monotonic() < deadline, "first shard did not exit"
                time.sleep(0.01)
        children = [int((tmp_path / f"{k}.child.pid").read_text()) for k in (1, 4)]
        assert all(running(pid) for pid in children)
        process.send_signal(signal.SIGTERM)
        output, _ = process.communicate(timeout=12)
        assert process.returncode == 130, output
        deadline = time.monotonic() + 5
        while any(running(pid) for pid in children):
            assert time.monotonic() < deadline, f"shard descendants survived cancellation: {output}"
            time.sleep(0.01)
    finally:
        # Also clean up after reproducing a broken wrapper.
        for path in tmp_path.glob("*.pid"):
            with suppress(ProcessLookupError):
                os.kill(int(path.read_text()), signal.SIGKILL)
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


def test_group_cleanup_reaps_a_leader_that_has_already_exited() -> None:
    import runpy
    import time

    stop_groups = runpy.run_path(str(SCRIPT))["stop_groups"]
    process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    try:
        deadline = time.monotonic() + 10
        while True:
            state = subprocess.check_output(
                ["ps", "-p", str(process.pid), "-o", "stat="], text=True
            )
            if state.strip().startswith("Z"):
                break
            assert time.monotonic() < deadline, "leader did not exit"
            time.sleep(0.01)
        # Do not poll/wait here: cleanup must handle the unreaped leader itself.
        stop_groups([process])
        assert process.returncode == 0
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()


def test_pair_keeps_multiprocessing_sockets_short_with_a_long_host_tmpdir(tmp_path: Path) -> None:
    host_tmp = tmp_path / ("long-host-temporary-directory-" * 3)
    host_tmp.mkdir()
    (tmp_path / "pytest.py").write_text("""
import multiprocessing

if __name__ == "__main__":
    with multiprocessing.get_context("spawn").Manager() as manager:
        values = manager.list(["socket works"])
        assert values[0] == "socket works"
        print(values[0])
""")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "1/4", "4/4"],
        env={**os.environ, "PYTHONPATH": str(tmp_path), "TMPDIR": str(host_tmp)},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("socket works") == 2
