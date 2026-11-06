"""FAT image preparation, byte fidelity and ownership through the public SD API."""

import os
import re
import struct
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from esp32tl.boards import SdCardRoute
from esp32tl.interfaces import SourceError
from esp32tl.interfaces.sd_card import FAT32_CLUSTERS, SdCard, fat32_clusters


@pytest.fixture
def slot(tmp_path: Path) -> Iterator[SdCard]:
    card = SdCard(SdCardRoute(sclk=18, mosi=23, miso=19, cs=5), directory=tmp_path)
    try:
        yield card
    finally:
        card.close()


def minfo(image: os.PathLike[str]) -> str:
    env = {**os.environ, "MTOOLS_SKIP_CHECK": "1"}
    args = ["minfo", "-i", os.fspath(image), "::"]
    return subprocess.run(args, check=True, capture_output=True, text=True, env=env).stdout


def boot_sector(image: os.PathLike[str]) -> tuple[int, int, int]:
    """Sectors per cluster, whether the FATs' length is FAT32's field, and the count of clusters,
    from what the image says of itself."""
    with open(image, "rb") as stream:
        sector = stream.read(512)
    per_cluster = sector[13]
    reserved, fats, root = struct.unpack_from("<HBH", sector, 14)
    total16, fat16 = (
        struct.unpack_from("<H", sector, 19)[0],
        struct.unpack_from("<H", sector, 22)[0],
    )
    total32, fat32 = struct.unpack_from("<II", sector, 32)
    data = (total16 or total32) - reserved - fats * (fat16 or fat32) - root * 32 // 512
    return per_cluster, int(fat16 == 0), data // per_cluster


def allocated(path: Path) -> int:
    return path.stat().st_blocks * 512


@pytest.mark.parametrize("size_mb", [0, -1, 1048577, 1.5, True, "32"])
def test_a_size_the_builder_does_not_make_is_refused(slot: SdCard, size_mb: int) -> None:
    with pytest.raises(ValueError) as refused:
        slot.image(size_mb=size_mb)
    assert str(refused.value) == "size_mb must be a whole number from 1 to 1048576 (MiB)"


@pytest.mark.parametrize("cluster_kb", [0, 3, 128, 32.0, True, "32"])
def test_a_cluster_mformat_does_not_make_is_refused(slot: SdCard, cluster_kb: int) -> None:
    with pytest.raises(ValueError) as refused:
        slot.image(size_mb=32768, cluster_kb=cluster_kb)
    assert str(refused.value) == "cluster_kb must be one of 1, 2, 4, 8, 16, 32, 64 (KiB)"


@pytest.mark.parametrize(
    ("size_mb", "cluster_kb", "clusters"),
    [(512, 64, 8190), (2048, 32, 65519), (1048576, 1, 1065418228)],
)
def test_a_fat32_card_with_a_count_of_clusters_it_cannot_have_is_refused(
    slot: SdCard, size_mb: int, cluster_kb: int, clusters: int
) -> None:
    with pytest.raises(ValueError) as refused:
        slot.image(size_mb=size_mb, cluster_kb=cluster_kb)
    assert str(refused.value) == (
        f"a FAT32 card of {size_mb} MiB with {cluster_kb} KiB clusters has {clusters} clusters;"
        " FAT32 takes 65525 to 268435445: choose another size_mb or cluster_kb"
    )


@pytest.mark.requires_mtools
@pytest.mark.parametrize("cluster_kb", [4, 32])
def test_a_32_gib_card_has_the_cluster_asked_for_and_stays_sparse(
    slot: SdCard, cluster_kb: int
) -> None:
    # 4 KiB as well: 32 KiB is what mformat takes by itself on this card, with -c or without.
    card = slot.image(size_mb=32768, cluster_kb=cluster_kb).with_text("CONFIG.TXT", "on\n").build()
    assert card.path.stat().st_size == 32768 << 20
    info = minfo(card)
    assert re.search(rf"^cluster size: {cluster_kb * 2} sectors$", info, re.M), info
    assert re.search(r"^heads: 255$", info, re.M), info
    assert re.search(r"^sectors per track: 63$", info, re.M), info
    per_cluster, fat32, clusters = boot_sector(card)
    assert (per_cluster, fat32) == (cluster_kb * 2, 1)
    assert clusters in FAT32_CLUSTERS
    # Its FATs and the file: the rest of the card is holes.
    assert allocated(card.path) <= 2 * (clusters + 2) * 4 + (1 << 20)
    assert card.read_text("CONFIG.TXT") == "on\n"
    slot.insert(card)


@pytest.mark.requires_mtools
def test_the_builder_makes_a_card_of_1_tib(slot: SdCard) -> None:
    card = slot.image(size_mb=1048576, cluster_kb=64).build()
    assert card.path.stat().st_size == 1 << 40
    assert boot_sector(card)[0] == 128
    assert allocated(card.path) < 256 << 20
    slot.insert(card)


@pytest.mark.requires_mtools
def test_below_512_mib_the_cluster_is_honoured_and_mformat_chooses_the_fat(slot: SdCard) -> None:
    fat16 = slot.image(size_mb=32, cluster_kb=4).build()
    assert boot_sector(fat16)[:2] == (8, 0)
    fat32 = slot.image(size_mb=100, cluster_kb=1).build()
    assert boot_sector(fat32)[:2] == (2, 1)
    assert boot_sector(fat32)[2] in FAT32_CLUSTERS


