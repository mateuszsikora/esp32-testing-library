"""Board profiles and the capability declaration (plan §4.5): plain pytest, no emulator."""

import tomllib
from pathlib import Path

import pytest

from esp32tl import boards
from esp32tl.backends import SERVICES
from esp32tl.boards import BoardProfile, Line, ProfileError, UartRoute, capabilities
from esp32tl.boards.capabilities import (
    Capability,
    CapabilityError,
    Request,
    check,
    declared,
    refusal,
)


def test_the_shipped_profiles_are_the_two_of_the_plan() -> None:
    assert boards.names() == ["esp32-devkitc", "lilygo-t-a7670g-r2"]
    assert boards.DEFAULT_PROFILE == "esp32-devkitc"


def test_esp32_devkitc_routes_the_gnss_on_uart1_gpio_4_and_5() -> None:
    profile = boards.profile("esp32-devkitc")
    assert profile == BoardProfile(
        name="esp32-devkitc",
        chip="esp32",
        flash_mb=4,
        psram_mb=0,
        uart={"gnss": UartRoute(port=1, tx=4, rx=5, baud=9600)},
        lines={},
    )
    assert profile.interfaces == ["console", "uart.gnss"]


def test_the_lilygo_profile_holds_only_what_the_specification_gives() -> None:
    # The file's data as it stands, so that a pin, a size or a GNSS route added from memory fails
    # here: they wait for the vendor schematic.
    with (boards.PROFILES / "lilygo-t-a7670g-r2.toml").open("rb") as file:
        assert tomllib.load(file) == {
            "name": "lilygo-t-a7670g-r2",
            "chip": "esp32",
            "uart": {"modem": {"port": 1, "tx": 26, "rx": 27, "baud": 115200}},
            "lines": {"modem": {"pwrkey": 4, "ri": {"pin": 33, "pull": "down"}}},
        }
    profile = boards.profile("lilygo-t-a7670g-r2")
    assert profile.flash_mb is None and profile.psram_mb is None
    assert profile.uart == {"modem": UartRoute(port=1, tx=26, rx=27, baud=115200)}
    assert profile.lines == {"modem": {"pwrkey": Line(4), "ri": Line(33, "down")}}
    assert profile.interfaces == ["console", "uart.modem", "lines.modem"]


def test_a_profile_is_loaded_once_and_cannot_be_changed() -> None:
    profile = boards.profile("esp32-devkitc")
    assert boards.profile("esp32-devkitc") is profile
    with pytest.raises(TypeError):
        profile.uart["modem"] = UartRoute(port=2, tx=26, rx=27, baud=115200)  # type: ignore[index]


def test_an_unknown_profile_name_lists_the_profiles() -> None:
    with pytest.raises(ProfileError) as error:
        boards.profile("esp32-wrover-kit")
    assert str(error.value) == (
        "no board profile named 'esp32-wrover-kit';"
        " the profiles are esp32-devkitc, lilygo-t-a7670g-r2"
    )


