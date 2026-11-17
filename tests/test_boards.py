"""Board profiles and the capability declaration (plan §4.5): plain pytest, no emulator."""

import re
import tomllib
from pathlib import Path

import pytest

from esp32tl import boards
from esp32tl.backends import SERVICES
from esp32tl.boards import BoardProfile, Line, ProfileError, SdCardRoute, UartRoute, capabilities
from esp32tl.boards.capabilities import (
    Capability,
    CapabilityError,
    Request,
    check,
    declared,
    refusal,
)


def test_the_shipped_profiles_include_the_example_assembly() -> None:
    assert boards.names() == [
        "esp32-c3-devkitm",
        "esp32-c6-devkitc",
        "esp32-devkitc",
        "esp32-s3-devkitc",
        "gps-alarm-clock",
        "lilygo-t-a7670g-r2",
        "sd-data-logger",
    ]
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
        # ESP-IDF's sdspi example's defaults for the ESP32
        sd_card=SdCardRoute(sclk=14, mosi=15, miso=2, cs=13),
    )
    assert profile.interfaces == ["console", "uart.gnss", "sd_card", "wifi", "bluetooth"]


def test_the_lilygo_profile_holds_the_specification_and_lilygo_s_documentation() -> None:
    # The file's data as it stands, so that a pin or a size added from memory fails here: the
    # modem's original route and lines are the specification's; added wiring is LilyGO's.
    with (boards.PROFILES / "lilygo-t-a7670g-r2.toml").open("rb") as file:
        assert tomllib.load(file) == {
            "name": "lilygo-t-a7670g-r2",
            "chip": "esp32",
            "flash_mb": 4,
            "psram_mb": 4,
            "uart": {
                "modem": {"port": 1, "tx": 26, "rx": 27, "baud": 115200},
                "gnss": {"port": 2, "tx": 21, "rx": 22, "baud": 9600},
            },
            "lines": {
                "modem": {
                    "pwrkey": {"pin": 4, "active": 1, "on_ms": 50, "off_ms": 2500},
                    "ri": {"pin": 33, "pull": "down"},
                    "dtr": 25,
                    "reset": {"pin": 5, "active": 1, "pulse_ms": 2000},
                },
                "gnss": {"wake": 19, "pps": 23},
                "board": {"power_enable": 12},
            },
            "sd_card": {"sclk": 14, "mosi": 15, "miso": 2, "cs": 13},
        }
    profile = boards.profile("lilygo-t-a7670g-r2")
    assert (profile.flash_mb, profile.psram_mb) == (4, 4)
    assert profile.uart == {
        "modem": UartRoute(port=1, tx=26, rx=27, baud=115200),
        "gnss": UartRoute(port=2, tx=21, rx=22, baud=9600),
    }
    assert profile.lines == {
        "modem": {
            "pwrkey": Line(4, active=1, on_ms=50, off_ms=2500),
            "ri": Line(33, "down"),
            "dtr": Line(25),
            "reset": Line(5, active=1, pulse_ms=2000),
        },
        "gnss": {"wake": Line(19), "pps": Line(23)},
        "board": {"power_enable": Line(12)},
    }
    assert profile.sd_card == SdCardRoute(sclk=14, mosi=15, miso=2, cs=13)
    assert profile.interfaces == [
        "console",
        "uart.modem",
        "uart.gnss",
        "lines.modem",
        "lines.gnss",
        "lines.board",
        "sd_card",
        "wifi",
        "bluetooth",
    ]


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
        " the profiles are esp32-c3-devkitm, esp32-c6-devkitc, esp32-devkitc, esp32-s3-devkitc,"
        " gps-alarm-clock, lilygo-t-a7670g-r2, sd-data-logger"
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

