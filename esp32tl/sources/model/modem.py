"""A cellular modem: AT dialogue and power on the test's clock, without a host network.

The optional data source can be the simulated PPP peer. With no peer, data bytes
are kept for inspection and produce no response. See docs/features/modem-technical.md.
"""

import re
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from esp32tl.boards import Line
from esp32tl.interfaces import SourceError
from esp32tl.interfaces.uart import Source
from esp32tl.link.boardlink import Output
from esp32tl.net import Internet
from esp32tl.net.ntp import DEFAULT_UTC
from esp32tl.units import Duration, minutes, number, shown

LATENCY_NS = 1_000_000
MAX_COMMAND = 4096
QUARTER_NS = minutes(15).ns
# The A76XX manual's range for +CCLK's zone, in quarters of an hour.
MAX_ZONE = 96
NO_OFFSET = Duration(0)
ANSWERS = {
    "AT+CPIN?": "+CPIN: READY",
    "AT+CGMM": "ESP32TL-MODEM",
    "AT+CGMR": "ESP32TL 1.0",
    "AT+CGSN": "350000000000000",
    "AT+CIMI": "260000000000000",
}


@dataclass(frozen=True)
class Command:
    """A complete AT command, stamped at the UART chunk containing its terminator, and its reply."""

    at: Duration
    text: str
    response: bytes
    fallback: bool = False


@dataclass(frozen=True)
class Restart:
    """A run of the modem that ended at `at`, by `cause`: a RESET pulse, which starts the next at
    once; a PWRKEY pulse that switched it off; its supply going; or a power call of the test's."""

    at: Duration
    cause: Literal["reset", "pwrkey", "supply", "test"]


