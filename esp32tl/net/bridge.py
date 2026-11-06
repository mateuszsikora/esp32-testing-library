"""A bridge from the simulated network to a real service: each TCP connection the device opens to
the bridged port is one the harness opens to a host address, with the bytes relayed both ways.

The service answers in wall time, so a run with a bridge is on the wall clock from power-on, as a
run with a real source is (protocol §12.4). Threads connect to the service, read it and write it,
so that no turn waits on it; at each turn the network hands over what the service sent since the
last one, stamped with the turn's time, and while a connection is open it renews its turn at the
next whole emulated millisecond: the service does not say when it will next send.
"""

import contextlib
import queue
import socket
import threading
from collections.abc import Callable

from esp32tl.net.ip import Connection

MS = 1_000_000

# How long, in wall seconds, connecting to the service may take before the device's connection is
# closed.
CONNECT_SECONDS = 5

# The most one read of the service's socket takes.
READ_BYTES = 65536


def address(to: object, call: str) -> tuple[str, int]:
    """`to` checked as a host and a TCP port, `("127.0.0.1", 1883)`; `call` names the call."""
    if (
        not isinstance(to, tuple)
        or len(to) != 2
        or not isinstance(to[0], str)
        or not to[0]
        or isinstance(to[1], bool)
        or not isinstance(to[1], int)
    ):
        raise TypeError(f"{call}: to is {to!r}, not a host and a port, as ('127.0.0.1', 1883)")
    if not 1 <= to[1] <= 65535:
        raise ValueError(f"{call}: the port of {to!r} is not 1 to 65535")
    return to[0], to[1]


