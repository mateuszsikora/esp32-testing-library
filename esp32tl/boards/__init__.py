"""Board profiles: pin tables as TOML data, no product knowledge (plan §4.5).

A profile is `<name>.toml` in this directory. It says which chip the board carries, where its
console is (`console`: UART0, or the chip's USB-Serial/JTAG controller) and which UART and pins
each of its interfaces uses: `[uart.<role>]` routes, `[lines.<role>]` sets of GPIO lines and an
`[sd_card]` slot on SPI. Nothing in it says what the firmware does with them.
`capabilities.toml`, beside the profiles, is no profile: it says what each backend can put at
the far end of an interface (`esp32tl.boards.capabilities`).
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


@dataclass(frozen=True)
class Chip:
    """What a profile's `chip` brings: its GPIO pads, those of them that are inputs only, its
    UARTs, the IO MUX pins of UART0, which a `console` on UART0 takes, and whether it has a
    USB-Serial/JTAG controller, which a profile's `console` may be instead, with the pads of its
    USB D- and D+, which such a `console` takes."""

    name: str
    title: str
    gpios: frozenset[int]
    input_only: frozenset[int]
    uarts: int
    console_pins: Mapping[int, str]
    usb_serial_jtag: bool
    usb_pins: Mapping[int, str]

    def console_pin(self, end: Literal["tx", "rx"]) -> int:
        """UART0's `tx` or `rx` pin, the console's."""
        return next(pin for pin, use in self.console_pins.items() if use == f"console.{end}")


# The chips a profile may name, those the runner emulates, by ESP-IDF target name.
CHIPS = {
    # The classic ESP32's GPIO pads: 0 to 39 without 24 and 28 to 31. 34 to 39 are inputs only,
    # so a UART's TX cannot be one of them. UART0 is on GPIO 1 (TX) and 3 (RX).
    "esp32": Chip(
        name="esp32",
        title="ESP32",
        gpios=frozenset(range(40)) - {24, 28, 29, 30, 31},
        input_only=frozenset(range(34, 40)),
        uarts=3,
        console_pins=MappingProxyType({1: "console.tx", 3: "console.rx"}),
        usb_serial_jtag=False,
        usb_pins=MappingProxyType({}),
    ),
    # The ESP32-S3's: 0 to 21 and 26 to 48, every one an output too (ESP32-S3 datasheet, section
    # 2.3). UART0 is on GPIO 43 (TX) and 44 (RX).
    "esp32s3": Chip(
        name="esp32s3",
        title="ESP32-S3",
        gpios=frozenset(range(49)) - {22, 23, 24, 25},
        input_only=frozenset(),
        uarts=3,
        console_pins=MappingProxyType({43: "console.tx", 44: "console.rx"}),
        usb_serial_jtag=True,
        usb_pins=MappingProxyType({19: "console.usb_dm", 20: "console.usb_dp"}),
    ),
    # The ESP32-C3's: 0 to 21 (ESP32-C3 datasheet, section 2.2). UART0 is on GPIO 21 (TX) and
    # 20 (RX); it has two UARTs.
    "esp32c3": Chip(
        name="esp32c3",
        title="ESP32-C3",
        gpios=frozenset(range(22)),
        input_only=frozenset(),
        uarts=2,
        console_pins=MappingProxyType({21: "console.tx", 20: "console.rx"}),
        usb_serial_jtag=True,
        usb_pins=MappingProxyType({18: "console.usb_dm", 19: "console.usb_dp"}),
    ),
    # The ESP32-C6's: 0 to 30 (ESP32-C6 datasheet, section 2.3). UART0 is on GPIO 16 (TX) and
    # 17 (RX); it has two UARTs besides the LP UART.
    "esp32c6": Chip(
        name="esp32c6",
        title="ESP32-C6",
        gpios=frozenset(range(31)),
        input_only=frozenset(),
        uarts=2,
        console_pins=MappingProxyType({16: "console.tx", 17: "console.rx"}),
        usb_serial_jtag=True,
        usb_pins=MappingProxyType({12: "console.usb_dm", 13: "console.usb_dp"}),
    ),
}
PULLS = ("up", "down")

# What the `console` interface takes on a board whose console is UART0: UART0 on its IO MUX pins.
# A route or a line that takes one of them is refused as a second use.
CONSOLE_PORT = 0

# Where a profile's `console` is: UART0, the default, or the chip's USB-Serial/JTAG controller,
# which takes the chip's USB D- and D+ pads and leaves UART0 and its pins to the profile's routes.
ConsoleKind = Literal["uart0", "usb-serial-jtag"]
UART0: Literal["uart0"] = "uart0"
USB_SERIAL_JTAG: Literal["usb-serial-jtag"] = "usb-serial-jtag"
CONSOLES: tuple[ConsoleKind, ...] = (UART0, USB_SERIAL_JTAG)

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
class LineRoute:
    """One GPIO line of a `[lines.<role>]` set, and the board's pull on it, if it has one."""

    pin: int
    pull: Literal["up", "down"] | None = None
    # Optional pulse polarity (0 or 1) and on/off thresholds in milliseconds at the ESP32 pad.
    active: int | None = None
    on_ms: int | None = None
    off_ms: int | None = None
    # A line whose pulse has one threshold, with `active`, in place of on_ms and off_ms.
    pulse_ms: int | None = None


