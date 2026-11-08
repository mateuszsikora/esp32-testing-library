"""The esp32tl pytest plugin, loaded through the `pytest11` entry point.

It provides the `board`, `console`, `gnss`, `modem`, `broker`, `lines`, `sd_card`, `card_cache`,
`wifi` and `bt` fixtures on top of pytest-embedded's classes (plan §3).
The board runs a board profile, `esp32-devkitc` unless the `board_profile` marker names another
(plan §4.5).

A test that uses `board` is parametrised over the backends by `pytest_generate_tests`, in one
`metafunc.parametrize` over `board`, `embedded_services`, `app_path` and `build_dir`, so they always
travel together under one id. pytest-embedded's `--embedded-services`, `--app-path` and
`--build-dir` are refused when a test uses `board`: an empty `embedded_services` parameter falls
back to the command line, and a session started with `--embedded-services esp` would make the
`esp32sim` case open a serial port. The app comes from the `app_path` marker instead, its build
from its `build_dir` keyword (relative to the app), or `build/` in it by default.
`--count` above 1 is refused too: a test has one board.

The test body sets up what is around the board, as `gnss.replay(path)`, `gnss.fix(at=...)` and
`gnss.tap(path)` do, then calls `board.power_on()`, which starts the run; from there the board's
clock moves only while the test waits on it (`board.wait`, `board.wait_until`, `console.expect`)
on esp32sim. Serial is opt-in, uses esp,idf and runs continuously on wall time; power_on resets it.
A source or a tap that the backend's capability declaration (`esp32tl.boards.capabilities`) does not
give skips the test at the call that asks for it.

The report gets the `esp32sim link` or `serial console` section for its backend, with the run
on the board's clock after it when the test failed.

Every test that uses `board` gets pytest-timeout's `timeout` marker of `DEFAULT_TIMEOUT` seconds of
wall time, unless the session or the test sets a timeout of its own: the runner waits for the test
for as long as the body runs, so this is what ends a run that hangs.

Before each test, the TLS certificate authorities are numbered from 0 again, skipping the numbers of
CAs still held, so that a test's own CAs get the same keys alone as in the suite
(`esp32tl.net.tls`).
"""

import json
import os
from collections.abc import Generator, Iterator
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import NoReturn

import pytest
import pytest_timeout
from pytest_embedded.dut import Dut
from pytest_embedded.log import MessageQueue
from pytest_embedded.utils import Meta
from pytest_embedded_idf.dut import IdfDut

from esp32tl import boards
from esp32tl.backends import SERVICES
from esp32tl.backends.esp32sim import BackendError, Esp32Sim, ended_while_waiting
from esp32tl.backends.serial import Serial, check_artifact
from esp32tl.boards import DEFAULT_PROFILE, BoardProfile, ProfileError
from esp32tl.interfaces.bluetooth import Bluetooth
from esp32tl.interfaces.board import Board as Board
from esp32tl.interfaces.card_cache import CARD_CACHE_MIB, CardCache
from esp32tl.interfaces.console import Console
from esp32tl.interfaces.gnss import Gnss
from esp32tl.interfaces.lines import Lines
from esp32tl.interfaces.modem import Modem
from esp32tl.interfaces.sd_card import SdCard
from esp32tl.interfaces.wifi import Wifi
from esp32tl.net.mqtt import Broker
from esp32tl.net.tls import number_authorities_from_zero

# The wall-time timeout of a test that uses `board`, in seconds, unless one is configured: 600 s
# covers at least three minutes of the board's busiest work, at the 0.3 times real time stage 0
# measured for CPU-bound code (docs/stage0-findings.md), and far more of an idle board.
DEFAULT_TIMEOUT = 600

