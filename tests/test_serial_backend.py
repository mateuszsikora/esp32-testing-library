"""Serial's real pytest-embedded fixture chain, with an in-memory port instead of hardware."""

import json
from pathlib import Path
from textwrap import dedent
from xml.etree import ElementTree

import pytest
from test_pytest_plugin import SHOW_PARAMS, fake_build

from esp32tl import boards, recording
from esp32tl.backends.esp32sim import BackendError
from esp32tl.backends.serial import check_artifact
from esp32tl.tap import Tap, sha256


@pytest.fixture(autouse=True)
def clear_hardware_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ESPPORT", raising=False)


# Keep IdfSerial._start itself: it must choose flash or reset from skip_autoflash. Replace the
# hardware constructor/flash/reset only, leaving pytest-embedded's app, serial and dut fixtures.
FAKE_SERIAL = """
from pathlib import Path
import pytest
import serial
import pytest_embedded_idf
from pytest_embedded_idf.serial import IdfSerial
from pytest_embedded_serial.serial import Serial as EmbeddedSerial

class FakeIdfSerial(IdfSerial):
    def __init__(self, app, msg_queue, port=None, skip_autoflash=False, **kwargs):
        Path("port-opened").touch()
        assert port == "test-port"
        self.app = app
        self.skip_autoflash = skip_autoflash
        self.flashes = 0
        self.resets = 0
        self.closed_by_owner = False
        EmbeddedSerial.__init__(self, msg_queue, port=serial.serial_for_url("loop://"))

    def _post_init(self):
        pass

    def flash(self):
        self.flashes += 1
        self.hard_reset()

    def hard_reset(self):
        self.resets += 1
        self.proc.write(b"boot number " + str(self.resets).encode() + b"\\r\\nHello world!\\r\\n")

    def close(self):
        self.closed_by_owner = True
        super().close()

@pytest.fixture(autouse=True)
def fake_serial(monkeypatch):
    monkeypatch.setattr(pytest_embedded_idf, "IdfSerial", FakeIdfSerial)
"""


def run_serial(pytester: pytest.Pytester, *args: str) -> pytest.RunResult:
    return pytester.runpytest_subprocess(
        "-p", "no:cacheprovider", "--esp32tl-backend", "serial", "--port", "test-port", *args
    )


