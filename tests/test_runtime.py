"""Runtime integrity and selection, without release publication."""

import http.server
import io
import json
import platform
import tarfile
import threading
import tomllib
import urllib.error
from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

import pytest

from esp32tl import __version__, runtime
from esp32tl.backends import esp32sim

# The real lookup, before the fixture below replaces it.
UNRELEASED = runtime.unreleased


@pytest.fixture(autouse=True)
def installed_as_a_release(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests run from an editable install, which is not a release: they install as one."""
    monkeypatch.setattr(runtime, "unreleased", lambda: None)


class Installed:
    """The installed distribution, holding `direct_url` as its `direct_url.json` when set."""

    def __init__(self, direct_url: object = None) -> None:
        self.direct_url = direct_url

    def read_text(self, name: str) -> str | None:
        assert name == "direct_url.json"
        return None if self.direct_url is None else json.dumps(self.direct_url)


REPOSITORY = "https://github.com/mateuszsikora/esp32-testing-library"
COMMIT = "769f30a1d490cbf37cbb08800e8cb22df5e1c851"


GIT = runtime.Unreleased(f"commit {COMMIT} of {REPOSITORY}", "a checkout of that commit")
DIRECTORY = runtime.Unreleased("the directory file:///src/esp32tl", "that directory")


@pytest.mark.parametrize(
    "direct_url,source",
    [
        ({"url": REPOSITORY, "vcs_info": {"vcs": "git", "commit_id": COMMIT}}, GIT),
        (
            {
                "url": REPOSITORY,
                "vcs_info": {"vcs": "git", "commit_id": COMMIT, "requested_revision": "main"},
            },
            GIT,
        ),
        # The release's own tag is the release.
        (
            {
                "url": REPOSITORY,
                "vcs_info": {
                    "vcs": "git",
                    "commit_id": COMMIT,
                    "requested_revision": f"v{__version__}",
                },
            },
            None,
        ),
        ({"url": "file:///src/esp32tl", "dir_info": {}}, DIRECTORY),
        ({"url": "file:///src/esp32tl", "dir_info": {"editable": True}}, DIRECTORY),
        ({"url": "file:///dl/esp32tl-0.1.0-py3-none-any.whl", "archive_info": {}}, None),
        (None, None),
    ],
)
def test_unreleased_reads_where_pip_installed_from(
    monkeypatch: pytest.MonkeyPatch, direct_url: object, source: runtime.Unreleased | None
) -> None:
    monkeypatch.setattr("importlib.metadata.distribution", lambda name: Installed(direct_url))
    assert UNRELEASED() == source


def test_unreleased_install_gets_no_release_runner(
    cached_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "unreleased", lambda: GIT)
    monkeypatch.setattr(runtime, "download", lambda url, path: pytest.fail(f"downloaded {url}"))
    message = (
        f"unreleased esp32tl: installed from commit {COMMIT} of {REPOSITORY}, not from release "
        f"v{__version__}, whose runner need not match it; run make build-runner rom-elf in a "
        "checkout of that commit, then set ESP32TL_RUNNER to its "
        "runner/target/release/esp32tl-runner, and ESP32TL_ROM, ESP32TL_ROM_ESP32S3, "
        "ESP32TL_ROM_ESP32C3, ESP32TL_ROM_ESP32C6 to the ROM ELFs in its "
        ".cache/esp-rom-elfs/20241011/"
    )
    with pytest.raises(runtime.RuntimeError) as refused:
        runtime.prepare()
    assert str(refused.value) == message
    with pytest.raises(esp32sim.BackendError) as refused_backend:
        esp32sim.runner_path()
    assert str(refused_backend.value) == message
    # The prepared cache's ROM still serves while its manifest matches this esp32tl.
    assert esp32sim.rom_path() == cached_runtime / runtime.ROM
    override = cached_runtime.parent / "built-runner"
    override.write_bytes((cached_runtime / "esp32tl-runner").read_bytes())
    override.chmod(0o755)
    monkeypatch.setenv("ESP32TL_RUNNER", str(override))
    assert esp32sim.runner_path() == override


def test_unreleased_install_downloads_nothing_without_a_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "absent"
    monkeypatch.setattr(runtime, "cache", lambda: dest)
    monkeypatch.setattr(runtime, "unreleased", lambda: DIRECTORY)
    monkeypatch.setattr(runtime, "download", lambda url, path: pytest.fail(f"downloaded {url}"))
    with pytest.raises(runtime.RuntimeError, match=r"^unreleased esp32tl: .* in that directory, "):
        runtime.prepare()
    assert not dest.exists()
    assert not dest.parent.exists() or list(dest.parent.iterdir()) == []


def test_unreleased_install_names_the_rom_variable_not_prepare(
    cached_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "unreleased", lambda: GIT)
    remedy = (
        "set ESP32TL_ROM_ESP32S3 to the esp32s3_rev0_rom.elf make rom-elf puts in a checkout's "
        ".cache/esp-rom-elfs/20241011/: esp32tl runtime prepare installs nothing for an "
        "unreleased esp32tl"
    )
    # A release before the ESP32-S3 left no ROM of it in the cache.
    (cached_runtime / runtime.ROMS["esp32s3"][0]).unlink()
    with pytest.raises(esp32sim.BackendError) as missing:
        esp32sim.rom_path("esp32s3")
    assert str(missing.value).endswith(f"esp32s3_rev0_rom.elf; {remedy}")
    # A commit with another esp32sim pin rejects the cache's manifest.
    path = cached_runtime / "manifest.json"
    info = json.loads(path.read_text())
    info["esp32sim"] = "0" * 40
    path.write_text(json.dumps(info))
    with pytest.raises(esp32sim.BackendError) as manifest:
        esp32sim.rom_path("esp32s3")
    assert "incompatible version, esp32sim revision or target" in str(manifest.value)
    assert str(manifest.value).endswith(f"; {remedy}")
    path.unlink()
    path.parent.joinpath("esp32tl-runner").unlink()
    for name, _ in runtime.ROMS.values():
        path.parent.joinpath(name).unlink(missing_ok=True)
    path.parent.rmdir()
    with pytest.raises(esp32sim.BackendError) as absent:
        esp32sim.rom_path("esp32s3")
    assert str(absent.value) == f"runtime missing: {remedy}"


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
    monkeypatch.setattr(runtime, "cached_file", lambda kind, chip: cached / "esp32tl-runner")
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


def test_a_checkout_s_missing_file_names_its_make_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(esp32sim, "ROOT", tmp_path)
    (tmp_path / "runner").mkdir()
    (tmp_path / "runner/Cargo.toml").touch()
    for chip in runtime.ROMS:
        monkeypatch.delenv(runtime.rom_variable(chip), raising=False)
    monkeypatch.delenv("ESP32TL_RUNNER", raising=False)
    runner = tmp_path / "runner/target/release/esp32tl-runner"
    with pytest.raises(esp32sim.BackendError) as missing:
        esp32sim.runner_path()
    assert str(missing.value) == f"no runner at {runner}: run make build-runner in {tmp_path}"
    rom = tmp_path / ".cache/esp-rom-elfs/20241011/esp32s3_rev0_rom.elf"
    with pytest.raises(esp32sim.BackendError) as missing:
        esp32sim.rom_path("esp32s3")
    assert str(missing.value) == f"no rom at {rom}: run make rom-elf in {tmp_path}"


def test_identity_rejects_old_runner(tmp_path: Path) -> None:
    runner = tmp_path / "runner"
    runner.write_text("#!/bin/sh\necho old\n")
    runner.chmod(0o755)
    with pytest.raises(runtime.RuntimeError, match="incompatible runner"):
        runtime.identity(runner)


def test_prepare_atomic_and_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dest = tmp_path / "cache/runtime"
    monkeypatch.setattr(runtime, "cache", lambda: dest)
    import hashlib

    roms = {chip: f"{chip} fixture ROM".encode() for chip in runtime.ROMS}
    monkeypatch.setattr(
        runtime,
        "ROMS",
        {
            chip: (name, hashlib.sha256(roms[chip]).hexdigest())
            for chip, (name, _) in runtime.ROMS.items()
        },
    )
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
                for chip, (name, _) in runtime.ROMS.items():
                    info = tarfile.TarInfo(name)
                    info.size = len(roms[chip])
                    archive.addfile(info, io.BytesIO(roms[chip]))
            monkeypatch.setattr(runtime, "ROM_ARCHIVE_SHA", runtime.sha(path))

    monkeypatch.setattr(runtime, "download", download)
    assert runtime.prepare() == dest
    assert len(downloads) == 3
    for chip, (name, _) in runtime.ROMS.items():
        assert (dest / name).read_bytes() == roms[chip]
    assert runtime.prepare() == dest
    assert len(downloads) == 3
    (dest / runtime.ROMS["esp32s3"][0]).write_bytes(b"corrupted")
    with pytest.raises(runtime.RuntimeError, match="checksum"):
        runtime.prepare()
    assert len(downloads) == 3


def test_rom_pins_match_developer_setup() -> None:
    script = (Path(__file__).resolve().parents[1] / "scripts/fetch-rom-elf").read_text()
    pinned = script.split('ROMS="', 1)[1].split('"', 1)[0].splitlines()
    assert pinned == [f"{name} {digest}" for name, digest in runtime.ROMS.values()]
    assert runtime.ROMS["esp32"] == (runtime.ROM, runtime.ROM_SHA)
    assert f"TARBALL_SHA256={runtime.ROM_ARCHIVE_SHA}" in script


@pytest.fixture
def cached_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "runtime"
    directory.mkdir()
    runner = directory / "esp32tl-runner"
    runner.write_text(f"#!/bin/sh\necho '{__version__} {runtime.SIM_REV}'\n")
    runner.chmod(0o755)
    roms = {}
    for chip, (name, _) in runtime.ROMS.items():
        rom = directory / name
        rom.write_bytes(f"{chip} test ROM".encode())
        roms[chip] = (name, runtime.sha(rom))
    monkeypatch.setattr(runtime, "ROMS", roms)
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
    for chip in runtime.ROMS:
        monkeypatch.delenv(runtime.rom_variable(chip), raising=False)
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


def test_runtime_revision_matches_runner_pins() -> None:
    runner = Path(__file__).resolve().parents[1] / "runner"
    cargo = tomllib.loads((runner / "Cargo.toml").read_text())
    dependencies = [
        dep
        for dep in cargo["dependencies"].values()
        if isinstance(dep, dict) and dep.get("git") == "https://github.com/mikroverk/esp32sim"
    ]
    assert dependencies
    assert {dep["rev"] for dep in dependencies} == {runtime.SIM_REV}
    lock = tomllib.loads((runner / "Cargo.lock").read_text())
    sources = {
        package["source"]
        for package in lock["package"]
        if package.get("source", "").startswith("git+https://github.com/mikroverk/esp32sim?")
    }
    assert sources == {
        f"git+https://github.com/mikroverk/esp32sim?rev={runtime.SIM_REV}#{runtime.SIM_REV}"
    }


def test_each_chip_has_its_rom_in_the_cache_and_its_own_override(
    cached_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for chip, (name, _) in runtime.ROMS.items():
        assert esp32sim.rom_path(chip) == cached_runtime / name
    override = cached_runtime.parent / "s3.elf"
    override.write_bytes((cached_runtime / runtime.ROMS["esp32s3"][0]).read_bytes())
    monkeypatch.setenv("ESP32TL_ROM_ESP32S3", str(override))
    assert esp32sim.rom_path("esp32s3") == override
    assert esp32sim.rom_path() == cached_runtime / runtime.ROM
    with pytest.raises(esp32sim.BackendError, match="esp32tl has no ROM for esp32p4"):
        esp32sim.rom_path("esp32p4")


class Release(http.server.BaseHTTPRequestHandler):
    """A release server: the status of each path from `statuses`, 404 for others."""

    statuses: ClassVar[dict[str, int]] = {}

    def do_GET(self) -> None:
        status = self.statuses.get(self.path.rsplit("/", 1)[-1], 404)
        self.send_response(status)
        body = b"{}" if status == 200 else b""
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[type[Release]]:
    """`Release` serving on 127.0.0.1 as RELEASE_URL, the cache in `tmp_path`."""
    handler = type("Handler", (Release,), {"statuses": {}})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(runtime, "cache", lambda: tmp_path / "cache/runtime")
    monkeypatch.setattr(runtime, "RELEASE_URL", f"http://127.0.0.1:{server.server_port}/v1")
    # urlopen honours a proxy from the environment: the local server must be reached directly.
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.setenv(name, "127.0.0.1")
    yield handler
    server.shutdown()
    server.server_close()


def test_a_missing_manifest_names_possible_causes_and_alternatives(
    release: type[Release],
) -> None:
    name = f"esp32tl-runner-{runtime.target()}"
    with pytest.raises(runtime.RuntimeError) as error:
        runtime.prepare()
    assert str(error.value) == (
        f"download failed: {runtime.RELEASE_URL}/{name}.json: HTTP Error 404: Not Found;"
        f" possible causes: release v{__version__} is not published (yet), or it has no"
        f" runtime for {runtime.target()}; alternatives: set ESP32TL_RUNNER and ESP32TL_ROM,"
        " or install esp32tl from a source checkout and run make setup there"
    )
    assert isinstance(error.value.__cause__, urllib.error.HTTPError)


@pytest.mark.parametrize(
    ("status", "message"),
    [(500, "HTTP Error 500: Internal Server Error"), (403, "HTTP Error 403: Forbidden")],
)
def test_another_http_error_for_the_manifest_keeps_its_message(
    release: type[Release], status: int, message: str
) -> None:
    name = f"esp32tl-runner-{runtime.target()}"
    release.statuses = {f"{name}.json": status}
    with pytest.raises(runtime.RuntimeError) as error:
        runtime.prepare()
    assert str(error.value) == f"download failed: {runtime.RELEASE_URL}/{name}.json: {message}"


def test_a_missing_runner_beside_its_manifest_keeps_its_message(
    release: type[Release], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The manifest is there, so the release is: the hint is for the manifest alone.
    name = f"esp32tl-runner-{runtime.target()}"
    manifest = {"version": __version__, "esp32sim": runtime.SIM_REV}
    manifest |= {"target": runtime.target(), "sha256": "0" * 64}
    original = runtime.download

    def download(url: str, path: Path) -> None:
        if url.endswith(".json"):
            path.write_text(json.dumps(manifest))
        else:
            original(url, path)

    monkeypatch.setattr(runtime, "download", download)
    with pytest.raises(runtime.RuntimeError) as error:
        runtime.prepare()
    assert str(error.value) == (
        f"download failed: {runtime.RELEASE_URL}/{name}: HTTP Error 404: Not Found"
    )