CARD_CACHE_LIMIT = "esp32tl_card_cache_mib"
# The board of the test that is running, for the session's `card_cache`.
RUNNING_BOARD = pytest.StashKey[Board]()


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("esp32tl")
    group.addoption(
        "--esp32tl-backend",
        choices=list(SERVICES),
        help="run board tests on this backend (default: esp32sim; serial is opt-in)",
    )
    parser.addini(
        CARD_CACHE_LIMIT,
        "the most disk the card_cache fixture's cards take, in MiB",
        default=str(CARD_CACHE_MIB),
    )


def card_cache_limit(config: pytest.Config) -> int:
    """`esp32tl_card_cache_mib`, in bytes."""
    value = str(config.getini(CARD_CACHE_LIMIT)).strip()
    if not value.isdecimal() or int(value) < 1:
        raise pytest.UsageError(f"{CARD_CACHE_LIMIT} must be a whole number of MiB, not {value!r}")
    return int(value) << 20


def pytest_configure(config: pytest.Config) -> None:
    card_cache_limit(config)
    config.addinivalue_line(
        "markers",
        "app_path(path, build_dir='build'): the ESP-IDF app, relative to the test file's"
        " directory (the default app); build_dir is relative to the app or absolute",
    )
    config.addinivalue_line(
        "markers",
        "app_recording(path): on serial, require the recording app_sha256 to match the artifact",
    )
    config.addinivalue_line(
        "markers",
        "board_profile(name) or board_profile(path=...): a bundled name or a local profile path"
        f" relative to the test file's directory; {DEFAULT_PROFILE} by default",
    )


class MarkerError(Exception):
    """A marker of esp32tl's used another way than its registration says."""


def profile_reference(node: pytest.Item) -> str | Path:
    """A bundled name or an absolute local path, always relative to the test file."""
    marker = node.get_closest_marker("board_profile")
    if marker is None:
        return DEFAULT_PROFILE
    if len(marker.args) == 1 and not marker.kwargs and isinstance(marker.args[0], str):
        return marker.args[0]
    if not marker.args and marker.kwargs.keys() == {"path"}:
        path = marker.kwargs["path"]
        # Path("") is Path("."), which str() turns into a non-empty "."
        if isinstance(path, str | Path) and str(path).strip() and Path(path) != Path():
            try:
                return (node.path.parent / path).resolve()
            except (OSError, RuntimeError, ValueError) as error:
                raise MarkerError(f"cannot resolve board_profile path {path!r}: {error}") from None
    raise MarkerError(
        "the board_profile marker takes one bundled name or path= (str or pathlib.Path),"
        f' as in @pytest.mark.board_profile("{DEFAULT_PROFILE}") or'
        ' @pytest.mark.board_profile(path="boards/test-board.toml"); do not combine them'
    )


# Local profiles are snapshots for this pytest session, keyed by their full path, not name.
LOCAL_PROFILES = pytest.StashKey[dict[Path, BoardProfile]]()


def load_profile(config: pytest.Config, reference: str | Path) -> BoardProfile:
    """Load a bundled profile by name, or a local one once per session by its full path."""
    if isinstance(reference, str):
        try:
            return boards.profile(reference)
        except ProfileError as error:
            if "/" in reference or reference.endswith(".toml"):
                raise ProfileError(
                    f'{error}; for a local file, use board_profile(path="{reference}")'
                ) from None
            raise
    profiles = config.stash.setdefault(LOCAL_PROFILES, {})
    if reference not in profiles:
        try:
            profiles[reference] = boards.load(reference)
        except ProfileError as error:
            raise ProfileError(f"{reference}: {error}") from None
    return profiles[reference]


@dataclass(frozen=True)
class BoardMarkers:
    """The markers read at collection, with the selected profile's reference and snapshot."""

    app_path: tuple[object, ...] | None
    app_kwargs: dict[str, object]
    profile: str | Path
    app_recording: tuple[object, ...] | None
    loaded_profile: BoardProfile


# What pytest_generate_tests read for each test that uses `board`, by the nodeid of its
# definition: pytest_collection_modifyitems compares it with what each of its items carries.
READ = pytest.StashKey[dict[str, BoardMarkers]]()


