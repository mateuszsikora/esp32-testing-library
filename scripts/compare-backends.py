#!/usr/bin/env python3
"""Runs the board tests on both backends and lists which ran on both and whether they agreed
(plan M4, from the specification: "Every test that a real board can run, runs on one too, and
the two results are compared").

    .venv/bin/python scripts/compare-backends.py --port PORT [--out DIR] [PYTEST_ARG...]

Each run is `python -m pytest` on the PYTEST_ARGs, options included, `tests/test_hello_world.py
tests/test_nmea_reader.py` when there are none, first with `--esp32tl-backend esp32sim`, then
with `--esp32tl-backend serial --port PORT`, each with a `--junitxml` of its own under DIR
(`build/backends` unless given); after a `--`, the arguments come after the script's own options.
A run whose pytest exits with a code other than 0 or 1 (interrupted, an internal or usage error,
no test collected) ends the script, exit 1, before anything is compared; tests that fail do not.

A test that uses `board` has the backend in its id (`test_hello_world[esp32sim]`); its two cases
are matched without it. A test whose id is the same in both runs does not use `board` and is left
out. Each test that uses `board` gets one line:

- `✓ <test>: passed on both`;
- `✗ <test>: failed on both: on esp32sim: <message>; on serial: <message>`;
- `✗ <test>: passed on esp32sim, failed on serial: <message>`, and the other way round;
- `- <test>: esp32sim only (passed): skipped on serial: <reason>`, `✗` when it failed there;
- `✗ <test>: serial only (passed): skipped on esp32sim: <reason>`: its results cannot be compared;
- `- <test>: skipped on both: <reason>`;
- `✗ <test>: collected on esp32sim only`, and the other way round.

An error at setup or teardown counts as a failure. The script exits 1 when a line has `✗`, or
when no test ran on both backends, since nothing was compared; 0 otherwise. Standard library only.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# The hardware smoke tests of plan M4 (serial-backend-user.md).
TESTS = ["tests/test_hello_world.py", "tests/test_nmea_reader.py"]
OUT = ROOT / "build" / "backends"
BACKENDS = ("esp32sim", "serial")
# pytest's exit codes for a run whose tests all passed, and for one where some failed.
COMPARABLE = (0, 1)
# How much of a failure's message a line shows.
MESSAGE_CHARS = 200


@dataclass(frozen=True)
class Outcome:
    """How one case of a test ended: `passed`, `failed` or `skipped`, and the message pytest gave
    a failure or a skip."""

    status: str
    message: str = ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the board tests on esp32sim and on serial and compare their outcomes.",
        usage="%(prog)s [-h] --port PORT [--out DIR] [PYTEST_ARG...]",
        # --out abbreviated would take a pytest option that starts the same way.
        allow_abbrev=False,
    )
    parser.add_argument(
        "--port", required=True, help="the real board's serial port, pytest-embedded's --port"
    )
    parser.add_argument(
        "--out", type=Path, default=OUT, metavar="DIR", help=f"default: {OUT.relative_to(ROOT)}"
    )
    # Everything argparse does not know is pytest's, its options included, in the order given.
    args, pytest_args = parser.parse_known_args(argv)
    out = args.out.resolve()
    if out.exists() and not out.is_dir():
        print(f"✗ {out} is not a directory: name a directory with --out")
        return 2
    out.mkdir(parents=True, exist_ok=True)
    # The script's options go before a `--`, after which pytest takes every argument as a path.
    split = pytest_args.index("--") if "--" in pytest_args else len(pytest_args)
    before, after = pytest_args[:split], pytest_args[split:]
    if not before and after[1:] == []:
        before, after = TESTS, []
    for backend in BACKENDS:
        report = out / f"{backend}.xml"
        # A run that ends before it writes its report must not leave an earlier one to compare.
        report.unlink(missing_ok=True)
        command = [
            sys.executable,
            "-m",
            "pytest",
            *before,
            f"--esp32tl-backend={backend}",
            *([f"--port={args.port}"] if backend == "serial" else []),
            f"--junitxml={report}",
            # pytest-embedded's app_path fixture records an XML attribute, on which xunit2 warns:
            # "record_xml_attribute is incompatible with junit_family 'xunit2' (use 'legacy' or
            # 'xunit1')".
            "-o",
            "junit_family=xunit1",
            *after,
        ]
        print(f"backends: {backend}: {' '.join(command[1:])}", flush=True)
        code = subprocess.run(command, cwd=ROOT).returncode
        if code not in COMPARABLE or not report.is_file():
            print(f"✗ {backend}: pytest exited with code {code}; the backends were not compared")
            return 1
    lines, counts = compare(*(outcomes(out / f"{backend}.xml") for backend in BACKENDS))
    for line in lines:
        print(line)
    print(f"backends: {summary(counts)}")
    if counts["both"] == 0:
        print("✗ no test ran on both backends: nothing was compared")
        return 1
    return 1 if any(line.startswith("✗") for line in lines) else 0


def outcomes(report: Path) -> dict[str, Outcome]:
    """How each test case in the JUnit XML `report` ended, by its id: `classname::name`."""
    found: dict[str, Outcome] = {}
    for case in ET.parse(report).iter("testcase"):
        found[f"{case.get('classname')}::{case.get('name')}"] = outcome(case)
    return found


def outcome(case: ET.Element) -> Outcome:
    """A failure or an error wins over a skip: a test that skipped in its body and then failed at
    teardown has both."""
    for tag in ("failure", "error"):
        element = case.find(tag)
        if element is not None:
            return Outcome("failed", element.get("message") or "")
    skipped = case.find("skipped")
    if skipped is not None:
        return Outcome("skipped", skipped.get("message") or "")
    return Outcome("passed")


def without_backend(test: str, backend: str) -> str | None:
    """`test`'s id without `backend` among its parameters, or None when it has no such parameter.
    pytest joins the ids of a case's parameters with `-`; esp32tl's is the backend's name."""
    head, bracket, params = test.partition("[")
    if not bracket or not params.endswith("]"):
        return None
    parts = params[:-1].split("-")
    if backend not in parts:
        return None
    parts.remove(backend)
    return f"{head}[{'-'.join(parts)}]" if parts else head


