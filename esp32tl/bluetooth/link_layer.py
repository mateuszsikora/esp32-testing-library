"""The LE link layer of esp32tl's controllers: what Bumble 0.0.236's controller lacks, or does
otherwise than the specification, for Bluetooth LE devices on a `LocalLink`.

`LinkLayer` is a Bumble controller with, for links whose controllers are all of this class:

- Legacy advertising at the interval its host set (Vol 4, Part E, 7.8.5: `Advertising_Interval_Min`
  in units of 0.625 ms), where Bumble takes the number for milliseconds, and of each legacy type
  but the directed ones: `ADV_IND`, `ADV_SCAN_IND` and `ADV_NONCONN_IND`, where Bumble sends
  `ADV_IND` alone. No random delay is added to the interval (Vol 6, Part B, 4.4.2.2.1:
  `advDelay`), so two runs advertise at the same instants. Advertising starts at the instant its
  host enables it and repeats at the interval until it is disabled, a connection is made from it,
  or the controller restarts.
- Scanning (7.8.10, 7.8.11): an advertising report of the event's own type for every advertising
  event heard, with the signal strength the advertiser's controller was given (`rssi`) and, when
  the scan is active and the advertising scannable, a scan response with the advertiser's scan
  response data, as if the scan request had been answered at once. Bumble sends the advertising
  data twice, the second time as a scan response, whatever the scan. With duplicate filtering
  on, each advertiser and each type is reported once until the scan is enabled again.
- Initiating (7.8.12): a connection is made to the first connectable advertising event heard
  from the peer address, or from a device on the filter accept list when the initiator filter
  policy says so; both hosts get the interval, latency and supervision timeout the initiator
  asked for, where Bumble gives the peripheral's host fixed ones. `LE_Create_Connection_Cancel`
  ends an initiation with `UNKNOWN_CONNECTION_IDENTIFIER` (7.8.13), which Bumble answers and
  does nothing for.
- The filter accept list (7.8.14 to 7.8.17), which Bumble does not keep.
- The resolving list (7.8.38 to 7.8.40), which Bumble does not keep either, and the resolvable
  private address the controller makes from its local IRK there for an own address type of
  `0x02` or `0x03` (7.8.5, 7.8.12; Vol 6, Part B, 1.3.2.2), where Bumble advertises from the
  random address whatever the type, all zeros when the host set none. The address is made from
  the controller's generator, and made again after the host's RPA timeout (7.8.45), or when the
  controller restarts; without an entry for the peer the type falls back to the public or the
  random address. Addresses the controller hears are not resolved: a host that wants a peer's
  identity resolves it itself.
- LE link supervision (Vol 6, Part B, 4.5.2): a connection that lost contact, because one of its
  devices left the air, falls with `CONNECTION_TIMEOUT` at its supervision timeout, on both ends,
  where Bumble keeps it for ever. The timeout is the one the connection was made or last
  updated with.
- ACL data for the host cut to the host's buffer and to what the transport carries, as for
  BR/EDR, where Bumble sends each L2CAP PDU whole; and sent from the connection's own address,
  where Bumble's link names the controller's random address (`Link` in `esp32tl.bluetooth.air`).
- `LE_Rand` (7.8.23) from the controller's generator, seeded with its address, where Bumble takes
  Python's global one: two runs get the same numbers.
- `LE_Set_Data_Length` (7.8.33), which Bumble lists as supported and has no handler for: the
  connection keeps the lengths both controllers support, on the 1M PHY at most 2120 µs, and the
  host is told so once.
- `HCI_Reset` (7.3.2): advertising, scanning and an initiation stop, the advertising and scan
  parameters and data go back to their defaults, the random address is cleared, and so is the
  filter accept list.
- Extended advertising and scanning, periodic advertising, the coded PHY and isochronous channels
  are left out: the LE features say none of them, so that a host uses the legacy commands.

The controller's host is told of its own LE connections, as Bumble's controller tells it. Which
device can be reached is the link's to decide (`Link.reaches`).
"""

import asyncio

from bumble import core, crypto, hci, ll
from bumble.controller import Connection, LegacyAdvertiser
from bumble.controller import Controller as BumbleController

from esp32tl.bluetooth.air import Link

AdvertisingType = hci.HCI_LE_Set_Advertising_Parameters_Command.AdvertisingType
EventType = hci.HCI_LE_Advertising_Report_Event.EventType

