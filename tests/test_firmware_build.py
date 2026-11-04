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


# Each fake writes its arguments to $FAKE_LOG, one per line; idf.py, which runs once per image,
# appends them.
FAKE_DOCKER = """#!/bin/sh
[ "$1" = info ] && exit 0
printf '%s\\n' "$@" >"$FAKE_LOG"
"""
FAKE_IDF_PY = """#!/bin/sh
[ "$1" = --version ] && { echo "ESP-IDF v9.9.9"; exit 0; }
printf '%s\\n' "$@" >>"$FAKE_LOG"
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
    for tool in ("cat", "cp", "dirname", "id", "mkdir", "mv", "rm", "sed"):
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
    script(tmp_path / "bin", "python3", '#!/bin/sh\nprintf "%s\\n" "$2/pppos-source"\n')
    example = tmp_path / "idf" / "examples" / "protocols" / "http_request"
    example.mkdir(parents=True)
    (example / "CMakeLists.txt").write_text("project(http_request)\n")
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
        "firmware/build.sh: unknown image 'nope'; the images are:"
        " hello_world nmea_reader gpio_echo sdspi sd_remount modem_at nvs_reader pppos_client"
        " mqtt_session http_request persistence\n"
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
    # every image, in the order of IMAGES: ESP-IDF's examples and the projects under firmware/
    projects = {
        "hello_world": f"{tmp_path}/idf/examples/get-started/hello_world",
        "nmea_reader": str(ROOT / "firmware" / "nmea_reader"),
        "gpio_echo": str(ROOT / "firmware" / "gpio_echo"),
        "sdspi": f"{tmp_path}/idf/examples/storage/sd_card/sdspi",
        "sd_remount": str(ROOT / "firmware" / "sd_remount"),
        "modem_at": str(ROOT / "firmware" / "modem_at"),
        "nvs_reader": str(ROOT / "firmware" / "nvs_reader"),
        "pppos_client": f"{tmp_path}/out/pppos-source",
        "mqtt_session": str(ROOT / "firmware" / "mqtt_session"),
        # copied, unchanged, once per ESP-IDF version: its component manager writes into it
        "http_request": f"{(tmp_path / 'out').resolve()}/.http_request-v9.9.9",
        "persistence": str(ROOT / "firmware" / "persistence"),
    }
    builds = {image: (tmp_path / "out" / image / "build").resolve() for image in projects}
    assert logged(tmp_path / "idf.log") == [
        argument
        for image, project in projects.items()
        for argument in [
            "-C",
            project,
            "-B",
            str(builds[image]),
            f"-DSDKCONFIG={builds[image]}/sdkconfig",
            "-DIDF_TARGET=esp32",
            *(
                [
                    f"-DSDKCONFIG_DEFAULTS={project}/sdkconfig.defaults;"
                    f"{ROOT}/firmware/pppos_client/sdkconfig.defaults"
                ]
                if image == "pppos_client"
                else []
            ),
            "build",
        ]
    ]
    for image, project in projects.items():
        assert json.loads((builds[image] / "esp32tl-build.json").read_text()) == {
            "idf_version": "v9.9.9",
            "container": None,
            "project": project,
        }
        assert (builds[image] / "esp32tl-built").exists()
    copied = Path(projects["http_request"]) / "CMakeLists.txt"
    assert copied.read_text() == "project(http_request)\n"
    assert "not for goldens" in result.stderr
    assert result.stdout == "".join(
        f"firmware/build.sh: {image} built in {build} (ESP-IDF v9.9.9)\n"
        for image, build in builds.items()
    )


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


@pytest.mark.parametrize("names", [(), ("gps_alarm_clock",)])
def test_examples_are_discovered_and_built_from_their_projects(
    tmp_path: Path, local_idf: dict[str, str], names: tuple[str, ...]
) -> None:
    result = run_build(local_idf, "--examples", *names)
    assert result.returncode == 0, result.stderr
    projects = (
        [ROOT / "examples" / name for name in names]
        if names
        else sorted(path.parent for path in (ROOT / "examples").glob("*/CMakeLists.txt"))
    )
    for project in projects:
        build = tmp_path / "out" / project.name / "build"
        assert (build / "esp32tl-built").is_file()
        assert json.loads((build / "esp32tl-build.json").read_text())["project"] == str(project)
        assert str(project) in logged(tmp_path / "idf.log")
    assert logged(tmp_path / "idf.log").count("build") == len(projects)


def test_example_build_forwards_the_mode_into_the_pinned_container(tmp_path: Path) -> None:
    script(tmp_path / "bin", "docker", FAKE_DOCKER)
    env = {
        "PATH": minimal_path(tmp_path / "bin"),
        "FIRMWARE_OUT": str(tmp_path / "out"),
        "FAKE_LOG": str(tmp_path / "docker.log"),
    }
    result = run_build(env, "--examples", "gps_alarm_clock")
    assert result.returncode == 0, result.stderr
    args = logged(tmp_path / "docker.log")
    assert args[-4:] == [CONTAINER, "firmware/build.sh", "--examples", "gps_alarm_clock"]
    assert f"{ROOT}:/project:ro" in args
    assert f"{tmp_path / 'out'}:/out" in args


def test_example_names_are_validated_before_creating_output(
    tmp_path: Path, local_idf: dict[str, str]
) -> None:
    result = run_build(local_idf, "--examples", "../firmware/nmea_reader")
    assert result.returncode == 1
    assert "unknown image '../firmware/nmea_reader'" in result.stderr
    assert not (tmp_path / "out").exists()


def test_failed_example_build_removes_its_completion_marker(
    tmp_path: Path, local_idf: dict[str, str]
) -> None:
    assert run_build(local_idf, "--examples", "gps_alarm_clock").returncode == 0
    built = tmp_path / "out" / "gps_alarm_clock" / "build" / "esp32tl-built"
    assert built.is_file()
    result = run_build({**local_idf, "FAKE_FAIL": "1"}, "--examples", "gps_alarm_clock")
    assert result.returncode == 2
    assert not built.exists()
