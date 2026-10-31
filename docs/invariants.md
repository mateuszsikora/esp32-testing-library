# Invariants

Rules about the domain that a test enforces: break one, and the named test fails. A rule that code
can break without any test failing is a note in the owning feature document, not an invariant
here. Numbers are never reused; a rule that stops holding is struck through, with the reason, so
that references to its number keep their meaning.

## Tests and backends

1. A test that uses `board` is parametrised with the backend's name as its id
   (`test_marked[esp32sim]`), and its `board`, `embedded_services` and `app_path` are set together;
   a test without `board` is not parametrised. With `esp32sim` the only backend, "once per backend"
   is not tested yet, nor is a test that uses `console` alone. `tests/test_pytest_plugin.py::test_board_is_parametrised_with_the_backend_its_services_and_the_app`
2. `--esp32tl-backend` takes only a backend's name: any other name is a usage error (exit code 4).
   That it limits the run to that backend is not tested while there is one backend.
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

## The esp32sim backend

7. The runner is given every file of the build's `flasher_args.json`, each at its offset.
   `tests/test_pytest_plugin.py::test_runner_command_flashes_every_file_at_its_offset`
8. The backend refuses a build that is missing, built for another chip than the classic ESP32,
   asks for flash encryption, or is unfinished.
   `tests/test_pytest_plugin.py::test_load_app_refuses_what_the_runner_cannot_run`
9. The release and file name the backend's ROM path is built from (`ROM_RELEASE`, `ROM_ELF`) are
   the ones `scripts/fetch-rom-elf` fetches.
   `tests/test_pytest_plugin.py::test_rom_is_the_one_make_setup_fetches`
10. Once the runner has exited, an expect that has not matched fails at once, with an `EOF` that
    names the exit code and the runner's last output: whole lines, less than `EOF_TAIL_BYTES`,
    without colour codes, and a long last line kept.
    `tests/test_pytest_plugin.py::test_expect_fails_at_once_when_the_runner_has_exited`,
    `tests/test_pytest_plugin.py::test_the_eof_carries_the_runner_s_last_whole_lines_without_colour`,
    `tests/test_pytest_plugin.py::test_the_eof_keeps_a_long_last_line`
11. A runner that exits with an error of its own is an error of the test, even when every expect
    passed, and its exit is reported once.
    `tests/test_pytest_plugin.py::test_a_runner_that_exits_with_an_error_fails_the_test`,
    `tests/test_pytest_plugin.py::test_a_runner_that_fails_to_start_is_reported_once`,
    `tests/test_pytest_plugin.py::test_an_eof_from_elsewhere_does_not_hide_the_runner_s_exit`

## The runner

12. For every `Stop` variant the test lists, the stop line's name is the variant's own name; a
    variant upstream adds fails the build instead, because `stop_name` has no catch-all arm.
    `runner/src/tests.rs::stop_names_are_the_variant_names`
13. The runner refuses, as a usage error, a command line without `--chip esp32`, without
    `--max-seconds` (a positive, finite number) or without `--flash-at`, and the flash and PSRAM
    sizes the classic ESP32 does not have (the test tries 0, 3, 32 and 2^44 MiB of flash and 2 MiB
    of PSRAM).
    `runner/src/cli/tests.rs::usage_errors`

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

## Documentation

17. A change to an owned path that leaves its documentation untouched fails `make docs-check`; a
    change to tests only, or a version-only bump, does not.
    `tests/test_check_docs.py::test_an_owned_path_without_docs_fails`,
    `tests/test_check_docs.py::test_tests_only_pass`,
    `tests/test_check_docs.py::test_version_only_bumps_pass`
