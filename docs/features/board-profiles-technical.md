# Board profiles: how they work

The loader is `esp32tl/boards/__init__.py`, the profiles are the `*.toml` files beside it, and the
capability declaration is `esp32tl/boards/capabilities.toml`, read by
`esp32tl/boards/capabilities.py`. The [pytest plugin](pytest-plugin-technical.md) uses both. What
the user sees is in [board-profiles-user.md](board-profiles-user.md).

## Data flow

1. **`pytest_generate_tests`**, for a test that uses `board`, after the `app_path` marker:
   `board_profile(metafunc.definition)` loads the profile the closest `board_profile` marker names,
   or `DEFAULT_PROFILE` (`esp32-devkitc`), through `boards.profile(name)`.
   `board_requests(metafunc.definition)` turns the `tap` markers of the test, its class and its
   module into `Request`s, one per tapped interface over every level, duplicates dropped.
   `check(requests, profile, declared())` raises `CapabilityError` for an interface the profile
   does not have and for a tap that no declared backend gives. A `MarkerError`, `ProfileError` or `CapabilityError` becomes
   `pytest.fail.Exception(f"{nodeid}: {error}", pytrace=False)`, raised `from None`: a collection
   error without the chained exception.
2. Per backend, `refusal(backend, requests, declared())` is the reason the backend cannot give the
   first request it cannot give, or `None`. The backend's parameter is
   `pytest.param(backend, SERVICES[backend], app_path, marks=marks, id=backend)`, where `marks` is
   `[pytest.mark.skip(reason=f"{backend}: {reason}")]` when there is a reason. pytest skips the item
   in its setup phase before it sets up any fixture, so pytest-embedded's fixtures, a serial port
   among them for a backend that runs `esp,idf`, are never set up for it.
