"""The GNSS receiver model (`esp32tl/sources/model/gnss.py`) on a clock of the test's own, without
an emulator: its sentences against NMEA worked out by hand, their checksums, when its epochs fall,
and how what the test tells it changes them, moving on the great circle in both hemispheres."""

import struct
from datetime import UTC, datetime
from functools import reduce
from pathlib import Path

import pytest

from esp32tl import recording
from esp32tl.interfaces import SourceError
from esp32tl.interfaces.gnss import Gnss
from esp32tl.link.boardlink import Wire
from esp32tl.report import RunLog
from esp32tl.sources.model.gnss import GnssReceiver, ModelError, angle, casic, sentence
from esp32tl.units import (
    EAST,
    NORTH,
    Duration,
    Heading,
    Position,
    about,
    km,
    km_h,
    knots,
    m,
    m_s,
    ms,
    seconds,
)

WARSAW = Position(52.229677, 21.012233)
BUENOS_AIRES = Position(-34.6037, -58.3816)
POWER_ON = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
S = 1_000_000_000


class Board:
    """The board's clock, as far as the receiver sees it: what `now` says, and the world's time
    at power-on."""

    def __init__(self, utc: datetime = POWER_ON) -> None:
        self.t = 0
        self.utc = utc
        self.receiver = GnssReceiver(lambda: self.t, lambda: self.utc, baud=BAUD)

    def run_to(self, t: int) -> list[tuple[int, bytes]]:
        """What the link would get from the turn at `t`."""
        self.t = t
        return list(self.receiver.poll(t))


def checksummed(body: str) -> bytes:
    check = reduce(lambda a, c: a ^ c, body.encode(), 0)
    return f"${body}*{check:02X}\r\n".encode()


def fields(epoch: bytes) -> list[list[str]]:
    """The fields of each sentence of `epoch`, its checksum checked."""
    found = []
    for line in epoch.decode().split("\r\n")[:-1]:
        body, _, check = line[1:].partition("*")
        assert checksummed(body) == f"{line}\r\n".encode()
        assert int(check, 16) == reduce(lambda a, c: a ^ ord(c), body, 0)
        found.append(body.split(","))
    return found


def position_of(rmc: list[str]) -> Position:
    """The position an RMC's fields give, decoded here rather than by the model."""
    lat = int(rmc[3][:2]) + float(rmc[3][2:]) / 60
    lon = int(rmc[5][:3]) + float(rmc[5][3:]) / 60
    return Position(-lat if rmc[4] == "S" else lat, -lon if rmc[6] == "W" else lon)


def test_a_fix_is_an_rmc_and_a_gga_with_the_same_time_once_a_second() -> None:
    board = Board()
    board.receiver.fix(at=WARSAW)
    assert board.run_to(0) == []
    assert board.receiver.quiet_until() == S
    # 52.229677° is 52° 13.78062', 21.012233° is 21° 0.73398'; 12:00:01 UTC on 1 January 2026.
    assert board.run_to(S) == [
        (
            S,
            b"$GPRMC,120001.00,A,5213.78062,N,02100.73398,E,0.00,,010126,,,A*4D\r\n"
            b"$GPGGA,120001.00,5213.78062,N,02100.73398,E,1,08,0.90,0.0,M,0.0,M,,*64\r\n",
        )
    ]
    # A turn that comes late gets every epoch due, each at its own time.
    epochs = board.run_to(3 * S + 500_000_000)
    assert [t for t, _ in epochs] == [2 * S, 3 * S]
    assert [fields(data)[0][1] for _, data in epochs] == ["120002.00", "120003.00"]
    assert board.receiver.quiet_until() == 4 * S
    assert board.receiver.sent == 3


def test_without_a_fix_it_talks_and_says_so() -> None:
    board = Board()
    # The first call of the model, as a cold start: talking, no fix.
    board.receiver.go_silent()
    board.receiver.resume()
    assert board.receiver.position is None
    assert board.run_to(S) == [
        (
            S,
            b"$GPRMC,120001.00,V,,,,,,,010126,,,N*7B\r\n"
            b"$GPGGA,120001.00,,,,,0,00,99.99,,,,,,*64\r\n",
        )
    ]


def test_the_southern_and_western_hemispheres_and_the_world_s_date() -> None:
    # Power-on a second before midnight at the end of 2026: the first epoch is in 2027.
    board = Board(datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC))
    board.receiver.fix(at=BUENOS_AIRES)
    # 34.6037° is 34° 36.222', 58.3816° is 58° 22.896'.
    assert board.run_to(S) == [
        (
            S,
            b"$GPRMC,000000.00,A,3436.22200,S,05822.89600,W,0.00,,010127,,,A*40\r\n"
            b"$GPGGA,000000.00,3436.22200,S,05822.89600,W,1,08,0.90,0.0,M,0.0,M,,*68\r\n",
        )
    ]


