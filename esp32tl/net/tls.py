"""TLS 1.2 in front of any stream service, with a certificate authority made for the test: the
device trusts the CA however its firmware takes one, and the harness reads the traffic in plain
text.

The server's side draws nothing from the system. The keys, the server random and the ephemeral
ECDHE key come from SHAKE-256 over a label naming the CA and the connection, so two runs put the
same bytes on the wire and the device's arithmetic on them takes the same time (RFC 5246, 5288,
7627, 7905, 8422, 5746, 6066 section 4). These keys are anyone's: never use them outside a test.
"""

import datetime
import functools
import gc
import hashlib
import hmac
import itertools
import math
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from ipaddress import ip_address

from cryptography import x509
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa, x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM, ChaCha20Poly1305
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from esp32tl.interfaces.condition import Condition
from esp32tl.net.ip import Connection, StreamClose, StreamService

# Wide enough that a device whose clock is wrong, or not set, still finds the dates valid.
NOT_BEFORE = datetime.datetime(2000, 1, 1, tzinfo=datetime.UTC)
NOT_AFTER = datetime.datetime(2099, 12, 31, tzinfo=datetime.UTC)

# The plugin's `number_authorities_from_zero()` restarts the count before each test; every CA of
# the process that something still holds, a wider fixture or a failed test's traceback, keeps its
# number, by which its keys go.
_numbers = itertools.count()
_held: "weakref.WeakValueDictionary[int, CertificateAuthority]" = weakref.WeakValueDictionary()


def number_authorities_from_zero() -> None:
    """The next `CertificateAuthority` takes the lowest number from 0 that no held CA has."""
    global _numbers
    if _held:
        # A CA in a reference cycle is gone only after a collection; what is left is held, in
        # every run alike.
        gc.collect()
    _numbers = itertools.count()


class CertificateAuthority:
    """A CA and server certificate for `names`; `ca_pem` is what the device trusts.

    `server_not_before` / `server_not_after` change only the leaf certificate's validity.
    They require timezone-aware datetimes; the CA keeps its 2000-2099 validity period.
    `number` is the lowest the test has not given yet and no CA still held has: the keys follow
    from it, so each CA has keys of its own, and the same ones in every run.
    """

    def __init__(
        self,
        names: list[str],
        *,
        server_not_before: datetime.datetime = NOT_BEFORE,
        server_not_after: datetime.datetime = NOT_AFTER,
    ) -> None:
        if not names:
            raise ValueError("TLS needs at least one server name")
        for name, value in (
            ("server_not_before", server_not_before),
            ("server_not_after", server_not_after),
        ):
            if not isinstance(value, datetime.datetime) or value.utcoffset() is None:
                raise ValueError(f"TLS {name} must be a timezone-aware datetime")
        server_not_before = server_not_before.astimezone(datetime.UTC)
        server_not_after = server_not_after.astimezone(datetime.UTC)
        if server_not_before >= server_not_after:
            raise ValueError("TLS server_not_before must be earlier than server_not_after")
        self.number = next(n for n in _numbers if n not in _held)
        _held[self.number] = self
        ca_key = _rsa_key(f"authority {self.number} ca")
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
        self.key = _rsa_key(f"authority {self.number} server")
        server = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
            .issuer_name(ca_name)
            .public_key(self.key.public_key())
            .serial_number(2)
            .not_valid_before(server_not_before)
            .not_valid_after(server_not_after)
            .add_extension(x509.SubjectAlternativeName([_name(n) for n in names]), False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False
            )
            .sign(ca_key, hashes.SHA256())
        )
        self.ca_pem = ca.public_bytes(serialization.Encoding.PEM).decode()
        # What the server sends: its certificate, then the CA's.
        self.chain = [c.public_bytes(serialization.Encoding.DER) for c in (server, ca)]


def _name(name: str) -> x509.GeneralName:
    try:
        return x509.IPAddress(ip_address(name))
    except ValueError:
        return x509.DNSName(name)