GOOD = """\
name = "x"
chip = "esp32"

[uart.gnss]
port = 1
tx = 4
rx = 5
baud = 9600

[lines.modem]
pwrkey = 12
ri = { pin = 33, pull = "down" }
"""


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("name = 'x'", "x.toml: missing chip"),
        ("name = 'x'\nchip = 'esp32'\npins = 3", "x.toml: unknown key pins"),
        (GOOD.replace("port = 1", "port = 0"), "x.toml: uart.gnss.port: UART0 is console's"),
        (GOOD.replace("tx = 4", "tx = 1"), "x.toml: uart.gnss.tx: GPIO 1 is console.tx already"),
        (
            GOOD.replace("pwrkey = 12", "pwrkey = 3"),
            "x.toml: lines.modem.pwrkey: GPIO 3 is console.rx already",
        ),
        ("name = 'y'\nchip = 'esp32'", "x.toml: name: 'y' is not the file's name, 'x'"),
        ("name = 'x'\nchip = 'esp32s3'", "x.toml: chip: 'esp32s3' is not one of esp32"),
        (
            "name = 'x'\nchip = 'esp32'\nflash_mb = -4",
            "x.toml: flash_mb: -4 is out of range, at least 0",
        ),
        ("name = 'x'\nchip = 'esp32'\npsram_mb = '4'", "x.toml: psram_mb: '4' is not an integer"),
        (
            GOOD.replace("port = 1", "port = 3"),
            "x.toml: uart.gnss.port: 3 is out of range, from 0 to 2",
        ),
        (GOOD.replace("port = 1", "port = true"), "x.toml: uart.gnss.port: True is not an integer"),
        (GOOD.replace("tx = 4", "tx = 34"), "x.toml: uart.gnss.tx: GPIO 34 is input only"),
        (GOOD.replace("rx = 5", "rx = 24"), "x.toml: uart.gnss.rx: the ESP32 has no GPIO 24"),
        (
            GOOD.replace("rx = 5", "rx = 40"),
            "x.toml: uart.gnss.rx: 40 is out of range, from 0 to 39",
        ),
        (
            GOOD.replace("baud = 9600", "baud = 0"),
            "x.toml: uart.gnss.baud: 0 is out of range, at least 1",
        ),
        (GOOD.replace("baud = 9600\n", ""), "x.toml: uart.gnss: missing baud"),
        (
            GOOD.replace("[uart.gnss]", "[uart.GNSS]"),
            "x.toml: uart.GNSS: a role is lowercase letters, digits and _",
        ),
        (
            GOOD.replace("pwrkey = 12", "pwrkey = 'x'"),
            "x.toml: lines.modem.pwrkey: 'x' is not an integer",
        ),
        (
            GOOD.replace('"down"', '"high"'),
            "x.toml: lines.modem.ri.pull: 'high' is not one of up, down",
        ),
        (GOOD.replace("pin = 33,", ""), "x.toml: lines.modem.ri: missing pin"),
        (
            GOOD.replace("pwrkey = 12", "pwrkey = 5"),
            "x.toml: lines.modem.pwrkey: GPIO 5 is uart.gnss.rx already",
        ),
        (GOOD.replace("rx = 5", "rx = 4"), "x.toml: uart.gnss.rx: GPIO 4 is uart.gnss.tx already"),
        (
            GOOD + "[uart.modem]\nport = 1\ntx = 26\nrx = 27\nbaud = 115200\n",
            "x.toml: uart.modem.port: UART1 is uart.gnss's",
        ),
        ("name = 'x'\nchip = 'esp32'\nuart = 1", "x.toml: uart: is not a table"),
        ("name = 'x'\nchip = ", "x.toml: Invalid value (at end of document)"),
    ],
)
def test_a_profile_that_breaks_the_schema_is_refused_with_the_key(
    tmp_path: Path, text: str, message: str
) -> None:
    (tmp_path / "x.toml").write_text(text)
    with pytest.raises(ProfileError) as error:
        boards.load(tmp_path / "x.toml")
    assert str(error.value) == message


def test_a_profile_without_sizes_or_interfaces_has_only_the_console(tmp_path: Path) -> None:
    (tmp_path / "bare.toml").write_text("name = 'bare'\nchip = 'esp32'\n")
    profile = boards.load(tmp_path / "bare.toml")
    assert (profile.flash_mb, profile.psram_mb, profile.interfaces) == (None, None, ["console"])
    (tmp_path / "x.toml").write_text(GOOD)
    assert boards.load(tmp_path / "x.toml").interfaces == ["console", "uart.gnss", "lines.modem"]


def test_every_backend_has_a_declaration() -> None:
    assert SERVICES.keys() <= declared().keys()


