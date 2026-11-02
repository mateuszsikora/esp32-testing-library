"""A real TLS handshake in memory: certificate validation and plaintext MQTT both ways."""

import contextlib
import json
import ssl
from pathlib import Path

import pytest
from cryptography import x509

from esp32tl.net import Internet
from esp32tl.net.tls import NOT_AFTER, NOT_BEFORE, CertificateAuthority


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
    waiting = net.broker.message("a/b")
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
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    client = context.wrap_bio(incoming, outgoing, server_hostname="broker.example")
    server_in, server_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    server = ca.context.wrap_bio(server_in, server_out, server_side=True)
    for _ in range(20):
        with contextlib.suppress(ssl.SSLWantReadError):
            client.do_handshake()
        server_in.write(outgoing.read())
        with contextlib.suppress(ssl.SSLWantReadError):
            server.do_handshake()
        incoming.write(server_out.read())
        if client.version():
            break
    else:
        pytest.fail("TLS did not finish its handshake")
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
