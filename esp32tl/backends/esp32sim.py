"""The `esp32sim` backend: the image runs in the runner crate, on the emulated clock (plan §3).

pytest-embedded runs no service for it, so its `dut` is the base `Dut`. The runner starts at
`power_on()`, not at setup, so that the test body can still set up what is around the board. It is
spawned as a `DuplicateStdoutPopen` on the dut's `msg_queue`, whose listener copies the runner's
output into `dut.log`; the runner's own lines (on stderr, starting `esp32tl-runner:`) go into the
same log.

The run is on the test's clock (`esp32tl.backends.clock`): the link holds every turn until the
test waits, in `wait`, `wait_until` or a console expect, so emulated time moves only then. The
console reaches the test as the link's `console` messages (`--console both`), in emulated-time
order with the turns. The runner waits in a held turn for as long as the test body runs, so it has
no `--peer-timeout`: the test's own timeout, in wall time, catches a run that hangs.

The backend owns the link's socket (`docs/protocol.md` §1): it binds a Unix socket before it
spawns the runner, passes it as `--board socket:PATH`, and serves the one connection with a
`BoardLink` in a thread of its own. The link carries a `Uart` for every UART route of the board
profile, `uart.gnss` a `Gnss`, which says nothing until the test gives it a source, as
`gnss.replay(path)` or `gnss.fix(at=...)` does before power-on, and the profile's GPIO lines as one
`Lines`, which puts the board's pulls on them from power-on and what the test drives at the instant
it does, and the chip's Wi-Fi station as one `Wifi`, the simulated network behind the access point
that `wifi.access_point(...)` puts on the runner's air, and the chip's Bluetooth as one `Bluetooth`,
whose controller is on the air of the test's devices once the test asks for it with `bt`: the runner
then has `--bt` and the application's ELF. Until it does, the runner has `--no-bt` and the ELF
instead, so that a firmware which starts its controller ends the run with a reason that names
`board.bluetooth`, rather than crash or hang in a controller esp32sim does not model. The backend
also owns the end of the run (§9), before the dut is torn down: `stop` at the turn the test left the
run at, `SIGTERM` for a runner that is not there, the link read to end of file, the runner waited
for, `SIGKILL` only as a last resort. Then it writes the recordings of the routes the test tapped.
The runner also gets the read end of a pipe whose write end only this process holds (`--parent-fd`):
a pytest that dies without its teardown, `SIGKILL` among the ways, closes it, and the runner ends
the run at once. Its log forwarder, `LogForwarder`, watches its parent's pid for the same end.

The runner starts once, at the first `power_on()`, and runs to teardown. `reset()` pulses the
chip's EN pin and `power_off()` switches the board's supply off, until the next `power_on()`; they
go to the runner on the link, at the board's time the test called them, and the chip comes back
through a power-on reset. Its flash keeps what the firmware wrote, in the runner, and the build's
files are never written. The board's clock runs on from the first power-on whatever the board does,
also while it is off; `boots` are the chip's boots, from the link's `reset` messages.

`flash()` reads the runner's flash through a file, `--flash-out`, beside the link's socket: in a
held turn the runner writes it on the link's `flash_dump` and the board's time does not move, and
after the run the file is what the runner wrote as the run ended (protocol 5.21). `nvs()` after a
`power_off()` has the runner write an NVS image into its flash in a held turn, with the supply off
(`flash_write`, protocol 5.25), for the next boot to read.

What the test does on the board, what the firmware prints and the bytes on every route go into a
`RunLog` (`esp32tl.report`), which the report of a test that failed shows after the link's.

A route with a real source, `gnss.real(port)`, puts the whole run on the wall clock (protocol
§12.4): the runner starts with `--realtime`, its `hello` says `wall`, and every recording of the run
says so too. So does a bridge from a simulated network to a real service, `network.bridge(port,
to)`. The run stays on the test's clock.
"""

import contextlib
import multiprocessing.connection
import multiprocessing.managers
import os
import shutil
import signal
import socket
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from multiprocessing import get_context
from multiprocessing.context import SpawnProcess
from multiprocessing.synchronize import Event as ProcessEvent
from pathlib import Path
from types import MappingProxyType
from typing import NoReturn, TypeVar

from pytest_embedded.dut import Dut
from pytest_embedded.log import DuplicateStdoutPopen, MessageQueue
from pytest_embedded.utils import Meta, remove_asci_color_code

from esp32tl import boards, build, flash, recording, runtime
from esp32tl.backends.clock import HeldClock, nanoseconds, seconds
from esp32tl.boards import BoardProfile, capabilities
from esp32tl.boards.capabilities import CapabilityError, Request
from esp32tl.build import Build, BuildError
from esp32tl.flash import FlashSnapshot, Namespaces, Partition
from esp32tl.interfaces import BoardOff, NeverHappened, RunEnded, SourceError
from esp32tl.interfaces.bluetooth import TAP_SOURCE, Bluetooth
from esp32tl.interfaces.board import (
    DEEPSLEEP_RESET,
    POWERON_RESET,
    Board,
    Boot,
    Sleep,
    UptimeZero,
    idf_reset_reason,
)
from esp32tl.interfaces.console import ConsoleInput, ConsoleLines
from esp32tl.interfaces.gnss import Gnss
from esp32tl.interfaces.history import History
from esp32tl.interfaces.lines import Lines
from esp32tl.interfaces.modem import Modem
from esp32tl.interfaces.sd_card import SdCard
from esp32tl.interfaces.uart import Uart
from esp32tl.interfaces.wifi import Wifi
from esp32tl.link.boardlink import BoardLink, Wire
from esp32tl.link.protocol import ChipReset, Power, Reset
from esp32tl.net import Internet
from esp32tl.net.handlers import refuse_board, refuse_wait
from esp32tl.net.ntp import DEFAULT_UTC
from esp32tl.report import RunLog, board_time, build_line, build_properties, shown
from esp32tl.sources.model.modem import Modem as ModemModel
from esp32tl.sources.real import Real
from esp32tl.sources.replay import Replay
from esp32tl.tap import Tap, app_image_sha256
from esp32tl.units import Duration

T = TypeVar("T")

# The checkout this package runs from, with an editable install (`make setup` makes one): the
# runner and the ROM are looked for there. From a wheel this is site-packages, and the messages
# below point to ESP32TL_RUNNER and ESP32TL_ROM; prebuilt runners come with milestone 10.
ROOT = Path(__file__).resolve().parents[2]

# The ROM ELF scripts/fetch-rom-elf puts in .cache/ (plan M0). The runner's own lookup prefers the
# newest release under ~/.espressif instead, so the backend always passes --rom.
ROM_RELEASE = runtime.ROM_RELEASE

# How long a run of the esp32tl command lasts by default, in emulated seconds: its clock runs
# free. hello_world runs 12 emulated seconds in 0.3 s of wall time on an M2 Pro, so 300 is a few
# seconds there (an image that never idles runs slower).
MAX_SECONDS = 300

# The --max-seconds of a run on the test's clock, a day of emulated time: the flag is required,
# and a run lasts as long as the test waits. A test that reaches it gets RunEnded.
HELD_MAX_SECONDS = 86400

# The world's time at power-on, unless the test gives another: runs repeat whatever the host's
# clock says.
POWER_ON_UTC = DEFAULT_UTC

# How soon after the chip's reset `wait_for_wake` returns, 1 ms of the board's time: the longest
# slice the runner runs while the board is on anyway, so asking for it adds no slice (protocol
# 6.1), and a sleep without events takes no turn at all.
WAKE_LATENCY_NS = 1_000_000

# How long the forwarder of the runner's log sleeps, in wall seconds, when it finds nothing new in
# the file. pytest-embedded's own forwarder reads it again at once, and every empty read is a round
# trip to the process that holds `msg_queue`: the two of them took more CPU than the runner.
LOG_POLL_SECONDS = 0.02

# How long, in wall seconds, the runner has after SIGTERM to send its stats, close the link and
# exit, before SIGKILL (protocol §9). It needs at most one emulated millisecond.
EXIT_SECONDS = 5

# The longest Unix socket path on macOS, NUL included; Linux takes 108.
SUN_PATH_MAX = 104


# What esp32sim does not model on a chip, by what a test asks for: an interface's kind, or
# `<kind>.<role>` for a role of it. The test is skipped with the reason; each gap esp32sim could
# close is an entry of https://github.com/mateuszsikora/esp32-testing-library/issues/229, and the
# reason names it. Every chip of `boards.CHIPS`
# but the classic ESP32 is named here, with its gaps or with none: a chip added there needs its
# own decision here (tests/test_pytest_plugin.py checks it).
CHIP_GAPS: dict[str, dict[str, str]] = {
    chip: {
        "sd_card": f"esp32sim does not model GP-SPI RX DMA on the {boards.CHIPS[chip].title},"
        " which ESP-IDF's SD SPI driver needs (#229, G2)",
        "wifi.station": "esp32sim has no virtual station for a SoftAP on the"
        f" {boards.CHIPS[chip].title} (#229, G4)",
    }
    for chip in ("esp32s3", "esp32c3", "esp32c6")
} | {
    "esp32p4": {
        "uart": "esp32sim's ESP32-P4 tells the board nothing its UARTs send or receive on their"
        " pins (#229, G6)",
        "sd_card": "esp32sim's ESP32-P4 tells the board no SPI pins (#229, G6)",
        "wifi": "the ESP32-P4 has no Wi-Fi of its own, and esp32sim's co-processor for it is"
        " not in the runner",
        "bluetooth": "the ESP32-P4 has no Bluetooth",
    },
    "esp32s31": {
        "uart": "esp32sim's ESP32-S31 tells the board nothing its UARTs send or receive on their"
        " pins (#229, G7)",
        "sd_card": "esp32sim's ESP32-S31 tells the board no SPI pins (#229, G7)",
        "wifi": "esp32sim's ESP32-S31 has no Ethernet relay for the test's network (#229, G7)",
        "bluetooth": "esp32sim models no Bluetooth on the ESP32-S31",
    },
}