def test_epochs_fall_on_whole_seconds_of_the_world_s_time() -> None:
    # Power-on at 12:00:00.250: the first whole second at least 1 s later is 12:00:02, at 1.75 s
    # of the board's time.
    board = Board(datetime(2026, 1, 1, 12, 0, 0, 250_000, tzinfo=UTC))
    assert board.run_to(S) == []
    assert board.receiver.quiet_until() == 1_750_000_000
    ((t, data),) = board.run_to(1_750_000_000)
    assert t == 1_750_000_000 and fields(data)[0][1] == "120002.00"


def test_a_move_goes_on_while_the_board_runs_on_the_great_circle() -> None:
    board = Board()
    board.receiver.fix(at=WARSAW)
    board.run_to(10 * S)
    board.receiver.move(heading=NORTH, speed=km_h(36))
    # 100 s at 10 m/s: a kilometre north, 19.44 knots on 0.0°.
    (_, epoch), *_ = board.run_to(110 * S)[-1:]
    rmc, gga = fields(epoch)
    assert position_of(rmc) == about(WARSAW.moved(NORTH, km(1)), within=m(0.05))
    assert WARSAW.distance_to(position_of(rmc)) == about(km(1), within=m(0.05))
    assert (rmc[7], rmc[8]) == ("19.44", "0.0")
    assert gga[2:6] == rmc[3:7]
    assert board.receiver.position == about(WARSAW.moved(NORTH, km(1)), within=m(0.001))


def test_the_course_is_the_great_circle_s_heading_where_the_receiver_is() -> None:
    board = Board()
    board.receiver.fix(at=WARSAW)
    board.receiver.move(heading=EAST, speed=m_s(100))
    (_, epoch), *_ = board.run_to(1000 * S)[-1:]
    rmc, _ = fields(epoch)
    here = WARSAW.moved(EAST, km(100))
    # Heading east from 52° N, the great circle turns south: 100 km on, it heads where the next
    # step along it goes, by about the longitude it crossed times the sine of the latitude,
    # 100 km / (R cos 52.23°) * sin 52.23° = 0.0202 rad, 1.16°.
    ahead = here.bearing_to(WARSAW.moved(EAST, km(100.001))).degrees
    assert float(rmc[8]) == pytest.approx(ahead, abs=0.05)
    assert float(rmc[8]) == pytest.approx(91.2, abs=0.05)
    # Turned the way the course goes now, which the sentence gives to 0.1°: up to 9 m off over
    # the next 10 km.
    board.receiver.stand_still()
    assert board.receiver.position == about(here, within=m(0.01))
    board.receiver.move(heading=Heading(float(rmc[8])), speed=m_s(100))
    (_, epoch), *_ = board.run_to(1100 * S)[-1:]
    assert position_of(fields(epoch)[0]) == about(WARSAW.moved(EAST, km(110)), within=m(9))


def test_a_course_that_rounds_to_360_is_0() -> None:
    board = Board()
    board.receiver.fix(at=Position(0, 0))
    board.receiver.move(heading=Heading(359.96), speed=knots(1))
    ((_, epoch),) = board.run_to(S)
    assert fields(epoch)[0][7:9] == ["1.00", "0.0"]


def test_a_lost_fix_says_no_fix_and_the_receiver_goes_on_moving() -> None:
    board = Board()
    board.receiver.fix(at=WARSAW)
    board.receiver.move(heading=EAST, speed=m_s(10))
    board.run_to(10 * S)
    board.receiver.lose_fix()
    ((_, epoch),) = board.run_to(11 * S)
    rmc, gga = fields(epoch)
    assert (rmc[2], rmc[3], gga[6], gga[7], gga[8]) == ("V", "", "0", "00", "99.99")
    assert len(board.run_to(20 * S)) == 9
    assert board.receiver.position == about(WARSAW.moved(EAST, m(200)), within=m(0.01))
    # Back where it is now, standing.
    here = board.receiver.position
    assert here is not None
    board.receiver.fix(at=here)
    ((_, epoch),) = board.run_to(21 * S)
    rmc, gga = fields(epoch)
    assert rmc[2] == "A" and rmc[7:9] == ["0.00", ""] and gga[6:9] == ["1", "08", "0.90"]
    assert position_of(rmc) == about(here, within=m(0.05))


