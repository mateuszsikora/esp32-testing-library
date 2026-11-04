"""`sd_remount` with the SD card taken out and put back while it runs (#106): the firmware sees
its writes fail once the card is out, keeps what it wrote before on that card, and mounts the card
the test puts in, another image or the same one again. Needs `make firmware` (sd_remount) and
mtools.
"""

import re
from pathlib import Path

import pytest

from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.interfaces.console import Console
from esp32tl.interfaces.sd_card import CardImage, SdCard
from esp32tl.report import board_time
from esp32tl.units import seconds

pytestmark = [pytest.mark.app_path("../firmware/sd_remount"), pytest.mark.requires_mtools]

SD_REMOUNT = Path(__file__).resolve().parent.parent / "firmware" / "sd_remount"

# A line every 100 ms; a write to a card that is gone fails within a few hundred.
TIMEOUT = 10
# The longest a slice runs, without a block point to end it sooner: an emulated millisecond.
SLICE = 1_000_000


def written(console: Console) -> list[int]:
    """The line numbers the firmware has announced, in order."""
    found = console.lines(r"^sd_remount: wrote (\d+)$")
    return [int(line.match[1]) for line in found if line.match is not None]


def lines(first: int, last: int) -> str:
    return "".join(f"{n}\n" for n in range(first, last + 1))


def take_out(sd_card: SdCard, console: Console, card: CardImage) -> int:
    """Takes `card` out and waits for the write that fails; returns the last line written
    before it, every one of which is on the card, and nothing after it."""
    assert sd_card.remove() == card.path
    failed = console.expect(rb"sd_remount: write failed \(([^)\r\n]+)\)", timeout=TIMEOUT)
    # the firmware learns of it from the I/O error alone: there is no card-detect line
    assert failed[1] == b"I/O error"
    last = written(console)[-1]
    return last


def test_writes_fail_once_the_card_is_out_and_go_on_on_the_card_put_in(
    board: Esp32Sim, sd_card: SdCard, console: Console
) -> None:
    first = sd_card.image(size_mb=32).build()
    sd_card.insert(first)
    board.power_on()
    console.expect_exact("sd_remount: mounted 1", timeout=TIMEOUT)
    console.expect_exact("sd_remount: wrote 3", timeout=TIMEOUT)
    out_at = board.now.ns
    last = take_out(sd_card, console, first)
    # what was written before is on the card, and the line that failed is not
    assert first.read_text("LOG.TXT") == lines(1, last)
    console.expect(r"sd_remount: no card \(ESP_ERR_\w+\)", timeout=TIMEOUT)

    # another card: a fresh FAT image, mounted afresh, the lines going on where they were
    second = sd_card.image(size_mb=64).with_text("README.TXT", "the second card\n").build()
    in_at = board.now.ns
    sd_card.insert(second)
    console.expect_exact("sd_remount: mounted 2", timeout=TIMEOUT)
    console.expect_exact(f"sd_remount: wrote {last + 2}", timeout=TIMEOUT)
    again_out_at = board.now.ns
    second_last = take_out(sd_card, console, second)
    assert second.read_text("LOG.TXT") == lines(last + 1, second_last)
    assert second.list_files() == ["LOG.TXT", "README.TXT"]

    # the first card again: the firmware appends to the file it left there
    again_in_at = board.now.ns
    sd_card.insert(first)
    console.expect_exact("sd_remount: mounted 3", timeout=TIMEOUT)
    console.expect_exact(f"sd_remount: wrote {second_last + 2}", timeout=TIMEOUT)
    assert first.read_text("LOG.TXT").startswith(lines(1, last) + f"{second_last + 1}\n")

    # the runner applied each change, in order, at the first slice boundary at or after the
    # instant the test made it, and said so
    changes = sd_card.changes
    assert [c.image for c in changes] == [None, second.path, None, first.path]
    for change, asked in zip(changes, [out_at, in_at, again_out_at, again_in_at], strict=True):
        assert asked <= change.t < asked + SLICE, (change, asked)

    # one more removal, which the run never reaches: the run still has the first card
    sd_card.remove()
    report = board.report(run=True)
    assert f"sd_card: a card holding {first.path}" in report
    put_in = re.escape(f"{second.path} put in")
    assert re.search(rf"\n  \S+ s  taken out\n  \S+ s  {put_in}\n", report)
    assert f"  {board_time(board.now.ns)}  not reached: taken out\n" in report
    assert "sd_card.remove()" in report
    assert board.properties()["sd_card"] == {
        "image": str(first.path),
        "changes": [
            {"t_ns": c.t, "image": None if c.image is None else str(c.image)} for c in changes
        ],
        "refused": [],
        "not_reached": [{"t_ns": board.now.ns, "image": None}],
        "faults": [],
    }


