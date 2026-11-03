"""The board profile's SD card slot, `sd_card`: what is in it. The `sd_card` fixture is it.

On the `esp32sim` backend the card is a model in the runner, holding an image file: an SD card in
SPI mode on the slot's pins, which writes what the firmware writes into the file as it goes. The
test puts a card in before power-on, and may take it out and put one in while the firmware runs.
The image builder and reader use mtools; external image paths remain supported.
"""

import errno
import os
import shutil
import subprocess
import threading
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile, TemporaryDirectory, mkdtemp

from esp32tl.boards import SdCardRoute
from esp32tl.boards.capabilities import Request
from esp32tl.interfaces import SourceError
from esp32tl.interfaces.uart import same_file
from esp32tl.link.boardlink import Interface, LinkError
from esp32tl.link.protocol import Input, SdCardChange
from esp32tl.report import RunLog

# The capacity unit of a high capacity card (CSD C_SIZE): an image is a whole number of them.
UNIT = 512 * 1024
# The most a card holds: 2^22 units (CSD C_SIZE).
MAX_SIZE = UNIT << 22

MTOOLS = ("mformat", "mcopy", "mmd", "mtype", "mdir")


def _mtools(command: str, *args: str) -> bytes:
    try:
        result = subprocess.run(
            [command, *args],
            capture_output=True,
            check=True,
            env={**os.environ, "MTOOLS_SKIP_CHECK": "1"},
        )
    except FileNotFoundError:
        raise SourceError(f"sd_card: {command} is missing; install mtools") from None
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode("utf-8", errors="replace").strip()
        raise SourceError(f"sd_card: {command} failed: {detail}") from None
    return result.stdout


def _path(name: str) -> PurePosixPath:
    """Only relative FAT paths, without traversal, wildcards or drive syntax."""
    if not isinstance(name, str) or not name:
        raise ValueError("card file paths must be non-empty relative strings")
    parts = name.split("/")
    if any(
        part in ("", ".", "..")
        or part.endswith((".", " "))
        or any(ord(char) < 32 or char in '<>:"\\|?*[]' for char in part)
        for part in parts
    ):
        raise ValueError(f"invalid card file path: {name!r}")
    return PurePosixPath(name)


@dataclass(frozen=True)
class CardImage(os.PathLike[str]):
    """A built FAT image, valid until its board closes. Reads reopen the current image.

    The test must wait for firmware to finish/close its writes before reading.
    This object never mounts the image or caches FAT metadata between calls.
    """

    path: Path
    _check: Callable[[], None]

    def __fspath__(self) -> str:
        self._check()
        return str(self.path)

    def read_bytes(self, name: str) -> bytes:
        self._check()
        path, is_directory = self._resolve(name)
        if is_directory:
            raise IsADirectoryError(errno.EISDIR, "card path is a directory", name)
        return _mtools("mtype", "-i", str(self.path), f"::{path}")

    def read_text(self, name: str, *, encoding: str = "utf-8") -> str:
        """Decode exact file bytes, preserving CRLF and other line endings."""
        return self.read_bytes(name).decode(encoding)

    def list_files(self, directory: str = "") -> list[str]:
        """Sorted immediate filenames in a directory; excludes subdirectories."""
        self._check()
        path = PurePosixPath(".")
        if directory:
            path, is_directory = self._resolve(directory)
            if not is_directory:
                raise NotADirectoryError(errno.ENOTDIR, "card path is not a directory", directory)
        return sorted(name for name, is_directory in self._entries(path) if not is_directory)

    def _entries(self, directory: PurePosixPath) -> list[tuple[str, bool]]:
        # Read actual directory entries instead of interpreting locale-dependent tool errors.
        target = "" if directory == PurePosixPath(".") else f"{directory}/"
        entries = _mtools("mdir", "-b", "-i", str(self.path), f"::/{target}")
        return [
            (PurePosixPath(entry).name, entry.endswith("/"))
            for entry in entries.decode("utf-8").splitlines()
        ]

    def _resolve(self, name: str) -> tuple[PurePosixPath, bool]:
        parts = _path(name).parts
        parent = PurePosixPath(".")
        for index, part in enumerate(parts):
            entry = next(
                (
                    entry
                    for entry in self._entries(parent)
                    if entry[0].casefold() == part.casefold()
                ),
                None,
            )
            if entry is None:
                raise FileNotFoundError(errno.ENOENT, "no such file or directory on card", name)
            actual_name, is_directory = entry
            parent /= actual_name
            if index < len(parts) - 1 and not is_directory:
                raise NotADirectoryError(errno.ENOTDIR, "card path is not a directory", str(parent))
        return parent, is_directory


