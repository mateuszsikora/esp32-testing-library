"""The esp32tl pytest plugin, loaded through the `pytest11` entry point.

It provides the `board`, `console`, `gnss` and `sd_card` fixtures on top of pytest-embedded's
classes (plan §3); `modem`, `bt`, `wifi`, `lines` and `scenario` come with later milestones.
The board runs a board profile, `esp32-devkitc` unless the `board_profile` marker names another
(plan §4.5).

A test that uses `board` is parametrised over the backends by `pytest_generate_tests`, in one
`metafunc.parametrize` over `board`, `embedded_services` and `app_path`, so that the three always
travel together under one id. pytest-embedded's `--embedded-services`, `--app-path` and
`--build-dir` are refused when a test uses `board`: an empty `embedded_services` parameter falls
back to the command line, and a session started with `--embedded-services esp` would make the
`esp32sim` case open a serial port. The app comes from the `app_path` marker instead, its build
from `build/` in it. `--count` above 1 is refused too: a test has one board.

The test body sets up what is around the board, as `gnss.replay(path)` does, then calls
`board.power_on()`, which starts the run; from there the board's clock moves only while the test
waits on it (`board.wait`, `board.wait_until`, `console.expect`). A source that the backend's
capability declaration (`esp32tl.boards.capabilities`) does not give skips the test at the call
that asks for it. The `tap` marker says which interfaces a test taps; a backend that cannot gets
its parameter marked `skip`, with the reason, before any fixture is set up, so pytest-embedded
never opens a serial port for it.

Every test that uses `board` gets pytest-timeout's `timeout` marker of `DEFAULT_TIMEOUT` seconds of
wall time, unless the session or the test sets a timeout of its own: the runner waits for the test
for as long as the body runs, so this is what ends a run that hangs.
"""

import json
from collections.abc import Generator, Iterator
from dataclasses import dataclass
from typing import NoReturn

import pytest
import pytest_timeout
from pytest_embedded.dut import Dut
from pytest_embedded.log import MessageQueue
from pytest_embedded.utils import Meta

from esp32tl import boards
from esp32tl.backends import SERVICES
from esp32tl.backends.esp32sim import BackendError, Esp32Sim, ended_while_waiting
from esp32tl.boards import DEFAULT_PROFILE, BoardProfile, ProfileError
from esp32tl.boards.capabilities import CapabilityError, Request, check, declared, refusal
from esp32tl.interfaces.console import Console
from esp32tl.interfaces.sd_card import SdCard
from esp32tl.interfaces.uart import Uart

# The wall-time timeout of a test that uses `board`, in seconds, unless one is configured: 600 s
# covers at least three minutes of the board's busiest work, at the 0.3 times real time stage 0
# measured for CPU-bound code (docs/stage0-findings.md), and far more of an idle board.
DEFAULT_TIMEOUT = 600


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
    config.addinivalue_line(
        "markers",
        f"board_profile(name): the board profile a test runs; {DEFAULT_PROFILE} by default",
    )
    config.addinivalue_line(
        "markers",
        "tap(*interfaces): the board interfaces the test taps; a backend that cannot gives a skip",
    )


class MarkerError(Exception):
    """A marker of esp32tl's used another way than its registration says."""


def board_profile(node: pytest.Item) -> BoardProfile:
    """The profile the closest `board_profile` marker names, else the default one."""
    marker = node.get_closest_marker("board_profile")
    if marker is None:
        return boards.profile(DEFAULT_PROFILE)
    if len(marker.args) != 1 or marker.kwargs or not isinstance(marker.args[0], str):
        raise MarkerError(
            "the board_profile marker takes one name,"
            f' as in @pytest.mark.board_profile("{DEFAULT_PROFILE}")'
        )
    return boards.profile(marker.args[0])


