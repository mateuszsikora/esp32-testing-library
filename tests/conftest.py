"""Hooks for this repository's test suite.

Tests that need a container carry the `requires_docker` marker and are skipped, with the
marker's name and what is missing as the reason, where Docker is absent: GitHub-hosted macOS
runners have none (plan M0).
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


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    marked = [item for item in items if item.get_closest_marker("requires_docker")]
    if not marked:
        return
    missing = docker_missing()
    if missing is not None:
        for item in marked:
            item.add_marker(pytest.mark.skip(reason=f"requires_docker: {missing}"))
