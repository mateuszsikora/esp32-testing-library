#!/usr/bin/env python3
"""Runs the determinism tests twice and compares the two runs (plan §6, the determinism job).

    .venv/bin/python scripts/check-determinism.py [--out DIR] [--runs N] [--load N] [PYTEST_ARG...]
                                                                            (make determinism)

Each run is `python -m pytest` on the PYTEST_ARGs, options included (`tests/test_nmea_reader.py
-k fix`, or `tests/test_determinism.py -n 8` for pytest-xdist's workers),
`tests/test_determinism.py` when there are none, with a `--basetemp` and a `--junitxml` of its own
under DIR (`build/determinism` unless given); after a `--`, the arguments come after the script's
own options. What an earlier check left in DIR is removed first; a DIR that holds anything else,
or is not a directory, is refused, exit 2, so that a mistyped `--out` removes nothing. `--runs N`
makes N runs, 2 or more, each compared with the first; `--load N` keeps N busy loops running beside
them, one process each, which also end once the script is gone, so that the runs share the
machine as under a loaded host (#287). Every run must pass. Each run must then have left, as
the first:

- the same files under its basetemp, byte for byte, except for the `created` field of a
  recording's header, which is when the tap was written. The tests write what the board did into
  their `tmp_path`: its console with the board's times, and its taps. Under xdist each worker has
  a basetemp of its own, `popen-gw0/` and so on, which the comparison leaves out of a file's path;
- for each test, the same `esp32sim_link` property in the JUnit XML: the link's warnings and
  error, the runner's stats (where the run ended, its turns, the bytes each UART moved) and the
  bytes on each route, with the run's basetemp, and the worker's, in a path written as
  `{basetemp}`, and the path of each card the SD card builder made, in a temporary directory of
  a random name, as `{card 1}`, `{card 2}` and so on, in the order the property names them.

Each difference is printed as `✗ <what>`, with a diff where both runs have the file or the test,
and the script exits 1, at the first run that differs; so does a run that fails, before it is
compared, and runs that left no file and no test that used the board, since nothing was compared.
Network transcripts compare exactly, times after a TLS handshake included: the harness's TLS
server draws its randomness from a seed (esp32tl/net/tls.py). Standard library only.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ["tests/test_determinism.py"]
OUT = ROOT / "build" / "determinism"
RUNS = ("run-1", "run-2")
# What a check leaves in its DIR, run-N and run-N.xml, and all that the next one removes there.
OUTPUT = re.compile(r"run-[1-9][0-9]*(\.xml)?")
# The basetemp pytest-xdist gives a worker under the run's (xdist's workermanage.py).
WORKER = re.compile(r"popen-gw[0-9]+")
# An image the SD card builder made: `card.img` in a directory of its own in the slot's
# temporary directory, `esp32tl-sd-*` (esp32tl/interfaces/sd_card.py), both named at random.
BUILT_CARD = re.compile(r"(?:/[^/\s\"]+)*/esp32tl-sd-[^/\s]+/[^/\s]+/card\.img")
# The user property the plugin gives every test that uses `board` (pytest-plugin-user.md).
PROPERTY = "esp32sim_link"
# How many lines of a file's diff are printed.
DIFF_LINES = 40


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the determinism tests twice and compare the two runs.",
        usage="%(prog)s [-h] [--out DIR] [--runs N] [--load N] [PYTEST_ARG...]",
        # --out abbreviated would take a pytest option that starts the same way.
        allow_abbrev=False,
    )
    parser.add_argument(
        "--out", type=Path, default=OUT, metavar="DIR", help=f"default: {OUT.relative_to(ROOT)}"
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=len(RUNS),
        metavar="N",
        help="how many runs, each compared with the first; default: 2",
    )
    parser.add_argument(
        "--load",
        type=int,
        default=0,
        metavar="N",
        help="busy loops kept running beside the runs, one process each; default: 0",
    )
    # Everything argparse does not know is pytest's, its options included, in the order given.
    args, pytest_args = parser.parse_known_args(argv)
    if args.runs < 2:
        print(f"✗ --runs {args.runs}: a check compares 2 runs or more")
        return 2
    if args.load < 0:
        print(f"✗ --load {args.load}: the busy loops are 0 or more")
        return 2
    out = args.out.resolve()
    if out.exists() and not out.is_dir():
        print(f"✗ {out} is not a directory: name an empty or new directory with --out")
        return 2
    if out.exists():
        others = sorted(p.name for p in out.iterdir() if not OUTPUT.fullmatch(p.name))
        if others:
            named = ", ".join(others[:3]) + (f" and {len(others) - 3} more" if others[3:] else "")
            print(
                f"✗ {out} holds {named}, which no determinism check left there:"
                " name an empty or new directory with --out"
            )
            return 2
        for path in out.iterdir():
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
    out.mkdir(parents=True, exist_ok=True)
    # The script's options go before a `--`, after which pytest takes every argument as a path.
    split = pytest_args.index("--") if "--" in pytest_args else len(pytest_args)
    before, after = pytest_args[:split], pytest_args[split:]
    if not before and after[1:] == []:
        before, after = TESTS, []
    runs = [f"run-{n}" for n in range(1, args.runs + 1)]
    loops: list[subprocess.Popen[bytes]] = []
    try:
        busy_loops(args.load, loops)
        for run in runs:
            code = run_pytest(out, run, before, after)
            if code != 0:
                print(f"✗ {run}: pytest exited with code {code}; the runs were not compared")
                return 1
            if run == runs[0]:
                continue
            failures, files, tests = compare(out, runs[0], run)
            for failure in failures:
                print(f"✗ {failure}")
            if failures:
                which = "the two runs differ" if len(runs) == 2 else f"{run} differs from {runs[0]}"
                print(f"determinism: {which} in {count(len(failures), 'place')}")
                return 1
            if files == 0 and tests == 0:
                print(
                    "✗ the runs left no file and no test that used the board: nothing was compared"
                )
                return 1
    finally:
        for loop in loops:
            loop.kill()
            loop.wait()
    which = "the two runs are" if len(runs) == 2 else f"the {len(runs)} runs are"
    print(
        f"determinism: {which} identical: {count(files, 'file')},"
        f" and the link of {count(tests, 'test')} that used the board"
    )
    return 0


def busy_loops(n: int, loops: list[subprocess.Popen[bytes]]) -> None:
    """Starts `n` processes that keep a CPU busy each, into `loops`, for the caller to kill. Each
    also ends once this process is gone, killed by a signal that left the caller no time to."""
    if n:
        print(f"determinism: {count(n, 'busy loop')} beside the runs", flush=True)
    for _ in range(n):
        loops.append(subprocess.Popen([sys.executable, "-c", busy_loop(os.getpid())]))


def busy_loop(parent: int) -> str:
    """The program of a busy loop: it spins, and every million turns, a few milliseconds, looks
    whether `parent` is still its parent."""
    return f"import os\nwhile os.getppid() == {parent}:\n    for _ in range(1_000_000): pass\n"


def run_pytest(out: Path, run: str, before: list[str], after: list[str]) -> int:
    """Runs pytest as `run`, with its basetemp and JUnit XML under `out`; its exit code."""
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
    started = time.monotonic()
    code = subprocess.run(command, cwd=ROOT).returncode
    took = time.monotonic() - started
    load = f", load average {os.getloadavg()[0]:.1f}" if hasattr(os, "getloadavg") else ""
    print(f"determinism: {run} took {took:.1f} s{load}", flush=True)
    return code


def compare(out: Path, first: str, second: str) -> tuple[list[str], int, int]:
    """What differs between the runs `first` and `second` under `out`, and how many files and
    tests that used the board were compared."""
    failures, files = compare_files(out / first, out / second)
    link_failures, tests = compare_links(out, first, second)
    return failures + link_failures, files, tests


def count(n: int, thing: str) -> str:
    return f"{n} {thing}" if n == 1 else f"{n} {thing}s"


def compare_files(first: Path, second: Path) -> tuple[list[str], int]:
    """What differs between the files under `first` and under `second`, and how many there are.
    Symbolic links, the `*current` ones pytest makes in a basetemp, are left out, and so is the
    basetemp of an xdist worker in a file's path."""
    failures: list[str] = []
    ours, theirs = files_under(first, failures), files_under(second, failures)
    failures += [f"{path}: only in {first.name}" for path in sorted(ours.keys() - theirs.keys())]
    failures += [f"{path}: only in {second.name}" for path in sorted(theirs.keys() - ours.keys())]
    for path in sorted(ours.keys() & theirs.keys()):
        a, b = ours[path].read_bytes(), theirs[path].read_bytes()
        if a != b and comparable(a) != comparable(b):
            diffed = diff(comparable(a), comparable(b), path, first.name, second.name)
            failures.append(f"{path} differs:\n{diffed}")
    return failures, len(ours.keys() | theirs.keys())


