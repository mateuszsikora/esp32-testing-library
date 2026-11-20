"""`board.nvs` on firmware/persistence, which counts its boots in NVS and prints the `note` the test
wrote beside the count: keys merged over what the build or the firmware left, and written while
the board is off between two boots. Needs `make firmware` (persistence)."""

import pytest

from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.flash import u32
from esp32tl.interfaces import BoardOff
from esp32tl.interfaces.console import Console
from esp32tl.units import seconds

pytestmark = pytest.mark.app_path("../firmware/persistence")


def test_a_merge_after_a_power_off_keeps_the_firmware_s_count_and_adds_the_test_s_key(
    board: Esp32Sim, console: Console
) -> None:
    board.power_on()
    console.expect_exact("persistence: boot=2 ")
    console.expect_exact("persistence: done")
    board.power_off()
    # in the instant the supply went off, the board's time not moving
    now = board.now
    board.nvs({"persistence": {"note": "from the test"}}, merge=True)
    assert board.now == now
    assert board.flash().nvs() == {"persistence": {"boots": u32(2), "note": "from the test"}}
    board.power_on()
    console.expect_exact("persistence: boot=3 ")
    console.expect_exact("persistence: note=from the test")
    console.expect_exact("persistence: done")
    assert "board.nvs(partition='nvs', merge=True)" in board.report(run=True)


def test_a_merge_while_the_board_stays_off_changes_a_setting_between_two_boots(
    board: Esp32Sim, console: Console
) -> None:
    board.nvs({"persistence": {"note": "first"}})
    board.power_on()
    console.expect_exact("persistence: note=first")
    console.expect_exact("persistence: done")
    board.power_off()
    board.wait(seconds(1))
    board.nvs({"persistence": {"note": "second"}}, merge=True)
    board.wait(seconds(1))
    board.power_on()
    console.expect_exact("persistence: boot=3 ")
    console.expect_exact("persistence: note=second")
    console.expect_exact("persistence: done")


def test_a_write_after_a_power_off_without_merge_replaces_the_partition(
    board: Esp32Sim, console: Console
) -> None:
    board.power_on()
    console.expect_exact("persistence: done")
    board.power_off()
    board.nvs({"persistence": {"note": "alone"}})
    assert board.flash().nvs() == {"persistence": {"note": "alone"}}
    board.power_on()
    # the count is gone with the rest: the firmware starts it again, and restarts once
    console.expect_exact("persistence: boot=1 ")
    console.expect_exact("persistence: note=alone")
    console.expect_exact("persistence: restarting")
    console.expect_exact("persistence: boot=2 ")
    console.expect_exact("persistence: done")


def test_a_merge_before_power_on_goes_over_the_test_s_earlier_keys(
    board: Esp32Sim, console: Console
) -> None:
    board.nvs({"persistence": {"boots": u32(10)}})
    board.nvs({"persistence": {"note": "merged"}}, merge=True)
    board.power_on()
    console.expect_exact("persistence: boot=11 ")
    console.expect_exact("persistence: note=merged")
    console.expect_exact("persistence: done")


def test_the_nvs_of_a_board_that_is_on_is_refused(board: Esp32Sim, console: Console) -> None:
    board.power_on()
    console.expect_exact("persistence: done")
    for merge in (False, True):
        with pytest.raises(BoardOff) as on:
            board.nvs({"persistence": {"note": "late"}}, merge=merge)
        assert str(on.value) == (
            "board.nvs: the board is on: call board.power_off() first, and write the flash while"
            " it is off"
        )
    assert board.flash().nvs() == {"persistence": {"boots": u32(2)}}
