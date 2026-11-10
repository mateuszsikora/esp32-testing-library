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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pytest_embedded_idf.app import IdfApp

from esp32tl import __version__
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
        self.events: list[tuple[int, Direction, bytes]] = []

    def record(self, t: int, direction: Direction, data: bytes) -> None:
        """`data` crossed the wire at `t`, `in` to the ESP32 or `out` of it. No bytes are no
        event."""
        if data:
            self.events.append((t, direction, data))

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


def app_image_sha256(app: IdfApp) -> str | None:
    """The app entry's binary hash on both backends, never an ELF or bootloader hash."""
    app_bin = app.flash_args.get("app", {}).get("file")
    return sha256(Path(app.binary_path or "") / app_bin) if app_bin else None
