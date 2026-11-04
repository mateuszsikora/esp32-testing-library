"""The SPP peer (`esp32tl/bluetooth/peer.py`) and the controller under it
(`esp32tl/bluetooth/controller.py`) on a Bumble virtual link, without the emulator.

A second Bumble device stands in for the ESP32 and does what ESP-IDF's `bt_spp_initiator` does:
an inquiry until it finds the peer's name, a connection, authentication with a PIN, encryption,
an SDP search for the Serial Port service, and an RFCOMM channel to it. A model in the test
answers what it writes, on the board's clock."""

import asyncio
import re
from collections.abc import Iterable, Iterator
from typing import Any

import pytest
from bumble import core, hci, rfcomm
from bumble.device import Connection, Device, DeviceConfiguration
from bumble.host import Host
from bumble.keys import MemoryKeyStore
from bumble.pairing import PairingConfig, PairingDelegate
from bumble.transport.common import AsyncPipeSink

from esp32tl.bluetooth.air import Air, AirError
from esp32tl.bluetooth.controller import INQUIRY_UNIT_S, Controller
from esp32tl.bluetooth.peer import (
    CHANNEL,
    CLASS_OF_DEVICE,
    BluetoothDevice,
    PeerError,
    SppPeer,
)
from esp32tl.interfaces import NeverHappened
from esp32tl.units import ms, seconds

MS = 1_000_000
DEVICE = "24:0A:C4:00:00:01"


class Elm:
    """A model as a test writes one: it answers every line the device sends with the line,
    `delay` later, and a prompt; it sends `greeting` at `at`, unprompted."""

    def __init__(self, delay: int = 50 * MS, greeting: bytes = b"", at: int = 0) -> None:
        self.delay = delay
        self.greeting = greeting
        self.at = at
        self.heard: list[tuple[int, bytes]] = []
        self._greeted = not greeting

    def receive(self, t: int, data: bytes) -> Iterable[tuple[int, bytes]]:
        self.heard.append((t, data))
        return [(t + self.delay, data.rstrip(b"\r") + b"\r\r>")]

    def poll(self, now: int) -> Iterable[tuple[int, bytes]]:
        if self._greeted or now < self.at:
            return ()
        self._greeted = True
        return [(self.at, self.greeting)]

    def quiet_until(self) -> int | None:
        return None if self._greeted else self.at

    def latency(self) -> int | None:
        return self.delay


class Pin(PairingDelegate):
    """The stand-in's PIN, and how many times it was asked for it."""

    def __init__(self, pin: str | None) -> None:
        super().__init__(io_capability=PairingDelegate.KEYBOARD_INPUT_ONLY)
        self.pin = pin
        self.asked = 0

    async def get_string(self, max_length: int) -> str | None:
        self.asked += 1
        return self.pin


class Initiator:
    """The ESP32's side, as `bt_spp_initiator` drives it, on a Bumble host."""

    def __init__(
        self,
        air: Air,
        pin: str | None = "1234",
        ssp: bool = False,
        address: str = DEVICE,
        name: str = "esp32",
    ) -> None:
        self.air = air
        self.delegate = Pin(pin)
        self.found: list[tuple[int, str, int, str | None]] = []
        # The signal strength of each result, as (address, rssi).
        self.heard_at: list[tuple[str, int]] = []
        self.received: list[tuple[int, bytes]] = []
        self.inquiry_complete: list[int] = []
        # Each link of this device that fell, as (t, reason).
        self.fell: list[tuple[int, int]] = []
        self.device = air.call(lambda: self._make(ssp, address, name))
        air.run(self.device.power_on(), within=seconds(0))

    def _make(self, ssp: bool, address: str, name: str) -> Device:
        controller = Controller(name, self.air.link, address)
        config = DeviceConfiguration(
            name=name,
            address=hci.Address(address),
            classic_enabled=True,
            le_enabled=False,
            classic_ssp_enabled=ssp,
            classic_sc_enabled=False,
        )
        device = Device(config=config, host=Host(controller, AsyncPipeSink(controller)))
        device.pairing_config_factory = lambda connection: PairingConfig(
            sc=False, mitm=False, bonding=True, delegate=self.delegate
        )
        device.on("inquiry_result", self._on_result)
        device.on("inquiry_complete", lambda: self.inquiry_complete.append(self.air.loop.now))
        device.on(device.EVENT_CONNECTION, self._on_connection)
        return device

    def _on_connection(self, connection: Connection) -> None:
        def fell(reason: int) -> None:
            self.fell.append((self.air.loop.now, reason))

        connection.on(connection.EVENT_DISCONNECTION, fell)

    def _on_result(
        self, address: hci.Address, class_of_device: int, eir: core.AdvertisingData, rssi: int
    ) -> None:
        name = eir.get(core.AdvertisingData.COMPLETE_LOCAL_NAME)
        assert name is None or isinstance(name, str)
        self.found.append((self.air.loop.now, str(address), class_of_device, name))
        self.heard_at.append((str(address), rssi))

    async def discover(self, name: str) -> hci.Address:
        """An inquiry until a device called `name` answers; then the inquiry is cancelled."""
        await self.device.start_discovery(auto_restart=False)
        while True:
            for _, address, _, found in self.found:
                if found == name:
                    await self.device.stop_discovery()
                    return hci.Address(address)
            await asyncio.sleep(0.01)

    async def connect(self, address: hci.Address) -> Connection:
        connection = await self.device.connect(address, transport=core.PhysicalTransport.BR_EDR)
        await connection.authenticate()
        await connection.encrypt()
        return connection

    async def open(self, connection: Connection, channel: int | None = None) -> rfcomm.DLC:
        """The SPP channel: on `channel`, or on the one SDP names."""
        if channel is None:
            channel = await rfcomm.find_rfcomm_channel_with_uuid(
                connection, core.BT_SERIAL_PORT_SERVICE
            )
        assert channel is not None
        multiplexer = await rfcomm.Client(connection).start()
        dlc = await multiplexer.open_dlc(channel)
        dlc.sink = lambda data: self.received.append((self.air.loop.now, data))
        return dlc

    async def session(self, name: str = "OBDII") -> rfcomm.DLC:
        return await self.open(await self.connect(await self.discover(name)))


@pytest.fixture
def air() -> Iterator[Air]:
    air = Air()
    yield air
    air.close()


def keys_of(device: Device) -> dict[str, bytes]:
    """The link keys `device` keeps, by the peer's address."""
    keystore = device.keystore
    assert isinstance(keystore, MemoryKeyStore)
    keys = {}
    for address, pairing in keystore.all_keys.items():
        assert pairing.link_key is not None
        keys[address] = pairing.link_key.value
    return keys


