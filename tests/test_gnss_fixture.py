"""The `gnss` fixture: a recording replayed through `gnss.replay(path)` into nmea_reader on the
real runner, and what a test that gives its source otherwise than its markers declare sees. Needs
`make firmware` (nmea_reader)."""

import time
from pathlib import Path

import pytest

from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.interfaces.console import Console
from esp32tl.interfaces.uart import Uart

ROOT = Path(__file__).resolve().parent.parent
NMEA_READER = ROOT / "firmware" / "nmea_reader"
RECORDING = ROOT / "recordings" / "gnss-generated-1.jsonl"


@pytest.mark.app_path("../firmware/nmea_reader")
@pytest.mark.source("uart.gnss", "replay")
def test_a_replay_reaches_nmea_reader_on_the_gnss_route(
    board: Esp32Sim, gnss: Uart, console: Console
) -> None:
    replay = gnss.replay("../recordings/gnss-generated-1.jsonl")
    # The first sentence, at 1 emulated second, is the first fix (validation-firmware-user.md).
    console.expect_exact("fix acquired lat=52.229677 lon=21.012233")
    # The second, an RMC at 2 s: 5213.7794,N 02100.7356,E.
    console.expect_exact("fix lat=52.229657 lon=21.012260")
    assert replay.sent >= 2
    assert board.link.warnings == []


def test_a_source_given_otherwise_than_declared_fails_the_test(pytester: pytest.Pytester) -> None:
    if not (NMEA_READER / "build" / "esp32tl-built").is_file():
        pytest.fail(
            f"{NMEA_READER}/build holds no finished build: run make firmware", pytrace=False
        )
    pytester.makepyfile(
        test_gnss=f"""
        import pytest

        pytestmark = pytest.mark.app_path({str(NMEA_READER)!r})

        @pytest.mark.source("uart.gnss", "replay")
        def test_expect_before_replay(console):
            console.expect("fix", timeout=120)

        def test_replay_without_marker(gnss):
            gnss.replay({str(RECORDING)!r})

        @pytest.mark.source("uart.gnss", "replay")
        def test_never_given(gnss):
            pass

        @pytest.mark.source("uart.gnss", "model")
        def test_model(gnss):
            pass

        @pytest.mark.board_profile("lilygo-t-a7670g-r2")
        def test_no_route(gnss):
            pass
        """
    )
    started = time.monotonic()
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-rA")
    # The expect fails at once: held, the runner would give up only after --peer-timeout's 60 s,
    # and the expect after its 120 s.
    assert time.monotonic() - started < 50
    # test_never_given passes its call and errors at teardown; test_expect_before_replay fails on
    # its expect only, not again at teardown.
    result.assert_outcomes(passed=1, failed=2, errors=3)
    result.stdout.fnmatch_lines_random(
        [
            "*SourceError: the run has not started: it waits for the replay of uart.gnss, which the"
            " source marker declares; give it before waiting on the console",
            "*SourceError: uart.gnss has no source marker that names replay: mark the test"
            ' @pytest.mark.source("uart.gnss", "replay") to replay into it',
            "*esp32sim: uart.gnss: a model source is not implemented yet; the backend gives replay",
            "*esp32sim: the run never started: it waited for the replay of uart.gnss, which the"
            " source marker declares; give it in the test body",
            "*esp32sim: the board profile lilygo-t-a7670g-r2 has no uart.gnss; its interfaces are"
            " console, uart.modem, lines.modem",
        ]
    )
    # A run held for a source that never came ends before the guest boots.
    result.stdout.fnmatch_lines(
        ["*Captured esp32sim link teardown*", "board esp32-devkitc, watching GPIO 4, GPIO 5"]
    )
    assert "stats: the run ended at 0.000000000 s (stop); 1 turn, 0 resets" in result.stdout.str()
