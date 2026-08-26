"""The action bar, which is why the skills appeared not to work."""

from dsor.quickslots import EMPTY, QUICK_SLOTS, decode


def a_bar(*names, count=17):
    body = count.to_bytes(4, "little") + bytes(4)
    for index in range(count):
        name = names[index] if index < len(names) else None
        if name is None:
            body += EMPTY
        else:
            body += len(name).to_bytes(2, "little") + name.encode()
    return body + bytes(4)


def test_the_bar_the_client_actually_had_held_one_skill():
    """Which is the whole of "les sorts marchent pas du tout": there was nothing on the
    bar to press, and granting a skill in the *book* does not put it on the *bar*."""
    real = bytes.fromhex(
        "1100000000000000" "0b00" + b"angrystrike".hex() + "ff" * 64 + "03000000"
    )
    got = decode(real)
    assert got is not None
    assert len(got) == 17
    assert got.filled == {0: "angrystrike"}


def test_a_full_bar_from_the_live_service_reads_out():
    """angrystrike, mightybash, warshout, frenzyshout, bloody360 in slots 0, 3, 7, 10
    and 13 — which is what a bar looks like when somebody has put skills on it."""
    body = a_bar(
        "angrystrike", None, None, "mightybash", None, None, None, "warshout",
        None, None, "frenzyshout", None, None, "bloody360",
    )
    got = decode(body)
    assert got.filled == {
        0: "angrystrike",
        3: "mightybash",
        7: "warshout",
        10: "frenzyshout",
        13: "bloody360",
    }


def test_an_empty_bar_is_a_bar_and_not_a_failure():
    got = decode(a_bar())
    assert got is not None
    assert got.filled == {}
    assert len(got) == 17


def test_anything_that_does_not_read_as_a_bar_gives_none():
    """Half a bar is not information, and the trailing bytes are not understood."""
    assert decode(b"") is None
    assert decode(bytes(8)) is None, "a count of zero"
    assert decode((999).to_bytes(4, "little") + bytes(8)) is None, "an absurd count"
    # A length that runs past the end.
    assert decode((1).to_bytes(4, "little") + bytes(4) + (200).to_bytes(2, "little")) is None
    # A length longer than any real skill id.
    assert decode((1).to_bytes(4, "little") + bytes(4) + (60).to_bytes(2, "little") + b"x" * 60) is None


def test_the_opcode_is_the_one_the_client_repeats():
    assert QUICK_SLOTS == 0x0051
