"""Prepare a real FAT image, feed GNSS on a wire, then read the firmware's files."""

import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from esp32tl import recording
from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.interfaces.console import Console
from esp32tl.interfaces.gnss import Gnss
from esp32tl.interfaces.lines import Lines, OutputLine
from esp32tl.interfaces.sd_card import SdCard
from esp32tl.units import Position, seconds

pytestmark = [pytest.mark.app_path(".."), pytest.mark.board_profile("sd-data-logger")]
START = datetime(2026, 11, 2, 12, tzinfo=UTC)
WARSAW = Position(52.229677, 21.012233)


def test_each_fix_appends_one_line(
    board: Esp32Sim, gnss: Gnss, card_image: Path, error: OutputLine
) -> None:
    # An existing row must survive: opening with "w" would destroy it.
    previous = card_image.with_name("previous.csv")
    previous.write_text("115959.00,52.000000,21.000000\n")
    mtools("mcopy", "-i", str(card_image), str(previous), "::20261102.CSV")
    gnss.fix(at=WARSAW)
    board.power_on(utc=START)
    board.wait(seconds(3.5))
    expected = (
        "115959.00,52.000000,21.000000\n"
        "120001.00,52.229677,21.012233\n"
        "120002.00,52.229677,21.012233\n"
        "120003.00,52.229677,21.012233\n"
    )
    assert read(card_image, "20261102.CSV") == expected
    error.assert_off()
    healthy_since = board.now

    gnss.fix(at=Position(-34.6037, -58.3816))
    board.wait(seconds(2))
    assert read(card_image, "20261102.CSV") == (
        expected + "120004.00,-34.603700,-58.381600\n120005.00,-34.603700,-58.381600\n"
    )
    assert names(card_image) == ["::/20261102.CSV"]
    error.assert_stayed_off(since=healthy_since)


@pytest.mark.parametrize(
    ("start", "old_file", "new_file"),
    [
        (datetime(2026, 12, 31, 23, 59, 57, tzinfo=UTC), "20261231.CSV", "20270101.CSV"),
        (datetime(2028, 2, 28, 23, 59, 57, tzinfo=UTC), "20280228.CSV", "20280229.CSV"),
    ],
)
def test_a_new_gnss_date_starts_a_new_file(
    board: Esp32Sim,
    gnss: Gnss,
    card_image: Path,
    start: datetime,
    old_file: str,
    new_file: str,
) -> None:
    gnss.fix(at=WARSAW)
    board.power_on(utc=start)
    board.wait(seconds(2.5))
    before_midnight = "235958.00,52.229677,21.012233\n235959.00,52.229677,21.012233\n"
    assert read(card_image, old_file) == before_midnight
    assert names(card_image) == [f"::/{old_file}"]

    board.wait(seconds(3))
    assert read(card_image, old_file) == before_midnight
    assert read(card_image, new_file) == (
        "000000.00,52.229677,21.012233\n"
        "000001.00,52.229677,21.012233\n"
        "000002.00,52.229677,21.012233\n"
    )
    assert names(card_image) == [f"::/{old_file}", f"::/{new_file}"]


def test_without_a_fix_no_file_is_created(
    board: Esp32Sim, gnss: Gnss, card_image: Path, error: OutputLine
) -> None:
    gnss.lose_fix()
    board.power_on(utc=START)
    board.wait(seconds(3.5))
    assert names(card_image) == []
    error.assert_off()  # No fix is not a storage error.


def test_losing_the_fix_pauses_logging_and_recovery_appends(
    board: Esp32Sim, gnss: Gnss, card_image: Path
) -> None:
    gnss.fix(at=WARSAW)
    board.power_on(utc=START)
    board.wait(seconds(2.5))
    expected = "120001.00,52.229677,21.012233\n120002.00,52.229677,21.012233\n"
    assert read(card_image, "20261102.CSV") == expected

    gnss.lose_fix()
    board.wait(seconds(3))
    assert read(card_image, "20261102.CSV") == expected
    gnss.fix(at=WARSAW)
    board.wait(seconds(2))
    assert read(card_image, "20261102.CSV") == (
        expected + "120006.00,52.229677,21.012233\n120007.00,52.229677,21.012233\n"
    )


