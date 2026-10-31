#!/usr/bin/env python3
"""Writes the generated GNSS recordings in recordings/ (plan M1), each from a fixed seed, so that
running it again gives the same bytes.

    python3 scripts/generate-gnss-recordings.py           write them
    python3 scripts/generate-gnss-recordings.py --check   exit 1 if a committed one differs

A recording is a stationary receiver with a fix from the first sentence on: one sentence per
emulated second, GGA and RMC in turn, the first at 1 s, the position wandering a few metres
around its start as the seed decides. Every sentence fits in its second at 9600 baud with room to
spare. The format is the recording format of plan §4.3: a header line, then one event per line.
Needs Python 3.9 or newer and the standard library only.
"""

import argparse
import base64
import json
import random
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = "scripts/generate-gnss-recordings.py"

BAUD = 9600
FIRST_NS = 1_000_000_000  # after nmea_reader has configured its UART
PERIOD_NS = 1_000_000_000
SENTENCES = 60
# timezone.utc rather than datetime.UTC, so that the macOS system python3 (3.9) runs it too
START = datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc)  # noqa: UP017
# Header `created`: when these parameters were fixed, not when the script ran, so the output
# depends on nothing but this file.
CREATED = "2026-10-31T00:00:00Z"
STEP = 20  # the largest move per second, in 1e-4 minutes of arc: under 4 m


@dataclass(frozen=True)
class Recording:
    name: str
    seed: int
    # the start position, in 1e-4 minutes of arc (NMEA's own resolution), north and east positive
    lat: int
    lon: int


RECORDINGS = [
    # Warsaw, 52.229677 N 21.012233 E
    Recording(
        "gnss-generated-1.jsonl", seed=1, lat=52 * 600_000 + 137_806, lon=21 * 600_000 + 7_340
    ),
    # Buenos Aires, 34.603700 S 58.381600 W: the other two hemispheres
    Recording(
        "gnss-generated-2.jsonl",
        seed=2,
        lat=-(34 * 600_000 + 362_220),
        lon=-(58 * 600_000 + 228_960),
    ),
]


def checksum(body: str) -> str:
    value = 0
    for char in body.encode("ascii"):
        value ^= char
    return f"{value:02X}"


def sentence(body: str) -> bytes:
    return f"${body}*{checksum(body)}\r\n".encode("ascii")


def angle(e4: int, degree_digits: int, positive: str, negative: str) -> str:
    """ddmm.mmmm or dddmm.mmmm, then its hemisphere letter, from 1e-4 minutes of arc."""
    magnitude = abs(e4)
    degrees, minutes = divmod(magnitude, 600_000)
    return (
        f"{degrees:0{degree_digits}d}{minutes // 10_000:02d}.{minutes % 10_000:04d},"
        f"{positive if e4 >= 0 else negative}"
    )


def epoch(when: datetime, lat: int, lon: int, rmc: bool) -> bytes:
    time = when.strftime("%H%M%S") + ".00"
    position = f"{angle(lat, 2, 'N', 'S')},{angle(lon, 3, 'E', 'W')}"
    if rmc:
        return sentence(f"GPRMC,{time},A,{position},0.0,,{when.strftime('%d%m%y')},,,A")
    return sentence(f"GPGGA,{time},{position},1,08,0.9,110.0,M,34.0,M,,")


def generate(recording: Recording) -> str:
    header = {
        "esp32tl_recording": 1,
        "board": "esp32-devkitc",
        "interface": "uart.gnss",
        "source": "generated",
        "clock": "emulated-fifo-write",
        "end": "hold",
        "app_sha256": None,
        "tools": {},
        "created": CREATED,
        "generator": {"script": SCRIPT, "seed": recording.seed, "baud": BAUD},
    }
    lines = [json.dumps(header)]
    rng = random.Random(recording.seed)
    lat, lon = recording.lat, recording.lon
    for i in range(SENTENCES):
        if i > 0:
            lat += rng.randint(-STEP, STEP)
            lon += rng.randint(-STEP, STEP)
        data = epoch(START + timedelta(seconds=i), lat, lon, rmc=i % 2 == 1)
        # 10 bit times a byte (8N1): the sentence is on the wire well before the next one
        assert len(data) * 10 * 1_000_000_000 // BAUD < PERIOD_NS // 2
        event = {
            "t": FIRST_NS + i * PERIOD_NS,
            "dir": "in",
            "data": base64.b64encode(data).decode(),
        }
        lines.append(json.dumps(event))
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check", action="store_true", help="exit 1 if a committed recording differs"
    )
    args = parser.parse_args()
    stale = []
    for recording in RECORDINGS:
        path = ROOT / "recordings" / recording.name
        text = generate(recording)
        if args.check:
            if not path.is_file() or path.read_text() != text:
                stale.append(path.relative_to(ROOT))
        else:
            path.write_text(text)
            print(f"{SCRIPT}: wrote {path.relative_to(ROOT)}")
    for path in stale:
        print(
            f"{SCRIPT}: {path} is not what the script generates; run python3 {SCRIPT}",
            file=sys.stderr,
        )
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
