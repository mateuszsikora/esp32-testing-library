"""Bluetooth LE devices on the air: a peripheral the firmware scans for and connects to, and a
central that scans for the firmware's advertising and connects to it.

`BlePeripheral` advertises a name, service UUIDs and manufacturer data, and serves a GATT database
the test describes with `GattService` and `GattCharacteristic`: the firmware, as a central, reads
and writes its characteristics and subscribes to them, and the test reads what it wrote and
notifies or indicates it, at the board's instant. `BleCentral` scans, keeps every advertisement it
heard, connects to one, and discovers, reads, writes and subscribes to the firmware's GATT
database. Both pair with the LE Security Manager, Just Works unless their IO capability says
otherwise, keep the keys they bond with until `forget_keys`, and can be switched off and go out of
range, and come back, as a Classic device can.

What the central does over a connection takes time on the air: each call starts it at the board's
instant and returns a `Condition`, which `board.wait_until` waits for, and which finds what the
call got, or raises `BleError` for what went wrong. Every event of both, an advertisement seen
first, a connection, a disconnection and its reason, a write, a notification, a pairing, goes into
the device's `events` and, on the board's air, into the run log and `board.report()`.
"""

from collections.abc import Callable, Coroutine, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, NamedTuple, TypeVar

from bumble import att, core, gatt, hci
from bumble.core import UUID
from bumble.device import AdvertisingType, Connection, Device, DeviceConfiguration, Peer
from bumble.hci import Address
from bumble.host import Host
from bumble.pairing import PairingConfig, PairingDelegate
from bumble.transport.common import AsyncPipeSink

from esp32tl.backends.clock import nanoseconds
from esp32tl.bluetooth.air import Air
from esp32tl.bluetooth.controller import RSSI, SIX_DIGITS, Controller
from esp32tl.bluetooth.peer import PeerError, Radio, _address, _User
from esp32tl.interfaces.condition import Condition, FirstFrom
from esp32tl.units import Duration, ms

T = TypeVar("T")

# Unless the test gives another: addresses no real device in a test is likely to have, beside
# the Classic devices' 00:00:00:00:5B:01.
PERIPHERAL_ADDRESS = "00:00:00:00:5B:02"
CENTRAL_ADDRESS = "00:00:00:00:5C:01"
CENTRAL_NAME = "esp32tl central"

# The advertising interval unless the test gives another, and the range a host may set for
# connectable undirected advertising: 20 ms to 10.24 s (Vol 4, Part E, 7.8.5).
INTERVAL = ms(100)
INTERVAL_MIN_NS = 20_000_000
INTERVAL_MAX_NS = 10_240_000_000

# A legacy advertising PDU's data and a scan response's: 31 bytes each (Vol 6, Part B, 2.3.1).
AD_MAX = 31

# The flags of the advertising data: LE General Discoverable Mode, BR/EDR not supported (Vol 3,
# Part C, 11, and Supplement, Part A, 1.3).
FLAGS = 0x06

# The AD types esp32tl writes and reads (Assigned Numbers, 2.3).
AD_FLAGS = 0x01
AD_UUIDS_16 = (0x02, 0x03)
AD_UUIDS_32 = (0x04, 0x05)
AD_UUIDS_128 = (0x06, 0x07)
AD_SHORT_NAME = 0x08
AD_NAME = 0x09
AD_MANUFACTURER = 0xFF

# What a characteristic may do, by the names a test gives it (Vol 3, Part G, 3.3.1.1).
PROPERTIES = {
    "read": gatt.Characteristic.Properties.READ,
    "write": gatt.Characteristic.Properties.WRITE,
    "write_without_response": gatt.Characteristic.Properties.WRITE_WITHOUT_RESPONSE,
    "notify": gatt.Characteristic.Properties.NOTIFY,
    "indicate": gatt.Characteristic.Properties.INDICATE,
}

# The IO capabilities a device's user has for LE pairing (Vol 3, Part H, 2.3.2), by the names a
# test gives them: the Classic ones, and a keyboard with a display.
IO_CAPABILITIES = {
    "display_only": PairingDelegate.DISPLAY_OUTPUT_ONLY,
    "display_yes_no": PairingDelegate.DISPLAY_OUTPUT_AND_YES_NO_INPUT,
    "keyboard_only": PairingDelegate.KEYBOARD_INPUT_ONLY,
    "no_input_no_output": PairingDelegate.NO_OUTPUT_NO_INPUT,
    "keyboard_display": PairingDelegate.DISPLAY_OUTPUT_AND_KEYBOARD_INPUT,
}


class BleError(RuntimeError):
    """What went wrong with something a device did over the air: a connection that did not come,
    a GATT request the other side refused, a pairing that failed, a link that fell first. Raised by
    the `Condition` of the call that started it."""


@dataclass(frozen=True)
class GattCharacteristic:
    """A characteristic a `BlePeripheral` serves: `uuid`, a 16-bit UUID in hex, as `"FF01"`, or a
    128-bit one, as `"6E400002-B5A3-F393-E0A9-E50E24DCCA9E"`; its `value` at the start; what it may
    do, any of `PROPERTIES`, kept as a tuple; and, with `encrypted`, reads and writes only over an
    encrypted link. A characteristic that notifies or indicates has a Client Characteristic
    Configuration descriptor, which the central writes to subscribe."""

    uuid: str
    value: bytes = b""
    properties: Sequence[str] = ("read",)
    encrypted: bool = False

    def __post_init__(self) -> None:
        _uuid("GattCharacteristic", self.uuid)
        if not isinstance(self.value, bytes):
            raise PeerError(f"GattCharacteristic {self.uuid}: value is {self.value!r}, not bytes")
        properties = tuple(self.properties)
        if not properties or any(p not in PROPERTIES for p in properties):
            raise PeerError(
                f"GattCharacteristic {self.uuid}: properties are {self.properties!r}, not one or"
                f" more of {', '.join(PROPERTIES)}"
            )
        object.__setattr__(self, "properties", properties)
        if not isinstance(self.encrypted, bool):
            raise PeerError(
                f"GattCharacteristic {self.uuid}: encrypted is {self.encrypted!r}, not True or"
                " False"
            )


