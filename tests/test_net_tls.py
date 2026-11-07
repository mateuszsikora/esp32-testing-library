"""A real TLS handshake in memory: certificate validation and plaintext MQTT both ways, every
cipher suite and group the server has, and the same bytes from the server in every run."""

import contextlib
import json
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from cryptography import x509

from esp32tl.net import Internet, tls
from esp32tl.net.ip import Connection
from esp32tl.net.tls import (
    NOT_AFTER,
    NOT_BEFORE,
    CertificateAuthority,
    Tls,
    number_authorities_from_zero,
)

# OpenSSL's SSL_OP_NO_EXTENDED_MASTER_SECRET, which Python's ssl does not name.
OP_NO_EXTENDED_MASTER_SECRET = 1 << 0


def tls_exchange() -> dict[str, object]:
    now = [0]
    net = Internet(lambda: now[0])
    ca = CertificateAuthority(["broker.example", "192.0.2.1"])
    certificate = x509.load_pem_x509_certificate(ca.ca_pem.encode())
    assert certificate.not_valid_before_utc == NOT_BEFORE
    assert certificate.not_valid_after_utc == NOT_AFTER
    listener = net.listen(8883, net.broker, tls=ca)
    started, completed = listener.handshake_started, listener.handshake_completed
    assert not started() and not completed()
    context = ssl.create_default_context(cadata=ca.ca_pem)
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    client = context.wrap_bio(incoming, outgoing, server_hostname="broker.example")
    closed: list[bool] = []

    def receive(data: bytes) -> None:
        incoming.write(data)

    server = listener.accept(("10.64.0.2", 4000), receive, lambda: closed.append(True))
    assert listener.handshakes_started == 0
    for _ in range(20):
        with contextlib.suppress(ssl.SSLWantReadError):
            client.do_handshake()
        data = outgoing.read()
        if data:
            # Fragment the TLS record at a point unrelated to its header.
            server.on_data(data[:3])
            assert started()
            server.on_data(data[3:])
        if client.version() and completed():
            break
    else:
        pytest.fail("TLS did not finish its handshake")
    assert client.version() == "TLSv1.2" and closed == []
    assert listener.handshakes_started == listener.handshakes_completed == 1
    assert completed()
    with pytest.raises(TypeError, match="call the TLS condition"):
        bool(completed)
    peer = client.getpeercert()
    assert peer is not None
    assert peer["subjectAltName"] == (
        ("DNS", "broker.example"),
        ("IP Address", "192.0.2.1"),
    )
    now[0] = 10_000_000
    waiting = net.broker.next_message("a/b")
    client.write(b"\x10\x0d\0\x04MQTT\x04\x02\0\x0a\0\x01c")
    server.on_data(outgoing.read())
    assert client.read() == b"\x20\x02\0\0"
    client.write(b"\x82\x08\0\x01\0\x03a/#\0")
    server.on_data(outgoing.read())
    assert client.read() == b"\x90\x03\0\x01\0"
    now[0] += 1_000_000
    client.write(b"\x30\x09\0\x03a/bdata")
    server.on_data(outgoing.read())
    assert waiting() is not None and waiting().text == "data"  # type: ignore[union-attr]
    assert client.read() == b"\x30\x09\0\x03a/bdata"
    net.broker.publish("a/b", "test")
    assert client.read() == b"\x30\x09\0\x03a/btest"
    client.write(b"\xe0\0")
    server.on_data(outgoing.read())
    assert closed == [True] and not net.broker.subscribed("a/#")()
    assert all(
        "data" not in e or "4d515454" in str(e["data"]) or len(str(e["data"])) < 40
        for e in net.events
    )  # no handshake ciphertext in the application transcript
    assert listener.handshakes_started == listener.handshakes_completed == 1
    assert started() and completed()  # Closure retains the listener history.
    return net.transcript()


def test_tls_mqtt_and_transcript(tmp_path: Path) -> None:
    transcript = tls_exchange()
    (tmp_path / "network.json").write_text(json.dumps(transcript))


