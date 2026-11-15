"""Device HTTP responses may arrive across arbitrary TCP boundaries."""

import pytest

from esp32tl.net import Internet
from esp32tl.net.client import ConnectionEnded, TcpConnection
from esp32tl.net.http_client import HttpClient

ClientFixture = tuple[HttpClient, TcpConnection, list[bytes], Internet]


@pytest.fixture
def client() -> tuple[HttpClient, TcpConnection, list[bytes], Internet]:
    network = Internet(lambda: 42)
    connection = TcpConnection(
        "10.64.0.2", 80, "10.64.0.1", "device:80/1", lambda: 42, lambda direction, data: None
    )
    sent: list[bytes] = []
    connection._opened(49152, sent.append, lambda: None)
    return HttpClient(lambda port: connection, network.log), connection, sent, network


@pytest.mark.parametrize(
    "wire",
    [
        b'HTTP/1.1 200 OK\r\nContent-Length: 11\r\n\r\n{"ok":true}',
        b'HTTP/1.1 200 OK\r\n\r\n{"ok":true}',
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        b'5;test=yes\r\n{"ok"\r\n6\r\n:true}\r\n0\r\nX-Trailer: value\r\n\r\n',
    ],
)
def test_framing_at_every_split(client: ClientFixture, wire: bytes) -> None:
    # A fresh exchange for every possible packet boundary, including within CRLF.
    for split in range(len(wire) + 1):
        http, connection, sent, network = client
        connection._received.clear()
        connection._observers.clear()
        connection.closed_at = None
        connection._closing = False
        exchange = http.get("/status", headers={"Accept": "application/json"})
        connection.on_data(wire[:split])
        connection.on_data(wire[split:])
        if b"Content-Length" not in wire and b"chunked" not in wire:
            assert exchange.response() is None
        connection.on_close()
        response = exchange.response()
        assert response is not None
        assert response.status == 200 and response.json() == {"ok": True}
        assert response.text == '{"ok":true}' and response.body == b'{"ok":true}'
        assert exchange.response() is response
    assert "Connection: close\r\n" in sent[0].decode()
    assert "http client: device:80/1 GET /status → 200" in "\n".join(network.report())
    assert network.transcript()["events"] == network.events


