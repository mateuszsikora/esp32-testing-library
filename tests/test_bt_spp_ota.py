"""firmware/bt_spp_ota and bt_spp_ota_v2: the firmware's Bluetooth controller starts for an app
booted from ota_1 as it does from ota_0, at a power-on, a software reset and a deep-sleep wake, and
the app talks to an `SppPeer` from ota_1. bt_spp_ota copies its own image into ota_1; bt_spp_ota_v2
copies bt_spp_ota's, a build of another ELF, whose hooks the runner takes once the test names it
with `bluetooth.add_elf`. Needs `make firmware` (bt_spp_ota, bt_spp_ota_v2)."""

import re
import time
from pathlib import Path

import pytest
from test_bt_spp_initiator import BLOCK, NAME, PIN, Heard

from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.bluetooth.peer import SppPeer
from esp32tl.build import BuildError
from esp32tl.interfaces import SourceError
from esp32tl.interfaces.bluetooth import Bluetooth
from esp32tl.interfaces.console import Console
from esp32tl.units import seconds

pytestmark = pytest.mark.app_path("../firmware/bt_spp_ota")

FIRMWARE = Path(__file__).parent.parent / "firmware"
# bt_spp_ota's ELF, as bt_spp_ota_v2's build keeps it beside the image it flashes into update.
UPDATE_ELF = FIRMWARE / "bt_spp_ota_v2" / "build" / "update" / "bt_spp_ota.elf"


def streams_from_ota_1(board: Esp32Sim, console: Console, peer: SppPeer, source: Heard) -> None:
    """The boots of bt_spp_ota and bt_spp_ota_v2 from the copy to ota_1 on, the controller started
    in each, then SPP to `peer` from ota_1 after the deep-sleep wake."""
    console.expect_exact("bt_spp_ota: updated boot=ota_1", timeout=seconds(10))
    console.expect_exact("bt_spp_ota: controller enabled running=ota_0", timeout=seconds(1))
    console.expect_exact("bt_spp_ota: booted reason=sw running=ota_1", timeout=seconds(5))
    console.expect_exact("bt_spp_ota: controller enabled running=ota_1", timeout=seconds(1))
    console.expect_exact("bt_spp_ota: booted reason=deepsleep running=ota_1", timeout=seconds(5))
    console.expect_exact(f"authentication success: {NAME}", timeout=seconds(5))
    board.wait_until(console.line(r"speed\("), within=seconds(5))
    assert peer.channel_open
    stream = b"".join(data for _, data in source.heard)
    assert len(stream) >= len(BLOCK)
    assert stream == (BLOCK * (len(stream) // len(BLOCK) + 1))[: len(stream)]
    assert [boot.reason for boot in board.boots] == [
        "POWERON_RESET",
        "SW_CPU_RESET",
        "DEEPSLEEP_RESET",
    ]
    assert not re.search(r"abort|Guru Meditation", board.console_output.decode(errors="replace"))


def test_an_app_booted_from_ota_1_starts_its_controller_and_talks_to_the_peer(
    board: Esp32Sim, bt: Bluetooth, console: Console
) -> None:
    source = Heard()
    peer = SppPeer(bt.air, name=NAME, pin=PIN, source=source)
    board.power_on()
    console.expect_exact("bt_spp_ota: booted reason=poweron running=ota_0", timeout=seconds(5))
    streams_from_ota_1(board, console, peer, source)
    # ota_1 of the consumer's table, as the bootloader boots it
    assert "Loaded app from partition at offset 0x210000" in board.console_output.decode(
        errors="replace"
    )


@pytest.mark.app_path("../firmware/bt_spp_ota_v2")
def test_an_update_built_from_another_elf_gets_its_hooks_from_the_elf_the_test_names(
    board: Esp32Sim, bt: Bluetooth, console: Console
) -> None:
    source = Heard()
    peer = SppPeer(bt.air, name=NAME, pin=PIN, source=source)
    assert bt.add_elf(UPDATE_ELF) == UPDATE_ELF.resolve()
    assert bt.add_elf(UPDATE_ELF) == UPDATE_ELF.resolve()
    assert bt.elfs == [UPDATE_ELF.resolve()]
    board.power_on()
    console.expect_exact("bt_spp_ota: v2", timeout=seconds(5))
    console.expect_exact("bt_spp_ota: booted reason=poweron running=ota_0", timeout=seconds(1))
    streams_from_ota_1(board, console, peer, source)
    # bt_spp_ota from ota_1: the boots after the update are not v2's
    assert console.lines("bt_spp_ota: v2") == [console.lines("bt_spp_ota: v2")[0]]


@pytest.mark.app_path("../firmware/bt_spp_ota_v2")
def test_an_update_built_from_an_elf_the_test_does_not_name_gets_no_controller(
    board: Esp32Sim, bt: Bluetooth, console: Console
) -> None:
    board.power_on()
    console.expect_exact("bt_spp_ota: booted reason=sw running=ota_1", timeout=seconds(10))
    # ESP-IDF's own controller, which esp32sim does not model, panics and the app boots again
    console.expect_exact(
        "Guru Meditation Error: Core  0 panic'ed (StoreProhibited)", timeout=seconds(2)
    )
    console.expect_exact("bt_spp_ota: booted reason=other running=ota_1", timeout=seconds(5))
    assert console.lines("bt_spp_ota: controller enabled running=ota_1") == []
    # The runner says why, in the dut's log, which its output reaches a little after it wrote it.
    said = "none of the --bt ELFs: it has no Bluetooth controller; pass its ELF as another --bt"
    deadline = time.monotonic() + 10
    while said not in Path(board.dut.logfile).read_text(errors="replace"):
        assert time.monotonic() < deadline, f"the runner never said {said!r}"
        time.sleep(0.1)


def test_add_elf_takes_a_build_directory_s_elf_and_refuses_what_is_not_an_elf(
    board: Esp32Sim, bt: Bluetooth, console: Console, tmp_path: Path
) -> None:
    assert (
        bt.add_elf(FIRMWARE / "bt_spp_ota" / "build")
        == (FIRMWARE / "bt_spp_ota" / "build" / "bt_spp_ota.elf").resolve()
    )
    with pytest.raises(BuildError, match=r"^bluetooth.add_elf: "):
        bt.add_elf(tmp_path)
    text = tmp_path / "app.elf"
    text.write_text("not an ELF")
    with pytest.raises(ValueError, match=r"bluetooth.add_elf: .*app.elf is not an ELF file"):
        bt.add_elf(text)
    with pytest.raises(FileNotFoundError, match=r"bluetooth.add_elf: cannot read .*missing.elf"):
        bt.add_elf(tmp_path / "missing.elf")
    # the run reads an ELF of add_elf: a tap that would write over it is refused, either way round
    elf = tmp_path / "update.elf"
    elf.write_bytes((FIRMWARE / "bt_spp_ota" / "build" / "bt_spp_ota.elf").read_bytes())
    bt.add_elf(elf)
    with pytest.raises(
        SourceError, match=r"cannot tap into .*update.elf: .* is the elf of bluetooth"
    ):
        bt.tap(elf)
    snoop = tmp_path / "hci.btsnoop"
    snoop.write_bytes(b"\x7fELF")
    bt.tap(snoop)
    with pytest.raises(
        SourceError, match=r"bluetooth.add_elf: .*hci.btsnoop is the tap of bluetooth"
    ):
        bt.add_elf(snoop)
    # a tap needs a run, and the run takes the ELF
    board.power_on()
    console.expect_exact("bt_spp_ota: booted reason=poweron running=ota_0", timeout=seconds(5))
