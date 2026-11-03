"""The board profile's SD card slot, `sd_card`: what is in it. The `sd_card` fixture is it.

On the `esp32sim` backend the card is a model in the runner, holding an image file: an SD card in
SPI mode on the slot's pins, which writes what the firmware writes into the file as it goes. The
image builder and reader use mtools; external image paths remain supported. Once a card is in,
the test can make its reads and writes fail and delay its reads, writes and erases on the board's
clock (`fail`, `delay`): the slot is then an interface of the link, which carries the card's fault
rules to the runner and each operation they acted on back (`CardIo`).
"""

import errno
import os
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile, TemporaryDirectory, mkdtemp
from typing import Literal, cast

from esp32tl.boards import SdCardRoute
from esp32tl.boards.capabilities import Request
from esp32tl.interfaces import SourceError
from esp32tl.interfaces.uart import same_file
from esp32tl.link.boardlink import Interface
from esp32tl.link.protocol import SD_MAX_DELAY, Input, SdClear, SdFault, SdIo
from esp32tl.report import RunLog
from esp32tl.units import Duration

# The capacity unit of a high capacity card (CSD C_SIZE): an image is a whole number of them.
UNIT = 512 * 1024
# The most a card holds: 2^22 units (CSD C_SIZE).
MAX_SIZE = UNIT << 22
# A card's blocks, which fault rules count in.
BLOCK = 512
# The longest delay a fault rule may give: twice ESP-IDF's 5 s write timeout.
MAX_DELAY = Duration(SD_MAX_DELAY)

Operation = Literal["read", "write", "erase"]

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


@dataclass(frozen=True, eq=False)
class CardIo:
    """An operation of the card that fault rules acted on: a read or a write of one block, or an
    erase of `blocks` blocks from `block`. `at` is when it began on the board's clock: when the
    command came, for a read or an erase; when the block's CRC came, for a write; for a later
    block of a multiple block read, when the host went on past it (protocol 5.19). `failed` is
    whether the card answered with an error, `delay` how long it held its answer, and `written`
    whether the image was written: a write that went through, an erase. `faults` are the rules
    that acted on it."""

    at: Duration
    op: Operation
    block: int
    blocks: int
    faults: tuple["CardFault", ...]
    failed: bool
    delay: Duration
    written: bool

    def __repr__(self) -> str:
        return f"CardIo({describe_io(self)})"


class CardFault:
    """A fault rule the test gave the card, from `at` on the board's clock: it acts on `op`s on
    `blocks` (every block with `None`), after letting `skip` of them through, and on `count` of
    them (every one until cleared with `None`). It makes them fail (`fail`), holds the card's
    answer to them for `delay`, or both. `hits` are the operations it acted on so far."""

    def __init__(
        self,
        slot: "SdCard",
        id: int,
        at: Duration,
        op: Operation,
        *,
        fail: bool,
        delay: Duration,
        blocks: range | None,
        skip: int,
        count: int | None,
    ) -> None:
        self._slot = slot
        self.id = id
        self.at = at
        self.op = op
        self.fail = fail
        self.delay = delay
        self.blocks = blocks
        self.skip = skip
        self.count = count
        self.cleared_at: Duration | None = None
        self._hits: list[CardIo] = []

    @property
    def hits(self) -> tuple[CardIo, ...]:
        with self._slot._lock:
            return tuple(self._hits)

    def clear(self) -> None:
        """The rule acts on no operation that begins from now on. A delay already running runs
        out."""
        self._slot._clear((self,), f"{self!r}.clear()")

    def __repr__(self) -> str:
        return f"CardFault({self.id})"

    def describe(self) -> str:
        """What the rule does, for the report: `fail writes on block 8, 1 time`."""
        what = "fail" if self.fail else f"delay by {self.delay}"
        blocks = (
            "every block"
            if self.blocks is None
            else f"block {self.blocks.start}"
            if len(self.blocks) == 1
            else f"blocks {self.blocks.start} to {self.blocks.stop - 1}"
        )
        skip = f", after skipping {self.skip}" if self.skip else ""
        count = (
            "until cleared"
            if self.count is None
            else f"{self.count} {'time' if self.count == 1 else 'times'}"
        )
        return f"{what} {self.op}s on {blocks}{skip}, {count}"


def describe_io(io: CardIo) -> str:
    """One line of what happened to an operation, for the report."""
    where = (
        f"blocks {io.block} to {io.block + io.blocks - 1}" if io.blocks > 1 else f"block {io.block}"
    )
    effects = (["failed"] if io.failed else []) + ([f"delayed {io.delay}"] if io.delay.ns else [])
    written = "written" if io.written else "not written"
    faults = ", ".join(str(fault.id) for fault in io.faults)
    noun = "fault" if len(io.faults) == 1 else "faults"
    return f"{io.at} {io.op} {where}: {', '.join(effects)}, {written} ({noun} {faults})"


