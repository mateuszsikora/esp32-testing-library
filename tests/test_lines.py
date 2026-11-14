"""`Lines`, the `lines` fixture's interface, without an emulator: what the test drives goes out
in the link's turn at the board's instant, the board's pulls from power-on and on release, and
what the firmware drives is read from the link's `gpio_out`. test_gpio_echo.py runs it on the
emulator."""

import re

import pytest

from esp32tl import boards
from esp32tl.boards import Line
from esp32tl.interfaces.lines import LineEvent, Lines
from esp32tl.link.boardlink import BoardLink
from esp32tl.link.protocol import GpioIn, GpioOut, Release, RunnerHello, Sync, Wait
from esp32tl.report import RunLog
from esp32tl.units import Duration, seconds

HELLO = RunnerHello(1, "esp32", "emulated-fifo-write", "3bdb9582a910974e4e025d274d6ec61e5c481be6")
LILYGO = boards.profile("lilygo-t-a7670g-r2")
S = 1_000_000_000


class Clock:
    """The board's time, set by the test."""

    def __init__(self) -> None:
        self.now = 0

    def __call__(self) -> int:
        return self.now


def lilygo() -> tuple[Lines, Clock]:
    clock = Clock()
    return Lines(LILYGO.name, LILYGO.lines, clock), clock


def test_the_lines_are_the_profile_s_and_named_by_pin_or_by_role() -> None:
    lines, _ = lilygo()
    assert lines.pins == {4, 5, 12, 19, 23, 25, 33}
    lines.drive("modem.ri", 1)
    lines.drive(4, True)
    assert [(e.name, e.pin, e.level) for e in lines.events()] == [
        ("modem.ri", 33, 1),
        ("modem.pwrkey", 4, 1),
    ]


@pytest.mark.parametrize(
    ("line", "level", "error", "message"),
    [
        (
            18,
            1,
            ValueError,
            "GPIO 18 is no line of the board profile lilygo-t-a7670g-r2; its lines are"
            " modem.pwrkey (GPIO 4), modem.ri (GPIO 33), modem.dtr (GPIO 25),"
            " modem.reset (GPIO 5), gnss.wake (GPIO 19), gnss.pps (GPIO 23),"
            " board.power_enable (GPIO 12)",
        ),
        ("ri", 1, ValueError, "'ri' is no line of the board profile lilygo-t-a7670g-r2"),
        (True, 1, ValueError, "True is no line"),
        ("modem.ri", 2, ValueError, "lines.drive: level 2 is not 0 or 1"),
        ("modem.ri", "1", TypeError, "lines.drive: level '1' is not 0 or 1"),
    ],
)
def test_a_line_or_a_level_that_is_not_one_is_refused(
    line: int | str, level: int, error: type[Exception], message: str
) -> None:
    lines, _ = lilygo()
    with pytest.raises(error, match=f"^{re.escape(message)}"):
        lines.drive(line, level)
    assert lines.events() == []


def test_a_profile_without_lines_has_none_to_drive() -> None:
    devkit = boards.profile("esp32-devkitc")
    lines = Lines(devkit.name, devkit.lines, Clock())
    assert lines.pins == frozenset()
    with pytest.raises(
        ValueError, match=re.escape("'modem.ri': the board profile esp32-devkitc has no lines")
    ):
        lines.release("modem.ri")


def test_the_board_s_pulls_go_out_in_the_first_turn_before_what_the_test_drives() -> None:
    lines, _ = lilygo()
    lines.drive("modem.pwrkey", 1)
    assert list(lines.inputs(0)) == [GpioIn(0, ((33, 0),)), GpioIn(0, ((4, 1),))]
    # Once.
    assert list(lines.inputs(5)) == []


