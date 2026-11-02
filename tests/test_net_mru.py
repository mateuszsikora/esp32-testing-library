"""Negotiated PPP MRU constrains complete IP packets, across PPP and the transport layers."""

import struct
from collections.abc import Callable

import pytest
from test_net_ip import DST, SRC, check, packet, tcp
from test_net_ppp import control, frame, unframe

from esp32tl.net import Internet


def opened(mru: int | None) -> Internet:
    net = Internet(lambda: 0)
    net.start(0)
    net.poll(0)
    options = b"" if mru is None else b"\x01\x04" + struct.pack(">H", mru)
    replies = net.receive(0, control(0xC021, 1, 7, options))
    assert unframe(replies[-1][1]) == struct.pack(">BBH", 2, 7, 4 + len(options)) + options
    net.receive(0, control(0xC021, 2, 1))
    net.receive(0, control(0x8021, 2, 2, b"\x03\x06\x0a\x40\0\x01"))
    net.receive(0, control(0x8021, 1, 8, b"\x03\x06\x0a\x40\0\x02"))
    assert net.ppp.ipcp_open
    return net


@pytest.mark.parametrize(
    ("mru", "mss", "window"),
    [
        (576, 1400, 8192),
        (68, 1400, 8192),
        (128, 1400, 8192),
        (576, 128, 8192),
        (576, 1400, 80),
        (1500, 1400, 8192),
        (65535, 1400, 8192),
    ],
)
def test_tcp_respects_mru_mss_and_window_without_losing_bytes(
    mru: int, mss: int, window: int
) -> None:
    payload = bytes(range(250)) * 4

    class Service:
        def accept(
            self, peer: tuple[str, int], write: Callable[[bytes], None], close: Callable[[], None]
        ) -> "Service":
            write(payload)
            return self

        def on_data(self, data: bytes) -> None:
            pass

        def on_close(self) -> None:
            pass

    net = opened(mru)
    net.ip.tcp[1883] = Service()
    replies = net.receive(
        0, frame(0x21, tcp(1, 0, 2, window=window, options=struct.pack(">BBH", 2, 4, mss)))
    )
    assert all(len(unframe(data)) <= mru for _, data in replies)
    received = bytearray()
    for _ in range(100):
        replies = net.receive(0, frame(0x21, tcp(2, 65537 + len(received), 0x10, window=window)))
        if not replies:
            break
        for _, data in replies:
            ip = unframe(data)
            assert len(ip) <= mru
            assert check(ip[:20]) == 0
            assert check(DST + SRC + struct.pack(">BBH", 0, 6, len(ip) - 20) + ip[20:]) == 0
            assert int.from_bytes(ip[24:28], "big") == 65537 + len(received)
            assert len(ip[40:]) <= min(mss, window)
            received.extend(ip[40:])
    assert received == payload


@pytest.mark.parametrize("extra", [0, 1, 65536])
def test_udp_sends_at_mru_and_logs_oversized_replies(extra: int) -> None:
    net = opened(576)
    payload = b"a" * (576 - 28 + extra)

    class Service:
        def on_datagram(self, data: bytes) -> bytes:
            return payload

    net.ip.udp[53] = Service()
    replies = net.receive(0, frame(0x21, packet(17, struct.pack(">HHHH", 4000, 53, 8, 0))))
    if extra:
        assert replies == []
        assert net.events[-1]["event"] == (
            f"net: dropped UDP reply of {576 + extra} bytes: "
            "exceeds peer MRU 576 (fragmentation unsupported)"
        )
    else:
        assert len(replies) == 1
        ip = unframe(replies[0][1])
        assert len(ip) == 576 and ip[28:] == payload


def test_mru_resets_on_renegotiation_termination_and_redial() -> None:
    net = opened(576)
    assert net.ppp.peer_mru == 576
    net.receive(0, control(0xC021, 1, 20))  # MRU omitted: back to default
    assert net.ppp.peer_mru == 1500 and not net.ppp.ipcp_open
    net = opened(576)
    net.receive(0, control(0xC021, 5, 20))
    assert net.ppp.peer_mru == 1500
    net = opened(576)
    net.stop()
    net.start(0)
    assert net.ppp.peer_mru == 1500
    assert opened(None).ppp.peer_mru == 1500


@pytest.mark.parametrize("mru", [0, 40, 67])
def test_mru_too_small_for_this_stack_gets_a_nak_not_an_ack(mru: int) -> None:
    net = Internet(lambda: 0)
    net.start(0)
    net.poll(0)
    replies = net.receive(0, control(0xC021, 1, 7, b"\x01\x04" + struct.pack(">H", mru)))
    assert unframe(replies[-1][1]) == b"\x03\x07\0\x08\x01\x04\0\x44"
    net.receive(0, control(0xC021, 2, 1))
    assert not net.ppp.lcp_open and net.ppp.peer_mru == 1500
    net.receive(0, control(0xC021, 1, 8, b"\x01\x04\0\x44"))
    assert net.ppp.lcp_open and net.ppp.peer_mru == 68


def test_ppp_does_not_emit_an_oversized_ip_packet_even_when_called_directly() -> None:
    net = opened(576)
    net.ppp.send_ip(b"x" * 577)
    assert net.poll(0) == []
    assert net.events[-1]["event"] == (
        "ppp: dropped IPv4 packet of 577 bytes: exceeds peer MRU 576"
    )
