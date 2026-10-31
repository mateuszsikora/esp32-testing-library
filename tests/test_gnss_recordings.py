"""The generated GNSS recordings in recordings/ (plan M1): what scripts/generate-gnss-recordings.py
writes, checked against the script and read back here as sentences, without esp32tl's recording
reader, which does not exist yet."""

import base64
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "generate-gnss-recordings.py"
RECORDINGS = ["gnss-generated-1.jsonl", "gnss-generated-2.jsonl"]


def read(name: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    lines = (ROOT / "recordings" / name).read_text().splitlines()
    header, *events = (json.loads(line) for line in lines)
    return header, events


def sentences(events: list[dict[str, Any]]) -> list[str]:
    return [base64.b64decode(event["data"]).decode("ascii") for event in events]


def first_position(name: str) -> tuple[str, str]:
    """The latitude and longitude fields of the first sentence, a GGA."""
    fields = sentences(read(name)[1])[0].split(",")
    return ",".join(fields[2:4]), ",".join(fields[4:6])


def test_the_committed_recordings_are_what_the_script_generates() -> None:
    check = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"], capture_output=True, text=True, timeout=30
    )
    assert check.returncode == 0, check.stderr


def test_check_names_a_recording_that_differs(tmp_path: Path) -> None:
    # The script writes next to itself: a copy in a scratch tree, with one recording edited.
    (tmp_path / "scripts").mkdir()
    (tmp_path / "recordings").mkdir()
    (tmp_path / "scripts" / SCRIPT.name).write_text(SCRIPT.read_text())
    for name in RECORDINGS:
        (tmp_path / "recordings" / name).write_text((ROOT / "recordings" / name).read_text())
    edited = tmp_path / "recordings" / RECORDINGS[1]
    edited.write_text(edited.read_text().replace('"seed": 2', '"seed": 3'))
    check = subprocess.run(
        [sys.executable, str(tmp_path / "scripts" / SCRIPT.name), "--check"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert check.returncode == 1
    assert check.stderr == (
        "scripts/generate-gnss-recordings.py: recordings/gnss-generated-2.jsonl is not what the"
        " script generates; run python3 scripts/generate-gnss-recordings.py\n"
    )


@pytest.mark.parametrize("name", RECORDINGS)
def test_a_recording_is_one_valid_sentence_per_emulated_second(name: str) -> None:
    header, events = read(name)
    assert header["esp32tl_recording"] == 1
    assert (header["board"], header["interface"]) == ("esp32-devkitc", "uart.gnss")
    assert (header["clock"], header["end"], header["source"]) == (
        "emulated-fifo-write",
        "hold",
        "generated",
    )
    assert header["generator"]["baud"] == 9600
    assert len(events) == 60
    assert [event["t"] for event in events] == [(1 + i) * 1_000_000_000 for i in range(60)]
    assert {event["dir"] for event in events} == {"in"}
    for i, text in enumerate(sentences(events)):
        assert text.startswith("$GPRMC," if i % 2 else "$GPGGA,") and text.endswith("\r\n")
        body, _, check = text[1:-2].partition("*")
        value = 0
        for char in body.encode("ascii"):
            value ^= char
        assert check == f"{value:02X}", text
        # NMEA's 82 characters at most, and on the wire (10 bit times a byte at 9600 baud)
        # well within its second
        assert len(text) <= 82
        assert len(text) * 10 / 9600 < 0.5


def test_the_two_recordings_are_in_different_places() -> None:
    assert first_position(RECORDINGS[0]) == ("5213.7806,N", "02100.7340,E")
    assert first_position(RECORDINGS[1]) == ("3436.2220,S", "05822.8960,W")
