"""Release assembly refuses missing, mixed or corrupted native assets."""

import hashlib
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def release_files(tmp_path: Path) -> Path:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    revision = tomllib.loads((ROOT / "runner/Cargo.toml").read_text())["dependencies"]["esp32"][
        "rev"
    ]
    for target in ("linux-x86_64", "macos-arm64"):
        name = f"esp32tl-runner-{target}"
        (tmp_path / name).write_bytes(target.encode())
        (tmp_path / (name + ".json")).write_text(
            json.dumps(
                {
                    "version": version,
                    "esp32sim": revision,
                    "target": target,
                    "sha256": hashlib.sha256(target.encode()).hexdigest(),
                }
            )
        )
    (tmp_path / f"esp32tl-{version}-py3-none-any.whl").write_bytes(b"wheel")
    (tmp_path / f"esp32tl-{version}.tar.gz").write_bytes(b"sdist")
    return tmp_path


def verify(directory: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts/check-release.py"), str(directory)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_release_checksums_cover_exactly_the_assets(release_files: Path) -> None:
    result = verify(release_files)
    assert result.returncode == 0, result.stderr
    lines = (release_files / "SHA256SUMS").read_text().splitlines()
    assert len(lines) == 6
    for line in lines:
        digest, name = line.split("  ")
        assert digest == hashlib.sha256((release_files / name).read_bytes()).hexdigest()


@pytest.mark.parametrize("problem", ["missing", "corrupted", "version", "extra"])
def test_bad_release_has_no_checksums(release_files: Path, problem: str) -> None:
    runner = release_files / "esp32tl-runner-linux-x86_64"
    if problem == "missing":
        runner.unlink()
    elif problem == "corrupted":
        runner.write_bytes(b"bad")
    elif problem == "version":
        path = runner.with_suffix(".json")
        value = json.loads(path.read_text())
        value["version"] = "wrong"
        path.write_text(json.dumps(value))
    else:
        (release_files / "esp32_rev300_rom.elf").write_bytes(b"must not redistribute")
    result = verify(release_files)
    assert result.returncode != 0
    assert "release verification failed" in result.stderr
    assert not (release_files / "SHA256SUMS").exists()


def published_check(directory: Path, tag: str | None = None) -> subprocess.CompletedProcess[str]:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/check-release.py"),
            str(directory),
            "--verify",
            "--tag",
            tag if tag is not None else f"v{version}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_published_release_verification_preserves_sums(release_files: Path) -> None:
    assert verify(release_files).returncode == 0
    before = (release_files / "SHA256SUMS").read_bytes()
    result = published_check(release_files)
    assert result.returncode == 0, result.stderr
    assert (release_files / "SHA256SUMS").read_bytes() == before


@pytest.mark.parametrize("problem", ["wheel", "sdist", "sums", "missing_sums", "tag"])
def test_publication_refuses_changed_assets(release_files: Path, problem: str) -> None:
    assert verify(release_files).returncode == 0
    sums = release_files / "SHA256SUMS"
    if problem == "wheel":
        next(release_files.glob("*.whl")).write_bytes(b"changed")
    elif problem == "sdist":
        next(release_files.glob("*.tar.gz")).write_bytes(b"changed")
    elif problem == "sums":
        sums.write_text("0" * 64 + "  unexpected.whl\n")
    elif problem == "missing_sums":
        sums.unlink()
    before = sums.read_bytes() if sums.exists() else None
    result = published_check(release_files, "vwrong" if problem == "tag" else None)
    assert result.returncode != 0
    assert "release verification failed" in result.stderr
    assert (sums.read_bytes() if sums.exists() else None) == before