def test_serial_is_opt_in_and_selects_esp_idf(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(SHOW_PARAMS)
    pytester.makepyfile("""
        def test_board(board, request):
            chosen = request.config.getoption("esp32tl_backend") or "esp32sim"
            assert board[0] == chosen
            assert board[1] == ("esp,idf" if chosen == "serial" else None)
            assert request.node.name == "test_board[" + chosen + "]"
    """)
    pytester.runpytest_subprocess("-q").assert_outcomes(passed=1)
    run_serial(pytester, "-q").assert_outcomes(passed=1)


@pytest.mark.parametrize("skip_autoflash", [False, True])
@pytest.mark.parametrize("local", [False, True])
def test_console_reset_clock_tap_and_autoflash(
    pytester: pytest.Pytester, skip_autoflash: bool, local: bool
) -> None:
    fake_build(pytester.path)
    pytester.makeconftest(FAKE_SERIAL)
    profile = "local" if local else "esp32-devkitc"
    marker = '@pytest.mark.board_profile(path="local.toml")' if local else ""
    if local:
        (pytester.path / "local.toml").write_text('name = "local"\nchip = "esp32"')
    reference = Tap("console", pytester.path / "reference.jsonl")
    recording.write(
        reference.path,
        reference.recording(
            board="esp32-devkitc",
            source="real",
            clock="wall",
            app_sha256=sha256(pytester.path / "build/app.bin"),
        ),
    )
    pytester.makepyfile(f"""
        import datetime
        import time
        import pytest
        import esp32tl
        from esp32tl.interfaces import BoardOff, NeverHappened, SourceError
        from esp32tl.units import seconds

        @pytest.mark.app_recording("reference.jsonl")
        {marker}
        def test_console(board, console, tmp_path):
            from esp32tl import Board
            assert isinstance(board, Board)
            assert board.console is console
            assert board.profile.name == {profile!r}
            assert board.name == "serial" and board.clock == "wall"
            assert board.dut.serial.flashes == {int(not skip_autoflash)}
            assert board.dut.serial.resets == 1
            with pytest.raises(BoardOff):
                console.expect("Hello", timeout=0)
            with pytest.raises(BoardOff, match="call board.power_on"):
                board.utc_at(seconds(0))
            with pytest.raises(ValueError, match="cannot set"):
                board.power_on(utc="not a clock to set")
            with pytest.raises(SourceError, match="recording of app_recording"):
                console.tap("reference.jsonl")
            with pytest.raises(SourceError, match="artifact of app"):
                console.tap("build/app.bin")
            tap = console.tap("console.jsonl")
            board.power_on()
            console.expect_exact("boot number 2")
            console.expect("Hello world!")
            assert b"boot number 1" not in board.console_output
            assert board.dut.serial.resets == 2
            board.reset()
            console.expect_exact("boot number 3")
            assert board.dut.serial.resets == 3
            assert board.dut.serial.flashes == {int(not skip_autoflash)}
            before = board.now
            time.sleep(.02)
            assert board.now > before
            before = board.now
            board.wait(seconds(.02))
            assert board.now >= before + seconds(.02)
            assert console.lines("Hello")[0].at <= board.now
            start = board.utc_at(seconds(0))
            assert board.utc_at(seconds(1.5)) - start == datetime.timedelta(seconds=1.5)
            assert start <= board.utc <= board.utc_at(board.now + seconds(1))
            condition = console.line("next")
            console.sendline("next line")
            assert board.wait_until(condition, within=seconds(1)).text == "next line"
            described = esp32tl.condition(lambda: None, "a trip/end from the device")
            with pytest.raises(TypeError, match="call the condition 'a trip/end"):
                bool(described)
            with pytest.raises(
                NeverHappened, match="^a trip/end from the device did not happen within 0.01 s:"
            ):
                board.wait_until(described, within=seconds(.01))
            with pytest.raises(TypeError, match="call the condition 'a trip/end"):
                bool(described)
            said = esp32tl.condition(lambda: None, "an answer", waiting=lambda: "nothing asked")
            with pytest.raises(
                NeverHappened,
                match="^an answer did not happen within 0.01 s: nothing asked; waited from ",
            ):
                board.wait_until(said, within=seconds(.01))
            with pytest.raises(NeverHappened):
                console.expect("missing", timeout=.01)
            with pytest.raises(BoardOff):
                board.power_on()
            with pytest.raises(SourceError):
                console.tap("late.jsonl")
            assert tap.events
    """)
    args = ("--skip-autoflash", "y") if skip_autoflash else ()
    run_serial(pytester, *args).assert_outcomes(passed=1)
    made = recording.read(pytester.path / "console.jsonl")
    assert made.header.board == profile
    assert made.header.clock == "wall"
    assert made.header.interface == "console"
    assert made.header.source == "real"
    assert made.header.app_sha256 == sha256(pytester.path / "build/app.bin")
    assert b"Hello world!" in b"".join(event.data for event in made.events)
    assert [e.data for e in made.events if e.dir == "in"] == [b"next line\n"]


def test_the_serial_report_names_the_build_the_test_loaded(pytester: pytest.Pytester) -> None:
    fake_build(pytester.path)
    pytester.makeconftest(FAKE_SERIAL)
    pytester.makepyfile("""
        def test_console(board, console):
            board.power_on()
            console.expect("Hello world!")
    """)
    digest = sha256(pytester.path / "build/app.bin")
    result = run_serial(pytester, "-rA", "--junitxml", "report.xml")
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(
        [
            "*Captured serial console teardown*",
            f"build: {pytester.path / 'build'} (ESP-IDF for esp32); app_sha256 {digest}",
            "serial: wall clock; * console bytes",
        ]
    )
    (prop,) = ElementTree.parse(pytester.path / "report.xml").iter("property")
    assert prop.get("name") == "serial_console"
    properties = json.loads(prop.get("value", ""))
    assert "app_sha256" not in properties
    assert properties["build"] == {
        "directory": str(pytester.path / "build"),
        "kind": "esp-idf",
        "chip": "esp32",
        "app_sha256": digest,
    }


@pytest.mark.parametrize("local", [False, True])
def test_serial_capabilities_skip_at_the_call(pytester: pytest.Pytester, local: bool) -> None:
    fake_build(pytester.path)
    pytester.makeconftest(FAKE_SERIAL)
    marker = ""
    if local:
        (pytester.path / "local.toml").write_text(
            (boards.PROFILES / "esp32-devkitc.toml").read_text().replace("esp32-devkitc", "local")
        )
        marker = 'import pytest\npytestmark = pytest.mark.board_profile(path="local.toml")\n'
    pytester.makepyfile(
        marker
        + dedent("""
        from datetime import UTC, datetime

        import pytest
        from esp32tl.units import Position, seconds
        def test_model(gnss):
            gnss.fix(at=Position(0, 0))
        def test_replay(gnss):
            gnss.replay("not-read.jsonl")
        def test_tap(gnss):
            gnss.tap("not-written.jsonl")
        def test_real_port(gnss):
            gnss.real("/dev/not-opened")
        def test_card_builder(board):
            board.sd_card.image().build()
        def test_card_cache_and_its_card(board, card_cache, tmp_path):
            assert card_cache.get("prepared") is None
            image = tmp_path / "card.img"
            image.write_bytes(bytes(512 * 1024))
            card_cache.put("prepared", image)
            assert card_cache.get("prepared").path.read_bytes() == image.read_bytes()
            board.sd_card.insert(card_cache.get("prepared"))
        def test_card(sd_card):
            sd_card.insert("not-read.img")
        def test_card_removal(sd_card):
            sd_card.remove()
        def test_nvs(board):
            board.nvs({"config": {"host": "test"}})

        def test_flash(board):
            board.flash()
        @pytest.mark.board_profile("lilygo-t-a7670g-r2")
        def test_modem_model(modem):
            modem.signal(dbm=-73)
        @pytest.mark.board_profile("lilygo-t-a7670g-r2")
        def test_modem_replay(modem):
            modem.replay("not-read.jsonl")
        @pytest.mark.board_profile("lilygo-t-a7670g-r2")
        def test_modem_tap(modem):
            modem.tap("not-written.jsonl")
        @pytest.mark.board_profile("lilygo-t-a7670g-r2")
        def test_network(modem):
            modem.network()
        @pytest.mark.board_profile("lilygo-t-a7670g-r2")
        def test_broker(broker):
            pytest.fail("the simulated broker must skip before the test body")
        @pytest.mark.board_profile("lilygo-t-a7670g-r2")
        def test_modem_clock(modem):
            modem.clock(zone=seconds(0))
        @pytest.mark.board_profile("lilygo-t-a7670g-r2")
        def test_modem_clock_not_set(modem):
            modem.clock_not_set(reads=datetime(2000, 1, 1, tzinfo=UTC))
        @pytest.mark.board_profile("lilygo-t-a7670g-r2")
        def test_modem_clock_set_by_network(modem):
            modem.clock_set_by_network()
        @pytest.mark.board_profile("lilygo-t-a7670g-r2")
        def test_modem_operator(modem):
            modem.operator(numeric="26001")
        @pytest.mark.board_profile("lilygo-t-a7670g-r2")
        def test_modem_hang(modem):
            modem.hang(pwrkey=False)
        @pytest.mark.board_profile("lilygo-t-a7670g-r2")
        def test_modem_drop_call(modem):
            modem.drop_call(terminate=True)
        @pytest.mark.board_profile("lilygo-t-a7670g-r2")
        def test_gpio(lines):
            lines.drive("modem.ri", True)
        def test_wifi(wifi):
            wifi.access_point("myssid", password="mypassword")
        def test_wifi_station(wifi):
            wifi.station("setup", password="password1")
        def test_bt(bt):
            pass
        def test_power_off(board):
            board.power_on()
            board.power_off()
        def test_boots(board):
            board.power_on()
            board.boot
        def test_wait_for_wake(board):
            board.power_on()
            board.wait_for_wake(within=seconds(1))
        def test_next_boot(board):
            board.power_on()
            board.next_boot()
        def test_uptime_zero(board):
            from esp32tl import POWERON_RESET, Boot
            from esp32tl.units import Duration
            board.power_on()
            board.uptime_zero(Boot(1, Duration(0), POWERON_RESET))
        def test_sleeps(board):
            board.power_on()
            board.sleeps
        def test_next_sleep(board):
            board.power_on()
            board.next_sleep()
        class Silent:
            def receive(self, t, data):
                return ()
            def poll(self, now):
                return ()
            def quiet_until(self):
                return None
            def latency(self):
                return None
        def test_own_model(board):
            board.uart("gnss").model(Silent())
    """)
    )
    result = run_serial(pytester, "-rs")
    result.assert_outcomes(skipped=33)
    result.stdout.fnmatch_lines(
        [
            "*serial: uart.gnss takes no model source on this backend, only real*",
            "*serial: uart.gnss takes no replay source on this backend, only real*",
            "*serial: uart.gnss cannot be tapped on this backend*",
            "*serial: uart.gnss has the device wired to the board at its far end: a host serial"
            " port goes there on the emulator only*",
            "*serial: sd_card takes no model source on this backend, only real*",
            "*serial: sd_card takes no model source on this backend, only real*",
            "*serial: NVS contents cannot be configured on this backend*",
            "*serial: flash contents cannot be read on this backend*",
            "*serial: uart.modem takes no model source on this backend, only real*",
            "*serial: uart.modem takes no replay source on this backend, only real*",
            "*serial: uart.modem cannot be tapped on this backend*",
            "*serial: uart.modem takes no model source on this backend, only real*",
            "*serial: uart.modem takes no model source on this backend, only real*",
            "*serial: uart.modem takes no model source on this backend, only real*",
            "*serial: uart.modem takes no model source on this backend, only real*",
            "*serial: uart.modem takes no model source on this backend, only real*",
            "*serial: uart.modem takes no model source on this backend, only real*",
            "*serial: uart.modem takes no model source on this backend, only real*",
            "*serial: uart.modem takes no model source on this backend, only real*",
            "*serial: GPIO lines cannot be controlled or observed on this backend*",
            "*serial: wifi takes no model source on this backend, only real*",
            "*serial: bluetooth takes no model source on this backend*",
            "*serial: the backend cannot switch the board's power*",
            "*serial: the backend does not see the chip's resets*",
            "*serial: the backend does not see the chip's resets*",
            "*serial: the backend does not see the chip's resets*",
            "*serial: the backend does not see the chip's resets*",
            "*serial: the backend does not see the chip's deep sleeps*",
            "*serial: the backend does not see the chip's deep sleeps*",
            "*serial: uart.gnss takes no model source on this backend, only real*",
        ]
    )
    assert not (pytester.path / "not-written.jsonl").exists()


def test_serial_modem_fixture_can_accompany_console_test(pytester: pytest.Pytester) -> None:
    fake_build(pytester.path)
    pytester.makeconftest(FAKE_SERIAL)
    pytester.makepyfile("""
        import pytest
        from esp32tl.interfaces.modem import Modem
        @pytest.mark.board_profile("lilygo-t-a7670g-r2")
        def test_console(board, modem, console):
            assert isinstance(modem, Modem)
            assert modem.source is None
            board.power_on()
            console.expect("Hello world!")
    """)
    run_serial(pytester).assert_outcomes(passed=1)


def test_artifact_hash_is_app_binary_and_refuses_missing_or_different_hash(tmp_path: Path) -> None:
    build = fake_build(tmp_path)
    (build / "app.bin").write_bytes(b"an application")
    digest = sha256(build / "app.bin")
    tap = Tap("console", tmp_path / "reference.jsonl")
    for expected in (digest, None, "0" * 64):
        recording.write(
            tap.path,
            tap.recording(
                board="esp32-devkitc",
                clock="wall",
                source="real",
                app_sha256=expected,
            ),
        )
        if expected == digest:
            assert check_artifact(str(tmp_path), tap.path).app_sha256 == digest
            (build / "bootloader/bootloader.bin").write_bytes(b"another bootloader")
            assert check_artifact(str(tmp_path), tap.path).app_sha256 == digest
        else:
            with pytest.raises(BackendError, match="app_sha256"):
                check_artifact(str(tmp_path), tap.path)


@pytest.mark.parametrize("invalid", [None, "hash", "missing", "target", "encryption"])
def test_serial_preflight_and_flashing_use_the_selected_build(
    pytester: pytest.Pytester, invalid: str | None
) -> None:
    app = pytester.path / "app source"
    default = fake_build(app)
    selected = fake_build(
        app,
        build_dir="../CI builds/debug",
        target="esp32s3" if invalid == "target" else "esp32",
        encrypted=invalid == "encryption",
    ).resolve()
    (default / "app.bin").write_bytes(b"default firmware")
    (selected / "app.bin").write_bytes(b"selected firmware")
    digest = sha256(selected / "app.bin")
    tap = Tap("console", pytester.path / "reference.jsonl")
    recording.write(
        tap.path,
        tap.recording(
            board="esp32-devkitc",
            clock="wall",
            source="real",
            app_sha256=sha256(default / "app.bin") if invalid == "hash" else digest,
        ),
    )
    if invalid == "missing":
        (selected / "app.bin").unlink()
    pytester.makeconftest(FAKE_SERIAL)
    pytester.makepyfile(f"""
        from pathlib import Path
        import pytest
        @pytest.mark.app_path("app source", build_dir="../CI builds/debug")
        @pytest.mark.app_recording("reference.jsonl")
        def test_selected(dut, board, console, build_dir):
            assert Path(dut.app.binary_path) == Path({str(selected)!r})
            assert Path(build_dir) == Path(dut.app.binary_path)
            assert Path(dut.serial.app.bin_file).read_bytes() == b"selected firmware"
            assert dut.serial.flashes == 1
            assert board.app_sha256 == {digest!r}
            console.tap("selected.jsonl")
            board.power_on()
            console.expect("Hello world!")
    """)
    result = run_serial(pytester)
    if invalid is None:
        result.assert_outcomes(passed=1)
        assert recording.read(pytester.path / "selected.jsonl").header.app_sha256 == digest
    else:
        result.assert_outcomes(errors=1)
        assert not (pytester.path / "port-opened").exists()
        message = {
            "hash": "recording app_sha256 * does not match artifact",
            "missing": "is not a finished build, missing",
            "target": "is built for esp32s3; the board's chip is esp32",
            "encryption": "asks for flash encryption, which serial does not support",
        }[invalid]
        result.stdout.fnmatch_lines([f"*serial: *{message}*"])
    assert (default / "app.bin").read_bytes() == b"default firmware"


def test_recording_mismatch_prevents_dut_setup(pytester: pytest.Pytester) -> None:
    fake_build(pytester.path)
    tap = Tap("console", pytester.path / "reference.jsonl")
    recording.write(
        tap.path,
        tap.recording(
            board="esp32-devkitc",
            clock="wall",
            source="real",
            app_sha256="0" * 64,
        ),
    )
    pytester.makeconftest("""
        import pytest
        @pytest.fixture
        def dut():
            raise AssertionError("must not open a port")
    """)
    pytester.makepyfile("""
        import pytest
        @pytest.mark.app_recording("reference.jsonl")
        def test_one(board):
            pass
    """)
    result = run_serial(pytester, "--skip-autoflash", "y")
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*serial: *recording app_sha256 * does not match artifact *"])
    result.stdout.no_fnmatch_line("*must not open a port*")


def test_disconnect_ends_wait_and_releases_reader(pytester: pytest.Pytester) -> None:
    fake_build(pytester.path)
    pytester.makeconftest(FAKE_SERIAL)
    pytester.makepyfile("""
        from esp32tl.units import seconds
        def test_disconnect(board):
            board.power_on()
            board.dut.serial.proc.close()
            board.wait_until(lambda: False, within=seconds(1))
    """)
    result = run_serial(pytester)
    # The call reports RunEnded; the teardown must not report the same error again.
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*RunEnded: serial: console connection lost:*"])


def test_console_only_uses_serial_backend(pytester: pytest.Pytester) -> None:
    fake_build(pytester.path)
    pytester.makeconftest(FAKE_SERIAL)
    pytester.makepyfile("""
        def test_console(console, request):
            assert request.node.name == "test_console[serial]"
            request.getfixturevalue("board").power_on()
            console.expect("Hello world!")
    """)
    run_serial(pytester).assert_outcomes(passed=1)


def test_reference_check_precedes_even_an_explicit_dut(pytester: pytest.Pytester) -> None:
    fake_build(pytester.path)
    tap = Tap("console", pytester.path / "reference.jsonl")
    recording.write(
        tap.path,
        tap.recording(
            board="esp32-devkitc",
            clock="wall",
            source="real",
            app_sha256="0" * 64,
        ),
    )
    pytester.makeconftest(FAKE_SERIAL)
    pytester.makepyfile("""
        import pytest
        @pytest.mark.app_recording("reference.jsonl")
        def test_one(dut, board):
            pass
    """)
    result = run_serial(pytester)
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*recording app_sha256 * does not match artifact *"])
    assert not (pytester.path / "port-opened").exists()


@pytest.mark.parametrize("power_on", [False, True])
def test_serial_tap_without_power_and_after_failure(
    pytester: pytest.Pytester, power_on: bool
) -> None:
    fake_build(pytester.path)
    pytester.makeconftest(FAKE_SERIAL)
    body = "board.power_on(); console.expect('Hello'); assert False" if power_on else "pass"
    pytester.makepyfile(f"""
        def test_tap(board, console):
            console.tap("console.jsonl")
            {body}
    """)
    result = run_serial(pytester)
    if power_on:
        result.assert_outcomes(failed=1)
        assert recording.read(pytester.path / "console.jsonl").events
    else:
        result.assert_outcomes(passed=1, errors=1)
        result.stdout.fnmatch_lines(
            ["*serial: the test set up the tap of console and never called*"]
        )
        assert not (pytester.path / "console.jsonl").exists()


def test_serial_refuses_multiple_boards_before_port_setup(pytester: pytest.Pytester) -> None:
    fake_build(pytester.path)
    pytester.makeconftest(FAKE_SERIAL)
    pytester.makepyfile("""
        import pytest
        @pytest.mark.parametrize("count", [2], indirect=True)
        def test_one(board):
            pass
    """)
    result = run_serial(pytester)
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["count is above 1: esp32tl runs one board in a test"])
    assert not (pytester.path / "port-opened").exists()


