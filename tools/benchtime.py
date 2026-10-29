"""Time tools/cryptobench's phases in host wall time, and compare runs.

    python3 tools/benchtime.py run OUT esp32sim --chip esp32 ... --max-seconds 60
    python3 tools/benchtime.py summary OUT [OUT ...]
    python3 tools/benchtime.py compare OUT_A OUT_B

`run` executes an esp32sim command line, stamps every console line with the host's monotonic clock
and writes the stamped console to OUT. `summary` reads one or more OUT files and prints, per phase,
the emulated time per operation, the median and range of emulated time over wall time, and whether
every run printed the same result hash. `compare` checks that two runs, typically the builds with and
without the accelerators, printed the same result hash for every phase; it exits 1 if they did not.
"""

import re
import statistics
import subprocess
import sys
import time

END = re.compile(r"BENCH end (\S+): (\d+) x ([\d.]+) us, (\d+) cycles total")
RESULT = re.compile(r"BENCH result (\S+) (\w+)")


def run(out, cmd):
    start = time.monotonic()
    lines = []
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1
    )
    for line in proc.stdout:
        lines.append((time.monotonic() - start, line.rstrip("\n")))
    proc.wait()
    with open(out, "w") as f:
        for stamp, line in lines:
            f.write(f"{stamp:9.3f} {line}\n")
    stop = [line for _, line in lines if "[emu] stop" in line]
    print(stop[-1].split("stop: ")[-1] if stop else "no stop line", f"| host wall {time.monotonic() - start:.2f} s")
    return proc.returncode


def parse(path):
    """Phases of one stamped console: name -> (emulated s, wall s, per-op us, result hash)."""
    phases, begins, results = {}, {}, {}
    for line in open(path, errors="replace"):
        stamp, _, text = line.lstrip().partition(" ")
        stamp = float(stamp)
        if m := re.search(r"BENCH begin (\S+)", text):
            begins[m.group(1)] = stamp
        elif m := END.search(text):
            name, n, per_op = m.group(1), int(m.group(2)), float(m.group(3))
            phases[name] = (n * per_op / 1e6, stamp - begins[name], per_op)
        elif m := RESULT.search(text):
            results[m.group(1)] = m.group(2)
        if "BENCH FAIL" in text:
            sys.exit(f"{path}: {text.strip()}")
    return {name: (*timing, results.get(name, "-")) for name, timing in phases.items()}


def summary(paths):
    runs = [parse(p) for p in paths]
    print(f"{'phase':36s} {'per op':>12s} {'emulated / wall':>22s}  results")
    for name in runs[0]:
        ratios = [r[name][0] / r[name][1] for r in runs if name in r and r[name][1] > 0]
        per_op = statistics.median(r[name][2] for r in runs if name in r)
        hashes = {r[name][3] for r in runs if name in r}
        agree = "same" if len(hashes) == 1 else "DIFFER " + ",".join(sorted(hashes))
        spread = f"{statistics.median(ratios):.2f} ({min(ratios):.2f}-{max(ratios):.2f})"
        print(f"{name:36s} {per_op:10.1f}us {spread:>22s}  {agree}")


def compare(a, b):
    ra, rb = parse(a), parse(b)
    bad = [n for n in ra if ra[n][3] != rb.get(n, (None,) * 4)[3]]
    for name in ra:
        print(f"{name:36s} {ra[name][3]:>18s} {rb.get(name, (None,) * 4)[3] or 'missing':>18s}")
    print("all results agree" if not bad else "results differ: " + ", ".join(bad))
    return 1 if bad else 0


if __name__ == "__main__":
    mode, args = sys.argv[1], sys.argv[2:]
    if mode == "run":
        sys.exit(run(args[0], args[1:]))
    if mode == "summary":
        summary(args)
    elif mode == "compare":
        sys.exit(compare(*args))
    else:
        sys.exit(__doc__)