@pytest.mark.requires_mtools
@pytest.mark.parametrize("cluster_kb", [8, 16, 32, 64])
def test_the_smallest_card_a_cluster_is_allowed_has_the_clusters_fat32_asks_for(
    slot: SdCard, cluster_kb: int
) -> None:
    size_mb = 512
    while fat32_clusters(size_mb, cluster_kb) < FAT32_CLUSTERS[0]:
        size_mb += 1
    # What mformat makes of it agrees with the check, on either side.
    card = slot.image(size_mb=size_mb, cluster_kb=cluster_kb).build()
    assert boot_sector(card)[2] in FAT32_CLUSTERS
    below = card.path.with_name("below.img")
    with below.open("wb") as stream:
        stream.truncate((size_mb - 1) << 20)
    sectors, per_cluster = str((size_mb - 1) << 11), str(cluster_kb * 2)
    subprocess.run(
        ["mformat", "-i", str(below), "-F", "-T", sectors, "-c", per_cluster, "::"],
        check=True,
        env={**os.environ, "MTOOLS_SKIP_CHECK": "1"},
    )
    if size_mb > 512:
        assert boot_sector(below)[2] < FAT32_CLUSTERS[0]
    with pytest.raises(ValueError, match="FAT32 takes 65525"):
        slot.image(size_mb=size_mb - 1, cluster_kb=cluster_kb)


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


@pytest.mark.parametrize(
    "name", ["/ROOT", "../OUT", "A/../OUT", "A//B", "A/*", "C:X", "A\\B", "[AB].TXT", "A[", "A]"]
)
def test_paths_cannot_escape_or_expand_as_mtools_patterns(slot: SdCard, name: str) -> None:
    with pytest.raises(ValueError, match="invalid card file path"):
        slot.image().with_text(name, "content")


@pytest.mark.parametrize("name", ["a.txt", "A.TXT/child", "DIR"])
def test_fat_case_and_file_directory_collisions_are_rejected(slot: SdCard, name: str) -> None:
    builder = slot.image().with_bytes("A.TXT", b"").with_bytes("dir/file", b"")
    with pytest.raises(ValueError, match="conflicts"):
        builder.with_bytes(name, b"")


@pytest.mark.parametrize("size", [0, -1, 1048577, True])
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


@pytest.mark.requires_mtools
def test_bracket_patterns_never_read_other_files(slot: SdCard, tmp_path: Path) -> None:
    # Model a filename created outside this API (for example, by firmware).
    # mcopy creates the literal name, while mtype would expand it as a pattern.
    card = slot.image(size_mb=1).with_text("A.TXT", "alpha").with_text("B.TXT", "beta").build()
    source = tmp_path / "literal.txt"
    source.write_text("literal")
    subprocess.run(
        ["mcopy", "-i", str(card.path), str(source), "::[AB].TXT"],
        check=True,
        capture_output=True,
        env={**os.environ, "MTOOLS_SKIP_CHECK": "1"},
    )
    assert card.list_files() == ["A.TXT", "B.TXT", "[AB].TXT"]
    for pattern in ("[AB].TXT", "[A-Z].TXT", "[!C].TXT", "DIR/[AB].TXT"):
        for read in (card.read_text, card.read_bytes, card.list_files):
            with pytest.raises(ValueError, match="invalid card file path"):
                read(pattern)
    assert card.read_text("A.TXT") == "alpha"
    assert card.read_text("B.TXT") == "beta"


@pytest.mark.requires_mtools
def test_readers_distinguish_missing_files_and_directories(slot: SdCard) -> None:
    card = slot.image().with_text("DIR/FILE.TXT", "content").build()
    for read in (card.read_text, card.read_bytes):
        with pytest.raises(IsADirectoryError) as directory:
            read("dir")
        assert directory.value.filename == "dir"
        for name in ("MISSING.TXT", "dir/missing.txt", "missing/file.txt"):
            with pytest.raises(FileNotFoundError) as missing:
                read(name)
            assert missing.value.filename == name
        with pytest.raises(NotADirectoryError):
            read("dir/file.txt/child")
        # Errors do not affect subsequent valid, case-insensitive reads.
        assert card.read_text("dir/file.txt") == "content"
    with pytest.raises(NotADirectoryError) as file:
        card.list_files("dir/file.txt")
    assert file.value.filename == "dir/file.txt"
    with pytest.raises(NotADirectoryError):
        card.list_files("dir/file.txt/child")
    with pytest.raises(FileNotFoundError):
        card.list_files("dir/missing")
    assert card.list_files("dir") == ["FILE.TXT"]


@pytest.mark.requires_mtools
def test_invalid_fat_remains_a_tool_error(slot: SdCard) -> None:
    card = slot.image().build()
    # An unreadable filesystem is not equivalent to a missing requested file.
    with card.path.open("r+b") as stream:
        stream.write(b"\0" * 512)
    with pytest.raises(SourceError, match="mdir failed"):
        card.read_text("FILE.TXT")
