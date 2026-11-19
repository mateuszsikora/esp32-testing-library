"""Bluetooth LE on the air without the emulator: the LE link layer of esp32tl's controllers
(`esp32tl/bluetooth/link_layer.py`), the LE-only guest controller of the ESP32-S3, ESP32-C3 and
ESP32-C6 (`esp32tl/bluetooth/guest.py`), and the LE peers (`esp32tl/bluetooth/le.py`), on an
`Air` of their own. A raw host, which keeps every packet its controller sends it, stands in for a
host where the link layer is under test; a `BleCentral` stands in for the firmware where a
`BlePeripheral` is."""

from typing import Any

import pytest
from bumble import crypto, hci
from bumble.pairing import PairingConfig
from bumble.smp import AddressResolver
from conftest import CountingList

from esp32tl.bluetooth.air import Air
from esp32tl.bluetooth.controller import Controller
from esp32tl.bluetooth.guest import (
    ADV_REPORT_FLOW_CONTROL,
    LE_ACL_LENGTH,
    SET_ADV_FLOW_CONTROL,
    GuestController,
)
from esp32tl.bluetooth.le import (
    Advertisement,
    BleCentral,
    BleError,
    BlePeripheral,
    GattCharacteristic,
    GattService,
    GattWrite,
)
from esp32tl.bluetooth.peer import PeerError
from esp32tl.interfaces.bluetooth import Bluetooth
from esp32tl.report import RunLog
from esp32tl.units import Duration, ms, seconds

EventType = hci.HCI_LE_Advertising_Report_Event.EventType
AdvertisingType = hci.HCI_LE_Set_Advertising_Parameters_Command.AdvertisingType

ADVERTISER = "00:00:00:00:5B:0A"
SCANNER = "00:00:00:00:5C:0A"


class Host:
    """A host that keeps what its controller sends it, parsed."""

    def __init__(self) -> None:
        self.packets: list[hci.HCI_Packet] = []

    def on_packet(self, data: bytes) -> None:
        self.packets.append(hci.HCI_Packet.from_bytes(data))

    def events(self, kind: type) -> list[Any]:
        return [p for p in self.packets if isinstance(p, kind)]

    def reports(self) -> list[Any]:
        return [r for e in self.events(hci.HCI_LE_Advertising_Report_Event) for r in e.reports]


def raw(
    air: Air, address: str, kind: type[Controller] = Controller, **kwargs: Any
) -> tuple[Any, Host]:
    controller = air.call(lambda: kind(address, air._link, address, **kwargs))
    host = Host()
    controller.host = host
    return controller, host


def send(air: Air, controller: Controller, command: hci.HCI_Command) -> None:
    air.call(lambda: controller.on_hci_packet(command))


def advertise(
    air: Air,
    controller: Controller,
    *,
    interval: int = 0x00A0,
    kind: int = AdvertisingType.ADV_IND,
    data: bytes = b"\x02\x01\x06",
    scan_response: bytes = b"\x04\x09abc",
    own_address_type: int = 0,
) -> None:
    send(
        air,
        controller,
        hci.HCI_LE_Set_Advertising_Parameters_Command(
            advertising_interval_min=interval,
            advertising_interval_max=interval,
            advertising_type=kind,
            own_address_type=own_address_type,
            peer_address_type=0,
            peer_address=hci.Address.ANY,
            advertising_channel_map=7,
            advertising_filter_policy=0,
        ),
    )
    send(air, controller, hci.HCI_LE_Set_Advertising_Data_Command(advertising_data=data))
    send(
        air, controller, hci.HCI_LE_Set_Scan_Response_Data_Command(scan_response_data=scan_response)
    )
    send(air, controller, hci.HCI_LE_Set_Advertising_Enable_Command(advertising_enable=1))


def scan(
    air: Air, controller: Controller, *, active: bool = True, duplicates: bool = False
) -> None:
    send(
        air,
        controller,
        hci.HCI_LE_Set_Scan_Parameters_Command(
            le_scan_type=int(active),
            le_scan_interval=0x10,
            le_scan_window=0x10,
            own_address_type=0,
            scanning_filter_policy=0,
        ),
    )
    send(
        air,
        controller,
        hci.HCI_LE_Set_Scan_Enable_Command(le_scan_enable=1, filter_duplicates=int(duplicates)),
    )


# The LE link layer.


