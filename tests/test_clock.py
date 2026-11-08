"""The test's clock and the console on it (`esp32tl/backends/clock.py`,
`esp32tl/interfaces/console.py`), without an emulator: a runner thread feeds `BoardLink` the
messages a runner would, taking each turn at the block point the harness's `wait` gives, and the
test waits on the clock from its own thread."""

import re
import struct
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TypeVar

import pexpect
import pytest
from test_net_ip import packet

from esp32tl import boards, recording
from esp32tl.backends.clock import HeldClock, nanoseconds, seconds
from esp32tl.interfaces import NeverHappened, RunEnded
from esp32tl.interfaces.console import Console, ConsoleLines, Line
from esp32tl.interfaces.lines import Lines
from esp32tl.interfaces.modem import Modem as ModemRoute
from esp32tl.interfaces.wifi import Wifi
from esp32tl.link.boardlink import BoardLink, Interface, RunTo, Wire
from esp32tl.link.protocol import Console as ConsoleMessage
from esp32tl.link.protocol import FlashDump, FlashDumpAck, Reset, RunnerHello, Stop, Sync, Wait
from esp32tl.net.ethernet import GATEWAY_MAC
from esp32tl.net.mqtt import Broker, Publication
from esp32tl.units import Duration, minutes, ms
from esp32tl.units import seconds as secs

HELLO = RunnerHello(1, "esp32", "emulated-fifo-write", "0" * 40)
MS = 1_000_000
S = 1_000_000_000
LILYGO = boards.profile("lilygo-t-a7670g-r2")

T = TypeVar("T")


class Run:
    """A run on the test's clock: `console`, `(t, bytes)` printed at their times, `resets`,
    `(t, cause)`, the chip's resets, each an event, and a runner thread that ends the run at
    `end` ns, as --max-seconds does, then ends the clock as the backend does once the runner has
    exited, with the end's time when `stats` says it. `syncs` are the block points it took turns
    at, and `latencies` the `latency` of each `wait`."""

    def __init__(
        self,
        console: Sequence[tuple[int, bytes]] = (),
        end: int = 3600 * S,
        stats: bool = True,
        *,
        clock: HeldClock | None = None,
        interfaces: Sequence[Interface] = (),
        resets: Sequence[tuple[int, int]] = (),
    ) -> None:
        self.clock = clock or HeldClock()
        self.console_output = bytearray()
        self.console_lines = ConsoleLines()
        self.link = BoardLink("esp32-devkitc", interfaces, hold=self.clock, console=self._console)
        self.pending = list(console)
        self.resets = list(resets)
        self.seq = 0
        self.latencies: list[int | None] = []
        self.end = end
        self.stats = stats
        self.syncs: list[int] = []
        # The turns the runner answered a flash dump in, and what it answers.
        self.dumps: list[int] = []
        self.dump_error: str | None = None
        self.stopped = False
        self.runner = threading.Thread(target=self._run, daemon=True)
        self.runner.start()

    def _run(self) -> None:
        self.link.receive(HELLO)
        t = 0
        while True:
            self.syncs.append(t)
            (reply,) = self.link.receive(Sync(t))
            while isinstance(reply, FlashDump):
                self.dumps.append(t)
                (reply,) = self.link.receive(FlashDumpAck(t, self.dump_error))
            if reply == Stop():
                self.stopped = True
                break
            assert isinstance(reply, Wait)
            self.latencies.append(reply.latency)
            bound = reply.quiet_until if reply.quiet_until is not None else self.end
            # A reset is an event: the block point is at most `latency` after it (rule 1).
            if self.resets and reply.latency is not None:
                bound = min(bound, self.resets[0][0] + reply.latency)
            if bound >= self.end:
                self._print(self.end)
                break
            self._print(bound)
            t = bound
        self.clock.end("the runner exited with code 0", self.end if self.stats else None)

    def _print(self, until: int) -> None:
        while self.pending and self.pending[0][0] <= until:
            t, data = self.pending.pop(0)
            while self.resets and self.resets[0][0] <= t:
                self._reset()
            self.link.receive(ConsoleMessage(t, data))
        while self.resets and self.resets[0][0] <= until:
            self._reset()

    def _reset(self) -> None:
        t, cause = self.resets.pop(0)
        self.seq += 1
        self.link.receive(Reset(self.seq, t, cause))

    # What the console needs of the board.

    def _console(self, t: int, data: bytes) -> None:
        self.console_output.extend(data)
        self.console_lines.feed(t, data)

    def wait_on_clock(
        self, condition: Callable[[], T | None], within: Duration, description: str
    ) -> T:
        return self.clock.wait_until(condition, nanoseconds(within, "within"), description)


