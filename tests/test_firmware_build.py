"""firmware/build.sh and the `requires_docker` marker of this suite (plan M0).

build.sh runs here against a fake `docker` and a fake `idf.py`, which record how they were
called: these tests need neither Docker nor ESP-IDF and take milliseconds. The real container
build is `make firmware`, and test_firmware_images.py checks what it built.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BUILD_SH = ROOT / "firmware" / "build.sh"
CONTAINER = (
    "espressif/idf:v5.3.6@sha256:cee52f0c4fdaf088992a7a739f5993154e95cf1d3ab61c24f5b40e6d56a3a070"
)


# Each fake writes its arguments to $FAKE_LOG, one per line.
FAKE_DOCKER = """#!/bin/sh
[ "$1" = info ] && exit 0
printf '%s\\n' "$@" >"$FAKE_LOG"
"""
FAKE_IDF_PY = """#!/bin/sh
[ "$1" = --version ] && { echo "ESP-IDF v9.9.9"; exit 0; }
printf '%s\\n' "$@" >"$FAKE_LOG"
[ -z "${FAKE_FAIL:-}" ] || exit 2
while [ $# -gt 0 ]; do
	[ "$1" = -B ] && build=$2
	shift
done
[ -n "${FAKE_NO_FLASHER_ARGS:-}" ] || echo '{}' >"$build/flasher_args.json"
"""


def script(bin_dir: Path, name: str, body: str) -> None:
    bin_dir.mkdir(exist_ok=True)
    path = bin_dir / name
    path.write_text(body)
    path.chmod(0o755)


def fake_docker(bin_dir: Path, exit_code: int) -> None:
    script(bin_dir, "docker", f"#!/bin/sh\nexit {exit_code}\n")


def minimal_path(bin_dir: Path) -> str:
    """A PATH with only the tools build.sh needs besides Docker or ESP-IDF, and whatever
    bin_dir already holds."""
    bin_dir.mkdir(exist_ok=True)
    for tool in ("cat", "dirname", "id", "mkdir", "rm", "sed"):
        found = shutil.which(tool)
        assert found is not None, tool
        (bin_dir / tool).symlink_to(found)
    return str(bin_dir)


def run_build(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(BUILD_SH), *args], env=env, capture_output=True, text=True)


def logged(path: Path) -> list[str]:
    return path.read_text().splitlines()


@pytest.fixture
def local_idf(tmp_path: Path) -> dict[str, str]:
    """The environment of an IDF_LOCAL=1 build with the fake idf.py."""
    script(tmp_path / "bin", "idf.py", FAKE_IDF_PY)
    return {
        "PATH": minimal_path(tmp_path / "bin"),
        "FIRMWARE_OUT": str(tmp_path / "out"),
        "FAKE_LOG": str(tmp_path / "idf.log"),
        "IDF_LOCAL": "1",
        "IDF_PATH": str(tmp_path / "idf"),
    }


@pytest.fixture
def marked_suite(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makeconftest((Path(__file__).parent / "conftest.py").read_text())
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.requires_docker
        def test_needs_docker():
            pass

        def test_plain():
            pass
        """
    )
    return pytester


