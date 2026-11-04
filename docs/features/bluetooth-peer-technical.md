# Bluetooth peer: how it works

Three modules in `esp32tl/bluetooth/`: `air.py`, the loop on the board's clock and the virtual
link; `controller.py`, Bumble's virtual controller with what Classic devices need and Bumble
0.0.236 lacks; `peer.py`, the device and the SPP peer. What the user sees is in
[bluetooth-peer-user.md](bluetooth-peer-user.md).

## Data flow

1. **The air.** `Air()` makes a `BoardLoop`, an `asyncio.SelectorEventLoop` whose `time()` is
   `now / 1e9`, `now` an `int` of nanoseconds that only `Air` moves, and a `Link`, a Bumble
   `LocalLink` that refuses delivery (below).
   The loop's selector, `_Selector`, never blocks: when asyncio asks it to wait, with a timeout
   other than 0, nothing is left to do at this instant, so it stops the loop and keeps the
   timeout, the distance to the loop's next timer (`None` for none). `BoardLoop.run_idle` runs the
   loop until that happens and returns the next timer's time, rounded to the nearest nanosecond and
   at least a nanosecond on. `_clock_resolution` is 1 ns, so a timer that rounds to the instant
   fires there. Rounding up would stop a nanosecond late wherever the float lands above it: a timer
   5.12 s after 49 ms, for one.
2. **Settling an instant.** `Air._settle` schedules every party's `poll(now)` with `call_soon`,
   then runs the loop idle. `Air.call(function)` runs `function` inside the loop the same way, or
   at once when called from inside it; `Air.start(coroutine)` makes a task there. Bumble's `Controller` takes the running loop in its
   constructor, so everything Bumble is made through `call`.
3. **Moving time.** `Air.advance(until)`, a `Duration` checked by `nanoseconds`, goes to
   `Air._advance(ns)`, which settles the current instant, then, while
   `quiet_until()`, the minimum of the loop's next timer and every party's `quiet_until`, is at or
   before `until`, sets `now` to it and settles; then sets `now` to `until` and settles. An
   instant may be settled more than once. `wait(d)` is `_advance(now + d)`; `run(awaitable,
   within=)` starts a task and advances to each next instant until the task is done, or to the
   deadline, where it cancels the task and raises `NeverHappened`.
4. **Failures.** The loop's exception handler, `Air._failed`, keeps the first exception asyncio
   reports, from a callback, a task, or a task whose exception nobody retrieved, with asyncio's
   message; `Air.fail(error)` keeps one a party found, without a message of asyncio's. `_settle`
   raises it as an `AirError`, `{asyncio's message}: {error}` or `{error}`, with the exception as
   its cause, and from then on every settle raises it again. A party whose `quiet_until` raises
   fails the air the same way, from `Air.quiet_until`.
5. **A device is made** by `BluetoothDevice.__init__`, which `SppPeer.__init__` calls once it has
   checked `pin` and `channel`: inside the loop, a `Controller` at `address` on the
   air's link, a Bumble `Device` on it through a `Host` and an `AsyncPipeSink`, configured Classic
   only (`le_enabled=False`), with `classic_ssp_enabled=False` and `classic_sc_enabled=False`, its
   class of device `class_of_device`; and a pairing delegate, `_Pin`, of IO capability
   `KEYBOARD_INPUT_ONLY`, which Bumble's host asks for the PIN on a PIN request, and which gives
   none for a device without one. Then `_serve(device)`, the services: nothing in
   `BluetoothDevice`, so its SDP server has no record and no `rfcomm.Server` listens. `SppPeer._serve`
   adds an SDP record from
   `rfcomm.make_service_sdp_records(RECORD_HANDLE, channel, BT_SERIAL_PORT_SERVICE)` and an
   `rfcomm.Server` listening on `channel`, whose `EVENT_START`, emitted with each new
   multiplexer, replaces the multiplexer's `acceptor` with `SppPeer._accept` for that link. Before
   any of it, the address is parsed by `_address` and looked up with
   `LocalLink.find_classic_controller`. Then `power_on`, `set_discoverable` and
   `set_connectable` run with `air.run(..., within=Duration(0))`: they must complete at the
   instant. The keystore is Bumble's default, a `MemoryKeyStore`. Last, an `SppPeer` joins the air
   as a party; a `BluetoothDevice` has no schedule of its own. `BluetoothDevice.rssi` reads and,
   checked by `_check_rssi`, writes `Controller.rssi`.
