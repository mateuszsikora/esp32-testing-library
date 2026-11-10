"""`sources/real.py`, `gnss.real(port)` and the runner under `--realtime` (protocol §12).

No hardware: a pseudo-terminal pair stands in for the USB adapter, with a thread on one end writing
the device's bytes in wall time. The tests that run firmware hold wall-time bounds on a host that
may be busy, so their margins are wide and their devices say a sentence again until it is heard: a
stall of the host costs bytes, as the design has it (protocol 12.3). What they tell apart is a run
paced to wall time from one that is not.

`test_a_receiver_on_a_usb_adapter` is the validation with real hardware (plan M5), and runs only
with `ESP32TL_REAL_GNSS_PORT` set.
"""

import contextlib
import os
import select
import statistics
import termios
import threading
import time
import tty
from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest
import serial

from esp32tl import recording
from esp32tl.backends import esp32sim
from esp32tl.backends.esp32sim import Esp32Sim
from esp32tl.interfaces import SourceError
from esp32tl.interfaces.console import Console
from esp32tl.interfaces.gnss import Gnss
from esp32tl.interfaces.uart import Uart
from esp32tl.link.boardlink import BoardLink, Wire
from esp32tl.link.protocol import RunnerHello, Sync, UartRx, UartTx, Wait
from esp32tl.report import RunLog
from esp32tl.sources import real as real_source
from esp32tl.sources.real import MS, Real
from esp32tl.tap import Tap
from esp32tl.units import seconds

NMEA_READER = pytest.mark.app_path("../firmware/nmea_reader")
BAUD = 9600
# One character on the wire at BAUD, in seconds.
CHAR_SECONDS = 10 / BAUD
FIRST_LINE = "nmea_reader: UART1 RX GPIO5 TX GPIO4 9600 baud"
FIX = b"$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47\r\n"
# What the firmware has read when it prints the fix: it parses at the line ending's first byte.
HEARD = FIX[:-1]
# A guest runs flat out for as long as it is behind wall time, under LAG_S before the lag is
# skipped (protocol 12.2): PACED_S of the board's time costs at least its excess over that.
LAG_S = 0.5
PACED_S = 1.0
AT_LEAST_S = 0.45
# How far behind wall time the guest may be at a turn that stamps a byte for the stamps to be held
# to their 10 ms: half of that.
KEPT_UP_S = 0.005


class Device:
    """The device's end of a pseudo-terminal, with the host's end at `port`."""

    def __init__(self) -> None:
        self.fd, self._host = os.openpty()
        tty.setraw(self.fd)
        tty.setraw(self._host)
        self.port = os.ttyname(self._host)

    def send(self, data: bytes) -> None:
        """`data`, as a wire at `BAUD` carries it."""
        for at in range(0, len(data), 8):
            os.write(self.fd, data[at : at + 8])
            time.sleep(8 * CHAR_SECONDS)

    def repeat(self, data: bytes, stop: threading.Event) -> threading.Thread:
        """Starts saying `data` as a wire carries it, a byte at a time, again and again until
        `stop`: a burst into a line at the bound would lose its first bytes to it."""

        def say() -> None:
            while not stop.is_set():
                for byte in data:
                    os.write(self.fd, bytes([byte]))
                    time.sleep(CHAR_SECONDS)

        thread = threading.Thread(target=say)
        thread.start()
        return thread

    def receive(self, until: bytes, timeout: float = 5) -> bytes:
        """What the host wrote, up to and including `until`."""
        got = b""
        deadline = time.monotonic() + timeout
        while until not in got:
            left = deadline - time.monotonic()
            assert left > 0, f"{until!r} did not come within {timeout} s: {got!r}"
            if select.select([self.fd], [], [], left)[0]:
                got += os.read(self.fd, 1024)
        return got

    def close(self) -> None:
        os.close(self.fd)
        os.close(self._host)


# A test that lets the fixture end the run names `device` before `board`: a device gone before the
# run ends is a port that failed, which fails the test.
@pytest.fixture
def device() -> Iterator[Device]:
    device = Device()
    yield device
    device.close()


@pytest.fixture
def real(device: Device) -> Iterator[Real]:
    source = Real("uart.gnss", device.port, BAUD)
    # The run's first turn, from which on what is dropped counts.
    source.poll(0)
    yield source
    source.close()


def seen(source: Real) -> int:
    """The bytes the reading thread has had from the port, since the first turn."""
    return len(source._read) + source.dropped + source._cut


def given(
    device: Device, source: Real, now: int, data: bytes, timeout: float = 5
) -> list[tuple[int, bytes]]:
    """The source's turn at `now`, once the reading thread has `data` from the device."""
    expected = seen(source) + len(data)
    os.write(device.fd, data)
    deadline = time.monotonic() + timeout
    while seen(source) < expected:
        assert time.monotonic() < deadline, f"the port gave {len(source._read)} bytes"
        time.sleep(0.001)
    return list(source.poll(now))


# The source on its own.


