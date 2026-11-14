"""The `sd_card` slot without the emulator: a card in and out before power-on and while the board
is on, what it sends on a `BoardLink`, and what it does with the runner's acknowledgements."""

from pathlib import Path

import pytest

from esp32tl.boards import SdCardRoute
from esp32tl.boards.capabilities import Request
from esp32tl.interfaces import SourceError
from esp32tl.interfaces.sd_card import CardChange, SdCard
from esp32tl.link.boardlink import BoardLink, Interface
from esp32tl.link.protocol import (
    RunnerHello,
    SdCardAck,
    SdCardChange,
    Stop,
    Sync,
    Wait,
)

HELLO = RunnerHello(1, "esp32", "emulated-fifo-write", "3bdb9582a910974e4e025d274d6ec61e5c481be6")
ROUTE = SdCardRoute(sclk=14, mosi=15, miso=2, cs=13)
MS = 1_000_000


class Backend:
    """What the slot asks of its backend: the clock and whether the board is on."""

    def __init__(self) -> None:
        self.now = 0
        self.on = False
        self.asked: list[Request] = []


@pytest.fixture
def backend() -> Backend:
    return Backend()


@pytest.fixture
def slot(backend: Backend, tmp_path: Path) -> SdCard:
    return SdCard(
        ROUTE,
        directory=tmp_path,
        asked=backend.asked.append,
        now=lambda: backend.now,
        started=lambda: backend.on,
    )


def image(directory: Path, name: str, units: int = 1) -> Path:
    path = directory / name
    path.write_bytes(bytes(units * 512 << 10))
    return path


def started(*interfaces: Interface) -> BoardLink:
    link = BoardLink("esp32-devkitc", interfaces)
    link.receive(HELLO)
    link.receive(Sync(0))
    return link


def test_before_power_on_the_card_goes_in_and_out_without_the_link(
    slot: SdCard, backend: Backend, tmp_path: Path
) -> None:
    a = image(tmp_path, "a.img")
    assert slot.insert("a.img") == a
    assert slot.files == {a: ("card", "sd_card")}
    assert slot.remove() == a
    assert slot.image_path is None and slot.files == {}
    slot.insert(a)
    assert list(slot.inputs(0)) == [] and slot.changes == []
    assert backend.asked == [Request("sd_card", source="model")] * 3


def test_with_the_board_on_each_change_goes_out_at_its_instant_and_is_acknowledged(
    slot: SdCard, backend: Backend, tmp_path: Path
) -> None:
    a, b = image(tmp_path, "a.img"), image(tmp_path, "b.img", 2)
    slot.insert(a)
    backend.on = True
    link = started(slot)
    backend.now = 2 * MS
    assert slot.remove() == a
    # the image the card left is free again
    assert slot.files == {}
    slot.insert(b)
    assert slot.image_path == b and slot.files == {b: ("card", "sd_card")}
    assert link.receive(Sync(2 * MS)) == [
        SdCardChange(2 * MS, None),
        SdCardChange(2 * MS, str(b)),
        Wait(0, None, None),
    ]
    # what the runner applied is there once it says so, in order
    assert slot.changes == []
    link.receive(SdCardAck(2 * MS, None))
    link.receive(SdCardAck(2 * MS, str(b)))
    assert slot.changes == [CardChange(2 * MS, None), CardChange(2 * MS, b)]
    assert link.error is None
    # nothing more to send
    assert link.receive(Sync(3 * MS)) == [Wait(0, None, None)]


def test_the_card_the_run_has_counts_only_the_changes_the_runner_acknowledged(
    slot: SdCard, backend: Backend, tmp_path: Path
) -> None:
    a, b = image(tmp_path, "a.img"), image(tmp_path, "b.img")
    assert slot.held is None
    slot.insert(a)
    # before power-on the test's card is the run's
    assert slot.held == a and slot.not_reached == []
    backend.on = True
    link = started(slot)
    backend.now = MS
    slot.remove()
    slot.insert(b)
    # asked for, not reached: the run still has the card it had at power-on
    assert slot.image_path == b and slot.held == a
    assert slot.not_reached == [CardChange(MS, None), CardChange(MS, b)]
    link.receive(Sync(MS))
    link.receive(SdCardAck(MS, None))
    assert slot.held is None and slot.not_reached == [CardChange(MS, b)]
    link.receive(SdCardAck(MS, str(b)))
    assert slot.held == b and slot.not_reached == []


