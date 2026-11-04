"""The HTTP server's side of one connection, without TCP: what it answers and keeps."""

from collections.abc import Callable

import pytest

from esp32tl.net.http import HttpServer


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
    requested = server.requested("/")
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


@pytest.mark.parametrize("legacy", [False, True])
def test_history_and_next_requests_use_append_order_without_consuming(
    server: HttpServer, legacy: bool
) -> None:
    condition = server.requested if legacy else server.next_request
    assert server.find_request() is None
    waiting = condition("/a", method="GET")
    Client(server).send(b"GET /a HTTP/1.1\r\n\r\n")
    first = server.requests[0]
    later = condition("/a", method="GET")
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
    ("method", "args", "kwargs", "description"),
    [
        ("next_request", (), {}, "the next HTTP request"),
        ("next_request", ("/",), {}, "the next HTTP request for '/'"),
        ("requested", ("/",), {"method": "GET"}, "the next HTTP request for '/' with method 'GET'"),
        ("requested", (), {"method": "GET"}, "the next HTTP request with method 'GET'"),
    ],
)
def test_http_conditions_are_named_and_reject_truth_testing(
    server: HttpServer,
    method: str,
    args: tuple[str, ...],
    kwargs: dict[str, str],
    description: str,
) -> None:
    condition = getattr(server, method)(*args, **kwargs)
    assert repr(condition) == description
    assert condition() is None
    with pytest.raises(TypeError, match=r"call the HTTP condition with \(\), or pass it to"):
        bool(condition)
    Client(server).send(b"GET / HTTP/1.1\r\n\r\n")
    assert condition() is server.requests[0]
    with pytest.raises(TypeError, match="call the HTTP condition"):
        bool(condition)


@pytest.mark.parametrize("method", ["next_request", "requested", "find_request"])
def test_a_path_filter_holding_a_query_is_refused(server: HttpServer, method: str) -> None:
    Client(server).send(b"GET /update?v=2 HTTP/1.1\r\n\r\n")
    named = "find_request" if method == "find_request" else "next_request"
    with pytest.raises(ValueError, match=rf"http\.{named}: '/update\?v=2' holds a query"):
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
