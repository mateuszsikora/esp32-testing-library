"""DNS: every IN A question resolves to the simulated network (RFC 1035)."""

import struct
from collections.abc import Callable
from ipaddress import IPv4Address


class Dns:
    def __init__(self, address: str, log: Callable[[str], None] = lambda line: None) -> None:
        self.address = IPv4Address(address).packed
        self.asked: list[str] = []
        self._log = log

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
        self.asked.append(name)
        self._log(f"dns: {name} → {IPv4Address(self.address)}")
        answer = qtype == 1 and qclass == 1
        header = struct.pack(">HHHHHH", ident, 0x8080 | (flags & 0x0100), 1, int(answer), 0, 0)
        question = name_wire + data[end : end + 4]
        return (
            header
            + question
            + (struct.pack(">HHHIH", 0xC00C, 1, 1, 60, 4) + self.address if answer else b"")
        )
