"""The `esp32sim` backend: the image runs in the runner crate, on the emulated clock (plan §3).

pytest-embedded runs no service for it, so its `dut` is the base `Dut`. The runner is spawned as a
`DuplicateStdoutPopen` on the dut's `msg_queue`, whose listener pumps the runner's output into
`pexpect_proc`, where `dut.expect` reads it: the shape of `pytest-embedded-qemu`. The runner's own
lines (on stderr, starting `esp32tl-runner:`) reach the same stream, because `DuplicateStdoutPopen`
writes stdout and stderr into one log.

That log is a file, so `dut.expect` never sees the end of it: on its own, an expect after the
runner has exited waits out its whole timeout. When the runner exits, the backend appends an
end-of-run line to the log, behind everything the runner wrote, and the dut's `pexpect_proc` ends
its stream once it has read that line: the expect fails at once, with pexpect's `EOF`.

The backend owns the link's socket (`docs/protocol.md` §1): it binds a Unix socket before it
spawns the runner, passes it as `--board socket:PATH`, and serves the one connection with a
`BoardLink` in a thread of its own. It also owns the end of the run (§9), before the dut is torn
down: `SIGTERM`, the link read to end of file, the runner waited for, `SIGKILL` only as a last
resort.
"""

import contextlib
import os
import re
import shutil
import signal
import socket
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import pexpect
from pytest_embedded.dut import Dut
from pytest_embedded.log import DuplicateStdoutPopen, MessageQueue, PexpectProcess
from pytest_embedded.utils import Meta, remove_asci_color_code
from pytest_embedded_idf.app import IdfApp

from esp32tl.link.boardlink import BoardLink, Interface

# The checkout this package runs from, with an editable install (`make setup` makes one): the
# runner and the ROM are looked for there. From a wheel this is site-packages, and the messages
# below point to ESP32TL_RUNNER and ESP32TL_ROM; prebuilt runners come with milestone 10.
ROOT = Path(__file__).resolve().parents[2]

# The ROM ELF scripts/fetch-rom-elf puts in .cache/ (plan M0). The runner's own lookup prefers the
# newest release under ~/.espressif instead, so the backend always passes --rom.
ROM_RELEASE = "20241011"
ROM_ELF = "esp32_rev300_rom.elf"

# How long a run lasts, in emulated seconds, unless the test ends it first: the `board` fixture
# stops the runner at teardown. A test that waits for anything later fails. It also bounds a runner
# left behind by a harness that died: hello_world runs 12 emulated seconds in 0.3 s of wall time
# on an M2 Pro, so 300 is a few seconds there (an image that never idles runs slower).
MAX_SECONDS = 300

# What the backend appends to the runner's log when the runner exits, with its exit code.
END_OF_RUN = re.compile(rb"\nesp32tl: the runner exited with code (-?\d+)\n")

