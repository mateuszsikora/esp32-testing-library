"""The validation images as `make firmware` built them (plan M0), or as the CI test jobs
receive them from the firmware job: what the backend will flash into the runner."""

import json
import re
import warnings
from pathlib import Path

import pytest
from pytest_embedded_idf.app import IdfApp

from esp32tl import boards

FIRMWARE = Path(__file__).resolve().parent.parent / "firmware"


def pinned() -> tuple[str, str]:
    """The ESP-IDF version and container that firmware/build.sh pins."""
    script = (FIRMWARE / "build.sh").read_text()
    version = re.search(r"^IDF_VERSION=(\S+)$", script, re.MULTILINE)
    container = re.search(r"^CONTAINER=(\S+)$", script, re.MULTILINE)
    assert version is not None and container is not None, (
        "firmware/build.sh no longer sets IDF_VERSION= and CONTAINER= on lines of their own"
    )
    return version[1], container[1].replace("$IDF_VERSION", version[1])


def idf_app(image: str) -> IdfApp:
    """The image's build, which has to be finished and, unless it was built with IDF_LOCAL=1,
    built in the container build.sh pins now. A local build passes, with a warning."""
    build = FIRMWARE / image / "build"
    if not (build / "esp32tl-built").is_file():
        pytest.fail(f"{build} holds no finished build: run make firmware", pytrace=False)
    how = json.loads((build / "esp32tl-build.json").read_text())
    idf_version, container = pinned()
    if how["container"] is None:
        warnings.warn(
            f"{image} was built with a local ESP-IDF {how['idf_version']}, not in {container}",
            stacklevel=2,
        )
    elif (how["container"], how["idf_version"]) != (container, idf_version):
        pytest.fail(
            f"{image} was built in {how['container']} (ESP-IDF {how['idf_version']}),"
            f" not in {container} ({idf_version}): run make firmware",
            pytrace=False,
        )
    return IdfApp(str(FIRMWARE / image))


def test_hello_world_has_the_classic_esp32_flash_layout() -> None:
    app = idf_app("hello_world")
    build = FIRMWARE / "hello_world" / "build"
    assert app.target == "esp32"
    assert [(f.offset, Path(f.file_path).relative_to(build)) for f in app.flash_files] == [
        (0x1000, Path("bootloader/bootloader.bin")),
        (0x8000, Path("partition_table/partition-table.bin")),
        (0x10000, Path("hello_world.bin")),
    ]
    assert all(Path(f.file_path).is_file() for f in app.flash_files)
    assert app.elf_file == str((build / "hello_world.elf").resolve())


def test_nmea_reader_has_the_classic_esp32_flash_layout() -> None:
    app = idf_app("nmea_reader")
    build = FIRMWARE / "nmea_reader" / "build"
    assert app.target == "esp32"
    assert [(f.offset, Path(f.file_path).relative_to(build)) for f in app.flash_files] == [
        (0x1000, Path("bootloader/bootloader.bin")),
        (0x8000, Path("partition_table/partition-table.bin")),
        (0x10000, Path("nmea_reader.bin")),
    ]
    assert all(Path(f.file_path).is_file() for f in app.flash_files)
    assert app.elf_file == str((build / "nmea_reader.elf").resolve())


def test_nmea_reader_takes_the_devkit_gnss_route_from_sdkconfig() -> None:
    config = idf_app("nmea_reader").sdkconfig
    assert config["NMEA_READER_UART_PORT"] == 1
    assert config["NMEA_READER_UART_RX_PIN"] == 5
    assert config["NMEA_READER_UART_TX_PIN"] == 4
    assert config["NMEA_READER_UART_BAUD"] == 9600


def test_gpio_echo_has_the_classic_esp32_flash_layout() -> None:
    app = idf_app("gpio_echo")
    build = FIRMWARE / "gpio_echo" / "build"
    assert app.target == "esp32"
    assert [(f.offset, Path(f.file_path).relative_to(build)) for f in app.flash_files] == [
        (0x1000, Path("bootloader/bootloader.bin")),
        (0x8000, Path("partition_table/partition-table.bin")),
        (0x10000, Path("gpio_echo.bin")),
    ]
    assert all(Path(f.file_path).is_file() for f in app.flash_files)


def test_gpio_echo_takes_the_lilygo_modem_lines_from_sdkconfig() -> None:
    config = idf_app("gpio_echo").sdkconfig
    assert config["GPIO_ECHO_IN_PIN"] == 33
    assert config["GPIO_ECHO_OUT_PIN"] == 4


def test_sdspi_has_the_classic_esp32_flash_layout() -> None:
    app = idf_app("sdspi")
    build = FIRMWARE / "sdspi" / "build"
    assert app.target == "esp32"
    # the example's project is called sd_card
    assert [(f.offset, Path(f.file_path).relative_to(build)) for f in app.flash_files] == [
        (0x1000, Path("bootloader/bootloader.bin")),
        (0x8000, Path("partition_table/partition-table.bin")),
        (0x10000, Path("sd_card.bin")),
    ]
    assert all(Path(f.file_path).is_file() for f in app.flash_files)
    assert app.elf_file == str((build / "sd_card.elf").resolve())


def test_sdspi_takes_the_devkit_sd_card_slot_from_sdkconfig() -> None:
    config = idf_app("sdspi").sdkconfig
    slot = boards.profile("esp32-devkitc").sd_card
    assert slot is not None
    pins = ("EXAMPLE_PIN_CLK", "EXAMPLE_PIN_MOSI", "EXAMPLE_PIN_MISO", "EXAMPLE_PIN_CS")
    assert [config[pin] for pin in pins] == [slot.sclk, slot.mosi, slot.miso, slot.cs]
    # unmodified: a card it cannot mount stays as it is
    assert config["EXAMPLE_FORMAT_IF_MOUNT_FAILED"] is False
    assert config["EXAMPLE_FORMAT_SD_CARD"] is False


def test_pppos_client_has_the_classic_layout_and_lilygo_route() -> None:
    app = idf_app("pppos_client")
    build = FIRMWARE / "pppos_client" / "build"
    assert app.target == "esp32"
    assert [(f.offset, Path(f.file_path).relative_to(build)) for f in app.flash_files] == [
        (0x1000, Path("bootloader/bootloader.bin")),
        (0x8000, Path("partition_table/partition-table.bin")),
        (0x10000, Path("pppos_client.bin")),
    ]
    route = boards.profile("lilygo-t-a7670g-r2").uart["modem"]
    config = app.sdkconfig
    assert config["EXAMPLE_MODEM_DEVICE_SIM7600"] is True
    assert config["EXAMPLE_MODEM_UART_TX_PIN"] == route.tx
    assert config["EXAMPLE_MODEM_UART_RX_PIN"] == route.rx
    assert config["EXAMPLE_FLOW_CONTROL_NONE"] is True
    assert config["EXAMPLE_MQTT_BROKER_URI"] == "mqtt://mqtt.eclipseprojects.io"
    assert config["LWIP_PPP_SUPPORT"] is True
    assert all(Path(f.file_path).is_file() for f in app.flash_files)
