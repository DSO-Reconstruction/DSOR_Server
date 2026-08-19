"""The unconnected handshake, checked against the recorded server's own bytes."""

import pytest

from raknet.address import SystemAddress
from raknet.constants import OFFLINE_MESSAGE_ID, RAKNET_PROTOCOL_VERSION
from raknet.offline import (
    MINIMUM_MTU,
    UDP_IPV4_OVERHEAD,
    HandshakeError,
    build_incompatible_protocol_version,
    build_open_connection_reply_1,
    build_open_connection_reply_2,
    parse_open_connection_request_1,
    parse_open_connection_request_2,
)

#: The GUID the recorded login service used, so replies can be compared byte for
#: byte with what the real server sent.
RECORDED_GUID = bytes.fromhex("00063fb2731c5c79")
RECORDED_MTU = 1292


def test_request_1_reports_protocol_version_and_probed_mtu(by_frame):
    request = parse_open_connection_request_1(by_frame[1])
    assert request.protocol_version == RAKNET_PROTOCOL_VERSION == 5
    # The client pads the datagram so the whole IP packet equals the MTU it is
    # probing, which is how the server learns it without being told.
    assert request.mtu == len(by_frame[1]) + UDP_IPV4_OVERHEAD == RECORDED_MTU


def test_reply_1_matches_the_real_server_byte_for_byte(by_frame):
    assert build_open_connection_reply_1(RECORDED_GUID, RECORDED_MTU) == by_frame[2]


def test_reply_1_is_28_bytes():
    # id(1) + magic(16) + guid(8) + security(1) + mtu(2). Writing four bytes
    # where the security flag and MTU go is a real and fatal mistake: it makes
    # the reply 29 bytes and announces a nonsense MTU.
    assert len(build_open_connection_reply_1(RECORDED_GUID, RECORDED_MTU)) == 28


def test_request_2_names_the_server_the_client_dialled(by_frame):
    request = parse_open_connection_request_2(by_frame[3])
    assert (request.server_address.ip, request.server_address.port) == (
        "47.245.158.101",
        2190,
    )
    assert request.mtu == RECORDED_MTU
    assert request.client_guid == bytes.fromhex("077000013213ce81")


def test_reply_2_matches_the_real_server_byte_for_byte(by_frame):
    # The address echoed back is the client as the server sees it, so behind NAT
    # it is the public endpoint rather than the client's own idea of itself.
    observed_client = SystemAddress("78.112.59.92", 52758)
    assert (
        build_open_connection_reply_2(RECORDED_GUID, observed_client, RECORDED_MTU)
        == by_frame[4]
    )


def test_every_offline_message_carries_the_magic(by_frame):
    for number in (1, 2, 3, 4):
        assert OFFLINE_MESSAGE_ID in by_frame[number]


def test_traffic_without_the_magic_is_refused():
    with pytest.raises(HandshakeError, match="offline message id"):
        parse_open_connection_request_1(bytes([0x05]) + b"\x00" * 32)


def test_wrong_message_id_is_refused():
    with pytest.raises(HandshakeError, match="0x05"):
        parse_open_connection_request_1(bytes([0x07]) + OFFLINE_MESSAGE_ID + b"\x05")


def test_mtu_below_raknet_minimum_is_refused():
    with pytest.raises(HandshakeError, match="minimum"):
        build_open_connection_reply_1(RECORDED_GUID, MINIMUM_MTU - 1)


def test_guid_must_be_eight_bytes():
    with pytest.raises(HandshakeError, match="8 bytes"):
        build_open_connection_reply_1(bytes.fromhex("1234"), RECORDED_MTU)


def test_version_mismatch_reply_states_our_version():
    reply = build_incompatible_protocol_version(RECORDED_GUID)
    assert reply[0] == 0x19
    assert reply[1] == RAKNET_PROTOCOL_VERSION
    assert reply[2:18] == OFFLINE_MESSAGE_ID