# The unit of the advertising interval, 0.625 ms, the shortest interval a host may set for a
# connectable or scannable event, 20 ms, and the default, 1.28 s (Vol 4, Part E, 7.8.5).
ADVERTISING_UNIT_S = 0.000625
ADVERTISING_INTERVAL_MIN = 0x0020
ADVERTISING_INTERVAL_DEFAULT = 0x0800

# The unit of an LE connection's supervision timeout, 10 ms (7.8.12), and the timeout of a
# connection whose initiator gave none this side can read: Bumble's own default, 720 ms.
SUPERVISION_UNIT_S = 0.01
SUPERVISION_TIMEOUT_DEFAULT = 72

# The advertising types this link carries, and the type of the report a scanner makes of each.
REPORTED = {
    AdvertisingType.ADV_IND: EventType.ADV_IND,
    AdvertisingType.ADV_SCAN_IND: EventType.ADV_SCAN_IND,
    AdvertisingType.ADV_NONCONN_IND: EventType.ADV_NONCONN_IND,
}
CONNECTABLE = {AdvertisingType.ADV_IND}
SCANNABLE = {AdvertisingType.ADV_IND, AdvertisingType.ADV_SCAN_IND}

# The own address types that take a resolvable private address from the resolving list, and fall
# back to the public or the random address (7.8.5).
RESOLVABLE_OR_PUBLIC = 0x02
RESOLVABLE_OR_RANDOM = 0x03

# The bit of the LE event mask that unmasks `LE_Enhanced_Connection_Complete` (7.8.1), and the
# LE event mask after a reset, which leaves it masked.
ENHANCED_CONNECTION_COMPLETE = 1 << 9
LE_EVENT_MASK_DEFAULT = 0x1F

# The RPA timeout until a host sets one, 900 s (7.8.45).
RPA_TIMEOUT_DEFAULT_S = 900

# An active scan (7.8.10), and the initiator filter policy that takes the filter accept list for
# the peer (7.8.12).
ACTIVE_SCAN = 0x01
FILTER_ACCEPT_LIST = 0x01

# What the link carries of LE: legacy advertising and connections, on the 1M PHY. Bumble's other
# defaults stay: encryption, the connection parameters request, the extended reject, the
# peripheral's feature exchange, LE ping, data length extension, privacy, the extended scanner
# filter policies, the 2M PHY, channel selection algorithm 2 and the minimum number of used
# channels.
LEFT_OUT_FEATURES = (
    hci.LeFeatureMask.LE_EXTENDED_ADVERTISING
    | hci.LeFeatureMask.LE_PERIODIC_ADVERTISING
    | hci.LeFeatureMask.LE_CODED_PHY
)

# The longest data an LE connection's PDU carries, and the time it takes on the 1M PHY, which
# this link's controllers support (Vol 6, Part B, 4.5.10): 251 octets in 2120 µs.
MAX_OCTETS = 251
MAX_TIME_US = 2120

# The packet boundary flags of ACL data for the host (Vol 4, Part E, 5.4.2): the first packet of
# an automatically flushable L2CAP PDU, and a continuing fragment.
FIRST_FLUSHABLE = 0b10
CONTINUING = 0b01


class Advertiser(LegacyAdvertiser):
    """Bumble's legacy advertiser, at the interval its host set, in units of 0.625 ms, for every
    legacy type but the directed ones."""

    @property
    def address(self) -> hci.Address:
        """The address the advertising is sent from, by its own address type (7.8.5)."""
        controller = self.controller
        assert isinstance(controller, LinkLayer)
        return controller.own_address(
            self.own_address_type, self.peer_address_type, self.peer_address
        )

    def _on_timer_fired(self) -> None:
        self.send_advertising_data()
        interval = max(self.advertising_interval_min, ADVERTISING_INTERVAL_MIN)
        self.timer_handle = asyncio.get_running_loop().call_later(
            interval * ADVERTISING_UNIT_S, self._on_timer_fired
        )

    def send_advertising_data(self) -> None:
        if self.enabled and self.advertising_type in REPORTED:
            self.controller.send_advertising_pdu(
                ll.AdvInd(advertiser_address=self.address, data=self.advertising_data)
            )


