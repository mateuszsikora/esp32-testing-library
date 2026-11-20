"""Flash contents from a test: a named partition of the build's table, NVS supplied before
power-on, and what a snapshot of the running board's flash holds (NVS, OTA state)."""

import hashlib
import io
import struct
import zlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Literal

from esp_idf_nvs_partition_gen import nvs_partition_gen as generator

ENTRY = struct.Struct("<HBBII16sI")
ENTRY_MAGIC = 0x50AA
MD5_MAGIC = 0xEBEB


@dataclass(frozen=True)
class Partition:
    name: str
    type: int
    subtype: int
    offset: int
    size: int
    flags: int = 0


def partitions(table: bytes) -> dict[str, Partition]:
    """Read ESP-IDF's 32-byte entries, optional MD5 record and erased padding by name."""
    found: dict[str, Partition] = {}
    for at in range(0, len(table), ENTRY.size):
        record = table[at : at + ENTRY.size]
        if len(record) != ENTRY.size:
            raise ValueError(f"partition table: truncated entry at {at:#x}")
        if record == b"\xff" * ENTRY.size:
            if any(b != 0xFF for b in table[at:]):
                raise ValueError("partition table: data after erased padding")
            break
        magic, kind, subtype, offset, size, label, flags = ENTRY.unpack(record)
        if magic == MD5_MAGIC:
            if record[:16] != b"\xeb\xeb" + b"\xff" * 14:
                raise ValueError("partition table: malformed MD5 record")
            if record[16:] != hashlib.md5(table[:at]).digest():
                raise ValueError("partition table: MD5 mismatch")
            if any(b != 0xFF for b in table[at + ENTRY.size :]):
                raise ValueError("partition table: data after MD5 record")
            break
        if magic != ENTRY_MAGIC:
            raise ValueError(f"partition table: invalid magic {magic:#x} at {at:#x}")
        name = label.split(b"\0", 1)[0].decode("utf-8")
        if not name or name in found:
            raise ValueError(f"partition table: empty or duplicate name {name!r}")
        found[name] = Partition(name, kind, subtype, offset, size, flags)
    if not found:
        raise ValueError("partition table: no partitions")
    return found


def no_partition(name: str, table: Mapping[str, Partition]) -> str:
    """What `board.nvs` and a snapshot say of a name the build's table does not have."""
    return f"the build has no partition {name!r}: it has {', '.join(table)}"


Encoding = Literal["u8", "i8", "u16", "i16", "u32", "i32", "u64", "i64"]
RANGES = {
    "u8": (0, 2**8 - 1),
    "i8": (-(2**7), 2**7 - 1),
    "u16": (0, 2**16 - 1),
    "i16": (-(2**15), 2**15 - 1),
    "u32": (0, 2**32 - 1),
    "i32": (-(2**31), 2**31 - 1),
    "u64": (0, 2**64 - 1),
    "i64": (-(2**63), 2**63 - 1),
}


@dataclass(frozen=True)
class Typed:
    """An integer with the NVS encoding the firmware uses to read it."""

    type: Encoding
    value: int

    def __post_init__(self) -> None:
        if self.type not in RANGES:
            raise ValueError(f"unsupported NVS integer encoding {self.type!r}")
        if type(self.value) is not int:
            raise TypeError(f"{self.type}() needs an int, got {self.value!r}")
        low, high = RANGES[self.type]
        if not low <= self.value <= high:
            raise ValueError(f"{self.type}() needs {low} <= value <= {high}, got {self.value}")


def u8(value: int) -> Typed:
    return Typed("u8", value)


def i8(value: int) -> Typed:
    return Typed("i8", value)


def u16(value: int) -> Typed:
    return Typed("u16", value)


def i16(value: int) -> Typed:
    return Typed("i16", value)


def u32(value: int) -> Typed:
    return Typed("u32", value)


def i32(value: int) -> Typed:
    return Typed("i32", value)


def u64(value: int) -> Typed:
    return Typed("u64", value)