def test_the_run_stands_still_until_the_test_waits() -> None:
    run = Run()
    # The link holds turn 0, before the guest's first instruction, for as long as the test does
    # not wait.
    run.runner.join(0.2)
    assert run.syncs == [0] and run.clock.now == 0
    run.clock.wait(2 * S)
    # One turn at the end of the wait, which nothing else bounds.
    assert run.syncs == [0, 2 * S] and run.clock.now == 2 * S
    run.clock.wait(500 * MS)
    assert run.clock.now == 2500 * MS


def test_wait_until_asks_at_every_turn_and_returns_what_the_condition_returned() -> None:
    run = Run(console=[(1234 * MS, b"ready\n")])
    asked: list[int] = []

    def ready() -> str | None:
        asked.append(run.clock.now)
        return "ready" if b"ready" in run.console_output else None

    assert run.clock.wait_until(ready, 10 * S, "ready") == "ready"
    # In steps of 100 ms: the line printed at 1.234 s is seen at the turn at 1.3 s.
    assert asked == [i * 100 * MS for i in range(14)]
    assert run.clock.now == 1300 * MS


def test_a_condition_that_holds_at_once_lets_no_time_pass() -> None:
    run = Run()
    assert run.clock.wait_until(lambda: 42, 10 * S, "the answer") == 42
    assert run.syncs == [0] and run.clock.now == 0


def test_mqtt_immediate_response_is_visible_after_waiting_for_connection() -> None:
    broker = Broker(lambda: 100 * MS)
    session = broker.accept(("10.64.0.2", 4000), lambda data: None, lambda: None)

    class MqttRun(Run):
        def _print(self, until: int) -> None:
            if not broker.clients:
                # Both events occur in one runner slice, before the test regains control.
                session.on_data(b"\x10\x0d\0\x04MQTT\x04\x02\0\x0a\0\x01c\x30\x09\0\x03a/bfast")

    run = MqttRun()
    connected, response = broker.next_connection(), broker.next_message("a/b", "fast")
    try:
        assert run.clock.wait_until(connected, S, repr(connected)) is broker.clients[0]
        at = run.clock.now
        message = run.clock.wait_until(response, S, repr(response))
        assert message is broker.find_message("a/b", "fast")
        assert run.clock.now == at == 100 * MS
        assert response() is message  # Waiting did not consume the match.
        next_response = broker.next_message("a/b", "fast")
        with pytest.raises(NeverHappened) as raised:
            run.clock.wait_until(next_response, 100 * MS, repr(next_response))
        assert "the next MQTT message on 'a/b' saying b'fast' did not happen" in str(raised.value)
    finally:
        run.clock.stop()
        run.runner.join(5)


def datagram_frame() -> bytes:
    """A UDP datagram to port 7000, in an Ethernet frame from the station, which ends in libpp's
    guard word as esp32sim hands it over (protocol 5.15)."""
    station = bytes.fromhex("246f28001122")
    datagram = packet(17, struct.pack(">HHHH", 5000, 7000, 8, 0))
    return GATEWAY_MAC + station + b"\x08\x00" + datagram + b"\xef\xbe\xad\xde"


