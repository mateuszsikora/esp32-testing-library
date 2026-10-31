"""Replay: a recording at the far end of an interface, with the recording's own timing (plan §4.4).

Only the stream case for now: a recording with no request in it, such as a GNSS receiver's NMEA
sentences, where every event answers the empty request and goes into the chip at its recorded
time. A recording with requests (`out` events) needs the matching engine, which comes later.
"""

from collections.abc import Iterable

from esp32tl.recording import Recording

# What a source sends into the chip: from `t`, in nanoseconds, these bytes.
Output = tuple[int, bytes]


class ReplayError(Exception):
    """A recording this replay cannot replay where it was asked to."""


class Replay:
    """`recording` replayed on `interface`, whose times are on `clock`.

    Each `in` event goes out in the link's turn at its own `t`, never earlier: `quiet_until` is
    the time of the next event not yet sent, so the runner stops there. A stream answers nothing,
    so `latency` is `None` (protocol §7), and what the firmware transmits is heard and dropped.
    After the last event the replay is silent (`end: hold`).
    """

    def __init__(self, recording: Recording, interface: str, clock: str, name: str = "") -> None:
        name = name or "the recording"
        header = recording.header
        if header.interface != interface:
            raise ReplayError(f"{name} is a recording of {header.interface}, not of {interface}")
        # The clock switches' too: a recording that leaves the clock and comes back is refused.
        clocks = sorted({line.clock for line in recording.lines} | {header.clock})
        if clocks != [clock]:
            raise ReplayError(
                f"{name} has times on the {' and '.join(clocks)} clock, and {interface} runs on"
                f" the {clock} clock: replaying it would compare times across clocks"
            )
        out = [e for e in recording.events if e.dir == "out"]
        if out:
            raise ReplayError(
                f"{name} has events out of the ESP32, the first at {out[0].t} ns: it holds"
                " requests, and replay answers none yet, only streams with events in"
            )
        if header.end != "hold":
            raise ReplayError(f"{name} ends with {header.end}: replay holds at the end only")
        self.events: tuple[Output, ...] = tuple((e.t, e.data) for e in recording.events)
        # How many of them the link has been given.
        self.sent = 0

    def receive(self, t: int, data: bytes) -> Iterable[Output]:
        """The firmware transmitted `data` at `t`: a stream does not listen."""
        return ()

    def poll(self, now: int) -> Iterable[Output]:
        """The events due at `now`: every one not sent yet whose `t` is not later."""
        start = self.sent
        while self.sent < len(self.events) and self.events[self.sent][0] <= now:
            self.sent += 1
        return self.events[start : self.sent]

    def quiet_until(self) -> int | None:
        """The time of the next event, `None` after the last."""
        return self.events[self.sent][0] if self.sent < len(self.events) else None

    def latency(self) -> int | None:
        """`None`: a stream has no request to answer."""
        return None
