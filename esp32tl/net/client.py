"""The test's side of a TCP connection it opens to the device: `Internet.connect(port)` returns a
`TcpConnection`, which writes and closes, keeps what the device sent, and gives conditions for
`board.wait_until` on what happens to it.

The connection comes from `10.64.0.1`, from an ephemeral port that is never reused, 49152 first,
so runs repeat. Its conditions fail loudly: one that can no longer hold, a connection refused when
the test waits for it to be established, raises `ConnectionEnded` at once rather than wait out its
time.
"""

from collections.abc import Callable

from esp32tl.interfaces.condition import Condition
from esp32tl.units import Duration


class ConnectionEnded(AssertionError):
    """What the test asked of its TCP connection can no longer happen: the device refused or reset
    it, closed it first, or it was lost: the link went down, or the device reset or lost its
    power."""


class NotReachable(RuntimeError):
    """`network.connect` before the network reaches the device: the link is down, the device has
    no address yet, or the station has sent no frame to learn its MAC from."""


# How each end reads in a sentence.
_HOW = {
    "closed": "closed",
    "refused": "refused by the device",
    "reset": "reset by the device",
    "lost": "lost with the link",
}


class TcpConnection:
    """One connection the test opened to `address`:`port` from `local_port`, at `at`.

    `state` is `"connecting"` until the device answers, then `"established"`, and at the end
    `"closed"` (both ends closed), `"refused"` (the device answered the SYN with a RST), `"reset"`
    (a RST later) or `"lost"` (the link went down, or the device reset or lost its power).
    `received` holds every byte the device sent, in order. Calls run while the board is held;
    the network calls back during the board's turns."""

    def __init__(
        self,
        address: str,
        port: int,
        local: str,
        identity: str,
        now: Callable[[], int],
        record: Callable[[str, bytes], None],
    ) -> None:
        self.address, self.port, self.local = address, port, local
        self.local_port = 0
        self.identity = identity
        self.at = Duration(now())
        self._now = now
        self._record = record
        self._write: Callable[[bytes], None] = lambda data: None
        self._close: Callable[[], None] = lambda: None
        self._received = bytearray()
        self._observers: list[Callable[[], None]] = []
        self._closing = False
        self._end: str | None = None
        self._why: str | None = None  # why a lost connection was lost, unless with the link
        self.established_at: Duration | None = None
        self.closed_at: Duration | None = None
        self.ended_at: Duration | None = None

    def _opened(
        self, local_port: int, write: Callable[[bytes], None], close: Callable[[], None]
    ) -> None:
        self.local_port, self._write, self._close = local_port, write, close

    @property
    def name(self) -> str:
        """`TCP 10.64.0.1:49152 → 10.64.0.2:80`, as the network's log names it."""
        return f"TCP {self.local}:{self.local_port} → {self.address}:{self.port}"

    def __repr__(self) -> str:
        return f"<{self.name}, {self.state}>"

    @property
    def state(self) -> str:
        if self._end is not None:
            return self._end
        return "connecting" if self.established_at is None else "established"

    @property
    def _how(self) -> str:
        """How the connection ended, in a sentence: `refused by the device`, `lost when the device
        reset`."""
        assert self._end is not None
        if self._end == "lost" and self._why is not None:
            return f"lost when {self._why}"
        return _HOW[self._end]

    @property
    def received(self) -> bytes:
        """Every byte the device sent on this connection so far."""
        return bytes(self._received)

    @property
    def text(self) -> str:
        """`received` as UTF-8, with replacement."""
        return self._received.decode(errors="replace")

    # The test's side.

    def write(self, data: bytes | str) -> None:
        """Send `data` to the device, `str` as UTF-8: onto the network's path now, if the connection
        is established and the device's window has room, else as soon as it does; the transcript
        has each piece when it goes. Raises `ConnectionEnded` once the
        connection has ended or the device has closed it, which closes the test's end too, and
        `ValueError` after `close()`."""
        if isinstance(data, str):
            data = data.encode()
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError(f"{self.name}: write takes bytes or str, not {data!r}")
        if self._end is not None:
            raise ConnectionEnded(f"{self.name} was {self._how}: nothing more reaches the device")
        if self.closed_at is not None:
            raise ConnectionEnded(
                f"{self.name} was closed by the device, which closed the test's end too"
            )
        if self._closing:
            raise ValueError(f"{self.name}: written after close()")
        if not data:
            return
        self._write(bytes(data))

    def close(self) -> None:
        """Close the test's end: a FIN after what is written. Once is enough; later calls, and a
        call after the connection ended, do nothing."""
        if self._closing or self._end is not None:
            return
        self._closing = True
        self._close()

    # Conditions.

    def established(self) -> Condition[Duration | None]:
        """A condition returning when the device accepted the connection. Raises
        `ConnectionEnded` once the device refused it, or it was lost (the link down, the device
        reset or without power) before the device answered."""

        def find() -> Duration | None:
            if self.established_at is not None:
                return self.established_at
            if self._end is not None:
                raise ConnectionEnded(f"{self.name} was {self._how} before it was established")
            return None

        return Condition(find, f"{self.name} established", "TCP")

    def closed(self) -> Condition[Duration | None]:
        """A condition returning when the device closed its end, a FIN, whoever closed first.
        Raises `ConnectionEnded` once the connection ended without it: refused, reset or lost."""

        def find() -> Duration | None:
            if self.closed_at is not None:
                return self.closed_at
            if self._end is not None:
                raise ConnectionEnded(f"{self.name} was {self._how}, not closed by the device")
            return None

        return Condition(find, f"{self.name} closed by the device", "TCP")

    def refused(self) -> Condition[Duration | None]:
        """A condition returning when the device answered the connection's SYN with a RST. Raises
        `ConnectionEnded` once the device accepted it instead, or it was lost first."""
        return self._ended_by("refused")

    def reset(self) -> Condition[Duration | None]:
        """A condition returning when the device reset the established connection. Raises
        `ConnectionEnded` once it ended another way: closed, refused or lost."""
        return self._ended_by("reset")

    def _ended_by(self, kind: str) -> Condition[Duration | None]:
        def find() -> Duration | None:
            if self._end == kind:
                return self.ended_at
            if self._end is not None:
                raise ConnectionEnded(f"{self.name} was {self._how}, not {_HOW[kind]}")
            if kind == "refused" and self.established_at is not None:
                raise ConnectionEnded(f"{self.name} was established, not {_HOW[kind]}")
            return None

        return Condition(find, f"{self.name} {_HOW[kind]}", "TCP")

    def next_data(
        self,
        until: bytes | str | None = None,
        *,
        size: int | None = None,
        start: int | None = None,
    ) -> Condition[bytes | None]:
        """A condition on the bytes the device sends after `start`, an offset into `received`,
        which is where `received` ends at this call unless given. With `until`, it returns them up
        to and including the first `until` among them, once that has arrived; with `size`, the
        first `size` of them, once they have; with neither, whatever has arrived, once anything
        has. Raises `ConnectionEnded` once the device can send no more and it has not: the
        connection closed by the device, or ended."""
        if isinstance(until, str):
            until = until.encode()
        if until is not None and (not isinstance(until, bytes) or not until):
            raise ValueError(f"{self.name}: next_data's until is {until!r}, not bytes to look for")
        if size is not None and (isinstance(size, bool) or not isinstance(size, int) or size < 1):
            raise ValueError(f"{self.name}: next_data's size is {size!r}, not a count of bytes")
        if until is not None and size is not None:
            raise ValueError(f"{self.name}: next_data takes until or size, not both")
        if start is None:
            start = len(self._received)
        elif isinstance(start, bool) or not isinstance(start, int):
            raise TypeError(f"{self.name}: next_data's start is {start!r}, not an offset")
        elif not 0 <= start <= len(self._received):
            raise ValueError(
                f"{self.name}: next_data's start is {start}, outside the"
                f" {len(self._received)} bytes received"
            )
        if until is not None:
            wanted = f" up to {until!r}"
        elif size is not None:
            wanted = f", {size} bytes"
        else:
            wanted = ""
        after = f" after byte {start}" if start else ""
        # Where the next search for `until` begins: after a miss, `len(until) - 1` bytes before the
        # end, where an `until` split across two arrivals would begin; after a match, the match.
        searched = start

        def find() -> bytes | None:
            # Searched where it lies, and copied only for a match: a long stream is not copied at
            # every poll.
            nonlocal searched
            received = self._received
            if until is not None:
                at = received.find(until, searched)
                if at >= 0:
                    searched = at
                    return bytes(received[start : at + len(until)])
                searched = max(searched, len(received) - len(until) + 1)
            elif size is not None:
                if len(received) - start >= size:
                    return bytes(received[start : start + size])
            elif len(received) > start:
                return bytes(received[start:])
            if self._end is not None or self.closed_at is not None:
                how = self._how if self._end is not None else "closed by the device"
                if until is not None:
                    missing = repr(until)
                elif size is not None:
                    missing = f"{size} bytes, only {len(received) - start}"
                else:
                    missing = "anything more"
                raise ConnectionEnded(f"{self.name} was {how} before the device sent {missing}")
            return None

        return Condition(find, f"the next data on {self.name}{after}{wanted}", "TCP")

    # The network's side, during the board's turns.

    def on_established(self) -> None:
        self.established_at = Duration(self._now())

    def on_sent(self, data: bytes) -> None:
        self._record("in", data)

    def on_data(self, data: bytes) -> None:
        self._record("out", data)
        self._received += data
        self._notify()

    def on_close(self) -> None:
        if self._end is None and self.closed_at is None:
            self.closed_at = Duration(self._now())
            self._notify()

    def on_end(self, kind: str, why: str | None = None) -> None:
        self._end, self._why = kind, why
        self.ended_at = Duration(self._now())
        self._notify()

    def _notify(self) -> None:
        for observer in self._observers:
            observer()
