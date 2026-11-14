# Bluetooth peer: how it works

Four modules in `esp32tl/bluetooth/`: `air.py`, the loop on the board's clock and the virtual
link; `controller.py`, Bumble's virtual controller with what Classic devices need and Bumble
0.0.236 lacks; `guest.py`, the firmware's controller; `peer.py`, the device and the SPP peer. The
interface that puts the firmware's controller on the board's link is
`esp32tl/interfaces/bluetooth.py`, the `bt` fixture. What the user sees is in
[bluetooth-peer-user.md](bluetooth-peer-user.md).

## Data flow

1. **The air.** `Air()` makes a `BoardLoop`, an `asyncio.SelectorEventLoop` whose `time()` is
   `now / 1e9`, `now` an `int` of nanoseconds that only `Air` moves, and a `Link`, a Bumble
   `LocalLink` that refuses delivery (below).
   The loop's selector, `_Selector`, never blocks: when asyncio asks it to wait, with a timeout
   other than 0, nothing is left to do at this instant, so it stops the loop and keeps the
   timeout, the distance to the loop's next timer (`None` for none). `BoardLoop.call_at`, which
   `call_later` and asyncio's own timeouts go through, puts every timer on the nanosecond nearest
   its `when`, as `round(when * 1e9) / 1e9`: the same float that `time()` gives at that
   nanosecond. A sum of floats lands a hair off it: 6 s and 5.12 s are 11.120000000000001 s. Were
   the timer kept there, asyncio would see a wait of about 2e-15 s at 11.12 s, and the selector
   would stop the loop; asyncio would still fire the timer in that iteration, within
   `_clock_resolution`, and what the timer schedules with `call_soon`, such as the
   `Connection_Complete` that ends a page, would run a nanosecond later. So a timer due at the
   instant gives a timeout of exactly 0 and one due later a timeout of about a nanosecond or more.
   A `when` with no nanosecond, as `math.inf` from `asyncio.timeout(math.inf)` or one whose
   nanoseconds overflow a float, stays as it is and never fires.
   `_clock_resolution` is half a nanosecond: the loop fires what is due at the instant, never a
   timer due at the next nanosecond, whose float may land a hair under `time()` plus 1 ns.
   `BoardLoop.run_idle` runs the loop until the selector stops it and returns the next timer's
   time, rounded to the nearest nanosecond and at least a nanosecond on. asyncio waits at most
   `MAXIMUM_SELECT_TIMEOUT`, 24 hours, so for a timer further away, `math.inf` too, it returns
   `now` plus 24 hours: the air stops there at an instant with nothing due, and `quiet_until()` is
   not `None` while such a timer is set.
2. **Settling an instant.** `Air._settle` schedules every party's `poll(now)` with `call_soon`, then
   runs the loop idle: what the parties, the timers due at `now` and what those schedule with
   `call_soon` all run at `now`, and the loop stops only when what is left is a timer after it. An
   advance that stops at an instant and then ends there settles it twice, so only an advance past an
   instant shows whether everything due at it ran there; and a party's poll runs the loop once more
   at every instant, so only an air without parties shows it. `Air.call(function)` runs `function`
   inside the loop the same way, or at once when called from inside it; `Air.start(coroutine)` makes
   a task there. Bumble's `Controller` takes the running loop in its constructor, so everything
   Bumble is made through `call`. Every call that runs the loop or reads its schedule holds
   `Air._lock`, a `threading.RLock` (`fail` and `now` do not): on the board's clock the link's
   thread and the test's call the air, the test only while the board holds the link in a turn, or
   before its first one.
3. **Moving time.** `Air.advance(until)`, a `Duration` checked by `nanoseconds`, goes to
   `Air._advance(ns)`, which settles the current instant, then, while
   `quiet_until()`, the minimum of the loop's next timer and every party's `quiet_until`, is at or
   before `until`, sets `now` to it and settles; then sets `now` to `until` and settles. An
   instant may be settled more than once. `wait(d)` is `_advance(now + d)`; `run(awaitable,
   within=)` starts a task and advances to each next instant until the task is done, or to the
   deadline, where it cancels the task and raises `NeverHappened`; `at_once(awaitable,
   description)` is the same with a `within` of 0. An `Air(board_clock=True)` is moved only by
   `follow(t)`, `_advance` in nanoseconds, which the interface calls: `advance`, `wait` and `run`
   first call `_test_moves`, which raises `AirError` naming the call, and `run` closes a coroutine
   it refuses, which would otherwise be reported as never awaited. `at_once` moves nothing, and
   is allowed.
4. **Failures.** The loop's exception handler, `Air._failed`, keeps the first exception asyncio
   reports, from a callback, a task, or a task whose exception nobody retrieved, with asyncio's
   message; `Air.fail(error)` keeps one a party found, without a message of asyncio's. `_settle`
   raises it as an `AirError`, `{asyncio's message}: {error}` or `{error}`, with the exception as
   its cause, and from then on every settle raises it again. A party whose `quiet_until` raises
   fails the air the same way, from `Air.quiet_until`.
5. **A device is made** by `BluetoothDevice.__init__`, which `SppPeer.__init__` calls once it has
   checked `pin` and `channel`: inside the loop, a `Controller` at `address` on the air's link, a
   Bumble `Device` on it through a `Host` and an `AsyncPipeSink`, configured Classic only
   (`le_enabled=False`), with `classic_ssp_enabled=False` and `classic_sc_enabled=False`, its class
   of device `class_of_device`; and a pairing delegate, `_Pin`, of IO capability
   `KEYBOARD_INPUT_ONLY`, which Bumble's host asks for the PIN on a PIN request, and which gives
   none for a device without one. Its `l2cap_extended_features` are `L2CAP_FEATURES`, Bumble's
   default without `ENHANCED_RETRANSMISSION_MODE`: the L2CAP information response names fixed
   channels and the FCS option only. Then `_serve(device)`, the services: nothing in
   `BluetoothDevice`, so its SDP server has no record and no `rfcomm.Server` listens.
   `SppPeer._serve` adds an SDP record from `rfcomm.make_service_sdp_records(RECORD_HANDLE, channel,
   BT_SERIAL_PORT_SERVICE)`, with, for a `service_name`, the `LanguageBaseAttributeIDList` (0x0006)
   of `LANGUAGE`, English, UTF-8 (106) and base 0x0100, and the name as a text string at 0x0100,
   base plus `SDP_SERVICE_NAME_ATTRIBUTE_ID_OFFSET`; and an `rfcomm.Server` listening on `channel`,
   whose `EVENT_START`, emitted with each new multiplexer, replaces the multiplexer's `acceptor`
   with `SppPeer._accept` for that link, and the L2CAP channel's `sink`, the multiplexer's `on_pdu`,
   with `screened`, which looks at each frame first (below). Before any of it, the address is parsed
   by `_address` and looked up with `LocalLink.find_classic_controller`. Then `power_on`,
   `set_discoverable` and `set_connectable` run with `air.at_once(...)`: they must complete at the
   instant, also on an air on the board's clock. The keystore is Bumble's default, a
   `MemoryKeyStore`. Last, an `SppPeer` joins the air as a party; a `BluetoothDevice` has no
   schedule of its own. `BluetoothDevice.rssi` reads and, checked by `_check_rssi`, writes
   `Controller.rssi`.
6. **The channel.** Bumble's multiplexer asks its acceptor when the device's PN command names the
   channel. `_accept` refuses, by returning `None`, so that Bumble answers DM, while another link
   holds the channel. Otherwise the link holds the channel from that moment, `_holder`, so that a
   second device whose PN comes before the first one's SABM is refused too, and `_accept` returns
   the server's frame size and credits; a link that drops, or an RFCOMM session that closes,
   before its channel opens lets it go. The authentication is checked at the SABM that opens the
   DLC, since Bluedroid, as initiator, authenticates between its PN and its SABM: `screened`
   parses each frame with `RFCOMM_Frame.from_bytes`, and for a SABM on a DLCI other than 0 whose
   DLC is in `multiplexer.dlcs` and `CONNECTING`, the state PN left it in, asks `_opens`. With
   `secure`, on a link the peer's controller does not count as authenticated
   (`Controller.authenticated`; Bumble's host on the responder's side is never told, as the
   specification sends `Authentication_Complete` to the initiator only), `_opens` takes the DLC
   out of `dlcs`, sends DM for its DLCI with `c_r` the opposite of the DLC's, as Bumble's UA has
   it, lets go of `_holder`, and the frame goes no further; the device may authenticate and send PN
   again. Every other frame goes to `on_pdu`. Each refusal goes into `refused` with the air's time,
   through `_refuse`. When the DLC opens, `SppPeer._opened` keeps it
   and sets its sink to `_received`; the DLC's close event forgets it and its holder, as when the
   link drops. Bumble 0.0.236's DLC answers the device's DISC with UA and stays open, so `_opened`
   wraps the DLC's `on_disc_frame` to also drop it from the multiplexer's `dlcs` and abort it, which
   closes it; a new Bumble may close it itself, and the wrapper is then to go. `_received(data)`
   appends `(now, "out", data)` to `events`, gives `_driving.receive(now, data)` to `_queue`, and
   flushes. `poll(now)`, the party's, does the same with `_driving.poll(now)`. Both run inside the
   loop and hand a `PeerError` to `Air.fail`. `_driving`, a property, is `source`, or for a
   `Model` (`esp32tl/sources/model/__init__.py`)
   `in_nanoseconds(source, f"{name}: source", _Misreturned)`, made again whenever `source` is
   another object: it converts each time at the edge, `Duration(now)` in and `.ns` out, and
   refuses what is not a `Duration` with `_Misreturned`, a `PeerError` that `_call` raises as it
   is rather than as something the source raised. `source` stays what the test gave.
7. **The answers.** `_queue` calls the source's method through `_call`, which turns an exception
   into a `PeerError` naming the peer and the method, reads what it returns, checks each output,
   a `(t, bytes)` pair with `t` an `int` not before `now`, drops one with empty bytes, and inserts
   the rest into `_pending` by time with `bisect.insort_right`, after those of the same time.
   `_flush(now)` takes every pending output due by `now` in order: with a DLC, appends
   `(t, "in", data)` to `events` and writes it; without, adds its length to `lost`.
   `quiet_until()` checks `_driving.quiet_until()`, an `int` after the air's instant or `None`,
   and returns the earliest of it and the next pending output. These are the checks `BoardLink`
   makes of a UART's source, with the peer's own messages. For a `Model` the checks of order are
   the same, on the converted times, and their messages, in seconds already, are the same.
