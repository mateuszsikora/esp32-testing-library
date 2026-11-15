"""The SD card's fault rules without the emulator: what `sd_card.fail`, `sd_card.delay` and the
clears send on the link, and when; what they refuse; and what the runner's `sd_op` makes of
them. `examples/sd_data_logger/tests/test_logger.py` runs them against a firmware."""

import re
from pathlib import Path

import pytest

from esp32tl.boards import SdCardRoute
from esp32tl.interfaces import RunEnded, SourceError
from esp32tl.interfaces.sd_card import UNIT, SdCard
from esp32tl.link.boardlink import BoardLink, Interface
from esp32tl.link.protocol import (
    RunnerHello,
    SdCardAck,
    SdCardChange,
    SdClear,
    SdFault,
    SdOp,
    Stop,
    Sync,
    Wait,
)
from esp32tl.report import RunLog
from esp32tl.units import Duration, ms, seconds

HELLO = RunnerHello(1, "esp32", "emulated-fifo-write", "357b0e542dfe97d4c62ec4a15c01c4969164448c")
ROUTE = SdCardRoute(sclk=14, mosi=15, miso=2, cs=13)
MS = 1_000_000


class Clock:
    def __init__(self) -> None:
        self.now = 0


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def card(tmp_path: Path, clock: Clock) -> SdCard:
    """A slot holding a 1 MiB card: blocks 0 to 2047."""
    image = tmp_path / "card.img"
    image.write_bytes(bytes(2 * UNIT))
    slot = SdCard(ROUTE, directory=tmp_path, log=RunLog(), now=lambda: clock.now)
    slot.insert(image)
    return slot


def started(*interfaces: Interface) -> BoardLink:
    link = BoardLink("esp32-devkitc", interfaces)
    link.receive(HELLO)
    return link


def test_rules_go_out_in_the_next_turn_at_the_instant_the_test_gave_them(
    card: SdCard, clock: Clock
) -> None:
    before = card.fail("write", count=1)
    link = started(card)
    # given before power-on: from the first instruction
    assert link.receive(Sync(0)) == [SdFault(0, 1, "write", "error", count=1), Wait(0, None, None)]
    clock.now = 3 * MS
    slow = card.delay("read", ms(50), blocks=range(64, 128))
    erase = card.delay("erase", seconds(1), count=2, blocks=7)
    assert link.receive(Sync(3 * MS)) == [
        SdFault(3 * MS, 2, "read", "delay", 50 * MS, None, (64, 127)),
        SdFault(3 * MS, 3, "erase", "delay", 1000 * MS, 2, (7, 7)),
        Wait(0, None, None),
    ]
    clock.now = 5 * MS
    slow.clear()
    # a rule cleared twice sends nothing more
    slow.clear()
    assert link.receive(Sync(5 * MS)) == [SdClear(5 * MS, (2,)), Wait(0, None, None)]
    clock.now = 6 * MS
    card.clear_faults()
    assert link.receive(Sync(6 * MS)) == [SdClear(6 * MS, None), Wait(0, None, None)]
    assert [f.active for f in card.faults] == [False, False, False]
    assert [f.call for f in card.faults] == [
        "sd_card.fail('write', count=1)",
        "sd_card.delay('read', seconds(0.05), blocks=range(64, 128))",
        "sd_card.delay('erase', seconds(1), count=2, blocks=7)",
    ]
    assert (before.at, slow.at, slow.cleared_at, erase.cleared_at) == (
        Duration(0),
        Duration(3 * MS),
        Duration(5 * MS),
        Duration(6 * MS),
    )
    assert link.error is None


def test_a_spent_rule_is_not_cleared(card: SdCard, clock: Clock) -> None:
    spent = card.fail("write", count=1)
    link = started(card)
    link.receive(Sync(0))
    link.receive(SdOp(MS, 1, "write", (9, 9), "error", None, False))
    clock.now = 2 * MS
    spent.clear()
    card.clear_faults()
    assert link.receive(Sync(2 * MS)) == [Wait(0, None, None)]
    assert spent.cleared_at is None
    assert "cleared" not in card.report()[0]
    # one the link has not told it is spent yet is cleared all the same: the runner takes that
    late = card.fail("write", count=1)
    late.clear()
    assert link.receive(Sync(3 * MS))[-2] == SdClear(3 * MS, (2,))


