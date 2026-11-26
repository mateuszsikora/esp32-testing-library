"""An HTTP/1.1 server the test runs (RFC 9110, 9112): one request per connection, answered from
what the test serves at each path, a fixed response or one its handler makes from the request,
now or `after` a delay on the board's clock, and closed. Every request is kept, stamped with the
board's time, for the test to look at.
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from http import HTTPStatus

from esp32tl.interfaces.condition import Condition, FirstFrom
from esp32tl.interfaces.history import History
from esp32tl.net.handlers import handling, reraise
from esp32tl.units import Duration

# The statuses whose response ends with its head (RFC 9112 6.3): no body, and no Content-Length.
NO_CONTENT = (204, 304)
# The statuses that take no body: those, and 205 Reset Content (RFC 9110 15.3.6), which is sent
# with its Content-Length of 0.
BODILESS = (204, 205, 304)
# The final statuses a response takes (RFC 9110 15): the server has no interim, 1xx, responses.
FINAL = range(200, 600)

# The longest request head, and the longest body, the server takes.
MAX_HEAD = 64 * 1024
MAX_BODY = 1024 * 1024

# RFC 9110's phrases for the statuses whose `HTTPStatus` phrase Python 3.13 changed to them, so
# that 3.11 and 3.12 send the status line 3.13 does.
_RFC9110 = {
    413: "Content Too Large",
    414: "URI Too Long",
    416: "Range Not Satisfiable",
    422: "Unprocessable Content",
}


def _phrase(status: int) -> str:
    """The reason phrase a response with `status` is sent with by default: `http.HTTPStatus`'s,
    RFC 9110's where Python 3.11 has an older one, and an empty one for a status Python does
    not know (RFC 9112 4)."""
    if status in _RFC9110:
        return _RFC9110[status]
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return ""


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


@dataclass(frozen=True, init=False)
class HttpResponse:
    """What the server answers: `status`, any final one from 200 to 599, sent with `reason`,
    `http.HTTPStatus`'s phrase unless the test names another, and `body`, text as UTF-8 with
    `text/plain; charset=utf-8` and bytes with `application/octet-stream` unless `content_type`
    names another, then `headers`, a dict or name and value pairs, as given. A 204, 205 or 304
    takes no body. With `after`, the response is written that much later on the board's clock
    than the request came, as a slow server's."""

    status: int
    reason: str
    body: bytes
    content_type: str
    headers: tuple[tuple[str, str], ...]
    after: Duration | None

    def __init__(
        self,
        status: int = 200,
        body: bytes | str = b"",
        *,
        content_type: str | None = None,
        headers: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
        after: Duration | None = None,
        reason: str | None = None,
    ) -> None:
        _fill(self, "HttpResponse", status, body, content_type, headers, after, reason)


# A handler: the response to each request for its path, made from the request.
Handler = Callable[[HttpRequest], HttpResponse]


def _fill(
    response: HttpResponse,
    what: str,
    status: int,
    body: bytes | str,
    content_type: str | None,
    headers: Mapping[str, str] | Iterable[tuple[str, str]] | None,
    after: Duration | None,
    reason: str | None = None,
) -> None:
    """Checks the parts of `response` and sets them; `what` names the call in the errors."""
    if isinstance(status, bool) or not isinstance(status, int) or status not in FINAL:
        raise ValueError(f"{what}: status {status!r} is not a final status, from 200 to 599")
    if reason is None:
        phrase = _phrase(status)
    elif not isinstance(reason, str):
        raise TypeError(f"{what}: the reason is text, not {reason!r}")
    elif any(c in reason for c in "\r\n"):
        raise ValueError(f"{what}: {reason!r} is not a reason phrase, which holds no line break")
    else:
        _latin1(what, f"the reason {reason!r}", reason)
        phrase = reason
    if isinstance(body, str):
        data = body.encode()
    elif isinstance(body, bytes | bytearray | memoryview):
        data = bytes(body)
    else:
        raise TypeError(f"{what}: the body is text or bytes, not {body!r}")
    if status in BODILESS and data:
        raise ValueError(f"{what}: a {status} response has no body")
    pairs = tuple((headers.items() if isinstance(headers, Mapping) else headers) or ())
    for name, value in pairs:
        if not isinstance(name, str) or not isinstance(value, str):
            raise TypeError(f"{what}: {name!r}: {value!r} is not a header field of text")
        if any(c in name + value for c in "\r\n") or ":" in name or not name:
            raise ValueError(f"{what}: {name!r}: {value!r} is not a header field")
        # Checked here, not where the head is written: a handler's response fails in the handler,
        # and the device gets its 500, instead of nothing at all.
        _latin1(what, f"{name!r}: {value!r}", name + value)
    kind = content_type or (
        "text/plain; charset=utf-8" if isinstance(body, str) else "application/octet-stream"
    )
    if any(c in kind for c in "\r\n"):
        raise ValueError(f"{what}: {kind!r} is not a content type")
    _latin1(what, repr(kind), kind)
    if after is not None:
        if not isinstance(after, Duration):
            raise TypeError(f"{what}: after takes a Duration, as seconds(5), not {after!r}")
        if after.ns < 0:
            raise ValueError(f"{what}: after takes a Duration of 0 or more, not {after}")
    for field_, value_ in [
        ("status", status),
        ("reason", phrase),
        ("body", data),
        ("content_type", kind),
        ("headers", pairs),
        ("after", after),
    ]:
        object.__setattr__(response, field_, value_)