class Modem:
    """An initially powered, registered modem with -73 dBm signal and echo off.

    `power` receives (time in ns, powered) whenever RI should change. A firmware-triggered
    change answers after LATENCY_NS; a test's change is at its current instant. `pwrkey` and
    `reset` hold the profile's pulse timing. `data` receives the data phase, never a host serial
    port.
    """

    def __init__(
        self,
        now: Callable[[], int],
        *,
        pwrkey: Line | None = None,
        reset: Line | None = None,
        power: Callable[[int, bool], None] = lambda t, powered: None,
        data: Source | None = None,
        world: Callable[[], datetime] = lambda: DEFAULT_UTC,
    ) -> None:
        self._now = now
        self._world = world
        self._lock = threading.RLock()
        self._power = power
        self._key = pwrkey
        self._pressed: int | None = None
        self._reset = reset
        self._reset_pressed: int | None = None
        self._supply = True
        # The board's supply, which the modem is on: apart from the test's `lose_power`.
        self._board = True
        self.powered = True
        self.hung = False
        self._pwrkey_works = True
        self._restarts: list[Restart] = []
        self.registered = True
        self._coverage = True
        self._function = 1
        self._dbm = -73.0
        self._echo = False
        self._registration = dict.fromkeys(("CREG", "CGREG", "CEREG"), 0)
        self._wrong_by = 0
        self._zone = 0
        # An unset clock: what it read, and when.
        self._unset: tuple[datetime, int] | None = None
        self._format = 0
        self._operator = ("ESP32TL", "ESP32TL", "26000")
        self._act = 7
        self.data_mode = False
        self.data = data
        self._line = bytearray()
        self._overflow = False
        self._skip_lf = False
        self._pluses = bytearray()
        self._last_data = 0
        self._escape_at: int | None = None
        self._commands: list[Command] = []
        self._data_received: list[tuple[Duration, bytes]] = []

    def connect_power(self, power: Callable[[int, bool], None]) -> None:
        """Attach RI before the run and publish its initial level."""
        with self._lock:
            self._power = power
            power(self._now(), self.powered)

    @property
    def commands(self) -> list[Command]:
        """Every completed AT command and reply, as a snapshot."""
        with self._lock:
            return list(self._commands)

    @property
    def data_received(self) -> list[tuple[Duration, bytes]]:
        """Data-mode payloads received so far, as a snapshot."""
        with self._lock:
            return list(self._data_received)

    @property
    def restarts(self) -> tuple[Restart, ...]:
        """Each time a run of the modem ended, with what ended it."""
        with self._lock:
            return tuple(self._restarts)

    def signal(self, *, dbm: float) -> None:
        """Set the measured signal strength in dBm for subsequent CSQ queries."""
        value = number(dbm, "modem.signal: dbm")
        with self._lock:
            self._dbm = value

    def clock(self, *, wrong_by: Duration = NO_OFFSET, zone: Duration = NO_OFFSET) -> None:
        """Set +CCLK to the world's time in `zone`, `wrong_by` off it."""
        for what, value in (("wrong_by", wrong_by), ("zone", zone)):
            if not isinstance(value, Duration):
                raise TypeError(f"modem.clock: {what} is {value!r}, not a Duration")
        quarters, rest = divmod(zone.ns, QUARTER_NS)
        if rest or abs(quarters) > MAX_ZONE:
            raise ValueError(
                f"modem.clock: zone is {shown(zone.seconds / 60)} minutes: a whole number of"
                " quarters of an hour from -24 to +24 hours"
            )
        with self._lock:
            self._wrong_by, self._zone, self._unset = wrong_by.ns, quarters, None

    def clock_not_set(self, *, reads: datetime) -> None:
        """Make +CCLK read `reads` now, with no zone, and run on from there."""
        if not isinstance(reads, datetime) or reads.utcoffset() is None:
            raise TypeError(
                f"modem.clock_not_set: reads is {reads!r}, not a datetime with a timezone"
            )
        with self._lock:
            self._unset = (reads, self._now())

    def clock_set_by_network(self) -> None:
        """Back to the world's time, in zone +00."""
        self.clock()

    def operator(
        self,
        *,
        name: str = "ESP32TL",
        numeric: str = "26000",
        short: str | None = None,
        act: int = 7,
    ) -> None:
        """Set what +COPS names: formats 0, 1 and 2 give `name`, `short` (`name` unless given) and
        `numeric`, then the access technology `act`."""
        if not isinstance(numeric, str) or not re.fullmatch("[0-9]{5,6}", numeric):
            raise ValueError(f"modem.operator: numeric is {numeric!r}, not 5 or 6 digits")
        short = name if short is None else short
        for what, value in (("name", name), ("short", short)):
            if not isinstance(value, str) or not value or '"' in value:
                raise ValueError(f"modem.operator: {what} is {value!r}, not text for quotes")
        if isinstance(act, bool) or not isinstance(act, int) or act < 0:
            raise ValueError(f"modem.operator: act is {act!r}, not a whole number")
        with self._lock:
            self._operator, self._act = (name, short, numeric), act

    def hang(self, *, pwrkey: bool = True) -> None:
        """Stop answering, passing data and moving RI until a restart; with `pwrkey` False,
        PWRKEY does nothing either, until RESET or a loss of supply."""
        if not isinstance(pwrkey, bool):
            raise TypeError(f"modem.hang: pwrkey is {pwrkey!r}, not True or False")
        with self._lock:
            if not self.powered:
                raise SourceError("modem.hang: the modem is off")
            self.hung = True
            self._pwrkey_works = pwrkey
            if not pwrkey:
                self._pressed = None
            self._end_data()

    def lose_registration(self) -> None:
        """Remove coverage and end data mode silently; queries report status 0."""
        with self._lock:
            self._coverage = self.registered = False
            self._end_data()

    def register(self) -> None:
        """Restore coverage; register when powered with functionality 1."""
        with self._lock:
            self._coverage = True
            self.registered = self.powered and self._function == 1

    def power_off(self) -> None:
        """Switch off with the supply still available for PWRKEY."""
        with self._lock:
            self._set_power(self._now(), False, "test")

    def power_on(self) -> None:
        """Restore supply and start the modem in command mode; with the board's supply off it
        starts once that comes back and PWRKEY is pulsed."""
        with self._lock:
            self._supply = True
            if self._board:
                self._set_power(self._now(), True, "test")

    def lose_power(self) -> None:
        """Remove supply, silence the UART and prevent PWRKEY from starting the modem."""
        with self._lock:
            self._supply = False
            self._set_power(self._now(), False, "supply")

    def restore_power(self) -> None:
        """Restore the supply; a PWRKEY pulse (or power_on) still has to start the modem."""
        with self._lock:
            self._supply = True

    def supply(self, on: bool) -> None:
        """The board's supply, which the modem is on, went off or came back. Off, the modem
        goes off as with `lose_power`; back, it waits for its PWRKEY pulse, and still has no
        supply if the test took it with `lose_power`: the two are apart."""
        with self._lock:
            self._board = on
            if not on and self.powered:
                self._set_power(self._now(), False, "supply")

    def _set_power(
        self, t: int, powered: bool, cause: Literal["reset", "pwrkey", "supply", "test"]
    ) -> None:
        # What the test says before the board's first instant is how the modem starts.
        if self.powered and t > 0:
            self._restarts.append(Restart(Duration(t), cause))
        self.powered = powered
        self.hung = False
        self._pwrkey_works = True
        self.registered = powered and self._coverage
        self._function = 1
        self._end_data(t)
        self._echo = False
        self._registration = dict.fromkeys(self._registration, 0)
        self._format = 0
        self._line.clear()
        self._pluses.clear()
        self._overflow = self._skip_lf = False
        self._pressed = self._reset_pressed = None
        self._power(t, powered)

    def driven(self, t: int, level: int | None) -> None:
        """PWRKEY at the ESP32 pad: toggle power on release of a sufficiently long pulse."""
        with self._lock:
            key = self._key
            if key is None or key.active is None or not self._pwrkey_works:
                return
            if level == key.active:
                if self._pressed is None:
                    self._pressed = t
                return
            since, self._pressed = self._pressed, None
            duration = key.off_ms if self.powered else key.on_ms
            assert duration is not None
            if (
                since is not None
                and t - since >= duration * 1_000_000
                and self._supply
                and self._board
            ):
                self._set_power(t + LATENCY_NS, not self.powered, "pwrkey")

    def reset_driven(self, t: int, level: int | None) -> None:
        """RESET at the ESP32 pad: restart a running modem on release of a sufficiently long
        pulse. A modem that is off stays off."""
        with self._lock:
            line = self._reset
            if line is None or line.active is None:
                return
            if level == line.active:
                if self._reset_pressed is None:
                    self._reset_pressed = t
                return
            since, self._reset_pressed = self._reset_pressed, None
            assert line.pulse_ms is not None
            if since is not None and t - since >= line.pulse_ms * 1_000_000 and self.powered:
                self._set_power(t + LATENCY_NS, True, "reset")

    def receive(self, t: int, data: bytes) -> Iterable[Output]:
        """Consume UART bytes at their chunk timestamp and return timed replies."""
        with self._lock:
            if not self.powered:
                return ()
            outputs: list[Output] = []
            payload = bytearray()

            def flush() -> None:
                if payload:
                    chunk = bytes(payload)
                    payload.clear()
                    self._data_received.append((Duration(t), chunk))
                    if self.data is not None:
                        outputs.extend(self.data.receive(t, chunk))

            for byte in data:
                if self._skip_lf:
                    self._skip_lf = False
                    if byte == 10:
                        continue
                if self.data_mode:
                    if isinstance(self.data, Internet):
                        # PPP is binary: +++ inside a frame must reach its FCS unchanged.
                        guarded = t - self._last_data >= 1_000_000_000
                        if byte == 43 and (guarded or self._pluses) and len(self._pluses) < 3:
                            self._pluses.append(byte)
                            if len(self._pluses) == 3:
                                self._escape_at = t + 1_000_000_000
                        else:
                            payload.extend(self._pluses)
                            self._pluses.clear()
                            self._escape_at = None
                            payload.append(byte)
                        self._last_data = t
                        continue
                    if byte == 43:
                        self._pluses.append(byte)
                        if len(self._pluses) == 3:
                            flush()
                            self._pluses.clear()
                            self.data_mode = False
                            outputs.append((t + LATENCY_NS, b"\r\nOK\r\n"))
                    else:
                        payload.extend(self._pluses)
                        self._pluses.clear()
                        payload.append(byte)
                    continue
                if byte in (10, 13):
                    self._skip_lf = byte == 13
                    if self._line and not self._overflow:
                        response = self._command(t, bytes(self._line))
                        if response:
                            outputs.append((t + LATENCY_NS, response))
                    self._line.clear()
                    self._overflow = False
                elif not self._overflow:
                    self._line.append(byte)
                    # Firmware sends an unterminated escape before AT to ensure command mode.
                    if self._line == b"+++":
                        self._line.clear()
                    elif len(self._line) > MAX_COMMAND:
                        self._line.clear()
                        self._overflow = True
            flush()
            return outputs

    def _command(self, t: int, raw: bytes) -> bytes:
        command = raw.decode("ascii", errors="replace")
        upper = command.upper()
        if not upper.startswith("AT"):
            return b""
        if self.hung:
            self._commands.append(Command(Duration(t), command, b""))
            return b""
        echo = raw + b"\r\n" if self._echo else b""
        answer = ANSWERS.get(upper, "")
        fallback = False
        terminal = "OK"
        if upper in ("AT", "ATE0", "ATE1"):
            if upper != "AT":
                self._echo = upper == "ATE1"
        elif upper == "AT+CSQ":
            csq = max(0, min(31, int((self._dbm + 113) // 2)))
            answer = f"+CSQ: {csq},99"
        elif upper[3:-1] in self._registration and upper.endswith("?"):
            name = upper[3:-1]
            answer = f"+{name}: {self._registration[name]},{int(self.registered)}"
        elif upper[3:].partition("=")[0] in self._registration and "=" in upper:
            name, _, mode = upper[3:].partition("=")
            if mode == "?":
                answer = f"+{name}: (0-2)"
            elif mode in ("0", "1", "2"):
                self._registration[name] = int(mode)
            else:
                terminal = "ERROR"
        elif upper == "AT+CFUN?":
            answer = f"+CFUN: {self._function}"
        elif upper.startswith("AT+CFUN="):
            value = upper[8:].split(",")[0]
            if value in ("0", "1", "4"):
                self._function = int(value)
                self.registered = value == "1" and self._coverage
                if not self.registered:
                    self._end_data(t)
            else:
                terminal = "ERROR"
        elif upper == "AT+COPS?":
            name = self._operator[self._format]
            answer = f'+COPS: 0,{self._format},"{name}",{self._act}'
            answer = answer if self.registered else "+COPS: 0"
        elif upper == "AT+COPS=3" or upper.startswith("AT+COPS=3,"):
            value = upper.partition(",")[2]
            if value in ("0", "1", "2"):
                self._format = int(value)
            else:
                terminal = "ERROR"
        elif upper == "AT+CCLK?":
            answer = self._cclk(t)
        elif re.fullmatch(r"ATDT?\*99[0-9*#]*#", upper) or upper.startswith("AT+CGDATA="):
            self.data_mode = self.registered
            terminal = "CONNECT" if self.registered else "NO CARRIER"
            if self.data_mode and isinstance(self.data, Internet):
                self._last_data = t
                self.data.start(t + LATENCY_NS)
        elif upper.startswith(("AT+CGDCONT=", "AT+CTZU=", "AT+COPS=")):
            pass
        elif upper not in ANSWERS:
            fallback = True
        response = echo + (f"\r\n{answer}\r\n" if answer else "").encode()
        response += f"\r\n{terminal}\r\n".encode()
        self._commands.append(Command(Duration(t), command, response, fallback))
        return response

    def _cclk(self, t: int) -> str:
        if self._unset is not None:
            reads, since = self._unset
            stamp = reads + timedelta(microseconds=(t - since) // 1000)
            return f'+CCLK: "{stamp:%y/%m/%d,%H:%M:%S}"'
        offset = t + self._wrong_by + self._zone * QUARTER_NS
        stamp = self._world() + timedelta(microseconds=offset // 1000)
        return f'+CCLK: "{stamp:%y/%m/%d,%H:%M:%S}{self._zone:+03d}"'

    def poll(self, now: int) -> Iterable[Output]:
        """Poll the optional data peer while in data mode."""
        with self._lock:
            if self._escape_at is not None and now >= self._escape_at:
                self._end_data(now)
                return [(now, b"\r\nOK\r\n")]
            return list(self.data.poll(now)) if self.data_mode and self.data is not None else ()

    def quiet_until(self) -> int | None:
        """The optional data peer's next deadline, or no deadline in command mode."""
        with self._lock:
            deadline = self.data.quiet_until() if self.data_mode and self.data is not None else None
            return min((t for t in (deadline, self._escape_at) if t is not None), default=None)

    def latency(self) -> int | None:
        """The response bound: 1 ms or the data peer's shorter latency."""
        with self._lock:
            latency = self.data.latency() if self.data_mode and self.data is not None else None
            return LATENCY_NS if latency is None else min(LATENCY_NS, latency)

    def _end_data(self, at: int | None = None) -> None:
        self.data_mode = False
        self._pluses.clear()
        self._escape_at = None
        if isinstance(self.data, Internet):
            self.data.stop(at)
