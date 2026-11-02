# Invariants

Rules about the domain that a test enforces: break one, and the named test fails. A rule that code
can break without any test failing is a note in the owning feature document, not an invariant
here. Numbers are never reused; a rule that stops holding is struck through, with the reason, so
that references to its number keep their meaning.

## Tests and backends

1. A test that uses `board` is parametrised with the backend's name as its id
   (`test_marked[esp32sim]`), and its `board`, `embedded_services` and `app_path` are set together;
   a test without `board` is not parametrised. Serial is opt-in; selecting it gives `esp,idf`,
   including for a test requesting only `console`.
   `tests/test_serial_backend.py::test_serial_is_opt_in_and_selects_esp_idf`,
   `tests/test_serial_backend.py::test_console_only_uses_serial_backend`.
   `tests/test_pytest_plugin.py::test_board_is_parametrised_with_the_backend_its_services_and_the_app`
2. `--esp32tl-backend` takes only a backend's name: any other name is a usage error (exit code 4).
   Explicit selection runs that backend only; the default is esp32sim.
   `tests/test_serial_backend.py::test_serial_is_opt_in_and_selects_esp_idf`.
   `tests/test_pytest_plugin.py::test_backend_option_takes_a_backend_name_only`
3. pytest-embedded's `--embedded-services`, `--app-path` and `--build-dir` are refused, with exit
   code 4 and no traceback on stdout, when a selected test uses `board`; `--app-path` is allowed
   when `-k` has deselected every test that uses `board`.
   `tests/test_pytest_plugin.py::test_pytest_embedded_options_are_refused_for_tests_with_a_board`,
   `tests/test_pytest_plugin.py::test_options_are_allowed_when_no_selected_test_uses_board`
4. A test has one board: `--count` other than 1, and a `count` parametrised above 1, are refused.
   `tests/test_pytest_plugin.py::test_more_than_one_dut_is_refused`,
   `tests/test_pytest_plugin.py::test_a_parametrised_count_above_one_is_refused`
5. An `app_path` marker without a path is a collection error that names the test.
   `tests/test_pytest_plugin.py::test_app_path_marker_needs_one_path`
6. The plugin is registered through the `pytest11` entry point as `esp32tl`, and pytest loads it.
   `tests/test_package.py::test_plugin_is_registered_through_pytest11`,
   `tests/test_package.py::test_pytest_loaded_the_plugin`

25. ~~A test whose `source` or `tap` markers ask for what a backend cannot give is skipped on that
    backend with the reason, before any fixture is set up, and runs on the others; one that asks for
    what no backend can give, or for an interface its profile does not have, is a collection error
    that names the test.~~ No longer holds since #65, which removed the `source` marker: see 47 for
    taps and 46 for sources.
26. Every backend in `SERVICES` has a capability declaration; on `serial` an on-board UART is real
    only and only the console can be tapped.
    `tests/test_boards.py::test_every_backend_has_a_declaration`,
    `tests/test_boards.py::test_the_declaration_is_the_specification_s_table`
29. ~~A board marker (`app_path`, `board_profile`, `source`, `tap`) that reaches a test from a
    `pytest.param`, or from a hook on the test, its class or its module, stops the run with a usage
    error, so no source a test asks for goes unchecked.~~ No longer holds since #65, which removed
    the `source` marker: see 48.
47. ~~A test whose `tap` markers ask for what a backend cannot tap is skipped on that backend with the
    reason, before any fixture is set up, and runs on the others; one that asks for what no backend
    can tap, or for an interface its profile does not have, is a collection error that names the
    test.~~ No longer holds since #21, which removed the `tap` marker: see 46.
48. ~~A board marker (`app_path`, `board_profile`, `tap`) that reaches a test from a `pytest.param`,
    or from a hook on the test, its class or its module, stops the run with a usage error, so no tap
    a test asks for goes unchecked.~~ No longer holds since #21, which removed the `tap` marker: see
    51.
51. A board marker (`app_path`, `board_profile`) that reaches a test from a `pytest.param`, or from
    a hook on the test, its class or its module, stops the run with a usage error, so no profile a
    test runs on goes unchecked.
    `tests/test_pytest_plugin.py::test_a_board_marker_in_a_pytest_param_is_refused`,
    `tests/test_pytest_plugin.py::test_a_board_marker_a_hook_adds_is_refused`

## Board profiles

27. ~~The `lilygo-t-a7670g-r2` profile holds only what the specification gives, the chip, the
    modem's UART1 on GPIO 26/27 at 115200 baud, PWRKEY on GPIO 4 and RI on GPIO 33, and the
    pull-down on RI of stage 0's gap 6.~~ No longer holds since #66, which gives the profile
    LilyGO's GNSS route, flash and PSRAM: see 66.
66. The `lilygo-t-a7670g-r2` profile holds what the specification gives, the chip, the modem's
    UART1 on GPIO 26/27 at 115200 baud, PWRKEY on GPIO 4 and RI on GPIO 33, the pull-down on RI
    of stage 0's gap 6, and what LilyGO's documentation gives, the GNSS receiver's UART2 on
    GPIO 21/22 at 9600 baud, 4 MB of flash and 4 MB of PSRAM; nothing else.
    `tests/test_boards.py::test_the_lilygo_profile_holds_the_specification_and_lilygo_s_documentation`
28. A profile that breaks the schema is refused with the file and the key at fault; a UART's TX is
    never an input-only GPIO, nor is an SD card slot's SCLK, MOSI or CS, though its MISO may be;
    and no GPIO serves two uses, the console's UART0 on GPIO 1 and 3 and the slot's four pins
    counted.
    `tests/test_boards.py::test_a_profile_that_breaks_the_schema_is_refused_with_the_key`,
    `tests/test_boards.py::test_an_sd_card_slot_may_take_an_input_only_pin_for_miso`

## The esp32sim backend

7. The runner is given every file of the build's `flasher_args.json`, each at its offset.
   `tests/test_pytest_plugin.py::test_runner_command_flashes_every_file_at_its_offset`
8. The backend refuses a build that is missing, built for another chip than the classic ESP32,
   asks for flash encryption, or is unfinished.
   `tests/test_pytest_plugin.py::test_load_app_refuses_what_the_runner_cannot_run`