def i64(value: int) -> Typed:
    return Typed("i64", value)


Value = str | bytes | Typed
Namespaces = Mapping[str, Mapping[str, Value]]


class _NvsKey(str):
    """A name whose length is its UTF-8 byte count, only at the generator boundary.

    Generator 0.3.0 copies key.encode() into entry_struct[8:8 + len(key)] in both
    Page.write_primitive_data and Page.write_varlen_data. A normal Unicode str grows
    that bytearray and can corrupt the following entry or extend the page. Keep the
    text and encoding intact, but make the slice exactly as long as the encoded key.
    """

    def __len__(self) -> int:
        return len(self.encode("utf-8"))


def _name(name: str) -> _NvsKey:
    if (
        not isinstance(name, str)
        or not name
        or any(c in name for c in "\0\r\n")
        or len(name.encode("utf-8")) > 15
    ):
        raise ValueError(f"NVS name {name!r}: use 1 to 15 UTF-8 bytes without NUL, CR or LF")
    return _NvsKey(name)


def _value(namespace: str, key: str, value: Value) -> tuple[str, str | bytes | int]:
    """Validate a value and select its NVS encoding without serialising it through CSV."""
    if isinstance(value, Typed):
        return value.type, value.value
    if isinstance(value, str):
        if "\0" in value:
            raise ValueError(f"{namespace}/{key}: an NVS string cannot contain NUL")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            # what FlashSnapshot.nvs reads of a string that is not UTF-8, which the generator
            # cannot write back
            raise ValueError(
                f"{namespace}/{key}: an NVS string is written as UTF-8, and this one holds bytes"
                " that are not (surrogates): give the key a value of its own"
            ) from None
        return "string", value
    if isinstance(value, bytes):
        return "binary", value
    raise TypeError(
        f"{namespace}/{key}: use str, bytes, u8(), i8(), u16(), i16(), u32(), i32(), u64() or"
        " i64(); a bare int has no NVS width"
    )


def nvs_image(namespaces: Namespaces, size: int) -> bytes:
    """Generate a whole NVS partition using Espressif's generator, without an ESP-IDF install."""
    if size < 0x3000 or size % 0x1000:
        raise ValueError("NVS partition size must be a multiple of 4096 and at least 12288 bytes")
    output = io.BytesIO()
    try:
        # The API takes the usable size, excluding the final reserved page. Use the
        # NVS methods directly: the CLI's write_entry wrapper interprets Wi-Fi key
        # names and adds unsolicited entries.
        nvs = generator.nvs_open(  # type: ignore[no-untyped-call]
            output, size - 4096, generator.Page.VERSION2
        )
        with nvs:
            for namespace, values in namespaces.items():
                nvs.write_namespace(_name(namespace))
                for key, value in values.items():
                    name = _name(key)
                    encoding, content = _value(namespace, key, value)
                    nvs.write_entry(name, content, encoding)
    except (generator.InputError, generator.InsufficientSizeError) as error:
        raise ValueError(f"NVS generator failed: {error}") from error
    image = output.getvalue()
    if len(image) != size:
        raise ValueError(f"NVS generator wrote {len(image)} bytes, expected {size}")
    return image


# ESP-IDF's NVS format, at the pin, v5.3.6: components/nvs_flash/src/nvs_page.hpp, nvs_types.hpp.
PAGE_SIZE = 4096
PAGE_ENTRIES = 126
NVS_ENTRY_SIZE = 32
NVS_ENTRIES_AT = 64
PAGE_ACTIVE, PAGE_FULL, PAGE_FREEING = 0xFFFFFFFE, 0xFFFFFFFC, 0xFFFFFFF8
# Version 2's byte, which ESP-IDF writes now: 0xff is version 1, and a lower byte a newer format.
PAGE_VERSION2 = 0xFE
ENTRY_WRITTEN = 0b10
NVS_INTEGERS: dict[int, tuple[Encoding, str]] = {
    0x01: ("u8", "<B"),
    0x11: ("i8", "<b"),
    0x02: ("u16", "<H"),
    0x12: ("i16", "<h"),
    0x04: ("u32", "<I"),
    0x14: ("i32", "<i"),
    0x08: ("u64", "<Q"),
    0x18: ("i64", "<q"),
}
NVS_U8, NVS_STRING, NVS_BLOB_V1, NVS_BLOB_DATA, NVS_BLOB_INDEX = 0x01, 0x21, 0x41, 0x42, 0x48
NVS_VARIABLE = (NVS_STRING, NVS_BLOB_V1, NVS_BLOB_DATA)
# `chunkIndex` of everything that is not a blob's chunk.
NVS_NO_CHUNK = 0xFF