class CardImageBuilder:
    """Declare initial files; build creates a fresh image without inserting it."""

    def __init__(self, slot: "SdCard", size_mb: int) -> None:
        if type(size_mb) is not int or not 1 <= size_mb <= 2048:
            raise ValueError("size_mb must be a whole number from 1 to 2048 (MiB)")
        self._slot = slot
        self._size_mb = size_mb
        self._files: dict[PurePosixPath, bytes] = {}

    def with_text(self, name: str, contents: str, *, encoding: str = "utf-8") -> "CardImageBuilder":
        return self.with_bytes(name, contents.encode(encoding))

    def with_bytes(self, name: str, contents: bytes) -> "CardImageBuilder":
        path = _path(name)
        if not isinstance(contents, bytes):
            raise TypeError("with_bytes contents must be bytes")
        key = str(path).casefold()
        for existing in self._files:
            other = str(existing).casefold()
            if key == other or key.startswith(other + "/") or other.startswith(key + "/"):
                raise ValueError(f"card file path conflicts with {str(existing)!r}: {name!r}")
        self._files[path] = contents
        return self

    def build(self) -> CardImage:
        self._slot._can_build()
        missing = [command for command in MTOOLS if shutil.which(command) is None]
        if missing:
            raise SourceError(f"sd_card: missing {', '.join(missing)}; install mtools")
        directory = self._slot._image_directory()
        path = directory / "card.img"
        try:
            with path.open("wb") as image:
                image.truncate(self._size_mb << 20)
            fat32 = ("-F",) if self._size_mb >= 512 else ()
            _mtools(
                "mformat",
                "-i",
                str(path),
                *fat32,
                "-T",
                str(self._size_mb << 11),
                "-h",
                "64",
                "-s",
                "32",
                "::",
            )
            directories: set[str] = set()
            for name, contents in self._files.items():
                for parent in reversed(name.parents):
                    if parent == PurePosixPath(".") or str(parent).casefold() in directories:
                        continue
                    _mtools("mmd", "-i", str(path), f"::{parent}")
                    directories.add(str(parent).casefold())
                with NamedTemporaryFile(dir=directory) as source:
                    source.write(contents)
                    source.flush()
                    _mtools("mcopy", "-i", str(path), source.name, f"::{name}")
        except BaseException:
            shutil.rmtree(directory)
            raise
        return CardImage(path, self._slot._check_open)


@dataclass(frozen=True)
class CardChange:
    """What the slot held from `t`, once the board was on: the card with the image `image`, or
    no card (`None`), as the runner acknowledged it."""

    t: int
    image: Path | None


