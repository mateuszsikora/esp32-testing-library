"""The esp32tl pytest plugin, loaded through the `pytest11` entry point.

It provides the `board` and `console` fixtures on top of pytest-embedded's classes (plan §3);
`gnss`, `modem`, `bt`, `wifi`, `sd_card`, `lines` and `scenario` come with later milestones.
The board runs a board profile, `esp32-devkitc` unless the `board_profile` marker names another
(plan §4.5).

A test that uses `board` is parametrised over the backends by `pytest_generate_tests`, in one
`metafunc.parametrize` over `board`, `embedded_services` and `app_path`, so that the three always
travel together under one id. pytest-embedded's `--embedded-services`, `--app-path` and
`--build-dir` are refused when a test uses `board`: an empty `embedded_services` parameter falls
back to the command line, and a session started with `--embedded-services esp` would make the
`esp32sim` case open a serial port. The app comes from the `app_path` marker instead, its build
from `build/` in it. `--count` above 1 is refused too: a test has one board.

The `source` and `tap` markers say what a test puts at the far end of its board's interfaces and
which it taps. A backend that cannot give them gets its parameter marked `skip`, with the reason
from the capability declaration (`esp32tl.boards.capabilities`): the test is skipped before any
fixture is set up, so pytest-embedded never opens a serial port for it.
"""

import json
from collections.abc import Generator, Iterator
from dataclasses import dataclass
from typing import NoReturn

import pytest
from pytest_embedded.dut import Dut
from pytest_embedded.log import MessageQueue
from pytest_embedded.utils import Meta

from esp32tl import boards
from esp32tl.backends import SERVICES
from esp32tl.backends.esp32sim import BackendError, Esp32Sim, ended_on_runner_exit
from esp32tl.boards import DEFAULT_PROFILE, BoardProfile, ProfileError
from esp32tl.boards.capabilities import CapabilityError, Request, check, declared, refusal
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
    config.addinivalue_line(
        "markers",
        f"board_profile(name): the board profile a test runs; {DEFAULT_PROFILE} by default",
    )
    config.addinivalue_line(
        "markers",
        "source(interface, source): what the test puts at the far end of a board interface, as in"
        ' source("uart.gnss", "replay"); a backend that cannot gives a skip',
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


def source_markers(node: pytest.Item) -> Iterator[tuple[str, object, pytest.Mark]]:
    """The `source` markers of `node` and its parents, closest first, each with the name and the
    identity of the level it is on. pytest gives a class the markers of its bases too, bases
    first; here each class of the MRO is a level of its own, the class before its bases, so that
    a subclass overrides its base. A marker a hook added to the class's node is in no class's
    own marks, and counts as the class's."""
    for level in reversed(node.listchain()):
        if isinstance(level, pytest.Class):
            mro = [(cls, class_marks(cls)) for cls in level.obj.__mro__]
            known = [mark for _, marks in mro for mark in marks]
            added = [mark for mark in level.own_markers if mark not in known]
            mro[0] = (level.obj, added + mro[0][1])
            for cls, marks in mro:
                for mark in marks:
                    if mark.name == "source":
                        yield cls.__name__, cls, mark
        else:
            for mark in level.own_markers:
                if mark.name == "source":
                    yield level.name, level, mark


def class_marks(cls: type) -> list[pytest.Mark]:
    """The marks `cls` itself carries, without its bases': decorators, or a `pytestmark`
    attribute of one mark or a list of them."""
    marks = cls.__dict__.get("pytestmark", [])
    if not isinstance(marks, list):
        marks = [marks]
    found = [getattr(m, "mark", m) for m in marks]
    return [m for m in found if isinstance(m, pytest.Mark)]


def board_requests(node: pytest.Item) -> list[Request]:
    """The sources and taps the `source` and `tap` markers of `node` and its parents ask for.

    For each interface the closest `source` marker wins, as the closest `app_path` does: one on a
    test overrides its class's, a subclass's overrides its base's, a class's its module's. Two on
    the same test, class or module are an error, since neither is closer. Taps add up over every
    level."""
    sources: dict[str, str] = {}
    levels: dict[str, object] = {}
    for name, level, marker in source_markers(node):
        args = marker.args
        if len(args) != 2 or marker.kwargs or not all(isinstance(a, str) for a in args):
            raise MarkerError(
                "the source marker takes an interface and a source,"
                ' as in @pytest.mark.source("uart.gnss", "replay")'
            )
        if args[0] not in sources:
            sources[args[0]] = args[1]
            levels[args[0]] = level
        elif levels[args[0]] is level:
            raise MarkerError(f"two source markers on {name} name {args[0]}")
    taps: list[str] = []
    for marker in node.iter_markers("tap"):
        args = marker.args
        if not args or marker.kwargs or not all(isinstance(a, str) for a in args):
            raise MarkerError(
                'the tap marker takes one or more interfaces, as in @pytest.mark.tap("console")'
            )
        taps += args
    return [Request(i, source=s) for i, s in sources.items()] + [
        Request(i, tap=True) for i in dict.fromkeys(taps)
    ]


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


def late_board_marker(config: pytest.Config, item: pytest.Item) -> str | None:
    """Which board marker `item` carries otherwise than its definition did, if one does. A
    pytest.param's marks and those a hook adds reach the item but not the definition
    pytest_generate_tests reads, so a source there would skip no backend, and a profile there
    would reach `board` unchecked."""
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
        requests = board_requests(item)
    except MarkerError:
        return "a source or tap marker"
    if [r for r in requests if r.source is not None] != [
        r for r in read.requests if r.source is not None
    ]:
        return "the source marker"
    if [r for r in requests if r.tap] != [r for r in read.requests if r.tap]:
        return "the tap marker"
    return None


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
    """The firmware in `app_path`, running on the backend this test is parametrised with, on
    the board profile the test chose."""
    assert request.param == Esp32Sim.name, request.param
    if not isinstance(dut, Dut):
        # `count` parametrised above 1 in the test: pytest-embedded made a dut per count.
        pytest.fail("count is above 1: esp32tl runs one board in a test", pytrace=False)
    try:
        backend = Esp32Sim(dut, app_path, msg_queue, _meta, board_profile(request.node))
    except BackendError as error:
        backend_failed(error)
    try:
        yield backend
    finally:
        try:
            backend.close()
        except BackendError as error:
            # An expect that ended on the runner's exit has already reported it, with its code
            # and what the link found.
            if not request.node.stash.get(EXPECT_SAW_RUNNER_EXIT, False):
                backend_failed(error)
        finally:
            # The link's warnings and the run's stats, in the report of every test: a section
            # for people, which pytest shows with a failure or -rA, and a user property for code,
            # as a JSON string: JUnit XML writes a property's str().
            request.node.add_report_section("teardown", "esp32sim link", backend.link.report())
            request.node.user_properties.append(
                ("esp32sim_link", json.dumps(backend.link.properties()))
            )


def backend_failed(error: BackendError) -> NoReturn:
    # from None: the message says it all, and the chained error would say it twice
    raise pytest.fail.Exception(f"{Esp32Sim.name}: {error}", pytrace=False) from None


@pytest.fixture
def console(board: Esp32Sim) -> Console:
    """The firmware's console; `console.expect` is `dut.expect`."""
    return Console(board.dut)
