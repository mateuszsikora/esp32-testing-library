"""Bluetooth Classic devices on the air: any device, and one with a Serial Port Profile service.

`BluetoothDevice` is a Bumble device on the air: it can be discovered by its name, with its class
of device and its signal strength, pairs with its PIN if it has one (legacy pairing, as Bluetooth
2.0 devices do), and, given an IO capability, with Secure Simple Pairing where the other side has
it too: its user confirms a number or not, types a passkey, or reads one off its display. It keeps
the link keys it pairs to and the history of its pairings, offers no service, and can be switched
off and go out of range, and come back, with the link keys it had.

`SppPeer` adds SPP on one RFCOMM channel, to one device at a time and, unless told otherwise, only
on a link the device has authenticated by the time it opens the channel. What the device writes on
the channel goes to the source, a `Source` or a `Model` as at the far end of a UART, and what the
source sends goes out on the channel at the board's time it gives. `written_lines` and
`next_written_line` read the device's writes as lines, cut at a separator across its chunks.
"""

import bisect
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from typing import NamedTuple, TypeVar

from bumble import core, hci, rfcomm, sdp, utils
from bumble.device import Connection, Device, DeviceConfiguration, with_connection_from_address
from bumble.hci import Address
from bumble.host import Host
from bumble.keys import MemoryKeyStore
from bumble.l2cap import L2CAP_Information_Request
from bumble.pairing import PairingConfig, PairingDelegate
from bumble.transport.common import AsyncPipeSink

from esp32tl.backends.clock import seconds
from esp32tl.bluetooth.air import Air
from esp32tl.bluetooth.controller import RSSI, SIX_DIGITS, Controller, Pairing, UnderWay
from esp32tl.interfaces.condition import Condition, FirstFrom
from esp32tl.interfaces.uart import Source, Supplied
from esp32tl.link.boardlink import Direction, Output
from esp32tl.sources.model import InNanoseconds, Model, in_nanoseconds
from esp32tl.units import Duration


class ChannelEvent(NamedTuple):
    """Bytes crossing SPP at a Duration on the air's clock."""

    at: Duration
    direction: Direction
    data: bytes


class WrittenLine(NamedTuple):
    """A line the device wrote on the SPP channel: the Duration on the air's clock of its last
    byte, the separator's when it has one; its bytes without the separator; the channel's opening
    it was written in, 0 for the first; and whether it ended in the separator, or was cut off by
    the channel's close."""

    at: Duration
    data: bytes
    opening: int
    complete: bool


class Refusal(NamedTuple):
    """A channel opening refused at a Duration on the air's clock."""

    at: Duration
    why: str


# Unless the test gives another: an address no real device in a test is likely to have, and the
# class of device of a device that says nothing about itself (major class Uncategorized).
ADDRESS = "00:00:00:00:5B:01"
CLASS_OF_DEVICE = 0x001F00
CHANNEL = 1

# What ends a line the device writes, unless the test gives another: the carriage return that ends
# an AT command (ITU-T V.250, S3's default).
SEPARATOR = b"\r"

# The longest name the extended inquiry response holds whole: its 240 bytes, less the field's
# length and type.
NAME_MAX = 238

# The SDP record's handle: the first one a server may give a record of its own.
RECORD_HANDLE = 0x00010001

# The name of the SPP service in its SDP record, unless the test gives another: what SPP adapters
# commonly call theirs.
SERVICE_NAME = "Serial Port"

# The primary language of the SDP record, English in UTF-8 (IANA MIBenum 106), and its attributes'
# base, from which the service's name is attribute 0x0100 (Vol 3, Part B, 5.1.7 and 5.1.8).
LANGUAGE = (0x656E, 106, 0x0100)

# The L2CAP features a device names (Vol 3, Part A, 4.12): Bumble's default without Enhanced
# Retransmission Mode, which its RFCOMM server does not take: it disconnects when a device such as
# Bluedroid asks for it. Without it in the features, the device stays in basic mode.
L2CAP_FEATURES = (
    L2CAP_Information_Request.ExtendedFeatures.FIXED_CHANNELS,
    L2CAP_Information_Request.ExtendedFeatures.FCS_OPTION,
)