def test_advertising_repeats_at_the_host_s_interval_and_an_active_scan_gets_the_scan_response() -> (
    None
):
    air = Air()
    advertiser, _ = raw(air, ADVERTISER)
    scanner, host = raw(air, SCANNER)
    scan(air, scanner)
    advertise(air, advertiser, interval=0x00A0)  # 160 slots of 0.625 ms: 100 ms
    air.wait(ms(350))
    reports = host.reports()
    assert [r.event_type for r in reports] == [EventType.ADV_IND, EventType.SCAN_RSP] * 4
    assert reports[1].data == b"\x04\x09abc" and reports[0].data == b"\x02\x01\x06"
    # every report has the signal strength the advertiser's controller was given
    assert {r.rssi for r in reports} == {advertiser.rssi}


def test_a_passive_scan_gets_no_scan_response_and_duplicates_are_filtered_when_asked() -> None:
    air = Air()
    advertiser, _ = raw(air, ADVERTISER)
    scanner, host = raw(air, SCANNER)
    scan(air, scanner, active=False, duplicates=True)
    advertise(air, advertiser, interval=0x0020)
    air.wait(ms(200))
    assert [r.event_type for r in host.reports()] == [EventType.ADV_IND]


def test_non_connectable_advertising_is_reported_as_such_and_never_connected_to() -> None:
    air = Air()
    advertiser, _ = raw(air, ADVERTISER)
    central, host = raw(air, SCANNER)
    scan(air, central)
    advertise(air, advertiser, kind=AdvertisingType.ADV_NONCONN_IND)
    send(air, central, create_connection(ADVERTISER))
    air.wait(ms(250))
    assert {r.event_type for r in host.reports()} == {EventType.ADV_NONCONN_IND}
    assert host.events(hci.HCI_LE_Connection_Complete_Event) == []


def create_connection(address: str, *, policy: int = 0) -> hci.HCI_LE_Create_Connection_Command:
    return hci.HCI_LE_Create_Connection_Command(
        le_scan_interval=0x10,
        le_scan_window=0x10,
        initiator_filter_policy=policy,
        peer_address_type=0,
        peer_address=hci.Address(address, hci.Address.PUBLIC_DEVICE_ADDRESS),
        own_address_type=0,
        connection_interval_min=0x18,
        connection_interval_max=0x28,
        max_latency=0,
        supervision_timeout=0x01F4,
        min_ce_length=0,
        max_ce_length=0,
    )


def test_both_hosts_get_the_parameters_the_central_asked_for() -> None:
    air = Air()
    advertiser, peripheral = raw(air, ADVERTISER)
    central, host = raw(air, SCANNER)
    advertise(air, advertiser)
    send(air, central, create_connection(ADVERTISER))
    # the next advertising event, 100 ms on, is the one the initiation hears
    air.wait(ms(150))
    for side in (host, peripheral):
        (complete,) = side.events(hci.HCI_LE_Connection_Complete_Event)
        assert (complete.connection_interval, complete.supervision_timeout) == (0x18, 0x01F4)
    assert not advertiser.le_legacy_advertiser.enabled


def test_an_initiation_by_the_filter_accept_list_connects_to_a_device_on_it() -> None:
    air = Air()
    advertiser, _ = raw(air, ADVERTISER)
    central, host = raw(air, SCANNER)
    send(
        air,
        central,
        hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command(
            address_type=0, address=hci.Address(ADVERTISER, hci.Address.PUBLIC_DEVICE_ADDRESS)
        ),
    )
    advertise(air, advertiser)
    send(air, central, create_connection("00:00:00:00:00:00", policy=1))
    air.wait(ms(150))
    (complete,) = host.events(hci.HCI_LE_Connection_Complete_Event)
    assert complete.status == 0 and str(complete.peer_address) == f"{ADVERTISER}/P"


def test_cancelling_an_initiation_completes_it_with_unknown_connection_identifier() -> None:
    air = Air()
    central, host = raw(air, SCANNER)
    send(air, central, create_connection(ADVERTISER))
    send(air, central, hci.HCI_LE_Create_Connection_Cancel_Command())
    complete = host.events(hci.HCI_Command_Complete_Event)[-1]
    assert complete.command_opcode == hci.HCI_LE_CREATE_CONNECTION_CANCEL_COMMAND
    (failed,) = host.events(hci.HCI_LE_Connection_Complete_Event)
    assert failed.status == hci.HCI_ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
    assert host.packets.index(complete) < host.packets.index(failed)
    # nothing to cancel any more
    send(air, central, hci.HCI_LE_Create_Connection_Cancel_Command())
    last = host.events(hci.HCI_Command_Complete_Event)[-1]
    assert last.return_parameters.status == hci.HCI_ErrorCode.COMMAND_DISALLOWED_ERROR


