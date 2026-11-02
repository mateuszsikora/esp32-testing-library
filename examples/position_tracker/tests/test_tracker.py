"""Positions and UTC at the broker, through GNSS, PPP, NTP and real TLS."""

import json
import ssl
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.flash import u16
from esp32tl.interfaces.console import Console
from esp32tl.interfaces.gnss import Gnss
from esp32tl.interfaces.modem import Modem
from esp32tl.net.mqtt import Broker, Message
from esp32tl.net.tls import CertificateAuthority
from esp32tl.units import NORTH, Position, about, km_h, m, seconds

pytestmark = [pytest.mark.app_path(".."), pytest.mark.board_profile("lilygo-t-a7670g-r2")]
WARSAW = Position(52.229677, 21.012233)
WORLD = datetime(2031, 5, 6, 12, 34, 56, tzinfo=UTC)
TOPIC = "tracker/position"
INTERVAL = seconds(10)
JITTER = seconds(0.25)  # RTOS scheduling, TLS records and UART transport, on the board's clock.


def test_reports_on_connect(board: Esp32Sim, gnss: Gnss, modem: Modem, broker: Broker) -> None:
    configure_tracker(board, modem)
    gnss.fix(at=WARSAW)
    board.power_on(utc=WORLD)

    message = board.wait_until(broker.message(TOPIC), within=seconds(60))
    assert message.at - broker.clients[0].at < seconds(1)
    assert set(json.loads(message.text)) == {"fix", "lat", "lon", "time"}
    assert message.qos == 0 and not message.retain
    assert "broker.test" in modem.network().dns.asked
    assert modem.network().ntp.requests > 0
    assert "tls: handshake done" in board.report()
    assert board.link.warnings == []


def test_reports_every_ten_seconds(
    board: Esp32Sim, gnss: Gnss, modem: Modem, broker: Broker
) -> None:
    configure_tracker(board, modem)
    gnss.fix(at=WARSAW)
    board.power_on(utc=WORLD)
    first = board.wait_until(broker.message(TOPIC), within=seconds(60))

    for number in range(1, 4):
        deadline = first.at + number * INTERVAL
        board.wait(deadline - JITTER - board.now)
        # History catches an early report even if it arrived between test observations.
        assert len(broker.messages) == number
        message = board.wait_until(broker.message(TOPIC), within=2 * JITTER)
        assert message.at == about(deadline, within=JITTER)
    assert len(broker.messages) == 4


def test_reported_position_follows_the_receiver(
    board: Esp32Sim, gnss: Gnss, modem: Modem, broker: Broker
) -> None:
    configure_tracker(board, modem)
    start = Position(-34.6037, -58.3816)
    gnss.fix(at=start)
    board.power_on(utc=WORLD)
    board.wait_until(broker.message(TOPIC), within=seconds(60))
    # The connect report may precede the receiver's first one-second epoch.
    first = board.wait_until(broker.message(TOPIC), within=INTERVAL + JITTER)
    assert reported_position(first).distance_to(start) == about(m(0), within=m(2))

    speed = km_h(36)
    moved_at = board.now
    gnss.move(heading=NORTH, speed=speed)
    message = board.wait_until(broker.message(TOPIC), within=INTERVAL + JITTER)
    expected = start.moved(NORTH, speed * (message.at - moved_at))
    # NMEA arrives once a second; the latest fix can lag by one receiver epoch.
    assert reported_position(message).distance_to(expected) == about(m(0), within=m(15))
    assert reported_position(message).distance_to(start) > m(80)


@pytest.mark.parametrize("world", [WORLD, datetime(2042, 9, 10, 3, 4, 5, tzinfo=UTC)])
def test_report_carries_the_worlds_time(
    board: Esp32Sim, gnss: Gnss, modem: Modem, broker: Broker, world: datetime
) -> None:
    configure_tracker(board, modem)
    gnss.fix(at=WARSAW)
    board.power_on(utc=world)
    for _ in range(2):
        message = board.wait_until(broker.message(TOPIC), within=seconds(60))
        reported = json.loads(message.text)["time"]
        assert isinstance(reported, int)
        # Whole-second Unix time plus a small allowance for delivery and NTP round trip.
        assert abs(reported - (world.timestamp() + message.at.seconds)) < 1.25
    assert modem.network().ntp.requests > 0
    assert "time.test" in modem.network().dns.asked


def test_without_a_fix_reports_null_coordinates(
    board: Esp32Sim, gnss: Gnss, modem: Modem, broker: Broker
) -> None:
    configure_tracker(board, modem)
    gnss.lose_fix()
    board.power_on(utc=WORLD)
    message = board.wait_until(broker.message(TOPIC), within=seconds(60))
    assert_no_position(message)


@pytest.mark.parametrize("silent", [False, True], ids=["lost-fix", "silent-receiver"])
def test_stops_reporting_a_stale_position(
    board: Esp32Sim, gnss: Gnss, modem: Modem, broker: Broker, silent: bool
) -> None:
    configure_tracker(board, modem)
    gnss.fix(at=WARSAW)
    board.power_on(utc=WORLD)
    board.wait_until(broker.message(TOPIC), within=seconds(60))
    first = board.wait_until(broker.message(TOPIC), within=INTERVAL + JITTER)
    reported_position(first)
    if silent:
        gnss.go_silent()
    else:
        gnss.lose_fix()
    message = board.wait_until(broker.message(TOPIC), within=INTERVAL + JITTER)
    assert_no_position(message)


