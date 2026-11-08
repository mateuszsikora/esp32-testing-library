"""The guest's Bluetooth controller (`esp32tl/bluetooth/guest.py`) and the interface that puts it on
the board's link (`esp32tl/interfaces/bluetooth.py`), without the emulator.

A Bumble host stands in for the firmware's Bluedroid. Its H4 packets reach the guest's controller
through a real `BoardLink`, in turns that `Runner` makes as the runner does (`docs/protocol.md`
§7): each turn ends at the earliest of the harness's `quiet_until` and the first event of the slice
plus the latency, and what the harness sends in a turn reaches the host at its time. The host has
an air of its own, on the same clock, so that its time and the board's are one."""

import asyncio
import threading
from collections.abc import Callable, Coroutine, Iterable, Iterator
from typing import Any, TypeVar

import pytest
from bumble import core, hci, rfcomm, sdp
from bumble.device import Connection, Device, DeviceConfiguration
from bumble.host import Host
from bumble.pairing import PairingConfig, PairingDelegate

from esp32tl.backends.clock import HeldClock
from esp32tl.bluetooth.air import Air, AirError
from esp32tl.bluetooth.controller import GIAC, Controller
from esp32tl.bluetooth.guest import ACL_LENGTH, ADDRESS, GuestController
from esp32tl.bluetooth.peer import SERVICE_NAME, BluetoothDevice, SppPeer
from esp32tl.boards.capabilities import Request
from esp32tl.interfaces import NeverHappened
from esp32tl.interfaces.bluetooth import LATENCY_NS, Bluetooth, BluetoothError
from esp32tl.link.boardlink import BoardLink
from esp32tl.link.protocol import (
    HCI_MAX,
    HciRx,
    HciTx,
    RunnerHello,
    Stop,
    Sync,
    Wait,
)
from esp32tl.units import Duration, seconds

T = TypeVar("T")
E = TypeVar("E", bound=hci.HCI_Event)

MS = 1_000_000
S = 1_000_000_000
HELLO = RunnerHello(1, "esp32", "emulated-fifo-write", "afcfba65da0da30a35fb0a7c381854c62b92e338")
PEER = "00:00:00:00:5B:01"


def public(address: str) -> hci.Address:
    return hci.Address(address, hci.Address.PUBLIC_DEVICE_ADDRESS)


GUEST = public(ADDRESS)


class Pin(PairingDelegate):
    def __init__(self, pin: str | None = "1234") -> None:
        super().__init__(io_capability=PairingDelegate.KEYBOARD_INPUT_ONLY)
        self.pin = pin

    async def get_string(self, max_length: int) -> str | None:
        return self.pin


class Echo:
    """A source that answers each write with `answer`, 1 ms later."""

    def __init__(self, answer: bytes = b"ok\r\n") -> None:
        self.answer = answer
        self.heard: list[tuple[int, bytes]] = []

    def receive(self, t: int, data: bytes) -> Iterable[tuple[int, bytes]]:
        self.heard.append((t, data))
        return [(t + MS, self.answer)] if self.answer else []

    def poll(self, now: int) -> Iterable[tuple[int, bytes]]:
        return ()

    def quiet_until(self) -> int | None:
        return None

    def latency(self) -> int | None:
        return MS


class Bluedroid:
    """The firmware's host: a Bumble `Device` on a `Host` whose packets go to the board's link.
    Every packet it is given is kept in `received`, and `drop` keeps one from it."""

    def __init__(self, pin: str | None = "1234") -> None:
        self.air = Air()
        self.sent: list[tuple[int, bytes]] = []
        self.received: list[tuple[int, hci.HCI_Packet]] = []
        self.drop: Callable[[hci.HCI_Packet], bool] = lambda packet: False
        self.delegate = Pin(pin)
        self.device = self.air.call(self._make)
        self.host = self.device.host

    def _make(self) -> Device:
        # Bumble's `TransportSource`: the host waits on it to end.
        self.terminated: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        config = DeviceConfiguration(
            name="ESP_SPP_INITIATOR",
            classic_enabled=True,
            le_enabled=False,
            # Bluedroid writes Simple_Pairing_Mode 1; the peer has none, so the two pair with a PIN.
            classic_ssp_enabled=True,
            classic_sc_enabled=False,
        )
        device = Device(config=config, host=Host(self, self))
        device.pairing_config_factory = lambda connection: PairingConfig(
            sc=False, mitm=False, bonding=True, delegate=self.delegate
        )
        return device

    # The host's transport.

    def set_packet_sink(self, sink: Any) -> None:
        pass

    def on_packet(self, packet: bytes) -> None:
        self.sent.append((self.air.loop.now, bytes(packet)))

    def deliver(self, data: bytes) -> None:
        packet = hci.HCI_Packet.from_bytes(data)
        self.received.append((self.air.loop.now, packet))
        if not self.drop(packet):
            self.air.call(lambda: self.host.on_packet(data))

    def take(self) -> list[tuple[int, bytes]]:
        sent, self.sent = self.sent, []
        return sent

    def events(self, kind: type[E]) -> list[E]:
        return [p for _, p in self.received if isinstance(p, kind)]


