"""nmea_reader in the runner, fed a generated recording through `--board socket:PATH` by a minimal
peer written here, which sends every sentence in the first turn and then lets the run go: the
firmware and the recordings checked together before the `gnss` fixture exists (#14). The expected
console lines are worked out from the recording's sentences here, independently of the
firmware's parser."""

import base64
import json
import shutil
import socket
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from esp32tl import boards
from esp32tl.backends import esp32sim

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "firmware" / "nmea_reader" / "build"


@pytest.fixture
def socket_path() -> Iterator[Path]:
    # short: sun_path holds 104 bytes on macOS, and pytest's tmp_path can be longer
    directory = Path(tempfile.mkdtemp(prefix="e32tl-", dir="/tmp"))
    yield directory / "board.sock"
    shutil.rmtree(directory)


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


def expected_fixes(events: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for event in events:
        fields = base64.b64decode(event["data"]).decode("ascii").split(",")
        # GGA: time, lat, N/S, lon, E/W; RMC: time, status, lat, N/S, lon, E/W
        lat = 2 if fields[0] == "$GPGGA" else 3
        position = (
            f"lat={degrees(fields[lat], fields[lat + 1], 2)}"
            f" lon={degrees(fields[lat + 2], fields[lat + 3], 3)}"
        )
        lines.append(f"{'fix' if lines else 'fix acquired'} {position}")
    return lines


def accept(server: socket.socket, process: subprocess.Popen[bytes]) -> socket.socket:
    """The runner's connection, or a failure as soon as the runner exits without one (a bad
    image or ROM ends it before it connects)."""
    server.settimeout(0.1)
    for _ in range(300):
        try:
            connection, _ = server.accept()
        except TimeoutError:
            if process.poll() is not None:
                raise AssertionError(
                    f"the runner exited with {process.returncode} before connecting"
                ) from None
            continue
        connection.settimeout(30)
        return connection
    raise AssertionError("the runner did not connect in 30 s")


# The route nmea_reader's sdkconfig defaults to: the GNSS route of the board profile.
PROFILE = boards.profile("esp32-devkitc")
GNSS = PROFILE.uart["gnss"]


def run(path: Path, events: list[dict[str, Any]], max_seconds: float) -> tuple[list[Any], str]:
    """Runs nmea_reader with the events sent into the GNSS route's RX pin at its baud. Returns
    the runner's messages after the first turn, and the console. On any failure the runner is
    killed and its stderr added to the error."""
    if not (BUILD / "esp32tl-built").is_file():
        pytest.fail(f"{BUILD} holds no finished build: run make firmware", pytrace=False)
    app = esp32sim.load_app(str(BUILD.parent))
    runner, rom = esp32sim.runner_path(), esp32sim.rom_path()
    command = esp32sim.runner_command(runner, rom, app, max_seconds, path)
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(path))
        server.listen(1)
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            with accept(server, process) as connection, connection.makefile("rwb") as wire:

                def write(**message: Any) -> None:
                    wire.write(json.dumps(message).encode() + b"\n")
                    wire.flush()

                assert json.loads(wire.readline())["type"] == "hello"
                write(
                    type="hello",
                    version=1,
                    chip=PROFILE.chip,
                    board=PROFILE.name,
                    watch=[GNSS.tx, GNSS.rx],
                )
                assert json.loads(wire.readline()) == {"type": "sync", "t": 0}
                for event in events:
                    write(
                        type="uart_rx",
                        t=event["t"],
                        pin=GNSS.rx,
                        baud=GNSS.baud,
                        data=event["data"],
                    )
                # nothing scheduled after these and no answers: the run goes to its end in one turn
                write(type="wait", ack=0, quiet_until=None, latency=None)
                messages = [json.loads(line) for line in wire.read().splitlines()]
            out, err = process.communicate(timeout=60)
            # the except below adds the runner's stderr
            assert process.returncode == 0
        except BaseException as error:
            process.kill()
            _, err = process.communicate()
            error.add_note(f"runner stderr:\n{err.decode()}")
            raise
    return messages, out.decode()


def replay(path: Path, recording: str, max_seconds: float) -> tuple[list[str], list[Any], str]:
    """`run` with the recording's events up to `max_seconds`. Returns the fix lines they should
    print as well."""
    lines = (ROOT / "recordings" / recording).read_text().splitlines()
    events = [json.loads(line) for line in lines[1:]]
    events = [event for event in events if event["t"] < max_seconds * 1_000_000_000]
    return expected_fixes(events), *run(path, events, max_seconds)


def nmea(body: str) -> bytes:
    check = 0
    for char in body.encode("ascii"):
        check ^= char
    return f"${body}*{check:02X}\r\n".encode("ascii")


def fixes(console: str) -> list[str]:
    return [line.rstrip("\r") for line in console.splitlines() if line.startswith("fix")]


def test_prints_the_fixes_of_the_recording(socket_path: Path) -> None:
    expected, messages, console = replay(socket_path, "gnss-generated-1.jsonl", 4.5)
    assert expected[0] == "fix acquired lat=52.229677 lon=21.012233"
    assert len(expected) == 4
    assert fixes(console) == expected
    route = f"UART{GNSS.port} RX GPIO{GNSS.rx} TX GPIO{GNSS.tx} {GNSS.baud} baud"
    assert f"nmea_reader: {route}" in console
    # the route the board profile gives, configured by the firmware itself
    routes = [m for m in messages if m["type"] == "uart_cfg"]
    assert [(m["port"], m["tx_pins"], m["rx_pin"], m["baud"]) for m in routes][-1] == (
        GNSS.port,
        [GNSS.tx],
        GNSS.rx,
        GNSS.baud,
    )
    stats = messages[-1]
    assert stats["type"] == "stats" and stats["end"] == "max_seconds"
    assert not [m for m in messages if m["type"] == "warn"]


def test_the_fix_follows_the_recording(socket_path: Path) -> None:
    expected, _, console = replay(socket_path, "gnss-generated-2.jsonl", 2.5)
    assert expected[0] == "fix acquired lat=-34.603700 lon=-58.381600"
    assert fixes(console) == expected


def test_only_well_formed_fixes_count_and_an_epoch_prints_once(socket_path: Path) -> None:
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
    events = [
        {"t": 1_000_000_000 + i * 200_000_000, "data": base64.b64encode(data).decode()}
        for i, data in enumerate(feed)
    ]
    _, console = run(socket_path, events, 3.5)
    assert fixes(console) == [
        # 13.78 minutes is 0.229666... degrees; 0.123456 minutes is 0.0020576 degrees
        "fix acquired lat=52.229667 lon=21.002058",
        "fix lat=-34.603700 lon=-58.381600",
        "fix lat=90.000000 lon=-180.000000",
    ]
