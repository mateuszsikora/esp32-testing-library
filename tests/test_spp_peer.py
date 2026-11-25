"""The SPP peer (`esp32tl/bluetooth/peer.py`) and the controller under it
(`esp32tl/bluetooth/controller.py`) on a Bumble virtual link, without the emulator.

A second Bumble device stands in for the ESP32 and does what ESP-IDF's `bt_spp_initiator` does:
an inquiry until it finds the peer's name, a connection, authentication with a PIN, encryption,
an SDP search for the Serial Port service, and an RFCOMM channel to it. A model in the test
answers what it writes, on the board's clock."""

import asyncio
import re
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Iterator
from functools import partial
from typing import Any

import pytest
from bumble import core, hci, l2cap, rfcomm, sdp
from bumble.device import Connection, Device, DeviceConfiguration
from bumble.host import Host
from bumble.keys import MemoryKeyStore
from bumble.pairing import PairingConfig, PairingDelegate
from bumble.transport.common import AsyncPipeSink
from conftest import CountingList

from esp32tl.bluetooth.air import Air, AirError
from esp32tl.bluetooth.controller import INQUIRY_UNIT_S, Controller
from esp32tl.bluetooth.peer import (
    CHANNEL,
    CLASS_OF_DEVICE,
    SERVICE_NAME,
    BluetoothDevice,
    ChannelEvent,
    PeerError,
    Refusal,
    SppPeer,
    WrittenLine,
)
from esp32tl.interfaces import NeverHappened
from esp32tl.interfaces.lines import LineEvent
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


class User(Pin):
    """The stand-in's user with Secure Simple Pairing, of `io_capability`: says yes to a number or
    a pairing when it `confirms`, types what `passkey` gives, and keeps what it was asked and
    shown, as `(what, number)`."""

    def __init__(
        self,
        io_capability: PairingDelegate.IoCapability,
        confirms: bool = True,
        passkey: Callable[[], Awaitable[int | None]] | None = None,
    ) -> None:
        super().__init__("1234")
        self.io_capability = io_capability
        self.confirms = confirms
        self.passkey = passkey
        self.seen: list[tuple[str, int | None]] = []

    async def confirm(self, auto: bool = False) -> bool:
        self.seen.append(("auto" if auto else "confirm", None))
        return True if auto else self.confirms

    async def compare_numbers(self, number: int, digits: int) -> bool:
        self.seen.append(("compare", number))
        return self.confirms

    async def get_number(self) -> int | None:
        number = None if self.passkey is None else await self.passkey()
        self.seen.append(("type", number))
        return number

    async def display_number(self, number: int, digits: int) -> None:
        self.seen.append(("show", number))


class Initiator:
    """The ESP32's side, as `bt_spp_initiator` drives it, on a Bumble host."""

    def __init__(
        self,
        air: Air,
        pin: str | None = "1234",
        ssp: bool = False,
        address: str = DEVICE,
        name: str = "esp32",
        user: User | None = None,
    ) -> None:
        self.air = air
        self.delegate = Pin(pin) if user is None else user
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
        ssp = ssp or isinstance(self.delegate, User)
        controller = Controller(name, self.air._link, address)
        self.controller = controller
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
        device.on("inquiry_complete", lambda: self.inquiry_complete.append(self.air._loop.now))
        device.on(device.EVENT_CONNECTION, self._on_connection)
        return device

    def _on_connection(self, connection: Connection) -> None:
        def fell(reason: int) -> None:
            self.fell.append((self.air._loop.now, reason))

        connection.on(connection.EVENT_DISCONNECTION, fell)

    def _on_result(
        self, address: hci.Address, class_of_device: int, eir: core.AdvertisingData, rssi: int
    ) -> None:
        name = eir.get(core.AdvertisingData.COMPLETE_LOCAL_NAME)
        assert name is None or isinstance(name, str)
        self.found.append((self.air._loop.now, str(address), class_of_device, name))
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
        dlc.sink = lambda data: self.received.append((self.air._loop.now, data))
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
    assert peer.events == [
        (Duration(1000 * MS), "out", b"hello\r"),
        (Duration(1050 * MS), "in", b"hello\r\n> "),
    ]
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
    assert kept.events == [(seconds(3), "in", b"ready\r")]
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
        if self.how == "rated":
            # a channel has no line rate: what a UART takes is refused
            return [(t, b"text", 9600)]  # type: ignore[list-item]
        if self.how == "empty":
            return [(t, b""), (t, b"done\r")]
        return super().receive(t, data)

    def quiet_until(self) -> int | None:
        if self.how == "float":
            return 1.5  # type: ignore[return-value]
        if self.how == "now":
            return 0
        return super().quiet_until()


def write_hello(air: Air, esp32: Initiator, name: str = "peer") -> None:
    async def write() -> None:
        dlc = await esp32.session(name)
        dlc.write(b"hello\r")
        await asyncio.sleep(1)

    air.run(write(), within=seconds(10))


@pytest.mark.parametrize(
    ("how", "message", "cause"),
    [
        (
            "past",
            "bench: source.receive sent b'too early' at -0.000000001 s, and the air is at 0 s:"
            " it cannot send into the past",
            None,
        ),
        ("raises", "bench: source.receive raised KeyError: 'model bug'", KeyError),
        ("pair", "bench: source.receive returned (0, 'text'), not a (t_ns, bytes) pair", None),
        (
            "rated",
            "bench: source.receive returned (0, b'text', 9600), not a (t_ns, bytes) pair",
            None,
        ),
    ],
)
def test_a_source_that_gets_its_answer_wrong_fails_the_air_naming_the_peer(
    air: Air, how: str, message: str, cause: type[Exception] | None
) -> None:
    SppPeer(air, name="bench", pin="1234", source=Faulty(how))
    with pytest.raises(AirError) as raised:
        write_hello(air, Initiator(air), "bench")
    assert str(raised.value) == message
    error = raised.value.__cause__
    assert isinstance(error, PeerError)
    if cause is not None:
        assert isinstance(error.__cause__, cause)


@pytest.mark.parametrize(
    ("how", "message"),
    [
        ("float", "bench: source.quiet_until returned 1.5, not nanoseconds"),
        (
            "now",
            "bench: source.quiet_until returned 0 s, not after the air's 0 s: what it sends by"
            " then belongs in the poll at 0 s",
        ),
    ],
)
def test_a_source_that_gets_its_quiet_until_wrong_fails_the_air(
    air: Air, how: str, message: str
) -> None:
    SppPeer(air, name="bench", pin="1234", source=Faulty(how))
    with pytest.raises(AirError) as raised:
        air.wait(seconds(1))
    assert str(raised.value) == message
    assert isinstance(raised.value.__cause__, PeerError)


def test_empty_answers_cross_nothing(air: Air) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Faulty("empty"))
    esp32 = Initiator(air)
    write_hello(air, esp32)
    assert peer.events == [(Duration(0), "out", b"hello\r"), (Duration(0), "in", b"done\r")]
    assert esp32.received == [(0, b"done\r")]


def test_two_runs_cross_the_channel_at_the_same_instants() -> None:
    def run() -> list[tuple[Duration, str, bytes]]:
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
        (
            {"service_name": ""},
            "SppPeer: service_name is '', not None or a string of 1 to 255 bytes",
        ),
        ({"service_name": 7}, "service_name is 7, not None or a string of 1 to 255 bytes"),
        (
            {"io_capability": "keyboard"},
            "SppPeer: io_capability is 'keyboard', not None or one of display_only,"
            " display_yes_no, keyboard_only, no_input_no_output",
        ),
        ({"confirms": 1}, "SppPeer: confirms is 1, not True or False"),
        (
            {"io_capability": "keyboard_only", "passkey": 1_000_000},
            "SppPeer: passkey is 1000000, not six digits, 0 to 999999",
        ),
        ({"io_capability": "keyboard_only", "passkey": True}, "passkey is True, not six digits"),
        (
            {"io_capability": "display_only", "passkey": 123456},
            "SppPeer: passkey is typed on a keyboard: give io_capability='keyboard_only'",
        ),
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
    assert peer.refused == [
        (Duration(0), "24:0A:C4:00:00:02: the channel is held by 24:0A:C4:00:00:01")
    ]
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
    assert peer.refused == [
        (Duration(0), "24:0A:C4:00:00:02: the channel is held by 24:0A:C4:00:00:01")
    ]
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
    assert peer.refused == [(Duration(0), "24:0A:C4:00:00:01: the link is not authenticated")]
    assert not peer.channel_open
    assert esp32.delegate.asked == 0


