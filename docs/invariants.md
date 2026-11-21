# Invariants

Rules about the domain that a test enforces: break one, and the named test fails. A rule that code
can break without any test failing is a note in the owning feature document, not an invariant
here. Numbers are never reused; a rule that stops holding is struck through, with the reason, so
that references to its number keep their meaning.

## Tests and backends

- **1.** A test that uses `board` is parametrised with the backend's name as its id
   (`test_marked[esp32sim]`), and its `board`, `embedded_services`, `app_path` and `build_dir` are
   set together; a test without `board` is not parametrised. Serial is opt-in; selecting it gives
   `esp,idf`, including for a test requesting only `console`.
   `tests/test_serial_backend.py::test_serial_is_opt_in_and_selects_esp_idf`,
   `tests/test_serial_backend.py::test_console_only_uses_serial_backend`.
   `tests/test_pytest_plugin.py::test_board_is_parametrised_with_the_backend_its_services_and_the_app`
- **2.** `--esp32tl-backend` takes only a backend's name: any other name is a usage error (exit code 4).
   Explicit selection runs that backend only; the default is esp32sim.
   `tests/test_serial_backend.py::test_serial_is_opt_in_and_selects_esp_idf`.
   `tests/test_pytest_plugin.py::test_backend_option_takes_a_backend_name_only`
- **3.** pytest-embedded's `--embedded-services`, `--app-path` and `--build-dir` are refused, with exit
   code 4 and no traceback on stdout, when a selected test uses `board`; `--app-path` is allowed
   when `-k` has deselected every test that uses `board`.
   `tests/test_pytest_plugin.py::test_pytest_embedded_options_are_refused_for_tests_with_a_board`,
   `tests/test_pytest_plugin.py::test_options_are_allowed_when_no_selected_test_uses_board`
- **4.** A test has one board: `--count` other than 1, and a `count` parametrised above 1, are refused.
   `tests/test_pytest_plugin.py::test_more_than_one_dut_is_refused`,
   `tests/test_pytest_plugin.py::test_a_parametrised_count_above_one_is_refused`
- **5.** An `app_path` marker without a path is a collection error that names the test.
   `tests/test_pytest_plugin.py::test_app_path_marker_needs_one_path`
- **6.** The plugin is registered through the `pytest11` entry point as `esp32tl`, and pytest loads it.
   `tests/test_package.py::test_plugin_is_registered_through_pytest11`,
   `tests/test_package.py::test_pytest_loaded_the_plugin`

- **25.** ~~A test whose `source` or `tap` markers ask for what a backend cannot give is skipped on that
    backend with the reason, before any fixture is set up, and runs on the others; one that asks for
    what no backend can give, or for an interface its profile does not have, is a collection error
    that names the test.~~ No longer holds since #65, which removed the `source` marker: see 47 for
    taps and 46 for sources.
- **26.** Every backend in `SERVICES` has a capability declaration; on `serial` an on-board UART is real
    only and only the console can be tapped.
    `tests/test_boards.py::test_every_backend_has_a_declaration`,
    `tests/test_boards.py::test_the_declaration_is_the_specification_s_table`
- **29.** ~~A board marker (`app_path`, `board_profile`, `source`, `tap`) that reaches a test from a
    `pytest.param`, or from a hook on the test, its class or its module, stops the run with a usage
    error, so no source a test asks for goes unchecked.~~ No longer holds since #65, which removed
    the `source` marker: see 48.
- **47.** ~~A test whose `tap` markers ask for what a backend cannot tap is skipped on that backend with the
    reason, before any fixture is set up, and runs on the others; one that asks for what no backend
    can tap, or for an interface its profile does not have, is a collection error that names the
    test.~~ No longer holds since #21, which removed the `tap` marker: see 46.
- **48.** ~~A board marker (`app_path`, `board_profile`, `tap`) that reaches a test from a `pytest.param`,
    or from a hook on the test, its class or its module, stops the run with a usage error, so no tap
    a test asks for goes unchecked.~~ No longer holds since #21, which removed the `tap` marker: see
    51.
- **51.** A board marker (`app_path`, `board_profile`) that reaches a test from a `pytest.param`, or from
    a hook on the test, its class or its module, stops the run with a usage error, so no profile a
    test runs on goes unchecked.
    `tests/test_pytest_plugin.py::test_a_board_marker_in_a_pytest_param_is_refused`,
    `tests/test_pytest_plugin.py::test_a_board_marker_a_hook_adds_is_refused`
- **222.** `board.wait_until` names a described condition, `esp32tl.condition(find, description)`,
    by its description, in `NeverHappened` and in the run report, on both backends; `bool()` of
    one raises `TypeError` naming the description, or its `service`, before and after a match.
    `tests/test_condition.py::test_bool_raises_before_and_after_a_match_naming_the_description`,
    `tests/test_condition.py::test_a_service_names_the_condition_in_the_type_error`,
    `tests/test_board_api.py::test_a_described_condition_names_its_timeout_and_refuses_bool`,
    `tests/test_serial_backend.py::test_console_reset_clock_tap_and_autoflash`

## Board profiles

- **27.** ~~The `lilygo-t-a7670g-r2` profile holds only what the specification gives, the chip, the
    modem's UART1 on GPIO 26/27 at 115200 baud, PWRKEY on GPIO 4 and RI on GPIO 33, and the
    pull-down on RI of stage 0's gap 6.~~ No longer holds since #66, which gives the profile
    LilyGO's GNSS route, flash and PSRAM: see 66.
- **66.** The `lilygo-t-a7670g-r2` profile holds what the specification gives, the chip, the modem's
    UART1 on GPIO 26/27 at 115200 baud, PWRKEY on GPIO 4 and RI on GPIO 33, the pull-down on RI
    of stage 0's gap 6, and what LilyGO's documentation gives, the GNSS receiver's UART2 on
    GPIO 21/22 at 9600 baud, 4 MB of flash and 4 MB of emulated PSRAM (8 MB physical), SPI SD
    SCLK/MOSI/MISO/CS on GPIO14/15/2/13, DTR/reset on 25/5, GNSS wake/PPS on 19/23 and peripheral
    power enable on 12; nothing else. The five added lines carry no pulls or pulse timings.
    They are observable without changing modem power, registration or AT settings; an inserted
    SD image works alongside both UART models.
    `tests/test_boards.py::test_the_lilygo_profile_holds_the_specification_and_lilygo_s_documentation`,
    `tests/test_modem_model.py::test_named_controls_are_observable_without_changing_modem_state`,
    `tests/test_sdspi.py::test_the_lilygo_slot_writes_an_image_alongside_both_uart_models`
- **28.** A profile that breaks the schema is refused with the file and the key at fault; a UART's TX is
    never an input-only GPIO, nor is an SD card slot's SCLK, MOSI or CS, though its MISO may be;
    and no GPIO serves two uses, the console's UART0 on GPIO 1 and 3 and the slot's four pins
    counted.
    Invalid UTF-8 raises `ProfileError`; through the marker it fails collection with the test id
    and resolved file path, without a traceback.
    `tests/test_pytest_plugin.py::test_invalid_local_profiles_fail_at_collection`,
    `tests/test_boards.py::test_a_profile_that_breaks_the_schema_is_refused_with_the_key`,
    `tests/test_boards.py::test_an_sd_card_slot_may_take_an_input_only_pin_for_miso`
- **123.** A local `board_profile(path=...)` resolves against the test file, independently of the
    invocation directory and marker inheritance. Its validated snapshot lasts for the pytest
    session, including after a symlink is retargeted at setup; different resolved paths and a
    bundled namesake never share cached profile data.
    A late switch between same-name files is refused, even with identical contents. Both
    backends and their recordings use the selected profile's TOML name; on esp32sim, its route
    metadata too.
    `tests/test_pytest_plugin.py::test_local_profiles_are_test_relative_and_inherited`,
    `tests/test_pytest_plugin.py::test_local_profiles_with_the_same_name_keep_separate_snapshots`,
    `tests/test_pytest_plugin.py::test_local_profile_snapshot_survives_retargeting_a_symlink`,
    `tests/test_pytest_plugin.py::test_late_local_profile_switch_with_the_same_name_is_refused`,
    `tests/test_pytest_plugin.py::test_a_tap_records_what_crossed_the_route_both_ways`,
    `tests/test_serial_backend.py::test_console_reset_clock_tap_and_autoflash`

## The esp32sim backend

- **7.** The runner is given every file of an ESP-IDF build's `flasher_args.json`, each at its
   offset; a PlatformIO build's files as 230 says.
   `tests/test_pytest_plugin.py::test_runner_command_flashes_every_file_at_its_offset`
- **8.** The backend refuses a build that is missing, built for another chip than the board
   profile's, asks for flash encryption, or is unfinished.
   `tests/test_pytest_plugin.py::test_load_app_refuses_what_the_runner_cannot_run`,
   `tests/test_pytest_plugin.py::test_load_app_takes_a_build_for_the_board_s_chip`
- **9.** The release and file names the backend's ROM paths are built from (`ROM_RELEASE`, and each
   chip's ELF and SHA-256 in `runtime.ROMS`) are the ones `scripts/fetch-rom-elf` fetches.
   `tests/test_pytest_plugin.py::test_rom_is_the_one_make_setup_fetches`
- **10.** ~~Once the runner has exited, an expect that has not matched fails at once, with an `EOF` that
    names the exit code and the runner's last output: whole lines, less than `EOF_TAIL_BYTES`,
    without colour codes, and a long last line kept.~~ No longer holds since #65, whose console
    reads the link on the test's clock and raises `RunEnded`: see 44.
- **11.** A runner that exits with an error of its own is an error of the test, even when every expect
    passed, and its exit is reported once.
    `tests/test_pytest_plugin.py::test_a_runner_that_exits_with_an_error_fails_the_test`,
    `tests/test_pytest_plugin.py::test_a_runner_that_fails_to_start_is_reported_once`,
    `tests/test_pytest_plugin.py::test_a_failure_of_another_kind_does_not_hide_the_runner_s_exit`
- **43.** Emulated time moves only while the test waits: the link holds every turn, before it polls the
    interfaces, until the test waits; `board.wait(d)` ends at the board's time `now + d` exactly;
    `wait_until` asks its condition at once and at every turn, at most 100 emulated milliseconds
    apart, in the test's thread, and returns what it returned; a condition that does not hold in
    time raises `NeverHappened`, an `AssertionError` naming it and the window it waited.
    `tests/test_clock.py::test_the_run_stands_still_until_the_test_waits`,
    `tests/test_clock.py::test_wait_until_asks_at_every_turn_and_returns_what_the_condition_returned`,
    `tests/test_clock.py::test_a_condition_that_never_holds_raises_never_happened_naming_it_and_the_window`,
    `tests/test_boardlink.py::test_a_hold_holds_every_turn_before_the_interfaces_are_polled`,
    `tests/test_pytest_plugin.py::test_the_test_s_waits_move_the_board_s_clock`
- **44.** Once the run has ended, a test that waits gets `RunEnded` at once, naming the board's time, what
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
- **45.** The runner starts at `board.power_on()`, not at setup: a test that never powers on starts no
    runner, and one that set up a source or a tap, or drove a line, and passed without powering on
    fails at teardown; a test that failed, was skipped or xfailed keeps its one outcome. A wait
    before power-on raises `BoardOff`, and a source or a tap after it `SourceError`.
    `tests/test_pytest_plugin.py::test_the_board_runs_the_profile_the_test_chose_and_starts_no_runner_unpowered`,
    `tests/test_pytest_plugin.py::test_a_test_that_sets_up_a_source_and_never_powers_on_fails_at_teardown`,
    `tests/test_pytest_plugin.py::test_a_test_that_taps_a_route_and_never_powers_on_fails_at_teardown`,
    `tests/test_pytest_plugin.py::test_the_lines_fixture_needs_a_profile_with_lines_and_a_drive_needs_power_on`,
    `tests/test_pytest_plugin.py::test_the_board_s_clock_and_sources_go_in_order`,
    `tests/test_gnss_fixture.py::test_a_source_given_out_of_order_fails_the_test`; the same holds
    for a card in the SD card slot at teardown, though the card may go in and out after power-on
    (147).
    `tests/test_sdspi.py::test_the_slot_used_out_of_order_fails_the_test`
- **46.** A source or a tap the backend's capability declaration does not give skips the test at the call
    that asks for it, reported at the test; one that no backend gives fails the test with
    `SourceError`.
    `tests/test_pytest_plugin.py::test_a_source_or_a_tap_the_backend_cannot_give_skips_at_the_call`,
    `tests/test_tap.py::test_a_tap_the_backend_refuses_is_not_made`
- **49.** Every test that uses `board` has a wall-time timeout, 600 s unless the session or the test sets
    one, 0 included; a run that hangs is ended by it, and the teardown ends the runner.
    `tests/test_pytest_plugin.py::test_a_test_that_uses_board_gets_a_wall_time_timeout_unless_one_is_set`,
    `tests/test_pytest_plugin.py::test_a_hung_run_is_ended_by_the_test_s_timeout`
- **50.** Every UART route of the board profile is on the link, listening and silent until the test gives
    it a source, and so is every GPIO line: the harness's `hello` watches the pins of all of them.
    `tests/test_pytest_plugin.py::test_every_route_and_line_of_the_profile_is_on_the_link`
