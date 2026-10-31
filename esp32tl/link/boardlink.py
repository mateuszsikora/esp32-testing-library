"""`BoardLink`: the harness's end of the link, between the runner and the board's interfaces.

It reads the runner's messages to end of file and writes only in its own turns, each ended by
exactly one `wait` (`docs/protocol.md` §4; section numbers below are that document's). Events go
to the interfaces on the pins they touch, as they arrive; what the interfaces answer, what they
send unprompted and what the scenario timeline has due go out in the next turn, none stamped
before its `sync`. The turn's `wait` bounds the next one with the minimum `quiet_until` and the
minimum `latency` over the interfaces and the timeline, `None` counting as no bound (§7).

A link made with `hold` takes its first turn, at `t = 0` before the guest's first instruction
(§6, rule 4), only once `start` is called: the backend holds the run there until the test has
given every source it declared, and emulated time does not move meanwhile.

Warnings and the run's `stats` are kept for the test report. What the harness finds wrong with
the run is kept in `error`, the first cause only; the harness then ends the run with `stop` at the
runner's next turn, and still reads to end of file. A `frm_err` warning is such an error: bytes
sent at another baud than the guest's UART receives at would otherwise be lost in silence.
"""

import socket
import threading
import traceback
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from esp32tl.link.protocol import (
    MAX_LINE,
    Adc,
    Console,
    Event,
    GpioIn,
    GpioOut,
    HarnessMessage,
    Input,
    ProtocolError,
    Release,
    Reset,
    RunnerHello,
    RunnerMessage,
    Stats,
    Stop,
    Sync,
    UartCfg,
    UartRx,
    UartTx,
    Wait,
    Warn,
    as_json,
    decode_harness,
    decode_runner,
    encode,
    handshake,
)

# `out` of the ESP32 or `in` to it, as in the recording format (plan 4.3).
Direction = Literal["in", "out"]

# What an interface sends into the chip: from `t`, in nanoseconds, these bytes.
Output = tuple[int, bytes]


@dataclass(frozen=True)
class Wire:
    """A UART interface's pins, as the board profile gives them. `tx` is the ESP32's TX pin,
    where the guest's bytes come out; `rx` its RX pin, where the interface's go in; either may
    be missing (a GNSS receiver that takes no commands has no `tx`). `baud` is the interface's
    own line rate, which its bytes go in at."""

    tx: int | None
    rx: int | None
    baud: int

    @property
    def pins(self) -> frozenset[int]:
        return frozenset(pin for pin in (self.tx, self.rx) if pin is not None)


class Interface:
    """One wire of the board, as `BoardLink` drives it: a UART for now.

    An interface receives `(t_ns, direction, bytes)` and returns `(t_ns, bytes)` (plan §3). It
    sees times on the backend's clock and nothing else of the backend, so it cannot tell the
    emulator from a real board but through that clock. The defaults are an interface with nothing
    behind it: it hears everything and says nothing.

    `BoardLink` calls it from the link's thread only, never while it is answering another call.
    """

    def __init__(self, name: str, wire: Wire) -> None:
        self.name = name
        self.wire = wire

    def receive(self, t: int, direction: Direction, data: bytes) -> Iterable[Output]:
        """`data` crossed the wire at `t`, `out` of the ESP32 (what the guest transmitted). The
        bytes of one wire come in order but cut anywhere: where one call ends carries no meaning
        but the `t` of the next (protocol 5.2), so requests are found by their terminator.
        Returns the answers, each stamped at least `latency()` after the request it answers."""
        return ()

    def poll(self, now: int) -> Iterable[Output]:
        """What the interface sends unprompted, asked at every turn; none stamped before `now`.
        Its outputs may lie in the future: the runner queues them (protocol §8)."""
        return ()

    def quiet_until(self) -> int | None:
        """Before this time the interface sends nothing unprompted; `None` when it has nothing
        scheduled. Asked after `poll(now)`, and must be later than `now`."""
        return None

    def latency(self) -> int | None:
        """The shortest time in which the interface answers what it receives, in nanoseconds;
        `None` when it answers nothing."""
        return None

    def configured(self, t: int, baud: int | None, transmits: bool, receives: bool) -> None:
        """The guest's UART routes on this wire changed at `t`: `transmits` whether a UART's TX
        reaches `wire.tx`, `receives` whether a UART's RX listens on `wire.rx`, and `baud` the
        route's rate (`None` while unset, or when no route touches the wire), the receiving
        route's when two ports share the wire. The interface's bytes still go in at `wire.baud`,
        its own line rate: a mismatch reaches the UART as the frame error it is (protocol 11.6),
        and this is how a receive-only interface can tell one before the `frm_err` warning."""

    def driven(self, t: int, changes: Sequence[tuple[int, int]], enabled: frozenset[int]) -> None:
        """The guest changed the output of this wire's pins at `t`: the `[pin, level]` changes in
        the order written, and the pins whose output driver is enabled after them."""


