# Runner decisions

Why the `esp32tl-runner` crate in `runner/`, its esp32sim pin and the link protocol it speaks
with the harness ([`docs/protocol.md`](../protocol.md)) are the way they are.

## esp32sim is pinned at the merge commit of the deep-sleep timer wake

Status: decided (#35)

`runner/Cargo.toml` takes `esp32`, `esp-soc`, `esp-periph` and, as a dev-dependency, `emu-core`
from git at `afcfba65da0da30a35fb0a7c381854c62b92e338`, the merge of mikroverk/esp32sim#336, with
which a classic chip in deep sleep wakes on its RTC timer
([stage 0, gap 4](../stage0-findings.md#gaps)). On it, `tools/deepsleep` boots 12 times in a 60 s
run, every boot after the first with wake cause 4 (`ESP_SLEEP_WAKEUP_TIMER`) after a
`DEEPSLEEP_RESET`; on the previous pin it boots once. `tests/test_timer_wake.py` keeps it so: it
runs `firmware/timer_wake` through two sleeps, and fails with `NeverHappened` on the previous pin.

Besides #336, these changes beyond the previous pin are on the classic chip's paths that every
firmware, or a part of esp32tl, goes through:

- mikroverk/esp32sim#314: the timer-group interrupt lines ignore `INT_ENA`. #35 was to run the
  deep-sleep validation again on it.
- mikroverk/esp32sim#290: `Timer::step` of the timer groups (`esp-periph/src/timg.rs`) skips the
  prescaler division below the divisor, on the same timer path.
- mikroverk/esp32sim#315: a received Wi-Fi frame raises MAC cause bits 24, 5 and 2
  (`EVENT_RX` in `esp32/src/wifi.rs`), the interrupt every `eth_rx` reaches the station through.
- mikroverk/esp32sim#323: `CCOUNT` restarts at a CPU reset, so also at every wake from deep sleep.
- mikroverk/esp32sim#332: IO_MUX function 0 is GPIO on the 24 pads whose ESP-IDF header says so
  (`GPIO_AT_FUNCTION_0` in `esp32/src/periph.rs`).
- mikroverk/esp32sim#313: SPI1 starts with its reset command phase, `USR_COMMAND` and an 8-bit
  command (`SpiMem::new` in `esp-periph/src/spi_mem.rs`), which the classic chip's SPI model
  wraps.
- mikroverk/esp32sim#329, which brings #316 (a sleeping classic core tests its armed timer lines
  inline) and #321 (`QUOU`, `QUOS`, `REMU` and `REMS` compiled to native AArch64 code).

The evidence that none of them changes what esp32tl relies on is the whole test suite, which
passes on the pin unchanged, and every esp32sim line the documentation cites, which is still
there, moved at most. The production images of stage 0 are not in this repository and were not
run again. That revision in `runner/Cargo.toml` is the project's esp32sim pin.

**Rejected:** upstream's head at the time, `0437f14`, because it also carries
mikroverk/esp32sim#344 (the ROM's UART download mode), which changes what a byte on a UART's RX
pin does when the 128-byte receive FIFO is full: `ClassicUart::host_input` queues it and releases
it at the baud rate instead of dropping it. The runner counts such a byte as dropped and sends
`rxfifo_ovf` ([protocol, 5.8](../protocol.md#58-warn-ext)) before it hands it over, so on that
revision it would report a drop that did not happen, and a firmware that falls behind its UART
would not lose bytes as the chip does. The merge commit is the smallest step that was checked.

## esp32sim is pinned at the merge commit of the chip-revision fix

Status: superseded (#35)

`runner/Cargo.toml` took the esp32sim crates from git at
`8c652151dd5a521249a785ca147f20a9f8eda68f`, the merge of mikroverk/esp32sim#322, with which
`hello_world` built for v3.0 boots after its power-on reset only. Upstream's head at the time was
rejected because it carried mikroverk/esp32sim#314 as well, which touches the timer path M9 depends
on. The pin moved for the deep-sleep timer wake: see
[esp32sim is pinned at the merge commit of the deep-sleep timer wake](#esp32sim-is-pinned-at-the-merge-commit-of-the-deep-sleep-timer-wake).

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

## A deep sleep runs in long steps of the RTC, to just short of its alarm

Status: decided (#74)

While the chip is in deep sleep, the runner moves esp32sim's bus on itself, with `Bus::tick` in
steps of up to 2^31 bus cycles (about 9 s), and stops two RTC slow ticks before the sleep timer's
alarm; esp32sim's own idle rounds then run the last ticks and wake the chip, as they would without
the skip. A tick of the bus ticks every peripheral, the RTC among them, so the RTC timer still
wakes the chip and the firmware's clock still counts the sleep. In a sleep the runner steps through,
neither the next emulated millisecond nor `latency` ends a slice: the digital domain is off, so the
only event is the wake, which ends `run` at its cycle, and no UART route can change. Turns, queued
inputs, paced bytes and the end of the run still end it. esp32sim's idle loop advances a sleeping
chip in rounds of 512 CPU cycles (`IDLE_CHUNK`), and the RTC timer's wake is no deadline its idle
skip stops at, so the sleep's cost was esp32sim's, not the run loop's slices: a day of
15-minute sleeps took 330 s of wall time on an M2 Pro, and takes 7 s with the skip, nearly all of it
in the 96 boots; a 15-minute sleep takes 2.4 µs.

The runner tells a deep sleep by what esp32sim's `enter_deep_sleep` asks of the RTC registers,
`SLEEP_EN` and `DG_WRAP_PD_EN` both set, and by what its `peripheral_event` then does to the cores,
which wait with no interrupt enabled. It reads the alarm from `SLP_TIMER0`/`SLP_TIMER1` and
`WAKEUP_ENA`, as esp32sim's `sleep_ticks_left` does, and converts slow ticks to bus cycles with
esp32sim's `RtcSlow` divider, 1600, which it reads from `esp32::periph::Peripherals::CLOCKS`. It
does not step through a sleep under `--realtime`, which paces the sleep to wall time, nor with the
RTC watchdog on, which counts through the sleep in esp32sim: a stage due in a step would fire at
the step's end, up to 9 s late, with its reset. Such a sleep keeps an awake chip's slice ends, so
that a signal still ends its slice within a millisecond.
The wake comes in esp32sim's first round at or after the alarm's tick either way, but the rounds end
at other cycles after a skip: a wake can come up to one round, 512 CPU cycles, from where it would
without one, a few microseconds of emulated time.

**Rejected:** leaving the sleep to esp32sim's idle rounds, because a day of the board's time costs
minutes of wall time, where a test of a day of wakes is to take seconds (#74). Setting the bus's
cycle counter forward, as the run loop does with the supply off, because the RTC would not count the
sleep: the timer would never wake the chip, and the firmware's clock after the wake would lose the
sleep. Jumping to the alarm's exact cycle, because the phase between bus cycles and slow ticks is in
esp32sim's clock tree, which the runner cannot read; two ticks short of it, esp32sim's own rounds
wake the chip at its tick. Asking esp32sim to skip a deep sleep in its idle loop, the place for it
in the end, because the runner can do it with what esp32sim exposes, and an upstream change waits
until it is shown to be needed there.

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
`reboot()` at the pin, which leaves both words 0 and IO_MUX at function 0: a GPIO with its output
disabled on the pads whose function 0 is GPIO, a function through which esp32sim drives no pad on
the others; `on_the_emulator_no_pad_is_driven_after_a_chip_reset` checks it.

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

Status: decided (#32), busy time by fault rules (#107)

Without a fault rule from the peer, the card is never busy after a write, an erase or CMD12, and
sends a data block one byte after its R1; the image is a seek and a read or write per block.
Without class 10 in its CSD, ESP-IDF does not send CMD6 and stays at the default 20 MHz, which
`sdspi` asks for anyway. The tests of interest are the firmware's file handling, not the card's
timing; a test that is about timing gives the card a delay
([below](#sd-faults-are-the-peers-rules-applied-by-the-card-on-the-buss-clock)).

**Postponed:** a busy time of the card's own on every write (esp32sim's card has `prg_ns` for it),
CMD6 and high speed, CMD23, and locking and write protection: each when a firmware needs it.

## SD faults are the peer's rules, applied by the card on the bus's clock

Status: decided (#107)

The peer gives the card rules as inputs, `sd_fault` and `sd_clear`, and the card applies them
within each transaction, as it answers everything else; what a rule did comes back as `sd_op`,
which is not an event ([protocol 5.20](../protocol.md#520-sd_fault-sd_clear-and-sd_op-ext-107)).
A rule names an operation (a block read, a block written, an erase), an error or a delay, its
blocks and a count. A delay is on the bus's clock: the card learns the bus cycle from
`BoardModel::advance_to`, which the bus calls at every tick, holds a read's data block back with
0xff and stays busy with 0x00 after a write or an erase, and a firmware polling it sees time pass
between its transactions, so its own timeouts fire as on a slow card. No esp32sim change was
needed: `advance_to` exists, and `bus.cycles` moves only in the tick that calls it. A delayed
write or erase is in the image when it starts; the busy is time only. Of the rules an operation
matches, the one given last applies, so a one-shot rule can be put on top of a lasting one. Rules
stay through a power cut, a chip reset and a change of card (#106), since they are the test's and
not the card's state: a rule lasts until it is spent or cleared, and needs no message to say a card
went. A delayed read and a busy time end with the power and with the card. The runner does not hold
a rule's blocks against a card's size, since cards of other sizes may follow; the harness checks
them at the call.

**Rejected:** rules that go with the card, as a bad block does: the harness would have to learn of
that to keep `active` true to the runner, and a test that swaps a failing card clears the rule in
one line.

**Rejected:** a schedule of faults on the command line, because a test gives a fault after the
firmware has initialised the card, at an instant it learns only while the run goes; answering
the card's bytes from the peer, a turn away, for the reason the card is in the runner at all
([above](#the-sd-card-is-a-model-of-spi-mode-in-the-runner-not-esp32sims-card)); a cycle passed
to `spi_transfer_pins` upstream, because `advance_to` already gives it; host sleeps or the wall
clock for delays, because the run would no longer repeat; writing a delayed block only once the
busy is over, because a real card programs the block during the busy, and a firmware that gives
up first would then see a write that never landed; and the first rule given winning, because a
lasting delay would then hide every later one-shot rule.

**Postponed:** an erase that fails, and programming errors reported later in CMD13's status, which
ESP-IDF does not read after a write in SPI mode; faults of the initialisation and register
commands (CMD0 to ACMD41, CMD9, CMD10), which fail the mount, not the file handling; and a block
programmed in part, as a card that loses power would leave it: the card writes a block whole or
not at all.

## The SD card's fault messages keep protocol version 1

Status: decided (#107)

`sd_fault`, `sd_clear` and `sd_op` are new message types on version 1. Harness and runner come from
one checkout and nothing else speaks the protocol yet, so there is no older peer to keep working
or to tell apart. A stale runner build fails the first rule with `unknown type "sd_fault"`, and
`make setup` fixes it.

**Rejected:** version 2, because a version tells apart peers that ship apart, and none do; and a
flag to gate the messages, for the same reason.

## The SD card slot changes its card between transactions, and a new card starts unpowered

Status: decided (#106)

`--sd-spi` is a slot (`runner/src/sd_card.rs`, `Slot`), empty unless `--sd-rw` puts a card in it,
and the peer's `sd_card` empties and fills it at a slice boundary
([protocol 5.19](../protocol.md#519-sd_card-and-sd_card_ack-ext-106)). A transaction is answered
whole in `spi_transfer_pins`, so a change never falls inside one. Taking the card out drops it,
with what it had not finished, and closes its image; every block it accepted is in the image
already, since it writes through. A card put in is a new `SdCard`, in SD mode until CMD0, so the
firmware must initialise it as after a power-on. The board on the pins is the runner's, so no
esp32sim change is needed: there is no hook upstream lacks.

**Rejected:** writing the block on its way, or the rest of a multiple block write, when the card
comes out, because a firmware's handling of a pulled card is what the test is about; keeping the
removed card's protocol state for the next one, because a real card loses it with its power;
making `sd_card_ack` an event with a `seq`, because nothing answers it and, with a `latency` set,
it would cost a turn; and ending the run with a protocol error for an image the runner cannot
open, because that is not the peer breaking the protocol: the acknowledgement carries the error,
and the harness fails the run with it.

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

## The network behind the access point is the peer's, over the link

Status: decided (#31)

With `--wifi` the runner attaches esp32sim's virtual access point (`StationLink::attach_new` with
no NAT) and puts the station's link in Ethernet relay mode (`SocBus::set_ethernet_relay`): the
station's frames go to the peer as `eth_tx`, and the peer's `eth_rx` frames come back to the
station. The network is the harness's, on the test's clock, as behind the modem
([harness decisions](harness.md#the-network-the-device-reaches-is-simulated-in-the-harness-in-emulated-time)),
so the runner takes no `--net`. Both hooks are public in esp32sim at the pin, so no esp32sim change
is needed (plan M7 named `attach_wifi` and `StationLink`; the relay sits in the same type).

**Rejected:** esp32sim's own network with `--net nat`, for the reasons #64 rejected NAT for the
modem: wall time, runs that do not repeat, and the device's traffic in the host's network instead
of in front of the test. Also rejected: esp32sim's own network without NAT, which gives DHCP, DNS
and NTP but refuses every TCP connection, and would be a second network, with other addresses and
services, than the one behind the modem.

## Ethernet frames are stamped at the slice boundary they are taken at

Status: decided (#31)

esp32sim's relay hands over the station's frames without the time it sent them
(`StationLink::take_relay_frames`), so `eth_tx` carries the cycle of the slice boundary the runner
takes them at, as `uart_cfg` and the drop warnings do: at most one emulated millisecond late, or
`latency` while no event is unacknowledged. The answer is then on time at `t + latency` like any
other. The peer's frames go to the link's queue of 64 when they come due, and those it has no room
for wait in the runner for the next boundary, in order: none is lost to a full queue.

**Rejected:** asking upstream for timestamps on relayed frames, because a millisecond is below
anything the station's TCP/IP stack times, and nothing yet needs them closer.

## The access point that goes off the air deauthenticates the station first

Status: decided (#31)

`wifi_ap` with `on: false` makes the access point send a station that has authenticated or
associated a deauthentication, reason 3 ("leaving"), stop beaconing, and leave once the frame is
off its queue. At the pin esp32sim's station does not notice beacons that stop coming: with the
access point simply detached, ESP-IDF's `http_request` kept its connection state for 60 emulated
seconds, and only its TCP connects failed, with `errno=113`. An access point that shuts down
deauthenticates its stations, so the station learns of it as from a real one. `on: true` attaches
a fresh access point from the same `--wifi`, as at power-on, but only once the old one has left:
attached at once, it would take the deauthentication off its queue before the station had it.

**Postponed:** an access point that goes out of range without a word, which needs the station to
time out on missing beacons in esp32sim; it comes when a firmware's test needs it and the emulator
can do it.

## The Wi-Fi messages keep the link protocol at version 1

Status: decided (#31)

`eth_tx`, `eth_rx` and `wifi_ap` pass only in a run started with `--wifi`, which only a harness
that speaks them gives the runner, and `stats.wifi` is a field an older peer ignores
([protocol §2](../protocol.md#2-framing)). No message of version 1 changes its meaning, so the
version stays, as it did when `gpio_out` came with M3.

**Rejected:** version 2, which every peer and the recording header's tools would have had to follow
for messages none of them can receive.

## A power-on reset is reboot() with SRAM and RTC memory zeroed, in the same machine

Status: decided (#105)

esp32sim has no call for a power-on reset: `reboot()` resets the cores, the digital peripherals
and PSRAM and keeps SRAM, the RTC memories and the RTC registers, as a reset with power does. For
`chip_reset` and `power` on, the runner calls `reboot()`, then writes zeros over DRAM, IRAM, the
cache's RAM and both RTC memories through `SocBus::load_bytes`, which moves on the page versions
that esp32sim's decoded-block cache checks, loads the ROM's ELF again for the data its reset
handler expects in DRAM, and puts in a new chip's RTC registers, which say `POWERON_RESET` and
start the RTC timer at 0. The machine, its flash, its bus cycle counter and its board stay, so the
run's time goes on and the guest's flash writes are kept (protocol 5.17).

**Rejected:** building a new `Machine` from the `--flash-at` files, because the flash the guest
wrote would have to be carried over, the bus cycle counter set forward on a machine whose
peripherals start at 0, and every part of `setup` run again inside the session; writing the zeros
into `SocBus`'s vectors directly, because blocks decoded from the old contents would stay valid;
`request_reset(1)` alone, which reports `POWERON_RESET` and keeps every memory; and asking
esp32sim for a power-on reset first, because the runner can do it with what esp32sim exposes, and
an upstream change waits until it is shown to be needed there.

## With the supply off the run loop idles the guest and still takes the turns

Status: decided (#105)

While the peer has the board's supply off, the runner moves the bus cycle counter from slice to
slice without running the guest, and keeps taking the peer's turns and applying its inputs at
their times. Without the guest, a slice is not cut at the next emulated millisecond: it takes no
wall time, so an hour without power is a few slices. The chip reports no route and drives no pad,
whatever its registers hold; bytes and frames for it are dropped and counted.

**Rejected:** stopping the run loop until the supply comes back, because the peer's turns, and
with them the test's waits and the models' times, would stop with it.

## The flash reaches the harness through a file, not the socket

Status: decided (#131)

The runner writes the whole flash to the file of `--flash-out`, on the peer's `flash_dump` in a turn
and on its own at the end of a run, and the harness reads the file
([protocol 5.21](../protocol.md#521-flash_dump-and-flash_dump_ack-ext-131)). `--flash-at` and the SD
card's image already assume a path both sides can read. The file is written through a rename, and
before `stats`. The flash is read whole on every turn that asks, since a read of a 4 MiB file on a
held board costs no emulated time, and a test does it a few times.

**Rejected:** base64 chunks on the socket, because that is a second mechanism beside the file the
end of a run needs: after `stats` the socket carries nothing more, and a guest that kept writing
after the last turn would be out of reach. A partition's offset and size in `flash_dump`, because the
harness has the table and the whole flash is one more `read_bytes` away.

**Rejected:** writing the file only when a test asks, by a flag the test sets before power-on, because
a test finds out after a failure what it wanted to read, and the cost is one 4 MiB write per run.

## flash_dump keeps protocol version 1

Status: decided (#131)

`flash_dump` and `flash_dump_ack` add no version, as the SD card's messages did not: the runner sends
the answer only to a peer that asked, and the two sides come from one checkout. The message has no `t`
because it goes into no queue: the runner answers it where it reads it, in the turn.

## board.flash() exchanges on the link's thread, in the turn

Status: decided (#131)

`HeldClock` makes the exchange: `turn` returns `FlashDump` to the link, which sends it and reads the
answer as any other message, then calls `dumped` to hold the turn on. Only the link's thread touches
the socket, and `BoardLink.run`'s one loop of reading and answering stays one loop.

**Rejected:** the test's thread writing on the socket while the link is held, because the link would
have to give up its reader for the time of an answer, and a read of a `flash_dump_ack` would race the
link's next `readline`.

## The station's frames keep the 4 bytes esp32sim leaves, and nothing goes upstream yet

Status: postponed (#143)

Every frame the station sends reaches the harness with libpp's guard word after it ([protocol
5.15](../protocol.md#515-eth_tx-and-eth_rx-ext-m7)): libpp counts 4 bytes more than the frame in its
transmit descriptor's length, taken to be its FCS, and a CCMP MIC too, while esp32sim takes that
length for the frame and cuts only the MIC. esp32sim's cut is short by those 4 bytes, and esp32sim,
which reads the descriptor, is the place to correct it. Nothing in the harness needs that now:
`Ethernet.receive` leaves the 4 bytes out and takes a frame without them alike, and a full-size
datagram fits the relay's 1518 bytes with them. What was measured covers one ESP-IDF version, the
classic ESP32, and no capture from a real board. The question goes to mikroverk/esp32sim when
something needs the station's frames to be exact, with what was measured: a 24-byte null data frame
has a descriptor length of 28, and a protected data frame one 12 bytes over its frame. Protocol 5.15
says how to measure it again. The pin then moves.

**Rejected:** cutting 4 bytes off every `eth_tx` in the runner, because the runner would hold the
arithmetic of one ESP-IDF version's libpp, and a firmware whose libpp counts its length otherwise
would lose 4 bytes of every frame without a sign. It would gain nothing: the harness takes frames
with the 4 bytes and without them alike. And the runner would no longer hand over what esp32sim
gives, which hides the very behaviour that is to go upstream.

## The guest's HCI goes over the board socket, on the board's clock

Status: decided (#34)

With `--bt`, the runner puts a controller of its own, `hci::Forwarder`, in esp32sim's VHCI session
in place of esp32sim's virtual BLE controller, and forwards every H4 packet of the guest's host to
the peer as `hci_tx`, stamped with the bus cycle the host sent it at; the peer's `hci_rx` reaches
the host as any input does, at its `t`
([protocol 5.22](../protocol.md#522-hci_tx-and-hci_rx-ext-34)). The guest's controller is then the
harness's, on the board's clock, on the same air as the Bluetooth peers, which are Bumble devices on
that clock already
([decision](harness.md#the-bluetooth-peer-runs-bumble-in-the-harness-on-the-boards-clock)). The
hooks need nothing new of esp32sim: `Session::new` and the `HciController` trait are public at the
pin. A spike on 2026-11-08 ran ESP-IDF v5.3.6's `bt_spp_initiator`, unmodified, through inquiry,
SDP, legacy pairing and an SPP channel against `SppPeer` this way, with esp32sim's `Enable` hook
taking mode 2 (mikroverk/esp32sim#347), and two runs exchanged the same packets byte for byte.

**Rejected:** an HCI transport in esp32sim to an external controller (a `--hci tcp:HOST:PORT`),
because what would sit at its other end, Root Canal or a real controller, keeps its own time in
wall time, so a run would not repeat and a test could not hold the controller while it is not
waiting; the peer the controller talks to is Bumble on the board's clock already. Also rejected: a
second socket between the runner and the harness for the HCI, because it would need an ordering of
its own against the board socket's turns, where one socket has the order of its lines.

## The Bluetooth messages keep the link protocol at version 1

Status: decided (#34)

`hci_tx` and `hci_rx` pass only in a run started with `--bt`, which only a harness that speaks them
gives the runner, as the Wi-Fi messages pass only with `--wifi`
([decision](#the-wi-fi-messages-keep-the-link-protocol-at-version-1)). No message of version 1
changes its meaning, and there is no peer of another checkout to keep apart.

**Rejected:** version 2, for the same reason as with the Wi-Fi messages.
