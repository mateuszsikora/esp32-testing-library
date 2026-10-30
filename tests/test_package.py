from importlib.metadata import entry_points

import pytest

import esp32tl
from esp32tl import cli, pytest_plugin


def test_plugin_is_registered_through_pytest11() -> None:
    (entry,) = entry_points(group="pytest11", name="esp32tl")
    assert entry.value == "esp32tl.pytest_plugin"


def test_pytest_loaded_the_plugin(pytestconfig: pytest.Config) -> None:
    assert pytestconfig.pluginmanager.get_plugin("esp32tl") is pytest_plugin


def test_cli_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out == f"esp32tl {esp32tl.__version__}\n"
