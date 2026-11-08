"""Bumble's virtual controller, with what Bluetooth Classic devices use and Bumble 0.0.236 lacks.

Bumble's `Controller` on a `LocalLink` pages, connects, carries ACL data and pairs with Secure
Simple Pairing, but answers an inquiry with `UNKNOWN_HCI_COMMAND_ERROR`, has no legacy pairing
with a PIN, takes a stored link key without checking it with the peer, and has no encryption on a
Classic link. `Controller` adds them, for links whose controllers are all of this class:

- Inquiry: every other controller on the link whose inquiry scan is on answers at the instant
  the inquiry starts, in the order of their addresses, with its class of device, its signal
  strength and its extended inquiry response; `Inquiry_Complete` follows `inquiry_length` times
  1.28 s later, or at once when `num_responses` have answered. `Inquiry_Cancel` ends it without
  one.
- Authentication, as the specification orders it (Vol 2, Part C, 4.2; Vol 2, Part F, 4): the
  host is asked for a link key first; with one, the peer proves it holds the same key; without,
  the link pairs with Secure Simple Pairing when both hosts support it, as Bumble does, and with
  a PIN otherwise. Legacy pairing makes the initialisation key from the PIN, a combination key
  from a random number of each side, and checks it both ways; a PIN that differs from the
  peer's yields another key, and the check that fails detaches the link with
  `AUTHENTICATION_FAILURE_ERROR`.
- Encryption: `Set_Connection_Encryption` on an authenticated link changes it on both sides.
- Page timeout and link supervision (Vol 4, Part E, 6.6, 6.21, 7.3.16, 7.3.42), which Bumble does
  not have for Classic: a page nobody answers ends in `PAGE_TIMEOUT` after `Page_Timeout`, and a
  link that lost contact falls with `CONNECTION_TIMEOUT` after `Link_Supervision_Timeout`. Nothing
  polls on this link, so a link is in contact until a device leaves, and its timer starts there.
- Rejecting a page (Vol 4, Part E, 7.1.9), which Bumble has no handler for: the pager's
  `Connection_Complete` carries the reason as its status, and this side's host gets one too.
- A page this side hears is kept apart from its own page to the same address, which Bumble's
  handler writes it over, until its host accepts it: this side's own page keeps what its
  `Create_Connection` asked, whatever becomes of the page heard. Two pages at one instant, both
  accepted, make one link: the page accepted first. A late answer tells the host of no failure
  where this side holds an entry for the peer, and a role switch asked of a side that holds none
  is refused, where Bumble's handler asserts one.
- Page scan (Vol 4, Part E, 7.3.18), which Bumble's controller ignores: a device whose page scan is
  off does not hear a page, nor a remote name request without a link, which pages too, so either
  ends at the page timeout. As for a device that left, this is decided at the instant the page
  starts: one that turns page scan on during it answers only the next.
- What a host such as ESP-IDF's Bluedroid sends that Bumble has no handler for: the link's policy
  and packet types, its clock offset and the peer's version, the host's flow control, the event
  filters and the inquiry access codes, the scan activities and types (Vol 4, Part E, 7.1.14,
  7.1.23, 7.1.24, 7.2.10, 7.2.12, 7.3.3, 7.3.20, 7.3.22, 7.3.40, 7.3.45, 7.3.48, 7.3.52). A
  command this class does not know is answered as 4.5 requires, and kept in `unknown_commands`.
- `HCI_Reset` (7.3.2), which Bumble 0.0.236 answers and does nothing for: the controller loses
  what it had under way and goes back to its defaults.
- ACL data for the host cut to the host's buffer (7.3.39), where Bumble sends each L2CAP PDU whole.
- Page 0 of the features with the BR/EDR features it has.

The keys are not the specification's: SAFER+ (E1, E21, E22) is replaced by SHA-256 over the same
inputs, so a key or a response holds on this link only, and only the host sees a key. The random
numbers come from a generator seeded with the controller's address, so two runs exchange the same.
"""

import asyncio
import hashlib
import random
from collections import Counter
from dataclasses import dataclass, field

from bumble import core, hci, lmp
from bumble.controller import Connection
from bumble.controller import Controller as BumbleController

from esp32tl.bluetooth.air import Link

# Bluetooth's inquiry length unit: 1.28 s.
INQUIRY_UNIT_S = 1.28

# The signal strength a device answers an inquiry with, in dBm, unless the test gives another.
RSSI = -50

# A baseband slot, the unit of the page and link supervision timeouts, and their defaults: 5.12 s
# and 20 s (Vol 4, Part E, 7.3.16 and 7.3.42).
SLOT_S = 0.000625
PAGE_TIMEOUT_SLOTS = 0x2000
LINK_SUPERVISION_TIMEOUT_SLOTS = 0x7D00

# The reasons a host may reject a page for (Vol 4, Part E, 7.1.9).
REJECT_REASONS = range(0x0D, 0x10)

# The bits of `Scan_Enable` (Vol 4, Part E, 7.3.18).
INQUIRY_SCAN = 0x01
PAGE_SCAN = 0x02

# The inquiry access code every controller scans for until its host writes others (7.3.45): the
# General Inquiry Access Code. Those a host may write are 0x9E8B00 to 0x9E8B3F.
GIAC = hci.HCI_GENERAL_INQUIRY_LAP
IAC_LAPS = range(0x9E8B00, 0x9E8B40)

# Page 0 of the features: what this controller does on a BR/EDR link (Vol 2, Part C, 3.3).
# Multi-slot packets only name what the host may ask for in `Change_Connection_Packet_Type`: the
# link carries data whole, with no slots.
BR_EDR_FEATURES = (
    hci.LmpFeatureMask.LMP_3_SLOT_PACKETS
    | hci.LmpFeatureMask.LMP_5_SLOT_PACKETS
    | hci.LmpFeatureMask.ENCRYPTION
    | hci.LmpFeatureMask.ROLE_SWITCH
    | hci.LmpFeatureMask.RSSI_WITH_INQUIRY_RESULTS
    | hci.LmpFeatureMask.EXTENDED_INQUIRY_RESPONSE
    | hci.LmpFeatureMask.SECURE_SIMPLE_PAIRING_CONTROLLER_SUPPORT
    | hci.LmpFeatureMask.HCI_LINK_SUPERVISION_TIMEOUT_CHANGED_EVENT
    | hci.LmpFeatureMask.EXTENDED_FEATURES
)

# The settings of `Set_Event_Filter` that filter nothing (7.3.3): clear every filter; return the
# inquiry responses of all devices; and let connections from all devices through to the host,
# with `Auto_Accept_Flag` off (0x01). Others are refused.
CLEAR_ALL_FILTERS = 0x00
INQUIRY_RESULT_FILTER = 0x01
CONNECTION_SETUP_FILTER = 0x02
ALL_DEVICES = 0x00
NO_AUTO_ACCEPT = 0x01

# The packet boundary flags of ACL data for the host (Vol 4, Part E, 5.4.2): the first packet of
# an automatically flushable L2CAP PDU, and a continuing fragment.
FIRST_FLUSHABLE = 0b10
CONTINUING = 0b01


@lmp.Packet.subclass
@dataclass
class LmpInRand(lmp.Packet):
    opcode = lmp.Opcode.LMP_IN_RAND

    random_number: bytes = field(metadata=hci.metadata(16))


@lmp.Packet.subclass
@dataclass
class LmpCombKey(lmp.Packet):
    opcode = lmp.Opcode.LMP_COMB_KEY

    random_number: bytes = field(metadata=hci.metadata(16))


@lmp.Packet.subclass
@dataclass
class LmpSres(lmp.Packet):
    opcode = lmp.Opcode.LMP_SRES

    authentication_response: bytes = field(metadata=hci.metadata(4))


@lmp.Packet.subclass
@dataclass
class LmpEncryptionModeReq(lmp.Packet):
    opcode = lmp.Opcode.LMP_ENCRYPTION_MODE_REQ

    encryption_mode: int = field(metadata=hci.metadata(1))