6. **The channel.** Bumble's multiplexer asks its acceptor when the device's PN command names the
   channel. `_accept` refuses, by returning `None`, so that Bumble answers DM, while another link
   holds the channel, and, with `secure`, on a link the peer's controller does not count as
   authenticated (`Controller.authenticated`): Bumble's host on the responder's side is never told,
   as the specification sends `Authentication_Complete` to the initiator only. Each refusal goes
   into `refused` with the air's time. Otherwise the link holds the channel from that moment,
   `_holder`, so that a second device whose PN comes before the first one's SABM is refused too, and
   `_accept` returns the server's frame size and credits; a link that drops, or an RFCOMM session
   that closes, before its channel opens lets it go. When the DLC opens, `SppPeer._opened` keeps it
   and sets its sink to `_received`; the DLC's close event forgets it and its holder, as when the
   link drops. Bumble 0.0.236's DLC answers the device's DISC with UA and stays open, so `_opened`
   wraps the DLC's `on_disc_frame` to also drop it from the multiplexer's `dlcs` and abort it, which
   closes it; a new Bumble may close it itself, and the wrapper is then to go. `_received(data)`
   appends `(now, "out", data)` to `events`, gives `source.receive(now, data)` to `_queue`, and
   flushes. `poll(now)`, the party's, does the same with `source.poll(now)`. Both run inside the
   loop and hand a `PeerError` to `Air.fail`.
7. **The answers.** `_queue` calls the source's method through `_call`, which turns an exception
   into a `PeerError` naming the peer and the method, reads what it returns, checks each output,
   a `(t, bytes)` pair with `t` an `int` not before `now`, drops one with empty bytes, and inserts
   the rest into `_pending` by time with `bisect.insort_right`, after those of the same time.
   `_flush(now)` takes every pending output due by `now` in order: with a DLC, appends
   `(t, "in", data)` to `events` and writes it; without, adds its length to `lost`.
   `quiet_until()` checks `source.quiet_until()`, an `int` after the air's instant or `None`,
   and returns the earliest of it and the next pending output. These are the checks `BoardLink`
   makes of a UART's source, with the peer's own messages.