@pytest.mark.parametrize("trusted", [False, True])
def test_tls_rejects_unknown_ca_or_wrong_hostname(trusted: bool) -> None:
    net = Internet(lambda: 0)
    ca = CertificateAuthority(["broker.example"])
    listener = net.listen(8883, net.broker, tls=ca)
    context = ssl.create_default_context(cadata=ca.ca_pem if trusted else None)
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    client = context.wrap_bio(incoming, outgoing, server_hostname="wrong.example")
    closed: list[bool] = []

    def receive(data: bytes) -> None:
        incoming.write(data)

    server = listener.accept(("10.64.0.2", 4000), receive, lambda: closed.append(True))
    with pytest.raises(ssl.SSLCertVerificationError):
        for _ in range(10):
            with contextlib.suppress(ssl.SSLWantReadError):
                client.do_handshake()
            server.on_data(outgoing.read())
    server.on_data(outgoing.read())
    assert closed == [True]
    assert not net.broker.clients
    assert listener.handshakes_started == 1
    assert listener.handshakes_completed == 0
    assert listener.handshake_started() and not listener.handshake_completed()


def test_server_dates_change_without_changing_ca_validity() -> None:
    from datetime import UTC, datetime, timedelta, timezone

    # An unverified MemoryBIO client lets us inspect dates without using the host's clock.
    before = datetime(2030, 1, 1, 2, tzinfo=timezone(timedelta(hours=2)))
    after = datetime(2032, 1, 1, tzinfo=UTC)
    ca = CertificateAuthority(["broker.example"], server_not_before=before, server_not_after=after)
    issuer = x509.load_pem_x509_certificate(ca.ca_pem.encode())
    assert issuer.not_valid_before_utc == NOT_BEFORE
    assert issuer.not_valid_after_utc == NOT_AFTER
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    net = Internet(lambda: 0)
    client = connect(net.listen(8883, net.broker, tls=ca), context).tls
    der = client.getpeercert(binary_form=True)
    assert der is not None
    certificate = x509.load_der_x509_certificate(der)
    certificate.verify_directly_issued_by(issuer)
    assert certificate.not_valid_before_utc == before.astimezone(UTC)
    assert certificate.not_valid_after_utc == after


@pytest.mark.parametrize("field", ["server_not_before", "server_not_after"])
def test_server_dates_require_a_timezone(field: str) -> None:
    from datetime import datetime

    with pytest.raises(ValueError, match=f"TLS {field} must be a timezone-aware datetime"):
        CertificateAuthority(["broker.example"], **{field: datetime(2031, 1, 1)})


@pytest.mark.parametrize("end_year", [2029, 2030])
def test_server_dates_require_a_nonempty_forward_interval(end_year: int) -> None:
    from datetime import UTC, datetime

    with pytest.raises(ValueError, match="TLS server_not_before must be earlier"):
        CertificateAuthority(
            ["broker.example"],
            server_not_before=datetime(2030, 1, 1, tzinfo=UTC),
            server_not_after=datetime(end_year, 1, 1, tzinfo=UTC),
        )


def test_tls_attempts_count_nonempty_input_once_per_connection() -> None:
    net = Internet(lambda: 0)
    listener = net.listen(8883, net.broker, tls=CertificateAuthority(["broker.example"]))
    first = listener.accept(("10.64.0.2", 4000), lambda data: None, lambda: None)
    first.on_data(b"")
    assert listener.handshakes_started == 0
    first.on_data(b"\x16")  # First fragment, without a full ClientHello yet.
    first.on_data(b"\x03\x03")
    assert listener.handshakes_started == 1
    first.on_close()
    first.on_data(b"ignored after closure")
    idle = listener.accept(("10.64.0.2", 4001), lambda data: None, lambda: None)
    idle.on_close()
    assert listener.handshakes_started == 1
    second = listener.accept(("10.64.0.2", 4002), lambda data: None, lambda: None)
    second.on_data(b"invalid TLS input")
    assert listener.handshakes_started == 2
    assert not net.broker.clients
    assert net.listen(9000, net.broker) is None


@pytest.mark.parametrize("completed", [False, True])
def test_tls_conditions_are_live_named_and_reject_truth_testing(completed: bool) -> None:
    net = Internet(lambda: 0)
    listener = net.listen(8883, net.broker, tls=CertificateAuthority(["broker.example"]))
    condition = listener.handshake_completed if completed else listener.handshake_started
    expected = "a TLS handshake to complete" if completed else "a TLS handshake to start"
    assert repr(condition) == expected
    assert not condition()
    with pytest.raises(TypeError, match="call the TLS condition"):
        bool(condition)
    server = listener.accept(("10.64.0.2", 4000), lambda data: None, lambda: None)
    assert not condition()  # Accept alone is not TLS input.
    server.on_data(b"\x16")
    assert condition() is (not completed)  # Input is not handshake completion.
    server.on_close()
    net.stop()
    assert condition() is (not completed)
    with pytest.raises(TypeError, match="call the TLS condition"):
        bool(condition)


