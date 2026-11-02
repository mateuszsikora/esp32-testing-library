"""FAT image preparation, byte fidelity and ownership through the public SD API."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from esp32tl.boards import SdCardRoute
from esp32tl.interfaces import SourceError
from esp32tl.interfaces.sd_card import SdCard


@pytest.fixture
def slot(tmp_path: Path) -> Iterator[SdCard]:
    card = SdCard(SdCardRoute(sclk=18, mosi=23, miso=19, cs=5), directory=tmp_path)
    try:
        yield card
    finally:
        card.close()


@pytest.mark.requires_mtools
@pytest.mark.parametrize("size_mb", [1, 32, 512])
def test_fat_variants_preserve_bytes_text_and_nested_files(slot: SdCard, size_mb: int) -> None:
    binary = bytes(range(256))
    card = (
        slot.image(size_mb=size_mb)
        .with_bytes("DATA.BIN", binary)
        .with_text("LOGS/FIRST.TXT", "zażółć\r\nsecond\n")
        .with_text("logs/NESTED/LATIN.TXT", "café", encoding="latin-1")
        .build()
    )
    assert slot.image_path is None  # Building never inserts a card.
    assert card.path.stat().st_size == size_mb << 20
    assert card.read_bytes("data.bin") == binary
    assert card.read_text("LOGS/FIRST.TXT") == "zażółć\r\nsecond\n"
    assert card.read_bytes("logs/nested/latin.txt") == b"caf\xe9"
    assert card.read_text("logs/nested/latin.txt", encoding="latin-1") == "café"
    assert card.list_files() == ["DATA.BIN"]
    assert card.list_files("LOGS") == ["FIRST.TXT"]
    assert card.list_files("logs/nested") == ["LATIN.TXT"]
    assert slot.insert(card) == card.path


@pytest.mark.requires_mtools
def test_each_build_is_independent_and_all_owned_images_are_cleaned(slot: SdCard) -> None:
    builder = slot.image().with_text("FIRST.TXT", "first")
    first = builder.build()
    second = builder.with_text("SECOND.TXT", "second").build()
    assert first.path != second.path
    assert first.list_files() == ["FIRST.TXT"]
    assert second.list_files() == ["FIRST.TXT", "SECOND.TXT"]
    slot.insert(first)
    slot.close()
    assert not first.path.exists()
    assert not second.path.exists()
    with pytest.raises(SourceError, match="board is closed"):
        first.read_bytes("FIRST.TXT")
    with pytest.raises(SourceError, match="board is closed"):
        builder.build()


def test_external_images_survive_slot_close(slot: SdCard, tmp_path: Path) -> None:
    path = tmp_path / "external.img"
    with path.open("wb") as stream:
        stream.truncate(512 * 1024)
    slot.insert(path)
    slot.close()
    assert path.exists()


@pytest.mark.parametrize("name", ["/ROOT", "../OUT", "A/../OUT", "A//B", "A/*", "C:X", "A\\B"])
def test_paths_cannot_escape_or_expand_as_mtools_patterns(slot: SdCard, name: str) -> None:
    with pytest.raises(ValueError, match="invalid card file path"):
        slot.image().with_text(name, "content")


@pytest.mark.parametrize("name", ["a.txt", "A.TXT/child", "DIR"])
def test_fat_case_and_file_directory_collisions_are_rejected(slot: SdCard, name: str) -> None:
    builder = slot.image().with_bytes("A.TXT", b"").with_bytes("dir/file", b"")
    with pytest.raises(ValueError, match="conflicts"):
        builder.with_bytes(name, b"")


@pytest.mark.parametrize("size", [0, -1, 2049, True])
def test_invalid_capacity_is_refused_before_allocating(slot: SdCard, size: int) -> None:
    with pytest.raises(ValueError, match="size_mb"):
        slot.image(size_mb=size)
    assert slot._temporary is None


def test_missing_mtools_names_the_dependency(slot: SdCard, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("esp32tl.interfaces.sd_card.shutil.which", lambda command: None)
    with pytest.raises(
        SourceError, match="missing mformat, mcopy, mmd, mtype, mdir; install mtools"
    ):
        slot.image().build()
    assert slot._temporary is None


@pytest.mark.requires_mtools
def test_failed_build_removes_the_partial_image(slot: SdCard) -> None:
    # A file larger than the volume must not leave a partial card behind.
    with pytest.raises(SourceError, match="mcopy failed"):
        slot.image(size_mb=1).with_bytes("FULL.BIN", b"x" * (2 << 20)).build()
    assert slot._temporary is not None
    assert list(Path(slot._temporary.name).iterdir()) == []