def test_a_reset_in_the_middle_of_an_initiation_tells_the_host_nothing_of_it() -> None:
    air = Air()
    central, host = raw(air, SCANNER)
    send(air, central, create_connection(ADVERTISER))
    send(air, central, hci.HCI_Reset_Command())
    air.wait(ms(10))
    assert host.events(hci.HCI_LE_Connection_Complete_Event) == []
    assert central.pending_le_connection is None
    assert host.events(hci.HCI_Command_Complete_Event)[-1].command_opcode == hci.HCI_RESET_COMMAND


def test_le_rand_draws_from_the_controller_s_own_generator() -> None:
    def numbers() -> list[bytes]:
        air = Air()
        controller, host = raw(air, SCANNER)
        for _ in range(3):
            send(air, controller, hci.HCI_LE_Rand_Command())
        return [
            e.return_parameters.random_number for e in host.events(hci.HCI_Command_Complete_Event)
        ]

    assert numbers() == numbers()
    assert len(set(numbers())) == 3


def test_advertising_from_the_resolving_list_takes_an_rpa_of_the_local_irk() -> None:
    air = Air()
    advertiser, _ = raw(air, ADVERTISER)
    scanner, host = raw(air, SCANNER)
    irk = bytes(range(16))
    send(
        air,
        advertiser,
        hci.HCI_LE_Add_Device_To_Resolving_List_Command(
            peer_identity_address_type=0,
            peer_identity_address=hci.Address.ANY,
            peer_irk=bytes(16),
            local_irk=irk,
        ),
    )
    send(air, advertiser, hci.HCI_LE_Set_Resolvable_Private_Address_Timeout_Command(rpa_timeout=1))
    scan(air, scanner, active=False)
    advertise(air, advertiser, own_address_type=0x02, interval=0x0320)  # 500 ms
    air.wait(ms(600))
    first, second = (r.address for r in host.reports())
    resolver = AddressResolver([(irk, hci.Address(ADVERTISER, hci.Address.PUBLIC_DEVICE_ADDRESS))])
    assert first.is_resolvable and resolver.resolve(first) is not None
    assert first == second  # within the RPA timeout, the same address
    air.wait(ms(1000))
    assert host.reports()[-1].address != first and resolver.resolve(host.reports()[-1].address)
    # without an entry for the peer, type 0x02 falls back to the public address
    fallback = air.call(lambda: advertiser.own_address(0x02, 0, hci.Address("11:22:33:44:55:66")))
    assert fallback == advertiser.public_address
    assert crypto.ah(irk, bytes(first)[3:]) == bytes(first)[:3]


def test_the_enhanced_connection_complete_names_the_local_rpa_when_unmasked() -> None:
    air = Air()
    advertiser, peripheral = raw(air, ADVERTISER)
    central, _ = raw(air, SCANNER)
    send(
        air,
        advertiser,
        hci.HCI_LE_Add_Device_To_Resolving_List_Command(
            peer_identity_address_type=0,
            peer_identity_address=hci.Address.ANY,
            peer_irk=bytes(16),
            local_irk=bytes(range(16)),
        ),
    )
    send(
        air,
        advertiser,
        hci.HCI_LE_Set_Event_Mask_Command(le_event_mask=bytes.fromhex("ff07000000000000")),
    )
    advertise(air, advertiser, own_address_type=0x02)
    rpa = air.call(lambda: advertiser.le_legacy_advertiser.address)
    command = create_connection(ADVERTISER)
    command.peer_address_type = 1
    command.peer_address = rpa
    send(air, central, command)
    air.wait(ms(150))
    (complete,) = peripheral.events(hci.HCI_LE_Enhanced_Connection_Complete_Event)
    assert complete.local_resolvable_private_address == rpa
    assert peripheral.events(hci.HCI_LE_Connection_Complete_Event) == []


def test_le_data_for_the_host_is_cut_to_its_buffer() -> None:
    air = Air()
    central = BleCentral(air)
    peripheral = BlePeripheral(
        air, name="big", services=[GattService("00FF", [GattCharacteristic("FF01", bytes(200))])]
    )
    controller = central._controller
    assert controller is not None
    controller.host_acl_length = 27
    seen = air.run(_first(central), within=seconds(1))
    air.run(_wait(central.connect(seen)), within=seconds(1))
    packets: list[int] = []
    original = controller.send_hci_packet

    def counting(packet: hci.HCI_Packet) -> None:
        if isinstance(packet, hci.HCI_AclDataPacket):
            packets.append(packet.data_total_length)
        original(packet)

    controller.send_hci_packet = counting  # type: ignore[method-assign]
    air.run(_wait(central.read("FF01")), within=seconds(1))
    assert packets and max(packets) <= 27
    assert peripheral.connected


