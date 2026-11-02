"""Matching with synthetic recordings; the times below are nanoseconds."""

from dataclasses import replace

import pytest

from esp32tl.matching import AT, GNSS, PMTK, UBX, Matching, Protocol
from esp32tl.recording import EMULATED, Direction, Event, Header, Recording

HEADER = Header("esp32-devkitc", "uart.gnss", "real", EMULATED, "2026-11-01T00:00:00Z")


def recording(*events: tuple[int, Direction, bytes], end: str = "hold") -> Recording:
    return Recording(
        replace(HEADER, end=end),
        tuple(Event(t, direction, data, EMULATED) for t, direction, data in events),
    )


def test_answers_wrap_per_request_including_recorded_timeouts() -> None:
    engine = Matching(
        recording(
            (10, "out", b"AT\r"),
            (15, "in", b"one"),
            (18, "in", b"two"),
            (20, "out", b"AT+X\r"),
            (23, "in", b"other"),
            (30, "out", b"AT\r"),  # The second AT was never answered.
            (40, "out", b"AT+X\r"),
            (49, "in", b"last"),
        )
    )
    assert engine.latency() == 3
    for t, request, expected in [
        (100, b"at \r", [(105, b"one"), (108, b"two")]),
        (200, b"AT\r", []),
        (300, b"AT+X\r", [(303, b"other")]),
        (400, b"AT\r", [(405, b"one"), (408, b"two")]),
        (500, b"AT+X\r", [(509, b"last")]),
        (600, b"AT+X\r", [(603, b"other")]),
    ]:
        assert engine.receive(t, request) == []
        assert engine.poll(t + 20) == expected
    assert engine.unseen_count == 0


def test_quiet_until_includes_stream_unknown_and_every_deferred_answer() -> None:
    engine = Matching(
        recording(
            (50, "in", b"boot"),
            (60, "out", b"AT\r"),
            (65, "in", b"fast"),
            (90, "in", b"slow"),
        )
    )
    assert engine.quiet_until() == 50
    engine.receive(10, b"AT\r")
    engine.receive(20, b"AT+UNKNOWN\r")
    engine.receive(21, b"at+unknown\r")
    assert engine.unseen_count == 2
    assert engine.unseen == {b"AT+UNKNOWN": 2}
    for t, expected in [
        (15, b"fast"),
        (25, b"\r\nERROR\r\n"),
        (26, b"\r\nERROR\r\n"),
        (40, b"slow"),
        (50, b"boot"),
    ]:
        assert engine.quiet_until() == t
        assert engine.poll(t - 1) == []
        assert engine.poll(t) == [(t, expected)]
    assert engine.quiet_until() is None


def test_request_time_is_its_final_fragment_and_chunk_boundaries_do_not_matter() -> None:
    engine = Matching(
        recording(
            (1, "out", b"A"),
            (5, "out", b"T\r"),
            (12, "in", b"ok"),
            (20, "out", b"AT+X\rAT+Y\r"),
            (30, "in", b"y"),
        )
    )
    assert engine.latency() == 7
    engine.receive(100, b"A")
    assert engine.quiet_until() is None
    engine.receive(200, b"T\rAT+X\rAT+Y\r")
    assert engine.poll(210) == [(207, b"ok"), (210, b"y")]
    assert engine.unseen_count == 0


def test_loop_repeats_unprompted_events_and_hold_stops() -> None:
    events: tuple[tuple[int, Direction, bytes], ...] = ((0, "in", b"first"), (10, "in", b"last"))
    hold = Matching(recording(*events))
    loop = Matching(recording(*events, end="loop"))
    assert hold.poll(20) == [(0, b"first"), (10, b"last")]
    assert hold.quiet_until() is None
    assert loop.poll(20) == [
        (0, b"first"),
        (10, b"last"),
        (10, b"first"),
        (20, b"last"),
        (20, b"first"),
    ]
    assert loop.quiet_until() == 30
    assert loop.latency() is None


@pytest.mark.parametrize("end", ["hold", "loop"])
def test_end_policy_does_not_discard_pending_answers_or_stop_matching(end: str) -> None:
    engine = Matching(recording((10, "out", b"AT\r"), (20, "in", b"ok"), end=end))
    for t in (100, 200):
        engine.receive(t, b"AT\r")
        assert engine.quiet_until() == t + 10
        assert engine.poll(t + 10) == [(t + 10, b"ok")]


def test_empty_loop_is_silent_and_zero_duration_stream_loop_is_refused() -> None:
    assert Matching(recording(end="loop")).quiet_until() is None
    with pytest.raises(ValueError, match="positive duration"):
        Matching(recording((0, "in", b"x"), end="loop"))


