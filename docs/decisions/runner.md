# Runner decisions

Why the `esp32tl-runner` crate in `runner/`, its esp32sim pin and the link protocol it speaks
with the harness ([`docs/protocol.md`](../protocol.md)) are the way they are.

## esp32sim is pinned at the merge commit of the virtual SoftAP station

Status: decided (15 November 2026)

`runner/Cargo.toml` takes all four esp32sim dependencies from
`357b0e542dfe97d4c62ec4a15c01c4969164448c`, the merge of mikroverk/esp32sim#362 on
2026-11-15. `v0.4.0` now exists: its annotated tag resolves to this same commit, not a later one.
The pin remains the merge SHA. It brings `VirtualSta`, `StationLink::attach_station`
(`esp-soc/src/wifi.rs:649-654`) and `SocBus::attach_wifi_station`
(`esp32/src/soc.rs:97-100`), for a virtual station joining the guest's own access point. Exposing
runner `--wifi-sta` and harness `wifi.station(...)` belongs to the next change.

The range `3bdb958..357b0e54` contains 31 non-merge commits, grouped here in full (merge commits
only join these branches):

- **NAT relay, mikroverk/esp32sim#350:** `c3ddf1f` adds the page's queue transport and
  `StationLink::set_nat` / `SocBus::attach_nat`; `41532e8` aborts replaced flows and hands their
  closes to the page; `cb3e52f` bounds old relay records at 256 KiB while retaining closes.
  `d57fccc` and `9fc28a7` update the evidence and experiment number. The runner attaches no NAT
  and calls neither new method. `attach_new(cfg, None, debug)` and relay-mode `net_step` keep
  their existing paths; the new queue transport is compiled but never instantiated here.
- **Nearby networks, mikroverk/esp32sim#352:** `917769d` exposes the page's nearby networks through
  `SocBus::wifi_nearby`; `b3593ff`, `0471468` and `4a4ed14` rerun/update its evidence.
  The classic wrapper calls `StationLink::set_beacons`, which the runner calls directly.
  Its filtering, replacement and return values are unchanged (`esp-soc/src/wifi.rs:691-701`).
- **Virtual station, mikroverk/esp32sim#362:** `2242462` adds the station, classic MAC receive
  flags, CLI option, tests and silicon receipt; `959eb93` counts relay frames lost after the
  station leaves and rejects multiple stations and `--net` with `--wifi-sta` in the upstream CLI.
  At this revision `StationLink::station_tx`, `next_rx`, `nothing_due` and `rx_meta` branch on
  the optional station and use their previous access-point paths when none is attached
  (`esp-soc/src/wifi.rs:705-724`, `esp-soc/src/wifi.rs:783-828`,
  `esp-soc/src/wifi.rs:767-770`, `esp-soc/src/wifi.rs:667-673`). The runner attaches none.
  `receive_relay_frame` also accepts a joined virtual station now; the existing AP path keeps
  its size and queue checks (`esp-soc/src/wifi.rs:866-873`). Its no-link error now says
  `Ethernet relay requires relay mode and a virtual AP or station`; the runner's configured AP
  does not take that error path. Capture was split into two helpers, preserving the old AP FCS
  removal; the runner attaches no capture tap. Reboot restarts an optional station and otherwise
  preserves the same AP, network and relay state.
  The classic MAC's `rx_ctrl` word 0 changes only when A1 matches the nonzero address slot 1,
  distinct from slot 0: then bit 29 alone marks the guest's SoftAP
  (`esp32/src/wifi.rs:230-245`). The runner's `--wifi` path sends frames to the guest station or
  broadcasts, not that SoftAP. Upstream's `docs/evidence/wifi-softap-rx-match` records silicon
  on an ESP32 revision 1.0 with ESP-IDF v5.5.4: all 3638 frames addressed to the SoftAP had bit
  29 alone. Station unicast's existing bits 28 and 29 remain, although silicon uses 28 alone;
  the receipt says libpp accepts both. These are upstream measurements, not new esp32tl runs.
- **S31 SHA and flash, mikroverk/esp32sim#359 and #360:** `a7869bf`, `f795351`, `8326939` add
  SHA DMA, its regression and shared AES/SHA tests; `67245f4` and `eb87662` implement/test USER+PE
  erase and no-WEL no-ops. The runner does not build the S31.
- **Mosaico and browser, mikroverk/esp32sim#363:** `9e43f53` adds S31 Wi-Fi validation;
  `155144a` adds board pull-downs and LEDC pads; `6cb4f5b` adds the motor, power switch and keys;
  `dba772b` makes BMM150 follow IMU motion; `b26ea2b` adds board firmware; `4c8d105` corrects
  output inversion and latched duty. `7ed8164` adds the compass demo; `7f691d8` adds browser S31
  support and IMU motion; `01f0d43` adds its web demo; `b4dad76` selects the flash size;
  `a157ff3`, `91d9158`, `4564e9a` and `4f94661` update chip/site documentation, attribution and
  browser flash sizing. Shared `BoardModel::pull_downs` defaults to zero and is consumed by the
  S31, not the classic adapter; the runner uses neither BMM150 nor IMU motion. None changes the
  runner's classic board callbacks.
- **Release prep, mikroverk/esp32sim#364:** `ff4450b` changes the workspace version to 0.4.0.
  It changes neither the edition (2021) nor a `rust-version` requirement. Rust 1.99.1 remains
  the runner's toolchain: the release build and all 165 Rust tests pass on it. Upstream evidence
  using 1.100.0 does not itself raise the minimum.

The classic `esp32/src/periph.rs` did not change between the pins: `ClassicGpio::new` still
uses `RegRam::new()` for IO_MUX (`esp32/src/periph.rs:235`), whose missing registers read zero.
There is no U0RXD reset fix in the range. Keep `reset_console_rx` and its tests; its comment and
runner documentation now name this pin. No runner implementation change is needed.