def a_datagram_on_the_path() -> tuple[Run, list[int]]:
    """A run on the test's clock, with one datagram on its way up a path, due at 110 ms; and the
    times the network took datagrams at."""
    clock = HeldClock()
    wifi = Wifi(now=lambda: clock.now, asked=lambda request: None)
    network = wifi.access_point("myssid", password="mypassword")
    taken: list[int] = []

    class Stamped:
        def on_datagram(self, data: bytes) -> bytes | None:
            taken.append(network.now())
            return None

    network.ip.udp[7000] = Stamped()
    network.set_path(rtt=ms(201))  # 100 ms up
    assert network.receive(10 * MS, datagram_frame()) == []
    return Run(clock=clock, interfaces=[wifi]), taken


def test_a_wait_that_ends_at_a_datagrams_time_finds_it_taken() -> None:
    run, taken = a_datagram_on_the_path()
    try:
        run.clock.wait(110 * MS - 1)
        assert taken == []
        run.clock.wait(1)
        assert run.clock.now == 110 * MS
        assert taken == [110 * MS]
    finally:
        run.clock.stop()
        run.runner.join(5)


def test_a_wait_until_returns_at_the_datagrams_time() -> None:
    run, taken = a_datagram_on_the_path()
    try:
        assert run.clock.wait_until(lambda: taken, 110 * MS, "the datagram") == [110 * MS]
        assert run.clock.now == 110 * MS
    finally:
        run.clock.stop()
        run.runner.join(5)


# Keepalive 10 s, so it expires at 15 s, and a Will on a/w; then a QoS 1 PUBLISH on a/b.
CONNECT_AND_PUBLISH = (
    b"\x10\x16\0\x04MQTT\x04\x06\0\x0a\0\x01c\0\x03a/w\0\x02by" + b"\x32\x0a\0\x03a/b\0\x09new"
)


def a_broker_with_deadlines(
    puback: Duration, behind: str, tmp_path: Path
) -> tuple[Run, Broker, Publication]:
    """A run on the test's clock, with a client connected at 0 whose keepalive expires at 15 s, and
    a publication of it whose PUBACK is due `puback` later; on the network `behind` a Wi-Fi access
    point or a modem in data mode."""
    clock = HeldClock()
    interface: Interface
    if behind == "wifi":
        wifi = Wifi(now=lambda: clock.now, asked=lambda request: None)
        network = wifi.access_point("myssid", password="mypassword")
        interface = wifi
    else:
        modem = ModemRoute(
            "uart.modem",
            Wire(tx=26, rx=27, baud=115200),
            clock=recording.EMULATED,
            directory=tmp_path,
            now=lambda: clock.now,
            lines=Lines(LILYGO.name, LILYGO.lines, lambda: clock.now),
            pins=LILYGO.lines["modem"],
        )
        network = modem.network()
        model = modem.modem_model()
        assert b"CONNECT" in b"".join(data for _, data in model.receive(0, b"ATD*99#\r\n"))
        assert model.data_mode
        # PPP's first LCP request: taken here, so that the runner's turns carry no input.
        assert list(model.poll(0))
        interface = modem
    broker = network.broker
    broker.puback = puback
    session = broker.accept(("10.0.0.2", 4000), lambda data: None, lambda: None)
    session.on_data(CONNECT_AND_PUBLISH)
    [message] = broker.messages
    assert message.publication is not None
    return Run(clock=clock, interfaces=[interface]), broker, message.publication


# Both ways a test's network reaches the board, as `Wifi.arrive` and `ModemModel.arrive` pass it on.
BEHIND = pytest.mark.parametrize("behind", ["wifi", "modem"])


