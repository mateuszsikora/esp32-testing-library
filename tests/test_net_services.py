"""DNS and NTP against wire bytes, including malformed requests and NTP's era wrap."""

import struct
from datetime import UTC, datetime

import pytest
from conftest import CountingList

from esp32tl.net.dns import Dns, DnsQuestion
from esp32tl.net.ntp import Ntp, NtpExchange
from esp32tl.units import Duration


def test_dns_answers_every_name_but_only_in_a_questions() -> None:
    dns = Dns("10.64.0.1", lambda: 0)
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


def _query(name: bytes, qtype: int = 1, qclass: int = 1, ident: int = 0x1234) -> bytes:
    labels = b"".join(bytes([len(part)]) + part for part in name.split(b".") if part)
    return (
        struct.pack(">HHHHHH", ident, 0x0100, 1, 0, 0, 0)
        + labels
        + b"\0"
        + struct.pack(">HH", qtype, qclass)
    )


def test_dns_answers_nxdomain_for_an_unknown_name_of_every_type() -> None:
    lines: list[str] = []
    dns = Dns("10.64.0.1", lambda: 0, lines.append)
    dns.unknown("broker.example")
    for qtype, qclass in ((1, 1), (28, 1), (16, 1), (255, 1), (1, 3)):
        query = _query(b"broker.example", qtype, qclass)
        # RCODE 3, recursion desired and available, the question again and no record.
        assert dns.on_datagram(query) == b"\x12\x34\x81\x83\0\x01\0\0\0\0\0\0" + query[12:]
    assert [q.answered for q in dns.questions] == ["nxdomain"] * 5
    assert lines == [
        "dns: broker.example is unknown from now on",
        *["dns: broker.example → NXDOMAIN"] * 5,
    ]


def test_dns_resolves_every_other_name_while_one_is_unknown() -> None:
    lines: list[str] = []
    dns = Dns("10.64.0.1", lambda: 0, lines.append)
    dns.unknown("broker.example")
    for name in (b"api.example", b"example", b"mqtt.broker.example", b"broker.example.com"):
        answer = dns.on_datagram(_query(name))
        assert (
            answer is not None and answer[2:4] == b"\x81\x80" and answer[-4:] == b"\x0a\x40\0\x01"
        )
    answer = dns.on_datagram(_query(b"api.example", 28))
    assert answer is not None and answer[2:8] == b"\x81\x80\0\x01\0\0"
    assert [q.answered for q in dns.questions] == ["address"] * 4 + ["no record"]
    assert lines[1:] == [
        "dns: api.example → 10.64.0.1",
        "dns: example → 10.64.0.1",
        "dns: mqtt.broker.example → 10.64.0.1",
        "dns: broker.example.com → 10.64.0.1",
        "dns: api.example → no record",
    ]


def test_dns_unknown_names_ignore_case_and_a_trailing_dot() -> None:
    lines: list[str] = []
    dns = Dns("10.64.0.1", lambda: 0, lines.append)
    dns.unknown("Broker.Example.")
    dns.unknown("API.example")
    # The Kelvin sign lowercases to k in Unicode, not in ASCII.
    dns.unknown("\u212aey.example")
    for name in (b"broker.example", b"BROKER.example", b"api.EXAMPLE", b"key.example"):
        dns.on_datagram(_query(name))
    assert [(q.name, q.answered) for q in dns.questions] == [
        ("broker.example", "nxdomain"),
        ("BROKER.example", "nxdomain"),
        ("api.EXAMPLE", "nxdomain"),
        ("key.example", "address"),
    ]
    assert lines[:2] == [
        "dns: broker.example is unknown from now on",
        "dns: api.example is unknown from now on",
    ]
    with pytest.raises(TypeError, match="dns: name takes text"):
        dns.unknown(b"broker.example")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="dns: name takes text"):
        dns.known(None)  # type: ignore[arg-type]


