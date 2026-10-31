# The `esp32tl` command

Installing esp32tl puts an `esp32tl` command on the `PATH` (`[project.scripts]` in
`pyproject.toml`, `esp32tl = "esp32tl.cli:main"`; in this repository, `.venv/bin/esp32tl` after
`make setup`). It prints the installed version and its help, and does nothing else: tests run
through pytest ([pytest plugin](pytest-plugin-user.md)). There is no `python -m esp32tl`.

## Using it

```sh
esp32tl --version
```

prints `esp32tl <version>`, the version of the installed package (`esp32tl 0.1.0` now), and exits
0.

`esp32tl` with no arguments, `esp32tl -h` and `esp32tl --help` print the help and exit 0:

```
usage: esp32tl [-h] [--version]

Test unmodified ESP32 firmware through its wires.

options:
  -h, --help  show this help message and exit
  --version   show program's version number and exit
```

Any other argument is an error from argparse, with exit code 2:

```
usage: esp32tl [-h] [--version]
esp32tl: error: unrecognized arguments: foo
```

## See also

- [pytest plugin](pytest-plugin-user.md)
