# esp32tl-runner

`esp32tl-runner` runs an unmodified image for the classic ESP32, the ESP32-S3, the ESP32-C3, the
ESP32-C6, the ESP32-P4 or the ESP32-S31 in the esp32sim emulator, on the emulated clock, and prints the guest's UART0 console on
stdout. [Chips](#chips) says what esp32sim models on each of them. With `--board`, it connects to a
peer, the harness or any program that speaks [the link protocol](../protocol.md), and wires the
chip's pins to it: the peer sees what the guest sends on its UARTs and the levels it drives on
the pins the peer watches, and sends bytes and levels back. With `--sd-spi`, an SD card slot sits
on four of the chip's pins, with or without `--board`, holding a card with the image file of
`--sd-rw`; with `--board` the peer can take the card out and put one in while the guest runs. With
`--wifi`, a Wi-Fi access point is on the air for the chip's station, and the network behind it is
the peer's. With `--bt`, the guest's Bluetooth controller is the peer's; with `--no-bt`, the guest
has none, and its call to start one ends the run.
The `esp32sim` backend of the pytest plugin starts it, with `--board socket:PATH` and itself as
the peer, for every test that uses `board` or `console`. This document is for running it by hand, to watch an image boot,
to drive it from a peer of your own, or to see why a run ended the way it did.

## Build it

`make setup` builds it, and `make build-runner` builds it alone
([development guide](../guides/development.md)). The binary is
`runner/target/release/esp32tl-runner`; `make build-runner RUNNER_PROFILE=dev` builds it without
fat LTO, quicker to link, into `runner/target/debug/esp32tl-runner`. The first build needs
network access to GitHub, where cargo fetches esp32sim.

## Run an image

Every file of the image's `flasher_args.json` goes in with `--flash-at`, at its offset. For the
`hello_world` validation image ([validation firmware](validation-firmware-user.md)), with the ROM
ELF that `make setup` fetches:

```sh
B=firmware/hello_world/build
runner/target/release/esp32tl-runner --chip esp32 \
  --rom .cache/esp-rom-elfs/20260528/esp32_rev300_rom.elf \
  --flash-at 0x1000=$B/bootloader/bootloader.bin \
  --flash-at 0x8000=$B/partition_table/partition-table.bin \
  --flash-at 0x10000=$B/hello_world.bin \
  --max-seconds 5
```

The same image built for the ESP32-S3 (`firmware/build.sh --chips hello_world@esp32s3`) has its
bootloader at 0x0, as on the ESP32-C3 and the ESP32-C6; the ESP32-P4's and the ESP32-S31's is at
0x2000, the classic ESP32's and the S2's at 0x1000:

```sh
B=firmware/hello_world/build-esp32s3
runner/target/release/esp32tl-runner --chip esp32s3 \
  --rom .cache/esp-rom-elfs/20260528/esp32s3_rev0_rom.elf \
  --flash-at 0x0=$B/bootloader/bootloader.bin \
  --flash-at 0x8000=$B/partition_table/partition-table.bin \
  --flash-at 0x10000=$B/hello_world.bin \
  --max-seconds 5
```

For the ESP32-P4, built for rev ≥ 3 as ESP-IDF builds by default, with the ROM of that revision:

```sh
B=firmware/hello_world/build-esp32p4
runner/target/release/esp32tl-runner --chip esp32p4 \
  --rom .cache/esp-rom-elfs/20260528/esp32p4_rev300_rom.elf \
  --flash-at 0x2000=$B/bootloader/bootloader.bin \
  --flash-at 0x8000=$B/partition_table/partition-table.bin \
  --flash-at 0x10000=$B/hello_world.bin \
  --max-seconds 5
```

`esp32tl-runner --help` (or `-h`) prints the usage on stderr and exits 0:

```text
usage: esp32tl-runner --chip CHIP --flash-at OFFSET=FILE... --max-seconds S [options]

  --chip CHIP           the chip: esp32 (the classic ESP32), esp32s3, esp32c3, esp32c6,
                        esp32p4 (rev < 3 or rev >= 3, as the images are built) or
                        esp32s31
  --flash-at OFFSET=FILE   write FILE into flash at OFFSET (hex, 0x optional); repeatable
  --max-seconds S       stop after S emulated seconds: the normal end of a run
  --rom F               the mask ROM ELF; default, as esp32sim's: $ESP32SIM_ROM_DIR,
                        then ~/.espressif/tools/esp-rom-elfs/*/ (newest release first,
                        not the pinned 20260528: whoever runs the tests passes --rom)
  --flash-mb N          flash size in MiB: 1, 2, 4, 8 or 16 (default 4)
  --psram-mb N          PSRAM size in MiB (default 0): 0 or 4 on the classic ESP32,
                        0, 2, 4 or 8 on the ESP32-S3, 0, 16 or 32 on the ESP32-P4,
                        0 or 16 on the ESP32-S31, 0 on the ESP32-C3 and ESP32-C6
  --board socket:PATH   connect to the peer on the Unix socket PATH (docs/protocol.md);
  --board tcp:HOST:PORT   or over TCP. Without --board the chip has nothing on its pins
  --peer-timeout S      how long, in wall seconds, a turn of the peer may take before the
                        run fails with exit code 4 (default 60; 0 waits for ever)
  --parent-fd N         the read end of a pipe, inherited as file descriptor N, whose write
                        end the process that started the runner holds: at its end of file
                        the run ends as with the peer gone (exit code 4); needs --board
  --console stdout|socket|both   where the console goes (default stdout); socket and both
                        need --board
  --console-port uart0|usb   which of the guest's outputs is the console (default uart0):
                        UART0, or the USB-Serial/JTAG controller of the ESP32-S3,
                        ESP32-C3, ESP32-C6, ESP32-P4 and ESP32-S31, whose input is the
                        peer's console_rx; so is UART0's on the ESP32-P4 and ESP32-S31,
                        whose UARTs have no pins in esp32sim
  --sd-spi sclk=N,mosi=N,miso=N,cs=N   an SD card slot on these GPIOs, in SPI mode; empty
                        unless --sd-rw puts a card in it, and the peer's sd_card can
                        put one in or take it out while the guest runs
  --sd-rw IMAGE         an SD card holding IMAGE, a whole number of 512 KiB units, in the
                        slot of --sd-spi from power-on; the guest's writes go into the file
  --wifi ssid=…,psk=…,chan=…,bssid=…   esp32sim's virtual access point, every key
                        optional; the network behind it is the peer's, over the
                        socket (eth_tx, eth_rx): needs --board
  --wifi-sta ssid=…[,psk=…][,mac=…]   a virtual station joining the guest's
                        access point; needs --board; exclusive with --wifi and --wifi-nearby
  --wifi-nearby ssid=…,chan=…,rssi=…,secure=0|1,bssid=…   a network around the
                        station that a scan lists and nothing can join; repeatable,
                        needs --wifi
  --realtime            pace the run to wall time, for a real device at the peer's end
                        of a route (docs/protocol.md, section 12): needs --board
  --flash-out PATH      write the whole flash to PATH when the peer's flash_dump asks for
                        it, and at the end of the run (docs/protocol.md, section 5.21)
  --bt ELF              the guest's Bluetooth controller is the peer's, over the socket
                        (hci_tx, hci_rx): esp32sim's VHCI hooks take their symbols from
                        ELF, the application's; needs --board; repeatable, the first the
                        application's: each ELF of another image the guest may boot, such
                        as an OTA update, and the hooks follow the app the bootloader
                        started, by the app_elf_sha256 of its esp_app_desc_t
  --no-bt ELF           the guest has no Bluetooth controller: its call to
                        esp_bt_controller_init, at that symbol of ELF, the application's,
                        ends the run (end bt_controller, exit code 5); needs --board,
                        exclusive with --bt
```

| Flag | Default | Accepts |
|---|---|---|
| `--chip` | none, required | `esp32`, `esp32s3`, `esp32c3`, `esp32c6`, `esp32p4` or `esp32s31`, in any case: `--chip esp32h2: not one of esp32, esp32s3, esp32c3, esp32c6, esp32p4, esp32s31` otherwise |
| `--flash-at OFFSET=FILE` | none, at least one required | `OFFSET` in hex, with or without a lowercase `0x`; `FILE` is everything after the first `=`. Repeatable; files are written in the order given |
| `--max-seconds S` | none, required | a finite number above 0; fractions are fine |
| `--rom F` | the lookup below | a path to the mask ROM ELF |
| `--flash-mb N` | `4` | `1`, `2`, `4`, `8` or `16` |
| `--psram-mb N` | `0` | `0`, or a size the chip takes: `4` on the classic ESP32; `2`, `4` or `8` on the ESP32-S3; `16` or `32` on the ESP32-P4; `16` on the ESP32-S31; none on the ESP32-C3 and ESP32-C6. Otherwise `--psram-mb: the ESP32-S3 supports only 0, 2, 4 or 8 MiB, got "3"`, naming the chip |
| `--board` | none: nothing on the chip's pins | `socket:PATH`, a Unix socket path that is not empty, or `tcp:HOST:PORT`, with a host that is not empty and a port from 0 to 65535 (`tcp:[::1]:7000` for IPv6) |
| `--peer-timeout S` | `60` | `0`, to wait for ever, or a positive number of seconds; fractions are fine. A value that rounds to 0 ns, or is too large for a Rust `Duration` (`1e20`), is a usage error |
| `--parent-fd N` | none: only the socket tells the runner its peer is gone | A file descriptor the runner inherits open, from 0: the read end of a pipe whose write end the process that started the runner holds ([below](#ended-with-the-process-that-started-it)). Only with `--board`: `--parent-fd ends a run with a peer when the process that started it is gone: it needs --board`; another value, `--parent-fd: expected a file descriptor, a number from 0, got "x"`; a descriptor that is not open, `--parent-fd 9: not an open file descriptor: Bad file descriptor (os error 9)`; one that is not a pipe, a file or `/dev/null`, `--parent-fd 5: not a pipe; the process that starts the runner keeps the write end of one` (exit 2, before the connection) |
| `--console` | `stdout` | `stdout`; `socket` and `both` only with `--board` |
| `--console-port` | `uart0` | `uart0`, or `usb` on the ESP32-S3, ESP32-C3, ESP32-C6, ESP32-P4 and ESP32-S31: the console is the chip's USB-Serial/JTAG controller ([below](#a-console-on-usb-serialjtag)). On the classic ESP32, `--console-port usb: esp32 has no USB-Serial/JTAG controller` (exit 2); another value, `--console-port: expected uart0 or usb, got "uart1"` |
| `--sd-rw IMAGE` | none: an empty slot | a path, valid UTF-8, to an image file of a whole number of 512 KiB units, from 512 KiB to 2 TiB; only with `--sd-spi` |
| `--sd-spi sclk=N,mosi=N,miso=N,cs=N` | none: no slot | the four pins in any order, each once, four different GPIOs of the chip ([Chips](#chips)); `sclk`, `mosi` and `cs` not input-only (34 to 39 on the classic ESP32) |
| `--wifi SPEC` | none: no access point | esp32sim's `key=value,...`, every key optional, an empty `SPEC` included: `ssid` (default `esp32sim`), `psk` (also `password` or `pass`; WPA2-PSK with it, open without), `chan` (also `channel` or `ch`; 1 to 14, default 6), `bssid` (default `02:53:49:4d:00:01`). `epoch` is refused. Only with `--board` |
| `--wifi-nearby SPEC` | none | esp32sim's `key=value,...`: `ssid` (1 to 32 bytes, required), `chan` (1 to 14, default 1), `rssi` in dBm (-128 to 127, default -60), `secure` (`0` or `1`, also `false`/`true` or `no`/`yes`; default `1`), `bssid` (default: one derived from the SSID, the same every run). Repeatable, at most 32, each on a BSSID of its own and not the access point's SSID or BSSID. Only with `--wifi` |
| `--realtime` | off: the guest runs as fast as it can | Paces the run to wall time. Only with `--board`: `--realtime paces a run for the peer's real device: it needs --board` |
| `--flash-out PATH` | none: the flash stays in the runner | A file to write the whole flash to, created or replaced; its directory must exist. With or without `--board` |
| `--bt ELF` | none: the guest has no Bluetooth controller | The application's ELF, from the same build as its image, with the controller's symbols in it (`esp_bt_controller_init` and the others of esp32sim's `--ble`). Repeatable: after the application's, the ELF of each other image the guest may boot, such as one an OTA update writes ([below](#an-app-from-another-build-an-ota-update)). Only with `--board`: `--bt makes the peer the guest's Bluetooth controller: it needs --board` |
| `--no-bt ELF` | none: the guest's call to `esp_bt_controller_init` runs ESP-IDF's own controller | The application's ELF, from the same build as its image ([below](#a-guest-with-no-bluetooth-controller)). Only with `--board`: `--no-bt ends the run for the peer when the guest starts its Bluetooth controller: it needs --board`; not with `--bt`: `--bt with --no-bt: the guest's controller is the peer's, or there is none`; on the ESP32-P4 and the ESP32-S31, `--no-bt: esp32p4 has no Bluetooth in esp32sim` |

A flag given twice keeps its last value, except `--flash-at` and `--wifi-nearby`, which add one
each time. Flags
take their value as the next argument: `--flash-mb=8` is an unknown argument.

### Where the ROM comes from

Without `--rom`, the runner looks for the chip's ROM ELF ([Chips](#chips)) in `$ESP32SIM_ROM_DIR`, then in
`~/.espressif/tools/esp-rom-elfs/*/`, the last directory in name order first, which for the dated
release directories is the newest. That is esp32sim's own lookup. It does not prefer release
`20260528`, the one the project pins and `make setup` puts in `.cache/esp-rom-elfs/20260528/`, and
it does not read `$IDF_TOOLS_PATH`. Pass `--rom` when the result has to match a test run: the
backend always does.

## Connect a peer

The peer listens first and the runner connects once, at start: bind the socket, then start the
runner with `--board socket:PATH`. Keep `PATH` short: `sun_path` holds 104 bytes on macOS and
108 on Linux. `--board tcp:HOST:PORT` is for a runner in a Linux container, where a Unix socket
does not cross Docker Desktop's file sharing on macOS.

What goes over the socket, and the rules the peer has to keep, are in
[the link protocol](../protocol.md): the handshake, the turns, the messages and how a run ends. In
short, the runner sends `hello`, the peer answers with its own `hello` and the pins it is wired to
(`watch`), and the runner sends `sync` with `t = 0` before the guest's first instruction. From
then on the peer answers each `sync` with its inputs and one `wait`; between its turns it only
reads.

The bytes the peer sends to a UART's RX pin reach its receive FIFO at the line's rate, as on the
chip: a byte that finds the 128-byte FIFO full is lost, and the UART raises its overflow interrupt
(`UART_RXFIFO_OVF_INT`, which ESP-IDF's driver reports as `UART_FIFO_OVF`); nothing brings it
later. The runner tells the peer with an `rxfifo_ovf` warning and counts it in `stats`
([protocol, 5.8](../protocol.md#58-warn-ext)), as it does a byte lost to a baud that is not the
UART's (`frm_err`).

With `--console socket` the guest's console, UART0 unless `--console-port usb`, goes to the peer
as `console` messages and not to stdout; with `--console both` it goes to both. On the ESP32-P4
and the ESP32-S31 UART0 has no pins ([Chips](#chips)): the console comes and goes as on
USB-Serial/JTAG, below. Messages written
outside the peer's turn reach the socket within about 20 wall milliseconds, so a peer can read the
console as it comes.

### A console on USB-Serial/JTAG

With `--console-port usb` the console is the chip's USB-Serial/JTAG controller, where an ESP-IDF
firmware with `CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y`, or an Arduino-ESP32 3.x one for the ESP32-S3
with `ARDUINO_USB_CDC_ON_BOOT=1` and `ARDUINO_USB_MODE=1`, has `printf` and `Serial`:

- stdout gets what the guest writes to the controller, not UART0; with `--console socket` or
  `both`, so does the peer, as `console` messages, one per line, each stamped with the end of the
  slice the guest wrote it in, at most 1 ms late ([protocol 5.7](../protocol.md#57-console));
- the peer's `console_rx` messages are what a USB host types, given to the controller whole when
  they come due, with no line rate ([protocol 5.23](../protocol.md#523-console_rx-ext-usb-console));
  those that come due with the board's supply off are lost and counted in `stats` as
  `console_rx_lost`;
- UART0 is no console any more: its bytes go only to a peer that watches its TX pin, as
  `uart_tx`. The ROM prints its banner on both, so that peer sees the banner, and stdout too.

```sh
esp32tl-runner --chip esp32s3 --rom .cache/esp-rom-elfs/20260528/esp32s3_rev0_rom.elf \
  --flash-at 0x0=firmware/console_repl_usb/build-esp32s3/bootloader/bootloader.bin \
  --flash-at 0x8000=firmware/console_repl_usb/build-esp32s3/partition_table/partition-table.bin \
  --flash-at 0x10000=firmware/console_repl_usb/build-esp32s3/console_repl_usb.bin \
  --max-seconds 2 --console-port usb
```

prints the ROM's banner, the bootloader, the application and the REPL's `console_repl>` prompt;
the same command with `--console-port uart0` prints the ROM's 11 lines alone.

The peer can also switch the board's supply off and on (`power`) and pulse the chip's EN pin
(`chip_reset`), as a test does with `board.power_off()`, `board.power_on()` and `board.reset()`
([pytest plugin](pytest-plugin-user.md#resets-and-power-cycles)). The run goes on through both:
the chip comes back through a power-on reset, with what the firmware wrote to flash, and the
run's time does not restart. While the supply is off the guest runs nothing, and the run's time
still passes from one turn of the peer to the next. The flash is the runner's copy of the
`--flash-at` files: the files are read once, at the start, and never written.
[The protocol](../protocol.md#517-power-ext-105) says what a power cycle keeps and what it clears.

A chip in deep sleep runs nothing either, and its RTC goes on: the runner moves the RTC through the
sleep in long steps, up to just before the alarm of a timer wake, and the chip wakes at the alarm's
tick through a `reset` with cause 5. A 15-minute sleep takes a few microseconds of wall time on an
M2 Pro, so a day of 15-minute wakes takes about 7 s, nearly all of it in the boots. Under
`--realtime` the sleep follows wall time like the rest of the run
([protocol 6.1](../protocol.md#61-how-the-runner-meets-the-block-point)). A sleep with the RTC
watchdog on is not stepped through: it runs in esp32sim's idle rounds, at what they cost a sleep
without the steps (about 3.7 s of wall time for 15 minutes on an M2 Pro, measured with the
watchdog off), and the watchdog counts through it, so a stage that resets before the timer's
alarm ends the sleep in that reset
([decision](../decisions/runner.md#a-deep-sleep-runs-in-long-steps-of-the-rtc-to-just-short-of-its-alarm)).

The peer also learns when each sleep began and what the firmware armed to wake it, without asking:
on the classic ESP32 the first slice boundary that finds the chip in deep sleep sends `sleep`, with
that boundary and the one before it, a millisecond apart at most and a few nanoseconds more, as the
earliest and the latest time the sleep can have begun, and the wake sources the firmware armed by
ESP-IDF's names, `ext0`, `ext1`, `timer`, `touch` and `ulp`. A sleep that its wake ends within the
same slice is sent at the wake's `reset`, just before it. A sleep with the RTC watchdog on is sent
as well, and so is one under `--realtime`. The wake's `reset` gives the rest: for a timer wake, its
time less the sleep's latest is the interval the firmware armed, to within about a millisecond and a
half: ESP-IDF counts it from `esp_deep_sleep_start()` less a fixed 400 µs at 160 MHz, the sleep
begins up to 1 ms before `latest`, and the RTC's slow clock runs a little late on esp32sim. esp32sim
wakes the chip on the timer and on EXT0 only, so a sleep armed for EXT1, touch or the ULP alone
never wakes; the `sleep` still says what was armed. On the other chips the runner sends no `sleep`.
[The protocol](../protocol.md#526-sleep-ext-308) says what the runner reads.

The peer also learns where each boot's `esp_timer` counts zero on the run's clock, without asking:
the runner reads the counter `esp_timer_get_time()` reads at every slice boundary and sends
`uptime`, with the earliest and the latest time the zero can have been. On the classic ESP32 the two
are up to a millisecond apart, the slice in which ESP-IDF started the counter, some tens of
milliseconds after the boot's `reset`, and up to two after a load while the counter counts; on the
ESP32-S3, -C3 and -C6 they are 60 ns apart at most, and on the ESP32-P4 65 ns, around the boot's
`reset` itself. On the ESP32-S31 the runner sends no `uptime`: esp32sim keeps the rate of its
counter to itself. A stamp the
firmware took from its uptime then has a time on the run's clock. When the zero it sent no longer
holds, because the counter counts from a zero that may lie before power-on, it sends `uptime` with
neither. A counter that stops, as the classic ESP32's does in deep sleep, sends nothing: the zero
sent still dates every stamp taken before the stop, so the boot that slept keeps it. [The
protocol](../protocol.md#524-uptime-ext-245) says what the runner reads on each chip.

With `--flash-out PATH` what the firmware wrote can be read: the file holds the whole flash, as big
as `--flash-mb` says, once the run has ended, however it ended short of a panic. A peer can also ask
for it in a turn, with `flash_dump`, as often as it likes: the guest does not run, and the run's time
does not move. [The protocol](../protocol.md#521-flash_dump-and-flash_dump_ack-ext-131) has the rules;
the `esp32sim` backend's way in is [`board.flash()`](flash-contents-user.md#read-the-flash-of-a-run).

While the board's supply is off, the peer can write a file into the flash in its turn, with
`flash_write` and an offset, without `--flash-out`: the guest finds the bytes at its next boot, and
the run's time does not move. With the supply on the message is a protocol error, `flash_write with
the supply on`, and the run ends with exit code 3.
[The protocol](../protocol.md#525-flash_write-and-flash_write_ack-ext-278) has the rules; the
`esp32sim` backend's way in is [`board.nvs(...)` between two
boots](flash-contents-user.md#change-nvs-between-two-boots).

With `--realtime` the run follows wall time, for a peer with a real device at the far end of a
route: its `hello` says `clock: wall`, it waits for wall time at each block point before the peer's
turn, and its `stats` carries `rt_resyncs`, the lags it skipped. The times on the socket stay emulated
nanoseconds, and the run does not repeat, whatever the peer does.
[The protocol](../protocol.md#12-wall-time---realtime-and-the-real-source-m5) has the rules; the
`esp32sim` backend passes the flag when a test gives a route a
[real source](real-source-user.md).

`--peer-timeout` bounds, in wall time, how long the runner waits for the peer's turn to end, counted
again after each answer to `flash_dump` and `flash_write`, and
how long a write to the socket may make no progress because the peer does not read; for `tcp:` it
also bounds each connection attempt, though not the name lookup before it. A peer that hangs then
fails the run with exit code 4 instead of hanging a CI job.

## Put an SD card in

`--sd-rw IMAGE --sd-spi sclk=N,mosi=N,miso=N,cs=N` wires a card holding the image file to those
GPIOs: an SD memory card in SPI mode, as a driver such as ESP-IDF's `sdspi` talks to it. For the
`sdspi` validation image, which takes ESP-IDF's default pins for the ESP32, with a FAT image made
by mtools:

```sh
truncate -s 32m card.img
MTOOLS_SKIP_CHECK=1 mformat -i card.img -T 65536 -h 64 -s 32 ::
B=firmware/sdspi/build
runner/target/release/esp32tl-runner --chip esp32 \
  --rom .cache/esp-rom-elfs/20260528/esp32_rev300_rom.elf \
  --flash-at 0x1000=$B/bootloader/bootloader.bin \
  --flash-at 0x8000=$B/partition_table/partition-table.bin \
  --flash-at 0x10000=$B/sd_card.bin \
  --sd-spi sclk=14,mosi=15,miso=2,cs=13 --sd-rw card.img \
  --max-seconds 5
MTOOLS_SKIP_CHECK=1 mtype -i card.img ::foo.txt
```

- The card writes through: every block the guest writes is in the file when the write's data
  response goes back, and stays there after the run. Copy the image first to keep it as it was.
- The card's size is the image's. It is a high capacity card, SDHC up to 32 GiB and SDXC above:
  block addresses, 512-byte blocks, default speed. Its CID names it `SIMSD`, by OEM `ES`, so
  ESP-IDF's `sdmmc_card_print_info` prints `Name: SIMSD`.
- The card hears a transaction only while its CS pin is a low output and its SCLK pin carries the
  master's clock; the guest reads its answers only when the master's MISO comes from the card's
  pin. A guest that routes the bus to other pins finds no card, as it would on the board.
- The card stays through chip resets: it keeps its state, as a powered card does, until the guest
  sends CMD0.
- `--sd-spi` without `--sd-rw` is an empty slot: every byte the guest reads from it is `0xff`, as
  from a slot with nothing in it, and ESP-IDF's driver times out (`ESP_ERR_TIMEOUT`).
- With `--board`, the peer takes the card out and puts one in with `sd_card`, and the runner says
  what the slot holds after each with `sd_card_ack`
  ([protocol 5.19](../protocol.md#519-sd_card-and-sd_card_ack-ext-106)). The change comes between
  two of the guest's SPI transactions. A card taken out keeps in its image every block it took,
  and the image is closed; the guest reads `0xff` from then on. A card put in starts unpowered, in
  SD mode, and the guest has to initialise it again. It can change with the board's supply off
  as well. Without `--board` the card stays in from power-on to the end of the run.
- With `--board`, the peer can make the card fail reads and writes, or take longer over reads,
  writes and erases, or only watch them, from an instant it chooses, on chosen blocks, for so many
  operations or until it clears the rule, and hears back what each rule did
  ([protocol 5.20](../protocol.md#520-sd_fault-sd_clear-and-sd_op-ext-107)). Delays are on the
  emulated clock: the guest polls the card for them as it would a slow card, and its own timeouts
  run on the same clock. A rule that only watches changes nothing the guest sees. Without `--board`
  the card always answers at once. The harness's way to give the rules is `sd_card.fail`,
  `sd_card.delay` and `sd_card.observe`
  ([pytest plugin](pytest-plugin-user.md#making-the-card-fail-or-slow-down)).

What the card does and does not model is in
[the technical side](runner-technical.md#the-sd-card).

## Put a Wi-Fi access point on the air

`--wifi SPEC` puts esp32sim's virtual access point on the air for the chip's Wi-Fi station, from
power-on, and makes the peer the network behind it. It needs `--board`: esp32sim's own network
behind the access point (its DHCP server, DNS, NTP and NAT to the host) is not used, and esp32sim's
`--net` flag is not taken. For the `http_request` validation image, which joins the SSID `myssid`
with the passphrase `mypassword`, ESP-IDF's defaults for its examples:

```sh
B=firmware/http_request/build
runner/target/release/esp32tl-runner --chip esp32 \
  --rom .cache/esp-rom-elfs/20260528/esp32_rev300_rom.elf \
  --flash-at 0x1000=$B/bootloader/bootloader.bin \
  --flash-at 0x8000=$B/partition_table/partition-table.bin \
  --flash-at 0x10000=$B/http_request.bin \
  --board socket:/tmp/peer.sock \
  --wifi ssid=myssid,psk=mypassword \
  --wifi-nearby ssid=cafe,chan=3,rssi=-70,secure=0 \
  --max-seconds 10
```

- The station sees what esp32sim's access point does: it answers scans, takes authentication and
  association, runs the WPA2 handshake when `psk` is set, and beacons on its channel. A station
  with another passphrase does not finish the handshake: ESP-IDF's example prints
  a log line with `Wi-Fi disconnected` in it, and gets no address.
- Each `--wifi-nearby` adds a network around the station: a scan lists it, with its channel, signal
  and security, and nothing can join it.
- Once the station has joined, every Ethernet frame it sends goes to the peer as an `eth_tx`
  message, and every `eth_rx` the peer sends reaches the station as a frame from the network: the
  peer answers DHCP, ARP and whatever the firmware sends after that
  ([protocol, 5.15](../protocol.md#515-eth_tx-and-eth_rx-ext-m7)). A peer that answers nothing
  leaves the station joined but without an address.
- The peer can take the access point off the air and put it back with `wifi_ap`
  ([protocol, 5.16](../protocol.md#516-wifi_ap-ext-m7)). Off, the access point deauthenticates the
  station, as one that shuts down does, and the networks around go with it; back on, it is a fresh
  access point, and the station has to join it again.
- The final `stats` has a `wifi` object with the frames each way and the drops
  ([protocol, 5.9](../protocol.md#59-stats-ext)).

The harness does all of this for a test through `board.wifi`; running it by hand needs a peer that
speaks the three messages.

The command line refuses, with exit code 2:

- `--wifi puts the peer's network behind the access point: it needs --board`, for `--wifi` without
  `--board`;
- `--wifi-nearby needs the access point of --wifi`;
- `--wifi: epoch sets esp32sim's own network's clock; the network here is the peer's`, for `epoch=`
  in `--wifi`;
- `--wifi: MESSAGE` and `--wifi-nearby: MESSAGE`, with esp32sim's own message for a spec it
  refuses, such as `--wifi: invalid WiFi channel '15': expected 1 through 14` or
  `--wifi-nearby: a nearby network needs an ssid of 1 to 32 bytes`;
- `--wifi-nearby ssid=SSID: a network around with the access point's SSID would be the access
  point` (or `BSSID`), where esp32sim's own command line would leave that network out with a line
  on stderr.

## Give the guest a Bluetooth controller

`--bt ELF` makes the peer the guest's Bluetooth controller. esp32sim installs its VHCI hooks over
the controller functions of the application's ELF, as its own `--ble` does, and the runner puts
itself where esp32sim's virtual BLE controller would be: each H4 packet the guest's Bluetooth host
sends goes to the peer as an `hci_tx` message, at the time the host sent it, and each `hci_rx` the
peer sends reaches the host as a packet from its controller
([protocol, 5.22](../protocol.md#522-hci_tx-and-hci_rx-ext-34)). esp32sim's own controller, its
`--ble-nearby` devices and its script's `ble` commands are not there.

For an ESP-IDF project `app` built in `build/`, the ELF from the same build as the image:

```sh
runner/target/release/esp32tl-runner --chip esp32 \
  --rom .cache/esp-rom-elfs/20260528/esp32_rev300_rom.elf \
  --flash-at 0x1000=build/bootloader/bootloader.bin \
  --flash-at 0x8000=build/partition_table/partition-table.bin \
  --flash-at 0x10000=build/app.bin \
  --board socket:/tmp/peer.sock \
  --bt build/app.elf \
  --max-seconds 20
```

The validation image `bt_spp_initiator`, ESP-IDF's example unmodified, runs this way under the
harness's `board.bluetooth` ([validation firmware](validation-firmware-user.md#bt_spp_initiator)).

What the guest sees:

- `esp_bt_controller_init`, `esp_bt_controller_enable` and the VHCI functions answer as esp32sim's
  `--ble` makes them answer. `esp_bt_controller_enable` takes the mode the config given to
  `esp_bt_controller_init` names, and no other, as ESP-IDF does: mode 1 (BLE), 2 (Classic alone,
  `CONFIG_BTDM_CTRL_MODE_BR_EDR_ONLY`) or 3 (dual mode). Another mode gets `ESP_ERR_INVALID_ARG`.
- At an enable in mode 2 esp32sim prints `[ble] the virtual controller is LE-only: enabled in mode
  2 (BR/EDR), its commands get Unknown HCI Command` on stderr. With `--bt` it is not so: the
  controller is the peer, and the runner cannot keep esp32sim's line out of its stderr.
- The host reads the peer's packets at its controller task's VHCI polls, not at the instant the
  peer stamped them: at most one packet a FreeRTOS tick, 10 ms at ESP-IDF's default
  `CONFIG_FREERTOS_HZ=100`, while packets wait.
- A packet is 1 to 496 bytes either way, what esp32sim's VHCI buffer carries. A peer's controller
  that sizes its ACL buffer to 491 bytes lets the host fill each packet.
- A chip reset or a power-on reset loses the packets the host has not read yet.

The harness's side is `board.bluetooth` (the `bt` fixture), which passes `--bt` and puts the
guest's controller on the air of the test's Bluetooth devices
([Bluetooth peer](bluetooth-peer-user.md#in-front-of-the-firmware-boardbluetooth)); running the
runner by hand needs a peer that speaks the two messages and is a controller.

The command line refuses `--bt` without `--board`, with exit code 2. An ELF esp32sim refuses, one
that is not an ELF or lacks a symbol the hooks need, ends the run before it starts with exit code
2 and esp32sim's message after the path: `esp32tl-runner: --bt ELF: BLE requires ELF symbol
esp_bt_controller_init`. One that cannot be read does too, as `esp32tl-runner: ELF: ERROR`. esp32sim
checks the rest when the firmware calls `esp_bt_controller_init`: a failure then is a `[ble] …`
line on stderr, the call fails, and the run goes on without a controller.

With `--flash-out`, the file holds the flash as the firmware has it, although esp32sim has written
its controller task's code over `esp_bt_controller_init` in the emulated flash: the runner puts
the function's own code back in the copy it writes.

The firmware itself does not see that copy: from its call to `esp_bt_controller_init` until the
next reset, a read of its own image, through `esp_partition_read` or `esp_image_verify`, finds
esp32sim's code there, and `esp_image_verify` fails with `ESP_ERR_IMAGE_INVALID`. A firmware that
copies or checks its running image does it before it starts its controller, as
[`bt_spp_ota`](validation-firmware-user.md#bt_spp_ota-and-bt_spp_ota_v2) does.

### An app from another build: an OTA update

esp32sim's hooks are at the addresses of one ELF. An app that the bootloader starts from another
build, one an OTA update wrote into the other app partition, has its functions elsewhere, and the
hooks of the build's ELF would patch the middle of other functions: the firmware then panics, or
its controller task fails as `[ble] controller task creation failed: 0x3ffea240`. Give the ELF of
every image the guest may boot, the application's first:

```sh
  --bt build/app.elf --bt update/build/app.elf
```

At every slice boundary the runner reads the `esp_app_desc_t` where the ELFs have `esp_app_desc`,
at the start of the app's DROM, and takes its `app_elf_sha256`, the SHA-256 of the ELF the image
was built from, which ESP-IDF writes into every image. When it changes, the hooks become those of
the ELF of that SHA-256; the forwarder stays the guest's controller. Until the bootloader has
mapped an app the hooks are the application's. The first app the bootloader starts is taken for
the application's own image: when its SHA-256 is none of the `--bt` ELFs', the application's ELF
is not byte for byte the file its image was built from (built again since, or processed after),
and its hooks stay, as a single `--bt` always kept them, with a line on stderr (below); an app of
the ELF's own SHA-256 still gets them. A later
app the bootloader started from an image whose ELF none of the `--bt` names gets no hooks, and the runner says so on stderr (below); its
`esp_bt_controller_init` then runs ESP-IDF's own controller, which on the classic ESP32 panics
with `StoreProhibited`. An image whose `app_elf_sha256` is all zeros, one built without
`elf2image --elf-sha256-offset`, keeps the hooks it has. The same image in another slot is the
same ELF and needs nothing more: an app booted from `ota_1` gets its controller at a power-on, a
software reset and a deep-sleep wake as from `ota_0`. The bootloader maps at the same address
each image it checks, so between a reset and its handing over the hooks, and the lines on stderr,
may follow an image it checks and then does not start, as after a failed check or a rollback,
before they follow the one it starts.

Every `--bt` ELF is checked before the run: one that esp32sim refuses ends it with exit code 2,
as for the application's, `esp32tl-runner: --bt ELF: BLE requires ELF symbol
esp_bt_controller_init`.

## A guest with no Bluetooth controller

Without `--bt`, esp32sim installs no VHCI hooks, and a firmware that starts its controller runs
ESP-IDF's own against Bluetooth hardware esp32sim does not model. On the classic ESP32,
`bt_spp_initiator` crashes there and boot-loops: it prints `BTDM_INIT: Using main XTAL as clock
source`, then `Guru Meditation Error: Core  0 panic'ed (StoreProhibited)` with `EXCVADDR:
0x0000019c`, 129 boots in 15 emulated seconds. On the ESP32-S3 and the ESP32-C3, `gatt_server`
prints nothing after `BLE_INIT: Bluetooth MAC: …`, and the run takes about 2.4 wall seconds for
each emulated one. A peer that does not watch for either sees nothing wrong.

`--no-bt ELF` gives the peer a reason instead. The runner puts an esp32sim stub at the address of
`esp_bt_controller_init` in the application's ELF, ESP-IDF's entry to its controller on every chip
the runner gives Bluetooth to; the stub returns `ESP_ERR_NOT_SUPPORTED` (`0x106`) at once, without
running the function. When the slice the call came in ends, at the next whole emulated millisecond
or sooner ([protocol, 6.1](../protocol.md#61-how-the-runner-meets-the-block-point)), the runner ends
the run: `stats` with `end: bt_controller` ([protocol, section 9](../protocol.md#9-ending-a-run)),
then

```text
esp32tl-runner: end: the guest called esp_bt_controller_init, and the run has no --bt: it has no Bluetooth controller at 0.117000 s (0.1 s wall, 0 resets, 1 turns)
```

and exit code 5. What the guest did in the rest of that slice, as `bt_spp_initiator`'s
`app_main initialize controller failed: ESP_ERR_NOT_SUPPORTED`, still reaches the peer. The same
holds on the ESP32-S3 and the ESP32-C3, where `gatt_server` ends the run at 0.033 s; no image here
starts the controller on the ESP32-C6, whose esp32sim hooks want ESP-IDF v5.5.x (#229, G5), but the
runner takes `--no-bt` there as well. A guest that never calls the function runs as it would
without the flag, and an ELF without the symbol gets no stub and the line `esp32tl-runner: --no-bt:
ELF has no esp_bt_controller_init, nothing to stop`.

A stub ends a block at every address of its bucket of esp32sim's 64-bit bloom of hooked PCs: on
`bt_spp_initiator`'s boot loop without `--bt`, a stub at an address the guest never runs took the
median of six free runs of 15 emulated s from 13.95 to 14.85 wall seconds on an M2 Pro. A guest
that calls the function pays it until the call.

The `esp32sim` backend passes `--no-bt` with the application's ELF whenever the test has not
looked at `board.bluetooth`, on every chip but the ESP32-P4 and the ESP32-S31 ([esp32sim
backend](esp32sim-backend-user.md#a-firmware-that-starts-its-controller-without-boardbluetooth)).

## What it prints

stdout carries the guest's console output, UART0's or with `--console-port usb` the
USB-Serial/JTAG controller's, and nothing else, unless `--console socket` sends it to the peer
instead. Everything the runner says goes to stderr, on lines that start with
`esp32tl-runner: `:

- `esp32tl-runner: SD card on SCLK GPIO N, MOSI GPIO N, MISO GPIO N, CS GPIO N: IMAGE (K KiB,
  write-through)` first, with `--sd-rw`, or
  `esp32tl-runner: SD card slot on SCLK GPIO N, MOSI GPIO N, MISO GPIO N, CS GPIO N: empty` with
  `--sd-spi` alone;
- `esp32tl-runner: Wi-Fi access point 'SSID' bssid BSSID channel N (WPA2-PSK), its network on the
  socket` with `--wifi`, `(open)` without `psk`; then, for each `--wifi-nearby`,
  `esp32tl-runner: Wi-Fi network around: 'SSID' bssid BSSID channel N RSSI dBm (open)`, or
  `(WPA2-PSK)` for one that is `secure`. For the example above:

  ```text
  esp32tl-runner: Wi-Fi access point 'myssid' bssid 02:53:49:4d:00:01 channel 6 (WPA2-PSK), its network on the socket
  esp32tl-runner: Wi-Fi network around: 'cafe' bssid 02:28:90:58:e8:90 channel 3 -70 dBm (open)
  ```

- `esp32tl-runner: ROM PATH` once the ROM ELF is loaded;
- `esp32tl-runner: flash 0x1000: PATH (N bytes)` for each `--flash-at` file, offset in hex;
- `esp32tl-runner: Bluetooth controller on the socket, VHCI hooks from ELF` with `--bt`, once for
  each `--bt` ELF, once they are loaded and checked;
- `esp32tl-runner: Bluetooth controller hooks from ELF, the app the bootloader started` with `--bt`,
  each time the app the bootloader started is another `--bt` ELF's than the one whose hooks were in
  place ([above](#an-app-from-another-build-an-ota-update));
- `esp32tl-runner: the app the bootloader started was built from an ELF of SHA-256 HEX, none of
  the --bt ELFs: it has no Bluetooth controller; pass its ELF as another --bt` with `--bt`, HEX the
  image's `app_elf_sha256`, when it is none of their ELFs;
- `esp32tl-runner: --bt ELF: the app the bootloader started first was built from an ELF of
  SHA-256 HEX, not this file's HEX: its hooks stay over it, as the application's` with `--bt`,
  ELF the application's, when the first app the bootloader starts records another SHA-256 than
  the file and than every `--bt` ELF;
- `esp32tl-runner: no Bluetooth controller: the guest's call to esp_bt_controller_init (ADDRESS,
  from ELF) ends the run` with `--no-bt`, ADDRESS in hex, or `esp32tl-runner: --no-bt: ELF has no
  esp_bt_controller_init, nothing to stop` for an ELF without it;
- `esp32tl-runner: chip reset at T s, cause CAUSE` each time the firmware resets the chip, as
  `esp_restart()` does: T in emulated seconds with six decimals, CAUSE the reset cause the ROM
  reports, in hex (`0x...`). The runner restarts the guest from the ROM, as the chip would, and
  the run goes on. With `--board`, the peer gets a `reset` message as well. The same line, with
  cause `0x1`, follows the peer's `chip_reset` and its `power` that switches the supply back on;
- `esp32tl-runner: power off at T s` when the peer switches the board's supply off.
  the run goes on. With `--board`, the peer gets a `reset` message as well.
- `esp32tl-runner: cannot write the flash: PATH: ERROR` when the end of the run cannot write the
  file of `--flash-out`; the exit code stays what the end of the run says.
- `esp32tl-runner: SD card out at T s: IMAGE`, `esp32tl-runner: SD card in at T s: IMAGE (K KiB,
  write-through)` and `esp32tl-runner: SD card refused at T s: IMAGE: ERROR` when the peer's
  `sd_card` takes the card out, puts one in, or puts in one the slot cannot take; T in emulated
  seconds with six decimals.
- `esp32tl-runner: SD card: N commands, R blocks read, W blocks written` at the end of a run with
  `--sd-spi`, just before the line that ends it, counting every card the slot held; with
  `, E image errors` added when reads, writes or erases of an image file failed, `, F operations
  faulted` when the peer's fault rules failed or delayed any, `, B operations observed` when its
  rules that only watch met any, and
  `; while it ran, cards put in: I, taken out: O` when the peer changed the card. Each such failure also prints
  `esp32tl-runner: SD card: reading block N: ERROR` (or `writing block N`, or
  `erasing blocks LO to HI`) when it happens, and the guest gets the error a card reports for it.

esp32sim can print lines of its own on stderr; it does with its debug switches set
([technical side](runner-technical.md#configuration)).

## How a run ends

Without `--board`, a run that gets past loading and that the emulator ends, not a signal, ends
with one line on stderr:

```text
esp32tl-runner: stop: VARIANT at T s (W s wall, N resets)
```

`VARIANT` is the name of the esp32sim `Stop` that ended it, T the emulated seconds (six decimals),
W the wall-clock seconds the run took after loading (one decimal), N the number of chip resets.

With `--board`, every run that gets past loading ends with one line naming its cause instead,
whatever ended it but a second signal:

```text
esp32tl-runner: end: CAUSE at T s (W s wall, N resets, K turns)
```

W counts from the connection, K is the number of turns the peer had. A run that cannot connect
prints `esp32tl-runner: end: cannot connect to the peer at BOARD: ERROR`, with `BOARD` as given
to `--board`, and nothing after it.

| Exit code | Meaning | What stderr shows last |
|---|---|---|
| 0 | the run reached `--max-seconds`: `stop: Halted`, the normal end | the stop line, or `end: max_seconds at ...` |
| 0 | with `--board`: the peer sent `stop` | `end: stop from the peer at ...` |
| 0 | with `--board`: `SIGTERM` or `SIGINT` | `end: signal at ...` |
| 1 | the emulator stopped for any other reason | the stop line, then `esp32tl-runner: the emulator stopped before --max-seconds: STOP`, the stop with its fields; with `--board`, `end: the emulator stopped before --max-seconds: STOP at ...` |
| 2 | a usage error | `esp32tl-runner: MESSAGE`, then `run esp32tl-runner --help for the usage` |
| 2 | an input that cannot be loaded: no ROM found, a file that cannot be read, a ROM that is not an ELF, an image that does not fit in flash, a `--bt` ELF that is not an ELF or lacks the controller's symbols, a `--no-bt` ELF that is not an ELF, an SD card image that cannot be opened for writing or is not a whole number of 512 KiB units | `esp32tl-runner: MESSAGE`, no stop line |
| 3 | with `--board`: a protocol error by the peer, such as a malformed line, a version or chip mismatch, an input stamped before its turn's `sync`, an `eth_rx` without `--wifi` or `--wifi-sta`, or `wifi_ap` without `--wifi` (including station mode), an `hci_rx` on a run without `--bt` or outside 1 to 496 bytes, an `sd_card` on a run without `--sd-spi` | `end: protocol error by the peer at BOARD: WHAT: LINE at ...`, `LINE` cut after 200 characters |
| 4 | with `--board`: the connection failed, or the peer is gone: it closed the socket, a read or write failed, `--peer-timeout` ran out, or `--parent-fd` reached end of file | `end: cannot connect to the peer at ...`, or `end: the peer at BOARD is gone: WHY at ...` |
| 5 | with `--no-bt`: the guest called `esp_bt_controller_init` | `end: the guest called esp_bt_controller_init, and the run has no --bt: it has no Bluetooth controller at ...` |
| 101 | a panic, in the runner or in esp32sim during the run | Rust's panic message, no stop line |
| 128 + N | with `--board`: a second `SIGTERM` or `SIGINT` (signal number N) | whatever came before it |

The variants other than `Halted` are `MaxInsns`, `Breakpoint`, `Unimplemented`, `SwReset`,
`Simcall`, `Ebreak`, `Watch`, `Exceptions`, `CostModel` and `CostModelLifecycle`. `SwReset` is
handled as a chip reset and never ends a run. With `--board`, the peer also gets a final `stats`
message for every end but a vanished peer and a second signal
([protocol, section 9](../protocol.md#9-ending-a-run)). Every message, with its cause, is in
[Failure modes](runner-technical.md#failure-modes).

### Ended by a signal

Without `--board`, the runner handles no signal. `SIGINT` (Ctrl-C) or `SIGTERM` ends it at once,
wherever the run is, without the stop line and with none of the exit codes above: the process dies
of the signal, so a shell reports 128 plus its number (130 for `SIGINT`, 143 for `SIGTERM`) and
Python's `Popen` its negative.

With `--board`, once the runner has connected, `SIGTERM` or `SIGINT` ends the run at the end of
the slice in progress, at most one emulated millisecond, or the rest of a deep sleep, which takes
a few wall microseconds, or at once if the runner is waiting for the peer. The peer gets the
events not yet sent and `stats` with `end: signal`, and the runner exits 0. Those last messages
get two wall seconds; a peer that does not read them by then is given up on, and the exit code
stays 0. A second `SIGTERM` or `SIGINT` ends the process at once, with 128 plus the signal's
number. Before the connection is made, a signal ends the process as it does
without `--board`. A test run usually ends otherwise: the backend starts the runner with
`--max-seconds 86400 --console both --peer-timeout 0` at `board.power_on()`, holds it in a turn
whenever the test is not waiting, and at teardown answers that turn with `stop`. A runner in no
held turn then, or still running 5 s later, gets one `SIGTERM` once it has said hello, then
`SIGKILL` 5 s after that ([esp32sim backend](esp32sim-backend-user.md#how-the-run-ends)).

### Ended with the process that started it

The runner finds a peer gone at its next read or write on the socket, and a peer that gives it a
long window to run in (a test's `board.wait(seconds(3600))`, on a guest that does little) may leave
it running at full speed, alone, for that long after the peer's process died. `--parent-fd N`
ends such a run at once: the process that starts the runner makes a pipe, keeps its write end and
lets the runner inherit its read end as descriptor `N`. The runner reads it on a thread of its own
and ignores what comes; at end of file, which comes once every write end is closed, as the kernel
does for a process that ends however it ends (`SIGKILL` included), the run ends as with a
signal, at the end of the slice in progress, but as the peer gone: no `stats`, the line

```text
esp32tl-runner: end: the peer at BOARD is gone: end of file on --parent-fd: the process that started the runner has ended at T s (W s wall, N resets, K turns)
```

and exit code 4. With `--flash-out` the flash is written first, as for any end. The runner ends
within what a signal takes, a slice of at most one emulated millisecond, a few wall milliseconds,
or one 20 ms poll of the socket when it is waiting for the peer, plus the write of the flash:
`tests/test_killed_session.py` holds it, on macOS and on Linux, to 5 wall seconds after
`SIGKILL`. On an M2 Pro (macOS 26), in a long wait on `console_repl` at its prompt, it was gone
0.16 wall seconds after its parent's `SIGKILL`; without `--parent-fd` it ran on at 99 % of a core.
Whoever starts the runner must keep the write end out of every other process: one that holds a
copy, a child it forks, keeps the runner running. Python's `os.pipe` makes both ends
non-inheritable, so `subprocess` passes only the read end, with `pass_fds`; the
[esp32sim backend](esp32sim-backend-user.md#how-the-run-ends) does so.

## Chips

`--chip` selects one of esp32sim's machines. Everything above works on each of them, except what
the last column lists: esp32sim does not model it on that chip. Where a flag asks for it
(`--wifi-sta`, a PSRAM size, and on the P4 and the S31 `--wifi`, `--bt` and `--sd-spi`), the
runner refuses the flag with a usage error (exit 2), for example
`--sd-spi: esp32p4 tells the board no SPI pins in esp32sim`; deep sleep
and DMA are the guest's to use, and no flag asks for them. [Issue #229](https://github.com/mateuszsikora/esp32-testing-library/issues/229)
tracks each gap.

| `--chip` | esp32sim's machine | Bus clock | GPIOs | UARTs | ROM ELF | Not modelled |
|---|---|---|---|---|---|---|
| `esp32` | `esp32` (two LX6 cores) | 240 MHz | 0 to 39 but 24 and 28 to 31; 34 to 39 input only | 3 | `esp32_rev300_rom.elf` | |
| `esp32s3` | `esp32s3` (two LX7 cores) | 240 MHz | 0 to 21 and 26 to 48 | 3 | `esp32s3_rev0_rom.elf` | deep sleep and its timer wake; GP-SPI RX DMA, so an SD card over ESP-IDF's SPI driver; `--wifi-sta` |
| `esp32c3` | `esp32c3` (one RISC-V core) | 160 MHz | 0 to 21 | 2 | `esp32c3_rev3_rom.elf` | as the S3, and PSRAM |
| `esp32c6` | `esp32c6` (one RISC-V core) | 160 MHz | 0 to 30 | 2 | `esp32c6_rev0_rom.elf` | as the C3; `--bt` needs an image built with ESP-IDF v5.5.x, which esp32sim checks |
| `esp32p4` | `esp32p4`, rev < 3 or rev ≥ 3 as the images are built (two RISC-V cores) | 360 MHz | 0 to 54 | 2 | `esp32p4_rev300_rom.elf` for rev ≥ 3, `esp32p4_rev0_rom.elf` for rev < 3 | deep sleep; the pins of every UART, so no UART route, and UART0's console without its pins; `--sd-spi`; `--wifi`, `--wifi-sta` and `--bt`: the chip has no radio |
| `esp32s31` | `esp32s31` (two RISC-V cores) | 320 MHz | 0 to 61 but 29 and 41 | 1 (UART0) | `esp32s31_rev0_rom.elf` | as the P4, with no Wi-Fi relay and no Bluetooth in esp32sim |

- **The SD card.** `--sd-spi` and `--sd-rw` are taken on every chip: the card answers the SPI
  transactions the CPU drives through the data buffer, as the first commands of ESP-IDF's `sdspi`
  example reached it on the S3 (`SD card: 2 commands`). What a transaction receives through RX
  DMA never reaches memory, and ESP-IDF's SD SPI driver reads through DMA, so its card
  initialisation times out (`sdmmc_card_init failed (0x107)`). A driver that moves every byte
  through the CPU would reach the card; none is tested here. The esp32sim backend skips a test's
  `sd_card` on these chips ([esp32sim backend](esp32sim-backend-user.md#chips-other-than-the-classic-esp32)).
- **Deep sleep.** On the S3, C3, C6, P4 and S31, `esp_deep_sleep_start()` puts the guest to sleep
  and nothing wakes it: the run goes on to its end without another boot, and the runner sends no
  `sleep` (protocol 5.26). On the P4 and the S31,
  `firmware/reset_reasons` with a 100 ms timer wake had no other boot in the 3 s after its sleep,
  measured in a run without a peer at the esp32sim pin `357b0e5`.
- **The console's input.** esp32sim starts every chip with its IO_MUX zeroed, which leaves U0RXD
  without its input; the runner sets the pad's reset state at power-on and after every reset
  (GPIO 3 on the classic ESP32, 44 on the S3, 20 on the C3, 17 on the C6), and what the firmware
  writes there afterwards stays.
- **UART0 without pins on the P4 and the S31.** esp32sim tells the board nothing these chips'
  UARTs send or receive, so no UART has a route the peer can watch: `uart_cfg` never comes, and a
  `uart_rx` finds no UART. With `--console-port uart0`, the default, the console is what the guest
  wrote to UART0, on stdout as on the other chips and, with `--console socket` or `both`, as
  `console` messages stamped with the end of the slice it was written in, at most 1 ms late. The
  peer's `console_rx` is UART0's input: on the P4 what fits into its 128-byte RX FIFO goes in
  when the message comes due and the rest at UART0's baud rate, on the S31 one byte per character
  time at its baud (115200 until the guest sets one) as long as the FIFO has room and UART0 takes
  it: while its clock or its receiver is off the byte waits. None of it is dropped. Bytes that come due with the board's supply off, or that wait in the S31's queue at
  a reset, are lost; the first are counted in `stats` as `console_rx_lost`.
- **The P4's revision.** ESP-IDF v5.5 builds for ESP32-P4 rev ≥ 3 unless
  `CONFIG_ESP32P4_SELECTS_REV_LESS_V3=y`, and esp32sim models both revisions, which do not run
  each other's images. The runner reads the image header at the bootloader's flash address,
  0x2000, from the last `--flash-at` file that covers it (a bootloader of its own, or an image of
  the whole flash from 0x0), or else the first other file that starts with a P4 image header, and
  runs the revision it asks for; it says which on stderr:
  `esp32tl-runner: ESP32-P4 rev ≥ 3 (v3.1), as the images are built` or
  `esp32tl-runner: ESP32-P4 rev < 3 (v1.0), as the images are built`. An image for the other
  revision is refused with esp32sim's message, exit 2, for example
  `--flash-at 0x10000=hello_world.bin: was built for ESP32-P4 rev < v3.0 (max_chip_rev_full=199); this model is rev ≥ 3 (v3.1). Rebuild without CONFIG_ESP32P4_SELECTS_REV_LESS_V3, or run a bootloader and app both built for rev < 3.`
  Pass the ROM of that revision with `--rom`: with the other one the guest does not boot.
- **GPIO on the P4 and the S31.** The peer sees every change of a pin the guest drives at its
  cycle, as on the other chips. A pin made an output at the level it already had, and on the S31
  a pin's output enable, are reported at the end of the slice they changed in.
- **USB-Serial/JTAG.** The S3, C3, C6, P4 and S31 have the controller, and `--console-port usb`
  reads and writes it; the classic ESP32 has none. On each of the S3, C3 and C6 the ROM prints
  its banner on UART0 and on the controller alike.
- **Reset causes** are the chip's numbers, as its ROM prints them: 1 is a power-on reset, 5 a
  wake from deep sleep, 12 a software CPU reset (`esp_restart()`), on every chip.
- **The reset-reason hint.** ESP-IDF's panic handler and its watchdogs leave the reason of the
  software reset they make in an RTC register the chip keeps through the reset,
  `RTC_RESET_CAUSE_REG`, where the next boot's `esp_reset_reason()` reads it: `ESP_RST_PANIC`
  rather than `ESP_RST_SW` after a panic, whose ROM cause, 12, is `esp_restart()`'s. esp32sim
  keeps the register on the classic ESP32, the S3, the C6 and the P4, and zeroes it at a reset
  on the C3 and the S31; there the runner writes it back, so that the firmware reads what it
  would on the chip. The peer gets it in every `reset` message, as `hint`
  ([protocol 5.5](../protocol.md#55-reset)).
- **Timer-group watchdogs on the classic ESP32.** esp32sim's timer groups have no watchdog
  (`esp-periph/src/timg.rs`): ESP-IDF's task watchdog and interrupt watchdog never fire, and a
  firmware that starves either runs on. The RTC watchdog resets the chip, with causes 9, 13 and 16
  ([protocol 5.5](../protocol.md#55-reset)).
- **The flash dump with `--bt` on the C6** holds esp32sim's trampoline over
  `esp_bt_controller_init`, which the other chips' dumps do not: esp32sim keeps the C6's copy of
  the code under it to itself.
- **Images for the P4 and the S31** need ESP-IDF v5.5 or later for the P4 and v6.1 for the S31
  (`idf.py --preview set-target esp32s31`): the validation images are built with v5.5.4 and v6.1
  ([validation firmware](validation-firmware-user.md)).

## See also

- [runner-technical.md](runner-technical.md): how it works, every message, the pins and profiles.
- [Link protocol](../protocol.md): what goes over the socket, for whoever writes a peer.
- [esp32sim-backend-user.md](esp32sim-backend-user.md): how tests start the runner, and
  `ESP32TL_RUNNER` / `ESP32TL_ROM` to use another build.
- [Stage 0 findings, Speed](../stage0-findings.md#speed): how emulated time relates to wall time.

## Join a guest access point

`--wifi-sta ssid=NAME[,psk=PASSPHRASE][,mac=xx:xx:xx:xx:xx:xx]` attaches esp32sim's virtual
station, with the harness as its Ethernet host. `--board` is required. `mac` defaults to
`02:53:54:41:00:01`; omit `psk` for an open AP. The start-up line is:

```text
esp32tl-runner: virtual station for 'SSID' (MAC), its network on the socket
```

The runner parses the spec with `StaConfig::parse` and prefixes its errors with `--wifi-sta: `.
It refuses `--wifi-sta` twice, alongside `--wifi` (a virtual AP and station together are not
modelled), or alongside `--wifi-nearby` (those networks need a virtual AP). `eth_tx`, `eth_rx`
and `stats.wifi` use the existing relay; `wifi_ap` is a protocol error because there is no virtual
AP to switch. Identical DHCP requests held before association occupy one pending queue entry;
once handed to esp32sim, subsequent retries are separate requests.

Only the classic ESP32 has the virtual station: on the other chips `--wifi-sta` is a usage error,
`--wifi-sta: esp32s3 has no virtual station for a SoftAP in esp32sim`. Open and WPA2-PSK are
supported. There are no keep-alive frames, explicit
leave/rejoin controls, hidden SSID probes, WPA3-SAE or required PMF; these wait on upstream support.
The device may drop an idle station after about 300 s and esp32sim rejoins. DHCP and lease expiry
belong to the harness, not the runner. esp32sim's `[emu] wifi-sta:` diagnostic remains in stderr,
including frames accepted by the relay but lost because the station subsequently left.

In relay mode the upstream diagnostic's `lease none` refers to esp32sim's disabled DHCP host,
not to the harness: read `network.lease` for the address the test acquired. A last join failure
is shown while the station is listening between attempts; during a new handshake the line
shows `Handshake` and its attempt count instead.
