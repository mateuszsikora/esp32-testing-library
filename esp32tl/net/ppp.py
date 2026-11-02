"""PPP over a serial line, the far end's side (RFC 1661, 1662, 1332, 1877): HDLC-like framing,
LCP and IPCP. The far end asks nothing of the device (no authentication) and hands it an address
and a DNS server. IPv4 datagrams go to `on_ip`.
"""

import struct
from collections.abc import Callable
from ipaddress import IPv4Address

DEFAULT_MRU = 1500
MIN_MRU = 68  # our supported minimum, enough for IPv4/TCP control packets

FLAG, ESCAPE = 0x7E, 0x7D
LCP, PAP, IPCP, IPV4 = 0xC021, 0xC023, 0x8021, 0x0021
CONF_REQ, CONF_ACK, CONF_NAK, CONF_REJ, TERM_REQ, TERM_ACK, CODE_REJ, PROTO_REJ = range(1, 9)
ECHO_REQ, ECHO_REP, DISCARD = 9, 10, 11

_FCS_TABLE = []
for _b in range(256):
    _v = _b
    for _ in range(8):
        _v = (_v >> 1) ^ 0x8408 if _v & 1 else _v >> 1
    _FCS_TABLE.append(_v)


def fcs16(data: bytes, fcs: int = 0xFFFF) -> int:
    for byte in data:
        fcs = (fcs >> 8) ^ _FCS_TABLE[(fcs ^ byte) & 0xFF]
    return fcs


def ip4(address: str) -> bytes:
    return IPv4Address(address).packed


