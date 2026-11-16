"""The link protocol, version 2: its messages, their NDJSON codec and the handshake.

`docs/protocol.md` is the contract; the section numbers below are its. Every message of version 2
is here, both directions, so that a fake runner in the tests speaks the same codec as `BoardLink`.
Decoding checks what a message must carry and ignores fields it does not know (§2); a `null` and
an absent field are the same. Times are integers in emulated nanoseconds (§3).
"""

import base64
import binascii
import json
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass, fields
from typing import Any, ClassVar, Literal, cast

VERSION = 2

# The longest line either side accepts, its newline not counted (§2).
MAX_LINE = 1 << 20

# An Ethernet frame without FCS, as esp32sim's relay takes and gives it (5.15). The station's
# frames end in 4 bytes more, libpp's guard word, within the same limit: a 1500-byte datagram's
# frame is 1518 bytes with them.
ETH_MIN = 14
ETH_MAX = 1518

# The longest H4 packet esp32sim's VHCI carries either way (5.22): its 512-byte buffer less the 16
# bytes before the packet, which hold its length word and the controller task's name
# (`esp-soc/src/ble/vhci.rs`).
HCI_MAX = 496

# The longest delay an `sd_fault` may give, in nanoseconds (5.20): 10 s.
SD_DELAY_MAX = 10_000_000_000
# The largest `count` an `sd_fault` may give (5.20): what the runner's JSON integers hold.
SD_COUNT_MAX = 2**63 - 1
# What an `sd_fault` applies to and what it does (5.20).
SD_OPS = ("read", "write", "erase")
SD_ACTIONS = ("error", "delay")

Sender = Literal["runner", "harness"]


class ProtocolError(Exception):
    """A line that is not a message of version 2."""


@dataclass(frozen=True)
class RunnerHello:
    """The runner's `hello` (5.1). Only `version` is required to decode it: a runner of another
    version may send another `hello`, and the harness answers it all the same (§4)."""

    TYPE: ClassVar[str] = "hello"
    version: int
    chip: str | None = None
    clock: str | None = None
    esp32sim: str | None = None


@dataclass(frozen=True)
class HarnessHello:
    """The harness's `hello` (5.1): its board profile and the pins the peer is wired to."""

    TYPE: ClassVar[str] = "hello"
    version: int
    chip: str
    board: str
    watch: tuple[int, ...] = ()


@dataclass(frozen=True)
class UartTx:
    """Bytes the guest wrote into a UART's TX FIFO, with the route at the write (5.2)."""

    TYPE: ClassVar[str] = "uart_tx"
    seq: int
    t: int
    port: int
    tx_pins: tuple[int, ...]
    baud: int | None
    data: bytes


@dataclass(frozen=True)
class UartCfg:
    """A UART route's configuration, when it changes on a watched pin (5.3)."""

    TYPE: ClassVar[str] = "uart_cfg"
    seq: int
    t: int
    port: int
    tx_pins: tuple[int, ...]
    rx_pin: int | None
    baud: int | None


@dataclass(frozen=True)
class GpioOut:
    """A change of a watched pin's output level or enable (5.4)."""

    TYPE: ClassVar[str] = "gpio_out"
    seq: int
    t: int
    changes: tuple[tuple[int, int], ...]
    enabled: tuple[int, ...]
    output: tuple[int, ...]


@dataclass(frozen=True)
class Reset:
    """The guest reset, `cause` the ROM's reset reason (5.5)."""

    TYPE: ClassVar[str] = "reset"
    seq: int
    t: int
    cause: int


@dataclass(frozen=True)
class WifiLink:
    """The virtual station's data link changed at a runner slice boundary."""

    TYPE: ClassVar[str] = "wifi_link"
    seq: int
    t: int
    ready: bool


@dataclass(frozen=True)
class EthTx:
    """An Ethernet frame the Wi-Fi station sent, with `--wifi` (5.15)."""

    TYPE: ClassVar[str] = "eth_tx"
    seq: int
    t: int
    data: bytes


@dataclass(frozen=True)
class HciTx:
    """An H4 packet the guest's Bluetooth host sent its controller, with its packet-type byte,
    with `--bt` (5.22)."""

    TYPE: ClassVar[str] = "hci_tx"
    seq: int
    t: int
    data: bytes


