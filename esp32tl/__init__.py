"""esp32tl: test unmodified ESP32 firmware through its wires."""

from importlib.metadata import version

__version__ = version("esp32tl")

from esp32tl.interfaces.board import Board as Board
from esp32tl.interfaces.condition import Condition as Condition
from esp32tl.interfaces.condition import condition as condition