class Runner:
    """The runner's side of the link, for a guest whose host is `guest`, in a thread of its own:
    events at the time the host sent them, turns at the block points the harness allows, inputs
    delivered at their time. The link is held at every turn by a `HeldClock`, as the backend
    holds it, so the test acts at the turn's instant and moves time only by waiting."""

    def __init__(self, guest: Bluedroid, bt: Bluetooth) -> None:
        self.guest = guest
        self.bt = bt
        self.clock = HeldClock()
        self.link = BoardLink("esp32-devkitc", [bt], hold=self.clock)
        self.seq = 0
        self.rx: list[HciRx] = []
        self.failure: BaseException | None = None
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        try:
            self.link.receive(HELLO)
            messages = self.link.receive(Sync(0))
            while (wait := self._answer(messages)) is not None:
                messages = self.link.receive(Sync(self._slice(wait)))
        except BaseException as error:
            self.failure = error
        self.clock.end(f"the runner stopped: {self.link.error or self.failure!r}")

    def _answer(self, messages: list[Any]) -> Wait | None:
        assert self.link.error is None, self.link.error
        if messages == [Stop()]:
            return None
        *inputs, wait = messages
        assert isinstance(wait, Wait), messages
        for message in inputs:
            assert isinstance(message, HciRx)
            assert 1 <= len(message.data) <= HCI_MAX
            self.rx.append(message)
        self.rx.sort(key=lambda message: message.t)
        return wait

    def _slice(self, wait: Wait) -> int:
        """The guest runs from the turn to the next block point, sending its packets as events;
        returns the block point."""
        assert wait.quiet_until is not None
        first: list[int] = []

        def send() -> None:
            for t, data in self.guest.take():
                self.seq += 1
                assert self.link.receive(HciTx(self.seq, t, data)) == []
                first.append(t)

        def block() -> int:
            assert wait.quiet_until is not None
            if first and wait.latency is not None:
                return min(wait.quiet_until, first[0] + wait.latency)
            return wait.quiet_until

        send()
        while True:
            due = [self.guest.air.quiet_until(), *(m.t for m in self.rx[:1])]
            target = min([t for t in due if t is not None and t <= block()], default=block())
            self.guest.air.advance(Duration(target))
            while self.rx and self.rx[0].t <= target:
                self.guest.deliver(self.rx.pop(0).data)
            send()
            if target >= block():
                return block()

    @property
    def now(self) -> int:
        return self.clock.now

    def run(self, coroutine: Coroutine[Any, Any, T], within: int = 30 * S) -> T:
        """Runs the host's `coroutine` on its air until it completes, as the board runs."""
        task = self.guest.air.start(coroutine)
        try:
            self.clock.wait_until(lambda: task if task.done() else None, within, str(coroutine))
        except NeverHappened:
            self.guest.air.call(task.cancel)
            raise
        return task.result()

    def advance(self, until: int) -> None:
        self.clock.wait(until - self.now)

    def wait_for(self, condition: Callable[[], object], within: int = 30 * S) -> None:
        self.clock.wait_until(lambda: condition() or None, within, "the condition")

    def stop(self) -> None:
        self.clock.stop()
        self.thread.join(5)
        if self.failure is not None:
            raise self.failure


@pytest.fixture
def bt() -> Iterator[Bluetooth]:
    bt = Bluetooth()
    bt.use()
    yield bt
    bt.close()


@pytest.fixture
def guest() -> Iterator[Bluedroid]:
    guest = Bluedroid()
    yield guest
    guest.air.close()


@pytest.fixture
def runner(guest: Bluedroid, bt: Bluetooth) -> Iterator[Runner]:
    runner = Runner(guest, bt)
    runner.run(guest.device.power_on())
    yield runner
    runner.stop()


