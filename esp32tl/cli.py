"""The `esp32tl` command: `run` and `replay` run an app on the esp32sim backend outside pytest
(plan M1), its console on stdout; `record` comes with M2.

A run is the backend's without pytest-embedded: the runner is spawned with `--board socket:PATH`,
its stdout and stderr are the command's own, and a `BoardLink` serves the socket in a thread. A
replay is given before the runner starts, and the link holds no turn: there is no test body to
wait for, so the clock runs free, and the console goes to stdout only. The profile's lines are on
the link too, with nothing driving them but the board's pulls. The run ends at
`--max-seconds`, or on Ctrl-C, which a SIGINT handler takes for the whole of `run`: the runner
gets one `SIGTERM`, sends its stats and exits (protocol §9), or dies of it before it has
connected; a second Ctrl-C kills it. The runner is in a session of its own, so the terminal's
`SIGINT` reaches this process only, and the runner never gets two signals from one Ctrl-C.
"""

import argparse
import contextlib
import math
import shutil
import signal
import socket
import subprocess
import sys
import threading
from collections.abc import Sequence
from pathlib import Path

from esp32tl import __version__, boards
from esp32tl.backends import esp32sim
from esp32tl.backends.esp32sim import BackendError, Esp32Sim
from esp32tl.boards import DEFAULT_PROFILE, ProfileError
from esp32tl.interfaces.lines import Lines
from esp32tl.interfaces.uart import Uart
from esp32tl.link.boardlink import BoardLink
from esp32tl.recording import RecordingError
from esp32tl.sources.replay import ReplayError

# What the command exits with when it cannot start the run, as the runner does for an input it
# cannot load; and when the user ended the run with Ctrl-C, as a shell reports SIGINT.
CANNOT_START = 2
INTERRUPTED = 128 + signal.SIGINT


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="esp32tl", description="Test unmodified ESP32 firmware through its wires."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    run_parser = commands.add_parser(
        "run",
        help="run an app on the emulator, its console on stdout",
        description="Run an ESP-IDF app on the emulator, its console on stdout.",
    )
    run_arguments(run_parser)
    replay_parser = commands.add_parser(
        "replay",
        help="run an app with a recording replayed into one of its board's interfaces",
        description="Run an ESP-IDF app on the emulator, its console on stdout, with a recording"
        " replayed into one of the board profile's interfaces at its recorded times.",
    )
    run_arguments(replay_parser)
    replay_parser.add_argument(
        "--interface",
        required=True,
        help="the interface to replay into: a UART route of the board profile, as gnss or"
        " uart.gnss",
    )
    replay_parser.add_argument(
        "--recording", required=True, type=Path, help="the recording to replay"
    )
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    replays: dict[str, Path] = {}
    if args.command == "replay":
        interface = args.interface if "." in args.interface else f"uart.{args.interface}"
        replays[interface] = args.recording
    return run(args.app, args.board, args.max_seconds, replays)


def run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--app",
        required=True,
        type=Path,
        help="the ESP-IDF app directory; its build is in APP/build",
    )
    parser.add_argument(
        "--board",
        default=DEFAULT_PROFILE,
        help=f"the board profile (default {DEFAULT_PROFILE})",
    )
    parser.add_argument(
        "--max-seconds",
        type=positive,
        default=esp32sim.MAX_SECONDS,
        metavar="S",
        help=f"end the run after S emulated seconds (default {esp32sim.MAX_SECONDS})",
    )


def positive(text: str) -> float:
    """A finite number of seconds above 0, as the runner's --max-seconds takes."""
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not (math.isfinite(value) and value > 0):
        raise argparse.ArgumentTypeError(f"{text!r} is not a number of seconds above 0")
    return value


def run(app: Path, board: str, max_seconds: float, replays: dict[str, Path]) -> int:
    """Runs `app` on the board profile `board`, each recording of `replays` replayed into its
    interface. Returns the exit code: the runner's, 1 when the link failed, CANNOT_START when
    the run could not start and INTERRUPTED after Ctrl-C. Ctrl-C is handled from the start: one
    before the runner starts keeps it from starting."""
    interrupts = Interrupts()
    previous = signal.signal(signal.SIGINT, interrupts.handle)
    try:
        return interruptible(app, board, max_seconds, replays, interrupts)
    finally:
        signal.signal(signal.SIGINT, previous)