def test_a_turn_hands_over_what_the_port_gave_stamped_with_the_turn(
    device: Device, real: Real
) -> None:
    assert list(real.poll(0)) == []
    assert given(device, real, 3 * MS, b"$GP") == [(3 * MS, b"$GP")]
    assert list(real.poll(4 * MS)) == []
    assert real.dropped == 0


def test_on_the_link_a_byte_gets_the_time_of_the_next_turn_within_a_millisecond(
    device: Device, tmp_path: Path
) -> None:
    # The stamping bound, without the host's scheduler (protocol 12.3): each turn asks for the next
    # by the next whole millisecond, and a byte handed over gets that turn's time.
    uart = Uart("uart.gnss", Wire(4, 5, BAUD), clock=recording.WALL, directory=tmp_path)
    source = uart.real(device.port)
    try:
        link = BoardLink("esp32-devkitc", [uart])
        link.receive(RunnerHello(1, "esp32", recording.WALL, "abc"))
        assert link.receive(Sync(0)) == [Wait(0, MS, None)]
        assert link.receive(Sync(MS)) == [Wait(0, 2 * MS, None)]
        os.write(device.fd, b"$")
        deadline = time.monotonic() + 5
        while not source._read:
            assert time.monotonic() < deadline
            time.sleep(0.001)
        # The byte came after the turn at 1 ms, so in the millisecond before the turn at 2 ms.
        assert link.receive(Sync(2 * MS)) == [UartRx(2 * MS, 5, BAUD, b"$"), Wait(0, 3 * MS, None)]
        assert link.error is None
    finally:
        source.close()


def test_it_promises_no_latency_and_renews_its_turn_at_the_next_whole_millisecond(
    real: Real,
) -> None:
    assert real.latency() is None
    for now, renewed in [(0, MS), (1, MS), (MS - 1, MS), (MS, 2 * MS), (7 * MS + 3, 8 * MS)]:
        real.poll(now)
        assert real.quiet_until() == renewed


def test_a_device_at_its_line_rate_loses_nothing_while_the_turns_keep_up(
    device: Device, real: Real
) -> None:
    # 10 ms of the line at each turn, 10 ms apart: the line is free again at the next turn.
    for turn in range(20):
        data = bytes([turn]) * 9
        assert given(device, real, turn * 10 * MS, data) == [(turn * 10 * MS, data)]
    assert real.dropped == 0


def test_a_backlog_keeps_the_newest_bytes_within_the_bound(device: Device, real: Real) -> None:
    # At 9600 baud the bound is the 128 bytes of BURST_BYTES, 133 ms of the line.
    data = bytes(i % 251 for i in range(300))
    assert given(device, real, 0, data) == [(0, data[-128:])]
    assert real.dropped == 172
    # The line is full up to the bound: it carries nothing more by the same time.
    assert given(device, real, 0, b"late") == []
    assert real.dropped == 176
    # 10 ms on, the line has room for 9 more.
    assert given(device, real, 10 * MS, bytes(range(20))) == [(10 * MS, bytes(range(11, 20)))]
    assert real.dropped == 187


def test_an_adapter_s_whole_usb_packets_at_the_line_rate_lose_nothing(
    device: Device, real: Real
) -> None:
    # As a CH9102 delivers 9600 baud, measured: 64 bytes at once, every 67 ms, and now and then a
    # moment before the line is free of the last.
    now = 0
    for turn in range(40):
        data = bytes([turn]) * 64
        assert given(device, real, now, data) == [(now, data)]
        now += (66 if turn % 3 else 67) * MS
    assert real.dropped == 0


def test_the_bound_is_50_ms_where_that_carries_more_than_a_burst(device: Device) -> None:
    source = Real("uart.modem", device.port, 115200)
    try:
        assert source.backlog_ns == 50 * MS
    finally:
        source.close()


def test_the_line_is_never_more_than_the_bound_behind(device: Device, real: Real) -> None:
    # As rule 5 of the protocol paces it, whatever the turns are given.
    free = 0
    for turn in range(10):
        now = turn * 3 * MS
        for t, data in given(device, real, now, bytes(30)):
            assert t == now
            start = max(t, free)
            free = start + len(data) * 10 * 10**9 // BAUD
            assert free - now <= real.backlog_ns


def test_what_the_device_sent_before_the_first_turn_is_not_counted_as_dropped(
    device: Device,
) -> None:
    source = Real("uart.gnss", device.port, BAUD)
    try:
        data = bytes(i % 251 for i in range(300))
        os.write(device.fd, data)
        deadline = time.monotonic() + 5
        while source._read[-1:] != data[-1:]:
            assert time.monotonic() < deadline
            time.sleep(0.001)
        assert source.dropped == 0
        assert list(source.poll(0)) == [(0, data[-128:])]
        assert source.dropped == 0
    finally:
        source.close()


def test_what_comes_after_the_last_turn_is_not_counted_as_dropped(
    device: Device, real: Real
) -> None:
    assert given(device, real, MS, b"$GP") == [(MS, b"$GP")]
    expected = seen(real) + 3000
    os.write(device.fd, bytes(3000))
    deadline = time.monotonic() + 5
    while seen(real) < expected:
        assert time.monotonic() < deadline
        time.sleep(0.001)
    real.close()
    assert real.dropped == 0


