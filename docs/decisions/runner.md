# Runner decisions

Why the `esp32tl-runner` crate in `runner/`, its esp32sim pin and the link protocol it speaks
with the harness ([`docs/protocol.md`](../protocol.md)) are the way they are.

## esp32sim is pinned at the merge commit of the chip-revision fix

Status: decided (#43)

`runner/Cargo.toml` takes `esp32`, `esp-soc` and, as a dev-dependency, `emu-core` from git at
`8c652151dd5a521249a785ca147f20a9f8eda68f`, the merge of mikroverk/esp32sim#322. Beyond stage 0's
fix build it brings mikroverk/esp32sim#300 (LL Privacy in the virtual BLE controller) and #307
(roadmap documentation). On it, `hello_world` built for v3.0 boots after its power-on reset only,
and every stage 0 run of the production images executes the same instructions as on the fix
build. That revision in `runner/Cargo.toml` is the project's esp32sim pin.

**Rejected:** upstream's head at the time, because it is one pull request further,
mikroverk/esp32sim#314 (classic timer-group interrupt lines ignore `INT_ENA`), which no firmware
had been run on and which touches the timer path M9 depends on. The merge commit is the smallest
step that was checked.

## The esp32sim command-line crate is not a dependency

Status: decided (#45)

The runner depends on `esp32` and `esp-soc` only, although #5 listed the `esp32sim` crate as well.
`src/cli.rs` has its own parser, with esp32sim's flag names and value formats (`--chip`,
`--flash-at`, `--max-seconds`, `--rom`, `--flash-mb`, `--psram-mb`), so that switching to
`esp32sim --board socket:` once mikroverk/esp32sim#325 lands is a rename.

**Rejected:** depending on the `esp32sim` crate, because the runner uses nothing from it, it would
compile the S3, C3, C6 and P4 models too, and its argument parser exits on flags it does not know,
so the runner could not reuse it for `--board socket:` either.

## Cargo runs in runner/, against a pinned toolchain

Status: decided (#45)

`runner/rust-toolchain.toml` pins Rust 1.99.1, the stable release the runner was first built
with. `rust-version = "1.95"` in `Cargo.toml` records the real minimum for a toolchain not from
rustup: esp-soc at the pin calls `AtomicUsize::try_update`, stable since 1.95. rustup reads the
toolchain file only from the directory cargo runs in, so the `Makefile` and the `cargo-fmt` hook
run cargo in `runner/`. `Cargo.lock` is committed and the Makefile's builds, tests and clippy pass
`--locked`.

**Rejected:** `cargo --manifest-path runner/Cargo.toml` from the root, because cargo then uses the
default toolchain (1.93 on the machine the crate was written on) and stops at
`requires rustc 1.95`.

## The dev profile is optimised, without fat LTO

Status: decided (#45)

`[profile.dev]` has `opt-level = 3` with overflow checks and debug assertions off, and no LTO;
`[profile.release]` copies upstream's (`lto = "fat"`, `codegen-units = 1`), because the emulator's
speed depends on it. `RUNNER_PROFILE=dev` is for local iteration: after a one-line change, a
release rebuild takes 10.8 s and a dev rebuild 0.26 s.

**Rejected:** an unoptimised `dev` profile, because an unoptimised emulator is too slow to boot an
image, and turning on overflow checks, because upstream builds and tests only in release, so its
code has never run with them.

## make test-runner follows RUNNER_PROFILE

Status: decided (#45)

`make test-runner` runs `cargo test --locked --profile $(RUNNER_PROFILE)`, the profile
`build-runner` uses. In release the test binary still goes through a fat-LTO link, 5.9 s when it
was measured: that costs a link, not a rebuild.

**Rejected:** cargo's default test profile, because it would build esp32sim a second time in
another profile.

## The runner looks for the ROM the way esp32sim does

Status: decided (#45)

Without `--rom`, `find_rom` in `runner/src/main.rs` tries `$ESP32SIM_ROM_DIR`, then
`~/.espressif/tools/esp-rom-elfs/*/`, newest release first, as esp32sim does, for runs by hand.
It does not prefer the pinned release `20241011` and does not read `$IDF_TOOLS_PATH`, unlike
`scripts/fetch-rom-elf`; that does not matter for the tests, because the `esp32sim` backend always
passes `--rom`
([harness.md](harness.md#the-runner-and-rom-paths-default-to-the-checkout-with-environment-overrides)).

## A Stop variant added upstream breaks the build

Status: decided (#45)

`stop_name` in `runner/src/main.rs` is an exhaustive `match` over esp-soc's `Stop`, which names
the variant in the runner's last stderr line,
`esp32tl-runner: stop: VARIANT at T s (W s wall, N resets)`.

**Rejected:** a catch-all arm, because a variant added upstream would then be reported under
another name.

## The socket board is postponed to M1

Status: superseded (#11)

`--console socket` and `--console both` were parsed but exited 2 until `--board socket:PATH`
existed, and the runner only reported a reset's cause on stderr. #11 brought the socket board:
see [The socket board records, and the run loop does the socket work](#the-socket-board-records-and-the-run-loop-does-the-socket-work).

## The socket board records, and the run loop does the socket work

Status: decided (#11)

`SocketBoard` in `runner/src/socket_board.rs` implements `uses_uart_pins`, `uart_tx` and
`gpio_output_at` only, and pushes what they report, with its bus cycle, into a log it shares with
`Session` (`Rc<RefCell<Recorder>>`). `Session` turns the log into messages between slices, where
it has the bus for `uart_cfg`, the drop warnings and the reset routes, and sends `reset` itself
on `Stop::SwReset`. `--console socket` and `both` need `--board`, and the parser says so.

**Rejected:** writing to the socket from the board callbacks, because they get no access to the
bus and would block the emulator in the middle of an instruction on a peer that does not read.

## A signal sets a flag, and the socket never blocks longer than 20 ms

Status: decided (#11)

With `--board`, `SIGTERM` and `SIGINT` go to signal-hook's `flag::register`, preceded by
`flag::register_conditional_shutdown`, so the first signal only sets an `AtomicBool` and a second
one exits at once with `128 + N`. The socket has a 20 ms read and write timeout (`POLL` in
`runner/src/transport.rs`), after which `SocketLink` looks at the flag and at `--peer-timeout`.
The handlers go in only after the connection, so a runner stuck connecting dies of a signal as
before.

**Rejected:** a flag-only handler around blocking reads, because with `SA_RESTART` a blocked read
resumes after the signal and the backend's `SIGKILL` comes 5 s later. Also rejected: a self-pipe
with `poll(2)`, because it needs `libc` or more of signal-hook for what a 20 ms timeout already
does.

## Writes go out at sync, at 64 KiB and after 20 wall milliseconds

Status: decided (#11)

`SocketLink` queues lines and writes them out at every `sync`, when 64 KiB are queued, and from
`Link::idle` between slices once the oldest queued line has waited 20 ms of wall time. A coalesced
`uart_tx` or `console` message is closed at the first slice boundary a character time after its
last byte. When the peer reads cannot change what it reads, since outside its turn it only reads,
so this costs no determinism.

**Rejected:** writing only at `sync` and when the buffer fills, the first version, because a run
with no turns (`quiet_until` and `latency` both `null`, as a GNSS replay has) then gave the peer
the console only at the end of the run, and `--console socket` keeps it off stdout.

## The last messages of a run ignore the signal and get two seconds

Status: decided (#11)

`Session::finish` calls `Link::closing` first: from then on a write no longer gives up on the
signal flag, and gives up on a peer that does not read after 2 s (`TEARDOWN`), under the 5 s after
which the backend sends `SIGKILL`. A write cut short keeps what it has not written, so the next
one goes on from there instead of sending part of the stream twice.

**Rejected:** letting the signal interrupt the last writes too, the first version, because after a
`SIGTERM` any write that had to wait, even 20 ms, was abandoned and the peer got no `stats`. Also
rejected: `--peer-timeout` as the limit, because its default of 60 s is past the backend's
`SIGKILL`.

## A slice is at most latency long while no event is unacknowledged

Status: decided (#11)

Besides the slice ends of protocol 6.1, `slice_end` caps a slice at `latency` while no event is
unacknowledged ([protocol §11](../protocol.md#11-changes-from-the-plan), item 15): an event in the
slice sets `B = t + latency`, and the callback that records it cannot stop `run`. It is slow with a
small `latency`: `hello_world` runs at about 30 wall seconds per emulated second with a `latency`
of 0, 0.3 with 1 µs and 0.1 without, on an M2 Pro.

**Rejected:** slices of up to a millisecond only, because a request sent mid slice with the guest
asleep in WFI would be seen only at the next millisecond, and an answer stamped `t + latency`
would reach the chip up to that late.

## The runner reads the socket only in the peer's turn

Status: decided (#53)

The runner reads the socket only between its `sync` and the peer's `wait`, and the peer writes
only then; every turn ends with exactly one `wait`
([protocol §11](../protocol.md#11-changes-from-the-plan), item 1).

**Rejected:** the plan's rule, which let data acknowledge an event and did not say when the runner
reads, because applying whatever had arrived by wall time would let a fast and a slow peer move
the block point, and with it the slice ends and `uart_cfg` times: the two would see different
runs.

## sync is sent only at the block point, and lateness is measured against it

Status: decided (#53)

The runner sends `sync` once per block point `B`, with `t = B`, and rule 2 measures a late `t`
against that `t` (protocol §11, items 2 and 3). The runner stops up to one CPU cycle after `B` at
every clock below 240 MHz, ESP-IDF's default 160 MHz included
([protocol §6.1](../protocol.md#61-how-the-runner-meets-the-block-point)).

**Rejected:** the plan's `sync` at every `advance_to`, at most once per emulated millisecond,
because a `sync` that hands over no turn gives the peer nothing to act on. Also rejected:
measuring lateness against where the runner stopped, because an answer at exactly
`t_req + latency` would then be late at those clocks.

## The run loop works in slices instead of BoardModel::next_deadline

Status: decided (#53)

The runner calls `Machine::run` in slices bounded by `max_cycles` and does the work that needs the
bus between them: `uart_cfg`, the drop warnings, delivering paced bytes and `adc`. `run` cuts its
last round at the ceiling (`esp-soc/src/machine.rs:691` at `8c65215`). `SocketBoard`'s callbacks
only record events (protocol §11, item 10).

**Rejected:** `BoardModel::next_deadline`, as plan 4.2 had it, because `BoardModel` callbacks get
no access to the bus, and its reason (an idle guest would "jump to the next timer" past `B`) does
not hold at the pin: the idle skip is capped at 512 cycles. Also rejected:
`Machine::run_until_cycle`, which stops exactly but schedules core 0 only, while the classic chip
has two cores.

## The harness's hello names the pins to watch

Status: decided (#53)

The harness's `hello` carries `watch`, the pins whose events it wants (protocol §11, item 7).

**Rejected:** reporting every pin, because `uart_tx` fires for UART0 too, so every console byte and
every LED toggle would be an event the peer must acknowledge within `latency`.

## SIGTERM ends a run with stats and exit 0, and the esp32sim backend sends it

Status: decided (#53)

`SIGTERM` or `SIGINT` ends the slice in progress, sends the pending events and warnings and
`stats` with `end: signal`, and exits 0. The backend's `board` teardown, before the `dut` is torn
down, sends `SIGTERM`, reads the socket to end of file within a wall-time limit, waits, and sends
`SIGKILL` only after 5 s ([protocol §9](../protocol.md#9-ending-a-run)). The runner does this with
`--board` since #11, and the backend starts it with `--board` since #12.

**Rejected:** a fixed turn cadence so that `stop` always arrives soon, because tests usually end
between turns, when `console.expect` matches. Also rejected: leaving the end to pytest-embedded's
teardown, because it calls `terminate()` and `kill()` back to back
(`pytest_embedded/plugin.py:505-506` in 2.9.3) and does not know the runner process.

## Merged warnings go out only when another message is due

Status: decided (#53)

Consecutive drops of one kind on one pin and port merge into one `warn`, which goes out when any
other message is due, with no time limit. `stats` is a message too, so the last warnings still go
out before every end that sends it
([protocol §5.8](../protocol.md#58-warn-ext)).

**Rejected:** flushing at every slice end, because each paced byte ends a slice, so a frame
overflowing the RX FIFO gave one warning per byte. Also rejected: a one-millisecond window,
because it still merged nothing at 9600 baud, where a character takes 1.04 ms.

## Received bytes are paced at the uart_rx message's baud

Status: decided (#53)

Bytes of a `uart_rx` enter the RX FIFO one character time (10 bit times, 8N1) apart at the
message's `baud`, the peer's line rate (protocol §11, item 6).

**Rejected:** pacing at the route's baud, as the plan and mikroverk/esp32sim#325 have it, because
the route's baud may be unset, and a mismatch should reach the UART as the frame error it is.

## GPIO input, release and ADC are postponed to M3

Status: superseded (#23)

Until #23 the runner answered `gpio_in`, `release` and `adc` with a protocol error naming the
message (exit code 3), and sent no `gpio_out`. #23 brought GPIO and the future-input queue: see
[GPIO inputs reach the chip through the board, at the slice boundary](#gpio-inputs-reach-the-chip-through-the-board-at-the-slice-boundary).
ADC: see [adc waits for an example with an analog pin](#adc-waits-for-an-example-with-an-analog-pin).

## GPIO inputs reach the chip through the board, at the slice boundary

Status: decided (#23)

`gpio_in` and `release` go into the runner's input queue with `uart_rx`. When one comes due, the
run loop hands its levels to `SocketBoard` (`Recorder::drive`, `Recorder::release`) and has the
chip take them at once through `take_edges` and `released_inputs`, with `SocBus::gpio_set_input`
and `gpio_release_input`, as the bus's `deliver_board_inputs` would
([protocol 5.11](../protocol.md#511-gpio_in-and-release-m3), item 16 of §11).

**Rejected:** leaving them to the bus's next tick, because that tick comes after the next round
of instructions: a level would raise its interrupt up to a quantum after its time, or an idle
chunk with the guest asleep in `waiti`. Also rejected: applying them with
`SocBus::gpio_set_input` without the board, because the plan and mikroverk/esp32sim#325 put GPIO
input through the `BoardModel` trait, and a board that holds the levels can give them back through
`input_levels` when a reset attaches it again.

## After a reset, gpio_out reads the pads from GPIO_ENABLE and GPIO_OUT

Status: decided (#23)

A chip reset calls no hook, so the runner reports the watched pins itself
([protocol 5.5](../protocol.md#55-reset)): `enabled` and `output` are `GPIO_ENABLE` and
`GPIO_OUT` read from the bus (`Guest::gpio_drive`), and `changes` holds every watched pin whose
pad level differs from the one the peer last saw in `changes` (`Recorder::levels_reported`),
taking a pad as driven high where both words have its bit. That is the pad's state right after
`reboot()` at the pin, which leaves both words 0 and IO_MUX at function 0, through which esp32sim
drives no pad; `on_the_emulator_no_pad_is_driven_after_a_chip_reset` checks it.

**Rejected:** comparing the new state with the last report's `enabled` and `output`, because
`changes` are pad levels and the two words are not: a pad UART TX drove high, with `GPIO_OUT` at
0, would never be reported low. Also rejected: the pad state itself, which esp32sim keeps private
(`ClassicGpio::driven`), because getting it means a change upstream that nothing needs at the
pin, where it would give the same answer; protocol §11, item 17 records what would change that.

## input_on_output looks at GPIO_ENABLE only

Status: decided (#23)

The runner warns `input_on_output` for a `gpio_in` level on a pin whose bit is set in
`GPIO_ENABLE` when the level comes due, one warning per pin and message
([protocol 5.8](../protocol.md#58-warn-ext)). A pad a peripheral drives with its own output
enable, or an IO_MUX function, gives no warning: which pads those drive is in esp32sim's private
`ClassicGpio::driven`.

**Rejected:** the union of `ClassicGpio::output_pins` over every output signal, which is public,
because for an IO_MUX function it ignores the output enable and would warn about pads the guest
only reads.

## adc waits for an example with an analog pin

Status: postponed (#23)

No example needs an analog pin yet, so #23 left `adc` out. The runner answers it with the
protocol error `adc is not implemented yet` (exit code 3) until it comes back as an issue of its
own, when an example needs one ([protocol §5](../protocol.md#5-messages)).

## The SD card is a model of SPI mode in the runner, not esp32sim's card

Status: decided (#32)

The card of `--sd-rw` is `runner/src/sd_card.rs`: an SD memory card in SPI mode, answering each
SPI transaction byte for byte in the board's `spi_transfer_pins`, whose `SpiPins` gives the routes
and the CS mask, so no esp32sim change is needed (plan M7). It takes only the image store from
esp32sim (`esp_periph::sdmmc::FileStore`, write-through), and copies the identity of esp32sim's own
card (CID `ES`/`SIMSD`, the serial from the size), so the two read as the same card. Validated on
ESP-IDF's unmodified `sdspi` example, whose 512-byte blocks go through the classic SPI DMA path:
esp32sim models that path, so there is nothing to file upstream about `SDSPI_DEFAULT_DMA`.

**Rejected:** driving esp32sim's `esp_periph::sdmmc::SdCard` through `SdDevice`, because it is a
card on the SD bus: in SPI mode there is no RCA, CMD2, CMD3 or CMD7, CMD9 and CMD10 send data
blocks, and its ACMD41 stays busy for the empty voltage window SPI hosts send. Also rejected:
answering the card on the socket, because every byte of a transaction is answered within it, and
the peer is a turn away; and asking upstream for an SPI mode first, because the runner shows it
works without one.

## The SD card answers at once and has no CMD6

Status: decided (#32)

The card is never busy after a write, an erase or CMD12, and sends a data block one byte after
its R1; the image is a seek and a read or write per block. Without class 10 in its CSD, ESP-IDF
does not send CMD6 and stays at the default 20 MHz, which `sdspi` asks for anyway. The tests of
interest are the firmware's file handling, not the card's timing.

**Postponed:** busy time (esp32sim's card has `prg_ns` for it), CMD6 and high speed, CMD23, and
locking and write protection: each when a firmware needs it.

## Under --realtime the runner waits at each block point until wall time reaches it

Status: decided (#27)

`--realtime` sets `machine.rt.enabled`, as esp32sim's flag of the same name does, and the runner
also waits, before each `sync`, until its own wall clock reaches the block point `B`, on the
socket with the time left as the timeout ([protocol 12.2](../protocol.md#122-the-runner-waits-for-wall-time-at-the-block-point)).
The harness can then hand the device's bytes over at the turn whose time they arrived by, and a
byte is stamped within the emulated millisecond after it arrived.

**Rejected:** esp32sim's pacing alone, because it lets the guest reach `B` up to about 2.3 ms
before wall time does, and the device's bytes up to `B` would be stamped at the next turn, up to
3.3 ms late. Also rejected: pacing in the runner alone, without `machine.rt.enabled`, because
`--realtime` would then mean something other than esp32sim's flag, which the runner's flags copy so
that a switch to `esp32sim --board socket:` is a rename; waiting after the peer's turn instead of
before `sync`, because the harness would then take its turn before the bytes up to `B` had
arrived; and a sleep, because the socket wait sees a peer that has gone, in the same 20 ms steps
in which it sees a signal.

## The runner starts its wall clock with esp32sim's pacing, and then follows esp32sim's resyncs

Status: decided (#27)

The runner turns on `machine.rt.enabled` at the first slice boundary at or after 2¹⁶ bus cycles,
just before it calls `run` again, and anchors its wall clock there, emulated time 0 being now, as
esp32sim's first check, in the first round of that `run`, anchors its own
([protocol 12.2](../protocol.md#122-the-runner-waits-for-wall-time-at-the-block-point)). It moves
its anchor only when `rt.resyncs` grows, to the slice boundary where it sees that. The wall time a
held turn takes is therefore caught up by running the guest flat out when it is under 500 ms, and
skipped past that, as esp32sim does with its own anchor, which is private
(`Realtime::wall_start`). `stats` reports the skips as `rt_resyncs`.

**Rejected:** turning pacing on at power-on and anchoring the runner's clock where the guest's first
instruction runs, the first version of this design, because esp32sim anchors only at its first
check, 2¹⁶ bus cycles in, and does not count that as a resync: a turn the test held in between
would set the two anchors apart by its whole length for the rest of the run, and the runner would
take the guest for behind and stop waiting at the block points.

Also rejected: moving the anchor after every turn, because each turn of a `real` source costs
harness time every emulated millisecond, and leaving it out of the clock would run the guest slower
than wall time: a 0.3 ms turn per millisecond runs it at 0.77 times real time, behind a device that
does not slow down. Also rejected: catching up every held turn whatever its length, because a test
stopped in a debugger for minutes would then race the guest through those minutes, every stamp
squeezed together; and a threshold of the runner's own, because two pacers with different
thresholds would disagree on where wall time stands.
