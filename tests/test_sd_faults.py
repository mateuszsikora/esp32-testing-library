"""The SD card's fault rules through the public `sd_card` API, without a runner: what the test may
give, what goes on the link, and what the card's reports become."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from esp32tl.boards import SdCardRoute
from esp32tl.interfaces import SourceError
from esp32tl.interfaces.sd_card import MAX_DELAY, CardFault, SdCard
from esp32tl.link.boardlink import BoardLink
from esp32tl.link.protocol import RunnerHello, SdClear, SdFault, SdIo, Sync, Wait
from esp32tl.report import RunLog
from esp32tl.units import Duration, ms, seconds

MS = 1_000_000
HELLO = RunnerHello(1, "esp32", "emulated-fifo-write", "8c652151dd5a521249a785ca147f20a9f8eda68f")


class Clock:
    """The board's time as the test reads it."""

    def __init__(self) -> None:
        self.t = 0

    def __call__(self) -> int:
        return self.t


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def log() -> RunLog:
    return RunLog()


@pytest.fixture
def slot(tmp_path: Path, clock: Clock, log: RunLog) -> Iterator[SdCard]:
    card = SdCard(
        SdCardRoute(sclk=14, mosi=15, miso=2, cs=13), directory=tmp_path, now=clock, log=log
    )
    try:
        yield card
    finally:
        card.close()


@pytest.fixture
def card(slot: SdCard, tmp_path: Path) -> SdCard:
    """The slot with a card of 1 MiB in it: blocks 0 to 2047."""
    image = tmp_path / "card.img"
    image.write_bytes(bytes(1 << 20))
    slot.insert(image)
    return slot


def test_a_slot_without_a_card_takes_no_fault(slot: SdCard) -> None:
    with pytest.raises(SourceError, match="sd_card: the slot is empty: insert a card before"):
        slot.fail("write")
    with pytest.raises(SourceError, match="the slot is empty"):
        slot.delay("read", ms(1))
    assert slot.inputs(0) == []


def test_each_rule_goes_on_the_link_as_given_and_at_the_turn_at_the_earliest(
    card: SdCard, clock: Clock
) -> None:
    clock.t = 3 * MS
    once = card.fail("write")
    slow = card.delay("read", ms(200), blocks=range(64, 96), skip=3)
    erases = card.delay("erase", seconds(1), count=2)
    picked = card.fail("read", blocks=7, count=None)
    assert [f.id for f in card.faults] == [1, 2, 3, 4]
    assert once.at == Duration(3 * MS)
    assert (once.count, slow.count, erases.count, picked.blocks) == (1, None, 2, range(7, 8))
    # a turn is never before the time the test gave its rule at; one later stamps them with it
    assert card.inputs(5 * MS) == [
        SdFault(5 * MS, 1, "write", True, 0, None, None, 0, 1),
        SdFault(5 * MS, 2, "read", False, 200 * MS, 64, 95, 3, None),
        SdFault(5 * MS, 3, "erase", False, 1000 * MS, None, None, 0, 2),
        SdFault(5 * MS, 4, "read", True, 0, 7, 7, 0, None),
    ]
    assert card.inputs(6 * MS) == []


def test_clearing_rules_goes_on_the_link_too(card: SdCard, clock: Clock) -> None:
    first = card.fail("write")
    card.delay("write", ms(5))
    card.inputs(0)
    clock.t = 2 * MS
    first.clear()
    assert first.cleared_at == Duration(2 * MS)
    clock.t = 4 * MS
    card.clear_faults()
    assert [f.cleared_at for f in card.faults] == [Duration(2 * MS), Duration(4 * MS)]
    assert card.inputs(4 * MS) == [SdClear(4 * MS, (1,)), SdClear(4 * MS)]


@pytest.mark.parametrize(
    ("give", "error", "found"),
    [
        (lambda c: c.fail("erase"), ValueError, "sd_card.fail: op is 'erase', not 'read' or"),
        (lambda c: c.delay("format", ms(1)), ValueError, "op is 'format', not 'read', 'write'"),
        (lambda c: c.delay("read", 0.2), TypeError, "sd_card.delay: by is 0.2, not a Duration"),
        (lambda c: c.delay("read", Duration(0)), ValueError, "by is 0 s; a delay is more than 0"),
        (lambda c: c.delay("read", MAX_DELAY + Duration(1)), ValueError, "at most 10 s"),
        (lambda c: c.fail("read", blocks=2048), ValueError, "the card's blocks are 0 to 2047"),
        (lambda c: c.fail("read", blocks=range(2000, 2049)), ValueError, "blocks are 0 to 2047"),
        (lambda c: c.fail("read", blocks=range(5, 5)), ValueError, "a non-empty range of them"),
        (lambda c: c.fail("read", blocks=range(0, 9, 2)), ValueError, "a non-empty range"),
        (lambda c: c.fail("read", blocks=-1), ValueError, "a non-empty range of them"),
        (lambda c: c.fail("read", blocks=True), TypeError, "blocks is True, not a block number"),
        (lambda c: c.fail("read", skip=-1), ValueError, "skip is -1, not a whole number of 0"),
        (lambda c: c.fail("read", count=0), ValueError, "count is 0, not None or a whole number"),
        (lambda c: c.delay("read", ms(1), count=1.5), ValueError, "count is 1.5"),
    ],
)
def test_a_rule_the_card_cannot_take_is_refused_before_it_goes_anywhere(
    card: SdCard, give: object, error: type[Exception], found: str
) -> None:
    with pytest.raises(error, match=found):
        give(card)  # type: ignore[operator]
    assert card.faults == ()
    assert card.inputs(0) == []