@lmp.Packet.subclass
@dataclass
class LmpSupervisionTimeout(lmp.Packet):
    opcode = lmp.Opcode.LMP_SUPERVISION_TIMEOUT

    supervision_timeout: int = field(metadata=hci.metadata(2))


@dataclass
class Security:
    """Where one link's authentication stands, on one side of it."""

    # Whether this side's host asked for the authentication.
    initiator: bool = False
    # Legacy pairing: the initialisation key, and each side's random number for the combination
    # key.
    init_key: bytes | None = None
    local_rand: bytes | None = None
    peer_rand: bytes | None = None
    # The key the link is authenticated with, or is being paired to when `pairing`.
    link_key: bytes | None = None
    pairing: bool = False
    # The challenge this side sent, until the peer answers it.
    challenge: bytes | None = None
    # The peer's challenge, until this side's host gives the key to answer it.
    peer_challenge: bytes | None = None
    authenticated: bool = False


@dataclass
class HeardPage:
    """A page this side heard, until its host's answer makes it a link or it goes."""

    # The entry the link will be: the peripheral, with no handle yet.
    connection: Connection
    # Whether the host accepted it as the central, and it waits for the pager's role switch.
    switching: bool = False


@dataclass(frozen=True)
class UnknownCommand:
    """A command the controller does not know, at `t`, nanoseconds of the air's time: answered
    with `UNKNOWN_HCI_COMMAND_ERROR`."""

    t: int
    op_code: int
    name: str


def _hash(*parts: bytes) -> bytes:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(len(part).to_bytes(2, "little"))
        digest.update(part)
    return digest.digest()


def init_key(pin: bytes, in_rand: bytes, responder: hci.Address) -> bytes:
    """The initialisation key of legacy pairing, E22's stand-in."""
    return _hash(b"init", pin, in_rand, bytes(responder))[:16]


def combination_key(
    init: bytes,
    initiator_rand: bytes,
    responder_rand: bytes,
    initiator: hci.Address,
    responder: hci.Address,
) -> bytes:
    """The combination key both sides compute, E21's stand-in."""
    parts = (init, initiator_rand, responder_rand, bytes(initiator), bytes(responder))
    return _hash(b"comb", *parts)[:16]


def response(link_key: bytes, challenge: bytes, claimant: hci.Address) -> bytes:
    """The claimant's answer to `challenge`, E1's SRES stand-in."""
    return _hash(b"sres", link_key, challenge, bytes(claimant))[:4]