SD_CARD = "\n[sd_card]\nsclk = 14\nmosi = 15\nmiso = 2\ncs = 13\n"


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
        (
            "name = 'x'\nchip = 'esp32p4'",
            "x.toml: chip: 'esp32p4' is not one of esp32, esp32s3, esp32c3, esp32c6",
        ),
        (
            "name = 'x'\nchip = ['esp32c3']",
            "x.toml: chip: ['esp32c3'] is not one of esp32, esp32s3, esp32c3, esp32c6",
        ),
        (
            "name = 'x'\nchip = { name = 'esp32c3' }",
            "x.toml: chip: {'name': 'esp32c3'} is not one of esp32, esp32s3, esp32c3, esp32c6",
        ),
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
        ("name = 'x'\nchip = 'esp32'\nsd_card = 1", "x.toml: sd_card: is not a table"),
        (GOOD + SD_CARD.replace("cs = 13\n", ""), "x.toml: sd_card: missing cs"),
        (GOOD + SD_CARD + "cd = 27\n", "x.toml: sd_card: unknown key cd"),
        (
            GOOD + SD_CARD.replace("cs = 13", "cs = 36"),
            "x.toml: sd_card.cs: GPIO 36 is input only",
        ),
        (
            GOOD + SD_CARD.replace("mosi = 15", "mosi = 4"),
            "x.toml: sd_card.mosi: GPIO 4 is uart.gnss.tx already",
        ),
        (
            GOOD + SD_CARD.replace("miso = 2", "miso = 13"),
            "x.toml: sd_card.cs: GPIO 13 is sd_card.miso already",
        ),
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
    assert (profile.flash_mb, profile.psram_mb, profile.interfaces) == (
        None,
        None,
        ["console", "wifi", "bluetooth"],
    )
    (tmp_path / "x.toml").write_text(GOOD)
    assert boards.load(tmp_path / "x.toml").interfaces == [
        "console",
        "uart.gnss",
        "lines.modem",
        "wifi",
        "bluetooth",
    ]


def test_an_sd_card_slot_may_take_an_input_only_pin_for_miso(tmp_path: Path) -> None:
    (tmp_path / "x.toml").write_text(GOOD + SD_CARD.replace("miso = 2", "miso = 39"))
    profile = boards.load(tmp_path / "x.toml")
    assert profile.sd_card == SdCardRoute(sclk=14, mosi=15, miso=39, cs=13)
    assert profile.interfaces[-3:] == ["sd_card", "wifi", "bluetooth"]


def test_every_backend_has_a_declaration() -> None:
    assert SERVICES.keys() <= declared().keys()


