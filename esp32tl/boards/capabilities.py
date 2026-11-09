"""What each backend can put at the far end of an interface, and what it can tap (plan §4.5).

The declaration is `capabilities.toml`, beside the profiles: per backend, per interface kind
(`console`, `uart`, `lines`, `sd_card`, `wifi`, `bluetooth`), the sources it takes (`model`,
`replay`, `real`) and whether it can tap them. A test asks for sources and taps through
`Request`s; `check` refuses what no backend can give, so that a misspelt request fails instead of
skipping everywhere, and `refusal` says why one backend cannot give the rest.
"""

import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from types import MappingProxyType

from esp32tl.boards import PROFILES, BoardProfile

DECLARATION = PROFILES / "capabilities.toml"

# The words a declaration may use: the kinds of interface a profile has, and the sources of the
# specification.
KINDS = ("console", "uart", "lines", "sd_card", "wifi", "bluetooth")
SOURCES = ("model", "replay", "real")


class CapabilityError(Exception):
    """A declaration that cannot be loaded, or a request that no backend can give."""


@dataclass(frozen=True)
class Capability:
    """What one backend can do with one kind of interface."""

    sources: tuple[str, ...] = ()
    tap: bool = False


# Backend name, then interface kind.
Capabilities = Mapping[str, Mapping[str, Capability]]


@dataclass(frozen=True)
class Request:
    """A test's use of one interface of its board: a source at its far end, or a tap on it."""

    interface: str
    source: str | None = None
    tap: bool = False


def kind(interface: str) -> str:
    """`uart` for `uart.gnss`, `console` for `console`, `sd_card` for `sd_card`."""
    return interface.partition(".")[0]


@cache
def declared() -> Capabilities:
    """The declaration esp32tl ships."""
    return load(DECLARATION)


def load(path: Path) -> Capabilities:
    try:
        with path.open("rb") as file:
            data = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise CapabilityError(f"{path.name}: {error}") from None
    backends = {}
    for backend, kinds in data.items():
        if not isinstance(kinds, dict):
            raise CapabilityError(f"{path.name}: {backend}: is not a table")
        for name in kinds:
            if name not in KINDS:
                raise CapabilityError(
                    f"{path.name}: {backend}.{name}: no such kind of interface;"
                    f" the kinds are {', '.join(KINDS)}"
                )
        backends[backend] = MappingProxyType(
            {name: capability(f"{path.name}: {backend}.{name}", v) for name, v in kinds.items()}
        )
    return MappingProxyType(backends)


def capability(where: str, value: object) -> Capability:
    if not isinstance(value, dict):
        raise CapabilityError(f"{where}: is not a table")
    unknown = sorted(value.keys() - {"sources", "tap"})
    if unknown:
        raise CapabilityError(f"{where}: unknown key {', '.join(unknown)}")
    sources = value.get("sources", [])
    if not isinstance(sources, list) or not all(isinstance(s, str) for s in sources):
        raise CapabilityError(f"{where}.sources: is not a list of names")
    for source in sources:
        if source not in SOURCES:
            raise CapabilityError(f"{where}.sources: {source!r} is not one of {', '.join(SOURCES)}")
    tap = value.get("tap", False)
    if not isinstance(tap, bool):
        raise CapabilityError(f"{where}.tap: is not true or false")
    return Capability(tuple(sources), tap)


def check(requests: Iterable[Request], profile: BoardProfile, capabilities: Capabilities) -> None:
    """Raises CapabilityError for a request the profile has no interface for, or that no
    backend can give."""
    for request in requests:
        interface = request.interface
        if interface not in profile.interfaces:
            raise CapabilityError(
                f"the board profile {profile.name} has no {interface};"
                f" its interfaces are {', '.join(profile.interfaces)}"
            )
        anywhere = [kinds.get(kind(interface), Capability()) for kinds in capabilities.values()]
        if request.source is not None:
            sources = list(dict.fromkeys(s for c in anywhere for s in c.sources))
            if not sources:
                raise CapabilityError(f"no backend takes a source on {interface}")
            if request.source not in sources:
                raise CapabilityError(
                    f"no backend takes a {request.source!r} source on {interface};"
                    f" the sources there are {', '.join(sources)}"
                )
        if request.tap and not any(c.tap for c in anywhere):
            raise CapabilityError(f"no backend can tap {interface}")


def refusal(backend: str, requests: Iterable[Request], capabilities: Capabilities) -> str | None:
    """Why `backend` cannot give the first request it cannot give, or None when it can give
    them all."""
    kinds = capabilities[backend]
    for request in requests:
        interface = request.interface
        can = kinds.get(kind(interface), Capability())
        if request.source is not None and request.source not in can.sources:
            only = f", only {', '.join(can.sources)}" if can.sources else ""
            return f"{interface} takes no {request.source} source on this backend{only}"
        if request.tap and not can.tap:
            return f"{interface} cannot be tapped on this backend"
    return None
