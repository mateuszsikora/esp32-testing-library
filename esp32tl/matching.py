"""Request matching and scheduling, independent of the wire (plan §4.4)."""

import heapq
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

from esp32tl.recording import Recording

Output = tuple[int, bytes]
# A binary protocol supplies the length of its first complete frame, or None.
Terminator = bytes | Callable[[bytes], int | None]


def identity(data: bytes) -> bytes:
    return data


@dataclass(frozen=True)
class Protocol:
    """Request framing, optional response prompt, matching key and fallback response.

    A prompt at the start of the current answer line makes the next request end in Ctrl-Z.
    Further answer bytes can cancel it until the payload starts (AT data mode).
    Normalisation changes keys only; answers always retain their recorded bytes.
    """

    terminator: Terminator = b"\r"
    prompt: bytes | None = None
    normalise: Callable[[bytes], bytes] = identity
    unknown_reply: bytes | Callable[[bytes], bytes] = b"\r\nERROR\r\n"

    def __post_init__(self) -> None:
        if self.terminator == b"":
            raise ValueError("a protocol's terminator must not be empty")
        if self.prompt == b"":
            raise ValueError("a protocol's prompt must not be empty")


def _at_key(data: bytes) -> bytes:
    # Case and whitespace in quoted strings are data, not AT syntax.
    parts = re.split(rb'("[^"\r\n]*")', data.strip())
    return b"".join(p if i % 2 else b"".join(p.split()).upper() for i, p in enumerate(parts))


def _pmtk_key(data: bytes) -> bytes:
    return data.strip().split(b"*", 1)[0].upper()


def _pmtk_unknown(data: bytes) -> bytes:
    command = re.match(rb"\$PMTK(\d{3})(?:[, *\r\n]|$)", data.strip(), re.IGNORECASE)
    if command is None:
        return b""
    payload = b"PMTK001," + command[1] + b",1"
    checksum = 0
    for byte in payload:
        checksum ^= byte
    return b"$" + payload + f"*{checksum:02X}\r\n".encode()


def _ubx_length(data: bytes) -> int | None:
    if len(data) < 6:
        return None
    if not data.startswith(b"\xb5\x62"):
        raise ValueError("a UBX request must start with b'\\xb5\\x62'")
    length = 8 + int.from_bytes(data[4:6], "little")
    return length if len(data) >= length else None


def _ubx_unknown(data: bytes) -> bytes:
    payload = b"\x05\x00\x02\x00" + data[2:4]
    a = b = 0
    for byte in payload:
        a = (a + byte) & 255
        b = (b + a) & 255
    return b"\xb5\x62" + payload + bytes((a, b))


AT = Protocol(b"\r", b"> ", _at_key, b"\r\nERROR\r\n")
PMTK = Protocol(b"\r\n", None, _pmtk_key, _pmtk_unknown)
UBX = Protocol(_ubx_length, None, identity, _ubx_unknown)


def _gnss_length(data: bytes) -> int | None:
    if data.startswith(b"\xb5"):
        return _ubx_length(data)
    end = data.find(b"\r\n")
    return end + 2 if end >= 0 else None


def _gnss_key(data: bytes) -> bytes:
    return data if data.startswith(b"\xb5\x62") else _pmtk_key(data)


def _gnss_unknown(data: bytes) -> bytes:
    return _ubx_unknown(data) if data.startswith(b"\xb5\x62") else _pmtk_unknown(data)


GNSS = Protocol(_gnss_length, None, _gnss_key, _gnss_unknown)


class _Framer:
    def __init__(self, protocol: Protocol) -> None:
        self.protocol = protocol
        self.buffer = b""
        self.prompted = False
        self.answer_tail = b""
        self._payload_started = False
        self._skip_lf = False

    def answer(self, data: bytes) -> None:
        if not data:
            return
        # A response ends the command's optional CRLF separator. A leading LF sent after
        # the prompt belongs to the payload, even when the command used a bare CR.
        self._skip_lf = False
        prompt = self.protocol.prompt
        if prompt is not None and not self._payload_started:
            self.answer_tail = (self.answer_tail + data)[-(len(prompt) + 1) :]
            self.prompted = self.answer_tail.endswith(prompt) and (
                len(self.answer_tail) == len(prompt) or self.answer_tail[:1] in (b"\r", b"\n")
            )

    def feed(self, data: bytes) -> list[tuple[bytes, bytes]]:
        self.buffer += data
        requests = []
        while self.buffer:
            if self._skip_lf:
                self._skip_lf = False
                if self.buffer.startswith(b"\n"):
                    self.buffer = self.buffer[1:]
                    if not self.buffer:
                        break
            if self.prompted:
                self._payload_started = True
            terminator = b"\x1a" if self.prompted else self.protocol.terminator
            if isinstance(terminator, bytes):
                end = self.buffer.find(terminator)
                length = end + len(terminator) if end >= 0 else None
            else:
                length = terminator(self.buffer)
            if length is None:
                break
            if not 0 < length <= len(self.buffer):
                raise ValueError("a protocol's frame length must be within the buffered bytes")
            request, self.buffer = self.buffer[:length], self.buffer[length:]
            key = request if self.prompted else self.protocol.normalise(request)
            self._skip_lf = terminator == b"\r"
            self.prompted = False
            self._payload_started = False
            self.answer_tail = b""
            if key.strip():
                requests.append((request, key))
        return requests


