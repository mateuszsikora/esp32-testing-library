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
        " hello_world console_repl nmea_reader gpio_echo sdspi sd_remount modem_at"
        " modem_recovery nvs_reader"
        " pppos_client mqtt_session http_request http_server persistence ota_state timer_wake"
        " deep_sleep"
        " bt_spp_initiator bt_spp_initiator_psram\n"
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
        "CCACHE_BASEDIR=/out",
        "--env",
        "CCACHE_NOHASHDIR=1",
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
        "console_repl": str(ROOT / "firmware" / "console_repl"),
        "nmea_reader": str(ROOT / "firmware" / "nmea_reader"),
        "gpio_echo": str(ROOT / "firmware" / "gpio_echo"),
        "sdspi": f"{tmp_path}/idf/examples/storage/sd_card/sdspi",
        "sd_remount": str(ROOT / "firmware" / "sd_remount"),
        "modem_at": str(ROOT / "firmware" / "modem_at"),
        "modem_recovery": str(ROOT / "firmware" / "modem_recovery"),
        "nvs_reader": str(ROOT / "firmware" / "nvs_reader"),
        "pppos_client": f"{tmp_path}/out/pppos-source",
        "mqtt_session": str(ROOT / "firmware" / "mqtt_session"),
        # copied, unchanged, once per ESP-IDF version: its component manager writes into it
        "http_request": f"{(tmp_path / 'out').resolve()}/.http_request-v9.9.9",
        "http_server": str(ROOT / "firmware" / "http_server"),
        "persistence": str(ROOT / "firmware" / "persistence"),
        "ota_state": str(ROOT / "firmware" / "ota_state"),
        "timer_wake": str(ROOT / "firmware" / "timer_wake"),
        "deep_sleep": f"{tmp_path}/idf/examples/system/deep_sleep",
        "bt_spp_initiator": (
            f"{tmp_path}/idf/examples/bluetooth/bluedroid/classic_bt/bt_spp_initiator"
        ),
        # the same project, with firmware/bt_spp_initiator_psram/sdkconfig.defaults after its own
        "bt_spp_initiator_psram": (
            f"{tmp_path}/idf/examples/bluetooth/bluedroid/classic_bt/bt_spp_initiator"
        ),
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
                    f"{ROOT}/firmware/{image}/sdkconfig.defaults"
                ]
                if image in ("pppos_client", "bt_spp_initiator_psram")
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
            "target": "esp32",
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


def test_chip_images_build_their_projects_for_the_chip(
    tmp_path: Path, local_idf: dict[str, str]
) -> None:
    result = run_build(local_idf, "--chips")
    assert result.returncode == 0, result.stderr
    out = (tmp_path / "out").resolve()
    hello = f"{tmp_path}/idf/examples/get-started/hello_world"
    projects = {
        "hello_world": hello,
        "console_repl": str(ROOT / "firmware" / "console_repl"),
        "gpio_echo": str(ROOT / "firmware" / "gpio_echo"),
        "nmea_reader": str(ROOT / "firmware" / "nmea_reader"),
        "nvs_reader": str(ROOT / "firmware" / "nvs_reader"),
        "http_request": f"{out}/.http_request-v9.9.9",
    }
    entries = [
        (image, chip, project)
        for chip in ("esp32s3", "esp32c3", "esp32c6")
        for image, project in projects.items()
    ]
    entries.append(("hello_world_psram", "esp32s3", hello))
    gatt = f"{tmp_path}/idf/examples/bluetooth/bluedroid/ble/gatt_server"
    entries += [("gatt_server", "esp32s3", gatt), ("gatt_server", "esp32c3", gatt)]
    expected = []
    for image, chip, project in entries:
        build = out / image / f"build-{chip}"
        expected += ["-C", project, "-B", str(build), f"-DSDKCONFIG={build}/sdkconfig"]
        expected.append(f"-DIDF_TARGET={chip}")
        if image == "hello_world_psram":
            # ESP-IDF's hello_world has no sdkconfig.defaults: only the image's own
            expected.append(
                f"-DSDKCONFIG_DEFAULTS={ROOT}/firmware/hello_world_psram/sdkconfig.defaults"
            )
        expected.append("build")
        assert json.loads((build / "esp32tl-build.json").read_text()) == {
            "idf_version": "v9.9.9",
            "container": None,
            "project": project,
            "target": chip,
        }
        assert (build / "esp32tl-built").exists()
    assert logged(tmp_path / "idf.log") == expected
    assert "firmware/build.sh: gpio_echo@esp32c3 built in " in result.stdout
    # the classic chip's build directories are not touched
    assert not (out / "hello_world" / "build").exists()


def test_chip_build_forwards_the_mode_into_the_pinned_container(tmp_path: Path) -> None:
    script(tmp_path / "bin", "docker", FAKE_DOCKER)
    env = {
        "PATH": minimal_path(tmp_path / "bin"),
        "FIRMWARE_OUT": str(tmp_path / "out"),
        "FAKE_LOG": str(tmp_path / "docker.log"),
    }
    result = run_build(env, "--chips", "gpio_echo@esp32s3")
    assert result.returncode == 0, result.stderr
    assert logged(tmp_path / "docker.log")[-4:] == [
        CONTAINER,
        "firmware/build.sh",
        "--chips",
        "gpio_echo@esp32s3",
    ]
    assert "unknown image 'gpio_echo@esp32'" in run_build(env, "--chips", "gpio_echo@esp32").stderr


@pytest.mark.parametrize("family, count", [((), 4), (("--examples",), 3), (("--chips",), 3)])
def test_build_shards_partition_the_complete_family(
    tmp_path: Path, family: tuple[str, ...], count: int
) -> None:
    script(tmp_path / "bin", "docker", FAKE_DOCKER)
    log = tmp_path / "docker.log"
    env = {
        "PATH": minimal_path(tmp_path / "bin"),
        "FIRMWARE_OUT": str(tmp_path / "out"),
        "FAKE_LOG": str(log),
    }

    def images(*args: str) -> list[str]:
        result = run_build(env, *family, *args)
        assert result.returncode == 0, result.stderr
        arguments = logged(log)
        selected = arguments[arguments.index("firmware/build.sh") + 1 :]
        if family:
            assert selected.pop(0) == family[0]
        return selected

    whole = images()
    shards = [images("--shard", f"{k}/{count}") for k in range(1, count + 1)]
    assert all(shards)
    assert sorted(image for shard in shards for image in shard) == sorted(whole)
    assert len(set(whole)) == len(whole)
    for k, shard in enumerate(shards):
        assert shard == whole[k::count]


@pytest.mark.parametrize("value", ["0/4", "5/4", "1/0", "1", "x/4", "1/2/3", "01/4", "1/04"])
def test_build_refuses_invalid_shard_before_starting_docker(tmp_path: Path, value: str) -> None:
    result = run_build({"PATH": minimal_path(tmp_path / "bin")}, "--shard", value)
    assert result.returncode != 0
    assert "--shard needs" in result.stderr


def test_build_refuses_an_empty_shard(tmp_path: Path) -> None:
    result = run_build({"PATH": minimal_path(tmp_path / "bin")}, "--shard", "2/2", "hello_world")
    assert result.returncode != 0
    assert "shard 2/2 selects no images" in result.stderr