Current citations were checked against the pinned source, including the unchanged UART, GPIO,
Bluetooth, PSRAM and scheduler paths. Shifted citations cover `esp32/src/soc.rs`, classic Wi-Fi,
`StationLink` and the `SocBus::can_defer` default. The implementation plan also had stale references
to reset, UART routing, the board deadline, `Machine::seconds`, `Stop::Halted`, `StationLink`, the CLI's
Wi-Fi attachment and the SPI callback (in `bus.rs`, not `spi.rs`); these are corrected. Historical
pin decisions and stage-0 hyperlinks retain the line numbers of their explicitly named revisions.

The whole Python suite passes on the pin: `make test` reports 2320 passed and one skipped
(the physical GNSS check, with `ESP32TL_REAL_GNSS_PORT` unset). Both Rust test profiles pass
all 165 tests; `make lint`, including Clippy with warnings denied, and `make docs-check` pass.
The Wi-Fi, Bluetooth (including PSRAM), UART, recording and replay assertions are unchanged.

**Rejected:** a release tag as the dependency revision, so the pin keeps the convention of the
previous merges; a later commit, which is outside this audit; adding the SoftAP harness API here,
which would mix a dependency update with a new feature; removing the IO_MUX workaround, since
upstream has not fixed its cause.

## esp32sim is pinned at the merge commit of the classic VHCI PSRAM fix

Status: superseded (15 November 2026)

