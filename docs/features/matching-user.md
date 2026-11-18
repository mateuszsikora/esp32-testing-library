# Matching recorded requests

`gnss.replay(path)` and `board.uart(role).replay(path)` can answer commands from a recording.
Each complete outgoing command is matched against its own recorded occurrences. Repeated calls
use successive answers, wrapping to the first. An occurrence with no answer stays silent, so
firmware retry and timeout behaviour can be tested.

## Protocols

```python
from esp32tl import AT, PMTK, UBX, GNSS, Protocol


def test_configuration(board):
    replay = board.gnss.replay("receiver.jsonl", protocol=PMTK)
    board.power_on()
    board.console.expect_exact("configured")
    assert replay.unseen_count == 0
```

The optional `protocol=` selects framing and matching. Without it, recorded requests beginning
with `$` or UBX sync select `GNSS`; other requests select `AT`. With no recorded requests and no
explicit protocol, the replay is a passive stream and ignores firmware transmissions. Passing a
protocol explicitly enables fallback responses even for such a stream.

| Constant | Request boundary | Matching | Unknown response |
|---|---|---|---|
| `AT` | `\r` with an optional following `\n`; a standalone `> ` prompt begins a Ctrl-Z payload | Uppercase and remove whitespace outside quoted strings; preserve quoted values and prompted payload bytes | `\r\nERROR\r\n` |
| `PMTK` | `\r\n` | Strip outer whitespace and checksum; uppercase the sentence | `$PMTK001,<cmd>,1*<checksum>\r\n`; silence for a non-PMTK line |
| `UBX` | Length from the binary frame header | Exact bytes, including checksum | UBX-ACK-NAK with the request's class and ID and a computed checksum |
| `GNSS` | PMTK lines or UBX frames on the same wire | As above, per frame | As above, per frame |

Input chunks may split a request anywhere, or contain several complete requests. Responses retain
exactly their recorded bytes; AT responses normally use `\r\n`. UBX payload and checksum bytes
are opaque: the engine frames by length but does not reject an intentionally corrupt checksum.

Custom protocols use `Protocol(terminator=b"!", prompt=None, normalise=lambda b: b.lower(),
unknown_reply=b"error!")`. `terminator` can instead be a callable taking buffered bytes and
returning the length of a complete first frame, or `None`. `unknown_reply` can be a callable
from raw request bytes (including its terminator) to response bytes; an empty result is silence.
`normalise` receives the complete request and returns its dictionary key. `prompt` is an optional
answer suffix at a line boundary (start of the response, or after CR/LF) that switches the next
request to opaque Ctrl-Z framing, useful for AT data entry. Further response bytes cancel a
transient prompt until the payload starts, so `value > text` and `> text` are ordinary text even
when a chunk ends just after `> `. The prompt itself can span chunks.

For CR-terminated commands, one immediately following LF is part of the separator, including
when CR and LF arrive in separate chunks before the response. Once a response starts, the
separator is closed. Leading LF, spaces and CR/LF in a subsequent prompted payload remain data;
changing a command from CRLF to CR does not change its payload's matching key.

## Timing and diagnostics

A command's time is the timestamp of its final fragment. Every recorded answer fragment keeps
its delay from that time, including gaps between fragments. Unknown requests receive the fallback
at command time plus the shortest recorded answer delay (zero if there are no recorded answers).
No response is sent before its scheduled time.

Input events before the first complete recorded request form the unprompted timeline, starting
at power-on. Every later input belongs to the preceding request until the next complete request;
the recording has no metadata to distinguish unsolicited traffic or pipelined responses. Use a
model when that grouping cannot describe your protocol.

`end: hold` stops the unprompted timeline after its last event. `end: loop` repeats it with a
period equal to the recording's final timestamp. Events at zero in the next cycle follow final
events of the preceding cycle at the same time. A nonempty unprompted loop needs positive
duration. Empty loops stay silent. Request matching and pending answers continue under either
end policy.

The returned replay exposes `sent` (emitted fragments), `unseen_count` (total unmatched commands)
and `unseen` (normalised request bytes mapped to their counts). `unseen_events` retains every
unknown occurrence as `(t_ns, request_bytes)`, in arrival order, including repeated requests.
The time is when the request's final fragment arrived; the bytes are the complete frame before
normalisation, including its terminator (the optional LF after an AT CR separator is not part
of the frame). Occurrences are retained even when the fallback is silence. Unknown requests are
listed in the [backend report](esp32sim-backend-user.md#requests-absent-from-a-replay), but do not by themselves
fail a test.

The occurrence history survives the rolling wire log dropping old entries. It is unbounded for
the run, so long runs with many unknown requests consume memory and produce larger reports.

See [replay](replay-user.md) for paths, clocks, route selection and setup errors, and
[the implementation](matching-technical.md) for scheduling details.