def test_the_declaration_is_the_specification_s_table() -> None:
    # Issue #1, "Backends and what each can swap", and plan finding 11: on a real board an
    # on-board UART is real only, and only the console can be tapped.
    assert declared() == {
        "esp32sim": {
            "console": Capability(tap=True),
            "uart": Capability(("model", "replay", "real"), tap=True),
            # the runner's card model, holding an image file
            "sd_card": Capability(("model",)),
            # esp32sim's access point, with the simulated network behind it
            "wifi": Capability(("model",)),
            # the guest's controller in the harness, on the air with the test's devices, and the
            # HCI between it and the firmware's host, tapped as btsnoop
            "bluetooth": Capability(("model",), tap=True),
        },
        "serial": {
            "console": Capability(tap=True),
            "uart": Capability(("real",)),
            "sd_card": Capability(("real",)),
            "wifi": Capability(("real",)),
        },
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
        ("esp32sim", Request("sd_card", source="model"), None),
        ("esp32sim", Request("wifi", source="model"), None),
        (
            "serial",
            Request("sd_card", source="model"),
            "sd_card takes no model source on this backend, only real",
        ),
        ("esp32sim", Request("bluetooth", source="model"), None),
        ("esp32sim", Request("bluetooth", tap=True), None),
        (
            "serial",
            Request("bluetooth", source="model"),
            "bluetooth takes no model source on this backend",
        ),
        ("serial", Request("bluetooth", tap=True), "bluetooth cannot be tapped on this backend"),
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
            " its interfaces are console, uart.gnss, sd_card, wifi, bluetooth",
        ),
        (
            boards.profile("gps-alarm-clock"),
            Request("sd_card", source="model"),
            "the board profile gps-alarm-clock has no sd_card;"
            " its interfaces are console, uart.gnss, lines.alarm, wifi, bluetooth",
        ),
        (DEVKITC, Request("sd_card", tap=True), "no backend can tap sd_card"),
        (
            DEVKITC,
            Request("wifi", source="replay"),
            "no backend takes a 'replay' source on wifi; the sources there are model, real",
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
            "c.toml: serial.uarts: no such kind of interface;"
            " the kinds are console, uart, lines, sd_card, wifi, bluetooth",
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


TIMING = "pulse timing needs active with on_ms and off_ms, or active with pulse_ms"


@pytest.mark.parametrize(
    ("timing", "message"),
    [
        ("active = 1", TIMING),
        ("active = 1, on_ms = 100", TIMING),
        ("pulse_ms = 2000", TIMING),
        ("active = 1, on_ms = 100, off_ms = 3000, pulse_ms = 2000", TIMING),
        ("active = 1, pulse_ms = 0", "pulse_ms: 0 is out of range"),
        ("active = 1, pulse_ms = 2.0", "pulse_ms: 2.0 is not an integer"),
        ("active = 2, on_ms = 100, off_ms = 3000", "active: 2 is out of range"),
        ("active = true, on_ms = 100, off_ms = 3000", "active: True is not an integer"),
        ("active = 1, on_ms = 0, off_ms = 3000", "on_ms: 0 is out of range"),
        ("active = 1, on_ms = 100, off_ms = 3.0", "off_ms: 3.0 is not an integer"),
    ],
)
def test_pulse_timing_is_checked_as_a_complete_output_line(
    tmp_path: Path,
    timing: str,
    message: str,
) -> None:
    path = tmp_path / "x.toml"
    path.write_text(GOOD.replace("pwrkey = 12", f"pwrkey = {{pin = 12, {timing}}}"))
    with pytest.raises(ProfileError, match=re.escape(message)):
        boards.load(path)


def test_a_timed_line_must_be_output_capable(tmp_path: Path) -> None:
    path = tmp_path / "x.toml"
    path.write_text(
        GOOD.replace("pwrkey = 12", "pwrkey = {pin = 34, active = 0, on_ms = 100, off_ms = 3000}")
    )
    with pytest.raises(ProfileError, match="GPIO 34 is input only"):
        boards.load(path)


# A profile for each chip besides the classic ESP32: the chip's pads, its UARTs and its console.
S3 = GOOD.replace('chip = "esp32"', 'chip = "esp32s3"').replace(
    "ri = { pin = 33,", "ri = { pin = 6,"
)
C3 = S3.replace('chip = "esp32s3"', 'chip = "esp32c3"').replace("pwrkey = 12", "pwrkey = 7")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (S3.replace("rx = 5", "rx = 22"), "x.toml: uart.gnss.rx: the ESP32-S3 has no GPIO 22"),
        (S3.replace("rx = 5", "rx = 49"), "x.toml: uart.gnss.rx: 49 is out of range, from 0 to 48"),
        (S3.replace("tx = 4", "tx = 43"), "x.toml: uart.gnss.tx: GPIO 43 is console.tx already"),
        (
            C3.replace("port = 1", "port = 2"),
            "x.toml: uart.gnss.port: 2 is out of range, from 0 to 1",
        ),
        (C3.replace("rx = 5", "rx = 22"), "x.toml: uart.gnss.rx: 22 is out of range, from 0 to 21"),
        (C3.replace("tx = 4", "tx = 20"), "x.toml: uart.gnss.tx: GPIO 20 is console.rx already"),
        (
            C3.replace('chip = "esp32c3"', 'chip = "esp32c6"').replace("tx = 4", "tx = 16"),
            "x.toml: uart.gnss.tx: GPIO 16 is console.tx already",
        ),
    ],
)
def test_a_profile_takes_the_pads_and_uarts_of_its_chip(
    tmp_path: Path, text: str, message: str
) -> None:
    path = tmp_path / "x.toml"
    path.write_text(text)
    with pytest.raises(ProfileError) as error:
        boards.load(path)
    assert str(error.value) == message


def test_gpio_34_drives_on_the_s3_where_the_classic_esp32_has_it_input_only(
    tmp_path: Path,
) -> None:
    path = tmp_path / "x.toml"
    path.write_text(S3.replace("tx = 4", "tx = 34"))
    assert boards.load(path).uart["gnss"].tx == 34


@pytest.mark.parametrize(
    ("name", "chip", "console", "psram"),
    [
        ("esp32-s3-devkitc", "esp32s3", (43, 44), 8),
        ("esp32-c3-devkitm", "esp32c3", (21, 20), 0),
        ("esp32-c6-devkitc", "esp32c6", (16, 17), 0),
    ],
)
def test_each_chip_s_devkit_routes_the_gnss_and_the_echo_lines(
    name: str, chip: str, console: tuple[int, int], psram: int
) -> None:
    profile = boards.profile(name)
    assert (profile.chip, profile.psram_mb) == (chip, psram)
    assert profile.uart["gnss"] == UartRoute(port=1, tx=4, rx=5, baud=9600)
    assert profile.lines["echo"]["in"].pin == 6 and profile.lines["echo"]["in"].pull == "down"
    assert profile.lines["echo"]["out"].pin == 7
    the_chip = boards.CHIPS[chip]
    assert (the_chip.console_pin("tx"), the_chip.console_pin("rx")) == console
    assert profile.interfaces == ["console", "uart.gnss", "lines.echo", "wifi", "bluetooth"]