@BEHIND
def test_a_wait_that_ends_at_a_puback_deadline_finds_it_emitted(
    behind: str, tmp_path: Path
) -> None:
    run, _, publication = a_broker_with_deadlines(ms(100), behind, tmp_path)
    try:
        run.clock.wait(100 * MS - 1)
        assert (publication.status, publication.puback_at) == ("pending", None)
        run.clock.wait(1)
        assert run.clock.now == 100 * MS
        assert publication.status == "emitted" and publication.puback_at == ms(100)
        # The deadline came before what the test does at its instant.
        assert not publication.release()
    finally:
        run.clock.stop()
        run.runner.join(5)


@BEHIND
def test_a_wait_until_returns_at_a_puback_deadline(behind: str, tmp_path: Path) -> None:
    run, _, publication = a_broker_with_deadlines(ms(100), behind, tmp_path)
    try:
        found = run.clock.wait_until(lambda: publication.puback_at, 100 * MS, "the PUBACK")
        assert found == ms(100) and run.clock.now == 100 * MS
    finally:
        run.clock.stop()
        run.runner.join(5)


@BEHIND
def test_a_wait_that_ends_at_keepalive_expiry_finds_the_connection_closed_and_the_will(
    behind: str, tmp_path: Path
) -> None:
    run, broker, _ = a_broker_with_deadlines(ms(100), behind, tmp_path)
    try:
        run.clock.wait(15 * S - 1)
        assert broker.session("c") is not None
        assert broker.find_message("a/w") is None
        run.clock.wait(1)
        assert run.clock.now == 15 * S
        assert broker.session("c") is None  # CleanSession=1: closing discards it
        will = broker.find_message("a/w")
        assert will is not None and will.at == secs(15)
        assert not broker.disconnect("c")
    finally:
        run.clock.stop()
        run.runner.join(5)


@BEHIND
def test_a_wait_until_returns_at_keepalive_expiry(behind: str, tmp_path: Path) -> None:
    run, broker, _ = a_broker_with_deadlines(ms(100), behind, tmp_path)
    try:
        will = broker.next_message("a/w")
        assert run.clock.wait_until(will, 15 * S, repr(will)).at == secs(15)
        assert run.clock.now == 15 * S
    finally:
        run.clock.stop()
        run.runner.join(5)


@BEHIND
def test_keepalive_expiry_wins_a_tie_with_a_puback_deadline_in_a_held_turn(
    behind: str, tmp_path: Path
) -> None:
    run, broker, publication = a_broker_with_deadlines(secs(15), behind, tmp_path)
    try:
        run.clock.wait(15 * S)
        assert broker.session("c") is None
        assert publication.status == "cancelled" and publication.puback_at is None
    finally:
        run.clock.stop()
        run.runner.join(5)


def test_a_service_that_raises_on_a_datagram_from_the_path_names_the_arrival() -> None:
    clock = HeldClock()
    wifi = Wifi(now=lambda: clock.now, asked=lambda request: None)
    network = wifi.access_point("myssid", password="mypassword")

    class Raises:
        def on_datagram(self, data: bytes) -> bytes | None:
            raise RuntimeError("a test's bug")

    network.ip.udp[7000] = Raises()
    network.set_path(rtt=ms(201))
    network.receive(0, datagram_frame())
    run = Run(clock=clock, interfaces=[wifi])
    with pytest.raises(RunEnded):
        run.clock.wait(S)
    assert run.link.error == "wifi.arrive raised RuntimeError: a test's bug"
    run.runner.join(5)


def test_a_wait_for_an_event_takes_a_turn_only_at_the_event() -> None:
    # A deep sleep of an hour: wait_until would look every 100 ms, 36 000 turns.
    run = Run(resets=[(3600 * S, 5)], end=2 * 3600 * S)

    def reset() -> int | None:
        return run.link.resets[0].t if run.link.resets else None

    assert run.clock.wait_for_event(reset, 3 * 3600 * S, "a reset", MS) == 3600 * S
    # Turn 0, then the turn the reset's latency brings, and no other.
    assert run.syncs == [0, 3600 * S + MS] and run.clock.now == 3600 * S + MS
    assert run.latencies == [MS]


