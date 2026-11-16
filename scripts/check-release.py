#!/usr/bin/env python3
"""Check a complete release; create SHA256SUMS for a draft or verify it before PyPI."""

import argparse
import hashlib
import json
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = ("linux-x86_64", "macos-arm64")


def checksum(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def check(directory: Path, *, verify: bool = False, tag: str | None = None) -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    cargo = tomllib.loads((ROOT / "runner/Cargo.toml").read_text())
    version = project["project"]["version"]
    if cargo["package"]["version"] != version:
        raise ValueError("Python and runner versions differ")
    if tag is not None and tag != f"v{version}":
        raise ValueError("release tag does not match the package version")
    revision = cargo["dependencies"]["esp32"]["rev"]
    names = {f"esp32tl-{version}-py3-none-any.whl", f"esp32tl-{version}.tar.gz"}
    for target in TARGETS:
        name = f"esp32tl-runner-{target}"
        names.update((name, name + ".json"))
        metadata = json.loads((directory / (name + ".json")).read_text())
        expected = {
            "version": version,
            "esp32sim": revision,
            "target": target,
            "sha256": checksum(directory / name),
        }
        if metadata != expected:
            raise ValueError(f"incompatible or corrupted runner: {name}")
    actual = {p.name for p in directory.iterdir()} - {"SHA256SUMS"}
    if actual != names:
        raise ValueError(
            f"release files differ: missing {names - actual}, unexpected {actual - names}"
        )
    for name in names:
        path = directory / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"not a nonempty regular release file: {name}")
    sums = "".join(f"{checksum(directory / name)}  {name}\n" for name in sorted(names))
    sums_path = directory / "SHA256SUMS"
    if verify:
        if sums_path.is_symlink() or sums_path.read_text() != sums:
            raise ValueError("published SHA256SUMS does not match the complete release")
    else:
        sums_path.write_text(sums)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument(
        "--verify", action="store_true", help="verify existing SHA256SUMS unchanged"
    )
    parser.add_argument("--tag", help="require the published tag to match the package version")
    args = parser.parse_args()
    if args.verify and args.tag is None:
        parser.error("--verify requires --tag")
    try:
        check(args.directory, verify=args.verify, tag=args.tag)
    except (OSError, ValueError, KeyError) as error:
        sys.exit(f"release verification failed: {error}")
