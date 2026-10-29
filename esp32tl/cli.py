"""The `esp32tl` command. `run`, `record` and `replay` come with the harness (plan §3)."""

import argparse
from collections.abc import Sequence

from esp32tl import __version__


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="esp32tl", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.parse_args(argv)
    parser.print_help()
    return 0
