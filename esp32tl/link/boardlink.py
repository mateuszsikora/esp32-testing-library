"""`BoardLink`: the harness's end of the link, between the runner and the board's interfaces.

It reads the runner's messages to end of file and writes only in its own turns, each ended by
exactly one `wait` (`docs/protocol.md` §4; section numbers below are that document's). Events go
to the interfaces on the pins they touch, as they arrive; what the interfaces answer, and what
they send unprompted, bytes or GPIO levels, go out in the next turn, none stamped before its
`sync`. The turn's `wait` bounds the next one with the minimum `quiet_until` and the minimum
`latency` over the interfaces, `None` counting as no bound (§7).

A link made with a `hold` asks it at the start of every turn, the first at `t = 0` before the
guest's first instruction (§6, rule 4), and takes the turn only once it returns: the `esp32sim`
backend holds the run there for as long as the test is not waiting, and emulated time does not
move meanwhile. What the hold returns bounds the next turn, as an interface's `quiet_until` does,
and `None` ends the run with `stop`. With `console`, the guest's console reaches it as the
runner's `console` messages carry it (`--console socket` or `both`).

With `--wifi`, the Ethernet frames the station sends (`eth_tx`) go to the one interface that is the
network behind the access point (`Interface.ethernet`), a frame per call, and what it answers goes
in as `eth_rx`, a frame per output.

The backend can also switch the board's supply and pulse the chip's EN pin (`board_input`): the
message goes out at the start of the next turn, before what the interfaces send. Every `reset` the
runner sends is kept in `resets`, in order, and given to `on_reset`.

Warnings and the run's `stats` are kept for the test report. What the harness finds wrong with
the run is kept in `error`, the first cause only; the harness then ends the run with `stop` at the
runner's next turn, and still reads to end of file. A `frm_err` warning is such an error: bytes
sent at another baud than the guest's UART receives at would otherwise be lost in silence.
"""