def test_degrade_changes_the_hdop_and_the_satellites_of_the_fix() -> None:
    board = Board()
    board.receiver.fix(at=WARSAW, hdop=1.2, satellites=11)
    ((_, epoch),) = board.run_to(S)
    assert fields(epoch)[1][7:9] == ["11", "1.20"]
    board.receiver.degrade(hdop=4.5)
    ((_, epoch),) = board.run_to(2 * S)
    assert fields(epoch)[1][7:9] == ["11", "4.50"]
    board.receiver.degrade(satellites=4)
    ((_, epoch),) = board.run_to(3 * S)
    assert fields(epoch)[1][6:9] == ["1", "04", "4.50"]


def test_a_silent_receiver_sends_nothing_until_it_resumes() -> None:
    board = Board()
    board.receiver.fix(at=WARSAW)
    board.run_to(S)
    board.receiver.go_silent()
    # Silent, it bounds no turn.
    assert board.receiver.quiet_until() is None
    assert board.run_to(5 * S + 300_000_000) == []
    board.receiver.resume()
    assert board.run_to(5 * S + 300_000_000) == []
    # From the next epoch on.
    assert board.receiver.quiet_until() == 6 * S
    assert [t for t, _ in board.run_to(6 * S)] == [6 * S]
    assert board.receiver.sent == 2


def test_the_epochs_of_a_silence_stay_unsent_when_no_turn_came_in_it() -> None:
    board = Board()
    board.receiver.fix(at=WARSAW)
    board.run_to(S)
    board.receiver.go_silent()
    # Silent, it bounds no turn: the next one is the test's, at 11.1 s, where it resumes before
    # the link polls it.
    board.t = 11_100_000_000
    board.receiver.resume()
    assert board.run_to(11_100_000_000) == []
    assert board.receiver.quiet_until() == 12 * S


def test_each_change_holds_from_the_board_s_instant() -> None:
    board = Board()
    board.receiver.fix(at=WARSAW)
    ((_, before),) = board.run_to(2 * S)[-1:]
    board.t = 2_500_000_000
    board.receiver.move(heading=NORTH, speed=m_s(10))
    board.t = 3 * S
    assert WARSAW.distance_to(board.receiver.position or WARSAW) == about(m(5), within=m(0.001))
    ((_, after),) = board.run_to(3 * S)
    # The epoch at 2 s, before the move, stood; the one at 3 s is 5 m on.
    assert WARSAW.distance_to(position_of(fields(before)[0])) == about(m(0), within=m(0.02))
    assert WARSAW.distance_to(position_of(fields(after)[0])) == about(m(5), within=m(0.02))


def test_what_the_receiver_cannot_be_is_refused() -> None:
    board = Board()
    with pytest.raises(ModelError, match=r"^gnss.move: the receiver has no position to move from"):
        board.receiver.move(heading=NORTH, speed=km_h(10))
    with pytest.raises(ModelError, match=r"^gnss.degrade: the receiver has no fix to degrade"):
        board.receiver.degrade(hdop=2)
    board.receiver.fix(at=WARSAW)
    with pytest.raises(ModelError, match="speed is -1 m/s, less than 0"):
        board.receiver.move(heading=NORTH, speed=m_s(-1))
    with pytest.raises(TypeError, match="heading is 90, not a Heading, as NORTH"):
        board.receiver.move(heading=90, speed=km_h(10))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match=r"speed is 10, not a Speed, as km_h\(50\)"):
        board.receiver.move(heading=NORTH, speed=10)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match=r"^gnss.fix: at is \(52, 21\), not a Position$"):
        board.receiver.fix(at=(52, 21))  # type: ignore[arg-type]
    with pytest.raises(ModelError, match=r"hdop is 0, not from 0\.01 to 99\.99"):
        board.receiver.fix(at=WARSAW, hdop=0)
    # Below what the GGA's two decimals can say.
    with pytest.raises(ModelError, match=r"hdop is 0\.001, not from 0\.01 to 99\.99"):
        board.receiver.degrade(hdop=0.001)
    with pytest.raises(ModelError, match=r"satellites is 0, not from 1 to 99:"):
        board.receiver.degrade(satellites=0)
    with pytest.raises(TypeError, match=r"satellites is 4\.0, not a whole number"):
        board.receiver.degrade(satellites=4.0)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="give it hdop=, satellites=, or both"):
        board.receiver.degrade()
    # Nothing refused changed it.
    assert board.receiver.position == WARSAW