def test_explicit_protocol_answers_unknown_without_recorded_requests_or_delays() -> None:
    engine = Matching(recording((100, "in", b"boot")), AT)
    assert engine.latency() == 0
    engine.receive(50, b"AT\r")
    assert engine.quiet_until() == 50
    assert engine.poll(50) == [(50, b"\r\nERROR\r\n")]
    assert engine.quiet_until() == 100
    silent = Matching(recording((1, "out", b"AT\r")))
    assert silent.latency() == 0
    silent.receive(10, b"AT\r")
    assert silent.poll(10) == []


def test_pmtk_fallback_has_the_command_flag_one_and_checksum() -> None:
    engine = Matching(recording(), PMTK)
    engine.receive(100, b"$PMTK220,1000*1F\r")
    assert engine.quiet_until() is None
    engine.receive(110, b"\n")
    assert engine.poll(110) == [(110, b"$PMTK001,220,1*32\r\n")]


def test_ubx_is_length_framed_even_with_terminators_in_payload_and_naks_class_id() -> None:
    # CFG-MSG poll and its NAK, with checksums computed independently.
    request = bytes.fromhex("b56206010200010a1447")
    engine = Matching(recording(), UBX)
    for byte in request[:-1]:
        engine.receive(100, bytes([byte]))
        assert engine.quiet_until() is None
    engine.receive(101, request[-1:])
    assert engine.poll(101) == [(101, bytes.fromhex("b5620500020006010e33"))]
    assert engine.unseen_count == 1
    # CR/LF and sync bytes are opaque inside the length-delimited payload.
    opaque = bytes.fromhex("b562060104000d0ab5620000")
    engine = Matching(recording((1, "out", opaque), (5, "in", b"answer")), UBX)
    engine.receive(10, opaque + opaque)
    assert engine.poll(14) == [(14, b"answer"), (14, b"answer")]


def test_gnss_accepts_pmtk_and_ubx_on_one_wire() -> None:
    engine = Matching(recording(), GNSS)
    engine.receive(10, b"$PMTK220,1000*1F\r\n" + bytes.fromhex("b56206010200010a1447"))
    assert engine.poll(10) == [
        (10, b"$PMTK001,220,1*32\r\n"),
        (10, bytes.fromhex("b5620500020006010e33")),
    ]


def test_at_prompt_switches_next_request_to_opaque_ctrl_z_payload() -> None:
    engine = Matching(
        recording(
            (1, "out", b'AT+CMGS="123"\r'),
            (4, "in", b"\r\n>"),
            (5, "in", b" "),
            (10, "out", b"Hello\r\nWorld\x1a"),
            (20, "in", b"\r\nOK\r\n"),
        )
    )
    engine.receive(100, b'at +cmgs="123"\r')
    assert engine.poll(104) == [(103, b"\r\n>"), (104, b" ")]
    engine.receive(110, b"Hello\r\n")
    assert engine.quiet_until() is None
    engine.receive(120, b"World\x1a")
    assert engine.poll(130) == [(130, b"\r\nOK\r\n")]
    assert engine.unseen_count == 0
    assert AT.normalise(b'AT+X="Ab c"\r') != AT.normalise(b'AT+X="ab c"\r')


def test_custom_protocol_and_incomplete_recording() -> None:
    protocol = Protocol(b"!", normalise=lambda data: data.lower(), unknown_reply=b"no")
    engine = Matching(recording((1, "out", b"Yes!"), (2, "in", b"yes")), protocol)
    engine.receive(10, b"YES!WHAT!")
    assert engine.poll(11) == [(11, b"yes"), (11, b"no")]
    with pytest.raises(ValueError, match="incomplete request"):
        Matching(recording((1, "out", b"AT")))
    with pytest.raises(ValueError, match="terminator must not be empty"):
        Protocol(b"")


def test_a_loop_keeps_pending_answers_and_does_not_repeat_recorded_commands() -> None:
    engine = Matching(
        recording(
            (5, "in", b"boot"),
            (10, "out", b"AT\r"),
            (20, "in", b"answer"),
            end="loop",
        )
    )
    assert engine.poll(5) == [(5, b"boot")]
    engine.receive(14, b"AT\r")
    assert engine.quiet_until() == 24
    assert engine.poll(24) == [(24, b"answer")]
    assert engine.quiet_until() == 25
    assert engine.poll(25) == [(25, b"boot")]
    assert engine.quiet_until() == 45
    assert engine.poll(44) == []