class SdCard(Interface):
    """The slot on `route`, empty until the test inserts a card.

    Before the first `board.power_on()`, `insert` puts a card in the slot from power-on and
    `remove` takes it out again. After it, both act at the board's current instant (`now`): the link
    sends them in its next turn (`sd_card`), the runner applies them at the first slice boundary
    at or after that instant, between two of the chip's SPI transactions, and says what the slot
    holds then (`sd_card_ack`), which `changes` keeps. `started` says whether the run has started:
    with the board's supply off after that, the card still goes through the link.

    `asked` is called with the `Request` of a `model` source before a card goes in or comes out,
    and refuses it by raising: the backend's checks, that a backend gives that source. A relative
    image path is taken from `directory`, the test file's. `log`, when given, gets the test's
    `insert` and `remove` as steps. `files` holds the files the run reads and writes, shared with
    the routes, so that no tap writes over the card's image and the card writes over no
    recording.
    """

    name = "sd_card"

    def __init__(
        self,
        route: SdCardRoute,
        *,
        directory: Path,
        asked: Callable[[Request], None] = lambda request: None,
        log: RunLog | None = None,
        files: dict[Path, tuple[str, str]] | None = None,
        now: Callable[[], int] = lambda: 0,
        started: Callable[[], bool] = lambda: False,
    ) -> None:
        super().__init__(self.name, None)
        self.route = route
        self.directory = directory
        self.asked = asked
        self.log = log
        self.files = {} if files is None else files
        self._now = now
        self._started = started
        # The image of the card in the slot, absolute, as the test last put it; None while the
        # slot is empty.
        self.image_path: Path | None = None
        self._temporary: TemporaryDirectory[str] | None = None
        self._closed = False
        self._lock = threading.Lock()
        # The changes the test made with the board on: not sent yet, then not acknowledged yet.
        self._pending: list[SdCardChange] = []
        self._sent: deque[SdCardChange] = deque()
        self._changes: list[CardChange] = []
        self._refused: list[CardChange] = []
        # The image of the card in the slot at power-on, kept by the first change made with the
        # board on; empty until then.
        self._at_power_on: list[Path | None] = []

    @property
    def card_slot(self) -> bool:
        return True

    def image(self, *, size_mb: int = 32) -> CardImageBuilder:
        """Start a FAT image builder. Building and insertion are separate operations."""
        self._can_build()
        return CardImageBuilder(self, size_mb)

    def _check_open(self) -> None:
        if self._closed:
            raise SourceError("sd_card: the board is closed")

    def _can_build(self) -> None:
        self._check_open()
        self.asked(Request(self.name, source="model"))

    def _image_directory(self) -> Path:
        if self._temporary is None:
            self._temporary = TemporaryDirectory(prefix="esp32tl-sd-")
        return Path(mkdtemp(dir=self._temporary.name)).resolve()

    def close(self) -> None:
        """Remove only builder-owned images, after the backend has stopped accessing them."""
        self._closed = True
        if self._temporary is not None:
            self._temporary.cleanup()

    def insert(self, image: str | os.PathLike[str]) -> Path:
        """Puts a card holding the image file `image` in the empty slot: from power-on before
        `board.power_on()`, from the board's current instant after it. The firmware's writes go
        into the file, which must be readable and writable. The image is a whole number of 512
        KiB units, from one to 2 TiB; its contents are the test's, a FAT file system or anything
        else. Returns the image's absolute path."""
        self._check_open()
        if self.image_path is not None:
            raise SourceError(f"{self.name} has a card already: remove it first")
        self.asked(Request(self.name, source="model"))
        path = (self.directory / image).resolve()
        try:
            size = path.stat().st_size
        except OSError as error:
            raise SourceError(f"{self.name}: no image at {path}: {error.strerror}") from None
        if not path.is_file():
            raise SourceError(f"{self.name}: {path} is not a file")
        if size == 0 or size % UNIT:
            raise SourceError(
                f"{self.name}: {path} has {size} bytes; an SD card image is a whole number of"
                " 512 KiB units"
            )
        if size > MAX_SIZE:
            raise SourceError(
                f"{self.name}: {path} has {size} bytes; an SD card image is at most 2 TiB"
            )
        # The card writes into it, and the runner opens it for writing when it goes in.
        if not os.access(path, os.R_OK | os.W_OK):
            raise SourceError(
                f"{self.name}: {path} cannot be read and written: the card writes into it"
            )
        for claimed, (kind, interface) in self.files.items():
            if same_file(path, claimed):
                raise SourceError(
                    f"{self.name}: cannot hold {path}: it is the {kind} of {interface}"
                )
        self.files[path] = ("card", self.name)
        t = self._change(None, str(path))
        self.image_path = path
        if self.log is not None:
            self.log.test(t, f"{self.name}.insert({os.fspath(image)!r})")
        return path

    def remove(self) -> Path:
        """Takes the card out of the slot: before `board.power_on()`, so that the slot is empty
        at power-on; after it, from the board's current instant. The firmware sees no answer
        from the slot from then on. What the card had written stays in its image; what it had
        not finished, it never writes. Returns the image's absolute path."""
        self._check_open()
        # Asked first: on a backend that has no card model the call skips, whatever is in the slot.
        self.asked(Request(self.name, source="model"))
        path = self.image_path
        if path is None:
            raise SourceError(f"{self.name} is empty: there is no card to remove")
        self.files.pop(path, None)
        t = self._change(path, None)
        self.image_path = None
        if self.log is not None:
            self.log.test(t, f"{self.name}.remove()")
        return path

    def _change(self, before: Path | None, image: str | None) -> int:
        """Queues the change from `before` to `image` for the link once the run has started; its
        instant, 0 before."""
        if not self._started():
            return 0
        t = self._now()
        with self._lock:
            if not self._at_power_on:
                self._at_power_on.append(before)
            self._pending.append(SdCardChange(t, image))
        return t

    @property
    def held(self) -> Path | None:
        """The image of the card in the slot as the run has it: after the last change the runner
        acknowledged, else from power-on. A change the run has not reached does not count."""
        with self._lock:
            if self._changes:
                return self._changes[-1].image
            if self._at_power_on:
                return self._at_power_on[0]
        return self.image_path

    @property
    def not_reached(self) -> list[CardChange]:
        """The changes the test made with the board on that the runner has not acknowledged, in
        order, at the instants the test made them: those the run ended before, once it has."""
        with self._lock:
            unacknowledged = [*self._sent, *self._pending]
        return [_card_change(change.t, change.image) for change in unacknowledged]

    @property
    def refused(self) -> list[CardChange]:
        """The changes the runner refused, at the instants it did, each with the image the test
        asked for: a refusal fails the link, so there is one at most."""
        with self._lock:
            return list(self._refused)

    @property
    def changes(self) -> list[CardChange]:
        """What the slot held after each change the test made with the board on, as the runner
        acknowledged it, in order: a change the run has not reached yet is not there."""
        with self._lock:
            return list(self._changes)

    # The link's side.

    def inputs(self, now: int) -> Iterable[Input]:
        with self._lock:
            pending, self._pending = self._pending, []
            sent = [SdCardChange(max(change.t, now), change.image) for change in pending]
            self._sent.extend(sent)
        return sent

    def card_changed(self, t: int, image: str | None, error: str | None) -> None:
        with self._lock:
            change = self._sent.popleft() if self._sent else None
        if change is None:
            raise LinkError(f"{self.name}: the runner acknowledged a change nobody asked for")
        if error is not None:
            with self._lock:
                self._refused.append(_card_change(t, change.image))
            raise LinkError(f"{self.name}: the runner refused the card at {t} ns: {error}")
        # What the runner says the slot holds, also when it is not what the test asked for.
        with self._lock:
            self._changes.append(_card_change(t, image))
        if image != change.image:
            wanted = "no card" if change.image is None else change.image
            raise LinkError(
                f"{self.name}: the runner says the slot holds {image or 'no card'} at {t} ns,"
                f" where the test asked for {wanted}"
            )


def _card_change(t: int, image: str | None) -> CardChange:
    return CardChange(t, None if image is None else Path(image))