def test_the_declaration_is_the_specification_s_table() -> None:
    # Issue #1, "Backends and what each can swap", and plan finding 11: on a real board an
    # on-board UART is real only, and only the console can be tapped.
    assert declared() == {
        "esp32sim": {
            "console": Capability(tap=True),
            "uart": Capability(("model", "replay", "real"), tap=True),
        },
        "serial": {"console": Capability(tap=True), "uart": Capability(("real",))},
    }


DEVKITC = boards.profile("esp32-devkitc")
LILYGO = boards.profile("lilygo-t-a7670g-r2")


@pytest.mark.parametrize(
    ("backend", "request_", "reason"),
    [
        ("esp32sim", Request("uart.gnss", source="model"), None),
        ("esp32sim", Request("uart.gnss", tap=True), None),
        ("serial", Request("uart.gnss", source="real"), None),
        ("serial", Request("console", tap=True), None),
        (
            "serial",
            Request("uart.gnss", source="model"),
            "uart.gnss takes no model source on this backend, only real",
        ),
        (
            "serial",
            Request("uart.gnss", source="replay"),
            "uart.gnss takes no replay source on this backend, only real",
        ),
        ("serial", Request("uart.gnss", tap=True), "uart.gnss cannot be tapped on this backend"),
    ],
)
def test_a_backend_refuses_what_it_does_not_declare(
    backend: str, request_: Request, reason: str | None
) -> None:
    check([request_], DEVKITC, declared())
    assert refusal(backend, [request_], declared()) == reason


def test_the_first_request_a_backend_cannot_give_is_the_reason() -> None:
    requests = [
        Request("console", tap=True),
        Request("uart.gnss", source="real"),
        Request("uart.gnss", tap=True),
        Request("uart.gnss", source="model"),
    ]
    assert refusal("serial", requests, declared()) == "uart.gnss cannot be tapped on this backend"
    assert refusal("esp32sim", requests, declared()) is None


@pytest.mark.parametrize(
    ("profile", "request_", "message"),
    [
        (
            DEVKITC,
            Request("uart.modem", source="model"),
            "the board profile esp32-devkitc has no uart.modem;"
            " its interfaces are console, uart.gnss",
        ),
        (
            DEVKITC,
            Request("uart.gnss", source="recorded"),
            "no backend takes a 'recorded' source on uart.gnss;"
            " the sources there are model, replay, real",
        ),
        (DEVKITC, Request("console", source="model"), "no backend takes a source on console"),
        (
            LILYGO,
            Request("lines.modem", source="model"),
            "no backend takes a source on lines.modem",
        ),
        (LILYGO, Request("lines.modem", tap=True), "no backend can tap lines.modem"),
    ],
)
def test_a_request_no_backend_can_give_is_an_error_not_a_skip(
    profile: BoardProfile, request_: Request, message: str
) -> None:
    with pytest.raises(CapabilityError) as error:
        check([request_], profile, declared())
    assert str(error.value) == message


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("serial = 1", "c.toml: serial: is not a table"),
        ("[serial]\nuart = 1", "c.toml: serial.uart: is not a table"),
        ("[serial]\nuart = { sniff = true }", "c.toml: serial.uart: unknown key sniff"),
        (
            "[serial]\nuart = { sources = 'real' }",
            "c.toml: serial.uart.sources: is not a list of names",
        ),
        ("[serial]\nuart = { tap = 1 }", "c.toml: serial.uart.tap: is not true or false"),
        (
            "[serial]\nuarts = { tap = true }",
            "c.toml: serial.uarts: no such kind of interface; the kinds are console, uart, lines",
        ),
        (
            "[serial]\nuart = { sources = ['modle'] }",
            "c.toml: serial.uart.sources: 'modle' is not one of model, replay, real",
        ),
    ],
)
def test_a_declaration_that_breaks_the_schema_is_refused(
    tmp_path: Path, text: str, message: str
) -> None:
    (tmp_path / "c.toml").write_text(text)
    with pytest.raises(CapabilityError) as error:
        capabilities.load(tmp_path / "c.toml")
    assert str(error.value) == message