def collected_board_markers(config: pytest.Config, node: pytest.Item) -> BoardMarkers | None:
    """Find the collection snapshot for a definition or one of its parametrised items."""
    callspec = getattr(node, "callspec", None)
    nodeid = node.nodeid.removesuffix(f"[{callspec.id}]") if callspec is not None else node.nodeid
    return config.stash.get(READ, {}).get(nodeid)


def board_profile(node: pytest.Item) -> BoardProfile:
    """The profile pytest_generate_tests validated for this test, as it was at collection."""
    read = collected_board_markers(node.config, node)
    # `board` gets its parameter only from pytest_generate_tests, which saves READ first.
    assert read is not None, node.nodeid
    return read.loaded_profile


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "board" not in metafunc.fixturenames:
        return
    app_path = metafunc.definition.path.parent
    build_dir: str | Path = "build"
    marker = metafunc.definition.get_closest_marker("app_path")
    if marker is not None:
        if (
            len(marker.args) != 1
            or not isinstance(marker.args[0], str | Path)
            or marker.kwargs.keys() - {"build_dir"}
            or not isinstance(marker.kwargs.get("build_dir", "build"), str | Path)
            or marker.kwargs.get("build_dir") == ""
        ):
            pytest.fail(
                f"{metafunc.definition.nodeid}: the app_path marker takes one path,"
                " and optionally build_dir as a non-empty path,"
                ' as in @pytest.mark.app_path("../firmware/hello_world", build_dir="build")',
                pytrace=False,
            )
        app_path /= marker.args[0]
        build_dir = marker.kwargs.get("build_dir", "build")
    try:
        profile = profile_reference(metafunc.definition)
        loaded_profile = load_profile(metafunc.config, profile)
    except (MarkerError, ProfileError) as error:
        # from None: raised inside the handler, the failure would show the error twice
        message = f"{metafunc.definition.nodeid}: {error}"
        raise pytest.fail.Exception(message, pytrace=False) from None
    reference = metafunc.definition.get_closest_marker("app_recording")
    if reference is not None and (
        len(reference.args) != 1
        or reference.kwargs
        or not isinstance(reference.args[0], str | Path)
    ):
        pytest.fail(
            f"{metafunc.definition.nodeid}: the app_recording marker takes one path,"
            ' as in @pytest.mark.app_recording("hello-console.jsonl")',
            pytrace=False,
        )
    read = BoardMarkers(
        app_path=marker.args if marker else None,
        app_kwargs=dict(marker.kwargs) if marker else {},
        profile=profile,
        app_recording=reference.args if reference else None,
        loaded_profile=loaded_profile,
    )
    metafunc.config.stash.setdefault(READ, {})[metafunc.definition.nodeid] = read
    chosen = metafunc.config.getoption("esp32tl_backend")
    backends = [chosen] if chosen else [Esp32Sim.name]
    params = [
        pytest.param(
            backend,
            SERVICES[backend],
            str(app_path.resolve()),
            str((app_path / build_dir).resolve()),
            id=backend,
        )
        for backend in backends
    ]
    metafunc.parametrize(
        ("board", "embedded_services", "app_path", "build_dir"), params, indirect=True
    )


PORT_REQUIRED = (
    "serial requires an explicit --port (or a parametrised port) without wildcards:"
    " automatic port discovery is disabled"
)
PORT_CONFLICT = "serial requires ESPPORT to be unset, empty or equal to the explicit port"
CACHED_PORT_CONFLICT = "serial requires pytest-embedded's cached port to match the explicit port"
FLASH_PORT_UNSUPPORTED = (
    "serial does not support --flash-port: use one port for flashing and console"
)


