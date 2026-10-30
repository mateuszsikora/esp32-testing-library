"""The esp32tl pytest plugin, loaded through the `pytest11` entry point.

It provides the `board` and `console` fixtures on top of pytest-embedded's classes (plan §3);
`gnss`, `modem`, `bt`, `wifi`, `sd_card`, `lines` and `scenario` come with later milestones.

A test that uses `board` is parametrised over the backends by `pytest_generate_tests`, in one
`metafunc.parametrize` over `board`, `embedded_services` and `app_path`, so that the three always
travel together under one id. pytest-embedded's `--embedded-services` and `--app-path` are refused
for such a test: an empty `embedded_services` parameter falls back to the command line, and a
session started with `--embedded-services esp` would make the `esp32sim` case open a serial port.
The app comes from the `app_path` marker instead.
"""

from collections.abc import Iterator

import pytest
from pytest_embedded.dut import Dut
from pytest_embedded.log import MessageQueue
from pytest_embedded.utils import Meta

from esp32tl.backends import SERVICES
from esp32tl.backends.esp32sim import BackendError, Esp32Sim
from esp32tl.interfaces.console import Console


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("esp32tl")
    group.addoption(
        "--esp32tl-backend",
        choices=list(SERVICES),
        help="run the tests that use `board` on this backend only (default: every backend)",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "app_path(path): the ESP-IDF app a test runs, its build in path/build; relative to the"
        " test file's directory, which is the default",
    )


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "board" not in metafunc.fixturenames:
        return
    for option in ("embedded_services", "app_path"):
        if metafunc.config.getoption(option, None):
            flag = "--" + option.replace("_", "-")
            raise pytest.UsageError(
                f"{flag} is set: esp32tl chooses it for every test that uses `board`"
                " (the backend through --esp32tl-backend, the app through the app_path marker)"
            )
    app_path = metafunc.definition.path.parent
    marker = metafunc.definition.get_closest_marker("app_path")
    if marker is not None:
        app_path /= marker.args[0]
    chosen = metafunc.config.getoption("esp32tl_backend")
    backends = [chosen] if chosen else list(SERVICES)
    metafunc.parametrize(
        ("board", "embedded_services", "app_path"),
        [(backend, SERVICES[backend], str(app_path.resolve())) for backend in backends],
        indirect=True,
        ids=backends,
    )


@pytest.fixture
def board(
    request: pytest.FixtureRequest, dut: Dut, app_path: str, msg_queue: MessageQueue, _meta: Meta
) -> Iterator[Esp32Sim]:
    """The firmware in `app_path`, running on the backend this test is parametrised with."""
    assert request.param == Esp32Sim.name, request.param
    try:
        backend = Esp32Sim(dut, app_path, msg_queue, _meta)
    except BackendError as error:
        # from None: the message says it all, and the chained error would say it twice
        raise pytest.fail.Exception(f"{Esp32Sim.name}: {error}", pytrace=False) from None
    try:
        yield backend
    finally:
        backend.close()


@pytest.fixture
def console(board: Esp32Sim) -> Console:
    """The firmware's console; `console.expect` is `dut.expect`."""
    return Console(board.dut)
