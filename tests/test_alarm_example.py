"""The example's boot assertion must reject glitches that have already recovered."""

from collections.abc import Callable
from importlib import import_module

import pytest

from esp32tl import boards
from esp32tl.interfaces.lines import Lines, OutputLine
from esp32tl.units import seconds

# The example is checked as a standalone module by mypy, not as an installed package.
assert_never_buzzed: Callable[[OutputLine], None] = import_module(
    "examples.gps_alarm_clock.tests.test_alarm"
).assert_never_buzzed


def alarm_lines() -> Lines:
    profile = boards.profile("gps-alarm-clock")
    return Lines(profile.name, profile.lines, lambda: seconds(5).ns)


def test_boot_assertion_allows_unknown_before_initialization_and_ignores_inputs() -> None:
    lines = alarm_lines()
    lines.drive("alarm.buzzer", 1)
    lines.driven(seconds(2).ns, [(18, 0)], frozenset({18}))
    assert_never_buzzed(lines.output("alarm.buzzer"))


@pytest.mark.parametrize(
    "levels,violation",
    [
        ([1, 0], "first violation at 1 s: level 1"),
        ([0, 1, 0], "first violation at 2 s: level 1"),
        ([0, None, 0], "first violation at 2 s: undriven/unknown"),
    ],
)
def test_boot_assertion_reports_the_first_violation_even_after_recovery(
    levels: list[int | None], violation: str
) -> None:
    lines = alarm_lines()
    for instant, level in enumerate(levels, start=1):
        lines.driven(
            seconds(instant).ns,
            [(18, level)] if level is not None else [(18, 0)],
            frozenset({18}) if level is not None else frozenset(),
        )
    with pytest.raises(AssertionError) as failure:
        assert_never_buzzed(lines.output("alarm.buzzer"))
    assert str(failure.value) == (
        "alarm.buzzer expected off after initialization (GPIO 18, level 0); " + violation
    )


def test_boot_assertion_catches_a_pulse_at_the_initialization_timestamp() -> None:
    lines = alarm_lines()
    for level in (1, 0):
        lines.driven(seconds(1).ns, [(18, level)], frozenset({18}))
    with pytest.raises(AssertionError, match="first violation at 1 s: level 1"):
        assert_never_buzzed(lines.output("alarm.buzzer"))


def test_boot_assertion_requires_the_firmware_to_initialize_the_output() -> None:
    lines = alarm_lines()
    with pytest.raises(AssertionError, match="observed undriven/unknown"):
        assert_never_buzzed(lines.output("alarm.buzzer"))