def explicit_port(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and "*" not in value


def serial_port_error(port: object, flash_port: object, override: object) -> str | None:
    if not explicit_port(port):
        return PORT_REQUIRED
    if override and override != port:
        return PORT_CONFLICT
    if flash_port is not None:
        return FLASH_PORT_UNSUPPORTED
    return None


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
                " marker, the build through that marker's build_dir keyword or its build/"
                " directory)"
            )
    if config.getoption("count", 1) != 1:
        raise pytest.UsageError("--count is set: esp32tl runs one board in a test")
    for item in items:
        callspec = getattr(item, "callspec", None)
        if callspec is not None and callspec.params.get("board") == Serial.name:
            port = callspec.params.get("port") or config.getoption("port", None)
            flash_port = callspec.params.get("flash_port") or config.getoption("flash_port", None)
            error = serial_port_error(port, flash_port, os.environ.get("ESPPORT"))
            if error:
                raise pytest.UsageError(f"{item.nodeid}: {error}")
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
    pytest_generate_tests reads, so a profile there would reach `board` unchecked."""
    callspec = getattr(item, "callspec", None)
    if callspec is None or "board" not in getattr(item, "fixturenames", ()):
        return None
    read = collected_board_markers(config, item)
    if read is None:
        return None
    reference = item.get_closest_marker("app_recording")
    if read.app_recording != (reference.args if reference else None):
        return "the app_recording marker"
    # The definition's markers were read without an error, so an error here is a new marker's.
    app_path = item.get_closest_marker("app_path")
    if read.app_path != (app_path.args if app_path else None) or read.app_kwargs != (
        app_path.kwargs if app_path else {}
    ):
        return "the app_path marker"
    try:
        if profile_reference(item) != read.profile:
            return "the board_profile marker"
    except MarkerError:
        return "the board_profile marker"
    return None


# Set on a test whose call failed on RunEnded, waiting on a run that ended: its error names the
# exit code and the link's error already.
CALL_SAW_RUN_END = pytest.StashKey[bool]()
# Set on a test whose setup or call did not pass (failed, skipped, xfailed): a board it never
# powered on is then not reported on top of that.
DID_NOT_PASS = pytest.StashKey[bool]()
# Set on a test whose setup or call failed: its report shows the run on the board's clock.
FAILED = pytest.StashKey[bool]()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    if report.when in ("setup", "call") and not report.passed:
        item.stash[DID_NOT_PASS] = True
    if report.when in ("setup", "call") and report.failed:
        item.stash[FAILED] = True
    if (
        report.when == "call"
        and call.excinfo is not None
        and ended_while_waiting(call.excinfo.value)
    ):
        item.stash[CALL_SAW_RUN_END] = True
    return report


# Cached before pytest-embedded's app fixture, which serial/dut depend on: a test may also
# request dut directly, in which case checking only inside board would be too late.
SERIAL_ARTIFACT = pytest.StashKey[str]()


def serial_artifact(request: pytest.FixtureRequest) -> str:
    node: pytest.Item = request.node
    callspec = getattr(node, "callspec", None)
    if callspec is not None and int(callspec.params.get("count", 1)) != 1:
        pytest.fail("count is above 1: esp32tl runs one board in a test", pytrace=False)
    port = request.getfixturevalue("port")
    port_error = serial_port_error(
        port, request.getfixturevalue("flash_port"), os.environ.get("ESPPORT")
    )
    if port_error:
        pytest.fail(port_error, pytrace=False)
    # pytest-embedded snapshots ESPPORT into these kwargs before app setup. Check that
    # snapshot too: a later fixture may have restored the environment in the meantime.
    options = request.getfixturevalue("_fixture_classes_and_options").kwargs.get("serial")
    if options is None:
        backend_failed(
            BackendError("the serial backend requires pytest-embedded's esp,idf services"),
            Serial.name,
        )
    if options.get("port") != port:
        pytest.fail(CACHED_PORT_CONFLICT, pytrace=False)
    port_error = serial_port_error(port, options.get("flash_port"), None)
    if port_error:
        pytest.fail(port_error, pytrace=False)
    if SERIAL_ARTIFACT not in node.stash:
        reference = request.node.get_closest_marker("app_recording")
        path = request.path.parent / reference.args[0] if reference else None
        try:
            node.stash[SERIAL_ARTIFACT] = check_artifact(
                request.getfixturevalue("app_path"),
                path,
                build_dir=request.getfixturevalue("build_dir"),
            )
        except BackendError as error:
            backend_failed(error, Serial.name)
    return node.stash[SERIAL_ARTIFACT]


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    number_authorities_from_zero()


@pytest.hookimpl(tryfirst=True)
def pytest_fixture_setup(
    fixturedef: pytest.FixtureDef[object], request: pytest.FixtureRequest
) -> None:
    callspec = getattr(request.node, "callspec", None)
    if (
        fixturedef.argname == "app"
        and callspec is not None
        and callspec.params.get("board") == Serial.name
    ):
        serial_artifact(request)


@pytest.fixture
def board(
    request: pytest.FixtureRequest,
    app_path: str,
    build_dir: str,
    msg_queue: MessageQueue,
    _meta: Meta,
) -> Iterator[Board]:
    """The firmware in `app_path`, on the backend this test is parametrised with, on the board
    profile the test chose; off until the test calls `board.power_on()`."""
    name = request.param
    assert name in SERVICES, name
    digest = serial_artifact(request) if name == Serial.name else ""
    # Resolve only after serial's artifact/recording check: dut opens and may flash the port.
    dut = request.getfixturevalue("dut")
    if not isinstance(dut, Dut):
        # `count` parametrised above 1 in the test: pytest-embedded made a dut per count.
        pytest.fail("count is above 1: esp32tl runs one board in a test", pytrace=False)
    profile = board_profile(request.node)
    try:
        backend: Esp32Sim | Serial
        if name == Serial.name:
            if not isinstance(dut, IdfDut):
                raise BackendError("the serial backend requires pytest-embedded's esp,idf services")
            reference = request.node.get_closest_marker("app_recording")
            backend = Serial(
                dut,
                profile,
                msg_queue,
                app_sha256=digest,
                directory=request.path.parent,
                reference=request.path.parent / reference.args[0] if reference else None,
                skip=skip_at_the_test,
            )
        else:
            backend = Esp32Sim(
                dut,
                app_path,
                msg_queue,
                _meta,
                profile,
                directory=request.path.parent,
                skip=skip_at_the_test,
                build_dir=build_dir,
            )
    except BackendError as error:
        backend_failed(error, name)
    # Whether the teardown fails the test: its report then shows the run, as for a failed call.
    failing = True
    request.config.stash[RUNNING_BOARD] = backend
    try:
        yield backend
    finally:
        del request.config.stash[RUNNING_BOARD]
        try:
            backend.close()
        except BackendError as error:
            # A test that waited on the run when it ended has reported it already, with its code
            # and what the link found.
            if not request.node.stash.get(CALL_SAW_RUN_END, False):
                backend_failed(error, name)
            failing = False
        else:
            # A test that set up a source and never powered on ran no firmware: it would pass
            # whatever the firmware does.
            set_up = backend.set_up()
            if set_up and not backend.started and not request.node.stash.get(DID_NOT_PASS, False):
                pytest.fail(
                    f"{name}: the test set up {' and '.join(set_up)} and never called"
                    " board.power_on(): it ran no firmware",
                    pytrace=False,
                )
            failing = False
        finally:
            # The link's warnings, the run's stats and the bytes on each route, in the report of
            # every test, and the run on the board's clock in that of a test that failed: a
            # section for people, which pytest shows with a failure or -rA, and a user property
            # for code, as a JSON string: JUnit XML writes a property's str().
            run = failing or request.node.stash.get(FAILED, False)
            request.node.add_report_section(
                "teardown", backend.report_section, backend.report(run=run)
            )
            request.node.user_properties.append(
                (
                    backend.report_property,
                    json.dumps(backend.properties()),
                )
            )


def skip_at_the_test(reason: str) -> NoReturn:
    """Skips the test from its body, reported at the test, as a skip marker's skip is, rather
    than at the line of esp32tl's that raised it."""
    raise pytest.skip.Exception(reason, _use_item_location=True)


