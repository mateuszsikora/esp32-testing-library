"""scripts/check-determinism.py, the determinism job's check, on two runs made up here: the files
they left, the JUnit XML, and `main` with a stand-in for pytest that leaves them. Neither the
runner nor pytest runs."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent


def load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


check = load("check_determinism", ROOT / "scripts" / "check-determinism.py")


def tap(created: str, times: list[int]) -> str:
    header = {"esp32tl_recording": 1, "interface": "uart.gnss", "created": created}
    events = [json.dumps({"t": t, "dir": "in", "data": "JA=="}) for t in times]
    return "\n".join([json.dumps(header), *events]) + "\n"


def run(directory: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        (directory / name).parent.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(text)
    return directory


def test_two_runs_that_differ_only_in_when_the_tap_was_written_are_identical(
    tmp_path: Path,
) -> None:
    first = run(
        tmp_path / "run-1",
        {"t0/console.txt": "1.0 s  fix\n", "t0/uart.gnss.jsonl": tap("2026-11-01T08:00:00Z", [1])},
    )
    second = run(
        tmp_path / "run-2",
        {"t0/console.txt": "1.0 s  fix\n", "t0/uart.gnss.jsonl": tap("2026-11-01T08:00:07Z", [1])},
    )
    # pytest's `*current` link to the newest numbered directory is not a file of the run.
    (first / "tcurrent").symlink_to(first / "t0")
    assert check.compare_files(first, second) == ([], 2)


def test_a_line_or_an_event_that_differs_is_shown_in_a_diff(tmp_path: Path) -> None:
    first = run(
        tmp_path / "run-1",
        {"t0/console.txt": "1.0 s  boot\n2.0 s  fix\n", "t0/uart.gnss.jsonl": tap("a", [1, 2])},
    )
    second = run(
        tmp_path / "run-2",
        {"t0/console.txt": "1.0 s  boot\n2.1 s  fix\n", "t0/uart.gnss.jsonl": tap("a", [1, 3])},
    )
    failures, files = check.compare_files(first, second)
    assert files == 2
    assert failures[0].splitlines()[0] == "t0/console.txt differs:"
    assert "    -2.0 s  fix" in failures[0].splitlines()
    assert "    +2.1 s  fix" in failures[0].splitlines()
    assert failures[1].startswith("t0/uart.gnss.jsonl differs:\n")
    assert '    +{"t": 3, "dir": "in", "data": "JA=="}' in failures[1].splitlines()


def test_a_file_one_run_left_and_the_other_did_not_is_named(tmp_path: Path) -> None:
    first = run(tmp_path / "run-1", {"t0/console.txt": "", "t0/uart.gnss.jsonl": tap("a", [])})
    second = run(tmp_path / "run-2", {"t0/console.txt": "", "t1/console.txt": ""})
    assert check.compare_files(first, second) == (
        ["t0/uart.gnss.jsonl: only in run-1", "t1/console.txt: only in run-2"],
        3,
    )


def test_only_a_recording_s_header_leaves_out_created(tmp_path: Path) -> None:
    other = json.dumps({"created": "2026-11-01T08:00:00Z"}) + "\n"
    assert check.comparable(other.encode()) == other.encode()
    assert check.comparable(b"not json\n") == b"not json\n"
    assert check.comparable(tap("a", [5]).encode()) == check.comparable(tap("b", [5]).encode())


def junit(path: Path, basetemp: Path, stats_t: int) -> None:
    link = {
        "stats": {"t": stats_t, "turns": 33},
        "routes": {"uart.gnss": {"tap": f"{basetemp}/t0/uart.gnss.jsonl"}},
    }
    value = json.dumps(link).replace('"', "&quot;")
    path.write_text(
        "<testsuites><testsuite>"
        '<testcase classname="tests.test_determinism" name="test_a_replay[esp32sim]">'
        f'<properties><property name="esp32sim_link" value="{value}"/></properties></testcase>'
        '<testcase classname="tests.test_units" name="test_seconds"/>'
        "</testsuite></testsuites>"
    )


def test_the_links_compare_without_the_run_s_basetemp(tmp_path: Path) -> None:
    junit(tmp_path / "run-1.xml", tmp_path / "run-1", 21_100_000_000)
    junit(tmp_path / "run-2.xml", tmp_path / "run-2", 21_100_000_000)
    assert check.compare_links(tmp_path, "run-1", "run-2") == ([], 1)


def test_a_basetemp_that_is_not_ascii_is_left_out_all_the_same(tmp_path: Path) -> None:
    # As the plugin writes it, the property escapes the path's letters: \u017c for ż.
    out = tmp_path / "zażółć"
    out.mkdir()
    junit(out / "run-1.xml", out / "run-1", 1)
    junit(out / "run-2.xml", out / "run-2", 1)
    assert "\\u017c" in (out / "run-1.xml").read_text()
    assert check.compare_links(out, "run-1", "run-2") == ([], 1)


def test_a_run_that_ended_elsewhere_is_shown_in_a_diff_of_the_link(tmp_path: Path) -> None:
    junit(tmp_path / "run-1.xml", tmp_path / "run-1", 21_100_000_000)
    junit(tmp_path / "run-2.xml", tmp_path / "run-2", 21_200_000_000)
    failures, tests = check.compare_links(tmp_path, "run-1", "run-2")
    assert tests == 1
    assert len(failures) == 1
    lines = failures[0].splitlines()
    assert lines[0] == "tests.test_determinism::test_a_replay[esp32sim]: the link differs:"
    assert '    -    "t": 21100000000,' in lines
    assert '    +    "t": 21200000000,' in lines


class Pytest:
    """Stands in for `subprocess.run` of pytest: records each command, leaves in the run's
    basetemp and JUnit XML what `left` says for that run, and exits with `code`."""

    def __init__(self, left: list[dict[str, str]], code: int = 0) -> None:
        self.left = left
        self.code = code
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], cwd: Path) -> subprocess.CompletedProcess[bytes]:
        self.commands.append(command)
        basetemp = Path(next(a for a in command if a.startswith("--basetemp=")).split("=", 1)[1])
        xml = Path(next(a for a in command if a.startswith("--junitxml=")).split("=", 1)[1])
        run(basetemp, self.left[len(self.commands) - 1])
        xml.write_text("<testsuites><testsuite/></testsuites>")
        return subprocess.CompletedProcess(command, self.code)


def test_pytest_gets_the_arguments_options_included_and_the_tests_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pytest_ = Pytest([{"t0/console.txt": "a\n"}] * 8)
    monkeypatch.setattr(check.subprocess, "run", pytest_)
    out = tmp_path / "out"
    assert check.main(["--out", str(out), "tests/test_nmea_reader.py", "-k", "fix"]) == 0
    assert check.main(["--out", str(out)]) == 0
    assert check.main(["--out", str(out), "-k", "fix", "--", "tests/t.py"]) == 0
    assert check.main(["--out", str(out), "--"]) == 0
    ours = [
        f"--basetemp={out / 'run-1'}",
        f"--junitxml={out}/run-1.xml",
        "-o",
        "junit_family=xunit1",
        "-p",
        "no:cacheprovider",
    ]
    assert [c[3:] for c in pytest_.commands[::2]] == [
        ["tests/test_nmea_reader.py", "-k", "fix", *ours],
        ["tests/test_determinism.py", *ours],
        # After a `--` pytest takes every argument as a path: the script's options come before.
        ["-k", "fix", *ours, "--", "tests/t.py"],
        ["tests/test_determinism.py", *ours],
    ]
    assert capsys.readouterr().out.endswith(
        "determinism: the two runs are identical: 1 file, and the link of 0 tests that used the"
        " board\n"
    )


def test_a_run_that_fails_ends_the_check_before_the_second(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pytest_ = Pytest([{}], code=1)
    monkeypatch.setattr(check.subprocess, "run", pytest_)
    assert check.main(["--out", str(tmp_path / "out")]) == 1
    assert len(pytest_.commands) == 1
    assert capsys.readouterr().out.endswith(
        "✗ run-1: pytest exited with code 1; the runs were not compared\n"
    )


def test_runs_that_differ_or_left_nothing_fail_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(check.subprocess, "run", Pytest([{"c.txt": "1\n"}, {"c.txt": "2\n"}]))
    assert check.main(["--out", str(tmp_path / "a")]) == 1
    assert capsys.readouterr().out.splitlines()[-7:] == [
        "✗ c.txt differs:",
        "    --- run-1/c.txt",
        "    +++ run-2/c.txt",
        "    @@ -1 +1 @@",
        "    -1",
        "    +2",
        "determinism: the two runs differ in 1 place",
    ]
    monkeypatch.setattr(check.subprocess, "run", Pytest([{}, {}]))
    assert check.main(["--out", str(tmp_path / "b")]) == 1
    assert capsys.readouterr().out.endswith(
        "✗ the runs left no file and no test that used the board: nothing was compared\n"
    )


def test_an_out_that_is_a_file_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "out").write_text("")
    assert check.main(["--out", str(tmp_path / "out")]) == 2
    assert capsys.readouterr().out == (
        f"✗ {tmp_path / 'out'} is not a directory: name an empty or new directory with --out\n"
    )


def test_an_out_directory_with_other_files_is_refused_and_an_earlier_check_s_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(check.subprocess, "run", Pytest([{"c.txt": ""}] * 2))
    run(tmp_path, {"keep.txt": "", "run-1/old.txt": "", "run-2.xml": ""})
    assert check.main(["--out", str(tmp_path)]) == 2
    assert capsys.readouterr().out == (
        f"✗ {tmp_path.resolve()} holds keep.txt, which no determinism check left there:"
        " name an empty or new directory with --out\n"
    )
    assert (tmp_path / "keep.txt").exists() and (tmp_path / "run-1" / "old.txt").exists()
    (tmp_path / "keep.txt").unlink()
    assert check.main(["--out", str(tmp_path)]) == 0
    assert not (tmp_path / "run-1" / "old.txt").exists()


def test_a_network_transcript_compares_exactly_after_a_tls_handshake_too() -> None:
    def transcript(late: int) -> bytes:
        events = [
            {"t_ns": 2, "event": "tls: handshake started"},
            {"t_ns": late, "event": "tls: handshake done, TLSv1.2"},
            {"t_ns": late, "stream": "8883/1", "direction": "out", "data": "616263"},
        ]
        return json.dumps({"esp32tl_network": 1, "events": events}).encode()

    assert check.comparable(transcript(3)) == transcript(3)
    assert check.comparable(transcript(3)) != check.comparable(transcript(4))
