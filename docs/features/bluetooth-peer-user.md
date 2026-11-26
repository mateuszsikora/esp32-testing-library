# Bluetooth peer

Bluetooth Classic devices for the firmware to talk to, or to find: `SppPeer` can be discovered by
its name, pairs with a PIN or with Secure Simple Pairing, and offers the Serial Port Profile (SPP),
whose bytes a model in the test answers, on the board's clock; `BluetoothDevice` is the same device with no SPP. And Bluetooth LE
devices, for firmware on a chip whose controller is LE only, the ESP32-S3, the ESP32-C3 and the
ESP32-C6: `BlePeripheral` advertises and serves a GATT database the firmware connects to as a
central, and `BleCentral` scans for the firmware's advertising, connects to it and uses its GATT
database ([below](#bluetooth-le-bleperipheral-and-blecentral)). They run on
[Bumble](https://github.com/google/bumble)'s virtual link, in the harness's process.

On the emulator `board.bluetooth` puts them in front of the firmware: the firmware's Bluetooth host
talks to a controller in the harness, on the same air as the test's devices
([below](#in-front-of-the-firmware-boardbluetooth)). Without a board, a peer is used from Python on
an `Air` of its own, with another Bumble device in the ESP32's place, as esp32tl's own tests do
(`tests/test_spp_peer.py`).

## In front of the firmware: `board.bluetooth`

```python
import pytest

from esp32tl import SppPeer
from esp32tl.units import seconds


@pytest.mark.app_path("path/to/an/spp/initiator")
def test_the_firmware_sends_to_the_peer(board):
    source = Echo()  # a model, as in "The model that answers" below
    peer = SppPeer(board.bluetooth.air, name="ESP_SPP_ACCEPTOR", pin="1234", source=source)
    board.power_on()
    board.wait_until(peer.is_open, within=seconds(20))
    board.wait(seconds(1))
    assert source.heard
```

`board.bluetooth` is the chip's Bluetooth, asked for before `board.power_on()`; the `bt` fixture is
the same object, asked for at setup. Below, `bt` is short for either. Any look at it counts:
`board.bluetooth.use()` is the call to make for a test that wants Bluetooth on and no device
nearby, which gives the firmware the harness's controller with nothing on the air.

**A firmware that starts its controller needs it.** A test that never looks at `board.bluetooth`, of
the console, of Wi-Fi or of the SD card on such a firmware, gives the board no Bluetooth controller.
On a chip esp32sim models Bluetooth on (all but the ESP32-P4 and the ESP32-S31), and for a build
with the application's ELF, which the runner's `--no-bt` reads the function's address from, its
first call to `esp_bt_controller_init` then ends the run, and the test fails with `the firmware
started its Bluetooth controller (esp_bt_controller_init) by 0.117 s, and the test gave the board no
Bluetooth: look at board.bluetooth (or take the bt fixture) before board.power_on();
board.bluetooth.use() gives the firmware the harness's controller with no device on the air`, rather
than leave the firmware to crash or hang in a controller esp32sim does not model while the
test passes ([esp32sim
backend](esp32sim-backend-user.md#a-firmware-that-starts-its-controller-without-boardbluetooth)). A
firmware that links the controller and never starts it is not refused. Without the ELF the backend
passes no `--no-bt`, and the call runs ESP-IDF's controller as before.

From the first look on:

- **The firmware's controller is in the harness.** The run forwards every HCI packet the firmware's
  Bluetooth host sends (ESP-IDF's Bluedroid, through esp32sim's VHCI) to a controller on the air of
  the test's devices, and its answers back, on the board's clock: the backend passes the runner
  `--bt` with the application's ELF ([esp32sim backend](esp32sim-backend-user.md#bluetooth)). The
  firmware finds, pairs with and opens SPP to an `SppPeer` as it would a device on a real air: a
  firmware that runs its controller for Classic alone (mode 2, `CONFIG_BTDM_CTRL_MODE_BR_EDR_ONLY`)
  gets it ([runner](runner-user.md#give-the-guest-a-bluetooth-controller)). ESP-IDF's
  `bt_spp_initiator`, unmodified, does all of it against the peer of the example above, with
  `secure=True`, and streams to it at about 299 kbit/s of the board's time; with another PIN on the
  peer its authentication fails (`authentication failed, status:9`) and it opens nothing
  ([validation firmware](validation-firmware-user.md#bt_spp_initiator)). The
  [OBD-II reader example](examples-user.md#obd-ii-reader-over-bluetooth-classic) does the same with
  a PIN alone, then talks AT and OBD-II commands to a model of an ELM327 adapter.
- **Bluedroid's buffers in PSRAM.** A firmware built with
  `CONFIG_BT_ALLOCATION_FROM_SPIRAM_FIRST`, run on a board profile with `psram_mb = 4`, gets its HCI
  through as one without the option does: its Bluedroid takes the buffers of the packets it sends
  from PSRAM, and esp32sim at the pin takes a packet from PSRAM that is mapped and readable as well
  as from internal DRAM ([mikroverk/esp32sim#353](https://github.com/mikroverk/esp32sim/pull/353)).
  `bt_spp_initiator_psram` is the case: it pairs, opens SPP and streams as the plain image does
  ([validation firmware](validation-firmware-user.md#bt_spp_initiator_psram)). A packet that the
  chip's cache does not map to PSRAM is still refused (below).
- **`bt.air` is on the board's clock.** Its time is `board.now`; the test moves it only by letting
  the board run, with `board.wait(...)`, `board.wait_until(...)` or `console.expect(...)`.
  `bt.air.wait`, `bt.air.advance` and `bt.air.run` refuse, with `AirError`: `air.wait: this air is
  on the board's clock, which only the board's link moves: let the board run with board.wait() or
  board.wait_until()`.
- **What the test does on the air happens at the board's current instant:** a device made with
  `SppPeer(bt.air, ...)` or `BluetoothDevice(bt.air, ...)`, before power-on or after, `peer.rssi`
  set, `peer.switch_off()` and the other calls of [a peer that leaves](#a-peer-that-leaves),
  `peer.disconnect()` ([the peer hangs up](#the-peer-hangs-up-disconnect)), and a
  coroutine on a device's Bumble side started with `bt.air.start(...)`, which then runs as the board
  runs. `board.wait_until` can wait for what the devices do, as `peer.channel_open`.
- **The board's supply and resets reach the controller.** `board.power_off()` takes it off the air
  at once, as a device switched off; `board.power_on()` puts it back, with nothing kept. A reset of
  the chip, `board.reset()` or one of the firmware's own, ends what it had under way the same way,
  and so does the firmware's `HCI_Reset`, which Bluedroid sends whenever it starts
  ([below](#the-firmwares-controller)). The firmware's host is told nothing: it starts again too.
  What the controller had made for the old host and not yet sent is dropped. A peer that had a link
  to the board loses contact, and its end falls at its supervision timeout, 20 s unless the link's
  central wrote another (`bt_spp_initiator`'s Bluedroid writes 5 s); until then the firmware's page
  to it ends in `PAGE_TIMEOUT`, and `bt_spp_initiator` does not page again: seen on 2026-11-08 with
  `board.reset()` 10 s into the stream.
- **A deep sleep or a disabled controller does not take it off the air.** The runner sends nothing
  when the chip goes into a deep sleep or when the firmware calls `esp_bt_controller_disable` or
  `esp_bt_controller_deinit`, so the controller stays on the air with its scans and links until the
  next reset: a device can still find it, and a link to it does not lose contact. The wake from a
  deep sleep is a reset: there the controller restarts, and a peer's link loses contact.

On the serial backend the test skips where it first reaches `board.bluetooth`, at setup for the
`bt` fixture: `serial: bluetooth takes no model source on this backend`. `board.bluetooth` asked
for the first time after `board.power_on()` is refused: `bluetooth: the board is on already: give
an interface its source before board.power_on()`. With a build that holds no ELF of the
application, `board.bluetooth` raises `BackendError: bluetooth: BUILD holds no ELF of the
application, whose symbols the runner's --bt needs` where the test body reaches it, and the `bt`
fixture is an error at setup with the same message after `esp32sim: `
([where it fails](pytest-plugin-user.md#one-board-and-its-interfaces)).

| Name | What |
|---|---|
| `board.bluetooth.use()` | Before `board.power_on()`: gives the firmware the harness's controller, with no device on the air until the test makes one; returns the `Air`. Once is enough; any other look at the interface does the same |
| `board.bluetooth.air` | The `Air` of the board's devices, on its clock |
| `board.bluetooth.controller` | The firmware's controller, a `GuestController` at `24:6F:28:00:11:24`, of the profile's chip ([below](#the-firmwares-controller)) |
| `board.bluetooth.unknown_commands()` | The commands of the firmware's host the controller did not know, `UnknownCommand(t, op_code, name)` named tuples, with `t: Duration` |
| `board.bluetooth.pairings()` | The firmware's pairings that ended, each a `Pairing` ([Secure Simple Pairing](#secure-simple-pairing)) |
| `board.bluetooth.devices()` | The other devices made on the air, `(name, address, present)` each, in the order of their addresses; `present` is false for one switched off or out of range now |
| `board.bluetooth.packets_from_host`, `.bytes_from_host`, `.packets_to_host`, `.bytes_to_host` | What crossed the VHCI, each way |
| `board.bluetooth.tap(path)` | Before `board.power_on()`: the HCI between the firmware's host and its controller, every packet both ways, written at teardown as a btsnoop file at `path` and its header in `path` with `.json` added ([tapping the HCI](run-report-user.md#tapping-the-hci)); returns the `Tap` |

The test's report has what the Bluetooth did, after the Wi-Fi's lines: `bluetooth: the guest's
controller at 24:6F:28:00:11:24; N packets (B bytes) from its host, M packets (B bytes) to it`, then
`unknown commands (N):` with one line per command, its board time, Bumble's name for it and its op
code, as `0.300220000 s  HCI_TRUNCATED_PAGE_COMMAND (0x043f)`, and `devices:` with each device made
on the air, as `ESP_SPP_ACCEPTOR (00:00:00:00:5B:01)`, and `phone (00:00:00:00:5C:01, off the air)`
for one switched off or out of range. The `esp32sim_link` property has the same under `bluetooth`:
`address`, the four counts, `tap` (the btsnoop file's path, or `null`), `unknown_commands` (`t_ns`,
`op_code`, `name` each), `pairings` (`t_ns`, `peer`, `initiator`, `method`, `number`, `status`,
`authenticated` each; [Secure Simple Pairing](#secure-simple-pairing)), `devices` (`name`,
`address`, `present` each), `dropped_reports` (below) and `le_devices` (`name`, `address`,
`role`, and `events`, `t_ns` and `text` each; [Bluetooth LE](#what-the-report-and-the-run-log-say));
`null` without `board.bluetooth`. Between `unknown commands` and
`devices` the report lists the firmware's pairings under `pairings (N):`; after `devices`, each LE
device's events. After
`board.bluetooth.tap(path)` the first line ends with `; tapped into {path}`.

### The firmware's controller

It does what the devices' controllers do ([time on the air](#time-on-the-air),
[a peer that leaves](#a-peer-that-leaves)), and what Bluedroid needs of a controller. What it
reports is the chip's, which it takes from the board profile's `chip`:

- **The classic ESP32**: three pages of features, 0 to 2, as its controller has (Bluedroid stops on
  a fourth), page 0 with the BR/EDR features: encryption, role switch, the RSSI and extended
  inquiry results, Secure Simple Pairing, and none for LE, since the firmware runs its controller
  for Classic alone. TODO(owner): what the classic ESP32's controller reports on page 0.
- **The ESP32-S3 and the ESP32-C3**, whose controllers are Bluetooth LE only: what an
  ESP32-S3 (QFN56, revision v0.2) answered on 2026-11-19, with ESP-IDF v5.3.6's controller
  `51d9dfd` started as Bluedroid's BLE 4.2 examples start it
  ([`tools/hci_probe`](../stage0-findings.md#reproducing), its capture in
  `tools/hci_probe/esp32s3.txt`). Version 5.0 (HCI and LMP version 9, revision and subversion
  `0x0016`), Espressif's company identifier `0x02E5`; page 0 of the features alone, `LE Supported
  (Controller)` and `BR/EDR Not Supported`; LE features `0xFF`, encryption to the extended scanner
  filter policies, with no 2M or coded PHY and no extended advertising (a controller-only build,
  whose controller runs with `BLE_50:1`, reports `0x080001F9FF` instead); `LE_Read_Buffer_Size` 251
  bytes and 12 packets; `LE_Read_Supported_States` `ffffffffff030000`; a filter accept list of 12
  and a resolving list of 10; a maximum data length of 251 octets and 17040 µs each way; an
  advertising power of 9 dBm; its own `Read_Local_Supported_Commands`, and 5 command packets in
  each `Command_Complete` and `Command_Status`. A command that list does not name, as
  `Read_Buffer_Size`, `Read_Local_Extended_Features` or `Inquiry`, and any command it does not
  know get `Command_Complete` with `UNKNOWN_HCI_COMMAND_ERROR`, and are listed in
  `bt.unknown_commands()`; Bluedroid without Classic sends none of them. Three extended advertising
  commands it refused, `LE_Read_Maximum_Advertising_Data_Length`,
  `LE_Read_Number_of_Supported_Advertising_Sets` and `LE_Read_Transmit_Power`, get its answers,
  `COMMAND_DISALLOWED` each. The ESP32-C3's
  controller is built from the same sources; TODO(owner): measured on an ESP32-C3, which takes
  the S3's values meanwhile.
- **The ESP32-C6**, whose controller is Bluetooth LE only too, and another: what an ESP32-C6
  (ESP32-C6FH4, QFN32, revision v0.2) answered on 2026-11-26, with ESP-IDF v5.3.6's controller
  `18aafc6` (`tools/hci_probe/esp32c6.txt`); v5.5.4's `bc223ab`, the release esp32sim's C6 hooks
  take, and Bluedroid built for BLE 5.0 got the same answers. Version 6.0 (HCI and LMP version
  `0x0E`, revision and subversion 0); a resolving list of 4; its own
  `Read_Local_Supported_Commands`, and 1 command packet; the same page 0, states, filter accept
  list, data lengths and advertising power as the S3. Of its LE features, `0x1B900F017FFF`, the
  guest reports the S3's `0xFF`: the link has no 2M or coded PHY and no extended or periodic
  advertising. Of its `LE_Read_Buffer_Size`, 517 bytes and 10 packets, it reports 491 bytes, the
  most the link carries in one packet, and 10. Its list leaves out commands the chip answers:
  `HCI_Reset`, `Set_Event_Mask_Page_2`, `Write_Authenticated_Payload_Timeout` and
  `Read_Remote_Version_Information` are taken as the S3's are, but
  `Read_Authenticated_Payload_Timeout`, which the chip answered too, is unknown here, and
  `Write_Authenticated_Payload_Timeout` completes whatever the handle, where the chip refused an
  unknown one. `LE_Read_Maximum_Advertising_Data_Length` and
  `LE_Read_Number_of_Supported_Advertising_Sets` get `COMMAND_DISALLOWED`, and
  `LE_Read_Transmit_Power` -15 and 20 dBm, as from the chip. A command it does not know gets
  `Command_Status` with `UNKNOWN_HCI_COMMAND_ERROR` when it is an LE command (OGF `0x08`), as
  `LE_Read_Buffer_Size` v2 or `LE_Read_Local_P-256_Public_Key`, and `Command_Complete` otherwise,
  as `Inquiry`. The chip sends no LE event while the host's event mask leaves out LE Meta (bit
  61), as the specification's default does; Bluedroid sets it, and the guest sends them either
  way. No image runs Bluetooth on the C6 in esp32sim yet (#229, G5).

  On every LE-only chip, its address stays `24:6F:28:00:11:24`, where a real chip's is its base
  MAC plus 2.

  It also takes Espressif's two vendor commands for the flow control of advertising reports, which
  Bluedroid sends an LE controller right after `HCI_Reset`: `0xFD09` turns it on with a number of
  credits and a lost threshold, and completes; the controller then sends no more `LE Advertising
  Report` events than the credits and drops the rest; `0xFD0A` gives credits back, and gets no
  command acknowledgment, as Bluedroid expects of it. With duplicate filtering, an advertiser whose
  report was dropped is not taken for reported, and its next advertising event is reported once a
  credit has come back. Once the reports dropped since the last count reach the lost
  threshold, the next credit given back brings, before any report, Espressif's vendor LE Meta
  event `0xF0` with their number, four bytes little-endian, which takes a credit: the S3 sent
  `04 3E 05 F0 68 00 00 00` for 104 reports when 2 credits came back. The C6 sends one byte more
  after the number, of another value at each run, which Bluedroid does not read; the guest sends
  the S3's event on every chip. The report says
  `advertising reports dropped for want of the host's credits: N` when it dropped any, and
  `dropped_reports` counts them. Bluedroid asserts on a report it has no credit for, so a
  controller that ignored the credits would stop the firmware.
- ACL data of at most 491 bytes a packet to the firmware, the most a packet of esp32sim's VHCI holds
  (496 bytes, 5 of them the packet's header): the peer's data cut to fit, also to the firmware's
  `Host_Buffer_Size` when that is smaller, on BR/EDR and on LE. From the firmware, packets of at
  most what the controller's buffer sizes say: on the classic ESP32 `Read_Buffer_Size` says 491;
  on the ESP32-S3 and the ESP32-C3 `LE_Read_Buffer_Size` says 251, so Bluedroid sends its data in
  packets of 251 bytes, and on the ESP32-C6 it says 491. TODO(owner): what the classic ESP32's
  controller reports in `Read_Buffer_Size`.
- An answer to every command: one it does not know gets `UNKNOWN_HCI_COMMAND_ERROR` in the event the
  host waits for, `Command_Status` for a command answered so before it completes, `Command_Complete`
  for any other, and is listed in `bt.unknown_commands()`.
- Each answer `100 µs` after the instant the packet it answers was sent. The firmware reads it at a
  later poll of the VHCI: its controller task hands the host at most one packet a poll, and after a
  packet the next poll returns nothing, so an answer waits behind those before it
  ([protocol 5.22](../protocol.md#522-hci_tx-and-hci_rx-ext-34)).

## Putting a peer on the air

```python
import asyncio

from esp32tl import Air, Model, SppPeer
from esp32tl.units import ms, seconds


class Echo(Model):
    """Answers every line with the line and a prompt, 50 ms later."""

    def receive(self, at, data):
        return [(at + ms(50), data.rstrip(b"\r") + b"\r\n> ")]

    def poll(self, now):
        return ()

    def quiet_until(self):
        return None

    def latency(self):
        return ms(50)


air = Air()
peer = SppPeer(air, name="peer", pin="1234", source=Echo())
...
air.run(the_device_side(), within=seconds(10))
assert peer.events == [(seconds(1), "out", b"hello\r"), (ms(1050), "in", b"hello\r\n> ")]
air.close()
```

`SppPeer(air, *, name, pin, source, address="00:00:00:00:5B:01", channel=1, secure=True,
class_of_device=0x001F00, rssi=-50, service_name="Serial Port", io_capability=None,
confirms=True, passkey=None)`:

| Argument | What it is |
|---|---|
| `name` | The name the peer is discovered by, in its extended inquiry response: a string of 1 to 238 bytes in UTF-8, the longest the response holds whole |
| `pin` | The PIN it pairs with: a string of 1 to 16 bytes in UTF-8 |
| `source` | What answers the bytes on the SPP channel (below) |
| `address` | Its Bluetooth address, six pairs of hex digits separated by colons; no other device on the air may have it, so a second peer needs its own |
| `channel` | The RFCOMM channel of its SPP service, 1 to 30 |
| `secure` | Whether the channel opens only on a link the device has authenticated by the time it opens it |
| `class_of_device` | Its class of device, three octets, which an inquiry reports |
| `rssi` | The signal strength an inquiry hears it at, in dBm, -127 to 20, the range of the specification's inquiry results |
| `service_name` | The name of the SPP service in its SDP record, a string of 1 to 255 bytes in UTF-8, or `None` for a record without one |
| `io_capability` | `None`, the default, for a peer that pairs with its PIN only; or what its user has for Secure Simple Pairing: `display_only`, `display_yes_no`, `keyboard_only` or `no_input_no_output` ([below](#secure-simple-pairing)) |
| `confirms` | With Secure Simple Pairing, whether its user says yes to the number to compare, or to a pairing it is asked to allow; a `display_only` user has no yes or no, and allows every pairing whatever `confirms` says |
| `passkey` | With `keyboard_only`, the passkey its user types when asked, 0 to 999999; `None`, the default, for a user who types none, and the pairing fails |

From the moment it is made, at the air's current instant, and until the test takes it off the air
([below](#a-peer-that-leaves)), the peer is:

- **Discoverable and connectable.** An inquiry finds it with its address, its class of device,
  `0x001F00` (uncategorised) unless given another, its signal strength and its name.
- **Paired with its PIN.** It pairs the way Bluetooth 2.0 devices do, with a PIN (legacy
  pairing), unless it has an `io_capability` and the device has Secure Simple Pairing too
  ([below](#secure-simple-pairing)). A device that gives another PIN fails to authenticate and is
  disconnected. The peer keeps the key of each device it paired with until
  `peer.forget_keys()`, so a device that paired once authenticates later without a PIN; a device
  that kept a key the peer does not have fails to authenticate. A link the device authenticated can be
  encrypted.
- **Offering SPP.** Its SDP record names the Serial Port service (`0x1101`) on RFCOMM `channel`, and
  the service, `Serial Port` unless given another (ESP-IDF's `bt_spp_initiator` prints the name it
  finds with `%s`, and a record without one makes it fault). The channel opens to one device at a
  time, also when two ask at the same instant, and, with `secure`, only on a link that device has
  authenticated, with its PIN or its key, by the time it opens the channel: Bluedroid asks for the
  channel (RFCOMM's PN), then authenticates, then opens it (SABM), and the peer checks at the
  opening. Encryption is the device's choice: ESP-IDF's `bt_spp_initiator`, which asks for
  `ESP_SPP_SEC_AUTHENTICATE`, paired with the PIN and did not encrypt the link (seen on 2026-11-08,
  ESP-IDF v5.3.6). The peer does not start the pairing itself, so a device that opens the channel
  without pairing is refused, as is a second device that asks while the first holds it; one refused
  at the opening may authenticate and open it again. The device sees its channel refused (Bumble
  raises `ConnectionError` with `CONNECTION_REFUSED`), and `peer.refused` lists each refusal as a `Refusal(at,
  why)` named tuple, as `(seconds(0), "24:0A:C4:00:00:01: the link is not authenticated")` or `(seconds(0), "24:0A:C4:00:00:02:
  the channel is held by 24:0A:C4:00:00:01")`. With `secure=False`, any device opens it.
  `peer.channel_open` says whether a device holds it now; the channel closes when the device closes
  it or the link. The channel is RFCOMM in L2CAP's basic mode: the peer does not offer Enhanced
  Retransmission Mode, so a device that would ask for it stays in basic mode.

### Secure Simple Pairing

```python
peer = SppPeer(
    bt.air, name="ESP_SPP_ACCEPTOR", pin="1234", source=model, io_capability="no_input_no_output"
)
board.power_on()
board.wait_until(peer.is_open, within=seconds(5))
assert [p.method for p in peer.pairings] == ["just_works"]
```

With an `io_capability`, a peer or a [device](#other-devices) has Secure Simple Pairing, and a
device that has it too pairs with it that way; one that has not, such as a firmware built without
`CONFIG_BT_SSP_ENABLED`, pairs with the PIN. How the two pair follows from what each user has, as
Bluetooth Core 5.4 orders it (Vol 3, Part C, 5.2.2.6):

| Either side has | How they pair | The key |
|---|---|---|
| `no_input_no_output` | **Just Works.** Nobody compares or types: against a side with no input and no output, a `display_yes_no` side is asked to allow it, and says yes when it `confirms`; every other side allows it by itself, `display_only` too, which has no yes or no to say | unauthenticated |
| `keyboard_only` (and neither has `no_input_no_output`) | **Passkey Entry.** A side with a display shows a six-digit passkey, and the side with the keyboard types it; with two keyboards, each user types one, and they must be the same. A peer with a keyboard types its `passkey` | authenticated |
| `display_yes_no`, both | **Numeric Comparison.** Both show one six-digit number, and each user says yes when it `confirms` | authenticated |
| `display_only` and a display | **Numeric Comparison**, which a side with a display only confirms by itself; a `display_yes_no` side compares the number and says yes when it `confirms` | unauthenticated |

A user who says no, a passkey that differs from the other side's, and a keyboard with no `passkey`
fail the pairing on both sides with `AUTHENTICATION_FAILURE`; the link stays up, and what comes next
is the device's to choose. The numbers come from a generator seeded
with the device's address, so two runs show the same numbers; each pairing draws new ones.

- `peer.pairing` is the pairing under way, `None` while there is none: what the user's display
  shows is its `number`, as `peer.pairing.number`, once the pairing is that far. A test that plays
  the other side's user reads the passkey off it.
- `peer.pairings` is the history of the peer's pairings that ended, in order, each a `Pairing`:
  `t: Duration`, the air's time it ended at, the board's on `bt.air`; `peer`, the other side's address; `initiator`,
  whether the peer asked for it (the peer never does: always `False`); `method`, `legacy`,
  `just_works`, `numeric_comparison`, `passkey_entry`, or `simple_pairing` for one that ended
  before its method was known; `number`, the number shown or the passkey typed, `None` for none;
  `status`, an HCI error code, `0` for success, `succeeded` its test; and `authenticated`, whether
  the key it made is. A legacy pairing is in it too, with no number; one under way when its link
  falls ends with the reason the link fell for.

Against ESP-IDF's `bt_spp_initiator`, which has Secure Simple Pairing with a keyboard only (on its
console, `spp key …;`): a peer with `no_input_no_output` pairs by Just Works, which Bluedroid allows
by itself, and the firmware opens SPP and streams as with the PIN; a peer with a display shows a
passkey, the firmware prints `ESP_BT_GAP_KEY_REQ_EVT Please enter passkey!`, and, since the run
cannot type on its console, Bluedroid gives up 35 s later (`authentication failed, status:9`)
([validation firmware](validation-firmware-user.md#bt_spp_initiator)).

`bt.pairings()` is the same history on the firmware's side, the firmware's controller's, and the
test's report lists it under `pairings (N):`, one line each, as `1.383592800 s  with
00:00:00:00:5B:01, by just_works, 238680: succeeded`, `from` for one the other side asked for, and
the error's name and code, as `AUTHENTICATION_FAILURE_ERROR (0x05)`, for one that failed.

## Other devices

A device that offers no SPP, there to be found among others, or to be the wrong one:

```python
from esp32tl import BluetoothDevice

phone = BluetoothDevice(
    air, name="phone", class_of_device=0x5A020C, rssi=-70, address="00:00:00:00:5C:01"
)
peer.rssi = -80
```

`BluetoothDevice(air, *, name, pin=None, class_of_device=0x001F00, rssi=-50,
address="00:00:00:00:5B:01", io_capability=None, confirms=True, passkey=None)` takes the arguments
of the same name in the table above, and pairs as a peer does, with `pairing` and `pairings`; two devices
on one air need their own addresses. It has what `SppPeer` has apart from the channel and the source:
it leaves and returns as [below](#a-peer-that-leaves), and keeps its keys. It offers no service: an
SDP search for the Serial Port service finds nothing, and RFCOMM cannot be opened to it. Without a
`pin`, a device that tries to pair with it fails with `PIN_OR_KEY_MISSING`.

An inquiry finds every device on the air, each with its own class and signal strength, in the order
of their addresses. `rssi` can be set at any time, on either class, and the next inquiry gets it. It
reaches the device in the RSSI and extended inquiry modes (Bumble asks for extended); the plain
`Inquiry_Result` has no such field. Nothing else depends on it: a device at -127 dBm connects as one
at 20. A device that has left hears nobody in its own inquiry, which still takes its whole length.

A peer or a device accepts every page: nothing in their API rejects one. Bumble's host rejects what
it does not expect once told to, with `peer.device.classic_accept_any = False`, for the reason
`CONNECTION_REJECTED_DUE_TO_LIMITED_RESOURCES`. The device that paged gets that reason at once, in
`error_code`, rather than `PAGE_TIMEOUT` at the page timeout; both can connect afterwards.

A page whose host does not answer within its connection accept timeout, 5 s from the
`Connection_Request` unless that host wrote another with `Write_Connection_Accept_Timeout`, 0x0001
to 0xB540 slots (up to 29 s), ends on both sides: both hosts get `Connection_Complete` with
`CONNECTION_ACCEPT_TIMEOUT_EXCEEDED`, the pager before its own page timeout of 5.12 s, and both can
page again at once. The target's host does not get it while its own page to the pager is still under
way, as when the two pages cross: the event names only the address, and would end that page in its
eyes. An answer after that is refused with `UNKNOWN_CONNECTION_IDENTIFIER`, unless the
pager has paged again meanwhile: it names only the address, so it is then the answer to that page. A
peer's and a device's host answers at the instant it hears, so only firmware behind the `bt`
fixture, or a host a test slows, meets this. With a connection accept timeout longer than the
pager's page timeout the pager reports `PAGE_TIMEOUT` first, and a host that accepts after that
still makes a link.

Two devices that page each other at the same instant get one link, with one handle on each side and
one central: the page whose target's host answers first makes it, and its pager is the central,
unless that host asked for the central's role. When it asked for it and the page it answered allows
no role switch, the other host's answer makes the link instead, of the asking device's own page,
with the roles the other host asked for. When both ask for the central's role and neither page
allows a switch, no role can be had: both get `Connection_Complete` with `ROLE_CHANGE_NOT_ALLOWED`,
and no link. The answer that makes no link changes nothing and reports no failure, as does an answer
to an earlier page that comes when the two already have a link. Bumble's host on a device that heard
the other's page during its own `connect` may give `connection.role` as the peripheral where the
link has it as the central. A host that accepts a page as the central after the page ended gets
`Connection_Complete` with `ROLE_CHANGE_NOT_ALLOWED`, and no link; one that accepts as the central a
device that has left gets it with `CONNECTION_TIMEOUT` when its end falls, 20 s after the device
left.

Nor does their API turn page scan off. Through Bumble, `await peer.device.set_connectable(False)`
does, inside `air.run`: the device no longer hears a page, and a device that pages it gets
`PAGE_TIMEOUT` at the page timeout, 5.12 s unless its host wrote another; a remote name request
ends the same way, unless the two already have a link. Page scan counts when the page starts: a
device that turns it back on with `set_connectable(True)` answers the next page, not the one under
way. `set_discoverable(False)` turns inquiry scan off alone: the device is not found, and can still
be paged.

## A peer that leaves

```python
peer.switch_off()  # it loses its power
peer.switch_on()
peer.out_of_range()  # it runs on, out of reach
peer.in_range()
peer.present  # on and in range
peer.forget_keys()  # it loses the link keys it keeps
peer.disconnect()  # it ends its links and stays on the air
```

Each call takes effect at the air's time, from the test or from a coroutine on the air. Power and
range are apart: a peer switched off and out of range is `present` again once both are undone.
Saying what is already so changes nothing. `disconnect()` is not a way of leaving: the peer hangs
up and stays on the air ([the peer hangs up](#the-peer-hangs-up-disconnect)).

To a device on the air both ways of leaving look the same:

- **Nothing is sent.** No `LMP_detach`: the device learns nothing at the instant the peer leaves,
  and what it writes meanwhile is gone.
- **An inquiry does not find it,** until it is back. An inquiry is answered at the instant it
  starts, so a peer that returns during one answers the next.
- **A page ends in `PAGE_TIMEOUT`** at the page timeout, 5.12 s unless the device's host wrote
  another with `Write_Page_Timeout`. So does a page to an address no device has. The page is
  answered or not at the instant it starts: a peer that returns before the timeout does not answer
  that page, only the next one. A real controller keeps paging until the timeout, so it may
  connect where this one times out.
- **A remote name request ends in `PAGE_TIMEOUT`** at the same timeout, with Bumble's
  `HCI_Error` on the device's side, as it needs a page too.
- **The link falls with `CONNECTION_TIMEOUT`** at the supervision timeout after the peer left, on
  each end still holding it: 20 s, unless the link's central wrote another with
  `Write_Link_Supervision_Timeout`, or 0 for a link that never falls. A peripheral that writes it
  gets `COMMAND_DISALLOWED`. An authentication under way ends with the same status when its link
  falls.
- **Coming back does not revive the link.** A peer back before the timeout can be discovered, and
  another device can connect to it, but nothing crosses the old link, which still falls at its
  timeout. Until it does, the device that holds it gets `CONNECTION_ALREADY_EXISTS` when it pages
  the peer. A device that ends the link itself first gets `PAGE_TIMEOUT` until the peer's end has
  fallen too.

They differ only at the peer:

| | `switch_off` | `out_of_range` |
|---|---|---|
| A source that has `supply(on)` | Gets `supply(False)`, and `supply(True)` from `switch_on`, at the air's time; what it had queued is dropped, and not counted in `lost` | Is not told; runs on, and what it sends counts in `lost` |
| Any other source | Runs on, and what it sends counts in `lost` | The same |
| The peer's end of its links | Gone at once: `channel_open` is false | Falls at its timeout: `channel_open` stays true until then, and a second device is refused the channel until then |
| What it has under way itself, through `peer.device` | A page or a remote name request ends at once in `PAGE_TIMEOUT`, an authentication in `CONNECTION_TIMEOUT`, an inquiry with its completion, and that inquiry does not restart by itself | Each runs to its own timeout, hearing nobody |

Link keys stay either way, and `forget_keys()` is the only way to lose them; it applies from the
peer's next link on. No specification or vendor document says what a given device keeps after
losing power: that it keeps its keys is this model's assumption, as are the sources' `supply` and
what a power loss does to what a source had queued. A test about a device that loses them calls
`forget_keys()`.

All of this has been exercised on an `Air` with a Bumble device in the ESP32's place, and through
the `bt` fixture's controller with a Bumble host in Bluedroid's: no firmware has seen a peer leave
yet. What ESP-IDF does with these events is not known.

## When the channel closes

The peer answers at once and starts nothing itself. At the device's RFCOMM DISC for the channel it
answers UA and the channel is closed: `channel_open` is false, `is_closed` holds, and a line the
close cut off is a `WrittenLine` with `complete=False`. It never sends DISC itself, never closes its
RFCOMM multiplexer or the L2CAP channel under it, and, while it stays on the air
([above](#a-peer-that-leaves)), never ends the ACL link by itself: it answers each of these when the
device's host asks, and that is all. A real SPP adapter may end the link itself sooner; this one
does only when the test calls [`disconnect()`](#the-peer-hangs-up-disconnect)
([decision](../decisions/harness.md#the-spp-peer-ends-nothing-itself-after-the-device-closes-the-channel)).

Measured on `examples/obd_reader`, built with ESP-IDF v5.3.6, from the run's HCI tap
(`board.bluetooth.tap(path)`): the reader ends a session it has nothing to read on with
`esp_spp_disconnect`, then pauses 5 s before its next search. Times are from the console line it
prints just before the call; the guest's controller gives every answer 100 µs after the packet it
answers.

| After | The firmware's host sends | The peer answers |
|---|---|---|
| 0.2 ms | RFCOMM DISC on the channel's DLCI | UA: the channel is closed. `ESP_SPP_CLOSE_EVT` reaches the firmware at about 20 ms |
| 20 ms | RFCOMM DISC on DLCI 0, the multiplexer | UA |
| 40 ms | L2CAP Disconnection Request, RFCOMM's channel | Disconnection Response |
| 4.06 s | `HCI_Disconnect`, reason `0x13` | `Disconnection_Complete`, the link's end |

Bluedroid closes the multiplexer without waiting, 20 ms after the UA, because its own DISC closed
the last DLC; had the other side's, it would wait `RFC_MCB_RELEASE_INACT_TIMER`, 2 s, which never
happens here, as the peer sends no DISC (`stack/rfcomm/rfc_utils.c:328-336`,
`stack/rfcomm/include/rfc_int.h:238`, under `components/bt/host/bluedroid/` of ESP-IDF v5.3.6).
With no L2CAP channel left on the link, the firmware's host ends it after
`L2CAP_LINK_INACTIVITY_TOUT`, 4 s (`common/include/common/bt_target.h:1551`): the 4.02 s from the
L2CAP close to `HCI_Disconnect`.

So a firmware that waits for the link's end after its own close, as
`ESP_BT_GAP_ACL_DISCONN_CMPL_STAT_EVT`, waits about 4 s on the emulator: its own host's inactivity
timer, not the peer. A firmware that opens the channel again within those 4 s gets the link it had.
In the same example with an adapter that stopped answering, after which the reader searches at
once, its search started 50 ms after the line it prints before `esp_spp_disconnect`, `obd_reader:
the adapter stopped answering`, and SDP 91 ms after it, on the old link: no page and no pairing.

### The peer hangs up: `disconnect()`

To end the link from the peer's side, as an adapter that drops its connection does, a test calls
`disconnect()`, as `examples/obd_reader/tests/test_obd_reader.py` does:

```python
board.wait_until(peer.disconnect(), within=ms(100))  # The peer's end of each link is gone.
board.wait_until(board.console.line("obd_reader: disconnected"), within=ms(100))
```

`disconnect()` is a `BluetoothDevice`'s, so a plain `BluetoothDevice` has it too. At the air's
time it ends every ACL link the peer has, each with `HCI_Disconnect` and Bumble's default reason,
`REMOTE_USER_TERMINATED_CONNECTION_ERROR`, `0x13`, which the firmware's host gets in its
`Disconnection_Complete`. RFCOMM and L2CAP end with the link: `channel_open` turns false, and a
line the end cut off is a `WrittenLine` with `complete=False`, as after any close. The peer stays
`present`, keeps its link keys and stays connectable, so the firmware can page it again at once,
authenticate with the key it kept, and open the channel again.

It returns a described condition that holds once every link the peer had at the call has ended. It
is read from the disconnection's own completion, not from `peer.device.connections` when it is
asked, so a firmware that pages the peer again before the next poll does not hide the end. On the
air the disconnection completes at the instant it starts. The other links are ended even when
ending one fails; what Bumble raised for the first that failed is then raised from the condition as
a `PeerError`. The condition is named `peer's disconnection (00:00:00:00:5B:01)` in
`NeverHappened`, and refuses `bool()`: `call the SPP condition with (), or pass it to
board.wait_until`, or `the Bluetooth condition` for a plain `BluetoothDevice`.

Wait for this condition, not for `peer.is_closed`: after the device's own close the channel is
closed already while the link is still up, so `is_closed` says nothing about the link. What the
firmware makes of it comes later, at its own events: wait for one of those, here the console line it
prints at `ESP_SPP_CLOSE_EVT`.

`disconnect()` takes no reason. For an adapter that ends the link itself, call `disconnect()`; for
one that goes silent, as one whose power or radio fails, call `out_of_range()`, whose links end at
the supervision timeout with `CONNECTION_TIMEOUT`, `0x08` ([above](#a-peer-that-leaves)), and
`in_range()` to bring it back. `disconnect()` raises `PeerError` at once
([below](#when-something-goes-wrong)) with no link, while the peer is out of range, and with a link
that lost contact: one the peer left while it was out of range, or one whose other side left the
air, as the board's controller does at `board.reset()`, at a reset the firmware makes itself, at its
`HCI_Reset`, or at `board.power_off()`. Such a link ends only at its supervision timeout, and
nothing the peer sends reaches the other side before then.

Measured on the same example: the firmware's host gets `Disconnection_Complete`, reason `0x13`, 0.1
ms after, and `ESP_SPP_CLOSE_EVT` reaches the firmware 10.6 ms after; no RFCOMM or L2CAP frame goes
before it, since the link's end closes them all. After the device's own close, a test that does not
want to wait for the firmware's 4 s hangs up the same way, and waits for the firmware's own sign of
the link's end, such as `ESP_BT_GAP_ACL_DISCONN_CMPL_STAT_EVT`, if it has one.

These times hold for that build and esp32tl's pinned Bumble; to measure them again, as after a
bump of either, tap a run of `examples/obd_reader` with an adapter it has nothing to read on, such
as `Elm327(version="OK")`, with `board.bluetooth.tap(path)`, and read the packets after
`obd_reader: not an ELM327 adapter: OK` in the tap's `events` or the btsnoop file.

The peer keeps no history of its openings and closes: a test sees the close's time only to within
one turn of `board.wait_until(peer.is_closed, ...)`, and who closed the channel and when the link
went only in the tap.

## The model that answers

`source` is what drives the SPP channel from the peer's end, written as for the far end of a UART
([a UART's own model](pytest-plugin-user.md#a-uarts-own-model)): a subclass of
`esp32tl.Model`, whose times are `Duration`s of the air's clock, or an object with the
four methods, whose times are nanoseconds as `int`s. Both kinds put the same bytes on the channel at
the same instants; when to prefer nanoseconds is said there.

| Method | Called | Returns |
|---|---|---|
| `receive(at, data)` | When the device writes `data` on the channel, at `at`, the instant it arrives | The answers, `(at, bytes)` pairs, each at `at` or later |
| `poll(now)` | At every instant the air stops at, and possibly more than once at one | What it sends unprompted: `(at, bytes)` pairs, each at `now` or later. A channel has no line rate: the `(at, bytes, baud)` triple a UART's source may give is refused, as anything else that is not a pair is ([below](#when-something-goes-wrong)) |
| `quiet_until()` | Whenever the air looks for its next instant | The next time it has something to send unprompted, later than `now`; `None` for nothing |
| `latency()` | Never, by the peer | The shortest time in which it answers, as a UART's source says it |

A `Model` defines all four, as a source in nanoseconds does. `Echo` in nanoseconds would return
`[(t + ms(50).ns, ...)]` from `receive` and `ms(50).ns` from `latency`.

The bytes the device writes come in the pieces the device wrote them in; a model that answers
lines finds them by their terminator. Each answer goes out at its own time, never earlier; answers
of the same time go out in the order they were given; empty bytes are dropped. What the source
sends while no device holds the channel is lost, as it would be on the air: `peer.lost` counts its
bytes. A source that raises, or returns what the peer cannot send, fails the air (below).

The BLE and SPP histories are
[read-only views that see later records](../guides/writing-tests.md#reading-a-history).

`peer.events` holds what crossed the channel, in order, as `ChannelEvent(at, direction, data)`
named tuples. `direction` is said from the firmware's side, as a tap's recording names it
([recording format](../recording-format.md)): `"out"`, written by the firmware to the peer;
`"in"`, sent by the peer to the firmware.

`ChannelEvent`, `WrittenLine`, `Refusal`, `Pairing` and `UnknownCommand` are imported from
`esp32tl`. All their timestamps are `Duration`s: `event.at`, `line.at`, `refusal.at`, `pairing.t`,
and `command.t`. Tuple unpacking still works for the named tuples; `Pairing` remains a frozen
dataclass. Compare these times directly with `Message.at`, console `ConsoleLine.at` or GPIO
`LineEvent.t`, without wrapping them in `Duration`. This breaks the former integer API: use `.ns`
only when integer nanoseconds are needed. The run report is unchanged; `esp32sim_link` JSON still
writes integer `t_ns` fields.

## Time on the air

An `Air()` has a clock of its own, which starts at 0 when it is made and moves only when the test
moves it; `bt.air` is the board's, [above](#in-front-of-the-firmware-boardbluetooth), and only the
board moves it. Nothing on the air reads the host's clock, so two runs do the same at the same
instants.
History timestamps use that same clock: board time on `bt.air`, or the standalone air's
time since creation on `Air()`.

| Call | What it does |
|---|---|
| `air.run(awaitable, *, within, description="")` | Runs a coroutine on the air, the device's side, until it completes, for at most `within`, a `Duration`, from now; returns its result or raises what it raised |
| `air.wait(duration)` | Lets the air run for `duration` |
| `air.advance(until)` | Moves the air to `until`, a `Duration` since 0 |
| `air.start(coroutine)` | Starts a coroutine on the air, as a task, run as far as it goes at the current instant; it goes on as the air's time moves, also on `bt.air` |
| `air.now` | The air's time, a `Duration` |
| `air.close()` | Ends the air |

On the air:

- Everything the link carries arrives at the instant it is sent: discovery, connection, pairing,
  SDP and RFCOMM take no time of the board's. Only what something waits for takes time: a model's
  answer at its time, a Bumble timer, an inquiry's length.
- An inquiry is answered by every device on the air whose inquiry scan is on, at the instant it
  starts, in the order of their addresses; it completes after its length, a multiple of 1.28 s
  (Bumble asks for 8, 10.24 s), unless cancelled.
- The link keys are not the ones the Bluetooth specification would derive: a key or a response
  holds on this air only. Secure Simple Pairing has no Diffie-Hellman exchange either: its numbers
  and passkeys are those a user sees, and its keys hold on this air only, of the type
  (authenticated or not) the specification gives them.

## When something goes wrong

| Message | Exception | When |
|---|---|---|
| `SppPeer: name is '', not a string of 1 to 238 bytes` | `PeerError` | A name that is not a string, empty, or longer than 238 bytes |
| `SppPeer: pin is 1234, not a string of 1 to 16 bytes` | `PeerError` | A PIN that is not a string, empty, or longer than 16 bytes |
| `SppPeer: channel is 31, not an RFCOMM channel, 1 to 30` | `PeerError` | A channel out of range, or not a whole number |
| `SppPeer: class_of_device is 16777216, not three octets, 0 to 0xFFFFFF` | `PeerError` | A class of device out of range, or not a whole number |
| `SppPeer: rssi is 21, not a signal strength in dBm, -127 to 20` | `PeerError` | An `rssi` out of range, or not a whole number, given when it is made or set later, which then leaves it as it was |
| `SppPeer: address is 'nonsense', not a Bluetooth address, as 00:00:00:00:5B:01` | `PeerError` | An address that is not six pairs of hex digits |
| `SppPeer: address 00:00:00:00:5B:01 is another device's on the air: give each its own` | `PeerError` | A second device at an address already on the air |
| `bench: source.receive raised KeyError: 'model bug'` | `AirError`, from `PeerError`, from the source's exception | The source raised, in `receive`, `poll`, `quiet_until` or `supply` |
| `bench: source.receive sent b'too early' at -0.000000001 s, and the air is at 0 s: it cannot send into the past` | `AirError`, from `PeerError` | The source returned a time before the instant it was asked at |
| `bench: source.receive returned (0, 'text'), not a (t_ns, bytes) pair` | `AirError`, from `PeerError` | The source returned something else |
| `bench: source.receive returned (seconds(0), 'text'), not a (Duration, bytes) pair` | `AirError`, from `PeerError` | A `Model` returned something else, a triple with a line rate among them: `bench: source.receive returned (seconds(0), b'text', 9600), not a (Duration, bytes) pair` |
| `bench: source.quiet_until returned 1.5, not nanoseconds` | `AirError`, from `PeerError` | `quiet_until` returned something other than an `int` or `None` |
| `bench: source.quiet_until returned 1.5, not a Duration` | `AirError`, from `PeerError` | A `Model`'s `quiet_until` returned something other than a `Duration` or `None` |
| `bench: source.quiet_until returned 0 s, not after the air's 0 s: what it sends by then belongs in the poll at 0 s` | `AirError`, from `PeerError` | `quiet_until` returned a time not after the air's |
| `{description} did not complete within 10 s: waited from 0 s to 10 s of the board's time` | `NeverHappened` | `air.run` ran out of `within`; `the awaitable` without a `description` |
| `air.wait: this air is on the board's clock, which only the board's link moves: let the board run with board.wait() or board.wait_until()` | `AirError` | `air.wait`, `air.advance` or `air.run`, each named, on `bt.air` |
| `peer: not connected: disconnect needs a link, which the device makes` | `PeerError` | `disconnect()` on a peer with no link, which a peer switched off never has |
| `peer: out of range: its links end at their supervision timeout, not with disconnect(); wait for it, and call in_range() for a new link` | `PeerError` | `disconnect()` on a peer out of range that still has a link |
| `peer: lost contact with 24:0A:C4:00:00:01: the link ends at its supervision timeout, not with disconnect(); wait for it` | `PeerError` | `disconnect()` with a link that lost contact and has not fallen yet: the peer left it while out of range and is back, or the other side left the air, as the board's controller does at a reset or a power-off |
| `peer's disconnection (00:00:00:00:5B:01): HCI_Error: …` | `PeerError`, from Bumble's exception, raised by the condition | Bumble raised while ending a link, the first that failed; the type and message are Bumble's, and the other links are ended all the same |
| `peer's disconnection (00:00:00:00:5B:01): Bumble cancelled it before the link ended` | `PeerError`, raised by the condition | Bumble cancelled the wait for a link's end under the call, as its `Device.disconnect` does at its host's `EVENT_FLUSH` |
| `SppPeer: service_name is '', not None or a string of 1 to 255 bytes` | `PeerError` | A service name that is not a string or `None`, empty, or longer than 255 bytes |
| `the air is at 1 s: it cannot go back to 0.5 s` | `ValueError` | `air.advance` to a time before the air's |
| `air.advance takes a Duration, as seconds(1), not 1` | `TypeError` | `air.advance(1)`: a plain number where a `Duration` belongs |
| `air.wait takes a Duration, as seconds(1), not 1` | `TypeError` | `air.wait(1)` |
| `air.run: within takes a Duration, as seconds(1), not 1` | `TypeError` | `air.run(..., within=1)` |
| `HCI_Error(hci/AUTHENTICATION_FAILURE_ERROR [0x5])` | Bumble's `HCI_Error`, on the device's side | The device gave a PIN other than the peer's; or, with Secure Simple Pairing, a user said no, a passkey typed differs from the one shown or the other side's, or a keyboard had none to type |
| `SppPeer: io_capability is 'keyboard', not None or one of display_only, display_yes_no, keyboard_only, no_input_no_output` | `PeerError` | An IO capability of another name |
| `SppPeer: confirms is 1, not True or False` | `PeerError` | |
| `SppPeer: passkey is 1000000, not six digits, 0 to 999999` | `PeerError` | A passkey out of range, or not a whole number |
| `SppPeer: passkey is typed on a keyboard: give io_capability='keyboard_only'` | `PeerError` | A passkey for a peer without a keyboard, which never types one |
| `HCI_Error(hci/PIN_OR_KEY_MISSING_ERROR [0x6])` | Bumble's `HCI_Error`, on the device's side | The device authenticated with a key the peer does not keep |
| `HCI_Error(hci/INSUFFICIENT_SECURITY_ERROR [0x2F])` | Bumble's `HCI_Error`, on the device's side | The device asked for encryption on a link it had not authenticated |
| `hci/PAGE_TIMEOUT_ERROR`, in `error_code` | Bumble's `core.ConnectionError`, on the device's side | Nobody answered the page: the peer has left, its end of an old link has not fallen, or no device has the address |
| `hci/CONNECTION_REJECTED_DUE_TO_LIMITED_RESOURCES_ERROR`, in `error_code` | Bumble's `core.ConnectionError`, on the device's side | The host of the device it paged rejected the page, with the reason its `Reject_Connection_Request` gave |
| `hci/CONNECTION_ACCEPT_TIMEOUT_ERROR`, in `error_code` | Bumble's `core.ConnectionError`, on the device's side | The host of the device it paged did not answer within its connection accept timeout, 5 s unless it wrote another |
| `HCI_Error(hci/PAGE_TIMEOUT_ERROR [0x4])` | Bumble's `HCI_Error`, on the device's side | Nobody answered `Device.request_remote_name` |
| `HCI_Error(hci/CONNECTION_ALREADY_EXISTS_ERROR [0xB])` | Bumble's `HCI_Error`, on the device's side | The device paged a peer while it holds a link to it, as one that lost contact and has not fallen; while a page of its own to that peer is under way; or while its host has yet to answer a page from that peer, which outlasts a page of its own that has already reported its end, until its connection accept timeout |
| `HCI_Error(hci/CONNECTION_TIMEOUT_ERROR [0x8])` | Bumble's `HCI_Error`, on the device's side | The link fell during `Connection.authenticate()` |
| `[ble] rejected invalid VHCI packet pointer, length or controller state`, on the runner's stderr; for a refused command, the firmware's `BT_HCI: command_timed_out … opcode: …` after it, `opcode: 0xc03` when it is the first, `HCI_Reset` | none; when the first command is refused, the four counts stay 0 | esp32sim refused a packet the firmware sent: it lies neither in internal DRAM nor in PSRAM that the chip's cache maps and can read ([technical](bluetooth-peer-technical.md#failure-modes)) |

A `BluetoothDevice` says the same of its arguments, starting `BluetoothDevice:` rather than
`SppPeer:`. `PeerError` is a `ValueError` and `AirError` a `RuntimeError`, both from `esp32tl`;
an `AirError`'s `__cause__` is what failed: whatever raises on the air, in a model or in Bumble,
is raised from the call that moved the time. What the peer finds wrong with its
source keeps the peer's message; anything else that raises on the air, in Bumble or in the test's
own coroutine's callbacks, is prefixed with asyncio's description of where it was raised, such as
`Exception in callback Host.on_packet(): …`.
`NeverHappened` is the board's, also from `esp32tl`.

## Bluetooth LE: `BlePeripheral` and `BleCentral`

On a chip whose controller is LE only (the ESP32-S3, the ESP32-C3; the ESP32-C6 once esp32sim's
hooks take its images, #229 G5), the firmware's Bluedroid talks LE to the harness's controller,
and the test puts LE devices beside the board. ESP-IDF's `gatt_server`, `gatt_client` and
`gatt_security_server`, unmodified, run against them on both chips (`tests/test_ble.py`;
[validation firmware](validation-firmware-user.md#gatt_server-gatt_client-and-gatt_security_server)).

### The firmware as a central: `BlePeripheral`

```python
import pytest

from esp32tl import BlePeripheral, GattCharacteristic, GattService
from esp32tl.units import seconds


@pytest.mark.board_profile("esp32-c3-devkitm")
@pytest.mark.app_path("../firmware/gatt_client", build_dir="build-esp32c3")
def test_the_firmware_writes_and_is_notified(board, bt, console):
    peripheral = BlePeripheral(
        bt.air,
        name="ESP_GATTS_DEMO",
        services=[
            GattService("00FF", [GattCharacteristic("FF01", properties=("write", "notify"))])
        ],
    )
    written = peripheral.next_write("FF01")
    board.power_on()
    board.wait_until(peripheral.is_subscribed("FF01"), within=seconds(5))
    assert board.wait_until(written, within=seconds(1)).value == bytes(range(35))
    board.wait_until(peripheral.notify("FF01", b"hello"), within=seconds(1))
    console.expect_exact("GATTC_DEMO: 68 65 6c 6c 6f", timeout=1)
```

`BlePeripheral(air, *, name, services=(), service_uuids=None, manufacturer_data=None,
address="00:00:00:00:5B:02", interval=ms(100), connectable=True, rssi=-50,
io_capability="no_input_no_output", confirms=True, passkey=None, bonding=True)`:

| Argument | What it is |
|---|---|
| `name` | Its name, a string of 1 to 248 bytes in UTF-8: whole in its GATT database (`0x2A00`), and in its advertising when it fits, in at most 29 bytes; a longer one is advertised shortened, its first 29 bytes of whole characters |
| `services` | Its GATT database: `GattService(uuid, characteristics)`, each a primary service, with `GattCharacteristic(uuid, value=b"", properties=("read",), encrypted=False)` each |
| `service_uuids` | The service UUIDs it advertises: the services' unless given; `()` for none |
| `manufacturer_data` | `(company identifier, bytes)` for the manufacturer specific data of its advertising, or `None` |
| `address` | Its public address, which it advertises from; no other device on the air may have it |
| `interval` | Its advertising interval, a `Duration` of 20 ms to 10.24 s |
| `connectable` | Whether a central can connect to it: undirected connectable and scannable advertising (`ADV_IND`), or scannable only (`ADV_SCAN_IND`) |
| `rssi` | The signal strength a scan hears it at, in dBm, -127 to 20 |
| `io_capability`, `confirms`, `passkey`, `bonding` | How it pairs ([below](#pairing)) |

A UUID is 4 hex digits for a 16-bit one, `"FF01"`, or a 128-bit one, 32 hex digits bare or in the
8-4-4-4-12 form with its dashes, `"6E400002-B5A3-F393-E0A9-E50E24DCCA9E"`; esp32tl writes them back
the first way for 16 bits and the second for 128. A 16-bit UUID written in its 128-bit form on the
Bluetooth base, `"0000180D-0000-1000-8000-00805F9B34FB"`, is the same UUID as `"180D"`, and esp32tl
writes it back so: two characteristics so named are one twice, and a filter on either finds both. A characteristic's `properties` are any of `read`, `write`,
`write_without_response`, `notify` and `indicate`; one that notifies or indicates has a Client
Characteristic Configuration descriptor. With `encrypted`, it is read and written only over an
encrypted link: an unencrypted one gets `INSUFFICIENT_ENCRYPTION`. A read of one without `read`
gets `READ_NOT_PERMITTED`, a write of one without `write` `WRITE_NOT_PERMITTED`. The UUIDs of the
characteristics must differ, as they name them.

From its creation, at the air's instant, it advertises every `interval`: the flags (LE General
Discoverable, BR/EDR not supported), then its name, its 16-bit service UUIDs, its 128-bit ones and
its manufacturer data, each in the 31 bytes of the advertising data while it fits, else in the scan
response, which an active scan gets. It takes one central at a time, stops advertising while
connected, and advertises again once the connection ends.

| Name | What |
|---|---|
| `peripheral.value(uuid)` | The characteristic's value now: what the firmware last wrote, or the test set or notified |
| `peripheral.set_value(uuid, value)` | Sets it, for the next read, without notifying |
| `peripheral.writes` | Every write of the firmware, `GattWrite(at, uuid, value)` |
| `peripheral.next_write(uuid=None, value=None)` | A condition: the first write after the call, to `uuid` and containing `value` where given |
| `peripheral.subscriptions` | Every write of a Client Characteristic Configuration, `Subscription(at, uuid, notify, indicate)` |
| `peripheral.subscribed(uuid)`, `peripheral.is_subscribed(uuid)` | Whether the connected central subscribed to the characteristic, and a condition for it |
| `peripheral.notify(uuid, value)`, `peripheral.indicate(uuid, value)` | Sets the value and sends it to the subscribed central, at the board's instant; a condition that finds the `Notification(at, uuid, value, indication)` once sent, for an indication once the central confirmed it |
| `peripheral.notifications` | What it sent |
| `peripheral.advertising` | Whether it advertises now |

### The firmware as a peripheral: `BleCentral`

```python
from esp32tl import BleCentral


@pytest.mark.board_profile("esp32-s3-devkitc")
@pytest.mark.app_path("../firmware/gatt_server", build_dir="build-esp32s3")
def test_the_firmware_is_read_written_and_notifies(board, bt, console):
    central = BleCentral(bt.air, name="phone")
    board.power_on()
    seen = board.wait_until(central.next_advertisement(name="ESP_GATTS_DEMO"), within=seconds(5))
    board.wait_until(central.connect(seen), within=seconds(2))
    read = board.wait_until(central.read("FF01"), within=seconds(1))
    assert read.value == bytes.fromhex("deedbeef")
    notified = central.next_notification("FF01")
    board.wait_until(central.subscribe("FF01"), within=seconds(1))
    assert board.wait_until(notified, within=seconds(1)).value == bytes(range(15))
    board.wait_until(central.write("FF01", b"\x01\x02\x03"), within=seconds(1))
    console.expect_exact("GATTS_DEMO: 01 02 03", timeout=1)
```

`BleCentral(air, *, name="esp32tl central", address="00:00:00:00:5C:01", rssi=-50,
io_capability="no_input_no_output", confirms=True, passkey=None, bonding=True)` scans from its
creation, actively, from its public address, and keeps every advertising event it hears, with the
scan response of a scannable one, in `central.advertisements`: `Advertisement(at, address,
address_type, name, service_uuids, manufacturer_data, rssi, connectable, data, scan_response)`,
`address_type` `"public"` or `"random"`, `name` the complete or the shortened local name, from the
advertising data or the scan response. It goes on scanning while connected.

What it does over a connection takes time on the air, so each call starts it at the board's
instant and returns a condition for `board.wait_until`, which finds what the call got, or raises
`BleError` for what went wrong, as `esp32tl central's write of FF01: the peer answered
WRITE_NOT_PERMITTED (0x03)`:

| Call | Starts | The condition finds |
|---|---|---|
| `central.next_advertisement(*, name=None, address=None, service_uuid=None)` | Nothing: a condition for the first advertising event heard after the call, matching what is given; `name` is compared with the name advertised, so for a device that advertises a shortened one, the shortened one | The `Advertisement` |
| `central.connect(to)` | A connection to `to`, an `Advertisement` heard or a public address, at the next connectable advertising event it hears from it, however long that takes: `central.cancel_connect()` ends it, and so does switching the central off, and the condition then raises `BleError` | `LeConnection(at, peer, role)` |
| `central.discover()` | The discovery of the peer's primary services, their characteristics and descriptors | `Discovery(at, services)`, each a `DiscoveredService(uuid, characteristics)` of `DiscoveredCharacteristic(uuid, handle, properties)`; kept in `central.discovery` until the connection ends |
| `central.read(uuid)` | A read of the characteristic | `GattRead(at, uuid, value)` |
| `central.write(uuid, value, *, with_response=True)` | A write request, or with `with_response=False` a write command | `GattWrite(at, uuid, value)` |
| `central.subscribe(uuid, *, indicate=False)` | A write of the characteristic's Client Characteristic Configuration, for notifications or indications | `Subscription(at, uuid, notify, indicate)` |
| `central.next_notification(uuid=None, value=None)` | Nothing: a condition for the first notification or indication received after the call | The `Notification` |
| `central.pair()` | A pairing, as the initiator ([below](#pairing)) | `LePairing(at, peer, succeeded, bonded, reason)` |
| `central.disconnect()` | The end of the connection | `Disconnection(at, peer, reason)` |

`read`, `write` and `subscribe` name a characteristic by its UUID, the first of that UUID in the
peer's database, and discover the database first when the central has not. `subscribe` needs the
characteristic to notify, or with `indicate` to indicate, and to have a Client Characteristic
Configuration descriptor, else its condition raises `BleError`; subscribing again to what the
central is subscribed to changes nothing, and each notification is still received once. A request
the peer never answers because the link fell first, as when the peer left the air, ends with
`BleError` when the connection falls: `esp32tl central's read of FF01: the connection ended before
the answer came`. One connection at a time. When two centrals take one advertising event for a
connection, the peripheral takes the first `CONNECT_IND` the link delivers; the other central's
host is told of a connection, as a real one's is, which falls 6 connection intervals later with
`CONNECTION_FAILED_TO_BE_ESTABLISHED` (`0x3E`). Its connections have Bumble's parameters: an interval of 15 to 30 ms, a latency of 0 and a
supervision timeout of 720 ms, until the peripheral asks for others: `gatt_server` asks for 20 to
40 ms and 4 s, and gets them. The notifications and indications it received are in
`central.notifications`.

### Both kinds

| Name | What |
|---|---|
| `device.connected`, `device.is_connected`, `device.is_disconnected` | Whether it has a connection now, and conditions for it |
| `device.peer` | The address of the device it is connected to, the identity address once a bond resolved it; `None` without a connection |
| `device.encrypted`, `device.is_encrypted` | Whether the connection is encrypted now, and a condition for it |
| `device.connections`, `device.disconnections`, `device.pairings` | Every `LeConnection`, `Disconnection` and `LePairing`, in order |
| `device.next_connection()`, `device.next_disconnection()`, `device.next_pairing()` | Conditions for the first one after the call |
| `device.events` | `LeEvent(at, text)`, everything it did ([below](#what-the-report-and-the-run-log-say)) |
| `device.switch_off()`, `switch_on()`, `out_of_range()`, `in_range()`, `present`, `rssi` | As for a Classic device ([a peer that leaves](#a-peer-that-leaves)), and [below](#leaving) |
| `device.forget_keys()` | It loses the keys it bonded with |
| `central.cancel_connect()` | Ends the connection being made, at the board's instant; nothing to cancel is nothing done |
| `device.device` | Its Bumble device |

A `Disconnection`'s `reason` is an HCI error code, with `reason_name` such as
`CONNECTION_TIMEOUT_ERROR (0x08)`: `0x08` for a link that lost contact, `0x13` for the other side's
disconnect, `0x16` for its own.

### Pairing

Pairing is the LE Security Manager's, Bumble's on the test's side and Bluedroid's on the firmware's,
with LE Secure Connections. Its association model comes from the two IO capabilities:
`no_input_no_output`, the default, pairs Just Works; `display_only`, `display_yes_no`,
`keyboard_only` and `keyboard_display` are the others. `confirms` is the user's yes to a number
to compare; `passkey`, six digits, is what the user types on a keyboard, or what a display shows
(Bumble draws one otherwise). A firmware without LE Secure Connections pairs the legacy way, which
Bumble does too.

A pairing is a bond, `LePairing.bonded`, when both sides asked for bonding (`bonding`, the default,
on this side, and the firmware's auth request, as `ESP_LE_AUTH_BOND`) and it gave a long term key,
of LE Secure Connections or of legacy pairing. The device then keeps the keys until
`forget_keys()`; without a bond it keeps none, and the next connection pairs again.

- **The central starts it** with `central.pair()`, or when the peer asks for security
  (`gatt_security_server` does as soon as it is connected): with keys kept for the peer, the
  central encrypts the link with them; without, it pairs, as a phone does. A peer that has forgotten
  the bond refuses the kept key: the link stays unencrypted, the central's events say
  `encryption with ADDRESS failed: …`, and it does not pair again by itself; `central.pair()` pairs
  again. A peripheral answers the pairing it is asked for; it does not start one.
- **Across a reset of the firmware.** Bluedroid keeps its bonds in the NVS, which a reset keeps:
  after `board.reset()` `gatt_security_server` advertises from a new resolvable private address, the
  central resolves it with the identity key it got when pairing, and the link is encrypted with the
  kept keys, with no pairing again; `central.peer` is then the firmware's identity address,
  `24:6F:28:00:11:24`.
- **The firmware's private address.** A host that puts its local identity key in the controller's
  resolving list and advertises with own address type `0x02` or `0x03`, as Bluedroid's
  `esp_ble_gap_config_local_privacy(true)` does, gets a resolvable private address made by the
  controller, made again after the host's RPA timeout or when the chip restarts, and told to the host
  in `LE_Enhanced_Connection_Complete`.

`device.pairings` holds each that ended, `LePairing(at, peer, succeeded, bonded, reason)`, where
`reason` is the Security Manager's for one that failed (Vol 3, Part H, 3.5.5), as `0x04` for a
confirm value that failed, a passkey typed that differs from the one shown.

### Leaving

`switch_off()`, `out_of_range()` and their undoing work as for a Classic device. To the other side:

- **The connection falls with `CONNECTION_TIMEOUT` (`0x08`)** at its supervision timeout after the
  device left, on each end still holding it: the timeout it was made or last updated with. With
  `gatt_server`, which asks for 4 s, the firmware logs `GATTS_DEMO: Disconnected, remote
  00:00:00:00:5c:01, reason 0x08` 4 s after the central went out of range, and advertises again;
  back in range, the central hears it and connects again. A `BlePeripheral` switched off under
  `gatt_client` is gone: the firmware logs `reason 0x08` at its own supervision timeout, and
  `gatt_client` does not scan again.
- **Advertising is not heard** from a device off the air, and a device off the air hears none.
- **An update of the connection's parameters** asked for once the device has left never
  completes: the connection falls at the timeout it had.

At the device itself, a switch off ends its connections at once, which its own Bumble host hears
with `0x08`, and a connection being made; a peripheral does not advertise while off; switched on,
a peripheral advertises again and a central scans again. Out of range, it
runs on, and its connection falls at its timeout as the other side's does. The board's own
supply and resets take the firmware's controller off the air as for Classic
([above](#in-front-of-the-firmware-boardbluetooth)): a central connected to the firmware sees its
link fall at its timeout, 720 ms for Bumble's.

### What the report and the run log say

Each LE device keeps its events in `device.events`, `LeEvent(at, text)`, and on `bt.air` puts each in
the run log as it happens, `bluetooth: phone: connected to 24:6F:28:00:11:24, as central`, and in
the report, after `devices:`, under its name, address and role:

```text
  phone (00:00:00:00:5C:01), LE central: 7 events
    0.225472533 s  advertising seen from 'ESP_GATTS_DEMO' 24:6F:28:00:11:24 (public)
    0.245472533 s  connected to 24:6F:28:00:11:24, as central
    0.655558400 s  discovered 4 services of 24:6F:28:00:11:24: 1801, 1800, 00FF, 00EE
    0.675725333 s  read FF01 of 24:6F:28:00:11:24: deedbeef
    0.715859733 s  notification of FF01 from 24:6F:28:00:11:24: 000102030405060708090a0b0c0d0e
    0.715992266 s  subscribed to notifications of FF01 of 24:6F:28:00:11:24
    0.745794666 s  wrote FF01 of 24:6F:28:00:11:24: 010203
```

That is `gatt_server` on the ESP32-S3 with the central of the example above (seen on 2026-11-19).
The notification comes before the subscription is done: `gatt_server` notifies as soon as the
Client Characteristic Configuration is written, before Bumble has the write's response.

The events: `advertising seen from 'NAME' ADDRESS (public|random)`, once for each address;
`connected to ADDRESS, as central` or `connected from ADDRESS, as peripheral`; `disconnected from
ADDRESS: CONNECTION_TIMEOUT_ERROR (0x08)` with the reason; `encrypted with ADDRESS`; `paired with
ADDRESS, bonded` or `pairing with ADDRESS failed: reason 0x04`; `encryption with ADDRESS failed:
…`; on a central `discovered N services
of ADDRESS: …`, `read UUID of ADDRESS: HEX`, `wrote UUID of ADDRESS: HEX`, `subscribed to
notifications of UUID of ADDRESS`, `notification of UUID from ADDRESS: HEX`; on a peripheral
`ADDRESS wrote UUID: HEX`, `ADDRESS subscribed to UUID: notifications`, `notification of UUID to
ADDRESS: HEX`. The btsnoop tap (`bt.tap(path)`) has the LE packets as they crossed the VHCI,
unchanged: the commands, the LE Meta events and the ACL data of the connections.

### What is left out

Extended and periodic advertising, the coded PHY, isochronous channels (LE Audio), directed
advertising, more than one connection on a central, the connection's events and their timing (data
crosses at the instant it is sent, as on the Classic link), the resolution of a peer's private
address by the firmware's controller (Bluedroid resolves it itself), and the out of band pairing:
[decisions](../decisions/harness.md#bluetooth-le-peers-on-bumbles-le-link-layer-with-esp32tls-fixes).

### When something goes wrong

| Message | Exception | When |
|---|---|---|
| `GattCharacteristic: uuid is 'FF0', not a 16-bit UUID in hex, as 'FF01', or a 128-bit one` | `PeerError` | A UUID of another form, also for a service, `service_uuids` and the UUIDs a call names, each naming its class |
| `GattCharacteristic FF01: properties are ('fly',), not one or more of read, write, write_without_response, notify, indicate` | `PeerError` | |
| `GattCharacteristic FF01: value is 'x', not bytes` | `PeerError` | |
| `GattService 00FF: characteristics are ['x'], not GattCharacteristic objects` | `PeerError` | |
| `BlePeripheral: characteristic FF01 is in the database twice: the UUIDs name the characteristics, give each its own` | `PeerError` | |
| `BlePeripheral: interval is 0.01 s, not 20 ms to 10.24 s` | `PeerError` | |
| `BlePeripheral: the manufacturer data (34 bytes) fit neither in the advertising data nor in the scan response, 31 bytes each` | `PeerError` | Also for the service UUIDs |
| `BlePeripheral: manufacturer_data is (70000, b''), not a (company identifier, bytes) pair` | `PeerError` | |
| `BleCentral: name is '', not a string of 1 to 248 bytes` | `PeerError` | |
| `BleCentral: io_capability is 'keyboard', not one of display_only, display_yes_no, keyboard_only, no_input_no_output, keyboard_display` | `PeerError` | |
| `sensor: no characteristic ABCD in its database` | `PeerError` | A peripheral's call for a UUID it does not serve |
| `sensor: no central is subscribed to notifications of FF01: wait for peripheral.is_subscribed('FF01')` | `PeerError` | `notify` before the central subscribed to notifications; `indicate` before it subscribed to indications says `indications` |
| `esp32tl central: not connected: read needs a connection, made with central.connect(...)` | `PeerError` | A central's call that needs a connection, each naming itself |
| `esp32tl central: connected to 00:00:00:00:5B:02 already: one connection at a time` | `PeerError` | |
| `esp32tl central: a connection is being made already: wait for it, or end it with central.cancel_connect()` | `PeerError` | `connect` while the last one is still under way |
| `esp32tl central's write of FF01: the peer answered WRITE_NOT_PERMITTED (0x03)` | `BleError`, from Bumble's `ATT_Error`, raised by the condition | The peer refused a request: also `READ_NOT_PERMITTED (0x02)` and `INSUFFICIENT_ENCRYPTION (0x0f)` |
| `esp32tl central's read of ABCD: 00:00:00:00:5B:02 has no characteristic ABCD` | `BleError` | |
| `esp32tl central's connection to 00:00:00:00:5B:0F: ConnectionError: ConnectionError(hci/UNKNOWN_CONNECTION_IDENTIFIER_ERROR [0x2])` | `BleError`, from Bumble's `ConnectionError` | The connection was cancelled with `central.cancel_connect()`, or the central was switched off while making it |
| `esp32tl central's pairing with 00:00:00:00:5B:02: reason 0x04` | `BleError` | The pairing failed, with the Security Manager's reason |

The conditions say what they wait for, `the next advertisement called 'ESP_GATTS_DEMO' heard by
phone (00:00:00:00:5C:01)` or `esp32tl central's read of FF01 (00:00:00:00:5C:01)`, in
`NeverHappened`, and refuse `bool()`: `call the BLE condition with (), or pass it to
board.wait_until`. `BleError` is a `RuntimeError` from `esp32tl`; its `__cause__` is what failed.

## See also

- [Bluetooth peer: how it works](bluetooth-peer-technical.md)
- [Models and units](models-user.md): `Duration` and the other units
- [Decisions: the Bluetooth peer](../decisions/harness.md#the-bluetooth-peer-runs-bumble-in-the-harness-on-the-boards-clock)
- [Decisions: how a peer leaves](../decisions/harness.md#a-peer-leaves-without-a-word-keeps-its-keys-and-its-old-link-stays-dead-until-it-falls)
- [Decisions: the air of the `bt` fixture is on the board's clock](../decisions/harness.md#the-air-of-the-bt-fixture-is-moved-by-the-boards-link-alone)
- [esp32sim backend: Bluetooth](esp32sim-backend-user.md#bluetooth)
- [Decisions: the LE peers](../decisions/harness.md#bluetooth-le-peers-on-bumbles-le-link-layer-with-esp32tls-fixes)

## Waiting for the SPP channel and device writes

`peer.is_open` and `peer.is_closed` are `Condition[bool]` properties for the channel's
current state. Pass them to `board.wait_until`, or call them with `()` to read the state.
`channel_open` remains a plain boolean property. Conditions refuse `bool()` (including
`assert peer.is_open`) with `TypeError`:
`call the SPP condition with (), or pass it to board.wait_until`.
Timeouts name the peer, state and address, for example
`ESP_SPP_ACCEPTOR's SPP channel is open (00:00:00:00:5B:01)`.

```python
board.wait_until(peer.is_open, within=seconds(20))
request = peer.next_write(b"AT")  # Capture the boundary before triggering the action.
board.reset()
event = board.wait_until(request, within=seconds(30))
assert b"AT" in event.data
assert event.direction == "out"
```

`next_write(data: bytes | None = None)` returns a `Condition[ChannelEvent | None]` for the
first device write recorded after the call. The result has `at` (a `Duration`), `direction`
(`"out"`) and `data` (bytes). With `data`, one chunk must contain those literal bytes;
chunks are not joined, and regular expressions and strings are not accepted. `None` and
`b""` match any write. A non-bytes filter raises `TypeError`, for example
`SppPeer.next_write: data is 'AT', not bytes or None`.
The description is `the next write to ESP_SPP_ACCEPTOR's SPP channel (00:00:00:00:5B:01)`
with ` containing b'AT'` when filtered.

The boundary is append order, even for writes at the same timestamp or in one run slice.
It survives channel closure and reopening. Peer answers (`"in"`) never match. Polling
consumes nothing: repeated calls return the same first matching event and independent
conditions can see the same event. Each poll looks only at the events since the last one, so a
long wait on a busy channel costs no more per poll than a short one. Treat `peer.events` as
history; do not clear, reorder or change it while conditions are outstanding: a poll does not
look again at an event it has seen.

These conditions belong to `SppPeer`; `BluetoothDevice` has no SPP channel. There are no
`next_open()` or `next_close()` events: a state condition does not detect a close and reopen
between polls. Wait for `is_closed` before triggering a reconnect when both states matter.

## Reading the device's writes as lines

For a device that talks to the peer in a line-based protocol, such as AT commands that each end in
a carriage return, `peer.written_lines()` joins the chunks the device wrote and cuts them into
lines, and `peer.next_written_line()` waits for the next one. No model is needed for a test that
only checks what was sent.

```python
from esp32tl import WrittenLine

command = peer.next_written_line(b"AT+CONNECT")  # Capture the boundary first.
board.reset()
line = board.wait_until(command, within=seconds(30))
assert line == WrittenLine(at=line.at, data=b"AT+CONNECT=1", opening=0, complete=True)
assert [line.data for line in peer.written_lines()] == [b"AT", b"AT+CONNECT=1"]
```

`written_lines(separator: bytes = b"\r")` returns a list of `WrittenLine(at, data, opening,
complete)` named tuples, in order:

| Field | What |
|---|---|
| `at` | The `Duration` of the chunk that brought the line's separator: a command written in two chunks is one line, at the time of the second. For a line cut off by a close, the time of its last byte |
| `data` | The line's bytes, without the separator; empty for two separators in a row. Not decoded |
| `opening` | Which opening of the channel the line was written on: `0` for the first, `1` after the device closed the channel and opened it again, and so on |
| `complete` | `True` for a line that ended in the separator, `False` for what was left after the last separator when the channel closed |

`separator` is any non-empty bytes, the default `b"\r"`: the carriage return that ends an AT
command. A separator of two bytes, such as `b"\r\n"`, is found even when the device writes them in
two chunks. With `b"\r"`, a device that ends its lines in `\r\n` starts every line after the first
with `b"\n"`: give the separator it uses.

Each opening of the channel is cut apart: a partial line at a channel's close is a line of its own,
with `complete=False`, and is never joined with the first line of the next opening. What the open
channel has after its last separator is not a line yet, as a console line that has not ended is
not one. The peer's answers (`"in"` events) are never lines.

`next_written_line(data: bytes | None = None, separator: bytes = b"\r")` returns a
`Condition[WrittenLine | None]` for the first complete line, cut at `separator`, that ends after the
call. With `data`, the line must contain those literal bytes; `None` and `b""` match any line. The
boundary rules are those of `next_write`: a line that ends after the call is found even when its
first bytes came before the call, at the same timestamp, or after the channel was closed and opened
again; a line cut off by a close never matches; polling consumes nothing, so repeated calls return
the same line. The description is
`the next line ending in b'\r' written to ESP_SPP_ACCEPTOR's SPP channel (00:00:00:00:5B:01)`,
with ` containing b'AT'` when filtered.

A `data` that is not bytes or `None` raises `TypeError`:
`SppPeer.next_written_line: data is 'AT', not bytes or None`. A separator that is not bytes raises
`TypeError`, `SppPeer.written_lines: separator is '\r', not bytes`, and an empty one `ValueError`,
`SppPeer.written_lines: separator is empty: give the bytes a line ends in` (each naming the method
called).

The console reads its lines differently: `console.line()` and `console.lines()` end a line at
`\n`, drop a `\r` before it, decode the text and match regular expressions. SPP carries bytes of
any protocol, so the peer's lines stay bytes, at a separator the test chooses
([decision](../decisions/harness.md#the-devices-spp-writes-are-read-as-lines-at-a-separator-the-test-chooses)).
