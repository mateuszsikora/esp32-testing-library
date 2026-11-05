"""Hooks for this repository's test suite.

Tests that need a container carry the `requires_docker` marker and are skipped, with the
marker's name and what is missing as the reason, where Docker is absent: GitHub-hosted macOS
runners have none (plan M0). Tests that make or read a FAT image carry `requires_mtools` and are
skipped the same way where mtools is not installed.

`--shard=K/N` runs only the tests of shard K of N, which is how CI splits the suite into short jobs.
"""

import functools
import hashlib
import shutil
import subprocess

import pytest

pytest_plugins = ["pytester"]


@functools.cache
def docker_missing() -> str | None:
    """What keeps Docker from running a container here, or None when nothing does."""
    docker = shutil.which("docker")
    if docker is None:
        return "no docker on PATH"
    try:
        info = subprocess.run([docker, "info"], capture_output=True, timeout=30)
    except subprocess.TimeoutExpired:
        return "'docker info' did not answer in 30 s"
    if info.returncode != 0:
        return "the Docker daemon is not reachable ('docker info' failed)"
    return None


# The mtools commands the tests run.
MTOOLS = ("mformat", "mcopy", "mdir", "mtype", "mmd")


@functools.cache
def mtools_missing() -> str | None:
    """Which mtools commands are not on PATH, or None when none is missing."""
    missing = [tool for tool in MTOOLS if shutil.which(tool) is None]
    return f"no {', '.join(missing)} on PATH (mtools)" if missing else None


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--shard",
        metavar="K/N",
        help="run only the tests of shard K of N, counted from 1; a test's shard comes from its id",
    )


def parse_shard(value: str) -> tuple[int, int]:
    """K and N of a `--shard` value."""
    shard, _, count = value.partition("/")
    if not (shard.isdecimal() and count.isdecimal() and 1 <= int(shard) <= int(count)):
        raise pytest.UsageError(f"--shard must be K/N with 1 <= K <= N, not {value!r}")
    return int(shard), int(count)


def in_shard(item: pytest.Item, shard: int, count: int) -> bool:
    # SHA-256, not hash(): every job must put a test in the same shard.
    digest = hashlib.sha256(item.nodeid.encode()).digest()
    return int.from_bytes(digest, "big") % count == shard - 1


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    value = config.getoption("--shard")
    if value is not None:
        shard, count = parse_shard(value)
        selected: list[pytest.Item] = []
        deselected: list[pytest.Item] = []
        for item in items:
            (selected if in_shard(item, shard, count) else deselected).append(item)
        config.hook.pytest_deselected(items=deselected)
        items[:] = selected
    for marker, missing in (
        ("requires_docker", docker_missing),
        ("requires_mtools", mtools_missing),
    ):
        marked = [item for item in items if item.get_closest_marker(marker)]
        if not marked:
            continue
        reason = missing()
        if reason is not None:
            for item in marked:
                item.add_marker(pytest.mark.skip(reason=f"{marker}: {reason}"))