def test_what_a_rule_did_reaches_the_rule_and_the_slot(card: SdCard) -> None:
    fault = card.fail("write", count=2)
    link = started(card)
    link.receive(Sync(0))
    link.receive(SdOp(2 * MS, 1, "write", (541, 541), "error", None, False))
    assert fault.active, "one of two"
    link.receive(SdOp(3 * MS, 1, "write", (509, 509), "error", None, False))
    assert not fault.active, "spent"
    (first, second) = fault.hits
    assert card.faulted == (first, second)
    assert (first.at, first.op, first.blocks, first.action, first.delay, first.written) == (
        Duration(2 * MS),
        "write",
        range(541, 542),
        "error",
        None,
        False,
    )
    assert str(second) == "write of block 509 failed, not written"
    assert card.report() == [
        "  fault 1: sd_card.fail('write', count=2) at 0 s; 2 operations",
        "    0.002 s  write of block 541 failed, not written",
        "    0.003 s  write of block 509 failed, not written",
    ]
    assert card.properties()[0]["operations"] == [
        {
            "t_ns": 2 * MS,
            "op": "write",
            "blocks": [541, 541],
            "action": "error",
            "delay_ns": None,
            "written": False,
        },
        {
            "t_ns": 3 * MS,
            "op": "write",
            "blocks": [509, 509],
            "action": "error",
            "delay_ns": None,
            "written": False,
        },
    ]
    delayed = card.delay("erase", ms(250))
    link.receive(Sync(4 * MS))
    link.receive(SdOp(5 * MS, 2, "erase", (64, 127), "delay", 250 * MS, True))
    assert str(delayed.hits[0]) == "erase of blocks 64 to 127 delayed 0.25 s, written"


@pytest.mark.parametrize(
    ("give", "error", "message"),
    [
        (
            lambda c: c.fail("erase"),
            ValueError,
            "sd_card.fail: op 'erase' is not 'read' or 'write'",
        ),
        (
            lambda c: c.delay("trim", ms(1)),
            ValueError,
            "sd_card.delay: op 'trim' is not 'read', 'write' or 'erase'",
        ),
        (lambda c: c.delay("read", 5), TypeError, "sd_card.delay: by is 5, not a Duration"),
        (
            lambda c: c.delay("read", Duration(0)),
            ValueError,
            "sd_card.delay: a delay of 0 s; one is 1 ns to 10 s",
        ),
        (
            lambda c: c.delay("write", seconds(10.5)),
            ValueError,
            "sd_card.delay: a delay of 10.5 s; one is 1 ns to 10 s",
        ),
        (lambda c: c.fail("read", count=0), ValueError, "sd_card.fail: a count of 0"),
        (lambda c: c.fail("read", count=True), TypeError, "sd_card.fail: count is True"),
        (
            lambda c: c.fail("read", count=2**63),
            ValueError,
            f"sd_card.fail: a count of {2**63}; it is 1 to 2**63 - 1, or None",
        ),
        (
            lambda c: c.fail("read", blocks=2048),
            ValueError,
            "sd_card.fail: blocks range(2048, 2049), and the card has blocks 0 to 2047",
        ),
        (
            lambda c: c.fail("read", blocks=range(10, 5)),
            ValueError,
            "sd_card.fail: blocks range(10, 5) is not a non-empty range of step 1",
        ),
        (
            lambda c: c.fail("read", blocks=range(0, 8, 2)),
            ValueError,
            "is not a non-empty range of step 1",
        ),
        (
            lambda c: c.fail("read", blocks=[1, 2]),
            TypeError,
            "sd_card.fail: blocks is [1, 2], not a block number or a range",
        ),
    ],
)
def test_a_rule_the_card_cannot_take_is_refused_at_the_call(
    card: SdCard, give: object, error: type[Exception], message: str
) -> None:
    with pytest.raises(error, match=re.escape(message)):
        give(card)  # type: ignore[operator]
    assert card.faults == ()


def test_an_empty_slot_or_a_closed_board_takes_no_rule(tmp_path: Path) -> None:
    slot = SdCard(ROUTE, directory=tmp_path)
    with pytest.raises(SourceError, match=r"sd_card\.fail: the slot is empty: insert a card first"):
        slot.fail("read")
    slot.close()
    with pytest.raises(SourceError, match="the board is closed"):
        slot.delay("read", ms(1))