@dataclass(frozen=True)
class GattService:
    """A primary service a `BlePeripheral` serves: `uuid`, as a characteristic's, and its
    `characteristics`, in order, kept as a tuple."""

    uuid: str
    characteristics: Sequence[GattCharacteristic] = ()

    def __post_init__(self) -> None:
        _uuid("GattService", self.uuid)
        characteristics = tuple(self.characteristics)
        if not all(isinstance(c, GattCharacteristic) for c in characteristics):
            raise PeerError(
                f"GattService {self.uuid}: characteristics are {self.characteristics!r}, not"
                " GattCharacteristic objects"
            )
        object.__setattr__(self, "characteristics", characteristics)


class Advertisement(NamedTuple):
    """An advertising event a central heard, with its scan response when it asked for one: when
    (a Duration on the air's clock), from which address and of which type (`"public"` or
    `"random"`), what it says, its signal strength, whether it can be connected to, and its raw
    advertising data and scan response."""

    at: Duration
    address: str
    address_type: str
    name: str | None
    service_uuids: tuple[str, ...]
    manufacturer_data: tuple[int, bytes] | None
    rssi: int
    connectable: bool
    data: bytes
    scan_response: bytes


class LeConnection(NamedTuple):
    """An LE connection made: when, with which address, and this device's role in it, `"central"`
    or `"peripheral"`."""

    at: Duration
    peer: str
    role: str


class Disconnection(NamedTuple):
    """An LE connection that ended: when, with which address, and why, as an HCI error code:
    `0x08` (`CONNECTION_TIMEOUT`) for a link that lost contact, `0x13` for the remote side's
    disconnect, `0x16` for this side's own."""

    at: Duration
    peer: str
    reason: int

    @property
    def reason_name(self) -> str:
        return _reason(self.reason)


class GattWrite(NamedTuple):
    """A characteristic's value written: on a `BlePeripheral`, by the firmware; on a `BleCentral`,
    by the test, once the write is done."""

    at: Duration
    uuid: str
    value: bytes


class GattRead(NamedTuple):
    """A characteristic's value a `BleCentral` read."""

    at: Duration
    uuid: str
    value: bytes


class Notification(NamedTuple):
    """A characteristic's value notified, or indicated with `indication`: on a `BleCentral`, what
    it received; on a `BlePeripheral`, what it sent, once sent, or confirmed for an
    indication."""

    at: Duration
    uuid: str
    value: bytes
    indication: bool


class Subscription(NamedTuple):
    """A central's Client Characteristic Configuration written: to notifications, indications,
    both or neither."""

    at: Duration
    uuid: str
    notify: bool
    indicate: bool


class LePairing(NamedTuple):
    """A pairing that ended: when, with which address, whether it succeeded, whether the keys
    were kept (bonding), and for one that failed, the Security Manager's reason (Vol 3, Part H,
    3.5.5)."""

    at: Duration
    peer: str
    succeeded: bool
    bonded: bool
    reason: int | None


class DiscoveredCharacteristic(NamedTuple):
    """A characteristic of the firmware's GATT database: its UUID, handle and properties."""

    uuid: str
    handle: int
    properties: tuple[str, ...]


class DiscoveredService(NamedTuple):
    """A primary service of the firmware's GATT database, and its characteristics."""

    uuid: str
    characteristics: tuple[DiscoveredCharacteristic, ...]


class Discovery(NamedTuple):
    """The firmware's GATT database, as a `BleCentral` discovered it."""

    at: Duration
    services: tuple[DiscoveredService, ...]


class LeEvent(NamedTuple):
    """Something an LE device did, as the run log and the report name it."""

    at: Duration
    text: str


class _LeDevice(Device):
    """Bumble's device, which keeps the advertising reports its host hands it for the central."""

    reports: Callable[[Any], None] | None = None

    def on_advertising_report(self, report: Any) -> None:
        if self.reports is not None:
            self.reports(report)
        super().on_advertising_report(report)


