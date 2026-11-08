"""ESP-IDF's storage/sd_card/sdspi example, unmodified, with the runner's SD card model in the
board profile's slot (plan M7): the test prepares a FAT image with mtools, the example mounts it
over SPI, writes, renames and reads its files, and the test reads them back from the image with
mtools. Also what the card's fault rules do to the example, and what a test that uses the
`sd_card` fixture out of order sees. Needs `make firmware` (sdspi) and mtools.
"""

import itertools
import os
import subprocess
from pathlib import Path

import pytest

from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.interfaces import RunEnded, SourceError
from esp32tl.interfaces.console import Console
from esp32tl.interfaces.gnss import Gnss
from esp32tl.interfaces.modem import Modem
from esp32tl.interfaces.sd_card import SdCard
from esp32tl.interfaces.uart import Uart
from esp32tl.units import ms, seconds

ROOT = Path(__file__).resolve().parent.parent
SDSPI = ROOT / "firmware" / "sdspi"

pytestmark = [pytest.mark.app_path("../firmware/sdspi"), pytest.mark.requires_mtools]

# The example is done in about 0.1 s of the board's time.
TIMEOUT = 5


def mtools(*args: str) -> str:
    """An mtools command on an image file. MTOOLS_SKIP_CHECK: mtools compares a file system's
    geometry with the drive's, and an image file is no drive."""
    env = {**os.environ, "MTOOLS_SKIP_CHECK": "1"}
    return subprocess.run(args, check=True, capture_output=True, text=True, env=env).stdout


def fat_image(path: Path, files: dict[str, bytes] | None = None) -> Path:
    """A 32 MiB FAT image at `path`, without a partition table, holding `files`."""
    path.write_bytes(b"")
    os.truncate(path, 32 << 20)
    mtools("mformat", "-i", str(path), "-T", str(32 << 11), "-h", "64", "-s", "32", "::")
    for name, data in (files or {}).items():
        source = path.with_name(name)
        source.write_bytes(data)
        mtools("mcopy", "-i", str(path), str(source), f"::{name}")
    return path


def read(image: Path, name: str) -> bytes:
    return subprocess.run(
        ["mtype", "-i", str(image), f"::{name}"],
        check=True,
        capture_output=True,
        env={**os.environ, "MTOOLS_SKIP_CHECK": "1"},
    ).stdout


def names(image: Path) -> list[str]:
    """The root directory's file names, as `mdir -b` lists them: `::/FOO.TXT`."""
    return sorted(mtools("mdir", "-b", "-i", str(image), "::").split())


def test_the_example_writes_its_files_into_the_image(
    tmp_path: Path, board: Esp32Sim, sd_card: SdCard, console: Console
) -> None:
    # foo.txt is there before: the example deletes it before its rename
    image = fat_image(tmp_path / "card.img", {"foo.txt": b"from the test\n"})
    assert sd_card.insert(image) == image
    board.power_on()
    console.expect_exact("example: Filesystem mounted", timeout=TIMEOUT)
    # the card's registers, as the driver read them: CID, CSD, SSR
    for line in ("Name: SIMSD", "Type: SDHC/SDXC", "Size: 32MB", "SSR: bus_width=1"):
        console.expect_exact(line, timeout=TIMEOUT)
    console.expect_exact("example: Read from file: 'Hello SIMSD!'", timeout=TIMEOUT)
    console.expect_exact("example: Read from file: 'Nihao SIMSD!'", timeout=TIMEOUT)
    console.expect_exact("example: Card unmounted", timeout=TIMEOUT)
    assert names(image) == ["::/FOO.TXT", "::/NIHAO.TXT"]
    assert read(image, "foo.txt") == b"Hello SIMSD!\n"
    assert read(image, "nihao.txt") == b"Nihao SIMSD!\n"
    # the run's report names the card and the test's step
    report = board.report(run=True)
    assert f"sd_card: a card holding {image}" in report
    assert f"sd_card.insert({str(image)!r})" in report
    assert board.properties()["sd_card"] == {
        "image": str(image),
        "changes": [],
        "refused": [],
        "not_reached": [],
        "faults": [],
    }


def test_the_example_mounts_a_32_gib_card_with_32_kib_clusters(
    board: Esp32Sim, sd_card: SdCard, console: Console
) -> None:
    card = sd_card.image(size_mb=32768, cluster_kb=32).build()
    sd_card.insert(card)
    board.power_on()
    console.expect_exact("example: Filesystem mounted", timeout=TIMEOUT)
    for line in ("Type: SDHC/SDXC", "Size: 32768MB"):
        console.expect_exact(line, timeout=TIMEOUT)
    console.expect_exact("example: Card unmounted", timeout=TIMEOUT)
    assert card.read_text("nihao.txt") == "Nihao SIMSD!\n"