async def _first(central: BleCentral) -> Any:
    import asyncio

    while not central.advertisements:
        await asyncio.sleep(0.01)
    return central.advertisements[0]


async def _wait(condition: Any) -> Any:
    import asyncio

    while not (found := condition()):
        await asyncio.sleep(0.001)
    return found


# The guest's controller on an LE-only chip.


@pytest.mark.parametrize("chip", ["esp32s3", "esp32c3", "esp32c6"])
def test_an_le_only_chip_s_controller_reports_le_and_no_br_edr(chip: str) -> None:
    air = Air()
    guest, host = raw(air, ADVERTISER, GuestController, chip=chip)
    send(air, guest, hci.HCI_Read_Local_Supported_Features_Command())
    send(air, guest, hci.HCI_LE_Read_Buffer_Size_Command())
    features, buffer = (e.return_parameters for e in host.events(hci.HCI_Command_Complete_Event))
    page0 = hci.LmpFeatureMask(int.from_bytes(features.lmp_features, "little"))
    assert page0 == (
        hci.LmpFeatureMask.LE_SUPPORTED_CONTROLLER | hci.LmpFeatureMask.BR_EDR_NOT_SUPPORTED
    )
    assert buffer.le_acl_data_packet_length == LE_ACL_LENGTH
    # extended advertising is left out: the host takes the legacy commands
    assert not guest.le_features & hci.LeFeatureMask.LE_EXTENDED_ADVERTISING


def test_an_le_only_chip_s_controller_answers_as_the_measured_esp32_s3() -> None:
    # What an ESP32-S3 answered tools/hci_probe (tools/hci_probe/esp32s3.txt), but the address
    air = Air()
    guest, host = raw(air, ADVERTISER, GuestController, chip="esp32s3")
    for command in (
        hci.HCI_Read_Local_Version_Information_Command(),
        hci.HCI_LE_Read_Local_Supported_Features_Command(),
        hci.HCI_LE_Read_Buffer_Size_Command(),
        hci.HCI_LE_Read_Filter_Accept_List_Size_Command(),
        hci.HCI_LE_Read_Resolving_List_Size_Command(),
        hci.HCI_LE_Read_Maximum_Data_Length_Command(),
        hci.HCI_LE_Read_Suggested_Default_Data_Length_Command(),
        hci.HCI_LE_Read_Advertising_Physical_Channel_Tx_Power_Command(),
        hci.HCI_Read_Local_Supported_Commands_Command(),
    ):
        send(air, guest, command)
    answers = [bytes(e).hex() for e in host.events(hci.HCI_Command_Complete_Event)]
    assert answers[:8] == [
        "040e0c0501100009160009e5021600",
        "040e0c05032000ff00000000000000",
        "040e0705022000fb000c",
        "040e05050f20000c",
        "040e05052a20000a",
        "040e0c052f2000fb009042fb009042",
        "040e08052320001b004801",
        "040e050507200009",
    ]
    assert answers[8] == "040e4405021000" + (
        "2000800000c000000000e40000002822000000000000040000f7ffff7f00000030f0ffffffffff07"
        + "00" * 24
    )


def test_the_data_length_told_is_the_connection_s_on_the_1m_phy() -> None:
    # The S3's controller supports 17040 µs, the coded PHY's; the connection, on the 1M PHY, 2120
    air = Air()
    guest, host = raw(air, SCANNER, GuestController, chip="esp32s3")
    peripheral = sensor(air)
    send(air, guest, create_connection(peripheral.address))
    air.wait(ms(150))
    (complete,) = host.events(hci.HCI_LE_Connection_Complete_Event)
    send(
        air,
        guest,
        hci.HCI_LE_Set_Data_Length_Command(
            connection_handle=complete.connection_handle, tx_octets=251, tx_time=2120
        ),
    )
    (change,) = host.events(hci.HCI_LE_Data_Length_Change_Event)
    assert (change.max_tx_octets, change.max_tx_time) == (251, 2120)
    assert (change.max_rx_octets, change.max_rx_time) == (251, 2120)


def test_an_le_only_chip_s_controller_answers_what_it_does_not_know_with_command_complete() -> None:
    air = Air()
    guest, host = raw(air, ADVERTISER, GuestController, chip="esp32c3")
    send(air, guest, hci.HCI_Read_Buffer_Size_Command())
    send(air, guest, hci.HCI_Read_Local_Extended_Features_Command(page_number=0))
    send(
        air,
        guest,
        hci.HCI_Inquiry_Command(lap=0x9E8B33, inquiry_length=8, num_responses=0),
    )
    assert [bytes(p).hex() for p in host.packets] == [
        "040e0405051001",
        "040e0405041001",
        "040e0405010401",
    ]
    assert [c.op_code for c in guest.unknown_commands] == [0x1005, 0x1004, 0x0401]


