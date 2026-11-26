"""PPP against independently framed control packets, including corrupt and split frames."""

import struct

import pytest

from esp32tl.net.ppp import Ppp, fcs16


def frame(protocol: int, payload: bytes, *, compressed: bool = False) -> bytes:
    body = (
        bytes([protocol]) if compressed else b"\xff\x03" + struct.pack(">H", protocol)
    ) + payload
    # Bitwise reference implementation, deliberately independent of the table in the peer.
    crc = 0xFFFF
    for byte in body:
        crc ^= byte
        for _ in range(8):
            crc = crc >> 1 ^ (0x8408 if crc & 1 else 0)
    body += struct.pack("<H", crc ^ 0xFFFF)
    return (
        b"\x7e"
        + b"".join(bytes([125, b ^ 32]) if b < 32 or b in (125, 126) else bytes([b]) for b in body)
        + b"\x7e"
    )


def unframe(data: bytes) -> bytes:
    out = bytearray()
    escaped = False
    for byte in data[1:-1]:
        if escaped:
            out.append(byte ^ 32)
            escaped = False
        elif byte == 125:
            escaped = True
        else:
            out.append(byte)
    assert fcs16(bytes(out)) == 0xF0B8
    return bytes(out[4:-2])


def control(protocol: int, code: int, ident: int, data: bytes = b"") -> bytes:
    return frame(protocol, struct.pack(">BBH", code, ident, 4 + len(data)) + data)


def opened() -> tuple[Ppp, list[bytes], list[bytes]]:
    sent: list[bytes] = []
    received: list[bytes] = []
    ppp = Ppp(sent.append, received.append)
    ppp.receive(control(0xC021, 1, 7))
    assert unframe(sent[0]) == b"\x01\x01\x00\x04"
    assert unframe(sent[1]) == b"\x02\x07\x00\x04"
    ppp.receive(control(0xC021, 2, 1))
    assert ppp.lcp_open
    options = b"\x03\x06\x0a\x40\x00\x01"
    assert unframe(sent[-1]) == b"\x01\x02\x00\x0a" + options
    ppp.receive(control(0x8021, 2, 2, options))
    ppp.receive(control(0x8021, 1, 8, b"\x03\x06\0\0\0\0\x81\x06\0\0\0\0"))
    assert unframe(sent[-1]) == b"\x03\x08\x00\x10\x03\x06\x0a\x40\0\x02\x81\x06\x0a\x40\0\x01"
    ppp.receive(control(0x8021, 1, 9, b"\x03\x06\x0a\x40\0\x02\x81\x06\x0a\x40\0\x01"))
    assert ppp.ipcp_open
    return ppp, sent, received


def test_fcs_reference_and_compressed_frames_at_every_split() -> None:
    assert fcs16(b"123456789") == 0x6F91
    for split in range(25):
        ppp, sent, received = opened()
        packet = frame(0x21, b"a\x7e\x7d\0+++z", compressed=True)
        ppp.receive(packet[:split])
        ppp.receive(packet[split:])
        assert received == [b"a\x7e\x7d\0+++z"]
        ppp.send_ip(b"reply")
        assert unframe(sent[-1]) == b"reply"


def test_ack_must_match_request_and_ipcp_waits_for_lcp() -> None:
    ppp = Ppp(lambda b: None, lambda b: None)
    ppp.receive(control(0x8021, 1, 1))
    assert not ppp.ipcp_open
    ppp.receive(control(0xC021, 1, 1))
    ppp.receive(control(0xC021, 2, 55))
    ppp.receive(control(0xC021, 2, 1, b"\x07\x02"))
    assert not ppp.lcp_open
    ppp.receive(control(0xC021, 2, 1))
    assert ppp.lcp_open


@pytest.mark.parametrize("options", [b"\x03\x04\xc0\x23", b"\x63\x02"])
def test_authentication_and_unknown_options_are_rejected(options: bytes) -> None:
    sent: list[bytes] = []
    ppp = Ppp(sent.append, lambda b: None)
    ppp.receive(control(0xC021, 1, 12, options))
    assert unframe(sent[-1]) == bytes([4, 12, 0, 4 + len(options)]) + options
    assert not ppp.lcp_open


def test_echo_terminate_and_corrupt_frames() -> None:
    ppp, sent, received = opened()
    ppp.receive(control(0xC021, 9, 20, b"1234data"))
    assert unframe(sent[-1]) == b"\x0a\x14\0\x0c\0\0\0\0data"
    ppp.receive(b"\x7eBAD\x7e")
    ppp.receive(frame(0x21, b"ignored")[:-1] + b"\x7d\x7e")
    ppp.receive(control(0xC021, 1, 20, b"\x01\0"))
    assert received == []
    ppp.receive(control(0xC021, 5, 21))
    assert unframe(sent[-1]) == b"\x06\x15\0\x04"
    assert not ppp.lcp_open and not ppp.ipcp_open
    ppp.receive(frame(0x21, b"ignored"))
    assert received == []