8. **The controller.** `Controller` subclasses Bumble's `Controller`. Both ends of every link are
   of this class: its additions talk to each other over `Link.send_lmp_packet`, which hands
   the packet object to the other controller with `call_soon`. Bumble's own handlers do the rest:
   paging, connection, ACL data, name requests, Secure Simple Pairing.
   - **Inquiry.** `on_hci_inquiry_command` answers with a command status, starts a timer of
     `inquiry_length * INQUIRY_UNIT_S`, and sends one result per other `Controller` on the link
     whose `classic_scan_enable` has inquiry scan (bit 0), sorted by address, in the form
     `inquiry_mode` asks for: `Extended_Inquiry_Result` with the other's `class_of_device`, `rssi` and
     `extended_inquiry_response` in extended mode (2), `Inquiry_Result_With_RSSI` with its `rssi` in
     mode 1, `Inquiry_Result`, which has no such field, in mode 0; clock offset 0. `num_responses` answers, when not 0,
     complete it at once. `Inquiry_Complete` comes from the timer; `Inquiry_Cancel` cancels it and
     sends none. `Write_Class_Of_Device`, `Read_Class_Of_Device`,
     `Write_Extended_Inquiry_Response` and `Write_Inquiry_Mode` keep and give what they are given.
   - **Authentication.** `Authentication_Requested` makes a `Security(initiator=True)` for the
     peer and sends `Link_Key_Request` to the host. With a key (`Link_Key_Request_Reply`), the
     initiator sends `LMP_au_rand` with a random challenge; the other controller answers with
     `LMP_sres` if it has the link's key, and otherwise sends its own host `Link_Key_Request` and
     answers once the host gives the key, or refuses with `LMP_not_accepted`
     (`PIN_OR_KEY_MISSING_ERROR`) when it gives none. The initiator compares the response and
     sends `Authentication_Complete`. Without a key (`Link_Key_Request_Negative_Reply`), it hands
     over to Bumble's Secure Simple Pairing when both controllers have
     `SECURE_SIMPLE_PAIRING_HOST_SUPPORT` in `lmp_features`, read from the other controller on the
     link, as LMP's features exchange would tell; otherwise it pairs with a PIN.
   - **Legacy pairing.** The initiator's host gets `PIN_Code_Request`; its reply sends
     `LMP_in_rand` with a random number, both sides computing the initialisation key from the
     PIN, that number and the responder's address. The responder's host gets `PIN_Code_Request`
     and its reply sends `LMP_accepted`; then each side sends `LMP_comb_key` with a random number
     of its own, and both compute the combination key from the initialisation key, both numbers
     and both addresses. The initiator challenges the responder (`LMP_au_rand`, `LMP_sres`); the
     responder, having answered, challenges the initiator; when the initiator's answer holds, the
     responder sends its host `Link_Key_Notification` (`COMBINATION_KEY`) and the initiator
     `LMP_accepted` for `LMP_sres`, on which the initiator sends its own host
     `Link_Key_Notification` and `Authentication_Complete` with `SUCCESS`. A response that does
     not hold, as from another PIN, makes the side that checked it send its host
     `Authentication_Complete` with `AUTHENTICATION_FAILURE_ERROR` if it is the initiator, send
     `LMP_detach` with that reason, and disconnect; an initiator that gets `LMP_detach` while it
     authenticates sends `Authentication_Complete` with the reason first. A negative reply to a
     PIN request ends the authentication with `PIN_OR_KEY_MISSING_ERROR`; an `LMP_not_accepted`
     for `LMP_in_rand` or `LMP_au_rand` sends `Authentication_Complete` with its error only to an
     initiator's host. LMP packets of these
     kinds for a link already detached on this side are dropped: the responder's challenge
     crosses the initiator's detach.
   - **Encryption.** `Set_Connection_Encryption` on a link authenticated by legacy pairing, by a
     key, or by Bumble's Secure Simple Pairing (`ssp_state.authenticated`) sends
     `LMP_encryption_mode_req` with the mode asked for, which this side keeps until it is
     accepted; the other side sends its host `Encryption_Change` with that mode and
     `LMP_accepted`, on which this side sends its own, with the same mode. On a link not
     authenticated the command status is `INSUFFICIENT_SECURITY_ERROR`.
   - **Keys.** `init_key`, `combination_key` and `response` are SHA-256 over their inputs, each
     length-prefixed and tagged, cut to 16, 16 and 4 bytes: they stand in for E22, E21 and E1,
     whose SAFER+ the link has no use for, since both ends are this class and only the hosts see
     a key. The random numbers come from `random.Random(str(public_address))`.
   - **Paging.** `on_hci_create_connection_command` answers `CONNECTION_ALREADY_EXISTS_ERROR` when
     `classic_connections` holds the peer; otherwise Bumble's handler sends `LmpHostConnectionReq`
     and this one starts a timer of `page_timeout * SLOT_S`, kept in `_paging`. A connection that
     completes cancels it. At the timer, `on_classic_connection_complete` gets
     `PAGE_TIMEOUT_ERROR`; on any failure it drops the entry Bumble's handler made and Bumble's own
     completion leaves. `Write_Page_Timeout` sets `page_timeout`, default `PAGE_TIMEOUT_SLOTS`, and
     refuses 0 with `INVALID_COMMAND_PARAMETERS_ERROR`: the specification's range starts at 1.
     TODO(owner): Vol 1, Part F, 2.11 defines `CONNECTION_ALREADY_EXISTS`, and 7.1.5 does not say
     how a controller reports it for `Create_Connection`; here it is the command status. #34 can see
     what ESP32's controller does.
   - **Remote name requests.** `on_hci_remote_name_request_command` starts a timer of the same
     length, kept in `_naming`, before Bumble's handler sends `LmpNameReq`; the answer cancels it, and
     at the timer the host gets `Remote_Name_Request_Complete` with `PAGE_TIMEOUT_ERROR`, which Vol 1,
     Part F, 2.4 names for this command. A second request to a peer with one pending keeps the
     first's timer.
   - **Link supervision.** Nothing polls on this link, so a link is in contact until a device
     leaves, and the specification's "no packets are received for a duration longer than the
     `Link_Supervision_Timeout`" counts from there. `Controller.left`, on the controller of the
     device that leaves, calls `Link.leave` with the peers it has a link to, not those it is still
     paging, and `_lose_contact` on both ends of each link, which keeps the time in `_lost_contact` and, through `_supervise`, starts a timer to
     `_link_timed_out`; a link that lost contact already keeps its time, and one still being paged
     is left to its page. `_link_timed_out` sends `Authentication_Complete` with
     `CONNECTION_TIMEOUT_ERROR` to an initiator's host still authenticating, then
     `on_classic_disconnected` with that reason. `Write_Link_Supervision_Timeout` needs the central:
     it keeps the value in `_supervision_timeouts`, by peer, sends `LmpSupervisionTimeout`, and
     on a link that lost contact starts the timer again from `_lost_contact`. The peripheral keeps
     what the packet says and sends its host `Link_Supervision_Timeout_Changed`; a packet the link
     refuses leaves the other end at its old value. 0 starts no timer.
     `on_classic_disconnected` cancels the timer and, once neither end holds the link, calls
     `Link.let_go`, as a page that fails does: a target that left when it heard the page may let
     go of its end first. `Controller.power_lost` ends what this side's host has under way, a page
     or a remote name request with `PAGE_TIMEOUT_ERROR`, an inquiry with `Inquiry_Complete`, so that
     no timer of theirs outlives the power and ends the next one; then it runs
     `on_classic_disconnected` on each of this side's links at once. `Controller.returned` calls
     `Link.back`.
   - `Write_Secure_Connections_Host_Support` is answered with `SUCCESS` and changes nothing.
     `lmp_features` loses Bumble's `BR_EDR_NOT_SUPPORTED`.

