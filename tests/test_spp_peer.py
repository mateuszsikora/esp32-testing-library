"""The SPP peer (`esp32tl/bluetooth/peer.py`) and the controller under it
(`esp32tl/bluetooth/controller.py`) on a Bumble virtual link, without the emulator.

A second Bumble device stands in for the ESP32 and does what ESP-IDF's `bt_spp_initiator` does:
an inquiry until it finds the peer's name, a connection, authentication with a PIN, encryption,
an SDP search for the Serial Port service, and an RFCOMM channel to it. A model in the test
answers what it writes, on the board's clock."""

import asyncio
import re
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Iterator
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
from esp32tl.units import Duration, ms, seconds

MS = 1_000_000
DEVICE = "24:0A:C4:00:00:01"


class Echo:
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
        return [(t + self.delay, data.rstrip(b"\r") + b"\r\n> ")]

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

    async def session(self, name: str = "peer") -> rfcomm.DLC:
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
    echo = Echo(delay=50 * MS)
    peer = SppPeer(air, name="peer", pin="1234", source=echo)
    esp32 = Initiator(air, pin="1234")

    async def scenario() -> None:
        dlc = await esp32.session()
        await asyncio.sleep(1)
        dlc.write(b"hello\r")
        await asyncio.sleep(1)

    air.run(scenario(), within=seconds(10))

    assert esp32.found == [(0, "00:00:00:00:5B:01/P", CLASS_OF_DEVICE, "peer")]
    assert esp32.delegate.asked == 1
    assert peer.channel_open
    # What the device wrote reached the model at the instant it was written, and the answer
    # came back at the time the model gave it.
    assert echo.heard == [(1000 * MS, b"hello\r")]
    assert esp32.received == [(1050 * MS, b"hello\r\n> ")]
    assert peer.events == [(1000 * MS, "out", b"hello\r"), (1050 * MS, "in", b"hello\r\n> ")]
    # Both sides keep the same key, under each other's address.
    assert keys_of(esp32.device) == {"00:00:00:00:5B:01/P": keys_of(peer.device)[DEVICE + "/P"]}


def test_a_wrong_pin_fails_the_authentication_and_detaches_the_link(air: Air) -> None:
    echo = Echo()
    peer = SppPeer(air, name="peer", pin="1234", source=echo)
    esp32 = Initiator(air, pin="0000")

    with pytest.raises(hci.HCI_Error) as raised:
        air.run(esp32.session(), within=seconds(10))
    assert raised.value.error_code == hci.HCI_ErrorCode.AUTHENTICATION_FAILURE_ERROR
    assert not esp32.device.connections
    assert not peer.device.connections
    assert keys_of(esp32.device) == {}
    assert keys_of(peer.device) == {}
    assert echo.heard == []


def test_a_device_that_paired_authenticates_with_its_key_without_a_pin(air: Air) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Echo())
    esp32 = Initiator(air)

    async def twice() -> None:
        connection = await esp32.connect(await esp32.discover("peer"))
        await connection.disconnect()
        connection = await esp32.connect(peer.device.public_address)
        await esp32.open(connection)

    air.run(twice(), within=seconds(10))
    assert esp32.delegate.asked == 1
    assert peer.channel_open


def test_a_peer_that_lost_the_key_refuses_the_device_that_kept_it(air: Air) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Echo())
    esp32 = Initiator(air)

    async def pair_and_disconnect() -> None:
        connection = await esp32.connect(await esp32.discover("peer"))
        await connection.disconnect()

    air.run(pair_and_disconnect(), within=seconds(10))
    peer.forget_keys()
    with pytest.raises(hci.HCI_Error) as raised:
        air.run(esp32.connect(peer.device.public_address), within=seconds(10))
    assert raised.value.error_code == hci.HCI_ErrorCode.PIN_OR_KEY_MISSING_ERROR


def test_an_inquiry_that_is_not_cancelled_completes_after_its_length(air: Air) -> None:
    SppPeer(air, name="peer", pin="1234", source=Echo())
    SppPeer(air, name="peer-2", pin="1234", source=Echo(), address="00:00:00:00:5B:00")
    esp32 = Initiator(air)

    async def inquiry() -> None:
        await esp32.device.start_discovery(auto_restart=False)
        await asyncio.sleep(20)

    air.run(inquiry(), within=seconds(30))
    # Every device that scans answers at once, in the order of their addresses; the stand-in
    # does not find itself.
    assert [(t, name) for t, _, _, name in esp32.found] == [(0, "peer-2"), (0, "peer")]
    # Bumble asks for 8 units of 1.28 s.
    assert esp32.inquiry_complete == [round(8 * INQUIRY_UNIT_S * 1e9)]


def test_what_the_source_sends_unprompted_goes_out_at_its_time_or_is_lost(air: Air) -> None:
    early = Echo(greeting=b"ready\r", at=100 * MS)
    late = Echo(greeting=b"ready\r", at=3000 * MS)
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
    assert lost.lost == len(b"ready\r")
    assert lost.events == []
    assert kept.lost == 0
    assert kept.events == [(3000 * MS, "in", b"ready\r")]
    assert esp32.received == [(3000 * MS, b"ready\r")]


class Faulty(Echo):
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


def write_hello(air: Air, esp32: Initiator) -> None:
    async def write() -> None:
        dlc = await esp32.session()
        dlc.write(b"hello\r")
        await asyncio.sleep(1)

    air.run(write(), within=seconds(10))


@pytest.mark.parametrize(
    ("how", "message", "cause"),
    [
        (
            "past",
            "peer: source.receive sent b'too early' at -0.000000001 s, and the air is at 0 s:"
            " it cannot send into the past",
            None,
        ),
        ("raises", "peer: source.receive raised KeyError: 'model bug'", KeyError),
        ("pair", "peer: source.receive returned (0, 'text'), not a (t_ns, bytes) pair", None),
    ],
)
def test_a_source_that_gets_its_answer_wrong_fails_the_air_naming_the_peer(
    air: Air, how: str, message: str, cause: type[Exception] | None
) -> None:
    SppPeer(air, name="peer", pin="1234", source=Faulty(how))
    with pytest.raises(AirError) as raised:
        write_hello(air, Initiator(air))
    assert str(raised.value) == message
    error = raised.value.__cause__
    assert isinstance(error, PeerError)
    if cause is not None:
        assert isinstance(error.__cause__, cause)


@pytest.mark.parametrize(
    ("how", "message"),
    [
        ("float", "peer: source.quiet_until returned 1.5, not nanoseconds"),
        (
            "now",
            "peer: source.quiet_until returned 0 s, not after the air's 0 s: what it sends by"
            " then belongs in the poll at 0 s",
        ),
    ],
)
def test_a_source_that_gets_its_quiet_until_wrong_fails_the_air(
    air: Air, how: str, message: str
) -> None:
    SppPeer(air, name="peer", pin="1234", source=Faulty(how))
    with pytest.raises(AirError) as raised:
        air.wait(seconds(1))
    assert str(raised.value) == message
    assert isinstance(raised.value.__cause__, PeerError)


def test_empty_answers_cross_nothing(air: Air) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Faulty("empty"))
    esp32 = Initiator(air)
    write_hello(air, esp32)
    assert peer.events == [(0, "out", b"hello\r"), (0, "in", b"OK\r")]
    assert esp32.received == [(0, b"OK\r")]


def test_two_runs_cross_the_channel_at_the_same_instants() -> None:
    def run() -> list[tuple[int, str, bytes]]:
        air = Air()
        try:
            peer = SppPeer(air, name="peer", pin="1234", source=Echo(delay=7 * MS))
            esp32 = Initiator(air)

            async def chat() -> None:
                dlc = await esp32.session()
                for command in (b"one\r", b"two\r", b"three\r"):
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
    echo = Echo()
    SppPeer(air, name="peer", pin="1234", source=echo)
    esp32 = Initiator(air, ssp=True)

    async def write() -> None:
        dlc = await esp32.session()
        dlc.write(b"hello\r")
        await asyncio.sleep(1)

    air.run(write(), within=seconds(10))
    assert esp32.delegate.asked == 1
    assert echo.heard == [(0, b"hello\r")]


