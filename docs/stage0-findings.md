# Stage 0: unmodified ESP-IDF images on the classic ESP32 in esp32sim

The question stage 0 answered: can esp32sim run an unmodified release image of a dual-core ESP-IDF
application with PSRAM, fast enough to test against, and where does it stop? This is the evidence
behind the dependency table in the specification
([#1](https://github.com/mateuszsikora/esp32-testing-library/issues/1)). It is a record of what was
measured, so it is amended by measuring again, not by argument.

Measured on 2026-10-29, locally, on an Apple silicon Mac (arm64, macOS), which other work was
sharing.

## Inputs

| What | Version |
|---|---|
| esp32sim, upstream | `main` at [`2e36224d34decc2fc9d2bba5fa4c5fdf2171b0a6`](https://github.com/mikroverk/esp32sim/tree/2e36224d34decc2fc9d2bba5fa4c5fdf2171b0a6) (2026-10-27). Release v0.3.0 refuses `--psram-mb` on the classic ESP32 ("--psram-mb is not available on the classic ESP32") |
| esp32sim, with the revision fix | [`165f56a9cf0f211669ce282bdce9426f821b1ec4`](https://github.com/mateuszsikora/esp32sim/commit/165f56a9cf0f211669ce282bdce9426f821b1ec4): upstream plus [mikroverk/esp32sim#322](https://github.com/mikroverk/esp32sim/pull/322) |
| Mask ROM | `esp32_rev300_rom.elf` from esp-rom-elfs `20260528`, SHA-256 `920b70635440517866aab2230964a570d2cf2b676658d93c52fbac108c1cca31` |
| ESP-IDF | v5.3.6, image `espressif/idf:v5.3.6@sha256:cee52f0c4fdaf088992a7a739f5993154e95cf1d3ab61c24f5b40e6d56a3a070` |
| The production images | The production release image, of an application not in this repository: dual-core at the default 160 MHz, PSRAM, Bluetooth Classic SPP, a cellular modem over UART through esp_modem and PPP, an SD card over SPI, and deep sleep; built with `CONFIG_ESP32_REV_MIN_3`. Also its replay image, a variant that replays a recorded session from the SD card instead of using Bluetooth |
| Public firmware | ESP-IDF v5.3.6 `examples/get-started/hello_world` (as is, with `CONFIG_ESP32_REV_MIN_3=y`, and with `CONFIG_SPIRAM=y`), `examples/bluetooth/bluedroid/classic_bt/bt_spp_initiator` and `examples/storage/sd_card/sdspi` unmodified, and this repository's `tools/cryptobench` and `tools/deepsleep` |

Both esp32sim builds carry one local change, needed only because the local Rust 1.93 lacks a newer
name; it changes no behaviour:

```diff
--- a/esp-soc/src/nat.rs
+++ b/esp-soc/src/nat.rs
-        counter.try_update(Ordering::Relaxed, Ordering::Relaxed, |n| (n < MAX_CONNECTS).then_some(n + 1)).ok().map(|_| Self(counter))
+        counter.fetch_update(Ordering::Relaxed, Ordering::Relaxed, |n| (n < MAX_CONNECTS).then_some(n + 1)).ok().map(|_| Self(counter))
```

The production images are built for v3.0, so they boot only on the build with the revision fix; the
gaps below are reproduced on upstream.

## Results

Instruction counts are core 0 + core 1; the instruction rate is for both cores together, as esp32sim
reports it. Every run is deterministic: six runs of each `tools/cryptobench` build executed the same
number of instructions to the instruction.

| Run | esp32sim | Emulated | Wall | Instructions | Minsn/s | Outcome |
|---|---|---|---|---|---|---|
| `tools/cryptobench`, accelerators on | upstream | 60 s | 7.0–9.5 s | 706 833 177 + 6 232 361 | 75–102 | Every check passes; figures under "Speed" |
| `tools/cryptobench`, accelerators off | upstream | 60 s | 31.3–34.1 s | 6 176 704 088 + 6 645 135 | 181–198 | Every check passes, and every result matches the accelerated build |
| `hello_world` with `CONFIG_SPIRAM=y`, `--psram-mb 4` | upstream | 3 s | 0.2 s | 9 544 727 + 5 041 174 | 86 | PSRAM found, tested and added to the heap (below) |
| Release image, empty NVS, no card | with fix | 60 s | 1.6 s | 70 037 356 + 9 019 168 | 50 | Boots to its diagnostic runtime and stays up |
| Release image, NVS naming a Bluetooth peer | with fix | 90 s | 76.0 s | 6 935 523 763 + 523 030 753 | 98 | Panics in the Bluetooth controller: 488 panics and resets in 90 s |
| The same, with `--ble` | with fix | 90 s | 3.1 s | 138 178 241 + 12 762 501 | 49 | No panic; 12 Bluetooth starts refused, retried with backoff |
| Replay image, no card | with fix | 600 s | 5.9 s | 236 740 911 + 24 379 202 | 44 | Runs, shuts down at 180 s, goes to deep sleep with a 2000 s timer |
| Replay image, no card | with fix | 2400 s | 10.9 s | 236 740 911 + 24 379 202 | 24 | The same counts: nothing ran after the deep sleep began |

What works on the classic chip:

- The real boot chain: mask ROM, second-stage bootloader, an OTA application at `0x20000`.
- PSRAM, shown positively by `hello_world` built with `CONFIG_SPIRAM=y`, at INFO level:
  `esp_psram: Found 4MB PSRAM device`, `esp_psram: SPI SRAM memory test OK`,
  `esp_psram: Adding pool of 4096K of PSRAM memory to heap allocator`, and a minimum free heap of
  4 462 692 bytes. The release image logs at WARN only, so for it the evidence is the absence of the
  warning IDF prints when PSRAM fails, and the fact that it runs with `--psram-mb 4`.
- NVS reads, `app_main`, every task the release image starts.
- The release image's power state machine: running with nothing answering, a shutdown, a modem
  power-off through its power key, deep sleep with a 2000 s timer.
- The VHCI interception (`--ble`): Bluedroid starts against esp32sim's virtual controller instead
  of the controller blob.
- The crypto accelerator models (SHA, AES, MPI) compute correct results: every phase of
  `tools/cryptobench` hashes its result in software, and the hashes are identical with the
  accelerators on and off (see "Speed").

## Speed

**Emulated time follows the instructions executed.** `tools/cryptobench` opens with 10 million
iterations of a loop: 80 031 103 cycles counted by the guest, which at 160 MHz is the 500.19 ms the
guest's own timer reports. So emulated time is not decoupled from the work done, and times measured
in it are consistent with each other. They are not measurements of a board: esp32sim does not model
cache timing, and its accelerator models take whatever time they take.

**CPU-bound code runs at 0.3 to 1.7 times real time; idle code runs far ahead of it.** Each
benchmark phase lasts at least 100 ms of emulated time with the accelerators on, and was timed by
the guest in emulated time and by the host in wall time from the console lines that open and close
it (`tools/benchtime.py`). A phase's result is hashed after its clock stops and after its end line
is out, so the hash is in neither time. esp32sim does not model UART transmit time: the transmit
side always reads as idle
([`esp-periph/src/uart.rs:1-2`](https://github.com/mikroverk/esp32sim/blob/2e36224d34decc2fc9d2bba5fa4c5fdf2171b0a6/esp-periph/src/uart.rs#L1-L2)),
so a line's length does not delay it. esp32sim passes the console to the host in batches, at least
once every 65 536 cycles of core 0
([`esp-soc/src/machine.rs:608`](https://github.com/mikroverk/esp32sim/blob/2e36224d34decc2fc9d2bba5fa4c5fdf2171b0a6/esp-soc/src/machine.rs#L608),
[`:759`](https://github.com/mikroverk/esp32sim/blob/2e36224d34decc2fc9d2bba5fa4c5fdf2171b0a6/esp-soc/src/machine.rs#L759),
[`:797`](https://github.com/mikroverk/esp32sim/blob/2e36224d34decc2fc9d2bba5fa4c5fdf2171b0a6/esp-soc/src/machine.rs#L797)),
which is 0.41 ms of emulated time at 160 MHz. A line can reach the host up to that much emulated
time after the guest printed it, so a phase measured between two stamps can come out up to 0.41 ms
longer or shorter than it ran: about 0.4% of the shortest phase, SHA-256 on 1 KiB at 102 ms.

Six runs of each build. Emulated times per operation are identical in every run of the same build.
Between two builds of this benchmark in which a phase's own code did not change, its time still
moved by up to 0.3%. Wall times vary more: the host's load average was 4.5 to 6.3 throughout, from
other work. Load can only lengthen the time a phase takes to run, never shorten it, so the table
gives the **best** ratio, the one least disturbed, then the median and the range. The calibration
loop, the same code in both builds, shows why: its best ratios are 1.31 and 1.32, 1% apart, while
its medians are 1.29 and 1.21. A best ratio can still be slightly high: by up to about 0.4% from the
console's batching, and by however late the host read the line that opened the phase.

| Phase | Accelerators on, per op | Emulated ÷ wall: best, median (range) | Software, per op | Emulated ÷ wall: best, median (range) |
|---|---|---|---|---|
| The calibration loop | 500.2 ms | 1.31, 1.29 (1.16–1.31) | 500.2 ms | 1.32, 1.21 (0.73–1.32) |
| SHA-256, 1 KiB | 102 µs | 0.65, 0.63 (0.59–0.65) | 522 µs | 1.64, 1.62 (1.46–1.64) |
| SHA-256, 16 KiB | 1.18 ms | 0.65, 0.63 (0.54–0.65) | 7.84 ms | 1.65, 1.61 (1.41–1.65) |
| SHA-256, 64 KiB | 4.64 ms | 0.65, 0.63 (0.45–0.65) | 31.3 ms | 1.65, 1.62 (1.40–1.65) |
| AES-128-CBC, 1 KiB | 81 µs | 0.55, 0.55 (0.53–0.55) | 452 µs | 1.37, 1.33 (1.03–1.37) |
| AES-128-CBC, 16 KiB | 1.22 ms | 0.55, 0.54 (0.44–0.55) | 7.22 ms | 1.35, 1.34 (1.05–1.35) |
| AES-128-CBC, 64 KiB | 4.85 ms | 0.55, 0.54 (0.52–0.55) | 28.9 ms | 1.39, 1.32 (1.16–1.39) |
| P-256 key generation | 100.2 ms | 0.75, 0.73 (0.44–0.75) | 98.2 ms | 0.98, 0.87 (0.68–0.98) |
| P-256 ECDH | 100.4 ms | 0.75, 0.74 (0.57–0.75) | 98.2 ms | 0.98, 0.95 (0.77–0.98) |
| P-256 ECDSA sign | 109.9 ms | 0.77, 0.75 (0.73–0.77) | 107.5 ms | 1.01, 0.97 (0.76–1.01) |
| P-256 ECDSA verify | 219.2 ms | 0.76, 0.72 (0.47–0.76) | 215.7 ms | 1.00, 0.95 (0.85–1.00) |
| RSA-2048-size `x^65537 mod n`, R² mod n recomputed each time, as for a new modulus | 7.31 ms | 0.60, 0.59 (0.53–0.60) | 61.5 ms | 1.31, 1.27 (1.16–1.31) |
| The same, R² mod n kept from the first call | 0.29 ms | 0.37, 0.37 (0.20–0.37) | 56.3 ms | 1.28, 1.23 (1.16–1.28) |
| RSA-2048-size, full 2048-bit exponent without CRT | 34.8 ms | 0.34, 0.33 (0.19–0.34) | 2320 ms | 1.32, 1.26 (0.88–1.32) |

What that means for testing:

- A test that keeps the CPU busy runs between a third of real time and 1.7 times it. It is slower
  where the firmware leans on an accelerator model: every accelerated phase's best ratio is between
  0.34 and 0.77, every software phase's between 0.98 and 1.65.
- An image that mostly waits runs much faster: the release image idling did 60 s of emulated time
  in 1.6 s, because the emulator skips ahead while both cores wait for an interrupt.
- An image in deep sleep runs faster still: the replay image did 2400 s in 10.9 s.
- So tests must measure timeouts in emulated time, never in wall time. A CPU-heavy scenario can
  take longer than it would on a board, and an idle one far less.

**What is checked.** Every mbedTLS call is checked, so no phase times an error path. SHA-256 is
checked against the one-block, two-block and million-`a` vectors of FIPS 180-2, and AES-128-CBC
against NIST SP 800-38A F.2.1. ECDH is computed from both sides, ECDSA verify must accept its own
signature and reject a changed digest, and `x^65537 mod n` must match square-and-multiply; those are
consistency checks through the same code. The independent check is the comparison between builds:
inputs come from a fixed seed, every phase's result is hashed in software (FNV-1a), and the build with
the accelerators and the build without print the same hash for every phase, in each of the six pairs
of runs (`tools/benchtime.py compare`). The exception is ECDSA verify, whose result is the return code
the call check already tests.

**Where the accelerators are used.** A trace of `esp_mpi_*` calls in the accelerated build shows the
MPI multiplier in every P-256 phase: about 3 150 calls to `esp_mpi_mul_mpi_hw_op` per key
generation, ECDH or signature, and about 6 300 per verification, each wrapped in enabling and
disabling the peripheral. P-256 therefore uses the accelerator but costs the same with it as without
it in this model, and its emulated ÷ wall ratio (best 0.75–0.77, against 0.98–1.01 in software) has
the same signature as the other accelerated phases. RSA uses the modular-exponentiation hardware,
and that part is cheap: 0.29 ms per operation once R² mod n is known. With a new modulus, most of
the 7.31 ms is computing R² mod n, which IDF v5.3.6 does in software
(`components/mbedtls/port/bignum/esp_bignum.c:197-215`, `calculate_rinv`, through
`mbedtls_mpi_mod_mpi`).

**What a TLS handshake costs**, for a client verifying a two-certificate chain:

| Handshake | Steps | Accelerators on | Software |
|---|---|---|---|
| ECDHE with an ECDSA chain | key generation + ECDH + ServerKeyExchange signature + two certificates | 100.2 + 100.4 + 3 × 219.2 ≈ 858 ms | 98.2 + 98.2 + 3 × 215.7 ≈ 844 ms |
| The same, with a client certificate (mutual TLS) | + an ECDSA signature | + 109.9 ≈ 968 ms | + 107.5 ≈ 951 ms |
| ECDHE with an RSA chain | key generation + ECDH + three RSA verifications, each under a new modulus | 100.2 + 100.4 + 3 × 7.31 ≈ 223 ms | 98.2 + 98.2 + 3 × 61.5 ≈ 381 ms |

About a second of emulated time for an ECDSA chain, and 1.1 to 1.3 s of wall time at the measured
0.75 to 0.77; about a quarter of that for an RSA chain. An RSA chain is cheaper because RSA
verification with e = 65537 is cheap in itself, as on real hardware; the accelerator model roughly
halves it again. Either is affordable in a test.

**What earlier drafts of this note got wrong**, each corrected by measuring:

- *A fixed 1.2 ms floor in the accelerator models.* Read from a benchmark that checked no results.
  Times grow with the data; 1.2 ms was the real cost of SHA-256 and AES on 16 KiB.
- *"15 to 37 times faster than real time".* That measured idle cores. CPU-bound code runs at 0.3 to
  1.7 times real time.
- *"P-256 does not use the accelerator".* It does, as the trace shows; it only costs the same.
- *1.4 ms per RSA public operation.* That was an average over 8 calls with one modulus, of which only
  the first computed R² mod n. One verification under a new modulus costs 7.3 ms; with the modulus
  reused, 0.29 ms. The same draft also credited the accelerator model with making a new-modulus
  verification eight times cheaper than software, but most of that verification is software.
- *AES-128-CBC on 64 KiB at 5.04 ms.* The result hash was computed inside the timed window; outside
  it, 4.85 ms.
- *Medians of emulated ÷ wall.* Under load, a median understates the ratio, because load only
  lengthens wall time: the calibration loop's medians differed by 6% between two builds running the
  same code. The best ratio is now the headline, with the median and range beside it.
- *Console lines reach the host the moment the guest writes them.* esp32sim passes them on in
  batches, at least every 0.41 ms of emulated time, which moves a phase's measured length by about
  0.4% at most.

## Gaps

Each gap is reproduced on public firmware on upstream esp32sim, so it can be reported without the
production images.

1. **The chip reports revision v2.0.** esp32sim hard-codes revision 2 in its eFuse model, and
   `SYSCON_DATE_REG` reads 0, so the third ECO bit IDF needs for v3.0 is missing. An image built for
   v3.0 is refused by its bootloader: `hello_world` with `CONFIG_ESP32_REV_MIN_3=y` resets 799 times
   in a 3 s run (`Image requires chip rev >= v3.0, but chip is v2.0`). Every module built on the
   ESP32-D0WD-V3 chip, such as the WROVER-E and WROOM-32E, is v3.x. Fixed in
   [mikroverk/esp32sim#322](https://github.com/mikroverk/esp32sim/pull/322), merged on 2026-10-29
   as [`8c65215`](https://github.com/mikroverk/esp32sim/commit/8c652151dd5a521249a785ca147f20a9f8eda68f).
   Rechecked on 2026-10-30 on upstream at that commit, with the same `nat.rs` change. In a 3 s run
   the same `hello_world` resets once, the power-on reset (`rst:0x1 (POWERON_RESET)`), and reports
   v3.0. The production images execute exactly the instructions they do on the fix build in the
   table above: the release image with empty NVS, 70 037 356 + 9 019 168 in 60 s; with NVS naming a
   Bluetooth peer, 6 935 523 763 + 523 030 753 in 90 s, with the same 488 panics; the same with
   `--ble`, 138 178 241 + 12 762 501; and the replay image, 236 740 911 + 24 379 202 in 600 s. The
   commit also carries [mikroverk/esp32sim#300](https://github.com/mikroverk/esp32sim/pull/300),
   which changes the virtual BLE controller; the `--ble` run is unaffected.
2. **Bluetooth Classic.** Without `--ble`, `bt_spp_initiator` panics in the controller task
   (`StoreProhibited` at `0x19c` in `btdm_rom_ref_init`, called from `r_rw_pre_main`, called from
   `btdm_controller_task`): the classic chip's controller runs partly from mask ROM and expects ROM
   state the emulator does not provide. With `--ble`, `esp_bt_controller_enable(ESP_BT_MODE_CLASSIC_BT)`
   returns `ESP_ERR_INVALID_ARG`, because esp32sim's virtual controller accepts only BLE and dual mode
   ([`esp-soc/src/ble/vhci.rs:236`](https://github.com/mikroverk/esp32sim/blob/2e36224d34decc2fc9d2bba5fa4c5fdf2171b0a6/esp-soc/src/ble/vhci.rs#L236)).
   It needs BR/EDR at the VHCI boundary and HCI forwarded to an external controller. An upstream
   issue is drafted, to follow
   [mikroverk/esp32sim#325](https://github.com/mikroverk/esp32sim/issues/325). *Since #34* it runs
   unmodified, with `--ble`'s hooks and the controller outside esp32sim: the runner's `--bt`
   forwards the guest's HCI to the harness, whose `bt` fixture is the controller, on the same air as
   the test's Bluetooth devices ([runner](features/runner-user.md#give-the-guest-a-bluetooth-controller),
   [Bluetooth peer](features/bluetooth-peer-user.md#in-front-of-the-firmware-boardbluetooth)), and
   `esp_bt_controller_enable` takes mode 2 from
   [mikroverk/esp32sim#347](https://github.com/mikroverk/esp32sim/pull/347) on (issue
   [mikroverk/esp32sim#346](https://github.com/mikroverk/esp32sim/issues/346)), merged on
   2026-11-08 as
   [`c99fb28`](https://github.com/mikroverk/esp32sim/commit/c99fb286c4378fcfa6d304b754d31e542ba3035a),
   the pin since #34. There, against an `SppPeer` named `ESP_SPP_ACCEPTOR` with the PIN `1234`, it
   finds the peer, pairs, opens the channel 1.37 s after power-on and streams to it at about
   299 kbit/s of the board's time ([validation firmware](features/validation-firmware-user.md#bt_spp_initiator)).
   Without `--ble` or `--bt` nothing changed for the controller in mask ROM; that run was not made
   again.
3. **SD card over SPI.** `examples/storage/sd_card/sdspi` makes fourteen transfers on SPI2, then
   `sdmmc_card_init failed (0x107)` and `Failed to initialize the card (ESP_ERR_TIMEOUT)`: the classic
   chip has no card model on its general-purpose SPI. esp32sim has an SD card model, wired only to the
   P4's SDMMC host. *Since #32* the runner has a card on the SPI pins, and the example runs to its end
   ([runner](features/runner-user.md#put-an-sd-card-in)).
4. **Deep-sleep timer wake.** `tools/deepsleep` sleeps with a 5 s timer, and in a 60 s run it boots
   once and never wakes, which rules out a timer running up to 12 times slow. The replay image's
   runs agree: 600 s and 2400 s execute the same instruction count. The guest also calibrates its
   slow clock to the nominal value (3 495 245 in IDF's Q13.19 format, 6.667 µs per cycle, 150 kHz),
   but that is what the guest believes, not a measurement of the emulator's timer. Rechecked on
   2026-11-07 on `8c65215`, through `esp32tl-runner` with `--max-seconds 60`: the same single boot,
   `wake cause 0`, and `stop: Halted at 60.000000 s (0.2 s wall, 0 resets)`. *Since #35* it wakes:
   reported in [mikroverk/esp32sim#331](https://github.com/mikroverk/esp32sim/issues/331), from
   another firmware, and fixed in [mikroverk/esp32sim#336](https://github.com/mikroverk/esp32sim/pull/336),
   merged on 2026-11-07 as
   [`afcfba6`](https://github.com/mikroverk/esp32sim/commit/afcfba65da0da30a35fb0a7c381854c62b92e338),
   the pin since #35. Rechecked on 2026-11-08 at that commit, the same way: 12 boots, each after the
   first with `rst:0x5 (DEEPSLEEP_RESET)` and `wake cause 4` (`ESP_SLEEP_WAKEUP_TIMER`), and
   `stop: Halted at 60.000000 s (0.8 s wall, 11 resets)`. Through the harness, a test that waits
   605 s on the board's clock sees the second boot at 5.045 s and one every 5.047 s after it, 120
   in all, in 8.5 s of wall time.
5. **External attachment.** The release image's GNSS UART reports no data and its modem UART has
   nothing on it: there is no way to put a process at the far end of a UART or a GPIO line from the
   stock CLI. Proposed in [mikroverk/esp32sim#325](https://github.com/mikroverk/esp32sim/issues/325).
6. **Board wiring.** The modem's ring indicator line (GPIO 33 on the first board) reads high with no
   modem fitted, because the board's pull-down is not modelled, so the application believes a modem
   is powered and presses its power key. That belongs in esp32tl's board profile, not in the
   emulator.
7. **Minor:** `rtc_clk_init: Bogus XTAL frequency: 8 MHz` at every boot. The guest calibrates the
   slow clock to its nominal value (gap 4), and nothing seen so far depends on the XTAL detection.
8. **Bluetooth HCI from PSRAM.** Found on 2026-11-10 on a production image built with
   `CONFIG_BT_ALLOCATION_FROM_SPIRAM_FIRST`, and reproduced on public firmware with
   `bt_spp_initiator_psram`, ESP-IDF's `bt_spp_initiator` with that option and PSRAM on, on
   `lilygo-t-a7670g-r2` at the pin `c99fb28`. Bluedroid's first command, `HCI_Reset`, is 4 bytes
   at `0x3f8010cf`, in PSRAM, and esp32sim's VHCI `Send` hook takes a packet from internal DRAM
   only on the classic ESP32, so it refuses it (`[ble] rejected invalid VHCI packet pointer, length
   or controller state`) and nothing crosses the VHCI; the S3 takes mapped PSRAM already. The fix
   belongs in esp32sim: the classic ESP32's `valid_packet_range` taking PSRAM that is mapped and
   readable, as the S3's does. *Since #180* the image gets through: the fix is
   [mikroverk/esp32sim#353](https://github.com/mikroverk/esp32sim/pull/353), merged on 2026-11-13 as
   [`3bdb958`](https://github.com/mikroverk/esp32sim/commit/3bdb9582a910974e4e025d274d6ec61e5c481be6),
   the pin since #180, on which the same image pairs, opens SPP and streams at about 299 kbit/s
   ([validation firmware](features/validation-firmware-technical.md#bt_spp_initiator_psram)).

## Reproducing

Build in the ESP-IDF v5.3.6 container from the table above. For an example, copy it out of
`$IDF_PATH/examples` first; for `hello_world` built for v3.0, append `CONFIG_ESP32_REV_MIN_3=y` to its
`sdkconfig.defaults`, and for the PSRAM run, `CONFIG_SPIRAM=y`. Then, with `B` the build directory
and `APP` the application image's name:

```sh
idf.py set-target esp32 build

esp32sim --chip esp32 --board none --boot rom --console uart0 --flash-mb 4 --max-seconds 3 \
  --flash-at 0x1000=$B/bootloader/bootloader.bin \
  --flash-at 0x8000=$B/partition_table/partition-table.bin \
  --flash-at 0x10000=$B/$APP.bin --elf $B/$APP.elf
```

- `hello_world` built for v3.0: as above; 799 resets in the 3 s on `2e36224`. From `8c65215` on it
  boots (gap 1).
- `hello_world` with PSRAM: add `--psram-mb 4`.
- `bt_spp_initiator` (`APP=bt_spp_initiator_demo`): `--max-seconds 2`, once as is and once with
  `--ble --trace-fn esp_bt_controller` to see the controller calls.
- `sdspi` (`APP=sdspi`): `--max-seconds 10`.
- `tools/deepsleep` (`APP=deepsleep`): `--max-seconds 60`.
- `tools/cryptobench` (`APP=cryptobench`): `--max-seconds 60`, through
  `python3 tools/benchtime.py run OUT esp32sim ...` for wall times per phase; then
  `python3 tools/benchtime.py summary OUT...` for the best ratio, the median and the range over
  several runs of one build. Add `--trace-fn esp_mpi_` to see the accelerator calls. For the
  software variant, build with
  `idf.py -B build-nohw -DSDKCONFIG=build-nohw/sdkconfig -DSDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.defaults.nohw" build`;
  `python3 tools/benchtime.py compare OUT_ACCELERATED OUT_SOFTWARE` must report that all results
  agree.

`tools/hci_probe` runs on a real board, not on esp32sim: it asks the chip's own Bluetooth controller,
through VHCI, what it reports, and prints every HCI packet both ways, `TX` and `RX` in hex, on the
USB-Serial/JTAG console. For an ESP32-S3, in the v5.3.6 container, into a build directory of its
own whose `sdkconfig` is made anew from `sdkconfig.defaults` (ESP-IDF reads the defaults only for
what an existing `sdkconfig` leaves unset, so an older one would keep its host configuration):

```sh
rm -rf tools/hci_probe/build-probe
idf.py -C tools/hci_probe -B tools/hci_probe/build-probe \
  -D SDKCONFIG=tools/hci_probe/build-probe/sdkconfig -DIDF_TARGET=esp32s3 build
```

then, on the host, from `tools/hci_probe/build-probe`, with esptool 5 (`python -m esptool --chip
esp32s3 --port PORT write-flash @flash_args`; esptool 4, as ESP-IDF v5.3.6 ships, spells it
`write_flash`), and read the console for 20 s after a reset. Check that the console says
`BLE_INIT: Feature Config, …, BLE_50:0, …` before comparing what follows.
Its `sdkconfig.defaults` configure Bluedroid as ESP-IDF's BLE 4.2 examples do, without starting it,
so that the controller runs as under them; without Bluedroid in the build the controller runs with
`BLE_50:1` and answers otherwise. The discard event of the flow control of advertising reports
comes only when more legacy advertising than the 2 credits and the lost threshold of 3 reaches the
3 s scan: a device that advertises in range, at an interval of a few hundred ms or less, is
enough; on a quiet bench the event does not come. Its count is what the scan heard and dropped,
104 in the capture below, not a constant. The ESP32-S3's
answers, of 2026-11-19, are in `tools/hci_probe/esp32s3.txt`, its address masked and the
advertising reports of the devices around left out; `esp32tl/bluetooth/guest.py` reports them for
an LE-only chip.

The production images, which are not in this repository, ran with `--flash-mb 4 --psram-mb 4` and
`--flash-at` at `0x1000` (bootloader), `0x8000` (partition table), `0x10000` (otadata) and `0x20000`
(the application in `ota_0`), plus `--flash-at 0x9000=<nvs image>` for the runs that named a
Bluetooth peer, the NVS image made with IDF's `nvs_partition_gen.py`.
