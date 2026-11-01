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

HELLO = RunnerHello(1, "esp32", "emulated-fifo-write", "8c652151dd5a521249a785ca147f20a9f8eda68f")
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
    assert lines.pins == {4, 33}
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
            5,
            1,
            ValueError,
            "GPIO 5 is no line of the board profile lilygo-t-a7670g-r2; its lines are"
            " modem.pwrkey (GPIO 4), modem.ri (GPIO 33)",
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
        LineEvent(2.0, "modem.ri", 33, "test", 1),
        LineEvent(2.0, "modem.pwrkey", 4, "test", 1),
        LineEvent(3.0, "modem.pwrkey", 4, "test", None),
        LineEvent(3.0, "modem.ri", 33, "test", None),
        LineEvent(4.0, "modem.ri", 33, "test", 1),
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
        LineEvent(1.0, "modem.pwrkey", 4, "firmware", 0),
        LineEvent(2.0, "modem.pwrkey", 4, "firmware", 1),
        LineEvent(3.0, "modem.pwrkey", 4, "firmware", None),
        LineEvent(4.0, "modem.pwrkey", 4, "firmware", 0),
        LineEvent(5.0, "modem.pwrkey", 4, "firmware", None),
        LineEvent(6.0, "modem.pwrkey", 4, "firmware", 1),
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
    assert link.receive(HELLO)[0].watch == (4, 33)  # type: ignore[union-attr]
    assert link.receive(Sync(0)) == [GpioIn(0, ((33, 0),)), Wait(0, None, None)]
    link.receive(GpioOut(1, S, ((4, 1),), (4,), (4,)))
    clock.now = 2 * S
    lines.drive("modem.ri", 1)
    assert link.receive(Sync(2 * S)) == [GpioIn(2 * S, ((33, 1),)), Wait(1, None, None)]
    assert link.error is None
    assert lines.level("modem.pwrkey") == 1
    assert [(e.t, e.by) for e in lines.events()] == [(1.0, "firmware"), (2.0, "test")]
