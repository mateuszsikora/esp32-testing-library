"""Explicit, versioned runtime installation; importing this module never uses the network."""

import hashlib
import json
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.request
from importlib import metadata
from pathlib import Path
from typing import NamedTuple

from esp32tl import __version__

SIM_REV = "357b0e542dfe97d4c62ec4a15c01c4969164448c"
ROM = "esp32_rev300_rom.elf"
ROM_SHA = "920b70635440517866aab2230964a570d2cf2b676658d93c52fbac108c1cca31"
# Every chip's mask ROM ELF in release 20241011, the one the runner boots, and its SHA-256.
ROMS = {
    "esp32": (ROM, ROM_SHA),
    "esp32s3": (
        "esp32s3_rev0_rom.elf",
        "c0ce0f338d1de1bdc6efbef1591779a2a42c1ab7d759d3c6ae8ae63a7dd34cfd",
    ),
    "esp32c3": (
        "esp32c3_rev3_rom.elf",
        "19ac22e08707df926fb0cf4c54795d4067b983fae8635f396ded173a6d78fc3c",
    ),
    "esp32c6": (
        "esp32c6_rev0_rom.elf",
        "788e1d38724aeb8fd974fa10c4a7b089c02627d35342ce84b9e0b12b239f3551",
    ),
}
ROM_ARCHIVE_SHA = "921f000164a421c7628fbfee55b173384aafaa51883adc65cd27bf9b0af9e9a9"
ROM_URL = "https://github.com/espressif/esp-rom-elfs/releases/download/20241011/esp-rom-elfs-20241011.tar.gz"
RELEASE_URL = (
    f"https://github.com/mateuszsikora/esp32-testing-library/releases/download/v{__version__}"
)


# What a missing or damaged file of the cache asks for, unless esp32tl is unreleased.
PREPARE = "run esp32tl runtime prepare"


class RuntimeError(ValueError):
    """An actionable runtime installation or compatibility error."""


def target() -> str:
    pair = (platform.system(), platform.machine().lower())
    supported = {("Darwin", "arm64"): "macos-arm64", ("Linux", "x86_64"): "linux-x86_64"}
    if pair not in supported or (pair[0] == "Linux" and platform.libc_ver()[0] != "glibc"):
        raise RuntimeError(
            f"unsupported runtime platform: {pair}; use ESP32TL_RUNNER and ESP32TL_ROM"
        )
    if pair[0] == "Darwin" and int(platform.mac_ver()[0].split(".")[0]) < 15:
        raise RuntimeError("unsupported runtime platform: macOS 15 or newer is required")
    if pair[0] == "Linux":
        version = tuple(int(part) for part in platform.libc_ver()[1].split(".")[:2])
        if version < (2, 35):
            raise RuntimeError("unsupported runtime platform: glibc 2.35 or newer is required")
    return supported[pair]


def cache() -> Path:
    root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    if not root.is_absolute():
        raise RuntimeError("XDG_CACHE_HOME must be absolute")
    return root / "esp32tl" / __version__ / target()


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify(path: Path, expected: str, remedy: str = PREPARE) -> None:
    if path.is_symlink() or not path.is_file() or sha(path) != expected:
        raise RuntimeError(f"invalid checksum or missing file: {path}; {remedy}")


