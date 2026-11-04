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

The keys are not the specification's: SAFER+ (E1, E21, E22) is replaced by SHA-256 over the same
inputs, so a key or a response holds on this link only, and only the host sees a key. The random
numbers come from a generator seeded with the controller's address, so two runs exchange the same.
"""

import asyncio
import hashlib
import random
from collections import Counter
from dataclasses import dataclass, field

from bumble import hci, lmp
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
    inquiry, legacy pairing, authentication with a stored key, encryption, a page timeout and link
    supervision."""

    link: Link

    def __init__(self, name: str, link: Link, address: str) -> None:
        super().__init__(name, link=link, public_address=address)
        self.lmp_features &= ~hci.LmpFeatureMask.BR_EDR_NOT_SUPPORTED
        self.class_of_device = 0
        self.extended_inquiry_response = bytes(240)
        self.inquiry_mode = 0
        self.rssi = RSSI
        self._random = random.Random(str(self.public_address))
        self._inquiry: asyncio.TimerHandle | None = None
        self._security: dict[hci.Address, Security] = {}
        # The encryption mode this side asked of each peer, until the peer accepts it.
        self._encryption: dict[hci.Address, int] = {}
        self.page_timeout = PAGE_TIMEOUT_SLOTS
        # The page this side started to each peer, until it ends.
        self._paging: dict[hci.Address, asyncio.TimerHandle] = {}
        # The remote name request this side made of each peer, until it is answered.
        self._naming: dict[hci.Address, asyncio.TimerHandle] = {}
        # How many answers this side's host owes each peer that paged it, and how many of those
        # are late, for an entry that went before they came. An answer names only the address.
        self._answers_due: Counter[hci.Address] = Counter()
        self._late_answers: Counter[hci.Address] = Counter()
        # Each link's timeout in slots, where a host or the central changed it; 0 for none.
        self._supervision_timeouts: dict[hci.Address, int] = {}
        # The time each link lost contact, on the loop's clock, and the timer it falls by.
        self._lost_contact: dict[hci.Address, float] = {}
        self._supervision: dict[hci.Address, asyncio.TimerHandle] = {}

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
                and c.classic_scan_enable & 0x01
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
        if peer in self.classic_connections:
            # Bumble would make a second connection over the first.
            self._send_hci_command_status(
                hci.HCI_ErrorCode.CONNECTION_ALREADY_EXISTS_ERROR, command.op_code
            )
            return
        super().on_hci_create_connection_command(command)
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
            # Bumble keeps the connection it made for the page.
            self.classic_connections.pop(peer_address, None)
        super().on_classic_connection_complete(peer_address, status)
        if status != hci.HCI_ErrorCode.SUCCESS:
            self._let_go(peer_address)

    def on_hci_accept_connection_request_command(
        self, command: hci.HCI_Accept_Connection_Request_Command
    ) -> None:
        peer = command.bd_addr
        entry = self.classic_connections.get(peer)
        was_late = self._late_answers[peer] > 0
        if self._answers_due[peer] > 0:
            self._answers_due[peer] -= 1
        if was_late:
            self._late_answers[peer] -= 1
        if not was_late and (entry is None or entry.handle == 0):
            super().on_hci_accept_connection_request_command(command)
            return
        # The page it answers ended, or was answered: no link comes of it, and what this side holds
        # for the peer now is not this answer's.
        self._send_hci_command_status(hci.HCI_ErrorCode.SUCCESS, command.op_code)
        if peer not in self._paging:
            # The event names only the address: to the host it would end this side's own page.
            self.send_hci_packet(
                hci.HCI_Connection_Complete_Event(
                    status=hci.HCI_ErrorCode.CONNECTION_TIMEOUT_ERROR,
                    connection_handle=0,
                    bd_addr=peer,
                    encryption_enabled=False,
                    link_type=hci.HCI_Connection_Complete_Event.LinkType.ACL,
                )
            )

    def on_classic_connection_request(self, peer_address: hci.Address, link_type: int) -> None:
        if link_type == hci.HCI_Connection_Complete_Event.LinkType.ACL:
            self._answers_due[peer_address] += 1
        super().on_classic_connection_request(peer_address, link_type)

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
        """No packet comes from `peer` from now on. A link still being paged has none to lose:
        its page ends by itself; a link that lost it already goes on counting from then."""
        if (
            peer in self.classic_connections
            and peer not in self._paging
            and peer not in self._lost_contact
        ):
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
        reason = hci.HCI_ErrorCode.CONNECTION_TIMEOUT_ERROR
        security = self._security.get(peer)
        if security is not None and security.initiator and not security.authenticated:
            self._authentication_complete(peer, reason)
        self.on_classic_disconnected(peer, reason)

    # Leaving: called by the device that leaves, on its own controller.

    def left(self) -> None:
        """This device is gone from the air, without a word: both ends of each of its links lose
        contact. A page this side has under way that nobody heard is no link: it ends by itself;
        one the target heard, the target holds, and only that end has a timeout to wait for."""
        peers = [
            peer
            for peer in self.classic_connections
            if peer not in self._paging or self._held_by(peer)
        ]
        self.link.leave(self.public_address, peers)
        for peer in peers:
            self._lose_contact(peer)
            other = self.link.find_classic_controller(peer)
            if isinstance(other, Controller):
                other._lose_contact(self.public_address)

    def _held_by(self, peer: hci.Address) -> bool:
        other = self.link.find_classic_controller(peer)
        return other is not None and self.public_address in other.classic_connections

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
        for peer in list(self.classic_connections):
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

    def on_classic_disconnected(self, peer_address: hci.Address, reason: int) -> None:
        self._security.pop(peer_address, None)
        self._encryption.pop(peer_address, None)
        self._supervision_timeouts.pop(peer_address, None)
        self._lost_contact.pop(peer_address, None)
        timer = self._supervision.pop(peer_address, None)
        if timer is not None:
            timer.cancel()
        connection = self.classic_connections.get(peer_address)
        if connection is not None and connection.handle == 0:
            # A page this side heard, whose host has yet to answer: handles start at 1, and no link
            # was made to tell it of. A page of this side's own also has handle 0, but ends through
            # `on_classic_connection_complete` and never comes here.
            del self.classic_connections[peer_address]
            self._late_answers[peer_address] = self._answers_due[peer_address]
        else:
            super().on_classic_disconnected(peer_address, reason)
        self._let_go(peer_address)

    def _let_go(self, peer: hci.Address) -> None:
        """The link holds no more on this side: when `peer` holds none either, they may page."""
        if not self._held_by(peer):
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