def spp_peer(bt: Bluetooth, **kwargs: Any) -> SppPeer:
    arguments: dict[str, Any] = {"name": "ESP_SPP_ACCEPTOR", "pin": "1234", "source": Echo()}
    return SppPeer(bt.air, **(arguments | kwargs))


def complete(runner: Runner, command: hci.HCI_Command) -> hci.HCI_Command_Complete_Event[Any]:
    """`command`'s `Command_Complete`, whatever its status."""
    event = runner.run(runner.guest.host.send_command(command))  # type: ignore[call-overload]
    assert isinstance(event, hci.HCI_Command_Complete_Event), event
    return event


def status_of(event: hci.HCI_Command_Complete_Event[Any]) -> int:
    parameters = event.return_parameters
    if isinstance(parameters, hci.HCI_StatusReturnParameters):
        return int(parameters.status)
    assert isinstance(parameters, bytes | hci.HCI_GenericReturnParameters)
    data = parameters if isinstance(parameters, bytes) else parameters.data
    return data[0]


async def link_to(guest: Bluedroid, address: str = PEER) -> Connection:
    return await guest.device.connect(address, transport=core.PhysicalTransport.BR_EDR)


# Power-on and what the controller says of itself.


def test_the_host_powers_on_through_the_link_and_reads_three_pages_of_features(
    runner: Runner,
) -> None:
    pages = [
        complete(runner, hci.HCI_Read_Local_Extended_Features_Command(page_number=page))
        for page in range(4)
    ]
    parameters = [event.return_parameters for event in pages]
    assert [p.status for p in parameters] == [
        0,
        0,
        0,
        hci.HCI_ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
    ]
    assert {p.maximum_page_number for p in parameters[:3]} == {2}
    page_0 = hci.LmpFeatureMask(int.from_bytes(parameters[0].extended_lmp_features, "little"))
    for feature in (
        hci.LmpFeatureMask.ENCRYPTION,
        hci.LmpFeatureMask.ROLE_SWITCH,
        hci.LmpFeatureMask.EXTENDED_INQUIRY_RESPONSE,
        hci.LmpFeatureMask.RSSI_WITH_INQUIRY_RESULTS,
        hci.LmpFeatureMask.SECURE_SIMPLE_PAIRING_CONTROLLER_SUPPORT,
        hci.LmpFeatureMask.EXTENDED_FEATURES,
    ):
        assert page_0 & feature, feature
    assert not page_0 & hci.LmpFeatureMask.BR_EDR_NOT_SUPPORTED
    assert not page_0 & hci.LmpFeatureMask.LE_SUPPORTED_CONTROLLER
    # The host's Simple_Pairing_Mode is on page 1.
    page_1 = int.from_bytes(parameters[1].extended_lmp_features, "little") << 64
    assert page_1 & hci.LmpFeatureMask.SECURE_SIMPLE_PAIRING_HOST_SUPPORT
    buffer = complete(runner, hci.HCI_Read_Buffer_Size_Command()).return_parameters
    assert buffer.hc_acl_data_packet_length == ACL_LENGTH == 491
    address = complete(runner, hci.HCI_Read_BD_ADDR_Command()).return_parameters
    assert address.bd_addr == GUEST
    assert runner.bt.unknown_commands() == []


def test_every_answer_comes_at_the_latency_and_no_turn_waits_longer(runner: Runner) -> None:
    sent = runner.now
    complete(runner, hci.HCI_Read_Local_Version_Information_Command())
    complete_at = runner.guest.received[-1][0]
    assert complete_at == sent + LATENCY_NS
    assert runner.bt.latency() == LATENCY_NS


# What Bluedroid sends that Bumble has no handler for.


@pytest.mark.parametrize(
    "command",
    [
        hci.HCI_Write_Default_Link_Policy_Settings_Command(default_link_policy_settings=0x0005),
        hci.HCI_Write_Inquiry_Scan_Activity_Command(
            inquiry_scan_interval=0x0800, inquiry_scan_window=0x0012
        ),
        hci.HCI_Write_Page_Scan_Activity_Command(
            page_scan_interval=0x0800, page_scan_window=0x0012
        ),
        hci.HCI_Write_Inquiry_Scan_Type_Command(scan_type=1),
        hci.HCI_Write_Page_Scan_Type_Command(page_scan_type=1),
        hci.HCI_Set_Event_Filter_Command(filter_type=0x01, filter_condition=bytes([0x00])),
        hci.HCI_Set_Event_Filter_Command(filter_type=0x00, filter_condition=b""),
        hci.HCI_Set_Event_Filter_Command(filter_type=0x02, filter_condition=bytes([0x00, 0x01])),
        hci.HCI_Command(
            op_code=hci.HCI_WRITE_CURRENT_IAC_LAP_COMMAND, parameters=bytes.fromhex("01338b9e")
        ),
    ],
    ids=lambda command: command.name,
)
def test_a_setting_bluedroid_writes_succeeds(runner: Runner, command: hci.HCI_Command) -> None:
    assert status_of(complete(runner, command)) == hci.HCI_ErrorCode.SUCCESS
    assert runner.bt.unknown_commands() == []


