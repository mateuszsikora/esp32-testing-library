"""The tracker's NVS schema and MQTT report format; no clocks or firmware startup here."""

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.flash import u16
from esp32tl.net.mqtt import Broker, Message
from esp32tl.units import Duration, Position

TOPIC = "tracker/position"
BROKER_HOST = "broker.test"
BROKER_PORT = 8883


def configure_tracker(
    board: Esp32Sim,
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

    def next(self) -> Callable[[], Report | None]:
        """The first report published after this condition is created."""
        return self._condition(require_position=False)

    def with_position(self) -> Callable[[], Report | None]:
        """The first future report with a fix, skipping valid no-fix reports."""
        return self._condition(require_position=True)

    def _condition(self, *, require_position: bool) -> Callable[[], Report | None]:
        start = len(self._broker.messages)

        def find() -> Report | None:
            return next(
                (r for r in self._since(start) if not require_position or r.position is not None),
                None,
            )

        find.description = (  # type: ignore[attr-defined]
            "a tracker report with a position" if require_position else "a tracker report"
        )
        return find
