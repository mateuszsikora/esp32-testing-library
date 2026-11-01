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
profile, which says nothing until the test gives it a source, as `gnss.replay(path)` does before
power-on, and the profile's GPIO lines as one `Lines`, which puts the board's pulls on them from
power-on and what the test drives at the instant it does. The backend also owns the end of the
run (§9), before the dut is torn down: `stop` at the turn the test left the run at, `SIGTERM` for
a runner that is not there, the link read to end of file, the runner waited for, `SIGKILL` only as
a last resort. Then it writes the recordings of the routes the test tapped.

What the test does on the board, what the firmware prints and the bytes on every route go into a
`RunLog` (`esp32tl.report`), which the report of a test that failed shows after the link's.
"""

import contextlib
import os
import shutil
import signal
import socket
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NoReturn, TypeVar

from pytest_embedded.dut import Dut
from pytest_embedded.log import DuplicateStdoutPopen, MessageQueue
from pytest_embedded.utils import Meta, remove_asci_color_code
from pytest_embedded_idf.app import IdfApp

from esp32tl import recording
from esp32tl.backends.clock import HeldClock, nanoseconds, seconds
from esp32tl.boards import BoardProfile, capabilities
from esp32tl.boards.capabilities import CapabilityError, Request
from esp32tl.interfaces import BoardOff, NeverHappened, RunEnded, SourceError
from esp32tl.interfaces.lines import Lines
from esp32tl.interfaces.uart import Uart
from esp32tl.link.boardlink import BoardLink, Wire
from esp32tl.report import RunLog
from esp32tl.tap import NO_SOURCE, sha256

T = TypeVar("T")

# The checkout this package runs from, with an editable install (`make setup` makes one): the
# runner and the ROM are looked for there. From a wheel this is site-packages, and the messages
# below point to ESP32TL_RUNNER and ESP32TL_ROM; prebuilt runners come with milestone 10.
ROOT = Path(__file__).resolve().parents[2]

# The ROM ELF scripts/fetch-rom-elf puts in .cache/ (plan M0). The runner's own lookup prefers the
# newest release under ~/.espressif instead, so the backend always passes --rom.
ROM_RELEASE = "20241011"
ROM_ELF = "esp32_rev300_rom.elf"

# How long a run of the esp32tl command lasts by default, in emulated seconds: its clock runs
# free. hello_world runs 12 emulated seconds in 0.3 s of wall time on an M2 Pro, so 300 is a few
# seconds there (an image that never idles runs slower).
MAX_SECONDS = 300

# The --max-seconds of a run on the test's clock, a day of emulated time: the flag is required,
# and a run lasts as long as the test waits. A test that reaches it gets RunEnded.
HELD_MAX_SECONDS = 86400

# The world's time at power-on, unless the test gives another: runs repeat whatever the host's
# clock says.
POWER_ON_UTC = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

# How long, in wall seconds, the runner has after SIGTERM to send its stats, close the link and
# exit, before SIGKILL (protocol §9). It needs at most one emulated millisecond.
EXIT_SECONDS = 5

# The longest Unix socket path on macOS, NUL included; Linux takes 108.
SUN_PATH_MAX = 104


class BackendError(Exception):
    """What keeps the backend from starting, said so that the reader knows what to do."""


def runner_path() -> Path:
    """$ESP32TL_RUNNER, or the release build `make setup` leaves in runner/target/."""
    path = Path(os.environ.get("ESP32TL_RUNNER") or ROOT / "runner/target/release/esp32tl-runner")
    if not (path.is_file() and os.access(path, os.X_OK)):
        raise BackendError(f"no runner at {path}: run make setup, or set ESP32TL_RUNNER")
    return path


def rom_path() -> Path:
    """$ESP32TL_ROM, or the pinned release `make setup` fetches into .cache/."""
    path = Path(
        os.environ.get("ESP32TL_ROM") or ROOT / ".cache/esp-rom-elfs" / ROM_RELEASE / ROM_ELF
    )
    if not path.is_file():
        raise BackendError(f"no ROM ELF at {path}: run make setup, or set ESP32TL_ROM")
    return path


def load_app(app_path: str) -> IdfApp:
    """The ESP-IDF build in `app_path`/build, checked for what the runner needs.

    The build directory is given to `IdfApp` as an absolute path: given `build`, it would fall back
    to a `build` directory in the working directory when the app has none.
    """
    build = Path(app_path) / "build"
    if not (build / "flasher_args.json").is_file():
        raise BackendError(f"{build} holds no ESP-IDF build: no flasher_args.json")
    app = IdfApp(app_path, build_dir=str(build))
    if app.target != "esp32":
        raise BackendError(f"{build} is built for {app.target}; esp32sim runs the classic esp32")
    if app.flash_settings.get("encrypt"):
        raise BackendError(f"{build} asks for flash encryption, which the runner does not do")
    missing = [f.file_path for f in app.flash_files if not Path(f.file_path).is_file()]
    if missing:
        raise BackendError(f"{build} is not a finished build, missing {', '.join(missing)}")
    return app


def runner_command(
    runner: Path, rom: Path, app: IdfApp, max_seconds: float, link: Path, *, held: bool = False
) -> list[str]:
    """The runner's command line: every file of `flasher_args.json` at its offset, its board the
    socket at `link`. A `held` run is on the test's clock: its console also goes on the link, and
    the runner waits for the harness's turn for as long as it takes."""
    command = [str(runner), "--chip", "esp32", "--rom", str(rom), "--max-seconds", str(max_seconds)]
    if held:
        command += ["--console", "both", "--peer-timeout", "0"]
    command += ["--board", f"socket:{link}"]
    for file in app.flash_files:
        command += ["--flash-at", f"{file.offset:#x}={file.file_path}"]
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
    kind, _, role = interface.partition(".")
    route = profile.uart.get(role) if kind == "uart" else None
    if route is None:
        raise BackendError(
            f"the board profile {profile.name} has no {interface};"
            f" its interfaces are {', '.join(profile.interfaces)}"
        )
    return Uart(
        interface,
        Wire(tx=route.tx, rx=route.rx, baud=route.baud),
        clock=clock,
        directory=directory,
        asked=asked,
        log=log,
        files=files,
    )