# The IO capabilities a device's user has for Secure Simple Pairing (Vol 3, Part C, 5.2.2.4), by
# the names a test gives them.
IO_CAPABILITIES = {
    "display_only": PairingDelegate.DISPLAY_OUTPUT_ONLY,
    "display_yes_no": PairingDelegate.DISPLAY_OUTPUT_AND_YES_NO_INPUT,
    "keyboard_only": PairingDelegate.KEYBOARD_INPUT_ONLY,
    "no_input_no_output": PairingDelegate.NO_OUTPUT_NO_INPUT,
}

T = TypeVar("T")


class PeerError(ValueError):
    """What the test tells a device that it cannot be, or what its source does that the peer
    cannot take: a call that raises, a result that is not bytes at a time, bytes stamped before
    the instant they are given at."""


class _Misreturned(PeerError):
    """What a `Model` source returned that is not of its kind, as the peer's adapter refuses it:
    the peer's own refusal, which it does not report as something the source raised."""


class _User(PairingDelegate):
    """The device's user. Answers a PIN request with the device's PIN, as a device with a fixed PIN
    does; without one, the pairing fails. With Secure Simple Pairing, of `io_capability`: says
    yes to a number to compare, or to a pairing it is asked to allow, when it `confirms`, unless
    it has a display only, with no yes or no to say, which allows whatever it is asked (Vol 3,
    Part C, 5.2.2.6, Table 5.7: automatic confirmation); types `passkey` when asked for one, and
    refuses without one; and reads what its display shows."""

    def __init__(
        self,
        pin: str | None,
        io_capability: PairingDelegate.IoCapability,
        confirms: bool,
        passkey: int | None,
    ) -> None:
        super().__init__(io_capability=io_capability)
        self.pin = pin
        self.confirms = confirms
        self.passkey = passkey

    async def get_string(self, max_length: int) -> str | None:
        return self.pin

    async def confirm(self, auto: bool = False) -> bool:
        # Asked to allow a pairing, or told one that needs no word from the user. Bumble asks a
        # display with no yes or no too, against a device with no input and no output.
        if auto or self.io_capability == PairingDelegate.DISPLAY_OUTPUT_ONLY:
            return True
        return self.confirms

    async def compare_numbers(self, number: int, digits: int) -> bool:
        return self.confirms

    async def get_number(self) -> int | None:
        return self.passkey

    async def display_number(self, number: int, digits: int) -> None:
        return None


class _Device(Device):
    """Bumble's device, which also answers a PIN request with Secure Simple Pairing on: Bumble's
    answers it only for a keyboard, and a device with a display then fails a legacy pairing with
    a device that has no Secure Simple Pairing, which pairs with the PIN."""

    @with_connection_from_address  # type: ignore[untyped-decorator]
    def on_pin_code_request(self, connection: Connection) -> None:
        delegate = self.pairing_config_factory(connection).delegate

        async def answer() -> None:
            pin = await connection.cancel_on_disconnection(delegate.get_string(16))
            if pin is None:
                command: hci.HCI_Command = hci.HCI_PIN_Code_Request_Negative_Reply_Command(
                    bd_addr=connection.peer_address
                )
            else:
                code = pin.encode()
                command = hci.HCI_PIN_Code_Request_Reply_Command(
                    bd_addr=connection.peer_address, pin_code_length=len(code), pin_code=code
                )
            await self.host.send_sync_command(command)  # type: ignore[arg-type]

        # As Bumble's own handler does: a task it keeps until it is done.
        utils.AsyncRunner.spawn(answer())  # type: ignore[no-untyped-call]