class Ppp:
    """One PPP link. `send` takes the bytes to put on the line; `on_ip` gets each IPv4 datagram
    the device sends once IPCP is open; `log` gets a line per event worth seeing."""

    def __init__(
        self,
        send: Callable[[bytes], None],
        on_ip: Callable[[bytes], None],
        *,
        local: str = "10.64.0.1",
        peer: str = "10.64.0.2",
        dns: str = "10.64.0.1",
        log: Callable[[str], None] = lambda line: None,
        ended: Callable[[], None] = lambda: None,
    ) -> None:
        self._ended = ended
        self._requests: dict[int, tuple[int, bytes]] = {}
        self._escaped = False
        self._discard = False
        self._send = send
        self._on_ip = on_ip
        self._log = log
        self.local, self.peer, self.dns = local, peer, dns
        self._buffer = bytearray()
        self._in_frame = False
        self._id = 0
        self.lcp_open = False
        self.ipcp_open = False
        self._lcp_acked_us = self._lcp_acked_them = False
        self._ipcp_acked_us = self._ipcp_acked_them = False
        self.started = False
        self.peer_mru = DEFAULT_MRU

    # The line.

    def start(self) -> None:
        """The modem said CONNECT: the far end opens LCP too."""
        if self.started:
            return
        self.started = True
        self._request(LCP, b"")

    def receive(self, data: bytes) -> None:
        for byte in data:
            if byte == FLAG:
                if self._buffer and not self._escaped and not self._discard:
                    self._frame(bytes(self._buffer))
                self._buffer.clear()
                self._in_frame = True
                self._escaped = self._discard = False
            elif self._in_frame and not self._discard:
                if self._escaped:
                    self._buffer.append(byte ^ 0x20)
                    self._escaped = False
                elif byte == ESCAPE:
                    self._escaped = True
                elif byte >= 0x20:  # default receive ACCM; we request no alternative
                    self._buffer.append(byte)
                if len(self._buffer) > 65540:
                    self._buffer.clear()
                    self._discard = True
                    self._log("ppp: dropped an oversized frame")

    def _frame(self, frame: bytes) -> None:
        if len(frame) < 4 or fcs16(frame) != 0xF0B8:
            self._log(f"ppp: dropped a frame of {len(frame)} bytes with a bad FCS")
            return
        body = frame[:-2]
        if body[:2] == b"\xff\x03":
            body = body[2:]
        if not body or (not body[0] & 1 and len(body) < 2):
            return
        if body[0] & 1:
            protocol, payload = body[0], body[1:]
        else:
            protocol, payload = struct.unpack(">H", body[:2])[0], body[2:]
        if protocol == LCP:
            self._lcp(payload)
        elif protocol == IPCP and self.lcp_open:
            self._ipcp(payload)
        elif protocol == IPV4 and self.ipcp_open:
            self._on_ip(payload)
        elif self.lcp_open:
            self._control(LCP, PROTO_REJ, self._next_id(), struct.pack(">H", protocol) + payload)

    def send_ip(self, datagram: bytes) -> None:
        if self.ipcp_open:
            if len(datagram) > self.peer_mru:
                self._log(
                    f"ppp: dropped IPv4 packet of {len(datagram)} bytes: "
                    f"exceeds peer MRU {self.peer_mru}"
                )
                return
            self._write(IPV4, datagram)

    def _write(self, protocol: int, payload: bytes) -> None:
        body = b"\xff\x03" + struct.pack(">H", protocol) + payload
        fcs = fcs16(body) ^ 0xFFFF
        body += struct.pack("<H", fcs)
        out = bytearray([FLAG])
        for byte in body:
            if byte in (FLAG, ESCAPE) or byte < 0x20:
                out += bytes([ESCAPE, byte ^ 0x20])
            else:
                out.append(byte)
        out.append(FLAG)
        self._send(bytes(out))

    # Control protocols.

    def _next_id(self) -> int:
        self._id = (self._id + 1) & 0xFF
        return self._id

    def _control(self, protocol: int, code: int, ident: int, data: bytes) -> None:
        self._write(protocol, struct.pack(">BBH", code, ident, 4 + len(data)) + data)

    def _request(self, protocol: int, options: bytes) -> None:
        ident = self._next_id()
        self._requests[protocol] = (ident, options)
        self._control(protocol, CONF_REQ, ident, options)

    @staticmethod
    def _options(data: bytes) -> list[tuple[int, bytes]]:
        options = []
        while data:
            if len(data) < 2 or not 2 <= data[1] <= len(data):
                raise ValueError("malformed PPP option")
            kind, length = data[0], data[1]
            options.append((kind, data[2:length]))
            data = data[length:]
        return options

    def _lcp(self, packet: bytes) -> None:
        if len(packet) < 4:
            return
        code, ident, length = struct.unpack(">BBH", packet[:4])
        if not 4 <= length <= len(packet):
            return
        data = packet[4:length]
        if code in (CONF_REQ, CONF_ACK, CONF_NAK, CONF_REJ):
            try:
                self._options(data)
            except ValueError:
                return
        if code == CONF_REQ:
            if self.lcp_open:
                self._reset_lcp()
            if not self.started:
                self.start()
            self._lcp_acked_them = False
            self.peer_mru = DEFAULT_MRU
            lengths = {1: 2, 2: 4, 5: 4, 7: 0, 8: 0}
            rejected = [
                (k, v) for k, v in self._options(data) if k not in lengths or len(v) != lengths[k]
            ]
            if rejected:
                self._control(LCP, CONF_REJ, ident, _pack(rejected))
                return
            mru = next(
                (int.from_bytes(v, "big") for k, v in self._options(data) if k == 1), DEFAULT_MRU
            )
            if mru < MIN_MRU:
                self._control(LCP, CONF_NAK, ident, _pack([(1, struct.pack(">H", MIN_MRU))]))
                return
            self.peer_mru = mru
            self._control(LCP, CONF_ACK, ident, data)
            self._lcp_acked_them = True
        elif code == CONF_ACK and self._requests.get(LCP) == (ident, data):
            self._lcp_acked_us = True
        elif code in (CONF_NAK, CONF_REJ) and self._requests.get(LCP, (None,))[0] == ident:
            self._lcp_acked_us = False
            self._request(LCP, b"")
        elif code == ECHO_REQ and self.lcp_open and len(data) >= 4:
            self._control(LCP, ECHO_REP, ident, b"\x00\x00\x00\x00" + data[4:])
        elif code == TERM_REQ:
            self._control(LCP, TERM_ACK, ident, b"")
            self._log("ppp: the device ended the link")
            self._reset_lcp()
            return
        if not self.lcp_open and self._lcp_acked_us and self._lcp_acked_them:
            self.lcp_open = True
            self._log("ppp: LCP open")
            self._request(IPCP, _pack([(3, ip4(self.local))]))

    def _ipcp(self, packet: bytes) -> None:
        if len(packet) < 4:
            return
        code, ident, length = struct.unpack(">BBH", packet[:4])
        if not 4 <= length <= len(packet):
            return
        data = packet[4:length]
        if code in (CONF_REQ, CONF_ACK, CONF_NAK, CONF_REJ):
            try:
                self._options(data)
            except ValueError:
                return
        if code == CONF_REQ:
            if self.ipcp_open:
                self.ipcp_open = False
                self._ipcp_acked_us = False
                self._requests.pop(IPCP, None)
                self._ended()
            if IPCP not in self._requests:
                self._request(IPCP, _pack([(3, ip4(self.local))]))
            self._ipcp_acked_them = False
            wanted = {3: ip4(self.peer), 129: ip4(self.dns), 131: ip4(self.dns)}
            options = self._options(data)
            rejected = [(k, v) for k, v in options if k not in wanted]
            if rejected:
                self._control(IPCP, CONF_REJ, ident, _pack(rejected))
                return
            naks = [(k, wanted[k]) for k, v in options if v != wanted[k]]
            if naks:
                self._control(IPCP, CONF_NAK, ident, _pack(naks))
                return
            self._control(IPCP, CONF_ACK, ident, data)
            self._ipcp_acked_them = True
        elif code == CONF_ACK and self._requests.get(IPCP) == (ident, data):
            self._ipcp_acked_us = True
        elif code in (CONF_NAK, CONF_REJ) and self._requests.get(IPCP, (None,))[0] == ident:
            self._ipcp_acked_us = False
            self._request(IPCP, _pack([(3, ip4(self.local))]))
        elif code == TERM_REQ:
            self._control(IPCP, TERM_ACK, ident, b"")
            self.ipcp_open = False
            self._ipcp_acked_us = self._ipcp_acked_them = False
            self._requests.pop(IPCP, None)
            self._ended()
            return
        if not self.ipcp_open and self._ipcp_acked_us and self._ipcp_acked_them:
            self.ipcp_open = True
            self._log(f"ppp: IPCP open, the device is {self.peer}, DNS {self.dns}")

    def _reset_lcp(self) -> None:
        self.lcp_open = self.ipcp_open = self.started = False
        self.peer_mru = DEFAULT_MRU
        self._lcp_acked_us = self._lcp_acked_them = False
        self._ipcp_acked_us = self._ipcp_acked_them = False
        self._requests.clear()
        self._ended()


def _pack(options: list[tuple[int, bytes]]) -> bytes:
    return b"".join(bytes([kind, 2 + len(value)]) + value for kind, value in options)
