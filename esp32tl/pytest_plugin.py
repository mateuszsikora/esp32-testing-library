"""The esp32tl pytest plugin, loaded through the `pytest11` entry point.

It provides the `board` and `console` fixtures on top of pytest-embedded's classes (plan §3);
`gnss`, `modem`, `bt`, `wifi`, `sd_card`, `lines` and `scenario` come with later milestones.

A test that uses `board` is parametrised over the backends by `pytest_generate_tests`, in one
`metafunc.parametrize` over `board`, `embedded_services` and `app_path`, so that the three always
travel together under one id. pytest-embedded's `--embedded-services`, `--app-path` and
`--build-dir` are refused when a test uses `board`: an empty `embedded_services` parameter falls
back to the command line, and a session started with `--embedded-services esp` would make the
`esp32sim` case open a serial port. The app comes from the `app_path` marker instead, its build
from `build/` in it. `--count` above 1 is refused too: a test has one board.
"""

from collections.abc import Generator, Iterator
from typing import NoReturn

import pytest
from pytest_embedded.dut import Dut
from pytest_embedded.log import MessageQueue
from pytest_embedded.utils import Meta

from esp32tl.backends import SERVICES
from esp32tl.backends.esp32sim import BackendError, Esp32Sim, ended_on_runner_exit
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
    app_path = metafunc.definition.path.parent
    marker = metafunc.definition.get_closest_marker("app_path")
    if marker is not None:
        if len(marker.args) != 1 or marker.kwargs:
            pytest.fail(
                f"{metafunc.definition.nodeid}: the app_path marker takes one path,"
                ' as in @pytest.mark.app_path("../firmware/hello_world")',
                pytrace=False,
            )
        app_path /= marker.args[0]
    chosen = metafunc.config.getoption("esp32tl_backend")
    backends = [chosen] if chosen else list(SERVICES)
    metafunc.parametrize(
        ("board", "embedded_services", "app_path"),
        [(backend, SERVICES[backend], str(app_path.resolve())) for backend in backends],
        indirect=True,
        ids=backends,
    )


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Refuses the options esp32tl sets itself, when a selected test uses `board`: last, after
    -k and -m have deselected theirs. Here rather than in pytest_generate_tests: raised there, a
    UsageError (or pytest.exit) is a collection error, with a traceback through pluggy and exit
    code 2; here it is one line and exit code 4."""
    if not any("board" in getattr(item, "fixturenames", ()) for item in items):
        return
    for option in ("embedded_services", "app_path", "build_dir"):
        if config.getoption(option, None):
            raise pytest.UsageError(
                f"--{option.replace('_', '-')} is set: esp32tl chooses it for every test that uses"
                " `board` (the backend through --esp32tl-backend, the app through the app_path"
                " marker, the build in its build/ directory)"
            )
    if config.getoption("count", 1) != 1:
        raise pytest.UsageError("--count is set: esp32tl runs one board in a test")


# Set on a test whose call failed on an expect that read the runner's end: its error names the exit
# code already.
EXPECT_SAW_RUNNER_EXIT = pytest.StashKey[bool]()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    if (
        report.when == "call"
        and call.excinfo is not None
        and ended_on_runner_exit(call.excinfo.value)
    ):
        item.stash[EXPECT_SAW_RUNNER_EXIT] = True
    return report


@pytest.fixture
def board(
    request: pytest.FixtureRequest, dut: Dut, app_path: str, msg_queue: MessageQueue, _meta: Meta
) -> Iterator[Esp32Sim]:
    """The firmware in `app_path`, running on the backend this test is parametrised with."""
    assert request.param == Esp32Sim.name, request.param
    if not isinstance(dut, Dut):
        # `count` parametrised above 1 in the test: pytest-embedded made a dut per count.
        pytest.fail("count is above 1: esp32tl runs one board in a test", pytrace=False)
    try:
        backend = Esp32Sim(dut, app_path, msg_queue, _meta)
    except BackendError as error:
        backend_failed(error)
    try:
        yield backend
    finally:
        try:
            backend.close()
        except BackendError as error:
            # An expect that ended on the runner's exit has already reported it, with its code.
            if not request.node.stash.get(EXPECT_SAW_RUNNER_EXIT, False):
                backend_failed(error)


def backend_failed(error: BackendError) -> NoReturn:
    # from None: the message says it all, and the chained error would say it twice
    raise pytest.fail.Exception(f"{Esp32Sim.name}: {error}", pytrace=False) from None


@pytest.fixture
def console(board: Esp32Sim) -> Console:
    """The firmware's console; `console.expect` is `dut.expect`."""
    return Console(board.dut)
