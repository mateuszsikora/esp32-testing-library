"""A real device on a host serial port, at the far end of an emulated route (protocol §12.3).

The device sends when it sends, in wall time, and cannot be held in a turn or say when it will next
send. A thread reads the port, so that the operating system's buffer does not fill while a turn is
held, and another writes it, so that a burst from the firmware does not hold the turn. At each turn
the source hands over what the port gave since the last one, stamped with the turn's time, and it
renews its turn at the next whole emulated millisecond. It keeps its line at most `backlog_ns`
behind, the newest bytes, and counts the ones it drops in `dropped`; what the device sent before the
run's first turn is no loss of the run's, and is not counted. What the firmware transmits goes to
the port's queue as soon as the link hands it over.
"""

import queue
import termios
import threading
from collections.abc import Iterable

import serial

from esp32tl.interfaces import SourceError

MS = 1_000_000

# One character on the line at 1 baud, in nanoseconds: ten bit times, 8N1. The bytes a time carries
# and the time some bytes take are the same sum, either way round: `_carried` and `_on_the_line`.
CHAR_NS_AT_1_BAUD = 10 * 10**9

# How far behind the line may fall: this long, or as long as the line takes to carry BURST_BYTES if
# that is longer, so that while the guest keeps up with wall time no byte is dropped.
BACKLOG_NS = 50 * MS

# Two packets of a full-speed USB adapter. One holds back what it receives until it has 64 bytes, as
# a CH9102 does at 9600 baud, where they are 67 ms on the line, and hands them over at once. The
# next can come just before the line is free of the last, so one packet of room would cut it.
BURST_BYTES = 128

# How long, in wall seconds, a read of the port blocks before the thread looks at whether the source
# has closed, and a write may stall before the run fails on it.
READ_SECONDS = 0.05
WRITE_SECONDS = 5

# The most bytes one write takes. pyserial's write timeout is for the whole of a write, and the
# limit is on a port that makes no progress: this takes under WRITE_SECONDS at any rate above 128
# baud.
WRITE_CHUNK = 64


