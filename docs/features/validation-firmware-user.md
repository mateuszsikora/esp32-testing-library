# Validation firmware

The validation images are the firmware the test suite runs: ESP-IDF projects built for the classic
ESP32, unmodified, in ESP-IDF's own container at a pinned version, so that every machine runs the
same binaries. A test names one with `pytest.mark.app_path("../firmware/hello_world")`
([pytest plugin](pytest-plugin-user.md)).

## Build the images

```sh
make firmware                    # every image, into firmware/<image>/build/
firmware/build.sh                # the same
firmware/build.sh hello_world    # only the images named
```

`make test` builds the images that are missing or whose last build did not finish, so the first
`make test` of a checkout needs Docker too, and leaves the others as they are; `make firmware`
builds them all again ([development guide](../guides/development.md)). Builds are incremental: a build directory made
the same way as before is built on, not from scratch.

`FIRMWARE_OUT=DIR firmware/build.sh` puts each image in `DIR/<image>/build/` instead, `DIR`
relative to the directory you run it from. `make firmware` and `make test` always use `firmware/`,
whatever `FIRMWARE_OUT` the shell exports, because the tests read the images there.

## The images

| Image | Built from |
|---|---|
| `hello_world` | ESP-IDF's `examples/get-started/hello_world`, unmodified, for target `esp32` |

## What a build leaves

`firmware/<image>/build/` holds an ESP-IDF build directory, the layout pytest-embedded's `IdfApp`
reads: the ELF, the binaries, `config/sdkconfig.json`, and `flasher_args.json`, which gives each
binary's flash offset and from which the backend builds the runner's `--flash-at` arguments. For
`hello_world` those are:

| Offset | File |
|---|---|
| `0x1000` | `bootloader/bootloader.bin` |
| `0x8000` | `partition_table/partition-table.bin` |
| `0x10000` | `hello_world.bin` |

and the ELF is `hello_world.elf`. Two more files say how the build was made:
`esp32tl-build.json` (the ESP-IDF version, the container, and the project it was built from) and
`esp32tl-built`, present only when the build finished. `firmware/**/build/` is ignored by git.

## What it needs: Docker

The build runs in `espressif/idf:v5.3.6`, pinned by digest. It needs Docker Desktop or a rootful
Docker Engine whose daemon answers `docker info`:

- Under rootless Docker or Podman, the files the build writes belong to another uid.
- With SELinux in enforcing mode, the container cannot read the repository.

Neither is supported. The first build pulls the image, 7 GB once unpacked; on a machine that has
it, building `hello_world` from scratch takes a couple of minutes.

## Build with a local ESP-IDF

To iterate without the container, source the `export.sh` of a local ESP-IDF, then:

```sh
IDF_LOCAL=1 firmware/build.sh
```

That ESP-IDF need not be v5.3.6, so it is not for goldens: those come from the container build
only. The build says so on stderr, with the version `idf.py --version` reports and `$IDF_PATH`:

```text
firmware/build.sh: IDF_LOCAL=1, ESP-IDF VERSION at IDF_PATH; not for goldens, which come from the container (v5.3.6)
```

`esp32tl-build.json` then records `"container": null`. Switching between a local and a container
build, or to another ESP-IDF version or install, removes the build directory and builds from
scratch. These two lines go to stdout, `DIR` being the absolute path of the build directory:

```text
firmware/build.sh: hello_world: DIR was built another way, removing it
firmware/build.sh: hello_world built in DIR (ESP-IDF VERSION)
```

The second one ends every finished build.

## What the tests accept

`tests/test_firmware_images.py` reads `firmware/hello_world/build/` and:

- passes on a build from the pinned container;
- passes on a local build (`IDF_LOCAL=1`), with the warning
  `hello_world was built with a local ESP-IDF VERSION, not in CONTAINER`;
- fails on a build from another container or ESP-IDF version, such as one made before the pin
  changed;
- fails when there is no finished build.

The two failures read:

```text
hello_world was built in CONTAINER (ESP-IDF VERSION), not in PINNED (PINNED_VERSION): run make firmware
DIR holds no finished build: run make firmware
```

`make test` does not rebuild an image that is finished but stale, so after a change of the pin
run `make firmware`.

## When the build stops

`firmware/build.sh` exits 1 with one of these on stderr, before writing anything, or with the exit
code of `idf.py` when the build itself fails:

- `firmware/build.sh: unknown image 'NAME'; the images are: hello_world`
- `firmware/build.sh: needs Docker, or IDF_LOCAL=1 to build with a local ESP-IDF`
- `firmware/build.sh: the Docker daemon is not reachable ('docker info' failed); is Docker running?`
- `firmware/build.sh: IDF_LOCAL=1 needs ESP-IDF in the environment: source $IDF_PATH/export.sh first`

The rest are in [Failure modes](validation-firmware-technical.md#failure-modes).

## See also

- [validation-firmware-technical.md](validation-firmware-technical.md): the container command, the
  provenance file and every message.
- [runner-user.md](runner-user.md#run-an-image): running an image by hand.
- [CI](../cicd.md): the `firmware` job, which builds the images once and hands them to the tests.
- [Firmware decisions](../decisions/firmware.md): why the build works this way.
