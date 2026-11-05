"""A real ESP32 through pytest-embedded's esp,idf services, on the wall clock.

IdfSerial owns connecting and flashing. Between power_on and close we own its reader, stamping
batches after each read poll and forwarding them to pytest-embedded's log. Only the test's
thread drains those bytes into the console, so matching never races a resize of its bytearray.
"""

import queue
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NoReturn, TypeVar

from pytest_embedded.log import MessageQueue
from pytest_embedded_idf.dut import IdfDut

from esp32tl import recording
from esp32tl.backends.clock import nanoseconds, seconds
from esp32tl.backends.esp32sim import BackendError, load_app, refuse, route_wire
from esp32tl.boards import BoardProfile, capabilities
from esp32tl.boards.capabilities import CapabilityError, Request
from esp32tl.flash import FlashSnapshot, Namespaces
from esp32tl.interfaces import BoardOff, NeverHappened, RunEnded, SourceError
from esp32tl.interfaces.board import Board, Boot
from esp32tl.interfaces.console import ConsoleLines
from esp32tl.interfaces.gnss import Gnss
from esp32tl.interfaces.lines import Lines
from esp32tl.interfaces.modem import Modem
from esp32tl.interfaces.sd_card import SdCard
from esp32tl.interfaces.uart import Uart
from esp32tl.interfaces.wifi import Wifi
from esp32tl.link.boardlink import Wire
from esp32tl.report import RunLog
from esp32tl.tap import Tap, app_image_sha256
from esp32tl.units import Duration

T = TypeVar("T")
POLL_SECONDS = 0.01
# One bounded join; a stuck daemon must not hold teardown for the whole test timeout.
READER_STOP_SECONDS = 1.0


def check_artifact(
    app_path: str, reference: Path | None = None, *, build_dir: str | Path | None = None
) -> str:
    """Validate the local build, optionally against a recording, before opening a port.

    This compares app.bin, not the ELF hash stored in the ESP-IDF app descriptor. It does not
    establish what is on a board when autoflash is skipped.
    """
    app = load_app(app_path, build_dir=build_dir, backend=Serial.name)
    try:
        digest = app_image_sha256(app)
        if digest is None:
            raise BackendError(f"{app.binary_path}/flasher_args.json names no app image")
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
    return digest


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
        app_sha256: str,
        directory: Path,
        reference: Path | None = None,
        skip: Callable[[str], NoReturn] = refuse,
    ) -> None:
        self.dut = dut
        self.profile = profile
        self.directory = directory
        self.app_sha256 = app_sha256
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
        self._console = Uart(
            "console",
            Wire(1, 3, dut.serial.baud),
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
    def powered(self) -> bool:
        return self._start is not None

    @property
    def started(self) -> bool:
        return self._start is not None

    @property
    def boots(self) -> tuple[Boot, ...]:
        self._skip("serial: the backend does not see the chip's resets")

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
                self.log.test(now, f"{description}: did not happen within {seconds(ns)}")
                raise NeverHappened(
                    f"{description} did not happen within {seconds(ns)}: waited from"
                    f" {seconds(start)} to {seconds(now)} of wall time"
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

    def tap_console(self, path: str | Path) -> Tap:
        return self._console.tap(path)

    def nvs(self, namespaces: Namespaces, partition: str = "nvs") -> None:
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
        lines = [f"serial: wall clock; {len(self._output)} console bytes"]
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
            "clock": self.clock,
            "app_sha256": self.app_sha256,
            "console_bytes": len(self._output),
            "error": self._error,
            "tap": str(tap.path) if tap else None,
        }
