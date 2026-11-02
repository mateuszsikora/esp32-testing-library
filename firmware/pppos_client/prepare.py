#!/usr/bin/env python3
"""Fetch the unmodified esp-protocols modem-v1.4.1 sources, verified by SHA-256."""

import hashlib
import io
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

REVISION = "2cc7c99664caf9705064affba6ea829137d4e1e8"
SHA256 = "7cdcf43c876e0bebbfeafe8aaa543c603df5a035d6e465f2cd743e5214041c32"
URL = f"https://codeload.github.com/espressif/esp-protocols/tar.gz/{REVISION}"


def prepare(out: Path) -> Path:
    source = out / f".esp-protocols-{REVISION}"
    if not source.is_dir():
        with urllib.request.urlopen(URL, timeout=60) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != SHA256:
            raise ValueError("esp-protocols archive SHA-256 does not match the pinned source")
        with tempfile.TemporaryDirectory(dir=out) as directory:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
                archive.extractall(directory, filter="data")
            Path(directory, f"esp-protocols-{REVISION}").rename(source)
    return source / "components/esp_modem/examples/pppos_client"


if __name__ == "__main__":
    print(prepare(Path(sys.argv[1])))
