"""The board profile's SD card slot, `sd_card`: what is in it. The `sd_card` fixture is it.

On the `esp32sim` backend the card is a model in the runner, holding an image file: an SD card in
SPI mode on the slot's pins, which writes what the firmware writes into the file as it goes. The
image builder and reader use mtools; external image paths remain supported.
"""

import errno
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile, TemporaryDirectory, mkdtemp

from esp32tl.boards import SdCardRoute
from esp32tl.boards.capabilities import Request
from esp32tl.interfaces import SourceError
from esp32tl.interfaces.uart import same_file
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


class SdCard:
    """The slot on `route`, empty until the test inserts a card.

    `asked` is called with the `Request` of a `model` source before the card goes in,
    and refuses it by raising: the backend's checks, that the board is still off and that a
    backend gives that source. A relative image path is taken from `directory`, the test file's.
    `log`, when given, gets the test's `insert` as a step, at 0: it comes before power-on.
    `files` holds the files the run reads and writes, shared with the routes, so that no tap
    writes over the card's image and the card writes over no recording.
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
    ) -> None:
        self.route = route
        self.directory = directory
        self.asked = asked
        self.log = log
        self.files = {} if files is None else files
        # The image of the card in the slot, absolute; None while the slot is empty.
        self.image_path: Path | None = None
        self._temporary: TemporaryDirectory[str] | None = None
        self._closed = False

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
        """Puts a card holding the image file `image` in the slot, before `board.power_on()`.
        The firmware's writes go into the file, which must be readable and writable. The image is
        a whole number of 512 KiB units, from one to 2 TiB; its contents are the test's, a FAT
        file system or anything else. Returns the image's absolute path."""
        self._check_open()
        if self.image_path is not None:
            raise SourceError(f"{self.name} has a card already: one per test")
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
        # The card writes into it, and the runner opens it for writing at power-on.
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
        self.image_path = path
        if self.log is not None:
            self.log.test(0, f"{self.name}.insert({os.fspath(image)!r})")
        return path
