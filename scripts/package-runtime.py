#!/usr/bin/env python3
"""Create one release asset and its compatibility/checksum manifest from a tested build."""

import json
import shutil
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from esp32tl import __version__, runtime

root = Path(__file__).resolve().parents[1]
cargo = tomllib.loads((root / "runner/Cargo.toml").read_text())
assert cargo["package"]["version"] == __version__
assert cargo["dependencies"]["esp32"]["rev"] == runtime.SIM_REV
binary = root / "runner/target/release/esp32tl-runner"
runtime.identity(binary)
out = root / "dist"
out.mkdir(exist_ok=True)
name = f"esp32tl-runner-{runtime.target()}"
shutil.copyfile(binary, out / name)
(out / f"{name}.json").write_text(
    json.dumps(
        {
            "version": __version__,
            "esp32sim": runtime.SIM_REV,
            "target": runtime.target(),
            "sha256": runtime.sha(binary),
        },
        indent=2,
    )
    + "\n"
)
