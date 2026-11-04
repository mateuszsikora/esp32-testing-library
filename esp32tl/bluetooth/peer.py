"""Bluetooth Classic devices on the air: any device, and one with a Serial Port Profile service.

`BluetoothDevice` is a Bumble device on the air: it can be discovered by its name, with its class
of device and its signal strength, pairs with its PIN if it has one (legacy pairing; it does not do
Secure Simple Pairing, as Bluetooth 2.0 devices do not), keeps the link keys it pairs to, offers no
service, and can be switched off and go out of range, and come back, with the link keys it had.

`SppPeer` adds SPP on one RFCOMM channel, to one device at a time and, unless told otherwise, only
on a link the device has authenticated. What the device writes on the channel goes to the source, a
`Source` as at the far end of a UART, and what the source sends goes out on the channel at the
board's time it gives.
"""

import bisect
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from typing import TypeVar

from bumble import core, rfcomm
from bumble.device import Connection, Device, DeviceConfiguration
from bumble.hci import Address
from bumble.host import Host
from bumble.keys import MemoryKeyStore
from bumble.pairing import PairingConfig, PairingDelegate
from bumble.transport.common import AsyncPipeSink

from esp32tl.backends.clock import seconds
from esp32tl.bluetooth.air import Air
from esp32tl.bluetooth.controller import RSSI, Controller
from esp32tl.interfaces.uart import Source, Supplied
from esp32tl.link.boardlink import Direction, Output
from esp32tl.units import Duration

# Unless the test gives another: an address no real device in a test is likely to have, and the
# class of device of a device that says nothing about itself (major class Uncategorized).
ADDRESS = "00:00:00:00:5B:01"
CLASS_OF_DEVICE = 0x001F00
CHANNEL = 1

# The longest name the extended inquiry response holds whole: its 240 bytes, less the field's
# length and type.
NAME_MAX = 238

# The SDP record's handle: the first one a server may give a record of its own.
RECORD_HANDLE = 0x00010001

T = TypeVar("T")


class PeerError(ValueError):
    """What the test tells a device that it cannot be, or what its source does that the peer
    cannot take: a call that raises, a result that is not bytes at a time, bytes stamped before
    the instant they are given at."""


class _Pin(PairingDelegate):
    """Answers a PIN request with the device's PIN, as a device with a fixed PIN does; without one,
    the pairing fails."""

    def __init__(self, pin: str | None) -> None:
        super().__init__(io_capability=PairingDelegate.KEYBOARD_INPUT_ONLY)
        self.pin = pin

    async def get_string(self, max_length: int) -> str | None:
        return self.pin


class BluetoothDevice:
    """A Classic device on `air` at `address`, discoverable and connectable from the start, called
    `name`, of class `class_of_device`, heard at `rssi` dBm, paired with `pin` if it has one, and
    offering no service: a device that is there to be found. `rssi` can be changed at any time, and
    the next inquiry gets it; the plain `Inquiry_Result` has no such field.

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
        public = _address(kind, address)
        if air.link.find_classic_controller(public) is not None:
            raise PeerError(
                f"{kind}: address {address} is another device's on the air: give each its own"
            )
        self.air = air
        self.name = name
        self.pin = pin
        self.class_of_device = class_of_device
        self._on = True
        self._in_range = True
        self._controller: Controller | None = None
        self.device = air.call(lambda: self._make(public))
        self.rssi = rssi
        air.run(self._power_on(), within=Duration(0), description=f"{name}'s power-on")

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
            self._controller.power_lost()
        if was_on != on:
            self._supply(on)
        if self.present and not was_present:
            self._controller.returned()

    def _supply(self, on: bool) -> None:
        """The device's power went off, or came back: nothing else here depends on it."""

    def _make(self, address: Address) -> Device:
        controller = Controller(self.name, self.air.link, str(address))
        self._controller = controller
        config = DeviceConfiguration(
            name=self.name,
            address=address,
            class_of_device=self.class_of_device,
            classic_enabled=True,
            le_enabled=False,
            classic_ssp_enabled=False,
            classic_sc_enabled=False,
        )
        device = Device(config=config, host=Host(controller, AsyncPipeSink(controller)))
        delegate = _Pin(self.pin)
        device.pairing_config_factory = lambda connection: PairingConfig(
            sc=False, mitm=False, bonding=True, delegate=delegate
        )
        self._serve(device)
        return device

    def _serve(self, device: Device) -> None:
        """The services `device` offers: none."""

    async def _power_on(self) -> None:
        await self.device.power_on()
        await self.device.set_discoverable(True)
        await self.device.set_connectable(True)


