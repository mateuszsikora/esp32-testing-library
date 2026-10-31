"""The console: the firmware's UART0, as pytest-embedded's `Dut` reads it."""

from pytest_embedded.dut import Dut


class Console:
    """`expect` and `expect_exact` are the dut's own, with pytest-embedded's arguments and
    timeouts. There is no `write` yet: the runner does not read its stdin."""

    def __init__(self, dut: Dut) -> None:
        self.dut = dut
        self.expect = dut.expect
        self.expect_exact = dut.expect_exact