def test_the_console_is_uart0_unless_the_profile_says_usb_serial_jtag(tmp_path: Path) -> None:
    path = tmp_path / "x.toml"
    path.write_text("name = 'x'\nchip = 'esp32s3'\n")
    assert boards.load(path).console == "uart0"
    path.write_text("name = 'x'\nchip = 'esp32s3'\nconsole = 'uart0'\n")
    assert boards.load(path).console == "uart0"
    for chip in ("esp32s3", "esp32c3", "esp32c6"):
        path.write_text(f"name = 'x'\nchip = '{chip}'\nconsole = 'usb-serial-jtag'\n")
        profile = boards.load(path)
        assert (profile.console, profile.interfaces[0]) == ("usb-serial-jtag", "console")
    # every shipped profile's console is UART0: their devkits have a USB-to-UART bridge
    assert {boards.profile(name).console for name in boards.names()} == {"uart0"}


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (
            "name = 'x'\nchip = 'esp32'\nconsole = 'usb-serial-jtag'\n",
            "x.toml: console: the ESP32 has no USB-Serial/JTAG controller; use uart0",
        ),
        (
            "name = 'x'\nchip = 'esp32s3'\nconsole = 'usb'\n",
            "x.toml: console: 'usb' is not one of uart0, usb-serial-jtag",
        ),
        (
            "name = 'x'\nchip = 'esp32s3'\nconsole = 1\n",
            "x.toml: console: 1 is not one of uart0, usb-serial-jtag",
        ),
    ],
)
def test_a_console_the_chip_does_not_have_is_refused(
    tmp_path: Path, text: str, message: str
) -> None:
    (tmp_path / "x.toml").write_text(text)
    with pytest.raises(ProfileError) as error:
        boards.load(tmp_path / "x.toml")
    assert str(error.value) == message


def test_a_console_on_usb_serial_jtag_leaves_uart0_and_its_pins_to_the_routes(
    tmp_path: Path,
) -> None:
    route = "[uart.uart0]\nport = 0\ntx = 43\nrx = 44\nbaud = 115200\n"
    path = tmp_path / "x.toml"
    path.write_text(f"name = 'x'\nchip = 'esp32s3'\nconsole = 'usb-serial-jtag'\n{route}")
    assert boards.load(path).uart["uart0"] == UartRoute(port=0, tx=43, rx=44, baud=115200)
    path.write_text(
        "name = 'x'\nchip = 'esp32s3'\nconsole = 'usb-serial-jtag'\n[lines.l]\na = 43\n"
    )
    assert boards.load(path).lines["l"]["a"].pin == 43
    # with the console on UART0, both are its
    path.write_text(f"name = 'x'\nchip = 'esp32s3'\n{route}")
    with pytest.raises(
        ProfileError, match=re.escape("x.toml: uart.uart0.port: UART0 is console's")
    ):
        boards.load(path)
    path.write_text("name = 'x'\nchip = 'esp32s3'\n[lines.l]\na = 43\n")
    with pytest.raises(ProfileError, match=re.escape("lines.l.a: GPIO 43 is console.tx already")):
        boards.load(path)


@pytest.mark.parametrize(
    ("chip", "dm", "dp"), [("esp32s3", 19, 20), ("esp32c3", 18, 19), ("esp32c6", 12, 13)]
)
def test_a_console_on_usb_serial_jtag_takes_the_usb_pads(
    tmp_path: Path, chip: str, dm: int, dp: int
) -> None:
    path = tmp_path / "x.toml"
    route = f"[uart.aux]\nport = 1\ntx = {dm}\nrx = 4\nbaud = 9600\n"
    path.write_text(f"name = 'x'\nchip = '{chip}'\nconsole = 'usb-serial-jtag'\n{route}")
    with pytest.raises(ProfileError) as error:
        boards.load(path)
    assert str(error.value) == f"x.toml: uart.aux.tx: GPIO {dm} is console.usb_dm already"
    path.write_text(
        f"name = 'x'\nchip = '{chip}'\nconsole = 'usb-serial-jtag'\n[lines.l]\na = {dp}\n"
    )
    with pytest.raises(ProfileError) as error:
        boards.load(path)
    assert str(error.value) == f"x.toml: lines.l.a: GPIO {dp} is console.usb_dp already"
    # with the console on UART0 the pads are GPIOs like any other
    path.write_text(f"name = 'x'\nchip = '{chip}'\n{route}")
    assert boards.load(path).uart["aux"].tx == dm