class LinkLayer(BumbleController):
    """A Bumble controller at `address` on `link`, named `name` in Bumble's logs, whose LE link
    layer advertises at its interval and with its types, answers an active scan with the scan
    response, initiates by the filter accept list, cancels an initiation, supervises its
    connections, cuts ACL data to the host's buffer and draws its random numbers from its own
    generator. `Controller`, in `esp32tl.bluetooth.controller`, adds Bluetooth Classic."""

    link: Link
    acl_to_host: int | None = None
    # The signal strength the controller is heard at, in dBm, and the host's buffer for ACL data,
    # which `Controller` keeps.
    rssi: int
    host_acl_length: int | None

    def __init__(self, name: str, link: Link, address: str) -> None:
        super().__init__(name, link=link, public_address=address)
        self.le_features = hci.LeFeatureMask(self.le_features & ~LEFT_OUT_FEATURES)
        self.supported_max_tx_octets = MAX_OCTETS
        self.supported_max_tx_time = MAX_TIME_US
        self.supported_max_rx_octets = MAX_OCTETS
        self.supported_max_rx_time = MAX_TIME_US
        # The device each LE connection is with, by the peer's address in `le_connections`, which
        # a random address the peer changes later does not find again.
        self._le_peers: dict[hci.Address, LinkLayer] = {}
        # Each LE connection's supervision timeout, in units of 10 ms; the time it lost contact,
        # on the loop's clock, and the timer it falls by.
        self._le_timeouts: dict[hci.Address, int] = {}
        self._le_lost_contact: dict[hci.Address, float] = {}
        self._le_supervision: dict[hci.Address, asyncio.TimerHandle] = {}
        # The connections whose data length the host was told.
        self._le_lengths_told: set[hci.Address] = set()
        self._le_defaults()

    def _le_defaults(self) -> None:
        """The LE settings `HCI_Reset` brings back (Vol 4, Part E, 7.3.2, 7.8.5, 7.8.10)."""
        self.le_legacy_advertiser = Advertiser(
            self,
            advertising_interval_min=ADVERTISING_INTERVAL_DEFAULT,
            advertising_interval_max=ADVERTISING_INTERVAL_DEFAULT,
            advertising_channel_map=0x07,
        )
        self.le_scan_enable = False
        self.le_scan_type = 0
        self.le_scan_interval = 0x10
        self.le_scan_window = 0x10
        self.filter_duplicates = False
        self.pending_le_connection = None
        self.le_event_mask = LE_EVENT_MASK_DEFAULT
        self.random_address = hci.Address("00:00:00:00:00:00")
        self.filter_accept_list: set[tuple[int, bytes]] = set()
        # The resolving list, the local IRK of each peer identity, and the resolvable private
        # address made from each local IRK, with the time it was made.
        self.resolving_list: dict[tuple[int, bytes], tuple[bytes, bytes]] = {}
        self.le_rpa_timeout = RPA_TIMEOUT_DEFAULT_S
        self._rpas: dict[bytes, tuple[hci.Address, float]] = {}
        # What a scan with duplicate filtering has reported since it was enabled.
        self._reported: set[tuple[bytes, int]] = set()

    def random_bytes(self, n: int) -> bytes:
        raise NotImplementedError

    # The host's settings.

    def on_hci_le_set_scan_enable_command(
        self, command: hci.HCI_LE_Set_Scan_Enable_Command
    ) -> hci.HCI_StatusReturnParameters:
        if command.le_scan_enable and not self.le_scan_enable:
            self._reported.clear()
        return super().on_hci_le_set_scan_enable_command(command)

    def on_hci_le_clear_filter_accept_list_command(
        self, command: hci.HCI_LE_Clear_Filter_Accept_List_Command
    ) -> hci.HCI_StatusReturnParameters:
        self.filter_accept_list.clear()
        return hci.HCI_StatusReturnParameters(hci.HCI_ErrorCode.SUCCESS)

    def on_hci_le_add_device_to_filter_accept_list_command(
        self, command: hci.HCI_LE_Add_Device_To_Filter_Accept_List_Command
    ) -> hci.HCI_StatusReturnParameters:
        entry = (int(command.address_type), bytes(command.address))
        if entry not in self.filter_accept_list:
            if len(self.filter_accept_list) >= self.filter_accept_list_size:
                return hci.HCI_StatusReturnParameters(
                    hci.HCI_ErrorCode.MEMORY_CAPACITY_EXCEEDED_ERROR
                )
            self.filter_accept_list.add(entry)
        return hci.HCI_StatusReturnParameters(hci.HCI_ErrorCode.SUCCESS)

    def on_hci_le_remove_device_from_filter_accept_list_command(
        self, command: hci.HCI_LE_Remove_Device_From_Filter_Accept_List_Command
    ) -> hci.HCI_StatusReturnParameters:
        self.filter_accept_list.discard((int(command.address_type), bytes(command.address)))
        return hci.HCI_StatusReturnParameters(hci.HCI_ErrorCode.SUCCESS)

    def on_hci_le_add_device_to_resolving_list_command(
        self, command: hci.HCI_LE_Add_Device_To_Resolving_List_Command
    ) -> hci.HCI_StatusReturnParameters:
        entry = (int(command.peer_identity_address_type), bytes(command.peer_identity_address))
        if (
            entry not in self.resolving_list
            and len(self.resolving_list) >= self.resolving_list_size
        ):
            return hci.HCI_StatusReturnParameters(hci.HCI_ErrorCode.MEMORY_CAPACITY_EXCEEDED_ERROR)
        self.resolving_list[entry] = (bytes(command.peer_irk), bytes(command.local_irk))
        return hci.HCI_StatusReturnParameters(hci.HCI_ErrorCode.SUCCESS)

    def on_hci_le_remove_device_from_resolving_list_command(self, command: hci.HCI_Command) -> None:
        """7.8.39, which Bumble has no class for, so its dispatch does not answer it: the
        parameters are the peer's identity address type and address."""
        parameters = bytes(command.parameters or b"")
        entry = (parameters[0], parameters[1:7]) if len(parameters) >= 7 else None
        removed = entry is not None and self.resolving_list.pop(entry, None) is not None
        status = (
            hci.HCI_ErrorCode.SUCCESS
            if removed
            else hci.HCI_ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
        )
        self.send_hci_packet(
            hci.HCI_Command_Complete_Event(
                num_hci_command_packets=1,
                command_opcode=command.op_code,
                return_parameters=hci.HCI_StatusReturnParameters(status),
            )
        )

    def on_hci_le_clear_resolving_list_command(
        self, command: hci.HCI_LE_Clear_Resolving_List_Command
    ) -> hci.HCI_StatusReturnParameters:
        self.resolving_list.clear()
        return hci.HCI_StatusReturnParameters(hci.HCI_ErrorCode.SUCCESS)

    def own_address(
        self, own_address_type: int, peer_address_type: int, peer_address: hci.Address
    ) -> hci.Address:
        """This side's address for an own address type and the peer it is for (7.8.5, 7.8.12):
        the public or the random address, or the resolvable private address made from the local
        IRK of the peer's entry in the resolving list, when there is one."""
        if own_address_type in (RESOLVABLE_OR_PUBLIC, RESOLVABLE_OR_RANDOM):
            entry = self.resolving_list.get((int(peer_address_type), bytes(peer_address)))
            if entry is not None and any(entry[1]):
                return self._rpa(entry[1])
        if own_address_type in (hci.OwnAddressType.PUBLIC, RESOLVABLE_OR_PUBLIC):
            return self.public_address
        return self.random_address

    def _rpa(self, irk: bytes) -> hci.Address:
        """The resolvable private address of `irk` (Vol 6, Part B, 1.3.2.2): a `prand` whose two
        most significant bits are 01, from the controller's generator, and its hash; made again
        when the RPA timeout has passed."""
        now = asyncio.get_running_loop().time()
        made = self._rpas.get(irk)
        if made is None or now - made[1] >= self.le_rpa_timeout:
            prand = bytearray(self.random_bytes(3))
            prand[2] = (prand[2] & 0x3F) | 0x40
            address = hci.Address(
                crypto.ah(irk, bytes(prand)) + bytes(prand), hci.Address.RANDOM_DEVICE_ADDRESS
            )
            made = self._rpas[irk] = (address, now)
        return made[0]

    def on_hci_le_rand_command(
        self, command: hci.HCI_LE_Rand_Command
    ) -> hci.HCI_LE_Rand_ReturnParameters:
        return hci.HCI_LE_Rand_ReturnParameters(
            status=hci.HCI_ErrorCode.SUCCESS, random_number=self.random_bytes(8)
        )

    def on_hci_le_set_data_length_command(
        self, command: hci.HCI_LE_Set_Data_Length_Command
    ) -> hci.HCI_StatusAndConnectionHandleReturnParameters:
        """7.8.33: the connection's lengths are the ones both controllers support; the host is told
        them, after the command completes, the first time it asks."""
        connection = self.find_le_connection_by_handle(command.connection_handle)
        if connection is None:
            status = hci.HCI_ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
        else:
            status = hci.HCI_ErrorCode.SUCCESS
            if connection.peer_address not in self._le_lengths_told:
                self._le_lengths_told.add(connection.peer_address)
                asyncio.get_running_loop().call_soon(self._tell_lengths, connection)
        return hci.HCI_StatusAndConnectionHandleReturnParameters(
            status=status, connection_handle=command.connection_handle
        )

    def _tell_lengths(self, connection: Connection) -> None:
        """`LE_Data_Length_Change` with the connection's lengths: what this side sends at most and
        the peer takes, and the other way round, in octets and in time on the 1M PHY, the one this
        link carries, at most `MAX_TIME_US` (Vol 6, Part B, 4.5.10). A controller may support a
        longer time, for the coded PHY, as the ESP32-S3's says in `LE_Read_Maximum_Data_Length`."""
        if self.le_connections.get(connection.peer_address) is not connection:
            return
        peer = self._le_peers.get(connection.peer_address, self)
        self.send_hci_packet(
            hci.HCI_LE_Data_Length_Change_Event(
                connection_handle=connection.handle,
                max_tx_octets=min(self.supported_max_tx_octets, peer.supported_max_rx_octets),
                max_tx_time=min(
                    self.supported_max_tx_time, peer.supported_max_rx_time, MAX_TIME_US
                ),
                max_rx_octets=min(self.supported_max_rx_octets, peer.supported_max_tx_octets),
                max_rx_time=min(
                    self.supported_max_rx_time, peer.supported_max_tx_time, MAX_TIME_US
                ),
            )
        )

    # Scanning and initiating.

    def _heard_on_link(self, sender: "LinkLayer", pdu: ll.AdvertisingPdu) -> None:
        """An advertising channel PDU from `sender`, which the link names, as Bumble's PDUs do not:
        an advertiser's private address names no controller."""
        if isinstance(pdu, ll.ConnectInd):
            self.on_le_connect_ind(pdu, sender)
        elif isinstance(pdu, ll.AdvInd):
            self._on_advertising(pdu, sender)

    def _on_advertising(self, pdu: ll.AdvInd, advertiser: "LinkLayer") -> None:
        """An advertising event heard: reported to a scanning host, with the scan response when
        the scan is active, and taken by an initiation it matches."""
        legacy = advertiser.le_legacy_advertiser
        kind = AdvertisingType(legacy.advertising_type)
        if self.le_scan_enable:
            self._report(pdu.advertiser_address, REPORTED[kind], pdu.data, advertiser.rssi)
            if self.le_scan_type == ACTIVE_SCAN and kind in SCANNABLE:
                self._report(
                    pdu.advertiser_address,
                    EventType.SCAN_RSP,
                    legacy.scan_response_data,
                    advertiser.rssi,
                )
        pending = self.pending_le_connection
        if (
            isinstance(pending, hci.HCI_LE_Create_Connection_Command)
            and kind in CONNECTABLE
            and self._initiates(pending, pdu.advertiser_address)
        ):
            self._connect(pdu.advertiser_address, advertiser)

    def _report(self, address: hci.Address, kind: int, data: bytes, rssi: int) -> None:
        if self.filter_duplicates:
            key = (bytes(address) + bytes([address.address_type]), kind)
            if key in self._reported:
                return
            self._reported.add(key)
        if not self.admits_report():
            return
        report = hci.HCI_LE_Advertising_Report_Event.Report(
            event_type=kind,
            address_type=address.address_type,
            address=address,
            data=data,
            rssi=rssi,
        )
        self.send_hci_packet(hci.HCI_LE_Advertising_Report_Event([report]))

    def admits_report(self) -> bool:
        """Whether the host takes one more advertising report: always, unless a vendor's flow
        control says otherwise (`GuestController`)."""
        return True

    def _initiates(
        self, pending: hci.HCI_LE_Create_Connection_Command, address: hci.Address
    ) -> bool:
        """Whether the initiation `pending` connects to the advertiser at `address`."""
        if pending.initiator_filter_policy == FILTER_ACCEPT_LIST:
            return (int(address.address_type), bytes(address)) in self.filter_accept_list
        return bool(pending.peer_address == address)

    def _connect(self, peer_address: hci.Address, advertiser: "LinkLayer") -> None:
        """The initiation connects to `advertiser` at `peer_address`: Bumble's, with the
        advertiser's address where a filter accept list chose it, and the timeout kept."""
        pending = self.pending_le_connection
        assert isinstance(pending, hci.HCI_LE_Create_Connection_Command)
        if peer_address in self.le_connections:
            return
        self_address = self.own_address(
            pending.own_address_type, pending.peer_address_type, pending.peer_address
        )
        connection = Connection(
            controller=self,
            handle=self.allocate_connection_handle(),
            role=hci.Role.CENTRAL,
            self_address=self_address,
            peer_address=peer_address,
            link=self.link,
            transport=core.PhysicalTransport.LE,
            link_type=hci.HCI_Connection_Complete_Event.LinkType.ACL,
        )
        self.le_connections[peer_address] = connection
        self._le_peers[peer_address] = advertiser
        interval = pending.connection_interval_min
        latency = pending.max_latency
        timeout = pending.supervision_timeout
        self._le_timeouts[peer_address] = timeout
        self.pending_le_connection = None
        self.send_advertising_pdu(
            ll.ConnectInd(
                initiator_address=self_address,
                advertiser_address=peer_address,
                interval=interval,
                latency=latency,
                timeout=timeout,
            )
        )
        self._connection_complete(connection, interval, latency, timeout)

    def on_le_connect_ind(
        self, packet: ll.ConnectInd, central: BumbleController | None = None
    ) -> None:
        """`central` connects to this side's advertising: the connection has the parameters the
        central gave, and the advertising stops."""
        legacy = self.le_legacy_advertiser
        if not (
            legacy.enabled
            and legacy.address == packet.advertiser_address
            and legacy.advertising_type in CONNECTABLE
        ):
            return
        peer_address = packet.initiator_address
        if peer_address in self.le_connections or not isinstance(central, LinkLayer):
            return
        connection = Connection(
            controller=self,
            handle=self.allocate_connection_handle(),
            role=hci.Role.PERIPHERAL,
            self_address=packet.advertiser_address,
            peer_address=peer_address,
            link=self.link,
            transport=core.PhysicalTransport.LE,
            link_type=hci.HCI_Connection_Complete_Event.LinkType.ACL,
        )
        self.le_connections[peer_address] = connection
        self._le_peers[peer_address] = central
        self._le_timeouts[peer_address] = packet.timeout
        legacy.stop()
        self._connection_complete(connection, packet.interval, packet.latency, packet.timeout)

    def _connection_complete(
        self, connection: Connection, interval: int, latency: int, timeout: int
    ) -> None:
        """The host is told of the new connection: by `LE_Enhanced_Connection_Complete` when its LE
        event mask unmasks it, with this side's resolvable private address, as 7.7.65.10 requires,
        and by `LE_Connection_Complete` otherwise. Its host needs the address its SMP pairs
        with."""
        peer = connection.peer_address
        if self.le_event_mask & ENHANCED_CONNECTION_COMPLETE:
            own = connection.self_address
            local = own if own.is_resolvable else hci.Address.ANY
            self.send_hci_packet(
                hci.HCI_LE_Enhanced_Connection_Complete_Event(
                    status=hci.HCI_ErrorCode.SUCCESS,
                    connection_handle=connection.handle,
                    role=connection.role,
                    peer_address_type=peer.address_type,
                    peer_address=peer,
                    local_resolvable_private_address=local,
                    peer_resolvable_private_address=hci.Address.ANY,
                    connection_interval=interval,
                    peripheral_latency=latency,
                    supervision_timeout=timeout,
                    central_clock_accuracy=0,
                )
            )
            return
        self.send_hci_packet(
            hci.HCI_LE_Connection_Complete_Event(
                status=hci.HCI_ErrorCode.SUCCESS,
                connection_handle=connection.handle,
                role=connection.role,
                peer_address_type=peer.address_type,
                peer_address=peer,
                connection_interval=interval,
                peripheral_latency=latency,
                supervision_timeout=timeout,
                central_clock_accuracy=0,
            )
        )

    def on_hci_le_create_connection_cancel_command(
        self, command: hci.HCI_LE_Create_Connection_Cancel_Command
    ) -> hci.HCI_StatusReturnParameters:
        """7.8.13: the initiation ends, and the host is told so, after the command completes, by
        an `LE_Connection_Complete` with `UNKNOWN_CONNECTION_IDENTIFIER`; without one, the command
        is disallowed."""
        pending = self.pending_le_connection
        if pending is None:
            return hci.HCI_StatusReturnParameters(hci.HCI_ErrorCode.COMMAND_DISALLOWED_ERROR)
        self._initiation_ended(pending)
        return hci.HCI_StatusReturnParameters(hci.HCI_ErrorCode.SUCCESS)

    def _initiation_ended(self, pending: hci.HCI_Command) -> None:
        """The initiation `pending` ends with no connection: once what is being answered now has
        gone out, the host gets the connection complete event its LE event mask asks for, with
        `UNKNOWN_CONNECTION_IDENTIFIER` (7.8.13). With no host, as in a restart, which tells the
        host nothing, the initiation just ends."""
        self.pending_le_connection = None
        if self.host is None:
            return
        address = (
            pending.peer_address
            if isinstance(pending, hci.HCI_LE_Create_Connection_Command)
            else hci.Address.ANY
        )
        status = hci.HCI_ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
        event: hci.HCI_Event
        if self.le_event_mask & ENHANCED_CONNECTION_COMPLETE:
            event = hci.HCI_LE_Enhanced_Connection_Complete_Event(
                status=status,
                connection_handle=0,
                role=hci.Role.CENTRAL,
                peer_address_type=address.address_type,
                peer_address=address,
                local_resolvable_private_address=hci.Address.ANY,
                peer_resolvable_private_address=hci.Address.ANY,
                connection_interval=0,
                peripheral_latency=0,
                supervision_timeout=0,
                central_clock_accuracy=0,
            )
        else:
            event = hci.HCI_LE_Connection_Complete_Event(
                status=status,
                connection_handle=0,
                role=hci.Role.CENTRAL,
                peer_address_type=address.address_type,
                peer_address=address,
                connection_interval=0,
                peripheral_latency=0,
                supervision_timeout=0,
                central_clock_accuracy=0,
            )
        asyncio.get_running_loop().call_soon(self.send_hci_packet, event)

    def on_hci_disconnect_command(self, command: hci.HCI_Disconnect_Command) -> None:
        """7.1.6 for an LE connection: the peer is sent `LL_TERMINATE_IND` with the host's reason,
        and this side's host gets `Disconnection_Complete` with
        `CONNECTION_TERMINATED_BY_LOCAL_HOST` (0x16), as Vol 1, Part F, 2.22 has it; Bumble gives
        both the host's reason. A Classic link's is Bumble's."""
        connection = self.find_le_connection_by_handle(command.connection_handle)
        if connection is None:
            super().on_hci_disconnect_command(command)
            return
        self._send_hci_command_status(hci.HCI_COMMAND_STATUS_PENDING, command.op_code)
        connection.send_ll_control_pdu(ll.TerminateInd(command.reason))
        self.on_le_disconnected(
            connection, hci.HCI_ErrorCode.CONNECTION_TERMINATED_BY_LOCAL_HOST_ERROR
        )

    def on_hci_le_connection_update_command(
        self, command: hci.HCI_LE_Connection_Update_Command
    ) -> None:
        connection = self.find_le_connection_by_handle(command.connection_handle)
        if connection is not None:
            self._le_timeouts[connection.peer_address] = command.supervision_timeout
        super().on_hci_le_connection_update_command(command)

    def on_ll_control_pdu(self, sender_address: hci.Address, packet: ll.ControlPdu) -> None:
        if isinstance(packet, ll.ConnectionUpdateInd) and sender_address in self.le_connections:
            self._le_timeouts[sender_address] = packet.timeout
        super().on_ll_control_pdu(sender_address, packet)

    # The remote device's version, which `Controller` answers for a Classic link.

    def le_remote_version(self, command: hci.HCI_Read_Remote_Version_Information_Command) -> bool:
        """`Read_Remote_Version_Information` for an LE connection (7.1.23): the peer's version,
        as `LL_VERSION_IND` would tell it. Whether the handle is an LE connection's."""
        connection = self.find_le_connection_by_handle(command.connection_handle)
        if connection is None:
            return False
        self._send_hci_command_status(hci.HCI_COMMAND_STATUS_PENDING, command.op_code)
        peer = self._le_peers.get(connection.peer_address)
        if peer is None or not self.link.reaches(self.public_address, peer.public_address):
            return True
        self.send_hci_packet(
            hci.HCI_Read_Remote_Version_Information_Complete_Event(
                status=hci.HCI_ErrorCode.SUCCESS,
                connection_handle=connection.handle,
                version=peer.lmp_version,
                manufacturer_name=peer.manufacturer_company_identifier,
                subversion=peer.lmp_subversion,
            )
        )
        return True

    # ACL data for the host, cut to its buffer.

    def on_link_acl_data(
        self, sender_address: hci.Address, transport: core.PhysicalTransport, data: bytes
    ) -> None:
        connection = self.le_connections.get(sender_address)
        if transport != core.PhysicalTransport.LE or connection is None:
            super().on_link_acl_data(sender_address, transport, data)
            return
        self.acl_to_host_packets(connection.handle, data)

    def acl_to_host_packets(self, handle: int, data: bytes) -> None:
        """An L2CAP PDU on `handle`, sent to the host in packets of at most the host's
        `Host_Buffer_Size` and `acl_to_host`, which 7.3.39 requires: Bumble sends it whole."""
        bounds = [b for b in (self.host_acl_length, self.acl_to_host) if b is not None]
        size = min(bounds) if bounds else max(len(data), 1)
        for at in range(0, max(len(data), 1), size):
            fragment = data[at : at + size]
            self.send_hci_packet(
                hci.HCI_AclDataPacket(
                    handle, FIRST_FLUSHABLE if at == 0 else CONTINUING, 0, len(fragment), fragment
                )
            )

    # LE link supervision.

    def _le_held(self) -> list["LinkLayer"]:
        """The devices this side has an LE connection with."""
        return [self._le_peers[peer] for peer in self.le_connections if peer in self._le_peers]

    def _le_holds(self, other: "LinkLayer") -> bool:
        return any(peer is other for peer in self._le_held())

    def le_lose_contact(self) -> None:
        """Both ends of each of this side's LE connections hear nothing from the other from now
        on: each falls at its supervision timeout."""
        for address, connection in list(self.le_connections.items()):
            self._le_lose_contact(address)
            other = self._le_peers.get(address)
            if other is not None:
                other._le_lose_contact(connection.self_address)

    def _le_lose_contact(self, peer: hci.Address) -> None:
        if peer not in self.le_connections or peer in self._le_lost_contact:
            return
        loop = asyncio.get_running_loop()
        self._le_lost_contact[peer] = loop.time()
        timeout = self._le_timeouts.get(peer, SUPERVISION_TIMEOUT_DEFAULT)
        self._le_supervision[peer] = loop.call_at(
            loop.time() + timeout * SUPERVISION_UNIT_S, self._le_timed_out, peer
        )

    def _le_timed_out(self, peer: hci.Address) -> None:
        connection = self.le_connections.get(peer)
        if connection is not None:
            self.on_le_disconnected(connection, hci.HCI_ErrorCode.CONNECTION_TIMEOUT_ERROR)

    def on_le_disconnected(self, connection: Connection, reason: int) -> None:
        peer = connection.peer_address
        other = self._le_peers.pop(peer, None)
        self._le_timeouts.pop(peer, None)
        self._le_lost_contact.pop(peer, None)
        self._le_lengths_told.discard(peer)
        timer = self._le_supervision.pop(peer, None)
        if timer is not None:
            timer.cancel()
        super().on_le_disconnected(connection, reason)
        if other is not None:
            self._let_go_of(other)

    def _let_go_of(self, other: "LinkLayer") -> None:
        """Called when this side drops what it held for `other`: `Controller` lets the link go when
        neither side holds anything for the other."""

    def le_power_lost(self) -> None:
        """The controller lost its power: its LE connections are gone at once, its own host told,
        and its advertising, scanning and initiation stop; an initiation under way ends as a
        cancelled one, so that the host does not wait for it."""
        for peer in list(self.le_connections):
            self._le_timed_out(peer)
        self.le_legacy_advertiser.stop()
        self.le_scan_enable = False
        if self.pending_le_connection is not None:
            self._initiation_ended(self.pending_le_connection)

    def le_restart(self) -> None:
        """`HCI_Reset`, or a restart of the chip: the LE settings go back to their defaults."""
        self.le_legacy_advertiser.stop()
        self._le_defaults()