9. The release and file name the backend's ROM path is built from (`ROM_RELEASE`, `ROM_ELF`) are
   the ones `scripts/fetch-rom-elf` fetches.
   `tests/test_pytest_plugin.py::test_rom_is_the_one_make_setup_fetches`
10. ~~Once the runner has exited, an expect that has not matched fails at once, with an `EOF` that
    names the exit code and the runner's last output: whole lines, less than `EOF_TAIL_BYTES`,
    without colour codes, and a long last line kept.~~ No longer holds since #65, whose console
    reads the link on the test's clock and raises `RunEnded`: see 44.
11. A runner that exits with an error of its own is an error of the test, even when every expect
    passed, and its exit is reported once.
    `tests/test_pytest_plugin.py::test_a_runner_that_exits_with_an_error_fails_the_test`,
    `tests/test_pytest_plugin.py::test_a_runner_that_fails_to_start_is_reported_once`,
    `tests/test_pytest_plugin.py::test_a_failure_of_another_kind_does_not_hide_the_runner_s_exit`
43. Emulated time moves only while the test waits: the link holds every turn, before it polls the
    interfaces, until the test waits; `board.wait(d)` ends at the board's time `now + d` exactly;
    `wait_until` asks its condition at once and at every turn, at most 100 emulated milliseconds
    apart, in the test's thread, and returns what it returned; a condition that does not hold in
    time raises `NeverHappened`, an `AssertionError` naming it and the window it waited.
    `tests/test_clock.py::test_the_run_stands_still_until_the_test_waits`,
    `tests/test_clock.py::test_wait_until_asks_at_every_turn_and_returns_what_the_condition_returned`,
    `tests/test_clock.py::test_a_condition_that_never_holds_raises_never_happened_naming_it_and_the_window`,
    `tests/test_boardlink.py::test_a_hold_holds_every_turn_before_the_interfaces_are_polled`,
    `tests/test_pytest_plugin.py::test_the_test_s_waits_move_the_board_s_clock`
44. Once the run has ended, a test that waits gets `RunEnded` at once, naming the board's time, what
    it waited for, the exit code, the link's error first when there is one, and the runner's last
    output: whole lines, less than `LAST_OUTPUT_BYTES`, without colour codes, and a long last line
    kept. A condition that holds on what the run printed last still returns, also in a wait that
    starts after the end.
    `tests/test_pytest_plugin.py::test_a_wait_fails_at_once_when_the_runner_has_exited`,
    `tests/test_pytest_plugin.py::test_a_mismatched_handshake_fails_the_test_with_both_sides_reasons`,
    `tests/test_pytest_plugin.py::test_the_eof_tail_is_the_runner_s_last_whole_lines_without_colour`,
    `tests/test_pytest_plugin.py::test_the_eof_tail_keeps_a_long_last_line`,
    `tests/test_clock.py::test_the_end_of_the_run_raises_run_ended_with_the_reason`,
    `tests/test_clock.py::test_a_condition_the_last_of_the_run_makes_true_still_holds`,
    `tests/test_clock.py::test_expect_finds_what_the_run_printed_after_it_has_ended`
45. The runner starts at `board.power_on()`, not at setup: a test that never powers on starts no
    runner, and one that set up a source or a tap, or drove a line, and passed without powering on
    fails at teardown; a test that failed, was skipped or xfailed keeps its one outcome. A wait
    before power-on raises `BoardOff`, and a source or a tap after it `SourceError`.
    `tests/test_pytest_plugin.py::test_the_board_runs_the_profile_the_test_chose_and_starts_no_runner_unpowered`,
    `tests/test_pytest_plugin.py::test_a_test_that_sets_up_a_source_and_never_powers_on_fails_at_teardown`,
    `tests/test_pytest_plugin.py::test_a_test_that_taps_a_route_and_never_powers_on_fails_at_teardown`,
    `tests/test_pytest_plugin.py::test_the_lines_fixture_needs_a_profile_with_lines_and_a_drive_needs_power_on`,
    `tests/test_pytest_plugin.py::test_the_board_s_clock_and_sources_go_in_order`,
    `tests/test_gnss_fixture.py::test_a_source_given_out_of_order_fails_the_test`; the same holds
    for a card in the SD card slot.
    `tests/test_sdspi.py::test_the_slot_used_out_of_order_fails_the_test`
46. A source or a tap the backend's capability declaration does not give skips the test at the call
    that asks for it, reported at the test; one that no backend gives fails the test with
    `SourceError`.
    `tests/test_pytest_plugin.py::test_a_source_or_a_tap_the_backend_cannot_give_skips_at_the_call`,
    `tests/test_tap.py::test_a_tap_the_backend_refuses_is_not_made`
49. Every test that uses `board` has a wall-time timeout, 600 s unless the session or the test sets
    one, 0 included; a run that hangs is ended by it, and the teardown ends the runner.
    `tests/test_pytest_plugin.py::test_a_test_that_uses_board_gets_a_wall_time_timeout_unless_one_is_set`,
    `tests/test_pytest_plugin.py::test_a_hung_run_is_ended_by_the_test_s_timeout`
50. Every UART route of the board profile is on the link, listening and silent until the test gives
    it a source, and so is every GPIO line: the harness's `hello` watches the pins of all of them.
    `tests/test_pytest_plugin.py::test_every_route_and_line_of_the_profile_is_on_the_link`
