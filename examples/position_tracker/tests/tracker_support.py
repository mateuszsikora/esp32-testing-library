"""Tracker broker setup, NVS schema and report format; no waits or firmware startup."""

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

from esp32tl import (
    Board,
    Broker,
    CertificateAuthority,
    Condition,
    Internet,
    Mark,
    Message,
    Tls,
    condition,
    u16,
)
from esp32tl.units import Duration, Position

TOPIC = "tracker/position"
CLIENT_ID = "position-tracker"
BROKER_HOST = "broker.test"
BROKER_PORT = 8883


def serve_broker(board: Board, *, ca: CertificateAuthority | None = None) -> tuple[Internet, Tls]:
    """Select the modem network and serve its broker over TLS, without provisioning NVS."""
    network = board.modem.network()
    credentials = CertificateAuthority([BROKER_HOST]) if ca is None else ca
    tls = network.listen(BROKER_PORT, network.broker, tls=credentials)
    return network, tls


def configure_tracker(
    board: Board,
    *,
    trusted_ca: str,
    broker_host: str = BROKER_HOST,
    broker_port: int = BROKER_PORT,
) -> None:
    """Write only the firmware's NVS configuration, before power-on."""
    board.nvs(
        {
            "config": {
                "broker_host": broker_host,
                "broker_port": u16(broker_port),
                "broker_ca": trusted_ca,
                "ntp_host": "time.test",
                "apn": "internet",
            }
        }
    )


@dataclass(frozen=True)
class Report:
    received_at: Duration
    utc: datetime
    position: Position | None

    @classmethod
    def decode(cls, message: Message) -> "Report":
        """Reject malformed reports instead of silently filtering them out of a wait."""
        try:
            data = json.loads(message.payload)
            if not isinstance(data, dict) or set(data) != {"fix", "lat", "lon", "time"}:
                raise ValueError("expected fix, lat, lon and time fields")
            if type(data["fix"]) is not bool or type(data["time"]) is not int:
                raise ValueError("fix must be boolean and time must be integer Unix seconds")
            position = None
            if data["fix"]:
                if any(type(data[key]) not in (int, float) for key in ("lat", "lon")):
                    raise ValueError("a fix needs numeric latitude and longitude")
                position = Position(data["lat"], data["lon"])
            elif data["lat"] is not None or data["lon"] is not None:
                raise ValueError("a report without a fix must have null coordinates")
            return cls(message.at, datetime.fromtimestamp(data["time"], UTC), position)
        except (ValueError, TypeError, OverflowError, OSError) as error:
            raise ValueError(f"invalid tracker report at {message.at}: {error}") from error


class Reports:
    """Application reports received by the broker, with conditions on future publications."""

    def __init__(self, broker: Broker) -> None:
        self._broker = broker

    def _since(self, mark: Mark | None) -> Iterator[Report]:
        for message in self._broker.find_messages(TOPIC, since=mark, client=CLIENT_ID):
            yield Report.decode(message)

    @property
    def history(self) -> list[Report]:
        return list(self._since(None))

    def when_received(self, *, with_position: bool = False) -> Condition[Report | None]:
        """Create a condition for the first future report, optionally requiring a position.

        Creating or polling it never consumes a report. Repeated polls retain the first
        match; create a new condition to observe publications after that point.
        """
        start = self._broker.mark()

        def find() -> Report | None:
            wanted = (r for r in self._since(start) if not with_position or r.position is not None)
            return next(wanted, None)

        return condition(
            find, "a tracker report with a position" if with_position else "a tracker report"
        )