def test_the_latency_of_a_wait_for_an_event_lasts_for_that_wait_only() -> None:
    run = Run(resets=[(2 * S, 5)])
    run.clock.wait_for_event(lambda: run.link.resets, 10 * S, "a reset", MS)
    run.clock.wait(S)
    run.clock.wait_until(lambda: run.clock.now >= 4 * S, 10 * S, "4 s")
    assert run.latencies[0] == MS and run.latencies[1:] == [None] * (len(run.latencies) - 1)


def test_a_wait_for_an_event_that_never_comes_raises_never_happened_at_its_end() -> None:
    run = Run()
    with pytest.raises(NeverHappened) as raised:
        run.clock.wait_for_event(lambda: run.link.resets, 600 * S, "a reset", MS)
    assert str(raised.value) == (
        "a reset did not happen within 600 s: waited from 0 s to 600 s of the board's time"
    )
    assert run.syncs == [0, 600 * S]


def test_a_wait_for_an_event_the_run_ends_before_raises_run_ended() -> None:
    run = Run(end=60 * S)
    with pytest.raises(RunEnded, match="the run ended at 60 s while the test waited for a reset"):
        run.clock.wait_for_event(lambda: run.link.resets, 600 * S, "a reset", MS)


def test_a_condition_that_never_holds_raises_never_happened_naming_it_and_the_window() -> None:
    run = Run()
    run.clock.wait(S)
    with pytest.raises(NeverHappened) as raised:
        run.clock.wait_until(lambda: False, 500 * MS, "the condition of the test")
    assert str(raised.value) == (
        "the condition of the test did not happen within 0.5 s: waited from 1 s to 1.5 s of the"
        " board's time"
    )
    assert isinstance(raised.value, AssertionError)
    # The window's end is a turn of its own, not a step past it.
    assert run.clock.now == 1500 * MS


def test_a_condition_that_raises_raises_in_the_test_and_leaves_the_link_held() -> None:
    run = Run()

    def broken() -> bool:
        raise KeyError("the test's own")

    with pytest.raises(KeyError, match="the test's own"):
        run.clock.wait_until(broken, S, "broken")
    assert run.link.error is None
    run.clock.wait(S)
    assert run.clock.now == S


def test_the_end_of_the_run_raises_run_ended_with_the_reason() -> None:
    run = Run(end=2 * S)
    with pytest.raises(RunEnded) as raised:
        run.clock.wait_until(lambda: False, 10 * S, "nothing")
    assert str(raised.value) == (
        "the run ended at 2 s while the test waited for nothing: the runner exited with code 0"
    )
    # A wait after the end raises at once.
    with pytest.raises(RunEnded, match=r"^the run ended at 2 s while the test waited until"):
        run.clock.wait(S)


def test_a_step_on_the_board_after_the_end_of_the_run_raises_run_ended_at_once() -> None:
    run = Run(end=2 * S)
    run.clock.check("board.reset()")
    with pytest.raises(RunEnded):
        run.clock.wait(10 * S)
    with pytest.raises(RunEnded) as raised:
        run.clock.check("board.reset()")
    assert str(raised.value) == (
        "the run ended at 2 s before board.reset(): the runner exited with code 0"
    )


def test_without_stats_run_ended_names_the_last_turn_only() -> None:
    run = Run(end=2 * S, stats=False)
    with pytest.raises(RunEnded, match=r"^the run ended after the turn at 1\.9 s while"):
        run.clock.wait_until(lambda: False, 10 * S, "nothing")


def test_a_condition_the_last_of_the_run_makes_true_still_holds() -> None:
    # Printed after the last turn, before the run ends.
    run = Run(console=[(1950 * MS, b"bye\n")], end=2 * S)
    assert run.clock.wait_until(lambda: b"bye" in run.console_output, 10 * S, "bye")


