"""ESP-IDF's hello_world, unmodified, prints through the backend into a pytest assertion (plan M0).

test_pytest_plugin.py runs this file against an image that prints something else, and checks that
it fails there.
"""

import pytest

from esp32tl.interfaces.console import Console
from esp32tl.pytest_plugin import Board

pytestmark = pytest.mark.app_path("../firmware/hello_world")


def test_hello_world(board: Board, console: Console) -> None:
    board.power_on()
    console.expect("Hello world!")
