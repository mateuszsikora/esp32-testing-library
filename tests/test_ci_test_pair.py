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