@pytest.mark.parametrize(
    "options",
    [
        (),
        ("--port", ""),
        ("--port", "/dev/cu.*"),
        ("--port-location", "1-2"),
        ("--port-mac", "aa:bb:cc:dd:ee:ff"),
    ],
)
def test_serial_requires_an_exact_port_before_setup(
    pytester: pytest.Pytester, options: tuple[str, ...]
) -> None:
    pytester.makeconftest(FAKE_SERIAL)
    pytester.makepyfile("def test_one(board): pass\n\ndef test_plain(): pass")
    result = pytester.runpytest_subprocess("--esp32tl-backend", "serial", *options)
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(
        ["*test_one*serial requires an explicit --port*automatic port discovery is disabled*"]
    )
    assert not (pytester.path / "port-opened").exists()
    # Selecting only a plain test needs no hardware selection.
    pytester.runpytest_subprocess("--esp32tl-backend", "serial", "-k", "plain").assert_outcomes(
        passed=1, deselected=1
    )


def test_port_parametrisation_selects_the_device(pytester: pytest.Pytester) -> None:
    fake_build(pytester.path)
    pytester.makeconftest(FAKE_SERIAL)
    pytester.makepyfile("""
        import pytest
        @pytest.mark.parametrize("port", ["test-port"], indirect=True)
        def test_one(board, console):
            board.power_on()
            console.expect("Hello world!")
    """)
    pytester.runpytest_subprocess("--esp32tl-backend", "serial").assert_outcomes(passed=1)


