"""DNS and NTP against wire bytes, including malformed requests and NTP's era wrap."""

import struct
from datetime import UTC, datetime

from esp32tl.net.dns import Dns
from esp32tl.net.ntp import Ntp


def test_dns_answers_every_name_but_only_in_a_questions() -> None:
    dns = Dns("10.64.0.1")
    question = b"\x06broker\x07example\0\0\x01\0\x01"
    query = b"\x12\x34\x01\0\0\x01" + b"\0" * 6 + question
    assert dns.on_datagram(query) == (
        b"\x12\x34\x81\x80\0\x01\0\x01\0\0\0\0"
        + question
        + b"\xc0\x0c\0\x01\0\x01\0\0\0\x3c\0\x04\x0a\x40\0\x01"
    )
    assert dns.asked == ["broker.example"]
    aaaa = query[:-4] + b"\0\x1c\0\x01"
    answer = dns.on_datagram(aaaa)
    assert answer is not None and answer[6:8] == b"\0\0"
    for end in range(len(query)):
        assert dns.on_datagram(query[:end]) is None
    assert dns.on_datagram(query[:12] + b"\xc0\x0c\0\x01\0\x01") is None


def test_ntp_world_time_fraction_origin_and_era() -> None:
    world = [datetime(2036, 2, 7, 6, 28, 15, tzinfo=UTC)]
    now = [500_000_000]
    ntp = Ntp(lambda: now[0], lambda: world[0])
    query = b"\x23" + b"\0" * 39 + b"abcdefgh"
    answer = ntp.on_datagram(query)
    assert answer is not None and len(answer) == 48
    assert answer[24:32] == b"abcdefgh"
    assert answer[40:] == struct.pack(">II", 0xFFFFFFFF, 0x80000000)
    now[0] += 1_000_000_000
    answer = ntp.on_datagram(query)
    assert answer is not None and answer[40:] == struct.pack(">II", 0, 0x80000000)
    world[0] = datetime(1900, 1, 1, tzinfo=UTC)
    answer = ntp.on_datagram(query)
    assert answer is not None and answer[40:] == struct.pack(">II", 1, 0x80000000)
    assert ntp.on_datagram(query[:47]) is None
    assert ntp.on_datagram(b"\x24" + query[1:]) is None
