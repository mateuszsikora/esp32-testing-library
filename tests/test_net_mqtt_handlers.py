"""The broker's `on_publish` handlers and the test's delayed publications, on a broker driven
directly: what a handler sees, when it runs, and what becomes of what it raises."""

from collections.abc import Callable

import pytest
from test_clock import Run, S
from test_net_mqtt import CONNECT, DUPLICATE, PUBACK, PUBLISH, SUBSCRIBE

from esp32tl import Registration
from esp32tl.backends.clock import HeldClock
from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.interfaces.console import Console
from esp32tl.net.handlers import handling, refuse_board, refuse_wait
from esp32tl.net.mqtt import TEST_CLIENT, Broker, ConnackCode, Message, _Session
from esp32tl.units import Duration, ms

# A Will on a/b saying "gone", QoS 1 and retained; keepalive 1 s.
WILL_CONNECT = b"\x10\x18\0\x04MQTT\x04\x2e\0\x01\0\x01c\0\x03a/b\0\x04gone"
# QoS 0 PUBLISH on a/c saying "x".
OTHER = b"\x30\x06\0\x03a/cx"


def connected(broker: Broker, connect: bytes = CONNECT) -> tuple[_Session, list[bytes], list[bool]]:
    sent: list[bytes] = []
    closed: list[bool] = []
    session = broker.accept(("10.0.0.2", 100), sent.append, lambda: closed.append(True))
    session.on_data(connect)
    return session, sent, closed


def test_a_matching_publication_calls_the_handler_once_at_its_time_and_another_does_not() -> None:
    now = [20]
    log: list[str] = []
    broker = Broker(lambda: now[0], log.append)
    seen: list[Message] = []
    registration = broker.on_publish("a/+", seen.append)
    assert isinstance(registration, Registration) and registration.active
    assert log == ["mqtt: on_publish 'a/+' registered"]
    session, _, _ = connected(broker)
    now[0] = 30
    session.on_data(PUBLISH)
    assert seen == [broker.messages[0]] and seen[0].at == Duration(30)
    assert seen[0].client == "c" and seen[0].text == "new"
    session.on_data(b"\x30\x06\0\x03b/cx")
    assert len(broker.messages) == 2 and len(seen) == 1


def test_the_test_s_own_publications_never_reach_a_handler() -> None:
    now = [0]
    broker = Broker(lambda: now[0])
    seen: list[Message] = []
    broker.on_publish("#", seen.append)
    connected(broker)
    broker.publish("a/b", "now")
    broker.publish("a/b", "later", after=ms(5))
    now[0] = 5_000_000
    broker.poll()
    assert [m.text for m in broker.messages] == ["now", "later"]
    assert all(m.client == TEST_CLIENT for m in broker.messages) and seen == []


def test_a_will_and_a_dup_retransmission_reach_a_handler() -> None:
    now = [0]
    broker = Broker(lambda: now[0])
    broker.puback = "hold"
    seen: list[Message] = []
    broker.on_publish("a/b", seen.append)
    session, _, _ = connected(broker, WILL_CONNECT)
    session.on_data(PUBLISH + DUPLICATE)
    assert [(m.dup, m.will) for m in seen] == [(False, False), (True, False)]
    now[0] = 7
    session.on_close()
    assert [(m.dup, m.will) for m in seen] == [(False, False), (True, False), (False, True)]
    assert seen[-1].at == Duration(7) and seen[-1].text == "gone"


def test_a_will_on_a_test_s_disconnect_reaches_a_handler_and_its_error_the_test() -> None:
    broker = Broker(lambda: 0)

    def fails(message: Message) -> None:
        raise ValueError(f"no answer to {message.text}")

    broker.on_publish("a/b", fails)
    connected(broker, WILL_CONNECT)
    with pytest.raises(ValueError, match="no answer to gone"):
        broker.disconnect("c")
    assert broker.messages[-1].will and broker.ended(1) is not None


