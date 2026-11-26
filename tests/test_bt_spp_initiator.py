"""ESP-IDF's `bt_spp_initiator`, unmodified, against an `SppPeer` through the `bt` fixture: the
firmware's Bluedroid finds the peer by its name, pairs with its PIN, opens SPP and streams to it,
with the peer's `secure=True`, the default. A peer of another PIN fails the authentication, and
with no peer the firmware's one inquiry runs its length and finds nothing. A test that never looks
at `board.bluetooth` fails, naming it, at the firmware's first call to its controller. The same
example built with its HCI buffers in PSRAM (bt_spp_initiator_psram) pairs and streams as well.
Needs `make firmware` (bt_spp_initiator, bt_spp_initiator_psram). On the serial backend the `bt`
fixture skips these."""

import json
import re
from collections.abc import Iterable
from pathlib import Path

import pytest
from bumble import hci

from esp32tl import recording
from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.bluetooth.peer import BluetoothDevice, SppPeer
from esp32tl.interfaces.bluetooth import LATENCY_NS, Bluetooth
from esp32tl.interfaces.console import Console
from esp32tl.report import board_time
from esp32tl.sources.model import Model, Timed
from esp32tl.units import Duration, seconds

pytestmark = pytest.mark.app_path("../firmware/bt_spp_initiator")

FIRMWARE = Path(__file__).parent.parent / "firmware" / "bt_spp_initiator"

# CONFIG_EXAMPLE_PEER_DEVICE_NAME, and the PIN main.c answers ESP_BT_GAP_PIN_REQ_EVT with.
NAME = "ESP_SPP_ACCEPTOR"
PIN = "1234"
# What each esp_spp_write sends: spp_data, ESP_SPP_MAX_MTU bytes, spp_data[i] = i.
BLOCK = bytes(i & 0xFF for i in range(990))
# Bluedroid's inquiry: inq_len 30 in main.c, in units of 1.28 s.
INQUIRY = seconds(30 * 1.28)


class Heard(Model):
    """A source that answers nothing and keeps what the firmware writes."""

    def __init__(self) -> None:
        self.heard: list[tuple[Duration, bytes]] = []

    def receive(self, at: Duration, data: bytes) -> Iterable[Timed]:
        self.heard.append((at, data))
        return ()

    def poll(self, now: Duration) -> Iterable[Timed]:
        return ()

    def quiet_until(self) -> Duration | None:
        return None

    def latency(self) -> Duration | None:
        return None

    def between(self, start: Duration, end: Duration) -> int:
        """The bytes heard from `start` to `end`, both included."""
        return sum(len(data) for at, data in self.heard if start <= at <= end)