def test_the_channel_opens_when_the_device_authenticates_between_pn_and_sabm(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bluedroid's order as initiator with ESP_SPP_SEC_AUTHENTICATE: PN, then authentication,
    then SABM. The peer checks at SABM."""
    echo = Echo()
    peer = SppPeer(air, name="peer", pin="1234", source=echo)
    esp32 = Initiator(air)
    order: list[str] = []
    connect = rfcomm.DLC.connect

    def authenticate_first(dlc: rfcomm.DLC) -> None:
        order.append("pn answered")

        async def later() -> None:
            await dlc.multiplexer.l2cap_channel.connection.authenticate()
            order.append("authenticated")
            connect(dlc)

        asyncio.get_running_loop().create_task(later())

    monkeypatch.setattr(rfcomm.DLC, "connect", authenticate_first)

    async def scenario() -> None:
        connection = await esp32.device.connect(
            peer.device.public_address, transport=core.PhysicalTransport.BR_EDR
        )
        dlc = await esp32.open(connection, channel=CHANNEL)
        dlc.write(b"hello\r")
        await asyncio.sleep(1)

    air.run(scenario(), within=seconds(10))
    assert order == ["pn answered", "authenticated"]
    assert peer.refused == [] and peer.channel_open
    assert echo.heard == [(0, b"hello\r")]


def test_a_device_refused_at_sabm_may_authenticate_and_open_the_channel(air: Air) -> None:
    echo = Echo()
    peer = SppPeer(air, name="peer", pin="1234", source=echo)
    esp32 = Initiator(air)
    second = Initiator(air, address="24:0A:C4:00:00:02")

    async def scenario() -> None:
        await esp32.device.host.send_command(
            hci.HCI_Read_Stored_Link_Key_Command(bd_addr=hci.Address.ANY, read_all_flag=1)
        )
        connection = await esp32.device.connect(
            peer.device.public_address, transport=core.PhysicalTransport.BR_EDR
        )
        multiplexer = await rfcomm.Client(connection).start()
        with pytest.raises(core.ConnectionError):
            await multiplexer.open_dlc(CHANNEL)
        # The refusal let the channel go: another device is not told it is held.
        assert not peer.channel_open and peer._holder is None
        await connection.authenticate()
        dlc = await multiplexer.open_dlc(CHANNEL)
        dlc.write(b"hello\r")
        await asyncio.sleep(1)
        other = await second.device.connect(
            peer.device.public_address, transport=core.PhysicalTransport.BR_EDR
        )
        await other.authenticate()
        with pytest.raises(core.ConnectionError):
            await second.open(other, channel=CHANNEL)

    air.run(scenario(), within=seconds(10))
    assert peer.refused == [
        (Duration(0), "24:0A:C4:00:00:01: the link is not authenticated"),
        (Duration(1_000 * MS), "24:0A:C4:00:00:02: the channel is held by 24:0A:C4:00:00:01"),
    ]
    assert echo.heard == [(0, b"hello\r")]

    assert esp32.controller.unknown_commands
    for history in (peer.events, peer.refused, peer.pairings, esp32.controller.unknown_commands):
        assert history
        for record in history:
            timestamp = record.at if isinstance(record, ChannelEvent | Refusal) else record.t
            assert isinstance(timestamp, Duration)
            assert seconds(0) <= timestamp <= air.now
    edge = LineEvent(seconds(0), "ready", 1, "test", 1)
    assert peer.events[0].at - edge.t == seconds(0)
    assert peer.events[0].direction == "out" and peer.events[0].data == b"hello\r"
    assert peer.refused[0].at == seconds(0)
    assert peer.refused[0].why.endswith("the link is not authenticated")


def sdp_record(air: Air, esp32: Initiator, peer: BluetoothDevice) -> dict[int, Any]:
    """The peer's SPP record, attribute by attribute, as the device's SDP search finds it."""

    async def search() -> dict[int, Any]:
        connection = await esp32.device.connect(
            peer.device.public_address, transport=core.PhysicalTransport.BR_EDR
        )
        async with sdp.Client(connection) as client:
            (record,) = await client.search_attributes(
                [core.BT_SERIAL_PORT_SERVICE], [sdp.SDP_ALL_ATTRIBUTES_RANGE]
            )
        return {attribute.id: attribute.value for attribute in record}

    return air.run(search(), within=seconds(10))


@pytest.mark.parametrize(
    ("name", "found"), [(SERVICE_NAME, b"Serial Port"), ("Dev B", b"Dev B"), (None, None)]
)
def test_the_sdp_record_names_the_service(air: Air, name: str | None, found: bytes | None) -> None:
    """bt_spp_initiator prints the name with %s, and newlib's printf faults on the NULL of a
    record without one."""
    peer = SppPeer(air, name="peer", pin="1234", source=Echo(), service_name=name)
    record = sdp_record(air, Initiator(air), peer)
    if found is None:
        assert 0x0100 not in record and 0x0006 not in record
        return
    assert record[0x0100].value == found
    language = [element.value for element in record[0x0006].value]
    assert language == [0x656E, 106, 0x0100]


def test_the_peer_names_no_enhanced_retransmission_mode_among_its_l2cap_features(
    air: Air,
) -> None:
    """Bumble's RFCOMM server is in basic mode and disconnects a device that asks for ERTM, as
    Bluedroid does when the features name it."""
    peer = SppPeer(air, name="peer", pin="1234", source=Echo())
    features = peer.device.l2cap_channel_manager.extended_features
    ertm = l2cap.L2CAP_Information_Request.ExtendedFeatures.ENHANCED_RETRANSMISSION_MODE
    assert ertm not in features and features
    assert (
        BluetoothDevice(
            air, name="phone", address=PHONE
        ).device.l2cap_channel_manager.extended_features
        == features
    )


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
    assert peer.events == [(Duration(0), "out", b"hello\r")]


def test_after_the_device_closes_the_channel_the_peer_ends_nothing_itself(air: Air) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Echo())
    esp32 = Initiator(air)
    closed: list[int] = []

    async def close_and_wait() -> rfcomm.DLC:
        dlc = await esp32.session()
        dlc.on(dlc.EVENT_CLOSE, lambda: closed.append(air._loop.now))
        closed.append(air._loop.now)
        await dlc.disconnect()
        # Longer than a host's timers after a close (Bluedroid's 2 s and 4 s) and than the
        # supervision timeout: the peer has none of its own to fire.
        await asyncio.sleep(60)
        return dlc

    dlc = air.run(close_and_wait(), within=seconds(70))
    # Answered at once: the UA came at the DISC's instant.
    [disc, ua] = closed
    assert ua == disc and peer.is_closed()
    # The multiplexer, its L2CAP channel and the link are as the device left them: open.
    multiplexer = dlc.multiplexer
    assert multiplexer.state == rfcomm.Multiplexer.State.CONNECTED
    assert multiplexer.l2cap_channel.state == multiplexer.l2cap_channel.State.OPEN
    assert esp32.fell == []
    assert [str(c.peer_address) for c in peer.device.connections.values()] == [DEVICE + "/P"]


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
            ended.append(air._loop.now)

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


@pytest.mark.parametrize("start", [1_000, 3_000, 6_000, 10_000])
def test_a_page_ends_exactly_at_the_page_timeout_whatever_instant_it_starts_at(
    air: Air, start: int
) -> None:
    """6 s and 5.12 s are 11.120000000000001 s as floats, 1 s and 5.12 s are 6.12 s exactly: the
    page fails at the page timeout either way, not a nanosecond after it."""
    esp32 = Initiator(air)
    air.advance(ms(start))
    ended: list[int] = []

    async def page() -> None:
        try:
            await link_to(esp32)
        finally:
            ended.append(air._loop.now)

    task = air.start(page())
    air.advance(ms(start + 5_120) - Duration(1))
    assert ended == []
    # Past the end: an advance that stops at it would settle it a second time, and hide a page
    # ended at the next nanosecond.
    air.advance(ms(start + 6_000))
    assert ended == [(start + 5_120) * MS]
    assert isinstance(task.exception(), core.ConnectionError)


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
    assert peer.refused == [
        (Duration(5_000 * MS), "24:0A:C4:00:00:02: the channel is held by " + DEVICE)
    ]

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
        link.on(link.EVENT_DISCONNECTION, lambda reason: fell_at_peer.append(air._loop.now))
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
    assert peer.events == [(Duration(0), "out", b"PING\r")]
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
    assert peer.events == [(Duration(0), "out", b"PING\r")]
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

    peer = SppPeer(air, name="bench", pin="1234", source=Broken())
    with pytest.raises(AirError) as raised:
        peer.switch_off()
    assert str(raised.value) == "bench: source.supply raised KeyError: 'model bug'"


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
                pages.append(air._loop.now)

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
    target = air._link.find_classic_controller(hci.Address(SPEAKER + "/P"))
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
        ended.append(air._loop.now)

    air.run(second(), within=seconds(10))
    assert ended == [9_120 * MS]


def test_a_power_loss_ends_an_inquiry(air: Air) -> None:
    phone, _ = phone_and_speaker(air)
    completed: list[int] = []
    phone.device.on("inquiry_complete", lambda: completed.append(air._loop.now))
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
    phone.device.on("inquiry_complete", lambda: completed.append(air._loop.now))
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


@pytest.mark.parametrize("start", [1_000, 6_000])
def test_a_remote_name_request_ends_exactly_at_the_page_timeout_whatever_instant_it_starts_at(
    air: Air, start: int
) -> None:
    """From 1 s the end is 6.12 s exactly as a float; from 6 s it is 11.120000000000001 s. No
    peer on the air: a party's poll, scheduled at every instant, would run the loop once more
    there and hide an end a nanosecond late."""
    esp32 = Initiator(air)
    air.advance(ms(start))
    ended: list[int] = []

    async def request() -> str:
        try:
            return await esp32.device.request_remote_name(PEER)
        finally:
            ended.append(air._loop.now)

    task = air.start(request())
    air.advance(ms(start + 5_120) - Duration(1))
    assert ended == []
    # Past the end, as an advance that stops at it settles it twice.
    air.advance(ms(start + 6_000))
    assert ended == [(start + 5_120) * MS]
    error = task.exception()
    assert isinstance(error, hci.HCI_Error)
    assert error.error_code == hci.HCI_ErrorCode.PAGE_TIMEOUT_ERROR


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


# Page scan: a device whose page scan is off hears no page.


def scanning(air: Air, device: BluetoothDevice, *, inquiry: bool, page: bool) -> None:
    async def write() -> None:
        await device.device.set_discoverable(inquiry)
        await device.device.set_connectable(page)

    air.run(write(), within=seconds(0))


def test_a_page_to_a_device_with_page_scan_off_ends_at_the_page_timeout(air: Air) -> None:
    phone, speaker = phone_and_speaker(air)
    scanning(air, speaker, inquiry=True, page=False)
    task = air.start(page(phone, SPEAKER))
    # Unheard: the speaker holds no entry for the page.
    assert held_by(air, speaker) == []
    air.advance(ms(5_119))
    assert not task.done()
    air.advance(ms(5_120))
    error = task.exception()
    assert isinstance(error, core.ConnectionError)
    assert error.error_code == hci.HCI_ErrorCode.PAGE_TIMEOUT_ERROR
    assert held_by(air, phone) == []
    assert held_by(air, speaker) == []


def test_a_device_that_turns_page_scan_back_on_can_be_paged(air: Air) -> None:
    phone, speaker = phone_and_speaker(air)
    scanning(air, speaker, inquiry=True, page=False)
    with pytest.raises(core.ConnectionError):
        air.run(page(phone, SPEAKER), within=seconds(10))
    scanning(air, speaker, inquiry=True, page=True)
    air.run(page(phone, SPEAKER), within=seconds(0))
    assert air.now == ms(5_120)
    assert list(phone.device.connections) == list(speaker.device.connections) == [1]


def test_page_scan_turned_on_during_a_page_answers_only_the_next(air: Air) -> None:
    """Decided at the instant the page starts, as for a device that left."""
    phone, speaker = phone_and_speaker(air)
    scanning(air, speaker, inquiry=True, page=False)
    task = air.start(page(phone, SPEAKER))
    air.wait(seconds(1))
    scanning(air, speaker, inquiry=True, page=True)
    assert held_by(air, speaker) == []
    air.advance(ms(5_119))
    assert not task.done()
    air.advance(ms(5_120))
    error = task.exception()
    assert isinstance(error, core.ConnectionError)
    assert error.error_code == hci.HCI_ErrorCode.PAGE_TIMEOUT_ERROR
    air.run(page(phone, SPEAKER), within=seconds(0))
    assert air.now == ms(5_120)


def test_a_device_with_only_page_scan_on_is_paged_and_not_found(air: Air) -> None:
    _, speaker = phone_and_speaker(air)
    esp32 = Initiator(air)
    scanning(air, speaker, inquiry=False, page=True)
    air.run(one_inquiry(esp32), within=seconds(20))
    assert [address for _, address, _, _ in esp32.found] == [PHONE + "/P"]
    connect = esp32.device.connect(
        hci.Address(SPEAKER + "/P"), transport=core.PhysicalTransport.BR_EDR
    )
    air.run(connect, within=seconds(0))


def test_a_device_with_only_inquiry_scan_on_is_found_and_not_paged(air: Air) -> None:
    _, speaker = phone_and_speaker(air)
    esp32 = Initiator(air)
    scanning(air, speaker, inquiry=True, page=False)
    assert air.run(esp32.discover("speaker"), within=seconds(20)) == hci.Address(SPEAKER + "/P")
    connect = esp32.device.connect(
        hci.Address(SPEAKER + "/P"), transport=core.PhysicalTransport.BR_EDR
    )
    with pytest.raises(core.ConnectionError) as raised:
        air.run(connect, within=seconds(10))
    assert raised.value.error_code == hci.HCI_ErrorCode.PAGE_TIMEOUT_ERROR


def test_a_remote_name_request_to_a_device_with_page_scan_off_ends_at_the_page_timeout(
    air: Air,
) -> None:
    phone, speaker = phone_and_speaker(air)
    scanning(air, speaker, inquiry=True, page=False)
    with pytest.raises(hci.HCI_Error) as raised:
        air.run(phone.device.request_remote_name(hci.Address(SPEAKER + "/P")), within=seconds(10))
    assert raised.value.error_code == hci.HCI_ErrorCode.PAGE_TIMEOUT_ERROR
    assert air.now == ms(5_120)


def test_a_pager_does_not_get_the_name_of_a_target_that_did_not_hear_its_page(air: Air) -> None:
    """The pager's own page, unheard, is no link for the request to go over."""
    phone, speaker = phone_and_speaker(air)
    scanning(air, speaker, inquiry=True, page=False)
    task = air.start(page(phone, SPEAKER))
    with pytest.raises(hci.HCI_Error) as raised:
        air.run(phone.device.request_remote_name(hci.Address(SPEAKER + "/P")), within=seconds(10))
    assert raised.value.error_code == hci.HCI_ErrorCode.PAGE_TIMEOUT_ERROR
    assert air.now == ms(5_120)
    assert isinstance(task.exception(), core.ConnectionError)


def test_a_page_nobody_heard_carries_no_name_request_to_its_pager(
    air: Air,
) -> None:
    """The target's own page, which nobody heard, is no link for the request either."""
    phone, speaker = phone_and_speaker(air)
    scanning(air, phone, inquiry=True, page=False)
    scanning(air, speaker, inquiry=True, page=False)
    task = air.start(page(speaker, PHONE))
    with pytest.raises(hci.HCI_Error) as raised:
        air.run(phone.device.request_remote_name(hci.Address(SPEAKER + "/P")), within=seconds(10))
    assert raised.value.error_code == hci.HCI_ErrorCode.PAGE_TIMEOUT_ERROR
    assert air.now == ms(5_120)
    assert isinstance(task.exception(), core.ConnectionError)


@pytest.mark.parametrize("asker", ["central", "peripheral"])
def test_a_remote_name_request_over_a_link_needs_no_page_scan(air: Air, asker: str) -> None:
    phone, speaker = phone_and_speaker(air)
    air.run(page(phone, SPEAKER), within=seconds(0))
    scanning(air, phone, inquiry=False, page=False)
    scanning(air, speaker, inquiry=False, page=False)
    if asker == "central":
        name = phone.device.request_remote_name(hci.Address(SPEAKER + "/P"))
        assert air.run(name, within=seconds(0)) == "speaker"
    else:
        name = speaker.device.request_remote_name(hci.Address(PHONE + "/P"))
        assert air.run(name, within=seconds(0)) == "phone"
    assert air.quiet_until() is None


def test_a_target_that_heard_the_page_gets_the_pager_s_name_without_its_page_scan(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The page the target heard is the link the request goes over, before its host answers."""
    phone, speaker = phone_and_speaker(air)
    scanning(air, phone, inquiry=False, page=False)
    slow_to_accept(speaker, monkeypatch, 1)
    task = air.start(page(phone, SPEAKER))
    name = speaker.device.request_remote_name(hci.Address(PHONE + "/P"))
    assert air.run(name, within=seconds(0)) == "phone"
    assert not task.done()
    air.wait(seconds(1))
    assert task.done() and task.exception() is None


def test_a_target_that_turns_page_scan_off_after_hearing_the_page_still_gives_its_name(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    slow_to_accept(speaker, monkeypatch, 1)
    task = air.start(page(phone, SPEAKER))
    scanning(air, speaker, inquiry=False, page=False)
    name = phone.device.request_remote_name(hci.Address(SPEAKER + "/P"))
    assert air.run(name, within=seconds(0)) == "speaker"
    # The page it heard goes on to its answer.
    air.wait(seconds(1))
    assert task.done() and task.exception() is None


# A target whose host takes its time to answer a page.


ANSWERS = (hci.HCI_Accept_Connection_Request_Command, hci.HCI_Reject_Connection_Request_Command)


def changing_answers(
    target: BluetoothDevice,
    patch: pytest.MonkeyPatch,
    change: Callable[[hci.HCI_AsyncCommand], Awaitable[hci.HCI_AsyncCommand | None]],
    refusable: bool = False,
) -> list[int]:
    """The host of `target` sends what `change` makes of each answer to a page it hears, nothing
    for `None`. Its other commands go on meanwhile. An answer its controller refuses as owed for
    nothing, as one after its connection accept timeout, raises from Bumble's task and fails the
    air, unless `refusable`: then its time goes in the list this gives."""
    host = target.device.host
    send = host.send_async_command
    refused: list[int] = []

    async def answer(command: hci.HCI_AsyncCommand, *arguments: Any) -> Any:
        if isinstance(command, ANSWERS):
            changed = await change(command)
            if changed is None:
                return None
            command = changed
            try:
                return await send(command, *arguments)
            except hci.HCI_Error as error:
                unknown = hci.HCI_ErrorCode.UNKNOWN_CONNECTION_IDENTIFIER_ERROR
                if not refusable or error.error_code != unknown:
                    raise
                refused.append(round(asyncio.get_running_loop().time() * 1e9))
                return None
        return await send(command, *arguments)

    patch.setattr(host, "send_async_command", answer)
    return refused


def slow_to_accept(
    target: BluetoothDevice, patch: pytest.MonkeyPatch, *delays: float, refusable: bool = False
) -> list[int]:
    """The host of `target` answers each page it hears after the next of `delays` seconds, as
    firmware may, and 0.1 s after those run out. `refusable` and what it gives are
    `changing_answers`'s."""
    left = list(delays)

    async def later(command: hci.HCI_AsyncCommand) -> hci.HCI_AsyncCommand:
        await asyncio.sleep(left.pop(0) if left else 0.1)
        return command

    return changing_answers(target, patch, later, refusable)


# The longest connection accept timeout a host may write, 29 s (Vol 4, Part E, 7.3.14).
LONGEST_ACCEPT_TIMEOUT = 0xB540


def write_accept_timeout(air: Air, device: BluetoothDevice, slots: int) -> int:
    """The status the controller of `device` answers `Write_Connection_Accept_Timeout` with."""
    command = hci.HCI_Write_Connection_Accept_Timeout_Command(connection_accept_timeout=slots)
    result = air.run(device.device.host.send_sync_command_raw(command), within=seconds(0))
    parameters = result.return_parameters
    assert isinstance(parameters, hci.HCI_StatusReturnParameters)
    return parameters.status


def patient(air: Air, target: BluetoothDevice) -> None:
    """The host of `target` takes up to 29 s to answer a page, past the pager's page timeout,
    which ends a page heard before its answer comes."""
    assert write_accept_timeout(air, target, LONGEST_ACCEPT_TIMEOUT) == hci.HCI_ErrorCode.SUCCESS


def falls_of(air: Air, device: BluetoothDevice) -> list[tuple[int, int]]:
    """The links of `device` that fall from now on, as (t, reason)."""
    fell: list[tuple[int, int]] = []

    def connected(connection: Connection) -> None:
        def down(reason: int) -> None:
            fell.append((air._loop.now, reason))

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
    patient(air, speaker)
    # Later than the supervision timeout, so that the end goes before the host answers either way.
    slow_to_accept(speaker, monkeypatch, 25)
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
    air.advance(seconds(26))
    assert task.done()
    assert told == []
    assert not speaker.device.connections


def failures_of(air: Air, device: BluetoothDevice) -> list[tuple[int, int]]:
    """The connections `device` fails to make from now on, as (t, status)."""
    failed: list[tuple[int, int]] = []

    def failure(error: core.ConnectionError) -> None:
        assert error.error_code is not None
        failed.append((air._loop.now, error.error_code))

    device.device.on(device.device.EVENT_CONNECTION_FAILURE, failure)
    return failed


def test_a_host_that_answers_a_page_after_its_end_fell_gets_no_link(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    patient(air, speaker)
    slow_to_accept(speaker, monkeypatch, 25)
    failed = failures_of(air, speaker)
    task = air.start(page(phone, SPEAKER))
    air.advance(ms(50))
    speaker.out_of_range()
    air.advance(seconds(26))
    assert task.done()
    assert failed == [(25_000 * MS, hci.HCI_ErrorCode.CONNECTION_TIMEOUT_ERROR)]
    assert not speaker.device.connections
    speaker.in_range()
    # Nothing is left of it on either side.
    monkeypatch.undo()
    air.run(page(phone, SPEAKER), within=seconds(1))


def test_a_late_answer_does_not_connect_the_next_page(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    patient(air, speaker)
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
    patient(air, speaker)
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
    patient(air, speaker)
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
    # The event names only the address: a failure would read as the end of the working link.
    assert failed == []
    assert (list(phone.device.connections), list(speaker.device.connections)) == handles
    controller = air._link.find_classic_controller(speaker.device.public_address)
    assert controller is not None
    assert [c.handle for c in controller.classic_connections.values()] == [1]


def test_a_late_answer_does_not_complete_a_page_to_a_device_that_left(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    patient(air, speaker)
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
        ended.append(air._loop.now)

    air.run(page_back(), within=seconds(10))
    assert ended == [10_620 * MS]
    assert not speaker.device.connections


def test_a_page_tried_again_with_a_slow_host_does_not_fail_the_air(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With an accept timeout past the page's end, the first answer connects the second page: the
    second answer is the late one, and the link stays, with no failure told of it."""
    phone, speaker = phone_and_speaker(air)
    patient(air, speaker)
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
    assert failed == []
    assert list(phone.device.connections) == [1]
    assert list(speaker.device.connections) == [1]


def test_every_late_answer_is_counted_before_a_new_page_is_answered(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    patient(air, speaker)
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
    patient(air, speaker)
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
    patient(air, speaker)
    slow_to_accept(speaker, monkeypatch, 5.5)
    failed = failures_of(air, speaker)
    outgoing, incoming = crossing_pages(air, phone, speaker)
    air.advance(seconds(7))
    assert isinstance(outgoing.exception(), core.ConnectionError)
    assert isinstance(incoming.exception(), core.ConnectionError)
    # The speaker's page ended at its timeout and left the phone's page its entry, which the
    # answer finds: with an accept timeout past the page's end the link is made though both pages
    # reported an end.
    assert failed == [(5_120 * MS, PAGE_TIMEOUT)]
    assert list(phone.device.connections) == list(speaker.device.connections) == [1]


def test_a_target_that_paged_too_loses_contact_with_a_pager_that_leaves(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    patient(air, speaker)
    slow_to_accept(speaker, monkeypatch, 25)
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
    air.advance(seconds(27))
    assert failed == [
        (5_120 * MS, PAGE_TIMEOUT),
        (26_000 * MS, hci.HCI_ErrorCode.CONNECTION_TIMEOUT_ERROR),
    ]
    phone.in_range()
    monkeypatch.undo()
    air.run(page(phone, SPEAKER), within=seconds(1))


def test_a_page_this_side_failed_does_not_mend_the_link_to_a_pager_that_lost_power(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    patient(air, speaker)
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
    patient(air, speaker)
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
    """The peers the controller of `device` holds an entry for: a page of its own, a link or a
    page it heard."""
    controller = air._link.find_classic_controller(device.device.public_address)
    assert isinstance(controller, Controller)
    return controller.held()


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
    patient(air, speaker)
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
    patient(air, speaker)
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
    patient(air, speaker)
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
    patient(air, speaker)
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
    patient(air, speaker)
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
    air.advance(ms(12_120) - Duration(1))
    assert not again.done()
    air.advance(ms(13_000))
    assert isinstance(again.exception(), core.ConnectionError)
    assert failed == [(12_120 * MS, PAGE_TIMEOUT)]
    assert not speaker.device.connections


def test_a_page_the_controller_refused_leaves_no_timer_and_can_be_tried_again(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, _ = phone_and_speaker(air)
    failed = failures_of(air, phone)
    controller = air._link.find_classic_controller(phone.device.public_address)
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
    patient(air, speaker)
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
    patient(air, speaker)
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
    patient(air, speaker)
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
    patient(air, speaker)
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


# The connection accept timeout: a page the host does not answer in time.

ACCEPT_TIMEOUT = hci.HCI_ErrorCode.CONNECTION_ACCEPT_TIMEOUT_ERROR


def test_a_page_its_host_does_not_answer_ends_on_both_sides_at_the_accept_timeout(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    skipping_answers(speaker, monkeypatch, 1)
    paging = failures_of(air, phone)
    refused = failures_of(air, speaker)
    task = air.start(page(phone, SPEAKER))
    air.advance(ms(5_000) - Duration(1))
    assert not task.done()
    assert held_by(air, speaker) == [hci.Address(PHONE + "/P")]
    air.advance(ms(6_000))
    error = task.exception()
    assert isinstance(error, core.ConnectionError)
    assert error.error_code == ACCEPT_TIMEOUT
    # The target's 5 s come before the pager's page timeout, 5.12 s, and both hosts hear them.
    assert paging == [(5_000 * MS, ACCEPT_TIMEOUT)]
    assert refused == [(5_000 * MS, ACCEPT_TIMEOUT)]
    assert held_by(air, phone) == held_by(air, speaker) == []
    air.run(page(phone, SPEAKER), within=seconds(1))
    assert list(phone.device.connections) == list(speaker.device.connections) == [1]


def test_an_answer_after_the_accept_timeout_counts_for_nothing(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    refused = slow_to_accept(speaker, monkeypatch, 6, refusable=True)
    failed = failures_of(air, speaker)
    first = air.start(page(phone, SPEAKER))
    air.advance(seconds(7))
    assert isinstance(first.exception(), core.ConnectionError)
    # The host heard at 5 s that the page ended: its answer at 6 s answers nothing.
    assert failed == [(5_000 * MS, ACCEPT_TIMEOUT)]
    assert refused == [6_000 * MS]
    assert not phone.device.connections and not speaker.device.connections
    assert held_by(air, phone) == held_by(air, speaker) == []
    # The next page gets its own answer, 0.1 s on.
    second = air.start(page(phone, SPEAKER))
    air.advance(ms(7_100))
    assert second.done() and second.exception() is None
    assert list(phone.device.connections) == list(speaker.device.connections) == [1]


def test_a_crossing_page_its_host_does_not_answer_ends_and_the_next_page_is_not_refused(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The speaker's own page reports `PAGE_TIMEOUT`, and the phone's, which its host leaves
    unanswered, ends at 6 s: the speaker's next page no longer gets `CONNECTION_ALREADY_EXISTS`."""
    phone, speaker = phone_and_speaker(air)
    skipping_answers(speaker, monkeypatch, 1)
    phone_failed = failures_of(air, phone)
    speaker_failed = failures_of(air, speaker)
    outgoing, incoming = crossing_pages(air, phone, speaker)
    air.advance(seconds(7))
    assert isinstance(outgoing.exception(), core.ConnectionError)
    assert isinstance(incoming.exception(), core.ConnectionError)
    assert phone_failed == [(6_000 * MS, ACCEPT_TIMEOUT)]
    assert speaker_failed == [(5_120 * MS, PAGE_TIMEOUT), (6_000 * MS, ACCEPT_TIMEOUT)]
    assert held_by(air, phone) == held_by(air, speaker) == []
    air.run(page(speaker, PHONE), within=seconds(1))
    assert list(phone.device.connections) == list(speaker.device.connections) == [1]


def test_a_late_answer_the_host_never_gives_ends_at_its_accept_timeout(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A power cycle takes the page heard and leaves the answer owed, which ends at 5 s: it does not
    take the answer to the next page, whose pager would get `PAGE_TIMEOUT`."""
    phone, speaker = phone_and_speaker(air)
    skipping_answers(speaker, monkeypatch, 1)
    paging = failures_of(air, phone)
    failed = failures_of(air, speaker)
    first = air.start(page(phone, SPEAKER))
    air.advance(ms(50))
    speaker.switch_off()
    speaker.switch_on()
    air.advance(seconds(6))
    assert isinstance(first.exception(), core.ConnectionError)
    assert paging == [(5_120 * MS, PAGE_TIMEOUT)]
    assert failed == [(5_000 * MS, ACCEPT_TIMEOUT)]
    air.run(page(phone, SPEAKER), within=seconds(1))
    assert list(phone.device.connections) == list(speaker.device.connections) == [1]


def test_an_answer_given_in_time_leaves_no_timer_to_end_the_link(air: Air) -> None:
    phone, speaker = phone_and_speaker(air)
    failed = failures_of(air, speaker)
    fell = falls_of(air, speaker)
    air.run(page(phone, SPEAKER), within=seconds(1))
    # Past the 5 s at which the answer's accept timeout would have ended.
    air.advance(seconds(6))
    assert failed == [] and fell == []
    assert list(phone.device.connections) == list(speaker.device.connections) == [1]


def test_the_accept_timeout_a_host_writes_takes_effect_from_the_next_page(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    skipping_answers(speaker, monkeypatch, 2)
    paging = failures_of(air, phone)
    first = air.start(page(phone, SPEAKER))
    air.advance(seconds(1))
    # 1 s; the page heard already keeps its 5 s.
    assert write_accept_timeout(air, speaker, 0x0640) == hci.HCI_ErrorCode.SUCCESS
    air.advance(seconds(6))
    assert isinstance(first.exception(), core.ConnectionError)
    second = air.start(page(phone, SPEAKER))
    air.advance(seconds(8))
    assert isinstance(second.exception(), core.ConnectionError)
    assert paging == [(5_000 * MS, ACCEPT_TIMEOUT), (7_000 * MS, ACCEPT_TIMEOUT)]


def test_a_page_timeout_shorter_than_the_accept_timeout_ends_the_pager_s_side_first(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pager hears nothing of the target's timeout, since its page is over."""
    phone, speaker = phone_and_speaker(air)
    write_page_timeout(air, phone, 1600)
    skipping_answers(speaker, monkeypatch, 1)
    paging = failures_of(air, phone)
    refused = failures_of(air, speaker)
    task = air.start(page(phone, SPEAKER))
    air.advance(seconds(6))
    assert isinstance(task.exception(), core.ConnectionError)
    assert paging == [(1_000 * MS, PAGE_TIMEOUT)]
    assert refused == [(5_000 * MS, ACCEPT_TIMEOUT)]
    assert held_by(air, phone) == held_by(air, speaker) == []


@pytest.mark.parametrize("slots", [0, 0xB541, 0xFFFF])
def test_an_accept_timeout_out_of_its_range_is_refused_and_changes_nothing(
    air: Air, slots: int
) -> None:
    _, speaker = phone_and_speaker(air)
    status = write_accept_timeout(air, speaker, slots)
    assert status == hci.HCI_ErrorCode.INVALID_COMMAND_PARAMETERS_ERROR
    controller = air._link.find_classic_controller(speaker.device.public_address)
    assert isinstance(controller, Controller)
    assert controller.connection_accept_timeout == 0x1F40


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
    controller = air._link.find_classic_controller(pager.device.public_address)
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
    patient(air, speaker)
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


def create_connection(address: str) -> hci.HCI_Create_Connection_Command:
    """The page `Device.connect` starts, for a host that sends it itself."""
    return hci.HCI_Create_Connection_Command(
        bd_addr=hci.Address(address + "/P"),
        packet_type=0xCC18,
        page_scan_repetition_mode=hci.HCI_R2_PAGE_SCAN_REPETITION_MODE,
        clock_offset=0,
        allow_role_switch=1,
        reserved=0,
    )


def test_a_device_that_rejects_a_crossing_page_takes_the_role_switch_on_its_own(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The speaker's host sends its page itself: when `Device.connect` fails, Bumble's host
    forgets the page it accepted from that address too, and the role change finds no connection."""
    phone, speaker = phone_and_speaker(air)
    rejecting(phone)
    accepting_as_central_after(speaker, monkeypatch, 1)
    failed = failures_of(air, speaker)

    async def crossing() -> Connection:
        paged, _ = await asyncio.gather(
            page(phone, SPEAKER),
            speaker.device.host.send_async_command(create_connection(PHONE)),
        )
        assert isinstance(paged, Connection)
        return paged

    task = air.start(crossing())
    air.advance(ms(500))
    # The rejection took the speaker's page, and left the phone's own for the role switch.
    assert failed == [(0, LIMITED)]
    assert held_by(air, phone) == [hci.Address(SPEAKER + "/P")]
    air.advance(seconds(2))
    assert task.result().role == hci.Role.PERIPHERAL
    assert list(phone.device.connections) == list(speaker.device.connections) == [1]
    assert speaker.device.connections[1].role == hci.Role.CENTRAL


def test_a_link_made_after_a_rejection_of_a_crossing_page_switches_roles_as_asked(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`Device.connect` asks `Create_Connection` to allow a role switch."""
    phone, speaker = phone_and_speaker(air)
    rejecting(phone)
    slow_to_accept(speaker, monkeypatch, 1)
    task = air.start(paging_each_other(phone, speaker))
    air.advance(seconds(2))
    outgoing, _ = task.result()
    assert isinstance(outgoing, Connection)
    link = speaker.device.connections[1]
    assert (outgoing.role, link.role) == (hci.Role.CENTRAL, hci.Role.PERIPHERAL)
    air.run(link.switch_role(hci.Role.CENTRAL), within=seconds(1))
    assert (outgoing.role, link.role) == (hci.Role.PERIPHERAL, hci.Role.CENTRAL)


def test_a_refused_role_switch_during_this_sides_page_with_an_answer_never_given_leaves_nothing(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    patient(air, speaker)
    write_page_timeout(air, phone, 1600)
    # The phone's first page gets no answer; its second, the central's after 0.5 s.
    accepting_as_central_after(speaker, monkeypatch, 0, 0.5)
    outgoing, first = crossing_pages(air, phone, speaker)
    air.advance(ms(2_100))
    assert isinstance(first.exception(), core.ConnectionError)
    second = air.start(page(phone, SPEAKER))
    air.advance(ms(2_200))
    refusing_role_switch(air, phone, speaker)
    air.advance(seconds(3))
    assert isinstance(second.exception(), core.ConnectionError)
    # The speaker's own page ended with the refusal, as its host heard it.
    assert isinstance(outgoing.exception(), core.ConnectionError)
    assert held_by(air, speaker) == []
    monkeypatch.undo()
    air.run(page(speaker, PHONE), within=seconds(1))
    assert list(phone.device.connections) == list(speaker.device.connections) == [1]


def test_a_page_heard_during_this_sides_own_goes_when_that_page_makes_the_link(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    slow_to_accept(speaker, monkeypatch, 1)
    task = air.start(paging_each_other(phone, speaker))
    air.advance(seconds(2))
    assert task.done()
    # The phone accepted at once, which made the speaker's own page the link; the speaker's answer
    # at 1 s is late.
    assert list(phone.device.connections) == list(speaker.device.connections) == [1]
    air.run(speaker.device.connections[1].disconnect(), within=seconds(1))
    assert held_by(air, speaker) == held_by(air, phone) == []
    monkeypatch.undo()
    air.run(page(speaker, PHONE), within=seconds(1))
    assert list(phone.device.connections) == list(speaker.device.connections) == [1]


def roles_of(air: Air, device: BluetoothDevice) -> list[hci.Role]:
    """The role the controller of `device` has on each of its entries."""
    controller = air._link.find_classic_controller(device.device.public_address)
    assert controller is not None
    return [entry.role for entry in controller.classic_connections.values()]


@pytest.mark.parametrize(
    "central", [None, "phone", "speaker", "both"], ids=["plain", "phone", "speaker", "both"]
)
def test_two_pages_at_one_instant_both_accepted_make_one_link(
    air: Air, central: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both hosts accept at once, plainly or as the central: the first acceptance makes the link
    of its page, and the other, which comes after it at the same instant, is late. Which runs
    first is the loop's order, which differs between Python versions."""
    phone, speaker = phone_and_speaker(air)
    if central in ("phone", "both"):
        accepting_as_central_after(phone, monkeypatch, 0.001)
    if central in ("speaker", "both"):
        accepting_as_central_after(speaker, monkeypatch, 0.001)
    phone_failed = failures_of(air, phone)
    speaker_failed = failures_of(air, speaker)
    task = air.start(paging_each_other(phone, speaker))
    air.advance(seconds(1))
    assert task.done()
    assert list(phone.device.connections) == list(speaker.device.connections) == [1]
    roles = roles_of(air, phone) + roles_of(air, speaker)
    assert sorted(roles) == [hci.Role.CENTRAL, hci.Role.PERIPHERAL]
    # A host that alone asks for the central's role has it, whichever page makes the link: as the
    # target of the other's page, by the switch, or as the pager of its own.
    if central == "phone":
        assert roles_of(air, phone) == [hci.Role.CENTRAL]
    if central == "speaker":
        assert roles_of(air, speaker) == [hci.Role.CENTRAL]
    assert phone_failed == speaker_failed == []
    air.run(phone.device.connections[1].disconnect(), within=seconds(1))
    assert held_by(air, speaker) == held_by(air, phone) == []
    monkeypatch.undo()
    air.run(page(speaker, PHONE), within=seconds(1))
    assert list(phone.device.connections) == list(speaker.device.connections) == [1]


def test_a_late_answer_to_a_crossing_page_tells_no_failure_on_the_link_it_made(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    slow_to_accept(phone, monkeypatch, 2)
    slow_to_accept(speaker, monkeypatch, 3)
    phone_failed = failures_of(air, phone)
    speaker_failed = failures_of(air, speaker)
    task = air.start(paging_each_other(phone, speaker))
    air.advance(seconds(4))
    assert task.done()
    # The phone's answer at 2 s made the speaker's page the link; the speaker's at 3 s is late.
    assert speaker_failed == phone_failed == []
    assert list(phone.device.connections) == list(speaker.device.connections) == [1]
    assert roles_of(air, speaker) == [hci.Role.CENTRAL]
    assert roles_of(air, phone) == [hci.Role.PERIPHERAL]


@pytest.mark.parametrize("phone_central", [False, True], ids=["plain", "central"])
def test_a_crossing_acceptance_whose_role_switch_would_be_refused_leaves_the_other_its_link(
    air: Air, phone_central: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The speaker accepts first, as the central, and the phone's page allows no role switch: the
    phone's acceptance at the same instant makes the link of the speaker's page. A plain one keeps
    that page's roles, and the switch, which comes after it, is refused; one as the central switches
    them, and the refusal of the speaker's switch ends nothing."""
    phone, speaker = phone_and_speaker(air)
    accepting_as_central_after(speaker, monkeypatch, 0.001)
    if phone_central:
        accepting_as_central_after(phone, monkeypatch, 0.001)
    else:
        slow_to_accept(phone, monkeypatch, 0.001)
    phone_failed = failures_of(air, phone)
    speaker_failed = failures_of(air, speaker)
    task = air.start(paging_each_other(phone, speaker))
    air.advance(Duration(1))
    refusing_role_switch(air, phone, speaker)
    air.advance(seconds(8))
    assert all(isinstance(result, Connection) for result in task.result())
    assert phone_failed == speaker_failed == []
    assert list(phone.device.connections) == list(speaker.device.connections) == [1]
    central, peripheral = (phone, speaker) if phone_central else (speaker, phone)
    assert roles_of(air, central) == [hci.Role.CENTRAL]
    assert roles_of(air, peripheral) == [hci.Role.PERIPHERAL]


def test_two_crossing_acceptances_as_the_central_that_neither_page_allows_make_no_link(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither page allows the role switch both hosts ask for, so no role can be had."""
    phone, speaker = phone_and_speaker(air)
    accepting_as_central_after(speaker, monkeypatch, 0.001)
    accepting_as_central_after(phone, monkeypatch, 0.001)
    phone_failed = failures_of(air, phone)
    speaker_failed = failures_of(air, speaker)
    task = air.start(paging_each_other(phone, speaker))
    air.advance(Duration(1))
    refusing_role_switch(air, phone, speaker)
    refusing_role_switch(air, speaker, phone)
    air.advance(seconds(8))
    assert all(isinstance(result, core.ConnectionError) for result in task.result())
    refused = hci.HCI_ErrorCode.ROLE_CHANGE_NOT_ALLOWED_ERROR
    assert phone_failed == speaker_failed == [(1 * MS, refused)]
    assert held_by(air, phone) == held_by(air, speaker) == []
    monkeypatch.undo()
    air.run(page(phone, SPEAKER), within=seconds(1))


def test_an_answer_to_a_crossing_page_after_this_sides_own_ended_makes_the_link(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The phone's page ends at 1 s. At 2 s the speaker's host accepts it as the central, and the
    phone's host answers the speaker's page while that role switch is on its way: the phone has no
    page of its own left for the switch, so its answer makes the link of the speaker's page."""
    phone, speaker = phone_and_speaker(air)
    write_page_timeout(air, phone, 1600)
    skipping_answers(phone, monkeypatch, 1)
    skipping_answers(speaker, monkeypatch, 1)
    phone_failed = failures_of(air, phone)
    speaker_failed = failures_of(air, speaker)
    task = air.start(paging_each_other(phone, speaker))
    air.advance(seconds(2))
    monkeypatch.undo()
    accept = hci.HCI_Accept_Connection_Request_Command

    def both_answer() -> None:
        speaker_answer = accept(bd_addr=hci.Address(PHONE + "/P"), role=hci.Role.CENTRAL)
        phone_answer = accept(bd_addr=hci.Address(SPEAKER + "/P"), role=hci.Role.PERIPHERAL)
        air._loop.create_task(speaker.device.host.send_async_command(speaker_answer))
        air._loop.create_task(phone.device.host.send_async_command(phone_answer))

    air.call(both_answer)
    air.advance(seconds(3))
    assert task.done()
    assert phone_failed == [(1_000 * MS, PAGE_TIMEOUT)]
    assert speaker_failed == []
    assert list(phone.device.connections) == list(speaker.device.connections) == [1]
    assert roles_of(air, speaker) == [hci.Role.CENTRAL]
    assert roles_of(air, phone) == [hci.Role.PERIPHERAL]


def test_an_acceptance_as_the_central_lost_during_this_sides_own_page_ends_with_that_page(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The phone leaves before the switch reaches it, and the speaker's own page to it outlasts the
    entry's fall at 20.05 s: the host hears only that page's end, which ends both in its eyes."""
    phone, speaker = phone_and_speaker(air)
    write_page_timeout(air, speaker, 0xFFFF)
    skipping_answers(phone, monkeypatch, 1)
    accepting_as_central_after(speaker, monkeypatch, 1)
    failed = failures_of(air, speaker)
    task = air.start(paging_each_other(phone, speaker))
    air.advance(ms(50))
    phone.out_of_range()
    air.advance(seconds(25))
    assert held_by(air, speaker) == [hci.Address(PHONE + "/P")]
    assert failed == []
    air.advance(seconds(41))
    assert task.done()
    assert failed == [(0xFFFF * 625_000, PAGE_TIMEOUT)]
    assert held_by(air, speaker) == []


def test_an_acceptance_as_the_central_after_the_page_ended_is_refused(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    patient(air, speaker)
    accepting_as_central_after(speaker, monkeypatch, 6)
    paging = failures_of(air, phone)
    accepting = failures_of(air, speaker)
    task = air.start(page(phone, SPEAKER))
    air.advance(seconds(7))
    assert isinstance(task.exception(), core.ConnectionError)
    # The pager holds nothing to switch: it refuses, and no link comes of the acceptance.
    assert paging == [(5_120 * MS, PAGE_TIMEOUT)]
    assert accepting == [(6_000 * MS, hci.HCI_ErrorCode.ROLE_CHANGE_NOT_ALLOWED_ERROR)]
    assert held_by(air, phone) == held_by(air, speaker) == []
    assert not phone.device.connections
    assert not speaker.device.connections
    monkeypatch.undo()
    air.run(page(phone, SPEAKER), within=seconds(1))
    assert list(phone.device.connections) == list(speaker.device.connections) == [1]


@pytest.mark.parametrize("how", LEAVING)
def test_a_host_that_accepted_as_the_central_a_pager_that_left_hears_of_it_when_its_end_falls(
    air: Air, how: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    accepting_as_central_after(speaker, monkeypatch, 1)
    failed = failures_of(air, speaker)
    task = air.start(page(phone, SPEAKER))
    air.advance(ms(50))
    leave(phone, how)
    # The role switch at 1 s is lost on the way; the entry falls 20 s after the pager left.
    air.advance(ms(20_050) - Duration(1))
    assert failed == []
    air.advance(seconds(40))
    assert task.done()
    assert failed == [(20_050 * MS, hci.HCI_ErrorCode.CONNECTION_TIMEOUT_ERROR)]
    assert held_by(air, speaker) == []
    assert not speaker.device.connections
    come_back(phone, how)
    monkeypatch.undo()
    air.run(page(phone, SPEAKER), within=seconds(1))


def test_a_pager_that_leaves_after_its_page_ended_is_lost_to_the_target(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone, speaker = phone_and_speaker(air)
    patient(air, speaker)
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
    # The rejection took the speaker's page; the phone's own is still under way.
    assert held_by(air, phone) == [hci.Address(SPEAKER + "/P")]
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
    patient(air, speaker)
    write_page_timeout(air, phone, 0xFFFF)
    slow_to_accept(speaker, monkeypatch, 25)
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


def test_a_page_under_way_keeps_its_entry_after_a_rejection_of_a_crossing_page(air: Air) -> None:
    phone, speaker = phone_and_speaker(air)
    rejecting(speaker)
    failed = failures_of(air, speaker)
    outgoing, incoming = crossing_pages(air, phone, speaker)
    air.advance(ms(1_001))
    assert isinstance(incoming.exception(), core.ConnectionError)
    # The rejection took the phone's page, and left the speaker's own.
    assert held_by(air, speaker) == [hci.Address(PHONE + "/P")]

    with pytest.raises(hci.HCI_Error) as raised:
        air.run(page(speaker, PHONE), within=seconds(0))
    assert raised.value.error_code == hci.HCI_ErrorCode.CONNECTION_ALREADY_EXISTS_ERROR
    air.advance(seconds(6))
    assert isinstance(outgoing.exception(), core.ConnectionError)
    assert failed == [(5_120 * MS, PAGE_TIMEOUT)]
    # Its timer ended it: the next page runs its whole length.
    phone.out_of_range()
    again = air.start(page(speaker, PHONE))
    air.advance(ms(11_120) - Duration(1))
    assert not again.done()
    air.advance(ms(12_000))
    assert failed == [(5_120 * MS, PAGE_TIMEOUT), (11_120 * MS, PAGE_TIMEOUT)]


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
    target = air._link.find_classic_controller(speaker.device.public_address)
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


# Secure Simple Pairing with the peer.

PEER_AT = "00:00:00:00:5B:01"
SUCCESS = hci.HCI_ErrorCode.SUCCESS
AUTHENTICATION_FAILURE = hci.HCI_ErrorCode.AUTHENTICATION_FAILURE_ERROR


def authenticated_keys(device: Device) -> dict[str, bool]:
    """Whether each link key `device` keeps is an authenticated one, by the peer's address."""
    keystore = device.keystore
    assert isinstance(keystore, MemoryKeyStore)
    return {
        address: pairing.link_key.authenticated
        for address, pairing in keystore.all_keys.items()
        if pairing.link_key is not None
    }


def pair(air: Air, esp32: Initiator) -> Connection:
    """The stand-in connects to the peer and authenticates; what fails, fails here."""

    async def connect() -> Connection:
        connection = await esp32.device.connect(PEER, transport=core.PhysicalTransport.BR_EDR)
        await connection.authenticate()
        return connection

    return air.run(connect(), within=seconds(10))


def test_two_users_compare_one_number_and_the_key_is_authenticated(air: Air) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Echo(), io_capability="display_yes_no")
    user = User(PairingDelegate.DISPLAY_OUTPUT_AND_YES_NO_INPUT)
    esp32 = Initiator(air, user=user)
    pair(air, esp32)
    [mine] = esp32.controller.pairings
    [theirs] = peer.pairings
    assert user.seen == [("compare", mine.number)]
    assert (mine.method, mine.initiator, mine.status, mine.authenticated) == (
        "numeric_comparison",
        True,
        SUCCESS,
        True,
    )
    assert (theirs.method, theirs.initiator, theirs.number, theirs.peer) == (
        "numeric_comparison",
        False,
        mine.number,
        DEVICE,
    )
    assert theirs.succeeded and theirs.authenticated and peer.pairing is None
    assert authenticated_keys(esp32.device) == {PEER_AT + "/P": True}
    assert keys_of(esp32.device)[PEER_AT + "/P"] == keys_of(peer.device)[DEVICE + "/P"]
    # Only the initiator's host is told the authentication is complete.
    connection = next(iter(peer.device.connections.values()))
    assert not connection.authenticated
    device = hci.Address(DEVICE, hci.Address.PUBLIC_DEVICE_ADDRESS)
    assert peer.air.call(lambda: peer._controller.authenticated(device))  # type: ignore[union-attr]


@pytest.mark.parametrize("who", ["the peer", "the device"])
def test_a_number_a_user_says_no_to_fails_the_pairing_on_both_sides(air: Air, who: str) -> None:
    peer = SppPeer(
        air,
        name="peer",
        pin="1234",
        source=Echo(),
        io_capability="display_yes_no",
        confirms=who != "the peer",
    )
    user = User(PairingDelegate.DISPLAY_OUTPUT_AND_YES_NO_INPUT, confirms=who != "the device")
    esp32 = Initiator(air, user=user)
    with pytest.raises(hci.HCI_Error) as failed:
        pair(air, esp32)
    assert failed.value.error_code == AUTHENTICATION_FAILURE
    [mine] = esp32.controller.pairings
    [theirs] = peer.pairings
    assert (mine.method, mine.status) == (theirs.method, theirs.status)
    assert (mine.method, mine.status, mine.authenticated) == (
        "numeric_comparison",
        AUTHENTICATION_FAILURE,
        False,
    )
    assert keys_of(peer.device) == {} and keys_of(esp32.device) == {}


def test_the_device_types_the_passkey_the_peer_shows(air: Air) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Echo(), io_capability="display_only")

    async def read_it_off_the_peer() -> int | None:
        while peer.pairing is None or peer.pairing.number is None:
            await asyncio.sleep(0.001)
        return peer.pairing.number

    user = User(PairingDelegate.KEYBOARD_INPUT_ONLY, passkey=read_it_off_the_peer)
    esp32 = Initiator(air, user=user)
    pair(air, esp32)
    [mine] = esp32.controller.pairings
    [theirs] = peer.pairings
    assert user.seen == [("type", theirs.number)]
    assert theirs.number is not None and 0 <= theirs.number < 1_000_000
    assert (mine.method, mine.number, mine.status, mine.authenticated) == (
        "passkey_entry",
        theirs.number,
        SUCCESS,
        True,
    )
    assert authenticated_keys(esp32.device) == {PEER_AT + "/P": True}


def test_a_wrong_passkey_fails_the_pairing_on_both_sides_and_neither_waits(air: Air) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Echo(), io_capability="display_only")

    async def wrong() -> int:
        while peer.pairing is None or peer.pairing.number is None:
            await asyncio.sleep(0.001)
        return (peer.pairing.number + 1) % 1_000_000

    esp32 = Initiator(air, user=User(PairingDelegate.KEYBOARD_INPUT_ONLY, passkey=wrong))
    with pytest.raises(hci.HCI_Error):
        pair(air, esp32)
    [mine] = esp32.controller.pairings
    [theirs] = peer.pairings
    assert (mine.status, theirs.status) == (AUTHENTICATION_FAILURE, AUTHENTICATION_FAILURE)
    assert mine.t == theirs.t
    assert peer.pairing is None


@pytest.mark.parametrize(("typed", "status"), [(123456, SUCCESS), (None, AUTHENTICATION_FAILURE)])
def test_two_keyboards_each_type_the_passkey(air: Air, typed: int | None, status: int) -> None:
    peer = SppPeer(
        air,
        name="peer",
        pin="1234",
        source=Echo(),
        io_capability="keyboard_only",
        passkey=typed,
    )

    async def type_it() -> int:
        return 123456

    esp32 = Initiator(air, user=User(PairingDelegate.KEYBOARD_INPUT_ONLY, passkey=type_it))
    if status == SUCCESS:
        pair(air, esp32)
    else:
        with pytest.raises(hci.HCI_Error):
            pair(air, esp32)
    [theirs] = peer.pairings
    assert (theirs.method, theirs.number, theirs.status) == ("passkey_entry", typed, status)


NO_IO = PairingDelegate.NO_OUTPUT_NO_INPUT
YES_NO = PairingDelegate.DISPLAY_OUTPUT_AND_YES_NO_INPUT


@pytest.mark.parametrize(
    ("peer_has", "peer_confirms", "esp32_has", "esp32_confirms", "esp32_saw", "status"),
    [
        ("no_input_no_output", True, NO_IO, True, [("auto", None)], SUCCESS),
        # Against no input and no output, a side with a yes and a no is asked to allow it; the
        # side with none, as the peer here, allows it by itself.
        ("no_input_no_output", False, YES_NO, True, [("confirm", None)], SUCCESS),
        # Bumble asks a display with no yes or no too, which allows it whatever `confirms` says.
        ("display_only", False, NO_IO, True, [("auto", None)], SUCCESS),
        ("no_input_no_output", True, YES_NO, False, [("confirm", None)], AUTHENTICATION_FAILURE),
        ("display_yes_no", False, NO_IO, True, [("auto", None)], AUTHENTICATION_FAILURE),
    ],
)
def test_a_side_without_display_or_keyboard_pairs_by_just_works_unauthenticated(
    air: Air,
    peer_has: str,
    peer_confirms: bool,
    esp32_has: PairingDelegate.IoCapability,
    esp32_confirms: bool,
    esp32_saw: list[tuple[str, None]],
    status: int,
) -> None:
    peer = SppPeer(
        air,
        name="peer",
        pin="1234",
        source=Echo(),
        io_capability=peer_has,
        confirms=peer_confirms,
    )
    user = User(esp32_has, confirms=esp32_confirms)
    esp32 = Initiator(air, user=user)
    if status == SUCCESS:
        pair(air, esp32)
        assert authenticated_keys(esp32.device) == {PEER_AT + "/P": False}
    else:
        with pytest.raises(hci.HCI_Error):
            pair(air, esp32)
    assert user.seen == esp32_saw
    [mine] = esp32.controller.pairings
    [theirs] = peer.pairings
    assert (theirs.method, theirs.status, theirs.authenticated) == ("just_works", status, False)
    assert (mine.method, mine.number) == ("just_works", theirs.number)
    assert esp32.delegate.asked == 0


def test_a_peer_with_simple_pairing_pairs_with_its_pin_with_a_device_without(air: Air) -> None:
    echo = Echo()
    peer = SppPeer(air, name="peer", pin="1234", source=echo, io_capability="display_yes_no")
    esp32 = Initiator(air)

    async def write() -> None:
        dlc = await esp32.session()
        dlc.write(b"hello\r")
        await asyncio.sleep(1)

    air.run(write(), within=seconds(10))
    assert esp32.delegate.asked == 1
    assert echo.heard == [(0, b"hello\r")]
    [theirs] = peer.pairings
    assert (theirs.method, theirs.number, theirs.status) == ("legacy", None, SUCCESS)


def test_a_legacy_pairing_that_fails_is_in_the_history_on_both_sides(air: Air) -> None:
    peer = SppPeer(air, name="peer", pin="0000", source=Echo())
    esp32 = Initiator(air)
    with pytest.raises(hci.HCI_Error):
        pair(air, esp32)
    [mine] = esp32.controller.pairings
    [theirs] = peer.pairings
    assert (mine.method, mine.initiator, mine.status) == ("legacy", True, AUTHENTICATION_FAILURE)
    assert (theirs.method, theirs.initiator, theirs.status) == (
        "legacy",
        False,
        AUTHENTICATION_FAILURE,
    )


def test_each_pairing_starts_anew_and_two_runs_show_the_same_numbers() -> None:
    def run() -> list[tuple[Duration, str, int | None]]:
        air = Air()
        try:
            peer = SppPeer(
                air, name="peer", pin="1234", source=Echo(), io_capability="display_yes_no"
            )
            esp32 = Initiator(air, user=User(PairingDelegate.DISPLAY_OUTPUT_AND_YES_NO_INPUT))
            connection = pair(air, esp32)
            air.run(connection.disconnect(), within=seconds(1))
            peer.forget_keys()
            esp32.device.keystore = MemoryKeyStore()
            pair(air, esp32)
            return [(p.t, p.method, p.number) for p in peer.pairings]
        finally:
            air.close()

    first = run()
    assert len(first) == 2 and first[0][2] != first[1][2]
    assert first == run()


def test_channel_conditions_follow_state_and_name_timeouts(air: Air) -> None:
    from test_clock import Run

    peer = SppPeer(air, name="peer", pin="1234", source=Echo())
    esp32 = Initiator(air)
    opened, closed = peer.is_open, peer.is_closed
    run = Run()
    try:
        for step, is_open in enumerate((False, True, False)):
            assert opened() is is_open
            assert closed() is not is_open
            ready, pending = (opened, closed) if is_open else (closed, opened)
            assert run.clock.wait_until(ready, MS, repr(ready)) is True
            with pytest.raises(NeverHappened, match=re.escape(repr(pending))):
                run.clock.wait_until(pending, MS, repr(pending))
            for condition in (opened, closed):
                assert "peer's SPP channel is" in repr(condition)
                assert "00:00:00:00:5B:01" in repr(condition)
                with pytest.raises(TypeError, match="call the SPP condition"):
                    bool(condition)
            if step == 0:
                dlc = air.run(esp32.session(), within=seconds(10))
            elif step == 1:
                air.run(dlc.multiplexer.l2cap_channel.connection.disconnect(), within=seconds(1))
    finally:
        run.clock.stop()
        run.runner.join(1)


def test_next_write_keeps_append_boundary_and_does_not_consume(air: Air) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Echo(delay=0))
    esp32 = Initiator(air)
    dlc = air.run(esp32.session(), within=seconds(10))
    events = peer.events
    air.call(lambda: dlc.write(b"old"))
    assert events[0].data == b"old"
    first = peer.next_write()
    same = peer.next_write()
    filtered = peer.next_write(b"target")
    empty = peer.next_write(b"")
    split = peer.next_write(b"onetwo")
    answer_only = peer.next_write(b"\r\n> ")
    assert first() is None
    with pytest.raises(TypeError, match="call the SPP condition"):
        bool(first)
    # All writes and answers share one air instant, before any polling.
    for data in (b"one", b"two", b"a target here", b"later target"):
        air.call(partial(dlc.write, data))
    history = list(peer.events)
    assert {event.at for event in history} == {seconds(0)}
    for _ in range(2):
        assert first() is same() is empty() is history[2]
        assert first() == ChannelEvent(seconds(0), "out", b"one")
        assert filtered() == ChannelEvent(seconds(0), "out", b"a target here")
        assert split() is None  # Chunks are never joined.
        assert answer_only() is None  # Peer answers never match.
        assert peer.events == history
    assert repr(filtered) == (
        "the next write to peer's SPP channel (00:00:00:00:5B:01) containing b'target'"
    )
    assert repr(peer.is_open) == "peer's SPP channel is open (00:00:00:00:5B:01)"
    assert repr(peer.is_closed) == "peer's SPP channel is closed (00:00:00:00:5B:01)"
    with pytest.raises(TypeError, match="call the SPP condition"):
        bool(first)
    assert peer.next_write()() is None


def test_next_write_survives_channel_close_and_reopen(air: Air) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Echo())
    esp32 = Initiator(air)
    dlc = air.run(esp32.session(), within=seconds(10))
    air.call(lambda: dlc.write(b"before"))
    pending = peer.next_write()
    air.run(dlc.disconnect(), within=seconds(1))
    assert peer.is_closed()
    assert pending() is None
    connection = dlc.multiplexer.l2cap_channel.connection
    air.run(connection.disconnect(), within=seconds(1))
    dlc = air.run(
        esp32.open(air.run(esp32.connect(peer.device.public_address), within=seconds(1))),
        within=seconds(1),
    )
    air.call(lambda: dlc.write(b"after"))
    assert peer.is_open()
    assert pending() == ChannelEvent(seconds(0), "out", b"after")
    assert pending() is peer.events[-1]


@pytest.mark.parametrize("data", ["text", 1, bytearray(b"x")])
def test_next_write_refuses_non_bytes(air: Air, data: Any) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Echo())
    with pytest.raises(TypeError, match="not bytes or None"):
        peer.next_write(data)


def reopen(air: Air, esp32: Initiator, peer: SppPeer, dlc: rfcomm.DLC) -> rfcomm.DLC:
    """The device closes the channel and its link, and opens both again."""
    air.run(dlc.disconnect(), within=seconds(1))
    air.run(dlc.multiplexer.l2cap_channel.connection.disconnect(), within=seconds(1))
    return air.run(
        esp32.open(air.run(esp32.connect(peer.device.public_address), within=seconds(1))),
        within=seconds(1),
    )


def test_a_command_written_in_two_chunks_is_one_line_at_the_time_of_the_second(air: Air) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Echo())
    esp32 = Initiator(air)
    dlc = air.run(esp32.session(), within=seconds(10))
    air.call(lambda: dlc.write(b"AT+CON"))
    assert peer.written_lines() == []  # Not ended yet: not a line.
    air.run(asyncio.sleep(0.25), within=seconds(1))
    air.call(lambda: dlc.write(b"NECT\rAT\rAT+X"))
    # The peer's answer to the first chunk is in the history, between the two, and is no line.
    assert ChannelEvent(ms(50), "in", b"AT+CON\r\n> ") in peer.events
    assert peer.written_lines() == [
        WrittenLine(ms(250), b"AT+CONNECT", 0, True),
        WrittenLine(ms(250), b"AT", 0, True),
    ]


def test_a_partial_line_at_a_channel_close_is_kept_apart_from_the_next_channel(air: Air) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Echo())
    esp32 = Initiator(air)
    dlc = air.run(esp32.session(), within=seconds(10))
    air.call(lambda: dlc.write(b"AT\rAT+PART"))
    pending = peer.next_written_line(b"PART")
    dlc = reopen(air, esp32, peer, dlc)
    closed_at = peer.written_lines()[-1]
    assert closed_at == WrittenLine(seconds(0), b"AT+PART", 0, False)
    air.call(lambda: dlc.write(b"IAL\rAT+X"))
    assert peer.written_lines() == [
        WrittenLine(seconds(0), b"AT", 0, True),
        closed_at,
        WrittenLine(seconds(0), b"IAL", 1, True),
    ]
    assert pending() is None  # The line cut off is not complete, and no line joins it.
    dlc = reopen(air, esp32, peer, dlc)
    assert peer.written_lines()[-1] == WrittenLine(seconds(0), b"AT+X", 1, False)


def test_lines_are_cut_at_the_separator_the_test_gives_even_across_chunks(air: Air) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Echo())
    esp32 = Initiator(air)
    dlc = air.run(esp32.session(), within=seconds(10))
    air.call(lambda: dlc.write(b"one\r"))
    air.call(lambda: dlc.write(b"\n\r\ntwo\r"))
    air.call(lambda: dlc.write(b"\nthree"))
    assert [line.data for line in peer.written_lines(b"\r\n")] == [b"one", b"", b"two"]
    assert [line.data for line in peer.written_lines()] == [b"one", b"\n", b"\ntwo"]
    assert [line.data for line in peer.written_lines(b"\n")] == [b"one\r", b"\r", b"two\r"]
    # A separator of three bytes in three chunks, after a long line with none.
    for data in [b"x" * 50] * 20 + [b"<", b"=", b">", b"y<=>"]:
        air.call(partial(dlc.write, data))
    assert [line.data for line in peer.written_lines(b"<=>")] == [
        b"one\r\n\r\ntwo\r\nthree" + b"x" * 1000,
        b"y",
    ]


def test_next_written_line_keeps_the_line_boundary_and_does_not_consume(air: Air) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Echo(delay=0))
    esp32 = Initiator(air)
    dlc = air.run(esp32.session(), within=seconds(10))
    air.call(lambda: dlc.write(b"old\rbegun"))
    first = peer.next_written_line()
    same = peer.next_written_line()
    filtered = peer.next_written_line(b"target")
    old = peer.next_written_line(b"old")
    crlf = peer.next_written_line(b"two", separator=b"\r\n")
    assert first() is None
    with pytest.raises(TypeError, match="call the SPP condition"):
        bool(first)
    for data in (b" here\r", b"a tar", b"get\rtwo\r\n"):
        air.call(partial(dlc.write, data))
    lines = peer.written_lines()
    for _ in range(2):
        assert first() is same() is lines[1]
        assert first() == WrittenLine(seconds(0), b"begun here", 0, True)
        assert filtered() == WrittenLine(seconds(0), b"a target", 0, True)
        assert old() is None
        assert crlf() == WrittenLine(seconds(0), b"old\rbegun here\ra target\rtwo", 0, True)
    assert repr(filtered) == (
        "the next line ending in b'\\r' written to peer's SPP channel (00:00:00:00:5B:01)"
        " containing b'target'"
    )
    assert peer.next_written_line()() is None


def test_next_written_line_survives_channel_close_and_reopen(air: Air) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Echo())
    esp32 = Initiator(air)
    dlc = air.run(esp32.session(), within=seconds(10))
    pending = peer.next_written_line()
    air.call(lambda: dlc.write(b"cut"))
    dlc = reopen(air, esp32, peer, dlc)
    assert pending() is None
    air.call(lambda: dlc.write(b"after\r"))
    assert pending() == WrittenLine(seconds(0), b"after", 1, True)


@pytest.mark.parametrize(
    ("call", "error", "message"),
    [
        (lambda peer: peer.next_written_line("AT"), TypeError, "data is 'AT', not bytes or None"),
        (lambda peer: peer.written_lines("\r"), TypeError, "separator is '\\r', not bytes"),
        (lambda peer: peer.next_written_line(separator=b""), ValueError, "separator is empty"),
    ],
)
def test_written_lines_refuse_what_is_not_bytes(
    air: Air, call: Callable[[SppPeer], Any], error: type[Exception], message: str
) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Echo())
    with pytest.raises(error, match=re.escape(message)):
        call(peer)


# A long wait on a busy channel: as many writes as polls, one poll after each. A poll that looked
# again at everything since the condition was made would read N * (N + 1) / 2 records, 50 M.
N = 10_000


def test_a_next_write_poll_reads_only_the_events_since_the_last_one(air: Air) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Echo(delay=0))
    dlc = air.run(Initiator(air).session(), within=seconds(10))
    air.call(lambda: dlc.write(b"before"))
    peer._events = history = CountingList[ChannelEvent](peer.events)
    alarm = peer.next_write(b"ALARM")
    # What the device writes, as the peer records it, without the air for each of N writes.
    for _ in range(N):
        history.append(ChannelEvent(seconds(0), "out", b"temp 21.5\r"))
        assert alarm() is None
    assert history.reads == N
    air.call(lambda: dlc.write(b"ALARM\r"))
    found = alarm()
    assert found is not None and found.data == b"ALARM\r"
    assert alarm() is found and history.reads == N + 2
    assert peer.next_write(b"before")() is None


def test_a_next_written_line_poll_reads_only_the_lines_since_the_last_one(air: Air) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Echo(delay=0))
    dlc = air.run(Initiator(air).session(), within=seconds(10))
    # The lines the peer cuts at a carriage return, which the condition scans.
    peer.written_lines()
    peer._lines[b"\r"].lines = lines = CountingList[WrittenLine]()
    alarm = peer.next_written_line(b"ALARM")
    for _ in range(N):
        # What the device writes, as the peer records it, without the air for each of N writes.
        peer._events.append(ChannelEvent(seconds(0), "out", b"temp 21.5\r"))
        assert alarm() is None
    assert len(lines) == N and lines.reads == N
    air.call(lambda: dlc.write(b"ALA"))
    assert alarm() is None
    air.call(lambda: dlc.write(b"RM\r"))
    assert alarm() == WrittenLine(seconds(0), b"ALARM", 0, True)
    assert alarm() is alarm() and lines.reads == N + 3


# The peer hangs up: `disconnect()` ends its links while it stays on the air.

REMOTE_USER_TERMINATED = hci.HCI_ErrorCode.REMOTE_USER_TERMINATED_CONNECTION_ERROR


def test_disconnect_ends_the_link_and_the_peer_stays_on_the_air_with_its_key(air: Air) -> None:
    echo = Echo()
    peer = SppPeer(air, name="peer", pin="1234", source=echo)
    esp32 = Initiator(air)
    dlc = air.run(esp32.session(), within=seconds(10))
    assert dlc.multiplexer.l2cap_channel.connection.authenticated and peer.channel_open
    air.call(lambda: dlc.write(b"AT\rAT+PART"))
    start = air._loop.now

    ended = peer.disconnect()
    assert air.run(_until(ended), within=seconds(1)) is True
    # Ended at the instant of the call, and the device's host was told the remote user did it.
    assert esp32.fell == [(start, REMOTE_USER_TERMINATED)]
    assert not peer.channel_open
    assert peer.present
    assert not peer.device.connections and not esp32.device.connections
    # The line the end cut off is a line too, not complete.
    assert peer.written_lines()[-1] == WrittenLine(Duration(start), b"AT+PART", 0, False)

    # Connectable: paged again at once, it authenticates with the key it kept.
    dlc = air.run(esp32.open(air.run(esp32.connect(PEER), within=seconds(1))), within=seconds(1))
    air.call(lambda: dlc.write(b"PING\r"))
    assert esp32.delegate.asked == 1
    assert peer.channel_open and echo.heard[-1] == (air._loop.now, b"PING\r")


def test_a_plain_device_disconnects_too_and_names_its_condition_bluetooth(air: Air) -> None:
    phone = BluetoothDevice(air, name="phone", pin="1234")
    esp32 = Initiator(air)
    air.run(esp32.connect(PEER), within=seconds(1))
    ended = phone.disconnect()
    assert repr(ended) == "phone's disconnection (00:00:00:00:5B:01)"
    with pytest.raises(TypeError, match="the Bluetooth condition"):
        bool(ended)
    assert ended() is True
    assert esp32.fell == [(air._loop.now, REMOTE_USER_TERMINATED)]
    peer = SppPeer(air, name="peer", pin="1234", source=Echo(), address="00:00:00:00:5B:02")
    air.run(esp32.connect(peer.device.public_address), within=seconds(10))
    with pytest.raises(TypeError, match="the SPP condition"):
        bool(peer.disconnect())


def test_the_disconnect_condition_holds_after_the_device_connected_again(air: Air) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Echo())
    esp32 = Initiator(air)
    air.run(esp32.session(), within=seconds(10))
    ended = peer.disconnect()
    # The device pages the peer back before the test asks the condition.
    air.run(esp32.connect(PEER), within=seconds(1))
    assert len(peer.device.connections) == 1
    assert ended() is True