def test_empty_port_parameter_uses_explicit_cli_port(pytester: pytest.Pytester) -> None:
    fake_build(pytester.path)
    pytester.makeconftest(FAKE_SERIAL)
    pytester.makepyfile("""
        import pytest
        @pytest.mark.parametrize("port", [None, ""], indirect=True)
        def test_one(board, console):
            board.power_on()
            console.expect("Hello world!")
    """)
    run_serial(pytester).assert_outcomes(passed=2)


def test_overridden_port_cannot_enable_discovery(pytester: pytest.Pytester) -> None:
    fake_build(pytester.path)
    pytester.makeconftest(
        FAKE_SERIAL
        + """
        \n@pytest.fixture
def port():
    return None
    """
    )
    pytester.makepyfile("def test_one(dut, board): pass")
    run_serial(pytester).assert_outcomes(errors=1)
    assert not (pytester.path / "port-opened").exists()


@pytest.mark.parametrize("espport", ["other-port", "/dev/cu.other*", " "])
def test_environment_cannot_override_explicit_port(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, espport: str
) -> None:
    monkeypatch.setenv("ESPPORT", espport)
    pytester.makeconftest(FAKE_SERIAL)
    pytester.makepyfile("def test_one(dut, board): pass\n\ndef test_plain(): pass")
    result = run_serial(pytester)
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*serial requires ESPPORT to be unset, empty or equal*"])
    assert not (pytester.path / "port-opened").exists()
    run_serial(pytester, "-k", "plain").assert_outcomes(passed=1, deselected=1)