def test_coordinates_carry_into_the_next_degree_when_they_round() -> None:
    assert angle(52.999999999, 2, "N", "S") == "5300.00000,N"
    assert angle(-0.5, 3, "E", "W") == "00030.00000,W"
    assert angle(90, 2, "N", "S") == "9000.00000,N"
    assert angle(-180, 3, "E", "W") == "18000.00000,W"


def test_a_sentence_is_checksummed_and_at_most_82_characters() -> None:
    assert sentence("GPGSV,1,1,00") == checksummed("GPGSV,1,1,00")
    # The longest the model makes: both 3-digit fields, a fast receiver, a 3-digit course.
    board = Board()
    board.receiver.fix(at=Position(-89.99, -179.99), hdop=99.99, satellites=99)
    board.receiver.move(heading=Heading(359.9), speed=knots(999.99))
    ((_, epoch),) = board.run_to(ms(1000).ns)
    assert all(len(line) + 2 <= 82 for line in epoch.split(b"\r\n")[:-1])
    with pytest.raises(ModelError, match="longer than NMEA's 82 characters"):
        sentence("GPTXT," + "x" * 80)
    assert seconds(1).ns == S


def test_a_change_at_an_epoch_s_instant_is_in_that_epoch() -> None:
    board = Board()
    board.receiver.fix(at=WARSAW)
    board.run_to(S)
    # The test's turn at 2 s comes before the link polls the receiver there.
    board.t = 2 * S
    board.receiver.lose_fix()
    ((t, epoch),) = board.run_to(2 * S)
    assert t == 2 * S and fields(epoch)[0][2] == "V"
    board.receiver.go_silent()
    board.t = 4 * S
    # Resumed at an epoch's instant: that epoch goes, the one at 3 s does not.
    board.receiver.resume()
    assert [t for t, _ in board.run_to(4 * S)] == [4 * S]


def test_the_receiver_moves_across_the_antimeridian_and_over_a_pole() -> None:
    board = Board()
    board.receiver.fix(at=Position(0, 179.995))
    board.receiver.move(heading=EAST, speed=m_s(10))
    (_, epoch), *_ = board.run_to(100 * S)[-1:]
    rmc, _ = fields(epoch)
    # 1 km east of 179.995° E is 179.996° W: 179° 59.76'.
    assert (rmc[5][:7], rmc[6], rmc[8]) == ("17959.7", "W", "90.0")
    assert position_of(rmc) == about(Position(0, 179.995).moved(EAST, km(1)), within=m(0.05))
    # 0.001° short of the north pole, 111 m, then a kilometre on: past it, on the far meridian,
    # heading south. cos(lat) is no help at the pole itself, and the course comes from the start.
    board = Board()
    board.receiver.fix(at=Position(89.999, 10))
    board.receiver.move(heading=NORTH, speed=m_s(10))
    (_, epoch), *_ = board.run_to(100 * S)[-1:]
    rmc, _ = fields(epoch)
    assert (rmc[4], rmc[6], rmc[8]) == ("N", "W", "180.0")
    assert position_of(rmc) == about(Position(89.999, 10).moved(NORTH, km(1)), within=m(0.05))
    assert position_of(rmc).lon == pytest.approx(-170, abs=1e-4)


def gnss_route(t: list[int], log: RunLog | None = None) -> Gnss:
    """A `Gnss` on the devkit's GNSS pins, its board's time `t[0]`, off the link."""
    return Gnss(
        "uart.gnss",
        Wire(tx=4, rx=5, baud=9600),
        clock=recording.EMULATED,
        directory=Path(__file__).resolve().parent.parent / "recordings",
        now=lambda: t[0],
        world=lambda: POWER_ON,
        log=log,
    )


def test_a_first_model_call_that_is_refused_leaves_the_route_without_a_source() -> None:
    route = gnss_route([0])
    with pytest.raises(TypeError, match="not a Position"):
        route.fix(at=(52, 21))  # type: ignore[arg-type]
    with pytest.raises(ModelError, match="no position to move from"):
        route.move(heading=NORTH, speed=km_h(10))
    with pytest.raises(TypeError, match="give it hdop=, satellites=, or both"):
        route.degrade()
    assert route.source is None and route.source_kind is None
    # Reading where it is makes no receiver either.
    with pytest.raises(SourceError, match=r"^uart.gnss has no receiver: give it one with"):
        assert route.position is None
    with pytest.raises(SourceError, match="has no receiver"):
        route.receiver()
    assert route.source is None
    route.replay("gnss-generated-1.jsonl")
    assert route.source_kind == "replay"
    with pytest.raises(SourceError, match="replays a recording: it has no receiver"):
        assert route.position is None