def test_a_run_that_ends_before_its_first_turn_raises_run_ended() -> None:
    clock = HeldClock()
    clock.end("the runner exited with code 2; its last output:\nno ROM")
    with pytest.raises(RunEnded) as raised:
        clock.wait(S)
    assert str(raised.value) == (
        "the run ended before its first turn while the test waited until 1 s: the runner exited"
        " with code 2; its last output:\nno ROM"
    )


def test_stop_ends_the_held_turn_with_stop() -> None:
    run = Run()
    run.clock.wait(S)
    assert run.clock.stop()
    run.runner.join(5)
    assert run.stopped and run.syncs == [0, S]


def test_the_board_s_durations_are_durations_of_0_or_more() -> None:
    assert [seconds(n) for n in (0, S, 1500 * MS, 1)] == ["0 s", "1 s", "1.5 s", "0.000000001 s"]
    assert nanoseconds(secs(1.5), "board.wait") == 1500 * MS
    assert nanoseconds(secs(0), "board.wait") == 0
    with pytest.raises(ValueError, match=r"^board.wait takes a Duration of 0 or more, not -1 s$"):
        nanoseconds(secs(-1), "board.wait")
    for wrong, example in ((2.5, "seconds(2.5)"), (True, "seconds(5)"), ("1", "seconds(5)")):
        message = f"board.wait takes a Duration, as {example}, not {wrong!r}"
        with pytest.raises(TypeError, match=f"^{re.escape(message)}$"):
            nanoseconds(wrong, "board.wait")  # type: ignore[arg-type]


def console_run(*printed: tuple[float, bytes], end: float = 3600) -> tuple[Run, Console]:
    run = Run(console=[(round(t * S), data) for t, data in printed], end=round(end * S))
    return run, Console(run)


def test_expect_finds_a_pattern_and_the_next_expect_searches_after_it() -> None:
    run, console = console_run((1.05, b"fix lat=1\r\n"), (2, b"fix lat=2\r\n"))
    match: Any = console.expect(rb"fix lat=(\d)")
    assert match.group(1) == b"1"
    # Seen at the next turn.
    assert run.clock.now == 1100 * MS
    assert console.expect("fix lat=(\\d)").group(1) == b"2"
    # A compiled text pattern, its flags kept.
    run, console = console_run((1, b"FIX\n"))
    assert console.expect(re.compile("fix", re.IGNORECASE)).group() == b"FIX"


def test_expect_of_a_list_matches_whichever_appears_first() -> None:
    _, console = console_run((1, b"b then a\n"))
    assert console.expect(["a", "b"]).group() == b"b"
    assert console.expect(["a", "b"]).group() == b"a"


def test_a_pattern_matches_across_lines_as_pexpect_s_does() -> None:
    # pexpect compiles text and bytes with re.DOTALL.
    _, console = console_run((1, b"start\nmiddle\nend\n"))
    assert console.expect(r"start.*end", timeout=2).group() == b"start\nmiddle\nend"
    # A compiled pattern keeps its own flags, and has no DOTALL unless it asks.
    _, console = console_run((1, b"start\nend\n"), end=3)
    with pytest.raises(RunEnded):
        console.expect(re.compile("start.*end"))
    # pexpect's buffer starts at the last match, and so does ^.
    _, console = console_run((1, b"one two\n"))
    console.expect("one ")
    assert console.expect("^two").group() == b"two"


def test_expect_exact_takes_the_text_as_it_is() -> None:
    _, console = console_run((1, b"lat=52.229677 (ok)\n"))
    assert console.expect_exact("52.229677 (ok)").group() == b"52.229677 (ok)"
    with pytest.raises(TypeError, match="expect_exact takes text or bytes"):
        console.expect_exact(re.compile("x"))  # type: ignore[arg-type]


def test_return_what_before_match_returns_the_bytes_since_the_last_match() -> None:
    _, console = console_run((1, b"boot\nready\nmore\nready\n"))
    assert console.expect("ready", return_what_before_match=True) == b"boot\n"
    assert console.expect("ready", return_what_before_match=True) == b"\nmore\n"


