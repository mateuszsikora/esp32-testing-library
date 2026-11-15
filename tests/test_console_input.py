"""Console input on UART0, including the native RX route after boot."""

from pathlib import Path

import pytest

from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.units import seconds


@pytest.mark.app_path("../firmware/console_repl")
def test_idf_repl_help(board: Esp32Sim, tmp_path: Path) -> None:
    tap = board.console.tap(tmp_path / "console.jsonl")
    board.power_on()
    board.console.expect_exact("console_repl>")
    at = board.now.ns
    board.console.sendline("help")
    assert any(
        t >= at and direction == "in" and data == b"help\n" for t, direction, data in tap.events
    )
    board.console.expect_exact("Print the summary of all registered commands")


@pytest.mark.app_path("../firmware/hello_world")
def test_console_fifo_overflow(board: Esp32Sim) -> None:
    if not hasattr(board, "link"):
        pytest.skip("serial: UART FIFO warnings are available only on esp32sim")
    board.power_on()
    board.console.expect_exact("Hello world!")
    board.console.write(b"x" * 256)
    board.wait(seconds(0.1))
    assert any(w.kind == "rxfifo_ovf" and w.port == 0 for w in board.link.warnings)