import socket
import traceback
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from esp32tl.link.protocol import (
    MAX_LINE,
    Adc,
    ChipReset,
    Console,
    EthRx,
    EthTx,
    Event,
    GpioIn,
    GpioOut,
    HarnessMessage,
    Input,
    Power,
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
    WifiAp,
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
    """One wire of the board, as `BoardLink` drives it: a UART on `wire`, or GPIO lines.

    A UART interface receives `(t_ns, direction, bytes)` and returns `(t_ns, bytes)` (plan §3);
    an interface without a `wire` has no UART, and sends GPIO levels through `inputs`. It sees
    times on the backend's clock and nothing else of the backend, so it cannot tell the emulator
    from a real board but through that clock. The defaults are an interface with nothing behind
    it: it hears everything and says nothing.

    `BoardLink` calls it from the link's thread only, never while it is answering another call.
    """

    def __init__(self, name: str, wire: Wire | None) -> None:
        self.name = name
        self.wire = wire

    @property
    def lossless(self) -> bool:
        """Whether UART receive overflow must fail this route's run."""
        return False

    @property
    def ethernet(self) -> bool:
        """Whether the interface is the network behind the Wi-Fi access point: it has no wire,
        `receive` gets each Ethernet frame the station sends (`eth_tx`), and each output it
        returns is one frame for the station (`eth_rx`). Its `inputs` may switch the access
        point (`wifi_ap`)."""
        return False

    @property
    def pins(self) -> frozenset[int]:
        """The GPIOs the interface is wired to, which the link watches: its wire's."""
        return frozenset() if self.wire is None else self.wire.pins

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

    def inputs(self, now: int) -> Iterable[Input]:
        """The GPIO inputs (`gpio_in`, `release`) the interface sends in the turn at `now`, on
        its own pins, asked at every turn after `poll`; none stamped before `now`."""
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
        """The guest changed the output of this interface's pins at `t`: the `[pin, level]`
        changes in the order written, and the pins whose output driver is enabled after them."""


class Hold(Protocol):
    """What holds the run in the harness's turns: the test's clock."""

    def turn(self, now: int) -> int | None:
        """Called at the start of the turn at `now`, before the interfaces are polled; returns,
        once the run may go on, the latest time of the next turn, later than `now`, or `None`
        to end the run with `stop` in this turn."""
        ...


class LinkError(Exception):
    """What the harness found wrong with the run: the runner broke the protocol, or an
    interface broke its promises or raised."""


# How errors name the hold and the console's reader.
HOLD = "the test's clock"
CONSOLE = "the console"

# The most bytes one `uart_rx` carries: 512 KiB is 683 KiB of base64, well within the 1 MiB
# line. A longer output goes out as several messages of the same `t`, which the runner paces one
# after another on the line, as one (protocol 6, rule 5).
UART_RX_CHUNK = 512 * 1024


class BoardLink:
    """The link to one run of the runner, on the board profile `board` of `chip`, with its
    `interfaces`. `watch`, sent in the harness's `hello`, is their pins. `on_error` is called
    once, from the link's thread, when the harness finds the run wrong, so that the backend can
    end the run without waiting for the runner's next turn. `hold` holds every turn until it
    returns, and bounds the next. `console` is called with the `t` and the bytes of every
    `console` message. With `clock`, a runner whose `hello` names another clock fails the link.
    `on_reset` is called, from the link's thread, with every `reset` the runner sends."""

    def __init__(
        self,
        board: str,
        interfaces: Sequence[Interface] = (),
        *,
        chip: str = "esp32",
        on_error: Callable[[str], None] | None = None,
        hold: Hold | None = None,
        console: Callable[[int, bytes], None] | None = None,
        clock: str | None = None,
        on_reset: Callable[[Reset], None] | None = None,
    ) -> None:
        owners: dict[int, str] = {}
        for interface in interfaces:
            for pin in interface.pins:
                if isinstance(pin, bool) or not isinstance(pin, int) or pin < 0:
                    raise ValueError(
                        f"the pins of {interface.name} hold {pin!r}, not a GPIO number"
                    )
                if pin in owners:
                    raise ValueError(f"GPIO {pin} is on {owners[pin]} and {interface.name}")
                owners[pin] = interface.name
        ethernet = [interface.name for interface in interfaces if interface.ethernet]
        if len(ethernet) > 1:
            raise ValueError(f"one network behind the access point, not {', '.join(ethernet)}")
        self.board = board
        self.chip = chip
        self.clock = clock
        self.interfaces = tuple(interfaces)
        self._watched = frozenset(owners)
        self.watch = tuple(sorted(self._watched))
        self.on_error = on_error
        self.hold = hold
        self.console = console
        self.on_reset = on_reset
        # The chip's resets, as the runner reported them.
        self.resets: list[Reset] = []
        # The board's own inputs for the next turn: its supply switched, its EN pulsed.
        self._board_inputs: list[Power | ChipReset] = []
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

    def board_input(self, message: Power | ChipReset) -> None:
        """Sends `message` at the start of the next turn, which must not be later than its `t`.
        Called from the test's thread while the hold keeps the link in a turn, or before the
        run."""
        self._board_inputs.append(message)

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
        elif isinstance(message, EthTx):
            self._eth_tx(message)
        elif isinstance(message, UartCfg):
            self._uart_cfg(message)
        elif isinstance(message, GpioOut):
            self._gpio_out(message)
        elif isinstance(message, Warn):
            if message.kind == "frm_err":
                raise LinkError(self._frame_errors(message))
            if message.kind == "rxfifo_ovf":
                owner = next(
                    (
                        i
                        for i in self.interfaces
                        if i.lossless and i.wire is not None and i.wire.rx == message.pin
                    ),
                    None,
                )
                if owner is not None:
                    raise LinkError(
                        f"{owner.name}: UART receive overflow (rxfifo_ovf) on GPIO "
                        f"{message.pin}: the simulated network requires a lossless route"
                    )
        elif isinstance(message, Console):
            if self.console is not None:
                with calling(CONSOLE, "console"):
                    self.console(message.t, message.data)
        elif isinstance(message, Sync):
            return self._turn(message.t)
        elif isinstance(message, Reset):
            # The interfaces need nothing of it: the board persists across it (5.5), and the
            # routes and pins it resets come again as uart_cfg and gpio_out.
            self.resets.append(message)
            if self.on_reset is not None:
                with calling("the board", "on_reset"):
                    self.on_reset(message)
        else:
            assert isinstance(message, Stats), message
        return []

    def _uart_tx(self, message: UartTx) -> None:
        for interface in self.interfaces:
            wire = interface.wire
            if wire is not None and wire.tx is not None and wire.tx in message.tx_pins:
                with calling(interface.name, "receive"):
                    answers = _outputs(
                        interface.name, "receive", interface.receive(message.t, "out", message.data)
                    )
                self._answers += [(interface, t, data) for t, data in answers]

    def _eth_tx(self, message: EthTx) -> None:
        for interface in self.interfaces:
            if interface.ethernet:
                with calling(interface.name, "receive"):
                    answers = _outputs(
                        interface.name, "receive", interface.receive(message.t, "out", message.data)
                    )
                self._answers += [(interface, t, data) for t, data in answers]

    def _uart_cfg(self, message: UartCfg) -> None:
        self._routes[message.port] = message
        for index, interface in enumerate(self.interfaces):
            wire = interface.wire
            if wire is None:
                continue
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
            None
            if pin is None
            else next((i for i in self.interfaces if i.wire is not None and i.wire.rx == pin), None)
        )
        if owner is not None:
            assert owner.wire is not None
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
            pins = interface.pins
            changes = [(pin, level) for pin, level in message.changes if pin in pins]
            enabled = pins & frozenset(message.enabled)
            # An enable-only change comes with empty changes (5.4).
            if changes or enabled != pins & self._enabled:
                with calling(interface.name, "driven"):
                    interface.driven(message.t, changes, enabled)
        self._enabled = frozenset(message.enabled)

    def _turn(self, now: int) -> list[HarnessMessage]:
        """The harness's turn at the block point `now`: the inputs, then the `wait`; or `stop`,
        when the hold ends the run."""
        self.turns += 1
        bounds: list[tuple[str, int | None]] = []
        if self.hold is not None:
            with calling(HOLD, "turn"):
                bound = self.hold.turn(now)
            if bound is None:
                self._stopped = True
                return [Stop()]
            bounds.append((HOLD, bound))
        board, self._board_inputs = self._board_inputs, []
        for switch in board:
            if switch.t < now:
                raise LinkError(
                    f"the board's {switch.TYPE} is stamped {switch.t} ns, before the turn at"
                    f" {now} ns"
                )
        outputs, self._answers = self._answers, []
        for interface in self.interfaces:
            with calling(interface.name, "poll"):
                polled = _outputs(interface.name, "poll", interface.poll(now))
            outputs += [(interface, t, data) for t, data in polled]
        inputs: list[tuple[Interface, Input]] = []
        for interface, t, data in outputs:
            if not data:
                continue
            if t < now:
                raise LinkError(
                    f"{interface.name} sent bytes stamped {t} ns in the turn at {now} ns: it"
                    " answered sooner than its latency, or sent past its quiet_until"
                )
            if interface.ethernet:
                inputs.append((interface, EthRx(t, data)))
                continue
            wire = interface.wire
            if wire is None or wire.rx is None:
                raise LinkError(f"{interface.name} sent bytes, and has no RX pin to send them on")
            inputs += [(interface, UartRx(t, wire.rx, wire.baud, chunk)) for chunk in _chunks(data)]
        for interface in self.interfaces:
            with calling(interface.name, "inputs"):
                inputs += [(interface, message) for message in interface.inputs(now)]
        for interface in self.interfaces:
            with calling(interface.name, "quiet_until"):
                bounds.append((interface.name, interface.quiet_until()))
        for interface, message in inputs:
            self._check_input(interface, message, now)
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
        return [
            *board,
            *(message for _, message in inputs),
            Wait(self._seq, quiet_until, latency),
        ]

    def _check_input(self, interface: Interface, message: Input, now: int) -> None:
        """Refuses, naming the interface that sent it, an input the runner would refuse: one that
        is not a message of version 1, one stamped before the turn, one on a pin the link does not
        watch; and one on a pin another interface is wired to, which the runner would take."""
        source = interface.name
        if not isinstance(message, UartRx | GpioIn | Release | Adc | EthRx | WifiAp):
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
        if isinstance(message, EthRx | WifiAp):
            if not interface.ethernet:
                raise LinkError(
                    f"{source} sent {message.TYPE}, and is not the network behind the access point"
                )
            return
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
        if not pins <= interface.pins:
            raise LinkError(
                f"{source} sent {message.TYPE} on {_pins(sorted(pins - interface.pins))}, which"
                " another interface is wired to"
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
            if stats.wifi is not None:
                w = stats.wifi
                lines.append(
                    f"  Wi-Fi: {_count(w.eth_tx, 'Ethernet frame')} sent"
                    f" ({w.eth_tx_dropped} dropped by the relay), {w.eth_rx} received"
                    f" ({w.eth_rx_dropped} with the access point off the air);"
                    f" {_count(w.rx_dropped, 'frame')} from the air with no receive descriptor"
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
