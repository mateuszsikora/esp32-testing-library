#!/bin/sh
# Builds the PlatformIO validation images, Arduino-ESP32 sketches, each into
# $FIRMWARE_OUT/<image>/.pio/build/<env>/, the build directory `pio run` leaves and esp32tl
# loads as it is. FIRMWARE_OUT is firmware/ by default. An image is a project and one of the
# environments of its platformio.ini: IMAGE alone is its esp32dev environment, for the classic
# ESP32, and IMAGE@ENV its environment ENV.
#
#   firmware/build-platformio.sh [IMAGE[@ENV]...]     all of them when none is named
#
# The build runs in a Python container pinned by digest, with pioarduino's PlatformIO Core pinned
# by version below and pioarduino's espressif32 platform pinned by its release URL in each
# project's platformio.ini. PlatformIO's core directory, what is ~/.platformio elsewhere, is
# .cache/platformio/ of the repository, mounted as /platformio: the core, the platform and the
# toolchains it downloads stay there between builds. PIO_LOCAL=1 builds with the `pio` of the
# calling shell and its own core directory instead, for iteration; goldens come from the
# container build only. Every build directory records how it was built in esp32tl-build.json,
# with "container": null for a local build.
#
# Builds are incremental. An image's build, .pio/build/<env>/, is removed first when its record is
# missing or, compared as text, differs from the record of the build about to start: another
# PlatformIO Core, platform, container or project directory. Before an environment's first build,
# the records of the project's other environments are compared instead. With it go the other
# environments' builds whose records differ too and what they share outside build/; another
# environment's build with the same record stays. PlatformIO itself cleans the build when
# platformio.ini changes, removing the record with it, so the record is written again once the
# build finishes. esp32tl-built marks a build that finished.
set -eu

PIOARDUINO_VERSION=6.2.0
CONTAINER=python:3.13-bookworm@sha256:cc55e4ded9c95f6ce2133b3d096db43f214d280c2a8af65d83e246c8fa7d168c
IMAGES="arduino_shell arduino_shell@esp32s3_usb"
# The environment of an image named without @ENV.
ENV=esp32dev

die() {
	echo "firmware/build-platformio.sh: $*" >&2
	exit 1
}

for image in "$@"; do
	case " $IMAGES " in
	*" $image "*) ;;
	*) die "unknown image '$image'; the images are: $IMAGES" ;;
	esac
done
# Intentional splitting: image names contain neither whitespace nor glob characters.
# shellcheck disable=SC2086
[ $# -gt 0 ] || set -- $IMAGES

# What the build needs is checked before anything is written. Docker creates /.dockerenv in every
# container; without it, ESP32TL_CONTAINER was left set on the host.
if [ -n "${ESP32TL_CONTAINER:-}" ]; then
	[ -f /.dockerenv ] || die "ESP32TL_CONTAINER is set outside a container; unset it"
	container="\"$ESP32TL_CONTAINER\""
	pio=$PLATFORMIO_CORE_DIR/penv/bin/pio
elif [ "${PIO_LOCAL:-}" = 1 ]; then
	command -v pio >/dev/null 2>&1 ||
		die "PIO_LOCAL=1 needs PlatformIO's pio on PATH"
	container=null
	pio=pio
else
	command -v docker >/dev/null 2>&1 ||
		die "needs Docker, or PIO_LOCAL=1 to build with a local PlatformIO"
	docker info >/dev/null 2>&1 ||
		die "the Docker daemon is not reachable ('docker info' failed); is Docker running?"
	container=
fi

# Before changing directory, so that a relative FIRMWARE_OUT is the caller's.
out=${FIRMWARE_OUT:-$(dirname "$0")}
mkdir -p "$out"
out=$(cd "$out" && pwd -P)
cd "$(dirname "$0")/.."

# Whether pio runs in the project itself, which it can only when the output directory is
# firmware/. The host decides, comparing two paths of its own by name, and tells the container: in
# there /out and /project are two mounts of one directory, and a copy of the project onto itself
# would remove its sources.
if [ -z "$container" ] || [ "$container" = null ]; then
	if [ "$out" = "$(pwd -P)/firmware" ]; then in_place=1; else in_place=0; fi
else
	in_place=${ESP32TL_IN_PLACE:-}
	case $in_place in
	0 | 1) ;;
	*) die "ESP32TL_IN_PLACE is '$in_place', not 0 or 1; run the script on the host" ;;
	esac
fi

# The host side: start the container, which runs this script again with ESP32TL_CONTAINER naming
# its image and ESP32TL_HOST_OUT the host's path of /out, for the messages. As in build.sh, the
# repository is read-only, the host's uid owns what the build writes and HOME=/tmp gives that uid
# a writable home.
if [ -z "$container" ]; then
	mkdir -p .cache/platformio
	exec docker run --rm --init \
		--user "$(id -u):$(id -g)" \
		--env HOME=/tmp \
		--env PLATFORMIO_CORE_DIR=/platformio \
		--env ESP32TL_CONTAINER="$CONTAINER" \
		--env FIRMWARE_OUT=/out \
		--env ESP32TL_HOST_OUT="$out" \
		--env ESP32TL_IN_PLACE="$in_place" \
		--volume "$PWD:/project:ro" \
		--volume "$out:/out" \
		--volume "$PWD/.cache/platformio:/platformio" \
		--workdir /project \
		"$CONTAINER" firmware/build-platformio.sh "$@"