def files_under(directory: Path, failures: list[str] | None = None) -> dict[str, Path]:
    """The files under `directory`, by their path in it without an xdist worker's basetemp.
    Two workers that left the same path, as two tests whose names start with the same 30
    characters can, go into `failures`: which of them is compared with which is not known."""
    found: dict[str, Path] = {}
    for parent, _, names in os.walk(directory):
        for name in names:
            path = Path(parent, name)
            if path.is_symlink():
                continue
            parts = path.relative_to(directory).parts
            if len(parts) > 1 and WORKER.fullmatch(parts[0]):
                parts = parts[1:]
            key = Path(*parts).as_posix()
            if key in found and failures is not None:
                failures.append(f"{key}: left by two workers in {directory.name}")
            found[key] = path
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


def diff(a: bytes, b: bytes, path: str, first: str = RUNS[0], second: str = RUNS[1]) -> str:
    """The unified diff of two versions of the file `path`, from the runs `first` and `second`,
    cut after DIFF_LINES lines."""
    lines = list(
        difflib.unified_diff(
            a.decode(errors="backslashreplace").splitlines(),
            b.decode(errors="backslashreplace").splitlines(),
            f"{first}/{path}",
            f"{second}/{path}",
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
            failures.append(f"{test}: the link differs:\n{diff(a, b, PROPERTY, first, second)}")
    return failures, sum(link is not None for link in ours.values())


def links(out: Path, run: str) -> dict[str, object]:
    """The `esp32sim_link` property of each test of `run`, by test, `None` for a test without
    one, with the run's basetemp, and an xdist worker's under it, written as `{basetemp}`, and
    each card the test built as `{card 1}`, `{card 2}` and so on."""
    basetemp = re.compile(re.escape(str(out / run)) + r"(?:/popen-gw[0-9]+)?")
    found: dict[str, object] = {}
    for case in ET.parse(out / f"{run}.xml").iter("testcase"):
        value = None
        for prop in case.iter("property"):
            if prop.get("name") == PROPERTY:
                # Decoded first: the plugin escapes what is not ASCII, a path's `ł` as \u0142.
                value = without(json.loads(prop.get("value") or "null"), basetemp, {})
        found[f"{case.get('classname')}::{case.get('name')}"] = value
    return found


def without(value: object, basetemp: re.Pattern[str], cards: dict[str, str]) -> object:
    """`value`, decoded JSON, with what `basetemp` matches written as `{basetemp}` in every string
    in it, and each image the SD card builder made, in a temporary directory of a random name, as
    `{card N}`, numbered in the order `cards`, theirs so far, first meets them."""
    if isinstance(value, str):

        def card(match: re.Match[str]) -> str:
            return cards.setdefault(match[0], f"{{card {len(cards) + 1}}}")

        return BUILT_CARD.sub(card, basetemp.sub("{basetemp}", value))
    if isinstance(value, list):
        return [without(item, basetemp, cards) for item in value]
    if isinstance(value, dict):
        return {key: without(item, basetemp, cards) for key, item in value.items()}
    return value


if __name__ == "__main__":
    sys.exit(main())
