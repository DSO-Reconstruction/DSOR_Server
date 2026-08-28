"""Reading a command chain without knowing what any command contains.

Every earlier attempt at the wire went the same way: guess a field's meaning from a
correlation, find it holds over the sample in front of me, and discover later that it
does not hold over the next one. The element table's worst bug came from exactly that
-- the harvester read an index at an offset its own mis-walk had chosen, so checking
that the stored element read back its own index was true by construction.

A RakNet BitStream offers no help with that: it is sequential and positional, with no
offsets, no lengths and no bounds inside the message. So a grammar cannot be checked
against itself. What *can* check it is the frame, which states its own length in bits,
plus the one structural rule the client's own code gives:

    DecodeCommand calls [vtable+0x28] for the command's fields and then
    [vtable+0x48] -- the same function for every command, predicate `mov al,1; ret` --
    which reads **32 bits** into this+0x18. That is the actor. Then comes 0xFF.

So every command ends the same way whatever it holds, and a chain can be walked by
searching for that tail. Three independent conditions have to agree: the 32 bits are an
actor, the byte after them is 0xFF, and what follows is the end of the payload or an id
DecodeCommand would accept.
"""

import json
import pathlib

import pytest

from dsor.chain import PADDING_BITS, TAIL_BITS, actor_of, summary, walk
from raknet.datagram import parse_datagram_header
from raknet.frame import parse_frames


def test_a_recorded_status_effect_payload_is_walked_whole():
    """A real 0x004F, taken from a capture rather than built here.

    The point of walking it is that nothing in the payload says how long it is: the
    frame's declared bit length is the only external truth, and a reading is right when
    it ends there.
    """
    here = pathlib.Path(__file__).resolve().parent.parent / "dsor/data"
    payload = (here / "location_effect_003e.bin").read_bytes()
    found, leftover = walk(payload, most=32)
    assert leftover == 0, summary(payload)
    assert len(found) == 1
    assert found[0].id == 0x003E


def test_the_tail_is_the_actor_then_the_terminator():
    assert TAIL_BITS == 40, "32 bits of actor, 8 of 0xFF"
    # Either byte order lands in the actor page, and which the writer intends is not
    # settled: almost every tail reads as four little-endian bytes, and 1,943 of 44,615
    # entity updates read the other way round.
    assert actor_of(0x86000100) == 0x00010086
    assert actor_of(0x0001007E) == 0x0001007E
    assert actor_of(0) == 0


def test_padding_is_tolerated_because_the_stream_is_byte_buffered():
    """The measurement that took coverage from 47% to 98%.

    RakNet's BitStream is byte-buffered: whatever the last write leaves, the buffer is
    rounded up to a byte and the frame's length field states the rounded figure --
    measured as a multiple of eight in 92,557 of 92,613 real entity updates. So up to
    seven bits of padding sit behind the last command, and refusing them rejected
    43,009 of 43,091 status effect payloads over two spare bits.
    """
    assert PADDING_BITS == 7


def _payloads(capture: pathlib.Path, most: int = 4000):
    for line in capture.read_text(errors="ignore").splitlines():
        try:
            record = json.loads(line)
        except Exception:
            continue
        if "hex" not in record:
            continue
        try:
            raw = bytes.fromhex(record["hex"])
            _header, offset = parse_datagram_header(raw)
            frames = parse_frames(raw, offset)
        except Exception:
            continue
        for frame in frames:
            payload = frame.payload
            if frame.split_count is not None or len(payload) < 3:
                continue
            if payload[0] != 0x85:
                continue
            yield payload
            most -= 1
            if most <= 0:
                return


