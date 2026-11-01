"""The runs the determinism job runs twice and compares (plan §6): `make determinism` runs this
file twice, each time under a `--basetemp` of its own, and `scripts/check-determinism.py` then
finds the two runs identical or prints what differs (docs/guides/testing.md#determinism).

Each test leaves in its `tmp_path` what the job compares: `console.txt`, every line nmea_reader
printed with the board's time it ended at, and `uart.gnss.jsonl`, the tap of the GNSS route. Each
test also ends the run at a time of its own choosing, so that two runs end at the same instant.
Alone, in the suite, each checks that its run did what it should, so that two runs that both did
nothing cannot pass the job.
"""

from pathlib import Path

import pytest

from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.interfaces.console import Console
from esp32tl.interfaces.gnss import Gnss
from esp32tl.report import board_time
from esp32tl.units import EAST, NORTH, Position, km, km_h, seconds

pytestmark = pytest.mark.app_path("../firmware/nmea_reader")

WARSAW = Position(52.229677, 21.012233)

# A fix line as nmea_reader prints it.
FIX = r"^fix(?: acquired)? lat=\S+ lon=\S+$"


@pytest.mark.parametrize("recording", ["gnss-generated-1", "gnss-generated-2"])
def test_a_replay(
    recording: str, board: Esp32Sim, gnss: Gnss, console: Console, tmp_path: Path
) -> None:
    gnss.replay(f"../recordings/{recording}.jsonl")
    gnss.tap(tmp_path / "uart.gnss.jsonl")
    board.power_on()
    console.expect_exact("fix acquired")
    board.wait(seconds(20))
    write_console(tmp_path, console)
    # One sentence a second from 1 s, and the run ends past 21 s.
    assert len(console.lines(FIX)) == 21


def test_the_receiver_model(board: Esp32Sim, gnss: Gnss, console: Console, tmp_path: Path) -> None:
    gnss.fix(at=WARSAW)
    gnss.tap(tmp_path / "uart.gnss.jsonl")
    board.power_on()
    board.wait_until(console.line(FIX), within=seconds(5))
    gnss.move(heading=NORTH, speed=km_h(36))
    board.wait(seconds(10))
    gnss.lose_fix()
    board.wait(seconds(5))
    gnss.fix(at=WARSAW.moved(EAST, km(2)))
    board.wait(seconds(5))
    write_console(tmp_path, console)
    assert len(console.lines(FIX)) >= 15


def write_console(directory: Path, console: Console) -> None:
    """Writes `console.txt` into `directory`: each line the firmware has printed, after the
    board's time it ended at."""
    lines = (f"{board_time(line.at.ns)}  {line.text}\n" for line in console.lines())
    (directory / "console.txt").write_text("".join(lines))
