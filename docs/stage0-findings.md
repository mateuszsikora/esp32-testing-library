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
| The consumer's images | The first consumer's release image: dual-core at the default 160 MHz, PSRAM, Bluetooth Classic SPP, a cellular modem over UART through esp_modem and PPP, an SD card over SPI, and deep sleep; built with `CONFIG_ESP32_REV_MIN_3`. Also its "desk" variant, which replays a recorded session from the SD card instead of using Bluetooth. Not in this repository |
| Public firmware | ESP-IDF v5.3.6 `examples/get-started/hello_world` (as is, with `CONFIG_ESP32_REV_MIN_3=y`, and with `CONFIG_SPIRAM=y`), `examples/bluetooth/bluedroid/classic_bt/bt_spp_initiator` and `examples/storage/sd_card/sdspi` unmodified, and this repository's `tools/cryptobench` and `tools/deepsleep` |

Both esp32sim builds carry one local change, needed only because the local Rust 1.93 lacks a newer
name; it changes no behaviour:

```diff
--- a/esp-soc/src/nat.rs
+++ b/esp-soc/src/nat.rs
-        counter.try_update(Ordering::Relaxed, Ordering::Relaxed, |n| (n < MAX_CONNECTS).then_some(n + 1)).ok().map(|_| Self(counter))
+        counter.fetch_update(Ordering::Relaxed, Ordering::Relaxed, |n| (n < MAX_CONNECTS).then_some(n + 1)).ok().map(|_| Self(counter))
```

The consumer's images are built for v3.0, so they boot only on the build with the revision fix; the
gaps below are reproduced on upstream.

## Results

Instruction counts are core 0 + core 1. Millions of instructions per second are for both cores
together.

| Run | esp32sim | Emulated | Wall | Instructions | Outcome |
|---|---|---|---|---|---|
| `tools/cryptobench`, accelerators on | upstream | 30 s | 4.8 s | 553 116 320 + 3 299 916 | Every check passes; figures under "Speed" |
| `tools/cryptobench`, accelerators off | upstream | 30 s | 7.7 s | 1 450 718 539 + 3 366 744 | Every check passes |
| `hello_world` with `CONFIG_SPIRAM=y`, `--psram-mb 4` | upstream | 3 s | 0.2 s | 9 544 727 + 5 041 174 | PSRAM found, tested and added to the heap (below) |
| Release image, empty NVS, no card | with fix | 60 s | 1.6 s | 70 037 356 + 9 019 168 | Boots to its diagnostic runtime and stays up |
| Release image, NVS naming a Bluetooth peer | with fix | 90 s | 76.0 s | 6 935 523 763 + 523 030 753 | Panics in the Bluetooth controller: 488 panics and resets in 90 s |
| The same, with `--ble` | with fix | 90 s | 3.1 s | 138 178 241 + 12 762 501 | No panic; 12 Bluetooth starts refused, retried with backoff |
| Desk image, no card | with fix | 600 s | 5.9 s | 236 740 911 + 24 379 202 | RUNNING, SHUTDOWN at 180 s, SLEEP with a 2000 s timer |
| Desk image, no card | with fix | 2400 s | 10.9 s | 236 740 911 + 24 379 202 | The same counts: nothing ran after SLEEP |

What works on the classic chip:

- The real boot chain: mask ROM, second-stage bootloader, an OTA application at `0x20000`.
- PSRAM, shown positively by `hello_world` built with `CONFIG_SPIRAM=y`, at INFO level:
  `esp_psram: Found 4MB PSRAM device`, `esp_psram: SPI SRAM memory test OK`,
  `esp_psram: Adding pool of 4096K of PSRAM memory to heap allocator`, and a minimum free heap of
  4 462 692 bytes. The release image logs at WARN only, so for it the evidence is the absence of the
  warning IDF prints when PSRAM fails, and the fact that it runs.
- NVS reads, `app_main`, every task the release image starts.
- The release image's power state machine: running with nothing answering, a shutdown, a modem
  power-off through its power key, deep sleep with a 2000 s timer.
- The VHCI interception (`--ble`): Bluedroid starts against esp32sim's virtual controller instead
  of the controller blob.

