"""The `card_cache` fixture's cards without the emulator: clones and what reaches them, the key,
the limit and eviction, atomic writes. Through `pytester`: its session scope and its option. Its
run on the real firmware is in `tests/test_power.py`.
"""

import hashlib
import os
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from esp32tl.boards import SdCardRoute
from esp32tl.interfaces import SourceError
from esp32tl.interfaces import card_cache as module
from esp32tl.interfaces.board import Board
from esp32tl.interfaces.card_cache import CARD_CACHE_MIB, CardCache, allocated, clone
from esp32tl.interfaces.sd_card import SdCard

ROOT = Path(__file__).resolve().parent.parent
PERSISTENCE = ROOT / "firmware" / "persistence"

MIB = 1 << 20


@pytest.fixture(params=["file system's clone", "sparse copy"])
def how(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Every test of what a clone is runs on both ways of making one."""
    if request.param == "sparse copy":
        monkeypatch.setattr(module, "_share_blocks", lambda source, target: False)
    return str(request.param)


@pytest.fixture
def slot(tmp_path: Path) -> Iterator[SdCard]:
    card = SdCard(SdCardRoute(sclk=18, mosi=23, miso=19, cs=5), directory=tmp_path)
    try:
        yield card
    finally:
        card.close()


@pytest.fixture
def running(slot: SdCard) -> SimpleNamespace:
    return SimpleNamespace(app_sha256="a" * 64, sd_card=slot, powered=False)


def cache_of(tmp_path: Path, running: SimpleNamespace, limit: int = 64 * MIB) -> CardCache:
    directory = tmp_path / "cache"
    directory.mkdir(exist_ok=True)
    return CardCache(directory, limit, lambda: cast(Board, running))


def image(path: Path, data: bytes = b"first", *, size: int = MIB, at: int = 4096) -> Path:
    """A card of `size`, sparse but for `data`."""
    with path.open("wb") as stream:
        stream.truncate(size)
        stream.seek(at)
        stream.write(data)
    return path


def contents(path: os.PathLike[str] | str) -> bytes:
    return Path(path).read_bytes()


def kept(cache: CardCache) -> list[Path]:
    return sorted(cache.directory.glob("*.img"))


def disk_use(cache: CardCache) -> int:
    return sum(allocated(path) for path in cache.directory.iterdir())


def test_a_card_never_kept_is_a_miss(tmp_path: Path, running: SimpleNamespace) -> None:
    assert cache_of(tmp_path, running).get("first-mount") is None


def test_a_hit_is_a_clone_in_the_test_s_directory_gone_with_its_board(
    tmp_path: Path, running: SimpleNamespace, slot: SdCard, how: str
) -> None:
    cache = cache_of(tmp_path, running)
    source = image(tmp_path / "card.img")
    cache.put("first-mount", source)
    card = cache.get("first-mount")
    assert card is not None
    assert contents(card) == contents(source)
    assert card.path.parent.parent.name.startswith("esp32tl-sd-")
    assert card.path.stat().st_size == MIB
    assert slot.insert(card) == card.path
    slot.close()
    assert not card.path.exists()
    assert kept(cache), "the cache's own card stays"
    with pytest.raises(SourceError, match="board is closed"):
        os.fspath(card)


def test_what_is_written_reaches_neither_the_cache_nor_another_clone(
    tmp_path: Path, running: SimpleNamespace, how: str
) -> None:
    cache = cache_of(tmp_path, running)
    source = image(tmp_path / "card.img")
    original = contents(source)
    cache.put("first-mount", source)
    first = cache.get("first-mount")
    second = cache.get("first-mount")
    assert first is not None and second is not None
    assert first.path != second.path

    with first.path.open("r+b") as stream:
        stream.seek(4096)
        stream.write(b"later")
    assert contents(second) == original
    fresh = cache.get("first-mount")
    assert fresh is not None and contents(fresh) == original
    # nor from the image it was kept from, nor back to it
    assert contents(source) == original
    image(source, b"after")
    newer = cache.get("first-mount")
    assert newer is not None and contents(newer) == original


def test_a_clone_of_a_read_only_image_can_be_written(
    tmp_path: Path, running: SimpleNamespace, slot: SdCard, how: str
) -> None:
    cache = cache_of(tmp_path, running)
    source = image(tmp_path / "card.img")
    source.chmod(0o444)
    cache.put("first-mount", source)
    card = cache.get("first-mount")
    assert card is not None
    slot.insert(card)


def test_a_card_belongs_to_its_name_and_its_application(
    tmp_path: Path, running: SimpleNamespace
) -> None:
    cache = cache_of(tmp_path, running)
    cache.put("first-mount", image(tmp_path / "card.img"))
    assert cache.get("another") is None
    running.app_sha256 = "b" * 64
    assert cache.get("first-mount") is None
    cache.put("first-mount", image(tmp_path / "other.img", b"other"))
    assert len(kept(cache)) == 2
    running.app_sha256 = "a" * 64
    card = cache.get("first-mount")
    assert card is not None and b"first" in contents(card)


def test_a_card_kept_again_replaces_the_one_before(
    tmp_path: Path, running: SimpleNamespace
) -> None:
    cache = cache_of(tmp_path, running)
    cache.put("first-mount", image(tmp_path / "card.img"))
    cache.put("first-mount", image(tmp_path / "newer.img", b"newer"))
    assert len(kept(cache)) == 1
    card = cache.get("first-mount")
    assert card is not None and b"newer" in contents(card)


def test_the_card_least_recently_used_goes_first(tmp_path: Path, running: SimpleNamespace) -> None:
    # Large enough for the volume to keep it sparse: APFS fills in a small file's holes.
    source = image(tmp_path / "card.img", bytes([1]) * MIB, size=1 << 30)
    each = allocated(source)
    # Room for three cards, and less than a fourth.
    cache = cache_of(tmp_path, running, limit=3 * each + each // 2)
    for name in ("a", "b", "c"):
        cache.put(name, source)
    assert cache.get("a") is not None, "a is the card used last now"
    cache.put("d", source)
    assert cache.get("b") is None
    assert all(cache.get(name) is not None for name in ("a", "c", "d"))


def test_disk_use_stays_within_the_limit_from_what_is_allocated(
    tmp_path: Path, running: SimpleNamespace
) -> None:
    limit = 8 * MIB
    cache = cache_of(tmp_path, running, limit=limit)
    # Each card reads as 1 GiB: it is what they have allocated that counts.
    for number in range(16):
        source = image(tmp_path / f"{number}.img", bytes([number + 1]) * MIB, size=1 << 30)
        cache.put(f"card {number}", source)
        assert disk_use(cache) <= limit
    assert 1 < len(kept(cache)) < 16


def test_a_card_above_the_limit_is_refused_and_evicts_nothing(
    tmp_path: Path, running: SimpleNamespace
) -> None:
    source = image(tmp_path / "card.img", bytes([1]) * (3 * MIB), size=1 << 30)
    needed = allocated(source)
    cache = cache_of(tmp_path, running, limit=MIB * 2)
    cache.put("small", image(tmp_path / "small.img"))
    with pytest.raises(SourceError) as refused:
        cache.put("first-mount", source)
    assert str(refused.value) == (
        f"card_cache: 'first-mount' needs {needed} bytes on disk; the limit is {2 * MIB}:"
        " raise esp32tl_card_cache_mib"
    )
    assert cache.get("first-mount") is None
    assert cache.get("small") is not None


def test_a_put_that_fails_leaves_no_part_of_a_card(
    tmp_path: Path, running: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = cache_of(tmp_path, running)
    source = image(tmp_path / "card.img")

    def fails_half_way(from_path: Path, to_path: Path) -> None:
        to_path.write_bytes(b"half")
        raise OSError(28, "No space left on device")

    with monkeypatch.context() as patch:
        patch.setattr(module, "clone", fails_half_way)
        with pytest.raises(SourceError, match=r"'first-mount': .*No space left on device"):
            cache.put("first-mount", source)
    assert cache.get("first-mount") is None
    assert [path.name for path in cache.directory.iterdir()] == ["lock"]


def test_a_cache_directory_that_fails_fails_with_the_card_s_name(
    tmp_path: Path, running: SimpleNamespace
) -> None:
    cache = CardCache(tmp_path / "gone", MIB, lambda: cast(Board, running))
    with pytest.raises(SourceError, match=r"^card_cache: 'first-mount': .*No such file"):
        cache.get("first-mount")
    with pytest.raises(SourceError, match=r"^card_cache: 'first-mount': .*No such file"):
        cache.put("first-mount", image(tmp_path / "card.img"))


def test_a_card_appears_whole_under_its_name_and_a_dead_run_s_part_is_removed(
    tmp_path: Path, running: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = cache_of(tmp_path, running)
    source = image(tmp_path / "card.img")
    stale = cache.directory / "from-a-run-that-died.tmp"
    stale.write_bytes(bytes(MIB))
    seen: list[tuple[list[Path], bytes]] = []
    replace = os.replace

    def replacing(temporary: Path, entry: Path) -> None:
        seen.append((kept(cache), contents(temporary)))
        replace(temporary, entry)

    monkeypatch.setattr(os, "replace", replacing)
    cache.put("first-mount", source)
    assert seen == [([], contents(source))]
    assert not stale.exists()
    assert [path.suffix for path in cache.directory.glob("*.*")] == [".img"]


def test_a_card_in_the_slot_of_a_board_that_is_on_is_not_kept(
    tmp_path: Path, running: SimpleNamespace, slot: SdCard
) -> None:
    cache = cache_of(tmp_path, running)
    source = image(tmp_path / "card.img")
    slot.insert(source)
    running.powered = True
    with pytest.raises(
        SourceError, match=r"is in the slot of a board that is on: call board.power"
    ):
        cache.put("first-mount", source)
    assert cache.get("first-mount") is None
    # another image is no matter of the firmware's
    cache.put("another", image(tmp_path / "another.img"))
    running.powered = False
    cache.put("first-mount", source)
    assert cache.get("first-mount") is not None


def test_what_is_kept_is_checked(tmp_path: Path, running: SimpleNamespace) -> None:
    cache = cache_of(tmp_path, running)
    with pytest.raises(SourceError, match=r"card_cache: 'x': no image at .*missing.img"):
        cache.put("x", "missing.img")
    with pytest.raises(SourceError, match=r"card_cache: 'x': no image at"):
        cache.put("x", tmp_path)
    for name in ("", None, 3):
        with pytest.raises(ValueError, match="a card's name is a non-empty string"):
            cache.get(name)  # type: ignore[arg-type]
    running.app_sha256 = None
    with pytest.raises(SourceError, match="the build names no app image"):
        cache.get("x")


def test_without_a_board_there_is_no_application_to_keep_a_card_for(tmp_path: Path) -> None:
    cache = CardCache(tmp_path, MIB, lambda: None)
    with pytest.raises(SourceError, match="no board is running: use it in a test that uses board"):
        cache.get("first-mount")


def test_the_sparse_copy_leaves_holes_and_reads_the_same(tmp_path: Path) -> None:
    source = tmp_path / "source.img"
    size = 64 * MIB
    with source.open("wb") as stream:
        stream.truncate(size)
        stream.seek(MIB)
        stream.write(b"\xa5" * 100)
        stream.seek(8 * MIB)
        stream.write(bytes(4 * MIB))  # written, as zeros
        stream.seek(32 * MIB - 1)
        stream.write(b"\x5a" * 2)
    target = tmp_path / "target.img"
    module._sparse_copy(source, target)
    assert target.stat().st_size == size
    digest = [hashlib.sha256(contents(path)).hexdigest() for path in (source, target)]
    assert digest[0] == digest[1]
    assert allocated(target) < 2 * MIB
    assert allocated(target) < allocated(source)


def test_a_clone_of_a_card_that_ends_in_a_hole_keeps_its_size(tmp_path: Path, how: str) -> None:
    source = tmp_path / "source.img"
    with source.open("wb") as stream:
        stream.truncate(2 * MIB)
    target = tmp_path / "target.img"
    clone(source, target)
    assert target.stat().st_size == 2 * MIB
    assert contents(target) == bytes(2 * MIB)


# The fixture, through pytest.


def suite(pytester: pytest.Pytester, body: str) -> None:
    if not (PERSISTENCE / "build" / "esp32tl-built").is_file():
        pytest.fail(
            f"{PERSISTENCE}/build holds no finished build: run make firmware", pytrace=False
        )
    pytester.makepyfile(
        test_cards=f"""
import pytest

pytestmark = pytest.mark.app_path({str(PERSISTENCE)!r})

def image(path, size=1 << 20):
    with open(path, "wb") as stream:
        stream.truncate(size)
        stream.write(b"kept")
    return path
{body}
"""
    )


KEEPS_A_CARD = """
def test_one(board, card_cache, tmp_path):
    assert card_cache.get("card") is None
    card_cache.put("card", image(tmp_path / "card.img"))

def test_two(board, card_cache):
    assert card_cache.get("card").path.read_bytes().startswith(b"kept")
"""


def test_a_session_keeps_a_card_for_the_next_test_and_the_next_session(
    pytester: pytest.Pytester,
) -> None:
    suite(pytester, KEEPS_A_CARD)
    result = pytester.runpytest_subprocess("-rA")
    result.assert_outcomes(passed=2)
    assert list((pytester.path / ".pytest_cache/d/esp32tl-card-cache").glob("*.img"))
    # test_one's miss fails now: the card is there from the session before.
    pytester.runpytest_subprocess().assert_outcomes(passed=1, failed=1)
    # as pytest's own cache, which --cache-clear empties
    pytester.runpytest_subprocess("--cache-clear").assert_outcomes(passed=2)


def test_without_pytest_s_cache_a_card_lasts_for_the_session(pytester: pytest.Pytester) -> None:
    suite(pytester, KEEPS_A_CARD)
    run = ("-p", "no:cacheprovider")
    pytester.runpytest_subprocess(*run).assert_outcomes(passed=2)
    pytester.runpytest_subprocess(*run).assert_outcomes(passed=2)


def test_the_limit_is_an_ini_option_of_4096_mib_by_default(pytester: pytest.Pytester) -> None:
    assert CARD_CACHE_MIB == 4096
    suite(
        pytester,
        """
def test_limit(card_cache, pytestconfig):
    assert card_cache.limit == EXPECTED << 20
""",
    )
    test = pytester.path / "test_cards.py"
    test.write_text(test.read_text().replace("EXPECTED", "4096"))
    pytester.runpytest_subprocess().assert_outcomes(passed=1)

    test.write_text(test.read_text().replace("4096", "7"))
    pytester.makeini("[pytest]\nesp32tl_card_cache_mib = 7\n")
    pytester.runpytest_subprocess().assert_outcomes(passed=1)


def test_the_limit_above_refuses_what_a_test_keeps(pytester: pytest.Pytester) -> None:
    suite(
        pytester,
        """
def test_keep(board, card_cache, tmp_path):
    path = tmp_path / "card.img"
    with open(path, "wb") as stream:
        stream.write(bytes([1]) * (2 << 20))
    card_cache.put("first-mount", path)
""",
    )
    pytester.makeini("[pytest]\nesp32tl_card_cache_mib = 1\n")
    result = pytester.runpytest_subprocess()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "*SourceError: card_cache: 'first-mount' needs * bytes on disk; the limit is 1048576:"
            " raise esp32tl_card_cache_mib"
        ]
    )


@pytest.mark.parametrize("value", ["none", "0", "-1", "1.5"])
def test_a_limit_that_is_no_whole_number_is_a_usage_error(
    pytester: pytest.Pytester, value: str
) -> None:
    pytester.makeini(f"[pytest]\nesp32tl_card_cache_mib = {value}\n")
    pytester.makepyfile("def test_nothing(): pass")
    result = pytester.runpytest_subprocess()
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(
        [f"*esp32tl_card_cache_mib must be a whole number of MiB, not {value!r}"]
    )