def test_the_device_finds_the_peer_by_its_name_and_its_answer_comes_on_the_boards_clock(
    air: Air,
) -> None:
    elm = Elm(delay=50 * MS)
    peer = SppPeer(air, name="OBDII", pin="1234", source=elm)
    esp32 = Initiator(air, pin="1234")

    async def scenario() -> None:
        dlc = await esp32.session()
        await asyncio.sleep(1)
        dlc.write(b"ATZ\r")
        await asyncio.sleep(1)

    air.run(scenario(), within=seconds(10))

    assert esp32.found == [(0, "00:00:00:00:5B:01/P", CLASS_OF_DEVICE, "OBDII")]
    assert esp32.delegate.asked == 1
    assert peer.channel_open
    # What the device wrote reached the model at the instant it was written, and the answer
    # came back at the time the model gave it.
    assert elm.heard == [(1000 * MS, b"ATZ\r")]
    assert esp32.received == [(1050 * MS, b"ATZ\r\r>")]
    assert peer.events == [(1000 * MS, "out", b"ATZ\r"), (1050 * MS, "in", b"ATZ\r\r>")]
    # Both sides keep the same key, under each other's address.
    assert keys_of(esp32.device) == {"00:00:00:00:5B:01/P": keys_of(peer.device)[DEVICE + "/P"]}


def test_a_wrong_pin_fails_the_authentication_and_detaches_the_link(air: Air) -> None:
    elm = Elm()
    peer = SppPeer(air, name="OBDII", pin="1234", source=elm)
    esp32 = Initiator(air, pin="0000")

    with pytest.raises(hci.HCI_Error) as raised:
        air.run(esp32.session(), within=seconds(10))
    assert raised.value.error_code == hci.HCI_ErrorCode.AUTHENTICATION_FAILURE_ERROR
    assert not esp32.device.connections
    assert not peer.device.connections
    assert keys_of(esp32.device) == {}
    assert keys_of(peer.device) == {}
    assert elm.heard == []


def test_a_device_that_paired_authenticates_with_its_key_without_a_pin(air: Air) -> None:
    peer = SppPeer(air, name="OBDII", pin="1234", source=Elm())
    esp32 = Initiator(air)

    async def twice() -> None:
        connection = await esp32.connect(await esp32.discover("OBDII"))
        await connection.disconnect()
        connection = await esp32.connect(peer.device.public_address)
        await esp32.open(connection)

    air.run(twice(), within=seconds(10))
    assert esp32.delegate.asked == 1
    assert peer.channel_open


def test_a_peer_that_lost_the_key_refuses_the_device_that_kept_it(air: Air) -> None:
    peer = SppPeer(air, name="OBDII", pin="1234", source=Elm())
    esp32 = Initiator(air)

    async def pair_and_disconnect() -> None:
        connection = await esp32.connect(await esp32.discover("OBDII"))
        await connection.disconnect()

    air.run(pair_and_disconnect(), within=seconds(10))
    peer.forget_keys()
    with pytest.raises(hci.HCI_Error) as raised:
        air.run(esp32.connect(peer.device.public_address), within=seconds(10))
    assert raised.value.error_code == hci.HCI_ErrorCode.PIN_OR_KEY_MISSING_ERROR


def test_an_inquiry_that_is_not_cancelled_completes_after_its_length(air: Air) -> None:
    SppPeer(air, name="OBDII", pin="1234", source=Elm())
    SppPeer(air, name="OBDII-2", pin="1234", source=Elm(), address="00:00:00:00:5B:00")
    esp32 = Initiator(air)

    async def inquiry() -> None:
        await esp32.device.start_discovery(auto_restart=False)
        await asyncio.sleep(20)

    air.run(inquiry(), within=seconds(30))
    # Every device that scans answers at once, in the order of their addresses; the stand-in
    # does not find itself.
    assert [(t, name) for t, _, _, name in esp32.found] == [(0, "OBDII-2"), (0, "OBDII")]
    # Bumble asks for 8 units of 1.28 s.
    assert esp32.inquiry_complete == [round(8 * INQUIRY_UNIT_S * 1e9)]


def test_what_the_source_sends_unprompted_goes_out_at_its_time_or_is_lost(air: Air) -> None:
    early = Elm(greeting=b"ELM327 v1.5\r", at=100 * MS)
    late = Elm(greeting=b"ELM327 v1.5\r", at=3000 * MS)
    lost = SppPeer(air, name="early", pin="1234", source=early, address="00:00:00:00:5B:02")
    kept = SppPeer(air, name="late", pin="1234", source=late, address="00:00:00:00:5B:03")
    esp32 = Initiator(air)

    async def connect_to_both() -> None:
        await asyncio.sleep(1)
        await esp32.session("early")
        await esp32.session("late")
        await asyncio.sleep(5)

    air.run(connect_to_both(), within=seconds(10))
    # The first greeting went out before any device held its channel.
    assert lost.lost == len(b"ELM327 v1.5\r")
    assert lost.events == []
    assert kept.lost == 0
    assert kept.events == [(3000 * MS, "in", b"ELM327 v1.5\r")]
    assert esp32.received == [(3000 * MS, b"ELM327 v1.5\r")]


class Faulty(Elm):
    """A source that gets one thing wrong, `how`."""

    def __init__(self, how: str) -> None:
        super().__init__()
        self.how = how

    def receive(self, t: int, data: bytes) -> Iterable[tuple[int, bytes]]:
        if self.how == "past":
            return [(t - 1, b"too early")]
        if self.how == "raises":
            raise KeyError("model bug")
        if self.how == "pair":
            return [(t, "text")]  # type: ignore[list-item]
        if self.how == "empty":
            return [(t, b""), (t, b"OK\r")]
        return super().receive(t, data)

    def quiet_until(self) -> int | None:
        if self.how == "float":
            return 1.5  # type: ignore[return-value]
        if self.how == "now":
            return 0
        return super().quiet_until()


def write_atz(air: Air, esp32: Initiator) -> None:
    async def write() -> None:
        dlc = await esp32.session()
        dlc.write(b"ATZ\r")
        await asyncio.sleep(1)

    air.run(write(), within=seconds(10))


@pytest.mark.parametrize(
    ("how", "message", "cause"),
    [
        (
            "past",
            "OBDII: source.receive sent b'too early' at -0.000000001 s, and the air is at 0 s:"
            " it cannot send into the past",
            None,
        ),
        ("raises", "OBDII: source.receive raised KeyError: 'model bug'", KeyError),
        ("pair", "OBDII: source.receive returned (0, 'text'), not a (t_ns, bytes) pair", None),
    ],
)
def test_a_source_that_gets_its_answer_wrong_fails_the_air_naming_the_peer(
    air: Air, how: str, message: str, cause: type[Exception] | None
) -> None:
    SppPeer(air, name="OBDII", pin="1234", source=Faulty(how))
    with pytest.raises(AirError) as raised:
        write_atz(air, Initiator(air))
    assert str(raised.value) == message
    error = raised.value.__cause__
    assert isinstance(error, PeerError)
    if cause is not None:
        assert isinstance(error.__cause__, cause)