- **55.** A line the board profile pulls is at its pull while the test does not drive it: from power-on,
    in every test on the profile, whether it uses `lines` or not, and again after `lines.release`.
    On the LilyGO profile RI reads 0 with no modem at the far end (stage 0's gap 6).
    `tests/test_gpio_echo.py::test_ri_reads_the_board_s_pull_down_from_power_on`,
    `tests/test_gpio_echo.py::test_the_pull_holds_in_a_test_that_does_not_use_lines`,
    `tests/test_cli.py::test_run_holds_the_board_s_pulls_on_its_lines`,
    `tests/test_gpio_echo.py::test_the_firmware_follows_ri_and_drives_pwrkey`,
    `tests/test_lines.py::test_the_board_s_pulls_go_out_in_the_first_turn_before_what_the_test_drives`,
    `tests/test_lines.py::test_a_drive_and_a_release_go_out_at_the_instant_the_test_made_them`
- **56.** What the test drives on a line takes effect at the board's instant it drove it, `board.now`, or
    from power-on when it drove it before; `lines.events()` holds every change of either end, a
    change of the firmware's at the time the firmware made it, not at the next look of a wait. An
    interface's GPIO input on a pin another interface is wired to fails the link with
    `{interface} sent {type} on GPIO {pin}, which another interface is wired to`.
    `tests/test_lines.py::test_a_drive_and_a_release_go_out_at_the_instant_the_test_made_them`,
    `tests/test_gpio_echo.py::test_the_firmware_follows_ri_and_drives_pwrkey`,
    `tests/test_gpio_echo.py::test_a_level_driven_before_power_on_holds_from_the_start`,
    `tests/test_boardlink.py::test_an_input_the_runner_would_refuse_is_refused_naming_its_interface`
- **63.** A card in the SD card slot writes through to the test's own image file: what the firmware
    wrote is in the file, readable with mtools, and a card the firmware only reads leaves the file
    as it was. The runner gets the card only once the test inserts one.
    `tests/test_sdspi.py::test_the_example_writes_its_files_into_the_image`,
    `tests/test_sdspi.py::test_a_card_without_a_file_system_does_not_mount`,
    `tests/test_pytest_plugin.py::test_runner_command_puts_a_card_in_the_slot_once_the_test_inserts_one`
- **147.** Once the run has started, with the supply on or off, `sd_card.remove()` and
    `sd_card.insert(image)` change the card at
    `board.now`: the runner applies the changes in the order the test made them, at the first
    slice boundary at or after that instant, and acknowledges each, and `sd_card.changes` lists
    them with the times it applied them; until then they are in `sd_card.not_reached`, and
    `sd_card.held`, the card the run has, does not count them. Once the
    card is out the firmware's file operations fail; what it had written before is in the image,
    and nothing after. A card put in, another image or the same one again, is mounted afresh by a
    firmware that mounts again, and a slot empty at power-on takes a card later.
    `tests/test_sd_remount.py::test_writes_fail_once_the_card_is_out_and_go_on_on_the_card_put_in`,
    `tests/test_sd_remount.py::test_a_slot_empty_at_power_on_takes_a_card_later`,
    `tests/test_sd_remount.py::test_a_card_swapped_with_the_supply_off_is_the_one_the_next_boot_finds`,
    `tests/test_sd_card_slot.py::test_with_the_board_on_each_change_goes_out_at_its_instant_and_is_acknowledged`,
    `tests/test_sd_card_slot.py::test_the_card_the_run_has_counts_only_the_changes_the_runner_acknowledged`
- **148.** The SD card slot holds one card at a time, and refuses a removal from an empty slot. Every
    insertion, before power-on or after it, checks the image's size and access and that no route
    replays from it or taps into it; a removal gives the image back. A change the runner refuses,
    which `sd_card.refused` keeps, or acknowledges with another image than the test asked for, or
    did not ask for, fails the link; only the slot sends `sd_card`, and a link has one slot.
    `tests/test_sd_card_slot.py::test_one_card_at_a_time_and_none_to_take_out_of_an_empty_slot`,
    `tests/test_sd_card_slot.py::test_every_insertion_checks_the_image_and_its_owner`,
    `tests/test_sd_card_slot.py::test_a_refused_or_another_card_fails_the_link`,
    `tests/test_sd_card_slot.py::test_an_acknowledgement_nobody_asked_for_fails_the_link`,
    `tests/test_sd_card_slot.py::test_only_the_slot_changes_the_card_and_a_board_has_one_slot`,
    `tests/test_sd_remount.py::test_a_card_the_runner_cannot_open_ends_the_run`
- **152.** The SD card's fault rules reach the runner in the link's next turn, at the board's
    instant the test gave them, before power-on at `t = 0`; only the slot sends `sd_fault` and
    `sd_clear`; each `sd_op` goes to the rule it names, which is spent once its count is reached,
    and a spent rule is not cleared; one that names no rule the test gave, or whose `op`, `action`
    or `delay` is not its rule's, fails the link. A rule needs a card and a run that has not ended,
    and one the card cannot take (an op the method does not take, a delay that is not a `Duration`
    of 1 ns to 10 s, a count outside 1 to 2**63 - 1, blocks the card does not have) is refused at
    the call.
    `tests/test_sd_faults.py::test_rules_go_out_in_the_next_turn_at_the_instant_the_test_gave_them`,
    `tests/test_sd_faults.py::test_what_a_rule_did_reaches_the_rule_and_the_slot`,
    `tests/test_sd_faults.py::test_an_sd_op_that_is_not_what_its_rule_does_fails_the_link`,
    `tests/test_sd_faults.py::test_a_rule_the_card_cannot_take_is_refused_at_the_call`,
    `tests/test_sd_faults.py::test_only_the_card_sends_sd_fault_and_sd_op_needs_a_card`,
    `tests/test_sd_faults.py::test_a_spent_rule_is_not_cleared`,
    `tests/test_sd_faults.py::test_a_run_that_has_ended_takes_no_rule`,
    `tests/test_sd_faults.py::test_an_sd_rule_the_runner_would_refuse_fails_the_link_naming_the_slot`,
    `tests/test_sd_faults.py::test_rules_and_card_changes_go_out_in_order_and_only_changes_are_acknowledged`,
    `tests/test_sdspi.py::test_a_rule_given_after_the_end_of_the_run_is_refused`
- **153.** On a firmware that has mounted the card, a failed block write or read is reported by the
    firmware and leaves the image as the card last accepted it, readable with mtools; once the rule
    is spent the firmware writes again. A delay passes on the board's clock: the firmware's own
    timeout, 5 s for ESP-IDF's writes, fires 5 s after the write, not in wall time; so does the 1 s
    read timeout ESP-IDF turns a failed read into.
    `examples/sd_data_logger/tests/test_logger.py::test_a_failed_read_reaches_the_firmware_as_the_driver_s_read_timeout`,
    `tests/test_sdspi.py::test_a_failed_read_of_the_boot_sector_fails_the_mount`,
    `tests/test_sdspi.py::test_a_read_slower_than_the_driver_s_timeout_fails_the_mount_on_the_board_s_clock`,
    `examples/sd_data_logger/tests/test_logger.py::test_a_failed_write_raises_the_error_pin_and_the_next_fix_writes_again`,
    `examples/sd_data_logger/tests/test_logger.py::test_a_slow_write_saves_its_row_later`,
    `examples/sd_data_logger/tests/test_logger.py::test_a_write_slower_than_the_driver_s_timeout_fails_on_the_board_s_clock`
- **67.** The runner gets the board profile's PSRAM as `--psram-mb`, in a test and in the `esp32tl`
    command: 0 on `esp32-devkitc`, 4 on `lilygo-t-a7670g-r2`.
    `tests/test_pytest_plugin.py::test_every_route_and_line_of_the_profile_is_on_the_link`,
    `tests/test_pytest_plugin.py::test_runner_command_gives_the_chip_the_profile_s_psram`,
    `tests/test_cli.py::test_run_gives_the_chip_the_profile_s_psram`
- **249.** On the esp32sim backend the chip's flash is the board profile's `flash_mb`, passed to
  the runner as `--flash-mb`, and without it the largest size that the build's bootloader and
  application image headers give; a build with neither header gets the runner's default. A
  `flash_mb` the runner does not have (1, 2, 4, 8 or 16 MB), a header that gives more flash than
  `flash_mb`, and a header above 16 MB with or without `flash_mb`, are refused when the board is set up,
  by the backend and by `esp32tl run`, before a runner starts, with a message that names the
  sizes and what to set; a header that gives less flash than `flash_mb` runs. `board.flash()`
  holds the whole of that flash.
  `tests/test_flash_size.py::test_the_runner_gets_the_profile_s_flash_else_the_header_s`,
  `tests/test_flash_size.py::test_a_profile_size_the_runner_does_not_have_is_refused`,
  `tests/test_flash_size.py::test_a_header_that_gives_more_than_the_profile_is_refused`,
  `tests/test_flash_size.py::test_a_header_that_gives_more_than_the_runner_has_is_refused`,
  `tests/test_flash_size.py::test_the_backend_refuses_at_setup_and_passes_the_size_at_power_on`,
  `tests/test_flash_size.py::test_esp32tl_run_refuses_a_build_for_more_flash_than_the_profile`,
  `tests/test_flash_size.py::test_an_8_mb_build_boots_on_8_and_16_mb_and_is_refused_on_4`

## The runner

- **12.** For every `Stop` variant the test lists, the stop line's name is the variant's own name; a
    variant upstream adds fails the build instead, because `stop_name` has no catch-all arm.
    `runner/src/tests.rs::stop_names_are_the_variant_names`
- **13.** The runner refuses, as a usage error, a command line without `--chip esp32`, without
    `--max-seconds` (a positive, finite number) or without `--flash-at`, and the flash and PSRAM
    sizes the classic ESP32 does not have (the test tries 0, 3, 32 and 2^44 MiB of flash and 2 MiB
    of PSRAM). It also refuses a `--board` that is neither `socket:PATH` nor `tcp:HOST:PORT`, a
    `--peer-timeout` that is negative, not finite, too large or rounds to 0 ns, and
    `--console socket` or `both` without `--board`.
    `runner/src/cli/tests.rs::usage_errors`
- **18.** With `--board`, the runner's messages are in non-decreasing `t`, and `seq` starts at 1 and
    grows by 1 over every event; the fake peer fails any session test that breaks either.
    `runner/src/session/tests.rs` (`FakePeer::send`)
- **19.** The first `sync` has `t = 0` and comes before the guest's first instruction, and inputs
    stamped 0 are in the RX FIFO at cycle 0.
    `runner/src/session/tests.rs::the_first_sync_comes_before_the_guest_runs_and_its_inputs_are_in_place`
- **20.** Before the `sync` at `B` the runner sends every event with `t ≤ B` and nothing later, also when
    `run` returns past `B`.
    `runner/src/session/tests.rs::events_after_the_block_point_wait_for_the_next_turn`,
    `runner/src/session/tests.rs::bytes_before_the_block_point_go_out_before_the_sync`
- **21.** An answer stamped exactly `t_event + latency` is on time, also with the guest asleep in WFI and
    on the real machine at the 40 MHz the core starts at; one stamped earlier than its turn's
    `sync` is a protocol error, exit code 3, with `stats`.
    `runner/src/session/tests.rs::an_answer_at_t_plus_latency_passes_with_the_guest_idle_in_wfi`,
    `runner/src/session/tests.rs::on_the_emulator_the_slices_stop_a_guest_asleep_in_waiti_at_the_block_point`,
    `runner/src/session/tests.rs::an_answer_before_t_plus_latency_is_late_and_ends_the_run`,
    `tests/test_runner_link.py::test_a_late_input_is_a_protocol_error_with_exit_code_3`
- **22.** A bus cycle converted to nanoseconds and back is the same cycle, past the 76 emulated seconds
    where `cycles × 10⁹` overflows 64 bits.
    `runner/src/protocol/tests.rs::time_conversion_rounds_as_section_3_says`
- **23.** The esp32sim revision the runner's `hello` reports is the one `runner/Cargo.toml` builds.
    `runner/src/protocol/tests.rs::the_esp32sim_revision_is_the_one_cargo_builds`
- **24.** With `--board`, `SIGTERM` while the runner waits for the peer ends the run with `stats`
    (`end: signal`) and exit 0, and bytes a write left when the signal came go out once, not
    twice.
    `tests/test_runner_link.py::test_sigterm_while_the_runner_waits_for_the_peer_ends_the_run_with_stats`,
    `runner/src/transport/tests.rs::a_write_cut_short_by_a_signal_goes_on_where_it_stopped_once_closing`
- **39.** An input on a pin outside the peer's `watch`, whether `uart_rx`, `gpio_in` or `release`, is a
    protocol error that ends the run with `stats` before the guest runs again.
    `runner/src/session/tests.rs::the_runner_refuses_what_the_protocol_forbids`
- **40.** A `gpio_in` or `release` reaches the chip at the slice boundary at its own cycle, before the
    first instruction when stamped 0, in the order of the input queue; on the real machine the
    guest sees the level there and its answer comes back as `gpio_out`.
    `runner/src/session/tests.rs::gpio_in_and_release_reach_the_chip_through_the_board_at_their_time`,
    `runner/src/session/tests.rs::on_the_emulator_gpio_in_reaches_the_guest_and_its_output_comes_back_as_gpio_out`
- **41.** After a guest reset, with a `watch` that is not empty, the runner sends a `gpio_out` at the
    reset's `t` with the watched pins as read from the chip, changing every pin the peer last saw
    at a level the chip no longer drives, and what the peer drives stays driven; on the real
    machine no pad is driven right after `reboot()`.
    `runner/src/session/tests.rs::after_a_reset_gpio_out_reports_the_pins_as_read_from_the_chip`,
    `runner/src/session/tests.rs::after_a_reset_gpio_out_changes_what_the_peer_last_saw_of_each_pad`,
    `runner/src/session/tests.rs::on_the_emulator_no_pad_is_driven_after_a_chip_reset`
- **149.** The runner changes the card in the slot of `--sd-spi` only at a slice boundary, at or after
    the `sd_card`'s `t`, in the order the peer sent them, and answers each with an `sd_card_ack`
    carrying no `seq`; a card for a full slot, or one whose image it cannot open or is not a card's
    size, is refused in the acknowledgement and leaves the slot as it was. An empty slot answers
    every byte with 0xff. A card taken out keeps in its image exactly the blocks it accepted, and
    the card put in next starts in SD mode, uninitialised. `sd_card` without `--sd-spi` is a
    protocol error.
    `runner/src/session/tests.rs::the_peer_takes_the_card_out_and_puts_cards_in_at_their_times`,
    `runner/src/session/tests.rs::a_card_the_slot_cannot_take_is_refused_and_the_slot_stays_as_it_was`,
    `runner/src/session/tests.rs::the_card_changes_with_the_supply_off_too`,
    `runner/src/session/tests.rs::sd_card_without_a_slot_is_a_protocol_error`,
    `runner/src/sd_card/tests.rs::an_empty_slot_answers_nothing_and_takes_one_card_at_a_time`,
    `runner/src/sd_card/tests.rs::a_card_taken_out_mid_write_keeps_the_blocks_it_accepted_and_comes_back_unpowered`,
    `runner/src/sd_card/tests.rs::a_power_cut_reaches_the_card_in_the_slot_and_nothing_else`
- **42.** A harness `hello` whose `watch` holds a pin the classic chip does not have (24, 28 to 31, 40
    and above) is a protocol error at the handshake, before the guest runs.
    `runner/src/session/tests.rs::a_handshake_mismatch_is_a_protocol_error_with_stats`
- **62.** The SD card hears a transaction only while its CS pin is a low output and its SCLK pin carries
    the master's clock, and the chip reads its answer only through the card's MISO pin; until a
    CMD0 with a good CRC it answers nothing. A card not selected leaves every byte at 0xff.
    `runner/src/sd_card/tests.rs::a_card_not_selected_or_not_clocked_hears_nothing`,
    `runner/src/sd_card/tests.rs::without_miso_the_chip_reads_high_but_the_card_listens`,
    `runner/src/sd_card/tests.rs::checks_crcs_of_cmd0_and_cmd8_always_and_of_the_rest_after_cmd59`
- **119.** With `--wifi`, every frame the station sent goes to the peer as an `eth_tx` event stamped at
     the slice boundary that took it, in order; an `eth_rx` reaches the station's link only when
     due and in order, waits in the runner while the link's queue is full, and is dropped and
     counted while the access point is off the air. `eth_rx` and `wifi_ap` without `--wifi` are
     protocol errors.
     `runner/src/session/tests.rs::the_station_frames_go_out_as_eth_tx_events_stamped_at_the_slice_boundary`,
     `runner/src/session/tests.rs::eth_rx_waits_for_room_in_the_link_and_goes_off_the_air_with_the_access_point`,
     `runner/src/session/tests.rs::eth_rx_and_wifi_ap_without_wifi_are_protocol_errors`
- **120.** An access point that leaves the air deauthenticates a station that has joined it, reason 3,
     and is gone only once that frame is off its queue; one that nobody joined is gone at once. One
     put back on the air while it is leaving comes back only once it has left, so the station still
     learns it must join again.
     `runner/src/session/tests.rs::on_the_emulator_the_access_point_deauthenticates_the_station_before_it_leaves`,
     `runner/src/session/tests.rs::an_access_point_put_back_while_it_is_leaving_comes_back_once_it_has_left`,
     `tests/test_http_request.py::test_rejoins_an_access_point_switched_off_and_on_at_one_instant`
- **208.** With `--bt`, every H4 packet the guest's host sends goes to the peer as an `hci_tx` event
     stamped with the cycle it was sent at, in order of `t` with the board's other messages and
     after a UART byte or pin change of the same cycle; its block point is `t + latency`. An
     `hci_rx` reaches the host's queue at its `t` and not before, the host reads one packet a
     poll, in order, and a chip reset or a power-on reset loses what it has not read. `hci_rx`
     without `--bt`, and one of 0 or more than 496 bytes, are protocol errors; the harness's codec
     refuses such an `hci_tx` or `hci_rx` too, and `BoardLink` takes `hci_rx` only from its one
     Bluetooth controller.
     `runner/src/session/tests.rs::each_hci_packet_goes_out_as_an_hci_tx_at_its_own_cycle_in_order_of_t`,
     `runner/src/session/tests.rs::an_hci_tx_sets_the_block_point_at_its_t_plus_latency`,
     `runner/src/session/tests.rs::an_hci_rx_reaches_the_guests_host_at_its_time_and_not_before`,
     `runner/src/session/tests.rs::a_chip_reset_loses_the_hci_packets_the_host_has_not_read`,
     `runner/src/session/tests.rs::a_power_on_reset_loses_the_hci_packets_the_host_has_not_read`,
     `runner/src/session/tests.rs::hci_rx_needs_bt_and_fits_the_vhci`,
     `runner/src/hci/tests.rs::a_chip_reset_loses_what_the_host_has_not_read_and_keeps_the_forwarder`,
     `runner/src/tests.rs::forward_leaves_the_forwarder_as_the_guests_controller`,
     `tests/test_link_protocol.py::test_an_h4_packet_is_1_to_496_bytes_either_way`,
     `tests/test_boardlink.py::test_one_bluetooth_controller`,
     `tests/test_boardlink.py::test_hci_rx_from_an_interface_that_is_not_the_controller_fails_the_link`

- **154.** Without a fault rule the SD card answers at once, as it always has. A rule applies to the
     operations that start once it is in place, at most one that fails or delays per operation,
     the one given last first (a rule that observes is beside it: invariant 264); each block of a
     multiple block read or write is one operation, and a block a CMD12 or a chip reset's CMD0 ends
     before it begins is none. A failed read sends the error token `0x04` and nothing more of the
     read; a failed write is answered `0x0d` and leaves its block on the image as it was. A cleared
     or spent rule applies no more.
     `runner/src/sd_card/tests.rs::without_a_rule_nothing_is_faulted_and_the_card_never_waits`,
     `runner/src/sd_card/tests.rs::a_failed_read_is_an_ecc_error_token_and_a_failed_write_leaves_the_block`,
     `runner/src/sd_card/tests.rs::each_block_of_a_multiple_block_read_is_one_operation`,
     `runner/src/sd_card/tests.rs::a_rule_is_not_met_by_the_block_a_cmd12_ends_before_it_begins`,
     `runner/src/sd_card/tests.rs::a_chip_reset_in_a_cmd18_uses_up_no_rule_when_the_host_starts_again_with_cmd0`,
     `runner/src/sd_card/tests.rs::a_failed_block_of_a_multiple_block_write_waits_for_the_stop_token`,
     `runner/src/sd_card/tests.rs::the_rule_given_last_applies_and_cleared_rules_apply_no_more`
- **151.** A delay is on the bus's clock: a delayed read sends 0xff until its time, a delayed write
     or erase holds MISO at 0x00 and hears nothing, a command included, until its time, and is in
     the image from its start. What a rule did goes to the peer as `sd_op` at the operation's
     start, with no `seq`. `sd_fault` and `sd_clear` without `--sd-spi` and an id used twice are
     protocol errors. A power cut, and taking the card out, end a delayed read and a busy time, and
     the rules stay, for the card put in next, as they do through a chip reset. Clearing a rule leaves
     a read or a write under way its delay.
     `runner/src/sd_card/tests.rs::a_delayed_read_holds_its_block_back_on_the_bus_clock`,
     `runner/src/sd_card/tests.rs::each_next_block_of_cmd18_waits_from_where_the_host_clocks_on_for_it`,
     `runner/src/sd_card/tests.rs::cleared_rules_act_no_more_but_a_running_delay_runs_out`,
     `runner/src/socket_board/tests.rs::the_card_hears_the_bus_cycle_and_its_reports_go_into_the_log`,
     `tests/test_sdspi.py::test_read_delays_hold_each_read_on_the_board_s_clock`,
     `runner/src/sd_card/tests.rs::a_delayed_write_keeps_the_card_busy_and_deaf_until_it_is_done`,
     `runner/src/sd_card/tests.rs::an_erase_meets_a_rule_on_any_block_of_its_range_and_is_busy_after_its_r1`,
     `runner/src/session/tests.rs::an_sd_fault_comes_due_on_the_card_and_what_it_did_goes_out_as_sd_op`,
     `runner/src/session/tests.rs::sd_fault_and_sd_clear_need_the_slot_and_an_id_is_used_once`,
     `runner/src/sd_card/tests.rs::a_power_cut_ends_a_delay_and_leaves_the_rules`,
     `runner/src/sd_card/tests.rs::rules_stay_through_a_change_of_card_and_a_delay_goes_with_the_card`,
     `tests/test_sd_remount.py::test_a_fault_rule_stays_through_a_change_of_card_until_it_is_cleared`,
     `tests/test_sdspi.py::test_fault_rules_are_the_test_s_and_outlast_a_power_cycle`,
     `tests/test_sdspi.py::test_fault_rules_outlast_a_reset_of_the_chip`
- **264.** A rule that observes changes nothing the guest sees: the card's bytes, their bus cycles
     and the image are those of the card without it, and a firmware run with and without it
     prints the same console at the same times and leaves the same image. An operation meets
     every rule that observes it as well as the one that fails or delays it, whichever was given
     last; each reports it with its own `sd_op`, in the order given, and counts it against its own
     `count`. The writes it reports cover every block the run changed.
     `runner/src/sd_card/tests.rs::a_rule_that_observes_changes_nothing_and_reports_what_it_meets`,
     `runner/src/sd_card/tests.rs::every_rule_that_observes_an_operation_reports_it_beside_the_one_that_acts`,
     `tests/test_sd_faults.py::test_a_rule_that_observes_goes_out_without_a_delay_and_says_what_it_met`,
     `tests/test_sdspi.py::test_a_rule_that_observes_changes_nothing_the_firmware_sees`
- **217.** A deep sleep is one slice to its wake: neither the next millisecond nor `latency` ends a
  slice in it, the runner moves the RTC through it in long steps to short of its alarm, and the wake
  still comes in esp32sim's first idle round at or after the alarm's tick, through a `reset` at its
  cycle, with the turn its latency calls for after it. An awake chip is not stepped through; a
  sleep with the RTC watchdog on, and any sleep under `--realtime`, keeps an awake chip's slice
  ends.
  `runner/src/session/tests.rs::a_deep_sleep_is_one_slice_to_its_wake`,
  `runner/src/session/tests.rs::a_deep_sleep_without_a_wake_goes_on_to_the_block_point`,
  `runner/src/session/tests.rs::a_deep_sleep_the_run_loop_cannot_step_through_keeps_its_slices`,
  `runner/src/session/tests.rs::realtime_paces_a_deep_sleep_like_any_other_stretch`,
  `runner/src/session/tests.rs::on_the_emulator_a_skipped_sleep_wakes_at_the_tick_of_its_alarm`,
  `runner/src/session/tests.rs::on_the_emulator_only_a_deep_sleep_is_skipped`,
  `examples/periodic_wake/tests/test_periodic_wake.py::test_a_day_of_wakes_spends_less_wall_time_asleep_than_booting`
- **215.** A received byte that finds a UART's 128-byte RX FIFO full is lost: the runner counts and
    warns it as `rxfifo_ovf`, the UART raises `UART_RXFIFO_OVF_INT_RAW`, and the byte never reaches
    the FIFO, not even once the firmware has read it empty; a byte at another baud is lost as a
    frame error and raises `UART_FRM_ERR_INT_RAW`.
    `runner/src/session/tests.rs::on_the_emulator_a_byte_into_a_full_rx_fifo_is_lost_and_raises_the_overflow_interrupt`,
    `runner/src/session/tests.rs::a_full_rx_fifo_drops_bytes_and_a_warning_is_cut_by_other_messages`,
    `runner/src/session/tests.rs::a_baud_mismatch_is_one_merged_frm_err_warning`

## Recordings and replay

- **30.** A committed recording reads and writes back byte for byte, and a recording that breaks the
    format is refused with its file and line.
    `tests/test_recording.py::test_a_committed_recording_reads_and_writes_back_byte_for_byte`,
    `tests/test_recording.py::test_a_recording_that_breaks_the_format_is_refused_with_its_line`
- **31.** Times on two clocks are never compared: `Event.since` across a clock switch raises
    `ClockError`, and a replay refuses a recording with a time on a clock other than its
    backend's.
    `tests/test_recording.py::test_times_compare_on_one_clock_only`,
    `tests/test_replay.py::test_what_a_stream_replay_cannot_replay_is_refused`
- **32.** A stream replay sends each event in the turn at its recorded time, never before, and its
    `quiet_until` is the next event's time.
    `tests/test_replay.py::test_each_event_goes_out_at_its_time_and_bounds_the_turn_before`,
    `tests/test_replay.py::test_a_replay_through_the_link_sends_each_event_in_the_turn_at_its_time`
- **33.** ~~A run with a declared source does not boot until the test has given it: the link's first
    turn waits for `start`, a `console.expect` before then fails at once with `SourceError`, and a
    run never started ends with `stop` before the guest boots, and fails a test whose setup and call
    passed without giving it at teardown; a test that failed, was skipped or xfailed keeps its one
    outcome.~~ No longer holds since #65: the source is given before `board.power_on()`, which
    starts the run (see 45).
- **34.** A recording replayed with `gnss.replay(path)` reaches the firmware's UART on the profile's GNSS
    route: `nmea_reader` prints the fixes of `gnss-generated-1.jsonl`.
    `tests/test_gnss_fixture.py::test_a_replay_reaches_nmea_reader_on_the_gnss_route`
- **35.** A replay at another baud than the firmware's UART receives at fails the test at once, with the
    interface and both bauds, rather than by a timeout; in `esp32tl replay` it is exit code 1.
    `tests/test_boardlink.py::test_a_frame_error_fails_the_run_at_once`,
    `tests/test_gnss_fixture.py::test_reports_a_fix_fails_on_a_baud_mismatch`,
    `tests/test_cli.py::test_a_baud_mismatch_exits_1_with_the_link_s_error`
- **36.** The first milestone's test, `test_reports_a_fix`, passes on `gnss-generated-1.jsonl` and fails
    when one digit of the recording changes.
    `tests/test_nmea_reader.py::test_reports_a_fix`,
    `tests/test_gnss_fixture.py::test_reports_a_fix_fails_on_a_changed_recording`
- **37.** A replay runs on the emulated clock: ten fixes, ten emulated seconds, reach the console,
    the board's clock reads just past 10 s, the runner says the emulated clock and counts no
    resyncs, and the ten emulated seconds take less than ten wall seconds: nothing paced the run
    to wall time.
    `tests/test_nmea_reader.py::test_time_is_emulated`
- **265.** What a real device sent replays on the emulated clock once converted, and the same way
    every run: `retime` keeps a wall-clock recording's `in` events at their spacing from the first,
    which goes in at the start, drops its `out` events and refuses a recording with a time on
    another clock; `from_nmea` makes one event an epoch, an epoch a period apart, and refuses a log
    with a bad checksum, a line that is no sentence, times that skip or go back, or an epoch that
    does not fit its period at the route's baud, naming the line. A capture retimed replays into
    `nmea_reader` with the fixes the capture's run printed, the same in two runs, and the committed
    log converts to the committed recording byte for byte.
    `tests/test_recording.py::test_retime_keeps_the_in_events_from_the_first_at_the_start_and_drops_the_out_ones`,
    `tests/test_recording.py::test_retime_refuses`,
    `tests/test_recording.py::test_a_log_is_an_epoch_an_event_a_period_apart`,
    `tests/test_recording.py::test_a_malformed_log_is_refused_with_its_line`,
    `tests/test_recording.py::test_the_committed_log_converts_to_the_committed_recording`,
    `tests/test_real_source.py::test_a_capture_converted_replays_on_the_emulated_clock_the_same_every_run`,
    `tests/test_nmea_reader.py::test_a_real_receiver_s_log_converted_gives_its_fixes`
- **52.** A tap records every byte that crossed its route, `out` of the ESP32 and `in` from whatever is
    at the far end, at the time it crossed, and writes it once the run has ended as a recording in
    time order that reads back: a tap of a replay holds the replay's events as they were recorded.
    `tests/test_tap.py::test_a_tap_writes_its_events_in_time_order_with_the_header`,
    `tests/test_tap.py::test_a_tapped_uart_records_both_ways_whatever_its_source`,
    `tests/test_pytest_plugin.py::test_a_tap_records_what_crossed_the_route_both_ways`,
    `tests/test_gnss_fixture.py::test_a_tap_of_a_replay_records_what_went_in`
- **54.** A tap never writes over a file the run replays from or another route is tapped into, and a
    replay never reads a file a route is tapped into: both are refused at the call. Nor do a tap
    and the SD card's image share a file, whichever comes first, and a replay never reads the
    card's image.
    `tests/test_tap.py::test_a_tap_never_writes_over_a_file_the_run_reads_or_writes`,
    `tests/test_sdspi.py::test_no_tap_writes_over_the_card_s_image`,
    `tests/test_sdspi.py::test_a_card_does_not_take_a_tap_s_file`
- **53.** The report of a test that failed shows the run on the board's clock: the test's steps,
    the console's lines and the bytes on every route, in the order of the board's time; that of a
    test that passed shows the bytes on every route, not the run. A profile with an SD card slot
    adds the card's image, and the test's `sd_card.insert` is one of the steps.
    `tests/test_pytest_plugin.py::test_the_report_of_a_failing_test_shows_the_run_on_the_board_s_clock`,
    `tests/test_pytest_plugin.py::test_a_tap_records_what_crossed_the_route_both_ways`,
    `tests/test_run_report.py::test_entries_are_shown_in_the_order_of_the_board_s_time`,
    `tests/test_sdspi.py::test_the_example_writes_its_files_into_the_image`
- **65.** Two runs of one recording are identical: the console line by line with the board's times, the
    tap but for its header's `created`, and the runner's stats, for `nmea_reader` fed each
    generated recording and fed the receiver model. `make determinism` (the `determinism` CI job)
    runs these tests twice and fails on any difference.
    `tests/test_determinism.py::test_a_replay`,
    `tests/test_determinism.py::test_the_receiver_model`
- **290.** Two runs of `sd_remount` whose card fails one write, is kept busy by four, and is taken
    out and put back give the same console at the same board times, the same operations the rules
    met and changes of card at the same times, and the same `LOG.TXT`. `make determinism` runs it
    twice; `scripts/check-determinism.py --runs N --load N` and pytest's `-n` run it more times in
    xdist workers beside busy loops (#287).
    `tests/test_determinism.py::test_sd_card_faults`

## Models, units and console lines

- **57.** The board's durations are `Duration`s of whole nanoseconds, 0 or more: `board.wait` and
    `wait_until` refuse a plain number. Quantities of different kinds never combine or compare
    equal, but for a speed times a time, a distance, and a distance over a time, a speed.
    `tests/test_units.py::test_quantities_of_different_kinds_do_not_mix`,
    `tests/test_units.py::test_a_speed_over_a_time_is_a_distance_and_back`,
    `tests/test_clock.py::test_the_board_s_durations_are_durations_of_0_or_more`,
    `tests/test_pytest_plugin.py::test_the_test_s_waits_move_the_board_s_clock`
- **58.** A position moved by a distance on a heading is that distance away, along the great circle that
    leaves on that heading, in both hemispheres, across the antimeridian and over a pole.
    `tests/test_units.py::test_a_move_follows_the_great_circle_and_measures_its_distance`,
    `tests/test_units.py::test_a_move_crosses_the_antimeridian_and_a_pole`
- **59.** The GNSS receiver model sends an RMC and a GGA with the same UTC time once a second, on whole
    seconds of the world's time, each sentence checksummed and at most 82 characters; what the test
    tells it holds from the board's instant, in the epoch at that instant too; the epochs of a
    silence are never sent.
    `tests/test_gnss_model.py::test_a_fix_is_an_rmc_and_a_gga_with_the_same_time_once_a_second`,
    `tests/test_gnss_model.py::test_epochs_fall_on_whole_seconds_of_the_world_s_time`,
    `tests/test_gnss_model.py::test_a_sentence_is_checksummed_and_at_most_82_characters`,
    `tests/test_gnss_model.py::test_each_change_holds_from_the_board_s_instant`,
    `tests/test_gnss_model.py::test_a_change_at_an_epoch_s_instant_is_in_that_epoch`,
    `tests/test_gnss_model.py::test_the_epochs_of_a_silence_stay_unsent_when_no_turn_came_in_it`
- **60.** `nmea_reader` fed by the receiver model reports where the receiver is: its first fix, a
    kilometre of movement to within 0.2 m, no fix while the fix is lost, the fix again in the epoch
    of the instant it comes back, in both hemispheres.
    `tests/test_nmea_reader_model.py::test_reports_where_the_receiver_is`,
    `tests/test_nmea_reader_model.py::test_reports_the_southern_and_western_hemispheres`,
    `tests/test_nmea_reader_model.py::test_follows_the_receiver_as_it_moves`,
    `tests/test_nmea_reader_model.py::test_says_nothing_while_the_receiver_has_no_fix`,
    `tests/test_nmea_reader_model.py::test_reports_again_when_the_fix_comes_back`
- **61.** A console line is stamped with the board's time its line ending came at, without its line
    ending and colour codes, and is a line only once it has ended; `console.line(pattern)` finds
    only lines that end after it was made, and after the last line it found.
    `tests/test_clock.py::test_console_lines_are_stamped_with_the_time_their_ending_came_at`,
    `tests/test_clock.py::test_a_line_condition_finds_the_first_matching_line_from_when_it_was_made`
- **224.** A model of the test's in `Duration`s and the same model in nanoseconds put the same bytes
    in at the same instants and hear the same bytes at the same instants, at the far end of a UART
    and of an SPP peer; what a model in `Duration`s returns wrong ends the run or fails the air as
    it would in nanoseconds, with its message in `Duration`s.
    `tests/test_duration_model.py::test_a_model_in_durations_says_on_a_uart_what_one_in_nanoseconds_says`,
    `tests/test_duration_model.py::test_a_model_in_durations_says_on_an_spp_peer_what_one_in_nanoseconds_says`,
    `tests/test_duration_model.py::test_what_a_model_in_durations_gets_wrong_on_a_uart_ends_the_run_in_durations`,
    `tests/test_duration_model.py::test_what_a_model_in_durations_answers_wrong_fails_the_air_in_durations`,
    `tests/test_duration_model.py::test_with_two_faults_in_one_turn_both_kinds_report_the_same_one`

## Bluetooth peer

- **68.** Time on the air moves only when the test moves it, and asyncio's timers fire at their time on
    the board's clock, never the host's, on its exact nanosecond; what a timer sets off runs at the
    timer's instant, wherever the float of its time lands.
    `tests/test_air.py::test_timers_fire_at_their_board_time_and_only_while_the_test_moves_it`,
    `tests/test_air.py::test_a_party_is_polled_at_every_instant_the_air_stops_at`,
    `tests/test_air.py::test_a_timer_set_at_any_instant_falls_on_its_exact_nanosecond`,
    `tests/test_air.py::test_what_a_timer_sets_off_runs_at_the_timer_s_instant`,
    `tests/test_air.py::test_a_timer_a_nanosecond_away_fires_at_the_next_nanosecond`
- **69.** The SPP peer is found by its name and pairs only with its PIN: another PIN fails the
    authentication, detaches the link and leaves no key on either side; a device that paired
    authenticates later with its key and no PIN, and one whose key the peer lost is refused.
    `tests/test_spp_peer.py::test_the_device_finds_the_peer_by_its_name_and_its_answer_comes_on_the_boards_clock`,
    `tests/test_spp_peer.py::test_a_wrong_pin_fails_the_authentication_and_detaches_the_link`,
    `tests/test_spp_peer.py::test_a_device_that_paired_authenticates_with_its_key_without_a_pin`,
    `tests/test_spp_peer.py::test_a_peer_that_lost_the_key_refuses_the_device_that_kept_it`
- **70.** What the device writes on the SPP channel reaches the model at the instant it arrives, and what
    the model sends goes out at the time it gives, never earlier, in order; what it sends while no
    device holds the channel is lost; two runs cross the channel at the same instants.
    `tests/test_spp_peer.py::test_the_device_finds_the_peer_by_its_name_and_its_answer_comes_on_the_boards_clock`,
    `tests/test_spp_peer.py::test_what_the_source_sends_unprompted_goes_out_at_its_time_or_is_lost`,
    `tests/test_spp_peer.py::test_what_the_source_sends_after_the_channel_closes_is_lost`,
    `tests/test_spp_peer.py::test_a_source_that_gets_its_answer_wrong_fails_the_air_naming_the_peer`,
    `tests/test_spp_peer.py::test_two_runs_cross_the_channel_at_the_same_instants`
- **71.** The SPP channel opens to one device at a time, and, unless the peer is made with
    `secure=False`, only on a link that device authenticated by the time it opens the channel, at
    its SABM, also when it authenticates after asking for it, at its PN; one refused at the SABM
    may authenticate and open it, and a refusal lets the channel go. Two that ask at one instant
    get it one.
    `tests/test_spp_peer.py::test_the_channel_opens_to_one_device_at_a_time`,
    `tests/test_spp_peer.py::test_two_devices_that_ask_for_the_channel_at_one_instant_get_it_one`,
    `tests/test_spp_peer.py::test_the_channel_opens_only_on_a_link_the_device_authenticated`,
    `tests/test_spp_peer.py::test_an_authenticated_link_needs_no_encryption_for_the_channel`,
    `tests/test_spp_peer.py::test_a_peer_that_is_not_secure_opens_the_channel_on_any_link`,
    `tests/test_spp_peer.py::test_the_channel_opens_when_the_device_authenticates_between_pn_and_sabm`,
    `tests/test_spp_peer.py::test_a_device_refused_at_sabm_may_authenticate_and_open_the_channel`,
    `tests/test_bluetooth_guest.py::test_bt_spp_initiator_s_session_finds_pairs_and_opens_the_channel`,
    `tests/test_bluetooth_guest.py::test_the_channel_is_refused_at_sabm_on_a_link_that_never_authenticated`
- **162.** A peer that leaves, switched off or out of range, sends nothing: the device's link falls
    with `CONNECTION_TIMEOUT` exactly at the supervision timeout after it left, a page ends in
    `PAGE_TIMEOUT` exactly at the page timeout, and an inquiry does not find it until it returns.
    `tests/test_spp_peer.py::test_a_link_to_a_peer_that_left_falls_at_the_supervision_timeout_without_a_detach`,
    `tests/test_spp_peer.py::test_a_page_to_a_peer_that_left_ends_at_the_page_timeout`,
    `tests/test_spp_peer.py::test_a_page_ends_exactly_at_the_page_timeout_whatever_instant_it_starts_at`,
    `tests/test_spp_peer.py::test_an_inquiry_does_not_find_a_peer_that_left_until_it_returns`
- **163.** A peer keeps its link keys across leaving and returning, however it left; only
    `forget_keys` loses them.
    `tests/test_spp_peer.py::test_a_device_authenticates_with_its_key_after_the_peer_returns`,
    `tests/test_spp_peer.py::test_a_peer_that_forgot_its_keys_refuses_after_it_returns`
- **164.** Coming back does not revive a link: nothing crosses it, it falls at its timeout counted
    from when contact was lost, and the peer can be discovered and connected to by another device
    meanwhile.
    `tests/test_spp_peer.py::test_coming_back_before_the_timeout_does_not_revive_the_link`,
    `tests/test_spp_peer.py::test_another_device_can_connect_to_a_peer_back_before_the_old_link_falls`,
    `tests/test_spp_peer.py::test_leaving_again_does_not_restart_the_timeout_of_a_link_that_lost_contact`
- **165.** A power loss calls `supply` on a source that has it and drops what it queued; out of range
    the source runs on, and what it sends counts in `lost`.
    `tests/test_spp_peer.py::test_a_power_loss_tells_a_supplied_source_and_drops_what_it_had_queued`,
    `tests/test_spp_peer.py::test_out_of_range_does_not_tell_a_supplied_source_and_what_it_sends_is_lost`,
    `tests/test_spp_peer.py::test_a_power_loss_leaves_a_source_that_is_not_supplied_running_and_it_is_lost`
- **166.** The page timeout and link supervision timeout a host writes take effect; only a central
    may write the second, on both ends, and 0 means no supervision.
    `tests/test_spp_peer.py::test_the_page_timeout_a_host_writes_takes_effect`,
    `tests/test_spp_peer.py::test_the_supervision_timeout_a_central_writes_takes_effect_on_both_ends`,
    `tests/test_spp_peer.py::test_a_peripheral_may_not_write_the_supervision_timeout`,
    `tests/test_spp_peer.py::test_a_supervision_timeout_of_zero_means_the_link_never_falls`
- **167.** Two runs in which a peer leaves and returns do the same at the same instants.
    `tests/test_spp_peer.py::test_two_runs_lose_the_peer_at_the_same_instants`
- **168.** One inquiry finds every device present, each with its own class of device and RSSI, in the
    modes that carry an RSSI; the next inquiry gets an RSSI set meanwhile.
    `tests/test_spp_peer.py::test_one_inquiry_finds_several_devices_each_with_its_own_rssi_and_class`,
    `tests/test_spp_peer.py::test_a_device_answers_with_the_rssi_it_has_at_each_inquiry`,
    `tests/test_spp_peer.py::test_the_rssi_is_given_in_the_mode_that_has_it_and_not_in_the_plain_one`
- **169.** A `BluetoothDevice` does not answer for SPP in SDP, and has no RFCOMM channel.
    `tests/test_spp_peer.py::test_a_device_without_spp_says_so_in_sdp_and_has_no_channel`
- **170.** A device that leaves during its own page can be reached again once both are back, and a
    power loss ends a page, a remote name request and an inquiry, whose timers do not end the next;
    a device that left finds nobody.
    `tests/test_spp_peer.py::test_a_device_that_leaves_during_its_own_page_can_be_reached_again`,
    `tests/test_spp_peer.py::test_a_device_that_left_during_its_own_page_can_be_paged_once_both_are_back`,
    `tests/test_spp_peer.py::test_a_page_that_outlasts_the_link_falling_at_the_other_end_lets_the_pair_go`,
    `tests/test_spp_peer.py::test_a_power_loss_ends_a_page_and_its_timer_does_not_end_the_next`,
    `tests/test_spp_peer.py::test_a_power_loss_ends_a_remote_name_request_and_its_timer_does_not_end_the_next`,
    `tests/test_spp_peer.py::test_a_power_loss_ends_an_inquiry`,
    `tests/test_spp_peer.py::test_a_power_loss_ends_an_inquiry_that_would_restart_itself`,
    `tests/test_spp_peer.py::test_a_power_loss_ends_an_authentication_with_a_status`,
    `tests/test_spp_peer.py::test_a_device_that_left_finds_nothing_in_its_own_inquiry`
- **171.** A remote name request nobody answers ends in `PAGE_TIMEOUT` exactly at the page timeout; a
    second to the same peer while one is pending ends when the first does.
    `tests/test_spp_peer.py::test_a_remote_name_request_to_a_peer_that_left_ends_at_the_page_timeout`,
    `tests/test_spp_peer.py::test_a_remote_name_request_ends_exactly_at_the_page_timeout_whatever_instant_it_starts_at`,
    `tests/test_spp_peer.py::test_a_second_remote_name_request_ends_when_the_first_does`,
    `tests/test_spp_peer.py::test_a_remote_name_request_is_answered_at_once_when_the_peer_is_there`
- **172.** A target that heard a page loses contact when the pager leaves, also after that page
    ended, and its end falls at the supervision timeout counted from then. A target whose host has not
    answered tells it of no link when its end goes, and a host that answers later, within its
    connection accept timeout (221), gets a failed `Connection_Complete`, unless this side holds a
    link or its own page for that address (205):
    such an answer, late because this side's entry for it went, makes no link,
    completes no later page, leaves a working link as it is, and does not fail the air. An acceptance
    that comes when only the pager's page has ended finds its entry, and makes a link, except with
    `role=CENTRAL` (205).
    `tests/test_spp_peer.py::test_a_target_that_heard_the_page_loses_contact_when_the_pager_leaves`,
    `tests/test_spp_peer.py::test_a_pager_whose_power_cycles_before_the_host_answers_gets_no_link`,
    `tests/test_spp_peer.py::test_a_target_that_leaves_before_its_host_answers_tells_its_host_of_no_link`,
    `tests/test_spp_peer.py::test_a_host_that_answers_a_page_after_its_end_fell_gets_no_link`,
    `tests/test_spp_peer.py::test_a_late_answer_does_not_connect_the_next_page`,
    `tests/test_spp_peer.py::test_an_answer_that_came_in_time_is_not_counted_late_afterwards`,
    `tests/test_spp_peer.py::test_a_late_answer_leaves_a_working_link_alone`,
    `tests/test_spp_peer.py::test_a_late_answer_does_not_complete_a_page_to_a_device_that_left`,
    `tests/test_spp_peer.py::test_a_page_tried_again_with_a_slow_host_does_not_fail_the_air`,
    `tests/test_spp_peer.py::test_two_answers_to_pages_that_ended_do_not_fail_the_air`,
    `tests/test_spp_peer.py::test_every_late_answer_is_counted_before_a_new_page_is_answered`,
    `tests/test_spp_peer.py::test_an_answer_owed_after_the_link_was_ended_does_not_fail_the_air`,
    `tests/test_spp_peer.py::test_an_answer_after_both_crossing_pages_ended_makes_a_link`,
    `tests/test_spp_peer.py::test_a_pager_that_leaves_after_its_page_ended_is_lost_to_the_target`,
    `tests/test_spp_peer.py::test_a_pager_that_rejected_a_page_and_leaves_during_its_own_is_lost_to_the_target`,
    `tests/test_spp_peer.py::test_a_device_that_leaves_is_not_lost_to_a_page_it_never_heard`,
    `tests/test_spp_peer.py::test_a_page_that_ended_leaves_no_timer_to_fell_the_next_link`,
    `tests/test_spp_peer.py::test_a_page_longer_than_the_supervision_timeout_keeps_its_entry_until_it_ends`,
    `tests/test_spp_peer.py::test_an_entry_that_goes_with_a_failed_page_leaves_no_timer_to_fell_the_next_link`,
    `tests/test_spp_peer.py::test_an_acceptance_on_a_working_link_is_the_hosts_own_error`,
    `tests/test_spp_peer.py::test_an_answer_nothing_is_owed_for_is_the_hosts_own_error`,
    `tests/test_spp_peer.py::test_a_power_loss_at_the_instant_a_page_is_heard_does_not_fail_the_air`
- **189.** A host that rejects a page gets its answer, and the pager's `Connection_Complete`
    carries the reason at the instant of the rejection; the target keeps no entry of the page it
    rejected, its host goes on working, and both can connect afterwards. A rejection is an answer: a
    late one reaches no pager, no later page and no working link, and does not fail the air. A
    reason outside 0x0D to 0x0F is refused, and the answer stays owed; one with nothing owed is the
    host's own error, also on a working link. A page of this side's own that fails does not take the
    page it heard, and neither that page nor one of this side's own under way lets a second page
    start over it; one the controller refused has no timer and stops no later page. An answer a host
    never gave takes no later page's, unless its end fell meanwhile, and then only until its
    connection accept timeout (221). A failed answer of this side's
    host leaves no entry, also with an answer never given still counted and a page of this side
    under way; an entry that goes takes its timer along.
    `tests/test_spp_peer.py::test_a_pager_gets_the_reason_at_the_instant_of_the_rejection`,
    `tests/test_spp_peer.py::test_a_host_that_rejects_a_page_goes_on_working`,
    `tests/test_spp_peer.py::test_both_can_connect_after_a_rejection`,
    `tests/test_spp_peer.py::test_a_rejection_of_a_page_that_ended_is_not_heard_by_the_pager`,
    `tests/test_spp_peer.py::test_a_rejection_after_the_end_fell_is_late_and_does_not_end_the_next_page`,
    `tests/test_spp_peer.py::test_a_late_rejection_leaves_a_working_link_alone`,
    `tests/test_spp_peer.py::test_the_answer_after_the_rejection_of_a_page_tried_again_is_late`,
    `tests/test_spp_peer.py::test_a_rejection_leaves_this_sides_own_page_to_end_by_itself`,
    `tests/test_spp_peer.py::test_an_answer_owed_while_this_side_pages_again_does_not_touch_that_page`,
    `tests/test_spp_peer.py::test_a_rejection_ends_the_supervision_of_a_page_whose_pager_left`,
    `tests/test_spp_peer.py::test_a_reason_outside_the_range_is_refused_and_the_answer_stays_owed`,
    `tests/test_spp_peer.py::test_a_reason_in_the_range_reaches_both_hosts`,
    `tests/test_spp_peer.py::test_a_rejection_nothing_is_owed_for_is_the_hosts_own_error`,
    `tests/test_spp_peer.py::test_a_rejection_on_a_working_link_is_the_hosts_own_error`,
    `tests/test_spp_peer.py::test_a_page_under_way_keeps_its_entry_after_a_rejection_of_a_crossing_page`,
    `tests/test_spp_peer.py::test_a_page_the_controller_refused_leaves_no_timer_and_can_be_tried_again`,
    `tests/test_spp_peer.py::test_a_page_this_side_failed_leaves_the_answer_to_a_crossing_page_its_entry`,
    `tests/test_spp_peer.py::test_a_target_that_paged_too_loses_contact_with_a_pager_that_leaves`,
    `tests/test_spp_peer.py::test_a_page_this_side_failed_does_not_mend_the_link_to_a_pager_that_lost_power`,
    `tests/test_spp_peer.py::test_a_device_that_leaves_during_its_own_page_loses_contact_with_a_page_it_heard`,
    `tests/test_spp_peer.py::test_an_answer_the_host_never_gave_does_not_spoil_a_later_page`,
    `tests/test_spp_peer.py::test_a_rejection_after_the_pager_left_and_its_page_ended_lets_the_pair_go`,
    `tests/test_spp_peer.py::test_a_page_of_this_side_that_fails_takes_its_entry_with_an_answer_never_given`,
    `tests/test_spp_peer.py::test_an_acceptance_whose_role_switch_is_refused_leaves_no_entry`,
    `tests/test_spp_peer.py::test_a_refused_role_switch_leaves_no_entry_with_an_answer_never_given`,
    `tests/test_spp_peer.py::test_a_refused_role_switch_leaves_no_entry_while_this_side_pages_too`,
    `tests/test_spp_peer.py::test_a_refused_role_switch_during_this_sides_page_with_an_answer_never_given_leaves_nothing`
- **200.** A device whose page scan is off does not hear a page: the page ends in `PAGE_TIMEOUT`
    exactly at the page timeout, and the target holds no entry for it. Page scan is read when the
    page starts, so one turned on during it answers only the next page. Inquiry scan and page scan
    are separate: a device with only inquiry scan on is found and not paged, one with only page scan
    on is paged and not found. A remote name request ends the same way, unless the two already have
    a link, or one of them heard the other's page, which it goes over without page scan; a page
    nobody heard, either way, is no link.
    `tests/test_spp_peer.py::test_a_page_to_a_device_with_page_scan_off_ends_at_the_page_timeout`,
    `tests/test_spp_peer.py::test_a_device_that_turns_page_scan_back_on_can_be_paged`,
    `tests/test_spp_peer.py::test_page_scan_turned_on_during_a_page_answers_only_the_next`,
    `tests/test_spp_peer.py::test_a_device_with_only_page_scan_on_is_paged_and_not_found`,
    `tests/test_spp_peer.py::test_a_device_with_only_inquiry_scan_on_is_found_and_not_paged`,
    `tests/test_spp_peer.py::test_a_remote_name_request_to_a_device_with_page_scan_off_ends_at_the_page_timeout`,
    `tests/test_spp_peer.py::test_a_pager_does_not_get_the_name_of_a_target_that_did_not_hear_its_page`,
    `tests/test_spp_peer.py::test_a_page_nobody_heard_carries_no_name_request_to_its_pager`,
    `tests/test_spp_peer.py::test_a_remote_name_request_over_a_link_needs_no_page_scan`,
    `tests/test_spp_peer.py::test_a_target_that_heard_the_page_gets_the_pager_s_name_without_its_page_scan`,
    `tests/test_spp_peer.py::test_a_target_that_turns_page_scan_off_after_hearing_the_page_still_gives_its_name`
- **202.** A page a device hears does not take the place of its own page to that address: a
    rejection of the page heard leaves its own page its entry, which takes the role switch of the
    other's acceptance as the central, and the link a plain acceptance makes switches roles as
    `Create_Connection` allowed. When its own page makes the link, the page heard goes with it, so
    that after the link ends the pair can page again.
    `tests/test_spp_peer.py::test_a_device_that_rejects_a_crossing_page_takes_the_role_switch_on_its_own`,
    `tests/test_spp_peer.py::test_a_link_made_after_a_rejection_of_a_crossing_page_switches_roles_as_asked`,
    `tests/test_spp_peer.py::test_a_page_under_way_keeps_its_entry_after_a_rejection_of_a_crossing_page`,
    `tests/test_spp_peer.py::test_a_page_heard_during_this_sides_own_goes_when_that_page_makes_the_link`
- **205.** Two pages at one instant that both hosts accept, plainly or as the central on either
    side, make one link, with one handle on each side and one central: the page accepted first, or
    the other when that acceptance asks for a role switch the page it accepted does not allow; none
    when both ask for the central's role and neither page allows a switch, and then both hosts hear
    `ROLE_CHANGE_NOT_ALLOWED`. A late answer tells its host of no failure on a working link or
    during its own page. A role switch asked of a pager whose page ended is refused, and no link
    comes of that acceptance; a host that accepted as the central a pager that left gets a failed
    `Connection_Complete` when its end falls, or when its own page to that pager ends, if that is
    later.
    `tests/test_spp_peer.py::test_two_pages_at_one_instant_both_accepted_make_one_link`,
    `tests/test_spp_peer.py::test_a_crossing_acceptance_whose_role_switch_would_be_refused_leaves_the_other_its_link`,
    `tests/test_spp_peer.py::test_two_crossing_acceptances_as_the_central_that_neither_page_allows_make_no_link`,
    `tests/test_spp_peer.py::test_an_answer_to_a_crossing_page_after_this_sides_own_ended_makes_the_link`,
    `tests/test_spp_peer.py::test_a_late_answer_to_a_crossing_page_tells_no_failure_on_the_link_it_made`,
    `tests/test_spp_peer.py::test_a_late_answer_leaves_a_working_link_alone`,
    `tests/test_spp_peer.py::test_a_page_tried_again_with_a_slow_host_does_not_fail_the_air`,
    `tests/test_spp_peer.py::test_an_acceptance_as_the_central_after_the_page_ended_is_refused`,
    `tests/test_spp_peer.py::test_a_host_that_accepted_as_the_central_a_pager_that_left_hears_of_it_when_its_end_falls`,
    `tests/test_spp_peer.py::test_an_acceptance_as_the_central_lost_during_this_sides_own_page_ends_with_that_page`
- **221.** A page whose host does not answer within its connection accept timeout, 5 s unless the
    host wrote another of 0x0001 to 0xB540 slots, ends at exactly that instant with
    `CONNECTION_ACCEPT_TIMEOUT_EXCEEDED` on the target's side and, when the pager's page timeout is
    the longer, as by default, on the pager's too; a pager with the shorter ends at its own page
    timeout. Both can page again: an unanswered crossing page no longer refuses this side's next
    page with `CONNECTION_ALREADY_EXISTS`. An answer after it is owed for nothing, and makes no
    link; a late answer the host never gives ends at its own timeout and takes no later page's; one
    given in time leaves no timer. A timeout written takes effect from the next page; one out of
    range is refused, and `Read_Connection_Accept_Timeout` gives the value written.
    `tests/test_spp_peer.py::test_a_page_its_host_does_not_answer_ends_on_both_sides_at_the_accept_timeout`,
    `tests/test_spp_peer.py::test_an_answer_after_the_accept_timeout_counts_for_nothing`,
    `tests/test_spp_peer.py::test_a_crossing_page_its_host_does_not_answer_ends_and_the_next_page_is_not_refused`,
    `tests/test_spp_peer.py::test_a_late_answer_the_host_never_gives_ends_at_its_accept_timeout`,
    `tests/test_spp_peer.py::test_an_answer_given_in_time_leaves_no_timer_to_end_the_link`,
    `tests/test_spp_peer.py::test_the_accept_timeout_a_host_writes_takes_effect_from_the_next_page`,
    `tests/test_spp_peer.py::test_a_page_timeout_shorter_than_the_accept_timeout_ends_the_pager_s_side_first`,
    `tests/test_spp_peer.py::test_an_accept_timeout_out_of_its_range_is_refused_and_changes_nothing`,
    `tests/test_bluetooth_guest.py::test_the_host_reads_back_the_accept_timeout_it_wrote`
- **209.** The firmware's controller reports three pages of features, 0 to 2, the fourth refused,
    page 0 with its BR/EDR features and without LE, and an ACL buffer of 491 bytes. Every packet it
    sends its host is 1 to 496 bytes; ACL data from a peer is cut to 491 bytes, and to the host's
    `Host_Buffer_Size` when that is less, and reaches the host whole.
    `tests/test_bluetooth_guest.py::test_the_host_powers_on_through_the_link_and_reads_three_pages_of_features`,
    `tests/test_bluetooth_guest.py::test_bt_spp_initiator_s_session_finds_pairs_and_opens_the_channel`,
    `tests/test_bluetooth_guest.py::test_acl_data_for_the_host_is_cut_to_its_buffer_size`,
    `tests/test_bluetooth_guest.py::test_a_packet_longer_than_the_vhci_carries_is_an_error`
- **210.** A controller answers every command: one it does not know with `UNKNOWN_HCI_COMMAND_ERROR`
  in `Command_Status` for an asynchronous command and in `Command_Complete` for any other, kept with
  its time; Bluedroid's settings that change nothing on the link succeed, and one that would filter
  events or write an inquiry access code outside the range is refused.
  `Host_Number_Of_Completed_Packets` gets no event. An inquiry is answered only by a device that
  scans for its access code. The commands about a link answer with the link, the peer's version
  included, and refuse a handle that is no link.
  `tests/test_bluetooth_guest.py::test_an_unknown_command_is_answered_in_the_event_the_host_waits_for_and_kept`,
  `tests/test_bluetooth_guest.py::test_a_setting_bluedroid_writes_succeeds`,
  `tests/test_bluetooth_guest.py::test_a_setting_that_would_change_what_is_not_modelled_is_refused`,
  `tests/test_bluetooth_guest.py::test_host_number_of_completed_packets_gets_no_event`,
  `tests/test_bluetooth_guest.py::test_an_inquiry_finds_only_devices_scanning_for_its_access_code`,
  `tests/test_bluetooth_guest.py::test_the_commands_about_a_link_are_answered_with_the_peer_s_version`,
  `tests/test_bluetooth_guest.py::test_a_command_about_no_link_is_refused_with_unknown_connection_identifier`,
  `tests/test_bluetooth_guest.py::test_the_host_reads_back_the_access_codes_it_wrote`
- **211.** `HCI_Reset`, a chip reset and the board's supply going off leave the controller holding
    nothing and its host told nothing: a page under way ends with no `Connection_Complete`, the
    answers its host owed are gone, so the next page connects, and the host's settings are back at
    their defaults. A peer's end of a link falls at its supervision timeout, and until then the
    firmware's page to it ends in `PAGE_TIMEOUT`. `board.reset()` sends none of the packets made for
    the old host in its turn.
    `tests/test_bluetooth_guest.py::test_a_reset_in_the_middle_of_a_link_tells_the_host_nothing_and_the_peer_s_end_falls`,
    `tests/test_bluetooth_guest.py::test_a_reset_in_the_middle_of_a_page_ends_it_with_no_word_and_no_timer`,
    `tests/test_bluetooth_guest.py::test_a_reset_clears_the_answers_the_host_owed_so_the_next_page_connects`,
    `tests/test_bluetooth_guest.py::test_a_reset_brings_back_the_defaults`,
    `tests/test_bluetooth_guest.py::test_the_supply_and_a_chip_reset_take_the_controller_off_the_air_and_back`,
    `tests/test_bluetooth_guest.py::test_board_reset_drops_what_the_controller_made_for_the_old_host`,
    `tests/test_bluetooth_guest.py::test_the_backend_s_supply_and_the_runner_s_resets_reach_the_controller`
- **212.** The air of the `bt` fixture is on the board's clock: only the board's link moves it, and
    `advance`, `wait` and `run` refuse. What the firmware's controller makes goes out exactly
    `LATENCY_NS` after the instant it made it, and an output due before its turn fails the link. Two
    runs cross the same packets at the same instants.
    `tests/test_bluetooth_guest.py::test_the_air_on_the_board_s_clock_moves_only_with_the_link`,
    `tests/test_bluetooth_guest.py::test_every_answer_comes_at_the_latency_and_no_turn_waits_longer`,
    `tests/test_bluetooth_guest.py::test_an_output_due_before_its_turn_fails_the_link`,
    `tests/test_bluetooth_guest.py::test_two_runs_cross_the_same_packets_at_the_same_instants`
- **213.** The SPP peer's SDP record names its service, `Serial Port` unless given another or none,
    and no device on the air names Enhanced Retransmission Mode among its L2CAP features.
    `tests/test_spp_peer.py::test_the_sdp_record_names_the_service`,
    `tests/test_spp_peer.py::test_the_peer_names_no_enhanced_retransmission_mode_among_its_l2cap_features`
- **214.** The runner gets `--bt` with the application's ELF only once the test has used the chip's
    Bluetooth; an interface never used asks for nothing and bounds no turn, and the `bt` fixture
    skips on serial. Asked for after power-on, it is refused for that, whatever the build holds.
    `tests/test_pytest_plugin.py::test_runner_command_forwards_the_guests_hci_once_the_test_uses_bluetooth`,
    `tests/test_bluetooth_guest.py::test_an_interface_the_test_never_used_asks_for_nothing_and_bounds_no_turn`,
    `tests/test_serial_backend.py::test_serial_capabilities_skip_at_the_call`,
    `tests/test_bluetooth_guest.py::test_bluetooth_asked_for_after_power_on_says_so_before_anything_else`
- **216.** ESP-IDF's `bt_spp_initiator`, unmodified, finds an `SppPeer` with `secure=True` by its name,
    pairs with its PIN, opens the channel, and what it writes reaches the peer's source as written;
    with another PIN its authentication fails and it opens nothing; with no such peer its one
    inquiry runs its 38.4 s and it pages nobody; two runs are the same.
    `tests/test_bt_spp_initiator.py::test_the_firmware_finds_pairs_with_and_streams_to_the_peer`,
    `tests/test_bt_spp_initiator.py::test_a_peer_with_another_pin_fails_the_authentication`,
    `tests/test_bt_spp_initiator.py::test_with_no_peer_the_inquiry_runs_its_length_and_opens_nothing`,
    `tests/test_determinism.py::test_spp_to_a_peer`
- **218.** A tap of the chip's Bluetooth holds every H4 packet the link carried between the
    firmware's host and its controller, both ways, at the times the link carried them, and nothing
    else; its btsnoop file holds the same to the microsecond, with its header in the sidecar, and
    Bumble reads it. A second tap, a missing directory and a file another's are refused.
    `tests/test_bluetooth_guest.py::test_a_tap_records_every_packet_both_ways_at_the_time_it_crossed`,
    `tests/test_bluetooth_guest.py::test_a_tap_is_one_per_test_needs_its_directory_and_writes_over_nothing`,
    `tests/test_bt_spp_initiator.py::test_the_tap_writes_the_hci_both_ways_as_btsnoop`,
    `tests/test_recording.py::test_bumble_reads_the_tap_as_it_writes_one`
- **219.** A device with Secure Simple Pairing pairs with one that has it by the association model
    of their IO capabilities: two that compare one number get an authenticated key, a passkey
    shown is the one to type, Just Works gives an unauthenticated key; a user's no, a wrong
    passkey or none fails the pairing on both sides, neither left waiting, and a device without it
    pairs with the PIN. Every pairing that ends is in both sides' histories, with its number, and
    two runs show the same numbers.
    `tests/test_spp_peer.py::test_two_users_compare_one_number_and_the_key_is_authenticated`,
    `tests/test_spp_peer.py::test_a_number_a_user_says_no_to_fails_the_pairing_on_both_sides`,
    `tests/test_spp_peer.py::test_the_device_types_the_passkey_the_peer_shows`,
    `tests/test_spp_peer.py::test_a_wrong_passkey_fails_the_pairing_on_both_sides_and_neither_waits`,
    `tests/test_spp_peer.py::test_two_keyboards_each_type_the_passkey`,
    `tests/test_spp_peer.py::test_a_side_without_display_or_keyboard_pairs_by_just_works_unauthenticated`,
    `tests/test_spp_peer.py::test_a_peer_with_simple_pairing_pairs_with_its_pin_with_a_device_without`,
    `tests/test_spp_peer.py::test_each_pairing_starts_anew_and_two_runs_show_the_same_numbers`
- **220.** ESP-IDF's `bt_spp_initiator` pairs by Just Works with an `SppPeer` that has no input and
    output, and streams to it; with one that shows a passkey it asks for it, and Bluedroid gives up
    35 s later; both sides' histories agree, and two runs are the same.
    `tests/test_bt_spp_initiator.py::test_a_peer_with_simple_pairing_and_no_display_pairs_by_just_works_and_streams`,
    `tests/test_bt_spp_initiator.py::test_a_peer_that_shows_a_passkey_waits_for_the_firmware_s_user_until_bluedroid_gives_up`,
    `tests/test_determinism.py::test_spp_with_simple_pairing`
- **295.** At the device's DISC for the SPP channel the peer answers UA at the DISC's instant and
    the channel is closed; it ends nothing else by itself: 60 s later the device's multiplexer and
    its L2CAP channel are still open, and the link is up on both sides.
    `tests/test_spp_peer.py::test_after_the_device_closes_the_channel_the_peer_ends_nothing_itself`

## The esp32tl command

- **38.** Ctrl-C ends a run of `esp32tl run` or `esp32tl replay` with one `SIGTERM` to the runner, which
    sends its statistics once it has connected, and the command exits 130 without a traceback,
    also before the runner has connected.
    `tests/test_cli.py::test_ctrl_c_ends_the_run_and_exits_130`,
    `tests/test_cli.py::test_ctrl_c_before_the_runner_connects_exits_130`

## Validation firmware

- **14.** `firmware/build.sh` builds in the ESP-IDF container pinned by digest, unless `IDF_LOCAL=1`.
    `tests/test_firmware_build.py::test_build_starts_the_pinned_container`
- **15.** A local build records in `esp32tl-build.json` how it was built, and a build directory made
    another way is removed before building.
    `tests/test_firmware_build.py::test_local_build_runs_idf_py_and_records_how`,
    `tests/test_firmware_build.py::test_build_replaces_a_build_directory_made_another_way`
- **16.** `esp32tl-built` marks only a build that finished: a failed build, or one without
    `flasher_args.json`, leaves none.
    `tests/test_firmware_build.py::test_failed_build_unmarks_a_finished_one`,
    `tests/test_firmware_build.py::test_build_fails_without_flasher_args`
- **64.** `sdspi` is ESP-IDF's example unmodified: its SD card pins are the `esp32-devkitc` profile's
    slot, and it formats no card.
    `tests/test_firmware_images.py::test_sdspi_takes_the_devkit_sd_card_slot_from_sdkconfig`

## Serial hardware boundary

- **98.** Serial's power-on resets once without another flash, excludes pre-reset console bytes, and
    uses wall time that advances outside waits. Console taps contain only output with clock
    `wall` and the selected artifact's app hash; they cannot overwrite the reference recording
    or flash artifacts.
    `tests/test_serial_backend.py::test_console_reset_clock_tap_and_autoflash`
- **99.** A serial reference recording must have an app hash matching the app binary, unaffected by
    bootloader changes. Missing or mismatched hashes fail, and mismatch prevents port setup even
    for an explicitly requested `dut`.
    `tests/test_serial_backend.py::test_artifact_hash_is_app_binary_and_refuses_missing_or_different_hash`,
    `tests/test_serial_backend.py::test_recording_mismatch_prevents_dut_setup`,
    `tests/test_serial_backend.py::test_reference_check_precedes_even_an_explicit_dut`
- **100.** Serial skips GNSS/modem UART models (including `modem.network()` and `broker`),
    replays and taps, SD-card models, GPIO control and
    NVS replacement. Merely requesting the modem fixture does not skip a console test. A disconnected
    console ends a wait with `RunEnded`, without a duplicate teardown failure.
    `tests/test_serial_backend.py::test_serial_capabilities_skip_at_the_call`,
    `test_serial_modem_fixture_can_accompany_console_test`,
    `tests/test_serial_backend.py::test_disconnect_ends_wait_and_releases_reader`
- **101.** Serial requires an exact port before setup; neither an absent/blank/wildcard selection nor
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
- **102.** A failed serial reset restores redirection and leaves the board off, producing no successful
    run recording; retry can start capture. A stuck reader cannot block esp32tl's close beyond
    its join bound, and neither stuck nor disconnected capture restarts another reader.
    `tests/test_serial_backend.py::test_failed_reset_restores_redirect_and_stays_off`,
    `test_stuck_reader_has_bounded_teardown_and_no_late_capture`,
    `test_dead_open_port_does_not_restart_redirect`
- **103.** A malformed `app_recording` marker names its test at collection; esp32sim does not read the
    referenced file. Console taps appear in the report, and serial logs wait starts and outcomes.
    Serial uses the same wait-step logging and duration validation messages as esp32sim.
    `tests/test_serial_backend.py::test_bad_reference_marker_names_the_test`,
    `test_serial_waits_and_console_tap_are_in_the_failure_report`,
    `test_wait_validation_and_closed_run_match_emulator`,
    `tests/test_pytest_plugin.py::test_reference_recording_is_not_read_on_esp32sim`,
    `test_console_tap_preserves_emulated_output`

## Documentation

- **17.** A change to an owned path that leaves its documentation untouched fails `make docs-check`; a
    change to tests only, or a version-only bump, does not.
    `tests/test_check_docs.py::test_an_owned_path_without_docs_fails`,
    `tests/test_check_docs.py::test_tests_only_pass`,
    `tests/test_check_docs.py::test_version_only_bumps_pass`

## Cellular modem

- **72.** Noncommands and command-mode `+++` produce no answer, including across UART chunks. Only a
    complete AT line is answered, at least 1 ms after the timestamp of its terminator-containing
    UART chunk; an escape cannot shift a registration reply by one command.
    `tests/test_modem_model.py::test_noise_and_command_mode_escape_never_leave_a_reply`,
    `tests/test_modem_model.py::test_command_framing_and_response_times_do_not_depend_on_uart_chunks`
- **73.** The modem and the test cannot both drive RI. PWRKEY uses the profile's pulse thresholds,
    and a missing supply prevents a pulse from restoring modem power. RI follows power; a power
    loss before a pending RI response cancels that response. The validation firmware leaves at
    least 2 s between the off pulse's release and the next on pulse.
    `tests/test_modem_model.py::test_model_selection_is_atomic_and_ri_has_one_driver`,
    `tests/test_modem_model.py::test_powerkey_uses_profile_timing_and_power_loss_prevents_restart`,
    `tests/test_modem_model.py::test_nominal_lilygo_pulses_work_even_when_a_tick_truncates_them`,
    `tests/test_modem_model.py::test_power_loss_before_a_scheduled_ri_response_cancels_that_response`,
    `tests/test_modem_fixture.py::test_modem_at_dialogue_and_changes_on_the_test_clock`
- **74.** Two AT-only firmware runs on the LilyGO route produce the same modem bytes at the same
    emulated times, without a network.
    `tests/test_modem_fixture.py::test_two_firmware_runs_produce_identical_modem_taps`
- **75.** Every AT command is retained with its time and reply, including unknown commands answered
    with fallback OK, independently of the rolling wire log's capacity.
    `tests/test_modem_model.py::test_a_tap_and_report_keep_commands_even_after_the_wire_log_rolls_over`
- **76.** Packet-data dial variants (`ATD*99#`, explicit context `ATD*99***1#`, `ATD*99##` and
    tone-prefixed `ATDT*99#`) enter data mode with CONNECT when registered and return NO CARRIER
    otherwise; none is a fallback OK.
    `tests/test_modem_model.py::test_packet_data_dial_variants_connect_or_report_no_carrier`
- **77.** A model's GPIO change appears in the run log only when sent, at its sent time; a cancelled
    pending change never appears as a wire event.
    `tests/test_modem_model.py::test_only_sent_model_gpio_changes_appear_in_the_run_log`
- **78.** A source selection that loses to another source must leave no GPIO ownership behind.
    `tests/test_modem_model.py::test_a_model_selection_that_loses_to_replay_does_not_claim_ri`
- **173.** `AT+CCLK?` is the world's time at the command's instant plus the test's offset, with the
    zone in quarters of an hour and `+00`, never `-00`, for none; an unset clock reads what the test
    said, runs on from it and has no zone; a zone that is no whole quarter of an hour from -24 to
    +24 hours is refused.
    `tests/test_modem_model.py::test_cclk_is_the_world_s_clock_at_the_command_s_instant`,
    `tests/test_modem_model.py::test_cclk_offsets_the_time_and_gives_the_zone_in_quarters_of_an_hour`,
    `tests/test_modem_model.py::test_clock_refuses_a_zone_that_is_no_quarter_of_an_hour_or_out_of_range`,
    `tests/test_modem_model.py::test_an_unset_clock_has_no_zone_and_runs_on_from_the_call`,
    `tests/test_modem_fixture.py::test_the_firmware_reads_a_wrong_clock_and_then_the_network_s`,
    `tests/test_modem_fixture.py::test_the_firmware_reads_an_unset_clock_with_no_zone_and_it_runs_on`
- **174.** `AT+COPS=3,<0|1|2>` chooses what `AT+COPS?` names and any other format gets `ERROR`; the
    format goes back to 0 at a power cycle, and the clock and the operator survive one.
    `tests/test_modem_model.py::test_the_operator_format_chooses_what_cops_names`,
    `tests/test_modem_model.py::test_any_other_operator_format_is_an_error_and_changes_nothing`,
    `tests/test_modem_model.py::test_the_operator_format_goes_back_at_a_power_cycle_and_clock_and_operator_stay`,
    `tests/test_modem_model.py::test_a_wrong_and_an_unset_clock_survive_a_power_cycle`,
    `tests/test_modem_fixture.py::test_the_firmware_reads_the_five_digit_code_and_the_network_s_time`
- **175.** A hung modem answers nothing and passes no data, sends no `NO CARRIER` and keeps RI high;
    its AT commands stay in the history with an empty response, shown as `(no answer)`. With
    `pwrkey=False`, PWRKEY does nothing until a restart: RESET, a loss of supply,
    `modem.power_off()` or `modem.power_on()`, which end the hang; `restore_power()` does not. A
    second `hang` is refused rather than change that.
    `tests/test_modem_model.py::test_a_hung_modem_answers_nothing_and_keeps_its_commands`,
    `tests/test_modem_model.py::test_a_hung_modem_passes_no_data_and_sends_no_no_carrier`,
    `tests/test_modem_model.py::test_the_report_shows_a_command_with_no_answer`,
    `tests/test_modem_model.py::test_pwrkey_recovers_a_hung_modem_that_honours_it`,
    `tests/test_modem_model.py::test_pwrkey_does_nothing_to_a_hung_modem_that_ignores_it`,
    `tests/test_modem_model.py::test_a_loss_of_supply_ends_the_hang_and_a_later_start_has_pwrkey`,
    `tests/test_modem_model.py::test_a_second_hang_is_refused_and_keeps_the_first_s_choice`,
    `tests/test_modem_recovery.py::test_recovers_a_hung_modem`,
    `tests/test_modem_model.py::test_the_test_s_power_calls_end_a_hang_and_restore_power_does_not`
- **176.** A RESET pulse at the profile's polarity, judged at release against its threshold, restarts
    a running modem in command mode and ends a hang; a shorter pulse, and any pulse on a modem that
    is off, do nothing. `lines.board.power_enable` has no effect on the modem. The firmware's
    recovery procedure brings a hung modem back with PWRKEY working and with it ignored.
    `tests/test_modem_model.py::test_reset_restarts_a_hung_modem_in_command_mode`,
    `tests/test_modem_model.py::test_reset_is_judged_at_release_and_at_the_profile_s_threshold`,
    `tests/test_modem_model.py::test_reset_is_active_high_at_the_pad`,
    `tests/test_modem_model.py::test_reset_takes_its_polarity_and_threshold_from_the_profile`,
    `tests/test_modem_model.py::test_reset_of_a_modem_in_data_mode_ends_the_call_and_returns_to_command_mode`,
    `tests/test_modem_model.py::test_reset_of_a_modem_that_is_off_does_nothing`,
    `tests/test_modem_model.py::test_power_enable_has_no_effect_on_the_modem`,
    `tests/test_modem_recovery.py::test_recovers_a_hung_modem`,
    `tests/test_modem_recovery.py::test_recovers_a_modem_that_hangs_while_the_firmware_dials`
- **177.** `modem.restarts` holds what ended each run of the modem, at the instant it did; starting a
    modem that is off is no restart, nor is a call before the board's first instant.
    `tests/test_modem_model.py::test_restarts_name_what_ended_each_run_and_a_start_is_no_restart`,
    `tests/test_modem_model.py::test_a_route_watches_pwrkey_and_reset_through_lines`
- **178.** `modem.drop_call` keeps registration, sends `NO CARRIER` and leaves data mode, with an LCP
    Terminate-Request first only when asked; AT works again and a new dial connects.
    `lose_registration` stays silent.
    `tests/test_modem_model.py::test_a_dropped_call_stays_registered_says_no_carrier_and_leaves_data_mode`,
    `tests/test_modem_model.py::test_a_dropped_call_offers_a_terminate_request_first_when_the_test_asks`,
    `tests/test_modem_model.py::test_a_call_dropped_without_terminate_offers_no_terminate_request`,
    `tests/test_modem_model.py::test_lose_registration_stays_silent`,
    `tests/test_net_ppp.py::test_the_far_end_offers_to_terminate_an_open_link_only`
- **179.** At the pin, firmware on `esp_modem` learns of a call dropped with a Terminate-Request from
    `IP_EVENT_PPP_LOST_IP` 6 s later, and of one without it from its own timeout, and redials on
    both, with the modem registered and not restarted. A call dropped while it is leaving data mode
    restarts nothing either, and every command is answered: until `exit_data` stops listening, 14 s
    into leaving, `+++` gets no answer and `esp_modem` writes `\n` before its `AT`; later, `+++` takes
    `NO CARRIER` as its answer and `AT` goes out alone.
    `tests/test_modem_recovery.py::test_redials_after_a_dropped_call`,
    `tests/test_modem_recovery.py::test_a_call_dropped_while_the_firmware_leaves_data_mode_restarts_nothing`
- **180.** A command stuck to bytes left on the command line is lost whole, with no answer and no
    entry in the history, on a hung modem and a healthy one; the next newline clears the line.
    `tests/test_modem_model.py::test_a_command_stuck_to_ppp_bytes_is_lost_whole`
- **276.** `modem.mode_changes` holds every dial the modem answered, into data mode on `CONNECT`
    and into command mode on `NO CARRIER`, stamped as the dial's command, and every end of data
    mode with its cause: an escape that took effect, `drop_call`, `hang`, `lose_registration` or a
    restart, at the restart's instant. An escape within the guard time, and a loss or a start in
    command mode, add none; `+++` is never in `commands`. `next_mode_change` finds the first entry
    after its call, into its mode when given, and returns the same one at every poll.
    `tests/test_modem_model.py::test_each_dial_is_a_mode_change_stamped_as_its_command_with_its_result`,
    `tests/test_modem_model.py::test_an_escape_is_a_mode_change_only_once_it_takes_effect`,
    `tests/test_modem_model.py::test_the_test_s_calls_that_end_data_mode_say_why`,
    `tests/test_modem_model.py::test_a_pwrkey_restart_ends_data_mode_at_the_restart_s_instant`,
    `tests/test_modem_model.py::test_a_reset_restart_and_a_loss_in_command_mode`,
    `tests/test_modem_model.py::test_the_next_mode_change_is_a_condition_from_this_call`

## Flash contents

- **79.** An NVS partition is selected by name from the build's own table, and its test image is flashed
    after the build's files at that partition's offset. A later call replaces the whole image.
    `tests/test_flash.py::test_nvs_flashes_at_named_partition_after_build_and_replaces_previous_call`
- **80.** NVS integers have explicit widths and checked ranges; a bare integer or boolean is refused.
    `tests/test_flash.py::test_integer_widths_check_their_range`,
    `tests/test_flash.py::test_nvs_requires_a_type_for_integers_and_refuses_other_values`
- **81.** The firmware reads exactly the NVS types and values the test supplies before power-on.
    Strings retain their UTF-8 bytes, including CRLF and lines beginning with `#`. Key and
    namespace names have no special meaning: no entries are changed or added for Wi-Fi-looking
    names, and identical inputs produce identical images.
    `tests/test_nvs_reader.py::test_firmware_reads_typed_values`,
    `tests/test_flash.py::test_real_generator_preserves_string_bytes_and_has_exact_partition_size`,
    `tests/test_flash.py::test_nvs_names_do_not_change_values_or_add_keys`
- **82.** ~~NVS cannot be supplied after power-on.~~ No longer holds since #278, which lets
    `board.nvs` write while the board is off between two boots: see 274. A passing test that
    supplies NVS without powering on fails teardown; it ran no firmware.
    `tests/test_flash.py::test_nvs_refuses_unknown_wrong_type_encrypted_and_late_calls`,
    `tests/test_pytest_plugin.py::test_nvs_without_power_on_fails_teardown`
- **83.** Binary NVS values are limited by the format and partition capacity, not by a CSV field
    limit. Blobs above 64 KiB are reconstructed unchanged from the generated chunks.
    `tests/test_flash.py::test_large_blob_fits_without_a_csv_field_limit`
- **84.** UTF-8 keys and namespaces consume their encoded byte count in fixed-size NVS headers,
    including the final slot of a page. Entry contents and CRCs remain correct, and the final
    image must always match the partition's size.
    `tests/test_flash.py::test_utf8_key_at_page_boundary_preserves_entries`,
    `tests/test_flash.py::test_utf8_namespace_at_page_boundary_preserves_entries`,
    `tests/test_flash.py::test_generator_output_must_match_partition_size`
- **273.** `board.nvs(..., merge=True)` keeps every namespace and key of the partition the test does
    not name, with its value and type, and gives the test's keys the test's values: before the first
    power-on over the build's image and the test's earlier calls, after a power-off over what the
    firmware left. Without `merge` the partition holds the test's keys alone, after a power-off too.
    `tests/test_flash.py::test_a_merge_before_power_on_keeps_the_build_s_keys_and_the_earlier_call_s`,
    `tests/test_flash.py::test_a_merge_between_boots_keeps_what_the_firmware_wrote`,
    `tests/test_flash.py::test_a_write_between_boots_without_merge_replaces_the_partition`,
    `tests/test_nvs_between_boots.py::test_a_merge_after_a_power_off_keeps_the_firmware_s_count_and_adds_the_test_s_key`,
    `tests/test_nvs_between_boots.py::test_a_write_after_a_power_off_without_merge_replaces_the_partition`,
    `tests/test_nvs_between_boots.py::test_a_merge_before_power_on_goes_over_the_test_s_earlier_keys`
- **286.** A merge never drops a value the firmware wrote without the test naming its key: a key
    NVS holds with two types refuses the merge before anything is written, unless the test gives
    it a value. On the ESP32-S31, whose esp32sim model keeps the firmware's flash writes apart until
    a reset, a dump or a write with the supply off works on them, and a write keeps the firmware's
    bytes outside its range.
    `tests/test_flash.py::test_a_merge_over_a_key_of_two_types_is_refused_unless_the_test_names_it`,
    `tests/test_flash.py::test_a_key_written_with_two_types_is_told_with_both_and_reads_as_the_later`,
    `runner/src/chip/tests.rs::the_s31_flash_has_what_the_guest_wrote_once_synced`,
    `runner/src/session/tests.rs::the_flash_is_synced_for_a_dump_or_a_write_only_with_the_supply_off_for_good`,
    `tests/test_boardlink.py::test_a_flash_dump_goes_out_after_the_board_s_switches_of_the_turn`
- **274.** `board.nvs` writes the runner's flash only while the board's supply is off, in the turn
    the test holds, and the firmware reads it at its next boot; the board's time does not move.
    While the supply is on it raises `BoardOff` naming `board.power_off()`, and the runner refuses a
    `flash_write` with the supply on as a protocol error, also in the turn the supply comes back in;
    it accepts one in the turn of the `power` that switched it off.
    `tests/test_nvs_between_boots.py::test_the_nvs_of_a_board_that_is_on_is_refused`,
    `tests/test_nvs_between_boots.py::test_a_merge_while_the_board_stays_off_changes_a_setting_between_two_boots`,
    `tests/test_flash.py::test_nvs_refuses_unknown_wrong_type_encrypted_and_late_calls`,
    `runner/src/session/tests.rs::a_flash_write_with_the_supply_off_is_in_the_flash_at_the_next_boot`,
    `runner/src/session/tests.rs::a_flash_write_with_the_supply_on_is_a_protocol_error`,
    `tests/test_boardlink.py::test_a_flash_write_goes_out_after_the_board_s_switches_of_the_turn`,
    `tests/test_clock.py::test_a_flash_write_is_answered_in_the_turn_the_link_is_held_at_and_moves_no_time`

## Request matching

- **85.** Each request cycles through its recorded occurrences independently, including silent ones;
    `hold` and `loop` apply to unprompted events and do not stop command matching.
    `tests/test_matching.py::test_answers_wrap_per_request_including_recorded_timeouts`,
    `tests/test_matching.py::test_end_policy_does_not_discard_pending_answers_or_stop_matching`,
    `tests/test_matching.py::test_loop_repeats_unprompted_events_and_hold_stops`
- **86.** Request time is the final fragment's timestamp. The minimum recorded answer delay bounds
    latency, and every pending answer, including unknown-command fallbacks, bounds quiet_until.
    `tests/test_matching.py::test_request_time_is_its_final_fragment_and_chunk_boundaries_do_not_matter`,
    `tests/test_matching.py::test_quiet_until_includes_stream_unknown_and_every_deferred_answer`
- **87.** An unknown request is counted and listed; its default PMTK reply uses flag 1 and its UBX NAK
    identifies the request's class and ID. Protocol framing works across UART chunks.
    `tests/test_matching.py::test_pmtk_fallback_has_the_command_flag_one_and_checksum`,
    `tests/test_matching.py::test_ubx_is_length_framed_even_with_terminators_in_payload_and_naks_class_id`,
    `tests/test_replay.py::test_unseen_requests_are_in_the_backend_report_and_json_properties`,
    `tests/test_replay.py::test_gnss_replay_answers_configuration_commands_through_the_link`

- **88.** AT prompt recognition does not depend on response chunk boundaries. Command CR/CRLF
    separators, including a split CRLF before the response, never become prompted payload
    bytes; actual leading payload whitespace is preserved.
    `tests/test_matching.py::test_prompt_like_text_does_not_depend_on_response_fragmentation`,
    `tests/test_matching.py::test_at_command_endings_do_not_change_prompted_payload_bytes`,
    `tests/test_matching.py::test_at_prompt_switches_next_request_to_opaque_ctrl_z_payload`

- **89.** Every unknown replay request retains its completion time and raw frame in arrival order,
    including repeated normalised keys and silent fallbacks. Known requests are excluded. The
    text and JSON reports preserve the full history and bytes after the wire log rolls over;
    the existing normalised counts remain available.
    `tests/test_matching.py::test_unseen_history_retains_raw_occurrences_and_completion_times_without_a_reply`,
    `tests/test_replay.py::test_unseen_requests_are_in_the_backend_report_and_json_properties`


## Simulated network

- **183.** A stated RTT is exact: what the device sends at `t` reaches the network at `t` plus the delay up and is answered at `t + rtt`, the two
    delays adding up to it; a rate adds each datagram's size in bits at that rate, one datagram after another; an RTT below 1 ms is refused. The default is
    the network without a path. LCP, IPCP, ARP and DHCP are not delayed.
    `tests/test_net_path.py::test_a_stated_rtt_is_exact_from_the_request_to_the_answers_stamp`,
    `tests/test_net_path.py::test_the_two_directions_add_up_to_the_rtt`,
    `tests/test_net_path.py::test_by_default_the_answer_is_stamped_1_ms_after_the_request_in_its_turn`,
    `tests/test_net_path.py::test_the_rate_takes_its_time_each_way_from_the_size_of_the_datagram`,
    `tests/test_net_path.py::test_an_rtt_below_the_floor_is_refused`,
    `tests/test_net_path.py::test_ppp_control_is_not_delayed`,
    `tests/test_net_path.py::test_arp_and_dhcp_are_not_delayed`
- **184.** No datagram on the path overtakes another, when the path changes either; `quiet_until` holds the runner at the head of each queue, and a
    reset or the end of the link drops what is on the path.
    `tests/test_net_path.py::test_a_smaller_rtt_applies_to_what_enters_after_it_and_overtakes_nothing_going_up`,
    `tests/test_net_path.py::test_a_smaller_rtt_overtakes_nothing_going_down`,
    `tests/test_net_path.py::test_the_network_never_stamps_an_answer_before_the_turn_that_hands_it_over`,
    `tests/test_net_path.py::test_a_reset_drops_what_is_on_the_path_and_keeps_the_path`,
    `tests/test_net_path.py::test_the_end_of_the_ppp_link_drops_what_is_on_the_path`
- **186.** What has come up the path by a turn is taken in before the test holds that turn: a record is there when `board.now` reaches its stamp, and
    what the test does at that instant comes after it.
    `tests/test_boardlink.py::test_arrivals_come_before_the_hold_and_the_poll_after_it`,
    `tests/test_clock.py::test_a_wait_that_ends_at_a_datagrams_time_finds_it_taken`,
    `tests/test_clock.py::test_a_wait_until_returns_at_the_datagrams_time`,
    `tests/test_mqtt_session.py::test_a_record_is_there_when_the_board_says_its_time`
- **199.** The broker's deadlines run before the test holds the turn at their time: at the instant a delayed PUBACK is due it is emitted, at
    the instant a keepalive expires the connection is closed and its Will published, and what the test does at that instant comes after them.
    Expiry still wins a tie with a PUBACK deadline.
    `tests/test_clock.py::test_a_wait_that_ends_at_a_puback_deadline_finds_it_emitted`,
    `tests/test_clock.py::test_a_wait_until_returns_at_a_puback_deadline`,
    `tests/test_clock.py::test_a_wait_that_ends_at_keepalive_expiry_finds_the_connection_closed_and_the_will`,
    `tests/test_clock.py::test_a_wait_until_returns_at_keepalive_expiry`,
    `tests/test_clock.py::test_keepalive_expiry_wins_a_tie_with_a_puback_deadline_in_a_held_turn`
- **187.** A rate counts the IPv4 datagram alone, not what follows it in its Ethernet frame, and a
    frame without anything after its datagram gives the same datagram.
    `tests/test_net_path.py::test_what_follows_a_datagram_in_its_frame_takes_no_time_at_the_rate`,
    `tests/test_net_ethernet.py::test_what_follows_an_ipv4_datagram_in_its_frame_is_not_part_of_it`
- **198.** The frame of a 1500-byte datagram from the station, with the 4 bytes of libpp's guard
    word after it, is an `eth_tx` of 1518 bytes: the codec takes it, refuses one of 1519, and
    `Ethernet.receive` gives the datagram alone.
    `tests/test_net_ethernet.py::test_a_full_size_datagram_and_the_guard_word_are_an_eth_tx_of_1518_bytes`
- **188.** A resumed session stays connected while its queue waits for packet IDs: each PUBACK sends the next message, and a QoS 1 message published
    meanwhile goes behind the queue.
    `tests/test_net_mqtt_sessions.py::test_a_resumed_queue_waits_for_packet_ids_and_each_puback_frees_one`
- **201.** A connected client with no free packet ID is not disconnected: a QoS 1 message for it, a retained one that a SUBSCRIBE
    matched included, waits in its session's queue, and each PUBACK sends the next; one that a SUBSCRIBE matched goes with RETAIN 1, the
    rest with RETAIN 0. A SUBSCRIBE again while one waits does not queue it twice. CleanSession=1 loses the queue with the connection;
    CleanSession=0 keeps it for the next.
    `tests/test_net_mqtt.py::test_qos_one_with_no_free_id_waits_for_a_puback_instead_of_reusing_one`,
    `tests/test_net_mqtt_sessions.py::test_a_connected_client_with_no_free_id_keeps_the_message_and_its_connection`,
    `tests/test_net_mqtt_sessions.py::test_a_retained_message_waiting_after_a_subscribe_keeps_retain_one`,
    `tests/test_net_mqtt_sessions.py::test_a_subscribe_on_every_connection_while_waiting_queues_its_retained_message_once`
- **185.** A QoS 1 backlog from esp-mqtt, one message at a time, takes about three times as long at an RTT of 600 ms as at 200 ms, each exchange
    spaced by the RTT and some 15 ms of UART and firmware.
    `tests/test_mqtt_session.py::test_a_qos1_backlog_follows_the_rtt`
- **181.** The broker keeps a session by client ID when CleanSession is 0: a resumed CONNECT says Session Present 1 and sends
    what is unacknowledged with DUP and the original packet IDs, in the order first sent, then the queue in order, with DUP 0;
    an offline client's QoS 1 messages are queued and its QoS 0 dropped. CleanSession=1 discards the state at CONNECT and at
    disconnection, and says Session Present 0, as does a refused CONNECT, which leaves a stored session alone.
    `tests/test_net_mqtt_sessions.py::test_first_clean_session_zero_has_no_session_and_the_second_does`,
    `tests/test_net_mqtt_sessions.py::test_offline_qos1_is_queued_in_order_and_qos0_is_dropped`,
    `tests/test_net_mqtt_sessions.py::test_unacknowledged_go_again_with_dup_and_their_ids_before_the_queue`,
    `tests/test_net_mqtt_sessions.py::test_clean_session_discards_a_stored_session_at_connect`,
    `tests/test_net_mqtt_sessions.py::test_clean_session_discards_its_state_at_disconnection`,
    `tests/test_net_mqtt_sessions.py::test_a_refused_connect_says_no_session_and_touches_none`
- **182.** esp-mqtt with CleanSession=0 sees Session Present, receives a QoS 1 message published while it was offline, and one
    redelivered with DUP after `broker.disconnect` in the same instant as its publication, whose first PUBACK counts for nothing.
    `tests/test_mqtt_session.py::test_first_connection_finds_no_session_and_the_next_one_does`,
    `tests/test_mqtt_session.py::test_receives_a_qos1_message_published_while_it_was_offline`,
    `tests/test_mqtt_session.py::test_a_message_whose_puback_is_lost_comes_again_with_dup`
- **90.** PPP delivers IPv4 only after both sides acknowledge LCP and IPCP; acknowledgments must match
    the pending request, including after termination and renegotiation. Framing survives arbitrary byte splits, escaping and compressed headers.
    `tests/test_net_ppp.py::test_ack_must_match_request_and_ipcp_waits_for_lcp`,
    `tests/test_net_ppp.py::test_fcs_reference_and_compressed_frames_at_every_split`,
    `tests/test_net_ppp.py::test_renegotiation_and_termination_invalidate_old_acknowledgments`
- **91.** TCP honors the peer's receive window and MSS, delivers a retransmitted payload once, wraps
    sequence numbers, and notifies connection loss once. For the connections the test opens, see
    225 and 226.
    `tests/test_net_ip.py::test_tcp_handshake_data_duplicate_flow_control_fin_and_wrap`,
    `tests/test_net_ip.py::test_reset_notifies_once_and_unbound_port_refuses`
- **92.** MQTT Will is emitted once on connection loss at the loss time, never after DISCONNECT.
    Keepalive deadlines use the board clock, and wildcard filters respect `$` topics.
    `tests/test_net_mqtt.py::test_will_on_transport_loss_once_and_never_on_disconnect`,
    `tests/test_net_mqtt.py::test_keepalive_expires_on_the_board_clock`,
    `tests/test_net_mqtt.py::test_topic_filters`
- **93.** NTP returns world UTC plus emulated time; TLS verifies the per-test CA and named server and
    exposes application plaintext in its transcript. The server completes TLS 1.2 over each suite
    and group it lists and refuses, with an alert and a logged reason, a ClientHello it cannot
    serve and a record that does not decrypt.
    `tests/test_net_services.py::test_ntp_world_time_fraction_origin_and_era`,
    `tests/test_net_tls.py::test_tls_mqtt_and_transcript`,
    `tests/test_net_tls.py::test_tls_rejects_unknown_ca_or_wrong_hostname`,
    `tests/test_net_tls.py::test_every_suite_and_group_carries_data_both_ways`,
    `tests/test_net_tls.py::test_a_client_without_tls_1_2_is_refused_with_an_alert`,
    `tests/test_net_tls.py::test_a_client_hello_the_server_cannot_serve_is_refused`,
    `tests/test_net_tls.py::test_a_record_that_does_not_decrypt_closes_the_connection`,
    `tests/test_net_tls.py::test_a_maximum_fragment_length_limits_the_servers_records`
- **94.** Only a modem with a simulated network treats receive overflow as fatal; binary pluses do not
    escape data mode without idle guards. Losing registration or supply resets the network.
    `tests/test_modem_model.py::test_only_a_network_route_fails_on_receive_overflow`,
    `tests/test_modem_model.py::test_network_selection_guarded_escape_and_loss_reset`
- **95.** The harness's TLS server draws nothing from the system: after the CAs are numbered from 0
    again, the same ClientHello gets the same bytes, and another connection or CA other bytes; a
    CA still held from an earlier test keeps its number, and a new CA never gets its keys. So
    the determinism check compares a network transcript exactly, times after a handshake included,
    and two runs of the position tracker over TLS leave the same transcript, tap and console.
    `tests/test_net_tls.py::test_the_server_sends_the_same_bytes_in_every_run`,
    `tests/test_net_tls.py::test_a_ca_that_outlives_its_test_keeps_its_number_and_its_keys_to_itself`,
    `tests/test_check_determinism.py::test_a_network_transcript_compares_exactly_after_a_tls_handshake_too`,
    `tests/test_determinism.py::test_tls_on_the_device` (under `make determinism`)

- **96.** Anonymous MQTT clients have distinct deterministic IDs before Will construction or takeover;
    they do not disconnect each other or collide with IDs already supplied by named clients.
    Empty IDs without CleanSession receive CONNACK code 2 and close.
    `tests/test_net_mqtt.py::test_anonymous_clients_have_independent_ids_and_wills`,
    `tests/test_net_mqtt.py::test_anonymous_id_skips_an_id_already_supplied_by_a_client`,
    `tests/test_net_mqtt.py::test_empty_id_without_clean_session_gets_identifier_rejected`
- **97.** Acknowledged PPP MRU bounds every outgoing IPv4 packet. TCP preserves payload bytes while
    respecting MRU, MSS and the receive window; oversized UDP replies are logged and dropped.
    Renegotiation and redial cannot inherit the previous MRU, and unsupported small values are
    NAKed rather than acknowledged.
    `tests/test_net_mru.py::test_tcp_respects_mru_mss_and_window_without_losing_bytes`,
    `tests/test_net_mru.py::test_udp_sends_at_mru_and_logs_oversized_replies`,
    `tests/test_net_mru.py::test_mru_resets_on_renegotiation_termination_and_redial`,
    `tests/test_net_mru.py::test_mru_too_small_for_this_stack_gets_a_nak_not_an_ack`,
    `tests/test_net_mru.py::test_ppp_does_not_emit_an_oversized_ip_packet_even_when_called_directly`
- **121.** Behind the access point ARP never answers for the device's own address, so a lease is never
     declined, and answers every other address with the network's MAC; DHCP acknowledges only the
     device's one address and NAKs another, and answers no datagram the IP layer would drop.
     `tests/test_net_ethernet.py::test_arp_answers_every_address_but_the_devices_own`,
     `tests/test_net_ethernet.py::test_dhcp_naks_another_address_and_ignores_another_server`,
     `tests/test_net_ethernet.py::test_dhcp_checks_the_datagram_as_the_ip_layer_does`
- **122.** ESP-IDF's unmodified `http_request` example joins the test's access point with ESP-IDF's
     default SSID and passphrase, gets its address by DHCP and fetches from the test's HTTP server;
     after the access point leaves and comes back it joins again and fetches again; with a wrong
     passphrase it gets no address.
     `tests/test_http_request.py::test_joins_and_fetches_from_the_tests_server`,
     `tests/test_http_request.py::test_rejoins_once_the_access_point_is_back`,
     `tests/test_http_request.py::test_a_wrong_password_never_gets_an_address`


- **104.** The GPS alarm drives its buzzer only while a fresh valid fix is within the NVS-configured
    radius. Moving across the radius changes its output within two fix intervals; no fix,
    loss of fix, receiver silence or missing configuration cannot leave it on. Both destination
    and radius come from typed NVS values, including southern/western destinations. After silence
    immediately following the first observed on state, the buzzer stays on for 2.3 s and turns
    off by 2.7 s (the 2.5 s fix lifetime with observation/UART tolerance). Off-from-boot checks
    permit unknown only before initialization; subsequent releases and active pulses fail.
    `examples/gps_alarm_clock/tests/test_alarm.py`, `tests/test_alarm_example.py`


- **105.** Named GPIO output conditions observe only what the firmware drives. Neither on nor off
    matches an undriven output, for either polarity, and constructing a handle queues no input.
    Saved conditions read the live state; waits report the line, desired state, pin and level.
    `tests/test_lines.py::test_named_output_conditions_follow_the_firmware_and_exclude_undriven`,
    `tests/test_lines.py::test_output_handles_resolve_pin_names_and_filter_events_without_driving`,
    `tests/test_gpio_echo.py::test_named_output_waits_report_the_line_and_state`

- **106.** GPIO interval assertions require a known matching state at the starting instant and every
    subsequent firmware transition through now, catching pulses even after the output recovers.
    Input writes cannot satisfy an output assertion; none of these assertions advance time.
    `tests/test_lines.py::test_output_history_catches_short_pulses_even_after_state_recovers`,
    `tests/test_lines.py::test_output_history_requires_a_known_start_and_rejects_released_outputs`,
    `tests/test_lines.py::test_output_history_uses_start_state_and_includes_the_current_instant`,
    `tests/test_lines.py::test_output_history_ignores_inputs_other_pins_and_future_events`

- **107.** The SD logger appends exactly one row per valid dated RMC, preserving existing rows and
    using a separate file for each GNSS UTC date, including New Year and leap day. GGA and
    invalid or missing fixes add no rows. Without a card it keeps processing new GNSS input
    and continuously asserts the storage-error pin after initialization fails.
    `examples/sd_data_logger/tests/test_logger.py`


- **108.** Board interface properties and their fixture shortcuts share objects and state. Accessing
    console, GNSS, modem, lines or SD interfaces neither selects a UART source nor powers on;
    explicit `board.modem.network().broker` and the broker fixture select the same modem network.
    `tests/test_board_api.py::test_fixture_aliases_share_interfaces_without_powering_on`,
    `tests/test_board_api.py::test_modem_alias_does_not_select_a_source`,
    `tests/test_board_api.py::test_broker_alias_uses_the_same_modem_network`

- **109.** ~~Building an SD image does not insert it; each build has independent contents. Binary bytes
    and text line endings survive FAT preparation and reading. Board close removes all owned
    images and invalidates readers/builders, while external images survive. A saved builder
    cannot build after power-on, and a failed build leaves no partial image.~~ No longer holds
    since #106, which lets a test build a card to put in while the firmware runs: see 146.
- **146.** Building an SD image does not insert it; each build has independent contents, also one
    made after power-on, for a card to put in while the firmware runs. Binary bytes and text line
    endings survive FAT preparation and reading. Board close removes all owned images and
    invalidates readers/builders, while external images survive, and a failed build leaves no
    partial image.
    `tests/test_card_image.py`,
    `tests/test_board_api.py::test_a_build_after_power_on_is_a_replacement_and_close_removes_images`


- **195.** The builder makes cards of 1 to 1048576 MiB, and clusters of 1 to 64 KiB in powers of two, as
    asked: `-c` reaches the boot sector, on a card too of the size where mformat would choose that
    cluster by itself. From 512 MiB a card is FAT32, with 255 heads and 63 sectors, and a count of
    clusters outside 65525 to 268435445 is refused at `image()`, agreeing with what mformat makes on
    either side of the limit. A 32 GiB card with 32 KiB clusters mounts in ESP-IDF's unmodified
    `sdspi`, and takes what its FATs and files take of the disk.
    `tests/test_card_image.py::test_a_size_the_builder_does_not_make_is_refused`,
    `tests/test_card_image.py::test_a_cluster_mformat_does_not_make_is_refused`,
    `tests/test_card_image.py::test_a_fat32_card_with_a_count_of_clusters_it_cannot_have_is_refused`,
    `tests/test_card_image.py::test_a_32_gib_card_has_the_cluster_asked_for_and_stays_sparse`,
    `tests/test_card_image.py::test_the_builder_makes_a_card_of_1_tib`,
    `tests/test_card_image.py::test_the_smallest_card_a_cluster_is_allowed_has_the_clusters_fat32_asks_for`,
    `tests/test_sdspi.py::test_the_example_mounts_a_32_gib_card_with_32_kib_clusters`
- **289.** A card built again, by another slot, later on the host's clock and in another time
    zone, is the same bytes, FAT16 and FAT32 alike: its files, its directories and their `.` and
    `..` dated 1980-01-01 00:00, and the n-th card a slot builds has the volume serial number
    `E32D` and n in four hex digits, so that the first two differ only there.
    `tests/test_card_image.py::test_a_card_built_again_is_the_same_bytes_and_a_second_card_another_serial`
- **196.** What a test takes from `card_cache` is its own: whether the file system clones or the harness
    copies, what is written to a clone reaches neither the cache nor another clone, nor does a later
    write to the image a card was kept from. The clone is writable, sparse where its source is, and gone
    with the board. A card is kept for a name and an application: another of either is a miss. A
    second test finds what the first prepared, without the first's boots, and what the firmware then
    writes stays on its clone.
    `tests/test_card_cache.py::test_what_is_written_reaches_neither_the_cache_nor_another_clone`,
    `tests/test_card_cache.py::test_a_hit_is_a_clone_in_the_test_s_directory_gone_with_its_board`,
    `tests/test_card_cache.py::test_a_clone_of_a_read_only_image_can_be_written`,
    `tests/test_card_cache.py::test_a_card_belongs_to_its_name_and_its_application`,
    `tests/test_card_cache.py::test_the_sparse_copy_leaves_holes_and_reads_the_same`,
    `tests/test_card_cache.py::test_a_session_keeps_a_card_for_the_next_test_and_the_next_session`,
    `tests/test_power.py::test_a_second_test_takes_a_kept_card_and_writes_to_its_own_clone`
- **197.** The cards `card_cache` keeps stay within `esp32tl_card_cache_mib`, counted from what they
    have allocated: the card least recently used goes first, and `get` counts as use. A card alone above
    the limit is refused and evicts nothing. A card appears under its name whole, a failed `put` leaves
    no part of one and fails with the card's name, and what a run that died left is removed. A card in
    the slot of a board that is on is not kept.
    `tests/test_card_cache.py::test_the_card_least_recently_used_goes_first`,
    `tests/test_card_cache.py::test_disk_use_stays_within_the_limit_from_what_is_allocated`,
    `tests/test_card_cache.py::test_the_limit_counts_what_a_card_has_allocated_once_it_is_synced`,
    `tests/test_card_cache.py::test_a_card_above_the_limit_is_refused_and_evicts_nothing`,
    `tests/test_card_cache.py::test_a_put_that_fails_leaves_no_part_of_a_card`,
    `tests/test_card_cache.py::test_a_cache_directory_that_fails_fails_with_the_card_s_name`,
    `tests/test_card_cache.py::test_a_card_appears_whole_under_its_name_and_a_dead_run_s_part_is_removed`,
    `tests/test_card_cache.py::test_a_card_in_the_slot_of_a_board_that_is_on_is_not_kept`,
    `tests/test_card_cache.py::test_a_limit_that_is_no_whole_number_is_a_usage_error`

- **110.** FAT paths never expand into multiple entries: readers and builders reject bracket patterns,
    even when a matching literal filename exists. Missing paths, file-as-directory paths and
    directory-as-file reads raise the corresponding standard filesystem exceptions. An invalid
    FAT filesystem remains a tool error, not a missing file.
    `tests/test_card_image.py::test_bracket_patterns_never_read_other_files`,
    `tests/test_card_image.py::test_paths_cannot_escape_or_expand_as_mtools_patterns`,
    `tests/test_card_image.py::test_readers_distinguish_missing_files_and_directories`,
    `tests/test_card_image.py::test_invalid_fat_remains_a_tool_error`

- **111.** The position tracker publishes over verified MQTT/TLS immediately on connection and every
    10 seconds on the test clock. Reported positions follow GNSS, including southern/western
    coordinates, and Unix UTC follows NTP's world epoch, including dates after 2038. No fix,
    loss of fix or receiver silence produces null coordinates rather than a stale current fix.
    A wrong CA or hostname, or a server certificate outside its validity period at the NTP
    time, leaves the broker's cumulative history empty through 30 seconds after the first TLS
    input. The date tests keep the CA valid and include an accepted server
    certificate valid at that world epoch. Missing NVS configuration never connects. During
    NTP silence, no broker lookup, TLS attempt, MQTT connection or publication occurs in the
    30 seconds after the time-server lookup. Resuming NTP yields a report with valid GNSS
    position and current world UTC within 60 seconds.
    `examples/position_tracker/tests/test_tracker.py`

- **112.** NTP silence suppresses new replies without incrementing the reply count. Resume answers only
    new requests with current world UTC; transitions are idempotent and do not replay a backlog.
    `tests/test_net_services.py::test_ntp_silence_and_resume_preserve_time_and_reply_count`,
    `tests/test_net_services.py::test_ntp_can_be_silent_before_its_first_request`

- **113.** Server certificate validity can change independently of the signing CA. Validity inputs are
    aware datetimes normalized to UTC, with the end strictly after the start. The CA keeps its
    default dates and signs the generated leaf.
    `tests/test_net_tls.py::test_server_dates_change_without_changing_ca_validity`,
    `tests/test_net_tls.py::test_server_dates_require_a_timezone`,
    `tests/test_net_tls.py::test_server_dates_require_a_nonempty_forward_interval`

- **114.** Tracker report conditions observe future firmware publications only on the tracker topic,
    ignoring unrelated messages and test publications. Position waits skip valid no-fix reports
    but reject malformed ones. Repeated polling returns the first match; later invalid messages
    cannot hide it, and the complete report history still detects malformed publications.
    Wait timeouts and run reports distinguish a tracker report from one requiring a position.
    Boolean coercion of a report condition raises before and after a match; explicit calls return
    the observed report or None.
    `examples/position_tracker/tests/test_tracker_support.py`

- **115.** A TLS listener counts attempts once per connection on its first nonempty input, including
    malformed input. Empty input, an idle TCP connection, later fragments, application traffic
    and input after closure do not add attempts. Counts survive closure and new connections.
    Completed handshakes are counted once after server-side success, not on partial or failed
    input. Saved cumulative TLS conditions observe these live counts, retain history after
    closure/reset, reject boolean coercion and name the desired event in board timeouts.
    `tests/test_net_tls.py::test_tls_attempts_count_nonempty_input_once_per_connection`,
    `tests/test_net_tls.py::test_tls_mqtt_and_transcript`,
    `tests/test_net_tls.py::test_tls_rejects_unknown_ca_or_wrong_hostname`,
    `tests/test_net_tls.py::test_tls_conditions_are_live_named_and_reject_truth_testing`,
    `examples/position_tracker/tests/test_tracker_support.py::test_tls_timeout_names_the_expected_handshake`

- **124.** MQTT CONNECT policy uses only named MQTT 3.1.1 return codes and runs on fully parsed
    public metadata before session state is installed. Rejection closes after CONNACK, never takes
    over an accepted client, processes pipelined publications, subscribes or emits a Will.
    Attempts record the selected code and board time separately from accepted clients, and both
    histories and the policy survive a network reset. Retries can succeed after credentials or
    policy change over plain TCP and TLS; `None` restores credential observation without
    authentication. Empty IDs without CleanSession are rejected before policy, while accepted
    anonymous clients receive IDs only after policy evaluation. Policy errors produce no CONNACK
    or outcome: they leave `on_data` of a directly used broker, and fail the link of a board run.
    `tests/test_net_mqtt_policy.py::test_connack_codes_and_rejection_isolation`,
    `tests/test_net_mqtt_policy.py::test_retries_credentials_policy_changes_and_success`,
    `tests/test_net_mqtt_policy.py::test_rejected_takeover_keeps_old_client_and_will`,
    `tests/test_net_mqtt_policy.py::test_empty_id_rejection_precedes_policy_and_has_an_outcome`,
    `tests/test_net_mqtt_policy.py::test_internet_reset_keeps_the_policy_and_both_histories`,
    `tests/test_net_mqtt_policy.py::test_policy_errors_in_a_board_run_fail_the_link_naming_them`

- **125.** MQTT next-event conditions capture append order at creation, exclude earlier events even
    at equal timestamps, and repeatedly return the first matching record without consuming it. So
    does `next_connection_attempt`. History queries return the first matching recorded event even
    after closure. A pre-armed publication condition sees a response delivered in the same slice as
    CONNECT; a condition made after that slice does not. Subscription conditions read current state
    and do not latch previous subscriptions. All MQTT conditions name the expected event and reject
    boolean coercion before and after a match.
    `tests/test_net_mqtt.py::test_history_and_next_events_use_append_order_without_consuming`,
    `tests/test_net_mqtt.py::test_subscription_condition_tracks_current_state_without_latching`,
    `tests/test_net_mqtt.py::test_mqtt_conditions_are_named_and_reject_truth_testing`,
    `tests/test_clock.py::test_mqtt_immediate_response_is_visible_after_waiting_for_connection`,
    `examples/position_tracker/tests/test_tracker_support.py::test_mqtt_timeout_names_the_expected_connection`
- **251.** `broker.subscribe_requests` keeps one `SubscribeRequest` per SUBSCRIBE and
    UNSUBSCRIBE a connected client sent, in arrival order, with its time, client, connection,
    packet ID and each filter's requested and granted QoS (`None` for UNSUBSCRIBE); an
    UNSUBSCRIBE and a CleanSession=1 CONNECT remove nothing from it. A malformed SUBSCRIBE or
    UNSUBSCRIBE is refused whole: no entry, and no subscription added or removed.
    `next_subscribe(filter=None)` returns the first SUBSCRIBE appended after the call, naming that
    filter exactly when one is given, never an UNSUBSCRIBE, without consuming it, and names its
    condition.
    `tests/test_net_mqtt.py::test_subscribe_requests_keep_every_filter_after_unsubscribe_and_clean_session`,
    `tests/test_net_mqtt.py::test_next_subscribe_sees_a_later_subscribe_to_the_filter_without_consuming`,
    `tests/test_net_mqtt.py::test_a_malformed_subscribe_or_unsubscribe_is_refused_whole`,
    `tests/test_net_mqtt.py::test_mqtt_conditions_are_named_and_reject_truth_testing`
- **258.** The end of the device's link (power off, coverage lost, a dropped or hung call, the
    access point off the air) loses the connections the test opened and leaves the broker's end of
    each the device opened open and silent: the broker's deadlines run with the link down, and the
    keepalive expiry publishes the Will, when the client set one, 1.5 times the keepalive after the
    device's last packet, not at the loss. A client back with the same ID before then, connecting
    from another source port, takes the connection over (MQTT-3.1.4-2); with CleanSession=0 on both
    connections the old Will is discarded unsent, and with CleanSession=1 on either it is published
    at the takeover, as it is when the CONNECT comes at the instant the old keepalive runs out; a
    CONNECT after the expiry finds the old connection closed and its Will published by the expiry. A
    SYN from the old source port, its options parsed, resets the old connection, its Will going
    then, and opens a new one. A device connection the link's end, a chip reset or a DHCP lease
    given up left open is dropped, with no FIN, when its service closes it, and so are, at once, one
    its service had closed and one still in its handshake, and what waited for its window is
    discarded; after a chip reset behind the modem or the access point, or a lease given up, the
    gone stack's SYNs still on the path never arrive, and its data does, where the link's end, and a
    reset behind a virtual station, clear the path; what its service writes beyond the window is
    discarded and counted. A virtual station leaving the device's access point drops the device's
    connections still in their handshake, ends those with bytes or a FIN not acknowledged and those
    whose FIN was acknowledged with no FIN back, and leaves the rest as they were: what their
    service writes or closes meanwhile reaches the device in order once the station is back with the
    same address, and a station back with another loses them. A stale close of an ended connection
    leaves the new one on its port alone. While the link is down, a bridged connection keeps its
    millisecond turns. A keepalive of 0 sets no deadline.
    `tests/test_net_ip.py::test_modem_link_loss_leaves_tcp_open_and_the_will_to_the_keepalive`,
    `tests/test_net_mqtt_sessions.py::test_a_lost_link_publishes_the_will_when_the_keepalive_runs_out`,
    `tests/test_net_mqtt_sessions.py::test_a_device_back_before_its_keepalive_takes_over_its_connection`,
    `tests/test_net_mqtt_sessions.py::test_a_stored_session_outlives_a_lost_link_and_its_keepalive`,
    `tests/test_net_mqtt_sessions.py::test_a_new_stack_connects_from_the_old_port_once_the_keepalive_closed_it`,
    `tests/test_net_mqtt_sessions.py::test_the_links_end_drops_a_connection_its_service_has_closed`,
    `tests/test_net_mqtt_sessions.py::test_a_keepalive_of_zero_keeps_a_lost_links_connection_and_its_will`,
    `tests/test_net_ethernet.py::test_a_keepalive_that_runs_out_before_the_device_is_back_drops_the_connection`,
    `tests/test_net_mqtt_sessions.py::test_a_takeover_at_the_old_keepalives_deadline_publishes_its_will`,
    `tests/test_net_mqtt_sessions.py::test_the_links_end_drops_a_connection_still_in_its_handshake`,
    `tests/test_net_mqtt_sessions.py::test_a_stale_close_leaves_the_new_connection_on_its_port_alone`,
    `tests/test_net_client.py::test_a_chip_reset_drops_a_device_connection_still_in_its_handshake`,
    `tests/test_net_client.py::test_a_released_lease_orphans_the_devices_connections_on_its_address`,
    `tests/test_net_station.py::test_a_station_that_leaves_and_rejoins_leaves_the_devices_connection_to_its_end`,
    `tests/test_net_station.py::test_what_a_service_writes_while_the_station_is_away_waits_for_it`,
    `tests/test_net_client.py::test_an_unsupported_dhcp_message_leaves_the_address_and_the_connections`,
    `tests/test_net_client.py::test_what_an_orphaned_connection_cannot_take_is_discarded_and_counted`,
    `tests/test_net_client.py::test_the_gone_stacks_syn_on_the_path_never_arrives_but_its_data_does`,
    `tests/test_net_mqtt_sessions.py::test_a_new_stacks_syn_from_the_old_port_resets_the_orphaned_connection`,
    `tests/test_net_station.py::test_a_station_back_with_another_address_loses_the_connections_on_the_old_one`,
    `tests/test_net_station.py::test_a_station_that_leaves_with_bytes_unacknowledged_ends_the_connection`,
    `tests/test_net_client.py::test_a_malformed_datagram_on_the_path_survives_a_reset`,
    `tests/test_net_client.py::test_a_malformed_syn_leaves_the_orphaned_connection_and_its_will`,
    `tests/test_net_station.py::test_a_station_that_leaves_after_the_services_fin_was_acknowledged_retires_it`,
    `tests/test_net_bridge.py::test_a_lost_link_leaves_the_stream_at_the_service_open_until_the_run_ends`,
    `tests/test_modem_model.py::test_the_keepalive_runs_out_with_the_call_ended`,
    `tests/test_mqtt_session.py::test_a_lost_link_publishes_the_will_at_the_keepalive_not_at_the_loss`,
    `tests/test_mqtt_session.py::test_a_device_back_before_its_keepalive_takes_over_its_session_and_no_will_goes`
- **223.** `broker.find_messages(since=broker.mark())` returns every matching message appended
    after the mark, in append order, those at the mark's board time included and those appended
    before it excluded; `client=` keeps one client ID's messages, and the test's publications carry
    `TEST_CLIENT`. Reading consumes nothing; a mark from another broker, and a `since` that is not
    a mark, are refused.
    `tests/test_net_mqtt.py::test_find_messages_reads_every_match_after_a_mark_by_topic_payload_and_client`,
    `tests/test_net_mqtt.py::test_find_messages_refuses_a_mark_from_another_broker_and_an_index`,
    `examples/position_tracker/tests/test_tracker_support.py::test_waits_start_now_filter_the_topic_and_preserve_the_first_match`
- **256.** A Will the broker publishes on a connection loss is in `messages` with `will` set and
    `client` the ID of the client that set it; the client's own publications, the same topic and
    payload included, and the test's have `will` unset. `will=True` and `will=False` on
    `next_message`, `find_message` and `find_messages` keep only Wills or only the rest, and
    `next_message` names its condition by them; `find_messages` combines `will` with `client=`,
    which the other two do not take. Any other `will` than `True`, `False` or `None` is refused.
    `tests/test_net_mqtt.py::test_a_will_is_told_apart_from_the_same_bytes_the_client_published_itself`
- **272.** Each accepted MQTT connection gets one `ConnectionEnd` in `broker.connection_ends` when
    it ends, at the board time it ended, with `by` the way it did: `disconnect` (the client's
    DISCONNECT, logged `mqtt: <id> sent DISCONNECT`), `keepalive`, `takeover`, `closed` (its stream
    closed under it), `malformed` or `test` (`broker.disconnect`), and `will_sent` whether the
    broker published its Will then, after the end is recorded. A connection never accepted gets
    none, and the stream's own close after the broker's adds none. A lost link
    (`modem.drop_call()`) ends the connection as `keepalive` at 1.5 times its keepalive after the
    device's last packet, Will sent; a DISCONNECT ends it at once, no Will.
    `broker.next_end(client_id=None)` waits for the next end, of one client ID when given, and
    polling consumes nothing.
    `tests/test_net_mqtt_ends.py::test_each_way_a_connection_ends_has_its_own_cause`,
    `tests/test_net_mqtt_ends.py::test_the_end_is_recorded_before_the_will_is_published`,
    `tests/test_net_mqtt_ends.py::test_the_tls_layer_closing_under_a_session_ends_it_as_closed`,
    `tests/test_net_mqtt_ends.py::test_a_takeover_that_discards_the_will_says_so`,
    `tests/test_net_mqtt_ends.py::test_a_connection_never_accepted_records_no_end`,
    `tests/test_net_mqtt_ends.py::test_next_end_keeps_one_client_id_and_consumes_nothing`,
    `tests/test_net_mqtt_ends.py::test_a_lost_link_ends_the_connection_at_its_keepalive_with_the_will`,
    `tests/test_net_mqtt_ends.py::test_a_reset_of_the_network_ends_the_connection_as_closed`,
    `tests/test_modem_model.py::test_a_dropped_call_ends_the_mqtt_connection_at_its_keepalive_with_the_will`,
    `tests/test_modem_model.py::test_a_disconnect_ends_the_mqtt_connection_at_once_with_no_will`

- **126.** TLS next-event conditions require a started/completed counter increment after construction.
    Prior successes cannot satisfy them after closure or reset, and a failed new attempt does
    not satisfy completion. Completion of a handshake already in progress does qualify.
    Equal timestamps do not collapse events, polling consumes nothing, and cumulative queries
    continue to reflect history. Next-event conditions name their scope and reject truth testing.
    `tests/test_net_tls.py::test_next_tls_handshake_excludes_previous_success_across_reconnects`

- **127.** Incoming MQTT QoS 1 routes on receipt even while PUBACK is held. Pending receipts with
    the same connection/packet ID share a handle and the first receipt's deadline; each DUP
    flag and receipt time remain visible. Release emits once at the current simulated time.
    After emission, identifier reuse creates a new handle; old release calls send nothing.
    Default/zero-delay ACKs are immediate, and policy changes affect only new exchanges.
    A pending ID reused with another topic or payload joins the same handle and is logged.
    A QoS 0 PUBLISH with DUP set closes the connection as malformed.
    `tests/test_net_mqtt.py::test_default_puback_records_emission_separately_from_receipt`,
    `tests/test_net_mqtt.py::test_hold_routes_duplicates_and_release_acknowledges_only_the_selected_exchange`,
    `tests/test_net_mqtt.py::test_pending_id_reused_for_different_content_is_merged_and_logged`,
    `tests/test_net_mqtt.py::test_qos_zero_publish_with_dup_closes_as_malformed`,
    `tests/test_net_mqtt.py::test_delayed_puback_uses_first_receipt_and_policy_changes_only_new_exchanges`,
    `tests/test_net_mqtt.py::test_manual_release_cancels_a_scheduled_puback`,
    `tests/test_net_mqtt.py::test_late_poll_stamps_actual_emission_time_and_does_not_backdate`

- **128.** Closing an MQTT connection cancels pending PUBACK handles permanently, including on
    disconnect, transport loss, takeover and keepalive expiry. Graceful DISCONNECT suppresses
    the Will, and so does a takeover of a session both connections keep (258); other losses emit
    it once. Keepalive expiry wins a tie with an ACK deadline.
    A reconnect has a fresh connection identity; a stale handle cannot acknowledge its packets.
    A stream whose CONNECT is rejected also takes an ID; client records and Wills carry theirs.
    Synchronous stream-write failure cancels instead of claiming emission. A lost link cancels
    nothing: a delayed handle is emitted at its deadline, to a link that is down, and retained
    receipts stay; emitted history survives closure.
    `tests/test_net_mqtt.py::test_connection_end_cancels_pending_ack_and_stale_release_cannot_ack_reconnect`,
    `tests/test_net_mqtt.py::test_failed_stream_write_cancels_instead_of_claiming_puback_emission`,
    `tests/test_net_mqtt.py::test_rejected_connect_uses_a_connection_id_and_accepted_records_carry_theirs`,
    `tests/test_net_mqtt.py::test_disconnect_leaves_other_clients_pending_and_emitted_history_intact`,
    `tests/test_net_mqtt_failures.py::test_a_lost_link_leaves_a_delayed_puback_to_its_deadline_and_keeps_retained_receipt`

- **129.** Broker disconnect affects only the selected stream over both TCP and TLS. Another stream
    and a new NTP exchange remain usable over the same open PPP link. Reconnect restores no
    subscriptions or broker session even with CleanSession zero; retransmission is recorded
    again and its PUBACK must use the new handle. Emission precedes client consumption.
    `tests/test_net_mqtt_failures.py::test_puback_faults_reconnect_and_other_network_activity`

- **150.** HTTP and DHCP next-event conditions capture append order at creation, exclude earlier
    requests or leases even at equal timestamps, and repeatedly return the first matching record
    without consuming it. History queries return the first matching recorded request or lease. All
    HTTP and DHCP conditions name the expected event, in a board timeout and in the run report too,
    and reject boolean coercion before and after a match. A path filter holding a query is refused
    rather than matching nothing, `serve` refuses such a path too, and a refused request satisfies
    no condition or query.
    `tests/test_net_http.py::test_history_and_next_requests_use_append_order_without_consuming`,
    `tests/test_net_http.py::test_http_conditions_are_named_and_reject_truth_testing`,
    `tests/test_net_http.py::test_a_path_filter_holding_a_query_is_refused`,
    `tests/test_net_http.py::test_serve_refuses_a_path_holding_a_query`,
    `tests/test_net_http.py::test_a_refused_request_satisfies_no_condition_or_query`,
    `tests/test_net_ethernet.py::test_dhcp_next_lease_uses_append_order_without_consuming`,
    `tests/test_net_ethernet.py::test_dhcp_next_lease_is_named_and_rejects_truth_testing`,
    `tests/test_http_request.py::test_a_timeout_names_the_expected_request_or_lease`
- **203.** A bridge puts the whole run on the wall clock: the runner's `hello` says `wall`. It is
    refused after power-on, also once the board is off again, and in front of a replay already
    given; a replay after it takes only a wall recording. A refused bridge leaves the port's
    service and the run's clock as they were.
    `tests/test_http_request.py::test_fetches_from_a_real_service_through_a_bridge`,
    `tests/test_http_request.py::test_a_bridge_comes_before_power_on`,
    `tests/test_pppos_client.py::test_a_bridge_refuses_a_replay_given_before_it`,
    `tests/test_pppos_client.py::test_a_replay_given_after_a_bridge_takes_the_wall_clock_only`,
    `tests/test_net_bridge.py::test_the_backend_may_refuse_a_bridge`
- **204.** A bridged connection carries the bytes both ways in order, each handed to the device in
    a turn and stamped from it; the service's end of the stream closes the device's after its last
    bytes, and the device's close and the end of the run end the stream at the service; a lost link
    does not, until the run ends.
    What the service sends after the device's close reaches nothing and is counted in the transcript.
    `tests/test_net_bridge.py::test_a_connection_goes_to_the_service_and_its_answer_comes_back`,
    `tests/test_net_bridge.py::test_the_service_closing_closes_the_devices_end_after_its_last_bytes`,
    `tests/test_net_bridge.py::test_the_device_closing_ends_the_stream_at_the_service`,
    `tests/test_net_bridge.py::test_what_the_service_sends_after_the_device_closed_is_dropped_and_logged`,
    `tests/test_net_bridge.py::test_a_lost_link_leaves_the_stream_at_the_service_open_until_the_run_ends`,
    `tests/test_net_bridge.py::test_close_ends_a_connection_the_service_keeps_open`,
    `tests/test_net_bridge.py::test_the_services_answer_reaches_the_station_in_a_tcp_segment_stamped_at_the_turn`
- **225.** A connection the test opens to the device comes from `10.64.0.1` and from the next
     ephemeral port, 49152 first, never reused; what the test writes before the device accepts
     waits for the handshake, and goes in pieces no larger than the device's MSS and window, a zero
     window holding it until the device's update; behind PPP and behind Ethernet alike.
     `tests/test_net_client.py::test_the_test_opens_exchanges_and_closes_a_connection`,
     `tests/test_net_client.py::test_a_zero_window_holds_the_data_until_the_devices_update`,
     `tests/test_net_client.py::test_ports_count_up_from_49152_and_a_lost_link_ends_every_connection`
- **226.** A RST to the test's SYN is `refused`, a later one `reset`, and a lost link `lost`: each is
     logged, and every condition that can then no longer hold raises `ConnectionEnded` rather than
     wait; a segment from the device ahead of its stream fails the turn with `OutOfOrder`.
     `tests/test_net_client.py::test_a_rst_to_the_syn_is_refused_in_the_condition_and_the_log`,
     `tests/test_net_client.py::test_a_rst_on_an_established_connection_is_a_reset`,
     `tests/test_net_client.py::test_the_device_closes_first_and_the_tests_end_follows`,
     `tests/test_net_client.py::test_a_segment_ahead_of_the_stream_fails_the_turn`
- **227.** `network.connect` refuses, with `NotReachable` naming what is missing, while the link is
     down, before the device has an address (IPCP open, or a DHCP ACK on this link since the
     device's last DHCP message), and, for an address the test gives, before the station has sent
     a frame.
     `tests/test_net_client.py::test_connecting_before_the_device_has_an_address_says_so`,
     `tests/test_net_client.py::test_a_dhcp_release_gives_the_address_up`,
     `tests/test_http_server.py::test_connecting_before_the_device_has_an_address_says_so`
- **228.** The `http_server` image, behind the test's access point, answers a request the test
     writes on a connection it opened to port 80, closes it when asked, takes a second request on a
     connection kept open, refuses a body over its limit, and refuses a port nobody listens on.
     `tests/test_http_server.py::test_answers_a_get_and_closes_when_asked`,
     `tests/test_http_server.py::test_echoes_json_on_a_connection_kept_open`,
     `tests/test_http_server.py::test_a_body_over_its_limit_is_refused_without_being_read`,
     `tests/test_http_server.py::test_a_port_nobody_listens_on_refuses`
- **229.** The device's chip reset and `board.power_off()` end every connection the test opened to
     it as `lost`, with the reason in the log and in `ConnectionEnded`, leave the device's own
     connections as they are, and leave the device without an address, so that `connect` raises
     `NotReachable`, until IPCP opens again or DHCP acknowledges it again; what the test wrote is
     in the transcript only once it went onto the path.
     `tests/test_net_client.py::test_a_device_that_resets_loses_the_tests_connections_and_its_address`,
     `tests/test_net_client.py::test_a_rst_to_the_syn_is_refused_in_the_condition_and_the_log`,
     `tests/test_http_server.py::test_a_reset_loses_the_tests_connections_until_the_device_listens_again`
- **282.** With `set_path(drop=True)` every IPv4 datagram that enters the path, either way, is
  dropped, while LCP is still answered and PPP stays open: esp-mqtt's keepalive runs out with the
  modem in data mode. A dropped datagram leaves nothing behind it: after the restore, the device's
  next datagram is answered at the path's RTT. `drop=True` takes no RTT or rate.
  `tests/test_net_drop.py::test_a_black_hole_drops_every_datagram_up_and_ppp_control_still_answers`,
  `tests/test_net_drop.py::test_an_answer_made_in_a_black_hole_is_dropped_going_down`,
  `tests/test_net_drop.py::test_after_the_restore_the_next_datagram_is_answered_at_the_paths_rtt`,
  `tests/test_net_drop.py::test_a_black_hole_takes_no_rtt_or_rate`,
  `tests/test_mqtt_session.py::test_a_black_hole_runs_out_the_keepalive_with_ppp_up_and_the_restore_answers_at_the_rtt`
- **283.** What the network's TCP has to send while the path drops, or on a port blocked with
  `drop`, a FIN included, waits, and reaches the device in order once the path is restored or the
  port unblocked, a full window included: the restore and `unblock` ask the device for the ACK and
  window it sent into the black hole with a keepalive's probe. A SYN whose SYN-ACK the black hole
  dropped is answered again when the device sends it again. A block or an unblock that swallowed
  nothing sends no probe. The test cannot open a connection through a black hole.
  `tests/test_net_drop.py::test_the_network_s_tcp_holds_what_it_has_to_send_until_the_restore`,
  `tests/test_net_drop.py::test_a_full_window_moves_on_once_the_device_answers_the_probe`,
  `tests/test_net_drop.py::test_a_probe_due_while_the_station_is_away_goes_when_it_has_a_lease_again`,
  `tests/test_net_drop.py::test_a_block_or_unblock_with_nothing_dropped_sends_no_probe`,
  `tests/test_net_drop.py::test_a_syn_whose_syn_ack_the_black_hole_dropped_is_answered_again_after_the_restore`,
  `tests/test_net_drop.py::test_the_broker_s_keepalive_runs_out_in_a_black_hole_and_ppp_stays_open`,
  `tests/test_net_drop.py::test_the_test_cannot_connect_through_a_black_hole`,
  `tests/test_net_drop.py::test_a_dropped_port_holds_its_service_s_writes_until_unblock`,
  `tests/test_net_drop.py::test_blocking_a_blocked_port_changes_how`
- **284.** A TCP port blocked with `drop` answers none of the device's segments, a new SYN or one
  of an established connection, while DNS and NTP answer; one blocked with `refuse` answers a SYN
  with RST and leaves the connections already open as they are; `unblock` lets a new connection
  through.
  `tests/test_net_drop.py::test_a_port_blocked_with_drop_swallows_the_device_s_segments_and_dns_and_ntp_answer`,
  `tests/test_net_drop.py::test_a_new_connection_to_a_dropped_port_gets_no_answer`,
  `tests/test_net_drop.py::test_a_refused_port_answers_a_syn_with_rst_and_leaves_open_connections`,
  `tests/test_mqtt_session.py::test_a_blocked_broker_port_drops_or_refuses_while_dns_answers`

## Explicit build selection

- **116.** A selected build is resolved relative to the application or taken as absolute, independently
     of the test's working directory. Only its artifacts reach the runner or serial flashing;
     missing or invalid selections never fall back to the app's or cwd's `build/`. The closest
     app marker supplies both app and build, with `build` as the default for an omitted keyword.
     `tests/test_pytest_plugin.py::test_load_app_selects_only_the_explicit_build`,
     `tests/test_pytest_plugin.py::test_board_loads_each_selected_build_from_another_directory`,
     `tests/test_pytest_plugin.py::test_two_builds_of_one_app_run_the_selected_firmware`,
     `tests/test_cli.py::test_cli_passes_only_the_selected_build_to_the_runner`
- **117.** Explicit builds retain target, encryption, missing-file and serial recording-hash checks;
     serial refuses invalid selected artifacts before even an explicitly requested `dut` opens
     a port. Taps hash the selected application's binary.
     `tests/test_pytest_plugin.py::test_selected_build_keeps_validation`,
     `tests/test_serial_backend.py::test_serial_preflight_and_flashing_use_the_selected_build`,
     `tests/test_pytest_plugin.py::test_two_builds_of_one_app_run_the_selected_firmware`
- **118.** A change to `app_path`'s `build_dir` keyword after collection read it is refused, including
     an in-place mutation of the original marker.
     `tests/test_pytest_plugin.py::test_a_late_build_keyword_is_refused`
- **230.** A PlatformIO build directory runs as it is: `firmware.factory.bin` alone at `0x0` when it
     is there, which must hold `firmware.bin` where pioarduino put it, else `bootloader.bin` at
     `0x1000`, `partitions.bin` at `0x8000` and `firmware.bin` where pioarduino flashes it: at the
     table's `ota_0`, else at `0x10000`, the start of an application partition that holds it, also
     when the table has a `factory` partition; nothing is written at the otadata; no file of the
     directory is written, renamed or added.
     `tests/test_platformio_build.py::test_a_build_without_a_factory_image_is_flashed_as_pioarduino_flashes_it`,
     `tests/test_platformio_build.py::test_firmware_bin_goes_at_ota_0_as_pioarduino_flashes_it_even_with_a_factory_partition`,
     `tests/test_platformio_build.py::test_where_firmware_bin_goes_must_be_an_application_partition_that_holds_it`,
     `tests/test_platformio_build.py::test_a_factory_image_is_flashed_whole_from_0x0`,
     `tests/test_platformio_build.py::test_a_factory_image_must_hold_firmware_bin_where_pioarduino_put_it`,
     `tests/test_platformio_build.py::test_nvs_reader_built_by_platformio_runs_as_it_is`
- **231.** The partition table `board.nvs` and `board.flash` read is the one at the offset the build
     names, from the flash file that holds it: inside `firmware.factory.bin` too.
     `tests/test_platformio_build.py::test_board_nvs_and_board_flash_find_the_table_of_either_build`,
     `tests/test_flash.py::test_nvs_reports_missing_or_invalid_partition_table`
- **232.** ~~A PlatformIO build whose application or bootloader header names another chip than the
     classic ESP32 is refused with that chip's name, on both backends; the serial backend refuses
     any PlatformIO build before a port opens.~~ No longer holds since #194, where the serial
     backend flashes a PlatformIO build: see 234 for the chip and 235 for what serial flashes.
- **233.** A recording's `app_sha256` from a PlatformIO build is the hash of `firmware.bin`, with or
     without `firmware.factory.bin`.
     `tests/test_platformio_build.py::test_the_application_image_s_hash_is_firmware_bin_s`
- **234.** A PlatformIO build whose application or bootloader header names another chip than the
     classic ESP32 is refused with that chip's name, on both backends, and so is one whose
     `config/sdkconfig.json` enables flash encryption or secure boot; on serial, before a port
     opens.
     `tests/test_platformio_build.py::test_an_image_for_another_chip_is_refused_by_its_name`,
     `tests/test_platformio_build.py::test_a_bootloader_for_another_chip_is_refused`,
     `tests/test_platformio_build.py::test_a_build_that_enables_flash_encryption_or_secure_boot_is_refused`
- **235.** On serial, a PlatformIO build is flashed as esp32sim runs it: the build's files at the
     offsets of 230, with the flash mode, frequency and size of their headers kept, and the table's
     otadata partition erased when no file of the build covers it, and, unless the build's own
     otadata selects a slot, the first sector of a `factory` partition no file of the build
     covers; an ESP-IDF build keeps pytest-embedded's `IdfApp`.
     `tests/test_platformio_build.py::test_serial_flashes_the_files_esp32sim_runs_with_the_otadata_erased`,
     `tests/test_platformio_build.py::test_serial_flashes_a_factory_image_whole_from_0x0`,
     `tests/test_platformio_build.py::test_a_table_without_otadata_gets_none`,
     `tests/test_platformio_build.py::test_a_factory_partition_the_build_does_not_fill_is_not_booted`,
     `tests/test_platformio_build.py::test_serial_runs_a_platformio_build`,
     `tests/test_platformio_build.py::test_an_esp_idf_build_keeps_pytest_embedded_s_app`

## Resets and power cycles

- **137.** With the board's supply off the runner runs no instruction of the guest, takes the
     peer's turns at their times, routes no UART (bytes for the chip are `unrouted_rx`) and drives
     no pad; when the supply comes back the chip resets with cause 1, and the run's time has not
     restarted.
     `runner/src/session/tests.rs::with_the_supply_off_the_guest_runs_nothing_and_its_time_goes_on`,
     `runner/src/session/tests.rs::with_the_supply_off_no_pad_is_driven`
- **138.** A `chip_reset` with the supply on is a reset with cause 1, and does nothing with it off;
     a `power` to the state the supply is in does nothing.
     `runner/src/session/tests.rs::en_resets_the_chip_with_power_on_as_its_cause_and_does_nothing_without_power`
- **139.** A power-on reset zeroes SRAM and both RTC memories, puts the RTC registers back to
     `POWERON_RESET` with the RTC timer at 0, and keeps the flash and the bus cycle counter.
     `runner/src/session/tests.rs::on_the_emulator_a_power_on_reset_clears_sram_and_the_rtc_and_keeps_the_flash`
- **130.** On the firmware, `esp_restart()` keeps RAM and RTC memory, and `board.reset()` and a power
     cycle clear both and give `POWERON_RESET`; NVS and the SD card's files keep what every boot
     wrote, and the build's flash files are unchanged.
     `tests/test_power.py::test_a_restart_keeps_ram_and_rtc_memory_and_board_reset_clears_them`,
     `tests/test_power.py::test_the_card_keeps_its_power_and_its_files_through_board_reset`,
     `tests/test_power.py::test_a_power_cycle_keeps_nvs_and_the_card_and_the_clock_runs_on_while_off`
- **131.** `board.now` runs on through every reset and while the board is off, and `board.boots`
     holds one boot per reset, at its time, with its cause.
     `tests/test_power.py::test_a_restart_keeps_ram_and_rtc_memory_and_board_reset_clears_them`,
     `tests/test_power.py::test_a_power_cycle_keeps_nvs_and_the_card_and_the_clock_runs_on_while_off`
- **132.** The SD card loses its initialisation with the board's supply, and keeps every block it
     received whole.
     `runner/src/sd_card/tests.rs::after_a_power_cut_the_card_is_uninitialised_and_keeps_its_whole_blocks`
- **133.** A modem and a GNSS receiver model on the board's supply go off with it; the modem keeps its
     power through `board.reset()` and comes back off, until PWRKEY, after a power cycle, and a
     supply the test took with `lose_power()` stays taken; the receiver sends again with the fix
     the test gave it.
     `tests/test_modem_fixture.py::test_the_modem_keeps_its_power_through_a_reset_and_loses_it_with_the_board`,
     `tests/test_modem_model.py::test_a_modem_on_the_board_s_supply_waits_for_pwrkey_when_it_comes_back`,
     `tests/test_modem_model.py::test_the_board_s_supply_and_the_test_s_lose_power_are_apart`,
     `tests/test_gnss_model.py::test_without_the_board_s_supply_the_receiver_is_silent_and_comes_back_with_its_fix`,
     `tests/test_nmea_reader_model.py::test_the_receiver_loses_its_power_with_the_board_and_keeps_its_fix`
- **134.** The board's `power` and `chip_reset` go out once, first in the harness's answer to the turn
     the link is held at when the test calls for them, and one stamped before that turn fails the
     link; an interface cannot send them.
     `tests/test_boardlink.py::test_the_board_s_power_and_en_go_out_first_in_the_answer_to_a_turn`,
     `tests/test_boardlink.py::test_a_board_input_stamped_before_its_turn_fails_the_link`,
     `tests/test_boardlink.py::test_an_interface_cannot_switch_the_board_s_power`
- **135.** On serial, `board.reset()` pulses EN again, and `board.power_off()`, `board.boot`,
     `board.next_boot()` and `board.wait_for_wake()` skip the test with their reasons.
     `tests/test_serial_backend.py::test_console_reset_clock_tap_and_autoflash`,
     `tests/test_serial_backend.py::test_serial_capabilities_skip_at_the_call`
- **136.** Once the backend has ended the test's clock, `board.reset()`, `board.power_off()` and
     `board.power_on()` raise `RunEnded` without waiting, and log and switch nothing.
     `tests/test_clock.py::test_a_step_on_the_board_after_the_end_of_the_run_raises_run_ended_at_once`,
     `tests/test_pytest_plugin.py::test_the_board_s_power_and_reset_after_the_run_ended_raise_run_ended_and_log_nothing`
- **206.** A deep sleep with a timer wake ends in a boot of its own, after `DEEPSLEEP_RESET`, with the
     timer as its wake cause, RTC slow memory kept, and the sleep's length between it and the boot
     before: a chip that never wakes fails the test instead of halting at the end of the run.
     `tests/test_timer_wake.py::test_a_timer_wake_boots_the_chip_again_after_its_sleep`
- **207.** `board.wait_for_wake` returns the chip's next boot at most 1 ms after it, takes no turn of
     the link in a sleep without events, carries its latency in its own waits only, and fails on a
     boot that is no wake from deep sleep and on a chip that does not boot within its time: a run
     that only halts at its end is a failure, and the line the firmware prints after its wake is
     still to come.
     `tests/test_clock.py::test_a_wait_for_an_event_takes_a_turn_only_at_the_event`,
     `tests/test_clock.py::test_the_latency_of_a_wait_for_an_event_lasts_for_that_wait_only`,
     `tests/test_clock.py::test_a_wait_for_an_event_that_never_comes_raises_never_happened_at_its_end`,
     `tests/test_clock.py::test_a_wait_for_an_event_the_run_ends_before_raises_run_ended`,
     `tests/test_timer_wake.py::test_wait_for_wake_runs_the_board_through_its_sleep_to_the_wake`,
     `tests/test_timer_wake.py::test_wait_for_wake_raises_never_happened_for_a_board_that_does_not_sleep`,
     `tests/test_timer_wake.py::test_wait_for_wake_fails_on_a_boot_that_is_no_wake`,
     `tests/test_deep_sleep.py::test_the_deep_sleep_example_wakes_on_its_timer_and_says_how_long_it_slept`
- **250.** `board.next_boot()` is satisfied by the first boot after the call, through any reset, and
     `board.next_boot(cause)` by the first one of that cause only, so an `esp_restart()` does not
     satisfy `POWERON_RESET`; it consumes nothing, and a timeout names it as
     `the next boot (POWERON_RESET)`.
     `tests/test_power.py::test_next_boot_is_the_boot_after_it_of_its_cause_through_a_reset_and_a_power_cycle`,
     `tests/test_power.py::test_next_boot_names_an_unnamed_cause_and_refuses_one_that_is_not_a_number`
- **257.** A boot's `uptime_zero` brackets where its `esp_timer` counted zero on the board's clock:
     a stamp of `uptime` the firmware printed lies between `earliest + uptime` and
     `latest + uptime`. On the classic ESP32 the zero comes after `boot.at` and `latest` is at most
     1 ms after `earliest`, 2 ms for a load while the counter counts, which the runner places a
     boundary later, once LACTLOAD has surely taken it; on the ESP32-S3, -C3 and -C6
     `earliest ≤ boot.at ≤ latest`, within one SYSTIMER tick. The runner sends `uptime` once for
     each start or new load of the counter, between the slice boundaries around it, with
     `earliest` rounded down and `latest` up, and not for a new rate with the same load; once a
     zero it sent no longer holds and it has none in its place (the counter counts from a zero
     that may lie before power-on), it sends `null` for both, once, and `uptime_zero` is `None`.
     A counter that stops sends nothing and keeps the zero sent, which a zero read after it is
     compared with: a boot that went to deep sleep keeps the `uptime_zero` it had before the
     sleep, and the wake's boot gets its own. Never as an event.
     `tests/test_power.py::test_each_boot_s_uptime_zero_puts_the_firmware_s_uptime_on_the_board_s_clock`,
     `tests/test_chips.py::TestOnESP32S3::test_hello_world_boots_and_restarts_through_the_rom` (and the C3's and C6's),
     `runner/src/session/tests.rs::a_counter_seen_counting_is_placed_between_the_boundary_before_and_the_one_that_saw_it`,
     `runner/src/session/tests.rs::a_counter_read_at_its_zero_is_sent_again_only_when_its_zero_moves`,
     `runner/src/session/tests.rs::a_load_that_counts_from_before_power_on_has_no_zero_on_the_classic_chip`,
     `runner/src/session/tests.rs::a_counter_that_stops_keeps_the_zero_sent`,
     `runner/src/session/tests.rs::a_lact_stopped_by_a_deep_sleep_leaves_the_boot_its_zero_until_the_wake`,
     `tests/test_timer_wake.py::test_a_boot_that_sleeps_keeps_its_uptime_zero_through_the_wake`,
     `runner/src/chip/tests.rs::the_classic_lact_reads_as_counting_from_its_load_once_enabled_and_a_reset_stops_it`,
     `runner/src/chip/tests.rs::the_s3_systimer_reads_zero_at_its_reset_and_where_it_is_loaded`,
     `tests/test_boardlink.py::test_each_boot_s_uptime_zero_is_the_runner_s_last_word_on_the_boot_it_came_in`
- **287.** `board.next_boot(uptime_zero=True)` returns the first boot after the call (of `cause`)
     only while its `uptime_zero` is known, read again at each poll: not before, and never a later
     boot in its place, so a boot whose zero never comes times out as
     `the next boot, with its uptime zero`. `board.uptime_zero(boot)` is the held boot's zero as the
     backend knows it now, and refuses a boot unequal to the board's boot of its number.
     `tests/test_condition.py::test_next_boot_with_uptime_zero_waits_for_the_zero_of_the_first_boot_after_it`,
     `tests/test_condition.py::test_next_boot_with_uptime_zero_finds_nothing_for_a_boot_whose_zero_never_comes`,
     `tests/test_condition.py::test_next_boot_with_uptime_zero_and_a_cause_passes_over_a_boot_of_another_cause`,
     `tests/test_condition.py::test_board_uptime_zero_reads_the_boot_s_zero_now`,
     `tests/test_power.py::test_the_boot_next_boot_returns_dates_the_firmware_s_stamps`
- **293.** Each deep sleep of the classic ESP32 is in `board.sleeps` once, under the boot that went
     into it, with what the firmware armed to wake it by ESP-IDF's names: the runner sends one
     `sleep` a boot, never as an event, at the first slice boundary that finds the chip asleep, or
     at the reset of its wake when the sleep began in that slice, before the `reset`, with the
     boundary before it as `earliest`; `latest − earliest` is at most 1 ms and the few bus cycles a
     slice can end past its millisecond. A sleep the RTC watchdog counts through is a sleep too, and
     an awake chip sends none. On `firmware/timer_wake` the next boot's `at − latest` is the 2 s it
     armed, within 2 ms short and 1 ms long, and a second `sleep` in one boot is a link error.
     `tests/test_timer_wake.py::test_each_sleep_is_recorded_with_the_timer_it_armed_and_the_wake_comes_at_its_interval`,
     `runner/src/session/tests.rs::each_deep_sleep_is_sent_once_between_the_boundary_before_and_the_one_that_saw_it`,
     `runner/src/session/tests.rs::a_deep_sleep_the_rtc_watchdog_counts_through_is_a_sleep_too`,
     `runner/src/session/tests.rs::on_the_emulator_a_deep_sleep_says_what_it_armed_until_its_wake_s_reset`,
     `tests/test_boardlink.py::test_each_sleep_is_kept_under_the_boot_that_went_into_it`,
     `tests/test_boardlink.py::test_a_runner_that_breaks_the_protocol_is_stopped_at_its_next_turn`,
     `tests/test_condition.py::test_next_sleep_is_the_first_sleep_after_it_and_reads_only_the_new_ones`

## Reading the flash

- **190.** A `flash_dump` writes the guest's flash as it is at that turn, not the build's, and the
     turn goes on: no time moves, no slice runs and no turn is counted, for the runner and for
     `board.flash()`. Its answer starts `--peer-timeout` again. With `--bt`, the code esp32sim's
     controller trampoline covers in the flash is written in its place.
     `runner/src/session/tests.rs::a_flash_dump_writes_the_flash_of_the_turn_and_the_turn_goes_on`,
     `runner/src/session/tests.rs::on_the_emulator_the_flash_read_has_the_code_esp32sims_trampoline_covers`,
     `runner/src/session/tests.rs::a_flash_dump_moves_no_time_and_runs_no_guest`,
     `runner/src/session/tests.rs::each_flash_dump_ack_starts_the_peer_timeout_again`,
     `tests/test_clock.py::test_a_flash_dump_is_answered_in_the_turn_the_link_is_held_at_and_moves_no_time`,
     `tests/test_flash_snapshot.py::test_boots_read_as_the_current_value_with_the_board_s_time_unmoved`
- **191.** With `--flash-out`, a run that ends at `--max-seconds`, on `stop`, in the emulator, on a
     protocol error or with the peer gone leaves the file, in place by the time `stats` has come; a
     file that cannot be written changes no exit code.
     `runner/src/session/tests.rs::every_end_of_a_run_leaves_the_flash_in_the_file`,
     `runner/src/session/tests.rs::a_flash_dump_that_cannot_be_written_says_why_and_the_run_goes_on`,
     `tests/test_runner_link.py::test_the_file_is_there_once_stats_have_come_whatever_ended_the_run`
- **192.** `board.flash()` reads the current value after a reset, after a power-off and after
     `RunEnded`; a read in a turn removes the file, so a run that later leaves none says so.
     `tests/test_flash_snapshot.py::test_boots_read_as_the_current_value_with_the_board_s_time_unmoved`,
     `tests/test_flash_snapshot.py::test_boots_read_after_a_power_off_and_while_the_board_stays_off`,
     `tests/test_flash_snapshot.py::test_boots_read_after_the_run_has_ended`,
     `tests/test_flash_snapshot.py::test_a_run_that_left_no_flash_says_so_and_a_read_of_a_turn_leaves_no_file`
- **193.** The NVS reader reads what `nvs_get_*` would find: pages by sequence number, only entries
     the bitmap says are written and whose CRCs hold, the later of two writes of a key, a blob only
     with all of its chunks, without the partial copy of a page being freed, and never a broken
     item's data as entries. What `nvs_image` writes it reads back, with every type.
     `tests/test_flash.py::test_pages_are_read_by_sequence_number_not_by_their_place_in_the_partition`,
     `tests/test_flash.py::test_the_state_bitmap_decides_what_is_there`,
     `tests/test_flash.py::test_a_key_written_twice_reads_as_the_later_write`,
     `tests/test_flash.py::test_a_string_whose_span_or_data_is_incomplete_is_not_there`,
     `tests/test_flash.py::test_the_data_of_a_string_or_a_blob_is_never_read_as_entries`,
     `tests/test_flash.py::test_an_entry_whose_header_is_inconsistent_is_one_entry_of_nothing`,
     `tests/test_flash.py::test_a_blob_needs_all_of_its_chunks_and_a_new_version_replaces_the_old`,
     `tests/test_flash.py::test_the_partial_copy_of_a_page_being_freed_is_dropped`,
     `tests/test_flash.py::test_a_round_trip_through_the_generator_reads_what_went_in`
- **194.** `ota()` selects as `esp_ota_get_boot_partition()` does: the highest valid `seq`, over the
     slots from `ota_0` to the first gap, an entry in state `invalid` or `aborted` or with a broken CRC
     left out, else the factory app or the first slot. Through an update and a rollback it matches
     what the firmware reports. It says what the OTA data selects now: a power cycle while an entry
     is `pending_verify` boots the slot before it, and the entry then reads `aborted`.
     `tests/test_flash.py::test_ota_boots_the_slot_of_the_highest_valid_sequence`,
     `tests/test_flash.py::test_ota_leaves_out_an_entry_that_failed`,
     `tests/test_flash.py::test_ota_leaves_out_an_entry_whose_crc_fails_and_names_a_state_it_does_not_know`,
     `tests/test_flash.py::test_ota_with_erased_data_boots_the_factory_app_or_the_first_slot`,
     `tests/test_flash.py::test_ota_counts_the_slots_from_ota_0_to_the_first_gap_as_esp_idf_does`,
     `tests/test_ota_state.py::test_the_ota_state_read_matches_what_the_firmware_reports`,
     `tests/test_ota_state.py::test_a_power_cycle_before_the_app_confirms_itself_boots_the_slot_before_it`

## The real source

- **155.** A `real` source hands over what the port gave stamped with the turn's time, promises no
     latency, and renews its turn at the next whole emulated millisecond.
     `tests/test_real_source.py::test_a_turn_hands_over_what_the_port_gave_stamped_with_the_turn`,
     `tests/test_real_source.py::test_it_promises_no_latency_and_renews_its_turn_at_the_next_whole_millisecond`
- **156.** A `real` source keeps its line at most 50 ms behind, or as long as 128 bytes take on it if
     that is longer: it sends the newest bytes that fit,
     counts the rest in `dropped`, and neither a tap nor the run's log holds what it dropped; at the
     line rate with turns that keep up, it drops nothing, and what the device sent before the run's
     first turn or after its last is not counted.
     `tests/test_real_source.py::test_what_comes_after_the_last_turn_is_not_counted_as_dropped`,
     `tests/test_real_source.py::test_what_the_device_sent_before_the_first_turn_is_not_counted_as_dropped`,
     `tests/test_real_source.py::test_an_adapter_s_whole_usb_packets_at_the_line_rate_lose_nothing`,
     `tests/test_real_source.py::test_the_bound_is_50_ms_where_that_carries_more_than_a_burst`,
     `tests/test_real_source.py::test_a_backlog_keeps_the_newest_bytes_within_the_bound`,
     `tests/test_real_source.py::test_a_device_at_its_line_rate_loses_nothing_while_the_turns_keep_up`,
     `tests/test_real_source.py::test_a_tap_records_the_source_s_stamps_and_not_what_it_dropped`,
     `tests/test_real_source.py::test_a_long_hold_drops_the_backlog_and_an_answer_reaches_the_guest_within_the_bound`
- **157.** A `uart_tx` with no baud, or one further from the port's than 3 %, fails a run with a
     `real` source, within the 3 % it does not. Its bytes are in the tap and the run's log, as the
     firmware transmitted them, and not in the port's queue.
     `tests/test_real_source.py::test_a_baud_within_the_3_percent_passes`,
     `tests/test_real_source.py::test_a_baud_further_from_the_port_fails`,
     `tests/test_real_source.py::test_no_baud_fails`,
     `tests/test_real_source.py::test_the_link_fails_the_run_on_a_transmission_at_another_baud`
- **158.** A `real` source puts the whole run on the wall clock at the call: the runner's `--realtime`
     and `hello`, the link, every recording and later replays. It refuses a replay already given; a
     run without one, or with a call that failed, stays on the emulated clock.
     `tests/test_real_source.py::test_a_real_source_that_could_not_be_made_leaves_the_run_as_it_was`,
     `tests/test_real_source.py::test_a_real_source_puts_the_run_on_the_wall_clock`,
     `tests/test_real_source.py::test_without_a_real_source_the_run_stays_emulated`,
     `tests/test_real_source.py::test_a_replay_on_another_route_must_be_on_the_wall_clock`,
     `tests/test_real_source.py::test_a_replay_given_after_a_real_source_takes_the_wall_clock_only`
- **159.** On `serial`, `real(port)` skips the test at the call and opens no port.
     `tests/test_serial_backend.py::test_serial_capabilities_skip_at_the_call`
- **161.** Closing a `real` source discards what the port's driver holds, and closes a port already
     gone all the same.
     `tests/test_real_source.py::test_closing_discards_what_the_driver_holds_even_on_a_port_that_is_gone`
- **160.** Under `--realtime` the runner starts pacing, and its wall clock, at the first slice
     boundary at or after 2¹⁶ bus cycles at which the guest runs, after any turn there; it waits at
     each block point until the wall clock reaches it, and moves the clock to the boundary at which
     it sees a resync. A signal or the peer gone ends the wait.
     `runner/src/session/tests.rs::pacing_starts_at_the_first_boundary_past_2_16_cycles_after_the_turn_there`,
     `runner/src/session/tests.rs::a_turn_held_before_pacing_starts_does_not_count_against_the_wall_clock`,
     `runner/src/session/tests.rs::a_resync_moves_the_wall_clock_to_the_boundary_it_is_seen_at`,
     `runner/src/session/tests.rs::a_signal_or_the_peer_gone_ends_the_wait_for_wall_time`,
     `tests/test_real_source.py::test_a_turn_held_before_the_first_2_16_cycles_does_not_cost_the_waits`

## Console input

- **236.** Console input uses UART0 RX on GPIO 3 at a host rate independent of the guest rate.
     A full UART0 RX FIFO loses arriving bytes and reports `rxfifo_ovf`, like other UARTs.
     `tests/test_console_writes.py::test_console_writes_keep_the_instant_and_host_baud`,
     `tests/test_console_input.py::test_console_fifo_overflow`
- **237.** U0RXD starts with its input and pull-up enabled after reset; subsequent firmware
     writes may disable or reroute it.
     `runner/src/session/tests.rs::console_rx_reset_restores_native_input_but_firmware_can_disable_or_reroute_it`

## Measured pytest shard assignments

Repository pytest sharding assigns each node ID to exactly one shard. The four-shard measured
assignments in `tests/conftest.py` replace, rather than supplement, the hash assignment; other
counts and unknown node IDs use SHA-256 modulo N. Assignment is independent of the selected
test files. `tests/test_shard.py::test_measured_assignments_preserve_coverage` checks these
properties, and `test_measured_assignments_exist_and_work_in_partial_collection` detects stale
assignments against real collection.
That regression is parameterized over the assignment table and collects each exact node ID
with its assigned shard, covering exceptions outside the HTTP test file as well.

## Firmware build shards

For a fixed ordered image list and shard count N, `firmware/build.sh --shard K/N` selects
positions congruent to K-1 modulo N. Across K=1..N every image occurs exactly once; sharding
never silently drops an image. Invalid or empty shards fail before starting Docker or writing
build output. `tests/test_firmware_build.py` checks both validation and example families.


## CI test pair outcomes

A paired test job succeeds only if both distinct pytest shards exit zero. Both shards start
concurrently with independent temporary, basetemp and pytest-cache directories, and both logs
are retained in the job output even if one fails. An empty pytest collection is a failure, not
a successful optimization. `tests/test_ci_test_pair.py` enforces these orchestration rules.

A long host TMPDIR must not make paired tests exceed Unix socket path limits: the wrapper uses
short independent roots under `/tmp`. `test_pair_keeps_multiprocessing_sockets_short_with_a_long_host_tmpdir`
checks this with real spawned managers.


CI runs each shard's `tests/test_real_source.py` selection before starting the concurrent pair,
and excludes it from the parallel phase. Wall-clock checks keep their assertions and execute
once without a competing shard. `tests/test_shard.py::test_the_workflow_runs_every_shard`
checks the serial selection and parallel exclusion alongside the complete shard matrix.


Group cleanup must not depend on whether the pytest leader is still alive. After `SIGTERM`,
remaining groups receive `SIGKILL` following a shared five-second grace period, including
orphaned descendants that ignore `SIGTERM`.
`tests/test_ci_test_pair.py::test_pair_cancellation_kills_descendants_ignoring_sigterm` checks
both a live leader at cancellation and one that has already exited.

`test_group_cleanup_reaps_a_leader_that_has_already_exited` also checks cleanup of an unreaped
leader; its zombie state must not turn successful cleanup into a macOS permission error.


## HTTP client framing

A device HTTP response is independent of TCP packet boundaries: Content-Length and chunked
responses complete only after their framed body, and unframed responses complete only at a
peer FIN. A malformed or truncated response fails its condition instead of returning a partial
body; transport loss does not count as EOF. Metadata limits exclude their separators even
when a separator arrives in several packets. Chunked decoding consumes each chunk once, so
many small chunks do not stall network callbacks through repeated decoding.
`tests/test_net_http_client.py` enforces this.


Both platform test callers must use the shared test workflow with their matching native build
and firmware dependencies. `tests/test_shard.py::test_the_workflow_runs_every_shard` checks both
callers as well as complete shard coverage after expanding the shared matrix.

**Runtime integrity.** Installed runtimes match the Python version, full esp32sim revision and
platform; ROM bytes match the pinned SHA-256. Archive links and path traversal are never
extracted, failed preparations expose no installation, and explicit paths take precedence.
`tests/test_runtime.py` enforces these rules; the wheel smoke test verifies offline firmware boot.

**Release completeness.** A release contains exactly one version-matched wheel and sdist, and
an executable/manifest pair for each native target. Missing assets, extra files, wrong manifest
versions and corrupted executables fail assembly before SHA256SUMS is written.
`tests/test_release.py` enforces this boundary.

**Published release integrity.** PyPI publication verifies the tag against the package version
and checks the existing SHA256SUMS without regenerating it. Changed wheel or sdist bytes, missing
or altered sums, and a mismatched tag prevent publication. `tests/test_release.py` checks these
cases and verifies that failed checks do not rewrite the published checksums.


**Runtime selection independence.** An explicit runner combined with a cached ROM must work when
the replaced cached runner is missing, corrupt or non-executable. An explicit ROM combined with
a cached runner must work when the replaced cached ROM is missing or corrupt. Selected files
and cache manifests remain checked; `runtime.prepare()` still rejects an incomplete or damaged
installation. `tests/test_runtime.py::test_override_does_not_validate_replaced_cache_file`,
`test_selected_cache_file_still_requires_checksum` and
`test_selected_cache_file_still_requires_manifest` enforce these rules.

**Unreleased installs.** An esp32tl that pip installed from a git commit or a source directory,
editable or not, gets no release runner: `runtime.prepare()` downloads nothing, with or without a
cache, and the backend does not take the cached runner, both with `unreleased esp32tl`, while
`ESP32TL_RUNNER` still serves. A cached ROM serves when the cache's manifest matches; when the
cache cannot give it, the message names the ROM's variable, not `esp32tl runtime prepare`. A git
install of the release's tag `v<version>`, an install from an index or an archive, and one with no
`direct_url.json` count as the release. A checkout's missing runner or ROM names the checkout's
make target, `build-runner` or `rom-elf`, since `prepare` refuses a checkout's install.
`tests/test_runtime.py::test_unreleased_reads_where_pip_installed_from`,
`test_unreleased_install_gets_no_release_runner`,
`test_unreleased_install_downloads_nothing_without_a_cache`,
`test_unreleased_install_names_the_rom_variable_not_prepare` and
`test_a_checkout_s_missing_file_names_its_make_target` enforce these rules.

## Runtime revision pins

The runtime installer and every esp32sim dependency in `runner/Cargo.toml` and
`runner/Cargo.lock` must name the same revision. Otherwise the installer would
reject the runner built from this checkout.
`tests/test_runtime.py::test_runtime_revision_matches_runner_pins` enforces this.

## Version-only release changes

Only an increasing stable version applied identically to Python, Cargo and the runner lockfile
may use lightweight CI or trigger a release tag after merge. All other bytes and regular-file
modes must remain unchanged. Mixed changes, missing or inconsistent fields, unchanged/reversed
diffs and unknown refs cannot take the shortcut. The bump validates all files before writing.
`tests/test_release_version.py` checks this against real local git repositories and CLI output.

## A host behind a virtual Wi-Fi station

The virtual station and virtual access point are exclusive, and station mode cannot switch an
access point. `tests/test_wifi.py::test_station_flags_properties_and_exclusions`,
`runner/src/cli/tests.rs::wifi_station_parses_and_refuses_conflicting_modes` and
`runner/src/session/tests.rs::virtual_station_relays_frames_and_refuses_access_point_switches`
enforce this.

The harness answers ARP only for its current leased address, sends TCP from that address, and
forgets it on reset or expiry. Before a lease even an explicit TCP destination is not reachable.
`tests/test_net_station.py::test_arp_answers_only_our_address_and_resolves_before_tcp` and
`test_lease_lost_offer_retries_nak_and_expiry` enforce this. Identical DHCP retries held before
association occupy one runner queue entry:
`runner/src/session/tests.rs::station_dhcp_retries_do_not_accumulate_behind_association_backpressure`.

A new ARP destination cannot postpone an existing resolution retry. Due batches retry all
pending destinations one second apart, and resolving every destination clears the retry timer.
`tests/test_net_station.py::test_new_arp_destination_does_not_postpone_pending_retries` enforces
this with staggered destinations, two retry batches and replies releasing the queued datagrams.


## Radio diagnostics at teardown

- **238.** Radio diagnostics in a readable runner log must appear in the backend's teardown
  report even when live log forwarding has not delivered them, including a diagnostic joined
  to the guest's final console fragment without a newline.
  `tests/test_pytest_plugin.py::test_radio_diagnostics_survive_a_stopped_log_forwarder`
  disables forwarding and places the diagnostics before more than a log tail's worth of output,
  with and without a preceding console fragment.

## Virtual station DHCP lifecycle

`test_no_discover_or_deadline_until_ready` enforces silence and no retry deadline before data-link readiness. `test_disconnect_discards_exchange_and_stale_reply` checks pending and bound exchanges: disconnect clears the lease, loses connections, and old replies cannot bind a new exchange. `test_delayed_softap_starts_dhcp_at_link_ready` uses real Arduino firmware with a 15300 ms startup delay, for open and WPA2 networks; the first DISCOVER has the readiness timestamp, without a lease-duration bound. `test_softap_leaves_and_rejoins` checks real link loss and fresh DHCP. Existing reset/power-cycle regressions check lease invalidation and reacquisition.

`station_disconnect_drops_backpressure_and_future_ethernet` verifies that queued and future Ethernet input cannot cross a reconnect. `station_lifecycle_reads_handshake_state_and_flushes_relay` checks the pinned emulator API and relay queue clearing. The stale-reply matrix also covers reset and power cycle during both pending and bound exchanges.

## Plugin warning defaults

- **239.** The XML attribute warning default suppresses the exact experimental API message,
  including when the consumer directly requests the shared fixture.
  `tests/test_pytest_plugin.py::test_xml_attribute_warning_is_silent_without_consumer_filters`
  enforces this through a consumer pytest subprocess without firmware or a runner.

- **240.** Other warning messages and categories remain visible.
  `tests/test_pytest_plugin.py::test_xml_attribute_filter_keeps_other_warnings` enforces this
  through a consumer pytest subprocess without firmware or a runner.

- **241.** Consumer ini, command-line and marker filters can override the XML attribute warning
  default. `tests/test_pytest_plugin.py::test_consumer_can_override_xml_attribute_filter`
  enforces this through consumer pytest subprocesses without firmware or a runner.

## Bluetooth history times

Public Bluetooth histories carry `Duration`s on their owning air's clock, including channel
traffic, refused openings, pairings and unknown commands. Named records retain tuple unpacking.
`test_a_device_refused_at_sabm_may_authenticate_and_open_the_channel` checks every populated
history and compares channel time directly with a GPIO `LineEvent.t`. The guest-controller
session and unknown-command tests in `tests/test_bluetooth_guest.py` check board-clock times,
unchanged report text and JSON integer `t_ns` fields.

## Independent xdist board runs

`tests/test_xdist.py` requires the same real-firmware console bytes and simulated timestamps
alone, serially and under two workers, preserved per-test JUnit link properties, isolated card
clones, and rejection of serial distributed execution before accessing hardware.
`test_a_hung_run_is_ended_by_the_test_s_timeout` checks timeout teardown in workers too.

## SPP conditions

- **242.** SPP state conditions describe the peer's current channel state and refuse boolean coercion.
  `test_channel_conditions_follow_state_and_name_timeouts` in `tests/test_spp_peer.py` checks
  state changes and timeout descriptions.

- **243.** `next_write` sees only device writes appended after
  its creation, including equal-time writes, without consuming events or joining chunks;
  `test_next_write_keeps_append_boundary_and_does_not_consume` checks these rules.

- **244.** Its boundary survives channel closure and reopening, enforced by
  `test_next_write_survives_channel_close_and_reopen`.

- **260.** `SppPeer.written_lines(separator)` joins the device's writes of one opening of the
  channel and cuts them at `separator`: a line written in two chunks is one line, at the time of
  the chunk that brought its separator, and a partial line at a channel's close is a line of its
  own, not complete, never joined with the next opening's first. What the open channel has after
  its last separator is not a line.
  `tests/test_spp_peer.py::test_a_command_written_in_two_chunks_is_one_line_at_the_time_of_the_second`,
  `tests/test_spp_peer.py::test_a_partial_line_at_a_channel_close_is_kept_apart_from_the_next_channel`,
  `tests/test_spp_peer.py::test_lines_are_cut_at_the_separator_the_test_gives_even_across_chunks`
- **261.** `next_written_line` finds the first complete line that ends after its creation, even
  one begun before it, at equal timestamps and across a channel close and reopen, without
  consuming anything; a line cut off by a close never matches.
  `tests/test_spp_peer.py::test_next_written_line_keeps_the_line_boundary_and_does_not_consume`,
  `tests/test_spp_peer.py::test_next_written_line_survives_channel_close_and_reopen`

## Project defaults

- **245.** A test that uses `board` takes its app, its build and its board profile from the
  closest marker, then from `esp32tl_app_path`, `esp32tl_build_dir` and `esp32tl_board_profile`
  or `esp32tl_board_profile_path`, then from the test file's directory, `build` and
  `esp32-devkitc`. An `app_path` marker replaces both the project's app and its build;
  `esp32tl_app_path` and `esp32tl_board_profile_path` resolve against the configuration file's
  directory, whatever directory pytest starts in, and `esp32tl_build_dir` against the selected
  app; a quoted value and both profile options set are usage errors, and a default profile
  that does not load names its option.
  `tests/test_pytest_plugin.py::test_project_defaults_resolve_against_the_configuration_file_from_any_directory`,
  `tests/test_pytest_plugin.py::test_project_defaults_take_a_bundled_profile_and_absolute_paths`,
  `tests/test_pytest_plugin.py::test_a_build_directory_alone_applies_to_the_test_file_s_directory`,
  `tests/test_pytest_plugin.py::test_an_override_on_the_command_line_resolves_against_the_configuration_file`,
  `tests/test_pytest_plugin.py::test_both_profile_defaults_are_a_usage_error`,
  `tests/test_pytest_plugin.py::test_a_wrong_profile_default_names_its_option`,
  `tests/test_pytest_plugin.py::test_a_quoted_value_in_an_ini_file_is_a_usage_error`,
  `tests/test_pytest_plugin.py::test_a_quoted_value_in_toml_is_a_usage_error_without_the_ini_note`,
  `tests/test_pytest_plugin.py::test_a_quoted_value_from_the_command_line_gets_no_ini_note`,
  `tests/test_pytest_plugin.py::test_an_ini_file_takes_the_values_without_quotes`

## Frame times of a model that frames by silence

- **246.** `IdleFramedModel.on_frame(at, frame)` keeps its two arguments, `at` the instant the
  silence ended the frame; while it runs, `frame_first_at` and `frame_last_at` are the `at` of
  the frame's first and last pieces. A piece exactly `gap` after the last continues the frame,
  one later starts the next.
  `tests/test_model_bases.py::test_on_frame_knows_when_the_frame_s_first_and_last_pieces_came`,
  `tests/test_model_bases.py::test_a_piece_at_the_gap_s_end_continues_the_frame_and_a_later_one_starts_the_next`,
  `tests/test_model_bases.py::test_a_subclass_that_reads_no_frame_times_works_as_before`,
  `tests/test_model_bases.py::test_a_dropped_frame_leaves_no_first_piece_behind`

## The emulated device's Wi-Fi MACs

- **247.** Every chip esp32tl runs has an entry in `EMULATED_MACS`, `None` only for the ESP32-P4,
  which has no Wi-Fi. A chip's station MAC is the base MAC the runner gives its machine, and its
  SoftAP MAC is derived from that as ESP-IDF derives it with the chip's default number of universal
  MACs: four (the classic ESP32, S3, C3, C6) adds one to the last octet, two (the S31) sets the
  locally administered bit. The virtual station's default MAC is none of them, and `wifi.station`
  refuses the board's own chip's two as its own, and only those; a `Wifi` given a chip the table
  does not name raises `KeyError`.
  `tests/test_wifi.py::test_every_chip_esp32tl_runs_has_its_macs_or_none`,
  `tests/test_wifi.py::test_the_device_s_macs_are_the_runner_s_base_mac_and_the_softap_mac_esp_idf_derives`,
  `tests/test_wifi.py::test_the_virtual_station_refuses_the_board_s_chip_s_macs`,
  `tests/test_wifi.py::test_the_virtual_station_takes_another_chip_s_macs_and_any_without_a_chip`,
  `tests/test_wifi.py::test_a_wifi_on_a_chip_the_table_does_not_name_is_refused`,
  `tests/test_wifi.py::test_the_emulated_board_s_wifi_refuses_its_own_chip_s_macs`
- **292.** The station MAC `emulated_macs` gives for the profile's chip before power-on is the MAC the
  firmware's DHCP lease from the virtual access point carries, on the classic ESP32, the S3, the C3
  and the C6.
  `tests/test_http_request.py::test_joins_and_fetches_from_the_tests_server`,
  `tests/test_chips.py::TestOnESP32S3::test_http_request_joins_and_fetches_from_the_tests_server`,
  `tests/test_chips.py::TestOnESP32C3::test_http_request_joins_and_fetches_from_the_tests_server`,
  `tests/test_chips.py::TestOnESP32C6::test_http_request_joins_and_fetches_from_the_tests_server`

## Runtime download errors

- **248.** Only an HTTP 404 for the release's runtime manifest adds the possible causes and the
  alternatives to `download failed`; another status for the manifest, and a 404 for the runner
  after the manifest was found, keep their message.
  `tests/test_runtime.py::test_a_missing_manifest_names_possible_causes_and_alternatives`,
  `tests/test_runtime.py::test_another_http_error_for_the_manifest_keeps_its_message`,
  `tests/test_runtime.py::test_a_missing_runner_beside_its_manifest_keeps_its_message`

## A console on USB-Serial/JTAG

- **252.** A board profile's `console` is `uart0` unless it says `usb-serial-jtag`, which only the
  ESP32-S3, ESP32-C3 and ESP32-C6 take; on the classic ESP32 the profile does not load. With it,
  UART0 and its pins are free for the profile's routes and lines, and the controller's
  USB D- and D+ pads are not.
  `tests/test_boards.py::test_the_console_is_uart0_unless_the_profile_says_usb_serial_jtag`,
  `tests/test_boards.py::test_a_console_the_chip_does_not_have_is_refused`,
  `tests/test_boards.py::test_a_console_on_usb_serial_jtag_leaves_uart0_and_its_pins_to_the_routes`,
  `tests/test_boards.py::test_a_console_on_usb_serial_jtag_takes_the_usb_pads`,
  `tests/test_pytest_plugin.py::test_invalid_local_profiles_fail_at_collection`
- **253.** The runner refuses `--console-port usb` on the classic ESP32 before the run, and
  `console_rx` on a run without it as a protocol error. With it, the USB-Serial/JTAG controller's
  output is the `console`, one message per line at the end of its slice, UART0's bytes are not,
  and `console_rx` reaches the controller when it comes due, or is counted in
  `stats.console_rx_lost` with the board's supply off.
  `runner/src/cli/tests.rs::console_port_selects_uart0_or_the_usb_serial_jtag_controller`,
  `tests/test_runner_link.py::test_the_runner_prints_the_console_port_it_is_given_on_stdout`,
  `runner/src/session/tests.rs::console_rx_needs_the_console_on_usb`,
  `runner/src/session/tests.rs::with_the_console_on_usb_its_output_goes_out_as_console_and_uart0_stays_off_it`,
  `runner/src/session/tests.rs::console_rx_reaches_the_usb_console_when_it_comes_due_and_is_lost_with_the_supply_off`
- **254.** The esp32sim backend gives the runner `--console-port usb` for a profile whose console
  is on USB-Serial/JTAG, and nothing for one on UART0.
  `tests/test_pytest_plugin.py::test_runner_command_selects_the_usb_console_of_a_profile_that_has_it`
- **255.** The serial backend runs a profile for the ESP32-S3 whose console is on USB-Serial/JTAG,
  skips one for the ESP32-C3 or ESP32-C6 with that console before the port is opened, and refuses
  one for the S3, C3 or C6 whose console is on UART0.
  `tests/test_serial_backend.py::test_an_s3_with_its_console_on_usb_serial_jtag_runs_on_the_serial_backend`,
  `tests/test_serial_backend.py::test_a_usb_serial_jtag_console_on_a_chip_not_validated_on_hardware_skips`,
  `tests/test_pytest_plugin.py::test_the_serial_backend_refuses_a_board_with_another_chip`

## The public API

- **259.** Every type that a public method or property of a class `esp32tl` exports takes or
  returns, and every type of such a class's public attributes and fields, is importable from
  `esp32tl`, `esp32tl.units` or `esp32tl.recording`, under the class's own name but for
  `ModemModel`; `esp32tl.__all__` names every public name of the package root but its submodules.
  Left out are what only the backend calls: the members the link's `Interface` declares, on an
  interface; the pairs `BACKEND_SIDE` names, as the package's services' `accept`; and the
  constructors of the classes the backend makes. The protocols a test implements, `Model`,
  `Source`, `StreamService` and the rest, keep all of their methods. A public attribute that
  holds an instance of one of the package's generic classes, as a `Condition`, is annotated with
  its parameters, so that what waiting on it returns is checked too.
  `tests/test_public_api.py::test_every_type_a_test_meets_is_public`,
  `tests/test_public_api.py::test_all_names_what_the_package_exports`
- **285.** No public method or property of a class `esp32tl` exports is annotated as returning
  `Callable[[], …]`, alone or in a union as `Callable[[], …] | None`: what such a member hands
  out to be called with no arguments is a described condition and is annotated `Condition[…]`,
  as `Broker.next_message`'s `Condition[Message | None]` and `Broker.is_subscribed`'s
  `Condition[bool]`, so that a type checker keeps its `description` and a helper typed with a
  `Condition` accepts it.
  `tests/test_public_api.py::test_a_public_method_returning_a_function_of_no_arguments_says_condition`
- **291.** Every public method, property and attribute of a class `esp32tl` exports that is a
  `Condition` or a `LineCondition` has the shape of its kind: a `next_*` one, and one for an
  action's outcome, is a method; any other is a condition for a state, a property with no
  parameter and a method with one, named `is_<state>` unless it is one for what has happened once
  and for good, as `TcpConnection.established`. The actions, what has happened, and
  `Console.line` and `HttpExchange.response`, named for what they return, are listed in the
  test, and an entry that is no condition fails it.
  `tests/test_public_api.py::test_every_condition_has_the_shape_of_its_kind`

## A killed pytest session

- **262.** A pytest killed with `SIGKILL` in the middle of a long `board.wait` leaves, within 5 wall
  seconds, no runner, no log forwarder, no pytest-embedded `MessageQueueManager` server, no dut
  listener and no resource tracker running. The runner ends a run at end of file on
  `--parent-fd` as with the peer gone, exit code 4, without `stats`; the backend passes the pipe's
  read end, and a power-on that fails, at the start or before it, leaves neither end open. A signal
  while the parent lives still ends the run with `stats` and exit 0.
  `tests/test_killed_session.py::test_a_killed_pytest_leaves_no_runner_forwarder_or_manager`,
  `tests/test_killed_session.py::test_the_runner_gets_the_read_end_of_a_pipe_whose_write_end_the_backend_holds`,
  `runner/src/parent/tests.rs::the_flags_stay_clear_while_the_write_end_is_open_and_are_set_once_it_closes`,
  `tests/test_killed_session.py::test_a_power_on_that_fails_before_the_start_closes_the_pipe`,
  `runner/src/session/tests.rs::the_parent_s_end_ends_the_run_as_the_peer_gone_without_stats`,
  `runner/src/session/tests.rs::a_signal_while_the_parent_lives_ends_the_run_with_stats`

## A Bluetooth controller the test did not give

- **263.** A firmware that calls `esp_bt_controller_init` in a run whose test never looked at
  `board.bluetooth` does not run on: the runner, given `--no-bt` and the application's ELF, answers
  the call with `ESP_ERR_NOT_SUPPORTED` and ends the run at the end of that slice with `stats`
  `end: bt_controller` and exit code 5, and the test fails with a reason that names
  `board.bluetooth`. A firmware that never calls it runs to the end. The backend gives `--no-bt`
  for every `Bluetooth` the test did not use, on a chip esp32sim models Bluetooth on and for a
  build whose ELF is a file, and `--bt` alone for one it used.
  `runner/src/session/tests.rs::under_no_bt_the_guests_call_to_its_bluetooth_controller_ends_the_run_at_the_slice_end`,
  `runner/src/session/tests.rs::under_no_bt_a_guest_that_never_starts_its_controller_runs_to_the_end`,
  `runner/src/cli/tests.rs::no_bt_names_the_application_elf_needs_a_board_and_excludes_bt`,
  `tests/test_pytest_plugin.py::test_runner_command_forwards_the_guests_hci_once_the_test_uses_bluetooth`,
  `tests/test_pytest_plugin.py::test_runner_command_gives_no_bluetooth_where_esp32sim_has_none_or_the_build_no_elf`,
  `tests/test_bt_spp_initiator.py::test_a_test_that_gives_the_board_no_bluetooth_fails_naming_board_bluetooth`,
  `tests/test_chips.py::TestOnESP32S3::test_without_bt_the_controller_s_start_ends_the_run_naming_board_bluetooth`,
  `tests/test_chips.py::TestOnESP32C3::test_without_bt_the_controller_s_start_ends_the_run_naming_board_bluetooth`

## History conditions

- **266.** A "next …" condition over an append-only history reads, at each poll, only the records
  appended since its last poll, and after its match only the match: a poll after each of 10 000
  records reads each of them once. The boundary is still the history's length when it was made,
  polling never moves it, and every later poll returns the same record.
  `tests/test_condition.py::test_first_from_reads_each_record_once_and_keeps_its_match`,
  `tests/test_condition.py::test_next_boot_reads_only_the_boots_since_the_last_poll`,
  `tests/test_net_mqtt.py::test_a_next_message_poll_reads_only_the_messages_since_the_last_one`,
  `tests/test_net_mqtt.py::test_a_next_delivery_poll_reads_only_the_deliveries_since_the_last_one`,
  `tests/test_net_mqtt.py::test_a_next_subscribe_poll_reads_only_the_requests_since_the_last_one`,
  `tests/test_net_http.py::test_a_next_request_poll_reads_only_the_requests_since_the_last_one`,
  `tests/test_net_services.py::test_a_next_exchange_poll_reads_only_the_exchanges_since_the_last_one`,
  `tests/test_net_services.py::test_a_next_question_poll_reads_only_the_questions_since_the_last_one`,
  `tests/test_spp_peer.py::test_a_next_write_poll_reads_only_the_events_since_the_last_one`,
  `tests/test_spp_peer.py::test_a_next_written_line_poll_reads_only_the_lines_since_the_last_one`,
  `tests/test_net_client.py::test_a_next_data_poll_searches_only_the_bytes_since_the_last_one`,
  `tests/test_ble_peer.py::test_a_next_advertisement_poll_reads_only_the_advertisements_since_the_last_one`,
  `tests/test_ble_peer.py::test_a_next_write_poll_reads_only_the_writes_since_the_last_one`,
  `tests/test_lines.py::test_a_next_change_poll_reads_only_the_events_since_the_last_one`,
  `tests/test_net_link_changes.py::test_a_next_link_poll_reads_only_the_changes_since_the_last_one`
- **279.** A line's `next_change` matches only a change made after the call: a level that already
  holds does not satisfy it, it returns the same `LineEvent` at every poll, and a second
  `next_change` made after the first one matched returns the next change, a different event. A
  change by the test or a model does not match `by="firmware"`.
  `tests/test_lines.py::test_next_change_waits_for_a_change_after_the_call_even_while_the_level_holds`,
  `tests/test_lines.py::test_next_change_by_the_firmware_ignores_the_test_s_and_a_model_s_changes`,
  `tests/test_modem_recovery.py::test_two_successive_next_presses_of_pwrkey_are_the_recovery_s_two_presses`

## The reset reason ESP-IDF reports

- **267.** Each boot's `Boot.idf_reason` is what the firmware's `esp_reset_reason()` reports for
  it, though the ROM reports `esp_restart()` and a panic alike: on the classic ESP32 for a
  power-on, `esp_restart()`, a panic, a task watchdog's panic and a wake from deep sleep, and on
  the S3, C3, C6, P4 and S31 for the first four. The runner sends with every `reset` the
  reset-reason hint register as the guest left it, and 0 after a power-on; on every chip a chip
  reset keeps the register, through esp32sim or the runner putting it back, and a power-on clears
  it. A software reset (cause 3 or 12) takes the hint only for `ESP_RST_PANIC`, `ESP_RST_INT_WDT`,
  `ESP_RST_TASK_WDT` and `ESP_RST_BROWNOUT`, and every other cause ignores it.
  `tests/test_reset_reasons.py::test_each_boot_is_named_as_the_firmware_s_esp_reset_reason`,
  `tests/test_chips.py::TestOnESP32S3::test_each_boot_is_named_as_the_firmware_s_esp_reset_reason`,
  `tests/test_chips.py::TestOnESP32C3::test_each_boot_is_named_as_the_firmware_s_esp_reset_reason`,
  `tests/test_chips.py::TestOnESP32C6::test_each_boot_is_named_as_the_firmware_s_esp_reset_reason`,
  `tests/test_chips.py::TestOnESP32P4::test_each_boot_is_named_as_the_firmware_s_esp_reset_reason`,
  `tests/test_chips.py::TestOnESP32S31::test_each_boot_is_named_as_the_firmware_s_esp_reset_reason`,
  `tests/test_reset_reasons.py::test_a_software_reset_takes_the_hint_the_boot_before_left`,
  `tests/test_reset_reasons.py::test_any_other_cause_ignores_the_hint`,
  `runner/src/session/tests.rs::a_reset_is_forwarded_with_its_cause_its_hint_and_the_reset_routes`,
  `runner/src/session/tests.rs::with_the_supply_off_the_guest_runs_nothing_and_its_time_goes_on`,
  `runner/src/chip/tests.rs::the_classic_chip_keeps_its_reset_hint`,
  `runner/src/chip/tests.rs::the_s3_keeps_its_reset_hint`,
  `runner/src/chip/tests.rs::the_c3_keeps_its_reset_hint`,
  `runner/src/chip/tests.rs::the_c6_keeps_its_reset_hint`,
  `runner/src/chip/tests.rs::the_p4_keeps_its_reset_hint`,
  `runner/src/chip/tests.rs::the_s31_keeps_its_reset_hint`

## Bluetooth LE

- **268.** The firmware's controller is its chip's: on the ESP32-S3, the ESP32-C3 and the
  ESP32-C6 it reports page 0 alone, `LE Supported (Controller)` and `BR/EDR Not Supported`, an LE
  ACL buffer of 251 bytes and no extended advertising; on the classic ESP32 it keeps BR/EDR and no
  LE.
  On an LE-only chip it answers what the ESP32-S3 answered `tools/hci_probe`, the same bytes, and
  answers a command the S3's supported commands do not name with `Command_Complete` and
  `UNKNOWN_HCI_COMMAND`.
  `tests/test_ble_peer.py::test_an_le_only_chip_s_controller_reports_le_and_no_br_edr`,
  `tests/test_ble_peer.py::test_the_classic_esp32_s_controller_keeps_br_edr_and_no_le`,
  `tests/test_ble_peer.py::test_an_le_only_chip_s_controller_answers_as_the_measured_esp32_s3`,
  `tests/test_ble_peer.py::test_an_le_only_chip_s_controller_answers_what_it_does_not_know_with_command_complete`
- **269.** With Espressif's flow control of advertising reports on (`0xFD09`), the guest's
  controller sends its host no more `LE Advertising Report` events than the credits it has, and
  counts the rest in `dropped_reports`; a credit given back (`0xFD0A`) gets no command acknowledgment, and neither
  command is an unknown one. Once the reports dropped reach the lost threshold, the next credit given
  back first brings the discard event (LE Meta `0xF0`) with their number, which takes a credit.
  `tests/test_ble_peer.py::test_espressif_s_flow_control_sends_no_more_reports_than_the_credits`,
  `tests/test_ble_peer.py::test_past_the_lost_threshold_a_credit_given_back_first_tells_how_many_were_dropped`
- **270.** An LE connection one of whose devices left the air falls on both ends with
  `CONNECTION_TIMEOUT` (`0x08`) at the supervision timeout it was made or last updated with, counted
  from the instant it left.
  An update of the connection asked for once it lost contact does not change that timeout.
  `tests/test_ble_peer.py::test_out_of_range_both_ends_fall_at_the_supervision_timeout_and_reconnect_in_range`,
  `tests/test_ble_peer.py::test_an_update_of_a_connection_that_lost_contact_never_completes`,
  `tests/test_ble.py::TestBleOnESP32S3::test_a_central_out_of_range_falls_at_its_timeout_and_reconnects`,
  `tests/test_ble.py::TestBleOnESP32C3::test_a_central_out_of_range_falls_at_its_timeout_and_reconnects`
- **271.** A `BleCentral` that bonded with `gatt_security_server` encrypts its next connection to
  it with the kept keys after a reset of the firmware, at the firmware's new resolvable private
  address, without pairing again.
  `tests/test_ble.py::TestBleOnESP32S3::test_just_works_pairing_bonds_across_a_firmware_reset`,
  `tests/test_ble.py::TestBleOnESP32C3::test_just_works_pairing_bonds_across_a_firmware_reset`

## What the device asked NTP and DNS

- **277.** NTP keeps every client request the device sent, answered or not, and DNS every question
  it parsed, each at the board's time it came off the path; a datagram that is neither is not
  kept. A request while NTP is silent is kept with `answered=False`, one after `resume()` with
  `answered=True`, and `requests` counts the answered ones. `next_exchange` and `next_question`
  return the first record appended after their call, the same one at every later poll.
  `tests/test_net_services.py::test_ntp_keeps_every_request_with_its_time_and_whether_it_was_answered`,
  `tests/test_net_services.py::test_ntp_next_exchange_is_a_described_condition_from_its_call`,
  `tests/test_net_services.py::test_dns_keeps_every_question_it_parsed_with_its_time_and_type`,
  `tests/test_net_services.py::test_dns_next_question_is_a_described_condition_from_its_call`,
  `tests/test_net_path.py::test_a_stated_rtt_is_exact_from_the_request_to_the_answers_stamp`,
  `examples/position_tracker/tests/test_tracker.py::test_retries_ntp_further_apart_and_resolves_its_name_again`

## The link's changes

- **275.** `network.link_changes` records the link between the device and the network coming up and
  going down, each at the board's instant: behind a modem up at each IPCP open and down at the
  first way it ends, with a cause naming that way; behind a virtual station up when the station
  joins and down when it leaves or the device resets. A change to what the link already is records
  nothing, but for a station's join counted again while joined, which is a down and an up at its
  instant, and behind a Wi-Fi access point nothing is recorded and `next_link_up` and
  `next_link_down` raise `ValueError`. `modem.drop_call()` and `modem_recovery`'s redial give a
  down, `call ended` (`far end terminated` with `terminate=True`), then an up, `IPCP open`, and
  `arduino_shell`'s SoftAP leaving and coming back gives `station left` then `station joined`, at
  the same times in two runs.
  `tests/test_net_link_changes.py::test_ipcp_open_is_up_and_the_devices_terminate_request_is_down`,
  `tests/test_net_link_changes.py::test_what_the_device_does_to_an_open_link_names_its_down`,
  `tests/test_net_link_changes.py::test_a_link_that_never_opened_ipcp_records_nothing`,
  `tests/test_net_link_changes.py::test_a_device_reset_is_down_and_its_renegotiation_no_second_one`,
  `tests/test_net_link_changes.py::test_each_way_the_modem_ends_the_call_names_its_down`,
  `tests/test_net_link_changes.py::test_a_dropped_call_and_a_redial_give_a_down_and_an_up`,
  `tests/test_net_link_changes.py::test_a_station_joining_and_leaving_is_up_and_down`,
  `tests/test_net_link_changes.py::test_behind_an_access_point_nothing_is_recorded_and_the_conditions_refuse`,
  `tests/test_modem_recovery.py::test_redials_after_a_dropped_call`,
  `tests/test_modem_recovery.py::test_recovers_a_hung_modem`,
  `tests/test_arduino_softap.py::test_softap_leaves_and_rejoins`,
  `tests/test_determinism.py::test_a_dropped_call_and_the_redial`,
  `tests/test_determinism.py::test_device_softap_left_and_joined_again`

## A SYN the device does not answer

- **280.** The SYN of a connection the test opened goes again, the same segment, 1, 3, 7, 15, 31
  and 63 s after the first while the device answers none, and the connection ends as
  `"timed out"` 127 s after the first, with no SYN more. A SYN-ACK or a RST to any of them ends the
  timer. While the connection is connecting, a wait on any of its conditions, or on the HTTP
  client's `response`, that times out names every SYN sent so far: each one sent again before the
  last as having got no answer, the last as having had none in its age, or `has just gone` at age
  0, and the condition's `waiting` is called once for it. The run report lists each connection
  the test opened with its state. On `http_server`, a `GET` sent
  when the DHCP lease holds is answered after the SYN sent again 1 s later, with no wait for a
  console line, and a port nothing listens on refuses that SYN.
  `tests/test_net_client.py::test_an_unanswered_syn_goes_again_on_a_doubling_rto_then_times_out`,
  `tests/test_net_client.py::test_the_devices_answer_to_a_syn_sent_again_ends_its_timer`,
  `tests/test_net_client.py::test_a_connection_lost_while_connecting_keeps_no_timer`,
  `tests/test_net_client.py::test_a_http_response_waited_for_before_the_syn_is_answered_says_so`,
  `tests/test_clock.py::test_a_tcp_wait_that_times_out_names_the_syns_sent_again_on_the_clock`,
  `tests/test_http_server.py::test_a_get_sent_at_the_lease_is_answered_once_its_syn_goes_again`,
  `tests/test_http_server.py::test_a_wait_on_a_syn_with_no_answer_yet_says_so`,
  `tests/test_http_server.py::test_a_syn_to_a_port_nobody_listens_on_is_refused_once_the_device_has_its_address`,
  `tests/test_net_client.py::test_a_syn_ack_slower_than_the_rto_meets_the_syn_sent_again_once`,
  `tests/test_arduino_shell.py::test_serves_a_get_sent_at_the_lease_once_its_syn_goes_again`
- **281.** `network.accepting(port)` connects at once and again 1 s after each refusal, on the
  board's clock whether a wait polls it or not, up to 30 connections; it returns the first
  connection the device accepts, open, and raises `ConnectionEnded` once a connection ended
  another way, the device reset or closed the accepted one before a poll saw it, or all 30 were
  refused, and `NotReachable` when a connection again finds the device
  out of reach. On `arduino_shell` with `WebServer` 200 ms after the address, a `GET` at the lease
  is refused, and `accepting(80)` is accepted after one refusal, with no wait for a console line.
  `tests/test_net_client.py::test_accepting_connects_again_a_second_after_each_refusal`,
  `tests/test_net_client.py::test_accepting_gives_up_after_30_refusals`,
  `tests/test_net_client.py::test_accepting_raises_for_a_lost_connection_and_an_unreachable_device`,
  `tests/test_net_client.py::test_accepting_refuses_a_connection_that_ended_before_it_was_seen`,
  `tests/test_arduino_shell.py::test_a_server_that_listens_after_its_address_is_waited_for_with_accepting`

## The build a test loaded

- **288.** The report section of every test that uses `board`, one that passed too, starts with
  the build the test loaded, `build: {directory} ({kind} for {chip}); app_sha256 {sha256}`: the
  build directory the `app_path` marker's `build_dir` selected, as an absolute path, not another
  `build/` beside it, `ESP-IDF` or `PlatformIO`, the chip it is built for and the SHA-256 of its
  app image, `none` when there is none; the user property has the same as `build`. On esp32sim
  the hash is of the image the runner read as the first `power_on` started it: not the one the
  directory held at setup, nor one written into it after; a test that never powers on reports
  the image at setup. On serial the `serial console` section starts with the same line, of the
  build `check_artifact` checked.
  `tests/test_pytest_plugin.py::test_the_report_names_the_build_the_test_loaded`,
  `tests/test_pytest_plugin.py::test_the_report_names_the_image_the_runner_read_at_power_on`,
  `tests/test_pytest_plugin.py::test_the_report_of_a_failing_test_shows_the_run_on_the_board_s_clock`,
  `tests/test_serial_backend.py::test_the_serial_report_names_the_build_the_test_loaded`,
  `tests/test_run_report.py::test_the_build_line_names_each_kind_as_its_tool_does`