def _latin1(what: str, shown: str, text: str) -> None:
    """Raises `ValueError` for `text` of the head that Latin-1, which `_respond` writes the head
    in, cannot encode; `shown` is what the message names."""
    try:
        text.encode("latin-1")
    except UnicodeEncodeError:
        raise ValueError(
            f"{what}: {shown} is not Latin-1, which the head is sent in: put it in the body"
        ) from None


class HttpServer:
    """`now` is the board's clock. The `StreamService` of `esp32tl.net.ip`. A path nobody serves
    gets `404 Not Found`. A handler's exception, and the `TypeError` for what it returns that is
    not an `HttpResponse`, goes to `test_error` once the device has its `500`."""

    def __init__(
        self,
        now: Callable[[], int],
        log: Callable[[str], None] = lambda line: None,
        *,
        test_error: Callable[[BaseException], None] = reraise,
    ) -> None:
        self._now = now
        self._log = log
        self._test_error = test_error
        self._requests: list[HttpRequest] = []
        self._served: dict[str, HttpResponse | Handler] = {}
        # The exchanges whose response waits for its `after`, in the order their requests came.
        self._waiting: list[_Exchange] = []

    @property
    def requests(self) -> Sequence[HttpRequest]:
        return History(self._requests)

    def serve(
        self,
        path: str,
        response: bytes | str | HttpResponse | Handler = b"",
        *,
        status: int | None = None,
        content_type: str | None = None,
        headers: Mapping[str, str] | None = None,
        reason: str | None = None,
    ) -> None:
        """Answers every request for `path`, from now on, replacing what was served there.
        `response` is an `HttpResponse`; a handler, called with each request once it has been
        read in full, at its `at` on the board's clock, which returns the `HttpResponse`; or the
        body of one with `status`, `content_type`, `headers` and `reason`, as `HttpResponse`
        takes them: text goes as UTF-8, `text/plain` unless `content_type` says otherwise, bytes
        as `application/octet-stream`. A 204, 205 or 304 takes no body; a 204 or 304 is sent
        without `Content-Type` and `Content-Length`."""
        if not path.startswith("/"):
            raise ValueError(f"http.serve: {path!r} is not a path, which starts with /")
        if "?" in path:
            raise ValueError(
                f"http.serve: {path!r} holds a query, which a path does not:"
                " serving the path answers it whatever the query"
            )
        if isinstance(response, HttpResponse) or callable(response):
            given = [
                name
                for name, value in [
                    ("status", status),
                    ("content_type", content_type),
                    ("headers", headers),
                    ("reason", reason),
                ]
                if value is not None
            ]
            if given:
                kind = "an HttpResponse" if isinstance(response, HttpResponse) else "a handler"
                raise TypeError(
                    f"http.serve: {', '.join(given)} with {kind}: the HttpResponse carries them"
                )
            self._served[path] = response
            return
        fixed = HttpResponse.__new__(HttpResponse)
        _fill(
            fixed,
            "http.serve",
            200 if status is None else status,
            response,
            content_type,
            headers,
            None,
            reason,
        )
        self._served[path] = fixed

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
        return next(filter(_wants(path, method), self._requests), None)

    def accept(
        self, peer: tuple[str, int], write: Callable[[bytes], None], close: Callable[[], None]
    ) -> "_Exchange":
        return _Exchange(self, write, close)

    def poll(self) -> None:
        """Writes each response whose `after` has run out by now, earliest first."""
        now = self._now()
        due = sorted((e for e in self._waiting if e.due <= now), key=lambda e: e.due)
        for exchange in due:
            exchange.send()

    def quiet_until(self) -> int | None:
        """When the next response waiting for its `after` is due, or `None`."""
        return min((exchange.due for exchange in self._waiting), default=None)

    def _answer(self, request: HttpRequest) -> tuple[HttpResponse, BaseException | None]:
        """Records `request` and makes its response: the served one, the handler's, or `404`;
        a `500` and the error for a handler that raised or returned something else."""
        self._requests.append(request)
        served = self._served.get(request.path)
        what = f"http: {request.method} {request.target}"
        if served is None:
            response = HttpResponse(404, "not found\n")
        elif isinstance(served, HttpResponse):
            response = served
        else:
            try:
                with handling(f"the HTTP server's handler for {request.path!r}"):
                    answer = served(request)
                if not isinstance(answer, HttpResponse):
                    raise TypeError(
                        f"http.serve: the handler for {request.path!r} returned {answer!r},"
                        " not an HttpResponse"
                    )
            except BaseException as error:
                # pytest.fail and pytest.skip too, which are no `Exception`: on the link's thread
                # they would end it without an answer to the runner's turn.
                self._log(f"{what} → 500, the handler failed: {type(error).__name__}: {error}")
                return HttpResponse(500, "internal server error\n"), error
            response = answer
        after = response.after
        delayed = f", after {after}" if after is not None and after.ns else ""
        self._log(f"{what} → {response.status}{delayed}")
        return response, None