@pytest.mark.parametrize(
    ("how", "message"),
    [
        ("float", "OBDII: source.quiet_until returned 1.5, not nanoseconds"),
        (
            "now",
            "OBDII: source.quiet_until returned 0 s, not after the air's 0 s: what it sends by"
            " then belongs in the poll at 0 s",
        ),
    ],
)
def test_a_source_that_gets_its_quiet_until_wrong_fails_the_air(
    air: Air, how: str, message: str
) -> None:
    SppPeer(air, name="OBDII", pin="1234", source=Faulty(how))
    with pytest.raises(AirError) as raised:
        air.wait(seconds(1))
    assert str(raised.value) == message
    assert isinstance(raised.value.__cause__, PeerError)


def test_empty_answers_cross_nothing(air: Air) -> None:
    peer = SppPeer(air, name="OBDII", pin="1234", source=Faulty("empty"))
    esp32 = Initiator(air)
    write_atz(air, esp32)
    assert peer.events == [(0, "out", b"ATZ\r"), (0, "in", b"OK\r")]
    assert esp32.received == [(0, b"OK\r")]


def test_two_runs_cross_the_channel_at_the_same_instants() -> None:
    def run() -> list[tuple[int, str, bytes]]:
        air = Air()
        try:
            peer = SppPeer(air, name="OBDII", pin="1234", source=Elm(delay=7 * MS))
            esp32 = Initiator(air)

            async def chat() -> None:
                dlc = await esp32.session()
                for command in (b"ATZ\r", b"ATE0\r", b"0100\r"):
                    dlc.write(command)
                    await asyncio.sleep(0.02)

            air.run(chat(), within=seconds(10))
            return list(peer.events)
        finally:
            air.close()

    first = run()
    assert len(first) == 6
    assert first == run()


def test_simple_pairing_still_works_between_two_devices_that_support_it(air: Air) -> None:
    """Bumble's own Secure Simple Pairing, which the controller leaves to Bumble when both hosts
    support it, followed by the encryption the controller adds."""
    esp32 = Initiator(air, ssp=True)
    phone = Initiator(air, pin="unused", ssp=True, address="00:00:00:00:5B:09", name="phone")
    air.run(phone.device.set_connectable(True), within=seconds(0))
    air.run(esp32.connect(phone.device.public_address), within=seconds(10))
    assert esp32.delegate.asked == 0
    assert phone.delegate.asked == 0
    assert keys_of(esp32.device)["00:00:00:00:5B:09/P"] == keys_of(phone.device)[DEVICE + "/P"]


def test_a_device_with_simple_pairing_pairs_with_the_peer_s_pin(air: Air) -> None:
    """ESP-IDF's default, `CONFIG_BT_SSP_ENABLED`, against a peer that has only a PIN."""
    elm = Elm()
    SppPeer(air, name="OBDII", pin="1234", source=elm)
    esp32 = Initiator(air, ssp=True)

    async def write() -> None:
        dlc = await esp32.session()
        dlc.write(b"ATZ\r")
        await asyncio.sleep(1)

    air.run(write(), within=seconds(10))
    assert esp32.delegate.asked == 1
    assert elm.heard == [(0, b"ATZ\r")]


def test_encryption_needs_an_authenticated_link(air: Air) -> None:
    peer = SppPeer(air, name="OBDII", pin="1234", source=Elm())
    esp32 = Initiator(air)

    async def encrypt_first() -> None:
        connection = await esp32.device.connect(
            peer.device.public_address, transport=core.PhysicalTransport.BR_EDR
        )
        await connection.encrypt()

    with pytest.raises(hci.HCI_Error) as raised:
        air.run(encrypt_first(), within=seconds(10))
    assert raised.value.error_code == hci.HCI_ErrorCode.INSUFFICIENT_SECURITY_ERROR


def test_nothing_happens_on_the_air_until_the_device_writes(air: Air) -> None:
    peer = SppPeer(air, name="OBDII", pin="1234", source=Elm())
    esp32 = Initiator(air)
    air.run(esp32.session(), within=seconds(10))
    assert peer.quiet_until() is None
    with pytest.raises(NeverHappened):
        air.run(asyncio.sleep(3600), within=ms(100), description="an hour's sleep")


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"name": ""}, "SppPeer: name is '', not a string of 1 to 238 bytes"),
        ({"name": "x" * 239}, "not a string of 1 to 238 bytes"),
        ({"pin": "12345678901234567"}, "pin is '12345678901234567', not a string of 1 to 16"),
        ({"pin": 1234}, "SppPeer: pin is 1234, not a string of 1 to 16 bytes"),
        ({"channel": 31}, "SppPeer: channel is 31, not an RFCOMM channel, 1 to 30"),
        ({"channel": True}, "SppPeer: channel is True, not an RFCOMM channel, 1 to 30"),
        (
            {"address": "nonsense"},
            "SppPeer: address is 'nonsense', not a Bluetooth address, as 00:00:00:00:5B:01",
        ),
        ({"address": "00:00:00:00:5B:0G"}, "not a Bluetooth address"),
    ],
)
def test_the_peer_refuses_what_it_cannot_be(
    air: Air, arguments: dict[str, object], message: str
) -> None:
    given: dict[str, object] = {"name": "OBDII", "pin": "1234", **arguments}
    with pytest.raises(PeerError, match=re.escape(message)):
        SppPeer(air, source=Elm(), **given)  # type: ignore[arg-type]


def test_two_devices_on_the_air_cannot_share_an_address(air: Air) -> None:
    SppPeer(air, name="A", pin="1234", source=Elm())
    with pytest.raises(PeerError) as raised:
        SppPeer(air, name="B", pin="1234", source=Elm())
    assert str(raised.value) == (
        "SppPeer: address 00:00:00:00:5B:01 is another device's on the air: give each its own"
    )


def test_the_longest_name_is_found_whole(air: Air) -> None:
    name = "N" * 238
    SppPeer(air, name=name, pin="1234", source=Elm())
    esp32 = Initiator(air)
    air.run(esp32.discover(name), within=seconds(1))
    assert esp32.found[0][3] == name


def test_the_channel_opens_to_one_device_at_a_time(air: Air) -> None:
    elm = Elm()
    peer = SppPeer(air, name="OBDII", pin="1234", source=elm)
    first = Initiator(air)
    second = Initiator(air, address="24:0A:C4:00:00:02", name="second")

    async def both() -> None:
        dlc = await first.session()
        with pytest.raises(core.ConnectionError):
            await second.session()
        dlc.write(b"ONE\r")
        await asyncio.sleep(1)

    air.run(both(), within=seconds(10))
    assert peer.refused == [(0, "24:0A:C4:00:00:02: the channel is held by 24:0A:C4:00:00:01")]
    assert elm.heard == [(0, b"ONE\r")]
    assert first.received == [(50 * MS, b"ONE\r\r>")]
    assert second.received == []


