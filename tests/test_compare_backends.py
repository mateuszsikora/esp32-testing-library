"""scripts/compare-backends.py, the comparison of the two backends, on JUnit XML made up here: the
outcomes it reads, the tests it matches and the line it gives each, and `main` with a stand-in for
pytest that leaves the XML. Neither the runner, pytest nor a board runs."""

import importlib.util
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from xml.sax.saxutils import quoteattr

import pytest

ROOT = Path(__file__).resolve().parent.parent


def load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


compare = load("compare_backends", ROOT / "scripts" / "compare-backends.py")
Outcome = compare.Outcome


def junit(cases: dict[str, str]) -> str:
    """JUnit XML with a case for each `classname::name` in `cases`, ending as its value says:
    `passed`, or `failure: …`, `error: …`, `skipped: …` with the message."""
    body = []
    for test, ending in cases.items():
        classname, name = test.split("::")
        status, _, message = ending.partition(": ")
        inner = "" if status == "passed" else f"<{status} message={quoteattr(message)}/>"
        attributes = f"classname={quoteattr(classname)} name={quoteattr(name)}"
        body.append(f"<testcase {attributes}>{inner}</testcase>")
    return f"<testsuites><testsuite>{''.join(body)}</testsuite></testsuites>"


def test_a_failure_or_an_error_wins_over_a_skip(tmp_path: Path) -> None:
    (tmp_path / "r.xml").write_text(
        "<testsuites><testsuite>"
        '<testcase classname="t" name="a"/>'
        '<testcase classname="t" name="b"><failure message="assert 1 == 2">trace</failure>'
        "</testcase>"
        '<testcase classname="t" name="c"><error message="error at setup"/></testcase>'
        '<testcase classname="t" name="d"><skipped message="serial: no"/></testcase>'
        '<testcase classname="t" name="e"><skipped message="s"/><error message="at teardown"/>'
        "</testcase>"
        "</testsuite></testsuites>"
    )
    assert compare.outcomes(tmp_path / "r.xml") == {
        "t::a": Outcome("passed"),
        "t::b": Outcome("failed", "assert 1 == 2"),
        "t::c": Outcome("failed", "error at setup"),
        "t::d": Outcome("skipped", "serial: no"),
        "t::e": Outcome("failed", "at teardown"),
    }


def test_the_backend_is_taken_out_of_the_id_wherever_it_is_among_the_parameters() -> None:
    assert compare.without_backend("t::test_a[esp32sim]", "esp32sim") == "t::test_a"
    assert compare.without_backend("t::test_a[serial-gnss-generated-1]", "serial") == (
        "t::test_a[gnss-generated-1]"
    )
    assert compare.without_backend("t::test_a[0-esp32sim]", "esp32sim") == "t::test_a[0]"
    assert compare.without_backend("t::test_a[esp32sim]", "serial") is None
    assert compare.without_backend("t::test_a", "esp32sim") is None


def test_each_test_that_uses_board_gets_a_line_and_the_others_are_left_out() -> None:
    sim = {
        "t::test_hello[esp32sim]": Outcome("passed"),
        "t::test_fix[esp32sim-gnss-1]": Outcome("passed"),
        "t::test_both_fail[esp32sim]": Outcome("failed", "assert False\nmore"),
        "t::test_differs[esp32sim]": Outcome("passed"),
        "t::test_sim_fails[esp32sim]": Outcome("failed", "Timeout"),
        "t::test_power[esp32sim]": Outcome("skipped", "no reason"),
        "t::test_off[esp32sim]": Outcome("skipped", "off on both"),
        "t::test_only_here[esp32sim]": Outcome("passed"),
        # A test without `board`, parametrised over the backends' names by itself.
        "t::test_profile[esp32sim-0]": Outcome("passed"),
        "t::test_units": Outcome("passed"),
    }
    real = {
        "t::test_hello[serial]": Outcome("passed"),
        "t::test_fix[serial-gnss-1]": Outcome("skipped", "serial: uart.gnss takes no replay"),
        "t::test_both_fail[serial]": Outcome("failed", "assert False"),
        "t::test_differs[serial]": Outcome("failed", "NeverHappened: 'Hello' within 30 s"),
        "t::test_sim_fails[serial]": Outcome("skipped", "serial: lines"),
        "t::test_power[serial]": Outcome("passed"),
        "t::test_off[serial]": Outcome("skipped", "off on both"),
        "t::test_only_there[serial]": Outcome("passed"),
        "t::test_profile[esp32sim-0]": Outcome("passed"),
        "t::test_units": Outcome("passed"),
    }
    lines, counts = compare.compare(sim, real)
    assert lines == [
        "✓ t::test_hello: passed on both",
        "- t::test_fix[gnss-1]: esp32sim only (passed): skipped on serial:"
        " serial: uart.gnss takes no replay",
        "✗ t::test_both_fail: failed on both: on esp32sim: assert False; on serial: assert False",
        "✗ t::test_differs: passed on esp32sim, failed on serial:"
        " NeverHappened: 'Hello' within 30 s",
        "✗ t::test_sim_fails: esp32sim only (failed: Timeout): skipped on serial: serial: lines",
        "✗ t::test_power: serial only (passed): skipped on esp32sim: no reason",
        "- t::test_off: skipped on both: off on both",
        "✗ t::test_only_here: collected on esp32sim only",
        "✗ t::test_only_there: collected on serial only",
    ]
    assert counts == {
        "both": 3,
        "agreed": 2,
        "esp32sim": 2,
        "serial": 1,
        "neither": 1,
        "left out": 2,
    }
    assert compare.summary(counts) == (
        "3 tests ran on both backends, 2 agreed and 1 did not; 2 ran on esp32sim only,"
        " 1 on serial only, 1 on neither; 2 tests without `board` left out"
    )


