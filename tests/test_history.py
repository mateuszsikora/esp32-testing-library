"""Public wire histories are live, read-only sequences."""

from collections.abc import Iterator
from pathlib import Path
from threading import Lock
from typing import TypeVar
from unittest.mock import Mock

import pytest
from conftest import CountingList
from test_boardlink import feed, started
from test_flash import device as device
from test_modem_model import route
from test_sd_faults import ROUTE
from test_spp_peer import Echo

from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.bluetooth.air import Air
from esp32tl.bluetooth.le import BleCentral, BlePeripheral
from esp32tl.bluetooth.peer import SppPeer
from esp32tl.interfaces.history import History
from esp32tl.interfaces.sd_card import SdCard
from esp32tl.interfaces.wifi import Wifi
from esp32tl.link.protocol import Reset, Sleep, Sync, Uptime
from esp32tl.matching import Matching
from esp32tl.net import Internet
from esp32tl.net.client import TcpConnection
from esp32tl.net.http import HttpRequest
from esp32tl.net.mqtt import Delivery
from esp32tl.sources.model.modem import ModeChange, ModemPower
from esp32tl.tap import Tap
from esp32tl.units import Duration


def test_live_reads_and_snapshots() -> None:
    records = CountingList([1, 2])
    view = History(records)
    assert len(view) == 2 and view and records.reads == 0
    assert view[-1] == 2 and records.reads == 1
    iterator = iter(view)
    frozen = view[::-1]
    records.append(3)
    assert list(iterator) == [1, 2]
    assert isinstance(frozen, list) and frozen == [2, 1]
    assert view == [1, 2, 3] and view == (1, 2, 3)
    assert repr(view) == "[1, 2, 3]"
    assert view != bytes([1, 2, 3]) and History(["a"]) != "a"
    with pytest.raises(AttributeError):
        view.append(4)  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        view.clear()  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        view[0] = 9  # type: ignore[index]
    with pytest.raises(TypeError):
        hash(view)


def test_locked_reads() -> None:
    lock = Lock()

    def length() -> int:
        assert lock.locked()
        return 3

    def record(index: int) -> int:
        assert lock.locked()
        return index + 10

    view = History(length, record, lock=lock)
    assert len(view) == 3 and view[-2] == 11 and view[:] == [10, 11, 12]


def test_public_history_properties(device: Esp32Sim, tmp_path: Path) -> None:
    device.process = Mock()
    net = Internet(lambda: 0, link="station")
    air = Air()
    slot = SdCard(ROUTE, directory=tmp_path)
    modem, _, _ = route(tmp_path, [1])
    modem.signal(dbm=-72)
    tap = Tap("uart.modem", tmp_path / "tap.jsonl")
    matching = Matching(tap.recording(board="board", source="model", clock="emulated-fifo-write"))
    image = tmp_path / "card.img"
    image.write_bytes(bytes(512 * 1024))
    slot.insert(image)
    owners: list[tuple[object, str]] = [
        (slot.fail("read"), "hits"),
        (net, "events connections link_changes leases"),
        (net.link, "leases"),
        (net.dns, "questions asked"),
        (net.ntp, "exchanges"),
        (net.dhcp, "leases"),
        (
            net.broker,
            "messages clients connection_ends connection_attempts subscribe_requests deliveries",
        ),
        (net.http, "requests"),
        (net.http_client, "exchanges"),
        (TcpConnection("host", 80, "local", "test", lambda: 0, lambda d, b: None), "syns"),
        (Wifi(now=lambda: 0), "events"),
        (
            BlePeripheral(air, name="sensor"),
            "events connections disconnections pairings writes subscriptions notifications",
        ),
        (
            BleCentral(air),
            "events connections disconnections pairings advertisements notifications",
        ),
        (SppPeer(air, name="peer", pin="1234", source=Echo()), "events refused pairings"),
        (device, "boots sleeps"),
        (modem.modem_model(), "restarts commands power_changes mode_changes data_received"),
        (modem, "restarts commands power_changes mode_changes"),
        (slot, "changes refused faulted"),
        (tap, "events"),
        (matching, "unseen_events"),
    ]
    for owner, names in owners:
        for name in names.split():
            assert isinstance(getattr(owner, name), History), (type(owner), name)
            with pytest.raises(AttributeError):
                setattr(owner, name, [])


def test_board_views_and_incremental_polls(device: Esp32Sim) -> None:
    device.process = Mock()
    device.link = link = started()
    link.resets = resets = CountingList[Reset]()
    link.sleep_boots = sleep_boots = CountingList[int]()
    boots, sleeps = device.boots, device.sleeps
    for t in range(1, 5):
        feed(link, Sleep(t, t, t, ("timer",)), Reset(t, t, 5, 0), Sync(t))
    next_boot, next_sleep = device.next_boot(), device.next_sleep()
    assert next_boot() is None and next_sleep() is None
    assert resets.reads == sleep_boots.reads == 0
    feed(link, Sleep(5, 5, 5, ("ext0",)), Reset(5, 5, 5, 0), Sync(5))
    assert next_boot() is not None and next_sleep() is not None
    assert resets.reads == sleep_boots.reads == 1
    assert boots[-1].number == 6 and sleeps[-1].boot == 5
    assert boots[-1].uptime_zero is None
    feed(link, Uptime(8, 6, 8), Sync(8))
    assert boots[-1].uptime_zero is not None


def test_modem_views_and_incremental_polls(tmp_path: Path) -> None:
    modem, _, _ = route(tmp_path, [1])
    modem.signal(dbm=-73)
    model = modem.modem_model()
    model._power_changes = powers = CountingList([ModemPower(Duration(0), True, "test")])
    model._mode_changes = modes = CountingList([ModeChange(Duration(0), "command", "drop")])
    power, mode = modem.next_power_change(False), modem.next_mode_change("data")
    assert power() is None and mode() is None and powers.reads == modes.reads == 0
    history = model.power_changes
    model.power_off()
    assert power() is not None and powers.reads == 1
    assert history[-1].powered is False
    for _ in range(5):
        modes.append(ModeChange(Duration(1), "command", "no carrier"))
        assert mode() is None
    assert modes.reads == 5


def test_reading_an_event_keeps_the_transcript_and_report_intact() -> None:
    network = Internet(lambda: 0)
    events = network.events
    network.log("connected")
    events[0].clear()
    events[:][0]["event"] = "changed"
    next(iter(events)).pop("t_ns")
    assert network.transcript() == {
        "esp32tl_network": 1,
        "events": [{"t_ns": 0, "event": "connected"}],
    }
    assert "  0.000000000 s  connected" in network.report()


T = TypeVar("T")


class CountVisited(CountingList[T]):
    """Count only the indices an iterator actually visits, including a partial scan."""

    def __iter__(self) -> Iterator[T]:
        return (self[i] for i in range(len(self)))


def test_first_match_queries_stop_reading_after_the_match() -> None:
    network = Internet(lambda: 0)
    request = HttpRequest(Duration(0), "GET", "/", "/", "HTTP/1.1", (), b"")
    network.http._requests = requests = CountVisited([request] * 1000)
    assert network.http.find_request("/") is request
    assert requests.reads == 1

    delivery = Delivery(Duration(0), "client", 1, "topic", b"data", 0, None, False, False)
    network.broker._deliveries = deliveries = CountVisited([delivery] * 1000)
    assert network.broker.find_delivery("topic") is delivery
    assert deliveries.reads == 1
