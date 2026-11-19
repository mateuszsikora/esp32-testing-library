"""The board profile's SD card slot, `sd_card`: what is in it. The `sd_card` fixture is it.

On the `esp32sim` backend the card is a model in the runner, holding an image file: an SD card in
SPI mode on the slot's pins, which writes what the firmware writes into the file as it goes. The
test puts a card in before power-on, and may take it out and put one in while the firmware runs.
The image builder and reader use mtools; external image paths remain supported.

The test can also make the card fail or slow down at block level, from the board's current
instant on: `sd_card.fail("write")`, `sd_card.delay("read", ms(50))`, or only watch what the
firmware does with it: `sd_card.observe("write")`. The rules go to the runner on the link
(`sd_fault`, `sd_clear`), and what they met comes back (`sd_op`) as `FaultedOperation`s.
"""

import errno
import os
import shutil
import subprocess
import threading
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile, TemporaryDirectory, mkdtemp
from typing import Literal

from esp32tl.boards import SdCardRoute
from esp32tl.boards.capabilities import Request
from esp32tl.interfaces import RunEnded, SourceError
from esp32tl.interfaces.uart import same_file
from esp32tl.link.boardlink import Interface, LinkError, counted
from esp32tl.link.protocol import (
    SD_COUNT_MAX,
    SD_DELAY_MAX,
    Input,
    SdCardChange,
    SdClear,
    SdFault,
    SdOp,
)
from esp32tl.report import RunLog
from esp32tl.units import Duration

# The capacity unit of a high capacity card (CSD C_SIZE): an image is a whole number of them.
UNIT = 512 * 1024
# The most a card holds: 2^22 units (CSD C_SIZE).
MAX_SIZE = UNIT << 22
# The most the builder makes, in MiB: 1 TiB, as #132 chose after mformat asserted near 2 TiB, which
# mtools 4.0.43 and 4.0.50 have not repeated; `insert` goes further
# (docs/features/pytest-plugin-technical.md).
BUILD_MAX_MB = 1 << 20
# The clusters the builder makes, in KiB: mformat's -c takes 2 to 128 sectors.
CLUSTER_KB = (1, 2, 4, 8, 16, 32, 64)
# From here on the builder makes FAT32 whatever the clusters, in the geometry its BPB usually has.
FAT32_FROM_MB = 512
FAT32_HEADS = 255
FAT32_SECTORS = 63
# What a FAT32 volume's count of clusters may be (Microsoft's FAT specification): a file system
# with fewer is FAT16 to whoever mounts it, whatever it says of itself.
FAT32_CLUSTERS = range(65525, 0x0FFFFFF5 + 1)
# mformat's FAT32: its reserved sectors and FATs, and the FAT entries a sector holds.
FAT32_RESERVED = 32
FAT32_FATS = 2
FAT32_ENTRIES = 128

MTOOLS = ("mformat", "mcopy", "mmd", "mtype", "mdir")

# A block of the card, the unit its fault rules count in.
BLOCK = 512
# The longest delay a rule may give.
MAX_DELAY = Duration(SD_DELAY_MAX)

ReadOrWrite = Literal["read", "write"]
Operation = Literal["read", "write", "erase"]
# What a rule does to the operations it meets.
Action = Literal["error", "delay", "observe"]


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


def fat32_clusters(size_mb: int, cluster_kb: int) -> int:
    """The clusters of a FAT32 file system on a whole card, with FATs just long enough for them,
    as mformat makes them (tests/test_card_image.py)."""
    sectors = (size_mb << 11) - FAT32_RESERVED
    per_cluster = cluster_kb * 2
    per_fat_sector = FAT32_ENTRIES * per_cluster + FAT32_FATS
    fat = (sectors + 2 * per_cluster + per_fat_sector - 1) // per_fat_sector
    return (sectors - FAT32_FATS * fat) // per_cluster