@dataclass(frozen=True)
class _Item:
    """A written NVS item that holds: `data` its 8 bytes, `payload` what its span holds."""

    namespace: int
    type: int
    chunk: int
    key: str
    data: bytes
    payload: bytes = b""


def _crc(data: bytes) -> int:
    """`esp_rom_crc32_le(0xffffffff, ...)`, as NVS and the OTA data use it."""
    return zlib.crc32(data, 0xFFFFFFFF)


def _nvs_pages(image: bytes) -> list[bytes]:
    """The pages that hold data, oldest first, as `PageManager::load` orders them: by sequence
    number, not by where they are in the partition. A page being freed is complete, and the
    active page after it is a partial copy of it, which `PageManager::load` drops."""
    found: list[tuple[int, int, bytes]] = []
    for at in range(0, len(image) - PAGE_SIZE + 1, PAGE_SIZE):
        page = image[at : at + PAGE_SIZE]
        state, sequence, version = struct.unpack_from("<IIB", page)
        (crc,) = struct.unpack_from("<I", page, 28)
        if state not in (PAGE_ACTIVE, PAGE_FULL, PAGE_FREEING) or crc != _crc(page[4:28]):
            continue
        if version < PAGE_VERSION2:
            raise ValueError(f"NVS page at {at:#x}: format {version:#x} is newer than version 2")
        found.append((sequence, state, page))
    found.sort(key=lambda entry: entry[0])
    if found and found[-1][1] == PAGE_ACTIVE and any(e[1] == PAGE_FREEING for e in found):
        found.pop()
    return [page for _, _, page in found]


def _header_holds(index: int, kind: int, span: int, chunk: int, data: bytes) -> bool:
    """`Item::checkHeaderConsistency`, but for the CRC: what the header of an entry at `index` must
    say of its type, its span and its chunk."""
    if kind in NVS_INTEGERS:
        return span == 1
    if kind == NVS_BLOB_INDEX:
        return span == 1 and chunk == NVS_NO_CHUNK
    if kind not in NVS_VARIABLE or (kind == NVS_BLOB_DATA and chunk == NVS_NO_CHUNK):
        return False
    (size,) = struct.unpack_from("<H", data)
    return (
        index + span <= PAGE_ENTRIES and span == 1 + (size + NVS_ENTRY_SIZE - 1) // NVS_ENTRY_SIZE
    )


