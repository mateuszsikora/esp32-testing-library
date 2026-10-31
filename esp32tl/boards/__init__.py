"""Board profiles: pin tables as TOML data, no product knowledge (plan §4.5).

A profile is `<name>.toml` in this directory. It says which chip the board carries and which UART
and pins each of its interfaces uses: `[uart.<role>]` routes and `[lines.<role>]` sets of GPIO
lines. Nothing in it says what the firmware does with them. `capabilities.toml`, beside the
profiles, is no profile: it says what each backend can put at the far end of an interface
(`esp32tl.boards.capabilities`).
"""

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

PROFILES = Path(__file__).parent

# The profile a test gets without the board_profile marker: it carries every validation firmware.
DEFAULT_PROFILE = "esp32-devkitc"

# The chips a profile may name: the classic ESP32, the only one the runner emulates.
CHIPS = ("esp32",)

# The classic ESP32's GPIO pads: 0 to 39 without 24 and 28 to 31. 34 to 39 are inputs only, so a
# UART's TX cannot be one of them.
GPIOS = frozenset(range(40)) - {24, 28, 29, 30, 31}
INPUT_ONLY = frozenset(range(34, 40))
UART_PORTS = range(3)
PULLS = ("up", "down")

# What the `console` interface takes, on every board: UART0 on its IO MUX pins. A route or a line
# that takes one of them is refused as a second use.
CONSOLE_PORT = 0
CONSOLE_PINS = {1: "console.tx", 3: "console.rx"}

# Role names: `[uart.gnss]`, `[lines.modem]`, the `pwrkey` of a line set.
ROLE = re.compile(r"[a-z][a-z0-9_]*")


class ProfileError(Exception):
    """A profile that cannot be loaded, said with the file and the key at fault."""


@dataclass(frozen=True)
class UartRoute:
    """`[uart.<role>]`: the UART `port` the firmware drives it from, on `tx` and `rx`."""

    port: int
    tx: int
    rx: int
    baud: int


@dataclass(frozen=True)
class Line:
    """One GPIO line of a `[lines.<role>]` set, and the board's pull on it, if it has one."""

    pin: int
    pull: Literal["up", "down"] | None = None


@dataclass(frozen=True)
class BoardProfile:
    """A loaded profile. `flash_mb` and `psram_mb` are None when the profile does not know them.

    Frozen but not hashable, because its mappings are not: it cannot be a set member or a key.
    """

    name: str
    chip: str
    flash_mb: int | None
    psram_mb: int | None
    uart: Mapping[str, UartRoute]
    lines: Mapping[str, Mapping[str, Line]]

    @property
    def interfaces(self) -> list[str]:
        """`console`, which every board has, then `uart.<role>` and `lines.<role>`, as the
        profile lists them."""
        return [
            "console",
            *(f"uart.{role}" for role in self.uart),
            *(f"lines.{role}" for role in self.lines),
        ]


def names() -> list[str]:
    """The profiles esp32tl ships, by name."""
    return sorted(p.stem for p in PROFILES.glob("*.toml") if p.name != "capabilities.toml")


@cache
def profile(name: str) -> BoardProfile:
    """The shipped profile called `name`."""
    if name not in names():
        raise ProfileError(
            f"no board profile named {name!r}; the profiles are {', '.join(names())}"
        )
    return load(PROFILES / f"{name}.toml")


def load(path: Path) -> BoardProfile:
    """The profile in `path`, whose `name` must be the file's name without `.toml`."""
    try:
        with path.open("rb") as file:
            data = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ProfileError(f"{path.name}: {error}") from None
    return Reader(path.name).profile(data)


