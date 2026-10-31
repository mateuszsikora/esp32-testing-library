"""The console: the firmware's UART0, as pytest-embedded's `Dut` reads it."""

import functools
from collections.abc import Callable, Mapping
from typing import Any

from pytest_embedded.dut import Dut

from esp32tl.interfaces import SourceError


class Console:
    """`expect` and `expect_exact` are the dut's own, with pytest-embedded's arguments and
    timeouts, once the run has started. Before, while `waiting()` names the interfaces whose
    source the run waits for, they raise `SourceError` at once: the run could not reach what they
    wait for. There is no `write` yet: the runner does not read its stdin."""

    def __init__(self, dut: Dut, waiting: Callable[[], Mapping[str, str]] = dict) -> None:
        self.dut = dut
        self._waiting = waiting
        # Wrapped, so that they show the dut's own signatures and documentation.
        self.expect = self._checked(dut.expect)
        self.expect_exact = self._checked(dut.expect_exact)

    def _checked(self, expect: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(expect)
        def checked(*args: Any, **kwargs: Any) -> Any:
            self._started()
            return expect(*args, **kwargs)

        return checked

    def _started(self) -> None:
        waiting = self._waiting()
        if waiting:
            names = " and ".join(f"the {source} of {name}" for name, source in waiting.items())
            raise SourceError(
                f"the run has not started: it waits for {names}, which the source marker"
                " declares; give it before waiting on the console"
            )