def test_with_immediate_the_puback_is_written_before_the_answer() -> None:
    broker = Broker(lambda: 0)
    session, sent, _ = connected(broker, CONNECT + SUBSCRIBE)

    def answer(message: Message) -> None:
        assert sent[-1] == PUBACK
        broker.publish("a/reply", b"ok " + message.payload)

    broker.on_publish("a/b", answer)
    session.on_data(PUBLISH)
    # The copy to the client's own subscription, the PUBACK, then the answer.
    assert sent[-3][0] == 0x32 and sent[-2] == PUBACK and sent[-1] == b"\x30\x0f\0\x07a/replyok new"


@pytest.mark.parametrize("policy", ["hold", "delay"])
def test_with_a_held_or_delayed_puback_the_handler_still_runs_on_receipt(policy: str) -> None:
    broker = Broker(lambda: 0)
    broker.puback = "hold" if policy == "hold" else ms(10)
    session, sent, _ = connected(broker)
    seen: list[Message] = []
    broker.on_publish("a/b", seen.append)
    session.on_data(PUBLISH)
    assert len(seen) == 1 and PUBACK not in sent and seen[0].publication is broker.pending[0]


def test_two_handlers_are_called_in_the_order_given_and_remove_takes_one_away() -> None:
    log: list[str] = []
    broker = Broker(lambda: 0, log.append)
    calls: list[str] = []
    first = broker.on_publish("a/#", lambda message: calls.append("first"))
    broker.on_publish("a/b", lambda message: calls.append("second"))
    session, _, _ = connected(broker)
    session.on_data(PUBLISH)
    assert calls == ["first", "second"]
    first.remove()
    first.remove()
    assert not first.active and log.count("mqtt: on_publish 'a/#' removed") == 1
    assert repr(first) == "<on_publish 'a/#', removed>"
    session.on_data(PUBLISH)
    assert calls == ["first", "second", "second"]


def test_a_handler_removed_by_an_earlier_one_is_not_called_for_the_same_message() -> None:
    broker = Broker(lambda: 0)
    calls: list[str] = []
    second: list[Registration] = []
    broker.on_publish("a/b", lambda message: second[0].remove())
    second.append(broker.on_publish("a/b", lambda message: calls.append("second")))
    session, _, _ = connected(broker)
    session.on_data(PUBLISH)
    assert calls == []


def test_a_handler_may_disconnect_the_client_and_publish_from_inside() -> None:
    broker = Broker(lambda: 0)
    session, sent, closed = connected(broker, CONNECT + SUBSCRIBE)

    def answer(message: Message) -> None:
        broker.publish("a/x", "bye")
        assert broker.disconnect("c")

    broker.on_publish("a/b", answer)
    session.on_data(PUBLISH)
    assert sent[-1] == b"\x30\x08\0\x03a/xbye" and closed == [True]
    assert broker.ended(1) is not None and broker.ended(1).by == "test"  # type: ignore[union-attr]


def test_after_routes_a_publication_later_through_poll_and_quiet_until() -> None:
    now = [100]
    log: list[str] = []
    broker = Broker(lambda: now[0], log.append)
    session, sent, _ = connected(broker, CONNECT + SUBSCRIBE)
    deadline = broker.quiet_until()

    def answer(message: Message) -> None:
        broker.publish("a/reply", "late", after=ms(50))

    broker.on_publish("a/b", answer)
    session.on_data(PUBLISH)
    due = 100 + 50_000_000
    assert broker.quiet_until() == due < deadline  # type: ignore[operator]
    assert [m.topic for m in broker.messages] == ["a/b"]
    assert log[-1] == "mqtt: (test) publishes on a/reply at 0.0500001 s, after 0.05 s"
    now[0] = due - 1
    broker.poll()
    assert len(broker.messages) == 1
    now[0] = due
    broker.poll()
    reply = broker.messages[-1]
    assert (reply.topic, reply.text, reply.at, reply.client) == (
        "a/reply",
        "late",
        Duration(due),
        TEST_CLIENT,
    )
    assert sent[-1] == b"\x30\x0d\0\x07a/replylate" and broker.quiet_until() == deadline