@pytest.mark.board_profile("lilygo-t-a7670g-r2")
def test_the_lilygo_slot_writes_an_image_alongside_both_uart_models(
    tmp_path: Path,
    board: Esp32Sim,
    sd_card: SdCard,
    console: Console,
    gnss: Gnss,
    modem: Modem,
) -> None:
    gnss.lose_fix()
    modem.register()
    image = fat_image(tmp_path / "lilygo.img")
    sd_card.insert(image)
    board.power_on()
    console.expect_exact("example: Card unmounted", timeout=TIMEOUT)
    assert read(image, "foo.txt") == b"Hello SIMSD!\n"
    assert read(image, "nihao.txt") == b"Nihao SIMSD!\n"


def test_no_tap_writes_over_the_card_s_image(
    tmp_path: Path, board: Esp32Sim, sd_card: SdCard, gnss: Uart
) -> None:
    image = tmp_path / "card.img"
    image.write_bytes(bytes(1 << 20))
    sd_card.insert(image)
    with pytest.raises(SourceError, match="it is the card of sd_card"):
        gnss.tap(image)
    # nor does a replay read what the card writes
    with pytest.raises(SourceError, match=r"cannot replay .*: it is the card of sd_card"):
        gnss.replay(image)
    # a test that sets up a card and does not power on fails at teardown
    board.power_on()


def test_a_card_does_not_take_a_tap_s_file(
    tmp_path: Path, board: Esp32Sim, sd_card: SdCard, gnss: Uart
) -> None:
    tap = tmp_path / "gnss.jsonl"
    gnss.tap(tap)
    tap.write_bytes(bytes(1 << 20))
    with pytest.raises(SourceError, match=r"sd_card: cannot hold .*: it is the tap of uart.gnss"):
        sd_card.insert(tap)
    assert board.report().endswith("sd_card: empty")
    assert board.properties()["sd_card"] == {
        "image": None,
        "changes": [],
        "refused": [],
        "not_reached": [],
        "faults": [],
    }
    board.power_on()


def test_a_rule_given_after_the_end_of_the_run_is_refused(
    tmp_path: Path, board: Esp32Sim, sd_card: SdCard
) -> None:
    sd_card.insert(fat_image(tmp_path / "card.img"))
    board.power_on()
    board.wait(seconds(0.01))
    board._terminate()
    with pytest.raises(RunEnded):
        board.wait(seconds(1))
    with pytest.raises(RunEnded, match="the run has ended"):
        sd_card.fail("read")


def test_a_card_without_a_file_system_does_not_mount(
    tmp_path: Path, board: Esp32Sim, sd_card: SdCard, console: Console
) -> None:
    image = tmp_path / "blank.img"
    image.write_bytes(bytes(1 << 20))
    sd_card.insert(image)
    board.power_on()
    # the example does not format a card it cannot mount
    console.expect_exact("example: Failed to mount filesystem.", timeout=TIMEOUT)
    assert image.read_bytes() == bytes(1 << 20)


def test_read_delays_hold_each_read_on_the_board_s_clock(
    tmp_path: Path, board: Esp32Sim, sd_card: SdCard, console: Console
) -> None:
    image = fat_image(tmp_path / "card.img", {"foo.txt": b"from the test\n"})
    sd_card.insert(image)
    slow = sd_card.delay("read", ms(20))
    board.power_on()
    console.expect_exact("example: Card unmounted", timeout=TIMEOUT)
    reads = slow.hits
    assert len(reads) > 1
    assert all(io.op == "read" and io.delay == ms(20) and io.action == "delay" for io in reads)
    # the driver waits for each block before it asks for the next one
    assert all(b.at - a.at >= ms(20) for a, b in itertools.pairwise(reads))
    assert read(image, "foo.txt") == b"Hello SIMSD!\n"
    assert sd_card.faulted == reads
    assert (
        f"  fault 1: sd_card.delay('read', seconds(0.02)) at 0 s; {len(reads)} operations"
        in board.report().splitlines()
    )


def test_a_read_slower_than_the_driver_s_timeout_fails_the_mount_on_the_board_s_clock(
    tmp_path: Path, board: Esp32Sim, sd_card: SdCard, console: Console
) -> None:
    image = fat_image(tmp_path / "card.img")
    before = image.read_bytes()
    sd_card.insert(image)
    slow = sd_card.delay("read", seconds(1.5), count=1)
    board.power_on()
    console.expect_exact("example: Failed to mount filesystem.", timeout=TIMEOUT)
    [first] = slow.hits
    assert first.blocks == range(0, 1)
    # ESP-IDF's read timeout is 1 s (SDMMC_DEFAULT_CMD_TIMEOUT_MS): 0x107 is ESP_ERR_TIMEOUT
    [timeout] = console.lines("sdmmc_read_blocks failed \\(0x107\\)")
    assert seconds(1) <= timeout.at - first.at < seconds(1.01)
    assert image.read_bytes() == before