class CardImageBuilder:
    """Declare initial files; build creates a fresh image without inserting it. `cluster_kb` is
    the size of its clusters; `None` leaves them to mformat."""

    def __init__(self, slot: "SdCard", size_mb: int, cluster_kb: int | None = None) -> None:
        if type(size_mb) is not int or not 1 <= size_mb <= BUILD_MAX_MB:
            raise ValueError(f"size_mb must be a whole number from 1 to {BUILD_MAX_MB} (MiB)")
        if cluster_kb is not None:
            if type(cluster_kb) is not int or cluster_kb not in CLUSTER_KB:
                choices = ", ".join(str(kb) for kb in CLUSTER_KB)
                raise ValueError(f"cluster_kb must be one of {choices} (KiB)")
            if size_mb >= FAT32_FROM_MB:
                clusters = fat32_clusters(size_mb, cluster_kb)
                if clusters not in FAT32_CLUSTERS:
                    raise ValueError(
                        f"a FAT32 card of {size_mb} MiB with {cluster_kb} KiB clusters has"
                        f" {clusters} clusters; FAT32 takes {FAT32_CLUSTERS[0]} to"
                        f" {FAT32_CLUSTERS[-1]}: choose another size_mb or cluster_kb"
                    )
        self._slot = slot
        self._size_mb = size_mb
        self._cluster_kb = cluster_kb
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
            fat32 = self._size_mb >= FAT32_FROM_MB
            cluster = () if self._cluster_kb is None else ("-c", str(self._cluster_kb * 2))
            _mtools(
                "mformat",
                "-i",
                str(path),
                *(("-F",) if fat32 else ()),
                "-T",
                str(self._size_mb << 11),
                "-h",
                str(FAT32_HEADS if fat32 else 64),
                "-s",
                str(FAT32_SECTORS if fat32 else 32),
                *cluster,
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


@dataclass(frozen=True)
class FaultedOperation:
    """A block operation of the card that one of the test's rules met: `fault`, at `at` on the
    board's clock, the `op` on `blocks` (one block for a read or a write), and what the rule did,
    `action` "error", "delay" with its `delay`, or "observe", nothing. `written` says whether the
    card wrote the image: never for a read or a failed write, always for a write or erase it
    carried out."""

    at: Duration
    fault: "CardFault"
    op: Operation
    blocks: range
    action: Action
    delay: Duration | None
    written: bool

    def __str__(self) -> str:
        blocks = (
            f"block {self.blocks.start}"
            if len(self.blocks) == 1
            else f"blocks {self.blocks.start} to {self.blocks[-1]}"
        )
        if self.action == "delay":
            what = f"delayed {self.delay}"
        else:
            what = "failed" if self.action == "error" else "observed"
        return f"{self.op} of {blocks} {what}, {'written' if self.written else 'not written'}"


class CardFault:
    """A rule the test gave the card, from `at` on the board's clock: what `op`s it applies to,
    on which `blocks` (`None`: any), what it does (`action` "error", "delay" by `delay`, or
    "observe", nothing), and to how many (`count`; `None`: every one until cleared). `hits` are
    the operations it met so far, as the link has brought them."""

    def __init__(
        self,
        slot: "SdCard",
        id: int,
        at: Duration,
        op: Operation,
        action: Action,
        delay: Duration | None,
        count: int | None,
        blocks: range | None,
        call: str,
    ) -> None:
        self._slot = slot
        self.id = id
        self.at = at
        self.op = op
        self.action = action
        self.delay = delay
        self.count = count
        self.blocks = blocks
        self.call = call
        self.cleared_at: Duration | None = None
        self._hits: list[FaultedOperation] = []

    @property
    def hits(self) -> tuple[FaultedOperation, ...]:
        with self._slot._lock:
            return tuple(self._hits)

    @property
    def active(self) -> bool:
        """Whether the rule still applies: not cleared, and with operations left."""
        with self._slot._lock:
            return self._active()

    def _active(self) -> bool:
        spent = self.count is not None and len(self._hits) >= self.count
        return self.cleared_at is None and not spent

    def clear(self) -> None:
        """Removes the rule from the board's current instant on. An operation it applied to that
        is still under way, a delayed read or a busy write, keeps its delay."""
        self._slot._clear([self], f"{self.call}.clear()")

    def __repr__(self) -> str:
        return f"<CardFault {self.id}: {self.call}>"


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
    recording. `now`
    is the board's clock, which stamps the changes and the fault rules, and `ended` says whether
    the run is over, when no rule reaches the card any more.

    On the link the slot is the card's fault rules too: it sends the rules the test gave in the next
    turn, and takes what the runner reports of them.
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
        ended: Callable[[], bool] = lambda: False,
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
        self._now = now
        self._ended = ended
        self._started = started
        self._lock = threading.Lock()
        # The changes the test made with the board on, and the rules and clears it gave, in order:
        # not sent yet; then the changes not acknowledged yet.
        self._pending: list[SdCardChange | SdFault | SdClear] = []
        self._sent: deque[SdCardChange] = deque()
        self._changes: list[CardChange] = []
        self._refused: list[CardChange] = []
        # The image of the card in the slot at power-on, kept by the first change made with the
        # board on; empty until then.
        self._at_power_on: list[Path | None] = []
        # The card's size in blocks, once it is in.
        self._blocks = 0
        self._faults: dict[int, CardFault] = {}
        self._faulted: list[FaultedOperation] = []

    @property
    def card_slot(self) -> bool:
        return True

    def image(self, *, size_mb: int = 32, cluster_kb: int | None = None) -> CardImageBuilder:
        """Start a FAT image builder. Building and insertion are separate operations."""
        self._can_build()
        return CardImageBuilder(self, size_mb, cluster_kb)

    def owned_image(self) -> CardImage:
        """A path for an image the slot owns, with nothing at it yet: gone when the board closes,
        as a built image is. Asks no backend: the card goes in, or skips, at `insert`."""
        self._check_open()
        return CardImage(self._image_directory() / "card.img", self._check_open)

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
        self._blocks = size // BLOCK
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
            pending = [m for m in self._pending if isinstance(m, SdCardChange)]
            unacknowledged = [*self._sent, *pending]
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

    # Fault rules: the test's side.

    def fail(
        self,
        op: ReadOrWrite,
        *,
        count: int | None = None,
        blocks: int | range | None = None,
    ) -> CardFault:
        """Makes the card fail reads or writes from the board's current instant on: a read
        answers the data error token "card ECC failed", a write the data response "write
        error", and the block on the image stays as it was. `count` operations, or every one
        until the rule is cleared; on `blocks`, a block number or a range of them, or any. A
        block of a multiple block read or write is one operation, and a read the firmware ends
        with its stop command does not reach the block after its last."""
        if op not in ("read", "write"):
            raise ValueError(f"sd_card.fail: op {op!r} is not 'read' or 'write'")
        return self._give("sd_card.fail", repr(op), op, "error", None, count, blocks)

    def delay(
        self,
        op: Operation,
        by: Duration,
        *,
        count: int | None = None,
        blocks: int | range | None = None,
    ) -> CardFault:
        """Makes the card take `by` longer, on the board's clock, from its current instant on: a
        read holds its data block back, a write keeps the card busy after its data response, an
        erase after its R1, `by` from the operation's start; firmware that times out first sees
        its timeout. At most 10 s. `count` and `blocks` as for `fail`; an erase meets a rule
        whose blocks overlap its range."""
        if op not in ("read", "write", "erase"):
            raise ValueError(f"sd_card.delay: op {op!r} is not 'read', 'write' or 'erase'")
        if not isinstance(by, Duration):
            raise TypeError(f"sd_card.delay: by is {by!r}, not a Duration")
        if not Duration(1) <= by <= MAX_DELAY:
            raise ValueError(f"sd_card.delay: a delay of {by}; one is 1 ns to {MAX_DELAY}")
        return self._give("sd_card.delay", f"{op!r}, {by!r}", op, "delay", by, count, blocks)

    def observe(
        self,
        op: Operation,
        *,
        count: int | None = None,
        blocks: int | range | None = None,
    ) -> CardFault:
        """Watches the card's reads, writes or erases from the board's current instant on,
        changing nothing: the firmware sees the card, its answers and their timing as without the
        rule, and the rule's `hits` list the operations, with whether the card wrote the image.
        A rule that fails or delays the same operation does so as without this one, which records
        it too. `count` and `blocks` as for `delay`."""
        if op not in ("read", "write", "erase"):
            raise ValueError(f"sd_card.observe: op {op!r} is not 'read', 'write' or 'erase'")
        return self._give("sd_card.observe", repr(op), op, "observe", None, count, blocks)

    def clear_faults(self) -> None:
        """Removes every rule from the board's current instant on."""
        self._clear(self.faults, "sd_card.clear_faults()", every=True)

    @property
    def faults(self) -> tuple[CardFault, ...]:
        """Every rule the test gave, in order, cleared ones too."""
        with self._lock:
            return tuple(self._faults.values())

    @property
    def faulted(self) -> tuple[FaultedOperation, ...]:
        """The operations the rules met, in the order of the board's time: one per rule an
        operation met, in the order the test gave them."""
        with self._lock:
            return tuple(self._faulted)

    def _give(
        self,
        what: str,
        arguments: str,
        op: Operation,
        action: Action,
        delay: Duration | None,
        count: int | None,
        blocks: int | range | None,
    ) -> CardFault:
        self._check_open()
        if self.image_path is None:
            raise SourceError(f"{what}: the slot is empty: insert a card first")
        if self._ended():
            raise RunEnded(f"{what}: the run has ended: no rule reaches the card any more")
        if count is not None and (isinstance(count, bool) or not isinstance(count, int)):
            raise TypeError(f"{what}: count is {count!r}, not a whole number")
        if count is not None and not 1 <= count <= SD_COUNT_MAX:
            raise ValueError(f"{what}: a count of {count}; it is 1 to 2**63 - 1, or None")
        span = self._span(blocks, what)
        if count is not None:
            arguments += f", count={count}"
        if blocks is not None:
            arguments += f", blocks={blocks!r}"
        call = f"{what}({arguments})"
        now = self._now()
        with self._lock:
            fault = CardFault(
                self, len(self._faults) + 1, Duration(now), op, action, delay, count, span, call
            )
            self._faults[fault.id] = fault
            self._pending.append(
                SdFault(
                    now,
                    fault.id,
                    op,
                    action,
                    None if delay is None else delay.ns,
                    count,
                    None if span is None else (span.start, span[-1]),
                )
            )
        if self.log is not None:
            self.log.test(now, call)
        return fault

    def _span(self, blocks: int | range | None, what: str) -> range | None:
        if blocks is None:
            return None
        if isinstance(blocks, int) and not isinstance(blocks, bool):
            blocks = range(blocks, blocks + 1)
        if not isinstance(blocks, range):
            raise TypeError(f"{what}: blocks is {blocks!r}, not a block number or a range")
        if blocks.step != 1 or not blocks:
            raise ValueError(f"{what}: blocks {blocks!r} is not a non-empty range of step 1")
        if blocks.start < 0 or blocks.stop > self._blocks:
            raise ValueError(
                f"{what}: blocks {blocks!r}, and the card has blocks 0 to {self._blocks - 1}"
            )
        return blocks

    def _clear(self, faults: Iterable[CardFault], call: str, every: bool = False) -> None:
        self._check_open()
        now = self._now()
        with self._lock:
            ids = []
            for fault in faults:
                # A spent rule stays as it ended. One that looks active may be spent by the time
                # this comes due, which the runner takes as no error.
                if fault._active():
                    fault.cleared_at = Duration(now)
                    ids.append(fault.id)
            if ids:
                self._pending.append(SdClear(now, None if every else tuple(ids)))
        if self.log is not None:
            self.log.test(now, call)

    # The link's side.

    def inputs(self, now: int) -> Iterable[Input]:
        with self._lock:
            pending, self._pending = self._pending, []
            # Given before power-on, or while the link was between turns: at the turn's instant.
            sent = [replace(message, t=max(message.t, now)) for message in pending]
            self._sent.extend(m for m in sent if isinstance(m, SdCardChange))
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

    def sd_op(self, message: SdOp) -> None:
        with self._lock:
            fault = self._faults.get(message.fault)
            what = f"the runner says rule {message.fault} met a {message.op} as {message.action}"
            if fault is None:
                raise LinkError(f"{self.name}: {what}; the test gave no rule {message.fault}")
            # The report and the hits say what the test's rule does, so the runner must agree.
            delay = None if fault.delay is None else fault.delay.ns
            if (message.op, message.action, message.delay) != (fault.op, fault.action, delay):
                raise LinkError(f"{self.name}: {what}; the test gave it as {fault.call}")
            first, last = message.blocks
            done = FaultedOperation(
                Duration(message.t),
                fault,
                fault.op,
                range(first, last + 1),
                fault.action,
                fault.delay,
                message.written,
            )
            fault._hits.append(done)
            self._faulted.append(done)

    def report(self) -> list[str]:
        """The rules the test gave and what each did, for the run's report."""
        lines = []
        for fault in self.faults:
            cleared = "" if fault.cleared_at is None else f", cleared at {fault.cleared_at}"
            hits = fault.hits
            lines.append(
                f"  fault {fault.id}: {fault.call} at {fault.at}{cleared};"
                f" {counted(len(hits), 'operation')}"
            )
            lines += [f"    {hit.at}  {hit}" for hit in hits]
        return lines

    def properties(self) -> list[dict[str, object]]:
        """The same as data, for the report's `user_properties`."""
        return [
            {
                "id": fault.id,
                "call": fault.call,
                "t_ns": fault.at.ns,
                "cleared_t_ns": None if fault.cleared_at is None else fault.cleared_at.ns,
                "operations": [
                    {
                        "t_ns": hit.at.ns,
                        "op": hit.op,
                        "blocks": [hit.blocks.start, hit.blocks[-1]],
                        "action": hit.action,
                        "delay_ns": None if hit.delay is None else hit.delay.ns,
                        "written": hit.written,
                    }
                    for hit in fault.hits
                ],
            }
            for fault in self.faults
        ]


def _card_change(t: int, image: str | None) -> CardChange:
    return CardChange(t, None if image is None else Path(image))