@pytest.mark.parametrize(
    ("command", "status"),
    [
        # Only devices of a class of device: an event filter, which is not modelled.
        (
            hci.HCI_Set_Event_Filter_Command(
                filter_type=0x01, filter_condition=bytes([0x01]) + bytes(6)
            ),
            hci.HCI_ErrorCode.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR,
        ),
        # Auto-accept, which is not modelled.
        (
            hci.HCI_Set_Event_Filter_Command(
                filter_type=0x02, filter_condition=bytes([0x00, 0x02])
            ),
            hci.HCI_ErrorCode.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR,
        ),
        # A LAP outside 0x9E8B00 to 0x9E8B3F, and a count that does not match.
        (
            hci.HCI_Command(
                op_code=hci.HCI_WRITE_CURRENT_IAC_LAP_COMMAND, parameters=bytes.fromhex("01000000")
            ),
            hci.HCI_ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
        ),
        (
            hci.HCI_Command(
                op_code=hci.HCI_WRITE_CURRENT_IAC_LAP_COMMAND, parameters=bytes.fromhex("02338b9e")
            ),
            hci.HCI_ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
        ),
    ],
)
def test_a_setting_that_would_change_what_is_not_modelled_is_refused(
    runner: Runner, command: hci.HCI_Command, status: int
) -> None:
    assert status_of(complete(runner, command)) == status


def test_host_number_of_completed_packets_gets_no_event(runner: Runner) -> None:
    before = len(runner.guest.received)
    command = hci.HCI_Command(
        op_code=hci.HCI_HOST_NUMBER_OF_COMPLETED_PACKETS_COMMAND,
        parameters=bytes.fromhex("0101000100"),
    )
    runner.guest.air.call(lambda: runner.guest.host.send_hci_packet(command))
    runner.advance(runner.now + 100 * MS)
    assert len(runner.guest.received) == before
    assert runner.bt.unknown_commands() == []


def test_an_inquiry_finds_only_devices_scanning_for_its_access_code(
    runner: Runner, bt: Bluetooth
) -> None:
    peer = spp_peer(bt)
    limited = BluetoothDevice(bt.air, name="limited", address="00:00:00:00:5B:02")
    limited_only = hci.HCI_Command(
        op_code=hci.HCI_WRITE_CURRENT_IAC_LAP_COMMAND, parameters=bytes.fromhex("01008b9e")
    )
    bt.air.call(lambda: limited._controller.on_hci_packet(limited_only))  # type: ignore[union-attr]
    found: list[str] = []
    runner.guest.device.on(
        "inquiry_result",
        lambda address, *args: found.append(str(address).removesuffix("/P")),
    )

    async def inquiry() -> None:
        await runner.guest.device.start_discovery(auto_restart=False)
        await asyncio.sleep(11)

    runner.run(inquiry())
    assert found == [peer.address]
    assert limited._controller is not None and limited._controller.iac_laps == [0x9E8B00]


def test_the_commands_about_a_link_are_answered_with_the_peer_s_version(
    runner: Runner, bt: Bluetooth
) -> None:
    spp_peer(bt)
    connection = runner.run(link_to(runner.guest))
    handle = connection.handle
    policy = complete(
        runner,
        hci.HCI_Write_Link_Policy_Settings_Command(
            connection_handle=handle, link_policy_settings=5
        ),
    ).return_parameters
    assert (policy.status, policy.connection_handle) == (0, handle)
    for command in (
        hci.HCI_Change_Connection_Packet_Type_Command(connection_handle=handle, packet_type=0xCC18),
        hci.HCI_Read_Clock_Offset_Command(connection_handle=handle),
        hci.HCI_Read_Remote_Version_Information_Command(connection_handle=handle),
    ):
        status = runner.run(runner.guest.host.send_command(command))
        assert status.status == hci.HCI_COMMAND_STATUS_PENDING
    runner.advance(runner.now + MS)
    (changed,) = runner.guest.events(hci.HCI_Connection_Packet_Type_Changed_Event)
    assert (changed.status, changed.packet_type) == (0, 0xCC18)
    (offset,) = runner.guest.events(hci.HCI_Read_Clock_Offset_Complete_Event)
    assert (offset.status, offset.clock_offset) == (0, 0)
    (version,) = runner.guest.events(hci.HCI_Read_Remote_Version_Information_Complete_Event)
    peer_controller = bt.air.link.find_classic_controller(public(PEER))
    assert peer_controller is not None
    assert (version.version, version.manufacturer_name, version.subversion) == (
        peer_controller.lmp_version,
        peer_controller.manufacturer_company_identifier,
        peer_controller.lmp_subversion,
    )
    assert bt.controller.link_policies == {handle: 5}


