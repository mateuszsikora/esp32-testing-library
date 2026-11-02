"""An NTP server telling the world's time: a UTC instant at the board's power-on, plus the board's
clock."""

import struct
from collections.abc import Callable
from datetime import UTC, datetime

NTP_EPOCH = datetime(1900, 1, 1, tzinfo=UTC)
DEFAULT_UTC = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


class Ntp:
    def __init__(
        self,
        now: Callable[[], int],
        utc_at_power_on: Callable[[], datetime] = lambda: DEFAULT_UTC,
        log: Callable[[str], None] = lambda line: None,
    ) -> None:
        self._now = now
        self._world = utc_at_power_on
        self._log = log
        self.requests = 0

    def _stamp(self) -> bytes:
        delta = self._world() - NTP_EPOCH
        ns = (
            (delta.days * 86400 + delta.seconds) * 1_000_000_000
            + delta.microseconds * 1000
            + self._now()
        )
        seconds, rest = divmod(ns, 1_000_000_000)
        return struct.pack(">II", seconds & 0xFFFFFFFF, (rest << 32) // 1_000_000_000)

    def on_datagram(self, data: bytes) -> bytes | None:
        if len(data) < 48 or data[0] & 7 != 3 or (data[0] >> 3) & 7 not in (3, 4):
            return None
        self.requests += 1
        stamp = self._stamp()
        header = struct.pack(">BBbbII4s", (0 << 6) | (4 << 3) | 4, 1, 0, -20, 0, 0x10, b"ESPT")
        self._log("ntp: answered")
        return header + stamp + data[40:48] + stamp + stamp
