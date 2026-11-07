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
2. **Settling an instant.** `Air._settle` schedules every party's `poll(now)` with `call_soon`,
   then runs the loop idle: what the parties, the timers due at `now` and what those schedule
   with `call_soon` all run at `now`, and the loop stops only when what is left is a timer after
   it. An advance that stops at an instant and then ends there settles it twice, so only an
   advance past an instant shows whether everything due at it ran there; and a party's poll runs
   the loop once more at every instant, so only an air without parties shows it.
   `Air.call(function)` runs `function` inside the loop the same way, or at once when called from
   inside it; `Air.start(coroutine)` makes a task there. Bumble's `Controller` takes the running
   loop in its constructor, so everything Bumble is made through `call`.
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
     how a controller reports it for `Create_Connection`; here it is the command status. #34 can see
     what ESP32's controller does.
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
     replaces the first. `_answers_due` counts the answers the host owes each peer. When the page's
     end falls, `_link_timed_out` drops it through `_drop_heard` without telling the host, since no
     link was made, and that makes all the answers still owed for it late, in `_late_answers`.
     `_take_answer`, which both handlers of an answer call, counts the answer. One nothing was
     owed for gets `UNKNOWN_CONNECTION_IDENTIFIER_ERROR`, as the host's own error, on a working link
     and during this side's own page too. It gives the page the answer is for, from `_heard`, unless
     one is counted late, or `_heard` holds no page from the peer still waiting for an answer, as
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
     names only the address, so answers are told apart only by their order: the oldest is taken as
     the late one, and with a page tried again while the first answer is on its way, that first
     answer connects the second page, and the second answer is the late one. A link being made marks
     nothing late, so that an answer a host never gives does not take the next page's, except when
     this side's own page makes the link while it holds a page heard from that peer: that page can
     make no link any more, so `on_classic_connection_complete` drops it through `_drop_heard`, and
     the answers still owed for it are late. Without that it would stay in `_heard` after the link
     ended, and refuse this side's next page with `CONNECTION_ALREADY_EXISTS`.
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
     `INVALID_COMMAND_PARAMETERS_ERROR`, and the host still owes its answer. Then `_take_answer`, so a
     rejection is an answer like an acceptance. One for a page that still waits: a command status of
     `SUCCESS`, `LMP_not_accepted` for `LMP_HOST_CONNECTION_REQ` with the reason, the page removed
     from `_heard` with whatever a page whose pager left has running (`_forget_link`), this side's
     own page left as it is, and `Connection_Complete` to
     this side's host with the reason as its status, unless this side is itself paging that peer, for
     the same reason as a late answer. With a page tried again, the first rejection ends the second
     page, and the second answer finds no page in `_heard` and is late. Bumble's
     completion of the page gives the pager's host `Connection_Complete` with the reason, as 7.1.9
     requires. A pager whose page already ended has told its host: `on_lmp_packet` drops the rejection
     and the page's future, or its host would hear of that page twice. A pager out of reach hears
     nothing, and its page ends at its timer. TODO(owner): 7.1.9 says this side's host gets
     `Connection_Complete` and gives its status only for the pager's; here it is the reason, as
     [the decision](../decisions/harness.md#a-host-that-rejects-a-page-gets-the-reason-too) says. #34
     can see what ESP32's controller does.
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
`classic_allow_role_switch`, `classic_role_change`, `classic_pending_commands`,
`on_classic_connection_complete`, which gives a handle to the entry it finds for the address,
`on_classic_disconnected`, `on_classic_remote_name_response`, `on_classic_role_change_request`,
`on_classic_connection_request` for a synchronous link, `on_hci_create_connection_command`,
`on_hci_remote_name_request_command`, the `on_hci_<command>` dispatch by name, `on_lmp_packet`,
`send_lmp_packet`, `send_hci_packet`. `bumble.controller.Connection`, which a page heard is made
of. `bumble.device.Device`: `auto_restart_inquiry`. Connection state is keyed by address, so a
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
  outlasts it.
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
- `tests/test_air.py` also: a timer falling on its exact nanosecond; what it schedules running at
  that nanosecond, whether its float lands on the instant or above it; a timer a nanosecond away
  firing at the next nanosecond; a timeout of `math.inf` or 1e300 s, which never fires and stops
  the air 24 hours on.
  `tests/test_spp_peer.py`: a page started at 1 s, 3 s, 6 s or 10 s, and a remote name request
  started at 1 s or 6 s, ending at exactly the page timeout, with no peer on the air, measured by
  an advance past the end, which settles it only once.

Not covered: firmware. esp32sim has no Classic controller for the guest, so no ESP32 image talks
to a peer yet, and no firmware has seen a peer leave. Nor is a clock offset other than 0. The
air's time is exact to the nanosecond up to 2^22 s, about 48 days: from there `round(when * 1e9)`
works on a count of nanoseconds near 2^52, where a float's own error and the product's add up to
more than half a nanosecond, so a timer can fire a nanosecond early or late. From 2^23 s, about 97
days, a float of seconds no longer tells one nanosecond from the next, timers a nanosecond apart
fall together, and where `time()` plus `_clock_resolution` rounds back to `time()` the loop spins
at one instant, with no error. No test goes that far.
There is no `Connection_Accept_Timeout` (Vol 4, Part E, 6.7), and the page's timer runs until the
target's host accepts rather than until its baseband answers: a host that accepts after the page
ended, as after 6 s, makes a link on both ends when the pager has already reported `PAGE_TIMEOUT`,
and the pager's next page gets `CONNECTION_ALREADY_EXISTS`. Bumble's host accepts at the instant it
hears, so only a slowed host reaches this. For the same reason a target keeps the page it heard as a
link after that page ended, so the pager's remote name request then gets the target's name, with
page scan off too, where a real controller would page again and end in `PAGE_TIMEOUT`.
An acceptance with `role=CENTRAL` after the page ended makes no link, where a plain one does: the
pager refuses the role switch. Bumble's host fails the air with
`ObjectLookupError` when it accepts a peer's page with `role=CENTRAL` after its own
`Device.connect` to that peer failed, as when the peer rejected this side's page while both paged:
`connect` forgets the page waiting under that address, and `Role_Change` then finds no connection.
The controller makes the link; a host that sends `Create_Connection` itself, as the test of this
does, does not meet it.
Every page a host hears is taken to get one answer, as Bumble's host gives. One that never answers:
its entry stays with nothing to end it, so this device's own pages to that peer get
`CONNECTION_ALREADY_EXISTS`, also after a page of its own has reported `PAGE_TIMEOUT`; and an answer
counted late stays owed, so after its end fell, as across a restart of its firmware, or after this
device's own page to that peer made the link, the answer to the next page is taken for it and that
pager gets `PAGE_TIMEOUT`.
When two pages cross, the controllers agree on one link and its central, but Bumble's host of a
device that heard the other's page during its own `Device.connect` may take itself for the
peripheral where its controller is the central: its `on_connection_request`, which accepts the page
heard, puts a peripheral's `Connection` in `pending_connections` over the central's that
`Device.connect` left there, and the `Connection_Complete` of either page takes it. Only a
`Role_Change` corrects it. Two pages at one instant are told apart by the order the loop runs them
in, and their link is the page whose acceptance runs first; a real controller is paging or scanning
at a given moment, and which page it answers is its own.
An answer to a synchronous link is not modelled: 7.1.9 lets `Reject_Connection_Request` decline one,
and here, when no answer to a page is owed, it gets `UNKNOWN_CONNECTION_IDENTIFIER_ERROR`, leaving the
entry in `sco_links` and the requester waiting; when one is owed, it is taken as that answer. Bumble's
host does not send it, so only a command written by hand reaches this.
`Create_Connection_Cancel` for Classic is not in Bumble 0.0.236, so `Device.connect(timeout=)` cannot
end a page early. Two devices searching the peer's SDP at the same instant fail the air with
`Exception in callback Host.on_packet(): invalid state`: Bumble 0.0.236's `sdp.Server` keeps one
channel at a time.

## See also

- [Bluetooth peer](bluetooth-peer-user.md)
- [Decisions: the Bluetooth peer](../decisions/harness.md#the-bluetooth-peer-runs-bumble-in-the-harness-on-the-boards-clock)
- [Bumble](https://github.com/google/bumble), 0.0.236: `bumble/controller.py`, `bumble/link.py`
- [Bluetooth Core 5.4, Vol 4, Part E](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/Core-54/out/en/host-controller-interface/host-controller-interface-functional-specification.html),
  read on 2026-11-04: 6.6, 6.7, 6.21, 7.1.9, 7.3.16, 7.3.42, 7.7.33, 7.7.38, 7.7.46; [Vol 1, Part F](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/Core-54/out/en/architecture,-mixing,-and-conventions/controller-error-codes.html):
  2.4, 2.8, 2.11
