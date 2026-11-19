"""The air around the board: Bumble's controllers on one virtual link, run on the board's clock.

Bumble is written for asyncio, and its controllers, hosts and timers take their time from the
event loop. `Air` runs them on a `BoardLoop`, whose `time()` is the board's time and never the
host's, and moves that time only in `advance`: it runs everything due at one instant until nothing
is left to do there, then goes to the next instant something is due, a timer of the loop or a
party's `quiet_until`, as the link goes from turn to turn. Times are nanoseconds on the board's
clock. Nothing here reads the host's clock, so two runs do the same at the same instants.

An air made with `board_clock` is the one the guest's Bluetooth controller is on, on the emulator:
the board's link alone moves it, with `follow`, as the run goes from turn to turn, and the test
waits through the board. Its `advance`, `wait` and `run` refuse. The test reaches it from its own
thread while the board holds the link in a turn, and the link from the link's thread: a lock keeps
the two apart.
"""

import asyncio
import math
import selectors
import threading
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from contextvars import Context
from typing import Any, Protocol, TypeVar, TypeVarTuple

from bumble import controller, core, hci, ll, lmp
from bumble.link import LocalLink

from esp32tl.backends.clock import nanoseconds, seconds
from esp32tl.interfaces import NeverHappened
from esp32tl.units import Duration

T = TypeVar("T")
Ts = TypeVarTuple("Ts")


class AirError(RuntimeError):
    """Something on the air failed while time moved: a callback or a task raised, or a party
    asked to be woken at an instant that has passed. Raised to whoever moved the time. Also what
    `advance`, `wait` and `run` raise on an air on the board's clock, which they cannot move."""


class Party(Protocol):
    """Something on the air with a schedule of its own besides the loop's timers: a peer whose
    source answers at times it chooses. `poll` runs inside the loop at every instant the air
    stops at; `quiet_until` is the next instant it needs one, later than the current one."""

    def poll(self, now: int) -> None: ...

    def quiet_until(self) -> int | None: ...


class _Selector(selectors.DefaultSelector):
    """The loop's selector, which never blocks: a timeout other than 0 means the loop has
    nothing left to do at this instant, so the selector stops it and notes how far away its
    next timer is (`None` for none)."""

    def __init__(self) -> None:
        super().__init__()
        self.loop: BoardLoop | None = None
        self.idle = False
        self.next_in: float | None = None

    def select(self, timeout: float | None = None) -> list[tuple[Any, int]]:
        events: list[tuple[Any, int]] = super().select(0)
        if timeout is None or timeout > 0:
            assert self.loop is not None
            self.idle = True
            self.next_in = timeout
            self.loop.stop()
        return events


class BoardLoop(asyncio.SelectorEventLoop):
    """An asyncio loop on the board's clock: `time()` is `now`, in seconds, and only `Air`
    moves it. Every timer falls on a nanosecond. `run_idle` runs it until nothing is left to do
    at `now`."""

    def __init__(self) -> None:
        selector = _Selector()
        super().__init__(selector)
        selector.loop = self
        self._idle_selector = selector
        self.now = 0
        # Under the nanosecond between two timers: the loop fires those due at `now`, never one
        # due at the next nanosecond, whose float may land a hair under `time()` + 1 ns.
        self._clock_resolution = 0.5e-9

    def time(self) -> float:
        return self.now / 1e9

    def call_at(
        self,
        when: float,
        callback: Callable[[*Ts], object],
        *args: *Ts,
        context: Context | None = None,
    ) -> asyncio.TimerHandle:
        """Schedules `callback` at the nanosecond nearest `when`, as the float `now / 1e9` that
        `time()` gives there. Otherwise a sum of floats, 6 s and 5.12 s, lands a hair above the
        instant: the loop fires the timer there, within `_clock_resolution`, but its selector
        sees a wait left and stops the loop, and what the timer schedules runs a nanosecond
        later. `call_later` comes through here. A `when` with no nanosecond, as `math.inf` from
        `asyncio.timeout(math.inf)`, stays as it is: such a timer never fires."""
        ns = when * 1e9
        if math.isfinite(ns):
            when = round(ns) / 1e9
        return super().call_at(when, callback, *args, context=context)

    def run_idle(self) -> int | None:
        """Runs what is due at `now` until nothing is left; returns the time of the loop's next
        timer, `None` for none."""
        selector = self._idle_selector
        selector.idle = False
        self.run_forever()
        if not selector.idle or selector.next_in is None:
            return None
        # A difference of floats, whose error is far under a nanosecond: the nearest nanosecond is
        # the timer's own, which is after `now`, as it is not due there. At least a nanosecond on
        # all the same, so that the air always moves.
        return max(round((self.now / 1e9 + selector.next_in) * 1e9), self.now + 1)