@pytest.mark.parametrize(
    ("wire", "error"),
    [
        (b"nonsense\r\n\r\n", "malformed status line"),
        (b"HTTP/1.1 200 OK\r\nBad header\r\n\r\n", "malformed header"),
        (b"HTTP/1.1 200 OK\r\nContent-Length: -1\r\n\r\n", "invalid Content-Length"),
        (b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\na", "body completed"),
        (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nz\r\n", "chunk size"),
        (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n1\r\naXX", "CRLF"),
        (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n", "body completed"),
        (
            b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nTransfer-Encoding: chunked\r\n\r\n",
            "both Content-Length",
        ),
        (b"HTTP/1.1 200 OK\r\nConnection: keep-alive\r\n\r\n", "keep-alive"),
        (b"HTTP/1.1 200 OK\r\nContent-Length: 1048577\r\n\r\n", "exceeds"),
        (b"HTTP/1.1", "head completed"),
    ],
)
def test_malformed_responses_fail_the_condition(
    client: ClientFixture, wire: bytes, error: str
) -> None:
    http, connection, _, network = client
    exchange = http.get("/")
    connection.on_data(wire)
    connection.on_close()
    for _ in range(2):
        with pytest.raises(ValueError, match=error):
            exchange.response()
    assert error in "\n".join(network.report())


def test_post_json_and_no_content(client: ClientFixture) -> None:
    http, connection, sent, _ = client
    exchange = http.post("/echo", json={"value": "ż"})
    head, body = sent[0].split(b"\r\n\r\n")
    assert b"Content-Type: application/json" in head
    assert f"Content-Length: {len(body)}".encode() in head
    assert body.decode() == '{"value": "ż"}'
    connection.on_data(b"HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 204 No Content\r\n\r\n")
    response = exchange.response()
    assert response is not None
    assert response.status == 204
    assert response.body == b""


@pytest.mark.parametrize("kind", ["refused", "reset", "lost"])
def test_connection_failure(client: ClientFixture, kind: str) -> None:
    http, connection, _, _ = client
    exchange = http.get("/")
    connection.on_end(kind)
    with pytest.raises(ConnectionEnded, match=kind):
        exchange.response()


@pytest.mark.parametrize(
    "headers",
    [
        {"Connection": "keep-alive"},
        {"X-Test": "a\r\nb"},
        {"Content-Length": "123"},
        {"Bad Name": "a"},
    ],
)
def test_invalid_request_opens_no_connection(
    client: ClientFixture, headers: dict[str, str]
) -> None:
    http, _, sent, _ = client
    with pytest.raises(ValueError):
        http.get("/", headers=headers)
    assert not sent and not http.exchanges


def test_tls_and_body_conflict_refused(client: ClientFixture) -> None:
    http, _, sent, _ = client
    with pytest.raises(ValueError, match="TLS"):
        http.get("https://device/status")
    with pytest.raises(ValueError, match="body or json"):
        http.post("/", "body", json={})
    assert not sent


@pytest.mark.parametrize(
    "partial",
    [
        b"HTTP/1.1 200 OK\r\n\r\npartial",
        b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\npartial",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n7\r\npartial\r\n",
    ],
)
def test_reset_is_not_a_body_delimiter(client: ClientFixture, partial: bytes) -> None:
    http, connection, _, _ = client
    exchange = http.get("/")
    connection.on_data(partial)
    assert exchange.response() is None
    connection.on_end("reset")
    with pytest.raises(ConnectionEnded, match="reset"):
        exchange.response()


@pytest.mark.parametrize(
    ("wire", "error"),
    [
        (b"HTTP/1.1 101 Switching Protocols\r\n\r\n", "protocol switching"),
        (b"HTTP/1.1 100 Continue\r\n\r\n" * 17, "informational responses"),
        (
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0\r\nbad\r\n\r\n",
            "malformed header",
        ),
        (b"HTTP/1.1 200 OK\r\nConnection: upgrade,keep-alive\r\n\r\n", "keep-alive"),
    ],
)
def test_unsupported_and_malformed_metadata(client: ClientFixture, wire: bytes, error: str) -> None:
    http, connection, _, _ = client
    exchange = http.get("/")
    connection.on_data(wire)
    with pytest.raises(ValueError, match=error):
        exchange.response()


@pytest.mark.parametrize("section", ["head", "chunk size", "trailers"])
@pytest.mark.parametrize("extra", [0, 1])
@pytest.mark.parametrize("split", [0, 1, 2, 3, 4])
def test_metadata_limits_ignore_partial_delimiters(
    client: ClientFixture, section: str, extra: int, split: int
) -> None:
    http, connection, _, _ = client
    exchange = http.get("/")
    chunked = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
    if section == "head":
        prefix = b""
        field = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nX-Pad: "
        delimiter, suffix = b"\r\n\r\n", b""
    elif section == "chunk size":
        prefix, field = chunked, b"1;pad="
        delimiter, suffix = b"\r\n", b"x\r\n0\r\n\r\n"
    else:
        prefix, field = chunked + b"0\r\n", b"X-Pad: "
        delimiter, suffix = b"\r\n\r\n", b""
    field += b"a" * (65536 + extra - len(field))
    connection.on_data(prefix + field + delimiter[:split])
    if not extra and split < len(delimiter):
        assert exchange.response() is None
    connection.on_data(delimiter[split:] + suffix)
    if extra:
        with pytest.raises(ValueError, match="exceeds 65536 bytes"):
            exchange.response()
    else:
        response = exchange.response()
        assert response is not None and response.status == 200
        assert response.body == (b"x" if section == "chunk size" else b"")


@pytest.mark.timeout(5)
def test_many_small_chunks_do_not_stall_network_callbacks(client: ClientFixture) -> None:
    # The previous repeated decoding took ~19 s for this 32 KiB response; incremental
    # parsing takes well below a second. The generous timeout catches that regression
    # without asserting a machine-specific benchmark ratio.
    http, connection, _, _ = client
    exchange = http.get("/")
    wire = (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        + b"1\r\nx\r\n" * 32000
        + b"0\r\n\r\n"
    )
    for offset in range(0, len(wire), 1460):
        connection.on_data(wire[offset : offset + 1460])
    response = exchange.response()
    assert response is not None and response.body == b"x" * 32000