# The lines of the runner's log the report ends with, from where each starts: esp32sim's Wi-Fi
# diagnostics, and the runner's when the Bluetooth hooks follow an app the bootloader started from
# another ELF than the build's, or find none for it, or keep the build's for a first app built from
# another file than its ELF (`hci::Images`, `bluetooth.add_elf`).
RADIO_DIAGNOSTICS = (
    "[emu] wifi",
    ": the app the bootloader started first was built from an ELF",
    "esp32tl-runner: Bluetooth controller hooks from ",
    "esp32tl-runner: the app the bootloader started was built from an ELF",
)

# The runner's `stats` `end` when the firmware called `esp_bt_controller_init` under `--no-bt`
# (protocol 5.9 and §9).
NO_BT_END = "bt_controller"


def no_bluetooth(at: int) -> str:
    """Why a run under `--no-bt` ended at `at`, the end of the slice the firmware started its
    Bluetooth controller in: the test gave the board none, and what to do about it."""
    return (
        f"the firmware started its Bluetooth controller (esp_bt_controller_init) by {seconds(at)},"
        " and the test gave the board no Bluetooth: look at board.bluetooth (or take the bt"
        " fixture) before board.power_on(); board.bluetooth.use() gives the firmware the"
        " harness's controller with no device on the air"
    )


# The runner's `stats` `end` when esp32sim could not start the firmware's Bluetooth controller under
# `--bt`, with esp32sim's line as `detail` (protocol 5.9 and §9).
BT_FAILED_END = "bt_controller_failed"


def bluetooth_failed(at: int, line: str) -> str:
    """Why a run under `--bt` ended at `at`, the end of the slice in which esp32sim noted `line`:
    it could not start the firmware's Bluetooth controller, so the firmware would have gone on
    without one."""
    return (
        f"esp32sim could not start the firmware's Bluetooth controller by {seconds(at)}: {line};"
        " the failure is esp32sim's, which stands in for the chip's controller, so the run ended"
        " rather than let the firmware go on without one"
    )


class BackendError(Exception):
    """What keeps the backend from starting, said so that the reader knows what to do."""


def runtime_path(kind: str, chip: str = "esp32") -> Path:
    """The runner, or `chip`'s ROM ELF: ESP32TL_RUNNER, or ESP32TL_ROM for the classic ESP32 and
    ESP32TL_ROM_<CHIP> (ESP32TL_ROM_ESP32S3) for another chip, else the checkout's, else the
    prepared runtime's."""
    variable = "ESP32TL_RUNNER" if kind == "runner" else runtime.rom_variable(chip)
    explicit = os.environ.get(variable)
    if kind != "runner" and chip not in runtime.ROMS:
        raise BackendError(
            f"esp32tl has no ROM for {chip}; the chips are {', '.join(runtime.ROMS)}"
        )
    local = ROOT / (
        "runner/target/release/esp32tl-runner"
        if kind == "runner"
        else f".cache/esp-rom-elfs/{ROM_RELEASE}/{runtime.ROMS[chip][0]}"
    )
    try:
        if explicit:
            path = Path(explicit).resolve()
        elif (ROOT / "runner/Cargo.toml").is_file():
            path = local
        else:
            return runtime.cached_file(kind, chip=chip)
        if not path.is_file():
            # The checkout's own file is built there; prepare installs nothing for a checkout.
            remedy = (
                f"run make {'build-runner' if kind == 'runner' else 'rom-elf'} in {ROOT}"
                if path == local
                else "run make setup in a checkout, or esp32tl runtime prepare"
            )
            raise runtime.RuntimeError(f"no {kind} at {path}: {remedy}")
        if kind == "runner":
            runtime.identity(path)
        else:
            runtime.verify(path, runtime.ROMS[chip][1])
        return path
    except runtime.RuntimeError as error:
        raise BackendError(str(error)) from error


def runner_path() -> Path:
    return runtime_path("runner")


def rom_path(chip: str = "esp32", app: Build | None = None) -> Path:
    """The ROM ELF the runner boots `app` with on `chip`: the chip's own, or on the ESP32-P4 the
    one of the revision the build is for (`build.p4_rev3`), as the runner picks the revision."""
    return runtime_path("rom", rom_key(chip, app))


def rom_key(chip: str, app: Build | None = None) -> str:
    """The key of runtime.ROMS for `app` on `chip`: `runtime.P4_REV0` for an ESP32-P4 build for
    rev < 3, the chip's name otherwise."""
    if chip == "esp32p4" and app is not None:
        try:
            if not build.p4_rev3(app):
                return runtime.P4_REV0
        except BuildError as error:
            raise BackendError(str(error)) from error
    return chip


def load_app(
    app_path: str,
    *,
    build_dir: str | Path | None = None,
    backend: str = "esp32sim",
    chip: str = "esp32",
) -> Build:
    """The finished, unencrypted build for `chip`, the board's, selected relative to `app_path`,
    an ESP-IDF or a PlatformIO build directory (`esp32tl.build`); `backend` names the backend in
    the errors."""
    directory = (Path(app_path) / (build_dir if build_dir is not None else "build")).resolve()
    try:
        return build.load(app_path, directory, backend=backend, chip=chip)
    except BuildError as error:
        raise BackendError(str(error)) from error


# The flash sizes the runner takes, in MB (`--flash-mb`).
RUNNER_FLASH_MB = (1, 2, 4, 8, 16)


def runner_flash_mb(profile: BoardProfile, app: Build) -> int | None:
    """The flash the runner gives the chip, in MB: `profile.flash_mb`, else the size the build's
    image headers give (`build.header_flash`), else `None`, the runner's default. Raises
    `BackendError` for a profile size the runner does not take, for a header that gives more than
    the runner's largest flash, whatever the profile, and for a header that gives more flash than
    the profile: its application would stop at start-up in spi_flash, and the chip reboot until
    the test's timeout. A header that gives less flash than the chip has is what a real board
    boots too."""
    try:
        header = build.header_flash(app)
    except BuildError as error:
        raise BackendError(str(error)) from error
    if profile.flash_mb is not None and profile.flash_mb not in RUNNER_FLASH_MB:
        sizes = ", ".join(map(str, RUNNER_FLASH_MB[:-1])) + f" or {RUNNER_FLASH_MB[-1]}"
        raise BackendError(
            f"board profile {profile.name!r} has flash_mb = {profile.flash_mb}; the runner's"
            f" flash is {sizes} MB: set one of these in the profile"
        )
    if header is None:
        return profile.flash_mb
    gives = f"{header.path}: the image header of the {header.what} gives {header.mb} MB of flash"
    # Before the profile's size: no flash_mb would let this build run.
    if header.mb > RUNNER_FLASH_MB[-1]:
        raise BackendError(
            f"{gives}, more than the runner's largest, {RUNNER_FLASH_MB[-1]} MB: build for"
            f" {RUNNER_FLASH_MB[-1]} MB of flash or less"
        )
    if profile.flash_mb is None:
        return header.mb
    if header.mb > profile.flash_mb:
        raise BackendError(
            f"{gives}, more than the {profile.flash_mb} MB of board profile {profile.name!r}"
            f" (flash_mb = {profile.flash_mb}): the application would stop at start-up; set"
            f" flash_mb = {header.mb} in the profile, or build for {profile.flash_mb} MB of flash"
        )
    return profile.flash_mb


