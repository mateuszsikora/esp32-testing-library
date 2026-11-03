"""One board owns the interface state, regardless of how a test requests it."""

import pytest

from esp32tl import Board
from esp32tl.interfaces import SourceError
from esp32tl.interfaces.console import Console
from esp32tl.interfaces.gnss import Gnss
from esp32tl.interfaces.lines import Lines
from esp32tl.interfaces.modem import Modem
from esp32tl.interfaces.sd_card import SdCard
from esp32tl.net.mqtt import Broker

pytestmark = pytest.mark.app_path("../firmware/hello_world")


@pytest.mark.board_profile("sd-data-logger")
def test_fixture_aliases_share_interfaces_without_powering_on(
    board: Board, console: Console, gnss: Gnss, lines: Lines, sd_card: SdCard
) -> None:
    assert isinstance(board, Board)
    assert board.console is console
    assert board.gnss is gnss is board.uart("gnss")
    assert board.lines is lines
    assert board.sd_card is sd_card
    assert gnss.source is None
    assert sd_card.image_path is None
    assert not board.powered
    board.power_on()
    board.console.expect_exact("Hello world!")
    assert console is board.console  # Matching through either access uses one cursor.


@pytest.mark.board_profile("lilygo-t-a7670g-r2")
def test_modem_alias_does_not_select_a_source(board: Board, modem: Modem) -> None:
    assert board.modem is modem is board.uart("modem")
    assert modem.source is None
    assert not board.powered


@pytest.mark.board_profile("lilygo-t-a7670g-r2")
def test_broker_alias_uses_the_same_modem_network(board: Board, broker: Broker) -> None:
    assert broker is board.modem.network().broker
    board.power_on()


@pytest.mark.requires_mtools
def test_a_build_after_power_on_is_a_replacement_and_close_removes_images(board: Board) -> None:
    builder = board.sd_card.image().with_text("BEFORE.TXT", "before boot")
    card = builder.build()
    board.sd_card.insert(card)
    board.power_on()
    # a card to put in later, while the firmware runs
    replacement = builder.with_text("AFTER.TXT", "after boot").build()
    assert replacement.path != card.path
    board.console.expect_exact("Hello world!")
    assert card.read_text("BEFORE.TXT") == "before boot"
    assert replacement.list_files() == ["AFTER.TXT", "BEFORE.TXT"]
    board.close()
    assert not card.path.exists() and not replacement.path.exists()
    with pytest.raises(SourceError, match="the board is closed"):
        builder.build()