def test_expect_s_timeout_is_in_seconds_of_the_board_s_time() -> None:
    _, console = console_run((5, b"late\n"))
    with pytest.raises(NeverHappened) as raised:
        console.expect("late", timeout=2)
    assert str(raised.value) == (
        "'late' on the console did not happen within 2 s: waited from 0 s to 2 s of the board's"
        " time"
    )
    # What did not match stays to be found.
    assert console.expect("late", timeout=10).group() == b"late"


def test_expect_eof_returns_what_the_console_printed_after_the_last_match() -> None:
    _, console = console_run((1, b"one\n"), (2, b"two\nthree\n"), end=3)
    console.expect("one")
    assert console.expect(pexpect.EOF) == b"\ntwo\nthree"
    _, console = console_run((1, b"one\n"), end=3)
    assert console.expect(["never", pexpect.EOF], return_what_before_match=True) == b"one\n"


def test_expect_finds_what_the_run_printed_after_it_has_ended() -> None:
    _, console = console_run((1, b"one two\n"), end=2)
    with pytest.raises(RunEnded):
        console.expect("three")
    # The run is over, and what it printed is still there to match.
    assert console.expect("two").group() == b"two"
    with pytest.raises(RunEnded, match="while the test waited for 'three' on the console"):
        console.expect("three")


def test_expect_raises_run_ended_when_the_run_ends_first() -> None:
    _, console = console_run((1, b"one\n"), end=2)
    with pytest.raises(RunEnded) as raised:
        console.expect("two")
    assert str(raised.value) == (
        "the run ended at 2 s while the test waited for 'two' on the console: the runner exited"
        " with code 0"
    )


def test_expect_refuses_what_is_not_a_pattern_or_a_time() -> None:
    _, console = console_run()
    with pytest.raises(ValueError, match="no pattern"):
        console.expect([])
    with pytest.raises(TypeError, match="is not a pattern"):
        console.expect(42)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match=r"^expect: timeout is -1 s, less than 0$"):
        console.expect("x", timeout=-1)
    with pytest.raises(ValueError, match=r"^expect: timeout is -0.5 s, less than 0$"):
        console.expect("x", timeout=secs(-0.5))
    with pytest.raises(ValueError, match="timeout is nan, not a Duration or a number of seconds"):
        console.expect("x", timeout=float("nan"))
    with pytest.raises(TypeError, match="timeout is '1', not a Duration or a number of seconds"):
        console.expect("x", timeout="1")  # type: ignore[arg-type]


def test_expect_s_timeout_may_be_a_duration() -> None:
    _, console = console_run((5, b"late\n"))
    with pytest.raises(NeverHappened, match=r"did not happen within 2\.5 s"):
        console.expect("late", timeout=secs(2.5))
    assert console.expect("late", timeout=minutes(1)).group() == b"late"


def test_console_lines_are_stamped_with_the_time_their_ending_came_at() -> None:
    run, console = console_run(
        (1, b"\x1b[0;32mI (10) boot: one\x1b[0m\r\n"), (2, b"two "), (3, b"halves\nthree")
    )
    run.clock.wait(4 * S)
    # Without colour codes and line endings; "three" has not ended, and is no line yet.
    assert console.lines() == [Line(secs(1), "I (10) boot: one"), Line(secs(3), "two halves")]
    assert console.lines(since=secs(1.5)) == [Line(secs(3), "two halves")]
    assert console.lines(since=secs(3.5)) == []
    (found,) = console.lines(r"(?P<word>\w+) halves")
    assert found.at == secs(3) and found["word"] == "two" and found[1] == "two"
    with pytest.raises(TypeError, match="was found by no pattern, and has no groups"):
        console.lines()[0]["word"]
    with pytest.raises(TypeError, match=r"since is 1\.5, not a Duration"):
        console.lines(since=1.5)  # type: ignore[arg-type]