def _nvs_items(page: bytes) -> Iterator[_Item]:
    """What `Page::mLoadEntryTable` and `Page::readItem` keep of a page: entries the state bitmap
    says are written, whose header holds, and, for a string or a blob's chunk, whose whole span is
    written and whose data's CRC holds. The rest is what an erase or a power cut left. A header that
    holds owns its span, kept or not, so that its data is never read as entries; one that does not
    is one entry of nothing, as `Page::eraseEntryAndSpan` has it."""

    def written(index: int) -> bool:
        return (page[32 + index // 4] >> (index % 4 * 2)) & 0b11 == ENTRY_WRITTEN

    index = 0
    while index < PAGE_ENTRIES:
        at = NVS_ENTRIES_AT + index * NVS_ENTRY_SIZE
        raw = page[at : at + NVS_ENTRY_SIZE]
        namespace, kind, span, chunk, crc = struct.unpack_from("<BBBBI", raw)
        data = raw[24:]
        if (
            not written(index)
            or crc != _crc(raw[:4] + raw[8:])
            or not _header_holds(index, kind, span, chunk, data)
        ):
            index += 1
            continue
        key = raw[8:24].split(b"\0", 1)[0].decode("utf-8", "surrogateescape")
        if kind not in NVS_VARIABLE:
            yield _Item(namespace, kind, chunk, key, data)
            index += 1
            continue
        size, _, data_crc = struct.unpack("<HHI", data)
        payload = page[at + NVS_ENTRY_SIZE :][:size]
        if all(written(i) for i in range(index, index + span)) and data_crc == _crc(payload):
            yield _Item(namespace, kind, chunk, key, data, payload)
        index += span


def nvs_values(image: bytes) -> dict[str, dict[str, Value]]:
    """The namespaces and keys of a whole NVS partition, as `nvs_get_*` finds them: what was
    erased is gone, and of a key written twice, as a power cut leaves it, the later write holds,
    by page sequence and then by place in the page. A key written with two types, which NVS keeps
    apart, reads as the later."""
    items = [item for page in _nvs_pages(image) for item in _nvs_items(page)]
    names = {i.data[0]: i.key for i in items if i.namespace == 0 and i.type == NVS_U8}
    chunks = {(i.namespace, i.key, i.chunk): i.payload for i in items if i.type == NVS_BLOB_DATA}
    values: dict[str, dict[str, Value]] = {name: {} for name in names.values()}
    for item in items:
        if item.namespace == 0 or item.namespace not in names:
            continue
        value = _nvs_value(item, chunks)
        if value is not None:
            values[names[item.namespace]][item.key] = value
    return values


def _nvs_value(item: _Item, chunks: Mapping[tuple[int, str, int], bytes]) -> Value | None:
    """The value of `item`; `None` for a blob's chunk, and for an index whose chunks are not all
    there, which `Storage::eraseMismatchedBlobIndexes` erases."""
    if item.type in NVS_INTEGERS:
        encoding, layout = NVS_INTEGERS[item.type]
        return Typed(encoding, struct.unpack_from(layout, item.data)[0])
    if item.type == NVS_STRING:
        return item.payload.split(b"\0", 1)[0].decode("utf-8", "surrogateescape")
    if item.type == NVS_BLOB_V1:
        return item.payload
    if item.type != NVS_BLOB_INDEX:
        return None
    size, count, start = struct.unpack_from("<IBB", item.data)
    parts = [chunks.get((item.namespace, item.key, start + n)) for n in range(count)]
    blob = b"".join(part for part in parts if part is not None)
    return blob if None not in parts and len(blob) == size else None


# The states of `esp_ota_img_states_t`, esp_flash_partitions.h.
OTA_STATES = {
    0: "new",
    1: "pending_verify",
    2: "valid",
    3: "invalid",
    4: "aborted",
    0xFFFFFFFF: "undefined",
}
# `invalid` and `aborted`: the bootloader does not select them
# (`bootloader_common_ota_select_invalid`).
OTA_FAILED = (3, 4)
OTA_ENTRY = struct.Struct("<I20sII")
OTA_SECTOR = 0x1000
OTA_NO_SEQ = 0xFFFFFFFF
TYPE_APP, TYPE_DATA, SUBTYPE_FACTORY, SUBTYPE_OTA_DATA, SUBTYPE_NVS = 0, 1, 0, 0, 2
SUBTYPE_OTA_MIN, SUBTYPE_OTA_MAX, SUBTYPE_TEST = 0x10, 0x20, 0x20
FLAG_ENCRYPTED = 1


@dataclass(frozen=True)
class OtaEntry:
    """One copy of `esp_ota_select_entry_t`. `state` names `esp_ota_img_states_t`, as `new` or
    `pending_verify`; `valid` is `bootloader_common_ota_select_valid`, which also leaves out an
    entry in state `invalid` or `aborted`."""

    seq: int
    state: str
    valid: bool


@dataclass(frozen=True)
class OtaData:
    """The OTA data partition: the two entries, and the name of the app partition they select now,
    as `esp_ota_get_boot_partition` finds it. With rollback on, the bootloader aborts an entry still
    `pending_verify` at its next start, before it selects: the next boot may be another slot."""

    boot_partition: str
    entries: tuple[OtaEntry, ...]


@dataclass(frozen=True)
class FlashSnapshot:
    """The whole flash at one instant of a run, and the build's partition table to read it by."""

    contents: bytes = field(repr=False)
    table: Mapping[str, Partition] = field(hash=False)

    def partition(self, name: str) -> bytes:
        """The bytes of the partition `name` of the build's table."""
        return self._bytes(self._entry(name))

    def nvs(self, partition: str = "nvs") -> dict[str, dict[str, Value]]:
        """The NVS partition's namespaces and keys, in the shape `board.nvs(...)` takes: a new
        mapping each time."""
        entry = self._entry(partition)
        if (entry.type, entry.subtype) != (TYPE_DATA, SUBTYPE_NVS) or entry.flags & FLAG_ENCRYPTED:
            raise ValueError(f"board.flash: partition {partition!r} is not unencrypted NVS")
        return nvs_values(self._bytes(entry))

    def ota(self) -> OtaData:
        """The OTA data partition's entries, and what they select, as `esp_ota_get_boot_partition`
        reads them."""
        found = [
            p for p in self.table.values() if (p.type, p.subtype) == (TYPE_DATA, SUBTYPE_OTA_DATA)
        ]
        if not found:
            raise ValueError(f"the build has no OTA data partition: it has {self._names()}")
        data = self._bytes(found[0])
        raw = [OTA_ENTRY.unpack_from(data, at) for at in (0, OTA_SECTOR)]
        entries = tuple(
            OtaEntry(
                seq,
                OTA_STATES.get(state, f"{state:#x}"),
                seq != OTA_NO_SEQ
                and state not in OTA_FAILED
                and crc == _crc(struct.pack("<I", seq)),
            )
            for seq, _, state, crc in raw
        )
        return OtaData(self._boot_partition(entries), entries)

    def _boot_partition(self, entries: tuple[OtaEntry, ...]) -> str:
        apps = {p.subtype: p.name for p in self.table.values() if p.type == TYPE_APP}
        # `esp_ota_get_app_partition_count`: from ota_0, to the first slot the table does not have.
        count = 0
        while SUBTYPE_OTA_MIN + count < SUBTYPE_OTA_MAX and SUBTYPE_OTA_MIN + count in apps:
            count += 1
        valid = [entry for entry in entries if entry.valid]
        if not count or not valid:
            return self._default_boot_partition()
        # `ota_seq - 1` on a `uint32_t`: a sequence of 0 wraps.
        slot = ((max(entry.seq for entry in valid) - 1) & 0xFFFFFFFF) % count
        return apps[SUBTYPE_OTA_MIN + slot]

    def _default_boot_partition(self) -> str:
        """`find_default_boot_partition`: the factory app, else the first OTA slot, else the test
        app."""
        subtypes = [SUBTYPE_FACTORY, *range(SUBTYPE_OTA_MIN, SUBTYPE_OTA_MAX), SUBTYPE_TEST]
        for subtype in subtypes:
            for partition in self.table.values():
                if (partition.type, partition.subtype) == (TYPE_APP, subtype):
                    return partition.name
        raise ValueError(f"board.flash: the build has no app partition: it has {self._names()}")

    def _entry(self, name: str) -> Partition:
        if name not in self.table:
            raise ValueError(no_partition(name, self.table))
        return self.table[name]

    def _bytes(self, entry: Partition) -> bytes:
        if entry.offset + entry.size > len(self.contents):
            raise ValueError(
                f"board.flash: partition {entry.name!r} ends past the flash, of"
                f" {len(self.contents)} bytes"
            )
        return self.contents[entry.offset : entry.offset + entry.size]

    def _names(self) -> str:
        return ", ".join(self.table)