def test_a_recorded_pmtk_command_matches_without_its_checksum() -> None:
    engine = Matching(
        recording(
            (1, "out", b"$PMTK220,1000*1F\r\n"),
            (8, "in", b"$PMTK001,220,3*30\r\n"),
        )
    )
    engine.receive(100, b"$pmtk220,1000\r\n")
    assert engine.poll(107) == [(107, b"$PMTK001,220,3*30\r\n")]
    assert engine.unseen_count == 0


def test_unknown_with_empty_fallback_is_counted_but_schedules_nothing() -> None:
    engine = Matching(recording(), PMTK)
    engine.receive(10, b"$NONPMTK\r\n")
    assert engine.unseen_count == 1
    assert engine.quiet_until() is None
    assert engine.poll(10) == []


def test_incomplete_binary_request_and_invalid_custom_framing_are_refused() -> None:
    with pytest.raises(ValueError, match="incomplete request"):
        Matching(recording((1, "out", bytes.fromhex("b5620601020001"))), UBX)
    with pytest.raises(ValueError, match="UBX request must start"):
        Matching(recording((1, "out", b"not ubx")), UBX)
    engine = Matching(recording(), Protocol(lambda data: 0))
    with pytest.raises(ValueError, match="frame length must be within"):
        engine.receive(1, b"x")


@pytest.mark.parametrize("response", [b"value > text\r\nOK\r\n", b"\r\n> text\r\nOK\r\n"])
def test_prompt_like_text_does_not_depend_on_response_fragmentation(response: bytes) -> None:
    # Include a transient line-start prompt followed by ordinary text, and a prompt-like
    # substring inside a line. Both compilation and live framing must accept the next AT.
    for cut in range(1, len(response)):
        for parts in ((response,), (response[:cut], response[cut:])):
            rows: list[tuple[int, Direction, bytes]] = [(1, "out", b"AT+VALUE\r")]
            rows += [(2 + i, "in", part) for i, part in enumerate(parts)]
            rows += [(10, "out", b"AT\r"), (20, "in", b"OK\r\n")]
            engine = Matching(recording(*rows), AT)
            engine.receive(100, b"AT+VALUE\r")
            for i, part in enumerate(parts):
                assert engine.poll(101 + i) == [(101 + i, part)]
            engine.receive(200, b"AT\r")
            assert engine.poll(210) == [(210, b"OK\r\n")]
            assert engine.unseen_count == 0


@pytest.mark.parametrize("recorded_ending", [(b"\r",), (b"\r\n",), (b"\r", b"\n")])
@pytest.mark.parametrize("live_ending", [(b"\r",), (b"\r\n",), (b"\r", b"\n")])
@pytest.mark.parametrize("payload", [b"hello\x1a", b"\n \r\nhello\x1a"])
def test_at_command_endings_do_not_change_prompted_payload_bytes(
    recorded_ending: tuple[bytes, ...],
    live_ending: tuple[bytes, ...],
    payload: bytes,
) -> None:
    command = b'AT+CMGS="123"'
    rows: list[tuple[int, Direction, bytes]] = [(1, "out", command + recorded_ending[0])]
    rows += [(2, "out", part) for part in recorded_ending[1:]]
    rows += [(5, "in", b"\r\n> "), (10, "out", payload), (20, "in", b"\r\nOK\r\n")]
    engine = Matching(recording(*rows), AT)
    assert payload in engine.answers
    engine.receive(100, command + live_ending[0])
    for part in live_ending[1:]:
        engine.receive(101, part)
    assert engine.poll(104) == [(104, b"\r\n> ")]
    # Preserve leading whitespace even when it is its own first payload fragment.
    engine.receive(110, payload[:1])
    engine.receive(111, payload[1:])
    assert engine.poll(121) == [(121, b"\r\nOK\r\n")]
    assert engine.unseen_count == 0


def test_unseen_history_retains_raw_occurrences_and_completion_times_without_a_reply() -> None:
    engine = Matching(
        recording((1, "out", b"AT\r"), (2, "in", b"OK")),
        replace(AT, unknown_reply=b""),
    )
    engine.receive(10, b"AT\r")  # Known requests never enter the unseen history.
    assert engine.poll(11) == [(11, b"OK")]
    engine.receive(20, b" at +missing ")
    assert engine.unseen_events == []
    engine.receive(25, b"\rAT+MISSING\r")
    engine.receive(30, b"AT+MISSING\r")
    assert engine.unseen_count == 3
    assert engine.unseen == {b"AT+MISSING": 3}
    assert engine.unseen_events == [
        (25, b" at +missing \r"),
        (25, b"AT+MISSING\r"),
        (30, b"AT+MISSING\r"),
    ]
    assert engine.quiet_until() is None
    assert engine.poll(40) == []