def test_the_classic_esp32_s_controller_keeps_br_edr_and_no_le() -> None:
    air = Air()
    guest, _ = raw(air, ADVERTISER, GuestController)
    assert not guest.le_only
    assert not guest.lmp_features & hci.LmpFeatureMask.LE_SUPPORTED_CONTROLLER
    assert guest.lmp_features & hci.LmpFeatureMask.ENCRYPTION


def test_espressif_s_flow_control_sends_no_more_reports_than_the_credits() -> None:
    air = Air()
    advertiser, _ = raw(air, ADVERTISER)
    guest, host = raw(air, SCANNER, GuestController, chip="esp32c3")
    # enable, 2 credits, lost threshold 20
    send(
        air,
        guest,
        hci.HCI_Command(op_code=SET_ADV_FLOW_CONTROL, parameters=bytes([1, 2, 0, 20, 0])),
    )
    complete = host.events(hci.HCI_Command_Complete_Event)[-1]
    assert complete.command_opcode == SET_ADV_FLOW_CONTROL
    scan(air, guest, active=False)
    advertise(air, advertiser, interval=0x0020)
    air.wait(ms(100))
    assert len(host.reports()) == 2 and guest.dropped_reports > 0
    # the host gives credits back with no answer: Bluedroid sends it NOACK
    before = len(host.packets)
    send(air, guest, hci.HCI_Command(op_code=ADV_REPORT_FLOW_CONTROL, parameters=bytes([1, 0])))
    assert len(host.packets) == before
    air.wait(ms(30))
    assert len(host.reports()) == 3
    assert guest.unknown_commands == []


def test_past_the_lost_threshold_a_credit_given_back_first_tells_how_many_were_dropped() -> None:
    # As the S3 did: 2 credits, threshold 3; credits back bring the discard event first, which
    # takes one, then a report
    air = Air()
    advertiser, _ = raw(air, ADVERTISER)
    guest, host = raw(air, SCANNER, GuestController, chip="esp32s3")
    send(
        air, guest, hci.HCI_Command(op_code=SET_ADV_FLOW_CONTROL, parameters=bytes([1, 2, 0, 3, 0]))
    )
    scan(air, guest, active=False)
    advertise(air, advertiser, interval=0x0020)
    air.wait(ms(200))
    dropped = guest.dropped_reports
    assert len(host.reports()) == 2 and dropped >= 3
    before = len(host.packets)
    send(air, guest, hci.HCI_Command(op_code=ADV_REPORT_FLOW_CONTROL, parameters=bytes([2, 0])))
    air.wait(ms(25))
    discard, report = host.packets[before : before + 2]
    assert bytes(discard) == bytes([4, 0x3E, 5, 0xF0]) + dropped.to_bytes(4, "little")
    assert isinstance(report, hci.HCI_LE_Advertising_Report_Event)
    assert len(host.reports()) == 3


# The LE peers.


def sensor(air: Air, **kwargs: Any) -> BlePeripheral:
    services = [
        GattService(
            "00FF",
            [
                GattCharacteristic("FF01", b"\x01", ("read", "write", "notify", "indicate")),
                GattCharacteristic("FF02", b"secret", ("read",), encrypted=True),
                GattCharacteristic("FF03", b"ro", ("read",)),
            ],
        )
    ]
    return BlePeripheral(air, name="sensor", services=services, **kwargs)


def connected(air: Air, **kwargs: Any) -> tuple[BlePeripheral, BleCentral]:
    peripheral = sensor(air, **{k[2:]: v for k, v in kwargs.items() if k.startswith("p_")})
    central = BleCentral(air, **{k[2:]: v for k, v in kwargs.items() if k.startswith("c_")})
    seen = air.run(_wait(central.next_advertisement(name="sensor")), within=seconds(1))
    air.run(_wait(central.connect(seen)), within=seconds(1))
    return peripheral, central


def test_a_peripheral_advertises_its_name_services_and_manufacturer_data() -> None:
    air = Air()
    BlePeripheral(
        air,
        name="sensor",
        services=[GattService("181A", [GattCharacteristic("2A6E", b"\x00\x01")])],
        manufacturer_data=(0x02E5, b"\x01\x02"),
        interval=ms(200),
    )
    central = BleCentral(air)
    # the first event, at the peripheral's creation, came before the central scanned
    air.wait(ms(650))
    first, second, third = central.advertisements
    assert first.name == "sensor" and first.service_uuids == ("181A",)
    assert first.manufacturer_data == (0x02E5, b"\x01\x02") and first.connectable
    assert (second.at - first.at, third.at - second.at) == (ms(200), ms(200))
    assert first.address == "00:00:00:00:5B:02" and first.address_type == "public"