class _Stream:
    """The bytes of SHAKE-256 over `label`, read in order."""

    def __init__(self, label: str) -> None:
        self._label = f"esp32tl tls: {label}"
        self._blocks = itertools.count()

    def read(self, count: int) -> bytes:
        return hashlib.shake_256(f"{self._label} {next(self._blocks)}".encode()).digest(count)


def _small_primes(limit: int) -> list[int]:
    sieve = bytearray([1]) * limit
    sieve[:2] = b"\0\0"
    for i in range(2, math.isqrt(limit) + 1):
        if sieve[i]:
            sieve[i * i :: i] = bytes(len(range(i * i, limit, i)))
    return [i for i, prime in enumerate(sieve) if prime]


_PRIMORIAL = math.prod(_small_primes(4000))
_E = 65537


@functools.cache
def _rsa_key(label: str) -> rsa.RSAPrivateKey:
    """An RSA-2048 key whose primes are the first two that `label`'s stream yields."""
    stream = _Stream(f"rsa {label}")
    p, q = _prime(stream), _prime(stream)
    d = pow(_E, -1, (p - 1) * (q - 1))
    return rsa.RSAPrivateNumbers(
        p,
        q,
        d,
        rsa.rsa_crt_dmp1(d, p),
        rsa.rsa_crt_dmq1(d, q),
        rsa.rsa_crt_iqmp(p, q),
        rsa.RSAPublicNumbers(_E, p * q),
    ).private_key()


def _prime(stream: _Stream) -> int:
    """A 1024-bit prime with its two top bits set, so that two of them make 2048 bits."""
    while True:
        candidate = int.from_bytes(stream.read(128)) | (3 << 1022) | 1
        if math.gcd(candidate, _PRIMORIAL) != 1 or candidate % _E == 1:
            continue
        if _probably_prime(candidate, stream):
            return candidate


