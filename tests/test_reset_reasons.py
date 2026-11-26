"""firmware/reset_reasons through a power-on, `esp_restart()`, a panic, a task watchdog's panic
and a wake from deep sleep: each boot's `Boot.idf_reason` is what the firmware's
`esp_reset_reason()` printed, also for the three software resets the ROM cannot tell apart; then
through each warm `board.reset(kind=...)`; and, without firmware, how `idf_reset_reason` names
each chip's causes and each kind's. Needs `make firmware` (reset_reasons) for the first two
tests."""

import pytest

from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.interfaces.board import (
    DEEPSLEEP_RESET,
    POWERON_RESET,
    RESET_KINDS,
    ResetKind,
    idf_reset_reason,
    warm_reset,
)
from esp32tl.interfaces.console import Console

pytestmark = pytest.mark.app_path("../firmware/reset_reasons")

CHIPS = ["esp32", "esp32s3", "esp32c3", "esp32c6", "esp32p4", "esp32s31"]


def hint(reason: int) -> int:
    """`esp_reset_reason_set_hint(reason)`'s value of the register (reset_reason.c)."""
    return reason | reason << 16 | 0x8000_0000


# Each warm kind of board.reset(): the ROM's cause, the hint and esp_reset_reason(), as ESP-IDF's
# panic handler, its brownout handler and the bootloader's RTC watchdog leave them.
WARM = {
    "panic": (12, hint(4), "ESP_RST_PANIC"),
    "task_wdt": (12, hint(6), "ESP_RST_TASK_WDT"),
    "int_wdt": (12, hint(5), "ESP_RST_INT_WDT"),
    "rtc_wdt": (16, 0, "ESP_RST_WDT"),
    "brownout": (3, hint(9), "ESP_RST_BROWNOUT"),
}

BOOT = rb"reset_reasons: reason=(\w+) rom=(\d+)"
# The ROM's reason after esp_restart() and after a panic alike (RESET_REASON in esp32/rom/rtc.h).
SW_CPU_RESET = 12
REASONS = [
    "ESP_RST_POWERON",
    "ESP_RST_SW",
    "ESP_RST_PANIC",
    "ESP_RST_TASK_WDT",
    "ESP_RST_DEEPSLEEP",
]


# The kinds in an order where each boot ends where the firmware waits, done or in a deep sleep,
# before the next reset: a panic's boot panics again as the task watchdog, whose boot sleeps.
WARM_ORDER: list[ResetKind] = ["brownout", "rtc_wdt", "int_wdt", "panic", "task_wdt"]


def check_warm_resets(board: Esp32Sim, console: Console, settled: bytes) -> None:
    """From a boot that waits, each warm kind of `board.reset`: the boot after it prints the
    kind's `esp_reset_reason()` and the ROM's cause, which its `Boot` has too, and the firmware
    goes on to the next boot that waits, which prints `settled`."""
    for kind in WARM_ORDER:
        cause, _, reason = WARM[kind]
        before = len(board.boots)
        board.reset(kind=kind)
        printed = console.expect(BOOT, timeout=2).groups()
        assert (printed[0].decode(), int(printed[1])) == (reason, cause), kind
        # the boot the reset began; a panic's is followed by the one its own panic begins
        reset = board.boots[before]
        assert (reset.cause, reset.idf_reason) == (cause, reason), kind
        console.expect(settled, timeout=2)
    log = board.report(run=True)
    assert "board.reset(kind='panic')" in log
    assert "SW_CPU_RESET, ESP_RST_PANIC (reset cause 12)" in log


def printed_reasons(console: Console, count: int, timeout: float) -> list[tuple[str, int]]:
    """The reason and the ROM's cause the image printed at each of its next `count` boots, each
    within `timeout` seconds of the board's time of the one before."""
    printed = []
    for _ in range(count):
        reason, rom = console.expect(BOOT, timeout=timeout).groups()
        printed.append((reason.decode(), int(rom)))
    return printed


def test_each_boot_is_named_as_the_firmware_s_esp_reset_reason(
    board: Esp32Sim, console: Console
) -> None:
    board.power_on()
    printed = printed_reasons(console, len(REASONS), timeout=1)
    # before the end: a hint the chip lost makes every software reset ESP_RST_SW, and the image
    # then panics again and again, never done
    assert [reason for reason, _ in printed] == REASONS
    assert [(boot.idf_reason, boot.cause) for boot in board.boots] == printed
    console.expect_exact("reset_reasons: done", timeout=1)
    assert [boot.cause for boot in board.boots] == [
        POWERON_RESET,
        SW_CPU_RESET,
        SW_CPU_RESET,
        SW_CPU_RESET,
        DEEPSLEEP_RESET,
    ]


def test_each_warm_reset_boots_with_the_reason_its_event_leaves(
    board: Esp32Sim, console: Console
) -> None:
    board.power_on()
    console.expect_exact("reset_reasons: done", timeout=2)
    check_warm_resets(board, console, rb"reset_reasons: done")