def identity(path: Path) -> None:
    try:
        result = subprocess.run(
            [str(path), "--runtime-version"], capture_output=True, text=True, timeout=10, check=True
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(
            f"cannot verify runner {path}: {error}; rebuild or run esp32tl runtime prepare"
        ) from error
    if result.stdout.strip() != f"{__version__} {SIM_REV}":
        raise RuntimeError(
            f"incompatible runner {path}: expected {__version__} {SIM_REV}, "
            f"got {result.stdout.strip()!r}"
        )


def manifest(path: Path) -> dict[str, str]:
    try:
        value = json.loads(path.read_text())
        if (value["version"], value["esp32sim"], value["target"]) != (
            __version__,
            SIM_REV,
            target(),
        ):
            raise ValueError("incompatible version, esp32sim revision or target")
        digest = value["sha256"]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            raise ValueError("invalid SHA-256")
        return {key: str(item) for key, item in value.items()}
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise RuntimeError(f"invalid runtime manifest {path}: {error}") from error


def rom_variable(chip: str) -> str:
    """The variable that names `chip`'s ROM ELF: ESP32TL_ROM for the classic ESP32,
    ESP32TL_ROM_<CHIP> (ESP32TL_ROM_ESP32S3) for another chip."""
    return "ESP32TL_ROM" if chip == "esp32" else f"ESP32TL_ROM_{chip.upper()}"


class Unreleased(NamedTuple):
    """Where an unreleased esp32tl was installed from, and where its runner is built."""

    source: str
    checkout: str


def unreleased() -> Unreleased | None:
    """Where this esp32tl was installed from when that is not a release: a commit of a git
    repository other than the release's tag, or a source directory, from the `direct_url.json` pip
    writes into the installed distribution (PEP 610). None for an install from an index or from
    an archive, as of a release's wheel, from the release's tag `v<version>`, and when pip
    recorded nothing."""
    try:
        text = metadata.distribution("esp32tl").read_text("direct_url.json")
        info = json.loads(text) if text else None
    except (metadata.PackageNotFoundError, OSError, ValueError):
        return None
    if not isinstance(info, dict):
        return None
    vcs = info.get("vcs_info")
    if isinstance(vcs, dict):
        if vcs.get("requested_revision") == f"v{__version__}":
            return None
        return Unreleased(
            f"commit {vcs.get('commit_id')} of {info.get('url')}", "a checkout of that commit"
        )
    if "dir_info" in info:
        return Unreleased(f"the directory {info.get('url')}", "that directory")
    return None


def require_release() -> None:
    """Refuse the release's runner when this esp32tl is not that release: their link and command
    line may differ though both carry the same version."""
    installed_from = unreleased()
    if installed_from is not None:
        raise RuntimeError(
            f"unreleased esp32tl: installed from {installed_from.source}, not from release "
            f"v{__version__}, whose runner need not match it; run make build-runner rom-elf in "
            f"{installed_from.checkout}, then set ESP32TL_RUNNER to its "
            f"runner/target/release/esp32tl-runner, and "
            f"{', '.join(rom_variable(chip) for chip in ROMS)} to the ROM ELFs in its "
            ".cache/esp-rom-elfs/20241011/"
        )


def cached_file(kind: str, directory: Path | None = None, chip: str = "esp32") -> Path:
    """Verify just the selected file and its cache manifest, without touching its peer: the
    runner, or `chip`'s ROM. An unreleased esp32tl gets no cached runner, and `prepare` installs
    nothing for it, so a cached ROM it cannot use names its variable instead."""
    if kind == "runner":
        require_release()
    released = kind == "runner" or unreleased() is None
    remedy = (
        PREPARE
        if released
        else f"set {rom_variable(chip)} to the {ROMS[chip][0]} make rom-elf puts in a "
        "checkout's .cache/esp-rom-elfs/20241011/: esp32tl runtime prepare installs nothing "
        "for an unreleased esp32tl"
    )
    directory = cache() if directory is None else directory
    if not directory.is_dir():
        raise RuntimeError(f"runtime missing: {remedy}")
    try:
        info = manifest(directory / "manifest.json")
    except RuntimeError as error:
        if released:
            raise
        raise RuntimeError(f"{error}; {remedy}") from error
    if kind == "runner":
        path = directory / "esp32tl-runner"
        verify(path, info["sha256"])
        identity(path)
    else:
        name, digest = ROMS[chip]
        path = directory / name
        verify(path, digest, remedy)
    return path


def installed(directory: Path | None = None) -> Path:
    """Verify the complete installation, even if the backend has explicit path overrides."""
    directory = cache() if directory is None else directory
    cached_file("runner", directory)
    for chip in ROMS:
        cached_file("rom", directory, chip)
    return directory


def download(url: str, path: Path) -> None:
    try:
        with urllib.request.urlopen(url, timeout=60) as response, path.open("wb") as stream:
            shutil.copyfileobj(response, stream)
    except (OSError, urllib.error.URLError) as error:
        raise RuntimeError(f"download failed: {url}: {error}") from error


def missing_manifest_hint() -> str:
    """What a 404 for the release's runtime manifest may mean, and the ways round it. Only
    possibilities: the server says no more than that the file is not there."""
    return (
        f"possible causes: release v{__version__} is not published (yet), or it has no runtime"
        f" for {target()}; alternatives: set ESP32TL_RUNNER and ESP32TL_ROM, or install esp32tl"
        " from a source checkout and run make setup there"
    )


def extract_rom(archive: Path, destination: Path, name: str = ROM) -> None:
    # Never extract paths, links, devices or arbitrary archive members onto the filesystem.
    try:
        with tarfile.open(archive) as tar:
            members = [m for m in tar.getmembers() if m.name == name]
            if len(members) != 1 or not members[0].isfile() or members[0].size > 2_000_000:
                raise RuntimeError("invalid ROM archive member")
            source = tar.extractfile(members[0])
            assert source is not None
            with source, destination.open("wb") as stream:
                shutil.copyfileobj(source, stream)
    except (tarfile.TarError, OSError) as error:
        raise RuntimeError(f"invalid ROM archive: {error}") from error


def prepare() -> Path:
    require_release()
    dest = cache()
    # Never silently replace a corrupt installation; no network is needed for a valid one.
    if dest.exists():
        return installed(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".install-", dir=dest.parent) as temporary:
        stage = Path(temporary) / "runtime"
        stage.mkdir()
        name = f"esp32tl-runner-{target()}"
        try:
            download(f"{RELEASE_URL}/{name}.json", stage / "manifest.json")
        except RuntimeError as error:
            cause = error.__cause__
            if isinstance(cause, urllib.error.HTTPError) and cause.code == 404:
                raise RuntimeError(f"{error}; {missing_manifest_hint()}") from cause
            raise
        info = manifest(stage / "manifest.json")
        download(f"{RELEASE_URL}/{name}", stage / "esp32tl-runner")
        verify(stage / "esp32tl-runner", info["sha256"])
        (stage / "esp32tl-runner").chmod(0o755)
        identity(stage / "esp32tl-runner")
        archive = Path(temporary) / "rom.tar.gz"
        download(ROM_URL, archive)
        verify(archive, ROM_ARCHIVE_SHA)
        for name, _ in ROMS.values():
            extract_rom(archive, stage / name, name)
        installed(stage)
        try:
            stage.rename(dest)
        except OSError:
            # Another preparer may have won the race; accept only a complete valid install.
            installed(dest)
    return dest