def interruptible(
    app: Path, board: str, max_seconds: float, replays: dict[str, Path], interrupts: "Interrupts"
) -> int:
    try:
        profile = boards.profile(board)
        uarts: list[Uart] = []
        for interface, path in replays.items():
            uart = esp32sim.profile_uart(
                profile, interface, clock=Esp32Sim.clock, directory=Path.cwd()
            )
            uart.replay(path)
            uarts.append(uart)
        image = esp32sim.load_app(str(app.resolve()))
        runner, rom = esp32sim.runner_path(), esp32sim.rom_path()
    except (ProfileError, BackendError, RecordingError, ReplayError) as error:
        print(f"esp32tl: {error}", file=sys.stderr)
        return CANNOT_START
    if interrupts.count:
        return INTERRUPTED
    try:
        listener, link_path = esp32sim.listen()
    except BackendError as error:
        print(f"esp32tl: {error}", file=sys.stderr)
        return CANNOT_START
    try:
        command = esp32sim.runner_command(
            runner, rom, image, max_seconds, link_path, psram_mb=profile.psram_mb
        )
        try:
            process = subprocess.Popen(command, start_new_session=True)
        except OSError as error:
            print(f"esp32tl: cannot start {command[0]}: {error.strerror}", file=sys.stderr)
            return CANNOT_START
        served = Run(process, listener)
        interrupts.attach(served)
        # The board's pulls, as on the backend's link: no test drives the lines, so their time is
        # never asked for.
        lines = Lines(profile.name, profile.lines, lambda: 0)
        link = BoardLink(
            profile.name,
            [*uarts, lines],
            chip=profile.chip,
            on_error=lambda reason: served.terminate(),
            clock=Esp32Sim.clock,
        )
        code = served.until_exit(link)
    finally:
        listener.close()
        shutil.rmtree(link_path.parent, ignore_errors=True)
    for line in link.report().splitlines():
        print(f"esp32tl: {line}", file=sys.stderr)
    if interrupts.count:
        return INTERRUPTED
    if code < 0:
        # Killed by a signal: what a shell would report.
        return 128 - code
    if code != 0:
        return code
    # The report's error line says why.
    return 0 if link.error is None else 1


class Interrupts:
    """Ctrl-C during a run, counted by the SIGINT handler: the first ends the run with one
    SIGTERM, the second kills the runner."""

    def __init__(self) -> None:
        self.count = 0
        self._run: Run | None = None

    def handle(self, signum: int, frame: object) -> None:
        self.count += 1
        if self._run is not None:
            self._run.interrupt(self.count)

    def attach(self, run: "Run") -> None:
        """From now on, Ctrl-C goes to `run`; one that came before it does now."""
        self._run = run
        if self.count:
            run.interrupt(self.count)


class Run:
    """The runner `process`, whose one connection `listener` takes."""

    def __init__(self, process: subprocess.Popen[bytes], listener: socket.socket) -> None:
        self.process = process
        self.listener = listener
        self._conn: socket.socket | None = None
        self._exited = threading.Event()
        # One SIGTERM at most: a second one kills the runner, with 128 + 15 (protocol §9).
        # Reentrant: the SIGINT handler can run in the main thread while it holds the lock.
        self._terminated = False
        self._terminating = threading.RLock()

    def terminate(self) -> None:
        """Sends the runner SIGTERM, once, unless it has exited. From any thread."""
        with self._terminating:
            if not self._terminated and not self._exited.is_set():
                self._terminated = True
                self.process.send_signal(signal.SIGTERM)

    def interrupt(self, count: int) -> None:
        """The `count`th Ctrl-C: the first ends the run, the next ones kill the runner."""
        if count == 1:
            self.terminate()
        elif not self._exited.is_set():
            self.process.kill()

    def until_exit(self, link: BoardLink) -> int:
        """Serves `link` until the runner exits. Returns its exit code."""
        serving = threading.Thread(target=self._serve, args=(link,), daemon=True)
        serving.start()
        code = self.process.wait()
        self._exited.set()
        # A runner that is gone has closed its end of the link; were the thread still reading,
        # the shutdown ends the read.
        serving.join(esp32sim.EXIT_SECONDS)
        if serving.is_alive() and self._conn is not None:
            with contextlib.suppress(OSError):
                self._conn.shutdown(socket.SHUT_RDWR)
            serving.join()
        if self._conn is not None:
            self._conn.close()
        return code

    def _serve(self, link: BoardLink) -> None:
        """Accepts the runner's one connection and reads it to end of file. Gives up on a runner
        that exits without connecting, as one does on a usage error (protocol §9)."""
        self.listener.settimeout(0.05)
        while True:
            # Read before the accept: a runner that connected and then exited is accepted still.
            exited = self._exited.is_set()
            try:
                conn, _ = self.listener.accept()
            except TimeoutError:
                if exited:
                    return
                continue
            break
        conn.settimeout(None)
        self._conn = conn
        link.run(conn)