@pytest.mark.parametrize("espport", ["", "test-port"])
def test_environment_can_agree_with_explicit_port(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, espport: str
) -> None:
    monkeypatch.setenv("ESPPORT", espport)
    fake_build(pytester.path)
    pytester.makeconftest(FAKE_SERIAL)
    pytester.makepyfile("""
        def test_one(board, console):
            board.power_on()
            console.expect("Hello world!")
    """)
    run_serial(pytester).assert_outcomes(passed=1)


@pytest.mark.parametrize("restore", [False, True])
def test_setup_environment_cannot_redirect_port(pytester: pytest.Pytester, restore: bool) -> None:
    fake_build(pytester.path)
    pytester.makeconftest(
        FAKE_SERIAL
        + f"""
import os
@pytest.fixture
def port(monkeypatch):
    monkeypatch.setenv("ESPPORT", "other-port")
    return "test-port"

@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(fixturedef, request):
    result = yield
    if {restore!r} and fixturedef.argname == "_fixture_classes_and_options":
        os.environ.pop("ESPPORT", None)
    return result
"""
    )
    pytester.makepyfile("def test_one(dut, board): pass")
    result = run_serial(pytester)
    result.assert_outcomes(errors=1)
    message = (
        "serial requires pytest-embedded's cached port to match the explicit port"
        if restore
        else "serial requires ESPPORT to be unset, empty or equal to the explicit port"
    )
    result.stdout.fnmatch_lines([f"*{message}*"])
    assert not (pytester.path / "port-opened").exists()