class Link(LocalLink):
    """Bumble's virtual link, which refuses delivery where no radio would reach: to and from a
    device that has left, and between two devices on a link that lost contact, until both ends
    have let it go. What is refused vanishes without a word, as on the air, also what is sent to
    an address no device has. Bumble's own link raises for that address, and delivers to any
    controller it holds. Whether a device can be reached is decided here and nowhere else.

    Devices are named by their public addresses here. An advertising event reaches every device on
    the air, whatever links were lost; LE data and control PDUs reach the device the connection is
    with, from the connection's own address, where Bumble's link names the sender's random
    address whatever address the connection has."""

    def __init__(self) -> None:
        # Bumble's LocalLink leaves its constructor unannotated.
        super().__init__()  # type: ignore[no-untyped-call]
        self._absent: set[hci.Address] = set()
        self._severed: set[frozenset[hci.Address]] = set()

    def present(self, address: hci.Address) -> bool:
        """Whether the device at `address` is on the air: it has not left, or it came back."""
        return address not in self._absent

    def reaches(self, sender: hci.Address, receiver: hci.Address) -> bool:
        """Whether what `sender` sends to `receiver` arrives."""
        return (
            self.present(sender)
            and self.present(receiver)
            and frozenset((sender, receiver)) not in self._severed
        )

    def leave(self, address: hci.Address, peers: Iterable[hci.Address]) -> None:
        """The device at `address` is gone, from its links to `peers` too: coming back does not
        mend them."""
        self._absent.add(address)
        self._severed.update(frozenset((address, peer)) for peer in peers)

    def back(self, address: hci.Address) -> None:
        """The device at `address` is on the air again; the links it lost stay severed."""
        self._absent.discard(address)

    def let_go(self, one: hci.Address, other: hci.Address) -> None:
        """Neither end holds the link between `one` and `other` any more: they may page again."""
        self._severed.discard(frozenset((one, other)))

    def send_acl_data(
        self,
        sender_controller: controller.Controller,
        destination_address: hci.Address,
        transport: core.PhysicalTransport,
        data: bytes,
    ) -> None:
        if transport == core.PhysicalTransport.LE:
            connection = sender_controller.le_connections.get(destination_address)
            receiver = self._le_peer(sender_controller, destination_address)
            if (
                connection is not None
                and receiver is not None
                and self.reaches(sender_controller.public_address, receiver.public_address)
            ):
                source = connection.self_address
                asyncio.get_running_loop().call_soon(
                    receiver.on_link_acl_data, source, transport, data
                )
            return
        if self.reaches(sender_controller.public_address, destination_address):
            super().send_acl_data(sender_controller, destination_address, transport, data)

    def _le_peer(
        self, sender: controller.Controller, address: hci.Address
    ) -> controller.Controller | None:
        """The controller `sender`'s LE connection to `address` is with."""
        peers = getattr(sender, "_le_peers", {})
        return peers.get(address) or self.find_le_controller(address)

    def send_advertising_pdu(
        self, sender_controller: controller.Controller, packet: ll.AdvertisingPdu
    ) -> None:
        if not self.present(sender_controller.public_address):
            return
        loop = asyncio.get_running_loop()
        for receiver in self.controllers:
            if receiver is not sender_controller and self.present(receiver.public_address):
                heard = getattr(receiver, "_heard_on_link", None)
                if heard is not None:
                    loop.call_soon(heard, sender_controller, packet)
                else:
                    loop.call_soon(receiver.on_ll_advertising_pdu, packet)

    def send_ll_control_pdu(
        self, sender_address: hci.Address, receiver_address: hci.Address, packet: ll.ControlPdu
    ) -> None:
        sender = self.find_le_controller(sender_address)
        if sender is None:
            return
        receiver = self._le_peer(sender, receiver_address)
        if receiver is not None and self.reaches(sender.public_address, receiver.public_address):
            asyncio.get_running_loop().call_soon(receiver.on_ll_control_pdu, sender_address, packet)

    def send_lmp_packet(
        self,
        sender_controller: controller.Controller,
        receiver_address: hci.Address,
        packet: lmp.Packet,
    ) -> None:
        if (
            self.reaches(sender_controller.public_address, receiver_address)
            and self.find_classic_controller(receiver_address) is not None
        ):
            super().send_lmp_packet(sender_controller, receiver_address, packet)


