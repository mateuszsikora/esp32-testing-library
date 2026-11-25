"""The HTTP server's side of one connection, without TCP: what it answers and keeps."""

from collections.abc import Callable

import pytest
from conftest import CountingList

from esp32tl.net.handlers import refuse_board, refuse_wait
from esp32tl.net.http import HttpRequest, HttpResponse, HttpServer
from esp32tl.units import ms, seconds


class Client:
    def __init__(self, server: HttpServer) -> None:
        self.received = bytearray()
        self.closed = False
        self.connection = server.accept(("10.64.0.2", 40000), self.received.extend, self.close)

    def close(self) -> None:
        self.closed = True

    def send(self, *chunks: bytes) -> None:
        for chunk in chunks:
            self.connection.on_data(chunk)

    @property
    def head(self) -> list[str]:
        return bytes(self.received).split(b"\r\n\r\n")[0].decode().split("\r\n")

    @property
    def body(self) -> bytes:
        return bytes(self.received).split(b"\r\n\r\n", 1)[1]


@pytest.fixture
def server() -> HttpServer:
    return HttpServer(lambda: 42, lambda line: None)


def test_serves_a_path_and_keeps_the_request(server: HttpServer) -> None:
    server.serve("/", "hello from the test\n")
    requested = server.next_request("/")
    client = Client(server)
    # http_request's request, cut anywhere
    client.send(
        b"GET / HTTP/1.0\r\nHost: exam", b"ple.com:80\r\nUser-Agent: esp-idf/1.0 esp32\r\n\r\n"
    )
    assert client.head == [
        "HTTP/1.1 200 OK",
        "Content-Type: text/plain; charset=utf-8",
        "Content-Length: 20",
        "Connection: close",
    ]
    assert client.body == b"hello from the test\n" and client.closed
    request = requested()
    assert request is not None and request.at.ns == 42
    assert (request.method, request.target, request.path, request.version) == (
        "GET",
        "/",
        "/",
        "HTTP/1.0",
    )
    assert request.header("host") == "example.com:80" and request.header("Accept") is None


def test_a_body_by_content_length_bytes_head_and_absolute_form(server: HttpServer) -> None:
    server.serve("/data", b"\x00\x01", status=201, headers={"X-Test": "1"})
    client = Client(server)
    client.send(b"POST /data?x=1 HTTP/1.1\r\nContent-Length: 5\r\n\r\nab", b"cde")
    assert client.head[:3] == [
        "HTTP/1.1 201 Created",
        "Content-Type: application/octet-stream",
        "Content-Length: 2",
    ]
    assert "X-Test: 1" in client.head and client.body == b"\x00\x01"
    assert server.requests[-1].path == "/data" and server.requests[-1].text == "abcde"
    head = Client(server)
    head.send(b"HEAD http://example.com/data HTTP/1.1\r\n\r\n")
    assert head.head[0] == "HTTP/1.1 201 Created" and head.body == b""
    assert server.requests[-1].path == "/data"


def test_an_unserved_path_is_404_and_next_request_filters(server: HttpServer) -> None:
    by_path = server.next_request("/a")
    by_method = server.next_request(method="POST")
    client = Client(server)
    client.send(b"GET /b HTTP/1.1\r\n\r\n")
    assert client.head[0] == "HTTP/1.1 404 Not Found" and client.body == b"not found\n"
    assert by_path() is None and by_method() is None
    Client(server).send(b"GET /a HTTP/1.1\r\n\r\n")
    request = by_path()
    assert request is not None and request.path == "/a"
    assert server.find_request("/b") is server.requests[0]
    assert server.find_request(method="POST") is None
    assert server.find_request("/a", method="GET") is request


def test_history_and_next_requests_use_append_order_without_consuming(server: HttpServer) -> None:
    assert server.find_request() is None
    waiting = server.next_request("/a", method="GET")
    Client(server).send(b"GET /a HTTP/1.1\r\n\r\n")
    first = server.requests[0]
    later = server.next_request("/a", method="GET")
    assert later() is None
    Client(server).send(b"POST /a HTTP/1.1\r\n\r\n")
    Client(server).send(b"GET /b HTTP/1.1\r\n\r\n")
    assert later() is None and len(server.requests) == 3
    # the same board time as the first request: append order, not the timestamp, is the boundary
    Client(server).send(b"GET /a HTTP/1.1\r\n\r\n")
    second = server.requests[-1]
    assert first.at == second.at
    for _ in range(2):
        assert later() is second
        assert waiting() is first
        assert server.find_request("/a", method="GET") is first