def test_overridden_services_fail_without_a_traceback(pytester: pytest.Pytester) -> None:
    fake_build(pytester.path)
    pytester.makeconftest(FAKE_SERIAL + "\n@pytest.fixture\ndef embedded_services(): return None\n")
    pytester.makepyfile("def test_one(dut, board): pass")
    result = run_serial(pytester)
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(
        ["serial: the serial backend requires pytest-embedded's esp,idf services"]
    )
    result.stdout.no_fnmatch_line("*KeyError*")
    result.stdout.no_fnmatch_line("*esp32tl/pytest_plugin.py:*")
    assert not (pytester.path / "port-opened").exists()


@pytest.mark.parametrize("port", ["other-port", "*", "", None])
def test_cached_port_override_names_the_snapshot(
    pytester: pytest.Pytester, port: str | None
) -> None:
    fake_build(pytester.path)
    pytester.makeconftest(
        FAKE_SERIAL
        + f"""
@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(fixturedef, request):
    result = yield
    if fixturedef.argname == "_fixture_classes_and_options":
        result.kwargs["serial"]["port"] = {port!r}
    return result
"""
    )
    pytester.makepyfile("def test_one(dut, board): pass")
    result = run_serial(pytester)
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(
        ["serial requires pytest-embedded's cached port to match the explicit port"]
    )
    result.stdout.no_fnmatch_line("*serial requires ESPPORT*")
    assert not (pytester.path / "port-opened").exists()


@pytest.mark.parametrize("flash_port", ["other-port", "*", ""])
@pytest.mark.parametrize("selection", ["cli", "parameter", "fixture"])
def test_separate_flash_port_is_refused_before_opening(
    pytester: pytest.Pytester, flash_port: str, selection: str
) -> None:
    fake_build(pytester.path)
    conftest = FAKE_SERIAL
    test = "def test_one(dut, board): pass"
    args: tuple[str, ...] = ()
    if selection == "cli":
        args = ("--flash-port", flash_port)
    elif selection == "parameter":
        test = (
            f'import pytest\n@pytest.mark.parametrize("flash_port", [{flash_port!r}], '
            f"indirect=True)\n{test}"
        )
    else:
        conftest += f"\n@pytest.fixture\ndef flash_port(): return {flash_port!r}\n"
    pytester.makeconftest(conftest)
    pytester.makepyfile(test)
    result = run_serial(pytester, *args)
    # An empty parameter falls back to None in pytest-embedded, so it enables no second port.
    if selection == "parameter" and flash_port == "":
        result.assert_outcomes(passed=1)
    else:
        if selection == "fixture":
            result.assert_outcomes(errors=1)
            result.stdout.fnmatch_lines(["*serial does not support --flash-port*"])
        else:
            assert result.ret == pytest.ExitCode.USAGE_ERROR
            result.stderr.fnmatch_lines(["*serial does not support --flash-port*"])
        assert not (pytester.path / "port-opened").exists()


