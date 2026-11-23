"""One HTTP/1.1 request per device TCP connection, driven by simulated network callbacks."""

import json as jsonlib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from esp32tl.interfaces.condition import Condition
from esp32tl.interfaces.history import History
from esp32tl.net.client import ConnectionEnded, TcpConnection
from esp32tl.net.http import MAX_BODY, MAX_HEAD

_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")


@dataclass(frozen=True)
class ClientResponse:
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode(errors="replace")

    def json(self) -> Any:
        return jsonlib.loads(self.body)


def _fields(lines: list[str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in lines:
        name, colon, value = line.partition(":")
        if (
            not colon
            or not _TOKEN.fullmatch(name)
            or any((ord(c) < 32 and c != "\t") or ord(c) == 127 for c in value)
        ):
            raise ValueError(f"malformed header field: {line!r}")
        name, value = name.lower(), value.strip()
        if name in fields:
            fields[name] += ", " + value
        else:
            fields[name] = value
    return fields


class _ResponseParser:
    """Consume the TCP receive buffer by offset; never copy or revisit its consumed prefix."""

    def __init__(self) -> None:
        self._offset = 0
        self._scan = 0
        self._state = "head"
        self._informational = 0
        self._status = 0
        self._headers: dict[str, str] = {}
        self._size = 0
        self._body = bytearray()

    def _advance(self, offset: int, state: str) -> None:
        self._offset = self._scan = offset
        self._state = state

    def _delimited(self, data: bytearray, separator: bytes, what: str) -> int | None:
        end = data.find(separator, self._scan)
        if end >= 0:
            length = end - self._offset
        else:
            # A suffix may be the start of a delimiter, not part of the limited field.
            pending = next(
                (
                    n
                    for n in range(len(separator) - 1, 0, -1)
                    if len(data) - self._offset >= n and data.endswith(separator[:n])
                ),
                0,
            )
            length = len(data) - self._offset - pending
            self._scan = max(self._offset, len(data) - len(separator) + 1)
        if length > MAX_HEAD:
            raise ValueError(f"{what} exceeds 65536 bytes")
        return end if end >= 0 else None

    def _head(self, head: bytearray) -> None:
        status, *lines = head.decode("latin-1").split("\r\n")
        if not re.fullmatch(r"HTTP/1\.[01] [1-5][0-9]{2} [\t\x20-\x7e\x80-\xff]*", status):
            raise ValueError(f"malformed status line: {status!r}")
        self._status = int(status.split(" ")[1])
        self._headers = _fields(lines)
        if self._status < 200:
            if self._status == 101:
                raise ValueError("protocol switching is unsupported")
            self._informational += 1
            if self._informational > 16:
                raise ValueError("more than 16 informational responses")
            return
        headers = self._headers
        if "keep-alive" in {v.strip().lower() for v in headers.get("connection", "").split(",")}:
            raise ValueError("keep-alive is unsupported; expected Connection: close")
        length = headers.get("content-length")
        transfer = headers.get("transfer-encoding")
        if length is not None and transfer is not None:
            raise ValueError("both Content-Length and Transfer-Encoding are present")
        if self._status in (204, 304):
            self._state = "length"
            self._size = 0
        elif transfer is not None:
            if transfer.lower() != "chunked":
                raise ValueError(f"unsupported Transfer-Encoding: {transfer!r}")
            self._state = "chunk size"
        elif length is not None:
            if not re.fullmatch(r"[0-9]+", length):
                raise ValueError(f"invalid Content-Length: {length!r}")
            digits = length.lstrip("0") or "0"
            if len(digits) > 7 or int(digits) > MAX_BODY:
                raise ValueError("response body exceeds 1048576 bytes")
            self._size = int(digits)
            self._state = "length"
        else:
            self._state = "close"

    def _response(self, body: bytes) -> ClientResponse:
        return ClientResponse(self._status, self._headers, body)

    def parse(self, data: bytearray, closed: bool) -> ClientResponse | None:
        while True:
            if self._state == "head":
                end = self._delimited(data, b"\r\n\r\n", "response head")
                if end is None:
                    break
                self._head(data[self._offset : end])
                self._advance(end + 4, self._state)
            elif self._state == "length":
                if len(data) - self._offset < self._size:
                    break
                return self._response(bytes(data[self._offset : self._offset + self._size]))
            elif self._state == "close":
                if len(data) - self._offset > MAX_BODY:
                    raise ValueError("response body exceeds 1048576 bytes")
                if closed:
                    return self._response(bytes(data[self._offset :]))
                break
            elif self._state == "chunk size":
                end = self._delimited(data, b"\r\n", "chunk size line")
                if end is None:
                    break
                size_text = data[self._offset : end].split(b";", 1)[0]
                if not re.fullmatch(rb"[0-9a-fA-F]+", size_text):
                    raise ValueError(f"malformed chunk size: {bytes(size_text)!r}")
                self._size = int(size_text, 16)
                if len(self._body) + self._size > MAX_BODY:
                    raise ValueError("response body exceeds 1048576 bytes")
                self._advance(end + 2, "chunk data" if self._size else "trailers")
            elif self._state == "chunk data":
                end = self._offset + self._size
                if len(data) < end + 2:
                    break
                if data[end : end + 2] != b"\r\n":
                    raise ValueError("chunk data is missing its terminating CRLF")
                self._body.extend(data[self._offset : end])
                self._advance(end + 2, "chunk size")
            else:  # trailers, including the empty trailer section
                if data[self._offset : self._offset + 2] == b"\r\n":
                    return self._response(bytes(self._body))
                end = self._delimited(data, b"\r\n\r\n", "trailers")
                if end is None:
                    break
                _fields(data[self._offset : end].decode("latin-1").split("\r\n"))
                return self._response(bytes(self._body))
        if closed:
            part = "head" if self._state == "head" else "body"
            raise ValueError(f"connection closed before the response {part} completed")
        return None


class HttpExchange:
    """A request, its TCP connection, and a non-consuming response condition."""

    def __init__(
        self, connection: TcpConnection, method: str, path: str, log: Callable[[str], None]
    ) -> None:
        self.connection, self.method, self.path = connection, method, path
        self._log = log
        self._parser = _ResponseParser()
        self._result: ClientResponse | None = None
        self._error: ValueError | ConnectionEnded | None = None
        # A wait that times out before the device answered the connection's SYN says so.
        self.response: Condition[ClientResponse | None] = Condition(
            self._find, f"HTTP {method} {path} response", "HTTP", connection.unanswered
        )
        connection._observers.append(self._update)

    def _update(self) -> None:
        if self._result is not None or self._error is not None:
            return
        c = self.connection
        try:
            self._result = self._parser.parse(c._received, c.closed_at is not None)
            if self._result is not None:
                self._log(
                    f"http client: {c.identity} {self.method} {self.path} → {self._result.status}"
                )
                c.close()
            elif c.state in ("refused", "reset", "lost", "closed", "timed out"):
                raise ConnectionEnded(f"{c.name}: {c.state} before HTTP response completed")
        except (ValueError, ConnectionEnded) as error:
            self._error = type(error)(f"HTTP {self.method} {self.path}: {error}")
            self._log(f"http client: {c.identity} {self._error}")
            c.close()

    def _find(self) -> ClientResponse | None:
        if self._error is not None:
            raise self._error
        return self._result


class HttpClient:
    def __init__(self, connect: Callable[[int], TcpConnection], log: Callable[[str], None]) -> None:
        self._connect, self._log = connect, log
        self._exchanges: list[HttpExchange] = []

    @property
    def exchanges(self) -> Sequence[HttpExchange]:
        return History(self._exchanges)

    def get(
        self, path: str, *, port: int = 80, headers: dict[str, str] | None = None
    ) -> HttpExchange:
        return self._request("GET", path, b"", port, headers)

    def post(
        self,
        path: str,
        body: bytes | str | None = None,
        *,
        json: object = None,
        port: int = 80,
        headers: dict[str, str] | None = None,
    ) -> HttpExchange:
        fields = dict(headers or {})
        if json is not None:
            if body is not None:
                raise ValueError("http_client.post takes body or json, not both")
            body = jsonlib.dumps(json, ensure_ascii=False).encode()
            if not any(k.lower() == "content-type" for k in fields):
                fields["Content-Type"] = "application/json"
        if body is not None and not isinstance(body, (str, bytes)):
            raise TypeError("http_client.post body must be bytes or str")
        data = body.encode() if isinstance(body, str) else body or b""
        return self._request("POST", path, data, port, fields)

    def _request(
        self, method: str, path: str, body: bytes, port: int, headers: dict[str, str] | None
    ) -> HttpExchange:
        if (
            not path.startswith("/")
            or any(ord(c) <= 32 or ord(c) >= 127 for c in path)
            or "#" in path
        ):
            raise ValueError(
                "http_client requires an ASCII path starting with /; TLS is unsupported"
            )
        fields = dict(headers or {})
        for name, value in fields.items():
            if not _TOKEN.fullmatch(name) or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise ValueError(f"invalid request header: {name!r}: {value!r}")
            if name.lower() in ("connection", "content-length", "transfer-encoding"):
                raise ValueError(f"http_client controls {name}; keep-alive is unsupported")
        # Validate encoding before opening a connection.
        extra = "".join(f"{k}: {v}\r\n" for k, v in fields.items()).encode("latin-1")
        connection = self._connect(port)
        host = (
            b""
            if any(k.lower() == "host" for k in fields)
            else (f"Host: {connection.address}:{port}\r\n".encode())
        )
        request = (
            f"{method} {path} HTTP/1.1\r\n".encode()
            + host
            + extra
            + f"Connection: close\r\nContent-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        exchange = HttpExchange(connection, method, path, self._log)
        self._exchanges.append(exchange)
        self._log(f"http client: {connection.identity} {method} {path} requested")
        connection.write(request)
        return exchange