class BluetoothDevice:
    """A Classic device on `air` at `address`, discoverable and connectable from the start, called
    `name`, of class `class_of_device`, heard at `rssi` dBm, paired with `pin` if it has one, and
    offering no service: a device that is there to be found.

    With `io_capability`, one of `IO_CAPABILITIES`, it has Secure Simple Pairing, which a device
    that has it too pairs with, by the association model of the two capabilities: its user says
    yes to a number or to the pairing when it `confirms`, the default, and no otherwise, but for a
    `display_only` user, who has no yes or no and allows every pairing; and it types `passkey`,
    six digits, when asked for one (`keyboard_only`). A device that has none pairs with the PIN.
    Without it the device pairs with the PIN only. `pairings` is the history of the device's
    pairings that ended, each a `Pairing`. `rssi` can be changed at any time, and the next inquiry
    gets it; the plain `Inquiry_Result` has no such field.

    `switch_off` and `out_of_range` take it off the air without a word, until `switch_on` and
    `in_range` have both undone what was done; `present` says whether it is on the air. To another
    device the two look the same. The link keys stay until `forget_keys`.
    """

    def __init__(
        self,
        air: Air,
        *,
        name: str,
        pin: str | None = None,
        class_of_device: int = CLASS_OF_DEVICE,
        rssi: int = RSSI,
        address: str = ADDRESS,
        io_capability: str | None = None,
        confirms: bool = True,
        passkey: int | None = None,
    ) -> None:
        kind = type(self).__name__
        if not isinstance(name, str) or not 1 <= len(name.encode()) <= NAME_MAX:
            raise PeerError(f"{kind}: name is {name!r}, not a string of 1 to {NAME_MAX} bytes")
        if pin is not None and (not isinstance(pin, str) or not 1 <= len(pin.encode()) <= 16):
            raise PeerError(f"{kind}: pin is {pin!r}, not a string of 1 to 16 bytes")
        if (
            isinstance(class_of_device, bool)
            or not isinstance(class_of_device, int)
            or not 0 <= class_of_device <= 0xFFFFFF
        ):
            raise PeerError(
                f"{kind}: class_of_device is {class_of_device!r}, not three octets, 0 to 0xFFFFFF"
            )
        self._check_rssi(rssi)
        if io_capability is not None and io_capability not in IO_CAPABILITIES:
            raise PeerError(
                f"{kind}: io_capability is {io_capability!r}, not None or one of"
                f" {', '.join(IO_CAPABILITIES)}"
            )
        if not isinstance(confirms, bool):
            raise PeerError(f"{kind}: confirms is {confirms!r}, not True or False")
        if passkey is not None:
            if isinstance(passkey, bool) or not isinstance(passkey, int):
                raise PeerError(f"{kind}: passkey is {passkey!r}, not six digits, 0 to 999999")
            if not 0 <= passkey < SIX_DIGITS:
                raise PeerError(f"{kind}: passkey is {passkey!r}, not six digits, 0 to 999999")
            if io_capability != "keyboard_only":
                raise PeerError(
                    f"{kind}: passkey is typed on a keyboard: give io_capability='keyboard_only'"
                )
        public = _address(kind, address)
        if air._link.find_classic_controller(public) is not None:
            raise PeerError(
                f"{kind}: address {address} is another device's on the air: give each its own"
            )
        self.air = air
        self.name = name
        self.pin = pin
        self.class_of_device = class_of_device
        self.io_capability = io_capability
        self.confirms = confirms
        self.passkey = passkey
        self._on = True
        self._in_range = True
        self._controller: Controller | None = None
        self.device = air.call(lambda: self._make(public))
        self.rssi = rssi
        air.at_once(self._power_on(), description=f"{name}'s power-on")

    @property
    def address(self) -> str:
        return str(self.device.public_address).removesuffix("/P")

    @property
    def rssi(self) -> int:
        """The signal strength, in dBm, this device answers an inquiry with."""
        assert self._controller is not None
        return self._controller.rssi

    @rssi.setter
    def rssi(self, rssi: int) -> None:
        self._check_rssi(rssi)
        assert self._controller is not None
        self._controller.rssi = rssi

    def _check_rssi(self, rssi: int) -> None:
        if isinstance(rssi, bool) or not isinstance(rssi, int) or not -127 <= rssi <= 20:
            raise PeerError(
                f"{type(self).__name__}: rssi is {rssi!r}, not a signal strength in dBm, -127 to 20"
            )

    @property
    def pairings(self) -> list[Pairing]:
        """The device's pairings that ended, in order: with whom, how, the number it showed or
        its user typed, and whether it succeeded."""
        assert self._controller is not None
        return list(self._controller.pairings)

    @property
    def pairing(self) -> UnderWay | None:
        """The pairing under way, the latest to start, as far as it has gone: its `number` is
        what the device's display shows, or what its user typed; `None` while none is."""
        assert self._controller is not None
        under_way = list(self._controller.under_way.values())
        return under_way[-1] if under_way else None

    @property
    def present(self) -> bool:
        """Whether the device is on the air: switched on and in range."""
        return self._on and self._in_range

    def switch_off(self) -> None:
        """The device loses its power, at the air's time: gone from the air."""
        self.air.call(lambda: self._become(False, self._in_range))

    def switch_on(self) -> None:
        """The device has its power back, at the air's time; on the air if it is in range."""
        self.air.call(lambda: self._become(True, self._in_range))

    def out_of_range(self) -> None:
        """The device is out of reach, at the air's time: gone from the air, still running."""
        self.air.call(lambda: self._become(self._on, False))

    def in_range(self) -> None:
        """The device is within reach again, at the air's time; on the air if it is switched on."""
        self.air.call(lambda: self._become(self._on, True))

    def forget_keys(self) -> None:
        """The device loses the link keys it keeps: from its next link on, another device that
        kept one fails to authenticate, and one pairs with the PIN again."""
        self.device.keystore = MemoryKeyStore()

    def _become(self, on: bool, in_range: bool) -> None:
        assert self._controller is not None
        was_present = self.present
        was_on = self._on
        self._on, self._in_range = on, in_range
        if was_present and not self.present:
            self._controller.left()
        if was_on and not on:
            # The inquiry under way does not start again when it ends here.
            self.device.auto_restart_inquiry = False
            self._controller.power_lost()
        if was_on != on:
            self._supply(on)
        if self.present and not was_present:
            self._controller.returned()

    def _supply(self, on: bool) -> None:
        """The device's power went off, or came back: nothing else here depends on it."""

    def _make(self, address: Address) -> Device:
        controller = Controller(self.name, self.air._link, str(address))
        self._controller = controller
        config = DeviceConfiguration(
            name=self.name,
            address=address,
            class_of_device=self.class_of_device,
            classic_enabled=True,
            le_enabled=False,
            classic_ssp_enabled=self.io_capability is not None,
            classic_sc_enabled=False,
            l2cap_extended_features=L2CAP_FEATURES,
        )
        device = _Device(config=config, host=Host(controller, AsyncPipeSink(controller)))
        capability = IO_CAPABILITIES.get(
            self.io_capability or "", PairingDelegate.NO_OUTPUT_NO_INPUT
        )
        delegate = _User(self.pin, capability, self.confirms, self.passkey)
        # Protection from a man in the middle asked for whenever the user can see or type a number.
        mitm = capability != PairingDelegate.NO_OUTPUT_NO_INPUT
        device.pairing_config_factory = lambda connection: PairingConfig(
            sc=False, mitm=mitm, bonding=True, delegate=delegate
        )
        self._serve(device)
        return device

    def _serve(self, device: Device) -> None:
        """The services `device` offers: none."""

    async def _power_on(self) -> None:
        await self.device.power_on()
        await self.device.set_discoverable(True)
        await self.device.set_connectable(True)


