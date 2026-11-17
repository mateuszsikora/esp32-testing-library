# Board profiles: how they work

The loader is `esp32tl/boards/__init__.py`, the profiles are the `*.toml` files beside it, and the
capability declaration is `esp32tl/boards/capabilities.toml`, read by
`esp32tl/boards/capabilities.py`. The [pytest plugin](pytest-plugin-technical.md) uses both. What
the user sees is in [board-profiles-user.md](board-profiles-user.md).

## Data flow

1. **`pytest_generate_tests`**, for a test that uses `board`, after the `app_path` marker:
   `profile_reference(metafunc.definition)` reads the closest `board_profile` marker once: a
   bundled name, `DEFAULT_PROFILE` (`esp32-devkitc`) without a marker, or for `path=` the resolved
   `node.path.parent / path`. `load_profile(config, reference)` loads that same reference, a name
   through `boards.profile(name)`, a path through `boards.load`, cached by absolute `Path` in
   `config.stash[LOCAL_PROFILES]` for the session. An unknown name that contains `/` or ends in
   `.toml` gets `; for a local file, use board_profile(path="NAME")` appended to its error. Relative paths always use the test file, even for inherited markers. A `MarkerError` or
   `ProfileError` becomes `pytest.fail.Exception(f"{nodeid}: {error}", pytrace=False)`, raised
   `from None`: a collection error without the chained exception.