def test_a_delayed_publication_survives_a_disconnect_and_goes_in_the_order_made() -> None:
    now = [0]
    broker = Broker(lambda: now[0])
    connected(broker)
    broker.publish("a/b", "second", after=ms(2))
    broker.publish("a/b", "first", after=ms(1))
    broker.publish("a/b", "third", after=ms(2), retain=True, qos=1)
    broker.publish("a/b", "zero", after=ms(0))
    assert [m.text for m in broker.messages] == ["zero"]
    broker.disconnect("c")
    now[0] = 2_000_000
    broker.poll()
    assert [m.text for m in broker.messages] == ["zero", "first", "second", "third"]
    assert all(m.at == ms(2) for m in broker.messages[1:])
    assert broker.retained["a/b"].text == "third" and broker.retained["a/b"].qos == 1


@pytest.mark.parametrize(
    ("after", "error", "message"),
    [
        (
            Duration(-1),
            ValueError,
            "mqtt: publish: after is -0.000000001 s; it takes a Duration of 0 or more",
        ),
        (50, TypeError, "mqtt: publish: after is 50, not a Duration, as ms(50)"),
    ],
)
def test_after_refuses_a_negative_duration_and_anything_else(
    after: object, error: type[Exception], message: str
) -> None:
    broker = Broker(lambda: 0)
    with pytest.raises(error) as raised:
        broker.publish("a/b", "x", after=after)  # type: ignore[arg-type]
    assert str(raised.value) == message and broker.quiet_until() is None


def test_on_publish_refuses_a_bad_filter_and_a_handler_that_is_not_callable() -> None:
    broker = Broker(lambda: 0)
    for bad in ("", "a/#/b", "a+", 3):
        with pytest.raises(ValueError, match="is not an MQTT topic filter"):
            broker.on_publish(bad, lambda message: None)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="mqtt: on_publish: the handler 'x' is not callable"):
        broker.on_publish("a/b", "x")  # type: ignore[arg-type]


def test_an_exception_reaches_the_test_and_leaves_the_connection_up() -> None:
    log: list[str] = []
    broker = Broker(lambda: 30, log.append)
    session, sent, closed = connected(broker)
    later: list[Message] = []

    def fails(message: Message) -> None:
        raise ValueError("bad request")

    broker.on_publish("a/b", fails)
    broker.on_publish("a/b", later.append)
    with pytest.raises(ValueError, match="bad request") as raised:
        session.on_data(PUBLISH)
    assert raised.value.__notes__ == [
        "raised by the broker's on_publish handler for 'a/b', given c's message on 'a/b' at"
        " 0.00000003 s of the board's time"
    ]
    # The rest of the event went on: the PUBACK, the next handler, and the connection.
    assert PUBACK in sent and len(later) == 1 and closed == []
    assert "mqtt: on_publish 'a/b' raised ValueError: bad request" in log
    state = broker.session("c")
    assert state is not None and state.connected
    session.on_data(b"\xc0\0")
    assert sent[-1] == b"\xd0\0"


def test_on_a_network_what_a_handler_raises_goes_to_test_error() -> None:
    errors: list[BaseException] = []
    broker = Broker(lambda: 0, test_error=errors.append)
    session, _, _ = connected(broker)

    def fails(message: Message) -> None:
        raise KeyError(message.topic)

    broker.on_publish("a/b", fails)
    session.on_data(PUBLISH + PUBLISH)
    assert [type(error) for error in errors] == [KeyError, KeyError]


def test_a_wait_on_the_board_from_a_handler_or_a_policy_raises_naming_it() -> None:
    errors: list[BaseException] = []
    broker = Broker(lambda: 0, test_error=errors.append)
    session, _, _ = connected(broker)
    broker.on_publish("a/+", lambda message: refuse_wait("board.wait_until"))
    session.on_data(PUBLISH)
    (error,) = errors
    assert isinstance(error, RuntimeError) and str(error) == (
        "board.wait_until: called from the broker's on_publish handler for 'a/+':"
        " a handler must not wait"
    )
    refuse_wait("board.wait")  # outside a handler, nothing to refuse

    def policy(connect: object) -> ConnackCode:
        refuse_wait("board.wait")
        return ConnackCode.ACCEPTED

    broker.connection_policy = policy
    with pytest.raises(RuntimeError) as raised:
        connected(broker)
    assert str(raised.value) == (
        "board.wait: called from the broker's connection_policy: a policy must not wait"
    )