def test_a_line_condition_finds_the_first_matching_line_from_when_it_was_made() -> None:
    run, console = console_run((1, b"fix 1\n"), (2, b"other\n"), (3, b"fix 3\n"), (4, b"fix 4\n"))
    run.clock.wait(1500 * MS)
    condition = console.line(r"fix (?P<n>\d)")
    assert condition() is None
    line = run.clock.wait_until(condition, 10 * S, repr(condition))
    assert (line.at, line.text, line["n"]) == (secs(3), "fix 3", "3")
    assert run.clock.now == 3 * S
    # Asked again, the same condition looks after the line it found.
    again = run.clock.wait_until(condition, 10 * S, repr(condition))
    assert (again.at, again["n"]) == (secs(4), "4")
    # Lines compare by their time and text, not by their match.
    assert console.lines("fix 4") == console.lines("fix 4") == [Line(secs(4), "fix 4")]
    never = console.line(re.compile("never"))
    with pytest.raises(NeverHappened) as raised:
        run.clock.wait_until(never, S, repr(never))
    assert str(raised.value) == (
        "a console line matching 'never' did not happen within 1 s: waited from 4 s to 5 s of"
        " the board's time"
    )
    with pytest.raises(TypeError, match="is not a regular expression on text"):
        console.line(b"fix")  # type: ignore[arg-type]


def test_a_flash_dump_is_answered_in_the_turn_the_link_is_held_at_and_moves_no_time() -> None:
    run = Run()
    assert run.clock.dump_flash() == FlashDumpAck(0)
    assert run.dumps == [0] and run.syncs == [0]
    run.clock.wait(2 * S)
    syncs = list(run.syncs)
    assert run.clock.dump_flash() == FlashDumpAck(2 * S)
    assert run.clock.dump_flash() == FlashDumpAck(2 * S)
    assert run.dumps == [0, 2 * S, 2 * S]
    assert run.syncs == syncs and run.clock.now == 2 * S
    # the turn goes on: the next wait lets the run go from it
    run.clock.wait(1 * S)
    assert run.clock.now == 3 * S


def test_a_flash_dump_says_what_the_runner_answered() -> None:
    run = Run()
    run.dump_error = "/tmp/flash.bin: no space"
    assert run.clock.dump_flash() == FlashDumpAck(0, "/tmp/flash.bin: no space")


def test_a_flash_dump_once_the_run_has_ended_asks_nothing_and_a_wait_gets_run_ended() -> None:
    run = Run(end=1 * S)
    with pytest.raises(RunEnded):
        run.clock.wait(2 * S)
    assert run.clock.dump_flash() is None
    assert run.dumps == []
    with pytest.raises(RunEnded):
        run.clock.wait(1 * S)


def test_a_flash_dump_the_end_of_the_run_cuts_short_returns() -> None:
    clock = HeldClock()
    answer: list[FlashDumpAck | None] = [FlashDumpAck(99)]
    asking = threading.Thread(target=lambda: answer.__setitem__(0, clock.dump_flash()))
    # before the first turn it waits for one, as `wait` does
    asking.start()
    asking.join(0.1)
    assert asking.is_alive()
    clock.end("the runner exited with code 1")
    asking.join(5)
    assert answer == [None]


def test_the_test_is_over_while_the_runner_holds_a_dump_unanswered() -> None:
    clock = HeldClock()
    results: list[int | RunTo | FlashDump | None] = []
    link = threading.Thread(target=lambda: results.append(clock.turn(0)))
    link.start()
    answer: list[FlashDumpAck | None] = [FlashDumpAck(99)]
    asking = threading.Thread(target=lambda: answer.__setitem__(0, clock.dump_flash()))
    asking.start()
    link.join(5)
    assert results == [FlashDump()]
    clock.end("the runner exited with code 101")
    asking.join(5)
    assert not asking.is_alive() and answer == [None]