@dataclass(frozen=True)
class SdCardRoute:
    """`[sd_card]`: the GPIOs of an SD card slot wired for SPI mode, as the chip sees them."""

    sclk: int
    mosi: int
    miso: int
    cs: int


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
    lines: Mapping[str, Mapping[str, LineRoute]]
    sd_card: SdCardRoute | None = None
    # `uart0` or `usb-serial-jtag`: where the firmware's console is.
    console: ConsoleKind = "uart0"

    @property
    def interfaces(self) -> list[str]:
        """`console`, which every board has, then `uart.<role>` and `lines.<role>`, as the
        profile lists them, then `sd_card` when the board has a slot, then `wifi` and
        `bluetooth`, which every chip esp32tl runs has."""
        return [
            "console",
            *(f"uart.{role}" for role in self.uart),
            *(f"lines.{role}" for role in self.lines),
            *(["sd_card"] if self.sd_card is not None else []),
            "wifi",
            "bluetooth",
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
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ProfileError(f"{path.name}: {error}") from None
    return Reader(path.name).profile(data)


class Reader:
    """Checks a profile's data key by key; every error names the file and the key."""

    def __init__(self, file: str) -> None:
        self.file = file
        self.chip = CHIPS["esp32"]

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
        pin = self.integer(key, value, 0, max(self.chip.gpios))
        if pin not in self.chip.gpios:
            raise self.fail(key, f"the {self.chip.title} has no GPIO {pin}")
        if output and pin in self.chip.input_only:
            raise self.fail(key, f"GPIO {pin} is input only")
        return pin

    def roles(self, key: str, value: object) -> dict[str, Any]:
        table = self.table(key, value)
        for role in table:
            if not ROLE.fullmatch(role):
                raise self.fail(f"{key}.{role}", "a role is lowercase letters, digits and _")
        return table

    def profile(self, data: dict[str, Any]) -> BoardProfile:
        self.keys(
            "",
            data,
            {"name", "chip"},
            {"console", "flash_mb", "psram_mb", "uart", "lines", "sd_card"},
        )
        name = data["name"]
        stem = self.file.removesuffix(".toml")
        if name != stem:
            raise self.fail("name", f"{name!r} is not the file's name, {stem!r}")
        # A string first: an array or a table is no key of CHIPS, and not hashable either.
        if not isinstance(data["chip"], str) or data["chip"] not in CHIPS:
            raise self.fail("chip", f"{data['chip']!r} is not one of {', '.join(CHIPS)}")
        self.chip = CHIPS[data["chip"]]
        console = self.console(data.get("console", UART0))
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
        sd_card = self.sd_card(data["sd_card"]) if "sd_card" in data else None
        self.distinct(uart, lines, sd_card, console)
        return BoardProfile(
            name=name,
            chip=data["chip"],
            flash_mb=sizes["flash_mb"],
            psram_mb=sizes["psram_mb"],
            uart=MappingProxyType(uart),
            lines=MappingProxyType({role: MappingProxyType(ls) for role, ls in lines.items()}),
            sd_card=sd_card,
            console=console,
        )

    def console(self, value: object) -> ConsoleKind:
        """`console`: one of `CONSOLES`, and USB-Serial/JTAG only on a chip that has it."""
        if not isinstance(value, str) or value not in CONSOLES:
            raise self.fail("console", f"{value!r} is not one of {', '.join(CONSOLES)}")
        if value == USB_SERIAL_JTAG and not self.chip.usb_serial_jtag:
            raise self.fail(
                "console", f"the {self.chip.title} has no USB-Serial/JTAG controller; use {UART0}"
            )
        return USB_SERIAL_JTAG if value == USB_SERIAL_JTAG else UART0

    def uart(self, key: str, value: object) -> UartRoute:
        table = self.table(key, value)
        self.keys(key, table, {"port", "tx", "rx", "baud"}, set())
        return UartRoute(
            port=self.integer(f"{key}.port", table["port"], 0, self.chip.uarts - 1),
            tx=self.gpio(f"{key}.tx", table["tx"], output=True),
            rx=self.gpio(f"{key}.rx", table["rx"]),
            baud=self.integer(f"{key}.baud", table["baud"], 1),
        )

    def sd_card(self, value: object) -> SdCardRoute:
        table = self.table("sd_card", value)
        self.keys("sd_card", table, {"sclk", "mosi", "miso", "cs"}, set())
        # The chip drives SCLK, MOSI and CS; the card drives MISO.
        return SdCardRoute(
            sclk=self.gpio("sd_card.sclk", table["sclk"], output=True),
            mosi=self.gpio("sd_card.mosi", table["mosi"], output=True),
            miso=self.gpio("sd_card.miso", table["miso"]),
            cs=self.gpio("sd_card.cs", table["cs"], output=True),
        )

    def lines(self, key: str, value: object) -> dict[str, LineRoute]:
        lines = {}
        for role, line in self.roles(key, value).items():
            where = f"{key}.{role}"
            if not isinstance(line, dict):
                lines[role] = LineRoute(self.gpio(where, line))
                continue
            self.keys(where, line, {"pin"}, {"pull", "active", "on_ms", "off_ms", "pulse_ms"})
            pull = line.get("pull")
            if pull is not None and pull not in PULLS:
                raise self.fail(f"{where}.pull", f"{pull!r} is not one of {', '.join(PULLS)}")
            timing = {"active", "on_ms", "off_ms", "pulse_ms"} & line.keys()
            if timing and timing not in ({"active", "on_ms", "off_ms"}, {"active", "pulse_ms"}):
                raise self.fail(
                    where,
                    "pulse timing needs active with on_ms and off_ms, or active with pulse_ms",
                )
            lines[role] = LineRoute(
                self.gpio(f"{where}.pin", line["pin"], output=bool(timing)),
                pull,
                self.integer(f"{where}.active", line["active"], 0, 1) if timing else None,
                self.integer(f"{where}.on_ms", line["on_ms"], 1) if "on_ms" in timing else None,
                self.integer(f"{where}.off_ms", line["off_ms"], 1) if "off_ms" in timing else None,
                self.integer(f"{where}.pulse_ms", line["pulse_ms"], 1)
                if "pulse_ms" in timing
                else None,
            )
        return lines

    def distinct(
        self,
        uart: dict[str, UartRoute],
        lines: dict[str, dict[str, LineRoute]],
        sd_card: SdCardRoute | None,
        console: str,
    ) -> None:
        """One UART per route, and one use per pin; a console on UART0 takes UART0 and its pins,
        one on USB-Serial/JTAG the controller's USB D- and D+ pads instead: firmware that
        routes a peripheral there takes the pads from the controller, and the console with them."""
        on_uart0 = console == UART0
        ports = {CONSOLE_PORT: "console"} if on_uart0 else {}
        for role, route in uart.items():
            if route.port in ports:
                raise self.fail(f"uart.{role}.port", f"UART{route.port} is {ports[route.port]}'s")
            ports[route.port] = f"uart.{role}"
        pins = dict(self.chip.console_pins if on_uart0 else self.chip.usb_pins)
        uses = [
            (f"uart.{r}.{end}", getattr(u, end)) for r, u in uart.items() for end in ("tx", "rx")
        ]
        uses += [(f"lines.{r}.{n}", line.pin) for r, ls in lines.items() for n, line in ls.items()]
        if sd_card is not None:
            uses += [
                (f"sd_card.{end}", getattr(sd_card, end)) for end in ("sclk", "mosi", "miso", "cs")
            ]
        for key, pin in uses:
            if pin in pins:
                raise self.fail(key, f"GPIO {pin} is {pins[pin]} already")
            pins[pin] = key