## Speed

**Emulated time follows the instructions executed.** `tools/cryptobench` opens with 10 million
iterations of a loop: 80 031 103 cycles counted by the guest, which at 160 MHz is the 500.19 ms the
guest's own timer reports. So emulated time is not decoupled from the work done, and times measured
in it are consistent with each other. They are not measurements of a board: esp32sim does not model
cache timing, and its accelerator models take whatever time they take.

**CPU-bound code runs at roughly real time; idle code runs far ahead of it.** Each benchmark phase
was timed in emulated time by the guest and in wall time by the host, from the console lines that
open and close it:

| Phase | Emulated per operation | Emulated ÷ wall |
|---|---|---|
| The calibration loop | 500.2 ms | 1.31 |
| SHA-256 of 1 / 16 / 64 KiB, accelerator | 102 µs / 1.18 ms / 4.64 ms | 0.63–0.65 |
| SHA-256 of 1 / 16 / 64 KiB, software | 522 µs / 7.84 ms / 31.3 ms | 1.49–1.65 |
| AES-128-CBC of 1 / 16 / 64 KiB, accelerator | 81 µs / 1.22 ms / 4.85 ms | 0.39–0.55 |
| AES-128-CBC of 1 / 16 / 64 KiB, software | 453 µs / 7.22 ms / 28.9 ms | 0.96–1.37 |
| P-256 key generation, ECDH, ECDSA sign, ECDSA verify, accelerator | 100 / 100 / 110 / 219 ms | 0.74–0.78 |
| The same, software | 98 / 98 / 108 / 216 ms | 0.99–1.01 |
| RSA-2048-size `x^65537 mod n`, accelerator / software | 1.41 ms / 57.0 ms | 0.55 / 1.34 |
| RSA-2048-size, full 2048-bit exponent without CRT, accelerator / software | 34.8 ms / 2318 ms | 0.34 / 1.33 |

So a test that keeps the CPU busy runs between a third of real time and one and a half times it,
slower where the firmware leans on an accelerator model; an image that mostly waits runs much faster
(the release image idling: 60 s emulated in 1.6 s), because the emulator skips ahead while both cores
wait for an interrupt; and
an image in deep sleep runs faster still (the desk image: 2400 s in 10.9 s). Tests must therefore
measure timeouts in emulated time, never in wall time: a CPU-heavy scenario can take longer than it
would on a board, and an idle one far less.

Three things the benchmark shows that an earlier draft of this note got wrong:

- **There is no fixed 1.2 ms floor.** An earlier run without any result checks showed 1.2 ms for
  several operations and read it as a fixed granularity in the accelerator models. With every call
  checked and every result verified, times grow with the data (SHA-256: 102 µs, 1.18 ms, 4.64 ms
  for 1, 16 and 64 KiB). The 1.2 ms values were the real cost of those particular sizes.
- **P-256 does not use the accelerator.** It costs the same with the MPI accelerator on and off, so
  elliptic-curve operations run in software either way.
- **RSA does.** The accelerator model makes `x^65537 mod n` forty times cheaper than software, which
  is why RSA looked faster than ECDH.

**What a TLS handshake costs** with these figures, for a client verifying a two-certificate chain:

| Handshake | Steps | Emulated |
|---|---|---|
| ECDHE with an ECDSA chain | key generation 100 + ECDH 100 + ServerKeyExchange signature 219 + two certificates 2 × 219 | ≈ 857 ms |
| The same, with a client certificate (mutual TLS) | + an ECDSA signature 110 | ≈ 967 ms |
| ECDHE with an RSA chain | key generation 100 + ECDH 100 + three RSA verifications 3 × 1.4 | ≈ 204 ms |

About a second of emulated time for an ECDSA chain, and 1.1 to 1.3 s of wall time at the measured
0.75; a fifth of that for an RSA chain. That is affordable in a test, but it is not free,
and an ECDSA chain costs more than an RSA one here mainly because the RSA side is accelerated in the
model.

## Gaps

Each gap is reproduced on public firmware on upstream esp32sim, so it can be reported without the
first consumer's firmware.