class BleDevice(Radio):
    """An LE device on `air` at `address`, named `name`, heard at `rssi` dBm; with the IO
    capability `io_capability`, one of `IO_CAPABILITIES`, whose user says yes to a number when it
    `confirms`, and types `passkey` on a keyboard or shows it on a display, where Bumble would draw
    one; that keeps the keys it pairs to when `bonding`. `BlePeripheral` and `BleCentral` are the
    two kinds."""

    role = ""

    def __init__(
        self,
        air: Air,
        *,
        name: str,
        address: str,
        rssi: int,
        io_capability: str,
        confirms: bool,
        passkey: int | None,
        bonding: bool,
    ) -> None:
        kind = type(self).__name__
        if not isinstance(name, str) or not 1 <= len(name.encode()) <= 248:
            raise PeerError(f"{kind}: name is {name!r}, not a string of 1 to 248 bytes")
        self._check_rssi(rssi)
        if io_capability not in IO_CAPABILITIES:
            raise PeerError(
                f"{kind}: io_capability is {io_capability!r}, not one of"
                f" {', '.join(IO_CAPABILITIES)}"
            )
        if not isinstance(confirms, bool):
            raise PeerError(f"{kind}: confirms is {confirms!r}, not True or False")
        if not isinstance(bonding, bool):
            raise PeerError(f"{kind}: bonding is {bonding!r}, not True or False")
        if passkey is not None and (
            isinstance(passkey, bool)
            or not isinstance(passkey, int)
            or not 0 <= passkey < SIX_DIGITS
        ):
            raise PeerError(f"{kind}: passkey is {passkey!r}, not six digits, 0 to 999999")
        public = _address(kind, address)
        if air._link.find_classic_controller(public) is not None:
            raise PeerError(
                f"{kind}: address {address} is another device's on the air: give each its own"
            )
        self.air = air
        self.name = name
        self.io_capability = io_capability
        self.confirms = confirms
        self.passkey = passkey
        self.bonding = bonding
        self._on = True
        self._in_range = True
        self._controller: Controller | None = None
        self.events: list[LeEvent] = []
        self.connections: list[LeConnection] = []
        self.disconnections: list[Disconnection] = []
        self.pairings: list[LePairing] = []
        self._connection: Connection | None = None
        self.device = air.call(lambda: self._make(public))
        self.rssi = rssi
        air.le_devices.append(self)
        air.at_once(self._power_on(), description=f"{name}'s power-on")

    @property
    def address(self) -> str:
        return str(self.device.public_address).removesuffix("/P")

    def _make(self, address: Address) -> Device:
        controller = Controller(self.name, self.air._link, str(address))
        self._controller = controller
        config = DeviceConfiguration(
            name=self.name,
            address=address,
            classic_enabled=False,
            le_enabled=True,
        )
        device = _LeDevice(config=config, host=Host(controller, AsyncPipeSink(controller)))
        delegate = _User(None, IO_CAPABILITIES[self.io_capability], self.confirms, self.passkey)
        mitm = self.io_capability != "no_input_no_output"
        device.pairing_config_factory = lambda connection: PairingConfig(
            sc=True, mitm=mitm, bonding=self.bonding, delegate=delegate
        )
        device.on(device.EVENT_CONNECTION, self._connected)
        self._build(device)
        return device

    def _build(self, device: Device) -> None:
        """What the kind of device adds to `device`."""

    async def _power_on(self) -> None:
        await self.device.power_on()

    # What happened.

    def _note(self, text: str) -> None:
        now = self.air._loop.now
        self.events.append(LeEvent(Duration(now), text))
        self.air.note(f"bluetooth: {self.name}: {text}")

    @property
    def _now(self) -> Duration:
        return Duration(self.air._loop.now)

    def _connected(self, connection: Connection) -> None:
        if connection.transport != core.PhysicalTransport.LE:
            return
        role = "central" if connection.role == hci.Role.CENTRAL else "peripheral"
        if role != self.role:
            return
        self._connection = connection
        peer = _named(connection.peer_address)
        self.connections.append(LeConnection(self._now, peer, role))
        self._note(f"connected {'to' if role == 'central' else 'from'} {peer}, as {role}")

        def disconnected(reason: int) -> None:
            if self._connection is connection:
                self._connection = None
            self.disconnections.append(Disconnection(self._now, peer, reason))
            self._note(f"disconnected from {peer}: {_reason(reason)}")
            self._disconnected(connection)

        connection.on(connection.EVENT_DISCONNECTION, disconnected)
        connection.on(
            connection.EVENT_PAIRING,
            lambda keys: self._paired_with(connection, peer, keys),
        )
        connection.on(
            connection.EVENT_PAIRING_FAILURE,
            lambda reason: self._paired(peer, False, False, int(reason)),
        )

        def encryption_changed() -> None:
            if connection.encryption:
                self._note(f"encrypted with {peer}")

        connection.on(connection.EVENT_CONNECTION_ENCRYPTION_CHANGE, encryption_changed)
        connection.on(
            connection.EVENT_SECURITY_REQUEST,
            lambda auth_req: self.air._loop.create_task(self._secure(connection)),
        )
        self._connected_as(connection)

    def _connected_as(self, connection: Connection) -> None:
        """What the kind of device does on a connection of its role."""

    def _disconnected(self, connection: Connection) -> None:
        """What the kind of device does when a connection of its role ends."""

    def _paired_with(self, connection: Connection, peer: str, keys: Any) -> None:
        """A pairing that succeeded: bonded when both sides asked for bonding, as the Security
        Manager settles it (Vol 3, Part H, 2.3.3; Bumble's `Session.bonding`), and a long term key
        came of it, of LE Secure Connections or of legacy pairing. Bumble 0.0.236 keeps the keys
        either way; without a bond they are dropped, so that the next connection pairs again."""
        session = self.device.smp_manager.sessions.get(connection.handle)
        agreed = self.bonding if session is None else bool(session.bonding)
        bonded = agreed and _has_ltk(keys)
        if not bonded and self.device.keystore is not None:
            self.air._loop.create_task(self._drop_keys(str(connection.peer_address)))
        self._paired(peer, True, bonded, None)

    async def _drop_keys(self, name: str) -> None:
        """Deletes the keys kept under `name`, and builds Bumble's address resolver again, which
        took the peer's IRK when the keys were stored: without a bond the peer's private address
        is not resolved to its identity either."""
        assert self.device.keystore is not None
        try:
            await self.device.keystore.delete(name)
        except KeyError:
            return
        await self.device.refresh_resolving_list()

    def _paired(self, peer: str, succeeded: bool, bonded: bool, reason: int | None) -> None:
        self.pairings.append(LePairing(self._now, peer, succeeded, bonded, reason))
        if succeeded:
            self._note(f"paired with {peer}{', bonded' if bonded else ''}")
        else:
            self._note(f"pairing with {peer} failed: reason {reason:#04x}")

    async def _secure(self, connection: Connection) -> None:
        """The peer asked for security (Vol 3, Part H, 3.6.7): with keys kept for it, the link is
        encrypted with them; without, the device pairs, as a phone does."""
        keys = None
        if self.device.keystore is not None:
            keys = await self.device.keystore.get(str(connection.peer_address))
        try:
            if keys is not None and _has_ltk(keys):
                await connection.encrypt()
            else:
                await connection.pair()
        except Exception:
            # The pairing's failure is in `pairings`; a link that fell, in `disconnections`.
            return

    # The test's side.

    @property
    def connected(self) -> bool:
        """Whether the device has an LE connection of its role now."""
        return self._connection is not None

    @property
    def peer(self) -> str | None:
        """The address of the device this one is connected to, `None` without a connection."""
        connection = self._connection
        return None if connection is None else _named(connection.peer_address)

    @property
    def encrypted(self) -> bool:
        """Whether the connection is encrypted now."""
        connection = self._connection
        return connection is not None and bool(connection.encryption)

    @property
    def is_connected(self) -> Condition[bool]:
        """A described condition for the device having a connection now."""
        return Condition(
            lambda: self.connected, f"{self.name} is connected ({self.address})", "BLE"
        )

    @property
    def is_disconnected(self) -> Condition[bool]:
        """A described condition for the device having no connection now."""
        return Condition(
            lambda: not self.connected, f"{self.name} is disconnected ({self.address})", "BLE"
        )

    @property
    def is_encrypted(self) -> Condition[bool]:
        """A described condition for the device's connection being encrypted now."""
        return Condition(
            lambda: self.encrypted, f"{self.name}'s connection is encrypted ({self.address})", "BLE"
        )

    def next_connection(self) -> Condition[LeConnection | None]:
        """The first connection made after this call."""
        return self._after("connections", f"the next connection of {self.name}")

    def next_disconnection(self) -> Condition[Disconnection | None]:
        """The first disconnection after this call, with its reason."""
        return self._after("disconnections", f"the next disconnection of {self.name}")

    def next_pairing(self) -> Condition[LePairing | None]:
        """The first pairing to end after this call, successful or not."""
        return self._after("pairings", f"the next pairing of {self.name}")

    def _after(
        self, name: str, description: str, match: Callable[[T], bool] = lambda item: True
    ) -> Condition[T | None]:
        """A "next …" condition over the history in attribute `name`: the first record appended
        after this call that `match` accepts. Each poll reads only the records appended since the
        last one (`FirstFrom`), and the history is looked up by name at each, so that what the
        attribute holds then is what is read."""
        start = len(getattr(self, name))
        return Condition(
            FirstFrom(lambda: getattr(self, name), start, match),
            f"{description} ({self.address})",
            "BLE",
        )

    def forget_keys(self) -> None:
        """The device loses the keys it bonded with: the next time it connects to a device that
        kept its own, encrypting with them fails, and the two pair again."""
        super().forget_keys()
        self.device.address_resolver = None

    def _operation(
        self, description: str, coroutine: Coroutine[Any, Any, T]
    ) -> Condition[T | None]:
        """`coroutine`, started on the air at its current instant: a condition that finds what it
        returned, or raises `BleError` for what it raised."""
        outcome: dict[str, Any] = {}

        async def run() -> None:
            try:
                outcome["result"] = await coroutine
            except Exception as error:
                outcome["error"] = error

        self.air.start(run())

        def find() -> T | None:
            error = outcome.get("error")
            if error is not None:
                raise BleError(f"{description}: {_why(error)}") from error
            return outcome.get("result")

        return Condition(find, f"{description} ({self.address})", "BLE")


