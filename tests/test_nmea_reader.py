"""nmea_reader, unmodified, fed the generated GNSS recordings through the `gnss` fixture: the first
milestone of the specification (plan M1). A replayed NMEA recording reaches the app through UART1
in emulated time, and the test asserts on what it prints. The expected console lines are worked
out from the recordings' sentences here, independently of the firmware's parser.

test_gnss_fixture.py runs test_reports_a_fix against a changed recording, and against a profile
whose GNSS route runs at another baud than the firmware's, and checks that it fails on both.
"""

import re
import time
from pathlib import Path

import pytest

from esp32tl import recording
from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.interfaces.console import Console
from esp32tl.interfaces.uart import Uart

RECORDINGS = Path(__file__).resolve().parent.parent / "recordings"

pytestmark = [
    pytest.mark.app_path("../firmware/nmea_reader"),
    pytest.mark.source("uart.gnss", "replay"),
]

# A fix line as nmea_reader prints it: `fix acquired` on the first, `fix` on every later one.
# Up to its end only: the console comes in pieces, and without the lookahead a line cut short
# would match.
FIX = re.compile(rb"fix(?: acquired)? lat=\S+ lon=\S+(?=\r?\n)")

# How many wall seconds ten emulated seconds of fixes may take: the emulator runs 15 to 37 times
# faster than real time, but every recorded sentence is an exchange with the harness, a wall-time
# block, so the margin is deliberate (plan §7).
TEN_FIXES_WALL_SECONDS = 5


def test_reports_a_fix(gnss: Uart, console: Console) -> None:
    gnss.replay("../recordings/gnss-generated-1.jsonl")
    console.expect_exact("fix acquired lat=52.229677 lon=21.012233", timeout=30)


def test_fix_follows_the_recording(board: Esp32Sim, gnss: Uart, console: Console) -> None:
    expected = expected_fixes("gnss-generated-2.jsonl")
    assert expected[0] == "fix acquired lat=-34.603700 lon=-58.381600"
    gnss.replay("../recordings/gnss-generated-2.jsonl")
    # The route the board profile gives, configured by the firmware itself.
    console.expect_exact("nmea_reader: UART1 RX GPIO5 TX GPIO4 9600 baud")
    # Each the next fix line the firmware printed: none in between.
    assert [next_fix(console) for _ in expected[:5]] == expected[:5]
    # The first sentence comes at 1 s, long after the firmware configured UART1: no byte dropped.
    assert board.link.warnings == []


def test_time_is_emulated(gnss: Uart, console: Console) -> None:
    # The run is held before the guest boots until the replay is given.
    started = time.monotonic()
    gnss.replay("../recordings/gnss-generated-1.jsonl")
    # One sentence per emulated second from 1 s: the tenth fix is at 10 emulated seconds.
    fixes = [next_fix(console) for _ in range(10)]
    elapsed = time.monotonic() - started
    assert fixes == expected_fixes("gnss-generated-1.jsonl")[:10]
    assert elapsed < TEN_FIXES_WALL_SECONDS, f"ten fixes took {elapsed:.2f} wall seconds"


def test_only_well_formed_fixes_count_and_an_epoch_prints_once(
    tmp_path: Path, gnss: Uart, console: Console
) -> None:
    gga = "GPGGA,120000.00,5213.7806,N,02100.7340,E,1,08,0.9,110.0,M,34.0,M,,"
    feed = [
        nmea(gga)[:-5] + b"00\r\n",  # a wrong checksum
        nmea(gga.replace(",E,1,", ",E,0,")),  # GGA, fix quality 0
        nmea("GPRMC,120000.00,V,5213.7806,N,02100.7340,E,0.0,,291026,,,N"),  # RMC, status V
        nmea("GPGGA,120000.00,,,,,1,08,0.9,110.0,M,34.0,M,,"),  # no position
        nmea("GPGSV,1,1,00"),  # neither GGA nor RMC
        nmea("GNGGA,120001.00,5213.78,N,02100.123456,E,1,12,0.6,110.0,M,34.0,M,,"),
        nmea("GNRMC,120001.00,A,0000.0000,N,00000.0000,E,0.0,,291026,,,A"),  # the same epoch
        # a '$' starts the sentence over
        b"\x00garbage$GPRMC" + nmea("GPRMC,120002.00,A,3436.2220,S,05822.8960,W,0.0,,291026,,,A"),
        # longer than NMEA's 82 characters
        nmea("GPGGA,120003.00,5213.7806,N,02100.7340,E,1,08,0.9,110.0,M,34.0,M," + "0" * 40),
        nmea("GPRMC,120004.00,A,9000.0000,N,18000.0000,W,0.0,,291026,,,A"),
    ]
    header = recording.Header(
        "esp32-devkitc", "uart.gnss", "generated", recording.EMULATED, "2026-10-31T00:00:00Z"
    )
    events = [
        recording.Event(1_000_000_000 + i * 200_000_000, "in", data, recording.EMULATED)
        for i, data in enumerate(feed)
    ]
    recording.write(tmp_path / "feed.jsonl", recording.Recording(header, tuple(events)))
    gnss.replay(tmp_path / "feed.jsonl")
    # The last sentence's fix is the third: nothing else printed a fix line in between.
    assert [next_fix(console) for _ in range(3)] == [
        # 13.78 minutes is 0.229666... degrees; 0.123456 minutes is 0.0020576 degrees
        "fix acquired lat=52.229667 lon=21.002058",
        "fix lat=-34.603700 lon=-58.381600",
        "fix lat=90.000000 lon=-180.000000",
    ]


def next_fix(console: Console) -> str:
    """The next fix line on the console."""
    match: re.Match[bytes] = console.expect(FIX)
    return match.group().decode()


def degrees(field: str, hemisphere: str, degree_digits: int) -> str:
    """An NMEA ddmm.mmmm or dddmm.mmmm field as nmea_reader prints it: decimal degrees, six
    decimals, rounded half up, south and west negative."""
    whole, _, decimals = field.partition(".")
    minutes_e6 = int(whole[degree_digits:] + (decimals + "000000")[:6])
    e6 = int(whole[:degree_digits]) * 1_000_000 + (minutes_e6 + 30) // 60
    assert hemisphere in ("N", "S", "E", "W"), hemisphere
    if hemisphere in ("S", "W"):
        e6 = -e6
    # the sign of the rounded value: a south latitude that rounds to 0 prints 0.000000
    sign, magnitude = ("-", -e6) if e6 < 0 else ("", e6)
    return f"{sign}{magnitude // 1_000_000}.{magnitude % 1_000_000:06d}"


def expected_fixes(name: str) -> list[str]:
    """The fix lines the recording `name` in recordings/ should print, one per sentence."""
    lines: list[str] = []
    for event in recording.read(RECORDINGS / name).events:
        fields = event.data.decode("ascii").split(",")
        # GGA: time, lat, N/S, lon, E/W; RMC: time, status, lat, N/S, lon, E/W
        lat = 2 if fields[0] == "$GPGGA" else 3
        position = (
            f"lat={degrees(fields[lat], fields[lat + 1], 2)}"
            f" lon={degrees(fields[lat + 2], fields[lat + 3], 3)}"
        )
        lines.append(f"{'fix' if lines else 'fix acquired'} {position}")
    return lines


def nmea(body: str) -> bytes:
    check = 0
    for char in body.encode("ascii"):
        check ^= char
    return f"${body}*{check:02X}\r\n".encode("ascii")