def test_next_tls_handshake_excludes_previous_success_across_reconnects() -> None:
    net = Internet(lambda: 123)  # All attempts share a board timestamp.
    ca = CertificateAuthority(["broker.example"])
    listener = net.listen(8883, net.broker, tls=ca)
    context = ssl.create_default_context(cadata=ca.ca_pem)
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    started = listener.next_handshake_started()
    completed = listener.next_handshake_completed()
    assert repr(started) == "the next TLS handshake to start"
    assert repr(completed) == "the next TLS handshake to complete"
    assert not started() and not completed()
    for condition in (started, completed):
        with pytest.raises(TypeError, match="call the TLS condition"):
            bool(condition)

    def handshake(port: int) -> None:
        incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
        client = context.wrap_bio(incoming, outgoing, server_hostname="broker.example")

        def receive(data: bytes) -> None:
            incoming.write(data)

        server = listener.accept(("10.64.0.2", port), receive, lambda: None)
        # Begin before arming another completion condition: completion, not TCP accept
        # or first input, is the boundary for next_handshake_completed().
        with contextlib.suppress(ssl.SSLWantReadError):
            client.do_handshake()
        server.on_data(outgoing.read())
        in_progress = listener.next_handshake_completed()
        assert started() and not completed() and not in_progress()
        for _ in range(20):
            with contextlib.suppress(ssl.SSLWantReadError):
                client.do_handshake()
            data = outgoing.read()
            if data:
                server.on_data(data)
            if client.version() and completed():
                break
        else:
            pytest.fail("TLS did not finish its handshake")
        assert in_progress()
        server.on_close()
        net.stop()

    handshake(4000)
    previous_started, previous_completed = started, completed
    started = listener.next_handshake_started()
    completed = listener.next_handshake_completed()
    assert not started() and not completed()
    assert listener.handshake_started() and listener.handshake_completed()
    failed = listener.accept(("10.64.0.2", 4001), lambda data: None, lambda: None)
    assert not started() and not completed()  # TCP accept is neither event.
    failed.on_data(b"invalid TLS input")
    assert started() and not completed()
    started = listener.next_handshake_started()
    assert not started()
    handshake(4002)
    for _ in range(2):
        assert started() and completed()
        assert previous_started() and previous_completed()
        assert listener.handshake_started() and listener.handshake_completed()
    assert listener.handshakes_started == 3 and listener.handshakes_completed == 2
    for condition in (started, completed):
        with pytest.raises(TypeError, match="call the TLS condition"):
            bool(condition)
    assert not listener.next_handshake_started()()
    assert not listener.next_handshake_completed()()


class Echo:
    """A stream service that writes back what it reads, and counts its closed connections."""

    def __init__(self) -> None:
        self.closed = 0

    def accept(
        self, peer: tuple[str, int], write: Callable[[bytes], None], close: Callable[[], None]
    ) -> "Echoing":
        return Echoing(self, write)


@dataclass
class Echoing:
    echo: Echo
    on_data: Callable[[bytes], None]

    def on_close(self) -> None:
        self.echo.closed += 1


@dataclass
class Client:
    """A client's side of a connection in memory, and each write of the server's side."""

    tls: ssl.SSLObject
    outgoing: ssl.MemoryBIO
    server: Connection
    wrote: list[bytes]
    closed: list[bool]

    def push(self) -> None:
        data = self.outgoing.read()
        if data:
            self.server.on_data(data)

    def round_trip(self, data: bytes) -> bytes:
        self.tls.write(data)
        self.push()
        back = b""
        with contextlib.suppress(ssl.SSLWantReadError):
            while len(back) <= len(data):
                back += self.tls.read()
        return back


def connect(listener: Tls, context: ssl.SSLContext, port: int = 4000) -> Client:
    """The client's handshake with `listener`, finished."""
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    wrote: list[bytes] = []
    closed: list[bool] = []

    def receive(data: bytes) -> None:
        wrote.append(data)
        incoming.write(data)

    client = Client(
        context.wrap_bio(incoming, outgoing, server_hostname="broker.example"),
        outgoing,
        listener.accept(("10.64.0.2", port), receive, lambda: closed.append(True)),
        wrote,
        closed,
    )
    for _ in range(20):
        with contextlib.suppress(ssl.SSLWantReadError):
            client.tls.do_handshake()
        client.push()
        if client.tls.version():
            return client
    pytest.fail("TLS did not finish its handshake")