def test_a_command_about_no_link_is_refused_with_unknown_connection_identifier(
    runner: Runner,
) -> None:
    policy = complete(
        runner,
        hci.HCI_Write_Link_Policy_Settings_Command(connection_handle=7, link_policy_settings=1),
    ).return_parameters
    assert policy.status == hci.HCI_ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
    for command in (
        hci.HCI_Change_Connection_Packet_Type_Command(connection_handle=7, packet_type=0xCC18),
        hci.HCI_Read_Clock_Offset_Command(connection_handle=7),
        hci.HCI_Read_Remote_Version_Information_Command(connection_handle=7),
    ):
        status = runner.run(runner.guest.host.send_command(command))
        assert status.status == hci.HCI_ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR


# What the controller does not know.


def test_an_unknown_command_is_answered_in_the_event_the_host_waits_for_and_kept(
    runner: Runner, bt: Bluetooth
) -> None:
    sync = complete(runner, hci.HCI_Read_Stored_Link_Key_Command(bd_addr=GUEST, read_all_flag=1))
    assert status_of(sync) == hci.HCI_ErrorCode.UNKNOWN_HCI_COMMAND_ERROR
    asynchronous = runner.run(
        runner.guest.host.send_command(
            hci.HCI_Truncated_Page_Command(
                bd_addr=public(PEER), page_scan_repetition_mode=0, clock_offset=0
            )
        )
    )
    assert isinstance(asynchronous, hci.HCI_Command_Status_Event)
    assert asynchronous.status == hci.HCI_ErrorCode.UNKNOWN_HCI_COMMAND_ERROR
    vendor = complete(runner, hci.HCI_Command(op_code=0xFC3F, parameters=b"\x01"))
    assert status_of(vendor) == hci.HCI_ErrorCode.UNKNOWN_HCI_COMMAND_ERROR
    unknown = bt.unknown_commands()
    assert [(op, name) for _, op, name in unknown] == [
        (hci.HCI_READ_STORED_LINK_KEY_COMMAND, "HCI_READ_STORED_LINK_KEY_COMMAND"),
        (hci.HCI_TRUNCATED_PAGE_COMMAND, "HCI_TRUNCATED_PAGE_COMMAND"),
        (0xFC3F, "[OGF=0x3f, OCF=0x003f]"),
    ]
    assert all(t > 0 for t, _, _ in unknown)
    report = "\n".join(bt.report())
    assert "unknown commands (3):" in report
    assert "HCI_TRUNCATED_PAGE_COMMAND (0x043f)" in report
    properties = bt.properties()
    assert properties is not None
    assert properties["unknown_commands"][2] == {  # type: ignore[index]
        "t_ns": unknown[2][0],
        "op_code": 0xFC3F,
        "name": "[OGF=0x3f, OCF=0x003f]",
    }


# HCI_Reset.


def test_a_reset_in_the_middle_of_a_link_tells_the_host_nothing_and_the_peer_s_end_falls(
    runner: Runner, bt: Bluetooth
) -> None:
    peer = spp_peer(bt)
    runner.run(link_to(runner.guest))
    reset_at = runner.now
    complete(runner, hci.HCI_Reset_Command())
    assert runner.guest.events(hci.HCI_Disconnection_Complete_Event) == []
    assert bt.controller.classic_connections == {}
    # The peer holds its end until its supervision timeout: a page meanwhile is not heard.
    with pytest.raises(core.ConnectionError) as page:
        runner.run(link_to(runner.guest))
    assert page.value.error_code == hci.HCI_ErrorCode.PAGE_TIMEOUT_ERROR
    runner.wait_for(lambda: not peer.device.connections)
    assert runner.now == pytest.approx(reset_at + 20 * S, abs=10 * MS)
    connection = runner.run(link_to(runner.guest))
    assert connection.peer_address == public(PEER)


