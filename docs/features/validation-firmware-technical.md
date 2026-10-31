# Validation firmware: how it works

`firmware/build.sh` builds the validation images; the `Makefile` targets `firmware` and
`firmware/%/build/esp32tl-built` call it. `tests/test_firmware_build.py` tests the script against
fakes, `tests/test_firmware_images.py` checks the images it built.

## Data flow

`firmware/build.sh [IMAGE...]` runs twice for a container build: once on the host, which starts
the container, and once inside it, which builds. With `IDF_LOCAL=1` it runs once, on the host.

1. **Image names.** Each argument must be in `IMAGES`, or the script dies before anything is
   written. With no arguments, the list is all of `IMAGES`.
2. **Mode**, decided before anything is written:
   - `ESP32TL_CONTAINER` set: the container side. `/.dockerenv`, which Docker creates in every
     container, must exist; without it the variable was left set on the host, and a build that
     took it for the container's would record a container it never ran in. `container` is the
     variable's value as a JSON string.
   - else `IDF_LOCAL=1`: a local build. `idf.py` must be on `PATH` and `IDF_PATH` set.
     `container` is `null`.
   - else the host side of a container build: `docker` must be on `PATH` and `docker info` must
     succeed.
3. **Output directory.** `FIRMWARE_OUT`, or the script's own directory (`firmware/`) when it is
   unset or empty, resolved before the script changes directory (so a relative `FIRMWARE_OUT` is
   the caller's), created, and made absolute with `pwd -P`. The script then changes to the
   repository root.
4. **Host side** (container build only): `exec docker run` with the image names, after the
   default above, as arguments:

   ```sh
   docker run --rm --init \
     --user "$(id -u):$(id -g)" \
     --env HOME=/tmp \
     --env ESP32TL_CONTAINER="$CONTAINER" \
     --env FIRMWARE_OUT=/out \
     --env ESP32TL_HOST_OUT="$out" \
     --volume "$PWD:/project:ro" \
     --volume "$out:/out" \
     --workdir /project \
     "$CONTAINER" firmware/build.sh "$@"
   ```

   - `--volume "$PWD:/project:ro"`: the repository read-only, so the build cannot write into it
     except through `/out`. `hello_world` writes nothing into its project directory. A project
     under `firmware/` that takes components from the component manager writes
     `dependencies.lock` and `managed_components/` into its own directory, and needs that
     directory made writable first.
   - `--volume "$out:/out"` with `FIRMWARE_OUT=/out`: the output directory, the only writable
     mount.
   - `--user "$(id -u):$(id -g)"`: the host's uid owns what the build writes. Under rootless
     Docker or Podman that uid maps to another one on the host, which is why they are not
     supported. Neither mount carries an SELinux label option, so in enforcing mode the container
     cannot read the repository.
   - `--env HOME=/tmp`: the host's uid is unknown to the image; this gives it a writable home.
   - `--env ESP32TL_CONTAINER`: the pinned image reference, which the container side records.
   - `--env ESP32TL_HOST_OUT`: the host's path of `/out`, for the messages.
   - `--rm`: the container is removed when it exits. `--init`: Docker runs its init process as
     PID 1, which forwards signals to the build and reaps its children. TODO(owner): build.sh
     does not say why it passes `--init`; confirm that signal forwarding is the reason.
5. **Build side** (in the container, or the local build):
   - `idf.py --version` gives the ESP-IDF version, the text after `ESP-IDF `. A local build says
     on stderr that it is not for goldens.
   - For each image: the project is `project_of IMAGE` (`hello_world` is
     `$IDF_PATH/examples/get-started/hello_world`), the build directory `$out/IMAGE/build`. If
     that directory exists and its `esp32tl-build.json` is not, byte for byte, the record this
     build would write (or is missing), the directory is removed with `rm -rf`: CMake refuses to
     reuse a build directory configured for another project directory. Then `esp32tl-built` is
     removed, `esp32tl-build.json` written, and

     ```sh
     idf.py -C "$project" -B "$build" -DSDKCONFIG="$build/sdkconfig" -DIDF_TARGET=esp32 build
     ```

     runs. `sdkconfig` goes in the build directory because the project directory may be
     ESP-IDF's own examples, or read-only. After a successful `idf.py`, the script checks that
     `flasher_args.json` exists, creates `esp32tl-built`, and prints the `built in` line.

The record is written before `idf.py` runs, so a failed compile keeps the build directory and the
next build is still incremental; the marker, removed first, is what says it finished.

The Makefile's `firmware` target runs `FIRMWARE_OUT= firmware/build.sh`; the pattern rule
`firmware/%/build/esp32tl-built` runs `FIRMWARE_OUT= firmware/build.sh $*`, and `test` depends on
`$(FIRMWARE_IMAGES:%=firmware/%/build/esp32tl-built)` with `FIRMWARE_IMAGES := hello_world`. The
rule has no prerequisites, so make rebuilds an image only when its marker is missing.

## Schema

**`esp32tl-build.json`**, in each build directory: one line of JSON and a newline, written with
`printf`, fields in this order:

```json
{"idf_version": "v5.3.6", "container": "espressif/idf:v5.3.6@sha256:cee52f0c4fdaf088992a7a739f5993154e95cf1d3ab61c24f5b40e6d56a3a070", "project": "<IDF_PATH>/examples/get-started/hello_world"}
```

| Field | Type | Value |
|---|---|---|
| `idf_version` | string | what `idf.py --version` printed after `ESP-IDF ` |
| `container` | string or `null` | the image reference `ESP32TL_CONTAINER` named, which the host side sets to `CONTAINER`; `null` for an `IDF_LOCAL=1` build |
| `project` | string | the absolute path of the ESP-IDF project, as seen where the build ran (inside the container, under the image's `IDF_PATH`) |

`idf_version` and `project` are escaped for `\` and `"` only; `container` is inserted as it is.
`build.sh` compares the file as text, so any change to the format counts as "built another way"
and rebuilds from scratch. `tests/test_firmware_images.py` reads it as JSON, `container` and
`idf_version` only.

**`esp32tl-built`**: an empty file, present only after a build finished: removed before `idf.py`
runs, created after it succeeded and wrote `flasher_args.json`. `flasher_args.json` cannot be the
marker, because CMake writes it while configuring, before anything is compiled: a failed or
interrupted build would count as built. Read by the Makefile pattern rule,
`tests/test_firmware_images.py` and `tests/test_pytest_plugin.py`.

Everything else in the build directory is ESP-IDF's (`flasher_args.json`, the ELF, the binaries,
`config/sdkconfig.json`, `project_description.json`). The CI `firmware` job packs the files at the
top of each build directory, `config/sdkconfig.json`, and the `.bin` and `.elf` files of
`bootloader/` and `partition_table/` ([CI](../cicd.md)).

## Interfaces

- `firmware/build.sh [IMAGE...]`: exits 0 when every image built, 1 with a message (below), or
  with the exit code of `docker run` or `idf.py` when those fail. Messages go to stderr, except
  the `was built another way` and `built in` lines, which go to stdout.
- `make firmware`, `make test`: [development guide](../guides/development.md).

## Configuration

| Name | Kind | Default | Effect |
|---|---|---|---|
| `IDF_VERSION` | script constant | `v5.3.6` | the ESP-IDF tag of the container |
| `CONTAINER` | script constant | `espressif/idf:$IDF_VERSION@sha256:cee52f0c4fdaf088992a7a739f5993154e95cf1d3ab61c24f5b40e6d56a3a070` | the image the build runs in, pinned by digest (a multi-arch index) |
| `IMAGES` | script constant | `hello_world` | the images; `project_of` maps each to its project |
| `FIRMWARE_OUT` | environment | `firmware/` | where `IMAGE/build/` goes; empty means the default; `/out` inside the container |
| `IDF_LOCAL` | environment | unset | `1` builds with the ESP-IDF of the calling shell |
| `ESP32TL_CONTAINER` | environment, set by the host side | unset | marks the container side and names the image it records |
| `ESP32TL_HOST_OUT` | environment, set by the host side | unset | the host's path of `/out`, shown in messages |
| `IDF_PATH` | environment | from `export.sh`, or the image | the ESP-IDF tree the projects come from |

`tests/test_firmware_images.py` reads `IDF_VERSION=` and `CONTAINER=` from `build.sh` with
regular expressions, so each has to stay on a line of its own. Changing either, or anything under
`firmware/`, changes the CI cache key of the firmware job.

## Failure modes

`build.sh` dies with `firmware/build.sh: MESSAGE` on stderr and exit 1:

| Message | Cause |
|---|---|
| `unknown image 'NAME'; the images are: hello_world` | an argument not in `IMAGES`; nothing written |
| `ESP32TL_CONTAINER is set outside a container; unset it` | `ESP32TL_CONTAINER` set and no `/.dockerenv`; nothing written |
| `IDF_LOCAL=1 needs ESP-IDF in the environment: source $IDF_PATH/export.sh first` | `IDF_LOCAL=1` without `idf.py` on `PATH` or without `IDF_PATH`; nothing written |
| `needs Docker, or IDF_LOCAL=1 to build with a local ESP-IDF` | no `docker` on `PATH`; nothing written |
| `the Docker daemon is not reachable ('docker info' failed); is Docker running?` | `docker info` failed; nothing written |
| `'idf.py --version' printed no ESP-IDF version` | no line starting `ESP-IDF ` in its output |
| `IMAGE: the build wrote no flasher_args.json` | `idf.py` succeeded but left no `flasher_args.json`; no marker |

A failing `idf.py` ends the script with its exit code (`set -e`), its own output, no marker and the
record kept. A failing `docker run` (a pull that fails, a mount refused) ends it with Docker's exit
code.

`tests/test_firmware_images.py` fails the test with:

- `DIR holds no finished build: run make firmware` when `esp32tl-built` is missing;
- `IMAGE was built in CONTAINER (ESP-IDF VERSION), not in PINNED (PINNED_VERSION): run make firmware`
  when the record names another container or version;
- `firmware/build.sh no longer sets IDF_VERSION= and CONTAINER= on lines of their own` when it
  cannot read the pin.

A local build passes with the `UserWarning` `IMAGE was built with a local ESP-IDF VERSION, not in
CONTAINER`.

## Tests

`tests/test_firmware_build.py` runs `build.sh` against a fake `docker` and a fake `idf.py` that
write their arguments to `$FAKE_LOG`, with a `PATH` holding only them and `cat`, `dirname`, `id`,
`mkdir`, `rm` and `sed`. It needs neither Docker nor ESP-IDF and takes milliseconds:

- the four early refusals (unknown image, no Docker, unreachable daemon, `IDF_LOCAL=1` without
  ESP-IDF) and `ESP32TL_CONTAINER` on the host (skipped when the suite itself runs in Docker):
  each message verbatim, exit 1, and no output directory created;
- `test_build_starts_the_pinned_container`: the `docker run` arguments, exactly;
- `test_local_build_runs_idf_py_and_records_how`: the `idf.py` arguments, the record with
  `"container": null`, the marker, the "not for goldens" warning and the `built in` line;
- reuse of a build directory with the same record, and removal of one with another record;
- no marker when `flasher_args.json` is missing, and a failed build removing the marker of an
  earlier one (exit code 2, the fake's).

The same file tests the `requires_docker` marker of `tests/conftest.py`, which skips a marked
test with `requires_docker: no docker on PATH`, `requires_docker: the Docker daemon is not
reachable ('docker info' failed)` or `requires_docker: 'docker info' did not answer in 30 s`, and
runs it when `docker info` succeeds. No test in `tests/` carries the marker at present.

`tests/test_firmware_images.py::test_hello_world_has_the_classic_esp32_flash_layout` reads the
real `hello_world` build through `IdfApp`, after the provenance check above: target `esp32`,
flash files `0x1000` `bootloader/bootloader.bin`, `0x8000` `partition_table/partition-table.bin`
and `0x10000` `hello_world.bin`, all present, and the ELF `hello_world.elf`.

Not covered: the build inside the real container (only `make firmware` and the CI `firmware` job
run it), the container side of the script (`ESP32TL_CONTAINER` set inside a container), the
`'idf.py --version'` failure, and the foreign-container branch of the provenance check, which only
a stale real build reaches. The timeout reason of `requires_docker` is untested.

## See also

- [validation-firmware-user.md](validation-firmware-user.md): building the images.
- [Firmware decisions](../decisions/firmware.md): why the build works this way.
- [CI](../cicd.md): the `firmware` job and its tarball.
- [Implementation plan, M0](../implementation-plan.md#m0-bootstrap-console-only) and
  [section 7](../implementation-plan.md#7-risks-and-what-we-do-about-them): the pinned ESP-IDF and
  goldens.
- [Stage 0 findings, Inputs](../stage0-findings.md#inputs): the ESP-IDF image and digest stage 0
  measured with.
