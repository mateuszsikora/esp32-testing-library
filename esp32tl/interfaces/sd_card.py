"""The board profile's SD card slot, `sd_card`: what is in it. The `sd_card` fixture is it.

On the `esp32sim` backend the card is a model in the runner, holding an image file: an SD card in
SPI mode on the slot's pins, which writes what the firmware writes into the file as it goes. The
test prepares the image and reads it back with ordinary tools, such as mtools.
"""

import os
from collections.abc import Callable
from pathlib import Path

from esp32tl.boards import SdCardRoute
from esp32tl.boards.capabilities import Request
from esp32tl.interfaces import SourceError
from esp32tl.interfaces.uart import same_file
from esp32tl.report import RunLog

# The capacity unit of a high capacity card (CSD C_SIZE): an image is a whole number of them.
UNIT = 512 * 1024
# The most a card holds: 2^22 units (CSD C_SIZE).
MAX_SIZE = UNIT << 22


class SdCard:
    """The slot on `route`, empty until the test inserts a card.

    `asked` is called with the `Request` of a `model` source before the card goes in,
    and refuses it by raising: the backend's checks, that the board is still off and that a
    backend gives that source. A relative image path is taken from `directory`, the test file's.
    `log`, when given, gets the test's `insert` as a step, at 0: it comes before power-on.
    `files` holds the files the run reads and writes, shared with the routes, so that no tap
    writes over the card's image and the card writes over no recording.
    """

    name = "sd_card"

    def __init__(
        self,
        route: SdCardRoute,
        *,
        directory: Path,
        asked: Callable[[Request], None] = lambda request: None,
        log: RunLog | None = None,
        files: dict[Path, tuple[str, str]] | None = None,
    ) -> None:
        self.route = route
        self.directory = directory
        self.asked = asked
        self.log = log
        self.files = {} if files is None else files
        # The image of the card in the slot, absolute; None while the slot is empty.
        self.image: Path | None = None

    def insert(self, image: str | os.PathLike[str]) -> Path:
        """Puts a card holding the image file `image` in the slot, before `board.power_on()`.
        The firmware's writes go into the file, which must be readable and writable. The image is
        a whole number of 512 KiB units, from one to 2 TiB; its contents are the test's, a FAT
        file system or anything else. Returns the image's absolute path."""
        if self.image is not None:
            raise SourceError(f"{self.name} has a card already: one per test")
        self.asked(Request(self.name, source="model"))
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
        if size > MAX_SIZE:
            raise SourceError(
                f"{self.name}: {path} has {size} bytes; an SD card image is at most 2 TiB"
            )
        # The card writes into it, and the runner opens it for writing at power-on.
        if not os.access(path, os.R_OK | os.W_OK):
            raise SourceError(
                f"{self.name}: {path} cannot be read and written: the card writes into it"
            )
        for claimed, (kind, interface) in self.files.items():
            if same_file(path, claimed):
                raise SourceError(
                    f"{self.name}: cannot hold {path}: it is the {kind} of {interface}"
                )
        self.files[path] = ("card", self.name)
        self.image = path
        if self.log is not None:
            self.log.test(0, f"{self.name}.insert({str(image)!r})")
        return path