def test_a_failed_read_of_the_boot_sector_fails_the_mount(
    tmp_path: Path, board: Esp32Sim, sd_card: SdCard, console: Console
) -> None:
    image = fat_image(tmp_path / "card.img")
    sd_card.insert(image)
    broken = sd_card.fail("read", blocks=0, count=1)
    board.power_on()
    console.expect_exact("example: Failed to mount filesystem.", timeout=TIMEOUT)
    [io] = broken.hits
    assert (io.op, io.blocks, io.action, io.written) == ("read", range(0, 1), "error", False)
    # The error token comes right after R1, where ESP-IDF looks only for the start token: it
    # then polls for one that never comes, and gives up after its 1 s read timeout.
    [timeout] = console.lines("sdmmc_read_blocks failed \\(0x107\\)")
    assert seconds(1) <= timeout.at - io.at < seconds(1.01)


def test_fault_rules_are_the_test_s_and_outlast_a_power_cycle(
    tmp_path: Path, board: Esp32Sim, sd_card: SdCard, console: Console
) -> None:
    image = fat_image(tmp_path / "card.img")
    sd_card.insert(image)
    board.power_on()
    console.expect_exact("example: Card unmounted", timeout=TIMEOUT)
    board.power_off()
    # given with the supply off, it acts on the first read after it comes back
    broken = sd_card.fail("read", blocks=0, count=1)
    board.power_on()
    console.expect_exact("example: Failed to mount filesystem.", timeout=TIMEOUT)
    [io] = broken.hits
    assert (io.blocks, io.action) == (range(0, 1), "error")
    assert board.boots[-1].number == 2


def test_fault_rules_outlast_a_reset_of_the_chip(
    tmp_path: Path, board: Esp32Sim, sd_card: SdCard, console: Console
) -> None:
    image = fat_image(tmp_path / "card.img")
    sd_card.insert(image)
    board.power_on()
    console.expect_exact("example: Card unmounted", timeout=TIMEOUT)
    broken = sd_card.fail("read", blocks=0, count=1)
    board.reset()
    console.expect_exact("example: Failed to mount filesystem.", timeout=TIMEOUT)
    [io] = broken.hits
    assert (io.blocks, io.action) == (range(0, 1), "error")
    assert board.boots[-1].number == 2


def test_an_empty_slot_never_answers(board: Esp32Sim, console: Console) -> None:
    board.power_on()
    console.expect_exact(
        "example: Failed to initialize the card (ESP_ERR_TIMEOUT)", timeout=TIMEOUT
    )


def test_the_slot_used_out_of_order_fails_the_test(
    tmp_path: Path, pytester: pytest.Pytester
) -> None:
    if not (SDSPI / "build" / "esp32tl-built").is_file():
        pytest.fail(f"{SDSPI}/build holds no finished build: run make firmware", pytrace=False)
    image = fat_image(tmp_path / "card.img")
    pytester.makepyfile(
        test_sd=f"""
        import pytest

        pytestmark = pytest.mark.app_path({str(SDSPI)!r})

        def test_remove_from_an_empty_slot(board, sd_card):
            board.power_on()
            sd_card.remove()

        def test_never_powered(sd_card):
            sd_card.insert({str(image)!r})

        def test_two_cards(sd_card):
            sd_card.insert({str(image)!r})
            sd_card.insert({str(image)!r})

        def test_a_bad_image(sd_card):
            sd_card.insert("missing.img")

        def test_a_short_image(sd_card, tmp_path):
            (tmp_path / "short.img").write_bytes(bytes(1000))
            sd_card.insert(tmp_path / "short.img")

        def test_a_read_only_image(sd_card, tmp_path):
            (tmp_path / "ro.img").write_bytes(bytes(512 << 10))
            (tmp_path / "ro.img").chmod(0o444)
            sd_card.insert(tmp_path / "ro.img")

        @pytest.mark.board_profile("gps-alarm-clock")
        def test_no_slot(sd_card):
            pass
        """
    )
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-rA")
    # test_never_powered passes its call and errors at teardown; test_two_cards fails on its call
    # only, as the other calls that raise.
    result.assert_outcomes(passed=1, failed=5, errors=2)
    result.stdout.fnmatch_lines_random(
        [
            "*SourceError: sd_card is empty: there is no card to remove",
            "*esp32sim: the test set up a card in sd_card and never called board.power_on():"
            " it ran no firmware",
            "*SourceError: sd_card has a card already: remove it first",
            f"*SourceError: sd_card: no image at {pytester.path}/missing.img: No such file or"
            " directory",
            "*SourceError: sd_card: */short.img has 1000 bytes; an SD card image is a whole"
            " number of 512 KiB units",
            "*SourceError: sd_card: */ro.img cannot be read and written: the card writes into it",
            "*esp32sim: the board profile gps-alarm-clock has no sd_card; its interfaces are"
            " console, uart.gnss, lines.alarm, wifi, bluetooth",
        ]
    )