9. **The link that refuses.** `Link` subclasses Bumble's `LocalLink`, which raises
   `InvalidArgumentError` from `send_lmp_packet` for an address it does not hold and delivers to
   any it does; `remove_controller` alone would fail the air on the next packet, and neither end
   would time out. Whether a device can be reached is decided in `Link.reaches(sender, receiver)`
   and nowhere else: neither is in `_absent`, and the pair is not in `_severed`. `send_acl_data` and
   `send_lmp_packet` drop what it refuses, and `send_lmp_packet` what is for an address nobody has.
   `leave(address, peers)` adds to both; `back(address)` takes the address out of `_absent` and
   leaves `_severed`, so that a link is not revived; `let_go(one, other)` takes the pair out.
   `Controller.on_hci_inquiry_command` asks `Link.present` of both the device that asks, which
   then hears nobody, and each that would answer. If #34 chooses another controller, this
   class is what moves; `SppPeer`'s calls do not.
10. **A device that leaves.** `switch_off`, `switch_on`, `out_of_range` and `in_range` call
    `BluetoothDevice._become(on, in_range)` through `Air.call`. It keeps `_on` and `_in_range`, whose
    conjunction is `present`, and calls `Controller.left` on a change to absent, `power_lost` on a
    change to off, `_supply` on any change of power, and `Controller.returned` on a change to
    present, in that order. `_supply` does nothing in `BluetoothDevice`; `SppPeer._supply` calls
    `source.supply(on)` when the source is `Supplied` (`esp32tl/interfaces/uart.py`), through
    `_call`, and clears `_pending` first. `_flush` counts an
    output in `lost` when no DLC is open or `Link.reaches` refuses the holder's address, so what goes
    out on a link that lost contact does not enter `events`. `forget_keys` puts a new
    `MemoryKeyStore` in `device.keystore`; a link under way keeps the key in its `Security`.

## Interfaces

