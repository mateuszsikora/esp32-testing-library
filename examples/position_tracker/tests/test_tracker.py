"""Positions and UTC at the broker, through GNSS, PPP, NTP and real TLS."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from tracker_support import (
    BROKER_HOST,
    BROKER_PORT,
    TOPIC,
    Reports,
    configure_tracker,
    serve_broker,
)

from esp32tl import Board
from esp32tl.net.tls import CertificateAuthority
from esp32tl.units import NORTH, Position, about, km_h, m, seconds

pytestmark = [pytest.mark.app_path(".."), pytest.mark.board_profile("lilygo-t-a7670g-r2")]
WARSAW = Position(52.229677, 21.012233)
WORLD = datetime(2031, 5, 6, 12, 34, 56, tzinfo=UTC)
STARTUP = seconds(60)
INTERVAL = seconds(10)
JITTER = seconds(0.25)  # RTOS scheduling, TLS records and UART transport, on the board's clock.
NTP_OUTAGE = seconds(30)
REJECTION_WINDOW = seconds(30)  # Covers the MQTT client's 10-second reconnect delay.
UTC_TOLERANCE = timedelta(seconds=1.25)  # Whole-second timestamps and NTP/transport latency.
# The client's wait for a CONNACK, its reconnect delay and a new TLS handshake.
RECONNECT_WINDOW = seconds(30)


def test_reports_on_connect(board: Board) -> None:
    network, tls = serve_broker(board)
    reports = Reports(network.broker)
    configure_tracker(board, trusted_ca=tls.ca.ca_pem)
    connected, report = network.broker.next_connection(), reports.when_received()
    board.power_on(utc=WORLD)

    client = board.wait_until(connected, within=STARTUP)
    first = board.wait_until(report, within=seconds(1))
    assert first.received_at - client.at < seconds(1)


def test_reports_every_ten_seconds(board: Board) -> None:
    network, tls = serve_broker(board)
    reports = Reports(network.broker)
    configure_tracker(board, trusted_ca=tls.ca.ca_pem)
    board.power_on(utc=WORLD)
    first = board.wait_until(reports.when_received(), within=STARTUP)

    for number in range(1, 4):
        deadline = first.received_at + number * INTERVAL
        board.wait(deadline - JITTER - board.now)
        assert len(reports.history) == number  # No early or extra tracker reports.
        report = board.wait_until(reports.when_received(), within=2 * JITTER)
        assert report.received_at == about(deadline, within=JITTER)
    assert len(reports.history) == 4


def test_reported_position_follows_the_receiver(board: Board) -> None:
    network, tls = serve_broker(board)
    reports = Reports(network.broker)
    configure_tracker(board, trusted_ca=tls.ca.ca_pem)
    start = Position(-34.6037, -58.3816)
    board.gnss.fix(at=start)
    board.power_on(utc=WORLD)
    first = board.wait_until(reports.when_received(with_position=True), within=STARTUP)
    assert first.position is not None
    assert first.position.distance_to(start) == about(m(0), within=m(2))

    speed = km_h(36)
    moved_at = board.now
    board.gnss.move(heading=NORTH, speed=speed)
    report = board.wait_until(reports.when_received(), within=INTERVAL + JITTER)
    expected = start.moved(NORTH, speed * (report.received_at - moved_at))
    # NMEA arrives once a second; the latest fix can lag by one receiver epoch.
    assert report.position is not None
    assert report.position.distance_to(expected) == about(m(0), within=m(15))
    assert report.position.distance_to(start) > m(80)


@pytest.mark.parametrize("world", [WORLD, datetime(2042, 9, 10, 3, 4, 5, tzinfo=UTC)])
def test_report_carries_the_worlds_time(board: Board, world: datetime) -> None:
    network, tls = serve_broker(board)
    reports = Reports(network.broker)
    configure_tracker(board, trusted_ca=tls.ca.ca_pem)
    board.power_on(utc=world)
    for _ in range(2):
        report = board.wait_until(reports.when_received(), within=STARTUP)
        expected = world + timedelta(seconds=report.received_at.seconds)
        assert abs(report.utc - expected) < UTC_TOLERANCE


def test_without_a_fix_reports_null_coordinates(board: Board) -> None:
    network, tls = serve_broker(board)
    reports = Reports(network.broker)
    configure_tracker(board, trusted_ca=tls.ca.ca_pem)
    board.gnss.lose_fix()
    board.power_on(utc=WORLD)
    board.wait(seconds(2))  # Let the receiver deliver its no-fix NMEA epoch.
    report = board.wait_until(reports.when_received(), within=INTERVAL + JITTER)
    assert report.position is None


def test_losing_the_fix_clears_the_reported_position(board: Board) -> None:
    network, tls = serve_broker(board)
    reports = Reports(network.broker)
    configure_tracker(board, trusted_ca=tls.ca.ca_pem)
    board.gnss.fix(at=WARSAW)
    board.power_on(utc=WORLD)
    first = board.wait_until(reports.when_received(with_position=True), within=STARTUP)
    assert first.position == WARSAW

    board.gnss.lose_fix()
    report = board.wait_until(reports.when_received(), within=INTERVAL + JITTER)
    assert report.position is None


def test_a_silent_receiver_expires_the_reported_position(board: Board) -> None:
    network, tls = serve_broker(board)
    reports = Reports(network.broker)
    configure_tracker(board, trusted_ca=tls.ca.ca_pem)
    board.gnss.fix(at=WARSAW)
    board.power_on(utc=WORLD)
    first = board.wait_until(reports.when_received(with_position=True), within=STARTUP)
    assert first.position == WARSAW

    board.gnss.go_silent()
    report = board.wait_until(reports.when_received(), within=INTERVAL + JITTER)
    assert report.position is None


def test_publishes_unretained_qos_zero_over_tls(board: Board) -> None:
    network, tls = serve_broker(board)
    configure_tracker(board, trusted_ca=tls.ca.ca_pem)
    board.power_on(utc=WORLD)
    message = board.wait_until(network.broker.next_message(TOPIC), within=STARTUP)
    assert message.qos == 0 and not message.retain
    assert BROKER_HOST in network.dns.asked
    assert tls.handshake_completed()
    assert board.properties()["warnings"] == []


def test_refuses_an_untrusted_ca(board: Board) -> None:
    network, tls = serve_broker(board)
    unknown_ca = CertificateAuthority([BROKER_HOST])
    configure_tracker(board, trusted_ca=unknown_ca.ca_pem)
    board.power_on(utc=WORLD)
    board.wait_until(tls.handshake_started, within=STARTUP)
    board.wait(REJECTION_WINDOW)
    assert not network.broker.clients
    assert not network.broker.messages


def test_refuses_the_wrong_server_name(board: Board) -> None:
    network, tls = serve_broker(board)
    configure_tracker(board, trusted_ca=tls.ca.ca_pem, broker_host="impostor.test")
    board.power_on(utc=WORLD)
    board.wait_until(tls.handshake_started, within=STARTUP)
    board.wait(REJECTION_WINDOW)
    assert not network.broker.clients
    assert not network.broker.messages


@pytest.mark.parametrize(
    ("not_before", "not_after"),
    [
        (datetime(2020, 1, 1, tzinfo=UTC), datetime(2030, 1, 1, tzinfo=UTC)),
        (datetime(2032, 1, 1, tzinfo=UTC), datetime(2040, 1, 1, tzinfo=UTC)),
    ],
    ids=["expired", "not-yet-valid"],
)
def test_refuses_a_server_certificate_outside_its_validity_period(
    board: Board, not_before: datetime, not_after: datetime
) -> None:
    ca = CertificateAuthority(
        [BROKER_HOST], server_not_before=not_before, server_not_after=not_after
    )
    network, tls = serve_broker(board, ca=ca)
    configure_tracker(board, trusted_ca=tls.ca.ca_pem)
    board.power_on(utc=WORLD)
    board.wait_until(tls.handshake_started, within=STARTUP)
    board.wait(REJECTION_WINDOW)
    assert network.ntp.requests > 0
    assert not network.broker.clients
    assert not network.broker.messages


def test_accepts_a_server_certificate_valid_at_the_ntp_time(board: Board) -> None:
    ca = CertificateAuthority(
        [BROKER_HOST],
        server_not_before=datetime(2030, 1, 1, tzinfo=UTC),
        server_not_after=datetime(2032, 1, 1, tzinfo=UTC),
    )
    network, tls = serve_broker(board, ca=ca)
    reports = Reports(network.broker)
    configure_tracker(board, trusted_ca=tls.ca.ca_pem)
    board.power_on(utc=WORLD)
    report = board.wait_until(reports.when_received(), within=STARTUP)
    expected = WORLD + timedelta(seconds=report.received_at.seconds)
    assert abs(report.utc - expected) < UTC_TOLERANCE
    assert network.ntp.requests > 0
    assert "time.test" in network.dns.asked


def test_waits_for_ntp_and_connects_when_it_returns(board: Board) -> None:
    network, tls = serve_broker(board)
    reports = Reports(network.broker)
    configure_tracker(board, trusted_ca=tls.ca.ca_pem)
    network.ntp.go_silent()
    board.gnss.fix(at=WARSAW)
    board.power_on(utc=WORLD)
    board.wait_until(lambda: "time.test" in network.dns.asked, within=STARTUP)
    board.wait(NTP_OUTAGE)
    assert BROKER_HOST not in network.dns.asked
    assert not tls.handshake_started()
    assert not network.broker.clients
    assert not network.broker.messages

    network.ntp.resume()
    report = board.wait_until(reports.when_received(with_position=True), within=STARTUP)
    assert tls.handshake_started()
    assert network.ntp.requests > 0
    assert report.position == WARSAW
    expected = WORLD + timedelta(seconds=report.received_at.seconds)
    assert abs(report.utc - expected) < UTC_TOLERANCE


def test_missing_configuration_does_not_connect(board: Board) -> None:
    network = board.modem.network()
    board.power_on(utc=WORLD)
    board.wait_until(
        board.console.line("position_tracker: invalid or missing NVS configuration"),
        within=seconds(5),
    )
    board.wait(seconds(2))
    assert not network.broker.clients
    assert not network.broker.messages
    assert network.ntp.requests == 0


def test_a_broker_that_never_answers_gets_tried_again(board: Board) -> None:
    network = board.modem.network()
    broker = SilentBroker()
    ca = CertificateAuthority([BROKER_HOST])
    network.listen(BROKER_PORT, broker, tls=ca)
    configure_tracker(board, trusted_ca=ca.ca_pem)
    board.power_on(utc=WORLD)

    board.wait_until(lambda: broker.connections and broker.connections[0].heard, within=STARTUP)
    connect = bytes(broker.connections[0].heard)  # The whole CONNECT: it comes in one TLS record.
    assert connect[0] == 0x10  # MQTT CONNECT
    assert b"position-tracker" in connect
    board.wait_until(lambda: len(broker.connections) > 1, within=RECONNECT_WINDOW)
    assert broker.open_when_accepted[1] == 0  # The first was closed before the second opened.


class SilentBroker:
    """Takes every connection and answers nothing, as a broker hung before its CONNACK."""

    def __init__(self) -> None:
        self.connections: list[Connection] = []
        self.open_when_accepted: list[int] = []  # Connections still open at each accept.

    def accept(
        self, peer: tuple[str, int], write: Callable[[bytes], None], close: Callable[[], None]
    ) -> "Connection":
        self.open_when_accepted.append(sum(not c.closed for c in self.connections))
        self.connections.append(Connection())
        return self.connections[-1]


class Connection:
    def __init__(self) -> None:
        self.heard = bytearray()
        self.closed = False

    def on_data(self, data: bytes) -> None:
        self.heard += data

    def on_close(self) -> None:
        self.closed = True