3. **`board` fixture**, at setup: `board_profile(request.node)` again (the marker was checked at
   collection, and the profile is cached), passed to `Esp32Sim(..., profile)`, which keeps it as
   `Esp32Sim.profile` and gives its `name` and `chip` to the `BoardLink`, for the harness's `hello`
   ([esp32sim-backend-technical.md](esp32sim-backend-technical.md#data-flow)). Every `uart.*` route
   becomes a `Uart` on the link, its `Wire` the route's `tx`, `rx` and `baud`; the profile's lines
   are not on the link.
4. **A source, in the test body.** `gnss.replay(path)` asks the backend, through the `Uart`'s
   `asked`, for a `replay` on `uart.gnss`, and the first call of the receiver model, as
   `gnss.fix(at=...)`, for a `model`. `Esp32Sim._asked` builds `[Request(interface, source=source)]`,
   runs `capabilities.check` on it (a `CapabilityError` becomes a `SourceError` with its message),
   then `capabilities.refusal(Esp32Sim.name, ...)`, whose reason skips the test at the call
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

No other key is allowed, at any level. A role, and a line's name, matches `[a-z][a-z0-9_]*`.

`[uart.<role>]` takes exactly these keys:

| Key | Rule |
|---|---|
| `port` | 1 or 2: in `UART_PORTS` (0 to 2), and not `CONSOLE_PORT`, 0 |
| `tx` | A GPIO of the classic ESP32 that can drive an output: 0 to 33, without 24 and 28 to 31 |
| `rx` | A GPIO of the classic ESP32: 0 to 39, without 24 and 28 to 31 (`GPIOS`) |
| `baud` | At least 1 |

`[lines.<role>]` maps a line's name to its GPIO, either as an integer (`pwrkey = 4`) or as a table
`{ pin = 33, pull = "down" }`, where `pull` is `up` or `down` and may be left out. A bare integer
says nothing about the board's pull.

Across the profile, no two routes share a `port`, and no GPIO serves two uses (a route's `tx` and
`rx`, a line). The `console` interface is not in the file, because every board has it, but it
takes its share: UART0 and its IO MUX pins, GPIO 1 (`console.tx`) and GPIO 3 (`console.rx`), in
`CONSOLE_PORT` and `CONSOLE_PINS`. GPIO 6 to 11, which most modules give to the SPI flash, are not
refused
([why](../decisions/harness.md#a-profile-reserves-the-consoles-uart-and-pins-not-the-flash-pins)).
Integers are TOML integers: `true` is not a port.

### The capability declaration: `esp32tl/boards/capabilities.toml`

One table per backend, keyed by the backend's name in `SERVICES`; in it, one inline table per
interface kind, where the kind of `uart.gnss` is `uart` and that of `console` is `console`:

| Key | Type | Default |
|---|---|---|
| `sources` | list of `SOURCES`: `model`, `replay`, `real` | `[]` |
| `tap` | boolean | `false` |

A kind is one of `KINDS`: `console`, `uart`, `lines`. A kind a backend does not list is
`Capability()`: no source, no tap. The file declares `esp32sim` and `serial`:

```toml
[esp32sim]
console = { tap = true }
uart = { sources = ["model", "replay", "real"], tap = true }

[serial]
console = { tap = true }
uart = { sources = ["real"], tap = false }
```

`serial` is not in `SERVICES`, so no test is parametrised with it; `check` counts it among the
backends that could give a request.

## Interfaces

| Name | Kind | What |
|---|---|---|
| `boards.profile(name)` | function, cached | The shipped profile; `ProfileError` for another name |
| `boards.load(path)` | function | The profile in any file, checked against [Schema](#schema) |
| `boards.names()` | function | The shipped profiles' names, sorted |
| `boards.DEFAULT_PROFILE` | `str` | `"esp32-devkitc"` |
| `BoardProfile` | frozen dataclass | `name`, `chip`, `flash_mb: int \| None`, `psram_mb: int \| None`, `uart: Mapping[str, UartRoute]`, `lines: Mapping[str, Mapping[str, Line]]`; property `interfaces`: `["console", "uart.<role>", ..., "lines.<role>", ...]`, in file order. Not hashable: its mappings are not |
| `UartRoute` | frozen dataclass | `port`, `tx`, `rx`, `baud` |
| `Line` | frozen dataclass | `pin`, `pull: "up" \| "down" \| None` |
| `capabilities.declared()` | function, cached | The shipped declaration: backend name, then kind, then `Capability` |
| `capabilities.load(path)` | function | A declaration in any file |
| `Capability` | frozen dataclass | `sources: tuple[str, ...] = ()`, `tap: bool = False` |
| `Request` | frozen dataclass | `interface`, `source: str \| None = None`, `tap: bool = False` |
| `capabilities.check(requests, profile, capabilities)` | function | Raises `CapabilityError` for a request the profile or every backend refuses |
| `capabilities.refusal(backend, requests, capabilities)` | function | The reason, without the backend's name, or `None`; `KeyError` for an undeclared backend |
| `board_profile(node)`, `board_requests(node)` | functions, `esp32tl/pytest_plugin.py` | The marker readers; `MarkerError` for a marker used otherwise than registered |
| `BoardMarkers` | frozen dataclass, `esp32tl/pytest_plugin.py` | The `app_path` marker's arguments, the profile's name and the taps, as `pytest_generate_tests` read them for a definition; kept in `config.stash[READ]` |
| `late_board_marker(config, item)` | function, `esp32tl/pytest_plugin.py` | Which board marker an item carries otherwise than its definition did, or `None` |

The markers, `board_profile(name)` and `tap(*interfaces)`, are registered in `pytest_configure` with the plugin's other marker
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
| `x.toml: uart: is not a table` | `uart` or `lines`, or a role in them, is not a table |
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
uart, lines`, `....sources: is not a list of names`, `....sources: 'modle' is not one of model,
replay, real` and `....tap: is not true or false`. The request errors of `check` and the skip reasons of `refusal`
are listed in [board-profiles-user.md](board-profiles-user.md#when-no-backend-can-give-it) and
[above it](board-profiles-user.md#when-a-backend-cannot-give-it).

## Tests

`tests/test_boards.py`, plain pytest:

- `test_the_shipped_profiles_are_the_two_of_the_plan`, `test_esp32_devkitc_routes_the_gnss_on_uart1_gpio_4_and_5`:
  the names, the default, and `esp32-devkitc` field by field.
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

`tests/test_pytest_plugin.py` runs the markers through pytest, with `serial` added to `SERVICES`
in the generated suite's `conftest.py` (`WITH_SERIAL`):
`test_a_backend_that_cannot_give_a_tap_skips_with_the_reason` (the skip reason, and that
pytest-embedded's fixtures are not set up for the skipped case, from `--setup-plan`),
`test_a_marker_no_backend_can_satisfy_is_a_collection_error`,
`test_markers_on_a_class_or_a_module_count`, `test_a_board_marker_in_a_pytest_param_is_refused`,
`test_a_board_marker_a_hook_adds_is_refused`, and, on the fake runner,
`test_the_board_runs_the_profile_the_test_chose_and_starts_no_runner_unpowered` and
`test_a_source_the_backend_cannot_give_skips_at_the_call`.

Not covered: which reason the plugin shows when one backend refuses several taps (`refusal`
itself is tested), `app_path` arriving late (`board_profile` and `tap` are tested), and markers a
fixture adds at setup, which nothing checks.

## See also

- [Board profiles](board-profiles-user.md)
- [pytest plugin: how it works](pytest-plugin-technical.md)
- [Plan section 4.5, board profiles](../implementation-plan.md#45-board-profiles), and finding 12
  in [plan section 1](../implementation-plan.md#1-findings-that-amend-the-specification)
- [Harness decisions](../decisions/harness.md)