@pytest.mark.parametrize("chip", CHIPS)
@pytest.mark.parametrize("cause", [3, 12])
def test_a_software_reset_takes_the_hint_the_boot_before_left(chip: str, cause: int) -> None:
    # ESP_RST_PANIC, ESP_RST_INT_WDT, ESP_RST_TASK_WDT and ESP_RST_BROWNOUT (esp_system.h)
    for reason, name in [
        (4, "ESP_RST_PANIC"),
        (5, "ESP_RST_INT_WDT"),
        (6, "ESP_RST_TASK_WDT"),
        (9, "ESP_RST_BROWNOUT"),
    ]:
        assert idf_reset_reason(chip, cause, hint(reason)) == name
    # no hint, a hint IDF does not take for a reset's reason, and values that are no hint: the top
    # bit clear, as a wake stub's address, or the halves apart
    for value in [0, hint(1), hint(7), hint(4) & 0x7FFF_FFFF, 0x4008_0004, 0x8005_0004]:
        assert idf_reset_reason(chip, cause, value) == "ESP_RST_SW", hex(value)


@pytest.mark.parametrize("chip", CHIPS)
def test_any_other_cause_ignores_the_hint(chip: str) -> None:
    assert idf_reset_reason(chip, POWERON_RESET, hint(4)) == "ESP_RST_POWERON"
    assert idf_reset_reason(chip, DEEPSLEEP_RESET, 0x400D_1234) == "ESP_RST_DEEPSLEEP"
    assert idf_reset_reason(chip, 15, 0) == "ESP_RST_BROWNOUT"
    # a cause no chip has
    assert idf_reset_reason(chip, 63, hint(4)) == "ESP_RST_UNKNOWN"


@pytest.mark.parametrize(
    ("chip", "cause", "reason"),
    [
        ("esp32", 6, "ESP_RST_SDIO"),
        ("esp32", 7, "ESP_RST_TASK_WDT"),
        ("esp32", 8, "ESP_RST_INT_WDT"),
        ("esp32", 9, "ESP_RST_WDT"),
        ("esp32", 11, "ESP_RST_WDT"),
        ("esp32", 13, "ESP_RST_WDT"),
        ("esp32", 16, "ESP_RST_WDT"),
        ("esp32", 4, "ESP_RST_UNKNOWN"),
        ("esp32", 10, "ESP_RST_UNKNOWN"),
        ("esp32", 14, "ESP_RST_UNKNOWN"),
        ("esp32s3", 6, "ESP_RST_UNKNOWN"),
        ("esp32s3", 17, "ESP_RST_WDT"),
        ("esp32s3", 18, "ESP_RST_WDT"),
        ("esp32s3", 20, "ESP_RST_UNKNOWN"),
        ("esp32s3", 21, "ESP_RST_USB"),
        ("esp32s3", 22, "ESP_RST_USB"),
        ("esp32c3", 20, "ESP_RST_POWERON"),
        ("esp32c6", 6, "ESP_RST_SDIO"),
        ("esp32c6", 20, "ESP_RST_EFUSE"),
        ("esp32c6", 24, "ESP_RST_JTAG"),
        ("esp32p4", 7, "ESP_RST_WDT"),
        ("esp32p4", 8, "ESP_RST_UNKNOWN"),
        ("esp32p4", 19, "ESP_RST_PWR_GLITCH"),
        ("esp32p4", 20, "ESP_RST_EFUSE"),
        ("esp32p4", 23, "ESP_RST_USB"),
        ("esp32p4", 26, "ESP_RST_CPU_LOCKUP"),
        ("esp32s31", 6, "ESP_RST_DEEPSLEEP"),
        ("esp32s31", 7, "ESP_RST_TASK_WDT"),
        ("esp32s31", 8, "ESP_RST_INT_WDT"),
        ("esp32s31", 17, "ESP_RST_UNKNOWN"),
    ],
)
def test_each_chip_names_its_own_causes(chip: str, cause: int, reason: str) -> None:
    assert idf_reset_reason(chip, cause, 0) == reason


@pytest.mark.parametrize("chip", CHIPS)
def test_each_warm_reset_reads_as_its_idf_reason_on_every_chip(chip: str) -> None:
    for kind, (cause, value, reason) in WARM.items():
        warm = warm_reset(kind)
        assert warm is not None, kind
        assert (warm.cause, warm.hint, warm.idf_reason) == (cause, value, reason), kind
        assert idf_reset_reason(chip, warm.cause, warm.hint) == warm.idf_reason, kind


def test_en_is_a_power_on_reset_and_any_other_kind_is_refused() -> None:
    assert ("en", *WARM) == RESET_KINDS
    assert warm_reset("en") is None
    with pytest.raises(ValueError) as refused:
        warm_reset("sw")
    assert str(refused.value) == (
        "board.reset: kind is 'en', 'panic', 'task_wdt', 'int_wdt', 'rtc_wdt' or 'brownout',"
        " not 'sw'"
    )


def test_a_chip_without_a_table_has_no_reason() -> None:
    assert idf_reset_reason("esp32h2", POWERON_RESET, 0) is None