def test_disconnect_without_a_link_says_the_device_makes_it(air: Air) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Echo())
    with pytest.raises(
        PeerError, match=r"^peer: not connected: disconnect needs a link, which the device makes$"
    ):
        peer.disconnect()
    # Switched off, the peer's end of every link is gone at once: none left to end.
    esp32 = Initiator(air)
    air.run(link_to(esp32), within=seconds(1))
    peer.switch_off()
    with pytest.raises(PeerError, match="not connected"):
        peer.disconnect()


def test_disconnect_out_of_range_says_the_links_end_at_their_supervision_timeout(
    air: Air,
) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Echo())
    esp32 = Initiator(air)
    air.run(esp32.session(), within=seconds(10))
    peer.out_of_range()
    with pytest.raises(
        PeerError,
        match=re.escape(
            "peer: out of range: its links end at their supervision timeout, not with"
            " disconnect(); wait for it, and call in_range() for a new link"
        ),
    ):
        peer.disconnect()
    # Back in range, the link that lost contact still ends only at its timeout.
    peer.in_range()
    with pytest.raises(
        PeerError,
        match=re.escape(
            f"peer: lost contact with {DEVICE}: the link ends at its supervision timeout, not"
            " with disconnect(); wait for it"
        ),
    ):
        peer.disconnect()
    assert peer.device.connections and esp32.fell == []
    air.advance(seconds(20))
    assert esp32.fell == [(20_000 * MS, hci.HCI_ErrorCode.CONNECTION_TIMEOUT_ERROR)]
    with pytest.raises(PeerError, match="not connected"):
        peer.disconnect()


