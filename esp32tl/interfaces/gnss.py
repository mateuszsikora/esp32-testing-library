"""The `gnss` fixture: the profile's `uart.gnss` route, whose far end is a replay, a real device or
a receiver the test describes, by what the test calls first, before `board.power_on()`."""

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from esp32tl.boards.capabilities import Request
from esp32tl.interfaces import SourceError
from esp32tl.interfaces.uart import Uart
from esp32tl.link.boardlink import Wire
from esp32tl.report import RunLog
from esp32tl.sources.model.gnss import HDOP, SATELLITES, GnssReceiver
from esp32tl.units import Heading, Position, Speed


class Gnss(Uart):
    """`Uart`, with the receiver model's calls: the first of them, before power-on, makes the
    route's source a `GnssReceiver`, and every later one tells that receiver. `now` gives the
    board's time in nanoseconds, `world` the world's time at power-on; the other arguments are
    `Uart`'s. Each call goes into the `log` as a step of the test, at the board's time."""

    # What the route's report says the source was made from, and a tap's recording names it by.
    DETAIL = "a GNSS receiver"
    MODEL = "model:gnss"

    def __init__(
        self,
        name: str,
        wire: Wire,
        *,
        clock: str | Callable[[], str],
        directory: Path,
        now: Callable[[], int],
        world: Callable[[], datetime],
        asked: Callable[[Request], None] = lambda request: None,
        log: RunLog | None = None,
        files: dict[Path, tuple[str, str]] | None = None,
    ) -> None:
        super().__init__(
            name, wire, clock=clock, directory=directory, asked=asked, log=log, files=files
        )
        self._clock_now = now
        self._world = world

    def fix(self, at: Position, *, hdop: float = HDOP, satellites: int = SATELLITES) -> None:
        """A fix at `at`, standing still, of `hdop` with `satellites` in use."""
        self._tell(
            lambda receiver: receiver.fix(at, hdop=hdop, satellites=satellites),
            f"fix(at={at!r}, hdop={hdop!r}, satellites={satellites!r})",
        )

    def move(self, *, heading: Heading, speed: Speed) -> None:
        """Moving from where the receiver is now, on `heading`, at `speed`."""
        self._tell(
            lambda receiver: receiver.move(heading=heading, speed=speed),
            f"move(heading={heading!r}, speed={speed!r})",
        )

    def stand_still(self) -> None:
        """Standing where the receiver is now."""
        self._tell(GnssReceiver.stand_still, "stand_still()")

    def degrade(self, *, hdop: float | None = None, satellites: int | None = None) -> None:
        """The fix the receiver has, of another HDOP, another number of satellites, or both."""
        self._tell(
            lambda receiver: receiver.degrade(hdop=hdop, satellites=satellites),
            f"degrade(hdop={hdop!r}, satellites={satellites!r})",
        )

    def lose_fix(self) -> None:
        """No fix from now on, until the next `fix`; the receiver goes on moving."""
        self._tell(GnssReceiver.lose_fix, "lose_fix()")

    def go_silent(self) -> None:
        """No sentences from now on, until `resume`."""
        self._tell(GnssReceiver.go_silent, "go_silent()")

    def resume(self) -> None:
        """Sentences again, from the epoch at this instant on."""
        self._tell(GnssReceiver.resume, "resume()")

    def recorded_source(self) -> str:
        """`model:gnss` for the receiver model, as the recording format names a model."""
        return self.MODEL if self.source_kind == "model" else super().recorded_source()

    @property
    def position(self) -> Position | None:
        """Where the receiver is now, fix or not; `None` until its first fix. Reading it makes
        no receiver: without one, a `SourceError`."""
        return self.receiver().position

    def receiver(self) -> GnssReceiver:
        """The route's receiver, once a model call has made it; a `SourceError` before, or on a
        route that replays a recording."""
        source = self.source
        if isinstance(source, GnssReceiver):
            return source
        self._refuse_replay()
        raise SourceError(f"{self.name} has no receiver: give it one with gnss.fix(at=...)")

    def _tell(self, call: Callable[[GnssReceiver], None], step: str) -> None:
        """`call` on the route's receiver, logged as `step`. The first one, which must come
        before power-on, makes the receiver, and gives it to the route only once `call` has
        succeeded: a call refused leaves the route as it was."""
        step = f"{self.name}.{step}"
        source = self.source
        if isinstance(source, GnssReceiver):
            call(source)
            if self.log is not None:
                self.log.test(self._clock_now(), step)
            return
        self._refuse_replay()
        # First: it refuses a source after power-on, and skips a backend that gives no model.
        self._ask("model")
        receiver = GnssReceiver(self._clock_now, self._world)
        call(receiver)
        self._give(receiver, "model", self.DETAIL, step=step)

    def _refuse_replay(self) -> None:
        if self.source is not None:
            what = "has a real device" if self.source_kind == "real" else "replays a recording"
            raise SourceError(
                f"{self.name} {what}: it has no receiver to describe; one source per test"
            )
