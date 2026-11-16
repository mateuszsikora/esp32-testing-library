#!/usr/bin/env python3
"""Update release metadata or recognize an exact, increasing version-only git diff."""

import argparse
import os
import re
import subprocess
import tomllib
from pathlib import Path

FILES = ("pyproject.toml", "runner/Cargo.toml", "runner/Cargo.lock")
VERSION = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"


def number(value: str) -> tuple[int, ...]:
    if not re.fullmatch(VERSION, value):
        raise ValueError("version must be MAJOR.MINOR.PATCH without a v prefix or leading zeros")
    return tuple(map(int, value.split(".")))


def versions(files: dict[str, str]) -> str:
    project, cargo, lock = (tomllib.loads(files[name]) for name in FILES)
    runner = [p["version"] for p in lock["package"] if p["name"] == "esp32tl-runner"]
    version = project["project"]["version"]
    if runner != [version] or cargo["package"]["version"] != version:
        raise ValueError("Python, runner and lockfile versions must match")
    number(version)
    return str(version)


def bumped(files: dict[str, str], version: str) -> dict[str, str]:
    if number(version) <= number(versions(files)):
        raise ValueError("new version must be greater than the current version")
    result = {}
    for name, section in zip(FILES, ("project", "package", None), strict=True):
        if section:
            pattern = rf'(^\[{section}\]\n(?:(?!^\[).)*?^version = ")[^"\n]+(")'
        else:
            pattern = r'(^name = "esp32tl-runner"\nversion = ")[^"\n]+(")'
        updated, count = re.subn(
            pattern, lambda m: m[1] + version + m[2], files[name], flags=re.M | re.S
        )
        if count != 1:
            raise ValueError(f"expected exactly one release version in {name}")
        result[name] = updated
    if versions(result) != version:
        raise ValueError("updated metadata does not match the requested version")
    return result


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args]).decode("utf-8")


def classify(base: str, head: str) -> str | None:
    # Compare full trees, not labels, titles, commit messages or only a list of paths.
    if set(git("diff", "--name-only", base, head).splitlines()) != set(FILES):
        return None
    for name in FILES:
        before = git("ls-tree", base, "--", name).split()
        after = git("ls-tree", head, "--", name).split()
        if before[:2] != ["100644", "blob"] or after[:2] != before[:2]:
            return None
    old = {name: git("show", f"{base}:{name}") for name in FILES}
    new = {name: git("show", f"{head}:{name}") for name in FILES}
    try:
        version = versions(new)
        return version if bumped(old, version) == new else None
    except (ValueError, KeyError, TypeError):
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    bump = commands.add_parser("bump")
    bump.add_argument("version")
    check = commands.add_parser("classify")
    check.add_argument("base")
    check.add_argument("head", nargs="?", default="HEAD")
    args = parser.parse_args()
    if args.command == "bump":
        # Validate all three files before writing any of them.
        files = {name: Path(name).read_bytes().decode("utf-8") for name in FILES}
        try:
            updated = bumped(files, args.version)
        except (ValueError, KeyError, TypeError) as error:
            parser.error(str(error))
        for name, content in updated.items():
            Path(name).write_text(content)
        print(f"Updated Python, runner and lockfile to {args.version}")
    else:
        version = classify(args.base, args.head)
        output = f"version_only={str(version is not None).lower()}\nversion={version or ''}\n"
        print(output, end="")
        if path := os.environ.get("GITHUB_OUTPUT"):
            with Path(path).open("a") as stream:
                stream.write(output)


if __name__ == "__main__":
    main()
