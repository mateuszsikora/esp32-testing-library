"""Report waits must neither reuse history nor hide malformed firmware publications."""

import json
from datetime import UTC, datetime

import pytest
from tracker_support import TOPIC, Reports, serve_broker

from esp32tl import Board
from esp32tl.interfaces import NeverHappened
from esp32tl.net.mqtt import Broker, Message
from esp32tl.units import Position, seconds

POSITION = {"fix": True, "lat": 52.229677, "lon": 21.012233, "time": 1935837296}
NO_FIX = {**POSITION, "fix": False, "lat": None, "lon": None}


def received(
    broker: Broker, payload: object, *, topic: str = TOPIC, client: str = "tracker"
) -> None:
    broker.messages.append(
        Message(
            seconds(len(broker.messages)), client, topic, json.dumps(payload).encode(), 0, False
        )
    )


def test_waits_start_now_filter_the_topic_and_preserve_the_first_match() -> None:
    broker = Broker(lambda: 0)
    received(broker, POSITION)
    reports = Reports(broker)
    next_report, positioned = reports.when_received(), reports.when_received(with_position=True)
    assert next_report() is None and positioned() is None
    received(broker, "unrelated telemetry", topic="tracker/health")
    received(broker, POSITION, client="(test)")
    assert next_report() is None and positioned() is None
    received(broker, NO_FIX)
    without_fix = next_report()
    assert without_fix is not None and without_fix.position is None
    assert positioned() is None
    received(broker, POSITION)
    with_fix = positioned()
    assert with_fix is not None and with_fix.position == Position(52.229677, 21.012233)
    assert with_fix.utc == datetime(2031, 5, 6, 12, 34, 56, tzinfo=UTC)
    assert with_fix.received_at == seconds(4)
    assert next_report() == without_fix  # Repeated polling retains the first matching report.
    assert len(reports.history) == 3
    assert reports.when_received()() is None


@pytest.mark.parametrize(
    "bad_report",
    [
        [],
        {},
        {**POSITION, "extra": 0},
        {**POSITION, "fix": 1},
        {**POSITION, "time": True},
        {**POSITION, "time": "1935837296"},
        {**POSITION, "lat": True},
        {**POSITION, "lat": 91},
        {**NO_FIX, "lat": 52.229677},
        {**POSITION, "lon": None},
    ],
)
def test_position_wait_rejects_malformed_reports_instead_of_skipping_them(
    bad_report: object,
) -> None:
    broker = Broker(lambda: 0)
    waiting = Reports(broker).when_received(with_position=True)
    received(broker, bad_report)
    received(broker, POSITION)
    with pytest.raises(ValueError, match="invalid tracker report at 0 s"):
        waiting()


def test_first_match_is_not_masked_by_a_later_malformed_report() -> None:
    broker = Broker(lambda: 0)
    reports = Reports(broker)
    waiting = reports.when_received()
    received(broker, POSITION)
    received(broker, {})
    assert waiting() is not None
    with pytest.raises(ValueError, match="invalid tracker report at 1 s"):
        _ = reports.history


@pytest.mark.app_path("..")
@pytest.mark.board_profile("lilygo-t-a7670g-r2")
@pytest.mark.parametrize(
    ("with_position", "description"),
    [(False, "a tracker report"), (True, "a tracker report with a position")],
)
def test_report_timeout_names_the_expected_report(
    board: Board, with_position: bool, description: str
) -> None:
    # No NVS configuration or listener: exercise a real timeout without publishing.
    network = board.modem.network()
    reports = Reports(network.broker)
    board.power_on()
    with pytest.raises(NeverHappened, match=f"{description} did not happen within"):
        board.wait_until(reports.when_received(with_position=with_position), within=seconds(0.1))
    assert f"waits up to 0.1 s for {description}" in board.report(run=True)


@pytest.mark.parametrize("with_position", [False, True])
def test_report_conditions_reject_truth_testing_even_after_a_match(with_position: bool) -> None:
    broker = Broker(lambda: 0)
    condition = Reports(broker).when_received(with_position=with_position)
    assert condition() is None
    with pytest.raises(TypeError, match="call the report condition"):
        bool(condition)
    received(broker, POSITION)
    assert condition() is not None
    with pytest.raises(TypeError, match="call the report condition"):
        bool(condition)


@pytest.mark.app_path("..")
@pytest.mark.board_profile("lilygo-t-a7670g-r2")
@pytest.mark.parametrize("completed", [False, True])
def test_tls_timeout_names_the_expected_handshake(board: Board, completed: bool) -> None:
    _, tls = serve_broker(board)
    condition = tls.handshake_completed if completed else tls.handshake_started
    expected = "a TLS handshake to complete" if completed else "a TLS handshake to start"
    # Broker setup leaves the board off and NVS unprovisioned, so no handshake can arrive.
    assert not board.powered
    board.power_on()
    with pytest.raises(NeverHappened, match=f"{expected} did not happen within"):
        board.wait_until(condition, within=seconds(0.1))
    assert f"waits up to 0.1 s for {expected}" in board.report(run=True)
