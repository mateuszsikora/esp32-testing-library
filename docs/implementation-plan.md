# esp32tl implementation plan

Derived from issue #1, "Specify esp32tl: test unmodified ESP32 firmware through its wires".
Written 2026-10-29 against esp32sim `main` at `2e36224` (227 commits after v0.3.0) and
pytest-embedded 2.9.3. Revised the same day after five reviews, the last of which found no
blocking items and recommended that further review go to `docs/protocol.md` and the M1 fake-peer
test rather than to this plan.

The specification says that anything the plan finds wrong gets amended there, not worked around.
Section 1 lists those findings first. Everything after it builds on the amended specification.

## 1. Findings that amend the specification

Each item names the fact, where it was checked, and what changes.

1. **pytest-embedded cannot load a third-party service.** `--embedded-services` resolves names
   through a closed dictionary, `SERVICE_LIB_NAMES` in `pytest_embedded/utils.py`, and
   `FIXTURES_SERVICES` decides which fixture classes each service contributes. A package named
   `pytest-embedded-esp32sim` is not reachable by name until that dictionary changes upstream.
   *Amendment:* esp32tl ships its own pytest plugin (`pytest11` entry point) whose fixtures build on
   pytest-embedded's classes (`Dut`, `DuplicateStdoutPopen`, `PexpectProcess`) the way
   `pytest-embedded-qemu` does, and delegates the `serial` backend to pytest-embedded's `esp`
   and `idf` services (`IdfDut`). The registry entry is offered upstream once the plugin is stable, as a
   one-line change plus the package. The harness API in the spec is unchanged by this.

2. **The stock esp32sim CLI cannot attach an out-of-process board on the classic chip.** `--board`
   on `esp32` accepts `none`, `bare`, `esp32dev`, `ttgo-tdisplay` and `ai-thinker-cam`
   (`cli/src/lib.rs:551`). Issue mikroverk/esp32sim#325 is open with no comments. *Consequence:*
   the first milestone goes through esp32tl's own runner crate, as the spec's fallback already
   says. This is the main path, not a contingency, until #325 lands.

3. **The runner crate is feasible as described.** The `cli` crate is also a library (`[lib]
   name = "esp32sim"` in `cli/Cargo.toml`); `esp32` exports `machine`, `Machine` and `Stop`;
   `Machine` exposes `load_input`, `boot_rom`, `run`, `reboot`, `drain_console`, `max_cycles` and
   `console`; `bus.board` and `bus.attach_board_devices()` are public and used exactly this way by
   `cli/examples/uart_echo.rs`. The crates are not on crates.io, so the runner depends on them as a
   git dependency pinned by revision. The build profile uses fat LTO and one codegen unit, so a
   cold release build takes minutes; CI caches it.

4. **Guest reset needs no inference.** The spec's fallback for the missing reset callback is to
   infer a reset from the board being re-attached. In the runner, the run loop is ours: `run`
   returns `Stop::SwReset`, we call `reboot()`, which returns the reset cause as a `u32`
   (`esp-soc/src/machine.rs:306`), and we send `reset` with that cause to the peer ourselves.
   *Amendment:* the "infer from re-attach" fallback is dropped. If #325 lands with
   `guest_reset`, the message is produced by the trait instead; the peer sees the same message.