def test_the_firmware_finds_pairs_with_and_streams_to_the_peer(
    board: Esp32Sim, bt: Bluetooth, console: Console
) -> None:
    source = Heard()
    peer = SppPeer(bt.air, name=NAME, pin=PIN, source=source)
    assert peer.secure
    # made before power-on, so that it sees the line whatever turn brings it
    opening = console.line(r"ESP_SPP_OPEN_EVT handle:\d+ rem_bda:\[00:00:00:00:5b:01\]")
    board.power_on()
    console.expect_exact("ESP_SPP_DISCOVERY_COMP_EVT scn_num:1", timeout=seconds(5))
    console.expect_exact("-- [0] scn:1 service_name:Serial Port", timeout=1)
    console.expect_exact(f"Input pin code: {PIN}", timeout=1)
    console.expect_exact(f"authentication success: {NAME}", timeout=1)
    opened = board.wait_until(opening, within=seconds(1))
    # The peer checked at SABM, after the authentication, and let the channel open.
    assert peer.channel_open and peer.refused == []

    # The example measures what it wrote every 3 s or more, from its ESP_SPP_OPEN_EVT.
    speed = board.wait_until(
        console.line(r"speed\(([\d.]+)s ~ ([\d.]+)s\): ([\d.]+) kbit/s"), within=seconds(5)
    )
    start, end, kbit_s = (float(speed[group]) for group in (1, 2, 3))
    # What it counted reached the peer, in its blocks, as they were written.
    written = kbit_s * 1000 * (end - start) / 8
    heard = source.between(opened.at, speed.at)
    assert abs(heard - written) <= len(BLOCK), (heard, written)
    stream = b"".join(data for _, data in source.heard)
    # 3 s or more at about 299 kbit/s is about 113 blocks
    assert len(stream) >= 100 * len(BLOCK)
    assert stream == (BLOCK * (len(stream) // len(BLOCK) + 1))[: len(stream)]
    assert bt.unknown_commands() == []
    # esp32sim's note of the mode-2 enable, which the runner keeps from esp32sim and prints at
    # the end of the slice, is in the runner's output as esp32sim printed it, and ends nothing
    assert board.process is not None
    output = Path(board.process._logfile).read_text(errors="replace")
    assert (
        "[ble] the virtual controller is LE-only: enabled in mode 2 (BR/EDR), its commands get"
        " Unknown HCI Command" in output
    )


@pytest.mark.app_path("../firmware/bt_spp_initiator_psram")
@pytest.mark.board_profile("lilygo-t-a7670g-r2")
def test_with_its_hci_buffers_in_psram_the_firmware_pairs_with_and_streams_to_the_peer(
    board: Esp32Sim, bt: Bluetooth, console: Console
) -> None:
    # CONFIG_BT_ALLOCATION_FROM_SPIRAM_FIRST: every HCI packet Bluedroid sends is in PSRAM, which
    # the profile's psram_mb = 4 gives the chip.
    source = Heard()
    peer = SppPeer(bt.air, name=NAME, pin=PIN, source=source)
    opening = console.line(r"ESP_SPP_OPEN_EVT handle:\d+ rem_bda:\[00:00:00:00:5b:01\]")
    board.power_on()
    console.expect_exact(f"authentication success: {NAME}", timeout=seconds(5))
    board.wait_until(opening, within=seconds(1))
    board.wait_until(console.line(r"speed\("), within=seconds(5))
    assert peer.channel_open
    stream = b"".join(data for _, data in source.heard)
    assert len(stream) >= len(BLOCK)
    assert stream == (BLOCK * (len(stream) // len(BLOCK) + 1))[: len(stream)]
    assert bt.packets_from_host > 0 and bt.packets_to_host > 0
    assert bt.unknown_commands() == []


def test_a_peer_with_another_pin_fails_the_authentication(
    board: Esp32Sim, bt: Bluetooth, console: Console
) -> None:
    source = Heard()
    peer = SppPeer(bt.air, name=NAME, pin="0000", source=source)
    board.power_on()
    console.expect_exact(f"Input pin code: {PIN}", timeout=seconds(5))
    # HCI's AUTHENTICATION_FAILURE (0x05), and ESP_BT_STATUS_AUTH_FAILURE (9) to the example.
    console.expect_exact("Authentication fail reason 5", timeout=1)
    console.expect_exact("authentication failed, status:9", timeout=1)
    console.expect(r"ESP_SPP_CLOSE_EVT status:0 handle:\d+ close_by_remote:1", timeout=1)
    board.wait(seconds(2))
    assert console.lines("ESP_SPP_OPEN_EVT") == []
    assert not peer.channel_open and source.heard == []
    # The link fell before the firmware asked for the channel: nothing was refused at SABM.
    assert peer.refused == []


def test_a_peer_with_simple_pairing_and_no_display_pairs_by_just_works_and_streams(
    board: Esp32Sim, bt: Bluetooth, console: Console
) -> None:
    source = Heard()
    peer = SppPeer(bt.air, name=NAME, pin=PIN, source=source, io_capability="no_input_no_output")
    opening = console.line(r"ESP_SPP_OPEN_EVT handle:\d+ rem_bda:\[00:00:00:00:5b:01\]")
    board.power_on()
    console.expect_exact(f"authentication success: {NAME}", timeout=seconds(5))
    board.wait_until(opening, within=seconds(1))
    board.wait_until(console.line(r"speed\("), within=seconds(5))
    # Bluedroid confirms Just Works by itself: the example prints neither a PIN nor a number.
    assert console.lines(re.compile("Input pin code|ESP_BT_GAP_CFM_REQ_EVT|KEY_REQ")) == []
    [theirs] = peer.pairings
    [mine] = bt.pairings()
    assert (theirs.method, theirs.initiator, theirs.peer, theirs.authenticated) == (
        "just_works",
        False,
        "24:6F:28:00:11:24",
        False,
    )
    assert theirs.succeeded and mine.succeeded and mine.initiator
    assert (mine.method, mine.number, mine.t) == ("just_works", theirs.number, theirs.t)
    assert peer.channel_open and len(b"".join(data for _, data in source.heard)) > 0
    assert bt.unknown_commands() == []
    report = bt.report()
    assert report[1:3] == [
        "  pairings (1):",
        f"    {board_time(mine.t.ns)}  with 00:00:00:00:5B:01, by just_works,"
        f" {mine.number:06d}: succeeded",
    ]
    properties = bt.properties()
    assert properties is not None
    assert properties["pairings"] == [
        {
            "t_ns": mine.t.ns,
            "peer": "00:00:00:00:5B:01",
            "initiator": True,
            "method": "just_works",
            "number": mine.number,
            "status": 0,
            "authenticated": False,
        }
    ]


@pytest.mark.parametrize("display", ["display_only", "display_yes_no"])
def test_a_peer_that_shows_a_passkey_waits_for_the_firmware_s_user_until_bluedroid_gives_up(
    board: Esp32Sim, bt: Bluetooth, console: Console, display: str
) -> None:
    peer = SppPeer(bt.air, name=NAME, pin=PIN, source=Heard(), io_capability=display)
    board.power_on()
    # The example has a keyboard only, on its console, which the run cannot type on.
    asked = board.wait_until(
        console.line("ESP_BT_GAP_KEY_REQ_EVT Please enter passkey!"), within=seconds(5)
    )
    shown = peer.pairing
    assert shown is not None and shown.method == "passkey_entry" and shown.number is not None
    failed = board.wait_until(console.line("authentication failed, status:9"), within=seconds(40))
    # Bluedroid's security timer, 35 s, ends it with User_Passkey_Request_Negative_Reply.
    assert seconds(35) <= failed.at - asked.at <= seconds(35.1)
    [theirs] = peer.pairings
    [mine] = bt.pairings()
    assert (theirs.method, theirs.number) == ("passkey_entry", shown.number)
    assert (theirs.status, mine.status) == (5, 5)
    assert (mine.method, mine.number) == ("passkey_entry", None)
    assert not peer.channel_open
    assert bt.unknown_commands() == []


def test_the_tap_writes_the_hci_both_ways_as_btsnoop(pytester: pytest.Pytester) -> None:
    if not (FIRMWARE / "build" / "esp32tl-built").is_file():
        pytest.fail(f"{FIRMWARE}/build holds no finished build: run make firmware", pytrace=False)
    tapped = pytester.path / "hci.btsnoop"
    seen = pytester.path / "seen.json"
    pytester.makepyfile(
        test_tap=f"""
        import json

        import pytest

        from esp32tl.bluetooth.peer import SppPeer
        from esp32tl.units import seconds

        pytestmark = pytest.mark.app_path({str(FIRMWARE)!r})

        class Heard:
            def receive(self, t, data):
                return ()

            def poll(self, now):
                return ()

            def quiet_until(self):
                return None

            def latency(self):
                return 0

        def test_tapped(board, bt, console):
            SppPeer(bt.air, name={NAME!r}, pin={PIN!r}, source=Heard())
            tap = bt.tap({str(tapped)!r})
            board.power_on()
            board.wait_until(console.line("ESP_SPP_OPEN_EVT"), within=seconds(5))
            board.power_off()
            assert len(tap.events) == bt.packets_from_host + bt.packets_to_host
            assert "tapped into" in bt.report()[0]
            events = [(t, d, data.hex()) for t, d, data in tap.events]
            open({str(seen)!r}, "w").write(json.dumps(events))
        """
    )
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    # Written at teardown: every packet the tap saw, in time order, to the microsecond.
    hci = recording.read_btsnoop(tapped)
    events = [(e.t, e.dir, e.data) for e in hci.events]
    seen_events = sorted(json.loads(seen.read_text()), key=lambda event: event[0])
    assert events == [(t // 1000 * 1000, d, bytes.fromhex(data)) for t, d, data in seen_events]
    header = json.loads(recording.sidecar(tapped).read_text())
    assert (header["interface"], header["source"], header["clock"]) == (
        "bluetooth",
        "model:bluetooth",
        "emulated-fifo-write",
    )
    assert header["devices"] == [{"name": NAME, "address": "00:00:00:00:5B:01"}]
    assert re.fullmatch("[0-9a-f]{40}", header["tools"]["esp32sim"])
    # Bluedroid's first command is HCI_Reset, and its Command_Complete the first answer,
    # LATENCY_NS after it.
    first, answer = hci.events[0], next(e for e in hci.events if e.dir == "in")
    assert (first.dir, first.data) == ("out", bytes.fromhex("01030c00"))
    assert answer.data[:2] == b"\x04\x0e" and answer.data[4:6] == bytes.fromhex("030c")
    assert answer.t - first.t == LATENCY_NS
    # The firmware's PIN reached the controller (PIN_Code_Request_Reply), and the peer's name the
    # firmware.
    assert any(e.dir == "out" and e.data[1:3] == bytes.fromhex("0d04") for e in hci.events)
    assert any(e.dir == "in" and NAME.encode() in e.data for e in hci.events)


def test_a_test_that_gives_the_board_no_bluetooth_fails_naming_board_bluetooth(
    pytester: pytest.Pytester,
) -> None:
    if not (FIRMWARE / "build" / "esp32tl-built").is_file():
        pytest.fail(f"{FIRMWARE}/build holds no finished build: run make firmware", pytrace=False)
    pytester.makepyfile(
        test_no_bt=f"""
        import pytest

        from esp32tl.units import seconds

        pytestmark = pytest.mark.app_path({str(FIRMWARE)!r})

        def test_without_bluetooth(board):
            board.power_on()
            try:
                board.wait(seconds(15))
            finally:
                print("boots:", len(board.boots))
        """
    )
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(failed=1)
    # Without --no-bt the firmware boot-looped in ESP-IDF's own controller, about 130 boots in
    # 15 s, and the test passed. Now the runner ends the run in the slice of its call, once.
    result.stdout.fnmatch_lines(
        [
            "*RunEnded: the run ended at 0.* s while the test waited until 15 s: the firmware"
            " started its Bluetooth controller (esp_bt_controller_init) by 0.* s, and the test"
            " gave the board no Bluetooth: look at board.bluetooth (or take the bt fixture) before"
            " board.power_on(); board.bluetooth.use() gives the firmware the harness's controller"
            " with no device on the air; the runner exited with code 5*",
            "boots: 1",
        ]
    )


@pytest.mark.parametrize("around", ["nobody", "a phone"])
def test_with_no_peer_the_inquiry_runs_its_length_and_opens_nothing(
    board: Esp32Sim, bt: Bluetooth, console: Console, around: str
) -> None:
    if around == "a phone":
        BluetoothDevice(bt.air, name="phone", class_of_device=0x5A020C)
    board.power_on()
    # The inquiry's start, then its end: the same event, asked again for the next.
    changed = console.line("ESP_BT_GAP_DISC_STATE_CHANGED_EVT")
    started = board.wait_until(changed, within=seconds(5))
    ended = board.wait_until(changed, within=INQUIRY + seconds(1))
    assert INQUIRY <= ended.at - started.at <= INQUIRY + seconds(0.1)
    # main.c logs each device it finds, and the name in its extended inquiry response.
    found = len(console.lines("ESP_BT_GAP_DISC_RES_EVT"))
    named = len(console.lines(r"SPP_INITIATOR_DEMO: phone$"))
    assert (found, named) == ((1, 1) if around == "a phone" else (0, 0))
    # main.c starts no other inquiry and pages nobody.
    board.wait(seconds(2))
    assert len(console.lines("ESP_BT_GAP_DISC_STATE_CHANGED_EVT")) == 2
    assert console.lines(re.compile("ESP_SPP_DISCOVERY_COMP_EVT|ESP_SPP_OPEN_EVT")) == []
    assert bt.unknown_commands() == []


def test_the_host_asks_its_controller_for_one_inquiry_then_pages_the_peer_it_found(
    board: Esp32Sim, bt: Bluetooth, console: Console, tmp_path: Path
) -> None:
    # The example of "Tapping the HCI" in docs/features/run-report-user.md, which spells out the
    # 30 of INQUIRY so that it reads on its own.
    peer = SppPeer(bt.air, name=NAME, pin=PIN, source=Heard())
    tap = bt.tap(tmp_path / "hci.btsnoop")
    board.power_on()
    board.wait_until(console.line("ESP_SPP_OPEN_EVT"), within=seconds(5))
    commands = [
        hci.HCI_Packet.from_bytes(data)
        for _, direction, data in tap.events
        if direction == "out" and data[0] == hci.HCI_COMMAND_PACKET
    ]
    [inquiry] = [c for c in commands if isinstance(c, hci.HCI_Inquiry_Command)]
    assert inquiry.inquiry_length == 30  # inq_len in main.c, in units of 1.28 s
    [page] = [c for c in commands if isinstance(c, hci.HCI_Create_Connection_Command)]
    assert str(page.bd_addr).removesuffix("/P") == peer.address
    assert commands.index(inquiry) < commands.index(page)