def test_bad_sentences_cannot_create_files(
    board: Esp32Sim, gnss: Gnss, card_image: Path, tmp_path: Path
) -> None:
    prefix = "GPRMC,{time},A,5213.78062,N,02100.73398,E,0.0,,{date},,,A"
    feed = [
        nmea(prefix.format(time="120001.00", date="000000")),
        nmea(prefix.format(time="120001.00", date="310226")),
        nmea(prefix.format(time="120001.00", date="290227")),
        nmea(prefix.format(time="246000.00", date="021126")),
        nmea(prefix.format(time="120001.x", date="021126")),
        nmea(prefix.format(time="120001.00", date="../../")),
        b"$GPRMC,120001.00,A,5213.78062,N,02100.73398,E,0.0,,021126,,,A*XX\r\n",
        b"$" + b"x" * 100 + b"\r\n",
        nmea(prefix.format(time="120003.00", date="021126")),
    ]
    header = recording.Header(
        "sd-data-logger", "uart.gnss", "generated", recording.EMULATED, "2026-11-02T00:00:00Z"
    )
    events = tuple(
        recording.Event(1_000_000_000 + i * 250_000_000, "in", data, recording.EMULATED)
        for i, data in enumerate(feed)
    )
    path = tmp_path / "feed.jsonl"
    recording.write(path, recording.Recording(header, events))
    gnss.replay(path)
    board.power_on(utc=START)
    board.wait(seconds(4))
    assert names(card_image) == ["::/20261102.CSV"]
    assert read(card_image, "20261102.CSV") == "120003.00,52.229677,21.012233\n"


def test_without_a_card_the_error_pin_stays_on_and_fixes_keep_arriving(
    board: Esp32Sim, gnss: Gnss, error: OutputLine, console: Console
) -> None:
    gnss.lose_fix()
    board.power_on(utc=START)
    console.expect_exact("sd_data_logger: SD card unavailable (ESP_ERR_TIMEOUT)")
    console.expect_exact("sd_data_logger: ready")
    error.assert_on()
    failed_since = board.now

    # Supply new input after initialization has failed, proving the UART loop survives.
    gnss.fix(at=WARSAW)
    pattern = rb"sd_data_logger: fix 20261102 (\d{6}\.00) \(card unavailable\)"
    first = console.expect(pattern, timeout=seconds(2)).group(1)
    second = console.expect(pattern, timeout=seconds(2)).group(1)
    assert second > first
    error.assert_stayed_on(since=failed_since)


@pytest.fixture
def error(lines: Lines) -> OutputLine:
    return lines.output("logger.error")


@pytest.fixture
def card_image(tmp_path: Path, sd_card: SdCard) -> Path:
    missing = [tool for tool in ("mformat", "mcopy", "mdir", "mtype") if not shutil.which(tool)]
    if missing:
        pytest.skip(f"SD logger tests need mtools: missing {', '.join(missing)}")
    image = tmp_path / "card.img"
    with image.open("wb") as file:
        file.truncate(32 << 20)
    mtools("mformat", "-i", str(image), "-T", str(32 << 11), "-h", "64", "-s", "32", "::")
    sd_card.insert(image)
    return image


def mtools(*args: str) -> str:
    return subprocess.run(
        args,
        check=True,
        capture_output=True,
        env={**os.environ, "MTOOLS_SKIP_CHECK": "1"},
    ).stdout.decode("ascii")


def read(image: Path, name: str) -> str:
    return mtools("mtype", "-i", str(image), f"::{name}")


def names(image: Path) -> list[str]:
    return sorted(mtools("mdir", "-b", "-i", str(image), "::").split())


def nmea(body: str) -> bytes:
    checksum = 0
    for byte in body.encode("ascii"):
        checksum ^= byte
    return f"${body}*{checksum:02X}\r\n".encode("ascii")
