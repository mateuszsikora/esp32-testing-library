"""The profile's uart.modem route, a replay or a cellular modem described by the test."""

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path

from esp32tl.boards import LineRoute
from esp32tl.boards.capabilities import Request
from esp32tl.interfaces import SourceError
from esp32tl.interfaces.condition import Condition, FirstFrom
from esp32tl.interfaces.lines import Lines
from esp32tl.interfaces.uart import Uart
from esp32tl.link.boardlink import Wire
from esp32tl.net import Internet
from esp32tl.net.handlers import reraise
from esp32tl.net.ntp import DEFAULT_UTC
from esp32tl.report import RunLog, board_time
from esp32tl.sources.model.modem import (
    LATENCY_NS,
    NO_OFFSET,
    Mode,
    ModeChange,
    ModemCommand,
    ModemPower,
    ModemRestart,
)
from esp32tl.sources.model.modem import Modem as ModemModel
from esp32tl.units import Duration


class Modem(Uart):
    """The first successful model call before power-on selects the model; later calls change it
    on the test's clock. Merely requesting the fixture keeps the route silent. `bridging` goes to
    the network, which calls it before a bridge to a real service is made (`Internet.bridge`), and
    so does `test_error`, which takes an exception of the test's code that the network ran while
    the board advanced."""

    def __init__(
        self,
        name: str,
        wire: Wire,
        *,
        clock: str | Callable[[], str],
        directory: Path,
        now: Callable[[], int],
        lines: Lines,
        world: Callable[[], datetime] = lambda: DEFAULT_UTC,
        pins: Mapping[str, LineRoute],
        asked: Callable[[Request], None] = lambda request: None,
        log: RunLog | None = None,
        files: dict[Path, tuple[str, str]] | None = None,
        bridging: Callable[[str], None] = lambda call: None,
        test_error: Callable[[BaseException], None] = reraise,
    ) -> None:
        super().__init__(
            name, wire, clock=clock, directory=directory, asked=asked, log=log, files=files
        )
        self._now = now
        self._bridging = bridging
        self._test_error = test_error
        self._world = world
        self._lines = lines
        self._pins = pins

    def network(self) -> Internet:
        """Select the modem and its simulated network before power-on, or return it later."""
        if isinstance(self.source, ModemModel) and isinstance(self.source.data, Internet):
            return self.source.data
        if not isinstance(self.source, ModemModel):
            # Before the backend's checks: after power-on they would say a source can still come.
            self._refuse_source("modem")
        self.asked(Request(self.name, source="model"))
        self._tell(lambda model: None, "network()")
        network = Internet(
            self._now, self._world, bridging=self._bridging, test_error=self._test_error
        )
        self.modem_model().data = network
        return network

    @property
    def lossless(self) -> bool:
        """PPP cannot recover a UART overflow in this simulated network."""
        return isinstance(self.source, ModemModel) and isinstance(self.source.data, Internet)

    def signal(self, *, dbm: float) -> None:
        """Set the measured signal strength in dBm for subsequent CSQ queries."""
        self._tell(lambda model: model.signal(dbm=dbm), f"signal(dbm={dbm!r})")

    def clock(self, *, wrong_by: Duration = NO_OFFSET, zone: Duration = NO_OFFSET) -> None:
        """Set +CCLK to the world's time in `zone`, `wrong_by` off it."""
        self._tell(
            lambda model: model.clock(wrong_by=wrong_by, zone=zone),
            f"clock(wrong_by={wrong_by!r}, zone={zone!r})",
        )

    def clock_not_set(self, *, reads: datetime) -> None:
        """Make +CCLK read `reads` now, with no zone, and run on from there."""
        self._tell(
            lambda model: model.clock_not_set(reads=reads), f"clock_not_set(reads={reads!r})"
        )

    def clock_set_by_network(self) -> None:
        """Back to the world's time, in zone +00."""
        self._tell(ModemModel.clock_set_by_network, "clock_set_by_network()")

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
        self._tell(
            lambda model: model.operator(name=name, numeric=numeric, short=short, act=act),
            f"operator(name={name!r}, numeric={numeric!r}, short={short!r}, act={act!r})",
        )

    def hang(self, *, pwrkey: bool = True) -> None:
        """Stop answering, passing data and moving RI until a restart; with `pwrkey` False,
        PWRKEY does nothing either, until that restart."""
        self._tell(lambda model: model.hang(pwrkey=pwrkey), f"hang(pwrkey={pwrkey!r})")

    def drop_call(self, *, terminate: bool = False) -> None:
        """End the data call and stay registered: NO CARRIER, then command mode. With `terminate`,
        first an LCP Terminate-Request."""
        self._tell(
            lambda model: model.drop_call(terminate=terminate),
            f"drop_call(terminate={terminate!r})",
        )

    def lose_registration(self) -> None:
        """Remove coverage and end data mode silently; queries report status 0."""
        self._tell(ModemModel.lose_registration, "lose_registration()")

    def register(self) -> None:
        """Restore coverage; register when powered with functionality 1."""
        self._tell(ModemModel.register, "register()")

    def power_on(self) -> None:
        """Restore supply and start the modem in command mode."""
        self._tell(ModemModel.power_on, "power_on()")

    def power_off(self) -> None:
        """Switch off with the supply still available for PWRKEY."""
        self._tell(ModemModel.power_off, "power_off()")

    def lose_power(self) -> None:
        """Remove supply, silence the UART and prevent PWRKEY from starting the modem."""
        self._tell(ModemModel.lose_power, "lose_power()")

    def restore_power(self) -> None:
        """Restore supply without starting an off modem."""
        self._tell(ModemModel.restore_power, "restore_power()")

    @property
    def powered(self) -> bool:
        """Whether the selected model is powered."""
        return self.modem_model().powered

    @property
    def registered(self) -> bool:
        """Whether the selected model is registered."""
        return self.modem_model().registered

    @property
    def data_mode(self) -> bool:
        """Whether the selected model is in data mode."""
        return self.modem_model().data_mode

    @property
    def hung(self) -> bool:
        """Whether the selected model is hung."""
        return self.modem_model().hung

    @property
    def restarts(self) -> Sequence[ModemRestart]:
        """Each time a run of the selected model ended, with what ended it."""
        return self.modem_model().restarts

    @property
    def power_changes(self) -> Sequence[ModemPower]:
        """Each time the selected model switched on or off, with what switched it, in the order the
        model made them."""
        return self.modem_model().power_changes

    def next_power_change(self, powered: bool | None = None) -> Condition[ModemPower | None]:
        """A described condition for the first power change after this call, or the first to
        `powered`: True for on, False for off. A change the other way does not satisfy it, and a
        later one to `powered` still can. The boundary is the changes there are now,
        `power_changes` at this call; the condition consumes nothing, and returns the same change
        each time it is asked."""
        if powered is not None and not isinstance(powered, bool):
            raise ValueError(
                f"modem.next_power_change: powered is {powered!r}, not True, False or None"
            )
        model = self.modem_model()
        find = FirstFrom(
            lambda: model.power_changes,
            len(model.power_changes),
            lambda change: powered is None or change.powered == powered,
        )
        description = (
            "the modem's next power change"
            if powered is None
            else f"the modem's next power-{'on' if powered else 'off'}"
        )
        return Condition(find, description)

    @property
    def mode_changes(self) -> Sequence[ModeChange]:
        """Each dial the selected model answered, and each return to command mode, in the order
        the model made them."""
        return self.modem_model().mode_changes

    def next_mode_change(self, mode: Mode | None = None) -> Condition[ModeChange | None]:
        """A described condition for the first mode change after this call, or the first into
        `mode`, `"command"` or `"data"`: a change into the other mode does not satisfy it, and a
        later one into `mode` still can. A dial refused with `NO CARRIER` is a change into
        command mode. The boundary is the changes there are now, `mode_changes` at this call; the
        condition consumes nothing, and returns the same change each time it is asked."""
        if mode not in (None, "command", "data"):
            raise ValueError(
                f"modem.next_mode_change: mode is {mode!r}, not 'command', 'data' or None"
            )
        model = self.modem_model()
        find = FirstFrom(
            lambda: model.mode_changes,
            len(model.mode_changes),
            lambda change: mode is None or change.mode == mode,
        )
        description = (
            "the modem's next mode change"
            if mode is None
            else f"the modem's next change to {mode} mode"
        )
        return Condition(find, description)

    @property
    def commands(self) -> Sequence[ModemCommand]:
        """Every completed AT command and reply."""
        return self.modem_model().commands

    def modem_model(self) -> ModemModel:
        """The selected model; reading this never selects a source."""
        if isinstance(self.source, ModemModel):
            return self.source
        self._refuse_source("modem")
        raise SourceError(f"{self.name} has no modem model: select it before board.power_on()")

    def recorded_source(self) -> str:
        if isinstance(self.source, ModemModel):
            return "model:modem"
        return super().recorded_source()

    def command_report(self) -> list[str]:
        """Every AT command with its time and fallback marker for the report."""
        if not isinstance(self.source, ModemModel):
            return []
        commands = ["modem AT commands:"] + [
            f"  {board_time(c.at.ns)}  {c.text!r}"
            + (" (fallback OK)" if c.fallback else "" if c.response else " (no answer)")
            for c in self.commands
        ]
        if isinstance(self.source.data, Internet):
            commands += self.source.data.report()
        return commands

    def _tell(self, call: Callable[[ModemModel], None], step: str) -> None:
        if isinstance(self.source, ModemModel):
            call(self.source)
            if self.log is not None:
                self.log.test(self._now(), f"{self.name}.{step}")
            return
        self._refuse_source("modem")
        self._ask("model")
        key = self._pins.get("pwrkey")
        if key is None or key.on_ms is None or "ri" not in self._pins:
            raise SourceError(
                f"{self.name}: the modem model needs pwrkey pulse timing and ri in the profile"
            )
        reset = self._pins.get("reset")
        if reset is not None and reset.active is not None and reset.pulse_ms is None:
            raise SourceError(
                f"{self.name}: the modem model needs reset with pulse_ms in the profile,"
                " not on_ms and off_ms"
            )
        # Apply and validate the first call without touching the lines on failure.
        model = ModemModel(self._now, pwrkey=key, reset=reset, world=self._world)
        call(model)
        watch = {"modem.pwrkey": model.driven}
        if "reset" in self._pins:
            watch["modem.reset"] = model.reset_driven
        self._give(
            model,
            "model",
            "a cellular modem",
            step=f"{self.name}.{step}",
            prepare=lambda: self._lines.attach(
                self.name,
                output="modem.ri",
                watch=watch,
                latency=LATENCY_NS,
            ),
        )
        model.connect_power(
            lambda t, powered: self._lines.model_drive(self.name, "modem.ri", t, int(powered))
        )