def runner_command(
    runner: Path,
    rom: Path,
    app: Build,
    max_seconds: float,
    link: Path,
    *,
    held: bool = False,
    flash_mb: int | None = None,
    psram_mb: int | None = None,
    sd_card: SdCard | None = None,
    extra_flash: tuple[tuple[int, Path], ...] = (),
    wifi: Wifi | None = None,
    bluetooth: Bluetooth | None = None,
    realtime: bool = False,
    flash_out: Path | None = None,
    console: str = boards.UART0,
    parent_fd: int | None = None,
) -> list[str]:
    """The runner's command line: every flash file of the build at its offset, its board the socket
    at `link`. A `held` run is on the test's clock: its console also goes on the link, and the
    runner waits for the harness's turn for as long as it takes. `flash_mb`, from `runner_flash_mb`,
    is the flash the runner gives the chip; `None` leaves the runner's default. `psram_mb`, the
    board profile's, is the PSRAM it gives the chip; `None`, a profile that does not say, leaves the
    runner's default. An `sd_card` puts the runner's card slot on its pins, and a card holding its
    image in it when the test has inserted one. `extra_flash` overrides the build's flash contents
    at the supplied offsets, in order. A `wifi` with an access point puts it, and the networks
    around, on the runner's air, with the network behind it on the link. A `bluetooth` the test uses
    makes the runner forward the guest's HCI on the link (`--bt`), with the symbols of the
    application's ELF, `app.elf`, then of each other ELF of `bluetooth.add_elf`; one it does not
    use gives the runner the ELF as `--no-bt`, on a chip esp32sim models Bluetooth on, so that
    the firmware's call to `esp_bt_controller_init` ends the run. A `realtime` run is paced to
    wall time, for a real device at the far end of a route. `flash_out` is the file the runner
    writes the whole flash to when the link asks, and at the end of the run. `console` is the
    board profile's: `usb-serial-jtag` makes the chip's USB-Serial/JTAG controller the console, on
    stdout and on the link, in place of UART0 (`--console-port usb`). `parent_fd` is the
    descriptor, inherited by the runner, of the read end of a pipe whose write end the backend
    holds: the runner ends the run once it reads end of file on it, when the backend's process
    has ended (`--parent-fd`, protocol §9)."""
    command = [
        *(str(runner), "--chip", app.chip, "--rom", str(rom)),
        *("--max-seconds", str(max_seconds)),
    ]
    if held:
        command += ["--console", "both", "--peer-timeout", "0"]
    if console == boards.USB_SERIAL_JTAG:
        command += ["--console-port", "usb"]
    command += ["--board", f"socket:{link}"]
    for file in app.flash_files:
        command += ["--flash-at", f"{file.offset:#x}={file.path}"]
    for offset, image in extra_flash:
        command += ["--flash-at", f"{offset:#x}={image}"]
    if flash_mb is not None:
        command += ["--flash-mb", str(flash_mb)]
    if psram_mb is not None:
        command += ["--psram-mb", str(psram_mb)]
    if sd_card is not None:
        r = sd_card.route
        command += ["--sd-spi", f"sclk={r.sclk},mosi={r.mosi},miso={r.miso},cs={r.cs}"]
        if sd_card.image_path is not None:
            command += ["--sd-rw", str(sd_card.image_path)]
    if wifi is not None:
        command += wifi.runner_args()
    if bluetooth is not None and bluetooth.used:
        command += ["--bt", str(app.elf)]
        own = app.elf.resolve() if app.elf is not None else None
        for elf in bluetooth.elfs:
            if elf != own:
                command += ["--bt", str(elf)]
    elif (
        bluetooth is not None
        and app.elf is not None
        and app.elf.is_file()
        and "bluetooth" not in CHIP_GAPS.get(app.chip, {})
    ):
        command += ["--no-bt", str(app.elf)]
    if realtime:
        command.append("--realtime")
    if flash_out is not None:
        command += ["--flash-out", str(flash_out)]
    if parent_fd is not None:
        command += ["--parent-fd", str(parent_fd)]
    return command


def profile_uart(
    profile: BoardProfile,
    interface: str,
    *,
    clock: str,
    directory: Path,
    asked: Callable[[Request], None] = lambda request: None,
    log: RunLog | None = None,
    files: dict[Path, tuple[str, str]] | None = None,
) -> Uart:
    """The `Uart` on the route `profile` gives `interface` (`uart.gnss`), at the route's pins and
    baud; the other arguments are `Uart`'s."""
    return Uart(
        interface,
        route_wire(profile, interface),
        clock=clock,
        directory=directory,
        asked=asked,
        log=log,
        files=files,
    )


def route_wire(profile: BoardProfile, interface: str) -> Wire:
    """The pins and baud of the route `profile` gives `interface` (`uart.gnss`)."""
    kind, _, role = interface.partition(".")
    route = profile.uart.get(role) if kind == "uart" else None
    if route is None:
        raise BackendError(
            f"the board profile {profile.name} has no {interface};"
            f" its interfaces are {', '.join(profile.interfaces)}"
        )
    return Wire(tx=route.tx, rx=route.rx, baud=route.baud)


def _forward_log(
    msg_queue: MessageQueue,
    logfile: str,
    ready: ProcessEvent | None = None,
    parent: int | None = None,
) -> None:
    """The body of `LogForwarder`: puts what the runner adds to `logfile` on `msg_queue`, and
    sleeps `LOG_POLL_SECONDS` whenever there is nothing new, never putting an empty read. After
    `SIGTERM`, within `LOG_POLL_SECONDS`, it reads the file once more, puts what it finds, and
    returns, even if something still writes to the file. The handler only sets a flag: one that
    took a lock could find it held by the interrupted code and wait for ever. The file is read in
    binary, as pytest-embedded's forwarder reads it: a UTF-8 sequence may straddle the end of what
    has been written so far. `ready` is set once the handler is in place: a `SIGTERM` before that
    would end the process without the last read.

    `parent` is the pid of the process that started it, pytest's. A read that finds nothing new
    also compares it with `os.getppid()`: once they differ the parent has died without its
    teardown, and nobody will send `SIGTERM`. The forwarder then reads the file once more, puts what
    it finds, ends the `MessageQueueManager` that holds `msg_queue` (`end_manager`) and returns."""
    stopping = False
    orphaned = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    if ready is not None:
        ready.set()

    def put(data: bytes) -> bool:
        try:
            msg_queue.put(data)  # type: ignore[no-untyped-call]
        except Exception:  # the queue is closed
            return False
        return True

    with open(logfile, "rb") as log:
        while not stopping:
            data = log.read()
            if data:
                if not put(data):
                    return
            elif parent is not None and os.getppid() != parent:
                orphaned = True
                break
            else:
                time.sleep(LOG_POLL_SECONDS)
        data = log.read()
        if data:
            put(data)
    if orphaned:
        end_manager(msg_queue)


def end_manager(msg_queue: MessageQueue) -> None:
    """Asks the server process of the `MessageQueueManager` that `msg_queue` is a proxy of to shut
    down, as `BaseManager.shutdown` does in the process that started it. pytest-embedded starts one
    for the session (its `_mp_manager` fixture) and shuts it down at the end of the session; a
    pytest that died before then leaves it, and the dut's listener blocked on its queue, for good.
    The proxy's `_token` and `_authkey` are where `multiprocessing` keeps the server's address and
    key; without them, or without a server to answer, this does nothing."""
    token = getattr(msg_queue, "_token", None)
    authkey = getattr(msg_queue, "_authkey", None)
    if token is None:
        return
    with contextlib.suppress(Exception):
        connection = multiprocessing.connection.Client(token.address, authkey=authkey)
        try:
            multiprocessing.managers.dispatch(connection, None, "shutdown")  # type: ignore[attr-defined]
        finally:
            connection.close()


class LogForwarder(SpawnProcess):
    """The process that copies the runner's log file to `msg_queue`, in place of pytest-embedded's
    `_PopenRedirectProcess`, which reads the file in a loop without a pause. `ready` is set once
    its `SIGTERM` handler is in place, after it has imported `esp32tl`. It ends by itself when the
    process that made it dies (`_forward_log`)."""

    def __init__(self, msg_queue: MessageQueue, logfile: str) -> None:
        self.ready = get_context("spawn").Event()
        super().__init__(
            target=_forward_log, args=(msg_queue, logfile, self.ready, os.getpid()), daemon=True
        )


class RunnerPopen(DuplicateStdoutPopen):
    """A `DuplicateStdoutPopen` whose forwarder is `LogForwarder` and whose `close` waits for it,
    and that closes its log when the runner cannot start: the parent opens the log before it
    starts the process, and nothing else can close it then."""

    # pytest-embedded types it as its own forwarder's class; it takes any process class with that
    # constructor (`DuplicateStdoutPopen.__init__` starts it, `close` terminates it)
    REDIRECT_CLS = LogForwarder  # type: ignore[assignment]

    def __init__(
        self, msg_queue: MessageQueue, cmd: list[str], meta: Meta, pass_fds: tuple[int, ...] = ()
    ) -> None:
        try:
            super().__init__(msg_queue, cmd, meta, pass_fds=pass_fds)
        except OSError:
            log = getattr(self, "_fw", None)
            if log is not None:
                log.close()
            raise

    def close(self) -> None:
        """Waits up to `EXIT_SECONDS` for the forwarder to be ready, so that a runner that ended
        while it was still starting does not lose its last lines; sends it `SIGTERM` and closes the
        log, as `DuplicateStdoutPopen.close` does; then waits up to `EXIT_SECONDS` for it to end,
        so that the runner's last lines are on `msg_queue` before the backend's teardown goes on.
        The wait for readiness ends as soon as the forwarder is no longer alive."""
        forwarder = self._p
        if isinstance(forwarder, LogForwarder):
            # in short waits, so that a forwarder that died while starting costs no EXIT_SECONDS
            deadline = time.monotonic() + EXIT_SECONDS
            while (
                forwarder.is_alive()
                and not forwarder.ready.wait(0.05)
                and time.monotonic() < deadline
            ):
                pass
        super().close()  # type: ignore[no-untyped-call]
        if forwarder is not None:
            forwarder.join(EXIT_SECONDS)


def listen() -> tuple[socket.socket, Path]:
    """A Unix socket listening for the runner, alone in a new directory. The directory is under
    /tmp when the system's temporary directory would make its path too long for `sun_path`."""
    directory = Path(tempfile.mkdtemp(prefix="esp32tl-"))
    if len(os.fsencode(directory / "link.sock")) >= SUN_PATH_MAX:
        directory.rmdir()
        directory = Path(tempfile.mkdtemp(prefix="esp32tl-", dir="/tmp"))
    path = directory / "link.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
        listener.listen(1)
    except OSError as error:
        listener.close()
        shutil.rmtree(directory, ignore_errors=True)
        raise BackendError(f"cannot listen on {path}: {error.strerror}") from error
    return listener, path