class Timeline(Protocol):
    """Inputs scheduled at times of their own rather than in answer to the guest: the scenario."""

    @property
    def pins(self) -> frozenset[int]:
        """The pins its inputs drive, watched from the handshake on: an input on a pin outside
        `watch` is a protocol error (5.1)."""
        ...

    def due(self, now: int) -> Iterable[Input]:
        """The inputs to send in the turn at `now`, none stamped before it."""
        ...

    def quiet_until(self) -> int | None:
        """The time of the next input it holds back, asked after `due(now)`; `None` when none."""
        ...


class LinkError(Exception):
    """What the harness found wrong with the run: the runner broke the protocol, or an
    interface or the scenario broke its promises or raised."""


# How errors name the timeline.
SCENARIO = "the scenario"

# The most bytes one `uart_rx` carries: 512 KiB is 683 KiB of base64, well within the 1 MiB
# line. A longer output goes out as several messages of the same `t`, which the runner paces one
# after another on the line, as one (protocol 6, rule 5).
UART_RX_CHUNK = 512 * 1024


class BoardLink:
    """The link to one run of the runner, on the board profile `board` of `chip`, with its
    `interfaces` and the scenario's `timeline`. `watch`, sent in the harness's `hello`, is their
    pins. `on_error` is called once, from the link's thread, when the harness finds the run
    wrong, so that the backend can end the run without waiting for the runner's next turn. With
    `hold`, the first turn waits for `start`. With `clock`, a runner whose `hello` names another
    clock fails the link."""

    def __init__(
        self,
        board: str,
        interfaces: Sequence[Interface] = (),
        *,
        chip: str = "esp32",
        timeline: Timeline | None = None,
        on_error: Callable[[str], None] | None = None,
        hold: bool = False,
        clock: str | None = None,
    ) -> None:
        owners: dict[int, str] = {}
        for interface in interfaces:
            for pin in interface.wire.pins:
                if pin in owners:
                    raise ValueError(f"GPIO {pin} is on {owners[pin]} and {interface.name}")
                owners[pin] = interface.name
        self.board = board
        self.chip = chip
        self.clock = clock
        self.interfaces = tuple(interfaces)
        self.timeline = timeline
        # The scenario drives pins of interfaces, or pins of its own.
        scenario_pins = frozenset(timeline.pins) if timeline is not None else frozenset()
        for pin in scenario_pins:
            if isinstance(pin, bool) or not isinstance(pin, int) or pin < 0:
                raise ValueError(f"the scenario's pins hold {pin!r}, not a GPIO number")
        self._watched = frozenset(owners) | scenario_pins
        self.watch = tuple(sorted(self._watched))
        self.on_error = on_error
        # What the run told and what the harness found, read once `run` has returned.
        self.runner: RunnerHello | None = None
        self.warnings: list[Warn] = []
        self.stats: Stats | None = None
        self.turns = 0
        self.error: str | None = None
        self.error_traceback: str | None = None
        self.connected = False
        self._seq = 0
        self._t = 0
        self._stopped = False
        self._routes: dict[int, UartCfg] = {}
        # What each interface was last told of its wire's routes; before the first uart_cfg,
        # every port has no pins and no baud (5.3).
        self._configured: list[tuple[int | None, bool, bool]] = [(None, False, False)] * len(
            self.interfaces
        )
        self._enabled: frozenset[int] = frozenset()
        self._answers: list[tuple[Interface, int, bytes]] = []
        # The first turn waits for this, and then boots the guest, or stops the run.
        self._started = threading.Event()
        self._boot = True
        self._starting = threading.Lock()
        if not hold:
            self._started.set()

    @property
    def held(self) -> bool:
        """Whether the first turn still waits for `start`."""
        return not self._started.is_set()

    def start(self, boot: bool = True) -> bool:
        """Lets the first turn go: with `boot`, it is taken as any other, and the guest boots
        after it; without, it is `stop`, and the run ends before the guest's first instruction.
        Only the first call counts, and returns True; on a link made without `hold`, none does.
        From any thread."""
        with self._starting:
            if self._started.is_set():
                return False
            self._boot = boot
            self._started.set()
            return True

    def run(self, conn: socket.socket) -> None:
        """Reads the runner's messages from `conn` to end of file, writing the harness's in its
        turns, whatever has gone wrong: the runner sends its `stats` before it closes the link,
        also after a mismatched handshake. Leaves `conn` open."""
        # Here only: "connected" is what the report's "never connected" is about.
        self.connected = True
        writable = True
        with conn.makefile("rb") as stream:
            while line := stream.readline(MAX_LINE + 1):
                if not line.endswith(b"\n"):
                    if len(line) > MAX_LINE:
                        self._fail(f"the runner sent a line of more than {MAX_LINE} bytes")
                        while line and not line.endswith(b"\n"):
                            line = stream.readline(MAX_LINE + 1)
                        if line:
                            continue
                    self._fail("the link ended inside a line: the runner died mid-message")
                    break
                try:
                    message = decode_runner(line)
                except ProtocolError as error:
                    self._fail(f"the runner sent a malformed message: {error}")
                    continue
                replies = self.receive(message)
                if not (replies and writable):
                    continue
                try:
                    payload = b"".join(encode(reply) for reply in replies)
                except Exception as error:
                    # Unreachable with checked inputs; were it reached, the runner would wait
                    # for a turn that never ends.
                    self._fail(f"the harness cannot encode its turn: {_raised(error)}", error)
                    payload = b""
                    if isinstance(message, Sync) and not self._stopped:
                        self._stopped = True
                        payload = encode(Stop())
                if payload:
                    try:
                        conn.sendall(payload)
                    except OSError:
                        # The runner closed its end in the harness's turn, as it does on SIGTERM
                        # (protocol §9): what it sends before end of file is still to be read.
                        writable = False

    def receive(self, message: RunnerMessage) -> list[HarnessMessage]:
        """Takes the runner's next message; returns what the harness writes now, which is
        something only at the handshake and at the end of the runner's turn. Never raises:
        whatever goes wrong is the link's `error`, and the run ends at the next turn."""
        try:
            return self._receive(message)
        except LinkError as error:
            self._fail(str(error), error.__cause__)
        except Exception as error:
            # A fault of the harness's own: it fails the link all the same, so that the runner
            # is not left waiting for a turn that never ends.
            self._fail(f"the harness failed on {message.TYPE}: {_raised(error)}", error)
        if isinstance(message, Sync) and not self._stopped:
            self._stopped = True
            return [Stop()]
        return []

    def _receive(self, message: RunnerMessage) -> list[HarnessMessage]:
        if self.stats is not None:
            raise LinkError(f"the runner sent {message.TYPE} after its stats")
        if self.runner is None:
            if not isinstance(message, RunnerHello):
                raise LinkError(f"the runner's first message is {message.TYPE}, not hello")
            self.runner = message
            answer, mismatch = handshake(
                message, chip=self.chip, board=self.board, watch=self.watch
            )
            if mismatch is not None:
                # Not on_error: the runner finds the mismatch in the answer and ends the run
                # itself, with the exit code that says so (§4).
                self._fail(mismatch, notify=False)
            elif self.clock is not None and message.clock != self.clock:
                # The interfaces' times are on the backend's clock: the run would not be.
                self._fail(f"the runner's clock is {message.clock}, the backend's {self.clock}")
            return [answer]
        if isinstance(message, RunnerHello):
            raise LinkError("the runner sent a second hello")
        if isinstance(message, Warn):
            self.warnings.append(message)
        elif isinstance(message, Stats):
            self.stats = message
        if self.error is not None:
            # Only the end of the run is of interest now.
            if isinstance(message, Sync) and not self._stopped:
                self._stopped = True
                return [Stop()]
            return []
        if message.t < self._t:
            raise LinkError(
                f"the runner sent {message.TYPE} at {message.t} ns after a message at {self._t} ns"
            )
        self._t = message.t
        if isinstance(message, Event):
            if message.seq != self._seq + 1:
                raise LinkError(
                    f"the runner sent {message.TYPE} with seq {message.seq} after {self._seq}"
                )
            self._seq = message.seq
        if isinstance(message, UartTx):
            self._uart_tx(message)
        elif isinstance(message, UartCfg):
            self._uart_cfg(message)
        elif isinstance(message, GpioOut):
            self._gpio_out(message)
        elif isinstance(message, Warn):
            if message.kind == "frm_err":
                raise LinkError(self._frame_errors(message))
        elif isinstance(message, Sync):
            if self.turns == 0:
                self._started.wait()
                if not self._boot:
                    self._stopped = True
                    return [Stop()]
            return self._turn(message.t)
        else:
            # Reset needs nothing of the interfaces: the board persists across it (5.5), and
            # the routes it resets come again as uart_cfg. Console is the console tap's (M2).
            assert isinstance(message, Reset | Console | Stats), message
        return []

    def _uart_tx(self, message: UartTx) -> None:
        for interface in self.interfaces:
            if interface.wire.tx is not None and interface.wire.tx in message.tx_pins:
                with calling(interface.name, "receive"):
                    answers = _outputs(
                        interface.name, "receive", interface.receive(message.t, "out", message.data)
                    )
                self._answers += [(interface, t, data) for t, data in answers]

    def _uart_cfg(self, message: UartCfg) -> None:
        self._routes[message.port] = message
        for index, interface in enumerate(self.interfaces):
            wire = interface.wire
            baud, transmits, receives = None, False, False
            # Over every port: two of them can share a wire, one transmitting, one receiving.
            for route in self._routes.values():
                tx = wire.tx is not None and wire.tx in route.tx_pins
                rx = wire.rx is not None and route.rx_pin == wire.rx
                if rx or (tx and not receives):
                    baud = route.baud
                transmits, receives = transmits or tx, receives or rx
            state = (baud, transmits, receives)
            if state != self._configured[index]:
                self._configured[index] = state
                with calling(interface.name, "configured"):
                    interface.configured(message.t, *state)

    def _frame_errors(self, warning: Warn) -> str:
        """Why a `frm_err` warning fails the run: the bytes went in at another baud than the
        guest's UART receives at, so the firmware lost them without a sign the test could see,
        and whatever it should have done with them would fail later as a timeout (5.8)."""
        rate = "with no baud set" if warning.baud is None else f"at {warning.baud} baud"
        uart = "UART" if warning.port is None else f"UART{warning.port}"
        dropped = "bytes" if warning.nbytes is None else _count(warning.nbytes, "byte")
        pin = warning.pin
        owner = (
            None if pin is None else next((i for i in self.interfaces if i.wire.rx == pin), None)
        )
        if owner is not None:
            sender = f"{owner.name} sends at {owner.wire.baud} baud on GPIO {pin}"
        elif pin is not None:
            sender = f"bytes on GPIO {pin}"
        else:
            sender = "bytes on a pin the runner did not name"
        return (
            f"{sender}, and the firmware's {uart} receives there {rate}:"
            f" {dropped} dropped as frame errors (frm_err)"
        )

    def _gpio_out(self, message: GpioOut) -> None:
        for interface in self.interfaces:
            pins = interface.wire.pins
            changes = [(pin, level) for pin, level in message.changes if pin in pins]
            enabled = pins & frozenset(message.enabled)
            # An enable-only change comes with empty changes (5.4).
            if changes or enabled != pins & self._enabled:
                with calling(interface.name, "driven"):
                    interface.driven(message.t, changes, enabled)
        self._enabled = frozenset(message.enabled)

    def _turn(self, now: int) -> list[HarnessMessage]:
        """The harness's turn at the block point `now`: the inputs, then the `wait`."""
        self.turns += 1
        outputs, self._answers = self._answers, []
        for interface in self.interfaces:
            with calling(interface.name, "poll"):
                polled = _outputs(interface.name, "poll", interface.poll(now))
            outputs += [(interface, t, data) for t, data in polled]
        inputs: list[tuple[str, Input]] = []
        for interface, t, data in outputs:
            if not data:
                continue
            if t < now:
                raise LinkError(
                    f"{interface.name} sent bytes stamped {t} ns in the turn at {now} ns: it"
                    " answered sooner than its latency, or sent past its quiet_until"
                )
            if interface.wire.rx is None:
                raise LinkError(f"{interface.name} sent bytes, and has no RX pin to send them on")
            inputs += [
                (interface.name, UartRx(t, interface.wire.rx, interface.wire.baud, chunk))
                for chunk in _chunks(data)
            ]
        bounds: list[tuple[str, int | None]] = []
        for interface in self.interfaces:
            with calling(interface.name, "quiet_until"):
                bounds.append((interface.name, interface.quiet_until()))
        if self.timeline is not None:
            with calling(SCENARIO, "due"):
                inputs += [(SCENARIO, message) for message in self.timeline.due(now)]
            with calling(SCENARIO, "quiet_until"):
                bounds.append((SCENARIO, self.timeline.quiet_until()))
        for source, message in inputs:
            self._check_input(source, message, now)
        quiet_until = None
        for name, bound in bounds:
            if bound is None:
                continue
            if isinstance(bound, bool) or not isinstance(bound, int):
                raise LinkError(f"{name}.quiet_until returned {bound!r}, not nanoseconds")
            if bound <= now:
                raise LinkError(
                    f"{name} is quiet until {bound} ns, not after the turn at {now} ns: what it"
                    " sends by then belongs in this turn"
                )
            quiet_until = bound if quiet_until is None else min(quiet_until, bound)
        latency = None
        for interface in self.interfaces:
            with calling(interface.name, "latency"):
                bound = interface.latency()
            if bound is None:
                continue
            if isinstance(bound, bool) or not isinstance(bound, int) or bound < 0:
                raise LinkError(f"{interface.name} declares a latency of {bound!r} ns")
            latency = bound if latency is None else min(latency, bound)
        return [*(message for _, message in inputs), Wait(self._seq, quiet_until, latency)]

    def _check_input(self, source: str, message: Input, now: int) -> None:
        """Refuses, naming its source, an input the runner would refuse: one that is not a
        message of version 1, one stamped before the turn, one on a pin the link does not watch."""
        if not isinstance(message, UartRx | GpioIn | Release | Adc):
            raise LinkError(f"{source} sent {message!r}, which is not an input")
        try:
            # What the runner reads, through the same codec: types, ranges, the line's length.
            decode_harness(encode(message))
        except (ProtocolError, TypeError, ValueError) as error:
            raise LinkError(
                f"{source} sent a {message.TYPE} the link cannot carry: {error}"
            ) from None
        if message.t < now:
            raise LinkError(
                f"{source} sent {message.TYPE} stamped {message.t} ns in the turn at {now} ns:"
                " it held it back past its quiet_until"
            )
        if isinstance(message, GpioIn):
            pins = {pin for pin, _ in message.levels}
        elif isinstance(message, Release):
            pins = set(message.pins)
        else:
            pins = {message.pin}
        if not pins <= self._watched:
            raise LinkError(
                f"{source} sent {message.TYPE} on {_pins(sorted(pins - self._watched))}, which"
                " the link does not watch"
            )

    def _fail(self, reason: str, cause: BaseException | None = None, notify: bool = True) -> None:
        if self.error is not None:
            return
        self.error = reason
        if cause is not None:
            self.error_traceback = "".join(traceback.format_exception(cause))
        if notify and self.on_error is not None:
            self.on_error(reason)

    def report(self) -> str:
        """What the run told and what the harness found, for the test report."""
        lines = [f"board {self.board}, watching {_pins(self.watch) or 'no pins'}"]
        runner = self.runner
        if not self.connected:
            lines.append("the runner never connected")
        elif runner is not None and runner.esp32sim is None:
            lines.append(f"runner: protocol version {runner.version}")
        elif runner is not None:
            lines.append(
                f"runner: protocol version {runner.version}, esp32sim {runner.esp32sim},"
                f" clock {runner.clock}; {_count(self.turns, 'turn')} taken by the harness"
            )
        if self.warnings:
            lines.append(f"warnings ({len(self.warnings)}):")
            lines += [f"  {_time(w.t)}  {w.kind}: {w.detail}" for w in self.warnings]
        stats = self.stats
        if stats is not None:
            end = stats.end + "".join(f", {extra}" for extra in (stats.stop, stats.detail) if extra)
            lines.append(
                f"stats: the run ended at {_time(stats.t)} ({end});"
                f" {_count(stats.turns, 'turn')}, {_count(stats.resets, 'reset')}"
            )
            lines += [
                f"  UART{u.port}: {u.tx_bytes} bytes sent, {u.rx_bytes} received,"
                f" {u.rxfifo_ovf} rxfifo_ovf, {u.frm_err} frm_err"
                for u in stats.uart
            ]
            lines.append(
                f"  unrouted_rx_bytes {stats.unrouted_rx_bytes},"
                f" inputs_on_output {stats.inputs_on_output},"
                f" inputs_not_applied {stats.inputs_not_applied},"
                f" rx_bytes_not_paced {stats.rx_bytes_not_paced}"
            )
        elif self.connected:
            lines.append("no stats: the link ended without them (the runner was killed, or died)")
        if self.error is not None:
            lines.append(f"error: {self.error}")
        if self.error_traceback is not None:
            lines.append(self.error_traceback.rstrip())
        return "\n".join(lines)

    def properties(self) -> dict[str, Any]:
        """The same as data, for the report's `user_properties`."""
        return {
            "board": self.board,
            "watch": list(self.watch),
            "warnings": [as_json(warning) for warning in self.warnings],
            "stats": None if self.stats is None else as_json(self.stats),
            "error": self.error,
        }