def test_what_the_advertising_data_does_not_hold_goes_into_the_scan_response() -> None:
    air = Air()
    long = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
    BlePeripheral(air, name="a rather long sensor name", service_uuids=[long])
    central = BleCentral(air)
    air.wait(ms(150))
    (heard,) = central.advertisements
    assert heard.service_uuids == (long,) and heard.name == "a rather long sensor name"
    assert len(heard.data) <= 31 and heard.scan_response
    with pytest.raises(PeerError, match="fit neither in the advertising data nor"):
        BlePeripheral(
            air, name="m", manufacturer_data=(0x02E5, bytes(30)), address="00:00:00:00:5B:03"
        )


def test_a_name_too_long_for_a_scan_response_is_advertised_shortened() -> None:
    air = Air()
    name = "a sensor whose name is longer than any scan response holds"
    BlePeripheral(air, name=name)
    central = BleCentral(air)
    air.wait(ms(150))
    (heard,) = central.advertisements
    assert heard.name == name[:29] and name.startswith(heard.name)
    assert bytes([30, 0x08]) + name[:29].encode() == heard.scan_response


def test_a_central_connects_discovers_reads_writes_and_is_notified_and_indicated() -> None:
    air = Air()
    peripheral, central = connected(air)
    assert peripheral.connected and central.peer == peripheral.address
    discovery = air.run(_wait(central.discover()), within=seconds(1))
    (mine,) = [s for s in discovery.services if s.uuid == "00FF"]
    assert [c.uuid for c in mine.characteristics] == ["FF01", "FF02", "FF03"]
    assert mine.characteristics[0].properties == ("read", "write", "notify", "indicate")
    assert air.run(_wait(central.read("FF01")), within=seconds(1)).value == b"\x01"
    air.run(_wait(central.write("FF01", b"abc")), within=seconds(1))
    assert peripheral.value("FF01") == b"abc" and peripheral.writes[-1].value == b"abc"
    air.run(_wait(central.subscribe("FF01")), within=seconds(1))
    assert peripheral.subscribed("FF01")
    air.run(_wait(peripheral.notify("FF01", b"n1")), within=seconds(1))
    air.run(_wait(central.subscribe("FF01", indicate=True)), within=seconds(1))
    air.run(_wait(peripheral.indicate("FF01", b"i1")), within=seconds(1))
    assert [(n.value, n.indication) for n in central.notifications] == [
        (b"n1", False),
        (b"i1", True),
    ]


def test_a_refused_request_raises_ble_error_from_its_condition() -> None:
    air = Air()
    _, central = connected(air)
    write = central.write("FF03", b"no")
    with pytest.raises(BleError, match="central's write of FF03"):
        air.run(_wait(write), within=seconds(1))
    missing = central.read("ABCD")
    with pytest.raises(BleError, match="has no characteristic ABCD"):
        air.run(_wait(missing), within=seconds(1))


def test_an_encrypted_characteristic_reads_once_the_link_is_paired() -> None:
    air = Air()
    peripheral, central = connected(air)
    with pytest.raises(BleError):
        air.run(_wait(central.read("FF02")), within=seconds(1))
    pairing = air.run(_wait(central.pair()), within=seconds(1))
    assert pairing.succeeded and pairing.bonded and central.encrypted and peripheral.encrypted
    assert peripheral.pairings[-1].succeeded
    assert air.run(_wait(central.read("FF02")), within=seconds(1)).value == b"secret"


def test_a_passkey_shown_on_one_side_and_typed_on_the_other_pairs_and_another_fails() -> None:
    air = Air()
    _, central = connected(
        air,
        p_io_capability="display_only",
        p_passkey=123456,
        c_io_capability="keyboard_only",
        c_passkey=123456,
    )
    assert air.run(_wait(central.pair()), within=seconds(2)).succeeded
    air = Air()
    _, central = connected(
        air,
        p_io_capability="display_only",
        p_passkey=123456,
        c_io_capability="keyboard_only",
        c_passkey=654321,
    )
    with pytest.raises(BleError, match="pairing"):
        air.run(_wait(central.pair()), within=seconds(2))
    assert not central.pairings[-1].succeeded


def test_numeric_comparison_pairs_when_both_users_confirm() -> None:
    air = Air()
    _, central = connected(air, p_io_capability="display_yes_no", c_io_capability="display_yes_no")
    assert air.run(_wait(central.pair()), within=seconds(2)).succeeded