@pytest.mark.parametrize(
    "marker",
    [
        "app_recording",
        "app_recording()",
        'app_recording("a", "b")',
        "app_recording(42)",
        'app_recording(path="a")',
    ],
)
def test_bad_reference_marker_names_the_test(pytester: pytest.Pytester, marker: str) -> None:
    pytester.makeconftest(SHOW_PARAMS)
    pytester.makepyfile(
        test_reference=f"import pytest\n@pytest.mark.{marker}\ndef test_bad(board): pass"
    )
    result = run_serial(pytester)
    assert result.ret == pytest.ExitCode.INTERRUPTED
    result.stdout.fnmatch_lines(
        [
            "*test_reference.py::test_bad: the app_recording marker takes one path, as in "
            '@pytest.mark.app_recording("hello-console.jsonl")'
        ]
    )


@pytest.mark.parametrize("retry", [False, True])
def test_failed_reset_restores_redirect_and_stays_off(
    pytester: pytest.Pytester, retry: bool
) -> None:
    fake_build(pytester.path)
    pytester.makeconftest(FAKE_SERIAL)
    pytester.makepyfile(f"""
        import pytest
        from pathlib import Path
        def test_reset(board, console, monkeypatch):
            console.tap("console.jsonl")
            serial = board.dut.serial
            reset = serial.hard_reset
            def fail():
                raise OSError("reset failed")
            monkeypatch.setattr(serial, "hard_reset", fail)
            with pytest.raises(OSError, match="reset failed"):
                board.power_on()
            assert not board.powered
            assert serial._redirect_thread.is_alive()
            if {retry!r}:
                monkeypatch.setattr(serial, "hard_reset", reset)
                board.power_on()
                console.expect("Hello world!")
            else:
                board.close()
                assert not Path("console.jsonl").exists()
    """)
    result = run_serial(pytester)
    result.assert_outcomes(passed=1, errors=0 if retry else 1)
    if retry:
        assert recording.read(pytester.path / "console.jsonl").events
    else:
        result.stdout.fnmatch_lines(
            ["*serial: the test set up the tap of console and never called*"]
        )
        assert not (pytester.path / "console.jsonl").exists()


def test_dead_open_port_does_not_restart_redirect(pytester: pytest.Pytester) -> None:
    fake_build(pytester.path)
    pytester.makeconftest(FAKE_SERIAL)
    pytester.makepyfile("""
        import pytest
        from esp32tl.backends.esp32sim import BackendError
        from esp32tl.interfaces import RunEnded
        from esp32tl.units import seconds
        def test_disconnect(board, monkeypatch):
            board.power_on()
            serial = board.dut.serial
            assert serial.proc.is_open
            def fail():
                raise OSError("USB disconnected")
            def must_not_restart():
                pytest.fail("restarted the disconnected transport")
            monkeypatch.setattr(serial.proc, "read_all", fail)
            monkeypatch.setattr(serial, "start_redirect_thread", must_not_restart)
            with pytest.raises(RunEnded, match="console connection lost: USB disconnected"):
                board.wait_until(lambda: False, within=seconds(1))
            with pytest.raises(BackendError, match="console connection lost: USB disconnected"):
                board.close()
            assert "the run ended first" in board.report(run=True)
    """)
    run_serial(pytester).assert_outcomes(passed=1)


@pytest.mark.parametrize("late_error", [False, True])
def test_stuck_reader_has_bounded_teardown_and_no_late_capture(
    pytester: pytest.Pytester, late_error: bool
) -> None:
    fake_build(pytester.path)
    pytester.makeconftest(FAKE_SERIAL)
    pytester.makepyfile(f"""
        import pytest
        import time
        import threading
        import esp32tl.backends.serial as backend
        from esp32tl.backends.esp32sim import BackendError
        from esp32tl import recording
        def test_stuck(board, console, monkeypatch):
            console.tap("console.jsonl")
            board.power_on()
            console.expect("Hello world!")
            entered, release = threading.Event(), threading.Event()
            STUCK_SECONDS = 5
            def stuck():
                entered.set()
                release.wait(STUCK_SECONDS)
                if {late_error!r}:
                    raise OSError("late disconnect")
                return b"late bytes"
            def must_not_restart():
                pytest.fail("restarted while the old reader was stuck")
            monkeypatch.setattr(board.dut.serial.proc, "read_all", stuck)
            monkeypatch.setattr(board.dut.serial, "start_redirect_thread", must_not_restart)
            monkeypatch.setattr(backend, "READER_STOP_SECONDS", .02)
            joins = []
            join = board._reader.join
            def joined(timeout=None):
                joins.append(timeout)
                join(timeout)
            monkeypatch.setattr(board._reader, "join", joined)
            try:
                assert entered.wait(1)
                start = time.monotonic()
                with pytest.raises(BackendError, match="console reader did not stop within 0.02 s"):
                    board.close()
                # The close waited READER_STOP_SECONDS for the reader, and for nothing that waits
                # on the read, stuck until the test releases it, STUCK_SECONDS at most: it took
                # less than half of those, and the reader is still in the read.
                assert joins == [.02]
                assert time.monotonic() - start < STUCK_SECONDS / 2
                assert board._reader.is_alive()
                captured = recording.read("console.jsonl")
                assert captured.events
            finally:
                release.set()
                board._reader.join(1)
            assert not board._reader.is_alive()
            assert b"late bytes" not in board.console_output
            assert "did not stop within" in board.properties()["error"]
            board.close()  # idempotent after the failed close
    """)
    run_serial(pytester).assert_outcomes(passed=1)