55. A line the board profile pulls is at its pull while the test does not drive it: from power-on,
    in every test on the profile, whether it uses `lines` or not, and again after `lines.release`.
    On the LilyGO profile RI reads 0 with no modem at the far end (stage 0's gap 6).
    `tests/test_gpio_echo.py::test_ri_reads_the_board_s_pull_down_from_power_on`,
    `tests/test_gpio_echo.py::test_the_pull_holds_in_a_test_that_does_not_use_lines`,
    `tests/test_cli.py::test_run_holds_the_board_s_pulls_on_its_lines`,
    `tests/test_gpio_echo.py::test_the_firmware_follows_ri_and_drives_pwrkey`,
    `tests/test_lines.py::test_the_board_s_pulls_go_out_in_the_first_turn_before_what_the_test_drives`,
    `tests/test_lines.py::test_a_drive_and_a_release_go_out_at_the_instant_the_test_made_them`
56. What the test drives on a line takes effect at the board's instant it drove it, `board.now`, or
    from power-on when it drove it before; `lines.events()` holds every change of either end, a
    change of the firmware's at the time the firmware made it, not at the next look of a wait. An
    interface's GPIO input on a pin another interface is wired to fails the link with
    `{interface} sent {type} on GPIO {pin}, which another interface is wired to`.
    `tests/test_lines.py::test_a_drive_and_a_release_go_out_at_the_instant_the_test_made_them`,
    `tests/test_gpio_echo.py::test_the_firmware_follows_ri_and_drives_pwrkey`,
    `tests/test_gpio_echo.py::test_a_level_driven_before_power_on_holds_from_the_start`,
    `tests/test_boardlink.py::test_an_input_the_runner_would_refuse_is_refused_naming_its_interface`
63. A card in the SD card slot writes through to the test's own image file: what the firmware
    wrote is in the file, readable with mtools, and a card the firmware only reads leaves the file
    as it was. The runner gets the card only once the test inserts one.
    `tests/test_sdspi.py::test_the_example_writes_its_files_into_the_image`,
    `tests/test_sdspi.py::test_a_card_without_a_file_system_does_not_mount`,
    `tests/test_pytest_plugin.py::test_runner_command_puts_a_card_in_the_slot_once_the_test_inserts_one`
67. The runner gets the board profile's PSRAM as `--psram-mb`, in a test and in the `esp32tl`
    command: 0 on `esp32-devkitc`, 4 on `lilygo-t-a7670g-r2`.
    `tests/test_pytest_plugin.py::test_every_route_and_line_of_the_profile_is_on_the_link`,
    `tests/test_pytest_plugin.py::test_runner_command_gives_the_chip_the_profile_s_psram`,
    `tests/test_cli.py::test_run_gives_the_chip_the_profile_s_psram`

## The runner

12. For every `Stop` variant the test lists, the stop line's name is the variant's own name; a
    variant upstream adds fails the build instead, because `stop_name` has no catch-all arm.
    `runner/src/tests.rs::stop_names_are_the_variant_names`
13. The runner refuses, as a usage error, a command line without `--chip esp32`, without
    `--max-seconds` (a positive, finite number) or without `--flash-at`, and the flash and PSRAM
    sizes the classic ESP32 does not have (the test tries 0, 3, 32 and 2^44 MiB of flash and 2 MiB
    of PSRAM). It also refuses a `--board` that is neither `socket:PATH` nor `tcp:HOST:PORT`, a
    `--peer-timeout` that is negative, not finite, too large or rounds to 0 ns, and
    `--console socket` or `both` without `--board`.
    `runner/src/cli/tests.rs::usage_errors`
18. With `--board`, the runner's messages are in non-decreasing `t`, and `seq` starts at 1 and
    grows by 1 over every event; the fake peer fails any session test that breaks either.
    `runner/src/session/tests.rs` (`FakePeer::send`)
19. The first `sync` has `t = 0` and comes before the guest's first instruction, and inputs
    stamped 0 are in the RX FIFO at cycle 0.
    `runner/src/session/tests.rs::the_first_sync_comes_before_the_guest_runs_and_its_inputs_are_in_place`
20. Before the `sync` at `B` the runner sends every event with `t ≤ B` and nothing later, also when
    `run` returns past `B`.
    `runner/src/session/tests.rs::events_after_the_block_point_wait_for_the_next_turn`,
    `runner/src/session/tests.rs::bytes_before_the_block_point_go_out_before_the_sync`
21. An answer stamped exactly `t_event + latency` is on time, also with the guest asleep in WFI and
    on the real machine at the 40 MHz the core starts at; one stamped earlier than its turn's
    `sync` is a protocol error, exit code 3, with `stats`.
    `runner/src/session/tests.rs::an_answer_at_t_plus_latency_passes_with_the_guest_idle_in_wfi`,
    `runner/src/session/tests.rs::on_the_emulator_the_slices_stop_a_guest_asleep_in_waiti_at_the_block_point`,
    `runner/src/session/tests.rs::an_answer_before_t_plus_latency_is_late_and_ends_the_run`,
    `tests/test_runner_link.py::test_a_late_input_is_a_protocol_error_with_exit_code_3`
22. A bus cycle converted to nanoseconds and back is the same cycle, past the 76 emulated seconds
    where `cycles × 10⁹` overflows 64 bits.
    `runner/src/protocol/tests.rs::time_conversion_rounds_as_section_3_says`
23. The esp32sim revision the runner's `hello` reports is the one `runner/Cargo.toml` builds.
    `runner/src/protocol/tests.rs::the_esp32sim_revision_is_the_one_cargo_builds`
24. With `--board`, `SIGTERM` while the runner waits for the peer ends the run with `stats`
    (`end: signal`) and exit 0, and bytes a write left when the signal came go out once, not
    twice.
    `tests/test_runner_link.py::test_sigterm_while_the_runner_waits_for_the_peer_ends_the_run_with_stats`,
    `runner/src/transport/tests.rs::a_write_cut_short_by_a_signal_goes_on_where_it_stopped_once_closing`
39. An input on a pin outside the peer's `watch`, whether `uart_rx`, `gpio_in` or `release`, is a
    protocol error that ends the run with `stats` before the guest runs again.
    `runner/src/session/tests.rs::the_runner_refuses_what_the_protocol_forbids`
40. A `gpio_in` or `release` reaches the chip at the slice boundary at its own cycle, before the
    first instruction when stamped 0, in the order of the input queue; on the real machine the
    guest sees the level there and its answer comes back as `gpio_out`.
    `runner/src/session/tests.rs::gpio_in_and_release_reach_the_chip_through_the_board_at_their_time`,
    `runner/src/session/tests.rs::on_the_emulator_gpio_in_reaches_the_guest_and_its_output_comes_back_as_gpio_out`
41. After a guest reset, with a `watch` that is not empty, the runner sends a `gpio_out` at the
    reset's `t` with the watched pins as read from the chip, changing every pin the peer last saw
    at a level the chip no longer drives, and what the peer drives stays driven; on the real
    machine no pad is driven right after `reboot()`.
    `runner/src/session/tests.rs::after_a_reset_gpio_out_reports_the_pins_as_read_from_the_chip`,
    `runner/src/session/tests.rs::after_a_reset_gpio_out_changes_what_the_peer_last_saw_of_each_pad`,
    `runner/src/session/tests.rs::on_the_emulator_no_pad_is_driven_after_a_chip_reset`
42. A harness `hello` whose `watch` holds a pin the classic chip does not have (24, 28 to 31, 40
    and above) is a protocol error at the handshake, before the guest runs.
    `runner/src/session/tests.rs::a_handshake_mismatch_is_a_protocol_error_with_stats`
62. The SD card hears a transaction only while its CS pin is a low output and its SCLK pin carries
    the master's clock, and the chip reads its answer only through the card's MISO pin; until a
    CMD0 with a good CRC it answers nothing. A card not selected leaves every byte at 0xff.
    `runner/src/sd_card/tests.rs::a_card_not_selected_or_not_clocked_hears_nothing`,
    `runner/src/sd_card/tests.rs::without_miso_the_chip_reads_high_but_the_card_listens`,
    `runner/src/sd_card/tests.rs::checks_crcs_of_cmd0_and_cmd8_always_and_of_the_rest_after_cmd59`

## Recordings and replay

30. A committed recording reads and writes back byte for byte, and a recording that breaks the
    format is refused with its file and line.
    `tests/test_recording.py::test_a_committed_recording_reads_and_writes_back_byte_for_byte`,
    `tests/test_recording.py::test_a_recording_that_breaks_the_format_is_refused_with_its_line`
31. Times on two clocks are never compared: `Event.since` across a clock switch raises
    `ClockError`, and a replay refuses a recording with a time on a clock other than its
    backend's.
    `tests/test_recording.py::test_times_compare_on_one_clock_only`,
    `tests/test_replay.py::test_what_a_stream_replay_cannot_replay_is_refused`
32. A stream replay sends each event in the turn at its recorded time, never before, and its
    `quiet_until` is the next event's time.
    `tests/test_replay.py::test_each_event_goes_out_at_its_time_and_bounds_the_turn_before`,
    `tests/test_replay.py::test_a_replay_through_the_link_sends_each_event_in_the_turn_at_its_time`
33. ~~A run with a declared source does not boot until the test has given it: the link's first
    turn waits for `start`, a `console.expect` before then fails at once with `SourceError`, and a
    run never started ends with `stop` before the guest boots, and fails a test whose setup and call
    passed without giving it at teardown; a test that failed, was skipped or xfailed keeps its one
    outcome.~~ No longer holds since #65: the source is given before `board.power_on()`, which
    starts the run (see 45).
34. A recording replayed with `gnss.replay(path)` reaches the firmware's UART on the profile's GNSS
    route: `nmea_reader` prints the fixes of `gnss-generated-1.jsonl`.
    `tests/test_gnss_fixture.py::test_a_replay_reaches_nmea_reader_on_the_gnss_route`
35. A replay at another baud than the firmware's UART receives at fails the test at once, with the
    interface and both bauds, rather than by a timeout; in `esp32tl replay` it is exit code 1.
    `tests/test_boardlink.py::test_a_frame_error_fails_the_run_at_once`,
    `tests/test_gnss_fixture.py::test_reports_a_fix_fails_on_a_baud_mismatch`,
    `tests/test_cli.py::test_a_baud_mismatch_exits_1_with_the_link_s_error`
36. The first milestone's test, `test_reports_a_fix`, passes on `gnss-generated-1.jsonl` and fails
    when one digit of the recording changes.
    `tests/test_nmea_reader.py::test_reports_a_fix`,
    `tests/test_gnss_fixture.py::test_reports_a_fix_fails_on_a_changed_recording`
37. A replay runs on the emulated clock: ten fixes, ten emulated seconds, reach the console in
    under five wall seconds, and the board's clock reads just past 10 s.
    `tests/test_nmea_reader.py::test_time_is_emulated`
52. A tap records every byte that crossed its route, `out` of the ESP32 and `in` from whatever is
    at the far end, at the time it crossed, and writes it once the run has ended as a recording in
    time order that reads back: a tap of a replay holds the replay's events as they were recorded.
    `tests/test_tap.py::test_a_tap_writes_its_events_in_time_order_with_the_header`,
    `tests/test_tap.py::test_a_tapped_uart_records_both_ways_whatever_its_source`,
    `tests/test_pytest_plugin.py::test_a_tap_records_what_crossed_the_route_both_ways`,
    `tests/test_gnss_fixture.py::test_a_tap_of_a_replay_records_what_went_in`
54. A tap never writes over a file the run replays from or another route is tapped into, and a
    replay never reads a file a route is tapped into: both are refused at the call. Nor do a tap
    and the SD card's image share a file, whichever comes first, and a replay never reads the
    card's image.
    `tests/test_tap.py::test_a_tap_never_writes_over_a_file_the_run_reads_or_writes`,
    `tests/test_sdspi.py::test_no_tap_writes_over_the_card_s_image`,
    `tests/test_sdspi.py::test_a_card_does_not_take_a_tap_s_file`
53. The report of a test that failed shows the run on the board's clock: the test's steps,
    the console's lines and the bytes on every route, in the order of the board's time; that of a
    test that passed shows the bytes on every route, not the run. A profile with an SD card slot
    adds the card's image, and the test's `sd_card.insert` is one of the steps.
    `tests/test_pytest_plugin.py::test_the_report_of_a_failing_test_shows_the_run_on_the_board_s_clock`,
    `tests/test_pytest_plugin.py::test_a_tap_records_what_crossed_the_route_both_ways`,
    `tests/test_run_report.py::test_entries_are_shown_in_the_order_of_the_board_s_time`,
    `tests/test_sdspi.py::test_the_example_writes_its_files_into_the_image`
65. Two runs of one recording are identical: the console line by line with the board's times, the
    tap but for its header's `created`, and the runner's stats, for `nmea_reader` fed each
    generated recording and fed the receiver model. `make determinism` (the `determinism` CI job)
    runs these tests twice and fails on any difference.
    `tests/test_determinism.py::test_a_replay`,
    `tests/test_determinism.py::test_the_receiver_model`

## Models, units and console lines

57. The board's durations are `Duration`s of whole nanoseconds, 0 or more: `board.wait` and
    `wait_until` refuse a plain number. Quantities of different kinds never combine or compare
    equal, but for a speed times a time, a distance, and a distance over a time, a speed.
    `tests/test_units.py::test_quantities_of_different_kinds_do_not_mix`,
    `tests/test_units.py::test_a_speed_over_a_time_is_a_distance_and_back`,
    `tests/test_clock.py::test_the_board_s_durations_are_durations_of_0_or_more`,
    `tests/test_pytest_plugin.py::test_the_test_s_waits_move_the_board_s_clock`
58. A position moved by a distance on a heading is that distance away, along the great circle that
    leaves on that heading, in both hemispheres, across the antimeridian and over a pole.
    `tests/test_units.py::test_a_move_follows_the_great_circle_and_measures_its_distance`,
    `tests/test_units.py::test_a_move_crosses_the_antimeridian_and_a_pole`
59. The GNSS receiver model sends an RMC and a GGA with the same UTC time once a second, on whole
    seconds of the world's time, each sentence checksummed and at most 82 characters; what the test
    tells it holds from the board's instant, in the epoch at that instant too; the epochs of a
    silence are never sent.
    `tests/test_gnss_model.py::test_a_fix_is_an_rmc_and_a_gga_with_the_same_time_once_a_second`,
    `tests/test_gnss_model.py::test_epochs_fall_on_whole_seconds_of_the_world_s_time`,
    `tests/test_gnss_model.py::test_a_sentence_is_checksummed_and_at_most_82_characters`,
    `tests/test_gnss_model.py::test_each_change_holds_from_the_board_s_instant`,
    `tests/test_gnss_model.py::test_a_change_at_an_epoch_s_instant_is_in_that_epoch`,
    `tests/test_gnss_model.py::test_the_epochs_of_a_silence_stay_unsent_when_no_turn_came_in_it`
60. `nmea_reader` fed by the receiver model reports where the receiver is: its first fix, a
    kilometre of movement to within 0.2 m, no fix while the fix is lost, the fix again in the epoch
    of the instant it comes back, in both hemispheres.
    `tests/test_nmea_reader_model.py::test_reports_where_the_receiver_is`,
    `tests/test_nmea_reader_model.py::test_reports_the_southern_and_western_hemispheres`,
    `tests/test_nmea_reader_model.py::test_follows_the_receiver_as_it_moves`,
    `tests/test_nmea_reader_model.py::test_says_nothing_while_the_receiver_has_no_fix`,
    `tests/test_nmea_reader_model.py::test_reports_again_when_the_fix_comes_back`
61. A console line is stamped with the board's time its line ending came at, without its line
    ending and colour codes, and is a line only once it has ended; `console.line(pattern)` finds
    only lines that end after it was made, and after the last line it found.
    `tests/test_clock.py::test_console_lines_are_stamped_with_the_time_their_ending_came_at`,
    `tests/test_clock.py::test_a_line_condition_finds_the_first_matching_line_from_when_it_was_made`

## Bluetooth peer

68. Time on the air moves only when the test moves it, and asyncio's timers fire at their time on
    the board's clock, never the host's.
    `tests/test_air.py::test_timers_fire_at_their_board_time_and_only_while_the_test_moves_it`,
    `tests/test_air.py::test_a_party_is_polled_at_every_instant_the_air_stops_at`
69. The SPP peer is found by its name and pairs only with its PIN: another PIN fails the
    authentication, detaches the link and leaves no key on either side; a device that paired
    authenticates later with its key and no PIN, and one whose key the peer lost is refused.
    `tests/test_spp_peer.py::test_the_device_finds_the_peer_by_its_name_and_its_answer_comes_on_the_boards_clock`,
    `tests/test_spp_peer.py::test_a_wrong_pin_fails_the_authentication_and_detaches_the_link`,
    `tests/test_spp_peer.py::test_a_device_that_paired_authenticates_with_its_key_without_a_pin`,
    `tests/test_spp_peer.py::test_a_peer_that_lost_the_key_refuses_the_device_that_kept_it`
70. What the device writes on the SPP channel reaches the model at the instant it arrives, and what
    the model sends goes out at the time it gives, never earlier, in order; what it sends while no
    device holds the channel is lost; two runs cross the channel at the same instants.
    `tests/test_spp_peer.py::test_the_device_finds_the_peer_by_its_name_and_its_answer_comes_on_the_boards_clock`,
    `tests/test_spp_peer.py::test_what_the_source_sends_unprompted_goes_out_at_its_time_or_is_lost`,
    `tests/test_spp_peer.py::test_what_the_source_sends_after_the_channel_closes_is_lost`,
    `tests/test_spp_peer.py::test_a_source_that_gets_its_answer_wrong_fails_the_air_naming_the_peer`,
    `tests/test_spp_peer.py::test_two_runs_cross_the_channel_at_the_same_instants`
71. The SPP channel opens to one device at a time, and, unless the peer is made with
    `secure=False`, only on a link that device authenticated; two that ask at one instant get it
    one.
    `tests/test_spp_peer.py::test_the_channel_opens_to_one_device_at_a_time`,
    `tests/test_spp_peer.py::test_two_devices_that_ask_for_the_channel_at_one_instant_get_it_one`,
    `tests/test_spp_peer.py::test_the_channel_opens_only_on_a_link_the_device_authenticated`,
    `tests/test_spp_peer.py::test_an_authenticated_link_needs_no_encryption_for_the_channel`,
    `tests/test_spp_peer.py::test_a_peer_that_is_not_secure_opens_the_channel_on_any_link`

## The esp32tl command

38. Ctrl-C ends a run of `esp32tl run` or `esp32tl replay` with one `SIGTERM` to the runner, which
    sends its statistics once it has connected, and the command exits 130 without a traceback,
    also before the runner has connected.
    `tests/test_cli.py::test_ctrl_c_ends_the_run_and_exits_130`,
    `tests/test_cli.py::test_ctrl_c_before_the_runner_connects_exits_130`

## Validation firmware

14. `firmware/build.sh` builds in the ESP-IDF container pinned by digest, unless `IDF_LOCAL=1`.
    `tests/test_firmware_build.py::test_build_starts_the_pinned_container`
15. A local build records in `esp32tl-build.json` how it was built, and a build directory made
    another way is removed before building.
    `tests/test_firmware_build.py::test_local_build_runs_idf_py_and_records_how`,
    `tests/test_firmware_build.py::test_build_replaces_a_build_directory_made_another_way`
16. `esp32tl-built` marks only a build that finished: a failed build, or one without
    `flasher_args.json`, leaves none.
    `tests/test_firmware_build.py::test_failed_build_unmarks_a_finished_one`,
    `tests/test_firmware_build.py::test_build_fails_without_flasher_args`
64. `sdspi` is ESP-IDF's example unmodified: its SD card pins are the `esp32-devkitc` profile's
    slot, and it formats no card.
    `tests/test_firmware_images.py::test_sdspi_takes_the_devkit_sd_card_slot_from_sdkconfig`

## Serial hardware boundary

98. Serial's power-on resets once without another flash, excludes pre-reset console bytes, and
    uses wall time that advances outside waits. Console taps contain only output with clock
    `wall` and the selected artifact's app hash; they cannot overwrite the reference recording
    or flash artifacts.
    `tests/test_serial_backend.py::test_console_reset_clock_tap_and_autoflash`
99. A serial reference recording must have an app hash matching the app binary, unaffected by
    bootloader changes. Missing or mismatched hashes fail, and mismatch prevents port setup even
    for an explicitly requested `dut`.
    `tests/test_serial_backend.py::test_artifact_hash_is_app_binary_and_refuses_missing_or_different_hash`,
    `tests/test_serial_backend.py::test_recording_mismatch_prevents_dut_setup`,
    `tests/test_serial_backend.py::test_reference_check_precedes_even_an_explicit_dut`
100. Serial skips GNSS/modem UART models (including `modem.network()` and `broker`),
    replays and taps, SD-card models, GPIO control and
    NVS replacement. Merely requesting the modem fixture does not skip a console test. A disconnected
    console ends a wait with `RunEnded`, without a duplicate teardown failure.
    `tests/test_serial_backend.py::test_serial_capabilities_skip_at_the_call`,
    `test_serial_modem_fixture_can_accompany_console_test`,
    `tests/test_serial_backend.py::test_disconnect_ends_wait_and_releases_reader`
101. Serial requires an exact port before setup; neither an absent/blank/wildcard selection nor
    a fixture overriding it to `None` can enable autodetection. An indirect port parameter works;
    a run selecting no board test needs no port. `ESPPORT` cannot redirect that selection, even
    through cached options, and a separate flashing port is refused before opening a device.
    Cached-port mismatches name the snapshot; missing serial services produce a setup error
    without a traceback, before a device opens.
    `tests/test_serial_backend.py::test_serial_requires_an_exact_port_before_setup`,
    `test_port_parametrisation_selects_the_device`, `test_overridden_port_cannot_enable_discovery`,
    `test_environment_cannot_override_explicit_port`,
    `test_environment_can_agree_with_explicit_port`,
    `test_setup_environment_cannot_redirect_port`,
    `test_cached_port_override_names_the_snapshot`,
    `test_overridden_services_fail_without_a_traceback`,
    `test_separate_flash_port_is_refused_before_opening`
102. A failed serial reset restores redirection and leaves the board off, producing no successful
    run recording; retry can start capture. A stuck reader cannot block esp32tl's close beyond
    its join bound, and neither stuck nor disconnected capture restarts another reader.
    `tests/test_serial_backend.py::test_failed_reset_restores_redirect_and_stays_off`,
    `test_stuck_reader_has_bounded_teardown_and_no_late_capture`,
    `test_dead_open_port_does_not_restart_redirect`
103. A malformed `app_recording` marker names its test at collection; esp32sim does not read the
    referenced file. Console taps appear in the report, and serial logs wait starts and outcomes.
    Serial uses the same wait-step logging and duration validation messages as esp32sim.
    `tests/test_serial_backend.py::test_bad_reference_marker_names_the_test`,
    `test_serial_waits_and_console_tap_are_in_the_failure_report`,
    `test_wait_validation_and_closed_run_match_emulator`,
    `tests/test_pytest_plugin.py::test_reference_recording_is_not_read_on_esp32sim`,
    `test_console_tap_preserves_emulated_output`

## Documentation

17. A change to an owned path that leaves its documentation untouched fails `make docs-check`; a
    change to tests only, or a version-only bump, does not.
    `tests/test_check_docs.py::test_an_owned_path_without_docs_fails`,
    `tests/test_check_docs.py::test_tests_only_pass`,
    `tests/test_check_docs.py::test_version_only_bumps_pass`

## Cellular modem

72. Noncommands and command-mode `+++` produce no answer, including across UART chunks. Only a
    complete AT line is answered, at least 1 ms after the timestamp of its terminator-containing
    UART chunk; an escape cannot shift a registration reply by one command.
    `tests/test_modem_model.py::test_noise_and_command_mode_escape_never_leave_a_reply`,
    `tests/test_modem_model.py::test_command_framing_and_response_times_do_not_depend_on_uart_chunks`
73. The modem and the test cannot both drive RI. PWRKEY uses the profile's pulse thresholds,
    and a missing supply prevents a pulse from restoring modem power. RI follows power; a power
    loss before a pending RI response cancels that response. The validation firmware leaves at
    least 2 s between the off pulse's release and the next on pulse.
    `tests/test_modem_model.py::test_model_selection_is_atomic_and_ri_has_one_driver`,
    `tests/test_modem_model.py::test_powerkey_uses_profile_timing_and_power_loss_prevents_restart`,
    `tests/test_modem_model.py::test_nominal_lilygo_pulses_work_even_when_a_tick_truncates_them`,
    `tests/test_modem_model.py::test_power_loss_before_a_scheduled_ri_response_cancels_that_response`,
    `tests/test_modem_fixture.py::test_modem_at_dialogue_and_changes_on_the_test_clock`
74. Two AT-only firmware runs on the LilyGO route produce the same modem bytes at the same
    emulated times, without a network.
    `tests/test_modem_fixture.py::test_two_firmware_runs_produce_identical_modem_taps`
75. Every AT command is retained with its time and reply, including unknown commands answered
    with fallback OK, independently of the rolling wire log's capacity.
    `tests/test_modem_model.py::test_a_tap_and_report_keep_commands_even_after_the_wire_log_rolls_over`
76. Packet-data dial variants (`ATD*99#`, explicit context `ATD*99***1#`, `ATD*99##` and
    tone-prefixed `ATDT*99#`) enter data mode with CONNECT when registered and return NO CARRIER
    otherwise; none is a fallback OK.
    `tests/test_modem_model.py::test_packet_data_dial_variants_connect_or_report_no_carrier`
77. A model's GPIO change appears in the run log only when sent, at its sent time; a cancelled
    pending change never appears as a wire event.
    `tests/test_modem_model.py::test_only_sent_model_gpio_changes_appear_in_the_run_log`
78. A source selection that loses to another source must leave no GPIO ownership behind.
    `tests/test_modem_model.py::test_a_model_selection_that_loses_to_replay_does_not_claim_ri`

## Flash contents

79. An NVS partition is selected by name from the build's own table, and its test image is flashed
    after the build's files at that partition's offset. A later call replaces the whole image.
    `tests/test_flash.py::test_nvs_flashes_at_named_partition_after_build_and_replaces_previous_call`
80. NVS integers have explicit widths and checked ranges; a bare integer or boolean is refused.
    `tests/test_flash.py::test_integer_widths_check_their_range`,
    `tests/test_flash.py::test_nvs_requires_a_type_for_integers_and_refuses_other_values`
81. The firmware reads exactly the NVS types and values the test supplies before power-on.
    Strings retain their UTF-8 bytes, including CRLF and lines beginning with `#`. Key and
    namespace names have no special meaning: no entries are changed or added for Wi-Fi-looking
    names, and identical inputs produce identical images.
    `tests/test_nvs_reader.py::test_firmware_reads_typed_values`,
    `tests/test_flash.py::test_real_generator_preserves_string_bytes_and_has_exact_partition_size`,
    `tests/test_flash.py::test_nvs_names_do_not_change_values_or_add_keys`
82. NVS cannot be supplied after power-on. A passing test that supplies NVS without powering on
    fails teardown; it ran no firmware.
    `tests/test_flash.py::test_nvs_refuses_unknown_wrong_type_encrypted_and_late_calls`,
    `tests/test_pytest_plugin.py::test_nvs_without_power_on_fails_teardown`
83. Binary NVS values are limited by the format and partition capacity, not by a CSV field
    limit. Blobs above 64 KiB are reconstructed unchanged from the generated chunks.
    `tests/test_flash.py::test_large_blob_fits_without_a_csv_field_limit`
84. UTF-8 keys and namespaces consume their encoded byte count in fixed-size NVS headers,
    including the final slot of a page. Entry contents and CRCs remain correct, and the final
    image must always match the partition's size.
    `tests/test_flash.py::test_utf8_key_at_page_boundary_preserves_entries`,
    `tests/test_flash.py::test_utf8_namespace_at_page_boundary_preserves_entries`,
    `tests/test_flash.py::test_generator_output_must_match_partition_size`

## Request matching

85. Each request cycles through its recorded occurrences independently, including silent ones;
    `hold` and `loop` apply to unprompted events and do not stop command matching.
    `tests/test_matching.py::test_answers_wrap_per_request_including_recorded_timeouts`,
    `tests/test_matching.py::test_end_policy_does_not_discard_pending_answers_or_stop_matching`,
    `tests/test_matching.py::test_loop_repeats_unprompted_events_and_hold_stops`
86. Request time is the final fragment's timestamp. The minimum recorded answer delay bounds
    latency, and every pending answer, including unknown-command fallbacks, bounds quiet_until.
    `tests/test_matching.py::test_request_time_is_its_final_fragment_and_chunk_boundaries_do_not_matter`,
    `tests/test_matching.py::test_quiet_until_includes_stream_unknown_and_every_deferred_answer`
87. An unknown request is counted and listed; its default PMTK reply uses flag 1 and its UBX NAK
    identifies the request's class and ID. Protocol framing works across UART chunks.
    `tests/test_matching.py::test_pmtk_fallback_has_the_command_flag_one_and_checksum`,
    `tests/test_matching.py::test_ubx_is_length_framed_even_with_terminators_in_payload_and_naks_class_id`,
    `tests/test_replay.py::test_unseen_requests_are_in_the_backend_report_and_json_properties`,
    `tests/test_replay.py::test_gnss_replay_answers_configuration_commands_through_the_link`

88. AT prompt recognition does not depend on response chunk boundaries. Command CR/CRLF
    separators, including a split CRLF before the response, never become prompted payload
    bytes; actual leading payload whitespace is preserved.
    `tests/test_matching.py::test_prompt_like_text_does_not_depend_on_response_fragmentation`,
    `tests/test_matching.py::test_at_command_endings_do_not_change_prompted_payload_bytes`,
    `tests/test_matching.py::test_at_prompt_switches_next_request_to_opaque_ctrl_z_payload`

89. Every unknown replay request retains its completion time and raw frame in arrival order,
    including repeated normalised keys and silent fallbacks. Known requests are excluded. The
    text and JSON reports preserve the full history and bytes after the wire log rolls over;
    the existing normalised counts remain available.
    `tests/test_matching.py::test_unseen_history_retains_raw_occurrences_and_completion_times_without_a_reply`,
    `tests/test_replay.py::test_unseen_requests_are_in_the_backend_report_and_json_properties`


## Simulated network

90. PPP delivers IPv4 only after both sides acknowledge LCP and IPCP; acknowledgments must match
    the pending request, including after termination and renegotiation. Framing survives arbitrary byte splits, escaping and compressed headers.
    `tests/test_net_ppp.py::test_ack_must_match_request_and_ipcp_waits_for_lcp`,
    `tests/test_net_ppp.py::test_fcs_reference_and_compressed_frames_at_every_split`,
    `tests/test_net_ppp.py::test_renegotiation_and_termination_invalidate_old_acknowledgments`
91. TCP honors the peer's receive window and MSS, delivers a retransmitted payload once, wraps
    sequence numbers, and notifies connection loss once.
    `tests/test_net_ip.py::test_tcp_handshake_data_duplicate_flow_control_fin_and_wrap`,
    `tests/test_net_ip.py::test_reset_notifies_once_and_unbound_port_refuses`
92. MQTT Will is emitted once on connection loss at the loss time, never after DISCONNECT.
    Keepalive deadlines use the board clock, and wildcard filters respect `$` topics.
    `tests/test_net_mqtt.py::test_will_on_transport_loss_once_and_never_on_disconnect`,
    `tests/test_net_mqtt.py::test_keepalive_expires_on_the_board_clock`,
    `tests/test_net_mqtt.py::test_topic_filters`
93. NTP returns world UTC plus emulated time; TLS verifies the per-test CA and named server and
    exposes application plaintext in its transcript.
    `tests/test_net_services.py::test_ntp_world_time_fraction_origin_and_era`,
    `tests/test_net_tls.py::test_tls_mqtt_and_transcript`,
    `tests/test_net_tls.py::test_tls_rejects_unknown_ca_or_wrong_hostname`
94. Only a modem with a simulated network treats receive overflow as fatal; binary pluses do not
    escape data mode without idle guards. Losing registration or supply resets the network.
    `tests/test_modem_model.py::test_only_a_network_route_fails_on_receive_overflow`,
    `tests/test_modem_model.py::test_network_selection_guarded_escape_and_loss_reset`
95. TLS determinism ignores times only after handshake starts and compares plaintext streams;
    changed plaintext or pre-handshake times still fail.
    `tests/test_check_determinism.py::test_network_tls_compares_plaintext_without_times_only_after_handshake`

96. Anonymous MQTT clients have distinct deterministic IDs before Will construction or takeover;
    they do not disconnect each other or collide with IDs already supplied by named clients.
    Empty IDs without CleanSession receive CONNACK code 2 and close.
    `tests/test_net_mqtt.py::test_anonymous_clients_have_independent_ids_and_wills`,
    `tests/test_net_mqtt.py::test_anonymous_id_skips_an_id_already_supplied_by_a_client`,
    `tests/test_net_mqtt.py::test_empty_id_without_clean_session_gets_identifier_rejected`
97. Acknowledged PPP MRU bounds every outgoing IPv4 packet. TCP preserves payload bytes while
    respecting MRU, MSS and the receive window; oversized UDP replies are logged and dropped.
    Renegotiation and redial cannot inherit the previous MRU, and unsupported small values are
    NAKed rather than acknowledged.
    `tests/test_net_mru.py::test_tcp_respects_mru_mss_and_window_without_losing_bytes`,
    `tests/test_net_mru.py::test_udp_sends_at_mru_and_logs_oversized_replies`,
    `tests/test_net_mru.py::test_mru_resets_on_renegotiation_termination_and_redial`,
    `tests/test_net_mru.py::test_mru_too_small_for_this_stack_gets_a_nak_not_an_ack`,
    `tests/test_net_mru.py::test_ppp_does_not_emit_an_oversized_ip_packet_even_when_called_directly`


104. The GPS alarm drives its buzzer only while a fresh valid fix is within the NVS-configured
    radius. Moving across the radius changes its output within two fix intervals; no fix,
    loss of fix, receiver silence or missing configuration cannot leave it on. Both destination
    and radius come from typed NVS values, including southern/western destinations. After silence
    immediately following the first observed on state, the buzzer stays on for 2.3 s and turns
    off by 2.7 s (the 2.5 s fix lifetime with observation/UART tolerance). Off-from-boot checks
    permit unknown only before initialization; subsequent releases and active pulses fail.
    `examples/gps_alarm_clock/tests/test_alarm.py`, `tests/test_alarm_example.py`


105. Named GPIO output conditions observe only what the firmware drives. Neither on nor off
    matches an undriven output, for either polarity, and constructing a handle queues no input.
    Saved conditions read the live state; waits report the line, desired state, pin and level.
    `tests/test_lines.py::test_named_output_conditions_follow_the_firmware_and_exclude_undriven`,
    `tests/test_lines.py::test_output_handles_resolve_pin_names_and_filter_events_without_driving`,
    `tests/test_gpio_echo.py::test_named_output_waits_report_the_line_and_state`

106. GPIO interval assertions require a known matching state at the starting instant and every
    subsequent firmware transition through now, catching pulses even after the output recovers.
    Input writes cannot satisfy an output assertion; none of these assertions advance time.
    `tests/test_lines.py::test_output_history_catches_short_pulses_even_after_state_recovers`,
    `tests/test_lines.py::test_output_history_requires_a_known_start_and_rejects_released_outputs`,
    `tests/test_lines.py::test_output_history_uses_start_state_and_includes_the_current_instant`,
    `tests/test_lines.py::test_output_history_ignores_inputs_other_pins_and_future_events`

107. The SD logger appends exactly one row per valid dated RMC, preserving existing rows and
    using a separate file for each GNSS UTC date, including New Year and leap day. GGA and
    invalid or missing fixes add no rows. Without a card it keeps processing new GNSS input
    and continuously asserts the storage-error pin after initialization fails.
    `examples/sd_data_logger/tests/test_logger.py`


108. Board interface properties and their fixture shortcuts share objects and state. Accessing
    console, GNSS, modem, lines or SD interfaces neither selects a UART source nor powers on;
    explicit `board.modem.network().broker` and the broker fixture select the same modem network.
    `tests/test_board_api.py::test_fixture_aliases_share_interfaces_without_powering_on`,
    `tests/test_board_api.py::test_modem_alias_does_not_select_a_source`,
    `tests/test_board_api.py::test_broker_alias_uses_the_same_modem_network`

109. Building an SD image does not insert it; each build has independent contents. Binary bytes
    and text line endings survive FAT preparation and reading. Board close removes all owned
    images and invalidates readers/builders, while external images survive. A saved builder
    cannot build after power-on, and a failed build leaves no partial image.
    `tests/test_card_image.py`,
    `tests/test_board_api.py::test_build_rechecks_power_and_backend_close_removes_images`


110. FAT paths never expand into multiple entries: readers and builders reject bracket patterns,
    even when a matching literal filename exists. Missing paths, file-as-directory paths and
    directory-as-file reads raise the corresponding standard filesystem exceptions. An invalid
    FAT filesystem remains a tool error, not a missing file.
    `tests/test_card_image.py::test_bracket_patterns_never_read_other_files`,
    `tests/test_card_image.py::test_paths_cannot_escape_or_expand_as_mtools_patterns`,
    `tests/test_card_image.py::test_readers_distinguish_missing_files_and_directories`,
    `tests/test_card_image.py::test_invalid_fat_remains_a_tool_error`