def test_handling_marks_this_context_alone_and_nests() -> None:
    with handling("outer"):
        with (
            handling("inner", "model"),
            pytest.raises(RuntimeError, match="called from inner: a model must not wait"),
        ):
            refuse_wait("board.wait")
        with pytest.raises(RuntimeError, match="called from outer: a handler must not wait"):
            refuse_wait("board.wait")
    refuse_wait("board.wait")


@pytest.mark.parametrize(
    ("wait", "how"), [("wait", "raise"), ("wait_until", "raise"), ("wait", "pytest.fail")]
)
def test_on_the_held_clock_what_a_handler_raises_fails_the_test_s_wait_at_the_next_turn(
    wait: str, how: str
) -> None:
    clock = HeldClock()
    broker = Broker(lambda: clock.now, test_error=clock.fail)
    session = broker.accept(("10.64.0.2", 4000), lambda data: None, lambda: None)

    def fails(message: Message) -> None:
        if how == "pytest.fail":
            # A BaseException: the link's thread must not die of it.
            pytest.fail(f"cannot answer {message.text}")
        raise ValueError(f"cannot answer {message.text}")

    broker.on_publish("a/b", fails)

    class MqttRun(Run):
        def _print(self, until: int) -> None:
            if not broker.clients:
                # In the link's thread, while the run goes to the end of the test's wait.
                session.on_data(CONNECT + b"\x30\x09\0\x03a/bdata")

    run = MqttRun(clock=clock)
    try:
        expected = pytest.fail.Exception if how == "pytest.fail" else ValueError
        with pytest.raises(expected, match="cannot answer data"):
            if wait == "wait":
                run.clock.wait(S)
            else:
                run.clock.wait_until(lambda: None, S, "nothing")
        # Raised at the first turn after it, whatever the wait asks; the run goes on from there.
        at = run.clock.now
        assert at == (S if wait == "wait" else 100_000_000)
        state = broker.session("c")
        assert state is not None and state.connected
        run.clock.wait(S)
        assert run.clock.now == at + S
    finally:
        run.clock.stop()
        run.runner.join(5)


def test_pytest_fail_and_skip_in_a_handler_reach_the_test_and_the_rest_of_the_event_goes_on() -> (
    None
):
    broker = Broker(lambda: 0)
    session, sent, closed = connected(broker)
    broker.on_publish("a/b", lambda message: pytest.skip("not today"))
    broker.on_publish("a/b", lambda message: pytest.fail("no answer"))
    # The first one the handlers raised; both were kept, and the PUBACK went.
    with pytest.raises(pytest.skip.Exception, match="not today"):
        session.on_data(PUBLISH)
    assert PUBACK in sent and closed == []


@pytest.mark.parametrize(
    ("call", "refused"),
    [
        ("board.wait", lambda board: board.wait(ms(1))),
        ("board.wait_until", lambda board: board.wait_until(lambda: None, within=ms(1))),
        ("board.wait_for_wake", lambda board: board.wait_for_wake(within=ms(1))),
        ("board.wait_on_clock", lambda board: board.wait_on_clock(lambda: None, ms(1), "x")),
        ("board.flash", lambda board: board.flash()),
        ("board.nvs", lambda board: board.nvs({"ns": {}})),
        ("board.reset", lambda board: board.reset()),
        ("board.power_on", lambda board: board.power_on()),
        ("board.power_off", lambda board: board.power_off()),
        ("console.expect", lambda board: Console.__new__(Console).expect("x")),
        ("console.expect_exact", lambda board: Console.__new__(Console).expect_exact("x")),
    ],
)
def test_every_call_on_the_board_from_a_handler_is_refused_before_it_touches_the_board(
    call: str, refused: Callable[[Esp32Sim], object]
) -> None:
    # Never set up: a call that got past its guard would fail on a missing attribute.
    board = Esp32Sim.__new__(Esp32Sim)
    rule = "must not wait" if "wait" in call or "expect" in call else "must not call the board"
    with (
        handling("the broker's on_publish handler for 'a/b'"),
        pytest.raises(RuntimeError) as raised,
    ):
        refused(board)
    assert str(raised.value) == (
        f"{call}: called from the broker's on_publish handler for 'a/b': a handler {rule}"
    )
    refuse_board(call)  # outside a handler, nothing to refuse