class _Lines:
    """The device's writes in `events` cut into `WrittenLine`s at `separator`, each opening of the
    channel apart: brought up to date by `update`, which reads only the events it has not read.

    A line ends at the separator, or at its opening's close with what it has, if anything; the
    line of the opening still open is not one until it ends. `lines` only grows."""

    def __init__(self, separator: bytes) -> None:
        self.separator = separator
        self.lines: list[WrittenLine] = []
        self._read = 0
        self._opening = 0
        self._partial = bytearray()
        self._partial_at = Duration(0)

    def update(
        self, events: list[ChannelEvent], openings: list[int], closed: int
    ) -> list[WrittenLine]:
        """`lines`, with what `events` added since the last update. `openings` holds the index in
        `events` at which each opening of the channel began, and `closed` how many have closed."""
        for index in range(self._read, len(events)):
            event = events[index]
            if event.direction != "out":
                continue
            opening = bisect.bisect_right(openings, index) - 1
            if opening != self._opening:
                self._cut()
                self._opening = opening
            # The partial line holds no separator: one may start only in its last bytes.
            search = max(0, len(self._partial) - len(self.separator) + 1)
            self._partial += event.data
            self._partial_at = event.at
            begin = 0
            while (end := self._partial.find(self.separator, search)) != -1:
                line = bytes(self._partial[begin:end])
                self.lines.append(WrittenLine(event.at, line, opening, True))
                begin = search = end + len(self.separator)
            del self._partial[:begin]
        self._read = len(events)
        if self._opening < closed:
            self._cut()
        return self.lines

    def _cut(self) -> None:
        """The opening's line not ended when the channel closed, if it has a byte."""
        if self._partial:
            self.lines.append(
                WrittenLine(self._partial_at, bytes(self._partial), self._opening, False)
            )
            self._partial.clear()


