"""Backends: `esp32sim` (the runner, emulated clock) and `serial` (a real board, wall clock)."""

# The `embedded_services` each backend runs under, in the order tests are parametrised; None is no
# service, pytest-embedded's base `Dut`. `serial` (`esp,idf`) comes with milestone 4.
SERVICES: dict[str, str | None] = {"esp32sim": None}