def test_disconnect_refuses_a_link_whose_other_side_left_the_air(air: Air) -> None:
    # The device's chip starts again, as at a board's reset, and its host is told nothing: the
    # peer, on the air all along, holds a link that lost contact.
    peer = SppPeer(air, name="peer", pin="1234", source=Echo())
    esp32 = Initiator(air)
    air.run(esp32.session(), within=seconds(10))
    air.call(esp32.controller._restart)
    assert peer.present
    with pytest.raises(
        PeerError,
        match=re.escape(
            f"peer: lost contact with {DEVICE}: the link ends at its supervision timeout, not"
            " with disconnect(); wait for it"
        ),
    ):
        peer.disconnect()
    air.advance(seconds(20))
    assert not peer.device.connections and not peer.channel_open


def test_what_bumble_raises_in_a_disconnect_is_raised_once_every_link_was_ended(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    peer = BluetoothDevice(air, name="peer", pin="1234")
    first = Initiator(air)
    second = Initiator(air, address="24:0A:C4:00:00:02", name="second")
    air.run(link_to(first), within=seconds(1))
    air.run(link_to(second), within=seconds(1))
    refusing, _ = peer.device.connections.values()
    assert str(refusing.peer_address) == DEVICE + "/P"

    async def refused(reason: int = 0) -> None:
        raise hci.HCI_Error(hci.HCI_ErrorCode.COMMAND_DISALLOWED_ERROR)  # type: ignore[no-untyped-call]

    monkeypatch.setattr(refusing, "disconnect", refused)
    ended = peer.disconnect()
    with pytest.raises(PeerError, match=r"^peer's disconnection \(00:00:00:00:5B:01\): HCI_Error"):
        ended()
    # The other link ended all the same.
    assert second.fell == [(air._loop.now, REMOTE_USER_TERMINATED)] and first.fell == []
    assert list(peer.device.connections.values()) == [refusing]


def test_a_disconnect_bumble_cancels_under_the_call_is_raised_from_the_condition(
    air: Air, monkeypatch: pytest.MonkeyPatch
) -> None:
    peer = SppPeer(air, name="peer", pin="1234", source=Echo())
    air.run(link_to(Initiator(air)), within=seconds(1))
    [connection] = peer.device.connections.values()

    async def cancelled(reason: int = 0) -> None:
        # As Bumble's `Device.disconnect` does at the host's `EVENT_FLUSH`.
        raise asyncio.CancelledError

    monkeypatch.setattr(connection, "disconnect", cancelled)
    ended = peer.disconnect()
    with pytest.raises(
        PeerError,
        match=re.escape("peer's disconnection (00:00:00:00:5B:01): Bumble cancelled it before"),
    ):
        ended()


async def _until(condition: Callable[[], Any]) -> Any:
    while not (found := condition()):
        await asyncio.sleep(0.001)
    return found