def test_a_reset_in_the_middle_of_a_page_ends_it_with_no_word_and_no_timer(
    runner: Runner, bt: Bluetooth
) -> None:
    nobody = "00:00:00:00:5B:09"
    page = runner.guest.air.start(link_to(runner.guest, nobody))
    runner.advance(runner.now + S)
    assert public(nobody) in bt.controller.classic_connections
    complete(runner, hci.HCI_Reset_Command())
    assert bt.controller.classic_connections == {} and bt.controller._paging == {}
    page.cancel()
    runner.advance(runner.now + 6 * S)
    # No Connection_Complete at the old page's timeout, and the next page starts.
    assert runner.guest.events(hci.HCI_Connection_Complete_Event) == []
    command = hci.HCI_Create_Connection_Command(
        bd_addr=public(nobody),
        packet_type=0xCC18,
        page_scan_repetition_mode=0,
        reserved=0,
        clock_offset=0,
        allow_role_switch=1,
    )
    status = runner.run(runner.guest.host.send_command(command))
    assert status.status == hci.HCI_COMMAND_STATUS_PENDING


def test_a_reset_clears_the_answers_the_host_owed_so_the_next_page_connects(
    runner: Runner, bt: Bluetooth
) -> None:
    """#34's second comment: an answer counted late outlived the host's restart, and took the
    answer to the next page, whose pager got PAGE_TIMEOUT."""
    phone = BluetoothDevice(bt.air, name="phone", address="00:00:00:00:5C:01")
    runner.run(runner.guest.device.set_connectable(True))
    # The host misses the first Connection_Request, as across a restart of its firmware.
    runner.guest.drop = lambda packet: isinstance(packet, hci.HCI_Connection_Request_Event)
    first = bt.air.start(phone.device.connect(ADDRESS, transport=core.PhysicalTransport.BR_EDR))
    runner.advance(runner.now + MS)
    assert bt.controller._answers_due[phone.device.public_address] == 1
    runner.guest.drop = lambda packet: False
    complete(runner, hci.HCI_Reset_Command())
    assert bt.controller._answers_due == {} and bt.controller._late_answers == {}
    runner.run(runner.guest.device.set_connectable(True))
    runner.wait_for(first.done)
    with pytest.raises(core.ConnectionError):
        first.result()
    second = bt.air.start(phone.device.connect(ADDRESS, transport=core.PhysicalTransport.BR_EDR))
    runner.wait_for(second.done, within=10 * S)
    assert second.result().peer_address == GUEST


def test_a_reset_brings_back_the_defaults(runner: Runner, bt: Bluetooth) -> None:
    controller = bt.controller
    runner.run(runner.guest.device.set_discoverable(True))
    complete(runner, hci.HCI_Write_Page_Timeout_Command(page_timeout=0x1000))
    complete(runner, hci.HCI_Host_Buffer_Size_Command(100, 0, 10, 0))
    assert controller.classic_scan_enable and controller.page_timeout == 0x1000
    complete(runner, hci.HCI_Reset_Command())
    assert controller.classic_scan_enable == 0
    assert controller.page_timeout == 0x2000
    assert controller.host_acl_length is None and controller.iac_laps == [GIAC]
    assert not controller.lmp_features & hci.LmpFeatureMask.SECURE_SIMPLE_PAIRING_HOST_SUPPORT


# The SPP peer, as bt_spp_initiator meets it.


async def discover(guest: Bluedroid, name: str) -> hci.Address:
    found: list[tuple[hci.Address, str | None]] = []

    def result(address: hci.Address, cod: int, eir: core.AdvertisingData, rssi: int) -> None:
        found.append((address, eir.get(core.AdvertisingData.COMPLETE_LOCAL_NAME)))

    guest.device.on("inquiry_result", result)
    await guest.device.start_discovery(auto_restart=False)
    while not any(n == name for _, n in found):
        await asyncio.sleep(0.01)
    await guest.device.stop_discovery()
    return next(a for a, n in found if n == name)