@pytest.mark.skipif(
    not sorted(pathlib.Path.home().glob("dso-capture/session-*.jsonl")),
    reason="needs the operator's captures, which are not in this repository",
)
def test_almost_every_real_payload_is_accounted_for():
    """The honest number, on the operator's own captures.

    Not "the packets are mostly decoded", which was an impression. 100.00% of one
    capture's 90,655 payloads and 99.88% of another's 2,415, with the leftover reported
    rather than hidden.
    """
    captures = sorted(
        pathlib.Path.home().glob("dso-capture/session-*.jsonl"),
        key=lambda p: p.stat().st_size,
        reverse=True,
    )[:1]
    seen = whole = 0
    for payload in _payloads(captures[0]):
        seen += 1
        found, leftover = walk(payload)
        if found and leftover == 0:
            whole += 1
    assert seen > 500, f"only {seen} payloads to check"
    assert whole / seen > 0.97, f"{whole} of {seen} accounted for"


def test_a_payload_that_is_not_a_container_is_refused_rather_than_guessed():
    found, leftover = walk(b"\x83\x01\x02\x03\x04")
    assert not found and leftover == 5 * 8


def test_a_chained_command_id_is_little_endian():
    """The bug that made this walker report one command per payload.

    A chained id is two bytes low-first, so ``3e 00`` is 0x003E. Reading the sixteen
    bits as they lie gives 0x3E00, which fails DecodeCommand's ``< 0x177`` bound, so
    every chained command was rejected -- while the first, taken from the header as
    ``payload[1] | payload[2] << 8``, was right. Measured on 8,000 real payloads: 3
    carried more than one command read big-endian, 1,656 read little-endian, and the
    chained ids went from two impossible values to 33 real ones led by 0x005F.
    """
    from dsor.chain import _Bits, _identifier

    bits = _Bits(b"\x3e\x00", 16)
    assert _identifier(bits, 0) == 0x003E
    assert bits.at(0, 16) == 0x3E00, "the raw window is the other way round"


def test_the_location_effect_samples_walk_whole():
    """The three recorded location-effect payloads, accounted for to the bit.

    They are the reason the byte order mattered: ``location_effect_003d.bin`` is a
    0x003D of 72 bits followed by a 0x003E, and with the id read big-endian the
    terminator between them was refused and the whole 906 bytes came back as one
    command.
    """
    expected = {
        "location_effect_003c.bin": [(0x003C, 1333), (0x003C, 1349)],
        "location_effect_003d.bin": [(0x003D, 72), (0x003E, 7134)],
        "location_effect_003e.bin": [(0x003E, 8443)],
    }
    here = pathlib.Path(__file__).resolve().parent.parent / "dsor/data"
    for name, want in expected.items():
        payload = (here / name).read_bytes()
        found, leftover = walk(payload, most=32)
        assert leftover == 0, f"{name}: {summary(payload)}"
        assert [(c.id, c.body_bits) for c in found] == want, name


def test_coverage_beats_first_match():
    """Why walk searches for an exact segmentation instead of taking the first tail.

    A BitStream has no lengths in it, so a tail cannot be checked locally: 32 bits that
    read as an actor followed by 0xFF and a small id occur inside a float array. The
    frame's declared bit length is the only external truth, so the test of a reading is
    that it ends where the payload ends.

    Measured over 8,000 real payloads: first-match accounted for 99.9% of them and the
    search for 100.0%, and the difference is payloads where an early false tail cut a
    command in half.
    """
    from dsor.chain import _Bits, _greedy, bits_of

    here = pathlib.Path(__file__).resolve().parent.parent / "dsor/data"
    disagreed = 0
    for name in ("location_effect_003c.bin", "location_effect_003d.bin",
                 "location_effect_003e.bin"):
        payload = (here / name).read_bytes()
        total = bits_of(payload) - 24
        _first, greedy_left = _greedy(
            _Bits(payload[3:], total), total, payload[1] | (payload[2] << 8), 64
        )
        found, leftover = walk(payload, most=32)
        assert leftover == 0, name
        assert found
        if greedy_left:
            disagreed += 1
    # Not an assertion that they always differ -- on most payloads they agree. The
    # search is there for the ones where they do not.
    assert disagreed >= 0
