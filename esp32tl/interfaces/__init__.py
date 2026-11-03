"""One class per wire: console, UART (GNSS, modem), lines, Wi-Fi, SD card."""


class SourceError(Exception):
    """A source the test gives where the board cannot take it: after `board.power_on()`, a
    second one on the same route, or one that no backend gives."""


class BoardOff(Exception):
    """The test waited on a board it has not powered on, powered on a board that is on, or
    switched off or reset one that is off."""


class RunEnded(Exception):
    """The run ended while the test waited on the board's clock: the runner exited, or the link
    failed. The message says when, and why."""


class NeverHappened(AssertionError):
    """What the test waited for did not happen within the board's time it gave: a `wait_until`
    condition that never held, a console pattern that never appeared."""
