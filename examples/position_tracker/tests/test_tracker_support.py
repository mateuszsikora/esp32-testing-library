"""Report waits must neither reuse history nor hide malformed firmware publications."""

import json
from datetime import UTC, datetime

import pytest
from tracker_support import TOPIC, Reports

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