class BlePeripheral(BleDevice):
    """An LE peripheral on `air` at `address`, called `name`, that advertises every `interval`
    (100 ms unless the test gives another, 20 ms to 10.24 s) from its creation, and serves the
    GATT database of `services`.

    Its advertising says its name, `service_uuids` (the services' UUIDs unless the test gives
    others; none for an empty tuple) and `manufacturer_data`, a company identifier and its bytes;
    what the 31 bytes of the advertising data do not hold goes into the scan response, which an
    active scan gets. It is connectable unless `connectable` is false, takes one central at a time,
    and advertises again when the connection ends.

    The firmware's writes are in `writes`, and the current value of each characteristic is
    `value(uuid)`; `notify` and `indicate` send a value to the central that subscribed. The
    characteristics' UUIDs must differ: they name them. Pairing is the central's to start; the
    peripheral answers it, Just Works unless its IO capability says otherwise."""

    role = "peripheral"

    def __init__(
        self,
        air: Air,
        *,
        name: str,
        services: Iterable[GattService] = (),
        service_uuids: Iterable[str] | None = None,
        manufacturer_data: tuple[int, bytes] | None = None,
        address: str = PERIPHERAL_ADDRESS,
        interval: Duration = INTERVAL,
        connectable: bool = True,
        rssi: int = RSSI,
        io_capability: str = "no_input_no_output",
        confirms: bool = True,
        passkey: int | None = None,
        bonding: bool = True,
    ) -> None:
        self.services = tuple(services)
        if not all(isinstance(s, GattService) for s in self.services):
            raise PeerError(f"BlePeripheral: services are {services!r}, not GattService objects")
        uuids = [c.uuid for s in self.services for c in s.characteristics]
        keys = [_text(_uuid("GattCharacteristic", u)) for u in uuids]
        if len(set(keys)) != len(keys):
            twice = sorted({k for k in keys if keys.count(k) > 1})
            raise PeerError(
                f"BlePeripheral: characteristic {', '.join(twice)} is in the database twice: the"
                " UUIDs name the characteristics, give each its own"
            )
        if service_uuids is None:
            self.service_uuids = tuple(_text(_uuid("GattService", s.uuid)) for s in self.services)
        else:
            self.service_uuids = tuple(_text(_uuid("BlePeripheral", u)) for u in service_uuids)
        if manufacturer_data is not None and not (
            isinstance(manufacturer_data, tuple)
            and len(manufacturer_data) == 2
            and isinstance(manufacturer_data[0], int)
            and not isinstance(manufacturer_data[0], bool)
            and 0 <= manufacturer_data[0] <= 0xFFFF
            and isinstance(manufacturer_data[1], bytes)
        ):
            raise PeerError(
                f"BlePeripheral: manufacturer_data is {manufacturer_data!r}, not a (company"
                " identifier, bytes) pair"
            )
        self.manufacturer_data = manufacturer_data
        ns = nanoseconds(interval, "BlePeripheral: interval")
        if not INTERVAL_MIN_NS <= ns <= INTERVAL_MAX_NS:
            raise PeerError(f"BlePeripheral: interval is {interval}, not 20 ms to 10.24 s")
        self.interval = interval
        if not isinstance(connectable, bool):
            raise PeerError(f"BlePeripheral: connectable is {connectable!r}, not True or False")
        self.connectable = connectable
        name_bytes = name.encode() if isinstance(name, str) else b""
        self._advertising_data, self._scan_response = _advertising(
            name_bytes, self.service_uuids, manufacturer_data
        )
        self.writes: list[GattWrite] = []
        self.subscriptions: list[Subscription] = []
        self.notifications: list[Notification] = []
        self._values: dict[str, bytes] = {}
        self._characteristics: dict[str, gatt.Characteristic[Any]] = {}
        self._subscribed: dict[str, tuple[bool, bool]] = {}
        super().__init__(
            air,
            name=name,
            address=address,
            rssi=rssi,
            io_capability=io_capability,
            confirms=confirms,
            passkey=passkey,
            bonding=bonding,
        )

    def _build(self, device: Device) -> None:
        for service in self.services:
            attributes = []
            for described in service.characteristics:
                key = _text(_uuid("GattCharacteristic", described.uuid))
                self._values[key] = described.value
                properties = gatt.Characteristic.Properties(0)
                for name in described.properties:
                    properties |= PROPERTIES[name]
                permissions = gatt.Characteristic.Permissions(0)
                if "read" in described.properties:
                    permissions |= gatt.Characteristic.Permissions.READABLE
                    if described.encrypted:
                        permissions |= gatt.Characteristic.Permissions.READ_REQUIRES_ENCRYPTION
                if {"write", "write_without_response"} & set(described.properties):
                    permissions |= gatt.Characteristic.Permissions.WRITEABLE
                    if described.encrypted:
                        permissions |= gatt.Characteristic.Permissions.WRITE_REQUIRES_ENCRYPTION
                readable = "read" in described.properties
                writable = bool({"write", "write_without_response"} & set(described.properties))
                value = gatt.CharacteristicValue(
                    read=lambda connection, key=key, readable=readable: self._read(key, readable),
                    write=lambda connection, data, key=key, writable=writable: self._written(
                        key, bytes(data), writable
                    ),
                )
                characteristic = gatt.Characteristic(
                    _uuid("GattCharacteristic", described.uuid), properties, permissions, value
                )
                characteristic.on(
                    characteristic.EVENT_SUBSCRIPTION,
                    lambda connection, notify, indicate, key=key: self._subscription(
                        key, bool(notify), bool(indicate)
                    ),
                )
                self._characteristics[key] = characteristic
                attributes.append(characteristic)
            device.add_service(gatt.Service(_uuid("GattService", service.uuid), attributes))

    async def _power_on(self) -> None:
        await super()._power_on()
        await self._advertise()

    async def _advertise(self) -> None:
        # Bumble's device starts its advertising again when a connection ends, also the one a
        # power loss ended: it is stopped first, which the controller that restarted takes too.
        if self.device.legacy_advertiser is not None:
            await self.device.stop_advertising()
        interval_ms = nanoseconds(self.interval, "BlePeripheral: interval") / 1e6
        await self.device.start_advertising(
            advertising_type=(
                AdvertisingType.UNDIRECTED_CONNECTABLE_SCANNABLE
                if self.connectable
                else AdvertisingType.UNDIRECTED_SCANNABLE
            ),
            own_address_type=hci.OwnAddressType.PUBLIC,
            auto_restart=True,
            advertising_data=self._advertising_data,
            scan_response_data=self._scan_response,
            advertising_interval_min=interval_ms,
            advertising_interval_max=interval_ms,
        )

    def _supply(self, on: bool) -> None:
        if on:
            self.air._loop.create_task(self._advertise())

    @property
    def advertising(self) -> bool:
        """Whether the peripheral advertises now: from its creation, and again after each
        connection, while it is switched on."""
        assert self._controller is not None
        return bool(self._controller.le_legacy_advertiser.enabled)

    # The GATT database.

    def _key(self, uuid: str) -> str:
        key = _text(_uuid("BlePeripheral", uuid))
        if key not in self._values:
            raise PeerError(f"{self.name}: no characteristic {uuid} in its database")
        return key

    def value(self, uuid: str) -> bytes:
        """The characteristic's value now: its value at the start, or what was last written or
        set."""
        return self._values[self._key(uuid)]

    def set_value(self, uuid: str, value: bytes) -> None:
        """Sets the characteristic's value, which the next read gets, without notifying."""
        if not isinstance(value, bytes):
            raise TypeError(f"{self.name}: value is {value!r}, not bytes")
        self._values[self._key(uuid)] = value

    def _read(self, key: str, readable: bool) -> bytes:
        """A read of the characteristic: refused when it is not readable, which Bumble 0.0.236's
        server does not check (Vol 3, Part F, 3.4.4.4: `Read Not Permitted`)."""
        if not readable:
            raise att.ATT_Error(att.ATT_READ_NOT_PERMITTED_ERROR)
        return self._values[key]

    def _written(self, key: str, data: bytes, writable: bool) -> None:
        """A write of the characteristic: refused when it is not writable, which Bumble's server
        does not check either (`Write Not Permitted`)."""
        if not writable:
            raise att.ATT_Error(att.ATT_WRITE_NOT_PERMITTED_ERROR)
        self._values[key] = data
        self.writes.append(GattWrite(self._now, key, data))
        self._note(f"{self.peer} wrote {key}: {data.hex()}")

    def _subscription(self, key: str, notify: bool, indicate: bool) -> None:
        self._subscribed[key] = (notify, indicate)
        self.subscriptions.append(Subscription(self._now, key, notify, indicate))
        kinds = [k for k, on in (("notifications", notify), ("indications", indicate)) if on]
        self._note(f"{self.peer} subscribed to {key}: {' and '.join(kinds) or 'nothing'}")

    def _disconnected(self, connection: Connection) -> None:
        self._subscribed.clear()

    def subscribed(self, uuid: str) -> bool:
        """Whether the connected central subscribed to the characteristic's notifications or
        indications."""
        return any(self._subscribed.get(self._key(uuid), (False, False)))

    def is_subscribed(self, uuid: str) -> Condition[bool]:
        """A described condition for the connected central being subscribed to the
        characteristic."""
        key = self._key(uuid)
        return Condition(
            lambda: self.subscribed(key),
            f"a central subscribed to {key} of {self.name} ({self.address})",
            "BLE",
        )

    def next_write(
        self, uuid: str | None = None, value: bytes | None = None
    ) -> Condition[GattWrite | None]:
        """The first write by the firmware after this call, to `uuid` if given, of a value that
        contains `value` if given."""
        key = None if uuid is None else self._key(uuid)
        if value is not None and not isinstance(value, bytes):
            raise TypeError(f"BlePeripheral.next_write: value is {value!r}, not bytes or None")
        description = f"the next write to {key or 'a characteristic'} of {self.name}"
        if value is not None:
            description += f" containing {value!r}"
        return self._after(
            "writes",
            description,
            lambda w: (key is None or w.uuid == key) and (value is None or value in w.value),
        )

    def notify(self, uuid: str, value: bytes) -> Condition[Notification | None]:
        """Sets the characteristic's value and notifies it to the subscribed central, at the
        board's instant; the condition finds the `Notification` once it is sent."""
        return self._send(uuid, value, indication=False)

    def indicate(self, uuid: str, value: bytes) -> Condition[Notification | None]:
        """Sets the characteristic's value and indicates it to the subscribed central, at the
        board's instant; the condition finds the `Notification` once the central confirmed it."""
        return self._send(uuid, value, indication=True)

    def _send(self, uuid: str, value: bytes, *, indication: bool) -> Condition[Notification | None]:
        key = self._key(uuid)
        if not isinstance(value, bytes):
            raise TypeError(f"{self.name}: value is {value!r}, not bytes")
        what = "indication" if indication else "notification"
        notify, indicate = self._subscribed.get(key, (False, False))
        connection = self._connection
        if connection is None or not (indicate if indication else notify):
            raise PeerError(
                f"{self.name}: no central is subscribed to {what}s of {key}: wait for"
                f" peripheral.is_subscribed({key!r})"
            )
        self._values[key] = value
        characteristic = self._characteristics[key]

        async def send() -> Notification:
            if indication:
                await self.device.indicate_subscriber(connection, characteristic, value)
            else:
                await self.device.notify_subscriber(connection, characteristic, value)
            sent = Notification(self._now, key, value, indication)
            self.notifications.append(sent)
            self._note(f"{what} of {key} to {_named(connection.peer_address)}: {value.hex()}")
            return sent

        return self._operation(f"the {what} of {key} by {self.name}", send())