class SppPeer(BluetoothDevice):
    """A `BluetoothDevice` paired with `pin`, with SPP on RFCOMM `channel`, whose bytes `source`
    answers.

    The channel opens to one device at a time; with `secure`, the default, only on a link that
    device has authenticated. A channel the peer refuses is in `refused`, with its
    time and why. What the device writes reaches `source.receive` at the board's time it arrives,
    and what `source` returns, or sends from `poll`, goes out on the channel at its own time,
    never earlier. What the source sends while no channel is open is lost, as it would be on the
    air; `lost` counts its bytes. `events` holds what crossed the channel, in order: `out` of the
    device, `in` to it, as a tap's recording names them.

    The source gets `supply` on a power loss, when it is `Supplied`; out of range it runs on, and
    what it sends is lost.
    """

    def __init__(
        self,
        air: Air,
        *,
        name: str,
        pin: str,
        source: Source,
        address: str = ADDRESS,
        channel: int = CHANNEL,
        secure: bool = True,
        class_of_device: int = CLASS_OF_DEVICE,
        rssi: int = RSSI,
    ) -> None:
        if pin is None:
            raise PeerError(f"SppPeer: pin is {pin!r}, not a string of 1 to 16 bytes")
        if isinstance(channel, bool) or not isinstance(channel, int) or not 1 <= channel <= 30:
            raise PeerError(f"SppPeer: channel is {channel!r}, not an RFCOMM channel, 1 to 30")
        self.source = source
        self.channel = channel
        self.secure = secure
        self.events: list[tuple[int, Direction, bytes]] = []
        self.refused: list[tuple[int, str]] = []
        self.lost = 0
        self._pending: list[Output] = []
        self._dlc: rfcomm.DLC | None = None
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
        )
        air.join(self)

    @property
    def channel_open(self) -> bool:
        """Whether a device holds the SPP channel now."""
        return self._dlc is not None

    def _supply(self, on: bool) -> None:
        """A source that is `Supplied` loses its power with the peer, and what it had queued
        with it; any other runs on, and what it sends is lost."""
        if isinstance(self.source, Supplied):
            self._pending.clear()
            source = self.source
            with self._failing():
                self._call("supply", lambda: source.supply(on))

    def _serve(self, device: Device) -> None:
        device.sdp_service_records = {
            RECORD_HANDLE: rfcomm.make_service_sdp_records(
                RECORD_HANDLE, self.channel, core.BT_SERIAL_PORT_SERVICE
            )
        }
        server = rfcomm.Server(device)
        server.listen(self._opened, self.channel)

        def started(multiplexer: rfcomm.Multiplexer) -> None:
            # Bumble's server accepts every channel it listens on; this one asks the peer first.
            l2cap = multiplexer.l2cap_channel
            connection = l2cap.connection
            multiplexer.acceptor = lambda channel: self._accept(server, connection, channel)

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
        """The channel's frame size and credits for `connection`, or `None` to refuse it."""
        device = _named(connection)
        assert self._controller is not None
        if self._holder is not None:
            why = f"{device}: the channel is held by {_named(self._holder)}"
        elif self.secure and not self._controller.authenticated(connection.peer_address):
            why = f"{device}: the link is not authenticated"
        else:
            self._holder = connection

            def disconnected(reason: int) -> None:
                # A channel accepted and never opened is let go with its link.
                if self._holder is connection:
                    self._holder = None

            connection.on(connection.EVENT_DISCONNECTION, disconnected)
            return server.dlc_configs.get(channel)
        self.refused.append((self.air.loop.now, why))
        return None

    def _opened(self, dlc: rfcomm.DLC) -> None:
        self._dlc = dlc
        dlc.sink = self._received

        def closed() -> None:
            if self._dlc is dlc:
                self._dlc = None
                self._holder = None

        dlc.on(dlc.EVENT_CLOSE, closed)
        # Bumble 0.0.236's DLC answers the device's DISC and stays open; the channel closes there.
        answer = dlc.on_disc_frame

        def disconnected(frame: rfcomm.RFCOMM_Frame) -> None:
            answer(frame)
            dlc.multiplexer.dlcs.pop(dlc.dlci, None)
            dlc.abort()

        dlc.on_disc_frame = disconnected  # type: ignore[method-assign,assignment]

    def _received(self, data: bytes) -> None:
        now = self.air.loop.now
        self.events.append((now, "out", data))
        with self._failing():
            self._queue(now, "receive", lambda: self.source.receive(now, data))
            self._flush(now)

    # The air's `Party`.

    def poll(self, now: int) -> None:
        """Sends what is due at `now`: the source's answers, and what it sends unprompted."""
        with self._failing():
            self._queue(now, "poll", lambda: self.source.poll(now))
            self._flush(now)

    def quiet_until(self) -> int | None:
        """The next time the source has something to send: its next answer, or its own
        `quiet_until`, which must be nanoseconds after the air's instant."""
        now = self.air.loop.now
        bound = self._call("quiet_until", self.source.quiet_until)
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
            self.events.append((t, "in", data))
            self._dlc.write(data)

    def _reaches_holder(self) -> bool:
        """Whether what the peer sends reaches the device that holds its channel."""
        assert self._holder is not None
        return self.air.link.reaches(self.device.public_address, self._holder.peer_address)


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