class Controller(BumbleController):
    """A Bumble controller at `address` on `link`, named `name` in Bumble's logs, which also does
    inquiry, legacy pairing, authentication with a stored key, encryption, a page timeout, link
    supervision, the rejection of a page and page scan, keeps a page it hears apart from its
    own, resets, answers the commands it does not know and cuts ACL data to the host's buffer.

    `acl_to_host` is the most ACL data a packet to the host carries, besides what the host's
    `Host_Buffer_Size` says: `None` for no bound of the transport's."""

    link: Link
    acl_to_host: int | None = None

    def __init__(self, name: str, link: Link, address: str) -> None:
        super().__init__(name, link=link, public_address=address)
        self.lmp_features = hci.LmpFeatureMask(
            (self.lmp_features & ~hci.LmpFeatureMask.BR_EDR_NOT_SUPPORTED) | BR_EDR_FEATURES
        )
        # What the controller's host was told it does not know, in order.
        self.unknown_commands: list[UnknownCommand] = []
        self.rssi = RSSI
        self._defaults()
        self._random = random.Random(str(self.public_address))
        self._inquiry: asyncio.TimerHandle | None = None
        self._security: dict[hci.Address, Security] = {}
        # The encryption mode this side asked of each peer, until the peer accepts it.
        self._encryption: dict[hci.Address, int] = {}
        # The page this side started to each peer, until it ends. Its entry in
        # `classic_connections` is this side's own page while the page is here, and a link after.
        self._paging: dict[hci.Address, asyncio.TimerHandle] = {}
        # The page this side heard from each peer, kept apart from its own page to that peer.
        self._heard: dict[hci.Address, HeardPage] = {}
        # The remote name request this side made of each peer, until it is answered.
        self._naming: dict[hci.Address, asyncio.TimerHandle] = {}
        # How many answers this side's host owes each peer that paged it, and how many of those
        # are late, for a page heard that went before they came. An answer names only the address.
        self._answers_due: Counter[hci.Address] = Counter()
        self._late_answers: Counter[hci.Address] = Counter()
        # Each link's timeout in slots, where a host or the central changed it; 0 for none.
        self._supervision_timeouts: dict[hci.Address, int] = {}
        # The time each link lost contact, on the loop's clock, and the timer it falls by.
        self._lost_contact: dict[hci.Address, float] = {}
        self._supervision: dict[hci.Address, asyncio.TimerHandle] = {}

    def _defaults(self) -> None:
        """The values the specification gives the host's settings, which `HCI_Reset` brings back
        (Vol 4, Part E, 7.3.2)."""
        self.class_of_device = 0
        self.extended_inquiry_response = bytes(240)
        self.inquiry_mode = 0
        self.page_timeout = PAGE_TIMEOUT_SLOTS
        self.classic_scan_enable = 0
        # The inquiry access codes this side's inquiry scan answers (7.3.45).
        self.iac_laps = [GIAC]
        # Kept, and change nothing: the link has no hold or sniff mode, and a role switch is
        # allowed or not as `Create_Connection` and Bumble's `classic_allow_role_switch` say.
        self.default_link_policy = 0
        self.link_policies: dict[int, int] = {}
        # Kept, and change nothing: the link has no radio, so no scan has a timing or a type.
        self.inquiry_scan_activity = (0x1000, 0x0012)
        self.page_scan_activity = (0x0800, 0x0012)
        self.inquiry_scan_type = 0
        self.page_scan_type = 0
        # The host's buffer for ACL data from this side, from `Host_Buffer_Size` (7.3.39): until it
        # says, the host takes any length.
        self.host_acl_length: int | None = None
        self.lmp_features &= ~(
            hci.LmpFeatureMask.SECURE_SIMPLE_PAIRING_HOST_SUPPORT
            | hci.LmpFeatureMask.LE_SUPPORTED_HOST
        )

    def random_bytes(self, n: int) -> bytes:
        return self._random.randbytes(n)

    # The local device's description.

    def on_hci_write_class_of_device_command(
        self, command: hci.HCI_Write_Class_Of_Device_Command
    ) -> hci.HCI_StatusReturnParameters:
        self.class_of_device = command.class_of_device
        return hci.HCI_StatusReturnParameters(hci.HCI_ErrorCode.SUCCESS)

    def on_hci_read_class_of_device_command(
        self, _command: hci.HCI_Read_Class_Of_Device_Command
    ) -> hci.HCI_Read_Class_Of_Device_ReturnParameters:
        return hci.HCI_Read_Class_Of_Device_ReturnParameters(
            status=hci.HCI_ErrorCode.SUCCESS, class_of_device=self.class_of_device
        )

    def on_hci_write_extended_inquiry_response_command(
        self, command: hci.HCI_Write_Extended_Inquiry_Response_Command
    ) -> hci.HCI_StatusReturnParameters:
        self.extended_inquiry_response = command.extended_inquiry_response.ljust(240, b"\0")
        return hci.HCI_StatusReturnParameters(hci.HCI_ErrorCode.SUCCESS)

    def on_hci_write_inquiry_mode_command(
        self, command: hci.HCI_Write_Inquiry_Mode_Command
    ) -> hci.HCI_StatusReturnParameters:
        self.inquiry_mode = command.inquiry_mode
        return hci.HCI_StatusReturnParameters(hci.HCI_ErrorCode.SUCCESS)

    def on_hci_write_secure_connections_host_support_command(
        self, _command: hci.HCI_Write_Secure_Connections_Host_Support_Command
    ) -> hci.HCI_StatusReturnParameters:
        # Secure Connections over BR/EDR is not modelled; legacy pairing and Bumble's SSP are.
        return hci.HCI_StatusReturnParameters(hci.HCI_ErrorCode.SUCCESS)

    # What the controller says of itself.

    def on_hci_read_local_extended_features_command(
        self, command: hci.HCI_Read_Local_Extended_Features_Command
    ) -> hci.HCI_Read_Local_Extended_Features_ReturnParameters:
        # Bumble's handler gives empty features for the page after the last, which is not one.
        if command.page_number > self.lmp_features_max_page_number:
            return hci.HCI_Read_Local_Extended_Features_ReturnParameters(
                status=hci.HCI_ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR,
                page_number=command.page_number,
                maximum_page_number=self.lmp_features_max_page_number,
                extended_lmp_features=bytes(8),
            )
        return super().on_hci_read_local_extended_features_command(command)

    # Settings that change nothing on this link, which has no radio: no scan has a timing or a
    # type, and no link a hold or a sniff mode.

    def on_hci_write_default_link_policy_settings_command(
        self, command: hci.HCI_Write_Default_Link_Policy_Settings_Command
    ) -> hci.HCI_StatusReturnParameters:
        """Vol 4, Part E, 7.2.12: the policy of the links to come, kept."""
        self.default_link_policy = command.default_link_policy_settings
        return hci.HCI_StatusReturnParameters(hci.HCI_ErrorCode.SUCCESS)

    def on_hci_write_link_policy_settings_command(
        self, command: hci.HCI_Write_Link_Policy_Settings_Command
    ) -> hci.HCI_StatusAndConnectionHandleReturnParameters:
        """7.2.10: one link's policy, kept."""
        handle = command.connection_handle
        if self._link_of(handle) is None:
            status = hci.HCI_ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
        else:
            status = hci.HCI_ErrorCode.SUCCESS
            self.link_policies[handle] = command.link_policy_settings
        return hci.HCI_StatusAndConnectionHandleReturnParameters(status, handle)

    def on_hci_write_inquiry_scan_activity_command(
        self, command: hci.HCI_Write_Inquiry_Scan_Activity_Command
    ) -> hci.HCI_StatusReturnParameters:
        """7.3.22: kept."""
        self.inquiry_scan_activity = (command.inquiry_scan_interval, command.inquiry_scan_window)
        return hci.HCI_StatusReturnParameters(hci.HCI_ErrorCode.SUCCESS)

    def on_hci_write_page_scan_activity_command(
        self, command: hci.HCI_Write_Page_Scan_Activity_Command
    ) -> hci.HCI_StatusReturnParameters:
        """7.3.20: kept."""
        self.page_scan_activity = (command.page_scan_interval, command.page_scan_window)
        return hci.HCI_StatusReturnParameters(hci.HCI_ErrorCode.SUCCESS)

    def on_hci_write_inquiry_scan_type_command(
        self, command: hci.HCI_Write_Inquiry_Scan_Type_Command
    ) -> hci.HCI_StatusReturnParameters:
        """7.3.48: kept."""
        self.inquiry_scan_type = command.scan_type
        return hci.HCI_StatusReturnParameters(hci.HCI_ErrorCode.SUCCESS)

    def on_hci_write_page_scan_type_command(
        self, command: hci.HCI_Write_Page_Scan_Type_Command
    ) -> hci.HCI_StatusReturnParameters:
        """7.3.52: kept."""
        self.page_scan_type = command.page_scan_type
        return hci.HCI_StatusReturnParameters(hci.HCI_ErrorCode.SUCCESS)

    def on_hci_set_event_filter_command(
        self, command: hci.HCI_Set_Event_Filter_Command
    ) -> hci.HCI_StatusReturnParameters:
        """7.3.3: the settings that filter nothing are taken, as no filter is set until a host
        sets one; one that would filter is refused with `UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE`,
        since no event is filtered here. Bluedroid's discovery fails unless the first is taken."""
        condition = bytes(command.filter_condition)
        filters_nothing = command.filter_type == CLEAR_ALL_FILTERS or (
            command.filter_type == INQUIRY_RESULT_FILTER and condition[:1] == bytes([ALL_DEVICES])
        )
        filters_nothing |= command.filter_type == CONNECTION_SETUP_FILTER and condition == bytes(
            [ALL_DEVICES, NO_AUTO_ACCEPT]
        )
        if filters_nothing:
            return hci.HCI_StatusReturnParameters(hci.HCI_ErrorCode.SUCCESS)
        return hci.HCI_StatusReturnParameters(
            hci.HCI_ErrorCode.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR
        )

    def on_hci_write_current_iac_lap_command(self, command: hci.HCI_Command) -> None:
        """7.3.45: the inquiry access codes this side's inquiry scan answers from now on, which
        an inquiry's `LAP` must be among. Bumble 0.0.236 has no class for the command, so its
        `Command_Complete` is sent here."""
        parameters = bytes(command.parameters)
        count = parameters[0] if parameters else 0
        laps = [
            int.from_bytes(parameters[at : at + 3], "little")
            for at in range(1, len(parameters) - 2, 3)
        ]
        if (
            not 1 <= count <= 0x40
            or len(parameters) != 1 + 3 * count
            or not all(lap in IAC_LAPS for lap in laps)
        ):
            status = hci.HCI_ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR
        else:
            status = hci.HCI_ErrorCode.SUCCESS
            self.iac_laps = laps
        self._command_complete(command, status)

    # Flow control of the host.

    def on_hci_host_buffer_size_command(
        self, command: hci.HCI_Host_Buffer_Size_Command
    ) -> hci.HCI_StatusReturnParameters:
        """7.3.39: this side cuts the ACL data it sends the host to the host's packet length."""
        length = command.host_acl_data_packet_length
        self.host_acl_length = length if length > 0 else None
        return hci.HCI_StatusReturnParameters(hci.HCI_ErrorCode.SUCCESS)

    def on_hci_host_number_of_completed_packets_command(self, command: hci.HCI_Command) -> None:
        """7.3.40: the host frees its buffers. No event comes of it; this side does not hold its
        data back for the host's count of buffers (`Host_Total_Num_ACL_Data_Packets`)."""

    # The link: commands about one link, answered by this side's link manager.

    def on_hci_change_connection_packet_type_command(
        self, command: hci.HCI_Change_Connection_Packet_Type_Command
    ) -> None:
        """7.1.14: the packet types are kept by no one, since the link carries data whole, and
        `Connection_Packet_Type_Changed` (7.7.29) follows at once."""
        connection = self._linked_command(command.connection_handle, command.op_code)
        if connection is not None:
            self.send_hci_packet(
                hci.HCI_Connection_Packet_Type_Changed_Event(
                    status=hci.HCI_ErrorCode.SUCCESS,
                    connection_handle=connection.handle,
                    packet_type=command.packet_type,
                )
            )

    def on_hci_read_clock_offset_command(self, command: hci.HCI_Read_Clock_Offset_Command) -> None:
        """7.1.24: the offset is 0, as in an inquiry's results: the link has no clocks."""
        connection = self._linked_command(command.connection_handle, command.op_code)
        if connection is not None:
            self.send_hci_packet(
                hci.HCI_Read_Clock_Offset_Complete_Event(
                    status=hci.HCI_ErrorCode.SUCCESS,
                    connection_handle=connection.handle,
                    clock_offset=0,
                )
            )

    def on_hci_read_remote_version_information_command(
        self, command: hci.HCI_Read_Remote_Version_Information_Command
    ) -> None:
        """7.1.23: the version the peer's controller has, what `LMP_version_req` would tell, read
        from it on the link. A peer that cannot be reached does not answer: the link falls at its
        timeout."""
        connection = self._linked_command(command.connection_handle, command.op_code)
        if connection is None:
            return
        peer = connection.peer_address
        other = self.link.find_classic_controller(peer)
        if other is None or not self.link.reaches(peer, self.public_address):
            return
        self.send_hci_packet(
            hci.HCI_Read_Remote_Version_Information_Complete_Event(
                status=hci.HCI_ErrorCode.SUCCESS,
                connection_handle=connection.handle,
                version=other.lmp_version,
                manufacturer_name=other.manufacturer_company_identifier,
                subversion=other.lmp_subversion,
            )
        )

    def _link_of(self, handle: int) -> Connection | None:
        """The link with `handle`, if there is one: not a page, whose entry has handle 0."""
        connection = self.find_classic_connection_by_handle(handle)
        if connection is None or not self._has_link(connection.peer_address):
            return None
        return connection

    def _linked_command(self, handle: int, op_code: int) -> Connection | None:
        """The command status of a command about the link with `handle`, and the link, or
        `UNKNOWN_CONNECTION_IDENTIFIER` and `None` when there is none."""
        connection = self._link_of(handle)
        if connection is None:
            self._send_hci_command_status(
                hci.HCI_ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR, op_code
            )
            return None
        self._send_hci_command_status(hci.HCI_COMMAND_STATUS_PENDING, op_code)
        return connection

    # What the controller does not know.

    def on_hci_command(  # type: ignore[override]
        self, command: hci.HCI_Command
    ) -> hci.HCI_StatusReturnParameters | None:
        """A command with no handler (Vol 4, Part E, 4.5): `UNKNOWN_HCI_COMMAND_ERROR`, in the
        event the host waits for, `Command_Status` for a command that has one before its
        completion, `Command_Complete` for any other, also one Bumble has no class for. 4.5
        leaves the event to the vendor; Bumble 0.0.236 answers nothing to the first kind, and the
        host waits for ever. Kept in `unknown_commands`."""
        loop = asyncio.get_running_loop()
        self.unknown_commands.append(
            UnknownCommand(round(loop.time() * 1e9), command.op_code, command.name)
        )
        status = hci.HCI_ErrorCode.UNKNOWN_HCI_COMMAND_ERROR
        if isinstance(command, hci.HCI_SyncCommand):
            return hci.HCI_StatusReturnParameters(status)
        if isinstance(command, hci.HCI_AsyncCommand):
            self._send_hci_command_status(status, command.op_code)
        else:
            self._command_complete(command, status)
        return None

    def _command_complete(self, command: hci.HCI_Command, status: hci.HCI_ErrorCode) -> None:
        """`Command_Complete` with only a status, for a command Bumble has no class for, which
        its dispatch does not answer."""
        self.send_hci_packet(
            hci.HCI_Command_Complete_Event(
                num_hci_command_packets=1,
                command_opcode=command.op_code,
                return_parameters=hci.HCI_StatusReturnParameters(status),
            )
        )

    # Reset.

    def on_hci_reset_command(
        self, command: hci.HCI_Reset_Command
    ) -> hci.HCI_StatusReturnParameters:
        """Vol 4, Part E, 7.3.2: "the current operational state will be lost, the Controller will
        enter standby mode and the Controller will automatically revert to the default values".
        The radio stops for a moment: each peer loses contact, and its end falls at its
        supervision timeout, as when this device leaves; this side ends what it had under way and
        lets every link go, as on a power loss. Its host hears nothing of it: it asked for the
        reset, and the `Command_Complete` is all 7.3.2 gives it. Bumble 0.0.236's own handler keeps
        everything."""
        self._restart()
        self._defaults()
        return super().on_hci_reset_command(command)

    def _restart(self, back: bool = True) -> None:
        """The controller starts again, with its host told nothing: every peer loses contact,
        and this side keeps nothing it held for one, nor any answer its host owed. Without
        `back` it stays off the air, until `returned`."""
        host = self.host
        self.host = None
        try:
            present = self.link.present(self.public_address)
            if present:
                self.left()
            self.power_lost()
            if present and back:
                self.returned()
        finally:
            self.host = host
        self._heard.clear()
        self._answers_due.clear()
        self._late_answers.clear()
        self._security.clear()
        self._encryption.clear()
        self._supervision_timeouts.clear()
        self._lost_contact.clear()
        for timer in self._supervision.values():
            timer.cancel()
        self._supervision.clear()
        # What the link manager waited for from a peer: an answer that comes now finds nothing.
        self.classic_pending_commands.clear()
        self.sco_links.clear()

    # ACL data for the host, cut to its buffer.

    def on_link_acl_data(
        self, sender_address: hci.Address, transport: core.PhysicalTransport, data: bytes
    ) -> None:
        """An L2CAP PDU from `sender_address`, sent to the host in packets of at most the host's
        `Host_Buffer_Size` and `acl_to_host`, which 7.3.39 requires: Bumble sends it whole."""
        connection = self.classic_connections.get(sender_address)
        if transport != core.PhysicalTransport.BR_EDR or connection is None:
            super().on_link_acl_data(sender_address, transport, data)
            return
        bounds = [b for b in (self.host_acl_length, self.acl_to_host) if b is not None]
        size = min(bounds) if bounds else max(len(data), 1)
        for at in range(0, max(len(data), 1), size):
            fragment = data[at : at + size]
            self.send_hci_packet(
                hci.HCI_AclDataPacket(
                    connection.handle,
                    FIRST_FLUSHABLE if at == 0 else CONTINUING,
                    0,
                    len(fragment),
                    fragment,
                )
            )

    # Inquiry.

    def on_hci_inquiry_command(self, command: hci.HCI_Inquiry_Command) -> None:
        if self._inquiry is not None:
            self._send_hci_command_status(
                hci.HCI_ErrorCode.COMMAND_DISALLOWED_ERROR, command.op_code
            )
            return
        self._send_hci_command_status(hci.HCI_ErrorCode.SUCCESS, command.op_code)
        loop = asyncio.get_running_loop()
        self._inquiry = loop.call_later(
            command.inquiry_length * INQUIRY_UNIT_S, self._inquiry_complete
        )
        # A device that left hears nobody; it still waits out the inquiry's length.
        heard = self.link.controllers if self.link.present(self.public_address) else ()
        others = sorted(
            (
                c
                for c in heard
                if c is not self
                and isinstance(c, Controller)
                and c.classic_scan_enable & INQUIRY_SCAN
                and command.lap in c.iac_laps
                and self.link.present(c.public_address)
            ),
            key=lambda c: bytes(reversed(bytes(c.public_address))),
        )
        for answered, other in enumerate(others, start=1):
            self.send_hci_packet(self._inquiry_result(other))
            if answered == command.num_responses:
                self._inquiry.cancel()
                self._inquiry_complete()
                break

    def _inquiry_result(self, other: "Controller") -> hci.HCI_Event:
        if self.inquiry_mode == hci.HCI_EXTENDED_INQUIRY_MODE:
            return hci.HCI_Extended_Inquiry_Result_Event(
                num_responses=1,
                bd_addr=other.public_address,
                page_scan_repetition_mode=0,
                reserved=0,
                class_of_device=other.class_of_device,
                clock_offset=0,
                rssi=other.rssi,
                extended_inquiry_response=other.extended_inquiry_response,
            )
        if self.inquiry_mode == hci.HCI_INQUIRY_WITH_RSSI_MODE:
            return hci.HCI_Inquiry_Result_With_RSSI_Event(
                bd_addr=[other.public_address],
                page_scan_repetition_mode=[0],
                reserved=[0],
                class_of_device=[other.class_of_device],
                clock_offset=[0],
                rssi=[other.rssi],
            )
        return hci.HCI_Inquiry_Result_Event(
            bd_addr=[other.public_address],
            page_scan_repetition_mode=[0],
            reserved_0=[0],
            reserved_1=[0],
            class_of_device=[other.class_of_device],
            clock_offset=[0],
        )

    def _inquiry_complete(self) -> None:
        self._inquiry = None
        self.send_hci_packet(hci.HCI_Inquiry_Complete_Event(status=hci.HCI_ErrorCode.SUCCESS))

    def on_hci_inquiry_cancel_command(
        self, _command: hci.HCI_Inquiry_Cancel_Command
    ) -> hci.HCI_StatusReturnParameters:
        if self._inquiry is None:
            return hci.HCI_StatusReturnParameters(hci.HCI_ErrorCode.COMMAND_DISALLOWED_ERROR)
        self._inquiry.cancel()
        self._inquiry = None
        return hci.HCI_StatusReturnParameters(hci.HCI_ErrorCode.SUCCESS)

    # Paging: a page nobody answers ends at the page timeout.

    def on_hci_write_page_timeout_command(
        self, command: hci.HCI_Write_Page_Timeout_Command
    ) -> hci.HCI_StatusReturnParameters:
        if command.page_timeout == 0:
            return hci.HCI_StatusReturnParameters(
                hci.HCI_ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR
            )
        self.page_timeout = command.page_timeout
        return hci.HCI_StatusReturnParameters(hci.HCI_ErrorCode.SUCCESS)

    def on_hci_create_connection_command(self, command: hci.HCI_Create_Connection_Command) -> None:
        peer = command.bd_addr
        if peer in self.classic_connections or peer in self._heard:
            # Bumble would make a second connection over the first, and the page's timer would
            # end the next one. A page this side heard waits for its host's answer.
            self._send_hci_command_status(
                hci.HCI_ErrorCode.CONNECTION_ALREADY_EXISTS_ERROR, command.op_code
            )
            return
        super().on_hci_create_connection_command(command)
        # Bumble refuses a page while an LE connection is pending, and makes no entry for it.
        if peer in self.classic_connections:
            self._paging[peer] = asyncio.get_running_loop().call_later(
                self.page_timeout * SLOT_S, self._page_timed_out, peer
            )

    def _page_timed_out(self, peer: hci.Address) -> None:
        self.on_classic_connection_complete(peer, hci.HCI_ErrorCode.PAGE_TIMEOUT_ERROR)

    def on_classic_connection_complete(self, peer_address: hci.Address, status: int) -> None:
        page = self._paging.pop(peer_address, None)
        if page is not None:
            page.cancel()
        if status != hci.HCI_ErrorCode.SUCCESS:
            if page is not None:
                # Bumble keeps the entry it made for this side's own page.
                self.classic_connections.pop(peer_address, None)
            heard = self._heard.get(peer_address)
            if heard is not None and heard.switching:
                # The event names only the address: to the host it ends its acceptance too.
                del self._heard[peer_address]
            if not self._holds(peer_address):
                self._forget_link(peer_address)
        elif page is not None:
            # This side's own page made the link, or ended with the one a page heard made: a page
            # still heard from the peer can make none.
            self._drop_heard(peer_address)
        super().on_classic_connection_complete(peer_address, status)
        if status != hci.HCI_ErrorCode.SUCCESS:
            self._let_go(peer_address)

    def on_classic_connection_request(self, peer_address: hci.Address, link_type: int) -> None:
        if link_type != hci.HCI_Connection_Complete_Event.LinkType.ACL:
            super().on_classic_connection_request(peer_address, link_type)
            return
        # Bumble's handler writes it in `classic_connections`, over this side's own page.
        self._heard[peer_address] = HeardPage(
            Connection(
                controller=self,
                handle=0,
                role=hci.Role.PERIPHERAL,
                self_address=self.public_address,
                peer_address=peer_address,
                link=self.link,
                transport=core.PhysicalTransport.BR_EDR,
                link_type=link_type,
                classic_allow_role_switch=self.classic_allow_role_switch,
            )
        )
        self._answers_due[peer_address] += 1
        self.send_hci_packet(
            hci.HCI_Connection_Request_Event(
                bd_addr=peer_address, class_of_device=0, link_type=link_type
            )
        )

    def on_hci_accept_connection_request_command(
        self, command: hci.HCI_Accept_Connection_Request_Command
    ) -> None:
        peer = command.bd_addr
        heard = self._take_answer(peer, command.op_code)
        if heard is None:
            return
        self._send_hci_command_status(hci.HCI_ErrorCode.SUCCESS, command.op_code)
        if command.role != hci.Role.CENTRAL:
            self._accepted(peer, heard)
            return
        # The role switch comes before the acceptance, as in Bumble's handler.
        heard.switching = True
        future = self.send_lmp_packet(peer, lmp.LmpSwitchReq())
        future.add_done_callback(lambda done: self._role_switched(peer, heard, done.result()))

    def _role_switched(self, peer: hci.Address, heard: HeardPage, status: int) -> None:
        if self._heard.get(peer) is not heard:
            # It went while the switch was on its way.
            return
        if status == hci.HCI_ErrorCode.SUCCESS:
            self.classic_role_change(heard.connection)
            self._accepted(peer, heard)
            return
        del self._heard[peer]
        if self._own_page_accepted(peer):
            # The peer accepted this side's own page meanwhile, which makes the link: this
            # acceptance goes as a late answer, since the event names only the address.
            return
        self.send_lmp_packet(peer, lmp.LmpNotAccepted(lmp.Opcode.LMP_HOST_CONNECTION_REQ, status))
        self.on_classic_connection_complete(peer, status)

    def on_classic_role_change_request(self, peer_address: hci.Address) -> None:
        if peer_address not in self.classic_connections or (
            peer_address not in self._paging and self._switching_for_a_page(peer_address)
        ):
            # A switch the peer asks before it accepts this side's page belongs to that page: one
            # that ended here, which left no entry, where Bumble's handler asserts one, or a link
            # made of the peer's own page instead, whose roles are its own.
            self.send_lmp_packet(
                peer_address,
                lmp.LmpNotAccepted(
                    lmp.Opcode.LMP_SWITCH_REQ, hci.HCI_ErrorCode.ROLE_CHANGE_NOT_ALLOWED_ERROR
                ),
            )
            return
        super().on_classic_role_change_request(peer_address)

    def _switching_for_a_page(self, peer: hci.Address) -> bool:
        """Whether `peer`'s host accepted a page of this side as the central, and waits for the
        role switch."""
        other = self.link.find_classic_controller(peer)
        if not isinstance(other, Controller):
            return False
        heard = other._heard.get(self.public_address)
        return heard is not None and heard.switching

    def _accepted(self, peer: hci.Address, heard: HeardPage) -> None:
        """The page `heard` becomes the link, in place of this side's own page to `peer`, which
        ends with it."""
        del self._heard[peer]
        self.send_lmp_packet(peer, lmp.LmpAccepted(lmp.Opcode.LMP_HOST_CONNECTION_REQ))
        self.classic_connections[peer] = heard.connection
        self.on_classic_connection_complete(peer, hci.HCI_ErrorCode.SUCCESS)

    def on_hci_reject_connection_request_command(
        self, command: hci.HCI_Reject_Connection_Request_Command
    ) -> None:
        peer = command.bd_addr
        if command.reason not in REJECT_REASONS:
            # A refused command answers nothing: the host still owes its answer.
            self._send_hci_command_status(
                hci.HCI_ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR, command.op_code
            )
            return
        if self._take_answer(peer, command.op_code) is None:
            return
        self._send_hci_command_status(hci.HCI_ErrorCode.SUCCESS, command.op_code)
        self.send_lmp_packet(
            peer, lmp.LmpNotAccepted(lmp.Opcode.LMP_HOST_CONNECTION_REQ, command.reason)
        )
        # The page goes, with what a page whose pager left has running; this side's own page stays.
        # What is still owed stays as it was: a host may never give it, and then it must not take
        # the next page's answer.
        del self._heard[peer]
        self._forget_link(peer)
        self._let_go(peer)
        self._tell_host_no_link(peer, command.reason)

    def _take_answer(self, peer: hci.Address, op_code: int) -> HeardPage | None:
        """Counts an answer of this side's host to a page from `peer`, and gives the page it
        answers, if this side still holds one with no link to `peer`, and the peer has not accepted
        this side's own page. If not, this has told the host: an answer nothing was owed for is its
        own error, and one with no page left makes no link, and what this side holds for the peer
        now, a link or its own page, is not this answer's."""
        if self._answers_due[peer] == 0:
            self._send_hci_command_status(
                hci.HCI_ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR, op_code
            )
            return None
        self._answers_due[peer] -= 1
        heard = self._heard.get(peer)
        if self._late_answers[peer] > 0:
            self._late_answers[peer] -= 1
        elif heard is not None and not heard.switching and not self._has_link(peer):
            if not self._own_page_accepted(peer):
                return heard
            # This side's own page makes the link, or fails with the peer's acceptance.
            self._drop_heard(peer)
        self._send_hci_command_status(hci.HCI_ErrorCode.SUCCESS, op_code)
        self._tell_host_no_link(peer, hci.HCI_ErrorCode.CONNECTION_TIMEOUT_ERROR)
        return None

    def _tell_host_no_link(self, peer: hci.Address, status: int) -> None:
        """No link comes of the page this side's host answered. The event names only the address:
        to the host it would end this side's own page, so that page goes on to end by itself, and
        it would fail a working link, which goes on as it is."""
        if peer not in self.classic_connections:
            super().on_classic_connection_complete(peer, status)

    def _own_page_accepted(self, peer: hci.Address) -> bool:
        """Whether the peer's host accepted this side's own page, while that page is under way: the
        acceptance, or the role switch before it, is on its way, and a page heard from the peer can
        make no link. With two pages at one instant, the first acceptance makes the one link."""
        other = self.link.find_classic_controller(peer)
        if peer not in self._paging or not isinstance(other, Controller):
            return False
        if other._has_link(self.public_address):
            return True
        # The role switch goes to this side's own entry, which refuses it unless `Create_Connection`
        # allowed one, and a refusal ends both pages: then the page heard makes the link.
        own = self.classic_connections[peer]
        return self._switching_for_a_page(peer) and own.classic_allow_role_switch

    def _drop_heard(self, peer: hci.Address) -> None:
        """The page this side heard from `peer`, if any, goes with no link. The answers still owed
        for it are late; one given already is not owed."""
        heard = self._heard.pop(peer, None)
        if heard is not None and not heard.switching:
            self._late_answers[peer] = self._answers_due[peer]

    def _has_link(self, peer: hci.Address) -> bool:
        """Whether this side's entry for `peer` in `classic_connections` is a link: its own page
        has one too, while that page is under way."""
        return peer in self.classic_connections and peer not in self._paging

    def _holds(self, peer: hci.Address) -> bool:
        """Whether this side holds more for `peer` than a page of its own: a link, made or being
        made, or a page it heard."""
        return peer in self._heard or self._has_link(peer)

    def held(self) -> list[hci.Address]:
        """The peers this side holds anything for: a page of its own, a link or a page it heard."""
        return list(dict.fromkeys([*self.classic_connections, *self._heard]))

    # Page scan: a device whose page scan is off hears no page.

    def _hears(self, sender: hci.Address, packet: lmp.Packet) -> bool:
        """Whether `packet` from `sender` reaches this side's link manager: a page needs page
        scan, and so does a name request that has no link to go over."""
        if self.classic_scan_enable & PAGE_SCAN:
            return True
        if isinstance(packet, lmp.LmpHostConnectionReq):
            return False
        if isinstance(packet, lmp.LmpNameReq):
            return self._linked(sender)
        return True

    def _linked(self, peer: hci.Address) -> bool:
        """Whether this side and `peer` have a link, made or being made: one of them heard the
        other's page."""
        other = self.link.find_classic_controller(peer)
        return self._holds(peer) or (
            isinstance(other, Controller) and other._holds(self.public_address)
        )

    # Remote name requests: a page too, so one nobody answers ends at the page timeout.

    def on_hci_remote_name_request_command(
        self, command: hci.HCI_Remote_Name_Request_Command
    ) -> None:
        peer = command.bd_addr
        if peer not in self._naming:
            self._naming[peer] = asyncio.get_running_loop().call_later(
                self.page_timeout * SLOT_S, self._name_timed_out, peer
            )
        super().on_hci_remote_name_request_command(command)

    def on_classic_remote_name_response(
        self, peer_address: hci.Address, name_offset: int, name_length: int, name_fregment: bytes
    ) -> None:
        timer = self._naming.pop(peer_address, None)
        if timer is not None:
            timer.cancel()
        super().on_classic_remote_name_response(
            peer_address, name_offset, name_length, name_fregment
        )

    def _name_timed_out(self, peer: hci.Address) -> None:
        timer = self._naming.pop(peer, None)
        if timer is not None:
            timer.cancel()
        self.send_hci_packet(
            hci.HCI_Remote_Name_Request_Complete_Event(
                status=hci.HCI_ErrorCode.PAGE_TIMEOUT_ERROR, bd_addr=peer, remote_name=b""
            )
        )

    # Link supervision: a link that lost contact falls at its timeout.

    def on_hci_write_link_supervision_timeout_command(
        self, command: hci.HCI_Write_Link_Supervision_Timeout_Command
    ) -> hci.HCI_Write_Link_Supervision_Timeout_ReturnParameters:
        connection = self.find_classic_connection_by_handle(command.handle)
        if connection is None:
            status = hci.HCI_ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
        elif connection.role != hci.Role.CENTRAL:
            status = hci.HCI_ErrorCode.COMMAND_DISALLOWED_ERROR
        else:
            status = hci.HCI_ErrorCode.SUCCESS
            timeout = command.link_supervision_timeout
            self._set_supervision_timeout(connection.peer_address, timeout)
            self.send_lmp_packet(
                connection.peer_address, LmpSupervisionTimeout(supervision_timeout=timeout)
            )
        return hci.HCI_Write_Link_Supervision_Timeout_ReturnParameters(
            status=status, handle=command.handle
        )

    def _set_supervision_timeout(self, peer: hci.Address, timeout: int) -> None:
        self._supervision_timeouts[peer] = timeout
        if peer in self._lost_contact:
            self._supervise(peer)

    def _lose_contact(self, peer: hci.Address) -> None:
        """No packet comes from `peer` from now on. This side's own page has no contact to lose:
        it ends by itself; a link that lost it already goes on counting from then."""
        if self._holds(peer) and peer not in self._lost_contact:
            self._lost_contact[peer] = asyncio.get_running_loop().time()
            self._supervise(peer)

    def _supervise(self, peer: hci.Address) -> None:
        """The link to `peer` falls at its timeout after it lost contact, if it has a timeout."""
        timer = self._supervision.pop(peer, None)
        if timer is not None:
            timer.cancel()
        timeout = self._supervision_timeouts.get(peer, LINK_SUPERVISION_TIMEOUT_SLOTS)
        if timeout != 0:
            when = self._lost_contact[peer] + timeout * SLOT_S
            loop = asyncio.get_running_loop()
            self._supervision[peer] = loop.call_at(when, self._link_timed_out, peer)

    def _link_timed_out(self, peer: hci.Address) -> None:
        """The link to `peer` falls, and so does a page this side heard from it: no link was made
        of that page to tell the host of, unless the host accepted it as the central, and waits.
        This side's own page ends by itself."""
        reason = hci.HCI_ErrorCode.CONNECTION_TIMEOUT_ERROR
        security = self._security.get(peer)
        if security is not None and security.initiator and not security.authenticated:
            self._authentication_complete(peer, reason)
        heard = self._heard.get(peer)
        self._drop_heard(peer)
        if heard is not None and heard.switching:
            # The role switch was lost on the way: the acceptance ends here.
            self._tell_host_no_link(peer, reason)
        if self._has_link(peer):
            self.on_classic_disconnected(peer, reason)
        else:
            self._forget_link(peer)
            self._let_go(peer)

    # Leaving: called by the device that leaves, on its own controller.

    def left(self) -> None:
        """This device is gone from the air, without a word: both ends of each of its links lose
        contact, and so does whoever holds a page of this device that it holds nothing for itself,
        as after that page ended or crossed one this device rejected. A page of its own that nobody
        heard is no link: it ends by itself; a page of another that it never heard is none either.
        One the target heard, the target holds, and only that end has a timeout to wait for."""
        mine = [peer for peer in self.held() if self._holds(peer)]
        holders = [
            other.public_address
            for other in self.link.controllers
            if isinstance(other, Controller)
            and other is not self
            and other._holds(self.public_address)
        ]
        peers = mine + holders
        self.link.leave(self.public_address, peers)
        for peer in peers:
            self._lose_contact(peer)
            other = self.link.find_classic_controller(peer)
            if isinstance(other, Controller):
                other._lose_contact(self.public_address)

    def _held_by(self, peer: hci.Address) -> bool:
        other = self.link.find_classic_controller(peer)
        return isinstance(other, Controller) and self.public_address in other.held()

    def returned(self) -> None:
        """This device is on the air again. The links it left stay severed until they fall."""
        self.link.back(self.public_address)

    def power_lost(self) -> None:
        """This device lost its power: what it has under way ends, and its end of each link is
        gone at once. Only its own host hears of it."""
        for peer in list(self._paging):
            self._page_timed_out(peer)
        for peer in list(self._naming):
            self._name_timed_out(peer)
        if self._inquiry is not None:
            self._inquiry.cancel()
            self._inquiry_complete()
        for peer in self.held():
            self._link_timed_out(peer)

    # Authentication: this side's host asks.

    def on_hci_authentication_requested_command(
        self, command: hci.HCI_Authentication_Requested_Command
    ) -> None:
        connection = self.find_classic_connection_by_handle(command.connection_handle)
        if connection is None:
            self._send_hci_command_status(
                hci.HCI_ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR, command.op_code
            )
            return
        self._send_hci_command_status(hci.HCI_COMMAND_STATUS_PENDING, command.op_code)
        self._security[connection.peer_address] = Security(initiator=True)
        self.send_hci_packet(hci.HCI_Link_Key_Request_Event(bd_addr=connection.peer_address))

    def on_hci_link_key_request_reply_command(
        self, command: hci.HCI_Link_Key_Request_Reply_Command
    ) -> hci.HCI_StatusAndAddressReturnParameters:
        peer = command.bd_addr
        security = self._security.get(peer)
        if security is None or peer not in self.classic_connections:
            return self._status(hci.HCI_ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR, peer)
        security.link_key = command.link_key
        if security.peer_challenge is not None:
            # The peer's challenge waited for the key.
            challenge, security.peer_challenge = security.peer_challenge, None
            self._answer(peer, security, challenge)
        elif security.initiator:
            self._challenge(peer, security)
        return self._status(hci.HCI_ErrorCode.SUCCESS, peer)

    def on_hci_link_key_request_negative_reply_command(
        self, command: hci.HCI_Link_Key_Request_Negative_Reply_Command
    ) -> hci.HCI_StatusAndAddressReturnParameters:
        peer = command.bd_addr
        security = self._security.get(peer)
        if security is None or peer not in self.classic_connections:
            return self._status(hci.HCI_ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR, peer)
        if security.peer_challenge is not None:
            # Asked to prove a key this side's host does not have.
            security.peer_challenge = None
            self.send_lmp_packet(
                peer,
                lmp.LmpNotAccepted(
                    lmp.Opcode.LMP_AU_RAND, hci.HCI_ErrorCode.PIN_OR_KEY_MISSING_ERROR
                ),
            )
        elif self._both_simple_pairing(peer):
            # Bumble's Secure Simple Pairing, from its IO capability exchange on.
            del self._security[peer]
            super().on_hci_link_key_request_negative_reply_command(command)
        else:
            self.send_hci_packet(hci.HCI_PIN_Code_Request_Event(bd_addr=peer))
        return self._status(hci.HCI_ErrorCode.SUCCESS, peer)

    def on_hci_pin_code_request_reply_command(
        self, command: hci.HCI_PIN_Code_Request_Reply_Command
    ) -> hci.HCI_StatusAndAddressReturnParameters:
        peer = command.bd_addr
        security = self._security.get(peer)
        if security is None or peer not in self.classic_connections:
            return self._status(hci.HCI_ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR, peer)
        pin = bytes(command.pin_code[: command.pin_code_length])
        if security.initiator:
            in_rand = self.random_bytes(16)
            security.init_key = init_key(pin, in_rand, peer)
            self.send_lmp_packet(peer, LmpInRand(random_number=in_rand))
        else:
            assert security.peer_rand is not None
            security.init_key = init_key(pin, security.peer_rand, self.public_address)
            security.peer_rand = None
            self.send_lmp_packet(peer, lmp.LmpAccepted(lmp.Opcode.LMP_IN_RAND))
        return self._status(hci.HCI_ErrorCode.SUCCESS, peer)

    def on_hci_pin_code_request_negative_reply_command(
        self, command: hci.HCI_PIN_Code_Request_Negative_Reply_Command
    ) -> hci.HCI_StatusAndAddressReturnParameters:
        peer = command.bd_addr
        security = self._security.pop(peer, None)
        if security is None or peer not in self.classic_connections:
            return self._status(hci.HCI_ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR, peer)
        if security.initiator:
            self._authentication_complete(peer, hci.HCI_ErrorCode.PIN_OR_KEY_MISSING_ERROR)
        else:
            self.send_lmp_packet(
                peer,
                lmp.LmpNotAccepted(
                    lmp.Opcode.LMP_IN_RAND, hci.HCI_ErrorCode.PIN_OR_KEY_MISSING_ERROR
                ),
            )
        return self._status(hci.HCI_ErrorCode.SUCCESS, peer)

    def on_hci_set_connection_encryption_command(
        self, command: hci.HCI_Set_Connection_Encryption_Command
    ) -> None:
        connection = self.find_classic_connection_by_handle(command.connection_handle)
        if connection is None:
            self._send_hci_command_status(
                hci.HCI_ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR, command.op_code
            )
            return
        if not self.authenticated(connection.peer_address):
            self._send_hci_command_status(
                hci.HCI_ErrorCode.INSUFFICIENT_SECURITY_ERROR, command.op_code
            )
            return
        self._send_hci_command_status(hci.HCI_COMMAND_STATUS_PENDING, command.op_code)
        mode = 1 if command.encryption_enable else 0
        self._encryption[connection.peer_address] = mode
        self.send_lmp_packet(connection.peer_address, LmpEncryptionModeReq(encryption_mode=mode))

    # Authentication: what the peer's controller sends.

    def on_lmp_packet(self, sender_address: hci.Address, packet: lmp.Packet) -> None:
        peer = sender_address
        if not self._hears(peer, packet):
            # Unheard, as on the air: the pager's timer ends it.
            return
        if isinstance(packet, OURS_PACKETS) and peer not in self.classic_connections:
            # Sent before the link was detached on this side, as a challenge that crosses the
            # detach of a failed check.
            return
        match packet:
            case LmpInRand(random_number):
                # The peer pairs: this side's host is asked for the PIN.
                self._security[peer] = Security(peer_rand=random_number)
                self.send_hci_packet(hci.HCI_PIN_Code_Request_Event(bd_addr=peer))
            case LmpCombKey(random_number):
                self._on_comb_key(peer, random_number)
            case lmp.LmpAuRand(random_number):
                self._on_challenge(peer, random_number)
            case LmpSres(authentication_response):
                self._on_response(peer, authentication_response)
            case LmpEncryptionModeReq(encryption_mode):
                self._encryption_change(peer, encryption_mode)
                self.send_lmp_packet(peer, lmp.LmpAccepted(lmp.Opcode.LMP_ENCRYPTION_MODE_REQ))
            case LmpSupervisionTimeout(supervision_timeout):
                self._supervision_timeout_changed(peer, supervision_timeout)
            case lmp.LmpAccepted(response_opcode) if response_opcode in OURS:
                self._on_accepted(peer, response_opcode)
                super().on_lmp_packet(sender_address, packet)
            case lmp.LmpNotAccepted(response_opcode, error_code) if response_opcode in OURS:
                security = self._security.pop(peer, None)
                if security is not None and security.initiator:
                    self._authentication_complete(peer, error_code)
                super().on_lmp_packet(sender_address, packet)
            case lmp.LmpNotAccepted(lmp.Opcode.LMP_HOST_CONNECTION_REQ) if peer not in self._paging:
                # The rejection of a page that ended: its host heard the end already.
                self.classic_pending_commands.get(peer, {}).pop(
                    lmp.Opcode.LMP_HOST_CONNECTION_REQ, None
                )
            case lmp.LmpDetach(error_code):
                security = self._security.pop(peer, None)
                if security is not None and security.initiator and not security.authenticated:
                    self._authentication_complete(peer, error_code)
                super().on_lmp_packet(sender_address, packet)
            case _:
                super().on_lmp_packet(sender_address, packet)

    def _supervision_timeout_changed(self, peer: hci.Address, timeout: int) -> None:
        self._set_supervision_timeout(peer, timeout)
        self.send_hci_packet(
            hci.HCI_Link_Supervision_Timeout_Changed_Event(
                connection_handle=self.classic_connections[peer].handle,
                link_supervision_timeout=timeout,
            )
        )

    def _on_accepted(self, peer: hci.Address, opcode: int) -> None:
        security = self._security.get(peer)
        if opcode == lmp.Opcode.LMP_ENCRYPTION_MODE_REQ:
            self._encryption_change(peer, self._encryption.pop(peer, 1))
        elif security is None:
            return
        elif opcode == lmp.Opcode.LMP_IN_RAND:
            # The peer has the initialisation key: each side gives its part of the combination.
            security.local_rand = self.random_bytes(16)
            self.send_lmp_packet(peer, LmpCombKey(random_number=security.local_rand))
        elif opcode == lmp.Opcode.LMP_SRES:
            # The peer checked this side's answer: legacy pairing is over.
            self._paired(peer, security)
            self._authentication_complete(peer, hci.HCI_ErrorCode.SUCCESS)

    def _on_comb_key(self, peer: hci.Address, peer_rand: bytes) -> None:
        security = self._security.get(peer)
        if security is None or security.init_key is None:
            return
        if security.initiator:
            assert security.local_rand is not None
            security.link_key = combination_key(
                security.init_key, security.local_rand, peer_rand, self.public_address, peer
            )
            security.pairing = True
            # The initiator checks the responder first.
            self._challenge(peer, security)
        else:
            security.local_rand = self.random_bytes(16)
            self.send_lmp_packet(peer, LmpCombKey(random_number=security.local_rand))
            security.link_key = combination_key(
                security.init_key, peer_rand, security.local_rand, peer, self.public_address
            )
            security.pairing = True

    def _challenge(self, peer: hci.Address, security: Security) -> None:
        security.challenge = self.random_bytes(16)
        self.send_lmp_packet(peer, lmp.LmpAuRand(random_number=security.challenge))

    def _on_challenge(self, peer: hci.Address, challenge: bytes) -> None:
        security = self._security.setdefault(peer, Security())
        if security.link_key is not None:
            self._answer(peer, security, challenge)
        else:
            # A key from an earlier pairing, which only this side's host knows.
            security.peer_challenge = challenge
            self.send_hci_packet(hci.HCI_Link_Key_Request_Event(bd_addr=peer))

    def _answer(self, peer: hci.Address, security: Security, challenge: bytes) -> None:
        assert security.link_key is not None
        sres = response(security.link_key, challenge, self.public_address)
        self.send_lmp_packet(peer, LmpSres(authentication_response=sres))
        if security.pairing and not security.initiator:
            # Legacy pairing checks both ways: the responder checks the initiator next.
            self._challenge(peer, security)
        elif not security.initiator:
            security.authenticated = True

    def _on_response(self, peer: hci.Address, sres: bytes) -> None:
        security = self._security.get(peer)
        if security is None or security.challenge is None or security.link_key is None:
            return
        challenge, security.challenge = security.challenge, None
        if sres != response(security.link_key, challenge, peer):
            self._authentication_failed(peer, security)
        elif security.initiator and not security.pairing:
            security.authenticated = True
            self._authentication_complete(peer, hci.HCI_ErrorCode.SUCCESS)
        elif not security.initiator:
            # The responder has checked the initiator: the pairing holds on both sides.
            self._paired(peer, security)
            self.send_lmp_packet(peer, lmp.LmpAccepted(lmp.Opcode.LMP_SRES))
        # An initiator in a pairing waits for the responder's challenge, then its acceptance.

    def _paired(self, peer: hci.Address, security: Security) -> None:
        assert security.link_key is not None
        security.authenticated = True
        security.pairing = False
        self.send_hci_packet(
            hci.HCI_Link_Key_Notification_Event(
                bd_addr=peer,
                link_key=security.link_key,
                key_type=hci.LinkKeyType.COMBINATION_KEY,
            )
        )

    def _authentication_failed(self, peer: hci.Address, security: Security) -> None:
        reason = hci.HCI_ErrorCode.AUTHENTICATION_FAILURE_ERROR
        del self._security[peer]
        if security.initiator:
            self._authentication_complete(peer, reason)
        self.send_lmp_packet(peer, lmp.LmpDetach(reason))
        self.on_classic_disconnected(peer, reason)

    def _authentication_complete(self, peer: hci.Address, status: int) -> None:
        connection = self.classic_connections.get(peer)
        if connection is None:
            return
        self.send_hci_packet(
            hci.HCI_Authentication_Complete_Event(
                status=status, connection_handle=connection.handle
            )
        )

    def _encryption_change(self, peer: hci.Address, mode: int) -> None:
        connection = self.classic_connections.get(peer)
        if connection is None:
            return
        self.send_hci_packet(
            hci.HCI_Encryption_Change_Event(
                status=hci.HCI_ErrorCode.SUCCESS,
                connection_handle=connection.handle,
                encryption_enabled=mode,
            )
        )

    def _forget_link(self, peer: hci.Address) -> None:
        self._security.pop(peer, None)
        self._encryption.pop(peer, None)
        self._supervision_timeouts.pop(peer, None)
        self._lost_contact.pop(peer, None)
        timer = self._supervision.pop(peer, None)
        if timer is not None:
            timer.cancel()

    def on_classic_disconnected(self, peer_address: hci.Address, reason: int) -> None:
        self._forget_link(peer_address)
        super().on_classic_disconnected(peer_address, reason)
        self._let_go(peer_address)

    def _let_go(self, peer: hci.Address) -> None:
        """When neither side holds anything for the other any more, they may page."""
        if peer not in self.held() and not self._held_by(peer):
            self.link.let_go(self.public_address, peer)

    def authenticated(self, peer: hci.Address) -> bool:
        """Whether the link to `peer` is authenticated, on this side: by legacy pairing, by a
        key, or by Bumble's Secure Simple Pairing. A responder's host is not told, as the
        specification sends `Authentication_Complete` to the initiator's only."""
        connection = self.classic_connections.get(peer)
        if connection is None:
            return False
        security = self._security.get(peer)
        legacy = security is not None and security.authenticated
        return legacy or connection.ssp_state.authenticated

    def _both_simple_pairing(self, peer: hci.Address) -> bool:
        """Whether both hosts support Secure Simple Pairing: what LMP's features exchange would
        tell, read from the peer's controller on the link."""
        other = self.link.find_classic_controller(peer)
        ssp = hci.LmpFeatureMask.SECURE_SIMPLE_PAIRING_HOST_SUPPORT
        return other is not None and bool(self.lmp_features & other.lmp_features & ssp)

    @staticmethod
    def _status(
        status: hci.HCI_ErrorCode, peer: hci.Address
    ) -> hci.HCI_StatusAndAddressReturnParameters:
        return hci.HCI_StatusAndAddressReturnParameters(status=status, bd_addr=peer)


# The packets of authentication, encryption and link supervision, which this module handles.
OURS_PACKETS = (
    LmpInRand,
    LmpCombKey,
    lmp.LmpAuRand,
    LmpSres,
    LmpEncryptionModeReq,
    LmpSupervisionTimeout,
)

# The requests of this module, whose acceptance or refusal it handles.
OURS = frozenset(
    {
        lmp.Opcode.LMP_IN_RAND,
        lmp.Opcode.LMP_AU_RAND,
        lmp.Opcode.LMP_SRES,
        lmp.Opcode.LMP_ENCRYPTION_MODE_REQ,
    }
)
