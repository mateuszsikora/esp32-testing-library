#!/usr/bin/env python3
"""Real wheel, HTTP downloads, real runner and firmware, then offline reuse outside checkout."""

import functools
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import venv
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from esp32tl import runtime  # noqa: E402

firmware = Path(sys.argv[1]).resolve()
wheels = list((root / "dist").glob("*.whl"))
if len(wheels) != 1:
    sys.exit("expected exactly one release wheel in dist/")
wheel = wheels[0]
with tempfile.TemporaryDirectory(prefix="esp32tl-wheel-") as temporary:
    work = Path(temporary)
    venv.EnvBuilder(with_pip=True).create(work / "venv")
    python = work / "venv/bin/python"
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"PYTHONPATH", "ESP32TL_RUNNER", "ESP32TL_ROM"}
    }
    env["XDG_CACHE_HOME"] = str(work / "cache")
    subprocess.run([python, "-m", "pip", "install", str(wheel)], cwd=work, env=env, check=True)
    # Preparation and firmware runs have no Rust tools on PATH.
    env["PATH"] = f"{work / 'venv/bin'}:/usr/bin:/bin"
    assets = work / "assets"
    shutil.copytree(root / "dist", assets)
    # Use the actual pinned upstream archive, not a fabricated fixture or mocked downloader.
    runtime.download(runtime.ROM_URL, assets / "rom.tar.gz")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(assets))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    code = (
        "from esp32tl import runtime; from esp32tl.cli import main; "
        f"runtime.RELEASE_URL={url!r}; runtime.ROM_URL={url + '/rom.tar.gz'!r}; "
        "raise SystemExit(main(['runtime','prepare']))"
    )
    try:
        subprocess.run([python, "-c", code], cwd=work, env=env, check=True)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    shutil.copytree(firmware, work / "app/build")
    (work / "test_boot.py").write_text("""import pytest
@pytest.mark.app_path("app")
def test_boot(board):
    board.power_on()
    board.console.expect("Hello world!")
""")
    # Make every Python network connection fail, including accidental installer downloads.
    (work / "sitecustomize.py").write_text("""import socket
def denied(*args, **kwargs):
    raise AssertionError("network disabled")
socket.create_connection = denied
original_connect = socket.socket.connect
def connect(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6):
        denied()
    return original_connect(self, address)
socket.socket.connect = connect
socket.socket.connect_ex = denied
""")
    env["PYTHONPATH"] = str(work)
    for _ in range(2):
        subprocess.run(
            [work / "venv/bin/esp32tl", "runtime", "prepare"], cwd=work, env=env, check=True
        )
        subprocess.run(
            [python, "-m", "pytest", "-q", "test_boot.py"], cwd=work, env=env, check=True
        )
    print(json.dumps({"wheel": wheel.name, "target": runtime.target(), "offline_runs": 2}))