1. **The chip reports revision v2.0.** esp32sim hard-codes revision 2 in its eFuse model, and
   `SYSCON_DATE_REG` reads 0, so the third ECO bit IDF needs for v3.0 is missing. An image built for
   v3.0 is refused by its bootloader: `hello_world` with `CONFIG_ESP32_REV_MIN_3=y` resets 799 times
   in a 3 s run (`Image requires chip rev >= v3.0, but chip is v2.0`). Every module built on the
   ESP32-D0WD-V3 chip, such as the WROVER-E and WROOM-32E, is v3.x. Fixed in
   [mikroverk/esp32sim#322](https://github.com/mikroverk/esp32sim/pull/322), not yet merged.
2. **Bluetooth Classic.** Without `--ble`, `bt_spp_initiator` panics in the controller task
   (`StoreProhibited` at `0x19c` in `btdm_rom_ref_init`, called from `r_rw_pre_main`, called from
   `btdm_controller_task`): the classic chip's controller runs partly from mask ROM and expects ROM
   state the emulator does not provide. With `--ble`, `esp_bt_controller_enable(ESP_BT_MODE_CLASSIC_BT)`
   returns `ESP_ERR_INVALID_ARG`, because esp32sim's virtual controller accepts only BLE and dual mode
   ([`esp-soc/src/ble/vhci.rs:236`](https://github.com/mikroverk/esp32sim/blob/2e36224d34decc2fc9d2bba5fa4c5fdf2171b0a6/esp-soc/src/ble/vhci.rs#L236)).
   It needs BR/EDR at the VHCI boundary and HCI forwarded to an external controller. An upstream
   issue is drafted, to follow
   [mikroverk/esp32sim#325](https://github.com/mikroverk/esp32sim/issues/325).
3. **SD card over SPI.** `examples/storage/sd_card/sdspi` makes fourteen transfers on SPI2, then
   `sdmmc_card_init failed (0x107)` and `Failed to initialize the card (ESP_ERR_TIMEOUT)`: the classic
   chip has no card model on its general-purpose SPI. esp32sim has an SD card model, wired only to the
   P4's SDMMC host.
4. **Deep-sleep timer wake.** `tools/deepsleep` sleeps with a 5 s timer, and in a 60 s run it boots
   once and never wakes. The desk image's runs agree: 600 s and 2400 s execute the same instruction
   count. This is not a slow clock running late: the guest's slow-clock calibration reads 3 495 245,
   which in IDF's Q13.19 format is 6.667 µs per cycle, the nominal 150 kHz.
5. **External attachment.** The consumer's GNSS UART reports no data and its modem UART has nothing
   on it: there is no way to put a process at the far end of a UART or a GPIO line from the stock
   CLI. Proposed in [mikroverk/esp32sim#325](https://github.com/mikroverk/esp32sim/issues/325).
6. **Board wiring.** The modem's ring indicator line (GPIO 33 on the first board) reads high with no
   modem fitted, because the board's pull-down is not modelled, so the application believes a modem
   is powered and presses its power key. That belongs in esp32tl's board profile, not in the
   emulator.
7. **Minor:** `rtc_clk_init: Bogus XTAL frequency: 8 MHz` at every boot. The slow clock is calibrated
   correctly (gap 4), and nothing seen so far depends on the XTAL detection.

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

- `hello_world` built for v3.0: as above; 799 resets in the 3 s.
- `hello_world` with PSRAM: add `--psram-mb 4`.
- `bt_spp_initiator` (`APP=bt_spp_initiator_demo`): `--max-seconds 2`, once as is and once with
  `--ble --trace-fn esp_bt_controller` to see the controller calls.
- `sdspi` (`APP=sdspi`): `--max-seconds 10`.
- `tools/deepsleep` (`APP=deepsleep`): `--max-seconds 60`.
- `tools/cryptobench` (`APP=cryptobench`): `--max-seconds 30`. For the software variant, build with
  `idf.py -B build-nohw -DSDKCONFIG=build-nohw/sdkconfig -DSDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.defaults.nohw" build`.
  Wall times per phase come from timing the console's `BENCH begin` and `BENCH end` lines on the host.
