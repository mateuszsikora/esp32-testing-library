"""Resets the board through RTS (EN) and records its console for SECONDS into OUT.

Usage: .venv/bin/python -I tools/casic_probe/capture.py PORT OUT [SECONDS]"""

import sys
import time

import serial

port, out = sys.argv[1], sys.argv[2]
seconds = float(sys.argv[3]) if len(sys.argv) > 3 else 150
with serial.Serial(port, 115200, timeout=0.2) as s, open(out, "w") as f:
    s.dtr = False
    s.rts = True  # EN low
    time.sleep(0.1)
    s.rts = False
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        line = s.readline()
        if not line:
            continue
        text = line.decode(errors="replace").rstrip("\r\n")
        f.write(text + "\n")
        f.flush()
        if not text.startswith("NMEA "):
            print(text, flush=True)
        if "casic_probe: done" in text:
            break
