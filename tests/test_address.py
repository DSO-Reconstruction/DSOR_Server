"""SystemAddress encoding.

RakNet complements the four IPv4 bytes and leaves the port alone.  Reversing the
bytes instead looks just as plausible in a hex dump and fails silently, so the
distinction is worth a test of its own.
"""

import pytest

from raknet.address import (
    SYSTEM_ADDRESS_SIZE,
    SystemAddress,
    decode_address,
    encode_address,
    encode_unspecified,
)


def test_ipv4_is_complemented_not_reversed():
    encoded = encode_address(SystemAddress("192.168.1.47", 1234))
    assert encoded[1:5] == bytes([0x3F, 0x57, 0xFE, 0xD0]), "expected XOR 0xFF"
    assert encoded[1:5] != bytes([47, 1, 168, 192]), "byte reversal is the wrong rule"


def test_port_is_plain_big_endian():
    assert encode_address(SystemAddress("0.0.0.0", 2190))[5:] == b"\x08\x8e"


def test_address_is_seven_bytes():
    assert len(encode_address(SystemAddress("10.0.0.1", 1))) == SYSTEM_ADDRESS_SIZE


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Straight from the capture. The first is the address the client dialled,
        # so decoding it must give back the real server endpoint: the sample
        # validates itself.
        ("04d00a619a088e", ("47.245.158.101", 2190)),
        # The second is the client as the server saw it, i.e. after NAT.
        ("04b18fc4a3ce16", ("78.112.59.92", 52758)),
    ],
)
def test_decodes_real_capture_bytes(raw, expected):
    address, offset = decode_address(bytes.fromhex(raw))
    assert (address.ip, address.port) == expected
    assert offset == SYSTEM_ADDRESS_SIZE
    assert encode_address(address).hex() == raw, "re-encoding must be byte identical"


def test_unspecified_padding_round_trips():
    address, _ = decode_address(encode_unspecified())
    assert (address.ip, address.port) == ("0.0.0.0", 0)


def test_rejects_truncated_buffer():
    with pytest.raises(ValueError, match="SystemAddress"):
        decode_address(bytes.fromhex("04d00a"))


def test_rejects_unknown_ip_version():
    with pytest.raises(ValueError, match="IP version"):
        decode_address(bytes.fromhex("06" + "00" * 6))


def test_rejects_impossible_port():
    with pytest.raises(ValueError, match="port"):
        encode_address(SystemAddress("10.0.0.1", 70000))
