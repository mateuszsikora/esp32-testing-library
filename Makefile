PYTHON ?= python3
# Fixed: the pre-commit hooks run the tools from .venv.
override VENV := .venv
BIN := $(VENV)/bin

.PHONY: setup build-runner rom-elf test-runner firmware examples test determinism lint docs-check hooks clean

# Python environment, the runner and the ROM ELF. `make test` runs this first,
# so a clean checkout needs one command.
setup: $(VENV)/.installed build-runner rom-elf

# --clear: a venv left behind by a failed run (say, created with too old a
# Python) would otherwise keep its interpreter.
$(VENV)/.installed: pyproject.toml
	@$(PYTHON) -c 'import sys; sys.exit(sys.version_info < (3, 11))' || \
		{ echo "esp32tl needs Python 3.11 or newer; run with PYTHON=python3.11 (or newer)" >&2; exit 1; }
	$(PYTHON) -m venv --clear $(VENV)
	$(BIN)/python -m pip install --upgrade pip
	$(BIN)/python -m pip install -e '.[dev]'
	touch $@

# The mask ROM ELFs, release 20260528 of espressif/esp-rom-elfs (plan M0),
# from ~/.espressif when ESP-IDF has it, from GitHub otherwise. The script
# checks the checksum on every run, so this target stays phony.
rom-elf:
	scripts/fetch-rom-elf

# The runner crate (plan §4.2), against esp32sim pinned by revision in
# runner/Cargo.toml. cargo's own cache in runner/target/ is the cache: esp32sim
# is built again only when that revision changes. RUNNER_PROFILE=dev builds
# without fat LTO, for local iteration; the binary lands in target/debug/.
# Commands run in runner/, because rustup takes the toolchain from
# runner/rust-toolchain.toml only there, not through --manifest-path.
RUNNER_PROFILE ?= release
build-runner:
	cd runner && cargo build --locked --profile $(RUNNER_PROFILE)

# In the same profile as build-runner, so the tests reuse its build of
# esp32sim instead of making a second one (in release, the test binary still
# goes through fat LTO; that costs a link, not a rebuild).
test-runner:
	cd runner && cargo test --locked --profile $(RUNNER_PROFILE)

# Validation firmware, built in the pinned ESP-IDF container (plan M0) into
# firmware/<image>/build/, and the Arduino-ESP32 images in the pinned PlatformIO
# container into firmware/<image>/.pio/build/<env>/. IDF_LOCAL=1 and
# PIO_LOCAL=1 build with the ESP-IDF or the PlatformIO of the calling shell
# instead, for iteration; goldens come from the container builds only.
# An empty FIRMWARE_OUT is firmware/, whatever the calling shell exports:
# the tests read the images there.
firmware:
	FIRMWARE_OUT= firmware/build.sh
	FIRMWARE_OUT= firmware/build.sh --chips
	FIRMWARE_OUT= firmware/build-platformio.sh

# The images the tests read, built only when missing; build.sh writes
# esp32tl-built when a build finishes. The CI test jobs receive the images
# from the firmware job, so they build nothing (and the macOS ones have no
# Docker). `make firmware` rebuilds them.
FIRMWARE_IMAGES := hello_world console_repl nmea_reader gpio_echo sdspi sd_remount modem_at modem_recovery nvs_reader pppos_client mqtt_session http_request http_server persistence ota_state timer_wake reset_reasons deep_sleep bt_spp_initiator bt_spp_initiator_psram hello_world_8mb
firmware/%/build/esp32tl-built:
	FIRMWARE_OUT= firmware/build.sh $*

# The images of the other chips the runner emulates, each in
# firmware/<image>/build-<chip>/, built only when missing too.
CHIP_IMAGES := $(foreach chip,esp32s3 esp32c3 esp32c6,$(foreach image,hello_world console_repl gpio_echo nmea_reader nvs_reader http_request,firmware/$(image)/build-$(chip))) firmware/hello_world_psram/build-esp32s3 $(foreach chip,esp32s3 esp32c3,$(foreach image,gatt_server gatt_client gatt_security_server,firmware/$(image)/build-$(chip))) $(foreach chip,esp32s3 esp32c3 esp32c6,firmware/hello_world_8mb/build-$(chip)) $(foreach chip,esp32s3 esp32c3 esp32c6,firmware/console_repl_usb/build-$(chip)) $(foreach chip,esp32p4 esp32s31,$(foreach image,hello_world console_repl console_repl_usb gpio_echo nvs_reader hello_world_psram,firmware/$(image)/build-$(chip))) firmware/hello_world_rev1/build-esp32p4 $(foreach chip,esp32s3 esp32c3 esp32c6 esp32p4 esp32s31,firmware/reset_reasons/build-$(chip))
firmware/%/build-esp32s3/esp32tl-built:
	FIRMWARE_OUT= firmware/build.sh --chips $*@esp32s3
