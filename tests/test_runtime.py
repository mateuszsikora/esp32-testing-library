"""Runtime integrity and selection, without release publication."""

import io
import json
import platform
import tarfile
from pathlib import Path

import pytest

from esp32tl import __version__, runtime
from esp32tl.backends import esp32sim


def test_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    with pytest.raises(runtime.RuntimeError, match="unsupported runtime platform"):
        runtime.target()


def test_manifest_version(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            dict(
                version="wrong", esp32sim=runtime.SIM_REV, target=runtime.target(), sha256="0" * 64
            )
        )
    )
    with pytest.raises(runtime.RuntimeError, match="incompatible"):
        runtime.manifest(path)


def test_checksum(tmp_path: Path) -> None:
    path = tmp_path / "file"
    path.write_bytes(b"bad")
    with pytest.raises(runtime.RuntimeError, match="checksum"):
        runtime.verify(path, runtime.ROM_SHA)


@pytest.mark.parametrize(
    "name,kind", [(runtime.ROM, tarfile.SYMTYPE), ("../escape", tarfile.REGTYPE)]
)
def test_archive_refuses_links_and_traversal(tmp_path: Path, name: str, kind: bytes) -> None:
    archive = tmp_path / "archive.tar"
    with tarfile.open(archive, "w") as tar:
        item = tarfile.TarInfo(name)
        item.type = kind
        item.linkname = "../escape"
        tar.addfile(item, io.BytesIO())
    with pytest.raises(runtime.RuntimeError, match="archive member"):
        runtime.extract_rom(archive, tmp_path / "rom")
    assert not (tmp_path / "rom").exists()


def test_download_failure_leaves_no_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "cache/runtime"
    monkeypatch.setattr(runtime, "cache", lambda: dest)
    monkeypatch.setattr(runtime, "RELEASE_URL", "http://127.0.0.1:1")
    with pytest.raises(runtime.RuntimeError, match="download failed"):
        runtime.prepare()
    assert not dest.exists()
    assert list(dest.parent.iterdir()) == []


def test_selection_explicit_checkout_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(esp32sim, "ROOT", tmp_path)
    monkeypatch.setattr(runtime, "identity", lambda path: None)
    cached = tmp_path / "cached"
    cached.mkdir()
    (cached / "esp32tl-runner").touch()
    monkeypatch.setattr(runtime, "cached_file", lambda kind: cached / "esp32tl-runner")
    assert esp32sim.runner_path() == cached / "esp32tl-runner"
    local = tmp_path / "runner/target/release/esp32tl-runner"
    local.parent.mkdir(parents=True)
    (tmp_path / "runner/Cargo.toml").touch()
    local.touch()
    assert esp32sim.runner_path() == local
    override = tmp_path / "override"
    override.touch()
    monkeypatch.setenv("ESP32TL_RUNNER", str(override))
    assert esp32sim.runner_path() == override
    override.unlink()
    with pytest.raises(esp32sim.BackendError, match="no runner"):
        esp32sim.runner_path()


def test_identity_rejects_old_runner(tmp_path: Path) -> None:
    runner = tmp_path / "runner"
    runner.write_text("#!/bin/sh\necho old\n")
    runner.chmod(0o755)
    with pytest.raises(runtime.RuntimeError, match="incompatible runner"):
        runtime.identity(runner)


def test_prepare_atomic_and_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dest = tmp_path / "cache/runtime"
    monkeypatch.setattr(runtime, "cache", lambda: dest)
    rom = b"fixture ROM"
    import hashlib

    monkeypatch.setattr(runtime, "ROM_SHA", hashlib.sha256(rom).hexdigest())
    binary = f"#!/bin/sh\necho '{__version__} {runtime.SIM_REV}'\n".encode()
    downloads: list[str] = []

    def download(url: str, path: Path) -> None:
        assert not dest.exists()
        downloads.append(url)
        if path.name == "manifest.json":
            path.write_text(
                json.dumps(
                    {
                        "version": __version__,
                        "esp32sim": runtime.SIM_REV,
                        "target": runtime.target(),
                        "sha256": hashlib.sha256(binary).hexdigest(),
                    }
                )
            )
        elif path.name == "esp32tl-runner":
            path.write_bytes(binary)
        else:
            with tarfile.open(path, "w") as archive:
                info = tarfile.TarInfo(runtime.ROM)
                info.size = len(rom)
                archive.addfile(info, io.BytesIO(rom))
            monkeypatch.setattr(runtime, "ROM_ARCHIVE_SHA", runtime.sha(path))

    monkeypatch.setattr(runtime, "download", download)
    assert runtime.prepare() == dest
    assert len(downloads) == 3
    assert runtime.prepare() == dest
    assert len(downloads) == 3
    (dest / runtime.ROM).write_bytes(b"corrupted")
    with pytest.raises(runtime.RuntimeError, match="checksum"):
        runtime.prepare()
    assert len(downloads) == 3