@dataclass(frozen=True)
class SdOp:
    """A block operation of the SD card that the harness's rule `fault` applied to, at `t`
    (5.20): its first and last block, the rule's `action` and `delay`, and whether the card wrote
    the image. Not an event: it has no `seq`."""

    TYPE: ClassVar[str] = "sd_op"
    t: int
    fault: int
    op: str
    blocks: tuple[int, int]
    action: str
    delay: int | None
    written: bool


@dataclass(frozen=True)
class SdCardAck:
    """What the SD card slot holds after the harness's `sd_card`, and why the change was
    refused, if it was (5.19). Not an event: nothing answers it."""

    TYPE: ClassVar[str] = "sd_card_ack"
    t: int
    image: str | None
    error: str | None = None


@dataclass(frozen=True)
class FlashDumpAck:
    """The file of `--flash-out` holds the flash of the turn at `t`, unless `error` says why it
    does not (5.21). Not an event: the turn it answers is still going on."""

    TYPE: ClassVar[str] = "flash_dump_ack"
    t: int
    error: str | None = None


@dataclass(frozen=True)
class Sync:
    """The end of the runner's turn at the block point `t` (5.6)."""

    TYPE: ClassVar[str] = "sync"
    t: int


@dataclass(frozen=True)
class Console:
    """Bytes the guest wrote to UART0, with `--console socket` or `both` (5.7)."""

    TYPE: ClassVar[str] = "console"
    t: int
    data: bytes


@dataclass(frozen=True)
class Warn:
    """What the emulator dropped or refused (5.8). `nbytes` is the `bytes` field. A kind this
    version does not list is accepted, as the protocol asks."""

    TYPE: ClassVar[str] = "warn"
    t: int
    kind: str
    detail: str
    port: int | None = None
    pin: int | None = None
    baud: int | None = None
    nbytes: int | None = None


@dataclass(frozen=True)
class WifiStats:
    """`stats.wifi`, with `--wifi` (5.9)."""

    eth_tx: int
    eth_tx_dropped: int
    eth_rx: int
    eth_rx_dropped: int
    rx_dropped: int


@dataclass(frozen=True)
class UartStats:
    """One port's entry in `stats.uart`."""

    port: int
    tx_bytes: int
    rx_bytes: int
    rxfifo_ovf: int
    frm_err: int


@dataclass(frozen=True)
class Stats:
    """The last message of a run (5.9). `stop` and `detail` come with some kinds of `end`."""

    TYPE: ClassVar[str] = "stats"
    t: int
    end: str
    resets: int
    turns: int
    uart: tuple[UartStats, ...]
    unrouted_rx_bytes: int
    inputs_on_output: int
    inputs_not_applied: int
    rx_bytes_not_paced: int
    stop: str | None = None
    detail: str | None = None
    wifi: WifiStats | None = None
    rt_resyncs: int | None = None


@dataclass(frozen=True)
class UartRx:
    """Bytes the peer sends into the chip on `pin`, at its own line rate `baud` (5.10)."""

    TYPE: ClassVar[str] = "uart_rx"
    t: int
    pin: int
    baud: int
    data: bytes


@dataclass(frozen=True)
class GpioIn:
    """From `t` the peer drives each pin to its level (5.11)."""

    TYPE: ClassVar[str] = "gpio_in"
    t: int
    levels: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class Release:
    """From `t` the peer stops driving the pins (5.11)."""

    TYPE: ClassVar[str] = "release"
    t: int
    pins: tuple[int, ...]


@dataclass(frozen=True)
class Adc:
    """From `t` the pad of `pin` sees `volts` (5.12)."""

    TYPE: ClassVar[str] = "adc"
    t: int
    pin: int
    volts: float


@dataclass(frozen=True)
class EthRx:
    """From `t` the access point puts this Ethernet frame on the air for the station (5.15)."""

    TYPE: ClassVar[str] = "eth_rx"
    t: int
    data: bytes


@dataclass(frozen=True)
class HciRx:
    """From `t` this H4 packet, with its packet-type byte, is for the guest's Bluetooth host,
    which reads it at its next VHCI poll (5.22)."""

    TYPE: ClassVar[str] = "hci_rx"
    t: int
    data: bytes


@dataclass(frozen=True)
class WifiAp:
    """From `t` the access point is on the air, or off it (5.16)."""

    TYPE: ClassVar[str] = "wifi_ap"
    t: int
    on: bool


@dataclass(frozen=True)
class Power:
    """From `t` the board's supply is on, or off (5.17)."""

    TYPE: ClassVar[str] = "power"
    t: int
    on: bool