@pytest.mark.parametrize(
    ("args", "kwargs", "description"),
    [
        ((), {}, "the next HTTP request"),
        (("/",), {}, "the next HTTP request for '/'"),
        (("/",), {"method": "GET"}, "the next HTTP request for '/' with method 'GET'"),
        ((), {"method": "GET"}, "the next HTTP request with method 'GET'"),
    ],
)
def test_http_conditions_are_named_and_reject_truth_testing(
    server: HttpServer, args: tuple[str, ...], kwargs: dict[str, str], description: str
) -> None:
    condition = server.next_request(*args, **kwargs)
    assert repr(condition) == description
    assert condition() is None
    with pytest.raises(TypeError, match=r"call the HTTP condition with \(\), or pass it to"):
        bool(condition)
    Client(server).send(b"GET / HTTP/1.1\r\n\r\n")
    assert condition() is server.requests[0]
    with pytest.raises(TypeError, match="call the HTTP condition"):
        bool(condition)


@pytest.mark.parametrize("method", ["next_request", "find_request"])
def test_a_path_filter_holding_a_query_is_refused(server: HttpServer, method: str) -> None:
    Client(server).send(b"GET /update?v=2 HTTP/1.1\r\n\r\n")
    with pytest.raises(ValueError, match=rf"http\.{method}: '/update\?v=2' holds a query"):
        getattr(server, method)("/update?v=2")
    request = server.find_request("/update")
    assert request is not None and request.target == "/update?v=2"


def test_a_refused_request_satisfies_no_condition_or_query(server: HttpServer) -> None:
    waiting = server.next_request("/upload")
    client = Client(server)
    client.send(b"POST /upload HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n")
    assert client.head[0] == "HTTP/1.1 501 Not Implemented"
    assert waiting() is None and server.find_request("/upload") is None


@pytest.mark.parametrize(
    ("request_", "status"),
    [
        (b"GET /\r\n\r\n", "400 Bad Request"),
        (b"GET / HTTP/2\r\n\r\n", "400 Bad Request"),
        (b"GET / HTTP/1.1\r\nbroken\r\n\r\n", "400 Bad Request"),
        (b"GET / HTTP/1.1\r\nContent-Length: 1\r\nContent-Length: 2\r\n\r\n", "400 Bad Request"),
        (b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n", "501 Not Implemented"),
        # `isdigit` takes a superscript two, which `int` refuses
        (b"POST / HTTP/1.1\r\nContent-Length: \xb2\r\n\r\n", "400 Bad Request"),
        (b"POST / HTTP/1.1\r\nContent-Length: -1\r\n\r\n", "400 Bad Request"),
        # more digits than `int` takes
        (
            b"POST / HTTP/1.1\r\nContent-Length: " + b"9" * 5000 + b"\r\n\r\n",
            "413 Content Too Large",
        ),
        (b"POST / HTTP/1.1\r\nContent-Length: 2000000\r\n\r\n", "413 Content Too Large"),
        (b"GET / HTTP/1.1\r\nX: " + b"a" * 70000, "431 Request Header Fields Too Large"),
    ],
)
def test_what_it_cannot_take_is_refused_and_not_kept(
    server: HttpServer, request_: bytes, status: str
) -> None:
    client = Client(server)
    client.send(request_)
    assert client.head[0] == f"HTTP/1.1 {status}" and client.closed
    assert server.requests == []
    client.send(b"more")
    assert client.head[0] == f"HTTP/1.1 {status}"


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.serve("relative", "x"),
        lambda s: s.serve("/", "x", status=299),
        lambda s: s.serve("/", "x", headers={"X": "a\r\nb"}),
        lambda s: s.serve("/", "x", headers={"X:Y": "a"}),
    ],
)
def test_serve_refuses_what_is_not_http(
    server: HttpServer, call: Callable[[HttpServer], None]
) -> None:
    with pytest.raises(ValueError):
        call(server)


def test_serve_refuses_a_path_holding_a_query(server: HttpServer) -> None:
    with pytest.raises(ValueError, match=r"http\.serve: '/a\?b' holds a query"):
        server.serve("/a?b", "x")
    server.serve("/a", "x")
    client = Client(server)
    client.send(b"GET /a?b HTTP/1.1\r\n\r\n")
    assert client.head[0] == "HTTP/1.1 200 OK" and client.body == b"x"


def test_a_content_length_with_leading_zeros_is_its_value(server: HttpServer) -> None:
    client = Client(server)
    client.send(b"POST / HTTP/1.1\r\nContent-Length: " + b"0" * 5000 + b"3\r\n\r\nabc")
    assert client.head[0] == "HTTP/1.1 404 Not Found"
    assert server.requests[-1].body == b"abc"


