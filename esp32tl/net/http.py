"""An HTTP/1.1 server the test runs (RFC 9110, 9112): one request per connection, answered from
what the test serves at each path and closed. Every request is kept, stamped with the board's
time, for the test to look at.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from esp32tl.interfaces.condition import Condition, FirstFrom
from esp32tl.units import Duration

# The statuses whose response ends with its head (RFC 9112 6.3): no body, and no Content-Length.
NO_CONTENT = (204, 304)

# The longest request head, and the longest body, the server takes.
MAX_HEAD = 64 * 1024
MAX_BODY = 1024 * 1024
REASONS = {
    200: "OK",
    201: "Created",
    204: "No Content",
    301: "Moved Permanently",
    302: "Found",
    304: "Not Modified",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    413: "Content Too Large",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
    501: "Not Implemented",
    503: "Service Unavailable",
}


@dataclass(frozen=True)
class HttpRequest:
    """One request: its method, target as sent, path (the target without the query, of an
    absolute-form target too), version, header fields in order and body."""

    at: Duration
    method: str
    target: str
    path: str
    version: str
    headers: tuple[tuple[str, str], ...]
    body: bytes

    def header(self, name: str) -> str | None:
        """The first field called `name`, whatever its case."""
        return next((v for k, v in self.headers if k.lower() == name.lower()), None)

    @property
    def text(self) -> str:
        return self.body.decode(errors="replace")


@dataclass(frozen=True)
class HttpResponse:
    status: int = 200
    body: bytes = b""
    content_type: str = "text/plain; charset=utf-8"
    headers: tuple[tuple[str, str], ...] = field(default=())


class HttpServer:
    """`now` is the board's clock. The `StreamService` of `esp32tl.net.ip`. A path nobody serves
    gets `404 Not Found`."""

    def __init__(
        self, now: Callable[[], int], log: Callable[[str], None] = lambda line: None
    ) -> None:
        self._now = now
        self._log = log
        self.requests: list[HttpRequest] = []
        self._served: dict[str, HttpResponse] = {}

    def serve(
        self,
        path: str,
        body: bytes | str = b"",
        *,
        status: int = 200,
        content_type: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Answers every request for `path`, from now on, with `status` and `body`: text goes as
        UTF-8, `text/plain` unless `content_type` says otherwise, bytes as
        `application/octet-stream`. `headers` are added as given. A 204 or 304 takes no body,
        and is sent without `Content-Type` and `Content-Length`."""
        if not path.startswith("/"):
            raise ValueError(f"http.serve: {path!r} is not a path, which starts with /")
        if "?" in path:
            raise ValueError(
                f"http.serve: {path!r} holds a query, which a path does not:"
                " serving the path answers it whatever the query"
            )
        if status not in REASONS:
            raise ValueError(f"http.serve: status {status} is not one of {sorted(REASONS)}")
        if status in NO_CONTENT and body:
            raise ValueError(f"http.serve: a {status} response has no body")
        for name, value in (headers or {}).items():
            if any(c in name + value for c in "\r\n") or ":" in name or not name:
                raise ValueError(f"http.serve: {name!r}: {value!r} is not a header field")
        data = body.encode() if isinstance(body, str) else body
        kind = content_type or (
            "text/plain; charset=utf-8" if isinstance(body, str) else "application/octet-stream"
        )
        self._served[path] = HttpResponse(status, data, kind, tuple((headers or {}).items()))

    def next_request(
        self, path: str | None = None, *, method: str | None = None
    ) -> Condition[HttpRequest | None]:
        """A non-consuming condition for the first request, for `path` and with `method` when
        given, appended after this call. The boundary is append order, even at equal
        timestamps. Polling never advances that boundary. A refused request is not recorded,
        so it satisfies no condition."""
        _refuse_query(path, "next_request")
        start = len(self.requests)
        description = (
            "the next HTTP request"
            + (f" for {path!r}" if path is not None else "")
            + (f" with method {method!r}" if method is not None else "")
        )
        find = FirstFrom(lambda: self.requests, start, _wants(path, method))
        return Condition(find, description, "HTTP")

    def find_request(
        self, path: str | None = None, *, method: str | None = None
    ) -> HttpRequest | None:
        """Return the first match among the requests recorded so far, or None; consume nothing."""
        _refuse_query(path, "find_request")
        return next(filter(_wants(path, method), self.requests), None)

    def accept(
        self, peer: tuple[str, int], write: Callable[[bytes], None], close: Callable[[], None]
    ) -> "_Exchange":
        return _Exchange(self, write, close)

    def _answer(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        response = self._served.get(request.path, HttpResponse(404, b"not found\n"))
        self._log(f"http: {request.method} {request.target} → {response.status}")
        return response


class _Exchange:
    def __init__(
        self, server: HttpServer, write: Callable[[bytes], None], close: Callable[[], None]
    ) -> None:
        self._server, self._write, self._close = server, write, close
        self._buffer = bytearray()
        self._head: tuple[str, str, str, tuple[tuple[str, str], ...]] | None = None
        self._length = 0
        self._done = False

    def on_data(self, data: bytes) -> None:
        if self._done:
            return
        self._buffer += data
        if self._head is None:
            end = self._buffer.find(b"\r\n\r\n")
            if end < 0:
                if len(self._buffer) > MAX_HEAD:
                    self._fail(431)
                return
            if end > MAX_HEAD:
                self._fail(431)
                return
            head = bytes(self._buffer[:end]).decode("latin-1")
            del self._buffer[: end + 4]
            parsed = _head(head)
            if parsed is None:
                self._fail(400)
                return
            self._head = parsed
            headers = parsed[3]
            if any(k.lower() == "transfer-encoding" for k, _ in headers):
                self._fail(501)
                return
            lengths = {v.strip() for k, v in headers if k.lower() == "content-length"}
            # ASCII digits only (RFC 9110 8.6): `isdigit` takes `²` too, which `int` refuses
            if len(lengths) > 1 or not all(v.isascii() and v.isdigit() for v in lengths):
                self._fail(400)
                return
            digits = lengths.pop().lstrip("0") if lengths else ""
            # compared by its digits first: `int` refuses more than 4300 of them
            if len(digits) > len(str(MAX_BODY)) or int(digits or "0") > MAX_BODY:
                self._fail(413)
                return
            self._length = int(digits or "0")
        if len(self._buffer) < self._length:
            return
        method, target, version, headers = self._head
        path = _path(target)
        request = HttpRequest(
            Duration(self._server._now()),
            method,
            target,
            path,
            version,
            headers,
            bytes(self._buffer[: self._length]),
        )
        self._respond(self._server._answer(request), head_only=method == "HEAD")

    def _fail(self, status: int) -> None:
        self._server._log(f"http: refused a request: {status} {REASONS[status]}")
        self._respond(
            HttpResponse(status, f"{REASONS[status].lower()}\n".encode()), head_only=False
        )

    def _respond(self, response: HttpResponse, *, head_only: bool) -> None:
        self._done = True
        lines = [f"HTTP/1.1 {response.status} {REASONS[response.status]}"]
        if response.status not in NO_CONTENT:
            lines += [
                f"Content-Type: {response.content_type}",
                f"Content-Length: {len(response.body)}",
            ]
        lines += [
            "Connection: close",
            *(f"{name}: {value}" for name, value in response.headers),
        ]
        head = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
        self._write(head if head_only else head + response.body)
        self._close()

    def on_close(self) -> None:
        self._done = True


def _wants(path: str | None, method: str | None) -> Callable[[HttpRequest], bool]:
    """Whether a request is for `path` and with `method`, each when given."""
    return lambda request: (
        (path is None or request.path == path) and (method is None or request.method == method)
    )


def _refuse_query(path: str | None, what: str) -> None:
    """A filter is compared with a request's path, which has no query: one with `?` would match
    nothing, and `find_request(...) is None` would pass for a request the firmware did send."""
    if path is not None and "?" in path:
        raise ValueError(
            f"http.{what}: {path!r} holds a query, which a path does not: filter by the path"
            " and read the request's target"
        )


def _path(target: str) -> str:
    """The path of a request target (RFC 9112 3.2): of an origin-form target (`/a?b`), all of it
    before the query; of an absolute-form one (`http://host/a?b`), what follows its authority,
    `/` when nothing does; any other form, as an authority-form `host:port` or `*`, as it is."""
    if target.startswith("/"):
        return target.split("?", 1)[0]
    scheme, colon_slashes, rest = target.partition("://")
    if colon_slashes and scheme[:1].isalpha() and all(c.isalnum() or c in "+-." for c in scheme):
        rest = rest.split("?", 1)[0]
        return "/" + rest.partition("/")[2]
    return target


def _head(text: str) -> tuple[str, str, str, tuple[tuple[str, str], ...]] | None:
    """The request line and header fields of a request head, or `None` for a malformed one."""
    lines = text.split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or not parts[0].isalpha() or not parts[2].startswith("HTTP/1."):
        return None
    headers = []
    for line in lines[1:]:
        name, colon, value = line.partition(":")
        if not colon or not name or name != name.strip():
            return None
        headers.append((name, value.strip()))
    return parts[0], parts[1], parts[2], tuple(headers)
