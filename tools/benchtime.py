"""Time tools/cryptobench's phases in host wall time.

Runs an esp32sim command line, stamps every console line with the host's monotonic clock, and for
each `BENCH begin` / `BENCH end` pair prints the phase's emulated time (from the guest), its wall
time (from the stamps), their ratio and the result hash. The stamped console goes to OUT.

    python3 tools/benchtime.py OUT esp32sim --chip esp32 ... --max-seconds 40
"""
import re, subprocess, sys, time, json
out, cmd = sys.argv[1], sys.argv[2:]
t0 = time.monotonic(); lines = []
p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1)
for line in p.stdout:
    lines.append((time.monotonic() - t0, line.rstrip("\n")))
p.wait(); wall = time.monotonic() - t0
with open(out, "w") as f:
    for t, l in lines: f.write(f"{t:9.3f} {l}\n")
begins = {}
for t, l in lines:
    m = re.search(r"BENCH begin (\S+)", l)
    if m: begins[m.group(1)] = t
    m = re.search(r"BENCH end (\S+): (\d+) x ([\d.]+) us, (\d+) cycles total, result=(\w+)", l)
    if m:
        name, n, us, cyc, res = m.group(1), int(m.group(2)), float(m.group(3)), int(m.group(4)), m.group(5)
        w = t - begins[name]; emu = n * us / 1e6
        print(f"  {name:30s} emulated {emu*1000:10.1f} ms  wall {w*1000:9.1f} ms  emu/wall {emu/w if w else float('inf'):7.2f}  per-op {us:10.1f} us  cycles {cyc}  result={res}")
for t, l in lines:
    if "BENCH FAIL" in l or "BENCH done" in l or "BENCH start" in l: print("  " + l.strip())
stop = [l for _, l in lines if "[emu] stop" in l]
print("  " + (stop[-1].split("stop: ")[-1] if stop else "NO STOP LINE"), f"| host wall {wall:.2f}s")
