"""Deterministic AT dialogue, independent of the runner and of the simulated network."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from esp32tl import boards, recording
from esp32tl.interfaces import SourceError
from esp32tl.interfaces.lines import Lines
from esp32tl.interfaces.modem import Modem as ModemRoute
from esp32tl.link.boardlink import BoardLink, Wire
from esp32tl.link.protocol import GpioIn, GpioOut, RunnerHello, Sync, UartCfg, UartTx, Wait
from esp32tl.report import RunLog
from esp32tl.sources.model.modem import LATENCY_NS, MAX_COMMAND, Modem
from esp32tl.units import Duration, hours, minutes, ms, seconds

LILYGO = boards.profile("lilygo-t-a7670g-r2")


def reply(model: Modem, command: str, t: int = 0) -> bytes:
    return b"".join(data for _, data in model.receive(t, command.encode() + b"\r\n"))


@pytest.mark.parametrize(
    ("command", "answer"),
    [
        ("AT", b"\r\nOK\r\n"),
        ("ATE0", b"\r\nOK\r\n"),
        ("AT+CPIN?", b"\r\n+CPIN: READY\r\n\r\nOK\r\n"),
        ("AT+CREG?", b"\r\n+CREG: 0,1\r\n\r\nOK\r\n"),
        ("AT+CGREG?", b"\r\n+CGREG: 0,1\r\n\r\nOK\r\n"),
        ("AT+CEREG?", b"\r\n+CEREG: 0,1\r\n\r\nOK\r\n"),
        ("AT+CSQ", b"\r\n+CSQ: 20,99\r\n\r\nOK\r\n"),
        ('AT+CGDCONT=1,"IP","internet"', b"\r\nOK\r\n"),
        ("AT+CFUN=1", b"\r\nOK\r\n"),
        ("AT+CTZU=1", b"\r\nOK\r\n"),
        ("AT+COPS=0", b"\r\nOK\r\n"),
        ("AT+COPS=?", b"\r\nOK\r\n"),
        ("AT+COPS?", b'\r\n+COPS: 0,0,"ESP32TL",7\r\n\r\nOK\r\n'),
        ("ATD*99#", b"\r\nCONNECT\r\n"),
        ('AT+CGDATA="PPP",1', b"\r\nCONNECT\r\n"),
        ("AT+NEWCOMMAND", b"\r\nOK\r\n"),
    ],
)
def test_the_at_dialogue(command: str, answer: bytes) -> None:
    model = Modem(lambda: 0)
    assert reply(model, command, 123) == answer
    assert [(c.at, c.text, c.response) for c in model.commands] == [
        (Duration(123), command, answer)
    ]
    assert model.commands[0].fallback == (command == "AT+NEWCOMMAND")


def test_command_framing_and_response_times_do_not_depend_on_uart_chunks() -> None:
    dialogue = b"+++AT\r\nnoiseAT\r\nATE0\nAT+CPIN?\rAT+CEREG?\r\n"
    reference = Modem(lambda: 0)
    expected = list(reference.receive(500, dialogue))
    for size in range(1, len(dialogue) + 1):
        model = Modem(lambda: 0)
        result = [
            output
            for i in range(0, len(dialogue), size)
            for output in model.receive(500, dialogue[i : i + size])
        ]
        assert result == expected
        assert model.commands == reference.commands
    assert [c.text for c in reference.commands] == ["AT", "ATE0", "AT+CPIN?", "AT+CEREG?"]
    assert all(t == 500 + LATENCY_NS for t, _ in expected)
    # The time of the command's last byte, not the first chunk's.
    model = Modem(lambda: 0)
    assert list(model.receive(1, b"AT+CS")) == []
    assert list(model.receive(2, b"Q")) == []
    assert next(iter(model.receive(3, b"\r")))[0] == 3 + LATENCY_NS
    assert model.commands[0].at == Duration(3)


def test_noise_and_command_mode_escape_never_leave_a_reply() -> None:
    model = Modem(lambda: 0)
    for data in (b"+", b"+", b"+", b"\r\n", b"noiseAT\r", b" AT\r", b"\xff\n"):
        assert list(model.receive(0, data)) == []
    assert reply(model, "AT+CEREG?").startswith(b"\r\n+CEREG: 0,1")
    assert len(model.commands) == 1


def test_an_overlong_line_is_discarded_and_the_next_command_is_read() -> None:
    model = Modem(lambda: 0)
    assert list(model.receive(0, b"AT" + b"x" * MAX_COMMAND + b"\r\n")) == []
    assert reply(model, "AT") == b"\r\nOK\r\n"


@pytest.mark.parametrize(
    ("dbm", "csq"), [(-200, 0), (-113, 0), (-111, 1), (-73, 20), (-51, 31), (0, 31)]
)
def test_signal_maps_to_csq(dbm: int, csq: int) -> None:
    model = Modem(lambda: 0)
    model.signal(dbm=dbm)
    assert f"+CSQ: {csq},99".encode() in reply(model, "AT+CSQ")


@pytest.mark.parametrize("name", ["CREG", "CGREG", "CEREG"])
def test_registration_and_function_changes_are_visible_at_the_next_query(name: str) -> None:
    model = Modem(lambda: 0)
    assert reply(model, f"AT+{name}=2") == b"\r\nOK\r\n"
    model.lose_registration()
    assert f"+{name}: 2,0".encode() in reply(model, f"AT+{name}?")
    assert reply(model, "ATD*99#") == b"\r\nNO CARRIER\r\n"
    model.register()
    assert f"+{name}: 2,1".encode() in reply(model, f"AT+{name}?")
    reply(model, "AT+CFUN=0")
    model.register()
    assert not model.registered
    reply(model, "AT+CFUN=1")
    assert model.registered
    reply(model, "AT+CFUN=4")
    assert b"+CFUN: 4" in reply(model, "AT+CFUN?")
    assert not model.registered


def test_echo_can_be_enabled_and_disabled() -> None:
    model = Modem(lambda: 0)
    assert reply(model, "ATE1") == b"\r\nOK\r\n"
    assert reply(model, "AT") == b"AT\r\n\r\nOK\r\n"
    assert reply(model, "ATE0") == b"ATE0\r\n\r\nOK\r\n"
    assert reply(model, "AT") == b"\r\nOK\r\n"


def test_dial_and_escape_change_parser_even_inside_a_chunk() -> None:
    for size in range(1, 40):
        model = Modem(lambda: 0)
        data = b"ATD*99#\r\n\x7ePPP\x7e+++AT+CSQ\r\n"
        outputs = [
            out for i in range(0, len(data), size) for out in model.receive(0, data[i : i + size])
        ]
        assert b"".join(d for _, d in outputs) == (
            b"\r\nCONNECT\r\n\r\nOK\r\n\r\n+CSQ: 20,99\r\n\r\nOK\r\n"
        )
        assert b"".join(d for _, d in model.data_received) == b"\x7ePPP\x7e"
        assert not model.data_mode
        assert [c.text for c in model.commands] == ["ATD*99#", "AT+CSQ"]


def test_data_is_passed_to_the_optional_peer_without_being_parsed_as_at() -> None:
    class Peer:
        def receive(self, t: int, data: bytes) -> list[tuple[int, bytes]]:
            return [(t + LATENCY_NS, data[::-1])]

        def poll(self, now: int) -> list[tuple[int, bytes]]:
            return [(now, b"poll")]

        def quiet_until(self) -> int:
            return 12345

        def latency(self) -> int:
            return LATENCY_NS // 2

    model = Modem(lambda: 0, data=Peer())
    assert list(model.poll(0)) == []
    reply(model, "ATD*99#")
    assert list(model.receive(10, b"AT\r\n")) == [(10 + LATENCY_NS, b"\n\rTA")]
    assert list(model.poll(20)) == [(20, b"poll")]
    assert model.quiet_until() == 12345
    assert model.latency() == LATENCY_NS // 2
    model.lose_registration()
    assert list(model.poll(30)) == [] and model.quiet_until() is None


def route(tmp_path: Path, now: list[int]) -> tuple[ModemRoute, Lines, RunLog]:
    log = RunLog()
    lines = Lines(LILYGO.name, LILYGO.lines, lambda: now[0], log=log)
    return (
        ModemRoute(
            "uart.modem",
            Wire(tx=26, rx=27, baud=115200),
            clock=recording.EMULATED,
            directory=tmp_path,
            now=lambda: now[0],
            lines=lines,
            pins=LILYGO.lines["modem"],
            log=log,
        ),
        lines,
        log,
    )


def test_model_selection_is_atomic_and_ri_has_one_driver(tmp_path: Path) -> None:
    modem, lines, _ = route(tmp_path, [0])
    with pytest.raises(ValueError):
        modem.signal(dbm=float("nan"))
    assert modem.source is None and lines.events() == []
    lines.drive("modem.ri", 1)
    with pytest.raises(SourceError, match="driven by the test already"):
        modem.power_on()
    assert modem.source is None
    lines.release("modem.ri")
    modem.power_on()
    changes: list[Callable[[], None]] = [
        lambda: lines.drive(33, 0),
        lambda: lines.release("modem.ri"),
    ]
    for change in changes:
        with pytest.raises(SourceError, match=r"driven by uart\.modem"):
            change()
    assert modem.powered
    assert list(lines.inputs(0))[-1] == GpioIn(0, ((33, 1),))


def test_named_controls_are_observable_without_changing_modem_state(tmp_path: Path) -> None:
    now = [0]
    modem, lines, _ = route(tmp_path, now)
    modem.register()
    reply(modem.model(), "ATE1")
    controls = {
        "modem.dtr": 25,
        "modem.reset": 5,
        "gnss.wake": 19,
        "gnss.pps": 23,
        "board.power_enable": 12,
    }
    for t, level in ((1, 1), (4, 0)):
        now[0] = seconds(t).ns
        lines.driven(
            now[0], [(pin, level) for pin in controls.values()], frozenset(controls.values())
        )
        assert all(lines.level(name) == level for name in controls)
        assert modem.powered and modem.registered
        assert reply(modem.model(), "AT", now[0]) == b"AT\r\n\r\nOK\r\n"


def test_powerkey_uses_profile_timing_and_power_loss_prevents_restart(tmp_path: Path) -> None:
    now = [0]
    modem, lines, log = route(tmp_path, now)
    modem.power_off()
    list(lines.inputs(0))
    # Short pulse: no start. Disabled output also ends the pulse.
    lines.driven(1, [(4, 1)], frozenset({4}))
    lines.driven(ms(49).ns, [(4, 0)], frozenset())
    assert not modem.powered
    lines.driven(seconds(1).ns, [(4, 1)], frozenset({4}))
    lines.driven(seconds(1).ns + ms(100).ns, [(4, 0)], frozenset({4}))
    assert modem.powered
    assert list(lines.inputs(seconds(1).ns + ms(100).ns)) == []
    assert lines.quiet_until() == seconds(1).ns + ms(101).ns
    assert list(lines.inputs(seconds(1).ns + ms(101).ns)) == [
        GpioIn(seconds(1).ns + ms(101).ns, ((33, 1),))
    ]
    assert lines.quiet_until() is None
    lines.driven(seconds(2).ns, [(4, 1)], frozenset({4}))
    lines.driven(seconds(5).ns, [(4, 0)], frozenset({4}))
    assert not modem.powered
    now[0] = seconds(6).ns
    modem.lose_power()
    lines.driven(seconds(7).ns, [(4, 1)], frozenset({4}))
    lines.driven(seconds(8).ns, [(4, 0)], frozenset({4}))
    assert not modem.powered
    modem.restore_power()
    lines.driven(seconds(9).ns, [(4, 1)], frozenset({4}))
    lines.driven(seconds(10).ns, [(4, 0)], frozenset({4}))
    assert modem.powered
    assert any(e.by == "model" and e.level == 1 for e in lines.events())
    assert any("6.000000000 s" in entry and "lose_power()" in entry for entry in log.lines())


def test_power_loss_clears_partial_commands_and_data_mode(tmp_path: Path) -> None:
    modem, lines, _ = route(tmp_path, [0])
    modem.power_on()
    reply(modem.model(), "ATD*99#")
    modem.lose_power()
    assert not modem.data_mode and not modem.registered
    assert reply(modem.model(), "AT") == b""
    assert list(lines.inputs(0))[-1] == GpioIn(0, ((33, 0),))
    modem.power_on()
    list(modem.model().receive(0, b"AT+"))
    modem.power_off()
    modem.power_on()
    assert reply(modem.model(), "AT") == b"\r\nOK\r\n"


def test_a_tap_and_report_keep_commands_even_after_the_wire_log_rolls_over(tmp_path: Path) -> None:
    modem, _, log = route(tmp_path, [0])
    tap = modem.tap("modem.jsonl")
    modem.signal(dbm=-73)
    for i in range(250):
        modem.receive(i, "out", b"AT+NEW\r\n")
    assert len(modem.commands) == 250
    assert "fallback OK" in modem.command_report()[1]
    assert "0.000000000 s" in modem.command_report()[1]
    assert len(log.lines()) < 250
    assert modem.recorded_source() == "model:modem"
    assert tap.events[0] == (0, "out", b"AT+NEW\r\n")
    assert tap.events[1] == (LATENCY_NS, "in", b"\r\nOK\r\n")


def test_the_real_link_routes_uart_and_gpio_without_duplicate_owners(tmp_path: Path) -> None:
    modem, lines, _ = route(tmp_path, [0])
    modem.power_on()
    link = BoardLink(LILYGO.name, [modem, lines])
    link.receive(
        RunnerHello(1, "esp32", "emulated-fifo-write", "8c652151dd5a521249a785ca147f20a9f8eda68f")
    )
    assert link.receive(Sync(0))[-1] == Wait(0, None, LATENCY_NS)
    link.receive(UartCfg(1, 0, 1, (26,), 27, 115200))
    link.receive(UartTx(2, 100, 1, (26,), 115200, b"AT\r"))
    outputs = link.receive(Sync(100 + LATENCY_NS))
    assert link.error is None, link.error
    assert any(getattr(output, "data", None) == b"\r\nOK\r\n" for output in outputs)
    link.receive(GpioOut(3, seconds(1).ns, ((4, 1),), (4,), (4,)))
    link.receive(Sync(seconds(1).ns + LATENCY_NS))
    link.receive(GpioOut(4, seconds(4).ns, ((4, 0),), (4,), (4,)))
    outputs = link.receive(Sync(seconds(4).ns + LATENCY_NS))
    assert GpioIn(seconds(4).ns + LATENCY_NS, ((33, 0),)) in outputs
    assert link.error is None, link.error


def test_a_replay_and_model_are_exclusive_and_reading_state_selects_neither(tmp_path: Path) -> None:
    modem, lines, _ = route(tmp_path, [0])
    with pytest.raises(SourceError, match="has no modem model"):
        assert modem.powered
    assert modem.source is None
    recording.write(
        tmp_path / "input.jsonl",
        recording.Recording(
            recording.Header(
                LILYGO.name, "uart.modem", "generated", recording.EMULATED, "2026-01-01T12:00:00Z"
            ),
            (),
        ),
    )
    modem.replay("input.jsonl")
    with pytest.raises(SourceError, match="has its source already"):
        modem.signal(dbm=-73)
    assert modem.source_kind == "replay"
    assert modem.recorded_source() == "replay"
    assert lines.latency() is None and lines.events() == []
    other, _, _ = route(tmp_path, [0])
    other.power_on()
    with pytest.raises(SourceError, match="has its source already"):
        other.replay("input.jsonl")


def test_power_loss_before_a_scheduled_ri_response_cancels_that_response(tmp_path: Path) -> None:
    now = [0]
    modem, lines, _ = route(tmp_path, now)
    modem.power_off()
    assert list(lines.inputs(0))[-1] == GpioIn(0, ((33, 0),))
    lines.driven(0, [(4, 1)], frozenset({4}))
    lines.driven(ms(100).ns, [(4, 0)], frozenset({4}))
    assert modem.powered
    # A test deadline ends the turn before the response's 1 ms latency elapses.
    now[0] = ms(100).ns + 500_000
    assert list(lines.inputs(now[0])) == []
    assert lines.quiet_until() == ms(101).ns
    modem.lose_power()
    assert list(lines.inputs(now[0])) == []  # RI was low and stays low.
    assert lines.quiet_until() is None
    assert list(lines.inputs(ms(101).ns)) == []
    assert not modem.powered
    assert not any(event.by == "model" and event.level == 1 for event in lines.events())


@pytest.mark.parametrize("command", ["ATD*99#", "ATD*99***1#", "ATD*99##", "ATDT*99#"])
def test_packet_data_dial_variants_connect_or_report_no_carrier(command: str) -> None:
    model = Modem(lambda: 0)
    assert reply(model, command) == b"\r\nCONNECT\r\n"
    assert model.data_mode and not model.commands[-1].fallback
    model.lose_registration()
    assert reply(model, command) == b"\r\nNO CARRIER\r\n"
    assert not model.data_mode and not model.commands[-1].fallback


def test_nominal_lilygo_pulses_work_even_when_a_tick_truncates_them() -> None:
    model = Modem(lambda: 0, pwrkey=LILYGO.lines["modem"]["pwrkey"])
    model.power_off()
    model.driven(0, 1)
    model.driven(ms(49.9).ns, 0)
    assert not model.powered
    model.driven(seconds(1).ns, 1)
    model.driven(seconds(1).ns + ms(99.9).ns, 0)
    assert model.powered
    model.driven(seconds(2).ns, 1)
    model.driven(seconds(2).ns + ms(2499.9).ns, 0)
    assert model.powered
    model.driven(seconds(5).ns, 1)
    model.driven(seconds(5).ns + ms(2999.9).ns, 0)
    assert not model.powered


@pytest.mark.parametrize("name", ["CREG", "CGREG", "CEREG"])
def test_registration_test_command_lists_modes_without_changing_the_setting(name: str) -> None:
    model = Modem(lambda: 0)
    reply(model, f"AT+{name}=2")
    assert reply(model, f"AT+{name}=?") == f"\r\n+{name}: (0-2)\r\n\r\nOK\r\n".encode()
    assert not model.commands[-1].fallback
    assert f"+{name}: 2,1".encode() in reply(model, f"AT+{name}?")


def test_echo_preserves_non_ascii_command_bytes() -> None:
    model = Modem(lambda: 0)
    reply(model, "ATE1")
    assert list(model.receive(0, b"AT+X=\xff\r")) == [(LATENCY_NS, b"AT+X=\xff\r\n\r\nOK\r\n")]
    assert model.commands[-1].text == "AT+X=\ufffd"


def test_only_sent_model_gpio_changes_appear_in_the_run_log(tmp_path: Path) -> None:
    now = [0]
    modem, lines, log = route(tmp_path, now)
    modem.power_off()
    list(lines.inputs(0))
    lines.driven(0, [(4, 1)], frozenset({4}))
    lines.driven(ms(99.9).ns, [(4, 0)], frozenset({4}))
    before = [entry for entry in log.lines() if "modem.ri in" in entry]
    assert len(before) == 1 and before[0].endswith("0")
    list(lines.inputs(ms(100.9).ns))
    now[0] = seconds(1).ns
    modem.power_off()
    list(lines.inputs(now[0]))
    changes = [entry.split() for entry in log.lines() if "modem.ri in" in entry]
    assert [(entry[0], entry[-1]) for entry in changes] == [
        ("0.000000000", "0"),
        ("0.100900000", "1"),
        ("1.000000000", "0"),
    ]
    # A cancelled response is never reported as a wire change.
    lines.driven(seconds(2).ns, [(4, 1)], frozenset({4}))
    lines.driven(seconds(2).ns + ms(99.9).ns, [(4, 0)], frozenset({4}))
    now[0] = seconds(2).ns + ms(100).ns
    modem.lose_power()
    list(lines.inputs(now[0]))
    assert len([entry for entry in log.lines() if "modem.ri in" in entry]) == 3


def test_a_model_selection_that_loses_to_replay_does_not_claim_ri(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from esp32tl.boards.capabilities import Request

    modem, lines, _ = route(tmp_path, [0])
    recording.write(
        tmp_path / "input.jsonl",
        recording.Recording(
            recording.Header(
                LILYGO.name, "uart.modem", "generated", recording.EMULATED, "2026-01-01T12:00:00Z"
            ),
            (),
        ),
    )
    asked, resume = Event(), Event()

    def pause_model(request: Request) -> None:
        if request.source == "model":
            asked.set()
            assert resume.wait(5)

    monkeypatch.setattr(modem, "asked", pause_model)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(modem.power_on)
        try:
            assert asked.wait(5)
            modem.replay("input.jsonl")
        finally:
            resume.set()
        with pytest.raises(SourceError, match="has its source already"):
            pending.result(timeout=5)
    assert modem.source_kind == "replay"
    assert lines.latency() is None and lines.events() == []
    lines.drive("modem.ri", 1)


def test_network_selection_guarded_escape_and_loss_reset(tmp_path: Path) -> None:
    now = [0]
    modem, _, _ = route(tmp_path, now)
    network = modem.network()
    assert modem.network() is network and modem.lossless
    model = modem.model()
    assert b"CONNECT" in reply(model, "ATD*99#")
    model.poll(1_000_000)
    # Pluses within an ordinary binary stream never escape, across UART chunks.
    model.receive(2_000_000, b"~abc++")
    model.receive(3_000_000, b"+xyz~")
    assert model.data_mode
    assert b"".join(data for _, data in model.data_received) == b"~abc+++xyz~"
    model.receive(2_000_000_000, b"+")
    model.receive(2_010_000_000, b"++")
    assert model.quiet_until() == 3_010_000_000
    assert list(model.poll(3_010_000_000)) == [(3_010_000_000, b"\r\nOK\r\n")]
    assert not model.data_mode and not network.active
    reply(model, "ATD*99#", 4_000_000_000)
    assert network.active
    model.lose_registration()
    assert not network.active and not network.ppp.started and not model.data_mode
    assert list(network.poll(5_000_000_000)) == []
    model.register()
    reply(model, "ATD*99#", 6_000_000_000)
    model.lose_power()
    assert not network.active and not network.ppp.started


def test_only_a_network_route_fails_on_receive_overflow(tmp_path: Path) -> None:
    from esp32tl.link.protocol import Warn

    for selected, pin, fails in [(True, 27, True), (True, 5, False), (False, 27, False)]:
        modem, _, _ = route(tmp_path, [0])
        if selected:
            modem.network()
        link = BoardLink("lilygo-t-a7670g-r2", interfaces=[modem])
        link.receive(RunnerHello(1, "esp32", "emulated-fifo-write", "revision"))
        warning = Warn(1, "rxfifo_ovf", "dropped", pin=pin, port=1, nbytes=1)
        link.receive(warning)
        assert (link.error is not None) is fails
        assert link.warnings == [warning]
        if fails:
            assert "uart.modem: UART receive overflow (rxfifo_ovf) on GPIO 27" in str(link.error)


def test_the_board_s_supply_and_the_test_s_lose_power_are_apart(tmp_path: Path) -> None:
    now = [0]
    modem, lines, _ = route(tmp_path, now)
    modem.signal(dbm=-73)
    modem.lose_power()
    modem.supply(False)
    modem.supply(True)
    # the board's power is back, the modem's own supply is still the test's to give back
    lines.driven(seconds(1).ns, [(4, 1)], frozenset({4}))
    lines.driven(seconds(1).ns + ms(100).ns, [(4, 0)], frozenset({4}))
    assert not modem.powered
    modem.restore_power()
    lines.driven(seconds(2).ns, [(4, 1)], frozenset({4}))
    lines.driven(seconds(2).ns + ms(100).ns, [(4, 0)], frozenset({4}))
    assert modem.powered
    # and the test's power_on waits for the board's supply
    modem.supply(False)
    modem.power_on()
    assert not modem.powered
    modem.supply(True)
    assert not modem.powered


def test_a_modem_on_the_board_s_supply_waits_for_pwrkey_when_it_comes_back(
    tmp_path: Path,
) -> None:
    now = [0]
    modem, lines, _ = route(tmp_path, now)
    modem.signal(dbm=-73)
    assert modem.powered
    modem.supply(False)
    assert not modem.powered
    assert list(lines.inputs(0))[-1] == GpioIn(0, ((33, 0),))
    modem.supply(True)
    assert not modem.powered
    lines.driven(seconds(1).ns, [(4, 1)], frozenset({4}))
    lines.driven(seconds(1).ns + ms(100).ns, [(4, 0)], frozenset({4}))
    assert modem.powered


def test_cclk_is_the_world_s_clock_at_the_command_s_instant() -> None:
    world = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    model = Modem(lambda: 0, world=lambda: world)
    assert reply(model, "AT+CCLK?") == b'\r\n+CCLK: "26/01/01,12:00:00+00"\r\n\r\nOK\r\n'
    assert not model.commands[-1].fallback
    assert b'"26/01/01,12:00:05+00"' in reply(model, "AT+CCLK?", seconds(5.9).ns)
    assert b'"26/01/01,12:01:05+00"' in reply(model, "AT+CCLK?", seconds(65).ns)


@pytest.mark.parametrize(
    ("wrong_by", "zone", "answer"),
    [
        (hours(-1), hours(2), b'"26/01/01,13:00:05+08"'),
        (seconds(0), hours(-2), b'"26/01/01,10:00:05-08"'),
        (seconds(0), minutes(15), b'"26/01/01,12:15:05+01"'),
        (seconds(0), minutes(-15), b'"26/01/01,11:45:05-01"'),
        (seconds(0), seconds(0), b'"26/01/01,12:00:05+00"'),
        (seconds(0), hours(24), b'"26/01/02,12:00:05+96"'),
        (seconds(0), hours(-24), b'"25/12/31,12:00:05-96"'),
        (hours(24), seconds(0), b'"26/01/02,12:00:05+00"'),
    ],
)
def test_cclk_offsets_the_time_and_gives_the_zone_in_quarters_of_an_hour(
    wrong_by: Duration, zone: Duration, answer: bytes
) -> None:
    world = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    model = Modem(lambda: 0, world=lambda: world)
    model.clock(wrong_by=wrong_by, zone=zone)
    assert answer in reply(model, "AT+CCLK?", seconds(5).ns)


@pytest.mark.parametrize(
    ("zone", "shown"),
    [
        (minutes(20), "20"),
        (minutes(-20), "-20"),
        (hours(24) + minutes(15), "1455"),
        (hours(-25), "-1500"),
        (seconds(1), "0.016667"),
    ],
)
def test_clock_refuses_a_zone_that_is_no_quarter_of_an_hour_or_out_of_range(
    zone: Duration, shown: str
) -> None:
    with pytest.raises(ValueError) as refused:
        Modem(lambda: 0).clock(zone=zone)
    assert str(refused.value) == (
        f"modem.clock: zone is {shown} minutes: a whole number of quarters of an hour"
        " from -24 to +24 hours"
    )


@pytest.mark.parametrize("arguments", [{"zone": 2}, {"wrong_by": 3600.0}])
def test_clock_takes_durations_only(arguments: dict[str, object]) -> None:
    with pytest.raises(TypeError, match="not a Duration"):
        Modem(lambda: 0).clock(**arguments)  # type: ignore[arg-type]


def test_an_unset_clock_has_no_zone_and_runs_on_from_the_call() -> None:
    now = [seconds(10).ns]
    model = Modem(lambda: now[0], world=lambda: datetime(2026, 1, 1, tzinfo=UTC))
    model.clock(wrong_by=hours(1), zone=hours(2))
    model.clock_not_set(reads=datetime(2000, 1, 1, tzinfo=UTC))
    assert reply(model, "AT+CCLK?", seconds(10).ns) == (
        b'\r\n+CCLK: "00/01/01,00:00:00"\r\n\r\nOK\r\n'
    )
    assert b'"00/01/01,00:01:05"' in reply(model, "AT+CCLK?", seconds(75).ns)
    model.clock_set_by_network()
    assert b'"26/01/01,00:01:15+00"' in reply(model, "AT+CCLK?", seconds(75).ns)


def test_an_unset_clock_needs_what_it_reads() -> None:
    model = Modem(lambda: 0)
    with pytest.raises(TypeError, match="reads"):
        model.clock_not_set()  # type: ignore[call-arg]
    naive = datetime(2000, 1, 1)
    with pytest.raises(TypeError, match="not a datetime with a timezone"):
        model.clock_not_set(reads=naive)
    other = datetime(2000, 1, 1, 12, tzinfo=timezone(timedelta(hours=2)))
    model.clock_not_set(reads=other)
    assert b'"00/01/01,12:00:00"' in reply(model, "AT+CCLK?")


def test_setting_the_clock_stays_a_fallback() -> None:
    model = Modem(lambda: 0)
    assert reply(model, 'AT+CCLK="26/01/01,12:00:00+00"') == b"\r\nOK\r\n"
    assert model.commands[-1].fallback
    assert b'"26/01/01,12:00:00+00"' in reply(model, "AT+CCLK?")


@pytest.mark.parametrize(
    ("command", "answer"),
    [
        ("AT+COPS=3,0", b'+COPS: 0,0,"Example Mobile",7'),
        ("AT+COPS=3,1", b'+COPS: 0,1,"EXM",7'),
        ("AT+COPS=3,2", b'+COPS: 0,2,"26001",7'),
    ],
)
def test_the_operator_format_chooses_what_cops_names(command: str, answer: bytes) -> None:
    model = Modem(lambda: 0)
    model.operator(name="Example Mobile", numeric="26001", short="EXM", act=7)
    assert reply(model, command) == b"\r\nOK\r\n"
    assert not model.commands[-1].fallback
    assert answer in reply(model, "AT+COPS?")


def test_the_default_operator_s_code_matches_the_imsi_and_short_falls_back_to_the_name() -> None:
    model = Modem(lambda: 0)
    assert b'+COPS: 0,1,"ESP32TL",7' in reply(model, "AT+COPS=3,1") + reply(model, "AT+COPS?")
    assert b'+COPS: 0,2,"26000",7' in reply(model, "AT+COPS=3,2") + reply(model, "AT+COPS?")
    assert reply(model, "AT+CIMI").startswith(b"\r\n26000")


@pytest.mark.parametrize("command", ["AT+COPS=3", "AT+COPS=3,3", "AT+COPS=3,", "AT+COPS=3,2,1"])
def test_any_other_operator_format_is_an_error_and_changes_nothing(command: str) -> None:
    model = Modem(lambda: 0)
    reply(model, "AT+COPS=3,2")
    assert reply(model, command) == b"\r\nERROR\r\n"
    assert not model.commands[-1].fallback
    assert b'+COPS: 0,2,"26000",7' in reply(model, "AT+COPS?")


def test_the_operator_format_goes_back_at_a_power_cycle_and_clock_and_operator_stay() -> None:
    world = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    model = Modem(lambda: 0, world=lambda: world)
    model.operator(name="Example Mobile", numeric="260010", act=2)
    model.clock(zone=hours(2))
    reply(model, "AT+COPS=3,2")
    model.power_off()
    model.power_on()
    assert b'+COPS: 0,0,"Example Mobile",2' in reply(model, "AT+COPS?")
    assert b'+08"' in reply(model, "AT+CCLK?")
    reply(model, "AT+COPS=3,2")
    assert b'+COPS: 0,2,"260010",2' in reply(model, "AT+COPS?")


def test_an_unregistered_modem_names_no_operator() -> None:
    model = Modem(lambda: 0)
    reply(model, "AT+COPS=3,2")
    model.lose_registration()
    assert reply(model, "AT+COPS?") == b"\r\n+COPS: 0\r\n\r\nOK\r\n"


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"numeric": "2600"}, "modem.operator: numeric is '2600', not 5 or 6 digits"),
        ({"numeric": "2600000"}, "modem.operator: numeric is '2600000', not 5 or 6 digits"),
        ({"numeric": "2600a"}, "modem.operator: numeric is '2600a', not 5 or 6 digits"),
        ({"numeric": 26000}, "modem.operator: numeric is 26000, not 5 or 6 digits"),
        ({"name": ""}, "modem.operator: name is '', not text for quotes"),
        ({"short": 'a"b'}, "modem.operator: short is 'a\"b', not text for quotes"),
        ({"act": True}, "modem.operator: act is True, not a whole number"),
        ({"act": -1}, "modem.operator: act is -1, not a whole number"),
    ],
)
def test_operator_refuses_what_cops_could_not_say(
    arguments: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError) as refused:
        Modem(lambda: 0).operator(**arguments)  # type: ignore[arg-type]
    assert str(refused.value) == message


def test_the_clock_and_operator_are_steps_of_the_run(tmp_path: Path) -> None:
    modem, _, log = route(tmp_path, [0])
    modem.clock(zone=hours(2))
    modem.clock_not_set(reads=datetime(2000, 1, 1, tzinfo=UTC))
    modem.clock_set_by_network()
    modem.operator(numeric="26001")
    logged = "\n".join(log.lines())
    for step in (
        "clock(wrong_by=",
        "clock_not_set(reads=",
        "clock_set_by_network()",
        "operator(name=",
    ):
        assert f"uart.modem.{step}" in logged
    modem.model()


def test_a_refused_clock_or_operator_leaves_the_route_without_a_source(tmp_path: Path) -> None:
    modem, _, _ = route(tmp_path, [0])
    with pytest.raises(ValueError):
        modem.clock(zone=minutes(20))
    with pytest.raises(ValueError):
        modem.operator(numeric="2600")
    assert modem.source is None