def test_out_of_range_both_ends_fall_at_the_supervision_timeout_and_reconnect_in_range() -> None:
    air = Air()
    peripheral, central = connected(air)
    left = air.now
    peripheral.out_of_range()
    air.wait(seconds(1))
    assert [d.reason for d in central.disconnections] == [0x08]
    assert [d.reason for d in peripheral.disconnections] == [0x08]
    # Bumble's central asks for 720 ms
    assert central.disconnections[0].at - left == ms(720)
    assert not peripheral.present
    peripheral.in_range()
    assert air.run(_wait(central.next_advertisement(name="sensor")), within=seconds(1))
    seen = central.advertisements[-1]
    air.run(_wait(central.connect(seen)), within=seconds(1))
    assert peripheral.connected


def test_a_peripheral_switched_off_is_gone_and_advertises_again_switched_on() -> None:
    air = Air()
    peripheral, central = connected(air)
    peripheral.switch_off()
    assert not peripheral.connected  # its own end is gone at once
    air.wait(seconds(1))
    assert central.disconnections[-1].reason == 0x08
    count = len(central.advertisements)
    air.wait(ms(300))
    assert len(central.advertisements) == count
    peripheral.switch_on()
    air.wait(ms(250))
    assert len(central.advertisements) > count and peripheral.advertising


def test_forgotten_keys_make_the_two_pair_again() -> None:
    air = Air()
    peripheral, central = connected(air)
    air.run(_wait(central.pair()), within=seconds(1))
    air.run(_wait(central.disconnect()), within=seconds(1))
    central.forget_keys()
    seen = air.run(_wait(central.next_advertisement(name="sensor")), within=seconds(1))
    air.run(_wait(central.connect(seen)), within=seconds(1))
    assert air.run(_wait(central.pair()), within=seconds(1)).succeeded
    assert len(central.pairings) == 2 and len(peripheral.pairings) == 2


def test_the_test_s_mistakes_are_peer_errors() -> None:
    air = Air()
    with pytest.raises(PeerError, match="not a 16-bit UUID in hex"):
        GattCharacteristic("FF0")
    with pytest.raises(PeerError, match="properties are"):
        GattCharacteristic("FF01", properties=("fly",))
    with pytest.raises(PeerError, match="in the database twice"):
        BlePeripheral(
            air,
            name="twice",
            services=[
                GattService("00FF", [GattCharacteristic("FF01"), GattCharacteristic("FF01")])
            ],
        )
    with pytest.raises(PeerError, match="interval is"):
        BlePeripheral(air, name="fast", interval=ms(10))
    peripheral, central = connected(air)
    with pytest.raises(PeerError, match="no central is subscribed to notifications of FF01"):
        peripheral.notify("FF01", b"x")
    with pytest.raises(PeerError, match="connected to 00:00:00:00:5B:02 already"):
        central.connect(peripheral.address)
    with pytest.raises(PeerError, match="another device's on the air"):
        BleCentral(air)
    lonely = BleCentral(air, address="00:00:00:00:5C:09")
    with pytest.raises(PeerError, match="not connected: read needs a connection"):
        lonely.read("FF01")


def test_the_board_s_report_and_run_log_name_the_le_events() -> None:
    log = RunLog()
    bt = Bluetooth(log=log, chip="esp32s3")
    central = BleCentral(bt.air, name="phone")
    central._note("connected to 24:6F:28:00:11:24, as central")
    report = bt.report()
    assert "  phone (00:00:00:00:5C:01), LE central: 1 event" in report
    assert "    0.000000000 s  connected to 24:6F:28:00:11:24, as central" in report
    assert bt.controller.chip == "esp32s3" and bt.controller.le_only
    properties = bt.properties()
    assert properties is not None
    assert properties["le_devices"] == [
        {
            "name": "phone",
            "address": "00:00:00:00:5C:01",
            "role": "central",
            "events": [{"t_ns": 0, "text": "connected to 24:6F:28:00:11:24, as central"}],
        }
    ]
    assert any("bluetooth: phone: connected to" in line for line in log.lines())
    bt.close()


def test_two_runs_advertise_and_connect_at_the_same_instants() -> None:
    def run() -> list[Duration]:
        air = Air()
        peripheral, central = connected(air)
        air.run(_wait(central.read("FF01")), within=seconds(1))
        return [e.at for e in central.events + peripheral.events]

    assert run() == run()


# Fixes of the first review: polls, bonding, legacy keys, a pending connection, the local reason.

N = 1000


