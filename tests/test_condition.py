"""A described condition (`esp32tl/interfaces/condition.py`), exported as `esp32tl.condition`."""

from types import SimpleNamespace

import pytest
from conftest import CountingList

import esp32tl
from esp32tl import Condition, condition
from esp32tl.interfaces.board import DEEPSLEEP_RESET, POWERON_RESET, Board, Boot, UptimeZero
from esp32tl.interfaces.condition import FirstFrom, waiting
from esp32tl.units import Duration


def test_the_package_exports_the_constructor_and_its_type() -> None:
    assert esp32tl.condition is condition
    assert isinstance(condition(lambda: None, "an event"), Condition)


def test_a_call_asks_find_and_the_repr_is_the_description() -> None:
    found: list[str] = []
    made = condition(lambda: found[0] if found else None, "a trip/end from the device")
    assert made() is None
    found.append("trip")
    assert made() == "trip"
    assert repr(made) == "a trip/end from the device"
    assert not hasattr(made, "__qualname__")


def test_bool_raises_before_and_after_a_match_naming_the_description() -> None:
    found: list[int] = []
    made = condition(lambda: found[0] if found else None, "a trip/end from the device")
    message = (
        r"call the condition 'a trip/end from the device' with \(\), or pass it to"
        r" board.wait_until"
    )
    with pytest.raises(TypeError, match=message):
        bool(made)
    found.append(1)
    assert made() == 1
    with pytest.raises(TypeError, match=message):
        assert made


def test_a_service_names_the_condition_in_the_type_error() -> None:
    made = condition(lambda: None, "the next MQTT message", service="MQTT")
    assert repr(made) == "the next MQTT message"
    with pytest.raises(
        TypeError, match=r"^call the MQTT condition with \(\), or pass it to board.wait_until$"
    ):
        bool(made)


def test_the_condition_is_frozen() -> None:
    made = condition(lambda: None, "an event")
    with pytest.raises(AttributeError):
        made.description = "another"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("find", "description", "service", "message"),
    [
        (None, "an event", None, "condition: find is None, not a function of no arguments"),
        (lambda: None, "", None, "condition: description is '', not text naming the event"),
        (lambda: None, b"x", None, "condition: description is b'x', not text naming the event"),
        (lambda: None, "an event", "", "condition: service is '', not text naming it"),
    ],
)
def test_what_is_not_a_condition_is_refused(
    find: object, description: object, service: object, message: str
) -> None:
    with pytest.raises(TypeError, match=f"^{message}$"):
        condition(find, description, service=service)  # type: ignore[arg-type]


def test_waiting_says_why_only_for_a_condition_that_has_it() -> None:
    reasons = ["the SYN at 1 s got no answer", None]
    said = condition(lambda: None, "an event", waiting=lambda: reasons.pop(0))
    assert said.waiting is not None
    assert waiting(said) == "the SYN at 1 s got no answer" and waiting(said) is None
    assert waiting(condition(lambda: None, "an event")) is None
    # Any other callable says nothing, whatever attribute it has.
    assert waiting(SimpleNamespace(waiting=lambda: "no")) is None
    with pytest.raises(TypeError, match=r"^condition: waiting is 'no', not a function of no"):
        condition(lambda: None, "an event", waiting="no")  # type: ignore[arg-type]


def test_first_from_reads_each_record_once_and_keeps_its_match() -> None:
    # A poll after each of N records: a scan from `start` at every poll would read N * (N + 1) / 2.
    n = 10_000
    history = CountingList[int]([-1, 7])
    asked: list[int] = []

    def wanted(record: int) -> bool:
        asked.append(record)
        return record == 7

    find = FirstFrom(lambda: history, 2, wanted)
    for record in range(n):
        history.append(record % 7)
        assert find() is None
    # The records before `start`, a 7 among them, are never read.
    assert history.reads == n and len(asked) == n
    history.append(7)
    history.append(7)
    assert find() == 7 and history.reads == n + 1
    # Found, later polls read that record again and ask nothing.
    for _ in range(3):
        assert find() == 7
    assert history.reads == n + 4 and len(asked) == n + 1
    assert FirstFrom(lambda: history, len(history), wanted)() is None


def test_first_from_reads_the_match_from_the_history_at_each_poll() -> None:
    # A history built anew at each call, as `boots` is, whose records gain what compares equal.
    first = Boot(1, Duration(0), POWERON_RESET)
    boots: list[Boot] = [first]
    find = FirstFrom(lambda: tuple(boots), 1, lambda boot: boot.cause == DEEPSLEEP_RESET)
    assert find() is None
    boots.append(Boot(2, Duration(5), POWERON_RESET))
    boots.append(Boot(3, Duration(9), DEEPSLEEP_RESET))
    asleep = find()
    assert asleep is boots[2] and asleep.uptime_zero is None
    zero = UptimeZero(Duration(9), Duration(10))
    boots[2] = Boot(3, Duration(9), DEEPSLEEP_RESET, zero)
    again = find()
    assert again == asleep and again is not None and again.uptime_zero == zero


def test_next_boot_reads_only_the_boots_since_the_last_poll() -> None:
    boots = CountingList[Boot]([Boot(1, Duration(0), POWERON_RESET)])
    board = SimpleNamespace(boots=boots)
    woke = Board.next_boot(board, DEEPSLEEP_RESET)  # type: ignore[arg-type]
    for number in range(2, 1002):
        boots.append(Boot(number, Duration(number), POWERON_RESET))
        assert woke() is None
    assert boots.reads == 1000
    boots.append(Boot(1002, Duration(1002), DEEPSLEEP_RESET))
    assert woke() is boots[-1] and woke() == Boot(1002, Duration(1002), DEEPSLEEP_RESET)
    assert boots.reads == 1000 + 1 + 1 + 1