def client_context(ca: CertificateAuthority) -> ssl.SSLContext:
    context = ssl.create_default_context(cadata=ca.ca_pem)
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    return context


@pytest.mark.parametrize(
    "suite",
    ["ECDHE-RSA-AES128-GCM-SHA256", "ECDHE-RSA-AES256-GCM-SHA384", "ECDHE-RSA-CHACHA20-POLY1305"],
)
@pytest.mark.parametrize("group", ["X25519", "prime256v1", "secp384r1"])
@pytest.mark.parametrize("extended_master_secret", [True, False])
def test_every_suite_and_group_carries_data_both_ways(
    suite: str, group: str, extended_master_secret: bool
) -> None:
    net = Internet(lambda: 0)
    ca = CertificateAuthority(["broker.example"])
    echo = Echo()
    context = client_context(ca)
    context.set_ciphers(suite)
    context.set_ecdh_curve(group)
    if not extended_master_secret:
        context.options |= OP_NO_EXTENDED_MASTER_SECRET
    client = connect(net.listen(8883, echo, tls=ca), context)
    cipher = client.tls.cipher()
    assert cipher is not None and cipher[0] == suite
    assert net.events[-1]["event"] == "tls: handshake done, TLSv1.2"
    # More than one record's worth, in both directions.
    data = bytes(range(256)) * 100
    assert client.round_trip(data) == data
    assert echo.closed == 0


def test_the_server_sends_the_same_bytes_in_every_run() -> None:
    # A ClientHello, recorded once, as the device's firmware sends the same one in every run.
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    probe = client_context(CertificateAuthority(["broker.example"]))
    with contextlib.suppress(ssl.SSLWantReadError):
        probe.wrap_bio(incoming, outgoing, server_hostname="broker.example").do_handshake()
    hello = outgoing.read()

    def run() -> tuple[str, list[bytes]]:
        number_authorities_from_zero()
        net = Internet(lambda: 0)
        flights: list[bytes] = []
        ca = CertificateAuthority(["broker.example"])
        listener = net.listen(8883, net.broker, tls=ca)
        for port in (4000, 4001):
            listener.accept(("10.64.0.2", port), flights.append, lambda: None).on_data(hello)
        other = CertificateAuthority(["broker.example"])
        del net.ip.tcp[8883]
        listener = net.listen(8883, net.broker, tls=other)
        listener.accept(("10.64.0.2", 4002), flights.append, lambda: None).on_data(hello)
        assert other.ca_pem != ca.ca_pem
        return ca.ca_pem, flights

    first, second = run(), run()
    assert first == second
    # The second connection draws a server random and an ephemeral key of its own; another CA
    # has keys of its own.
    flights = first[1]
    assert len(set(flights)) == 3 and len(flights[0]) == len(flights[1])


def test_a_ca_that_outlives_its_test_keeps_its_number_and_its_keys_to_itself() -> None:
    # Relative numbers only: a failed test before this one holds its CAs through its traceback.
    # A wider fixture's CA, made in an earlier test, and the untrusted CA of the next test.
    trusted = CertificateAuthority(["broker.test"])
    number_authorities_from_zero()
    unknown = CertificateAuthority(["broker.test"])
    assert unknown.number != trusted.number
    assert trusted.ca_pem != unknown.ca_pem and trusted.chain != unknown.chain
    # A CA nothing holds, in a reference cycle through its listener, gives its number back.
    net = Internet(lambda: 0)
    net.listen(8883, net.broker, tls=unknown)
    freed = unknown.number
    del net, unknown
    number_authorities_from_zero()
    assert freed not in tls._held
    again = CertificateAuthority(["broker.test"])
    assert again.number <= freed and again.number != trusted.number
    assert again.ca_pem != trusted.ca_pem