def test_the_first_model_call_that_succeeds_makes_the_receiver() -> None:
    t = [0]
    route = gnss_route(t)
    route.go_silent()
    assert route.source_kind == "model" and route.position is None
    route.fix(at=WARSAW)
    t[0] = 5 * S
    route.move(heading=NORTH, speed=m_s(1))
    t[0] = 6 * S
    assert route.position == about(WARSAW.moved(NORTH, m(1)), within=m(0.001))
    assert route.receiver() is route.source


def test_each_model_call_is_a_step_of_the_test_in_the_run_s_log() -> None:
    t = [0]
    log = RunLog()
    route = gnss_route(t, log)
    with pytest.raises(ModelError):
        route.move(heading=NORTH, speed=km_h(10))
    route.fix(at=WARSAW)
    t[0] = 10 * S
    route.move(heading=NORTH, speed=km_h(36))
    assert (route.source_kind, route.source_detail) == ("model", "a GNSS receiver")
    # The refused call is not one; the first line is the log's heading.
    heading, *steps = log.lines()
    assert heading == "the run on the board's clock, 2 entries:"
    assert [line.split(None, 3)[3] for line in steps] == [
        "uart.gnss.fix(at=Position(lat=52.229677, lon=21.012233), hdop=0.9, satellites=8)",
        "uart.gnss.move(heading=NORTH, speed=m_s(10))",
    ]
    assert steps[1].split()[:2] == ["10.000000000", "s"]


def test_a_tap_on_a_modelled_route_records_its_epochs_as_model_gnss(tmp_path: Path) -> None:
    route = gnss_route([0])
    assert route.recorded_source() == "none"
    tap = route.tap(tmp_path / "tap.jsonl")
    route.fix(at=WARSAW)
    route.poll(S)
    assert route.recorded_source() == "model:gnss"
    made = tap.recording(
        board="esp32-devkitc", source=route.recorded_source(), clock=recording.EMULATED
    )
    assert made.header.source == "model:gnss"
    ((t, direction, data),) = tap.events
    assert (t, direction) == (S, "in") and data.startswith(b"$GPRMC,120001.00,A,")
    replayed = gnss_route([0])
    replayed.replay("gnss-generated-1.jsonl")
    assert replayed.recorded_source() == "replay"


def test_without_the_board_s_supply_the_receiver_is_silent_and_comes_back_with_its_fix() -> None:
    board = Board()
    board.receiver.fix(at=WARSAW)
    board.run_to(S)
    board.receiver.supply(False)
    assert board.receiver.quiet_until() is None
    assert board.run_to(5 * S + 300_000_000) == []
    # the test's silence and the supply are apart: resuming does not give it power
    board.receiver.resume()
    assert board.run_to(5 * S + 600_000_000) == []
    board.receiver.supply(True)
    assert board.receiver.quiet_until() == 6 * S
    [(t, epoch)] = board.run_to(6 * S)
    assert t == 6 * S and b",A," in epoch


# CASIC frames, built here from the specification rather than by the model.
AID_INI = (0x0B, 0x01)
GPS_EPH = (0x08, 0x07)
CFG_MSG = (0x06, 0x01)
ACK_AFTER_NS = 1_500_000
BAUD = 9600