@dataclass(frozen=True)
class ChipReset:
    """At `t` the chip's EN pin is pulsed: a power-on reset of the chip alone (5.18)."""

    TYPE: ClassVar[str] = "chip_reset"
    t: int


@dataclass(frozen=True)
class SdFault:
    """From `t` the SD card applies the rule `id` to the block operations it matches (5.20):
    `count` of them, or every one until cleared, on `blocks`, first and last, or any."""

    TYPE: ClassVar[str] = "sd_fault"
    t: int
    id: int
    op: str
    action: str
    delay: int | None = None
    count: int | None = None
    blocks: tuple[int, int] | None = None


@dataclass(frozen=True)
class SdClear:
    """From `t` the SD card's rules `ids`, or all of them, are gone (5.20)."""

    TYPE: ClassVar[str] = "sd_clear"
    t: int
    ids: tuple[int, ...] | None = None


@dataclass(frozen=True)
class SdCardChange:
    """From `t` the SD card slot holds a card with the image file `image`, or, with `None`, no
    card (5.19)."""

    TYPE: ClassVar[str] = "sd_card"
    t: int
    image: str | None


@dataclass(frozen=True)
class FlashDump:
    """The whole flash into the file of `--flash-out`, now, in this turn (5.21): the runner answers
    with `flash_dump_ack`, and the turn goes on."""

    TYPE: ClassVar[str] = "flash_dump"


@dataclass(frozen=True)
class Wait:
    """The end of the peer's turn (5.13); `None` is no bound."""

    TYPE: ClassVar[str] = "wait"
    ack: int
    quiet_until: int | None
    latency: int | None


@dataclass(frozen=True)
class Stop:
    """The end of the peer's turn and of the run (5.14)."""

    TYPE: ClassVar[str] = "stop"


Event = WifiLink | UartTx | UartCfg | GpioOut | Reset | EthTx | HciTx
RunnerMessage = (
    RunnerHello | Event | Sync | Console | Warn | SdCardAck | SdOp | FlashDumpAck | Stats
)
Input = (
    UartRx
    | GpioIn
    | Release
    | Adc
    | EthRx
    | HciRx
    | WifiAp
    | Power
    | ChipReset
    | SdCardChange
    | SdFault
    | SdClear
)
HarnessMessage = HarnessHello | Input | FlashDump | Wait | Stop
Message = RunnerMessage | HarnessMessage

# The fields whose JSON name is not the attribute's.
JSON_NAMES = {"nbytes": "bytes"}


def as_json(message: Message | UartStats | WifiStats) -> dict[str, Any]:
    """The message as the JSON object that goes on the socket."""
    obj: dict[str, Any] = {}
    if not isinstance(message, UartStats | WifiStats):
        obj["type"] = message.TYPE
    for field in fields(message):
        value = getattr(message, field.name)
        # `stats.wifi` is there with `--wifi` only, `stats.rt_resyncs` with `--realtime` only
        optional = field.name in ("wifi", "rt_resyncs")
        if not (isinstance(message, Stats) and optional and value is None):
            obj[JSON_NAMES.get(field.name, field.name)] = _to_json(value)
    return obj


def _to_json(value: Any) -> Any:
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, tuple):
        return [_to_json(item) for item in value]
    if isinstance(value, UartStats | WifiStats):
        return as_json(value)
    return value


def encode(message: Message) -> bytes:
    """One line of NDJSON, newline included."""
    line = json.dumps(as_json(message), separators=(",", ":"), allow_nan=False).encode()
    if len(line) > MAX_LINE:
        raise ProtocolError(f"{message.TYPE} is {len(line)} bytes, over the {MAX_LINE}-byte line")
    return line + b"\n"


def decode(line: bytes, sender: Sender) -> Message:
    """The message on one line from `sender`, its newline stripped or not."""
    line = line.removesuffix(b"\n")
    if len(line) > MAX_LINE:
        raise ProtocolError(f"a line of {len(line)} bytes, over the {MAX_LINE}-byte limit")
    try:
        # Decoded first: given bytes, json.loads would take UTF-16 and UTF-32 too.
        obj = json.loads(
            line.decode("utf-8"), parse_constant=_no_constant, object_pairs_hook=_no_duplicates
        )
    except UnicodeDecodeError as error:
        raise ProtocolError(f"not UTF-8: {error}") from None
    except ValueError as error:
        raise ProtocolError(f"not JSON: {error}") from None
    if not isinstance(obj, dict):
        raise ProtocolError(f"not a JSON object: {_excerpt(line)}")
    kind = obj.get("type")
    decoder = DECODERS[sender].get(kind) if isinstance(kind, str) else None
    if decoder is None:
        raise ProtocolError(f"no message type {kind!r} from the {sender}: {_excerpt(line)}")
    try:
        return decoder(Fields(obj))
    except ProtocolError as error:
        raise ProtocolError(f"{kind}: {error}") from None