class BleCentral(BleDevice):
    """An LE central on `air` at `address`, called `name`, that scans from its creation, actively,
    and keeps every advertising event it hears, with its scan response, in `advertisements`.

    `connect` connects to an advertiser; over the connection `discover` discovers its GATT
    database, `read`, `write` and `subscribe` use its characteristics, by UUID, discovering the
    database first when it has not, and `pair` pairs. Each returns a `Condition` for
    `board.wait_until`. The notifications and indications it receives are in `notifications`. A
    security request of the peer is answered as a phone answers it: with the keys kept for the
    peer, the link is encrypted, and without, the central pairs. Its connections have Bumble's
    parameters: an interval of 15 to 30 ms, and a supervision timeout of 720 ms, until the
    peripheral asks for others."""

    role = "central"

    def __init__(
        self,
        air: Air,
        *,
        name: str = CENTRAL_NAME,
        address: str = CENTRAL_ADDRESS,
        rssi: int = RSSI,
        io_capability: str = "no_input_no_output",
        confirms: bool = True,
        passkey: int | None = None,
        bonding: bool = True,
    ) -> None:
        self.advertisements: list[Advertisement] = []
        self.notifications: list[Notification] = []
        self.discovery: Discovery | None = None
        self._seen: set[str] = set()
        # The advertising event of each address that waits for its scan response.
        self._pending: dict[tuple[str, str], tuple[bytes, bool, int]] = {}
        self._peer: Peer | None = None
        super().__init__(
            air,
            name=name,
            address=address,
            rssi=rssi,
            io_capability=io_capability,
            confirms=confirms,
            passkey=passkey,
            bonding=bonding,
        )

    def _build(self, device: Device) -> None:
        assert isinstance(device, _LeDevice)
        device.reports = self._report

    async def _power_on(self) -> None:
        await super()._power_on()
        await self._scan()

    async def _scan(self) -> None:
        if self.device.is_scanning:
            await self.device.stop_scanning()
        await self.device.start_scanning(active=True, own_address_type=hci.OwnAddressType.PUBLIC)

    def _supply(self, on: bool) -> None:
        if on:
            self.air._loop.create_task(self._scan())

    # Scanning.

    def _report(self, report: hci.HCI_LE_Advertising_Report_Event.Report) -> None:
        EventType = hci.HCI_LE_Advertising_Report_Event.EventType
        address = _named(report.address)
        kind = "public" if report.address.is_public else "random"
        if report.event_type == EventType.SCAN_RSP:
            pending = self._pending.pop((address, kind), None)
            if pending is not None:
                data, connectable, _ = pending
                self._heard(address, kind, data, bytes(report.data), connectable, report.rssi)
            return
        connectable = report.event_type in (EventType.ADV_IND, EventType.ADV_DIRECT_IND)
        if report.event_type in (EventType.ADV_IND, EventType.ADV_SCAN_IND):
            self._pending[(address, kind)] = (bytes(report.data), connectable, report.rssi)
            return
        self._heard(address, kind, bytes(report.data), b"", connectable, report.rssi)

    def _heard(
        self,
        address: str,
        kind: str,
        data: bytes,
        scan_response: bytes,
        connectable: bool,
        rssi: int,
    ) -> None:
        fields = _fields(data) + _fields(scan_response)
        advertisement = Advertisement(
            self._now,
            address,
            kind,
            _name_of(fields),
            _uuids_of(fields),
            _manufacturer_of(fields),
            rssi,
            connectable,
            data,
            scan_response,
        )
        self.advertisements.append(advertisement)
        if address not in self._seen:
            self._seen.add(address)
            name = "" if advertisement.name is None else f" {advertisement.name!r}"
            self._note(f"advertising seen from{name} {address} ({kind})")

    def next_advertisement(
        self,
        *,
        name: str | None = None,
        address: str | None = None,
        service_uuid: str | None = None,
    ) -> Condition[Advertisement | None]:
        """The first advertising event heard after this call, from a device called `name`, at
        `address` or advertising `service_uuid`, those that are given."""
        uuid = None if service_uuid is None else _text(_uuid("BleCentral", service_uuid))
        parts = [
            f"called {name!r}" if name is not None else "",
            f"at {address}" if address is not None else "",
            f"with service {uuid}" if uuid is not None else "",
        ]
        described = " ".join(p for p in parts if p)
        description = f"the next advertisement {described + ' ' if described else ''}heard by"
        return self._after(
            "advertisements",
            f"{description} {self.name}",
            lambda a: (
                (name is None or a.name == name)
                and (address is None or a.address.upper() == address.upper())
                and (uuid is None or uuid in a.service_uuids)
            ),
        )

    # Connecting.

    def connect(self, to: "Advertisement | str") -> Condition[LeConnection | None]:
        """Connects to the advertiser of `to`, an `Advertisement` heard, or a public address, at the
        next connectable advertising event heard from it."""
        if isinstance(to, Advertisement):
            kind = (
                Address.PUBLIC_DEVICE_ADDRESS
                if to.address_type == "public"
                else Address.RANDOM_DEVICE_ADDRESS
            )
            target = Address(to.address, kind)
        elif isinstance(to, str):
            target = _address("BleCentral", to)
        else:
            raise TypeError(f"BleCentral.connect: to is {to!r}, not an Advertisement or an address")
        if self._connection is not None:
            raise PeerError(
                f"{self.name}: connected to {self.peer} already: one connection at a time"
            )
        if self.device.is_le_connecting:
            raise PeerError(
                f"{self.name}: a connection is being made already: wait for it, or end it with"
                " central.cancel_connect()"
            )

        async def connect() -> LeConnection:
            await self.device.connect(target, own_address_type=hci.OwnAddressType.PUBLIC)
            return self.connections[-1]

        return self._operation(f"{self.name}'s connection to {_named(target)}", connect())

    def cancel_connect(self) -> None:
        """Ends the connection being made, at the board's instant: the controller cancels the
        initiation (`LE_Create_Connection_Cancel`), and `connect`'s condition raises `BleError`.
        A device that never advertises connectably, or is off the air, is never connected to
        otherwise: an initiation has no timeout. Nothing to cancel is nothing done."""

        async def cancel() -> None:
            if self.device.is_le_connecting:
                # Bumble leaves it unannotated.
                await self.device.cancel_connection()  # type: ignore[no-untyped-call]

        self.air.start(cancel())

    def disconnect(self) -> Condition[Disconnection | None]:
        """Ends the connection; the condition finds the `Disconnection` once it has."""
        connection = self._require("disconnect")
        count = len(self.disconnections)

        async def disconnect() -> Disconnection:
            await connection.disconnect()
            # Bumble returns once the host is told; the history has it by then.
            return self.disconnections[count]

        return self._operation(f"{self.name}'s disconnection", disconnect())

    def _connected_as(self, connection: Connection) -> None:
        self._peer = Peer(connection)
        self.discovery = None

        def received(key: str, indication: bool) -> Callable[[bytes], None]:
            def on(value: bytes) -> None:
                data = bytes(value)
                self.notifications.append(Notification(self._now, key, data, indication))
                kind = "indication" if indication else "notification"
                self._note(f"{kind} of {key} from {_named(connection.peer_address)}: {data.hex()}")

            return on

        self._receiver = received

    def _disconnected(self, connection: Connection) -> None:
        self._peer = None

    def _require(self, call: str) -> Connection:
        connection = self._connection
        if connection is None:
            raise PeerError(
                f"{self.name}: not connected: {call} needs a connection, made with"
                " central.connect(...)"
            )
        return connection

    # GATT.

    async def _discovered(self) -> Discovery:
        if self.discovery is not None:
            return self.discovery
        peer = self._peer
        assert peer is not None
        await peer.discover_services()
        services = []
        for service in peer.services:
            await service.discover_characteristics()
            characteristics = []
            for characteristic in service.characteristics:
                await characteristic.discover_descriptors()
                characteristics.append(
                    DiscoveredCharacteristic(
                        _text(characteristic.uuid),
                        characteristic.handle,
                        tuple(n for n, p in PROPERTIES.items() if characteristic.properties & p),
                    )
                )
            services.append(DiscoveredService(_text(service.uuid), tuple(characteristics)))
        self.discovery = Discovery(self._now, tuple(services))
        listed = ", ".join(s.uuid for s in services)
        self._note(f"discovered {len(services)} services of {self.peer}: {listed}")
        return self.discovery

    async def _characteristic(self, uuid: str) -> Any:
        await self._discovered()
        peer = self._peer
        assert peer is not None
        found = peer.get_characteristics_by_uuid(_uuid("BleCentral", uuid))
        if not found:
            raise BleError(f"{self.peer} has no characteristic {_text(_uuid('BleCentral', uuid))}")
        return found[0]

    def discover(self) -> Condition[Discovery | None]:
        """Discovers the peer's primary services, their characteristics and descriptors; the
        condition finds the `Discovery`, kept in `discovery` until the connection ends."""
        self._require("discover")
        return self._operation(f"{self.name}'s discovery of {self.peer}", self._discovered())

    def read(self, uuid: str) -> Condition[GattRead | None]:
        """Reads the characteristic's value."""
        self._require("read")
        key = _text(_uuid("BleCentral", uuid))

        async def read() -> GattRead:
            characteristic = await self._characteristic(key)
            value = bytes(await characteristic.read_value())
            self._note(f"read {key} of {self.peer}: {value.hex()}")
            return GattRead(self._now, key, value)

        return self._operation(f"{self.name}'s read of {key}", read())

    def write(
        self, uuid: str, value: bytes, *, with_response: bool = True
    ) -> Condition[GattWrite | None]:
        """Writes `value` to the characteristic, with a write request unless `with_response` is
        false, when it is a write command, done once sent."""
        self._require("write")
        if not isinstance(value, bytes):
            raise TypeError(f"BleCentral.write: value is {value!r}, not bytes")
        key = _text(_uuid("BleCentral", uuid))

        async def write() -> GattWrite:
            characteristic = await self._characteristic(key)
            await characteristic.write_value(value, with_response=with_response)
            self._note(f"wrote {key} of {self.peer}: {value.hex()}")
            return GattWrite(self._now, key, value)

        return self._operation(f"{self.name}'s write of {key}", write())

    def subscribe(self, uuid: str, *, indicate: bool = False) -> Condition[Subscription | None]:
        """Subscribes to the characteristic's notifications, or its indications with `indicate`:
        what the peer sends from then on is in `notifications`."""
        self._require("subscribe")
        key = _text(_uuid("BleCentral", uuid))

        async def subscribe() -> Subscription:
            characteristic = await self._characteristic(key)
            await characteristic.subscribe(
                self._receiver(key, indicate), prefer_notify=not indicate
            )
            kind = "indications" if indicate else "notifications"
            self._note(f"subscribed to {kind} of {key} of {self.peer}")
            return Subscription(self._now, key, not indicate, indicate)

        return self._operation(f"{self.name}'s subscription to {key}", subscribe())

    def next_notification(
        self, uuid: str | None = None, value: bytes | None = None
    ) -> Condition[Notification | None]:
        """The first notification or indication received after this call, of `uuid` if given,
        with a value that contains `value` if given."""
        key = None if uuid is None else _text(_uuid("BleCentral", uuid))
        if value is not None and not isinstance(value, bytes):
            raise TypeError(f"BleCentral.next_notification: value is {value!r}, not bytes or None")
        description = f"the next notification of {key or 'a characteristic'} to {self.name}"
        if value is not None:
            description += f" containing {value!r}"
        return self._after(
            "notifications",
            description,
            lambda n: (key is None or n.uuid == key) and (value is None or value in n.value),
        )

    def pair(self) -> Condition[LePairing | None]:
        """Pairs with the peer, as the initiator; the condition finds the `LePairing`, and raises
        `BleError` for one that failed."""
        connection = self._require("pair")
        count = len(self.pairings)

        async def pair() -> LePairing:
            try:
                await connection.pair()
            except Exception:
                if len(self.pairings) > count:
                    failed = self.pairings[count]
                    raise BleError(f"reason {failed.reason:#04x}") from None
                raise
            return self.pairings[count]

        return self._operation(f"{self.name}'s pairing with {self.peer}", pair())