| Name | What |
|---|---|
| `Air()` | `esp32tl/bluetooth/air.py`: `loop`, a `BoardLoop`; `link`, a `Link`; `now`, a `Duration`; `join(party)`, which settles the current instant with the party in it; `call(function)`; `start(coroutine)`, a task; `fail(error)`; `quiet_until()`, nanoseconds or `None`; `advance(until)`, a `Duration`; `wait(duration)`; `run(awaitable, *, within, description="")`; `close()` |
| `Party` | A protocol: `poll(now)`, inside the loop at every instant the air stops at, returning nothing; `quiet_until()`, later than `now`, or `None` |
| `BoardLoop()` | `now`, an `int` of nanoseconds; `time()`; `run_idle()`, the next timer's time or `None` |
| `AirError` | A `RuntimeError`: what failed on the air, as its `__cause__` |
| `Link()` | `esp32tl/bluetooth/air.py`: Bumble's `LocalLink`; `present(address)`, `reaches(sender, receiver)`, `leave(address, peers)`, `back(address)`, `let_go(one, other)` |
| `Controller(name, link, address)` | `esp32tl/bluetooth/controller.py`: Bumble's `Controller` with the additions above; `link`, a `Link`; `class_of_device`, `extended_inquiry_response` (240 bytes), `inquiry_mode`, `rssi`, `page_timeout`, in slots; `authenticated(peer)`; `random_bytes(n)`; `left()`, `returned()`, `power_lost()` |
| `Security` | Dataclass: one link's authentication on one side: `initiator`, `init_key`, `local_rand`, `peer_rand`, `link_key`, `pairing`, `challenge`, `peer_challenge`, `authenticated` |
| `LmpInRand`, `LmpCombKey`, `LmpSres`, `LmpEncryptionModeReq`, `LmpSupervisionTimeout` | LMP packets Bumble does not define, registered with `lmp.Packet.subclass` |
| `init_key(pin, in_rand, responder)`, `combination_key(init, initiator_rand, responder_rand, initiator, responder)`, `response(link_key, challenge, claimant)` | The keys and the response, above |
| `INQUIRY_UNIT_S` | 1.28 |
| `RSSI` | `esp32tl/bluetooth/controller.py`: `-50`, in dBm, a value of ours: nothing says what a device's should be |
| `SLOT_S`, `PAGE_TIMEOUT_SLOTS`, `LINK_SUPERVISION_TIMEOUT_SLOTS` | `0.000625`; `0x2000`, 5.12 s; `0x7D00`, 20 s: Bluetooth Core 5.4, Vol 4, Part E, 7.3.16 and 7.3.42 |
| `BluetoothDevice(air, *, name, pin=None, class_of_device=CLASS_OF_DEVICE, rssi=RSSI, address=ADDRESS)` | `esp32tl/bluetooth/peer.py`: `device`, the Bumble `Device`; `address`; `name`, `pin`, `class_of_device`; `rssi`, read and written; `present`; `switch_off()`, `switch_on()`, `out_of_range()`, `in_range()`, `forget_keys()` |
| `SppPeer(air, *, name, pin, source, address=ADDRESS, channel=CHANNEL, secure=True, class_of_device=CLASS_OF_DEVICE, rssi=RSSI)` | A `BluetoothDevice`, also: `source`, `channel`, `secure`; `events`; `refused`, `(t, why)` pairs; `lost`; `channel_open`; the party's `poll(now)` and `quiet_until()` |
| `ADDRESS`, `CLASS_OF_DEVICE`, `CHANNEL`, `NAME_MAX`, `RECORD_HANDLE` | `"00:00:00:00:5B:01"`, an address no real device in a test is likely to have; `0x001F00`; `1`; `238`, the extended inquiry response's 240 bytes less the name field's length and type; `0x00010001`, the first SDP record handle a server may give a record of its own |
| `PeerError` | A `ValueError`: what a peer cannot be, or what its source sent that it cannot send |
| `Source` | `esp32tl/interfaces/uart.py`: `receive(t, data)`, `poll(now)`, `quiet_until()`, `latency()`; the peer never calls `latency` |

## Bumble's internals it relies on

`bumble.link.LocalLink`: `send_acl_data`, `send_lmp_packet`, `controllers`,
`find_classic_controller`. `bumble.controller.Controller`: `classic_connections`,
`on_classic_connection_complete`, `on_classic_disconnected`, `on_classic_remote_name_response`,
`on_hci_create_connection_command`, `on_hci_remote_name_request_command`, the
`on_hci_<command>` dispatch by name, `on_lmp_packet`, `send_lmp_packet`, `send_hci_packet`.
Connection state is keyed by address, so a refused delivery is by address too.

