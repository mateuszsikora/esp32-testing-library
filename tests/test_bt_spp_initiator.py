"""ESP-IDF's `bt_spp_initiator`, unmodified, against an `SppPeer` through the `bt` fixture: the
firmware's Bluedroid finds the peer by its name, pairs with its PIN, opens SPP and streams to it,
with the peer's `secure=True`, the default. A peer of another PIN fails the authentication, and
with no peer the firmware's one inquiry runs its length and finds nothing. Needs `make firmware`
(bt_spp_initiator). On the serial backend the `bt` fixture skips these."""

import re
from collections.abc import Iterable

import pytest

from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.bluetooth.peer import BluetoothDevice, SppPeer
from esp32tl.interfaces.bluetooth import Bluetooth
from esp32tl.interfaces.console import Console
from esp32tl.units import seconds

pytestmark = pytest.mark.app_path("../firmware/bt_spp_initiator")

# CONFIG_EXAMPLE_PEER_DEVICE_NAME, and the PIN main.c answers ESP_BT_GAP_PIN_REQ_EVT with.
NAME = "ESP_SPP_ACCEPTOR"
PIN = "1234"
# What each esp_spp_write sends: spp_data, ESP_SPP_MAX_MTU bytes, spp_data[i] = i.
BLOCK = bytes(i & 0xFF for i in range(990))
# Bluedroid's inquiry: inq_len 30 in main.c, in units of 1.28 s.
INQUIRY = seconds(30 * 1.28)


class Heard:
    """A source that answers nothing and keeps what the firmware writes."""

    def __init__(self) -> None:
        self.heard: list[tuple[int, bytes]] = []

    def receive(self, t: int, data: bytes) -> Iterable[tuple[int, bytes]]:
        self.heard.append((t, data))
        return ()

    def poll(self, now: int) -> Iterable[tuple[int, bytes]]:
        return ()

    def quiet_until(self) -> int | None:
        return None

    def latency(self) -> int:
        return 0

    def between(self, start: int, end: int) -> int:
        """The bytes heard from `start` to `end`, nanoseconds, both included."""
        return sum(len(data) for t, data in self.heard if start <= t <= end)


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
    heard = source.between(opened.at.ns, speed.at.ns)
    assert abs(heard - written) <= len(BLOCK), (heard, written)
    stream = b"".join(data for _, data in source.heard)
    # 3 s or more at about 299 kbit/s is about 113 blocks
    assert len(stream) >= 100 * len(BLOCK)
    assert stream == (BLOCK * (len(stream) // len(BLOCK) + 1))[: len(stream)]
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