def board_requests(node: pytest.Item) -> list[Request]:
    """The taps the `tap` markers of `node` and its parents ask for, added up over every level:
    a tap asks for more, never for something else."""
    taps: list[str] = []
    for marker in node.iter_markers("tap"):
        args = marker.args
        if not args or marker.kwargs or not all(isinstance(a, str) for a in args):
            raise MarkerError(
                'the tap marker takes one or more interfaces, as in @pytest.mark.tap("console")'
            )
        taps += args
    return [Request(i, tap=True) for i in dict.fromkeys(taps)]


@dataclass(frozen=True)
class BoardMarkers:
    """What the board markers of a test say: the `app_path` marker's arguments, the profile's
    name and the requests."""

    app_path: tuple[object, ...] | None
    profile: str
    requests: tuple[Request, ...]


# What pytest_generate_tests read for each test that uses `board`, by the nodeid of its
# definition: pytest_collection_modifyitems compares it with what each of its items carries.
READ = pytest.StashKey[dict[str, BoardMarkers]]()


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
    try:
        profile = board_profile(metafunc.definition)
        requests = board_requests(metafunc.definition)
        check(requests, profile, declared())
    except (MarkerError, ProfileError, CapabilityError) as error:
        # from None: raised inside the handler, the failure would show the error twice
        message = f"{metafunc.definition.nodeid}: {error}"
        raise pytest.fail.Exception(message, pytrace=False) from None
    read = BoardMarkers(marker.args if marker else None, profile.name, tuple(requests))
    metafunc.config.stash.setdefault(READ, {})[metafunc.definition.nodeid] = read
    chosen = metafunc.config.getoption("esp32tl_backend")
    backends = [chosen] if chosen else list(SERVICES)
    params = []
    for backend in backends:
        reason = refusal(backend, requests, declared())
        marks = [pytest.mark.skip(reason=f"{backend}: {reason}")] if reason else []
        params.append(
            pytest.param(
                backend, SERVICES[backend], str(app_path.resolve()), marks=marks, id=backend
            )
        )
    metafunc.parametrize(("board", "embedded_services", "app_path"), params, indirect=True)


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
    for item in items:
        late = late_board_marker(config, item)
        if late is not None:
            raise pytest.UsageError(
                f"{item.nodeid}: {late} reached the test after esp32tl read it, from a"
                " pytest.param or a hook: put it on the test, its class or its module"
            )
    if not timeout_configured(config):
        for item in items:
            if "board" in getattr(item, "fixturenames", ()) and not item.get_closest_marker(
                "timeout"
            ):
                item.add_marker(pytest.mark.timeout(DEFAULT_TIMEOUT))


def timeout_configured(config: pytest.Config) -> bool:
    """Whether the session sets pytest-timeout's timeout, 0 included: --timeout, then the
    PYTEST_TIMEOUT environment variable, then the `timeout` ini option, as pytest-timeout reads
    them. With pytest-timeout disabled (`-p no:timeout`) there is no timeout to give."""
    if not config.pluginmanager.has_plugin("timeout"):
        return True
    # pytest-timeout leaves it unannotated.
    settings = pytest_timeout.get_env_settings(config)  # type: ignore[no-untyped-call]
    return settings.timeout is not None


def late_board_marker(config: pytest.Config, item: pytest.Item) -> str | None:
    """Which board marker `item` carries otherwise than its definition did, if one does. A
    pytest.param's marks and those a hook adds reach the item but not the definition
    pytest_generate_tests reads, so a tap there would skip no backend, and a profile there would
    reach `board` unchecked."""
    callspec = getattr(item, "callspec", None)
    if callspec is None or "board" not in getattr(item, "fixturenames", ()):
        return None
    # The definition's nodeid is the item's without the parameters' id.
    read = config.stash.get(READ, {}).get(item.nodeid.removesuffix(f"[{callspec.id}]"))
    if read is None:
        return None
    # The definition's markers were read without an error, so an error here is a new marker's.
    app_path = item.get_closest_marker("app_path")
    if read.app_path != (app_path.args if app_path else None):
        return "the app_path marker"
    try:
        if board_profile(item).name != read.profile:
            return "the board_profile marker"
    except (MarkerError, ProfileError):
        return "the board_profile marker"
    try:
        if board_requests(item) != list(read.requests):
            return "the tap marker"
    except MarkerError:
        return "the tap marker"
    return None


