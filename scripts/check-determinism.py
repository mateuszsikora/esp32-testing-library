#!/usr/bin/env python3
"""Runs the determinism tests twice and compares the two runs (plan §6, the determinism job).

    .venv/bin/python scripts/check-determinism.py [--out DIR] [PYTEST_ARG...]   (make determinism)

Each run is `python -m pytest` on the PYTEST_ARGs, options included (`tests/test_nmea_reader.py
-k fix`), `tests/test_determinism.py` when there are none, with a `--basetemp` and a `--junitxml`
of its own under DIR (`build/determinism` unless given); after a `--`, the arguments come after
the script's own options. What an earlier check left in DIR is removed first; a DIR that holds
anything else, or is not a directory, is refused, exit 2, so that a mistyped `--out` removes
nothing. Both runs must pass. They must then have left:

- the same files under their basetemps, byte for byte, except for the `created` field of a
  recording's header, which is when the tap was written. The tests write what the board did into
  their `tmp_path`: its console with the board's times, and its taps;
- for each test, the same `esp32sim_link` property in the JUnit XML: the link's warnings and
  error, the runner's stats (where the run ended, its turns, the bytes each UART moved) and the
  bytes on each route, with the run's basetemp in a path written as `{basetemp}`.

Each difference is printed as `✗ <what>`, with a diff where both runs have the file or the test,
and the script exits 1; so does a run that fails, before anything is compared, and two runs that
left no file and no test that used the board, since nothing was compared. Nothing in the runs speaks
TLS yet, so everything is compared, times included. Standard library only.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ["tests/test_determinism.py"]
OUT = ROOT / "build" / "determinism"
RUNS = ("run-1", "run-2")
# What a check leaves in its DIR, and all that the next one removes there.
OUTPUTS = {name for run in RUNS for name in (run, f"{run}.xml")}
# The user property the plugin gives every test that uses `board` (pytest-plugin-user.md).
PROPERTY = "esp32sim_link"
# How many lines of a file's diff are printed.
DIFF_LINES = 40


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the determinism tests twice and compare the two runs.",
        usage="%(prog)s [-h] [--out DIR] [PYTEST_ARG...]",
        # --out abbreviated would take a pytest option that starts the same way.
        allow_abbrev=False,
    )
    parser.add_argument(
        "--out", type=Path, default=OUT, metavar="DIR", help=f"default: {OUT.relative_to(ROOT)}"
    )
    # Everything argparse does not know is pytest's, its options included, in the order given.
    args, pytest_args = parser.parse_known_args(argv)
    out = args.out.resolve()
    if out.exists() and not out.is_dir():
        print(f"✗ {out} is not a directory: name an empty or new directory with --out")
        return 2
    if out.exists():
        others = sorted(p.name for p in out.iterdir() if p.name not in OUTPUTS)
        if others:
            named = ", ".join(others[:3]) + (f" and {len(others) - 3} more" if others[3:] else "")
            print(
                f"✗ {out} holds {named}, which no determinism check left there:"
                " name an empty or new directory with --out"
            )
            return 2
        for name in OUTPUTS:
            if (out / name).is_dir():
                shutil.rmtree(out / name)
            else:
                (out / name).unlink(missing_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    # The script's options go before a `--`, after which pytest takes every argument as a path.
    split = pytest_args.index("--") if "--" in pytest_args else len(pytest_args)
    before, after = pytest_args[:split], pytest_args[split:]
    if not before and after[1:] == []:
        before, after = TESTS, []
    for run in RUNS:
        command = [
            sys.executable,
            "-m",
            "pytest",
            *before,
            f"--basetemp={out / run}",
            f"--junitxml={out / run}.xml",
            # pytest-embedded's app_path fixture records an XML attribute, on which xunit2 warns:
            # "record_xml_attribute is incompatible with junit_family 'xunit2' (use 'legacy' or
            # 'xunit1')".
            "-o",
            "junit_family=xunit1",
            # The second run must not be told what the first did.
            "-p",
            "no:cacheprovider",
            *after,
        ]
        print(f"determinism: {run}: {' '.join(command[1:])}", flush=True)
        code = subprocess.run(command, cwd=ROOT).returncode
        if code != 0:
            print(f"✗ {run}: pytest exited with code {code}; the runs were not compared")
            return 1
    failures, files = compare_files(out / RUNS[0], out / RUNS[1])
    link_failures, tests = compare_links(out, *RUNS)
    failures += link_failures
    for failure in failures:
        print(f"✗ {failure}")
    if failures:
        print(f"determinism: the two runs differ in {count(len(failures), 'place')}")
        return 1
    if files == 0 and tests == 0:
        print("✗ the runs left no file and no test that used the board: nothing was compared")
        return 1
    print(
        f"determinism: the two runs are identical: {count(files, 'file')},"
        f" and the link of {count(tests, 'test')} that used the board"
    )
    return 0


def count(n: int, thing: str) -> str:
    return f"{n} {thing}" if n == 1 else f"{n} {thing}s"


def compare_files(first: Path, second: Path) -> tuple[list[str], int]:
    """What differs between the files under `first` and under `second`, and how many there are.
    Symbolic links, the `*current` ones pytest makes in a basetemp, are left out."""
    ours, theirs = files_under(first), files_under(second)
    failures = [f"{path}: only in {first.name}" for path in sorted(ours - theirs)]
    failures += [f"{path}: only in {second.name}" for path in sorted(theirs - ours)]
    for path in sorted(ours & theirs):
        a, b = (first / path).read_bytes(), (second / path).read_bytes()
        if a != b and comparable(a) != comparable(b):
            failures.append(f"{path} differs:\n{diff(comparable(a), comparable(b), path)}")
    return failures, len(ours | theirs)


def files_under(directory: Path) -> set[str]:
    found = set()
    for parent, _, names in os.walk(directory):
        for name in names:
            path = Path(parent, name)
            if not path.is_symlink():
                found.add(path.relative_to(directory).as_posix())
    return found


def comparable(data: bytes) -> bytes:
    """`data`, or, when it is a recording, the recording without its header's `created`."""
    first, newline, rest = data.partition(b"\n")
    try:
        header = json.loads(first)
    except ValueError:
        return data
    if not isinstance(header, dict) or "esp32tl_recording" not in header:
        return data
    header.pop("created", None)
    return json.dumps(header, sort_keys=True).encode() + newline + rest


