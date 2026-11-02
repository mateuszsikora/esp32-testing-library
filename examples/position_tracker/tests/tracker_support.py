"""Tracker broker setup, NVS schema and report format; no waits or firmware startup."""

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

from esp32tl import Board
from esp32tl.flash import u16
from esp32tl.net import Internet
from esp32tl.net.mqtt import Broker, Message
from esp32tl.net.tls import CertificateAuthority, Tls
from esp32tl.units import Duration, Position

TOPIC = "tracker/position"
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

    def _since(self, start: int) -> Iterator[Report]:
        for message in self._broker.messages[start:]:
            if message.topic == TOPIC and message.client != "(test)":
                yield Report.decode(message)

    @property
    def history(self) -> list[Report]:
        return list(self._since(0))

    def when_received(self, *, with_position: bool = False) -> Callable[[], Report | None]:
        """Create a condition for the first future report, optionally requiring a position.

        Creating or polling it never consumes a report. Repeated polls retain the first
        match; create a new condition to observe publications after that point.
        """
        return _ReportCondition(self, len(self._broker.messages), with_position)


@dataclass(frozen=True, repr=False)
class _ReportCondition:
    reports: Reports
    start: int
    with_position: bool

    def __call__(self) -> Report | None:
        return next(
            (
                report
                for report in self.reports._since(self.start)
                if not self.with_position or report.position is not None
            ),
            None,
        )

    def __repr__(self) -> str:
        return "a tracker report with a position" if self.with_position else "a tracker report"

    def __bool__(self) -> bool:
        raise TypeError("call the report condition with (), or pass it to board.wait_until")
