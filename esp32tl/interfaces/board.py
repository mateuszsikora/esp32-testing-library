"""The public board API shared by simulator and hardware backends."""

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property
from pathlib import Path
from typing import TypeVar, cast

from esp32tl.boards import BoardProfile
from esp32tl.flash import FlashSnapshot, Namespaces
from esp32tl.interfaces.bluetooth import Bluetooth
from esp32tl.interfaces.condition import Condition
from esp32tl.interfaces.console import Clocked, Console
from esp32tl.interfaces.gnss import Gnss
from esp32tl.interfaces.lines import Lines
from esp32tl.interfaces.modem import Modem
from esp32tl.interfaces.sd_card import SdCard
from esp32tl.interfaces.uart import Uart
from esp32tl.interfaces.wifi import Wifi
from esp32tl.tap import Tap
from esp32tl.units import Duration

T = TypeVar("T")

# The classic chip's reset causes, as its ROM reports them (`RESET_REASON` in ESP-IDF's
# esp_rom/esp32/include/esp32/rom/rtc.h) and prints them in its banner, `rst:0x1 (POWERON_RESET)`.
RESET_REASONS = {
    1: "POWERON_RESET",
    3: "SW_RESET",
    4: "OWDT_RESET",
    5: "DEEPSLEEP_RESET",
    6: "SDIO_RESET",
    7: "TG0WDT_SYS_RESET",
    8: "TG1WDT_SYS_RESET",
    9: "RTCWDT_SYS_RESET",
    10: "INTRUSION_RESET",
    11: "TGWDT_CPU_RESET",
    12: "SW_CPU_RESET",
    13: "RTCWDT_CPU_RESET",
    14: "EXT_CPU_RESET",
    15: "RTCWDT_BROWN_OUT_RESET",
    16: "RTCWDT_RTC_RESET",
}

# The cause of a power-on reset: the run's first boot, board.reset(), and power coming back.
POWERON_RESET = 1
# The cause of a wake from deep sleep, whatever woke the chip.
DEEPSLEEP_RESET = 5


@dataclass(frozen=True)
class Boot:
    """One boot of the chip: its `number`, 1 for the first, the board's time `at` which it
    started, and the reset `cause` the ROM reports for it, which `reason` names."""

    number: int
    at: Duration
    cause: int

    @property
    def reason(self) -> str:
        return reset_reason(self.cause)


def reset_reason(cause: int) -> str:
    """The ROM's name for reset `cause`, as `POWERON_RESET`; `reset cause {cause}` for one
    `RESET_REASONS` does not name."""
    return RESET_REASONS.get(cause, f"reset cause {cause}")


class Board(ABC):
    """One device and its interfaces. Obtaining an interface does not power it on.

    The pytest fixture supplies a backend implementing this API. Interface
    fixtures return these same objects, including the console's matching cursor.
    """

    name: str
    profile: BoardProfile

    @property
    @abstractmethod
    def clock(self) -> str:
        """The clock the run's times are on, as the recording format names it."""

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

    @property
    def wifi(self) -> Wifi:
        return self._get_wifi()

    @property
    def bluetooth(self) -> Bluetooth:
        return self._get_bluetooth()

    @abstractmethod
    def uart(self, role: str) -> Uart: ...

    @abstractmethod
    def _get_lines(self) -> Lines: ...

    @abstractmethod
    def _get_sd_card(self) -> SdCard: ...

    @abstractmethod
    def _get_wifi(self) -> Wifi: ...

    @abstractmethod
    def _get_bluetooth(self) -> Bluetooth: ...

    @abstractmethod
    def power_on(self, utc: datetime | None = None) -> None: ...

    @abstractmethod
    def power_off(self) -> None: ...

    @abstractmethod
    def reset(self) -> None: ...

    @property
    @abstractmethod
    def app_sha256(self) -> str | None: ...

    @property
    @abstractmethod
    def powered(self) -> bool: ...

    @property
    @abstractmethod
    def started(self) -> bool: ...

    @property
    @abstractmethod
    def boots(self) -> tuple[Boot, ...]: ...

    @property
    def boot(self) -> Boot:
        """The chip's current boot: the last of `boots`."""
        return self.boots[-1]

    def next_boot(self, cause: int | None = None) -> Condition[Boot | None]:
        """A described condition for the first boot after this call, through any reset, or
        through a reset of `cause` only, as `POWERON_RESET`: a boot of another cause does not
        satisfy it, and a later one of `cause` still can. The boundary is the boots there are now,
        `boots` at this call; the condition consumes nothing, and returns the same boot each time
        it is asked. Arm it before `reset()`, `power_on()` or what makes the firmware restart."""
        if cause is not None and (isinstance(cause, bool) or not isinstance(cause, int)):
            raise TypeError(
                f"board.next_boot: cause is {cause!r}, not a reset cause as a number, such as"
                " POWERON_RESET"
            )
        start = len(self.boots)

        def find() -> Boot | None:
            return next(
                (boot for boot in self.boots[start:] if cause is None or boot.cause == cause),
                None,
            )

        description = "the next boot" if cause is None else f"the next boot ({reset_reason(cause)})"
        return Condition(find, description)

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
    def wait_for_wake(self, *, within: Duration) -> Boot: ...

    @abstractmethod
    def wait_on_clock(
        self, condition: Callable[[], T | None], within: Duration, description: str
    ) -> T: ...

    @abstractmethod
    def nvs(self, namespaces: Namespaces, partition: str = "nvs") -> None: ...

    @abstractmethod
    def flash(self) -> FlashSnapshot: ...

    @abstractmethod
    def tap_console(self, path: str | Path) -> Tap: ...

    @abstractmethod
    def report(self, run: bool = False) -> str: ...

    @abstractmethod
    def properties(self) -> dict[str, object]: ...

    @abstractmethod
    def close(self) -> None: ...
