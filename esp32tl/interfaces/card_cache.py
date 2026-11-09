"""The `card_cache` fixture: SD cards a test prepared, kept for the tests after it.

Firmware that prepares a card on its first mount does it once, and later tests take a clone of the
result. A card is kept for an application: its key is the test's name for it and the hash of the
app image, so a new build starts again. What a card holds on disk is bounded by a limit, counted
from what the files have allocated, and the card least recently used goes first.

The test chooses to take a clone: the image in the slot still writes through, and what the firmware
writes reaches that clone only. A clone shares the file system's blocks where it can, and otherwise
is a copy that leaves holes for zeros, because a card is mostly never written.
"""

import ctypes
import errno
import fcntl
import hashlib
import os
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from esp32tl.interfaces import SourceError
from esp32tl.interfaces.board import Board
from esp32tl.interfaces.sd_card import CardImage
from esp32tl.interfaces.uart import same_file

# The default of `esp32tl_card_cache_mib`: the tool's choice, for a few large cards.
CARD_CACHE_MIB = 4096

# ioctl_ficlone(2): _IOW(0x94, 9, int).
FICLONE = 0x40049409
# What the sparse copy looks at, at a time, for zeros.
CHUNK = 64 * 1024


def allocated(path: Path) -> int:
    """The bytes the file has on disk: `st_blocks` counts in 512-byte units on macOS and Linux."""
    return path.stat().st_blocks * 512


def _share_blocks(source: Path, target: Path) -> bool:
    """Makes `target` a clone of `source` that shares their blocks until either is written; false
    if that fails: file systems refuse it in several ways, and the copy after this one reports a
    failure that is not about cloning."""
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None)
        return bool(libc.clonefile(os.fsencode(source), os.fsencode(target), 0) == 0)
    with source.open("rb") as origin, target.open("xb") as clone:
        try:
            fcntl.ioctl(clone.fileno(), FICLONE, origin.fileno())
        except OSError:
            target.unlink()
            return False
    return True


def _sparse_copy(source: Path, target: Path) -> None:
    """Copies `source` to the new file `target` without what reads as zeros: the holes of `source`
    are never read, and a chunk of its data that is all zeros stays a hole."""
    with source.open("rb", buffering=0) as origin, target.open("xb") as copy:
        size = os.fstat(origin.fileno()).st_size
        at = 0
        while at < size:
            try:
                at = origin.seek(at, os.SEEK_DATA)
            except OSError as error:
                if error.errno == errno.ENXIO:  # nothing but a hole from here on
                    break
                raise
            end = origin.seek(at, os.SEEK_HOLE)
            origin.seek(at)
            while at < end:
                chunk = origin.read(min(CHUNK, end - at))
                if not chunk:
                    break
                if chunk.count(0) != len(chunk):
                    copy.seek(at)
                    copy.write(chunk)
                at += len(chunk)
            at = end
        copy.truncate(size)


def clone(source: Path, target: Path) -> None:
    """A file at the new path `target` that reads as `source`, and that the test may write without
    `source` seeing it, nor the other way round."""
    if not _share_blocks(source, target):
        _sparse_copy(source, target)
    # A clone takes the mode of its source, and the card writes into what comes out of the cache.
    target.chmod(0o600)


def _sync(path: Path) -> None:
    with path.open("rb") as file:
        os.fsync(file.fileno())


def _touch(path: Path) -> None:
    # Last use is in the modification time: a volume may not keep access times.
    now = time.time_ns()
    os.utime(path, ns=(now, now))


class CardCache:
    """The cards in `directory`, which take at most `limit` bytes of disk. `board` is the board of
    the running test: the fixture is the session's, and a card belongs to the test's application
    and goes into the test's directory.

    The lock is that of whole operations: pytest-xdist's workers, or another session, may share
    the directory, and a card must not go while it is being cloned.
    """

    def __init__(self, directory: Path, limit: int, board: Callable[[], Board | None]) -> None:
        self.directory = directory
        self.limit = limit
        self._board = board

    def get(self, name: str) -> CardImage | None:
        """A clone of the card kept as `name` for this application, in the test's directory and
        gone with its board, or `None`: never kept, evicted, or kept for another build."""
        board, entry = self._find(name)
        try:
            with self._locked():
                if not entry.exists():
                    return None
                card = board.sd_card.owned_image()
                clone(entry, card.path)
                _touch(entry)
        except OSError as error:
            raise SourceError(f"card_cache: {name!r}: {error}") from None
        return card

    def put(self, name: str, image: str | os.PathLike[str]) -> None:
        """Keeps a copy of the image file `image` as `name` for this application, in place of one
        kept before, evicting the cards least recently used to stay within the limit. Others see
        the old card, the new one or none, never part of one. The image is the test's still, and
        what the firmware writes to it after this is not kept. A card in the slot of a board that
        is on is refused: what the firmware has open would be kept half written."""
        board, entry = self._find(name)
        slot = board.sd_card
        source = (slot.directory / image).resolve()
        if not source.is_file():
            raise SourceError(f"card_cache: {name!r}: no image at {source}")
        if board.powered and any(
            held is not None and same_file(source, held) for held in (slot.image_path, slot.held)
        ):
            raise SourceError(
                f"card_cache: {name!r}: {source} is in the slot of a board that is on:"
                " call board.power_off() first"
            )
        needed = allocated(source)
        self._check_limit(name, needed)
        temporary = entry.with_suffix(".tmp")
        try:
            with self._locked():
                for stale in self.directory.glob("*.tmp"):  # of a run that died: the lock is ours
                    stale.unlink()
                entry.unlink(missing_ok=True)
                self._evict(needed)
                try:
                    clone(source, temporary)
                    _touch(temporary)
                    # Measured once synced: a file system that allocates a file's blocks then, as
                    # ext4 does, may take more than it counted before. A sparse copy may also
                    # round differently from the file it copies.
                    _sync(temporary)
                    written = allocated(temporary)
                    self._check_limit(name, written)
                    self._evict(written)
                    os.replace(temporary, entry)
                finally:
                    temporary.unlink(missing_ok=True)
        except OSError as error:
            raise SourceError(f"card_cache: {name!r}: {error}") from None

    def _find(self, name: str) -> tuple[Board, Path]:
        """The running test's board, and where `name` is kept for its application."""
        if not isinstance(name, str) or not name:
            raise ValueError(f"card_cache: a card's name is a non-empty string, not {name!r}")
        board = self._board()
        if board is None:
            raise SourceError("card_cache: no board is running: use it in a test that uses board")
        application = board.app_sha256
        if application is None:
            raise SourceError("card_cache: the build names no app image to keep a card for")
        key = hashlib.sha256(f"{application}\0{name}".encode()).hexdigest()
        return board, self.directory / f"{key}.img"

    def _check_limit(self, name: str, needed: int) -> None:
        if needed > self.limit:
            raise SourceError(
                f"card_cache: {name!r} needs {needed} bytes on disk; the limit is {self.limit}:"
                " raise esp32tl_card_cache_mib"
            )

    def _evict(self, needed: int) -> None:
        """Removes the cards least recently used until `needed` more bytes fit."""
        cards = [(p.stat().st_mtime_ns, p.name, p) for p in self.directory.glob("*.img")]
        used = sum(allocated(path) for *_, path in cards)
        for _, _, path in sorted(cards):
            if used + needed <= self.limit:
                return
            used -= allocated(path)
            path.unlink()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with (self.directory / "lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield
