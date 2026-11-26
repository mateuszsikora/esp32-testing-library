"""The public board API shared by simulator and hardware backends."""

from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from functools import cached_property
from pathlib import Path
from typing import Literal, TypeVar, cast, get_args

from esp32tl.boards import BoardProfile
from esp32tl.flash import FlashSnapshot, Namespaces
from esp32tl.interfaces.bluetooth import Bluetooth
from esp32tl.interfaces.condition import Condition, FirstFrom
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

# What ESP-IDF's esp_reset_reason() makes of each chip's reset causes, by ESP-IDF target name:
# get_reset_reason() of components/esp_system/port/soc/<chip>/reset_reason.c, the same in v5.3.6,
# v5.5.4 and v6.1, with the causes of components/soc/<chip>/include/soc/reset_reasons.h. A cause a
# table leaves out is ESP_RST_UNKNOWN, as the function's default. _SOFTWARE_RESET marks the causes
# that read the hint the boot before left (`idf_reset_reason`).
_SOFTWARE_RESET = "software reset"
_COMMON = {
    1: "ESP_RST_POWERON",
    3: _SOFTWARE_RESET,
    12: _SOFTWARE_RESET,
    5: "ESP_RST_DEEPSLEEP",
    15: "ESP_RST_BROWNOUT",
}
# The S3, the C3 and the C6: the classic chip's causes but SDIO's, two more watchdogs, the
# timer-group watchdog's CPU reset (17) and the super watchdog (18), and USB's.
_S3_C3_C6 = {
    **_COMMON,
    7: "ESP_RST_TASK_WDT",
    8: "ESP_RST_INT_WDT",
    **dict.fromkeys((9, 11, 13, 16, 17, 18), "ESP_RST_WDT"),
    21: "ESP_RST_USB",
    22: "ESP_RST_USB",
}
# The P4 and the S31: the LP watchdogs, CPU lock-up and power glitches, USB at 0x16 and 0x17.
_P4_S31 = {
    **_COMMON,
    **dict.fromkeys((9, 11, 13, 16, 18), "ESP_RST_WDT"),
    19: "ESP_RST_PWR_GLITCH",
    20: "ESP_RST_EFUSE",
    22: "ESP_RST_USB",
    23: "ESP_RST_USB",
    24: "ESP_RST_JTAG",
    26: "ESP_RST_CPU_LOCKUP",
}
_IDF_RESET_REASONS: dict[str, dict[int, str]] = {
    "esp32": {
        **_COMMON,
        6: "ESP_RST_SDIO",
        7: "ESP_RST_TASK_WDT",
        8: "ESP_RST_INT_WDT",
        **dict.fromkeys((9, 11, 13, 16), "ESP_RST_WDT"),
    },
    "esp32s3": _S3_C3_C6,
    # SOC_EFUSE_HAS_EFUSE_RST_BUG: the C3's eFuse CRC reset is a power-on's.
    "esp32c3": {**_S3_C3_C6, 20: "ESP_RST_POWERON"},
    "esp32c6": {**_S3_C3_C6, 6: "ESP_RST_SDIO", 20: "ESP_RST_EFUSE", 24: "ESP_RST_JTAG"},
    # Every watchdog is ESP_RST_WDT, a task's and an interrupt's too ("Code is the same for INT vs
    # Task WDT"). The eFuse CRC reset is ESP_RST_EFUSE from chip revision v0.1 on
    # (ESP_CHIP_REV_ABOVE(rev, 1) is 1 <= rev), every revision esp32sim models; IDF says
    # ESP_RST_DEEPSLEEP for it on v0.0.
    "esp32p4": {**_P4_S31, 7: "ESP_RST_WDT"},
    # Cause 6 is the CPU's power-down, a wake from deep sleep as 5 is; 7 and 8, the core resets of
    # the two timer groups' watchdogs, are the task's and the interrupt's, as on the classic chip.
    "esp32s31": {
        **_P4_S31,
        6: "ESP_RST_DEEPSLEEP",
        7: "ESP_RST_TASK_WDT",
        8: "ESP_RST_INT_WDT",
    },
}
# The hints a software reset takes as its reason; any other is ESP_RST_SW (get_reset_reason).
_HINTED = {4: "ESP_RST_PANIC", 5: "ESP_RST_INT_WDT", 6: "ESP_RST_TASK_WDT", 9: "ESP_RST_BROWNOUT"}


# The kinds of reset `board.reset(kind=...)` gives. "en" pulses the chip's EN pin, a power-on
# reset; each other is a warm reset, which keeps SRAM, the RTC memories and the RTC registers, as
# ESP-IDF's own path through the event it names leaves it.
ResetKind = Literal["en", "panic", "task_wdt", "int_wdt", "rtc_wdt", "brownout"]
RESET_KINDS: tuple[ResetKind, ...] = get_args(ResetKind)


def reset_hint(reason: int) -> int:
    """The value of the reset-reason hint register for `esp_reset_reason_t` `reason`, as
    `esp_reset_reason_set_hint` writes it (`components/esp_system/port/soc/<chip>/
    reset_reason.c`)."""
    return reason | reason << 16 | 0x8000_0000


@dataclass(frozen=True)
class WarmReset:
    """A warm reset of one kind: the `cause` the ROM reports, the `hint` the register holds, and
    what `esp_reset_reason()` makes of the two, `idf_reason`."""

    cause: int
    hint: int
    idf_reason: str


# What ESP-IDF v5.3.6 to v6.1 leaves before each warm reset, the same on every chip of
# _IDF_RESET_REASONS. A panic sets the hint ESP_RST_PANIC, a task watchdog's ESP_RST_TASK_WDT and
# an interrupt watchdog's ESP_RST_INT_WDT (components/esp_system/panic.c, esp_panic_handler) and
# resets the CPU, 12, RESET_REASON_CPU0_SW (port/soc/<chip>/system_internal.c, esp_restart_noos);
# the brownout handler, with CONFIG_ESP_BROWNOUT_DET, sets ESP_RST_BROWNOUT and resets the system,
# 3, RESET_REASON_CORE_SW (esp_rom_software_reset_system in brownout.c). The bootloader's RTC
# watchdog resets the RTC too, 16, RESET_REASON_SYS_RTC_WDT (WDT_STAGE_ACTION_RESET_RTC in
# bootloader_init.c), and with it the hint register. Causes of
# components/soc/<chip>/include/soc/reset_reasons.h.
_WARM_RESETS: dict[str, WarmReset] = {
    "panic": WarmReset(12, reset_hint(4), "ESP_RST_PANIC"),
    "task_wdt": WarmReset(12, reset_hint(6), "ESP_RST_TASK_WDT"),
    "int_wdt": WarmReset(12, reset_hint(5), "ESP_RST_INT_WDT"),
    "rtc_wdt": WarmReset(16, 0, "ESP_RST_WDT"),
    "brownout": WarmReset(3, reset_hint(9), "ESP_RST_BROWNOUT"),
}


def warm_reset(kind: str) -> WarmReset | None:
    """The cause and hint of a reset of `kind`, one of `RESET_KINDS`: `None` for "en", the EN
    pulse, which is a power-on reset; `ValueError` for any other string."""
    if kind not in RESET_KINDS:
        kinds = ", ".join(repr(k) for k in RESET_KINDS[:-1])
        raise ValueError(f"board.reset: kind is {kinds} or {RESET_KINDS[-1]!r}, not {kind!r}")
    return _WARM_RESETS.get(kind)


def idf_reset_reason(chip: str, cause: int, hint: int) -> str | None:
    """What `esp_reset_reason()` reports on `chip`, an ESP-IDF target name, after a reset of
    `cause`, the ROM's reset reason, with `hint` in the register ESP-IDF keeps its reset-reason
    hint in (`RTC_RESET_CAUSE_REG`, `RTC_CNTL_STORE6` on the classic ESP32) as the reset found it.
    `None` for a chip esp32tl has no table for.

    The hint is `h | h << 16 | 0x80000000` for an `esp_reset_reason_t` `h`, as
    `esp_reset_reason_set_hint` writes it; a value with the top bit clear or the halves apart is
    none (`esp_reset_reason_get_hint`): the register also holds a deep-sleep wake stub's address."""
    table = _IDF_RESET_REASONS.get(chip)
    if table is None:
        return None
    reason = table.get(cause, "ESP_RST_UNKNOWN")
    if reason != _SOFTWARE_RESET:
        return reason
    low, high = hint & 0x7FFF, hint >> 16 & 0x7FFF
    if hint & 0x8000_0000 and low == high:
        return _HINTED.get(low, "ESP_RST_SW")
    return "ESP_RST_SW"


@dataclass(frozen=True)
class UptimeZero:
    """Where a boot's uptime counter, the one `esp_timer_get_time()` reads, counted zero on the
    board's clock: no sooner than `earliest`, no later than `latest`. A stamp of `uptime` the
    firmware took from it was taken at a board time between `earliest + uptime` and
    `latest + uptime`."""

    earliest: Duration
    latest: Duration


@dataclass(frozen=True)
class Boot:
    """One boot of the chip: its `number`, 1 for the first, the board's time `at` which it
    started, and the reset `cause` the ROM reports for it, which `reason` names. `uptime_zero`
    is where its uptime counter counted zero as the backend knew it when this record was read:
    `None` before the backend has seen the counter count, and it stays so in a record read then,
    which is frozen; `None` too for a counter that counts from a zero that may lie before
    power-on. A counter that stops, as in deep sleep, keeps the zero it had.
    `board.uptime_zero(boot)` reads it now, and `board.next_boot(uptime_zero=True)` returns the
    boot only once it is known. `idf_reason` is what ESP-IDF's `esp_reset_reason()` reports for
    the boot, as `ESP_RST_PANIC` (`idf_reset_reason`); `None` where the backend cannot tell.
    Neither takes part in comparing two boots."""

    number: int
    at: Duration
    cause: int
    uptime_zero: UptimeZero | None = field(default=None, compare=False)
    idf_reason: str | None = field(default=None, compare=False)

    @property
    def reason(self) -> str:
        return reset_reason(self.cause)


@dataclass(frozen=True)
class Sleep:
    """One deep sleep of the chip: the number of the `boot` that went into it, as in
    `board.boots`, and the board's time it began at, no sooner than `earliest` and no later than
    `latest`. `wakes` is what the firmware armed to wake it, by ESP-IDF's names for the sources a
    deep sleep wakes from: `"ext0"`, `"ext1"`, `"timer"`, `"touch"` and `"ulp"`. The wake is the
    next boot, a `DEEPSLEEP_RESET`: for a timer armed for `d`, `next.at - latest` is about `d`."""

    boot: int
    earliest: Duration
    latest: Duration
    wakes: frozenset[str]


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
    def reset(self, kind: ResetKind = "en") -> None: ...

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
    def boots(self) -> Sequence[Boot]: ...

    @property
    def boot(self) -> Boot:
        """The chip's current boot: the last of `boots`."""
        return self.boots[-1]

    def next_boot(
        self, cause: int | None = None, *, uptime_zero: bool = False
    ) -> Condition[Boot | None]:
        """A described condition for the first boot after this call, through any reset, or
        through a reset of `cause` only, as `POWERON_RESET`: a boot of another cause does not
        satisfy it, and a later one of `cause` still can. The boundary is the boots there are now,
        `boots` at this call; the condition consumes nothing, and returns the same boot each time
        it is asked. Arm it before `reset()`, `power_on()` or what makes the firmware restart.

        With `uptime_zero`, it returns that boot only while its `uptime_zero` is known, read again
        at each poll, and `None` before: a wait on it times out for a boot whose zero never comes,
        as on a firmware that never starts the counter, or on the ESP32-S31. It still waits on
        the first boot after the call of `cause`, not on a later one that has a zero."""
        if cause is not None and (isinstance(cause, bool) or not isinstance(cause, int)):
            raise TypeError(
                f"board.next_boot: cause is {cause!r}, not a reset cause as a number, such as"
                " POWERON_RESET"
            )
        if not isinstance(uptime_zero, bool):
            raise TypeError(f"board.next_boot: uptime_zero is {uptime_zero!r}, not True or False")
        first = FirstFrom(
            lambda: self.boots, len(self.boots), lambda boot: cause is None or boot.cause == cause
        )
        description = "the next boot" if cause is None else f"the next boot ({reset_reason(cause)})"
        if not uptime_zero:
            return Condition(first, description)

        # The match is read again at each poll, so the boot's zero, which comes after the boot
        # has begun, is seen once the backend has it; a filter of `FirstFrom` would refuse the
        # boot for good at the poll that saw it without one.
        def counted() -> Boot | None:
            boot = first()
            return boot if boot is not None and boot.uptime_zero is not None else None

        return Condition(counted, f"{description}, with its uptime zero")

    def uptime_zero(self, boot: Boot) -> UptimeZero | None:
        """Where `boot`'s uptime counter counted zero, as the backend knows it now: the
        `uptime_zero` of the same boot in `boots`, read again, where `boot.uptime_zero` is what was
        known when `boot` was read. `None` while it is not known."""
        if not isinstance(boot, Boot):
            raise TypeError(
                f"board.uptime_zero: boot is {boot!r}, not a Boot from board.boots,"
                " board.next_boot() or board.wait_for_wake()"
            )
        boots = self.boots
        if not 1 <= boot.number <= len(boots) or boots[boot.number - 1] != boot:
            raise ValueError(
                f"board.uptime_zero: boot {boot.number} at {boot.at} (reset cause {boot.cause})"
                f" is not one of this board's {len(boots)} boots"
            )
        return boots[boot.number - 1].uptime_zero

    @property
    @abstractmethod
    def sleeps(self) -> Sequence[Sleep]:
        """The chip's deep sleeps, oldest first: at most one per boot, since the wake is a
        reset."""

    def next_sleep(self) -> Condition[Sleep | None]:
        """A described condition for the first deep sleep after this call. The boundary is the
        sleeps there are now, `sleeps` at this call; the condition consumes nothing, and returns
        the same sleep each time it is asked."""
        return Condition(
            FirstFrom(lambda: self.sleeps, len(self.sleeps), lambda _: True), "the next deep sleep"
        )

    @property
    @abstractmethod
    def now(self) -> Duration: ...

    @property
    @abstractmethod
    def utc(self) -> datetime:
        """The world's time now: `utc_at(now)`."""

    @abstractmethod
    def utc_at(self, at: Duration) -> datetime:
        """The world's time at the board's instant `at`: the UTC instant at the first power-on,
        plus `at`. `at` may be later than `now`, for a model that dates what it sends ahead.
        Raises `BoardOff` before the first power-on; after a `power_off` it still answers, since
        the world's time runs on."""

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
    def nvs(
        self, namespaces: Namespaces, partition: str = "nvs", *, merge: bool = False
    ) -> None: ...

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