@pytest.mark.parametrize("wrong", ["ca", "hostname"])
def test_refuses_an_untrusted_broker(
    board: Esp32Sim, gnss: Gnss, modem: Modem, broker: Broker, console: Console, wrong: str
) -> None:
    server = CertificateAuthority(["broker.test"])
    trusted = CertificateAuthority(["broker.test"]) if wrong == "ca" else server
    configure_tracker(
        board,
        modem,
        server=server,
        trusted=trusted,
        host="impostor.test" if wrong == "hostname" else "broker.test",
    )
    gnss.fix(at=WARSAW)
    board.power_on(utc=WORLD)
    board.wait_until(console.line("Failed to verify peer certificate"), within=seconds(60))
    assert not broker.clients
    assert not broker.messages
    assert modem.network().ntp.requests > 0


@pytest.mark.parametrize(
    ("not_before", "not_after"),
    [(2020, 2030), (2032, 2040)],
    ids=["expired", "not-yet-valid"],
)
def test_refuses_a_server_certificate_outside_its_validity_period(
    board: Esp32Sim,
    modem: Modem,
    broker: Broker,
    console: Console,
    tmp_path: Path,
    not_before: int,
    not_after: int,
) -> None:
    server = DatedBroker(tmp_path, not_before=not_before, not_after=not_after)
    configure_tracker(board, modem, server=server)
    board.power_on(utc=WORLD)
    board.wait_until(console.line("Failed to verify peer certificate"), within=seconds(60))
    assert modem.network().ntp.requests > 0
    assert not broker.clients
    assert not broker.messages


def test_accepts_a_server_certificate_valid_at_the_ntp_time(
    board: Esp32Sim, modem: Modem, broker: Broker, tmp_path: Path
) -> None:
    # The same certificate construction as the rejection cases, valid around WORLD (2031).
    server = DatedBroker(tmp_path, not_before=2030, not_after=2032)
    configure_tracker(board, modem, server=server)
    board.power_on(utc=WORLD)
    message = board.wait_until(broker.message(TOPIC), within=seconds(60))
    assert abs(json.loads(message.text)["time"] - (WORLD.timestamp() + message.at.seconds)) < 1.25
    assert modem.network().ntp.requests > 0


def test_waits_for_ntp_before_connecting(
    board: Esp32Sim, gnss: Gnss, modem: Modem, broker: Broker, console: Console
) -> None:
    configure_tracker(board, modem)
    del modem.network().ip.udp[123]  # A reachable network with no NTP responder.
    gnss.fix(at=WARSAW)
    board.power_on(utc=WORLD)
    board.wait_until(console.line("position_tracker: waiting for NTP"), within=seconds(15))
    assert "time.test" in modem.network().dns.asked
    assert "broker.test" not in modem.network().dns.asked
    assert not broker.clients
    assert not broker.messages


def test_missing_configuration_does_not_connect(
    board: Esp32Sim, modem: Modem, broker: Broker, console: Console
) -> None:
    board.power_on(utc=WORLD)
    board.wait_until(
        console.line("position_tracker: invalid or missing NVS configuration"),
        within=seconds(5),
    )
    board.wait(seconds(2))
    assert not broker.clients
    assert not broker.messages
    assert modem.network().ntp.requests == 0


def configure_tracker(
    board: Esp32Sim,
    modem: Modem,
    *,
    server: CertificateAuthority | None = None,
    trusted: CertificateAuthority | None = None,
    host: str = "broker.test",
) -> None:
    """The application's NVS schema and a TLS listener, all selected before power-on."""
    server = server or CertificateAuthority([host])
    network = modem.network()
    network.listen(8883, network.broker, tls=server)
    board.nvs(
        {
            "config": {
                "broker_host": host,
                "broker_port": u16(8883),
                "broker_ca": (trusted or server).ca_pem,
                "ntp_host": "time.test",
                "apn": "internet",
            }
        }
    )


def reported_position(message: Message) -> Position:
    report = json.loads(message.text)
    assert report["fix"] is True
    return Position(report["lat"], report["lon"])


def assert_no_position(message: Message) -> None:
    report = json.loads(message.text)
    assert report["fix"] is False
    assert report["lat"] is None and report["lon"] is None


class DatedBroker(CertificateAuthority):
    """A valid CA signing a broker certificate with independently controlled validity."""

    def __init__(self, directory: Path, *, not_before: int, not_after: int) -> None:
        ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "tracker test CA")])
        ca = (
            x509.CertificateBuilder()
            .subject_name(ca_name)
            .issuer_name(ca_name)
            .public_key(ca_key.public_key())
            .serial_number(1)
            .not_valid_before(datetime(2000, 1, 1, tzinfo=UTC))
            .not_valid_after(datetime(2099, 12, 31, tzinfo=UTC))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(False, False, False, False, False, True, True, False, False),
                critical=True,
            )
            .sign(ca_key, hashes.SHA256())
        )
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        leaf = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "broker.test")]))
            .issuer_name(ca_name)
            .public_key(key.public_key())
            .serial_number(2)
            .not_valid_before(datetime(not_before, 1, 1, tzinfo=UTC))
            .not_valid_after(datetime(not_after, 1, 1, tzinfo=UTC))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("broker.test")]), False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
            .sign(ca_key, hashes.SHA256())
        )
        self.ca_pem = ca.public_bytes(serialization.Encoding.PEM).decode()
        chain = directory / "broker.pem"
        chain.touch(mode=0o600)
        chain.write_bytes(
            leaf.public_bytes(serialization.Encoding.PEM)
            + ca.public_bytes(serialization.Encoding.PEM)
            + key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.context.load_cert_chain(chain)
