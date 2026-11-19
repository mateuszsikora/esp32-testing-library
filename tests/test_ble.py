"""Bluetooth LE through the harness: ESP-IDF's Bluedroid GATT examples, unmodified, on the ESP32-S3
and the ESP32-C3 (`firmware/build.sh --chips`), against the test's `BleCentral` and `BlePeripheral`
on `bt.air`. `gatt_server` is found, connected to, read, written and notifies; `gatt_client` finds
the test's peripheral, connects, discovers, writes and is notified; `gatt_security_server` pairs
Just Works and keeps the bond across a reset of the firmware. The ESP32-C6 waits for esp32sim's
hooks of ESP-IDF v5.5.x (#229, G5). Needs `make firmware` (the `--chips` images)."""

from pathlib import Path

import pytest

from esp32tl import (
    BleCentral,
    BlePeripheral,
    GattCharacteristic,
    GattService,
    recording,
)
from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.interfaces.bluetooth import TAP_SOURCE, Bluetooth
from esp32tl.interfaces.console import Console
from esp32tl.units import ms, seconds

CHIPS = [("esp32s3", "esp32-s3-devkitc"), ("esp32c3", "esp32-c3-devkitm")]

# What gatt_server advertises and serves (examples/bluetooth/bluedroid/ble/gatt_server, v5.3.6):
# two services, 0x00FF and 0x00EE, each with one characteristic that reads 0xDEEDBEEF and
# notifies 15 bytes, 0 to 14, when a central subscribes.
SERVER_NAME = "ESP_GATTS_DEMO"
FIFTEEN = bytes(range(15))

# What gatt_client looks for and writes: a device called ESP_GATTS_DEMO with service 0x00FF and
# its notifying characteristic 0xFF01, to which it writes 35 bytes, 0 to 34.
THIRTY_FIVE = bytes(range(35))

# gatt_security_server: the name in its scan response, from a resolvable private address.
SECURE_NAME = "ESP_BLE_SECURITY"