class Bridge:
    """What listens on the bridged TCP port of the simulated network: the service at `to`, a host
    and a port, gets one connection for each the device opens. `log` gets the network's lines.
    `connections` counts the connections the device opened."""

    def __init__(self, port: int, to: tuple[str, int], log: Callable[[str], None]) -> None:
        self.port = port
        self.to = to
        self.connections = 0
        self._log = log
        # The connections the turns still look at, and every one, for `close`.
        self._open: list[_Relay] = []
        self._all: list[_Relay] = []

    def accept(
        self, peer: tuple[str, int], write: Callable[[bytes], None], close: Callable[[], None]
    ) -> Connection:
        self.connections += 1
        relay = _Relay(f"{self.port}/{self.connections}", self.to, write, close)
        self._open.append(relay)
        self._all.append(relay)
        return relay

    def quiet_until(self, now: int) -> int | None:
        """The next whole emulated millisecond after `now` while a connection is open at the
        device's end, since the service may send on it at any time; else none."""
        return (now // MS + 1) * MS if any(r.open for r in self._open) else None

    def pump(self) -> None:
        """In a turn: what each service sent since the last turn, in the order it came, and the
        lines saying what happened to its connection; a connection the service ended is closed
        at the device's end after its last bytes. A connection the device closed is looked at
        until the service ends it too, so that what the service sent after it is logged."""
        for relay in list(self._open):
            for line in relay.pump():
                self._log(line)
            if relay.done:
                self._open.remove(relay)

    def close(self) -> None:
        """Ends every connection to the service and waits for its threads: the run is over. What
        the service sent and no turn handed over is not handed over. Closing twice does nothing."""
        for relay in self._all:
            relay.end()
        for relay in self._all:
            relay.join()
        self._open.clear()


class _Relay:
    """One connection: the device's, in the simulated network, and the harness's to the service.
    The turns call `on_data`, `on_close` and `pump`; one thread connects and writes, another
    reads, and they leave what they have for the next turn under `_lock`."""

    def __init__(
        self,
        name: str,
        to: tuple[str, int],
        write: Callable[[bytes], None],
        close: Callable[[], None],
    ) -> None:
        self.name = name
        self.to = to
        self._service = f"{to[0]}:{to[1]}"
        self._write = write
        self._close = close
        self._lock = threading.Lock()
        # What the threads have for the next turn, in order: bytes the service sent, and lines.
        self._arrived: list[bytes | str] = []
        # Whether the service ended the connection, or it failed: the turn closes the device's end.
        self._ended = False
        # Whether the device's end is closed: by the device, by the network or by this side. Set
        # by the turns under `_lock`; from then on the reader drops what the service sends, and
        # counts it in `_dropped` for the next turn's log.
        self._device_closed = False
        self._dropped = 0
        # Whether the writer still writes: once it has failed, what the device sends is dropped.
        self._writing = True
        self._closing = threading.Event()
        self._socket: socket.socket | None = None
        # What the device sent and the service has not taken yet; None ends the writing.
        self._outgoing: queue.SimpleQueue[bytes | None] = queue.SimpleQueue()
        self._reader: threading.Thread | None = None
        self._writer = threading.Thread(target=self._run, daemon=True)
        self._writer.start()

    # The turns' side.

    def on_data(self, data: bytes) -> None:
        if self._writing:
            self._outgoing.put(data)

    def on_close(self) -> None:
        """The device's end is closed, both ways, as the network's TCP closes it on a FIN: what
        the device sent goes to the service, which then reads the end of the stream. What the
        service sends after it reaches nobody, and the turns log how much."""
        with self._lock:
            self._device_closed = True
        self._outgoing.put(None)

    def pump(self) -> list[str]:
        """Hands what came over to the device, and returns the lines for the network's log."""
        with self._lock:
            arrived, self._arrived = self._arrived, []
            ended = self._ended
            closed = self._device_closed
            dropped, self._dropped = self._dropped, 0
        lines = []
        for item in arrived:
            if isinstance(item, str):
                lines.append(item)
            elif closed:
                dropped += len(item)
            else:
                self._write(item)
        if dropped:
            lines.append(
                f"bridge: {self.name} dropped {dropped} bytes {self._service} sent after the"
                " device closed its end"
            )
        if ended and not closed:
            with self._lock:
                self._device_closed = True
            self._close()
        return lines

    @property
    def open(self) -> bool:
        """Whether the device's end is open: the service's bytes still reach it."""
        with self._lock:
            return not self._device_closed

    @property
    def done(self) -> bool:
        """Whether no turn has anything more to log or hand over on this connection: both ends
        are closed, and what came before is taken."""
        with self._lock:
            return self._device_closed and self._ended and not self._arrived and not self._dropped

    # The threads' side.

    def _note(self, item: bytes | str, *, ended: bool = False) -> None:
        with self._lock:
            if self._ended:
                return
            if isinstance(item, bytes) and self._device_closed:
                self._dropped += len(item)
            else:
                self._arrived.append(item)
            self._ended = ended

    def _run(self) -> None:
        try:
            connection = socket.create_connection(self.to, timeout=CONNECT_SECONDS)
        except OSError as error:
            if not self._closing.is_set():
                self._note(
                    f"bridge: {self.name} cannot connect to {self._service}: {_reason(error)}",
                    ended=True,
                )
            return
        with self._lock:
            if self._closing.is_set():
                connection.close()
                return
            self._socket = connection
        connection.settimeout(None)
        self._note(f"bridge: {self.name} connected to {self._service}")
        self._reader = threading.Thread(target=self._read, args=(connection,), daemon=True)
        self._reader.start()
        while (data := self._outgoing.get()) is not None:
            try:
                connection.sendall(data)
            except OSError as error:
                self._writing = False
                if not self._closing.is_set():
                    self._note(
                        f"bridge: {self.name} cannot write to {self._service}: {_reason(error)}",
                        ended=True,
                    )
                break
        # The device closed its end: the service reads the end of the stream, as from the device.
        with contextlib.suppress(OSError):
            connection.shutdown(socket.SHUT_WR)

    def _read(self, connection: socket.socket) -> None:
        try:
            while data := connection.recv(READ_BYTES):
                self._note(data)
            if not self._closing.is_set():
                self._note(f"bridge: {self.name} closed by {self._service}", ended=True)
        except OSError as error:
            if not self._closing.is_set():
                self._note(
                    f"bridge: {self.name} lost {self._service}: {_reason(error)}", ended=True
                )
        finally:
            connection.close()

    def end(self) -> None:
        """Ends both threads: the run is over."""
        with self._lock:
            self._closing.set()
            connection = self._socket
        self._outgoing.put(None)
        if connection is not None:
            # Wakes the reader, which closes the socket.
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)

    def join(self) -> None:
        # A connect in progress ends within CONNECT_SECONDS.
        self._writer.join()
        if self._reader is not None:
            self._reader.join()


def _reason(error: OSError) -> str:
    return error.strerror or str(error) or type(error).__name__
