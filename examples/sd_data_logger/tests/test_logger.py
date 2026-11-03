"""Prepare a real FAT image, feed GNSS on a wire, then read the firmware's files."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from esp32tl import Board, recording
from esp32tl.interfaces.console import Console
from esp32tl.units import Duration, Position, ms, seconds

pytestmark = [pytest.mark.app_path(".."), pytest.mark.board_profile("sd-data-logger")]
START = datetime(2026, 11, 2, 12, tzinfo=UTC)
WARSAW = Position(52.229677, 21.012233)
# Every model scenario boots on a whole UTC second: its first fix is at 1 s,
# then one per second. Read after the final fix's UART reception and FAT close,
# but before the next epoch. Later whole-interval waits preserve this margin.
FIX_INTERVAL = seconds(1)
WRITE_MARGIN = seconds(0.5)


def test_each_fix_appends_one_line(board: Board) -> None:
    # An existing row must survive: opening with "w" would destroy it.
    card = (
        board.sd_card.image(size_mb=32)
        .with_text("20261102.CSV", "115959.00,52.000000,21.000000\n")
        .build()
    )
    error = board.lines.output("logger.error")
    board.sd_card.insert(card)
    board.gnss.fix(at=WARSAW)
    board.power_on(utc=START)
    board.wait(3 * FIX_INTERVAL + WRITE_MARGIN)
    expected = (
        "115959.00,52.000000,21.000000\n"
        "120001.00,52.229677,21.012233\n"
        "120002.00,52.229677,21.012233\n"
        "120003.00,52.229677,21.012233\n"
    )
    assert card.read_text("20261102.CSV") == expected
    error.assert_off()
    healthy_since = board.now

    board.gnss.fix(at=Position(-34.6037, -58.3816))
    board.wait(2 * FIX_INTERVAL)
    assert card.read_text("20261102.CSV") == (
        expected + "120004.00,-34.603700,-58.381600\n120005.00,-34.603700,-58.381600\n"
    )
    assert card.list_files() == ["20261102.CSV"]
    error.assert_stayed_off(since=healthy_since)


@pytest.mark.parametrize(
    ("start", "old_file", "new_file"),
    [
        (datetime(2026, 12, 31, 23, 59, 57, tzinfo=UTC), "20261231.CSV", "20270101.CSV"),
        (datetime(2028, 2, 28, 23, 59, 57, tzinfo=UTC), "20280228.CSV", "20280229.CSV"),
    ],
)
def test_a_new_gnss_date_starts_a_new_file(
    board: Board,
    start: datetime,
    old_file: str,
    new_file: str,
) -> None:
    card = board.sd_card.image(size_mb=32).build()
    board.sd_card.insert(card)
    board.gnss.fix(at=WARSAW)
    board.power_on(utc=start)
    board.wait(2 * FIX_INTERVAL + WRITE_MARGIN)
    before_midnight = "235958.00,52.229677,21.012233\n235959.00,52.229677,21.012233\n"
    assert card.read_text(old_file) == before_midnight
    assert card.list_files() == [old_file]

    board.wait(3 * FIX_INTERVAL)
    assert card.read_text(old_file) == before_midnight
    assert card.read_text(new_file) == (
        "000000.00,52.229677,21.012233\n"
        "000001.00,52.229677,21.012233\n"
        "000002.00,52.229677,21.012233\n"
    )
    assert card.list_files() == [old_file, new_file]


def test_without_a_fix_no_file_is_created(board: Board) -> None:
    card = board.sd_card.image(size_mb=32).build()
    error = board.lines.output("logger.error")
    board.sd_card.insert(card)
    board.gnss.lose_fix()
    board.power_on(utc=START)
    board.wait(3 * FIX_INTERVAL + WRITE_MARGIN)
    assert card.list_files() == []
    error.assert_off()  # No fix is not a storage error.


def test_losing_the_fix_pauses_logging_and_recovery_appends(board: Board) -> None:
    card = board.sd_card.image(size_mb=32).build()
    board.sd_card.insert(card)
    board.gnss.fix(at=WARSAW)
    board.power_on(utc=START)
    board.wait(2 * FIX_INTERVAL + WRITE_MARGIN)
    expected = "120001.00,52.229677,21.012233\n120002.00,52.229677,21.012233\n"
    assert card.read_text("20261102.CSV") == expected

    board.gnss.lose_fix()
    board.wait(3 * FIX_INTERVAL)
    assert card.read_text("20261102.CSV") == expected
    board.gnss.fix(at=WARSAW)
    board.wait(2 * FIX_INTERVAL)
    assert card.read_text("20261102.CSV") == (
        expected + "120006.00,52.229677,21.012233\n120007.00,52.229677,21.012233\n"
    )


def test_bad_sentences_cannot_create_files(board: Board, tmp_path: Path) -> None:
    card = board.sd_card.image(size_mb=32).build()
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
    first_sentence_at = seconds(1)
    sentence_interval = seconds(0.25)
    path = write_gnss_recording(
        tmp_path / "feed.jsonl", feed, first_at=first_sentence_at, interval=sentence_interval
    )
    board.sd_card.insert(card)
    board.gnss.replay(path)
    board.power_on(utc=START)
    last_sentence_at = first_sentence_at + (len(feed) - 1) * sentence_interval
    board.wait(last_sentence_at + WRITE_MARGIN)
    assert card.list_files() == ["20261102.CSV"]
    assert card.read_text("20261102.CSV") == "120003.00,52.229677,21.012233\n"


def test_a_write_that_fails_after_logging_works_stops_logging_and_keeps_the_rows(
    board: Board,
) -> None:
    card = board.sd_card.image(size_mb=32).build()
    error = board.lines.output("logger.error")
    board.sd_card.insert(card)
    board.gnss.fix(at=WARSAW)
    board.power_on(utc=START)
    board.wait(2 * FIX_INTERVAL + WRITE_MARGIN)
    saved = "120001.00,52.229677,21.012233\n120002.00,52.229677,21.012233\n"
    assert card.read_text("20261102.CSV") == saved
    error.assert_off()

    # The next write the card gets fails, whichever block it is: the row's data.
    fault = board.sd_card.fail("write")
    board.console.expect_exact("sd_data_logger: SD card write failed", timeout=FIX_INTERVAL)
    board.console.expect_exact("sd_data_logger: fix 20261102 120003.00 (card unavailable)")
    error.assert_on()
    [failed] = fault.hits
    assert (failed.op, failed.failed, failed.written) == ("write", True, False)
    # The firmware writes no more until it restarts, and the file is as it was before the fault.
    next_unsaved_fix_time(board.console)
    assert card.read_text("20261102.CSV") == saved
    assert board.sd_card.affected == (failed,)


def test_a_slow_card_delays_each_saved_fix_by_its_busy_time(board: Board) -> None:
    card = board.sd_card.image(size_mb=32).build()
    error = board.lines.output("logger.error")
    board.sd_card.insert(card)
    board.gnss.fix(at=WARSAW)
    slow = board.sd_card.delay("write", ms(300))
    board.power_on(utc=START)
    # The firmware waits out each write before it reads the next fix, which the UART buffers.
    board.console.expect_exact("fix 20261102 120002.00 (saved)", timeout=5 * FIX_INTERVAL)
    first, second = board.console.lines(r"\(saved\)")
    # The second row takes a write of its data and one of its directory entry, in turn.
    writes = [io for io in slow.hits if first.at < io.at < second.at]
    assert len(writes) == 2
    assert writes[1].at - writes[0].at >= ms(300)
    assert second.at - writes[0].at >= ms(600)
    assert card.read_text("20261102.CSV") == (
        "120001.00,52.229677,21.012233\n120002.00,52.229677,21.012233\n"
    )
    error.assert_off()


def test_a_write_busier_than_esp_idf_s_timeout_fails_on_the_board_s_clock(board: Board) -> None:
    card = board.sd_card.image(size_mb=32).build()
    error = board.lines.output("logger.error")
    board.sd_card.insert(card)
    board.gnss.fix(at=WARSAW)
    board.power_on(utc=START)
    board.wait(2 * FIX_INTERVAL + WRITE_MARGIN)
    error.assert_off()

    # The card takes the next block, then stays busy past ESP-IDF's 5 s write timeout
    # (SDMMC_WRITE_CMD_TIMEOUT_MS): 0x107 is ESP_ERR_TIMEOUT.
    busy = board.sd_card.delay("write", seconds(6), count=1)
    board.console.expect_exact("sd_data_logger: SD card write failed", timeout=7 * FIX_INTERVAL)
    [write] = busy.hits
    [timeout] = board.console.lines(r"sdmmc_write_blocks failed \(0x107\)")
    assert seconds(5) <= timeout.at - write.at < seconds(5.01)
    # The block went into the image before the card was busy: a timeout is not a failed write.
    assert (write.failed, write.written) == (False, True)
    error.assert_on()


def test_without_a_card_the_error_pin_stays_on_and_fixes_keep_arriving(board: Board) -> None:
    error = board.lines.output("logger.error")
    board.gnss.lose_fix()
    board.power_on(utc=START)
    board.console.expect_exact("sd_data_logger: SD card unavailable (ESP_ERR_TIMEOUT)")
    board.console.expect_exact("sd_data_logger: ready")
    error.assert_on()
    failed_since = board.now

    # Supply new input after initialization has failed, proving the UART loop survives.
    board.gnss.fix(at=WARSAW)
    first = next_unsaved_fix_time(board.console)
    second = next_unsaved_fix_time(board.console)
    assert first.date() == second.date() == START.date()
    assert second > first
    error.assert_stayed_on(since=failed_since)


def write_gnss_recording(
    path: Path, sentences: list[bytes], *, first_at: Duration, interval: Duration
) -> Path:
    """Wrap the scenario's raw UART bytes in a recording, without changing their checksums."""
    header = recording.Header(
        "sd-data-logger", "uart.gnss", "generated", recording.EMULATED, "2026-11-02T00:00:00Z"
    )
    events = tuple(
        recording.Event((first_at + i * interval).ns, "in", data, recording.EMULATED)
        for i, data in enumerate(sentences)
    )
    recording.write(path, recording.Recording(header, events))
    return path


def next_unsaved_fix_time(console: Console) -> datetime:
    """Wait at most two fix intervals for the next fix reported with unavailable storage."""
    pattern = rb"sd_data_logger: fix (\d{8} \d{6}\.\d+) \(card unavailable\)"
    match = console.expect(pattern, timeout=2 * FIX_INTERVAL)
    return datetime.strptime(match.group(1).decode("ascii"), "%Y%m%d %H%M%S.%f").replace(tzinfo=UTC)


def nmea(body: str) -> bytes:
    checksum = 0
    for byte in body.encode("ascii"):
        checksum ^= byte
    return f"${body}*{checksum:02X}\r\n".encode("ascii")
