"""The chip's Bluetooth, `bluetooth`: the guest's controller on the air around the board, on the
board's clock. The `bt` fixture is it.

On the `esp32sim` backend the firmware's host, ESP-IDF's Bluedroid, talks to its controller through
esp32sim's VHCI, which the runner's `--bt` forwards on the link: every H4 packet the host sends
reaches `receive` at its time on the board's clock, and what the guest's controller sends its host
goes out `LATENCY_NS` after the instant it made it. The controller is a `GuestController` on an
`Air` whose time is the board's: the link moves it from turn to turn, and the test reaches it, to
put an `SppPeer` or a `BluetoothDevice` on it or to switch one off, while the board holds the link
in a turn, at the board's current instant.

`tap(path)` records the HCI between the firmware's host and its controller, every packet both ways
at the time it crossed the link, for a btsnoop file the backend writes at the end of the run.
"""

import os
import threading
from collections.abc import Callable, Iterable
from pathlib import Path

from bumble import hci

from esp32tl import recording
from esp32tl.backends.clock import seconds
from esp32tl.bluetooth.air import Air
from esp32tl.bluetooth.controller import Controller, Pairing, UnknownCommand
from esp32tl.bluetooth.guest import ADDRESS, GuestController
from esp32tl.boards.capabilities import Request
from esp32tl.interfaces import SourceError
from esp32tl.interfaces.uart import same_file
from esp32tl.link.boardlink import Direction, Interface, Output, counted
from esp32tl.link.protocol import HCI_MAX
from esp32tl.report import RunLog, board_time
from esp32tl.tap import Tap

NAME = "bluetooth"

# How long the guest's controller takes to send what it makes, 10 µs of the board's time: within
# the microseconds a controller takes to answer a command, and far below the 10 ms the host sleeps
# between two polls of the VHCI when it has nothing to read (protocol 5.22), so that it reads every
# answer at its next poll. At 0 every packet would take a turn of its own.
LATENCY_NS = 10_000

# What a tap's header says was at the far end of the firmware's host: esp32tl's controller, on the
# air with the test's devices.
TAP_SOURCE = "model:bluetooth"


class BluetoothError(RuntimeError):
    """What the guest's controller sends that the link cannot carry: a packet due before the turn
    it goes out in, or longer than esp32sim's VHCI carries."""