def on_chip(chip: str, profile: str) -> type:
    def image(name: str) -> pytest.MarkDecorator:
        return pytest.mark.app_path(f"../firmware/{name}", build_dir=f"build-{chip}")

    @pytest.mark.board_profile(profile)
    class Tests:
        @image("gatt_server")
        def test_a_central_finds_connects_reads_writes_and_is_notified_by_gatt_server(
            self, board: Esp32Sim, bt: Bluetooth, console: Console, tmp_path: Path
        ) -> None:
            tap = bt.tap(tmp_path / "hci.btsnoop")
            central = BleCentral(bt.air, name="phone")
            board.power_on()
            seen = board.wait_until(central.next_advertisement(name=SERVER_NAME), within=seconds(5))
            assert seen.address == bt.address and seen.address_type == "public"
            assert seen.connectable and set(seen.service_uuids) == {"00FF", "00EE"}
            board.wait_until(central.connect(seen), within=seconds(2))
            console.expect_exact("GATTS_DEMO: Connected, conn_id 0, remote 00:00:00:00:5c:01")
            discovery = board.wait_until(central.discover(), within=seconds(2))
            assert {"00FF", "00EE"} <= {s.uuid for s in discovery.services}
            read = board.wait_until(central.read("FF01"), within=seconds(1))
            assert read.value == bytes.fromhex("deedbeef")
            # subscribing writes the CCCD, and gatt_server notifies at once
            notified = central.next_notification("FF01")
            board.wait_until(central.subscribe("FF01"), within=seconds(1))
            console.expect_exact("GATTS_DEMO: Notification enable", timeout=1)
            assert board.wait_until(notified, within=seconds(1)).value == FIFTEEN
            board.wait_until(central.write("FF01", b"\x01\x02\x03"), within=seconds(1))
            console.expect_exact("GATTS_DEMO: value len 3, value", timeout=1)
            console.expect_exact("GATTS_DEMO: 01 02 03", timeout=1)
            assert bt.unknown_commands() == []
            # the report and the run log name the LE events
            report = "\n".join(bt.report())
            assert "phone (00:00:00:00:5C:01), LE central:" in report
            assert f"advertising seen from {SERVER_NAME!r} {bt.address} (public)" in report
            assert (
                "notification of FF01 from 24:6F:28:00:11:24: 000102030405060708090a0b0c0d0e"
                in (report)
            )
            # the tap has the LE packets: Bluedroid's LE Set Advertising Enable and the
            # controller's LE Meta events, unchanged
            sent = [data for _, direction, data in tap.events if direction == "out"]
            received = [data for _, direction, data in tap.events if direction == "in"]
            assert bytes.fromhex("010a200101") in sent
            assert any(data[:2] == b"\x04\x3e" for data in received)
            # and the btsnoop file the backend writes of it carries them as they are: the H4 data
            # link (1002), and the LE Meta events among the records the host received
            data, _ = recording.dumps_btsnoop(
                tap.recording(board=board.profile.name, source=TAP_SOURCE, clock=board.clock)
            )
            assert data[:8] == b"btsnoop\0" and int.from_bytes(data[12:16], "big") == 1002
            records, at = [], 16
            while at < len(data):
                length = int.from_bytes(data[at + 4 : at + 8], "big")
                flags = int.from_bytes(data[at + 8 : at + 12], "big")
                records.append((flags & 1, data[at + 24 : at + 24 + length]))
                at += 24 + length
            assert len(records) == len(tap.events)
            assert any(received and packet[:2] == b"\x04\x3e" for received, packet in records)

        @image("gatt_server")
        def test_a_central_out_of_range_falls_at_its_timeout_and_reconnects(
            self, board: Esp32Sim, bt: Bluetooth, console: Console
        ) -> None:
            central = BleCentral(bt.air, name="phone")
            board.power_on()
            seen = board.wait_until(central.next_advertisement(name=SERVER_NAME), within=seconds(5))
            board.wait_until(central.connect(seen), within=seconds(2))
            # gatt_server asks for a supervision timeout of 4 s on connecting
            console.expect_exact(
                "Connection params update, status 0, conn_int 32, latency 0, timeout 400"
            )
            gone = central.next_disconnection()
            central.out_of_range()
            lost = board.now
            fell = board.wait_until(gone, within=seconds(5))
            assert fell.reason == 0x08 and fell.at - lost == seconds(4)
            console.expect_exact(
                "GATTS_DEMO: Disconnected, remote 00:00:00:00:5c:01, reason 0x08", timeout=1
            )
            # gatt_server advertises again; back in range, the central finds it and reconnects
            central.in_range()
            again = board.wait_until(
                central.next_advertisement(name=SERVER_NAME), within=seconds(1)
            )
            board.wait_until(central.connect(again), within=seconds(2))
            console.expect_exact("GATTS_DEMO: Connected, conn_id 0, remote 00:00:00:00:5c:01")
            assert [d.reason for d in central.disconnections] == [0x08]
            assert bt.unknown_commands() == []

        @image("gatt_client")
        def test_gatt_client_finds_the_peripheral_connects_discovers_writes_and_is_notified(
            self, board: Esp32Sim, bt: Bluetooth, console: Console
        ) -> None:
            peripheral = BlePeripheral(
                bt.air,
                name=SERVER_NAME,
                services=[
                    GattService(
                        "00FF",
                        [GattCharacteristic("FF01", b"\x11", ("read", "write", "notify"))],
                    )
                ],
            )
            write = peripheral.next_write("FF01")
            board.power_on()
            console.expect_exact("GATTC_DEMO: Device found ESP_GATTS_DEMO", timeout=5)
            board.wait_until(peripheral.is_connected, within=seconds(2))
            assert peripheral.peer == bt.address
            # it subscribes, then writes 35 bytes
            board.wait_until(peripheral.is_subscribed("FF01"), within=seconds(2))
            written = board.wait_until(write, within=seconds(1))
            assert written.value == THIRTY_FIVE and peripheral.value("FF01") == THIRTY_FIVE
            console.expect_exact("GATTC_DEMO: Characteristic write successfully", timeout=1)
            sent = board.wait_until(peripheral.notify("FF01", b"hello"), within=seconds(1))
            assert sent.value == b"hello"
            console.expect_exact("GATTC_DEMO: Notification received", timeout=1)
            console.expect_exact("GATTC_DEMO: 68 65 6c 6c 6f", timeout=1)
            assert bt.unknown_commands() == []
            # switched off, the peripheral is gone: the firmware's link falls at its timeout
            peripheral.switch_off()
            console.expect_exact(
                "GATTC_DEMO: Disconnected, remote 00:00:00:00:5b:02, reason 0x08", timeout=10
            )

        @image("gatt_security_server")
        def test_just_works_pairing_bonds_across_a_firmware_reset(
            self, board: Esp32Sim, bt: Bluetooth, console: Console
        ) -> None:
            central = BleCentral(bt.air, name="phone")
            board.power_on()
            first = board.wait_until(
                central.next_advertisement(name=SECURE_NAME), within=seconds(5)
            )
            # a resolvable private address, which the controller made from Bluedroid's IRK
            assert first.address_type == "random" and int(first.address[:2], 16) >> 6 == 0b01
            paired = central.next_pairing()
            board.wait_until(central.connect(first), within=seconds(2))
            # the firmware asks for security, and the central pairs: Just Works, bonded
            pairing = board.wait_until(paired, within=seconds(2))
            assert pairing.succeeded and pairing.bonded
            console.expect_exact("SEC_GATTS_DEMO: Pairing successfully, auth_mode", timeout=1)
            board.wait_until(central.is_encrypted, within=seconds(1))
            board.reset()
            # the reset took the controller off the air for a moment: the central's link falls at
            # its supervision timeout, Bumble's 720 ms
            fell = board.wait_until(central.next_disconnection(), within=seconds(2))
            assert fell.reason == 0x08
            again = board.wait_until(
                central.next_advertisement(name=SECURE_NAME), within=seconds(5)
            )
            assert again.address != first.address  # a new RPA, the same identity
            board.wait_until(central.connect(again), within=seconds(2))
            # the bond held: the link is encrypted with the kept keys, no pairing again
            board.wait_until(central.is_encrypted, within=seconds(2))
            console.expect_exact("SEC_GATTS_DEMO: Authentication complete", timeout=1)
            assert central.peer == bt.address  # resolved to the firmware's identity
            assert len(central.pairings) == 1
            assert bt.unknown_commands() == []
            board.wait(ms(100))

    Tests.__name__ = Tests.__qualname__ = f"TestBleOn{chip.upper()}"
    return Tests


TestBleOnESP32S3, TestBleOnESP32C3 = (on_chip(*chip) for chip in CHIPS)