def backend_failed(error: BackendError, name: str) -> NoReturn:
    # from None: the message says it all, and the chained error would say it twice
    raise pytest.fail.Exception(f"{name}: {error}", pytrace=False) from None


@pytest.fixture
def console(board: Board) -> Console:
    """The firmware's console, read on the board's clock: `console.expect` lets the board run
    until a pattern appears."""
    return board.console


@pytest.fixture
def gnss(board: Board) -> Gnss:
    """The far end of the board profile's `uart.gnss` route: `gnss.replay(path)`, before
    `board.power_on()`, replays a recording into it; `gnss.fix(at=...)` or another call of the
    receiver model makes it a receiver the test describes; and `gnss.tap(path)` records what
    crosses it."""
    try:
        return board.gnss
    except BackendError as error:
        backend_failed(error, board.name)


@pytest.fixture
def modem(board: Board) -> Modem:
    """The profile's uart.modem route; select its model or replay before power-on."""
    try:
        return board.modem
    except BackendError as error:
        backend_failed(error, board.name)


@pytest.fixture
def lines(board: Board) -> Lines:
    """The board profile's GPIO lines, `[lines.<role>]`: `lines.drive(line, level)` and
    `lines.release(line)` at the board's current instant, `lines.level(line)` what the firmware
    drives, `lines.events()` every change with its time."""
    try:
        return board.lines
    except BackendError as error:
        backend_failed(error, board.name)


