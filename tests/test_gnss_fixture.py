"""The `gnss` fixture: a recording replayed through `gnss.replay(path)` into nmea_reader on the
real runner, and tapped through `gnss.tap(path)`, what a test that gives its source out of order
sees, and test_nmea_reader.py's
test_reports_a_fix failing on a changed recording and on a baud mismatch. Needs `make firmware`
(nmea_reader)."""

import base64
import hashlib
import json
import re
import shutil
import time
from pathlib import Path

import pytest

from esp32tl import recording
from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.interfaces.console import Console
from esp32tl.interfaces.gnss import Gnss

ROOT = Path(__file__).resolve().parent.parent
NMEA_READER = ROOT / "firmware" / "nmea_reader"
RECORDING = ROOT / "recordings" / "gnss-generated-1.jsonl"


@pytest.mark.app_path("../firmware/nmea_reader")
def test_a_replay_reaches_nmea_reader_on_the_gnss_route(
    board: Esp32Sim, gnss: Gnss, console: Console
) -> None:
    replay = gnss.replay("../recordings/gnss-generated-1.jsonl")
    board.power_on()
    # The first sentence, at 1 emulated second, is the first fix (validation-firmware-user.md).
    console.expect_exact("fix acquired lat=52.229677 lon=21.012233")
    # The second, an RMC at 2 s: 5213.7794,N 02100.7356,E.
    console.expect_exact("fix lat=52.229657 lon=21.012260")
    assert replay.sent >= 2
    assert board.link.warnings == []


def test_a_tap_of_a_replay_records_what_went_in(pytester: pytest.Pytester) -> None:
    if not (NMEA_READER / "build" / "esp32tl-built").is_file():
        pytest.fail(
            f"{NMEA_READER}/build holds no finished build: run make firmware", pytrace=False
        )
    tapped = pytester.path / "gnss-tap.jsonl"
    pytester.makepyfile(
        test_tap=f"""
        import pytest

        from esp32tl.units import seconds

        pytestmark = pytest.mark.app_path({str(NMEA_READER)!r})

        def test_tapped(board, gnss):
            replay = gnss.replay({str(RECORDING)!r})
            tap = gnss.tap({str(tapped)!r})
            board.power_on()
            board.wait(seconds(5.5))
            assert tap.events == [(t, "in", data) for t, data in replay.events[:5]]
        """
    )
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    # Written at teardown: the first five sentences, at 1 s to 5 s, as recorded; nmea_reader
    # transmits nothing on its UART1.
    made = recording.read(tapped)
    replayed = recording.read(RECORDING)
    assert [(e.t, e.dir, e.data) for e in made.events] == [
        (e.t, "in", e.data) for e in replayed.events[:5]
    ]
    build = NMEA_READER / "build"
    app = json.loads((build / "flasher_args.json").read_text())["app"]["file"]
    assert made.header.app_sha256 == hashlib.sha256((build / app).read_bytes()).hexdigest()
    assert re.fullmatch("[0-9a-f]{40}", made.header.tools["esp32sim"])
    assert (made.header.source, made.header.interface) == ("replay", "uart.gnss")


def test_a_source_given_out_of_order_fails_the_test(pytester: pytest.Pytester) -> None:
    if not (NMEA_READER / "build" / "esp32tl-built").is_file():
        pytest.fail(
            f"{NMEA_READER}/build holds no finished build: run make firmware", pytrace=False
        )
    pytester.makepyfile(
        test_gnss=f"""
        import pytest

        pytestmark = pytest.mark.app_path({str(NMEA_READER)!r})

        def test_expect_before_power_on(gnss, console):
            gnss.replay({str(RECORDING)!r})
            console.expect("fix")

        def test_replay_after_power_on(board, gnss):
            board.power_on()
            gnss.replay({str(RECORDING)!r})

        def test_never_powered(gnss):
            gnss.replay({str(RECORDING)!r})

        @pytest.mark.board_profile("lilygo-t-a7670g-r2")
        def test_no_route(gnss):
            pass
        """
    )
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-rA")
    # test_never_powered passes its call and errors at teardown; test_expect_before_power_on
    # fails on its expect only, not again at teardown.
    result.assert_outcomes(passed=1, failed=2, errors=2)
    result.stdout.fnmatch_lines_random(
        [
            "*BoardOff: the board is off; call board.power_on() first",
            "*SourceError: uart.gnss: the board is on already: give a route its source before"
            " board.power_on()",
            "*esp32sim: the test set up the replay of uart.gnss and never called"
            " board.power_on(): it ran no firmware",
            "*esp32sim: the board profile lilygo-t-a7670g-r2 has no uart.gnss; its interfaces are"
            " console, uart.modem, lines.modem",
        ]
    )
    # Every route of the profile is on the link; a board never powered on ran no runner.
    result.stdout.fnmatch_lines(
        [
            "*Captured esp32sim link teardown*",
            "board esp32-devkitc, watching GPIO 4, GPIO 5",
            "the runner never connected",
        ]
    )