class SppPeer(BluetoothDevice):
    """A `BluetoothDevice` paired with `pin`, or with Secure Simple Pairing given `io_capability`,
    with SPP on RFCOMM `channel`, whose bytes `source` answers, and whose SDP record names the
    service `service_name`, none for `None`.

    The channel opens to one device at a time; with `secure`, the default, only on a link that
    device has authenticated by the time it opens the channel (RFCOMM's SABM), which may be after
    it asked for it (PN), as Bluedroid does. A channel the peer refuses is in `refused`, with its
    time and why. What the device writes reaches `source.receive` at the board's time it arrives,
    and what `source` returns, or sends from `poll`, goes out on the channel at its own time,
    never earlier. What the source sends while no channel is open is lost, as it would be on the
    air; `lost` counts its bytes. `events` holds what crossed the channel, in order: `out` of the
    device, `in` to it, as a tap's recording names them. `written_lines` cuts what the device
    wrote into lines at a separator, a carriage return unless the test gives another.

    The source gets `supply` on a power loss, when it is `Supplied`; out of range it runs on, and
    what it sends is lost. A `Model` states its times in `Duration`s; any other source in
    nanoseconds.
    """

    def __init__(
        self,
        air: Air,
        *,
        name: str,
        pin: str,
        source: Source | Model,
        address: str = ADDRESS,
        channel: int = CHANNEL,
        secure: bool = True,
        class_of_device: int = CLASS_OF_DEVICE,
        rssi: int = RSSI,
        service_name: str | None = SERVICE_NAME,
        io_capability: str | None = None,
        confirms: bool = True,
        passkey: int | None = None,
    ) -> None:
        if pin is None:
            raise PeerError(f"SppPeer: pin is {pin!r}, not a string of 1 to 16 bytes")
        if isinstance(channel, bool) or not isinstance(channel, int) or not 1 <= channel <= 30:
            raise PeerError(f"SppPeer: channel is {channel!r}, not an RFCOMM channel, 1 to 30")
        if service_name is not None and (
            not isinstance(service_name, str) or not 1 <= len(service_name.encode()) <= 255
        ):
            raise PeerError(
                f"SppPeer: service_name is {service_name!r}, not None or a string of 1 to 255 bytes"
            )
        self.source = source
        self._adapter: InNanoseconds | None = None
        self.channel = channel
        self.secure = secure
        self.service_name = service_name
        self.events: list[ChannelEvent] = []
        self.refused: list[Refusal] = []
        self.lost = 0
        self._pending: list[Output] = []
        self._dlc: rfcomm.DLC | None = None
        # The index in `events` at which each opening of the channel began, how many of them have
        # closed, and the lines cut from the device's writes at each separator a test asked for.
        self._openings: list[int] = []
        self._closed = 0
        self._lines: dict[bytes, _Lines] = {}
        # The link the channel is given to, from the moment the peer accepts it until it closes:
        # two devices that ask at one instant are both accepted before either channel opens.
        self._holder: Connection | None = None
        super().__init__(
            air,
            name=name,
            pin=pin,
            class_of_device=class_of_device,
            rssi=rssi,
            address=address,
            io_capability=io_capability,
            confirms=confirms,
            passkey=passkey,
        )
        air._join(self)

    @property
    def channel_open(self) -> bool:
        """Whether a device holds the SPP channel now."""
        return self._dlc is not None

    @property
    def is_open(self) -> Condition[bool]:
        """A described condition for a device holding the SPP channel now."""
        return Condition(
            lambda: self.channel_open,
            f"{self.name}'s SPP channel is open ({self.address})",
            "SPP",
        )

    @property
    def is_closed(self) -> Condition[bool]:
        """A described condition for no device holding the SPP channel now."""
        return Condition(
            lambda: not self.channel_open,
            f"{self.name}'s SPP channel is closed ({self.address})",
            "SPP",
        )

    def next_write(self, data: bytes | None = None) -> Condition[ChannelEvent | None]:
        """The first device write appended after this call, optionally containing `data`.

        Match within one chunk, without consuming history or joining chunks. The boundary
        is append order, even at equal timestamps and across channel reopenings.
        """
        if data is not None and not isinstance(data, bytes):
            raise TypeError(f"SppPeer.next_write: data is {data!r}, not bytes or None")
        find = FirstFrom(
            lambda: self.events,
            len(self.events),
            lambda event: event.direction == "out" and (data is None or data in event.data),
        )
        description = f"the next write to {self.name}'s SPP channel ({self.address})"
        if data is not None:
            description += f" containing {data!r}"
        return Condition(find, description, "SPP")

    def written_lines(self, separator: bytes = SEPARATOR) -> list[WrittenLine]:
        """What the device wrote, its chunks of each opening of the channel joined and cut at
        `separator`, as `WrittenLine`s in order: each at the time of its separator, without it,
        on the opening it was written in. What an opening's close cut off is a line too, at the
        time of its last byte and not `complete`, never joined with the next opening's first;
        what the open channel has after its last separator is not a line yet."""
        return list(self._written(separator, "written_lines"))

    def next_written_line(
        self, data: bytes | None = None, separator: bytes = SEPARATOR
    ) -> Condition[WrittenLine | None]:
        """The first complete line, cut at `separator` as `written_lines` cuts them, that ends
        after this call, optionally containing `data`.

        The boundary is the lines' order, as `next_write`'s is the writes': a line that ends
        after the call is found even when its first bytes came before it, at the same timestamp,
        or after the channel was closed and opened again. A line cut off by a close never
        matches. Nothing is consumed: asked again, the condition finds the same line."""
        if data is not None and not isinstance(data, bytes):
            raise TypeError(f"SppPeer.next_written_line: data is {data!r}, not bytes or None")
        find = FirstFrom(
            lambda: self._written(separator, "next_written_line"),
            len(self._written(separator, "next_written_line")),
            lambda line: line.complete and (data is None or data in line.data),
        )
        description = (
            f"the next line ending in {separator!r} written to {self.name}'s SPP channel"
            f" ({self.address})"
        )
        if data is not None:
            description += f" containing {data!r}"
        return Condition(find, description, "SPP")

    def _written(self, separator: bytes, method: str) -> list[WrittenLine]:
        """The lines cut at `separator`, up to date; `method` names the call that asked."""
        if not isinstance(separator, bytes):
            raise TypeError(f"SppPeer.{method}: separator is {separator!r}, not bytes")
        if not separator:
            raise ValueError(f"SppPeer.{method}: separator is empty: give the bytes a line ends in")
        lines = self._lines.get(separator)
        if lines is None:
            lines = self._lines[separator] = _Lines(separator)
        return lines.update(self.events, self._openings, self._closed)

    @property
    def _driving(self) -> Source:
        """What the peer calls: `source`, or a `Model` in nanoseconds, whose refusals are
        `_Misreturned`s naming the peer."""
        source = self.source
        if not isinstance(source, Model):
            return source
        if self._adapter is None or self._adapter.model is not source:
            self._adapter = in_nanoseconds(source, f"{self.name}: source", _Misreturned)
        return self._adapter

    def _supply(self, on: bool) -> None:
        """A source that is `Supplied` loses its power with the peer, and what it had queued
        with it; any other runs on, and what it sends is lost."""
        if isinstance(self._driving, Supplied):
            self._pending.clear()
            source = self._driving
            with self._failing():
                self._call("supply", lambda: source.supply(on))

    def _serve(self, device: Device) -> None:
        record = rfcomm.make_service_sdp_records(
            RECORD_HANDLE, self.channel, core.BT_SERIAL_PORT_SERVICE
        )
        if self.service_name is not None:
            language, encoding, base = LANGUAGE
            record += [
                sdp.ServiceAttribute(
                    sdp.SDP_LANGUAGE_BASE_ATTRIBUTE_ID_LIST_ATTRIBUTE_ID,
                    sdp.DataElement.sequence(
                        [
                            sdp.DataElement.unsigned_integer_16(language),
                            sdp.DataElement.unsigned_integer_16(encoding),
                            sdp.DataElement.unsigned_integer_16(base),
                        ]
                    ),
                ),
                sdp.ServiceAttribute(
                    base + sdp.SDP_SERVICE_NAME_ATTRIBUTE_ID_OFFSET,
                    sdp.DataElement.text_string(self.service_name.encode()),
                ),
            ]
        device.sdp_service_records = {RECORD_HANDLE: record}
        server = rfcomm.Server(device)
        server.listen(self._opened, self.channel)

        def started(multiplexer: rfcomm.Multiplexer) -> None:
            # Bumble's server accepts every channel it listens on; this one asks the peer first,
            # at PN for the holder, and at SABM for the authentication.
            l2cap = multiplexer.l2cap_channel
            connection = l2cap.connection
            multiplexer.acceptor = lambda channel: self._accept(server, connection, channel)
            deliver = l2cap.sink
            assert deliver is not None

            def screened(pdu: bytes) -> None:
                frame = rfcomm.RFCOMM_Frame.from_bytes(pdu)
                if frame.type == rfcomm.FrameType.SABM and frame.dlci != 0:
                    # The DLC that PN made, which waits for this SABM to open.
                    dlc = multiplexer.dlcs.get(frame.dlci)
                    if (
                        dlc is not None
                        and dlc.state == rfcomm.DLC.State.CONNECTING
                        and not self._opens(multiplexer, connection, dlc)
                    ):
                        return
                deliver(pdu)

            l2cap.sink = screened

            def ended() -> None:
                # A channel accepted and never opened is let go with its RFCOMM session.
                if self._holder is connection and self._dlc is None:
                    self._holder = None

            l2cap.on(l2cap.EVENT_CLOSE, ended)

        server.on(server.EVENT_START, started)

    # The channel.

    def _accept(
        self, server: rfcomm.Server, connection: Connection, channel: int
    ) -> tuple[int, int] | None:
        """The channel's frame size and credits for `connection`, which holds it from now until
        it opens or goes, or `None` to refuse it: at the device's PN, which comes first."""
        if self._holder is not None:
            self._refuse(connection, f"the channel is held by {_named(self._holder)}")
            return None
        self._holder = connection

        def disconnected(reason: int) -> None:
            # A channel accepted and never opened is let go with its link.
            if self._holder is connection:
                self._holder = None

        connection.on(connection.EVENT_DISCONNECTION, disconnected)
        return server.dlc_configs.get(channel)

    def _opens(
        self, multiplexer: rfcomm.Multiplexer, connection: Connection, dlc: rfcomm.DLC
    ) -> bool:
        """Whether the channel opens at the device's SABM: with `secure`, only on a link it has
        authenticated by now. One that has not gets DM, as Bumble's multiplexer answers a PN it
        refuses, and lets the channel go: the device may authenticate and ask again."""
        assert self._controller is not None
        if not self.secure or self._controller.authenticated(connection.peer_address):
            return True
        self._refuse(connection, "the link is not authenticated")
        multiplexer.dlcs.pop(dlc.dlci, None)
        multiplexer.send_frame(rfcomm.RFCOMM_Frame.dm(c_r=1 - dlc.c_r, dlci=dlc.dlci))
        if self._holder is connection:
            self._holder = None
        return False

    def _refuse(self, connection: Connection, why: str) -> None:
        self.refused.append(Refusal(Duration(self.air._loop.now), f"{_named(connection)}: {why}"))

    def _opened(self, dlc: rfcomm.DLC) -> None:
        self._dlc = dlc
        self._openings.append(len(self.events))
        dlc.sink = self._received

        def closed() -> None:
            if self._dlc is dlc:
                self._dlc = None
                self._holder = None
                self._closed += 1

        dlc.on(dlc.EVENT_CLOSE, closed)
        # Bumble 0.0.236's DLC answers the device's DISC and stays open; the channel closes there.
        answer = dlc.on_disc_frame

        def disconnected(frame: rfcomm.RFCOMM_Frame) -> None:
            answer(frame)
            dlc.multiplexer.dlcs.pop(dlc.dlci, None)
            dlc.abort()

        dlc.on_disc_frame = disconnected  # type: ignore[method-assign,assignment]

    def _received(self, data: bytes) -> None:
        now = self.air._loop.now
        self.events.append(ChannelEvent(Duration(now), "out", data))
        with self._failing():
            self._queue(now, "receive", lambda: self._driving.receive(now, data))
            self._flush(now)

    # The air's `Party`.

    def poll(self, now: int) -> None:
        """Sends what is due at `now`: the source's answers, and what it sends unprompted."""
        with self._failing():
            self._queue(now, "poll", lambda: self._driving.poll(now))
            self._flush(now)

    def quiet_until(self) -> int | None:
        """The next time the source has something to send: its next answer, or its own
        `quiet_until`, which must be nanoseconds after the air's instant."""
        now = self.air._loop.now
        bound = self._call("quiet_until", self._driving.quiet_until)
        if bound is not None:
            if isinstance(bound, bool) or not isinstance(bound, int):
                raise PeerError(
                    f"{self.name}: source.quiet_until returned {bound!r}, not nanoseconds"
                )
            if bound <= now:
                raise PeerError(
                    f"{self.name}: source.quiet_until returned {seconds(bound)}, not after the"
                    f" air's {seconds(now)}: what it sends by then belongs in the poll at"
                    f" {seconds(now)}"
                )
        times = [bound]
        if self._pending:
            times.append(self._pending[0][0])
        due = [t for t in times if t is not None]
        return min(due) if due else None

    @contextmanager
    def _failing(self) -> Iterator[None]:
        """Inside the loop, where Bumble called the peer: what goes wrong fails the air."""
        try:
            yield
        except PeerError as error:
            self.air.fail(error)

    def _call(self, method: str, call: Callable[[], T]) -> T:
        try:
            return call()
        except _Misreturned:
            raise
        except Exception as error:
            raise PeerError(
                f"{self.name}: source.{method} raised {type(error).__name__}: {error}"
            ) from error

    def _queue(self, now: int, method: str, call: Callable[[], Iterable[Output]]) -> None:
        """What the source's `method` returns, read, checked and queued by time."""
        outputs = self._call(method, lambda: list(call()))
        for output in outputs:
            if not (
                isinstance(output, tuple)
                and len(output) == 2
                and isinstance(output[0], int)
                and not isinstance(output[0], bool)
                and isinstance(output[1], bytes)
            ):
                raise PeerError(
                    f"{self.name}: source.{method} returned {output!r:.80}, not a (t_ns, bytes)"
                    " pair"
                )
            t, data = output
            if t < now:
                raise PeerError(
                    f"{self.name}: source.{method} sent {data!r:.80} at {seconds(t)}, and the"
                    f" air is at {seconds(now)}: it cannot send into the past"
                )
            if not data:
                continue
            # After every output of the same time already queued: bytes keep their order.
            bisect.insort_right(self._pending, (t, data), key=lambda pending: pending[0])

    def _flush(self, now: int) -> None:
        while self._pending and self._pending[0][0] <= now:
            t, data = self._pending.pop(0)
            if self._dlc is None or not self._reaches_holder():
                self.lost += len(data)
                continue
            self.events.append(ChannelEvent(Duration(t), "in", data))
            self._dlc.write(data)

    def _reaches_holder(self) -> bool:
        """Whether what the peer sends reaches the device that holds its channel."""
        assert self._holder is not None
        return self.air._link.reaches(self.device.public_address, self._holder.peer_address)


def _named(connection: Connection) -> str:
    return str(connection.peer_address).removesuffix("/P")


def _address(kind: str, address: str) -> Address:
    """`address`, `XX:XX:XX:XX:XX:XX` in hex, as Bumble's public address; a `PeerError` for
    anything else, in the words of `kind`, the class of the device."""
    parts = address.split(":") if isinstance(address, str) else []
    if len(parts) != 6 or not all(
        len(part) == 2 and all(c in "0123456789abcdefABCDEF" for c in part) for part in parts
    ):
        raise PeerError(f"{kind}: address is {address!r}, not a Bluetooth address, as {ADDRESS}")
    return Address(address, Address.PUBLIC_DEVICE_ADDRESS)
