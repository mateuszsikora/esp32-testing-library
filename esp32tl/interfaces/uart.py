"""A UART of the board profile, `uart.<role>`: what is at the far end of its route, as the link
drives it, and the tap that records what crosses it. The `gnss` fixture is the one of
`uart.gnss`."""

import os
import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Protocol, TypeVar, runtime_checkable

from esp32tl import recording
from esp32tl.boards.capabilities import Request
from esp32tl.interfaces import SourceError
from esp32tl.link.boardlink import Direction, Interface, LinkError, Output, Wire
from esp32tl.matching import Protocol as ReplayProtocol
from esp32tl.report import RunLog
from esp32tl.sources.model import InNanoseconds, Model, in_nanoseconds
from esp32tl.sources.real import Real
from esp32tl.sources.replay import Replay
from esp32tl.tap import NO_SOURCE, Tap
from esp32tl.units import Duration


class Source(Protocol):
    """What drives a UART from its far end: a replay, a model or a real device. The methods are
    those of the link's `Interface`, without the wire."""

    def receive(self, t: int, data: bytes) -> Iterable[Output]: ...

    def poll(self, now: int) -> Iterable[Output]: ...

    def quiet_until(self) -> int | None: ...

    def latency(self) -> int | None: ...


# A model the test gives, in nanoseconds or a `Model`, which `Uart.model` returns as it was given.
S = TypeVar("S", bound=Source | Model)


@runtime_checkable
class Supplied(Protocol):
    """A source that is a part of the board, on the board's supply: a model of the receiver or
    the modem soldered on, or wired to the board's power, or a model of the test's with this
    method. It starts with power; the first power-on calls nothing."""

    def supply(self, on: bool) -> None:
        """The board's supply went off, or came back, at the board's time now."""
        ...


@runtime_checkable
class Arriving(Protocol):
    """A source whose far end takes things in on its own time: the network, on its path."""

    def arrive(self, now: int) -> None: ...


@runtime_checkable
class RateBound(Protocol):
    """A source that cannot take what the firmware transmits at any rate: a real device, behind a
    port whose rate is fixed for the run."""

    def transmitted(self, t: int, baud: int | None) -> None:
        """The firmware transmits at `baud` next, `None` with none set. Raises to fail the run."""
        ...