def diff(a: bytes, b: bytes, path: str) -> str:
    """The unified diff of two versions of the file `path`, cut after DIFF_LINES lines."""
    lines = list(
        difflib.unified_diff(
            a.decode(errors="backslashreplace").splitlines(),
            b.decode(errors="backslashreplace").splitlines(),
            f"{RUNS[0]}/{path}",
            f"{RUNS[1]}/{path}",
            n=2,
            lineterm="",
        )
    )
    if len(lines) > DIFF_LINES:
        lines = [*lines[:DIFF_LINES], f"... ({len(lines) - DIFF_LINES} more lines of the diff)"]
    return "\n".join(f"    {line}" for line in lines)


def compare_links(out: Path, first: str, second: str) -> tuple[list[str], int]:
    """What differs between the `esp32sim_link` properties of the tests in the JUnit XML of the
    runs `first` and `second`, and how many tests have one."""
    ours, theirs = links(out, first), links(out, second)
    failures = [f"{test}: only in {first}" for test in ours if test not in theirs]
    failures += [f"{test}: only in {second}" for test in theirs if test not in ours]
    for test, link in ours.items():
        if test in theirs and link != theirs[test]:
            a = json.dumps(link, indent=2, sort_keys=True).encode()
            b = json.dumps(theirs[test], indent=2, sort_keys=True).encode()
            failures.append(f"{test}: the link differs:\n{diff(a, b, PROPERTY)}")
    return failures, sum(link is not None for link in ours.values())


def links(out: Path, run: str) -> dict[str, object]:
    """The `esp32sim_link` property of each test of `run`, by test, `None` for a test without
    one, with the run's basetemp written as `{basetemp}`."""
    basetemp = str(out / run)
    found: dict[str, object] = {}
    for case in ET.parse(out / f"{run}.xml").iter("testcase"):
        value = None
        for prop in case.iter("property"):
            if prop.get("name") == PROPERTY:
                # Decoded first: the plugin escapes what is not ASCII, a path's `ł` as \u0142.
                value = without(json.loads(prop.get("value") or "null"), basetemp)
        found[f"{case.get('classname')}::{case.get('name')}"] = value
    return found


def without(value: object, basetemp: str) -> object:
    """`value`, decoded JSON, with `basetemp` written as `{basetemp}` in every string in it."""
    if isinstance(value, str):
        return value.replace(basetemp, "{basetemp}")
    if isinstance(value, list):
        return [without(item, basetemp) for item in value]
    if isinstance(value, dict):
        return {key: without(item, basetemp) for key, item in value.items()}
    return value


if __name__ == "__main__":
    sys.exit(main())