class SdCard(Interface):
    """The slot on `route`, empty until the test inserts a card.

    `asked` is called with the `Request` of a `model` source before the card goes in,
    and refuses it by raising: the backend's checks, that the board is still off and that a
    backend gives that source. A relative image path is taken from `directory`, the test file's.
    `log`, when given, gets the test's `insert` as a step, at 0: it comes before power-on, and the
    test's fault rules at the board's time `now` gives. `files` holds the files the run reads and
    writes, shared with the routes, so that no tap writes over the card's image and the card
    writes over no recording.

    On the link the slot has no wire: its `inputs` carry the fault rules the test gives, stamped
    no earlier than the turn, and `card_io` the operations they acted on.
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
    ) -> None:
        super().__init__(self.name, None)
        self.route = route
        self.directory = directory
        self.asked = asked
        self.log = log
        self.files = {} if files is None else files
        # The image of the card in the slot, absolute; None while the slot is empty.
        self.image_path: Path | None = None
        self._temporary: TemporaryDirectory[str] | None = None
        self._closed = False
        self._now = now
        self._lock = threading.Lock()
        self._blocks = 0
        self._faults: dict[int, CardFault] = {}
        self._affected: list[CardIo] = []
        self._pending: list[SdFault | SdClear] = []

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
        self._blocks = size // BLOCK
        if self.log is not None:
            self.log.test(0, f"{self.name}.insert({os.fspath(image)!r})")
        return path

    # Fault rules: the test's side.

    def fail(
        self,
        op: Literal["read", "write"],
        *,
        blocks: int | range | None = None,
        skip: int = 0,
        count: int | None = 1,
    ) -> CardFault:
        """From now on the card fails `op`s: a read answers with an error token instead of its
        data, a write with a write error, and leaves its block as it was. `blocks`, a block
        number or a `range` of them, limits the rule to those blocks; `skip` matching operations
        go through first; it then fails `count` of them, every one until cleared with `None`.
        Before power-on the rule is there from the first instruction."""
        if op not in ("read", "write"):
            raise ValueError(f"{self.name}.fail: op is {op!r}, not 'read' or 'write'")
        return self._give("fail", repr(op), op, True, Duration(0), blocks, skip, count)

    def delay(
        self,
        op: Operation,
        by: Duration,
        *,
        blocks: int | range | None = None,
        skip: int = 0,
        count: int | None = None,
    ) -> CardFault:
        """From now on the card holds its answer to `op`s for `by` of the board's time: a read
        sends no data block until then, a write or an erase keeps the card busy. The firmware
        waits on the board's clock, so its own timeouts see the delay. `by` is more than 0 and
        at most `MAX_DELAY`, 10 s. `blocks`, `skip` and `count` as for `fail`, but `count`
        defaults to every operation until cleared."""
        if op not in ("read", "write", "erase"):
            raise ValueError(f"{self.name}.delay: op is {op!r}, not 'read', 'write' or 'erase'")
        if not isinstance(by, Duration):
            raise TypeError(f"{self.name}.delay: by is {by!r}, not a Duration")
        if not 0 < by.ns <= SD_MAX_DELAY:
            raise ValueError(
                f"{self.name}.delay: by is {by}; a delay is more than 0 s and at most {MAX_DELAY}"
            )
        return self._give("delay", f"{op!r}, {by!r}", op, False, by, blocks, skip, count)

    def clear_faults(self) -> None:
        """Every fault rule acts on no operation that begins from now on. A delay already
        running runs out."""
        self._clear(None, f"{self.name}.clear_faults()")

    @property
    def faults(self) -> tuple[CardFault, ...]:
        """The fault rules the test gave, in order, cleared ones included."""
        with self._lock:
            return tuple(self._faults.values())

    @property
    def affected(self) -> tuple[CardIo, ...]:
        """Every operation a fault rule acted on, in the order they began."""
        with self._lock:
            return tuple(self._affected)

    def _give(
        self,
        method: str,
        shown: str,
        op: Operation,
        fail: bool,
        delay: Duration,
        blocks: int | range | None,
        skip: int,
        count: int | None,
    ) -> CardFault:
        """The rule of `fail` or `delay`, checked, queued for the link and logged as the call,
        `shown` its positional arguments."""
        what = f"{self.name}.{method}"
        self._check_open()
        if self.image_path is None:
            raise SourceError(
                f"{self.name}: the slot is empty: insert a card before giving it faults"
            )
        span = self._span(blocks, what)
        if isinstance(skip, bool) or not isinstance(skip, int) or skip < 0:
            raise ValueError(f"{what}: skip is {skip!r}, not a whole number of 0 or more")
        if count is not None and (
            isinstance(count, bool) or not isinstance(count, int) or count < 1
        ):
            raise ValueError(f"{what}: count is {count!r}, not None or a whole number of 1 or more")
        arguments = [shown]
        if blocks is not None:
            arguments.append(f"blocks={blocks!r}")
        if skip:
            arguments.append(f"skip={skip}")
        if count != (1 if fail else None):
            arguments.append(f"count={count}")
        step = f"{what}({', '.join(arguments)})"
        now = self._now()
        with self._lock:
            id = len(self._faults) + 1
            fault = CardFault(
                self,
                id,
                Duration(now),
                op,
                fail=fail,
                delay=delay,
                blocks=span,
                skip=skip,
                count=count,
            )
            self._faults[id] = fault
            self._pending.append(
                SdFault(
                    now,
                    id,
                    op,
                    fail,
                    delay.ns,
                    None if span is None else span.start,
                    None if span is None else span.stop - 1,
                    skip,
                    count,
                )
            )
        if self.log is not None:
            self.log.test(now, f"{step}: {fault!r}")
        return fault

    def _span(self, blocks: int | range | None, what: str) -> range | None:
        """`blocks` as a range of the card's blocks, checked."""
        if blocks is None:
            return None
        if isinstance(blocks, int) and not isinstance(blocks, bool):
            span = range(blocks, blocks + 1)
        elif isinstance(blocks, range):
            span = blocks
        else:
            raise TypeError(f"{what}: blocks is {blocks!r}, not a block number or a range")
        if span.step != 1 or not span or span.start < 0:
            raise ValueError(
                f"{what}: blocks is {blocks!r}, not a block or a non-empty range of them"
            )
        if span.stop > self._blocks:
            raise ValueError(
                f"{what}: blocks is {blocks!r}; the card's blocks are 0 to {self._blocks - 1}"
            )
        return span

    def _clear(self, faults: Iterable[CardFault] | None, step: str) -> None:
        self._check_open()
        now = self._now()
        with self._lock:
            cleared = list(self._faults.values()) if faults is None else list(faults)
            for fault in cleared:
                if fault.cleared_at is None:
                    fault.cleared_at = Duration(now)
            ids = None if faults is None else tuple(fault.id for fault in cleared)
            if self._faults:
                self._pending.append(SdClear(now, ids))
        if self.log is not None:
            self.log.test(now, step)

    # The link's side.

    @property
    def sd_card(self) -> bool:
        return True

    def inputs(self, now: int) -> Iterable[Input]:
        with self._lock:
            pending, self._pending = self._pending, []
        return [replace(message, t=max(message.t, now)) for message in pending]

    def card_io(self, io: SdIo) -> None:
        with self._lock:
            faults = tuple(self._faults[id] for id in io.rules if id in self._faults)
            done = CardIo(
                Duration(io.t),
                cast(Operation, io.op),
                io.block,
                io.blocks,
                faults,
                io.failed,
                Duration(io.delay),
                io.written,
            )
            self._affected.append(done)
            for fault in faults:
                fault._hits.append(done)

    def report(self) -> list[str]:
        """The card in the slot, then each fault rule with the operations it acted on."""
        if self.image_path is None:
            return [f"{self.name}: empty"]
        lines = [f"{self.name}: a card holding {self.image_path}"]
        for fault in self.faults:
            cleared = "" if fault.cleared_at is None else f", cleared at {fault.cleared_at}"
            hits = fault.hits
            lines.append(
                f"  fault {fault.id} from {fault.at}: {fault.describe()}{cleared};"
                f" {len(hits)} {'operation' if len(hits) == 1 else 'operations'}"
            )
            lines += [f"    {describe_io(io)}" for io in hits]
        return lines

    def properties(self) -> dict[str, object]:
        """The same as data, for the report's `user_properties`."""
        return {
            "image": None if self.image_path is None else str(self.image_path),
            "faults": [
                {
                    "id": f.id,
                    "t_ns": f.at.ns,
                    "op": f.op,
                    "fail": f.fail,
                    "delay_ns": f.delay.ns,
                    "blocks": None if f.blocks is None else [f.blocks.start, f.blocks.stop - 1],
                    "skip": f.skip,
                    "count": f.count,
                    "cleared_ns": None if f.cleared_at is None else f.cleared_at.ns,
                }
                for f in self.faults
            ],
            "affected": [
                {
                    "t_ns": io.at.ns,
                    "op": io.op,
                    "block": io.block,
                    "blocks": io.blocks,
                    "faults": [f.id for f in io.faults],
                    "failed": io.failed,
                    "delay_ns": io.delay.ns,
                    "written": io.written,
                }
                for io in self.affected
            ],
        }
