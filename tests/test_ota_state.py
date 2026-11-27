"""firmware/ota_state through an update and a rollback: what the test reads of the OTA data matches
what the firmware reports, at every step. Needs `make firmware` (ota_state)."""

import re

import pytest

from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.flash import OtaData, OtaEntry
from esp32tl.interfaces.console import Console
from esp32tl.units import ms

pytestmark = pytest.mark.app_path("../firmware/ota_state")

REPORT = rb"ota_state: (\w+) running=(\w+) boot=(\w+) state=(\w+)"
ERASED = OtaEntry(0xFFFFFFFF, "undefined", False)


def reported(console: Console) -> tuple[str, str, str, str]:
    step, running, boot, state = console.expect(REPORT, timeout=30).groups()
    return step.decode(), running.decode(), boot.decode(), state.decode()


def test_the_ota_state_read_matches_what_the_firmware_reports(
    board: Esp32Sim, console: Console
) -> None:
    board.power_on()
    assert board.flash().ota() == OtaData("factory", (ERASED, ERASED))

    assert reported(console) == ("booted", "factory", "factory", "none")
    assert board.flash().ota() == OtaData("factory", (ERASED, ERASED))

    # copied to ota_0 and selected, the firmware has not restarted yet
    assert reported(console) == ("updated", "factory", "ota_0", "none")
    first = OtaData("ota_0", (OtaEntry(1, "new", True), ERASED))
    assert board.flash().ota() == first
    app = board.flash().partition("factory")
    assert board.flash().partition("ota_0")[:0x1000] == app[:0x1000]

    # the bootloader has marked the new app
    assert reported(console) == ("booted", "ota_0", "ota_0", "pending_verify")
    assert board.flash().ota().entries[0] == OtaEntry(1, "pending_verify", True)

    assert reported(console) == ("marked_valid", "ota_0", "ota_0", "valid")
    assert board.flash().ota() == OtaData("ota_0", (OtaEntry(1, "valid", True), ERASED))

    assert reported(console) == ("updated", "ota_0", "ota_1", "valid")
    assert board.flash().ota() == OtaData(
        "ota_1", (OtaEntry(1, "valid", True), OtaEntry(2, "new", True))
    )

    assert reported(console) == ("booted", "ota_1", "ota_1", "pending_verify")
    assert board.flash().ota() == OtaData(
        "ota_1", (OtaEntry(1, "valid", True), OtaEntry(2, "pending_verify", True))
    )

    # rolled back: the entry that failed no longer counts, and ota_0 boots again
    console.expect_exact("ota_state: rolling_back running=ota_1", timeout=30)
    assert reported(console) == ("booted", "ota_0", "ota_0", "valid")
    assert board.flash().ota() == OtaData(
        "ota_0", (OtaEntry(1, "valid", True), OtaEntry(2, "invalid", False))
    )
    assert reported(console) == ("done", "ota_0", "ota_0", "valid")
    assert [boot.reason for boot in board.boots][1:] == ["SW_CPU_RESET"] * (len(board.boots) - 1)
    assert not re.search(r"abort|Guru Meditation", board.console_output.decode(errors="replace"))


def test_a_power_cycle_before_the_app_confirms_itself_boots_the_slot_before_it(
    board: Esp32Sim, console: Console
) -> None:
    board.power_on()
    while reported(console) != ("booted", "ota_1", "ota_1", "pending_verify"):
        pass
    # what the OTA data selects now, as the firmware reads it too
    assert board.flash().ota() == OtaData(
        "ota_1", (OtaEntry(1, "valid", True), OtaEntry(2, "pending_verify", True))
    )
    board.power_off()
    count = len(board.boots)
    board.power_on()
    # within a millisecond of the reset the bootloader has not run: the entry is still pending
    while len(board.boots) == count:
        board.wait(ms(1))
    assert board.now - board.boot.at <= ms(1)
    assert board.flash().ota().entries[1] == OtaEntry(2, "pending_verify", True)
    # the bootloader aborts an entry still pending at its start, before it selects
    assert reported(console) == ("booted", "ota_0", "ota_0", "valid")
    assert board.flash().ota() == OtaData(
        "ota_0", (OtaEntry(1, "valid", True), OtaEntry(2, "aborted", False))
    )


def test_a_read_at_the_reset_sees_the_ota_data_the_boot_before_left(
    board: Esp32Sim, console: Console
) -> None:
    board.power_on()
    assert reported(console) == ("booted", "factory", "factory", "none")
    assert reported(console) == ("updated", "factory", "ota_0", "none")
    # 1 ms at a time, so the read comes within a millisecond of the reset
    while len(board.boots) < 2:
        board.wait(ms(1))
    assert board.now - board.boot.at <= ms(1)
    # the chip has reset, its bootloader has not run yet
    assert board.flash().ota().entries[0] == OtaEntry(1, "new", True)
    assert reported(console) == ("booted", "ota_0", "ota_0", "pending_verify")
    assert board.flash().ota().entries[0] == OtaEntry(1, "pending_verify", True)