# The board profile the harness's hello names, until board profiles come (#13).
BOARD = "esp32-devkitc"

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
    runner: Path, rom: Path, app: IdfApp, max_seconds: float, link: Path
) -> list[str]:
    """The runner's command line: every file of `flasher_args.json` at its offset, its board the
    socket at `link`."""
    command = [str(runner), "--chip", "esp32", "--rom", str(rom), "--max-seconds", str(max_seconds)]
    command += ["--board", f"socket:{link}"]
    for file in app.flash_files:
        command += ["--flash-at", f"{file.offset:#x}={file.file_path}"]
    return command


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
    """A run of the app in `app_path` in the runner, its console going to `dut`, its board the
    link to `interfaces`.

    `app` is the `IdfApp` the runner flashes. The dut's own `app` is pytest-embedded's base `App`,
    without `flash_files` or `target`: without a service, pytest-embedded builds nothing more.
    `link` is the `BoardLink`, whose warnings, stats and error are complete once `close` returns.
    """

    name = "esp32sim"

    def __init__(
        self,
        dut: Dut,
        app_path: str,
        msg_queue: MessageQueue,
        meta: Meta,
        interfaces: Sequence[Interface] = (),
    ) -> None:
        self.dut = dut
        self.app = load_app(app_path)
        self.link = BoardLink(BOARD, interfaces, on_error=self._end_run)
        runner, rom = runner_path(), rom_path()
        # Bound before the runner starts, which connects once (protocol §1).
        self._listener, self._socket = listen()
        self._conn: socket.socket | None = None
        self._exited = threading.Event()
        # One SIGTERM at most: a second one kills the runner, with 128 + 15 (protocol §9).
        self._terminated = False
        self._terminating = threading.Lock()
        command = runner_command(runner, rom, self.app, MAX_SECONDS, self._socket)
        try:
            self.process = RunnerPopen(msg_queue, command, meta)
        except OSError as error:
            self._close_link()
            raise BackendError(f"cannot start {command[0]}: {error.strerror}") from error
        end_stream_at_end_of_run(dut.pexpect_proc, lambda: self.link.error)
        self._linker = threading.Thread(target=self._serve_link, daemon=True)
        self._linker.start()
        # The only thread that waits for the runner: Popen.poll() from another thread can miss an
        # exit while this one is in waitpid().
        self._waiter = threading.Thread(target=self._mark_end_of_run, daemon=True)
        self._waiter.start()

    def _end_run(self, reason: str) -> None:
        """Ends the run as soon as the harness finds the link wrong, instead of at the runner's
        next turn, which may come only at --max-seconds: an expect that is waiting then fails
        on the end of the run, with the reason ahead of it in the log."""
        self._terminate()

    def _terminate(self) -> None:
        """Sends the runner SIGTERM, once, unless it has exited. Not
        DuplicateStdoutPopen.terminate(), which closes the log before the signal."""
        with self._terminating:
            if not self._terminated and not self._exited.is_set():
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
        code = self.process.wait()
        self._exited.set()
        # The runner's exit ends the link; the link's last messages are read first, so that what
        # the harness found wrong is in the log ahead of the end, where an expect's EOF shows it.
        self._linker.join(EXIT_SECONDS)
        # Appended through a file of its own: the runner's log forwarder sends it on in order.
        with open(self.process._logfile, "a") as log:
            if self.link.error is not None:
                log.write(f"\nesp32tl: the link failed: {self.link.error}\n")
            log.write(f"\nesp32tl: the runner exited with code {code}\n")

    def close(self) -> None:
        """Ends the run (protocol §9): SIGTERM once the runner has said hello, on which it sends
        its last warnings and its stats and closes the link; the link read to end of file; the
        runner waited for, and killed only when it has not exited within EXIT_SECONDS. Then stops
        the forwarding of its log. The forwarder reads the log in a busy loop and has nearly
        always sent the runner's last lines on by then, but it is not waited for: they may miss
        `dut.log`, never the test's result. A runner that exited with an error, one that had to
        be killed, and a link the harness found wrong fail the test, even one whose expects all
        passed before it."""
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
        problems = []
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


class RunnerExited(pexpect.EOF):
    """The EOF that ends the stream at the end of the run. pexpect raises an EOF of its own from
    it, and pytest-embedded its "Not found" one from that, so it is found in the chain:
    `ended_on_runner_exit`."""


def ended_on_runner_exit(error: BaseException) -> bool:
    """Whether `error` came from an expect that read the end of the run."""
    cause: BaseException | None = error
    # A __cause__ set by hand can make a cycle, as the traceback module knows.
    seen: set[int] = set()
    while cause is not None and id(cause) not in seen:
        if isinstance(cause, RunnerExited):
            return True
        seen.add(id(cause))
        cause = cause.__cause__ or cause.__context__
    return False


# How much of the runner's last output the EOF of an expect carries: pytest-embedded's own message
# shows the pexpect buffer, which pexpect empties at EOF.
EOF_TAIL_BYTES = 2048


def end_stream_at_end_of_run(
    proc: PexpectProcess, link_error: Callable[[], str | None] = lambda: None
) -> None:
    """Makes `proc` raise `RunnerExited` once it has read the end-of-run line, instead of
    reading an unchanging file until the expect times out. The EOF carries the runner's exit
    code and the end of its output, whole lines without colour codes, and first what
    `link_error` says the harness found wrong. That comes from the link, not from the log, where
    the guest's own output could cut or imitate it. The backend writes the end-of-run line once
    the link has been read to its end, which it waits up to EXIT_SECONDS for, so it is final by
    then unless something else holds the link open; what is found later the teardown reports."""
    read = proc.read_nonblocking
    seen = b""
    cut = False
    ended: str | None = None

    def read_nonblocking(size: int = 1, timeout: int = -1) -> bytes:
        nonlocal seen, cut, ended
        if ended is not None:
            raise RunnerExited(ended)
        data: bytes = read(size, timeout)
        seen += data
        if len(seen) > EOF_TAIL_BYTES:
            seen, cut = seen[-EOF_TAIL_BYTES:], True
        end = END_OF_RUN.search(seen)
        if end is not None:
            tail = seen[: end.start()]
            # From the first whole line, if one follows the cut one: better a line cut at its start
            # than no output at all, which a single long line ending in \r\n would leave.
            if cut:
                rest = tail.partition(b"\n")[2]
                if rest.strip():
                    tail = rest
            # The harness's reason first: when it ended the run, the runner exited 0.
            error = link_error()
            reason = f"the link failed: {error}; " if error is not None else ""
            ended = (
                f"{reason}the runner exited with code {end[1].decode()}; its last output:\n"
                + remove_asci_color_code(tail)
            )
        return data

    proc.read_nonblocking = read_nonblocking  # type: ignore[method-assign]
