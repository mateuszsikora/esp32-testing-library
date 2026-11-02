"""Positions and UTC at the broker, through GNSS, PPP, NTP and real TLS."""

from datetime import UTC, datetime, timedelta

import pytest
from tracker_support import BROKER_HOST, BROKER_PORT, TOPIC, Reports, configure_tracker

from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.interfaces.console import Console
from esp32tl.interfaces.gnss import Gnss
from esp32tl.interfaces.modem import Modem
from esp32tl.net.mqtt import Broker
from esp32tl.net.tls import CertificateAuthority, Tls
from esp32tl.units import NORTH, Position, about, km_h, m, seconds

pytestmark = [pytest.mark.app_path(".."), pytest.mark.board_profile("lilygo-t-a7670g-r2")]
WARSAW = Position(52.229677, 21.012233)
WORLD = datetime(2031, 5, 6, 12, 34, 56, tzinfo=UTC)
STARTUP = seconds(60)
INTERVAL = seconds(10)
JITTER = seconds(0.25)  # RTOS scheduling, TLS records and UART transport, on the board's clock.
REJECTION_WINDOW = seconds(30)  # Covers the MQTT client's 10-second reconnect delay.
UTC_TOLERANCE = timedelta(seconds=1.25)  # Whole-second timestamps and NTP/transport latency.


@pytest.mark.usefixtures("tls_listener")
def test_reports_on_connect(
    board: Esp32Sim, broker: Broker, broker_ca: CertificateAuthority, reports: Reports
) -> None:
    configure_tracker(board, trusted_ca=broker_ca.ca_pem)
    connected, report = broker.connected(), reports.when_received()
    board.power_on(utc=WORLD)

    client = board.wait_until(connected, within=STARTUP)
    first = board.wait_until(report, within=seconds(1))
    assert first.received_at - client.at < seconds(1)


@pytest.mark.usefixtures("tls_listener")
def test_reports_every_ten_seconds(
    board: Esp32Sim, broker_ca: CertificateAuthority, reports: Reports
) -> None:
    configure_tracker(board, trusted_ca=broker_ca.ca_pem)
    board.power_on(utc=WORLD)
    first = board.wait_until(reports.when_received(), within=STARTUP)

    for number in range(1, 4):
        deadline = first.received_at + number * INTERVAL
        board.wait(deadline - JITTER - board.now)
        assert len(reports.history) == number  # No early or extra tracker reports.
        report = board.wait_until(reports.when_received(), within=2 * JITTER)
        assert report.received_at == about(deadline, within=JITTER)
    assert len(reports.history) == 4


@pytest.mark.usefixtures("tls_listener")
def test_reported_position_follows_the_receiver(
    board: Esp32Sim, gnss: Gnss, broker_ca: CertificateAuthority, reports: Reports
) -> None:
    configure_tracker(board, trusted_ca=broker_ca.ca_pem)
    start = Position(-34.6037, -58.3816)
    gnss.fix(at=start)
    board.power_on(utc=WORLD)
    first = board.wait_until(reports.when_received(with_position=True), within=STARTUP)
    assert first.position is not None
    assert first.position.distance_to(start) == about(m(0), within=m(2))

    speed = km_h(36)
    moved_at = board.now
    gnss.move(heading=NORTH, speed=speed)
    report = board.wait_until(reports.when_received(), within=INTERVAL + JITTER)
    expected = start.moved(NORTH, speed * (report.received_at - moved_at))
    # NMEA arrives once a second; the latest fix can lag by one receiver epoch.
    assert report.position is not None
    assert report.position.distance_to(expected) == about(m(0), within=m(15))
    assert report.position.distance_to(start) > m(80)


@pytest.mark.parametrize("world", [WORLD, datetime(2042, 9, 10, 3, 4, 5, tzinfo=UTC)])
@pytest.mark.usefixtures("tls_listener")
def test_report_carries_the_worlds_time(
    board: Esp32Sim, broker_ca: CertificateAuthority, reports: Reports, world: datetime
) -> None:
    configure_tracker(board, trusted_ca=broker_ca.ca_pem)
    board.power_on(utc=world)
    for _ in range(2):
        report = board.wait_until(reports.when_received(), within=STARTUP)
        expected = world + timedelta(seconds=report.received_at.seconds)
        assert abs(report.utc - expected) < UTC_TOLERANCE


@pytest.mark.usefixtures("tls_listener")
def test_without_a_fix_reports_null_coordinates(
    board: Esp32Sim, gnss: Gnss, broker_ca: CertificateAuthority, reports: Reports
) -> None:
    configure_tracker(board, trusted_ca=broker_ca.ca_pem)
    gnss.lose_fix()
    board.power_on(utc=WORLD)
    board.wait(seconds(2))  # Let the receiver deliver its no-fix NMEA epoch.
    report = board.wait_until(reports.when_received(), within=INTERVAL + JITTER)
    assert report.position is None


@pytest.mark.usefixtures("tls_listener")
def test_losing_the_fix_clears_the_reported_position(
    board: Esp32Sim, gnss: Gnss, broker_ca: CertificateAuthority, reports: Reports
) -> None:
    configure_tracker(board, trusted_ca=broker_ca.ca_pem)
    gnss.fix(at=WARSAW)
    board.power_on(utc=WORLD)
    first = board.wait_until(reports.when_received(with_position=True), within=STARTUP)
    assert first.position == WARSAW

    gnss.lose_fix()
    report = board.wait_until(reports.when_received(), within=INTERVAL + JITTER)
    assert report.position is None


