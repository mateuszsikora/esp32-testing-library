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
    net.listen(8883, net.broker, tls=ca)
    context = ssl.create_default_context(cadata=ca.ca_pem)
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    client = context.wrap_bio(incoming, outgoing, server_hostname="broker.example")
    closed: list[bool] = []

    def receive(data: bytes) -> None:
        incoming.write(data)

    server = net.ip.tcp[8883].accept(("10.64.0.2", 4000), receive, lambda: closed.append(True))
    for _ in range(20):
        with contextlib.suppress(ssl.SSLWantReadError):
            client.do_handshake()
        data = outgoing.read()
        if data:
            # Fragment the TLS record at a point unrelated to its header.
            server.on_data(data[:3])
            server.on_data(data[3:])
        if client.version() and any("handshake done" in str(e.get("event")) for e in net.events):
            break
    else:
        pytest.fail("TLS did not finish its handshake")
    assert client.version() == "TLSv1.2" and closed == []
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
    return net.transcript()


def test_tls_mqtt_and_transcript(tmp_path: Path) -> None:
    transcript = tls_exchange()
    (tmp_path / "network.json").write_text(json.dumps(transcript))


@pytest.mark.parametrize("trusted", [False, True])
def test_tls_rejects_unknown_ca_or_wrong_hostname(trusted: bool) -> None:
    net = Internet(lambda: 0)
    ca = CertificateAuthority(["broker.example"])
    net.listen(8883, net.broker, tls=ca)
    context = ssl.create_default_context(cadata=ca.ca_pem if trusted else None)
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    client = context.wrap_bio(incoming, outgoing, server_hostname="wrong.example")
    closed: list[bool] = []

    def receive(data: bytes) -> None:
        incoming.write(data)

    server = net.ip.tcp[8883].accept(("10.64.0.2", 4000), receive, lambda: closed.append(True))
    with pytest.raises(ssl.SSLCertVerificationError):
        for _ in range(10):
            with contextlib.suppress(ssl.SSLWantReadError):
                client.do_handshake()
            server.on_data(outgoing.read())
    server.on_data(outgoing.read())
    assert closed == [True]
    assert not net.broker.clients


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