def test_a_drive_and_a_release_go_out_at_the_instant_the_test_made_them() -> None:
    lines, clock = lilygo()
    list(lines.inputs(0))
    clock.now = 2 * S
    lines.drive("modem.ri", 1)
    lines.drive("modem.pwrkey", 1)
    clock.now = 3 * S
    lines.release("modem.pwrkey")
    # The pulled line goes back to its pull; the other is released.
    lines.release("modem.ri")
    # Asked for after the turn it was stamped at: it goes out in the next one, not before it.
    assert list(lines.inputs(2 * S)) == [
        GpioIn(2 * S, ((33, 1),)),
        GpioIn(2 * S, ((4, 1),)),
        Release(3 * S, (4,)),
        GpioIn(3 * S, ((33, 0),)),
    ]
    clock.now = 4 * S
    lines.drive("modem.ri", 1)
    assert list(lines.inputs(5 * S)) == [GpioIn(5 * S, ((33, 1),))]
    assert lines.events() == [
        LineEvent(seconds(2), "modem.ri", 33, "test", 1),
        LineEvent(seconds(2), "modem.pwrkey", 4, "test", 1),
        LineEvent(seconds(3), "modem.pwrkey", 4, "test", None),
        LineEvent(seconds(3), "modem.ri", 33, "test", None),
        LineEvent(seconds(4), "modem.ri", 33, "test", 1),
    ]


def test_what_the_test_already_does_is_not_an_event() -> None:
    lines, _ = lilygo()
    lines.release("modem.ri")
    lines.drive("modem.ri", 0)
    lines.drive("modem.ri", 0)
    assert list(lines.inputs(0)) == [GpioIn(0, ((33, 0),)), GpioIn(0, ((33, 0),))]
    assert [e.level for e in lines.events()] == [0]


def test_the_level_is_what_the_firmware_drives() -> None:
    lines, _ = lilygo()
    assert lines.level("modem.pwrkey") is None
    # The output enabled, low: driven at 0.
    lines.driven(S, [(4, 0)], frozenset({4}))
    assert lines.level("modem.pwrkey") == 0
    lines.driven(2 * S, [(4, 1)], frozenset({4}))
    assert lines.level(4) == 1
    # The output off: the pad is reported low, as every pad that stops being driven.
    lines.driven(3 * S, [(4, 0)], frozenset())
    assert lines.level(4) is None
    lines.driven(4 * S, [(4, 0)], frozenset({4}))
    # An enable-only change of a pad at 0.
    lines.driven(5 * S, [], frozenset())
    assert lines.level(4) is None
    # A pad a peripheral drives high through the matrix, without its bit in GPIO_ENABLE.
    lines.driven(6 * S, [(4, 1)], frozenset())
    assert lines.level(4) == 1
    assert lines.events() == [
        LineEvent(seconds(1), "modem.pwrkey", 4, "firmware", 0),
        LineEvent(seconds(2), "modem.pwrkey", 4, "firmware", 1),
        LineEvent(seconds(3), "modem.pwrkey", 4, "firmware", None),
        LineEvent(seconds(4), "modem.pwrkey", 4, "firmware", 0),
        LineEvent(seconds(5), "modem.pwrkey", 4, "firmware", None),
        LineEvent(seconds(6), "modem.pwrkey", 4, "firmware", 1),
    ]


def test_a_pull_up_drives_its_line_high() -> None:
    lines = Lines("board", {"x": {"irq": Line(12, "up"), "led": Line(13)}}, Clock())
    lines.drive("x.irq", 0)
    lines.release("x.irq")
    lines.drive("x.led", 1)
    lines.release("x.led")
    assert list(lines.inputs(0)) == [
        GpioIn(0, ((12, 1),)),
        GpioIn(0, ((12, 0),)),
        GpioIn(0, ((12, 1),)),
        GpioIn(0, ((13, 1),)),
        Release(0, (13,)),
    ]


def test_on_the_link_the_lines_are_watched_and_driven_in_their_turn() -> None:
    lines, clock = lilygo()
    link = BoardLink(LILYGO.name, [lines])
    assert link.receive(HELLO)[0].watch == (4, 5, 12, 19, 23, 25, 33)  # type: ignore[union-attr]
    assert link.receive(Sync(0)) == [GpioIn(0, ((33, 0),)), Wait(0, None, None)]
    link.receive(GpioOut(1, S, ((4, 1),), (4,), (4,)))
    clock.now = 2 * S
    lines.drive("modem.ri", 1)
    assert link.receive(Sync(2 * S)) == [GpioIn(2 * S, ((33, 1),)), Wait(1, None, None)]
    assert link.error is None
    assert lines.level("modem.pwrkey") == 1
    assert [(e.t, e.by) for e in lines.events()] == [(seconds(1), "firmware"), (seconds(2), "test")]