class _Exchange:
    def __init__(
        self, server: HttpServer, write: Callable[[bytes], None], close: Callable[[], None]
    ) -> None:
        self._server, self._write, self._close = server, write, close
        self._buffer = bytearray()
        self._head: tuple[str, str, str, tuple[tuple[str, str], ...]] | None = None
        self._length = 0
        self._done = False
        # A response waiting for its `after`: what it answers, as `GET /fw`, whether it goes
        # without its body, and when it is due on the board's clock.
        self._waiting: tuple[str, HttpResponse, bool] | None = None
        self.due = 0

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
        self._done = True
        response, error = self._server._answer(request)
        head_only = method == "HEAD"
        if error is not None:
            self._respond(response, head_only=head_only)
            self._server._test_error(error)
        elif response.after is not None and response.after.ns:
            self._waiting = (f"{method} {target}", response, head_only)
            self.due = request.at.ns + response.after.ns
            self._server._waiting.append(self)
        else:
            self._respond(response, head_only=head_only)

    def send(self) -> None:
        """Writes the response that waited for its `after`."""
        assert self._waiting is not None
        _, response, head_only = self._waiting
        self._respond(response, head_only=head_only)

    def _fail(self, status: int) -> None:
        self._done = True
        phrase = _phrase(status)
        self._server._log(f"http: refused a request: {status} {phrase}")
        self._respond(HttpResponse(status, f"{phrase.lower()}\n"), head_only=False)

    def _respond(self, response: HttpResponse, *, head_only: bool) -> None:
        self._unwait()
        # With the space before an empty reason too (RFC 9112 4): `HTTP/1.1 599 `.
        lines = [f"HTTP/1.1 {response.status} {response.reason}"]
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

    def _unwait(self) -> None:
        if self._waiting is not None:
            self._waiting = None
            self._server._waiting.remove(self)

    def on_close(self) -> None:
        self._done = True
        if self._waiting is not None:
            what = self._waiting[0]
            self._unwait()
            self._server._log(
                f"http: {what}: the connection closed before its response, due at"
                f" {Duration(self.due)}: nothing written"
            )


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
