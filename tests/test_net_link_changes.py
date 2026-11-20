"""`network.link_changes`: the link coming up and going down, behind a modem and behind a station,
with hand-made PPP frames and data-link transitions, without an emulator."""

from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import CountingList
from test_modem_model import reply, route
from test_net_ppp import control

from esp32tl import LinkChange
from esp32tl.net import Internet
from esp32tl.sources.model.modem import Modem
from esp32tl.units import Duration

MS = 1_000_000
LCP, IPCP = 0xC021, 0x8021
TERM_REQ = 5


def open_ppp(net: Internet, t: int) -> None:
    """The device opens LCP and IPCP at `t`, acknowledging what the far end asked last."""
    net.poll(t)
    net.receive(t, control(LCP, 1, 7))
    net.receive(t, control(LCP, 2, net.ppp._requests[LCP][0]))
    ipcp = net.ppp._requests[IPCP]
    net.receive(t, control(IPCP, 2, ipcp[0], ipcp[1]))
    net.receive(t, control(IPCP, 1, 8, b"\x03\x06\x0a\x40\0\x02"))
    assert net.ppp.ipcp_open


def opened() -> Internet:
    net = Internet(lambda: 0)
    net.start(0)
    open_ppp(net, 0)
    return net


def changes(net: Internet) -> list[tuple[int, bool, str]]:
    return [(change.at.ns, change.up, change.cause) for change in net.link_changes]


def test_ipcp_open_is_up_and_the_devices_terminate_request_is_down() -> None:
    net = opened()
    assert changes(net) == [(0, True, "IPCP open")]
    net.receive(5 * MS, control(LCP, TERM_REQ, 3))
    assert changes(net)[1:] == [(5 * MS, False, "device terminated")]
    # The call that ends after it is no second down.
    net.stop(7 * MS)
    assert len(net.link_changes) == 2
    net.start(9 * MS)
    open_ppp(net, 9 * MS)
    assert changes(net)[2:] == [(9 * MS, True, "IPCP open")]
    assert all(isinstance(change, LinkChange) for change in net.link_changes)


@pytest.mark.parametrize(
    ("packet", "cause"),
    [
        (control(IPCP, TERM_REQ, 3), "device terminated"),
        (control(LCP, 1, 9), "device renegotiated LCP"),
        (control(IPCP, 1, 9, b"\x03\x06\x0a\x40\0\x02"), "device renegotiated IPCP"),
    ],
    ids=["ipcp-terminate", "lcp-renegotiated", "ipcp-renegotiated"],
)
def test_what_the_device_does_to_an_open_link_names_its_down(packet: bytes, cause: str) -> None:
    net = opened()
    net.receive(4 * MS, packet)
    assert changes(net)[1:] == [(4 * MS, False, cause)]


def test_a_link_that_never_opened_ipcp_records_nothing() -> None:
    net = Internet(lambda: 0)
    net.start(0)
    net.receive(MS, control(LCP, 1, 7))
    net.receive(MS, control(LCP, 2, 1))
    assert net.ppp.lcp_open and not net.ppp.ipcp_open
    net.receive(2 * MS, control(LCP, TERM_REQ, 3))
    net.stop(3 * MS)
    assert net.link_changes == []


def test_a_device_reset_is_down_and_its_renegotiation_no_second_one() -> None:
    net = opened()
    net.device_reset(3 * MS, "the device reset")
    assert changes(net)[1:] == [(3 * MS, False, "device reset")]
    # The new firmware opens LCP and IPCP again on the same call.
    open_ppp(net, 6 * MS)
    assert changes(net)[2:] == [(6 * MS, True, "IPCP open")]
    net.device_reset(8 * MS, "the device lost its power")
    assert changes(net)[3:] == [(8 * MS, False, "device lost its power")]


def dialled(tmp_path: Path, now: list[int]) -> tuple[Modem, Internet]:
    """A modem model that has dialled, behind which PPP has opened IPCP at `now`."""
    modem, _, _ = route(tmp_path, now)
    network = modem.network()
    model = modem.modem_model()
    assert reply(model, "ATD*99#", now[0]) == b"\r\nCONNECT\r\n"
    now[0] += MS
    open_ppp(network, now[0])
    assert changes(network) == [(now[0], True, "IPCP open")]
    now[0] += MS
    return model, network


def escape(model: Modem, now: list[int]) -> None:
    now[0] += 2_000 * MS
    model.receive(now[0], b"+++")
    now[0] += 1_000 * MS
    list(model.poll(now[0]))