def test_a_run_that_has_ended_takes_no_rule(tmp_path: Path) -> None:
    image = tmp_path / "card.img"
    image.write_bytes(bytes(2 * UNIT))
    ended = [False]
    slot = SdCard(ROUTE, directory=tmp_path, ended=lambda: ended[0])
    slot.insert(image)
    fault = slot.fail("write")
    ended[0] = True
    with pytest.raises(RunEnded, match=r"sd_card\.fail: the run has ended"):
        slot.fail("read")
    with pytest.raises(RunEnded, match=r"sd_card\.delay: the run has ended"):
        slot.delay("read", ms(1))
    assert slot.faults == (fault,)
    # clearing asks for nothing the run would still have to do
    fault.clear()
    assert not fault.active


def test_rules_and_card_changes_go_out_in_order_and_only_changes_are_acknowledged(
    tmp_path: Path,
) -> None:
    big, small = tmp_path / "big.img", tmp_path / "small.img"
    big.write_bytes(bytes(2 * UNIT))
    small.write_bytes(bytes(UNIT))
    slot = SdCard(ROUTE, directory=tmp_path, now=lambda: MS, started=lambda: True)
    slot.insert(big)
    link = started(slot)
    link.receive(Sync(0))
    assert link.receive(SdCardAck(MS, str(big))) == []
    fault = slot.fail("write", blocks=2047)
    slot.remove()
    with pytest.raises(SourceError, match="the slot is empty"):
        slot.fail("read")
    slot.insert(small)
    # against the card now in the slot: 1024 blocks
    with pytest.raises(ValueError, match="the card has blocks 0 to 1023"):
        slot.fail("read", blocks=2047)
    later = slot.delay("read", ms(1))
    assert len(slot.not_reached) == 2, "the changes, not the rules"
    assert link.receive(Sync(MS)) == [
        SdFault(MS, 1, "write", "error", None, None, (2047, 2047)),
        SdCardChange(MS, None),
        SdCardChange(MS, str(small)),
        SdFault(MS, 2, "read", "delay", MS, None, None),
        Wait(0, None, None),
    ]
    assert len(slot.not_reached) == 2
    link.receive(SdCardAck(2 * MS, None))
    link.receive(SdCardAck(2 * MS, str(small)))
    assert slot.not_reached == []
    assert link.error is None
    # a rule outlives the card it was given on
    assert fault.active and later.active


def test_an_sd_rule_the_runner_would_refuse_fails_the_link_naming_the_slot() -> None:
    class Slot(Interface):
        @property
        def card_slot(self) -> bool:
            return True

        def inputs(self, now: int) -> list[SdFault]:
            return [SdFault(now, 1, "erase", "error")]

    link = started(Slot("sd_card", None))
    assert link.receive(Sync(0)) == [Stop()]
    assert link.error == (
        "sd_card sent a sd_fault the link cannot carry: sd_fault: an erase cannot fail, only be"
        " delayed"
    )


def test_only_the_card_sends_sd_fault_and_sd_op_needs_a_card(card: SdCard) -> None:
    class Impostor(Interface):
        def inputs(self, now: int) -> list[SdFault]:
            return [SdFault(now, 1, "read", "error")]

    link = started(Impostor("lines", None))
    assert link.receive(Sync(0)) == [Stop()]
    assert link.error == "lines sent sd_fault, and is not the SD card slot"

    link = started()
    link.receive(SdOp(0, 1, "read", (0, 0), "error", None, False))
    assert link.error == "the runner sent sd_op, and no interface is the SD card slot"

    with pytest.raises(ValueError, match="one SD card slot, not sd_card, sd_card"):
        BoardLink("esp32-devkitc", [card, card])


def test_the_steps_go_in_the_run_log(card: SdCard, clock: Clock) -> None:
    assert card.log is not None
    clock.now = 2 * MS
    card.fail("read", blocks=range(0, 4)).clear()
    steps = [line for line in card.log.lines() if " test " in line]
    assert steps[-2:] == [
        "  0.002000000 s  test  sd_card.fail('read', blocks=range(0, 4))",
        "  0.002000000 s  test  sd_card.fail('read', blocks=range(0, 4)).clear()",
    ]
