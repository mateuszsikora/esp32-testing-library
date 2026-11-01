"""A cellular modem: AT dialogue and power on the test's clock, without a host network.

The optional data source is the seam for the simulated PPP peer (#30). With no peer, data bytes
are kept for inspection and produce no response. See docs/features/modem-technical.md.
"""

import re
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from esp32tl.boards import Line
from esp32tl.interfaces.uart import Source
from esp32tl.link.boardlink import Output
from esp32tl.units import Duration, number

LATENCY_NS = 1_000_000
MAX_COMMAND = 4096
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


class Modem:
    """An initially powered, registered modem with -73 dBm signal and echo off.

    `power` receives (time in ns, powered) whenever RI should change. A firmware-triggered
    change answers after LATENCY_NS; a test's change is at its current instant. `pwrkey` holds
    the profile's pulse timing. `data` receives the data phase, never a host serial port.
    """

    def __init__(
        self,
        now: Callable[[], int],
        *,
        pwrkey: Line | None = None,
        power: Callable[[int, bool], None] = lambda t, powered: None,
        data: Source | None = None,
    ) -> None:
        self._now = now
        self._lock = threading.RLock()
        self._power = power
        self._key = pwrkey
        self._pressed: int | None = None
        self._supply = True
        self.powered = True
        self.registered = True
        self._coverage = True
        self._function = 1
        self._dbm = -73.0
        self._echo = False
        self._registration = dict.fromkeys(("CREG", "CGREG", "CEREG"), 0)
        self.data_mode = False
        self.data = data
        self._line = bytearray()
        self._overflow = False
        self._skip_lf = False
        self._pluses = bytearray()
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

    def signal(self, *, dbm: float) -> None:
        """Set the measured signal strength in dBm for subsequent CSQ queries."""
        value = number(dbm, "modem.signal: dbm")
        with self._lock:
            self._dbm = value

    def lose_registration(self) -> None:
        """Remove coverage and end data mode silently; queries report status 0."""
        with self._lock:
            self._coverage = self.registered = False
            self.data_mode = False
            self._pluses.clear()

    def register(self) -> None:
        """Restore coverage; register when powered with functionality 1."""
        with self._lock:
            self._coverage = True
            self.registered = self.powered and self._function == 1

    def power_off(self) -> None:
        """Switch off with the supply still available for PWRKEY."""
        with self._lock:
            self._set_power(self._now(), False)

    def power_on(self) -> None:
        """Restore supply and start the modem in command mode."""
        with self._lock:
            self._supply = True
            self._set_power(self._now(), True)

    def lose_power(self) -> None:
        """Remove supply, silence the UART and prevent PWRKEY from starting the modem."""
        with self._lock:
            self._supply = False
            self._set_power(self._now(), False)

    def restore_power(self) -> None:
        """Restore the supply; a PWRKEY pulse (or power_on) still has to start the modem."""
        with self._lock:
            self._supply = True

    def _set_power(self, t: int, powered: bool) -> None:
        self.powered = powered
        self.registered = powered and self._coverage
        self._function = 1
        self.data_mode = False
        self._echo = False
        self._registration = dict.fromkeys(self._registration, 0)
        self._line.clear()
        self._pluses.clear()
        self._overflow = self._skip_lf = False
        self._pressed = None
        self._power(t, powered)

    def driven(self, t: int, level: int | None) -> None:
        """PWRKEY at the ESP32 pad: toggle power on release of a sufficiently long pulse."""
        with self._lock:
            key = self._key
            if key is None or key.active is None:
                return
            if level == key.active:
                if self._pressed is None:
                    self._pressed = t
                return
            since, self._pressed = self._pressed, None
            duration = key.off_ms if self.powered else key.on_ms
            assert duration is not None
            if since is not None and t - since >= duration * 1_000_000 and self._supply:
                self._set_power(t + LATENCY_NS, not self.powered)

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
                    self.data_mode = False
            else:
                terminal = "ERROR"
        elif upper == "AT+COPS?":
            answer = '+COPS: 0,0,"ESP32TL",7' if self.registered else "+COPS: 0"
        elif re.fullmatch(r"ATDT?\*99[0-9*#]*#", upper) or upper.startswith("AT+CGDATA="):
            self.data_mode = self.registered
            terminal = "CONNECT" if self.registered else "NO CARRIER"
        elif upper.startswith(("AT+CGDCONT=", "AT+CTZU=", "AT+COPS=")):
            pass
        elif upper not in ANSWERS:
            fallback = True
        response = echo + (f"\r\n{answer}\r\n" if answer else "").encode()
        response += f"\r\n{terminal}\r\n".encode()
        self._commands.append(Command(Duration(t), command, response, fallback))
        return response

    def poll(self, now: int) -> Iterable[Output]:
        """Poll the optional data peer while in data mode."""
        with self._lock:
            return list(self.data.poll(now)) if self.data_mode and self.data is not None else ()

    def quiet_until(self) -> int | None:
        """The optional data peer's next deadline, or no deadline in command mode."""
        with self._lock:
            return self.data.quiet_until() if self.data_mode and self.data is not None else None

    def latency(self) -> int | None:
        """The response bound: 1 ms or the data peer's shorter latency."""
        with self._lock:
            latency = self.data.latency() if self.data_mode and self.data is not None else None
            return LATENCY_NS if latency is None else min(LATENCY_NS, latency)