def test_two_devices_that_ask_for_the_channel_at_one_instant_get_it_one(air: Air) -> None:
    elm = Elm()
    peer = SppPeer(air, name="OBDII", pin="1234", source=elm)
    first = Initiator(air)
    second = Initiator(air, address="24:0A:C4:00:00:02", name="second")

    async def at_once() -> None:
        links = [await device.connect(peer.device.public_address) for device in (first, second)]
        # On the channel they know: Bumble 0.0.236's SDP server serves one search at a time.
        opened = await asyncio.gather(
            first.open(links[0], CHANNEL), second.open(links[1], CHANNEL), return_exceptions=True
        )
        assert isinstance(opened[0], rfcomm.DLC)
        assert isinstance(opened[1], core.ConnectionError)
        opened[0].write(b"ONE\r")
        await asyncio.sleep(1)

    air.run(at_once(), within=seconds(10))
    assert peer.refused == [(0, "24:0A:C4:00:00:02: the channel is held by 24:0A:C4:00:00:01")]
    assert first.received == [(50 * MS, b"ONE\r\r>")]
    assert second.received == []


def test_the_channel_opens_only_on_a_link_the_device_authenticated(air: Air) -> None:
    elm = Elm()
    peer = SppPeer(air, name="OBDII", pin="1234", source=elm)
    esp32 = Initiator(air)

    async def without_pairing() -> None:
        connection = await esp32.device.connect(
            peer.device.public_address, transport=core.PhysicalTransport.BR_EDR
        )
        await esp32.open(connection)

    with pytest.raises(core.ConnectionError):
        air.run(without_pairing(), within=seconds(10))
    assert peer.refused == [(0, "24:0A:C4:00:00:01: the link is not authenticated")]
    assert not peer.channel_open
    assert esp32.delegate.asked == 0


def test_an_authenticated_link_needs_no_encryption_for_the_channel(air: Air) -> None:
    elm = Elm()
    peer = SppPeer(air, name="OBDII", pin="1234", source=elm)
    esp32 = Initiator(air)

    async def authenticated_only() -> None:
        connection = await esp32.device.connect(
            peer.device.public_address, transport=core.PhysicalTransport.BR_EDR
        )
        await connection.authenticate()
        dlc = await esp32.open(connection)
        dlc.write(b"ATZ\r")
        await asyncio.sleep(1)

    air.run(authenticated_only(), within=seconds(10))
    assert peer.refused == []
    assert elm.heard == [(0, b"ATZ\r")]


def test_a_peer_that_is_not_secure_opens_the_channel_on_any_link(air: Air) -> None:
    elm = Elm()
    peer = SppPeer(air, name="OBDII", pin="1234", source=elm, secure=False)
    esp32 = Initiator(air)

    async def without_pairing() -> None:
        connection = await esp32.device.connect(
            peer.device.public_address, transport=core.PhysicalTransport.BR_EDR
        )
        dlc = await esp32.open(connection)
        dlc.write(b"ATZ\r")
        await asyncio.sleep(1)

    air.run(without_pairing(), within=seconds(10))
    assert esp32.delegate.asked == 0
    assert elm.heard == [(0, b"ATZ\r")]


@pytest.mark.parametrize("closes", ["channel", "link"])
def test_what_the_source_sends_after_the_channel_closes_is_lost(air: Air, closes: str) -> None:
    peer = SppPeer(air, name="OBDII", pin="1234", source=Elm(delay=500 * MS))
    esp32 = Initiator(air)

    async def write_and_leave() -> None:
        dlc = await esp32.session()
        dlc.write(b"ATZ\r")
        if closes == "channel":
            await dlc.disconnect()
        else:
            await dlc.multiplexer.l2cap_channel.connection.disconnect()
        await asyncio.sleep(1)

    air.run(write_and_leave(), within=seconds(10))
    assert not peer.channel_open
    assert peer.lost == len(b"ATZ\r\r>")
    assert peer.events == [(0, "out", b"ATZ\r")]


def test_a_device_that_gives_no_pin_fails_the_authentication(air: Air) -> None:
    peer = SppPeer(air, name="OBDII", pin="1234", source=Elm())
    esp32 = Initiator(air, pin=None)
    with pytest.raises(hci.HCI_Error) as raised:
        air.run(esp32.connect(peer.device.public_address), within=seconds(10))
    assert raised.value.error_code == hci.HCI_ErrorCode.PIN_OR_KEY_MISSING_ERROR
    assert keys_of(peer.device) == {}


def test_encryption_turned_off_is_off_on_both_sides(air: Air) -> None:
    peer = SppPeer(air, name="OBDII", pin="1234", source=Elm())
    esp32 = Initiator(air)

    async def on_and_off() -> Connection:
        connection = await esp32.connect(peer.device.public_address)
        await connection.encrypt(enable=False)
        return connection

    connection = air.run(on_and_off(), within=seconds(10))
    assert not connection.is_encrypted
    assert not any(c.is_encrypted for c in peer.device.connections.values())


def test_a_device_opens_the_channel_again_after_closing_it(air: Air) -> None:
    elm = Elm()
    peer = SppPeer(air, name="OBDII", pin="1234", source=elm)
    esp32 = Initiator(air)

    async def twice() -> None:
        dlc = await esp32.session()
        await dlc.disconnect()
        connection = dlc.multiplexer.l2cap_channel.connection
        await connection.disconnect()
        dlc = await esp32.open(await esp32.connect(peer.device.public_address))
        dlc.write(b"ATZ\r")
        await asyncio.sleep(1)

    air.run(twice(), within=seconds(10))
    assert peer.refused == []
    assert elm.heard == [(0, b"ATZ\r")]


def test_a_peer_with_something_due_at_once_leaves_the_air_well(air: Air) -> None:
    peer = SppPeer(air, name="OBDII", pin="1234", source=Elm(greeting=b"hi", at=0))
    # What was due when the peer joined went out then, to nobody.
    assert peer.lost == 2
    assert air.quiet_until() is None


# A peer that leaves: the same to the device whichever way it goes.

LEAVING = ["switch_off", "out_of_range"]
RETURNING = {"switch_off": "switch_on", "out_of_range": "in_range"}
PEER = hci.Address("00:00:00:00:5B:01/P")
Echo = Elm


def leave(peer: SppPeer, how: str) -> None:
    getattr(peer, how)()


def come_back(peer: SppPeer, how: str) -> None:
    getattr(peer, RETURNING[how])()


async def link_to(esp32: Initiator) -> Connection:
    """A link to the peer, not authenticated."""
    return await esp32.device.connect(PEER, transport=core.PhysicalTransport.BR_EDR)


async def write_timeout(esp32: Initiator, command: hci.HCI_SyncCommand[Any]) -> int:
    """The status the controller answers `command` with."""
    result = await esp32.device.host.send_sync_command_raw(command)
    parameters = result.return_parameters
    assert isinstance(parameters, hci.HCI_StatusReturnParameters)
    return parameters.status


@pytest.mark.parametrize("how", LEAVING)
def test_a_link_to_a_peer_that_left_falls_at_the_supervision_timeout_without_a_detach(
    air: Air, how: str
) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Echo())
    esp32 = Initiator(air)
    air.run(esp32.connect(PEER), within=seconds(10))
    air.advance(seconds(2))

    leave(peer, how)
    assert not peer.present
    air.advance(ms(21_999))
    # No LMP_detach: the device goes on holding the link for the whole of the timeout.
    assert esp32.fell == []
    assert esp32.device.connections

    air.advance(seconds(22))
    assert esp32.fell == [(22_000 * MS, hci.HCI_ErrorCode.CONNECTION_TIMEOUT_ERROR)]
    assert not esp32.device.connections


