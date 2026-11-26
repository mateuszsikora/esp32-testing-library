"""DNS: every IN A question resolves to the simulated network but for the names the test makes
unknown, which get NXDOMAIN (RFC 1035)."""

import string
import struct
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from ipaddress import IPv4Address
from typing import Literal

from esp32tl.interfaces.condition import Condition, FirstFrom
from esp32tl.interfaces.history import History
from esp32tl.units import Duration

DnsAnswer = Literal["address", "no record", "nxdomain"]

# Names compare without regard to ASCII case alone (RFC 4343); `str.lower` folds all of Unicode.
_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


@dataclass(frozen=True)
class DnsQuestion:
    """One DNS question the device asked, at the board's time.

    `at` is when the query reached the server, when it came off the path up: without a path, when
    the link carried it. `name` is the name asked for, its labels joined by dots, with no final dot
    and in the case the device sent; `qtype` its QTYPE, 1 for A and 28 for AAAA. `answered` is what
    the server answered it with: `"nxdomain"` for any question for a name the test made unknown,
    `"address"` for an IN A question for any other name, `"no record"` for any other."""

    at: Duration
    name: str
    qtype: int
    answered: DnsAnswer


class Dns:
    """`address` is the one every IN A question resolves to; `now` is the board's clock.

    `questions` keeps every question the server parsed, in the order it came, whatever its type,
    and each is answered; a query it cannot parse is neither answered nor kept. A question for a
    name `unknown` made unknown, of any type, is answered with NXDOMAIN until `known` undoes it."""

    def __init__(
        self, address: str, now: Callable[[], int], log: Callable[[str], None] = lambda line: None
    ) -> None:
        self.address = IPv4Address(address).packed
        self._questions: list[DnsQuestion] = []
        self._unknown: set[str] = set()
        self._now = now
        self._log = log

    @property
    def questions(self) -> Sequence[DnsQuestion]:
        return History(self._questions)

    @property
    def asked(self) -> Sequence[str]:
        """The names of `questions`, in order."""
        return History(self._questions.__len__, lambda i: self._questions[i].name)

    def unknown(self, name: str) -> None:
        """From now on, answer every question for exactly `name`, compared without regard to ASCII
        case and without a trailing dot, with NXDOMAIN and no record. Logs a line unless `name`
        already is unknown."""
        name = _normalized(name)
        if name not in self._unknown:
            self._unknown.add(name)
            self._log(f"dns: {name} is unknown from now on")

    def known(self, name: str) -> None:
        """Undo `unknown(name)`: questions for `name` are answered as any other name's again.
        Logs a line unless `name` is not unknown."""
        name = _normalized(name)
        if name in self._unknown:
            self._unknown.remove(name)
            self._log(f"dns: {name} is known again")

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
        nxdomain = name.translate(_ASCII_LOWER) in self._unknown
        answer = not nxdomain and qtype == 1 and qclass == 1
        answered: DnsAnswer = "nxdomain" if nxdomain else "address" if answer else "no record"
        self._questions.append(DnsQuestion(Duration(self._now()), name, qtype, answered))
        said = IPv4Address(self.address) if answer else "NXDOMAIN" if nxdomain else "no record"
        self._log(f"dns: {name} → {said}")
        # RCODE 3 is NXDOMAIN, 0 no error.
        flags = 0x8080 | (flags & 0x0100) | (3 if nxdomain else 0)
        header = struct.pack(">HHHHHH", ident, flags, 1, int(answer), 0, 0)
        question = name_wire + data[end : end + 4]
        return (
            header
            + question
            + (struct.pack(">HHHIH", 0xC00C, 1, 1, 60, 4) + self.address if answer else b"")
        )


def _normalized(name: str) -> str:
    if not isinstance(name, str):
        raise TypeError("dns: name takes text")
    return name.removesuffix(".").translate(_ASCII_LOWER)