class Reader:
    """Checks a profile's data key by key; every error names the file and the key."""

    def __init__(self, file: str) -> None:
        self.file = file

    def fail(self, key: str, what: str) -> ProfileError:
        """An error at `key`; at the top level, where `key` is empty, at the file."""
        return ProfileError(f"{self.file}: {key}: {what}" if key else f"{self.file}: {what}")

    def keys(self, key: str, table: dict[str, Any], required: set[str], optional: set[str]) -> None:
        missing = sorted(required - table.keys())
        if missing:
            raise self.fail(key, f"missing {', '.join(missing)}")
        unknown = sorted(table.keys() - required - optional)
        if unknown:
            raise self.fail(key, f"unknown key {', '.join(unknown)}")

    def table(self, key: str, value: object) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise self.fail(key, "is not a table")
        return value

    def integer(self, key: str, value: object, low: int, high: int | None = None) -> int:
        # bool is an int to Python, not to TOML: `port = true` is no port.
        if not isinstance(value, int) or isinstance(value, bool):
            raise self.fail(key, f"{value!r} is not an integer")
        if value < low or (high is not None and value > high):
            bound = f"at least {low}" if high is None else f"from {low} to {high}"
            raise self.fail(key, f"{value} is out of range, {bound}")
        return value

    def gpio(self, key: str, value: object, *, output: bool = False) -> int:
        pin = self.integer(key, value, 0, max(GPIOS))
        if pin not in GPIOS:
            raise self.fail(key, f"the ESP32 has no GPIO {pin}")
        if output and pin in INPUT_ONLY:
            raise self.fail(key, f"GPIO {pin} is input only")
        return pin

    def roles(self, key: str, value: object) -> dict[str, Any]:
        table = self.table(key, value)
        for role in table:
            if not ROLE.fullmatch(role):
                raise self.fail(f"{key}.{role}", "a role is lowercase letters, digits and _")
        return table

    def profile(self, data: dict[str, Any]) -> BoardProfile:
        self.keys("", data, {"name", "chip"}, {"flash_mb", "psram_mb", "uart", "lines"})
        name = data["name"]
        stem = self.file.removesuffix(".toml")
        if name != stem:
            raise self.fail("name", f"{name!r} is not the file's name, {stem!r}")
        if data["chip"] not in CHIPS:
            raise self.fail("chip", f"{data['chip']!r} is not one of {', '.join(CHIPS)}")
        sizes = {
            key: self.integer(key, data[key], 0) if key in data else None
            for key in ("flash_mb", "psram_mb")
        }
        uart = {
            role: self.uart(f"uart.{role}", value)
            for role, value in self.roles("uart", data.get("uart", {})).items()
        }
        lines = {
            role: self.lines(f"lines.{role}", value)
            for role, value in self.roles("lines", data.get("lines", {})).items()
        }
        self.distinct(uart, lines)
        return BoardProfile(
            name=name,
            chip=data["chip"],
            flash_mb=sizes["flash_mb"],
            psram_mb=sizes["psram_mb"],
            uart=MappingProxyType(uart),
            lines=MappingProxyType({role: MappingProxyType(ls) for role, ls in lines.items()}),
        )

    def uart(self, key: str, value: object) -> UartRoute:
        table = self.table(key, value)
        self.keys(key, table, {"port", "tx", "rx", "baud"}, set())
        return UartRoute(
            port=self.integer(f"{key}.port", table["port"], UART_PORTS[0], UART_PORTS[-1]),
            tx=self.gpio(f"{key}.tx", table["tx"], output=True),
            rx=self.gpio(f"{key}.rx", table["rx"]),
            baud=self.integer(f"{key}.baud", table["baud"], 1),
        )

    def lines(self, key: str, value: object) -> dict[str, Line]:
        lines = {}
        for role, line in self.roles(key, value).items():
            where = f"{key}.{role}"
            if not isinstance(line, dict):
                lines[role] = Line(self.gpio(where, line))
                continue
            self.keys(where, line, {"pin"}, {"pull"})
            pull = line.get("pull")
            if pull is not None and pull not in PULLS:
                raise self.fail(f"{where}.pull", f"{pull!r} is not one of {', '.join(PULLS)}")
            lines[role] = Line(self.gpio(f"{where}.pin", line["pin"]), pull)
        return lines

    def distinct(self, uart: dict[str, UartRoute], lines: dict[str, dict[str, Line]]) -> None:
        """One UART per route, and one use per pin; the console's UART and pins are taken."""
        ports = {CONSOLE_PORT: "console"}
        for role, route in uart.items():
            if route.port in ports:
                raise self.fail(f"uart.{role}.port", f"UART{route.port} is {ports[route.port]}'s")
            ports[route.port] = f"uart.{role}"
        pins = dict(CONSOLE_PINS)
        uses = [
            (f"uart.{r}.{end}", getattr(u, end)) for r, u in uart.items() for end in ("tx", "rx")
        ]
        uses += [(f"lines.{r}.{n}", line.pin) for r, ls in lines.items() for n, line in ls.items()]
        for key, pin in uses:
            if pin in pins:
                raise self.fail(key, f"GPIO {pin} is {pins[pin]} already")
            pins[pin] = key
