"""Hooks for this repository's test suite.

Tests that need a container carry the `requires_docker` marker and are skipped, with the
marker's name and what is missing as the reason, where Docker is absent: GitHub-hosted macOS
runners have none (plan M0). Tests that make or read a FAT image carry `requires_mtools` and are
skipped the same way where mtools is not installed.
"""

import functools
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
MTOOLS = ("mformat", "mcopy", "mdir", "mtype")


@functools.cache
def mtools_missing() -> str | None:
    """Which mtools commands are not on PATH, or None when none is missing."""
    missing = [tool for tool in MTOOLS if shutil.which(tool) is None]
    return f"no {', '.join(missing)} on PATH (mtools)" if missing else None


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
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