class RunnerPopen(DuplicateStdoutPopen):
    """A `DuplicateStdoutPopen` that closes its log when the runner cannot start: the parent
    opens the log before it starts the process, and nothing else can close it then."""

    def __init__(self, msg_queue: MessageQueue, cmd: list[str], meta: Meta) -> None:
        try:
            super().__init__(msg_queue, cmd, meta)
        except OSError:
            log = getattr(self, "_fw", None)
            if log is not None:
                log.close()
            raise


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


class Esp32Sim:
    """A run of the app in `app_path` in the runner, on the board profile `profile`, its console
    going to `dut`'s log and to the test, its board the link to every UART route and every GPIO
    line of the profile.

    `app` is the `IdfApp` the runner flashes. The dut's own `app` is pytest-embedded's base `App`,
    without `flash_files` or `target`: without a service, pytest-embedded builds nothing more.
    `profile`'s name and chip go into the harness's `hello`. `directory` is the test file's, which
    a relative recording path is taken from. `skip` is called with the reason when the test asks
    a route for a source this backend's capability declaration does not give, and raises; the
    `board` fixture makes it pytest's skip. `link` is the `BoardLink`, whose warnings, stats and
    error are complete once `close` returns; `log` the `RunLog` of the run.
    """

    name = "esp32sim"
    # The clock the run's times are on, which the runner's `hello` must name (protocol 5.1).
    clock = recording.EMULATED

    def __init__(
        self,
        dut: Dut,
        app_path: str,
        msg_queue: MessageQueue,
        meta: Meta,
        profile: BoardProfile,
        directory: Path | None = None,
        skip: Callable[[str], NoReturn] | None = None,
    ) -> None:
        self.dut = dut
        self.profile = profile
        self.directory = directory or Path.cwd()
        self._msg_queue = msg_queue
        self._meta = meta
        self._skip = skip or refuse
        self.app = load_app(app_path)
        self._runner, self._rom = runner_path(), rom_path()
        # What the guest printed on its console, as the link brings it.
        self.console_output = bytearray()
        self.log = RunLog()
        self._hold = HeldClock()
        # Every route of the profile, listening and silent until the test gives it a source.
        # One mapping of the files they read and write: no tap writes over another route's file.
        files: dict[Path, tuple[str, str]] = {}
        self._uarts = {
            f"uart.{role}": profile_uart(
                profile,
                f"uart.{role}",
                clock=self.clock,
                directory=self.directory,
                asked=self._asked,
                log=self.log,
                files=files,
            )
            for role in profile.uart
        }
        # Before power-on the clock's time is 0: what the test drives then holds from the start.
        self._lines = Lines(profile.name, profile.lines, lambda: self._hold.now, log=self.log)
        self.link = BoardLink(
            profile.name,
            [*self._uarts.values(), self._lines],
            chip=profile.chip,
            on_error=self._end_run,
            hold=self._hold,
            console=self._printed,
            clock=self.clock,
        )
        self.process: RunnerPopen | None = None
        self._utc = POWER_ON_UTC
        self._exited = threading.Event()
        # One SIGTERM at most: a second one kills the runner, with 128 + 15 (protocol §9).
        self._terminated = False
        self._terminating = threading.Lock()

    def uart(self, role: str) -> Uart:
        """The `Uart` on the link for the profile's `uart.<role>` route."""
        interface = f"uart.{role}"
        uart = self._uarts.get(interface)
        if uart is None:
            raise BackendError(
                f"the board profile {self.profile.name} has no {interface};"
                f" its interfaces are {', '.join(self.profile.interfaces)}"
            )
        return uart

    def lines(self) -> Lines:
        """The `Lines` on the link for the profile's `[lines.<role>]` sets."""
        if not self.profile.lines:
            raise BackendError(
                f"the board profile {self.profile.name} has no lines;"
                f" its interfaces are {', '.join(self.profile.interfaces)}"
            )
        return self._lines

    def _printed(self, t: int, data: bytes) -> None:
        self.console_output.extend(data)
        self.log.console(t, data)

    def _asked(self, request: Request) -> None:
        """Refuses a source or a tap on an interface once the board is on, or one that no
        backend gives, and skips the test when this one does not."""
        if self.powered:
            what = "tap a route" if request.tap else "give a route its source"
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

    def set_up(self) -> list[str]:
        """What the test has set up that only a run uses: the sources it gave and the taps it
        asked for, as `the replay of uart.gnss`, and the lines it drove, as `the level of
        modem.ri`."""
        driven = dict.fromkeys(e.name for e in self._lines.events() if e.by == "test")
        return [
            f"the {kind} of {name}"
            for name, uart in self._uarts.items()
            for kind, given in ((uart.source_kind, uart.source), ("tap", uart.tapped))
            if given is not None
        ] + [f"the level of {name}" for name in driven]

    @property
    def powered(self) -> bool:
        """Whether the test has called `power_on`."""
        return self.process is not None

    def power_on(self, utc: datetime | None = None) -> None:
        """Starts the runner, which boots the guest: the board's clock starts at 0 and stays
        there until the test waits. `utc`, timezone-aware, is the world's time at power-on;
        `POWER_ON_UTC` unless given."""
        if self.powered:
            raise BoardOff("the board is on already: power it on once")
        if utc is not None and not isinstance(utc, datetime):
            raise TypeError(f"power_on: utc is {utc!r}, not a datetime with a timezone")
        if utc is not None and utc.utcoffset() is None:
            raise ValueError(f"power_on: utc is {utc!r}, not a datetime with a timezone")
        # Bound before the runner starts, which connects once (protocol §1).
        self._listener, self._socket = listen()
        self._conn: socket.socket | None = None
        command = runner_command(
            self._runner, self._rom, self.app, HELD_MAX_SECONDS, self._socket, held=True
        )
        try:
            process = RunnerPopen(self._msg_queue, command, self._meta)
        except OSError as error:
            self._close_link()
            raise BackendError(f"cannot start {command[0]}: {error.strerror}") from error
        self.process = process
        self._utc = POWER_ON_UTC if utc is None else utc.astimezone(UTC)
        self.log.test(0, f"board.power_on(), the world's time {self._utc:%Y-%m-%dT%H:%M:%SZ}")
        self._linker = threading.Thread(target=self._serve_link, daemon=True)
        self._linker.start()
        # The only thread that waits for the runner: Popen.poll() from another thread can miss an
        # exit while this one is in waitpid().
        self._waiter = threading.Thread(target=self._mark_end_of_run, daemon=True)
        self._waiter.start()

    @property
    def now(self) -> float:
        """The board's time since power-on, in seconds: where the test's clock stands."""
        return self._on().now / 10**9

    @property
    def utc(self) -> datetime:
        """The world's time: the UTC instant at power-on, plus the board's time since."""
        return self._utc + timedelta(microseconds=self._on().now // 1000)

    def wait(self, duration: float) -> None:
        """Lets the board run for `duration` seconds of its own time."""
        clock = self._on()
        ns = nanoseconds(duration)
        self.log.test(clock.now, f"board.wait({seconds(ns)})")
        try:
            clock.wait(ns)
        except RunEnded:
            self.log.test(clock.now, "the run ended while the test waited")
            raise

    def wait_until(self, condition: Callable[[], T], *, within: float) -> T:
        """Lets the board run until `condition` returns something true, for at most `within`
        seconds of its time; returns what it returned. `condition` is asked now, then at every
        turn of the link, at least every 100 ms of the board's time. Raises `NeverHappened`
        when `within` runs out, and `RunEnded` when the run ends first."""
        name = getattr(condition, "__qualname__", None)
        description = f"the condition {name}" if name else repr(condition)
        return self.wait_on_clock(condition, within, description)

    def wait_on_clock(self, condition: Callable[[], T], within: float, description: str) -> T:
        """`wait_until`, with `description` naming the condition in its errors and in the log."""
        clock = self._on()
        ns = nanoseconds(within)
        self.log.test(clock.now, f"waits up to {seconds(ns)} for {description}")
        try:
            value = clock.wait_until(condition, ns, description)
        except NeverHappened:
            self.log.test(clock.now, f"{description}: did not happen within {seconds(ns)}")
            raise
        except RunEnded:
            self.log.test(clock.now, f"{description}: the run ended first")
            raise
        self.log.test(clock.now, f"{description}: happened")
        return value

    def _on(self) -> HeldClock:
        if not self.powered:
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
        # Where the run ended, from the runner's stats when it sent them.
        at = self.link.stats.t if self.link.stats is not None else None
        self._hold.end(
            f"{reason}the runner exited with code {code}; its last output:\n{output}", at
        )

    def close(self) -> None:
        """Ends the run (protocol §9): `stop` at the turn the test left it at, on which the
        runner sends its last warnings and its stats, closes the link and exits 0; SIGTERM, once
        the runner has said hello, when it has not exited within EXIT_SECONDS of that; the link
        read to end of file; the runner waited for, and killed only when it has not exited within
        EXIT_SECONDS of the signal. Then stops the forwarding of its log. The forwarder reads the
        log in a busy loop and has nearly always sent the runner's last lines on by then, but it
        is not waited for: they may miss `dut.log`, never the test's result. A runner that exited
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
        self._close_link()
        self._waiter.join()
        # pytest-embedded leaves it unannotated.
        self.process.close()  # type: ignore[no-untyped-call]
        # Nor does it close the stdin pipe it opens, which the runner does not read.
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
            problems.append(f"the runner exited with code {self.process.returncode}")
        if self.link.error is not None:
            problems.append(f"the link failed: {self.link.error}")
        if problems:
            raise BackendError("; ".join(problems) + f"; its output is in {self.dut.logfile}")

    def _write_taps(self) -> list[str]:
        """Writes the recording of every tapped route; returns what could not be written."""
        tapped = [uart for uart in self._uarts.values() if uart.tapped is not None]
        if not tapped:
            return []
        runner = self.link.runner
        tools = {"esp32sim": runner.esp32sim} if runner and runner.esp32sim else {}
        # The app image alone, as the format has it: the header's app_sha256 (plan §4.3).
        app_bin = self.app.flash_args.get("app", {}).get("file")
        try:
            app_sha256 = sha256(Path(self.app.binary_path or "") / app_bin) if app_bin else None
        except OSError:
            app_sha256 = None
        problems = []
        for uart in tapped:
            assert uart.tapped is not None
            try:
                made = uart.tapped.recording(
                    board=self.profile.name,
                    # TODO(#69): a model's source is model:<name> (docs/recording-format.md), not
                    # the kind alone.
                    source=uart.source_kind or NO_SOURCE,
                    clock=self.clock,
                    app_sha256=app_sha256,
                    tools=tools,
                )
                recording.write(uart.tapped.path, made)
            except (OSError, recording.RecordingError) as error:
                # A problem of the test's like the others, so that the link's error, which a
                # recording the format refuses comes with, is not lost.
                problems.append(f"the tap of {uart.name} cannot be written: {error}")
        return problems

    def report(self, run: bool = False) -> str:
        """The link's report, then the bytes on every route and, with `run`, the run on the
        board's clock (`RunLog.lines`): for the report of a test that failed."""
        lines = [self.link.report(), "routes:"]
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
            tap = f"; tapped into {uart.tapped.path}" if uart.tapped is not None else ""
            lines.append(f"  {name}: {source}; {crossed}{tap}")
        if run:
            lines += self.log.lines()
        return "\n".join(lines)

    def properties(self) -> dict[str, object]:
        """The link's `properties()`, and the routes: for each, the kind of source at the far end
        and what it was made from (a replay's recording, as the test named it), the bytes each way
        and the tap's file."""
        routes = {}
        for name, uart in self._uarts.items():
            traffic = self.log.traffic.get(name)
            routes[name] = {
                "source": uart.source_kind,
                "detail": uart.source_detail,
                "bytes_in": traffic.bytes_in if traffic else 0,
                "bytes_out": traffic.bytes_out if traffic else 0,
                "tap": None if uart.tapped is None else str(uart.tapped.path),
            }
        return self.link.properties() | {"routes": routes}

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