def test_bt_spp_initiator_s_session_finds_pairs_and_opens_the_channel(
    runner: Runner, bt: Bluetooth, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As Bluedroid does it: inquiry until the name, a link, SDP, PN, then authentication with
    the PIN, then SABM; no encryption."""
    source = Echo()
    peer = spp_peer(bt, source=source)
    guest = runner.guest
    connect = rfcomm.DLC.connect

    def authenticate_then_connect(dlc: rfcomm.DLC) -> None:
        async def later() -> None:
            await dlc.multiplexer.l2cap_channel.connection.authenticate()
            connect(dlc)

        asyncio.get_running_loop().create_task(later())

    monkeypatch.setattr(rfcomm.DLC, "connect", authenticate_then_connect)
    received: list[bytes] = []

    async def session() -> str:
        address = await discover(guest, "ESP_SPP_ACCEPTOR")
        connection = await guest.device.connect(address, transport=core.PhysicalTransport.BR_EDR)
        async with sdp.Client(connection) as client:
            names = await client.search_attributes(
                [core.BT_SERIAL_PORT_SERVICE], [sdp.SDP_ALL_ATTRIBUTES_RANGE]
            )
        (record,) = names
        name = next(a.value.value for a in record if a.id == 0x0100)
        channel = await rfcomm.find_rfcomm_channel_with_uuid(
            connection, core.BT_SERIAL_PORT_SERVICE
        )
        assert channel == 1
        multiplexer = await rfcomm.Client(connection).start()
        dlc = await multiplexer.open_dlc(channel)
        dlc.sink = received.append
        dlc.write(b"x" * 2000)
        await asyncio.sleep(0.1)
        return bytes(name).decode()

    assert runner.run(session()) == SERVICE_NAME
    assert peer.channel_open and peer.refused == []
    # Bluedroid authenticated with the PIN after PN, and did not encrypt.
    assert peer._controller is not None and peer._controller.authenticated(GUEST)
    assert guest.events(hci.HCI_Encryption_Change_Event) == []
    assert b"".join(data for _, data in source.heard) == b"x" * 2000
    assert b"".join(received) == b"ok\r\n" * len(source.heard)
    # Every packet the host got fits esp32sim's VHCI, its ACL data cut to the buffer.
    acl = [p for _, p in guest.received if isinstance(p, hci.HCI_AclDataPacket)]
    assert acl and all(len(p.data) <= ACL_LENGTH for p in acl)
    assert all(len(bytes(p)) <= HCI_MAX for _, p in guest.received)


def test_the_channel_is_refused_at_sabm_on_a_link_that_never_authenticated(
    runner: Runner, bt: Bluetooth
) -> None:
    peer = spp_peer(bt)
    guest = runner.guest

    async def session() -> None:
        connection = await link_to(guest)
        multiplexer = await rfcomm.Client(connection).start()
        await multiplexer.open_dlc(1)

    with pytest.raises(core.ConnectionError) as refused:
        runner.run(session())
    assert refused.value.error_code == core.ConnectionError.CONNECTION_REFUSED
    ((at, why),) = peer.refused
    assert why == f"{ADDRESS}: the link is not authenticated" and 0 < at < runner.now
    assert not peer.channel_open


def test_acl_data_for_the_host_is_cut_to_its_buffer_size(runner: Runner, bt: Bluetooth) -> None:
    spp_peer(bt, source=Echo(answer=b"y" * 1500), secure=False)
    complete(runner, hci.HCI_Host_Buffer_Size_Command(100, 0, 10, 0))
    received: list[bytes] = []

    async def session() -> None:
        connection = await link_to(runner.guest)
        multiplexer = await rfcomm.Client(connection).start()
        dlc = await multiplexer.open_dlc(1)
        dlc.sink = received.append
        dlc.write(b"?")
        await asyncio.sleep(0.1)

    runner.run(session())
    assert b"".join(received) == b"y" * 1500
    acl = [p for _, p in runner.guest.received if isinstance(p, hci.HCI_AclDataPacket)]
    assert max(len(p.data) for p in acl) == 100
    assert {p.pb_flag for p in acl} == {0b10, 0b01}


# The interface's rules.


def test_an_output_due_before_its_turn_fails_the_link() -> None:
    """A runner that ran past the bound the harness gave it: the controller's answer to a packet
    at 1 ms is due at 1 ms plus the latency, and the turn comes 1 ms later."""
    bt = Bluetooth()
    bt.use()
    link = BoardLink("esp32-devkitc", [bt])
    link.receive(HELLO)
    link.receive(Sync(0))
    reset = bytes(hci.HCI_Reset_Command())
    assert link.receive(HciTx(1, MS, reset)) == []
    assert link.receive(Sync(2 * MS)) == [Stop()]
    assert link.error == (
        "bluetooth.poll raised BluetoothError: the guest's controller made an event at 0.001 s,"
        " due at 0.00101 s, before the turn at 0.002 s: the link bounds every turn by the"
        " interfaces' quiet_until and latency (protocol §7)"
    )
    bt.close()


def test_a_packet_longer_than_the_vhci_carries_is_an_error(bt: Bluetooth) -> None:
    bt.air.call(lambda: bt.on_packet(b"\x04" + bytes(HCI_MAX)))
    with pytest.raises(
        BluetoothError, match="of 497 bytes at 0 s; esp32sim's VHCI carries 1 to 496"
    ):
        bt.poll(LATENCY_NS)


def test_an_interface_the_test_never_used_asks_for_nothing_and_bounds_no_turn() -> None:
    asked: list[Request] = []
    bt = Bluetooth(asked=asked.append)
    assert not bt.used and bt.latency() is None and bt.quiet_until() is None
    assert list(bt.poll(0)) == [] and bt.properties() is None
    assert bt.report() == ["bluetooth: not used"]
    with pytest.raises(BluetoothError, match="the run has no --bt"):
        bt.receive(0, "out", bytes(hci.HCI_Reset_Command()))
    air = bt.air
    assert bt.used and air.board_clock and bt.air is air
    assert asked == [Request("bluetooth", source="model")]
    bt.close()


def test_the_air_on_the_board_s_clock_moves_only_with_the_link(bt: Bluetooth) -> None:
    air = bt.air
    moves: list[tuple[Callable[[], object], str]] = [
        (lambda: air.wait(seconds(1)), "air.wait"),
        (lambda: air.advance(seconds(1)), "air.advance"),
        (lambda: air.run(asyncio.sleep(0), within=seconds(1)), "air.run"),
    ]
    for move, call in moves:
        with pytest.raises(AirError) as refused:
            move()
        assert str(refused.value) == (
            f"{call}: this air is on the board's clock, which only the board's link moves: let"
            " the board run with board.wait() or board.wait_until()"
        )
    # A device is made at the board's instant, and the link moves the air.
    peer = spp_peer(bt)
    bt.arrive(2 * S)
    assert air.now == seconds(2)
    peer.switch_off()
    assert not peer.present and air.now == seconds(2)


def test_the_supply_and_a_chip_reset_take_the_controller_off_the_air_and_back(
    runner: Runner, bt: Bluetooth
) -> None:
    peer = spp_peer(bt)
    runner.run(link_to(runner.guest))
    received = len(runner.guest.received)
    bt.supply(False)
    assert not bt.air.link.present(GUEST) and bt.controller.classic_connections == {}
    bt.supply(True)
    assert bt.air.link.present(GUEST)
    bt.chip_reset(runner.now)
    assert bt.air.link.present(GUEST)
    runner.advance(runner.now + 21 * S)
    # The host was told nothing; the peer's end fell at its timeout.
    assert len(runner.guest.received) == received
    assert not peer.device.connections


def test_the_report_names_the_traffic_and_the_devices_on_the_air(
    runner: Runner, bt: Bluetooth
) -> None:
    spp_peer(bt)
    BluetoothDevice(bt.air, name="phone", address="00:00:00:00:5C:01")
    report = bt.report()
    assert report[0].startswith(f"bluetooth: the guest's controller at {ADDRESS}; ")
    assert f"{bt.packets_from_host} packets ({bt.bytes_from_host} bytes) from its host" in report[0]
    assert (
        report[-1]
        == "  on the air: ESP_SPP_ACCEPTOR (00:00:00:00:5B:01), phone (00:00:00:00:5C:01)"
    )
    properties = bt.properties()
    assert properties is not None
    assert properties["packets_to_host"] == bt.packets_to_host > 0
    assert properties["devices"] == [
        {"name": "ESP_SPP_ACCEPTOR", "address": PEER},
        {"name": "phone", "address": "00:00:00:00:5C:01"},
    ]


def test_two_runs_cross_the_same_packets_at_the_same_instants() -> None:
    def one() -> list[tuple[int, bytes]]:
        bt = Bluetooth()
        bt.use()
        guest = Bluedroid()
        runner = Runner(guest, bt)
        runner.run(guest.device.power_on())
        spp_peer(bt)
        runner.run(link_to(guest))
        runner.stop()
        crossed = [(t, bytes(p)) for t, p in guest.received]
        guest.air.close()
        bt.close()
        return crossed

    assert one() == one()


def test_the_guest_controller_is_a_controller_of_the_air() -> None:
    air = Air()
    controller = air.call(lambda: GuestController("esp32", air.link))
    assert isinstance(controller, Controller)
    assert controller.public_address == GUEST and controller.lmp_features_max_page_number == 2
    air.close()
