"""A UART of the board profile, `uart.<role>`: what is at the far end of its route, as the link
drives it. The `gnss` fixture is the one of `uart.gnss`."""

import os
import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Protocol

from esp32tl import recording
from esp32tl.interfaces import SourceError
from esp32tl.link.boardlink import Direction, Interface, Output, Wire
from esp32tl.sources.replay import Replay


class Source(Protocol):
    """What drives a UART from its far end: a replay now, a model or a real device later. The
    methods are those of the link's `Interface`, without the wire."""

    def receive(self, t: int, data: bytes) -> Iterable[Output]: ...

    def poll(self, now: int) -> Iterable[Output]: ...

    def quiet_until(self) -> int | None: ...

    def latency(self) -> int | None: ...


class Uart(Interface):
    """The interface `name` (`uart.gnss`) on `wire`, whose source the test gives in its body.

    `declared` is the source the test's `source` marker names for it, `None` without one; the
    backend puts only a declared UART on the link. Until the source is given the UART hears
    everything and says nothing, and the backend holds the run before the guest boots, so the
    source's first event can be at 0 ns. `clock` is the backend's: a recording on another one is
    refused. A relative path is taken from `directory`, the test file's. `on_source` is called
    with the name once the source is given.
    """

    def __init__(
        self,
        name: str,
        wire: Wire,
        *,
        declared: str | None,
        clock: str,
        directory: Path,
        on_source: Callable[[str], None] = lambda name: None,
    ) -> None:
        super().__init__(name, wire)
        self.declared = declared
        self.clock = clock
        self.directory = directory
        self.on_source = on_source
        self.source: Source | None = None
        self._giving = threading.Lock()

    def replay(self, path: str | os.PathLike[str]) -> Replay:
        """Replays the recording at `path`, relative to the test file's directory, into the
        chip, at its recorded times: the stream case, a recording with no requests. Returns the
        `Replay`, whose `sent` counts the events the link has been given."""
        if self.declared != "replay":
            raise SourceError(
                f"{self.name} has no source marker that names replay: mark the test"
                f' @pytest.mark.source("{self.name}", "replay") to replay into it'
            )
        file = self.directory / path
        replay = Replay(recording.read(file), self.name, self.clock, name=str(path))
        self._give(replay)
        return replay

    def _give(self, source: Source) -> None:
        with self._giving:
            if self.source is not None:
                raise SourceError(f"{self.name} has its source already: one per test")
            self.source = source
        self.on_source(self.name)

    def receive(self, t: int, direction: Direction, data: bytes) -> Iterable[Output]:
        return () if self.source is None else self.source.receive(t, data)

    def poll(self, now: int) -> Iterable[Output]:
        return () if self.source is None else self.source.poll(now)

    def quiet_until(self) -> int | None:
        return None if self.source is None else self.source.quiet_until()

    def latency(self) -> int | None:
        return None if self.source is None else self.source.latency()
