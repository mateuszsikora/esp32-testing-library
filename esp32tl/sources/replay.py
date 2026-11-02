"""Replay a recording on one interface and clock, through the matching engine (plan §4.4)."""

from esp32tl.matching import Matching, Protocol
from esp32tl.recording import Recording

# What a source sends into the chip: from `t`, in nanoseconds, these bytes.
Output = tuple[int, bytes]


class ReplayError(Exception):
    """A recording this replay cannot replay where it was asked to."""


class Replay(Matching):
    """A matching engine with the route and clock checks a source needs."""

    def __init__(
        self,
        recording: Recording,
        interface: str,
        clock: str,
        name: str = "",
        *,
        protocol: Protocol | None = None,
    ) -> None:
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
        try:
            super().__init__(recording, protocol)
        except ValueError as error:
            raise ReplayError(f"{name}: {error}") from None
