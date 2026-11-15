"""What a test reads of the running board's flash: firmware/persistence's boot count in NVS, while
the board stands still, after a reset, after a power-off and after the end of the run. Needs
`make firmware` (persistence)."""

import hashlib

import pytest

from esp32tl.backends import esp32sim
from esp32tl.backends.esp32sim import BackendError, Esp32Sim
from esp32tl.flash import FlashSnapshot, nvs_values, u32
from esp32tl.interfaces import BoardOff, RunEnded
from esp32tl.interfaces.console import Console
from esp32tl.units import seconds

pytestmark = pytest.mark.app_path("../firmware/persistence")


def boots(snapshot: FlashSnapshot) -> object:
    return snapshot.nvs()["persistence"]["boots"]


def test_boots_read_as_the_current_value_with_the_board_s_time_unmoved(
    board: Esp32Sim, console: Console
) -> None:
    board.power_on()
    # what the build holds, before the guest's first instruction: no boot counted yet
    first = board.flash()
    assert first.nvs() == {}
    assert board.now == seconds(0)

    console.expect_exact("persistence: done")
    now, turns = board.now, board.link.turns
    snapshot = board.flash()
    again = board.flash()
    assert boots(snapshot) == u32(2)
    assert again.contents == snapshot.contents
    assert board.now == now, "reading the flash moves no time"
    assert board.link.turns == turns, "nor does it take a turn"
    # a snapshot is of its instant: the first one holds what it held
    assert first.nvs() == {}
    with pytest.raises(TypeError):
        snapshot.table["x"] = snapshot.table["nvs"]  # type: ignore[index]

    board.reset()
    console.expect_exact("persistence: done")
    assert boots(board.flash()) == u32(3)
    assert boots(snapshot) == u32(2)
    assert "board.flash()" in board.report(run=True)


def test_boots_read_after_a_power_off_and_while_the_board_stays_off(
    board: Esp32Sim, console: Console
) -> None:
    board.power_on()
    console.expect_exact("persistence: done")
    board.power_off()
    # asked in the same instant, before the supply has gone
    assert boots(board.flash()) == u32(2)
    board.wait(seconds(1))
    off = board.now
    assert boots(board.flash()) == u32(2)
    assert board.now == off
    board.power_on()
    console.expect_exact("persistence: done")
    assert boots(board.flash()) == u32(3)


def test_boots_read_after_the_run_has_ended(
    board: Esp32Sim, console: Console, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(esp32sim, "HELD_MAX_SECONDS", 1)
    board.power_on()
    console.expect_exact("persistence: done")
    # a file from a turn must not pass for the end of the run
    assert boots(board.flash()) == u32(2)
    with pytest.raises(RunEnded):
        board.wait(seconds(2))
    snapshot = board.flash()
    assert boots(snapshot) == u32(2)
    assert board.flash().contents == snapshot.contents
    assert len(snapshot.contents) == 4 << 20


def test_a_partition_reads_as_the_whole_of_it_and_the_build_stays_as_it_was(
    board: Esp32Sim, console: Console
) -> None:
    built = {
        f.offset: hashlib.sha256(f.path.read_bytes()).hexdigest() for f in board.app.flash_files
    }
    board.power_on()
    console.expect_exact("persistence: done")
    snapshot = board.flash()
    nvs = snapshot.partition("nvs")
    assert len(nvs) == 0x6000
    assert nvs_values(nvs) == snapshot.nvs()
    assert nvs == snapshot.contents[0x9000:0xF000]
    # the app the runner flashed, at its offset
    factory = snapshot.partition("factory")
    app = next(f for f in board.app.flash_files if f.offset == 0x10000)
    assert factory.startswith(app.path.read_bytes())
    assert built == {
        f.offset: hashlib.sha256(f.path.read_bytes()).hexdigest() for f in board.app.flash_files
    }


def test_the_flash_cannot_be_read_before_power_on_and_says_what_it_refuses(
    board: Esp32Sim, console: Console
) -> None:
    with pytest.raises(BoardOff) as off:
        board.flash()
    assert str(off.value) == (
        "board.flash: read the flash after board.power_on(): until then it holds the build and the"
        " test's NVS"
    )
    board.power_on()
    snapshot = board.flash()
    with pytest.raises(ValueError, match=r"^the build has no partition 'x': it has nvs, phy_init"):
        snapshot.partition("x")
    with pytest.raises(ValueError, match=r"^the build has no partition 'x': it has nvs, phy_init"):
        snapshot.nvs("x")
    with pytest.raises(ValueError) as refused:
        snapshot.nvs("factory")
    assert str(refused.value) == "board.flash: partition 'factory' is not unencrypted NVS"
    with pytest.raises(ValueError, match=r"^the build has no OTA data partition: it has nvs"):
        snapshot.ota()


def test_a_run_that_left_no_flash_says_so_and_a_read_of_a_turn_leaves_no_file(
    board: Esp32Sim, console: Console, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(esp32sim, "HELD_MAX_SECONDS", 1)
    board.power_on()
    console.expect_exact("persistence: done")
    board.flash()
    # what each read of a turn removes: what is there after the end comes from the end alone
    assert not board._flash_out.exists()
    with pytest.raises(RunEnded):
        board.wait(seconds(2))
    board._flash_out.unlink()
    with pytest.raises(BackendError, match=r"board\.flash: the run left no flash to read"):
        board.flash()