def test_the_cards_reports_become_hits_of_the_rules_that_acted(card: SdCard) -> None:
    first = card.fail("write")
    second = card.delay("write", ms(5))
    card.card_io(SdIo(7 * MS, "write", 8, 1, (1, 2), True, 5 * MS, False))
    card.card_io(SdIo(9 * MS, "write", 9, 1, (2,), False, 5 * MS, True))
    [both, slow] = card.affected
    assert first.hits == (both,)
    assert second.hits == (both, slow)
    assert both.faults == (first, second)
    assert (both.at, both.op, both.block, both.blocks) == (Duration(7 * MS), "write", 8, 1)
    assert (both.failed, both.delay, both.written) == (True, ms(5), False)
    assert repr(slow) == "CardIo(0.009 s write block 9: delayed 0.005 s, written (fault 2))"


def test_the_report_and_the_properties_say_what_each_rule_did(
    card: SdCard, clock: Clock, log: RunLog
) -> None:
    clock.t = MS
    card.fail("write", blocks=range(8, 16), skip=2)
    card.delay("erase", ms(250), count=1)
    card.card_io(SdIo(2 * MS, "erase", 64, 32, (2,), False, 250 * MS, True))
    clock.t = 3 * MS
    card.clear_faults()
    image = card.image_path
    assert card.report() == [
        f"sd_card: a card holding {image}",
        "  fault 1 from 0.001 s: fail writes on blocks 8 to 15, after skipping 2, 1 time,"
        " cleared at 0.003 s; 0 operations",
        "  fault 2 from 0.001 s: delay by 0.25 s erases on every block, 1 time,"
        " cleared at 0.003 s; 1 operation",
        "    0.002 s erase blocks 64 to 95: delayed 0.25 s, written (fault 2)",
    ]
    properties = card.properties()
    assert properties["image"] == str(image)
    assert properties["faults"] == [
        {
            "id": 1,
            "t_ns": MS,
            "op": "write",
            "fail": True,
            "delay_ns": 0,
            "blocks": [8, 15],
            "skip": 2,
            "count": 1,
            "cleared_ns": 3 * MS,
        },
        {
            "id": 2,
            "t_ns": MS,
            "op": "erase",
            "fail": False,
            "delay_ns": 250 * MS,
            "blocks": None,
            "skip": 0,
            "count": 1,
            "cleared_ns": 3 * MS,
        },
    ]
    assert properties["affected"] == [
        {
            "t_ns": 2 * MS,
            "op": "erase",
            "block": 64,
            "blocks": 32,
            "faults": [2],
            "failed": False,
            "delay_ns": 250 * MS,
            "written": True,
        }
    ]
    steps = "\n".join(log.lines())
    assert "sd_card.fail('write', blocks=range(8, 16), skip=2): CardFault(1)" in steps
    assert "sd_card.delay('erase', seconds(0.25), count=1): CardFault(2)" in steps
    assert "sd_card.clear_faults()" in steps


def test_an_empty_slot_reports_itself_empty(slot: SdCard) -> None:
    assert slot.report() == ["sd_card: empty"]
    assert slot.properties() == {"image": None, "faults": [], "affected": []}


def test_the_slot_is_the_links_sd_card_and_its_rules_pass_the_links_checks(card: SdCard) -> None:
    fault = card.fail("read", blocks=range(0, 2048))
    link = BoardLink("esp32-devkitc", [card])
    assert link.watch == ()
    link.receive(HELLO)
    out = link.receive(Sync(0))
    assert out == [SdFault(0, 1, "read", True, 0, 0, 2047, 0, 1), Wait(0, None, None)]
    link.receive(SdIo(MS, "read", 0, 1, (1,), True, 0, False))
    assert link.error is None
    assert [io.block for io in fault.hits] == [0]
    assert isinstance(card.faults[0], CardFault)


def test_a_closed_slot_takes_no_fault(card: SdCard) -> None:
    card.close()
    with pytest.raises(SourceError, match="sd_card: the board is closed"):
        card.fail("write")
    with pytest.raises(SourceError, match="the board is closed"):
        card.clear_faults()