2. **`board` fixture**, at setup: `board_profile(request.node)` retrieves the saved
   `BoardMarkers.loaded_profile` through `collected_board_markers`, without resolving the path
   again. That object is passed to `Esp32Sim(..., profile)` or `Serial`.
   The simulator keeps it as
   `Esp32Sim.profile` and gives its `name` and `chip` to the `BoardLink`, for the harness's `hello`
   ([esp32sim-backend-technical.md](esp32sim-backend-technical.md#data-flow)). Every `uart.*` route
   becomes a `Uart` on the link, its `Wire` the route's `tx`, `rx` and `baud`; every line of every
   `[lines.<role>]` set is on one `Lines`, with its `pull`, which the link drives on the line from
   power-on ([pytest-plugin-technical.md](pytest-plugin-technical.md#data-flow)). An `[sd_card]`
   slot becomes an empty `SdCard`, the `sd_card` fixture.
3. **A source or a tap, in the test body.** `gnss.replay(path)` asks the backend, through the
   `Uart`'s `asked`, for `Request("uart.gnss", source="replay")`, the first call of the receiver
   model, as `gnss.fix(at=...)`, for `Request("uart.gnss", source="model")`, `gnss.tap(path)`
   for `Request("uart.gnss", tap=True)`, and `sd_card.insert(image)` and `sd_card.remove()`,
   through the `SdCard`'s, for `Request("sd_card", source="model")`, before or after power-on.
   `Esp32Sim._asked`, and for the slot `Esp32Sim._declared`, runs `capabilities.check([request], ...)`
   (a `CapabilityError` becomes a `SourceError` with its message), then
   `capabilities.refusal(Esp32Sim.name, [request], ...)`, whose reason skips the test at the call
   ([esp32sim-backend-technical.md](esp32sim-backend-technical.md#data-flow)). It reads the
   declaration through `capabilities.declared()` each time.

`boards.profile(name)` checks `name` against `names()`, the stems of the `*.toml` files in
`PROFILES` (`esp32tl/boards/`) except `capabilities.toml`, then `load(PROFILES / f"{name}.toml")`.
It is `functools.cache`d, so every test gets the same object; the mappings in it are
`MappingProxyType`s and the dataclasses are frozen, so no test can change what the next one gets.
`declared()` is cached the same way.

Local profiles do not enter that name cache or `names()`. Different resolved paths stay separate
even if their TOML names or contents match. The session cache preserves the validated snapshot
if a file changes or disappears between collection and setup. `BoardMarkers.loaded_profile`
retains that object for each definition and its parametrised items, so retargeting a symlink
after collection's marker checks cannot select another profile at setup. `BoardMarkers.profile`
holds a bundled name (`str`) or resolved local path (`Path`); `late_board_marker` compares
`profile_reference(item)` against it, without loading a new file. Reports, handshakes and
recording headers continue to carry `BoardProfile.name`, and UART recordings use that profile's
wire metadata. Paths are selection identities within a session, not new recording fields.

`load(path)` reads the UTF-8 file with `tomllib` and checks it key by key with `Reader`, in the
order of [Schema](#schema). File access, UTF-8 decoding and TOML parsing errors become
`ProfileError` with the filename. The first error raises `ProfileError` and loading stops.

## Schema

### A profile: `<name>.toml`, bundled or local

| Key | Type | Required | Rule |
|---|---|---|---|
| `name` | string | yes | The file's name without `.toml` |
| `chip` | string | yes | One of `CHIPS`, the chips the runner emulates: `esp32` (the classic chip), `esp32s3`, `esp32c3`, `esp32c6`. The chip decides the GPIOs, the UARTs and the console's pins below ([Chips](#chips)) |
| `flash_mb`, `psram_mb` | integer | no | At least 0; absent is "not known", `None` in `BoardProfile`. The esp32sim backend gives the runner `psram_mb` as `--psram-mb` when it is set ([esp32sim-backend-technical.md](esp32sim-backend-technical.md#data-flow)), and the runner refuses a size the chip does not take (0 or 4 on the classic ESP32; 0, 2, 4 or 8 on the ESP32-S3; 0 on the ESP32-C3 and ESP32-C6). `flash_mb` is not passed on: the runner's flash is its default, 4 MiB |
| `[uart.<role>]` | table | no | One per UART route; keys below |
| `[lines.<role>]` | table | no | One per set of GPIO lines; keys below |
| `[sd_card]` | table | no | The board's SD card slot, wired for SPI mode; keys below |

No other key is allowed, at any level. A role, and a line's name, matches `[a-z][a-z0-9_]*`.

`[uart.<role>]` takes exactly these keys:

| Key | Rule |
|---|---|
| `port` | A UART of the chip (0 to `Chip.uarts - 1`: 0 to 2, or 0 to 1 on the C3 and the C6), and not `CONSOLE_PORT`, 0 |
| `tx` | A GPIO of the chip that can drive an output: in `Chip.gpios` and not in `Chip.input_only` |
| `rx` | A GPIO of the chip: in `Chip.gpios` |
| `baud` | At least 1 |

`[sd_card]` takes exactly `sclk`, `mosi`, `miso` and `cs`, the GPIOs of the slot as the chip sees
them: `sclk`, `mosi` and `cs` can drive an output (not in `Chip.input_only`), `miso` is any GPIO
of `Chip.gpios`. A board has one slot at most, and the interface is `sd_card`, with no role.

`[lines.<role>]` maps a line's name to its GPIO, either as an integer (`pwrkey = 4`) or as a table
`{ pin = 33, pull = "down" }`, where `pull` is `up` or `down` and may be left out. A bare integer
says nothing about the board's pull. On `esp32sim` a pull is a level the harness drives on the line
while the test does not, which the firmware's own output wins over.

Across the profile, no two routes share a `port`, and no GPIO serves two uses (a route's `tx` and
`rx`, a line, a pin of the slot). The `console` interface is not in the file, because every board has it, but it
takes its share: UART0 and its IO MUX pins, `CONSOLE_PORT` and the chip's `Chip.console_pins`,
on the classic ESP32 GPIO 1 (`console.tx`) and GPIO 3 (`console.rx`). GPIO 6 to 11, which most modules give to the SPI flash, are not
refused
([why](../decisions/harness.md#a-profile-reserves-the-consoles-uart-and-pins-not-the-flash-pins)).
Integers are TOML integers: `true` is not a port.

### Pulse timing on a line

A line table may also contain `active`, `on_ms` and `off_ms`, all together, for a line whose pulse
toggles, or `active` and `pulse_ms`, for a line with one threshold. `active` is integer
0 or 1; durations are positive integer milliseconds. A timed line must be on an output-capable
ESP32 pin. `Reader.lines` stores them on `Line`, defaulting all four to `None` for untimed lines.
Any other set fails with `lines.modem.pwrkey: pulse timing needs active with on_ms and off_ms, or active with pulse_ms`
(prefixed by the profile filename); invalid types and ranges use the usual key-specific errors.

The LilyGO PWRKEY is `{ pin = 4, active = 1, on_ms = 50, off_ms = 2500 }`, with threshold
provenance in [modem-user.md](modem-user.md#pwrkey-and-reporting), and its RESET
`{ pin = 5, active = 1, pulse_ms = 2000 }`, with provenance in
[modem-user.md](modem-user.md#a-hung-modem-and-reset). They describe the
ESP32 pad polarity. The modem model consumes these timings; `Lines` itself interprets no pulse.
`tests/test_boards.py::test_pulse_timing_is_checked_as_a_complete_output_line` covers the schema.

### LilyGO wiring audit

`lilygo-t-a7670g-r2.toml` describes the classic ESP32 A7670G board with the external L76K,
using LilyGO revision `63af963038e93c91baf39d89a3a736d6bc26ff13` throughout:

- [ESP32 T-A7670X pin table and product specification](https://github.com/Xinyuan-LilyGO/LilyGO-T-A76XX/blob/63af963038e93c91baf39d89a3a736d6bc26ff13/docs/en/esp32/a7670-esp32/README.MD#-pins-map).
- [ExternalGPS_A7670G_Only example](https://github.com/Xinyuan-LilyGO/LilyGO-T-A76XX/blob/63af963038e93c91baf39d89a3a736d6bc26ff13/examples/ExternalGPS_A7670G_Only/ExternalGPS_A7670G_Only.ino#L20-L35),
  explicitly restricted to ESP32 A7670G plus L76K, not the S3 series.
- [T-A7670X V1.4 schematic](https://github.com/Xinyuan-LilyGO/LilyGO-T-A76XX/blob/63af963038e93c91baf39d89a3a736d6bc26ff13/schematic/esp32/T-A7670X-V1.4.pdf),
  whose ESP32 sheet labels the SD nets GPIO13/CS, GPIO15/MOSI, GPIO14/CLK and GPIO2/MISO.

| Profile entry | Verified assignment | Evidence |
|---|---|---|
| `sd_card` | SCLK 14, MOSI 15, MISO 2, CS 13 | Pin table, example's `BOARD_SDCARD_*`, schematic SD net labels |
| `lines.modem.dtr` | 25 | Pin table and `BOARD_MODEM_DTR_PIN` |
| `lines.modem.reset` | 5, active high, 2000 ms | Pin table's modem RESET and example's `BOARD_RST_PIN`; polarity from LilyGO's `MODEM_RESET_LEVEL HIGH`, threshold from SIMCom's table 12 |
| `lines.gnss.wake` | 19 | GPS-only pin table and `BOARD_GPS_WAKEUP_PIN` |
| `lines.gnss.pps` | 23 | GPS-only pin table and `BOARD_GPS_PPS_PIN` |
| `lines.board.power_enable` | 12 | Pin table's peripheral power control and `BOARD_POWER_ON_PIN` |

The pin table's upload warning calls GPIO2 CS, contradicting its own table, the example and
schematic, which agree on MISO=2 and CS=13. The profile uses the latter mapping. The same
README links V1.4 and discusses ESP32 V1.x; it does not establish which PCB revision the
repository's R2 name denotes. The example holds GPIO5 low during setup, and LilyGO's ATdebug
example pulses it high for 2600 ms; the profile takes polarity and threshold from these and SIMCom's
table 12, which do not establish them across revisions. GPIO12's physical power
control and GPIO19's documented wake/standby behavior are not implemented as model effects.
GPIO 12, 5, 2 and 15 are strapping pins; esp32sim at the pinned revision sets the strapping
register to a constant (`gpio.strap = 0x13` in `esp32/src/periph.rs`), so their levels at reset
change nothing in the emulator, whereas GPIO 12 high at reset breaks the WROVER-E's boot.
TODO(maintainers): match the R2-labelled hardware to a manufacturer schematic and verify its
reset polarity, pulls and power sequencing; the model takes RESET's polarity from LilyGO's example.

Four of the five new lines are bare integer entries, and `modem.reset` carries its pulse timing.
`Lines` watches and exposes them without assigning default pulls, or generating PPS. The modem
interface attaches its PWRKEY and RESET observers and RI driver only; GNSS models generate UART messages independently
of these GPIOs. The slot still passes `--sd-spi sclk=14,mosi=15,miso=2,cs=13` to the runner at
power-on, and `--sd-rw` with a card in it, with no dependency on GPIO12. No schema or protocol change is needed.
The physical 8 MB PSRAM remains represented by `psram_mb = 4`, the runner's supported maximum;
`flash_mb = 4` and both UART routes remain as documented in the [user guide](board-profiles-user.md).

### The capability declaration: `esp32tl/boards/capabilities.toml`

One table per backend, keyed by the backend's name in `SERVICES`; in it, one inline table per
interface kind, where the kind of `uart.gnss` is `uart` and that of `console`, `sd_card`, `wifi`
or `bluetooth` is the interface itself:

| Key | Type | Default |
|---|---|---|
| `sources` | list of `SOURCES`: `model`, `replay`, `real` | `[]` |
| `tap` | boolean | `false` |

A kind is one of `KINDS`: `console`, `uart`, `lines`, `sd_card`, `wifi`, `bluetooth`. A kind a
backend does not list is `Capability()`: no source, no tap. The file declares `esp32sim` and
`serial`:

```toml
[esp32sim]
console = { tap = true }
uart = { sources = ["model", "replay", "real"], tap = true }
sd_card = { sources = ["model"] }
wifi = { sources = ["model"] }
bluetooth = { sources = ["model"], tap = true }

[serial]
console = { tap = true }
uart = { sources = ["real"], tap = false }
sd_card = { sources = ["real"] }
wifi = { sources = ["real"] }
```

The `model` on `sd_card` is the runner's card model holding an image file
([runner-technical.md](runner-technical.md#the-sd-card)); on `serial` it is the card in the slot.
The `model` on `wifi` is esp32sim's access point (`--wifi`) with the harness's network behind it;
`wifi.access_point` and `wifi.nearby` ask for it, so on `serial` they skip. The `model` on
`bluetooth` is the firmware's controller in the harness, behind the runner's `--bt`
([bluetooth-peer-technical.md](bluetooth-peer-technical.md)); the `bt` fixture asks for it, and
`serial` lists no `bluetooth`, so there it skips. Its tap is the HCI between the firmware's host
and that controller, which `bt.tap(path)` asks for and writes as btsnoop
([run-report-technical.md](run-report-technical.md#the-hci-tap)).

`serial` is in `SERVICES` with services `esp,idf`; tests select it explicitly with
`--esp32tl-backend serial`. Both backends are counted by `check`.

### Chips

`CHIPS` maps each chip a profile may name to a `Chip`: `name`, `title` (in messages), `gpios`,
`input_only`, `uarts` and `console_pins`; `Chip.console_pin("tx")` and `("rx")` give UART0's pins.
Every pad the chip has counts, the flash's and the PSRAM's included, as on the classic ESP32.

| `chip` | `gpios` | `input_only` | `uarts` | `console_pins` (TX, RX) | Source |
|---|---|---|---|---|---|
| `esp32` | 0 to 39 without 24 and 28 to 31 | 34 to 39 | 3 | 1, 3 | ESP32 datasheet |
| `esp32s3` | 0 to 48 without 22 to 25 | none | 3 | 43, 44 | ESP32-S3 datasheet, section 2.3 |
| `esp32c3` | 0 to 21 | none | 2 | 21, 20 | ESP32-C3 datasheet, section 2.2 |
| `esp32c6` | 0 to 30 | none | 2 | 16, 17 | ESP32-C6 datasheet, section 2.3 |

The same pads are esp32sim's own pin tables at the pin (`esp-soc/src/pins.rs`, `ChipPins`), which
the runner checks the link's `watch` against ([protocol 5.1](../protocol.md#51-hello)).

## Interfaces

| Name | Kind | What |
|---|---|---|
| `boards.profile(name)` | function, cached | The shipped profile; `ProfileError` for another name |
| `boards.load(path)` | function | The profile in any file, checked against [Schema](#schema) |
| `boards.names()` | function | The shipped profiles' names, sorted |
| `boards.DEFAULT_PROFILE` | `str` | `"esp32-devkitc"` |
| `BoardProfile` | frozen dataclass | `name`, `chip`, `flash_mb: int \| None`, `psram_mb: int \| None`, `uart: Mapping[str, UartRoute]`, `lines: Mapping[str, Mapping[str, Line]]`, `sd_card: SdCardRoute \| None = None`; property `interfaces`: `["console", "uart.<role>", ..., "lines.<role>", ..., "sd_card", "wifi", "bluetooth"]`, in file order, `sd_card` only with a slot, `wifi` and `bluetooth` always: every chip of `CHIPS` has them. Not hashable: its mappings are not |
| `UartRoute` | frozen dataclass | `port`, `tx`, `rx`, `baud` |
| `SdCardRoute` | frozen dataclass | `sclk`, `mosi`, `miso`, `cs` |
| `Line` | frozen dataclass | `pin`, `pull: "up" \| "down" \| None = None`, `active`, `on_ms`, `off_ms`, `pulse_ms` (each `int \| None = None`); [pulse schema](#pulse-timing-on-a-line) |
| `capabilities.declared()` | function, cached | The shipped declaration: backend name, then kind, then `Capability` |
| `capabilities.load(path)` | function | A declaration in any file |
| `Capability` | frozen dataclass | `sources: tuple[str, ...] = ()`, `tap: bool = False` |
| `Request` | frozen dataclass | `interface`, `source: str \| None = None`, `tap: bool = False`, `role: str \| None = None`: what the source is for when an interface has more than one, `station` for `wifi.station`; `check` and `refusal` do not read it |
| `capabilities.check(requests, profile, capabilities)` | function | Raises `CapabilityError` for a request the profile or every backend refuses |
| `capabilities.refusal(backend, requests, capabilities)` | function | The reason, without the backend's name, or `None`; `KeyError` for an undeclared backend |
| `board_profile(node)` | function, `esp32tl/pytest_plugin.py` | The `loaded_profile` collection saved for the test's definition; never reads the marker or the file |
| `load_profile(config, reference)` | function, `esp32tl/pytest_plugin.py` | Loads a bundled name or a resolved local path, local ones once per session in `config.stash[LOCAL_PROFILES]`; `ProfileError` prefixed with the path for a bad local file |
| `profile_reference(node)` | function, `esp32tl/pytest_plugin.py` | The closest marker's bundled name (`str`) or resolved local path (`Path`); validates exactly one positional string or `path=` string/`Path` |
| `BoardMarkers` | frozen dataclass, `esp32tl/pytest_plugin.py` | The `app_path` arguments and keywords, the `app_recording` arguments, the profile's bundled name or resolved path, and `loaded_profile`, the validated object; kept by definition nodeid in `config.stash[READ]` |
| `collected_board_markers(config, node)` | function, `esp32tl/pytest_plugin.py` | The saved `BoardMarkers` for a definition or parametrised item, or `None` before collection saves it |
| `late_board_marker(config, item)` | function, `esp32tl/pytest_plugin.py` | Which board marker an item carries otherwise than its definition did, or `None` |

The marker, `board_profile(name)` or `board_profile(path=...)`, is registered in `pytest_configure`
with the plugin's other markers
([pytest-plugin-technical.md](pytest-plugin-technical.md#interfaces)).

## Failure modes

Profile errors, `ProfileError`, are `<file>: <key>: <what>`, and `<file>: <what>` for the
top level; at collection they come after
`<test id>: `. Local-file errors also carry the resolved path before the loader's message.
The ones `tests/test_boards.py` checks, verbatim:

| Message | Cause |
|---|---|
| `no board profile named 'NAME'; the profiles are esp32-devkitc, gps-alarm-clock, lilygo-t-a7670g-r2, sd-data-logger` | `boards.profile` with another name |
| `x.toml: <tomllib's message>`, as `x.toml: Invalid value (at end of document)` | Not TOML, or not readable |
| `x.toml: 'utf-8' codec can't decode byte ...` | Invalid UTF-8; the decoder's message gives the byte and position |
| `x.toml: missing chip`, `x.toml: unknown key pins` | A top-level key missing or unknown |
| `x.toml: uart.gnss: missing baud`, `x.toml: lines.modem.ri: missing pin` | The same in a table |
| `x.toml: name: 'y' is not the file's name, 'x'` | `name` differs from the file's name |
| `x.toml: chip: 'esp32p4' is not one of esp32, esp32s3, esp32c3, esp32c6` | Another chip |
| `x.toml: uart: is not a table`, `x.toml: sd_card: is not a table` | `uart`, `lines` or `sd_card`, or a role in them, is not a table |
| `x.toml: sd_card: missing cs`, `x.toml: sd_card: unknown key cd` | A pin of the slot missing, or another key |
| `x.toml: sd_card.cs: GPIO 36 is input only` | `sclk`, `mosi` or `cs` on an input-only pin, 34 to 39 on the classic ESP32 |
| `x.toml: sd_card.mosi: GPIO 4 is uart.gnss.tx already`, `x.toml: sd_card.cs: GPIO 13 is sd_card.miso already` | A pin of the slot with another use |
| `x.toml: psram_mb: '4' is not an integer`, `x.toml: uart.gnss.port: True is not an integer` | Not a TOML integer |
| `x.toml: flash_mb: -4 is out of range, at least 0`, `x.toml: uart.gnss.port: 3 is out of range, from 0 to 2` | Out of range; a GPIO above the chip's highest too (`from 0 to 39` on the classic ESP32, `from 0 to 21` on the C3); a UART2 on the C3 or the C6 (`from 0 to 1`) |
| `x.toml: uart.gnss.rx: the ESP32 has no GPIO 24`, `x.toml: uart.gnss.rx: the ESP32-S3 has no GPIO 22` | A GPIO the chip does not have within its range: 24 or 28 to 31 on the classic ESP32, 22 to 25 on the S3 |
| `x.toml: uart.gnss.tx: GPIO 34 is input only` | A `tx` on an input-only pin, 34 to 39 on the classic ESP32 |
| `x.toml: uart.GNSS: a role is lowercase letters, digits and _` | A role or a line's name outside `[a-z][a-z0-9_]*` |
| `x.toml: lines.modem.ri.pull: 'high' is not one of up, down` | Another pull |
| `x.toml: uart.modem.port: UART1 is uart.gnss's`, `x.toml: uart.gnss.port: UART0 is console's` | Two routes on one UART, or a route on the console's |
| `x.toml: lines.modem.pwrkey: GPIO 5 is uart.gnss.rx already`, `x.toml: uart.gnss.tx: GPIO 1 is console.tx already` | One GPIO with two uses, the console's pins counted |

Declaration errors, `CapabilityError` from `capabilities.load`, are `<file>: <backend>[.<kind>]:
is not a table`, `...: unknown key <key>`, `...: no such kind of interface; the kinds are console,
uart, lines, sd_card, wifi, bluetooth`, `....sources: is not a list of names`, `....sources: 'modle' is not one of model,
replay, real` and `....tap: is not true or false`. The request errors of `check` and the skip reasons of `refusal`
are listed in [board-profiles-user.md](board-profiles-user.md#when-no-backend-can-give-it) and
[above it](board-profiles-user.md#when-a-backend-cannot-give-it).

## Tests

`tests/test_boards.py`, plain pytest:

- `test_the_shipped_profiles_include_the_example_assembly`, `test_esp32_devkitc_routes_the_gnss_on_uart1_gpio_4_and_5`:
  the names, the default, and `esp32-devkitc` field by field, its slot included.
  `test_an_sd_card_slot_may_take_an_input_only_pin_for_miso`: a slot's MISO on GPIO 39.
- `test_the_lilygo_profile_holds_the_specification_and_lilygo_s_documentation`: the file's TOML
  data compared whole, including SD wiring and the five untimed control lines.
- `test_a_profile_is_loaded_once_and_cannot_be_changed`, `test_an_unknown_profile_name_lists_the_profiles`,
  `test_a_profile_without_sizes_or_interfaces_has_only_the_console`.
- `test_a_profile_that_breaks_the_schema_is_refused_with_the_key`: each message of
  [Failure modes](#failure-modes).
- `test_a_profile_takes_the_pads_and_uarts_of_its_chip`: the S3's, the C3's and the C6's pads,
  UARTs and console pins; `test_gpio_34_drives_on_the_s3_where_the_classic_esp32_has_it_input_only`;
  `test_each_chip_s_devkit_routes_the_gnss_and_the_echo_lines`: the three devkit profiles.
- `test_every_backend_has_a_declaration`: every key of `SERVICES` is declared.
  `test_the_declaration_is_the_specification_s_table`: the declaration as a whole.
- `test_a_backend_refuses_what_it_does_not_declare`, `test_the_first_request_a_backend_cannot_give_is_the_reason`,
  `test_a_request_no_backend_can_give_is_an_error_not_a_skip`, `test_a_declaration_that_breaks_the_schema_is_refused`.

`tests/test_pytest_plugin.py` runs the marker through pytest, with `serial` added to `SERVICES`
in the generated suite's `conftest.py` (`WITH_SERIAL`):
`test_a_marker_no_backend_can_satisfy_is_a_collection_error`,
`test_markers_on_a_class_or_a_module_count` (a class's marker closer than its module's),
`test_a_board_marker_in_a_pytest_param_is_refused` (`board_profile` and `app_path`),
`test_a_board_marker_a_hook_adds_is_refused`, and, on the fake runner,
`test_the_board_runs_the_profile_the_test_chose_and_starts_no_runner_unpowered` and
`test_a_source_or_a_tap_the_backend_cannot_give_skips_at_the_call`.
`test_every_route_and_line_of_the_profile_is_on_the_link` checks LilyGO with an empty slot and
with an inserted image alongside GNSS and modem models: runner SD arguments, PSRAM and the
handshake's watched GPIOs. It needs no firmware build or real emulator.
`tests/test_sdspi.py::test_the_lilygo_slot_writes_an_image_alongside_both_uart_models` runs the
unmodified ESP-IDF SD example on LilyGO and checks the image's written files with both UART
models selected. It needs the sdspi build, runner, ROM ELF and mtools.
`tests/test_modem_model.py::test_named_controls_are_observable_without_changing_modem_state`
checks that the additional GPIOs can change while modem power, registration and AT settings
remain intact.

Local selection in `tests/test_pytest_plugin.py` covers ambiguous arguments, missing and invalid
files, inherited markers and absolute paths, invocation outside the test directory, separate
snapshots for same-name files and a bundled namesake, and late path switches from params/hooks.
`test_local_profile_snapshot_survives_retargeting_a_symlink` retargets a symlink and removes the
original file at setup, then checks that both parametrised cases use the collection snapshot.
`test_invalid_local_profiles_fail_at_collection` includes invalid UTF-8 and requires the test id
and resolved file path without a traceback.
`test_a_tap_records_what_crossed_the_route_both_ways` also runs with a local profile, checking the
handshake and recording. `tests/test_serial_backend.py::test_console_reset_clock_tap_and_autoflash`
checks local selection and recording identity through serial's real fixture chain with a fake port.

Not covered: `app_path` added late by a hook (in a `pytest.param` it is tested), and markers a
fixture adds at setup, which nothing checks.

## See also

- [Board profiles](board-profiles-user.md)
- [pytest plugin: how it works](pytest-plugin-technical.md)
- [Plan section 4.5, board profiles](../implementation-plan.md#45-board-profiles), and finding 12
  in [plan section 1](../implementation-plan.md#1-findings-that-amend-the-specification)
- [Harness decisions](../decisions/harness.md)