def test_encryption_needs_an_authenticated_link(air: Air) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Echo())
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
    peer = SppPeer(air, name="peer", pin="1234", source=Echo())
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
    given: dict[str, object] = {"name": "peer", "pin": "1234", **arguments}
    with pytest.raises(PeerError, match=re.escape(message)):
        SppPeer(air, source=Echo(), **given)  # type: ignore[arg-type]


def test_two_devices_on_the_air_cannot_share_an_address(air: Air) -> None:
    SppPeer(air, name="A", pin="1234", source=Echo())
    with pytest.raises(PeerError) as raised:
        SppPeer(air, name="B", pin="1234", source=Echo())
    assert str(raised.value) == (
        "SppPeer: address 00:00:00:00:5B:01 is another device's on the air: give each its own"
    )


def test_the_longest_name_is_found_whole(air: Air) -> None:
    name = "N" * 238
    SppPeer(air, name=name, pin="1234", source=Echo())
    esp32 = Initiator(air)
    air.run(esp32.discover(name), within=seconds(1))
    assert esp32.found[0][3] == name


def test_the_channel_opens_to_one_device_at_a_time(air: Air) -> None:
    echo = Echo()
    peer = SppPeer(air, name="peer", pin="1234", source=echo)
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
    assert echo.heard == [(0, b"ONE\r")]
    assert first.received == [(50 * MS, b"ONE\r\n> ")]
    assert second.received == []


def test_two_devices_that_ask_for_the_channel_at_one_instant_get_it_one(air: Air) -> None:
    echo = Echo()
    peer = SppPeer(air, name="peer", pin="1234", source=echo)
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
    assert first.received == [(50 * MS, b"ONE\r\n> ")]
    assert second.received == []


def test_the_channel_opens_only_on_a_link_the_device_authenticated(air: Air) -> None:
    echo = Echo()
    peer = SppPeer(air, name="peer", pin="1234", source=echo)
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
    echo = Echo()
    peer = SppPeer(air, name="peer", pin="1234", source=echo)
    esp32 = Initiator(air)

    async def authenticated_only() -> None:
        connection = await esp32.device.connect(
            peer.device.public_address, transport=core.PhysicalTransport.BR_EDR
        )
        await connection.authenticate()
        dlc = await esp32.open(connection)
        dlc.write(b"hello\r")
        await asyncio.sleep(1)

    air.run(authenticated_only(), within=seconds(10))
    assert peer.refused == []
    assert echo.heard == [(0, b"hello\r")]


def test_a_peer_that_is_not_secure_opens_the_channel_on_any_link(air: Air) -> None:
    echo = Echo()
    peer = SppPeer(air, name="peer", pin="1234", source=echo, secure=False)
    esp32 = Initiator(air)

    async def without_pairing() -> None:
        connection = await esp32.device.connect(
            peer.device.public_address, transport=core.PhysicalTransport.BR_EDR
        )
        dlc = await esp32.open(connection)
        dlc.write(b"hello\r")
        await asyncio.sleep(1)

    air.run(without_pairing(), within=seconds(10))
    assert esp32.delegate.asked == 0
    assert echo.heard == [(0, b"hello\r")]


@pytest.mark.parametrize("closes", ["channel", "link"])
def test_what_the_source_sends_after_the_channel_closes_is_lost(air: Air, closes: str) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Echo(delay=500 * MS))
    esp32 = Initiator(air)

    async def write_and_leave() -> None:
        dlc = await esp32.session()
        dlc.write(b"hello\r")
        if closes == "channel":
            await dlc.disconnect()
        else:
            await dlc.multiplexer.l2cap_channel.connection.disconnect()
        await asyncio.sleep(1)

    air.run(write_and_leave(), within=seconds(10))
    assert not peer.channel_open
    assert peer.lost == len(b"hello\r\n> ")
    assert peer.events == [(0, "out", b"hello\r")]


def test_a_device_that_gives_no_pin_fails_the_authentication(air: Air) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Echo())
    esp32 = Initiator(air, pin=None)
    with pytest.raises(hci.HCI_Error) as raised:
        air.run(esp32.connect(peer.device.public_address), within=seconds(10))
    assert raised.value.error_code == hci.HCI_ErrorCode.PIN_OR_KEY_MISSING_ERROR
    assert keys_of(peer.device) == {}


def test_encryption_turned_off_is_off_on_both_sides(air: Air) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Echo())
    esp32 = Initiator(air)

    async def on_and_off() -> Connection:
        connection = await esp32.connect(peer.device.public_address)
        await connection.encrypt(enable=False)
        return connection

    connection = air.run(on_and_off(), within=seconds(10))
    assert not connection.is_encrypted
    assert not any(c.is_encrypted for c in peer.device.connections.values())


def test_a_device_opens_the_channel_again_after_closing_it(air: Air) -> None:
    echo = Echo()
    peer = SppPeer(air, name="peer", pin="1234", source=echo)
    esp32 = Initiator(air)

    async def twice() -> None:
        dlc = await esp32.session()
        await dlc.disconnect()
        connection = dlc.multiplexer.l2cap_channel.connection
        await connection.disconnect()
        dlc = await esp32.open(await esp32.connect(peer.device.public_address))
        dlc.write(b"hello\r")
        await asyncio.sleep(1)

    air.run(twice(), within=seconds(10))
    assert peer.refused == []
    assert echo.heard == [(0, b"hello\r")]


def test_a_peer_with_something_due_at_once_leaves_the_air_well(air: Air) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Echo(greeting=b"hi", at=0))
    # What was due when the peer joined went out then, to nobody.
    assert peer.lost == 2
    assert air.quiet_until() is None


# A peer that leaves: the same to the device whichever way it goes.

LEAVING = ["switch_off", "out_of_range"]
RETURNING = {"switch_off": "switch_on", "out_of_range": "in_range"}
PEER = hci.Address("00:00:00:00:5B:01/P")


def leave(device: BluetoothDevice, how: str) -> None:
    getattr(device, how)()


def come_back(device: BluetoothDevice, how: str) -> None:
    getattr(device, RETURNING[how])()


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
    assert peer.lost == len(b"PING\r\n> ")
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


# A device that leaves with something of its own under way.


def phone_and_speaker(air: Air) -> tuple[BluetoothDevice, BluetoothDevice]:
    phone = BluetoothDevice(air, name="phone", address=PHONE)
    speaker = BluetoothDevice(air, name="speaker", address=SPEAKER)
    return phone, speaker


def page(device: BluetoothDevice, address: str) -> Coroutine[Any, Any, Connection]:
    return device.device.connect(
        hci.Address(address + "/P"), transport=core.PhysicalTransport.BR_EDR
    )


def test_a_device_that_leaves_during_its_own_page_can_be_reached_again(air: Air) -> None:
    phone, speaker = phone_and_speaker(air)
    speaker.out_of_range()
    task = air.start(page(phone, SPEAKER))
    air.wait(seconds(1))
    phone.out_of_range()
    air.advance(seconds(6))
    assert isinstance(task.exception(), core.ConnectionError)
    phone.in_range()
    speaker.in_range()
    assert phone.present and speaker.present
    # Once both are back and neither holds a link, a page connects.
    air.run(page(phone, SPEAKER), within=seconds(1))
    assert air.now == seconds(6)


def test_a_device_that_left_during_its_own_page_can_be_paged_once_both_are_back(air: Air) -> None:
    phone, speaker = phone_and_speaker(air)
    speaker.out_of_range()
    task = air.start(page(phone, SPEAKER))
    air.wait(seconds(1))
    phone.out_of_range()
    phone.in_range()
    speaker.in_range()
    # A page under way is no link: nothing was severed by leaving during it.
    air.run(page(speaker, PHONE), within=seconds(1))
    assert air.now == seconds(1)
    assert task.done()


