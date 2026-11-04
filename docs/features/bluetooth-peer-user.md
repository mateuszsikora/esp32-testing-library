# Bluetooth peer

Bluetooth Classic devices for the firmware to talk to, or to find: `SppPeer` can be discovered by
its name, pairs with a PIN, and offers the Serial Port Profile (SPP), whose bytes a model in the
test answers, on the board's clock; `BluetoothDevice` is the same device with no SPP. They run on
[Bumble](https://github.com/google/bumble)'s virtual link, in the harness's process.

esp32sim has no Bluetooth Classic controller for the firmware yet, so no fixture puts a peer in
front of firmware: today a peer is used from Python, on an `Air`, with another Bumble device in the
ESP32's place, as esp32tl's own tests do (`tests/test_spp_peer.py`).

## Putting a peer on the air

```python
import asyncio

from esp32tl.bluetooth.air import Air
from esp32tl.bluetooth.peer import SppPeer
from esp32tl.units import ms, seconds


class Elm:
    """Answers every line with the line and a prompt, 50 ms later."""

    def receive(self, t, data):
        return [(t + ms(50).ns, data.rstrip(b"\r") + b"\r\r>")]

    def poll(self, now):
        return ()

    def quiet_until(self):
        return None

    def latency(self):
        return ms(50).ns


air = Air()
peer = SppPeer(air, name="OBDII", pin="1234", source=Elm())
...
air.run(the_device_side(), within=seconds(10))
assert peer.events == [(1_000_000_000, "out", b"ATZ\r"), (1_050_000_000, "in", b"ATZ\r\r>")]
air.close()
```

`SppPeer(air, *, name, pin, source, address="00:00:00:00:5B:01", channel=1, secure=True,
class_of_device=0x001F00, rssi=-50)`:

| Argument | What it is |
|---|---|
| `name` | The name the peer is discovered by, in its extended inquiry response: a string of 1 to 238 bytes in UTF-8, the longest the response holds whole |
| `pin` | The PIN it pairs with: a string of 1 to 16 bytes in UTF-8 |
| `source` | What answers the bytes on the SPP channel (below) |
| `address` | Its Bluetooth address, six pairs of hex digits separated by colons; no other device on the air may have it, so a second peer needs its own |
| `channel` | The RFCOMM channel of its SPP service, 1 to 30 |
| `secure` | Whether the channel opens only on a link the device has authenticated |
| `class_of_device` | Its class of device, three octets, which an inquiry reports |
| `rssi` | The signal strength an inquiry hears it at, in dBm, -127 to 20, the range of the specification's inquiry results |

From the moment it is made, at the air's current instant, and until the test takes it off the air
([below](#a-peer-that-leaves)), the peer is:

- **Discoverable and connectable.** An inquiry finds it with its address, its class of device,
  `0x001F00` (uncategorised) unless given another, its signal strength and its name.
- **Paired with its PIN.** It pairs the way Bluetooth 2.0 devices do, with a PIN (legacy
  pairing), and never with Secure Simple Pairing. A device that gives another PIN fails to
  authenticate and is disconnected. The peer keeps the key of each device it paired with until
  `peer.forget_keys()`, so a device that paired once authenticates later without a PIN; a device
  that kept a key the peer does not have fails to authenticate. A link the device authenticated can be
  encrypted.
- **Offering SPP.** Its SDP record names the Serial Port service (`0x1101`) on RFCOMM `channel`. The
  channel opens to one device at a time, also when two ask at the same instant, and, with `secure`,
  only on a link that device has authenticated, with its PIN or its key; encryption is the device's
  choice. The peer does not start the pairing itself, so a device that opens the channel without
  pairing is refused, as is a second device while the first holds it. The device sees its channel
  refused (Bumble raises `ConnectionError` with `CONNECTION_REFUSED`), and `peer.refused` lists each
  refusal as `(t, why)`, as `(0, "24:0A:C4:00:00:01: the link is not authenticated")` or
  `(0, "24:0A:C4:00:00:02: the channel is held by 24:0A:C4:00:00:01")`. With `secure=False`, any
  device opens it. `peer.channel_open` says whether a device holds it now; the channel closes when
  the device closes it or the link.

## Other devices

A device that offers no SPP, there to be found among others, or to be the wrong one:

```python
from esp32tl.bluetooth.peer import BluetoothDevice

phone = BluetoothDevice(
    air, name="phone", class_of_device=0x5A020C, rssi=-70, address="00:00:00:00:5C:01"
)
peer.rssi = -80
```

`BluetoothDevice(air, *, name, pin=None, class_of_device=0x001F00, rssi=-50,
address="00:00:00:00:5B:01")` takes the arguments of the same name in the table above; two devices
on one air need their own addresses. It has what `SppPeer` has apart from the channel and the source:
it leaves and returns as [below](#a-peer-that-leaves), and keeps its keys. It offers no service: an
SDP search for the Serial Port service finds nothing, and RFCOMM cannot be opened to it. Without a
`pin`, a device that tries to pair with it fails with `PIN_OR_KEY_MISSING`.

An inquiry finds every device on the air, each with its own class and signal strength, in the order
of their addresses. `rssi` can be set at any time, on either class, and the next inquiry gets it. It
reaches the device in the RSSI and extended inquiry modes (Bumble asks for extended); the plain
`Inquiry_Result` has no such field. Nothing else depends on it: a device at -127 dBm connects as one
at 20.

Secure Simple Pairing is not modelled on these devices: they pair with a PIN, as `SppPeer` does.

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

Link keys stay either way, and `forget_keys()` is the only way to lose them; it applies from the
peer's next link on. No specification or vendor document says what a given device keeps after
losing power: that it keeps its keys is this model's assumption, as are the sources' `supply` and
what a power loss does to what a source had queued. A test about a device that loses them calls
`forget_keys()`.

All of this has been exercised only on an `Air`, with a Bumble device in the ESP32's place: no
firmware has seen a peer leave. What ESP-IDF does with these events is not known yet.

## The model that answers

`source` is what drives the SPP channel from the peer's end, written as for the far end of a UART:
an object with four methods, all times in nanoseconds of the air's clock.

| Method | Called | Returns |
|---|---|---|
| `receive(t, data)` | When the device writes `data` on the channel, at `t`, the instant it arrives | The answers, `(t, bytes)` pairs, each at `t` or later |
| `poll(now)` | At every instant the air stops at, and possibly more than once at one | What it sends unprompted: `(t, bytes)` pairs, each at `now` or later |
| `quiet_until()` | Whenever the air looks for its next instant | The next time it has something to send unprompted, an `int` later than `now`; `None` for nothing |
| `latency()` | Never, by the peer | The shortest time in which it answers, as a UART's source says it |

The bytes the device writes come in the pieces the device wrote them in; a model that answers
lines finds them by their terminator. Each answer goes out at its own time, never earlier; answers
of the same time go out in the order they were given; empty bytes are dropped. What the source
sends while no device holds the channel is lost, as it would be on the air: `peer.lost` counts its
bytes. A source that raises, or returns what the peer cannot send, fails the air (below).

`peer.events` holds what crossed the channel, in order, as `(t, direction, bytes)`: `out` of the
device, `in` to it, as a tap's recording names them.

## Time on the air

The air has a clock of its own, which starts at 0 when the `Air` is made and moves only when the
test moves it; no board runs beside it yet, so it is not `board.now`. Nothing on the air reads the
host's clock, so two runs do the same at the same instants.

| Call | What it does |
|---|---|
| `air.run(awaitable, *, within, description="")` | Runs a coroutine on the air, the device's side, until it completes, for at most `within`, a `Duration`, from now; returns its result or raises what it raised |
| `air.wait(duration)` | Lets the air run for `duration` |
| `air.advance(until)` | Moves the air to `until`, a `Duration` since 0 |
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
  holds on this air only.

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
| `OBDII: source.receive raised KeyError: 'model bug'` | `AirError`, from `PeerError`, from the source's exception | The source raised, in `receive`, `poll`, `quiet_until` or `supply` |
| `OBDII: source.receive sent b'too early' at -0.000000001 s, and the air is at 0 s: it cannot send into the past` | `AirError`, from `PeerError` | The source returned a time before the instant it was asked at |
| `OBDII: source.receive returned (0, 'text'), not a (t_ns, bytes) pair` | `AirError`, from `PeerError` | The source returned something else |
| `OBDII: source.quiet_until returned 1.5, not nanoseconds` | `AirError`, from `PeerError` | `quiet_until` returned something other than an `int` or `None` |
| `OBDII: source.quiet_until returned 0 s, not after the air's 0 s: what it sends by then belongs in the poll at 0 s` | `AirError`, from `PeerError` | `quiet_until` returned a time not after the air's |
| `{description} did not complete within 10 s: waited from 0 s to 10 s of the board's time` | `NeverHappened` | `air.run` ran out of `within`; `the awaitable` without a `description` |
| `the air is at 1 s: it cannot go back to 0.5 s` | `ValueError` | `air.advance` to a time before the air's |
| `air.advance takes a Duration, as seconds(1), not 1` | `TypeError` | `air.advance(1)`: a plain number where a `Duration` belongs |
| `air.wait takes a Duration, as seconds(1), not 1` | `TypeError` | `air.wait(1)` |
| `air.run: within takes a Duration, as seconds(1), not 1` | `TypeError` | `air.run(..., within=1)` |
| `HCI_Error(hci/AUTHENTICATION_FAILURE_ERROR [0x5])` | Bumble's `HCI_Error`, on the device's side | The device gave a PIN other than the peer's |
| `HCI_Error(hci/PIN_OR_KEY_MISSING_ERROR [0x6])` | Bumble's `HCI_Error`, on the device's side | The device authenticated with a key the peer does not keep |
| `HCI_Error(hci/INSUFFICIENT_SECURITY_ERROR [0x2F])` | Bumble's `HCI_Error`, on the device's side | The device asked for encryption on a link it had not authenticated |
| `hci/PAGE_TIMEOUT_ERROR`, in `error_code` | Bumble's `core.ConnectionError`, on the device's side | Nobody answered the page: the peer has left, its end of an old link has not fallen, or no device has the address |
| `HCI_Error(hci/PAGE_TIMEOUT_ERROR [0x4])` | Bumble's `HCI_Error`, on the device's side | Nobody answered `Device.request_remote_name` |
| `HCI_Error(hci/CONNECTION_ALREADY_EXISTS_ERROR [0xB])` | Bumble's `HCI_Error`, on the device's side | The device paged a peer while it holds a link to it, as one that lost contact and has not fallen |
| `HCI_Error(hci/CONNECTION_TIMEOUT_ERROR [0x8])` | Bumble's `HCI_Error`, on the device's side | The link fell during `Connection.authenticate()` |

A `BluetoothDevice` says the same of its arguments, starting `BluetoothDevice:` rather than
`SppPeer:`. `PeerError` is a `ValueError`, in `esp32tl.bluetooth.peer`; `AirError` a `RuntimeError`, in
`esp32tl.bluetooth.air`, whose `__cause__` is what failed: whatever raises on the air, in a model
or in Bumble, is raised from the call that moved the time. What the peer finds wrong with its
source keeps the peer's message; anything else that raises on the air, in Bumble or in the test's
own coroutine's callbacks, is prefixed with asyncio's description of where it was raised, such as
`Exception in callback Host.on_packet(): …`.
`NeverHappened` is the board's, from `esp32tl.interfaces`.

## See also

- [Bluetooth peer: how it works](bluetooth-peer-technical.md)
- [Models and units](models-user.md): `Duration` and the other units
- [Decisions: the Bluetooth peer](../decisions/harness.md#the-bluetooth-peer-runs-bumble-in-the-harness-on-the-boards-clock)
- [Decisions: how a peer leaves](../decisions/harness.md#a-peer-leaves-without-a-word-keeps-its-keys-and-its-old-link-stays-dead-until-it-falls)