class Matching:
    """A recording compiled to per-request answer sequences and an unprompted timeline.

    Request time is the time of its final fragment. Each occurrence has its own answer list,
    including empty lists (timeouts); lists wrap independently of the stream's end policy.
    Outputs stay queued until poll; quiet_until includes every queued answer.
    """

    def __init__(self, recording: Recording, protocol: Protocol | None = None) -> None:
        clocks = {recording.header.clock} | {line.clock for line in recording.lines}
        if len(clocks) != 1:
            raise ValueError("matching cannot compare times across clocks")
        requests = [event for event in recording.events if event.dir == "out"]
        if protocol is None and requests:
            prefix = next((e.data.lstrip()[:1] for e in requests if e.data.strip()), b"")
            protocol = GNSS if prefix in (b"$", b"\xb5") else AT
        self.protocol = protocol
        self._framer = None if protocol is None else _Framer(protocol)
        self.answers: dict[bytes, list[list[Output]]] = {}
        self.events: tuple[Output, ...]
        events: list[Output] = []
        framer = None if protocol is None else _Framer(protocol)
        answer: list[Output] | None = None
        request_t = 0
        delays = []
        for event in recording.events:
            if event.dir == "out":
                assert framer is not None
                for _, key in framer.feed(event.data):
                    answer = []
                    self.answers.setdefault(key, []).append(answer)
                    request_t = event.t
            elif answer is None:
                events.append((event.t, event.data))
            else:
                delay = event.t - request_t
                answer.append((delay, event.data))
                delays.append(delay)
            if event.dir == "in" and framer is not None:
                framer.answer(event.data)
        if framer is not None and framer.buffer.strip():
            raise ValueError("the recording ends with an incomplete request")
        self.events = tuple(events)
        self._latency = min(delays, default=0) if protocol is not None else None
        self._period = recording.lines[-1].t if recording.lines else 0
        self._loop = recording.header.end == "loop" and bool(events)
        if self._loop and self._period == 0:
            raise ValueError(
                "a looping recording with unprompted events must have positive duration"
            )
        self._offset = 0
        self._cursor = 0
        self._indices: Counter[bytes] = Counter()
        self.unseen: Counter[bytes] = Counter()
        # All occurrences, with completion time and raw frame bytes, outside the rolling log.
        self.unseen_events: list[Output] = []
        self.sent = 0
        self._pending: list[tuple[int, int, bytes]] = []
        self._order = 0

    @property
    def unseen_count(self) -> int:
        return self.unseen.total()

    def receive(self, t: int, data: bytes) -> list[Output]:
        if self._framer is None:
            return []
        assert self.protocol is not None and self._latency is not None
        for request, key in self._framer.feed(data):
            choices = self.answers.get(key)
            if choices is None:
                self.unseen[key] += 1
                self.unseen_events.append((t, request))
                reply = self.protocol.unknown_reply
                response = reply(request) if callable(reply) else reply
                outputs = [(self._latency, response)] if response else []
            else:
                outputs = choices[self._indices[key] % len(choices)]
                self._indices[key] += 1
            for delay, response in outputs:
                heapq.heappush(self._pending, (t + delay, self._order, response))
                self._order += 1
        return []

    def _next_event(self) -> Output | None:
        if self._cursor == len(self.events):
            return None
        t, data = self.events[self._cursor]
        return t + self._offset, data

    def poll(self, now: int) -> list[Output]:
        outputs = []
        while (event := self._next_event()) is not None and event[0] <= now:
            outputs.append(event)
            self._cursor += 1
            if self._loop and self._cursor == len(self.events):
                self._cursor = 0
                self._offset += self._period
        while self._pending and self._pending[0][0] <= now:
            t, _, data = heapq.heappop(self._pending)
            outputs.append((t, data))
        outputs.sort(key=lambda output: output[0])
        if self._framer is not None:
            for _, data in outputs:
                self._framer.answer(data)
        self.sent += len(outputs)
        return outputs

    def quiet_until(self) -> int | None:
        times = []
        if (event := self._next_event()) is not None:
            times.append(event[0])
        if self._pending:
            times.append(self._pending[0][0])
        return min(times, default=None)

    def latency(self) -> int | None:
        return self._latency
