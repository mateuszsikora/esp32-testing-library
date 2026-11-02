"""TLS in front of any stream service, with a certificate authority made for the test: the device
trusts the CA however its firmware takes one, and the harness reads the traffic in plain text.
"""

import datetime
import ssl
import tempfile
from collections.abc import Callable
from ipaddress import ip_address
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from esp32tl.net.ip import Connection, StreamService

# Wide enough that a device whose clock is wrong, or not set, still finds the dates valid.
NOT_BEFORE = datetime.datetime(2000, 1, 1, tzinfo=datetime.UTC)
NOT_AFTER = datetime.datetime(2099, 12, 31, tzinfo=datetime.UTC)


class CertificateAuthority:
    """A CA and one server certificate for `names`. `ca_pem` is what the device is to trust."""

    def __init__(self, names: list[str]) -> None:
        if not names:
            raise ValueError("TLS needs at least one server name")
        ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "esp32tl test CA")])
        ca = (
            x509.CertificateBuilder()
            .subject_name(ca_name)
            .issuer_name(ca_name)
            .public_key(ca_key.public_key())
            .serial_number(1)
            .not_valid_before(NOT_BEFORE)
            .not_valid_after(NOT_AFTER)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(False, False, False, False, False, True, True, False, False), True
            )
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), False)
            .sign(ca_key, hashes.SHA256())
        )
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        server = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
            .issuer_name(ca_name)
            .public_key(key.public_key())
            .serial_number(2)
            .not_valid_before(NOT_BEFORE)
            .not_valid_after(NOT_AFTER)
            .add_extension(x509.SubjectAlternativeName([_name(n) for n in names]), False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False
            )
            .sign(ca_key, hashes.SHA256())
        )
        self.ca_pem = ca.public_bytes(serialization.Encoding.PEM).decode()
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        with tempfile.TemporaryDirectory() as directory:
            chain = Path(directory) / "chain.pem"
            chain.write_bytes(
                server.public_bytes(serialization.Encoding.PEM)
                + ca.public_bytes(serialization.Encoding.PEM)
                + key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
            chain.chmod(0o600)
            self.context.load_cert_chain(chain)


def _name(name: str) -> x509.GeneralName:
    try:
        return x509.IPAddress(ip_address(name))
    except ValueError:
        return x509.DNSName(name)


class Tls:
    """The `StreamService` that ends TLS and hands the plain stream to `inner`."""

    def __init__(
        self,
        inner: StreamService,
        ca: CertificateAuthority,
        log: Callable[[str], None] = lambda line: None,
    ) -> None:
        self.inner = inner
        self.ca = ca
        self._log = log

    def accept(
        self, peer: tuple[str, int], write: Callable[[bytes], None], close: Callable[[], None]
    ) -> "_Session":
        self._log("tls: handshake started")
        return _Session(self, peer, write, close)


class _Session:
    def __init__(
        self,
        tls: Tls,
        peer: tuple[str, int],
        write: Callable[[bytes], None],
        close: Callable[[], None],
    ) -> None:
        self._incoming, self._outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
        self._ssl = tls.ca.context.wrap_bio(self._incoming, self._outgoing, server_side=True)
        self._write, self._close = write, close
        self._tls = tls
        self._peer = peer
        self._inner: Connection | None = None
        self._closed = False
        self._ready = False

    def _flush(self) -> None:
        data = self._outgoing.read()
        if data:
            self._write(data)

    def on_data(self, data: bytes) -> None:
        if self._closed:
            return
        self._incoming.write(data)
        if not self._ready:
            try:
                self._ssl.do_handshake()
            except ssl.SSLWantReadError:
                self._flush()
                return
            except ssl.SSLError as error:
                self._tls._log(f"tls: handshake failed: {error}")
                self._flush()
                self._end()
                return
            self._ready = True
            self._tls._log(f"tls: handshake done, {self._ssl.version()}")
            self._flush()
            self._inner = self._tls.inner.accept(self._peer, self._send_plain, self._end)
        while not self._closed:
            try:
                plain = self._ssl.read(65536)
            except ssl.SSLWantReadError:
                break
            except ssl.SSLError:
                self._end()
                break
            if not plain:
                self._end()
                break
            assert self._inner is not None
            self._inner.on_data(plain)
        self._flush()

    def _send_plain(self, data: bytes) -> None:
        if self._closed:
            return
        try:
            while data:
                count = self._ssl.write(data)
                data = data[count:]
                self._flush()
        except ssl.SSLError:
            self._end()

    def _end(self) -> None:
        if not self._closed:
            self.on_close()
            self._close()

    def on_close(self) -> None:
        if not self._closed:
            self._closed = True
            if self._inner is not None:
                self._inner.on_close()