def decode_runner(line: bytes) -> RunnerMessage:
    """A message from the runner."""
    return cast(RunnerMessage, decode(line, "runner"))


def decode_harness(line: bytes) -> HarnessMessage:
    """A message from the harness, as the runner reads it."""
    return cast(HarnessMessage, decode(line, "harness"))


def _no_constant(name: str) -> None:
    raise ValueError(f"{name} is not a JSON number")


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj = dict(pairs)
    if len(obj) < len(pairs):
        names = [name for name, _ in pairs]
        raise ValueError(f"field {next(n for n in names if names.count(n) > 1)!r} given twice")
    return obj


def _excerpt(line: bytes) -> str:
    return repr(line[:120]) + ("..." if len(line) > 120 else "")


class Fields:
    """The fields of one JSON object, read with their types checked."""

    def __init__(self, obj: dict[str, Any]) -> None:
        self.obj = obj

    def _get(self, name: str) -> Any:
        value = self.obj.get(name)
        if value is None:
            raise ProtocolError(f"no {name}")
        return value

    def integer(self, name: str, minimum: int = 0) -> int:
        return _int(self._get(name), name, minimum)

    def opt_integer(self, name: str, minimum: int = 0) -> int | None:
        value = self.obj.get(name)
        return None if value is None else _int(value, name, minimum)

    def string(self, name: str) -> str:
        value = self._get(name)
        if not isinstance(value, str):
            raise ProtocolError(f"{name} is not a string: {value!r}")
        return value

    def opt_string(self, name: str) -> str | None:
        return None if self.obj.get(name) is None else self.string(name)

    def pins(self, name: str) -> tuple[int, ...]:
        return tuple(_int(pin, name, 0) for pin in self.array(name))

    def opt_integers(self, name: str) -> tuple[int, ...] | None:
        """An array of non-negative integers; None when absent or null."""
        if self.obj.get(name) is None:
            return None
        return tuple(_int(value, name, 0) for value in self.array(name))

    def levels(self, name: str) -> tuple[tuple[int, int], ...]:
        pairs = []
        for pair in self.array(name):
            if not (isinstance(pair, list) and len(pair) == 2):
                raise ProtocolError(f"{name} holds {pair!r}, not a [pin, level] pair")
            pin, level = _int(pair[0], name, 0), _int(pair[1], name, 0)
            if level > 1:
                raise ProtocolError(f"{name} holds level {level}; a level is 0 or 1")
            pairs.append((pin, level))
        return tuple(pairs)

    def array(self, name: str) -> list[Any]:
        value = self._get(name)
        if not isinstance(value, list):
            raise ProtocolError(f"{name} is not an array: {value!r}")
        return value

    def data(self, name: str) -> bytes:
        value = self.string(name)
        try:
            return base64.b64decode(value, validate=True)
        except binascii.Error as error:
            raise ProtocolError(f"{name} is not base64 ({error})") from None

    def boolean(self, name: str) -> bool:
        value = self._get(name)
        if not isinstance(value, bool):
            raise ProtocolError(f"{name} is not true or false: {value!r}")
        return value

    def choice(self, name: str, choices: tuple[str, ...]) -> str:
        value = self.string(name)
        if value not in choices:
            raise ProtocolError(f"{name} is {value!r}, not {' or '.join(choices)}")
        return value

    def pair(self, name: str) -> tuple[int, int] | None:
        """`[first, last]`, first not after last; None when absent or null."""
        if self.obj.get(name) is None:
            return None
        items = self.array(name)
        if len(items) != 2:
            raise ProtocolError(f"{name} is {items!r}, not [first, last]")
        first, last = (_int(item, name, 0) for item in items)
        if first > last:
            raise ProtocolError(f"{name} is {first} to {last}: the last before the first")
        return first, last

    def frame(self, name: str) -> bytes:
        data = self.data(name)
        if not ETH_MIN <= len(data) <= ETH_MAX:
            raise ProtocolError(
                f"{name} is an Ethernet frame of {len(data)} bytes; one without FCS has"
                f" {ETH_MIN} to {ETH_MAX}"
            )
        return data

    def packet(self, name: str) -> bytes:
        data = self.data(name)
        if not 1 <= len(data) <= HCI_MAX:
            raise ProtocolError(
                f"{name} is an H4 packet of {len(data)} bytes; esp32sim's VHCI carries 1 to"
                f" {HCI_MAX}"
            )
        return data

    def number(self, name: str) -> float:
        value = self._get(name)
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ProtocolError(f"{name} is not a number: {value!r}")
        if not math.isfinite(value):
            raise ProtocolError(f"{name} is not finite: {value!r}")
        return float(value)