@pytest.mark.parametrize("how", LEAVING)
def test_a_page_to_a_peer_that_left_ends_at_the_page_timeout(air: Air, how: str) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Echo())
    esp32 = Initiator(air)
    air.advance(seconds(1))
    leave(peer, how)
    ended: list[int] = []

    async def page() -> None:
        try:
            await link_to(esp32)
        finally:
            ended.append(air.loop.now)

    with pytest.raises(core.ConnectionError) as raised:
        air.run(page(), within=seconds(10))
    assert raised.value.error_code == hci.HCI_ErrorCode.PAGE_TIMEOUT_ERROR
    assert ended == [6_120 * MS]
    assert not esp32.device.connections


def test_a_page_to_an_address_nobody_has_ends_at_the_page_timeout(air: Air) -> None:
    esp32 = Initiator(air)
    with pytest.raises(core.ConnectionError) as raised:
        air.run(link_to(esp32), within=seconds(10))
    assert raised.value.error_code == hci.HCI_ErrorCode.PAGE_TIMEOUT_ERROR
    assert air.now == ms(5_120)


def test_a_page_started_while_the_peer_is_away_does_not_find_it_when_it_returns(air: Air) -> None:
    """The page is one delivery, refused: the model has no page train that goes on repeating."""
    peer = SppPeer(air, name="peer", pin="1234", source=Echo())
    esp32 = Initiator(air)
    peer.out_of_range()
    task = air.start(link_to(esp32))
    air.advance(seconds(1))
    peer.in_range()
    air.advance(ms(5_120))
    assert isinstance(task.exception(), core.ConnectionError)
    # The next page finds it.
    air.run(link_to(esp32), within=seconds(1))


@pytest.mark.parametrize("how", LEAVING)
def test_an_inquiry_does_not_find_a_peer_that_left_until_it_returns(air: Air, how: str) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Echo())
    esp32 = Initiator(air)

    async def inquiry() -> None:
        await esp32.device.start_discovery(auto_restart=False)
        await asyncio.sleep(11)

    leave(peer, how)
    air.run(inquiry(), within=seconds(20))
    assert esp32.found == []
    assert esp32.inquiry_complete == [10_240 * MS]

    come_back(peer, how)
    assert peer.present
    air.run(inquiry(), within=seconds(20))
    assert [name for _, _, _, name in esp32.found] == ["peer"]


@pytest.mark.parametrize("how", LEAVING)
def test_a_device_authenticates_with_its_key_after_the_peer_returns(air: Air, how: str) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Echo())
    esp32 = Initiator(air)
    air.run(esp32.connect(PEER), within=seconds(10))
    kept = keys_of(peer.device)

    leave(peer, how)
    air.wait(seconds(21))
    assert keys_of(peer.device) == kept
    come_back(peer, how)
    assert keys_of(peer.device) == kept

    dlc = air.run(esp32.session("peer"), within=seconds(10))
    # The PIN was asked for once, by the first pairing.
    assert esp32.delegate.asked == 1
    assert isinstance(dlc, rfcomm.DLC)
    assert peer.channel_open


def test_a_peer_that_forgot_its_keys_refuses_after_it_returns(air: Air) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Echo())
    esp32 = Initiator(air)
    air.run(esp32.connect(PEER), within=seconds(10))
    peer.switch_off()
    peer.forget_keys()
    air.wait(seconds(21))
    peer.switch_on()
    with pytest.raises(hci.HCI_Error) as raised:
        air.run(esp32.connect(PEER), within=seconds(10))
    assert raised.value.error_code == hci.HCI_ErrorCode.PIN_OR_KEY_MISSING_ERROR


@pytest.mark.parametrize("how", LEAVING)
def test_coming_back_before_the_timeout_does_not_revive_the_link(air: Air, how: str) -> None:
    echo = Echo()
    peer = SppPeer(air, name="peer", pin="1234", source=echo)
    esp32 = Initiator(air)
    dlc = air.run(esp32.session("peer"), within=seconds(10))

    leave(peer, how)
    air.wait(seconds(5))
    come_back(peer, how)
    assert peer.present
    dlc.write(b"PING\r")
    air.wait(seconds(1))
    # The peer can be discovered again, and what the device writes on the old link is gone.
    assert echo.heard == []
    assert peer.events == []
    air.run(esp32.discover("peer"), within=seconds(1))

    # The device still holds the link, so it cannot page the peer yet.
    with pytest.raises(hci.HCI_Error) as raised:
        air.run(link_to(esp32), within=seconds(1))
    assert raised.value.error_code == hci.HCI_ErrorCode.CONNECTION_ALREADY_EXISTS_ERROR

    assert esp32.fell == []
    air.advance(seconds(20))
    assert esp32.fell == [(20_000 * MS, hci.HCI_ErrorCode.CONNECTION_TIMEOUT_ERROR)]

    # Fallen, the link can be made anew, with the key.
    dlc = air.run(esp32.session("peer"), within=seconds(10))
    dlc.write(b"PING\r")
    air.wait(seconds(1))
    assert echo.heard == [(20_000 * MS, b"PING\r")]
    assert esp32.delegate.asked == 1


def test_another_device_can_connect_to_a_peer_back_before_the_old_link_falls(air: Air) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Echo())
    first = Initiator(air)
    second = Initiator(air, address="24:0A:C4:00:00:02", name="second")
    air.run(first.session("peer"), within=seconds(10))

    peer.out_of_range()
    air.wait(seconds(5))
    peer.in_range()
    connection = air.run(second.connect(PEER), within=seconds(10))
    assert connection.is_encrypted
    # The peer's end of the old link has not fallen, so its channel is still that link's.
    with pytest.raises(core.ConnectionError):
        air.run(second.open(connection, CHANNEL), within=seconds(1))
    assert peer.refused == [(5_000 * MS, "24:0A:C4:00:00:02: the channel is held by " + DEVICE)]

    air.advance(seconds(20))
    assert not peer.channel_open
    dlc = air.run(second.open(connection, CHANNEL), within=seconds(1))
    assert isinstance(dlc, rfcomm.DLC)


def test_a_device_that_ends_its_link_to_an_absent_peer_pages_it_once_the_peer_let_go(
    air: Air,
) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Echo())
    esp32 = Initiator(air)
    connection = air.run(esp32.connect(PEER), within=seconds(10))
    peer.out_of_range()
    air.wait(seconds(1))
    air.run(connection.disconnect(), within=seconds(1))
    assert air.now == seconds(1)
    assert not esp32.device.connections
    peer.in_range()

    # The peer's end still holds the link: nobody answers the page.
    with pytest.raises(core.ConnectionError) as raised:
        air.run(link_to(esp32), within=seconds(10))
    assert raised.value.error_code == hci.HCI_ErrorCode.PAGE_TIMEOUT_ERROR

    air.advance(seconds(20))
    assert not peer.device.connections
    air.run(link_to(esp32), within=seconds(1))
    assert air.now == seconds(20)


