"""The board profile's SD card slot, `sd_card`: what is in it. The `sd_card` fixture is it.

On the `esp32sim` backend the card is a model in the runner, holding an image file: an SD card in
SPI mode on the slot's pins, which writes what the firmware writes into the file as it goes. The
test prepares the image and reads it back with ordinary tools, such as mtools.
"""

import os
from collections.abc import Callable
from pathlib import Path

from esp32tl.boards import SdCardRoute
from esp32tl.interfaces import SourceError

# The capacity unit of a high capacity card (CSD C_SIZE): an image is a whole number of them.
UNIT = 512 * 1024


class SdCard:
    """The slot on `route`, empty until the test inserts a card.

    `asked` is called with the name and the kind of source (`model`) before the card goes in,
    and refuses it by raising: the backend's checks, that the board is still off and that a
    backend gives that source. A relative image path is taken from `directory`, the test file's.
    """

    name = "sd_card"

    def __init__(
        self,
        route: SdCardRoute,
        *,
        directory: Path,
        asked: Callable[[str, str], None] = lambda name, source: None,
    ) -> None:
        self.route = route
        self.directory = directory
        self.asked = asked
        # The image of the card in the slot, absolute; None while the slot is empty.
        self.image: Path | None = None

    def insert(self, image: str | os.PathLike[str]) -> Path:
        """Puts a card holding the image file `image` in the slot, before `board.power_on()`.
        The firmware's writes go into the file. The image is a whole number of 512 KiB units, at
        least one; its contents are the test's, a FAT file system or anything else. Returns the
        image's absolute path."""
        if self.image is not None:
            raise SourceError(f"{self.name} has a card already: one per test")
        self.asked(self.name, "model")
        path = (self.directory / image).resolve()
        try:
            size = path.stat().st_size
        except OSError as error:
            raise SourceError(f"{self.name}: no image at {path}: {error.strerror}") from None
        if not path.is_file():
            raise SourceError(f"{self.name}: {path} is not a file")
        if size == 0 or size % UNIT:
            raise SourceError(
                f"{self.name}: {path} has {size} bytes; an SD card image is a whole number of"
                " 512 KiB units"
            )
        self.image = path
        return path