@pytest.mark.parametrize(
    ("target", "path"),
    [
        # origin-form: the whole path before the query, `://` in it or not (RFC 9112 3.2.1)
        ("/proxy/http://example.com/fw.bin", "/proxy/http://example.com/fw.bin"),
        ("/a?next=http://b/c", "/a"),
        # absolute-form: what follows the authority (RFC 9112 3.2.2)
        ("http://example.com/fw.bin?x=1", "/fw.bin"),
        ("HTTP://example.com", "/"),
        # asterisk-form and authority-form stay as they are
        ("*", "*"),
        ("example.com:443", "example.com:443"),
    ],
)
def test_the_path_follows_the_form_of_the_target(
    server: HttpServer, target: str, path: str
) -> None:
    server.serve("/proxy/http://example.com/fw.bin", "firmware")
    Client(server).send(f"GET {target} HTTP/1.1\r\nHost: test\r\n\r\n".encode())
    assert server.requests[-1].path == path


@pytest.mark.parametrize("status", [204, 304])
def test_a_status_without_content_ends_with_its_head(server: HttpServer, status: int) -> None:
    with pytest.raises(ValueError, match=f"a {status} response has no body"):
        server.serve("/", "payload", status=status)
    server.serve("/", status=status, headers={"ETag": '"1"'})
    client = Client(server)
    client.send(b"GET / HTTP/1.1\r\n\r\n")
    assert bytes(client.received).endswith(b"\r\n\r\n") and client.body == b""
    assert client.head == [
        f"HTTP/1.1 {status} {'No Content' if status == 204 else 'Not Modified'}",
        "Connection: close",
        'ETag: "1"',
    ]


def test_a_next_request_poll_reads_only_the_requests_since_the_last_one(
    server: HttpServer,
) -> None:
    # As many requests as polls: rescanning from the boundary would read 50 M of them.
    n = 10_000
    server._requests = history = CountingList[HttpRequest]()
    posted = server.next_request("/log", method="POST")
    for _ in range(n):
        Client(server).send(b"GET /log HTTP/1.1\r\n\r\n")
        assert posted() is None
    assert history.reads == n
    Client(server).send(b"POST /log HTTP/1.1\r\nContent-Length: 0\r\n\r\n")
    request = history[-1]
    assert posted() is request and posted() is request
    assert history.reads == n + 1 + 2


class Clock:
    """The board's clock, set by the test."""

    def __init__(self) -> None:
        self.now = 0

    def __call__(self) -> int:
        return self.now


def test_a_handler_answers_each_request_from_the_request() -> None:
    log: list[str] = []
    server = HttpServer(lambda: 42, log.append)
    seen: list[HttpRequest] = []

    def update(request: HttpRequest) -> HttpResponse:
        seen.append(request)
        # The request is recorded before its handler runs.
        assert server.requests[-1] is request
        version = request.target.partition("v=")[2]
        return HttpResponse(
            200,
            f'{{"current": "{version}", "agent": "{request.header("user-agent")}"}}',
            content_type="application/json",
            headers={"X-Echo": request.text},
        )

    server.serve("/update", update)
    client = Client(server)
    client.send(
        b"POST /update?v=1.2 HTTP/1.1\r\nUser-Agent: fw/1.2\r\nContent-Length: 4\r\n\r\n",
        b"ping",
    )
    [request] = seen
    assert request.at.ns == 42 and request.path == "/update" and request.body == b"ping"
    assert client.head == [
        "HTTP/1.1 200 OK",
        "Content-Type: application/json",
        "Content-Length: 37",
        "Connection: close",
        "X-Echo: ping",
    ]
    assert client.body == b'{"current": "1.2", "agent": "fw/1.2"}' and client.closed
    assert log == ["http: POST /update?v=1.2 → 200"]
    # once per request, HEAD too, which gets the head alone
    head = Client(server)
    head.send(b"HEAD /update?v=2 HTTP/1.1\r\n\r\n")
    assert len(seen) == 2 and head.head[0] == "HTTP/1.1 200 OK" and head.body == b""


def test_a_handler_and_a_fixed_response_replace_each_other(server: HttpServer) -> None:
    server.serve("/", "fixed")
    server.serve("/", lambda request: HttpResponse(201, "computed"))
    client = Client(server)
    client.send(b"GET / HTTP/1.1\r\n\r\n")
    assert client.head[0] == "HTTP/1.1 201 Created" and client.body == b"computed"
    server.serve("/", HttpResponse(body=b"\x01"))
    again = Client(server)
    again.send(b"GET / HTTP/1.1\r\n\r\n")
    assert again.body == b"\x01" and "Content-Type: application/octet-stream" in again.head
    server.serve("/", "text again")
    last = Client(server)
    last.send(b"GET / HTTP/1.1\r\n\r\n")
    assert last.body == b"text again"