def test_renegotiation_and_termination_invalidate_old_acknowledgments() -> None:
    ppp, sent, received = opened()
    ppp.receive(control(0xC021, 5, 50))
    ppp.receive(control(0xC021, 2, 1))  # stale ACK from the previous link
    ppp.receive(control(0xC021, 1, 51))
    assert not ppp.lcp_open
    request = unframe(sent[-2])
    ppp.receive(control(0xC021, 2, request[1], request[4:]))
    assert ppp.lcp_open and not ppp.ipcp_open
    ppp.receive(frame(0x21, b"not yet"))
    assert received == []
    ppp, _, _ = opened()
    ppp.receive(control(0xC021, 1, 52))
    assert not ppp.ipcp_open and not ppp.lcp_open


def test_the_far_end_offers_to_terminate_an_open_link_only() -> None:
    ppp, sent, _ = opened()
    assert ppp.lcp_open
    sent.clear()
    ppp.terminate()
    assert len(sent) == 1
    assert sent[0].startswith(b"\x7e")
    # LCP, Terminate-Request (RFC 1661, section 5.5), no data
    body = unframe(sent[0])
    assert body[0] == 5 and struct.unpack(">H", body[2:4])[0] == 4 and len(body) == 4
    # and it waits for the device: it still counts as open until it answers
    assert ppp.lcp_open
    closed = Ppp(sent.append, lambda datagram: None)
    sent.clear()
    closed.terminate()
    assert sent == []


def test_a_far_end_refusing_lcp_answers_no_configure_request_and_ends_at_the_terminate() -> None:
    sent: list[bytes] = []
    logged: list[str] = []
    ppp = Ppp(sent.append, lambda datagram: None, refuse="lcp", log=logged.append)
    ppp.start()
    assert sent == []  # the far end asks nothing either
    for ident in (1, 2, 3):
        ppp.receive(control(0xC021, 1, ident, b"\x01\x04\x05\xdc"))
    assert sent == [] and not ppp.lcp_open and not ppp.negotiation_failed
    assert logged == ["ppp: refusing LCP: the device's Configure-Requests go unanswered"]
    # the device gives up: its Terminate-Request is acknowledged, and the negotiation is over
    ppp.receive(control(0xC021, 5, 4))
    assert [unframe(data) for data in sent] == [b"\x06\x04\x00\x04"]
    assert ppp.negotiation_failed and not ppp.lcp_open


def test_a_far_end_refusing_ipcp_opens_lcp_and_ends_it_at_the_first_ipcp_request() -> None:
    sent: list[bytes] = []
    ended: list[str] = []
    ppp = Ppp(sent.append, lambda datagram: None, refuse="ipcp", ended=ended.append)
    ppp.start()
    assert unframe(sent[0]) == b"\x01\x01\x00\x04"
    ppp.receive(control(0xC021, 1, 7))
    ppp.receive(control(0xC021, 2, 1))
    assert ppp.lcp_open
    # no IPCP Configure-Request of the far end's: the last frame is the device's Configure-Ack
    assert [unframe(data)[0] for data in sent] == [1, 2]
    sent.clear()
    ppp.receive(control(0x8021, 1, 8, b"\x03\x06\0\0\0\0"))
    [terminate] = [unframe(data) for data in sent]
    assert terminate[0] == 5 and terminate[2:] == b"\x00\x04"  # LCP Terminate-Request, no data
    # a second IPCP request gets nothing; a Terminate-Ack of another identifier changes nothing
    ppp.receive(control(0x8021, 1, 9, b"\x03\x06\0\0\0\0"))
    ppp.receive(control(0xC021, 6, (terminate[1] + 1) & 0xFF))
    assert len(sent) == 1 and ppp.lcp_open and not ppp.ipcp_open
    assert not ppp.negotiation_failed
    ppp.receive(control(0xC021, 6, terminate[1]))
    assert ppp.negotiation_failed and not ppp.lcp_open and not ppp.ipcp_open
    assert ended == ["far end terminated"] and ppp.ipcp_opened == 0


def test_a_device_that_terminates_a_refused_ipcp_first_ends_the_negotiation_too() -> None:
    sent: list[bytes] = []
    ppp = Ppp(sent.append, lambda datagram: None, refuse="ipcp")
    ppp.start()
    ppp.receive(control(0xC021, 1, 7))
    ppp.receive(control(0xC021, 2, 1))
    ppp.receive(control(0xC021, 5, 8))
    assert unframe(sent[-1]) == b"\x06\x08\x00\x04"
    assert ppp.negotiation_failed


def test_a_device_terminate_request_with_no_refusal_is_no_failed_negotiation() -> None:
    ppp, _, _ = opened()
    ppp.receive(control(0xC021, 5, 21))
    assert not ppp.negotiation_failed