def test_a_fault_rule_stays_through_a_change_of_card_until_it_is_cleared(
    board: Esp32Sim, sd_card: SdCard, console: Console
) -> None:
    first = sd_card.image(size_mb=32).build()
    sd_card.insert(first)
    board.power_on()
    console.expect_exact("sd_remount: wrote 2", timeout=TIMEOUT)
    fault = sd_card.fail("write")
    sd_card.remove()
    second = sd_card.image(size_mb=32).build()
    sd_card.insert(second)
    console.expect_exact("sd_remount: mounted 2", timeout=TIMEOUT)

    # the firmware mounts the new card, and its writes meet the rule given on the old one
    board.wait_until(lambda: fault.hits, within=seconds(TIMEOUT))
    assert all(hit.op == "write" and not hit.written for hit in fault.hits)
    assert second.list_files() == []

    fault.clear()
    wrote = board.wait_until(console.line(r"^sd_remount: wrote \d+$"), within=seconds(TIMEOUT))
    assert wrote.match is not None
    assert "LOG.TXT" in second.list_files()


def test_a_slot_empty_at_power_on_takes_a_card_later(
    board: Esp32Sim, sd_card: SdCard, console: Console
) -> None:
    board.power_on()
    console.expect(r"sd_remount: no card \(ESP_ERR_\w+\)", timeout=TIMEOUT)
    card = sd_card.image(size_mb=32).build()
    sd_card.insert(card)
    console.expect_exact("sd_remount: mounted 1", timeout=TIMEOUT)
    console.expect_exact("sd_remount: wrote 2", timeout=TIMEOUT)
    assert card.read_text("LOG.TXT").startswith(lines(1, 2))


def test_a_card_swapped_with_the_supply_off_is_the_one_the_next_boot_finds(
    board: Esp32Sim, sd_card: SdCard, console: Console
) -> None:
    first = sd_card.image(size_mb=32).build()
    sd_card.insert(first)
    board.power_on()
    console.expect_exact("sd_remount: wrote 1", timeout=TIMEOUT)
    board.power_off()
    assert sd_card.remove() == first.path
    second = sd_card.image(size_mb=32).build()
    sd_card.insert(second)
    # the runner applies the changes with the supply off, at their time
    board.wait(seconds(0.1))
    assert [c.image for c in sd_card.changes] == [None, second.path]
    assert sd_card.held == second.path
    assert first.read_text("LOG.TXT") == lines(1, written(console)[-1])
    assert second.list_files() == []
    # the new boot counts from 1 again, and writes on the card it finds
    board.power_on()
    console.expect_exact("sd_remount: mounted 1", timeout=TIMEOUT)
    console.expect_exact("sd_remount: wrote 2", timeout=TIMEOUT)
    assert second.read_text("LOG.TXT").startswith(lines(1, 2))


def test_a_card_the_runner_cannot_open_ends_the_run(pytester: pytest.Pytester) -> None:
    if not (SD_REMOUNT / "build" / "esp32tl-built").is_file():
        pytest.fail(f"{SD_REMOUNT}/build holds no finished build: run make firmware", pytrace=False)
    pytester.makepyfile(
        test_refused=f"""
        import os

        import pytest

        pytestmark = pytest.mark.app_path({str(SD_REMOUNT)!r})

        def test_a_card_gone_before_it_goes_in(board, sd_card, console):
            board.power_on()
            card = sd_card.image(size_mb=1).build()
            sd_card.insert(card)
            # checked at insert, gone by the time the runner opens it
            os.remove(card.path)
            console.expect_exact("sd_remount: mounted 1", timeout=5)
        """
    )
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-rA")
    # the waiting call fails on the end of the run, with the link's reason first
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "*RunEnded: the run ended * while the test waited for 'sd_remount: mounted 1' on the"
            " console: the link failed: sd_card: the runner refused the card at * ns: */card.img:"
            " No such file or directory (os error 2)*",
        ]
    )