def test_a_next_advertisement_poll_reads_only_the_advertisements_since_the_last_one() -> None:
    air = Air()
    central = BleCentral(air)
    central.advertisements = history = CountingList[Advertisement]()
    seen = central.next_advertisement(name="sensor")
    other = Advertisement(
        seconds(0), "00:00:00:00:5B:09", "public", "other", (), None, -50, True, b"", b""
    )
    for _ in range(N):
        history.append(other)
        assert seen() is None
    assert history.reads == N
    history.append(other._replace(name="sensor"))
    found = seen()
    assert found is not None and found.name == "sensor"
    assert seen() is found and history.reads == N + 2


def test_a_next_write_poll_reads_only_the_writes_since_the_last_one() -> None:
    air = Air()
    peripheral = sensor(air)
    peripheral.writes = history = CountingList[GattWrite]()
    alarm = peripheral.next_write("FF01", b"ALARM")
    for _ in range(N):
        history.append(GattWrite(seconds(0), "FF01", b"21.5"))
        assert alarm() is None
    assert history.reads == N
    history.append(GattWrite(seconds(0), "FF01", b"ALARM"))
    found = alarm()
    assert found is not None and found.value == b"ALARM"
    assert alarm() is found and history.reads == N + 2


def test_without_bonding_on_one_side_the_pairing_is_not_a_bond_and_no_key_is_kept() -> None:
    air = Air()
    peripheral, central = connected(air, c_bonding=False)
    pairing = air.run(_wait(central.pair()), within=seconds(1))
    assert pairing.succeeded and not pairing.bonded
    assert not peripheral.pairings[-1].bonded
    air.wait(ms(1))
    for device in (central, peripheral):
        keystore = device.device.keystore
        assert keystore is not None
        assert air.run(keystore.get_all(), within=seconds(0)) == []
    # nor does the central resolve the peripheral's private address to its identity
    resolver = central.device.address_resolver
    identity = hci.Address(peripheral.address, hci.Address.PUBLIC_DEVICE_ADDRESS)
    assert resolver is None or not resolver.can_resolve_to(identity)


def test_legacy_pairing_bonds_with_its_central_and_peripheral_keys() -> None:
    air = Air()
    peripheral, central = connected(air)
    # Bluedroid without ESP_LE_AUTH_REQ_SC_*: legacy pairing, whose keys Bumble keeps as
    # ltk_central and ltk_peripheral
    for device in (central, peripheral):
        device.device.pairing_config_factory = lambda connection: PairingConfig(
            sc=False, mitm=False, bonding=True
        )
    pairing = air.run(_wait(central.pair()), within=seconds(1))
    assert pairing.succeeded and pairing.bonded
    keystore = central.device.keystore
    assert keystore is not None
    keys = air.run(keystore.get(peripheral.address + "/P"), within=seconds(0))
    assert keys is not None and keys.ltk is None and keys.ltk_peripheral is not None


def test_a_connection_to_a_device_that_never_comes_is_cancelled_and_another_made() -> None:
    air = Air()
    central = BleCentral(air)
    waiting = central.connect("00:00:00:00:5B:0F")
    air.wait(ms(500))
    assert waiting() is None
    with pytest.raises(PeerError, match="a connection is being made already"):
        central.connect("00:00:00:00:5B:0E")
    central.cancel_connect()
    air.wait(ms(1))
    with pytest.raises(BleError, match="connection to 00:00:00:00:5B:0F"):
        waiting()
    peripheral = sensor(air)
    air.run(_wait(central.connect(peripheral.address)), within=seconds(1))
    assert central.peer == peripheral.address


def test_a_central_switched_off_while_connecting_ends_the_attempt_and_connects_switched_on() -> (
    None
):
    air = Air()
    central = BleCentral(air)
    waiting = central.connect("00:00:00:00:5B:0F")
    central.switch_off()
    air.wait(ms(1))
    with pytest.raises(BleError):
        waiting()
    central.switch_on()
    peripheral = sensor(air)
    air.run(_wait(central.connect(peripheral.address)), within=seconds(1))
    assert central.connected


def test_a_disconnect_is_the_local_host_s_on_its_side_and_the_remote_user_s_on_the_other() -> None:
    air = Air()
    peripheral, central = connected(air)
    ended = air.run(_wait(central.disconnect()), within=seconds(1))
    assert ended.reason == hci.HCI_ErrorCode.CONNECTION_TERMINATED_BY_LOCAL_HOST_ERROR
    assert ended.reason_name == "CONNECTION_TERMINATED_BY_LOCAL_HOST_ERROR (0x16)"
    air.wait(ms(1))
    assert peripheral.disconnections[-1].reason == (
        hci.HCI_ErrorCode.REMOTE_USER_TERMINATED_CONNECTION_ERROR
    )