def raises(request: HttpRequest) -> HttpResponse:
    raise ZeroDivisionError("a bug of the test's")


@pytest.mark.parametrize(
    ("handler", "error", "message"),
    [
        (
            lambda request: None,
            TypeError,
            "http.serve: the handler for '/fw' returned None, not an HttpResponse",
        ),
        (
            lambda request: "text",
            TypeError,
            "http.serve: the handler for '/fw' returned 'text', not an HttpResponse",
        ),
        (raises, ZeroDivisionError, "a bug of the test's"),
    ],
)
def test_a_handler_that_fails_gives_a_500_and_the_error_to_the_test(
    handler: Callable[[HttpRequest], HttpResponse],
    error: type[Exception],
    message: str,
) -> None:
    log: list[str] = []
    errors: list[BaseException] = []
    server = HttpServer(lambda: 42, log.append, test_error=errors.append)
    server.serve("/fw", handler)
    client = Client(server)
    client.send(b"GET /fw HTTP/1.1\r\n\r\n")
    assert client.head[0] == "HTTP/1.1 500 Internal Server Error" and client.closed
    assert client.body == b"internal server error\n"
    [raised] = errors
    assert type(raised) is error and str(raised) == message
    assert log == [f"http: GET /fw → 500, the handler failed: {error.__name__}: {message}"]
    # recorded all the same: the device did send it
    assert server.find_request("/fw") is not None


def test_without_a_board_the_handler_s_error_is_raised_once_the_500_is_written(
    server: HttpServer,
) -> None:
    server.serve("/fw", raises)
    client = Client(server)
    with pytest.raises(ZeroDivisionError, match="a bug of the test's"):
        client.send(b"GET /fw HTTP/1.1\r\n\r\n")
    assert client.head[0] == "HTTP/1.1 500 Internal Server Error" and client.closed


def test_a_handler_that_waits_on_the_board_fails_with_the_guard(server: HttpServer) -> None:
    def waits(request: HttpRequest) -> HttpResponse:
        refuse_wait("board.wait_until")  # as `board.wait_until` asks first
        return HttpResponse()

    server.serve("/fw", waits)
    client = Client(server)
    with pytest.raises(RuntimeError) as raised:
        client.send(b"GET /fw HTTP/1.1\r\n\r\n")
    assert str(raised.value) == (
        "board.wait_until: called from the HTTP server's handler for '/fw': a handler must not wait"
    )
    assert client.head[0] == "HTTP/1.1 500 Internal Server Error"
    # Outside a handler, a wait is the test's own.
    refuse_wait("board.wait_until")


def test_a_handler_that_drives_the_board_fails_with_the_guard(server: HttpServer) -> None:
    def resets(request: HttpRequest) -> HttpResponse:
        refuse_board("board.reset")  # as `board.reset()` asks first
        return HttpResponse()

    server.serve("/fw", resets)
    client = Client(server)
    with pytest.raises(RuntimeError) as raised:
        client.send(b"GET /fw HTTP/1.1\r\n\r\n")
    assert str(raised.value) == (
        "board.reset: called from the HTTP server's handler for '/fw':"
        " a handler must not call the board"
    )
    assert client.head[0] == "HTTP/1.1 500 Internal Server Error"
    refuse_board("board.reset")


@pytest.mark.parametrize("handled", [False, True])
def test_after_writes_the_response_that_much_later_on_the_board_s_clock(handled: bool) -> None:
    clock = Clock()
    clock.now = 1_000
    log: list[str] = []
    server = HttpServer(clock, log.append)
    slow = HttpResponse(200, "slow", after=ms(500))
    server.serve("/", (lambda request: slow) if handled else slow)
    assert server.quiet_until() is None
    first, second = Client(server), Client(server)
    first.send(b"GET / HTTP/1.1\r\n\r\n")
    clock.now = 2_000
    second.send(b"HEAD / HTTP/1.1\r\n\r\n")
    # recorded at its arrival, answered later
    assert [r.at.ns for r in server.requests] == [1_000, 2_000]
    assert server.quiet_until() == 1_000 + 500_000_000
    assert log == ["http: GET / → 200, after 0.5 s", "http: HEAD / → 200, after 0.5 s"]
    clock.now = 500_000_999
    server.poll()
    assert first.received == bytearray() and not first.closed
    clock.now = 500_001_000
    server.poll()
    assert first.body == b"slow" and first.closed and not second.closed
    assert server.quiet_until() == 2_000 + 500_000_000
    clock.now = 600_000_000
    server.poll()
    assert second.head[0] == "HTTP/1.1 200 OK" and second.body == b"" and second.closed
    assert server.quiet_until() is None


