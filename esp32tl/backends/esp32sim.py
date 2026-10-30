"""The `esp32sim` backend: the image runs in the runner crate, on the emulated clock (plan §3).

pytest-embedded runs no service for it, so its `dut` is the base `Dut`. The runner is spawned as a
`DuplicateStdoutPopen` on the dut's `msg_queue`, whose listener pumps the runner's output into
`pexpect_proc`, where `dut.expect` reads it: the shape of `pytest-embedded-qemu`. The runner's own
lines (on stderr, starting `esp32tl-runner:`) reach the same stream, because `DuplicateStdoutPopen`
writes stdout and stderr into one log.
"""

import os
import subprocess
from pathlib import Path

from pytest_embedded.dut import Dut
from pytest_embedded.log import DuplicateStdoutPopen, MessageQueue
from pytest_embedded.utils import Meta
from pytest_embedded_idf.app import IdfApp

# The checkout this package runs from: `make setup` builds the runner and fetches the ROM there.
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


def runner_command(runner: Path, rom: Path, app: IdfApp, max_seconds: float) -> list[str]:
    """The runner's command line: every file of `flasher_args.json` at its offset."""
    command = [str(runner), "--chip", "esp32", "--rom", str(rom), "--max-seconds", str(max_seconds)]
    for file in app.flash_files:
        command += ["--flash-at", f"{file.offset:#x}={file.file_path}"]
    return command


class Esp32Sim:
    """A run of the app in `app_path` in the runner, its console going to `dut`."""

    name = "esp32sim"

    def __init__(self, dut: Dut, app_path: str, msg_queue: MessageQueue, meta: Meta) -> None:
        self.dut = dut
        self.app = load_app(app_path)
        command = runner_command(runner_path(), rom_path(), self.app, MAX_SECONDS)
        self.process = DuplicateStdoutPopen(msg_queue, command, meta)

    def close(self) -> None:
        # Closes the log and its forwarding process too; pytest-embedded leaves it unannotated.
        self.process.terminate()  # type: ignore[no-untyped-call]
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
