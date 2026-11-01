"""The SPP peer (`esp32tl/bluetooth/peer.py`) and the controller under it
(`esp32tl/bluetooth/controller.py`) on a Bumble virtual link, without the emulator.

A second Bumble device stands in for the ESP32 and does what ESP-IDF's `bt_spp_initiator` does:
an inquiry until it finds the peer's name, a connection, authentication with a PIN, encryption,
an SDP search for the Serial Port service, and an RFCOMM channel to it. A model in the test
answers what it writes, on the board's clock."""

import asyncio
import re
from collections.abc import Iterable, Iterator

import pytest
from bumble import core, hci, rfcomm
from bumble.device import Connection, Device, DeviceConfiguration
from bumble.host import Host
from bumble.keys import MemoryKeyStore
from bumble.pairing import PairingConfig, PairingDelegate
from bumble.transport.common import AsyncPipeSink

from esp32tl.bluetooth.air import Air, AirError
from esp32tl.bluetooth.controller import INQUIRY_UNIT_S, Controller
from esp32tl.bluetooth.peer import CHANNEL, CLASS_OF_DEVICE, PeerError, SppPeer
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
        self.received: list[tuple[int, bytes]] = []
        self.inquiry_complete: list[int] = []
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
        return device

    def _on_result(
        self, address: hci.Address, class_of_device: int, eir: core.AdvertisingData, rssi: int
    ) -> None:
        name = eir.get(core.AdvertisingData.COMPLETE_LOCAL_NAME)
        assert name is None or isinstance(name, str)
        self.found.append((self.air.loop.now, str(address), class_of_device, name))

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
    peer.device.keystore = MemoryKeyStore()
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
