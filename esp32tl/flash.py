"""Contents of a named partition in the build's flash, supplied before power-on."""

import hashlib
import io
import struct
from collections.abc import Mapping
from dataclasses import dataclass
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


Encoding = Literal["u8", "u16", "u32", "i32"]
RANGES = {"u8": (0, 255), "u16": (0, 65535), "u32": (0, 2**32 - 1), "i32": (-(2**31), 2**31 - 1)}


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


def u16(value: int) -> Typed:
    return Typed("u16", value)


def u32(value: int) -> Typed:
    return Typed("u32", value)


def i32(value: int) -> Typed:
    return Typed("i32", value)


Namespaces = Mapping[str, Mapping[str, str | bytes | Typed]]


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


def _value(namespace: str, key: str, value: str | bytes | Typed) -> tuple[str, str | bytes | int]:
    """Validate a value and select its NVS encoding without serialising it through CSV."""
    if isinstance(value, Typed):
        return value.type, value.value
    if isinstance(value, str):
        if "\0" in value:
            raise ValueError(f"{namespace}/{key}: an NVS string cannot contain NUL")
        return "string", value
    if isinstance(value, bytes):
        return "binary", value
    raise TypeError(
        f"{namespace}/{key}: use str, bytes, u8(), u16(), u32() or i32();"
        " a bare int has no NVS width"
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
