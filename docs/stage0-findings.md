# Stage 0: unmodified ESP-IDF images on the classic ESP32 in esp32sim

The question stage 0 answered: can esp32sim run an unmodified release image of a dual-core ESP-IDF
application with PSRAM, fast enough to test against, and where does it stop? This is the evidence
behind the dependency table in the specification (#1). It is a record of what was measured, so it is
amended by measuring again, not by argument.

Measured on 2026-10-29, locally, on an Apple silicon Mac (arm64, macOS).

## Inputs

| What | Version |
|---|---|
| esp32sim | `main` at `2e36224` (2026-10-27), built locally. Release v0.3.0 refuses `--psram-mb` on the classic ESP32 ("--psram-mb is not available on the classic ESP32") |
| Mask ROM | `esp32_rev300_rom.elf`, esp-rom-elfs `20260528`, SHA-256 `920b7063…1cca31` |
| ESP-IDF | v5.3.6, image `espressif/idf:v5.3.6@sha256:cee52f0c4fdaf088992a7a739f5993154e95cf1d3ab61c24f5b40e6d56a3a070` |
| Application | The first consumer's release image: dual-core, PSRAM, Bluetooth Classic SPP, a cellular modem over UART through esp_modem and PPP, an SD card over SPI, and deep sleep; built with `CONFIG_ESP32_REV_MIN_3`. Also its "desk" variant, which replays a recorded session from the SD card instead of using Bluetooth |
| Public reproductions | ESP-IDF v5.3.6 `examples/get-started/hello_world` with `CONFIG_ESP32_REV_MIN_3=y` added, and `examples/bluetooth/bluedroid/classic_bt/bt_spp_initiator` unmodified (`CONFIG_BTDM_CTRL_MODE_BR_EDR_ONLY=y`) |

Two local changes to esp32sim were needed during stage 0, neither of them in a release:

1. `try_update` renamed to `fetch_update` in `esp-soc/src/nat.rs`, because the local Rust 1.93 lacks
   the newer name. Toolchain only; no behaviour change.
2. Bit 31 of `SYSCON_DATE_REG` set, so the chip reports revision v3.0. Superseded by
   mikroverk/esp32sim#322, which does it properly.

Common flags: `--chip esp32 --board none --boot rom --flash-mb 4 --psram-mb 4`, the image written
with `--flash-at` at `0x1000` (bootloader), `0x8000` (partition table), `0x10000` (otadata) and
`0x20000` (`ota_0`), and `--elf` for symbols.

## Results

| Run | Emulated | Wall | Outcome |
|---|---|---|---|
| Crypto benchmark (`tools/cryptobench`) | 120 s | 7.8 s | Every primitive runs; figures below |
| Release image, empty NVS, no card | 60 s | 1.6 s | Boots to its diagnostic runtime and stays up |
| Release image, NVS naming a Bluetooth peer | 90 s | — | Panics in the Bluetooth controller, then a boot loop |
| The same, with `--ble` | 90 s | 3.3 s | No panic; every Bluetooth start is refused, retried with backoff |
| Desk image, no card | 600 s and 2400 s | 6.2 s and 11.0 s | RUNNING, then SHUTDOWN at 180 s, then SLEEP; it never wakes |

What works on the classic chip:

- The real boot chain: mask ROM, second-stage bootloader, an OTA application at `0x20000`.
- PSRAM: no initialisation warning, which IDF prints at WARN when it fails.
- NVS reads, `app_main`, every task the application starts.
- The application's power state machine: running with nothing answering, a shutdown, a modem
  power-off through its power key, deep sleep with a 2000 s timer.
- The VHCI interception (`--ble`): Bluedroid starts against esp32sim's virtual controller instead
  of the controller blob.

## Speed

40 to 53 million instructions per second with the JIT, which the classic chip has too. Against real
time: 15 times faster on the run dominated by the crypto benchmark, 27 to 37 times on images that
stayed awake and mostly idle. Runs that include deep sleep go faster still (97 and 218 times), but
only because the cores are halted.

Crypto, in emulated time per operation: SHA-256 of 16 KiB 1.2 ms, AES-128-CBC of 16 KiB 1.2 ms, P-256
key generation 100 ms, ECDH 100 ms, ECDSA sign 110 ms and verify 219 ms, RSA-2048 public operation
1.2 ms, a full-length RSA-2048 exponent 35 ms. The repeated 1.2 ms suggests the accelerator models
complete on a fixed granularity rather than at silicon speed, so these are not measurements of a
board. What they do show is that a TLS handshake costs well under a second of emulated time and a
fraction of that in wall time, so TLS is not a constraint on testing.

## Gaps

Each gap is reproduced on public firmware where it can be, so that it can be reported upstream.

1. **The chip reports revision v2.0.** esp32sim hard-codes revision 2 in its eFuse model, and
   `SYSCON_DATE_REG` reads 0, so the third ECO bit IDF needs for v3.0 is missing. An image built for
   v3.0 is refused by the bootloader: `hello_world` with `CONFIG_ESP32_REV_MIN_3=y` resets 799 times
   in 3 s (`Image requires chip rev >= v3.0, but chip is v2.0`). Every ESP32-D0WD-V3 module, such as
   the WROVER-E and WROOM-32E, is v3. Fix proposed in mikroverk/esp32sim#322.
2. **Bluetooth Classic.** Without `--ble`, `bt_spp_initiator` panics in the controller task
   (`StoreProhibited` at `0x19c` in `btdm_rom_ref_init`, called from `r_rw_pre_main`, called from
   `btdm_controller_task`): the classic chip's controller runs partly from mask ROM and expects ROM
   state the emulator does not provide. With `--ble`, `esp_bt_controller_enable(ESP_BT_MODE_CLASSIC_BT)`
   returns `ESP_ERR_INVALID_ARG`, because esp32sim's virtual controller accepts only BLE and dual mode
   (`esp-soc/src/ble/vhci.rs:236`). Needs BR/EDR at the VHCI boundary and HCI forwarded to an
   external controller; an upstream issue is drafted, to follow #325.
3. **SD card over SPI.** Fourteen transfers on SPI2, then `sdmmc_card_init failed (0x107)`: the
   classic chip has no card model on its general-purpose SPI. esp32sim has an SD card model, wired
   only to the P4's SDMMC host.
4. **Deep-sleep timer wake.** The desk image's 600 s and 2400 s runs execute the same instruction
   count, so nothing runs after it goes to sleep, although its timer was 2000 s.
5. **External attachment.** The GNSS UART reports no data and the modem UART has nothing on it; there
   is no way to put a process at the far end of a UART or a GPIO line from the stock CLI. Proposed in
   mikroverk/esp32sim#325.
6. **Board wiring.** The modem's ring indicator line (GPIO 33 on the first board) reads high with no
   modem fitted, because the board's pull-down is not modelled, so the application believes a modem
   is powered and presses its power key. That belongs in esp32tl's board profile, not in the
   emulator.
7. **Minor:** `rtc_clk_init: Bogus XTAL frequency: 8 MHz` at every boot, with no visible effect.

## Reproducing

The public reproductions need only ESP-IDF and esp32sim:

```sh
# hello_world, built for v3.0 and up, in the IDF v5.3.6 container
cp -r $IDF_PATH/examples/get-started/hello_world . && cd hello_world
echo "CONFIG_ESP32_REV_MIN_3=y" >> sdkconfig.defaults
idf.py set-target esp32 build

esp32sim --chip esp32 --board none --boot rom --console uart0 --max-seconds 3 \
  --flash-at 0x1000=build/bootloader/bootloader.bin \
  --flash-at 0x8000=build/partition_table/partition-table.bin \
  --flash-at 0x10000=build/hello_world.bin --elf build/hello_world.elf
```

`bt_spp_initiator` is built unmodified the same way (its image is
`build/bt_spp_initiator_demo.bin`) and run with and without `--ble`, adding
`--trace-fn esp_bt_controller` to see the controller calls.

The crypto benchmark is `tools/cryptobench`, built the same way and run for 120 s.