def test_a_long_message_is_cut_and_a_missing_one_named() -> None:
    assert compare.short("x" * 300) == "x" * 200 + "..."
    assert compare.short("  \n") == "(no message)"


class Pytest:
    """Stands in for `subprocess.run` of pytest: records each command, writes the JUnit XML that
    `left` gives for that run unless it is None, and exits with the run's code."""

    def __init__(self, left: Sequence[str | None], codes: tuple[int, ...] | None = None) -> None:
        self.left = left
        self.codes = codes or (0,) * len(left)
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], cwd: Path) -> subprocess.CompletedProcess[bytes]:
        self.commands.append(command)
        run = len(self.commands) - 1
        xml = Path(next(a for a in command if a.startswith("--junitxml=")).split("=", 1)[1])
        if self.left[run] is not None:
            xml.write_text(self.left[run] or "")
        return subprocess.CompletedProcess(command, self.codes[run])


AGREE = [
    junit({"t::test_hello[esp32sim]": "passed", "t::test_units": "passed"}),
    junit({"t::test_hello[serial]": "passed", "t::test_units": "passed"}),
]


def test_pytest_runs_on_each_backend_with_the_arguments_and_the_smoke_tests_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pytest_ = Pytest(AGREE * 3)
    monkeypatch.setattr(compare.subprocess, "run", pytest_)
    out = tmp_path / "out"
    port = ["--port", "/dev/ttyUSB0", "--out", str(out)]
    assert compare.main([*port, "tests/test_hello_world.py", "-k", "hello"]) == 0
    assert compare.main(port) == 0
    assert compare.main([*port, "-k", "hello", "--", "tests/t.py"]) == 0

    def ours(backend: str) -> list[str]:
        serial = ["--port=/dev/ttyUSB0"] if backend == "serial" else []
        return [
            f"--esp32tl-backend={backend}",
            *serial,
            f"--junitxml={out / backend}.xml",
            "-o",
            "junit_family=xunit1",
        ]

    assert [c[3:] for c in pytest_.commands] == [
        ["tests/test_hello_world.py", "-k", "hello", *ours("esp32sim")],
        ["tests/test_hello_world.py", "-k", "hello", *ours("serial")],
        ["tests/test_hello_world.py", "tests/test_nmea_reader.py", *ours("esp32sim")],
        ["tests/test_hello_world.py", "tests/test_nmea_reader.py", *ours("serial")],
        # After a `--` pytest takes every argument as a path: the script's options come before.
        ["-k", "hello", *ours("esp32sim"), "--", "tests/t.py"],
        ["-k", "hello", *ours("serial"), "--", "tests/t.py"],
    ]
    assert capsys.readouterr().out.splitlines()[-2:] == [
        "✓ t::test_hello: passed on both",
        "backends: 1 test ran on both backends, 1 agreed and 0 did not; 0 ran on esp32sim only,"
        " 0 on serial only, 0 on neither; 1 test without `board` left out",
    ]


def test_the_port_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        compare.main([])
    assert raised.value.code == 2
    assert "the following arguments are required: --port" in capsys.readouterr().err


def test_failing_tests_are_compared_but_a_run_that_could_not_test_ends_the_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    port = ["--port", "/dev/ttyUSB0", "--out", str(tmp_path)]
    differ = junit({"t::test_hello[serial]": "failure: serial: no such port"})
    monkeypatch.setattr(compare.subprocess, "run", Pytest([AGREE[0], differ], codes=(0, 1)))
    assert compare.main(port) == 1
    assert capsys.readouterr().out.splitlines()[-2] == (
        "✗ t::test_hello: passed on esp32sim, failed on serial: serial: no such port"
    )
    # A usage error, as a missing firmware build: the serial run is not attempted.
    pytest_ = Pytest([None, None], codes=(4, 0))
    monkeypatch.setattr(compare.subprocess, "run", pytest_)
    assert compare.main(port) == 1
    assert len(pytest_.commands) == 1
    assert capsys.readouterr().out.endswith(
        "✗ esp32sim: pytest exited with code 4; the backends were not compared\n"
    )
    # The report of the run before is not compared in place of the one this run did not write.
    assert not (tmp_path / "esp32sim.xml").exists()


def test_no_test_on_both_backends_fails_the_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    skipped = junit({"t::test_fix[serial]": "skipped: serial: uart.gnss takes no replay"})
    left = [junit({"t::test_fix[esp32sim]": "passed"}), skipped]
    monkeypatch.setattr(compare.subprocess, "run", Pytest(left))
    assert compare.main(["--port", "/dev/ttyUSB0", "--out", str(tmp_path)]) == 1
    assert capsys.readouterr().out.splitlines()[-1] == (
        "✗ no test ran on both backends: nothing was compared"
    )


def test_an_out_that_is_a_file_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "out").write_text("")
    assert compare.main(["--port", "/dev/ttyUSB0", "--out", str(tmp_path / "out")]) == 2
    assert capsys.readouterr().out == (
        f"✗ {tmp_path / 'out'} is not a directory: name a directory with --out\n"
    )
