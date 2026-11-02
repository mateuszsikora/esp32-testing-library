"""The public board API shared by simulator and hardware backends."""

from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime
from functools import cached_property
from pathlib import Path
from typing import TypeVar, cast

from esp32tl import flash
from esp32tl.boards import BoardProfile
from esp32tl.interfaces.console import Clocked, Console
from esp32tl.interfaces.gnss import Gnss
from esp32tl.interfaces.lines import Lines
from esp32tl.interfaces.modem import Modem
from esp32tl.interfaces.sd_card import SdCard
from esp32tl.interfaces.uart import Uart
from esp32tl.tap import Tap
from esp32tl.units import Duration

T = TypeVar("T")


class Board(ABC):
    """One device and its interfaces. Obtaining an interface does not power it on.

    The pytest fixture supplies a backend implementing this API. Interface
    fixtures return these same objects, including the console's matching cursor.
    """

    name: str
    clock: str
    profile: BoardProfile

    @cached_property
    def console(self) -> Console:
        # Each backend exposes its own console buffers through the Clocked protocol.
        return Console(cast(Clocked, self))

    @property
    def gnss(self) -> Gnss:
        uart = self.uart("gnss")
        assert isinstance(uart, Gnss), uart
        return uart

    @property
    def modem(self) -> Modem:
        uart = self.uart("modem")
        assert isinstance(uart, Modem), uart
        return uart

    @property
    def lines(self) -> Lines:
        return self._get_lines()

    @property
    def sd_card(self) -> SdCard:
        return self._get_sd_card()

    @abstractmethod
    def uart(self, role: str) -> Uart: ...

    @abstractmethod
    def _get_lines(self) -> Lines: ...

    @abstractmethod
    def _get_sd_card(self) -> SdCard: ...

    @abstractmethod
    def power_on(self, utc: datetime | None = None) -> None: ...

    @property
    @abstractmethod
    def powered(self) -> bool: ...

    @property
    @abstractmethod
    def now(self) -> Duration: ...

    @property
    @abstractmethod
    def utc(self) -> datetime: ...

    @abstractmethod
    def wait(self, duration: Duration) -> None: ...

    @abstractmethod
    def wait_until(self, condition: Callable[[], T | None], *, within: Duration) -> T: ...

    @abstractmethod
    def wait_on_clock(
        self, condition: Callable[[], T | None], within: Duration, description: str
    ) -> T: ...

    @abstractmethod
    def nvs(self, namespaces: flash.Namespaces, partition: str = "nvs") -> None: ...

    @abstractmethod
    def tap_console(self, path: str | Path) -> Tap: ...

    @abstractmethod
    def report(self, run: bool = False) -> str: ...

    @abstractmethod
    def properties(self) -> dict[str, object]: ...

    @abstractmethod
    def close(self) -> None: ...
