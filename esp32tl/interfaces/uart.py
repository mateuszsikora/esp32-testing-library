"""A UART of the board profile, `uart.<role>`: what is at the far end of its route, as the link
drives it, and the tap that records what crosses it. The `gnss` fixture is the one of
`uart.gnss`."""

import os
import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Protocol

from esp32tl import recording
from esp32tl.boards.capabilities import Request
from esp32tl.interfaces import SourceError
from esp32tl.link.boardlink import Direction, Interface, Output, Wire
from esp32tl.report import RunLog
from esp32tl.sources.replay import Replay
from esp32tl.tap import NO_SOURCE, Tap


class Source(Protocol):
    """What drives a UART from its far end: a replay now, a model or a real device later. The
    methods are those of the link's `Interface`, without the wire."""

    def receive(self, t: int, data: bytes) -> Iterable[Output]: ...

    def poll(self, now: int) -> Iterable[Output]: ...

    def quiet_until(self) -> int | None: ...

    def latency(self) -> int | None: ...


class Uart(Interface):
    """The interface `name` (`uart.gnss`) on `wire`, whose source the test gives in its body.

    What the test does with it decides what is at the far end: `replay(path)` a recording. Until
    then the UART hears everything and says nothing. `tap(path)` records what crosses it, both
    ways, whatever is at the far end. `clock` is the backend's: a recording on another one is
    refused. A relative path is taken from `directory`, the test file's. `asked` is called with
    the request, a source (`replay`) or a tap on this interface, before the source or the tap is
    made, and refuses it by raising: the backend's checks, that the board is still off and that a
    backend gives it. `log`, when given, gets every byte that crosses the wire, and the test's
    steps on the UART, at 0: they come before power-on. `files` holds the files the run reads and
    writes, by path, with what each is for, the kind (`replay`, `tap`) and the route. The backend
    gives its UARTs one mapping, so that a tap never writes over a recording a replay read, or
    over another route's tap.
    """

    def __init__(
        self,
        name: str,
        wire: Wire,
        *,
        clock: str,
        directory: Path,
        asked: Callable[[Request], None] = lambda request: None,
        log: RunLog | None = None,
        files: dict[Path, tuple[str, str]] | None = None,
    ) -> None:
        super().__init__(name, wire)
        self.clock = clock
        self.directory = directory
        self.asked = asked
        self.log = log
        self.source: Source | None = None
        # The kind of source the test gave, as the capability declaration names it: `replay`.
        self.source_kind: str | None = None
        # What the source was made from, as the test named it: the replay's recording.
        self.source_detail: str | None = None
        self.tapped: Tap | None = None
        self.files = {} if files is None else files
        self._giving = threading.Lock()

    def replay(self, path: str | os.PathLike[str]) -> Replay:
        """Replays the recording at `path`, relative to the test file's directory, into the
        chip, at its recorded times: the stream case, a recording with no requests. Returns the
        `Replay`, whose `sent` counts the events the link has been given."""
        self._ask("replay")
        file = self.directory / path
        # Two replays of one file harm nothing; a replay of a file a tap or the SD card will
        # write over does.
        self._unclaimed(file, f"{self.name}: cannot replay {path}", writers_only=True)
        replay = Replay(recording.read(file), self.name, self.clock, name=str(path))
        self._give(replay, "replay", str(path), file)
        return replay

    def tap(self, path: str | os.PathLike[str]) -> Tap:
        """Records what crosses the UART from power-on, both ways, and writes it to `path`,
        relative to the test file's directory, as a recording once the run has ended: what the
        firmware transmitted, and what the far end sent in, whatever it is. The directory must
        exist; the file is replaced. Returns the `Tap`, whose `events` the run fills."""
        if self.tapped is not None:
            raise SourceError(f"{self.name} is tapped already: one tap per test")
        self.asked(Request(self.name, tap=True))
        file = self.directory / path
        if not file.parent.is_dir():
            raise FileNotFoundError(
                f"{self.name}: cannot tap into {path}: {file.parent} is not a directory"
            )
        self._unclaimed(file, f"{self.name}: cannot tap into {path}")
        with self._giving:
            if self.tapped is not None:
                raise SourceError(f"{self.name} is tapped already: one tap per test")
            self.tapped = Tap(self.name, file)
            self.files[file.resolve()] = ("tap", self.name)
        if self.log is not None:
            self.log.test(0, f"{self.name}.tap({str(path)!r})")
        return self.tapped

    def recorded_source(self) -> str:
        """What a tap's recording says was at the far end (`docs/recording-format.md`): the
        kind of source, `replay`, or `none` for nothing."""
        return self.source_kind or NO_SOURCE

    def _ask(self, source: str) -> None:
        if self.source is not None:
            raise SourceError(f"{self.name} has its source already: one per test")
        self.asked(Request(self.name, source=source))

    def _unclaimed(self, file: Path, what: str, writers_only: bool = False) -> None:
        """Refuses `file` when the run reads or writes it already, or, with `writers_only`, when
        a route is tapped into it or it is the SD card's image: either would write over it."""
        for claimed, (kind, route) in self.files.items():
            if writers_only and kind not in WRITERS:
                continue
            if same_file(file, claimed):
                raise SourceError(f"{what}: it is the {kind} of {route}")

    def _give(
        self,
        source: Source,
        kind: str,
        detail: str,
        file: Path | None = None,
        step: str | None = None,
    ) -> None:
        """`source`, of `kind`, made from `detail`, as the route's: `file` the run reads it from,
        if any, and `step` what the log says the test did, `{name}.{kind}({detail!r})` unless
        given."""
        with self._giving:
            if self.source is not None:
                raise SourceError(f"{self.name} has its source already: one per test")
            self.source = source
            self.source_kind = kind
            self.source_detail = detail
            if file is not None:
                self.files[file.resolve()] = (kind, self.name)
        if self.log is not None:
            self.log.test(0, step or f"{self.name}.{kind}({detail!r})")

    def receive(self, t: int, direction: Direction, data: bytes) -> Iterable[Output]:
        self._crossed(t, "out", data)
        if self.source is None:
            return ()
        return self._sent(self.source.receive(t, data))

    def poll(self, now: int) -> Iterable[Output]:
        return () if self.source is None else self._sent(self.source.poll(now))

    def _sent(self, outputs: Iterable[Output]) -> list[Output]:
        """`outputs`, read, each that is a `(t, bytes)` pair, `t` nanoseconds from 0, recorded as
        what the far end gave: the link refuses the others, and fails the run. The link may
        refuse a pair too, one stamped before its turn, and that fails the run as well."""
        outputs = list(outputs)
        for output in outputs:
            if isinstance(output, tuple) and len(output) == 2:
                t, data = output
                if (
                    isinstance(t, int)
                    and not isinstance(t, bool)
                    and t >= 0
                    and isinstance(data, bytes)
                ):
                    self._crossed(t, "in", data)
        return outputs

    def _crossed(self, t: int, direction: Direction, data: bytes) -> None:
        if self.tapped is not None:
            self.tapped.record(t, direction, data)
        if self.log is not None:
            self.log.route(t, self.name, direction, data)

    def quiet_until(self) -> int | None:
        return None if self.source is None else self.source.quiet_until()

    def latency(self) -> int | None:
        return None if self.source is None else self.source.latency()


# The kinds of `files` entry whose file the run writes: a replay must not read one of them.
WRITERS = ("tap", "card")


def same_file(a: Path, b: Path) -> bool:
    """Whether `a` and `b` name one file: the same file when both exist, else the same name in the
    same directory, its case aside. A tap's file need not exist yet, and on macOS's file system
    `R.jsonl` is `r.jsonl`; on one that tells them apart, two such names are refused all the
    same."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        pass
    try:
        same_directory = os.path.samefile(a.parent, b.parent)
    except OSError:
        return False
    return same_directory and a.name.casefold() == b.name.casefold()
