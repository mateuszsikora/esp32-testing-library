"""Real firmware in independent xdist workers, compared with serial execution."""

import json
from pathlib import Path
from xml.etree import ElementTree

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_board_results_and_reports_match_serial_execution(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_parallel=f"""
        import json
        import time
        from pathlib import Path
        import pytest
        from esp32tl.units import Position, seconds

        @pytest.mark.app_path({str(ROOT / "firmware/nmea_reader")!r})
        @pytest.mark.parametrize("case", [0, 1])
        def test_board(case, board, gnss, console, card_cache, tmp_path, request):
            if hasattr(request.config, "workerinput"):
                ready = Path(__file__).parent / request.config.workerinput["workerid"]
                ready.touch()
                deadline = time.monotonic() + 30
                while len(list(ready.parent.glob("gw*"))) < 2:
                    assert time.monotonic() < deadline, "second worker did not start"
                    time.sleep(0.01)
            # Both workers contend for the same cache entry, without requiring mtools.
            source = tmp_path / "card.img"
            source.write_bytes(b"prepared card" * 1024)
            for _ in range(5):
                card_cache.put("shared", source)
                clone = card_cache.get("shared")
                assert clone.path.read_bytes() == source.read_bytes()
                clone.path.write_bytes(b"changed clone")
                assert card_cache.get("shared").path.read_bytes() == source.read_bytes()
            gnss.fix(at=Position(52.229677, 21.012233))
            board.power_on()
            board.wait(seconds(3))
            request.node.user_properties.append(("socket", str(board._socket)))
            request.node.user_properties.append(("temporary", str(tmp_path)))
            lines = [(line.at.ns, line.text) for line in console.lines()]
            assert any("fix acquired" in text for _, text in lines)
            request.node.user_properties.append(("observed", json.dumps(lines)))
        """
    )
    results = []
    for name, args, count in (
        ("alone", ["-n", "0", "-k", "esp32sim-0"], 1),
        ("serial", ["-n", "0"], 2),
        ("parallel", ["-n", "2"], 2),
    ):
        report = pytester.path / f"{name}.xml"
        run = pytester.runpytest_subprocess(*args, f"--junitxml={report}", timeout=180)
        run.assert_outcomes(passed=count)
        cases = ElementTree.parse(report).findall(".//testcase")
        assert len(cases) == count
        observed = {}
        sockets = set()
        directories = set()
        for case in cases:
            props = {
                p.attrib["name"]: p.attrib["value"] for p in case.findall("./properties/property")
            }
            assert json.loads(props["esp32sim_link"])
            sockets.add(props["socket"])
            directories.add(props["temporary"])
            observed[case.attrib["name"]] = json.loads(props["observed"])
        assert len(sockets) == len(directories) == count
        assert all(not Path(socket).exists() for socket in sockets)
        results.append(observed)
    assert results[1] == results[2]
    assert all(results[1][name] == value for name, value in results[0].items())


@pytest.mark.parametrize("workers", ["1", "2", "auto"])
def test_serial_is_rejected_before_opening_a_port(pytester: pytest.Pytester, workers: str) -> None:
    pytester.makepyfile("def test_board(board): pass")
    result = pytester.runpytest_subprocess(
        "-n", workers, "--esp32tl-backend", "serial", "--port", "/nonexistent", timeout=60
    )
    assert result.ret != 0
    assert "serial backend cannot run in pytest-xdist workers; use -n 0" in result.stderr.str()
    assert "SerialException" not in result.stderr.str()
