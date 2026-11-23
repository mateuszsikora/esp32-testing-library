"""A tap: what crossed one route's wire during a run, both ways, written as a recording (plan §4.3,
`docs/recording-format.md`).

The route's interface hands the tap every byte the firmware transmitted on it (`out`) and every
byte its source sent into the ESP32 (`in`), with the time it gave them, whatever the source is: a
replay, a model, a real device, or nothing. At the end of the run the backend writes
`Tap.recording`, the events in time order with a header that says what was at the far end, to the
file the test named.
"""

import hashlib
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from esp32tl import __version__
from esp32tl.build import Build
from esp32tl.interfaces.history import History
from esp32tl.recording import Direction, Event, Header, Recording

# The header's `source` for a route the test gave nothing at its far end: it heard what the
# firmware sent and said nothing.
NO_SOURCE = "none"


class Tap:
    """What crossed the wire of `interface` (`uart.gnss`), kept for the recording at `path`.

    `record` takes the events in the order they reach the interface, which is not always the
    order of their times: an answer may be stamped later than a byte the firmware transmits after
    it. `recording` puts them in time order, those of one time in the order recorded.
    """

    def __init__(self, interface: str, path: Path) -> None:
        self.interface = interface
        self.path = path
        self._events: list[tuple[int, Direction, bytes]] = []

    @property
    def events(self) -> Sequence[tuple[int, Direction, bytes]]:
        return History(self._events)

    def record(self, t: int, direction: Direction, data: bytes) -> None:
        """`data` crossed the wire at `t`, `in` to the ESP32 or `out` of it. No bytes are no
        event."""
        if data:
            self._events.append((t, direction, data))

    def recording(
        self,
        *,
        board: str,
        source: str,
        clock: str,
        app_sha256: str | None = None,
        tools: dict[str, str] | None = None,
        created: datetime | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Recording:
        """The events as a recording made on `board` with `source` at the far end, on `clock`.
        `created` is now unless given; `extra` holds the header's fields of the tap's own."""
        header = Header(
            board=board,
            interface=self.interface,
            source=source,
            clock=clock,
            created=utc_time(created or datetime.now(UTC)),
            app_sha256=app_sha256,
            tools={"esp32tl": __version__} | (tools or {}),
            extra=extra or {},
        )
        events = sorted(self.events, key=lambda event: event[0])
        return Recording(header, tuple(Event(t, d, data, clock) for t, d, data in events))


def utc_time(moment: datetime) -> str:
    """`moment` as the header's `created`: UTC, to the second."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256(path: str | os.PathLike[str]) -> str:
    """The SHA-256 of the file at `path`, as the header's `app_sha256`."""
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for block in iter(lambda: file.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


def app_image_sha256(build: Build) -> str | None:
    """The hash of the build's application image on both backends, never of an ELF or the
    bootloader: ESP-IDF's `app` entry of `flasher_args.json`, PlatformIO's `firmware.bin`."""
    return sha256(build.app_image) if build.app_image is not None else None