# Set on a test whose call failed on RunEnded, waiting on a run that ended: its error names the
# exit code and the link's error already.
CALL_SAW_RUN_END = pytest.StashKey[bool]()
# Set on a test whose setup or call did not pass (failed, skipped, xfailed): a board it never
# powered on is then not reported on top of that.
DID_NOT_PASS = pytest.StashKey[bool]()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    if report.when in ("setup", "call") and not report.passed:
        item.stash[DID_NOT_PASS] = True
    if (
        report.when == "call"
        and call.excinfo is not None
        and ended_while_waiting(call.excinfo.value)
    ):
        item.stash[CALL_SAW_RUN_END] = True
    return report


@pytest.fixture
def board(
    request: pytest.FixtureRequest, dut: Dut, app_path: str, msg_queue: MessageQueue, _meta: Meta
) -> Iterator[Esp32Sim]:
    """The firmware in `app_path`, on the backend this test is parametrised with, on the board
    profile the test chose; off until the test calls `board.power_on()`."""
    assert request.param == Esp32Sim.name, request.param
    if not isinstance(dut, Dut):
        # `count` parametrised above 1 in the test: pytest-embedded made a dut per count.
        pytest.fail("count is above 1: esp32tl runs one board in a test", pytrace=False)
    try:
        backend = Esp32Sim(
            dut,
            app_path,
            msg_queue,
            _meta,
            board_profile(request.node),
            directory=request.path.parent,
            skip=skip_at_the_test,
        )
    except BackendError as error:
        backend_failed(error)
    try:
        yield backend
    finally:
        try:
            backend.close()
        except BackendError as error:
            # A test that waited on the run when it ended has reported it already, with its code
            # and what the link found.
            if not request.node.stash.get(CALL_SAW_RUN_END, False):
                backend_failed(error)
        else:
            # A test that set up a source and never powered on ran no firmware: it would pass
            # whatever the firmware does.
            set_up = backend.set_up()
            if set_up and not backend.powered and not request.node.stash.get(DID_NOT_PASS, False):
                pytest.fail(
                    f"{Esp32Sim.name}: the test set up {' and '.join(set_up)} and never called"
                    " board.power_on(): it ran no firmware",
                    pytrace=False,
                )
        finally:
            # The link's warnings and the run's stats, in the report of every test: a section
            # for people, which pytest shows with a failure or -rA, and a user property for code,
            # as a JSON string: JUnit XML writes a property's str().
            request.node.add_report_section("teardown", "esp32sim link", backend.link.report())
            request.node.user_properties.append(
                ("esp32sim_link", json.dumps(backend.link.properties()))
            )


def skip_at_the_test(reason: str) -> NoReturn:
    """Skips the test from its body, reported at the test, as a skip marker's skip is, rather
    than at the line of esp32tl's that raised it."""
    raise pytest.skip.Exception(reason, _use_item_location=True)


def backend_failed(error: BackendError) -> NoReturn:
    # from None: the message says it all, and the chained error would say it twice
    raise pytest.fail.Exception(f"{Esp32Sim.name}: {error}", pytrace=False) from None


@pytest.fixture
def console(board: Esp32Sim) -> Console:
    """The firmware's console, read on the board's clock: `console.expect` lets the board run
    until a pattern appears."""
    return Console(board)


@pytest.fixture
def gnss(board: Esp32Sim) -> Uart:
    """The far end of the board profile's `uart.gnss` route: `gnss.replay(path)`, before
    `board.power_on()`, replays a recording into it."""
    try:
        return board.uart("gnss")
    except BackendError as error:
        backend_failed(error)


@pytest.fixture
def sd_card(board: Esp32Sim) -> SdCard:
    """The board profile's SD card slot, empty until the test calls `sd_card.insert(image)`
    before `board.power_on()`."""
    try:
        return board.sd_card()
    except BackendError as error:
        backend_failed(error)