def _int(value: Any, name: str, minimum: int) -> int:
    # bool is an int in Python, and true is not a number in JSON
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError(f"{name} is not an integer: {value!r}")
    if value < minimum:
        raise ProtocolError(f"{name} is {value}, below {minimum}")
    return value


def _runner_hello(f: Fields) -> RunnerHello:
    version = f.integer("version")
    if version != VERSION:
        # Whatever else it says is another version's.
        return RunnerHello(version)
    return RunnerHello(
        version, f.opt_string("chip"), f.opt_string("clock"), f.opt_string("esp32sim")
    )


def _harness_hello(f: Fields) -> HarnessHello:
    watch = () if f.obj.get("watch") is None else f.pins("watch")
    return HarnessHello(f.integer("version"), f.string("chip"), f.string("board"), watch)


def _uart_stats(value: Any) -> UartStats:
    if not isinstance(value, dict):
        raise ProtocolError(f"uart holds {value!r}, not an object")
    f = Fields(value)
    return UartStats(
        f.integer("port"),
        f.integer("tx_bytes"),
        f.integer("rx_bytes"),
        f.integer("rxfifo_ovf"),
        f.integer("frm_err"),
    )


def _wifi_stats(value: Any) -> WifiStats | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ProtocolError(f"wifi holds {value!r}, not an object")
    f = Fields(value)
    return WifiStats(
        f.integer("eth_tx"),
        f.integer("eth_tx_dropped"),
        f.integer("eth_rx"),
        f.integer("eth_rx_dropped"),
        f.integer("rx_dropped"),
    )


def _sd_op(f: Fields) -> SdOp:
    blocks = f.pair("blocks")
    if blocks is None:
        raise ProtocolError("no blocks")
    return SdOp(
        f.integer("t"),
        f.integer("fault"),
        f.choice("op", SD_OPS),
        blocks,
        f.choice("action", SD_ACTIONS),
        f.opt_integer("delay", 1),
        f.boolean("written"),
    )


def _sd_fault(f: Fields) -> SdFault:
    """An `sd_fault`, checked as the runner checks it without the card."""
    op = f.choice("op", SD_OPS)
    action = f.choice("action", SD_ACTIONS)
    delay = f.opt_integer("delay")
    if action == "error":
        if op == "erase":
            raise ProtocolError("an erase cannot fail, only be delayed")
        if delay is not None:
            raise ProtocolError("an error takes no delay")
    elif delay is None:
        raise ProtocolError("a delay takes delay")
    elif not 1 <= delay <= SD_DELAY_MAX:
        raise ProtocolError(f"a delay of {delay} ns; one is 1 ns to {SD_DELAY_MAX} ns (10 s)")
    count = f.opt_integer("count", 1)
    if count is not None and count > SD_COUNT_MAX:
        raise ProtocolError(f"a count of {count}; one is at most {SD_COUNT_MAX}")
    return SdFault(f.integer("t"), f.integer("id"), op, action, delay, count, f.pair("blocks"))


def _stats(f: Fields) -> Stats:
    return Stats(
        t=f.integer("t"),
        end=f.string("end"),
        resets=f.integer("resets"),
        turns=f.integer("turns"),
        uart=tuple(_uart_stats(entry) for entry in f.array("uart")),
        unrouted_rx_bytes=f.integer("unrouted_rx_bytes"),
        inputs_on_output=f.integer("inputs_on_output"),
        inputs_not_applied=f.integer("inputs_not_applied"),
        rx_bytes_not_paced=f.integer("rx_bytes_not_paced"),
        stop=f.opt_string("stop"),
        detail=f.opt_string("detail"),
        wifi=_wifi_stats(f.obj.get("wifi")),
        rt_resyncs=f.opt_integer("rt_resyncs"),
    )


