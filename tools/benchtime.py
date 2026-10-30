"""Time tools/cryptobench's phases in host wall time, and compare runs.

    python3 tools/benchtime.py run OUT esp32sim --chip esp32 ... --max-seconds 60
    python3 tools/benchtime.py summary OUT [OUT ...]
    python3 tools/benchtime.py compare OUT_A OUT_B

`run` executes an esp32sim command line, stamps every console line with the host's monotonic clock
and writes the stamped console to OUT. It exits with esp32sim's status, which is 0 for a run that
stopped at `--max-seconds`; whether the benchmark finished is for `summary` and `compare` to check.

`summary` reads one or more OUT files of the same build and prints, per phase, the emulated time
per operation and emulated time over wall time: the best (largest) ratio first, then the median and
the range. Load on the host can only lengthen a phase's wall time, never shorten it, so the best
ratio is the one least disturbed by other work. Runs of one build print identical emulated times, so
it refuses files whose phases or times per operation differ: those are different builds.

`compare` checks that two runs, typically the builds with and without the accelerators, ran the same
phases and printed the same result hash for each; it exits 1 if they did not.

Every command refuses a run that did not reach `BENCH done`, or that printed `BENCH FAIL`.
"""

import re
import statistics
import subprocess
import sys
import time

BEGIN = re.compile(r"BENCH begin (\S+)")
END = re.compile(r"BENCH end (\S+): (\d+) x ([\d.]+) us, (\d+) cycles total")
RESULT = re.compile(r"BENCH result (\S+) (\w+)")
NO_RESULT = "-"


def run(out, cmd):
    start = time.monotonic()
    lines = []
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        bufsize=1,
    )
    for line in proc.stdout:
        lines.append((time.monotonic() - start, line.rstrip("\n")))
    proc.wait()
    with open(out, "w") as f:
        for stamp, line in lines:
            f.write(f"{stamp:9.3f} {line}\n")
    stop = [line for _, line in lines if "[emu] stop" in line]
    wall = time.monotonic() - start
    print(stop[-1].split("stop: ")[-1] if stop else "no stop line", f"| host wall {wall:.2f} s")
    return proc.returncode


def parse(path):
    """The phases of one stamped console, in order.

    name -> (emulated s, wall s, per-op us, hash)
    """
    phases, begins, results, done = {}, {}, {}, False
    with open(path, errors="replace") as f:
        for line in f:
            stamp, _, text = line.lstrip().partition(" ")
            try:
                stamp = float(stamp)
            except ValueError:
                continue
            if "BENCH FAIL" in text:
                sys.exit(f"{path}: {text.strip()}")
            if "BENCH done" in text:
                done = True
            elif m := BEGIN.search(text):
                begins[m.group(1)] = stamp
            elif m := END.search(text):
                name, n, per_op = m.group(1), int(m.group(2)), float(m.group(3))
                if name not in begins:
                    sys.exit(f"{path}: phase {name} ended without beginning")
                phases[name] = (n * per_op / 1e6, stamp - begins[name], per_op)
            elif m := RESULT.search(text):
                results[m.group(1)] = m.group(2)
    if not done:
        sys.exit(f"{path}: incomplete run, no 'BENCH done' line (raise --max-seconds?)")
    return {name: (*timing, results.get(name, NO_RESULT)) for name, timing in phases.items()}


def summary(paths):
    runs = [parse(p) for p in paths]
    names = list(runs[0])
    for path, r in zip(paths, runs, strict=True):
        if list(r) != names:
            sys.exit(f"{path}: its phases differ from {paths[0]}'s; summarise one build at a time")
        for name in names:
            if r[name][2] != runs[0][name][2]:
                sys.exit(
                    f"{path}: {name} took {r[name][2]} us per op, {paths[0]} {runs[0][name][2]} us;"
                    " summarise one build at a time"
                )
    print(f"{'phase':36s} {'per op':>12s} {'best':>6s} {'median':>7s} {'range':>11s}  results")
    for name in names:
        ratios = [r[name][0] / r[name][1] for r in runs if r[name][1] > 0]
        per_op = runs[0][name][2]
        hashes = {r[name][3] for r in runs}
        agree = "same" if len(hashes) == 1 else "DIFFER " + ",".join(sorted(hashes))
        if ratios:
            best, median, worst = max(ratios), statistics.median(ratios), min(ratios)
            ratio = f"{best:6.2f} {median:7.2f} {worst:5.2f}-{best:.2f}"
        else:
            ratio = f"{'-':>6s} {'-':>7s} {'-':>11s}"
        print(f"{name:36s} {per_op:10.1f}us {ratio}  {agree}")


def compare(a, b):
    ra, rb = parse(a), parse(b)
    bad = 0
    for name in list(ra) + [n for n in rb if n not in ra]:
        ha, hb = (r[name][3] if name in r else "missing" for r in (ra, rb))
        bad += ha != hb
        print(f"{name:36s} {ha:>18s} {hb:>18s}{'' if ha == hb else '  DIFFER'}")
    print(f"phases differing or missing: {bad}" if bad else "all results agree")
    return 1 if bad else 0


def main(argv):
    if len(argv) < 2 or argv[1] not in ("run", "summary", "compare"):
        sys.exit(__doc__)
    mode, args = argv[1], argv[2:]
    if mode == "run" and len(args) >= 2:
        return run(args[0], args[1:])
    if mode == "summary" and args:
        summary(args)
        return 0
    if mode == "compare" and len(args) == 2:
        return compare(*args)
    sys.exit(__doc__)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