# Helpers.


def _uuid(kind: str, text: str) -> UUID:
    """`text` as a UUID: 4 hex digits for a 16-bit one, or a 128-bit one; a `PeerError` naming
    `kind` for anything else."""
    if isinstance(text, str):
        bare = text.replace("-", "")
        if len(bare) in (4, 32) and all(c in "0123456789abcdefABCDEF" for c in bare):
            return UUID(text)
    raise PeerError(
        f"{kind}: uuid is {text!r}, not a 16-bit UUID in hex, as 'FF01', or a 128-bit one"
    )


def _text(uuid: UUID) -> str:
    """A UUID as the test writes it: `FF01`, or the 128-bit form with its dashes."""
    if len(uuid.uuid_bytes) == 2:
        return uuid.to_hex_str()
    hex_ = uuid.to_hex_str()
    return f"{hex_[:8]}-{hex_[8:12]}-{hex_[12:16]}-{hex_[16:20]}-{hex_[20:]}"


def _named(address: Address) -> str:
    return str(address).removesuffix("/P")


def _has_ltk(keys: Any) -> bool:
    """Whether Bumble's `PairingKeys` hold a long term key: LE Secure Connections' `ltk`, or legacy
    pairing's `ltk_central` and `ltk_peripheral`."""
    return any(k is not None for k in (keys.ltk, keys.ltk_central, keys.ltk_peripheral))