def test_leaving_again_does_not_restart_the_timeout_of_a_link_that_lost_contact(air: Air) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Echo())
    esp32 = Initiator(air)
    air.run(link_to(esp32), within=seconds(1))
    peer.out_of_range()
    air.wait(seconds(5))
    peer.in_range()
    air.wait(seconds(5))
    peer.out_of_range()
    air.advance(seconds(30))
    assert esp32.fell == [(20_000 * MS, hci.HCI_ErrorCode.CONNECTION_TIMEOUT_ERROR)]


def test_an_authentication_under_way_ends_when_the_link_falls(air: Air) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Echo())
    esp32 = Initiator(air)
    connection = air.run(link_to(esp32), within=seconds(1))
    peer.out_of_range()
    with pytest.raises(hci.HCI_Error) as raised:
        air.run(connection.authenticate(), within=seconds(30))
    assert raised.value.error_code == hci.HCI_ErrorCode.CONNECTION_TIMEOUT_ERROR
    assert air.now == seconds(20)


# The timeouts a host writes.


def test_the_page_timeout_a_host_writes_takes_effect(air: Air) -> None:
    esp32 = Initiator(air)
    write = hci.HCI_Write_Page_Timeout_Command(page_timeout=0x0800)  # 1.28 s
    assert air.run(write_timeout(esp32, write), within=seconds(0)) == hci.HCI_SUCCESS
    with pytest.raises(core.ConnectionError):
        air.run(link_to(esp32), within=seconds(10))
    assert air.now == ms(1_280)


def test_a_page_timeout_of_zero_is_refused_and_changes_nothing(air: Air) -> None:
    esp32 = Initiator(air)
    write = hci.HCI_Write_Page_Timeout_Command(page_timeout=0)
    status = air.run(write_timeout(esp32, write), within=seconds(0))
    assert status == hci.HCI_ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR
    with pytest.raises(core.ConnectionError):
        air.run(link_to(esp32), within=seconds(10))
    assert air.now == ms(5_120)


def test_the_supervision_timeout_a_central_writes_takes_effect_on_both_ends(air: Air) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Echo())
    esp32 = Initiator(air)
    connection = air.run(link_to(esp32), within=seconds(1))
    write = hci.HCI_Write_Link_Supervision_Timeout_Command(
        handle=connection.handle,
        link_supervision_timeout=0x0C80,  # 2 s
    )
    assert air.run(write_timeout(esp32, write), within=seconds(0)) == hci.HCI_SUCCESS

    fell_at_peer: list[int] = []
    for link in peer.device.connections.values():
        link.on(link.EVENT_DISCONNECTION, lambda reason: fell_at_peer.append(air.loop.now))
    air.advance(seconds(1))
    peer.out_of_range()
    air.advance(seconds(10))
    assert esp32.fell == [(3_000 * MS, hci.HCI_ErrorCode.CONNECTION_TIMEOUT_ERROR)]
    assert fell_at_peer == [3_000 * MS]


def test_a_supervision_timeout_written_after_contact_is_lost_counts_from_that_instant(
    air: Air,
) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Echo())
    esp32 = Initiator(air)
    connection = air.run(link_to(esp32), within=seconds(1))
    peer.out_of_range()
    air.wait(seconds(1))
    write = hci.HCI_Write_Link_Supervision_Timeout_Command(
        handle=connection.handle,
        link_supervision_timeout=0x0C80,  # 2 s
    )
    air.run(write_timeout(esp32, write), within=seconds(0))
    air.wait(seconds(5))
    assert esp32.fell == [(2_000 * MS, hci.HCI_ErrorCode.CONNECTION_TIMEOUT_ERROR)]


def test_a_supervision_timeout_of_zero_means_the_link_never_falls(air: Air) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Echo())
    esp32 = Initiator(air)
    connection = air.run(link_to(esp32), within=seconds(1))
    write = hci.HCI_Write_Link_Supervision_Timeout_Command(
        handle=connection.handle, link_supervision_timeout=0
    )
    air.run(write_timeout(esp32, write), within=seconds(0))
    peer.out_of_range()
    air.wait(seconds(100))
    assert esp32.fell == []
    assert esp32.device.connections


def test_a_peripheral_may_not_write_the_supervision_timeout(air: Air) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Echo())
    esp32 = Initiator(air)
    air.run(link_to(esp32), within=seconds(1))
    handle = next(iter(peer.device.connections))
    write = hci.HCI_Write_Link_Supervision_Timeout_Command(
        handle=handle, link_supervision_timeout=0x0C80
    )

    async def status() -> int:
        result = await peer.device.host.send_sync_command_raw(write)
        parameters = result.return_parameters
        assert isinstance(parameters, hci.HCI_StatusReturnParameters)
        return parameters.status

    assert air.run(status(), within=seconds(0)) == hci.HCI_ErrorCode.COMMAND_DISALLOWED_ERROR


def test_a_supervision_timeout_for_no_link_is_refused(air: Air) -> None:
    esp32 = Initiator(air)
    write = hci.HCI_Write_Link_Supervision_Timeout_Command(
        handle=0x0123, link_supervision_timeout=0x0C80
    )
    status = air.run(write_timeout(esp32, write), within=seconds(0))
    assert status == hci.HCI_ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR


# What happens to the source.


class Powered(Echo):
    """A model on the peer's supply: silent without it, as a model of a part with no power is."""

    def __init__(self, delay: int = 50 * MS, greeting: bytes = b"", at: int = 0) -> None:
        super().__init__(delay, greeting, at)
        self.supplied: list[bool] = []

    def supply(self, on: bool) -> None:
        self.supplied.append(on)


def test_a_power_loss_tells_a_supplied_source_and_drops_what_it_had_queued(air: Air) -> None:
    source = Powered(delay=500 * MS)
    peer = SppPeer(air, name="peer", pin="1234", source=source)
    esp32 = Initiator(air)
    dlc = air.run(esp32.session("peer"), within=seconds(10))
    dlc.write(b"PING\r")
    air.wait(ms(100))

    peer.switch_off()
    assert source.supplied == [False]
    # Its end of the link went with the power.
    assert not peer.channel_open
    assert not peer.device.connections
    air.wait(seconds(1))
    assert peer.lost == 0
    assert peer.events == [(0, "out", b"PING\r")]
    assert esp32.received == []

    peer.switch_on()
    assert source.supplied == [False, True]


def test_out_of_range_does_not_tell_a_supplied_source_and_what_it_sends_is_lost(air: Air) -> None:
    source = Powered(delay=500 * MS)
    peer = SppPeer(air, name="peer", pin="1234", source=source)
    esp32 = Initiator(air)
    dlc = air.run(esp32.session("peer"), within=seconds(10))
    dlc.write(b"PING\r")
    air.wait(ms(100))

    peer.out_of_range()
    air.wait(seconds(1))
    assert source.supplied == []
    # The peer does not know yet: its channel stays open until its end of the link falls.
    assert peer.channel_open
    assert peer.lost == len(b"PING\r\r>")
    assert peer.events == [(0, "out", b"PING\r")]
    assert esp32.received == []

    air.advance(ms(20_100))
    assert not peer.channel_open


