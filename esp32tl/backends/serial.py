"""A real ESP32 through pytest-embedded's esp,idf services, on the wall clock.

IdfSerial owns connecting and flashing. Between power_on and close we own its reader, stamping
batches after each read poll and forwarding them to pytest-embedded's log. Only the test's
thread drains those bytes into the console, so matching never races a resize of its bytearray.

IdfSerial flashes the files of an `IdfApp`, which reads them from `flasher_args.json`. A PlatformIO
build has none: `PlatformioApp` is the `IdfApp` the plugin gives pytest-embedded for one, holding
the files of `esp32tl.build`, so that IdfSerial writes the flash esp32sim runs.
"""

import functools
import queue
import shutil
import tempfile
import threading
import time
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn, TypeVar

from pytest_embedded.app import App
from pytest_embedded.log import MessageQueue
from pytest_embedded_idf.app import FlashFile, IdfApp
from pytest_embedded_idf.dut import IdfDut

from esp32tl import boards, build, flash, recording
from esp32tl.backends.clock import nanoseconds, seconds
from esp32tl.backends.esp32sim import BackendError, load_app, refuse, route_wire
from esp32tl.boards import BoardProfile, capabilities
from esp32tl.boards.capabilities import CapabilityError, Request
from esp32tl.flash import FlashSnapshot, Namespaces
from esp32tl.interfaces import BoardOff, NeverHappened, RunEnded, SourceError
from esp32tl.interfaces.bluetooth import Bluetooth
from esp32tl.interfaces.board import Board, Boot, Sleep
from esp32tl.interfaces.condition import waiting
from esp32tl.interfaces.console import ConsoleLines
from esp32tl.interfaces.gnss import Gnss
from esp32tl.interfaces.lines import Lines
from esp32tl.interfaces.modem import Modem
from esp32tl.interfaces.sd_card import SdCard
from esp32tl.interfaces.uart import Uart
from esp32tl.interfaces.wifi import Wifi
from esp32tl.link.boardlink import Wire
from esp32tl.report import RunLog, build_line, build_properties
from esp32tl.tap import Tap, app_image_sha256
from esp32tl.units import Duration

T = TypeVar("T")
POLL_SECONDS = 0.01
# One bounded join; a stuck daemon must not hold teardown for the whole test timeout.
READER_STOP_SECONDS = 1.0


@dataclass(frozen=True)
class Artifact:
    """The build `check_artifact` validated, and the hash of its application image."""

    app_sha256: str
    build: build.Build


def check_artifact(
    app_path: str,
    reference: Path | None = None,
    *,
    build_dir: str | Path | None = None,
    chip: str = "esp32",
) -> Artifact:
    """Validate the local build for `chip`, the board profile's, optionally against a recording,
    before opening a port.

    This compares app.bin of an ESP-IDF build, firmware.bin of a PlatformIO one, not the ELF hash
    stored in the ESP-IDF app descriptor. It does not establish what is on a board when autoflash
    is skipped.
    """
    app = load_app(app_path, build_dir=build_dir, backend=Serial.name, chip=chip)
    try:
        digest = app_image_sha256(app)
        if digest is None:
            raise BackendError(f"{app.directory}/flasher_args.json names no app image")
        if reference is not None:
            expected = recording.read(reference).header.app_sha256
            if expected is None:
                raise BackendError(f"{reference}: recording has no app_sha256")
            if expected != digest:
                raise BackendError(
                    f"{reference}: recording app_sha256 {expected} does not match artifact {digest}"
                )
    except (OSError, recording.RecordingError) as error:
        raise BackendError(str(error)) from error
    return Artifact(digest, app)


# What a PlatformIO build gives esptool's write-flash: the resets and the stub ESP-IDF's
# flasher_args.json names, and the images' flash mode, frequency and size kept as the build wrote
# them, so that the board gets the bytes esp32sim runs. IdfSerial.flash() passes `after` as
# hard_reset whatever its value.
PLATFORMIO_ESPTOOL_ARGS = {"before": "default-reset", "after": "hard-reset", "stub": True}
KEEP = {"flash_mode": "keep", "flash_freq": "keep", "flash_size": "keep"}
PLATFORMIO_WRITE_FLASH_ARGS = [
    "--flash-mode",
    "keep",
    "--flash-freq",
    "keep",
    "--flash-size",
    "keep",
]
# The partitions esp32sim has erased and a board may not: the otadata (data, ota), all of it, and
# the factory application (app, factory), its first sector, where its image header is.
DATA, OTA_DATA = 1, 0
APP, FACTORY = 0, 0
SECTOR = 0x1000
ERASED = b"\xff"


