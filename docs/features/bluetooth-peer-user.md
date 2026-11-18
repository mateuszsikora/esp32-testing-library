# Bluetooth peer

Bluetooth Classic devices for the firmware to talk to, or to find: `SppPeer` can be discovered by
its name, pairs with a PIN or with Secure Simple Pairing, and offers the Serial Port Profile (SPP),
whose bytes a model in the test answers, on the board's clock; `BluetoothDevice` is the same device with no SPP. They run on
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
the same object, asked for at setup. Below, `bt` is short for either. From then on:

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
  set, `peer.switch_off()` and the other calls of [a peer that leaves](#a-peer-that-leaves), and a
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
| `board.bluetooth.air` | The `Air` of the board's devices, on its clock |
| `board.bluetooth.controller` | The firmware's controller, a `GuestController` at `24:6F:28:00:11:24` |
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
`authenticated` each; [Secure Simple Pairing](#secure-simple-pairing)) and `devices` (`name`,
`address`, `present` each); `null` without `board.bluetooth`. Between `unknown commands` and
`devices` the report lists the firmware's pairings under `pairings (N):`. After
`board.bluetooth.tap(path)` the first line ends with `; tapped into {path}`.

### The firmware's controller

It does what the devices' controllers do ([time on the air](#time-on-the-air),
[a peer that leaves](#a-peer-that-leaves)), and what Bluedroid needs of a controller:

- Three pages of features, 0 to 2, as the classic ESP32's controller has (Bluedroid stops on a
  fourth), page 0 with the BR/EDR features: encryption, role switch, the RSSI and extended inquiry
  results, Secure Simple Pairing, and none for LE, since the firmware runs its controller for
  Classic alone. TODO(owner): what the classic ESP32's controller reports on page 0.
- An ACL buffer of 491 bytes, the most a packet of esp32sim's VHCI holds (496 bytes, 5 of them the
  packet's header), both ways: the firmware's own data, and the peer's data cut to fit, also to the
  firmware's `Host_Buffer_Size` when that is smaller. TODO(owner): what the classic ESP32's
  controller reports in `Read_Buffer_Size`.
- An answer to every command: one it does not know gets `UNKNOWN_HCI_COMMAND_ERROR` in the event the
  host waits for, `Command_Status` for a command answered so before it completes, `Command_Complete`
  for any other, and is listed in `bt.unknown_commands()`.
- Each answer `10 µs` after the instant the packet it answers was sent, which the firmware reads at
  its next poll of the VHCI.

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
```

Each call takes effect at the air's time, from the test or from a coroutine on the air. Power and
range are apart: a peer switched off and out of range is `present` again once both are undone.
Saying what is already so changes nothing.

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

## The model that answers

`source` is what drives the SPP channel from the peer's end, written as for the far end of a UART
([a UART's own model](pytest-plugin-user.md#a-uarts-own-model)): a subclass of
`esp32tl.Model`, whose times are `Duration`s of the air's clock, or an object with the
four methods, whose times are nanoseconds as `int`s. Both kinds put the same bytes on the channel at
the same instants; when to prefer nanoseconds is said there.

| Method | Called | Returns |
|---|---|---|
| `receive(at, data)` | When the device writes `data` on the channel, at `at`, the instant it arrives | The answers, `(at, bytes)` pairs, each at `at` or later |
| `poll(now)` | At every instant the air stops at, and possibly more than once at one | What it sends unprompted: `(at, bytes)` pairs, each at `now` or later |
| `quiet_until()` | Whenever the air looks for its next instant | The next time it has something to send unprompted, later than `now`; `None` for nothing |
| `latency()` | Never, by the peer | The shortest time in which it answers, as a UART's source says it |

A `Model` defines all four, as a source in nanoseconds does. `Echo` in nanoseconds would return
`[(t + ms(50).ns, ...)]` from `receive` and `ms(50).ns` from `latency`.

The bytes the device writes come in the pieces the device wrote them in; a model that answers
lines finds them by their terminator. Each answer goes out at its own time, never earlier; answers
of the same time go out in the order they were given; empty bytes are dropped. What the source
sends while no device holds the channel is lost, as it would be on the air: `peer.lost` counts its
bytes. A source that raises, or returns what the peer cannot send, fails the air (below).

`peer.events` holds what crossed the channel, in order, as `ChannelEvent(at, direction, data)` named tuples: `out` of the
device, `in` to it, as a tap's recording names them.

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
| `bench: source.receive returned (seconds(0), 'text'), not a (Duration, bytes) pair` | `AirError`, from `PeerError` | A `Model` returned something else |
| `bench: source.quiet_until returned 1.5, not nanoseconds` | `AirError`, from `PeerError` | `quiet_until` returned something other than an `int` or `None` |
| `bench: source.quiet_until returned 1.5, not a Duration` | `AirError`, from `PeerError` | A `Model`'s `quiet_until` returned something other than a `Duration` or `None` |
| `bench: source.quiet_until returned 0 s, not after the air's 0 s: what it sends by then belongs in the poll at 0 s` | `AirError`, from `PeerError` | `quiet_until` returned a time not after the air's |
| `{description} did not complete within 10 s: waited from 0 s to 10 s of the board's time` | `NeverHappened` | `air.run` ran out of `within`; `the awaitable` without a `description` |
| `air.wait: this air is on the board's clock, which only the board's link moves: let the board run with board.wait() or board.wait_until()` | `AirError` | `air.wait`, `air.advance` or `air.run`, each named, on `bt.air` |
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

## See also

- [Bluetooth peer: how it works](bluetooth-peer-technical.md)
- [Models and units](models-user.md): `Duration` and the other units
- [Decisions: the Bluetooth peer](../decisions/harness.md#the-bluetooth-peer-runs-bumble-in-the-harness-on-the-boards-clock)
- [Decisions: how a peer leaves](../decisions/harness.md#a-peer-leaves-without-a-word-keeps-its-keys-and-its-old-link-stays-dead-until-it-falls)
- [Decisions: the air of the `bt` fixture is on the board's clock](../decisions/harness.md#the-air-of-the-bt-fixture-is-moved-by-the-boards-link-alone)
- [esp32sim backend: Bluetooth](esp32sim-backend-user.md#bluetooth)

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
conditions can see the same event. Treat `peer.events` as history; do not clear or reorder it
while conditions are outstanding.

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