fi

# In the container, PlatformIO Core goes into the core directory's penv/, where PlatformIO's own
# installer puts it and where the platform installs its Python packages beside it. A penv with
# another version is made again.
if [ "$container" != null ]; then
	if [ "$("$pio" --version 2>/dev/null)" != "PlatformIO Core, version $PIOARDUINO_VERSION" ]; then
		echo "firmware/build-platformio.sh: installing pioarduino $PIOARDUINO_VERSION in $PLATFORMIO_CORE_DIR/penv"
		rm -rf "$PLATFORMIO_CORE_DIR/penv"
		python3 -m venv "$PLATFORMIO_CORE_DIR/penv"
		"$PLATFORMIO_CORE_DIR/penv/bin/pip" install --quiet --disable-pip-version-check \
			"pioarduino==$PIOARDUINO_VERSION"
	fi
fi

pio_version=$("$pio" --version | sed -n 's/^PlatformIO Core, version //p')
[ -n "$pio_version" ] || die "'pio --version' printed no PlatformIO Core version"
if [ "$container" = null ]; then
	echo "firmware/build-platformio.sh: PIO_LOCAL=1, PlatformIO Core $pio_version; not for goldens, which come from the container (pioarduino $PIOARDUINO_VERSION)" >&2
fi

json_string() {
	printf '"%s"' "$(printf '%s' "$1" | sed 's/[\\"]/\\&/g')"
}

for entry in "$@"; do
	image=${entry%@*}
	env=$ENV
	[ "$entry" = "$image" ] || env=${entry#*@}
	project=$PWD/firmware/$image
	# The first `platform =` line: in arduino_shell the one of its [env] section, which every
	# environment inherits.
	platform=$(sed -n '/^platform *= */{s///p;q;}' "$project/platformio.ini")
	[ -n "$platform" ] || die "$image: $project/platformio.ini sets no platform"
	# pio runs only in a writable project directory, and the repository is read-only in the
	# container: it runs in $out/<image>/, which is the project itself through the writable /out
	# when FIRMWARE_OUT is firmware/ (in_place), and a copy of its platformio.ini and src/ otherwise.
	directory=$out/$image
	if [ "$in_place" = 0 ]; then
		mkdir -p "$directory"
		rm -rf "$directory/src"
		cp -p "$project/platformio.ini" "$directory/"
		cp -Rp "$project/src" "$directory/"
	fi
	workspace=$directory/.pio
	build=$workspace/build/$env
	shown=${ESP32TL_HOST_OUT:-$out}/$image/.pio/build/$env
	provenance=$(printf '{"platformio": %s, "platform": %s, "container": %s, "project": %s}' \
		"$(json_string "$pio_version")" "$(json_string "$platform")" "$container" \
		"$(json_string "$project")")
	# This environment's record, or, before its first build, those of the project's other
	# environments: a .pio/ without any was made by something else.
	stale=
	if [ -d "$build" ]; then
		[ "$(cat "$build/esp32tl-build.json" 2>/dev/null)" = "$provenance" ] || stale=1
	elif [ -d "$workspace" ]; then
		for record in "$workspace"/build/*/esp32tl-build.json; do
			[ -f "$record" ] && [ "$(cat "$record")" = "$provenance" ] || stale=1
		done
	fi
	# What another way left goes: this environment's build, every other environment's build whose
	# record is not this one, and what the environments share outside build/. Another
	# environment's build made this way stays, and so does build/project.checksum, without which
	# pio would remove every build.
	if [ -n "$stale" ]; then
		echo "firmware/build-platformio.sh: $entry: $shown was built another way, removing it"
		for path in "$workspace"/* "$workspace"/.[!.]*; do
			[ -e "$path" ] || continue
			if [ "$path" != "$workspace/build" ]; then
				rm -rf "$path"
				continue
			fi
			for sibling in "$workspace"/build/*/; do
				sibling=${sibling%/}
				[ -d "$sibling" ] || continue
				if [ "$sibling" != "$build" ] &&
					[ "$(cat "$sibling/esp32tl-build.json" 2>/dev/null)" = "$provenance" ]; then
					continue
				fi
				rm -rf "$sibling"
			done
		done
	fi
	mkdir -p "$build"
	rm -f "$build/esp32tl-built"
	printf '%s\n' "$provenance" >"$build/esp32tl-build.json"
	"$pio" run --project-dir "$directory" --environment "$env"
	[ -f "$build/firmware.bin" ] || die "$entry: the build wrote no firmware.bin"
	printf '%s\n' "$provenance" >"$build/esp32tl-build.json"
	: >"$build/esp32tl-built"
	echo "firmware/build-platformio.sh: $entry built in $shown (PlatformIO Core $pio_version)"
done