def frame(cls: int, id: int, payload: bytes, *, checksum: int | None = None) -> bytes:
    """The CASIC frame of `cls` and `id` carrying `payload`, its checksum worked out word by
    word unless given."""
    if checksum is None:
        words = struct.unpack(f"<{len(payload) // 4}I", payload[: len(payload) // 4 * 4])
        checksum = ((id << 24) + (cls << 16) + len(payload) + sum(words)) % 2**32
    head = b"\xba\xce" + struct.pack("<HBB", len(payload), cls, id)
    return head + payload + struct.pack("<I", checksum)


def ack(cls: int, id: int, *, nak: bool = False) -> bytes:
    """ACK-ACK, or ACK-NAK, for a frame of `cls` and `id`."""
    return frame(0x05, 0x00 if nak else 0x01, bytes((cls, id, 0, 0)))


def aid_ini() -> bytes:
    """An AID-INI frame, 56 bytes of payload (ZKW's CASIC specification, 2.14.1): Warsaw at
    100 m, week 2399 and its time of week, position and time valid, in latitude and longitude."""
    payload = struct.pack(
        "<ddddffffIHBB",
        52.229677,  # latitude
        21.012233,  # longitude
        100.0,  # altitude
        388_800.0,  # time of week
        0.0,  # frequency bias
        0.0,  # position accuracy
        0.0,  # time accuracy
        0.0,  # frequency accuracy
        0,  # reserved
        2399,  # week number
        0,  # time source
        0x23,  # flags: position valid, time valid, latitude and longitude
    )
    assert len(payload) == 56
    return frame(*AID_INI, payload)


def ephemeris() -> bytes:
    """A GPS ephemeris frame, MSG-GPSEPH: 72 bytes of payload, whatever their values."""
    return frame(*GPS_EPH, bytes(range(72)))


def on_line(count: int) -> int:
    """How long `count` characters of 10 bits take at `BAUD`, in nanoseconds, rounded down."""
    return count * 10 * S // BAUD


def write(board: Board, t: int, data: bytes) -> None:
    """The firmware writes `data` to the receiver at `t`: it answers in its turns, not here."""
    board.t = t
    assert list(board.receiver.receive(t, data)) == []


def answers(board: Board, until: int) -> list[tuple[int, bytes]]:
    """The CASIC answers the receiver sends in the turns it asks for, up to `until`."""
    sent: list[tuple[int, bytes]] = []
    while (bound := board.receiver.quiet_until()) is not None and bound <= until:
        sent += [(t, data) for t, data in board.run_to(bound) if data.startswith(b"\xba")]
    board.t = max(board.t, until)
    return sent


def test_casic_frames_and_answers_are_those_of_quectel_s_specification() -> None:
    # Quectel's L76K GNSS protocol specification, 3.2.2: a CFG-PRT query, a CFG-MSG and a
    # CFG-RATE, and the ACK-ACK for the query.
    query = bytes.fromhex("BACE0000060000000600")
    assert casic(0x06, 0x00, b"") == query == frame(0x06, 0x00, b"")
    assert casic(0x06, 0x01, bytes.fromhex("4E000100")) == bytes.fromhex(
        "BACE040006014E00010052000701"
    )
    assert casic(0x06, 0x04, bytes.fromhex("F4010000")) == bytes.fromhex(
        "BACE04000604F4010000F8010604"
    )
    board = Board()
    write(board, 0, query)
    assert answers(board, S // 2) == [
        (on_line(10) + ACK_AFTER_NS, bytes.fromhex("BACE04000501060000000A000501"))
    ]
    # The ACK-NAK an AT6558 receiver gave for a GPS ephemeris (JosephHewitt/wardriver_rev3#175).
    board.receiver.refuse(*GPS_EPH)
    write(board, S // 2, ephemeris())
    assert answers(board, S - 1) == [
        (S // 2 + on_line(82) + ACK_AFTER_NS, bytes.fromhex("BACE04000500080700000C070500"))
    ]


def test_casic_frames_get_ack_ack_naming_them_answer_after_their_last_byte() -> None:
    board = Board()
    board.receiver.fix(at=WARSAW)
    t = 300_000_000
    assert board.receiver.latency() == ACK_AFTER_NS
    write(board, t, aid_ini())
    # The second frame waits on the line behind the first.
    write(board, t + 1, ephemeris())
    first = t + on_line(66)
    second = first + on_line(82)
    assert board.receiver.quiet_until() == first
    # A frame still on the line is not heard yet.
    assert board.receiver.casic_frames == []
    assert answers(board, t + S // 2) == [
        (first + ACK_AFTER_NS, ack(*AID_INI)),
        (second + ACK_AFTER_NS, ack(*GPS_EPH)),
    ]
    board.receiver.answer_after(ms(250))
    assert board.receiver.latency() == 250_000_000
    write(board, 2 * S + 1, frame(*CFG_MSG, bytes(4)))
    third = 2 * S + 1 + on_line(14)
    assert answers(board, 3 * S - 1) == [(third + 250_000_000, ack(*CFG_MSG))]
    frames = board.receiver.casic_frames
    assert [(f.at, f.cls, f.id, f.answer) for f in frames] == [
        (Duration(first), *AID_INI, "ack"),
        (Duration(second), *GPS_EPH, "ack"),
        (Duration(third), *CFG_MSG, "ack"),
    ]
    assert frames[1].payload == bytes(range(72))


def test_a_frame_cut_over_several_pieces_is_heard_when_its_last_byte_arrives() -> None:
    board = Board()
    data = aid_ini()
    pieces = [data[:1], data[1:3], data[3:7], data[7:-1], data[-1:]]
    for i, piece in enumerate(pieces):
        write(board, i * 1000, piece)
    # Written faster than the line takes them, the pieces go out one after another.
    end = sum(on_line(len(piece)) for piece in pieces)
    assert answers(board, S // 2) == [(end + ACK_AFTER_NS, ack(*AID_INI))]
    [heard] = board.receiver.casic_frames
    assert heard.at == Duration(end) and heard.payload == data[6:-4]


def test_two_frames_in_one_piece_and_frames_between_nmea_and_noise() -> None:
    board = Board()
    write(board, 5, aid_ini() + ephemeris())
    assert answers(board, S // 4) == [
        (5 + on_line(66) + ACK_AFTER_NS, ack(*AID_INI)),
        (5 + on_line(66 + 82) + ACK_AFTER_NS, ack(*GPS_EPH)),
    ]
    cfg = frame(*CFG_MSG, bytes(4))
    mixed = (
        checksummed("PCAS03,1,0,0,0,1,0,0,0")
        # A 0xBA that starts no header.
        + b"\xba\x00noise\xba"
        + cfg
        + checksummed("PCAS10,0")
        # A header whose length is more than the receiver takes is noise: the frame after it
        # is still found.
        + b"\xba\xce\xff\xff"
        + ephemeris()
        + b"\xba"
    )
    t = S // 4
    write(board, t, mixed)
    assert answers(board, S // 2) == [
        (t + on_line(mixed.index(cfg) + len(cfg)) + ACK_AFTER_NS, ack(*CFG_MSG)),
        (t + on_line(len(mixed) - 1) + ACK_AFTER_NS, ack(*GPS_EPH)),
    ]
    # The last 0xBA may start a header: it does.
    rest = frame(*CFG_MSG, bytes(8))[1:]
    write(board, S // 2, rest)
    assert answers(board, S - 1) == [(S // 2 + on_line(len(rest)) + ACK_AFTER_NS, ack(*CFG_MSG))]
    assert [(f.cls, f.id) for f in board.receiver.casic_frames] == [
        AID_INI,
        GPS_EPH,
        CFG_MSG,
        GPS_EPH,
        CFG_MSG,
    ]


def test_a_frame_with_a_bad_checksum_gets_ack_nak() -> None:
    board = Board()
    good = aid_ini()
    bad = frame(*AID_INI, good[6:-4], checksum=int.from_bytes(good[-4:], "little") ^ 1)
    write(board, 1000, bad + ephemeris())
    assert answers(board, S // 2) == [
        (1000 + on_line(66) + ACK_AFTER_NS, ack(*AID_INI, nak=True)),
        (1000 + on_line(66 + 82) + ACK_AFTER_NS, ack(*GPS_EPH)),
    ]
    assert [(f.cls, f.id, f.answer) for f in board.receiver.casic_frames] == [
        (*AID_INI, "nak"),
        (*GPS_EPH, "ack"),
    ]


def test_the_answers_are_those_an_l76k_gave() -> None:
    # tools/casic_probe/l76k.txt: an L76K, SW=URANUS5,V5.3.0.0, answered AID-INI and a zero
    # MSG-GPSEPH with ACK-ACK, and a CFG-PRT query and AID-INI with a bad checksum with ACK-NAK.
    board = Board()
    query = frame(0x06, 0x00, b"")
    bad_query = frame(0x06, 0x00, b"", checksum=int.from_bytes(query[-4:], "little") ^ 1)
    good = aid_ini()
    bad_aid = frame(*AID_INI, good[6:-4], checksum=int.from_bytes(good[-4:], "little") ^ 1)
    write(board, 0, good + frame(*GPS_EPH, bytes(72)) + bad_query + bad_aid)
    assert [data.hex() for _, data in answers(board, S // 2)] == [
        "bace040005010b0100000f010501",
        "bace04000501080700000c070501",
        "bace04000500060000000a000500",
        "bace040005000b0100000f010500",
    ]


def test_a_refused_class_and_id_gets_ack_nak_from_then_on() -> None:
    board = Board()
    write(board, 1, aid_ini())
    assert answers(board, S // 4) == [(1 + on_line(66) + ACK_AFTER_NS, ack(*AID_INI))]
    board.receiver.refuse(*AID_INI)
    write(board, S // 4, aid_ini() + ephemeris())
    assert answers(board, S // 2) == [
        (S // 4 + on_line(66) + ACK_AFTER_NS, ack(*AID_INI, nak=True)),
        (S // 4 + on_line(66 + 82) + ACK_AFTER_NS, ack(*GPS_EPH)),
    ]
    assert [f.answer for f in board.receiver.casic_frames] == ["ack", "nak", "ack"]
    with pytest.raises(ModelError, match=r"^gnss.refuse: cls is 256, not from 0 to 255$"):
        board.receiver.refuse(256, 1)
    with pytest.raises(TypeError, match=r"^gnss.refuse: id is True, not a whole number$"):
        board.receiver.refuse(1, True)
    with pytest.raises(TypeError, match=r"^gnss.answer_after: 10 is not a Duration, as ms\(1.5\)$"):
        board.receiver.answer_after(10)  # type: ignore[arg-type]
    with pytest.raises(ModelError, match=r"^gnss.answer_after: -0.001 s is less than 0$"):
        board.receiver.answer_after(ms(-1))


def test_a_power_cycle_loses_a_frame_half_heard_and_the_answers_not_sent() -> None:
    board = Board()
    data = aid_ini()
    write(board, 0, data[:10])
    board.receiver.supply(False)
    assert board.receiver.latency() is None and board.receiver.quiet_until() is None
    # Without its supply the receiver hears nothing.
    write(board, 1000, data[10:])
    board.receiver.supply(True)
    # What it heard of the frame before the power went is lost with it.
    write(board, S // 4, data[10:])
    assert answers(board, S // 2) == []
    assert board.receiver.casic_frames == []
    write(board, S // 2, data)
    assert answers(board, S - 1) == [(S // 2 + on_line(66) + ACK_AFTER_NS, ack(*AID_INI))]
    # A frame heard, whose answer is not sent yet when the power goes, is never answered.
    write(board, S + S // 4, data)
    heard = S + S // 4 + on_line(66)
    assert answers(board, heard) == []
    board.receiver.supply(False)
    board.receiver.supply(True)
    assert answers(board, 2 * S - 1) == []
    assert [(f.at, f.answer) for f in board.receiver.casic_frames] == [
        (Duration(S // 2 + on_line(66)), "ack"),
        (Duration(heard), "ack"),
    ]


def test_a_silent_receiver_hears_the_frames_and_answers_none() -> None:
    board = Board()
    data = aid_ini()
    board.receiver.go_silent()
    assert board.receiver.latency() is None
    write(board, 0, data)
    assert answers(board, S // 4) == []
    board.receiver.resume()
    assert board.receiver.latency() == ACK_AFTER_NS
    write(board, S // 4, data)
    assert answers(board, S // 2) == [(S // 4 + on_line(66) + ACK_AFTER_NS, ack(*AID_INI))]
    # Silenced while a frame is on the line: it is heard silent.
    write(board, S // 2, data)
    board.receiver.go_silent()
    board.receiver.resume()
    assert answers(board, S - 1) == []
    # Silenced once a frame is heard, before its answer: the answer is not sent.
    write(board, S + 1, data)
    heard = S + 1 + on_line(66)
    assert answers(board, heard) == []
    board.receiver.go_silent()
    board.receiver.resume()
    assert answers(board, 2 * S - 1) == []
    assert [(f.at, f.answer) for f in board.receiver.casic_frames] == [
        (Duration(on_line(66)), None),
        (Duration(S // 4 + on_line(66)), "ack"),
        (Duration(S // 2 + on_line(66)), None),
        (Duration(heard), "ack"),
    ]


def test_the_route_s_casic_calls_are_steps_and_its_frames_need_a_receiver() -> None:
    t = [0]
    log = RunLog()
    route = gnss_route(t, log)
    with pytest.raises(SourceError, match=r"^uart.gnss has no receiver: give it one with"):
        _ = route.casic_frames
    route.refuse(*CFG_MSG)
    assert route.source_kind == "model"
    route.answer_after(ms(5))
    frames = route.casic_frames
    _, *steps = log.lines()
    assert [line.split(None, 3)[3] for line in steps] == [
        "uart.gnss.refuse(cls=6, id=1)",
        "uart.gnss.answer_after(seconds(0.005))",
    ]
    assert route.latency() == 5_000_000
    # The route's baud, 9600, times the frame on the line.
    assert list(route.receive(7, "out", frame(*CFG_MSG, bytes(4)))) == []
    heard = 7 + on_line(14)
    assert route.quiet_until() == heard
    t[0] = heard
    # The frame is there at the start of the turn at its last byte, before the test's hold.
    assert frames == []
    route.arrive(heard)
    assert [f.answer for f in frames] == ["nak"]
    assert list(route.poll(heard)) == []
    assert route.quiet_until() == heard + 5_000_000
    t[0] = heard + 5_000_000
    assert list(route.poll(heard + 5_000_000)) == [(heard + 5_000_000, ack(*CFG_MSG, nak=True))]