def test_serial_waits_and_console_tap_are_in_the_failure_report(pytester: pytest.Pytester) -> None:
    fake_build(pytester.path)
    pytester.makeconftest(FAKE_SERIAL)
    pytester.makepyfile("""
        from esp32tl.units import seconds
        def test_wait(board, console):
            console.tap("console.jsonl")
            board.power_on()
            console.expect_exact("Hello world!")
            board.wait(seconds(.01))
            console.expect_exact("missing", timeout=.01)
    """)
    result = run_serial(pytester)
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "*NeverHappened: 'missing' on the console did not happen within 0.01 s: "
            "waited from * to * of wall time*",
            "*console: tapped into */console.jsonl*",
            "*console.tap('console.jsonl')*",
            "*waits up to 30 s for 'Hello world!' on the console*",
            "*'Hello world!' on the console: happened*",
            "*board.wait(0.01 s)*",
            "*'missing' on the console: did not happen within 0.01 s*",
        ]
    )
    result.stdout.no_fnmatch_line("*elapsed time*")


def test_wait_validation_and_closed_run_match_emulator(pytester: pytest.Pytester) -> None:
    fake_build(pytester.path)
    pytester.makeconftest(FAKE_SERIAL)
    pytester.makepyfile("""
        import pytest
        from esp32tl.interfaces import BoardOff, RunEnded
        from esp32tl.units import seconds
        def test_wait(board):
            with pytest.raises(BoardOff):
                board.wait(1)
            with pytest.raises(BoardOff):
                board.wait_until(lambda: False, within=1)
            board.power_on()
            with pytest.raises(TypeError) as error:
                board.wait_until(lambda: False, within=1)
            assert str(error.value) == (
                "board.wait_until: within takes a Duration, as seconds(1), not 1"
            )
            with pytest.raises(ValueError) as error:
                board.wait_until(lambda: False, within=seconds(-1))
            assert str(error.value) == (
                "board.wait_until: within takes a Duration of 0 or more, not -1 s"
            )
            board.wait(seconds(0))
            board.close()
            with pytest.raises(RunEnded, match="the run is closed"):
                board.wait(seconds(0))
            assert "the run ended while the test waited" in "\\n".join(board.log.lines())
    """)
    run_serial(pytester).assert_outcomes(passed=1)


def test_serial_build_errors_name_serial(tmp_path: Path) -> None:
    fake_build(tmp_path / "s3", target="esp32s3")
    with pytest.raises(BackendError, match="is built for esp32s3; the board's chip is esp32"):
        check_artifact(str(tmp_path / "s3"))
    fake_build(tmp_path / "encrypted", encrypted=True)
    with pytest.raises(
        BackendError, match="asks for flash encryption, which serial does not support"
    ):
        check_artifact(str(tmp_path / "encrypted"))


def test_an_s3_with_its_console_on_usb_serial_jtag_runs_on_the_serial_backend(
    pytester: pytest.Pytester,
) -> None:
    fake_build(pytester.path, target="esp32s3")
    pytester.makeconftest(FAKE_SERIAL)
    (pytester.path / "s3-usb.toml").write_text(
        'name = "s3-usb"\nchip = "esp32s3"\nconsole = "usb-serial-jtag"\n'
    )
    pytester.makepyfile("""
        import pytest

        @pytest.mark.board_profile(path="s3-usb.toml")
        def test_console(board, console):
            assert board.profile.console == "usb-serial-jtag"
            assert board.dut.serial.flashes == 1
            board.power_on()
            console.expect_exact("Hello world!")
            console.sendline("help")
    """)
    run_serial(pytester, "-q").assert_outcomes(passed=1)


@pytest.mark.parametrize(("chip", "title"), [("esp32c3", "ESP32-C3"), ("esp32c6", "ESP32-C6")])
def test_a_usb_serial_jtag_console_on_a_chip_not_validated_on_hardware_skips(
    pytester: pytest.Pytester, chip: str, title: str
) -> None:
    fake_build(pytester.path, target=chip)
    pytester.makeconftest(FAKE_SERIAL)
    (pytester.path / "usb.toml").write_text(
        f'name = "usb"\nchip = "{chip}"\nconsole = "usb-serial-jtag"\n'
    )
    pytester.makepyfile("""
        import pytest

        @pytest.mark.board_profile(path="usb.toml")
        def test_console(board):
            pass
    """)
    result = run_serial(pytester, "-rs")
    result.assert_outcomes(skipped=1)
    result.stdout.fnmatch_lines(
        [
            "SKIPPED [[]1[]] *.py:3: serial: the board profile usb has its console on the"
            f" {title}'s USB-Serial/JTAG controller, which the serial backend has been validated"
            " on with the ESP32-S3 only"
        ]
    )
    # nothing reached the port
    assert not (pytester.path / "port-opened").exists()