class Real:
    """The source on the host serial port `port`, at the wire's `baud`, for the route `name`
    (`uart.gnss`). Opening it starts the reading; `close` ends it, and closes the port."""

    def __init__(self, name: str, port: str, baud: int) -> None:
        # pyserial takes None for a port it does not open yet: a read would fail during the run.
        if not isinstance(port, str) or not port:
            raise TypeError(f"{name}.real takes the path of a serial port, not {port!r}")
        self.name = name
        self.port = port
        self.baud = baud
        # Rounded up, so that the line carries all of BURST_BYTES in it.
        self.backlog_ns = max(BACKLOG_NS, -(-BURST_BYTES * CHAR_NS_AT_1_BAUD // baud))
        # The bytes dropped to keep the line within the bound, from the first turn on, for the test
        # report.
        self.dropped = 0
        # Whether the first turn has looked at the port: until then there is no run to lose bytes.
        self._turned = False
        self._now = 0
        # When the line is free again, as the runner paces it: `line_free(pin)` of rule 5, from this
        # source's own messages, the only ones on its RX pin.
        self._line_free = 0
        self._lock = threading.Lock()
        self._read = bytearray()
        # What the firmware transmitted and the port has not taken yet; None ends the thread.
        self._to_write: queue.SimpleQueue[bytes | None] = queue.SimpleQueue()
        self._failed: Exception | None = None
        # Whether a turn has raised the failure already: a thread may keep none after it.
        self._told = False
        self._closing = threading.Event()
        try:
            # A second reader would take bytes from this one. This locks out a second `Real` and any
            # program that takes the same advisory lock; no others, as a kernel lock cannot tell.
            self._port = serial.Serial(
                port, baud, timeout=READ_SECONDS, write_timeout=WRITE_SECONDS, exclusive=True
            )
        except (OSError, ValueError, termios.error) as error:
            raise SourceError(f"{name}: cannot open the serial port {port}: {error}") from error
        self._reader = threading.Thread(target=self._read_port, daemon=True)
        self._reader.start()
        self._writer = threading.Thread(target=self._write_port, daemon=True)
        self._writer.start()

    def _read_port(self) -> None:
        # The most a turn can send: what the line carries in the bound from an idle line.
        keep = self._carried(self.backlog_ns)
        while not self._closing.is_set():
            try:
                data = self._port.read(max(1, self._port.in_waiting))
            except Exception as error:
                self._fail(error)
                return
            if data:
                with self._lock:
                    self._read += data
                    over = len(self._read) - keep
                    if over > 0:
                        del self._read[:over]
                        if self._turned:
                            self.dropped += over

    def _write_port(self) -> None:
        while (data := self._to_write.get()) is not None:
            for at in range(0, len(data), WRITE_CHUNK):
                if self._closing.is_set():
                    return
                try:
                    self._port.write(data[at : at + WRITE_CHUNK])
                except Exception as error:
                    self._fail(error)
                    return

    def _fail(self, error: Exception) -> None:
        """The port failed, in a thread of its own, or a thread cannot go on: the next turn fails
        the run on the first cause, or `close` does when there is no next turn. One that comes from
        closing the source is none."""
        if not self._closing.is_set():
            with self._lock:
                if not self._told:
                    self._failed = self._failed or error

    def _carried(self, ns: int) -> int:
        """The bytes the line carries in `ns`, 8N1 at the port's baud."""
        return max(0, ns) * self.baud // CHAR_NS_AT_1_BAUD

    def _on_the_line(self, count: int) -> int:
        """How long `count` bytes take on the line, in nanoseconds: rule 5's floor."""
        return count * CHAR_NS_AT_1_BAUD // self.baud

    def poll(self, now: int) -> Iterable[tuple[int, bytes]]:
        """What the port gave since the last turn, no more than the line carries by `now` plus the
        bound, stamped `now`: the time of the first turn that looks at the port after a byte
        arrived. A port that has failed fails the run."""
        self._now = now
        with self._lock:
            self._raise_failed()
            self._turned = True
            data = bytes(self._read)
            self._read.clear()
            start = max(now, self._line_free)
            room = self._carried(now + self.backlog_ns - start)
            if len(data) > room:
                self.dropped += len(data) - room
                data = data[len(data) - room :]
        if not data:
            return ()
        self._line_free = start + self._on_the_line(len(data))
        return [(now, data)]

    def _raise_failed(self) -> None:
        """Raises what the port failed with, once."""
        failed, self._failed = self._failed, None
        if failed is not None:
            self._told = True
            raise SourceError(f"{self.name}: the serial port {self.port}: {failed}")

    def receive(self, t: int, data: bytes) -> Iterable[tuple[int, bytes]]:
        """What the firmware transmitted goes to the port now, or behind what it has not taken yet:
        the turn does not wait for the wire. A port that stalls fails the run at its next turn. The
        answer, if any, comes in `poll`, when the device gives it."""
        self._to_write.put(data)
        return ()

    def transmitted(self, t: int, baud: int | None) -> None:
        """The firmware transmits at `baud` next. The port's rate is fixed for the run, and cannot
        give the device what a wire at another rate does not carry intact: the run fails on it, by
        the 3 % of `UartRoute::matches_baud`, as it does on a `frm_err`."""
        if baud is None:
            raise SourceError(
                f"{self.name}: the firmware transmits with no baud set, and the serial port"
                f" {self.port} runs at {self.baud} baud"
            )
        if abs(baud - self.baud) * 100 > self.baud * 3:
            raise SourceError(
                f"{self.name}: the firmware transmits at {baud} baud, and the serial port"
                f" {self.port} runs at {self.baud} baud"
            )

    def quiet_until(self) -> int:
        """The next whole emulated millisecond after the turn, where the runner ends a slice anyway:
        the device does not know when it will next send."""
        return (self._now // MS + 1) * MS

    def latency(self) -> int | None:
        """A real device promises no answer time, and needs none: its bytes are stamped at the turn
        they are handed over in."""
        return None

    def close(self) -> None:
        """Ends the reading and the writing, and closes the port; what the port has not taken yet
        is not written: the run is over. A failure no turn was left to raise is raised here, once
        the port is closed: the last seconds of a run can stall a write. Closing twice does
        nothing."""
        self._closing.set()
        self._to_write.put(None)
        self._port.cancel_write()
        self._writer.join()
        self._reader.join()
        self._port.close()
        with self._lock:
            self._raise_failed()