def test_the_test_s_calls_and_the_firmware_s_changes_go_into_the_run_s_log() -> None:
    log = RunLog()
    clock = Clock()
    lines = Lines(LILYGO.name, LILYGO.lines, clock, log=log)
    clock.now = S
    lines.drive("modem.ri", 1)
    # A call that changes nothing is still a step of the test.
    lines.drive(33, 1)
    lines.driven(S + 9_000, [(4, 1)], frozenset({4}))
    clock.now = 2 * S
    lines.release("modem.ri")
    assert log.lines() == [
        "the run on the board's clock, 4 entries:",
        "  1.000000000 s  test              lines.drive('modem.ri', 1)",
        "  1.000000000 s  test              lines.drive(33, 1)",
        "  1.000009000 s  modem.pwrkey out  1",
        "  2.000000000 s  test              lines.release('modem.ri')",
    ]


@pytest.mark.parametrize("active", [0, 1])
def test_named_output_conditions_follow_the_firmware_and_exclude_undriven(active: int) -> None:
    lines, _ = lilygo()
    output = lines.output("modem.pwrkey", active=active)
    on, off = output.is_on, output.is_off
    assert not on() and not off()  # Unknown is not off, including for an active-low output.
    lines.drive(4, active)
    assert not on() and not off()  # Driving the input cannot satisfy a firmware-output wait.
    lines.driven(S, [(4, active)], frozenset({4}))
    assert on() and not off()
    lines.driven(2 * S, [(4, 1 - active)], frozenset({4}))
    assert off() and not on()  # A condition reads the live level, not the level at creation.
    lines.driven(3 * S, [(4, 0)], frozenset())
    assert not on() and not off()


def test_output_handles_resolve_pin_names_and_filter_events_without_driving() -> None:
    lines, clock = lilygo()
    output = lines.output(4)
    assert (output.name, output.pin, output.active) == ("modem.pwrkey", 4, 1)
    assert output.events() == []
    # Only the board's RI pull: constructing and reading a handle queues no GPIO input.
    assert list(lines.inputs(0)) == [GpioIn(0, ((33, 0),))]
    clock.now = S
    lines.drive(33, 1)
    lines.drive(4, 0)
    lines.driven(2 * S, [(4, 1)], frozenset({4}))
    assert output.events() == [
        LineEvent(seconds(1), "modem.pwrkey", 4, "test", 0),
        LineEvent(seconds(2), "modem.pwrkey", 4, "firmware", 1),
    ]
    assert repr(output.is_on) == "modem.pwrkey is on (GPIO 4, level 1)"
    assert repr(lines.output(4, active=0).is_off) == "modem.pwrkey is off (GPIO 4, level 1)"


@pytest.mark.parametrize(
    "active,error", [(2, ValueError), (-1, ValueError), ("1", TypeError), (None, TypeError)]
)
def test_output_polarity_is_validated_before_waiting(active: int, error: type[Exception]) -> None:
    lines, _ = lilygo()
    with pytest.raises(error, match=r"lines\.output: active .* is not 0 or 1"):
        lines.output(4, active=active)


def test_output_name_is_validated_before_waiting() -> None:
    lines, _ = lilygo()
    with pytest.raises(ValueError, match="'missing' is no line of the board profile"):
        lines.output("missing")


def test_a_condition_cannot_accidentally_pass_an_assertion_without_being_called() -> None:
    lines, _ = lilygo()
    output = lines.output(4)
    for condition in (output.is_on, output.is_off):
        with pytest.raises(TypeError, match="call the output condition with"):
            bool(condition)


