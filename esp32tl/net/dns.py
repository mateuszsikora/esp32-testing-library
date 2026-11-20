"""DNS: every IN A question resolves to the simulated network (RFC 1035)."""

import struct
from collections.abc import Callable
from dataclasses import dataclass
from ipaddress import IPv4Address

from esp32tl.interfaces.condition import Condition, FirstFrom
from esp32tl.units import Duration


@dataclass(frozen=True)
class DnsQuestion:
    """One DNS question the device asked, at the board's time.

    `at` is when the query reached the server, when it came off the path up: without a path, when
    the link carried it. `name` is the name asked for, its labels joined by dots, with no final dot
    and in the case the device sent; `qtype` its QTYPE, 1 for A and 28 for AAAA."""

    at: Duration
    name: str
    qtype: int


class Dns:
    """`address` is the one every IN A question resolves to; `now` is the board's clock.

    `questions` keeps every question the server parsed, in the order it came, whatever its type,
    and each is answered; a query it cannot parse is neither answered nor kept."""

    def __init__(
        self, address: str, now: Callable[[], int], log: Callable[[str], None] = lambda line: None
    ) -> None:
        self.address = IPv4Address(address).packed
        self.questions: list[DnsQuestion] = []
        self._now = now
        self._log = log

    @property
    def asked(self) -> list[str]:
        """The names of `questions`, in order."""
        return [question.name for question in self.questions]

    def next_question(self, name: str | None = None) -> Condition[DnsQuestion | None]:
        """A non-consuming condition for the first question recorded after this call; with
        `name`, the first for exactly that name. The boundary is append order, even at equal
        times, and polling never moves it."""
        if name is not None and not isinstance(name, str):
            raise TypeError("dns: name takes text or None")
        find = FirstFrom(
            lambda: self.questions,
            len(self.questions),
            lambda question: name is None or question.name == name,
        )
        description = "the next DNS question" + (f" for {name!r}" if name is not None else "")
        return Condition(find, description, "DNS")

    def on_datagram(self, data: bytes) -> bytes | None:
        if len(data) < 12:
            return None
        ident, flags, questions = struct.unpack(">HHH", data[:6])
        if flags & 0xF800 or questions != 1:
            return None
        labels: list[bytes] = []
        at, end = 12, 0
        visited: set[int] = set()
        while at < len(data) and at not in visited:
            visited.add(at)
            length = data[at]
            if length & 0xC0 == 0xC0:
                if at + 1 >= len(data):
                    return None
                end = end or at + 2
                at = ((length & 0x3F) << 8) | data[at + 1]
                continue
            at += 1
            if length == 0:
                end = end or at
                break
            if length > 63 or at + length > len(data):
                return None
            labels.append(data[at : at + length])
            at += length
        else:
            return None
        name_wire = b"".join(bytes([len(label)]) + label for label in labels) + b"\0"
        if len(name_wire) > 255 or end + 4 > len(data):
            return None
        qtype, qclass = struct.unpack(">HH", data[end : end + 4])
        name = b".".join(labels).decode("ascii", errors="backslashreplace")
        self.questions.append(DnsQuestion(Duration(self._now()), name, qtype))
        self._log(f"dns: {name} → {IPv4Address(self.address)}")
        answer = qtype == 1 and qclass == 1
        header = struct.pack(">HHHHHH", ident, 0x8080 | (flags & 0x0100), 1, int(answer), 0, 0)
        question = name_wire + data[end : end + 4]
        return (
            header
            + question
            + (struct.pack(">HHHIH", 0xC00C, 1, 1, 60, 4) + self.address if answer else b"")
        )
