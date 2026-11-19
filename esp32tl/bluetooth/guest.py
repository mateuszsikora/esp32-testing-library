"""The guest's Bluetooth controller: the one the firmware's host, ESP-IDF's Bluedroid, talks to
through esp32sim's VHCI and the runner's `--bt`, on the air with the peers.

It is an esp32tl `Controller`, so it does all a controller on the air does, and also what is the
guest's alone, by the chip it is in. The classic ESP32's is BR/EDR, as the firmware enables it:
three pages of features, no LE, and an ACL buffer the VHCI carries both ways. The ESP32-S3's,
ESP32-C3's and ESP32-C6's are Bluetooth LE only: page 0 of the features says LE and no BR/EDR, and
the controller takes Espressif's vendor commands for the flow control of advertising reports,
which Bluedroid sends them. The interface that puts it on the board's link is
`esp32tl.interfaces.bluetooth.Bluetooth`.
"""

import asyncio

from bumble import hci

from esp32tl.bluetooth.air import Link
from esp32tl.bluetooth.controller import Controller, UnknownCommand
from esp32tl.link.protocol import HCI_MAX
from esp32tl.units import Duration

# The guest's address, which its host reads with `Read_BD_ADDR`. A real chip's controller has its
# base MAC plus 2, as the ESP32-S3 measured below had; on esp32sim the firmware's controller is this
# one, at this fixed address.
ADDRESS = "24:6F:28:00:11:24"

# The most ACL data one packet carries either way: esp32sim's VHCI carries H4 packets of at most
# `HCI_MAX` bytes, 5 of them the packet-type byte and the ACL header. TODO(owner): what the classic
# ESP32's controller reports in `Read_Buffer_Size`.
ACL_LENGTH = HCI_MAX - 5

# The last page of the features: the classic ESP32's controller is Bluetooth 4.2, with pages 0 to 2,
# and Bluedroid takes no more (`MAX_FEATURES_CLASSIC_PAGE_COUNT`; it asserts on a fourth in
# `get_features_classic`, `components/bt/host/bluedroid/device/controller.c:523`, v5.3.6).
MAX_PAGE = 2

# The features of pages 0 to `MAX_PAGE`, of which page 0 is the controller's and 1 and 2 its host's.
PAGES_MASK = (1 << 64 * (MAX_PAGE + 1)) - 1

# The chips whose controller is Bluetooth LE only, by ESP-IDF target name. The classic ESP32's is
# dual-mode, and the firmware runs it BR/EDR only.
LE_CHIPS = frozenset({"esp32s3", "esp32c3", "esp32c6"})

# What an LE-only chip's controller reports, as an ESP32-S3 (QFN56, revision v0.2) answered
# `tools/hci_probe` on 2026-11-19: ESP-IDF v5.3.6, controller library `51d9dfd`, built as
# Bluedroid's BLE 4.2 examples are (`CONFIG_BT_BLE_42_FEATURES_SUPPORTED=y`, so the controller
# runs with `BLE_50:0`). The ESP32-C3's controller is built from the same sources
# (`components/bt/controller/esp32c3`); TODO(owner): measured on an ESP32-C3 and an ESP32-C6.
#
# `Read_Local_Version_Information`: HCI and LMP version 5.0 (9), revision and subversion 0x0016,
# Espressif's company identifier.
LE_VERSION = 0x09
LE_REVISION = 0x0016
ESPRESSIF = 0x02E5
# Page 0 of the features (Vol 2, Part C, 3.3): LE supported, BR/EDR not, and no other page.
LE_ONLY_FEATURES = (
    hci.LmpFeatureMask.LE_SUPPORTED_CONTROLLER | hci.LmpFeatureMask.BR_EDR_NOT_SUPPORTED
)
# `LE_Read_Local_Supported_Features`: bits 0 to 7 alone, encryption to the extended scanner filter
# policies. Built with `BLE_50:1`, as a controller-only build is, it reports `0x080001F9FF`: the
# 2M PHY, the coded PHY, extended and periodic advertising and more, which this link leaves out.
LE_FEATURES = 0xFF
# `LE_Read_Supported_States`: every state and combination of the first 42 bits (7.8.27), where
# Bumble's says `ffff3fffff030000`.
LE_STATES = bytes.fromhex("ffffffffff030000")
# `LE_Read_Buffer_Size`: 251 bytes, 12 packets.
LE_ACL_LENGTH = 251
LE_ACL_PACKETS = 12
# `LE_Read_Filter_Accept_List_Size` 12, `LE_Read_Resolving_List_Size` 10.
FILTER_ACCEPT_LIST_SIZE = 12
RESOLVING_LIST_SIZE = 10
# `LE_Read_Maximum_Data_Length`: 251 octets and 17040 µs (0x4290) each way.
LE_MAX_TIME_US = 0x4290
# `LE_Read_Advertising_Physical_Channel_Tx_Power`: 9 dBm.
ADVERTISING_TX_POWER = 9
# `Read_Local_Supported_Commands`, the 64 bytes as they came.
LE_SUPPORTED_COMMANDS = bytes.fromhex(
    "2000800000c000000000e40000002822000000000000040000f7ffff7f00000030f0ffffffffff07" + "00" * 24
)
# The commands that bitmap names (Vol 4, Part E, 6.27), by Bumble's masks: any other the controller
# answers `Command_Complete` with `UNKNOWN_HCI_COMMAND`, as the S3 answered `Read_Buffer_Size`,
# `Read_Local_Extended_Features`, `LE_Read_Buffer_Size` v2 and `Inquiry`, though Bumble's
# controller has handlers for them. Bluedroid without Classic sends none of them.
LE_SUPPORTED = int.from_bytes(LE_SUPPORTED_COMMANDS, "little")
# The commands the controller takes at once, in each `Command_Complete` and `Command_Status`, where
# Bumble's says 1.
LE_COMMAND_PACKETS = 5

