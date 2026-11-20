"""The test's side of a TCP connection it opens to the device: `Internet.connect(port)` returns a
`TcpConnection`, which writes and closes, keeps what the device sent, and gives conditions for
`board.wait_until` on what happens to it.

The connection comes from `10.64.0.1`, from an ephemeral port that is never reused, 49152 first,
so runs repeat. Its conditions fail loudly: one that can no longer hold, a connection refused when
the test waits for it to be established, raises `ConnectionEnded` at once rather than wait out its
time; and a wait on one that times out while the connection is still connecting says which SYNs
got no answer.
"""

from collections.abc import Callable

from esp32tl.interfaces.condition import Condition
from esp32tl.net.ip import SYN_RETRIES
from esp32tl.units import Duration


class ConnectionEnded(AssertionError):
    """What the test asked of its TCP connection can no longer happen: the device refused or reset
    it, closed it first, it timed out with no answer to its SYNs, or it was lost: the link went
    down, or the device reset or lost its power."""


class NotReachable(RuntimeError):
    """`network.connect` before the network reaches the device: the link is down, the device has
    no address yet, or the station has sent no frame to learn its MAC from."""


# How each end reads in a sentence.
_HOW = {
    "closed": "closed",
    "refused": "refused by the device",
    "reset": "reset by the device",
    "lost": "lost with the link",
    "timed out": f"timed out with no answer to its {1 + SYN_RETRIES} SYNs",
}


def _listed(times: list[Duration]) -> str:
    """`0.957 s`, `0.957 s and 1.957 s`, `0.957 s, 1.957 s and 3.957 s`."""
    shown = [str(t) for t in times]
    return shown[0] if len(shown) == 1 else f"{', '.join(shown[:-1])} and {shown[-1]}"


class TcpConnection:
    """One connection the test opened to `address`:`port` from `local_port`, at `at`.

    `state` is `"connecting"` until the device answers, then `"established"`, and at the end
    `"closed"` (both ends closed), `"refused"` (the device answered the SYN with a RST), `"reset"`
    (a RST later), `"lost"` (the link went down, or the device reset or lost its power) or
    `"timed out"` (no answer to any of its SYNs). `syns` holds when each SYN went, and `received`
    every byte the device sent, in order. Calls run while the board is held; the network calls
    back during the board's turns."""

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
        self.syns: list[Duration] = []
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

    def unanswered(self) -> str | None:
        """While the connection is connecting, what its SYNs got: each SYN sent again says that the
        one before it got no answer in its RTO, and the last one has had none so far. `the SYN at
        1.032 s has had no answer in 0.5 s (no SYN-ACK, no RST)`; `the SYNs at 1 s and 2 s got no
        answer (no SYN-ACK, no RST), and the SYN at 4 s has just gone` at the instant the last one
        went. `None` once the device answered or the connection ended. A wait on any of its
        conditions that times out says it."""
        if self.state != "connecting" or not self.syns:
            return None
        *before, last = self.syns
        age = Duration(self._now() - last.ns)
        if age.ns:
            now = f"has had no answer in {age}" if not before else f"has had none in {age}"
        else:
            now = "has just gone"
        if not before:
            return f"the SYN at {last} {now}" + (" (no SYN-ACK, no RST)" if age.ns else "")
        which = "SYN" if len(before) == 1 else "SYNs"
        return (
            f"the {which} at {_listed(before)} got no answer (no SYN-ACK, no RST),"
            f" and the SYN at {last} {now}"
        )

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

    # Conditions. What has happened to the connection, once and for good, is a property named for
    # it, `established`, that returns when it happened however long ago; the next bytes are
    # `next_data(...)`, a method, since the call is where it starts looking.

    @property
    def established(self) -> Condition[Duration | None]:
        """A condition returning when the device accepted the connection. Raises
        `ConnectionEnded` once the device refused it, or it was lost (the link down, the device
        reset or without power) or timed out before the device answered."""

        def find() -> Duration | None:
            if self.established_at is not None:
                return self.established_at
            if self._end is not None:
                raise ConnectionEnded(f"{self.name} was {self._how} before it was established")
            return None

        return Condition(find, f"{self.name} established", "TCP", self.unanswered)

    @property
    def closed(self) -> Condition[Duration | None]:
        """A condition returning when the device closed its end, a FIN, whoever closed first.
        Raises `ConnectionEnded` once the connection ended without it: refused, reset or lost."""

        def find() -> Duration | None:
            if self.closed_at is not None:
                return self.closed_at
            if self._end is not None:
                raise ConnectionEnded(f"{self.name} was {self._how}, not closed by the device")
            return None

        return Condition(find, f"{self.name} closed by the device", "TCP", self.unanswered)

    @property
    def refused(self) -> Condition[Duration | None]:
        """A condition returning when the device answered the connection's SYN with a RST. Raises
        `ConnectionEnded` once the device accepted it instead, or it was lost first."""
        return self._ended_by("refused")

    @property
    def reset(self) -> Condition[Duration | None]:
        """A condition returning when the device reset the established connection. Raises
        `ConnectionEnded` once it ended another way: closed, refused, lost or timed out."""
        return self._ended_by("reset")

    @property
    def timed_out(self) -> Condition[Duration | None]:
        """A condition returning when the connection timed out with no answer to any of its SYNs,
        127 s after the first. Raises `ConnectionEnded` once the device answered instead,
        accepting or refusing it, or it was lost first."""
        return self._ended_by("timed out")

    def _ended_by(self, kind: str) -> Condition[Duration | None]:
        def find() -> Duration | None:
            if self._end == kind:
                return self.ended_at
            if self._end is not None:
                raise ConnectionEnded(f"{self.name} was {self._how}, not {_HOW[kind]}")
            if kind in ("refused", "timed out") and self.established_at is not None:
                raise ConnectionEnded(f"{self.name} was established, not {_HOW[kind]}")
            return None

        return Condition(find, f"{self.name} {_HOW[kind]}", "TCP", self.unanswered)

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

        return Condition(
            find, f"the next data on {self.name}{after}{wanted}", "TCP", self.unanswered
        )

    # The network's side, during the board's turns.

    def on_syn(self) -> None:
        self.syns.append(Duration(self._now()))

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
