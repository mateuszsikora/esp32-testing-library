"""The link protocol's codec and handshake (`docs/protocol.md`), without a runner."""

import base64
import json
import re
from pathlib import Path
from typing import Any

import pytest

from esp32tl.link import protocol
from esp32tl.link.protocol import (
    MAX_LINE,
    Adc,
    ChipReset,
    Console,
    EthRx,
    EthTx,
    GpioIn,
    GpioOut,
    HarnessHello,
    Message,
    Power,
    ProtocolError,
    Release,
    Reset,
    RunnerHello,
    SdClear,
    SdFault,
    SdIo,
    Sender,
    Stats,
    Stop,
    Sync,
    UartCfg,
    UartRx,
    UartStats,
    UartTx,
    Wait,
    Warn,
    WifiAp,
    WifiStats,
    as_json,
    decode,
    encode,
    handshake,
)

ROOT = Path(__file__).resolve().parent.parent

EVERY_MESSAGE: list[tuple[Sender, Message]] = [
    ("runner", RunnerHello(1, "esp32", "emulated-fifo-write", "8c65215" + "0" * 33)),
    ("runner", UartTx(7, 1_500_000_000, 1, (26,), 115200, b"AT\r")),
    ("runner", UartTx(8, 1_500_000_000, 1, (), None, b"\x00\xff")),
    ("runner", UartCfg(3, 312_456_789, 1, (4,), 5, 9600)),
    ("runner", UartCfg(4, 312_456_789, 2, (), None, None)),
    ("runner", GpioOut(9, 2_000_000_000, ((4, 1), (5, 0)), (4,), (4,))),
    ("runner", Reset(12, 4_100_000_000, 12)),
    ("runner", EthTx(13, 4_200_000_000, b"\xff" * 6 + b"\x24\x6f\x28\x00\x11\x22\x08\x06")),
    (
        "runner",
        Stats(5, "stop", 0, 2, (), 0, 0, 0, 0, wifi=WifiStats(3, 0, 2, 1, 0)),
    ),
    ("runner", Sync(2_000_000_000)),
    ("runner", Console(812_345_678, b"Hello world!\n")),
    ("runner", Warn(1_000_000_000, "frm_err", "baud", port=1, pin=5, baud=115200, nbytes=3)),
    ("runner", Warn(1_000_000_000, "a kind of a later version", "detail")),
    ("runner", SdIo(523_000_000, "write", 8, 1, (1,), True, 0, False)),
    ("runner", SdIo(524_000_000, "erase", 64, 32, (2, 3), False, 250_000_000, True)),
    (
        "runner",
        Stats(
            12_000_000_000,
            "emulator",
            1,
            13,
            (UartStats(1, 0, 864, 0, 0), UartStats(2, 5, 0, 1, 2)),
            72,
            0,
            3,
            40,
            stop="Unimplemented",
            detail="Unimplemented(0x40080000)",
        ),
    ),
    ("harness", HarnessHello(1, "esp32", "esp32-devkitc", (4, 5))),
    ("harness", HarnessHello(1, "esp32", "esp32-devkitc")),
    ("harness", UartRx(2_000_000_000, 5, 9600, b"$GPGGA*6E\r\n")),
    ("harness", GpioIn(3_000_000_000, ((33, 1),))),
    ("harness", Release(3_500_000_000, (33,))),
    ("harness", Adc(3_000_000_000, 34, 1.25)),
    ("harness", EthRx(4_201_000_000, bytes(14))),
    ("harness", WifiAp(5_000_000_000, False)),
    ("harness", WifiAp(6_000_000_000, True)),
    ("harness", Power(7_000_000_000, False)),
    ("harness", Power(8_000_000_000, True)),
    ("harness", ChipReset(9_000_000_000)),
    ("harness", SdFault(2_000_000_000, 1, "write", True, 0, None, None, 0, 1)),
    ("harness", SdFault(2_000_000_000, 2, "read", False, 1_500_000_000, 64, 95, 3, None)),
    ("harness", SdClear(3_000_000_000, (1, 2))),
    ("harness", SdClear(3_000_000_000)),
    ("harness", Wait(9, 3_000_000_000, 1_000_000)),
    ("harness", Wait(0, None, None)),
    ("harness", Stop()),
]


@pytest.mark.parametrize(("sender", "message"), EVERY_MESSAGE)
def test_every_message_goes_through_the_codec_unchanged(sender: Sender, message: Message) -> None:
    line = encode(message)
    assert line.endswith(b"\n") and line.count(b"\n") == 1
    assert decode(line, sender) == message


def protocol_examples() -> list[dict[str, Any]]:
    """The JSON objects in the code blocks of `docs/protocol.md`."""
    text = (ROOT / "docs" / "protocol.md").read_text()
    examples = []
    decoder = json.JSONDecoder()
    for block in re.findall(r"```json\n(.*?)```", text, re.DOTALL):
        at = 0
        while block[at:].strip():
            at += len(block[at:]) - len(block[at:].lstrip())
            obj, at = decoder.raw_decode(block, at)
            examples.append(obj)
    return examples