def test_what_a_source_sends_after_the_peer_returns_on_the_old_link_is_lost(air: Air) -> None:
    source = Echo(greeting=b"hello\r", at=2_000 * MS)
    peer = SppPeer(air, name="peer", pin="1234", source=source)
    esp32 = Initiator(air)
    air.run(esp32.session("peer"), within=seconds(10))
    peer.out_of_range()
    air.wait(seconds(1))
    peer.in_range()
    air.wait(seconds(2))
    assert peer.lost == len(b"hello\r")
    assert peer.events == []
    assert esp32.received == []


def test_a_power_loss_leaves_a_source_that_is_not_supplied_running_and_it_is_lost(
    air: Air,
) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Echo(greeting=b"hello\r", at=1_000 * MS))
    peer.switch_off()
    air.wait(seconds(2))
    assert peer.lost == len(b"hello\r")


def test_a_supplied_source_that_raises_fails_the_air_naming_the_peer(air: Air) -> None:
    class Broken(Echo):
        def supply(self, on: bool) -> None:
            raise KeyError("model bug")

    peer = SppPeer(air, name="peer", pin="1234", source=Broken())
    with pytest.raises(AirError) as raised:
        peer.switch_off()
    assert str(raised.value) == "peer: source.supply raised KeyError: 'model bug'"


# The two pairs of calls.


def test_a_peer_is_present_only_when_it_is_on_and_in_range(air: Air) -> None:
    source = Powered()
    peer = SppPeer(air, name="peer", pin="1234", source=source)
    assert peer.present
    peer.switch_off()
    peer.out_of_range()
    assert not peer.present
    peer.switch_on()
    # Back on, still out of range; the source has its power.
    assert not peer.present
    assert source.supplied == [False, True]
    peer.in_range()
    assert peer.present


def test_saying_it_twice_changes_nothing(air: Air) -> None:
    source = Powered()
    peer = SppPeer(air, name="peer", pin="1234", source=source)
    peer.switch_on()
    peer.in_range()
    assert source.supplied == []
    peer.switch_off()
    peer.switch_off()
    assert source.supplied == [False]
    peer.switch_on()
    peer.switch_on()
    assert source.supplied == [False, True]
    peer.out_of_range()
    peer.out_of_range()
    peer.in_range()
    assert peer.present


def test_a_peer_can_leave_from_a_coroutine_on_the_air(air: Air) -> None:
    echo = Echo()
    peer = SppPeer(air, name="peer", pin="1234", source=echo)
    esp32 = Initiator(air)

    async def scenario() -> None:
        dlc = await esp32.session("peer")
        await asyncio.sleep(1)
        peer.switch_off()
        # At the same instant, and on the same call: refused.
        dlc.write(b"PING\r")
        await asyncio.sleep(25)

    air.run(scenario(), within=seconds(30))
    assert echo.heard == []
    assert esp32.fell == [(21_000 * MS, hci.HCI_ErrorCode.CONNECTION_TIMEOUT_ERROR)]


def test_two_runs_lose_the_peer_at_the_same_instants() -> None:
    def run() -> list[object]:
        air = Air()
        try:
            source = Powered(delay=7 * MS, greeting=b"hello\r", at=1_500 * MS)
            peer = SppPeer(air, name="peer", pin="1234", source=source)
            esp32 = Initiator(air)
            other = Initiator(air, address="24:0A:C4:00:00:02", name="other")
            dlc = air.run(esp32.session("peer"), within=seconds(10))
            dlc.write(b"PING\r")
            air.wait(seconds(1))
            peer.out_of_range()
            air.wait(seconds(1))
            peer.in_range()
            air.run(other.connect(PEER), within=seconds(10))
            peer.switch_off()
            pages: list[int] = []

            async def page() -> None:
                with pytest.raises(core.ConnectionError):
                    await esp32.device.connect(
                        hci.Address("00:00:00:00:5B:7F/P"),
                        transport=core.PhysicalTransport.BR_EDR,
                    )
                pages.append(air.loop.now)

            air.advance(seconds(5))
            air.run(page(), within=seconds(10))
            air.advance(seconds(40))
            peer.switch_on()
            air.run(esp32.session("peer"), within=seconds(10))
            return [peer.events, peer.lost, esp32.fell, other.fell, pages, esp32.found]
        finally:
            air.close()

    first = run()
    # The first link fell 20 s after the peer went out of range; the second 20 s after the power
    # loss.
    assert first[2] == [(21_000 * MS, hci.HCI_ErrorCode.CONNECTION_TIMEOUT_ERROR)]
    assert first[3] == [(22_000 * MS, hci.HCI_ErrorCode.CONNECTION_TIMEOUT_ERROR)]
    assert first == run()


# Other devices.

PHONE = "00:00:00:00:5C:01"
SPEAKER = "00:00:00:00:5C:02"


async def one_inquiry(esp32: Initiator) -> None:
    """One inquiry, whole, in the extended mode: `start_discovery` writes it each time."""
    await esp32.device.start_discovery(auto_restart=False)
    await asyncio.sleep(11)


async def one_inquiry_in_mode(esp32: Initiator, mode: int) -> None:
    """One inquiry, whole, in `mode`, as a host that does not ask for the extended one."""
    esp32.device.auto_restart_inquiry = False
    await esp32.device.host.send_sync_command(hci.HCI_Write_Inquiry_Mode_Command(inquiry_mode=mode))
    await esp32.device.send_async_command(
        hci.HCI_Inquiry_Command(lap=hci.HCI_GENERAL_INQUIRY_LAP, inquiry_length=8, num_responses=0)
    )
    await asyncio.sleep(11)


def test_one_inquiry_finds_several_devices_each_with_its_own_rssi_and_class(air: Air) -> None:
    BluetoothDevice(air, name="phone", class_of_device=0x5A020C, rssi=-70, address=PHONE)
    BluetoothDevice(air, name="speaker", class_of_device=0x240404, rssi=-40, address=SPEAKER)
    SppPeer(air, name="peer", pin="1234", source=Echo(), rssi=-90)
    esp32 = Initiator(air)
    air.run(one_inquiry(esp32), within=seconds(20))

    # In the order of their addresses, at the instant the inquiry starts, then completes once.
    assert esp32.found == [
        (0, "00:00:00:00:5B:01/P", CLASS_OF_DEVICE, "peer"),
        (0, PHONE + "/P", 0x5A020C, "phone"),
        (0, SPEAKER + "/P", 0x240404, "speaker"),
    ]
    assert esp32.heard_at == [
        ("00:00:00:00:5B:01/P", -90),
        (PHONE + "/P", -70),
        (SPEAKER + "/P", -40),
    ]
    assert esp32.inquiry_complete == [10_240 * MS]