def test_dns_known_undoes_unknown_for_that_name_alone() -> None:
    lines: list[str] = []
    dns = Dns("10.64.0.1", lambda: 0, lines.append)
    dns.unknown("broker.example")
    dns.unknown("api.example")
    dns.unknown("Broker.example")  # Already unknown: no line.
    dns.known("BROKER.example.")
    dns.known("broker.example")  # Already known: no line.
    dns.known("never.example")
    for name in (b"broker.example", b"api.example"):
        dns.on_datagram(_query(name))
    assert [(q.name, q.answered) for q in dns.questions] == [
        ("broker.example", "address"),
        ("api.example", "nxdomain"),
    ]
    assert lines == [
        "dns: broker.example is unknown from now on",
        "dns: api.example is unknown from now on",
        "dns: broker.example is known again",
        "dns: broker.example → 10.64.0.1",
        "dns: api.example → NXDOMAIN",
    ]


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


def test_ntp_silence_and_resume_preserve_time_and_reply_count() -> None:
    now = [0]
    events: list[str] = []
    ntp = Ntp(lambda: now[0], lambda: datetime(2031, 5, 6, tzinfo=UTC), events.append)
    query = b"\x23" + b"\0" * 39 + b"abcdefgh"
    first = ntp.on_datagram(query)
    assert first is not None and ntp.requests == 1

    ntp.go_silent()
    ntp.go_silent()
    now[0] = 10_000_000_000
    assert ntp.on_datagram(query) is None
    assert ntp.requests == 1
    ntp.resume()
    ntp.resume()
    assert ntp.requests == 1  # Resume itself emits no answer or backlog.
    resumed = ntp.on_datagram(query)
    assert resumed is not None and ntp.requests == 2
    assert struct.unpack(">II", resumed[40:])[0] == struct.unpack(">II", first[40:])[0] + 10
    assert events == ["ntp: answered", "ntp: silent", "ntp: resumed", "ntp: answered"]


def test_ntp_can_be_silent_before_its_first_request() -> None:
    ntp = Ntp(lambda: 0)
    ntp.go_silent()
    assert ntp.on_datagram(b"\x23" + b"\0" * 47) is None
    assert ntp.requests == 0


QUERY = b"\x23" + b"\0" * 39 + b"abcdefgh"


def test_ntp_keeps_every_request_with_its_time_and_whether_it_was_answered() -> None:
    now = [1_000_000_000]
    ntp = Ntp(lambda: now[0])
    assert ntp.on_datagram(QUERY) is not None
    ntp.go_silent()
    for t in (16_000_000_000, 31_000_000_000):
        now[0] = t
        assert ntp.on_datagram(QUERY) is None
    ntp.resume()
    now[0] = 40_000_000_000
    assert ntp.on_datagram(QUERY) is not None
    # Neither a short datagram, a server's packet nor an NTP v2 request is a request.
    assert ntp.on_datagram(QUERY[:47]) is None
    assert ntp.on_datagram(b"\x24" + QUERY[1:]) is None
    assert ntp.on_datagram(b"\x13" + QUERY[1:]) is None
    assert ntp.exchanges == [
        NtpExchange(Duration(1_000_000_000), answered=True, synchronised=True),
        NtpExchange(Duration(16_000_000_000), answered=False, synchronised=None),
        NtpExchange(Duration(31_000_000_000), answered=False, synchronised=None),
        NtpExchange(Duration(40_000_000_000), answered=True, synchronised=True),
    ]
    assert ntp.requests == 2


def test_ntp_next_exchange_is_a_described_condition_from_its_call() -> None:
    now = [0]
    ntp = Ntp(lambda: now[0])
    exchanges = ntp.exchanges
    ntp.on_datagram(QUERY)
    assert len(exchanges) == 1
    any_request = ntp.next_exchange()
    unanswered = ntp.next_exchange(answered=False)
    answered = ntp.next_exchange(answered=True)
    assert (repr(any_request), repr(unanswered), repr(answered)) == (
        "the next NTP request",
        "the next unanswered NTP request",
        "the next answered NTP request",
    )
    assert any_request() is None and unanswered() is None and answered() is None
    with pytest.raises(TypeError, match=r"call the NTP condition with \(\)"):
        bool(any_request)
    ntp.go_silent()
    now[0] = 15_000_000_000
    ntp.on_datagram(QUERY)
    silent = NtpExchange(Duration(15_000_000_000), answered=False, synchronised=None)
    assert any_request() == silent and unanswered() == silent and answered() is None
    ntp.resume()
    now[0] = 30_000_000_000
    ntp.on_datagram(QUERY)
    # Non-consuming: the first match stays the answer.
    assert any_request() == silent and unanswered() == silent
    assert answered() == NtpExchange(Duration(30_000_000_000), answered=True, synchronised=True)
    with pytest.raises(TypeError, match="ntp: answered takes True, False or None"):
        ntp.next_exchange(answered=1)  # type: ignore[arg-type]


