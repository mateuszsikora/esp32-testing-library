"""Partition parsing, the NVS generator's input, pre-boot flash overrides, and what a snapshot of
the flash reads: NVS by its format, OTA data."""

import hashlib
import io
import struct
import zlib
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest
from pytest_embedded.dut import Dut
from pytest_embedded.log import MessageQueue
from pytest_embedded.utils import Meta
from pytest_embedded_idf.app import FlashFile, IdfApp

from esp32tl import boards, flash
from esp32tl.backends import esp32sim
from esp32tl.backends.esp32sim import BackendError, Esp32Sim
from esp32tl.flash import (
    FlashSnapshot,
    OtaData,
    OtaEntry,
    Partition,
    i8,
    i16,
    i32,
    i64,
    u8,
    u16,
    u32,
    u64,
)


def entry(
    name: str, offset: int, size: int, kind: int = 1, subtype: int = 2, flags: int = 0
) -> bytes:
    return struct.pack("<HBBII16sI", 0x50AA, kind, subtype, offset, size, name.encode(), flags)


def table_bytes() -> bytes:
    data = entry("settings", 0x11000, 0x6000) + entry("factory", 0x20000, 0x100000, 0, 0)
    return data + b"\xeb\xeb" + b"\xff" * 14 + hashlib.md5(data).digest() + b"\xff" * 32