def test_one_card_at_a_time_and_none_to_take_out_of_an_empty_slot(
    slot: SdCard, backend: Backend, tmp_path: Path
) -> None:
    with pytest.raises(SourceError, match="sd_card is empty: there is no card to remove"):
        slot.remove()
    slot.insert(image(tmp_path, "a.img"))
    backend.on = True
    with pytest.raises(SourceError, match="sd_card has a card already: remove it first"):
        slot.insert(image(tmp_path, "b.img"))
    assert list(slot.inputs(0)) == []


def test_every_insertion_checks_the_image_and_its_owner(
    slot: SdCard, backend: Backend, tmp_path: Path
) -> None:
    tap = image(tmp_path, "tap.jsonl")
    slot.files[tap] = ("tap", "uart.gnss")
    backend.on = True
    (tmp_path / "short.img").write_bytes(bytes(1000))
    for name, error in [
        ("tap.jsonl", "cannot hold .*: it is the tap of uart.gnss"),
        ("short.img", "has 1000 bytes; an SD card image is a whole number of 512 KiB units"),
        ("missing.img", "no image at .*missing.img"),
    ]:
        with pytest.raises(SourceError, match=error):
            slot.insert(name)
    assert slot.image_path is None and list(slot.inputs(0)) == []
    # the same image twice, with the card out in between
    a = image(tmp_path, "a.img")
    slot.insert(a)
    slot.remove()
    slot.insert(a)
    assert list(slot.inputs(0)) == [
        SdCardChange(0, str(a)),
        SdCardChange(0, None),
        SdCardChange(0, str(a)),
    ]


@pytest.mark.parametrize(
    ("ack", "error"),
    [
        (
            SdCardAck(MS, None, "/x/a.img: No such file or directory (os error 2)"),
            "sd_card: the runner refused the card at 1000000 ns: /x/a.img: No such file",
        ),
        (
            SdCardAck(MS, "/elsewhere.img"),
            "sd_card: the runner says the slot holds /elsewhere.img at 1000000 ns, where the test"
            " asked for /",
        ),
    ],
)
def test_a_refused_or_another_card_fails_the_link(
    slot: SdCard, backend: Backend, tmp_path: Path, ack: SdCardAck, error: str
) -> None:
    backend.on = True
    link = started(slot)
    backend.now = MS
    a = image(tmp_path, "a.img")
    slot.insert(a)
    link.receive(Sync(MS))
    link.receive(ack)
    assert link.error is not None and link.error.startswith(error), link.error
    assert link.receive(Sync(2 * MS)) == [Stop()]
    assert slot.not_reached == []
    if ack.error is not None:
        # the slot stays as it was, and the report still has the change, as refused
        assert slot.refused == [CardChange(MS, a)] and slot.changes == [] and slot.held is None
    else:
        # what the runner says it holds is what the run has
        assert slot.refused == [] and slot.held == Path("/elsewhere.img")


def test_an_acknowledgement_nobody_asked_for_fails_the_link(slot: SdCard) -> None:
    link = started(slot)
    link.receive(SdCardAck(MS, None))
    assert link.error == "sd_card: the runner acknowledged a change nobody asked for"
    link = started()
    link.receive(SdCardAck(MS, None))
    assert link.error == "the runner sent sd_card_ack, and no interface is the SD card slot"


class Other(Interface):
    """An interface that is not the slot, and sends `sd_card` all the same."""

    def __init__(self) -> None:
        super().__init__("lines", None)

    def inputs(self, now: int) -> list[SdCardChange]:
        return [SdCardChange(now, None)]


def test_only_the_slot_changes_the_card_and_a_board_has_one_slot(slot: SdCard) -> None:
    link = BoardLink("esp32-devkitc", [Other()])
    link.receive(HELLO)
    assert link.receive(Sync(0)) == [Stop()]
    assert link.error == "lines sent sd_card, and is not the SD card slot"
    with pytest.raises(ValueError, match="one SD card slot, not sd_card, sd_card"):
        BoardLink("esp32-devkitc", [slot, slot])