SYNCHRONISED = struct.pack(">BBbbII4s", 0x24, 1, 0, -20, 0, 0x10, b"ESPT")
UNSYNCHRONISED = struct.pack(">BBbbII4s", 0xE4, 0, 0, -20, 0, 0x10, b"INIT")


def test_ntp_answers_unsynchronised_with_leap_3_stratum_0_and_the_worlds_time() -> None:
    now = [2_500_000_000]
    world = datetime(2031, 5, 6, tzinfo=UTC)
    ntp = Ntp(lambda: now[0], lambda: world)
    synchronised = ntp.on_datagram(QUERY)
    assert synchronised is not None and synchronised[:16] == SYNCHRONISED
    ntp.unsynchronised()
    answer = ntp.on_datagram(QUERY)
    assert answer is not None and len(answer) == 48
    assert answer[:16] == UNSYNCHRONISED
    # A server never synchronised has no reference time; the origin is the client's and the
    # receive and transmit stamps are still the world's.
    assert answer[16:24] == bytes(8) and synchronised[16:24] != bytes(8)
    assert answer[24:] == synchronised[24:]
    assert answer[24:32] == b"abcdefgh"
    seconds = int((world - datetime(1900, 1, 1, tzinfo=UTC)).total_seconds()) + 2
    assert answer[40:] == struct.pack(">II", seconds, 0x80000000)


def test_ntp_switches_between_synchronised_and_unsynchronised_answers() -> None:
    events: list[str] = []
    ntp = Ntp(lambda: 0, log=events.append)
    ntp.synchronised()  # Already the default: no line.
    ntp.unsynchronised()
    ntp.unsynchronised()
    answer = ntp.on_datagram(QUERY)
    assert answer is not None and answer[:16] == UNSYNCHRONISED
    ntp.synchronised()
    ntp.synchronised()
    answer = ntp.on_datagram(QUERY)
    assert answer is not None and answer[:16] == SYNCHRONISED
    ntp.unsynchronised()
    answer = ntp.on_datagram(QUERY)
    assert answer is not None and answer[:16] == UNSYNCHRONISED
    assert ntp.requests == 3
    assert events == [
        "ntp: unsynchronised",
        "ntp: answered unsynchronised",
        "ntp: synchronised",
        "ntp: answered",
        "ntp: unsynchronised",
        "ntp: answered unsynchronised",
    ]


def test_ntp_go_silent_wins_over_unsynchronised() -> None:
    now = [0]
    ntp = Ntp(lambda: now[0])
    ntp.unsynchronised()
    ntp.go_silent()
    assert ntp.on_datagram(QUERY) is None
    ntp.synchronised()
    now[0] = 1_000_000_000
    assert ntp.on_datagram(QUERY) is None
    ntp.unsynchronised()
    ntp.resume()
    now[0] = 2_000_000_000
    answer = ntp.on_datagram(QUERY)
    assert answer is not None and answer[:16] == UNSYNCHRONISED
    assert ntp.exchanges == [
        NtpExchange(Duration(0), answered=False, synchronised=None),
        NtpExchange(Duration(1_000_000_000), answered=False, synchronised=None),
        NtpExchange(Duration(2_000_000_000), answered=True, synchronised=False),
    ]
    assert ntp.requests == 1


def test_ntp_exchanges_say_which_answers_were_unsynchronised() -> None:
    now = [0]
    ntp = Ntp(lambda: now[0])
    unanswered = ntp.next_exchange(answered=False)
    for t, call in (
        (1_000_000_000, ntp.synchronised),
        (2_000_000_000, ntp.unsynchronised),
        (3_000_000_000, ntp.go_silent),
        (4_000_000_000, ntp.resume),
        (5_000_000_000, ntp.synchronised),
    ):
        call()
        now[0] = t
        ntp.on_datagram(QUERY)
    assert [(e.answered, e.synchronised) for e in ntp.exchanges] == [
        (True, True),
        (True, False),
        (False, None),
        (True, False),
        (True, True),
    ]
    assert unanswered() == NtpExchange(Duration(3_000_000_000), False, None)