class Bluetooth(Interface):
    """The guest's Bluetooth controller at `address`, as the link drives it, on `air`, which is
    made on the test's first look at it, `use`. Until then the interface is not there: it asks
    for no `--bt`, sends nothing and bounds no turn. `asked` is called with the request at that
    first look, and refuses it by raising, as for a UART's source, and with the request of a tap
    at `tap`. A tap's path is taken from `directory`, the test file's, and claimed in `files`, the
    backend's mapping of the files the run reads and writes, with the routes' and the SD card's.

    The link calls `receive`, `arrive`, `poll`, `quiet_until` and `latency` from its thread; the
    backend calls `supply` and `chip_reset` when the board's supply switches and the chip resets.
    The air's lock keeps them apart from the test's calls on the air."""

    def __init__(
        self,
        *,
        asked: Callable[[Request], None] = lambda request: None,
        log: RunLog | None = None,
        address: str = ADDRESS,
        latency: int = LATENCY_NS,
        directory: Path | None = None,
        files: dict[Path, tuple[str, str]] | None = None,
    ) -> None:
        super().__init__(NAME, None)
        self.asked = asked
        self.log = log
        self.directory = Path.cwd() if directory is None else directory
        self.files = {} if files is None else files
        self.tapped: Tap | None = None
        self.address = address
        self._latency = latency
        self._lock = threading.Lock()
        self._air: Air | None = None
        self._controller: GuestController | None = None
        # What the controller made for its host and the link has not taken, with the air's time.
        self._out: list[tuple[int, bytes]] = []
        self.packets_from_host = 0
        self.bytes_from_host = 0
        self.packets_to_host = 0
        self.bytes_to_host = 0

    @property
    def bluetooth(self) -> bool:
        return True

    # The test's side.

    def use(self) -> Air:
        """Puts the guest's controller on an air on the board's clock, once: the test's first look
        at the interface, before `board.power_on()`. Returns the air."""
        with self._lock:
            if self._air is not None:
                return self._air
        self.asked(Request(NAME, source="model"))
        with self._lock:
            if self._air is None:
                air = Air(board_clock=True)
                controller = air.call(lambda: GuestController("esp32", air.link, self.address))
                controller.host = self
                self._air, self._controller = air, controller
                if self.log is not None:
                    self.log.test(0, f"bluetooth: the guest's controller at {self.address}")
            return self._air

    def tap(self, path: str | os.PathLike[str]) -> Tap:
        """Records the HCI between the firmware's host and its controller from power-on, every H4
        packet both ways, and writes it once the run has ended to `path`, relative to the test
        file's directory, as a btsnoop file, with its header in `path` and `.json`
        (`docs/recording-format.md`). The directory must exist; both files are replaced. Returns
        the `Tap`, whose `events` the run fills: `out` from the host, `in` to it."""
        if self.tapped is not None:
            raise SourceError(f"{NAME} is tapped already: one tap per test")
        # The tap first, so that one asked for too late is refused as a tap, as a UART's is.
        self.asked(Request(NAME, tap=True))
        self.use()
        file = self.directory / path
        if not file.parent.is_dir():
            raise FileNotFoundError(
                f"{NAME}: cannot tap into {path}: {file.parent} is not a directory"
            )
        header = recording.sidecar(file)
        for claimed, (kind, route) in self.files.items():
            for mine in (file, header):
                if same_file(mine, claimed):
                    raise SourceError(
                        f"{NAME}: cannot tap into {path}: {mine} is the {kind} of {route}"
                    )
        with self._lock:
            if self.tapped is not None:
                raise SourceError(f"{NAME} is tapped already: one tap per test")
            self.tapped = Tap(NAME, file)
            self.files[file.resolve()] = ("tap", NAME)
            self.files[header.resolve()] = ("tap", NAME)
        if self.log is not None:
            self.log.test(0, f"{NAME}.tap({str(path)!r})")
        return self.tapped

    @property
    def used(self) -> bool:
        """Whether the test has looked at the interface: the run then has `--bt`."""
        return self._air is not None

    @property
    def air(self) -> Air:
        """The air around the board, on its clock, where the test puts its devices."""
        return self.use()

    @property
    def controller(self) -> GuestController:
        """The guest's controller."""
        self.use()
        assert self._controller is not None
        return self._controller

    def close(self) -> None:
        """The air is over, at the end of the test."""
        if self._air is not None:
            self._air.close()

    # The backend's side.

    def supply(self, on: bool) -> None:
        """The board's supply switches, at the air's time: off, the controller leaves the air and
        loses all it had; on, it is back, at its defaults. From the test's thread, held."""
        air, controller = self._air, self._controller
        if air is None or controller is None:
            return
        air.call(controller.chip_on if on else controller.chip_off)
        if not on:
            self._out.clear()

    def chip_reset(self, t: int) -> None:
        """The chip reset at `t`: the controller with it, and what it had made for the host that
        is gone. From the link's thread."""
        air, controller = self._air, self._controller
        if air is None or controller is None:
            return
        air.follow(t)
        air.call(controller.chip_reset)
        self._out.clear()

    # Bumble's `TransportSink`: what the controller sends its host.

    def on_packet(self, packet: bytes) -> None:
        assert self._air is not None
        self._out.append((self._air.loop.now, bytes(packet)))

    # The link's side.

    def receive(self, t: int, direction: Direction, data: bytes) -> Iterable[Output]:
        """The H4 packet `data` of the guest's host, at `t`: the air goes to `t`, and the controller
        takes it there. What it answers goes out at the next `poll`."""
        air, controller = self._air, self._controller
        if air is None or controller is None:
            raise BluetoothError(
                "the runner sent hci_tx, and the test gave the board no Bluetooth: the run has no"
                " --bt"
            )
        air.follow(t)
        self.packets_from_host += 1
        self.bytes_from_host += len(data)
        if self.tapped is not None:
            self.tapped.record(t, "out", data)
        air.call(lambda: controller.on_packet(data))
        return ()

    def arrive(self, now: int) -> None:
        if self._air is not None:
            self._air.follow(now)

    def poll(self, now: int) -> Iterable[Output]:
        """What the controller made by `now`, each `latency` after the instant it made it: never
        before the turn, by rule 1 of the protocol (§6) and its bound on the turn (§7), so one that
        would be is an error."""
        air = self._air
        if air is None:
            return ()
        air.follow(now)
        out, self._out = self._out, []
        outputs = []
        for at, packet in out:
            due = at + self._latency
            if due < now:
                raise BluetoothError(
                    f"the guest's controller made {_named(packet)} at {seconds(at)}, due at"
                    f" {seconds(due)}, before the turn at {seconds(now)}: the link bounds every"
                    " turn by the interfaces' quiet_until and latency (protocol §6, rule 1, and §7)"
                )
            if not 1 <= len(packet) <= HCI_MAX:
                raise BluetoothError(
                    f"the guest's controller made {_named(packet)} of {len(packet)} bytes at"
                    f" {seconds(at)}; esp32sim's VHCI carries 1 to {HCI_MAX}"
                )
            self.packets_to_host += 1
            self.bytes_to_host += len(packet)
            if self.tapped is not None:
                self.tapped.record(due, "in", packet)
            outputs.append((due, packet))
        return outputs

    def quiet_until(self) -> int | None:
        """The air's next instant, plus `latency`: before then the controller makes nothing."""
        air = self._air
        if air is None:
            return None
        due = air.quiet_until()
        return None if due is None else due + self._latency

    def latency(self) -> int | None:
        return None if self._air is None else self._latency

    # The report.

    def unknown_commands(self) -> list[UnknownCommand]:
        """UnknownCommand records from the guest's host, with `t: Duration` on the board clock."""
        if self._controller is None:
            return []
        return list(self._controller.unknown_commands)

    def pairings(self) -> list[Pairing]:
        """The pairings of the guest's controller that ended, in order: with whom, how, and
        whether they succeeded."""
        if self._controller is None:
            return []
        return list(self._controller.pairings)

    def devices(self) -> list[tuple[str, str, bool]]:
        """The other devices made on the air, `(name, address, present)`, in the order of their
        addresses: `present` is false for one switched off or out of range now."""
        if self._air is None:
            return []
        others = [
            c
            for c in self._air.link.controllers
            if isinstance(c, Controller) and c is not self._controller
        ]
        others.sort(key=lambda c: bytes(reversed(bytes(c.public_address))))
        link = self._air.link
        return [
            (c.name, str(c.public_address).removesuffix("/P"), link.present(c.public_address))
            for c in others
        ]

    def report(self) -> list[str]:
        """The guest's controller, the packets each way, the commands it did not know, its
        pairings and the devices made on the air, those off it now marked."""
        if self._air is None:
            return ["bluetooth: not used"]
        lines = [
            f"bluetooth: the guest's controller at {self.address};"
            f" {counted(self.packets_from_host, 'packet')} ({self.bytes_from_host} bytes) from"
            f" its host, {counted(self.packets_to_host, 'packet')} ({self.bytes_to_host} bytes)"
            " to it" + ("" if self.tapped is None else f"; tapped into {self.tapped.path}")
        ]
        unknown = self.unknown_commands()
        if unknown:
            lines.append(f"  unknown commands ({len(unknown)}):")
            lines += [f"    {board_time(t.ns)}  {name} ({op:#06x})" for t, op, name in unknown]
        pairings = self.pairings()
        if pairings:
            lines.append(f"  pairings ({len(pairings)}):")
            lines += [f"    {board_time(p.t.ns)}  {_pairing(p)}" for p in pairings]
        devices = [f"{n} ({a}{'' if on else ', off the air'})" for n, a, on in self.devices()]
        lines.append("  devices: " + (", ".join(devices) or "none but the guest's"))
        return lines

    def properties(self) -> dict[str, object] | None:
        if self._air is None:
            return None
        return {
            "address": self.address,
            "packets_from_host": self.packets_from_host,
            "bytes_from_host": self.bytes_from_host,
            "packets_to_host": self.packets_to_host,
            "bytes_to_host": self.bytes_to_host,
            "tap": None if self.tapped is None else str(self.tapped.path),
            "unknown_commands": [
                {"t_ns": t.ns, "op_code": op, "name": name}
                for t, op, name in self.unknown_commands()
            ],
            "pairings": [
                {
                    "t_ns": p.t.ns,
                    "peer": p.peer,
                    "initiator": p.initiator,
                    "method": p.method,
                    "number": p.number,
                    "status": int(p.status),
                    "authenticated": p.authenticated,
                }
                for p in self.pairings()
            ],
            "devices": [{"name": n, "address": a, "present": on} for n, a, on in self.devices()],
        }


def _pairing(pairing: Pairing) -> str:
    """A pairing of the guest's, as the report shows it: `with 00:00:00:00:5B:01, by
    just_works, 238680: succeeded`."""
    who = "with" if pairing.initiator else "from"
    number = "" if pairing.number is None else f", {pairing.number:06d}"
    outcome = (
        "succeeded"
        if pairing.succeeded
        else f"{hci.HCI_ErrorCode(pairing.status).name} ({pairing.status:#04x})"
    )
    return f"{who} {pairing.peer}, by {pairing.method}{number}: {outcome}"


def _named(packet: bytes) -> str:
    """An H4 packet, by its kind."""
    kinds = {1: "a command", 2: "ACL data", 3: "SCO data", 4: "an event", 5: "ISO data"}
    return kinds.get(packet[0], "a packet") if packet else "an empty packet"