DECODERS: dict[Sender, dict[str, Callable[[Fields], Message]]] = {
    "runner": {
        "hello": _runner_hello,
        "uart_tx": lambda f: UartTx(
            f.integer("seq", 1),
            f.integer("t"),
            f.integer("port"),
            f.pins("tx_pins"),
            f.opt_integer("baud", 1),
            f.data("data"),
        ),
        "uart_cfg": lambda f: UartCfg(
            f.integer("seq", 1),
            f.integer("t"),
            f.integer("port"),
            f.pins("tx_pins"),
            f.opt_integer("rx_pin"),
            f.opt_integer("baud", 1),
        ),
        "gpio_out": lambda f: GpioOut(
            f.integer("seq", 1),
            f.integer("t"),
            f.levels("changes"),
            f.pins("enabled"),
            f.pins("output"),
        ),
        "reset": lambda f: Reset(f.integer("seq", 1), f.integer("t"), f.integer("cause")),
        "wifi_link": lambda f: WifiLink(f.integer("seq", 1), f.integer("t"), f.boolean("ready")),
        "eth_tx": lambda f: EthTx(f.integer("seq", 1), f.integer("t"), f.frame("data")),
        "hci_tx": lambda f: HciTx(f.integer("seq", 1), f.integer("t"), f.packet("data")),
        "sync": lambda f: Sync(f.integer("t")),
        "console": lambda f: Console(f.integer("t"), f.data("data")),
        "warn": lambda f: Warn(
            f.integer("t"),
            f.string("kind"),
            f.string("detail"),
            f.opt_integer("port"),
            f.opt_integer("pin"),
            f.opt_integer("baud"),
            f.opt_integer("bytes"),
        ),
        "sd_op": _sd_op,
        "sd_card_ack": lambda f: SdCardAck(
            f.integer("t"), f.opt_string("image"), f.opt_string("error")
        ),
        "flash_dump_ack": lambda f: FlashDumpAck(f.integer("t"), f.opt_string("error")),
        "stats": _stats,
    },
    "harness": {
        "hello": _harness_hello,
        "uart_rx": lambda f: UartRx(
            f.integer("t"), f.integer("pin"), f.integer("baud", 1), f.data("data")
        ),
        "gpio_in": lambda f: GpioIn(f.integer("t"), f.levels("levels")),
        "release": lambda f: Release(f.integer("t"), f.pins("pins")),
        "adc": lambda f: Adc(f.integer("t"), f.integer("pin"), f.number("volts")),
        "eth_rx": lambda f: EthRx(f.integer("t"), f.frame("data")),
        "hci_rx": lambda f: HciRx(f.integer("t"), f.packet("data")),
        "wifi_ap": lambda f: WifiAp(f.integer("t"), f.boolean("on")),
        "power": lambda f: Power(f.integer("t"), f.boolean("on")),
        "chip_reset": lambda f: ChipReset(f.integer("t")),
        "sd_fault": _sd_fault,
        "sd_clear": lambda f: SdClear(f.integer("t"), f.opt_integers("ids")),
        "sd_card": lambda f: SdCardChange(f.integer("t"), f.opt_string("image")),
        "wait": lambda f: Wait(
            f.integer("ack"), f.opt_integer("quiet_until"), f.opt_integer("latency")
        ),
        "flash_dump": lambda f: FlashDump(),
        "stop": lambda f: Stop(),
    },
}


def handshake(
    runner: RunnerHello, *, chip: str, board: str, watch: Iterable[int]
) -> tuple[HarnessHello, str | None]:
    """The harness's answer to the runner's `hello`, and what is wrong with the runner's, or
    None. The answer goes out also on a mismatch: it carries the harness's version and chip, so
    the runner finds the mismatch too and ends the run with its `stats` (§4)."""
    answer = HarnessHello(VERSION, chip, board, tuple(sorted(set(watch))))
    if runner.version != VERSION:
        return answer, (
            f"the runner speaks link protocol version {runner.version}, the harness {VERSION}"
        )
    if runner.chip != chip:
        return answer, f"the runner emulates chip {runner.chip!r}, the board profile is {chip!r}"
    if runner.clock is None or runner.esp32sim is None:
        missing = "clock" if runner.clock is None else "esp32sim"
        return answer, f"the runner's hello has no {missing}"
    return answer, None