def test_dns_keeps_every_question_it_parsed_with_its_time_and_type() -> None:
    now = [2_000_000_000]
    dns = Dns("10.64.0.1", lambda: now[0])
    questions, asked = dns.questions, dns.asked
    question = b"\x04time\x04test\0\0\x01\0\x01"
    query = b"\x12\x34\x01\0\0\x01" + b"\0" * 6 + question
    dns.on_datagram(query)
    now[0] = 62_000_000_000
    dns.on_datagram(query[:-4] + b"\0\x1c\0\x01")
    # Neither a truncated query, a response, another opcode nor two questions is a question.
    assert dns.on_datagram(query[:-1]) is None
    assert dns.on_datagram(query[:2] + b"\x81\x00" + query[4:]) is None
    assert dns.on_datagram(query[:2] + b"\x11\x00" + query[4:]) is None
    assert dns.on_datagram(query[:4] + b"\0\x02" + query[6:] + question) is None
    assert questions == [
        DnsQuestion(Duration(2_000_000_000), "time.test", 1, "address"),
        DnsQuestion(Duration(62_000_000_000), "time.test", 28, "no record"),
    ]
    assert asked == ["time.test", "time.test"]


def test_dns_next_question_is_a_described_condition_from_its_call() -> None:
    now = [0]
    dns = Dns("10.64.0.1", lambda: now[0])

    def query(name: bytes) -> bytes:
        labels = b"".join(bytes([len(part)]) + part for part in name.split(b"."))
        return b"\0\x01\x01\0\0\x01" + b"\0" * 6 + labels + b"\0\0\x01\0\x01"

    dns.on_datagram(query(b"time.test"))
    any_question = dns.next_question()
    time_test = dns.next_question("time.test")
    assert (repr(any_question), repr(time_test)) == (
        "the next DNS question",
        "the next DNS question for 'time.test'",
    )
    assert any_question() is None and time_test() is None
    with pytest.raises(TypeError, match=r"call the DNS condition with \(\)"):
        bool(time_test)
    now[0] = 5_000_000_000
    dns.on_datagram(query(b"broker.test"))
    now[0] = 9_000_000_000
    dns.on_datagram(query(b"time.test"))
    now[0] = 12_000_000_000
    dns.on_datagram(query(b"time.test"))
    assert any_question() == DnsQuestion(Duration(5_000_000_000), "broker.test", 1, "address")
    assert time_test() == DnsQuestion(Duration(9_000_000_000), "time.test", 1, "address")
    with pytest.raises(TypeError, match="dns: name takes text or None"):
        dns.next_question(b"time.test")  # type: ignore[arg-type]


N = 10_000


def test_a_next_exchange_poll_reads_only_the_exchanges_since_the_last_one() -> None:
    ntp = Ntp(lambda: 0)
    ntp._exchanges = history = CountingList[NtpExchange]()
    ntp.go_silent()
    answered = ntp.next_exchange(answered=True)
    for _ in range(N):
        ntp.on_datagram(QUERY)
        assert answered() is None
    assert history.reads == N
    ntp.resume()
    ntp.on_datagram(QUERY)
    exchange = history[-1]
    assert answered() is exchange and answered() is exchange
    assert history.reads == N + 1 + 2


def test_a_next_question_poll_reads_only_the_questions_since_the_last_one() -> None:
    dns = Dns("10.64.0.1", lambda: 0)
    dns._questions = history = CountingList[DnsQuestion]()
    other = b"\0\x01\x01\0\0\x01" + b"\0" * 6 + b"\x06broker\x04test\0\0\x01\0\x01"
    time_test = dns.next_question("time.test")
    for _ in range(N):
        dns.on_datagram(other)
        assert time_test() is None
    assert history.reads == N
    dns.on_datagram(b"\0\x01\x01\0\0\x01" + b"\0" * 6 + b"\x04time\x04test\0\0\x01\0\x01")
    question = history[-1]
    assert time_test() is question and time_test() is question
    assert history.reads == N + 1 + 2