5. **PR #322 (chip revision v3.0) is not needed for any validation firmware.** The bootloader
   refuses an image only when it was built with `CONFIG_ESP32_REV_MIN_3=y`; IDF's default is
   `ESP32_REV_MIN_0`, and the examples in the Validation section use the default. *Amendment:* the
   patch branch is not created until the first firmware that needs it is run. Milestones 0 to 7 pin
   upstream `main`. *Update, 2026-10-30:* #322 was merged as `8c65215`, which is now the pin. It
   also brings mikroverk/esp32sim#300 (LL Privacy in the virtual BLE controller) and
   mikroverk/esp32sim#307 (the roadmap, documentation only), and nothing else beyond the fix build
   stage 0 used. On it, `hello_world` built with `CONFIG_ESP32_REV_MIN_3=y` boots on its power-on
   reset and reports v3.0, and the production images stage 0 measured execute the same
   instructions as on the fix build in every configuration stage 0 ran them in: the release image
   with empty NVS, and with NVS naming a Bluetooth peer with and without `--ble`, and the replay
   image. Those images need no patch branch either. The branch is first needed for whichever of BT
   Classic (M8) and the timer wake (M9) is declined upstream. *Update, 2026-11-08:* the timer wake
   was not declined: mikroverk/esp32sim#336 was merged as `afcfba6`, the pin from #35
   ([decisions](decisions/runner.md#esp32sim-is-pinned-at-the-merge-commit-of-the-deep-sleep-timer-wake))
   until #34 moved it to `c99fb28`, the merge of mikroverk/esp32sim#347
   ([decisions](decisions/runner.md#esp32sim-is-pinned-at-the-merge-commit-of-the-bluetooth-classic-enable)).

6. **`pppos_client` is not in ESP-IDF.** It lives in `espressif/esp-protocols` under
   `components/esp_modem/examples/pppos_client`. The validation list stays, the build recipe fetches
   esp-protocols at a pinned tag.

7. **Transmit timing is FIFO-write time** (`uart_tx` fires at the FIFO write, `esp32/src/bus.rs`
   per #325). Already in the spec; recorded here because the recording header's `clock` field
   depends on it (section 4.3).

8. **Receive pacing belongs in the runner, not in Python.** The classic RX FIFO holds 128 bytes and
   overflows silently beyond it (`esp-periph/src/uart.rs`); `UartInput` carries no per-byte timing.
   The runner releases peer bytes one character time apart at the route's baud. Python peers send
   whole messages with one timestamp and never pace bytes. This also answers the spec's worry about
   Python being too slow: nothing in Python runs per byte on the emulated clock.

9. **Bluetooth Classic on the emulator is a core change, not a board.** esp32sim's `--ble` hooks the
   controller through guest symbols (a FreeRTOS task installed over a trampoline,
   `docs/esp32.md`); the controller is not on a pin, so the HCI tap cannot go through `BoardModel`.
   The Bluetooth milestone cannot start until the HCI transport exists upstream or on the patch
   branch. It is scheduled last among the emulator interfaces for that reason.

10. **ADC is not a `BoardModel` hook.** The trait (`esp-soc/src/board.rs`) has no analog method;
    on the classic chip voltages enter through the machine's script actions
    (`ScriptAction::Analog(pin, AnalogSource)` in `esp-soc/src/machine.rs`, applied through
    `SocBus::analog_set`, which the runner can call directly), and #325 does not mention ADC. *Amendment:* the spec's table row "GPIO / ADC lines into the ESP32:
    scenario-driven" holds, but ADC goes through the machine API in the runner, outside the
    `BoardModel` transport. Even an accepted #325 leaves ADC on our side until a hook is proposed
    upstream (milestone 10). The runner is therefore not dropped the day #325 ships; it shrinks.

11. **The `real` source on the emulator needs its own time design.** A real device on a host
    serial port must have its wall-time bytes stamped into emulated time and the run paced with
    `--realtime`; #325 defers exactly this ("the mechanics of stamping wall time as emulated time
    need their own design"). On the `serial` backend the on-board peripherals are soldered, so a
    tap on them would need a hardware sniffer. *Amendment:* `real` on `esp32sim` gets its own
    milestone (5) with that design; on `serial` the only tappable interface is the console, and the
    capability table says so.

12. **The LilyGO profile cannot carry the first milestone.** The spec gives the T-A7670G R2 only
    the modem route (UART1 on GPIO 26/27) and says other pins come from the vendor schematic, not
    from memory. A GNSS UART route for that board is unknown, and on it UART1 is the modem's.
    *Amendment:* every validation firmware runs on a generic `esp32-devkitc` profile whose
    routes are ours to choose, because `nmea_reader` is this repository's firmware and the IDF
    examples take their pins from `sdkconfig`. No milestone needs the LilyGO profile. The LilyGO profile is
    filled in from the schematic when someone has it, and gets a GNSS entry only then (on that
    board the GNSS may well be reached through the modem's AT commands). *Amended by section 11:*
    the profile takes its GNSS route, flash and PSRAM from LilyGO's documentation, and M6's
    validation runs on its modem route, so M6 needs the LilyGO profile.

## 2. Decisions on the open questions

| Question | Decision | Why |
|---|---|---|
| Transport shape (#325 vs generalised `--cooja`) | One Python-side interface, `BoardLink`, with exactly the messages #325 proposes plus the runner-only extensions marked in 4.1. The runner speaks that protocol today; if upstream chooses the `--cooja` shape, only the runner and the `BoardLink` adapter change | The harness never sees the wire format |
| First-milestone NMEA source | Generated, written into the recording format | Deterministic, needs no hardware, and the same file format takes a real receiver's recording later with no code change |
| Modelled Bluetooth peer description | Deferred to milestone 8, as the spec says. Leaning to Bumble scripts (Python, already a dependency of the model) | Decided when the HCI path exists |
| PPP peer on macOS and CI | ~~`pppd` in a Linux container on both~~ *Superseded by section 11:* the harness ends PPP itself and simulates the network in emulated time | No container, no wall time, runs repeat |
| When the repository goes public | After milestone 1 passes on CI, as the spec says ("private until a first piece works end to end") | The plan does not change a decision the spec already made |
| Who owns the console stream | pytest-embedded's `DuplicateStdoutPopen` owns the runner's stdout; the socket `console` message is opt-in (`--console stdout\|socket\|both`) for the console tap. *Amended by section 11:* every run uses `--console both`; `console.expect` reads the `console` messages on the test's clock, and stdout only feeds `dut.log` | One owner per stream |

## 3. Architecture

```
esp32-testing-library/
├── pyproject.toml              esp32tl, Python ≥ 3.11, pytest, pytest-embedded, pytest-embedded-idf[serial]
├── esp32tl/
│   ├── pytest_plugin.py        board, console, gnss, modem, bt, wifi, sd_card, lines, scenario
│   ├── cli.py                  esp32tl run | record | replay
│   ├── backends/
│   │   ├── base.py             Backend: start, stop, console stream, capabilities per interface
│   │   ├── esp32sim.py         spawns the runner, owns the BoardLink socket, emulated clock
│   │   └── serial.py           wraps pytest-embedded's IdfDut (services esp,idf), wall clock
│   ├── link/
│   │   ├── protocol.py         NDJSON messages, version, handshake (section 4.1)
│   │   └── boardlink.py        BoardLink: routes uart/gpio events to interfaces, collects quiet_until/latency
│   ├── boards/                 board profiles, data only (TOML)
│   │   ├── esp32-devkitc.toml  generic module, routes on free pins; carries every validation firmware
│   │   └── lilygo-t-a7670g-r2.toml
│   ├── interfaces/             one class per wire: Console, Uart (GNSS, modem), Lines, Hci, Wifi, SdCard
│   ├── sources/
│   │   ├── model/              nmea.py, at_modem.py, (bt peer later)
│   │   ├── replay.py           Recording reader + matching engine
│   │   └── real.py             host serial port, wall-time stamping (milestone 5)
│   ├── recording.py            NDJSON recording format: header, events, btsnoop passthrough
│   ├── matching.py             the generic request/response engine (section 4.4)
│   ├── tap.py                  wraps any source, writes a recording
│   └── scenario.py             timed external events
├── runner/                     Rust crate esp32tl-runner (section 4.2)
│   ├── Cargo.toml              esp32sim, esp32, esp-soc as git deps pinned by rev
│   └── src/{main.rs, socket_board.rs, protocol.rs}
├── firmware/
│   ├── nmea_reader/            the small ESP-IDF app kept in this repo
│   └── build.sh                builds validation firmware at a pinned IDF in Docker
├── recordings/                 committed recordings used by tests
├── tests/                      pytest suite (unit + end-to-end)
└── docs/                       this plan, protocol, recording format, board profile format
```

Boundaries that keep the spec's rules:

- **No product knowledge.** `boards/` holds pin tables; `sources/model/` holds protocols (NMEA, AT).
  Nothing knows what the firmware does with them.
- **One transport interface.** Only `backends/esp32sim.py` and the runner know the socket. An
  interface object receives `(t_ns, direction, bytes)` and returns `(t_ns, bytes)`; it cannot tell
  emulator from real board except through the clock it is given.
- **Models are processes in spirit.** Each model is a class with `on_rx(t, bytes)`,
  `next_output(now) -> (t, bytes) | None`, `quiet_until`, `latency`. Running one in another process
  or language only needs the same NDJSON on a socket, which the `BoardLink` already speaks.
- **Fixtures sit on pytest-embedded, not beside it.** `console` wraps pytest-embedded's `dut`
  (`console.expect` is `dut.expect`). The backend is chosen by a `pytest_generate_tests` hook in
  the esp32tl plugin (a fixture cannot parametrise another fixture): one `metafunc.parametrize`
  call with `embedded_services` and `app_path` together, `indirect=True` and shared `ids`, so the
  two always travel in pairs, and `--esp32tl-backend` filters the list before parametrising. The
  two backends map onto pytest-embedded differently:
  - `esp32sim`: **no services** (an empty `embedded_services`, which pytest-embedded turns into
    the base `Dut`). The backend spawns the runner as `DuplicateStdoutPopen(msg_queue, cmd, meta)`
    with the fixture's `msg_queue`, whose listener pumps it into `pexpect_proc`, so the base
    `dut.expect` sees the console. This is the `pytest-embedded-qemu` shape. The backend builds
    `IdfApp(app_path)` directly (a plain class, no service needed) and turns its `flash_files`,
    read from `flasher_args.json`, into the runner's `--flash-at` offsets.
  - `serial`: `esp,idf` (registry names). `esp` alone gives only `hard_reset` and `erase_flash`;
    `flash()` lives in `IdfSerial` from `pytest-embedded-idf`, together with `IdfApp`, so the
    harness can put the CI artifact on the board before the test. pytest-embedded's `serial`,
    `app` and `--port` keep their meaning, and `idf` brings the panic decoder for console
    assertions.
  `--embedded-services` is never given on the command line: `_services()` rejects any name
  outside `SERVICE_LIB_NAMES`, and an empty parameter falls back to the CLI option, so a session
  started with `--embedded-services esp` would make the `esp32sim` case open a serial port. For
  the same reason the firmware path reaches `app` through the `app_path` parametrisation, not
  `--app-path`. esp32tl adds only `--esp32tl-backend`, which limits the parametrisation to one
  backend.
- **Distribution.** pytest-embedded declares no extras (PyPI 2.9.3: `requires_dist` is pytest,
  pexpect, filelock), so the services are separate dependencies: `pytest-embedded` and
  `pytest-embedded-idf[serial]`, whose `serial` extra pulls `pytest-embedded-serial-esp` and
  with it `pytest-embedded-serial`. `pip install esp32tl` brings the Python side only. `make setup` builds the
  runner from the pinned revision (cached by revision); prebuilt runner binaries per OS come with
  milestone 10. "Clean checkout, one command" therefore needs cargo and network access to the
  git dependency, and says so in the README.

## 4. Contracts to write down before code

### 4.1 Link protocol (runner ⇄ harness)

*Superseded by [`docs/protocol.md`](protocol.md), the contract (#10). Kept as written; that
document's last section lists what changed and why.*

Newline-delimited JSON over a Unix socket (TCP as an option for a Linux container), times in
emulated nanoseconds, following #325 so the runner is replaceable by upstream's `SocketBoard`.
Messages marked *ext* are runner extensions that #325 does not cover; they stay in the runner
whatever happens upstream (finding 10).

| Direction | Message | Fields |
|---|---|---|
| both | `hello` | `version`, `chip`, `clock` (`emulated-fifo-write` or `wall`); the harness adds `board` (the runner does not know the profile, its board is the socket) |
| runner → harness | `uart_tx` | `seq`, `t`, `port`, `tx_pins`, `baud`, `data` (base64). The trait fires per byte; the runner coalesces consecutive bytes on one route into one message, `t` being the first byte's, and flushes at the latest at the block point of rule 1 and on any route change |
| runner → harness | `uart_cfg` *ext* | `seq`, `t`, `port`, `tx_pins`, `rx_pin`, `baud`: sent when a route's configuration changes, so a peer on a receive-only route (the GNSS case: the firmware never transmits) learns the baud instead of trusting the profile; a mismatch is otherwise a silent `FRM_ERR`. The runner reads `bus.periph.uart_route(port)` (public, `esp32/src/periph.rs:921`) in `advance_to` and sends the message when the result changes. `baud` and `rx_pin` are `null` until the firmware configures them; that is the valid initial state, not an error |
| runner → harness | `gpio_out` | `seq`, `t`, `changes: [[pin, level]]`, `enabled`, `output` |
| runner → harness | `reset` | `seq`, `t`, `cause` |
| runner → harness | `sync` | `t` (how far emulated time has advanced) |
| runner → harness | `console` (opt-in) | `t`, `data` (UART0 bytes; the console as a tap) |
| runner → harness | `warn` *ext* | `t`, `kind`, `detail`: what the emulator dropped or refused and the peer cannot see: RX FIFO overflow (`RXFIFO_OVF`), a frame error from a baud mismatch (`FRM_ERR`), `uart_rx` on an unrouted pin ("unrouted input is dropped", `board.rs`), `gpio_in` on a pin the firmware drives as output |
| runner → harness | `stats` *ext* | sent once before the socket closes: per route, bytes each way, overflows, frame errors, dropped inputs; `BoardLink` puts them in the test report next to the matching engine's unseen requests |
| harness → runner | `uart_rx` | `t`, `pin`, `baud`, `data` |
| harness → runner | `gpio_in` | `t`, `levels: [[pin, level]]` |
| harness → runner | `release` | `t`, `pins` |
| harness → runner | `adc` *ext* | `t`, `pin`, `volts` (applied through the machine's analog source, not the board) |
| harness → runner | `wait` | `ack` (last `seq` seen), `quiet_until`, `latency` |
| harness → runner | `stop` | the runner answers with a final `sync`, then `stats`, closes the socket and exits 0; any other exit code names the cause on stderr |

`gpio_in`, `release` and `adc` may carry a future `t` (scenario events with `at=`); the runner
queues them by `t` in a queue of its own and applies each at its time. (`machine.script` is
public too, but its cursor walks `events` in order and an appended event must be later than
everything the cursor has passed; an unwritten ordering the runner should not depend on.) `BoardLink` folds the scenario timeline into `quiet_until` (rule 1), otherwise
the runner would run past a scheduled event.

Rules:

1. **Every runner → harness event carries `seq`, and the peer acknowledges it** with `wait`
   (carrying `ack`) or with data at or after it. A peer with nothing to say still sends `wait`.
   The runner blocks only at `min(quiet_until, t_first_unacked + latency)`, never per byte. A peer
   that acknowledges a request and answers later must set `quiet_until` no later than the
   answer's `t`, or the runner runs past it and the answer is late under rule 2.
2. **A `t` earlier than the runner's current emulated time is a protocol error** that ends the run
   naming the peer: the peer broke its `latency` promise. No silent clamping, so two runs of one
   recording give identical output (section 6) only when `t` comes from the recording, which is
   the only place it may come from. **`latency` is therefore bound to the source:** a `replay`
   peer declares the smallest request-to-answer delay in its recording (and may never answer
   sooner), a `model` peer declares its shortest possible answer delay. A replay whose recording
   holds an answer faster than the declared `latency` would otherwise fail at that request.
   **"At once" means `t_req + latency`.** The runner may flush a `uart_tx` only at the block
   point, which is already `t_first_unacked + latency ≤ t_req + latency`, so an answer stamped
   `t_req` would be late by construction; one stamped `t_req + latency` is always on time.
3. **`sync` goes out at every `advance_to`** and not more often than once per emulated
   millisecond.
4. **The guest boots after the first `wait`.** A peer that comes up slower than the firmware would
   otherwise miss the first configuration bytes.
5. A closed socket ends the run with an error naming the peer. Bytes in `uart_rx` are paced by the
   runner at the route's baud from `t`. Protocol version is checked in `hello`.

The Rust unit test with a fake peer (milestone 1) covers rules 2 and 4 explicitly.

### 4.2 Runner crate

*Where this section and [`docs/protocol.md`](protocol.md) differ, the protocol holds: run-loop
slices instead of `next_deadline`, received bytes delivered through `bus.periph.uart_pin_input`
rather than `BoardModel::uart_rx`, emulator stops in `stats.end` rather than `warn`, and `SIGTERM`
ending a run with `stats`. Its sections 6.1 and 11 say why.*

- `esp32tl-runner --chip esp32 --board socket:PATH --flash-at ... --max-seconds S [--wifi ...]
  [--realtime] [--console stdout|socket|both]`: the flag names copy esp32sim's so that a future
  switch to `esp32sim --board socket:` is a rename.
- `SocketBoard: BoardModel` with `uses_uart_pins`, `uart_tx`, `uart_rx`, `gpio_output_at`,
  `input_levels`, `take_edges`, `released_inputs`, `next_deadline`, `advance_to`. The
  `next_deadline` is the minimum of three: the next paced byte, the peer's `quiet_until`, and
  `t_first_unacked + latency`. The third one matters when the core sleeps in WFI waiting for an
  answer (the common case with a modem): the classic bus folds the board deadline into its own
  (`esp32/src/soc.rs:106-112`) and `idle_budget` skips the idle core only that far, so without it
  the emulator would jump to the next timer, past `t_req + latency`, and an answer stamped there
  (rule 2) would arrive late. The board deadline counts only while `pins_active`, which the
  capabilities cached at `attach_board_devices` set and `SocketBoard` enables through
  `uses_uart_pins`.
- Time units: the protocol speaks nanoseconds, the trait `VirtualCycle`. `bus.cycles()` runs at a
  fixed reference independent of the momentary CPU clock (`tick` scales through
  `cpu_scale_half`, `esp32/src/bus.rs:642`; `Machine::seconds` divides by `S::CPU_HZ`,
  `esp-soc/src/machine.rs:166`), so `ns = cycles × 10⁹ / CPU_HZ` holds for the whole run,
  including across the XTAL to PLL switch at boot. `docs/protocol.md` states the formula and
  where `CPU_HZ` comes from (`esp32::periph::CPU_HZ`), so a peer in another language need not
  discover it.
- Owns the run loop: `run` → on `Stop::SwReset` send `reset` with the cause `reboot()` returns,
  continue; `Stop::Halted` (reached `max_cycles`, `esp-soc/src/machine.rs:589`) is the normal end
  after `--max-seconds`, as is `stop` from the peer. Every other `Stop` variant
  (`esp-soc/src/soc.rs:9`: `MaxInsns`, `Breakpoint`, `Unimplemented`, `Simcall`, `Ebreak`,
  `Watch`, `Exceptions`, `CostModel`, `CostModelLifecycle`) ends the run with `stats`, the variant
  name on stderr and a non-zero exit code; `Unimplemented` and `Exceptions` also go out as `warn`,
  because they are what separates "the firmware hung" from "the emulator cannot".
- Handles the *ext* messages outside the trait: `adc` through the machine's analog source,
  `uart_cfg` from the bus.
- Console on UART0 goes to stdout by default so pytest-embedded's `DuplicateStdoutPopen` owns it;
  `--console socket` or `both` adds the `console` message for the tap.
- Stays small: one file for the board, one for the protocol, one for the CLI. When #325 ships, the
  `SocketBoard` half is replaced by upstream's; the *ext* half stays until the hooks it needs exist
  upstream (milestone 10). Until then its revision pin of esp32sim is the project's pin.

### 4.3 Recording format

*As built (#14): [`docs/recording-format.md`](recording-format.md) is the format, and holds where
the two differ: `end`, `app_sha256` and `tools` may be left out, header fields of a tool's own are
kept, and nothing reads the HCI sidecar yet. Kept as written.*

One file per tap, `.jsonl`. Line 1 is the header:

```json
{"esp32tl_recording": 1, "board": "esp32-devkitc", "interface": "uart.gnss",
 "source": "model:nmea", "clock": "emulated-fifo-write", "end": "hold",
 "app_sha256": "...", "tools": {"esp32tl": "0.1.0", "esp32sim": "8c652151dd5a521249a785ca147f20a9f8eda68f"},
 "created": "2026-10-29T18:00:00Z"}
```

Then one event per line: `{"t": 123456789, "dir": "in" | "out", "data": "<base64>"}`, `in` meaning
into the ESP32. `clock` uses the same vocabulary as `hello`: `emulated-fifo-write` for emulator
runs and `wall` for a real board or a real device; the reader refuses to compare timings across the
two. `app_sha256` is the hash of `app.bin` only, so a bootloader or partition-table change does not
invalidate recordings; on the `serial` backend it is compared with what the harness flashed.
`end` is `hold` (default) or `loop`. A recording may switch clock part-way with an event line
`{"t": ..., "clock": "wall"}` (a modem session after `CONNECT`, section M6); the reader applies
the cross-clock refusal from that line on. HCI taps are btsnoop files and the
header lives in a sidecar `.json`.

### 4.4 Matching engine

Protocol-agnostic, configured by a small `Protocol` dataclass: `terminator`, `prompt`, `normalise`
(case, whitespace), `unknown_reply`. Built from a recording by grouping `out` events into requests
and the `in` events until the next request into answers. Rules, in the spec's words:

1. Each request is answered from its own recording, next answer in recorded order, wrapping.
2. An unseen request gets `unknown_reply` at once, that is at `t_req + latency` (protocol rule
   2), counted and listed in the test report.
3. Timing is the recording's: delay = answer time minus request time; a recorded silence replays
   as silence.
4. End of recording: `loop` or `hold` per header.

Streams with no request (NMEA sentences) are the degenerate case: every event is an answer to the
empty request, released at its recorded time.

The engine computes the peer's `latency` from the recording when it loads it (the minimum
request-to-answer delay, protocol rule 2), and `quiet_until` as the minimum of the next unprompted
event and every answer waiting to be sent, so a deferred answer is never late.

Default `Protocol` configurations shipped with the engine (values fixed in milestone 2):

| Protocol | Terminator | Unknown reply |
|---|---|---|
| AT (Hayes) | `\r` in, `\r\n` out | `\r\nERROR\r\n` (verbose result code, ATV1, the default; firmware parsers match the leading `\r\n`) |
| NMEA PMTK (MediaTek) | `\r\n` | `$PMTK001,<cmd>,1*<checksum>\r\n` (flag 1 is "unsupported packet type"; 0 means "invalid packet") |
| UBX (u-blox) | binary frame, opaque | `UBX-ACK-NAK` for the request's class and id |

### 4.5 Board profiles

TOML, data only. Two profiles at the start.

*As built (#13, #66): the LilyGO profile takes its GNSS route, flash and PSRAM from LilyGO's pin
table for the T-A7670X ESP32 and its `ExternalGPS_A7670G_Only` example, and the esp32sim backend
gives the runner the profile's `psram_mb` as `--psram-mb`;
[`docs/features/board-profiles-technical.md`](features/board-profiles-technical.md) holds the
schema, and [`docs/decisions/harness.md`](decisions/harness.md) why.*

`esp32-devkitc.toml` carries every validation firmware. Its GNSS route is ours to choose because
`nmea_reader` is this repository's firmware: UART1 on GPIO 4 (TX) and 5 (RX). GPIO 16 and 17 are
avoided on purpose: on WROVER modules they belong to the PSRAM, and the same firmware should run
on both module families. GPIO 5 is a strapping pin (SDIO slave timing, together with GPIO 15); a
receiver holding it low at boot does not change the boot mode, and the comment in the profile says
so. If the pin is ever moved, GPIO 12 and 15 are the ones to avoid: GPIO 12 selects the VDD_SDIO
voltage and can stop the boot, and GPIO 15 low silences the ROM log on U0TXD, which removes the
banner a console assertion may expect.

```toml
name = "esp32-devkitc"
chip = "esp32"
flash_mb = 4
psram_mb = 0

[uart.gnss]
port = 1
tx = 4
rx = 5                    # strapping pin (SDIO slave timing): a low level at boot is harmless here
baud = 9600
```

`lilygo-t-a7670g-r2.toml` holds the modem route and lines the spec gives, and from LilyGO's pin
table for the T-A7670X ESP32 and its `ExternalGPS_A7670G_Only` example the GNSS route of the
board's GPS version, its flash and its PSRAM; nothing from memory. The WROVER-E carries 8 MB of
PSRAM, and esp32sim models 0 or 4:

```toml
name = "lilygo-t-a7670g-r2"
chip = "esp32"            # ESP32-WROVER-E
flash_mb = 4
psram_mb = 4

[uart.modem]
port = 1
tx = 26                   # ESP32 TX → modem RX
rx = 27
baud = 115200

[uart.gnss]
port = 2
tx = 21                   # ESP32 TX → receiver RX
rx = 22
baud = 9600

[lines.modem]
pwrkey = 4
ri = { pin = 33, pull = "down" }
```

Capability declaration per backend lives beside the profile: which interfaces can be `model`,
`replay` or `real`, and which can be tapped. On `serial`, no on-board interface can be modelled
or replayed, and only the console can be tapped. The `board` fixture skips with that reason.

## 5. Milestones

Each milestone ends with a test that runs from a clean checkout with one command (`make test`, or
`pytest` after `make setup`), on macOS and Linux CI.

### M0. Bootstrap (console only)

Goal: an unmodified `hello_world` prints through the runner into a pytest assertion.

- Repository layout above, `pyproject.toml`, `ruff`, `mypy`, pre-commit,
  `make setup | build-runner | firmware | test`.
- CI on GitHub Actions. GitHub-hosted macOS runners have no Docker, so: one `firmware` job on
  the self-hosted Linux runner builds every validation image in the `espressif/idf:v5.3.6`
  container (classic target) and hands it on through the Actions cache, keyed by IDF tag and
  example path; `test` jobs on self-hosted Linux and macOS restore it and run only the runner and
  Python. (The cache, not an artifact: artifacts count against the account's storage quota.)
  Tests that need a container carry the `requires_docker` marker and skip with that reason when
  Docker is absent. Python matrix 3.11, 3.13 and 3.14 (3.14 is what this machine runs).
- `runner/`: builds against esp32sim at `8c65215`; `--board socket:` not yet wired, console on
  stdout. `make setup` builds it, cached by revision.
- ROM ELF acquisition: `make setup` finds `~/.espressif/tools/esp-rom-elfs/*/esp32_rev300_rom.elf`
  or downloads release `20241011` of `espressif/esp-rom-elfs` (the release the spec's own test
  used) into `.cache/`; never committed.
- `firmware/build.sh` runs the same container build locally. A developer with a local IDF
  (v5.5.5 here) can iterate with `IDF_LOCAL=1`; goldens come from the container.
- `esp32tl/backends/esp32sim.py` spawns the runner as a `DuplicateStdoutPopen`; the `board` and
  `console` fixtures exist; `console.expect("Hello world!")` passes.

Done when: `make test` passes on a clean clone in CI, and the test fails when the app binary is
replaced by one that prints something else.

### M1. The first milestone of the spec: NMEA replay into an unmodified app

Goal: a replayed NMEA recording reaches `nmea_reader` through UART1 in emulated time; pytest asserts
the printed fix; a changed recording changes the assertion's outcome. Runs on the
`esp32-devkitc` profile.

- *The runner's part follows [`docs/protocol.md`](protocol.md), which replaces the
  `next_deadline` reasoning below with run-loop slices (its sections 6.1 and 11).*
- Runner: `SocketBoard` with `uart_tx`/`uart_rx`, byte coalescing, baud pacing, `seq`/`ack`,
  `quiet_until`/`latency`, `reset`, `sync`, `uart_cfg` from `bus.periph.uart_route`, `warn`
  (`FRM_ERR` is exactly what `uart_cfg` exists to catch). Rust unit test with a fake peer covering
  pacing, deadline arithmetic, coalescing and flush points, reset forwarding, a late `t` (rule 2),
  an answer stamped exactly `t_req + latency` passing, the same with the guest idle in WFI when
  the request goes out (the `next_deadline` third component; without it this case fails at
  random depending on whether the firmware happened to sleep), and boot-after-first-`wait`
  (rule 4).
- Python: `link/protocol.py`, `link/boardlink.py`, `recording.py`, `sources/replay.py` (stream
  case only), the `gnss` fixture with `replay(path)`; `warn` messages collected into the test
  report.
- `firmware/nmea_reader`: ESP-IDF app, UART1 on pins from `sdkconfig` (defaults to the devkit
  profile's GNSS route, 9600 baud, and the committed recording is at the same baud; `uart_cfg`
  makes a mismatch a test failure rather than silence), parses `$GPGGA`/`$GPRMC`, prints
  `fix acquired lat=<...> lon=<...>` on the first fix and one line per later fix. Minimal NMEA
  parsing, no library, so the firmware is auditable.
- `recordings/gnss-generated-1.jsonl`: produced by a committed script from a fixed seed, one
  sentence per emulated second at 9600 baud.
- Tests: `test_reports_a_fix` (the spec's example), `test_fix_follows_the_recording` (a second
  recording with a different position prints that position), `test_time_is_emulated` (ten fixes
  arrive in under five wall seconds; the emulator runs 15 to 37 times faster than real time per
  #325, but each exchange with the Python peer is a wall-time block, so the margin is deliberate).
- CLI: `esp32tl run --board ... --app ...` and `esp32tl replay --interface gnss --recording ...`.

Done when: the spec's "done" sentence holds: same test, clean checkout, one command, fails when the
recording is changed. The repository goes public here.

### M2. Tap and the matching engine

*As built (#21): a test taps a route with `gnss.tap(path)` before `board.power_on()`, not with the
`tap` marker, which is gone, and the backend writes the recording at the end of the run
([run report](features/run-report-user.md)). `stats` go into the `esp32sim link` section of every
test, with the bytes on each route, and a test that failed gets the run on the board's clock
there as well. `esp32tl record` and the engine's unseen-request list come with #20.*

- `tap(path)` on `model` and `replay` sources; writes the format in 4.3 in both directions. (`real`
  comes with M5.)
- `matching.py` with the four rules and the default `Protocol` configurations of 4.4 with their
  concrete unknown replies.
- Unit tests driven by synthetic recordings: wrap-around, unseen request counted, recorded timeout,
  `loop` vs `hold`, and that an unknown request and a deferred answer both set `quiet_until`.
- `stats` at `stop`, merged with the engine's unseen-request list into one report per test.
- `gnss.replay` now answers configuration commands the firmware sends.
- `esp32tl record` for the emulator backend (records a modelled source, which proves the tap before
  a real device is involved).

### M3. GPIO, ADC and the scenario timeline

*Amended by section 11: the test's clock replaces the `scenario` fixture's timeline for what a test
does in its body; `lines` stays, and the modem model needs it for PWRKEY and RI.*

*As built (#23, #24): the runner's part is in [`protocol.md`](protocol.md) 5.4 and 5.11, without
`adc`, which waits for an example that needs it. `lines` has `drive(line, level)`, `release(line)`,
`level(line)` and `events()`, with no `at=`; there is no `scenario` fixture, and `BoardLink` has no
timeline ([`decisions/harness.md`](decisions/harness.md#gpio-lines-are-an-interface-of-the-link-and-the-scenario-timeline-is-gone)).
The profile's pulls are levels the harness drives, and `gpio_echo` validates the lines on the
LilyGO profile ([validation firmware](features/validation-firmware-user.md#gpio_echo)).*

- Runner: `gpio_out`, `gpio_in`, `release` through the trait; `adc` through the machine's analog
  source (finding 10). Future-dated events are queued by `t` in the runner (4.1). `lines` fixture
  with `drive(pin, level, at=)`, `release(pin)`, `events()`; `scenario` fixture as a sorted
  timeline that the backend consumes and that `BoardLink` folds into `quiet_until`.
- Board profile pulls honoured on release.

### M4. The `serial` backend

- `board` parametrised over `esp32sim` and `serial`; the capability table decides skips with a
  reason; pytest-embedded's own `--port` selects the board (no esp32tl duplicate). The `idf`
  service flashes the CI artifact itself at start (`IdfSerial._start`, unless
  `--skip-autoflash`), so the harness does not flash a second time; `--skip-autoflash` stays for
  iterating on a board already carrying the image, and then the check of a recording's
  `app_sha256` against the artifact is the only guarantee that the test runs the image it
  claims to run. `hello_world` and `nmea_reader` run on a real classic devkit (console only; the
  GNSS test is skipped on `serial` with the reason "uart.gnss cannot be modelled on this
  backend"; as built, `serial: uart.gnss takes no replay source on this backend, only real`, from
  [`docs/features/board-profiles-user.md`](features/board-profiles-user.md)).
- A comparison script lists which tests ran on both backends and whether they agreed. This is
  the spec's "every test that a real board can run, runs on one too", so it is its own milestone.

*Update, 2026-11-07:* the comparison is `scripts/compare-backends.py` (#26), which runs pytest once
on each backend and compares the outcomes in the two JUnit XML files
([comparing the two backends](features/serial-backend-user.md#comparing-the-two-backends)).

### M5. The `real` source on the emulator

*Amended by section 11: M6 no longer depends on this milestone.*

*Designed in [`protocol.md`, section 12](protocol.md#12-wall-time---realtime-and-the-real-source-m5)
(#27), which holds where it differs from the text below: the run is on the wall clock from
power-on with no clock switch, the source renews its turn at the next whole emulated millisecond,
and the runner keeps a wall clock of its own for the wait at the block point.*

- Design, written into docs before code: a real device on a host serial port, the runner under
  `--realtime` (`machine.rt.enabled`, public, which is all the CLI flag sets:
  `cli/src/lib.rs:829`), and each received byte stamped with the emulated cycle current at the
  `advance_to` in which it arrived; transmitted bytes leave at their `uart_tx` time, which under
  `--realtime` tracks wall time. The recording header says `clock: wall`. Two more sentences
  belong in that design so they do not surface in the implementation: a real device does not
  know its `quiet_until`, so the `real` source renews `wait` in short slices (one emulated
  millisecond); and at the block point the runner under `--realtime` waits on the socket with a
  timeout until wall time catches up with emulated time, never without bound.
- `sources/real.py`, `gnss.real(port)`, `tap()` on `real`. Validated with a GNSS receiver on a
  USB serial adapter (the first recording from a real receiver replaces nothing: the generated
  recording stays as the deterministic one).
- Not attempted on `serial`: finding 11.

*Update, 2026-11-04:* built in #28 as protocol section 12 has it, and tested on a pseudo-terminal
pair. Validated with `tests/test_real_source.py::test_a_receiver_on_a_usb_adapter`, run by hand: the
L76K of a LilyGO T-A7670G on a balcony, reaching the host through that board's CH9102 at 9600 baud,
gave `nmea_reader` a fix in 45.6 s, with no resync, no byte dropped after the firmware's start, and
541 sentences, all with a valid checksum, in the tap. A small bridge loaded into the board's RAM, which
is not in this repository, joined the L76K's UART to the CH9102's. The firmware transmitted nothing, so
writing to a real port is tested on a pseudo-terminal only. The recording replaces nothing, and holds
where the receiver was, so it is not committed.

### M6. Cellular modem model and PPP

*Superseded in part by section 11: the PPP data phase goes to a network the harness simulates in
emulated time, not to `pppd`; the model is `sources/model/modem.py`; its validation is
`pppos_client` reaching the test's MQTT broker on the LilyGO profile's modem route; and the
scenario events below become the modem model's own calls on the test's clock. The AT subset, the
PWRKEY and RI lines and the tap stay as described here.*

- `sources/model/at_modem.py`: PWRKEY on the GPIO line (power-on pulse length from the profile), RI
  reflecting power, `AT`, `ATE0`, `AT+CPIN?`, `AT+CREG?`/`AT+CEREG?`, `AT+CSQ`, `AT+CGDCONT`,
  `ATD*99#` → `CONNECT`, `+++` back to command mode. The subset grows from what the validation
  firmware sends, recorded by the tap.
- PPP: on `CONNECT` the model hands the byte stream to `pppd` in a container (privileges only
  inside the container, host socket in between), NAT to the host. From that moment the far end
  lives in wall time, which is the `real` source in disguise: the data phase uses M5's stamping
  and runs under `--realtime`, while the AT dialogue before `CONNECT` stays in emulated time and
  the recording switches clock at `CONNECT` (4.3). Without this the emulator would run 15 to 37
  times faster than `pppd` and the firmware's LCP and TCP timers would expire before the
  container answers. Network traffic is never recorded; the AT dialogue and link conditions are,
  and they test deterministically without `pppd`. Tests that reach the data phase carry
  `requires_docker` and are outside the determinism job (section 6).
- Validation: esp-protocols `pppos_client` at a pinned tag reaches a local HTTP server started by
  the test; the test asserts on the server's request log.
- Scenario events: registration lost and regained, signal change, modem power loss.
- First board-specific firmware runs on the LilyGO profile, whose GNSS route, flash and PSRAM
  come from LilyGO's documentation (section 11); the validation firmware does not, it uses the
  devkit profile with a `[uart.modem]` route added.

### M7. Wi-Fi and SD card

- Wi-Fi: the runner exposes esp32sim's `--wifi`/`--wifi-nearby`/`--net` through
  `m.bus.attach_wifi(cfg, nat)` and `StationLink`, both public (the CLI calls them on the classic
  machine from another crate, `cli/src/lib.rs:554`, `esp-soc/src/wifi.rs:490`); the `wifi`
  fixture configures them and puts link events on the scenario timeline. Validation: an IDF
  example that joins and fetches from a local server. *Update, 2026-11-02:* done in #31, with
  section 11's network instead of esp32sim's: the runner's `--wifi` and `--wifi-nearby`, the
  station's frames relayed on the link (`eth_tx`, `eth_rx`) to the harness's network, which gains
  Ethernet, ARP, DHCP and an HTTP server, and the access point switched off and on at the test's
  instant (`wifi_ap`) instead of on a scenario timeline; the access point that leaves
  deauthenticates the station, since esp32sim's station does not notice missing beacons. ESP-IDF's
  `protocols/http_request`, unmodified, is the validation
  ([decisions](decisions/runner.md#the-network-behind-the-access-point-is-the-peers-over-the-link)).
- SD card, main path: a card model implementing `spi_transfer_pins` in the runner's board. The
  classic SPI bus calls it when `uses_spi_pins()` is true (`esp32/src/spi.rs:541-549`) and
  `SpiPins.cs` carries the select mask, so a transfer-level card model is feasible without a core
  change. The FAT image is prepared with `mtools` (`brew install mtools` on macOS; not installed
  here today). The remaining risk is `sdspi`'s DMA path (`SDSPI_DEFAULT_DMA`), which esp32sim
  declares modelled; if it is not, that is the upstream issue to file. Validation:
  `storage/sd_card/sdspi`, then inspect the image with `mcopy`. *Update, 2026-11-01:* done in #32 as
  planned, without a core change: the runner's card (`--sd-rw`, `--sd-spi`), the profile's
  `[sd_card]` slot and `sd_card.insert(image)`. The DMA path is modelled, so there is nothing to
  file; insertion and removal on the scenario timeline wait for the scenario
  ([decisions](decisions/harness.md#inserting-and-removing-the-sd-card-while-the-firmware-runs-is-postponed)).

### M8. Bluetooth (HCI)

Blocked on the HCI transport (finding 9). Work that can start earlier: the Bumble-based peer
(`bumble` 0.0.236 on PyPI) and SPP payload replay through the matching engine, both testable against
a Bumble virtual link without the emulator. Then:

- Upstream design issue for an HCI socket on the classic chip, after #325 settles.
- Choose Root Canal vs Bumble's own virtual controller once the HCI socket exists.
- btsnoop tap; `bt.model(peer)`, `bt.replay(path)`.
- Validation: `bt_spp_initiator` against a modelled and a replayed peer; real path with
  `controller_hci_uart_esp32` on a second board.

*Update, 2026-11-01:* the Bumble-based peer is done in #33, without the emulator: `SppPeer` on an
`Air` whose loop runs on the board's clock, answered by a `Source` as a UART's far end is, rather
than replayed through the matching engine, which waits for #20. Bumble 0.0.236's virtual controller
has no inquiry, legacy pairing or Classic encryption, so esp32tl's controller adds them
([decisions](decisions/harness.md#esp32tl-adds-inquiry-legacy-pairing-key-checks-and-encryption-to-bumbles-virtual-controller));
the controller for the ESP32's side is still #34's to choose.

*Update, 2026-11-08:* #34 chose it: the runner forwards the guest's HCI to the harness over the
board socket, as `hci_tx` and `hci_rx`, so that the guest's controller is the harness's, on the
board's clock
([decision](decisions/runner.md#the-guests-hci-goes-over-the-board-socket-on-the-boards-clock)).
The harness's side followed: the `bt` fixture, the firmware's controller on the air of the test's
devices, on the board's clock
([decision](decisions/harness.md#the-firmwares-controller-is-a-controller-of-the-air-with-the-guests-features-and-buffer)).
The btsnoop tap and the validation with `bt_spp_initiator` are still to come.

*Update, 2026-11-10:* the validation with `bt_spp_initiator` came with the pin at
mikroverk/esp32sim#347 (#174), against a modelled peer; and the btsnoop tap with `bt.tap(path)`,
the header in a sidecar `.json`
([decision](decisions/harness.md#the-hci-tap-is-a-btsnoop-file-on-the-boards-time-with-its-header-beside-it)).
`bt.model(peer)` became `SppPeer(bt.air, ...)`; `bt.replay(path)` and the replayed peer wait for
#20. Peers have Secure Simple Pairing too, given an IO capability
([decision](decisions/harness.md#secure-simple-pairing-is-bumbles-with-esp32tls-numbers-key-types-and-failures)).

### M9. Deep sleep and time skip

- Upstream issue for the missing timer wake (esp32sim notes LACT sleep-time stepping as not
  modelled). Patch on the branch if declined. *Update, 2026-11-08:* reported upstream as
  mikroverk/esp32sim#331 from another firmware, fixed in mikroverk/esp32sim#336 and pinned in #35
  ([stage 0, gap 4](stage0-findings.md#gaps)); no patch branch.
- `scenario.skip_to_next_wake()`; validation with `system/deep_sleep`. A deep sleep that does not
  wake ends in `Stop::Halted` at `max_cycles`, a normal end, so the test asserts on the wake-up
  itself (the post-wake console line) and treats a run that only halts as a failure; otherwise an
  emulator that does not wake passes silently. `tests/test_timer_wake.py` does so since #35.
  *Update, 2026-11-08:* the call is `board.wait_for_wake(within=...)` on the test's clock (#36;
  there is no `scenario` since #64), and `tests/test_deep_sleep.py` runs ESP-IDF's
  `system/deep_sleep` through two sleeps with it
  ([decisions](decisions/harness.md#the-wait-for-a-wake-from-deep-sleep-is-a-board-call-that-asks-for-a-turn-after-each-event)).

### M10. Upstreaming and stabilisation

- React to #325: replace the `SocketBoard` half of the runner or keep it; `BoardLink` stays
  either way.
- Propose upstream an ADC hook on `BoardModel` (or a socket-driven equivalent of the `adc` script
  action) and a route-configuration hook for `uart_cfg`; until both exist the runner's *ext* half
  stays, which is acceptable and is said so in the README.
- pytest-embedded PR: registry entry and `pytest-embedded-esp32sim` package extracted from the
  plugin.
- Prebuilt runner binaries per OS in GitHub Releases, so `pip install esp32tl` plus one download
  is enough without cargo.
- Docs: protocol, recording format, board profile format, writing a model, writing a product model
  in a consumer's tree.

## 6. Test strategy

| Layer | What | How |
|---|---|---|
| Unit (Python) | matching engine, recording I/O, protocol codec, board profiles, scenario ordering | plain pytest, no emulator |
| Unit (Rust) | `SocketBoard` pacing, deadline arithmetic, reset forwarding, late-`t` error, boot after first `wait` | `cargo test` with a fake peer |
| End-to-end, emulator | each validation firmware against model and replay | pytest with the `esp32sim` backend, runs in CI |
| End-to-end, real board | the subset the capability table allows | pytest with `serial`, manual or self-hosted runner |
| Determinism | same recording twice gives identical console output and identical tap | CI job diffing two runs; holds because of protocol rule 2. After a TLS handshake only the plain-text traffic is compared, not its times (section 11) |

CI matrix: firmware built once on the self-hosted Linux runner per IDF tag and handed on through
the Actions cache; tests on self-hosted Linux and macos-latest with Python 3.11, 3.13 and 3.14;
the runner built once per revision of its sources and esp32sim pin, and cached; container-backed
tests run on Linux only. *Amended by section 11:* with the network simulated in the harness, no
planned test needs a container; Docker is for building firmware only.

## 7. Risks and what we do about them

| Risk | Mitigation |
|---|---|
| #325 declined or stalled | The runner is the main path already; `BoardLink` is the only consumer of the protocol |
| #325 accepted but ADC and route configuration stay outside the trait | The runner's *ext* half remains; proposed upstream in M10; documented as a permanent small crate if declined |
| HCI transport never lands upstream | Bluetooth stays on the patch branch; everything else ships without it |
| Fat-LTO release builds in CI are slow | Measured in #8: a cold build takes 44 s on the self-hosted runner and 20 s on macos-latest; the runner binary is cached by revision, and a `dev` profile skips the 10 s fat-LTO re-link when iterating locally |
| No Docker on GitHub-hosted macOS runners | Firmware built on the self-hosted Linux runner and shared through the Actions cache. No planned test needs a container since section 11 |
| A minimal TCP in Python misses what a device's stack does | The link reorders nothing and loses a byte only when the firmware leaves its receive FIFO full, which the runner reports as `rxfifo_ovf` and which then fails the run; checked against lwIP in two images (section 11) |
| Wall-time to emulated-time stamping for `real` has no upstream design | Own design in M5, written before code; `clock: wall` recordings never compared with emulated ones |
| A real board shares a USB port with other sessions | `serial` tests off by default, selected with `--esp32tl-backend serial` |
| Timing of emulator recordings misread as wire timing | `clock` header field, refused on cross-clock comparison |
| ESP-IDF version drift between local (v5.5.5) and pinned (v5.3.6) | Goldens come from the container build only |
| Python peer latency makes `test_time_is_emulated` flaky | Threshold with margin (five wall seconds for ten emulated seconds) |

## 8. Environment checked on this machine (2026-10-29)

| Item | State |
|---|---|
| Python | 3.14.6 (`pytest-embedded` not installed yet) |
| Rust | cargo 1.93.0, too old for esp32sim at the pin: `esp-soc/src/nat.rs` calls `AtomicUsize::try_update`, stable since Rust 1.95. Stage 0 and the pin's recheck renamed it to `fetch_update` locally. The runner pins 1.99.1, the stable release it was first built with, in `runner/rust-toolchain.toml` (#5) |
| ESP-IDF | v5.5.5 at `~/esp/esp-idf`, xtensa toolchain present |
| ROM ELFs | `~/.espressif/tools/esp-rom-elfs/20241011/esp32_rev300_rom.elf` present; newer releases `20260313` and `20260528` exist, `20241011` is pinned |
| IDF examples | `hello_world`, `bt_spp_initiator`, `sd_card/sdspi`, `deep_sleep`, `controller_hci_uart_esp32` present; `pppos_client` is in esp-protocols |
| Docker | 24.0.2 (Docker Desktop) |
| `pppd` | `/usr/sbin/pppd` (needs root; not used directly) |
| `mtools` | not installed |
| esp32sim | not installed; builds from source in M0 |

## 9. Order of work for the first two weeks

1. M0 in full, including the two-job CI shape (firmware artifact, then tests).
2. M1: runner `SocketBoard` and its Rust fake-peer test first (the riskiest piece of M1, and the
   one that fixes protocol rules 2 and 4 in code), then `nmea_reader`, then the Python side, then
   the three tests.
3. Open the upstream conversation on #325 with a link to the working runner, so the review has
   something concrete to look at.

If the two weeks run short, the piece to drop is the prebuilt firmware artifact in CI: a local
container build is enough for M1, and the artifact job returns before M2.

## 10. Amendments to carry back to issue #1

The review of the first draft asked for three more amendments beyond section 1's first nine. Text
proposed for the issue, to be posted once agreed:

1. **ADC.** "ADC is not a `BoardModel` hook; the emulator takes analog input through the machine's
   script actions. esp32tl drives it from the runner outside the socket transport, and proposes a
   hook upstream after #325. The runner therefore shrinks rather than disappears when #325 lands."
2. **`real` on the emulator.** "A real device at the far end of an emulated interface needs a
   wall-time to emulated-time stamping rule, which #325 defers. esp32tl designs it in its own
   milestone (after the `serial` backend) and records such sessions with `clock: wall`. On a real
   board only the console can be tapped; on-board peripherals would need a hardware sniffer, which
   is out of scope."
3. **First-milestone board.** "The first milestone runs on a generic `esp32-devkitc` profile with
   a GNSS route chosen for the repository's own `nmea_reader` firmware (UART1, GPIO 4/5). The
   LilyGO T-A7670G R2 profile takes the modem route and lines from the spec and, from LilyGO's
   documentation, the GNSS route of the board's GPS version (UART2, GPIO 21/22), 4 MB of flash and
   PSRAM; the rest comes when a test needs it. The modem's validation firmware runs on its modem
   route." *Amended by section 11, before this text was posted.*

## 11. Amendments from the test-clock and network spike (2026-10-31)

A spike ran the production release image of [stage 0](stage0-findings.md#inputs), unmodified,
under esp32tl, with a modelled GNSS receiver and a simulated network, and turned up how tests should
read and what the network takes. The decisions, with what they rejected and the measurements behind
them, are in [`decisions/harness.md`](decisions/harness.md):

- [The board's clock is the test's](decisions/harness.md#the-boards-clock-is-the-tests-emulated-time-moves-only-while-the-test-waits):
  `board.power_on()`, `board.wait()`, `board.wait_until()`, `board.now`, and `console.expect`
  timed on the board's clock. This takes over the timeline part of M3's `scenario` fixture.
- [A run starts at `board.power_on()`, and so does the runner](decisions/harness.md#a-run-starts-at-boardpower_on-and-so-does-the-runner),
  and [the test's own timeout catches a hung run](decisions/harness.md#the-tests-own-timeout-catches-a-hung-run-not-the-runners-peer-timeout).
- [What is at a route's far end is what the test does with it](decisions/harness.md#what-is-at-a-routes-far-end-is-what-the-test-does-with-it-before-power-on-not-a-marker),
  not a `source` marker.
- [Models describe the world in its own terms, in units](decisions/harness.md#models-describe-the-world-in-its-own-terms-in-units):
  the GNSS receiver model (`sources/model/gnss.py`) comes before M2, and a units module with it.
  Models are named after their device, so section 3's `nmea.py` and `at_modem.py` become
  `gnss.py` and `modem.py`; they share one world time, which NTP tells too.
- [Tests observe the device from outside](decisions/harness.md#tests-observe-the-device-from-outside-and-product-knowledge-stays-in-the-consumers-tests);
  a consumer's vocabulary lives in its own tests.
- [The network is simulated in the harness](decisions/harness.md#the-network-the-device-reaches-is-simulated-in-the-harness-in-emulated-time):
  PPP, IPv4, UDP, an accept-only TCP, DNS, NTP, TLS with a CA made per test, and an MQTT broker,
  in `esp32tl/net/`. This replaces M6's `pppd` data phase, and with it M6's dependence on M5,
  Docker and `--realtime`.
- [Flash contents go into a partition of the build's own table](decisions/harness.md#flash-contents-besides-the-build-go-into-a-partition-of-the-builds-own-table-by-name):
  `board.nvs({...})` first.
- [The LilyGO profile takes its GNSS route, flash and PSRAM from LilyGO's documentation](decisions/harness.md#the-lilygo-profile-takes-its-gnss-route-flash-and-psram-from-lilygos-documentation),
  amending finding 12.

What the spike could not reach, and which milestones it moves forward:

- Firmware that keeps what it collects on an SD card may lose it without one, so what such
  firmware sends to its cloud may be testable only with M7's card model.
- Firmware may confirm a modem's power-off on RI, so the modem model needs M3's `gpio_out` and
  `gpio_in` before it can model power.
- A modem model ignores what is not a command in command mode: firmware may send `+++` there to
  be sure it is out of data mode, and answering it shifts every later answer by one.
- A device that checks a certificate's dates needs the calendar first and may wait for NTP before
  TLS, as the measured image does, so the simulated network serves NTP from the start.
- The recording format's clock switch ([`recording-format.md`](recording-format.md#clock-switches))
  names M6's data phase as its use. M6 no longer switches clocks, so the modem's PR corrects that
  reference, and the switch stays for the `real` source of M5.

Order of work after M1. Section 9 covered M0 and M1; what the list below does not take from
section 5 follows it in that section's order:

1. The test's clock, `power_on()` with the runner started there, the console on the clock, the
   test's timeout, and routes without the `source` marker: one change to the backend and the
   plugin, with `tests/` moved to `power_on()`.
2. Units and the GNSS receiver model, validated on `nmea_reader`.
3. The LilyGO profile's GNSS route, flash and PSRAM, and `--psram-mb` from the profile.
4. `board.nvs()`.
5. The modem model and the simulated network, validated on `pppos_client` from esp-protocols at
   `modem-v1.4.1` on the LilyGO profile's modem route.
6. M3's `gpio_out` and `gpio_in` for the modem's PWRKEY and RI, then M7's SD card model.