firmware/%/build-esp32c3/esp32tl-built:
	FIRMWARE_OUT= firmware/build.sh --chips $*@esp32c3
firmware/%/build-esp32c6/esp32tl-built:
	FIRMWARE_OUT= firmware/build.sh --chips $*@esp32c6
firmware/%/build-esp32p4/esp32tl-built:
	FIRMWARE_OUT= firmware/build.sh --chips $*@esp32p4
firmware/%/build-esp32s31/esp32tl-built:
	FIRMWARE_OUT= firmware/build.sh --chips $*@esp32s31

# The same for the PlatformIO images; build-platformio.sh writes esp32tl-built
# into the build directory of the image's environment, esp32dev unless named.
PLATFORMIO_IMAGES := firmware/arduino_shell/.pio/build/esp32dev firmware/arduino_shell/.pio/build/esp32s3_usb
firmware/%/.pio/build/esp32dev/esp32tl-built:
	FIRMWARE_OUT= firmware/build-platformio.sh $*
# After the project's esp32dev build, never beside it: the two share the project's .pio/ and its
# copy of the sources, so make -j must not run them at once.
firmware/%/.pio/build/esp32s3_usb/esp32tl-built: | firmware/%/.pio/build/esp32dev/esp32tl-built
	FIRMWARE_OUT= firmware/build-platformio.sh $*@esp32s3_usb

# Every examples/<name>/CMakeLists.txt is a project, built by the same pinned builder.
EXAMPLE_PROJECTS := $(patsubst examples/%/CMakeLists.txt,%,$(wildcard examples/*/CMakeLists.txt))
examples:
	FIRMWARE_OUT= firmware/build.sh --examples

examples/%/build/esp32tl-built:
	FIRMWARE_OUT= firmware/build.sh --examples $*

test: setup test-runner $(FIRMWARE_IMAGES:%=firmware/%/build/esp32tl-built) $(CHIP_IMAGES:%=%/esp32tl-built) $(PLATFORMIO_IMAGES:%=%/esp32tl-built) $(EXAMPLE_PROJECTS:%=examples/%/build/esp32tl-built)
	$(BIN)/pytest

# The determinism job (plan §6): tests/test_determinism.py run twice, and the
# two runs compared; the position tracker example brings TLS on the device.
# Not after `setup`: the runner and the ROM ELF are taken
# as they are, as in the CI job, which receives the runner from the runner job.
determinism: $(VENV)/.installed firmware/arduino_shell/.pio/build/esp32dev/esp32tl-built firmware/nmea_reader/build/esp32tl-built firmware/pppos_client/build/esp32tl-built firmware/mqtt_session/build/esp32tl-built firmware/http_request/build/esp32tl-built firmware/bt_spp_initiator/build/esp32tl-built examples/position_tracker/build/esp32tl-built
	$(BIN)/python scripts/check-determinism.py

lint: $(VENV)/.installed
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .
	$(BIN)/mypy
	cd runner && cargo fmt --check
	cd runner && cargo clippy --locked --all-targets -- -D warnings

# The documentation contract (docs/guides/documentation.md): the changes since
# the merge base with origin/main (else main), or with BASE, must update the
# documentation that owns them. System python3, not the venv: the script needs
# only the standard library, and CI runs it without `make setup`.
docs-check:
	python3 scripts/check-docs.py $(if $(BASE),--base $(BASE))

hooks: $(VENV)/.installed
	$(BIN)/pre-commit install

# Keeps runner/target/ and .cache/: the runner's build fetches esp32sim and
# the ROM ELFs come from the network.
clean:
	rm -rf $(VENV) build dist .mypy_cache .ruff_cache .pytest_cache