def test_a_connection_closed_before_after_gets_nothing() -> None:
    clock = Clock()
    log: list[str] = []
    server = HttpServer(clock, log.append)
    server.serve("/", HttpResponse(body="late", after=seconds(5)))
    client = Client(server)
    client.send(b"GET / HTTP/1.1\r\n\r\n")
    client.connection.on_close()
    assert server.quiet_until() is None
    clock.now = 10_000_000_000
    server.poll()
    assert client.received == bytearray() and not client.closed
    assert log[-1] == (
        "http: GET /: the connection closed before its response, due at 5 s: nothing written"
    )
    assert len(server.requests) == 1


def test_an_after_of_zero_answers_at_once(server: HttpServer) -> None:
    server.serve("/", HttpResponse(body="now", after=ms(0)))
    client = Client(server)
    client.send(b"GET / HTTP/1.1\r\n\r\n")
    assert client.body == b"now" and server.quiet_until() is None


def test_a_handler_serves_a_file_in_parts_by_range(server: HttpServer) -> None:
    firmware = bytes(range(256)) * 4

    def parts(request: HttpRequest) -> HttpResponse:
        first, _, last = (request.header("Range") or "").removeprefix("bytes=").partition("-")
        start, end = int(first), min(int(last), len(firmware) - 1)
        if start >= len(firmware):
            return HttpResponse(416, headers={"Content-Range": f"bytes */{len(firmware)}"})
        return HttpResponse(
            206,
            firmware[start : end + 1],
            headers={"Content-Range": f"bytes {start}-{end}/{len(firmware)}"},
        )

    server.serve("/fw.bin", parts)
    received = bytearray()
    for start in range(0, len(firmware), 300):
        client = Client(server)
        client.send(f"GET /fw.bin HTTP/1.1\r\nRange: bytes={start}-{start + 299}\r\n\r\n".encode())
        assert client.head[0] == "HTTP/1.1 206 Partial Content"
        received += client.body
    assert received == firmware
    beyond = Client(server)
    beyond.send(b"GET /fw.bin HTTP/1.1\r\nRange: bytes=2000-2100\r\n\r\n")
    assert beyond.head[0] == "HTTP/1.1 416 Range Not Satisfiable"
    assert "Content-Range: bytes */1024" in beyond.head and beyond.body == b""


def test_a_response_is_checked_where_it_is_made() -> None:
    assert HttpResponse() == HttpResponse(200, b"", content_type="application/octet-stream")
    text = HttpResponse(body="é")
    assert (text.body, text.content_type) == ("é".encode(), "text/plain; charset=utf-8")
    pairs = HttpResponse(headers=[("Set-Cookie", "a=1"), ("Set-Cookie", "b=2")])
    assert pairs.headers == (("Set-Cookie", "a=1"), ("Set-Cookie", "b=2"))
    with pytest.raises(ValueError, match=r"^HttpResponse: status 418 is not one of \[200, 201"):
        HttpResponse(418)
    with pytest.raises(ValueError, match=r"^HttpResponse: a 304 response has no body$"):
        HttpResponse(304, "x")
    with pytest.raises(ValueError, match=r"^HttpResponse: 'X:Y': 'a' is not a header field$"):
        HttpResponse(headers={"X:Y": "a"})
    with pytest.raises(ValueError, match="is not a content type"):
        HttpResponse(content_type="text/plain\r\nX: 1")
    with pytest.raises(TypeError, match=r"^HttpResponse: the body is text or bytes, not 42$"):
        HttpResponse(body=42)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match=r"^HttpResponse: after takes a Duration, as seconds\(5\)"):
        HttpResponse(after=5)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match=r"^HttpResponse: after takes a Duration of 0 or more"):
        HttpResponse(after=ms(-1))
    with pytest.raises(AttributeError):
        text.status = 404  # type: ignore[misc]


@pytest.mark.parametrize(
    ("response", "kind"),
    [(HttpResponse(), "an HttpResponse"), (lambda request: HttpResponse(), "a handler")],
)
def test_serve_takes_status_and_headers_with_a_body_only(
    server: HttpServer, response: object, kind: str
) -> None:
    with pytest.raises(
        TypeError, match=rf"^http\.serve: status, headers with {kind}: the HttpResponse carries"
    ):
        server.serve("/", response, status=200, headers={"X": "1"})  # type: ignore[arg-type]
