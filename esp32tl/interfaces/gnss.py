"""The `gnss` fixture: the profile's `uart.gnss` route, whose far end is a replay or a receiver the
test describes, by what the test calls first, before `board.power_on()`."""

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from esp32tl.interfaces import SourceError
from esp32tl.interfaces.uart import Uart
from esp32tl.link.boardlink import Wire
from esp32tl.sources.model.gnss import HDOP, SATELLITES, GnssReceiver
from esp32tl.units import Heading, Position, Speed


class Gnss(Uart):
    """`Uart`, with the receiver model's calls: the first of them, before power-on, makes the
    route's source a `GnssReceiver`, and every later one tells that receiver. `now` gives the
    board's time in nanoseconds, `world` the world's time at power-on; the other arguments are
    `Uart`'s."""

    def __init__(
        self,
        name: str,
        wire: Wire,
        *,
        clock: str,
        directory: Path,
        now: Callable[[], int],
        world: Callable[[], datetime],
        asked: Callable[[str, str], None] = lambda name, source: None,
    ) -> None:
        super().__init__(name, wire, clock=clock, directory=directory, asked=asked)
        self._clock_now = now
        self._world = world

    def fix(self, at: Position, *, hdop: float = HDOP, satellites: int = SATELLITES) -> None:
        """A fix at `at`, standing still, of `hdop` with `satellites` in use."""
        self.receiver().fix(at, hdop=hdop, satellites=satellites)

    def move(self, *, heading: Heading, speed: Speed) -> None:
        """Moving from where the receiver is now, on `heading`, at `speed`."""
        self.receiver().move(heading=heading, speed=speed)

    def stand_still(self) -> None:
        """Standing where the receiver is now."""
        self.receiver().stand_still()

    def degrade(self, *, hdop: float | None = None, satellites: int | None = None) -> None:
        """The fix the receiver has, of another HDOP, another number of satellites, or both."""
        self.receiver().degrade(hdop=hdop, satellites=satellites)

    def lose_fix(self) -> None:
        """No fix from now on, until the next `fix`; the receiver goes on moving."""
        self.receiver().lose_fix()

    def go_silent(self) -> None:
        """No sentences from now on, until `resume`."""
        self.receiver().go_silent()

    def resume(self) -> None:
        """Sentences again, from the next epoch on."""
        self.receiver().resume()

    @property
    def position(self) -> Position | None:
        """Where the receiver is now, fix or not; `None` until its first fix."""
        return self.receiver().position

    def receiver(self) -> GnssReceiver:
        """The route's receiver: made at the first call, which must come before power-on, and
        refused on a route that replays a recording."""
        source = self.source
        if isinstance(source, GnssReceiver):
            return source
        if source is not None:
            raise SourceError(
                f"{self.name} replays a recording: it has no receiver to describe; one source"
                " per test"
            )
        self._ask("model")
        receiver = GnssReceiver(self._clock_now, self._world)
        self._give(receiver, "model")
        return receiver