def compare(sim: dict[str, Outcome], real: dict[str, Outcome]) -> tuple[list[str], dict[str, int]]:
    """A line for each test that uses `board`, in the order of the esp32sim run, then those only
    the serial run collected; and how many tests fell under each heading of `summary`."""
    # The same id in both runs is a test without `board`: esp32tl gives the two runs' cases ids
    # of their own backend.
    shared = sim.keys() & real.keys()
    left_out = len(shared)
    by_test: dict[str, dict[str, Outcome]] = {}
    for backend, run in zip(BACKENDS, (sim, real), strict=True):
        for test, result in run.items():
            name = without_backend(test, backend) if test not in shared else None
            if name is None:
                left_out += test not in shared
                continue
            by_test.setdefault(name, {})[backend] = result
    counts = dict.fromkeys(("both", "agreed", "esp32sim", "serial", "neither", "left out"), 0)
    counts["left out"] = left_out
    lines = []
    for test, results in by_test.items():
        line, heading = describe(test, results.get(BACKENDS[0]), results.get(BACKENDS[1]))
        lines.append(line)
        for name in heading:
            counts[name] += 1
    return lines, counts


def describe(test: str, sim: Outcome | None, real: Outcome | None) -> tuple[str, list[str]]:
    """The line of one test, and the headings of `summary` it counts under."""
    first, second = BACKENDS
    if sim is None or real is None:
        only = first if real is None else second
        return f"✗ {test}: collected on {only} only", []
    if sim.status == "skipped" and real.status == "skipped":
        return f"- {test}: skipped on both: {short(sim.message)}", ["neither"]
    if sim.status == "skipped" or real.status == "skipped":
        ran, skipped, result, skip = (
            (second, first, real, sim) if sim.status == "skipped" else (first, second, sim, real)
        )
        # A test only the real board ran has no result of the emulator's to compare with.
        mark = "✗" if result.status == "failed" or ran == second else "-"
        why = f": {short(result.message)}" if result.status == "failed" else ""
        return (
            f"{mark} {test}: {ran} only ({result.status}{why}): skipped on {skipped}:"
            f" {short(skip.message)}",
            [ran],
        )
    if sim.status == real.status == "passed":
        return f"✓ {test}: passed on both", ["both", "agreed"]
    if sim.status == real.status:
        return (
            f"✗ {test}: failed on both: on {first}: {short(sim.message)};"
            f" on {second}: {short(real.message)}",
            ["both", "agreed"],
        )
    failed, message = (second, real.message) if sim.status == "passed" else (first, sim.message)
    passed = first if failed == second else second
    return f"✗ {test}: passed on {passed}, failed on {failed}: {short(message)}", ["both"]


def short(message: str) -> str:
    """The first line of `message`, cut after MESSAGE_CHARS characters."""
    line = message.strip().splitlines()[0] if message.strip() else "(no message)"
    return line if len(line) <= MESSAGE_CHARS else f"{line[:MESSAGE_CHARS]}..."


def summary(counts: dict[str, int]) -> str:
    both, agreed = counts["both"], counts["agreed"]
    return (
        f"{count(both, 'test')} ran on both backends, {agreed} agreed and {both - agreed} did not;"
        f" {counts['esp32sim']} ran on esp32sim only, {counts['serial']} on serial only,"
        f" {counts['neither']} on neither; {count(counts['left out'], 'test')} without `board`"
        " left out"
    )


def count(n: int, thing: str) -> str:
    return f"{n} {thing}" if n == 1 else f"{n} {thing}s"


if __name__ == "__main__":
    sys.exit(main())