class PlatformioApp(IdfApp):
    """A PlatformIO build as pytest-embedded's `IdfSerial` flashes it: the build's files at their
    offsets (`esp32tl.build.platformio`), and, where no file of the build covers them, an erased
    otadata partition and, unless the build's own otadata selects a slot, an erased first sector
    of a `factory` partition, as the flash of esp32sim has them. Otherwise what a board had there
    would choose the application it boots: an otadata naming another slot, or an image left in
    `factory`, which the bootloader tries before `ota_0` with the otadata erased. The erased files
    are in a temporary directory, not in the build directory, removed with the app.

    pytest-embedded makes it from the `app` fixture's `app_path` and `build_dir`, as an `IdfApp`.
    `chip` is the board profile's, which `for_chip` gives a subclass of its own.
    """

    chip = "esp32"

    @classmethod
    @functools.cache
    def for_chip(cls, chip: str) -> type["PlatformioApp"]:
        """The class pytest-embedded makes the app from for a board of `chip`, one per chip: it
        calls the class with the fixture's arguments alone."""
        return cls if chip == cls.chip else type(cls.__name__, (cls,), {"chip": chip})

    def __init__(
        self,
        app_path: str | None = None,
        build_dir: str | None = None,
        part_tool: str | None = None,
        **kwargs: Any,
    ) -> None:
        # Not IdfApp.__init__, which reads flasher_args.json; the attributes it sets, from the
        # build instead.
        App.__init__(self, app_path, build_dir, **kwargs)
        self._sdkconfig = None
        self._target = None
        self._parttool = part_tool
        self._partition_table = None
        self.build = load_app(
            self.app_path, build_dir=self.binary_path, backend=Serial.name, chip=self.chip
        )
        table = flash.partitions(self.build.table())
        self._table: dict[str, dict[str, int]] = {
            p.name: {
                "type": p.type,
                "subtype": p.subtype,
                "offset": p.offset,
                "size": p.size,
                "flags": p.flags,
            }
            for p in table.values()
        }
        self.elf_file = str(self.build.elf) if self.build.elf is not None else None
        self.bin_file = str(self.build.app_image)
        self.is_loadable_elf = False
        self.is_linux_elf = False
        files = [FlashFile(f.offset, str(f.path)) for f in self.build.flash_files]
        otadata = [p for p in table.values() if (p.type, p.subtype) == (DATA, OTA_DATA)]
        factory = [p for p in table.values() if (p.type, p.subtype) == (APP, FACTORY)]
        # An otadata of the build's own that is not erased, boot_app0.bin's in an Arduino build's
        # firmware.factory.bin, selects a slot, and the bootloader does not try factory. The
        # erased ota_data_initial.bin of a framework = espidf build selects none.
        if any(self._covered(p.offset) and not self._erased(p) for p in otadata):
            factory = []
        directory: Path | None = None
        for p in otadata + factory:
            if self._covered(p.offset):
                continue
            if directory is None:
                directory = Path(tempfile.mkdtemp(prefix="esp32tl-erased-"))
                weakref.finalize(self, shutil.rmtree, directory, True)
            name = "otadata-erased.bin" if p in otadata else "factory-erased.bin"
            size = p.size if p in otadata else min(SECTOR, p.size)
            (directory / name).write_bytes(ERASED * size)
            files.append(FlashFile(p.offset, str(directory / name)))
        self.flash_files = sorted(files)
        self.flash_args = {
            "extra_esptool_args": {**PLATFORMIO_ESPTOOL_ARGS, "chip": self.build.chip},
            "write_flash_args": list(PLATFORMIO_WRITE_FLASH_ARGS),
            "flash_files": {hex(f.offset): f.file_path for f in self.flash_files},
        }
        self.flash_settings: dict[str, Any] = {**KEEP, "encrypt": False}

    def _erased(self, partition: flash.Partition) -> bool:
        """Whether the build's bytes of `partition`, as far as its file goes, are all erased."""
        data = build.flash_bytes(self.build, partition.offset, partition.size)
        return data.count(ERASED) == len(data)

    def _covered(self, offset: int) -> bool:
        return any(
            f.offset <= offset < f.offset + f.path.stat().st_size for f in self.build.flash_files
        )

    @property
    def target(self) -> str:
        return self.build.chip

    @property
    def partition_table(self) -> dict[str, Any]:
        """The build's table as IdfApp gives it, by name, from esp32tl's reader rather than
        ESP-IDF's gen_esp32part.py: `--erase-nvs` erases the `nvs` partition."""
        return self._table


