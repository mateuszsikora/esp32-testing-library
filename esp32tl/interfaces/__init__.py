"""One class per wire: console, UART (GNSS, modem), lines, HCI, Wi-Fi, SD card."""


class SourceError(Exception):
    """A source given otherwise than the test's `source` markers declare it, or not given before
    the test waits on the board."""
