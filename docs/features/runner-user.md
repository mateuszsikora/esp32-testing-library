# esp32tl-runner

`esp32tl-runner` runs an unmodified image for the classic ESP32 in the esp32sim emulator, on the
emulated clock, and prints the guest's UART0 console on stdout. With `--board`, it connects to a
peer, the harness or any program that speaks [the link protocol](../protocol.md), and wires the
chip's pins to it: the peer sees what the guest sends on its UARTs and the levels it drives on
the pins the peer watches, and sends bytes and levels back. With `--sd-rw` and `--sd-spi`, an SD
card holding an image file sits on four of the chip's pins, with or without `--board`. With
`--wifi`, a Wi-Fi access point is on the air for the chip's station, and the network behind it is
the peer's.
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
  --rom .cache/esp-rom-elfs/20241011/esp32_rev300_rom.elf \
  --flash-at 0x1000=$B/bootloader/bootloader.bin \
  --flash-at 0x8000=$B/partition_table/partition-table.bin \
  --flash-at 0x10000=$B/hello_world.bin \
  --max-seconds 5
```

`esp32tl-runner --help` (or `-h`) prints the usage on stderr and exits 0:

```text
usage: esp32tl-runner --chip esp32 --flash-at OFFSET=FILE... --max-seconds S [options]

  --chip esp32          the chip; only the classic ESP32 for now
  --flash-at OFFSET=FILE   write FILE into flash at OFFSET (hex, 0x optional); repeatable
  --max-seconds S       stop after S emulated seconds: the normal end of a run
  --rom F               the mask ROM ELF; default, as esp32sim's: $ESP32SIM_ROM_DIR,
                        then ~/.espressif/tools/esp-rom-elfs/*/ (newest release first,
                        not the pinned 20241011: whoever runs the tests passes --rom)
  --flash-mb N          flash size in MiB: 1, 2, 4, 8 or 16 (default 4)
  --psram-mb N          PSRAM size in MiB, 0 or 4 (default 0)
  --board socket:PATH   connect to the peer on the Unix socket PATH (docs/protocol.md);
  --board tcp:HOST:PORT   or over TCP. Without --board the chip has nothing on its pins
  --peer-timeout S      how long, in wall seconds, a turn of the peer may take before the
                        run fails with exit code 4 (default 60; 0 waits for ever)
  --console stdout|socket|both   where UART0 goes (default stdout); socket and both need
                        --board
  --sd-rw IMAGE         an SD card holding IMAGE, a whole number of 512 KiB units, on the
                        pins of --sd-spi; the guest's writes go into the file
  --sd-spi sclk=N,mosi=N,miso=N,cs=N   the GPIOs the card is wired to, in SPI mode
  --wifi ssid=…,psk=…,chan=…,bssid=…   esp32sim's virtual access point, every key
                        optional; the network behind it is the peer's, over the
                        socket (eth_tx, eth_rx): needs --board
  --wifi-nearby ssid=…,chan=…,rssi=…,secure=0|1,bssid=…   a network around the
                        station that a scan lists and nothing can join; repeatable,
                        needs --wifi
```

| Flag | Default | Accepts |
|---|---|---|
| `--chip` | none, required | `esp32`, in any case |
| `--flash-at OFFSET=FILE` | none, at least one required | `OFFSET` in hex, with or without a lowercase `0x`; `FILE` is everything after the first `=`. Repeatable; files are written in the order given |
| `--max-seconds S` | none, required | a finite number above 0; fractions are fine |
| `--rom F` | the lookup below | a path to the mask ROM ELF |
| `--flash-mb N` | `4` | `1`, `2`, `4`, `8` or `16` |
| `--psram-mb N` | `0` | `0` or `4` |
| `--board` | none: nothing on the chip's pins | `socket:PATH`, a Unix socket path that is not empty, or `tcp:HOST:PORT`, with a host that is not empty and a port from 0 to 65535 (`tcp:[::1]:7000` for IPv6) |
| `--peer-timeout S` | `60` | `0`, to wait for ever, or a positive number of seconds; fractions are fine. A value that rounds to 0 ns, or is too large for a Rust `Duration` (`1e20`), is a usage error |
| `--console` | `stdout` | `stdout`; `socket` and `both` only with `--board` |
| `--sd-rw IMAGE` | none: no card | a path, valid UTF-8, to an image file of a whole number of 512 KiB units, from 512 KiB to 2 TiB; only with `--sd-spi` |
| `--sd-spi sclk=N,mosi=N,miso=N,cs=N` | none | the four pins in any order, each once, four different GPIOs of the ESP32 (0 to 39 but 24 and 28 to 31); `sclk`, `mosi` and `cs` not input-only (34 to 39); only with `--sd-rw` |
| `--wifi SPEC` | none: no access point | esp32sim's `key=value,...`, every key optional, an empty `SPEC` included: `ssid` (default `esp32sim`), `psk` (also `password` or `pass`; WPA2-PSK with it, open without), `chan` (also `channel` or `ch`; 1 to 14, default 6), `bssid` (default `02:53:49:4d:00:01`). `epoch` is refused. Only with `--board` |
| `--wifi-nearby SPEC` | none | esp32sim's `key=value,...`: `ssid` (1 to 32 bytes, required), `chan` (1 to 14, default 1), `rssi` in dBm (-128 to 127, default -60), `secure` (`0` or `1`, also `false`/`true` or `no`/`yes`; default `1`), `bssid` (default: one derived from the SSID, the same every run). Repeatable, at most 32, each on a BSSID of its own and not the access point's SSID or BSSID. Only with `--wifi` |

A flag given twice keeps its last value, except `--flash-at` and `--wifi-nearby`, which add one
each time. Flags
take their value as the next argument: `--flash-mb=8` is an unknown argument.

### Where the ROM comes from

Without `--rom`, the runner looks for `esp32_rev300_rom.elf` in `$ESP32SIM_ROM_DIR`, then in
`~/.espressif/tools/esp-rom-elfs/*/`, the last directory in name order first, which for the dated
release directories is the newest. That is esp32sim's own lookup. It does not prefer release
`20241011`, the one the project pins and `make setup` puts in `.cache/esp-rom-elfs/20241011/`, and
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

With `--console socket` the guest's UART0 goes to the peer as `console` messages and not to
stdout; with `--console both` it goes to both. Messages written outside the peer's turn reach the
socket within about 20 wall milliseconds, so a peer can read the console as it comes.

The peer can also switch the board's supply off and on (`power`) and pulse the chip's EN pin
(`chip_reset`), as a test does with `board.power_off()`, `board.power_on()` and `board.reset()`
([pytest plugin](pytest-plugin-user.md#resets-and-power-cycles)). The run goes on through both:
the chip comes back through a power-on reset, with what the firmware wrote to flash, and the
run's time does not restart. While the supply is off the guest runs nothing, and the run's time
still passes from one turn of the peer to the next. The flash is the runner's copy of the
`--flash-at` files: the files are read once, at the start, and never written.
[The protocol](../protocol.md#517-power-ext-105) says what a power cycle keeps and what it clears.

`--peer-timeout` bounds, in wall time, how long the runner waits for the peer's turn to end and
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
  --rom .cache/esp-rom-elfs/20241011/esp32_rev300_rom.elf \
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
- The card is there from power-on to the end of the run, through chip resets: it keeps its state,
  as a powered card does, until the guest sends CMD0.
- With `--board`, the peer can make the card fail reads and writes, or take longer over reads,
  writes and erases, from an instant it chooses, on chosen blocks, for so many operations or until
  it clears the rule, and hears back what each rule did
  ([protocol 5.19](../protocol.md#519-sd_fault-sd_clear-and-sd_op-ext-107)). Delays are on the
  emulated clock: the guest polls the card for them as it would a slow card, and its own timeouts
  run on the same clock. Without `--board` the card always answers at once. The harness's way to
  give the rules is `sd_card.fail` and `sd_card.delay`
  ([pytest plugin](pytest-plugin-user.md#the-fixtures)).

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
  --rom .cache/esp-rom-elfs/20241011/esp32_rev300_rom.elf \
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

## What it prints

stdout carries the guest's UART0 output and nothing else, unless `--console socket` sends it to
the peer instead. Everything the runner says goes to stderr, on lines that start with
`esp32tl-runner: `:

- `esp32tl-runner: SD card on SCLK GPIO N, MOSI GPIO N, MISO GPIO N, CS GPIO N: IMAGE (K KiB,
  write-through)` first, with `--sd-rw`;
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
- `esp32tl-runner: chip reset at T s, cause CAUSE` each time the firmware resets the chip, as
  `esp_restart()` does: T in emulated seconds with six decimals, CAUSE the reset cause the ROM
  reports, in hex (`0x...`). The runner restarts the guest from the ROM, as the chip would, and
  the run goes on. With `--board`, the peer gets a `reset` message as well. The same line, with
  cause `0x1`, follows the peer's `chip_reset` and its `power` that switches the supply back on;
- `esp32tl-runner: power off at T s` when the peer switches the board's supply off.
- `esp32tl-runner: SD card: N commands, R blocks read, W blocks written` at the end of a run with
  `--sd-rw`, just before the line that ends it; with `, E image errors` added when reads, writes
  or erases of the image file failed, and `, F operations faulted` when the peer's fault rules
  failed or delayed any. Each such failure also prints
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
| 2 | an input that cannot be loaded: no ROM found, a file that cannot be read, a ROM that is not an ELF, an image that does not fit in flash, an SD card image that cannot be opened for writing or is not a whole number of 512 KiB units | `esp32tl-runner: MESSAGE`, no stop line |
| 3 | with `--board`: a protocol error by the peer, such as a malformed line, a version or chip mismatch, an input stamped before its turn's `sync`, an `eth_rx` or `wifi_ap` on a run without `--wifi` | `end: protocol error by the peer at BOARD: WHAT: LINE at ...`, `LINE` cut after 200 characters |
| 4 | with `--board`: the connection failed, or the peer is gone: it closed the socket, a read or write failed, or `--peer-timeout` ran out | `end: cannot connect to the peer at ...`, or `end: the peer at BOARD is gone: WHY at ...` |
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
the slice in progress, at most one emulated millisecond, or at once if the runner is waiting for
the peer. The peer gets the events not yet sent and `stats` with `end: signal`, and the runner
exits 0. Those last messages get two wall seconds; a peer that does not read them by then is given
up on, and the exit code stays 0. A second `SIGTERM` or `SIGINT` ends the process at once, with
128 plus the signal's number. Before the connection is made, a signal ends the process as it does
without `--board`. A test run usually ends otherwise: the backend starts the runner with
`--max-seconds 86400 --console both --peer-timeout 0` at `board.power_on()`, holds it in a turn
whenever the test is not waiting, and at teardown answers that turn with `stop`. A runner in no
held turn then, or still running 5 s later, gets one `SIGTERM` once it has said hello, then
`SIGKILL` 5 s after that ([esp32sim backend](esp32sim-backend-user.md#how-the-run-ends)).

## See also

- [runner-technical.md](runner-technical.md): how it works, every message, the pins and profiles.
- [Link protocol](../protocol.md): what goes over the socket, for whoever writes a peer.
- [esp32sim-backend-user.md](esp32sim-backend-user.md): how tests start the runner, and
  `ESP32TL_RUNNER` / `ESP32TL_ROM` to use another build.
- [Stage 0 findings, Speed](../stage0-findings.md#speed): how emulated time relates to wall time.
