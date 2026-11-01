"""The run on the board's clock, `esp32tl/report.py`: what goes into the log, and how the report
of a test that did not pass shows it."""

from esp32tl.report import RunLog, console_line, shown


def test_entries_are_shown_in_the_order_of_the_board_s_time() -> None:
    log = RunLog()
    log.test(0, "board.power_on()")
    # An answer stamped ahead of the firmware's next bytes.
    log.route(3_000, "uart.modem", "in", b"\r\nOK\r\n")
    log.route(2_000, "uart.modem", "out", b"AT\r")
    log.test(3_000, "waits up to 1 s for 'OK' on the console")
    assert log.lines() == [
        "the run on the board's clock, 4 entries:",
        "  0.000000000 s  test            board.power_on()",
        "  0.000002000 s  uart.modem out  AT\\r",
        "  0.000003000 s  uart.modem in   \\r\\nOK\\r\\n",
        "  0.000003000 s  test            waits up to 1 s for 'OK' on the console",
    ]


def test_the_console_is_cut_into_lines_each_at_the_time_of_its_first_byte() -> None:
    log = RunLog()
    log.console(10, b"\x1b[0;32mI (10) main: one")
    log.console(20, b" line\r\nand two\n")
    log.console(30, b"half")
    assert log.lines() == [
        "the run on the board's clock, 3 entries:",
        "  0.000000010 s  console  I (10) main: one line",
        "  0.000000020 s  console  and two",
        # A line the run did not finish is shown too.
        "  0.000000030 s  console  half",
    ]


def test_the_log_keeps_every_step_and_the_last_entries_and_counts_every_byte() -> None:
    log = RunLog(entries=2)
    log.test(0, "board.power_on()")
    for t in range(5):
        log.route(t, "uart.gnss", "in", b"$GP")
    log.route(9, "uart.gnss", "out", b"")
    log.test(9, "waits up to 1 s for 'x' on the console")
    assert log.lines() == [
        "the run on the board's clock, 7 entries: every step of the test, and the last 2 of the"
        " console and the routes:",
        "  0.000000000 s  test          board.power_on()",
        "  0.000000003 s  uart.gnss in  $GP",
        "  0.000000004 s  uart.gnss in  $GP",
        "  0.000000009 s  test          waits up to 1 s for 'x' on the console",
    ]
    traffic = log.traffic["uart.gnss"]
    assert (traffic.bytes_in, traffic.events_in, traffic.bytes_out, traffic.events_out) == (
        15,
        5,
        0,
        0,
    )


def test_an_empty_run_says_so() -> None:
    assert RunLog().lines() == ["the run on the board's clock: nothing happened"]


def test_bytes_are_shown_as_text_with_what_is_not_printable_escaped() -> None:
    assert shown(b"$GPGGA,1*66\r\n") == "$GPGGA,1*66\\r\\n"
    assert shown(b"\x00\x7e\x7f\\\t\xff") == "\\x00~\\x7f\\\\\\t\\xff"
    assert shown(b"x" * 101) == "x" * 100 + "... (101 bytes)"
    assert console_line(b"caf\xc3\xa9 \xff\r\n") == "café \\xff"


def test_a_console_line_cannot_move_the_cursor_over_the_report() -> None:
    log = RunLog()
    log.console(0, b"progress 10%\rprogress 20%\t\x1b[2K\x1b[0;32mok\x1b[0m\n")
    assert log.lines()[1:] == [
        "  0.000000000 s  console  progress 10%\\rprogress 20%\t\\x1b[2Kok",
    ]
