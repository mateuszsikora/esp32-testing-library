"""A part at the far end of a UART that changes its line rate when told, as a modem does on
`AT+IPR`: the models firmware/uart_rate_switch is tested against (`test_uart_rates.py`). A module
of its own, so that a test run in a pytester subprocess can take it as it is."""

from collections.abc import Iterable

from esp32tl import ReactiveModel, Timed, TimedAtRate
from esp32tl.units import Duration, ms

# The rate the part starts at, from power-on.
DEFAULT_BAUD = 9600


def same_rate(baud: int | None, rate: int) -> bool:
    """Whether a UART at `rate` reads what was sent at `baud`: within the 3 % the runner allows."""
    return baud is not None and abs(baud - rate) * 100 <= rate * 3


class RateSwitchingPart(ReactiveModel):
    """A part that speaks AT at a line rate of its own, `DEFAULT_BAUD` from power-on, and echoes
    what it hears, as a modem does with echo on (ATE1, the factory default). It answers `OK`
    `answer_after` after each command line, and `AT+IPR=<n>` at its rate before it changes to
    `<n>`, which everything after goes in at. What the firmware transmits at another rate it
    echoes too, as the garbage a UART reads of it, and takes as no command. It goes back to
    `DEFAULT_BAUD` when its supply goes off.

    `rate` is its rate now; `lines` every command line it took, with the rate it came at;
    `garbled` the instant and the rate of each transmission it heard at another rate; `routes`
    each change of the firmware's routes on its wire."""

    answer_after = ms(1)

    def __init__(self) -> None:
        self.rate = DEFAULT_BAUD
        self.lines: list[tuple[int, bytes]] = []
        self.garbled: list[tuple[Duration, int | None]] = []
        self.routes: list[tuple[Duration, int | None, bool, bool]] = []
        self._heard_at: int | None = None
        self._line = b""
        self._on = True

    def transmitted(self, at: Duration, baud: int | None) -> None:
        self._heard_at = baud

    def configured(self, at: Duration, baud: int | None, transmits: bool, receives: bool) -> None:
        self.routes.append((at, baud, transmits, receives))

    def receive(self, at: Duration, data: bytes) -> Iterable[Timed | TimedAtRate]:
        if not self._on:
            return []
        answer_at = at + self.answer_after
        answers: list[Timed | TimedAtRate] = [(answer_at, data, self.rate)]
        if not same_rate(self._heard_at, self.rate):
            self.garbled.append((at, self._heard_at))
            self._line = b""
            return answers
        self._line += data
        while b"\r\n" in self._line:
            line, _, self._line = self._line.partition(b"\r\n")
            self.lines.append((self.rate, line))
            answers.append((answer_at, b"\r\nOK\r\n", self.rate))
            if line.startswith(b"AT+IPR="):
                self.rate = int(line.removeprefix(b"AT+IPR="))
        return answers

    def supply(self, on: bool) -> None:
        self._on = on
        self._line = b""
        if not on:
            self.rate = DEFAULT_BAUD


class RateSwitchingModem(RateSwitchingPart):
    """The same part, which takes the frame errors its echo meets in the chip when the firmware
    listens at another rate, so that they do not fail the run: `dropped` is each `frm_err`, its
    instant, its count and the firmware's rate."""

    def __init__(self) -> None:
        super().__init__()
        self.dropped: list[tuple[Duration, int, int | None]] = []

    def frame_errors(self, at: Duration, count: int, baud: int | None) -> None:
        self.dropped.append((at, count, baud))
