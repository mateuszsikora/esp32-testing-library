# Board profiles: how they work

The loader is `esp32tl/boards/__init__.py`, the profiles are the `*.toml` files beside it, and the
capability declaration is `esp32tl/boards/capabilities.toml`, read by
`esp32tl/boards/capabilities.py`. The [pytest plugin](pytest-plugin-technical.md) uses both. What
the user sees is in [board-profiles-user.md](board-profiles-user.md).

## Data flow

1. **`pytest_generate_tests`**, for a test that uses `board`, after the `app_path` marker:
   `board_profile(metafunc.definition)` loads the profile the closest `board_profile` marker names,
   or `DEFAULT_PROFILE` (`esp32-devkitc`), through `boards.profile(name)`. A `MarkerError` or
   `ProfileError` becomes `pytest.fail.Exception(f"{nodeid}: {error}", pytrace=False)`, raised
   `from None`: a collection error without the chained exception.
2. **`board` fixture**, at setup: `board_profile(request.node)` again (the marker was checked at
   collection, and the profile is cached), passed to `Esp32Sim(..., profile)`, which keeps it as
   `Esp32Sim.profile` and gives its `name` and `chip` to the `BoardLink`, for the harness's `hello`
   ([esp32sim-backend-technical.md](esp32sim-backend-technical.md#data-flow)). Every `uart.*` route
   becomes a `Uart` on the link, its `Wire` the route's `tx`, `rx` and `baud`; every line of every
   `[lines.<role>]` set is on one `Lines`, with its `pull`, which the link drives on the line from
   power-on ([pytest-plugin-technical.md](pytest-plugin-technical.md#data-flow)). An `[sd_card]`
   slot becomes an empty `SdCard`, the `sd_card` fixture.
3. **A source or a tap, in the test body.** `gnss.replay(path)` asks the backend, through the
   `Uart`'s `asked`, for `Request("uart.gnss", source="replay")`, `gnss.tap(path)` for
   `Request("uart.gnss", tap=True)`, and `sd_card.insert(image)`, through the `SdCard`'s, for
   `Request("sd_card", source="model")`. `Esp32Sim._asked` runs `capabilities.check([request], ...)`
   (a `CapabilityError` becomes a `SourceError` with its message), then
   `capabilities.refusal(Esp32Sim.name, [request], ...)`, whose reason skips the test at the call
   ([esp32sim-backend-technical.md](esp32sim-backend-technical.md#data-flow)). It reads the
   declaration through `capabilities.declared()` each time.

`boards.profile(name)` checks `name` against `names()`, the stems of the `*.toml` files in
`PROFILES` (`esp32tl/boards/`) except `capabilities.toml`, then `load(PROFILES / f"{name}.toml")`.
It is `functools.cache`d, so every test gets the same object; the mappings in it are
`MappingProxyType`s and the dataclasses are frozen, so no test can change what the next one gets.
`declared()` is cached the same way.

`load(path)` reads the file with `tomllib` and checks it key by key with `Reader`, in the order of
[Schema](#schema). The first error raises `ProfileError` and loading stops.

## Schema

### A profile: `esp32tl/boards/<name>.toml`

| Key | Type | Required | Rule |
|---|---|---|---|
| `name` | string | yes | The file's name without `.toml` |
| `chip` | string | yes | One of `CHIPS`: `esp32`, the classic chip, the only one the runner emulates |
| `flash_mb`, `psram_mb` | integer | no | At least 0; absent is "not known", `None` in `BoardProfile` |
| `[uart.<role>]` | table | no | One per UART route; keys below |
| `[lines.<role>]` | table | no | One per set of GPIO lines; keys below |
| `[sd_card]` | table | no | The board's SD card slot, wired for SPI mode; keys below |

No other key is allowed, at any level. A role, and a line's name, matches `[a-z][a-z0-9_]*`.

`[uart.<role>]` takes exactly these keys:

| Key | Rule |
|---|---|
| `port` | 1 or 2: in `UART_PORTS` (0 to 2), and not `CONSOLE_PORT`, 0 |
| `tx` | A GPIO of the classic ESP32 that can drive an output: 0 to 33, without 24 and 28 to 31 |
| `rx` | A GPIO of the classic ESP32: 0 to 39, without 24 and 28 to 31 (`GPIOS`) |
| `baud` | At least 1 |

`[sd_card]` takes exactly `sclk`, `mosi`, `miso` and `cs`, the GPIOs of the slot as the chip sees
them: `sclk`, `mosi` and `cs` can drive an output (0 to 33, without 24 and 28 to 31), `miso` is
any GPIO of `GPIOS`. A board has one slot at most, and the interface is `sd_card`, with no role.

`[lines.<role>]` maps a line's name to its GPIO, either as an integer (`pwrkey = 4`) or as a table
`{ pin = 33, pull = "down" }`, where `pull` is `up` or `down` and may be left out. A bare integer
says nothing about the board's pull. On `esp32sim` a pull is a level the harness drives on the line
while the test does not, which the firmware's own output wins over.

Across the profile, no two routes share a `port`, and no GPIO serves two uses (a route's `tx` and
`rx`, a line, a pin of the slot). The `console` interface is not in the file, because every board has it, but it
takes its share: UART0 and its IO MUX pins, GPIO 1 (`console.tx`) and GPIO 3 (`console.rx`), in
`CONSOLE_PORT` and `CONSOLE_PINS`. GPIO 6 to 11, which most modules give to the SPI flash, are not
refused
([why](../decisions/harness.md#a-profile-reserves-the-consoles-uart-and-pins-not-the-flash-pins)).
Integers are TOML integers: `true` is not a port.

### The capability declaration: `esp32tl/boards/capabilities.toml`

One table per backend, keyed by the backend's name in `SERVICES`; in it, one inline table per
interface kind, where the kind of `uart.gnss` is `uart` and that of `console` or `sd_card` is the
interface itself:

| Key | Type | Default |
|---|---|---|
| `sources` | list of `SOURCES`: `model`, `replay`, `real` | `[]` |
| `tap` | boolean | `false` |

A kind is one of `KINDS`: `console`, `uart`, `lines`, `sd_card`. A kind a backend does not list is
`Capability()`: no source, no tap. The file declares `esp32sim` and `serial`:

```toml
[esp32sim]
console = { tap = true }
uart = { sources = ["model", "replay", "real"], tap = true }
sd_card = { sources = ["model"] }

[serial]
console = { tap = true }
uart = { sources = ["real"], tap = false }
sd_card = { sources = ["real"] }
```

The `model` on `sd_card` is the runner's card model holding an image file
([runner-technical.md](runner-technical.md#the-sd-card)); on `serial` it is the card in the slot.

`serial` is not in `SERVICES`, so no test is parametrised with it; `check` counts it among the
backends that could give a request.

## Interfaces

| Name | Kind | What |
|---|---|---|
| `boards.profile(name)` | function, cached | The shipped profile; `ProfileError` for another name |
| `boards.load(path)` | function | The profile in any file, checked against [Schema](#schema) |
| `boards.names()` | function | The shipped profiles' names, sorted |
| `boards.DEFAULT_PROFILE` | `str` | `"esp32-devkitc"` |
| `BoardProfile` | frozen dataclass | `name`, `chip`, `flash_mb: int \| None`, `psram_mb: int \| None`, `uart: Mapping[str, UartRoute]`, `lines: Mapping[str, Mapping[str, Line]]`, `sd_card: SdCardRoute \| None = None`; property `interfaces`: `["console", "uart.<role>", ..., "lines.<role>", ..., "sd_card"]`, in file order, `sd_card` only with a slot. Not hashable: its mappings are not |
| `UartRoute` | frozen dataclass | `port`, `tx`, `rx`, `baud` |
| `SdCardRoute` | frozen dataclass | `sclk`, `mosi`, `miso`, `cs` |
| `Line` | frozen dataclass | `pin`, `pull: "up" \| "down" \| None` |
| `capabilities.declared()` | function, cached | The shipped declaration: backend name, then kind, then `Capability` |
| `capabilities.load(path)` | function | A declaration in any file |
| `Capability` | frozen dataclass | `sources: tuple[str, ...] = ()`, `tap: bool = False` |
| `Request` | frozen dataclass | `interface`, `source: str \| None = None`, `tap: bool = False` |
| `capabilities.check(requests, profile, capabilities)` | function | Raises `CapabilityError` for a request the profile or every backend refuses |
| `capabilities.refusal(backend, requests, capabilities)` | function | The reason, without the backend's name, or `None`; `KeyError` for an undeclared backend |
| `board_profile(node)` | function, `esp32tl/pytest_plugin.py` | The marker's reader; `MarkerError` for a marker used otherwise than registered |
| `BoardMarkers` | frozen dataclass, `esp32tl/pytest_plugin.py` | The `app_path` marker's arguments and the profile's name, as `pytest_generate_tests` read them for a definition; kept in `config.stash[READ]` |
| `late_board_marker(config, item)` | function, `esp32tl/pytest_plugin.py` | Which board marker an item carries otherwise than its definition did, or `None` |

The marker, `board_profile(name)`, is registered in `pytest_configure` with the plugin's other marker
([pytest-plugin-technical.md](pytest-plugin-technical.md#interfaces)).

## Failure modes

Profile errors, `ProfileError`, are `<file>: <key>: <what>`, and `<file>: <what>` for the
top level; at collection they come after
`<test id>: `, but only a shipped profile is loaded there, so a test sees one only when a shipped
file is broken. The ones `tests/test_boards.py` checks, verbatim:

| Message | Cause |
|---|---|
| `no board profile named 'NAME'; the profiles are esp32-devkitc, lilygo-t-a7670g-r2` | `boards.profile` with another name |
| `x.toml: <tomllib's message>`, as `x.toml: Invalid value (at end of document)` | Not TOML, or not readable |
| `x.toml: missing chip`, `x.toml: unknown key pins` | A top-level key missing or unknown |
| `x.toml: uart.gnss: missing baud`, `x.toml: lines.modem.ri: missing pin` | The same in a table |
| `x.toml: name: 'y' is not the file's name, 'x'` | `name` differs from the file's name |
| `x.toml: chip: 'esp32s3' is not one of esp32` | Another chip |
| `x.toml: uart: is not a table`, `x.toml: sd_card: is not a table` | `uart`, `lines` or `sd_card`, or a role in them, is not a table |
| `x.toml: sd_card: missing cs`, `x.toml: sd_card: unknown key cd` | A pin of the slot missing, or another key |
| `x.toml: sd_card.cs: GPIO 36 is input only` | `sclk`, `mosi` or `cs` on 34 to 39 |
| `x.toml: sd_card.mosi: GPIO 4 is uart.gnss.tx already`, `x.toml: sd_card.cs: GPIO 13 is sd_card.miso already` | A pin of the slot with another use |
| `x.toml: psram_mb: '4' is not an integer`, `x.toml: uart.gnss.port: True is not an integer` | Not a TOML integer |
| `x.toml: flash_mb: -4 is out of range, at least 0`, `x.toml: uart.gnss.port: 3 is out of range, from 0 to 2` | Out of range; a GPIO above 39 too |
| `x.toml: uart.gnss.rx: the ESP32 has no GPIO 24` | 24 or 28 to 31 |
| `x.toml: uart.gnss.tx: GPIO 34 is input only` | A `tx` on 34 to 39 |
| `x.toml: uart.GNSS: a role is lowercase letters, digits and _` | A role or a line's name outside `[a-z][a-z0-9_]*` |
| `x.toml: lines.modem.ri.pull: 'high' is not one of up, down` | Another pull |
| `x.toml: uart.modem.port: UART1 is uart.gnss's`, `x.toml: uart.gnss.port: UART0 is console's` | Two routes on one UART, or a route on the console's |
| `x.toml: lines.modem.pwrkey: GPIO 5 is uart.gnss.rx already`, `x.toml: uart.gnss.tx: GPIO 1 is console.tx already` | One GPIO with two uses, the console's pins counted |

Declaration errors, `CapabilityError` from `capabilities.load`, are `<file>: <backend>[.<kind>]:
is not a table`, `...: unknown key <key>`, `...: no such kind of interface; the kinds are console,
uart, lines, sd_card`, `....sources: is not a list of names`, `....sources: 'modle' is not one of model,
replay, real` and `....tap: is not true or false`. The request errors of `check` and the skip reasons of `refusal`
are listed in [board-profiles-user.md](board-profiles-user.md#when-no-backend-can-give-it) and
[above it](board-profiles-user.md#when-a-backend-cannot-give-it).

## Tests

`tests/test_boards.py`, plain pytest:

- `test_the_shipped_profiles_are_the_two_of_the_plan`, `test_esp32_devkitc_routes_the_gnss_on_uart1_gpio_4_and_5`:
  the names, the default, and `esp32-devkitc` field by field, its slot included.
  `test_an_sd_card_slot_may_take_an_input_only_pin_for_miso`: a slot's MISO on GPIO 39.
- `test_the_lilygo_profile_holds_only_what_the_specification_gives`: the file's TOML data compared
  whole, so a pin or a size added from memory fails it.
- `test_a_profile_is_loaded_once_and_cannot_be_changed`, `test_an_unknown_profile_name_lists_the_profiles`,
  `test_a_profile_without_sizes_or_interfaces_has_only_the_console`.
- `test_a_profile_that_breaks_the_schema_is_refused_with_the_key`: each message of
  [Failure modes](#failure-modes).
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

Not covered: `app_path` added late by a hook (in a `pytest.param` it is tested), and markers a
fixture adds at setup, which nothing checks.

## See also

- [Board profiles](board-profiles-user.md)
- [pytest plugin: how it works](pytest-plugin-technical.md)
- [Plan section 4.5, board profiles](../implementation-plan.md#45-board-profiles), and finding 12
  in [plan section 1](../implementation-plan.md#1-findings-that-amend-the-specification)
- [Harness decisions](../decisions/harness.md)
