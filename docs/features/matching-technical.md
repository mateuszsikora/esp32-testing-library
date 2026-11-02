# Matching engine: how it works

`esp32tl/matching.py` implements `Protocol`, the constants `AT`, `PMTK`, `UBX`, `GNSS`, and
`Matching(recording, protocol=None)`. It has no dependency on a backend or wire. `Replay` in
`esp32tl/sources/replay.py` inherits it, adding interface and clock compatibility checks and
translating construction errors to `ReplayError`. The [user guide](matching-user.md) describes
protocol selection and the public behaviour.

## Compilation and scheduling

The recording and live wire use separate `_Framer` instances. A framer accumulates outgoing
bytes until its protocol recognises a complete frame. For a byte terminator the frame includes
that terminator; a callable supplies its full length. AT's prompt is recognised across answer
fragments, at the start of a response or after CR/LF. The framer retains the prompt length plus
one preceding byte and recomputes the candidate on each response fragment, so an intermediate
suffix is cancelled when more ordinary text arrives. Once outgoing payload bytes start, the
Ctrl-Z mode stays active until the payload completes, without normalisation.

A CR-terminated command consumes one optional following LF before the response, even across
outgoing chunks. The first nonempty response closes that separator window; a subsequent payload's
leading LF is preserved. Completing a frame resets the response tail so prompts cannot span
different request/answer exchanges.

Each completed recorded command creates an answer list in `answers[key]`, even when that list
stays empty. Subsequent inputs append `(delay, bytes)`, where delay is their timestamp minus the
completed request's timestamp. A new completed command starts a new list. Inputs before the
first command populate `events`, the unprompted `(absolute_time, bytes)` tuple. No payload is
inferred from a silence. Simultaneous commands in one chunk receive distinct occurrences;
following inputs belong to the last command.

`latency()` is the minimum recorded request-to-answer delay. It is zero when a protocol is
active but no answer delay exists, and `None` for a passive stream. `receive(t, data)` frames
requests, advances a separate index per key modulo the number of occurrences, and schedules
each selected fragment in a heap `(t + delay, insertion_order, bytes)`. An unknown key increments
`unseen` and schedules its fallback at `t + latency`. `receive` returns no immediate outputs.

`poll(now)` emits all due unprompted events and pending answers, ordered by timestamp. Equal-time
unprompted events precede answers, and answers retain insertion order. It feeds emitted answer
bytes back to the live prompt recogniser and increments `sent` once per emitted fragment.
`quiet_until()` returns the minimum of the next unprompted event and the earliest pending answer,
or `None` when neither exists. Thus acknowledging a request cannot hide a deferred answer from
the runner's next deadline. BoardLink calls these methods in its turn; its deadline enforcement
prevents polling an event late. Direct callers must likewise respect `quiet_until`.

Looping adds the final recording line's time to the unprompted timeline after each pass; it
never resets per-request indices or drops pending answers. See the
[decision](../decisions/harness.md#request-answers-wrap-independently-of-the-unprompted-timeline).
`Matching` rejects clocks that differ anywhere; `Replay` additionally requires the route's clock.

## Errors and limits

Construction raises `ValueError` for:

- `matching cannot compare times across clocks`;
- `the recording ends with an incomplete request` (non-whitespace buffered bytes);
- `a looping recording with unprompted events must have positive duration`;
- `a UBX request must start with b'\xb5\x62'` (a binary frame with enough header bytes and wrong sync).

`Protocol` rejects empty byte terminators and prompts with `a protocol's terminator must not be
empty` and `a protocol's prompt must not be empty`. A custom frame-length callback must return
`None` or a positive length no larger than the buffer; otherwise framing raises
`a protocol's frame length must be within the buffered bytes`. Live framing errors propagate
through the interface and fail the link as other source errors do.

AT normalises command syntax but preserves quoted values. A prompted payload's key includes its
Ctrl-Z. PMTK keys exclude checksums; fallback responses XOR their ASCII payload and use flag 1
(unsupported packet type). UBX framing reads the little-endian payload length at bytes 4–5 and
adds the eight envelope bytes. Its fallback is class 0x05, ID 0x00, two payload bytes (request
class and ID), and the two-byte running-sum checksum over class through payload, per
[u-blox's interface description](https://content.u-blox.com/sites/default/files/u-blox-D9-QZS-1.01_InterfaceDescription_UBX-21031777.pdf).
The engine deliberately does not classify asynchronous notifications among recorded answers.

## Tests

`tests/test_matching.py` uses synthetic recordings to check independent wrapping, silent
occurrences, stream and pending-answer deadlines, fallback timing, loop boundaries, empty and
zero-duration recordings, fragmented and coalesced commands, exact UBX bytes, PMTK checksums,
AT prompts, quoted values and custom protocols. Regression tests cover prompt-like response text
at every two-chunk split, CR/CRLF command endings (also split across chunks), and preservation
of whitespace at the start of prompted payloads.

`tests/test_replay.py` runs PMTK and UBX configuration requests through `Uart.replay` and
`BoardLink`, including a request split differently from its recording and a deferred answer.
It also checks unseen requests in the backend text report and JSON properties. These tests need
neither emulator nor firmware. The existing GNSS firmware tests continue to exercise passive
streams; `nmea_reader` itself sends no configuration commands.