@pytest.mark.parametrize(
    ("act", "cause"),
    [
        (lambda model, now: model.drop_call(), "call ended"),
        (lambda model, now: model.drop_call(terminate=True), "far end terminated"),
        (lambda model, now: model.hang(), "modem hung"),
        (lambda model, now: model.lose_registration(), "coverage lost"),
        (lambda model, now: model.power_off(), "modem off"),
        (lambda model, now: model.lose_power(), "modem lost power"),
        (lambda model, now: model.supply(False), "modem lost power"),
        (lambda model, now: model.power_on(), "modem restarted"),
        (escape, "device left data mode"),
    ],
    ids=[
        "drop-call",
        "drop-call-terminate",
        "hang",
        "lose-registration",
        "power-off",
        "lose-power",
        "board-supply",
        "restart",
        "escape",
    ],
)
def test_each_way_the_modem_ends_the_call_names_its_down(
    tmp_path: Path, act: Callable[[Modem, list[int]], None], cause: str
) -> None:
    now = [MS * 1000]
    model, network = dialled(tmp_path, now)
    act(model, now)
    assert not model.data_mode
    assert changes(network)[1:] == [(now[0], False, cause)]


def test_a_dropped_call_and_a_redial_give_a_down_and_an_up(tmp_path: Path) -> None:
    now = [MS * 1000]
    model, network = dialled(tmp_path, now)
    up = network.next_link_up()
    down = network.next_link_down()
    assert up() is None and down() is None
    now[0] = 1_500 * MS
    model.drop_call()
    assert down() == LinkChange(Duration(1_500 * MS), False, "call ended")
    assert up() is None
    now[0] = 9_000 * MS
    assert reply(model, "ATD*99#", now[0]) == b"\r\nCONNECT\r\n"
    open_ppp(network, 9_001 * MS)
    assert up() == LinkChange(Duration(9_001 * MS), True, "IPCP open")
    # Non-consuming: the same change at each call, and a later down does not move them.
    now[0] = 12_000 * MS
    model.drop_call()
    assert down() == LinkChange(Duration(1_500 * MS), False, "call ended")
    assert up() is up() and network.next_link_down()() is None


def test_the_conditions_are_described_and_refuse_bool() -> None:
    net = opened()
    up, down = net.next_link_up(), net.next_link_down()
    assert (repr(up), repr(down)) == ("the next link up", "the next link down")
    with pytest.raises(TypeError, match=r"call the network condition with \(\)"):
        bool(up)


def test_a_station_joining_and_leaving_is_up_and_down() -> None:
    net = Internet(lambda: 0, link="station")
    net.start(0)
    assert net.link_changes == []
    up = net.next_link_up()
    net._station_link(15 * MS, True)
    assert up() == LinkChange(Duration(15 * MS), True, "station joined")
    # A second ready while joined is a join counted again: it left and joined within a slice.
    net._station_link(16 * MS, True)
    down = net.next_link_down()
    net._station_link(20 * MS, False)
    net._station_link(21 * MS, False)
    assert down() == LinkChange(Duration(20 * MS), False, "station left")
    net._station_link(30 * MS, True)
    net.device_reset(40 * MS, "the device reset")
    net._station_link(41 * MS, False)
    assert changes(net) == [
        (15 * MS, True, "station joined"),
        (16 * MS, False, "station left"),
        (16 * MS, True, "station joined"),
        (20 * MS, False, "station left"),
        (30 * MS, True, "station joined"),
        (40 * MS, False, "device reset"),
    ]


def test_behind_an_access_point_nothing_is_recorded_and_the_conditions_refuse() -> None:
    net = Internet(lambda: 0, link="ethernet")
    net.start(0)
    net.device_reset(MS, "the device reset")
    net.stop(2 * MS)
    assert net.link_changes == []
    with pytest.raises(
        ValueError,
        match=r"^network.next_link_up: behind a Wi-Fi access point the network does not see the"
        r" device join or leave; wait for network.dhcp.next_lease\(\)$",
    ):
        net.next_link_up()
    with pytest.raises(
        ValueError,
        match=r"^network.next_link_down: behind a Wi-Fi access point the network does not see"
        r" the device join or leave; the test's own switches are in wifi.events$",
    ):
        net.next_link_down()


def test_a_next_link_poll_reads_only_the_changes_since_the_last_one() -> None:
    net = Internet(lambda: 0, link="station")
    net.start(0)
    net.link_changes = history = CountingList[LinkChange]()
    net._station_link(MS, True)
    up = net.next_link_up()
    net._station_link(2 * MS, False)
    for _ in range(1_000):
        assert up() is None
    # The down once, at the first poll after it, and nothing at the polls after.
    assert history.reads == 1
    net._station_link(3 * MS, True)
    found = up()
    assert found == LinkChange(Duration(3 * MS), True, "station joined")
    for _ in range(1_000):
        assert up() is found
    # Then only the match, once a poll.
    assert history.reads == 2 + 1_000