`runner/Cargo.toml` took `esp32`, `esp-soc`, `esp-periph` and, as a dev-dependency, `emu-core`
from git at `3bdb9582a910974e4e025d274d6ec61e5c481be6`, the merge of mikroverk/esp32sim#353
(2026-11-13), with which the classic ESP32's `valid_packet_range` takes a packet in PSRAM that is
mapped and readable as well as one in internal DRAM (`esp32/src/ble.rs:45-48`, with
`psram_readable` at `esp32/src/psram.rs:75-89`). On it `bt_spp_initiator_psram`, ESP-IDF's
`bt_spp_initiator` with Bluedroid's buffers in PSRAM, pairs with an `SppPeer`, opens SPP and
streams at about 299 kbit/s, 423 packets from the host and 432 to it;
`tests/test_bt_spp_initiator.py` keeps it so. On the previous pin the same test failed with
`NeverHappened`, every packet refused, and was a strict `xfail`
([VHCI packets in PSRAM](#vhci-packets-in-psram-are-esp32sims-to-take)).

Between `c99fb28` and `3bdb958` come 117 commits. Most are on chips or parts the runner does not
build: the ESP32-S31 (mikroverk/esp32sim#354, #358) and RISC-V JIT (#356, whose
`Bus::fetch_ahead` in `emu-core` has a default that reads nothing ahead, which the classic chip
keeps), the ROM's UART download mode on the S3, C3, C6 and P4 (#345, #348), the C3's MAC reset and
the C6's 802.15.4 (#340, #342), the Cheap Yellow Display board (#339), the browser's Bluetooth
pages (#343, #351), and Clippy's lints (#357). These are on classic paths that every firmware, or
a part of esp32tl, goes through:

- mikroverk/esp32sim#345 moves `ClassicUart`'s queue of host bytes into the shared `Uart`
  (`Uart::host_input_queued` and `Uart::advance`, `esp-periph/src/uart.rs:59-104`), and
  `uart_pin_input` hands a byte to `ClassicUart::wire_input` (`esp32/src/periph.rs:942-952`),
  which drops what does not fit, as the chip does, unless host bytes are queued. The runner
  hands each byte to `Uart::host_input` (`esp-periph/src/uart.rs:50-58`), as before: it drops
  what does not fit and raises `UART_RXFIFO_OVF_INT_RAW`; its new autobaud line does nothing on
  the classic chip, whose `UartLayout` sets `autobaud_en: 0`. `RXFIFO_FULL` now falls when
  `Uart::read` drains the FIFO below its threshold, where `ClassicUart`'s FIFO read did it before.
  `SocBus::next_deadline` also counts the next queued host byte, and the runner queues none.
- mikroverk/esp32sim#343: the virtual controller's lines go through `BleLog::note`
  (`esp-soc/src/ble/log.rs`), which prints them on stderr with `[ble]` as before unless
  `keep()` was called, which only the browser build does. The messages the documentation quotes,
  `[ble] rejected invalid VHCI packet pointer, length or controller state` among them, are the
  same.
- mikroverk/esp32sim#355: `StationLink` holds up to eight access points and a capture tap
  (`esp-soc/src/wifi.rs:530-559`). The runner attaches one and no tap; with one access point
  `station_tx`, `next_rx`, `set_beacons`, `attach` and `surviving_reboot` do what they did, and
  the relay is the same.

The evidence that none of them changes what esp32tl relies on is the whole test suite, which
passes on the pin, and every esp32sim line the documentation cites, which is still there, moved at
most; the ones whose code changed, the classic UART's delivery and `StationLink`, are described
anew ([runner](../features/runner-technical.md#received-bytes)). That revision in
`runner/Cargo.toml` is the project's esp32sim pin.

**Rejected:** a commit before #353, as there is none with the fix, and a later one, since #353's
merge was upstream's head when the pin moved. Also rejected: delivering a peer's bytes through
`ClassicUart::wire_input` now that it drops what does not fit, because the shared `Uart`'s
`host_input` already does what the runner needs and puts no byte behind esp32sim's queue; the
runner's code stays as it was.

The line numbers in this decision are those of `3bdb958`. The pin moved again for the
[virtual SoftAP station](#esp32sim-is-pinned-at-the-merge-commit-of-the-virtual-softap-station).

## esp32sim is pinned at the merge commit of the Bluetooth Classic enable

Status: superseded (#180)

`runner/Cargo.toml` took `esp32`, `esp-soc`, `esp-periph` and, as a dev-dependency, `emu-core`
from git at `c99fb286c4378fcfa6d304b754d31e542ba3035a`, the merge of mikroverk/esp32sim#347
(2026-11-08, upstream issue mikroverk/esp32sim#346), with which `esp_bt_controller_enable` takes
the mode of the config given to `esp_bt_controller_init`, as ESP-IDF does, and no other
(`esp-soc/src/ble/vhci.rs:199-216` and `:258-272`), and says on stderr that esp32sim's own
controller is LE-only when the mode is 2. On it, ESP-IDF's `bt_spp_initiator`, unmodified and
built for BR/EDR alone, enables its controller in mode 2 and, with the `bt` fixture's controller in
the harness, finds an `SppPeer`, pairs with its PIN and streams to it;
`tests/test_bt_spp_initiator.py` keeps it so. On the previous pin the `Enable` hook took mode 1
and 3 only, and the same image stopped at `enable controller failed: ESP_ERR_INVALID_ARG` and sent
no packet, as the spike of 2026-11-08 saw.

Between `afcfba6` and `c99fb28` come 14 commits, which bring three changes besides #347:

- mikroverk/esp32sim#262: the Seeed Studio XIAO ESP32-C6 board model and its browser demo, in
  `esp32c6/`, `web/`, `wasm/` and `examples/`, and a `hex` field in the web console's messages
  (`esp-soc/src/machine.rs:359-364`). Not on a path esp32tl uses: the runner builds neither the C6
  crate nor the web console.
- mikroverk/esp32sim#318, with `--hostfwd` (mikroverk/esp32sim#278) on it: the user-mode NAT's
  host side behind a transport trait (`esp-soc/src/nat.rs`, `nat/transport.rs`,
  `nat/std_transport.rs`), DNS answered by a transport that resolves names itself and SNTP answered
  locally on request (`esp-soc/src/net.rs`), and a chip reset that aborts the NAT's flows
  (`StationLink::surviving_reboot`, `esp-soc/src/wifi.rs:549-553`). The runner builds against it
  unchanged: `StationLink::attach_new(cfg, None, debug)` has the same signature, and the runner
  gives it no NAT, so `abort_all` has nothing to abort. All of it is `VirtualNet`'s, the network
  esp32sim answers for itself, which the runner never feeds: in relay mode `net_step` returns at
  once (`:641-642`) and the station's frames go to the peer.
  `tests/test_wifi.py`, `tests/test_http_request.py` and `test_wifi_http` of the determinism job
  pass.
- mikroverk/esp32sim#344 (`0437f14`): the classic ROM's UART download mode, for esptool. Its SLC
  (`ClassicSlc`) serves the ROM's download boot alone, which a run that boots from flash never
  enters. Its autobaud counters count the bytes `ClassicUart::host_input` takes while
  `UART_AUTOBAUD_EN` is set, and stand in for `RXD_CNT`, `LOWPULSE` and `HIGHPULSE` then
  (`esp32/src/periph.rs:593-599`, `:646-647`); ESP-IDF's application driver does not set it, and
  the bytes from the peer do not go through that method (below). Three changes are on every firmware's UART path: `RXFIFO_FULL`
  falls when a read leaves the FIFO below its threshold (`Uart::lower_rx_full_when_drained`,
  `esp-periph/src/uart.rs:55-61`, with `irq_dirty` from the bus's FIFO read,
  `esp32/src/bus.rs:252-265`), a byte or halfword store to a TX FIFO no longer reads the RX FIFO
  first (`:282-285`), and `TX_DONE` can be cleared and rises with the next byte written. ESP-IDF's
  `uart_wait_tx_done` clears `TX_DONE` and then returns at once, since the transmitter reads as idle
  (`components/esp_driver_uart/src/uart.c:1464-1477` in v5.3.6); the GNSS, modem and PPP tests
  over UART1 pass. The fourth change is the one the previous pin's decision rejected `0437f14`
  for: `uart_pin_input` hands a byte to `ClassicUart::host_input` (`esp32/src/periph.rs:960-970`),
  which queues what does not fit the 128-byte FIFO and releases it at the baud rate. The runner now
  delivers each byte to each port itself, through the shared `Uart` that `ClassicUart` wraps, its
  public field `.0`: `Uart::host_input` (`esp-periph/src/uart.rs:43-50`), which the previous pin's
  `uart_pin_input` called, drops a byte that finds the FIFO full and raises
  `UART_RXFIFO_OVF_INT_RAW`, and has neither the queue nor the autobaud counting
  ([runner](../features/runner-technical.md#received-bytes)). A byte that finds the FIFO full is
  lost, counted and warned (`rxfifo_ovf`, [protocol 5.8](../protocol.md#58-warn-ext)), as on the
  chip, and ESP-IDF's driver gets the interrupt it turns into `UART_FIFO_OVF`, as on the previous
  pin.
  `on_the_emulator_a_byte_into_a_full_rx_fifo_is_lost_and_raises_the_overflow_interrupt` in
  `runner/src/session/tests.rs` keeps it so, on the real `esp32::Machine`: on the runner's code of
  the previous pin, which handed `uart_pin_input` every byte, and with `ClassicUart::host_input`, it
  fails on the two bytes in esp32sim's queue.

The runner's steps through a deep sleep (#173, merged after this decision was written) drive
esp32sim code that is the same at both pins: `esp32/src/sleep.rs`, `esp32/src/soc.rs`, `emu-core`,
and the `RtcSlow` divider of 1600; `Peripherals::tick` only adds the pacing of a UART's queue,
which stays empty. `examples/periodic_wake` measures the same on both pins: pulses 900.266 s
apart, a day ending 85 525 s into the run, 192 turns.

The evidence that none of them changes what else esp32tl relies on is the whole test suite, which
passes on the pin, and every esp32sim line the documentation cites, which is still there, moved at
most; the two whose code changed, `surviving_reboot` and the `Enable` hook, are described anew.
That revision in `runner/Cargo.toml` is the project's esp32sim pin.

**Rejected:** upstream's head at the time, `88895c9`, because the project needs nothing more of it
than #347, and the merge commit of the change the project needs is the smallest step that was
checked, as with the pins before. Also rejected: a pin without #344, since every revision with #347
has it: #347's branch was built on `0437f14`. Also rejected: asking esp32sim for a way to drop a
byte instead of queueing it, since one is public at the pin: the `Uart` inside `ClassicUart`. Also
rejected: `ClassicUart::host_input` for a port with room, and `UART_RXFIFO_OVF_INT_RAW` set by the
runner for a full one, because it leans on the runner never handing over a byte that does not fit,
copies esp32sim's bit, and counts the peer's bytes as autobaud edges, which the previous pin did
not.

The line numbers above are those of `c99fb28`. The pin moved for Bluedroid's buffers in PSRAM: see
[esp32sim is pinned at the merge commit of the classic VHCI PSRAM fix](#esp32sim-is-pinned-at-the-merge-commit-of-the-classic-vhci-psram-fix).

## esp32sim is pinned at the merge commit of the deep-sleep timer wake

Status: superseded (#34)

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
The pin moved for Bluetooth Classic, and the runner keeps a full FIFO's drops itself: see
[esp32sim is pinned at the merge commit of the Bluetooth Classic enable](#esp32sim-is-pinned-at-the-merge-commit-of-the-bluetooth-classic-enable).

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
compile the P4 and S31 models too, which the runner does not run, and its argument parser exits on
flags it does not know, so the runner could not reuse it for `--board socket:` either. The runner
depends on the S3, C3 and C6 crates itself since
[one esp32sim chip per process](#the-runner-runs-one-esp32sim-chip-per-process-chosen-by---chip).

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
last round at the ceiling (`esp-soc/src/machine.rs:696` at `8c65215`). `SocketBoard`'s callbacks
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
answer as any other message, then calls `dumped` to hold the turn on (`answered` since #278,
which answers `flash_write` the same way). Only the link's thread touches
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

## VHCI packets in PSRAM are esp32sim's to take

Status: decided (#188)

A Bluedroid built with `CONFIG_BT_ALLOCATION_FROM_SPIRAM_FIRST` sends its HCI packets from PSRAM,
and at the pin `c99fb28` esp32sim's `Send` hook refuses any packet outside internal DRAM on the
classic ESP32, before the runner's `Forwarder` sees it; the S3's `VhciBus` already takes mapped
PSRAM. The fix goes upstream: the classic ESP32's `valid_packet_range` taking a range whose pages
map to installed PSRAM and that the cache can read, as a load of it would. Built locally, it lets
`bt_spp_initiator_psram` pair, open SPP and stream. Until esp32sim merged it the pin stayed, and the
image's test was a strict `xfail`
([validation firmware](../features/validation-firmware-technical.md#bt_spp_initiator_psram)).
esp32sim merged it as mikroverk/esp32sim#353, and the pin moved there: see
[esp32sim is pinned at the merge commit of the classic VHCI PSRAM fix](#esp32sim-is-pinned-at-the-merge-commit-of-the-classic-vhci-psram-fix).

**Rejected:** handling it in the runner, which would mean replacing esp32sim's `Send` hook with a
copy of its own; pinning a fork of esp32sim, which would hold the runner off upstream's fixes; and
building the firmware without the option, which would test another image than the one a user
ships.

## The runner restores U0RXD's input-enabled reset state

Status: decided (#195)

The pinned emulator initializes the classic IO_MUX to zero. A booted `hello_world` and ESP-IDF
REPL therefore have no RX route on GPIO 3: default REPL configuration leaves the pins unchanged.
The ESP32 TRM table 6.10-1 gives GPIO 3 reset configuration 3 (input and pull-up enabled).
The runner restores this pad at initial boot and after each reset. This allows the existing
`uart_rx` protocol to reach UART0 without changing firmware. Extending this workaround to every
pad is outside this console change; it should ultimately live in esp32sim's reset model.

**Rejected:** forcing UART0's route to GPIO 3 whenever none is configured, because that ignores
firmware intentionally disabling input, or rerouting it; making only the validation firmware
configure pins would conceal the missing post-boot route.

## Virtual station mode shares the Ethernet relay but has no access-point switch

Status: decided (#215)

`cli::Wifi` is an enum, with `AccessPoint` and `Station` variants. `--wifi-sta` is parsed by
esp32sim's `StaConfig::parse`, is exclusive with `--wifi` and `--wifi-nearby`, and requires
`--board`. The same session path carries Ethernet and counts `stats.wifi`; `wifi_ap` in station
mode is a protocol error. Power-on reset attaches a fresh virtual station; guest resets use
esp32sim's station restart. Pending identical DHCP client frames are coalesced only in station
mode, while backpressure holds them, so retries before association do not accumulate. Other
Ethernet frames and already-delivered requests keep their ordering and counts.

**Rejected:** a boolean on an access-point configuration whose `spec` would then mean two things;
and a host network stack in the runner. The harness owns DHCP, ARP, TCP and HTTP.

`StationLink::relay_lost` stays in esp32sim's `[emu] wifi-sta:` end-of-run line, alongside its
join diagnostics. It is not added to `stats.wifi`: those five fields measure the socket relay
and receive ring, while this counter is a station-specific loss after the relay accepted a
frame. It must not be folded into `eth_rx_dropped` or `eth_tx_dropped`. The runner emits both upstream Wi-Fi diagnostic lines at teardown through the existing
captured-output path. Previously it did not call `StationLink::report()` at all. No protocol version change or compatibility shim is needed.

## Station transitions invalidate relay queues

Status: decided

Protocol v2 carries `wifi_link` before Ethernet at the same collection boundary. The runner observes public station state and join count and flushes both relay directions using `set_relay`, without advancing guest time. Future scheduled Ethernet input is discarded as well. This avoids reusing a queued request after reconnect; reducing DHCP backoff would not do so. Existing pin APIs suffice; no upstream patch is required.

## The runner runs one esp32sim chip per process, chosen by --chip

Status: decided (#236)

`--chip` takes `esp32`, `esp32s3`, `esp32c3` and `esp32c6`, the chips esp32sim models well enough
for the run loop: a board on the chip's pins with UART and GPIO hooks, an Ethernet relay behind
its access point, and, on all but the C6 at the pinned ESP-IDF, VHCI. `main` monomorphises the
setup and the run over the chip's machine type (`go::<esp32s3::Machine>`), each implementing
`chip::Target` for the setup and `session::Guest` for the run loop, in a module of its own under
`runner/src/chip/`. What the protocol needs of the chip, its name, bus clock and GPIOs, is a
`chip::Profile` selected once per process (`chip::select`, a `OnceLock`): `protocol::ns` and
`protocol::cycles` read the selected clock, and the classic chip's is the default, which the unit
tests run on.

**Rejected:** a trait object for the machine, because `esp_soc::Machine<S>` is generic over the
chip and the run loop is already generic over `Guest`, so a vtable would buy nothing; a binary per
chip, because each would link esp32sim again with fat LTO and the runtime release would carry four
executables; passing the clock to every time conversion, because the protocol module converts in
about thirty places and one process never runs two chips; depending on esp32sim's CLI crate for
its chip dispatch, for the reasons of
[the esp32sim command-line crate is not a dependency](#the-esp32sim-command-line-crate-is-not-a-dependency).

Since #233 it takes `esp32p4` and `esp32s31` too, whose boards have GPIO hooks only
([the P4 runs on the model of its images' revision](#the-p4-runs-on-the-esp32sim-model-of-the-revision-its-images-are-built-for),
[UART0's console has no pins on the P4 and the S31](#on-the-p4-and-the-s31-uart0s-console-has-no-pins)).

## What esp32sim lacks on a chip is refused at the command line

Status: decided (#236)

`chip::Profile` says what esp32sim models on the chip, and `cli::parse` refuses a flag that asks
for something it does not: `--wifi-sta` on the S3, C3 and C6
(`--wifi-sta: esp32s3 has no virtual station for a SoftAP in esp32sim`), a `--psram-mb` size the
chip does not take (`--psram-mb: the ESP32-C3 supports only 0 MiB, got "2"`), and the pins of
`--sd-spi` against the chip's GPIOs; since #233, on the P4 and the S31, `--wifi`, `--wifi-sta`,
`--bt` and `--sd-spi` (`--sd-spi: esp32p4 tells the board no SPI pins in esp32sim`), whose buses
have no Ethernet relay, no VHCI hooks and no SPI pin hook (`Profile::wifi`, `bt`, `sd_spi`).
Deep sleep and GP-SPI RX DMA cannot be refused: nothing on
the command line asks for them, and the guest decides at run time. `--sd-spi` is taken on the S3,
C3 and C6: the card answers the transactions the CPU drives, and only those through RX DMA miss
it, which ESP-IDF's SD SPI driver needs; the esp32sim backend skips `sd_card` there instead. [#229](https://github.com/mateuszsikora/esp32-testing-library/issues/229)
lists each gap; when esp32sim fills one and the pin moves past it, the `Profile` flag changes.

**Rejected:** letting esp32sim's own `Err` reach the run, because `attach_wifi_station`'s
default error would come at setup with exit 2 all the same but without naming the flag, and a
PSRAM size esp32sim takes but the chip's modules do not would boot a board that cannot exist.

## The runner restores U0RXD's reset state on every chip

Status: decided (#236)

esp32sim creates the IO_MUX of the S3, the C3 and the C6 as zeroed register RAM, as it does the
classic chip's, so U0RXD has no input function after reset and the console's RX route is missing
(`unrouted_rx: uart_rx on GPIO 44, which no UART receives on` on the S3). Each chip module's
`reset_console_rx` writes the pad's reset value, function 0, `FUN_IE`, `FUN_WPU`, drive 2, to
its IO_MUX register at power-on and after every reset, as
[the runner restores U0RXD's input-enabled reset state](#the-runner-restores-u0rxds-input-enabled-reset-state)
does for GPIO 3: GPIO 44 on the S3 (`0x6000_90B4`), 20 on the C3 (`0x6000_9054`), 17 on the C6
(`0x6009_0048`). The fix belongs in esp32sim's reset model for every pad
([#229](https://github.com/mateuszsikora/esp32-testing-library/issues/229), G3).

**Rejected:** as for the classic chip, forcing UART0's route when none is configured.

## The console port is a flag of its own, `--console-port uart0|usb`

Status: decided (#248)

`--console stdout|socket|both` says where the console goes; `--console-port uart0|usb` says which
of the guest's outputs it is: UART0, the default, or the chip's USB-Serial/JTAG controller. It sets
esp32sim's `Console::mask` to bit 1 (UART0) or bit 0 (USB-Serial/JTAG) for stdout
(`esp-soc/src/machine.rs:33`), and, with `--console socket` or `both`, the source of the `console`
messages. `chip::Profile::usb_serial_jtag` is false on the classic ESP32 alone, and `cli::parse`
refuses the flag there, `--console-port usb: esp32 has no USB-Serial/JTAG controller`, exit 2, as
[what esp32sim lacks on a chip](#what-esp32sim-lacks-on-a-chip-is-refused-at-the-command-line) is.
The value names are esp32sim's own (`Console::parse_mask`, `esp-soc/src/console.rs:6-14`).

**Rejected:** new values of `--console` (`usb-socket`, `usb-both`), which would fold two choices
into one flag and double its values; and both outputs at once on the socket, which would mix two
streams that the guest writes independently into one `console` message, cut anywhere.

## The USB-Serial/JTAG console adds `console_rx` at protocol version 2

Status: decided (#248)

The controller has no pins, so the console's input cannot be `uart_rx` on a watched pin:
`console_rx` (`t`, `data`, [protocol 5.23](../protocol.md#523-console_rx-ext-usb-console)) goes into
the input queue and, when it comes due, into esp32sim's `serial_input` whole. USB has no character
time the guest can see, and esp32sim already hands the guest one 64-byte packet at a time, so the
runner paces nothing. Bytes that come due while the board's supply is off are dropped and counted
in `stats.console_rx_lost`, a field that is there only with `--console-port usb`. The output stays
`console`. The version stays 2: the runner reads the message only with `--console-port usb`, which
only a harness that sends it gives it, and an added field is ignored by a peer that does not know
it (protocol section 2). The project has no users yet, so no shim is kept either way.

**Rejected:** `uart_rx` on a pseudo pin (a GPIO number the chip does not have, or 255), which the
runner would have to special-case in `watch`, in its pin checks and in pacing, and which would
carry a `baud` that means nothing; and pacing `console_rx` at a made-up line rate.

## The USB-Serial/JTAG console's bytes are stamped with the end of their slice

Status: decided (#248)

esp32sim moves what the controller flushed into `Console::usb` inside `run`
(`Machine::drain_console`, `esp-soc/src/machine.rs:339-371`), with no cycle, and it has no pins for
the socket board to record. The runner takes the backlog at every slice boundary
(`Guest::take_usb_console`) and stamps it with the boundary's cycle: a byte's `t` is at most one
slice, 1 ms while the board is on, after the guest wrote it. Measured on `console_repl_usb`: the
help text the guest printed in one slice reaches the harness as one `console` message per line at
one `t`. esp32sim trims the backlog to its last 48 KiB once it passes 64 KiB, so more than 64 KiB
written in one slice would lose its start; no validation firmware comes near it.

**Rejected:** a hook in esp32sim for each flushed packet, with its cycle, which would need an
upstream change that no test needs yet; and draining `UsbSerialJtag::tx_out` from the runner,
which `run` empties first.

## The runner reads the esp_timer counter's registers at slice boundaries

Status: decided (#245)

Where a boot's `esp_timer` counts zero is what a test needs to put a stamp the firmware took with
`esp_timer_get_time()` on the board's clock, and `reset`'s `t` is not it: between the two lie the
ROM, the bootloader and the application's start-up. The runner reads the counter's registers at
every slice boundary and sends `uptime` with the earliest and the latest the zero can have been
([protocol 5.24](../protocol.md#524-uptime-ext-245)). It reads no guest memory, and nothing it reads
changes what the guest sees. On the ESP32-S3, -C3 and -C6 it reads SYSTIMER unit 0's count, a
public field of esp32sim's `Systimer`, and knows the zero to within a tick. On the classic ESP32
esp32sim keeps the LACT's count private (`ClassicTimer::lact`, `esp32/src/timers.rs`): the runner
sees that it counts and from what load, so the zero is known to within the slice in which it
started, at most a millisecond. A load while it counts is placed a boundary later, within two: the
boundary that sees the new LACTLOADLO and LACTLOADHI may come before the guest's LACTLOAD, a few
instructions later in the same critical section (`esp_timer_impl_set`).

**Rejected:** an esp32sim `Observer` with `Wants::MMIO`, to see the write that starts the LACT at its
instruction: any observer turns off esp32sim's round batching and virtual quanta for the whole run
(`esp-soc/src/machine.rs`, `run_unmodeled`), and observers cannot be removed. Writing LACTUPDATE
from the runner to latch the count: the latch is the guest's, and an `esp_timer_get_time()` in the
middle of its own update would read the runner's latch. Shorter slices until the counter starts:
tens of thousands of boundaries per boot, for a bound that a millisecond already meets. Estimating
the zero from what the firmware sends, the earliest arrival less the uptime it stamps: it is late
by the stamp's delay, which is what this was to remove.

A counter that stops leaves the zero sent (#307): a stop does not move it, and every stamp the
firmware took while the counter counted is still dated by it. On the classic ESP32 esp32sim's
`enter_deep_sleep` replaces both timer groups, so the boot that sleeps sees its LACT stop before the
wake's `reset`; with `null` there, the boot that slept lost its zero. `null` stays for a load that
counts from before power-on, whose zero is not known. **Rejected:** keeping `null` for a stop and
having the harness ignore it for a boot that has a zero: it puts the meaning of a stop in the
harness, which cannot tell a stop's `null` from a before-power-on load's.

**Postponed:** the classic chip's zero to within a tick, which needs esp32sim to expose the LACT's
count, a read-only accessor on `ClassicTimer`. Asked of upstream only once a test needs better than
a millisecond.
## The P4 runs on the esp32sim model of the revision its images are built for

Status: decided (#233)

esp32sim models ESP32-P4 rev < 3 and rev ≥ 3 as two machines, `Machine<Esp32p4<RevLt3>>` and
`Machine<Esp32p4<RevGe3>>`, which differ in ISA, interrupt controller, memory layout and ROM; an
image runs only on the revision it was built for. `--chip esp32p4` takes the revision from the
images' headers, as esp32sim's command line does (`p4_rev3`, `cli/src/lib.rs:661-669`), by a rule
of its own: before the setup `main` reads the header at the flash's bootloader address, 0x2000,
from the last `--flash-at` file that covers it, a bootloader file of its own or an image of the
whole flash from 0x0 (PlatformIO's `firmware.factory.bin`), and the first 24 bytes of every other
file, and `chip::esp32p4::rev3` answers rev ≥ 3 when the first of them that is a P4 image's
(magic 0xE9, chip ID 0x0012), the bootloader's first, asks for revision 3.00 or later
(`min_chip_rev_full`). esp32sim takes the first file's answer even when it holds no P4 image; the
runner passes over such a file. `main` then monomorphises over the machine of that revision and says which
on stderr (`esp32tl-runner: ESP32-P4 rev ≥ 3 (v3.1), as the images are built`). Each `--flash-at`
image goes through esp32sim's `Soc::check_image`, which refuses an image for the other revision
with esp32sim's message, exit 2. The ROM is the harness's choice, made the same way
([harness: the P4's ROM is the one of its build's revision](harness.md#the-p4s-rom-is-the-one-of-its-builds-revision)).

**Rejected:** two chip names (`esp32p4` and an `esp32p4rev3`), because ESP-IDF's target and the
chip name `hello` carries are `esp32p4` for both, and a board profile would have to say what the
build already says; a flag for the revision, which could disagree with the images; reading the
header in `Target::new`, which the images have not reached yet.

## On the P4 and the S31, UART0's console has no pins

Status: decided (#233)

esp32sim's P4 and S31 buses never call the board's UART hooks (`uart_tx`, `uart_rx`,
`uses_uart_pins`), and neither has a `uart_route`
([#229](https://github.com/mateuszsikora/esp32-testing-library/issues/229), G6 and G7).
`chip::Profile::uart_pins` is false for them, and the run loop takes the console as it takes the
USB-Serial/JTAG one ([its bytes are stamped with the end of their slice](#the-usb-serialjtag-consoles-bytes-are-stamped-with-the-end-of-their-slice)):
`Guest::take_uart0_console` takes what esp32sim drained from UART0's TX FIFO into
`Console::uart0`, at every slice boundary, and the peer's `console_rx`
([protocol 5.23](../protocol.md#523-console_rx-ext-usb-console)) goes into UART0 through
`Guest::uart0_console_input`. On the P4 that is esp32sim's `SocBus::uart_input`, which puts into
the 128-byte RX FIFO what fits and releases the rest at UART0's baud rate. The S31's takes host
bytes into the FIFO at once and drops what does not fit, so its machine is wrapped
(`chip::esp32s31::S31`), which keeps the bytes and runs the guest in pieces that end where the next
one is due: one byte per character time at UART0's baud (115200 until the guest sets one), when the
FIFO has room and UART0 takes it (`S31Bus::uart_input` refuses a byte while UART0's clock or
receiver is off); a refused byte stays queued for the next character time. Every UART route reads as having no pins (`chip::no_uart`), so a route to a model
cannot run; the esp32sim backend skips the test instead.

**Rejected:** decoding UART0's route from the P4's IO_MUX and GPIO matrix registers, the first
option of #233: it would give the console's pins, but the bytes the UART sends still never reach
the board, so the output would need the console stream all the same, and the input would gain only
a pacing that the queue already gives; every other route would still have no bytes.

## The P4's and the S31's GPIO output is reported where esp32sim does not report it

Status: decided (#233)

The P4's bus tells the board a change of a pin's driven level, `out & enable`, at its cycle
(`gpio_output_at`), but not an output enabled at the level the pad already had, so the peer would
never learn that the firmware drives a line low. After every `run`, the P4's `Guest::run_until`
hands the board the pads' enable and output words with no change: the socket board reports them only
if they differ from what it last reported, at the slice's end. esp32sim's P4 also leaves
`SocBus::gpio_release_input` to the trait's default, which does nothing, so the runner calls its
GPIO's own `release_input`.

The S31's bus calls the board's `gpio_changes` with the pads' levels and no cycle. Its wrapper turns
on esp32sim's record of pad changes (`observe_gpio`), which has the cycle of each, and after every
run hands the board each change of a pin the chip drove before the run or drives after it, at its
cycle, with the pads' output enable as it is at the end of the run (`Pads::drive`); a pin that
changes direction inside one slice is reported with the later direction. A change the peer's own
input caused is dropped (`take_gpio_inputs`).

**Rejected:** polling the pads at every slice end alone, which would lose a pulse shorter than a
slice and put every edge at the slice's end; leaving the enable-only change unreported, which
makes `lines.level` of a line the firmware drives low unknown until it first goes high.

## A power-on reset of the P4 clears what esp32sim's chip reset keeps

Status: decided (#233)

esp32sim's P4 chip reset keeps L2MEM, the TCM, the LP RAM and the LP registers (`LpAon`, the
reset cause among them). The runner's power-on reset reboots the chip, then zeroes L2MEM, the TCM
and the LP RAM through the bus, loads the ROM again and puts a new `LpAon` in place, whose cause is
POWERON. The S31's chip reset already makes its whole digital side again, SRAM and the LP memory
with it, and keeps the ROM, the efuses, the board inputs and the flash; its power-on reset is that
reset with the cause set to POWERON (`SocBus::set_reset_cause`).


## The P4 reports its uptime as the other SYSTIMER chips do, the S31 none yet

Status: decided (#233)

The P4's `esp_timer` counts on SYSTIMER unit 0, as on the S3, C3 and C6, and esp32sim's
`SystimerP4` wraps the shared `Systimer`, ticking it 2 for every 5 XTAL ticks: 22.5 bus cycles a
tick at 360 MHz. `chip::systimer_uptime` takes a whole number of bus cycles a tick, so the P4 has
its own, in half cycles, and its zero is known to within 23 bus cycles. The S31's SYSTIMER counts
at a rate set by its clock source and a divider that `SystimerS31` keeps private, so its `Guest`
keeps the trait's `uptime`, `Stopped`, and the run sends no `uptime`
([protocol 5.24](../protocol.md#524-uptime-ext-245)).

**Rejected:** a 23-cycle tick for the P4 in the shared function, which would put the zero about
2% of the count away from where it is; assuming the S31's default 16 MHz, which a firmware that
moves the source or the divider would make wrong without a word.

**Not yet:** the S31's uptime, which needs the divider from esp32sim.

## The runner puts the reset-reason hint back where esp32sim forgets it

Status: decided (#261)

On the ESP32-C3 and the ESP32-S31, esp32sim 357b0e5's chip reset makes RTC_CNTL or the S31's bus
anew, and with it zeroes `RTC_RESET_CAUSE_REG`, which the chip keeps through every reset but a
power-on. The runner's `chip_reset` reads the register before `reboot()` and writes it back after,
so that a firmware's `esp_reset_reason()` reports a panic as `ESP_RST_PANIC` there as on the chip,
and `Boot.idf_reason`, computed from the same value, agrees with it. Only this register is put
back: the rest of RTC_CNTL's and LP_SYS's store registers stay as esp32sim leaves them.

**Rejected:** sending the hint without putting it back, because the firmware under test would then
report `ESP_RST_SW` after a panic while `Boot.idf_reason` says `ESP_RST_PANIC`; and putting back
every store register, because the ROM and the bootloader read others of them (the XTAL
frequency, a wake stub and its CRC), and what they would do with them on esp32sim's C3 and S31 is
untested here.

**Not yet:** esp32sim keeping RTC_CNTL's registers through the C3's reset, and the S31's LP_STORE
registers, as it does on the other chips; the runner's write goes once it does.

## The peer writes the flash in its turn, with the supply off

Status: decided (#278)

`flash_write` writes a file into the runner's flash where the runner reads it, in the turn, as
`flash_dump` reads it, and only with the board's supply off: off since an earlier turn, or switched
off by a `power` due by the bus's current cycle, which the runner applies before the guest runs
again ([protocol 5.25](../protocol.md#525-flash_write-and-flash_write_ack-ext-278)). A
`board.nvs(..., merge=True)` reads the partition with `flash_dump`, merges, and writes it back in
the same held turn, and a `board.flash()` right after it sees the new bytes, all without moving
the board's time. The bytes come through a file, as for `flash_dump` and `--flash-at`.

**Rejected:** a `flash_write` with a `t`, queued as an input and applied when the run reaches it,
because its answer would come only once the test waits: `board.nvs` could not say in the call that
the write failed, and a `board.flash()` in the same turn would read the flash before it.

**Rejected:** a write with the supply on, because the guest could read the bytes half written, or
write over them with what it had cached, and NVS on a running chip has no way to take a change from
outside; the runner refuses it as a protocol error rather than leave the check to the harness.

The harness's side, the merge and the refusal while the board is on, is
[board.nvs merges by values, before power-on and between boots](harness.md#boardnvs-merges-by-values-before-power-on-and-between-boots).

## The classic RTC watchdog probe leaves reset modelling to esp32sim

Status: decided (#317)

The [hardware measurement](../stage0-findings.md#rtc-watchdog-during-classic-esp32-deep-sleep)
found two different effects. The system-reset stage action produced cause 9 only in esp32sim.
Patching a register after that reset would still expose a boot the board never took. The
RTC-reset action produced cause 16 on both targets, with both sleep bits clear on the board
and set in esp32sim. RTC-domain reset semantics belong in the chip model, where firmware
and the runner can observe the same state. Keep the runner's existing check of both cores
while waiting for a model fix.

## The Bluetooth hooks follow the app the bootloader started, by its ELF's SHA-256

Status: decided (#347)

esp32sim's VHCI hooks are at the addresses of one ELF. An app the bootloader starts from another
build, one an OTA update wrote into the other app partition, has its functions elsewhere, and the
hooks of the build's ELF patch the middle of other functions: its controller task fails, `[ble]
controller task creation failed: 0x3ffea240`, or it panics. The same image in another slot was
never the cause: the hooks are virtual addresses, and the trampoline goes where the MMU the
bootloader set maps them. `--bt` is repeatable, the application's ELF first, and at every slice
boundary the runner reads `app_elf_sha256` from the `esp_app_desc_t` mapped at the start of the
app's DROM and puts in the hooks of the ELF with that SHA-256, or none
([runner](../features/runner-technical.md#bluetooth-the-guests-hci-on-the-socket)). The harness
names the other ELFs with `board.bluetooth.add_elf`.

**Rejected:** working out at each reset which partition the bootloader will boot, from the
partition table and `otadata`, as ESP-IDF's bootloader does. That repeats the bootloader's choice:
the rollback states, `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE`, the fallback to another slot on an
image that fails verification, the factory and test apps, and the table's offset. The descriptor
the bootloader mapped is its choice already made.

**Rejected:** installing the hooks of every ELF at once, and taking at each hook the ones of the
running app. esp32sim keeps a hook's kind in a private map, and an address that is a hook in one
ELF is an instruction in the middle of a function in another, which the hook would take; it needs
a change to esp32sim for no gain over switching the whole set.

**Rejected:** refusing a run whose application's ELF is not byte for byte the file its image was
built from, or dropping its hooks then. A single `--bt` kept them for an ELF built again or
processed after `elf2image` with the same layout, and tests that passed so would fail: the first
app the bootloader starts is the application's, and its SHA-256 becomes the application ELF's
beside the ELF's own, with a line on stderr. A first image the bootloader only checks, of another
build, then takes the application's hooks for the run; the application's own image still gets them. Checking the image in the flash before the run instead needs the runner to find
the application's partition, which a merged flash file hides.

**Rejected:** matching an ELF to the image by the other fields of `esp_app_desc_t`, its version,
project name, time and date. Two builds can share all of them and still differ in layout; the
SHA-256 of the ELF is what ESP-IDF records to say which ELF the image is.

**Postponed:** `--no-bt`, which stubs `esp_bt_controller_init` of the application's ELF alone: an
app of another build that starts its controller in a test that never looks at `board.bluetooth`
runs ESP-IDF's controller. `add_elf` gives the run `--bt` anyway. And a firmware that reads its own
image after it started its controller finds esp32sim's trampoline in it until the next reset; that
needs esp32sim to keep the trampoline out of the guest's flash reads. On the ESP32-C6, hooks that
change while the trampoline is in the flash leave it there for good: its `Ble` forgets
`original_flash` at a new `enable` and keeps it `pub(crate)`, so the runner cannot put the code
back as it does on the other chips; that needs esp32sim to expose the C6's `restore_flash`. Nothing
makes the hooks change while an app runs, so no run reaches it.