class Uart(Interface):
    """The interface `name` (`uart.gnss`) on `wire`, whose source the test gives in its body.

    What the test does with it decides what is at the far end: `replay(path)` a recording,
    `real(port)` a device on a host serial port, `model(source)` a model the test wrote. Until
    then the UART hears everything and says nothing. `tap(path)` records what crosses it, both
    ways, whatever is at the far end. `clock` is the backend's, or how to ask it, for one that
    depends on the sources given: a recording on another one is refused. A relative path is taken
    from `directory`, the test file's. `asked` is called with the request, a source (`replay`,
    `real`, `model`) or a tap on this interface, before the source or the tap is made, and
    refuses it by raising: the backend's checks, that the board is still off and that a backend
    gives it. `log`, when given, gets every byte that crosses the wire, and the test's steps on
    the UART, at 0: they come before power-on. `files` holds the files the
    run reads and writes, by path, with what each is for, the kind (`replay`, `tap`) and the route.
    The backend gives its UARTs one mapping, so that a tap never writes over a recording a replay
    read, or over another route's tap.
    """

    def __init__(
        self,
        name: str,
        wire: Wire | None,
        *,
        clock: str | Callable[[], str],
        directory: Path,
        asked: Callable[[Request], None] = lambda request: None,
        log: RunLog | None = None,
        files: dict[Path, tuple[str, str]] | None = None,
    ) -> None:
        super().__init__(name, wire)
        self._clock = clock
        self.directory = directory
        self.asked = asked
        self.log = log
        # What the test gave; the route calls `_driving`, the same but for a `Model`.
        self.source: Source | Model | None = None
        self._adapter: InNanoseconds | None = None
        # The kind of source the test gave, as the capability declaration names it: `replay`.
        self.source_kind: str | None = None
        # What the source was made from, as the test named it: the replay's recording.
        self.source_detail: str | None = None
        self.tapped: Tap | None = None
        self.files = {} if files is None else files
        self._giving = threading.Lock()

    @property
    def recording_clock(self) -> str:
        """The clock this route's recordings are on."""
        return self._clock() if callable(self._clock) else self._clock

    def replay(
        self, path: str | os.PathLike[str], *, protocol: ReplayProtocol | None = None
    ) -> Replay:
        """Replays the recording at `path`, relative to the test file's directory, into the
        chip, with recorded timing and request matching. `protocol` overrides detection. Returns the
        `Replay`, whose `sent` counts the events the link has been given."""
        self._ask("replay")
        file = self.directory / path
        # Two replays of one file harm nothing; a replay of a file a tap or the SD card will
        # write over does.
        self._unclaimed(file, f"{self.name}: cannot replay {path}", writers_only=True)
        replay = Replay(
            recording.read(file),
            self.name,
            self.recording_clock,
            name=str(path),
            protocol=protocol,
        )
        self._give(replay, "replay", str(path), file)
        return replay

    def real(self, port: str) -> Real:
        """Puts the device on the host serial port `port` at the far end, at the route's baud.
        The whole run is then on the wall clock (protocol §12.4). Returns the `Real`, whose
        `dropped` counts the bytes the run could not carry."""
        self._ask("real")
        assert self.wire is not None
        source = Real(self.name, port, self.wire.baud)
        try:
            self._give(source, "real", port)
        except SourceError:
            source.close()
            raise
        return source

    def model(self, source: S) -> S:
        """Puts `source`, a model the test wrote, at the far end: what the firmware transmits
        goes to its `receive`, and what it returns, or sends from `poll`, goes into the chip at
        its own time. One that is `Supplied` loses its power with the board's supply, and gets it
        back. A `Model` states its times in `Duration`s, and the link's errors about them are in
        `Duration`s too (`instant`); any other source in nanoseconds. Returns `source`."""
        missing = [m for m in SOURCE_METHODS if not callable(getattr(source, m, None))]
        if missing:
            raise TypeError(
                f"{self.name}.model: {source!r} is not a Source: it has no {', '.join(missing)}"
            )
        self._ask("model")
        name = type(source).__name__
        self._give(source, "model", name, step=f"{self.name}.model({name})")
        return source

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
        kind of source, `replay` or `real`, `model:<class>` for a model the test gave, or `none`
        for nothing."""
        if self.source_kind == "model":
            return f"model:{self.source_detail}"
        return self.source_kind or NO_SOURCE

    def _ask(self, source: str) -> None:
        if self.source is not None:
            raise SourceError(f"{self.name} has its source already: one per test")
        self.asked(Request(self.name, source=source))

    def _refuse_source(self, part: str) -> None:
        """Refuses a call to esp32tl's model of `part` (`receiver`, `modem`) on a route whose
        source is another one, saying what the route has: there is no `part` there to tell
        anything. The caller has checked that the source is not that model."""
        if self.source is None:
            return
        what = {"real": "has a real device", "model": "has a model of the test's"}.get(
            self.source_kind or "", "replays a recording"
        )
        raise SourceError(f"{self.name} {what}: it has no {part} to describe; one source per test")

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
        source: Source | Model,
        kind: str,
        detail: str,
        file: Path | None = None,
        step: str | None = None,
        prepare: Callable[[], None] | None = None,
    ) -> None:
        """`source`, of `kind`, made from `detail`, as the route's: `file` the run reads it from,
        if any, and `step` what the log says the test did, `{name}.{kind}({detail!r})` unless
        given. `prepare`, if any, reserves model resources under the source-selection lock,
        after refusing an existing source, so a losing selection cannot leave resources behind."""
        with self._giving:
            if self.source is not None:
                raise SourceError(f"{self.name} has its source already: one per test")
            if prepare is not None:
                prepare()
            self.source = source
            self.source_kind = kind
            self.source_detail = detail
            if file is not None:
                self.files[file.resolve()] = (kind, self.name)
        if self.log is not None:
            self.log.test(0, step or f"{self.name}.{kind}({detail!r})")

    @property
    def _driving(self) -> Source | None:
        """What the route calls: `source`, or a `Model` in nanoseconds, whose refusals are
        `LinkError`s naming the route."""
        source = self.source
        if not isinstance(source, Model):
            return source
        if self._adapter is None or self._adapter.model is not source:
            self._adapter = in_nanoseconds(source, self.name, LinkError)
        return self._adapter

    def instant(self, t: int) -> str:
        """`t` as the link's errors write it: a `Duration`, `0.0020005 s`, for a `Model`, which
        states its times so; nanoseconds otherwise."""
        return str(Duration(t)) if isinstance(self.source, Model) else super().instant(t)

    def receive(self, t: int, direction: Direction, data: bytes) -> Iterable[Output]:
        self._crossed(t, "out", data)
        if self._driving is None:
            return ()
        return self._sent(self._driving.receive(t, data))

    def transmitted(self, t: int, baud: int | None, data: bytes) -> None:
        """A source that refuses the rate fails the run; the bytes that did it still crossed the
        guest's wire, for the tap and the run's log."""
        if isinstance(self._driving, RateBound):
            try:
                self._driving.transmitted(t, baud)
            except Exception:
                self._crossed(t, "out", data)
                raise

    def arrive(self, now: int) -> None:
        if isinstance(self._driving, Arriving):
            self._driving.arrive(now)

    def poll(self, now: int) -> Iterable[Output]:
        return () if self._driving is None else self._sent(self._driving.poll(now))

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
        return None if self._driving is None else self._driving.quiet_until()

    def supply(self, on: bool) -> None:
        """The board's supply went off, or came back: a model of a part of the board loses its
        power with it, and gets it back. A replay or a real device is not on it, and keeps
        sending; what it sends while the chip is off is lost."""
        if isinstance(self._driving, Supplied):
            self._driving.supply(on)

    def latency(self) -> int | None:
        return None if self._driving is None else self._driving.latency()


# What `Source` asks of a model: `model` refuses one without each of them, callable.
SOURCE_METHODS = ("receive", "poll", "quiet_until", "latency")

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