def test_what_a_held_turn_leaves_in_the_reader_is_bounded(device: Device, real: Real) -> None:
    # Before the turn looks at them, the thread keeps no more than a turn can send.
    expected = 3000
    os.write(device.fd, bytes(expected))
    deadline = time.monotonic() + 5
    while seen(real) < expected:
        assert time.monotonic() < deadline
        time.sleep(0.001)
    assert len(real._read) == 128
    # Dropped only by a turn: until one comes, nothing has been kept from the firmware.
    assert real.dropped == 0
    assert list(real.poll(MS)) == [(MS, bytes(128))]
    assert real.dropped == expected - 128


def test_what_the_firmware_transmits_goes_to_the_port_at_once(device: Device, real: Real) -> None:
    assert list(real.receive(5 * MS, b"AT\r\n")) == []
    assert device.receive(b"\r\n") == b"AT\r\n"


def read_all(device: Device, size: int, pause: float = 0) -> bytes:
    """`size` bytes the host wrote, reading up to 1 KiB every `pause` seconds."""
    got = b""
    deadline = time.monotonic() + 30
    while len(got) < size:
        assert time.monotonic() < deadline, f"{len(got)} of {size} bytes came"
        if select.select([device.fd], [], [], 1)[0]:
            got += os.read(device.fd, 1024)
        time.sleep(pause)
    return got


def test_a_burst_larger_than_the_port_takes_does_not_hold_the_turn(
    device: Device, real: Real
) -> None:
    # esp32sim's UART reads as idle, so the firmware can send this much in no time at all.
    data = bytes(i % 251 for i in range(48 << 10))
    started = time.monotonic()
    assert list(real.receive(0, data)) == []
    assert list(real.receive(0, b"after")) == []
    assert list(real.poll(MS)) == []
    # Nothing reads the device's end until below, and a pseudo-terminal holds far less than 48 KiB:
    # a turn that waited for the port would wait the WRITE_SECONDS it gives a port that takes
    # nothing. The bound is half of that.
    assert time.monotonic() - started < real_source.WRITE_SECONDS / 2
    assert read_all(device, len(data) + 5) == data + b"after"
    assert list(real.poll(2 * MS)) == []


