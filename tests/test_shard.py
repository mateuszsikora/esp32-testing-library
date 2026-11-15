"""`--shard=K/N` of tests/conftest.py, which splits the suite between CI's `test` jobs."""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# A file of many fast tests that need no image.
COLLECTED = "tests/test_check_docs.py"


def collect(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", COLLECTED, *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def ids(*args: str) -> set[str]:
    result = collect(*args)
    assert result.returncode == 0, result.stdout + result.stderr
    return {line for line in result.stdout.splitlines() if "::" in line}


def test_the_shards_divide_the_tests_among_them() -> None:
    every = ids()
    shards = [ids(f"--shard={k}/3") for k in (1, 2, 3)]
    assert all(shards)
    assert set().union(*shards) == every
    assert sum(map(len, shards)) == len(every)


def test_the_workflow_runs_every_shard() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    matrix = re.search(r"^ +pair: (\[.*\])$", workflow, re.MULTILINE)
    assert matrix is not None
    pairs = json.loads(matrix[1])
    assert all(len(pair.split()) == 2 for pair in pairs)
    shards = [shard for pair in pairs for shard in pair.split()]
    assert sorted(shards) == [f"{k}/4" for k in range(1, 5)]
    assert "SHARDS: ${{ matrix.pair }}" in workflow
    assert '.venv/bin/python .github/scripts/test-pair.py "$first" "$second"' in workflow


def test_one_shard_of_one_is_every_test() -> None:
    assert ids("--shard=1/1") == ids()


@pytest.mark.parametrize("value", ["0/3", "4/3", "1/0", "3", "a/b", "-1/3", "1/3/5", ""])
def test_a_shard_that_is_not_k_of_n_is_a_usage_error(value: str) -> None:
    result = collect(f"--shard={value}")
    assert result.returncode == pytest.ExitCode.USAGE_ERROR
    assert f"--shard must be K/N with 1 <= K <= N, not {value!r}" in result.stderr