class Serial(Board):
    """The real board. power_on is a reset, and so is reset; the test cannot switch the board's
    power, nor see the chip's resets. Hardware also runs outside the test's waits."""

    name = "serial"
    report_section = "serial console"
    report_property = "serial_console"
    clock = "wall"

    def __init__(
        self,
        dut: IdfDut,
        profile: BoardProfile,
        msg_queue: MessageQueue,
        *,
        artifact: Artifact,
        directory: Path,
        reference: Path | None = None,
        skip: Callable[[str], NoReturn] = refuse,
    ) -> None:
        self.dut = dut
        self.profile = profile
        self.directory = directory
        self.artifact = artifact
        self._skip = skip
        self._msg_queue = msg_queue
        self._output = bytearray()
        self._lines = ConsoleLines()
        self.log = RunLog()
        self._start: int | None = None
        self._utc = datetime.now(UTC)
        self._stop = threading.Event()
        self._reader: threading.Thread | None = None
        self._received: queue.Queue[tuple[int, bytes] | Exception] = queue.Queue()
        self._error: str | None = None
        self._closed = False
        self._ended_at = 0
        self._files: dict[Path, tuple[str, str]] = {
            Path(f.file_path).resolve(): ("artifact", "app") for f in dut.app.flash_files
        }
        if reference is not None:
            self._files[reference.resolve()] = ("recording", "app_recording")
        self._uarts: dict[str, Uart] = {}
        self._wifi: Wifi | None = None
        # UART0 on the classic ESP32's pins, or the chip's USB-Serial/JTAG, which has no pins: the
        # port is then the chip's own USB device, and its baud means nothing to it.
        usb = profile.console == boards.USB_SERIAL_JTAG
        self._console = Uart(
            "console",
            None if usb else Wire(1, 3, dut.serial.baud),
            log=self.log,
            clock=self.clock,
            directory=directory,
            asked=self._asked,
            files=self._files,
        )
        self._card = (
            SdCard(profile.sd_card, directory=directory, asked=self._asked, files=self._files)
            if profile.sd_card is not None
            else None
        )

    @property
    def app_sha256(self) -> str:
        """The app image's hash, checked before the port opened."""
        return self.artifact.app_sha256

    @property
    def powered(self) -> bool:
        return self._start is not None

    @property
    def started(self) -> bool:
        return self._start is not None

    @property
    def boots(self) -> tuple[Boot, ...]:
        self._skip("serial: the backend does not see the chip's resets")

    @property
    def sleeps(self) -> tuple[Sleep, ...]:
        self._skip("serial: the backend does not see the chip's deep sleeps")

    def power_off(self) -> None:
        self._skip("serial: the backend cannot switch the board's power")

    def reset(self) -> None:
        """Pulses EN through the adapter's RTS, as power_on does."""
        self._on()
        if self._closed:
            raise BoardOff("serial: the board is closed: the port is pytest-embedded's again")
        self.dut.serial.hard_reset()
        self.log.test(self.now.ns, "board.reset(): hard reset")

    def _on(self) -> None:
        if not self.powered:
            raise BoardOff("the board is off; call board.power_on() first")

    @property
    def now(self) -> Duration:
        self._on()
        assert self._start is not None
        return Duration(self._ended_at if self._closed else time.monotonic_ns() - self._start)

    @property
    def utc(self) -> datetime:
        return self._utc + timedelta(microseconds=self.now.ns // 1000)

    def power_on(self, utc: datetime | None = None) -> None:
        if self.powered or self._closed:
            raise BoardOff("the board is on already: power it on once")
        if utc is not None:
            raise ValueError("serial: power_on cannot set the real board's UTC; omit utc")
        serial = self.dut.serial
        serial.stop_redirect_thread()
        try:
            serial.proc.reset_input_buffer()
            self._utc = datetime.now(UTC)
            self._start = time.monotonic_ns()
            serial.hard_reset()
            self._reader = threading.Thread(target=self._read, daemon=True)
            self._reader.start()
            self.log.test(0, "board.power_on(): hard reset")
        except BaseException:
            self._start = None
            self._reader = None
            serial.start_redirect_thread()
            raise

    def _read(self) -> None:
        try:
            while not self._stop.is_set():
                data = self.dut.serial.proc.read_all()
                if self._stop.is_set():
                    return
                if data:
                    self._received.put((self.now.ns, data))
                    self._msg_queue.put(data)  # type: ignore[no-untyped-call]
                self._stop.wait(POLL_SECONDS)
        except Exception as error:
            if not self._stop.is_set():
                self._received.put(error)

    def _drain(self) -> None:
        if self._closed:
            return
        while True:
            try:
                event = self._received.get_nowait()
            except queue.Empty:
                return
            if isinstance(event, Exception):
                self._error = f"serial: console connection lost: {event}"
            else:
                at, data = event
                self._output.extend(data)
                self._lines.feed(at, data)
                self.log.console(at, data)
                if self._console.tapped is not None:
                    self._console.tapped.record(at, "out", data)

    @property
    def console_output(self) -> bytearray:
        self._drain()
        return self._output

    @property
    def console_lines(self) -> ConsoleLines:
        self._drain()
        return self._lines

    def wait(self, duration: Duration) -> None:
        self._on()
        ns = nanoseconds(duration, "board.wait")
        deadline = self.now.ns + ns
        self.log.test(self.now.ns, f"board.wait({seconds(ns)})")
        while True:
            self._drain()
            if self._error or self._closed:
                self.log.test(self.now.ns, "the run ended while the test waited")
                raise RunEnded(self._error or "serial: the run is closed")
            remaining = deadline - self.now.ns
            if remaining <= 0:
                return
            time.sleep(min(POLL_SECONDS, remaining / 1e9))

    def wait_until(self, condition: Callable[[], T | None], *, within: Duration) -> T:
        name = getattr(condition, "__qualname__", None)
        description = f"the condition {name}" if name else repr(condition)
        return self._waited(condition, within, description, "board.wait_until: within")

    def wait_for_wake(self, *, within: Duration) -> Boot:
        self._skip("serial: the backend does not see the chip's resets")

    def wait_on_clock(
        self, condition: Callable[[], T | None], within: Duration, description: str
    ) -> T:
        return self._waited(condition, within, description, "within")

    def _waited(
        self, condition: Callable[[], T | None], within: Duration, description: str, what: str
    ) -> T:
        self._on()
        ns = nanoseconds(within, what)
        start = self.now.ns
        deadline = start + ns
        self.log.test(start, f"waits up to {seconds(ns)} for {description}")
        while True:
            self._drain()
            value = condition()
            now = self.now.ns
            if value:
                self.log.test(now, f"{description}: happened")
                return value
            if self._error or self._closed:
                self.log.test(now, f"{description}: the run ended first")
                raise RunEnded(self._error or "serial: the run is closed")
            remaining = deadline - now
            if remaining <= 0:
                why = waiting(condition)
                because = f": {why}" if why is not None else ""
                self.log.test(now, f"{description}: did not happen within {seconds(ns)}{because}")
                raise NeverHappened(
                    f"{description} did not happen within {seconds(ns)}: "
                    + (f"{why}; " if why is not None else "")
                    + f"waited from {seconds(start)} to {seconds(now)} of wall time",
                    why,
                )
            time.sleep(min(POLL_SECONDS, remaining / 1e9))

    def _asked(self, request: Request) -> None:
        if self.powered:
            what = "tap a route" if request.tap else "give an interface its source"
            raise SourceError(
                f"{request.interface}: the board is on already: {what} before board.power_on()"
            )
        declared = capabilities.declared()
        try:
            capabilities.check([request], self.profile, declared)
        except CapabilityError as error:
            raise SourceError(str(error)) from None
        reason = capabilities.refusal(self.name, [request], declared)
        if reason is not None:
            self._skip(f"{self.name}: {reason}")
        # The `real` this backend declares is the device wired to the board, which no host port
        # reaches, and a tap on it would need a hardware sniffer (finding 11).
        if request.source == "real" and capabilities.kind(request.interface) == "uart":
            self._skip(
                f"{self.name}: {request.interface} has the device wired to the board at its far"
                " end: a host serial port goes there on the emulator only"
            )

    def uart(self, role: str) -> Uart:
        interface = f"uart.{role}"
        wire = route_wire(self.profile, interface)
        if interface not in self._uarts:
            if role == "gnss":
                self._uarts[interface] = Gnss(
                    interface,
                    wire,
                    clock=self.clock,
                    directory=self.directory,
                    now=lambda: self.now.ns if self.powered else 0,
                    world=lambda: self._utc,
                    asked=self._asked,
                    files=self._files,
                )
            elif role == "modem":

                def now() -> int:
                    return self.now.ns if self.powered else 0

                self._uarts[interface] = Modem(
                    interface,
                    wire,
                    clock=self.clock,
                    directory=self.directory,
                    now=now,
                    lines=Lines(self.profile.name, self.profile.lines, now, log=self.log),
                    pins=self.profile.lines.get("modem", {}),
                    asked=self._asked,
                    log=self.log,
                    files=self._files,
                )
            else:
                self._uarts[interface] = Uart(
                    interface,
                    wire,
                    clock=self.clock,
                    directory=self.directory,
                    asked=self._asked,
                    files=self._files,
                )
        return self._uarts[interface]

    def _get_lines(self) -> Lines:
        if not self.profile.lines:
            raise BackendError(f"the board profile {self.profile.name} has no lines")
        self._skip("serial: GPIO lines cannot be controlled or observed on this backend")

    def _get_sd_card(self) -> SdCard:
        if self._card is None:
            raise BackendError(f"the board profile {self.profile.name} has no sd_card")
        return self._card

    def _get_wifi(self) -> Wifi:
        """The real board's station: whatever access points are around it. Giving one skips."""
        if self._wifi is None:
            self._wifi = Wifi(now=lambda: self.now.ns if self.powered else 0, asked=self._asked)
        return self._wifi

    def _get_bluetooth(self) -> Bluetooth:
        """The real board's controller is on a real air, which the harness puts no device on: the
        capability declaration gives it no source, so the test skips."""
        self._asked(Request("bluetooth", source="model"))
        raise BackendError("serial: the chip's Bluetooth has no interface on this backend")

    def write_console(self, data: bytes) -> None:
        self._on()
        if self._closed:
            raise BackendError("serial: the run has ended")
        at = self.now.ns
        try:
            remaining = data
            while remaining:
                written = self.dut.serial.proc.write(remaining)
                if not written:
                    raise BackendError("serial: console write made no progress")
                self._console._crossed(at, "in", remaining[:written])
                remaining = remaining[written:]
        except Exception as error:
            raise BackendError(f"serial: console write failed: {error}") from error

    def tap_console(self, path: str | Path) -> Tap:
        return self._console.tap(path)

    def nvs(self, namespaces: Namespaces, partition: str = "nvs", *, merge: bool = False) -> None:
        self._skip("serial: NVS contents cannot be configured on this backend")

    def flash(self) -> FlashSnapshot:
        self._skip("serial: flash contents cannot be read on this backend")

    def set_up(self) -> list[str]:
        return ["the tap of console"] if self._console.tapped is not None else []

    def close(self) -> None:
        try:
            self._close()
        finally:
            if self._card is not None:
                self._card.close()

    def _close(self) -> None:
        if self._closed:
            return
        self._stop.set()
        if self._reader is not None:
            self._reader.join(timeout=READER_STOP_SECONDS)
        self._drain()
        if self._reader is not None and self._reader.is_alive():
            self._error = f"serial: console reader did not stop within {READER_STOP_SECONDS:g} s"
        self._ended_at = self.now.ns if self.powered else 0
        self._closed = True
        # pytest-embedded owns the port and closes it after the board fixture.
        if self._reader is not None and self._error is None:
            self.dut.serial.start_redirect_thread()
        tap = self._console.tapped
        if tap is not None and self.powered:
            try:
                recording.write(
                    tap.path,
                    tap.recording(
                        board=self.profile.name,
                        source="real",
                        clock=self.clock,
                        app_sha256=self.app_sha256,
                    ),
                )
            except (OSError, recording.RecordingError) as error:
                raise BackendError(f"the tap of console cannot be written: {error}") from error
        if self._error:
            raise BackendError(self._error.removeprefix("serial: "))

    def report(self, run: bool = False) -> str:
        lines = [
            build_line(self.artifact.build, self.app_sha256),
            f"serial: wall clock; {len(self._output)} console bytes",
        ]
        if self._console.tapped is not None:
            lines.append(f"console: tapped into {self._console.tapped.path}")
        if self._error:
            lines.append(self._error)
        if run:
            lines += self.log.lines()
        return "\n".join(lines)

    def properties(self) -> dict[str, object]:
        tap = self._console.tapped
        return {
            "build": build_properties(self.artifact.build, self.app_sha256),
            "clock": self.clock,
            "console_bytes": len(self._output),
            "error": self._error,
            "tap": str(tap.path) if tap else None,
        }