class Air:
    """One virtual Bluetooth link, and the loop its controllers run on, at the board's time.

    The air's time starts at 0 when it is made and moves only in `advance`, `wait` and `run`; with
    `board_clock`, only in `follow`, which the board's link calls, and those three refuse. What
    fails on the air, a callback, a task, a party, is raised from the call that moved the time, as
    an `AirError` whose cause is the failure. Every call holds the air's lock, so that the test's
    thread and the link's take turns on it.
    """

    def __init__(self, *, board_clock: bool = False) -> None:
        self.board_clock = board_clock
        self._lock = threading.RLock()
        self._loop = BoardLoop()
        self._loop.set_exception_handler(self._failed)
        self._link = Link()
        self._parties: list[Party] = []
        # The LE devices made on the air, in order, whose events the board's report lists, and
        # where those events go as they happen: the run log, on an air the board made.
        self.le_devices: list[Any] = []
        self.log: Callable[[int, str], None] | None = None
        self._next_timer: int | None = None
        self._error: BaseException | None = None
        # asyncio's description of where `_error` was raised; empty for one given to `fail`.
        self._error_context = ""

    @property
    def now(self) -> Duration:
        """The board's time on the air."""
        return Duration(self._loop.now)

    def _join(self, party: Party) -> None:
        """`party` is polled at every instant the air stops at from now on, the current one
        first, so that what it has due now is sent before anyone asks the air's next instant."""
        with self._lock:
            self._parties.append(party)
            self._settle()

    def note(self, text: str) -> None:
        """`text` happened on the air at its current instant: into the run log, if it has one."""
        if self.log is not None:
            self.log(self._loop.now, text)

    def call(self, function: Callable[[], T]) -> T:
        """`function`, called inside the loop at the current instant, and everything it sets
        off there; returns what it returned. From inside the loop, as from a coroutine on the
        air, `function` runs at once, and what it sets off follows when the caller yields."""
        with self._lock:
            if self._loop.is_running():
                return function()
            result: list[T] = []
            self._loop.call_soon(lambda: result.append(function()))
            # A function that raised fails the settle.
            self._settle()
            return result[0]

    def start(self, coroutine: Coroutine[Any, Any, T]) -> "asyncio.Task[T]":
        """A task for `coroutine` on the loop, run as far as it goes at the current instant."""
        return self.call(lambda: self._loop.create_task(coroutine))

    def fail(self, error: Exception) -> None:
        """Fails the air with `error`, whose message says all: the call that moved the time
        raises an `AirError` with that message. For a party, inside the loop."""
        if self._error is None:
            self._error = error
            self._error_context = ""

    def quiet_until(self) -> int | None:
        """The next instant something on the air is due: the loop's next timer or a party's
        `quiet_until`; `None` when nothing is. A party that raises fails the air."""
        with self._lock:
            times = [self._next_timer]
            for party in self._parties:
                try:
                    times.append(party.quiet_until())
                except Exception as error:
                    self.fail(error)
                    raise AirError(str(error)) from error
            due = [t for t in times if t is not None]
            return min(due) if due else None

    def advance(self, until: Duration) -> None:
        """Moves the air's time to `until`, a `Duration` since the air was made, stopping at
        every instant on the way that something is due at."""
        self._test_moves("air.advance")
        self._advance(nanoseconds(until, "air.advance"))

    def follow(self, until: int) -> None:
        """Moves the air's time to `until`, nanoseconds of the board's time, as `advance` does:
        the board's link, at each instant the run reaches, on an air on the board's clock."""
        self._advance(until)

    def _advance(self, until: int) -> None:
        with self._lock:
            if until < self._loop.now:
                raise ValueError(
                    f"the air is at {seconds(self._loop.now)}:"
                    f" it cannot go back to {seconds(until)}"
                )
            self._settle()
            while (next_time := self.quiet_until()) is not None and next_time <= until:
                if next_time <= self._loop.now:
                    raise AirError(
                        f"something on the air is due at {seconds(next_time)}, and the air is at"
                        f" {seconds(self._loop.now)} already"
                    )
                self._loop.now = next_time
                self._settle()
            self._loop.now = until
            self._settle()

    def _test_moves(self, call: str) -> None:
        """Refuses `call`, which would move the air, on an air on the board's clock."""
        if self.board_clock:
            raise AirError(
                f"{call}: this air is on the board's clock, which only the board's link moves:"
                " let the board run with board.wait() or board.wait_until()"
            )

    def wait(self, duration: Duration) -> None:
        """Lets the air run for `duration` from now."""
        self._test_moves("air.wait")
        self._advance(self._loop.now + nanoseconds(duration, "air.wait"))

    def run(self, awaitable: Awaitable[T], *, within: Duration, description: str = "") -> T:
        """Runs `awaitable` on the loop until it completes, for at most `within` of the board's
        time from now; returns its result, or raises what it raised. When `within` runs out
        first, the task is cancelled and `NeverHappened` names `description`."""
        try:
            self._test_moves("air.run")
        except AirError:
            # It will never run: closed, so that Python does not warn it was never awaited.
            if isinstance(awaitable, Coroutine):
                awaitable.close()
            raise
        return self._run(awaitable, nanoseconds(within, "air.run: within"), description)

    def at_once(self, awaitable: Awaitable[T], description: str = "") -> T:
        """Runs `awaitable` at the current instant, where it must complete, as `run` with a
        `within` of 0 does; on an air on the board's clock too, since the time does not move."""
        return self._run(awaitable, 0, description)

    def _run(self, awaitable: Awaitable[T], limit: int, description: str) -> T:
        async def awaited() -> T:
            return await awaitable

        with self._lock:
            start = self._loop.now
            deadline = start + limit
            task = self.start(awaited())
            while not task.done():
                next_time = self.quiet_until()
                if next_time is None or next_time > deadline:
                    self._advance(deadline)
                    if task.done():
                        break
                    task.cancel()
                    self._settle()
                    what = description or "the awaitable"
                    raise NeverHappened(
                        f"{what} did not complete within {seconds(limit)}: waited from"
                        f" {seconds(start)} to {seconds(self._loop.now)} of the board's time"
                    )
                self._advance(next_time)
            return task.result()

    def close(self) -> None:
        """Closes the loop: the air is over."""
        with self._lock:
            if not self._loop.is_closed():
                self._loop.close()

    def _settle(self) -> None:
        """Runs the loop at the current instant until nothing is left to do there, the parties
        polled first; raises what failed meanwhile."""
        if self._error is None:
            now = self._loop.now
            for party in self._parties:
                self._loop.call_soon(party.poll, now)
            self._next_timer = self._loop.run_idle()
        if self._error is not None:
            error = self._error
            message = f"{self._error_context}: {error}" if self._error_context else str(error)
            raise AirError(message) from error

    def _failed(self, loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        if self._error is None:
            exception = context.get("exception")
            if exception is None:
                self._error, self._error_context = AirError(context["message"]), ""
            else:
                self._error, self._error_context = exception, context.get("message", "")