def test_a_device_answers_with_the_rssi_it_has_at_each_inquiry(air: Air) -> None:
    phone = BluetoothDevice(air, name="phone", rssi=-70, address=PHONE)
    peer = SppPeer(air, name="peer", pin="1234", source=Echo())
    assert peer.rssi == -50
    esp32 = Initiator(air)
    air.run(one_inquiry(esp32), within=seconds(20))
    phone.rssi = -80
    peer.rssi = -60
    assert phone.rssi == -80
    air.run(one_inquiry(esp32), within=seconds(20))
    assert esp32.heard_at == [
        ("00:00:00:00:5B:01/P", -50),
        (PHONE + "/P", -70),
        ("00:00:00:00:5B:01/P", -60),
        (PHONE + "/P", -80),
    ]


def test_the_rssi_is_given_in_the_mode_that_has_it_and_not_in_the_plain_one(air: Air) -> None:
    BluetoothDevice(air, name="phone", class_of_device=0x5A020C, rssi=-70, address=PHONE)
    esp32 = Initiator(air)
    air.run(one_inquiry_in_mode(esp32, hci.HCI_INQUIRY_WITH_RSSI_MODE), within=seconds(20))
    assert esp32.heard_at == [(PHONE + "/P", -70)]
    # This mode has no extended inquiry response, so no name: it was not the extended result.
    assert esp32.found == [(0, PHONE + "/P", 0x5A020C, None)]

    # Bumble's host has no handler for the plain event, so the test is the one: it reaches the
    # host as the plain event, with the class and no such field.
    plain: list[hci.HCI_Inquiry_Result_Event] = []
    esp32.device.host.on_hci_inquiry_result_event = plain.append  # type: ignore[attr-defined]
    air.run(one_inquiry_in_mode(esp32, hci.HCI_STANDARD_INQUIRY_MODE), within=seconds(20))
    assert [(str(e.bd_addr[0]), e.class_of_device[0]) for e in plain] == [(PHONE + "/P", 0x5A020C)]
    assert not hasattr(plain[0], "rssi")


def test_a_device_without_spp_says_so_in_sdp_and_has_no_channel(air: Air) -> None:
    BluetoothDevice(air, name="phone", address=PHONE)
    esp32 = Initiator(air)

    async def look_for_spp() -> None:
        connection = await esp32.device.connect(
            hci.Address(PHONE + "/P"), transport=core.PhysicalTransport.BR_EDR
        )
        channel = await rfcomm.find_rfcomm_channel_with_uuid(
            connection, core.BT_SERIAL_PORT_SERVICE
        )
        assert channel is None
        with pytest.raises(core.ProtocolError):
            await rfcomm.Client(connection).start()

    air.run(look_for_spp(), within=seconds(10))


def test_a_device_without_a_pin_fails_a_pairing(air: Air) -> None:
    phone = BluetoothDevice(air, name="phone", address=PHONE)
    esp32 = Initiator(air)
    with pytest.raises(hci.HCI_Error) as raised:
        air.run(esp32.connect(hci.Address(PHONE + "/P")), within=seconds(10))
    assert raised.value.error_code == hci.HCI_ErrorCode.PIN_OR_KEY_MISSING_ERROR
    assert keys_of(phone.device) == {}


def test_a_device_with_a_pin_pairs_and_keeps_its_key_across_a_return(air: Air) -> None:
    phone = BluetoothDevice(air, name="phone", pin="0000", address=PHONE)
    esp32 = Initiator(air, pin="0000")
    air.run(esp32.connect(hci.Address(PHONE + "/P")), within=seconds(10))
    phone.switch_off()
    air.wait(seconds(21))
    phone.switch_on()
    air.run(esp32.connect(hci.Address(PHONE + "/P")), within=seconds(10))
    assert esp32.delegate.asked == 1


def test_a_device_without_spp_leaves_as_a_peer_does(air: Air) -> None:
    phone = BluetoothDevice(air, name="phone", address=PHONE)
    esp32 = Initiator(air)
    address = hci.Address(PHONE + "/P")
    air.run(
        esp32.device.connect(address, transport=core.PhysicalTransport.BR_EDR), within=seconds(1)
    )
    phone.out_of_range()
    assert not phone.present
    air.advance(seconds(30))
    assert esp32.fell == [(20_000 * MS, hci.HCI_ErrorCode.CONNECTION_TIMEOUT_ERROR)]
    with pytest.raises(core.ConnectionError):
        air.run(
            esp32.device.connect(address, transport=core.PhysicalTransport.BR_EDR),
            within=seconds(10),
        )
    assert air.now == ms(35_120)


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"name": ""}, "BluetoothDevice: name is '', not a string of 1 to 238 bytes"),
        ({"pin": ""}, "BluetoothDevice: pin is '', not a string of 1 to 16 bytes"),
        ({"pin": 1234}, "BluetoothDevice: pin is 1234, not a string of 1 to 16 bytes"),
        (
            {"class_of_device": 0x1000000},
            "BluetoothDevice: class_of_device is 16777216, not three octets, 0 to 0xFFFFFF",
        ),
        (
            {"class_of_device": True},
            "BluetoothDevice: class_of_device is True, not three octets, 0 to 0xFFFFFF",
        ),
        ({"rssi": 21}, "BluetoothDevice: rssi is 21, not a signal strength in dBm, -127 to 20"),
        ({"rssi": -128}, "BluetoothDevice: rssi is -128, not a signal strength in dBm, -127 to 20"),
        ({"rssi": True}, "BluetoothDevice: rssi is True, not a signal strength in dBm, -127 to 20"),
        (
            {"rssi": -70.5},
            "BluetoothDevice: rssi is -70.5, not a signal strength in dBm, -127 to 20",
        ),
        (
            {"address": "nonsense"},
            "BluetoothDevice: address is 'nonsense', not a Bluetooth address, as 00:00:00:00:5B:01",
        ),
    ],
)
def test_a_device_refuses_what_it_cannot_be(
    air: Air, arguments: dict[str, object], message: str
) -> None:
    given: dict[str, object] = {"name": "phone", **arguments}
    with pytest.raises(PeerError) as raised:
        BluetoothDevice(air, **given)  # type: ignore[arg-type]
    assert str(raised.value) == message


def test_a_peer_needs_a_pin(air: Air) -> None:
    with pytest.raises(PeerError) as raised:
        SppPeer(air, name="peer", pin=None, source=Echo())  # type: ignore[arg-type]
    assert str(raised.value) == "SppPeer: pin is None, not a string of 1 to 16 bytes"


def test_setting_an_rssi_it_cannot_have_is_refused_and_changes_nothing(air: Air) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Echo(), rssi=-60)
    with pytest.raises(PeerError) as raised:
        peer.rssi = 100
    assert str(raised.value) == "SppPeer: rssi is 100, not a signal strength in dBm, -127 to 20"
    assert peer.rssi == -60


def test_a_second_device_at_an_address_on_the_air_is_refused_in_its_own_words(air: Air) -> None:
    SppPeer(air, name="peer", pin="1234", source=Echo())
    with pytest.raises(PeerError) as raised:
        BluetoothDevice(air, name="phone")
    assert str(raised.value) == (
        "BluetoothDevice: address 00:00:00:00:5B:01 is another device's on the air: give each its"
        " own"
    )