8. **The controller.** `Controller` subclasses Bumble's `Controller`. Both ends of every link are
   of this class: its additions talk to each other over `Link.send_lmp_packet`, which hands
   the packet object to the other controller with `call_soon`. Bumble's own handlers do the rest:
   paging, connection, ACL data, name requests, most of Secure Simple Pairing.
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
   - **Secure Simple Pairing.** Bumble's handlers carry it from the IO capability exchange
     (`LMP_io_capability_req` and `_res`) through `LMP_simple_pairing_confirm`,
     `LMP_simple_pairing_number` and `LMP_dhkey_check`; the controller replaces five of Bumble's
     methods and catches two packets. The initiator's negative reply to `Link_Key_Request`, and the
     responder's `LMP_io_capability_req`, give the connection a fresh `SspState` first, since
     Bumble keeps the nonce of the pairing before and its `sent_dhkey_check`.
     `_start_ssp_exchange` takes the local nonce from `random_bytes(16)`, where Bumble takes it
     from `crypto.r()`, `secrets.token_bytes`. `_on_ssp_confirm_or_nonce_received`, once both
     nonces are there, picks the association model with `association(local, peer)`, Bumble's
     choice: Just Works when either IO capability is `NO_INPUT_NO_OUTPUT`, Passkey Entry when
     either is `KEYBOARD_ONLY`, Numeric Comparison otherwise; a side with a display in Passkey
     Entry draws the passkey with `self._random.randrange(1_000_000)`, where Bumble calls
     `secrets.randbelow`, and sends `User_Passkey_Notification`, a side with a keyboard
     `User_Passkey_Request`; in the other two the host gets `User_Confirmation_Request` with
     Bumble's `g2` of the two addresses and nonces, mod 10⁶. `_check_ssp_completion` is Bumble's,
     the DHKey check with `f6` over an all-zero DHKey once this side's host confirmed, and the key
     with `f5` once both have, but the key's type is P-192's, as the controller has no Secure
     Connections: `AUTHENTICATED_COMBINATION_KEY_GENERATED_FROM_P_192` when
     `authenticated_by(model, local, peer)`, Passkey Entry, or Numeric Comparison between two
     `DISPLAY_YES_NO` (Vol 3, Part C, 5.2.2.6, Table 5.7), `UNAUTHENTICATED_..._P_192` otherwise,
     where Bumble always names `AUTHENTICATED_..._P_256`; and only the initiator's host gets
     `Authentication_Complete`, as Vol 2, Part F, 4.2 has it and legacy pairing does here, where
     Bumble sends both. `_on_ssp_failed` sends `Simple_Pairing_Complete` with the reason, and
     `Authentication_Complete` to the initiator's host alone, once per pairing: both sides may
     find a DHKey check wrong. `_verify_peer_dhkey_check` is Bumble's, and on a check that fails
     also sends the peer `LMP_not_accepted` for `LMP_dhkey_check` with
     `AUTHENTICATION_FAILURE_ERROR` (Vol 2, Part C, 4.2.7.4), on which the peer fails its own
     pairing: Bumble tells only its own host, and the peer's waited for ever, as with two
     passkeys that differ. An `LMP_detach` during Secure Simple Pairing fails it with the
     detach's reason. `User_Passkey_Request_Reply` notes the passkey typed. A user's
     `User_Confirmation_Request_Negative_Reply` or `User_Passkey_Request_Negative_Reply` is
     Bumble's: `LMP_numeric_comparison_failed` or `LMP_passkey_failed` to the peer, and the
     failure on both sides, with the link left up. The DHKey is 32 zero bytes, Bumble's: there is
     no Diffie-Hellman exchange, so the key holds on this link only, as legacy pairing's does.
   - **The history of pairings.** `pairings` holds each pairing that ended, a `Pairing(t, peer,
     initiator, method, number, status, authenticated)`, `t` the loop's time in nanoseconds, and
     `under_way` the ones that have not, an `UnderWay(initiator, method, number, authenticated)`
     by peer. A legacy pairing starts when this side's host is sent `PIN_Code_Request`, on either
     side, and ends at `Link_Key_Notification` with `SUCCESS`, at the failed check with
     `AUTHENTICATION_FAILURE_ERROR`, at a negative reply to the PIN request with
     `PIN_OR_KEY_MISSING_ERROR`, or at the peer's `LMP_not_accepted` or `LMP_detach` with its
     error. Secure Simple Pairing starts as `simple_pairing` at the negative reply or the
     `LMP_io_capability_req`, takes the model's name and number when it is picked, and the
     passkey typed, and ends in `_check_ssp_completion` or `_on_ssp_failed`. A pairing under way
     when its link falls ends with the link's reason, also when `HCI_Reset`, a chip reset or a
     power loss takes this side's end of the link down. `authenticated` is the key's, and
     `False` for a pairing that failed and for legacy pairing.
   - **Encryption.** `Set_Connection_Encryption` on a link authenticated by legacy pairing, by a
     key, or by Secure Simple Pairing (`ssp_state.authenticated`) sends
     `LMP_encryption_mode_req` with the mode asked for, which this side keeps until it is
     accepted; the other side sends its host `Encryption_Change` with that mode and
     `LMP_accepted`, on which this side sends its own, with the same mode. On a link not
     authenticated the command status is `INSUFFICIENT_SECURITY_ERROR`.
   - **Keys.** `init_key`, `combination_key` and `response` are SHA-256 over their inputs, each
     length-prefixed and tagged, cut to 16, 16 and 4 bytes: they stand in for E22, E21 and E1,
     whose SAFER+ the link has no use for, since both ends are this class and only the hosts see
     a key. The random numbers come from `random.Random(str(public_address))`.
   - **Paging.** `on_hci_create_connection_command` answers `CONNECTION_ALREADY_EXISTS_ERROR` when
     `classic_connections` holds the peer, a page of this side's own or a link, or `_heard` holds a
     page from it, which waits for its host's answer; otherwise Bumble's handler makes this side's
     entry, with the `allow_role_switch` the command asks, and sends `LmpHostConnectionReq`, and
     this one starts a timer of `page_timeout * SLOT_S`, kept in `_paging`, unless Bumble refused
     the page with `CONTROLLER_BUSY_ERROR`, as it does while an LE connection is pending, and made
     no entry for it. An entry in `classic_connections` is this side's own page while `_paging`
     holds its peer, and a link otherwise (`_has_link`). A connection that completes cancels the
     timer. At the timer, `on_classic_connection_complete` gets `PAGE_TIMEOUT_ERROR`; on a failure
     it drops the entry of this side's own page, which Bumble's own completion leaves, and an
     acceptance of the peer's page that waits for its role switch, since the event names only the
     address; with nothing else held for the peer it drops any timer too (`_forget_link`). A page
     heard from the peer whose host has yet to answer stays in `_heard`, and waits for the answer.
     `Write_Page_Timeout` sets `page_timeout`, default
     `PAGE_TIMEOUT_SLOTS`, and refuses 0 with `INVALID_COMMAND_PARAMETERS_ERROR`: the specification's
     range starts at 1.
     TODO(owner): Vol 1, Part F, 2.11 defines `CONNECTION_ALREADY_EXISTS`, and 7.1.5 does not say
     how a controller reports it for `Create_Connection`; here it is the command status. The `bt`
     fixture puts this controller in place of ESP32's, so only a real board's shows what ESP32's
     does.
   - **Remote name requests.** `on_hci_remote_name_request_command` starts a timer of the same
     length, kept in `_naming`, before Bumble's handler sends `LmpNameReq`; the answer cancels it, and
     at the timer the host gets `Remote_Name_Request_Complete` with `PAGE_TIMEOUT_ERROR`, which Vol 1,
     Part F, 2.4 names for this command. A second request to a peer with one pending keeps the
     first's timer, so both end when the first does.
   - **Page scan.** Bumble keeps `Write_Scan_Enable` in `classic_scan_enable` and delivers a page
     whatever it says. `on_lmp_packet` first asks `_hears`: with page scan (bit 1, `PAGE_SCAN`) off,
     `LmpHostConnectionReq` is dropped before Bumble's handler, so the target makes no entry, its
     host hears nothing, and the pager's timer ends the page with `PAGE_TIMEOUT_ERROR`. `LmpNameReq`
     is dropped too, unless `_linked`: this side `_holds` the sender, or the sender holds this side,
     with a link or a page heard rather than only a page of its own, which the request goes over
     (Vol 4, Part E, 7.1.19 pages only when there is no connection); the requester's timer then ends
     it. Every other packet goes over a link and is delivered. Page scan is read when the packet
     arrives, which is the instant the page starts: one turned on during the page does not hear it,
     as a device that returns does not, and one turned off after the page was heard leaves it to its
     answer. Inquiry looks at bit 0, `INQUIRY_SCAN`, only.
   - **Link supervision.** Nothing polls on this link, so a link is in contact until a device
     leaves, and the specification's "no packets are received for a duration longer than the
     `Link_Supervision_Timeout`" counts from there. `Controller.left`, on the controller of the
     device that leaves, calls `Link.leave` with the peers it `_holds`, a link or a page it heard in
     `_heard`, leaving out a page of its own, and with each other controller that holds this device
     the same way: a pager whose page ended holds nothing, nor does one that rejected a crossing
     page while its own page goes on, yet the target holds their page. It calls
     `_lose_contact` on both ends of each, which keeps the time in `_lost_contact` and, through
     `_supervise`, starts a timer to
     `_link_timed_out`; a link that lost contact already keeps its time, and this side's own page
     is left to end by itself, though the entry of a page it heard meanwhile has a timer of its own.
     `_link_timed_out` sends `Authentication_Complete` with `CONNECTION_TIMEOUT_ERROR` to an
     initiator's host still authenticating, drops the page heard from that peer, if any
     (`_drop_heard`), and, when its host accepted that page as the central and waits for the role
     switch, which was lost on the way to a pager that left, sends the host `Connection_Complete`
     with `CONNECTION_TIMEOUT_ERROR`, unless this side holds an entry of its own for the peer, as
     for a late answer; then it runs `on_classic_disconnected` with that reason on a link, and
     otherwise only drops the timers and lets the pair go.
     `Write_Link_Supervision_Timeout` needs the central: it keeps the value in
     `_supervision_timeouts`, by peer, sends `LmpSupervisionTimeout`, and on a link that lost
     contact starts the timer again from `_lost_contact`. The peripheral keeps
     what the packet says and sends its host `Link_Supervision_Timeout_Changed`; a packet the link
     refuses leaves the other end at its old value. 0 starts no timer.
     `on_classic_disconnected` cancels the timer and, once neither end holds the link, calls
     `Link.let_go`, as a page that fails does: a target that left when it heard the page may let
     go of its end first. `Controller.power_lost` ends what this side's host has under way, a page
     or a remote name request with `PAGE_TIMEOUT_ERROR`, an inquiry with `Inquiry_Complete`, so that
     no timer of theirs outlives the power and ends the next one; then it runs `_link_timed_out` on
     each peer this side still holds, a link or a page heard, at once, so that an authentication
     under way ends with its status.
     `BluetoothDevice._become` first clears Bumble's `Device.auto_restart_inquiry`, or its host
     would start another inquiry at the completion; the controller has no model of its power, so one
     started through `peer.device` after that runs, and restarts, as on a device switched on.
     `Controller.returned` calls `Link.back`.
   - **A page this side's host has not answered.** Bumble's `on_classic_connection_request` writes
     the entry of a page heard in `classic_connections` with handle 0, over this side's own page to
     that address. `Controller.on_classic_connection_request` keeps an ACL page in `_heard` instead,
     as a `HeardPage` that holds the `Connection` the link will be, the peripheral with the
     controller's `classic_allow_role_switch`, and sends the host `Connection_Request` as Bumble
     does; a synchronous link still goes to Bumble's handler. A second page from the same peer
     replaces the first. Each `Connection_Request` adds an `Answer` to `_owed`, the list of answers
     the host owes that peer, oldest first, with its connection accept timer (below). When the
     page's end falls, `_link_timed_out` drops it through `_drop_heard` without telling the host,
     since no link was made, and that marks every answer still owed for it `late`.
     `_take_answer`, which both handlers of an answer call, takes the oldest answer owed and
     cancels its timer. With none owed the answer gets `UNKNOWN_CONNECTION_IDENTIFIER_ERROR`, as the
     host's own error, on a working link and during this side's own page too, and so does one that
     comes after its connection accept timeout, which took it off `_owed`. It gives the page the
     answer is for, from `_heard`, unless the answer is late, or `_heard` holds no page from the
     peer still waiting for an answer, as
     after its end fell or after an acceptance as the central, or this side already has a link to
     the peer, as when its own page completed after it heard the other's, or the peer's host has
     accepted this side's own page, which is still under way here (`_own_page_accepted`, which reads
     the peer's controller): the peer has a link to this side, or holds this side's page marked
     `switching` (`_switching_for_a_page`) and this side's own entry allows the role switch, as its
     `Create_Connection` asked. Then the answer is late, and the page heard goes through
     `_drop_heard`, since this side's own page makes the link. A switch this side's own entry would
     refuse ends both pages, so then the page heard is still the answer's, and makes the link. These
     are two pages at one instant, both accepted: the first acceptance sends `LMP_accepted`, or
     `LMP_switch_req` first, and the second, at the same instant, finds it on the other side while
     that is on its way, so the one link is the page accepted first, with the roles its target's
     host asked, and one handle on each side.
     A page that ended at its pager's timer is not
     among these: this side does not hear of it, so an acceptance then makes a link, and a rejection
     goes to a pager that drops it. A late answer: the host gets a command
     status of `SUCCESS` and, unless this side holds an entry for that peer in
     `classic_connections`, its own page or a link, `Connection_Complete` with
     `CONNECTION_TIMEOUT_ERROR` (`_tell_host_no_link`): the event names only the address, so it
     would end that page or fail that link in the host's eyes. What this side holds is left as it
     is, so that a late answer completes neither a new page nor a working link. An answer
     names only the address, so answers are told apart only by their order: the oldest is taken
     first, and the late ones are always the oldest, since `_drop_heard` marks all those owed. An
     answer that is not late answers the page in `_heard`, whichever page it was owed for: with a
     page tried again while the first answer is on its way, as when the pager's page timeout is
     shorter than this side's connection accept timeout, that first answer connects the second
     page, and the second answer finds the link and is told no link. A link being made marks
     nothing late, so that an answer a host never gives does not take the next page's, except when
     this side's own page makes the link while it holds a page heard from that peer: that page can
     make no link any more, so `on_classic_connection_complete` drops it through `_drop_heard`, and
     the answers still owed for it are late. Without that it would stay in `_heard` after the link
     ended, and refuse this side's next page with `CONNECTION_ALREADY_EXISTS`.
   - **The connection accept timeout** (Vol 4, Part E, 6.7). `on_classic_connection_request` starts
     each `Answer`'s timer with `call_later(connection_accept_timeout * SLOT_S)`, the timeout read
     at that instant, so a page heard keeps the time it had when the host writes another. Taking the
     answer cancels it, and so does `_restart`, which empties `_owed`; a power loss does not, since
     the answers owed outlive it here: a `BluetoothDevice` switched off for longer than the timeout
     has its host, which runs on, hear `CONNECTION_ACCEPT_TIMEOUT_EXCEEDED` while the device is off,
     as it hears the end of its own pages at the power loss. At the timer `_accept_timed_out` takes
     the answer off `_owed`, so a later answer from the host is owed for nothing. When the answer
     was the last one not late and the page in `_heard` still waits, not `switching`, with no link
     to the peer, `_refuse` refuses the page with `CONNECTION_ACCEPT_TIMEOUT_ERROR`, as a host's
     rejection does: `LMP_not_accepted` for `LMP_HOST_CONNECTION_REQ` with that reason, which a
     pager still paging reports in its `Connection_Complete` and one whose page ended drops, the
     page removed with what it had running, and `Connection_Complete` with the reason to this side's
     host unless it holds an entry for the peer. With the defaults, 5 s against the pager's 5.12 s,
     the pager hears this before its own timer. When the peer has accepted this side's own page
     meanwhile (`_own_page_accepted`) the page heard goes through `_drop_heard` instead, as for an
     answer. For a late answer, or one not late while another not late is still owed, nothing is
     sent to the peer, and the host gets `Connection_Complete` with
     `CONNECTION_ACCEPT_TIMEOUT_ERROR` under the same rule as a late answer (`_tell_host_no_link`).
     The page's own timer is unchanged: it runs until the target's host accepts, so a pager whose
     page timeout is shorter than the target's connection accept timeout ends at its own, and a host
     that accepts between the two still makes a link. `Write_Connection_Accept_Timeout` (7.3.14)
     sets `connection_accept_timeout`, default `ACCEPT_TIMEOUT_SLOTS`, and refuses a value outside
     `ACCEPT_TIMEOUTS`, 0x0001 to 0xB540, with `INVALID_COMMAND_PARAMETERS_ERROR`.
     `Read_Connection_Accept_Timeout` (7.3.13), which Bumble has no class for, is a generic
     `HCI_Command`: `on_hci_read_connection_accept_timeout_command` sends its own
     `Command_Complete`, with `SUCCESS` and the timeout in 2 bytes, little-endian, as
     `HCI_GenericReturnParameters`.
   - **Accepting a page.** `on_hci_accept_connection_request_command` replaces Bumble's, which
     works on `classic_connections` only. After the command status, `_accepted` takes the page from
     `_heard`, sends `LMP_accepted`, puts its `Connection` in `classic_connections`, in place of
     this side's own page to that peer if one is under way, and completes it, so the host gets one
     `Connection_Complete`, which ends its own page with the link. With `role=CENTRAL` the role
     switch comes first, in Bumble's order: `LMP_switch_req`, while the page waits in `_heard`
     marked `switching`. `_role_switched` then changes the entry's role, with `Role_Change` to the
     host, and accepts; on a refusal it drops the page, sends `LMP_not_accepted` for
     `LMP_HOST_CONNECTION_REQ` with the refusal's status, and completes the connection with that
     status, which ends this side's own page too, as the host cannot tell the two apart, unless the
     peer has accepted this side's own page meanwhile (`_own_page_accepted`), as when both hosts
     accept as the central at one instant and only one page allows the switch: then that page
     makes the link, and the refused acceptance goes as a late answer, with nothing sent and
     nothing told. A page that went before the switch's answer leaves the answer nothing to do. The
     pager's own page keeps the entry `Create_Connection` made, also when it heard a page from the
     target and rejected it, so it takes the switch with the `allow_role_switch` it asked. A switch
     the target asks before it accepts belongs to the pager's own page, and only that page, while
     under way, takes it: `Controller.on_classic_role_change_request` answers `LMP_not_accepted` for
     `LMP_SWITCH_REQ` with `ROLE_CHANGE_NOT_ALLOWED_ERROR` when the pager has no entry for the
     target, as after its page ended, where Bumble's handler asserts one, and when the target holds
     the pager's page marked `switching` while the pager's entry is a link, made of the target's own
     page, which the pager accepted at the same instant. In the first case the target's host gets
     `Connection_Complete` with that status, and no link comes of the acceptance; the pager drops
     the `LMP_not_accepted` that follows, as for a rejection of a page that ended. In the second,
     the pager's `LMP_accepted` reaches the target first and its own page makes the link, so the
     refusal finds no page waiting, and each side keeps the role of that page.
   - **Rejecting a page.** Bumble 0.0.236 has no handler for `Reject_Connection_Request`, so its
     `on_hci_command` would return `UNKNOWN_HCI_COMMAND_ERROR` where an asynchronous command has
     nowhere to put it, and the host would wait for ever. `on_hci_reject_connection_request_command`
     refuses a reason outside `REJECT_REASONS`, 0x0D to 0x0F as Vol 4, Part E, 7.1.9 has it, with
     `INVALID_COMMAND_PARAMETERS_ERROR`, and the host still owes its answer. Then `_take_answer`, so
     a rejection is an answer like an acceptance. One for a page that still waits: a command status
     of `SUCCESS`, `LMP_not_accepted` for `LMP_HOST_CONNECTION_REQ` with the reason, the page
     removed from `_heard` with whatever a page whose pager left has running (`_forget_link`), this
     side's own page left as it is, and `Connection_Complete` to this side's host with the reason as
     its status, unless this side is itself paging that peer, for the same reason as a late answer.
     With a page tried again, the first rejection ends the second page, and the second answer finds
     no page in `_heard` and is late. Bumble's completion of the page gives the pager's host
     `Connection_Complete` with the reason, as 7.1.9 requires. A pager whose page already ended has
     told its host: `on_lmp_packet` drops the rejection and the page's future, or its host would
     hear of that page twice. A pager out of reach hears nothing, and its page ends at its timer.
     TODO(owner): 7.1.9 says this side's host gets `Connection_Complete` and gives its status only
     for the pager's; here it is the reason, as [the
     decision](../decisions/harness.md#a-host-that-rejects-a-page-gets-the-reason-too) says. Only a
     real board's controller shows what ESP32's does.
   - `Write_Secure_Connections_Host_Support` is answered with `SUCCESS` and changes nothing.
     `lmp_features` loses Bumble's `BR_EDR_NOT_SUPPORTED` and gains `BR_EDR_FEATURES` on page 0:
     3- and 5-slot packets, encryption, role switch, RSSI with inquiry results, the extended
     inquiry response, Secure Simple Pairing's controller support, the
     `Link_Supervision_Timeout_Changed` event and extended features. `Read_Local_Extended_Features`
     for a page after `lmp_features_max_page_number` gets `INVALID_COMMAND_PARAMETERS_ERROR` and 8
     zero bytes, where Bumble's handler gives the page after the last empty.
   - **Settings that change nothing on this link**, kept in an attribute each and answered with
     `SUCCESS`: `Write_Default_Link_Policy_Settings` (Vol 4, Part E, 7.2.12) in
     `default_link_policy`, `Write_Link_Policy_Settings` (7.2.10) in `link_policies` by handle, or
     `UNKNOWN_CONNECTION_IDENTIFIER_ERROR` for a handle that is no link (`_link_of`: a page's entry
     has handle 0), `Write_Inquiry_Scan_Activity` (7.3.22), `Write_Page_Scan_Activity` (7.3.20),
     `Write_Inquiry_Scan_Type` (7.3.48) and `Write_Page_Scan_Type` (7.3.52). The link has no hold or
     sniff mode, a role switch goes as `Create_Connection` and `classic_allow_role_switch` say, and
     no scan has a timing or a type. `Set_Event_Filter` (7.3.3) takes the settings that filter
     nothing: filter type 0, clear all; type 1, inquiry results, with condition 0, all devices; type
     2, connection setup, with condition 0 and `Auto_Accept_Flag` 1, off. Any other gets
     `UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR`, since no event is filtered here.
   - **Inquiry access codes.** `Write_Current_IAC_LAP` (7.3.45), which Bumble has no class for, is a
     generic `HCI_Command`: `on_hci_write_current_iac_lap_command` reads `Num_Current_IAC` and the
     3-byte LAPs from its parameters, keeps them in `iac_laps` when the count is 1 to 0x40, matches
     the length and every LAP is in `IAC_LAPS`, 0x9E8B00 to 0x9E8B3F, and otherwise answers
     `INVALID_COMMAND_PARAMETERS_ERROR`, in a `Command_Complete` it sends itself
     (`_command_complete`), since Bumble's dispatch answers only an `HCI_SyncCommand`. `iac_laps`
     starts as `[GIAC]`, 0x9E8B33, and an inquiry is answered only by a controller whose `iac_laps`
     holds the inquiry's `lap`. `Read_Number_Of_Supported_IAC` (7.3.43) gives 0x40, the length of
     `IAC_LAPS`, and `Read_Current_IAC_LAP` (7.3.44) the codes in `iac_laps`, 3 bytes each.
   - **The host's flow control.** `Host_Buffer_Size` (7.3.39) keeps
     `host_acl_data_packet_length` in `host_acl_length`, 0 as `None`.
     `Host_Number_Of_Completed_Packets` (7.3.40), a generic command too, gets no event, as 7.3.40
     says, and changes nothing.
   - **Commands about a link**, asynchronous, each with `Command_Status` from `_linked_command`:
     `UNKNOWN_CONNECTION_IDENTIFIER_ERROR` for a handle that is no link, else `PENDING`, then at
     once `Connection_Packet_Type_Changed` (7.1.14, 7.7.29) with the packet types asked,
     `Read_Clock_Offset_Complete` (7.1.24, 7.7.28) with offset 0, as an inquiry's results have it,
     and `Read_Remote_Version_Information_Complete` (7.1.23, 7.7.12) with the `lmp_version`,
     `manufacturer_company_identifier` and `lmp_subversion` of the peer's controller, read from it
     on the link as `LMP_version_req` would tell, or nothing when the link holds no controller at
     the peer's address or `Link.reaches` refuses it: the link then falls at its supervision
     timeout.
   - **What it does not know.** `on_hci_command`, which Bumble's dispatch calls for a command with
     no `on_<name>` handler, appends an `UnknownCommand(t, op_code, name)` to `unknown_commands`,
     `t` the loop's time in nanoseconds, and answers `UNKNOWN_HCI_COMMAND_ERROR`: as the return
     parameters of a `HCI_SyncCommand`'s `Command_Complete`, in a `Command_Status` for an
     `HCI_AsyncCommand`, and in a `Command_Complete` of its own for a generic one, whose op code
     Bumble has no class for. Vol 4, Part E, 4.5 leaves the event to the vendor; Bumble 0.0.236's
     handler returns parameters that its dispatch drops for an asynchronous command, so its host
     would wait for ever. The override is `# type: ignore[override]`: it returns `None` too.
   - **Reset.** `on_hci_reset_command` (7.3.2) calls `_restart`, then `_defaults`, then Bumble's
     handler, whose `Command_Complete` is all the host hears. `_restart` sets `host` to `None`, so
     that nothing reaches the host, then calls `left()` if the controller is on the air,
     `power_lost()` always, and `returned()` if it was on the air and `back` is true, and puts the
     host back: each peer loses contact and its end falls at its supervision timeout, and this side
     ends its pages, name requests, inquiry and links at once, as on a power loss. Then it clears
     what `power_lost` leaves: `_heard`, `_owed` with its timers (a page heard that goes marks its
     answers late), `_security`, `_encryption`, `_supervision_timeouts`, `_lost_contact`,
     the `_supervision` timers, Bumble's `classic_pending_commands`, whose futures are dropped
     without being resolved, so that an answer that comes later finds nothing, and `sco_links`.
     `_defaults` puts back the host's settings, as `__init__` sets them: Bumble's `event_mask`,
     `event_mask_page_2` and `local_name`, which Bumble's controller reads nowhere,
     `class_of_device`, `extended_inquiry_response`, `inquiry_mode`, `page_timeout`,
     `connection_accept_timeout`, `classic_scan_enable`, `iac_laps`, the link policies, the scan
     activities and types, `host_acl_length`, and the host feature bits
     `SECURE_SIMPLE_PAIRING_HOST_SUPPORT` and `LE_SUPPORTED_HOST`. `rssi`, the device's own, stays.
     `_restart(back=False)` leaves the controller off the air, for the guest's supply.
   - **ACL data for the host.** `on_link_acl_data` cuts an L2CAP PDU from a Classic peer into
     packets of at most the smallest of `host_acl_length` and the class's `acl_to_host`, `None` in
     `Controller` and `ACL_LENGTH` in `GuestController`, the first with packet boundary flag
     `FIRST_FLUSHABLE` (0b10), the others `CONTINUING` (0b01) (Vol 4, Part E, 5.4.2), as 7.3.39
     requires; with neither bound it sends the PDU whole, as Bumble does. Each packet goes through
     `send_hci_packet`, so they reach the host in order. An LE PDU, or one with no connection,
     goes to Bumble's handler.

9. **The link that refuses.** `Link` subclasses Bumble's `LocalLink`, which raises
   `InvalidArgumentError` from `send_lmp_packet` for an address it does not hold and delivers to
   any it does; `remove_controller` alone would fail the air on the next packet, and neither end
   would time out. Whether a device can be reached is decided in `Link.reaches(sender, receiver)`
   and nowhere else: neither is in `_absent`, and the pair is not in `_severed`. `send_acl_data` and
   `send_lmp_packet` drop what it refuses, and `send_lmp_packet` what is for an address nobody has.
   `leave(address, peers)` adds to both; `back(address)` takes the address out of `_absent` and
   leaves `_severed`, so that a link is not revived; `let_go(one, other)` takes the pair out.
   `Controller.on_hci_inquiry_command` asks `Link.present` of both the device that asks, which
   then hears nobody, and each that would answer. The firmware's controller is on the same `Link`
   (#34), so a device leaves the firmware's air as it leaves another device's.
10. **A device that leaves.** `switch_off`, `switch_on`, `out_of_range` and `in_range` call
    `BluetoothDevice._become(on, in_range)` through `Air.call`. It keeps `_on` and `_in_range`, whose
    conjunction is `present`, and calls `Controller.left` on a change to absent, `power_lost` on a
    change to off, `_supply` on any change of power, and `Controller.returned` on a change to
    present, in that order. `_supply` does nothing in `BluetoothDevice`; `SppPeer._supply` calls
    `supply(on)` of `_driving` when it is `Supplied` (`esp32tl/interfaces/uart.py`), a `Model`'s
    when it has one, through
    `_call`, and clears `_pending` first. `_flush` counts an
    output in `lost` when no DLC is open or `Link.reaches` refuses the holder's address, so what goes
    out on a link that lost contact does not enter `events`. `forget_keys` puts a new
    `MemoryKeyStore` in `device.keystore`; a link under way keeps the key in its `Security`.
11. **The firmware's controller.** `GuestController(name, link, address=ADDRESS)` is a
    `Controller` with `lmp_features_max_page_number` 2 (`MAX_PAGE`), its features cut to the bits
    of pages 0 to 2 (`PAGES_MASK`) and without `LE_SUPPORTED_CONTROLLER`, `acl_data_packet_length`
    and `acl_to_host` both `ACL_LENGTH`, `HCI_MAX - 5`, 491. `chip_reset()` is `_restart()` and
    `_defaults()`; `chip_off()` is `_restart(back=False)` and `_defaults()`; `chip_on()` is
    `returned()`. Bluedroid's `get_features_classic` asserts on a fourth page
    (`components/bt/host/bluedroid/device/controller.c:523`, ESP-IDF v5.3.6), and Bumble reports
    four (`lmp_features_max_page_number = 3`). TODO(owner): `ADDRESS`, `24:6F:28:00:11:24`, is the
    spike's; what the classic ESP32's eFuse gives the controller on esp32sim, the base MAC plus 2,
    is not read.
12. **The interface.** `Bluetooth(*, asked, log, address, latency=LATENCY_NS, directory, files)`
    (`esp32tl/interfaces/bluetooth.py`) is an `Interface` named `bluetooth`, with no wire, whose
    `bluetooth` is true. `use()`, from `Board.bluetooth` the first time, calls
    `asked(Request("bluetooth", source="model"))`, then makes an `Air(board_clock=True)` and a
    `GuestController("esp32", air.link, address)` on it, through `air.call`, and makes itself the
    controller's `host`: Bumble's controller sends its host packets with `call_soon(host.on_packet,
    ...)`, and `on_packet` keeps each in `_out` with the air's time. `air` and `controller` call
    `use()`; `used` says whether it ran. Until then `latency()` and `quiet_until()` are `None`,
    `arrive` and `poll` do nothing, and `receive` raises `BluetoothError`: the runner has no `--bt`.
    `receive(t, ...)` follows the air to `t`, counts the packet, gives it to the controller's
    `on_packet` through `air.call`, and returns nothing: what the controller answers goes out in
    `poll`. `arrive(now)` follows the air to `now`, before the hold. `poll(now)` follows it to `now`
    again, after the test's calls in the hold, and returns every packet in `_out` at the instant it
    was made plus `latency`; one due before `now`, or not 1 to `HCI_MAX` bytes, raises
    `BluetoothError`. By rule 1 of the protocol (§6) the turn's block point is at most the first
    unacknowledged event plus the latency, and every event of the slice is at or after it, so an
    answer to an event is never due before the turn; an answer to a timer of the air is not either,
    since `quiet_until()` is the air's next instant plus `latency`. A packet due before the turn
    would be a runner or a harness that broke the rule: the interface says so rather than send it
    late. `latency()` is `latency` once used. `supply(on)` calls `chip_on` or `chip_off` through
    `air.call`, and on `False` drops `_out`: the runner's reboot drops what the host had not read.
    `chip_reset(t)` follows the air to `t`, calls `chip_reset` and drops `_out`. The backend calls
    it twice for `board.reset()`: from `Esp32Sim.reset()`, in the held turn and before it sends
    `ChipReset`, so that the turn carries none of the old host's packets, since the runner's reboot
    empties its queue at the reset and what goes in after it reaches the new host; and again at the
    runner's `reset` message, which finds nothing to drop. `report()` and `properties()` give the
    address, the four counts `packets_from_host`, `bytes_from_host`, `packets_to_host`,
    `bytes_to_host`, `unknown_commands()` as `(t, op_code, name)` and `devices()`, every other
    `Controller` on the link as `(name, address, present)`, `present` from `Link.present`, sorted by
    address as an inquiry sorts them. `close()` closes the air. `LATENCY_NS` is 10 µs: a controller
    answers a command within microseconds, and the host sleeps 10 ms between two VHCI polls when it
    has nothing to read (protocol 5.22), so the answer is there for its next poll; at 0 every packet
    would take a turn of its own. The spike measured 259 kbit/s of SPP with it; `bt_spp_initiator`
    reports about 299 kbit/s at the pins `c99fb28` and `3bdb958` (`tests/test_bt_spp_initiator.py`).
    `tap(path)` makes `tapped`, a `Tap("bluetooth", directory / path)`; `receive` records each
    packet into it as `out` at `t`, and `poll` each packet it returns as `in` at its due time. The
    backend writes it as btsnoop ([run-report-technical.md](run-report-technical.md#the-hci-tap)).
    `report()`'s first line ends with `; tapped into {path}` and `properties()` has `tap`.

## Interfaces

| Name | What |
|---|---|
| `Air(*, board_clock=False)` | `esp32tl/bluetooth/air.py`: `loop`, a `BoardLoop`; `link`, a `Link`; `board_clock`; `now`, a `Duration`; `join(party)`, which settles the current instant with the party in it; `call(function)`; `start(coroutine)`, a task; `fail(error)`; `quiet_until()`, nanoseconds or `None`; `advance(until)`, a `Duration`; `wait(duration)`; `run(awaitable, *, within, description="")`; `at_once(awaitable, description="")`; `follow(until)`, nanoseconds, the link's; `close()` |
| `Party` | A protocol: `poll(now)`, inside the loop at every instant the air stops at, returning nothing; `quiet_until()`, later than `now`, or `None` |
| `BoardLoop()` | `now`, an `int` of nanoseconds; `time()`; `run_idle()`, the next timer's time or `None` |
| `AirError` | A `RuntimeError`: what failed on the air, as its `__cause__` |
| `Link()` | `esp32tl/bluetooth/air.py`: Bumble's `LocalLink`; `present(address)`, `reaches(sender, receiver)`, `leave(address, peers)`, `back(address)`, `let_go(one, other)` |
| `Controller(name, link, address)` | `esp32tl/bluetooth/controller.py`: Bumble's `Controller` with the additions above; `link`, a `Link`; `acl_to_host`, a class attribute, `None`; `class_of_device`, `extended_inquiry_response` (240 bytes), `inquiry_mode`, `rssi`, `page_timeout`, `connection_accept_timeout`, in slots; `iac_laps`; `default_link_policy`, `link_policies`; `inquiry_scan_activity`, `page_scan_activity`, `(interval, window)`; `inquiry_scan_type`, `page_scan_type`; `host_acl_length`; `unknown_commands`; `authenticated(peer)`; `random_bytes(n)`; `left()`, `returned()`, `power_lost()` |
| `Pairing(t, peer, initiator, method, number, status, authenticated=False)` | `esp32tl/bluetooth/controller.py`: frozen dataclass, a pairing that ended; `succeeded` |
| `UnderWay(initiator, method, number=None, authenticated=False)` | A pairing under way; `Controller.under_way`, by peer, and `Controller.pairings` |
| `LEGACY`, `JUST_WORKS`, `NUMERIC_COMPARISON`, `PASSKEY_ENTRY`, `SIMPLE_PAIRING` | The methods' names: `"legacy"`, `"just_works"`, `"numeric_comparison"`, `"passkey_entry"`, `"simple_pairing"` |
| `association(local, peer)`, `authenticated_by(model, local, peer)` | The association model of two IO capabilities, and whether its key is authenticated |
| `SIX_DIGITS` | `1_000_000`: the range of a number to compare and of a passkey |
| `UnknownCommand(t, op_code, name)` | Frozen dataclass: a command the controller answered with `UNKNOWN_HCI_COMMAND_ERROR`, `t` in nanoseconds of the air's time |
| `GIAC`, `IAC_LAPS`, `BR_EDR_FEATURES` | `0x9E8B33`; `range(0x9E8B00, 0x9E8B40)`; page 0's features, above |
| `GuestController(name, link, address=ADDRESS)` | `esp32tl/bluetooth/guest.py`: a `Controller`, BR/EDR only, pages 0 to 2, `acl_to_host` `ACL_LENGTH`; `chip_reset()`, `chip_off()`, `chip_on()` |
| `ADDRESS`, `ACL_LENGTH`, `MAX_PAGE` | `esp32tl/bluetooth/guest.py`: `"24:6F:28:00:11:24"`; `491`, `HCI_MAX - 5`; `2` |
| `Bluetooth(*, asked=..., log=None, address=ADDRESS, latency=LATENCY_NS, directory=None, files=None)` | `esp32tl/interfaces/bluetooth.py`: the `Interface` above; `use()`, `used`, `air`, `controller`, `address`; `tap(path)`, `tapped`, `directory` (the working directory unless given), `files`; `pairings()`, the guest's controller's; `supply(on)`, `chip_reset(t)`; `packets_from_host`, `bytes_from_host`, `packets_to_host`, `bytes_to_host`; `unknown_commands()`, `devices()`; `report()`, `properties()`; `close()` |
| `LATENCY_NS`, `BluetoothError` | `esp32tl/interfaces/bluetooth.py`: `10_000`; a `RuntimeError`, what the controller sent that the link cannot carry |
| `Security` | Dataclass: one link's authentication on one side: `initiator`, `init_key`, `local_rand`, `peer_rand`, `link_key`, `pairing`, `challenge`, `peer_challenge`, `authenticated` |
| `LmpInRand`, `LmpCombKey`, `LmpSres`, `LmpEncryptionModeReq`, `LmpSupervisionTimeout` | LMP packets Bumble does not define, registered with `lmp.Packet.subclass` |
| `init_key(pin, in_rand, responder)`, `combination_key(init, initiator_rand, responder_rand, initiator, responder)`, `response(link_key, challenge, claimant)` | The keys and the response, above |
| `INQUIRY_UNIT_S` | 1.28 |
| `RSSI` | `esp32tl/bluetooth/controller.py`: `-50`, in dBm, a value of ours: nothing says what a device's should be |
| `SLOT_S`, `PAGE_TIMEOUT_SLOTS`, `ACCEPT_TIMEOUT_SLOTS`, `LINK_SUPERVISION_TIMEOUT_SLOTS` | `0.000625`; `0x2000`, 5.12 s; `0x1F40`, 5 s; `0x7D00`, 20 s: Bluetooth Core 5.4, Vol 4, Part E, 7.3.16, 7.3.14 and 7.3.42 |
| `ACCEPT_TIMEOUTS` | `range(0x0001, 0xB541)`: the connection accept timeouts a host may write, up to 29 s |
| `HeardPage(connection, switching=False)`, `Answer(late=False, timer=None)` | `esp32tl/bluetooth/controller.py`: dataclasses, a page this side heard, in `_heard`, and an answer its host owes, in `_owed` |
| `BluetoothDevice(air, *, name, pin=None, class_of_device=CLASS_OF_DEVICE, rssi=RSSI, address=ADDRESS, io_capability=None, confirms=True, passkey=None)` | `esp32tl/bluetooth/peer.py`: `device`, the Bumble `Device`; `address`; `name`, `pin`, `class_of_device`, `io_capability`, `confirms`, `passkey`; `rssi`, read and written; `present`; `pairing`, the controller's latest `UnderWay` or `None`; `pairings`, a copy of the controller's; `switch_off()`, `switch_on()`, `out_of_range()`, `in_range()`, `forget_keys()` |
| `IO_CAPABILITIES` | `esp32tl/bluetooth/peer.py`: the four names a test gives, to Bumble's `PairingDelegate.IoCapability`: `DISPLAY_OUTPUT_ONLY`, `DISPLAY_OUTPUT_AND_YES_NO_INPUT`, `KEYBOARD_INPUT_ONLY`, `NO_OUTPUT_NO_INPUT` |
| `SppPeer(air, *, name, pin, source, address=ADDRESS, channel=CHANNEL, secure=True, class_of_device=CLASS_OF_DEVICE, rssi=RSSI, service_name=SERVICE_NAME, io_capability=None, confirms=True, passkey=None)` | A `BluetoothDevice`, also: `source`, `channel`, `secure`, `service_name`; `events`; `refused`, `(t, why)` pairs; `lost`; `channel_open`; the party's `poll(now)` and `quiet_until()` |
| `SERVICE_NAME`, `LANGUAGE`, `L2CAP_FEATURES` | `esp32tl/bluetooth/peer.py`: `"Serial Port"`; `(0x656E, 106, 0x0100)`; `(FIXED_CHANNELS, FCS_OPTION)` |
| `ADDRESS`, `CLASS_OF_DEVICE`, `CHANNEL`, `NAME_MAX`, `RECORD_HANDLE` | `"00:00:00:00:5B:01"`, an address no real device in a test is likely to have; `0x001F00`; `1`; `238`, the extended inquiry response's 240 bytes less the name field's length and type; `0x00010001`, the first SDP record handle a server may give a record of its own |
| `PeerError` | A `ValueError`: what a peer cannot be, or what its source sent that it cannot send |
| `Source` | `esp32tl/interfaces/uart.py`: `receive(t, data)`, `poll(now)`, `quiet_until()`, `latency()`; the peer never calls `latency` |
| `Model` | `esp32tl/sources/model/__init__.py`: the same four in `Duration`s, which the peer takes as `source` too ([pytest-plugin-technical.md](pytest-plugin-technical.md#interfaces)) |

## Bumble's internals it relies on

`bumble.link.LocalLink`: `send_acl_data`, `send_lmp_packet`, `controllers`,
`find_classic_controller`. `bumble.controller.Controller`: `classic_connections`,
`classic_allow_role_switch`, `classic_role_change`, `classic_pending_commands`,
`on_classic_connection_complete`, which gives a handle to the entry it finds for the address,
`on_classic_disconnected`, `on_classic_remote_name_response`, `on_classic_role_change_request`,
`on_classic_connection_request` for a synchronous link, `on_hci_create_connection_command`,
`on_hci_remote_name_request_command`, the `on_hci_<command>` dispatch by name, also for a generic
`HCI_Command` Bumble has no class for, named by `HCI_Command.command_names`, and its fallback
`on_hci_command`, which the dispatch answers only for an `HCI_SyncCommand`; `on_lmp_packet`,
`send_lmp_packet`, `send_hci_packet`, which sends with `call_soon(host.on_packet, ...)` while `host`
is set; `host`, `find_classic_connection_by_handle`, `on_link_acl_data`, `sco_links`,
`lmp_features_max_page_number`, `lmp_version`, `lmp_subversion`,
`manufacturer_company_identifier`, `acl_data_packet_length`,
`on_hci_read_local_extended_features_command`, `on_hci_reset_command`; for Secure Simple Pairing,
`_start_ssp_exchange`, `_on_ssp_confirm_or_nonce_received`, `_check_ssp_completion`,
`_verify_peer_dhkey_check` and `_on_ssp_failed`, private methods it replaces or wraps, and which
the rest of Bumble's handlers call, `on_hci_user_passkey_request_reply_command`, and `SspState`'s
fields. `bumble.controller.Connection`, which a page heard is made of, and its `ssp_state`.
`bumble.crypto`: `f5`, `f6`, `g2`. `bumble.device.with_connection_from_address`, and the device's
host event `pin_code_request`, which Bumble's `Device` subscribes to by the name of its
`on_pin_code_request` method, so that the peer's `Device` subclass answers it; the delegate's
`confirm`, `compare_numbers`, `get_number` and `display_number`, which Bumble's `Device` calls by
the table of Vol 3, Part C, 5.2.2.6. `bumble.device.Device`:
`auto_restart_inquiry`. `bumble.device.DeviceConfiguration.l2cap_extended_features`.
`bumble.rfcomm`: `Multiplexer.acceptor`, `Multiplexer.dlcs`, `Multiplexer.send_frame`,
`Multiplexer.l2cap_channel`, whose `sink` is the multiplexer's `on_pdu`, `DLC.state`, `DLC.c_r`,
`RFCOMM_Frame.from_bytes` and `RFCOMM_Frame.dm`. Connection state is keyed by address, so a
refused delivery is by address too.

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
| `{name}: source.{method} returned {output!r:.80}, not a (Duration, bytes) pair` | `_Misreturned`, a `PeerError`, raised on the air | `InNanoseconds._outputs`, for a `Model` |
| `{name}: source.{method} sent {data!r:.80} at {t}, and the air is at {now}: it cannot send into the past` | `PeerError`, raised on the air | `_queue` |
| `{name}: source.quiet_until returned {bound!r}, not nanoseconds` | `PeerError`, raised on the air | `quiet_until`: not an `int`, or a `bool` |
| `{name}: source.quiet_until returned {bound!r}, not a Duration` | `_Misreturned`, a `PeerError`, raised on the air | `InNanoseconds.quiet_until`, for a `Model` |
| `{name}: source.quiet_until returned {t}, not after the air's {now}: what it sends by then belongs in the poll at {now}` | `PeerError`, raised on the air | `quiet_until` |
| `{error}`, `{message}: {error}` | `AirError` | `_settle` or `Air.quiet_until`, with what `Air.fail` kept, or with what the loop's exception handler kept and asyncio's `message`, such as `Exception in callback Host.on_packet()` or `Task exception was never retrieved` |
| `something on the air is due at {t}, and the air is at {now} already` | `AirError` | A party's `quiet_until` at or before `now`; the peer checks its source's before this can happen |
| `the air is at {now}: it cannot go back to {until}` | `ValueError` | `advance` |
| `{description} did not complete within {within}: waited from {start} to {end} of the board's time` | `NeverHappened` | `run`; `description` is `the awaitable` when not given |
| `air.advance takes a Duration, as seconds(1), not 1`, `air.run: within takes a Duration of 0 or more, not …` | `TypeError`, `ValueError` | From `nanoseconds` in `esp32tl/backends/clock.py`, for `advance`, `wait` and `run` |
| `{call}: this air is on the board's clock, which only the board's link moves: let the board run with board.wait() or board.wait_until()` | `AirError` | `_test_moves`, for `air.advance`, `air.wait` and `air.run` on an `Air(board_clock=True)` |
| `SppPeer: service_name is {service_name!r}, not None or a string of 1 to 255 bytes` | `PeerError` | `service_name` neither `None` nor a `str` of 1 to 255 bytes in UTF-8 |
| `{kind}: io_capability is {io_capability!r}, not None or one of display_only, display_yes_no, keyboard_only, no_input_no_output` | `PeerError` | `io_capability` not `None` nor a key of `IO_CAPABILITIES` |
| `{kind}: confirms is {confirms!r}, not True or False` | `PeerError` | `confirms` not a `bool` |
| `{kind}: passkey is {passkey!r}, not six digits, 0 to 999999` | `PeerError` | `passkey` a `bool`, not an `int`, or out of range |
| `{kind}: passkey is typed on a keyboard: give io_capability='keyboard_only'` | `PeerError` | A `passkey` with another `io_capability` |
| `the guest's controller made {kind} at {t}, due at {t + latency}, before the turn at {now}: the link bounds every turn by the interfaces' quiet_until and latency (protocol §6, rule 1, and §7)` | `BluetoothError`, which the link turns into its error `bluetooth.poll raised BluetoothError: …` | `Bluetooth.poll`: a packet due before the turn; `{kind}` is `a command`, `ACL data`, `SCO data`, `an event`, `ISO data` or `a packet` by its first byte, `an empty packet` for one with none |
| `the guest's controller made {kind} of {n} bytes at {t}; esp32sim's VHCI carries 1 to 496` | `BluetoothError`, as above | `Bluetooth.poll`: a packet the VHCI cannot carry |
| `the runner sent hci_tx, and the test gave the board no Bluetooth: the run has no --bt` | `BluetoothError`, as above, from `receive` | An `hci_tx` before `use()` |

One failure is esp32sim's and raises nothing in the harness. Its `Send` hook
(`esp-soc/src/ble/vhci.rs:309-316`) copies a packet out of the guest only when
`VhciBus::valid_packet_range` holds for it. The classic ESP32's (`esp32/src/ble.rs:45-48`) takes
internal DRAM, `0x3FFA_E000` to `0x4000_0000`, and, since
[mikroverk/esp32sim#353](https://github.com/mikroverk/esp32sim/pull/353), PSRAM that reads as a load
would read it: `psram_readable` (`esp32/src/psram.rs:75-89`) asks that the range lie in
`0x3F80_0000` to `0x3FC0_0000`, that every 32 KiB page of it map through the cache's MMU to PSRAM
that is installed, and that the cache be able to read. A Bluedroid built with
`CONFIG_BT_ALLOCATION_FROM_SPIRAM_FIRST` sends from that window (ESP-IDF v5.3.6
`components/bt/common/osi/include/osi/allocator.h` and `components/soc/esp32/include/soc/soc.h`),
so on a board profile with `psram_mb = 4` its packets pass. A packet the check refuses, anywhere
else or in a page the cache does not map, makes the hook print `[ble] rejected invalid VHCI packet
pointer, length or controller state` and drop it; `Bluetooth.receive` is not called for it, and
when it is the firmware's first command, the four counts stay 0 and Bluedroid's own timeout ends
its wait for `HCI_Reset`. The other addresses the hooks touch do not depend on PSRAM: the callbacks
table is read with no range check, and the receive buffer is the controller's BSS, which
`components/bt/controller/linker_rw_bt_controller.lf` puts in `dram0_bss` whatever the PSRAM
options.

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
  `quiet_until`; two runs with the same `events`; Secure Simple Pairing between two
  controllers that support it, with the stand-ins' keyboards, which both type Bumble's default 0; encryption refused on a link not authenticated, and turned off on
  both sides; the air quiet until the device writes; the peer's refusals of its arguments and of an
  address taken. A peer that leaves, either way: the link falling at exactly the supervision timeout
  with no detach; a page ending at exactly the page timeout, also to an address nobody has; an
  inquiry not finding it until it returns; the key kept across a return, and `forget_keys`; a return
  before the timeout reviving nothing, with `CONNECTION_ALREADY_EXISTS`, another device connecting
  meanwhile, and the old channel still held; a timeout not restarted by leaving again; each timeout
  a host writes, 0, a peripheral's and one for no link; the source told or running on, with
  `lost`; `present`; the calls from a coroutine; a remote name request ending at the page timeout,
  and answered with no timer left; a device leaving during its own page and being reached again,
  also when the target's end falls before the page ends; a target that heard the page losing contact
  when the pager leaves; a power loss ending a page, a name request, an authentication and an
  inquiry, also one that would restart itself, without their timers ending the next; a target leaving
  before its host answers, with no link told of, and a host answering after its end fell; a device
  that left finding nothing; two runs with the same `events`, `lost` and
  instants.
  A host that rejects a page: the pager getting the reason at that instant, not `PAGE_TIMEOUT`; the
  host's next command answered; both connecting afterwards; a rejection after the page ended, after
  the target's end fell, and of a page tried again, none of them failing the air or reaching a later
  page or a working link; this side's own page left to end by itself, and not started again; the
  supervision of a page whose pager left ending with the rejection; each edge of the reason's range;
  an answer nothing is owed for, also on a working link; an answer never given not taking a later
  page's. Crossing pages: a page of this side that failed leaving the answer to the other its entry,
  and one that made the link taking the page heard along, so the pair can page again after the link
  ends,
  whose contact can still be lost, also by the device that leaves; a rejection of the other leaving
  this side's own page its entry, which takes the role switch, and the link it makes switching
  roles as `Create_Connection` allowed. A pager leaving after its page ended, or after rejecting a
  crossing page. A role switch refused, also with an answer never given, with this side paging too,
  and with both, leaving nothing. An entry that goes with a failed page taking its timer along.
  Two pages at one instant, both accepted at once, plainly or as the central on either side or both,
  making one link with one handle on each end and one central, also when the first asks for a role
  switch the other's page does not allow, whether the other accepts plainly or as the central, or
  comes after the other's page ended, and making none when both ask for the central's role and
  neither page allows it; both accepted 1 s apart, with no failure told of the later answer; a late
  answer on a working link, also to a page tried again, telling no failure. An acceptance as the
  central after the page ended refused with no link; one of a pager that left told at the
  supervision timeout, either way of leaving, or at the end of this side's own page when that page
  outlasts it. The tests of a host that answers after the page ended, or never, write the longest
  connection accept timeout, 29 s, so that the page's end comes first, as their names say, and
  answer within it: an answer the controller refuses as owed for nothing fails the air there, as
  Bumble's host raises it from a task, and only the test of that refusal takes it.
  The connection accept timeout, at its default: a page the host leaves unanswered ending at exactly
  5 s on both sides with `CONNECTION_ACCEPT_TIMEOUT_EXCEEDED`, before the pager's page timeout, and
  the next page connecting; an answer after it refused as owed for nothing, with no link on either
  side; the crossing pages of #166, this side's own page reporting `PAGE_TIMEOUT` and the other
  ending at its timeout, so this side's next page connects; a late answer the host never gives,
  after a power cycle, ending at its timeout and leaving the next page its answer; an answer given
  in time leaving no timer; a timeout written during a page taking effect from the next; a pager's
  shorter page timeout ending its side first, with the target's host hearing its own timeout; 0,
  0xB541 and 0xFFFF refused.
  Page scan off: a page ending at exactly the page timeout with no entry on the target, one turned
  on during the page answering only the next, and one turned back on paged at once; inquiry scan
  and page scan each on alone; a remote name request ending at the page timeout, also to a target
  paging the asker or paged by it when nobody heard that page, and answered with page scan off on
  both ends over a link, either way, to a target that heard the page before its host answers, and
  by one that turned page scan off after hearing it.
  Other devices: several in one inquiry, each with its own class and RSSI; a new `rssi` at the next
  inquiry; RSSI in the RSSI mode and none in the plain one; a device without SPP that has no SDP
  record and no RFCOMM; one without a PIN refusing a pairing, and one with a PIN keeping its key
  across a return; leaving; their refusals of their arguments.
- `tests/test_spp_peer.py` also: the channel opening when the device authenticates between its PN
  and its SABM, a device refused at the SABM authenticating and opening it, the channel let go by
  the refusal so that a second device is refused only while the first holds it; the service name in
  the SDP record, the default, another and none, with the language attribute; no Enhanced
  Retransmission Mode among the L2CAP features of either class; the refusal of a service name.
- `tests/test_duration_model.py`: a peer whose source is a `Model`, against the same exchange with
  the same model in nanoseconds (a greeting at 1.5 s and three lines): the same `events`, the same
  bytes received at the same instants, and the same instants heard; a `Model` with `supply`, told
  by `switch_off` and `switch_on`; and what a `Model` returns wrong, in `receive` and
  `quiet_until`, failing the air with its message.
- `tests/test_bluetooth_guest.py`: a Bumble `Device` on a `Host` stands in for Bluedroid. Its
  packets go through a real `BoardLink` held by a `HeldClock` to a `Bluetooth`, from a thread,
  `Runner`, that makes the turns as the runner does (§6, rule 1), each ending at the earliest of the
  harness's `quiet_until` and the first event of the slice plus the latency, and delivers each
  `hci_rx` at its time; the test waits as a test waits on the board. Power-on through the link;
  three pages of features, the fourth refused, page 0 with the BR/EDR features and no LE, the host's
  SSP on page 1, an ACL buffer of 491 and the address; an answer at exactly the latency. Each
  setting Bluedroid writes succeeding, and the event filters, IACs and counts it would need modelled
  refused; `Host_Number_Of_Completed_Packets` with no event; an inquiry answered only by a device
  scanning for its access code. On a link: the link policy, the packet type, the clock offset and
  the peer's version; the same with no link, refused. An unknown synchronous, asynchronous and
  vendor command, each answered in the event the host waits for and kept with its time, in the
  report and the properties. `HCI_Reset` in the middle of a link, with no word to the host, the
  peer's end falling 20 s later and a page meanwhile ending in `PAGE_TIMEOUT`; in the middle of a
  page, with no `Connection_Complete` at its timeout and the next page started; with an answer owed,
  so the next page connects; and the defaults back, the last three after `HCI_Reset`, a chip reset
  and a power cycle each, the connection accept timeout among them; the access codes read back, and
  the connection accept timeout, 5 s at first, written, and 0 refused. `bt_spp_initiator`'s session:
  inquiry by name, a link, SDP with the service name, PN, authentication with the PIN, SABM, no
  encryption, 2000 bytes to the peer and its answers back, every packet within 496 bytes and the ACL
  data within 491; the channel refused at SABM without an authentication; ACL data cut to the host's
  `Host_Buffer_Size`. An output due before its turn failing the link; a packet longer than the VHCI
  carries; an interface never used asking for nothing; `bt.air` refusing `wait`, `advance` and
  `run`, and following the link; the supply and a chip reset taking the controller off the air and
  back with no word to the host; the report and the properties, a device off the air marked; two
  runs crossing the same packets at the same instants. The backend's side: `board.reset()` sending
  none of the old host's packets in its turn, the supply and the runner's `reset` reaching the
  controller, and `board.bluetooth` after power-on refused for that before the missing ELF is named.
- `tests/test_air.py` also: a timer falling on its exact nanosecond; what it schedules running at
  that nanosecond, whether its float lands on the instant or above it; a timer a nanosecond away
  firing at the next nanosecond; a timeout of `math.inf` or 1e300 s, which never fires and stops
  the air 24 hours on.
  `tests/test_spp_peer.py`: a page started at 1 s, 3 s, 6 s or 10 s, and a remote name request
  started at 1 s or 6 s, ending at exactly the page timeout, with no peer on the air, measured by
  an advance past the end, which settles it only once.

`tests/test_bt_spp_initiator.py::test_with_its_hci_buffers_in_psram_the_firmware_pairs_with_and_streams_to_the_peer`
runs `bt_spp_initiator_psram` on `lilygo-t-a7670g-r2`, whose profile has `psram_mb = 4`, and asks
of it what the test of the plain image asks: the authentication, the channel, a `speed(...)` line,
the firmware's blocks at the peer, packets both ways and no unknown command. At `c99fb28`, the pin
before mikroverk/esp32sim#353, it failed with `NeverHappened`, nothing having crossed the VHCI
([failure modes](#failure-modes)), and was a strict `xfail` until the pin moved.

With firmware: `tests/test_bt_spp_initiator.py` runs ESP-IDF's `bt_spp_initiator`, unmodified,
through the `bt` fixture's controller, against an `SppPeer` with `secure=True`, the default, whose
source, `Heard`, is a `Model`
([validation firmware](validation-firmware-technical.md#bt_spp_initiator)). Bluedroid discovers
the peer by the name in its extended inquiry response, connects, reads its SDP record and prints
the service's name, asks for ERTM and stays in basic mode, sends RFCOMM's PN, pairs with the PIN
`1234` (legacy pairing, though the firmware has Secure Simple Pairing on), and opens the channel at
SABM, which the peer allows on the authenticated link; then it streams 990-byte writes at about
299 kbit/s, and the source hears each as written, at its time. With another PIN on the peer the
authentication fails (`AUTHENTICATION_FAILURE`), the link falls, and the firmware never sends SABM,
so `peer.refused` stays empty. With no peer, and with a `BluetoothDevice` of another name, its one
inquiry runs 38.4 s and it pages nobody. The controller knows every command Bluedroid sends in
these runs (`bt.unknown_commands()` is empty), and two runs are the same
(`tests/test_determinism.py::test_spp_to_a_peer`). Against a peer with `io_capability`
`no_input_no_output` the firmware pairs by Just Works, which Bluedroid allows by itself, with no
PIN and no number printed, and streams; both sides' `pairings` hold the one pairing, with the same
number and time, unauthenticated, and the report and the `bluetooth` property list it. Against a
peer with `display_only` or `display_yes_no` it asks for the passkey the peer shows
(`ESP_BT_GAP_KEY_REQ_EVT`), which the run cannot type, and Bluedroid's security timer ends it
35 s later with `User_Passkey_Request_Negative_Reply`: both sides' pairings fail with
`AUTHENTICATION_FAILURE`, the peer's with the passkey it showed. Two runs of the first are the
same, the numbers included (`tests/test_determinism.py::test_spp_with_simple_pairing`).

`tests/test_spp_peer.py`, for Secure Simple Pairing, with the stand-in's user playing the
firmware's: two users comparing one number, the key authenticated and the same on both sides,
only the initiator's host told the authentication is complete; a number either user says no to,
failing on both sides with no key; the stand-in typing the passkey the peer shows, read off
`peer.pairing`; a wrong passkey failing on both sides at one instant, neither left waiting; two
keyboards, with and without a passkey on the peer; Just Works from either side with no input and
output, allowed or refused by the side asked, a display only allowing it whatever it confirms,
unauthenticated; a peer with Secure Simple Pairing pairing with its PIN with a device without; a failed legacy pairing in both histories; and two
runs of two pairings, each with new numbers, showing the same. The `PeerError`s of the four new
arguments.

Not covered: a firmware that sees a peer leave, a firmware that receives on the channel (the
example only writes), encryption, a firmware that types a passkey or confirms a number, which
needs input on its console (#51), Secure Connections, and out-of-band data, which Bumble's
controller takes and nothing here gives. Nor is a clock offset other than zero. The air's time is exact to the nanosecond up to 2^22 s, about 48 days: from there
`round(when * 1e9)` works on a count of nanoseconds near 2^52, where a float's own error and the
product's add up to more than half a nanosecond, so a timer can fire a nanosecond early or late.
From 2^23 s, about 97 days, a float of seconds no longer tells one nanosecond from the next, timers
a nanosecond apart fall together, and where `time()` plus `_clock_resolution` rounds back to
`time()` the loop spins at one instant, with no error. No test goes that far. The page's timer runs
until the target's host accepts rather than until its baseband answers. With the defaults the
target's connection accept timeout, 5 s, ends a page its host leaves unanswered before the pager's
5.12 s, on both sides; but when the pager's page timeout is the shorter, as when the target's host
writes up to 29 s with `Write_Connection_Accept_Timeout`, a host that accepts after the page ended,
as after 6 s, makes a link on both ends when the pager has already reported `PAGE_TIMEOUT`, and the
pager's next page gets `CONNECTION_ALREADY_EXISTS`. Bumble's host accepts at the instant it hears,
so only a slowed host reaches this. For the same reason a target keeps the page it heard as a link
after that page ended, until its host answers or its connection accept timeout ends it, so the
pager's remote name request then gets the target's name, with page scan off too, where a real
controller would page again and end in `PAGE_TIMEOUT`. An acceptance with
`role=CENTRAL` after the page ended makes no link, where a plain one does: the pager refuses the
role switch. Bumble's host fails the air with `ObjectLookupError` when it accepts a peer's page with
`role=CENTRAL` after its own `Device.connect` to that peer failed, as when the peer rejected this
side's page while both paged: `connect` forgets the page waiting under that address, and
`Role_Change` then finds no connection. The controller makes the link; a host that sends
`Create_Connection` itself, as the test of this does, does not meet it. Every page a host hears is
taken to get one answer, as Bumble's host gives, until the answer's connection accept timeout. A
host that does not answer meanwhile: its entry stays until then, so this device's own pages to that
peer get `CONNECTION_ALREADY_EXISTS`, also after a page of its own has reported `PAGE_TIMEOUT`; and
an answer counted late stays owed until then, so after its end fell, or after this device's own page
to that peer made the link, the answer to the next page is taken for it, and that page ends at its
own connection accept timeout. `HCI_Reset`, which Bluedroid sends at every start, clears both at
once. A host that answers after its connection accept timeout, having heard
`CONNECTION_ACCEPT_TIMEOUT_EXCEEDED`, gets `UNKNOWN_CONNECTION_IDENTIFIER` when nothing else is
owed, and when the peer has paged again meanwhile its answer is taken for that page: an answer names
only the address. TODO(owner): Vol 4, Part E, 7.1.8 does not say what a controller answers an
acceptance with no request pending; only a real board's controller shows what ESP32's does. The
controller does not hold ACL data back for the host's `Host_Total_Num_ACL_Data_Packets` (7.3.38 to
7.3.40): Bluedroid turns controller-to-host flow control on and counts its buffers, and the
controller sends what it has. `Read_Local_Supported_Commands` gives Bumble 0.0.236's list, which
does not name every command the controller answers, and `Read_Local_Version_Information` Bumble's
version, 5.0. TODO(owner): what the classic ESP32's controller reports in both. A device that sends
SABM for a DLC with no PN before it finds no DLC in Bumble 0.0.236's multiplexer, which drops the
frame; Bluedroid sends PN first. A reset the firmware makes itself (a panic, the watchdog,
`esp_restart()`) reaches the controller only with the runner's `reset` message: a packet the harness
sent in the turn before, due after the reset, is in the runner's queue already, and the new host
reads it first, within the 10 µs of the latency. The firmware's controller leaves the air only at a
chip reset, `board.reset()` included, and when the board's supply goes off: the runner sends nothing
when the chip goes into a deep sleep, or when the firmware calls `esp_bt_controller_disable` or
`esp_bt_controller_deinit`. Through a deep sleep, and after either call, the controller stays on the
air with its scans and its links, as if its radio were on: a device can find it and page it, and a
link made before does not lose contact. A deep sleep ends with a reset, `DEEPSLEEP_RESET`, at which
the controller leaves the air and comes back; the peer's end of a link then falls at its supervision
timeout counted from the wake, not from the sleep. After `esp_bt_controller_disable`, what the
controller sends waits in the runner's queue, and a later `enable` delivers it; after `deinit`, the
runner drops it ([protocol, 5.22](../protocol.md#522-hci_tx-and-hci_rx-ext-34)).
([Decision](../decisions/harness.md#the-firmwares-controller-does-not-follow-a-deep-sleep-or-a-disabled-controller).)
When two pages cross, the controllers agree on one link and its central, but Bumble's host of a
device that heard the other's page during its own `Device.connect` may take itself for the
peripheral where its controller is the central: its `on_connection_request`, which accepts the page
heard, puts a peripheral's `Connection` in `pending_connections` over the central's that
`Device.connect` left there, and the `Connection_Complete` of either page takes it. Only a
`Role_Change` corrects it. Two pages at one instant are told apart by the order the loop runs them
in, and their link is the page whose acceptance runs first; a real controller is paging or scanning
at a given moment, and which page it answers is its own. That order can differ between Python
versions: two Bumble hosts that accept at once make the link of one page on 3.14 and of the other on
3.11, so which device is the central then is not part of what a test can rely on. An answer to a
synchronous link is not modelled: 7.1.9 lets `Reject_Connection_Request` decline one, and here, when
no answer to a page is owed, it gets `UNKNOWN_CONNECTION_IDENTIFIER_ERROR`, leaving the entry in
`sco_links` and the requester waiting; when one is owed, it is taken as that answer. Bumble's host
does not send it, so only a command written by hand reaches this. `Create_Connection_Cancel` for
Classic is not in Bumble 0.0.236, so `Device.connect(timeout=)` cannot end a page early. Two devices
searching the peer's SDP at the same instant fail the air with `Exception in callback
Host.on_packet(): invalid state`: Bumble 0.0.236's `sdp.Server` keeps one channel at a time.

## See also

- [Bluetooth peer](bluetooth-peer-user.md)
- [Decisions: the Bluetooth peer](../decisions/harness.md#the-bluetooth-peer-runs-bumble-in-the-harness-on-the-boards-clock)
- [Bumble](https://github.com/google/bumble), 0.0.236: `bumble/controller.py`, `bumble/link.py`,
  `bumble/rfcomm.py`
- ESP-IDF v5.3.6: `components/bt/host/bluedroid/device/controller.c`, `get_features_classic`
- [Bluetooth Core 5.4, Vol 4, Part E](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/Core-54/out/en/host-controller-interface/host-controller-interface-functional-specification.html),
  read on 2026-11-04: 6.6, 6.7, 6.21, 7.1.9, 7.3.16, 7.3.42, 7.7.33, 7.7.38, 7.7.46; read on
  2026-11-08: 4.5, 5.4.2, 7.1.14, 7.1.23, 7.1.24, 7.2.10, 7.2.12, 7.3.2, 7.3.3, 7.3.20, 7.3.22,
  7.3.38, 7.3.39, 7.3.40, 7.3.43, 7.3.44, 7.3.45, 7.3.48, 7.3.52, 7.7.12, 7.7.28, 7.7.29; [Vol 1, Part F](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/Core-54/out/en/architecture,-mixing,-and-conventions/controller-error-codes.html):
  2.4, 2.8, 2.11