@contextmanager
def calling(name: str, method: str) -> Iterator[None]:
    """Turns what `name`'s `method` raises into the `LinkError` that names them. Callers read an
    iterable it returns inside the block, so that a generator raises there too."""
    try:
        yield
    except LinkError:
        raise
    except Exception as error:
        raise LinkError(f"{name}.{method} raised {_raised(error)}") from error


def _outputs(name: str, method: str, values: Iterable[Output]) -> list[Output]:
    """`values` read, each checked to be a `(t_ns, bytes)` pair."""
    outputs = []
    for value in values:
        if not (
            isinstance(value, tuple)
            and len(value) == 2
            and isinstance(value[0], int)
            and not isinstance(value[0], bool)
            and isinstance(value[1], bytes)
        ):
            raise LinkError(f"{name}.{method} returned {value!r:.80}, not a (t_ns, bytes) pair")
        outputs.append(value)
    return outputs


def _chunks(data: bytes) -> list[bytes]:
    return [data[at : at + UART_RX_CHUNK] for at in range(0, len(data), UART_RX_CHUNK)]


def _raised(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" + ("" if n == 1 else "s")


def _pins(pins: Iterable[int]) -> str:
    return ", ".join(f"GPIO {pin}" for pin in pins)


def _time(t: int) -> str:
    return f"{t // 10**9}.{t % 10**9:09d} s"