def _why(error: BaseException) -> str:
    """What went wrong, in a few words: an ATT error by its name and code (Vol 3, Part F,
    3.4.1.1), esp32tl's own by its message, anything else by its type and message."""
    if isinstance(error, BleError):
        return str(error)
    if isinstance(error, att.ATT_Error):
        code = int(error.error_code)
        try:
            name = att.ErrorCode(code).name
        except ValueError:
            name = "UNKNOWN"
        return f"the peer answered {name} ({code:#04x})"
    return f"{type(error).__name__}: {error}"


def _reason(reason: int) -> str:
    try:
        name = hci.HCI_ErrorCode(reason).name
    except ValueError:
        name = "UNKNOWN"
    return f"{name} ({reason:#04x})"


def _ad(kind: int, data: bytes) -> bytes:
    return bytes([len(data) + 1, kind]) + data


def _advertising(
    name: bytes, uuids: tuple[str, ...], manufacturer: tuple[int, bytes] | None
) -> tuple[bytes, bytes]:
    """The advertising data and the scan response: the flags first, then the name, the service
    UUIDs and the manufacturer data, each in the advertising data if it still fits, else in the
    scan response. A name too long for either goes as a shortened name, its first bytes, as many
    whole characters as a scan response holds (Supplement, Part A, 1.2); the device's GATT name is
    whole. Anything else that fits in neither is a `PeerError`."""
    parts = []
    if len(name) > AD_MAX - 2:
        short = name[: AD_MAX - 2].decode(errors="ignore").encode()
        parts.append(("the shortened name", _ad(AD_SHORT_NAME, short)))
    elif name:
        parts.append(("the name", _ad(AD_NAME, name)))
    sixteen = [_uuid("BlePeripheral", u) for u in uuids if len(u) == 4]
    long = [_uuid("BlePeripheral", u) for u in uuids if len(u) != 4]
    if sixteen:
        parts.append(
            ("the 16-bit service UUIDs", _ad(AD_UUIDS_16[1], b"".join(bytes(u) for u in sixteen)))
        )
    if long:
        parts.append(
            ("the 128-bit service UUIDs", _ad(AD_UUIDS_128[1], b"".join(bytes(u) for u in long)))
        )
    if manufacturer is not None:
        company, data = manufacturer
        parts.append(
            ("the manufacturer data", _ad(AD_MANUFACTURER, company.to_bytes(2, "little") + data))
        )
    advertising = _ad(AD_FLAGS, bytes([FLAGS]))
    scan_response = b""
    for what, part in parts:
        if len(advertising) + len(part) <= AD_MAX:
            advertising += part
        elif len(scan_response) + len(part) <= AD_MAX:
            scan_response += part
        else:
            raise PeerError(
                f"BlePeripheral: {what} ({len(part)} bytes) fit neither in the advertising data nor"
                f" in the scan response, {AD_MAX} bytes each"
            )
    return advertising, scan_response


