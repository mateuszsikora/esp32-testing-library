"""The guest's Bluetooth controller: the one the firmware's host, ESP-IDF's Bluedroid, talks to
through esp32sim's VHCI and the runner's `--bt`, on the air with the peers.

It is an esp32tl `Controller`, so it does all a controller on the air does, and also what is the
guest's alone: the classic ESP32's three pages of features, BR/EDR only, and an ACL buffer the
VHCI carries both ways. The interface that puts it on the board's link is
`esp32tl.interfaces.bluetooth.Bluetooth`.
"""

from bumble import hci

from esp32tl.bluetooth.air import Link
from esp32tl.bluetooth.controller import Controller
from esp32tl.link.protocol import HCI_MAX

# The guest's address, which its host reads with `Read_BD_ADDR`. TODO(owner): the address the
# classic ESP32's eFuse gives its Bluetooth controller on esp32sim (the base MAC plus 2).
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


class GuestController(Controller):
    """The controller of the guest's host, at `address` on `link`, named `name` in Bumble's logs:
    BR/EDR only, as the firmware enables it (`ESP_BT_MODE_CLASSIC_BT`), with an ACL buffer of
    `ACL_LENGTH` bytes, which it also cuts what it sends its host to."""

    acl_to_host = ACL_LENGTH

    def __init__(self, name: str, link: Link, address: str = ADDRESS) -> None:
        super().__init__(name, link, address)
        self.lmp_features_max_page_number = MAX_PAGE
        # No LE on page 0, where Bumble names it: the firmware runs its controller BR/EDR only.
        # TODO(owner): what the classic ESP32's controller reports on page 0 in that mode.
        self.lmp_features = hci.LmpFeatureMask(
            self.lmp_features & PAGES_MASK & ~hci.LmpFeatureMask.LE_SUPPORTED_CONTROLLER
        )
        # Bumble's default is LE's, 27 bytes, which carried about 21 kbit/s of SPP on the emulator;
        # 491 carried about 259 kbit/s.
        self.acl_data_packet_length = ACL_LENGTH

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