@pytest.mark.parametrize("active", [0, 1])
def test_output_assertions_report_current_state_without_advancing_time(active: int) -> None:
    lines, clock = lilygo()
    output = lines.output(4, active=active)
    for check in (output.assert_on, output.assert_off):
        with pytest.raises(AssertionError, match=r"GPIO 4.*now; observed undriven/unknown"):
            check()
    lines.driven(0, [(4, active)], frozenset({4}))
    output.assert_on()
    with pytest.raises(AssertionError, match=rf"expected off .*observed level {active}"):
        output.assert_off()
    lines.driven(0, [(4, 1 - active)], frozenset({4}))
    output.assert_off()
    with pytest.raises(AssertionError, match="expected on"):
        output.assert_on()
    assert clock.now == 0
    assert list(lines.inputs(0)) == [GpioIn(0, ((33, 0),))]


@pytest.mark.parametrize("active", [0, 1])
@pytest.mark.parametrize("on", [False, True])
def test_output_history_catches_short_pulses_even_after_state_recovers(
    active: int, on: bool
) -> None:
    lines, clock = lilygo()
    output = lines.output(4, active=active)
    level = active if on else 1 - active
    check = output.assert_stayed_on if on else output.assert_stayed_off
    lines.driven(0, [(4, level)], frozenset({4}))
    lines.driven(S, [(4, 1 - level)], frozenset({4}))
    lines.driven(S + 1, [(4, level)], frozenset({4}))
    clock.now = 2 * S
    assert output.is_on() if on else output.is_off()
    with pytest.raises(AssertionError, match=r"at 1 s during \[0, 2\] s; observed level"):
        check(since=seconds(0))
    check(since=seconds(1.5))
    assert clock.now == 2 * S


def test_output_history_requires_a_known_start_and_rejects_released_outputs() -> None:
    lines, clock = lilygo()
    output = lines.output(4)
    lines.driven(S, [(4, 0)], frozenset({4}))
    clock.now = 2 * S
    with pytest.raises(AssertionError, match=r"at 0 s .*undriven/unknown"):
        output.assert_stayed_off(since=seconds(0))
    output.assert_stayed_off(since=seconds(1))
    lines.driven(2 * S, [], frozenset())
    with pytest.raises(AssertionError, match=r"at 2 s .*undriven/unknown"):
        output.assert_stayed_off(since=seconds(1))


def test_output_history_uses_start_state_and_includes_the_current_instant() -> None:
    lines, clock = lilygo()
    output = lines.output(4)
    lines.driven(0, [(4, 1)], frozenset({4}))
    clock.now = 2 * S
    with pytest.raises(AssertionError, match=r"at 1 s .*observed level 1"):
        output.assert_stayed_off(since=seconds(1))
    lines.driven(2 * S, [(4, 0)], frozenset({4}))
    with pytest.raises(AssertionError, match=r"at 2 s .*observed level 0"):
        output.assert_stayed_on(since=seconds(1))
    output.assert_stayed_off(since=seconds(2))
    # Another transition at the same timestamp: the starting state is the last reported one.
    lines.driven(2 * S, [(4, 1)], frozenset({4}))
    output.assert_stayed_on(since=seconds(2))


def test_output_history_ignores_inputs_other_pins_and_future_events() -> None:
    lines, clock = lilygo()
    output = lines.output(4)
    lines.driven(0, [(4, 0)], frozenset({4}))
    clock.now = S
    lines.drive(4, 1)
    lines.driven(S, [(33, 1)], frozenset({4, 33}))
    lines.driven(3 * S, [(4, 1)], frozenset({4, 33}))
    clock.now = 2 * S
    output.assert_stayed_off(since=seconds(0))


@pytest.mark.parametrize(
    "since,error,message",
    [
        (1, TypeError, "since must be a Duration on the board's clock"),
        (seconds(-1), ValueError, "since must be between zero and board.now"),
        (seconds(3), ValueError, "since must be between zero and board.now"),
    ],
)
def test_output_history_validates_the_window(
    since: Duration, error: type[Exception], message: str
) -> None:
    lines, clock = lilygo()
    clock.now = 2 * S
    with pytest.raises(error, match=message):
        lines.output(4).assert_stayed_off(since=since)