class Esp32Sim(Board):
    """A run of the app in `app_path` in the runner, on the board profile `profile`, its console
    going to `dut`'s log and to the test, its board the link to every UART route and every GPIO
    line of the profile, and the runner's SD card slot on the profile's slot's pins, which holds
    the card the test inserts, before power-on or while the firmware runs.

    `app` is the `Build` the runner flashes. The dut's own `app` is pytest-embedded's base `App`,
    without `flash_files` or `target`: without a service, pytest-embedded builds nothing more.
    `profile`'s name and chip go into the harness's `hello`. `directory` is the test file's, which
    a relative recording path is taken from. `skip` is called with the reason when the test asks
    a route for a source this backend's capability declaration does not give, and raises; the
    `board` fixture makes it pytest's skip. `link` is the `BoardLink`, whose warnings, stats and
    error are complete once `close` returns; `log` the `RunLog` of the run.
    """

    name = "esp32sim"
    report_section = "esp32sim link"
    report_property = "esp32sim_link"

    def __init__(
        self,
        dut: Dut,
        app_path: str,
        msg_queue: MessageQueue,
        meta: Meta,
        profile: BoardProfile,
        directory: Path | None = None,
        skip: Callable[[str], NoReturn] | None = None,
        *,
        build_dir: str | Path | None = None,
    ) -> None:
        self.dut = dut
        self.profile = profile
        self.directory = directory or Path.cwd()
        self._msg_queue = msg_queue
        self._meta = meta
        self._skip = skip or refuse
        self.app = load_app(app_path, build_dir=build_dir, chip=profile.chip)
        # Hashed again as the runner starts, which reads the image then: a build directory built
        # again before power-on is reported as the image the runner flashed.
        self._app_sha256 = self._hash_app()
        # Refused here, at setup: a flash smaller than the build's header would only show as a
        # chip that reboots until the test's timeout.
        self.flash_mb = runner_flash_mb(profile, self.app)
        self._nvs: dict[str, tuple[int, Path]] = {}
        self._runner, self._rom = runner_path(), rom_path(profile.chip, self.app)
        # What the guest printed on its console, as the link brings it, and cut into lines.
        self.console_output = bytearray()
        self.console_lines = ConsoleLines()
        self.log = RunLog()
        self._radio_diagnostics: list[str] = []
        self._hold = HeldClock()
        self._utc = POWER_ON_UTC
        # The board's supply, from power_on to power_off; the runner runs from the first.
        self._supply = False
        # Every route of the profile, listening and silent until the test gives it a source.
        # One mapping of the files they and the SD card read and write: no tap writes over another
        # route's file or the card's image.
        self._files: dict[Path, tuple[str, str]] = {}
        # The console's input: UART0's RX pin at 115200 baud, or, on a board whose console is the
        # chip's USB-Serial/JTAG controller, that controller, which has no pins and no baud; on a
        # chip whose UARTs have no pins in esp32sim, UART0 without its pins.
        chip = boards.CHIPS[profile.chip]
        pinless = profile.console == boards.USB_SERIAL_JTAG or not chip.uart_pins
        self._console = ConsoleInput(
            "console",
            None if pinless else Wire(None, chip.console_pin("rx"), 115200),
            pinless=pinless,
            log=self.log,
            clock=self._run_clock,
            directory=self.directory,
            asked=self._asked,
            files=self._files,
        )

        # Before power-on the clock is 0: what drives a line then holds from the start.
        self._lines = Lines(profile.name, profile.lines, lambda: self._hold.now, log=self.log)
        self._uarts = {f"uart.{role}": self._route(f"uart.{role}") for role in profile.uart}
        self._sd_card = (
            SdCard(
                profile.sd_card,
                directory=self.directory,
                asked=self._declared,
                log=self.log,
                files=self._files,
                now=lambda: self._hold.now,
                ended=lambda: self._hold.ended,
                started=lambda: self.started,
            )
            if profile.sd_card is not None
            else None
        )
        self._wifi = Wifi(
            now=lambda: self._hold.now,
            world=lambda: self._utc,
            chip=profile.chip,
            asked=self._asked,
            log=self.log,
            bridging=self._bridging,
            test_error=self._hold.fail,
        )
        self._bluetooth = Bluetooth(
            asked=self._asked,
            log=self.log,
            directory=self.directory,
            files=self._files,
            chip=profile.chip,
        )
        slot = [] if self._sd_card is None else [self._sd_card]
        self.link = BoardLink(
            profile.name,
            [self._console, *self._uarts.values(), self._lines, self._wifi, self._bluetooth, *slot],
            chip=profile.chip,
            on_error=self._end_run,
            hold=self._hold,
            console=self._printed,
            clock=self._run_clock,
            on_reset=self._reset,
        )
        self.process: RunnerPopen | None = None
        # The write end of the runner's --parent-fd pipe, from power-on until the runner exits.
        self._parent: int | None = None
        self._exited = threading.Event()
        # One SIGTERM at most: a second one kills the runner, with 128 + 15 (protocol §9).
        self._terminated = False
        self._terminating = threading.Lock()

    def _route(self, interface: str) -> Uart:
        """The route's `Uart`: `Gnss` for uart.gnss, `Modem` for uart.modem. Models read
        the held clock; GNSS also reads the world's time, and the modem shares Lines."""
        wire = route_wire(self.profile, interface)
        if interface == "uart.gnss":
            return Gnss(
                interface,
                wire,
                clock=self._run_clock,
                directory=self.directory,
                now=lambda: self._hold.now,
                world=lambda: self._utc,
                asked=self._asked,
                log=self.log,
                files=self._files,
            )
        if interface == "uart.modem":
            return Modem(
                interface,
                wire,
                clock=self._run_clock,
                directory=self.directory,
                now=lambda: self._hold.now,
                lines=self._lines,
                world=lambda: self._utc,
                pins=self.profile.lines.get("modem", {}),
                asked=self._asked,
                log=self.log,
                files=self._files,
                bridging=self._bridging,
                test_error=self._hold.fail,
            )
        return Uart(
            interface,
            wire,
            clock=self._run_clock,
            directory=self.directory,
            asked=self._asked,
            log=self.log,
            files=self._files,
        )

    def uart(self, role: str) -> Uart:
        """The `Uart` on the link for the profile's `uart.<role>` route, a `Gnss` for `gnss`."""
        interface = f"uart.{role}"
        uart = self._uarts.get(interface)
        if uart is None:
            raise BackendError(
                f"the board profile {self.profile.name} has no {interface};"
                f" its interfaces are {', '.join(self.profile.interfaces)}"
            )
        return uart

    def _get_lines(self) -> Lines:
        """The `Lines` on the link for the profile's `[lines.<role>]` sets."""
        if not self.profile.lines:
            raise BackendError(
                f"the board profile {self.profile.name} has no lines;"
                f" its interfaces are {', '.join(self.profile.interfaces)}"
            )
        return self._lines

    def _get_sd_card(self) -> SdCard:
        """The profile's SD card slot."""
        if self._sd_card is None:
            raise BackendError(
                f"the board profile {self.profile.name} has no sd_card;"
                f" its interfaces are {', '.join(self.profile.interfaces)}"
            )
        return self._sd_card

    def _get_wifi(self) -> Wifi:
        """The chip's Wi-Fi station."""
        return self._wifi

    def _get_bluetooth(self) -> Bluetooth:
        """The chip's Bluetooth: its controller on the air, from the test's first look on, which
        must come before power-on, since the runner then needs `--bt` and the application's ELF."""
        if not self._bluetooth.used:
            # After power-on, the reason is that it is too late, whatever the build holds.
            self._asked(Request("bluetooth", source="model"))
            elf = self.app.elf
            if elf is None or not elf.is_file():
                raise BackendError(
                    f"bluetooth: {self.app.directory} holds no ELF of the application, whose"
                    " symbols the runner's --bt needs"
                )
            self._bluetooth.use()
        return self._bluetooth

    def write_console(self, data: bytes) -> None:
        clock = self._on()
        if clock.ended:
            raise RunEnded("the run has ended")
        self._console.write(clock.now, data)

    def tap_console(self, path: str | Path) -> Tap:
        return self._console.tap(path)

    def _printed(self, t: int, data: bytes) -> None:
        if self._console.tapped is not None:
            self._console.tapped.record(t, "out", data)
        self.console_output.extend(data)
        self.console_lines.feed(t, data)
        self.log.console(t, data)

    def _asked(self, request: Request) -> None:
        """Refuses a source or a tap on an interface once the board has been on, then as
        `_declared` does."""
        if self.started:
            what = "tap a route" if request.tap else "give an interface its source"
            state = "is on already" if self.powered else "has been on"
            raise SourceError(
                f"{request.interface}: the board {state}: {what} before board.power_on()"
            )
        self._declared(request)

    def _declared(self, request: Request) -> None:
        """Refuses a source or a tap that no backend gives, and skips the test when this one
        does not. The SD card slot asks only this: a card goes in and out while the run goes
        on, with the supply on or off."""
        declared = capabilities.declared()
        try:
            capabilities.check([request], self.profile, declared)
        except CapabilityError as error:
            raise SourceError(str(error)) from None
        reason = capabilities.refusal(self.name, [request], declared)
        if reason is not None:
            self._skip(f"{self.name}: {reason}")
        what = capabilities.kind(request.interface)
        gaps = CHIP_GAPS.get(self.profile.chip, {})
        gap = gaps.get(f"{what}.{request.role}") if request.role else None
        gap = gap or gaps.get(what)
        if gap is not None:
            self._skip(f"{self.name}: {request.interface} on {self.profile.chip}: {gap}")
        if request.source == "real":
            self._refuse_replays(f"{request.interface}: a real source")

    def _bridging(self, call: str) -> None:
        """Refuses a bridge to a real service once the board has been on, since the runner is on
        its clock from power-on, then a replay given before it, as a real source does."""
        if self.started:
            state = "is on already" if self.powered else "has been on"
            raise SourceError(
                f"{call}: the board {state}: a bridge puts the run on the {recording.WALL} clock,"
                " from power-on, so give it before board.power_on()"
            )
        self._refuse_replays(f"{call}: a bridge")
        self.log.test(0, call)

    def _refuse_replays(self, cause: str) -> None:
        """`cause`, a real source or a bridge, would put the whole run on the wall clock, and a
        replay given before it read a recording on the emulated clock, whose times would no longer
        mean what they say. A replay given once the run is on the wall clock read one on that
        clock."""
        if self.clock == recording.WALL:
            return
        for name, uart in self._uarts.items():
            if isinstance(uart.source, Replay):
                raise SourceError(
                    f"{cause} puts the run on the {recording.WALL} clock, and"
                    f" {name} replays a recording on the {self.clock} clock: replaying it would"
                    " compare times across clocks"
                )

    def _partition_table(self, call: str) -> dict[str, Partition]:
        """The build's partition table, by name, read where the build flashes it; `call` names the
        board's call in the errors."""
        try:
            return flash.partitions(self.app.table())
        except BuildError as error:
            raise BackendError(f"{call}: {error}") from error
        except (OSError, ValueError) as error:
            raise BackendError(
                f"{call}: cannot read the partition table of {self.app.directory}"
                f" at {self.app.partition_table:#x}: {error}"
            ) from error

    def nvs(self, namespaces: Namespaces, partition: str = "nvs", *, merge: bool = False) -> None:
        """Write the test's keys into a named NVS partition, before the first power-on or while
        the board is off after `power_off()`. Without `merge` the partition holds the test's keys
        and nothing else. With it, the test's keys go over what the partition holds now: the
        build's contents, and the test's earlier `nvs`, before the first power-on; the flash as
        the firmware left it after a power-off. Every namespace and key the test does not name
        keeps its value; the partition is written anew, so the firmware's layout of its items is
        not kept."""
        refuse_board("board.nvs")
        call = f"board.nvs(partition={partition!r}{', merge=True' if merge else ''})"
        if self.powered:
            raise BoardOff(
                "board.nvs: the board is on: call board.power_off() first, and write the flash"
                " while it is off"
            )
        if self.started:
            self._hold.check(call)
        found = self._partition_table("board.nvs")
        if partition not in found:
            raise BackendError(flash.no_partition(partition, found))
        entry = found[partition]
        if (entry.type, entry.subtype) != (1, 2) or entry.flags:
            raise BackendError(
                f"board.nvs: partition {partition!r} must be unencrypted, writable NVS"
            )
        if merge:
            current = self._partition_now(entry)
            # NVS keeps a key written with two types as two, and the image takes one value a key
            for (name, key), kinds in flash.nvs_types(current).items():
                if key not in namespaces.get(name, {}):
                    raise BackendError(
                        f"board.nvs: {name}/{key} holds a {' and a '.join(kinds)} in partition"
                        f" {partition!r}, which a merge cannot keep both of: give {name}/{key} a"
                        " value in the test's keys, or write without merge"
                    )
            merged = {name: dict(keys) for name, keys in flash.nvs_values(current).items()}
            for name, keys in namespaces.items():
                merged.setdefault(name, {}).update(keys)
            namespaces = merged
        image = flash.nvs_image(namespaces, entry.size)
        # The offset is a safe filename even when a partition's label contains path separators. A
        # write between boots gets a file of its own, by the board's time and, for a second one in
        # the same instant, a count, so that the image the runner started with stays what its
        # --flash-at names, and each write's image stays.
        run = Path(self.dut.logfile).parent
        path = run / f"nvs-{entry.offset:x}.bin"
        if self.started:
            stem = f"nvs-{entry.offset:x}-t{self._hold.now}"
            path, count = run / f"{stem}.bin", 1
            while path.exists():
                count += 1
                path = run / f"{stem}-{count}.bin"
        path.write_bytes(image)
        if not self.started:
            self._nvs[partition] = (entry.offset, path)
            self.log.test(0, call)
            return
        ack = self._hold.write_flash(entry.offset, str(path))
        if ack is None:
            self._hold.check(call)
        elif ack.error is not None:
            raise BackendError(f"board.nvs: the runner cannot write the flash: {ack.error}")
        self.log.test(self._hold.now, call)

    def _partition_now(self, entry: Partition) -> bytes:
        """What the partition `entry` holds now: before the first power-on, what the runner would
        start with, the build's files and the test's NVS images over them; after it, the
        runner's flash."""
        if self.started:
            contents = self._flash_now("board.nvs", "dump the flash to merge over it")
            return contents[entry.offset : entry.offset + entry.size]
        end = entry.offset + entry.size
        # erased, where nothing is flashed
        current = bytearray(b"\xff" * entry.size)
        images = [(f.offset, f.path) for f in self.app.flash_files] + list(self._nvs.values())
        for offset, image in images:
            if offset >= end:
                continue
            try:
                data = image.read_bytes()
            except OSError as error:
                raise BackendError(f"board.nvs: cannot read {image}: {error.strerror}") from error
            low, high = max(offset, entry.offset), min(offset + len(data), end)
            if low < high:
                current[low - entry.offset : high - entry.offset] = data[
                    low - offset : high - offset
                ]
        return bytes(current)

    def flash(self) -> FlashSnapshot:
        """An immutable copy of the whole flash at this instant: at the turn the link is held at,
        the board's time not moving, whether the board is on or off; once the run has ended, as
        the run left it. Each read in a turn removes the file, so that a later end of the run that
        wrote none says so. A runner that dies in the very turn it wrote one, before its answer
        comes, leaves that file, and it is what the run left: the guest has not run since."""
        refuse_board("board.flash")
        if not self.started:
            raise BoardOff(
                "board.flash: read the flash after board.power_on(): until then it holds the build"
                " and the test's NVS"
            )
        table = self._partition_table("board.flash")
        contents = self._flash_now("board.flash")
        self.log.test(self._hold.now, "board.flash()")
        return FlashSnapshot(contents, MappingProxyType(table))

    def _flash_now(self, call: str, dump: str = "write the flash") -> bytes:
        """The runner's whole flash at this instant, as `flash` reads it; `call` names the
        board's call in the errors, and `dump` what the runner could not do when it refuses."""
        ack = self._hold.dump_flash()
        if ack is not None and ack.error is not None:
            raise BackendError(f"{call}: the runner cannot {dump}: {ack.error}")
        try:
            contents = self._flash_out.read_bytes()
            if ack is not None:
                self._flash_out.unlink()
        except OSError as error:
            raise BackendError(
                f"{call}: the run left no flash to read: {error.strerror}; its output is in"
                f" {self.dut.logfile}"
            ) from error
        return contents

    def set_up(self) -> list[str]:
        """What the test has set up that only a run uses: the sources it gave and the taps it
        asked for, as `the replay of uart.gnss`, the lines it drove, as `the level of
        modem.ri`, `a card in sd_card`, and `the NVS partition 'nvs'`."""
        driven = dict.fromkeys(e.name for e in self._lines.events() if e.by == "test")
        set_up = [
            f"the {'real device' if kind == 'real' else kind} of {name}"
            for name, uart in self._uarts.items()
            for kind, given in ((uart.source_kind, uart.source), ("tap", uart.tapped))
            if given is not None
        ] + [f"the level of {name}" for name in driven]
        if self._sd_card is not None and self._sd_card.image_path is not None:
            set_up.append(f"a card in {self._sd_card.name}")
        if self._wifi.configuration is not None:
            set_up.append(
                "the station of wifi"
                if "--wifi-sta" in self._wifi.runner_args()
                else "the access point of wifi"
            )
        if self._bluetooth.devices():
            set_up.append("the devices on the Bluetooth air")
        if self._console.tapped is not None:
            set_up.append("the tap of console")
        if self._bluetooth.tapped is not None:
            set_up.append("the tap of bluetooth")

        set_up += [f"the NVS partition {name!r}" for name in self._nvs]
        return set_up

    @property
    def clock(self) -> str:
        """The clock the run's times are on, which the runner's `hello` must name (protocol 5.1):
        the wall clock with a real source on any route or a bridge on any network, for the whole
        run (protocol 12.4), else the emulated clock."""
        real = any(uart.source_kind == "real" for uart in self._uarts.values())
        bridged = any(network.bridges for _, network in self._networks())
        return recording.WALL if real or bridged else recording.EMULATED

    def _networks(self) -> list[tuple[str, Internet]]:
        """The simulated networks the test gave, by what they are behind: the modem's route or
        `wifi`."""
        networks = [
            (uart.name, uart.modem_model().data)
            for uart in self._uarts.values()
            if isinstance(uart, Modem) and uart.lossless
        ]
        if self._wifi.configuration is not None:
            networks.append(("wifi", self._wifi.network))
        return [(name, network) for name, network in networks if isinstance(network, Internet)]

    def _run_clock(self) -> str:
        """`clock`, for the routes and the link to ask when a recording or the runner comes."""
        return self.clock

    @property
    def app_sha256(self) -> str | None:
        """The app image's hash, as a recording's header has it, taken at setup and again as the
        runner starts, so that a rebuild during the run does not change it; `None` if the build
        has none or it cannot be read."""
        return self._app_sha256

    def _hash_app(self) -> str | None:
        try:
            return app_image_sha256(self.app)
        except OSError:
            return None

    @property
    def powered(self) -> bool:
        """Whether the board's supply is on: from `power_on` to `power_off`."""
        return self.process is not None and self._supply

    @property
    def started(self) -> bool:
        """Whether the test has called `power_on`, and with it started the runner."""
        return self.process is not None

    @property
    def boots(self) -> Sequence[Boot]:
        """The chip's boots, in order: the first at power-on, then one at each reset, whatever
        caused it."""
        self._on()
        return History(lambda: len(self.link.resets) + 1, self._boot_at)

    def _boot_at(self, index: int) -> Boot:
        at, cause, hint = Duration(0), POWERON_RESET, 0
        if index:
            reset = self.link.resets[index - 1]
            at, cause, hint = Duration(reset.t), reset.cause, reset.hint
        number = index + 1
        return Boot(
            number,
            at,
            cause,
            self._uptime_zero(number),
            idf_reset_reason(self.profile.chip, cause, hint),
        )

    @property
    def sleeps(self) -> Sequence[Sleep]:
        """The chip's deep sleeps, oldest first, from the link's `sleep` messages: when each
        began, between the two slice boundaries around it, and what it armed to wake it."""
        self._on()
        return History(self.link.sleep_boots.__len__, self._sleep_at)

    def _sleep_at(self, index: int) -> Sleep:
        boot = self.link.sleep_boots[index]
        sleep = self.link.sleeps[boot]
        return Sleep(boot, Duration(sleep.earliest), Duration(sleep.latest), frozenset(sleep.wakes))

    def _uptime_zero(self, boot: int) -> UptimeZero | None:
        """Where boot number `boot`'s uptime counter counted zero, as the runner last said: `None`
        before it said anything, and once it has said the counter counts from a zero that may lie
        before power-on. A stop says nothing, so a boot that slept keeps its zero."""
        uptime = self.link.uptimes.get(boot)
        if uptime is None or uptime.earliest is None or uptime.latest is None:
            return None
        return UptimeZero(Duration(uptime.earliest), Duration(uptime.latest))

    def _reset(self, message: Reset) -> None:
        """The chip reset: into the run's log, from the link's thread."""
        idf = idf_reset_reason(self.profile.chip, message.cause, message.hint)
        boot = Boot(len(self.link.resets) + 1, Duration(message.t), message.cause, idf_reason=idf)
        reason = boot.reason if idf is None else f"{boot.reason}, {idf}"
        self.log.chip(message.t, f"boot {boot.number}: {reason} (reset cause {boot.cause})")
        self._bluetooth.chip_reset(message.t)
        # The chip's TCP and address go with it: the test's connections to it are lost.
        for _, network in self._networks():
            network.device_reset(message.t, "the device reset")

    def power_on(self, utc: datetime | None = None) -> None:
        """The board's supply on. The first time, starts the runner, which boots the guest: the
        board's clock starts at 0 and stays there until the test waits. `utc`, timezone-aware,
        is the world's time at that power-on; `POWER_ON_UTC` unless given. After `power_off`,
        the supply comes back at the board's time now, and the chip boots through a power-on
        reset; the world's time has run on meanwhile, and cannot be given again."""
        refuse_board("board.power_on")
        if self.powered:
            raise BoardOff("the board is on already: call board.power_off() first")
        if self.started:
            if utc is not None:
                raise ValueError(
                    "power_on: utc is the world's time at the first power-on; it has run on"
                    " since, and the board's clock with it"
                )
            self._switch(True)
            return
        if utc is not None and not isinstance(utc, datetime):
            raise TypeError(f"power_on: utc is {utc!r}, not a datetime with a timezone")
        if utc is not None and utc.utcoffset() is None:
            raise ValueError(f"power_on: utc is {utc!r}, not a datetime with a timezone")
        # Bound before the runner starts, which connects once (protocol §1).
        self._listener, self._socket = listen()
        self._conn: socket.socket | None = None
        # Beside the socket, and gone with it: the whole flash, written once for every run.
        self._flash_out = self._socket.parent / "flash.bin"
        # The runner reads end of file on its end once this process has ended, however it ended:
        # the kernel closes the write end, which no other process holds (os.pipe's ends are not
        # inherited), and the run ends at once (protocol §9).
        end_of_parent, self._parent = os.pipe()
        self._app_sha256 = self._hash_app()
        try:
            command = runner_command(
                self._runner,
                self._rom,
                self.app,
                HELD_MAX_SECONDS,
                self._socket,
                held=True,
                flash_mb=self.flash_mb,
                psram_mb=self.profile.psram_mb,
                sd_card=self._sd_card,
                extra_flash=tuple(self._nvs.values()),
                wifi=self._wifi,
                bluetooth=self._bluetooth,
                realtime=self.clock == recording.WALL,
                flash_out=self._flash_out,
                console=self.profile.console,
                parent_fd=end_of_parent,
            )
            try:
                process = RunnerPopen(
                    self._msg_queue, command, self._meta, pass_fds=(end_of_parent,)
                )
            except OSError as error:
                raise BackendError(f"cannot start {command[0]}: {error.strerror}") from error
        except BaseException:
            # whatever failed, the board keeps no runner, and `close` lets go of nothing: the write
            # end, which a runner that did start before the failure reads end of file on, and the
            # link's socket and directory go here
            self._close_parent()
            self._close_link()
            raise
        finally:
            os.close(end_of_parent)
        self.process = process
        self._supply = True
        self._utc = POWER_ON_UTC if utc is None else utc.astimezone(UTC)
        self.log.test(0, f"board.power_on(), the world's time {self._utc:%Y-%m-%dT%H:%M:%SZ}")
        self._linker = threading.Thread(target=self._serve_link, daemon=True)
        self._linker.start()
        # The only thread that waits for the runner: Popen.poll() from another thread can miss an
        # exit while this one is in waitpid().
        self._waiter = threading.Thread(target=self._mark_end_of_run, daemon=True)
        self._waiter.start()

    def power_off(self) -> None:
        """The board's supply off, at the board's time now: the chip stops, and loses what is
        not in flash; the SD card, and the parts of the board a model stands for, lose their
        power with it. The board's clock runs on, and the test may wait, until `power_on`."""
        refuse_board("board.power_off")
        if not self.started:
            raise BoardOff("the board is off; call board.power_on() first")
        if not self._supply:
            raise BoardOff("the board is off already: call board.power_on() to switch it on")
        self._switch(False)

    def reset(self) -> None:
        """Pulses the chip's EN pin at the board's time now, as the board's reset button does:
        the chip boots again through a power-on reset, and the rest of the board keeps its
        power."""
        refuse_board("board.reset")
        if not self.powered:
            raise BoardOff("the board is off: board.reset() resets a board that is on")
        self._hold.check("board.reset()")
        now = self._hold.now
        # The Bluetooth controller resets with the chip, now: what it made for the old host must
        # not reach the new one, after the reboot that empties the runner's queue.
        self._bluetooth.chip_reset(now)
        self.link.board_input(ChipReset(now))
        self.log.test(now, "board.reset()")

    def _switch(self, on: bool) -> None:
        """The board's supply on or off at the board's time now, for the runner and for the
        models on it; `RunEnded`, and nothing switched, once the run has ended."""
        step = "board.power_on()" if on else "board.power_off()"
        self._hold.check(step)
        now = self._hold.now
        self.link.board_input(Power(now, on))
        self._supply = on
        if not on:
            # Before the models lose their power, the modem's network with them: the device's
            # loss is the cause.
            for _, network in self._networks():
                network.device_reset(now, "the device lost its power")
        for uart in self._uarts.values():
            uart.supply(on)
        self._bluetooth.supply(on)
        self.log.test(now, step)

    @property
    def now(self) -> Duration:
        """The board's time since the first power-on: where the test's clock stands. It runs on
        through resets, and while the board is off."""
        return Duration(self._on().now)

    @property
    def utc(self) -> datetime:
        """The world's time now: `utc_at(now)`, the UTC instant at the first power-on plus the
        board's time since."""
        return self._utc + timedelta(microseconds=self._on().now // 1000)

    def utc_at(self, at: Duration) -> datetime:
        """The world's time at the board's instant `at`: the UTC instant at the first power-on,
        plus `at`, which may be later than `now`. A model calls it from its `poll` or `receive`
        to date what it sends."""
        self._on()
        return self._utc + timedelta(microseconds=nanoseconds(at, "board.utc_at") // 1000)

    def wait(self, duration: Duration) -> None:
        """Lets the board run for `duration` of its own time."""
        refuse_wait("board.wait")
        clock = self._on()
        ns = nanoseconds(duration, "board.wait")
        self.log.test(clock.now, f"board.wait({seconds(ns)})")
        try:
            clock.wait(ns)
        except RunEnded:
            self.log.test(clock.now, "the run ended while the test waited")
            raise

    def wait_until(self, condition: Callable[[], T | None], *, within: Duration) -> T:
        """Lets the board run until `condition` returns something true, for at most `within` of
        its time; returns what it returned. `condition` is asked now, then at every turn of the
        link, at least every 100 ms of the board's time. Raises `NeverHappened` when `within`
        runs out, and `RunEnded` when the run ends first."""
        refuse_wait("board.wait_until")
        name = getattr(condition, "__qualname__", None)
        description = f"the condition {name}" if name else repr(condition)
        return self._waited(condition, within, description, "board.wait_until: within")

    def wait_for_wake(self, *, within: Duration) -> Boot:
        """Lets the board run to the chip's next boot, for at most `within` of its time, and
        returns it; the boot must be a wake from deep sleep. Unlike `wait_until`, it asks no
        condition every 100 ms: the run goes to the end of `within` at once, and the link takes a
        turn at most `WAKE_LATENCY_NS` after each event, the reset among them, so a deep sleep
        costs no turns. Raises `NeverHappened` when `within` runs out, `RunEnded` when the run
        ends first, and `AssertionError` for a boot through another reset."""
        refuse_wait("board.wait_for_wake")
        clock = self._on()
        if not self._supply:
            raise BoardOff("the board is off: board.wait_for_wake() waits for a board that is on")
        ns = nanoseconds(within, "board.wait_for_wake: within")
        known = len(self.link.resets)

        def booted() -> Boot | None:
            return self.boots[known + 1] if len(self.link.resets) > known else None

        description = "a wake from deep sleep"
        self.log.test(clock.now, f"board.wait_for_wake(), up to {seconds(ns)}")
        try:
            boot = clock.wait_for_event(booted, ns, description, WAKE_LATENCY_NS)
        except NeverHappened:
            self.log.test(clock.now, f"{description}: did not happen within {seconds(ns)}")
            raise
        except RunEnded:
            self.log.test(clock.now, f"{description}: the run ended first")
            raise
        if boot.cause != DEEPSLEEP_RESET:
            self.log.test(
                clock.now, f"{description}: boot {boot.number} is {boot.reason}, not a wake"
            )
            raise AssertionError(
                f"board.wait_for_wake: boot {boot.number} at {boot.at} is {boot.reason} (reset"
                f" cause {boot.cause}), not a wake from deep sleep (DEEPSLEEP_RESET)"
            )
        self.log.test(clock.now, f"{description}: boot {boot.number} at {boot.at}")
        return boot

    def wait_on_clock(
        self, condition: Callable[[], T | None], within: Duration, description: str
    ) -> T:
        """`wait_until`, with `description` naming the condition in its errors and in the log."""
        refuse_wait("board.wait_on_clock")
        return self._waited(condition, within, description, "within")

    def _waited(
        self, condition: Callable[[], T | None], within: Duration, description: str, what: str
    ) -> T:
        """The wait of `wait_until` and `wait_on_clock`, logged; `what` names `within` in the
        error for one that is not a `Duration`."""
        clock = self._on()
        ns = nanoseconds(within, what)
        self.log.test(clock.now, f"waits up to {seconds(ns)} for {description}")
        try:
            value = clock.wait_until(condition, ns, description)
        except NeverHappened as error:
            because = f": {error.why}" if error.why is not None else ""
            self.log.test(clock.now, f"{description}: did not happen within {seconds(ns)}{because}")
            raise
        except RunEnded:
            self.log.test(clock.now, f"{description}: the run ended first")
            raise
        self.log.test(clock.now, f"{description}: happened")
        return value

    def _on(self) -> HeldClock:
        if not self.started:
            raise BoardOff("the board is off; call board.power_on() first")
        return self._hold

    def _end_run(self, reason: str) -> None:
        """Ends the run as soon as the harness finds the link wrong, instead of at the runner's
        next turn, which may come only at the end of the test's wait. `reason` is the link's
        `error` too, which `_mark_end_of_run` puts first in the `RunEnded` a waiting test gets
        once the runner has exited."""
        self._terminate()

    def _terminate(self) -> None:
        """Sends the runner SIGTERM, once, unless it has exited. Not
        DuplicateStdoutPopen.terminate(), which closes the log before the signal."""
        with self._terminating:
            if self.process is not None and not self._terminated and not self._exited.is_set():
                self._terminated = True
                self.process.send_signal(signal.SIGTERM)

    def _serve_link(self) -> None:
        """Accepts the runner's one connection and reads it to end of file. Gives up on a runner
        that exits without connecting, as one does on a usage error (protocol §9)."""
        self._listener.settimeout(0.05)
        while True:
            # Read before the accept: a runner that connected and then exited is accepted still.
            exited = self._exited.is_set()
            try:
                conn, _ = self._listener.accept()
            except TimeoutError:
                if exited:
                    return
                continue
            break
        conn.settimeout(None)
        self._conn = conn
        self.link.run(conn)

    def _mark_end_of_run(self) -> None:
        assert self.process is not None
        code = self.process.wait()
        self._exited.set()
        # A runner gone while the link was held at a turn: the link stops waiting for the test,
        # and reads the link to its end.
        self._hold.stop()
        # The link's last messages are read first, so that the reason below holds what the
        # harness found wrong, and a test's last look at the console sees all of it.
        self._linker.join(EXIT_SECONDS)
        output = last_output(Path(self.process._logfile))
        error = self.link.error
        # Appended through a file of its own: the runner's log forwarder sends it on in order.
        with open(self.process._logfile, "a") as log:
            if error is not None:
                log.write(f"\nesp32tl: the link failed: {error}\n")
            log.write(f"\nesp32tl: the runner exited with code {code}\n")
        # The harness's reason first: when it ended the run, the runner exited 0.
        reason = f"the link failed: {error}; " if error is not None else ""
        if (why := self._runner_reason()) is not None:
            reason += f"{why}; "
        # Where the run ended, from the runner's stats when it sent them.
        at = self.link.stats.t if self.link.stats is not None else None
        self._hold.end(
            f"{reason}the runner exited with code {code}; its last output:\n{output}", at
        )

    def _runner_reason(self) -> str | None:
        """What the runner's `stats` say ended the run, when the runner's own words do not say it
        for the test: a firmware that started its Bluetooth controller under `--no-bt`, or a
        controller esp32sim could not start under `--bt`."""
        stats = self.link.stats
        if stats is not None and stats.end == NO_BT_END:
            return no_bluetooth(stats.t)
        if stats is not None and stats.end == BT_FAILED_END:
            return bluetooth_failed(stats.t, stats.detail or "")
        return None

    def close(self) -> None:
        """Ends the run, then lets go of what the test gave the board. What could not be let go
        fails the test, unless the end of the run has already said why it fails."""
        try:
            self._close()
        finally:
            problems = self._release()
            self._bluetooth.close()
        if problems:
            raise BackendError("; ".join(problems))

    def _release(self) -> list[str]:
        """Closes the SD card, every real source's port and every bridge's connections, each
        whatever another one raises: a port left open holds its thread, and its lock against the
        next test. Returns what raised."""
        closing = [("sd_card", self._sd_card.close)] if self._sd_card is not None else []
        closing += [
            (uart.name, uart.source.close)
            for uart in self._uarts.values()
            if isinstance(uart.source, Real)
        ]
        closing += [(name, network.close) for name, network in self._networks() if network.bridges]
        problems = []
        for name, close in closing:
            try:
                close()
            except SourceError as error:
                problems.append(str(error))
            except Exception as error:
                problems.append(f"{name} cannot be closed: {error}")
        return problems

    def _close(self) -> None:
        """Ends the run (protocol §9): `stop` at the turn the test left it at, on which the
        runner sends its last warnings and its stats, closes the link and exits 0; SIGTERM, once
        the runner has said hello, when it has not exited within EXIT_SECONDS of that; the link
        read to end of file; the runner waited for, and killed only when it has not exited within
        EXIT_SECONDS of the signal. Then stops the forwarding of its log and waits for it, so the
        runner's last lines are on the dut's queue when this returns; pytest-embedded's listener,
        which takes them off the queue into `dut.log`, is stopped right after the dut and may
        still miss them. Radio diagnostics are read from the finished
        runner log into the teardown report, independently of that forwarder. A runner that exited
        with an error, one that had to be killed, and a link the harness found wrong fail the
        test, even one whose expects all passed before it. Once the runner is gone and the link
        read, writes the recording of every route the test tapped, also of a run that failed; one
        that cannot be written fails the test too. A board never powered on has no runner to end,
        and no recording to write."""
        held = self._hold.stop()
        if self.process is None:
            return
        if held:
            # The runner ends the run on the stop and exits 0; the signal below is for one that
            # does not, and would race the stop for the run's end.
            self._exited.wait(EXIT_SECONDS)
        # The runner handles SIGTERM once it has connected, and has by its hello; before that the
        # signal kills it (protocol §9), as it would at the end of a test that ends while the
        # runner loads the images. It gets EXIT_SECONDS to get there, then as long again to exit.
        deadline = time.monotonic() + EXIT_SECONDS
        while (
            self.link.runner is None and not self._exited.wait(0.01) and time.monotonic() < deadline
        ):
            pass
        deadline = time.monotonic() + EXIT_SECONDS
        self._terminate()
        self._linker.join(max(0.0, deadline - time.monotonic()))
        # Whether the runner has exited, not whether the waiter is done: the waiter also waits
        # for the link.
        killed = not self._exited.wait(max(0.0, deadline - time.monotonic()))
        if killed:
            self.process.kill()
            self._exited.wait()
        # only now: a runner that read end of file on it would end the run as the peer gone
        self._close_parent()
        self._close_link()
        self._waiter.join()
        # The runner and waiter have finished writing. The asynchronous log forwarder can
        # still lag behind, so keep radio diagnostics directly from the complete source file:
        # esp32sim's Wi-Fi lines, and the runner's on the Bluetooth hooks of an app the
        # bootloader started from another ELF than the build's (`bluetooth.add_elf`).
        try:
            with open(self.process._logfile, encoding="utf-8", errors="replace") as log:
                # stdout and stderr share this file: the guest's last console fragment
                # need not end with a newline before the runner's first diagnostic.
                self._radio_diagnostics = [
                    marker + suffix.rstrip("\r\n")
                    for line in log
                    for marker in RADIO_DIAGNOSTICS
                    for _, found, suffix in [line.partition(marker)]
                    if found
                ]
        except OSError:
            # Optional diagnostics must not prevent closing the process or writing taps.
            self._radio_diagnostics = []
        # RunnerPopen.close waits for the forwarder.
        self.process.close()
        # DuplicateStdoutPopen does not close the stdin pipe it opens, which the runner does not
        # read.
        if self.process.stdin is not None:
            self.process.stdin.close()
        # Judged by the code after the runner has stopped, not by whether it had stopped before:
        # since #11 the runner ends a run on SIGTERM and exits 0.
        problems = self._write_taps()
        if killed:
            problems.append(
                f"the runner did not exit within {EXIT_SECONDS} s of SIGTERM, and was killed"
            )
        elif self.process.returncode != 0:
            if (why := self._runner_reason()) is not None:
                problems.append(why)
            problems.append(f"the runner exited with code {self.process.returncode}")
        if self.link.error is not None:
            problems.append(f"the link failed: {self.link.error}")
        if problems:
            raise BackendError("; ".join(problems) + f"; its output is in {self.dut.logfile}")

    def _write_taps(self) -> list[str]:
        """Writes the recording of every tapped route, and the btsnoop file of the Bluetooth
        tap; returns what could not be written."""
        tapped = [
            uart for uart in [self._console, *self._uarts.values()] if uart.tapped is not None
        ]
        if not tapped and self._bluetooth.tapped is None:
            return []
        runner = self.link.runner
        tools = {"esp32sim": runner.esp32sim} if runner and runner.esp32sim else {}
        problems = []
        hci = self._bluetooth.tapped
        if hci is not None:
            try:
                made = hci.recording(
                    board=self.profile.name,
                    source=TAP_SOURCE,
                    clock=self.clock,
                    app_sha256=self.app_sha256,
                    tools=tools,
                    # The devices the test made on the air, by their names: whom the packets
                    # name by address.
                    extra={
                        "devices": [
                            {"name": n, "address": a} for n, a, _ in self._bluetooth.devices()
                        ]
                    },
                )
                recording.write_btsnoop(hci.path, made)
            except (OSError, recording.RecordingError) as error:
                problems.append(f"the tap of bluetooth cannot be written: {error}")
        for uart in tapped:
            assert uart.tapped is not None
            try:
                made = uart.tapped.recording(
                    board=self.profile.name,
                    source=uart.recorded_source(),
                    clock=self.clock,
                    # The app image alone, as the format has it (plan §4.3).
                    app_sha256=self.app_sha256,
                    tools=tools,
                )
                recording.write(uart.tapped.path, made)
            except (OSError, recording.RecordingError) as error:
                # A problem of the test's like the others, so that the link's error, which a
                # recording the format refuses comes with, is not lost.
                problems.append(f"the tap of {uart.name} cannot be written: {error}")
        return problems

    def report(self, run: bool = False) -> str:
        """The build the test loaded (`build_line`), the link's report, then the bytes on every
        route, the SD card slot's card when the profile has a slot, and, with `run`, the run on
        the board's clock (`RunLog.lines`): for the report of a test that failed."""
        lines = [build_line(self.app, self.app_sha256), self.link.report(), "routes:"]
        for name, uart in self._uarts.items():
            traffic = self.log.traffic.get(name)
            source = (
                f"the {uart.source_kind} of {uart.source_detail}"
                if uart.source_kind is not None
                else "nothing at the far end"
            )
            crossed = (
                f"{traffic.bytes_in} bytes in, {traffic.bytes_out} out" if traffic else "no bytes"
            )
            if isinstance(uart.source, Real):
                source = f"the real device on {uart.source.port}"
                crossed += f", {uart.source.dropped} dropped"
            tap = f"; tapped into {uart.tapped.path}" if uart.tapped is not None else ""
            lines.append(f"  {name}: {source}; {crossed}{tap}")
            if isinstance(uart.source, Replay) and uart.source.unseen_count:
                lines.append(f"    unseen requests: {uart.source.unseen_count}")
                for request, count in uart.source.unseen.items():
                    lines.append(f"      {count} x {shown(request)}")
                lines.append("    unseen request events:")
                lines.extend(
                    f"      {board_time(t)}  {shown(request, limit=len(request))}"
                    for t, request in uart.source.unseen_events
                )
        if self._console.tapped is not None:
            lines.append(
                f"console: {len(self.console_output)} bytes out;"
                f" tapped into {self._console.tapped.path}"
            )
        if self._sd_card is not None:
            image = self._sd_card.held
            lines.append(f"sd_card: a card holding {image}" if image else "sd_card: empty")
            lines += [
                f"  {board_time(change.t)}  "
                + ("taken out" if change.image is None else f"{change.image} put in")
                for change in self._sd_card.changes
            ]
            lines += [
                f"  {board_time(change.t)}  {why}: "
                + ("taken out" if change.image is None else f"{change.image} put in")
                for why, changes in (
                    ("refused", self._sd_card.refused),
                    ("not reached", self._sd_card.not_reached),
                )
                for change in changes
            ]
            lines += self._sd_card.report()
        for uart in self._uarts.values():
            if isinstance(uart, Modem):
                lines += uart.command_report()
        if self._wifi.configuration is not None:
            lines += self._wifi.report()
        if self._bluetooth.used:
            lines += self._bluetooth.report()
        lines += self._radio_diagnostics
        if run:
            lines += self.log.lines()
        return "\n".join(lines)

    def properties(self) -> dict[str, object]:
        """The link's `properties()`, `build` (`build_properties`), and the routes: for each, the
        kind of source at the far end and what it was made from (a replay's recording, as the
        test named it), the bytes each way and the tap's file; and `sd_card`, the card's image as
        the run has it, the changes the runner acknowledged while the board was on and those it
        did not reach, or None without a slot."""
        routes: dict[str, dict[str, object]] = {}
        for name, uart in self._uarts.items():
            traffic = self.log.traffic.get(name)
            routes[name] = {
                "source": uart.source_kind,
                "detail": uart.source_detail,
                "bytes_in": traffic.bytes_in if traffic else 0,
                "bytes_out": traffic.bytes_out if traffic else 0,
                "tap": None if uart.tapped is None else str(uart.tapped.path),
            }
            if isinstance(uart.source, Real):
                routes[name]["dropped"] = uart.source.dropped
            if isinstance(uart, Modem) and isinstance(uart.source, ModemModel):
                routes[name]["commands"] = [
                    {
                        "t_ns": c.at.ns,
                        "command": c.text,
                        "fallback": c.fallback,
                        "response": c.response.decode("ascii", errors="replace"),
                    }
                    for c in uart.commands
                ]
                data = uart.modem_model().data
                if isinstance(data, Internet):
                    routes[name]["network"] = data.transcript()
            if isinstance(uart.source, Replay) and uart.source.unseen_count:
                routes[name]["unseen_requests"] = [
                    {"request_hex": request.hex(), "count": count}
                    for request, count in uart.source.unseen.items()
                ]
                routes[name]["unseen_request_events"] = [
                    {"t_ns": t, "request_hex": request.hex()}
                    for t, request in uart.source.unseen_events
                ]
        card = self._sd_card
        sd_card = (
            None
            if card is None
            else {
                "image": None if card.held is None else str(card.held),
                "changes": [
                    {"t_ns": c.t, "image": None if c.image is None else str(c.image)}
                    for c in card.changes
                ],
                **{
                    key: [
                        {"t_ns": c.t, "image": None if c.image is None else str(c.image)}
                        for c in changes
                    ]
                    for key, changes in (
                        ("refused", card.refused),
                        ("not_reached", card.not_reached),
                    )
                },
                "faults": card.properties(),
            }
        )
        properties = self.link.properties() | {
            "build": build_properties(self.app, self.app_sha256),
            "routes": routes,
            "sd_card": sd_card,
            "wifi": self._wifi.properties(),
            "bluetooth": self._bluetooth.properties(),
        }
        if self._console.tapped is not None:
            properties["console"] = {
                "bytes_out": len(self.console_output),
                "tap": str(self._console.tapped.path),
            }
        return properties

    def _close_parent(self) -> None:
        """Closes the write end of the runner's `--parent-fd` pipe, once."""
        if self._parent is not None:
            os.close(self._parent)
            self._parent = None

    def _close_link(self) -> None:
        """Closes the socket, once the runner is gone and the link read to its end."""
        if hasattr(self, "_linker"):
            # A runner that is gone has closed its end of the link, so the thread is at end of
            # file, or soon; were it still reading, the shutdown ends the read.
            self._linker.join(EXIT_SECONDS)
            if self._linker.is_alive() and self._conn is not None:
                with contextlib.suppress(OSError):
                    self._conn.shutdown(socket.SHUT_RDWR)
                self._linker.join()
        if self._conn is not None:
            self._conn.close()
        self._listener.close()
        shutil.rmtree(self._socket.parent, ignore_errors=True)


def refuse(reason: str) -> NoReturn:
    """What `Esp32Sim` does with a source its declaration does not give, outside pytest."""
    raise SourceError(reason)


def ended_while_waiting(error: BaseException) -> bool:
    """Whether `error` is, or came from, the `RunEnded` of a test that waited on the board: its
    message names the runner's exit code and what the link found already."""
    cause: BaseException | None = error
    # A __cause__ set by hand can make a cycle, as the traceback module knows.
    seen: set[int] = set()
    while cause is not None and id(cause) not in seen:
        if isinstance(cause, RunEnded):
            return True
        seen.add(id(cause))
        cause = cause.__cause__ or cause.__context__
    return False


# How much of the runner's last output `RunEnded` carries.
LAST_OUTPUT_BYTES = 2048


def last_output(log: Path) -> str:
    """The end of the runner's log, up to LAST_OUTPUT_BYTES: whole lines, without colour codes.
    From the first whole line, if one follows the cut one: better a line cut at its start than no
    output at all, which a single long line ending in \\r\\n would leave."""
    try:
        with log.open("rb") as file:
            size = file.seek(0, os.SEEK_END)
            file.seek(max(0, size - LAST_OUTPUT_BYTES))
            tail = file.read()
    except OSError as error:
        return f"(the log {log} cannot be read: {error.strerror})"
    if size > LAST_OUTPUT_BYTES:
        rest = tail.partition(b"\n")[2]
        if rest.strip():
            tail = rest
    return remove_asci_color_code(tail)