def test_requires_docker_skips_without_docker_on_path(
    marked_suite: pytest.Pytester, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    result = marked_suite.runpytest_subprocess("-rs", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1, skipped=1)
    result.stdout.fnmatch_lines(["SKIPPED * requires_docker: no docker on PATH"])


def test_requires_docker_skips_when_the_daemon_is_unreachable(
    marked_suite: pytest.Pytester, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_docker(tmp_path, exit_code=1)
    monkeypatch.setenv("PATH", str(tmp_path))
    result = marked_suite.runpytest_subprocess("-rs", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1, skipped=1)
    result.stdout.fnmatch_lines(
        ["SKIPPED * requires_docker: the Docker daemon is not reachable ('docker info' failed)"]
    )


def test_requires_docker_runs_the_test_when_docker_answers(
    marked_suite: pytest.Pytester, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_docker(tmp_path, exit_code=0)
    monkeypatch.setenv("PATH", str(tmp_path))
    result = marked_suite.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=2)


def test_build_refuses_an_unknown_image(tmp_path: Path) -> None:
    env = {"PATH": minimal_path(tmp_path / "bin"), "FIRMWARE_OUT": str(tmp_path / "out")}
    result = run_build(env, "hello_world", "nope")
    assert result.returncode == 1
    assert result.stderr == (
        "firmware/build.sh: unknown image 'nope'; the images are: hello_world\n"
    )
    assert not (tmp_path / "out").exists()


def test_build_names_docker_when_it_is_missing(tmp_path: Path) -> None:
    env = {"PATH": minimal_path(tmp_path / "bin"), "FIRMWARE_OUT": str(tmp_path / "out")}
    result = run_build(env)
    assert result.returncode == 1
    assert result.stderr == (
        "firmware/build.sh: needs Docker, or IDF_LOCAL=1 to build with a local ESP-IDF\n"
    )
    assert not (tmp_path / "out").exists()


def test_build_names_an_unreachable_docker_daemon(tmp_path: Path) -> None:
    fake_docker(tmp_path / "bin", exit_code=1)
    env = {"PATH": minimal_path(tmp_path / "bin"), "FIRMWARE_OUT": str(tmp_path / "out")}
    result = run_build(env)
    assert result.returncode == 1
    assert result.stderr == (
        "firmware/build.sh: the Docker daemon is not reachable ('docker info' failed);"
        " is Docker running?\n"
    )
    assert not (tmp_path / "out").exists()


def test_build_refuses_esp32tl_container_set_on_the_host(tmp_path: Path) -> None:
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
        "firmware/build.sh: ESP32TL_CONTAINER is set outside a container; unset it\n"
    )
    assert not (tmp_path / "out").exists()


def test_local_build_needs_an_exported_idf(tmp_path: Path) -> None:
    env = {
        "PATH": minimal_path(tmp_path / "bin"),
        "FIRMWARE_OUT": str(tmp_path / "out"),
        "IDF_LOCAL": "1",
    }
    result = run_build(env)
    assert result.returncode == 1
    assert result.stderr == (
        "firmware/build.sh: IDF_LOCAL=1 needs ESP-IDF in the environment:"
        " source $IDF_PATH/export.sh first\n"
    )
    assert not (tmp_path / "out").exists()


def test_build_starts_the_pinned_container(tmp_path: Path) -> None:
    script(tmp_path / "bin", "docker", FAKE_DOCKER)
    out = tmp_path / "out"
    env = {
        "PATH": minimal_path(tmp_path / "bin"),
        "FIRMWARE_OUT": str(out),
        "FAKE_LOG": str(tmp_path / "docker.log"),
    }
    result = run_build(env, "hello_world")
    assert result.returncode == 0, result.stderr
    out = out.resolve()
    assert logged(tmp_path / "docker.log") == [
        "run",
        "--rm",
        "--init",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--env",
        "HOME=/tmp",
        "--env",
        f"ESP32TL_CONTAINER={CONTAINER}",
        "--env",
        "FIRMWARE_OUT=/out",
        "--env",
        f"ESP32TL_HOST_OUT={out}",
        "--volume",
        f"{ROOT}:/project:ro",
        "--volume",
        f"{out}:/out",
        "--workdir",
        "/project",
        CONTAINER,
        "firmware/build.sh",
        "hello_world",
    ]


def test_local_build_runs_idf_py_and_records_how(tmp_path: Path, local_idf: dict[str, str]) -> None:
    result = run_build(local_idf)
    assert result.returncode == 0, result.stderr
    build = (tmp_path / "out" / "hello_world" / "build").resolve()
    project = f"{tmp_path}/idf/examples/get-started/hello_world"
    assert logged(tmp_path / "idf.log") == [
        "-C",
        project,
        "-B",
        str(build),
        f"-DSDKCONFIG={build}/sdkconfig",
        "-DIDF_TARGET=esp32",
        "build",
    ]
    assert json.loads((build / "esp32tl-build.json").read_text()) == {
        "idf_version": "v9.9.9",
        "container": None,
        "project": project,
    }
    assert (build / "esp32tl-built").exists()
    assert "not for goldens" in result.stderr
    assert result.stdout == f"firmware/build.sh: hello_world built in {build} (ESP-IDF v9.9.9)\n"


def test_build_reuses_a_build_directory_made_the_same_way(
    tmp_path: Path, local_idf: dict[str, str]
) -> None:
    assert run_build(local_idf).returncode == 0
    kept = tmp_path / "out" / "hello_world" / "build" / "kept"
    kept.touch()
    assert run_build(local_idf).returncode == 0
    assert kept.exists()


def test_build_replaces_a_build_directory_made_another_way(
    tmp_path: Path, local_idf: dict[str, str]
) -> None:
    build = tmp_path / "out" / "hello_world" / "build"
    build.mkdir(parents=True)
    (build / "esp32tl-build.json").write_text(f'{{"container": "{CONTAINER}"}}\n')
    (build / "left-over").touch()
    result = run_build(local_idf)
    assert result.returncode == 0, result.stderr
    assert "was built another way, removing it" in result.stdout
    assert not (build / "left-over").exists()
    assert json.loads((build / "esp32tl-build.json").read_text())["container"] is None


def test_build_fails_without_flasher_args(tmp_path: Path, local_idf: dict[str, str]) -> None:
    result = run_build({**local_idf, "FAKE_NO_FLASHER_ARGS": "1"})
    assert result.returncode == 1
    assert result.stderr.endswith(
        "firmware/build.sh: hello_world: the build wrote no flasher_args.json\n"
    )
    assert not (tmp_path / "out" / "hello_world" / "build" / "esp32tl-built").exists()


def test_failed_build_unmarks_a_finished_one(tmp_path: Path, local_idf: dict[str, str]) -> None:
    built = tmp_path / "out" / "hello_world" / "build" / "esp32tl-built"
    assert run_build(local_idf).returncode == 0
    assert built.exists()
    assert run_build({**local_idf, "FAKE_FAIL": "1"}).returncode == 2
    assert not built.exists()
