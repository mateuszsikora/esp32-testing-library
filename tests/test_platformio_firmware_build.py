"""firmware/build-platformio.sh, against a fake `docker` and a fake `pio` that record how they
were called, as test_firmware_build.py runs build.sh: no Docker, no PlatformIO, milliseconds. The
real container build is `make firmware`, and test_firmware_images.py checks what it built.

The tests of the host side, which start the fake `docker`, leave the repository's own
`.cache/platformio/` behind, empty if it was not there: the script creates it before `docker run`,
where Docker would create it owned by root."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BUILD_SH = ROOT / "firmware" / "build-platformio.sh"
CONTAINER = (
    "python:3.13-bookworm@sha256:cc55e4ded9c95f6ce2133b3d096db43f214d280c2a8af65d83e246c8fa7d168c"
)
PLATFORM = (
    "https://github.com/pioarduino/platform-espressif32/releases/download/55.03.312-1/"
    "platform-espressif32.zip"
)

FAKE_DOCKER = """#!/bin/sh
[ "$1" = info ] && exit 0
printf '%s\\n' "$@" >"$FAKE_LOG"
"""
# `pio run --project-dir DIR --environment ENV` appends its arguments to $FAKE_LOG and writes
# DIR/.pio/build/ENV/firmware.bin, as a finished build leaves it.
FAKE_PIO = """#!/bin/sh
[ "$1" = --version ] && { echo "PlatformIO Core, version 9.9.9"; exit 0; }
printf '%s\\n' "$@" >>"$FAKE_LOG"
[ -z "${FAKE_FAIL:-}" ] || exit 2
build=$3/.pio/build/$5
mkdir -p "$build"
[ -n "${FAKE_NO_FIRMWARE:-}" ] || : >"$build/firmware.bin"
"""


def script(bin_dir: Path, name: str, body: str) -> None:
    bin_dir.mkdir(exist_ok=True)
    path = bin_dir / name
    path.write_text(body)
    path.chmod(0o755)


def minimal_path(bin_dir: Path) -> str:
    """A PATH with only the tools the script needs besides Docker or PlatformIO, and whatever
    bin_dir already holds."""
    bin_dir.mkdir(exist_ok=True)
    for tool in ("cat", "cp", "dirname", "id", "mkdir", "rm", "sed"):
        found = shutil.which(tool)
        assert found is not None, tool
        (bin_dir / tool).symlink_to(found)
    return str(bin_dir)


def run_build(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(BUILD_SH), *args], env=env, capture_output=True, text=True)


@pytest.fixture
def local_pio(tmp_path: Path) -> dict[str, str]:
    """The environment of a PIO_LOCAL=1 build with the fake pio, into tmp_path/out."""
    script(tmp_path / "bin", "pio", FAKE_PIO)
    return {
        "PATH": minimal_path(tmp_path / "bin"),
        "FIRMWARE_OUT": str(tmp_path / "out"),
        "FAKE_LOG": str(tmp_path / "pio.log"),
        "PIO_LOCAL": "1",
    }


def build_dir(tmp_path: Path) -> Path:
    return (tmp_path / "out").resolve() / "arduino_shell" / ".pio" / "build" / "esp32dev"


def test_refuses_an_unknown_image(tmp_path: Path) -> None:
    env = {"PATH": minimal_path(tmp_path / "bin"), "FIRMWARE_OUT": str(tmp_path / "out")}
    result = run_build(env, "arduino_shell", "nope")
    assert result.returncode == 1
    assert result.stderr == (
        "firmware/build-platformio.sh: unknown image 'nope'; the images are: arduino_shell"
        " arduino_shell@esp32s3_usb\n"
    )
    assert not (tmp_path / "out").exists()


def test_names_docker_when_it_is_missing(tmp_path: Path) -> None:
    env = {"PATH": minimal_path(tmp_path / "bin"), "FIRMWARE_OUT": str(tmp_path / "out")}
    result = run_build(env)
    assert result.returncode == 1
    assert result.stderr == (
        "firmware/build-platformio.sh: needs Docker, or PIO_LOCAL=1 to build with a local"
        " PlatformIO\n"
    )
    assert not (tmp_path / "out").exists()


def test_local_build_needs_pio_on_path(tmp_path: Path) -> None:
    env = {
        "PATH": minimal_path(tmp_path / "bin"),
        "FIRMWARE_OUT": str(tmp_path / "out"),
        "PIO_LOCAL": "1",
    }
    result = run_build(env)
    assert result.returncode == 1
    assert (
        result.stderr
        == "firmware/build-platformio.sh: PIO_LOCAL=1 needs PlatformIO's pio on PATH\n"
    )
    assert not (tmp_path / "out").exists()


def test_refuses_esp32tl_container_set_on_the_host(tmp_path: Path) -> None:
    if Path("/.dockerenv").exists():
        pytest.skip("this suite runs in a Docker container")
    env = {
        "PATH": minimal_path(tmp_path / "bin"),
        "FIRMWARE_OUT": str(tmp_path / "out"),
        "ESP32TL_CONTAINER": CONTAINER,
    }
    result = run_build(env)
    assert result.returncode == 1
    assert result.stderr == (
        "firmware/build-platformio.sh: ESP32TL_CONTAINER is set outside a container; unset it\n"
    )


def test_starts_the_pinned_container_with_the_core_directory(tmp_path: Path) -> None:
    script(tmp_path / "bin", "docker", FAKE_DOCKER)
    out = tmp_path / "out"
    env = {
        "PATH": minimal_path(tmp_path / "bin"),
        "FIRMWARE_OUT": str(out),
        "FAKE_LOG": str(tmp_path / "docker.log"),
    }
    result = run_build(env, "arduino_shell")
    assert result.returncode == 0, result.stderr
    out = out.resolve()
    assert (tmp_path / "docker.log").read_text().splitlines() == [
        "run",
        "--rm",
        "--init",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--env",
        "HOME=/tmp",
        "--env",
        "PLATFORMIO_CORE_DIR=/platformio",
        "--env",
        f"ESP32TL_CONTAINER={CONTAINER}",
        "--env",
        "FIRMWARE_OUT=/out",
        "--env",
        f"ESP32TL_HOST_OUT={out}",
        "--env",
        "ESP32TL_IN_PLACE=0",
        "--volume",
        f"{ROOT}:/project:ro",
        "--volume",
        f"{out}:/out",
        "--volume",
        f"{ROOT}/.cache/platformio:/platformio",
        "--workdir",
        "/project",
        CONTAINER,
        "firmware/build-platformio.sh",
        "arduino_shell",
    ]


def test_the_host_tells_the_container_to_build_in_place_into_firmware(tmp_path: Path) -> None:
    # /out and /project are then two mounts of the project's directory, which only the host can
    # tell by name; the fake docker builds nothing, so firmware/ is left as it is
    script(tmp_path / "bin", "docker", FAKE_DOCKER)
    env = {
        "PATH": minimal_path(tmp_path / "bin"),
        "FIRMWARE_OUT": str(ROOT / "firmware"),
        "FAKE_LOG": str(tmp_path / "docker.log"),
    }
    result = run_build(env)
    assert result.returncode == 0, result.stderr
    args = (tmp_path / "docker.log").read_text().splitlines()
    assert "ESP32TL_IN_PLACE=1" in args and f"{ROOT / 'firmware'}:/out" in args


def test_local_build_runs_pio_in_a_copy_and_records_how(
    tmp_path: Path, local_pio: dict[str, str]
) -> None:
    result = run_build(local_pio)
    assert result.returncode == 0, result.stderr
    directory = (tmp_path / "out").resolve() / "arduino_shell"
    # every image: the project's esp32dev environment, then its esp32s3_usb one
    assert (tmp_path / "pio.log").read_text().splitlines() == [
        *("run", "--project-dir", str(directory), "--environment", "esp32dev"),
        *("run", "--project-dir", str(directory), "--environment", "esp32s3_usb"),
    ]
    # FIRMWARE_OUT is not firmware/: pio ran in a copy of the project's platformio.ini and src/
    project = ROOT / "firmware" / "arduino_shell"
    assert (directory / "platformio.ini").read_bytes() == (project / "platformio.ini").read_bytes()
    assert (directory / "src" / "main.cpp").read_bytes() == (
        project / "src" / "main.cpp"
    ).read_bytes()
    build = build_dir(tmp_path)
    assert json.loads((build / "esp32tl-build.json").read_text()) == {
        "platformio": "9.9.9",
        "platform": PLATFORM,
        "container": None,
        "project": str(project),
    }
    assert (build / "esp32tl-built").is_file()
    assert "not for goldens" in result.stderr
    usb = build.parent / "esp32s3_usb"
    assert json.loads((usb / "esp32tl-build.json").read_text()) == json.loads(
        (build / "esp32tl-build.json").read_text()
    )
    assert result.stdout == (
        f"firmware/build-platformio.sh: arduino_shell built in {build} (PlatformIO Core 9.9.9)\n"
        f"firmware/build-platformio.sh: arduino_shell@esp32s3_usb built in {usb}"
        " (PlatformIO Core 9.9.9)\n"
    )


def test_reuses_a_build_made_the_same_way(tmp_path: Path, local_pio: dict[str, str]) -> None:
    assert run_build(local_pio).returncode == 0
    kept = build_dir(tmp_path) / "kept"
    kept.touch()
    assert run_build(local_pio).returncode == 0
    assert kept.exists()


def test_replaces_a_build_made_another_way(tmp_path: Path, local_pio: dict[str, str]) -> None:
    build = build_dir(tmp_path)
    build.mkdir(parents=True)
    (build / "esp32tl-build.json").write_text(f'{{"container": "{CONTAINER}"}}\n')
    left_over = build.parent.parent / "libdeps"
    left_over.mkdir()
    result = run_build(local_pio)
    assert result.returncode == 0, result.stderr
    assert "was built another way, removing it" in result.stdout
    assert not left_over.exists()
    assert json.loads((build / "esp32tl-build.json").read_text())["container"] is None


def test_records_the_build_again_after_pio_cleaned_it(
    tmp_path: Path, local_pio: dict[str, str]
) -> None:
    # pio removes .pio/build/ when platformio.ini changes, the record with it
    script(
        tmp_path / "bin",
        "pio",
        FAKE_PIO.replace("build=$3/.pio/build/$5\n", 'build=$3/.pio/build/$5\nrm -rf "$build"\n'),
    )
    assert run_build(local_pio).returncode == 0
    build = build_dir(tmp_path)
    assert (build / "esp32tl-built").is_file()
    assert json.loads((build / "esp32tl-build.json").read_text())["platform"] == PLATFORM


def test_fails_without_firmware_bin(tmp_path: Path, local_pio: dict[str, str]) -> None:
    result = run_build({**local_pio, "FAKE_NO_FIRMWARE": "1"})
    assert result.returncode == 1
    assert result.stderr.endswith(
        "firmware/build-platformio.sh: arduino_shell: the build wrote no firmware.bin\n"
    )
    assert not (build_dir(tmp_path) / "esp32tl-built").exists()


def test_failed_build_unmarks_a_finished_one(tmp_path: Path, local_pio: dict[str, str]) -> None:
    built = build_dir(tmp_path) / "esp32tl-built"
    assert run_build(local_pio).returncode == 0
    assert built.exists()
    assert run_build({**local_pio, "FAKE_FAIL": "1"}).returncode == 2
    assert not built.exists()
