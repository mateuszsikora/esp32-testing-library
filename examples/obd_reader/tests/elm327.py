"""An ELM327-compatible OBD-II adapter, as the source of an `SppPeer`: it answers the commands the
reader writes on the SPP channel for a `Vehicle` the test changes, and keeps every command it was
sent, which is what the tests read at the peer. No waits, no board. A `ReactiveModel`: its times are
`Duration`s on the board's clock, and it sends nothing unprompted."""

from collections.abc import Iterable
from dataclasses import dataclass, field

from esp32tl.sources.model import ReactiveModel, Timed
from esp32tl.units import Duration, ms

VERSION = "ELM327 v1.5"
# How long it takes to answer: a command, and ATZ, which restarts it.
ANSWER_AFTER = ms(50)
RESET_TIME = ms(500)
PROMPT = b">"
# With protocol 0, automatic, the adapter looks for the vehicle's protocol at the first OBD-II
# request after power-on, ATZ or ATSP, and says so before the answer. A vehicle that does not
# answer, the ignition off, leaves it with none, and it looks again at the next request.
SEARCHING = "SEARCHING..."
UNABLE_TO_CONNECT = SEARCHING + "\rUNABLE TO CONNECT"


def is_hex(text: str) -> bool:
    return all(c in "0123456789ABCDEF" for c in text)


@dataclass
class Vehicle:
    """What the diagnostic port answers. A reading of `None` is a PID the vehicle does not
    support; with `ignition` off nothing answers."""

    ignition: bool = True
    rpm: float | None = 850.0
    speed: int | None = 0  # km/h
    coolant: int | None = 90  # °C

    def supported(self) -> dict[int, bytes]:
        """The data bytes of each mode 01 PID it answers, by PID."""
        data: dict[int, bytes] = {}
        if self.rpm is not None:
            data[0x0C] = round(self.rpm * 4).to_bytes(2, "big")
        if self.speed is not None:
            data[0x0D] = bytes([self.speed])
        if self.coolant is not None:
            data[0x05] = bytes([self.coolant + 40])
        return data


@dataclass(frozen=True)
class Request:
    at: Duration
    command: str


@dataclass
class Elm327(ReactiveModel):
    """The adapter's command interpreter. Each command is a line ending in a carriage return; its
    answer is the command's echo while echo is on (`ATE1`, the state after `ATZ`), the answer's
    lines, each ending in a carriage return, an empty line and the prompt `>`, `answer_after` after
    the command came (`reset_time` for `ATZ`). `version` is what it names itself after `ATZ` and
    `ATI`. With `silent` it hears every command and answers none."""

    vehicle: Vehicle = field(default_factory=Vehicle)
    answer_after: Duration = ANSWER_AFTER
    reset_time: Duration = RESET_TIME
    version: str = VERSION
    silent: bool = False
    echo: bool = True
    protocol_found: bool = field(default=False, init=False)
    requests: list[Request] = field(default_factory=list, init=False)
    _pending: bytes = field(default=b"", init=False)

    def commands(self) -> list[str]:
        """What the reader asked, in order."""
        return [request.command for request in self.requests]

    def receive(self, at: Duration, data: bytes) -> Iterable[Timed]:
        self._pending += data
        answers = []
        while b"\r" in self._pending:
            line, _, self._pending = self._pending.partition(b"\r")
            command = line.decode("ascii", "replace").strip().upper().replace(" ", "")
            self.requests.append(Request(at, command))
            if self.silent:
                continue
            echo = line + b"\r" if self.echo else b""
            text, delay = self.answer(command)
            answers.append((at + delay, echo + text.encode("ascii") + b"\r\r" + PROMPT))
        return answers

    def answer(self, command: str) -> tuple[str, Duration]:
        """The answer to `command` and how long it takes; changes the adapter's settings."""
        if command == "ATZ":
            self.echo = True
            self.protocol_found = False
            return self.version, self.reset_time
        if command == "ATI":
            return self.version, self.answer_after
        if command in ("ATE0", "ATE1"):
            self.echo = command == "ATE1"
            return "OK", self.answer_after
        if command.startswith("ATSP") and len(command) == 5:
            self.protocol_found = False
            return "OK", self.answer_after
        if len(command) == 4 and command.startswith("01") and is_hex(command[2:]):
            return self._mode01(int(command[2:], 16)), self.answer_after
        return "?", self.answer_after

    def _mode01(self, pid: int) -> str:
        if not self.vehicle.ignition:
            return UNABLE_TO_CONNECT
        searching = "" if self.protocol_found else SEARCHING + "\r"
        self.protocol_found = True
        supported = self.vehicle.supported()
        if pid == 0x00:
            # PIDs 01 to 20: PID n is bit 32 - n, the most significant byte first.
            bits = sum(1 << (32 - n) for n in supported)
            data = bits.to_bytes(4, "big")
        elif pid in supported:
            data = supported[pid]
        else:
            return searching + "NO DATA"
        return searching + " ".join(f"{byte:02X}" for byte in bytes([0x41, pid]) + data)