def test_the_examples_of_the_protocol_document_decode_and_encode_back() -> None:
    examples = protocol_examples()
    assert {obj["type"] for obj in examples} == {
        *protocol.DECODERS["runner"],
        *protocol.DECODERS["harness"],
    } - {"stop"}
    for obj in examples:
        # The runner's hello declares its clock; the harness's does not.
        runner = obj["type"] in protocol.DECODERS["runner"] and (
            obj["type"] != "hello" or "clock" in obj
        )
        message = decode(json.dumps(obj).encode(), "runner" if runner else "harness")
        assert {k: v for k, v in as_json(message).items() if v is not None} == obj


def test_null_and_an_absent_field_are_the_same_and_unknown_fields_are_ignored() -> None:
    absent = decode(b'{"type":"uart_cfg","seq":1,"t":0,"port":1,"tx_pins":[],"later":2}', "runner")
    null = decode(
        b'{"type":"uart_cfg","seq":1,"t":0,"port":1,"tx_pins":[],"rx_pin":null,"baud":null}',
        "runner",
    )
    assert absent == null == UartCfg(1, 0, 1, (), None, None)


def test_a_runner_hello_of_another_version_decodes_on_its_version_alone() -> None:
    assert decode(b'{"type":"hello","version":2,"chip":["esp32"]}', "runner") == RunnerHello(2)


@pytest.mark.parametrize(
    ("sender", "line", "error"),
    [
        ("runner", b"not json", "not JSON"),
        ("runner", b'["sync"]', "not a JSON object"),
        ("runner", b'{"t":0}', "no message type None"),
        ("runner", b'{"type":"teleport","t":0}', "no message type 'teleport' from the runner"),
        # Each side sends its own messages only.
        ("runner", b'{"type":"wait","ack":0}', "no message type 'wait' from the runner"),
        ("harness", b'{"type":"sync","t":0}', "no message type 'sync' from the harness"),
        ("runner", b'{"type":"sync"}', "sync: no t"),
        ("runner", b'{"type":"sync","t":null}', "sync: no t"),
        ("runner", b'{"type":"sync","t":1.0}', "sync: t is not an integer: 1.0"),
        ("runner", b'{"type":"sync","t":true}', "sync: t is not an integer: True"),
        ("runner", b'{"type":"sync","t":-1}', "sync: t is -1, below 0"),
        ("runner", b'{"type":"sync","t":"0"}', "sync: t is not an integer: '0'"),
        ("runner", b'{"type":"reset","seq":0,"t":0,"cause":12}', "reset: seq is 0, below 1"),
        (
            "runner",
            b'{"type":"console","t":0,"data":"SGVsbG8"}',
            "console: data is not base64",
        ),
        ("runner", b'{"type":"console","t":0,"data":"SGV@bG8="}', "console: data is not base64"),
        (
            "runner",
            b'{"type":"uart_tx","seq":1,"t":0,"port":1,"tx_pins":4,"baud":null,"data":""}',
            "uart_tx: tx_pins is not an array: 4",
        ),
        (
            "runner",
            b'{"type":"gpio_out","seq":1,"t":0,"changes":[[4,2]],"enabled":[],"output":[]}',
            "gpio_out: changes holds level 2",
        ),
        (
            "runner",
            b'{"type":"gpio_out","seq":1,"t":0,"changes":[4],"enabled":[],"output":[]}',
            "gpio_out: changes holds 4, not a [pin, level] pair",
        ),
        ("runner", b'{"type":"stats","t":0,"end":"stop"}', "stats: no resets"),
        ("harness", b'{"type":"uart_rx","t":0,"pin":5,"baud":0,"data":""}', "baud is 0, below 1"),
        ("harness", b'{"type":"adc","t":0,"pin":34,"volts":NaN}', "not JSON: NaN is not a JSON"),
        ("harness", b'{"type":"adc","t":0,"pin":34,"volts":"1"}', "volts is not a number"),
        ("harness", b'{"type":"hello","version":1,"chip":"esp32"}', "hello: no board"),
        ("harness", b'{"type":"sync"}\xff', "not UTF-8"),
        # JSON in UTF-16, which json.loads would take from bytes.
        ("runner", '{"type":"sync","t":0}'.encode("utf-16"), "not UTF-8"),
        ("runner", b'{"type":"sync","t":0,"t":5}', "not JSON: field 't' given twice"),
    ],
)
def test_what_is_not_a_message_of_version_1_is_refused(
    sender: Sender, line: bytes, error: str
) -> None:
    with pytest.raises(ProtocolError, match=re.escape(error)):
        decode(line, sender)


