"""A described condition (`esp32tl/interfaces/condition.py`), exported as `esp32tl.condition`."""

import pytest

import esp32tl
from esp32tl import Condition, condition


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