def nmea_reader_tests(pytester: pytest.Pytester) -> None:
    """tests/test_nmea_reader.py in pytester's directory, as tests/test_nmea_reader.py, its app
    the nmea_reader build of this checkout and its recordings those in pytester's recordings/."""
    if not (NMEA_READER / "build" / "esp32tl-built").is_file():
        pytest.fail(
            f"{NMEA_READER}/build holds no finished build: run make firmware", pytrace=False
        )
    marker = 'app_path("../firmware/nmea_reader")'
    text = (ROOT / "tests" / "test_nmea_reader.py").read_text()
    assert marker in text
    (pytester.path / "tests").mkdir()
    (pytester.path / "tests" / "test_nmea_reader.py").write_text(
        text.replace(marker, f"app_path({str(NMEA_READER)!r})")
    )
    shutil.copytree(ROOT / "recordings", pytester.path / "recordings")


def checksummed(sentence: bytes) -> bytes:
    """`sentence`, `$...*hh\r\n`, with its checksum made right."""
    body = sentence[1 : sentence.index(b"*")]
    check = 0
    for char in body:
        check ^= char
    return b"$" + body + f"*{check:02X}\r\n".encode()


def test_reports_a_fix_fails_on_a_changed_recording(pytester: pytest.Pytester) -> None:
    nmea_reader_tests(pytester)
    # One digit of the first sentence's latitude, its checksum made right again: the firmware
    # takes the sentence, and prints another position.
    path = pytester.path / "recordings" / "gnss-generated-1.jsonl"
    lines = path.read_text().splitlines()
    first = json.loads(lines[1])
    data = base64.b64decode(first["data"])
    assert b",5213.7806,N," in data
    first["data"] = base64.b64encode(
        checksummed(data.replace(b",5213.7806,N,", b",5213.7906,N,"))
    ).decode()
    lines[1] = json.dumps(first)
    path.write_text("\n".join(lines) + "\n")
    result = pytester.runpytest_subprocess(
        "-p", "no:cacheprovider", "tests", "-k", "test_reports_a_fix"
    )
    result.assert_outcomes(failed=1, deselected=3)
    # The recording reached the firmware, which printed what the changed sentence says.
    result.stdout.fnmatch_lines(["*fix acquired lat=52.229843 lon=21.012233*"])


def test_reports_a_fix_fails_on_a_baud_mismatch(pytester: pytest.Pytester) -> None:
    nmea_reader_tests(pytester)
    # The profile's GNSS route at 115200 baud, and nmea_reader still at 9600: the replay's bytes
    # go in at the profile's baud, and the firmware's UART1 drops them as frame errors.
    pytester.makeconftest(
        """
        import dataclasses

        from esp32tl import boards, pytest_plugin

        profile = boards.profile("esp32-devkitc")
        route = dataclasses.replace(profile.uart["gnss"], baud=115200)
        mismatched = dataclasses.replace(profile, uart={**profile.uart, "gnss": route})
        pytest_plugin.board_profile = lambda node: mismatched
        """
    )
    started = time.monotonic()
    result = pytester.runpytest_subprocess(
        "-p", "no:cacheprovider", "tests", "-k", "test_reports_a_fix"
    )
    # Not silence: the run ends on the first frame error, and the expect with it, well before
    # its 30 s of the board's time.
    assert time.monotonic() - started < 20
    result.assert_outcomes(failed=1, deselected=3)
    result.stdout.fnmatch_lines(
        [
            "*the link failed: uart.gnss sends at 115200 baud on GPIO 5, and the firmware's UART1"
            " receives there at 9600 baud: * bytes dropped as frame errors (frm_err)*"
        ]
    )