def test_a_page_that_outlasts_the_link_falling_at_the_other_end_lets_the_pair_go(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The target leaves at the instant it hears the page, before its host answers. Its end falls
    at 20 s, while the page goes on until 40.96 s; only then does neither hold anything."""
    phone, speaker = phone_and_speaker(air)
    target = air.link.find_classic_controller(hci.Address(SPEAKER + "/P"))
    assert target is not None
    heard = target.on_classic_connection_request

    def hear_and_leave(peer: hci.Address, link_type: int) -> None:
        heard(peer, link_type)
        speaker.out_of_range()

    with monkeypatch.context() as patch:
        patch.setattr(target, "on_classic_connection_request", hear_and_leave)
        write = hci.HCI_Write_Page_Timeout_Command(page_timeout=0xFFFF)
        air.run(phone.device.host.send_sync_command(write), within=seconds(0))
        with pytest.raises(core.ConnectionError):
            air.run(page(phone, SPEAKER), within=seconds(60))
    assert air.now == Duration(40_959_375_000)
    speaker.in_range()
    air.run(page(phone, SPEAKER), within=seconds(1))


def test_a_power_loss_ends_a_page_and_its_timer_does_not_end_the_next(air: Air) -> None:
    phone, speaker = phone_and_speaker(air)
    speaker.out_of_range()
    first = air.start(page(phone, SPEAKER))
    air.wait(seconds(4))
    phone.switch_off()
    # The page is over with the power, and its host hears of it once.
    error = first.exception()
    assert isinstance(error, core.ConnectionError)
    assert error.error_code == hci.HCI_ErrorCode.PAGE_TIMEOUT_ERROR
    assert not phone.device.connections
    phone.switch_on()
    ended: list[int] = []

    async def second() -> None:
        with pytest.raises(core.ConnectionError):
            await page(phone, SPEAKER)
        ended.append(air.loop.now)

    air.run(second(), within=seconds(10))
    assert ended == [9_120 * MS]


def test_a_power_loss_ends_an_inquiry(air: Air) -> None:
    phone, _ = phone_and_speaker(air)
    completed: list[int] = []
    phone.device.on("inquiry_complete", lambda: completed.append(air.loop.now))
    air.run(phone.device.start_discovery(auto_restart=False), within=seconds(0))
    air.wait(seconds(1))
    phone.switch_off()
    assert completed == [1_000 * MS]
    phone.switch_on()
    # Nothing is left of it: another can start, and completes after its own length.
    air.run(phone.device.start_discovery(auto_restart=False), within=seconds(0))
    air.wait(seconds(20))
    assert completed == [1_000 * MS, 11_240 * MS]


@pytest.mark.parametrize("how", LEAVING)
def test_a_device_that_left_finds_nothing_in_its_own_inquiry(air: Air, how: str) -> None:
    phone, _ = phone_and_speaker(air)
    found: list[str] = []
    phone.device.on("inquiry_result", lambda address, *_: found.append(str(address)))
    completed: list[int] = []
    phone.device.on("inquiry_complete", lambda: completed.append(air.loop.now))
    leave(phone, how)
    air.run(phone.device.start_discovery(auto_restart=False), within=seconds(0))
    air.wait(seconds(11))
    assert found == []
    assert completed == [10_240 * MS]


@pytest.mark.parametrize("how", LEAVING)
def test_a_remote_name_request_to_a_peer_that_left_ends_at_the_page_timeout(
    air: Air, how: str
) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Echo())
    esp32 = Initiator(air)
    air.advance(seconds(1))
    leave(peer, how)
    with pytest.raises(hci.HCI_Error) as raised:
        air.run(esp32.device.request_remote_name(PEER), within=seconds(60))
    assert raised.value.error_code == hci.HCI_ErrorCode.PAGE_TIMEOUT_ERROR
    assert air.now == ms(6_120)


def test_a_second_remote_name_request_ends_when_the_first_does(air: Air) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Echo())
    esp32 = Initiator(air)
    peer.out_of_range()
    first = air.start(esp32.device.request_remote_name(PEER))
    air.advance(seconds(4))
    second = air.start(esp32.device.request_remote_name(PEER))
    air.advance(ms(5_119))
    assert not first.done()
    assert not second.done()
    air.advance(ms(5_120))
    assert isinstance(first.exception(), hci.HCI_Error)
    assert isinstance(second.exception(), hci.HCI_Error)
    assert air.quiet_until() is None


def test_a_remote_name_request_is_answered_at_once_when_the_peer_is_there(air: Air) -> None:
    SppPeer(air, name="peer", pin="1234", source=Echo())
    esp32 = Initiator(air)
    assert air.run(esp32.device.request_remote_name(PEER), within=seconds(60)) == "peer"
    # The answer cancelled the timeout: nothing is due.
    assert air.quiet_until() is None


def test_a_power_loss_ends_a_remote_name_request_and_its_timer_does_not_end_the_next(
    air: Air,
) -> None:
    phone, speaker = phone_and_speaker(air)
    speaker.out_of_range()
    first = air.start(phone.device.request_remote_name(hci.Address(SPEAKER + "/P")))
    air.wait(seconds(4))
    phone.switch_off()
    assert isinstance(first.exception(), hci.HCI_Error)
    phone.switch_on()
    with pytest.raises(hci.HCI_Error):
        air.run(phone.device.request_remote_name(hci.Address(SPEAKER + "/P")), within=seconds(10))
    assert air.now == ms(9_120)


# A target whose host takes its time to answer a page.


ANSWERS = (hci.HCI_Accept_Connection_Request_Command, hci.HCI_Reject_Connection_Request_Command)


def changing_answers(
    target: BluetoothDevice,
    patch: pytest.MonkeyPatch,
    change: Callable[[hci.HCI_AsyncCommand], Awaitable[hci.HCI_AsyncCommand | None]],
) -> None:
    """The host of `target` sends what `change` makes of each answer to a page it hears, nothing
    for `None`. Its other commands go on meanwhile."""
    host = target.device.host
    send = host.send_async_command

    async def answer(command: hci.HCI_AsyncCommand, *arguments: Any) -> Any:
        if isinstance(command, ANSWERS):
            changed = await change(command)
            if changed is None:
                return None
            command = changed
        return await send(command, *arguments)

    patch.setattr(host, "send_async_command", answer)


def slow_to_accept(target: BluetoothDevice, patch: pytest.MonkeyPatch, *delays: float) -> None:
    """The host of `target` answers each page it hears after the next of `delays` seconds, as
    firmware may, and 0.1 s after those run out."""
    left = list(delays)

    async def later(command: hci.HCI_AsyncCommand) -> hci.HCI_AsyncCommand:
        await asyncio.sleep(left.pop(0) if left else 0.1)
        return command

    changing_answers(target, patch, later)


def falls_of(air: Air, device: BluetoothDevice) -> list[tuple[int, int]]:
    """The links of `device` that fall from now on, as (t, reason)."""
    fell: list[tuple[int, int]] = []

    def connected(connection: Connection) -> None:
        def down(reason: int) -> None:
            fell.append((air.loop.now, reason))

        connection.on(connection.EVENT_DISCONNECTION, down)

    device.device.on(device.device.EVENT_CONNECTION, connected)
    return fell


@pytest.mark.parametrize("how", LEAVING)
def test_a_target_that_heard_the_page_loses_contact_when_the_pager_leaves(
    air: Air, how: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    slow_to_accept(speaker, monkeypatch)
    fell = falls_of(air, speaker)
    task = air.start(page(phone, SPEAKER))
    air.advance(ms(50))
    leave(phone, how)
    air.advance(ms(120_000))
    assert task.done()
    # Accepted at 0.1 s, with nobody left: its end falls 20 s after the pager left.
    assert fell == [(20_050 * MS, hci.HCI_ErrorCode.CONNECTION_TIMEOUT_ERROR)]
    assert not speaker.device.connections
    come_back(phone, how)
    # It holds nothing, so it can page the one that returned.
    monkeypatch.undo()
    air.run(page(speaker, PHONE), within=seconds(1))


def test_a_pager_whose_power_cycles_before_the_host_answers_gets_no_link(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    slow_to_accept(speaker, monkeypatch)
    fell = falls_of(air, speaker)
    task = air.start(page(phone, SPEAKER))
    air.advance(ms(50))
    phone.switch_off()
    phone.switch_on()
    assert isinstance(task.exception(), core.ConnectionError)
    air.advance(ms(200))
    # The target's host accepted a page that was over: the pager has no link, and the target's end
    # falls by itself.
    assert not phone.device.connections
    air.advance(seconds(21))
    assert fell == [(20_050 * MS, hci.HCI_ErrorCode.CONNECTION_TIMEOUT_ERROR)]
    monkeypatch.undo()
    air.run(page(phone, SPEAKER), within=seconds(1))


def test_a_power_loss_ends_an_authentication_with_a_status(air: Air) -> None:
    phone = BluetoothDevice(air, name="phone", pin="0000", address=PHONE)
    speaker = BluetoothDevice(air, name="speaker", pin="0000", address=SPEAKER)
    connection = air.run(page(phone, SPEAKER), within=seconds(1))
    speaker.out_of_range()
    task = air.start(connection.authenticate())
    air.wait(seconds(1))
    phone.switch_off()
    error = task.exception()
    assert isinstance(error, hci.HCI_Error)
    assert error.error_code == hci.HCI_ErrorCode.CONNECTION_TIMEOUT_ERROR


@pytest.mark.parametrize("how", LEAVING)
def test_a_target_that_leaves_before_its_host_answers_tells_its_host_of_no_link(
    air: Air, how: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    # Later than the supervision timeout, so that the end goes before the host answers either way.
    slow_to_accept(speaker, monkeypatch, 30)
    told: list[int] = []
    host = speaker.device.host
    heard = host.on_hci_disconnection_complete_event

    def disconnection(event: hci.HCI_Disconnection_Complete_Event) -> None:
        told.append(event.connection_handle)
        heard(event)

    monkeypatch.setattr(host, "on_hci_disconnection_complete_event", disconnection)
    task = air.start(page(phone, SPEAKER))
    air.advance(ms(50))
    leave(speaker, how)
    air.advance(seconds(31))
    assert task.done()
    assert told == []
    assert not speaker.device.connections


def failures_of(air: Air, device: BluetoothDevice) -> list[tuple[int, int]]:
    """The connections `device` fails to make from now on, as (t, status)."""
    failed: list[tuple[int, int]] = []

    def failure(error: core.ConnectionError) -> None:
        assert error.error_code is not None
        failed.append((air.loop.now, error.error_code))

    device.device.on(device.device.EVENT_CONNECTION_FAILURE, failure)
    return failed


def test_a_host_that_answers_a_page_after_its_end_fell_gets_no_link(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    slow_to_accept(speaker, monkeypatch, 30)
    failed = failures_of(air, speaker)
    task = air.start(page(phone, SPEAKER))
    air.advance(ms(50))
    speaker.out_of_range()
    air.advance(seconds(31))
    assert task.done()
    assert failed == [(30_000 * MS, hci.HCI_ErrorCode.CONNECTION_TIMEOUT_ERROR)]
    assert not speaker.device.connections
    speaker.in_range()
    # Nothing is left of it on either side.
    monkeypatch.undo()
    air.run(page(phone, SPEAKER), within=seconds(1))


def test_a_late_answer_does_not_connect_the_next_page(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    slow_to_accept(speaker, monkeypatch, 6, 1)
    failed = failures_of(air, speaker)
    first = air.start(page(phone, SPEAKER))
    air.advance(ms(50))
    speaker.switch_off()
    speaker.switch_on()
    air.advance(ms(5_500))
    assert isinstance(first.exception(), core.ConnectionError)

    second = air.start(page(phone, SPEAKER))
    air.advance(ms(6_100))
    # The first answer, at 6 s, failed; the second page is still waiting for its own.
    assert failed == [(6_000 * MS, hci.HCI_ErrorCode.CONNECTION_TIMEOUT_ERROR)]
    assert not second.done()
    air.advance(ms(6_500))
    assert second.done()
    assert second.exception() is None
    assert phone.device.connections


def test_an_answer_that_came_in_time_is_not_counted_late_afterwards(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    slow_to_accept(speaker, monkeypatch, 0, 6)
    connection = air.run(page(phone, SPEAKER), within=seconds(1))
    air.run(connection.disconnect(), within=seconds(1))
    second = air.start(page(phone, SPEAKER))
    air.advance(ms(50))
    speaker.switch_off()
    speaker.switch_on()
    air.advance(seconds(7))
    assert isinstance(second.exception(), core.ConnectionError)
    # Only the second answer was late: the third, to this page, connects.
    air.run(page(phone, SPEAKER), within=seconds(1))
    assert phone.device.connections


def test_a_late_answer_leaves_a_working_link_alone(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    slow_to_accept(speaker, monkeypatch, 6)
    failed = failures_of(air, speaker)
    first = air.start(page(phone, SPEAKER))
    air.advance(ms(50))
    speaker.switch_off()
    speaker.switch_on()
    air.advance(ms(5_500))
    assert first.done()
    # A link the other way, made while the answer to the first page is on its way.
    air.run(page(speaker, PHONE), within=seconds(1))
    handles = list(phone.device.connections), list(speaker.device.connections)
    assert handles == ([1], [1])

    air.advance(seconds(7))
    assert failed == [(6_000 * MS, hci.HCI_ErrorCode.CONNECTION_TIMEOUT_ERROR)]
    assert (list(phone.device.connections), list(speaker.device.connections)) == handles
    controller = air.link.find_classic_controller(speaker.device.public_address)
    assert controller is not None
    assert [c.handle for c in controller.classic_connections.values()] == [1]


def test_a_late_answer_does_not_complete_a_page_to_a_device_that_left(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    slow_to_accept(speaker, monkeypatch, 6)
    first = air.start(page(phone, SPEAKER))
    air.advance(ms(50))
    speaker.switch_off()
    speaker.switch_on()
    phone.out_of_range()
    air.advance(ms(5_500))
    assert first.done()

    ended: list[int] = []

    async def page_back() -> None:
        with pytest.raises(core.ConnectionError) as raised:
            await page(speaker, PHONE)
        assert raised.value.error_code == hci.HCI_ErrorCode.PAGE_TIMEOUT_ERROR
        ended.append(air.loop.now)

    air.run(page_back(), within=seconds(10))
    assert ended == [10_620 * MS]
    assert not speaker.device.connections


def test_a_page_tried_again_with_a_slow_host_does_not_fail_the_air(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no accept timeout, the first answer connects the second page: the second answer is
    the late one, and the link stays."""
    phone, speaker = phone_and_speaker(air)
    slow_to_accept(speaker, monkeypatch, 6, 6)
    failed = failures_of(air, speaker)
    first = air.start(page(phone, SPEAKER))
    air.advance(ms(5_500))
    assert isinstance(first.exception(), core.ConnectionError)
    second = air.start(page(phone, SPEAKER))
    air.advance(seconds(7))
    assert second.done()
    assert second.exception() is None
    air.advance(seconds(12))
    assert failed == [(11_500 * MS, hci.HCI_ErrorCode.CONNECTION_TIMEOUT_ERROR)]
    assert list(phone.device.connections) == [1]
    assert list(speaker.device.connections) == [1]


def test_every_late_answer_is_counted_before_a_new_page_is_answered(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    slow_to_accept(speaker, monkeypatch, 6, 6, 1)
    first = air.start(page(phone, SPEAKER))
    air.advance(ms(5_500))
    assert isinstance(first.exception(), core.ConnectionError)
    second = air.start(page(phone, SPEAKER))
    air.advance(ms(5_700))
    speaker.switch_off()
    speaker.switch_on()
    air.advance(ms(10_700))
    assert isinstance(second.exception(), core.ConnectionError)

    third = air.start(page(phone, SPEAKER))
    air.advance(ms(11_600))
    # The answers at 6 s and 11.5 s were both to pages that ended; this page waits for its own.
    assert not third.done()
    air.advance(ms(11_700))
    assert third.done()
    assert third.exception() is None


def test_an_answer_owed_after_the_link_was_ended_does_not_fail_the_air(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    slow_to_accept(speaker, monkeypatch, 6, 6)
    failed = failures_of(air, speaker)
    first = air.start(page(phone, SPEAKER))
    air.advance(ms(5_500))
    assert isinstance(first.exception(), core.ConnectionError)
    second = air.start(page(phone, SPEAKER))
    air.advance(seconds(7))
    # The first answer connected the second page; the link ends before the second answer comes.
    air.run(second.result().disconnect(), within=seconds(1))
    assert not speaker.device.connections
    air.advance(seconds(12))
    assert failed == [(11_500 * MS, hci.HCI_ErrorCode.CONNECTION_TIMEOUT_ERROR)]
    assert not speaker.device.connections


def test_an_answer_after_both_crossing_pages_ended_makes_a_link(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    slow_to_accept(speaker, monkeypatch, 5.5)
    failed = failures_of(air, speaker)
    outgoing, incoming = crossing_pages(air, phone, speaker)
    air.advance(seconds(7))
    assert isinstance(outgoing.exception(), core.ConnectionError)
    assert isinstance(incoming.exception(), core.ConnectionError)
    # The speaker's page ended at its timeout and left the phone's page its entry, which the
    # answer finds: with no accept timeout the link is made though both pages reported an end.
    assert failed == [(5_120 * MS, PAGE_TIMEOUT)]
    assert list(phone.device.connections) == list(speaker.device.connections) == [1]


def test_a_target_that_paged_too_loses_contact_with_a_pager_that_leaves(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    slow_to_accept(speaker, monkeypatch, 30)
    failed = failures_of(air, speaker)
    outgoing, incoming = crossing_pages(air, phone, speaker)
    air.advance(seconds(2))
    phone.out_of_range()
    air.advance(seconds(10))
    assert isinstance(outgoing.exception(), core.ConnectionError)
    assert isinstance(incoming.exception(), core.ConnectionError)
    # The entry of the phone's page outlives the speaker's own page, and falls in its own time.
    assert held_by(air, speaker) == [hci.Address(PHONE + "/P")]
    air.advance(ms(21_999))
    assert held_by(air, speaker) == [hci.Address(PHONE + "/P")]
    air.advance(ms(22_001))
    assert held_by(air, speaker) == []
    air.advance(seconds(31))
    assert failed == [
        (5_120 * MS, PAGE_TIMEOUT),
        (31_000 * MS, hci.HCI_ErrorCode.CONNECTION_TIMEOUT_ERROR),
    ]
    phone.in_range()
    monkeypatch.undo()
    air.run(page(phone, SPEAKER), within=seconds(1))


def test_a_page_this_side_failed_does_not_mend_the_link_to_a_pager_that_lost_power(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    slow_to_accept(speaker, monkeypatch, 9)
    fell = falls_of(air, speaker)
    outgoing, incoming = crossing_pages(air, phone, speaker)
    air.advance(seconds(2))
    phone.switch_off()
    air.advance(seconds(6))
    assert isinstance(outgoing.exception(), core.ConnectionError)
    assert isinstance(incoming.exception(), core.ConnectionError)
    phone.switch_on()
    # The speaker still holds the phone's page, whose answer comes at 10 s: it reaches nobody.
    air.advance(seconds(11))
    assert not phone.device.connections
    assert list(speaker.device.connections) == [1]
    air.advance(seconds(23))
    assert fell == [(22_000 * MS, hci.HCI_ErrorCode.CONNECTION_TIMEOUT_ERROR)]


def test_an_answer_nothing_is_owed_for_is_the_hosts_own_error(air: Air) -> None:
    phone, _ = phone_and_speaker(air)
    accept = hci.HCI_Accept_Connection_Request_Command(
        bd_addr=hci.Address(SPEAKER + "/P"), role=hci.Role.PERIPHERAL
    )
    with pytest.raises(hci.HCI_Error) as raised:
        air.run(phone.device.host.send_async_command(accept), within=seconds(0))
    assert raised.value.error_code == hci.HCI_ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR


def test_two_answers_to_pages_that_ended_do_not_fail_the_air(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    slow_to_accept(speaker, monkeypatch, 6, 6)
    failed = failures_of(air, speaker)
    first = air.start(page(phone, SPEAKER))
    air.advance(ms(5_500))
    assert isinstance(first.exception(), core.ConnectionError)
    second = air.start(page(phone, SPEAKER))
    air.advance(ms(5_700))
    speaker.switch_off()
    air.advance(seconds(12))
    assert isinstance(second.exception(), core.ConnectionError)
    timeout = hci.HCI_ErrorCode.CONNECTION_TIMEOUT_ERROR
    assert failed == [(6_000 * MS, timeout), (11_500 * MS, timeout)]
    assert not speaker.device.connections


LIMITED = hci.HCI_ErrorCode.CONNECTION_REJECTED_DUE_TO_LIMITED_RESOURCES_ERROR
PAGE_TIMEOUT = hci.HCI_ErrorCode.PAGE_TIMEOUT_ERROR


def rejecting(device: BluetoothDevice) -> BluetoothDevice:
    """`device`, whose host rejects every page it hears, as Bumble's does with no one waiting."""
    device.device.classic_accept_any = False
    return device


def crossing_pages(
    air: Air, phone: BluetoothDevice, speaker: BluetoothDevice
) -> tuple[asyncio.Task[Connection], asyncio.Task[Connection]]:
    """The speaker pages the phone, which is away; the phone returns at 1 s, and pages it back. The
    speaker's entry is then that of the phone's page, and its own page is still under way."""
    phone.out_of_range()
    outgoing = air.start(page(speaker, PHONE))
    air.advance(seconds(1))
    phone.in_range()
    return outgoing, air.start(page(phone, SPEAKER))


async def paging_each_other(phone: BluetoothDevice, speaker: BluetoothDevice) -> Any:
    """Both page at one instant, so that each hears the other's page during its own."""
    return await asyncio.gather(page(phone, SPEAKER), page(speaker, PHONE), return_exceptions=True)


def reject_page(address: str, reason: int) -> hci.HCI_Reject_Connection_Request_Command:
    return hci.HCI_Reject_Connection_Request_Command(
        bd_addr=hci.Address(address + "/P"), reason=reason
    )


def write_page_timeout(air: Air, device: BluetoothDevice, slots: int) -> None:
    command = hci.HCI_Write_Page_Timeout_Command(page_timeout=slots)
    air.run(device.device.host.send_sync_command(command), within=seconds(0))


def held_by(air: Air, device: BluetoothDevice) -> list[hci.Address]:
    """The peers the controller of `device` holds an entry for."""
    controller = air.link.find_classic_controller(device.device.public_address)
    assert controller is not None
    return list(controller.classic_connections)


def test_a_pager_gets_the_reason_at_the_instant_of_the_rejection(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    rejecting(speaker)
    slow_to_accept(speaker, monkeypatch, 1)
    paging = failures_of(air, phone)
    rejected = failures_of(air, speaker)
    with pytest.raises(core.ConnectionError) as raised:
        air.run(page(phone, SPEAKER), within=seconds(2))
    assert raised.value.error_code == LIMITED
    assert air.now == seconds(1)
    assert paging == [(1_000 * MS, LIMITED)]
    assert rejected == [(1_000 * MS, LIMITED)]


def test_a_host_that_rejects_a_page_goes_on_working(air: Air) -> None:
    phone, speaker = phone_and_speaker(air)
    rejecting(speaker)
    with pytest.raises(core.ConnectionError):
        air.run(page(phone, SPEAKER), within=seconds(1))
    assert air.now == seconds(0)
    assert held_by(air, phone) == []
    assert held_by(air, speaker) == []
    air.run(speaker.device.set_connectable(True), within=seconds(1))


def test_both_can_connect_after_a_rejection(air: Air) -> None:
    phone, speaker = phone_and_speaker(air)
    rejecting(speaker)
    with pytest.raises(core.ConnectionError):
        air.run(page(phone, SPEAKER), within=seconds(1))
    # The other way, while the host would still reject a page.
    connection = air.run(page(speaker, PHONE), within=seconds(1))
    air.run(connection.disconnect(), within=seconds(1))
    speaker.device.classic_accept_any = True
    air.run(page(phone, SPEAKER), within=seconds(1))
    assert list(phone.device.connections) == list(speaker.device.connections) == [1]


def test_a_rejection_of_a_page_that_ended_is_not_heard_by_the_pager(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    rejecting(speaker)
    slow_to_accept(speaker, monkeypatch, 6)
    paging = failures_of(air, phone)
    rejected = failures_of(air, speaker)
    task = air.start(page(phone, SPEAKER))
    air.advance(seconds(7))
    assert isinstance(task.exception(), core.ConnectionError)
    assert paging == [(5_120 * MS, PAGE_TIMEOUT)]
    assert rejected == [(6_000 * MS, LIMITED)]
    assert held_by(air, speaker) == []
    speaker.device.classic_accept_any = True
    air.run(page(phone, SPEAKER), within=seconds(1))


def test_a_rejection_after_the_end_fell_is_late_and_does_not_end_the_next_page(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    rejecting(speaker)
    slow_to_accept(speaker, monkeypatch, 6, 1)
    paging = failures_of(air, phone)
    rejected = failures_of(air, speaker)
    first = air.start(page(phone, SPEAKER))
    air.advance(ms(50))
    speaker.switch_off()
    speaker.switch_on()
    air.advance(ms(5_500))
    assert isinstance(first.exception(), core.ConnectionError)

    second = air.start(page(phone, SPEAKER))
    air.advance(ms(6_100))
    # The answer to the first, at 6 s, reaches nobody; the second page waits for its own.
    assert rejected == [(6_000 * MS, hci.HCI_ErrorCode.CONNECTION_TIMEOUT_ERROR)]
    assert paging == [(5_120 * MS, PAGE_TIMEOUT)]
    assert not second.done()
    air.advance(ms(6_500))
    assert paging == [(5_120 * MS, PAGE_TIMEOUT), (6_500 * MS, LIMITED)]
    assert rejected[1:] == [(6_500 * MS, LIMITED)]


def test_a_late_rejection_leaves_a_working_link_alone(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    rejecting(speaker)
    slow_to_accept(speaker, monkeypatch, 6)
    first = air.start(page(phone, SPEAKER))
    air.advance(ms(50))
    speaker.switch_off()
    speaker.switch_on()
    air.advance(ms(5_500))
    assert first.done()
    air.run(page(speaker, PHONE), within=seconds(1))
    air.advance(seconds(7))
    assert list(phone.device.connections) == list(speaker.device.connections) == [1]
    assert held_by(air, speaker) == [hci.Address(PHONE + "/P")]


def test_the_answer_after_the_rejection_of_a_page_tried_again_is_late(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first answer ends the second page, as an acceptance would connect it, so the second
    answer has nothing left to answer."""
    phone, speaker = phone_and_speaker(air)
    rejecting(speaker)
    slow_to_accept(speaker, monkeypatch, 6, 6)
    paging = failures_of(air, phone)
    rejected = failures_of(air, speaker)
    first = air.start(page(phone, SPEAKER))
    air.advance(ms(5_500))
    assert isinstance(first.exception(), core.ConnectionError)
    second = air.start(page(phone, SPEAKER))
    air.advance(seconds(12))
    assert isinstance(second.exception(), core.ConnectionError)
    assert paging == [(5_120 * MS, PAGE_TIMEOUT), (6_000 * MS, LIMITED)]
    assert rejected == [
        (6_000 * MS, LIMITED),
        (11_500 * MS, hci.HCI_ErrorCode.CONNECTION_TIMEOUT_ERROR),
    ]


def test_a_rejection_leaves_this_sides_own_page_to_end_by_itself(air: Air) -> None:
    phone, speaker = phone_and_speaker(air)
    rejecting(speaker)
    paging = failures_of(air, phone)
    rejected = failures_of(air, speaker)
    outgoing, incoming = crossing_pages(air, phone, speaker)
    air.advance(ms(1_001))
    assert isinstance(incoming.exception(), core.ConnectionError)
    assert paging == [(1_000 * MS, LIMITED)]
    # The event names only the address: to the speaker's host it would end its own page.
    assert rejected == []
    assert not outgoing.done()
    air.advance(seconds(6))
    assert isinstance(outgoing.exception(), core.ConnectionError)
    assert rejected == [(5_120 * MS, PAGE_TIMEOUT)]


@pytest.mark.parametrize("rejects", [False, True], ids=["accept", "reject"])
def test_an_answer_owed_while_this_side_pages_again_does_not_touch_that_page(
    air: Air, rejects: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    slow_to_accept(speaker, monkeypatch, 6, 6)
    failed = failures_of(air, speaker)
    first = air.start(page(phone, SPEAKER))
    air.advance(ms(5_500))
    assert isinstance(first.exception(), core.ConnectionError)
    # The answer to the first page connects this one, and the answer to this one is left owing.
    speaker.device.classic_accept_any = not rejects
    second = air.start(page(phone, SPEAKER))
    air.advance(seconds(7))
    air.run(second.result().disconnect(), within=seconds(1))
    phone.out_of_range()
    again = air.start(page(speaker, PHONE))
    air.advance(seconds(8))
    assert held_by(air, speaker) == [hci.Address(PHONE + "/P")]
    # The answer comes at 11.5 s, and finds only this side's own page.
    air.advance(ms(12_000))
    assert held_by(air, speaker) == [hci.Address(PHONE + "/P")]
    air.advance(ms(12_119))
    assert not again.done()
    air.advance(ms(12_121))
    assert isinstance(again.exception(), core.ConnectionError)
    assert [status for _, status in failed] == [PAGE_TIMEOUT]
    assert not speaker.device.connections


def test_a_page_the_controller_refused_leaves_no_timer_and_can_be_tried_again(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, _ = phone_and_speaker(air)
    failed = failures_of(air, phone)
    controller = air.link.find_classic_controller(phone.device.public_address)
    assert controller is not None
    monkeypatch.setattr(controller, "pending_le_connection", object())
    with pytest.raises(hci.HCI_Error) as raised:
        air.run(page(phone, SPEAKER), within=seconds(0))
    assert raised.value.error_code == hci.HCI_ErrorCode.CONTROLLER_BUSY_ERROR
    monkeypatch.undo()
    air.run(page(phone, SPEAKER), within=seconds(1))
    assert list(phone.device.connections) == [1]
    # Past the 5.12 s the refused page's timer would have ended at.
    air.advance(seconds(6))
    assert failed == []
    assert list(phone.device.connections) == [1]


def test_a_device_that_leaves_during_its_own_page_loses_contact_with_a_page_it_heard(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pager's page ends first, so only the device that leaves holds anything of it."""
    phone, speaker = phone_and_speaker(air)
    write_page_timeout(air, phone, 1600)
    slow_to_accept(speaker, monkeypatch, 19)
    fell = falls_of(air, speaker)
    outgoing, incoming = crossing_pages(air, phone, speaker)
    air.advance(seconds(3))
    assert isinstance(incoming.exception(), core.ConnectionError)
    assert not outgoing.done()
    speaker.out_of_range()
    # The host answers at 20 s a page whose contact was lost at 3 s; that end falls 20 s after it.
    air.advance(seconds(25))
    assert isinstance(outgoing.exception(), core.ConnectionError)
    assert fell == [(23_000 * MS, hci.HCI_ErrorCode.CONNECTION_TIMEOUT_ERROR)]
    assert not speaker.device.connections
    assert not phone.device.connections


def skipping_answers(target: BluetoothDevice, patch: pytest.MonkeyPatch, skipped: int) -> None:
    """The host of `target` does not answer the next `skipped` pages it hears, as firmware may."""
    left = [skipped]

    async def maybe(command: hci.HCI_AsyncCommand) -> hci.HCI_AsyncCommand | None:
        if left[0] > 0:
            left[0] -= 1
            return None
        return command

    changing_answers(target, patch, maybe)


@pytest.mark.parametrize("rejects", [False, True], ids=["accept", "reject"])
def test_an_answer_the_host_never_gave_does_not_spoil_a_later_page(
    air: Air, rejects: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    skipping_answers(speaker, monkeypatch, 1)
    paging = failures_of(air, phone)
    first = air.start(page(phone, SPEAKER))
    air.advance(seconds(6))
    assert isinstance(first.exception(), core.ConnectionError)
    assert paging == [(5_120 * MS, PAGE_TIMEOUT)]

    # The second page gets its answer, and the third must get its own all the same.
    speaker.device.classic_accept_any = not rejects
    if rejects:
        with pytest.raises(core.ConnectionError):
            air.run(page(phone, SPEAKER), within=seconds(1))
    else:
        connection = air.run(page(phone, SPEAKER), within=seconds(1))
        air.run(connection.disconnect(), within=seconds(1))
    speaker.device.classic_accept_any = True
    air.run(page(phone, SPEAKER), within=seconds(1))
    assert list(phone.device.connections) == list(speaker.device.connections)
    assert len(phone.device.connections) == 1


def test_a_rejection_after_the_pager_left_and_its_page_ended_lets_the_pair_go(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    rejecting(speaker)
    slow_to_accept(speaker, monkeypatch, 6)
    task = air.start(page(phone, SPEAKER))
    air.advance(ms(50))
    phone.out_of_range()
    air.advance(seconds(7))
    assert isinstance(task.exception(), core.ConnectionError)
    assert held_by(air, speaker) == []
    phone.in_range()
    speaker.device.classic_accept_any = True
    air.run(page(phone, SPEAKER), within=seconds(1))


def test_a_page_of_this_side_that_fails_takes_its_entry_with_an_answer_never_given(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    skipping_answers(speaker, monkeypatch, 1)
    first = air.start(page(phone, SPEAKER))
    air.advance(seconds(6))
    assert isinstance(first.exception(), core.ConnectionError)
    connection = air.run(page(phone, SPEAKER), within=seconds(1))
    air.run(connection.disconnect(), within=seconds(1))

    phone.out_of_range()
    again = air.start(page(speaker, PHONE))
    air.advance(seconds(12))
    assert isinstance(again.exception(), core.ConnectionError)
    assert held_by(air, speaker) == []
    phone.in_range()
    air.run(page(speaker, PHONE), within=seconds(1))


def accepting_as_central_after(
    target: BluetoothDevice, patch: pytest.MonkeyPatch, *skipped_then_delay: float
) -> None:
    """The host of `target` does not answer one page for each 0 given, then accepts a page after
    the delay given, asking for the central's role."""
    left = list(skipped_then_delay)

    async def as_central(command: hci.HCI_AsyncCommand) -> hci.HCI_AsyncCommand | None:
        delay = left.pop(0)
        if delay == 0:
            return None
        await asyncio.sleep(delay)
        assert isinstance(command, hci.HCI_Accept_Connection_Request_Command)
        return hci.HCI_Accept_Connection_Request_Command(
            bd_addr=command.bd_addr, role=hci.Role.CENTRAL
        )

    changing_answers(target, patch, as_central)


def refusing_role_switch(air: Air, pager: BluetoothDevice, target: BluetoothDevice) -> None:
    controller = air.link.find_classic_controller(pager.device.public_address)
    assert controller is not None
    entry = controller.classic_connections[target.device.public_address]
    entry.classic_allow_role_switch = False


def test_an_acceptance_whose_role_switch_is_refused_leaves_no_entry(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    accepting_as_central_after(speaker, monkeypatch, 1)
    task = air.start(page(phone, SPEAKER))
    air.advance(ms(50))
    refusing_role_switch(air, phone, speaker)
    air.advance(seconds(2))
    assert isinstance(task.exception(), core.ConnectionError)
    assert held_by(air, speaker) == []
    monkeypatch.undo()
    air.run(page(phone, SPEAKER), within=seconds(1))


def test_a_refused_role_switch_leaves_no_entry_with_an_answer_never_given(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    accepting_as_central_after(speaker, monkeypatch, 0, 1)
    first = air.start(page(phone, SPEAKER))
    air.advance(seconds(6))
    assert isinstance(first.exception(), core.ConnectionError)
    second = air.start(page(phone, SPEAKER))
    air.advance(ms(6_050))
    refusing_role_switch(air, phone, speaker)
    air.advance(seconds(8))
    assert isinstance(second.exception(), core.ConnectionError)
    assert held_by(air, speaker) == []


def test_a_refused_role_switch_leaves_no_entry_while_this_side_pages_too(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    accepting_as_central_after(speaker, monkeypatch, 1)
    outgoing, incoming = crossing_pages(air, phone, speaker)
    air.advance(ms(1_050))
    refusing_role_switch(air, phone, speaker)
    air.advance(seconds(3))
    assert isinstance(incoming.exception(), core.ConnectionError)
    assert isinstance(outgoing.exception(), core.ConnectionError)
    assert held_by(air, speaker) == []
    monkeypatch.undo()
    air.run(page(speaker, PHONE), within=seconds(1))


def test_a_pager_that_leaves_after_its_page_ended_is_lost_to_the_target(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    slow_to_accept(speaker, monkeypatch, 10)
    fell = falls_of(air, speaker)
    task = air.start(page(phone, SPEAKER))
    air.advance(seconds(6))
    assert isinstance(task.exception(), core.ConnectionError)
    phone.out_of_range()
    # The answer at 10 s makes a link on the target's end only, which falls 20 s from the leaving.
    air.advance(seconds(27))
    assert fell == [(26_000 * MS, hci.HCI_ErrorCode.CONNECTION_TIMEOUT_ERROR)]
    assert not speaker.device.connections
    assert not phone.device.connections
    phone.in_range()
    monkeypatch.undo()
    air.run(page(phone, SPEAKER), within=seconds(1))


def test_a_pager_that_rejected_a_page_and_leaves_during_its_own_is_lost_to_the_target(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    rejecting(phone)
    slow_to_accept(speaker, monkeypatch, 3)
    fell = falls_of(air, speaker)
    task = air.start(paging_each_other(phone, speaker))
    air.advance(ms(500))
    assert held_by(air, phone) == []
    phone.out_of_range()
    air.advance(seconds(22))
    assert task.done()
    assert fell == [(20_500 * MS, hci.HCI_ErrorCode.CONNECTION_TIMEOUT_ERROR)]
    assert not speaker.device.connections
    assert not phone.device.connections


def test_a_page_that_ended_leaves_no_timer_to_fell_the_next_link(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    slow_to_accept(speaker, monkeypatch, 1)
    fell = falls_of(air, phone)
    task = air.start(page(phone, SPEAKER))
    air.advance(ms(50))
    phone.out_of_range()
    # The target's end is a link by 1 s, and its host ends it after the phone's page has ended.
    air.advance(seconds(6))
    assert isinstance(task.exception(), core.ConnectionError)
    held = list(speaker.device.connections.values())
    assert len(held) == 1
    air.run(held[0].disconnect(), within=seconds(1))
    phone.in_range()
    monkeypatch.undo()
    air.run(page(phone, SPEAKER), within=seconds(1))
    # Past the 20 s after the leaving that a timer of the old page would have ended this one at.
    air.advance(seconds(25))
    assert fell == []
    assert len(phone.device.connections) == 1


def test_an_entry_that_goes_with_a_failed_page_leaves_no_timer_to_fell_the_next_link(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    rejecting(phone)
    slow_to_accept(phone, monkeypatch, 3)
    # An acceptance that waits for a role switch nobody can answer: the entry stays, lost at 1 s,
    # with no answer owed, until the speaker's own page ends at 5.12 s and takes it.
    accepting_as_central_after(speaker, monkeypatch, 2)
    fell = falls_of(air, speaker)
    task = air.start(paging_each_other(phone, speaker))
    air.advance(seconds(1))
    phone.out_of_range()
    air.advance(seconds(6))
    assert task.done()
    assert held_by(air, speaker) == []
    phone.in_range()
    monkeypatch.undo()
    air.run(page(phone, SPEAKER), within=seconds(1))
    # Past the 20 s from the leaving, at which a timer of that entry would have felled this link.
    air.advance(seconds(25))
    assert fell == []
    assert len(speaker.device.connections) == len(phone.device.connections) == 1


def test_a_page_longer_than_the_supervision_timeout_keeps_its_entry_until_it_ends(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    write_page_timeout(air, phone, 0xFFFF)
    slow_to_accept(speaker, monkeypatch, 30)
    task = air.start(page(phone, SPEAKER))
    air.advance(seconds(1))
    phone.out_of_range()
    # The target's end falls at 21 s; the phone's own page has no contact to lose, and goes on.
    air.advance(seconds(25))
    assert held_by(air, speaker) == []
    assert held_by(air, phone) == [hci.Address(SPEAKER + "/P")]
    assert not task.done()
    air.advance(seconds(41))
    assert isinstance(task.exception(), core.ConnectionError)
    assert held_by(air, phone) == []


def test_a_device_that_leaves_is_not_lost_to_a_page_it_never_heard(air: Air) -> None:
    phone, speaker = phone_and_speaker(air)
    phone.out_of_range()
    air.start(page(speaker, PHONE))
    air.advance(seconds(1))
    phone.in_range()
    phone.out_of_range()
    phone.in_range()
    # Nothing is severed: the phone hears nothing of the old page, and can page the speaker at once.
    air.run(page(phone, SPEAKER), within=seconds(1))
    assert list(phone.device.connections) == [1]


def test_a_rejection_on_a_working_link_is_the_hosts_own_error(air: Air) -> None:
    phone, speaker = phone_and_speaker(air)
    failed = failures_of(air, speaker)
    air.run(page(phone, SPEAKER), within=seconds(1))
    reject = reject_page(PHONE, LIMITED)
    with pytest.raises(hci.HCI_Error) as raised:
        air.run(speaker.device.host.send_async_command(reject), within=seconds(0))
    assert raised.value.error_code == hci.HCI_ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
    air.wait(seconds(1))
    assert failed == []
    assert list(speaker.device.connections) == [1]


def test_an_acceptance_on_a_working_link_is_the_hosts_own_error(air: Air) -> None:
    phone, speaker = phone_and_speaker(air)
    failed = failures_of(air, speaker)
    air.run(page(phone, SPEAKER), within=seconds(1))
    accept = hci.HCI_Accept_Connection_Request_Command(
        bd_addr=hci.Address(PHONE + "/P"), role=hci.Role.PERIPHERAL
    )
    with pytest.raises(hci.HCI_Error) as raised:
        air.run(speaker.device.host.send_async_command(accept), within=seconds(0))
    assert raised.value.error_code == hci.HCI_ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
    air.wait(seconds(1))
    assert failed == []
    assert list(speaker.device.connections) == [1]


def test_a_page_under_way_is_not_started_again_after_a_rejection_took_the_entry(air: Air) -> None:
    phone, speaker = phone_and_speaker(air)
    rejecting(speaker)
    failed = failures_of(air, speaker)
    outgoing, incoming = crossing_pages(air, phone, speaker)
    air.advance(ms(1_001))
    assert isinstance(incoming.exception(), core.ConnectionError)
    assert held_by(air, speaker) == []

    with pytest.raises(hci.HCI_Error) as raised:
        air.run(page(speaker, PHONE), within=seconds(0))
    assert raised.value.error_code == hci.HCI_ErrorCode.CONNECTION_ALREADY_EXISTS_ERROR
    air.advance(seconds(6))
    assert isinstance(outgoing.exception(), core.ConnectionError)
    assert failed == [(5_120 * MS, PAGE_TIMEOUT)]
    # Its timer ended it: the next page runs its whole length.
    phone.out_of_range()
    again = air.start(page(speaker, PHONE))
    air.advance(ms(11_119))
    assert not again.done()
    air.advance(ms(11_121))
    assert again.done()
    assert [status for _, status in failed] == [PAGE_TIMEOUT, PAGE_TIMEOUT]


@pytest.mark.parametrize("rejects", [False, True], ids=["accept", "reject"])
def test_a_page_this_side_failed_leaves_the_answer_to_a_crossing_page_its_entry(
    air: Air, rejects: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    speaker.device.classic_accept_any = not rejects
    slow_to_accept(speaker, monkeypatch, 4.5)
    paging = failures_of(air, phone)
    answering = failures_of(air, speaker)
    outgoing, incoming = crossing_pages(air, phone, speaker)
    air.advance(ms(5_200))
    # The speaker's own page is over, and the phone's still waits for the speaker's host.
    assert isinstance(outgoing.exception(), core.ConnectionError)
    assert held_by(air, speaker) == [hci.Address(PHONE + "/P")]
    assert not incoming.done()
    # That entry refuses another page, though this one has reported its end.
    with pytest.raises(hci.HCI_Error) as raised:
        air.run(page(speaker, PHONE), within=seconds(0))
    assert raised.value.error_code == hci.HCI_ErrorCode.CONNECTION_ALREADY_EXISTS_ERROR
    air.advance(ms(5_600))
    assert incoming.done()
    if rejects:
        assert paging == [(5_500 * MS, LIMITED)]
        assert answering == [(5_120 * MS, PAGE_TIMEOUT), (5_500 * MS, LIMITED)]
        assert held_by(air, speaker) == []
    else:
        assert incoming.exception() is None
        assert paging == []
        assert answering == [(5_120 * MS, PAGE_TIMEOUT)]
        assert list(phone.device.connections) == list(speaker.device.connections) == [1]


def test_a_rejection_ends_the_supervision_of_a_page_whose_pager_left(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    rejecting(speaker)
    slow_to_accept(speaker, monkeypatch, 1)
    paging = failures_of(air, phone)
    rejected = failures_of(air, speaker)
    fell = falls_of(air, speaker)
    task = air.start(page(phone, SPEAKER))
    air.advance(ms(50))
    phone.out_of_range()
    air.advance(seconds(6))
    assert isinstance(task.exception(), core.ConnectionError)
    # The pager was out of reach of the rejection: its page ended by itself.
    assert paging == [(5_120 * MS, PAGE_TIMEOUT)]
    assert rejected == [(1_000 * MS, LIMITED)]
    phone.in_range()
    speaker.device.classic_accept_any = True
    air.run(page(phone, SPEAKER), within=seconds(1))
    # Past the 20 s the first page's timer would have fallen at.
    air.advance(seconds(25))
    assert fell == []
    assert list(speaker.device.connections) == [1]


@pytest.mark.parametrize("reason", [0x00, 0x0C, 0x10], ids=hex)
def test_a_reason_outside_the_range_is_refused_and_the_answer_stays_owed(
    air: Air, reason: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    slow_to_accept(speaker, monkeypatch, 1, 0)
    task = air.start(page(phone, SPEAKER))
    air.advance(ms(50))
    reject = reject_page(PHONE, reason)
    with pytest.raises(hci.HCI_Error) as raised:
        air.run(speaker.device.host.send_async_command(reject), within=seconds(0))
    assert raised.value.error_code == hci.HCI_ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR
    assert not task.done()
    # The acceptance on its way is still the answer to this page.
    air.advance(seconds(2))
    assert task.exception() is None
    assert list(speaker.device.connections) == [1]


@pytest.mark.parametrize("reason", [0x0D, 0x0E, 0x0F], ids=hex)
def test_a_reason_in_the_range_reaches_both_hosts(
    air: Air, reason: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    # The host's own acceptance waits, so that this rejection is the first answer.
    slow_to_accept(speaker, monkeypatch, 1, 0)
    paging = failures_of(air, phone)
    rejected = failures_of(air, speaker)
    task = air.start(page(phone, SPEAKER))
    air.advance(ms(50))
    reject = reject_page(PHONE, reason)
    air.run(speaker.device.host.send_async_command(reject), within=seconds(0))
    air.advance(ms(51))
    assert isinstance(task.exception(), core.ConnectionError)
    assert paging == [(50 * MS, reason)]
    assert rejected == [(50 * MS, reason)]


def test_a_rejection_nothing_is_owed_for_is_the_hosts_own_error(air: Air) -> None:
    phone, _ = phone_and_speaker(air)
    reject = reject_page(SPEAKER, LIMITED)
    with pytest.raises(hci.HCI_Error) as raised:
        air.run(phone.device.host.send_async_command(reject), within=seconds(0))
    assert raised.value.error_code == hci.HCI_ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR


def test_a_power_loss_at_the_instant_a_page_is_heard_does_not_fail_the_air(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    target = air.link.find_classic_controller(speaker.device.public_address)
    assert target is not None
    heard = target.on_classic_connection_request

    def hear_and_lose_power(peer: hci.Address, link_type: int) -> None:
        heard(peer, link_type)
        speaker.switch_off()

    monkeypatch.setattr(target, "on_classic_connection_request", hear_and_lose_power)
    with pytest.raises(core.ConnectionError) as raised:
        air.run(page(phone, SPEAKER), within=seconds(10))
    assert raised.value.error_code == hci.HCI_ErrorCode.PAGE_TIMEOUT_ERROR
    assert air.now == ms(5_120)


def test_a_power_loss_ends_an_inquiry_that_would_restart_itself(air: Air) -> None:
    phone, _ = phone_and_speaker(air)
    air.run(phone.device.start_discovery(), within=seconds(0))
    air.wait(seconds(1))
    phone.switch_off()
    air.wait(seconds(1))
    phone.switch_on()
    # A device switched off starts no inquiry by itself, so this one is not refused.
    air.run(phone.device.start_discovery(auto_restart=False), within=seconds(0))