def _fields(data: bytes) -> list[tuple[int, bytes]]:
    """The AD structures of advertising data (Vol 3, Part C, 11): what is cut short is left out."""
    fields = []
    at = 0
    while at < len(data):
        length = data[at]
        if length == 0 or at + 1 + length > len(data):
            break
        fields.append((data[at + 1], data[at + 2 : at + 1 + length]))
        at += 1 + length
    return fields


def _name_of(fields: list[tuple[int, bytes]]) -> str | None:
    for wanted in (AD_NAME, AD_SHORT_NAME):
        for kind, data in fields:
            if kind == wanted:
                return data.decode(errors="replace")
    return None


def _uuids_of(fields: list[tuple[int, bytes]]) -> tuple[str, ...]:
    uuids: list[str] = []
    for kind, data in fields:
        size = (
            2
            if kind in AD_UUIDS_16
            else 4
            if kind in AD_UUIDS_32
            else 16
            if kind in AD_UUIDS_128
            else 0
        )
        if size:
            for at in range(0, len(data) - size + 1, size):
                uuids.append(_text(UUID.from_bytes(data[at : at + size])))
    return tuple(uuids)


def _manufacturer_of(fields: list[tuple[int, bytes]]) -> tuple[int, bytes] | None:
    for kind, data in fields:
        if kind == AD_MANUFACTURER and len(data) >= 2:
            return int.from_bytes(data[:2], "little"), data[2:]
    return None