@pytest.fixture
def sd_card(board: Board) -> SdCard:
    """The board profile's SD card slot, empty until the test calls `sd_card.insert(image)`:
    before `board.power_on()` the card is in from power-on; after it, `sd_card.remove()` and
    `sd_card.insert(image)` take the card out and put one in at the board's current instant."""
    try:
        return board.sd_card
    except BackendError as error:
        backend_failed(error, board.name)


@pytest.fixture(scope="session")
def card_cache(request: pytest.FixtureRequest) -> Iterator[CardCache]:
    """Prepared SD cards, kept between tests and sessions for the running test's application:
    `card_cache.get(name)` is a clone of one for the test to insert, or `None`, and
    `card_cache.put(name, image)` keeps a copy of the image, with the board off."""
    config = request.config
    limit = card_cache_limit(config)

    def running() -> Board | None:
        return config.stash.get(RUNNING_BOARD, None)

    cache = getattr(config, "cache", None)
    if cache is not None:
        yield CardCache(cache.mkdir("esp32tl-card-cache"), limit, running)
        return
    # -p no:cacheprovider: nothing outlives the session.
    with TemporaryDirectory(prefix="esp32tl-card-cache-") as directory:
        yield CardCache(Path(directory), limit, running)


@pytest.fixture
def broker(modem: Modem) -> Broker:
    """An MQTT 3.1.1 broker at port 1883 behind the selected modem model."""
    return modem.network().broker


@pytest.fixture
def wifi(board: Board) -> Wifi:
    """The chip's Wi-Fi station: `wifi.access_point(ssid, password=...)`, before
    `board.power_on()`, puts an access point on the air and returns the simulated network behind
    it; `wifi.access_point_off()` and `wifi.access_point_on()` switch it at the board's current
    instant."""
    return board.wifi


@pytest.fixture
def bt(board: Board) -> Bluetooth:
    """The chip's Bluetooth: its controller on `bt.air`, on the board's clock, where the test puts
    the devices around the board, as `SppPeer(bt.air, ...)` and `BluetoothDevice(bt.air, ...)`,
    and switches them at the board's current instant. The run forwards the firmware's HCI to it."""
    try:
        return board.bluetooth
    except BackendError as error:
        backend_failed(error, board.name)
