"""An NTP server telling the world's time: a UTC instant at the board's power-on, plus the board's
clock."""

import struct
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from esp32tl.interfaces.condition import Condition, FirstFrom
from esp32tl.units import Duration

NTP_EPOCH = datetime(1900, 1, 1, tzinfo=UTC)
DEFAULT_UTC = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


@dataclass(frozen=True)
class NtpExchange:
    """One NTP request the device sent, at the board's time, and whether it was answered.

    `at` is when the request reached the server, when it came off the path up: without a path,
    when the link carried it. `answered` is `False` for a request that came while the server was
    silent."""

    at: Duration
    answered: bool


class Ntp:
    """`now` is the board's clock; `utc_at_power_on` the world's UTC at the board's power-on.

    `exchanges` keeps every well-formed client request, NTP v3 or v4 in client mode, in the order
    it came, answered or not; a datagram that is not one is neither answered nor kept."""

    def __init__(
        self,
        now: Callable[[], int],
        utc_at_power_on: Callable[[], datetime] = lambda: DEFAULT_UTC,
        log: Callable[[str], None] = lambda line: None,
    ) -> None:
        self._now = now
        self._world = utc_at_power_on
        self._log = log
        self.exchanges: list[NtpExchange] = []
        self._silent = False

    @property
    def requests(self) -> int:
        """How many requests were answered: the `exchanges` with `answered`."""
        return sum(exchange.answered for exchange in self.exchanges)

    def next_exchange(self, *, answered: bool | None = None) -> Callable[[], NtpExchange | None]:
        """A non-consuming condition for the first request recorded after this call; with
        `answered`, the first that was answered (`True`) or not (`False`). The boundary is append
        order, even at equal times, and polling never moves it."""
        if answered is not None and not isinstance(answered, bool):
            raise TypeError("ntp: answered takes True, False or None")
        description = {
            None: "the next NTP request",
            True: "the next answered NTP request",
            False: "the next unanswered NTP request",
        }[answered]
        find = FirstFrom(
            lambda: self.exchanges,
            len(self.exchanges),
            lambda exchange: answered is None or exchange.answered is answered,
        )
        return Condition(find, description, "NTP")

    def go_silent(self) -> None:
        """Stop answering new requests until `resume`, without moving the world's clock."""
        if not self._silent:
            self._silent = True
            self._log("ntp: silent")

    def resume(self) -> None:
        """Answer new requests with the current world time; missed requests are not replayed."""
        if self._silent:
            self._silent = False
            self._log("ntp: resumed")

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
        self.exchanges.append(NtpExchange(Duration(self._now()), not self._silent))
        if self._silent:
            return None
        stamp = self._stamp()
        header = struct.pack(">BBbbII4s", (0 << 6) | (4 << 3) | 4, 1, 0, -20, 0, 0x10, b"ESPT")
        self._log("ntp: answered")
        return header + stamp + data[40:48] + stamp + stamp