def test_a_slow_port_that_makes_progress_does_not_fail(
    device: Device, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(real_source, "WRITE_SECONDS", 0.5)
    source = Real("uart.gnss", device.port, BAUD)
    try:
        # Over twice the limit in all, and never long without progress. Linux's pty holds some 19
        # KiB and says it is writable only once they have nearly drained, about 1 s here, which is
        # why the writing thread does not wait for that.
        data = bytes(32 << 10)
        source.receive(0, data)
        assert read_all(device, len(data), pause=0.05) == data
        assert list(source.poll(0)) == []
    finally:
        source.close()


def test_a_port_that_takes_nothing_fails_the_next_turn(
    device: Device, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(real_source, "WRITE_SECONDS", 0.1)
    source = Real("uart.gnss", device.port, BAUD)
    try:
        source.receive(0, bytes(1 << 20))
        deadline = time.monotonic() + 10
        while source._failed is None:
            assert time.monotonic() < deadline, "the writing thread did not see the port stall"
            time.sleep(0.005)
        with pytest.raises(SourceError, match=r"uart\.gnss: the serial port .*: Write timeout"):
            source.poll(0)
    finally:
        source.close()


def test_closing_discards_what_the_driver_holds_even_on_a_port_that_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = Device()
    source = Real("uart.gnss", device.port, BAUD)
    flushed = []
    flush = source._port.reset_output_buffer

    def flushing() -> None:
        flushed.append(source._port.is_open)
        flush()

    monkeypatch.setattr(source._port, "reset_output_buffer", flushing)
    device.close()
    # What the thread found wrong with the port gone is for `close` to say; the port closes first.
    with contextlib.suppress(SourceError):
        source.close()
    assert flushed == [True]
    assert not source._port.is_open


def test_closing_does_not_wait_for_what_the_port_has_not_taken(device: Device) -> None:
    source = Real("uart.gnss", device.port, BAUD)
    source.receive(0, bytes(1 << 20))
    time.sleep(0.05)
    started = time.monotonic()
    source.close()
    assert time.monotonic() - started < real_source.WRITE_SECONDS / 2


@pytest.mark.parametrize("baud", [9600, 9312, 9888])
def test_a_baud_within_the_3_percent_passes(real: Real, baud: int) -> None:
    real.transmitted(0, baud)


@pytest.mark.parametrize("baud", [9311, 9889, 115200])
def test_a_baud_further_from_the_port_fails(device: Device, real: Real, baud: int) -> None:
    with pytest.raises(SourceError) as error:
        real.transmitted(0, baud)
    assert str(error.value) == (
        f"uart.gnss: the firmware transmits at {baud} baud, and the serial port {device.port} runs"
        " at 9600 baud"
    )


def test_no_baud_fails(device: Device, real: Real) -> None:
    with pytest.raises(SourceError) as error:
        real.transmitted(0, None)
    assert str(error.value) == (
        f"uart.gnss: the firmware transmits with no baud set, and the serial port {device.port}"
        " runs at 9600 baud"
    )


def test_a_port_that_fails_fails_the_turn() -> None:
    device = Device()
    source = Real("uart.gnss", device.port, BAUD)
    try:
        device.close()
        deadline = time.monotonic() + 5
        while source._failed is None:
            assert time.monotonic() < deadline, "the reading thread did not see the port fail"
            time.sleep(0.005)
        with pytest.raises(SourceError, match=r"uart\.gnss: the serial port "):
            source.poll(0)
    finally:
        source.close()


def test_a_port_that_cannot_be_opened_says_which(tmp_path: Path) -> None:
    missing = tmp_path / "no-such-port"
    with pytest.raises(SourceError, match=f"uart.gnss: cannot open the serial port {missing}"):
        Real("uart.gnss", str(missing), BAUD)


def test_a_port_that_refuses_its_settings_says_which(monkeypatch: pytest.MonkeyPatch) -> None:
    # pyserial does not wrap what tcsetattr raises, which is no OSError.
    def refuse(*args: object, **kwargs: object) -> None:
        raise termios.error(22, "Invalid argument")

    monkeypatch.setattr(serial, "Serial", refuse)
    with pytest.raises(SourceError, match=r"uart\.gnss: cannot open the serial port /dev/x: "):
        Real("uart.gnss", "/dev/x", BAUD)


@pytest.mark.parametrize("port", [None, ""])
def test_a_port_must_be_named(port: str) -> None:
    with pytest.raises(TypeError, match=r"uart\.gnss\.real takes the path of a serial port"):
        Real("uart.gnss", port, BAUD)


def test_a_failure_no_turn_was_left_to_raise_comes_from_close(device: Device) -> None:
    source = Real("uart.gnss", device.port, BAUD)
    source._fail(OSError("stalled"))
    with pytest.raises(SourceError, match=r"uart\.gnss: the serial port .*: stalled"):
        source.close()
    assert not source._port.is_open
    source.close()


def test_a_failure_a_turn_has_raised_does_not_come_from_close_as_well(device: Device) -> None:
    source = Real("uart.gnss", device.port, BAUD)
    source._fail(OSError("stalled"))
    with pytest.raises(SourceError):
        source.poll(0)
    source._fail(OSError("again"))
    source.close()


def test_a_thread_that_cannot_go_on_is_a_failure_of_the_port(
    device: Device, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An exception no one expects: no OSError.
    source = Real("uart.gnss", device.port, BAUD)

    def refuse(data: bytes) -> None:
        raise ValueError("no such thing")

    monkeypatch.setattr(source, "_write", refuse)
    try:
        source.receive(0, b"AT")
        deadline = time.monotonic() + 5
        while source._failed is None:
            assert time.monotonic() < deadline
            time.sleep(0.001)
        with pytest.raises(SourceError, match="no such thing"):
            source.poll(0)
    finally:
        source.close()


def test_two_routes_do_not_share_a_port(device: Device, real: Real) -> None:
    with pytest.raises(SourceError, match="cannot open the serial port"):
        Real("uart.other", device.port, BAUD)


def test_closing_twice_does_nothing(device: Device) -> None:
    source = Real("uart.gnss", device.port, BAUD)
    source.close()
    source.close()


def test_a_tap_records_the_source_s_stamps_and_not_what_it_dropped(
    device: Device, tmp_path: Path
) -> None:
    uart = Uart("uart.gnss", Wire(4, 5, BAUD), clock=recording.WALL, directory=tmp_path)
    source = uart.real(device.port)
    try:
        tap = uart.tap("tap.jsonl")
        assert list(uart.poll(0)) == []
        data = bytes(i % 251 for i in range(300))
        os.write(device.fd, data)
        deadline = time.monotonic() + 5
        while seen(source) < 300:
            assert time.monotonic() < deadline
            time.sleep(0.001)
        assert list(uart.poll(2 * MS)) == [(2 * MS, data[-128:])]
        uart.receive(3 * MS, "out", b"AT")
        assert tap.events == [
            (2 * MS, "in", data[-128:]),
            (3 * MS, "out", b"AT"),
        ]
        assert uart.recorded_source() == "real"
    finally:
        source.close()


def test_the_link_fails_the_run_on_a_transmission_at_another_baud(
    device: Device, tmp_path: Path
) -> None:
    log = RunLog()
    uart = Uart("uart.gnss", Wire(4, 5, BAUD), clock=recording.WALL, directory=tmp_path, log=log)
    source = uart.real(device.port)
    try:
        tap = uart.tap("tap.jsonl")
        link = BoardLink("esp32-devkitc", [uart])
        link.receive(RunnerHello(1, "esp32", recording.WALL, "abc"))
        link.receive(UartTx(1, 0, 1, (4,), 115200, b"AT"))
        assert link.error == (
            f"uart.gnss.transmitted raised SourceError: uart.gnss: the firmware transmits at 115200"
            f" baud, and the serial port {device.port} runs at 9600 baud"
        )
        # What did it is in the tap and the log, and did not go to the port.
        assert tap.events == [(0, "out", b"AT")]
        assert log.traffic["uart.gnss"].bytes_out == 2
        assert source._to_write.empty()
    finally:
        source.close()


# What the backend does with one, before the run.


@NMEA_READER
def test_a_real_source_puts_the_run_on_the_wall_clock(
    device: Device, board: Esp32Sim, gnss: Gnss
) -> None:
    assert board.clock == recording.EMULATED
    gnss.real(device.port)
    assert board.clock == recording.WALL
    assert gnss.recording_clock == recording.WALL
    assert gnss.source_kind == "real"
    assert board.set_up() == ["the real device of uart.gnss"]
    board.power_on()
    board.wait(seconds(0.002))
    assert board.link.clock == recording.WALL
    assert board.link.runner is not None
    assert board.link.runner.clock == recording.WALL


@NMEA_READER
@pytest.mark.parametrize("port", ["/dev/no-such-port-esp32tl", ""])
def test_a_real_source_that_could_not_be_made_leaves_the_run_as_it_was(
    board: Esp32Sim, gnss: Gnss, port: str
) -> None:
    with pytest.raises((SourceError, TypeError)):
        gnss.real(port)
    assert gnss.source is None
    assert board.clock == recording.EMULATED
    assert gnss.recording_clock == recording.EMULATED
    # And the route is free, for a recording on the clock the run is still on.
    gnss.replay("../recordings/gnss-generated-1.jsonl")
    board.power_on()
    board.wait(seconds(0.002))
    assert board.link.runner is not None
    assert board.link.runner.clock == recording.EMULATED


def test_only_a_realtime_run_passes_the_flag() -> None:
    app = esp32sim.load_app("firmware/nmea_reader")
    without = esp32sim.runner_command(Path("r"), Path("rom"), app, 1, Path("s"))
    assert "--realtime" not in without
    command = esp32sim.runner_command(Path("r"), Path("rom"), app, 1, Path("s"), realtime=True)
    assert "--realtime" in command


@NMEA_READER
def test_without_a_real_source_the_run_stays_emulated(board: Esp32Sim, console: Console) -> None:
    board.power_on()
    console.expect_exact(FIRST_LINE)
    assert board.clock == recording.EMULATED
    assert board.link.runner is not None
    assert board.link.runner.clock == recording.EMULATED


@NMEA_READER
def test_real_is_one_source_of_the_route(device: Device, board: Esp32Sim, gnss: Gnss) -> None:
    gnss.real(device.port)
    with pytest.raises(SourceError, match=r"uart\.gnss has its source already: one per test"):
        gnss.real(device.port)
    with pytest.raises(SourceError, match=r"uart\.gnss has its source already: one per test"):
        gnss.replay("../recordings/gnss-generated-1.jsonl")
    with pytest.raises(SourceError) as error:
        gnss.lose_fix()
    assert str(error.value) == (
        "uart.gnss has a real device: it has no receiver to describe; one source per test"
    )
    board.power_on()


@NMEA_READER
def test_real_comes_before_power_on(board: Esp32Sim, gnss: Gnss, device: Device) -> None:
    board.power_on()
    with pytest.raises(SourceError, match=r"give an interface its source before board\.power_on"):
        gnss.real(device.port)


@pytest.mark.app_path("../firmware/modem_at")
@pytest.mark.board_profile("lilygo-t-a7670g-r2")
def test_a_replay_on_another_route_must_be_on_the_wall_clock(
    board: Esp32Sim, device: Device, tmp_path: Path
) -> None:
    modem = board.uart("modem")
    gnss = board.uart("gnss")
    # Emulated, given first: the real source refuses it.
    emulated = tmp_path / "emulated.jsonl"
    write_recording(emulated, "uart.gnss", recording.EMULATED)
    gnss.replay(emulated)
    with pytest.raises(SourceError) as error:
        modem.real(device.port)
    assert str(error.value) == (
        "uart.modem: a real source puts the run on the wall clock, and uart.gnss replays a"
        " recording on the emulated-fifo-write clock: replaying it would compare times across"
        " clocks"
    )
    assert board.clock == recording.EMULATED
    assert modem.source is None
    board.power_on()


@pytest.mark.app_path("../firmware/modem_at")
@pytest.mark.board_profile("lilygo-t-a7670g-r2")
def test_a_replay_given_after_a_real_source_takes_the_wall_clock_only(
    device: Device, board: Esp32Sim, tmp_path: Path
) -> None:
    modem = board.uart("modem")
    gnss = board.uart("gnss")
    modem.real(device.port)
    emulated = tmp_path / "emulated.jsonl"
    write_recording(emulated, "uart.gnss", recording.EMULATED)
    with pytest.raises(Exception, match="runs on the wall clock"):
        gnss.replay(emulated)
    wall = tmp_path / "wall.jsonl"
    write_recording(wall, "uart.gnss", recording.WALL)
    gnss.replay(wall)
    board.power_on()


@pytest.mark.app_path("../firmware/modem_at")
@pytest.mark.board_profile("lilygo-t-a7670g-r2")
def test_a_port_that_failed_after_the_last_turn_fails_the_test_and_says_why(
    board: Esp32Sim, device: Device
) -> None:
    source = board.uart("modem").real(device.port)
    board.power_on()
    board.wait(seconds(0.002))
    source._fail(OSError("stalled"))
    # Not through `close`, which the fixture calls again: what it would report.
    problems = board._release()
    assert len(problems) == 1 and problems[0].startswith("uart.modem: the serial port ")
    assert problems[0].endswith(": stalled")


@pytest.mark.app_path("../firmware/modem_at")
@pytest.mark.board_profile("lilygo-t-a7670g-r2")
def test_a_port_that_cannot_be_closed_does_not_keep_the_others_open(
    board: Esp32Sim, device: Device
) -> None:
    other = Device()
    try:
        first = board.uart("modem").real(device.port)
        second = board.uart("gnss").real(other.port)
        board.power_on()
        board.wait(seconds(0.002))
        close = first.close

        def refuse() -> None:
            raise OSError("no")

        first.close = refuse  # type: ignore[method-assign]
        assert board._release() == ["uart.modem cannot be closed: no"]
        assert not second._port.is_open
        first.close = close  # type: ignore[method-assign]
        board.close()
        assert not first._port.is_open
    finally:
        other.close()


def write_recording(path: Path, interface: str, clock: str) -> None:
    header = recording.Header(
        "lilygo-t-a7670g-r2", interface, "real", clock, "2026-11-03T00:00:00Z"
    )
    events = (recording.Event(0, "in", b"$", clock),)
    recording.write(path, recording.Recording(header, events))


def test_a_port_that_fails_in_a_run_or_one_never_used_fail_the_test(
    pytester: pytest.Pytester,
) -> None:
    app = Path(__file__).resolve().parent.parent / "firmware" / "nmea_reader"
    pytester.makepyfile(
        f"""
        import os
        import tty

        import pytest

        from esp32tl.units import seconds

        pytestmark = pytest.mark.app_path({str(app)!r})

        def pty():
            device, host = os.openpty()
            tty.setraw(device)
            tty.setraw(host)
            return device, host, os.ttyname(host)

        def test_pulled_out(board, gnss):
            device, host, port = pty()
            gnss.real(port)
            board.power_on()
            board.wait(seconds(0.01))
            os.close(device)
            os.close(host)
            board.wait(seconds(5))

        def test_never_powered(gnss):
            device, host, port = pty()
            gnss.real(port)
        """
    )
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-rA")
    result.stdout.fnmatch_lines_random(
        [
            "*RunEnded: the run ended at * while the test waited until *: the link failed:"
            " uart.gnss.poll raised SourceError: uart.gnss: the serial port */dev/*",
            "*esp32sim: the test set up the real device of uart.gnss and never called"
            " board.power_on(): it ran no firmware",
        ]
    )


# Whole runs, with the runner paced to wall time.


def in_events(tap: Tap) -> list[tuple[int, bytes]]:
    return [(t, data) for t, direction, data in tap.events if direction == "in"]


def turn_walls(source: Real) -> dict[int, float]:
    """From now on, the wall time at which the harness took each of `source`'s turns, by the
    turn's `t`: the turn stamps what the port gave with that `t`, at that wall time."""
    walls: dict[int, float] = {}
    poll = source.poll

    def polled(now: int) -> Iterable[tuple[int, bytes]]:
        walls.setdefault(now, time.monotonic())
        return poll(now)

    source.poll = polled  # type: ignore[method-assign]
    return walls


@NMEA_READER
def test_the_firmware_reads_the_device_and_the_run_follows_wall_time(
    board: Esp32Sim, gnss: Gnss, console: Console, device: Device, tmp_path: Path
) -> None:
    gnss.real(device.port)
    tap = gnss.tap(tmp_path / "gnss.jsonl")
    console_tap = board.tap_console(tmp_path / "console.jsonl")
    board.power_on()
    console.expect_exact(FIRST_LINE)
    started = time.monotonic()
    board.wait(seconds(PACED_S))
    assert time.monotonic() - started >= AT_LEAST_S, "the block points did not wait for wall time"
    heard = threading.Event()
    writer = device.repeat(FIX, heard)
    console.expect_exact("fix acquired lat=48.117300 lon=11.516667", timeout=5)
    heard.set()
    writer.join()
    board.close()
    stats = board.link.stats
    assert stats is not None and stats.rt_resyncs is not None
    assert stats.end == "stop"
    # Every recording of the run says the wall clock, the console's too (protocol §12.4).
    made = recording.read(tap.path)
    assert made.header.clock == recording.WALL
    assert made.header.source == "real"
    assert HEARD in b"".join(e.data for e in made.lines if isinstance(e, recording.Event))
    assert recording.read(console_tap.path).header.clock == recording.WALL
    properties = board.properties()["routes"]
    assert isinstance(properties, dict)
    assert properties["uart.gnss"]["source"] == "real"
    assert properties["uart.gnss"]["detail"] == device.port
    dropped = properties["uart.gnss"]["dropped"]
    assert isinstance(dropped, int)
    report = board.report()
    assert f"the real device on {device.port}; " in report
    assert f" bytes in, 0 out, {dropped} dropped" in report
    count = f"{stats.rt_resyncs} resync{'' if stats.rt_resyncs == 1 else 's'}"
    assert f"{count}: stretches of wall time the guest did not see" in report


@NMEA_READER
def test_in_a_paced_run_bytes_are_stamped_close_to_their_arrival(
    board: Esp32Sim, gnss: Gnss, console: Console, device: Device, tmp_path: Path
) -> None:
    walls = turn_walls(gnss.real(device.port))
    tap = gnss.tap(tmp_path / "gnss.jsonl")
    board.power_on()
    console.expect_exact(FIRST_LINE)
    # Let the boot's lag go first: the guest is CPU-bound until here.
    board.wait(seconds(0.2))
    since = board.now.ns
    written: list[float] = []

    def write() -> None:
        for _ in range(10):
            written.append(time.monotonic())
            os.write(device.fd, b"$")
            time.sleep(0.1)

    writer = threading.Thread(target=write)
    writer.start()
    board.wait(seconds(1.2))
    writer.join()
    # What the last byte needs to be handed over, on a host that slowed the writer down.
    board.wait(seconds(0.05))
    board.close()
    stats = board.link.stats
    assert stats is not None and stats.rt_resyncs is not None
    # The bound holds while the guest keeps up with wall time: a skipped lag takes wall time out
    # of the run, and says nothing of the stamps.
    if stats.rt_resyncs > 0:
        pytest.skip(f"the host fell behind wall time: {stats.rt_resyncs} resyncs")
    stamps = [t for t, data in in_events(tap) for _ in data]
    assert len(stamps) == 10
    # Nor does a lag shorter than the one esp32sim skips: a byte that arrives while the guest is
    # behind gets the turn's time, behind its arrival by the lag. The runner sends no turn before
    # its wall clock has reached it (protocol 12.2), so the turn the harness took earliest, by wall
    # time less the board's, is one at which the guest kept up, and every other is behind it by
    # the guest's lag then, and the harness's. In a run that is not paced the board's time runs
    # ahead of wall time, and the turns after the 1.2 s are behind its earliest by more than the
    # LAG_S a paced guest can be: that is no host falling behind, and goes on to fail below.
    # Most of the stamps are held to the margin, so a lag at fewer than half of the bytes' turns
    # moves none of those and leaves the run to the bounds; at half or more it skips. A harness
    # slow to take its turns looks the same as a busy host here: the skip's reason, with the lags,
    # is in the short summary of every run (`-ra`), so a test that always skips shows.
    offsets = {t: wall - t / 1e9 for t, wall in walls.items() if t >= since}
    earliest = min(offsets.values())
    behind = [round((offsets[t] - earliest) * 1e3, 1) for t in stamps]
    lagged = sum(lag > KEPT_UP_S * 1e3 for lag in behind)
    if 2 * lagged >= len(behind) and max(behind) <= LAG_S * 1e3:
        pytest.skip(f"the host fell behind wall time: by {behind} ms at the turns of the bytes")
    # Each stamp against its arrival, around the run's own offset between the two clocks. This does
    # not hold the protocol's millisecond, which
    # test_on_the_link_a_byte_gets_the_time_of_the_next_turn_within_a_millisecond does: on a busy
    # host the guest falls behind by moments, so most of the stamps are held to a margin for the
    # scheduler, and all to the lag it may have (protocol 12.2). A run that is not paced has spent
    # its 1.2 s of the board's time before most of the bytes are written, and stamps them nearly
    # together.
    offs = [stamp - wall * 1e9 for stamp, wall in zip(stamps, written, strict=True)]
    center = statistics.median(offs)
    away = sorted(abs(off - center) for off in offs)
    assert statistics.median(away) < 10 * MS, f"{[a / MS for a in away]} ms off"
    assert away[-1] < 500 * MS, f"{[a / MS for a in away]} ms off"


@NMEA_READER
def test_a_turn_held_before_the_first_2_16_cycles_does_not_cost_the_waits(
    device: Device, board: Esp32Sim, gnss: Gnss
) -> None:
    gnss.real(device.port)
    board.power_on()
    # The run is held at turn 0, before the guest's first instruction.
    board.wait(seconds(0))
    time.sleep(1)
    started = time.monotonic()
    board.wait(seconds(PACED_S))
    # On a wall clock started at power-on, the run would have the second held to catch up, and no
    # wait: the second of the board's time would take next to nothing.
    assert time.monotonic() - started >= AT_LEAST_S


@NMEA_READER
def test_a_long_hold_drops_the_backlog_and_an_answer_reaches_the_guest_within_the_bound(
    board: Esp32Sim, gnss: Gnss, console: Console, device: Device, tmp_path: Path
) -> None:
    source = gnss.real(device.port)
    tap = gnss.tap(tmp_path / "gnss.jsonl")
    board.power_on()
    console.expect_exact(FIRST_LINE)
    answer = threading.Event()
    heard = threading.Event()
    sent: list[int] = []

    def stream() -> threading.Thread:
        # The device at its full line rate, until it answers.
        while not answer.is_set():
            device.send(b"x" * 8)
            sent.append(8)
        time.sleep(3 * CHAR_SECONDS)
        return device.repeat(FIX, heard)

    answering: list[threading.Thread] = []
    streamer = threading.Thread(target=lambda: answering.append(stream()))
    streamer.start()
    # A turn held for longer than the 500 ms esp32sim skips.
    time.sleep(2)
    answer.set()
    console.expect_exact("fix acquired lat=48.117300 lon=11.516667", timeout=5)
    (fix,) = console.lines("fix acquired")
    heard.set()
    streamer.join()
    answering[0].join()
    board.close()
    stats = board.link.stats
    assert stats is not None
    assert stats.rt_resyncs is not None and stats.rt_resyncs >= 1
    # What the device sent through the hold, as much of its 960 bytes a second as the host let its
    # thread send: the line carried the bound's 128 bytes of it at the first turn after the hold,
    # and what reached the reading thread only after that turn, none on an idle host, as many again
    # at most here; the source dropped the rest.
    assert source.dropped >= sum(sent) - 2 * real_source.BURST_BYTES
    events = in_events(tap)
    stamps = [t for t, data in events for _ in data]
    # The first whole sentence on the line, which the firmware printed the fix for; the device
    # said it after the hold, as fast as the host let its thread. From the stamp of its line ending
    # to the fix: at most the bound behind on the line, its last character, and the firmware's
    # parsing. Without the drop it would wait behind the hold's bytes, two seconds of them.
    ending = b"".join(data for _, data in events).index(HEARD) + len(HEARD) - 1
    late = fix.at.ns - stamps[ending]
    assert late < source.backlog_ns + 10 * MS, f"the fix came {late / MS} ms after its line ending"
    # As rule 5 of the protocol paces it: no message goes on the line more than the bound after
    # its stamp.
    free = 0
    for t, data in events:
        start = max(t, free)
        assert start - t <= source.backlog_ns, f"at {t} ns the line is {(start - t) / MS} ms behind"
        free = start + len(data) * 10 * 10**9 // BAUD


@pytest.mark.app_path("../firmware/modem_at")
@pytest.mark.board_profile("lilygo-t-a7670g-r2")
def test_the_firmware_and_the_device_talk_both_ways(
    board: Esp32Sim, console: Console, device: Device, tmp_path: Path
) -> None:
    uart = board.uart("modem")
    uart.real(device.port)
    tap = uart.tap(tmp_path / "modem.jsonl")
    board.power_on()
    answered = threading.Event()

    def answer() -> None:
        device.receive(b"AT\r\n", timeout=30)
        os.write(device.fd, b"\r\nOK\r\n")
        answered.set()

    responder = threading.Thread(target=answer)
    responder.start()
    console.expect_exact("modem_at: AT =>", timeout=30)
    console.expect_exact("OK", timeout=5)
    responder.join()
    assert answered.is_set()
    board.close()
    directions = {direction for _, direction, _ in tap.events}
    assert directions == {"in", "out"}
    assert b"AT\r\n" in b"".join(data for _, d, data in tap.events if d == "out")
    assert recording.read(tap.path).header.source == "real"


@NMEA_READER
@pytest.mark.skipif(
    "ESP32TL_REAL_GNSS_PORT" not in os.environ, reason="ESP32TL_REAL_GNSS_PORT is not set"
)
def test_a_receiver_on_a_usb_adapter(board: Esp32Sim, gnss: Gnss, console: Console) -> None:
    """A GNSS receiver on a USB serial adapter at the route's 9600 baud, with a sky view: a fix
    within its time to first fix. What it makes is no recording the suite reads: the generated
    recordings stay the deterministic reference (plan M5)."""
    source = gnss.real(os.environ["ESP32TL_REAL_GNSS_PORT"])
    tap = gnss.tap(os.environ.get("ESP32TL_REAL_GNSS_TAP", "/tmp/esp32tl-real-gnss.jsonl"))
    board.power_on()
    console.expect_exact(FIRST_LINE)
    # The boot is CPU-bound, and may lose what the receiver says meanwhile, as the design has it.
    dropped_in_the_boot = source.dropped
    console.expect(r"fix acquired lat=\S+ lon=\S+", timeout=300)
    board.close()
    assert source.dropped == dropped_in_the_boot
    made = recording.read(tap.path)
    assert made.header.clock == recording.WALL and made.header.source == "real"
    print(f"{len(made.lines)} events in {tap.path}")