def test_partition_table_reads_names_offsets_types_sizes_and_flags() -> None:
    assert flash.partitions(table_bytes()) == {
        "settings": flash.Partition("settings", 1, 2, 0x11000, 0x6000),
        "factory": flash.Partition("factory", 0, 0, 0x20000, 0x100000),
    }
    # Old tables without MD5, and flags on current entries.
    assert flash.partitions(entry("nvs", 0x9000, 0x6000, flags=1))["nvs"].flags == 1


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", "no partitions"),
        (b"\xff" * 32, "no partitions"),
        (b"\xaa\x50", "truncated entry"),
        (bytes(32), "invalid magic"),
        (entry("", 0, 0), "empty or duplicate"),
        (entry("nvs", 0, 0) * 2, "empty or duplicate"),
        (table_bytes().replace(b"settings", b"SettingS"), "MD5 mismatch"),
        (table_bytes() + entry("nvs", 0, 0), "data after MD5"),
        (b"\xff" * 32 + entry("nvs", 0, 0), "data after erased padding"),
    ],
)
def test_partition_table_refuses_corruption(data: bytes, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        flash.partitions(data)


def test_image_preserves_names_strings_blobs_and_integer_widths() -> None:
    text = '  café, "quoted"\r\n#not a comment\n'
    image = flash.nvs_image(
        {
            '#config,"': {
                'host,"': text,
                "ca": b"\x00\xff",
                "u8": u8(255),
                "u16": u16(8883),
                "u32": u32(2**32 - 1),
                "i32": i32(-(2**31)),
                "i8": i8(-128),
                "i16": i16(-32768),
                "u64": u64(2**64 - 1),
                "i64": i64(-(2**63)),
            },
            "other": {"empty": "", "blob": b""},
        },
        0x6000,
    )
    assert nvs_entries(image) == [
        (0, '#config,"', 0x01, b"\x01" + b"\xff" * 7),
        (1, 'host,"', 0x21, text.encode() + b"\0"),
        (1, "ca", 0x42, b"\x00\xff"),
        (1, "ca", 0x48, struct.pack("<IBB", 2, 1, 0) + b"\xff" * 2),
        (1, "u8", 0x01, b"\xff" * 8),
        (1, "u16", 0x02, struct.pack("<H", 8883) + b"\xff" * 6),
        (1, "u32", 0x04, b"\xff" * 8),
        (1, "i32", 0x14, struct.pack("<i", -(2**31)) + b"\xff" * 4),
        (1, "i8", 0x11, struct.pack("<b", -128) + b"\xff" * 7),
        (1, "i16", 0x12, struct.pack("<h", -32768) + b"\xff" * 6),
        (1, "u64", 0x08, b"\xff" * 8),
        (1, "i64", 0x18, struct.pack("<q", -(2**63))),
        (0, "other", 0x01, b"\x02" + b"\xff" * 7),
        (2, "empty", 0x21, b"\0"),
        (2, "blob", 0x42, b""),
        (2, "blob", 0x48, struct.pack("<IBB", 0, 1, 0) + b"\xff" * 2),
    ]


@pytest.mark.parametrize("value", [1, True, 1.2, None, bytearray(b"a")])
def test_nvs_requires_a_type_for_integers_and_refuses_other_values(value: object) -> None:
    with pytest.raises(
        TypeError,
        match=r"config/key: use str, bytes, u8\(\), i8\(\), u16\(\), i16\(\), u32\(\), i32\(\),"
        r" u64\(\) or i64\(\); a bare int has no NVS width",
    ):
        flash.nvs_image(cast(flash.Namespaces, {"config": {"key": value}}), 0x6000)


@pytest.mark.parametrize(
    ("wrap", "low", "high"),
    [
        (u8, 0, 255),
        (i8, -128, 127),
        (u16, 0, 65535),
        (i16, -32768, 32767),
        (u32, 0, 2**32 - 1),
        (i32, -(2**31), 2**31 - 1),
        (u64, 0, 2**64 - 1),
        (i64, -(2**63), 2**63 - 1),
    ],
)
def test_integer_widths_check_their_range(
    wrap: Callable[[int], flash.Typed], low: int, high: int
) -> None:
    assert wrap(low).value == low
    assert wrap(high).value == high
    for outside in (low - 1, high + 1):
        with pytest.raises(ValueError, match="needs"):
            wrap(outside)
    for wrong in (True, 1.0, "1"):
        with pytest.raises(TypeError, match="needs an int"):
            wrap(cast(int, wrong))


@pytest.mark.parametrize("name", ["", "x" * 16, "ą" * 8, "a\0b", "a\nb"])
def test_names_obey_nvs_byte_limit(name: str) -> None:
    with pytest.raises(ValueError, match="NVS name"):
        flash.nvs_image({name: {}}, 0x6000)
    with pytest.raises(ValueError, match="NVS name"):
        flash.nvs_image({"config": {name: "value"}}, 0x6000)


def test_real_generator_preserves_string_bytes_and_has_exact_partition_size() -> None:
    value = ' café, "quoted"\r\n#not a comment\n'
    image = flash.nvs_image(
        {"config": {"text": value, "blob": b"\0\xff", "port": u16(8883)}}, 0x6000
    )
    assert len(image) == 0x6000
    assert value.encode() + b"\0" in image
    assert image == flash.nvs_image(
        {"config": {"text": value, "blob": b"\0\xff", "port": u16(8883)}}, 0x6000
    )


def test_generator_failure_is_visible_and_bad_sizes_are_refused() -> None:
    with pytest.raises(ValueError, match="NVS generator failed"):
        flash.nvs_image({"config": {"blob": b"x" * 20000}}, 0x3000)
    for size in (0, 0x2000, 0x3001):
        with pytest.raises(ValueError, match="NVS partition size"):
            flash.nvs_image({}, size)
    with pytest.raises(ValueError, match="cannot contain NUL"):
        flash.nvs_image({"config": {"text": "a\0b"}}, 0x3000)


@pytest.fixture
def device(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Esp32Sim:
    table = tmp_path / "partition-table.bin"
    table.write_bytes(table_bytes())
    app = Mock(
        spec=IdfApp, flash_files=[FlashFile(offset=0x8000, file_path=str(table), encrypted=False)]
    )
    monkeypatch.setattr(esp32sim, "load_app", lambda _, *, build_dir=None: app)
    monkeypatch.setattr(esp32sim, "runner_path", lambda: Path("runner"))
    monkeypatch.setattr(esp32sim, "rom_path", lambda: Path("rom"))
    run = tmp_path / "run"
    run.mkdir()
    return Esp32Sim(
        Mock(spec=Dut, logfile=str(run / "dut.log")),
        "app",
        Mock(spec=MessageQueue),
        Meta(str(run), {}, {}),
        boards.profile("esp32-devkitc"),
    )


def test_nvs_flashes_at_named_partition_after_build_and_replaces_previous_call(
    device: Esp32Sim, monkeypatch: pytest.MonkeyPatch
) -> None:
    device.nvs({"config": {"host": "first"}}, partition="settings")
    device.nvs({"config": {"host": "second"}}, partition="settings")
    image = Path(device.dut.logfile).parent / "nvs-11000.bin"
    assert b"second\0" in image.read_bytes()
    assert b"first\0" not in image.read_bytes()
    assert device.set_up() == ["the NVS partition 'settings'"]
    assert not device.powered
    popen = Mock(side_effect=OSError("no executable"))
    monkeypatch.setattr(esp32sim, "RunnerPopen", popen)
    with pytest.raises(BackendError, match="cannot start"):
        device.power_on()
    command = popen.call_args.args[1]
    assert command[command.index("--flash-at") : command.index("--flash-at") + 4] == [
        "--flash-at",
        f"0x8000={device.app.flash_files[0].file_path}",
        "--flash-at",
        f"0x11000={image}",
    ]
    device.close()


def test_nvs_refuses_unknown_wrong_type_encrypted_and_late_calls(device: Esp32Sim) -> None:
    with pytest.raises(BackendError, match="no partition 'nvs': it has settings, factory"):
        device.nvs({})
    with pytest.raises(BackendError, match="must be unencrypted, writable NVS"):
        device.nvs({}, partition="factory")
    table = Path(device.app.flash_files[0].file_path)
    table.write_bytes(entry("nvs", 0x9000, 0x6000, flags=1))
    with pytest.raises(BackendError, match="must be unencrypted, writable NVS"):
        device.nvs({})
    device.process = Mock()
    with pytest.raises(BackendError, match=r"write the flash before board.power_on\(\)"):
        device.nvs({})


def test_nvs_preserves_previous_image_on_bad_replacement(device: Esp32Sim) -> None:
    device.nvs({"config": {"host": "kept"}}, partition="settings")
    image = Path(device.dut.logfile).parent / "nvs-11000.bin"
    before = image.read_bytes()
    with pytest.raises(ValueError, match="NVS generator failed"):
        device.nvs({"config": {"blob": b"x" * 50000}}, partition="settings")
    assert image.read_bytes() == before


def test_nvs_reports_missing_or_invalid_partition_table(device: Esp32Sim) -> None:
    table = Path(device.app.flash_files[0].file_path)
    table.write_bytes(b"broken")
    with pytest.raises(BackendError, match=r"cannot read .*truncated entry"):
        device.nvs({})
    table.unlink()
    with pytest.raises(BackendError, match="cannot read"):
        device.nvs({})
    device.app.flash_files.clear()
    with pytest.raises(BackendError, match=r"must flash one partition-table\.bin"):
        device.nvs({})


def test_nvs_partitions_are_independent_and_build_images_are_unchanged(device: Esp32Sim) -> None:
    table = Path(device.app.flash_files[0].file_path)
    layout = entry("nvs", 0x9000, 0x6000) + entry("settings", 0x11000, 0x6000)
    table.write_bytes(layout)
    built_nvs = table.parent / "built-nvs.bin"
    built_nvs.write_bytes(b"original build contents")
    device.app.flash_files.append(
        FlashFile(offset=0x9000, file_path=str(built_nvs), encrypted=False)
    )
    device.nvs({"config": {"host": "first"}})
    device.nvs({"config": {"host": "second"}}, partition="settings")
    run = Path(device.dut.logfile).parent
    assert b"first\0" in (run / "nvs-9000.bin").read_bytes()
    assert b"second\0" in (run / "nvs-11000.bin").read_bytes()
    assert table.read_bytes() == layout
    assert built_nvs.read_bytes() == b"original build contents"
    assert len(device.set_up()) == 2


def test_empty_nvs_partition_is_generated() -> None:
    image = flash.nvs_image({}, 0x3000)
    assert len(image) == 0x3000
    assert image[-0x1000:] == b"\xff" * 0x1000


def nvs_entries(image: bytes) -> list[tuple[int, str, int, bytes]]:
    """Read entries and string/blob payloads, checking CRCs independently of the generator.

    Walk each page's bitmap and entry spans, so strings cannot masquerade as keys and
    extra entries or duplicates cannot disappear into a dictionary of expected names.
    Other entry types retain their eight-byte inline data for the assertions below.
    """
    entries = []
    assert len(image) % 4096 == 0
    for offset in range(0, len(image), 4096):
        page = image[offset : offset + 4096]
        if page == b"\xff" * 4096:
            continue
        assert struct.unpack_from("<I", page, 28)[0] == zlib.crc32(page[4:28], 0xFFFFFFFF)
        slot = 0
        while slot < 126:
            state = (page[32 + slot // 4] >> (2 * (slot % 4))) & 3
            if state != 2:  # Only written entries; 3 is empty, 0 is erased.
                slot += 1
                continue
            at = 64 + slot * 32
            header = page[at : at + 32]
            namespace, kind, span = header[:3]
            assert 1 <= span <= 126 - slot
            assert struct.unpack_from("<I", header, 4)[0] == zlib.crc32(
                header[:4] + header[8:32], 0xFFFFFFFF
            )
            key = header[8:24].split(b"\0", 1)[0].decode()
            if kind in (0x21, 0x42):  # String (including NUL) or a V2 blob chunk.
                length = struct.unpack_from("<H", header, 24)[0]
                assert length <= (span - 1) * 32
                value = page[at + 32 : at + 32 + length]
                assert struct.unpack_from("<I", header, 28)[0] == zlib.crc32(value, 0xFFFFFFFF)
            else:
                value = header[24:32]
            entries.append((namespace, key, kind, value))
            slot += span
    return entries


@pytest.mark.parametrize("namespace", ["config", "sta.ssid", "sta.pswd", "ap.ssid"])
def test_nvs_names_do_not_change_values_or_add_keys(namespace: str) -> None:
    values: dict[str, str | bytes | flash.Typed] = {
        "sta.ssid": 'my-wifi, "café"\r\n#still the value\n',
        "sta.pswd": "password\r\n#not a comment\n",
        "ap.ssid": "access-point",
        "ap.passwd": "another-password",
        "ap.authmode": u8(3),
    }
    namespaces = {namespace: values, "other": values}
    first = flash.nvs_image(namespaces, 0x6000)
    assert first == flash.nvs_image(namespaces, 0x6000)
    expected = []
    for index, name in enumerate(namespaces, 1):
        expected.append((0, name, 0x01, bytes([index]) + b"\xff" * 7))
        for key, value in values.items():
            if isinstance(value, str):
                expected.append((index, key, 0x21, value.encode() + b"\0"))
            else:
                expected.append((index, key, 0x01, b"\x03" + b"\xff" * 7))
    assert nvs_entries(first) == expected


@pytest.mark.parametrize("length", [65535, 65536, 65537, 70000])
def test_large_blob_fits_without_a_csv_field_limit(length: int) -> None:
    blob = (bytes(range(256)) * ((length + 255) // 256))[:length]
    image = flash.nvs_image({"config": {"blob": blob}}, 0x20000)
    assert len(image) == 0x20000
    entries = nvs_entries(image)
    assert entries[0] == (0, "config", 0x01, b"\x01" + b"\xff" * 7)
    chunks = entries[1:-1]
    assert all((namespace, key, kind) == (1, "blob", 0x42) for namespace, key, kind, _ in chunks)
    assert b"".join(value for _, _, _, value in chunks) == blob
    namespace, key, kind, index = entries[-1]
    assert (namespace, key, kind) == (1, "blob", 0x48)
    assert struct.unpack_from("<I", index)[0] == length
    assert index[4:6] == bytes([len(chunks), 0])


@pytest.mark.parametrize("key", ["café", "é" * 7 + "a", "🛰" * 3])
@pytest.mark.parametrize("preceding", [123, 124, 125])
def test_utf8_key_at_page_boundary_preserves_entries(key: str, preceding: int) -> None:
    values = {f"k{i}": u8(i) for i in range(preceding)}
    values[key] = u8(1)
    values["after"] = u16(0x1234)
    image = flash.nvs_image({"config": values}, 0x6000)
    assert len(image) == 0x6000
    assert nvs_entries(image) == [
        (0, "config", 0x01, b"\x01" + b"\xff" * 7),
        *((1, f"k{i}", 0x01, bytes([i]) + b"\xff" * 7) for i in range(preceding)),
        (1, key, 0x01, b"\x01" + b"\xff" * 7),
        (1, "after", 0x02, b"\x34\x12" + b"\xff" * 6),
    ]


def test_utf8_namespace_at_page_boundary_preserves_entries() -> None:
    image = flash.nvs_image(
        {
            "config": {f"k{i}": u8(i) for i in range(124)},
            "café": {"clé": "value\r\n#text", "données": b"\0\xff"},
        },
        0x6000,
    )
    assert len(image) == 0x6000
    entries = nvs_entries(image)
    assert entries[125:128] == [
        (0, "café", 0x01, b"\x02" + b"\xff" * 7),
        (2, "clé", 0x21, b"value\r\n#text\0"),
        (2, "données", 0x42, b"\0\xff"),
    ]
    assert len(entries) == 129
    assert entries[128][:3] == (2, "données", 0x48)


def test_generator_output_must_match_partition_size(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("esp32tl.flash.generator.nvs_open", Mock(return_value=nullcontext()))
    with pytest.raises(ValueError, match="NVS generator wrote 0 bytes, expected 12288"):
        flash.nvs_image({}, 0x3000)


def test_a_round_trip_through_the_generator_reads_what_went_in() -> None:
    namespaces: flash.Namespaces = {
        "config": {
            "text": 'café, "quoted"\r\n#not a comment\n',
            "empty": "",
            "blob": b"\x00\xff\r\n",
            "no_blob": b"",
            "u8": u8(255),
            "i8": i8(-128),
            "u16": u16(65535),
            "i16": i16(-32768),
            "u32": u32(2**32 - 1),
            "i32": i32(-(2**31)),
            "u64": u64(2**64 - 1),
            "i64": i64(-(2**63)),
            "kóźlę": "ąę",
            "x" * 15: u8(1),
        },
        "nothing": {},
        "przestrzeń": {"config": u8(2)},
    }
    assert flash.nvs_values(flash.nvs_image(namespaces, 0x6000)) == namespaces


@pytest.mark.parametrize("length", [4000, 4064, 65535, 70000, 200000])
def test_a_blob_of_many_chunks_on_many_pages_round_trips(length: int) -> None:
    blob = bytes(i % 251 for i in range(length))
    image = flash.nvs_image({"big": {"blob": blob, "after": u8(1)}}, 0x60000)
    assert flash.nvs_values(image) == {"big": {"blob": blob, "after": u8(1)}}


def test_a_long_string_and_entries_across_pages_round_trip() -> None:
    namespaces = {"many": {f"key{i}": u32(i) for i in range(400)} | {"long": "ż" * 1900}}
    assert flash.nvs_values(flash.nvs_image(namespaces, 0x10000)) == namespaces


def test_a_version_1_blob_reads_as_bytes() -> None:
    from esp_idf_nvs_partition_gen import nvs_partition_gen as generator

    output = io.BytesIO()
    with generator.nvs_open(  # type: ignore[no-untyped-call]
        output, 0x3000 - 4096, generator.Page.VERSION1
    ) as nvs:
        nvs.write_namespace("old")
        nvs.write_entry("blob", b"\x01\x02", "binary")
    assert flash.nvs_values(output.getvalue()) == {"old": {"blob": b"\x01\x02"}}


# NVS pages written by hand, for what the generator never writes: pages out of order, what an
# erase or a power cut leaves.
WRITTEN, EMPTY, ERASED = 0b10, 0b11, 0b00
ACTIVE, FULL, FREEING = 0xFFFFFFFE, 0xFFFFFFFC, 0xFFFFFFF8


def nvs_item(
    namespace: int, kind: int, key: str, data: bytes, span: int = 1, chunk: int = 0xFF
) -> bytes:
    head = bytes((namespace, kind, span, chunk))
    rest = key.encode().ljust(16, b"\0") + data.ljust(8, b"\xff")
    return head + struct.pack("<I", zlib.crc32(head + rest, 0xFFFFFFFF)) + rest


def nvs_varlen(
    namespace: int, kind: int, key: str, payload: bytes, chunk: int = 0xFF
) -> list[bytes]:
    data = struct.pack("<HHI", len(payload), 0xFFFF, zlib.crc32(payload, 0xFFFFFFFF))
    padded = payload.ljust((len(payload) + 31) // 32 * 32, b"\xff")
    body = [padded[at : at + 32] for at in range(0, len(padded), 32)]
    return [nvs_item(namespace, kind, key, data, 1 + len(body), chunk), *body]


def nvs_page(
    sequence: int,
    entries: list[bytes],
    states: dict[int, int] | None = None,
    state: int = ACTIVE,
    version: int = 0xFE,
) -> bytes:
    """A page with `entries` written from its first slot, but those `states` names."""
    body = struct.pack("<IB", sequence, version) + b"\xff" * 19
    header = struct.pack("<I", state) + body + struct.pack("<I", zlib.crc32(body, 0xFFFFFFFF))
    bitmap = bytearray(b"\xff" * 32)
    for slot in range(len(entries)):
        slot_state = (states or {}).get(slot, WRITTEN)
        bitmap[slot // 4] &= ~(0b11 << (slot % 4 * 2)) & 0xFF
        bitmap[slot // 4] |= slot_state << (slot % 4 * 2)
    return (header + bytes(bitmap) + b"".join(entries)).ljust(4096, b"\xff")


NS = nvs_item(0, 0x01, "ns", b"\x01")


def boots(value: int) -> bytes:
    return nvs_item(1, 0x04, "boots", struct.pack("<I", value))


def test_the_state_bitmap_decides_what_is_there() -> None:
    assert flash.nvs_values(nvs_page(0, [NS, boots(1), boots(2)], {1: ERASED})) == {
        "ns": {"boots": u32(2)}
    }
    # erased, the key is gone, whatever its entry still holds
    assert flash.nvs_values(nvs_page(0, [NS, boots(1)], {1: ERASED})) == {"ns": {}}
    # written to the entry and not yet marked, as a power cut leaves it
    assert flash.nvs_values(nvs_page(0, [NS, boots(1), boots(2)], {2: EMPTY})) == {
        "ns": {"boots": u32(1)}
    }


def test_pages_are_read_by_sequence_number_not_by_their_place_in_the_partition() -> None:
    newer = nvs_page(7, [NS, boots(2)])
    older = nvs_page(3, [NS, boots(1)], state=FULL)
    assert flash.nvs_values(newer + older) == {"ns": {"boots": u32(2)}}
    assert flash.nvs_values(older + newer) == {"ns": {"boots": u32(2)}}


def test_a_key_written_twice_reads_as_the_later_write() -> None:
    # as a power cut leaves it: the new value written, the old one not yet erased
    assert flash.nvs_values(nvs_page(0, [NS, boots(1), boots(2)])) == {"ns": {"boots": u32(2)}}


def test_the_namespace_may_be_on_another_page_and_an_unknown_namespace_is_nothing() -> None:
    pages = nvs_page(1, [NS], state=FULL) + nvs_page(
        2, [boots(1), nvs_item(9, 0x01, "lost", b"\x01")]
    )
    assert flash.nvs_values(pages) == {"ns": {"boots": u32(1)}}


def test_a_page_that_holds_nothing_is_skipped() -> None:
    good = nvs_page(0, [NS, boots(1)])
    erased = b"\xff" * 4096
    corrupt = nvs_page(9, [NS, boots(9)], state=0xFFFFFFF0)
    invalid = nvs_page(9, [NS, boots(9)], state=0)
    bad_crc = bytearray(nvs_page(9, [NS, boots(9)]))
    bad_crc[4] ^= 1
    assert flash.nvs_values(erased + good + corrupt + invalid + bytes(bad_crc)) == {
        "ns": {"boots": u32(1)}
    }
    assert flash.nvs_values(erased) == {}


def test_a_page_of_a_newer_format_is_refused() -> None:
    with pytest.raises(ValueError, match=r"NVS page at 0x1000: format 0xfd is newer"):
        flash.nvs_values(b"\xff" * 4096 + nvs_page(0, [NS], version=0xFD))


def test_an_item_whose_crc_fails_is_not_there() -> None:
    bad = bytearray(boots(1))
    bad[24] ^= 1
    assert flash.nvs_values(nvs_page(0, [NS, bytes(bad)])) == {"ns": {}}


def test_a_string_whose_span_or_data_is_incomplete_is_not_there() -> None:
    string = nvs_varlen(1, 0x21, "s", b"x" * 40 + b"\0")
    assert flash.nvs_values(nvs_page(0, [NS, *string])) == {"ns": {"s": "x" * 40}}
    # a power cut before the last entry of its span was marked
    assert flash.nvs_values(nvs_page(0, [NS, *string], {3: EMPTY})) == {"ns": {}}
    torn = list(string)
    torn[1] = b"y" * 32
    assert flash.nvs_values(nvs_page(0, [NS, *torn])) == {"ns": {}}
    # a span past the end of the page
    long = nvs_item(1, 0x21, "s", struct.pack("<HHI", 1, 0xFFFF, 0), span=127)
    assert flash.nvs_values(nvs_page(0, [NS, long])) == {"ns": {}}


def test_the_data_of_a_string_or_a_blob_is_never_read_as_entries() -> None:
    ghost = nvs_item(1, 0x01, "ghost", b"\x01")
    real = nvs_item(1, 0x01, "real", b"\x01")
    for kind, chunk in ((0x21, 0xFF), (0x42, 0)):
        whole = nvs_varlen(1, kind, "s", ghost + b"tail\0", chunk=chunk)
        assert "ghost" not in flash.nvs_values(nvs_page(0, [NS, *whole, real]))["ns"]
        # its data's CRC fails: the item is gone, and its span with it
        torn = [*whole[:-1], b"\x00" + whole[-1][1:]]
        assert flash.nvs_values(nvs_page(0, [NS, *torn, real])) == {"ns": {"real": u8(1)}}
        # its last entry not marked written, as a power cut leaves it: the same
        assert flash.nvs_values(nvs_page(0, [NS, *whole, real], {3: EMPTY})) == {
            "ns": {"real": u8(1)}
        }


@pytest.mark.parametrize(
    "item",
    [
        # an integer takes one entry
        nvs_item(1, 0x01, "k", b"\x01", span=2),
        # a blob's index takes one entry, and belongs to no chunk
        nvs_item(1, 0x48, "k", struct.pack("<IBB", 0, 0, 0), span=2),
        nvs_item(1, 0x48, "k", struct.pack("<IBB", 0, 0, 0), chunk=0),
        # a span that is not what the data's size needs
        nvs_item(
            1, 0x21, "k", struct.pack("<HHI", 1, 0xFFFF, zlib.crc32(b"\0", 0xFFFFFFFF)), span=3
        ),
        # a type NVS does not have
        nvs_item(1, 0x77, "k", b"\x01"),
    ],
    ids=["integer-span", "index-span", "index-chunk", "string-span", "unknown-type"],
)
def test_an_entry_whose_header_is_inconsistent_is_one_entry_of_nothing(item: bytes) -> None:
    after = nvs_item(1, 0x01, "after", b"\x01")
    assert flash.nvs_values(nvs_page(0, [NS, item, after])) == {"ns": {"after": u8(1)}}


def test_a_blob_chunk_that_says_it_belongs_to_no_chunk_is_not_one() -> None:
    chunk = nvs_varlen(1, 0x42, "b", b"abc", chunk=0xFF)
    assert flash.nvs_values(nvs_page(0, [NS, *chunk, blob_index(3, 1)])) == {"ns": {}}


def blob_index(size: int, count: int, start: int = 0) -> bytes:
    return nvs_item(1, 0x48, "b", struct.pack("<IBBH", size, count, start, 0xFFFF))


def test_a_blob_needs_all_of_its_chunks_and_a_new_version_replaces_the_old() -> None:
    first = nvs_varlen(1, 0x42, "b", b"old", chunk=0)
    second = nvs_varlen(1, 0x42, "b", b"new-", chunk=0x80)
    third = nvs_varlen(1, 0x42, "b", b"blob", chunk=0x81)
    whole = [NS, *first, blob_index(3, 1)]
    assert flash.nvs_values(nvs_page(0, whole)) == {"ns": {"b": b"old"}}
    # the new version's chunks written, its index not yet: the old one holds
    assert flash.nvs_values(nvs_page(0, [*whole, *second, *third])) == {"ns": {"b": b"old"}}
    # its index written too, and the old version not yet erased: the new one holds
    updated = [*whole, *second, *third, blob_index(8, 2, 0x80)]
    assert flash.nvs_values(nvs_page(0, updated)) == {"ns": {"b": b"new-blob"}}
    # an index without one of its chunks holds nothing: the old version does
    assert flash.nvs_values(nvs_page(0, [*whole, *second, blob_index(8, 2, 0x80)])) == {
        "ns": {"b": b"old"}
    }
    # nor one whose chunks do not add up to its size
    assert flash.nvs_values(nvs_page(0, [NS, *first, blob_index(4, 1)])) == {"ns": {}}


def test_the_chunks_of_a_blob_may_be_on_different_pages() -> None:
    first = nvs_page(1, [NS, *nvs_varlen(1, 0x42, "b", b"abc", chunk=0)], state=FULL)
    second = nvs_page(2, [*nvs_varlen(1, 0x42, "b", b"def", chunk=1), blob_index(6, 2)])
    assert flash.nvs_values(second + first) == {"ns": {"b": b"abcdef"}}


def test_the_partial_copy_of_a_page_being_freed_is_dropped() -> None:
    freeing = nvs_page(1, [NS, boots(1)], state=FREEING)
    copy = nvs_page(2, [boots(7)])
    assert flash.nvs_values(freeing + copy) == {"ns": {"boots": u32(1)}}
    # the same copy with no page being freed is just the newest page
    assert flash.nvs_values(nvs_page(1, [NS, boots(1)], state=FULL) + copy) == {
        "ns": {"boots": u32(7)}
    }


def test_a_string_that_is_not_utf_8_reads_without_failing_the_rest() -> None:
    strange = nvs_varlen(1, 0x21, "s", b"caf\xe9\0")
    values = flash.nvs_values(nvs_page(0, [NS, *strange, boots(1)]))
    assert values == {"ns": {"s": "caf\udce9", "boots": u32(1)}}


TABLE = {
    "nvs": Partition("nvs", 1, 2, 0x100, 0x3000),
    "secret": Partition("secret", 1, 2, 0x100, 0x3000, flags=1),
    "readonly": Partition("readonly", 1, 2, 0x100, 0x3000, flags=2),
    "app": Partition("app", 0, 0, 0x100, 0x100),
    "past": Partition("past", 1, 2, 0x4000, 0x1000),
}


def test_a_snapshot_reads_by_the_table_and_refuses_what_is_not_nvs() -> None:
    image = flash.nvs_image({"ns": {"k": u8(1)}}, 0x3000)
    snapshot = FlashSnapshot(b"\xaa" * 0x100 + image, TABLE)
    assert snapshot.partition("nvs") == image
    assert snapshot.partition("app") == image[:0x100]
    assert snapshot.nvs() == {"ns": {"k": u8(1)}}
    assert snapshot.nvs("readonly") == {"ns": {"k": u8(1)}}
    with pytest.raises(ValueError) as encrypted:
        snapshot.nvs("secret")
    assert str(encrypted.value) == "board.flash: partition 'secret' is not unencrypted NVS"
    with pytest.raises(ValueError, match=r"board\.flash: partition 'app' is not unencrypted NVS"):
        snapshot.nvs("app")
    with pytest.raises(ValueError) as missing:
        snapshot.partition("x")
    assert str(missing.value) == (
        "the build has no partition 'x': it has nvs, secret, readonly, app, past"
    )
    with pytest.raises(ValueError, match="partition 'past' ends past the flash"):
        snapshot.partition("past")


def test_a_snapshot_is_immutable_and_its_repr_leaves_the_flash_out() -> None:
    snapshot = FlashSnapshot(
        flash.nvs_image({"ns": {"k": u8(1)}}, 0x3000), {"nvs": Partition("nvs", 1, 2, 0, 0x3000)}
    )
    with pytest.raises(AttributeError):
        snapshot.contents = b""  # type: ignore[misc]
    read = snapshot.nvs()
    read["ns"]["k"] = u8(2)
    assert snapshot.nvs() == {"ns": {"k": u8(1)}}
    assert "\\xff" not in repr(snapshot) and len(repr(snapshot)) < 200


def ota_entry(seq: int, state: int, crc: int | None = None) -> bytes:
    if crc is None:
        crc = zlib.crc32(struct.pack("<I", seq), 0xFFFFFFFF)
    return struct.pack("<I20sII", seq, b"\xff" * 20, state, crc)


def ota_snapshot(first: bytes, second: bytes, *apps: Partition) -> FlashSnapshot:
    data = first.ljust(0x1000, b"\xff") + second.ljust(0x1000, b"\xff")
    table = {"otadata": Partition("otadata", 1, 0, 0, 0x2000)} | {p.name: p for p in apps}
    return FlashSnapshot(data, table)


FACTORY = Partition("factory", 0, 0, 0x10000, 0x1000)
OTA_0 = Partition("ota_0", 0, 0x10, 0x20000, 0x1000)
OTA_1 = Partition("ota_1", 0, 0x11, 0x30000, 0x1000)
NO_ENTRY = b"\xff" * 32
UNSET = OtaEntry(0xFFFFFFFF, "undefined", False)
NEW, PENDING, VALID, INVALID, ABORTED = 0, 1, 2, 3, 4


def test_ota_with_erased_data_boots_the_factory_app_or_the_first_slot() -> None:
    assert ota_snapshot(NO_ENTRY, NO_ENTRY, OTA_0, OTA_1, FACTORY).ota() == OtaData(
        "factory", (UNSET, UNSET)
    )
    assert ota_snapshot(NO_ENTRY, NO_ENTRY, OTA_1, OTA_0).ota().boot_partition == "ota_0"


def test_ota_boots_the_slot_of_the_highest_valid_sequence() -> None:
    apps = (FACTORY, OTA_0, OTA_1)
    assert ota_snapshot(ota_entry(1, VALID), ota_entry(2, NEW), *apps).ota() == OtaData(
        "ota_1", (OtaEntry(1, "valid", True), OtaEntry(2, "new", True))
    )
    assert ota_snapshot(ota_entry(3, PENDING), ota_entry(2, VALID), *apps).ota() == OtaData(
        "ota_0", (OtaEntry(3, "pending_verify", True), OtaEntry(2, "valid", True))
    )


@pytest.mark.parametrize(("state", "name"), [(INVALID, "invalid"), (ABORTED, "aborted")])
def test_ota_leaves_out_an_entry_that_failed(state: int, name: str) -> None:
    apps = (FACTORY, OTA_0, OTA_1)
    assert ota_snapshot(ota_entry(1, VALID), ota_entry(2, state), *apps).ota() == OtaData(
        "ota_0", (OtaEntry(1, "valid", True), OtaEntry(2, name, False))
    )
    # with no entry left, the default again
    assert ota_snapshot(NO_ENTRY, ota_entry(2, state), *apps).ota().boot_partition == "factory"


def test_ota_leaves_out_an_entry_whose_crc_fails_and_names_a_state_it_does_not_know() -> None:
    apps = (FACTORY, OTA_0, OTA_1)
    assert ota_snapshot(ota_entry(1, VALID), ota_entry(2, NEW, crc=7), *apps).ota() == OtaData(
        "ota_0", (OtaEntry(1, "valid", True), OtaEntry(2, "new", False))
    )
    assert ota_snapshot(ota_entry(1, 9), NO_ENTRY, *apps).ota().entries[0].state == "0x9"


def test_ota_refuses_a_build_without_what_it_needs() -> None:
    snapshot = FlashSnapshot(b"", {"nvs": Partition("nvs", 1, 2, 0, 0)})
    with pytest.raises(ValueError) as missing:
        snapshot.ota()
    assert str(missing.value) == "the build has no OTA data partition: it has nvs"
    with pytest.raises(ValueError, match=r"board\.flash: the build has no app partition"):
        ota_snapshot(NO_ENTRY, NO_ENTRY).ota()


def test_ota_counts_the_slots_from_ota_0_to_the_first_gap_as_esp_idf_does() -> None:
    ota_2 = Partition("ota_2", 0, 0x12, 0x40000, 0x1000)
    # ota_0 and ota_2 are one slot: every sequence selects ota_0
    assert ota_snapshot(ota_entry(2, VALID), NO_ENTRY, OTA_0, ota_2).ota().boot_partition == "ota_0"
    # no slot at ota_0 is no slot at all: the default, whatever the entries say
    assert ota_snapshot(ota_entry(1, VALID), NO_ENTRY, OTA_1).ota().boot_partition == "ota_1"
    # three slots and a sequence of 0: (0 - 1) wraps in 32 bits, and 0xffffffff % 3 is 0
    three = (OTA_0, OTA_1, ota_2)
    assert ota_snapshot(ota_entry(0, VALID), NO_ENTRY, *three).ota().boot_partition == "ota_0"
