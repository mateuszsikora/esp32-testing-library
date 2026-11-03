"""Partition parsing, the NVS generator's input, and pre-boot flash overrides."""

import hashlib
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
from esp32tl.flash import i32, u8, u16, u32


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
        (0, "other", 0x01, b"\x02" + b"\xff" * 7),
        (2, "empty", 0x21, b"\0"),
        (2, "blob", 0x42, b""),
        (2, "blob", 0x48, struct.pack("<IBB", 0, 1, 0) + b"\xff" * 2),
    ]


@pytest.mark.parametrize("value", [1, True, 1.2, None, bytearray(b"a")])
def test_nvs_requires_a_type_for_integers_and_refuses_other_values(value: object) -> None:
    with pytest.raises(
        TypeError, match=r"config/key: use str, bytes, u8\(\), u16\(\), u32\(\) or i32\(\)"
    ):
        flash.nvs_image(cast(flash.Namespaces, {"config": {"key": value}}), 0x6000)


@pytest.mark.parametrize(
    ("wrap", "low", "high"),
    [(u8, 0, 255), (u16, 0, 65535), (u32, 0, 2**32 - 1), (i32, -(2**31), 2**31 - 1)],
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