def _probably_prime(n: int, stream: _Stream, rounds: int = 4) -> bool:
    """Miller-Rabin with base 2 and `rounds` bases from `stream`: few, as the candidates are random
    and `private_key()` validates the key, the primality of its primes included."""
    d, s = n - 1, 0
    while d % 2 == 0:
        d, s = d // 2, s + 1
    # Base 2 first, which rules out nearly every composite before the stream is read.
    bases = itertools.chain(
        [2], (2 + int.from_bytes(stream.read(128)) % (n - 3) for _ in range(rounds))
    )
    for base in bases:
        x = pow(base, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(s - 1):
            x = x * x % n
            if x == n - 1:
                break
        else:
            return False
    return True


@dataclass(frozen=True)
class _Suite:
    name: str
    aead: type[AESGCM] | type[ChaCha20Poly1305]
    key: int
    # 4 for AES-GCM, whose 8 other nonce bytes go in each record; 12 for ChaCha20-Poly1305.
    iv: int
    hash: str


# What the server speaks: ECDHE with an RSA signature and an AEAD. The client's order decides.
SUITES = {
    0xC02F: _Suite("ECDHE-RSA-AES128-GCM-SHA256", AESGCM, 16, 4, "sha256"),
    0xC030: _Suite("ECDHE-RSA-AES256-GCM-SHA384", AESGCM, 32, 4, "sha384"),
    0xCCA8: _Suite("ECDHE-RSA-CHACHA20-POLY1305", ChaCha20Poly1305, 32, 12, "sha256"),
}
# The groups, with the order of the NIST curves' base points.
X25519 = 0x001D
GROUPS: dict[int, tuple[str, ec.EllipticCurve | None, int]] = {
    X25519: ("x25519", None, 0),
    0x0017: (
        "secp256r1",
        ec.SECP256R1(),
        0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551,
    ),
    0x0018: (
        "secp384r1",
        ec.SECP384R1(),
        0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFC7634D81F4372DDF581A0DB248B0A77AECEC196ACCC52973,
    ),
}
# RSA PKCS #1 v1.5 signatures, the deterministic ones. A client without the extension takes SHA-1.
SIGNATURES: dict[int, hashes.HashAlgorithm] = {
    0x0601: hashes.SHA512(),
    0x0501: hashes.SHA384(),
    0x0401: hashes.SHA256(),
    0x0201: hashes.SHA1(),
}

TLS12 = b"\x03\x03"
CHANGE_CIPHER_SPEC, ALERT, HANDSHAKE, APPLICATION_DATA = 20, 21, 22, 23
CLIENT_HELLO, SERVER_HELLO, CERTIFICATE, SERVER_KEY_EXCHANGE = 1, 2, 11, 12
SERVER_HELLO_DONE, CLIENT_KEY_EXCHANGE, FINISHED = 14, 16, 20
MAX_FRAGMENT = 2**14
# Extensions.
MAX_FRAGMENT_LENGTH, SUPPORTED_GROUPS, EC_POINT_FORMATS = 1, 10, 11
SIGNATURE_ALGORITHMS, EXTENDED_MASTER_SECRET, SUPPORTED_VERSIONS = 13, 23, 43
RENEGOTIATION_INFO = 0xFF01
EMPTY_RENEGOTIATION_INFO_SCSV = 0x00FF
ALERTS = {
    0: "close_notify",
    10: "unexpected_message",
    20: "bad_record_mac",
    22: "record_overflow",
    40: "handshake_failure",
    42: "bad_certificate",
    43: "unsupported_certificate",
    44: "certificate_revoked",
    45: "certificate_expired",
    46: "certificate_unknown",
    47: "illegal_parameter",
    48: "unknown_ca",
    50: "decode_error",
    51: "decrypt_error",
    70: "protocol_version",
    80: "internal_error",
    90: "user_canceled",
    100: "no_renegotiation",
}


class _Fatal(Exception):
    """A fatal alert to send, and what the log says."""

    def __init__(self, alert: int, message: str) -> None:
        super().__init__(message)
        self.alert = alert


class _Closed(Exception):
    """The client sent an alert that ends the connection."""


def _prf(hash_name: str, secret: bytes, label: bytes, seed: bytes, length: int) -> bytes:
    """TLS 1.2's PRF, P_hash (RFC 5246 section 5)."""
    seed = label + seed
    a, out = seed, b""
    while len(out) < length:
        a = hmac.digest(secret, a, hash_name)
        out += hmac.digest(secret, a + seed, hash_name)
    return out[:length]


def _vector(data: bytes, width: int) -> bytes:
    return len(data).to_bytes(width) + data


class _Reader:
    """Reads a message's fields; running out is a decode_error."""

    def __init__(self, data: bytes, what: str) -> None:
        self._data, self._at, self._what = data, 0, what

    def take(self, count: int) -> bytes:
        if self._at + count > len(self._data):
            raise _Fatal(50, f"the {self._what} ends early")
        self._at += count
        return self._data[self._at - count : self._at]

    def number(self, width: int) -> int:
        return int.from_bytes(self.take(width))

    def vector(self, width: int) -> bytes:
        return self.take(self.number(width))

    def done(self) -> bool:
        return self._at == len(self._data)


class _Cipher:
    """One direction's AEAD and sequence number (RFC 5288 section 3, RFC 7905 section 2)."""

    def __init__(self, suite: _Suite, key: bytes, iv: bytes) -> None:
        self._aead = suite.aead(key)
        self._iv = iv
        self._sequence = 0

    def _nonce(self, explicit: bytes) -> bytes:
        if len(self._iv) == 4:
            return self._iv + explicit
        padded = bytes(4) + self._sequence.to_bytes(8)
        return bytes(a ^ b for a, b in zip(self._iv, padded, strict=True))

    def _header(self, kind: int, length: int) -> bytes:
        return self._sequence.to_bytes(8) + bytes([kind]) + TLS12 + length.to_bytes(2)

    def seal(self, kind: int, plain: bytes) -> bytes:
        # AES-GCM's explicit nonce is the sequence number: unique, and the same in every run.
        explicit = self._sequence.to_bytes(8) if len(self._iv) == 4 else b""
        sealed = self._aead.encrypt(self._nonce(explicit), plain, self._header(kind, len(plain)))
        self._sequence += 1
        return explicit + sealed

    def open(self, kind: int, fragment: bytes) -> bytes:
        explicit_length = 8 if len(self._iv) == 4 else 0
        if len(fragment) < explicit_length + 16:
            raise _Fatal(20, "a record is too short for its tag")
        explicit, sealed = fragment[:explicit_length], fragment[explicit_length:]
        try:
            plain = self._aead.decrypt(
                self._nonce(explicit), sealed, self._header(kind, len(sealed) - 16)
            )
        except InvalidTag:
            raise _Fatal(20, "a record does not decrypt") from None
        self._sequence += 1
        return plain


class Tls:
    """The `StreamService` that ends TLS and hands the plain stream to `inner`.

    `handshakes_started` counts connections that supplied nonempty input, even if invalid.
    The cumulative count survives closure; it does not imply handshake completion.
    """

    def __init__(
        self,
        inner: StreamService,
        ca: CertificateAuthority,
        log: Callable[[str], None] = lambda line: None,
    ) -> None:
        self.inner = inner
        self.ca = ca
        self._log = log
        self._accepted = 0
        self.handshakes_started = 0
        self.handshakes_completed = 0

    @property
    def handshake_started(self) -> Condition[bool]:
        """A cumulative condition, including past connections with nonempty TLS input."""
        return self._handshake(completed=False)

    @property
    def handshake_completed(self) -> Condition[bool]:
        """A cumulative condition, including past server-side handshake completions."""
        return self._handshake(completed=True)

    def next_handshake_started(self) -> Condition[bool]:
        """A non-consuming condition for a connection's first nonempty input after this call."""
        return self._handshake(completed=False, after=self.handshakes_started)

    def next_handshake_completed(self) -> Condition[bool]:
        """A non-consuming condition for a server-side completion after this call.

        A handshake already in progress can satisfy it. Arm before triggering a reconnect
        to exclude previous successes, including those at the same board timestamp.
        """
        return self._handshake(completed=True, after=self.handshakes_completed)

    def _handshake(self, *, completed: bool, after: int | None = None) -> Condition[bool]:
        """True once the started or completed count is past `after`, or past 0 without one."""

        def find() -> bool:
            count = self.handshakes_completed if completed else self.handshakes_started
            return count > (after if after is not None else 0)

        event = "complete" if completed else "start"
        scope = "a" if after is None else "the next"
        return Condition(find, f"{scope} TLS handshake to {event}", "TLS")

    def accept(
        self, peer: tuple[str, int], write: Callable[[bytes], None], close: StreamClose
    ) -> "_Session":
        self._log("tls: handshake started")
        stream = _Stream(f"authority {self.ca.number} connection {self._accepted}")
        self._accepted += 1
        return _Session(self, peer, write, close, stream)


class _Session:
    def __init__(
        self,
        tls: Tls,
        peer: tuple[str, int],
        write: Callable[[bytes], None],
        close: StreamClose,
        stream: _Stream,
    ) -> None:
        self._write, self._close = write, close
        self._tls = tls
        self._peer = peer
        self._stream = stream
        self._inner: Connection | None = None
        self._closed = False
        # How the session ended the TCP connection: `None` while it has not, `"close"` after its
        # FIN, `"reset"` after its RST. A reset still goes after a close, as the stack sends it
        # until both FINs have crossed.
        self._ended: str | None = None
        self._started = False
        # What the client sent that is not a whole record yet, and not a whole message yet.
        self._records = b""
        self._messages = b""
        # Every handshake message so far, for Finished and the extended master secret.
        self._transcript = b""
        self._expect = CLIENT_HELLO
        self._out = b""
        self._fragment = MAX_FRAGMENT
        self._read: _Cipher | None = None
        self._sealing: _Cipher | None = None
        self._pending_read: _Cipher | None = None
        self._pending_write: _Cipher | None = None

    @property
    def _ready(self) -> bool:
        return self._inner is not None

    def on_data(self, data: bytes) -> None:
        if self._closed:
            return
        if not self._started and data:
            self._started = True
            self._tls.handshakes_started += 1
        self._records += data
        try:
            while not self._closed and len(self._records) >= 5:
                kind, length = self._records[0], int.from_bytes(self._records[3:5])
                if kind not in (CHANGE_CIPHER_SPEC, ALERT, HANDSHAKE, APPLICATION_DATA):
                    raise _Fatal(10, f"the client sent a record of type {kind}, not TLS")
                if length > MAX_FRAGMENT + 2048:
                    raise _Fatal(22, f"a record of {length} bytes is too long")
                if len(self._records) < 5 + length:
                    break
                fragment = self._records[5 : 5 + length]
                self._records = self._records[5 + length :]
                if self._read is not None:
                    fragment = self._read.open(kind, fragment)
                if len(fragment) > MAX_FRAGMENT:
                    raise _Fatal(22, f"a record of {len(fragment)} plain bytes is too long")
                self._record(kind, fragment)
        except _Fatal as error:
            self._send(ALERT, bytes([2, error.alert]))
            if not self._ready:
                self._tls._log(f"tls: handshake failed: {error}")
            self._flush()
            self._end()
            return
        except _Closed as error:
            if not self._ready:
                self._tls._log(f"tls: handshake failed: {error}")
            self._flush()
            self._end()
            return
        self._flush()

    def _record(self, kind: int, fragment: bytes) -> None:
        if kind == HANDSHAKE:
            self._messages += fragment
            while len(self._messages) >= 4:
                length = int.from_bytes(self._messages[1:4])
                if len(self._messages) < 4 + length:
                    break
                message = self._messages[: 4 + length]
                self._messages = self._messages[4 + length :]
                self._handshake(message[0], message[4:], message)
        elif self._messages:
            raise _Fatal(10, "a handshake message is split around another record")
        elif kind == CHANGE_CIPHER_SPEC:
            if self._expect != CHANGE_CIPHER_SPEC or fragment != b"\x01":
                raise _Fatal(10, "the client changed cipher spec out of turn")
            self._read, self._expect = self._pending_read, FINISHED
        elif kind == ALERT:
            if len(fragment) != 2:
                raise _Fatal(50, "the client sent a malformed alert")
            if fragment[1] == 0:
                raise _Closed("the client closed the connection")
            if fragment[0] == 2:
                raise _Closed(f"the client sent the alert {ALERTS.get(fragment[1], fragment[1])}")
        elif kind == APPLICATION_DATA and self._ready:
            if fragment:
                assert self._inner is not None
                self._inner.on_data(fragment)
        else:
            raise _Fatal(10, f"the client sent a record of type {kind} out of turn")

    def _handshake(self, kind: int, body: bytes, message: bytes) -> None:
        if self._ready and kind == CLIENT_HELLO:
            self._send(ALERT, bytes([1, 100]))  # no_renegotiation, a warning.
            return
        if kind != self._expect:
            raise _Fatal(10, f"the client sent handshake message {kind} out of turn")
        if kind == CLIENT_HELLO:
            self._transcript += message
            self._client_hello(_Reader(body, "ClientHello"))
        elif kind == CLIENT_KEY_EXCHANGE:
            self._transcript += message
            self._client_key_exchange(_Reader(body, "ClientKeyExchange"))
        else:
            self._finished(body, message)

    def _client_hello(self, hello: _Reader) -> None:
        version = hello.take(2)
        self._client_random = hello.take(32)
        hello.vector(1)  # A session ID: the server resumes nothing.
        suites = hello.vector(2)
        codes = [int.from_bytes(suites[i : i + 2]) for i in range(0, len(suites) - 1, 2)]
        if 0 not in hello.vector(1):
            raise _Fatal(40, "the client offers no null compression")
        extensions: dict[int, bytes] = {}
        if not hello.done():
            block = _Reader(hello.vector(2), "ClientHello's extensions")
            while not block.done():
                extension, data = block.number(2), block.vector(2)
                if extension in extensions:
                    raise _Fatal(47, f"the client sent extension {extension} twice")
                extensions[extension] = data
        if SUPPORTED_VERSIONS in extensions:
            offered = extensions[SUPPORTED_VERSIONS][1:]
            if TLS12 not in [offered[i : i + 2] for i in range(0, len(offered), 2)]:
                raise _Fatal(70, "the client does not offer TLS 1.2, the only version served")
        elif version < TLS12:
            raise _Fatal(70, "the client does not offer TLS 1.2, the only version served")
        code = next((c for c in codes if c in SUITES), None)
        if code is None:
            names = ", ".join(s.name for s in SUITES.values())
            raise _Fatal(40, f"no cipher suite in common; the server has {names}")
        self._suite = SUITES[code]
        groups = _codes(extensions.get(SUPPORTED_GROUPS, b"\x00\x02\x00\x17"))
        group = next((g for g in groups if g in GROUPS), None)
        if group is None:
            names = ", ".join(name for name, _, _ in GROUPS.values())
            raise _Fatal(40, f"no group in common; the server has {names}")
        formats = extensions.get(EC_POINT_FORMATS)
        if formats is not None and 0 not in formats[1:]:
            raise _Fatal(47, "the client does not take uncompressed points")
        signatures = _codes(extensions.get(SIGNATURE_ALGORITHMS, b"\x00\x02\x02\x01"))
        signature = next((s for s in signatures if s in SIGNATURES), None)
        if signature is None:
            raise _Fatal(40, "the client takes no RSA PKCS #1 v1.5 signature")
        self._extended = EXTENDED_MASTER_SECRET in extensions
        reply: list[tuple[int, bytes]] = []
        if EMPTY_RENEGOTIATION_INFO_SCSV in codes or RENEGOTIATION_INFO in extensions:
            if extensions.get(RENEGOTIATION_INFO, b"\x00") != b"\x00":
                raise _Fatal(40, "the client renegotiates a connection it has not made")
            reply.append((RENEGOTIATION_INFO, b"\x00"))
        if self._extended:
            reply.append((EXTENDED_MASTER_SECRET, b""))
        if formats is not None:
            reply.append((EC_POINT_FORMATS, b"\x01\x00"))
        if MAX_FRAGMENT_LENGTH in extensions:
            length = extensions[MAX_FRAGMENT_LENGTH]
            if len(length) != 1 or not 1 <= length[0] <= 4:
                raise _Fatal(47, "the client asks for a maximum fragment length that is not one")
            self._fragment = 2 ** (8 + length[0])
            reply.append((MAX_FRAGMENT_LENGTH, length))

        self._server_random = self._stream.read(32)
        server_hello = (
            TLS12
            + self._server_random
            + b"\x00"
            + code.to_bytes(2)
            + b"\x00"
            + _vector(b"".join(c.to_bytes(2) + _vector(d, 2) for c, d in reply), 2)
        )
        certificates = _vector(b"".join(_vector(c, 3) for c in self._tls.ca.chain), 3)
        _, curve, order = GROUPS[group]
        if curve is None:
            self._ephemeral: x25519.X25519PrivateKey | ec.EllipticCurvePrivateKey = (
                x25519.X25519PrivateKey.from_private_bytes(self._stream.read(32))
            )
            point = self._ephemeral.public_key().public_bytes_raw()
        else:
            secret = int.from_bytes(self._stream.read(64)) % (order - 1) + 1
            self._ephemeral = ec.derive_private_key(secret, curve)
            point = self._ephemeral.public_key().public_bytes(
                serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
            )
        self._group = group
        params = b"\x03" + group.to_bytes(2) + _vector(point, 1)
        signed = self._tls.ca.key.sign(
            self._client_random + self._server_random + params,
            padding.PKCS1v15(),
            SIGNATURES[signature],
        )
        key_exchange = params + signature.to_bytes(2) + _vector(signed, 2)
        flight = b"".join(
            self._message(kind, body)
            for kind, body in (
                (SERVER_HELLO, server_hello),
                (CERTIFICATE, certificates),
                (SERVER_KEY_EXCHANGE, key_exchange),
                (SERVER_HELLO_DONE, b""),
            )
        )
        self._send(HANDSHAKE, flight)
        self._expect = CLIENT_KEY_EXCHANGE

    def _client_key_exchange(self, exchange: _Reader) -> None:
        point = exchange.vector(1)
        if not exchange.done():
            raise _Fatal(50, "the ClientKeyExchange is longer than its point")
        try:
            if isinstance(self._ephemeral, x25519.X25519PrivateKey):
                shared = self._ephemeral.exchange(x25519.X25519PublicKey.from_public_bytes(point))
            else:
                curve = GROUPS[self._group][1]
                assert curve is not None
                peer = ec.EllipticCurvePublicKey.from_encoded_point(curve, point)
                shared = self._ephemeral.exchange(ec.ECDH(), peer)
        except ValueError:
            raise _Fatal(47, "the client's ECDHE point is not one") from None
        suite = self._suite
        if self._extended:
            session_hash = hashlib.new(suite.hash, self._transcript).digest()
            seed, label = session_hash, b"extended master secret"
        else:
            seed, label = self._client_random + self._server_random, b"master secret"
        self._master = _prf(suite.hash, shared, label, seed, 48)
        block = _prf(
            suite.hash,
            self._master,
            b"key expansion",
            self._server_random + self._client_random,
            2 * suite.key + 2 * suite.iv,
        )
        client_key, server_key = block[: suite.key], block[suite.key : 2 * suite.key]
        ivs = block[2 * suite.key :]
        self._pending_read = _Cipher(suite, client_key, ivs[: suite.iv])
        self._pending_write = _Cipher(suite, server_key, ivs[suite.iv :])
        self._expect = CHANGE_CIPHER_SPEC

    def _verify_data(self, label: bytes) -> bytes:
        digest = hashlib.new(self._suite.hash, self._transcript).digest()
        return _prf(self._suite.hash, self._master, label, digest, 12)

    def _finished(self, body: bytes, message: bytes) -> None:
        if not hmac.compare_digest(body, self._verify_data(b"client finished")):
            raise _Fatal(51, "the client's Finished does not match the handshake")
        self._transcript += message
        self._send(CHANGE_CIPHER_SPEC, b"\x01")
        self._sealing = self._pending_write
        self._send(HANDSHAKE, self._message(FINISHED, self._verify_data(b"server finished")))
        self._expect = -1
        self._tls.handshakes_completed += 1
        self._tls._log("tls: handshake done, TLSv1.2")
        self._flush()
        self._inner = self._tls.inner.accept(self._peer, self._send_plain, self._end)

    def _message(self, kind: int, body: bytes) -> bytes:
        message = bytes([kind]) + _vector(body, 3)
        self._transcript += message
        return message

    def _send(self, kind: int, data: bytes) -> None:
        for at in range(0, max(len(data), 1), self._fragment):
            fragment = data[at : at + self._fragment]
            if self._sealing is not None:
                fragment = self._sealing.seal(kind, fragment)
            self._out += bytes([kind]) + TLS12 + _vector(fragment, 2)

    def _flush(self) -> None:
        data, self._out = self._out, b""
        if data:
            self._write(data)

    def _send_plain(self, data: bytes) -> None:
        if self._closed or not data:
            return
        self._send(APPLICATION_DATA, data)
        self._flush()

    def _end(self, *, reset: bool = False) -> None:
        """Ends the connection: the inner service's `close`, which passes `reset` through, so that
        a reset ends the TCP connection with its RST and no alert before it. The inner service
        hears of the end once (`on_close`); the TCP connection gets one close and one reset at
        most, a reset after the session's close or the device's FIN too, from the inner
        service's `on_close` as well."""
        if not self._closed:
            self.on_close()
        if self._ended == "reset" or (self._ended == "close" and not reset):
            return
        self._ended = "reset" if reset else "close"
        if reset:
            self._close(reset=True)
        else:
            self._close()

    def on_close(self) -> None:
        if not self._closed:
            self._closed = True
            if self._inner is not None:
                self._inner.on_close()


def _codes(data: bytes) -> list[int]:
    """The two-byte codes of a ClientHello extension's list, after its length."""
    return [int.from_bytes(data[i : i + 2]) for i in range(2, len(data) - 1, 2)]