## Configuration

None. `bumble` is a dependency of esp32tl, pinned exactly in `pyproject.toml`
([development.md](../guides/development.md#dependencies)).

## Failure modes

| Message | Exception | Cause |
|---|---|---|
| `{kind}: name is {name!r}, not a string of 1 to 238 bytes` | `PeerError` | `name` not a `str`, or its UTF-8 empty or over `NAME_MAX` bytes |
| `{kind}: pin is {pin!r}, not a string of 1 to 16 bytes` | `PeerError` | `pin` not a `str`, or its UTF-8 empty or over 16 bytes; `None` too for an `SppPeer` |
| `SppPeer: channel is {channel!r}, not an RFCOMM channel, 1 to 30` | `PeerError` | `channel` a `bool`, not an `int`, or out of range |
| `{kind}: class_of_device is {class_of_device!r}, not three octets, 0 to 0xFFFFFF` | `PeerError` | `class_of_device` a `bool`, not an `int`, or out of range |
| `{kind}: rssi is {rssi!r}, not a signal strength in dBm, -127 to 20` | `PeerError` | `_check_rssi`, from the constructor and the setter: `rssi` a `bool`, not an `int`, or out of range |
| `{kind}: address is {address!r}, not a Bluetooth address, as 00:00:00:00:5B:01` | `PeerError` | `_address`: not a `str` of six colon-separated pairs of hex digits |
| `{kind}: address {address} is another device's on the air: give each its own` | `PeerError` | A controller on the link has that public address |
| `{name}: source.{method} raised {type}: {error}` | `PeerError`, from the source's exception, raised on the air | `_call`, for `receive`, `poll`, `quiet_until` and `supply` |
| `{name}: source.{method} returned {output!r:.80}, not a (t_ns, bytes) pair` | `PeerError`, raised on the air | `_queue` |
| `{name}: source.{method} sent {data!r:.80} at {t}, and the air is at {now}: it cannot send into the past` | `PeerError`, raised on the air | `_queue` |
| `{name}: source.quiet_until returned {bound!r}, not nanoseconds` | `PeerError`, raised on the air | `quiet_until`: not an `int`, or a `bool` |
| `{name}: source.quiet_until returned {t}, not after the air's {now}: what it sends by then belongs in the poll at {now}` | `PeerError`, raised on the air | `quiet_until` |
| `{error}`, `{message}: {error}` | `AirError` | `_settle` or `Air.quiet_until`, with what `Air.fail` kept, or with what the loop's exception handler kept and asyncio's `message`, such as `Exception in callback Host.on_packet()` or `Task exception was never retrieved` |
| `something on the air is due at {t}, and the air is at {now} already` | `AirError` | A party's `quiet_until` at or before `now`; the peer checks its source's before this can happen |
| `the air is at {now}: it cannot go back to {until}` | `ValueError` | `advance` |
| `{description} did not complete within {within}: waited from {start} to {end} of the board's time` | `NeverHappened` | `run`; `description` is `the awaitable` when not given |
| `air.advance takes a Duration, as seconds(1), not 1`, `air.run: within takes a Duration of 0 or more, not …` | `TypeError`, `ValueError` | From `nanoseconds` in `esp32tl/backends/clock.py`, for `advance`, `wait` and `run` |

`{kind}` is the class's name, `BluetoothDevice` or `SppPeer`. On the device's side, Bumble's host
raises `HCI_Error` from `Connection.authenticate()` with the
status of `Authentication_Complete`, and from `Connection.encrypt()` with the command status; its
`Multiplexer.open_dlc` raises `core.ConnectionError` with `CONNECTION_REFUSED` on the DM of a
channel the peer refused. `Device.connect` raises `core.ConnectionError` with `PAGE_TIMEOUT_ERROR`
in `error_code` on a failed `Connection_Complete`, and `HCI_Error` with
`CONNECTION_ALREADY_EXISTS_ERROR` on the command status. A connection's `EVENT_DISCONNECTION` gives
`CONNECTION_TIMEOUT_ERROR` as its reason when a link falls.

## Tests

- `tests/test_air.py`: the loop's timers at their board time; an hour of it in well under a minute
  of wall time; `run`'s `NeverHappened` and the cancelled task; no going back, and a plain number
  refused; a party polled at its own instants and at the loop's; a party due in the past; a
  callback that raises.
- `tests/test_spp_peer.py`: a stand-in for the ESP32, a Bumble `Device` on a `Controller` that does
  what ESP-IDF's `bt_spp_initiator` does: inquiry until the name, connect, authenticate, encrypt,
  SDP for the Serial Port service, RFCOMM. The peer found by name and class of device, also by a
  name of 238 bytes; a PIN asked once; the model hearing the write at its instant and the answer at
  its time; the same key on both sides; another PIN failing and detaching with no key kept; no PIN
  given; a second connection authenticating with the key, no PIN; a peer that lost the key refusing;
  a device with Secure Simple Pairing pairing with the peer's PIN; an inquiry of two peers in
  address order, completing after 10.24 s; the channel to one device at a time, also to two that ask
  at one instant, only on an authenticated link, encrypted or not, unless `secure=False`, closed by
  the device's DISC or by the link, and opened again; a peer with something due the instant it
  joins; unprompted bytes lost before the channel opens and answers lost after it closes; sources
  that raise, answer into the past, return what is not a pair or an empty answer, or give a wrong
  `quiet_until`; two runs with the same `events`; Bumble's Secure Simple Pairing between two
  controllers that support it; encryption refused on a link not authenticated, and turned off on
  both sides; the air quiet until the device writes; the peer's refusals of its arguments and of an
  address taken. A peer that leaves, either way: the link falling at exactly the supervision timeout
  with no detach; a page ending at exactly the page timeout, also to an address nobody has; an
  inquiry not finding it until it returns; the key kept across a return, and `forget_keys`; a return
  before the timeout reviving nothing, with `CONNECTION_ALREADY_EXISTS`, another device connecting
  meanwhile, and the old channel still held; a timeout not restarted by leaving again; each timeout
  a host writes, 0, a peripheral's and one for no link; the source told or running on, with
  `lost`; `present`; the calls from a coroutine; a remote name request ending at the page timeout,
  and answered with no timer left; a device leaving during its own page and being reached again,
  also when the target's end falls before the page ends; a power loss ending a page, a name request
  and an inquiry, without their timers ending the next; a device that left finding nothing; two runs with the same `events`, `lost` and
  instants.
  Other devices: several in one inquiry, each with its own class and RSSI; a new `rssi` at the next
  inquiry; RSSI in the RSSI mode and none in the plain one; a device without SPP that has no SDP
  record and no RFCOMM; one without a PIN refusing a pairing, and one with a PIN keeping its key
  across a return; leaving; their refusals of their arguments.
- `tests/test_air.py` also: a timer falling on its exact nanosecond.

Not covered: firmware. esp32sim has no Classic controller for the guest, so no ESP32 image talks
to a peer yet, and no firmware has seen a peer leave. Nor is a clock offset other than 0. A page does not look at
the target's page scan: Bumble's controller delivers it whatever `classic_scan_enable` says, so a
device with `set_connectable(False)` still connects, though an inquiry respects inquiry scan.
`Create_Connection_Cancel` for Classic is not in Bumble 0.0.236, so `Device.connect(timeout=)` cannot
end a page early. Two devices searching the peer's SDP at the same instant fail the air with
`Exception in callback Host.on_packet(): invalid state`: Bumble 0.0.236's `sdp.Server` keeps one
channel at a time.

## See also

- [Bluetooth peer](bluetooth-peer-user.md)
- [Decisions: the Bluetooth peer](../decisions/harness.md#the-bluetooth-peer-runs-bumble-in-the-harness-on-the-boards-clock)
- [Bumble](https://github.com/google/bumble), 0.0.236: `bumble/controller.py`, `bumble/link.py`
- [Bluetooth Core 5.4, Vol 4, Part E](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/Core-54/out/en/host-controller-interface/host-controller-interface-functional-specification.html),
  read on 2026-11-04: 6.6, 6.21, 7.3.16, 7.3.42, 7.7.33, 7.7.38, 7.7.46; [Vol 1, Part F](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/Core-54/out/en/architecture,-mixing,-and-conventions/controller-error-codes.html):
  2.4, 2.8, 2.11