@pytest.mark.usefixtures("tls_listener")
def test_a_silent_receiver_expires_the_reported_position(
    board: Esp32Sim, gnss: Gnss, broker_ca: CertificateAuthority, reports: Reports
) -> None:
    configure_tracker(board, trusted_ca=broker_ca.ca_pem)
    gnss.fix(at=WARSAW)
    board.power_on(utc=WORLD)
    first = board.wait_until(reports.when_received(with_position=True), within=STARTUP)
    assert first.position == WARSAW

    gnss.go_silent()
    report = board.wait_until(reports.when_received(), within=INTERVAL + JITTER)
    assert report.position is None


@pytest.mark.usefixtures("tls_listener")
def test_publishes_unretained_qos_zero_over_tls(
    board: Esp32Sim, modem: Modem, broker: Broker, broker_ca: CertificateAuthority
) -> None:
    configure_tracker(board, trusted_ca=broker_ca.ca_pem)
    board.power_on(utc=WORLD)
    message = board.wait_until(broker.message(TOPIC), within=STARTUP)
    assert message.qos == 0 and not message.retain
    assert BROKER_HOST in modem.network().dns.asked
    assert "tls: handshake done" in board.report()
    assert board.link.warnings == []


def test_refuses_an_untrusted_ca(board: Esp32Sim, broker: Broker, tls_listener: Tls) -> None:
    unknown_ca = CertificateAuthority([BROKER_HOST])
    configure_tracker(board, trusted_ca=unknown_ca.ca_pem)
    board.power_on(utc=WORLD)
    board.wait_until(lambda: tls_listener.handshakes_started > 0, within=STARTUP)
    board.wait(REJECTION_WINDOW)
    assert not broker.clients
    assert not broker.messages


def test_refuses_the_wrong_server_name(
    board: Esp32Sim, broker: Broker, broker_ca: CertificateAuthority, tls_listener: Tls
) -> None:
    configure_tracker(board, trusted_ca=broker_ca.ca_pem, broker_host="impostor.test")
    board.power_on(utc=WORLD)
    board.wait_until(lambda: tls_listener.handshakes_started > 0, within=STARTUP)
    board.wait(REJECTION_WINDOW)
    assert not broker.clients
    assert not broker.messages


@pytest.mark.parametrize(
    ("not_before", "not_after"),
    [
        (datetime(2020, 1, 1, tzinfo=UTC), datetime(2030, 1, 1, tzinfo=UTC)),
        (datetime(2032, 1, 1, tzinfo=UTC), datetime(2040, 1, 1, tzinfo=UTC)),
    ],
    ids=["expired", "not-yet-valid"],
)
def test_refuses_a_server_certificate_outside_its_validity_period(
    board: Esp32Sim, modem: Modem, broker: Broker, not_before: datetime, not_after: datetime
) -> None:
    ca = CertificateAuthority(
        [BROKER_HOST], server_not_before=not_before, server_not_after=not_after
    )
    tls_listener = modem.network().listen(BROKER_PORT, broker, tls=ca)
    configure_tracker(board, trusted_ca=ca.ca_pem)
    board.power_on(utc=WORLD)
    board.wait_until(lambda: tls_listener.handshakes_started > 0, within=STARTUP)
    board.wait(REJECTION_WINDOW)
    assert modem.network().ntp.requests > 0
    assert not broker.clients
    assert not broker.messages


def test_accepts_a_server_certificate_valid_at_the_ntp_time(
    board: Esp32Sim, modem: Modem, broker: Broker, reports: Reports
) -> None:
    ca = CertificateAuthority(
        [BROKER_HOST],
        server_not_before=datetime(2030, 1, 1, tzinfo=UTC),
        server_not_after=datetime(2032, 1, 1, tzinfo=UTC),
    )
    modem.network().listen(BROKER_PORT, broker, tls=ca)
    configure_tracker(board, trusted_ca=ca.ca_pem)
    board.power_on(utc=WORLD)
    report = board.wait_until(reports.when_received(), within=STARTUP)
    expected = WORLD + timedelta(seconds=report.received_at.seconds)
    assert abs(report.utc - expected) < UTC_TOLERANCE
    assert modem.network().ntp.requests > 0
    assert "time.test" in modem.network().dns.asked


@pytest.mark.usefixtures("tls_listener")
def test_waits_for_ntp_before_connecting(
    board: Esp32Sim,
    gnss: Gnss,
    modem: Modem,
    broker: Broker,
    broker_ca: CertificateAuthority,
    console: Console,
) -> None:
    configure_tracker(board, trusted_ca=broker_ca.ca_pem)
    modem.network().ntp.go_silent()
    gnss.fix(at=WARSAW)
    board.power_on(utc=WORLD)
    board.wait_until(console.line("position_tracker: waiting for NTP"), within=seconds(15))
    assert "time.test" in modem.network().dns.asked
    assert BROKER_HOST not in modem.network().dns.asked
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


@pytest.fixture
def broker_ca() -> CertificateAuthority:
    return CertificateAuthority([BROKER_HOST])


@pytest.fixture
def tls_listener(modem: Modem, broker: Broker, broker_ca: CertificateAuthority) -> Tls:
    """Serve TLS in the simulated world; do not configure or power on the firmware."""
    return modem.network().listen(BROKER_PORT, broker, tls=broker_ca)


@pytest.fixture
def reports(broker: Broker) -> Reports:
    """Observe the broker independently of whether or how it is served."""
    return Reports(broker)