def test_a_line_is_at_most_one_mebibyte() -> None:
    data = b"x" * (MAX_LINE * 3 // 4 - 64)
    line = encode(Console(0, data))
    assert len(line) <= MAX_LINE + 1
    assert decode(line, "runner") == Console(0, data)
    with pytest.raises(ProtocolError, match="over the 1048576-byte line"):
        encode(Console(0, data + b"x" * 192))
    with pytest.raises(ProtocolError, match="a line of 1048577 bytes, over the 1048576-byte limit"):
        decode(b" " * (MAX_LINE + 1), "runner")


RUNNER = RunnerHello(1, "esp32", "emulated-fifo-write", "8c652151dd5a521249a785ca147f20a9f8eda68f")


def test_the_handshake_answers_with_the_board_and_the_watched_pins() -> None:
    answer, mismatch = handshake(RUNNER, chip="esp32", board="esp32-devkitc", watch=[5, 4, 5])
    assert answer == HarnessHello(1, "esp32", "esp32-devkitc", (4, 5))
    assert mismatch is None


@pytest.mark.parametrize(
    ("runner", "mismatch"),
    [
        (RunnerHello(2), "the runner speaks link protocol version 2, the harness 1"),
        (
            RunnerHello(1, "esp32s3", "emulated-fifo-write", "0"),
            "the runner emulates chip 'esp32s3', the board profile is 'esp32'",
        ),
        (RunnerHello(1, "esp32", None, "0"), "the runner's hello has no clock"),
    ],
)
def test_a_mismatched_handshake_is_answered_all_the_same(
    runner: RunnerHello, mismatch: str
) -> None:
    answer, found = handshake(runner, chip="esp32", board="esp32-devkitc", watch=[])
    # The answer carries the harness's version and chip, so the runner finds the mismatch too.
    assert answer == HarnessHello(1, "esp32", "esp32-devkitc", ())
    assert found == mismatch


@pytest.mark.parametrize(
    ("line", "sender"),
    [
        (b'{"type":"eth_tx","seq":1,"t":0,"data":"AAAAAAAAAAAAAAAAAA=="}', "runner"),
        (
            b'{"type":"eth_rx","t":0,"data":"' + base64.b64encode(bytes(1519)) + b'"}',
            "harness",
        ),
        (b'{"type":"wifi_ap","t":0,"on":1}', "harness"),
        (b'{"type":"wifi_ap","t":0}', "harness"),
        (b'{"type":"power","t":0,"on":0}', "harness"),
        (b'{"type":"power","t":0}', "harness"),
        (b'{"type":"chip_reset"}', "harness"),
        (b'{"type":"chip_reset","t":-1}', "harness"),
    ],
)
def test_ethernet_frames_are_14_to_1518_bytes_and_switches_are_true_or_false(
    line: bytes, sender: Sender
) -> None:
    with pytest.raises(ProtocolError):
        decode(line, sender)


@pytest.mark.parametrize(
    ("fields", "found"),
    [
        ('"op":"write"', "a rule that neither fails nor delays"),
        ('"op":"write","fail":false,"delay":0', "a rule that neither fails nor delays"),
        ('"op":"format","fail":true', "op is 'format', not one of read, write, erase"),
        ('"op":"erase","fail":true', "an erase cannot fail, only be delayed"),
        ('"op":"read","delay":10000000001', "over the 10000000000 ns a delay may last"),
        ('"op":"read","fail":true,"first":3', "first and last come together"),
        ('"op":"read","fail":true,"first":3,"last":2', "first 3 is after last 2"),
        ('"op":"read","fail":true,"count":0', "count is 0, below 1"),
        ('"op":"read","fail":1', "fail is not true or false"),
    ],
)
def test_an_sd_fault_says_what_it_does_and_no_more_than_the_runner_takes(
    fields: str, found: str
) -> None:
    with pytest.raises(ProtocolError, match=re.escape(found)):
        decode(b'{"type":"sd_fault","t":0,"id":1,' + fields.encode() + b"}", "harness")


@pytest.mark.parametrize(
    ("line", "sender"),
    [
        (b'{"type":"sd_fault","t":0,"id":0,"op":"read","fail":true}', "harness"),
        (b'{"type":"sd_clear","t":0,"ids":[0]}', "harness"),
        (b'{"type":"sd_clear","t":0,"ids":1}', "harness"),
        (
            b'{"type":"sd_io","t":0,"op":"read","block":0,"blocks":0,"rules":[1],'
            b'"failed":true,"delay":0,"written":false}',
            "runner",
        ),
        (
            b'{"type":"sd_io","t":0,"op":"read","block":0,"blocks":1,"rules":[1],'
            b'"failed":true,"delay":0}',
            "runner",
        ),
    ],
)
def test_sd_rule_ids_are_1_or_more_and_an_sd_io_says_everything(
    line: bytes, sender: Sender
) -> None:
    with pytest.raises(ProtocolError):
        decode(line, sender)


def test_stats_without_wifi_has_no_wifi_field() -> None:
    stats = Stats(5, "stop", 0, 2, (), 0, 0, 0, 0)
    assert "wifi" not in as_json(stats)
    assert decode(encode(stats), "runner") == stats