# Espressif's vendor commands for the flow control of advertising reports, which Bluedroid sends
# an LE controller when it is built with `BLE_ADV_REPORT_FLOW_CONTROL`
# (`components/bt/host/bluedroid/stack/include/stack/hcidefs.h`, v5.3.6): OGF 0x3F, the BLE group,
# subcodes 0x09 and 0x0A.
SET_ADV_FLOW_CONTROL = 0xFD09
ADV_REPORT_FLOW_CONTROL = 0xFD0A

# The vendor LE Meta subevent that tells the host how many advertising reports the controller
# dropped for want of credits (`HCI_BLE_ADV_DISCARD_REPORT_EVT`, `hcidefs.h`): its parameter is the
# count, four bytes little-endian. The S3 sent it when the host gave credits back, before the next
# report, with a count past the lost threshold, and it took a credit (`043e05f068000000`: 104).
DISCARD_REPORT = 0xF0


class GuestController(Controller):
    """The controller of the guest's host on `chip`, at `address` on `link`, named `name` in
    Bumble's logs, with an ACL buffer of `ACL_LENGTH` bytes, which it also cuts what it sends its
    host to.

    On the classic ESP32 it is BR/EDR only, as the firmware enables it (`ESP_BT_MODE_CLASSIC_BT`).
    On a chip of `LE_CHIPS` it is LE only and reports what an ESP32-S3's controller does, above; it
    answers every command it does not know with `Command_Complete`, as that controller does; and it
    takes Espressif's flow control of advertising reports: with it on, it sends its host no more
    reports than the credits the host gave it, drops the rest, counted in `dropped_reports`, and
    tells the host how many it dropped once the count passes the lost threshold and a credit comes
    back."""

    acl_to_host = ACL_LENGTH

    def __init__(
        self, name: str, link: Link, address: str = ADDRESS, *, chip: str = "esp32"
    ) -> None:
        super().__init__(name, link, address)
        self.chip = chip
        self.le_only = chip in LE_CHIPS
        if self.le_only:
            self.lmp_features_max_page_number = 0
            self.lmp_features = hci.LmpFeatureMask(LE_ONLY_FEATURES)
            self.hci_version = self.lmp_version = LE_VERSION
            self.hci_revision = self.lmp_subversion = LE_REVISION
            self.manufacturer_company_identifier = ESPRESSIF
            self.le_features = hci.LeFeatureMask(LE_FEATURES)
            self.le_states = LE_STATES
            self.le_acl_data_packet_length = LE_ACL_LENGTH
            self.total_num_le_acl_data_packets = LE_ACL_PACKETS
            self.filter_accept_list_size = FILTER_ACCEPT_LIST_SIZE
            self.resolving_list_size = RESOLVING_LIST_SIZE
            self.supported_max_tx_time = self.supported_max_rx_time = LE_MAX_TIME_US
            self.advertising_channel_tx_power = ADVERTISING_TX_POWER
        else:
            self.lmp_features_max_page_number = MAX_PAGE
            # No LE on page 0, where Bumble names it: the firmware runs its controller BR/EDR only.
            # TODO(owner): what the classic ESP32's controller reports on page 0 in that mode.
            self.lmp_features = hci.LmpFeatureMask(
                self.lmp_features & PAGES_MASK & ~hci.LmpFeatureMask.LE_SUPPORTED_CONTROLLER
            )
            # Bumble's default is LE's, 27 bytes, which carried about 21 kbit/s of SPP on the
            # emulator; 491 carried about 259 kbit/s.
            self.acl_data_packet_length = ACL_LENGTH
        self.dropped_reports = 0
        self._report_credits: int | None = None
        self._lost_threshold = 0
        self._discarded = 0

    def _defaults(self) -> None:
        super()._defaults()
        # The flow control of advertising reports is off until the host turns it on.
        self._report_credits = None
        self._discarded = 0

    def on_hci_command_packet(self, command: hci.HCI_Command) -> None:
        mask = hci.HCI_SUPPORTED_COMMANDS_MASKS.get(command.op_code)
        if self.le_only and mask is not None and not LE_SUPPORTED & mask:
            self._le_unknown(command)
            return
        super().on_hci_command_packet(command)

    def send_hci_packet(self, packet: hci.HCI_Packet) -> None:
        if self.le_only and isinstance(
            packet, (hci.HCI_Command_Complete_Event, hci.HCI_Command_Status_Event)
        ):
            packet.num_hci_command_packets = LE_COMMAND_PACKETS
        super().send_hci_packet(packet)

    def on_hci_read_local_supported_commands_command(
        self, command: hci.HCI_Read_Local_Supported_Commands_Command
    ) -> hci.HCI_Read_Local_Supported_Commands_ReturnParameters:
        if not self.le_only:
            return super().on_hci_read_local_supported_commands_command(command)
        return hci.HCI_Read_Local_Supported_Commands_ReturnParameters(
            hci.HCI_ErrorCode.SUCCESS, supported_commands=LE_SUPPORTED_COMMANDS
        )

    def on_hci_le_read_advertising_physical_channel_tx_power_command(
        self, command: hci.HCI_LE_Read_Advertising_Physical_Channel_Tx_Power_Command
    ) -> hci.HCI_LE_Read_Advertising_Physical_Channel_Tx_Power_ReturnParameters:
        """7.8.6: the chip's, where Bumble's answers 0 whatever `advertising_channel_tx_power`."""
        return hci.HCI_LE_Read_Advertising_Physical_Channel_Tx_Power_ReturnParameters(
            status=hci.HCI_ErrorCode.SUCCESS, tx_power_level=self.advertising_channel_tx_power
        )

    def _le_unknown(self, command: hci.HCI_Command) -> None:
        """A command the LE-only controller does not know: kept, and answered `Command_Complete`
        with `UNKNOWN_HCI_COMMAND`, also one that has a `Command_Status` (the S3 answered
        `Inquiry` so), where Vol 4, Part E, 4.5 leaves the event to the vendor."""
        loop = asyncio.get_running_loop()
        self.unknown_commands.append(
            UnknownCommand(Duration(round(loop.time() * 1e9)), command.op_code, command.name)
        )
        self._command_complete(command, hci.HCI_ErrorCode.UNKNOWN_HCI_COMMAND_ERROR)

    def on_hci_command(  # type: ignore[override]
        self, command: hci.HCI_Command
    ) -> hci.HCI_StatusReturnParameters | None:
        """Espressif's vendor commands for the flow control of advertising reports, on an LE-only
        chip: `SET_ADV_FLOW_CONTROL` (enable, credits, lost threshold) completes, and
        `ADV_REPORT_FLOW_CONTROL` (credits) gives credits back and is answered with nothing, as
        Bluedroid expects: it sends it `HCI_CMD_MSG_F_SRC_NOACK` and takes no command credit
        for it (`hci_layer.c`, v5.3.6). Any other goes to `Controller`'s."""
        parameters = bytes(getattr(command, "parameters", b"") or b"")
        if self.le_only and command.op_code == SET_ADV_FLOW_CONTROL and len(parameters) >= 5:
            enabled = parameters[0] != 0
            self._report_credits = int.from_bytes(parameters[1:3], "little") if enabled else None
            self._lost_threshold = int.from_bytes(parameters[3:5], "little")
            self._discarded = 0
            self._command_complete(command, hci.HCI_ErrorCode.SUCCESS)
            return None
        if self.le_only and command.op_code == ADV_REPORT_FLOW_CONTROL and len(parameters) >= 2:
            if self._report_credits is not None:
                self._report_credits += int.from_bytes(parameters[0:2], "little")
                self._tell_discarded()
            return None
        if self.le_only:
            self._le_unknown(command)
            return None
        return super().on_hci_command(command)

    def _tell_discarded(self) -> None:
        """With a credit back and more reports dropped since the last count than the lost
        threshold, the host is told how many, in Espressif's discard event, which takes a
        credit."""
        if self._report_credits and self._discarded and self._discarded >= self._lost_threshold:
            count, self._discarded = self._discarded, 0
            self._report_credits -= 1
            event = bytes([hci.HCI_EVENT_PACKET, hci.HCI_LE_META_EVENT, 5, DISCARD_REPORT])
            if self.host is not None:
                asyncio.get_running_loop().call_soon(
                    self.host.on_packet, event + count.to_bytes(4, "little")
                )

    def admits_report(self) -> bool:
        """Whether the host takes one more advertising report: always, without flow control; with
        it, while a credit is left, which the report takes."""
        if self._report_credits is None:
            return True
        if self._report_credits == 0:
            self.dropped_reports += 1
            self._discarded += 1
            return False
        self._report_credits -= 1
        return True

    def chip_reset(self) -> None:
        """The chip resets, and the controller in it with it, as at `HCI_Reset`: the firmware's
        host, which starts again too, is told nothing."""
        self._restart()
        self._defaults()

    def chip_off(self) -> None:
        """The board's supply goes off: the controller is off the air until `chip_on`, and keeps
        nothing."""
        self._restart(back=False)
        self._defaults()

    def chip_on(self) -> None:
        """The board's supply comes back: the controller is on the air, at its defaults."""
        self.returned()
