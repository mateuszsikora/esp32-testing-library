"""Console input uses the existing UART wire protocol and tap directions."""

from pathlib import Path
from typing import cast

import pytest

from esp32tl.interfaces.console import Clocked, Console, ConsoleInput
from esp32tl.link.boardlink import BoardLink, Wire
from esp32tl.link.protocol import RunnerHello, Sync, UartRx, Warn


def test_console_writes_keep_the_instant_and_host_baud(tmp_path: Path) -> None:
    route = ConsoleInput(
        "console", Wire(None, 3, 115200), clock="emulated-fifo-write", directory=tmp_path
    )
    tap = route.tap("console.jsonl")
    link = BoardLink("esp32-devkitc", [route])
    link.receive(RunnerHello(1, "esp32", "emulated-fifo-write", "0" * 40))
    assert link.watch == (3,)

    class Board:
        def write_console(self, data: bytes) -> None:
            route.write(123, data)

    console = Console(cast(Clocked, Board()))
    console.write("ą")
    console.sendline(b"help")
    assert link.receive(Sync(123))[:-1] == [
        UartRx(123, 3, 115200, "ą".encode()),
        UartRx(123, 3, 115200, b"help\n"),
    ]
    assert tap.events == [
        (123, "in", "ą".encode()),
        (123, "in", b"help\n"),
    ]
    assert route.poll(124) == []
    warning = Warn(124, "frm_err", "wrong baud", pin=3, port=0, baud=9600, nbytes=1)
    link.receive(warning)
    assert link.error is not None and "console" in link.error
    assert "frm_err" in link.report()


def test_console_rejects_non_bytes_before_touching_the_board() -> None:
    console = Console(cast(Clocked, object()))
    with pytest.raises(TypeError, match="bytes or str"):
        console.write(1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="bytes or str"):
        console.sendline(None)  # type: ignore[arg-type]