def test_rom_pins_match_developer_setup() -> None:
    script = (Path(__file__).resolve().parents[1] / "scripts/fetch-rom-elf").read_text()
    assert f"ROM_SHA256={runtime.ROM_SHA}" in script
    assert f"TARBALL_SHA256={runtime.ROM_ARCHIVE_SHA}" in script


@pytest.fixture
def cached_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "runtime"
    directory.mkdir()
    runner = directory / "esp32tl-runner"
    runner.write_text(f"#!/bin/sh\necho '{__version__} {runtime.SIM_REV}'\n")
    runner.chmod(0o755)
    rom = directory / runtime.ROM
    rom.write_bytes(b"test ROM")
    monkeypatch.setattr(runtime, "ROM_SHA", runtime.sha(rom))
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "version": __version__,
                "esp32sim": runtime.SIM_REV,
                "target": runtime.target(),
                "sha256": runtime.sha(runner),
            }
        )
    )
    monkeypatch.setattr(runtime, "cache", lambda: directory)
    monkeypatch.setattr(esp32sim, "ROOT", tmp_path / "site-packages")
    monkeypatch.delenv("ESP32TL_RUNNER", raising=False)
    monkeypatch.delenv("ESP32TL_ROM", raising=False)
    return directory


@pytest.mark.parametrize(
    "kind,damage",
    [
        ("runner", "missing"),
        ("runner", "corrupt"),
        ("runner", "not_executable"),
        ("rom", "missing"),
        ("rom", "corrupt"),
    ],
)
def test_override_does_not_validate_replaced_cache_file(
    cached_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    damage: str,
) -> None:
    replaced = cached_runtime / ("esp32tl-runner" if kind == "runner" else runtime.ROM)
    override = cached_runtime.parent / "override"
    override.write_bytes(replaced.read_bytes())
    override.chmod(0o755)
    monkeypatch.setenv(f"ESP32TL_{kind.upper()}", str(override))
    if damage == "missing":
        replaced.unlink()
    elif damage == "corrupt":
        replaced.write_bytes(b"corrupted")
    else:
        replaced.chmod(0o644)

    assert esp32sim.runner_path() == (
        override if kind == "runner" else cached_runtime / "esp32tl-runner"
    )
    assert esp32sim.rom_path() == (override if kind == "rom" else cached_runtime / runtime.ROM)
    # Explicit backend paths must not weaken prepare's full installation check.
    with pytest.raises(runtime.RuntimeError):
        runtime.prepare()


@pytest.mark.parametrize("kind", ["runner", "rom"])
def test_selected_cache_file_still_requires_checksum(cached_runtime: Path, kind: str) -> None:
    selected = cached_runtime / ("esp32tl-runner" if kind == "runner" else runtime.ROM)
    selected.write_bytes(b"corrupted")
    with pytest.raises(esp32sim.BackendError, match="checksum"):
        esp32sim.runtime_path(kind)


@pytest.mark.parametrize("kind", ["runner", "rom"])
def test_selected_cache_file_still_requires_manifest(cached_runtime: Path, kind: str) -> None:
    path = cached_runtime / "manifest.json"
    info = json.loads(path.read_text())
    info["version"] = "wrong"
    path.write_text(json.dumps(info))
    with pytest.raises(esp32sim.BackendError, match="incompatible version"):
        esp32sim.runtime_path(kind)
