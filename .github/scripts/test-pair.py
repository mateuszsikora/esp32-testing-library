#!/usr/bin/env python3
"""Run two existing pytest shards concurrently, failing if either fails."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack, suppress
from pathlib import Path


def stop_groups(processes: list[subprocess.Popen[bytes]]) -> None:
    groups = set()
    for process in processes:
        # A reaped leader can still have descendants in its process group.
        process.poll()
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except PermissionError:
            # A leader can become a non-signalable zombie between poll and killpg
            # on macOS. Track the group until it disappears or needs escalation.
            pass
        groups.add(process.pid)

    deadline = time.monotonic() + 5
    while groups and time.monotonic() < deadline:
        for process in processes:
            process.poll()  # Reap leaders so their zombies do not keep groups alive.
        for group in groups.copy():
            try:
                os.killpg(group, 0)
            except ProcessLookupError:
                groups.remove(group)
            except PermissionError:
                # Still present, but not signalable (e.g. a dying group on macOS).
                # Keep tracking it; only ESRCH proves the group disappeared.
                pass
        if groups:
            time.sleep(0.05)
    for group in groups:
        with suppress(ProcessLookupError):
            os.killpg(group, signal.SIGKILL)
    for process in processes:
        process.wait()


def run_pair(shards: list[str], arguments: list[str]) -> int:
    processes: list[subprocess.Popen[bytes]] = []
    with (
        tempfile.TemporaryDirectory(prefix="e32-pair-", dir="/tmp") as temporary,
        ExitStack() as stack,
    ):
        root = Path(temporary)
        logs = []
        try:
            for index, shard in enumerate(shards):
                directory = root / str(index)
                directory.mkdir()
                log = directory / "pytest.log"
                logs.append(log)
                output = stack.enter_context(log.open("wb"))
                command = [
                    sys.executable,
                    "-m",
                    "pytest",
                    f"--shard={shard}",
                    f"--basetemp={directory / 'tests'}",
                    "-o",
                    f"cache_dir={directory / 'cache'}",
                    *arguments,
                ]
                print(f"Starting shard {shard}", flush=True)
                processes.append(
                    subprocess.Popen(
                        command,
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        env={**os.environ, "TMPDIR": str(directory)},
                        start_new_session=True,
                    )
                )
            codes = [process.wait() for process in processes]
        finally:
            # Each pytest owns a process group, including its pytester/runner children.
            # On cancellation do not leave them running after the temporary files disappear.
            stop_groups(processes)
            for shard, log in zip(shards, logs, strict=False):
                print(f"\n--- shard {shard} ---", flush=True)
                print(log.read_text(errors="replace"), end="", flush=True)
        for shard, code in zip(shards, codes, strict=True):
            print(f"shard {shard}: exit {code}", flush=True)
        return 0 if all(code == 0 for code in codes) else 1


def main() -> int:
    if len(sys.argv) < 3 or any(not re.fullmatch(r"[1-4]/4", s) for s in sys.argv[1:3]):
        print("usage: test-pair.py K/4 J/4 [pytest arguments...]", file=sys.stderr)
        return 2
    if sys.argv[1] == sys.argv[2]:
        print("test-pair.py: the shards must be different", file=sys.stderr)
        return 2

    def interrupted(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        return run_pair(sys.argv[1:3], sys.argv[3:])
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
