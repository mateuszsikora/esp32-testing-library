"""A real ESP-IDF firmware reads the NVS values supplied by the test before boot."""

import pytest

from esp32tl.backends.esp32sim import BackendError, Esp32Sim
from esp32tl.flash import i8, i16, i32, i64, u8, u16, u32, u64
from esp32tl.interfaces.console import Console

pytestmark = pytest.mark.app_path("../firmware/nvs_reader")


@pytest.mark.parametrize("host,port", [("broker.test", 8883), ('café,"quoted"', 65535)])
def test_firmware_reads_typed_values(
    board: Esp32Sim, console: Console, host: str, port: int
) -> None:
    blob = b"\x00\xff\r\nCA\x80"
    board.nvs(
        {
            "config": {
                "broker_host": host,
                "broker_port": u16(port),
                "broker_ca": blob,
                "enabled": u8(255),
                "count": u32(4294967295),
                "delta": i32(-2147483648),
                "small": i8(-128),
                "medium": i16(-32768),
                "total": u64(18446744073709551615),
                "offset": i64(-9223372036854775808),
            }
        }
    )
    board.power_on()
    console.expect_exact(
        f"nvs_reader: host={host} port={port} enabled=255 count=4294967295 delta=-2147483648"
    )
    console.expect_exact(
        "nvs_reader: small=-128 medium=-32768 total=18446744073709551615"
        " offset=-9223372036854775808"
    )
    console.expect_exact(f"nvs_reader: blob={blob.hex()}")
    with pytest.raises(BackendError, match=r"write the flash before board.power_on\(\)"):
        board.nvs({})