def test_a_client_without_tls_1_2_is_refused_with_an_alert() -> None:
    net = Internet(lambda: 0)
    ca = CertificateAuthority(["broker.example"])
    context = ssl.create_default_context(cadata=ca.ca_pem)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    listener = net.listen(8883, Echo(), tls=ca)
    with pytest.raises(ssl.SSLError, match="PROTOCOL_VERSION"):
        connect(listener, context)
    assert listener.handshakes_started == 1 and listener.handshakes_completed == 0
    assert {
        "t_ns": 0,
        "event": "tls: handshake failed: the client does not offer TLS 1.2,"
        " the only version served",
    } in net.events


def test_a_record_that_does_not_decrypt_closes_the_connection() -> None:
    net = Internet(lambda: 0)
    ca = CertificateAuthority(["broker.example"])
    echo = Echo()
    client = connect(net.listen(8883, echo, tls=ca), client_context(ca))
    client.tls.write(b"hello")
    record = bytearray(client.outgoing.read())
    record[-1] ^= 1
    client.server.on_data(bytes(record))
    assert client.closed == [True] and echo.closed == 1
    with pytest.raises(ssl.SSLError, match="BAD_RECORD_MAC"):
        client.tls.read()


def test_a_maximum_fragment_length_limits_the_servers_records() -> None:
    net = Internet(lambda: 0)
    ca = CertificateAuthority(["broker.example"])
    listener = net.listen(8883, Echo(), tls=ca)
    hello = client_hello(extensions=[(1, b"\x01"), (10, b"\x00\x02\x00\x1d")])
    wrote: list[bytes] = []
    listener.accept(("10.64.0.2", 4000), wrote.append, lambda: None).on_data(hello)
    flight = b"".join(wrote)
    lengths = []
    while flight:
        lengths.append(int.from_bytes(flight[3:5]))
        flight = flight[5 + lengths[-1] :]
    assert len(lengths) > 2 and max(lengths) == 512
    # The ServerHello echoes the extension.
    assert b"\x00\x01\x00\x01\x01" in wrote[0][:200]


def client_hello(extensions: list[tuple[int, bytes]]) -> bytes:
    """A TLS 1.2 ClientHello for ECDHE-RSA-AES128-GCM-SHA256 with `extensions`."""
    block = b"".join(code.to_bytes(2) + len(data).to_bytes(2) + data for code, data in extensions)
    body = (
        b"\x03\x03"
        + bytes(32)
        + b"\x00"
        + b"\x00\x02\xc0\x2f"
        + b"\x01\x00"
        + len(block).to_bytes(2)
        + block
    )
    message = b"\x01" + len(body).to_bytes(3) + body
    return b"\x16\x03\x01" + len(message).to_bytes(2) + message


# The alerts, by RFC 5246's numbers.
HANDSHAKE_FAILURE, ILLEGAL_PARAMETER, PROTOCOL_VERSION = 40, 47, 70


@pytest.mark.parametrize(
    ("extensions", "alert", "message"),
    [
        (
            [(10, b"\x00\x02\x00\x1e")],
            HANDSHAKE_FAILURE,
            "no group in common; the server has x25519, secp256r1, secp384r1",
        ),
        (
            [(13, b"\x00\x02\x08\x04")],
            HANDSHAKE_FAILURE,
            "the client takes no RSA PKCS #1 v1.5 signature",
        ),
        (
            [(43, b"\x02\x03\x04")],
            PROTOCOL_VERSION,
            "the client does not offer TLS 1.2, the only version served",
        ),
        ([(11, b"\x01\x01")], ILLEGAL_PARAMETER, "the client does not take uncompressed points"),
        (
            [(1, b"\x05")],
            ILLEGAL_PARAMETER,
            "the client asks for a maximum fragment length that is not one",
        ),
        (
            [(10, b"\x00\x02\x00\x1d"), (10, b"\x00\x02\x00\x1d")],
            ILLEGAL_PARAMETER,
            "the client sent extension 10 twice",
        ),
    ],
)
def test_a_client_hello_the_server_cannot_serve_is_refused(
    extensions: list[tuple[int, bytes]], alert: int, message: str
) -> None:
    net = Internet(lambda: 0)
    listener = net.listen(8883, Echo(), tls=CertificateAuthority(["broker.example"]))
    wrote: list[bytes] = []
    closed: list[bool] = []
    server = listener.accept(("10.64.0.2", 4000), wrote.append, lambda: closed.append(True))
    server.on_data(client_hello(extensions))
    assert wrote == [b"\x15\x03\x03\x00\x02\x02" + bytes([alert])] and closed == [True]
    assert net.events[-1]["event"] == f"tls: handshake failed: {message}"
